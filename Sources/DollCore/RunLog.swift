import Foundation
import Synchronization

/// The persistent log (2026-11-18, owner: after a boot she checks the log of the last run).
///
/// Every log line goes to stderr as before (`Log.write`) and, once `Log.attach` was called (her
/// process does at boot), to `<data_dir>/logs/dollcore.log` as one JSON object per line:
/// `{"level","msg","time","ts"}`, plus `run` on the lines that delimit runs:
/// - `{"event":"start","pid","build"}` — the first line a process writes;
/// - `{"event":"end","how":"clean|fatal|boot_failed","detail"}` — written by the process itself when it
///   ends through a signal it handles (clean) or through an error it reports (fatal / boot_failed);
/// - `{"event":"crash","detail":"SIGSEGV"}` — written from a fatal-signal handler (no time: the handler
///   may only call async-signal-safe functions).
/// A run with none of these after its start ended without a word: killed (SIGKILL, SIGHUP), a crash
/// the handlers did not catch, the Mac shut down or lost power. Which one cannot be told. A Mac that
/// sleeps does not end the process (it continues after wake), so sleep is never an ending.
///
/// Bound: when a write would pass `[log] max_mb`, `dollcore.log` becomes `dollcore.1.log` (the older
/// `dollcore.1.log` is deleted) and the new file starts with a line saying what was deleted.
/// A failed write is said on stderr; the next write that succeeds records how many lines missed the file.
public struct LogEntry: Codable, Sendable, Equatable {
    public var ts: Double?
    public var time: String?
    public var level: String
    public var msg: String
    public var run: RunMark?

    public init(ts: Double?, level: String, msg: String, run: RunMark? = nil) {
        self.ts = ts
        self.time = ts.map(LogFile.timeText)
        self.level = level
        self.msg = msg
        self.run = run
    }
}

public struct RunMark: Codable, Sendable, Equatable {
    public enum Event: String, Codable, Sendable { case start, end, crash }
    public var event: Event
    public var pid: Int32?
    public var build: String?
    public var how: RunEnding.How?
    public var detail: String?

    public init(event: Event, pid: Int32? = nil, build: String? = nil, how: RunEnding.How? = nil, detail: String? = nil) {
        self.event = event
        self.pid = pid
        self.build = build
        self.how = how
        self.detail = detail
    }
}

/// How a run ended, as far as its log says.
public struct RunEnding: Codable, Sendable, Equatable {
    public enum How: String, Codable, Sendable {
        /// SIGINT or SIGTERM: the process stopped its parts and wrote its end line.
        case clean
        /// An error ended the running process (`fail` in dollcore after boot).
        case fatal
        /// An error stopped the boot.
        case bootFailed = "boot_failed"
        /// A fatal signal (SIGSEGV, SIGBUS, SIGILL, SIGTRAP — a Swift runtime trap —, SIGABRT, SIGFPE).
        case crashed
        /// No end line: killed, an uncaught crash, shutdown or power loss — not told apart.
        case noEnd = "no_end"
    }
    public var how: How
    /// The end line's time; for `crashed` and `noEnd` the run's last timed line.
    public var ts: Double?
    public var detail: String?
}

/// One group of WARN or ERROR lines: same level, same message once numbers and ids are masked.
public struct LogKind: Codable, Sendable, Equatable {
    public var level: String
    public var key: String
    public var count: Int
    public var first: Double?
    public var last: Double?
    /// The first line of the group, verbatim.
    public var example: String
}

/// The run before this one, read from the log.
public struct LastRun: Codable, Sendable, Equatable {
    /// Its start line's time; nil when its start was rotated out of the files (`complete` false).
    public var started: Double?
    public var pid: Int32?
    public var build: String?
    public var complete: Bool
    public var ending: RunEnding
    public var lastLine: Double?
    public var lines: Int
    /// Lines that are not JSON (a line cut short by a crash).
    public var unreadable: Int
    /// The run's first timed line in the files (its start line when `complete`).
    public var firstLine: Double?
    /// Every WARN and ERROR kind: ERROR first, then by count, then by first time.
    public var kinds: [LogKind]

    public var errors: Int { kinds.filter { $0.level == "ERROR" }.reduce(0) { $0 + $1.count } }
    public var warnings: Int { kinds.filter { $0.level == "WARN" }.reduce(0) { $0 + $1.count } }
}

public final class LogFile: Sendable {
    public static let current = "dollcore.log"
    public static let previous = "dollcore.1.log"

    public let dir: URL
    let maxBytes: Int
    private struct State {
        var fd: Int32
        var size: Int
        var lost = 0
        var lostWhy = ""
    }
    private let state: Mutex<State>

    /// Opens (creates) `dir/dollcore.log` for appending. Not writable = an error (the boot fails).
    public init(dir: URL, maxBytes: Int) throws {
        self.dir = dir
        self.maxBytes = maxBytes
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let fd = try Self.openAppend(dir.appending(path: Self.current))
        var st = stat()
        fstat(fd, &st)
        state = Mutex(State(fd: fd, size: Int(st.st_size)))
    }

    deinit {
        state.withLock { close($0.fd) }
    }

    static func openAppend(_ url: URL) throws -> Int32 {
        let fd = open(url.path, O_WRONLY | O_APPEND | O_CREAT | O_CLOEXEC, 0o644)
        guard fd >= 0 else { throw StoreError(description: "log: cannot open \(url.path): \(String(cString: strerror(errno)))") }
        return fd
    }

    public static func timeText(_ ts: Double) -> String {
        let c = Calendar.current.dateComponents([.year, .month, .day, .hour, .minute, .second], from: Date(timeIntervalSince1970: ts))
        return String(format: "%04d-%02d-%02d %02d:%02d:%02d", c.year!, c.month!, c.day!, c.hour!, c.minute!, c.second!)
    }

    static func encode(_ e: LogEntry) -> Data {
        let enc = JSONEncoder()
        enc.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        var d = (try? enc.encode(e)) ?? Data(#"{"level":"ERROR","msg":"log: a line could not be encoded"}"#.utf8)
        d.append(10)
        return d
    }

    /// The file descriptor lines go to now (the crash handler writes there).
    var fd: Int32 { state.withLock { $0.fd } }

    public func write(_ e: LogEntry) {
        let line = Self.encode(e)
        state.withLock { s in
            if s.size > 0, s.size + line.count > maxBytes { rotate(&s, at: e.ts) }
            if s.lost > 0 {
                let note = Self.encode(LogEntry(ts: e.ts, level: "ERROR",
                                                msg: "log: \(s.lost) line(s) before this one are not in this file (write failed: \(s.lostWhy)); they went to stderr only"))
                if Self.put(s.fd, note) {
                    s.size += note.count
                    s.lost = 0
                }
            }
            if s.lost == 0, Self.put(s.fd, line) {
                s.size += line.count
            } else {
                if s.lost == 0 {
                    s.lostWhy = String(cString: strerror(errno))
                    FileHandle.standardError.write(Data("log: writing \(dir.appending(path: Self.current).path) failed (\(s.lostWhy)); lines go to stderr only until it works again\n".utf8))
                }
                s.lost += 1
            }
        }
    }

    private static func put(_ fd: Int32, _ data: Data) -> Bool {
        data.withUnsafeBytes { buf in
            var off = 0
            while off < buf.count {
                let n = Foundation.write(fd, buf.baseAddress! + off, buf.count - off)
                if n <= 0 { return false }
                off += n
            }
            return true
        }
    }

    /// `dollcore.log` → `dollcore.1.log` (the older one deleted), a new `dollcore.log` that says so.
    private func rotate(_ s: inout State, at ts: Double?) {
        let cur = dir.appending(path: Self.current), old = dir.appending(path: Self.previous)
        let fm = FileManager.default
        let dropped = (try? fm.attributesOfItem(atPath: old.path)[.size] as? Int) ?? nil
        do {
            if dropped != nil { try fm.removeItem(at: old) }
            try fm.moveItem(at: cur, to: old)
            let fd = try Self.openAppend(cur)
            let was = s.fd
            s.fd = fd
            s.size = 0
            close(was)
            CrashMarks.moved(from: was, to: fd)
        } catch {
            FileHandle.standardError.write(Data("log: rotating \(cur.path) failed: \(error); it keeps growing\n".utf8))
            return
        }
        let note = Self.encode(LogEntry(ts: ts, level: "INFO",
                                        msg: "log: rotated ([log] max_mb reached): the file before this one is \(Self.previous)"
                                            + (dropped.map { "; the older \(Self.previous) (\($0) bytes) was deleted" } ?? "")))
        if Self.put(s.fd, note) { s.size += note.count }
    }
}

/// The fatal-signal handlers' end lines (async-signal-safe: preformatted bytes and `write`).
enum CrashMarks {
    nonisolated(unsafe) static var fd: Int32 = -1
    nonisolated(unsafe) static var lines = UnsafeMutablePointer<UnsafeMutablePointer<CChar>?>.allocate(capacity: 64)
    static let signals: [(Int32, String)] = [(SIGSEGV, "SIGSEGV"), (SIGBUS, "SIGBUS"), (SIGILL, "SIGILL"), (SIGTRAP, "SIGTRAP"),
                                             (SIGABRT, "SIGABRT"), (SIGFPE, "SIGFPE")]

    static func moved(from: Int32, to: Int32) {
        if fd == from { fd = to }
    }

    static func install(fd: Int32) {
        Self.fd = fd
        lines.initialize(repeating: nil, count: 64)
        for (sig, name) in signals {
            let e = LogEntry(ts: nil, level: "RUN", msg: "crashed: \(name)", run: RunMark(event: .crash, how: .crashed, detail: name))
            let s = String(decoding: LogFile.encode(e), as: UTF8.self)
            lines[Int(sig)] = strdup(s)
            var act = sigaction()
            act.__sigaction_u.__sa_handler = { sig in
                if CrashMarks.fd >= 0, let p = CrashMarks.lines[Int(sig)] { _ = Foundation.write(CrashMarks.fd, p, strlen(p)) }
                signal(sig, SIG_DFL)
                raise(sig)
            }
            sigemptyset(&act.sa_mask)
            act.sa_flags = 0
            sigaction(sig, &act, nil)
        }
    }
}

extension Log {
    static let sink = Mutex<LogFile?>(nil)

    /// From now on every log line also goes to `file` (her process, at boot). Writes the run's start line
    /// and installs the fatal-signal handlers.
    public static func attach(_ file: LogFile, pid: Int32, build: String) {
        sink.withLock { $0 = file }
        file.write(LogEntry(ts: Date().timeIntervalSince1970, level: "RUN", msg: "started: pid \(pid), build \(build)",
                            run: RunMark(event: .start, pid: pid, build: build)))
        CrashMarks.install(fd: file.fd)
    }

    /// A line for the file only (stderr already has it in its own form: the bridge's output).
    public static func fileOnly(_ level: String, _ msg: String) {
        sink.withLock { $0 }?.write(LogEntry(ts: Date().timeIntervalSince1970, level: level, msg: msg))
    }

    /// The run's end line (no-op before `attach`). `fatal` and `boot_failed` first log the error as an ERROR line.
    public static func end(_ how: RunEnding.How, _ detail: String) {
        guard let f = sink.withLock({ $0 }) else { return }
        let now = Date().timeIntervalSince1970
        if how != .clean { f.write(LogEntry(ts: now, level: "ERROR", msg: (how == .bootFailed ? "boot failed: " : "fatal: ") + detail)) }
        f.write(LogEntry(ts: now, level: "RUN", msg: "ended (\(how.rawValue)): \(detail)", run: RunMark(event: .end, how: how, detail: detail)))
    }
}

public enum RunLog {
    /// The run before this one in `dir` (read before this process writes its start line); nil when the
    /// folder has no log (an older data dir) or the log has no line.
    public static func lastRun(dir: URL) throws -> LastRun? {
        var entries: [LogEntry] = []
        var unreadable = 0
        var any = false
        for name in [LogFile.previous, LogFile.current] {
            let url = dir.appending(path: name)
            guard FileManager.default.fileExists(atPath: url.path) else { continue }
            any = true
            let data: Data
            do { data = try Data(contentsOf: url) } catch { throw StoreError(description: "log: cannot read \(url.path): \(error)") }
            let dec = JSONDecoder()
            for line in data.split(separator: 10, omittingEmptySubsequences: true) {
                if let e = try? dec.decode(LogEntry.self, from: line) { entries.append(e) } else { unreadable += 1 }
            }
        }
        guard any, !entries.isEmpty || unreadable > 0 else { return nil }
        let startAt = entries.lastIndex { $0.run?.event == .start }
        let run = startAt.map { Array(entries[$0...]) } ?? entries
        let start = startAt.map { entries[$0] }
        let lastTimed = run.last { $0.ts != nil }?.ts
        var ending = RunEnding(how: .noEnd, ts: lastTimed, detail: nil)
        if let m = run.last(where: { $0.run?.event == .end || $0.run?.event == .crash }), let mark = m.run {
            ending = mark.event == .crash
                ? RunEnding(how: .crashed, ts: lastTimed, detail: mark.detail)
                : RunEnding(how: mark.how ?? .clean, ts: m.ts, detail: mark.detail)
        }
        return LastRun(started: start?.ts, pid: start?.run?.pid, build: start?.run?.build, complete: start != nil, ending: ending,
                       lastLine: lastTimed, lines: run.count, unreadable: unreadable, firstLine: run.first { $0.ts != nil }?.ts, kinds: kinds(run))
    }

    /// WARN and ERROR lines grouped by `kindKey`.
    static func kinds(_ run: [LogEntry]) -> [LogKind] {
        var order: [String] = []
        var groups: [String: LogKind] = [:]
        for e in run where e.level == "ERROR" || e.level == "WARN" {
            let key = e.level + " " + kindKey(e.msg)
            if var g = groups[key] {
                g.count += 1
                if let t = e.ts { g.first = g.first ?? t; g.last = t }
                groups[key] = g
            } else {
                order.append(key)
                groups[key] = LogKind(level: e.level, key: kindKey(e.msg), count: 1, first: e.ts, last: e.ts, example: e.msg)
            }
        }
        return order.enumerated().map { (i: $0.offset, k: groups[$0.element]!) }.sorted {
            ($0.k.level == "ERROR" ? 0 : 1, -$0.k.count, $0.i) < ($1.k.level == "ERROR" ? 0 : 1, -$1.k.count, $1.i)
        }.map(\.k)
    }

    /// A message with what changes from line to line masked: hex ids (8+ hex digits with a digit
    /// among them) and numbers become `#`; runs of spaces become one.
    public static func kindKey(_ msg: String) -> String {
        msg.replacing(/(?=[0-9A-Fa-f]*[0-9])[0-9A-Fa-f]{8,}/, with: "#")
            .replacing(/[0-9]+(\.[0-9]+)?/, with: "#")
            .replacing(/\s+/, with: " ")
    }
}
