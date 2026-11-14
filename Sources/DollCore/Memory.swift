import Foundation

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
    case developer   // the developer's words in `dollctl chat` (2026-11-12)
    case tool        // command / job output
    case stranger    // someone other than the owner (no such input until M6; reserved)
    case doll        // her own guess
    case program     // read by the program itself, no model in between (an app's menu shortcuts)
    case selfReported = "self_reported"   // her computer actions, kept after she said the session reached what she expected
    case consolidated   // written by consolidation (2026-11-11): a separate model request over the day's timeline
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
    /// Answering the developer (`dollctl chat`, 2026-11-12), or her own work in a task the developer gave.
    case developer
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

/// What kind of thing a registry entry is (2026-11-11, SAG's shape). A label: entries are told
/// apart by their descriptions, not their types.
public enum EntityType: String, Sendable, CaseIterable, Codable {
    /// An application.
    case app
    /// A UI element, dialog or menu item.
    case ui
    /// A keyboard shortcut.
    case shortcut
    /// A file or path.
    case file
    case website
    case person
    case other
}

/// One thing a memory names (2026-11-11): the registry entry (`Registry`) by id, and the name as
/// the memory's text writes it. Two memories sharing an entry are neighbours: the candidate
/// pipeline expands one hop through them (`Memory.related`).
public struct MemoryEntity: Sendable, Equatable, Codable {
    /// The registry entry's id.
    public var id: String
    /// As it appears in the text.
    public var name: String

    public init(_ id: String, _ name: String) { (self.id, self.name) = (id, name) }

    /// `entities` as one line of JSON in the front-matter.
    static func json(_ list: [MemoryEntity]) -> String {
        let enc = JSONEncoder()
        enc.outputFormatting = [.withoutEscapingSlashes, .sortedKeys]
        return String(decoding: (try? enc.encode(list)) ?? Data("[]".utf8), as: UTF8.self)
    }

    /// A list as the front-matter gives it: each an object with exactly a non-blank `id` and
    /// `name`; no id twice. Anything else names what is wrong.
    static func validated(_ value: Any) throws -> [MemoryEntity] {
        guard let items = value as? [Any] else { throw MemoryError(description: "entities must be an array") }
        var out: [MemoryEntity] = []
        var ids = Set<String>()
        for (i, item) in items.enumerated() {
            guard let o = item as? [String: Any], Set(o.keys) == ["id", "name"],
                  let id = o["id"] as? String, let name = o["name"] as? String else {
                throw MemoryError(description: "entity \(i + 1) must be an object with exactly id and name, both strings")
            }
            let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !id.isEmpty, !trimmed.isEmpty else { throw MemoryError(description: "entity \(i + 1): id or name is empty") }
            guard ids.insert(id).inserted else { throw MemoryError(description: "entity \(i + 1): \(id) is listed twice") }
            out.append(MemoryEntity(id, trimmed))
        }
        return out
    }

    /// Drops later entities with an earlier one's id (program-built lists).
    static func unique(_ list: [MemoryEntity]) -> [MemoryEntity] {
        var seen = Set<String>()
        return list.filter { seen.insert($0.id).inserted }
    }
}

/// Why and when a memory stopped counting (consolidation, 2026-11-11): its file stays, with these in
/// the front-matter, out of the index (so out of recall and attaching); consolidation deletes it once
/// it has been retired longer than `[consolidate] retired_keep_days`.
public struct Retirement: Sendable, Equatable {
    public var ts: Double
    /// The id of the memory that took its place.
    public var supersededBy: String
    public var reason: String

    public init(ts: Double, supersededBy: String, reason: String) {
        self.ts = ts
        self.supersededBy = supersededBy
        self.reason = reason
    }
}

/// One memory file: a `+++` front-matter block, then the text verbatim. A record about one app at
/// one version (a menu shortcut the program read) also names the app's bundle id and version; an
/// experience, the owner's line it was for (JSON string) and its stats; a retired one, its
/// `Retirement`; any, its entities (one JSON line, absent when none).
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
    public var retired: Retirement?
    public var entities: [MemoryEntity] = []
    public var body: String

    /// The file's bytes; `Memory.parseFile` of them gives this value back.
    public var text: String {
        var lines = ["+++", "kind = \"\(kind)\"", "confidence = \(confidence)", "created_ts = \(ts)",
                     "source_event_seq = \(sourceSeq)", "source_tier = \"\(tier.rawValue)\""]
        if !app.isEmpty { lines += ["app = \"\(app)\"", "app_version = \"\(appVersion)\""] }
        if let task { lines.append("task = " + Self.json(task)) }
        if let s = stats {
            lines += ["turns = \(s.turns)", "seconds = \(s.seconds)", "attached_followed = \(s.attachedFollowed)",
                      "attached_success = \(s.attachedSuccess)", "attached_failed = \(s.attachedFailed)"]
        }
        if let r = retired {
            lines += ["retired_ts = \(r.ts)", "superseded_by = \"\(r.supersededBy)\"", "retired_reason = " + Self.json(r.reason)]
        }
        if !entities.isEmpty { lines.append("entities = " + MemoryEntity.json(entities)) }
        return (lines + ["+++", body]).joined(separator: "\n")
    }

    /// A string as a one-line JSON string (front-matter values that may hold quotes or newlines).
    static func json(_ s: String) -> String {
        let enc = JSONEncoder()
        enc.outputFormatting = .withoutEscapingSlashes
        return String(decoding: (try? enc.encode(s)) ?? Data("\"\"".utf8), as: UTF8.self)
    }
}

/// An experience memory as the program attaches it (and, 2026-11-11, a consolidated memory: no
/// stats, no steps).
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
    /// The bracket's label: the level, with the kind when the file has one (consolidated memories may).
    public var label: String { MemoryKind(rawValue: file.kind).map { "\(level)的\($0.label)" } ?? level }
}

/// The today index (2026-11-11: in memory): FTS5 trigram + embedding over the current timeline
/// segment, the same method as the memory index. Built from the segment's JSONL at start and fed on
/// every append; never on disk, never the source.
private let todaySchema = """
CREATE TABLE IF NOT EXISTS today_entries (
    id TEXT PRIMARY KEY,             -- <seq>-<part>: owner | developer | say-<channel> | thought | result | watch
    seq INTEGER NOT NULL,
    ts REAL NOT NULL,
    label TEXT NOT NULL,             -- 主人說 | 開發者說 | 你說｜<channel> | 你想 | <tool> 的結果 | watch
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
    /// 主人說 / 開發者說 / 你說｜`<channel>` / 你想 / `<tool> 的結果` / watch.
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
    private let sql: SQLite
    private let embedder: any Embedder

    /// Indexes every event of the timeline so far.
    init(_ timeline: Timeline, embedder: any Embedder) throws {
        sql = try SQLite(path: nil, schema: todaySchema)
        self.embedder = embedder
        let names = Doll.callNames(timeline.events)
        let rows = try put(timeline.events.map { ($0, names[$0.seq!]) })
        try sql.transaction { try insert(rows) }
    }

    /// What of one event is searchable: the owner's text; what her reply said with the speaking tools
    /// (one entry per channel, labelled 你說｜Discord 私訊 / 你說｜終端機 / 你說｜Discord 語音) and her
    /// thought; a tool result's text (computer results are already trimmed to what changed and the
    /// open windows; never images); a watch wake's text. Not the program's 【快捷鍵】 and 【相關記憶】,
    /// not `recall` results (copies of other data), and not a scenario's enter result such as
    /// `use_computer`'s (2026-11-11: the program's fixed menu text, not something that happened).
    /// Empty texts are left out.
    static func entries(_ ev: Event, tool: String?) -> [(id: String, label: String, text: String)] {
        let seq = ev.seq!
        var out: [(String, String, String)] = []
        switch ev.kind {
        case .owner: out = [("\(seq)-owner", "主人說", ev.text)]
        case .developer: out = [("\(seq)-developer", "開發者說", ev.text)]
        case .doll:
            let said = ToolCall.said(ev.calls)
            out = Channel.allCases.map { ch in
                ("\(seq)-say-\(ch.rawValue)", "你說｜\(ch.label)", said.filter { $0.channel == ch }.map(\.text).joined(separator: "\n"))
            } + [("\(seq)-thought", "你想", ev.text)]
        case .tool:
            let name = (tool ?? "").isEmpty ? "tool_call" : tool!
            if !unindexedResults.contains(name) { out = [("\(seq)-result", "\(name) 的結果", ev.text)] }
        case .watch: out = [("\(seq)-watch", "watch", ev.text)]
        case .memory: break
        }
        return out.map { ($0.0, $0.1, $0.2.trimmingCharacters(in: .whitespacesAndNewlines)) }.filter { !$0.2.isEmpty }
    }

    /// Tools whose results the today index leaves out.
    static let unindexedResults: Set<String> = Set(["recall"] + ScenarioKind.allCases.map(\.enterTool))

    /// The rows of `events`, their texts embedded in one call.
    private func put(_ events: [(Event, String?)]) throws -> [(Event, String, String, String, Data)] {
        let list = events.flatMap { ev, tool in Self.entries(ev, tool: tool).map { (ev, $0) } }
        let vectors = try embedder.embed(list.map(\.1.text), as: .document)
        return zip(list, vectors).map { (item, v) in (item.0, item.1.id, item.1.label, item.1.text, Memory.blob(v)) }
    }

    private func insert(_ rows: [(Event, String, String, String, Data)]) throws {
        for (ev, id, label, text, vector) in rows {
            try sql.exec("INSERT INTO today_entries (id, seq, ts, label, secret, text, vector) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         [.text(id), .int(ev.seq!), .real(ev.ts), .text(label), .int(ev.secret ? 1 : 0), .text(text), .blob(vector)])
            try sql.exec("INSERT INTO today_fts (text, id, secret) VALUES (?, ?, ?)",
                         [.text(text), .text(id), .int(ev.secret ? 1 : 0)])
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
        let rows = try put([(ev, tool)])
        try sql.transaction { try insert(rows) }
    }

    /// FTS5 hits ordered by BM25 rank; secret events only when `secret`; every hit.
    func fts(match: String, secret: Bool) throws -> [(id: String, rank: Double)] {
        try sql.query("""
            SELECT id, rank FROM today_fts WHERE today_fts MATCH ? AND (? OR secret = 0) ORDER BY rank, id
            """, [.text(match), .int(secret ? 1 : 0)]) { ($0.text(0)!, $0.real(1)) }
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
    let embedder: any Embedder
    /// The entity registry the memories' entities name (`<memory.dir>/<registry_dir>/`).
    public let registry: Registry

    init(config: MemoryConfig, root: URL, store: Store, embedder: any Embedder) throws {
        self.config = config
        self.root = root
        self.store = store
        self.embedder = embedder
        let fm = FileManager.default
        for dir in [config.secretDir, config.ownDir, config.publicDir] {
            try fm.createDirectory(at: root.appending(path: dir), withIntermediateDirectories: true)
        }
        registry = try Registry(dir: root.appending(path: config.registryDir), store: store, embedder: embedder, config: config)
        // An empty index is current whatever built it: writes into it need no rebuild at the next boot.
        if try store.countMemoryIndex() == 0, try store.countRegistryIndex() == 0 {
            try store.setMemoryIndexVersion(Self.indexVersion)
        }
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
    /// that already saw a secret event, and never a stranger's or the developer's. `own` (her own
    /// passwords, tokens) is for the owner's turns, the developer's and her own, never a stranger's.
    /// `public` is for everyone.
    public static func visible(_ audience: Audience, sawSecret: Bool) -> Set<MemoryFolder> {
        switch audience {
        case .stranger: [.public]
        case .developer: [.public, .own]
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
        var retiredTS: Double?, supersededBy = "", reason = ""
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
            case "retired_ts": retiredTS = Double(bare)
            case "superseded_by": supersededBy = bare
            case "retired_reason": reason = (try? JSONDecoder().decode(String.self, from: Data(value.utf8))) ?? ""
            case "entities":
                guard let list = try? JSONSerialization.jsonObject(with: Data(value.utf8)) else {
                    throw MemoryError(description: "entities is not JSON: \(value)")
                }
                f.entities = try MemoryEntity.validated(list)
            default: break
            }
        }
        f.stats = turns.map { ExperienceStats(turns: $0, seconds: seconds, attachedFollowed: followed,
                                              attachedSuccess: succeeded, attachedFailed: failed) }
        f.retired = retiredTS.map { Retirement(ts: $0, supersededBy: supersededBy, reason: reason) }
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
                      app: AppIdentity? = nil, experience: (task: String, stats: ExperienceStats)? = nil,
                      entities: [MemoryEntity] = []) throws -> String {
        let body = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !body.isEmpty else { throw MemoryError(description: "remember item with no text") }
        guard let confidence = config.confidence[tier] else {
            throw MemoryError(description: "no starting confidence configured for tier \(tier.rawValue)")
        }
        let entities = MemoryEntity.unique(entities)
        try checkEntities(entities)
        let ts = Date().timeIntervalSince1970
        let id = "\(sourceSeq)-\(Self.hex(6))"
        let url = directory(f).appending(path: "\(id).md")
        let file = MemoryFile(sourceSeq: sourceSeq, ts: ts, confidence: confidence, kind: kind?.rawValue ?? "", tier: tier,
                              app: app?.bundleID ?? "", appVersion: app?.version ?? "",
                              task: experience?.task, stats: experience?.stats, entities: entities, body: body)
        try file.text.write(to: url, atomically: true, encoding: .utf8)
        try index(id, url: url, folder: dirName(f), file)
        return id
    }

    /// Every entity must name an existing registry entry.
    private func checkEntities(_ list: [MemoryEntity]) throws {
        for e in list where try registry.entry(e.id) == nil {
            throw MemoryError(description: "entity \(e.name) names registry entry \(e.id), which does not exist")
        }
    }

    /// One file's index rows: metadata, FTS text, vector (of its text, `vector` when the caller
    /// already has it), registry entity ids.
    private func index(_ id: String, url: URL, folder: String, _ file: MemoryFile, vector: [Float]? = nil) throws {
        try store.putMemoryIndex(id: id, path: url.path, folder: folder, confidence: file.confidence, ts: file.ts,
                                 sourceSeq: file.sourceSeq, kind: file.kind, tier: file.tier.rawValue,
                                 entities: file.entities.map(\.id), text: file.body,
                                 vector: Self.blob(try vector ?? embedder.embed(file.body, as: .document)))
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
                guard parsed.tier == .selfReported, parsed.retired == nil, !parsed.body.isEmpty else { continue }
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

    /// A memory by id, when consolidation wrote it and it is not retired: what attaching shows for a
    /// consolidated hit (2026-11-11, provisional).
    func consolidated(_ id: String) throws -> Experience? {
        guard let url = url(of: id) else { return nil }
        let parsed = try Self.parseFile(String(contentsOf: url, encoding: .utf8))
        guard parsed.tier == .consolidated, parsed.retired == nil, !parsed.body.isEmpty else { return nil }
        let folder = url.deletingLastPathComponent().lastPathComponent
        return Experience(id: id, folder: folder, secret: folder == config.secretDir, level: level(parsed.confidence), file: parsed)
    }

    /// One memory file by id: its folder and parsed contents; nil when no folder holds it.
    func file(_ id: String) throws -> (folder: MemoryFolder, file: MemoryFile)? {
        guard let url = url(of: id) else { return nil }
        let dir = url.deletingLastPathComponent().lastPathComponent
        guard let folder = MemoryFolder.allCases.first(where: { dirName($0) == dir }) else { return nil }
        return (folder, try Self.parseFile(String(contentsOf: url, encoding: .utf8)))
    }

    /// Replaces a memory's text and entities (consolidation's update); the rest of its front-matter
    /// stays, the index follows.
    func update(_ id: String, text: String, entities: [MemoryEntity]) throws {
        let body = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !body.isEmpty else { throw MemoryError(description: "update of \(id) with no text") }
        guard let url = url(of: id) else { throw MemoryError(description: "no memory file \(id)") }
        var parsed = try Self.parseFile(String(contentsOf: url, encoding: .utf8))
        guard parsed.retired == nil else { throw MemoryError(description: "memory \(id) is retired") }
        parsed.body = body
        parsed.entities = MemoryEntity.unique(entities)
        try checkEntities(parsed.entities)
        try parsed.text.write(to: url, atomically: true, encoding: .utf8)
        try index(id, url: url, folder: url.deletingLastPathComponent().lastPathComponent, parsed)
    }

    /// Marks a memory retired in its front-matter and takes it out of the index; the file stays.
    func retire(_ id: String, _ r: Retirement) throws {
        guard let url = url(of: id) else { throw MemoryError(description: "no memory file \(id)") }
        var parsed = try Self.parseFile(String(contentsOf: url, encoding: .utf8))
        guard parsed.retired == nil else { throw MemoryError(description: "memory \(id) is already retired") }
        parsed.retired = r
        try parsed.text.write(to: url, atomically: true, encoding: .utf8)
        try store.deleteMemoryIndex(id: id)
    }

    /// Deletes every file retired before `cutoff` (seconds since 1970); returns their ids.
    func deleteRetired(before cutoff: Double) throws -> [String] {
        var gone: [String] = []
        for f in MemoryFolder.allCases {
            let names = (try? FileManager.default.contentsOfDirectory(atPath: directory(f).path)) ?? []
            for name in names.sorted() where name.hasSuffix(".md") {
                let url = directory(f).appending(path: name)
                guard let r = try Self.parseFile(String(contentsOf: url, encoding: .utf8)).retired, r.ts < cutoff else { continue }
                try FileManager.default.removeItem(at: url)
                gone.append(String(name.dropLast(3)))
            }
        }
        return gone
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
        let vectors = try embedder.embed(list.map(\.task), as: .document)
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

    /// Number of records on disk that belong in the index: not retired, not empty.
    private func fileCount() throws -> Int {
        var n = 0
        for f in MemoryFolder.allCases {
            let names = (try? FileManager.default.contentsOfDirectory(atPath: directory(f).path)) ?? []
            for name in names where name.hasSuffix(".md") {
                let parsed = try Self.parseFile(String(contentsOf: directory(f).appending(path: name), encoding: .utf8))
                if parsed.retired == nil, !parsed.body.isEmpty { n += 1 }
            }
        }
        return n
    }

    /// The index's version: change it whenever vectors or index rows would come out different from
    /// the same files, so an index built by older code is rebuilt at boot. 2: vectors are the mean
    /// of every token (1 stopped after the first token, 2026-11-09). 3: entity keys (2026-11-11).
    /// 4: registry entity ids and the registry's own index (2026-11-11). 5: Qwen3-Embedding in place
    /// of `NLContextualEmbedding` (2026-11-11).
    static let indexVersion = 5

    /// Called at boot: files written by a previous process (or by hand) are not in the index,
    /// and a new process would not find them. If the counts disagree (memories or registry
    /// entries), or older code built the index (`indexVersion`), the whole index is rebuilt —
    /// records are small and few, a full rebuild is cheaper than tracking which are missing.
    func syncOnBoot() throws {
        if try store.memoryIndexVersion() != Self.indexVersion || (try fileCount()) != (try store.countMemoryIndex())
            || registry.fileCount() != (try store.countRegistryIndex()) {
            try rebuild()
        }
    }

    /// Drops every index row and re-reads the markdown files, the registry's first (spec 6: SQLite
    /// is a rebuildable index). A memory naming a registry entry that has no file is an error.
    public func rebuild() throws {
        try store.clearMemoryIndex()
        try store.setMemoryIndexVersion(Self.indexVersion)
        try registry.rebuild()
        var files: [(id: String, url: URL, folder: String, file: MemoryFile)] = []
        for f in MemoryFolder.allCases {
            let dir = directory(f)
            guard let names = try? FileManager.default.contentsOfDirectory(atPath: dir.path) else { continue }
            for name in names.sorted() where name.hasSuffix(".md") {
                let url = dir.appending(path: name)
                let parsed = try Self.parseFile(String(contentsOf: url, encoding: .utf8))
                guard !parsed.body.isEmpty, parsed.retired == nil else { continue }
                do { try checkEntities(parsed.entities) } catch { throw MemoryError(description: "\(url.path): \(error)") }
                files.append((String(name.dropLast(3)), url, dirName(f), parsed))
            }
        }
        let vectors = try embedder.embed(files.map(\.file.body), as: .document)
        for (f, v) in zip(files, vectors) { try index(f.id, url: f.url, folder: f.folder, f.file, vector: v) }
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
    /// hits. With `tiers`, only records of those source tiers: every record is ranked by vector, not
    /// just the first `max_candidates`, so a few among many others are not lost. (Consolidation's
    /// related memories; her recall and the 【相關記憶】 go through `related` and `Selector`.)
    public func search(_ query: String, visible: Set<MemoryFolder>, limit: Int,
                       tiers: Set<SourceTier>? = nil) throws -> [MemoryHit] {
        try rank(query, vector: try embedder.embed(query, as: .query), visible: visible, limit: limit, tiers: tiers)
    }

    /// Everything related to a trigger (2026-11-11), with no count limit: in `visible` folders,
    /// only `tiers` when given, each memory or today's event once with every channel that found it:
    /// - fts: an FTS5 hit for `query` (memories, and today's events when `today` is given);
    /// - embedding: cosine to `query` at least `similarity`;
    /// - entity: has one of `entities` (the trigger's aligned registry entries);
    /// - hop: shares a registry entry with an fts or embedding hit other than itself (one hop).
    /// Today's events have no entities: fts and embedding only. In key order.
    public func related(_ query: String, entities: Set<String>, visible: Set<MemoryFolder>, tiers: Set<SourceTier>?,
                        today: TodayIndex?, similarity: Double) throws -> [Candidate] {
        let names = MemoryFolder.allCases.filter(visible.contains).map(dirName)
        guard !names.isEmpty else { return [] }
        let tierNames = tiers.map { $0.map(\.rawValue).sorted() }
        let qv = try embedder.embed(query, as: .query)
        let phrases = Self.queryPhrases(query, maxPhrases: config.maxQueryPhrases)
        let match = phrases.isEmpty ? nil
            : phrases.map { "\"\($0.replacingOccurrences(of: "\"", with: "\"\""))\"" }.joined(separator: " OR ")
        var channels: [String: Set<Candidate.Channel>] = [:]
        let cosines = Dictionary(uniqueKeysWithValues: try store.memoryVectors(folders: names).map { ("m:" + $0.id, Self.dot($0.vector, qv)) })
        if let match {
            for h in try store.memoryFTS(match: match, folders: names, limit: nil) { channels["m:" + h.id, default: []].insert(.fts) }
        }
        for (key, c) in cosines where c >= similarity { channels[key, default: []].insert(.embedding) }
        var todayCosines: [String: Double] = [:]
        if let today {
            let secret = visible.contains(.secret)
            if let match { for h in try today.fts(match: match, secret: secret) { channels["t:" + h.id, default: []].insert(.fts) } }
            for (id, v) in try today.vectors(secret: secret) {
                let c = Self.dot(v, qv)
                todayCosines["t:" + id] = c
                if c >= similarity { channels["t:" + id, default: []].insert(.embedding) }
            }
        }
        // Seeds for the hop: the memories fts or embedding found (with `tiers`, only of those tiers).
        var hits: [String: MemoryHit] = [:]
        func hit(_ id: String) throws -> MemoryHit? {
            if let h = hits[id] { return h }
            let h = try memoryHit(id, folders: names, tiers: tiers)
            hits[id] = h
            return h
        }
        let seeds = try channels.keys.filter { $0.hasPrefix("m:") }.map { String($0.dropFirst(2)) }.filter { try hit($0) != nil }
        let seedEntities = try store.memoryEntities(seeds)
        var seedsWith: [String: Set<String>] = [:]
        for (id, list) in seedEntities { for e in list { seedsWith[e, default: []].insert(id) } }
        for (id, _) in try store.memoriesWithEntities(entities.sorted(), folders: names, tiers: tierNames) {
            channels["m:" + id, default: []].insert(.entity)
        }
        var hopVia: [String: [String]] = [:]
        for (id, list) in try store.memoriesWithEntities(seedsWith.keys.sorted(), folders: names, tiers: tierNames) {
            let via = list.filter { !(seedsWith[$0] ?? []).subtracting([id]).isEmpty }
            guard !via.isEmpty else { continue }
            hopVia[id] = via
            channels["m:" + id, default: []].insert(.hop)
        }
        let memoryIDs = channels.keys.filter { $0.hasPrefix("m:") }.map { String($0.dropFirst(2)) }
        let own = try store.memoryEntities(memoryIDs)
        var out: [Candidate] = []
        for (key, found) in channels.sorted(by: { $0.key < $1.key }) {
            let id = String(key.dropFirst(2))
            if key.hasPrefix("t:") {
                guard let today, let h = try today.entry(id) else { continue }
                out.append(Candidate(key: key, hit: .today(h), channels: found, cosine: todayCosines[key] ?? 0,
                                     triggerEntities: [], hopEntities: []))
                continue
            }
            guard let h = try hit(id) else { continue }
            out.append(Candidate(key: key, hit: .memory(h), channels: found, cosine: cosines[key] ?? 0,
                                 triggerEntities: (own[id] ?? []).filter(entities.contains), hopEntities: hopVia[id] ?? []))
        }
        return out
    }

    /// One memory as a hit, when its index row is in `folders`, its file is not empty and (with
    /// `tiers`) its tier is one of them.
    private func memoryHit(_ id: String, folders: [String], tiers: Set<SourceTier>?) throws -> MemoryHit? {
        guard let meta = try store.memoryMeta(id), folders.contains(meta.folder),
              let body = try? String(contentsOfFile: meta.path, encoding: .utf8) else { return nil }
        let parsed = try Self.parseFile(body)
        guard !parsed.body.isEmpty, tiers?.contains(parsed.tier) ?? true else { return nil }
        return MemoryHit(id: id, folder: meta.folder, secret: meta.folder == config.secretDir,
                         confidence: meta.confidence, level: level(meta.confidence),
                         kind: MemoryKind(rawValue: parsed.kind), text: parsed.body)
    }

    static func dot(_ vector: Data, _ qv: [Float]) -> Double {
        var dot = 0.0
        vector.withUnsafeBytes { raw in
            let v = raw.bindMemory(to: Float.self)
            for i in 0..<min(v.count, qv.count) { dot += Double(v[i]) * Double(qv[i]) }
        }
        return dot
    }

    /// FTS5 and cosine lists over the memories, fused by RRF, plus `confidence_weight` × confidence.
    private func rank(_ query: String, vector qv: [Float], visible: Set<MemoryFolder>, limit: Int,
                      tiers: Set<SourceTier>?) throws -> [MemoryHit] {
        let names = MemoryFolder.allCases.filter(visible.contains).map(dirName)
        guard !names.isEmpty else { return [] }
        let phrases = Self.queryPhrases(query, maxPhrases: config.maxQueryPhrases)
        var found: [(String, Double)] = []
        if !phrases.isEmpty {
            let match = phrases.map { "\"\($0.replacingOccurrences(of: "\"", with: "\"\""))\"" }.joined(separator: " OR ")
            found = try store.memoryFTS(match: match, folders: names, limit: config.maxCandidates).map { ($0.id, $0.rank) }
        }
        let scored = try store.memoryVectors(folders: names).map { ($0.id, Self.dot($0.vector, qv)) }
        let ftsRanked = found.map(\.0)
        let vectorRanked = scored.sorted { $0.1 != $1.1 ? $0.1 > $1.1 : $0.0 < $1.0 }
            .prefix(tiers == nil ? config.maxCandidates : scored.count).map(\.0)

        // RRF over both lists, then confidence as an extra weighted term (spec 6).
        var fused: [String: Double] = [:]
        for (rank, id) in ftsRanked.enumerated() { fused[id, default: 0] += 1.0 / Double(config.rrfK + rank + 1) }
        for (rank, id) in vectorRanked.enumerated() { fused[id, default: 0] += 1.0 / Double(config.rrfK + rank + 1) }
        var hits: [(score: Double, hit: MemoryHit)] = []
        for (id, score) in fused {
            guard let hit = try memoryHit(id, folders: names, tiers: tiers) else { continue }
            hits.append((score + config.confidenceWeight * hit.confidence, hit))
        }
        return hits.sorted { $0.score != $1.score ? $0.score > $1.score : $0.hit.id < $1.hit.id }.prefix(limit).map(\.hit)
    }
}

/// One candidate of `Memory.related`: what found it, its cosine to the query, and the registry
/// entries that tie it to the trigger.
public struct Candidate: Sendable {
    public enum Channel: String, Sendable, CaseIterable, Comparable {
        case fts, embedding, entity, hop
        public static func < (a: Channel, b: Channel) -> Bool { allCases.firstIndex(of: a)! < allCases.firstIndex(of: b)! }
    }
    /// "m:<memory id>" or "t:<today entry id>".
    public var key: String
    public var hit: RecallHit
    public var channels: Set<Channel>
    public var cosine: Double
    /// The trigger's registry entries it has.
    public var triggerEntities: [String]
    /// The registry entries it shares with an fts or embedding hit (how the hop found it).
    public var hopEntities: [String]

    public var id: String { String(key.dropFirst(2)) }
}

/// Seconds since `clock`, which moves to now.
func lap(_ clock: inout ContinuousClock.Instant) -> Double {
    let now = ContinuousClock.now
    let d = clock.duration(to: now)
    clock = now
    return Double(d.components.seconds) + Double(d.components.attoseconds) / 1e18
}
