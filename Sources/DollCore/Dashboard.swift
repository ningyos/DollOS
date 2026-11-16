import Foundation
import Network
import Synchronization

/// The owner's dashboard (2026-11-09): a web page served by this process, her vision, memory and
/// actions updated live over SSE, plus the system operations, interrupt (cancel the model call in
/// flight) and pause/resume (no new turns; events keep queueing), and (2026-11-15) marking a memory
/// deep or not, and editing her calendar (each edit a 【行事曆】 program event in her timeline, no
/// turn). Nothing here can say anything to her or change a memory's text: the only POST routes
/// are those operations.

/// Wakes the dashboard's live connections: every appended event, a turn starting or ending, the
/// queue growing, pause or resume, an interrupt. Bursts coalesce: a slow subscriber gets the newest
/// change only, and refetches everything anyway.
public final class ChangeFeed: Sendable {
    public enum Change: String, Sendable { case event, turn, queue, pause, interrupt, calendar, maintenance, drives }

    private let subscribers = Mutex<[UUID: AsyncStream<Change>.Continuation]>([:])

    public init() {}

    public func notify(_ c: Change) {
        subscribers.withLock { for s in $0.values { s.yield(c) } }
    }

    public func subscribe() -> (id: UUID, stream: AsyncStream<Change>) {
        let (stream, cont) = AsyncStream.makeStream(of: Change.self, bufferingPolicy: .bufferingNewest(1))
        let id = UUID()
        subscribers.withLock { $0[id] = cont }
        return (id, stream)
    }

    public func unsubscribe(_ id: UUID) {
        subscribers.withLock { $0.removeValue(forKey: id) }?.finish()
    }

    public var count: Int { subscribers.withLock { $0.count } }
}

// what the page shows ------------------------------------------------------------

/// The pinned status bar.
public struct DashboardStatus: Codable, Sendable, Equatable {
    public var name: String
    /// Her todo phase (`TodoPhase.label`).
    public var todo: String
    /// The item in progress, waiting or failed; nil while idle.
    public var item: String?
    /// The items after it.
    public var pending: [String]
    /// Rounds the item has run, whether it waits for her choice, and her reasons to go on (design C, 2026-11-16).
    public var rounds: Int
    public var deciding: Bool
    public var goOn: [String]
    /// The footer's context figure: the estimate scaled by the last real prompt size.
    public var contextTokens: Int
    /// thinking (model call in flight) | acting (her tool calls) | idle (waiting for the next
    /// event) | maintenance (the daily system maintenance runs, `Maintenance.swift`)
    public var state: String
    /// While maintenance runs (since when) or is due and waits (`Doll.maintenanceLine`).
    public var maintenance: String?
    public var paused: Bool
    /// The effort of the call in flight, else the one the next call will use.
    public var effort: String
    /// Events waiting for her next turn.
    public var queued: Int
    public var turn: Int
    public var lastInterrupt: Double?
}

public struct DashboardCall: Codable, Sendable, Equatable {
    public struct Parameter: Codable, Sendable, Equatable {
        public var name: String
        public var value: String
    }
    public var name: String
    public var parameters: [Parameter]
    public var malformed: String?
    /// Her call's result, verbatim; nil while it has not been stored yet.
    public var result: String?
    public var image: String?
    public var window: String?
    public var secret: Bool
    /// A well-formed speaking call's channel (2026-11-14), from its name: Discord 私訊 | Discord 語音 |
    /// 終端機; nil for other calls. Whether it was delivered is in `result`.
    public var channel: String?
}

/// One timeline item: a turn (her reply with its calls and their results), an owner or developer
/// line, a watch wake, or a program event (【相關記憶】, 【快捷鍵】).
public struct DashboardItem: Codable, Sendable, Equatable {
    public var type: String          // turn | owner | developer | watch | program
    public var seq: Int64
    public var ts: Double
    public var secret: Bool
    public var text: String?         // owner / developer / watch / program: the event text
    public var voice: Bool?          // owner: spoken or typed
    public var image: String?        // watch: its screenshot
    public var thought: String?      // turn: her own thought, the text outside her calls (her words to people are the speaking calls)
    public var footer: String?       // turn: the 【狀況】 she answered
    public var calls: [DashboardCall]?
    public var seconds: Double?      // turn: model call and tool calls
    public var prompt: Int?
    public var cached: Int?
    public var effort: String?
}

/// The latest window read: its screenshot and element list.
public struct DashboardVision: Codable, Sendable, Equatable {
    public var seq: Int64?
    public var ts: Double?
    public var image: String?
    public var window: String?
}

public struct DashboardMemoryEntry: Codable, Sendable, Equatable {
    public var id: String
    public var folder: String
    public var title: String
    public var text: String
    public var kind: String
    public var confidence: Double
    public var level: String
    public var tier: String
    public var ts: Double
    /// Memory strength (2026-11-15): her deep mark, when it settled, its evidence (when, oldest
    /// first), its strength now (nil for tiers without one), and whether it has faded below
    /// `[select] strength_floor` (no longer attached; deep or settled ones never fade).
    public var deep: Bool
    public var settled: Double?
    public var evidence: [Double]
    public var strength: Double?
    public var faded: Bool
}

public struct DashboardMemory: Codable, Sendable, Equatable {
    public struct Folder: Codable, Sendable, Equatable {
        public var folder: String        // secret | own | public
        public var dir: String           // its directory name
        public var entries: [DashboardMemoryEntry]
    }
    public struct Sheet: Codable, Sendable, Equatable {
        public var seq: Int64
        public var title: String
        public var text: String
    }
    public var folders: [Folder]
    /// Experiences the program attached since the owner's last line.
    public var attached: [DashboardMemoryEntry]
    /// Every deep memory, in all folders, newest first.
    public var deep: [DashboardMemoryEntry]
    /// 【快捷鍵】 events since the owner's last line.
    public var cheatsheets: [Sheet]
}

extension Doll {
    public func dashboardStatus() throws -> DashboardStatus {
        DashboardStatus(
            name: name, todo: todo.phase.label, item: todo.current, pending: todo.pending,
            rounds: todo.rounds, deciding: todo.deciding, goOn: todo.goOn,
            contextTokens: Int(Double(try estimateContext()) * tokenRatio),
            state: inflight != nil ? "thinking" : inTurn ? "acting" : inMaintenance ? "maintenance" : "idle", maintenance: maintenanceLine(),
            paused: paused, effort: inflightEffort ?? nextEffort, queued: queue.count, turn: turnIndex,
            lastInterrupt: lastInterrupt)
    }

    /// The newest `limit` items, newest first. Her reply's stored calls are
    /// matched in order to the results stored after it, the way `callNames` does.
    public func dashboardTimeline(limit: Int) throws -> [DashboardItem] {
        let stats = timeline.turnStats
        var items: [DashboardItem] = []
        var open: Int?   // the index of the turn whose results are still coming
        for e in timeline.events {
            let seq = e.seq!
            switch e.kind {
            case .doll:
                let s = stats[seq]
                items.append(DashboardItem(
                    type: "turn", seq: seq, ts: e.ts, secret: e.secret,
                    thought: e.text.trimmingCharacters(in: .whitespacesAndNewlines), footer: e.footer,
                    calls: e.calls.map {
                        DashboardCall(name: $0.name, parameters: $0.parameters.map { .init(name: $0.name, value: $0.value) },
                                      malformed: $0.malformed, secret: false,
                                      channel: $0.malformed == nil ? Tools.speaking[$0.name]?.label : nil)
                    },
                    seconds: s?.seconds, prompt: s?.prompt, cached: s?.cached, effort: s?.effort))
                open = items.count - 1
            case .tool:
                guard let i = open, let at = items[i].calls?.firstIndex(where: { $0.result == nil }) else {
                    // A result with no call left to answer: shown alone rather than dropped.
                    items.append(DashboardItem(type: "program", seq: seq, ts: e.ts, secret: e.secret, text: e.text))
                    continue
                }
                items[i].calls![at].result = e.text
                items[i].calls![at].image = e.image
                items[i].calls![at].window = e.window
                items[i].calls![at].secret = e.secret
                items[i].secret = items[i].secret || e.secret
            case .owner, .developer:
                items.append(DashboardItem(type: e.kind.rawValue, seq: seq, ts: e.ts, secret: e.secret, text: e.text,
                                           voice: e.kind == .owner ? e.voice : nil))
                open = nil
            case .watch:
                items.append(DashboardItem(type: "watch", seq: seq, ts: e.ts, secret: e.secret, text: e.text, image: e.image))
                open = nil
            case .memory, .calendar:
                items.append(DashboardItem(type: "program", seq: seq, ts: e.ts, secret: e.secret, text: e.text))
            }
        }
        return Array(items.suffix(limit).reversed())
    }

    public func dashboardVision() throws -> DashboardVision {
        let events = timeline.events
        var out = DashboardVision()
        if let e = events.last(where: { $0.image != nil || $0.window != nil }) {
            out = DashboardVision(seq: e.seq, ts: e.ts, image: e.image, window: e.window)
        }
        return out
    }

    public func dashboardMemory() throws -> DashboardMemory {
        let now = Date().timeIntervalSince1970
        func entry(_ id: String, _ folder: String, _ f: MemoryFile) -> DashboardMemoryEntry {
            let has = Memory.strengthTiers.contains(f.tier)
            let strength = has ? Memory.activation(f.allEvidence.map(\.ts), now: now, decay: memoryConfig.strengthDecay,
                                                   unitDays: memoryConfig.strengthUnitDays) : nil
            let fading = memory.fadingStrength(tier: f.tier, evidence: f.allEvidence, deep: f.deep, settled: f.settled, now: now)
            return DashboardMemoryEntry(id: id, folder: folder, title: String(f.body.split(separator: "\n").first ?? ""),
                                        text: f.body, kind: f.kind, confidence: f.confidence, level: memory.level(f.confidence),
                                        tier: f.tier.rawValue, ts: f.ts, deep: f.deep, settled: f.settled,
                                        evidence: f.allEvidence.map(\.ts), strength: strength,
                                        faded: fading.map { $0 < pipeline.config.strengthFloor } ?? false)
        }
        let folders = try [MemoryFolder.own, .public, .secret].map { f in
            DashboardMemory.Folder(folder: f.rawValue, dir: memory.dirName(f),
                                   entries: try memory.list(f).map { entry($0.id, f.rawValue, $0.file) })
        }
        let attached = try attachedIDs.sorted().compactMap { id in
            try memory.experience(id).map { entry($0.id, $0.folder, $0.file) }
        }
        let events = timeline.events
        let since = events.last { $0.speaker != nil }?.seq ?? 0
        let sheets = events.filter { $0.kind == .memory && $0.seq! > since && $0.text.hasPrefix("【快捷鍵】") }.map {
            DashboardMemory.Sheet(seq: $0.seq!, title: String($0.text.split(separator: "\n").first ?? ""), text: $0.text)
        }
        let deep = folders.flatMap { f in f.entries.filter(\.deep) }.sorted { ($0.ts, $1.id) > ($1.ts, $0.id) }
        return DashboardMemory(folders: folders, attached: attached, deep: deep, cheatsheets: sheets)
    }

    /// The dashboard's mark (2026-11-15): memory `id` deep or not, through `Memory` (its file; the
    /// index holds no mark).
    public func dashboardSetDeep(_ id: String, _ deep: Bool) throws {
        try memory.setDeep(id, deep)
        log("INFO", "dashboard: memory \(id) \(deep ? "marked deep" : "no longer deep")")
    }
}

// HTTP ---------------------------------------------------------------------------

/// The page's HTTP server: Network.framework, HTTP/1.1, one request per connection (`/events` stays
/// open as an SSE stream).
public final class Dashboard: @unchecked Sendable {
    public enum Kind: Sendable, Equatable { case page, status, timeline, vision, memory, shot, events, interrupt, pause, resume, deep, undeep, calendar, calendarEdit, drives, goals, idealEdit, goalEdit, recordEval }

    /// Every route. A path ending in `/` matches by prefix (the rest is a screenshot's file name or a memory id).
    /// The POST routes are the system operations, and only those (spec 3.x, 2026-11-09).
    public static let routes: [(method: String, path: String, kind: Kind)] = [
        ("GET", "/", .page),
        ("GET", "/api/status", .status),
        ("GET", "/api/timeline", .timeline),
        ("GET", "/api/vision", .vision),
        ("GET", "/api/memory", .memory),
        ("GET", "/shots/", .shot),
        ("GET", "/events", .events),
        ("POST", "/api/interrupt", .interrupt),
        ("POST", "/api/pause", .pause),
        ("POST", "/api/resume", .resume),
        ("POST", "/api/memory/deep/", .deep),
        ("POST", "/api/memory/undeep/", .undeep),
        ("GET", "/api/calendar", .calendar),
        // The owner's calendar edits (2026-11-15): `add`, `change`, `delete`, with her tool's
        // parameters in the query string (`?title=…&date=2026-11-18&start=10:00`).
        ("POST", "/api/calendar/", .calendarEdit),
        // Her signals, concerns, wanderings and associations (m6-drives): read only.
        ("GET", "/api/drives", .drives),
        // Her direction (phase 2): ideal self, tree, record. The owner's edits: `ideal/edit?text=`,
        // `ideal/revert?id=`, `goals/add|change|done|drop?text=&parent=&id=`, `record/eval?id=&kind=good|bad|none`.
        ("GET", "/api/goals", .goals),
        ("POST", "/api/ideal/", .idealEdit),
        ("POST", "/api/goals/", .goalEdit),
        ("POST", "/api/record/", .recordEval),
    ]

    public struct Response: Sendable {
        public var status: Int
        public var contentType: String
        public var body: Data

        static func json<T: Encodable>(_ value: T, status: Int = 200) -> Response {
            let enc = JSONEncoder()
            enc.outputFormatting = .withoutEscapingSlashes
            do {
                return Response(status: status, contentType: "application/json; charset=utf-8", body: try enc.encode(value))
            } catch {
                return .text(500, "encoding failed: \(error)")
            }
        }

        static func text(_ status: Int, _ s: String) -> Response {
            Response(status: status, contentType: "text/plain; charset=utf-8", body: Data(s.utf8))
        }
    }

    let doll: Doll
    let config: DashboardConfig
    let queue = DispatchQueue(label: "dollcore.dashboard")
    private var listener: NWListener?

    public init(doll: Doll, config: DashboardConfig) {
        self.doll = doll
        self.config = config
    }

    /// The route a request names, and the rest of the path after a prefix route.
    public static func match(method: String, path: String) -> (Kind, String)? {
        for r in routes where r.method == method {
            if r.path.hasSuffix("/"), r.path != "/" {
                if path.hasPrefix(r.path) { return (r.kind, String(path.dropFirst(r.path.count))) }
            } else if path == r.path {
                return (r.kind, "")
            }
        }
        return nil
    }

    /// The query string's parameters, percent-decoded (`+` is a space, as a form sends it).
    public static func query(_ target: String) -> [String: String] {
        guard let q = target.firstIndex(of: "?") else { return [:] }
        var out: [String: String] = [:]
        for pair in target[target.index(after: q)...].split(separator: "&") {
            let kv = pair.split(separator: "=", maxSplits: 1).map { $0.replacingOccurrences(of: "+", with: " ").removingPercentEncoding ?? String($0) }
            if let k = kv.first, !k.isEmpty { out[k] = kv.count > 1 ? kv[1] : "" }
        }
        return out
    }

    /// A screenshot's file name as the page asks for it: one plain name, nothing that climbs or roots.
    public static func validShotName(_ raw: String) -> String? {
        guard let name = raw.removingPercentEncoding, !name.isEmpty, !name.hasPrefix("."), !name.contains(".."),
              name.allSatisfy({ $0.isASCII && ($0.isLetter || $0.isNumber || $0 == "." || $0 == "-" || $0 == "_") })
        else { return nil }
        return name
    }

    /// Answers one request (everything but `/events`, which streams).
    public func respond(method: String, target: String) async -> Response {
        let path = String(target.prefix { $0 != "?" })
        guard let (kind, rest) = Self.match(method: method, path: path) else {
            let known = Self.routes.contains { $0.path == path || ($0.path.hasSuffix("/") && $0.path != "/" && path.hasPrefix($0.path)) }
            return .text(known ? 405 : 404, known ? "method not allowed" : "not found")
        }
        do {
            switch kind {
            case .page:
                return Response(status: 200, contentType: "text/html; charset=utf-8", body: Data(DashboardPage.html.utf8))
            case .status: return .json(try await doll.dashboardStatus())
            case .timeline: return .json(try await doll.dashboardTimeline(limit: config.timelineItems))
            case .vision: return .json(try await doll.dashboardVision())
            case .memory: return .json(try await doll.dashboardMemory())
            case .shot:
                guard let name = Self.validShotName(rest) else { return .text(400, "bad screenshot name") }
                let url = doll.shots.appending(path: name)
                guard let data = try? Data(contentsOf: url) else { return .text(404, "no such screenshot") }
                return Response(status: 200, contentType: name.hasSuffix(".png") ? "image/png" : "image/jpeg", body: data)
            case .interrupt:
                let done = await doll.interrupt()
                return .json(["interrupted": done], status: done ? 200 : 409)
            case .pause:
                await doll.pause()
                return .json(["paused": true])
            case .resume:
                await doll.resume()
                return .json(["paused": false])
            case .deep, .undeep:
                guard let id = Self.validShotName(rest) else { return .text(400, "bad memory id") }
                do { try await doll.dashboardSetDeep(id, kind == .deep) } catch let e as MemoryError { return .text(409, e.description) }
                return .json(["id": id, "deep": kind == .deep ? "true" : "false"])
            case .calendar: return .json(await doll.dashboardCalendar())
            case .drives: return .json(await doll.dashboardDrives())
            case .goals: return .json(await doll.dashboardGoals())
            case .idealEdit, .goalEdit, .recordEval:
                let q = Self.query(target)
                do {
                    switch (kind, rest) {
                    case (.idealEdit, "edit"): try await doll.dashboardIdealEdit(text: q["text"] ?? "")
                    case (.idealEdit, "revert"):
                        guard let id = q["id"].flatMap({ Int($0) }) else { return .text(400, "id needed") }
                        try await doll.dashboardIdealRevert(id)
                    case (.goalEdit, let op) where ["add", "change", "done", "drop"].contains(op):
                        return .json(try await doll.dashboardGoalEdit(op, q))
                    case (.recordEval, "eval"):
                        guard let id = q["id"].flatMap({ Int($0) }) else { return .text(400, "id needed") }
                        return .json(try await doll.dashboardEvaluate(id, q["kind"] ?? ""))
                    default: return .text(404, "not found")
                    }
                } catch let e as ToolError { return .text(409, e.description) }
                return .json(await doll.dashboardGoals())
            case .calendarEdit:
                guard ["add", "change", "delete"].contains(rest) else { return .text(404, "not found") }
                do {
                    let line = try await doll.dashboardCalendarEdit(rest, Self.query(target))
                    return .json(["done": rest, "item": line])
                } catch let e as CalendarError { return .text(409, e.description) }
            case .events:
                return .text(400, "/events is a stream")
            }
        } catch {
            return .text(500, "\(error)")
        }
    }

    /// Starts listening; returns the bound port (port 0 in the config picks a free one, for tests).
    public func start() async throws -> UInt16 {
        let params = NWParameters.tcp
        params.allowLocalEndpointReuse = true
        guard let port = NWEndpoint.Port(rawValue: UInt16(config.port)) else {
            throw StoreError(description: "dashboard: bad port \(config.port)")
        }
        params.requiredLocalEndpoint = .hostPort(host: NWEndpoint.Host(config.bind), port: port)
        let l = try NWListener(using: params)
        listener = l
        l.newConnectionHandler = { [weak self] c in self?.accept(c) }
        let once = Mutex(false)
        return try await withCheckedThrowingContinuation { cont in
            l.stateUpdateHandler = { state in
                let first: Bool
                switch state {
                case .ready:
                    first = once.withLock { let f = !$0; $0 = true; return f }
                    if first { cont.resume(returning: l.port?.rawValue ?? 0) }
                case .failed(let e):
                    first = once.withLock { let f = !$0; $0 = true; return f }
                    if first { cont.resume(throwing: StoreError(description: "dashboard: cannot listen on \(self.config.bind):\(self.config.port): \(e)")) }
                    else { Log.write("ERROR", "dashboard listener failed: \(e)") }
                default: break
                }
            }
            l.start(queue: queue)
        }
    }

    public func stop() {
        listener?.cancel()
        listener = nil
    }

    private func accept(_ c: NWConnection) {
        c.start(queue: queue)
        readHead(c, Data())
    }

    private static let headEnd = Data("\r\n\r\n".utf8)
    /// Larger request heads are refused; the page's requests are a few hundred bytes.
    private static let maxHead = 64 * 1024

    private func readHead(_ c: NWConnection, _ buf: Data) {
        c.receive(minimumIncompleteLength: 1, maximumLength: Self.maxHead) { [weak self] data, _, done, error in
            guard let self else { c.cancel(); return }
            var buf = buf
            if let data { buf.append(data) }
            if let end = buf.range(of: Self.headEnd) {
                let head = String(decoding: buf[..<end.lowerBound], as: UTF8.self)
                let parts = head.split(separator: "\r\n", maxSplits: 1).first?.split(separator: " ") ?? []
                guard parts.count == 3 else {
                    self.send(c, .text(400, "bad request line"))
                    return
                }
                let method = String(parts[0]), target = String(parts[1])
                if method == "GET", target.prefix(while: { $0 != "?" }) == "/events" {
                    self.stream(c)
                    return
                }
                Task {
                    let r = await self.respond(method: method, target: target)
                    self.send(c, r)
                }
            } else if error != nil || done || buf.count > Self.maxHead {
                c.cancel()
            } else {
                self.readHead(c, buf)
            }
        }
    }

    private static func reason(_ status: Int) -> String {
        switch status {
        case 200: "OK"
        case 400: "Bad Request"
        case 404: "Not Found"
        case 405: "Method Not Allowed"
        case 409: "Conflict"
        default: "Internal Server Error"
        }
    }

    private func send(_ c: NWConnection, _ r: Response) {
        let head = "HTTP/1.1 \(r.status) \(Self.reason(r.status))\r\nContent-Type: \(r.contentType)\r\n"
            + "Content-Length: \(r.body.count)\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n"
        c.send(content: Data(head.utf8) + r.body, completion: .contentProcessed { _ in c.cancel() })
    }

    /// `/events`: one `change` event per feed notification (coalesced while the client is slow), a
    /// first one on connect so the page loads at once. Ends when the client goes away.
    private func stream(_ c: NWConnection) {
        let feed = doll.feed
        let (id, changes) = feed.subscribe()
        // The client never sends after its request: any read completing means it closed.
        c.receive(minimumIncompleteLength: 1, maximumLength: 1024) { _, _, _, _ in
            feed.unsubscribe(id)
            c.cancel()
        }
        let head = "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\nConnection: keep-alive\r\n\r\n"
        Task {
            guard await Self.write(c, Data(head.utf8) + Self.message("hello")) else { feed.unsubscribe(id); return }
            for await change in changes {
                guard await Self.write(c, Self.message(change.rawValue)) else { break }
            }
            feed.unsubscribe(id)
            c.cancel()
        }
    }

    private static func message(_ what: String) -> Data {
        Data("event: change\ndata: {\"what\":\"\(what)\"}\n\n".utf8)
    }

    private static func write(_ c: NWConnection, _ data: Data) async -> Bool {
        await withCheckedContinuation { cont in
            c.send(content: data, completion: .contentProcessed { cont.resume(returning: $0 == nil) })
        }
    }
}
