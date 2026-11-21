import Foundation
import Network
import Synchronization

/// The local API (2026-11-12): how `dollctl` and the Discord bridge reach her. HTTP/1.1 on
/// 127.0.0.1 only, one request per connection; a connection from anything but loopback is closed
/// unanswered.
///
///     GET  /api/status                    ApiStatus (JSON)
///     POST /api/developer   {"text":…}    a developer line (dollctl chat)
///     GET  /api/listen?to=developer       NDJSON stream: what she says to the developer
///     POST /api/owner       {"text":…}    an owner line (the bridge); needs `Authorization: Bearer <owner key>`
///     GET  /api/listen?to=owner           NDJSON stream for the bridge; needs the owner key; one at a time
///     GET  /api/voice                     WebSocket for the bridge's voice channel (2026-11-13, M6; `Voice.swift`);
///                                         needs the owner key; one at a time (409 while a live one is open; an open
///                                         one that does not answer a ping is replaced as stale); 503 when
///                                         `[voice] enabled = false`
///
/// Stream lines: `{"type":"hello"}` first, then `{"type":"say","text":…}` and
/// `{"type":"typing","on":true|false}` (a turn that follows that person's line started or ended).
/// The owner key is random per launch and handed to the bridge in its environment, so nothing else
/// on this machine can speak as the owner.
public final class LocalAPI: @unchecked Sendable {
    /// The configured port until `start`, then the bound one.
    public private(set) var port: Int
    /// What the bridge sends as `Authorization: Bearer …`.
    public let ownerKey: String
    let queue = DispatchQueue(label: "dollcore.api")
    private var listener: NWListener?
    private let doll = Mutex<Doll?>(nil)
    private let streams = Mutex<[Addressee: [UUID: AsyncStream<Data>.Continuation]]>([:])
    /// The Discord bridge, as `Bridge` reports it; also why a `discord.dm` had no stream.
    private let bridgeState = Mutex<String>("disabled（[bridge] enabled = false）")
    private var typingTask: Task<Void, Never>?
    /// The voice channel's peer (`[voice] enabled`); nil = `/api/voice` answers 503.
    private let voiceLink = Mutex<VoiceLink?>(nil)

    public init(config: ApiConfig) {
        port = config.port
        var bytes = [UInt8](repeating: 0, count: 32)
        for i in bytes.indices { bytes[i] = UInt8.random(in: 0...255) }
        ownerKey = bytes.map { String(format: "%02x", $0) }.joined()
    }

    /// The base URL the bridge gets as `DOLL_API`.
    public var baseURL: String { "http://127.0.0.1:\(port)" }

    public func setBridgeState(_ s: String) { bridgeState.withLock { $0 = s } }
    public var bridge: String { bridgeState.withLock { $0 } }

    public func setVoice(_ v: VoiceLink) { voiceLink.withLock { $0 = v } }

    /// Open streams for `to`.
    public func listening(_ to: Addressee) -> Int { streams.withLock { $0[to]?.count ?? 0 } }

    /// Her `discord.dm` (owner) or `dollctl.reply` (developer): queued on every open stream for `to`
    /// as a `say` line; nil, or why it went nowhere. `image` (design B, 2026-11-16): a screenshot file
    /// on this Mac, its absolute path in the line's `image` (the bridge runs here and attaches it).
    public func deliver(_ text: String, to: Addressee, image: URL? = nil) -> String? {
        let sent = send(.say(text, image: image?.path), to: to)
        guard sent == 0 else { return nil }
        switch to {
        case .owner: return "Discord 送不出去：bridge \(bridge)"
        case .developer: return "沒有開著的 dollctl chat"
        }
    }

    enum Message {
        case hello, say(String, image: String? = nil), typing(Bool)

        var line: Data {
            let obj: [String: Any] = switch self {
            case .hello: ["type": "hello"]
            case .say(let t, let image): image.map { ["type": "say", "text": t, "image": $0] } ?? ["type": "say", "text": t]
            case .typing(let on): ["type": "typing", "on": on]
            }
            return (try! JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys, .withoutEscapingSlashes])) + Data("\n".utf8)
        }
    }

    @discardableResult
    func send(_ m: Message, to: Addressee) -> Int {
        let line = m.line
        return streams.withLock { s in
            for c in (s[to] ?? [:]).values { c.yield(line) }
            return s[to]?.count ?? 0
        }
    }

    // routing ------------------------------------------------------------------

    public struct Request: Sendable {
        public var method: String
        public var target: String
        public var headers: [String: String]   // names lowercased
        public var body: Data

        public init(method: String, target: String, headers: [String: String] = [:], body: Data = Data()) {
            self.method = method
            self.target = target
            self.headers = headers
            self.body = body
        }

        var path: String { String(target.prefix { $0 != "?" }) }
        var query: [String: String] {
            guard let q = target.firstIndex(of: "?") else { return [:] }
            var out: [String: String] = [:]
            for pair in target[target.index(after: q)...].split(separator: "&") {
                let kv = pair.split(separator: "=", maxSplits: 1).map { String($0).removingPercentEncoding ?? String($0) }
                out[kv[0]] = kv.count > 1 ? kv[1] : ""
            }
            return out
        }
    }

    public struct Response: Sendable {
        public var status: Int
        public var body: Data

        static func json<T: Encodable>(_ value: T, status: Int = 200) -> Response {
            let enc = JSONEncoder()
            enc.keyEncodingStrategy = .convertToSnakeCase
            enc.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
            return Response(status: status, body: (try? enc.encode(value)) ?? Data("{}".utf8))
        }

        static func error(_ status: Int, _ msg: String) -> Response { json(["error": msg], status: status) }
    }

    /// A peer this API answers: loopback only.
    public static func isLoopback(_ endpoint: NWEndpoint) -> Bool {
        guard case .hostPort(let host, _) = endpoint else { return false }
        switch host {
        case .ipv4(let a): return a.isLoopback
        case .ipv6(let a): return a.isLoopback || a.asIPv4?.isLoopback == true
        case .name(let n, _): return n == "localhost"
        @unknown default: return false
        }
    }

    func authorized(_ r: Request) -> Bool {
        guard let h = r.headers["authorization"], h.hasPrefix("Bearer ") else { return false }
        let given = Array(h.dropFirst("Bearer ".count).utf8), want = Array(ownerKey.utf8)
        guard given.count == want.count else { return false }
        return zip(given, want).reduce(0) { $0 | ($1.0 ^ $1.1) } == 0
    }

    struct Line: Decodable { var text: String }

    /// Answers everything but the streams.
    public func respond(_ r: Request) async -> Response {
        guard let doll = doll.withLock({ $0 }) else { return .error(503, "not started") }
        switch (r.method, r.path) {
        case ("GET", "/api/status"):
            do {
                return .json(try await status(doll))
            } catch {
                return .error(500, "\(error)")
            }
        case ("POST", "/api/developer"), ("POST", "/api/owner"):
            let owner = r.path == "/api/owner"
            if owner, !authorized(r) { return .error(401, "owner key required") }
            guard let line = try? JSONDecoder().decode(Line.self, from: r.body) else {
                return .error(400, "body must be {\"text\": \"…\"}")
            }
            let text = line.text.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !text.isEmpty else { return .error(400, "empty text") }
            if owner { await doll.ownerLine(text) } else { await doll.developerSaid(text) }
            return .json(["queued": true])
        case ("GET", "/api/listen"):
            return .error(400, "/api/listen is a stream")
        default:
            return .error(404, "not found")
        }
    }

    func status(_ doll: Doll) async throws -> ApiStatus {
        let d = try await doll.dashboardStatus()
        return ApiStatus(name: doll.name, footer: try await doll.status(), state: d.state, maintenance: d.maintenance, connection: d.connection, paused: d.paused, queued: d.queued,
                         turn: d.turn, answering: await doll.answering()?.rawValue, bridge: bridge,
                         ownerStreams: listening(.owner), developerStreams: listening(.developer))
    }

    // server -------------------------------------------------------------------

    /// Starts listening on 127.0.0.1 (port 0 picks a free one, for tests) and following her turns
    /// for the typing lines; returns the bound port.
    public func start(doll d: Doll) async throws -> UInt16 {
        doll.withLock { $0 = d }
        let params = NWParameters.tcp
        params.allowLocalEndpointReuse = true
        let wanted = port
        guard let port = NWEndpoint.Port(rawValue: UInt16(wanted)) else {
            throw StoreError(description: "api: bad port \(wanted)")
        }
        params.requiredLocalEndpoint = .hostPort(host: .ipv4(.loopback), port: port)
        let l = try NWListener(using: params)
        listener = l
        l.newConnectionHandler = { [weak self] c in self?.accept(c) }
        let once = Mutex(false)
        let bound: UInt16 = try await withCheckedThrowingContinuation { cont in
            l.stateUpdateHandler = { state in
                switch state {
                case .ready:
                    if once.withLock({ let f = !$0; $0 = true; return f }) { cont.resume(returning: l.port?.rawValue ?? 0) }
                case .failed(let e):
                    if once.withLock({ let f = !$0; $0 = true; return f }) {
                        cont.resume(throwing: StoreError(description: "api: cannot listen on 127.0.0.1:\(wanted): \(e)"))
                    } else {
                        Log.write("ERROR", "api listener failed: \(e)")
                    }
                default: break
                }
            }
            l.start(queue: queue)
        }
        self.port = Int(bound)
        typingTask = Task { [weak self] in await self?.followTurns(d) }
        return bound
    }

    public func stop() {
        typingTask?.cancel()
        listener?.cancel()
        listener = nil
        streams.withLock { s in
            for c in s.values.flatMap(\.values) { c.finish() }
            s = [:]
        }
    }

    /// Typing on for whoever spoke last while a turn runs, off when it ends.
    private func followTurns(_ d: Doll) async {
        let (id, changes) = d.feed.subscribe()
        defer { d.feed.unsubscribe(id) }
        var shown: Addressee?
        for await change in changes where change == .turn {
            let now = await d.answering()
            guard now != shown else { continue }
            if let s = shown { send(.typing(false), to: s) }
            if let n = now { send(.typing(true), to: n) }
            shown = now
        }
    }

    private func accept(_ c: NWConnection) {
        guard Self.isLoopback(c.endpoint) else {
            Log.write("WARN", "api: refused a connection from \(c.endpoint)")
            c.cancel()
            return
        }
        c.start(queue: queue)
        read(c, Data())
    }

    private static let headEnd = Data("\r\n\r\n".utf8)
    /// Requests are a few hundred bytes plus one line of text.
    private static let maxRequest = 256 * 1024

    private func read(_ c: NWConnection, _ buf: Data) {
        c.receive(minimumIncompleteLength: 1, maximumLength: Self.maxRequest) { [weak self] data, _, done, error in
            guard let self else { c.cancel(); return }
            var buf = buf
            if let data { buf.append(data) }
            if let end = buf.range(of: Self.headEnd) {
                let lines = String(decoding: buf[..<end.lowerBound], as: UTF8.self).components(separatedBy: "\r\n")
                let parts = lines.first?.split(separator: " ") ?? []
                guard parts.count == 3 else { return self.send(c, .error(400, "bad request line")) }
                var headers: [String: String] = [:]
                for h in lines.dropFirst() {
                    guard let colon = h.firstIndex(of: ":") else { continue }
                    headers[h[..<colon].lowercased()] = h[h.index(after: colon)...].trimmingCharacters(in: .whitespaces)
                }
                let length = Int(headers["content-length"] ?? "0") ?? -1
                guard length >= 0, length <= Self.maxRequest else { return self.send(c, .error(400, "bad content-length")) }
                let body = buf[end.upperBound...]
                if body.count < length {
                    if error != nil || done { c.cancel() } else { self.read(c, buf) }
                    return
                }
                let r = Request(method: String(parts[0]), target: String(parts[1]), headers: headers, body: Data(body.prefix(length)))
                if r.method == "GET", r.path == "/api/listen" { return self.listen(c, r) }
                if r.method == "GET", r.path == "/api/voice" { return self.upgrade(c, r, leftover: Data(body.dropFirst(length))) }
                Task { self.send(c, await self.respond(r)) }
            } else if error != nil || done || buf.count > Self.maxRequest {
                c.cancel()
            } else {
                self.read(c, buf)
            }
        }
    }

    private static func reason(_ status: Int) -> String {
        switch status {
        case 101: "Switching Protocols"
        case 200: "OK"
        case 400: "Bad Request"
        case 401: "Unauthorized"
        case 404: "Not Found"
        case 409: "Conflict"
        case 503: "Service Unavailable"
        default: "Internal Server Error"
        }
    }

    private func send(_ c: NWConnection, _ r: Response) {
        let head = "HTTP/1.1 \(r.status) \(Self.reason(r.status))\r\nContent-Type: application/json; charset=utf-8\r\n"
            + "Content-Length: \(r.body.count)\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n"
        c.send(content: Data(head.utf8) + r.body, completion: .contentProcessed { _ in c.cancel() })
    }

    /// The largest WebSocket message the bridge may send (audio frames are ~640 bytes, control lines short).
    static let maxVoiceMessage = 1 << 20

    /// `/api/voice`: the bridge's WebSocket, handed to the voice link after the handshake.
    private func upgrade(_ c: NWConnection, _ r: Request, leftover: Data) {
        guard let link = voiceLink.withLock({ $0 }) else { return send(c, .error(503, "voice disabled ([voice] enabled = false)")) }
        guard authorized(r) else { return send(c, .error(401, "owner key required")) }
        let response: Data
        do {
            response = try WebSocket.handshake(headers: r.headers)
        } catch {
            return send(c, .error(400, "\(error)"))
        }
        Task {
            guard await link.claim() else { return self.send(c, .error(409, "a voice connection is already open")) }
            c.send(content: response, completion: .contentProcessed { _ in })
            await link.attach(WebSocketSession(connection: c, leftover: leftover, maxMessage: Self.maxVoiceMessage))
        }
    }

    /// One listener's stream: the response has no length and ends when either side closes.
    private func listen(_ c: NWConnection, _ r: Request) {
        guard let to = r.query["to"].flatMap(Addressee.init(rawValue:)) else {
            return send(c, .error(400, "to must be owner or developer"))
        }
        if to == .owner {
            guard authorized(r) else { return send(c, .error(401, "owner key required")) }
        }
        let (stream, cont) = AsyncStream.makeStream(of: Data.self)
        let id = UUID()
        let taken = streams.withLock { s -> Bool in
            // One bridge: a second owner stream would split her words.
            if to == .owner, !(s[.owner] ?? [:]).isEmpty { return false }
            s[to, default: [:]][id] = cont
            return true
        }
        guard taken else { return send(c, .error(409, "an owner stream is already open")) }
        let end: @Sendable () -> Void = { [weak self] in
            self?.streams.withLock { $0[to]?.removeValue(forKey: id) }?.finish()
            c.cancel()
        }
        // The client never sends after its request: any read completing means it closed.
        c.receive(minimumIncompleteLength: 1, maximumLength: 1024) { _, _, _, _ in end() }
        let head = "HTTP/1.1 200 OK\r\nContent-Type: application/x-ndjson\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n"
        Task {
            guard await Self.write(c, Data(head.utf8) + Message.hello.line) else { return end() }
            for await line in stream {
                guard await Self.write(c, line) else { break }
            }
            end()
        }
    }

    private static func write(_ c: NWConnection, _ data: Data) async -> Bool {
        await withCheckedContinuation { cont in
            c.send(content: data, completion: .contentProcessed { cont.resume(returning: $0 == nil) })
        }
    }
}

/// `GET /api/status` (keys snake_case).
public struct ApiStatus: Codable, Sendable, Equatable {
    public var name: String
    /// Her 【狀況】 footer as of now.
    public var footer: String
    /// thinking | acting | idle | maintenance
    public var state: String
    /// While maintenance runs (since when) or is due and waits.
    public var maintenance: String?
    /// While her model requests wait for the server to be reachable again (`Doll.connectionLine`).
    public var connection: String?
    public var paused: Bool
    public var queued: Int
    public var turn: Int
    /// While a turn runs: whose line it follows, owner | developer.
    public var answering: String?
    /// The Discord bridge: disabled, starting, up (pid), or down and why.
    public var bridge: String
    public var ownerStreams: Int
    public var developerStreams: Int

    public init(name: String, footer: String, state: String, maintenance: String? = nil, connection: String? = nil, paused: Bool, queued: Int, turn: Int,
                answering: String?, bridge: String, ownerStreams: Int, developerStreams: Int) {
        self.name = name
        self.footer = footer
        self.state = state
        self.maintenance = maintenance
        self.connection = connection
        self.paused = paused
        self.queued = queued
        self.turn = turn
        self.answering = answering
        self.bridge = bridge
        self.ownerStreams = ownerStreams
        self.developerStreams = developerStreams
    }

    /// What `dollctl status` prints.
    public var text: String {
        var lines = ["\(name)：\(["thinking": "在想", "acting": "在做事", "idle": "等下一件事", "maintenance": "系統維護中"][state] ?? state)"
                     + (paused ? "（暫停中）" : "") + "，第 \(turn) 回合，佇列 \(queued) 件"]
        if let s = maintenance { lines.append(s) }
        if let s = connection { lines.append(s) }
        if let s = answering { lines.append("這回合接在誰的話後面：\(Addressee(rawValue: s)?.label ?? s)") }
        lines.append("Discord bridge：\(bridge)（owner stream \(ownerStreams)）")
        lines.append("dollctl chat：\(developerStreams) 個開著")
        lines.append(footer)
        return lines.joined(separator: "\n")
    }
}
