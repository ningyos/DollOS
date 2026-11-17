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
    let filesConfig: FilesConfig
    let idle: IdleConfig
    let memoryConfig: MemoryConfig
    let selectConfig: SelectConfig
    /// Screenshot files; events name them, and each is re-sent byte-identical every turn.
    public nonisolated let shots: URL
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
    /// Her DollOS documents (`[core] docs`), read in place: `docsPath` is this folder's absolute path.
    let docsDir: URL
    var docsPath: String { docsDir.standardizedFileURL.path }
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
    var drives: Drives
    /// No signals file yet: every signal starts at its start value when the calendar is first
    /// caught up (the clock then is the right one).
    var signalsFresh = false
    /// The one task sleeping until the next signal crossing or concern change.
    var drivesTask: Task<Void, Never>?
    var drivesSerial = 0
    /// She finished one of her own concerns: with 動力 high, the next one surfaces at once.
    var chainAfter: Int?
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

    /// `embedder` is loaded by the caller before this (`MLXEmbedder.load`; tests: a fake): the today
    /// index embeds the segment's events here. `maintenanceModel`: the maintenance's requests; nil = `model`
    /// (tests script one model for both).
    public init(config: Config, root: URL, model: any MainModel,
                embedder: any Embedder, computer: (any ComputerDriver)? = nil, maintenanceModel: (any MainModel)? = nil,
                deliver: @escaping Deliver) throws {
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
        let data = core.dataDir.hasPrefix("/") ? URL(filePath: core.dataDir) : root.appending(path: core.dataDir)
        shots = data.appending(path: "shots")
        try FileManager.default.createDirectory(at: shots, withIntermediateDirectories: true)
        timelineDir = data.appending(path: "timeline")
        timeline = try Timeline(dir: timelineDir)
        maintenanceURL = data.appending(path: "maintenance.json")
        maintenanceState = try Self.readMaintenance(maintenanceURL) ?? MaintenanceFile()
        lastSpoke = timeline.events.last { $0.speaker != nil }?.ts ?? 0
        ownerLastLine = timeline.events.last { $0.kind == .owner }?.ts ?? 0
        drivesConfig = config.drives
        concernsURL = data.appending(path: "concerns.json")
        signalsURL = data.appending(path: "signals.json")
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
        let memRoot = memoryConfig.dir.hasPrefix("/") ? URL(filePath: memoryConfig.dir) : root.appending(path: memoryConfig.dir)
        memory = try Memory(config: memoryConfig, root: memRoot, store: try Store(path: data.appending(path: "doll.db")),
                            embedder: embedder)
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
        docsDir = core.docs.hasPrefix("/") ? URL(filePath: core.docs) : root.appending(path: core.docs)
        // She reads them in place: a docs folder without its index is a boot error.
        guard FileManager.default.fileExists(atPath: docsDir.appending(path: "README.md").path) else {
            throw TOMLError(description: "[core] docs: \(docsDir.path)/README.md does not exist")
        }

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
        // Memories written by a previous process exist only as markdown until indexed (spec 6).
        try memory.syncOnBoot()
        try await model.check()
    }

    /// Tells her this process just started (owner, 2026-11-14): a program line in the timeline, no
    /// turn of its own; she reads it with the next event.
    public func announceBoot() throws {
        try append(Event(.memory, .internal, Self.bootNotice + (try calendarBootText())))
    }
    static let bootNotice = "【開機】你剛被開機了。"

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
        signal(.ownerTalk, "「\(text.prefix(30))」")
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
                                drives: drivesConfig.enabled, ownerSleep: ownerSleepConfig.enabled)
            + (voiceEnabled ? "\n\n" + Instructions.voice(docs: docsPath) : "")
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
            case .calendar: plan.append((ev, .full))
            }
            if c.kind == .forced, seq == c.toSeq { plan.append((ev, .block(c))) }
        }
        // Older window reads go out as one line naming the window (2026-11-16, owner: she does not act
        // on a stale list, a list costs its tokens once), but in batches (2026-11-17, `fullReads`): the
        // server resumes only from the end of an earlier request, so a read put away the round after it
        // came made every round read everything after the item's first read again.
        let kept = Self.fullReads(plan.compactMap { if case .full = $0.1, $0.0.image != nil || $0.0.window != nil { $0.0.seq } else { nil } },
                                  batch: computerConfig.readBatch)
        var out = [ChatMessage(role: "system", content: systemPrompt())]
        for (ev, mode) in plan {
            switch mode {
            case .full:
                out += try render(ev, read: kept.contains(ev.seq!))
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
        let covered = timeline.compactions.filter { $0.kind != .talk && $0.fromSeq == start }.map(\.toSeq).max() ?? start
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
        let sub = todo.phase == .idle || todo.phase == .done ? nil : goalFile.link?.subgoal
        let rest = (todo.pending.isEmpty ? "" : "；後面還有：" + todo.pending.map { "「\($0)」" }.joined(separator: ""))
            + (sub.map { "（子目標 #\($0) 的步驟）" } ?? "")
        let item = "「\(todo.current ?? "")」"
        return switch todo.phase {
        case .idle, .done: "待命中（沒有 todo；要做事先用 todo.set 列）"
        case .doing: "進行中\(item)\(rest)"
        case .waiting: "等待中\(item)（有人說話才會再有回合）\(rest)"
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
        // One round of the item in progress (design C): this reply was written while it was 進行中.
        if todo.phase == .doing {
            todo.rounds += 1
            if todo.decideAt == 0 { todo.decideAt = computerConfig.decideAfterRounds }
        }

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
        // Her computer actions (click, type, key, scroll, menu) run in order and stop at the first
        // failure (2026-11-09): only the last one done carries the window's new read, the ones before
        // it answer with their line only, and the ones after a failure are not run. Actions written
        // after an ending call never run, so the one before it is the last. Every row she gives means
        // the read she saw before this reply (`Mac.beginReply`).
        await mac.beginReply(item: todo.current)
        let live = calls.prefix { !Tools.ending.contains($0.name) }
        let actionAt = live.indices.filter { Tools.actionNames.contains(live[$0].name) }
        var chain = Chain(), step = 0, failed: (step: Int, line: String)?, ended: String?
        for (i, call) in calls.enumerated() {
            speaksLater = Set(calls[(i + 1)...].compactMap { c in
                c.arguments == nil ? nil : c.name == Tools.Name.reply ? .developer : Tools.speaking[c.name] != nil ? .owner : nil
            })
            let name = call.name.isEmpty ? "tool_call" : call.name
            let isAction = Tools.actionNames.contains(call.name)
            var out: Outcome
            if let ended, Tools.speaking[call.name] == nil {
                out = Outcome(text: "失敗：這則回覆前面已經用 \(ended) 收起這一項了，後面的呼叫沒有執行；看了結果下一回合再做。",
                              actions: ["error \(name)"], background: true)
            } else if Tools.gated.contains(call.name), todo.phase != .doing {
                out = Outcome(text: gateText(name), actions: ["refused \(name)"])
            } else if Tools.gated.contains(call.name), todo.deciding {
                out = Outcome(text: "失敗：\(name) 沒有執行。" + decideText, actions: ["refused \(name)"])
            } else if isAction, let failed {
                out = Outcome(text: "失敗：前面第 \(failed.step) 步（\(failed.line)）失敗了，這一步沒做",
                              actions: ["skipped \(name)"], background: true)
            } else {
                if isAction { step += 1 }
                if Tools.gated.contains(call.name) { itemUsedComputer = true }
                if !call.name.hasPrefix("todo."), call.name != "think" { itemActed = true }
                chain.last = !actionAt.contains(i) || actionAt.last == i
                out = await execute(call, seen: events, reply: stored, chain: chain)
                if Tools.gated.contains(call.name) { out = noteRepeats(call, out) }
                if out.ends { ended = name }
                if isAction {
                    if let line = out.failedStep {
                        failed = (step, line)
                    } else if !chain.last {
                        chain.owed = true
                    }
                }
            }
            var resultEvent = Event(.tool, .internal, out.text, secret: out.secret, image: out.image, window: out.window)
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
        // The decision point (design C): the item has run `decideAt` rounds and is still in progress.
        if todo.phase == .doing, todo.current == before.current, !todo.deciding, todo.rounds >= todo.decideAt {
            todo.deciding = true
            try append(Event(.memory, .internal, "【該決定了】" + decideText))
            actions.append("decision point")
            log("INFO", "turn \(turnIndex): decision point for「\(todo.current ?? "")」at round \(todo.rounds)")
        }
        if todo != before {
            try timeline.recordTodo(todo, seq: stored.seq!)
            if todo.phase != before.phase || todo.current != before.current || todo.pending != before.pending {
                log("INFO", "turn \(turnIndex): todo \(before.phase.label) → \(todo.phase.label)「\(todo.current ?? "")」 pending \(todo.pending.count)")
            }
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
            let request = try Tools.decode(call, specs: tools)
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
            case .todoSet(let items, let subgoal):
                return try todoSet(items, subgoal: subgoal, reply: reply)
            case .todoDone(let result, let evidence):
                return try await todoDone(result, evidence: evidence, reply: reply)
            case .todoFail(let reason, let tried):
                return try todoFail(reason: reason, tried: tried)
            case .todoWait:
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
            case .todoContinue(let missing, let why):
                return try todoContinue(missing: missing, why: why)
            case .apps:
                return Outcome(text: try await mac.apps(), actions: [name])
            case .open(let app, let target):
                return computerOutcome(try await mac.open(app, target: target), name: name, chain: chain)
            case .readWindow(let app, let window):
                return computerOutcome(try await mac.readWindow(app, window: window), name: name, chain: chain)
            case .act(let action):
                return computerOutcome(try await mac.act(action), name: name, chain: chain)
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
            guard Tools.actionNames.contains(call.name) else { return Outcome(text: "失敗：\(error)", actions: ["error \(name)"]) }
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
    /// the 【相關記憶】 on a switch and the experience's entities; a success is a step of the item.
    func computerOutcome(_ r: MacResult, name: String, chain: Chain) -> Outcome {
        var out = Outcome(text: r.text, image: r.image, window: r.window, actions: [r.failed ? "error \(name)" : name])
        noteRead(r)
        if r.failed {
            out.failedStep = name
        } else {
            if let step = r.step { itemSteps.append((timeline.lastSeq + 1, step)) }
            if Tools.actionNames.contains(name), !chain.last {
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
        guard let window = r.window ?? (r.failed || r.step?.hasPrefix("open ") != true ? nil : "open\n" + r.text) else { return }
        latestRead = .window(window)
        ownerShot = r.image
    }

    /// Her result line when her words to the owner carried the screenshot (design B).
    static func shotSent(_ file: String) -> String { "（附上了你最後讀的視窗截圖給主人：\(file)）" }

    /// The decision point's words (design C): how many rounds, and the three choices.
    var decideText: String {
        "「\(todo.current ?? "")」已經做了 \(todo.rounds) 回合（做得成的事通常 7–14 回合）。電腦和檔案工具先停，選一個："
            + "① \(Tools.Name.todoContinue)(missing, why)：還缺什麼、為什麼值得繼續（主人看得到）；② \(Tools.Name.askOwner) 問主人；③ \(Tools.Name.todoFail) 說卡在哪裡、試過什麼。"
            + "選了繼續，再過 \(computerConfig.decideEveryRounds) 回合會再問一次。"
    }

    /// `todo.continue` (design C): her reason to go on, recorded with the list; the tools run again
    /// until the next point.
    func todoContinue(missing: String, why: String) throws -> Outcome {
        let item = try currentItem(Tools.Name.todoContinue)
        guard todo.deciding else {
            throw ToolError(description: "現在不用決定：「\(item)」做了 \(todo.rounds) 回合，第 \(todo.decideAt) 回合才要選。接著做。")
        }
        todo.goOn.append("第 \(todo.rounds) 回合：還缺「\(missing)」；值得繼續因為「\(why)」")
        decided()
        log("INFO", "turn \(turnIndex): todo.continue「\(item)」round \(todo.rounds): missing「\(missing)」why「\(why)」")
        return Outcome(text: "記下了，主人在後台看得到。電腦和檔案工具開放了；第 \(todo.decideAt) 回合會再問一次。", actions: ["todo continue"])
    }

    /// She chose at the decision point (go on, or asked the owner): the next point is `decide_every_rounds` on.
    func decided() {
        todo.deciding = false
        todo.decideAt = todo.rounds + computerConfig.decideEveryRounds
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
            shown = lines.prefix(6).enumerated().map { "  \($0.offset + 1)→" + String($0.element.prefix(80)) }.joined(separator: "\n")
        }
        guard let evidence else {
            return "還沒完成：這一項用過電腦或檔案工具，todo.done 要帶 evidence：從\(source)照抄證明做完的那段字。現在讀到的：\n\(shown)\n照這個判斷做完了沒有。"
        }
        // Several copied pieces joined by ；, ; or line breaks (m7-mac a-2, 2026-11-17: two real lines
        // of the read joined by 「；」 were refused) count when every piece is in the read.
        let read = Self.evidenceKey(haystack)
        let keys = evidence.split(whereSeparator: { "；;\n".contains($0) }).map { Self.evidenceKey(String($0)) }.filter { !$0.isEmpty }
        let key = Self.evidenceKey(evidence)
        if !key.isEmpty, read.contains(key) || (!keys.isEmpty && keys.allSatisfy(read.contains)) { return nil }
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
        latestRead = nil
        todo.rounds = 0
        todo.decideAt = computerConfig.decideAfterRounds
        todo.deciding = false
        todo.goOn = []
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
    func todoSet(_ items: [String], subgoal: Int? = nil, reply: Event) throws -> Outcome {
        func listed(_ xs: [String]) -> String { xs.isEmpty ? "沒有" : xs.map { "「\($0)」" }.joined() }
        let linkNote = try subgoal.map { try linkTodo(items, subgoal: $0) } ?? ""
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
                return Outcome(text: ended ? "清單結束了，現在待命中。" : "沒有項目，還是待命中。", actions: ["todo end"], background: true)
            }
            startLink(subgoal: subgoal, fresh: todo.phase != .failed)
            todo.phase = .doing
            todo.current = first
            todo.pending = Array(items.dropFirst())
            todo.startSeq = reply.seq! - 1
            beginItem()
            // Whose line was waiting before this reply: a 「好，我來」 she wrote ahead of todo.set in the
            // same reply is an acknowledgement and does not make the list her own.
            listFor = Self.unanswered(timeline.events.filter { ($0.seq ?? 0) <= reply.seq! })
            listReply = reply.seq!
            listReported = false
            return Outcome(text: "todo 列好了。現在進行中：「\(first)」；後面：\(listed(todo.pending))。" + linkNote, actions: ["todo set"])
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
            throw ToolError(description: "「\(item)」這一項你還沒做任何動作，也沒看過結果，不能說完成。先動手做；如果覺得已經是做完的樣子，至少用 read_window 或 read_file 親眼看過再說完成。")
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
        let step = itemFinished(item)
        let next = advance()
        let list = next == nil ? listFinished() : ""
        log("INFO", "turn \(turnIndex): todo 完成「\(item)」result「\(result)」, \(steps.count) actions")
        note += step + list
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
        scoreAttached(steps: steps, outcome: "failure")
        itemEndNow = TodoPhase.failed.label
        todo.phase = .failed
        log("INFO", "turn \(turnIndex): todo 失敗「\(item)」reason「\(reason)」")
        signal(.itemFailed, item)
        var concernNote = ""
        if drivesConfig.enabled {
            let c = try addConcern("沒做成：\(item)（\(reason)）", about: .duty, source: "todo", key: "todo:\(todo.startSeq)")
            concernNote = "這件事程式記成你的掛念 #\(c.id)；之後做成或決定放下，用 concern.end 結束它。"
        }
        let rest = todo.pending.isEmpty ? "（後面沒有別的項目了）" : "（剩下：" + todo.pending.map { "「\($0)」" }.joined() + "）"
        return Outcome(text: "記下了：「\(item)」沒做成。這一項的過程收起來了，之後只留你寫的原因、試過的和這份動作清單：\(Self.actionList(steps))。"
                       + "電腦工具先鎖著。現在決定剩下的\(rest)：用 todo.set 重寫剩下的項目；或照原樣列剩下的，接著做下一項；或跟主人說了以後用 todo.set 給空的陣列結束清單。" + concernNote,
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
    static let observingTools: Set<String> = ["open", "read_window", "click", "type", "key", "scroll", "menu", "read_file", "edit_file"]

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
                                          entities: [], deep: item.deep, now: now)
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

    /// Folders by `Memory.visible`: `secret` only if the turn saw secret events (5.1), `own` in the
    /// owner's turns and hers. Her memories and today's events (today's secret events only where the
    /// secret folder is visible) through the selection pipeline (`Selector.run`, `Rule.recall`):
    /// everything related to her query, the `[select] recall_limit` best by the fused ranking. No
    /// relevance gate beyond the search (she judges; 2026-11-15): a query nothing matches well still
    /// returns the nearest; 「0 條」 only when there is nothing to search.
    func recall(_ query: String, seen events: [Event]) async throws -> Outcome {
        let visible = Memory.visible(Self.audience(events), sawSecret: events.contains(where: \.secret))
        let trigger = Selector.Trigger(label: "recall「\(query)」", query: query, app: nil)
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
        // A secret hit makes the result a secret event (5.1: what the turn saw is what it may say).
        return Outcome(text: out, secret: hits.contains(where: \.secret), actions: ["recall \(hits.count)"])
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
            log("INFO", "turn \(turnIndex): 相關記憶（\(label)）query「\(query)」: task groups \(merged.sorted().joined(separator: " ")), "
                + "already attached \(already.count) \(already.sorted())")
            logSelection(report)
            let fresh = report.selected.compactMap { picks[$0.id] }
            guard !fresh.isEmpty else { return }
            attachedIDs.formUnion(fresh.map(\.id))
            let from = frontApp.map { "主人的話和前景 app「\($0.name)」" } ?? "主人的話"
            var text = "【相關記憶】程式用\(from)查到、最相近的過去經驗："
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
