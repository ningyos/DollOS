import Foundation
import SQLite3

/// One SQLite file: the append-only timeline (events), the 【狀況】 footer each reply
/// answered, and the jobs the program needs to kill after a restart.
///
/// This is not a WAL. Nothing is replayed after a crash; the queue lives in memory.
private let schema = """
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,              -- owner | doll | result | job
    source TEXT NOT NULL,            -- owner | internal
    has_secret INTEGER NOT NULL DEFAULT 0,
    voice INTEGER NOT NULL DEFAULT 0,  -- owner events: spoken (owner_voice) or typed (owner_text)
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS footers (
    seq INTEGER PRIMARY KEY,         -- her reply's seq
    text TEXT NOT NULL               -- the 【狀況】 she saw right before it; a prompt artifact, not an event
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    command TEXT NOT NULL,
    status TEXT NOT NULL,            -- running | ok | failed | timeout | cancelled | lost
    pgid INTEGER,
    started REAL NOT NULL,
    ended REAL
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
        case doll       // her reply, verbatim
        case result     // a short command's output (or "turned into a background job")
        case job        // a background job finished
    }
    public var kind: Kind
    public var principal: Principal
    public var text: String
    public var secret: Bool
    /// Only owner voice may cancel a model call in flight (voice itself is M6).
    public var voice: Bool
    public var seq: Int64?
    public var ts: Double
    /// For her replies: the footer that preceded it in the prompt. Kept in the prompt so the
    /// server's prefix cache stays valid; never part of the timeline proper.
    public var footer: String?

    public init(_ kind: Kind, _ principal: Principal, _ text: String, secret: Bool = false, voice: Bool = false) {
        self.kind = kind
        self.principal = principal
        self.text = text
        self.secret = secret
        self.voice = voice
        self.seq = nil
        self.ts = Date().timeIntervalSince1970
    }

    public var priority: Priority {
        switch kind {
        case .owner: voice ? .ownerVoice : .ownerText
        case .result, .job: .jobResult
        case .doll: .jobResult   // never queued; her replies go straight to the timeline
        }
    }
}

public struct Job: Sendable, Equatable {
    public let id: String
    public let command: String
    public let status: String
    public let pgid: Int32?
    public let started: Double
}

private enum Bind {
    case text(String), int(Int64), real(Double), null
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
        guard sqlite3_exec(db, schema, nil, nil, nil) == SQLITE_OK else { throw error() }
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

    private var now: Bind { .real(Date().timeIntervalSince1970) }

    // events -----------------------------------------------------------------
    private static let eventColumns =
        "SELECT e.seq, e.ts, e.kind, e.source, e.has_secret, e.text, f.text, e.voice"
        + " FROM events e LEFT JOIN footers f ON f.seq = e.seq"

    private static func event(_ r: Row) -> Event {
        var e = Event(Event.Kind(rawValue: r.text(2)!)!, Principal(rawValue: r.text(3)!)!, r.text(5)!,
                      secret: r.int(4)! != 0, voice: r.int(7)! != 0)
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
        try exec("INSERT INTO events (ts, kind, source, has_secret, voice, text) VALUES (?, ?, ?, ?, ?, ?)",
                 [.real(ev.ts), .text(ev.kind.rawValue), .text(ev.principal.rawValue),
                  .int(ev.secret ? 1 : 0), .int(ev.voice ? 1 : 0), .text(ev.text)])
        var out = ev
        out.seq = sqlite3_last_insert_rowid(db)
        return out
    }

    /// The whole timeline, oldest first. Only the owner exists in M1, so there is one projection.
    public func events() throws -> [Event] {
        try query(Self.eventColumns + " ORDER BY e.seq", [], Self.event)
    }

    // jobs -------------------------------------------------------------------
    public func addJob(_ id: String, command: String, pgid: Int32?) throws {
        try exec("INSERT INTO jobs (id, command, status, pgid, started) VALUES (?, ?, 'running', ?, ?)",
                 [.text(id), .text(command), pgid.map { Bind.int(Int64($0)) } ?? .null, now])
    }

    private static let jobColumns = "SELECT id, command, status, pgid, started FROM jobs"

    private static func job(_ r: Row) -> Job {
        Job(id: r.text(0)!, command: r.text(1)!, status: r.text(2)!, pgid: r.int(3).map { Int32($0) }, started: r.real(4))
    }

    public func job(_ id: String) throws -> Job? {
        try query(Self.jobColumns + " WHERE id = ?", [.text(id)], Self.job).first
    }

    public func runningJobs() throws -> [Job] {
        try query(Self.jobColumns + " WHERE status = 'running' ORDER BY started", [], Self.job)
    }

    public func finishJob(_ id: String, _ status: String) throws {
        try exec("UPDATE jobs SET status = ?, ended = ? WHERE id = ?", [.text(status), now, .text(id)])
    }
}
