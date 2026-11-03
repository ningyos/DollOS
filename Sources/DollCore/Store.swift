import Foundation
import SQLite3

/// One SQLite file: the timeline, work lines, jobs, memory notes, pinned corrections.
///
/// This is not a WAL. Nothing is replayed after a crash; the queue lives in memory.
private let schema = """
CREATE TABLE IF NOT EXISTS timeline (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    audience TEXT NOT NULL,          -- 'private' (owner + Doll) or 'dm:<who>'
    line_id TEXT,
    message TEXT NOT NULL            -- one chat-completions message as JSON
);
CREATE TABLE IF NOT EXISTS lines (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    opened_by TEXT NOT NULL,
    status TEXT NOT NULL,            -- active | paused | done
    next_step TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    last_touched REAL NOT NULL,
    steps_day TEXT NOT NULL DEFAULT '',
    steps_today INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    line_id TEXT,
    tool TEXT NOT NULL,
    args TEXT NOT NULL,
    status TEXT NOT NULL,            -- pending | running | ok | failed | timeout | cancelled | denied | lost
    pgid INTEGER,
    started REAL NOT NULL,
    ended REAL
);
CREATE TABLE IF NOT EXISTS pins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    text TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS notes USING fts5(text, ts UNINDEXED, tokenize='trigram');
"""

public struct StoreError: Error, CustomStringConvertible {
    public let description: String
}

public struct Line: Sendable {
    public let id: String
    public let title: String
    public let openedBy: String
    public let status: String
    public let nextStep: String
    public let summary: String
    public let lastTouched: Double
    public let stepsDay: String
    public let stepsToday: Int
}

public struct Job: Sendable {
    public let id: String
    public let lineID: String?
    public let tool: String
    public let args: String          // the tool arguments as JSON
    public let status: String
    public let pgid: Int32?
    public let started: Double
}

private enum Bind {
    case text(String), int(Int64), real(Double), null
}

private func bind(_ s: String?) -> Bind { s.map(Bind.text) ?? .null }

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

    // timeline ---------------------------------------------------------------
    public func append(_ audience: String, _ message: ChatMessage, lineID: String? = nil) throws {
        let json = String(decoding: try ChatMessage.encoder.encode(message), as: UTF8.self)
        try exec("INSERT INTO timeline (ts, audience, line_id, message) VALUES (?, ?, ?, ?)",
                 [now, .text(audience), bind(lineID), .text(json)])
    }

    /// The visible slice of the one timeline, oldest first.
    public func project(_ audiences: [String], window: Int) throws -> [ChatMessage] {
        let marks = Array(repeating: "?", count: audiences.count).joined(separator: ",")
        let rows = try query(
            "SELECT message FROM (SELECT seq, message FROM timeline WHERE audience IN (\(marks))"
                + " ORDER BY seq DESC LIMIT ?) ORDER BY seq",
            audiences.map(Bind.text) + [.int(Int64(window))]) { $0.text(0)! }
        var msgs = try rows.map { try JSONDecoder().decode(ChatMessage.self, from: Data($0.utf8)) }
        // A window cut must not start inside a tool exchange.
        while msgs.first?.role == "tool" { msgs.removeFirst() }
        return msgs
    }

    public func dmAudiences() throws -> [String] {
        try query("SELECT DISTINCT audience FROM timeline WHERE audience LIKE 'dm:%'") { $0.text(0)! }
    }

    // lines ------------------------------------------------------------------
    public func openLine(_ id: String, title: String, openedBy: String, nextStep: String) throws {
        try exec("INSERT INTO lines (id, title, opened_by, status, next_step, last_touched)"
                    + " VALUES (?, ?, ?, 'active', ?, ?)",
                 [.text(id), .text(title), .text(openedBy), .text(nextStep), now])
    }

    private static let lineColumns =
        "SELECT id, title, opened_by, status, next_step, summary, last_touched, steps_day, steps_today FROM lines"

    private static func line(_ r: Row) -> Line {
        Line(id: r.text(0)!, title: r.text(1)!, openedBy: r.text(2)!, status: r.text(3)!,
             nextStep: r.text(4)!, summary: r.text(5)!, lastTouched: r.real(6),
             stepsDay: r.text(7)!, stepsToday: Int(r.int(8)!))
    }

    public func lines(status: String? = nil) throws -> [Line] {
        if let status {
            return try query(Self.lineColumns + " WHERE status = ? ORDER BY last_touched", [.text(status)], Self.line)
        }
        return try query(Self.lineColumns + " ORDER BY last_touched", [], Self.line)
    }

    public func line(_ id: String) throws -> Line? {
        try query(Self.lineColumns + " WHERE id = ?", [.text(id)], Self.line).first
    }

    public func updateLine(_ id: String, status: String, nextStep: String, summary: String) throws {
        try exec("UPDATE lines SET status = ?, next_step = ?, summary = ?, last_touched = ? WHERE id = ?",
                 [.text(status), .text(nextStep), .text(summary), now, .text(id)])
    }

    public func countStep(_ id: String, today: String) throws {
        try exec("UPDATE lines SET steps_today = CASE WHEN steps_day = ? THEN steps_today + 1 ELSE 1 END,"
                    + " steps_day = ?, last_touched = ? WHERE id = ?",
                 [.text(today), .text(today), now, .text(id)])
    }

    // jobs -------------------------------------------------------------------
    public func addJob(_ id: String, lineID: String?, tool: String, args: String, status: String = "running") throws {
        try exec("INSERT INTO jobs (id, line_id, tool, args, status, started) VALUES (?, ?, ?, ?, ?, ?)",
                 [.text(id), bind(lineID), .text(tool), .text(args), .text(status), now])
    }

    private static let jobColumns = "SELECT id, line_id, tool, args, status, pgid, started FROM jobs"

    private static func job(_ r: Row) -> Job {
        Job(id: r.text(0)!, lineID: r.text(1), tool: r.text(2)!, args: r.text(3)!, status: r.text(4)!,
            pgid: r.int(5).map { Int32($0) }, started: r.real(6))
    }

    public func job(_ id: String) throws -> Job? {
        try query(Self.jobColumns + " WHERE id = ?", [.text(id)], Self.job).first
    }

    public func setJobStatus(_ id: String, _ status: String) throws {
        try exec("UPDATE jobs SET status = ? WHERE id = ?", [.text(status), .text(id)])
    }

    /// Jobs not finished yet: waiting for approval or running.
    public func openJobs() throws -> [Job] {
        try query(Self.jobColumns + " WHERE status IN ('pending', 'running') ORDER BY started", [], Self.job)
    }

    public func setJobPgid(_ id: String, _ pgid: Int32) throws {
        try exec("UPDATE jobs SET pgid = ? WHERE id = ?", [.int(Int64(pgid)), .text(id)])
    }

    public func finishJob(_ id: String, _ status: String) throws {
        try exec("UPDATE jobs SET status = ?, ended = ? WHERE id = ?", [.text(status), now, .text(id)])
    }

    // memory -----------------------------------------------------------------
    public func note(_ text: String) throws {
        try exec("INSERT INTO notes (text, ts) VALUES (?, ?)", [.text(text), now])
    }

    public func recall(_ query: String, limit: Int) throws -> [String] {
        let words = query.split(whereSeparator: \.isWhitespace).map(String.init)
        let terms = words.filter { $0.unicodeScalars.count >= 3 }
        let expr = (terms.isEmpty ? [query] : terms)
            .map { "\"" + $0.replacingOccurrences(of: "\"", with: "\"\"") + "\"" }
            .joined(separator: " OR ")
        do {
            return try self.query("SELECT text FROM notes WHERE notes MATCH ? ORDER BY rank LIMIT ?",
                                  [.text(expr), .int(Int64(limit))]) { $0.text(0)! }
        } catch let e as StoreError {
            throw ToolError("bad recall query: \(e.description)")
        }
    }

    public func pin(_ text: String) throws {
        try exec("INSERT INTO pins (ts, text) VALUES (?, ?)", [now, .text(text)])
    }

    public func pins() throws -> [String] {
        try query("SELECT text FROM pins ORDER BY id") { $0.text(0)! }
    }
}
