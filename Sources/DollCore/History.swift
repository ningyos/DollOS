import CryptoKit
import Foundation

/// The history of what is hers (2026-11-19, layer 4: version control and rollback only; owner
/// 2026-11-04 「skill、人設筆記、糾正紀錄放進 git：留紀錄、看 diff、可還原」).
///
/// What is kept (`History.versioned`): in `[core] data_dir`, her ideal self (`ideal-self.md`) and its
/// revision log, her goal tree (`goals.json`), the record of what she did with the owner's evaluations
/// (`record.json`), the corrections of her memories and the audit's flags (`memory-fixes.json`) and
/// her saved tools (`tools/*.json`); in `[memory] dir`, every memory file (secret, own, public) and the
/// entity registry. Not kept: what is its own history (timeline, logs, boot reports, the computer's
/// action log, screenshots, voice audio, signals), and program state (calendar, concerns, estimates,
/// curiosity, maintenance state, the SQLite index, which is rebuilt from the files).
///
/// One repository for both dirs, though they are configured apart: a bare repository at
/// `[history] dir` (under the data dir) whose commits hold two trees, `data/…` and `memory/…`. It has
/// no work tree: the program hashes the files and builds each commit itself (`hash-object`,
/// `update-index` on its own index file, `write-tree`, `commit-tree`, `update-ref`), so one change that
/// touches both dirs (a memory corrected: the new file, the retired one, the correction record) is one
/// commit, and nothing around the dirs (the project's own repository, when a data dir sits in it) is
/// ever looked at. Every git call names the repository explicitly and runs without the owner's global
/// or system git config and with every inherited `GIT_*` variable dropped.
///
/// A change is committed when it is made, by the program, with who made it, what it was and why
/// (`HistoryChange`). Going back never rewrites history: undoing one change applies its inverse as a
/// new commit; returning to a point writes that point's files as a new commit.
public struct HistoryError: Error, CustomStringConvertible, Sendable {
    public let description: String
}

/// Who made a change.
public enum HistoryWho: String, Codable, Sendable {
    case her, owner, maintenance, program
    /// How she reads it (`你` is her).
    var words: String {
        switch self {
        case .her: "你"
        case .owner: "主人"
        case .maintenance: "系統維護"
        case .program: "程式"
        }
    }
    /// How the owner reads it.
    var ownerWords: String {
        switch self {
        case .her: "她"
        case .owner: "主人"
        case .maintenance: "系統維護"
        case .program: "程式"
        }
    }
}

/// One change as it is committed.
public struct HistoryChange: Sendable {
    public var who: HistoryWho
    /// The maintenance task (`audit`, `review`, `consolidate`, `concerns`).
    public var task: String?
    /// The tool or action.
    public var what: String
    /// The reason the feature already collected (`why`, `basis`, the audit's quote, the owner's edit).
    public var why: String
    /// Her call's arguments, short.
    public var args: String?
    /// Feature records this change made (`ideal#3`, `fix#2`): what the feature buttons undo.
    public var refs: [String] = []
    public var undoes: String?
    public var restores: String?
    public var ts: Double

    public init(who: HistoryWho, task: String? = nil, what: String, why: String, args: String? = nil, refs: [String] = [],
                undoes: String? = nil, restores: String? = nil, ts: Double) {
        self.who = who
        self.task = task
        self.what = what
        self.why = why
        self.args = args
        self.refs = refs
        self.undoes = undoes
        self.restores = restores
        self.ts = ts
    }
}

/// One commit as the history lists it.
public struct HistoryEntry: Codable, Sendable, Equatable {
    public var id: String
    public var ts: Double
    public var who: HistoryWho
    public var task: String?
    public var what: String
    public var why: String
    public var args: String?
    public var refs: [String]
    public var undoes: String?
    public var restores: String?
    /// Changes before this one whose commit failed; their files went into this commit.
    public var unrecordedBefore: [String]
    /// The files it changed (`data/goals.json`, `memory/own/12-ab.md`).
    public var paths: [String]
    public var short: String { String(id.prefix(7)) }
}

/// The program's git: one executable, one bare repository, a clean environment.
struct Git: Sendable {
    let exe: String
    let gitDir: URL

    /// `name` on `PATH` (or a path), checked by running `git --version`.
    static func locate(_ name: String) throws -> String {
        located.lock()
        defer { located.unlock() }
        if let exe = locatedExe[name] { return exe }
        let exe = try find(name)
        locatedExe[name] = exe
        return exe
    }
    private static let located = NSLock()
    nonisolated(unsafe) private static var locatedExe: [String: String] = [:]

    /// The executable, checked once per process. What is returned is the git that `name` runs
    /// (`<exec-path>/git`): on macOS `/usr/bin/git` is a stub that looks up the developer tools on
    /// every call and costs as much again as git itself (2026-11-20: 12 ms against 6 ms a call).
    private static func find(_ name: String) throws -> String {
        let fm = FileManager.default
        var found: String?
        if name.contains("/") {
            found = fm.isExecutableFile(atPath: name) ? name : nil
        } else {
            for dir in (ProcessInfo.processInfo.environment["PATH"] ?? "").split(separator: ":") {
                let p = "\(dir)/\(name)"
                if fm.isExecutableFile(atPath: p) { found = p; break }
            }
        }
        let fix = "install git (on macOS: `xcode-select --install`) or set [history] git to its path"
        guard let exe = found else { throw HistoryError(description: "[history] git: \(name) not found on PATH; \(fix)") }
        let probe = Git(exe: exe, gitDir: URL(filePath: "/"))
        let r = try probe.raw(["--version"], cwd: URL(filePath: NSTemporaryDirectory()))
        guard r.code == 0, String(decoding: r.out, as: UTF8.self).hasPrefix("git version") else {
            throw HistoryError(description: "[history] git: \(exe) --version failed (\(String(decoding: r.err, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines))); \(fix)")
        }
        let e = try probe.raw(["--exec-path"], cwd: URL(filePath: NSTemporaryDirectory()))
        let own = String(decoding: e.out, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines) + "/git"
        return e.code == 0 && own.hasPrefix("/") && fm.isExecutableFile(atPath: own) ? own : exe
    }

    /// The environment every call runs with: no inherited `GIT_*` (a `GIT_DIR` or `GIT_WORK_TREE`
    /// from the shell would point elsewhere), no system or global config (the owner's own settings
    /// are neither read nor touched), never a prompt.
    static func environment(_ extra: [String: String]) -> [String: String] {
        var env = ProcessInfo.processInfo.environment.filter { !$0.key.hasPrefix("GIT_") }
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_CONFIG_GLOBAL"] = "/dev/null"
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["LC_ALL"] = "C"
        for (k, v) in extra { env[k] = v }
        return env
    }

    /// One git process. Its input and output go through files in `scratch`, not pipes: reading pipes
    /// needs threads of their own, and with every cooperative thread blocked in a call like this one
    /// (tests run many at once) the readers never ran. `scratch` is the repository (only the owner's
    /// user reads it: a blob's output may be a secret memory) or, before there is one, the temp dir.
    func raw(_ args: [String], input: Data? = nil, env: [String: String] = [:], cwd: URL, scratch: URL? = nil) throws -> (out: Data, err: Data, code: Int32) {
        let fm = FileManager.default
        let base = (scratch ?? cwd).appending(path: "io-\(UUID().uuidString)")
        let inURL = base.appendingPathExtension("in"), outURL = base.appendingPathExtension("out"), errURL = base.appendingPathExtension("err")
        defer { for u in [inURL, outURL, errURL] { try? fm.removeItem(at: u) } }
        for u in [outURL, errURL] {
            guard fm.createFile(atPath: u.path, contents: nil, attributes: [.posixPermissions: 0o600]) else {
                throw HistoryError(description: "cannot create \(u.path)")
            }
        }
        let p = Process()
        p.executableURL = URL(filePath: exe)
        p.arguments = args
        p.environment = Self.environment(env)
        p.currentDirectoryURL = cwd
        let out = try FileHandle(forWritingTo: outURL), err = try FileHandle(forWritingTo: errURL)
        defer { try? out.close(); try? err.close() }
        p.standardOutput = out
        p.standardError = err
        if let input {
            guard fm.createFile(atPath: inURL.path, contents: input, attributes: [.posixPermissions: 0o600]) else {
                throw HistoryError(description: "cannot create \(inURL.path)")
            }
            let h = try FileHandle(forReadingFrom: inURL)
            defer { try? h.close() }
            p.standardInput = h
            try p.run()
            p.waitUntilExit()
        } else {
            p.standardInput = FileHandle.nullDevice
            try p.run()
            p.waitUntilExit()
        }
        return (try Data(contentsOf: outURL), try Data(contentsOf: errURL), p.terminationStatus)
    }

    /// One call on the repository (`--git-dir` explicit, run inside it). `ok`: exit codes that are
    /// answers, not failures.
    @discardableResult
    func run(_ args: [String], input: Data? = nil, env: [String: String] = [:], ok: Set<Int32> = [0]) throws -> (out: Data, code: Int32) {
        let r = try raw(["--git-dir=\(gitDir.path)"] + args, input: input, env: env.merging(["GIT_DIR": gitDir.path]) { $1 }, cwd: gitDir)
        guard ok.contains(r.code) else {
            throw HistoryError(description: "git \(args.prefix(3).joined(separator: " ")) failed (\(r.code)): \(String(decoding: r.err, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines))")
        }
        return (r.out, r.code)
    }

    func text(_ args: [String], input: Data? = nil, env: [String: String] = [:]) throws -> String {
        String(decoding: try run(args, input: input, env: env).out, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

/// The repository and the files it keeps. Used from the `Doll` actor (one change at a time in the
/// process) and locked with `flock` against another process (the offline `consolidate`).
public final class History: @unchecked Sendable {
    let git: Git
    /// `data` and `memory`: the two trees' prefixes and the dirs they mirror.
    let roots: [(prefix: String, dir: URL)]
    let memoryRegistryDir: String
    let secretDir: String
    let ownDir: String
    static let branch = "refs/heads/main"
    /// Data-dir files kept, besides `tools/*.json`.
    static let dataFiles = ["ideal-self.md", "ideal-self-log.json", "goals.json", "record.json", "memory-fixes.json"]
    /// Changes whose commit failed in this process, for the next commit's message.
    var unrecorded: [String] = []
    /// The snapshot last committed (or found equal to the head): a change that writes nothing makes no
    /// git calls beyond one hashing.
    var last: [String: String]?
    let lockFD: Int32

    /// What opening found.
    public enum Boot: Sendable, Equatable {
        /// A new repository: the files as found are its first commit (their count).
        case adopted(Int)
        /// Files that differ from the last commit: changed with no commit (a crash between the write
        /// and the commit, a failed commit, a hand edit), committed now as found at boot.
        case found([String])
        case clean
    }

    /// Opens the encrypted folders' files for the owner's diff and for merging an undo.
    let box: SecretBox

    public init(config: HistoryConfig, data: URL, memory: MemoryConfig, memoryDir: URL, box: SecretBox) throws {
        let exe = try Git.locate(config.git)
        let dir = config.dir.hasPrefix("/") ? URL(filePath: config.dir) : data.appending(path: config.dir)
        git = Git(exe: exe, gitDir: dir.standardizedFileURL)
        roots = [("data", data.standardizedFileURL), ("memory", memoryDir.standardizedFileURL)]
        memoryRegistryDir = memory.registryDir
        secretDir = memory.secretDir
        ownDir = memory.ownDir
        self.box = box
        let fm = FileManager.default
        if !fm.fileExists(atPath: dir.appending(path: "HEAD").path) {
            try fm.createDirectory(at: dir, withIntermediateDirectories: true)
            let r = try git.raw(["init", "--bare", "--quiet", "--initial-branch=main", "--shared=0600", dir.path], cwd: dir)
            guard r.code == 0 else {
                throw HistoryError(description: "git init \(dir.path) failed: \(String(decoding: r.err, as: UTF8.self))")
            }
            // The repository's own settings, written in place (one process fewer per new repository):
            // paths shown as they are (her tools' names are Chinese), no line-ending changes.
            let config = dir.appending(path: "config")
            let h = try FileHandle(forWritingTo: config)
            defer { try? h.close() }
            try h.seekToEnd()
            try h.write(contentsOf: Data("[user]\n\tname = dollcore\n\temail = dollcore@localhost\n[core]\n\tquotepath = false\n\tautocrlf = false\n[gc]\n\tautoDetach = false\n".utf8))
        }
        // Only the owner's user reads it. The secret and own folders' files are in it as they are on
        // disk: encrypted (`SecretBox`).
        try fm.setAttributes([.posixPermissions: 0o700], ofItemAtPath: dir.path)
        let fd = Darwin.open(dir.appending(path: "doll.lock").path, O_CREAT | O_RDWR, 0o600)
        guard fd >= 0 else { throw HistoryError(description: "cannot open \(dir.path)/doll.lock: errno \(errno)") }
        lockFD = fd
    }

    deinit { close(lockFD) }

    /// One change at a time: `flock` against another process, and a lock against another thread of
    /// this one (a `flock` held through one descriptor lets every thread of the process in).
    func locked<T>(_ f: () throws -> T) throws -> T {
        threadLock.lock()
        defer { threadLock.unlock() }
        flock(lockFD, LOCK_EX)
        defer { flock(lockFD, LOCK_UN) }
        return try f()
    }
    private let threadLock = NSLock()

    /// At boot: the first commit of a new repository (everything as found), or what changed with no
    /// commit since the last one.
    public func open(ts: Double) throws -> Boot {
        try locked {
            guard try head() != nil else {
                let snap = try snapshot()
                _ = try commitLocked(HistoryChange(who: .program, what: "開始記錄",
                                                   why: "開始記錄改動：照開機時找到的樣子（\(snap.count) 個檔）", ts: ts), snap)
                return .adopted(snap.count)
            }
            let snap = try snapshot()
            let headTree = try tree(Self.branch)
            let diff = Set(snap.keys).union(headTree.keys).filter { snap[$0] != headTree[$0] }.sorted()
            guard !diff.isEmpty else {
                last = snap
                return .clean
            }
            _ = try commitLocked(HistoryChange(who: .program, what: "開機時發現沒記錄的改動",
                                               why: "開機時發現，沒有記錄：上次結束前寫了檔卻沒記進來（當掉或記錄失敗）", ts: ts), snap)
            return .found(diff)
        }
    }

    // files ------------------------------------------------------------------------------------

    /// Every kept file now: tree path → its URL.
    func files() -> [(path: String, url: URL)] {
        let fm = FileManager.default
        var out: [(String, URL)] = []
        let data = roots[0].dir, mem = roots[1].dir
        for f in Self.dataFiles where fm.fileExists(atPath: data.appending(path: f).path) {
            out.append(("data/" + f, data.appending(path: f)))
        }
        let tools = data.appending(path: "tools")
        for f in ((try? fm.contentsOfDirectory(atPath: tools.path)) ?? []).sorted() where f.hasSuffix(".json") && !f.hasPrefix(".") {
            out.append(("data/tools/" + f, tools.appending(path: f)))
        }
        // Memory folders and the registry: one level of folders of `.md` files. The data dir or the
        // repository inside the memory dir (a config that nests them) is not memory.
        let skip = Set([data.path, git.gitDir.path])
        for d in ((try? fm.contentsOfDirectory(atPath: mem.path)) ?? []).sorted() where !d.hasPrefix(".") {
            let sub = mem.appending(path: d)
            guard !skip.contains(sub.standardizedFileURL.path) else { continue }
            var isDir: ObjCBool = false
            guard fm.fileExists(atPath: sub.path, isDirectory: &isDir), isDir.boolValue else { continue }
            for f in ((try? fm.contentsOfDirectory(atPath: sub.path)) ?? []).sorted() where f.hasSuffix(".md") && !f.hasPrefix(".") {
                out.append(("memory/\(d)/\(f)", sub.appending(path: f)))
            }
        }
        return out
    }

    /// The file a tree path names; nil for a path outside the kept dirs.
    func url(_ path: String) -> URL? {
        let parts = path.split(separator: "/", maxSplits: 1).map(String.init)
        guard parts.count == 2, !parts[1].split(separator: "/").contains(where: { $0 == ".." || $0 == "." || $0.isEmpty }),
              let root = roots.first(where: { $0.prefix == parts[0] }) else { return nil }
        return root.dir.appending(path: parts[1])
    }

    /// Path → blob id of every kept file now, hashed in this process (nothing is written to the
    /// repository until a commit). A file is hashed again only when its size, inode or modification
    /// time (nanoseconds) changed since: most of her calls write nothing kept and cost a few `stat`s.
    func snapshot() throws -> [String: String] {
        var snap: [String: String] = [:], stats: [String: [Int]] = [:]
        for f in files() {
            var st = stat()
            guard lstat(f.url.path, &st) == 0 else { throw HistoryError(description: "cannot stat \(f.url.path): errno \(errno)") }
            let key = [Int(st.st_size), Int(st.st_ino), Int(st.st_mtimespec.tv_sec), Int(st.st_mtimespec.tv_nsec)]
            stats[f.path] = key
            if let c = statCache[f.path], c.key == key {
                snap[f.path] = c.id
            } else {
                let id = Self.blobID(try Data(contentsOf: f.url))
                statCache[f.path] = (key, id)
                snap[f.path] = id
            }
        }
        statCache = statCache.filter { stats[$0.key] != nil }
        return snap
    }
    private var statCache: [String: (key: [Int], id: String)] = [:]

    /// The branch's commit, read from the repository's files: two `rev-parse` processes around every
    /// commit were 62% of the history's processes (2026-11-20, measured over the test suite). The
    /// loose ref (git replaces it by a rename, so a read sees the old or the new id), else
    /// `packed-refs`; a repository in the reftable format is asked through git.
    func head() throws -> String? {
        let fm = FileManager.default
        guard !fm.fileExists(atPath: git.gitDir.appending(path: "reftable").path) else {
            let r = try git.run(["rev-parse", "-q", "--verify", Self.branch + "^{commit}"], ok: [0, 1])
            let s = String(decoding: r.out, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
            return r.code == 0 && !s.isEmpty ? s : nil
        }
        func id(_ s: Substring) throws -> String {
            guard s.count == 40, s.allSatisfy(\.isHexDigit) else {
                throw HistoryError(description: "\(Self.branch) in \(git.gitDir.path) is not a commit id: \(s.prefix(60))")
            }
            return String(s)
        }
        if let d = fm.contents(atPath: git.gitDir.appending(path: Self.branch).path) {
            return try id(Substring(String(decoding: d, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)))
        }
        guard let packed = fm.contents(atPath: git.gitDir.appending(path: "packed-refs").path) else { return nil }
        for line in String(decoding: packed, as: UTF8.self).split(separator: "\n") where line.hasSuffix(" " + Self.branch) {
            return try id(line.prefix(40))
        }
        return nil
    }

    /// Path → blob id of a commit's tree.
    func tree(_ rev: String) throws -> [String: String] {
        let out = try git.run(["ls-tree", "-r", "-z", "--full-tree", rev]).out
        var map: [String: String] = [:]
        for rec in out.split(separator: 0) {
            let s = String(decoding: rec, as: UTF8.self)
            guard let tab = s.firstIndex(of: "\t") else { continue }
            let meta = s[..<tab].split(separator: " ")
            guard meta.count == 3 else { continue }
            map[String(s[s.index(after: tab)...])] = String(meta[2])
        }
        return map
    }

    func blob(_ id: String) throws -> Data { try git.run(["cat-file", "blob", id]).out }

    /// The full id of a commit on the branch named by `rev` (a full or short id).
    func resolve(_ rev: String) throws -> String {
        guard rev.count >= 4, rev.count <= 40, rev.allSatisfy({ $0.isHexDigit }) else {
            throw HistoryError(description: "沒有編號是 \(rev) 的改動")
        }
        let r = try git.run(["rev-parse", "-q", "--verify", rev + "^{commit}"], ok: [0, 1, 128])
        let id = String(decoding: r.out, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        guard r.code == 0, !id.isEmpty, let head = try head(),
              try git.run(["merge-base", "--is-ancestor", id, head], ok: [0, 1]).code == 0 else {
            throw HistoryError(description: "沒有編號是 \(rev) 的改動")
        }
        return id
    }

    // committing --------------------------------------------------------------------------------

    /// Commits every kept file as it is now, as one change; nil when nothing changed. A failure
    /// leaves the files as they are and is remembered for the next commit's message.
    @discardableResult
    public func record(_ c: HistoryChange) throws -> HistoryEntry? {
        do {
            return try locked {
                let snap = try snapshot()
                if let last, last == snap { return nil }
                return try commitLocked(c, snap)
            }
        } catch {
            unrecorded.append("\(c.who.rawValue) \(c.what)")
            throw error
        }
    }

    static func line(_ s: String) -> String {
        s.replacingOccurrences(of: "\r", with: "").replacingOccurrences(of: "\n", with: "⏎").trimmingCharacters(in: .whitespaces)
    }

    static func message(_ c: HistoryChange, unrecorded: [String]) -> String {
        var m = "\(c.who.ownerWords)：\(line(c.what))" + (c.task.map { "（\($0)）" } ?? "") + "\n\n"
        m += "Who: \(c.who.rawValue)\n"
        if let t = c.task { m += "Task: \(line(t))\n" }
        m += "What: \(line(c.what))\n"
        m += "Why: \(line(c.why))\n"
        if let a = c.args, !a.isEmpty { m += "Args: \(line(a))\n" }
        for r in c.refs { m += "Ref: \(r)\n" }
        if let u = c.undoes { m += "Undoes: \(u)\n" }
        if let r = c.restores { m += "Restores: \(r)\n" }
        for u in unrecorded { m += "Unrecorded-Before: \(line(u))\n" }
        return m
    }

    /// One commit of `snap`: one `git fast-import` process, the changed files given inline (2026-11-19:
    /// five plumbing calls cost about 100 ms a change under test load, on the actor's path), and
    /// `rev-parse` for the head before and after it: fast-import does not move the branch when the
    /// commit does not descend from it (another process committed meanwhile).
    func commitLocked(_ c: HistoryChange, _ snap: [String: String]) throws -> HistoryEntry? {
        let parent = try head()
        let before = try last ?? parent.map { try self.tree($0) } ?? [:]
        let paths = Set(snap.keys).union(before.keys).filter { snap[$0] != before[$0] }.sorted()
        guard !paths.isEmpty else {
            last = snap
            return nil
        }
        // A change to an encrypted folder keeps no why and no args in its message (the commit message
        // is plain: `remember`'s args are the memory's text); the diff shows what changed, decrypted.
        var c = c
        if paths.contains(where: isSealed) { (c.why, c.args) = ("", nil) }
        let message = Self.message(c, unrecorded: unrecorded)
        var stream = Data()
        func add(_ s: String) { stream.append(Data(s.utf8)) }
        func data(_ d: Data) {
            add("data \(d.count)\n")
            stream.append(d)
            add("\n")
        }
        add("commit \(Self.branch)\nmark :1\n")
        add("author dollcore <dollcore@localhost> \(Int(c.ts)) +0000\ncommitter dollcore <dollcore@localhost> \(Int(c.ts)) +0000\n")
        data(Data(message.utf8))
        if let parent { add("from \(parent)\n") }
        for p in paths {
            if snap[p] != nil {
                guard let u = url(p) else { throw HistoryError(description: "not a kept path: \(p)") }
                let content = try Data(contentsOf: u)
                guard Self.blobID(content) == snap[p] else { throw HistoryError(description: "\(p) changed while it was being recorded") }
                add("M 100644 inline \(p)\n")
                data(content)
            } else {
                add("D \(p)\n")
            }
        }
        add("done\n")
        let marks = git.gitDir.appending(path: "io-marks-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: marks) }
        try git.run(["fast-import", "--quiet", "--done", "--date-format=raw", "--export-marks=\(marks.path)"], input: stream)
        guard let line = try String(contentsOf: marks, encoding: .utf8).split(separator: "\n").first(where: { $0.hasPrefix(":1 ") }) else {
            throw HistoryError(description: "fast-import wrote no commit id")
        }
        let id = String(line.dropFirst(3))
        // fast-import leaves the branch where it was when the commit does not descend from it.
        guard try head() == id else {
            throw HistoryError(description: "the history moved under this process (another dollcore wrote to it); not recorded")
        }
        let entry = Self.parse(Substring([id, "\(Int(c.ts))", message, paths.joined(separator: "\n")].joined(separator: "\u{1f}")))!
        unrecorded = []
        last = snap
        return entry
    }

    /// Git's blob id of `d` (SHA-1 of `blob <length>\0` and the bytes).
    static func blobID(_ d: Data) -> String {
        var h = Insecure.SHA1()
        h.update(data: Data("blob \(d.count)\0".utf8))
        h.update(data: d)
        return h.finalize().map { String(format: "%02x", $0) }.joined()
    }

    // reading -----------------------------------------------------------------------------------

    static func parse(_ record: Substring) -> HistoryEntry? {
        let f = record.split(separator: "\u{1f}", omittingEmptySubsequences: false)
        guard f.count >= 4 else { return nil }
        var e = HistoryEntry(id: f[0].trimmingCharacters(in: .whitespacesAndNewlines), ts: Double(f[1]) ?? 0, who: .program,
                             task: nil, what: "", why: "", args: nil, refs: [], undoes: nil, restores: nil, unrecordedBefore: [], paths: [])
        for line in f[2].split(separator: "\n") {
            guard let colon = line.range(of: ": ") else { continue }
            let k = line[..<colon.lowerBound], v = String(line[colon.upperBound...])
            switch k {
            case "Who": e.who = HistoryWho(rawValue: v) ?? .program
            case "Task": e.task = v
            case "What": e.what = v
            case "Why": e.why = v
            case "Args": e.args = v
            case "Ref": e.refs.append(v)
            case "Undoes": e.undoes = v
            case "Restores": e.restores = v
            case "Unrecorded-Before": e.unrecordedBefore.append(v)
            default: break
            }
        }
        e.paths = f[3].split(separator: "\n").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        return e
    }

    /// Newest first: at most `limit`, only those touching `paths` (tree paths or dir prefixes) when given.
    public func log(limit: Int, paths: [String] = [], from rev: String? = nil) throws -> [HistoryEntry] {
        guard let head = try head() else { return [] }
        let out = try git.run(["log", "--format=%x1e%H%x1f%at%x1f%B%x1f", "--name-only", "--no-renames", "-n", "\(limit)", rev ?? head, "--"]
                              + paths.map { ":(literal)" + $0 }).out
        return String(decoding: out, as: UTF8.self).split(separator: "\u{1e}").compactMap(Self.parse)
    }

    public func count(paths: [String] = []) throws -> Int {
        guard let head = try head() else { return 0 }
        return Int(try git.text(["rev-list", "--count", head, "--"] + paths.map { ":(literal)" + $0 })) ?? 0
    }

    public func entry(_ rev: String) throws -> HistoryEntry {
        let id = try resolve(rev)
        guard let e = try log(limit: 1, from: id).first else { throw HistoryError(description: "沒有編號是 \(rev) 的改動") }
        return e
    }

    /// The commit carrying a feature record (`ideal#3`, `fix#2`); nil when none does (made before the
    /// history began).
    public func entry(ref: String) throws -> HistoryEntry? {
        guard let head = try head() else { return nil }
        let pattern = "^Ref: " + NSRegularExpression.escapedPattern(for: ref) + "$"
        let id = try git.text(["log", "--format=%H", "-n", "1", "-E", "--grep=\(pattern)", head])
        return id.isEmpty ? nil : try entry(id)
    }

    /// The patch of one change (its first commit: against nothing). The encrypted folders' files, and
    /// any file stored as a box on either side (her documents written by a sealed turn), are decrypted
    /// in memory and shown whole, each line marked (` `, `-`, `+`), after git's patch of the rest.
    public func diff(_ rev: String) throws -> String {
        let id = try resolve(rev)
        let base = ["diff-tree", "--root", "--no-commit-id", "--no-color", "--no-ext-diff", "--no-renames"]
        var boxed: [(path: String, old: Substring, new: Substring)] = []
        for line in try git.text(base + ["-r", id, "--"]).split(separator: "\n") {
            // :100644 100644 <old> <new> M\t<path>
            let parts = line.split(separator: "\t", maxSplits: 1)
            let fields = parts[0].split(separator: " ")
            guard parts.count == 2, fields.count >= 4 else { continue }
            let path = String(parts[1])
            func sealedBlob(_ blobID: Substring) throws -> Bool {
                try blobID.contains(where: { $0 != "0" }) && blob(String(blobID)).starts(with: Data(SecretBox.prefix.utf8))
            }
            if try isSealed(path) || sealedBlob(fields[2]) || sealedBlob(fields[3]) { boxed.append((path, fields[2], fields[3])) }
        }
        var out = String(decoding: try git.run(base + ["-p", id, "--", "."] + boxed.map { ":(exclude,literal)\($0.path)" }).out, as: UTF8.self)
        for (path, old, new) in boxed {
            func text(_ blobID: Substring) throws -> [String]? {
                guard blobID.contains(where: { $0 != "0" }) else { return nil }
                let raw = String(decoding: try blob(String(blobID)), as: UTF8.self)
                let plain = SecretBox.isSealed(raw) ? try box.open(raw, place: "\(path) in the history") : raw
                return plain.components(separatedBy: "\n")
            }
            out += Self.wholeDiff(path: path, old: try text(old), new: try text(new))
        }
        return out
    }

    /// A patch of one file shown whole (no hunks): every line, `-` removed, `+` added, ` ` kept.
    static func wholeDiff(path: String, old: [String]?, new: [String]?) -> String {
        let a = old ?? [], b = new ?? []
        let change = b.difference(from: a)
        var removed = Set<Int>(), inserted = Set<Int>()
        for c in change {
            switch c {
            case .remove(let o, _, _): removed.insert(o)
            case .insert(let o, _, _): inserted.insert(o)
            }
        }
        var lines = ["diff --git a/\(path) b/\(path)", old == nil ? "--- /dev/null" : "--- a/\(path)",
                     new == nil ? "+++ /dev/null" : "+++ b/\(path)", "@@ -1,\(a.count) +1,\(b.count) @@ (decrypted)"]
        var i = 0, j = 0
        while i < a.count || j < b.count {
            if i < a.count, removed.contains(i) { lines.append("-" + a[i]); i += 1 }
            else if j < b.count, inserted.contains(j) { lines.append("+" + b[j]); j += 1 }
            else { lines.append(" " + a[i]); i += 1; j += 1 }
        }
        return lines.joined(separator: "\n") + "\n"
    }

    /// A path of an encrypted memory folder (`memory/<secret|own>/…`).
    func isSealed(_ path: String) -> Bool { [secretDir, ownDir].contains { path.hasPrefix("memory/\($0)/") } }

    /// `merge`, with an encrypted file (an encrypted folder's, or a document stored as a box on any
    /// side) decrypted for it and the result encrypted again.
    func mergeFile(path: String, cur: Data, base: Data, other: Data) throws -> Data? {
        let prefix = Data(SecretBox.prefix.utf8)
        guard isSealed(path) || [cur, base, other].contains(where: { $0.starts(with: prefix) }) else {
            return Self.merge(path: path, cur: cur, base: base, other: other)
        }
        func open(_ d: Data) throws -> Data {
            d.starts(with: prefix) ? Data(try box.open(String(decoding: d, as: UTF8.self), place: path).utf8) : d
        }
        guard let merged = Self.merge(path: path, cur: try open(cur), base: try open(base), other: try open(other)) else { return nil }
        return Data((try box.seal(String(decoding: merged, as: UTF8.self)) + "\n").utf8)
    }

    // going back --------------------------------------------------------------------------------

    func write(_ path: String, _ content: Data?) throws {
        guard let u = url(path) else { throw HistoryError(description: "not a kept path: \(path)") }
        if let content {
            try FileManager.default.createDirectory(at: u.deletingLastPathComponent(), withIntermediateDirectories: true)
            try content.write(to: u, options: .atomic)
        } else if FileManager.default.fileExists(atPath: u.path) {
            try FileManager.default.removeItem(at: u)
        }
    }

    /// Writes the inverse of change `rev` into the files (not committed: the caller reloads, then
    /// commits). Each file it touched goes back to how it was before it: unchanged since, directly;
    /// a file it created goes (later edits of that file with it); changed since, merged
    /// (`History.merge`) and refused on a conflict, naming the later changes. Returns the paths written.
    public func undoWrites(_ rev: String) throws -> [String] {
        try locked {
            let id = try resolve(rev)
            let parentR = try git.run(["rev-parse", "-q", "--verify", id + "^"], ok: [0, 1, 128])
            guard parentR.code == 0 else { throw HistoryError(description: "這是開始記錄時的樣子，前面沒有可以回去的") }
            let parent = String(decoding: parentR.out, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
            let after = try tree(id), before = try tree(parent)
            let changed = Set(after.keys).union(before.keys).filter { after[$0] != before[$0] }.sorted()
            let now = try snapshot()
            var writes: [(String, Data?)] = [], conflicts: [String] = []
            for p in changed {
                let b = after[p], t = before[p], c = now[p]
                if c == t { continue }
                if c == b {
                    writes.append((p, try t.map(blob)))
                } else if t == nil, p.hasSuffix(".md") || p.hasPrefix("data/tools/") {
                    // A memory, registry entry or tool the change created goes, its later edits with it.
                    writes.append((p, nil))
                } else if let b, c != nil, let u = url(p), let merged = try mergeFile(path: p, cur: try Data(contentsOf: u), base: try blob(b),
                                                                          other: try t.map(blob) ?? Data("{}".utf8)) {
                    // A record file the change created: merged against an empty one, so later records stay.
                    writes.append((p, merged))
                } else {
                    conflicts.append(p)
                }
            }
            if !conflicts.isEmpty {
                let later = try log(limit: 20, paths: conflicts, from: "\(id)..\(Self.branch)")
                throw HistoryError(description: "退不回去：\(conflicts.map(Self.thing).joined(separator: "、")) 後來又改過（"
                                   + later.map { "〔\($0.short)〕\($0.who.ownerWords) \($0.what)" }.joined(separator: "；")
                                   + "）。先退回那些，或回到某個時間點")
            }
            guard !writes.isEmpty else { throw HistoryError(description: "這個改動後來已經改回去了，沒有東西要退回") }
            for (p, d) in writes { try write(p, d) }
            return writes.map(\.0)
        }
    }

    /// Writes every kept file as it was right after change `rev` (not committed). Returns the paths written.
    public func restoreWrites(_ rev: String) throws -> [String] {
        try locked {
            let id = try resolve(rev)
            let target = try tree(id), now = try snapshot()
            let changed = Set(target.keys).union(now.keys).filter { target[$0] != now[$0] }.sorted()
            guard !changed.isEmpty else { throw HistoryError(description: "現在就是那時候的樣子，沒有東西要改") }
            for p in changed { try write(p, try target[p].map(blob)) }
            return changed
        }
    }

    // merging ------------------------------------------------------------------------------------

    /// A three-way merge of one file: `cur` (now) with the change from `base` to `other` applied.
    /// JSON by structure (objects by key, lists of objects by `id`; `next_id` counters keep their
    /// current value, so no id is handed out twice); front-matter markdown by key and body; anything
    /// else only when one side did not change. Nil: a conflict.
    static func merge(path: String, cur: Data, base: Data, other: Data) -> Data? {
        if cur == base { return other }
        if other == base || cur == other { return cur }
        if path.hasSuffix(".json") {
            guard let c = try? JSONSerialization.jsonObject(with: cur, options: .fragmentsAllowed),
                  let b = try? JSONSerialization.jsonObject(with: base, options: .fragmentsAllowed),
                  let o = try? JSONSerialization.jsonObject(with: other, options: .fragmentsAllowed),
                  let merged = try? mergeJSON(c, b, o, key: nil),
                  var data = try? JSONSerialization.data(withJSONObject: merged, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes, .fragmentsAllowed])
            else { return nil }
            if cur.last == 0x0A { data.append(0x0A) }
            return data
        }
        if path.hasSuffix(".md"), let c = frontMatter(cur), let b = frontMatter(base), let o = frontMatter(other) {
            var keys = c.keys.map(\.0)
            for (k, _) in o.keys where !keys.contains(k) { keys.append(k) }
            func value(_ kv: [(String, String)], _ k: String) -> String? { kv.first { $0.0 == k }?.1 }
            var lines = ["+++"]
            for k in keys {
                guard let v = pick(value(c.keys, k), value(b.keys, k), value(o.keys, k)) else { return nil }
                if let v { lines.append(v) }
            }
            guard let body = pick(c.body, b.body, o.body), let body else { return nil }
            lines.append("+++")
            return Data((lines.joined(separator: "\n") + "\n" + body).utf8)
        }
        return nil
    }

    /// The three-way choice for one value: nil = conflict; `.some(nil)` = absent.
    static func pick<T: Equatable>(_ c: T?, _ b: T?, _ o: T?) -> T?? {
        if c == b { return .some(o) }
        if o == b || c == o { return .some(c) }
        return nil
    }

    /// `+++` front-matter lines by key (the text before `=`), and the body after it.
    static func frontMatter(_ d: Data) -> (keys: [(String, String)], body: String)? {
        let lines = String(decoding: d, as: UTF8.self).components(separatedBy: "\n")
        guard lines.first == "+++", let end = lines.dropFirst().firstIndex(of: "+++") else { return nil }
        let keys = lines[1..<end].map { l in (l.split(separator: "=", maxSplits: 1).first.map { $0.trimmingCharacters(in: .whitespaces) } ?? l, l) }
        guard Set(keys.map(\.0)).count == keys.count else { return nil }
        return (keys, lines[(end + 1)...].joined(separator: "\n"))
    }

    struct Conflict: Error {}

    static func same(_ a: Any?, _ b: Any?) -> Bool {
        switch (a, b) {
        case (nil, nil): true
        case (let x?, let y?): (x as AnyObject).isEqual(y)
        default: false
        }
    }

    static func mergeJSON(_ c: Any?, _ b: Any?, _ o: Any?, key: String?) throws -> Any? {
        if same(c, b) { return o }
        if same(o, b) || same(c, o) { return c }
        if key == "next_id" { return c }
        // A side that has no such object or list (a file the change created) counts as an empty one.
        if let cd = c as? [String: Any], let od = o as? [String: Any] ?? (o == nil ? [:] : nil) {
            let bd = b as? [String: Any] ?? [:]
            var out: [String: Any] = [:]
            for k in Set(cd.keys).union(od.keys).union(bd.keys) {
                if let v = try mergeJSON(cd[k], bd[k], od[k], key: k) { out[k] = v }
            }
            return out
        }
        if let ca = c as? [Any], let oa = o as? [Any] ?? (o == nil ? [] : nil) {
            let ba = b as? [Any] ?? []
            func ids(_ a: [Any]) -> [(String, Any)]? {
                var out: [(String, Any)] = []
                for x in a {
                    guard let d = x as? [String: Any], let id = d["id"] else { return nil }
                    out.append(("\(id)", x))
                }
                return out
            }
            guard let ci = ids(ca), let oi = ids(oa), let bi = ids(ba) else { throw Conflict() }
            func find(_ l: [(String, Any)], _ id: String) -> Any? { l.first { $0.0 == id }?.1 }
            var order = ci.map(\.0)
            for (id, _) in oi where !order.contains(id) { order.append(id) }
            var out: [Any] = []
            for id in order {
                if let v = try mergeJSON(find(ci, id), find(bi, id), find(oi, id), key: nil) { out.append(v) }
            }
            return out
        }
        throw Conflict()
    }

    // words ---------------------------------------------------------------------------------------

    /// One kept file in plain words (for her and the owner).
    static func thing(_ path: String) -> String {
        let p = path.split(separator: "/").map(String.init)
        switch path {
        case "data/ideal-self.md": return "想成為的樣子"
        case "data/ideal-self-log.json": return "想成為的樣子的修改紀錄"
        case "data/goals.json": return "目標樹"
        case "data/record.json": return "做完的事的紀錄"
        case "data/memory-fixes.json": return "記憶的更正紀錄"
        default: break
        }
        if p.count == 3, p[0] == "data", p[1] == "tools" { return "工具「\(p[2].replacingOccurrences(of: ".json", with: ""))」" }
        if p.count == 3, p[0] == "memory" { return "記憶 \(p[2].replacingOccurrences(of: ".md", with: ""))" }
        return path
    }

    /// The folder of a memory path (`secret`, `own`, …), nil for anything else.
    func memoryFolder(_ path: String) -> String? {
        let p = path.split(separator: "/").map(String.init)
        return p.count == 3 && p[0] == "memory" ? p[1] : nil
    }

    func isRegistry(_ path: String) -> Bool { memoryFolder(path) == memoryRegistryDir }

    /// Plain words for a list of paths: the log file goes with the ideal self, registry entries are
    /// counted, files of a folder in `hidden` are only counted.
    func things(_ paths: [String], hidden: Set<String> = []) -> String {
        var out: [String] = [], hiddenCount = 0, registry = 0
        for p in paths where p != "data/ideal-self-log.json" || !paths.contains("data/ideal-self.md") {
            if let f = memoryFolder(p), hidden.contains(f) { hiddenCount += 1; continue }
            if isRegistry(p) { registry += 1; continue }
            let prefix = memoryFolder(p) == secretDir ? "秘密的" : ""
            out.append(prefix + Self.thing(p))
        }
        if registry > 0 { out.append("人事物登記 \(registry) 筆") }
        if hiddenCount > 0 { out.append("\(hiddenCount) 條你現在看不到的記憶") }
        return out.isEmpty ? "（沒有檔案）" : out.joined(separator: "、")
    }
}

/// The owner's view of one change.
public struct DashboardHistoryEntry: Codable, Sendable {
    public var entry: HistoryEntry
    /// The files it changed, in plain words.
    public var things: String
    /// It touched the secret memory folder.
    public var secret: Bool
}

extension Doll {
    /// Commits whatever kept file changed, as one change. Never throws: a failed commit is an ERROR
    /// in the log (the boot report carries it), the change stands, and the next commit names it.
    /// Returns the failure, nil when committed or when nothing changed.
    @discardableResult
    func commitHistory(_ who: HistoryWho, task: String? = nil, what: String, why: String, args: String? = nil,
                       undoes: String? = nil, restores: String? = nil) -> String? {
        // A change made while sealing (a turn that saw a secret) keeps its what, never why and args.
        let c = HistoryChange(who: who, task: task, what: what, why: sealing ? "" : why, args: sealing ? nil : args, refs: historyRefs,
                              undoes: undoes, restores: restores, ts: wallClock.now())
        do {
            if let e = try history.record(c) {
                historyRefs = []
                log("INFO", "history: \(e.short) \(who.rawValue)\(task.map { "/\($0)" } ?? "") \(what): \(e.paths.count) files")
                feed.notify(.history)
            }
            return nil
        } catch {
            log("ERROR", "history: commit failed (\(who.rawValue) \(what)); the change stands, not recorded: \(error)")
            return "\(error)"
        }
    }

    /// After one of her calls: what it changed is her change, with the reason her call gave.
    func commitCall(_ call: ToolCall) -> String? {
        let why = ["why", "reason", "basis", "based_on", "when", "cause"].compactMap { k in
            call.parameters.first { $0.name == k }?.value.trimmingCharacters(in: .whitespacesAndNewlines)
        }.first { !$0.isEmpty } ?? ""
        let args = call.parameters.map { "\($0.name)=\($0.value)" }.joined(separator: "；")
        return commitHistory(.her, what: call.name, why: why, args: String(args.prefix(300)))
    }

    /// The in-memory state and the index made to match the files the history just wrote.
    func reloadAfterHistory(_ paths: [String]) throws {
        var mem: [String] = []
        for p in paths {
            switch p {
            case "data/ideal-self.md":
                if drivesConfig.enabled, let text = try Concerns.readDocument(idealURL, box: memory.box) {
                    ideal = String(decoding: text, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
                }
            case "data/ideal-self-log.json": idealLog = try readDocument(IdealLog.self, idealLogURL, what: "ideal-self log") ?? IdealLog()
            case "data/goals.json":
                goalFile = try readDocument(GoalFile.self, goalsURL, what: "goal tree") ?? GoalFile()
                goalsChanged()
            case "data/record.json": recordFile = try readDocument(RecordFile.self, recordURL, what: "record") ?? RecordFile()
            case "data/memory-fixes.json": fixFile = try MemoryFixFile.read(fixesURL, box: memory.box) ?? MemoryFixFile()
            default:
                // Tools are read from disk at every use; memory and registry files need their index rows.
                if p.hasPrefix("memory/") { mem.append(String(p.dropFirst("memory/".count))) }
            }
        }
        try memory.reindex(mem)
        feed.notify(.drives)
        feed.notify(.history)
    }

    static func historyTime(_ ts: Double) -> String {
        let c = Calendar.current.dateComponents([.month, .day, .hour, .minute], from: Date(timeIntervalSince1970: ts))
        return String(format: "%02d-%02d %02d:%02d", c.month!, c.day!, c.hour!, c.minute!)
    }

    /// The memory folders `audience` may not see, by name.
    func hiddenFolders(_ audience: Audience) -> Set<String> {
        let visible = Memory.visible(audience, sawSecret: true)
        return Set(MemoryFolder.allCases.filter { !visible.contains($0) }.map { memory.dirName($0) })
    }

    func touchesSecret(_ paths: [String]) -> Bool { paths.contains { history.memoryFolder($0) == memoryConfig.secretDir } }

    /// What changed now, for her: the ideal self in full when it changed; the tree to look at.
    func historyAfterWords(_ paths: [String]) -> String {
        var s = ""
        if paths.contains("data/ideal-self.md") { s += "現在你想成為的樣子是：\(ideal)。" }
        if paths.contains("data/goals.json") { s += "目標樹用 goal.list 看。" }
        return s
    }

    // the owner (dashboard) -------------------------------------------------------------------------

    public func dashboardHistory(limit: Int? = nil) throws -> [DashboardHistoryEntry] {
        try history.log(limit: limit ?? historyConfig.dashboardItems).map {
            DashboardHistoryEntry(entry: $0, things: history.things($0.paths), secret: touchesSecret($0.paths))
        }
    }

    /// The owner's diff of one change (secret memories included: the dashboard is where his secrets show).
    public func dashboardHistoryDiff(_ id: String) throws -> String { try history.diff(id) }

    /// The owner undoes one change (anyone's): its inverse, a new commit; she is told in her next turn.
    @discardableResult
    public func dashboardHistoryUndo(_ id: String) throws -> HistoryEntry {
        let target = try history.entry(id)
        let paths = try history.undoWrites(target.id)
        try reloadAfterHistory(paths)
        let failed = commitHistory(.owner, what: "退回〔\(target.short)〕\(target.who.ownerWords)的「\(target.what)」",
                                   why: "主人在儀表板退回", undoes: target.id)
        let whose = target.who == .her ? "你" : target.who.words
        toldOwner(Event(.memory, .internal, "【主人退回了一個改動】\(Self.historyTime(target.ts)) \(whose)的「\(target.what)」"
                        + (target.why.isEmpty ? "" : "（那時的理由：\(target.why)）")
                        + "，主人在儀表板把它退回了：\(history.things(paths)) 回到那之前的樣子。" + historyAfterWords(paths),
                        secret: touchesSecret(paths)))
        if let failed { throw HistoryError(description: "退回了，但沒記進改動紀錄：\(failed)") }
        return try history.entry(Self.headID(history))
    }

    /// The owner returns everything kept to how it was right after change `id`.
    @discardableResult
    public func dashboardHistoryRestore(_ id: String) throws -> HistoryEntry {
        let target = try history.entry(id)
        let paths = try history.restoreWrites(target.id)
        try reloadAfterHistory(paths)
        let failed = commitHistory(.owner, what: "回到 \(Self.historyTime(target.ts))〔\(target.short)〕的樣子",
                                   why: "主人在儀表板回到那個時間點", restores: target.id)
        toldOwner(Event(.memory, .internal, "【主人把你的東西回到 \(Self.historyTime(target.ts)) 的樣子】那之後的改動都退回了，動到：\(history.things(paths))。"
                        + historyAfterWords(paths), secret: touchesSecret(paths)))
        if let failed { throw HistoryError(description: "回去了，但沒記進改動紀錄：\(failed)") }
        return try history.entry(Self.headID(history))
    }

    static func headID(_ h: History) throws -> String {
        guard let id = try h.head() else { throw HistoryError(description: "the history has no commit") }
        return id
    }

    /// A no-turn notice of the owner's edit, secret when it names a secret memory.
    func toldOwner(_ ev: Event) {
        do { try notice(ev) } catch { log("ERROR", "history: owner-edit notice not written: \(error)") }
    }

    /// The feature buttons (ideal-self revision, memory correction): shortcuts to undoing the commit
    /// that made that record.
    func undoRef(_ ref: String) throws {
        guard let e = try history.entry(ref: ref) else {
            throw HistoryError(description: "\(ref) was made before the history began; return to a point instead")
        }
        try dashboardHistoryUndo(e.id)
    }

    // hers ----------------------------------------------------------------------------------------

    /// What `about` names: tree paths (or dir prefixes) to filter by.
    func historyPaths(_ about: String) throws -> [String] {
        let a = about.trimmingCharacters(in: .whitespacesAndNewlines)
        switch a {
        case "想成為的樣子", "ideal": return ["data/ideal-self.md", "data/ideal-self-log.json"]
        case "目標", "目標樹", "goal", "goals": return ["data/goals.json"]
        case "工具", "tool", "tools": return ["data/tools/"]
        case "記憶", "memory": return ["memory/"]
        case "紀錄", "record": return ["data/record.json", "data/memory-fixes.json"]
        default: break
        }
        if OwnTools.validName(a) == nil, try toolStore.get(a) != nil || history.count(paths: ["data/tools/\(a).json"]) > 0 {
            return ["data/tools/\(a).json"]
        }
        if let (folder, _) = try memory.file(a) { return ["memory/\(memory.dirName(folder))/\(a).md"] }
        throw ToolError(description: "about 給：想成為的樣子、目標、工具、記憶、紀錄，或一個工具的名字、一條記憶的 id；不給就是全部")
    }

    /// `history.list`: her recent changes, short. What a folder her turn may not read holds is only counted.
    func historyListTool(about: String?, seen events: [Event]) throws -> Outcome {
        let paths = try about.map(historyPaths) ?? []
        let hidden = hiddenFolders(Self.audience(events))
        let list = try history.log(limit: historyConfig.listItems, paths: paths)
        let total = try history.count(paths: paths)
        guard !list.isEmpty else { return Outcome(text: "沒有改動紀錄" + (about.map { "（\($0)）" } ?? "") + "。", actions: ["history list"]) }
        var lines = ["改動紀錄（新的在前，這裡 \(list.count) 件，全部 \(total) 件；要退回你自己的改動，用 \(Tools.Name.historyUndo) 給〔〕裡的編號）："]
        for e in list {
            let hides = e.paths.contains { history.memoryFolder($0).map(hidden.contains) ?? false }
            var l = "〔\(e.short)〕\(Self.historyTime(e.ts)) \(e.who.words)"
            if hides {
                l += "改了你現在看不到的記憶（主人的回合才看得到）"
            } else {
                l += " \(e.what)" + (e.task.map { "（\($0)）" } ?? "")
                // No reason given (goal.add, remember… have none): what she wrote, so that seven
                // 「goal.add｜動到：目標樹」 in a row can be told apart (m10 a, 2026-11-19).
                if !e.why.isEmpty { l += "：\(e.why.prefix(60))" } else if let a = e.args, !a.isEmpty { l += "（\(a.prefix(60))）" }
                l += "｜動到：\(history.things(e.paths, hidden: hidden))"
            }
            lines.append(l)
        }
        return Outcome(text: lines.joined(separator: "\n"), actions: ["history list"])
    }

    /// `history.undo`: one change of hers back, saying why. The owner's, the maintenance's and the
    /// program's changes are not hers to undo.
    func historyUndoTool(id: String, why: String, seen events: [Event]) throws -> Outcome {
        func refuse(_ s: String) -> ToolError { ToolError(description: "沒有退回：" + s) }
        let notFound = "沒有編號是 \(id) 的改動；編號照抄 \(Tools.Name.historyList) 結果裡〔〕那一串。"
        guard let e = try? history.entry(id) else { throw refuse(notFound) }
        let hidden = hiddenFolders(Self.audience(events))
        guard !e.paths.contains(where: { history.memoryFolder($0).map(hidden.contains) ?? false }) else { throw refuse(notFound) }
        switch e.who {
        case .her: break
        case .owner: throw refuse("〔\(e.short)〕是主人改的，你不能退回。覺得不對，跟主人說。")
        case .maintenance: throw refuse("〔\(e.short)〕是系統維護改的（\(e.what)），你不能退回。覺得不對，跟主人說。")
        case .program: throw refuse("〔\(e.short)〕是程式記的，不是你的改動，不能退回。")
        }
        let w = why.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !w.isEmpty else { throw refuse("要給 why：為什麼退回，一句。") }
        let paths: [String]
        do { paths = try history.undoWrites(e.id) } catch let h as HistoryError { throw refuse(h.description + "。") }
        try reloadAfterHistory(paths)
        let failed = commitHistory(.her, what: "\(Tools.Name.historyUndo)〔\(e.short)〕「\(e.what)」", why: w, undoes: e.id)
        log("INFO", "turn \(turnIndex): history.undo \(e.short) (\(e.what)): \(paths)")
        return Outcome(text: "退回了：〔\(e.short)〕\(Self.historyTime(e.ts)) 你的「\(e.what)」。\(history.things(paths, hidden: hidden)) 回到那之前的樣子。"
                       + historyAfterWords(paths) + "主人在儀表板看得到。" + (failed.map { "（退回生效了，但沒記進改動紀錄：\($0)）" } ?? ""),
                       secret: touchesSecret(paths), actions: ["history undo"])
    }
}
