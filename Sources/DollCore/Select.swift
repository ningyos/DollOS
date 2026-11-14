import Foundation

/// The memory pipeline: the registry entries the program knows exactly, and the selection behind
/// the 【相關記憶】 and `recall`. No model in it (2026-11-15: the on-device model that extracted
/// and aligned names is gone; experiments/m6-registry-eval, experiments/m6-drop-fm).
public final class Selector {
    let memory: Memory
    let config: SelectConfig

    public init(memory: Memory, config: SelectConfig) {
        self.memory = memory
        self.config = config
    }

    var registry: Registry { memory.registry }

    // the program's entries -------------------------------------------------------------

    /// The entry of `type` that `names` give exactly: the first of `names` (in order) that some entry
    /// of that type has as its name or an alias (`Registry.key`). Entries of another type are never
    /// it, whatever their aliases. Several of that type for one name: the oldest (`created_ts`, then
    /// id), so the choice never changes as entries are added.
    func exactEntry(_ type: EntityType, names: [String]) throws -> (entry: RegistryEntry, by: String)? {
        for name in names {
            let found = try registry.exact(name).filter { $0.type == type }
            if let e = found.min(by: { $0.ts != $1.ts ? $0.ts < $1.ts : $0.id < $1.id }) { return (e, name) }
        }
        return nil
    }

    /// An app's entry when the registry has one: of type app, by its bundle id, else by its name
    /// exactly. Never writes the registry.
    public func appEntry(_ app: AppIdentity) throws -> RegistryEntry? {
        try exactEntry(.app, names: [app.bundleID, app.name])?.entry
    }

    /// The front app (or an app an experience used) as the program knows it: `appEntry`, with the
    /// app's name and bundle id added as aliases; none, a new app entry aliased by its bundle id.
    /// How: "bundle id", "name" or "new".
    public func programApp(_ app: AppIdentity) throws -> (entry: RegistryEntry, how: String) {
        guard let (e, by) = try exactEntry(.app, names: [app.bundleID, app.name]) else {
            return (try registry.create(.app, name: app.name, aliases: [app.bundleID],
                                        description: "macOS 上的 app「\(app.name)」（bundle id \(app.bundleID)）"), "new")
        }
        for a in [app.name, app.bundleID] { try registry.addAlias(e.id, a) }
        return (try registry.entry(e.id)!, by == app.bundleID ? "bundle id" : "name")
    }

    /// A menu shortcut's key chord as the program writes it: the shortcut entry with that chord
    /// exactly, else a new one.
    public func programShortcut(_ keys: String) throws -> RegistryEntry {
        if let (e, _) = try exactEntry(.shortcut, names: [keys]) { return e }
        return try registry.create(.shortcut, name: keys, aliases: [], description: "鍵盤快捷鍵 \(keys)")
    }

    // selection --------------------------------------------------------------------------

    /// What started one selection and what it knows.
    public struct Trigger {
        /// For the log: 主人的新訊息 / 前景 app 換成 X / recall「query」.
        public var label: String
        /// What FTS and the embedding search for.
        public var query: String
        /// The front app: its registry entry (`appEntry`), when it has one, is the trigger's entity.
        public var app: AppIdentity?

        public init(label: String, query: String, app: AppIdentity?) {
            (self.label, self.query, self.app) = (label, query, app)
        }
    }

    /// How a selection gives what it found (2026-11-15, experiments/m6-select-eval and m6-drop-fm).
    public enum Rule: Sendable {
        /// The program's 【相關記憶】: the embedding channel at `[select] similarity`; only candidates
        /// whose cosine is at least that (one tied only by FTS, the entity or the hop is not given);
        /// best by the cheap score (cosine + `entity_weight` when it has the front app's entry, ties
        /// by key); at most `[select] attach_max`.
        case attach
        /// Her `recall`: no gate beyond the search. The embedding channel is the `[select]
        /// recall_candidates` best by cosine, whatever their cosine; everything related ranked by
        /// RRF (`[memory] rrf_k`) over two lists: text (how many of the query's words it contains,
        /// then cosine) and cosine; the `recall_limit` best (ties by key).
        case recall
    }

    /// One selection: the front app's entry (`appEntry`), everything related (`Memory.related`),
    /// each through `prepare` (under which key; nil leaves it out; one per key, the best kept), then
    /// what `rule` gives, best first.
    public func run(_ t: Trigger, rule: Rule, visible: Set<MemoryFolder>, tiers: Set<SourceTier>?,
                    today: TodayIndex?, prepare: (Candidate) throws -> String?) throws -> SelectionReport {
        var report = SelectionReport(trigger: t.label)
        var clock = ContinuousClock.now
        var ids: Set<String> = []
        if let app = t.app {
            let e = try appEntry(app)
            report.entities.append("\(app.name)→" + (e.map { "\($0.id)「\($0.name)」" } ?? "none"))
            if let e { ids.insert(e.id) }
        }
        report.seconds.entity = lap(&clock)

        let embedding: Memory.EmbeddingChannel = switch rule {
        case .attach: .atLeast(config.similarity)
        case .recall: .best(config.recallCandidates)
        }
        let related = try memory.related(t.query, entities: ids, visible: visible, tiers: tiers, today: today,
                                         embedding: embedding)
        report.related = related.count
        for c in Candidate.Channel.allCases { report.channels[c] = related.filter { $0.channels.contains(c) }.count }
        report.seconds.related = lap(&clock)

        let score: [String: Double]
        let keep: (Candidate) -> Bool
        let limit: Int
        switch rule {
        case .attach:
            score = Dictionary(related.map { ($0.key, $0.cosine + config.entityWeight * Double($0.triggerEntities.count)) },
                               uniquingKeysWith: { a, _ in a })
            keep = { $0.cosine >= self.config.similarity }
            limit = config.attachMax
        case .recall:
            score = recallScores(related, query: t.query)
            keep = { _ in true }
            limit = config.recallLimit
        }
        var byKey: [String: (candidate: Candidate, score: Double)] = [:]
        for c in related {
            guard let key = try prepare(c) else { continue }
            let s = score[c.key]!
            if let have = byKey[key], have.score >= s { continue }
            var moved = c
            moved.key = key
            byKey[key] = (moved, s)
        }
        report.prepared = byKey.count
        let ranked = byKey.values.sorted { $0.score != $1.score ? $0.score > $1.score : $0.candidate.key < $1.candidate.key }
        report.ranked = ranked.prefix(SelectionReport.shown).map { ($0.candidate.id, $0.score, $0.candidate.cosine) }
        report.selected = Array(ranked.filter { keep($0.candidate) }.prefix(limit).map(\.candidate))
        report.seconds.rank = lap(&clock)
        return report
    }

    /// `Rule.recall`'s RRF score per candidate key.
    func recallScores(_ related: [Candidate], query: String) -> [String: Double] {
        let terms = Memory.queryPhrases(query, maxPhrases: memory.config.maxQueryPhrases)
            + Memory.shortTerms(query, maxTerms: memory.config.maxQueryPhrases)
        func words(_ c: Candidate) -> Int {
            let text = c.hit.text.lowercased()
            return terms.filter { text.contains($0) }.count
        }
        func byCosine(_ a: Candidate, _ b: Candidate) -> Bool { a.cosine != b.cosine ? a.cosine > b.cosine : a.key < b.key }
        let counted = related.map { ($0, words($0)) }
        let text = counted.filter { $0.1 > 0 }.sorted { $0.1 != $1.1 ? $0.1 > $1.1 : byCosine($0.0, $1.0) }.map(\.0)
        let cosine = related.sorted(by: byCosine)
        var fused: [String: Double] = [:]
        for list in [text, cosine] {
            for (rank, c) in list.enumerated() { fused[c.key, default: 0] += 1.0 / Double(memory.config.rrfK + rank + 1) }
        }
        return fused
    }
}

/// One run of the pipeline, for the log and the turn report.
public struct SelectionReport: Sendable {
    public struct Seconds: Sendable {
        /// The front app's entry, `Memory.related`, ranking.
        public var entity = 0.0, related = 0.0, rank = 0.0
        public var total: Double { entity + related + rank }
    }

    /// How many of the ranked the log line shows.
    static let shown = 10

    public var trigger: String
    /// The front app: name→entry id「canonical」, or →none when the registry has no entry for it.
    public var entities: [String] = []
    /// How many `Memory.related` found, in all and per channel (one candidate may count in several).
    public var related = 0
    public var channels: [Candidate.Channel: Int] = [:]
    /// How many were left after `prepare`.
    public var prepared = 0
    /// The first `shown` by the rule's ranking, with its score and cosine.
    public var ranked: [(id: String, score: Double, cosine: Double)] = []
    /// What is given, best first.
    public var selected: [Candidate] = []
    public var seconds = Seconds()

    public var line: String {
        func f(_ s: Double) -> String { String(format: "%.2f", s) }
        let ch = Candidate.Channel.allCases.map { "\($0.rawValue) \(channels[$0] ?? 0)" }.joined(separator: ", ")
        return "選記憶（\(trigger)）entities [\(entities.joined(separator: ", "))]; related \(related) (\(ch)), prepared \(prepared); "
            + "ranked [\(ranked.map { "\($0.id) \(String(format: "%.4f", $0.score)) cos \(f($0.cosine))" }.joined(separator: ", "))]; "
            + "given \(selected.count) [\(selected.map(\.id).joined(separator: ", "))]; "
            + "seconds entity \(f(seconds.entity)), related \(f(seconds.related)), "
            + "rank \(f(seconds.rank)), total \(f(seconds.total))"
    }
}
