import Foundation
import FoundationModels

/// The on-device model the memory pipeline uses (2026-11-11): the things a line of text names and
/// which registry entry a name means. (It scored each candidate memory until 2026-11-15; dropped:
/// experiments/m6-select-eval.) Production is Apple's Foundation Models
/// (`FoundationSelectionModel`); tests use a fake. No fallback: unavailable is a boot error.
public protocol SelectionModel: Sendable {
    /// Throws with the reason when the model cannot run.
    func check() async throws
    /// The things `text` names (`EntityType`), names as written.
    func entities(in text: String) async throws -> [Mention]
    /// Which of `candidates` (registry entries as `RegistryEntry.line`) `mention` most likely
    /// means, by position (0-based), or none.
    func pick(_ mention: Mention, context: String, candidates: [String]) async throws -> Int?
    /// 0–10: how sure `mention` and `entry` (a `RegistryEntry.line`) are the same thing.
    func same(_ mention: Mention, context: String, entry: String) async throws -> Int
    /// One sentence: what `mention` is and where (a new registry entry's description).
    func describe(_ mention: Mention, context: String) async throws -> String
}

public struct SelectionError: Error, CustomStringConvertible {
    public let description: String
    init(_ description: String) { self.description = description }
}

@Generable
struct FMEntity {
    @Guide(description: "種類", .anyOf(EntityType.allCases.map(\.rawValue)))
    var type: String
    @Guide(description: "名字，照原文寫")
    var name: String
}

@Generable
struct FMEntities {
    @Guide(description: "文字裡提到的具體東西；沒有就是空列表")
    var entities: [FMEntity]
}

@Generable
struct FMMatch {
    @Guide(description: "它指的那一條的編號；都不是就 0")
    var choice: Int
}

@Generable
struct FMDescription {
    @Guide(description: "一句話：它是什麼、在哪裡")
    var description: String
}

@Generable
struct FMScore {
    @Guide(description: "0 到 10 分", .range(0...10))
    var score: Int
}

/// Apple's on-device `SystemLanguageModel.default`, one fresh session per request, greedy sampling,
/// guided generation.
public struct FoundationSelectionModel: SelectionModel {
    public init() {}

    static let entityInstructions = """
        從使用者給的一段文字，找出它提到的具體東西，每個標上種類：
        - app：應用程式，例如 TextEdit、Finder、Safari
        - ui：畫面上的元素、對話框、選單項目，例如「另存新檔」對話框、Save 按鈕、File 選單
        - shortcut：快捷鍵，例如 ⌘S、⇧⌘G
        - file：檔案、資料夾或路徑，例如 notes.txt、~/Documents、桌面
        - website：網站或網址
        - person：人
        - other：其他具體的東西
        名字照原文寫，不翻譯、不改寫。抽象的動作（打開、存檔）不算。沒有就回空列表。
        """

    /// Picking alone said yes too easily (2026-11-11, 12 hand cases on the merged E/F registry:
    /// Safari → Finder, apple.com → copy.txt; 6/12 right); picking then `same` ≥ `[select]
    /// align_threshold` (7) got 10/12 (left: 文件 → none, ⌘S → ⇧⌥⌘S), about 1.4 s a name.
    static let pickInstructions = """
        你在幫一個 AI 助理整理她記憶裡提到的東西。下面先是一個名字（和它的種類、出現的上下文），然後是登記簿裡的幾個條目，每條有編號、正式名字、種類、別名和說明。
        判斷這個名字指的是哪一條：同一個東西的不同寫法算同一條（別的語言的名字、縮寫、完整路徑和只寫檔名、大小寫或全形半形不同）；名字一樣、但從說明和上下文看得出是不同的東西，就不算。
        回答它指的那一條的編號；都不是就回 0。
        """

    static let sameInstructions = """
        你在幫一個 AI 助理整理她記憶裡提到的東西。下面是一個名字（和它的種類、出現的上下文），和登記簿裡的一個條目（正式名字、種類、別名、說明）。
        打 0 到 10 分：它們是同一個東西的程度。10＝一定是同一個東西，只是寫法不同（別的語言的名字、縮寫、完整路徑和只寫檔名、大小寫或全形半形不同、多了狀態字）；0＝不同的東西，就算是同一類（例如兩個不同的 app、兩個不同的檔案、兩個不同的快捷鍵）、彼此有關、或很像。
        """

    static let describeInstructions = """
        你在幫一個 AI 助理整理她記憶裡提到的東西。下面是一個名字（和它的種類、出現的上下文）。用一句繁體中文寫它是什麼、在哪裡，例如「macOS 內建的文字編輯 app」「使用者文件資料夾裡的 notes.txt」。只寫上下文看得出來的。
        """

    static let options = GenerationOptions(samplingMode: .greedy)

    /// Available, then warmed (2026-11-11): one tiny request at boot, so her first triggered turn does
    /// not pay the cold start (measured: first call 2.9–4.2 s cold, 2.2 s after this, 1.9 s warm;
    /// `prewarm()` alone was not reliable: 2.8 and 3.9 s).
    public func check() async throws {
        switch SystemLanguageModel.default.availability {
        case .available: break
        case .unavailable(let reason):
            throw SelectionError("Apple Foundation Models (on-device) is unavailable: \(reason); the memory pipeline needs it")
        }
        do {
            _ = try await LanguageModelSession(instructions: Self.sameInstructions)
                .respond(to: "（暖機，打 0 分）", generating: FMScore.self, options: Self.options)
        } catch {
            throw SelectionError("on-device model warm-up failed: \(error)")
        }
    }

    public func entities(in text: String) async throws -> [Mention] {
        let reply: FMEntities
        do {
            reply = try await LanguageModelSession(instructions: Self.entityInstructions)
                .respond(to: text, generating: FMEntities.self, options: Self.options).content
        } catch {
            throw SelectionError("on-device entity extraction failed: \(error)")
        }
        var seen = Set<String>()
        return reply.entities.compactMap { e in
            let name = e.name.trimmingCharacters(in: .whitespacesAndNewlines)
            guard let type = EntityType(rawValue: e.type), !Registry.key(name).isEmpty, seen.insert(Registry.key(name)).inserted else { return nil }
            return Mention(type, name)
        }
    }

    public func pick(_ mention: Mention, context: String, candidates: [String]) async throws -> Int? {
        let choice: Int
        do {
            choice = try await LanguageModelSession(instructions: Self.pickInstructions)
                .respond(to: Selector.alignPrompt(mention, context: context, candidates: candidates), generating: FMMatch.self,
                         options: Self.options).content.choice
        } catch {
            throw SelectionError("on-device alignment of \(mention.name) failed: \(error)")
        }
        guard (0...candidates.count).contains(choice) else {
            throw SelectionError("on-device alignment of \(mention.name) answered \(choice); there are \(candidates.count) entries")
        }
        return choice > 0 ? choice - 1 : nil
    }

    public func same(_ mention: Mention, context: String, entry: String) async throws -> Int {
        do {
            return try await LanguageModelSession(instructions: Self.sameInstructions)
                .respond(to: Selector.samePrompt(mention, context: context, entry: entry), generating: FMScore.self,
                         options: Self.options).content.score
        } catch {
            throw SelectionError("on-device alignment check of \(mention.name) failed: \(error)")
        }
    }

    public func describe(_ mention: Mention, context: String) async throws -> String {
        let text: String
        do {
            text = try await LanguageModelSession(instructions: Self.describeInstructions)
                .respond(to: Selector.describePrompt(mention, context: context), generating: FMDescription.self,
                         options: Self.options).content.description.trimmingCharacters(in: .whitespacesAndNewlines)
        } catch {
            throw SelectionError("on-device description of \(mention.name) failed: \(error)")
        }
        guard !text.isEmpty else { throw SelectionError("on-device description of \(mention.name) is empty") }
        return text
    }
}

/// The memory pipeline (2026-11-11): names aligned to the registry, and the selection behind the
/// 【相關記憶】 and `recall`. One per process (its alignment cache lives as long as it does).
public final class Selector {
    let memory: Memory
    let model: any SelectionModel
    let config: SelectConfig
    /// Query-side alignments by the name's key (`Registry.key`); nil: no entry. Dropped whenever
    /// the registry changes.
    private var cache: [String: RegistryEntry?] = [:]
    private var cacheGeneration = -1

    public init(memory: Memory, model: any SelectionModel, config: SelectConfig) {
        self.memory = memory
        self.model = model
        self.config = config
    }

    var registry: Registry { memory.registry }

    static func alignPrompt(_ m: Mention, context: String, candidates: [String]) -> String {
        let list = candidates.isEmpty ? "（沒有相關的條目）" : candidates.enumerated().map { "[\($0.offset + 1)] \($0.element)" }.joined(separator: "\n")
        return "# 名字\n\(m.name)（種類：\(m.type.rawValue)）\n\n# 上下文\n\(context)\n\n# 登記簿\n\(list)"
    }

    static func samePrompt(_ m: Mention, context: String, entry: String) -> String {
        "# 名字\n\(m.name)（種類：\(m.type.rawValue)）\n\n# 上下文\n\(context)\n\n# 登記簿的條目\n\(entry)"
    }

    static func describePrompt(_ m: Mention, context: String) -> String {
        "# 名字\n\(m.name)（種類：\(m.type.rawValue)）\n\n# 上下文\n\(context)"
    }

    // alignment -------------------------------------------------------------------------

    /// The on-device model's alignment among `candidates`: it picks one (or none), then scores how
    /// sure the name and that entry are the same thing; at least `[select] align_threshold` is a
    /// match. With how it went: "model <score>", "none" (picked none), "rejected <name> <score>".
    nonisolated(nonsending) func confirmed(_ m: Mention, context: String,
                                           candidates: [RegistryEntry]) async throws -> (entry: RegistryEntry?, how: String) {
        guard !candidates.isEmpty else { return (nil, "none") }
        guard let i = try await model.pick(m, context: context, candidates: candidates.map(\.line)) else { return (nil, "none") }
        let score = try await model.same(m, context: context, entry: candidates[i].line)
        return score >= config.alignThreshold ? (candidates[i], "model \(score)") : (nil, "rejected \(candidates[i].name) \(score)")
    }

    /// A name a trigger gives (the query side): the one entry whose name or alias has its key;
    /// otherwise the on-device model among `Registry.nearby` (`confirmed`). Remembered per name until
    /// the registry changes. Never writes the registry.
    nonisolated(nonsending) public func resolve(_ m: Mention, context: String) async throws -> (entry: RegistryEntry?, how: String) {
        if registry.generation != cacheGeneration {
            cache = [:]
            cacheGeneration = registry.generation
        }
        let key = Registry.key(m.name)
        if let hit = cache[key] { return (hit, "cached") }
        let exact = try registry.exact(m.name)
        if exact.count == 1 {
            cache[key] = exact[0]
            return (exact[0], "exact")
        }
        let found = try await confirmed(m, context: context, candidates: try registry.nearby(m.name, limit: max(config.lookupK, exact.count)))
        cache[key] = found.entry
        return found
    }

    /// A name a memory being written gives (`remember`): the on-device model among
    /// `Registry.nearby` (entries with its key first; `confirmed`), else a new entry it describes;
    /// always the model, so the first homonym (same name, another thing) becomes its own entry. A
    /// name aligned to an entry under another key becomes one of its aliases.
    nonisolated(nonsending) public func place(_ m: Mention, context: String) async throws -> (entry: RegistryEntry, how: String) {
        let exact = try registry.exact(m.name)
        let found = try await confirmed(m, context: context, candidates: try registry.nearby(m.name, limit: max(config.lookupK, exact.count)))
        if let e = found.entry {
            try registry.addAlias(e.id, m.name)
            return (try registry.entry(e.id)!, found.how)
        }
        let description = try await model.describe(m, context: context)
        return (try registry.create(m.type, name: m.name, aliases: [], description: description), "new (\(found.how))")
    }

    /// A name the program itself knows (its own records, the front app), with the program's aliases
    /// (a bundle id). The one entry having the name or one of them; several (homonyms): the on-device
    /// model among them (`confirmed`); none: with `fuzzy` the on-device model among
    /// `Registry.nearby` (another language's name); no match: a new entry with `description`. The
    /// program's name and aliases become the entry's aliases.
    nonisolated(nonsending) public func program(_ m: Mention, aliases: [String], description: String, fuzzy: Bool,
                        context: String) async throws -> (entry: RegistryEntry, how: String) {
        var found: [RegistryEntry] = []
        for name in [m.name] + aliases {
            for e in try registry.exact(name) where !found.contains(where: { $0.id == e.id }) { found.append(e) }
        }
        var pick: (entry: RegistryEntry?, how: String) = (nil, "none")
        if found.count == 1 {
            pick = (found[0], "exact")
        } else if !found.isEmpty || fuzzy {
            pick = try await confirmed(m, context: context, candidates: found.isEmpty ? try registry.nearby(m.name, limit: config.lookupK) : found)
        }
        guard let e = pick.entry else {
            return (try registry.create(m.type, name: m.name, aliases: aliases, description: description), "new (\(pick.how))")
        }
        for a in [m.name] + aliases { try registry.addAlias(e.id, a) }
        return (try registry.entry(e.id)!, pick.how)
    }

    /// The front app (or an app an experience used) as the program knows it: aliased by its bundle id.
    nonisolated(nonsending) public func programApp(_ app: AppIdentity, context: String) async throws -> (entry: RegistryEntry, how: String) {
        try await program(Mention(.app, app.name), aliases: [app.bundleID],
                          description: "macOS 上的 app「\(app.name)」（bundle id \(app.bundleID)）", fuzzy: true, context: context)
    }

    /// A menu shortcut's key chord as the program writes it.
    nonisolated(nonsending) public func programShortcut(_ keys: String, context: String) async throws -> RegistryEntry {
        try await program(Mention(.shortcut, keys), aliases: [], description: "鍵盤快捷鍵 \(keys)", fuzzy: false, context: context).entry
    }

    // selection --------------------------------------------------------------------------

    /// What started one selection and what it knows.
    public struct Trigger {
        /// For the log: 主人的新訊息 / 前景 app 換成 X / recall「query」.
        public var label: String
        /// What FTS and the embedding search for.
        public var query: String
        /// The names it gives (the owner's line's, the query's, the window title, the focus).
        public var mentions: [Mention]
        /// The front app, aligned as the program knows it.
        public var app: AppIdentity?
        /// Now, for the on-device model's alignment of the names: why, the last owner line, the front app.
        public var situation: String
        /// Seconds the on-device model took to find `mentions` (0 when they were known).
        public var extractSeconds: Double

        public init(label: String, query: String, mentions: [Mention], app: AppIdentity?, situation: String,
                    extractSeconds: Double = 0) {
            (self.label, self.query, self.mentions, self.app, self.situation, self.extractSeconds)
                = (label, query, mentions, app, situation, extractSeconds)
        }
    }

    /// How a selection gives what it found (2026-11-15, experiments/m6-select-eval; the on-device
    /// model no longer scores candidates).
    public enum Rule: Sendable {
        /// The program's 【相關記憶】: the embedding channel at `[select] similarity`; only candidates
        /// whose cosine is at least that (one tied only by FTS, an entity or the hop is not given);
        /// best by the cheap score (cosine + `entity_weight` × the trigger's entities it has, ties by
        /// key); at most `[select] attach_max`.
        case attach
        /// Her `recall`: no gate beyond the search. The embedding channel is the `[select]
        /// recall_limit` best by cosine, whatever their cosine; everything related ranked by RRF
        /// (`[memory] rrf_k`) over three lists: text (how many of the query's words it contains,
        /// then cosine), cosine, and the trigger's entities (how many, then cosine); the
        /// `recall_limit` best (ties by key).
        case recall
    }

    /// One selection: the trigger's names aligned (`resolve`, the front app by `programApp`),
    /// everything related (`Memory.related`), each through `prepare` (under which key; nil leaves it
    /// out; one per key, the best kept), then what `rule` gives, best first.
    nonisolated(nonsending) public func run(_ t: Trigger, rule: Rule, visible: Set<MemoryFolder>, tiers: Set<SourceTier>?,
                                            today: TodayIndex?, prepare: (Candidate) throws -> String?) async throws -> SelectionReport {
        var report = SelectionReport(trigger: t.label)
        report.seconds.extract = t.extractSeconds
        var clock = ContinuousClock.now
        var ids: [String] = []
        if let app = t.app {
            let (e, how) = try await programApp(app, context: t.situation)
            report.aligned.append("\(app.name)→\(e.id)「\(e.name)」 \(how)")
            ids.append(e.id)
        }
        var seen = Set<String>()
        for m in t.mentions where seen.insert(Registry.key(m.name)).inserted {
            let (e, how) = try await resolve(m, context: t.situation)
            report.aligned.append("\(m.name)→" + (e.map { "\($0.id)「\($0.name)」" } ?? "none") + " \(how)")
            if let e { ids.append(e.id) }
        }
        report.seconds.align = lap(&clock)

        let embedding: Memory.EmbeddingChannel = switch rule {
        case .attach: .atLeast(config.similarity)
        case .recall: .best(config.recallLimit)
        }
        let related = try memory.related(t.query, entities: Set(ids), visible: visible, tiers: tiers, today: today,
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
        let entity = related.filter { !$0.triggerEntities.isEmpty }
            .sorted { $0.triggerEntities.count != $1.triggerEntities.count ? $0.triggerEntities.count > $1.triggerEntities.count : byCosine($0, $1) }
        var fused: [String: Double] = [:]
        for list in [text, cosine, entity] {
            for (rank, c) in list.enumerated() { fused[c.key, default: 0] += 1.0 / Double(memory.config.rrfK + rank + 1) }
        }
        return fused
    }
}

/// One run of the pipeline, for the log and the turn report.
public struct SelectionReport: Sendable {
    public struct Seconds: Sendable {
        /// Finding the trigger's names, aligning them, `Memory.related`, ranking.
        public var extract = 0.0, align = 0.0, related = 0.0, rank = 0.0
        public var total: Double { extract + align + related + rank }
    }

    /// How many of the ranked the log line shows.
    static let shown = 10

    public var trigger: String
    /// Each trigger name: name→entry id「canonical」how (exact / cached / model / none / new).
    public var aligned: [String] = []
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
        return "選記憶（\(trigger)）entities [\(aligned.joined(separator: ", "))]; related \(related) (\(ch)), prepared \(prepared); "
            + "ranked [\(ranked.map { "\($0.id) \(String(format: "%.4f", $0.score)) cos \(f($0.cosine))" }.joined(separator: ", "))]; "
            + "given \(selected.count) [\(selected.map(\.id).joined(separator: ", "))]; "
            + "seconds extract \(f(seconds.extract)), align \(f(seconds.align)), related \(f(seconds.related)), "
            + "rank \(f(seconds.rank)), total \(f(seconds.total))"
    }
}
