import Foundation

/// Her concerns (掛念, m6-drives, 2026-11-15, owner): one concrete gap each, between how things are
/// and how she expects or hopes them to be, in her own words, with where it came from and when.
/// Sources in phase 1: she notes one herself (`concern.note`); the program notes the ones it can be
/// sure of without a model: a timed calendar item within `[drives] calendar_concern_hours`, a todo
/// item she marked failed. A concern ends when done or let go (`concern.end`); the program ends a
/// calendar one when its time has passed.
///
/// A thought is one concern surfacing, in the words she wrote, two ways:
///   wandering (【走神】)  she is idle and a signal is out of range: one concern related to that
///                        signal surfaces and she gets a turn (`Doll.drivesCheck`); signals in range,
///                        nothing and no model call. One timer waits for the next crossing.
///   association (【聯想】) at the start of any turn, what is in front of her (the new lines and
///                        results) against her open concerns; one above `[select] similarity` is
///                        attached in that turn, no extra turn (`Doll.associate`).
///
/// `<data_dir>/concerns.json` and `<data_dir>/signals.json`; malformed = an error at boot.
public struct Concern: Sendable, Codable, Equatable {
    /// What it is about: which signal makes it surface.
    public enum About: String, Sendable, Codable, CaseIterable {
        /// The owner (how he is, something of his): surfaces when 親近 is low.
        case owner
        /// A duty: promised, due, unfinished, failed: surfaces when 壓力 is high.
        case duty
        /// Her own things: surfaces when 動力 is high, one after another.
        case own

        var label: String {
            switch self {
            case .owner: "主人"
            case .duty: "該做的"
            case .own: "自己的事"
            }
        }
    }
    public var id: Int
    public var text: String
    public var about: About
    /// "her", "calendar", "todo".
    public var source: String
    /// Dedupe key for the program's ones (calendar occurrence, failed item's seq).
    public var key: String?
    public var created: Double
    public var due: Double?
    public var surfaced: [Double] = []
    public var associated: [Double] = []
    /// Its `deadline` already raised 壓力.
    public var nearRaised = false
    public var ended: Double?
    /// "done", "let_go", "passed".
    public var end: String?
}

struct ConcernFile: Codable, Equatable {
    var nextID = 1
    var items: [Concern] = []
}

enum Concerns {
    static let wanderTag = "【走神】"
    static let associateTag = "【聯想】"
    /// How much of the new lines and results an association compares: the embedding's input is
    /// capped anyway (`embedding_max_tokens`); a screen summary's first 2000 characters hold its
    /// window titles and the focused element.
    static let associateChars = 2000

    static let encoder: JSONEncoder = {
        let e = JSONEncoder()
        e.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        e.keyEncodingStrategy = .convertToSnakeCase
        return e
    }()
    static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    static func read<T: Decodable>(_ type: T.Type, _ url: URL, what: String) throws -> T? {
        guard FileManager.default.fileExists(atPath: url.path) else { return nil }
        do { return try decoder.decode(T.self, from: Data(contentsOf: url)) } catch {
            throw StoreError(description: "\(url.path): not a \(what) file: \(error)")
        }
    }

    static func write<T: Encodable>(_ value: T, _ url: URL) throws {
        try encoder.encode(value).write(to: url, options: .atomic)
    }

    /// The concern a signal surfaces: of its kind, the one surfaced least recently (never first),
    /// then the nearest due, then the oldest. `except`: just ended (chaining).
    static func pick(_ items: [Concern], for s: Signal, except: Int? = nil) -> Concern? {
        let about: Concern.About
        switch s {
        case .closeness: about = .owner
        case .stress: about = .duty
        case .drive: about = .own
        }
        return items.filter { $0.ended == nil && $0.about == about && $0.id != except }.min { a, b in
            let la = a.surfaced.last ?? 0, lb = b.surfaced.last ?? 0
            if la != lb { return la < lb }
            let da = a.due ?? .infinity, db = b.due ?? .infinity
            if da != db { return da < db }
            return a.id < b.id
        }
    }

    static func origin(_ c: Concern) -> String {
        let at = Doll.nowText(c.created)
        switch c.source {
        case "calendar": return "#\(c.id)，程式從行事曆記的"
        case "todo": return "#\(c.id)，程式在 todo 失敗時記的，\(at)"
        default: return "#\(c.id)，你 \(at) 記的"
        }
    }

    static func wanderText(_ c: Concern) -> String {
        "\(wanderTag)你想起掛著的一件事（\(origin(c))）：\(c.text)"
    }
    // Not 「手上沒事」 (step 0, 2026-11-15): then her sleep section said "with nothing in hand, sleep",
    // and 3 of 20 idle runs about an unhurried step answered a mid-afternoon surfacing by calling sleep.
    static let wanderOwner = "\(wanderTag)你想到好一陣子沒跟主人說上話了。"

    static func associateText(_ c: Concern) -> String { "\(associateTag)（想到）\(c.text)（#\(c.id)）" }
}

/// What the dashboard shows of her inner life.
public struct DashboardDrives: Sendable, Codable {
    public struct Value: Sendable, Codable { public var signal: String; public var label: String; public var value: Double; public var edge: Double; public var outWhenLow: Bool; public var out: Bool }
    public struct Sample: Sendable, Codable { public var ts: Double; public var values: [String: Double] }
    public var enabled: Bool
    public var values: [Value]
    public var felt: String?
    /// The last day, every 15 minutes and at each change.
    public var curve: [Sample]
    public var changes: [SignalChange]
    public var open: [Concern]
    public var ended: [Concern]
    /// Recent wanderings and associations: time and the line.
    public var thoughts: [Thought]
    public var nextCrossing: Double?
    public struct Thought: Sendable, Codable { public var ts: Double; public var kind: String; public var text: String }
}

extension Doll {
    // storage ---------------------------------------------------------------------------------------

    func saveConcerns() throws { try Concerns.write(concernFile, concernsURL) }
    func saveSignals() throws { try Concerns.write(drives.state, signalsURL) }

    var openConcerns: [Concern] { concernFile.items.filter { $0.ended == nil } }

    /// One event's effect on the signals, saved, and the timer re-armed.
    func signal(_ e: SignalEvent, _ detail: String) {
        // Before the first catch-up a new data dir has no signals yet (they start then).
        guard drivesConfig.enabled, !signalsFresh else { return }
        let now = wallClock.now()
        let before = drives.state.log.count
        drives.apply(e, at: now, detail: detail)
        for c in drives.state.log.dropFirst(before) {
            log("INFO", String(format: "drives: %@ %.2f → %.2f (%@)", c.signal.label, c.before, c.after, c.cause))
        }
        do { try saveSignals() } catch { log("ERROR", "drives: saving \(signalsURL.path) failed: \(error)") }
        feed.notify(.drives)
        scheduleDrives()
    }

    @discardableResult
    func addConcern(_ text: String, about: Concern.About, source: String, key: String? = nil, due: Double? = nil) throws -> Concern {
        let c = Concern(id: concernFile.nextID, text: text, about: about, source: source, key: key, created: wallClock.now(), due: due)
        concernFile.nextID += 1
        concernsNew.insert(c.id)
        concernFile.items.append(c)
        try saveConcerns()
        log("INFO", "drives: concern #\(c.id) (\(about.rawValue), \(source)) 「\(text)」")
        feed.notify(.drives)
        return c
    }

    func endConcern(_ id: Int, how: String) throws -> Concern {
        guard let i = concernFile.items.firstIndex(where: { $0.id == id }) else {
            throw ToolError(description: "沒有 #\(id) 這件掛念；還掛著的：" + listConcerns())
        }
        guard concernFile.items[i].ended == nil else {
            throw ToolError(description: "#\(id) 已經結束了（\(concernFile.items[i].end ?? "")）")
        }
        concernFile.items[i].ended = wallClock.now()
        concernFile.items[i].end = how
        let now = wallClock.now()
        concernFile.items.removeAll { $0.ended.map { $0 < now - drivesConfig.logDays * 86400 } ?? false }
        try saveConcerns()
        log("INFO", "drives: concern #\(id) ended (\(how))")
        feed.notify(.drives)
        return concernFile.items.first { $0.id == id }!
    }

    func listConcerns() -> String {
        let open = openConcerns
        return open.isEmpty ? "沒有" : open.map { "#\($0.id)〔\($0.about.label)〕\($0.text)" }.joined(separator: "；")
    }

    // her tools ------------------------------------------------------------------------------------

    func concernNote(text: String, about: String, due: String?) throws -> Outcome {
        guard let a = Concern.About(rawValue: about) else { throw ToolError(description: "about 只能是 owner、duty、own") }
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { throw ToolError(description: "text 是空的") }
        guard openConcerns.count < drivesConfig.maxOpen else {
            throw ToolError(description: "已經掛著 \(drivesConfig.maxOpen) 件了，先用 concern.end 結束做完或放下的：" + listConcerns())
        }
        var dueTS: Double?
        if let due, !due.isEmpty {
            let parts = due.split(separator: " ").map(String.init)
            guard parts.count == 2, let day = Day(parts[0]), let time = TimeOfDay(parts[1]) else {
                throw ToolError(description: "due 要寫成 YYYY-MM-DD HH:MM，例：2026-11-18 10:00")
            }
            dueTS = day.at(time)
        }
        let c = try addConcern(t, about: a, source: "her", due: dueTS)
        return Outcome(text: "記下了：#\(c.id)〔\(a.label)〕\(t)。", actions: ["concern note"])
    }

    func concernEnd(id: Int, how: String) throws -> Outcome {
        guard how == "done" || how == "let_go" else { throw ToolError(description: "how 只能是 done 或 let_go") }
        let c = try endConcern(id, how: how)
        if how == "done" {
            signal(.concernDone, "#\(c.id) \(c.text)")
            if c.about == .own { chainAfter = c.id }
        }
        return Outcome(text: "#\(c.id) 結束了（\(how == "done" ? "做完" : "放下")）。", actions: ["concern end"])
    }

    func ownerFeedback(kind: String, quote: String) throws -> Outcome {
        let q = quote.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !q.isEmpty else { throw ToolError(description: "quote 要照抄主人的那句話") }
        // Only a line the owner did say counts: the quote must be in one of his lines today.
        guard timeline.events.contains(where: { $0.kind == .owner && $0.text.contains(q) }) else {
            throw ToolError(description: "今天主人的話裡找不到「\(q)」；quote 要照抄他說的那句（一部分也可以）")
        }
        switch kind {
        case "praise": signal(.praise, "「\(q)」")
        case "correction": signal(.correction, "「\(q)」")
        default: throw ToolError(description: "kind 只能是 praise 或 correction")
        }
        return Outcome(text: "記下了。", actions: ["owner feedback \(kind)"])
    }

    // the program's concerns --------------------------------------------------------------------------

    /// Calendar items within the window become concerns; passed ones end; near ones raise 壓力 once.
    func refreshProgramConcerns(now: Double) throws {
        let window = drivesConfig.calendarConcernHours * 3600
        var changed = false
        for o in agenda.starts(after: now, through: now + window) where o.role == nil {
            guard let start = o.startTS else { continue }
            let key = "calendar:\(o.id):\(Int(start))"
            guard !concernFile.items.contains(where: { $0.key == key }) else { continue }
            try addConcern("\(o.title)（\(Self.nowText(start))）", about: .duty, source: "calendar", key: key, due: start)
        }
        for i in concernFile.items.indices where concernFile.items[i].ended == nil {
            let c = concernFile.items[i]
            if c.source == "calendar", let due = c.due, due <= now {
                concernFile.items[i].ended = now
                concernFile.items[i].end = "passed"
                changed = true
                log("INFO", "drives: concern #\(c.id) ended (its time passed)")
            } else if let due = c.due, !c.nearRaised, due - now <= drivesConfig.nearHours * 3600 {
                concernFile.items[i].nearRaised = true
                changed = true
                signal(.deadline, "#\(c.id) \(c.text)")
            }
        }
        if changed { try saveConcerns(); feed.notify(.drives) }
    }

    /// The next moment a program concern appears, comes near, or passes.
    func nextConcernChange(after now: Double) -> Double? {
        var best: Double?
        func take(_ t: Double?) { if let t, t > now { best = min(best ?? t, t) } }
        let window = drivesConfig.calendarConcernHours * 3600
        take(agenda.nextStart(after: now + window).map { $0 - window })
        for c in openConcerns {
            guard let due = c.due else { continue }
            if !c.nearRaised { take(due - drivesConfig.nearHours * 3600) }
            if c.source == "calendar" { take(due) }
        }
        return best.map { $0 + 1 }
    }

    // wandering ------------------------------------------------------------------------------------

    /// Idle: no todo in hand, nothing queued or owed, not in maintenance, no turn running, no watch.
    var idleForWander: Bool {
        !inTurn && !paused && maintenanceState.running == nil && queue.isEmpty && !followUp && watch == nil
            && (todo.phase == .idle || todo.phase == .done)
    }

    /// After every turn, on the timer, and on waking: program concerns, then at most one wandering.
    func drivesCheck() {
        guard drivesConfig.enabled, calendarCaughtUp, !stopped else { return }
        let now = wallClock.now()
        do { try refreshProgramConcerns(now: now) } catch { log("ERROR", "drives: program concerns: \(error)") }
        defer { scheduleDrives() }
        // A signal back in range forgets its surfacing (here, even when she is busy).
        let pending = drives.pending(at: now)
        guard idleForWander else { return }
        // 動力 high: finishing one of her own things leads straight to the next.
        if let done = chainAfter {
            chainAfter = nil
            if drives.isOut(.drive, at: now), let next = Concerns.pick(concernFile.items, for: .drive, except: done) {
                return wander(next, signal: .drive, now: now, chained: true)
            }
        }
        for s in pending {
            drives.state.surfaced.insert(s)
            do { try saveSignals() } catch { log("ERROR", "drives: saving \(signalsURL.path) failed: \(error)") }
            if let c = Concerns.pick(concernFile.items, for: s) { return wander(c, signal: s, now: now, chained: false) }
            switch s {
            case .closeness: return wanderLine(Concerns.wanderOwner, signal: s)
            default: log("INFO", "drives: \(s.label) out of range, no concern of that kind; nothing surfaces")
            }
        }
    }

    func wander(_ c: Concern, signal s: Signal, now: Double, chained: Bool) {
        if let i = concernFile.items.firstIndex(where: { $0.id == c.id }) { concernFile.items[i].surfaced.append(now) }
        do { try saveConcerns() } catch { log("ERROR", "drives: saving \(concernsURL.path) failed: \(error)") }
        associatedIDs.insert(c.id)
        log("INFO", "drives: wander (\(s.label)\(chained ? ", chained" : "")) #\(c.id)「\(c.text)」")
        put(Event(.memory, .internal, withOwnerSleep(Concerns.wanderText(c))))
    }

    func wanderLine(_ text: String, signal s: Signal) {
        log("INFO", "drives: wander (\(s.label)) \(text)")
        put(Event(.memory, .internal, withOwnerSleep(text)))
    }

    /// A wandering or the maintenance notice inside the owner-sleep span carries the 【狀況】 line,
    /// so the turn that might message him has it in front of her (2026-11-16).
    func withOwnerSleep(_ text: String) -> String {
        ownerSleepLine(now: wallClock.now()).map { text + "\n（\($0)）" } ?? text
    }

    func scheduleDrives() {
        drivesTask?.cancel()
        drivesTask = nil
        drivesSerial += 1
        guard drivesConfig.enabled, !stopped, calendarCaughtUp else { return }
        let now = wallClock.now()
        let next = [drives.nextCrossing(after: now), nextConcernChange(after: now)].compactMap { $0 }.min()
        guard let next else { return }
        let serial = drivesSerial, clock = wallClock
        drivesTask = Task { [weak self] in
            do { try await clock.sleep(until: next) } catch { return }
            await self?.drivesWake(serial)
        }
    }

    func drivesWake(_ serial: Int) {
        guard serial == drivesSerial else { return }
        drivesTask = nil
        drivesCheck()
    }

    // association ----------------------------------------------------------------------------------

    /// At the start of a turn: the new lines and results since her last reply against her open
    /// concerns; the nearest at or above `[select] similarity`, not given since the owner's last
    /// line, goes into this turn (no turn of its own).
    func associate() async {
        guard drivesConfig.enabled else { return }
        let events = timeline.events
        let lastReply = events.lastIndex { $0.kind == .doll } ?? -1
        let fresh = events[(lastReply + 1)...].filter { $0.kind == .owner || $0.kind == .developer || $0.kind == .tool }
        let query = String(fresh.map(\.text).joined(separator: "\n").prefix(Concerns.associateChars))
        // Not one noted since the last turn began (its own result would bring it straight back).
        let candidates = openConcerns.filter { !associatedIDs.contains($0.id) && !concernsNew.contains($0.id) }
        concernsNew = []
        guard !query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, !candidates.isEmpty else { return }
        do {
            let missing = candidates.filter { concernVectors[$0.id] == nil }
            if !missing.isEmpty {
                for (c, v) in zip(missing, try embedder.embed(missing.map(\.text), as: .document)) { concernVectors[c.id] = v }
            }
            guard let qv = try embedder.embed([query], as: .query).first else { return }
            let scored = candidates.map { c in (c, zip(qv, concernVectors[c.id]!).reduce(0.0) { $0 + Double($1.0 * $1.1) }) }
            guard let best = scored.max(by: { $0.1 < $1.1 }) else { return }
            log("INFO", String(format: "drives: association best #%d %.3f (threshold %.2f)", best.0.id, best.1, selectConfig.similarity))
            guard best.1 >= selectConfig.similarity else { return }
            associatedIDs.insert(best.0.id)
            if let i = concernFile.items.firstIndex(where: { $0.id == best.0.id }) { concernFile.items[i].associated.append(wallClock.now()) }
            try saveConcerns()
            let text = Concerns.associateText(best.0)
            try append(Event(.memory, .internal, text))
            associatedNow.append(text)
        } catch {
            log("ERROR", "drives: association failed: \(error)")
        }
    }

    // the dashboard ----------------------------------------------------------------------------------

    public func dashboardDrives() -> DashboardDrives {
        let now = wallClock.now()
        let values = Signal.allCases.map { s in
            DashboardDrives.Value(signal: s.rawValue, label: s.label, value: drives.value(s, at: now), edge: drivesConfig.spec(s).edge,
                                  outWhenLow: s.outWhenLow, out: drives.isOut(s, at: now))
        }
        // The curve: replay the log from a day ago (values before a change are its `before`).
        var curve: [DashboardDrives.Sample] = []
        let from = now - 86400
        var t = from
        while t <= now {
            curve.append(.init(ts: t, values: Dictionary(uniqueKeysWithValues: Signal.allCases.map { ($0.rawValue, replay($0, at: t)) })))
            t += 900
        }
        let thoughts = timeline.events.filter { $0.kind == .memory && ($0.text.hasPrefix(Concerns.wanderTag) || $0.text.hasPrefix(Concerns.associateTag)) }
            .suffix(30).map { DashboardDrives.Thought(ts: $0.ts, kind: $0.text.hasPrefix(Concerns.wanderTag) ? "走神" : "聯想", text: $0.text) }
        return DashboardDrives(enabled: drivesConfig.enabled, values: values, felt: drives.felt(at: now), curve: curve,
                               changes: Array(drives.state.log.filter { $0.ts >= from }.suffix(200)),
                               open: openConcerns, ended: concernFile.items.filter { $0.ended != nil },
                               thoughts: thoughts, nextCrossing: drives.nextCrossing(after: now))
    }

    /// A signal's value at a past `t` from the log: the first change after `t` knows where it was
    /// coming from (its `before` decays back from the change before it); none after `t` = now's math.
    func replay(_ s: Signal, at t: Double) -> Double {
        let changes = drives.state.log.filter { $0.signal == s }
        guard let next = changes.firstIndex(where: { $0.ts > t }) else { return drives.value(s, at: t) }
        guard next > 0 else { return changes[next].before }
        let prev = changes[next - 1]
        let one = Drives(config: drivesConfig, state: SignalState(points: Dictionary(uniqueKeysWithValues: Signal.allCases.map {
            ($0, SignalState.Point(value: $0 == s ? prev.after : 0, ts: prev.ts))
        })))
        return one.value(s, at: t)
    }

}
