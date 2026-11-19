import Foundation

/// Her three simulated signals (m6-drives, 2026-11-15, owner). Designed by what they do, not named
/// after real neurochemicals. Events move them; between events each relaxes toward its rest value
/// with its own half-life; every change is recorded with the event that caused it. She never sees a
/// number: a signal outside its usual range gives 【狀況】 one plain sentence (`felt`) and may make a
/// concern surface (`Doll+Drives`). (睡意 is gone since 2026-11-16: sleep became system maintenance.)
///
///     動力 drive       climbs on its own (its rest is above its high edge: left idle she wants to do her
///                      own things; idle-drive, 2026-11-20); progress, being affirmed ↑ (sooner); high →
///                      one of her own things surfaces, and when that episode is over it is set back to
///                      `drive_reset` and climbs again (`endEpisode`)
///     壓力 stress      mistakes, corrections, deadlines ↑; praise ↓; high → duties surface
///     親近 closeness   talk with the owner, thanks ↑; recedes within hours; low → the owner surfaces
public enum Signal: String, CaseIterable, Sendable, Codable {
    case drive, stress, closeness

    public var label: String {
        switch self {
        case .drive: "動力"
        case .stress: "壓力"
        case .closeness: "親近"
        }
    }

    /// Which side of the range counts: 親近 is out when low, the others when high.
    var outWhenLow: Bool { self == .closeness }
}

/// One signal's numbers (`[drives]`): values live in 0…1.
public struct SignalSpec: Sendable, Equatable {
    /// Where it relaxes to between events.
    public var rest: Double
    /// Hours to close half the gap to `rest`.
    public var halfLifeH: Double
    /// The edge of the usual range on the side that counts (`Signal.outWhenLow`).
    public var edge: Double
}

/// The event kinds that move signals; each one's deltas are in `[drives]`.
public enum SignalEvent: String, CaseIterable, Sendable, Codable {
    case itemDone = "item_done"           // a todo item done
    case itemFailed = "item_failed"       // a todo item failed
    case toolErrors = "tool_errors"       // a second tool failure in a row (and each one after)
    case praise                           // the owner praised or thanked her (her owner.feedback)
    case correction                       // the owner corrected her (her owner.feedback)
    case ownerTalk = "owner_talk"         // a line from the owner
    case deadline                         // one of her concerns came within `near_hours` of its time
    case concernDone = "concern_done"     // she finished one of her concerns
}

/// One recorded change: when, what, why, from and to.
public struct SignalChange: Sendable, Codable, Equatable {
    public var ts: Double
    public var signal: Signal
    public var before: Double
    public var after: Double
    /// What caused it, in words (the event and its detail: 「todo 完成：寄出季報」).
    public var cause: String
}

/// The signals' state: each one's value at its last change, and the change log.
public struct SignalState: Sendable, Codable, Equatable {
    public struct Point: Sendable, Codable, Equatable {
        public var value: Double
        public var ts: Double
    }
    public var points: [Signal: Point]
    /// The excursions already surfaced: a signal out of range surfaces once, then not again until
    /// it has been back in range (`Drives.check`).
    public var surfaced: Set<Signal> = []
    public var log: [SignalChange] = []

    enum CodingKeys: String, CodingKey { case points, surfaced, log }
    public init(points: [Signal: Point], surfaced: Set<Signal> = [], log: [SignalChange] = []) {
        self.points = points
        self.surfaced = surfaced
        self.log = log
    }
    public init(from d: any Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        let raw = try c.decode([String: Point].self, forKey: .points)
        var dropped = false
        func signal(_ k: String, _ key: CodingKeys) throws -> Signal? {
            if k == Self.removed { dropped = true; return nil }
            guard let s = Signal(rawValue: k) else {
                throw DecodingError.dataCorruptedError(forKey: key, in: c, debugDescription: "unknown signal \(k)")
            }
            return s
        }
        var points: [Signal: Point] = [:]
        for (k, v) in raw {
            guard let s = try signal(k, .points) else { continue }
            guard (0...1).contains(v.value) else {
                throw DecodingError.dataCorruptedError(forKey: .points, in: c, debugDescription: "\(k) value \(v.value) outside 0…1")
            }
            points[s] = v
        }
        guard points.count == Signal.allCases.count else {
            throw DecodingError.dataCorruptedError(forKey: .points, in: c, debugDescription: "points must hold each of \(Signal.allCases.map(\.rawValue))")
        }
        self.points = points
        surfaced = Set(try c.decode([String].self, forKey: .surfaced).compactMap { try signal($0, .surfaced) })
        log = try c.decode([RawChange].self, forKey: .log).compactMap { r in
            try signal(r.signal, .log).map { SignalChange(ts: r.ts, signal: $0, before: r.before, after: r.after, cause: r.cause) }
        }
        droppedRemoved = dropped
    }

    /// 睡意, removed 2026-11-16 (sleep became system maintenance): a `signals.json` written before
    /// holds it; reading drops it (points, surfaced, log), the boot log says so (`droppedRemoved`),
    /// and the next save writes the file without it.
    static let removed = "sleepiness"
    /// Read from a file that still held 睡意 (never saved).
    public var droppedRemoved = false

    private struct RawChange: Decodable {
        var ts: Double, signal: String, before: Double, after: Double, cause: String
    }
    public func encode(to e: any Encoder) throws {
        var c = e.container(keyedBy: CodingKeys.self)
        try c.encode(Dictionary(uniqueKeysWithValues: points.map { ($0.rawValue, $1) }), forKey: .points)
        try c.encode(surfaced.map(\.rawValue).sorted(), forKey: .surfaced)
        try c.encode(log, forKey: .log)
    }
}

/// The math, free of the actor: the tests and the simulated week drive it on their own clock.
public struct Drives: Sendable {
    public let config: DrivesConfig
    public var state: SignalState

    public init(config: DrivesConfig, state: SignalState) {
        self.config = config
        self.state = state
    }

    /// A fresh start at `now`: 動力 at `drive_reset` (as after an episode), 親近 at `closeness_start`,
    /// the others at their rest values.
    public init(config: DrivesConfig, now: Double) {
        self.config = config
        state = SignalState(points: Dictionary(uniqueKeysWithValues: Signal.allCases.map { s in
            let v = switch s { case .drive: config.driveReset; case .closeness: config.closenessStart; case .stress: config.stressRest }
            return (s, SignalState.Point(value: v, ts: now))
        }))
    }

    /// The value at `t` (not before the last change).
    public func value(_ s: Signal, at t: Double) -> Double {
        let p = state.points[s]!
        let dt = max(0, t - p.ts) / 3600
        let spec = config.spec(s)
        return spec.rest + (p.value - spec.rest) * pow(0.5, dt / spec.halfLifeH)
    }

    public func values(at t: Double) -> [Signal: Double] {
        Dictionary(uniqueKeysWithValues: Signal.allCases.map { ($0, value($0, at: t)) })
    }

    /// Outside the usual range, on the side that counts.
    public func isOut(_ s: Signal, at t: Double) -> Bool {
        let v = value(s, at: t), edge = config.spec(s).edge
        return s.outWhenLow ? v < edge : v > edge
    }

    /// Moves `s` by `delta` (clamped to 0…1) at `t`, recording the cause. No record when nothing moved.
    public mutating func move(_ s: Signal, by delta: Double, at t: Double, cause: String) {
        let before = value(s, at: t)
        let after = min(1, max(0, before + delta))
        state.points[s] = .init(value: after, ts: t)
        guard after != before else { return }
        state.log.append(SignalChange(ts: t, signal: s, before: before, after: after, cause: cause))
    }

    /// Sets `s` to `value` at `t`, recording the cause.
    public mutating func set(_ s: Signal, to v: Double, at t: Double, cause: String) {
        let before = value(s, at: t)
        state.points[s] = .init(value: v, ts: t)
        guard v != before else { return }
        state.log.append(SignalChange(ts: t, signal: s, before: before, after: v, cause: cause))
    }

    /// One event's effect on every signal it moves (`[drives]`'s deltas). `detail`: the cause's words.
    public mutating func apply(_ e: SignalEvent, at t: Double, detail: String) {
        let cause = "\(Self.eventLabel(e))：\(detail)"
        for (s, d) in config.deltas(e) where d != 0 { move(s, by: d, at: t, cause: cause) }
        trim(before: t - config.logDays * 86400)
    }

    mutating func trim(before t: Double) {
        if let i = state.log.firstIndex(where: { $0.ts >= t }) { state.log.removeFirst(i) } else { state.log.removeAll() }
    }

    static func eventLabel(_ e: SignalEvent) -> String {
        switch e {
        case .itemDone: "todo 完成"
        case .itemFailed: "todo 失敗"
        case .toolErrors: "工具連續失敗"
        case .praise: "主人誇她"
        case .correction: "主人糾正她"
        case .ownerTalk: "主人跟她說話"
        case .deadline: "掛念的事快到了"
        case .concernDone: "掛念的事做完了"
        }
    }

    /// Back in range: the excursion's surfacing is forgotten, so the next crossing surfaces again.
    /// Returns the signals out of range and not yet surfaced, in `[drives] order`.
    public mutating func pending(at t: Double) -> [Signal] {
        for s in Signal.allCases where !isOut(s, at: t) { state.surfaced.remove(s) }
        return config.order.filter { isOut($0, at: t) && !state.surfaced.contains($0) }
    }

    /// 動力's episode (idle-drive, 2026-11-20): its surfacing gave her a turn of her own; at the first
    /// idle moment after it (she is not in a turn, a list, the maintenance, or owing anyone) the
    /// episode is over, whatever she did in it: 動力 goes back to `drive_reset` and the climb starts
    /// over. Whatever raised it during the episode (her own items, steps, a concern done) is wiped
    /// by this, so finishing her own things never chains into the next one. Returns false when no
    /// episode was open.
    public mutating func endEpisode(at t: Double, cause: String) -> Bool {
        guard state.surfaced.contains(.drive) else { return false }
        state.surfaced.remove(.drive)
        set(.drive, to: config.driveReset, at: t, cause: cause)
        trim(before: t - config.logDays * 86400)
        return true
    }

    /// The next moment after `t` a signal leaves its range (relaxing or building toward it), or comes
    /// back into it while its excursion is marked surfaced (so the mark is cleared on time); nil when
    /// none will without an event. One timer waits for this.
    public func nextCrossing(after t: Double) -> Double? {
        var best: Double?
        for s in Signal.allCases {
            let v = value(s, at: t), spec = config.spec(s)
            let out = isOut(s, at: t)
            // Only a change of side matters: into range when surfaced, out of range otherwise.
            guard !out || state.surfaced.contains(s) else { continue }
            // v(t+h) = rest + (v - rest)·½^(h/H) crosses edge when ½^(h/H) = (edge - rest)/(v - rest).
            let r = (spec.edge - spec.rest) / (v - spec.rest)
            let at = r > 0 && r < 1 ? t + log2(1 / r) * spec.halfLifeH * 3600 : nil
            // A hair past the edge so `isOut` agrees when the timer wakes.
            if let a = at.map({ $0 + 1 }), a > t { best = min(best ?? a, a) }
        }
        return best
    }

    /// 【狀況】's felt sentence: one, by the combination of signals out of range; nil when none.
    public func felt(at t: Double) -> String? {
        let out = Set(Signal.allCases.filter { isOut($0, at: t) })
        for (combo, line) in Self.feltTable where combo.isSubset(of: out) { return line }
        return nil
    }

    /// The fixed set, most specific first (a pair before its singles).
    static let feltTable: [(Set<Signal>, String)] = [
        ([.stress, .drive], "你有點緊繃，但也想做點自己的事"),
        ([.stress, .closeness], "你有點緊繃，也有點想主人"),
        ([.stress], "你現在有點緊繃"),
        ([.closeness], "有點想主人了"),
        ([.drive], "你想做點自己的事"),
    ]
}
