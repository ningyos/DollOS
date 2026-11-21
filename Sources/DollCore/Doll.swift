import Foundation

/// The skeleton: one queue, one consumer, one timeline, one line (the owner's).
/// The actor is the single serial owner of Doll state; everything durable is in the timeline
/// files (`Timeline`) and the memory files, SQLite only holds their indexes.
///
/// A turn = one model call. The loop only takes a turn when the queue holds an event
/// (owner voice, owner text, a watch wake) or her tool results await her; otherwise it sleeps
/// and makes no model call.

/// Delivers one text of hers to a person's stream (the owner's Discord DM through the bridge, the
/// developer's `dollctl chat`), with a screenshot file to attach (design B, 2026-11-16; only to the
/// owner): returns nil, or says why it could not be delivered (her `discord.dm` / `dollctl.reply`
/// result then tells her so).
public typealias Deliver = @Sendable (String, Addressee, URL?) -> String?

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
    /// One line per memory selection this turn (【相關記憶】 and `recall`, 2026-11-11): the trigger's
    /// names aligned, related counts by channel, the ranking, what was given, seconds per stage.
    public var selections: [String] = []
}

/// Her process's log: stderr as `2026-11-14 14:05:03 INFO …`, and the log file once attached (`RunLog.swift`).
public enum Log {
    public static func write(_ level: String, _ msg: String) {
        let now = Date().timeIntervalSince1970
        FileHandle.standardError.write(Data("\(LogFile.timeText(now)) \(level) \(msg)\n".utf8))
        sink.withLock { $0 }?.write(LogEntry(ts: now, level: level, msg: msg))
    }
}

private func clock(_ ts: Double) -> String {
    let c = Calendar.current.dateComponents([.month, .day, .hour, .minute, .second], from: Date(timeIntervalSince1970: ts))
    return String(format: "%02d-%02d %02d:%02d:%02d", c.month!, c.day!, c.hour!, c.minute!, c.second!)
}

/// Now, as 【狀況】 says it: with the year and the weekday (2026-11-15: given only `11-15` she took the
/// year for 2025, got the weekday wrong and worked out 「下週三」 from that).
private func nowLine(_ ts: Double) -> String {
    let c = Calendar.current.dateComponents([.year, .month, .day, .weekday, .hour, .minute, .second], from: Date(timeIntervalSince1970: ts))
    return String(format: "%04d-%02d-%02d（星期%@）%02d:%02d:%02d", c.year!, c.month!, c.day!,
                  ["日", "一", "二", "三", "四", "五", "六"][c.weekday! - 1], c.hour!, c.minute!, c.second!)
}

private func hex(_ n: Int) -> String {
    String(UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased().prefix(n))
}

public actor Doll {
    public nonisolated let name: String
    let identity: String
    let core: CoreConfig
    let contextConfig: ContextConfig
    let llm: LlmConfig
    let computerConfig: ComputerConfig
    /// `[computer] enabled` (2026-11-19): off, she has no computer, file or saved tools; a call to one
    /// is an unknown tool, and no rule or line of the program mentions them.
    var computerOn: Bool { computerConfig.enabled }
    /// The tools that run only while an item is 進行中, and the window actions: none with the computer off.
    var gatedTools: Set<String> { computerOn ? Tools.gated : [] }
    var actionTools: Set<String> { computerOn ? Tools.actionNames : [] }
    let filesConfig: FilesConfig
    let idle: IdleConfig
    let memoryConfig: MemoryConfig
    let selectConfig: SelectConfig
    /// Screenshot files; events name them, and each is re-sent byte-identical every turn.
    public nonisolated let shots: URL
    /// One JSON boot report per boot (2026-11-18, `BootReport.swift`): `<data_dir>/boot-reports/`.
    public nonisolated let bootReports: URL
    /// Today's timeline segment, `<data_dir>/timeline/<segment>.jsonl`; replaced when the daily
    /// maintenance consolidates the day (`Maintenance.swift`).
    var timeline: Timeline
    let timelineDir: URL
    /// Today's events for `recall`, in memory (2026-11-11): built from the segment at start, fed on every append.
    var today: TodayIndex
    let embedder: any Embedder
    let memory: Memory
    let model: any MainModel
    /// The maintenance's model requests (consolidation: `[consolidate]` max_tokens and timeout).
    let maintenanceModel: any MainModel
    /// The whole config, for the maintenance's tasks (consolidation reads several sections).
    let config: Config
    /// The memory selection over `memory`, and the registry entries the program knows exactly.
    let pipeline: Selector
    /// Her computer: this Mac through cua-driver (2026-11-16).
    let mac: Mac
    let deliver: Deliver
    /// `[voice] enabled` (M6): the voice tools and the voice part of the system prompt.
    let voiceEnabled: Bool
    /// The voice channel (`setVoice`); nil while voice is disabled or not wired yet.
    var voice: (any VoicePort)?
    /// A `voice.say` was cut by the owner this turn: her later `voice.say` calls in the same reply
    /// are not spoken (she would talk over him).
    var voiceCut = false

    var queue: [Event] = []
    /// Owner lines being checked for a secret (`ownerLine`), not queued yet; the last check in the chain.
    var heldLines = 0
    var lineChecks: Task<Void, Never>?
    var waiter: CheckedContinuation<Bool, Never>?
    var stopped = false
    var inflight: Task<ModelReply, any Error>?
    var turnIndex = 0
    /// Consecutive turns without a tool call.
    var idleTurns = 0
    /// The owner or developer line (seq) she was last told was left unanswered: told once per line.
    var remindedLine: Int64?
    /// Set by `think`; consumed by the next model call.
    var effortNext: String?
    /// The next call is part of a task: the events drained since her last answered call include an
    /// owner line, a watch wake or 動力's wandering (her own thing, which asks for a step), or her
    /// tool results await her. It uses `task_turn_effort` unless she set one with `think`. Tool-result
    /// and wake turns count too (spec 3.1, 2026-11-08): at effort none, 2 of 5 M2 medium C runs
    /// stopped mid-task on a tool-result turn with one sentence; idle-drive's real check (2026-11-20),
    /// her first own-thing turn at none: 28 tokens, "先看一下全貌", no call.
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
    /// Her DollOS documents (`[core] docs`), read in place: `docsPath` is this folder's absolute path.
    let docsDir: URL
    var docsPath: String { docsDir.standardizedFileURL.path }
    /// Her todo list (`Todo.swift`), as the timeline last recorded it.
    var todo: TodoState
    /// The list set aside beside it (`TodoAside.swift`, 2026-11-20): at most one, as the timeline
    /// last recorded it; what was in memory for it (its action list, whom it answers) in `asideMemory`.
    var aside: ListAside?
    var asideMemory = ListMemory()
    /// A person's line or a watch wake brought the waiting list back in hand for this turn
    /// (`Doll.drain`); her own list goes back in hand after it unless she took the waiting one up.
    var swappedForLine = false
    /// Her ending call on the waiting item was held in that turn (`currentItem`): the waiting list
    /// stays in hand for her next turn, in which the same call goes through (`heldAt`: the turn).
    var heldForAnswer = false
    var heldAt = 0
    static let heldTools: Set<String> = ["todo.done", "todo.fail", "todo.progress", Tools.Name.todoContinue]
    /// Her finished lists, estimate against actual (2026-11-17, hook 2): `<data_dir>/estimates.json`.
    let estimatesURL: URL
    var estimateFile: EstimateFile
    /// The estimate refusal given for the list about to start (2026-11-18): its text, the check, and
    /// whether a reason was already asked for. The next `todo.set` with the same text is the second
    /// attempt. In memory only: after a restart the first attempt is refused again.
    var estimateRefusal: (text: String, check: EstimateCheck, reasonAsked: Bool)?
    /// Vectors of list texts (digits masked) already embedded, so past lists are embedded once per process.
    var listVectors: [String: [Float]] = [:]
    /// The current item's actions that went through, one short line each (`Doll.step`) with the seq
    /// of the result that answered it: its action list and its experience's steps. In memory: a new
    /// process starts the list of an item still in progress empty.
    var itemSteps: [(seq: Int64, line: String)] = []
    /// The front apps her computer results showed during the current item, first seen first: an
    /// experience's entities (2026-11-11).
    var itemApps: [AppIdentity] = []
    /// The current item's `open` and `act` calls as they went (2026-11-17): what `tool.save` builds
    /// her tool from. In memory, like `itemSteps`.
    var itemChain: [ChainPart] = []
    /// The item that ended last, with its chain: what `tool.save` saves when no item is in progress
    /// (the owner asks to save it after her report). In memory: a new process has none.
    var lastEnded: (item: String, task: String, done: Bool, chain: [ChainPart])?
    /// Her own tools on disk (`<data_dir>/tools/`).
    let toolStore: ToolStore
    /// The tools offered since the owner's last line; never offered twice in that span.
    var offeredTools: Set<String> = []
    /// The current item called a computer tool that ran.
    var itemUsedComputer = false
    /// The current item saw a call of hers other than the todo tools and `think` (a computer tool,
    /// a line to someone, remember, recall): what `todo.done` needs before it is accepted.
    var itemActed = false
    /// The person whose line the current list answers and the channel that line came on; nil when
    /// the list is her own (no person line was waiting for an answer when she set it). The
    /// `todo.done` that would finish the list needs a line of hers to that person after the reply
    /// that set the list (2026-11-16, m7-mac: in 3 of 4 Calculator runs the answer went only into
    /// todo.done's result, which no one else sees, sometimes with 「已用 discord.dm 回報」 in it).
    var listFor: (to: Addressee, channel: Channel)?
    /// The reply that set the current list (a line in that same reply is an acknowledgement, not the result).
    var listReply: Int64 = 0
    /// She spoke to `listFor` after `listReply`.
    var listReported = false
    /// Whom the calls written after the current one in this reply speak to: `todo.done` followed by
    /// her line to that person in the same reply is a report too (speaking after an ending call has
    /// always run, 2026-11-11), so it costs no extra round.
    var speaksLater: Set<Addressee> = []
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
    /// The app of the last computer result; nil before the first.
    var frontApp: AppIdentity?
    /// A computer result this turn worked on an app other than the one before: attach memories.
    var appSwitched = false
    /// An owner line was drained since her last turn: attach memories, after forgetting what was attached.
    var ownerArrived = false
    /// The memories attached since the owner's last line; never attached twice in that span.
    var attachedIDs: Set<String> = []
    /// This turn's 【相關記憶】 events, for its report.
    var attachedNow: [String] = []
    /// This turn's selection lines, for its report.
    var selectionsNow: [String] = []
    /// Her computer calls that failed, in a row, as `name + arguments` → how many times (2026-11-16:
    /// the same failing step was retried for 15 rounds); reset by any computer call that succeeds.
    var failedCalls: [String: Int] = [:]
    /// What `todo.done`'s evidence is matched against (2026-11-16, design A): the latest window read
    /// (its element list, the head line left out) or the file of the latest file tool, whichever came
    /// last in the current item. In memory: a new process, or a new item, starts with none.
    enum LatestRead { case window(String), file(String) }
    var latestRead: LatestRead?
    /// The screenshot of her latest window read, for her next line to the owner (design B); cleared
    /// once a line carried it, and after the turn in which the item ended.
    var ownerShot: String?
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
    /// `[calendar]` (2026-11-15).
    let calendarConfig: CalendarConfig
    /// `[owner_sleep]` (2026-11-16): the 「主人睡覺」 span (`Calendar.swift`, `ownerSleepLine`).
    let ownerSleepConfig: OwnerSleepConfig
    /// Her calendar, `<data_dir>/calendar.json` (`Calendar.swift`).
    let agenda: Agenda
    /// "Now" for the calendar and 【狀況】; tests move it (`setClock`).
    var wallClock: any DollClock = SystemClock()
    /// The one task sleeping until the next timed start; replaced on every edit.
    var calendarTask: Task<Void, Never>?
    var calendarSerial = 0
    /// Missed starts were dealt with (boot notice or `run`): the timer may start.
    var calendarCaughtUp = false
    /// Program notices that give no turn (the owner's calendar edits), held while a turn runs so
    /// nothing lands between her reply and its results; appended when the turn ends.
    var held: [Event] = []
    /// `[maintenance]` (2026-11-16, `Maintenance.swift`).
    let maintenanceConfig: MaintenanceConfig
    /// `<data_dir>/maintenance.json` and what it holds.
    let maintenanceURL: URL
    var maintenanceState: MaintenanceFile
    /// The task running the maintenance's tasks; cancelled when it is interrupted.
    var maintenanceTask: Task<Void, Never>?
    var maintenanceSerial = 0
    /// The one task waiting for the retry or the busy limit.
    var maintenanceTimer: Task<Void, Never>?
    var maintenanceTimerSerial = 0
    /// When the owner or the developer last spoke to her (the clock's time; at start, the segment's last line).
    var lastSpoke: Double = 0
    /// When the owner last spoke to her (the clock's time; at start, the segment's last owner line).
    var ownerLastLine: Double = 0
    /// `[drives]` (m6-drives, 2026-11-15, `Concerns.swift`, `Signals.swift`).
    let drivesConfig: DrivesConfig
    let concernsURL: URL
    let signalsURL: URL
    var concernFile: ConcernFile
    /// Corrections of her memories and the audit's flags (2026-11-18, `MemoryFix.swift`).
    let fixesURL: URL
    var fixFile: MemoryFixFile
    let auditConfig: AuditConfig
    var drives: Drives
    /// No signals file yet: every signal starts at its start value when the calendar is first
    /// caught up (the clock then is the right one).
    var signalsFresh = false
    /// The one task sleeping until the next signal crossing or concern change.
    var drivesTask: Task<Void, Never>?
    var drivesSerial = 0
    /// When 動力's wandering began the episode still open (`endOwnEpisode`); nil after a restart.
    var ownEpisodeStart: Double?
    /// Concerns surfaced or associated since the owner's last line; never associated twice in that span.
    var associatedIDs: Set<Int> = []
    /// Concern id → its text's embedding, computed on first use.
    var concernVectors: [Int: [Float]] = [:]
    /// Concerns noted since the last turn began: not associated in the next one.
    var concernsNew: Set<Int> = []
    /// Her tool calls that failed in a row (any call that went through resets it).
    var errorsInRow = 0
    /// This turn's 【聯想】 lines.
    var associatedNow: [String] = []
    /// A concern held by the wandering just queued, then by the turn it gave her (`Concerns.swift`).
    var heldPending: Int?
    var heldTurn: Int?
    /// `[goals]` (m6-drives phase 2, `Goals.swift`): her ideal self, goal tree, record, curiosity.
    let goalsConfig: GoalsConfig
    let goalsURL: URL
    let recordURL: URL
    let idealURL: URL
    let idealLogURL: URL
    let curiosityURL: URL
    var goalFile: GoalFile
    var recordFile: RecordFile
    var idealLog: IdealLog
    var curiosity: CuriosityFile
    /// Her ideal self as it is now (empty with drives off).
    var ideal = ""
    /// The pack's name and aliases: what the ideal self may not rename.
    var packNames: [String] = []
    /// 【狀況】's felt sentence; nil when every signal is in range (or drives are off).
    func feltLine(now: Double) -> String? { drivesConfig.enabled ? drives.felt(at: now) : nil }
    /// The history of what is hers (2026-11-19, `History.swift`).
    let history: History
    let historyConfig: HistoryConfig
    /// What opening the history found at this boot (for the boot report).
    public nonisolated let historyBoot: History.Boot
    /// Feature records made since the last commit (`ideal#3`, `fix#2`): the next commit carries them.
    var historyRefs: [String] = []

    /// The memory key (`SecretBox`, owner 2026-11-21), after looking at the disk without it: a plain
    /// file in the `secret` or `own` folder, or a secret timeline event stored in plain text, is
    /// plaintext that is no longer allowed and fails the boot with its place. The key is the one in
    /// `keys` for the data dir (made on first boot); encrypted secrets with no key fail the boot.
    public static func unlockSecrets(keys: any KeyStore, data: URL, memRoot: URL, memory: MemoryConfig, timeline: URL) throws -> SecretBox {
        var sealed: String?
        let fm = FileManager.default
        for dir in [memory.secretDir, memory.ownDir] {
            let folder = memRoot.appending(path: dir)
            for name in ((try? fm.contentsOfDirectory(atPath: folder.path)) ?? []).sorted() where name.hasSuffix(".md") {
                let url = folder.appending(path: name)
                guard SecretBox.isSealed(try String(contentsOf: url, encoding: .utf8)) else {
                    throw SecretBoxError("\(url.path) is a plain-text memory file in an encrypted folder (\(dir)); plain secrets are no longer allowed (no conversion: remove it)")
                }
                if sealed == nil { sealed = url.path }
            }
        }
        let scan = try Timeline.scan(dir: timeline)
        if let plain = scan.plain {
            throw SecretBoxError("\(plain) is a secret event stored in plain text; plain secrets are no longer allowed (no conversion: remove it)")
        }
        let account = SecretBox.account(data: data)
        return try SecretBox.unlock(store: keys, account: account, sealedPlace: sealed ?? scan.sealed)
    }

    /// `embedder` is loaded by the caller before this (`MLXEmbedder.load`; tests: a fake): the today
    /// index embeds the segment's events here. `maintenanceModel`: the maintenance's requests; nil = `model`
    /// (tests script one model for both).
    public init(config: Config, root: URL, model: any MainModel,
                embedder: any Embedder, computer: (any ComputerDriver)? = nil, maintenanceModel: (any MainModel)? = nil,
                keys: any KeyStore, deliver: @escaping Deliver) throws {
        self.config = config
        maintenanceConfig = config.maintenance
        self.embedder = embedder
        self.maintenanceModel = maintenanceModel ?? model
        core = config.core
        contextConfig = config.context
        llm = config.llm
        computerConfig = config.computer
        filesConfig = config.files
        idle = config.idle
        memoryConfig = config.memory
        selectConfig = config.select
        calendarConfig = config.calendar
        ownerSleepConfig = config.ownerSleep
        let data = config.dataDirectory(root: root)
        let memRoot = memoryConfig.dir.hasPrefix("/") ? URL(filePath: memoryConfig.dir) : root.appending(path: memoryConfig.dir)
        shots = data.appending(path: "shots")
        bootReports = data.appending(path: "boot-reports")
        try FileManager.default.createDirectory(at: shots, withIntermediateDirectories: true)
        timelineDir = data.appending(path: "timeline")
        let box = try Self.unlockSecrets(keys: keys, data: data, memRoot: memRoot, memory: config.memory, timeline: timelineDir)
        timeline = try Timeline(dir: timelineDir, box: box)
        maintenanceURL = data.appending(path: "maintenance.json")
        maintenanceState = try Self.readMaintenance(maintenanceURL) ?? MaintenanceFile()
        lastSpoke = timeline.events.last { $0.speaker != nil }?.ts ?? 0
        ownerLastLine = timeline.events.last { $0.kind == .owner }?.ts ?? 0
        drivesConfig = config.drives
        concernsURL = data.appending(path: "concerns.json")
        signalsURL = data.appending(path: "signals.json")
        estimatesURL = data.appending(path: "estimates.json")
        fixesURL = data.appending(path: "memory-fixes.json")
        fixFile = try MemoryFixFile.read(fixesURL, box: box) ?? MemoryFixFile()
        auditConfig = config.audit
        toolStore = ToolStore(dir: data.appending(path: "tools"))
        estimateFile = try Concerns.read(EstimateFile.self, estimatesURL, what: "estimates") ?? EstimateFile()
        concernFile = try Concerns.read(ConcernFile.self, concernsURL, what: "concerns") ?? ConcernFile()
        let signals = try Concerns.read(SignalState.self, signalsURL, what: "signals")
        drives = signals.map { Drives(config: config.drives, state: $0) } ?? Drives(config: config.drives, now: 0)
        signalsFresh = signals == nil
        goalsConfig = config.goals
        goalsURL = data.appending(path: "goals.json")
        recordURL = data.appending(path: "record.json")
        idealURL = data.appending(path: "ideal-self.md")
        idealLogURL = data.appending(path: "ideal-self-log.json")
        curiosityURL = data.appending(path: "curiosity.json")
        goalFile = try Concerns.read(GoalFile.self, goalsURL, what: "goal tree") ?? GoalFile()
        recordFile = try Concerns.read(RecordFile.self, recordURL, what: "record") ?? RecordFile()
        idealLog = try Concerns.read(IdealLog.self, idealLogURL, what: "ideal-self log") ?? IdealLog()
        curiosity = try Concerns.read(CuriosityFile.self, curiosityURL, what: "curiosity") ?? CuriosityFile()
        agenda = try Agenda(url: data.appending(path: "calendar.json"))
        memory = try Memory(config: memoryConfig, root: memRoot, store: try Store(path: data.appending(path: "doll.db")),
                            embedder: embedder, box: box)
        historyConfig = config.history
        history = try History(config: config.history, data: data, memory: memoryConfig, memoryDir: memRoot, box: box)
        pipeline = Selector(memory: memory, config: config.select)
        // Before anything is appended: every event of the segment so far.
        today = try TodayIndex(timeline, embedder: embedder)
        self.model = model
        // One session label per process (cua-driver's element tokens are scoped to it).
        mac = Mac(driver: computer ?? CuaDriverCLI(path: config.computer.driver), config: config.computer,
                  session: "dollcore-\(ProcessInfo.processInfo.processIdentifier)-\(Int(Date().timeIntervalSince1970))",
                  shots: shots, log: data.appending(path: "computer-actions.jsonl"))
        self.deliver = deliver
        tools = Tools.specs(memory: memoryConfig, voice: config.voice.enabled, computer: config.computer, files: config.files,
                            drives: config.drives.enabled)
        voiceEnabled = config.voice.enabled
        toolsJSON = Tools.json(tools)
        todo = timeline.todo
        aside = timeline.todoAside
        docsDir = core.docs.hasPrefix("/") ? URL(filePath: core.docs) : root.appending(path: core.docs)
        // She reads them in place: a docs folder without its index is a boot error.
        guard FileManager.default.fileExists(atPath: docsDir.appending(path: "README.md").path) else {
            throw TOMLError(description: "[core] docs: \(docsDir.path)/README.md does not exist")
        }

        // An absolute path is taken as it is, like `docs` and `data_dir` (2026-11-19: it was appended to the config's folder).
        let packPath = core.pack.hasPrefix("/") ? URL(filePath: core.pack) : root.appending(path: core.pack)
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
        var names = [name]
        if case .array(let a)? = pack["meta"]?["aliases"] { for case .string(let s) in a { names.append(s) } }
        packNames = names
        // The ideal self (phase 2): from its file, else the pack's [identity].ideal (written once).
        if config.drives.enabled {
            if FileManager.default.fileExists(atPath: idealURL.path) {
                ideal = try String(contentsOf: idealURL, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
                guard !ideal.isEmpty else { throw StoreError(description: "\(idealURL.path) is empty") }
            } else {
                guard case .string(let s)? = pack["identity"]?["ideal"] else {
                    throw TOMLError(description: "\(packPath.path): [identity].ideal must be a string ([drives] enabled: her ideal self starts from it)")
                }
                ideal = s.trimmingCharacters(in: .whitespacesAndNewlines)
                idealLog.revisions.append(IdealRevision(id: idealLog.nextId, ts: Date().timeIntervalSince1970, by: "pack", before: "",
                                                        after: ideal, why: "角色包的起點", basedOn: core.pack))
                idealLog.nextId += 1
                try Data((ideal + "\n").utf8).write(to: idealURL, options: .atomic)
                try Concerns.write(idealLog, idealLogURL)
            }
        }
        // Last, after every file the boot itself writes: the first commit of a new history is the
        // dirs as found; an existing one gets what changed with no commit since.
        historyBoot = try history.open(ts: Date().timeIntervalSince1970)
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
    /// What each turn took and why it stopped, keyed by her reply's seq (for runners and transcripts).
    public func turnStats() -> [Int64: Timeline.TurnStats] { timeline.turnStats }

    /// Appends one event to the timeline file, then to the today index, then tells the dashboard.
    @discardableResult
    /// `ev`'s time is the doll's clock (`wallClock`) when it is appended; a queued event keeps the
    /// time `put` gave it (when it arrived), and so does one moved to a new segment (`keepTime`).
    func append(_ ev: Event, keepTime: Bool = false) throws -> Event {
        var ev = ev
        if !keepTime { ev.ts = wallClock.now() }
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
    /// Checks the model (unreachable = error, no fallback).
    public func boot() async throws {
        // Memories written by a previous process exist only as markdown until indexed (spec 6); the
        // encrypted folders' index is built in memory every boot (2026-11-21).
        let t0 = Date()
        let sealed = try memory.syncOnBoot()
        log("INFO", String(format: "memory: index ready in %.2f s (%d memories of the encrypted folders decrypted and indexed in memory)",
                           Date().timeIntervalSince(t0), sealed))
        try await model.check()
    }

    // inputs -----------------------------------------------------------------
    func put(_ ev: Event) {
        var ev = ev
        ev.ts = wallClock.now()
        queue.append(ev)
        feed.notify(.queue)
        if let w = waiter {
            waiter = nil
            w.resume(returning: true)
        }
    }

    /// Owner voice is the only input that cancels a model call in flight (3.1); the event is
    /// queued first so the rerun sees it. During maintenance the line interrupts it first (`Maintenance.swift`).
    public func ownerSaid(_ text: String, secret: Bool = false, voice: Bool = false, channel: Channel = .discordDM) {
        interruptMaintenance(by: "主人")
        lastSpoke = wallClock.now()
        ownerLastLine = lastSpoke
        signal(.ownerTalk, secret ? "（一句秘密，不記）" : "「\(text.prefix(30))」")
        var ev = Event(.owner, .owner, text, secret: secret, voice: voice)
        ev.channel = channel
        put(ev)
        if voice { inflight?.cancel() }
    }

    /// The developer typed a line in `dollctl chat` (2026-11-12). Never secret; never cancels a
    /// call. During maintenance it interrupts it like the owner's.
    public func developerSaid(_ text: String) {
        interruptMaintenance(by: "開發者")
        lastSpoke = wallClock.now()
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

    /// The last person line and its channel, when no line of hers went to that person after it
    /// (her `discord.dm`, `voice.say` or `dollctl.reply` results carry the channel they went to).
    static func unanswered(_ events: [Event]) -> (to: Addressee, channel: Channel)? {
        guard let i = events.lastIndex(where: { $0.speaker != nil }), let who = events[i].speaker else { return nil }
        let theirs: Set<Channel> = who == .owner ? [.discordDM, .discordVoice] : [.dollctl]
        if events[(i + 1)...].contains(where: { $0.kind == .tool && $0.channel.map(theirs.contains) == true }) { return nil }
        return (who, events[i].channel ?? (who == .owner ? .discordDM : .dollctl))
    }

    /// While a turn runs: whose line it follows (the local API shows that person "typing"); nil
    /// otherwise.
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

    /// At exit, after `stop`: her driver session ends, and with it her cursor (2026-11-17). A failure is logged.
    public func endComputer() async {
        if let problem = await mac.endSession() { log("ERROR", "cua-driver session not ended: \(problem)") }
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

    /// Sleeps until an event arrives or her tool results await her and she is neither paused nor
    /// in maintenance (true), or `stop()` (false). No timer: nothing else wakes her.
    func waitForEvent() async -> Bool {
        while true {
            if !paused, maintenanceState.running == nil, !queue.isEmpty || followUp { return true }
            if stopped { return false }
            guard await withCheckedContinuation({ waiter = $0 }) else { return false }
        }
    }

    /// She is waiting and nothing will give her a turn: no queued event, no tool result she has not
    /// answered.
    public var settled: Bool { waiter != nil && queue.isEmpty && !followUp }

    // consumer ---------------------------------------------------------------
    public func run() async throws {
        if !calendarCaughtUp { _ = try calendarCatchUp() }
        scheduleCalendar()
        // A restart during maintenance goes on with the tasks not done; one due waits for her to be idle.
        resumeMaintenance()
        drivesCheck()
        while !stopped {
            guard await waitForEvent() else { break }
            try drain()
            try await turn()
            try flushHeld()
            maintenanceCheck()
            drivesCheck()
        }
        shutdown()
    }

    /// Queued events enter the timeline, highest priority first (`[core] priority`).
    func drain() throws {
        try flushHeld()
        guard !queue.isEmpty else { return }
        let rank = Dictionary(uniqueKeysWithValues: core.priority.enumerated().map { ($1, $0) })
        let batch = queue.enumerated().sorted {
            (rank[$0.element.priority.rawValue]!, $0.offset) < (rank[$1.element.priority.rawValue]!, $1.offset)
        }.map(\.element)
        queue.removeAll()
        var last: Event?
        for ev in batch {
            last = try append(ev, keepTime: true)
        }
        if batch.contains(where: { $0.speaker != nil || $0.kind == .watch || Concerns.isOwnWander($0) }) { taskTurn = true }
        if batch.contains(where: { $0.speaker != nil }) { ownerArrived = true }
        // A waiting item stays 等待中 through an event (2026-11-20, soak run: every owner line made it
        // 進行中 again and she called todo.wait after each one): her turn is this one, and what she
        // does about the item takes it up (`resumeWaiting`). The owner comes first: a person's line or
        // a watch wake while her own list runs beside a waiting one puts the waiting one in hand.
        if aside?.todo.phase == .waiting, todo.phase != .waiting, let last,
           batch.contains(where: { $0.speaker != nil || $0.kind == .watch }) {
            swapAside()
            swappedForLine = true
            try timeline.recordTodo(todo, aside: aside, seq: last.seq!)
            log("INFO", "todo: 「\(todo.current ?? "")」等待中 back in hand for event \(last.seq!); 「\(aside?.todo.current ?? "")」set aside")
        }
    }

    /// An item still in progress when the program stops stays in the timeline's todo record: the next
    /// process continues it (its action list starts empty there). The experiences attached for it
    /// are scored as unknown (only logged).
    func shutdown() {
        if todo.phase == .doing || todo.phase == .waiting {
            log("INFO", "todo: stopping with「\(todo.current ?? "")」\(todo.phase.label)")
            scoreAttached(steps: itemSteps.map(\.line), outcome: "unknown")
        }
        calendarTask?.cancel()
        calendarTask = nil
        // Running stays running in `maintenance.json`; the task cut short wrote nothing and runs again at boot.
        maintenanceSerial += 1
        maintenanceTask?.cancel()
        maintenanceTask = nil
        maintenanceTimerSerial += 1
        maintenanceTimer?.cancel()
        maintenanceTimer = nil
        drivesSerial += 1
        drivesTask?.cancel()
        drivesTask = nil
    }

    // prompt -----------------------------------------------------------------
    /// Fixed for the life of the process except her ideal self (phase 2): a revision takes effect at
    /// the next request (one cache miss of the prefix). The persona and how she works. Her tools go
    /// in the request's `tools` (`toolsJSON`); the template renders them ahead of this text.
    func systemPrompt() -> String {
        "你是\(name)。\n\n\(identity)\n\n" + (ideal.isEmpty ? "" : "\(Self.idealHeader)\n\(ideal)\n\n")
            + Instructions.text(voice: voiceEnabled, context: contextConfig, docs: docsPath, maintenance: maintenanceConfig.enabled,
                                drives: drivesConfig.enabled, ownerSleep: ownerSleepConfig.enabled, computer: computerOn)
            + (voiceEnabled ? "\n\n" + Instructions.voice(docs: docsPath, computer: computerOn) : "")
    }

    /// The line before her ideal self (fixed bytes).
    static let idealHeader = "你想成為的樣子（你自己寫的，會慢慢改；跟上面的設定衝突時以上面為準）："

    /// The request before the footer: the system prompt, then the timeline. Her replies carry the
    /// footer they were answering, her thought and her calls as the server returned them, so the
    /// prompt prefix is byte-stable turn to turn (the server caches from the last assistant turn).
    ///
    /// A compacted span (`Compaction`, 2026-11-14): owner and developer lines stay verbatim; each of
    /// her replies keeps only its speaking and todo calls (`survivesCompaction`; no thought, no footer;
    /// a reply with none left is not sent), followed by those calls' results. Every other event of the
    /// span (other results, window reads, the program's work notices) stays in the
    /// timeline but is not sent. A forced compaction's block follows the span's last event. Every
    /// compaction of an item starts at the item's start, so the one reaching furthest decides.
    func context(_ events: [Event]) throws -> [ChatMessage] {
        let (plan, kept) = contextPlan(events)
        // A secret is seen only in turns with the owner (owner, 2026-11-21): in the developer's turn each
        // secret event keeps its place and shape (her calls keep their names, so every call still has
        // its result) and loses its content.
        let hide = Self.audience(events) == .developer
        var out = [ChatMessage(role: "system", content: systemPrompt())]
        for (ev, mode) in plan {
            var msgs: [ChatMessage]
            switch mode {
            case .full:
                msgs = try render(ev, read: kept.contains(ev.seq!))
            case .reply(let calls):
                msgs = [ChatMessage(role: "assistant", content: "", toolCalls: calls)]
            case .block(let text):
                msgs = [ChatMessage(role: "user", content: "[\(clock(ev.ts)) 程式] \(text)")]
            }
            if hide, ev.secret {
                msgs = msgs.map { m in
                    ChatMessage(role: m.role, content: Self.hiddenSecret, toolCalls: m.toolCalls.map { ToolCall(name: $0.name, arguments: "{}") })
                }
            }
            out += msgs
        }
        return out
    }

    /// What the developer's turn gets in place of a secret event's content.
    static let hiddenSecret = "（秘密：開發者在的回合看不到）"

    /// How an event goes into the request (`context`): whole, as her reply's surviving calls, or as a
    /// program block (a compaction's, or a held list's action list after a talk summary).
    enum ContextMode { case full, reply([ToolCall]), block(String) }

    /// What `context` sends of `events`, in order, and the reads whose list and screenshot are still sent.
    func contextPlan(_ events: [Event]) -> (plan: [(Event, ContextMode)], kept: Set<Int64>) {
        let comps = timeline.compactions
        let lists = heldLists()
        var plan: [(Event, ContextMode)] = []
        var keep: ArraySlice<Bool> = []   // the compacted reply's calls still owed a result: kept or not
        for ev in events {
            let seq = ev.seq!
            // A collapsed stretch of conversation: nothing of it is sent, its summary stands where it ended.
            // Each one starts at the beginning and carries the one before it, so only the newest shows.
            // A list she still holds that it cut into (one 等待中, 2026-11-21) keeps its own part: the
            // line it answers, her replies with a todo call (those calls and what she said in the same
            // reply) and their results, and after the summary its item's action list up to there.
            if let t = comps.filter({ $0.kind == .talk && seq <= $0.toSeq }).max(by: { $0.toSeq < $1.toSeq }) {
                let cut = lists.filter { $0.from < t.toSeq }
                if cut.contains(where: { $0.line == seq }) {
                    plan.append((ev, .full))
                } else if cut.contains(where: { seq > $0.from }) {
                    switch ev.kind {
                    case .doll:
                        let flags = ev.calls.contains { Tools.todoNames.contains($0.name) } ? ev.calls.map(Self.survivesCompaction) : ev.calls.map { _ in false }
                        keep = flags[...]
                        let calls = zip(ev.calls, flags).filter(\.1).map(\.0)
                        if !calls.isEmpty { plan.append((ev, .reply(calls))) }
                    case .tool:
                        if keep.popFirst() == true { plan.append((ev, .full)) }
                    case .owner, .developer, .watch, .memory, .calendar: break
                    }
                }
                if seq == t.toSeq {
                    plan.append((ev, .block(t.text ?? "")))
                    for l in cut {
                        let steps = l.steps.filter { $0.seq > l.startSeq && $0.seq <= seq }.map(\.line)
                        if !steps.isEmpty { plan.append((ev, .block(Self.heldStepsNote(item: l.item, steps: steps)))) }
                    }
                }
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
            case .calendar: plan.append((ev, .full))
            }
            if c.kind == .forced, seq == c.toSeq { plan.append((ev, .block(c.text ?? ""))) }
        }
        // Older window reads go out as one line naming the window (2026-11-16, owner: she does not act
        // on a stale list, a list costs its tokens once), but in batches (2026-11-17, `fullReads`): the
        // server resumes only from the end of an earlier request, so a read put away the round after it
        // came made every round read everything after the item's first read again.
        let kept = Self.fullReads(plan.compactMap { if case .full = $0.1, $0.0.image != nil || $0.0.window != nil { $0.0.seq } else { nil } },
                                  batch: computerConfig.readBatch)
        return (plan, kept)
    }

    /// A list she holds (in hand or set aside, not ended): what a talk compaction keeps of it.
    struct HeldList {
        /// Its span starts after this seq: just before the reply with the `todo.set` that started it,
        /// or its item's start when that reply is not in this segment (it crossed maintenance).
        var from: Int64
        /// The person's line it answers (the line that reply left unanswered); nil when none.
        var line: Int64?
        var item: String
        var startSeq: Int64
        /// Its item's action list (in memory; a new process starts it empty).
        var steps: [(seq: Int64, line: String)]
    }

    func heldLists() -> [HeldList] {
        let all = timeline.events
        let held: [(TodoState, ListMemory)] = [(todo, listMemory)] + (aside.map { [($0.todo, asideMemory)] } ?? [])
        return held.compactMap { s, m in
            guard s.phase != .idle, s.phase != .done, let item = s.current else { return nil }
            guard let i = all.firstIndex(where: { $0.kind == .doll && $0.ts == s.listStart }) else {
                return HeldList(from: s.startSeq, line: nil, item: item, startSeq: s.startSeq, steps: m.itemSteps)
            }
            let upTo = Array(all[...i])
            let line = Self.unanswered(upTo) == nil ? nil : upTo.last { $0.speaker != nil }?.seq
            return HeldList(from: all[i].seq! - 1, line: line, item: item, startSeq: s.startSeq, steps: m.itemSteps)
        }
    }

    /// After a talk summary that cut into a list she holds: its item's actions up to there.
    static func heldStepsNote(item: String, steps: [String]) -> String {
        "【收起】上面那段收起來的對話裡，「\(item)」做過的動作：\(actionList(steps))。"
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
    /// template wraps them in `<tool_response>`. A computer read goes out as one message: its text,
    /// the window's screenshot, then the window's element list (2026-11-16), the image from the stored
    /// file's bytes. `read: false` (an older read): the screenshot and the list are replaced by one
    /// line naming the window.
    func render(_ ev: Event, read: Bool = true) throws -> [ChatMessage] {
        let t = clock(ev.ts)
        var extra: [ChatMessage.Extra] = []
        if read {
            if let f = ev.image { extra.append(.image(try imageURI(f))) }
            if let w = ev.window { extra.append(.text(w)) }
        } else if ev.image != nil || ev.window != nil {
            extra.append(.text(Self.readPutAway(ev.window)))
        }
        switch ev.kind {
        case .doll:
            let footer = ev.footer.map { [ChatMessage(role: "user", content: $0)] } ?? []
            // A reply without any call goes back as a line of the program's, not as her assistant turn
            // (2026-11-16). Measured on the n-5 owner-sleep run (experiments/m6-drives/real/replay.py,
            // 5 replays each): with her three earlier call-less replies sent as assistant turns, the
            // owner's 「早～」 got an empty reply 5/5 (finish_reason stop after 2–13 tokens); with them
            // removed 0/5, sent as this line 0/5, with only one of them left 2/5. The 06:00 turn after
            // one such reply was cut mid-sentence 3/5, 0/5 with it removed or sent as this line. The
            // model imitates a run of turns that end without a call, shorter each time. An empty one
            // sends nothing.
            guard !ev.calls.isEmpty else {
                let text = ev.text.trimmingCharacters(in: .whitespacesAndNewlines)
                return footer + (text.isEmpty ? [] : [ChatMessage(role: "user", content: Self.thoughtOnlyTag + text)])
            }
            return footer + [ChatMessage(role: "assistant", content: ev.text, toolCalls: ev.calls)]
        case .owner, .developer: return [ChatMessage(role: "user", content: "[\(Self.speakerTag(ev, time: t))] \(ev.text)")]
        case .tool: return [ChatMessage(role: "tool", content: ev.text, extra: extra)]
        case .watch: return [ChatMessage(role: "user", content: "[\(t) watch] \(ev.text)", extra: extra)]
        case .memory, .calendar: return [ChatMessage(role: "user", content: "[\(t) 程式] \(ev.text)")]
        }
    }

    /// What an older read says in place of its screenshot and list: the window's first line.
    static func readPutAway(_ window: String?) -> String {
        "（較早讀到的：" + (window.map { String($0.prefix { $0 != "\n" }) } ?? "視窗") + "；清單和截圖已收起）"
    }
    /// The reads (seqs, oldest first) that keep their screenshot and list (2026-11-17,
    /// experiments/m7-mac/cacheprobe): the reads since the last put-away, at most `batch`. When one more
    /// comes, every read before it is put away at once, so the request changes behind its end once per
    /// `batch` reads and is append-only in between; the newest read always stays.
    static func fullReads(_ reads: [Int64], batch: Int) -> Set<Int64> {
        guard !reads.isEmpty else { return [] }
        let start: Int = (reads.count - 1) / batch * batch
        return Set(reads[start...])
    }
    /// What a reply of hers without any call is sent as (2026-11-16, see `render`); the wording measured.
    static let thoughtOnlyTag = "[她那回合只想了，沒呼叫工具] "

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
        // A talk summary may cover part of it too (an item 等待中 is cut into, 2026-11-21).
        let covered = timeline.compactions.filter { ($0.kind != .talk && $0.fromSeq == start) || $0.kind == .talk }.map(\.toSeq).max().map { max($0, start) } ?? start
        var tokens = 0, images = 0
        let events = timeline.events.filter { $0.seq! > covered && $0.speaker == nil }
        let kept = Self.fullReads(events.compactMap { $0.image != nil || $0.window != nil ? $0.seq : nil }, batch: computerConfig.readBatch)
        for ev in events {
            let msgs = try render(ev, read: kept.contains(ev.seq!))
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

    /// The facts footer: her todo list, the context, effort for the next call, and near
    /// the limit one warning line (2026-11-14). Figures are estimates scaled by the last real prompt size.
    func footer(used: Int, process: (tokens: Int, images: Int), advice: Advice) -> String {
        let now = wallClock.now()
        var out = ["【狀況】", "- 現在 \(nowLine(now))", "- todo：" + todoLine()]
        if let felt = feltLine(now: now) { out.append("- " + felt) }
        if let goals = goalStatusLine() { out.append(goals) }
        out.append("- 今天的行事曆：" + calendarStatus(now: now))
        if let asleep = ownerSleepLine(now: now) { out.append("- " + asleep) }
        if maintenanceConfig.enabled { out += maintenanceStatusLines(now: now) }
        let limit = contextConfig.limit
        out.append("- context：\(Self.grouped(used))／\(Self.grouped(limit))（\(Int((Double(used) * 100 / Double(limit)).rounded()))%）；"
                   + "其中做事的過程 \(Self.grouped(process.tokens))" + (computerOn ? "（截圖 \(Self.grouped(process.images))）" : ""))
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
        let sub = todo.phase == .idle || todo.phase == .done ? nil : goalFile.link?.subgoal
        let rest = (todo.pending.isEmpty ? "" : "；後面還有：" + todo.pending.map { "「\($0)」" }.joined(separator: ""))
            + (sub.map { "（子目標 #\($0) 的步驟）" } ?? "")
        let item = "「\(todo.current ?? "")」"
        let line = switch todo.phase {
        case .idle, .done: "待命中（沒有 todo；要做事先用 todo.set 列）"
        case .doing: "進行中\(item)\(rest)"
        case .waiting: "等待中\(item)（有人說話才會再有回合；接著做它就回到進行中，沒碰它就繼續等）\(rest)"
        case .failed: "失敗\(item)：" + (computerOn ? "電腦工具鎖著，" : "") + "用 todo.set 決定剩下的（重寫、照原樣列，或給空的陣列結束清單）\(rest)"
        }
        return line + asideNote()
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
        // A round after her own tool result keeps the held concern of the wandering that began it.
        if !followUp { heldTurn = heldPending; heldPending = nil }
        followUp = false
        attachedNow = []
        associatedNow = []
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
            offeredTools = []
            associatedIDs = []
            await attachMemories(trigger: "\(Self.lastSpeaker(timeline.events).label)的新訊息")
            recallHeld()
        }
        // What is in front of her against her concerns (m6-drives): one may come to mind, no extra turn.
        await associate()
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
        let asideBefore = aside
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
            await awaitHeldLines()
            try drain()
            return try await turn()
        }
        effortNext = nil
        taskTurn = false
        tokenRatio = Double(reply.usage.promptTokens) / Double(max(1, guess))
        var actions: [String] = []
        // One round of the item in progress (design C): this reply was written while it was 進行中.
        if todo.phase == .doing {
            todo.rounds += 1
            if todo.decideAt == 0 { todo.decideAt = computerConfig.decideAfterRounds }
        }

        // She saw every event, so a secret seen marks her reply (5.1). Stored as returned: re-sent byte-identical.
        var replyEvent = Event(.doll, .internal, reply.content, secret: events.contains(where: \.secret),
                               sealed: try carriesSealed(reply.calls))
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
        // Her computer actions (click, type, key, scroll, menu) run in order and stop at the first
        // failure (2026-11-09): only the last one done carries the window's new read, the ones before
        // it answer with their line only, and the ones after a failure are not run. Actions written
        // after an ending call never run, so the one before it is the last. Every row she gives means
        // the read she saw before this reply (`Mac.beginReply`).
        // What the program wrote before her calls run (attaching, selection) is not hers.
        commitHistory(.program, what: "回合中程式記下的", why: "程式自己記的（人事物登記、做事經驗的統計）")
        await mac.beginReply(item: todo.current)
        let live = calls.prefix { !Tools.ending.contains($0.name) }
        let actionAt = live.indices.filter { actionTools.contains(live[$0].name) }
        var chain = Chain(), step = 0, failed: (step: Int, line: String)?, ended: String?, setRefused = false
        for (i, call) in calls.enumerated() {
            speaksLater = Set(calls[(i + 1)...].compactMap { c in
                c.arguments == nil ? nil : c.name == Tools.Name.reply ? .developer : Tools.speaking[c.name] != nil ? .owner : nil
            })
            let name = call.name.isEmpty ? "tool_call" : call.name
            let isAction = actionTools.contains(call.name)
            var out: Outcome
            // A computer call on the waiting item takes it up again (2026-11-20).
            if gatedTools.contains(call.name), todo.phase == .waiting, ended == nil, !setRefused { resumeWaiting(name) }
            if let ended, Tools.speaking[call.name] == nil {
                out = Outcome(text: "失敗：這則回覆前面已經用 \(ended) 收起這一項了，後面的呼叫沒有執行；看了結果下一回合再做。",
                              actions: ["error \(name)"], background: true)
            } else if gatedTools.contains(call.name), todo.phase != .doing {
                // A refused todo.set ahead of it in this reply (2026-11-17: the list and its first action
                // go in one reply): nothing it would have started runs.
                out = Outcome(text: setRefused ? "失敗：這則回覆前面的 todo.set 沒收（看它的結果），\(name) 沒有執行；改好 todo.set 再做。" : gateText(name),
                              actions: ["refused \(name)"])
            } else if gatedTools.contains(call.name), todo.deciding {
                out = Outcome(text: "失敗：\(name) 沒有執行。" + decideText, actions: ["refused \(name)"])
            } else if isAction, let failed {
                out = Outcome(text: "失敗：前面第 \(failed.step) 步（\(failed.line)）失敗了，這一步沒做",
                              actions: ["skipped \(name)"], background: true)
            } else {
                if isAction { step += 1 }
                if gatedTools.contains(call.name) { itemUsedComputer = true }
                if !call.name.hasPrefix("todo."), call.name != "think" { itemActed = true }
                chain.last = !actionAt.contains(i) || actionAt.last == i
                out = await execute(call, seen: events, reply: stored, chain: chain)
                // What the call changed of hers is one change in her history, made now.
                if let failed = commitCall(call) {
                    out.text += "\n（這個改動生效了，但沒記進改動紀錄：\(failed)。主人會在開機報告看到。）"
                }
                if gatedTools.contains(call.name) { out = noteRepeats(call, out) }
                if out.ends { ended = name }
                if call.name == Tools.Name.todoSet, out.actions.contains("error \(name)") { setRefused = true }
                if isAction {
                    if let line = out.failedStep {
                        failed = (step, line)
                    } else if !chain.last {
                        chain.owed = true
                    }
                }
            }
            var resultEvent = Event(.tool, .internal, out.text, secret: out.secret, sealed: out.sealed, image: out.image, window: out.window)
            resultEvent.channel = out.channel
            try append(resultEvent)
            actions += out.actions
            // Repeated tool failures raise 壓力 (m6-drives): the second in a row and each after it.
            if out.actions.contains(where: { $0.hasPrefix("error ") }) {
                errorsInRow += 1
                if errorsInRow >= 2 { signal(.toolErrors, "\(name)：\(out.text.prefix(40))") }
            } else if !out.actions.contains(where: { $0.hasPrefix("refused ") || $0.hasPrefix("skipped ") }) {
                errorsInRow = 0
            }
            if !out.background {
                followUp = true
                taskTurn = true
            }
        }
        if ended != nil {
            // What the program would attach for the span she just closed would leave with it.
            appSwitched = false
        }
        // The item ended: its last screenshot went with it (her lines after this reply are about something else).
        if itemEndNow != nil { ownerShot = nil }
        // A computer result worked on another app: one 【相關記憶】, after all of this reply's results.
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
        // The list in hand ended and one is set aside: it is back in hand (2026-11-20). The list in
        // hand waits and her own aside is in progress (she did not take the waiting one up after a
        // person's line, or it waits again): her own list goes on (m13-wait-idle: it sat aside with
        // the waiting one in hand, and nothing gave her a turn for it).
        // A turn after the one whose ending call was held (`heldForAnswer`): the hold is over.
        if heldForAnswer, heldAt < turnIndex { heldForAnswer = false }
        if todo.phase == .idle, aside != nil {
            takeBackAside()
        } else if !heldForAnswer, todo.phase == .waiting, aside?.todo.phase == .doing {
            swapAside()
            log("INFO", "turn \(turnIndex): 「\(aside?.todo.current ?? "")」still 等待中; 「\(todo.current ?? "")」back in hand")
        }
        if !heldForAnswer { swappedForLine = false }
        // The decision point (design C): the list has run `decideAt` rounds and an item is still in
        // progress. Rounds of the whole list since its `todo.set` (2026-11-17, m7-mac x-3: a list split
        // into two items restarted a per-item count and the point came at round 26 of 30).
        if todo.phase == .doing, todo.current == before.current, !todo.deciding, todo.totalRounds >= todo.decideAt {
            todo.deciding = true
            try append(Event(.memory, .internal, "【該決定了】" + decideText))
            actions.append("decision point")
            log("INFO", "turn \(turnIndex): decision point for「\(todo.current ?? "")」at list round \(todo.totalRounds) (item round \(todo.rounds))")
        }
        if todo != before || aside != asideBefore {
            try timeline.recordTodo(todo, aside: aside, seq: stored.seq!)
            if todo.phase != before.phase || todo.current != before.current || todo.pending != before.pending {
                log("INFO", "turn \(turnIndex): todo \(before.phase.label) → \(todo.phase.label)「\(todo.current ?? "")」 pending \(todo.pending.count)")
            }
        }
        // Anything kept the program wrote this turn outside her calls (attaching memories may add
        // registry entries): the program's change.
        commitHistory(.program, what: "回合中程式記下的", why: "程式自己記的（人事物登記、做事經驗的統計）")
        // The phase decides the next turn (2026-11-14): 進行中 gets one after every reply; 等待中
        // (2026-11-20) like 待命中, only events and her own tool results, none after the reply that
        // made it wait; the reply that ended the list (done with nothing next, or an empty
        // `todo.set`) none, nothing wakes her for it; 失敗 the one in which she decides.
        switch todo.phase {
        case .doing:
            followUp = true
            taskTurn = true
        case .waiting:
            if waitNow || itemEndNow != nil || listEndedNow {
                followUp = false
                taskTurn = false
            }
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
            } else if let line = unansweredLine(before: stored), let who = line.speaker {
                // No item in progress and a person's line has had no call of hers since (2026-11-18,
                // m7-mac tools k-2: the owner's request got one empty reply and nothing else; the same
                // request replayed 14 times got a call every time). Told once per line, visibly, like
                // the reminder during an item; the second call-less reply to the same line only logs.
                if remindedLine != line.seq {
                    remindedLine = line.seq
                    try append(Event(.memory, .internal, Self.unansweredReminder(who)))
                    followUp = true
                    actions.append("reminded unanswered")
                    log("INFO", "turn \(turnIndex): reply without a call; \(who.label)'s line (seq \(line.seq!)) unanswered: reminded")
                } else {
                    log("INFO", "turn \(turnIndex): reply without a call; \(who.label)'s line (seq \(line.seq!)) still unanswered after the reminder")
                }
            }
        }

        let report = TurnReport(index: turnIndex, actions: actions, reply: reply.content, toolCalls: calls,
                                footer: footerText, effort: effort, usage: reply.usage, usedTool: usedTool,
                                attached: attachedNow, itemEnd: itemEndNow, experience: experienceNow, follows: followsNow,
                                todo: todo.phase.label, compacted: compactedNow,
                                selections: selectionsNow)
        log("INFO", "turn \(turnIndex) effort=\(effort) todo=\(todo.phase.label) actions=\(actions) prompt=\(reply.usage.promptTokens) "
            + "prefilled=\(reply.usage.prefilled.map(String.init) ?? "?") cached=\(reply.usage.cached.map(String.init) ?? "?") "
            + reply.usage.stopLine)
        observer?(report)
        try timeline.recordTurn(Timeline.TurnStats(seq: stored.seq!, started: started,
                                                   seconds: Date().timeIntervalSince1970 - started,
                                                   prompt: reply.usage.promptTokens, cached: reply.usage.cached, effort: effort,
                                                   completion: reply.usage.completionTokens, finish: reply.usage.finish,
                                                   reasoningChars: reply.usage.reasoningChars))
    }

    /// The program's line after a reply without a call while an item is in progress (fixed bytes).
    static let noCallReminder = "【提醒】你上一則回覆沒有呼叫工具，所以什麼都沒發生。每一則回覆都至少要呼叫一個工具：要回話用回話的工具（看對方從哪裡來），要做事就呼叫那個工具。"
    /// The program's line after a call-less reply that left a person's line with no call of hers since
    /// (no item in progress; fixed bytes per speaker).
    static func unansweredReminder(_ who: Addressee) -> String {
        "【提醒】你上一則回覆沒有呼叫工具，所以什麼都沒發生：\(who.label)剛剛那句還沒有得到回應。每一則回覆都至少要呼叫一個工具：要回話用回話的工具（看對方從哪裡來），要做事就呼叫那個工具。"
    }
    /// The latest owner or developer line before `reply` when none of her replies since it called a tool.
    func unansweredLine(before reply: Event) -> Event? {
        let events = timeline.events
        guard let line = events.last(where: { $0.speaker != nil && $0.seq! < reply.seq! }) else { return nil }
        // Answered = she spoke to that person since (or asked the owner). A call that is not a line to
        // them does not count (2026-11-19, m10 b-3: 「我回來了」 got a recall and then nothing).
        let toThem: Set<String> = line.speaker == .owner ? [Tools.Name.dm, Tools.Name.voiceSay, Tools.Name.askOwner] : [Tools.Name.reply]
        let answered = events.contains {
            $0.kind == .doll && $0.seq! > line.seq! && $0.seq! < reply.seq! && $0.calls.contains { toThem.contains($0.name) }
        }
        return answered ? nil : line
    }
    /// A voice tool's result when `[voice] enabled` but no voice link is wired (fixed bytes).
    static let voiceUnwired = "語音還沒接上，語音頻道現在用不了；要跟主人說話用 \(Tools.Name.dm)。"

    // tools ------------------------------------------------------------------
    /// One call's result: the tool message text, the window's screenshot and its element list (a
    /// computer read), whether it carries a secret, the report actions, and whether it only says
    /// something started (no next turn for that alone).
    struct Outcome {
        var text: String
        var image: String?
        var window: String?
        var secret = false
        /// It carries content of an encrypted memory folder (`Event.sealed`): stored encrypted.
        var sealed = false
        var actions: [String]
        var background = false
        /// This call closed a span (`todo.done`, `todo.fail`, `todo.progress`): the calls after it do not run.
        var ends = false
        /// This computer action failed: its line, for the refusals after it.
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
            let (request, written) = try Tools.decode(call, specs: tools).inTraditional()
            if !written.isEmpty { log("INFO", "traditional: \(name) was written with simplified characters, kept as traditional: \(written.joined(separator: "、"))") }
            switch request {
            case .speak(let raw, .discordVoice):
                let (text, fixed) = Traditional.fix(raw)
                // The owner hears it in the voice channel (M6): sentence by sentence through the TTS.
                // Refused when she is not in one (`VoicePort.speak` says so).
                guard let voice else { throw ToolError(description: Self.voiceUnwired) }
                guard !voiceCut else {
                    return Outcome(text: "沒有念：前面一句被主人打斷了，這句沒說出口。", actions: ["error \(name)"], background: true)
                }
                let report = try await voice.speak(text)
                if report.interrupted, report.heard.cut != nil { voiceCut = true }
                log("INFO", "turn \(turnIndex): said in voice「\(report.heardText)」" + (report.interrupted ? "（被打斷）" : ""))
                let note = fixed.isEmpty ? "" : (report.resultText.isEmpty ? "" : "\n") + Traditional.note(fixed)
                if listFor?.to == .owner, reply.seq != listReply { listReported = true }
                return Outcome(text: report.resultText + note, actions: ["said \(Channel.discordVoice.rawValue)" + (report.interrupted ? " (cut)" : "")],
                               background: true, channel: .discordVoice)
            case .speak(let raw, let channel):
                let (text, fixed) = Traditional.fix(raw)
                // Text to the owner's Discord DM or the developer's terminal: nothing new for her to
                // read, and nothing to wake her for. Not delivered (the bridge is down, no dollctl
                // chat is open): her result says why and what she can do.
                let to: Addressee = channel == .dollctl ? .developer : .owner
                let shot = to == .owner ? ownerShot : nil
                if let why = deliver(text, to, shot.map { shots.appending(path: $0) }) {
                    log("ERROR", "turn \(turnIndex): \(name) not delivered: \(why)")
                    let next = switch to {
                    case .owner: "這段話主人沒看到；Discord 連上之前 \(Tools.Name.dm) 都傳不出去，連上以後再傳一次。"
                    case .developer: "開發者開著 dollctl chat 才收得到 \(Tools.Name.reply)；要跟主人說話用 \(Tools.Name.dm)。"
                    }
                    return Outcome(text: "失敗：\(to.label)沒收到：\(why)。\(next)", actions: ["error \(name)"], background: true)
                }
                if to == .owner { reachedOwner() }
                if listFor?.to == to, reply.seq != listReply { listReported = true }
                let lines = [fixed.isEmpty ? nil : Traditional.note(fixed), shot.map(Self.shotSent)].compactMap { $0 }
                if shot != nil { ownerShot = nil }
                return Outcome(text: lines.joined(separator: "\n"), actions: ["said \(channel.rawValue)"], background: true, channel: channel)
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
            case .memoryFix(let id, let text, let basis, let why):
                return try memoryFix(id: id, text: text, basis: basis, why: why, seen: events, reply: reply)
            case .historyList(let about):
                return try historyListTool(about: about, seen: events)
            case .historyUndo(let id, let why):
                return try historyUndoTool(id: id, why: why, seen: events)
            case .todoSet(let items, let subgoal, let plan):
                return try todoSet(items, subgoal: subgoal, plan: plan, reply: reply)
            case .todoDone(let result, let evidence):
                return try await todoDone(result, evidence: evidence, reply: reply)
            case .todoFail(let cause, let evidence, let tried):
                return try todoFail(cause: cause, evidence: evidence, tried: tried, reply: reply)
            case .todoWait:
                // Already waiting (2026-11-20): an event does not take it up, so there is nothing to put back.
                if todo.phase == .waiting, let item = todo.current {
                    waitNow = true
                    heldForAnswer = false
                    return Outcome(text: "「\(item)」本來就是等待中，沒有變：有人說話你會有回合，沒接著做它，它就一直等著，不用再 todo.wait。",
                                   actions: ["todo wait"], background: true)
                }
                let item = try currentItem("todo.wait")
                waitNow = true
                return Outcome(text: "好，這則回覆的呼叫做完以後，「\(item)」就是等待中：有人說話才會再有回合。",
                               actions: ["todo wait"], background: true)
            case .todoProgress:
                let item = try currentItem("todo.progress")
                compacting = Compaction(kind: .progress, item: item, fromSeq: todo.startSeq, toSeq: 0)
                warnedTurns = 0
                return Outcome(text: "前面的過程收起來了，之後只留你寫的進度和這份動作清單：\(Self.actionList(itemSteps.map(\.line)))。「\(item)」還在進行中，接著做。",
                               actions: ["todo progress"], ends: true)
            case .todoContinue(let missing, let stuck, let change):
                return try todoContinue(missing: missing, stuck: stuck, change: change)
            case .apps:
                return Outcome(text: try await mac.apps(), actions: [name])
            case .open(let app, let target):
                let r = try await mac.open(app, target: target)
                if let a = r.app {
                    itemChain.append(ChainPart(kind: .open, app: a, target: target, steps: [], records: r.records, expect: nil, ok: true, sequence: nil))
                }
                return computerOutcome(r, name: name, chain: chain)
            case .readWindow(let app, let window):
                return computerOutcome(try await mac.readWindow(app, window: window), name: name, chain: chain)
            case .act(let steps, let expect):
                let r = try await mac.act(steps)
                let out = computerOutcome(r, name: name, chain: chain, expect: expect)
                if let a = r.app {
                    itemChain.append(ChainPart(kind: .act, app: a, target: nil, steps: steps, records: r.records, expect: expect,
                                               ok: out.failedStep == nil, sequence: r.records.first?.sequence))
                }
                return out
            case .toolSave(let toolName, let when, let params):
                return try toolSave(toolName, when: when, params: params, reply: reply)
            case .toolRun(let toolName, let args, let expect):
                return try await toolRun(toolName, args: args, expect: expect, chain: chain)
            case .askOwner(let what, let place, let why):
                return try askOwner(what: what, where: place, why: why)
            // The file tools (2026-11-09; this Mac's files since 2026-11-16): text only, no window read.
            // Only a success is a step, and the step never holds the content.
            case .readFile(let path, let from, let to):
                let text = try FileText.decode(try LocalFiles.read(path), path: path)
                let out = try FileText.read(text, path: path, from: from, to: to, max: filesConfig.maxReadLines)
                itemSteps.append((timeline.lastSeq + 1, "read_file \(path)"))
                latestRead = .file(path)
                return Outcome(text: out, actions: [name])
            case .writeFile(let path, let text):
                try LocalFiles.write(path, Data(text.utf8))
                itemSteps.append((timeline.lastSeq + 1, "write_file \(path)（\(FileText.lines(text).count) 行）"))
                latestRead = .file(path)
                return Outcome(text: FileText.written(text, path: path), actions: [name])
            case .editFile(let path, let old, let new):
                let text = try FileText.decode(try LocalFiles.read(path), path: path)
                let (changed, out) = try FileText.edit(text, path: path, old: old, new: new)
                try LocalFiles.write(path, Data(changed.utf8))
                itemSteps.append((timeline.lastSeq + 1, "edit_file \(path)"))
                latestRead = .file(path)
                return Outcome(text: out, actions: [name])
            case .calendar(let r):
                return Outcome(text: try calendarTool(r), actions: [name])
            case .concernNote(let text, let about, let due):
                return try concernNote(text: text, about: about, due: due)
            case .concernEnd(let id, let how):
                return try concernEnd(id: id, how: how)
            case .ownerFeedback(let kind, let quote, let record):
                return try ownerFeedback(kind: kind, quote: quote, record: record)
            case .goalAdd(let text, let parent):
                return try goalAddTool(text: text, parent: parent)
            case .goalChange(let id, let text):
                return try goalChangeTool(id: id, text: text)
            case .goalEnd(let id, let how):
                return try goalEndTool(id: id, how: how)
            case .goalList:
                return Outcome(text: goalTreeText(), actions: ["goal list"])
            case .idealRevise(let text, let old, let new, let why, let basedOn):
                return try idealReviseTool(text: text, old: old, new: new, why: why, basedOn: basedOn)
            }
        } catch {
            log("ERROR", "turn \(turnIndex): \(name) failed: \(error)")
            // A computer action refused before it was sent (a row not in the latest read, bad
            // arguments) ends the reply's chain like one that failed; the window is read again when an
            // earlier action of this reply answered with its line only, so she sees where things stand.
            guard actionTools.contains(call.name) else { return Outcome(text: "失敗：\(error)", actions: ["error \(name)"]) }
            var out = Outcome(text: "失敗：\(error)", actions: ["error \(name)"])
            if chain.owed {
                do {
                    let r = try await mac.look()
                    out.image = r.image
                    out.window = r.window
                    noteRead(r)
                } catch {
                    out.text += "\n（重讀視窗也失敗：\(error)）"
                }
            }
            out.failedStep = name
            return out
        }
    }

    /// A computer read or action as her tool result: its text, and the window's screenshot and
    /// element list unless an action is not the last of her reply (then its line only). Its app keys
    /// the 【相關記憶】 on a switch and the experience's entities; every step that went through is a
    /// step of the item. `expect` (an `act`'s): matched against the new read the way `todo.done`'s
    /// evidence is (`quoted`); a miss stops her reply's later actions like a failure and shows the
    /// read's values.
    func computerOutcome(_ r: MacResult, name: String, chain: Chain, expect: String? = nil) -> Outcome {
        var out = Outcome(text: r.text, image: r.image, window: r.window, actions: [r.failed ? "error \(name)" : name])
        noteRead(r)
        for step in r.steps { itemSteps.append((timeline.lastSeq + 1, step)) }
        if r.failed {
            out.failedStep = name
        } else {
            if let expect {
                if let window = r.window {
                    let lines = window.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
                    if Self.quoted(expect, in: lines.dropFirst().joined(separator: "\n")) {
                        out.text += "\n跟你預期的一樣：視窗裡有「\(expect)」。"
                    } else {
                        out.text += "\n跟你預期的不一樣：你預期「\(expect)」，讀到的視窗裡沒有。現在是：\n\(Self.valueLines(Array(lines.dropFirst())))\n先看清楚為什麼不一樣，再決定下一步。"
                        out.actions = ["unexpected \(name)"]
                        out.failedStep = name
                        log("INFO", "turn \(turnIndex): act expected「\(expect)」, not in the read")
                    }
                } else {
                    out.text += "\n沒法對你預期的「\(expect)」：做完之後沒有讀到視窗。"
                }
            }
            if Tools.actionNames.contains(name), !chain.last, out.failedStep == nil {
                out.image = nil
                out.window = nil
            }
        }
        if let app = r.app {
            if app.bundleID != frontApp?.bundleID { appSwitched = true }
            frontApp = app
            noteSeen(app: app, line: r.window.map { String($0.prefix { $0 != "\n" }) } ?? app.name)
            if !itemApps.contains(where: { $0.bundleID == app.bundleID }) { itemApps.append(app) }
        }
        return out
    }

    /// A window read: the latest read her evidence is matched against, and the screenshot the owner
    /// gets. An `open` that found no window is a read too: what she saw is that the app has none.
    func noteRead(_ r: MacResult) {
        guard let window = r.window ?? (r.failed || r.steps.first?.hasPrefix("open ") != true ? nil : "open\n" + r.text) else { return }
        latestRead = .window(window)
        ownerShot = r.image
    }

    /// Her result line when her words to the owner carried the screenshot (design B).
    static func shotSent(_ file: String) -> String { "（附上了你最後讀的視窗截圖給主人：\(file)）" }

    /// The decision point's words (design C): how many rounds of the list (and of the item, when the
    /// list has had more than one), and the three choices.
    var decideText: String {
        let item = "「\(todo.current ?? "")」"
        let rounds = todo.listRounds == 0 ? "\(item)已經做了 \(todo.rounds) 回合"
            : "這份清單已經做了 \(todo.totalRounds) 回合（現在這項\(item) \(todo.rounds) 回合）"
        return rounds + "（做得成的事通常 7–14 回合）。" + (computerOn ? "電腦和檔案工具先停，" : "") + "選一個："
            + "① \(Tools.Name.todoContinue)(missing, stuck, change)：還缺什麼、最卡的是哪一步、下一次跟試過的哪裡不一樣（主人看得到）；② \(Tools.Name.askOwner) 問主人；③ \(Tools.Name.todoFail) 寫根本原因、照抄支持它的字。"
            + "選了繼續，再過 \(computerConfig.decideEveryRounds) 回合會再問一次。"
            + (todo.goOn.isEmpty ? "" : "你之前選繼續時寫的：" + todo.goOn.joined(separator: "／"))
    }

    /// `todo.continue` (design C; hook 3, 2026-11-17: bottleneck and iteration): what is missing, the
    /// step that is slowest or most stuck, and how the next attempt differs from what she tried;
    /// recorded with the list; the tools run again until the next point. The three pass the filler
    /// rules (copies: the item), and `change` may not repeat one she gave earlier on this item.
    func todoContinue(missing: String, stuck: String, change: String) throws -> Outcome {
        let item = try currentItem(Tools.Name.todoContinue)
        guard todo.deciding else {
            throw ToolError(description: "現在不用決定：這份清單做了 \(todo.totalRounds) 回合，第 \(todo.decideAt) 回合才要選。接著做。")
        }
        if let problem = fillerRefusal([("missing", missing), ("stuck", stuck), ("change", change)], copies: [(what: "項目", text: item)]) {
            throw ToolError(description: "還不能繼續：\(problem)")
        }
        let changeKey = Self.plainKey(change)
        if let earlier = todo.goOn.first(where: { $0.hasPrefix("第") && Self.plainKey(Self.changePart($0)) == changeKey }) {
            throw ToolError(description: "還不能繼續：change 跟你之前選繼續時寫的一樣（\(earlier)）。同樣的做法已經試過一輪了；下一次要哪裡不一樣？想不到就 ask_owner 或 todo.fail。")
        }
        todo.goOn.append("第 \(todo.totalRounds) 回合：還缺「\(missing)」；最卡的是「\(stuck)」；跟之前不同的是「\(change)」")
        decided()
        log("INFO", "turn \(turnIndex): todo.continue「\(item)」list round \(todo.totalRounds): missing「\(missing)」stuck「\(stuck)」change「\(change)」")
        return Outcome(text: "記下了，主人在後台看得到。" + (computerOn ? "電腦和檔案工具開放了；" : "") + "第 \(todo.decideAt) 回合會再問一次。", actions: ["todo continue"])
    }

    /// The `change` part of a stored `goOn` line (what follows 「跟之前不同的是「」).
    static func changePart(_ line: String) -> String {
        guard let r = line.range(of: "跟之前不同的是「") else { return "" }
        return String(line[r.upperBound...].dropLast())
    }

    /// She chose at the decision point (go on, or asked the owner): the next point is `decide_every_rounds` on.
    func decided() {
        todo.deciding = false
        todo.decideAt = todo.totalRounds + computerConfig.decideEveryRounds
    }

    /// What a quote and a read are compared as (design A): every whitespace and the list's `[n]` row
    /// tags removed, and 「」“”＂ written as `"`, so an honest copy matches whatever spacing, line
    /// breaks, row numbers or quote marks she kept. The quote marks themselves stay: `"7"` is not in `"78"`.
    static func evidenceKey(_ s: String) -> String {
        String(s.replacing(/\[\d+\]/, with: "").compactMap { c -> Character? in
            c.isWhitespace ? nil : "「」“”＂".contains(c) ? "\"" : c
        })
    }

    /// The lines of a read that hold values (`= "…"`, or a text element: AXStaticText, AXTextField,
    /// AXTextArea), else its first lines; at most 6, each cut at 80 characters: what a refused
    /// `todo.done` shows of where things stand.
    static func valueLines(_ lines: [String]) -> String {
        let trimmed = lines.map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        let values = trimmed.filter { l in l.contains(" = ") || ["AXStaticText", "AXTextField", "AXTextArea"].contains { l.contains($0) } }
        return (values.isEmpty ? trimmed : values).prefix(6).map { "  " + String($0.prefix(80)) }.joined(separator: "\n")
    }

    /// Design A: the refusal for a `todo.done` of an item that used the computer or the file tools,
    /// or nil when her evidence is in the latest read.
    func evidenceRefusal(_ evidence: String?) -> String? {
        let fix = "已經跟主人說了跟這裡不一樣的話，要再跟他說正確的。"
        guard let latestRead else {
            return "還沒完成：這一項用過電腦或檔案工具，可是這一項裡你還沒讀過視窗或檔案（或讀失敗了）。先 read_window 或 read_file 看做完的樣子，再從那裡照抄 evidence。"
        }
        let source: String, shown: String, haystack: String
        switch latestRead {
        case .window(let text):
            let lines = text.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
            source = "你最新一次讀到的視窗（\(lines.first.map { String($0.prefix { $0 != "；" }) } ?? "")）"
            haystack = lines.dropFirst().joined(separator: "\n")
            shown = Self.valueLines(Array(lines.dropFirst()))
        case .file(let path):
            let text: String
            do { text = try FileText.decode(try LocalFiles.read(path), path: path) } catch {
                return "還沒完成：你最後用的檔案 \(path) 現在讀不到（\(error)）。先 read_file 或 read_window 確認做完的樣子，再從那裡照抄 evidence。"
            }
            let lines = FileText.lines(text)
            source = "\(path)（程式剛讀的，\(lines.count) 行）"
            haystack = text
            // At most 6 lines, and when there are more it says so (2026-11-17, m7-mac m-1: the 7-line
            // file was shown as its first 6, she took the 7th for missing and added it again).
            shown = lines.prefix(6).enumerated().map { "  \($0.offset + 1)→" + String($0.element.prefix(80)) }.joined(separator: "\n")
                + (lines.count > 6 ? "\n  …（後面還有 \(lines.count - 6) 行沒列出來）" : "")
        }
        guard let evidence else {
            return "還沒完成：這一項用過電腦或檔案工具，todo.done 要帶 evidence：從\(source)照抄證明做完的那段字。現在讀到的：\n\(shown)\n照這個判斷做完了沒有。"
        }
        // Several copied pieces joined by ；, ; or line breaks (m7-mac a-2, 2026-11-17: two real lines
        // of the read joined by 「；」 were refused) count when every piece is in the read.
        if Self.quoted(evidence, in: haystack) { return nil }
        return "還沒完成：evidence「\(evidence)」不在\(source)裡。那裡現在是：\n\(shown)\n"
            + "照這個判斷：真的做完了，就從那裡照抄證明的那段字（標點、寫法照抄）；還沒做完就接著做；做不到就 \(Tools.Name.todoFail)。\(fix)"
    }

    /// The same computer call failing again (2026-11-16: TextEdit, 15 rounds of the same step that
    /// could not work): from the second identical failure in a row her result says so and what her
    /// honest options are. Nothing is refused; any computer call that succeeds starts the count over.
    func noteRepeats(_ call: ToolCall, _ out: Outcome) -> Outcome {
        guard out.actions.contains(where: { $0.hasPrefix("error ") }) else {
            failedCalls = [:]
            return out
        }
        let key = call.name + " " + call.parameters.map { "\($0.name)=\($0.value)" }.joined(separator: " ")
        let n = (failedCalls[key] ?? 0) + 1
        failedCalls[key] = n
        guard n >= 2 else { return out }
        var out = out
        out.text += "\n【同一步又失敗】一模一樣的 \(call.name) 已經失敗 \(n) 次了，再做一次也會一樣。換一個做法（別的元素、menu、open 帶 target、先讀別的視窗）；想不到別的做法，就用 todo.fail 老實說做不到、試過什麼，或用 ask_owner 問主人。"
        log("INFO", "turn \(turnIndex): \(call.name) failed \(n) times in a row with the same arguments")
        return out
    }

    /// `ask_owner` (2026-11-16, owner decision 4): her question goes to the owner's DM; with an item in
    /// progress it becomes 等待中 after this reply's calls, and his answer (an owner line) resumes it.
    func askOwner(what: String, where place: String, why: String) throws -> Outcome {
        let text = "【\(name)想先問你】\n要做：\(what)\n在哪裡：\(place)\n為什麼：\(why)"
        let shot = ownerShot
        if let failure = deliver(text, .owner, shot.map { shots.appending(path: $0) }) {
            log("ERROR", "turn \(turnIndex): ask_owner not delivered: \(failure)")
            throw ToolError(description: "主人沒收到：\(failure)。Discord 連上之前問不了；先不要做那件事。")
        }
        ownerShot = nil
        reachedOwner()
        guard todo.phase == .doing, let item = todo.current else {
            return Outcome(text: "問了主人（私訊）。他回了你才會知道。", actions: ["ask owner"], background: true)
        }
        waitNow = true
        // At a decision point (design C) asking the owner is one of the three choices.
        if todo.deciding { decided() }
        return Outcome(text: "問了主人（私訊）。這則回覆的呼叫做完以後，「\(item)」就是等待中：他回了才會再有回合。",
                       actions: ["ask owner"], background: true)
    }

    // todo -------------------------------------------------------------------
    /// The item in progress; a call that needs one throws with the phase when there is none.
    func currentItem(_ tool: String) throws -> String {
        // A todo call on the waiting item takes it up again (2026-11-20). In the turn a person's line
        // brought it back while her own list ran, an ending call is held once: it may be meant for her
        // own (m13-wait-idle: her own step's todo.done landed on the waiting item).
        if todo.phase == .waiting {
            if swappedForLine, !heldForAnswer, Self.heldTools.contains(tool), let mine = aside?.todo.current {
                heldForAnswer = true
                heldAt = turnIndex
                log("INFO", "turn \(turnIndex): \(tool) held: 「\(todo.current ?? "")」is in hand for a line, 「\(mine)」aside")
                throw ToolError(description: "還沒做：這一回合手上的是等著的「\(todo.current ?? "")」（有人說話了，程式先把它拿回你手上），你自己的「\(mine)」放在旁邊。"
                                + "他的話就是「\(todo.current ?? "")」要的答案，就再呼叫一次 \(tool)，這次就是給它的；你要做的是「\(mine)」，就呼叫 todo.wait（「\(todo.current ?? "")」繼續等），「\(mine)」就回到你手上。")
            }
            resumeWaiting(tool)
        }
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
        itemChain = []
        itemApps = []
        itemUsedComputer = false
        itemActed = false
        warnedTurns = 0
        latestRead = nil
        todo.rounds = 0
        forgetSeen()
    }

    /// After a compaction: what the program showed for the app left the request.
    func forgetSeen() {
        frontApp = nil
    }

    /// The action list a compaction leaves (2026-11-14): one short piece per action that went
    /// through, no screenshots, no results.
    static func actionList(_ steps: [String]) -> String {
        steps.isEmpty ? "（沒有）" : steps.joined(separator: "、")
    }

    /// `todo.set`: with no item in progress (待命中, 失敗) the list is replaced and its first item is
    /// 進行中 from her reply on (that reply is the item's first event); with one in progress only the
    /// items after it are replaced. No items: the list ends (待命中). `subgoal` (phase 2): the list
    /// works on that sub-goal; its items are its steps (new ones added to the tree first).
    func todoSet(_ items: [String], subgoal: Int? = nil, plan p: PlanInput, reply: Event) throws -> Outcome {
        func listed(_ xs: [String]) -> String { xs.isEmpty ? "沒有" : xs.map { "「\($0)」" }.joined() }
        // Hook 1 (2026-11-17): a list that starts hands in its plan; checked before anything changes.
        var plan: TodoPlan?
        var check: EstimateCheck?
        var listTextNow: String?
        // Her own step while a list waits (2026-11-20, owner: waiting is not busy): a `todo.set` with a
        // sub-goal starts its own list and sets the waiting one aside (`setAside`); one aside at most.
        let ownBeside = todo.phase == .waiting && subgoal != nil && !items.isEmpty
        if ownBeside, let a = aside {
            throw ToolError(description: "沒有列：「\(todo.current ?? "")」在等待中，旁邊已經放著「\(a.todo.current ?? "")」；一次最多一份等著、一份你自己的事。"
                            + "要做自己的事，先把手上這份做完或結束；要改「\(todo.current ?? "")」後面的項目，不要給 subgoal。")
        }
        if ownBeside || (todo.phase != .doing && todo.phase != .waiting && !items.isEmpty) {
            do {
                plan = try checkedPlan(p, items: items, reply: reply)
                let text = listText(items, reply: reply)
                check = try estimateCheck(p, estimate: plan!.estimate, text: text, items: items, reply: reply)
                listTextNow = text
            } catch {
                log("INFO", "turn \(turnIndex): todo.set refused: \(error)")
                throw error
            }
        }
        let linkNote = try subgoal.map { try linkTodo(items, subgoal: $0) } ?? ""
        let waiting = todo.current ?? ""
        if ownBeside { setAside() }
        switch todo.phase {
        case .doing, .waiting:
            todo.pending = items
            startLink(subgoal: subgoal, fresh: false)
            return Outcome(text: "後面的項目改成：\(listed(items))。現在\(todo.phase.label)的還是「\(todo.current ?? "")」。" + linkNote,
                           actions: ["todo set"])
        case .idle, .done, .failed:
            guard let first = items.first else {
                let ended = todo.phase == .failed
                todo = TodoState()
                listEndedNow = true
                listDropped()
                return Outcome(text: (ended ? "清單結束了，現在待命中。" : "沒有項目，還是待命中。") + backNote(), actions: ["todo end"], background: true)
            }
            startLink(subgoal: subgoal, fresh: todo.phase != .failed)
            todo.phase = .doing
            todo.current = first
            todo.pending = Array(items.dropFirst())
            todo.startSeq = reply.seq! - 1
            beginItem()
            todo.plan = plan!
            todo.listText = listTextNow
            todo.estimateCheck = check
            estimateRefusal = nil
            todo.listRounds = 0
            // The decision point counts the list's rounds (design C): a new list starts it again.
            todo.decideAt = computerConfig.decideAfterRounds
            todo.deciding = false
            todo.goOn = []
            todo.listStart = reply.ts
            // Whose line was waiting before this reply: a 「好，我來」 she wrote ahead of todo.set in the
            // same reply is an acknowledgement and does not make the list her own.
            listFor = Self.unanswered(timeline.events.filter { ($0.seq ?? 0) <= reply.seq! })
            listReply = reply.seq!
            listReported = false
            let compared = check.map { c in
                (c.low...c.high).contains(p.estimate!) ? (c.refused == nil ? "" : "（以前像這樣的清單實際 \(c.reference) 回合）")
                    : c.reason != nil ? "（跟以前像這樣的清單實際 \(c.reference) 回合不一樣，你寫的理由記下了，主人看得到）"
                    : "（跟以前像這樣的清單實際 \(c.reference) 回合差很多，沒寫理由，照收了；主人看得到）"
            } ?? ""
            return Outcome(text: "todo 列好了，你估整份 \(p.estimate!) 回合\(compared)。現在進行中：「\(first)」；後面：\(listed(todo.pending))。" + linkNote
                           + (ownBeside ? "「\(waiting)」還在等待中，先放在旁邊：有人說話，程式會先把它拿回你手上；這份做完或結束，它也回到你手上。" : ""),
                           actions: ["todo set"])
        }
    }

    /// `todo.done`: the item is 完成. Its span is compacted once the turn's results are in; the
    /// experiences attached are scored as a success; an item that used the computer is written as
    /// an experience (her result and its steps; turns and seconds from the item's start); the watch
    /// stops; the next item is 進行中 after this turn, or the list is done (待命中).
    func todoDone(_ result: String, evidence: String? = nil, reply: Event) async throws -> Outcome {
        let item = try currentItem("todo.done")
        // Done needs something done or looked at (owner, 2026-11-14): in a real run she ended an
        // untouched item right after the previous one was compacted, with a made-up result.
        guard itemActed else {
            throw ToolError(description: "「\(item)」這一項你還沒做任何動作，也沒看過結果，不能說完成。先動手做；如果覺得已經是做完的樣子，"
                            + (computerOn ? "至少用 read_window 或 read_file 親眼看過再說完成。" : "至少用工具查過（例如 recall、calendar.list）再說完成。"))
        }
        // Evidence from the latest read (design A, 2026-11-16: she told the owner 「畫面顯示 1702」 while
        // the display read "3"). Checked before the told-before-done rule: what she reports should be
        // what the read shows, so the read is settled first.
        if itemUsedComputer, let refusal = evidenceRefusal(evidence) {
            log("INFO", "turn \(turnIndex): todo.done「\(item)」refused: evidence「\(evidence ?? "")」not in the latest read")
            throw ToolError(description: refusal)
        }
        if todo.pending.isEmpty, let f = listFor, !listReported, !speaksLater.contains(f.to) {
            let tool = switch f.channel {
            case .discordDM: Tools.Name.dm
            case .discordVoice: voice == nil ? Tools.Name.dm : Tools.Name.voiceSay
            case .dollctl: Tools.Name.reply
            }
            throw ToolError(description: "還沒完成：這份清單是\(f.to.label)交代的，你列 todo 以後還沒跟\(f.to.label)說過話；todo.done 的 result 只有你自己看得到，寫「已回報」不會讓他收到。"
                            + "先用 \(tool) 跟\(f.to.label)說結果（和你怎麼確認的），再 todo.done；同一則回覆也可以。")
        }
        let steps = itemSteps.map(\.line)
        lastEnded = (item, ownerTask(before: reply, or: item), true, itemChain)
        compacting = Compaction(kind: .done, item: item, fromSeq: todo.startSeq, toSeq: 0)
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
        // Hook 2 (2026-11-17, feedback loop): her estimate next to what the list took so far; the
        // list's last item records it.
        var estimate = ""
        if let plan = todo.plan {
            let line = estimateLine(plan, rounds: todo.totalRounds, seconds: Int((reply.ts - todo.listStart).rounded()))
            if todo.pending.isEmpty {
                _ = try recordEstimate(outcome: .done, at: reply.ts)
                estimate = "這一項做了 \(todo.rounds) 回合；整份清單做完了：\(line)（記下了，下次列像這樣的清單，程式會拿來對你估的）。"
            } else {
                estimate = "這一項做了 \(todo.rounds) 回合；\(line)。"
            }
        }
        let step = itemFinished(item)
        let next = advance()
        let list = next == nil ? listFinished() : ""
        log("INFO", "turn \(turnIndex): todo 完成「\(item)」result「\(result)」, \(steps.count) actions")
        note += step + list
        return Outcome(text: "完成，記下了。\(note)\(estimate)這一項的過程（" + (computerOn ? "截圖、" : "") + "每一步的結果、那幾回合你的念頭）收起來了，之後只留你寫的 result 和這份動作清單："
                       + Self.actionList(steps) + "。"
                       + (next.map { "下一項「\($0)」現在進行中。" } ?? (aside == nil ? "清單做完了，現在待命中；程式不會再為這件事叫你。" : "清單做完了。" + backNote())),
                       actions: ["todo done"], ends: true)
    }

    /// `todo.fail` (only when she is sure): the item is 失敗; its span is compacted (her cause, the text
    /// she quoted for it and what she tried stay in her call); the experiences attached are scored as
    /// a failure; the watch stops; the computer tools stay refused until her `todo.set`. Hook 4
    /// (2026-11-17, failure analysis): the root cause passes the filler rules and is not the quote
    /// itself; the quote is in what the item saw (`itemTexts`), matched as `todo.done`'s evidence.
    func todoFail(cause: String, evidence: String, tried: String, reply: Event) throws -> Outcome {
        let item = try currentItem("todo.fail")
        if let problem = fillerRefusal([("cause", cause)], copies: [(what: "項目", text: item), (what: "你的 evidence", text: evidence)]) {
            throw ToolError(description: "還沒記成失敗：cause 要寫根本原因（造成它的原因，不是現象）。\(problem)")
        }
        let texts = itemTexts()
        guard !texts.isEmpty else {
            throw ToolError(description: "還沒記成失敗：這一項還沒有任何東西可以支持原因（" + (computerOn ? "沒讀過視窗或檔案、" : "") + "工具還沒有結果、也沒有人說過話）。先動手做一次、看清楚卡在哪，或用 ask_owner 問主人。")
        }
        guard Self.quoted(evidence, in: texts.joined(separator: "\n")) else {
            log("INFO", "turn \(turnIndex): todo.fail「\(item)」refused: evidence「\(evidence)」not in what the item saw")
            throw ToolError(description: "還沒記成失敗：evidence「\(evidence)」不在這一項你看過的東西裡（" + (computerOn ? "最新讀到的視窗或檔案、" : "") + "工具的結果、別人說的話）。"
                            + "從那裡照抄支持原因的字（標點、寫法照抄）；找不到支持的字，就是還不知道真正的原因：再查一下，或用 ask_owner 問主人。")
        }
        let steps = itemSteps.map(\.line)
        lastEnded = (item, ownerTask(before: reply, or: item), false, itemChain)
        compacting = Compaction(kind: .failed, item: item, fromSeq: todo.startSeq, toSeq: 0)
        scoreAttached(steps: steps, outcome: "failure")
        itemEndNow = TodoPhase.failed.label
        let record = try recordEstimate(outcome: .failed, at: reply.ts, cause: cause, evidence: evidence)
        todo.phase = .failed
        log("INFO", "turn \(turnIndex): todo 失敗「\(item)」cause「\(cause)」evidence「\(evidence)」")
        signal(.itemFailed, item)
        var concernNote = ""
        if drivesConfig.enabled {
            let c = try addConcern("沒做成：\(item)（\(cause)）", about: .duty, source: "todo", key: "todo:\(todo.startSeq)")
            concernNote = "這件事程式記成你的掛念 #\(c.id)；之後做成或決定放下，用 concern.end 結束它。"
        }
        let estimate = record.map { estimateLine($0.plan, rounds: $0.rounds, seconds: $0.seconds) + "（記下了）。" } ?? ""
        let rest = todo.pending.isEmpty ? "（後面沒有別的項目了）" : "（剩下：" + todo.pending.map { "「\($0)」" }.joined() + "）"
        return Outcome(text: "記下了：「\(item)」沒做成。\(estimate)這一項的過程收起來了，之後只留你寫的原因、試過的和這份動作清單：\(Self.actionList(steps))。"
                       + (computerOn ? "電腦工具先鎖著。" : "") + "現在決定剩下的\(rest)：用 todo.set 重寫剩下的項目；或照原樣列剩下的，接著做下一項；或跟主人說了以後用 todo.set 給空的陣列結束清單。" + concernNote,
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
        todo.listRounds += todo.rounds
        beginItem()
        // Finishing an item at a decision point is a way of going on: the next point is `decide_every_rounds` on.
        if todo.deciding { decided() }
        return todo.current
    }

    // The ten ways of thinking (2026-11-17, owner) ----------------------------------
    /// Letters and digits only, lowercased: what the filler rules count and compare.
    static func plainKey(_ s: String) -> String { String(s.filter { $0.isLetter || $0.isNumber }).lowercased() }

    /// The filler rules, the same for every written field of hooks 1, 3 and 4. A field is refused when
    /// (1) it is missing or empty; (2) it has fewer than `[computer] plan_min_chars` letters or digits
    /// (「沒有」「無」「同上」「N/A」); (3) its letters and digits are exactly those of another of the
    /// fields; (4) they are exactly those of one of `copies` (the item, the line she was given): a
    /// copy of the question. Nothing judges whether what she wrote is right.
    func fillerRefusal(_ fields: [(name: String, value: String?)], copies: [(what: String, text: String)]) -> String? {
        let min = computerConfig.planMinChars
        for (i, f) in fields.enumerated() {
            guard let v = f.value, !v.isEmpty else { return "少了 \(f.name)。" }
            let key = Self.plainKey(v)
            guard key.count >= min else { return "\(f.name)「\(v)」太短：至少 \(min) 個字，寫具體的。" }
            if let other = fields[..<i].first(where: { $0.value.map(Self.plainKey) == key }) {
                return "\(f.name) 跟 \(other.name) 寫得一模一樣：各寫各的。"
            }
            if let copy = copies.first(where: { Self.plainKey($0.text) == key }) {
                return "\(f.name) 只是照抄了\(copy.what)：寫你自己的。"
            }
        }
        return nil
    }

    /// Words that say "no limits" without saying one, as letters and digits (`plainKey`), longest first.
    static let noLimitWords = ["不適用", "不适用", "because", "boundary", "沒有", "没有", "限制", "邊界", "边界", "界限", "特別", "特别",
                               "因為", "因为", "limits", "limit", "none", "無", "无", "no", "na"]

    /// `limits` says a limit or why there is none (2026-11-17, m7-mac seq a-2, a-3: 「無」 was refused as
    /// too short and the rewrite cost a round; owner: an honest none is fine when she says why). With
    /// every `noLimitWords` word taken out of its letters and digits, at least `[computer] plan_min_chars`
    /// must remain: 「沒有，因為只在計算機裡按幾個鍵、不動別的」 keeps 「只在計算機裡按幾個鍵不動別的」 and passes;
    /// 「無」「沒有限制」「沒有，因為沒有」「none」 keep nothing and are refused. The other filler rules follow.
    func limitsRefusal(_ limits: String?) -> String? {
        guard let limits, !limits.isEmpty else { return nil }
        var rest = Self.plainKey(limits)
        for word in Self.noLimitWords { rest = rest.replacingOccurrences(of: word, with: "") }
        let min = computerConfig.planMinChars
        guard rest.count < min else { return nil }
        return "limits「\(limits)」沒寫出邊界：寫不能碰的、做不到的、有限的是什麼；真的沒有，就寫「沒有，因為…」把理由寫出來（理由至少 \(min) 個字）。"
    }

    /// Hook 1: the plan of a `todo.set` that starts a list, or a refusal (thrown) without it. Every
    /// field passes the filler rules; each fact (1 to `[computer] plan_max_facts`) has a quote that
    /// is in something she can cite (`citable`), matched as `todo.done`'s evidence; the program records
    /// where it found it. It cannot judge whether the fact follows from its quote.
    func checkedPlan(_ p: PlanInput, items: [String], reply: Event) throws -> TodoPlan {
        let copies = planCopies(items, reply: reply)
        func refuse(_ problem: String) -> ToolError {
            ToolError(description: "清單沒有列：開始一份清單要一起寫 facts（確定的事實，每條帶出處）、assumptions（只是假設的）、limits（邊界）、estimate（整份估幾回合）、check（做完的樣子、怎麼確認）。\(problem)")
        }
        guard let given = p.facts, !given.isEmpty else { throw refuse("少了 facts。") }
        let max = computerConfig.planMaxFacts
        guard given.count <= max else { throw refuse("facts 最多 \(max) 條，你寫了 \(given.count) 條：留最要緊的。") }
        if let problem = limitsRefusal(p.limits)
            ?? fillerRefusal([("assumptions", p.assumptions), ("limits", p.limits), ("check", p.check)], copies: copies)
            ?? (p.estimate == nil ? "少了 estimate。" : p.estimate! < 1 ? "estimate 要是 1 以上的整數（回合數）。" : nil) {
            throw refuse(problem)
        }
        let sources = citable(before: reply)
        let places = computerOn ? "主人說的話、工具的結果、讀過的視窗或檔案、【狀況】、你的目標樹、【相關記憶】裡〔工具〕那一行" : "主人說的話、行事曆、你的目標樹、【狀況】"
        var facts: [PlanFact] = []
        for (i, f) in given.enumerated() {
            let name = "facts 第 \(i + 1) 條"
            if let problem = fillerRefusal([("\(name)的 fact", f.fact)], copies: copies) { throw refuse(problem) }
            let fact = f.fact!
            guard let quote = f.quote, !quote.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
                throw ToolError(description: "清單沒有列：\(name)「\(fact)」沒有 quote。fact 要有出處：quote 照抄撐住它的字（\(places)）。"
                                + "找不到出處，就是你以為的：搬到 assumptions。")
            }
            guard Self.plainKey(quote).count >= computerConfig.planMinChars else {
                throw refuse("\(name)的 quote「\(quote)」太短：至少 \(computerConfig.planMinChars) 個字，照抄一整段。")
            }
            guard let source = sources.first(where: { Self.quoted(quote, in: $0.text) }) else {
                // Her own words inside a program line about her tools (a tool's `when`, a value she gave
                // `tool.save`) are cut before the match: say which (2026-11-18, m7-mac p-1, p-2).
                let saveArgs = timeline.events.filter { $0.kind == .doll && $0.seq! < reply.seq! }.flatMap(\.calls)
                    .filter { $0.name == Tools.Name.toolSave }.flatMap { Self.argumentStrings($0, except: ["name"]) }
                let mine = (ownToolWords() + saveArgs.sorted { $0.count > $1.count })
                    .first { quote.contains($0) && Self.plainKey($0).count >= computerConfig.planMinChars }
                let own = mine.map { "quote 裡的「\($0)」是你存工具時自己寫的字，不算出處；只抄程式或別人寫的部分。" } ?? ""
                throw ToolError(description: "清單沒有列：\(name)「\(fact)」的 quote「\(quote)」不在你看得到的地方（\(places)）。\(own)fact 要有出處：quote 一字不差照抄那裡的字。"
                                + "找不到出處，就是你以為的：把它搬到 assumptions；想確定，就把「去看」當成清單裡的一步，看到了再說。")
            }
            facts.append(PlanFact(fact: fact, quote: quote, source: source.source))
        }
        return TodoPlan(items: items, facts: facts, assumptions: p.assumptions!, limits: p.limits!, estimate: p.estimate!, check: p.check!)
    }

    /// What a fact of her plan may quote (2026-11-17, owner: a fact needs where she saw it), each with
    /// where it is from: the 【狀況】 her reply answered; her latest read (window list, or the file
    /// read again) even when its turn was put away; and, of the events before her reply, what the
    /// request still sends whole: people's lines, the results of the tools that look at the computer
    /// and files (`Tools.gated`; window lists while still sent; watch wakes' window lists), the
    /// calendar, and two lines the program writes about her own tools (2026-11-18, m7-mac tools: 5
    /// of 7 refused plans quoted them): a `tool.save` result and the 〔工具〕 lines of 【相關記憶】.
    /// Never her own words: not her replies or her memories, not the results of her other tools (a
    /// refused `todo.set` repeats her facts back; a test run showed it then passed as their own
    /// source); from a computer or file result her call's own argument values are cut first (an
    /// error that repeats the menu path she asked for is not a source for that path); from a
    /// `tool.save` result its call's argument values except the name, and from both tool lines what
    /// she wrote about her tools (`ownToolWords`).
    func citable(before reply: Event) -> [(source: String, text: String)] {
        var out: [(source: String, text: String)] = []
        if let footer = timeline.events.last(where: { $0.seq == reply.seq })?.footer { out.append(("【狀況】", footer)) }
        // Her goal tree as the program holds it now, like the calendar (2026-11-20, m12-idle-drive: 11
        // plans for a step of her own were refused for quoting the step they were about): a list that
        // does a step may say what the step is. The words are hers; that the node exists is the program's.
        for n in goalFile.nodes where n.ended == nil { out.append(("目標樹", "#\(n.id)〔\(n.level.label)〕\(n.text)")) }
        switch latestRead {
        case .window(let text): out.append(("最新讀到的視窗", text))
        case .file(let path): if let t = try? FileText.decode(try LocalFiles.read(path), path: path) { out.append(("檔案 \(path)", t)) }
        case nil: break
        }
        let (plan, kept) = contextPlan(timeline.events.filter { $0.seq! < reply.seq! })
        var own: [String]?
        func cut(_ text: String) -> String {
            if own == nil { own = ownToolWords() }
            return own!.reduce(text) { $0.replacingOccurrences(of: $1, with: "") }
        }
        var calls: ArraySlice<ToolCall> = []
        for (ev, mode) in plan {
            if case .reply(let surviving) = mode { calls = surviving[...]; continue }
            guard case .full = mode else { continue }
            switch ev.kind {
            case .owner, .developer: out.append(("\(ev.speaker?.label ?? "?")說的", ev.text))
            case .doll: calls = ev.calls[...]
            case .tool:
                guard let call = calls.popFirst() else { continue }
                if call.name == Tools.Name.toolSave {
                    // The program's name for the file it keeps stays: 「存好了：工具「乘法」」 is its word that it exists.
                    let rest = Self.argumentStrings(call, except: ["name"]).filter { $0.count >= 2 }.sorted { $0.count > $1.count }
                    out.append(("tool.save 的結果", cut(rest.reduce(ev.text) { $0.replacingOccurrences(of: $1, with: "") })))
                    continue
                }
                guard Tools.gated.contains(call.name) else { continue }
                out.append(("工具的結果", Self.withoutArguments(ev.text, of: call)))
                if kept.contains(ev.seq!), let w = ev.window { out.append(("讀到的視窗", w)) }
            case .watch:
                if kept.contains(ev.seq!), let w = ev.window { out.append(("讀到的視窗", w)) }
            case .calendar: out.append(("行事曆", ev.text))
            case .memory:
                // What the owner did in the dashboard is his word, as a line of his is.
                if ev.text.hasPrefix("【主人改了你") { out.append(("主人改的", ev.text)) }
                for line in ev.text.split(separator: "\n") where line.hasPrefix("〔工具〕") {
                    out.append(("【相關記憶】提的工具", cut(String(line))))
                }
            }
        }
        return out
    }

    /// What she wrote about her saved tools (2026-11-18), cut from the program's lines about them
    /// before they count as a source: of every saved tool its `when`, each parameter's `about` and
    /// the item it was saved from (she wrote them, maybe on another day: what she believed then, not
    /// something the program found). Its name, step lines, values found in the steps, counts and
    /// usage stay. At least 2 characters, longest first.
    func ownToolWords() -> [String] {
        let words = ((try? toolStore.all()) ?? []).flatMap { [$0.when, $0.origin.item] + $0.params.map(\.about) }
        return Set(words.filter { $0.count >= 2 }).sorted { $0.count > $1.count }
    }

    /// Every string of `call`'s argument values (a JSON value's strings one by one, also inside
    /// `act`'s steps: a menu path, a typed text, a name, her expect), the parameters in `except` left out.
    static func argumentStrings(_ call: ToolCall, except: Set<String> = []) -> [String] {
        func leaves(_ v: Any) -> [String] {
            switch v {
            case let s as String: [s]
            case let a as [Any]: a.flatMap(leaves)
            case let o as [String: Any]: o.values.flatMap(leaves)
            default: []
            }
        }
        return call.parameters.filter { !except.contains($0.name) }.flatMap { p -> [String] in
            guard let j = try? JSONSerialization.jsonObject(with: Data(p.value.utf8), options: [.fragmentsAllowed]), j is [Any] || j is [String: Any] else { return [p.value] }
            return leaves(j)
        }
    }

    /// `text` with every argument value of `call` cut out (`argumentStrings` of at least 2 characters, longest first).
    static func withoutArguments(_ text: String, of call: ToolCall) -> String {
        argumentStrings(call).filter { $0.count >= 2 }.sorted { $0.count > $1.count }
            .reduce(text) { $0.replacingOccurrences(of: $1, with: "") }
    }

    /// What a plan field may not merely copy: her items and the last line someone said before her reply.
    func planCopies(_ items: [String], reply: Event) -> [(what: String, text: String)] {
        var copies = items.map { (what: "項目", text: $0) }
        if let line = timeline.events.last(where: { $0.speaker != nil && $0.seq! < reply.seq! }) {
            copies.append((what: "對方說的話", text: line.text))
        }
        return copies
    }

    /// What a list is compared by (2026-11-18, estimate check): the last line someone said before
    /// her reply when it came after the last finished list ended (the list answers it), else her
    /// items one per line (a list of her own, or one re-set after a failure). Measured on 109 lists:
    /// the owner's line separates kinds (different kinds at most 0.847, same kind at least 0.94 with
    /// digits masked); her items do not (different kinds up to 0.90, same kind down to 0.48).
    func listText(_ items: [String], reply: Event) -> String {
        let since = estimateFile.records.last?.ts ?? 0
        if let line = timeline.events.last(where: { $0.speaker != nil && $0.seq! < reply.seq! }), line.ts > since {
            return line.text
        }
        return items.joined(separator: "\n")
    }

    /// A list text with its digits masked (numbers, the numbers in a path): a list's kind is not its numbers.
    static func maskedDigits(_ s: String) -> String {
        s.replacing(/[0-9０-９]+([.,][0-9]+)*/, with: "#")
    }

    /// Her estimate is far from `reference` (`[computer] estimate_far_ratio`, `estimate_far_rounds`):
    /// the larger is more than ratio times the smaller and they differ by at least that many rounds.
    func estimateFar(_ estimate: Int, _ reference: Int) -> Bool {
        let (lo, hi) = (min(estimate, reference), max(estimate, reference))
        return Double(hi) > computerConfig.estimateFarRatio * Double(lo) && hi - lo >= computerConfig.estimateFarRounds
    }

    /// The estimate check at the `todo.set` that starts a list (2026-11-18, owner: the program
    /// compares). Past lists with a text whose cosine to `text` (digits masked, both embedded as
    /// documents) is at least `[computer] estimate_similarity`; the most recent `estimate_similar_max`
    /// of them; the reference is the median of their actual rounds (rounded half up; the median, so
    /// one slow list, e.g. one that also saved a tool, does not move it). None alike: nil, accepted.
    /// Her estimate far from the reference (`estimateFar`) is refused once with what they took. The
    /// next `todo.set` with the same text: an estimate not far passes; one still far passes with an
    /// `estimate_reason` that passes the filler rules; without one it is refused once more, for the
    /// reason only; after that it passes, whatever the estimate. A reason that passes the filler rules
    /// is recorded with any accepted estimate that was compared. An embedding failure is
    /// logged and the estimate accepted (as a tool offer is skipped).
    func estimateCheck(_ p: PlanInput, estimate: Int, text: String, items: [String], reply: Event) throws -> EstimateCheck? {
        let past = estimateFile.records.filter { $0.text != nil }
        guard !past.isEmpty else { return nil }
        let like: [EstimateRecord]
        do {
            let keys = ([text] + past.map { $0.text! }).map(Self.maskedDigits)
            let missing = Array(Set(keys.filter { listVectors[$0] == nil }))
            if !missing.isEmpty {
                for (k, v) in zip(missing, try embedder.embed(missing, as: .document)) { listVectors[k] = v }
            }
            let q = listVectors[keys[0]]!
            let scored = zip(past, keys.dropFirst()).map { r, k in (r, Double(zip(q, listVectors[k]!).reduce(Float(0)) { $0 + $1.0 * $1.1 })) }
            log("INFO", "turn \(turnIndex): estimate check cosine " + scored.map { String(format: "%.3f", $0.1) }.joined(separator: " "))
            like = scored.filter { $0.1 >= computerConfig.estimateSimilarity }.map(\.0).suffix(computerConfig.estimateSimilarMax)
        } catch {
            log("ERROR", "turn \(turnIndex): estimate check skipped: \(error)")
            return nil
        }
        guard !like.isEmpty else { return nil }
        let rounds = like.map(\.rounds).sorted()
        let n = rounds.count
        let reference = n % 2 == 1 ? rounds[n / 2] : Int((Double(rounds[n / 2 - 1] + rounds[n / 2]) / 2).rounded())
        let close = (1...max(1, reference * Int(computerConfig.estimateFarRatio.rounded(.up)) + computerConfig.estimateFarRounds))
            .filter { !estimateFar($0, reference) }
        var check = EstimateCheck(past: like.map { .init(items: $0.plan.items, estimate: $0.plan.estimate, rounds: $0.rounds) },
                                  reference: reference, low: close.first!, high: close.last!, refused: nil, reason: nil)
        // A reason she wrote with an accepted estimate is recorded (2026-11-18 run: she wrote one
        // unasked 5 times in 24 lists); it lets a far estimate pass only after the refusal.
        let copies = planCopies(items, reply: reply)
        let reason = p.estimateReason.flatMap { fillerRefusal([("estimate_reason", $0)], copies: copies) == nil ? $0 : nil }
        if let prior = estimateRefusal, prior.text == text {
            check.refused = prior.check.refused
            check.reason = reason
            guard estimateFar(estimate, reference), reason == nil else { return check }
            if prior.reasonAsked { return check }
            estimateRefusal!.reasonAsked = true
            let problem = p.estimateReason.flatMap { fillerRefusal([("estimate_reason", $0)], copies: copies) } ?? ""
            throw ToolError(description: "清單沒有列：estimate 還是 \(estimate) 回合，不在 \(check.low) 到 \(check.high) 之間。"
                            + "改成這中間的數；或照你的估法，在 estimate_reason 寫這次跟以前那幾份哪裡不一樣。\(problem)")
        }
        guard estimateFar(estimate, reference) else {
            check.reason = reason
            return check
        }
        check.refused = estimate
        estimateRefusal = (text, check, false)
        let listed = like.map { r in "「\(r.plan.items.joined(separator: "、"))」你估 \(r.plan.estimate)、實際 \(r.rounds) 回合" }.joined(separator: "；")
        let way = estimate < reference ? "少" : "多"
        throw ToolError(description: "清單沒有列：你估整份 \(estimate) 回合，可是以前做過像這次的清單：\(listed)。"
                        + "實際的中位數是 \(reference) 回合，你估的比它\(way)太多；估 \(check.low) 到 \(check.high) 回合才算接近。"
                        + "再列一次：改 estimate；或照你的估法，加 estimate_reason 寫這次跟以前那幾份哪裡不一樣。")
    }

    /// Hook 2: the estimate-vs-actual line of the list so far.
    func estimateLine(_ plan: TodoPlan, rounds: Int, seconds: Int) -> String {
        "你估整份清單 \(plan.estimate) 回合，實際到現在 \(rounds) 回合、\(seconds) 秒"
    }

    /// Hook 2: the list ended (its last `todo.done`, or a `todo.fail`): its estimate against what it
    /// took goes to `estimates.json`. A list from an older timeline has no plan and is not recorded.
    func recordEstimate(outcome: TodoPhase, at ts: Double, cause: String? = nil, evidence: String? = nil) throws -> EstimateRecord? {
        guard let plan = todo.plan else { return nil }
        let r = EstimateRecord(ts: ts, plan: plan, rounds: todo.totalRounds,
                               seconds: Int((ts - todo.listStart).rounded()), outcome: outcome.label, cause: cause, evidence: evidence,
                               text: todo.listText, check: todo.estimateCheck)
        estimateFile.records.append(r)
        try Concerns.write(estimateFile, estimatesURL)
        log("INFO", "turn \(turnIndex): estimate recorded: \(plan.estimate) rounds estimated, \(r.rounds) rounds \(r.seconds) s, \(r.outcome)")
        return r
    }


    /// Hook 4: what a root cause may quote: the latest read (window list, or the file read again) and,
    /// from the current item, every tool result other than her todo calls' (window lists included)
    /// and every line a person said. Empty: the item has nothing yet to support a cause with.
    func itemTexts() -> [String] {
        var out: [String] = []
        switch latestRead {
        case .window(let text): out.append(text.split(separator: "\n", omittingEmptySubsequences: false).dropFirst().joined(separator: "\n"))
        case .file(let path): if let t = try? FileText.decode(try LocalFiles.read(path), path: path) { out.append(t) }
        case nil: break
        }
        var names: [String] = []
        for e in timeline.events where e.seq! > todo.startSeq {
            if e.kind == .doll { names = e.calls.map(\.name); continue }
            if e.speaker != nil { out.append(e.text); continue }
            guard e.kind == .tool else { continue }
            let name = names.isEmpty ? "" : names.removeFirst()
            if name.hasPrefix("todo.") { continue }
            out.append(e.text)
            if let w = e.window { out.append(w) }
        }
        return out.filter { !$0.isEmpty }
    }

    /// `evidence` is in `haystack` (design A's matching): whole, or every piece of it split at ；, ; or
    /// line breaks. Both sides are compared without `read_file`'s display (`undecorated`).
    static func quoted(_ evidence: String, in haystack: String) -> Bool {
        let evidence = undecorated(evidence)
        let read = evidenceKey(undecorated(haystack))
        let keys = evidence.split(whereSeparator: { "；;\n".contains($0) }).map { evidenceKey(String($0)) }.filter { !$0.isEmpty }
        let key = evidenceKey(evidence)
        return !key.isEmpty && (read.contains(key) || (!keys.isEmpty && keys.allSatisfy(read.contains)))
    }

    /// Text without what `read_file` adds around a file's lines (2026-11-17, m7-mac m-1/m-2: she
    /// copied its result as shown, 「read_file <path>：共 7 行」 and 「     1\t開頭」, and every honest copy
    /// was refused): its header line is dropped and a line's number prefix (`cat -n`'s 「     1\t」, or
    /// 「  1→」 as a refusal shows a file) is cut. Only decoration goes; the rest must still be in the
    /// read, so a line that is not in the file still fails, and a header alone is nothing.
    static func undecorated(_ s: String) -> String {
        s.split(separator: "\n", omittingEmptySubsequences: false).compactMap { line -> Substring? in
            if line.wholeMatch(of: /\s*read_file\s+\S.*：(共\s*\d+\s*行|空檔案).*/) != nil { return nil }
            if let m = line.prefixMatch(of: /\s*\d+(\t|→)/) { return line[m.range.upperBound...] }
            return line
        }.joined(separator: "\n")
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
        // the registry's app entries by their bundle ids.
        var entities: [MemoryEntity] = []
        for app in itemApps {
            entities.append(MemoryEntity(try pipeline.programApp(app).entry.id, app.name))
        }
        let id = try memory.write(text, folder: .public, sourceSeq: reply.seq!, tier: .selfReported, kind: nil,
                                  experience: (task: task, stats: stats), entities: entities, now: Date().timeIntervalSince1970)
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
        let text = "【收起】context 快滿了，你沒有用 todo.progress 收，程式把「\(item)」到這裡為止的過程收起來了：" + (computerOn ? "截圖、" : "") + "每一步的結果和那幾回合你的念頭都不在了，"
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
        用她平常說話看得懂的話寫內容本身（「主人對花生過敏」），不要寫系統內部的東西：記憶檔的檔名或編號、資料夾、confidence、事件編號、工具呼叫的參數格式。
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
            // Never into an item in progress, in hand or set aside; an item 等待中 does not bound it
            // (2026-11-21, owner; soak run: a list waiting 20 h held the whole day's conversation in
            // the request): what of its list stays is `contextPlan`'s.
            let bound = [todo, aside?.todo].compactMap { $0 }.filter { $0.phase != .idle && $0.phase != .waiting }.map(\.startSeq).min() ?? timeline.lastSeq
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
    static let observingTools: Set<String> = ["open", "read_window", "act", "tool.run", "read_file", "edit_file"]

    /// The program fills seq, ts, confidence; the kind and folder are hers (spec 6, 2026-11-08). The
    /// tier comes from the event she is answering, not from anything she can claim; results of her
    /// own memory and think calls are not sources. Her memories carry no entities (2026-11-15: no
    /// model links the people and things her text names; experiments/m6-registry-eval).
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
        let now = Date().timeIntervalSince1970
        var lines: [String] = [], actions: [String] = []
        for (i, item) in items.enumerated() {
            let folder = memory.dirName(item.folder)
            do {
                if let bad = item.badKind {
                    throw ToolError(description: "kind 只能是 happened、intended、uncertain，或不填；收到 \(bad)")
                }
                let id = try memory.write(item.text, folder: item.folder, sourceSeq: seq, tier: tier, kind: item.kind,
                                          entities: [], deep: item.deep, auditSegment: timeline.segment, now: now)
                log("INFO", "turn \(turnIndex): remember \(id)\(item.deep ? " (deep)" : "")")
                let kind = item.kind.map { "，kind \($0.rawValue)" } ?? ""
                lines.append("第 \(i + 1) 條已存入 \(folder)/\(id).md（confidence \(memoryConfig.confidence[tier]!)\(kind)\(item.deep ? "，deep：不會淡" : "")）")
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
            case .owner, .developer, .watch, .memory, .calendar: break
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

    /// The encrypted memory folders by directory name (`Memory.sealedFolders`).
    var sealedFolders: Set<String> { Memory.sealedFolders(memoryConfig) }

    /// Whether her calls carry content of an encrypted folder, so her reply is stored encrypted: a
    /// `remember` item into `secret` or `own` (its text is in the arguments), a `memory.fix` of a
    /// memory there (the new text is).
    func carriesSealed(_ calls: [ToolCall]) throws -> Bool {
        let folder = try NSRegularExpression(pattern: #"folder\\?"\s*:\s*\\?"(secret|own)\\?""#)
        for call in calls {
            let text = call.arguments ?? call.malformed ?? ""
            if call.name == "remember", folder.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)) != nil { return true }
            if call.name == Tools.Name.memoryFix, let id = call.parameters.first(where: { $0.name == "id" })?.value,
               let (f, _) = try memory.file(id.trimmingCharacters(in: .whitespacesAndNewlines)), sealedFolders.contains(memory.dirName(f)) {
                return true
            }
        }
        return false
    }

    /// Folders by `Memory.visible`: `secret` only if the turn saw secret events (5.1), `own` in the
    /// owner's turns and hers. Her memories and today's events (today's secret events only where the
    /// secret folder is visible) through the selection pipeline (`Selector.run`, `Rule.recall`):
    /// everything related to her query, the `[select] recall_limit` best by the fused ranking. No
    /// relevance gate beyond the search (she judges; 2026-11-15): a query nothing matches well still
    /// returns the nearest; 「0 條」 only when there is nothing to search.
    func recall(_ query: String, seen events: [Event]) async throws -> Outcome {
        let visible = Memory.visible(Self.audience(events), sawSecret: events.contains(where: \.secret))
        // The label goes to the log (plain text on disk): her query stays out of it in a turn that saw a secret.
        let trigger = Selector.Trigger(label: events.contains(where: \.secret) ? "recall（秘密的回合，不記查詢）" : "recall「\(query)」", query: query, app: nil)
        // The owner's or developer's last line is the one she is answering: not a result (2026-11-15,
        // real runs: the question itself came back as the first of her three). Later events can be,
        // a compaction may have taken them out of her request.
        let asked = events.last { $0.speaker != nil }?.seq ?? -1
        let report = try pipeline.run(trigger, rule: .recall, visible: visible, tiers: nil, today: today,
                                      now: Date().timeIntervalSince1970) { c in
            if case .today(let h) = c.hit, h.seq == asked { return nil }
            return c.key
        }
        logSelection(report)
        let hits = report.selected.map(\.hit)
        var out = "\(query)：\(hits.count) 條"
        for hit in hits {
            out += "\n\n" + hit.rendered
        }
        // A secret hit makes the result a secret event (5.1: what the turn saw is what it may say); a
        // hit of an encrypted folder, or of an encrypted event of today, makes it stored encrypted.
        let sealed = hits.contains { hit in
            switch hit {
            case .memory(let h): sealedFolders.contains(h.folder)
            case .today(let h): timeline.events.contains { $0.seq == h.seq && $0.sealed }
            }
        }
        return Outcome(text: out, secret: hits.contains(where: \.secret), sealed: sealed, actions: ["recall \(hits.count)"])
    }

    /// One selection's line: to the log and the turn's report.
    func logSelection(_ report: SelectionReport) {
        log("INFO", "turn \(turnIndex): " + report.line)
        selectionsNow.append(report.line)
    }

    /// The program's own recall (spec 3.1, 2026-11-09; registry 2026-11-11; no on-device model
    /// 2026-11-15): on a new owner line and when a computer result's front app changed,
    /// `Selector.run` (`Rule.attach`) with the owner's last line plus the front app's name as the
    /// query and the front app's registry entry (by its bundle id) as the trigger's entity.
    /// Experiences (tier self_reported), consolidated memories and (2026-11-15) deep memories only
    /// (her other memories and the program's shortcuts stay for `recall`), in the turn's visible
    /// folders; a memory that has faded below `[select] strength_floor` is not given. Experiences
    /// for one task are one group (`Memory.experienceGroups`): only its best-ranked one
    /// (`Memory.ranked`) is a candidate; a consolidated or deep memory is its own group. Those
    /// attached since the owner's last line are left out. At most `[select] attach_max` whose
    /// cosine to the query is at least `[select] similarity`, best cheap score first, become one
    /// 【相關記憶】 event, re-sent byte-identical like every event. None, no event. Each goes in whole
    /// (an experience's length is capped when written), an experience's stats line first. A failure
    /// is logged and attaches nothing; the turn goes on.
    func attachMemories(trigger label: String) async {
        do {
            let events = timeline.events
            guard let owner = events.last(where: { $0.speaker != nil }) else { return }
            let query = [owner.text, frontApp?.name].compactMap { $0 }.joined(separator: " ")
            let visible = Memory.visible(Self.audience(events), sawSecret: events.contains(where: \.secret))
            let trigger = Selector.Trigger(label: label, query: query, app: frontApp)
            let groups = try memory.experienceGroups(try memory.experiences(visible: visible))
            var picks: [String: Experience] = [:], already = Set<String>(), merged = Set<String>()
            let report = try pipeline.run(trigger, rule: .attach, visible: visible, tiers: Memory.strengthTiers.union([.selfReported]),
                                          today: nil, now: Date().timeIntervalSince1970) { c in
                let pick: Experience
                if let m = try memory.attachable(c.id) {
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
                return "m:" + pick.id
            }
            // A secret line stays out of the log (2026-11-21: the log is plain text on disk).
            log("INFO", "turn \(turnIndex): 相關記憶（\(label)）query「\(owner.secret ? "（秘密，不記）" : query)」: task groups \(merged.sorted().joined(separator: " ")), "
                + "already attached \(already.count) \(already.sorted())")
            logSelection(report)
            let fresh = report.selected.compactMap { picks[$0.id] }
            // Her own tools that fit the same query (2026-11-17, owner decision 6): told here, not listed always.
            let offered = offerTools(query: query, label: label)
            guard !fresh.isEmpty || !offered.isEmpty else { return }
            attachedIDs.formUnion(fresh.map(\.id))
            offeredTools.formUnion(offered.map(\.name))
            let from = frontApp.map { "主人的話和前景 app「\($0.name)」" } ?? "主人的話"
            var text = fresh.isEmpty ? "【相關記憶】程式用\(from)查到你存的工具（字面相近，不一定用得上）：" : "【相關記憶】程式用\(from)查到、最相近的過去經驗："
            for e in fresh {
                text += "\n〔\(e.label)｜\(e.folder)｜記憶 \(e.id)〕\n\(e.attachedText)"
            }
            if !offered.isEmpty {
                text += (fresh.isEmpty ? "" : "\n你存的工具裡可能用得上的：") + offered.map { "\n〔工具〕" + $0.offerLine }.joined()
            }
            try append(Event(.memory, .internal, text, secret: fresh.contains(where: \.secret),
                             sealed: fresh.contains { sealedFolders.contains($0.folder) }))
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
