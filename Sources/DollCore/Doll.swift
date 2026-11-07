import Foundation

/// The skeleton: one queue, one consumer, one timeline, one line (the owner's).
/// The actor is the single serial owner of Doll state; everything durable is in `Store`.
///
/// A turn = one model call. The loop only takes a turn when the queue holds an event
/// (owner voice, owner text, a command's result); with an empty queue it sleeps and
/// makes no model call.

/// One sentence for the owner.
public typealias Say = @Sendable (String) -> Void

/// What one turn did; for logs, tests and the smoke run.
public struct TurnReport: Sendable {
    public var index: Int
    public var actions: [String]       // "ran" | "job <id>" | "think <level>" | "said"
    public var reply: String
    public var footer: String
    public var effort: String
    public var usage: ModelUsage
    public var usedTool: Bool
}

enum Log {
    static func write(_ level: String, _ msg: String) {
        let c = Calendar.current.dateComponents([.year, .month, .day, .hour, .minute, .second], from: Date())
        let ts = String(format: "%04d-%02d-%02d %02d:%02d:%02d", c.year!, c.month!, c.day!, c.hour!, c.minute!, c.second!)
        FileHandle.standardError.write(Data("\(ts) \(level) \(msg)\n".utf8))
    }
}

private func clock(_ ts: Double) -> String {
    let c = Calendar.current.dateComponents([.month, .day, .hour, .minute, .second], from: Date(timeIntervalSince1970: ts))
    return String(format: "%02d-%02d %02d:%02d:%02d", c.month!, c.day!, c.hour!, c.minute!, c.second!)
}

private func hex(_ n: Int) -> String {
    String(UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased().prefix(n))
}

struct RunningJob {
    let command: String
    let started: Double
    let handle: any ExecHandle
}

public actor Doll {
    public nonisolated let name: String
    let identity: String
    let core: CoreConfig
    let llm: LlmConfig
    let shell: ShellConfig
    let idle: IdleConfig
    public nonisolated let scratch: URL
    let store: Store
    let model: any MainModel
    let executor: any Executor
    let say: Say

    var queue: [Event] = []
    var waiter: CheckedContinuation<Bool, Never>?
    var stopped = false
    var inflight: Task<ModelReply, any Error>?
    var running: [String: RunningJob] = [:]
    var turnIndex = 0
    /// Consecutive turns without a tool call.
    var idleTurns = 0
    /// Set by 【我想一下】; consumed by the next model call.
    var effortNext: String?
    var observer: (@Sendable (TurnReport) -> Void)?
    /// Tests read her log lines here; stderr gets them regardless.
    var logSink: (@Sendable (String) -> Void)?
    /// Last real prompt size versus our estimate of the same prompt, to scale the footer's context figure.
    var tokenRatio = 1.0

    public init(config: Config, root: URL, model: any MainModel, executor: (any Executor)? = nil,
                say: @escaping Say) throws {
        core = config.core
        llm = config.llm
        shell = config.shell
        idle = config.idle
        let data = core.dataDir.hasPrefix("/") ? URL(filePath: core.dataDir) : root.appending(path: core.dataDir)
        scratch = URL(filePath: (shell.scratchDir as NSString).expandingTildeInPath)
        try FileManager.default.createDirectory(at: scratch, withIntermediateDirectories: true)
        store = try Store(path: data.appending(path: "doll.db"))
        self.model = model
        self.executor = executor ?? LocalShell(cwd: scratch.path, hardTimeoutS: shell.timeoutS)
        self.say = say

        let packPath = root.appending(path: core.pack)
        let pack = try TOML.parse(String(contentsOf: packPath, encoding: .utf8))
        guard case .string(let name)? = pack["meta"]?["name"] else {
            throw TOMLError(description: "\(packPath.path): [meta].name must be a string")
        }
        self.name = name
        identity = try ["self", "personality", "taboos"].map { key in
            guard case .string(let s)? = pack["identity"]?[key] else {
                throw TOMLError(description: "\(packPath.path): [identity].\(key) must be a string")
            }
            return s.trimmingCharacters(in: .whitespacesAndNewlines)
        }.joined(separator: "\n\n")
    }

    public func setObserver(_ f: (@Sendable (TurnReport) -> Void)?) { observer = f }
    public func setLogSink(_ f: (@Sendable (String) -> Void)?) { logSink = f }

    func log(_ level: String, _ msg: String) {
        Log.write(level, msg)
        logSink?("\(level) \(msg)")
    }

    // boot -------------------------------------------------------------------
    /// Checks the model (unreachable = error, no fallback) and kills jobs that outlived the
    /// previous process. M2 reattaches them instead.
    public func boot() async throws {
        try await model.check()
        for job in try store.runningJobs() {
            if let pgid = job.pgid { LocalShell.killGroup(pgid) }
            try store.finishJob(job.id, "lost")
            log("WARN", "job \(job.id) (\(job.command)) was running when the previous process died; killed, not rerun")
        }
    }

    // inputs -----------------------------------------------------------------
    func put(_ ev: Event) {
        queue.append(ev)
        if let w = waiter {
            waiter = nil
            w.resume(returning: true)
        }
    }

    /// Owner voice is the only input that cancels a model call in flight (3.1); the event is
    /// queued first so the rerun sees it.
    public func ownerSaid(_ text: String, secret: Bool = false, voice: Bool = false) {
        put(Event(.owner, .owner, text, secret: secret, voice: voice))
        if voice { inflight?.cancel() }
    }

    /// Ends `run()` after the current turn.
    public func stop() {
        stopped = true
        inflight?.cancel()
        waiter?.resume(returning: false)
        waiter = nil
    }

    /// Sleeps until an event arrives (true) or `stop()` (false). No timer: nothing wakes her but events.
    func waitForEvent() async -> Bool {
        if !queue.isEmpty { return true }
        if stopped { return false }
        return await withCheckedContinuation { waiter = $0 }
    }

    // consumer ---------------------------------------------------------------
    public func run() async throws {
        while !stopped {
            guard await waitForEvent() else { break }
            try drain()
            try await turn()
        }
        try shutdown()
    }

    /// Queued events enter the timeline, highest priority first (`[core] priority`).
    func drain() throws {
        guard !queue.isEmpty else { return }
        let rank = Dictionary(uniqueKeysWithValues: core.priority.enumerated().map { ($1, $0) })
        let batch = queue.enumerated().sorted {
            (rank[$0.element.priority.rawValue]!, $0.offset) < (rank[$1.element.priority.rawValue]!, $1.offset)
        }.map(\.element)
        queue.removeAll()
        for ev in batch { try store.append(ev) }
    }

    func shutdown() throws {
        for (id, job) in running {
            job.handle.cancel()
            try store.finishJob(id, "cancelled")
        }
        running = [:]
    }

    // prompt -----------------------------------------------------------------
    func systemPrompt() -> String {
        "你是\(name)。\n\n\(identity)\n\n" + Instructions.text(name: name, shell: shell)
    }

    /// One event as prompt messages. Her replies carry the footer they were answering, so the
    /// prompt prefix is byte-stable turn to turn (the server caches from the last assistant turn).
    func render(_ ev: Event) -> [ChatMessage] {
        let t = clock(ev.ts)
        switch ev.kind {
        case .doll:
            return (ev.footer.map { [ChatMessage(role: "user", content: $0)] } ?? []) + [ChatMessage(role: "assistant", content: ev.text)]
        case .owner: return [ChatMessage(role: "user", content: "[\(t) 主人] \(ev.text)")]
        case .result: return [ChatMessage(role: "user", content: "[\(t) 結果]\n\(ev.text)")]
        case .job: return [ChatMessage(role: "user", content: "[\(t) job 跑完]\n\(ev.text)")]
        }
    }

    func seconds(_ d: Double) -> String { "\(Int(d.rounded())) 秒" }

    /// The facts footer: running jobs, context length, effort for the next call.
    public func footer(contextTokens: Int) -> String {
        let now = Date().timeIntervalSince1970
        var out = ["【狀況】", "- 現在 \(clock(now))"]
        let jobs = running.sorted { $0.value.started < $1.value.started }
        if jobs.isEmpty {
            out.append("- 背景 job：沒有")
        }
        for (id, j) in jobs {
            out.append("- 背景 job：\(id)（\(j.command)）跑了 \(seconds(now - j.started))")
        }
        out.append("- context 約 \(contextTokens) token；下一回合 effort：\(effortNext ?? llm.defaultEffort)")
        return out.joined(separator: "\n")
    }

    /// `status` in the CLI.
    public func status() throws -> String {
        footer(contextTokens: Int(Double(try estimateContext()) * tokenRatio))
    }

    func estimateContext() throws -> Int {
        let msgs = [ChatMessage(role: "system", content: systemPrompt())] + (try store.events().flatMap(render))
        return msgs.reduce(0) { $0 + estimateTokens($1.content) + 4 }
    }

    // one turn ---------------------------------------------------------------
    /// Drain the queue and take one turn; what `run()` does per event.
    func runOnce() async throws {
        try drain()
        try await turn()
    }

    /// One model call over the whole timeline. Owner voice may cancel the call in flight; the
    /// step then restarts from scratch with the queue drained again, so the voice is in it.
    func turn() async throws {
        turnIndex += 1
        let events = try store.events()
        var messages = [ChatMessage(role: "system", content: systemPrompt())] + events.flatMap(render)
        let estimate = messages.reduce(0) { $0 + estimateTokens($1.content) + 4 }
        let footerText = footer(contextTokens: Int(Double(estimate) * tokenRatio))
        messages.append(ChatMessage(role: "user", content: footerText))
        let effort = effortNext ?? llm.defaultEffort

        let task = Task { try await model.chat(messages, effort: effort) }
        inflight = task
        let reply: ModelReply
        do {
            reply = try await task.value
            inflight = nil
        } catch is CancellationError {
            inflight = nil
            if stopped { throw CancellationError() }
            log("INFO", "turn \(turnIndex) interrupted; rerunning with the new events")
            turnIndex -= 1
            try drain()
            return try await turn()
        }
        effortNext = nil
        tokenRatio = Double(reply.usage.promptTokens) / Double(max(1, estimate))
        let r = Reply.parse(reply.content)
        var actions: [String] = []

        // She saw every event, so a secret seen marks her reply (5.1).
        let stored = try store.append(Event(.doll, .internal, reply.content, secret: events.contains(where: \.secret)))
        try store.attachFooter(stored.seq!, footerText)

        if let level = r.think {
            if Effort.levels.contains(level), level != "none" {
                effortNext = level
                actions.append("think \(level)")
            } else {
                log("WARN", "turn \(turnIndex): 【我想一下】\(level) ignored; only medium or high")
            }
        }
        if let cmd = r.command {
            actions.append(try await runCommand(cmd))
        }
        if !r.speech.isEmpty {
            say(r.speech)
            actions.append("said")
        }

        if r.usedTool {
            idleTurns = 0
        } else {
            idleTurns += 1
            if idleTurns >= idle.maxTurnsWithoutTool {
                log("INFO", "idle: \(idleTurns) turns in a row without a tool call")
                idleTurns = 0
            }
        }

        let report = TurnReport(index: turnIndex, actions: actions, reply: reply.content, footer: footerText,
                                effort: effort, usage: reply.usage, usedTool: r.usedTool)
        log("INFO", "turn \(turnIndex) effort=\(effort) actions=\(actions) prompt=\(reply.usage.promptTokens) "
            + "prefilled=\(reply.usage.prefilled.map(String.init) ?? "?") cached=\(reply.usage.cached.map(String.init) ?? "?")")
        observer?(report)
    }

    // commands ---------------------------------------------------------------
    /// Short: the result is queued at once (her next turn). Long: a background job whose end is queued later.
    func runCommand(_ cmd: String) async throws -> String {
        let handle = try executor.start(cmd)
        if let r = await handle.wait(shell.waitS) {
            put(Event(.result, .internal, "$ \(cmd)\n" + show(r)))
            return "ran"
        }
        let id = "j" + hex(6)
        try store.addJob(id, command: cmd, pgid: handle.pgid)
        running[id] = RunningJob(command: cmd, started: Date().timeIntervalSince1970, handle: handle)
        // Not an event: she learns it went background from the timeline when the next event comes.
        try store.append(Event(.result, .internal,
            "$ \(cmd)\n（超過 \(Int(shell.waitS)) 秒還沒跑完，轉背景 job \(id)；跑完會變一個事件回來）"))
        Task { self.jobFinished(id, await handle.finish()) }
        return "job \(id)"
    }

    /// The tail of the output plus the exit code.
    func show(_ r: ExecResult) -> String {
        let cap = shell.resultTailChars
        var text = r.output
        if text.unicodeScalars.count > cap {
            text = "…[前面截掉了]\n" + String(String.UnicodeScalarView(text.unicodeScalars.suffix(cap)))
        }
        if r.timedOut { return text + "\n(超過 \(Int(shell.timeoutS)) 秒，被砍掉)" }
        return text + (text.isEmpty || text.hasSuffix("\n") ? "" : "\n") + "(exit \(r.exitCode))"
    }

    func jobFinished(_ id: String, _ r: ExecResult) {
        guard let job = running.removeValue(forKey: id) else { return }
        do {
            try store.finishJob(id, r.timedOut ? "timeout" : (r.exitCode == 0 ? "ok" : (r.exitCode < 0 ? "cancelled" : "failed")))
        } catch {
            log("ERROR", "job \(id) bookkeeping failed: \(error)")
        }
        put(Event(.job, .internal, "\(id)（\(job.command)）\n" + show(r)))
    }
}
