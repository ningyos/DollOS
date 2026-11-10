import Foundation
import SQLite3

/// The memory index (M3a): rebuildable from the markdown files, never the source.
/// `tokenize='trigram'` because the records are Chinese: the default tokenizer would
/// not split them at all.
private let memorySchema = """
CREATE TABLE IF NOT EXISTS memory_index (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    folder TEXT NOT NULL,            -- the config directory name of secret | own | public
    confidence REAL NOT NULL,
    ts REAL NOT NULL,
    source_seq INTEGER NOT NULL,
    kind TEXT NOT NULL               -- happened | intended | uncertain, as she gave it; empty when she gave none
);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    text, id UNINDEXED, folder UNINDEXED, tokenize='trigram'
);
CREATE TABLE IF NOT EXISTS memory_vectors (
    id TEXT PRIMARY KEY,
    vector BLOB NOT NULL             -- Float32, L2-normalized
);
CREATE TABLE IF NOT EXISTS memory_index_version (
    one INTEGER PRIMARY KEY CHECK (one = 1),
    version INTEGER NOT NULL         -- Memory.indexVersion of the code that built the index
);
"""

public struct StoreError: Error, CustomStringConvertible {
    public let description: String
}

public enum Principal: String, Sendable, Codable {
    case owner, `internal`
}

public struct Event: Sendable, Equatable {
    public enum Kind: String, Sendable, Codable {
        case owner      // the owner spoke
        case doll       // her reply, verbatim (tool call text included)
        case tool       // the result of one of her tool calls, in call order
        case watch      // her watch woke her: the region changed, or max_wait passed
        case memory     // 【相關記憶】: records the program attached for her (spec 3.1, 2026-11-09)
    }
    public var kind: Kind
    public var principal: Principal
    public var text: String
    public var secret: Bool
    /// Only owner voice may cancel a model call in flight (voice itself is M6).
    public var voice: Bool
    /// The full-screen screenshot's file name under the data dir's `shots/`; rendered as an image part.
    public var image: String?
    /// The line rendered after that screenshot: where the front window's crop sits on the full
    /// screen, or why there is no crop.
    public var cropNote: String?
    /// The front window's crop, a file under `shots/`; rendered as an image part after `cropNote`.
    public var crop: String?
    /// The front window's outline taken with those screenshots, or why there is none followed by the
    /// menu bar's and Dock's items; rendered verbatim as the last text part.
    public var window: String?
    public var seq: Int64?
    public var ts: Double
    /// For her replies: the footer that preceded it in the prompt. Kept in the prompt so the
    /// server's prefix cache stays valid; a `footer` record in the timeline file, not an event.
    public var footer: String?

    public init(_ kind: Kind, _ principal: Principal, _ text: String, secret: Bool = false, voice: Bool = false,
                image: String? = nil, cropNote: String? = nil, crop: String? = nil, window: String? = nil) {
        self.kind = kind
        self.principal = principal
        self.text = text
        self.secret = secret
        self.voice = voice
        self.image = image
        self.cropNote = cropNote
        self.crop = crop
        self.window = window
        self.seq = nil
        self.ts = Date().timeIntervalSince1970
    }

    public var priority: Priority {
        switch kind {
        case .owner: voice ? .ownerVoice : .ownerText
        case .watch: .watch
        case .doll, .tool, .memory: .watch   // never queued; these go straight to the timeline
        }
    }
}


// timeline ---------------------------------------------------------------------

/// Her raw timeline (2026-11-10): one append-only JSONL file per segment, `<data_dir>/timeline/<segment>.jsonl`.
/// A segment is one day, from waking to falling asleep; sleep does not exist yet, so a process
/// continues the newest segment and a data dir has one. The file is the only source of the events,
/// the footers, the turn stats and the closed scenarios; `TodayIndex` is a rebuildable index of it.
///
/// One JSON object per line, told apart by `type` (keys snake_case, absent optionals left out):
///
///     {"type":"event","seq":1,"ts":…,"kind":"owner|doll|tool|watch|memory","source":"owner|internal",
///      "secret":false,"voice":false,"text":"…","image":"…","crop_note":"…","crop":"…","window":"…"}
///     {"type":"footer","seq":2,"text":"【狀況】…"}               the footer her reply `seq` answered
///     {"type":"scenario","kind":"computer","start_seq":…,"enter_result_seq":…,"end_seq":…,
///      "last_seq":…,"success":true,"summary":"…"}                   a scenario she left with `outcome`
///     {"type":"turn","seq":2,"started":…,"seconds":…,"prompt":…,"cached":…,"effort":"medium"}
///                                                                   what the turn of reply `seq` took
///
/// Her replies are stored raw (tool call text included) and re-sent byte-identical. Each line is
/// written and synced before `append` returns, so a crash loses at most the line being written; a
/// cut-off last line is dropped when the file is next opened. The whole file is read once at start
/// and kept in memory, appended to as lines are written.
public final class Timeline {
    public let file: URL
    /// The segment's id: the file name without `.jsonl` (local time it began, `yyyyMMdd-HHmmss`).
    public let segment: String
    /// Every event, oldest first, footers attached to her replies.
    public private(set) var events: [Event] = []
    /// Every closed scenario, oldest first.
    public private(set) var closedScenarios: [ClosedScenario] = []
    /// Every recorded turn by her reply's seq.
    public private(set) var turnStats: [Int64: TurnStats] = [:]
    private var position: [Int64: Int] = [:]
    private var handle: FileHandle?

    /// What one turn took, keyed by her reply's seq; for the dashboard, not the prompt.
    public struct TurnStats: Sendable, Equatable, Codable {
        public var seq: Int64
        public var started: Double
        public var seconds: Double
        public var prompt: Int
        public var cached: Int?
        public var effort: String
    }

    private struct Line: Decodable { var type: String }
    private struct EventLine: Codable {
        var type = "event"
        var seq: Int64
        var ts: Double
        var kind: Event.Kind
        var source: Principal
        var secret: Bool
        var voice: Bool
        var text: String
        var image: String?
        var cropNote: String?
        var crop: String?
        var window: String?
    }
    private struct FooterLine: Codable {
        var type = "footer"
        var seq: Int64
        var text: String
    }
    private struct ScenarioLine: Codable {
        var type = "scenario"
        var kind: ScenarioKind
        var startSeq: Int64
        var enterResultSeq: Int64
        var endSeq: Int64
        var lastSeq: Int64
        var success: Bool
        var summary: String
    }
    private struct TurnLine: Codable {
        var type = "turn"
        var seq: Int64
        var started: Double
        var seconds: Double
        var prompt: Int
        var cached: Int?
        var effort: String
    }

    private static let encoder: JSONEncoder = {
        let e = JSONEncoder()
        e.keyEncodingStrategy = .convertToSnakeCase
        e.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        return e
    }()
    private static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    /// Opens the newest segment under `dir`, or starts one when there is none.
    public init(dir: URL) throws {
        let fm = FileManager.default
        try fm.createDirectory(at: dir, withIntermediateDirectories: true)
        let names = try fm.contentsOfDirectory(atPath: dir.path).filter { $0.hasSuffix(".jsonl") }.sorted()
        if let newest = names.last {
            segment = String(newest.dropLast(".jsonl".count))
        } else {
            let f = DateFormatter()
            f.locale = Locale(identifier: "en_US_POSIX")
            f.dateFormat = "yyyyMMdd-HHmmss"
            segment = f.string(from: Date())
        }
        file = dir.appending(path: segment + ".jsonl")
        if !fm.fileExists(atPath: file.path) {
            guard fm.createFile(atPath: file.path, contents: nil) else {
                throw StoreError(description: "cannot create \(file.path)")
            }
        }
        try load()
    }

    deinit { try? handle?.close() }

    private func load() throws {
        var data = try Data(contentsOf: file)
        if let end = data.last, end != UInt8(ascii: "\n") {
            // A crash cut the last line short: drop it, so the next line starts on its own.
            let keep = (data.lastIndex(of: UInt8(ascii: "\n")).map { $0 + 1 }) ?? 0
            data = data.prefix(keep)
            let h = try FileHandle(forWritingTo: file)
            try h.truncate(atOffset: UInt64(keep))
            try h.close()
        }
        for (n, line) in data.split(separator: UInt8(ascii: "\n")).enumerated() {
            let raw = Data(line)
            func bad(_ e: Error) -> StoreError { StoreError(description: "\(file.path) line \(n + 1): \(e)") }
            do {
                switch try Self.decoder.decode(Line.self, from: raw).type {
                case "event": take(try Self.decoder.decode(EventLine.self, from: raw))
                case "footer":
                    let f = try Self.decoder.decode(FooterLine.self, from: raw)
                    guard let i = position[f.seq] else { throw StoreError(description: "footer for unknown seq \(f.seq)") }
                    events[i].footer = f.text
                case "scenario": closedScenarios.append(Self.scenario(try Self.decoder.decode(ScenarioLine.self, from: raw)))
                case "turn":
                    let t = try Self.decoder.decode(TurnLine.self, from: raw)
                    turnStats[t.seq] = TurnStats(seq: t.seq, started: t.started, seconds: t.seconds, prompt: t.prompt,
                                                 cached: t.cached, effort: t.effort)
                case let other: throw StoreError(description: "unknown record type \(other)")
                }
            } catch { throw bad(error) }
        }
    }

    private func take(_ l: EventLine) {
        var e = Event(l.kind, l.source, l.text, secret: l.secret, voice: l.voice, image: l.image, cropNote: l.cropNote,
                      crop: l.crop, window: l.window)
        e.seq = l.seq
        e.ts = l.ts
        position[l.seq] = events.count
        events.append(e)
    }

    private static func scenario(_ l: ScenarioLine) -> ClosedScenario {
        ClosedScenario(kind: l.kind, startSeq: l.startSeq, enterResultSeq: l.enterResultSeq, endSeq: l.endSeq,
                       lastSeq: l.lastSeq, success: l.success, summary: l.summary)
    }

    /// Writes one line and syncs it to disk.
    private func write<T: Encodable>(_ record: T) throws {
        if handle == nil {
            let h = try FileHandle(forWritingTo: file)
            try h.seekToEnd()
            handle = h
        }
        try handle!.write(contentsOf: try Self.encoder.encode(record) + Data([UInt8(ascii: "\n")]))
        try handle!.synchronize()
    }

    /// The newest event's seq; 0 for an empty timeline.
    public var lastSeq: Int64 { events.last?.seq ?? 0 }

    /// Appends one event and returns it with its seq.
    @discardableResult
    public func append(_ ev: Event) throws -> Event {
        let line = EventLine(seq: lastSeq + 1, ts: ev.ts, kind: ev.kind, source: ev.principal, secret: ev.secret,
                             voice: ev.voice, text: ev.text, image: ev.image, cropNote: ev.cropNote, crop: ev.crop,
                             window: ev.window)
        try write(line)
        take(line)
        return events[events.count - 1]
    }

    public func attachFooter(_ seq: Int64, _ text: String) throws {
        guard let i = position[seq] else { throw StoreError(description: "no event \(seq) to attach a footer to") }
        try write(FooterLine(seq: seq, text: text))
        events[i].footer = text
    }

    /// Records a scenario she left with `outcome`; requests render its span from then on.
    public func closeScenario(_ s: ClosedScenario) throws {
        let line = ScenarioLine(kind: s.kind, startSeq: s.startSeq, enterResultSeq: s.enterResultSeq, endSeq: s.endSeq,
                                lastSeq: s.lastSeq, success: s.success, summary: s.summary)
        try write(line)
        closedScenarios.append(Self.scenario(line))
    }

    public func recordTurn(_ t: TurnStats) throws {
        try write(TurnLine(seq: t.seq, started: t.started, seconds: t.seconds, prompt: t.prompt, cached: t.cached,
                           effort: t.effort))
        turnStats[t.seq] = t
    }
}

// SQLite -----------------------------------------------------------------------

enum Bind {
    case text(String), int(Int64), real(Double), null, blob(Data)
}

private let transient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)

struct Row {
    let stmt: OpaquePointer
    func text(_ i: Int32) -> String? {
        sqlite3_column_type(stmt, i) == SQLITE_NULL ? nil : String(cString: sqlite3_column_text(stmt, i))
    }
    func int(_ i: Int32) -> Int64? {
        sqlite3_column_type(stmt, i) == SQLITE_NULL ? nil : sqlite3_column_int64(stmt, i)
    }
    func real(_ i: Int32) -> Double { sqlite3_column_double(stmt, i) }
    func blob(_ i: Int32) -> Data {
        guard let p = sqlite3_column_blob(stmt, i) else { return Data() }
        return Data(bytes: p, count: Int(sqlite3_column_bytes(stmt, i)))
    }
}

/// One SQLite connection; only indexes live in SQLite (the memory index on disk, the today index in memory).
final class SQLite {
    private let db: OpaquePointer

    /// `path` nil: a private in-memory database, gone with the connection.
    init(path: URL?, schema: String) throws {
        if let path { try FileManager.default.createDirectory(at: path.deletingLastPathComponent(), withIntermediateDirectories: true) }
        let name = path?.path ?? ":memory:"
        var handle: OpaquePointer?
        guard sqlite3_open(name, &handle) == SQLITE_OK, let handle else {
            throw StoreError(description: "cannot open \(name)")
        }
        db = handle
        sqlite3_busy_timeout(db, 5000)   // a second connection (tests) may hold a write briefly
        guard sqlite3_exec(db, schema, nil, nil, nil) == SQLITE_OK else { throw error() }
    }

    deinit { sqlite3_close(db) }

    private func error() -> StoreError { StoreError(description: String(cString: sqlite3_errmsg(db))) }

    @discardableResult
    func query<T>(_ sql: String, _ args: [Bind] = [], _ each: (Row) -> T) throws -> [T] {
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK, let stmt else { throw error() }
        defer { sqlite3_finalize(stmt) }
        for (n, a) in args.enumerated() {
            let i = Int32(n + 1)
            switch a {
            case .text(let s): sqlite3_bind_text(stmt, i, s, -1, transient)
            case .int(let v): sqlite3_bind_int64(stmt, i, v)
            case .real(let v): sqlite3_bind_double(stmt, i, v)
            case .null: sqlite3_bind_null(stmt, i)
            case .blob(let d): sqlite3_bind_blob(stmt, i, (d as NSData).bytes, Int32(d.count), transient)
            }
        }
        var out: [T] = []
        while true {
            switch sqlite3_step(stmt) {
            case SQLITE_ROW: out.append(each(Row(stmt: stmt)))
            case SQLITE_DONE: return out
            default: throw error()
            }
        }
    }

    func exec(_ sql: String, _ args: [Bind] = []) throws {
        try query(sql, args) { _ in () }
    }

    /// Runs `body` in one transaction: all of it lands, or none.
    func transaction(_ body: () throws -> Void) throws {
        try exec("BEGIN")
        do {
            try body()
            try exec("COMMIT")
        } catch {
            try? exec("ROLLBACK")
            throw error
        }
    }
}

/// `<data_dir>/doll.db`: the memory index only (the timeline is in `Timeline`'s files).
public final class Store {
    private let sql: SQLite

    public init(path: URL) throws {
        sql = try SQLite(path: path, schema: memorySchema)
    }

    private func query<T>(_ s: String, _ args: [Bind] = [], _ each: (Row) -> T) throws -> [T] {
        try sql.query(s, args, each)
    }

    private func exec(_ s: String, _ args: [Bind]) throws { try sql.exec(s, args) }

    // memory index (M3a) ------------------------------------------------------
    public struct MemoryMeta {
        public let id: String
        public let path: String
        public let folder: String
        public let confidence: Double
        public let kind: String
    }

    public struct MemoryVectorRow {
        public let id: String
        public let vector: Data
    }

    /// Inserts or replaces one index entry (the markdown file was already written).
    public func putMemoryIndex(id: String, path: String, folder: String, confidence: Double,
                               ts: Double, sourceSeq: Int64, kind: String, text: String, vector: Data) throws {
        try exec("""
            INSERT INTO memory_index (id, path, folder, confidence, ts, source_seq, kind)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET path = excluded.path, folder = excluded.folder,
                confidence = excluded.confidence, ts = excluded.ts,
                source_seq = excluded.source_seq, kind = excluded.kind
            """, [.text(id), .text(path), .text(folder),
                  .real(confidence), .real(ts), .int(sourceSeq), .text(kind)])
        try exec("DELETE FROM memory_fts WHERE id = ?", [.text(id)])
        try exec("INSERT INTO memory_fts (text, id, folder) VALUES (?, ?, ?)",
                 [.text(text), .text(id), .text(folder)])
        try exec("INSERT INTO memory_vectors (id, vector) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET vector = excluded.vector",
                 [.text(id), .blob(vector)])
    }

    public func countMemoryIndex() throws -> Int {
        try query("SELECT count(*) FROM memory_index", []) { Int($0.int(0)!) }.first ?? 0
    }

    /// Removes one entry from every index table (its markdown file is already gone).
    public func deleteMemoryIndex(id: String) throws {
        for table in ["memory_index", "memory_fts", "memory_vectors"] {
            try exec("DELETE FROM \(table) WHERE id = ?", [.text(id)])
        }
    }

    public func clearMemoryIndex() throws {
        for sql in ["DELETE FROM memory_index", "DELETE FROM memory_fts", "DELETE FROM memory_vectors"] {
            try exec(sql, [])
        }
    }

    /// The `Memory.indexVersion` the index was built with; nil for an index from before versions.
    public func memoryIndexVersion() throws -> Int? {
        try query("SELECT version FROM memory_index_version WHERE one = 1", []) { Int($0.int(0)!) }.first
    }

    public func setMemoryIndexVersion(_ v: Int) throws {
        try exec("INSERT INTO memory_index_version (one, version) VALUES (1, ?) ON CONFLICT(one) DO UPDATE SET version = excluded.version",
                 [.int(Int64(v))])
    }

    /// FTS5 hits ordered by BM25 rank (lower is better), from the given folders only; the id/folder
    /// columns are UNINDEXED so the MATCH only sees text. The id tiebreak matters: equal BM25 scores
    /// otherwise order arbitrarily, and RRF ranks would change between a run and an index rebuild.
    public func memoryFTS(match: String, folders: [String], limit: Int) throws -> [(id: String, rank: Double)] {
        let marks = folders.map { _ in "?" }.joined(separator: ", ")
        return try query("""
            SELECT id, rank FROM memory_fts WHERE memory_fts MATCH ? AND folder IN (\(marks)) ORDER BY rank, id LIMIT ?
            """, [.text(match)] + folders.map { .text($0) } + [.int(Int64(limit))]) { ($0.text(0)!, $0.real(1)) }
    }

    public func memoryVectors(folders: [String]) throws -> [MemoryVectorRow] {
        let marks = folders.map { _ in "?" }.joined(separator: ", ")
        return try query("""
            SELECT m.id, m.vector FROM memory_vectors m JOIN memory_index m2 ON m2.id = m.id WHERE m2.folder IN (\(marks))
            """, folders.map { .text($0) }) {
            MemoryVectorRow(id: $0.text(0)!, vector: $0.blob(1))
        }
    }

    public func memoryMeta(_ id: String) throws -> MemoryMeta? {
        try query("SELECT id, path, folder, confidence, kind FROM memory_index WHERE id = ?",
                  [.text(id)]) {
            MemoryMeta(id: $0.text(0)!, path: $0.text(1)!, folder: $0.text(2)!,
                       confidence: $0.real(3), kind: $0.text(4)!)
        }.first
    }
}

extension ScenarioKind: Codable {}
