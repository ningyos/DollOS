import Foundation
import Synchronization

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

/// Resumes a continuation exactly once, from whichever side gets there first.
private final class Once<T: Sendable>: Sendable {
    private let slot: Mutex<CheckedContinuation<T, Never>?>
    init(_ c: CheckedContinuation<T, Never>) { slot = Mutex(c) }
    func resume(_ value: T) { slot.withLock { $0.take() }?.resume(returning: value) }
}

/// The task's result, or nil if it has not finished within `seconds` (the task keeps running).
func finished<T: Sendable>(_ task: Task<T, any Error>, within seconds: Double) async -> Result<T, any Error>? {
    await withCheckedContinuation { (c: CheckedContinuation<Result<T, any Error>?, Never>) in
        let once = Once(c)
        Task { once.resume(await task.result) }
        Task {
            try? await Task.sleep(for: .seconds(seconds))
            once.resume(nil)
        }
    }
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
    let decision: any DecisionModel
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
    /// Owner events drained since the last completed turn: the input of this turn's extraction.
    var turnOwner: [Event] = []
    /// The extraction started for the turn in progress (runs beside its model call).
    var extracting: Task<[Candidate], any Error>?
    /// The extraction of the last completed turn; the next turn collects it for its footer.
    var lastExtraction: (turn: Int, task: Task<[Candidate], any Error>)?
    /// Candidates in this turn's footer. Shown once, then dropped (spec 6, 2026-11-08).
    var proposals: [Candidate] = []

    public init(config: Config, root: URL, model: any MainModel, decision: any DecisionModel,
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
        self.decision = decision
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
        // Apple FM judges every 【記】's kind and proposes candidates; unavailable = error (spec 9.1).
        try await decision.check()
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
        for ev in batch {
            let stored = try store.append(ev)
            if stored.kind == .owner { turnOwner.append(stored) }
        }
    }

    func shutdown() throws {
        extracting?.cancel()
        lastExtraction?.task.cancel()
        for (id, job) in running {
            job.handle.cancel()
            try store.finishJob(id, "cancelled")
        }
        running = [:]
    }

    // prompt -----------------------------------------------------------------
    func systemPrompt() -> String {
        "你是\(name)。\n\n\(identity)\n\n" + Instructions.text(name: name, shell: shell, memory: memoryConfig)
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
        if !proposals.isEmpty {
            out.append("【可能值得記】（從主人上一回合的話抽出來的；要記就自己寫【記】，不理就算了）")
            for c in proposals { out.append("- \(c.text)") }
        }
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
        // Last turn's extraction goes into this footer; waited for only up to candidate_wait_s.
        if let last = lastExtraction {
            lastExtraction = nil
            proposals = await collect(last.task, from: last.turn)
        }
        // This turn's: beside the model call, never before it (spec 6). A rerun after owner
        // voice restarts it over every owner event of the turn.
        if !turnOwner.isEmpty {
            extracting?.cancel()
            let utterances = turnOwner.map { Utterance(seq: $0.seq!, text: $0.text) }
            let decision = decision, max = memoryConfig.maxCandidatesPerTurn
            extracting = Task.detached { try await decision.extractCandidates(utterances, max: max) }
        }
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
        lastExtraction = extracting.map { (turnIndex, $0) }
        extracting = nil
        turnOwner = []
        proposals = []
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
        // 【記】：the program fills seq, ts, confidence, kind (spec 6). The tier comes from
        // the event she is answering, not from anything she can claim. Her own tool
        // acknowledgements are not sources.
        for note in r.notes {
            let programAck: (Event) -> Bool = { e in
                e.kind == .result && (e.text.hasPrefix("【記】") || e.text.hasPrefix("【想】"))
            }
            let source = events.last { $0.kind != .doll && !programAck($0) }
            let tier: SourceTier = switch source?.kind {
            case .owner?: .owner
            case .result?, .job?: .tool
            default: .doll
            }
            let seq = source?.seq ?? stored.seq!
            let sourceText = switch source?.kind {
            case .owner?: "[主人] \(source!.text)"
            case .result?, .job?: "[工具結果] \(source!.text)"
            default: "[我說] \(r.speech)"
            }
            do {
                let judged = try await decision.judgeKind(source: sourceText, memory: note.text)
                log("INFO", "【記】kind=\(judged.kind.rawValue) (\(judged.reason))")
                let id = try memory.write(note.text, secret: note.secret, sourceSeq: seq, tier: tier, kind: judged.kind)
                put(Event(.result, .internal, "【記】已存入 \(id)（\(note.secret ? memoryConfig.secretDir : memoryConfig.publicDir)/\(id).md，confidence \(memoryConfig.confidence[tier]!)，kind \(judged.kind.rawValue)）"))
                actions.append("note \(note.secret ? "secret" : "public")")
            } catch {
                log("ERROR", "【記】failed: \(error)")
                put(Event(.result, .internal, "【記】失败了：\(error)"))
            }
        }
        // 【想】：same secret rule as her replies (5.1): a turn sees secrets only if it saw secret events.
        if let query = r.query {
            let secretVisible = events.contains(where: \.secret)
            do {
                let hits = try memory.search(query, secretVisible: secretVisible)
                var out = "【想】\(query)：\(hits.count) 條"
                for hit in hits {
                    out += "\n\n〔\(hit.label)｜\(hit.folder)〕\n\(hit.text)"
                }
                // A hit from secret/ makes the result a secret event (5.1: what the turn saw is what it may say).
                put(Event(.result, .internal, out, secret: hits.contains(where: \.secret)))
                actions.append("search \(hits.count)")
            } catch {
                log("ERROR", "【想】failed: \(error)")
                put(Event(.result, .internal, "【想】失败了：\(error)"))
            }
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

    // candidates -------------------------------------------------------------
    /// The candidates of turn `from`, if its extraction finishes within `candidate_wait_s`.
    /// A miss is logged and skipped: a lost proposal, not a fallback.
    func collect(_ task: Task<[Candidate], any Error>, from: Int) async -> [Candidate] {
        let wait = memoryConfig.candidateWaitS
        switch await finished(task, within: wait) {
        case .success(let found)?:
            log("INFO", "candidates from turn \(from): "
                + (found.isEmpty ? "none" : found.map { "#\($0.sourceSeq) \($0.text)" }.joined(separator: " / ")))
            return found
        case .failure(let error)?:
            log("ERROR", "candidate extraction for turn \(from) failed: \(error)")
            return []
        case nil:
            task.cancel()
            log("WARN", "candidate extraction for turn \(from) not done after \(wait) s; skipped")
            return []
        }
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
