import Foundation
import NaturalLanguage

/// M3a memory (spec 6): the markdown files under `<memory.dir>/<secret_dir>/`, `<own_dir>/` and
/// `<public_dir>/` are the source of truth. The SQLite tables in `Store`
/// (FTS5 trigram + embedding blobs + metadata) are only an index, rebuildable with
/// `rebuild()`. No fallback: if the embedding model is unavailable, that is an error.
public struct MemoryError: Error, CustomStringConvertible {
    public let description: String
}

/// Where a record's content came from; decides its starting confidence (config `memory.confidence_*`).
public enum SourceTier: String, Sendable, CaseIterable {
    case owner       // the owner's own words
    case tool        // command / job output
    case stranger    // someone other than the owner (no such input until M6; reserved)
    case doll        // her own guess
    case program     // read by the program itself, no model in between (an app's menu shortcuts)
    case selfReported = "self_reported"   // her computer actions, kept after she said the session reached what she expected
}

/// What a memory says about its event (spec 6), given by her on `remember`. Facts and
/// preferences have none. Only `happened` counts as already happened.
public enum MemoryKind: String, Sendable, CaseIterable {
    case happened, intended, uncertain

    /// How recall shows it after the confidence level: 「確定的打算」「聽說的已發生」.
    public var label: String {
        switch self {
        case .happened: "已發生"
        case .intended: "打算"
        case .uncertain: "未證實"
        }
    }
}

/// Which layer a memory is in (spec 6, 2026-11-08). The directory names come from config.
public enum MemoryFolder: String, Sendable, CaseIterable {
    /// The owner's secrets.
    case secret
    /// Her own secrets: her passwords, accounts, tokens.
    case own
    case `public`
}

/// Whose turn a recall runs in; decides the folders it reads (`Memory.visible`).
public enum Audience: Sendable {
    /// Answering the owner.
    case owner
    /// Her own work, woken by something she set up (a watch).
    case doll
    /// Answering someone other than the owner (no such input until M6).
    case stranger
}

public struct MemoryHit: Sendable {
    public var id: String
    public var folder: String       // the config directory name of secret | own | public
    public var secret: Bool         // in the owner's secret folder
    public var confidence: Double
    public var level: String        // 確定 / 大概 / 聽說, from the confidence thresholds
    /// nil when she gave none (front-matter `kind = ""`): a fact or a preference.
    public var kind: MemoryKind?
    public var text: String

    /// What she sees: the level and the kind together, 「確定的打算」「聽說的已發生」, or the level alone (spec 6).
    public var label: String { kind.map { "\(level)的\($0.label)" } ?? level }
}

/// An experience's stats (spec 3.1, 2026-11-09): in its file's front-matter, so the embedded text
/// stays what she did. `turns` and `seconds`: the task that wrote it, from the owner's line to her
/// `outcome`. The other three: how often it was attached and then followed, and how those ended.
public struct ExperienceStats: Sendable, Equatable {
    public var turns: Int
    public var seconds: Int
    public var attachedFollowed: Int
    public var attachedSuccess: Int
    public var attachedFailed: Int

    public init(turns: Int, seconds: Int, attachedFollowed: Int = 0, attachedSuccess: Int = 0, attachedFailed: Int = 0) {
        self.turns = turns
        self.seconds = seconds
        self.attachedFollowed = attachedFollowed
        self.attachedSuccess = attachedSuccess
        self.attachedFailed = attachedFailed
    }

    /// The first line of the experience when attached; she sees it, the embedding does not.
    public var line: String {
        "成績：這次 \(turns) 回合、\(seconds) 秒；附上後照做 \(attachedFollowed) 次，成功 \(attachedSuccess) 次"
            + "（這是已知可行的做法；如果看到更短的走法，可以直接走。）"
    }
}

/// One memory file: a `+++` front-matter block, then the text verbatim. A record about one app at
/// one version (a menu shortcut the program read) also names the app's bundle id and version; an
/// experience, the owner's line it was for (JSON string) and its stats.
public struct MemoryFile: Sendable, Equatable {
    public var sourceSeq: Int64
    public var ts: Double
    public var confidence: Double
    public var kind: String
    public var tier: SourceTier
    public var app = ""
    public var appVersion = ""
    public var task: String?
    public var stats: ExperienceStats?
    public var body: String

    /// The file's bytes; `Memory.parseFile` of them gives this value back.
    public var text: String {
        var lines = ["+++", "kind = \"\(kind)\"", "confidence = \(confidence)", "created_ts = \(ts)",
                     "source_event_seq = \(sourceSeq)", "source_tier = \"\(tier.rawValue)\""]
        if !app.isEmpty { lines += ["app = \"\(app)\"", "app_version = \"\(appVersion)\""] }
        if let task {
            let enc = JSONEncoder()
            enc.outputFormatting = .withoutEscapingSlashes
            lines.append("task = " + String(decoding: (try? enc.encode(task)) ?? Data("\"\"".utf8), as: UTF8.self))
        }
        if let s = stats {
            lines += ["turns = \(s.turns)", "seconds = \(s.seconds)", "attached_followed = \(s.attachedFollowed)",
                      "attached_success = \(s.attachedSuccess)", "attached_failed = \(s.attachedFailed)"]
        }
        return (lines + ["+++", body]).joined(separator: "\n")
    }
}

/// An experience memory as the program attaches it.
public struct Experience: Sendable {
    public var id: String
    public var folder: String
    public var secret: Bool
    /// 確定 / 大概 / 聽說 (experiences have no kind).
    public var level: String
    public var file: MemoryFile

    public var stats: ExperienceStats? { file.stats }
    /// The owner's line it was for: front-matter `task`, or (a file without one) the text's first
    /// line after 「經驗：」.
    public var task: String {
        if let t = file.task { return t }
        let first = file.body.prefix { $0 != "\n" }
        return String(first.hasPrefix("經驗：") ? first.dropFirst(3) : first)
    }
    /// Its numbered steps after the 「步驟：」 line, without the numbers (steps cut for length are not
    /// there). Only after that line: her summary above it may number things too.
    public var steps: [String] {
        let lines = file.body.components(separatedBy: "\n")
        guard let start = lines.firstIndex(of: "步驟：") else { return [] }
        return lines[(start + 1)...].compactMap { line in
            guard let dot = line.range(of: ". "), !line[..<dot.lowerBound].isEmpty,
                  line[..<dot.lowerBound].allSatisfy(\.isASCII), line[..<dot.lowerBound].allSatisfy(\.isNumber) else { return nil }
            return String(line[dot.upperBound...])
        }
    }
    /// What she is shown: the stats line (when it has stats), then the text verbatim.
    public var attachedText: String { (stats.map { $0.line + "\n" } ?? "") + file.body }
}

/// `NLContextualEmbedding`, mean-pooled over L2-normalized token vectors (Apple's
/// documented pooling for subword embeddings), then L2-normalized again.
final class Embedder {
    let language: NLLanguage
    private let model: NLContextualEmbedding

    init(language: String) throws {
        guard #available(macOS 13.0, *) else {
            throw MemoryError(description: "NLContextualEmbedding is unavailable on this system")
        }
        let lang = NLLanguage(rawValue: language)
        guard let m = NLContextualEmbedding(language: lang) else {
            throw MemoryError(description: "no contextual embedding model for \(language)")
        }
        guard m.hasAvailableAssets else {
            throw MemoryError(description: "embedding model assets for \(language) are not on this device")
        }
        do { try m.load() } catch {
            throw MemoryError(description: "cannot load the embedding model for \(language): \(error)")
        }
        self.language = lang
        model = m
    }

    func embed(_ s: String) throws -> [Float] {
        guard #available(macOS 13.0, *) else {
            throw MemoryError(description: "NLContextualEmbedding is unavailable on this system")
        }
        let result: NLContextualEmbeddingResult
        do { result = try model.embeddingResult(for: s, language: language) } catch {
            throw MemoryError(description: "embedding failed: \(error)")
        }
        var sums: [Double] = []
        // The block returns whether to go on: true visits every token (false stopped after the first).
        result.enumerateTokenVectors(in: result.string.startIndex..<result.string.endIndex) { token, _ in
            let norm = token.reduce(0) { $0 + Double($1) * Double($1) }.squareRoot()
            guard norm > 0 else { return true }
            if sums.isEmpty { sums = token.map { Double($0) / norm } }
            else { for i in 0..<token.count { sums[i] += Double(token[i]) / norm } }
            return true
        }
        guard !sums.isEmpty else { throw MemoryError(description: "embedding of an empty string") }
        let n = Double(sums.count)
        let mean = sums.map { $0 / n }
        let norm = mean.reduce(0) { $0 + $1 * $1 }.squareRoot()
        guard norm > 0 else { throw MemoryError(description: "embedding of an unembeddable string") }
        return mean.map { Float($0 / norm) }
    }
}

public final class Memory {
    let config: MemoryConfig
    private let root: URL
    private let store: Store
    private let embedder: Embedder

    init(config: MemoryConfig, root: URL, store: Store) throws {
        self.config = config
        self.root = root
        self.store = store
        embedder = try Embedder(language: config.language)
        let fm = FileManager.default
        for dir in [config.secretDir, config.ownDir, config.publicDir] {
            try fm.createDirectory(at: root.appending(path: dir), withIntermediateDirectories: true)
        }
        // An empty index is current whatever built it: writes into it need no rebuild at the next boot.
        if try store.countMemoryIndex() == 0 { try store.setMemoryIndexVersion(Self.indexVersion) }
    }

    static func blob(_ v: [Float]) -> Data {
        Data(bytes: v, count: v.count * MemoryLayout<Float>.size)
    }

    func dirName(_ f: MemoryFolder) -> String {
        switch f {
        case .secret: config.secretDir
        case .own: config.ownDir
        case .public: config.publicDir
        }
    }

    private func directory(_ f: MemoryFolder) -> URL { root.appending(path: dirName(f)) }

    /// Which folders a turn may read (spec 6, 2026-11-08). `secret` keeps the 5.1 rule: only a turn
    /// that already saw a secret event, and never a stranger's. `own` (her own passwords, tokens) is
    /// for the owner's turns and her own, never a stranger's. `public` is for everyone.
    public static func visible(_ audience: Audience, sawSecret: Bool) -> Set<MemoryFolder> {
        switch audience {
        case .stranger: [.public]
        case .owner, .doll: sawSecret ? [.public, .own, .secret] : [.public, .own]
        }
    }

    private static func hex(_ n: Int) -> String {
        String(UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased().prefix(n))
    }

    // markdown files ----------------------------------------------------------
    /// The file of the memory `id`, in whichever folder holds it; nil when none does.
    private func url(of id: String) -> URL? {
        MemoryFolder.allCases.map { directory($0).appending(path: "\(id).md") }
            .first { FileManager.default.fileExists(atPath: $0.path) }
    }

    static func parseFile(_ text: String) throws -> MemoryFile {
        let lines = text.components(separatedBy: "\n")
        guard lines.first == "+++", let end = lines.dropFirst().firstIndex(of: "+++") else {
            throw MemoryError(description: "no +++ front-matter block")
        }
        var f = MemoryFile(sourceSeq: 0, ts: 0, confidence: 0, kind: "", tier: .doll, body: "")
        var turns: Int?, seconds = 0, followed = 0, succeeded = 0, failed = 0
        for line in lines[1..<end] {
            let parts = line.split(separator: "=", maxSplits: 1, omittingEmptySubsequences: false)
            guard parts.count == 2 else { continue }
            let key = parts[0].trimmingCharacters(in: .whitespaces)
            let value = parts[1].trimmingCharacters(in: .whitespaces)
            let bare = value.hasPrefix("\"") && value.hasSuffix("\"") && value.count >= 2
                ? String(value.dropFirst().dropLast()) : value
            switch key {
            case "kind": f.kind = bare
            case "confidence": f.confidence = Double(bare) ?? 0
            case "created_ts": f.ts = Double(bare) ?? 0
            case "source_event_seq": f.sourceSeq = Int64(bare) ?? 0
            case "source_tier": f.tier = SourceTier(rawValue: bare) ?? .doll
            case "app": f.app = bare
            case "app_version": f.appVersion = bare
            case "task": f.task = try? JSONDecoder().decode(String.self, from: Data(value.utf8))
            case "turns": turns = Int(bare)
            case "seconds": seconds = Int(bare) ?? 0
            case "attached_followed": followed = Int(bare) ?? 0
            case "attached_success": succeeded = Int(bare) ?? 0
            case "attached_failed": failed = Int(bare) ?? 0
            default: break
            }
        }
        f.stats = turns.map { ExperienceStats(turns: $0, seconds: seconds, attachedFollowed: followed,
                                              attachedSuccess: succeeded, attachedFailed: failed) }
        f.body = lines[(end + 1)...].joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
        return f
    }

    /// Bundle id → version of every app whose shortcuts are in her public memories (front-matter `app`).
    func learnedApps() throws -> [String: String] {
        var out: [String: String] = [:]
        for (_, parsed) in try appRecords() { out[parsed.app] = parsed.appVersion }
        return out
    }

    /// Deletes every public record about `bundleID` (all versions), files and index rows. Returns how many.
    @discardableResult
    func forgetApp(_ bundleID: String) throws -> Int {
        let gone = try appRecords().filter { $0.1.app == bundleID }
        for (url, _) in gone {
            try FileManager.default.removeItem(at: url)
            try store.deleteMemoryIndex(id: String(url.lastPathComponent.dropLast(3)))
        }
        return gone.count
    }

    private func appRecords() throws -> [(URL, MemoryFile)] {
        let dir = directory(.public)
        let names = (try? FileManager.default.contentsOfDirectory(atPath: dir.path)) ?? []
        return try names.sorted().filter { $0.hasSuffix(".md") }.compactMap { name in
            let url = dir.appending(path: name)
            let parsed = try Self.parseFile(String(contentsOf: url, encoding: .utf8))
            return parsed.app.isEmpty ? nil : (url, parsed)
        }
    }

    /// An experience also gets the owner's line it was for and its stats in the front-matter, so the
    /// embedded text stays what she did (spec 3.1, 2026-11-09).
    @discardableResult
    public func write(_ text: String, folder f: MemoryFolder, sourceSeq: Int64, tier: SourceTier, kind: MemoryKind?,
                      app: AppIdentity? = nil, experience: (task: String, stats: ExperienceStats)? = nil) throws -> String {
        let body = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !body.isEmpty else { throw MemoryError(description: "remember item with no text") }
        guard let confidence = config.confidence[tier] else {
            throw MemoryError(description: "no starting confidence configured for tier \(tier.rawValue)")
        }
        let ts = Date().timeIntervalSince1970
        let id = "\(sourceSeq)-\(Self.hex(6))"
        let folder = dirName(f)
        let url = directory(f).appending(path: "\(id).md")
        let file = MemoryFile(sourceSeq: sourceSeq, ts: ts, confidence: confidence, kind: kind?.rawValue ?? "", tier: tier,
                              app: app?.bundleID ?? "", appVersion: app?.version ?? "",
                              task: experience?.task, stats: experience?.stats, body: body)
        try file.text.write(to: url, atomically: true, encoding: .utf8)
        let vector = try embedder.embed(body)
        try store.putMemoryIndex(id: id, path: url.path, folder: folder, confidence: confidence,
                                ts: ts, sourceSeq: sourceSeq, kind: kind?.rawValue ?? "", text: body, vector: Self.blob(vector))
        return id
    }

    // experiences -------------------------------------------------------------
    /// 確定 / 大概 / 聽說 for a confidence (spec 6).
    func level(_ confidence: Double) -> String {
        confidence >= config.highConfidence ? "確定" : confidence >= config.mediumConfidence ? "大概" : "聽說"
    }

    /// Every experience (tier self_reported) in the folders `visible` names, by id.
    func experiences(visible: Set<MemoryFolder>) throws -> [Experience] {
        var out: [Experience] = []
        for f in MemoryFolder.allCases where visible.contains(f) {
            let names = (try? FileManager.default.contentsOfDirectory(atPath: directory(f).path)) ?? []
            for name in names.sorted() where name.hasSuffix(".md") {
                let parsed = try Self.parseFile(String(contentsOf: directory(f).appending(path: name), encoding: .utf8))
                guard parsed.tier == .selfReported, !parsed.body.isEmpty else { continue }
                out.append(Experience(id: String(name.dropLast(3)), folder: dirName(f), secret: f == .secret,
                                      level: level(parsed.confidence), file: parsed))
            }
        }
        return out.sorted { $0.id < $1.id }
    }

    /// One experience by id; nil when no folder holds it or it is not an experience.
    func experience(_ id: String) throws -> Experience? {
        guard let url = url(of: id) else { return nil }
        let parsed = try Self.parseFile(String(contentsOf: url, encoding: .utf8))
        guard parsed.tier == .selfReported else { return nil }
        let folder = url.deletingLastPathComponent().lastPathComponent
        return Experience(id: id, folder: folder, secret: folder == config.secretDir, level: level(parsed.confidence), file: parsed)
    }

    /// Changes an experience's stats in its file; the text, and so the index, stay as they are.
    /// Returns the new stats; nil when the file has none (written before stats existed).
    @discardableResult
    func updateStats(_ id: String, _ change: (inout ExperienceStats) -> Void) throws -> ExperienceStats? {
        guard let url = url(of: id) else { throw MemoryError(description: "no memory file \(id)") }
        var parsed = try Self.parseFile(String(contentsOf: url, encoding: .utf8))
        guard var stats = parsed.stats else { return nil }
        change(&stats)
        parsed.stats = stats
        try parsed.text.write(to: url, atomically: true, encoding: .utf8)
        return stats
    }

    /// Experiences in groups: two whose owner lines embed at least `experience_group_similarity`
    /// alike are in one group, and so is anything linked through a chain of such pairs.
    func experienceGroups(_ list: [Experience]) throws -> [[Experience]] {
        let vectors = try list.map { try embedder.embed($0.task) }
        let groups = Self.groups(list.count) { i, j in
            zip(vectors[i], vectors[j]).reduce(0.0) { $0 + Double($1.0) * Double($1.1) } >= config.experienceGroupSimilarity
        }
        return groups.map { $0.map { list[$0] } }
    }

    /// Connected components of `0..<n` under `similar`; each group in index order, groups by their first index.
    static func groups(_ n: Int, similar: (Int, Int) -> Bool) -> [[Int]] {
        var parent = Array(0..<n)
        func root(_ i: Int) -> Int { parent[i] == i ? i : root(parent[i]) }
        for i in 0..<n {
            for j in (i + 1)..<n where similar(i, j) {
                let a = root(i), b = root(j)
                if a != b { parent[max(a, b)] = min(a, b) }
            }
        }
        var byRoot: [Int: [Int]] = [:]
        for i in 0..<n { byRoot[root(i), default: []].append(i) }
        return byRoot.values.sorted { $0[0] < $1[0] }
    }

    /// Best first (spec 3.1, 2026-11-09, [Claude 定的]): any attached-then-failed goes below those
    /// without; then fewest turns of the run that wrote it; more attached successes only break
    /// ties. Experiences without stats come after those with; the id settles the rest.
    public static func ranked(_ list: [Experience]) -> [Experience] {
        func key(_ e: Experience) -> (Int, Int, Int, Int) {
            guard let s = e.stats else { return (1, 0, 0, 0) }
            return (0, s.attachedFailed > 0 ? 1 : 0, s.turns, -s.attachedSuccess)
        }
        return list.sorted { a, b in
            let ka = key(a), kb = key(b)
            return ka != kb ? ka < kb : a.id < b.id
        }
    }

    /// Number of records on disk (the source of truth).
    private func fileCount() -> Int {
        let fm = FileManager.default
        return MemoryFolder.allCases.reduce(0) { n, f in
            n + ((try? fm.contentsOfDirectory(atPath: directory(f).path)) ?? [])
                .filter { $0.hasSuffix(".md") }.count
        }
    }

    /// The index's version: change it whenever vectors or index rows would come out different from
    /// the same files, so an index built by older code is rebuilt at boot. 2: vectors are the mean
    /// of every token (1 stopped after the first token, 2026-11-09).
    static let indexVersion = 2

    /// Called at boot: files written by a previous process (or by hand) are not in the index,
    /// and a new process would not find them. If the counts disagree, or older code built the
    /// index (`indexVersion`), the whole index is rebuilt — records are small and few, a full
    /// rebuild is cheaper than tracking which are missing.
    func syncOnBoot() throws {
        if try store.memoryIndexVersion() != Self.indexVersion || fileCount() != (try store.countMemoryIndex()) {
            try rebuild()
        }
    }

    /// Drops every index row and re-reads the markdown files (spec 6: SQLite is a rebuildable index).
    public func rebuild() throws {
        try store.clearMemoryIndex()
        try store.setMemoryIndexVersion(Self.indexVersion)
        let fm = FileManager.default
        for f in MemoryFolder.allCases {
            let dir = directory(f)
            let folder = dirName(f)
            guard let names = try? fm.contentsOfDirectory(atPath: dir.path) else { continue }
            for name in names.sorted() where name.hasSuffix(".md") {
                let url = dir.appending(path: name)
                let raw = try String(contentsOf: url, encoding: .utf8)
                let parsed = try Self.parseFile(raw)
                guard !parsed.body.isEmpty else { continue }
                let id = String(name.dropLast(3))
                let vector = try embedder.embed(parsed.body)
                try store.putMemoryIndex(id: id, path: url.path, folder: folder, confidence: parsed.confidence,
                                        ts: parsed.ts, sourceSeq: parsed.sourceSeq, kind: parsed.kind,
                                        text: parsed.body, vector: Self.blob(vector))
            }
        }
    }

    // retrieval ---------------------------------------------------------------
    /// Phrases for the FTS5 trigram index: contiguous CJK runs become every 3-character
    /// window (trigrams), Latin/digit runs of 3+ stay whole. Anything shorter cannot match
    /// a trigram index. Phrases are quoted, OR-joined at the call site.
    static func queryPhrases(_ query: String, maxPhrases: Int) -> [String] {
        var out: [String] = []
        var latin = ""
        var cjk = ""
        func flushLatin() { if latin.count >= 3 { out.append(latin.lowercased()) }; latin = "" }
        func flushCJK() {
            if cjk.count >= 3 {
                let chars = Array(cjk)
                for i in 0...(chars.count - 3) { out.append(String(chars[i..<(i + 3)])) }
            }
            cjk = ""
        }
        for scalar in query.unicodeScalars {
            if scalar.value >= 0x2E80 && scalar.value <= 0x9FFF {
                flushLatin()
                cjk.unicodeScalars.append(scalar)
            } else if scalar.isASCII, Character(scalar).isLetter || Character(scalar).isNumber {
                flushCJK()
                latin.unicodeScalars.append(scalar)
            } else {
                flushLatin()
                flushCJK()
            }
        }
        flushLatin()
        flushCJK()
        var seen = Set<String>()
        var unique: [String] = []
        for p in out where seen.insert(p).inserted { unique.append(p) }
        return Array(unique.prefix(maxPhrases))
    }

    /// Searches only the folders `visible` lets this turn read (`Memory.visible`); at most `limit`
    /// hits (default `top_k`). With `tiers`, only records of those source tiers: every record is
    /// ranked by vector, not just the first `max_candidates`, so a few among many others are not lost.
    public func search(_ query: String, visible: Set<MemoryFolder>, limit: Int? = nil,
                       tiers: Set<SourceTier>? = nil) throws -> [MemoryHit] {
        let names = MemoryFolder.allCases.filter(visible.contains).map(dirName)
        guard !names.isEmpty else { return [] }
        var ftsRanked: [String] = []
        let phrases = Self.queryPhrases(query, maxPhrases: config.maxQueryPhrases)
        if !phrases.isEmpty {
            let match = phrases.map { "\"\($0.replacingOccurrences(of: "\"", with: "\"\""))\"" }.joined(separator: " OR ")
            ftsRanked = try store.memoryFTS(match: match, folders: names, limit: config.maxCandidates)
        }
        let qv = try embedder.embed(query)
        let scored = try store.memoryVectors(folders: names).map { row -> (String, Double) in
            var dot = 0.0
            row.vector.withUnsafeBytes { raw in
                let v = raw.bindMemory(to: Float.self)
                for i in 0..<min(v.count, qv.count) { dot += Double(v[i]) * Double(qv[i]) }
            }
            return (row.id, dot)
        }
        let vectorRanked = scored.sorted { $0.1 > $1.1 }.prefix(tiers == nil ? config.maxCandidates : scored.count).map(\.0)

        // RRF over both lists, then confidence as an extra weighted term (spec 6).
        var fused: [String: Double] = [:]
        for (rank, id) in ftsRanked.enumerated() { fused[id, default: 0] += 1.0 / Double(config.rrfK + rank + 1) }
        for (rank, id) in vectorRanked.enumerated() { fused[id, default: 0] += 1.0 / Double(config.rrfK + rank + 1) }
        var hits: [MemoryHit] = []
        for (id, var score) in fused {
            guard let meta = try store.memoryMeta(id) else { continue }
            if !names.contains(meta.folder) { continue }
            score += config.confidenceWeight * meta.confidence
            guard let body = try? String(contentsOfFile: meta.path, encoding: .utf8) else { continue }
            let parsed = try Self.parseFile(body)
            guard !parsed.body.isEmpty, tiers?.contains(parsed.tier) ?? true else { continue }
            let level = self.level(meta.confidence)
            hits.append(MemoryHit(id: id, folder: meta.folder, secret: meta.folder == config.secretDir,
                                  confidence: meta.confidence, level: level,
                                  kind: MemoryKind(rawValue: parsed.kind), text: parsed.body))
            fused[id] = score
        }
        return hits.sorted {
            let a = fused[$0.id] ?? 0, b = fused[$1.id] ?? 0
            return a != b ? a > b : $0.id < $1.id
        }.prefix(limit ?? config.topK).map { $0 }
    }
}
