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
            + "（這是已知可行的做法；如果看到更短的走法，可以直接走；結果猜得到的幾步可以寫在同一則回覆裡。）"
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

/// The today index (2026-11-10): `<data_dir>/today.db`, FTS5 trigram + embedding over the current
/// timeline segment, the same method as the memory index. Rebuildable from the segment's JSONL,
/// never the source.
private let todaySchema = """
CREATE TABLE IF NOT EXISTS today_meta (
    one INTEGER PRIMARY KEY CHECK (one = 1),
    version INTEGER NOT NULL,        -- TodayIndex.version of the code that built it
    segment TEXT NOT NULL,           -- the timeline segment it indexes
    seq INTEGER NOT NULL             -- the newest event of that segment it has taken in
);
CREATE TABLE IF NOT EXISTS today_entries (
    id TEXT PRIMARY KEY,             -- <seq>-<part>: owner | say | thought | result | watch
    seq INTEGER NOT NULL,
    ts REAL NOT NULL,
    label TEXT NOT NULL,             -- 主人說 | 你說 | 你想 | <tool> 的結果 | watch
    secret INTEGER NOT NULL,
    text TEXT NOT NULL,
    vector BLOB NOT NULL             -- Float32, L2-normalized
);
CREATE VIRTUAL TABLE IF NOT EXISTS today_fts USING fts5(
    text, id UNINDEXED, secret UNINDEXED, tokenize='trigram'
);
"""

/// One of today's events as recall shows it.
public struct TodayHit: Sendable {
    public var id: String
    public var seq: Int64
    public var ts: Double
    /// 主人說 / 你說 / 你想 / `<tool> 的結果` / watch.
    public var label: String
    public var secret: Bool
    public var text: String

    /// What she sees in the bracket: 「今天 14:05｜主人說」.
    public var tag: String {
        let c = Calendar.current.dateComponents([.hour, .minute], from: Date(timeIntervalSince1970: ts))
        return String(format: "今天 %02d:%02d｜", c.hour!, c.minute!) + label
    }
}

/// One recall result: a long-term memory or one of today's events.
public enum RecallHit: Sendable {
    case memory(MemoryHit)
    case today(TodayHit)

    public var secret: Bool {
        switch self {
        case .memory(let h): h.secret
        case .today(let h): h.secret
        }
    }

    /// 〔確定的打算｜public〕 or 〔今天 14:05｜主人說〕, then the text.
    public var rendered: String {
        switch self {
        case .memory(let h): "〔\(h.label)｜\(h.folder)〕\n\(h.text)"
        case .today(let h): "〔\(h.tag)〕\n\(h.text)"
        }
    }
}

public final class TodayIndex {
    /// Change it whenever entries or vectors would come out different from the same timeline, so an
    /// index built by older code is rebuilt at start.
    static let version = 1
    private let sql: SQLite
    private let embedder: Embedder

    init(path: URL, embedder: Embedder) throws {
        sql = try SQLite(path: path, schema: todaySchema)
        self.embedder = embedder
    }

    /// What of one event is searchable: the owner's text; her reply's `say` texts (together) and her
    /// thought; a tool result's text (computer results are already trimmed to what changed and the
    /// open windows; never images); a watch wake's text. Not the program's 【快捷鍵】 and 【相關記憶】,
    /// and not `recall` results: those are copies of other data. Empty texts are left out.
    static func entries(_ ev: Event, tool: String?) -> [(id: String, label: String, text: String)] {
        let seq = ev.seq!
        var out: [(String, String, String)] = []
        switch ev.kind {
        case .owner: out = [("\(seq)-owner", "主人說", ev.text)]
        case .doll:
            let parsed = ParsedReply(ev.text)
            out = [("\(seq)-say", "你說", parsed.said.joined(separator: "\n")), ("\(seq)-thought", "你想", parsed.thought)]
        case .tool:
            let name = (tool ?? "").isEmpty ? "tool_call" : tool!
            if name != "recall" { out = [("\(seq)-result", "\(name) 的結果", ev.text)] }
        case .watch: out = [("\(seq)-watch", "watch", ev.text)]
        case .memory: break
        }
        return out.map { ($0.0, $0.1, $0.2.trimmingCharacters(in: .whitespacesAndNewlines)) }.filter { !$0.2.isEmpty }
    }

    private func meta() throws -> (version: Int, segment: String, seq: Int64)? {
        try sql.query("SELECT version, segment, seq FROM today_meta WHERE one = 1") {
            (Int($0.int(0)!), $0.text(1)!, $0.int(2)!)
        }.first
    }

    private func setSeq(_ seq: Int64, segment: String) throws {
        try sql.exec("""
            INSERT INTO today_meta (one, version, segment, seq) VALUES (1, ?, ?, ?)
            ON CONFLICT(one) DO UPDATE SET version = excluded.version, segment = excluded.segment, seq = excluded.seq
            """, [.int(Int64(Self.version)), .text(segment), .int(seq)])
    }

    private func put(_ ev: Event, tool: String?) throws -> [(Event, String, String, String, Data)] {
        try Self.entries(ev, tool: tool).map { (ev, $0.id, $0.label, $0.text, Memory.blob(try embedder.embed($0.text))) }
    }

    private func insert(_ rows: [(Event, String, String, String, Data)]) throws {
        for (ev, id, label, text, vector) in rows {
            try sql.exec("INSERT INTO today_entries (id, seq, ts, label, secret, text, vector) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         [.text(id), .int(ev.seq!), .real(ev.ts), .text(label), .int(ev.secret ? 1 : 0), .text(text), .blob(vector)])
            try sql.exec("INSERT INTO today_fts (text, id, secret) VALUES (?, ?, ?)",
                         [.text(text), .text(id), .int(ev.secret ? 1 : 0)])
        }
    }

    /// At start: rebuilt when it was built by other code, for another segment, or has not taken in
    /// every event (a crash between the timeline line and the index; a deleted index file).
    func sync(_ timeline: Timeline) throws {
        if let m = try meta(), m.version == Self.version, m.segment == timeline.segment, m.seq == timeline.lastSeq { return }
        try rebuild(timeline)
    }

    /// Drops every entry and indexes the segment again from its events.
    func rebuild(_ timeline: Timeline) throws {
        let names = Doll.callNames(timeline.events)
        let rows = try timeline.events.flatMap { try put($0, tool: names[$0.seq!]) }
        try sql.transaction {
            try sql.exec("DELETE FROM today_entries")
            try sql.exec("DELETE FROM today_fts")
            try insert(rows)
            try setSeq(timeline.lastSeq, segment: timeline.segment)
        }
    }

    /// Takes in the timeline's newest event, right after it was appended.
    func add(_ timeline: Timeline) throws {
        let events = timeline.events
        guard let ev = events.last else { return }
        var tool: String?
        if ev.kind == .tool {
            let tail = Array(events[(events.lastIndex { $0.kind == .doll } ?? 0)...])
            tool = Doll.callNames(tail)[ev.seq!]
        }
        let rows = try put(ev, tool: tool)
        try sql.transaction {
            try insert(rows)
            try setSeq(ev.seq!, segment: timeline.segment)
        }
    }

    /// FTS5 hits ordered by BM25 rank; secret events only when `secret`.
    func fts(match: String, secret: Bool, limit: Int) throws -> [(id: String, rank: Double)] {
        try sql.query("""
            SELECT id, rank FROM today_fts WHERE today_fts MATCH ? AND (? OR secret = 0) ORDER BY rank, id LIMIT ?
            """, [.text(match), .int(secret ? 1 : 0), .int(Int64(limit))]) { ($0.text(0)!, $0.real(1)) }
    }

    func vectors(secret: Bool) throws -> [(id: String, vector: Data)] {
        try sql.query("SELECT id, vector FROM today_entries WHERE (? OR secret = 0)", [.int(secret ? 1 : 0)]) {
            ($0.text(0)!, $0.blob(1))
        }
    }

    func entry(_ id: String) throws -> TodayHit? {
        try sql.query("SELECT id, seq, ts, label, secret, text FROM today_entries WHERE id = ?", [.text(id)]) {
            TodayHit(id: $0.text(0)!, seq: $0.int(1)!, ts: $0.real(2), label: $0.text(3)!, secret: $0.int(4)! != 0,
                     text: $0.text(5)!)
        }.first
    }

    /// Number of entries; for tests.
    func count() throws -> Int {
        try sql.query("SELECT count(*) FROM today_entries") { Int($0.int(0)!) }.first ?? 0
    }
}

public final class Memory {
    let config: MemoryConfig
    private let root: URL
    private let store: Store
    let embedder: Embedder

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

    /// Every memory file in folder `f`, newest first (ties by id), read from the markdown files; for
    /// the dashboard.
    func list(_ f: MemoryFolder) throws -> [(id: String, file: MemoryFile)] {
        let names = (try? FileManager.default.contentsOfDirectory(atPath: directory(f).path)) ?? []
        return try names.filter { $0.hasSuffix(".md") }.map { name in
            (String(name.dropLast(3)), try Self.parseFile(String(contentsOf: directory(f).appending(path: name), encoding: .utf8)))
        }.sorted { ($0.1.ts, $1.0) > ($1.1.ts, $0.0) }
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
        try rank(query, visible: visible, limit: limit ?? config.topK, tiers: tiers, today: nil).compactMap {
            if case .memory(let h) = $0 { h } else { nil }
        }
    }

    /// `recall` (2026-11-10): her memories and today's events in one list, top `top_k`. Today's
    /// secret events only where the secret folder is visible.
    public func recall(_ query: String, visible: Set<MemoryFolder>, today: TodayIndex) throws -> [RecallHit] {
        try rank(query, visible: visible, limit: config.topK, tiers: nil, today: today)
    }

    /// FTS5 and cosine lists over the memories (and today's events when `today` is given), fused by
    /// RRF, plus `confidence_weight` × confidence (a today hit counts as `today_confidence`). The two
    /// corpora are one list per method: FTS hits by BM25 score, vector hits by cosine.
    private func rank(_ query: String, visible: Set<MemoryFolder>, limit: Int, tiers: Set<SourceTier>?,
                      today: TodayIndex?) throws -> [RecallHit] {
        let names = MemoryFolder.allCases.filter(visible.contains).map(dirName)
        guard !names.isEmpty else { return [] }
        let todaySecret = visible.contains(.secret)
        // Keys: "m:<memory id>" and "t:<today entry id>".
        var ftsRanked: [String] = []
        let phrases = Self.queryPhrases(query, maxPhrases: config.maxQueryPhrases)
        if !phrases.isEmpty {
            let match = phrases.map { "\"\($0.replacingOccurrences(of: "\"", with: "\"\""))\"" }.joined(separator: " OR ")
            var found = try store.memoryFTS(match: match, folders: names, limit: config.maxCandidates).map { ("m:" + $0.id, $0.rank) }
            if let today {
                found += try today.fts(match: match, secret: todaySecret, limit: config.maxCandidates).map { ("t:" + $0.id, $0.rank) }
            }
            ftsRanked = found.sorted { $0.1 != $1.1 ? $0.1 < $1.1 : $0.0 < $1.0 }.prefix(config.maxCandidates).map(\.0)
        }
        let qv = try embedder.embed(query)
        func cosine(_ vector: Data) -> Double {
            var dot = 0.0
            vector.withUnsafeBytes { raw in
                let v = raw.bindMemory(to: Float.self)
                for i in 0..<min(v.count, qv.count) { dot += Double(v[i]) * Double(qv[i]) }
            }
            return dot
        }
        var scored = try store.memoryVectors(folders: names).map { ("m:" + $0.id, cosine($0.vector)) }
        if let today { scored += try today.vectors(secret: todaySecret).map { ("t:" + $0.id, cosine($0.vector)) } }
        let vectorRanked = scored.sorted { $0.1 != $1.1 ? $0.1 > $1.1 : $0.0 < $1.0 }
            .prefix(tiers == nil ? config.maxCandidates : scored.count).map(\.0)

        // RRF over both lists, then confidence as an extra weighted term (spec 6).
        var fused: [String: Double] = [:]
        for (rank, key) in ftsRanked.enumerated() { fused[key, default: 0] += 1.0 / Double(config.rrfK + rank + 1) }
        for (rank, key) in vectorRanked.enumerated() { fused[key, default: 0] += 1.0 / Double(config.rrfK + rank + 1) }
        var hits: [(key: String, score: Double, hit: RecallHit)] = []
        for (key, score) in fused {
            let id = String(key.dropFirst(2))
            if key.hasPrefix("t:") {
                guard let today, let hit = try today.entry(id) else { continue }
                hits.append((key, score + config.confidenceWeight * config.todayConfidence, .today(hit)))
                continue
            }
            guard let meta = try store.memoryMeta(id) else { continue }
            if !names.contains(meta.folder) { continue }
            guard let body = try? String(contentsOfFile: meta.path, encoding: .utf8) else { continue }
            let parsed = try Self.parseFile(body)
            guard !parsed.body.isEmpty, tiers?.contains(parsed.tier) ?? true else { continue }
            let hit = MemoryHit(id: id, folder: meta.folder, secret: meta.folder == config.secretDir,
                                confidence: meta.confidence, level: level(meta.confidence),
                                kind: MemoryKind(rawValue: parsed.kind), text: parsed.body)
            hits.append((key, score + config.confidenceWeight * meta.confidence, .memory(hit)))
        }
        return hits.sorted { $0.score != $1.score ? $0.score > $1.score : $0.key < $1.key }.prefix(limit).map(\.hit)
    }
}
