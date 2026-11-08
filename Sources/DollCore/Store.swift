import Foundation
import SQLite3

/// One SQLite file: the append-only timeline (events) and the 【狀況】 footer each reply answered.
///
/// This is not a WAL. Nothing is replayed after a crash; the queue lives in memory.
private let schema = """
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,              -- owner | doll | tool | watch
    source TEXT NOT NULL,            -- owner | internal
    has_secret INTEGER NOT NULL DEFAULT 0,
    voice INTEGER NOT NULL DEFAULT 0,  -- owner events: spoken (owner_voice) or typed (owner_text)
    text TEXT NOT NULL,              -- her replies: raw, tool call text included, exactly as re-sent
    image TEXT,                      -- the full-screen screenshot file under shots/, re-sent byte-identical
    crop_note TEXT,                  -- the line before the front window's crop: where it sits, or why there is none
    crop TEXT,                       -- the front window's crop file under shots/, re-sent byte-identical
    window TEXT                      -- the front window's outline sent after them, verbatim
);
CREATE TABLE IF NOT EXISTS footers (
    seq INTEGER PRIMARY KEY,         -- her reply's seq
    text TEXT NOT NULL               -- the 【狀況】 she saw right before it; a prompt artifact, not an event
);
"""

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
"""

public struct StoreError: Error, CustomStringConvertible {
    public let description: String
}

public enum Principal: String, Sendable {
    case owner, `internal`
}

public struct Event: Sendable, Equatable {
    public enum Kind: String, Sendable {
        case owner      // the owner spoke
        case doll       // her reply, verbatim (tool call text included)
        case tool       // the result of one of her tool calls, in call order
        case watch      // her watch woke her: the region changed, or max_wait passed
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
    /// The front window's outline (or why there is none) taken with those screenshots; rendered
    /// verbatim as the last text part.
    public var window: String?
    public var seq: Int64?
    public var ts: Double
    /// For her replies: the footer that preceded it in the prompt. Kept in the prompt so the
    /// server's prefix cache stays valid; never part of the timeline proper.
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
        case .doll, .tool: .watch   // never queued; her replies and tool results go straight to the timeline
        }
    }
}

private enum Bind {
    case text(String), int(Int64), real(Double), null, blob(Data)
}

private let transient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)

private struct Row {
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

public final class Store {
    private let db: OpaquePointer

    public init(path: URL) throws {
        try FileManager.default.createDirectory(at: path.deletingLastPathComponent(), withIntermediateDirectories: true)
        var handle: OpaquePointer?
        guard sqlite3_open(path.path, &handle) == SQLITE_OK, let handle else {
            throw StoreError(description: "cannot open \(path.path)")
        }
        db = handle
        sqlite3_busy_timeout(db, 5000)   // a second connection (dollctl, tests) may hold a write briefly
        guard sqlite3_exec(db, schema, nil, nil, nil) == SQLITE_OK,
              sqlite3_exec(db, memorySchema, nil, nil, nil) == SQLITE_OK else { throw error() }
    }

    deinit { sqlite3_close(db) }

    private func error() -> StoreError { StoreError(description: String(cString: sqlite3_errmsg(db))) }

    @discardableResult
    private func query<T>(_ sql: String, _ args: [Bind] = [], _ each: (Row) -> T) throws -> [T] {
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

    private func exec(_ sql: String, _ args: [Bind]) throws {
        try query(sql, args) { _ in () }
    }

    // events -----------------------------------------------------------------
    private static let eventColumns =
        "SELECT e.seq, e.ts, e.kind, e.source, e.has_secret, e.text, f.text, e.voice, e.image, e.window, e.crop_note, e.crop"
        + " FROM events e LEFT JOIN footers f ON f.seq = e.seq"

    private static func event(_ r: Row) -> Event {
        var e = Event(Event.Kind(rawValue: r.text(2)!)!, Principal(rawValue: r.text(3)!)!, r.text(5)!,
                      secret: r.int(4)! != 0, voice: r.int(7)! != 0, image: r.text(8), cropNote: r.text(10),
                      crop: r.text(11), window: r.text(9))
        e.seq = r.int(0)
        e.ts = r.real(1)
        e.footer = r.text(6)
        return e
    }

    public func attachFooter(_ seq: Int64, _ text: String) throws {
        try exec("INSERT INTO footers (seq, text) VALUES (?, ?)", [.int(seq), .text(text)])
    }

    public func footer(_ seq: Int64) throws -> String? {
        try query("SELECT text FROM footers WHERE seq = ?", [.int(seq)]) { $0.text(0)! }.first
    }

    /// Appends one event and returns it with its seq.
    @discardableResult
    public func append(_ ev: Event) throws -> Event {
        try exec("INSERT INTO events (ts, kind, source, has_secret, voice, text, image, crop_note, crop, window)"
                 + " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 [.real(ev.ts), .text(ev.kind.rawValue), .text(ev.principal.rawValue),
                  .int(ev.secret ? 1 : 0), .int(ev.voice ? 1 : 0), .text(ev.text), ev.image.map { .text($0) } ?? .null,
                  ev.cropNote.map { .text($0) } ?? .null, ev.crop.map { .text($0) } ?? .null,
                  ev.window.map { .text($0) } ?? .null])
        var out = ev
        out.seq = sqlite3_last_insert_rowid(db)
        return out
    }

    /// The whole timeline, oldest first. Only the owner exists so far, so there is one projection.
    public func events() throws -> [Event] {
        try query(Self.eventColumns + " ORDER BY e.seq", [], Self.event)
    }

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

    public func clearMemoryIndex() throws {
        for sql in ["DELETE FROM memory_index", "DELETE FROM memory_fts", "DELETE FROM memory_vectors"] {
            try exec(sql, [])
        }
    }

    /// FTS5 hits ordered by BM25 rank, from the given folders only; the id/folder columns are
    /// UNINDEXED so the MATCH only sees text. The id tiebreak matters: equal BM25 scores otherwise
    /// order arbitrarily, and RRF ranks would change between a run and an index rebuild.
    public func memoryFTS(match: String, folders: [String], limit: Int) throws -> [String] {
        let marks = folders.map { _ in "?" }.joined(separator: ", ")
        return try query("""
            SELECT id FROM memory_fts WHERE memory_fts MATCH ? AND folder IN (\(marks)) ORDER BY rank, id LIMIT ?
            """, [.text(match)] + folders.map { .text($0) } + [.int(Int64(limit))]) { $0.text(0)! }
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
