import Foundation

/// The skeleton: one queue, one consumer, one timeline, one line (the owner's).
/// The actor is the single serial owner of Doll state; everything durable is in the timeline
/// files (`Timeline`) and the memory files, SQLite only holds their indexes.
///
/// A turn = one model call. The loop only takes a turn when the queue holds an event
/// (owner voice, owner text, a watch wake) or her tool results await her; otherwise it sleeps
/// and makes no model call.

/// Delivers one text of hers to a person's stream (the owner's Discord DM through the bridge, the
/// developer's `dollctl chat`): returns nil, or says why it could not be delivered (her
/// `discord.dm` / `dollctl.reply` result then tells her so).
public typealias Deliver = @Sendable (String, Addressee) -> String?

/// What one turn did; for logs, tests and the smoke run.
public struct TurnReport: Sendable {
    public var index: Int
    public var actions: [String]       // "said <channel>" (discord_dm | dollctl | discord_voice, " (cut)" when the owner cut it) | "<tool>" | "think <level>" | "remember <folder>" | "recall <n>" | "error <tool>"
    public var reply: String
    public var toolCalls: [ToolCall]
    public var footer: String
    public var effort: String
    public var usage: ModelUsage
    public var usedTool: Bool
    /// The 【相關記憶】 events the program appended this turn, verbatim (spec 3.1, 2026-11-09).
    public var attached: [String]
    /// How a todo item ended this turn: 完成 / 失敗 from her `todo.done` / `todo.fail`; nil when none ended.
    public var itemEnd: String?
    /// The experience memory written this turn (an item that used the computer, done), verbatim.
    public var experience: String?
    /// Per experience attached during the item that ended this turn: how many of its steps she
    /// did in order, and what was counted (spec 3.1, 2026-11-09).
    public var follows: [String] = []
    /// The todo phase after this turn (`TodoPhase.label`).
    public var todo: String = ""
    /// The compactions recorded this turn (`Compaction.Kind` raw values), the forced one first.
    public var compacted: [String] = []
    /// The 【快捷鍵】 events the program appended this turn, verbatim (spec 3.1, 2026-11-09).
    public var cheatsheets: [String]
    /// One line per memory selection this turn (【相關記憶】 and `recall`, 2026-11-11): the trigger's
    /// names aligned, related counts by channel, prefilter, scores, what was given, seconds per stage.
    public var selections: [String] = []
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

/// The one watch she has set (a new one replaces it).
struct Watch: Sendable {
    let id: Int
    let region: Region?
    let maxWait: Double
    let task: Task<Void, Never>

    var target: String { region.map { "\($0)" } ?? "整個畫面" }
}

public actor Doll {
    public nonisolated let name: String
    let identity: String
    let core: CoreConfig
    let contextConfig: ContextConfig
    let llm: LlmConfig
    let computerConfig: ComputerConfig
    let filesConfig: FilesConfig
    let idle: IdleConfig
    let memoryConfig: MemoryConfig
    let selectConfig: SelectConfig
    /// Screenshot files; events name them, and each is re-sent byte-identical every turn.
    public nonisolated let shots: URL
    /// Today's timeline segment, `<data_dir>/timeline/<segment>.jsonl`.
    let timeline: Timeline
    /// Today's events for `recall`, in memory (2026-11-11): built from the segment at start, fed on every append.
    let today: TodayIndex
    let memory: Memory
    let model: any MainModel
    /// Apple's on-device model (tests: a fake): entities, alignment, scores.
    let selector: any SelectionModel
    /// The registry alignment and the memory selection over `memory` with `selector`.
    let pipeline: Selector
    let computer: any ComputerDriver
    let deliver: Deliver
    /// `[voice] enabled` (M6): the voice tools and the voice part of the system prompt.
    let voiceEnabled: Bool
    /// The voice channel (`setVoice`); nil while voice is disabled or not wired yet.
    var voice: (any VoicePort)?
    /// A `voice.say` was cut by the owner this turn: her later `voice.say` calls in the same reply
    /// are not spoken (she would talk over him).
    var voiceCut = false

    var queue: [Event] = []
    var waiter: CheckedContinuation<Bool, Never>?
    var stopped = false
    var inflight: Task<ModelReply, any Error>?
    var turnIndex = 0
    /// Consecutive turns without a tool call.
    var idleTurns = 0
    /// Set by `think`; consumed by the next model call.
    var effortNext: String?
    /// The next call is part of a task: the events drained since her last answered call include an
    /// owner line or a watch wake, or her tool results await her. It uses `task_turn_effort` unless
    /// she set one with `think`. Tool-result and wake turns count too (spec 3.1, 2026-11-08): at
    /// effort none, 2 of 5 M2 medium C runs stopped mid-task on a tool-result turn with one sentence.
    var taskTurn = false
    /// The effort the next model call uses.
    var nextEffort: String { effortNext ?? (taskTurn ? llm.taskTurnEffort : llm.defaultEffort) }
    /// Her tool calls produced results she has not answered yet: she gets the next turn even with
    /// an empty queue (a watch's ack alone does not count; its wake will).
    var followUp = false
    /// Every tool, built once: the request's `tools` is the same for the life of the process (the
    /// template puts it at the front of the prompt; a change would void the prefix cache).
    let tools: [Tools.Spec]
    let toolsJSON: String
    /// Where her DollOS documents come from (`[core] docs`); `syncDocs` copies them into the VM.
    let docsDir: URL
    /// Her todo list (`Todo.swift`), as the timeline last recorded it.
    var todo: TodoState
    /// The current item's actions that went through, one short line each (`Doll.step`) with the seq
    /// of the result that answered it: its action list and its experience's steps. In memory: a new
    /// process starts the list of an item still in progress empty.
    var itemSteps: [(seq: Int64, line: String)] = []
    /// The front apps her computer results showed during the current item, first seen first: an
    /// experience's entities (2026-11-11).
    var itemApps: [AppIdentity] = []
    /// The current item called a computer tool that ran.
    var itemUsedComputer = false
    /// The current item saw a call of hers other than the todo tools and `think` (a computer tool,
    /// a line to someone, remember, recall): what `todo.done` needs before it is accepted.
    var itemActed = false
    /// The compaction her todo call made this turn; recorded once the turn's results are in (its span
    /// ends at the turn's last event).
    var compacting: Compaction?
    /// Her `todo.done` / `todo.fail` / `todo.set` started an item this turn whose span begins after
    /// the turn's last event (the turn belongs to the item before it).
    var startAfterTurn = false
    /// Her `todo.wait` or `watch` this reply: the item becomes 等待中 once the reply's calls ran.
    var waitNow = false
    /// Her empty `todo.set` ended the list this reply: nothing wakes her for it.
    var listEndedNow = false
    /// Turns whose 【狀況】 showed the near-full warning while the item was in progress.
    var warnedTurns = 0
    /// This turn's item end, compactions and experience, for its report.
    var itemEndNow: String?
    var compactedNow: [String] = []
    var experienceNow: String?
    var followsNow: [String] = []
    /// Bundle id → version of the apps whose shortcuts are in her memory; read from the memory
    /// files on first use, then kept here.
    var learnedApps: [String: String]?
    /// The front app in the last computer result; nil before the first.
    var frontApp: AppIdentity?
    /// The front window's title and the focused element's name in the last computer result.
    var frontTitle: String?
    var frontFocus: String?
    /// The things the owner's line (by its seq) names, found once per line.
    var ownerMentions: (seq: Int64, list: [Mention])?
    /// A computer result this turn showed a front app other than the one before: attach memories.
    var appSwitched = false
    /// An owner line was drained since her last turn: attach memories, after forgetting what was attached.
    var ownerArrived = false
    /// The memories attached since the owner's last line; never attached twice in that span.
    var attachedIDs: Set<String> = []
    /// This turn's 【相關記憶】 events, for its report.
    var attachedNow: [String] = []
    /// This turn's selection lines, for its report.
    var selectionsNow: [String] = []
    /// Menu shortcuts per app version (`bundle id` + version), read once per process and shared by
    /// shortcut learning and the cheatsheet.
    var shortcutCache: [String: [MenuShortcut]] = [:]
    /// Bundle ids whose 【快捷鍵】 went into the timeline since the owner's last line.
    var cheatsheetSent: Set<String> = []
    /// Apps a computer result switched to this turn whose cheatsheet is still to be sent.
    var cheatsheetDue: [AppIdentity] = []
    /// This turn's 【快捷鍵】 events, for its report.
    var cheatsheetNow: [String] = []
    var watch: Watch?
    var watchSerial = 0
    /// Wakes from the current watch since her last computer action.
    var watchIdle = 0
    /// The last full screenshot's pixel size: where her screen coordinates must fall.
    var screenSize: (width: Int, height: Int)?
    /// The last window crop she was shown: where it sits on the full screenshot and its pixel size,
    /// to turn `on: window` coordinates into full-screen ones. nil when the last result had no crop.
    var cropMap: (region: Region, width: Int, height: Int)?
    /// Screenshot file → its `data:` URI, so a stored image is read and encoded once.
    var imageURIs: [String: String] = [:]
    var observer: (@Sendable (TurnReport) -> Void)?
    /// Tests read her log lines here; stderr gets them regardless.
    var logSink: (@Sendable (String) -> Void)?
    /// Last real prompt size versus our estimate of the same prompt, to scale the footer's context figure.
    var tokenRatio = 1.0
    /// The dashboard's live feed: every appended event, turn start and end, pause, resume, interrupt.
    public nonisolated let feed = ChangeFeed()
    /// Set from the dashboard (2026-11-09): no new turn starts; events keep queueing; the turn in
    /// flight finishes.
    var paused = false
    /// Set by `interrupt()`: the cancelled call ends its turn instead of rerunning.
    var interruptRequested = false
    /// When the dashboard last interrupted a call; nil before the first.
    var lastInterrupt: Double?
    /// A turn is running (model call or her tool calls).
    var inTurn = false
    /// The effort of the model call in flight.
    var inflightEffort: String?

    /// `embedder` is loaded by the caller before this (`MLXEmbedder.load`; tests: a fake): the today
    /// index embeds the segment's events here.
    public init(config: Config, root: URL, model: any MainModel, selector: any SelectionModel,
                embedder: any Embedder, computer: (any ComputerDriver)? = nil,
                deliver: @escaping Deliver) throws {
        core = config.core
        contextConfig = config.context
        llm = config.llm
        computerConfig = config.computer
        filesConfig = config.files
        idle = config.idle
        memoryConfig = config.memory
        selectConfig = config.select
        self.selector = selector
        let data = core.dataDir.hasPrefix("/") ? URL(filePath: core.dataDir) : root.appending(path: core.dataDir)
        shots = data.appending(path: "shots")
        try FileManager.default.createDirectory(at: shots, withIntermediateDirectories: true)
        timeline = try Timeline(dir: data.appending(path: "timeline"))
        let memRoot = memoryConfig.dir.hasPrefix("/") ? URL(filePath: memoryConfig.dir) : root.appending(path: memoryConfig.dir)
        memory = try Memory(config: memoryConfig, root: memRoot, store: try Store(path: data.appending(path: "doll.db")),
                            embedder: embedder)
        pipeline = Selector(memory: memory, model: selector, config: config.select)
        // Before anything is appended: every event of the segment so far.
        today = try TodayIndex(timeline, embedder: embedder)
        self.model = model
        self.computer = computer ?? VMPalDriver(config.computer)
        self.deliver = deliver
        tools = Tools.specs(memory: memoryConfig, voice: config.voice.enabled, computer: config.computer, files: config.files)
        voiceEnabled = config.voice.enabled
        toolsJSON = Tools.json(tools)
        todo = timeline.todo
        docsDir = core.docs.hasPrefix("/") ? URL(filePath: core.docs) : root.appending(path: core.docs)

        let packPath = root.appending(path: core.pack)
        let pack = try TOML.parse(String(contentsOf: packPath, encoding: .utf8))
        guard case .string(let name)? = pack["meta"]?["name"] else {
            throw TOMLError(description: "\(packPath.path): [meta].name must be a string")
        }
        self.name = name
        let parts = try ["self", "personality", "taboos"].map { key in
            guard case .string(let s)? = pack["identity"]?[key] else {
                throw TOMLError(description: "\(packPath.path): [identity].\(key) must be a string")
            }
            return s.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        // Optional example dialogues (2026-11-12): a list of traits alone made her recite them.
        var examples: [String] = []
        if let value = pack["identity"]?["examples"] {
            guard case .string(let s) = value else {
                throw TOMLError(description: "\(packPath.path): [identity].examples must be a string")
            }
            examples = [Self.examplesHeader + "\n" + s.trimmingCharacters(in: .whitespacesAndNewlines)]
        }
        identity = (parts + examples).joined(separator: "\n\n")
    }

    /// The line before a pack's example dialogues in the system prompt (fixed bytes).
    static let examplesHeader = "說話的樣子（範例，只示範語氣和長短，不是固定台詞，也不是真的發生過的對話；「我：」後面那句，真的回話時一樣要用 \(Tools.Name.dm) 這些回話的工具說出來，不然沒人聽得到）："

    public func setObserver(_ f: (@Sendable (TurnReport) -> Void)?) { observer = f }
    public func setLogSink(_ f: (@Sendable (String) -> Void)?) { logSink = f }
    /// The voice channel (`[voice] enabled`, M6), wired before `run()`.
    public func setVoice(_ v: any VoicePort) { voice = v }

    /// Rebuilds the memory index from the markdown files (spec 6: SQLite is a rebuildable index).
    public func rebuildMemory() throws { try memory.rebuild() }

    /// The whole timeline, oldest first, footers attached to her replies.
    public func timelineEvents() -> [Event] { timeline.events }

    /// Appends one event to the timeline file, then to the today index, then tells the dashboard.
    @discardableResult
    func append(_ ev: Event) throws -> Event {
        let stored = try timeline.append(ev)
        try today.add(timeline)
        feed.notify(.event)
        return stored
    }

    func log(_ level: String, _ msg: String) {
        Log.write(level, msg)
        logSink?("\(level) \(msg)")
    }

    // boot -------------------------------------------------------------------
    /// Checks the model and the on-device model (unreachable or unavailable = error, no fallback); the
    /// on-device model's check also warms it.
    public func boot() async throws {
        // Memories written by a previous process exist only as markdown until indexed (spec 6).
        try memory.syncOnBoot()
        try await model.check()
        try await selector.check()
    }

    /// Tells her this process just started (owner, 2026-11-14): a program line in the timeline, no
    /// turn of its own; she reads it with the next event. `docsFailure`: why `syncDocs` failed.
    public func announceBoot(docsFailure: String? = nil) throws {
        try append(Event(.memory, .internal, Self.bootNotice + (docsFailure.map {
            "DollOS 文件這次沒有複製進你的電腦（\($0)）；\(Self.docsVMPath) 裡可能是舊的或沒有。"
        } ?? "")))
    }
    static let bootNotice = "【開機】你剛被開機了。"

    /// Where her DollOS documents are in her computer (2026-11-14, owner): she reads them with `read_file`.
    public static let docsVMPath = "~/DollOS/docs"

    /// Copies every `.md` file of `[core] docs` into her computer at `docsVMPath`, replacing what is
    /// there (a file removed from the source stays in the VM). Returns nil, or why it failed (ERROR in
    /// the log; the boot notice tells her). Not fatal: the VM may be suspended at boot, and nothing
    /// but her reading the docs needs it.
    public func syncDocs() async -> String? {
        do {
            let names = try FileManager.default.contentsOfDirectory(atPath: docsDir.path).filter { $0.hasSuffix(".md") }.sorted()
            guard !names.isEmpty else { throw ToolError(description: "\(docsDir.path) 裡沒有 .md 檔") }
            for n in names {
                try await computer.writeFile("\(Self.docsVMPath)/\(n)", try Data(contentsOf: docsDir.appending(path: n)))
            }
            log("INFO", "docs: \(names.count) files copied from \(docsDir.path) into \(Self.docsVMPath)")
            return nil
        } catch {
            log("ERROR", "docs: copying \(docsDir.path) into \(Self.docsVMPath) failed: \(error)")
            return "\(error)"
        }
    }

    // inputs -----------------------------------------------------------------
    func put(_ ev: Event) {
        queue.append(ev)
        feed.notify(.queue)
        if let w = waiter {
            waiter = nil
            w.resume(returning: true)
        }
    }

    /// Owner voice is the only input that cancels a model call in flight (3.1); the event is
    /// queued first so the rerun sees it.
    public func ownerSaid(_ text: String, secret: Bool = false, voice: Bool = false, channel: Channel = .discordDM) {
        var ev = Event(.owner, .owner, text, secret: secret, voice: voice)
        ev.channel = channel
        put(ev)
        if voice { inflight?.cancel() }
    }

    /// The developer typed a line in `dollctl chat` (2026-11-12). Never secret; never cancels a call.
    public func developerSaid(_ text: String) {
        var ev = Event(.developer, .developer, text)
        ev.channel = .dollctl
        put(ev)
    }

    /// A program notice from the voice channel (M6): the owner joined or left, she was removed, the
    /// link dropped. Queued like a person's line, so it gives her a turn; renders as 程式.
    public func voiceNotice(_ text: String) {
        put(Event(.memory, .internal, text))
    }

    /// A person's line as she sees it (2026-11-12): time, who, where it came from, spoken or typed —
    /// `14:32 主人｜Discord 私訊｜文字`. Lines from before channels were recorded have no place part.
    static func speakerTag(_ ev: Event, time: String) -> String {
        let who = ev.speaker?.label ?? "?"
        return ([time + " " + who] + [ev.channel?.label].compactMap { $0 } + [ev.voice ? "語音" : "文字"]).joined(separator: "｜")
    }

    /// Whoever spoke to her last in `events`; the owner when nobody has. She picks where she
    /// answers herself (2026-11-14); this decides what the turn may see (`audience`) and who is
    /// shown "typing".
    public static func lastSpeaker(_ events: [Event]) -> Addressee {
        events.last { $0.speaker != nil }?.speaker ?? .owner
    }

    /// While a turn runs: whose line it follows (the local API shows that person "typing"); nil
    /// while she sleeps.
    public func answering() -> Addressee? {
        inTurn ? Self.lastSpeaker(timeline.events) : nil
    }

    /// Ends `run()` after the current turn.
    public func stop() {
        stopped = true
        inflight?.cancel()
        waiter?.resume(returning: false)
        waiter = nil
    }

    /// Stops giving turns (2026-11-09, dashboard). The turn in flight finishes; events keep queueing.
    public func pause() {
        paused = true
        log("INFO", "paused from the dashboard")
        feed.notify(.pause)
    }

    /// Turns resume: whatever queued meanwhile, or her unanswered tool results, gets the next one.
    public func resume() {
        paused = false
        log("INFO", "resumed from the dashboard")
        feed.notify(.pause)
        if let w = waiter {
            waiter = nil
            w.resume(returning: true)
        }
    }

    /// Cancels the model call in flight (2026-11-09, dashboard). Unlike owner voice, nothing reruns
    /// it: the turn ends without a reply and she sleeps until the next event. false: no call in flight.
    @discardableResult
    public func interrupt() -> Bool {
        guard let task = inflight else { return false }
        interruptRequested = true
        task.cancel()
        return true
    }

    /// Sleeps until an event arrives or her tool results await her and she is not paused (true), or
    /// `stop()` (false). No timer: nothing else wakes her.
    func waitForEvent() async -> Bool {
        while true {
            if !paused, !queue.isEmpty || followUp { return true }
            if stopped { return false }
            guard await withCheckedContinuation({ waiter = $0 }) else { return false }
        }
    }

    /// She is asleep and nothing will wake her: no queued event, no tool result she has not
    /// answered, no watch.
    public var settled: Bool { waiter != nil && queue.isEmpty && !followUp && watch == nil }

    // consumer ---------------------------------------------------------------
    public func run() async throws {
        while !stopped {
            guard await waitForEvent() else { break }
            try drain()
            try await turn()
        }
        shutdown()
    }

    /// Queued events enter the timeline, highest priority first (`[core] priority`).
    func drain() throws {
        guard !queue.isEmpty else { return }
        let rank = Dictionary(uniqueKeysWithValues: core.priority.enumerated().map { ($1, $0) })
        let batch = queue.enumerated().sorted {
            (rank[$0.element.priority.rawValue]!, $0.offset) < (rank[$1.element.priority.rawValue]!, $1.offset)
        }.map(\.element)
        queue.removeAll()
        var last: Event?
        for ev in batch {
            last = try append(ev)
        }
        if batch.contains(where: { $0.speaker != nil || $0.kind == .watch }) { taskTurn = true }
        if batch.contains(where: { $0.speaker != nil }) { ownerArrived = true }
        // Something happened: a waiting item is in progress again (its turn is this one).
        if todo.phase == .waiting, let last {
            todo.phase = .doing
            try timeline.recordTodo(todo, seq: last.seq!)
            log("INFO", "todo: 等待中 → 進行中「\(todo.current ?? "")」(event \(last.seq!))")
        }
    }

    /// An item still in progress when the program stops stays in the timeline's todo record: the next
    /// process continues it (its action list starts empty there). The experiences attached for it
    /// are scored as unknown (only logged); the watch stops.
    func shutdown() {
        if todo.phase == .doing || todo.phase == .waiting {
            log("INFO", "todo: stopping with「\(todo.current ?? "")」\(todo.phase.label)")
            scoreAttached(steps: itemSteps.map(\.line), outcome: "unknown")
        }
        stopWatch()
    }

    // prompt -----------------------------------------------------------------
    /// Fixed for the life of the process: the persona and how she works. Her tools go in the
    /// request's `tools` (`toolsJSON`); the template renders them ahead of this text.
    func systemPrompt() -> String {
        "你是\(name)。\n\n\(identity)\n\n" + Instructions.text(voice: voiceEnabled, context: contextConfig) + (voiceEnabled ? "\n\n" + Instructions.voice : "")
    }

    /// The request before the footer: the system prompt, then the timeline. Her replies carry the
    /// footer they were answering, her thought and her calls as the server returned them, so the
    /// prompt prefix is byte-stable turn to turn (the server caches from the last assistant turn).
    ///
    /// A compacted span (`Compaction`, 2026-11-14): owner and developer lines stay verbatim; each of
    /// her replies keeps only its speaking and todo calls (`survivesCompaction`; no thought, no footer;
    /// a reply with none left is not sent), followed by those calls' results. Every other event of the
    /// span (other results, screenshots, crops, watch wakes, the program's work notices) stays in the
    /// timeline but is not sent. A forced compaction's block follows the span's last event. Every
    /// compaction of an item starts at the item's start, so the one reaching furthest decides.
    func context(_ events: [Event]) throws -> [ChatMessage] {
        enum Mode { case full, reply([ToolCall]), block(Compaction) }
        let comps = timeline.compactions
        var plan: [(Event, Mode)] = []
        var keep: ArraySlice<Bool> = []   // the compacted reply's calls still owed a result: kept or not
        for ev in events {
            let seq = ev.seq!
            // A collapsed stretch of conversation: nothing of it is sent, its summary stands where it ended.
            // Each one starts at the beginning and carries the one before it, so only the newest shows.
            if let t = comps.filter({ $0.kind == .talk && seq <= $0.toSeq }).max(by: { $0.toSeq < $1.toSeq }) {
                if seq == t.toSeq { plan.append((ev, .block(t))) }
                continue
            }
            guard let c = comps.filter({ $0.kind != .talk && $0.fromSeq < seq && seq <= $0.toSeq }).max(by: { $0.toSeq < $1.toSeq }) else {
                plan.append((ev, .full))
                continue
            }
            switch ev.kind {
            case .owner, .developer: plan.append((ev, .full))
            case .doll:
                let flags = ev.calls.map(Self.survivesCompaction)
                keep = flags[...]
                let calls = zip(ev.calls, flags).filter(\.1).map(\.0)
                if !calls.isEmpty { plan.append((ev, .reply(calls))) }
            case .tool:
                if keep.popFirst() == true { plan.append((ev, .full)) }
            case .watch: break
            case .memory:
                if !Self.workNotice(ev.text) { plan.append((ev, .full)) }
            }
            if c.kind == .forced, seq == c.toSeq { plan.append((ev, .block(c))) }
        }
        // Only the last `image_turns_kept` turns keep their screenshots (spec 3.1, 2026-11-09): a turn's
        // images are the ones after her reply, up to her next; a watch wake's belong to the turn before it.
        var turnOfImage: [Int64: Int64] = [:], turns: [Int64] = [], lastReply: Int64 = 0
        for (ev, mode) in plan {
            guard case .full = mode else { continue }
            if ev.kind == .doll { lastReply = ev.seq! }
            guard ev.image != nil || ev.crop != nil else { continue }
            turnOfImage[ev.seq!] = lastReply
            if turns.last != lastReply { turns.append(lastReply) }
        }
        let kept = computerConfig.imageTurnsKept
        let withImages = Set(kept == 0 ? turns : turns.suffix(kept))
        var out = [ChatMessage(role: "system", content: systemPrompt())]
        for (ev, mode) in plan {
            switch mode {
            case .full:
                out += try render(ev, images: turnOfImage[ev.seq!].map(withImages.contains) ?? true)
            case .reply(let calls):
                out.append(ChatMessage(role: "assistant", content: "", toolCalls: calls))
            case .block(let c):
                out.append(ChatMessage(role: "user", content: "[\(clock(ev.ts)) 程式] \(c.text ?? "")"))
            }
        }
        return out
    }

    /// A call of hers that stays when its reply's span is compacted: what she said to someone, and her todo calls.
    static func survivesCompaction(_ call: ToolCall) -> Bool {
        call.malformed == nil && (Tools.speaking[call.name] != nil || Tools.todoNames.contains(call.name))
    }

    /// The program's notices about the work at hand: they leave with a compacted span (the others,
    /// such as 【開機】 and 【語音】, stay).
    static func workNotice(_ text: String) -> Bool {
        ["【相關記憶】", "【快捷鍵】", "【提醒】"].contains { text.hasPrefix($0) }
    }

    /// One event as prompt messages. Tool results follow her reply directly, in call order; the
    /// template wraps them in `<tool_response>`. A computer result goes out as one message: its
    /// text, the full-screen screenshot, the crop's line, the front window's crop, the window's
    /// outline (2026-11-09), images from the stored files' bytes. `images: false`: the screenshots
    /// are replaced by one fixed line, the rest stays.
    func render(_ ev: Event, images: Bool = true) throws -> [ChatMessage] {
        let t = clock(ev.ts)
        var extra: [ChatMessage.Extra] = []
        if images {
            if let f = ev.image { extra.append(.image(try imageURI(f))) }
            if let n = ev.cropNote { extra.append(.text(n)) }
            if let f = ev.crop { extra.append(.image(try imageURI(f))) }
        } else if ev.image != nil || ev.crop != nil {
            extra.append(.text(Self.imagesPutAway))
        }
        if let w = ev.window { extra.append(.text(w)) }
        switch ev.kind {
        case .doll:
            return (ev.footer.map { [ChatMessage(role: "user", content: $0)] } ?? [])
                + [ChatMessage(role: "assistant", content: ev.text, toolCalls: ev.calls)]
        case .owner, .developer: return [ChatMessage(role: "user", content: "[\(Self.speakerTag(ev, time: t))] \(ev.text)")]
        case .tool: return [ChatMessage(role: "tool", content: ev.text, extra: extra)]
        case .watch: return [ChatMessage(role: "user", content: "[\(t) watch] \(ev.text)", extra: extra)]
        case .memory: return [ChatMessage(role: "user", content: "[\(t) 程式] \(ev.text)")]
        }
    }

    /// What an older result says where its screenshots were (fixed bytes).
    static let imagesPutAway = "（這回合的截圖已收起；只有最近幾回合帶圖）"

    func imageURI(_ file: String) throws -> String {
        if let uri = imageURIs[file] { return uri }
        let data = try Data(contentsOf: shots.appending(path: file))
        let mime = file.hasSuffix(".png") ? "image/png" : "image/jpeg"
        let uri = "data:\(mime);base64," + data.base64EncodedString()
        imageURIs[file] = uri
        return uri
    }

    func estimate(_ msgs: [ChatMessage]) -> Int {
        msgs.reduce(0) {
            $0 + estimateTokens($1.content) + 4 + $1.extra.reduce(0) {
                switch $1 {
                case .image: $0 + computerConfig.screenshotTokens
                case .text(let t): $0 + estimateTokens(t)
                }
            }
        }
    }

    func seconds(_ d: Double) -> String { "\(Int(d.rounded())) 秒" }

    /// What the current item's span still takes in the request: the events after its start that no
    /// compaction covers, people's lines left out (estimated tokens, screenshots counted as
    /// `screenshot_tokens` each). Zero without an item.
    func processTokens() throws -> (tokens: Int, images: Int) {
        guard todo.phase == .doing || todo.phase == .waiting else { return (0, 0) }
        let start = todo.startSeq
        let covered = timeline.compactions.filter { $0.kind != .talk && $0.fromSeq == start }.map(\.toSeq).max() ?? start
        var tokens = 0, images = 0
        for ev in timeline.events where ev.seq! > covered && ev.speaker == nil {
            let msgs = try render(ev)
            tokens += estimate(msgs)
            images += msgs.reduce(0) { $0 + $1.extra.filter { if case .image = $0 { true } else { false } }.count }
        }
        return (tokens, images * computerConfig.screenshotTokens)
    }

    /// What 【狀況】 says near the limit (one line, none before `warn_ratio`).
    enum Advice: Equatable {
        case none
        /// The current item's process is a good part of it: she should compact it (`todo.progress`).
        case compact
        /// Mostly conversation and earlier results: compacting the item would not free much.
        case conversation
    }

    func advice(used: Int, process: Int) -> Advice {
        guard Double(used) >= Double(contextConfig.limit) * contextConfig.warnRatio else { return .none }
        return todo.phase == .doing && process * 4 >= used ? .compact : .conversation
    }

    /// `n` with a comma every three digits (42,000).
    static func grouped(_ n: Int) -> String {
        let s = String(abs(n))
        var out = ""
        for (i, ch) in s.enumerated() {
            if i > 0, (s.count - i) % 3 == 0 { out.append(",") }
            out.append(ch)
        }
        return (n < 0 ? "-" : "") + out
    }

    /// The facts footer: her todo list, her watch, the context, effort for the next call, and near
    /// the limit one warning line (2026-11-14). Figures are estimates scaled by the last real prompt size.
    func footer(used: Int, process: (tokens: Int, images: Int), advice: Advice) -> String {
        let now = Date().timeIntervalSince1970
        var out = ["【狀況】", "- 現在 \(clock(now))", "- todo：" + todoLine()]
        out.append(watch.map { "- watch：盯著 \($0.target)，最多等 \(seconds($0.maxWait))" } ?? "- watch：沒有")
        let limit = contextConfig.limit
        out.append("- context：\(Self.grouped(used))／\(Self.grouped(limit))（\(Int((Double(used) * 100 / Double(limit)).rounded()))%）；"
                   + "其中做事的過程 \(Self.grouped(process.tokens))（截圖 \(Self.grouped(process.images))）")
        out.append("- 下一回合 effort：\(nextEffort)")
        switch advice {
        case .none: break
        case .compact:
            let left = max(1, contextConfig.forceAfterTurns - warnedTurns)
            out.append("- context 快滿了：用 todo.progress 寫下這一項做到哪裡，前面的過程就會收起來（留下你寫的進度和動作清單）；"
                       + "再 \(left) 回合沒收，程式會自己收（留動作清單和最近 \(contextConfig.forceKeepTurns) 回合）。")
        case .conversation:
            out.append("- context 快滿了，但大部分是對話和之前留下的結果，不是正在做的這一項的過程；收起過程也省不了多少。"
                       + "到上限的時候，程式會把最舊的那一段對話換成一段摘要（【之前的對話】），原文 recall 不一定查得到。")
        }
        return out.joined(separator: "\n")
    }

    /// The todo line of 【狀況】.
    func todoLine() -> String {
        let rest = todo.pending.isEmpty ? "" : "；後面還有：" + todo.pending.map { "「\($0)」" }.joined(separator: "")
        let item = "「\(todo.current ?? "")」"
        return switch todo.phase {
        case .idle, .done: "待命中（沒有 todo；要做事先用 todo.set 列）"
        case .doing: "進行中\(item)\(rest)"
        case .waiting: "等待中\(item)（有人說話或 watch 叫你才會再有回合）\(rest)"
        case .failed: "失敗\(item)：電腦工具鎖著，用 todo.set 決定剩下的（重寫、照原樣列，或給空的陣列結束清單）\(rest)"
        }
    }

    /// `status` in the CLI: the footer as the next turn would see it.
    public func status() throws -> String {
        let used = Int(Double(try estimateContext()) * tokenRatio)
        let p = try processTokens()
        let process = (Int(Double(p.tokens) * tokenRatio), Int(Double(p.images) * tokenRatio))
        return footer(used: used, process: process, advice: advice(used: used, process: process.0))
    }

    func estimateContext() throws -> Int {
        estimate(try context(timeline.events))
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
        inTurn = true
        feed.notify(.turn)
        defer {
            inTurn = false
            feed.notify(.turn)
        }
        let started = Date().timeIntervalSince1970
        followUp = false
        attachedNow = []
        cheatsheetNow = []
        itemEndNow = nil
        compactedNow = []
        experienceNow = nil
        followsNow = []
        selectionsNow = []
        voiceCut = false
        waitNow = false
        startAfterTurn = false
        listEndedNow = false
        // A new owner or developer line starts a task: what was attached before may be attached again.
        if ownerArrived {
            ownerArrived = false
            attachedIDs = []
            cheatsheetSent = []
            await attachMemories(trigger: "\(Self.lastSpeaker(timeline.events).label)的新訊息")
        }
        // She was warned `force_after_turns` times and did not compact: the program does.
        if todo.phase == .doing, warnedTurns >= contextConfig.forceAfterTurns { try forceCompact() }
        // At the limit the oldest conversation is collapsed into a summary (owner, 2026-11-15).
        await compactTalk()
        let events = timeline.events
        var messages = try context(events)
        let guess = estimate(messages)
        let used = Int(Double(guess) * tokenRatio)
        let p = try processTokens()
        let process = (Int(Double(p.tokens) * tokenRatio), Int(Double(p.images) * tokenRatio))
        let warn = advice(used: used, process: process.0)
        let footerText = footer(used: used, process: process, advice: warn)
        warnedTurns = warn == .compact ? warnedTurns + 1 : 0
        messages.append(ChatMessage(role: "user", content: footerText))
        let effort = nextEffort
        let before = todo
        let reply: ModelReply
        do {
            let task = Task { try await model.chat(messages, tools: toolsJSON, effort: effort) }
            inflight = task
            inflightEffort = effort
            feed.notify(.turn)
            reply = try await task.value
            inflight = nil
            inflightEffort = nil
        } catch is CancellationError {
            inflight = nil
            inflightEffort = nil
            if stopped { throw CancellationError() }
            turnIndex -= 1
            if interruptRequested {
                interruptRequested = false
                lastInterrupt = Date().timeIntervalSince1970
                feed.notify(.interrupt)
                log("INFO", "turn \(turnIndex + 1) interrupted from the dashboard; no reply, she waits for the next event")
                return
            }
            log("INFO", "turn \(turnIndex + 1) interrupted; rerunning with the new events")
            try drain()
            return try await turn()
        }
        effortNext = nil
        taskTurn = false
        tokenRatio = Double(reply.usage.promptTokens) / Double(max(1, guess))
        var actions: [String] = []

        // She saw every event, so a secret seen marks her reply (5.1). Stored as returned: re-sent byte-identical.
        var replyEvent = Event(.doll, .internal, reply.content, secret: events.contains(where: \.secret))
        replyEvent.calls = reply.calls
        let stored = try append(replyEvent)
        try timeline.attachFooter(stored.seq!, footerText)
        for call in reply.calls where call.arguments == nil {
            log("WARN", "turn \(turnIndex): a call the server returned as text (\(call.name.isEmpty ? "unreadable" : call.name)): "
                + String(reply.content.suffix(160)).replacingOccurrences(of: "\n", with: "⏎"))
        }

        // Her text content is her own thought (2026-11-10): nobody hears it; she speaks with the speaking tools.
        let calls = reply.calls
        // Every call, in the order she wrote it, gets exactly one tool result right after her reply
        // (spec 6, 2026-11-08); a malformed or unknown one gets a result saying what was wrong. A
        // computer tool while no item is 進行中 is refused with what to do first (2026-11-14).
        // Her `todo.done` / `todo.fail` / `todo.progress` ends a span: the calls after it are refused,
        // except the speaking tools (2026-11-11: she wrote her words after the end and the owner heard
        // nothing; speaking touches no computer).
        // Her computer actions (click, type, key, scroll, drag) run in order and stop at the first
        // failure (2026-11-09): only the last one done carries the observation, the ones before it
        // answer with their line only, and the ones after a failure are not run. Actions written
        // after an ending call never run, so the one before it is the last.
        let live = calls.prefix { !Tools.ending.contains($0.name) }
        let actionAt = live.indices.filter { Tools.actionNames.contains(live[$0].name) }
        var chain = Chain(), step = 0, failed: (step: Int, line: String)?, ended: String?
        for (i, call) in calls.enumerated() {
            let name = call.name.isEmpty ? "tool_call" : call.name
            let isAction = Tools.actionNames.contains(call.name)
            let out: Outcome
            if let ended, Tools.speaking[call.name] == nil {
                out = Outcome(text: "失敗：這則回覆前面已經用 \(ended) 收起這一項了，後面的呼叫沒有執行；看了結果下一回合再做。",
                              actions: ["error \(name)"], background: true)
            } else if Tools.gated.contains(call.name), todo.phase != .doing {
                out = Outcome(text: gateText(name), actions: ["refused \(name)"])
            } else if isAction, let failed {
                out = Outcome(text: "失敗：前面第 \(failed.step) 步（\(failed.line)）失敗了，這一步沒做",
                              actions: ["skipped \(name)"], background: true)
            } else {
                if isAction { step += 1 }
                if Tools.gated.contains(call.name) { itemUsedComputer = true }
                if !call.name.hasPrefix("todo."), call.name != "think" { itemActed = true }
                chain.last = !actionAt.contains(i) || actionAt.last == i
                out = await execute(call, seen: events, reply: stored, chain: chain)
                if out.ends { ended = name }
                if isAction {
                    if let line = out.failedStep {
                        failed = (step, line)
                    } else if !chain.last {
                        chain.owed = true
                    }
                }
            }
            var resultEvent = Event(.tool, .internal, out.text, secret: out.secret, image: out.image,
                                    cropNote: out.cropNote, crop: out.crop, window: out.window)
            resultEvent.channel = out.channel
            try append(resultEvent)
            actions += out.actions
            if !out.background {
                followUp = true
                taskTurn = true
            }
        }
        if ended != nil {
            // What the program would attach for the span she just closed would leave with it.
            cheatsheetDue = []
            appSwitched = false
        }
        // A computer result showed another front app: its 【快捷鍵】 (once per task), then one
        // 【相關記憶】, after all of this reply's results.
        for app in cheatsheetDue { await sendCheatsheet(app) }
        cheatsheetDue = []
        if appSwitched {
            appSwitched = false
            await attachMemories(trigger: "前景 app 換成 \(frontApp?.name ?? "?")")
        }
        // The span her todo call closed this turn: from now on it renders compacted. It ends with the
        // turn's last event; an item her call moved on to starts right after it.
        if var c = compacting {
            compacting = nil
            c.toSeq = timeline.lastSeq
            try timeline.compact(c)
            compactedNow.append(c.kind.rawValue)
            forgetSeen()
            log("INFO", "turn \(turnIndex): compacted「\(c.item)」(\(c.kind.rawValue)) events \(c.fromSeq + 1)…\(c.toSeq)")
        }
        if startAfterTurn { todo.startSeq = timeline.lastSeq }
        if waitNow, todo.phase == .doing { todo.phase = .waiting }
        if todo != before {
            try timeline.recordTodo(todo, seq: stored.seq!)
            log("INFO", "turn \(turnIndex): todo \(before.phase.label) → \(todo.phase.label)「\(todo.current ?? "")」 pending \(todo.pending.count)")
        }
        // The phase decides the next turn (2026-11-14): 進行中 gets one after every reply; 等待中
        // none until an event; the reply that ended the list (done with nothing next, or an empty
        // `todo.set`) none, nothing wakes her for it; 失敗 the one in which she decides.
        switch todo.phase {
        case .doing:
            followUp = true
            taskTurn = true
        case .waiting:
            followUp = false
            taskTurn = false
        case .idle, .done:
            if itemEndNow != nil || listEndedNow {
                followUp = false
                taskTurn = false
            }
        case .failed: break
        }

        // A reply without a call (2026-11-11): every reply calls at least one tool. With an item in
        // progress she is reminded and woken again (she had only thought, or wrote nothing, and the
        // task stopped); the `max_turns_without_tool`-th one in a row writes the log line instead and
        // lets her rest until an event.
        let usedTool = !calls.isEmpty
        if usedTool {
            idleTurns = 0
        } else {
            idleTurns += 1
            if idleTurns >= idle.maxTurnsWithoutTool {
                log("INFO", "idle: \(idleTurns) turns in a row without a tool call")
                idleTurns = 0
                followUp = false
            } else if todo.phase == .doing {
                try append(Event(.memory, .internal, Self.noCallReminder))
                followUp = true
                actions.append("reminded")
            }
        }

        let report = TurnReport(index: turnIndex, actions: actions, reply: reply.content, toolCalls: calls,
                                footer: footerText, effort: effort, usage: reply.usage, usedTool: usedTool,
                                attached: attachedNow, itemEnd: itemEndNow, experience: experienceNow, follows: followsNow,
                                todo: todo.phase.label, compacted: compactedNow,
                                cheatsheets: cheatsheetNow, selections: selectionsNow)
        log("INFO", "turn \(turnIndex) effort=\(effort) todo=\(todo.phase.label) actions=\(actions) prompt=\(reply.usage.promptTokens) "
            + "prefilled=\(reply.usage.prefilled.map(String.init) ?? "?") cached=\(reply.usage.cached.map(String.init) ?? "?")")
        observer?(report)
        try timeline.recordTurn(Timeline.TurnStats(seq: stored.seq!, started: started,
                                                   seconds: Date().timeIntervalSince1970 - started,
                                                   prompt: reply.usage.promptTokens, cached: reply.usage.cached, effort: effort))
    }

    /// The program's line after a reply without a call while an item is in progress (fixed bytes).
    static let noCallReminder = "【提醒】你上一則回覆沒有呼叫工具，所以什麼都沒發生。每一則回覆都至少要呼叫一個工具：要回話用回話的工具（看對方從哪裡來），要做事就呼叫那個工具。"
    /// A voice tool's result when `[voice] enabled` but no voice link is wired (fixed bytes).
    static let voiceUnwired = "語音還沒接上，語音頻道現在用不了；要跟主人說話用 \(Tools.Name.dm)。"

    // tools ------------------------------------------------------------------
    /// One call's result: the tool message text, its screenshots (full screen, then the front
    /// window's crop after its line) and the front window's outline, whether it carries a secret, the
    /// report actions, and whether it only says something started (no next turn for that alone).
    struct Outcome {
        var text: String
        var image: String?
        var cropNote: String?
        var crop: String?
        var window: String?
        var secret = false
        var actions: [String]
        var background = false
        /// This call closed a span (`todo.done`, `todo.fail`, `todo.progress`): the calls after it do not run.
        var ends = false
        /// This computer action failed (before or in the VM): its line, for the refusals after it.
        var failedStep: String?
        /// A speaking call that went out: the channel it went to (2026-11-14).
        var channel: Channel?
    }

    /// Where a reply's computer actions stand (2026-11-09): whether this call is the last action she
    /// wrote (its result carries the observation), and whether an earlier action already answered
    /// with its line only (so a failure here must show the screen, or she sees nothing of this reply).
    struct Chain {
        var last = true
        var owed = false
    }

    /// Runs one tool call. Every failure becomes the call's result text; nothing here throws.
    func execute(_ call: ToolCall, seen events: [Event], reply: Event, chain: Chain = Chain()) async -> Outcome {
        let name = call.name.isEmpty ? "tool_call" : call.name
        do {
            let request = try Tools.decode(call, specs: tools)
            switch request {
            case .speak(let text, .discordVoice):
                // The owner hears it in the voice channel (M6): sentence by sentence through the TTS.
                // Refused when she is not in one (`VoicePort.speak` says so).
                guard let voice else { throw ToolError(description: Self.voiceUnwired) }
                guard !voiceCut else {
                    return Outcome(text: "沒有念：前面一句被主人打斷了，這句沒說出口。", actions: ["error \(name)"], background: true)
                }
                let report = try await voice.speak(text)
                if report.interrupted, report.heard.cut != nil { voiceCut = true }
                log("INFO", "turn \(turnIndex): said in voice「\(report.heardText)」" + (report.interrupted ? "（被打斷）" : ""))
                return Outcome(text: report.resultText, actions: ["said \(Channel.discordVoice.rawValue)" + (report.interrupted ? " (cut)" : "")],
                               background: true, channel: .discordVoice)
            case .speak(let text, let channel):
                // Text to the owner's Discord DM or the developer's terminal: nothing new for her to
                // read, and nothing to wake her for. Not delivered (the bridge is down, no dollctl
                // chat is open): her result says why and what she can do.
                let to: Addressee = channel == .dollctl ? .developer : .owner
                if let why = deliver(text, to) {
                    log("ERROR", "turn \(turnIndex): \(name) not delivered: \(why)")
                    let next = switch to {
                    case .owner: "這段話主人沒看到；Discord 連上之前 \(Tools.Name.dm) 都傳不出去，連上以後再傳一次。"
                    case .developer: "開發者開著 dollctl chat 才收得到 \(Tools.Name.reply)；要跟主人說話用 \(Tools.Name.dm)。"
                    }
                    return Outcome(text: "失敗：\(to.label)沒收到：\(why)。\(next)", actions: ["error \(name)"], background: true)
                }
                return Outcome(text: "", actions: ["said \(channel.rawValue)"], background: true, channel: channel)
            case .voiceJoin:
                guard let voice else { throw ToolError(description: Self.voiceUnwired) }
                let ch = try await voice.join()
                return Outcome(text: "進了語音頻道「\(ch)」。你只聽得到主人；在裡面說話用 \(Tools.Name.voiceSay)。", actions: [name], background: true)
            case .voiceLeave:
                guard let voice else { throw ToolError(description: Self.voiceUnwired) }
                try await voice.leave()
                return Outcome(text: "離開語音頻道了。之後跟主人說話用 \(Tools.Name.dm)。", actions: [name], background: true)
            case .think(let level):
                effortNext = level
                return Outcome(text: "下一回合 effort：\(level)", actions: ["think \(level)"])
            case .remember(let items):
                return await remember(items, seen: events, reply: reply)
            case .recall(let query):
                return try await recall(query, seen: events)
            case .todoSet(let items):
                return todoSet(items, reply: reply)
            case .todoDone(let result):
                return try await todoDone(result, reply: reply)
            case .todoFail(let reason, let tried):
                return try todoFail(reason: reason, tried: tried)
            case .todoWait:
                let item = try currentItem("todo.wait")
                waitNow = true
                return Outcome(text: "好，這則回覆的呼叫做完以後，「\(item)」就是等待中：有人說話或 watch 叫你才會再有回合。",
                               actions: ["todo wait"], background: true)
            case .todoProgress:
                let item = try currentItem("todo.progress")
                compacting = Compaction(kind: .progress, item: item, fromSeq: todo.startSeq, toSeq: 0)
                warnedTurns = 0
                return Outcome(text: "前面的過程收起來了，之後只留你寫的進度和這份動作清單：\(Self.actionList(itemSteps.map(\.line)))。「\(item)」還在進行中，接著做。",
                               actions: ["todo progress"], ends: true)
            case .screenshot:
                watchIdle = 0
                return await withScreenshot("現在的畫面：", actions: [name])
            case .act(let action):
                return await perform(action, said: Self.describe(action), note: nil, name: name, chain: chain)
            case .clickAt(let x, let y, let onWindow, let button, let count):
                let p = try screenPoint(x, y, onWindow: onWindow)
                let action = ComputerAction.click(x: p.x, y: p.y, button: button, count: count)
                let line = onWindow ? "click 視窗圖 \(x),\(y) → 全螢幕 \(p.x),\(p.y)" + Self.extra(button, count) : Self.describe(action)
                return await perform(action, said: line, note: onWindow ? "視窗圖 \(x),\(y) → 全螢幕 \(p.x),\(p.y)" : nil, name: name, chain: chain)
            case .watch(let region, let maxWait):
                let replaced = watch != nil
                let w = startWatch(region, maxWait: maxWait ?? computerConfig.watchDefaultMaxWaitS)
                waitNow = true
                return Outcome(text: "開始盯著\(w.target)，最多等 \(seconds(w.maxWait))；有變化或時間到會叫你。"
                               + (replaced ? "（取代了上一個 watch）" : "") + "這則回覆的呼叫做完以後，這一項就是等待中。",
                               actions: [name], background: true)
            // The file tools (2026-11-09): text only, no screenshot and no window line, the screen did
            // not change. Only a success is a step, and the step never holds the content.
            case .readFile(let path, let from, let to):
                let text = try FileText.decode(try await computer.readFile(path), path: path)
                let out = try FileText.read(text, path: path, from: from, to: to, max: filesConfig.maxReadLines)
                itemSteps.append((timeline.lastSeq + 1, "read_file \(path)"))
                return Outcome(text: out, actions: [name])
            case .writeFile(let path, let text):
                try await computer.writeFile(path, Data(text.utf8))
                itemSteps.append((timeline.lastSeq + 1, "write_file \(path)（\(FileText.lines(text).count) 行）"))
                return Outcome(text: FileText.written(text, path: path), actions: [name])
            case .editFile(let path, let old, let new):
                let text = try FileText.decode(try await computer.readFile(path), path: path)
                let (changed, out) = try FileText.edit(text, path: path, old: old, new: new)
                try await computer.writeFile(path, Data(changed.utf8))
                itemSteps.append((timeline.lastSeq + 1, "edit_file \(path)"))
                return Outcome(text: out, actions: [name])
            }
        } catch {
            log("ERROR", "turn \(turnIndex): \(name) failed: \(error)")
            // A computer action refused before the VM (bad arguments, a point off the screen) ends the
            // reply's chain like one that failed in it; the screen is shown when an earlier action
            // of this reply answered with its line only.
            guard Tools.actionNames.contains(call.name) else { return Outcome(text: "失敗：\(error)", actions: ["error \(name)"]) }
            var out = chain.owed ? await withScreenshot("失敗：\(error)", actions: ["error \(name)"])
                                 : Outcome(text: "失敗：\(error)", actions: ["error \(name)"])
            out.failedStep = name
            return out
        }
    }

    /// `text` and the open windows / focus line, plus a fresh full-screen screenshot, the front
    /// window's crop with where it sits on the full screen and its size, and the window's outline
    /// (spec 3.1, 2026-11-09). No front window: no crop and no outline, the result says why, and the
    /// menu bar's and Dock's items with their centers follow in the outline's place. A failed
    /// screenshot, crop, outline, menu bar/Dock summary or window list is said in the result (and is
    /// an error); none is dropped silently.
    func withScreenshot(_ text: String, actions: [String]) async -> Outcome {
        let computer = computer
        async let front: Result<FrontWindow, Error> = {
            do { return .success(try await computer.frontWindow()) } catch { return .failure(error) }
        }()
        var out = Outcome(text: text, actions: actions)
        // A successful action may have nothing of its own to say (2026-11-10): no leading blank line.
        func add(_ line: String) { out.text = out.text.isEmpty ? line : out.text + "\n" + line }
        cropMap = nil
        do { out.image = try await capture(nil) } catch {
            log("ERROR", "turn \(turnIndex): screenshot failed: \(error)")
            add("截圖失敗：\(error)")
            out.actions.append("error screenshot")
        }
        switch await front {
        case .success(let w):
            out.window = w.summary
            switch w.place {
            case .at(let r):
                do {
                    out.crop = try await capture(r)
                    let size = cropMap.map { "，圖 \($0.width)×\($0.height)" } ?? ""
                    out.cropNote = "視窗圖：最前面的視窗，在全螢幕 (\(r.x0),\(r.y0))–(\(r.x1),\(r.y1))\(size)；用這張圖上的座標點要加 on: window。"
                } catch {
                    log("ERROR", "turn \(turnIndex): window crop failed: \(error)")
                    out.cropNote = "沒有視窗圖：截視窗失敗：\(error)"
                    out.actions.append("error crop")
                }
            case .unknown(let why):
                out.cropNote = "沒有視窗圖：\(why)"
            }
        case .failure(let error):
            log("ERROR", "turn \(turnIndex): window outline failed: \(error)")
            out.actions.append("error window")
            let desktop: String
            do { desktop = try await computer.desktopSummary() } catch {
                log("ERROR", "turn \(turnIndex): menu bar and Dock summary failed: \(error)")
                desktop = "Dock 和選單列概要失敗：\(error)"
                out.actions.append("error desktop")
            }
            out.window = "沒有視窗圖和視窗概要：\(error)\n\(desktop)"
        }
        // The open windows, the focused element and the front app: after the rest, never alongside it
        // (VMPal runs at most two CLI commands per VM at once; a third fails, 2026-11-09). The line
        // ends the result's text; the front app keys her shortcut memories and the 【相關記憶】 on a switch.
        do {
            let state = try await computer.frontState()
            add(state.line)
            frontTitle = state.title
            frontFocus = state.focus
            if let app = state.app {
                if computerConfig.learnShortcuts { await learnShortcuts(app) }
                if app.name != frontApp?.name {
                    appSwitched = true
                    if computerConfig.cheatsheet, !cheatsheetSent.contains(app.bundleID),
                       !cheatsheetDue.contains(where: { $0.bundleID == app.bundleID }) {
                        cheatsheetDue.append(app)
                    }
                }
                frontApp = app
                if !itemApps.contains(where: { $0.bundleID == app.bundleID }) { itemApps.append(app) }
            }
        } catch {
            log("ERROR", "turn \(turnIndex): open windows and focus failed: \(error)")
            add("開著的視窗和焦點讀不到：\(error)")
            out.actions.append("error windows")
        }
        return out
    }

    /// The program's own shortcut memories (spec 3.1, 2026-11-09): the first time the front app is
    /// one not recorded at its version, every menu shortcut it has becomes one public memory, tier
    /// program, no kind; the app's records from another version are deleted first. She is not told:
    /// they are in her memory for `recall`. A failed read is logged and tried again next time. Its
    /// entities (2026-11-11): the app (`Selector.programApp`) and the keys (`Selector.programShortcut`).
    /// Not the menu item: two apps' items of one title are different things, and the text has it.
    func learnShortcuts(_ app: AppIdentity) async {
        do {
            if learnedApps == nil { learnedApps = try memory.learnedApps() }
            guard learnedApps?[app.bundleID] != app.version else { return }
            let shortcuts = try await shortcuts(of: app)
            // Re-checked after the await: another call may have recorded it meanwhile. Marked before
            // the writes (which await the registry alignment), unmarked if they fail.
            guard learnedApps?[app.bundleID] != app.version else { return }
            let before = learnedApps?[app.bundleID]
            learnedApps?[app.bundleID] = app.version
            do {
                let removed = try memory.forgetApp(app.bundleID)
                let appEntity = try await pipeline.programApp(app, context: "\(app.name) 的選單快捷鍵").entry
                for s in shortcuts {
                    let text = "\(app.name)：\(s.path)＝\(s.keys)（\(app.name) \(app.version)）"
                    let keys = try await pipeline.programShortcut(s.keys, context: text)
                    try memory.write(text, folder: .public, sourceSeq: 0, tier: .program, kind: nil, app: app,
                                     entities: [MemoryEntity(appEntity.id, app.name), MemoryEntity(keys.id, s.keys)])
                }
                log("INFO", "shortcuts of \(app.bundleID) \(app.version): \(shortcuts.count) written, \(removed) old removed")
            } catch {
                learnedApps?[app.bundleID] = before
                throw error
            }
        } catch {
            log("ERROR", "shortcuts of \(app.bundleID) \(app.version) failed: \(error)")
        }
    }

    /// The menu shortcuts of `app` at its version: from the cache, else read from the VM once.
    func shortcuts(of app: AppIdentity) async throws -> [MenuShortcut] {
        let key = "\(app.bundleID)\t\(app.version)"
        if let cached = shortcutCache[key] { return cached }
        let read = try await computer.menuShortcuts(app.bundleID)
        shortcutCache[key] = read
        return read
    }

    /// The front app's whole cheatsheet into the timeline (spec 3.1, 2026-11-09): `【快捷鍵】<App>
    /// <version>`, then one `選單 › 項目 = 鍵` line per shortcut in the reader's order; stored once,
    /// re-sent byte-identical like every event. Once per app since the owner's last line. A failed
    /// read, or an app without shortcuts, is said in the log only; a failed read is tried again on
    /// the next switch to that app.
    func sendCheatsheet(_ app: AppIdentity) async {
        guard !cheatsheetSent.contains(app.bundleID) else { return }
        do {
            let shortcuts = try await shortcuts(of: app)
            guard !shortcuts.isEmpty else {
                log("INFO", "turn \(turnIndex): 快捷鍵 of \(app.bundleID) \(app.version): none, no event")
                return
            }
            let text = Self.cheatsheetText(app, shortcuts)
            try append(Event(.memory, .internal, text))
            cheatsheetSent.insert(app.bundleID)
            cheatsheetNow.append(text)
            log("INFO", "turn \(turnIndex): 快捷鍵 of \(app.bundleID) \(app.version): \(shortcuts.count) lines, \(text.count) chars")
        } catch {
            log("ERROR", "turn \(turnIndex): 快捷鍵 of \(app.bundleID) \(app.version) failed: \(error)")
        }
    }

    static func cheatsheetText(_ app: AppIdentity, _ shortcuts: [MenuShortcut]) -> String {
        (["【快捷鍵】\(app.name) \(app.version)"] + shortcuts.map { "\($0.path) = \($0.keys)" }).joined(separator: "\n")
    }

    /// Records the front app's shortcuts now, as the first use of it would (a run that starts with
    /// them already in her memory).
    public func learnFrontApp() async throws {
        guard let app = try await computer.frontApp() else { throw ComputerError("沒有前景 app") }
        await learnShortcuts(app)
    }

    /// Takes a full-screen screenshot, or the front window's crop at `region`, and stores its bytes
    /// as a file under `shots/`. Notes its pixel size for `click`'s coordinates.
    func capture(_ region: Region?) async throws -> String {
        let data: Data, format: String
        if let region {
            (data, format) = (try await computer.windowShot(region), computerConfig.windowCropFormat)
            cropMap = ImageSize.of(data).map { (region, $0.width, $0.height) }
        } else {
            (data, format) = (try await computer.screenshot(region: nil), computerConfig.screenshotFormat)
            screenSize = ImageSize.of(data)
        }
        let file = "\(hex(16)).\(format == "png" ? "png" : "jpg")"
        try data.write(to: shots.appending(path: file))
        return file
    }

    /// Runs one action. A success says only what she does not already have (2026-11-10: her call is in
    /// the context, so it is not echoed): `note` (what the program worked out, e.g. a window-crop point's
    /// screen point), then VMPal's words without its bookkeeping; nothing at all when nothing is left. A
    /// failure names the step (`said`) and the reason. The last action of her reply (and every failure)
    /// comes with fresh screenshots; an earlier one with its text only. The action's own time,
    /// screenshots excluded, goes to the log (M2 round 6: element-click latency).
    func perform(_ action: ComputerAction, said: String, note: String?, name: String, chain: Chain) async -> Outcome {
        watchIdle = 0
        let start = ContinuousClock.now
        let result: Result<String, Error>
        do { result = .success(try await computer.act(action)) } catch { result = .failure(error) }
        let took = start.duration(to: .now)
        log("INFO", "turn \(turnIndex): \(Self.describe(action)) took \(String(format: "%.2f", Double(took.components.seconds) + Double(took.components.attoseconds) / 1e18)) s")
        switch result {
        case .success(let answer):
            itemSteps.append((timeline.lastSeq + 1, Self.step(action)))
            let text = [note ?? "", Self.condense(answer)].filter { !$0.isEmpty }.joined(separator: "\n")
            guard chain.last else { return Outcome(text: text, actions: [name]) }
            return await withScreenshot(text, actions: [name])
        case .failure(let error):
            // A failed action is not a step of the experience (spec 3.1, 2026-11-09).
            // VMPal's or agent-desktop's own words, verbatim; the screenshot shows where things stand.
            log("ERROR", "turn \(turnIndex): \(name) failed: \(error)")
            var out = await withScreenshot("失敗：\(said)\n\(error)", actions: ["error \(name)"])
            out.failedStep = said
            return out
        }
    }

    /// A successful action's answer as she reads it (2026-11-10): VMPal's bookkeeping lines are left
    /// out: `Done: 1 action in 0.7 s. Screen 1280×800 (screenshot pixels), frame 8386.`, the echo of
    /// where a pointer action landed (`left_click at (558, 328).`, `double_click on “x” at (…).`),
    /// `Typed 46 characters.`, and the clipboard note of a paste. What she uses stays: `Changed: …`,
    /// `Nothing changed …` (a click that may have missed), and anything else. They were most of the
    /// ~90 tokens a result took, kept in every later turn.
    static func condense(_ answer: String) -> String {
        answer.split(separator: "\n", omittingEmptySubsequences: false).map(String.init).filter { line in
            !(line.hasPrefix("Done: ") && line.contains(" action")
              || line.range(of: #"^(left_click|right_click|middle_click|double_click|triple_click)( on .*)? at \(\d+, \d+\)\.$"#, options: .regularExpression) != nil
              || line.range(of: #"^Typed \d+ characters?\.$"#, options: .regularExpression) != nil
              || line == "經剪貼簿貼上（cmd+v），剪貼簿已還原。")
        }.joined(separator: "\n")
    }

    /// `click` x, y in full-screenshot pixels: as given (`on: screen`), or from the last window crop
    /// through where it sits on the screen and its size (`on: window`). A point off the screen, or
    /// off the crop, is an error and nothing is clicked.
    func screenPoint(_ x: Int, _ y: Int, onWindow: Bool) throws -> (x: Int, y: Int) {
        let width = screenSize?.width ?? computerConfig.screenshotWidth
        guard onWindow else {
            guard x >= 0, y >= 0, x < width, screenSize.map({ y < $0.height }) ?? true else {
                throw ToolError(description: "沒有點：\(x),\(y) 在螢幕外（全螢幕截圖 "
                                + (screenSize.map { "\($0.width)×\($0.height)" } ?? "\(width) 寬") + "）")
            }
            return (x, y)
        }
        guard let m = cropMap else {
            throw ToolError(description: "沒有點：最近的結果沒有視窗圖，on: window 換算不了；用全螢幕截圖的座標")
        }
        guard x >= 0, y >= 0, x < m.width, y < m.height else {
            throw ToolError(description: "沒有點：\(x),\(y) 不在視窗圖裡（視窗圖 \(m.width)×\(m.height)）")
        }
        let r = m.region
        let sx = r.x0 + Int((Double(x) * Double(r.x1 - r.x0) / Double(m.width)).rounded(.down))
        let sy = r.y0 + Int((Double(y) * Double(r.y1 - r.y0) / Double(m.height)).rounded(.down))
        return (sx, sy)
    }

    static func extra(_ button: String, _ count: Int) -> String {
        (button == "left" ? "" : " \(button)") + (count == 2 ? " 雙擊" : "")
    }

    /// What an action was, for the first line of its result.
    static func describe(_ a: ComputerAction) -> String {
        switch a {
        case .click(let x, let y, let button, let count): return "click \(x),\(y)\(extra(button, count))"
        case .clickText(let text, let button, let count): return "click「\(text)」\(extra(button, count))"
        case .clickElement(let role, let name, let button, let count):
            return "click 元素 " + [role, name.map { "\"\($0)\"" }].compactMap { $0 }.joined(separator: " ") + extra(button, count)
        case .type(let text): return "type「\(text)」"
        case .key(let keys): return "key \(keys)"
        case .scroll(let direction, let amount): return "scroll \(direction) \(amount)"
        case .drag(let x0, let y0, let x1, let y1): return "drag \(x0),\(y0) → \(x1),\(y1)"
        }
    }

    // watch ------------------------------------------------------------------
    /// Replaces any watch she had. The loop compares screenshots of the region; a change wakes her
    /// (at most once per `watch_min_interval_s`), and so does `max_wait` passing without a wake.
    func startWatch(_ region: Region?, maxWait: Double) -> Watch {
        stopWatch()
        watchSerial += 1
        let id = watchSerial
        watchIdle = 0
        let w = Watch(id: id, region: region, maxWait: maxWait,
                      task: Task { await self.watchLoop(id, region: region, maxWait: maxWait) })
        watch = w
        return w
    }

    func stopWatch() {
        watch?.task.cancel()
        watch = nil
    }

    func watchLoop(_ id: Int, region: Region?, maxWait: Double) async {
        let c = computerConfig
        var base: Data
        do { base = try await computer.screenshot(region: region) } catch {
            guard watch?.id == id else { return }
            log("ERROR", "watch \(id): first screenshot failed: \(error)")
            watch = nil
            put(Event(.watch, .internal, "watch 失敗，沒有在盯：\(error)"))
            return
        }
        var lastWake: ContinuousClock.Instant?
        var deadline = ContinuousClock.now + .seconds(maxWait)
        while !Task.isCancelled {
            try? await Task.sleep(for: .seconds(c.watchPollS))
            guard watch?.id == id, !Task.isCancelled else { return }
            let shot: Data
            do { shot = try await computer.screenshot(region: region) } catch {
                log("WARN", "watch \(id): screenshot failed: \(error)")
                continue
            }
            guard watch?.id == id else { return }
            let now = ContinuousClock.now
            let changed = shot != base
            let spaced = lastWake.map { now - $0 >= .seconds(c.watchMinIntervalS) } ?? true
            guard (changed && spaced) || now >= deadline else { continue }
            base = shot
            lastWake = now
            deadline = now + .seconds(maxWait)
            await watchWake(id, changed: changed)
        }
    }

    func watchWake(_ id: Int, changed: Bool) async {
        guard let w = watch, w.id == id else { return }
        watchIdle += 1
        var text = changed ? "盯著的\(w.target)變了。" : "盯著的\(w.target) \(seconds(w.maxWait))沒變化。"
        if watchIdle >= computerConfig.watchStopAfter {
            text += "（連續 \(watchIdle) 次叫你都沒操作電腦，watch 停了）"
            log("INFO", "watch \(id) stopped after \(watchIdle) wakes in a row without a computer action")
            stopWatch()
        }
        let image: String?
        do { image = try await capture(nil) } catch {
            log("ERROR", "watch \(id): screenshot failed: \(error)")
            text += "\n截圖失敗：\(error)"
            image = nil
        }
        put(Event(.watch, .internal, text, image: image))
    }

    // todo -------------------------------------------------------------------
    /// The item in progress; a call that needs one throws with the phase when there is none.
    func currentItem(_ tool: String) throws -> String {
        guard todo.phase == .doing, let item = todo.current else {
            throw ToolError(description: "\(tool) 要有進行中的項目；現在是\(todo.phase.label)" + (todo.phase == .idle ? "，先用 todo.set 列 todo" : ""))
        }
        return item
    }

    /// A computer tool's result while no item is 進行中 (2026-11-14): refused, with what to do first.
    func gateText(_ name: String) -> String {
        switch todo.phase {
        case .failed:
            "失敗：「\(todo.current ?? "")」失敗了，\(name) 先鎖著。先用 todo.set 決定剩下的：重寫剩下的項目、照原樣列剩下的接著做，或跟主人說了以後給空的陣列結束清單。"
        case .waiting:
            "失敗：「\(todo.current ?? "")」在等待中，\(name) 沒有執行。"
        case .idle, .done, .doing:
            "失敗：現在沒有進行中的 todo，\(name) 不能用。先用 todo.set 列 todo（小事就一項，每項寫做完的樣子），再做。"
        }
    }

    /// A new item is in progress: its action list, apps and computer use start empty, and the front
    /// app counts as newly seen (the previous item's 【快捷鍵】 and 【相關記憶】 left the request with
    /// it; 2026-11-09, chained E→F: F got no TextEdit sheet because TextEdit was already in front).
    func beginItem() {
        itemSteps = []
        itemApps = []
        itemUsedComputer = false
        itemActed = false
        warnedTurns = 0
        forgetSeen()
    }

    /// After a compaction: what the program showed for the front app left the request.
    func forgetSeen() {
        frontApp = nil
        frontTitle = nil
        frontFocus = nil
        cheatsheetSent = []
    }

    /// The action list a compaction leaves (2026-11-14): one short piece per action that went
    /// through, no screenshots, no results.
    static func actionList(_ steps: [String]) -> String {
        steps.isEmpty ? "（沒有）" : steps.joined(separator: "、")
    }

    /// `todo.set`: with no item in progress (待命中, 失敗) the list is replaced and its first item is
    /// 進行中 from her reply on (that reply is the item's first event); with one in progress only the
    /// items after it are replaced. No items: the list ends (待命中).
    func todoSet(_ items: [String], reply: Event) -> Outcome {
        func listed(_ xs: [String]) -> String { xs.isEmpty ? "沒有" : xs.map { "「\($0)」" }.joined() }
        switch todo.phase {
        case .doing, .waiting:
            todo.pending = items
            return Outcome(text: "後面的項目改成：\(listed(items))。現在\(todo.phase.label)的還是「\(todo.current ?? "")」。",
                           actions: ["todo set"])
        case .idle, .done, .failed:
            guard let first = items.first else {
                let ended = todo.phase == .failed
                todo = TodoState()
                listEndedNow = true
                return Outcome(text: ended ? "清單結束了，現在待命中。" : "沒有項目，還是待命中。", actions: ["todo end"], background: true)
            }
            todo.phase = .doing
            todo.current = first
            todo.pending = Array(items.dropFirst())
            todo.startSeq = reply.seq! - 1
            beginItem()
            return Outcome(text: "todo 列好了。現在進行中：「\(first)」；後面：\(listed(todo.pending))。", actions: ["todo set"])
        }
    }

    /// `todo.done`: the item is 完成. Its span is compacted once the turn's results are in; the
    /// experiences attached are scored as a success; an item that used the computer is written as
    /// an experience (her result and its steps; turns and seconds from the item's start); the watch
    /// stops; the next item is 進行中 after this turn, or the list is done (待命中).
    func todoDone(_ result: String, reply: Event) async throws -> Outcome {
        let item = try currentItem("todo.done")
        // Done needs something done or looked at (owner, 2026-11-14): in a real run she ended an
        // untouched item right after the previous one was compacted, with a made-up result.
        guard itemActed else {
            throw ToolError(description: "「\(item)」這一項你還沒做任何動作，也沒看過結果，不能說完成。先動手做；如果覺得已經是做完的樣子，至少用 screenshot 或 read_file 親眼看過再說完成。")
        }
        let steps = itemSteps.map(\.line)
        compacting = Compaction(kind: .done, item: item, fromSeq: todo.startSeq, toSeq: 0)
        stopWatch()
        scoreAttached(steps: steps, outcome: "success")
        itemEndNow = TodoPhase.done.label
        var note = ""
        if itemUsedComputer {
            do {
                note = try await writeExperience(item: item, result: result, steps: steps, reply: reply)
            } catch {
                log("ERROR", "turn \(turnIndex): experience for「\(item)」not written: \(error)")
                note = "這次的經驗沒存成：\(error)。"
            }
        }
        let next = advance()
        log("INFO", "turn \(turnIndex): todo 完成「\(item)」result「\(result)」, \(steps.count) actions")
        return Outcome(text: "完成，記下了。\(note)這一項的過程（截圖、每一步的結果、那幾回合你的念頭）收起來了，之後只留你寫的 result 和這份動作清單："
                       + Self.actionList(steps) + "。"
                       + (next.map { "下一項「\($0)」現在進行中。" } ?? "清單做完了，現在待命中；程式不會再為這件事叫你。"),
                       actions: ["todo done"], ends: true)
    }

    /// `todo.fail` (only when she is sure): the item is 失敗; its span is compacted (her reason and
    /// what she tried stay in her call); the experiences attached are scored as a failure; the watch
    /// stops; the computer tools stay refused until her `todo.set`.
    func todoFail(reason: String, tried: String) throws -> Outcome {
        let item = try currentItem("todo.fail")
        let steps = itemSteps.map(\.line)
        compacting = Compaction(kind: .failed, item: item, fromSeq: todo.startSeq, toSeq: 0)
        stopWatch()
        scoreAttached(steps: steps, outcome: "failure")
        itemEndNow = TodoPhase.failed.label
        todo.phase = .failed
        log("INFO", "turn \(turnIndex): todo 失敗「\(item)」reason「\(reason)」")
        let rest = todo.pending.isEmpty ? "（後面沒有別的項目了）" : "（剩下：" + todo.pending.map { "「\($0)」" }.joined() + "）"
        return Outcome(text: "記下了：「\(item)」沒做成。這一項的過程收起來了，之後只留你寫的原因、試過的和這份動作清單：\(Self.actionList(steps))。"
                       + "電腦工具先鎖著。現在決定剩下的\(rest)：用 todo.set 重寫剩下的項目；或照原樣列剩下的，接著做下一項；或跟主人說了以後用 todo.set 給空的陣列結束清單。",
                       actions: ["todo fail"], ends: true)
    }

    /// The next item is 進行中 (its span starts after this turn), or the list is done: 待命中. Returns the next item.
    func advance() -> String? {
        guard !todo.pending.isEmpty else {
            todo = TodoState()
            return nil
        }
        todo.current = todo.pending.removeFirst()
        todo.phase = .doing
        startAfterTurn = true
        beginItem()
        return todo.current
    }

    /// One experience memory for an item that used the computer and is done: the owner's last line
    /// as its task, the item and her result as its summary, the item's steps; turns = her replies
    /// since the item started (this one included), seconds from the item's start to this reply.
    func writeExperience(item: String, result: String, steps: [String], reply: Event) async throws -> String {
        let events = timeline.events
        let task = events.last { $0.speaker != nil && $0.seq! < reply.seq! }?.text ?? item
        let text = Self.experienceText(task: task, summary: "這一項：\(item)\n\(result)", steps: steps,
                                       maxChars: memoryConfig.experienceMaxChars)
        let span = events.filter { $0.seq! > todo.startSeq && $0.seq! <= reply.seq! }
        let startTs = events.first { $0.seq! >= todo.startSeq }?.ts ?? reply.ts
        let stats = ExperienceStats(turns: span.filter { $0.kind == .doll }.count, seconds: Int((reply.ts - startTs).rounded()))
        // Its entities: the apps it went through (what the program knows of it, 2026-11-11),
        // aligned to the registry by their bundle ids.
        var entities: [MemoryEntity] = []
        for app in itemApps {
            entities.append(MemoryEntity(try await pipeline.programApp(app, context: text).entry.id, app.name))
        }
        let id = try memory.write(text, folder: .public, sourceSeq: reply.seq!, tier: .selfReported, kind: nil,
                                  experience: (task: task, stats: stats), entities: entities)
        experienceNow = text
        log("INFO", "turn \(turnIndex): experience \(id) written, \(steps.count) steps, \(text.count) chars, "
            + "\(stats.turns) turns, \(stats.seconds) s")
        return "這次的經過和步驟存成一條經驗。"
    }

    /// The program compacts the item in progress itself (2026-11-14): she was warned
    /// `force_after_turns` times and did not call `todo.progress`. Everything of the item before
    /// her last `force_keep_turns` turns leaves the request; a block with the action list up to there
    /// takes its place. Nothing to compact (too few turns, or already compacted that far): only logged.
    func forceCompact() throws {
        warnedTurns = 0
        guard let item = todo.current else { return }
        let start = todo.startSeq
        let replies = timeline.events.filter { $0.kind == .doll && $0.seq! > start }
        let keep = contextConfig.forceKeepTurns
        let prior = timeline.compactions.filter { $0.kind != .talk && $0.fromSeq == start }.map(\.toSeq).max() ?? start
        guard replies.count > keep, case let cut = replies[replies.count - keep].seq! - 1, cut > prior else {
            log("INFO", "turn \(turnIndex): forced compaction of「\(item)」: nothing before the last \(keep) turns left to compact")
            return
        }
        let steps = itemSteps.filter { $0.seq <= cut }.map(\.line)
        let text = "【收起】context 快滿了，你沒有用 todo.progress 收，程式把「\(item)」到這裡為止的過程收起來了：截圖、每一步的結果和那幾回合你的念頭都不在了，"
            + "最近 \(keep) 回合還在下面。到這裡做過的動作：\(Self.actionList(steps))。"
        try timeline.compact(Compaction(kind: .forced, item: item, fromSeq: start, toSeq: cut, text: text))
        compactedNow.append(Compaction.Kind.forced.rawValue)
        forgetSeen()
        log("INFO", "turn \(turnIndex): forced compaction of「\(item)」events \(start + 1)…\(cut), \(steps.count) actions")
    }

    static let talkInstructions = """
        下面是一段對話紀錄的最舊的部分，要從一個 AI 角色的 context 裡收起來。請寫一段給她自己以後看的摘要，用繁體中文、用「你」稱呼她：
        - 誰跟她說了什麼、她答應了什麼、決定了什麼；主人交代的規矩和偏好要逐條留下，盡量用原話。
        - 她做過的事和結果（一項一行就好）。
        - 還沒了結的事：有人在等她回覆的、她說之後要做的。
        紀錄開頭如果有「【更早的摘要】」，那是更早一次收起來時寫的：把它跟後面的紀錄合成一份新的摘要，裡面還有用的都要留著，已經了結的事可以縮成一句。
        只寫紀錄裡有的，不要補、不要猜；不用寫開場白和結語。
        """
    static let talkHeader = "【之前的對話】context 到上限了，程式把到這裡為止最舊的一段（對話和做過的事）收起來，換成另外寫的這段摘要；原文 recall 不一定查得到。\n"

    /// The context reached `[context] limit` (owner, 2026-11-15: the last resort, so it runs at once):
    /// the oldest `talk_share` of what is still in the request, conversation included, is replaced by
    /// a summary a separate clean request writes; an earlier summary is merged into the new one, so
    /// there is one block however often this ran. It never cuts into the item in progress, and it
    /// cuts just before someone's line. The originals stay in the timeline file (recall finds them).
    /// A failed summary request leaves everything as it was and is tried again next turn.
    func compactTalk() async {
        do {
            let all = timeline.events
            let used = Int(Double(estimate(try context(all))) * tokenRatio)
            guard used >= contextConfig.limit else { return }
            let earlier = timeline.compactions.filter { $0.kind == .talk }.max { $0.toSeq < $1.toSeq }
            let last = earlier?.toSeq ?? 0
            let bound = todo.phase == .idle ? timeline.lastSeq : todo.startSeq
            let live = all.filter { $0.seq! > last }
            let total = live.reduce(0) { $0 + estimateTokens($1.text) }
            var sum = 0, cut: Int64?
            for ev in live {
                // A cut sits just before someone's line, at most at `bound`.
                if ev.speaker != nil, ev.seq! - 1 > last, ev.seq! - 1 <= bound {
                    if cut != nil, Double(sum) > Double(total) * contextConfig.talkShare { break }
                    cut = ev.seq! - 1
                }
                sum += estimateTokens(ev.text)
            }
            guard let cut else {
                log("WARN", "turn \(turnIndex + 1): context \(used) of \(contextConfig.limit), but there is no conversation before the item in progress to collapse")
                return
            }
            let span = live.filter { $0.seq! <= cut }
            let before = earlier?.text.map { "【更早的摘要】\n" + $0.dropFirst(Self.talkHeader.count) + "\n\n【後面的紀錄】\n" } ?? ""
            let record = try before + context(span).dropFirst().map { m in
                let calls = m.toolCalls.map { "\($0.name)(\($0.arguments ?? ""))" }.joined(separator: "；")
                let who = m.role == "assistant" ? "她" : m.role == "tool" ? "結果" : "事件"
                return "\(who)：" + [m.content, calls].filter { !$0.isEmpty }.joined(separator: " ")
            }.joined(separator: "\n")
            let t0 = Date()
            let reply = try await model.chat([ChatMessage(role: "system", content: Self.talkInstructions),
                                              ChatMessage(role: "user", content: record)], tools: nil, effort: contextConfig.talkEffort)
            let summary = reply.content.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !summary.isEmpty else { throw ModelError("the summary request returned no text") }
            // One rolling summary: it starts at the beginning and replaces the one before it.
            try timeline.compact(Compaction(kind: .talk, item: "", fromSeq: 0, toSeq: cut, text: Self.talkHeader + summary))
            compactedNow.append(Compaction.Kind.talk.rawValue)
            forgetSeen()
            log("INFO", String(format: "turn \(turnIndex + 1): context \(used) of \(contextConfig.limit): events \(last + 1)…\(cut) collapsed into a summary of \(summary.count) chars (%.1f s)", Date().timeIntervalSince(t0)))
        } catch {
            log("ERROR", "turn \(turnIndex + 1): the oldest conversation was not collapsed: \(error)")
        }
    }

    /// One step of an experience: only what she did, in full-screenshot coordinates (a click on the
    /// window crop is stored where it landed); no VM answer text (spec 3.1, 2026-11-09).
    static func step(_ a: ComputerAction) -> String {
        switch a {
        case .clickElement(let role, let name, let button, let count):
            return "click " + [role, name.map { "\"\($0)\"" }].compactMap { $0 }.joined(separator: " ") + extra(button, count)
        case .type(let text): return "type 「\(text)」"
        default: return describe(a)
        }
    }

    /// One experience memory (spec 3.1, 2026-11-09): the owner's line, her summary, then the
    /// program's numbered steps under 「步驟：」(`Experience.steps` reads only those). Over `maxChars`,
    /// steps go from the middle first (the first and last steps say the most), each gap marked; the
    /// original numbers stay.
    static func experienceText(task: String, summary: String, steps: [String], maxChars: Int) -> String {
        let numbered = steps.enumerated().map { "\($0.offset + 1). \($0.element)" }
        var kept = Array(numbered.indices)
        func render() -> String {
            var lines = ["經驗：\(task)", summary, "步驟："]
            var prev = -1
            for i in kept {
                if i > prev + 1 { lines.append("（省略 \(i - prev - 1) 步）") }
                lines.append(numbered[i])
                prev = i
            }
            if prev < numbered.count - 1 { lines.append("（省略 \(numbered.count - 1 - prev) 步）") }
            return lines.joined(separator: "\n")
        }
        var text = render()
        while text.count > maxChars, !kept.isEmpty {
            kept.remove(at: kept.count / 2)
            text = render()
        }
        return text.count > maxChars ? String(text.prefix(maxChars - 1)) + "…" : text
    }

    // memory -----------------------------------------------------------------
    /// Tools whose results are what the computer showed: a memory taken from one is tool-tier.
    static let observingTools: Set<String> = ["click", "type", "key", "scroll", "drag", "screenshot", "read_file", "edit_file"]

    /// The program fills seq, ts, confidence; the kind and folder are hers (spec 6, 2026-11-08). The
    /// tier comes from the event she is answering, not from anything she can claim; results of her
    /// own memory and think calls are not sources. Each item's entities come from the on-device
    /// model (2026-11-11), each aligned to the registry (`Selector.place`: an entry, or a new one the
    /// model describes); if any of that fails, that item is not stored.
    func remember(_ items: [Tools.Request.Item], seen events: [Event], reply: Event) async -> Outcome {
        let names = Self.callNames(events)
        let source = events.last { e in
            e.speaker != nil || e.kind == .watch || (e.kind == .tool && Self.observingTools.contains(names[e.seq!] ?? ""))
        }
        let tier: SourceTier = switch source?.kind {
        case .owner?: .owner
        case .developer?: .developer
        case .tool?, .watch?: .tool
        default: .doll
        }
        // With no source event she is the source: the reply this call belongs to.
        let seq = source?.seq ?? reply.seq!
        var lines: [String] = [], actions: [String] = []
        for (i, item) in items.enumerated() {
            let folder = memory.dirName(item.folder)
            do {
                if let bad = item.badKind {
                    throw ToolError(description: "kind 只能是 happened、intended、uncertain，或不填；收到 \(bad)")
                }
                var entities: [MemoryEntity] = [], placed: [String] = []
                for m in try await selector.entities(in: item.text) {
                    let (e, how) = try await pipeline.place(m, context: item.text)
                    entities.append(MemoryEntity(e.id, m.name))
                    placed.append("\(m.name)→\(e.id)「\(e.name)」 \(how)")
                }
                let id = try memory.write(item.text, folder: item.folder, sourceSeq: seq, tier: tier, kind: item.kind,
                                          entities: entities)
                log("INFO", "turn \(turnIndex): remember \(id) entities [\(placed.joined(separator: ", "))]")
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
    public static func callNames(_ events: [Event]) -> [Int64: String] {
        var out: [Int64: String] = [:]
        var pending: ArraySlice<ToolCall> = []
        for e in events {
            switch e.kind {
            case .doll: pending = e.calls[...]
            case .tool: if let call = pending.popFirst() { out[e.seq!] = call.name }
            case .owner, .developer, .watch, .memory: break
            }
        }
        return out
    }

    /// Whose turn this is: answering the developer or working on a task the developer gave (the
    /// developer spoke last, 2026-11-12), answering the owner, or her own work (woken by her watch).
    /// There is no stranger input yet (M6), so this timeline never yields `.stranger`.
    static func audience(_ events: [Event]) -> Audience {
        if lastSpeaker(events) == .developer { return .developer }
        switch events.last(where: { $0.kind == .owner || $0.kind == .watch })?.kind {
        case .owner?: return .owner
        default: return .doll
        }
    }

    /// Folders by `Memory.visible`: `secret` only if the turn saw secret events (5.1), `own` in the
    /// owner's turns and hers. Her memories and today's events (today's secret events only where the
    /// secret folder is visible) through the selection pipeline (2026-11-11, `Selector.run`): the
    /// names her query gives (on-device model) aligned to the registry, everything related, the
    /// `[select] prefilter` best by the cheap score, the on-device model's score for each; those at
    /// or above `[select] threshold`, best first. No fixed count; none is 「0 條」.
    func recall(_ query: String, seen events: [Event]) async throws -> Outcome {
        let visible = Memory.visible(Self.audience(events), sawSecret: events.contains(where: \.secret))
        var clock = ContinuousClock.now
        let mentions = try await selector.entities(in: query)
        let trigger = Selector.Trigger(label: "recall「\(query)」", query: query, mentions: mentions, app: nil,
                                       situation: situation("她用 recall 查「\(query)」", events: events),
                                       extractSeconds: lap(&clock))
        let report = try await pipeline.run(trigger, visible: visible, tiers: nil, today: today) { c in
            (c.key, Self.selectionText(c.hit))
        }
        logSelection(report)
        let hits = report.selected.map(\.candidate.hit)
        var out = "\(query)：\(hits.count) 條"
        for hit in hits {
            out += "\n\n" + hit.rendered
        }
        // A secret hit makes the result a secret event (5.1: what the turn saw is what it may say).
        return Outcome(text: out, secret: hits.contains(where: \.secret), actions: ["recall \(hits.count)"])
    }

    /// A candidate as the on-device model reads it.
    static func selectionText(_ hit: RecallHit) -> String {
        switch hit {
        case .memory(let h): h.text
        case .today(let h): "（\(h.tag)）\(h.text)"
        }
    }

    /// What the on-device model is told about now: why memories are being looked up, the last owner
    /// or developer line, and the front app, window title and focused element of the last computer result.
    func situation(_ why: String, events: [Event]) -> String {
        var lines = ["- 為什麼查記憶：\(why)"]
        if let line = events.last(where: { $0.speaker != nil }) { lines.append("- \(line.speaker!.label)最後說：「\(line.text)」") }
        if let app = frontApp { lines.append("- 前景 app：\(app.name)") }
        if let t = frontTitle { lines.append("- 前景視窗：「\(t)」") }
        if let f = frontFocus { lines.append("- 焦點：\(f)") }
        return lines.joined(separator: "\n")
    }

    /// One selection's line: to the log and the turn's report.
    func logSelection(_ report: SelectionReport) {
        log("INFO", "turn \(turnIndex): " + report.line)
        selectionsNow.append(report.line)
    }

    /// The program's own recall (spec 3.1, 2026-11-09; registry and scoring 2026-11-11): on a new
    /// owner line and when a computer result's front app changed, `Selector.run` with the owner's
    /// last line plus the front app's name as the query; the names the owner's line gives (on-device
    /// model, once per line), the front window's title and the focused element as the trigger's
    /// names, the front app aligned by its bundle id. Experiences (tier self_reported) and
    /// consolidated memories only (her other memories and the program's shortcuts stay for
    /// `recall`), in the turn's visible folders. Experiences for one task are one group
    /// (`Memory.experienceGroups`): only its best-ranked one (`Memory.ranked`) is a candidate; a
    /// consolidated memory is its own group. Those attached since the owner's last line are left out.
    /// Every one scored at or above `[select] threshold` becomes part of one 【相關記憶】 event, best
    /// score first, re-sent byte-identical like every event. None, no event. Each goes in whole (an
    /// experience's length is capped when written), an experience's stats line first. A failure is
    /// logged and attaches nothing; the turn goes on.
    func attachMemories(trigger label: String) async {
        do {
            let events = timeline.events
            guard let owner = events.last(where: { $0.speaker != nil }) else { return }
            let query = [owner.text, frontApp?.name].compactMap { $0 }.joined(separator: " ")
            let visible = Memory.visible(Self.audience(events), sawSecret: events.contains(where: \.secret))
            var clock = ContinuousClock.now
            if ownerMentions?.seq != owner.seq {
                ownerMentions = (owner.seq!, try await selector.entities(in: owner.text))
            }
            var mentions = ownerMentions!.list
            if let t = frontTitle { mentions.append(Mention(.other, t)) }
            if let f = frontFocus { mentions.append(Mention(.ui, f)) }
            let trigger = Selector.Trigger(label: label, query: query, mentions: mentions, app: frontApp,
                                           situation: situation(label, events: events), extractSeconds: lap(&clock))
            let groups = try memory.experienceGroups(try memory.experiences(visible: visible))
            var picks: [String: Experience] = [:], already = Set<String>(), merged = Set<String>()
            let report = try await pipeline.run(trigger, visible: visible, tiers: [.selfReported, .consolidated], today: nil) { c in
                let pick: Experience
                if let m = try memory.consolidated(c.id) {
                    pick = m
                } else if let g = groups.first(where: { $0.contains { $0.id == c.id } }) {
                    let ranked = Memory.ranked(g)
                    pick = ranked[0]
                    if ranked.count > 1 {
                        merged.insert("[" + ranked.map { e in
                            e.id + (e.stats.map { "(\($0.turns) 回合, 照做 \($0.attachedFollowed), 成功 \($0.attachedSuccess), 失敗 \($0.attachedFailed))" } ?? "(no stats)")
                        }.joined(separator: " > ") + "]")
                    }
                } else { return nil }
                if attachedIDs.contains(pick.id) { already.insert(pick.id); return nil }
                picks[pick.id] = pick
                return ("m:" + pick.id, pick.file.body)
            }
            log("INFO", "turn \(turnIndex): 相關記憶（\(label)）query「\(query)」: task groups \(merged.sorted().joined(separator: " ")), "
                + "already attached \(already.count) \(already.sorted())")
            logSelection(report)
            let fresh = report.selected.compactMap { picks[$0.candidate.id] }
            guard !fresh.isEmpty else { return }
            attachedIDs.formUnion(fresh.map(\.id))
            let from = frontApp.map { "主人的話和前景 app「\($0.name)」" } ?? "主人的話"
            var text = "【相關記憶】程式用\(from)查到、判斷用得上的過去經驗："
            for e in fresh {
                text += "\n〔\(e.label)｜\(e.folder)〕\n\(e.attachedText)"
            }
            try append(Event(.memory, .internal, text, secret: fresh.contains(where: \.secret)))
            attachedNow.append(text)
        } catch {
            log("ERROR", "turn \(turnIndex): 相關記憶（\(label)）failed: \(error)")
        }
    }

    /// Her experiences in their groups, each best first (as attaching ranks them): one line per
    /// experience, `id: stats line` (or `id: no stats`). For reports.
    public func experienceRanking() throws -> [[String]] {
        try memory.experienceGroups(try memory.experiences(visible: Set(MemoryFolder.allCases))).map { g in
            Memory.ranked(g).map { "\($0.id): " + ($0.stats.map { "\($0.line.prefix { $0 != "（" })；失敗 \($0.attachedFailed) 次" } ?? "no stats") }
        }
    }

    /// At a session's end, each experience attached since the owner's last line: the share of its
    /// steps she did in order this session (`matchedSteps`). At least `experience_follow_ratio` is
    /// followed, and a followed one counts her outcome in its stats (`attached_followed` and
    /// `attached_success` or `attached_failed`); an unknown outcome is only logged.
    func scoreAttached(steps done: [String], outcome: String) {
        for id in attachedIDs.sorted() {
            do {
                guard let e = try memory.experience(id) else { continue }
                let want = e.steps
                let matched = Self.matchedSteps(want, done)
                let ratio = want.isEmpty ? 0 : Double(matched) / Double(want.count)
                let followed = ratio >= memoryConfig.experienceFollowRatio
                var line = "experience \(id): \(matched)/\(want.count) steps done in order (ratio \(String(format: "%.2f", ratio)))"
                if followed, outcome != "unknown" {
                    let stats = try memory.updateStats(id) { s in
                        s.attachedFollowed += 1
                        if outcome == "success" { s.attachedSuccess += 1 } else { s.attachedFailed += 1 }
                    }
                    line += stats.map { s in
                        ", followed, counted \(outcome): 照做 \(s.attachedFollowed)、成功 \(s.attachedSuccess)、失敗 \(s.attachedFailed)"
                    } ?? ", followed; its file has no stats, nothing counted"
                } else {
                    line += followed ? ", followed; outcome unknown, nothing counted" : ", not followed"
                }
                log("INFO", "turn \(turnIndex): \(line)")
                followsNow.append(line)
            } catch {
                log("ERROR", "turn \(turnIndex): scoring experience \(id) failed: \(error)")
            }
        }
    }

    /// How many of `want` appear in order among `done` (longest common subsequence under `sameStep`).
    static func matchedSteps(_ want: [String], _ done: [String]) -> Int {
        guard !want.isEmpty, !done.isEmpty else { return 0 }
        let a = want.map(StepShape.init), b = done.map(StepShape.init)
        var row = Array(repeating: 0, count: b.count + 1)
        for i in 0..<a.count {
            var prev = 0   // row[j] of the previous i
            for j in 0..<b.count {
                let up = row[j + 1]
                row[j + 1] = a[i].matches(b[j]) ? prev + 1 : max(up, row[j])
                prev = up
            }
        }
        return row[b.count]
    }
}

/// One experience step (`Doll.step`'s line) parsed for matching, the Experience2 analysis's loose
/// rule: a click matches by the same element or text name, or a point within 40 px, with the same
/// button; any double-click matches any double-click; keys case-insensitive; typed text exactly;
/// a file tool by its path alone (write_file's line count aside); anything else by its whole line.
enum StepShape {
    enum Target { case point(Int, Int), name(String), role(String) }
    case click(Target, button: String, double: Bool)
    case key(String)
    case type(String)
    case file(tool: String, path: String)
    case other(String)

    static let fileTools = ["read_file", "write_file", "edit_file"]

    init(_ line: String) {
        if line.hasPrefix("key ") {
            // Experiences written before `key` took modifiers (2026-11-09) may say `shift+cmd+s`:
            // put each chord's modifiers in the order `key` writes them now, so it is the same step.
            let chords = line.dropFirst(4).trimmingCharacters(in: .whitespaces).lowercased().split(separator: " ").map { chord in
                let parts = chord.split(separator: "+").map(String.init)
                return parts.count > 1 ? Tools.chord(key: parts.last!, modifiers: Array(parts.dropLast())) : String(chord)
            }
            self = .key(chords.joined(separator: " "))
            return
        }
        if line.hasPrefix("type") {
            var t = line.dropFirst(4).trimmingCharacters(in: .whitespaces)
            if t.hasPrefix("「"), t.hasSuffix("」"), t.count >= 2 { t = String(t.dropFirst().dropLast()) }
            self = .type(t)
            return
        }
        for tool in Self.fileTools where line.hasPrefix(tool + " ") {
            var path = line.dropFirst(tool.count + 1).trimmingCharacters(in: .whitespaces)
            if tool == "write_file", path.hasSuffix("行）"), let open = path.lastIndex(of: "（") { path = String(path[..<open]) }
            self = .file(tool: tool, path: path)
            return
        }
        guard line.hasPrefix("click") else { self = .other(line); return }
        var rest = line.dropFirst(5).trimmingCharacters(in: .whitespaces)
        var double = false, button = "left"
        if rest.hasSuffix(" 雙擊") || rest == "雙擊" { double = true; rest = String(rest.dropLast(2)).trimmingCharacters(in: .whitespaces) }
        for b in ["right", "middle"] where rest.hasSuffix(" " + b) {
            button = b
            rest = String(rest.dropLast(b.count)).trimmingCharacters(in: .whitespaces)
        }
        let xy = rest.split(separator: ",")
        if xy.count == 2, let x = Int(xy[0]), let y = Int(xy[1]) {
            self = .click(.point(x, y), button: button, double: double)
        } else if rest.hasPrefix("「"), rest.hasSuffix("」"), rest.count >= 2 {
            self = .click(.name(String(rest.dropFirst().dropLast())), button: button, double: double)
        } else if let open = rest.firstIndex(of: "\""), let close = rest.lastIndex(of: "\""), open < close {
            self = .click(.name(String(rest[rest.index(after: open)..<close])), button: button, double: double)
        } else {
            self = .click(.role(rest), button: button, double: double)
        }
    }

    func matches(_ o: StepShape) -> Bool {
        switch (self, o) {
        case let (.click(t1, b1, d1), .click(t2, b2, d2)):
            if d1 || d2 { return d1 && d2 }
            guard b1 == b2 else { return false }
            switch (t1, t2) {
            case let (.point(x1, y1), .point(x2, y2)):
                let dx = Double(x1 - x2), dy = Double(y1 - y2)
                return (dx * dx + dy * dy).squareRoot() <= 40
            case let (.name(n1), .name(n2)): return n1 == n2
            case let (.role(r1), .role(r2)): return r1 == r2
            default: return false
            }
        case let (.key(k1), .key(k2)): return k1 == k2
        case let (.type(t1), .type(t2)): return t1 == t2
        case let (.file(f1, p1), .file(f2, p2)): return f1 == f2 && p1 == p2
        case let (.other(l1), .other(l2)): return l1 == l2
        default: return false
        }
    }
}
