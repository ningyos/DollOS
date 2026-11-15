import Foundation

/// Her four simulated signals (m6-drives, 2026-11-15, owner). Designed by what they do, not named
/// after real neurochemicals. Events move them; between events each relaxes toward its rest value
/// with its own half-life (睡意 instead builds at a steady rate while she is awake); every change is
/// recorded with the event that caused it. She never sees a number: a signal outside its usual
/// range gives 【狀況】 one plain sentence (`felt`) and may make a concern surface (`Doll+Drives`).
///
///     動力 drive       progress, being affirmed ↑; high → her own things surface, one after another
///     壓力 stress      mistakes, corrections, deadlines ↑; praise and sleep ↓; high → duties surface
///     親近 closeness   talk with the owner, thanks ↑; recedes within hours; low → the owner surfaces
///     睡意 sleepiness  builds while awake and working; sleep clears it; high → "time to sleep"
public enum Signal: String, CaseIterable, Sendable, Codable {
    case drive, stress, closeness, sleepiness

    public var label: String {
        switch self {
        case .drive: "動力"
        case .stress: "壓力"
        case .closeness: "親近"
        case .sleepiness: "睡意"
        }
    }

    /// Which side of the range counts: 親近 is out when low, the others when high.
    var outWhenLow: Bool { self == .closeness }
}

/// One signal's numbers (`[drives]`): values live in 0…1.
public struct SignalSpec: Sendable, Equatable {
    /// Where it relaxes to between events (睡意: its value right after sleep).
    public var rest: Double
    /// Hours to close half the gap to `rest` (睡意: unused).
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
    case sleep                            // her sleep: 睡意 cleared, 壓力 partly down
    case wake                             // the morning wake: 睡意 cleared again (it built while she slept)
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
        var points: [Signal: Point] = [:]
        for (k, v) in raw {
            guard let s = Signal(rawValue: k) else {
                throw DecodingError.dataCorruptedError(forKey: .points, in: c, debugDescription: "unknown signal \(k)")
            }
            guard (0...1).contains(v.value) else {
                throw DecodingError.dataCorruptedError(forKey: .points, in: c, debugDescription: "\(k) value \(v.value) outside 0…1")
            }
            points[s] = v
        }
        guard points.count == Signal.allCases.count else {
            throw DecodingError.dataCorruptedError(forKey: .points, in: c, debugDescription: "points must hold each of \(Signal.allCases.map(\.rawValue))")
        }
        self.points = points
        surfaced = Set(try c.decode([String].self, forKey: .surfaced).map {
            guard let s = Signal(rawValue: $0) else {
                throw DecodingError.dataCorruptedError(forKey: .surfaced, in: c, debugDescription: "unknown signal \($0)")
            }
            return s
        })
        log = try c.decode([SignalChange].self, forKey: .log)
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

    /// A fresh start at `now`: every signal at its rest value, 親近 at `closeness_start`.
    public init(config: DrivesConfig, now: Double) {
        self.config = config
        state = SignalState(points: Dictionary(uniqueKeysWithValues: Signal.allCases.map {
            ($0, SignalState.Point(value: $0 == .closeness ? config.closenessStart : config.spec($0).rest, ts: now))
        }))
    }

    /// The value at `t` (not before the last change).
    public func value(_ s: Signal, at t: Double) -> Double {
        let p = state.points[s]!
        let dt = max(0, t - p.ts) / 3600
        let spec = config.spec(s)
        if s == .sleepiness { return min(1, p.value + config.sleepinessPerHour * dt) }
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

    /// Sets `s` to `value` at `t` (sleep clearing 睡意), recording the cause.
    public mutating func set(_ s: Signal, to v: Double, at t: Double, cause: String) {
        let before = value(s, at: t)
        state.points[s] = .init(value: v, ts: t)
        guard v != before else { return }
        state.log.append(SignalChange(ts: t, signal: s, before: before, after: v, cause: cause))
    }

    /// One event's effect on every signal it moves (`[drives]`'s deltas). `detail`: the cause's words.
    public mutating func apply(_ e: SignalEvent, at t: Double, detail: String) {
        let cause = "\(Self.eventLabel(e))：\(detail)"
        switch e {
        case .sleep:
            set(.sleepiness, to: config.spec(.sleepiness).rest, at: t, cause: cause)
            let rest = config.spec(.stress).rest
            set(.stress, to: rest + (value(.stress, at: t) - rest) * config.sleepStressKeep, at: t, cause: cause)
            // 親近 does not recede while she sleeps (the night is not "no one talked to me"): its
            // point is pinned here and `thaw` moves it to the wake.
            state.points[.closeness] = .init(value: value(.closeness, at: t), ts: t)
        case .wake:
            set(.sleepiness, to: config.spec(.sleepiness).rest, at: t, cause: cause)
            thaw(at: t)
        default:
            for (s, d) in config.deltas(e) where d != 0 { move(s, by: d, at: t, cause: cause) }
        }
        trim(before: t - config.logDays * 86400)
    }

    /// Woken (any way): 親近 resumes receding from where it was when she fell asleep.
    public mutating func thaw(at t: Double) {
        let p = state.points[.closeness]!
        state.points[.closeness] = .init(value: p.value, ts: max(p.ts, t))
    }

    mutating func trim(before t: Double) {
        if let i = state.log.firstIndex(where: { $0.ts >= t }) { state.log.removeFirst(i) } else { state.log.removeAll() }
    }

    /// When 睡意 leaves its range at its steady rate (now or past when it already has); nil when it never will.
    public func sleepyAt() -> Double? {
        let p = state.points[.sleepiness]!
        let edge = config.spec(.sleepiness).edge
        if p.value > edge { return p.ts }
        guard config.sleepinessPerHour > 0 else { return nil }
        return p.ts + (edge - p.value) / config.sleepinessPerHour * 3600 + 1
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
        case .sleep: "睡覺"
        case .wake: "起床"
        }
    }

    /// Back in range: the excursion's surfacing is forgotten, so the next crossing surfaces again.
    /// Returns the signals out of range and not yet surfaced, in `[drives] order`.
    public mutating func pending(at t: Double) -> [Signal] {
        for s in Signal.allCases where !isOut(s, at: t) { state.surfaced.remove(s) }
        return config.order.filter { isOut($0, at: t) && !state.surfaced.contains($0) }
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
            var at: Double?
            if s == .sleepiness {
                // Builds only: it never comes back into range on its own.
                if !out, config.sleepinessPerHour > 0 { at = t + (spec.edge - v) / config.sleepinessPerHour * 3600 }
            } else {
                // v(t+h) = rest + (v - rest)·½^(h/H) crosses edge when ½^(h/H) = (edge - rest)/(v - rest).
                let r = (spec.edge - spec.rest) / (v - spec.rest)
                if r > 0, r < 1 { at = t + log2(1 / r) * spec.halfLifeH * 3600 }
            }
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
        ([.stress, .sleepiness], "你又累又有點緊繃"),
        ([.stress, .drive], "事情很多，你繃著但做得起勁"),
        ([.stress, .closeness], "你有點緊繃，也有點想主人"),
        ([.drive, .sleepiness], "今天做了很多，開始想睡了"),
        ([.stress], "你現在有點緊繃"),
        ([.sleepiness], "有點想睡了"),
        ([.closeness], "有點想主人了"),
        ([.drive], "今天做得很順，精神不錯"),
    ]
}
