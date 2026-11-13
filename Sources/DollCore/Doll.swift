import Foundation

/// The skeleton: one queue, one consumer, one timeline, one line (the owner's).
/// The actor is the single serial owner of Doll state; everything durable is in the timeline
/// files (`Timeline`) and the memory files, SQLite only holds their indexes.
///
/// A turn = one model call. The loop only takes a turn when the queue holds an event
/// (owner voice, owner text, a watch wake) or her tool results await her; otherwise it sleeps
/// and makes no model call.

/// One sentence for whoever spoke to her last (2026-11-12): delivers it, and returns nil, or says
/// why it could not be delivered (her `say` result then tells her so).
public typealias Say = @Sendable (String, Addressee) -> String?

/// What one turn did; for logs, tests and the smoke run.
public struct TurnReport: Sendable {
    public var index: Int
    public var actions: [String]       // "said" (a say call) | "<tool>" | "think <level>" | "remember <folder>" | "recall <n>" | "error <tool>"
    public var reply: String
    public var toolCalls: [ToolCall]
    public var footer: String
    public var effort: String
    public var usage: ModelUsage
    public var usedTool: Bool
    /// The 【相關記憶】 events the program appended this turn, verbatim (spec 3.1, 2026-11-09).
    public var attached: [String]
    /// How a scenario ended this turn: "success" / "failure" from her `outcome`; nil when none ended.
    public var outcome: String?
    /// The experience memory written this turn (on her success), verbatim.
    public var experience: String?
    /// Per experience attached during the scenario that ended this turn: how many of its steps she
    /// did in order, and what was counted (spec 3.1, 2026-11-09).
    public var follows: [String] = []
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
    let say: Say
    /// `[voice] enabled` (M6): the voice tools and the voice part of the system prompt.
    let voiceEnabled: Bool
    /// The voice channel (`setVoice`); nil while voice is disabled or not wired yet.
    var voice: (any VoicePort)?
    /// A `say` in the voice channel was cut by the owner this turn: her later `say` calls in the
    /// same reply are not spoken (she would talk over him).
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
    /// Her internal tools, built once; the system prompt lists them, so it never changes between turns.
    let tools: [Tools.Spec]
    /// Each scenario kind's tools, `outcome` last; listed only in its enter call's result.
    let scenarioTools: [(ScenarioKind, [Tools.Spec])]
    /// The open scenario; nil at the top level. One at a time.
    var scenario: Scenario?
    /// The scenario her `outcome` closed this turn; stored once the turn's results are in.
    var closing: ClosedScenario?
    /// This turn's scenario end and experience memory, for its report.
    var outcomeNow: String?
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
                say: @escaping Say) throws {
        core = config.core
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
        self.say = say
        tools = Tools.specs(memory: memoryConfig, voice: config.voice.enabled)
        voiceEnabled = config.voice.enabled
        scenarioTools = ScenarioKind.allCases.map { ($0, $0.tools(config.computer, config.files) + [Tools.exitSpec]) }

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
    static let examplesHeader = "說話的樣子（範例，只示範語氣和長短，不是固定台詞，也不是真的發生過的對話；「我：」後面那句，真的回話時一樣要用 say 說出來，不然沒人聽得到）："

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

    /// Who her `say` goes to (2026-11-12): whoever spoke to her last in `events`; the owner when
    /// nobody has.
    public static func addressee(_ events: [Event]) -> Addressee {
        events.last { $0.speaker != nil }?.speaker ?? .owner
    }

    /// While a turn runs: who it would speak to (the local API shows that person "typing"); nil
    /// while she sleeps.
    public func speakingTo() -> Addressee? {
        inTurn ? Self.addressee(timeline.events) : nil
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
        for ev in batch {
            try append(ev)
        }
        if batch.contains(where: { $0.speaker != nil || $0.kind == .watch }) { taskTurn = true }
        if batch.contains(where: { $0.speaker != nil }) { ownerArrived = true }
    }

    /// A scenario still open when the program stops (turn cap, stop) never got her outcome: it ends
    /// unknown, and nothing is stored for it, so later requests keep its turns in full (there is no
    /// summary of hers to put in their place, and a program-written one would put words in her mouth).
    func shutdown() {
        if let s = scenario { leave(s, outcome: "unknown") }
        stopWatch()
    }

    // prompt -----------------------------------------------------------------
    /// Fixed for the life of the process: the template's tool block, then the persona. This is the
    /// same text the template would render from `tools` plus a system message, so the prompt opens
    /// the way she was trained on.
    func systemPrompt() -> String {
        Tools.block(tools) + "\n\n你是\(name)。\n\n\(identity)\n\n" + Instructions.text + (voiceEnabled ? "\n\n" + Instructions.voice : "")
    }

    /// The request before the footer: the system prompt, then the timeline. Her replies carry the
    /// footer they were answering and her raw content (tool call text included), so the prompt
    /// prefix is byte-stable turn to turn (the server caches from the last assistant turn).
    ///
    /// A scenario she left with `outcome` (spec 3.1, 2026-11-09): everything up to and including her
    /// reply that entered it renders as before (with the results of calls she wrote ahead of the
    /// enter call); the enter call's result becomes `Scenario.result` (how it ended and her summary);
    /// owner lines that came in meanwhile stay; her `say` calls in the reply that left it follow as
    /// her message (with their empty results). Every other event of the span (results, screenshots, crops, watch wakes, the
    /// program's 【相關記憶】 and 【快捷鍵】, her replies in between, the exit reply's results) stays in
    /// the timeline but is not sent. The open scenario renders in full.
    func context(_ events: [Event]) throws -> [ChatMessage] {
        let closed = timeline.closedScenarios
        enum Mode { case full, enterResult(ClosedScenario), exitSpeech }
        var plan: [(Event, Mode)] = []
        var i = 0
        for ev in events {
            let seq = ev.seq!
            while i < closed.count, closed[i].lastSeq < seq { i += 1 }
            guard i < closed.count, seq > closed[i].startSeq else { plan.append((ev, .full)); continue }
            let c = closed[i]
            if seq < c.enterResultSeq || ev.speaker != nil { plan.append((ev, .full)) }
            else if seq == c.enterResultSeq { plan.append((ev, .enterResult(c))) }
            else if seq == c.endSeq { plan.append((ev, .exitSpeech)) }
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
            case .enterResult(let c):
                out.append(ChatMessage(role: "tool", content: Scenario.result(success: c.success, summary: c.summary)))
            case .exitSpeech:
                // What she said to the owner in the reply that left: her say calls and their (empty) results.
                let says = ParsedReply(ev.text).calls.filter { $0.name == "say" && $0.malformed == nil }
                if !says.isEmpty {
                    out.append(ChatMessage(role: "assistant", content: says.map(\.text).joined(separator: "\n")))
                    out += says.map { _ in ChatMessage(role: "tool", content: "") }
                }
            }
        }
        return out
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
                + [ChatMessage(role: "assistant", content: ev.text)]
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

    /// The facts footer: which menu she is in, her watch, context length, effort for the next call.
    public func footer(contextTokens: Int) -> String {
        let now = Date().timeIntervalSince1970
        var out = ["【狀況】", "- 現在 \(clock(now))"]
        out.append(scenario.map { "- 選單：\($0.kind.menuName)（做完呼叫 outcome(success, summary) 結束）" }
                   ?? "- 選單：頂層（" + ScenarioKind.allCases.map { "\($0.menuName)工具鎖著；\($0.enterTool)(expect) 打開" }
                       .joined(separator: "；") + "）")
        out.append(watch.map { "- watch：盯著 \($0.target)，最多等 \(seconds($0.maxWait))" } ?? "- watch：沒有")
        out.append("- context 約 \(contextTokens) token；下一回合 effort：\(nextEffort)")
        return out.joined(separator: "\n")
    }

    /// `status` in the CLI.
    public func status() throws -> String {
        footer(contextTokens: Int(Double(try estimateContext()) * tokenRatio))
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
        outcomeNow = nil
        experienceNow = nil
        followsNow = []
        selectionsNow = []
        voiceCut = false
        // A new owner or developer line starts a task: what was attached before may be attached again.
        if ownerArrived {
            ownerArrived = false
            attachedIDs = []
            cheatsheetSent = []
            await attachMemories(trigger: "\(Self.addressee(timeline.events).label)的新訊息")
        }
        let events = timeline.events
        var messages = try context(events)
        let guess = estimate(messages)
        let footerText = footer(contextTokens: Int(Double(guess) * tokenRatio))
        messages.append(ChatMessage(role: "user", content: footerText))
        let effort = nextEffort

        let task = Task { try await model.chat(messages, effort: effort) }
        inflight = task
        inflightEffort = effort
        feed.notify(.turn)
        let reply: ModelReply
        do {
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

        // Calls the model wrote inside its thinking (no `</think>`) are her reply (2026-11-11).
        let rescued = ParsedReply.rescued(content: reply.content, reasoning: reply.reasoning)
        if rescued != nil { log("INFO", "turn \(turnIndex): her calls were inside the thinking (no </think>); taken as her reply") }
        let content = rescued ?? reply.content
        // She saw every event, so a secret seen marks her reply (5.1). Stored raw: re-sent byte-identical.
        let stored = try append(Event(.doll, .internal, content, secret: events.contains(where: \.secret)))
        try timeline.attachFooter(stored.seq!, footerText)

        // Text outside her calls is her own thought (2026-11-10): nobody hears it; she speaks with `say`.
        let parsed = ParsedReply(content)
        // Every call, in the order she wrote it, gets exactly one tool result right after her reply
        // (spec 6, 2026-11-08); a malformed, unknown or locked one gets a result saying what was wrong.
        // Her `outcome` ends the scenario: the calls after it are refused, except `say` (2026-11-11: she
        // wrote it after the outcome and the owner heard nothing; speaking touches no computer), and
        // nothing wakes her for it.
        // Her computer actions (click, type, key, scroll, drag) run in order and stop at the first
        // failure (2026-11-09): only the last one done carries the observation, the ones before it
        // answer with their line only, and the ones after a failure are not run. Actions written
        // after an `outcome` never run, so the one before it is the last.
        let live = parsed.calls.prefix { $0.name != "outcome" }
        let actionAt = live.indices.filter { Tools.actionNames.contains(live[$0].name) }
        var chain = Chain(), step = 0, failed: (step: Int, line: String)?
        for (i, call) in parsed.calls.enumerated() {
            let name = call.name.isEmpty ? "tool_call" : call.name
            let isAction = Tools.actionNames.contains(call.name)
            let out: Outcome
            if outcomeNow != nil, call.name != "say" {
                out = Outcome(text: "失敗：這次用電腦已經用 outcome 結束了；outcome 後面的呼叫沒有執行。",
                              actions: ["error \(name)"], background: true)
            } else if isAction, let failed {
                out = Outcome(text: "失敗：前面第 \(failed.step) 步（\(failed.line)）失敗了，這一步沒做",
                              actions: ["skipped \(name)"], background: true)
            } else {
                if isAction { step += 1 }
                chain.last = !actionAt.contains(i) || actionAt.last == i
                out = await execute(call, seen: events, reply: stored, chain: chain)
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
            resultEvent.to = out.to
            let result = try append(resultEvent)
            if out.enters { scenario?.enterResultSeq = result.seq }
            actions += out.actions
            if !out.background {
                followUp = true
                taskTurn = true
            }
        }
        if outcomeNow != nil {
            followUp = false
            taskTurn = false
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
        // The scenario she left this turn: from now on its span renders as her summary.
        if var c = closing {
            closing = nil
            c.lastSeq = timeline.lastSeq
            try timeline.closeScenario(c)
        }

        // A reply without a call (2026-11-11): every reply calls at least one tool. With a menu open she
        // is reminded and woken again (she had only thought, or wrote nothing, and the task stopped);
        // the `max_turns_without_tool`-th one in a row writes the log line instead and lets her rest.
        let usedTool = !parsed.calls.isEmpty
        if usedTool {
            idleTurns = 0
        } else {
            idleTurns += 1
            if idleTurns >= idle.maxTurnsWithoutTool {
                log("INFO", "idle: \(idleTurns) turns in a row without a tool call")
                idleTurns = 0
            } else if scenario != nil {
                try append(Event(.memory, .internal, Self.noCallReminder))
                followUp = true
                actions.append("reminded")
            }
        }

        let report = TurnReport(index: turnIndex, actions: actions, reply: content, toolCalls: parsed.calls,
                                footer: footerText, effort: effort, usage: reply.usage, usedTool: usedTool,
                                attached: attachedNow, outcome: outcomeNow, experience: experienceNow, follows: followsNow,
                                cheatsheets: cheatsheetNow, selections: selectionsNow)
        log("INFO", "turn \(turnIndex) effort=\(effort) actions=\(actions) prompt=\(reply.usage.promptTokens) "
            + "prefilled=\(reply.usage.prefilled.map(String.init) ?? "?") cached=\(reply.usage.cached.map(String.init) ?? "?")")
        observer?(report)
        try timeline.recordTurn(Timeline.TurnStats(seq: stored.seq!, started: started,
                                                   seconds: Date().timeIntervalSince1970 - started,
                                                   prompt: reply.usage.promptTokens, cached: reply.usage.cached, effort: effort))
    }

    /// The program's line after a reply without a call while a menu is open (fixed bytes).
    static let noCallReminder = "【提醒】你上一則回覆沒有呼叫工具，所以什麼都沒發生。每一則回覆都至少要呼叫一個工具：要跟主人說話用 say，要做事就呼叫那個工具。"

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
        /// This call entered a scenario: its result is the one her summary replaces once she leaves.
        var enters = false
        /// This computer action failed (before or in the VM): its line, for the refusals after it.
        var failedStep: String?
        /// A `say` call: who it went to (2026-11-12).
        var to: Addressee?
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
            let request = try Tools.decode(call, specs: tools + (scenario?.tools ?? []),
                                           locked: scenarioTools.filter { $0.0 != scenario?.kind })
            switch request {
            case .say(let text):
                // Spoken to whoever spoke last; nothing new for her to read, and nothing to wake her
                // for. Not delivered (the bridge is down, no dollctl chat is open): her result says why.
                let to = Self.addressee(timeline.events)
                // In the voice channel (M6), the owner hears it: sentence by sentence through the TTS.
                if to == .owner, let voice, await voice.channel() != nil {
                    guard !voiceCut else {
                        return Outcome(text: "沒有念：前面一句被主人打斷了，這句沒說出口。", actions: ["error say"], background: true, to: to)
                    }
                    let report = try await voice.speak(text)
                    if report.interrupted, report.heard.cut != nil { voiceCut = true }
                    log("INFO", "turn \(turnIndex): said in voice「\(report.heardText)」" + (report.interrupted ? "（被打斷）" : ""))
                    return Outcome(text: report.resultText, actions: [report.interrupted ? "said voice (cut)" : "said voice"],
                                   background: true, to: to)
                }
                if let why = say(text, to) {
                    log("ERROR", "turn \(turnIndex): say to \(to.rawValue) not delivered: \(why)")
                    return Outcome(text: "失敗：\(to.label)沒收到：\(why)", actions: ["error say"], background: true, to: to)
                }
                return Outcome(text: "", actions: ["said"], background: true, to: to)
            case .sendText(let text):
                if let why = say(text, .owner) {
                    log("ERROR", "turn \(turnIndex): send_text not delivered: \(why)")
                    throw ToolError(description: "主人沒收到：\(why)")
                }
                return Outcome(text: "", actions: ["sent text"], background: true, to: .owner)
            case .joinVoice:
                guard let voice else { throw ToolError(description: "語音還沒接上") }
                let ch = try await voice.join()
                return Outcome(text: "進了語音頻道「\(ch)」。你只聽得到主人；對主人 say 的話會念出來。", actions: ["join_voice"], background: true)
            case .leaveVoice:
                guard let voice else { throw ToolError(description: "語音還沒接上") }
                try await voice.leave()
                return Outcome(text: "離開語音頻道了。對主人 say 的話回到 Discord 私訊的文字。", actions: ["leave_voice"], background: true)
            case .think(let level):
                effortNext = level
                return Outcome(text: "下一回合 effort：\(level)", actions: ["think \(level)"])
            case .remember(let items):
                return await remember(items, seen: events, reply: reply)
            case .recall(let query):
                return try await recall(query, seen: events)
            case .enter(let kind, let expect):
                if let open = scenario {
                    throw ToolError(description: "\(open.kind.menuName)選單還開著，不能再打開；先用 outcome(success, summary) 結束這次")
                }
                scenario = Scenario(kind: kind, startSeq: reply.seq!, tools: scenarioTools.first { $0.0 == kind }!.1,
                                    expect: expect, task: events.last { $0.speaker != nil }?.text ?? "")
                var out = await entered(kind)
                out.enters = true
                return out
            case .outcome(let success, let summary):
                guard let s = scenario, let enterResult = s.enterResultSeq else {
                    throw ToolError(description: "outcome 只能在打開的選單裡呼叫")
                }
                leave(s, outcome: success ? "success" : "failure")
                closing = ClosedScenario(kind: s.kind, startSeq: s.startSeq, enterResultSeq: enterResult, endSeq: reply.seq!,
                                         lastSeq: reply.seq!, success: success, summary: summary)
                log("INFO", "turn \(turnIndex): outcome success=\(success) summary「\(summary)」")
                guard success else {
                    return Outcome(text: "記下了：沒達成。離開\(s.kind.menuName)選單了；\(s.kind.menuName)工具鎖回去了。",
                                   actions: ["outcome failure"])
                }
                let text = Self.experienceText(task: s.task, summary: summary, steps: s.steps,
                                               maxChars: memoryConfig.experienceMaxChars)
                // Its run: from the last owner or developer line to this reply, her turns counted with this one.
                let owner = events.last { $0.speaker != nil }
                let turns = events.filter { $0.kind == .doll && $0.seq! > (owner?.seq ?? 0) }.count + 1
                let stats = ExperienceStats(turns: turns, seconds: Int((reply.ts - (owner?.ts ?? reply.ts)).rounded()))
                // Its entities: the apps it went through (what the program knows of it, 2026-11-11),
                // aligned to the registry by their bundle ids.
                var entities: [MemoryEntity] = []
                for app in s.apps {
                    entities.append(MemoryEntity(try await pipeline.programApp(app, context: text).entry.id, app.name))
                }
                let id = try memory.write(text, folder: .public, sourceSeq: reply.seq!, tier: .selfReported, kind: nil,
                                          experience: (task: s.task, stats: stats), entities: entities)
                experienceNow = text
                log("INFO", "turn \(turnIndex): experience \(id) written, \(s.steps.count) steps, \(text.count) chars, "
                    + "\(stats.turns) turns, \(stats.seconds) s")
                return Outcome(text: "記下了：達成。你的經過和這次的步驟存成一條經驗。離開\(s.kind.menuName)選單了；\(s.kind.menuName)工具鎖回去了。",
                               actions: ["outcome success"])
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
                return Outcome(text: "開始盯著\(w.target)，最多等 \(seconds(w.maxWait))；有變化或時間到會叫你。"
                               + (replaced ? "（取代了上一個 watch）" : ""),
                               actions: [name], background: true)
            // The file tools (2026-11-09): text only, no screenshot and no window line, the screen did
            // not change. Only a success is a step, and the step never holds the content.
            case .readFile(let path, let from, let to):
                let text = try FileText.decode(try await computer.readFile(path), path: path)
                let out = try FileText.read(text, path: path, from: from, to: to, max: filesConfig.maxReadLines)
                scenario?.steps.append("read_file \(path)")
                return Outcome(text: out, actions: [name])
            case .writeFile(let path, let text):
                try await computer.writeFile(path, Data(text.utf8))
                scenario?.steps.append("write_file \(path)（\(FileText.lines(text).count) 行）")
                return Outcome(text: FileText.written(text, path: path), actions: [name])
            case .editFile(let path, let old, let new):
                let text = try FileText.decode(try await computer.readFile(path), path: path)
                let (changed, out) = try FileText.edit(text, path: path, old: old, new: new)
                try await computer.writeFile(path, Data(changed.utf8))
                scenario?.steps.append("edit_file \(path)")
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
                if scenario != nil, !scenario!.apps.contains(where: { $0.bundleID == app.bundleID }) { scenario!.apps.append(app) }
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
            scenario?.steps.append(Self.step(action))
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

    // scenarios ----------------------------------------------------------------
    /// The kind's own work on entering; its result text is what she sees first. The computer: its
    /// menu, then a fresh screenshot. The front app at entry counts as newly seen, whatever it was
    /// before: a previous scenario's 【快捷鍵】 and 【相關記憶】 left the context with it (2026-11-09,
    /// chained E→F: F got no TextEdit sheet because TextEdit was already in front).
    func entered(_ kind: ScenarioKind) async -> Outcome {
        switch kind {
        case .computer:
            frontApp = nil
            frontTitle = nil
            frontFocus = nil
            cheatsheetSent = []
            return await withScreenshot(Tools.menu(scenario!.tools), actions: [kind.enterTool])
        }
    }

    /// Ends the open scenario as `outcome` (success / failure / unknown) and relocks its tools. The
    /// computer: experiences attached since the owner's last line are scored against her steps, and
    /// the watch stops.
    func leave(_ s: Scenario, outcome: String) {
        switch s.kind {
        case .computer:
            scoreAttached(steps: s.steps, outcome: outcome)
            stopWatch()
        }
        if outcome == "unknown" { log("INFO", "turn \(turnIndex): outcome unknown: \(s.kind.rawValue) scenario ended without her outcome") }
        scenario = nil
        outcomeNow = outcome
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
    static let observingTools: Set<String> = ["use_computer", "click", "type", "key", "scroll", "drag", "screenshot", "read_file", "edit_file"]

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
            case .doll: pending = ParsedReply(e.text).calls[...]
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
        if addressee(events) == .developer { return .developer }
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
