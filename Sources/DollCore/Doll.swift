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
    public var actions: [String]       // "said" | "ran" | "job <id>" | "think <level>" | "remember <folder>" | "recall <n>" | "error <tool>"
    public var reply: String
    public var toolCalls: [ToolCall]
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
    let memoryConfig: MemoryConfig
    public nonisolated let scratch: URL
    let store: Store
    let memory: Memory
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
    /// Set by `think`; consumed by the next model call.
    var effortNext: String?
    /// Her tool calls produced results she has not answered yet: she gets the next turn even with
    /// an empty queue (a background-job ack alone does not count; its job event will).
    var followUp = false
    /// Her tools, built once; the system prompt lists them, so it never changes between turns.
    let tools: [Tools.Spec]
    var observer: (@Sendable (TurnReport) -> Void)?
    /// Tests read her log lines here; stderr gets them regardless.
    var logSink: (@Sendable (String) -> Void)?
    /// Last real prompt size versus our estimate of the same prompt, to scale the footer's context figure.
    var tokenRatio = 1.0

    public init(config: Config, root: URL, model: any MainModel,
                executor: (any Executor)? = nil,
                say: @escaping Say) throws {
        core = config.core
        llm = config.llm
        shell = config.shell
        idle = config.idle
        memoryConfig = config.memory
        let data = core.dataDir.hasPrefix("/") ? URL(filePath: core.dataDir) : root.appending(path: core.dataDir)
        scratch = URL(filePath: (shell.scratchDir as NSString).expandingTildeInPath)
        try FileManager.default.createDirectory(at: scratch, withIntermediateDirectories: true)
        store = try Store(path: data.appending(path: "doll.db"))
        // No fallback: a missing embedding model is an error, the same as an unreachable Strata (spec 3.1).
        let memRoot = memoryConfig.dir.hasPrefix("/") ? URL(filePath: memoryConfig.dir) : root.appending(path: memoryConfig.dir)
        memory = try Memory(config: memoryConfig, root: memRoot, store: store)
        self.model = model
        self.executor = executor ?? LocalShell(cwd: scratch.path, hardTimeoutS: shell.timeoutS)
        self.say = say
        tools = Tools.specs(shell: shell, memory: memoryConfig)

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

    /// Rebuilds the memory index from the markdown files (spec 6: SQLite is a rebuildable index).
    public func rebuildMemory() throws { try memory.rebuild() }

    func log(_ level: String, _ msg: String) {
        Log.write(level, msg)
        logSink?("\(level) \(msg)")
    }

    // boot -------------------------------------------------------------------
    /// Checks the model (unreachable = error, no fallback) and kills jobs that outlived the
    /// previous process. M2 reattaches them instead.
    public func boot() async throws {
        // Memories written by a previous process exist only as markdown until indexed (spec 6).
        try memory.syncOnBoot()
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

    /// Sleeps until an event arrives or her tool results await her (true), or `stop()` (false).
    /// No timer: nothing else wakes her.
    func waitForEvent() async -> Bool {
        if !queue.isEmpty || followUp { return true }
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
        for ev in batch {
            try store.append(ev)
        }
    }

    func shutdown() throws {
        for (id, job) in running {
            job.handle.cancel()
            try store.finishJob(id, "cancelled")
        }
        running = [:]
    }

    // prompt -----------------------------------------------------------------
    /// Fixed for the life of the process: the template's tool block, then the persona. This is the
    /// same text the template would render from `tools` plus a system message, so the prompt opens
    /// the way she was trained on.
    func systemPrompt() -> String {
        Tools.block(tools) + "\n\n你是\(name)。\n\n\(identity)\n\n" + Instructions.text
    }

    /// One event as prompt messages. Her replies carry the footer they were answering and her raw
    /// content (tool call text included), so the prompt prefix is byte-stable turn to turn (the server
    /// caches from the last assistant turn). Tool results follow her reply directly, in call order;
    /// the template wraps them in `<tool_response>`.
    func render(_ ev: Event) -> [ChatMessage] {
        let t = clock(ev.ts)
        switch ev.kind {
        case .doll:
            return (ev.footer.map { [ChatMessage(role: "user", content: $0)] } ?? [])
                + [ChatMessage(role: "assistant", content: ev.text)]
        case .owner: return [ChatMessage(role: "user", content: "[\(t) 主人] \(ev.text)")]
        case .tool: return [ChatMessage(role: "tool", content: ev.text)]
        case .job: return [ChatMessage(role: "user", content: "[\(t) job 跑完]\n\(ev.text)")]
        }
    }

    func estimate(_ msgs: [ChatMessage]) -> Int {
        msgs.reduce(0) { $0 + estimateTokens($1.content) + 4 }
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
        estimate([ChatMessage(role: "system", content: systemPrompt())] + (try store.events().flatMap(render)))
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
        followUp = false
        let events = try store.events()
        var messages = [ChatMessage(role: "system", content: systemPrompt())] + events.flatMap(render)
        let guess = estimate(messages)
        let footerText = footer(contextTokens: Int(Double(guess) * tokenRatio))
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
        tokenRatio = Double(reply.usage.promptTokens) / Double(max(1, guess))
        var actions: [String] = []

        // She saw every event, so a secret seen marks her reply (5.1). Stored raw: re-sent byte-identical.
        let stored = try store.append(Event(.doll, .internal, reply.content, secret: events.contains(where: \.secret)))
        try store.attachFooter(stored.seq!, footerText)

        let parsed = ParsedReply(reply.content)
        if !parsed.speech.isEmpty {
            say(parsed.speech)
            actions.append("said")
        }
        // Every call, in the order she wrote it, gets exactly one tool result right after her reply
        // (spec 6, 2026-11-08); a malformed or unknown one gets a result saying what was wrong.
        for call in parsed.calls {
            let out = await execute(call, seen: events, reply: stored)
            try store.append(Event(.tool, .internal, out.text, secret: out.secret))
            actions += out.actions
            if !out.background { followUp = true }
        }

        let usedTool = !parsed.calls.isEmpty
        if usedTool {
            idleTurns = 0
        } else {
            idleTurns += 1
            if idleTurns >= idle.maxTurnsWithoutTool {
                log("INFO", "idle: \(idleTurns) turns in a row without a tool call")
                idleTurns = 0
            }
        }

        let report = TurnReport(index: turnIndex, actions: actions, reply: reply.content, toolCalls: parsed.calls,
                                footer: footerText, effort: effort, usage: reply.usage, usedTool: usedTool)
        log("INFO", "turn \(turnIndex) effort=\(effort) actions=\(actions) prompt=\(reply.usage.promptTokens) "
            + "prefilled=\(reply.usage.prefilled.map(String.init) ?? "?") cached=\(reply.usage.cached.map(String.init) ?? "?")")
        observer?(report)
    }

    // tools ------------------------------------------------------------------
    /// One call's result: the tool message text, whether it carries a secret, the report actions,
    /// and whether it only says the command went background (no next turn for that alone).
    struct Outcome {
        var text: String
        var secret = false
        var actions: [String]
        var background = false
    }

    /// Runs one tool call. Every failure becomes the call's result text; nothing here throws.
    func execute(_ call: ToolCall, seen events: [Event], reply: Event) async -> Outcome {
        let name = call.name.isEmpty ? "tool_call" : call.name
        do {
            switch try Tools.decode(call, specs: tools) {
            case .shell(let cmd):
                return try await runCommand(cmd)
            case .think(let level):
                effortNext = level
                return Outcome(text: "下一回合 effort：\(level)", actions: ["think \(level)"])
            case .remember(let items):
                return remember(items, seen: events, reply: reply)
            case .recall(let query):
                return try recall(query, seen: events)
            }
        } catch {
            log("ERROR", "turn \(turnIndex): \(name) failed: \(error)")
            return Outcome(text: "失敗：\(error)", actions: ["error \(name)"])
        }
    }

    /// The program fills seq, ts, confidence; the kind is hers (spec 6, 2026-11-08). The tier comes
    /// from the event she is answering, not from anything she can claim; results of her own memory
    /// and think calls are not sources.
    func remember(_ items: [Tools.Request.Item], seen events: [Event], reply: Event) -> Outcome {
        let names = Self.callNames(events)
        let source = events.last { e in
            e.kind != .doll && !(e.kind == .tool && names[e.seq!] != "shell")
        }
        let tier: SourceTier = switch source?.kind {
        case .owner?: .owner
        case .tool?, .job?: .tool
        default: .doll
        }
        // With no source event she is the source: the reply this call belongs to.
        let seq = source?.seq ?? reply.seq!
        var lines: [String] = [], actions: [String] = []
        for (i, item) in items.enumerated() {
            let folder = item.secret ? memoryConfig.secretDir : memoryConfig.publicDir
            do {
                if let bad = item.badKind {
                    throw ToolError(description: "kind 只能是 happened、intended、uncertain，或不填；收到 \(bad)")
                }
                let id = try memory.write(item.text, secret: item.secret, sourceSeq: seq, tier: tier, kind: item.kind)
                let kind = item.kind.map { "，kind \($0.rawValue)" } ?? ""
                lines.append("第 \(i + 1) 條已存入 \(folder)/\(id).md（confidence \(memoryConfig.confidence[tier]!)\(kind)）")
                actions.append("remember \(folder)")
            } catch {
                log("ERROR", "remember item \(i + 1) failed: \(error)")
                lines.append("第 \(i + 1) 條沒存：\(error)")
                actions.append("error remember")
            }
        }
        return Outcome(text: lines.joined(separator: "\n"), actions: actions)
    }

    /// The tool each result answers, by the result's seq: her reply's calls, read back from its raw
    /// text, matched in order to the tool results that follow it.
    static func callNames(_ events: [Event]) -> [Int64: String] {
        var out: [Int64: String] = [:]
        var pending: ArraySlice<ToolCall> = []
        for e in events {
            switch e.kind {
            case .doll: pending = ParsedReply(e.text).calls[...]
            case .tool: if let call = pending.popFirst() { out[e.seq!] = call.name }
            case .owner, .job: break
            }
        }
        return out
    }

    /// Same secret rule as her replies (5.1): a turn sees secrets only if it saw secret events.
    func recall(_ query: String, seen events: [Event]) throws -> Outcome {
        let hits = try memory.search(query, secretVisible: events.contains(where: \.secret))
        var out = "\(query)：\(hits.count) 條"
        for hit in hits {
            out += "\n\n〔\(hit.label)｜\(hit.folder)〕\n\(hit.text)"
        }
        // A hit from secret/ makes the result a secret event (5.1: what the turn saw is what it may say).
        return Outcome(text: out, secret: hits.contains(where: \.secret), actions: ["recall \(hits.count)"])
    }

    // commands ---------------------------------------------------------------
    /// Short: the output is the call's result. Long: a background job whose end is queued later as a job event.
    func runCommand(_ cmd: String) async throws -> Outcome {
        let handle = try executor.start(cmd)
        if let r = await handle.wait(shell.waitS) {
            return Outcome(text: "$ \(cmd)\n" + show(r), actions: ["ran"])
        }
        let id = "j" + hex(6)
        try store.addJob(id, command: cmd, pgid: handle.pgid)
        running[id] = RunningJob(command: cmd, started: Date().timeIntervalSince1970, handle: handle)
        Task { self.jobFinished(id, await handle.finish()) }
        return Outcome(text: "$ \(cmd)\n（超過 \(Int(shell.waitS)) 秒還沒跑完，轉背景 job \(id)；跑完會變一個事件回來）",
                       actions: ["job \(id)"], background: true)
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
