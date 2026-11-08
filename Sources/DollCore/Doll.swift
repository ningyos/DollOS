import Foundation

/// The skeleton: one queue, one consumer, one timeline, one line (the owner's).
/// The actor is the single serial owner of Doll state; everything durable is in `Store`.
///
/// A turn = one model call. The loop only takes a turn when the queue holds an event
/// (owner voice, owner text, a watch wake) or her tool results await her; otherwise it sleeps
/// and makes no model call.

/// One sentence for the owner.
public typealias Say = @Sendable (String) -> Void

/// What one turn did; for logs, tests and the smoke run.
public struct TurnReport: Sendable {
    public var index: Int
    public var actions: [String]       // "said" | "<tool>" | "think <level>" | "remember <folder>" | "recall <n>" | "error <tool>"
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
    let idle: IdleConfig
    let memoryConfig: MemoryConfig
    /// Screenshot files; events name them, and each is re-sent byte-identical every turn.
    public nonisolated let shots: URL
    let store: Store
    let memory: Memory
    let model: any MainModel
    let computer: any ComputerDriver
    let say: Say

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
    /// The computer menu; listed only in `use_computer()`'s result.
    let computerTools: [Tools.Spec]
    /// Between `use_computer()` and `leave_computer()`.
    var inComputer = false
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

    public init(config: Config, root: URL, model: any MainModel,
                computer: (any ComputerDriver)? = nil,
                say: @escaping Say) throws {
        core = config.core
        llm = config.llm
        computerConfig = config.computer
        idle = config.idle
        memoryConfig = config.memory
        let data = core.dataDir.hasPrefix("/") ? URL(filePath: core.dataDir) : root.appending(path: core.dataDir)
        shots = data.appending(path: "shots")
        try FileManager.default.createDirectory(at: shots, withIntermediateDirectories: true)
        store = try Store(path: data.appending(path: "doll.db"))
        // No fallback: a missing embedding model is an error, the same as an unreachable Strata (spec 3.1).
        let memRoot = memoryConfig.dir.hasPrefix("/") ? URL(filePath: memoryConfig.dir) : root.appending(path: memoryConfig.dir)
        memory = try Memory(config: memoryConfig, root: memRoot, store: store)
        self.model = model
        self.computer = computer ?? VMPalDriver(config.computer)
        self.say = say
        tools = Tools.specs(memory: memoryConfig)
        computerTools = Tools.computerSpecs(config.computer)

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
    /// Checks the model (unreachable = error, no fallback).
    public func boot() async throws {
        // Memories written by a previous process exist only as markdown until indexed (spec 6).
        try memory.syncOnBoot()
        try await model.check()
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
            try store.append(ev)
        }
        if batch.contains(where: { $0.kind == .owner || $0.kind == .watch }) { taskTurn = true }
    }

    func shutdown() {
        stopWatch()
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
    /// the template wraps them in `<tool_response>`. A computer result goes out as one message: its
    /// text, the full-screen screenshot, the crop's line, the front window's crop, the window's
    /// outline (2026-11-09), images from the stored files' bytes.
    func render(_ ev: Event) throws -> [ChatMessage] {
        let t = clock(ev.ts)
        var extra: [ChatMessage.Extra] = []
        if let f = ev.image { extra.append(.image(try imageURI(f))) }
        if let n = ev.cropNote { extra.append(.text(n)) }
        if let f = ev.crop { extra.append(.image(try imageURI(f))) }
        if let w = ev.window { extra.append(.text(w)) }
        switch ev.kind {
        case .doll:
            return (ev.footer.map { [ChatMessage(role: "user", content: $0)] } ?? [])
                + [ChatMessage(role: "assistant", content: ev.text)]
        case .owner: return [ChatMessage(role: "user", content: "[\(t) 主人] \(ev.text)")]
        case .tool: return [ChatMessage(role: "tool", content: ev.text, extra: extra)]
        case .watch: return [ChatMessage(role: "user", content: "[\(t) watch] \(ev.text)", extra: extra)]
        }
    }

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
        out.append(inComputer ? "- 選單：電腦（用完呼叫 leave_computer() 離開）"
                              : "- 選單：頂層（電腦工具鎖著；use_computer() 打開）")
        out.append(watch.map { "- watch：盯著 \($0.target)，最多等 \(seconds($0.maxWait))" } ?? "- watch：沒有")
        out.append("- context 約 \(contextTokens) token；下一回合 effort：\(nextEffort)")
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
        var messages = [ChatMessage(role: "system", content: systemPrompt())] + (try events.flatMap(render))
        let guess = estimate(messages)
        let footerText = footer(contextTokens: Int(Double(guess) * tokenRatio))
        messages.append(ChatMessage(role: "user", content: footerText))
        let effort = nextEffort

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
        taskTurn = false
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
        // (spec 6, 2026-11-08); a malformed, unknown or locked one gets a result saying what was wrong.
        for call in parsed.calls {
            let out = await execute(call, seen: events, reply: stored)
            try store.append(Event(.tool, .internal, out.text, secret: out.secret, image: out.image,
                                   cropNote: out.cropNote, crop: out.crop, window: out.window))
            actions += out.actions
            if !out.background {
                followUp = true
                taskTurn = true
            }
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
    }

    /// Runs one tool call. Every failure becomes the call's result text; nothing here throws.
    func execute(_ call: ToolCall, seen events: [Event], reply: Event) async -> Outcome {
        let name = call.name.isEmpty ? "tool_call" : call.name
        do {
            let request = try Tools.decode(call, specs: inComputer ? tools + computerTools : tools,
                                           locked: inComputer ? [] : computerTools)
            switch request {
            case .think(let level):
                effortNext = level
                return Outcome(text: "下一回合 effort：\(level)", actions: ["think \(level)"])
            case .remember(let items):
                return remember(items, seen: events, reply: reply)
            case .recall(let query):
                return try recall(query, seen: events)
            case .useComputer:
                guard !inComputer else { return Outcome(text: "已經在電腦選單裡了。", actions: [name]) }
                inComputer = true
                return await withScreenshot(Tools.menu(computerTools), actions: [name])
            case .leaveComputer:
                inComputer = false
                let had = watch != nil
                stopWatch()
                return Outcome(text: "離開電腦選單了；電腦工具鎖回去了" + (had ? "，watch 也停了。" : "。"), actions: [name])
            case .screenshot:
                watchIdle = 0
                return await withScreenshot("現在的畫面：", actions: [name])
            case .act(let action):
                return await perform(action, said: Self.describe(action), name: name)
            case .clickAt(let x, let y, let onWindow, let button, let count):
                let p = try screenPoint(x, y, onWindow: onWindow)
                let action = ComputerAction.click(x: p.x, y: p.y, button: button, count: count)
                let line = onWindow ? "click 視窗圖 \(x),\(y) → 全螢幕 \(p.x),\(p.y)" + Self.extra(button, count) : Self.describe(action)
                return await perform(action, said: line, name: name)
            case .watch(let region, let maxWait):
                let replaced = watch != nil
                let w = startWatch(region, maxWait: maxWait ?? computerConfig.watchDefaultMaxWaitS)
                return Outcome(text: "開始盯著\(w.target)，最多等 \(seconds(w.maxWait))；有變化或時間到會叫你。"
                               + (replaced ? "（取代了上一個 watch）" : ""),
                               actions: [name], background: true)
            }
        } catch {
            log("ERROR", "turn \(turnIndex): \(name) failed: \(error)")
            return Outcome(text: "失敗：\(error)", actions: ["error \(name)"])
        }
    }

    /// `text` plus a fresh full-screen screenshot, the front window's crop with where it sits on the
    /// full screen and its size, and the window's outline (spec 3.1, 2026-11-09). No front window: no
    /// crop and no outline, the result says why, and the menu bar's and Dock's items with their
    /// centers follow in the outline's place. A failed screenshot, crop, outline or menu bar/Dock
    /// summary is said in the result (and is an error); none is dropped silently.
    func withScreenshot(_ text: String, actions: [String]) async -> Outcome {
        let computer = computer
        async let front: Result<FrontWindow, Error> = {
            do { return .success(try await computer.frontWindow()) } catch { return .failure(error) }
        }()
        var out = Outcome(text: text, actions: actions)
        cropMap = nil
        do { out.image = try await capture(nil) } catch {
            log("ERROR", "turn \(turnIndex): screenshot failed: \(error)")
            out.text += "\n截圖失敗：\(error)"
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
        return out
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

    /// Runs one action and returns `said` and the VM's text, or the failure, with fresh screenshots.
    /// The action's own time, screenshots excluded, goes to the log (M2 round 6: element-click latency).
    func perform(_ action: ComputerAction, said: String, name: String) async -> Outcome {
        watchIdle = 0
        let start = ContinuousClock.now
        let result: Result<String, Error>
        do { result = .success(try await computer.act(action)) } catch { result = .failure(error) }
        let took = start.duration(to: .now)
        log("INFO", "turn \(turnIndex): \(Self.describe(action)) took \(String(format: "%.2f", Double(took.components.seconds) + Double(took.components.attoseconds) / 1e18)) s")
        switch result {
        case .success(let answer):
            return await withScreenshot("\(said)\n\(answer)", actions: [name])
        case .failure(let error):
            // VMPal's or agent-desktop's own words, verbatim; the screenshot shows where things stand.
            log("ERROR", "turn \(turnIndex): \(name) failed: \(error)")
            return await withScreenshot("失敗：\(said)\n\(error)", actions: ["error \(name)"])
        }
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

    // memory -----------------------------------------------------------------
    /// Tools whose results are what the computer showed: a memory taken from one is tool-tier.
    static let observingTools: Set<String> = ["use_computer", "click", "type", "key", "scroll", "drag", "screenshot"]

    /// The program fills seq, ts, confidence; the kind and folder are hers (spec 6, 2026-11-08). The
    /// tier comes from the event she is answering, not from anything she can claim; results of her
    /// own memory and think calls are not sources.
    func remember(_ items: [Tools.Request.Item], seen events: [Event], reply: Event) -> Outcome {
        let names = Self.callNames(events)
        let source = events.last { e in
            e.kind == .owner || e.kind == .watch || (e.kind == .tool && Self.observingTools.contains(names[e.seq!] ?? ""))
        }
        let tier: SourceTier = switch source?.kind {
        case .owner?: .owner
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
                let id = try memory.write(item.text, folder: item.folder, sourceSeq: seq, tier: tier, kind: item.kind)
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
            case .owner, .watch: break
            }
        }
        return out
    }

    /// Whose turn this is: answering the owner, or her own work (woken by her watch). There is no
    /// stranger input yet (M6), so this timeline never yields `.stranger`.
    static func audience(_ events: [Event]) -> Audience {
        switch events.last(where: { $0.kind == .owner || $0.kind == .watch })?.kind {
        case .owner?: .owner
        default: .doll
        }
    }

    /// Folders by `Memory.visible`: `secret` only if the turn saw secret events (5.1), `own` in the
    /// owner's turns and hers.
    func recall(_ query: String, seen events: [Event]) throws -> Outcome {
        let visible = Memory.visible(Self.audience(events), sawSecret: events.contains(where: \.secret))
        let hits = try memory.search(query, visible: visible)
        var out = "\(query)：\(hits.count) 條"
        for hit in hits {
            out += "\n\n〔\(hit.label)｜\(hit.folder)〕\n\(hit.text)"
        }
        // A hit from secret/ makes the result a secret event (5.1: what the turn saw is what it may say).
        return Outcome(text: out, secret: hits.contains(where: \.secret), actions: ["recall \(hits.count)"])
    }
}
