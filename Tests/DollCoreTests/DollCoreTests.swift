import CryptoKit
import Foundation
import ImageIO
import Synchronization
import Testing
@testable import DollCore

private let root = URL(filePath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()

/// One scripted reply: what she says plus her tool calls (name, parameters), rendered as raw
/// `<tool_call>` text in the template's format, the way Strata returns it with no `tools` sent. A
/// string literal is a reply used verbatim (raw call text included, if any).
private struct Scripted: ExpressibleByStringLiteral {
    var content: String
    var calls: [(String, [(String, String)])]
    init(_ content: String = "", _ calls: (String, [(String, String)])...) { self.content = content; self.calls = calls }
    init(stringLiteral value: String) { self.init(value) }

    var raw: String {
        let blocks = calls.map { name, params in
            "<tool_call>\n<function=\(name)>\n"
                + params.map { "<parameter=\($0.0)>\n\($0.1)\n</parameter>\n" }.joined()
                + "</function>\n</tool_call>"
        }.joined(separator: "\n")
        if blocks.isEmpty { return content }
        return content.isEmpty ? blocks : content + "\n\n" + blocks
    }
}

/// `key("cmd+s")` → key(key: "s", modifiers: ["cmd"]); a lone key has no modifiers.
private func key(_ chord: String) -> (String, [(String, String)]) {
    let parts = chord.split(separator: "+").map(String.init)
    let mods = parts.dropLast().map { "\"\($0)\"" }.joined(separator: ", ")
    return ("key", [("key", parts.last!)] + (mods.isEmpty ? [] : [("modifiers", "[\(mods)]")]))
}
private func watch(_ target: String, _ maxWait: Int? = nil) -> (String, [(String, String)]) {
    ("watch", [("target", target)] + (maxWait.map { [("max_wait_s", "\($0)")] } ?? []))
}
private let useComputer: (String, [(String, String)]) = ("use_computer", [("expect", "畫面上看得到結果")])
private func outcome(_ success: Bool, _ summary: String) -> (String, [(String, String)]) {
    ("outcome", [("success", "\(success)"), ("summary", summary)])
}
private let screenshotCall: (String, [(String, String)]) = ("screenshot", [])
private func say(_ text: String) -> (String, [(String, String)]) { ("say", [("text", text)]) }
private func think(_ level: String) -> (String, [(String, String)]) { ("think", [("level", level)]) }
private func recall(_ q: String) -> (String, [(String, String)]) { ("recall", [("query", q)]) }
private func remember(_ items: [(String, Bool)]) -> (String, [(String, String)]) {
    remember(kinds: items.map { ($0.0, $0.1, nil) })
}
private func remember(kinds items: [(String, Bool, String?)]) -> (String, [(String, String)]) {
    remember(folders: items.map { ($0.0, $0.1 ? "secret" : "public", $0.2) })
}
private func remember(folders items: [(String, String, String?)]) -> (String, [(String, String)]) {
    struct Item: Encodable { let text: String; let folder: String; let kind: String? }
    let json = try! JSONEncoder().encode(items.map { Item(text: $0.0, folder: $0.1, kind: $0.2) })
    return ("remember", [("items", String(decoding: json, as: UTF8.self))])
}

/// Returns scripted replies as raw content and records what each request saw.
private actor FakeModel: MainModel {
    var replies: [Scripted]
    var requests: [(messages: [ChatMessage], effort: String)] = []
    /// When each request reached the model.
    var starts: [ContinuousClock.Instant] = []

    init(_ replies: [Scripted]) { self.replies = replies }

    func check() async throws {}

    /// When set, the next request hangs until the call is cancelled (a long model call).
    var stallNext = false

    func chat(_ messages: [ChatMessage], effort: String) async throws -> ModelReply {
        requests.append((messages, effort))
        starts.append(.now)
        if stallNext {
            stallNext = false
            try await Task.sleep(for: .seconds(30))
        }
        guard !replies.isEmpty else { throw ModelError("script ran out at request \(requests.count)") }
        let r = replies.removeFirst()
        let prompt = messages.reduce(0) { $0 + estimateTokens($1.content) + 4 }
        return ModelReply(content: r.raw, usage: ModelUsage(promptTokens: prompt, completionTokens: 10))
    }

    func stall() { stallNext = true }

    func add(_ more: [Scripted]) { replies += more }
    func seen(_ i: Int) -> [ChatMessage] { requests[i].messages }
}

private final class Recorder: Sendable {
    let said = Mutex<[String]>([])
    let reports = Mutex<[TurnReport]>([])
    let logs = Mutex<[String]>([])
}

/// The VM: records actions; a screenshot is "frame <n> <region or full>", the window crop "frame <n>
/// crop <region>", so bytes change only when the test moves the frame (or `changing` moves it on
/// every capture). With `screenSize` / `cropSize` set, a PNG header of that size comes first. The
/// window outline names the frame too, or fails with `windowFailure`; the window sits at `place`.
private final class FakeComputer: ComputerDriver, Sendable {
    struct State {
        var actions: [ComputerAction] = []
        var failNext: String?
        /// Actions that still pass before `failNext` fires.
        var failAfter = 0
        var frame = 0
        var changing = false
        var captures = 0
        var windowFailure: String?
        var desktopFailure: String?
        var place = FrontWindow.Place.at(Region(10, 20, 300, 200))
        var screenSize: (Int, Int)?
        var cropSize: (Int, Int)?
        var app: AppIdentity?
        var shortcuts: [MenuShortcut] = []
        var frontAppReads = 0
        var menuReads: [String] = []
        var windowsLine = fakeWindowsLine
        var stateFailure: String?
        var menuFailure: String?
        /// The VM's files by absolute path (`~` is /Users/doll); folders are implied.
        var files: [String: Data] = [:]
    }
    let state = Mutex(State())

    func act(_ action: ComputerAction) async throws -> String {
        try state.withLock { s in
            s.actions.append(action)
            if let f = s.failNext {
                if s.failAfter > 0 { s.failAfter -= 1 } else {
                    s.failNext = nil
                    throw ComputerError(f)
                }
            }
            return "Done: 1 action in 0.1 s."
        }
    }

    func screenshot(region: Region?) async throws -> Data {
        state.withLock { s in
            s.captures += 1
            if s.changing { s.frame += 1 }
            let head = region == nil ? s.screenSize.map { pngHeader($0.0, $0.1) } ?? Data() : Data()
            return head + Data("frame \(s.frame) \(region.map { "\($0)" } ?? "full")".utf8)
        }
    }

    func windowShot(_ region: Region) async throws -> Data {
        state.withLock { s in (s.cropSize.map { pngHeader($0.0, $0.1) } ?? Data()) + Data("frame \(s.frame) crop \(region)".utf8) }
    }

    func frontWindow() async throws -> FrontWindow {
        try state.withLock { s in
            if let f = s.windowFailure { throw ComputerError(f) }
            return FrontWindow(summary: "前景 app：Fake，視窗標題「frame \(s.frame)」\nwindow \"frame \(s.frame)\"\n  button \"OK\"",
                               place: s.place)
        }
    }

    func desktopSummary() async throws -> String {
        try state.withLock { s in
            if let f = s.desktopFailure { throw ComputerError(f) }
            return "Dock 和選單列（項目中心，全螢幕截圖座標）：\ndockitem \"Finder\" (67,765)\nmenuitem \"Clock\" (1205,13)"
        }
    }

    func frontApp() async throws -> AppIdentity? { state.withLock { $0.frontAppReads += 1; return $0.app } }

    func menuShortcuts(_ bundleID: String) async throws -> [MenuShortcut] {
        try state.withLock {
            $0.menuReads.append(bundleID)
            if let f = $0.menuFailure { throw ComputerError(f) }
            return $0.shortcuts
        }
    }
    func failMenus(_ text: String?) { state.withLock { $0.menuFailure = text } }
    func setApp(_ app: AppIdentity?, _ shortcuts: [MenuShortcut]) { state.withLock { $0.app = app; $0.shortcuts = shortcuts } }

    func frontState() async throws -> FrontState {
        try state.withLock { s in
            if let f = s.stateFailure { throw ComputerError(f) }
            return FrontState(line: s.windowsLine, app: s.app)
        }
    }
    func failState(_ text: String?) { state.withLock { $0.stateFailure = text } }

    func setFrame(_ n: Int) { state.withLock { $0.frame = n } }
    func setChanging(_ on: Bool) { state.withLock { $0.changing = on } }
    func failNext(_ text: String, after: Int = 0) { state.withLock { $0.failNext = text; $0.failAfter = after } }
    func failWindow(_ text: String?) { state.withLock { $0.windowFailure = text } }
    func failDesktop(_ text: String?) { state.withLock { $0.desktopFailure = text } }
    func setPlace(_ p: FrontWindow.Place) { state.withLock { $0.place = p } }
    func setSizes(screen: (Int, Int)?, crop: (Int, Int)?) { state.withLock { $0.screenSize = screen; $0.cropSize = crop } }

    static func expand(_ path: String) -> String {
        path == "~" ? "/Users/doll" : path.hasPrefix("~/") ? "/Users/doll" + path.dropFirst() : path.hasPrefix("/") ? path : "/Users/doll/" + path
    }
    func readFile(_ path: String) async throws -> Data {
        try state.withLock { s in
            if let f = s.failNext { s.failNext = nil; throw ComputerError(f) }
            guard let d = s.files[Self.expand(path)] else { throw ComputerError("沒有這個檔案：\(path)") }
            return d
        }
    }
    func writeFile(_ path: String, _ data: Data) async throws {
        try state.withLock { s in
            if let f = s.failNext { s.failNext = nil; throw ComputerError(f) }
            s.files[Self.expand(path)] = data
        }
    }
    func put(_ path: String, _ text: String) { state.withLock { $0.files[Self.expand(path)] = Data(text.utf8) } }
    func file(_ path: String) -> String? { state.withLock { $0.files[Self.expand(path)].map { String(decoding: $0, as: UTF8.self) } } }
    var actions: [ComputerAction] { state.withLock { $0.actions } }
}

/// The fake's open windows and focus line, the last line of every computer result.
private let fakeWindowsLine = "開著的視窗：Finder、Fake「frame」（前景）；焦點：button \"OK\""

/// The on-device model's stand-in: entities by exact text (none otherwise); a pick rule (default:
/// the first candidate whose line contains the name, case-insensitively); a same rule (default:
/// 10); a new entry described as 「測試：<name>」; a score rule (default: 10). Records every request.
private final class FakeSelector: SelectionModel, Sendable {
    typealias PickRule = @Sendable (Mention, String, [String]) -> Int?
    struct State {
        var entities: [String: [Mention]] = [:]
        var pick: PickRule?
        var same: (@Sendable (Mention, String) -> Int)?
        var score: (@Sendable (String, String) -> Int)?
        var picked: [(mention: Mention, context: String, candidates: [String])] = []
        var described: [Mention] = []
        var scored: [(situation: String, memory: String)] = []
        var unavailable: String?
    }
    let state = Mutex(State())

    func check() async throws {
        if let why = state.withLock({ $0.unavailable }) { throw SelectionError(why) }
    }
    func entities(in text: String) async throws -> [Mention] { state.withLock { $0.entities[text] ?? [] } }
    func pick(_ mention: Mention, context: String, candidates: [String]) async throws -> Int? {
        state.withLock { s in
            s.picked.append((mention, context, candidates))
            if let rule = s.pick { return rule(mention, context, candidates) }
            return candidates.firstIndex { $0.lowercased().contains(mention.name.lowercased()) }
        }
    }
    func same(_ mention: Mention, context: String, entry: String) async throws -> Int {
        state.withLock { $0.same?(mention, entry) ?? 10 }
    }
    func describe(_ mention: Mention, context: String) async throws -> String {
        state.withLock { s in
            s.described.append(mention)
            return context.contains("桌面") ? "桌面上的 \(mention.name)" : "測試：\(mention.name)"
        }
    }
    func score(situation: String, memory: String) async throws -> Int {
        state.withLock { s in
            s.scored.append((situation, memory))
            return s.score?(situation, memory) ?? 10
        }
    }
    func setEntities(_ text: String, _ list: [Mention]) { state.withLock { $0.entities[text] = list } }
    func setPick(_ f: PickRule?) { state.withLock { $0.pick = f } }
    func setSame(_ f: (@Sendable (Mention, String) -> Int)?) { state.withLock { $0.same = f } }
    func setScore(_ f: (@Sendable (String, String) -> Int)?) { state.withLock { $0.score = f } }
    var scored: [(situation: String, memory: String)] { state.withLock { $0.scored } }
    var picked: [(mention: Mention, context: String, candidates: [String])] { state.withLock { $0.picked } }
}

/// A PNG's signature and IHDR: enough for its pixel size.
private func pngHeader(_ width: Int, _ height: Int) -> Data {
    func be32(_ v: Int) -> [UInt8] { [24, 16, 8, 0].map { UInt8((v >> $0) & 0xFF) } }
    return Data([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0, 0, 0, 13] + Array("IHDR".utf8) + be32(width) + be32(height)
                + [8, 2, 0, 0, 0])
}

private struct Harness {
    let doll: Doll
    let model: FakeModel
    let vm: FakeComputer
    let selector: FakeSelector
    let rec: Recorder
    let dir: URL
    let store: Store      // a second connection to the memory index, for checks

    init(_ replies: [Scripted] = [], tune: (inout Config) -> Void = { _ in }) async throws {
        dir = FileManager.default.temporaryDirectory.appending(path: "dollcore-test-\(UUID().uuidString)")
        var cfg = try Config.load(root.appending(path: "dollcore.example.toml"))
        cfg.core.dataDir = dir.path
        cfg.computer.watchPollS = 0.02
        cfg.computer.watchMinIntervalS = 0
        cfg.memory.dir = dir.appending(path: "memory").path
        cfg.computer.cheatsheet = false   // the cheatsheet's own tests switch it on
        tune(&cfg)
        model = FakeModel(replies)
        let rec = Recorder()
        self.rec = rec
        let vm = FakeComputer()
        self.vm = vm
        let selector = FakeSelector()
        self.selector = selector
        doll = try Doll(config: cfg, root: root, model: model, selector: selector, computer: vm,
                        say: { text in rec.said.withLock { $0.append(text) } })
        await doll.setObserver { r in rec.reports.withLock { $0.append(r) } }
        await doll.setLogSink { line in rec.logs.withLock { $0.append(line) } }
        store = try Store(path: dir.appending(path: "doll.db"))
    }

    var said: [String] { rec.said.withLock { $0 } }
    var reports: [TurnReport] { rec.reports.withLock { $0 } }
    var logs: [String] { rec.logs.withLock { $0 } }
    var last: TurnReport { reports.last! }

    func turn(_ replies: Scripted...) async throws {
        await model.add(replies)
        try await doll.runOnce()
    }

    /// The timeline as a new process reads it from the file.
    func timeline() throws -> Timeline { try Timeline(dir: dir.appending(path: "timeline")) }
    func events() throws -> [Event] { try timeline().events }

    /// Her tool results, oldest first.
    func toolResults() throws -> [Event] { try events().filter { $0.kind == .tool } }

    /// Waits until the queue has drained into the timeline `count` events of `kind`, or gives up.
    func waitFor(_ kind: Event.Kind, count: Int = 1, seconds: Double = 5) async throws {
        let deadline = ContinuousClock.now + .seconds(seconds)
        while ContinuousClock.now < deadline {
            if await doll.queue.filter({ $0.kind == kind }).count >= count { return }
            try await Task.sleep(for: .milliseconds(20))
        }
    }
}

// tool calls -----------------------------------------------------------------

// parser ---------------------------------------------------------------------

@Test func parserReadsOneCall() {
    let p = ParsedReply("<tool_call>\n<function=recall>\n<parameter=query>\n主人的貓\n</parameter>\n</function>\n</tool_call>")
    #expect(p.thought == "")
    #expect(p.calls == [ToolCall(name: "recall", parameters: [.init("query", "主人的貓")])])
}

@Test func parserKeepsCallsInTextualOrderAndSpeechBeforeThem() {
    let raw = "好，我查一下再跑。\n\n<tool_call>\n<function=recall>\n<parameter=query>\nA\n</parameter>\n</function>\n</tool_call>\n"
        + "<tool_call>\n<function=key>\n<parameter=keys>\nls\n</parameter>\n</function>\n</tool_call>\n"
        + "<tool_call><function=think><parameter=level>high</parameter></function></tool_call>"
    let p = ParsedReply(raw)
    #expect(p.thought == "好，我查一下再跑。")
    #expect(p.calls.map(\.name) == ["recall", "key", "think"])
    #expect(p.calls.map { $0.parameters[0].value } == ["A", "ls", "high"])
    #expect(p.calls.allSatisfy { $0.malformed == nil })
    #expect(ParsedReply("就聊聊，沒有呼叫。\n").calls.isEmpty && ParsedReply("就聊聊，沒有呼叫。\n").thought == "就聊聊，沒有呼叫。")
}

@Test func parserStripsExactlyOneNewlineEachSideAndKeepsTheRest() {
    let raw = "<tool_call>\n<function=type>\n<parameter=text>\n\n  echo a\n  echo b  \n\n</parameter>\n</function>\n</tool_call>"
    #expect(ParsedReply(raw).calls[0].parameters == [.init("text", "\n  echo a\n  echo b  \n")])
    let tight = "<tool_call><function=key><parameter=key>Return</parameter></function></tool_call>"
    #expect(ParsedReply(tight).calls[0].parameters == [.init("key", "Return")])
}

@Test func keyIsOneKeyWithModifiersInAFixedOrder() throws {
    let cfg = try Config.load(root.appending(path: "dollcore.example.toml"))
    let specs = Tools.computerSpecs(cfg.computer, cfg.files)
    func decode(_ params: [(String, String)]) throws -> Tools.Request {
        let body = params.map { "<parameter=\($0.0)>\n\($0.1)\n</parameter>\n" }.joined()
        return try Tools.decode(ParsedReply("<tool_call>\n<function=key>\n\(body)</function>\n</tool_call>").calls[0], specs: specs)
    }
    func chord(_ params: [(String, String)]) throws -> String? {
        guard case .act(.key(let k)) = try decode(params) else { return nil }
        return k
    }
    #expect(try chord([("key", "Return")]) == "return")
    // Any order, case or repeat she writes comes out as the one order, so the same chord is the same step.
    #expect(try chord([("key", "S"), ("modifiers", "[\"alt\", \"CMD\", \"shift\", \"cmd\"]")]) == "cmd+shift+alt+s")
    #expect(try chord([("key", "a"), ("modifiers", "[]")]) == "a")
    // A chord or a sequence in `key`, an unknown modifier, or modifiers that are not a JSON array: errors that say how.
    for bad in [[("key", "cmd a")], [("key", "cmd+a")], [("key", "a"), ("modifiers", "[\"option\"]")],
                [("key", "a"), ("modifiers", "cmd")]] {
        #expect(throws: ToolError.self) { try decode(bad) }
    }
    do { _ = try decode([("key", "cmd shift s")]); Issue.record("accepted") } catch let e as ToolError {
        #expect(e.description.hasPrefix("key 只放一個鍵，收到 cmd shift s；修飾鍵放 modifiers"))
    }
}

@Test func jsonParametersDecodeFromTheirText() throws {
    let specs = Tools.specs(memory: try Config.load(root.appending(path: "dollcore.example.toml")).memory)
    let raw = "<tool_call>\n<function=remember>\n<parameter=items>\n"
        + "[{\"text\": \"主人養貓\", \"folder\": \"public\"},\n {\"text\": \"我的 VM 密碼\", \"folder\": \"own\"}, {\"text\": \"姐姐的事\", \"folder\": \"secret\"}]\n</parameter>\n</function>\n</tool_call>"
    guard case .remember(let items) = try Tools.decode(ParsedReply(raw).calls[0], specs: specs) else {
        Issue.record("not remember"); return
    }
    #expect(items.map(\.text) == ["主人養貓", "我的 VM 密碼", "姐姐的事"] && items.map(\.folder) == [.public, .own, .secret])
    let wrongFolder = ParsedReply("<tool_call>\n<function=remember>\n<parameter=items>\n[{\"text\": \"x\", \"folder\": \"private\"}]\n</parameter>\n</function>\n</tool_call>")
    #expect(throws: ToolError.self) { try Tools.decode(wrongFolder.calls[0], specs: specs) }
    let bad = ParsedReply("<tool_call>\n<function=remember>\n<parameter=items>\n主人養貓\n</parameter>\n</function>\n</tool_call>")
    #expect(throws: ToolError.self) { try Tools.decode(bad.calls[0], specs: specs) }
}

@Test func malformedBlocksBecomeCallsThatSayWhatIsWrong() {
    // unclosed <tool_call>, then a good one
    let a = ParsedReply("嗯\n<tool_call>\n<function=key>\n<parameter=keys>\nls\n</parameter>\n</function>\n"
                        + "<tool_call>\n<function=think>\n<parameter=level>\nhigh\n</parameter>\n</function>\n</tool_call>")
    #expect(a.thought == "嗯")
    #expect(a.calls.map(\.name) == ["key", "think"])
    #expect(a.calls[0].malformed == "<tool_call> 沒有 </tool_call> 收尾" && a.calls[1].malformed == nil)
    // <function> outside <tool_call>
    let b = ParsedReply("<function=key>\n<parameter=keys>\nls\n</parameter>\n</function>")
    #expect(b.calls == [ToolCall(name: "key", malformed: "<function=key> 沒有包在 <tool_call> 和 </tool_call> 裡")])
    #expect(b.thought == "")
    // missing </function>, missing </parameter>, junk inside, no function, duplicate parameter
    let cases: [(String, String)] = [
        ("<tool_call>\n<function=key>\n<parameter=keys>\nls\n</parameter>\n</tool_call>", "key：<function=key> 沒有 </function> 收尾"),
        ("<tool_call>\n<function=key>\n<parameter=keys>\nls\n</function>\n</tool_call>", "key：參數 keys 沒有 </parameter> 收尾"),
        ("<tool_call>\n<function=key>\nkeys: ls\n</function>\n</tool_call>", "key：<function=key> 裡除了 <parameter=…>…</parameter> 還有別的字：keys: ls"),
        ("<tool_call>\n{\"name\": \"key\"}\n</tool_call>", "<tool_call> 裡面要先寫 <function=工具名>"),
        ("<tool_call><function=key><parameter=keys>a</parameter><parameter=keys>b</parameter></function></tool_call>", "key：參數 keys 寫了兩次"),
    ]
    for (raw, problem) in cases {
        #expect(ParsedReply(raw).calls.map(\.malformed) == [problem], "\(raw)")
    }
}

@Test func toolCallLikeTextThatIsNotTheFormatIsAnErrorNeverSpeech() {
    // <invoke name=…> (seen from iq2_xs, /tmp/m2why/replay1.log), then a good call
    let a = ParsedReply("好，我開電腦看一下。\n<invoke name=\"use_computer\">\n</invoke>\n"
                        + "<tool_call>\n<function=think>\n<parameter=level>\nhigh\n</parameter>\n</function>\n</tool_call>")
    #expect(a.thought == "好，我開電腦看一下。")
    #expect(a.calls.map(\.name) == ["use_computer", "think"])
    #expect(a.calls[0].malformed == "<invoke name=\"use_computer\"> 不是工具呼叫的格式，沒有執行" && a.calls[1].malformed == nil)
    // a stray <parameter=…> with nothing around it: the rest is part of the broken call
    let b = ParsedReply("我開電腦。\n<parameter=level>\nmedium\n</parameter>\n")
    #expect(b.thought == "我開電腦。")
    #expect(b.calls == [ToolCall(name: "", malformed: "<parameter=level> 不是工具呼叫的格式，沒有執行")])
    let unclosed = ParsedReply("嗯<parameter=x>\nabc")
    #expect(unclosed.thought == "嗯" && unclosed.calls.count == 1)
    // <tool_call …> with attributes, lone closing tags, and two replies seen from iq2_xs at effort
    // medium (/tmp/m2why/latency.log): `<parameter name=…>…</invoke>`, and a <function=…> whose
    // only wrapper is a trailing </tool_call> (one mistake, one error)
    for raw in ["<tool_call name=\"key\">ls</tool_call>", "好</invoke>", "好\n</function>\n</tool_call>",
                "<parameter name=\"level\">\nmedium\n</parameter>\n\n</invoke>",
                "<function=use_computer>\n</function>\n</tool_call>"] {
        let p = ParsedReply(raw)
        #expect(p.calls.count == 1 && p.calls[0].malformed != nil && !p.thought.contains("<") , "\(raw)")
    }
    #expect(ParsedReply("<tool_call name=\"key\">ls</tool_call>").calls[0].name == "key")
    #expect(ParsedReply("<parameter name=\"level\">\nmedium\n</parameter>").calls[0].name == "")
    #expect(ParsedReply("<function name=\"use_computer\"></function>").calls.map(\.name) == ["use_computer"])
}

@Test func aMalformedCallsResultTellsTheFormat() throws {
    let specs = Tools.specs(memory: try Config.load(root.appending(path: "dollcore.example.toml")).memory)
    let call = ParsedReply("<invoke name=\"use_computer\">\n</invoke>").calls[0]
    #expect(throws: ToolError.self) { try Tools.decode(call, specs: specs) }
    do { _ = try Tools.decode(call, specs: specs) } catch {
        #expect("\(error)" == "<invoke name=\"use_computer\"> 不是工具呼叫的格式，沒有執行\n" + ParsedReply.format)
    }
}

@Test func systemPromptOpensWithTheTemplateToolBlockThenThePersona() async throws {
    let h = try await Harness()
    let sys = await h.doll.systemPrompt()
    #expect(sys.hasPrefix("# Tools\n\nYou have access to the following functions:\n\n<tools>\n{\"type\": \"function\", \"function\": {\"name\": \"say\""))
    #expect(sys.contains("\n</tools>\n\nIf you choose to call a function ONLY reply in the following format with NO suffix:"))
    #expect(sys.contains("</IMPORTANT>\n\n你是"))
    let listed = sys.components(separatedBy: "\n").filter { $0.hasPrefix("{\"type\": \"function\"") }
    #expect(listed.count == 5)
    for (line, name) in zip(listed, ["say", "think", "remember", "recall", "use_computer"]) {
        #expect(line.contains("\"name\": \"\(name)\""))
        #expect((try? JSONSerialization.jsonObject(with: Data(line.utf8))) != nil)   // valid JSON
    }
    #expect(!sys.contains("\"name\": \"click\""))                       // the computer menu is not in it
}

// tool calls -----------------------------------------------------------------

@Test func everyCallInOneReplyRunsInOrderAndGetsItsOwnResult() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("做三件事")
    try await h.turn(Scripted("好，一次做完", recall("一次做完"), think("medium"), recall("下一回合")))
    #expect(h.said.isEmpty)                                              // text outside her calls is her thought: never said
    // recall also searches today's timeline (2026-11-10): her thought; the second one, the think result before it
    #expect(h.last.actions.count == 3 && h.last.actions[0].hasPrefix("recall ") && h.last.actions[1] == "think medium"
            && h.last.actions[2].hasPrefix("recall ") && h.last.usedTool)
    let results = try h.toolResults()
    #expect(results[0].text.hasPrefix("一次做完：") && results[0].text.contains("〔今天 ") && results[0].text.contains("好，一次做完")
            && results[1].text == "下一回合 effort：medium" && results[2].text.contains("\n下一回合 effort：medium"))
    #expect(await h.doll.followUp)                                       // the results give her the next turn
    try await h.turn("都好了")
    let msgs = await h.model.seen(1)
    let a = try #require(msgs.firstIndex { $0.role == "assistant" })
    // her raw content, call text included, is what goes back
    #expect(msgs[a].content == Scripted("好，一次做完", recall("一次做完"), think("medium"), recall("下一回合")).raw)
    #expect(msgs[(a + 1)...(a + 3)].map(\.role) == ["tool", "tool", "tool"])
    #expect(msgs[(a + 1)...(a + 3)].map(\.content) == results.map(\.text))
    #expect(await h.model.requests.map(\.effort) == ["medium", "medium"])    // owner turn, then her think
}

@Test func aRawReplyRunsItsCallsInTheOrderSheWroteThem() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("查一下再列目錄")
    let raw = "我先查，再看目錄。\n\n<tool_call>\n<function=recall>\n<parameter=query>\n列目錄\n</parameter>\n</function>\n</tool_call>\n"
        + "<tool_call>\n<function=think>\n<parameter=level>\nmedium\n</parameter>\n</function>\n</tool_call>"
    try await h.turn(Scripted(stringLiteral: raw))
    #expect(h.said.isEmpty)
    #expect(h.last.actions.count == 2 && h.last.actions[0].hasPrefix("recall ") && h.last.actions[1] == "think medium")
    #expect(try h.toolResults().map { $0.text.hasPrefix("列目錄：") && $0.text.contains("〔今天 ") || $0.text == "下一回合 effort：medium" } == [true, true])
    let doll = try #require(try h.events().first { $0.kind == .doll })
    #expect(doll.text == raw)                                            // stored byte-identical
}

@Test func failedCallsStillGetAToolResult() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("試試")
    try await h.turn(Scripted("", ("bogus", []), ("recall", [("q", "ls")]), think("low"),
                              remember(kinds: [("會失敗的記憶", false, "done"), ("主人喜歡貓", false, nil)]), ("recall", []), useComputer,
                              ("remember", [("items", "主人養貓")])))
    let results = try h.toolResults()
    #expect(results.count == 7)
    #expect(results[0].text == "失敗：沒有 bogus 這個工具")
    #expect(results[1].text == "失敗：recall 沒有 q 這個參數")
    #expect(results[2].text == "失敗：think 的 level 只能是 medium 或 high，收到 low")
    #expect(results[3].text.hasPrefix("第 1 條沒存：kind 只能是 happened、intended、uncertain，或不填；收到 done\n第 2 條已存入 public/"))
    #expect(results[4].text == "失敗：recall 少了參數 query")
    #expect(results[5].text.hasPrefix("電腦選單打開了"))                  // later calls still run
    #expect(results[6].text.hasPrefix("失敗：remember 的 items 要是 JSON 陣列"))
    #expect(h.last.actions == ["error bogus", "error recall", "error think", "error remember", "remember public",
                               "error recall", "use_computer", "error remember"])
    #expect(await h.doll.effortNext == nil)
}

@Test func aMalformedCallGetsAResultSayingWhatWasWrong() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("試試")
    try await h.turn("<function=recall>\n<parameter=query>\nls\n</parameter>\n</function>")
    #expect(h.said.isEmpty)
    #expect(try h.toolResults().map(\.text) == ["失敗：<function=recall> 沒有包在 <tool_call> 和 </tool_call> 裡\n" + ParsedReply.format])
    #expect(h.last.actions == ["error recall"] && h.last.usedTool)
}

@Test func aPastToolRoundRendersByteIdenticallyOnLaterTurnsImagesIncluded() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("記一下我養貓，然後看電腦")
    try await h.turn(Scripted("好", remember([("主人養貓", false)]), useComputer))
    h.vm.setFrame(7)                                                    // the screen changes in between
    try await h.turn(Scripted("再看一次", screenshotCall))
    try await h.turn("記好了，畫面看過了")
    await h.doll.ownerSaid("謝謝")
    try await h.turn("不客氣")
    let reqs = await h.model.requests.map(\.messages)
    let enc = { (m: [ChatMessage]) in try ChatMessage.encoder.encode(m) }
    // Each request is the previous one plus new messages: same bytes, footer and images included.
    for i in 1..<reqs.count {
        #expect(try enc(Array(reqs[i].prefix(reqs[i - 1].count))) == (try enc(reqs[i - 1])))
    }
    // Two computer results, each a full screenshot and a window crop: the stored files' exact bytes,
    // as data URIs in the tool messages, full screen first.
    let results = try h.toolResults().filter { $0.image != nil }
    #expect(results.count == 2 && results.allSatisfy { $0.crop != nil })
    let files = results.flatMap { [$0.image!, $0.crop!] }
    let bytes = try files.map { try Data(contentsOf: h.dir.appending(path: "shots/\($0)")) }
    #expect(bytes.map { String(decoding: $0, as: UTF8.self) }
            == ["frame 0 full", "frame 0 crop 10,20,300,200", "frame 7 full", "frame 7 crop 10,20,300,200"])
    let uris = bytes.map { "data:image/jpeg;base64," + $0.base64EncodedString() }
    #expect(reqs[3].flatMap(\.images) == uris)
    #expect(reqs[3].filter { !$0.images.isEmpty }.allSatisfy { $0.role == "tool" })
    // On the wire: [text, full screenshot, the crop's place, the crop, window outline].
    let wire = String(decoding: try enc([reqs[3].first { !$0.images.isEmpty }!]), as: UTF8.self)
    #expect(wire.contains(#""content":[{"text":"#)
            && wire.contains(#"{"image_url":{"url":"\#(uris[0])"},"type":"image_url"},"#
                             + #"{"text":"\n視窗圖：最前面的視窗，在全螢幕 (10,20)–(300,200)；用這張圖上的座標點要加 on: window。","type":"text"},"#
                             + #"{"image_url":{"url":"\#(uris[1])"},"type":"image_url"},{"text":"\n前景 app：Fake"#))
    // The system prompt, tool block included, is the same on every turn.
    #expect(Set(reqs.map { $0[0] }.map { "\($0.role)|\($0.content)" }).count == 1)
    #expect(reqs[0][0].role == "system" && reqs[0][0].content.hasPrefix("# Tools\n"))
}

@Test func toolResultsGiveHerTheNextTurnButAWatchAckAloneDoesNot() async throws {
    let h = try await Harness([Scripted("", useComputer), Scripted("", say("好了")), Scripted("", say("盯著"), watch("window", 1)), Scripted("", say("沒變"))])
    let doll = h.doll
    let runner = Task { try await doll.run() }
    // Poll instead of fixed sleeps: a busy parallel suite made the 500 ms waits race.
    func waitFor(_ n: Int, said s: Int, _ seconds: Double = 20) async throws {
        let deadline = ContinuousClock.now + .seconds(seconds)
        while (await h.model.requests.count < n || h.said.count < s), ContinuousClock.now < deadline {
            try await Task.sleep(for: .milliseconds(20))
        }
    }
    await h.doll.ownerSaid("開電腦")
    try await waitFor(2, said: 1)
    #expect(await h.model.requests.count == 2 && h.said == ["好了"])     // the result woke her once
    await h.doll.ownerSaid("盯著畫面")
    try await waitFor(3, said: 2)
    try await Task.sleep(for: .milliseconds(200))                       // well inside max_wait (1 s)
    #expect(await h.model.requests.count == 3)                          // neither the watch ack nor say woke her
    try await waitFor(4, said: 3)                                       // max_wait passing does
    #expect(await h.model.requests.count == 4 && h.said.last == "沒變")
    await h.doll.stop()
    try await runner.value
}

// queue and turns ------------------------------------------------------------

@Test func drainOrdersByConfiguredPriority() async throws {
    let h = try await Harness()
    await h.doll.put(Event(.watch, .internal, "r"))
    await h.doll.ownerSaid("text")
    await h.doll.ownerSaid("voice", voice: true)
    try await h.turn("好")
    let events = try h.events()
    #expect(events.map(\.text) == ["voice", "text", "r", "好"])
    #expect(events.map(\.priority) == [.ownerVoice, .ownerText, .watch, .watch])
}

@Test func ownerVoiceCancelsTheCallAndTheRerunSeesTheVoiceWithTheSameEffort() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("這題難")
    try await h.turn(Scripted("", say("等我"), think("high")))         // next call is high
    await h.model.stall()
    await h.model.add([Scripted("", say("好"))])
    await h.doll.ownerSaid("開始算")
    let doll = h.doll
    let running = Task { try await doll.runOnce() }
    while await h.model.requests.count < 2 { try await Task.sleep(for: .milliseconds(10)) }
    await h.doll.ownerSaid("等等不用算了", voice: true)      // cancels the stalled call
    try await running.value
    let reqs = await h.model.requests
    #expect(reqs.count == 3)
    #expect(reqs[1].effort == "high" && reqs[2].effort == "high")    // the think survives the cancelled call
    #expect(!reqs[1].messages.contains { $0.content.contains("等等不用算了") })
    #expect(reqs[2].messages.contains { $0.content.contains("等等不用算了") })
    #expect(h.reports.map(\.index) == [1, 2] && h.said == ["等我", "好"])
    #expect(h.logs.contains { $0.contains("turn 2 interrupted") })
    let later = try h.events().filter { $0.kind == .owner }.map(\.text)
    #expect(later == ["這題難", "開始算", "等等不用算了"])
}

@Test func noEventMeansNoTurn() async throws {
    let h = try await Harness([Scripted("", say("哈囉"))])
    let doll = h.doll
    let runner = Task { try await doll.run() }
    try await Task.sleep(for: .milliseconds(500))
    #expect(await h.model.requests.isEmpty)
    await h.doll.ownerSaid("嗨")
    try await waitUntil { await h.model.requests.count == 1 && h.said == ["哈囉"] }
    #expect(await h.model.requests.count == 1 && h.said == ["哈囉"])
    try await Task.sleep(for: .milliseconds(300))
    #expect(await h.model.requests.count == 1)        // her reply alone is not an event
    await h.doll.stop()
    try await runner.value
}

// footer ---------------------------------------------------------------------

@Test func footerIsStoredAndReplayedVerbatimBeforeHerReply() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("嗨")
    try await h.turn("哈囉")
    let first = h.last.footer
    #expect(first.hasPrefix("【狀況】\n- 現在 ") && first.contains("- 選單：頂層（電腦工具鎖著；use_computer(expect) 打開）")
            && first.contains("- watch：沒有") && first.contains("下一回合 effort：medium"))
    #expect(try h.events().last!.footer == first)
    await h.doll.ownerSaid("再來")
    try await h.turn("好")
    let msgs = await h.model.seen(1)
    let i = msgs.firstIndex { $0.content == first }!
    #expect(msgs[i].role == "user" && msgs[i + 1] == ChatMessage(role: "assistant", content: "哈囉"))
    #expect(msgs.last!.content == h.last.footer && msgs.last!.content != first)
    #expect(try h.events().filter { $0.text.contains("【狀況】") }.isEmpty)   // footers are not events
}

@Test func thinkSetsTheNextCallsEffortOnceAndShowsInTheFooter() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("這題難")
    try await h.turn(Scripted("", say("等我"), think("high")))
    let status = try await h.doll.status()
    #expect(h.last.actions == ["said", "think high"] && status.contains("effort：high"))
    await h.doll.ownerSaid("嗯")
    try await h.turn("想好了")
    await h.doll.ownerSaid("嗯")
    try await h.turn("嗯")
    #expect(await h.model.requests.map(\.effort) == ["medium", "high", "medium"])
    #expect(h.reports[1].footer.contains("effort：high") && h.reports[2].footer.contains("effort：medium"))
    // Up only (2026-11-11: `none` removed; at none she repeated think(none) 16 turns in a row).
    await h.doll.ownerSaid("再查")
    try await h.turn(Scripted("", think("none")))
    #expect(try h.toolResults().last!.text == "失敗：think 的 level 只能是 medium 或 high，收到 none")
}

@Test func taskTurnsUseTaskTurnEffortAndOnlyAnEmptyTurnTheDefault() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("查一下")
    try await h.turn(Scripted("好", recall("一")))                     // owner line drained
    try await h.turn(Scripted("", think("high")))                      // her tool result
    try await h.turn("想好了")                                          // her think wins over the task effort
    try await h.turn("嗯")                                              // nothing new: the default
    await h.doll.put(Event(.watch, .internal, "盯著的整個畫面變了。"))
    try await h.turn("看到了")                                          // a watch wake
    await h.doll.ownerSaid("再說一次", voice: true)
    try await h.turn("沒有喔")                                          // owner voice
    #expect(await h.model.requests.map(\.effort) == ["medium", "medium", "high", "none", "medium", "medium"])
    #expect(h.reports.map(\.effort) == ["medium", "medium", "high", "none", "medium", "medium"])
    #expect(h.reports[1].footer.contains("effort：medium") && h.reports[3].footer.contains("effort：none"))

    let off = try await Harness(tune: { $0.llm.taskTurnEffort = "none"; $0.llm.defaultEffort = "high" })
    await off.doll.ownerSaid("嗨")
    try await off.turn(Scripted("哈囉", recall("嗨")))
    try await off.turn("沒有")
    try await off.turn("嗯")
    #expect(await off.model.requests.map(\.effort) == ["none", "none", "high"])
}

// idle -----------------------------------------------------------------------

@Test func idleTurnsWithoutAToolCallWriteOneLogLine() async throws {
    let h = try await Harness { $0.idle.maxTurnsWithoutTool = 2 }
    let idle = { h.logs.filter { $0.contains("idle:") } }
    await h.doll.ownerSaid("1")
    try await h.turn("嗯")
    #expect(idle().isEmpty)
    await h.doll.ownerSaid("2")
    try await h.turn(Scripted("", recall("x")))                       // a tool call resets the count
    try await h.turn("嗯")
    #expect(idle().isEmpty)
    await h.doll.ownerSaid("3")
    try await h.turn("嗯")
    #expect(idle() == ["INFO idle: 2 turns in a row without a tool call"])
    await h.doll.ownerSaid("4")
    try await h.turn("嗯")
    #expect(idle().count == 1)
}

// restart, config ------------------------------------------------------------

/// The markdown files are the source of truth; a process whose index tables are empty (fresh
/// database, or a lost/corrupt index) must re-index them at boot or recall finds nothing.
@Test func bootIndexesMemoriesLeftInMarkdown() async throws {
    let h = try await Harness([Scripted("", remember([("主人的伺服器叫 Strata", false)]))])
    await h.doll.ownerSaid("記一下我的伺服器")
    try await h.turn()
    #expect(try h.store.countMemoryIndex() == 1)
    try h.store.clearMemoryIndex()                     // what the next process sees
    let again = try await Harness([Scripted("", recall("伺服器叫什麼"))]) {
        $0.core.dataDir = h.dir.path
        $0.memory.dir = h.dir.appending(path: "memory").path
    }
    try await again.doll.boot()
    #expect(try h.store.countMemoryIndex() == 1)
    await again.doll.ownerSaid("我的伺服器叫什麼？")
    try await again.turn()
    #expect(again.last.actions.count == 1 && again.last.actions[0].hasPrefix("recall "))
    // the indexed row is the one that answers: FTS finds it, public, from the owner
    let ids = try h.store.memoryFTS(match: "\"Strata\"", folders: ["public"], limit: 5).map(\.id)
    #expect(ids.count == 1)
    let meta = try #require(try h.store.memoryMeta(ids[0]))
    #expect(meta.folder == "public" && meta.confidence > 0.8)
    // the tool result in her timeline carries the indexed text
    #expect(try h.toolResults().last!.text.contains("〔確定｜public〕\n主人的伺服器叫 Strata"))
    await h.doll.stop()
}

/// Vectors from older code (index version 1 kept only the first token's vector) are wrong even
/// when every file is indexed: a different index version rebuilds the whole index at boot; the
/// current one leaves it alone.
@Test func bootRebuildsAnIndexBuiltByAnotherIndexVersion() async throws {
    let h = try await Harness([Scripted("", remember([("主人的伺服器叫 Strata", false)]))])
    #expect(try h.store.memoryIndexVersion() == Memory.indexVersion && Memory.indexVersion == 4)
    await h.doll.ownerSaid("記一下我的伺服器")
    try await h.turn()
    let good = try h.store.memoryVectors(folders: ["public"])
    #expect(good.count == 1)
    let meta = try #require(try h.store.memoryMeta(good[0].id))
    func stale() throws {      // what an old process left: same rows, another vector
        try h.store.putMemoryIndex(id: good[0].id, path: meta.path, folder: meta.folder, confidence: meta.confidence, ts: 0,
                                   sourceSeq: 1, kind: meta.kind, tier: "owner", entities: [], text: "主人的伺服器叫 Strata",
                                   vector: Memory.blob([1, 0, 0]))
    }
    func boot() async throws {
        let again = try await Harness { $0.core.dataDir = h.dir.path; $0.memory.dir = h.dir.appending(path: "memory").path }
        try await again.doll.boot()
    }
    // Current version, counts agree: nothing is rebuilt.
    try stale()
    try await boot()
    #expect(try h.store.memoryVectors(folders: ["public"]).first?.vector == Memory.blob([1, 0, 0]))
    // Version 1 (or none): rebuilt, the vector made again by the current code, the version stamped.
    try h.store.setMemoryIndexVersion(1)
    try await boot()
    #expect(try h.store.memoryVectors(folders: ["public"]).first?.vector == good[0].vector)
    #expect(try h.store.memoryIndexVersion() == 4)
    await h.doll.stop()
}

@Test func configRejectsUnknownKeysAndBadPriority() throws {
    let file = FileManager.default.temporaryDirectory.appending(path: "dollcore-cfg-\(UUID().uuidString).toml")
    try "[llm]\nbase_url = \"http://x\"\nmodel = \"m\"\nbogus = 1\n".write(to: file, atomically: true, encoding: .utf8)
    #expect(throws: TOMLError.self) { try Config.load(file) }
    try "[llm]\nbase_url = \"http://x\"\nmodel = \"m\"\n[core]\npriority = [\"owner_text\"]\n".write(to: file, atomically: true, encoding: .utf8)
    #expect(throws: TOMLError.self) { try Config.load(file) }
    try "[llm]\nbase_url = \"http://x\"\nmodel = \"m\"\n[idle]\nmax_turns_without_tool = 0\n".write(to: file, atomically: true, encoding: .utf8)
    #expect(throws: TOMLError.self) { try Config.load(file) }
}

// memory (M3a) ---------------------------------------------------------------
//
// A corpus built for the acceptance numbers in the plan: 30 records written through
// remember, 10 questions, ≥ 8 right. Two of the ten can only be answered by one side of
// the hybrid index: `kettlebell` is not a paraphrasable Chinese token (FTS-only), and
// 出國帶回什麼 shares no trigram with its record (embedding-only). The rest were checked
// against the real NLContextualEmbedding model before locking the numbers.

private let memoryCorpus = [
"主人最喜歡吃牛肉麵，尤其是巷口張家那碗",
"主人講過他小時候住在高雄",
"主人每週三晚上會跟阿明打籃球",
"主人的貓叫咪咪，是一隻橘貓",
"主人討厭香菜，碗裡有香菜會全部挑掉",
"主人正在寫一篇關於時序記憶的論文",
"主人的工作專案叫 DollOS，用 Swift 寫的",
"主人打算二月請假去北海道滑雪",
"主人擔心下個月的房租會調漲",
"主人跟姐姐吵架了，心情很低落",                      // #9: secret
"主人買了一把 kettlebell 十八公斤在家練力量",
"主人上次說他不太會游泳",
"主人喜歡聽伍佰的歌，開車時會大聲唱",
"主人的生日是三月十二日",
"主人對花生過敏，嚴重的話會起蕁麻疹",              // #14: secret
"主人週一週二都睡很晚，早上起不來",
"主人說他想學彈吉他",
"主人的腳踏車上週在火車站被偷了",
"主人幫咪咪買了自動餵食器",
"主人習慣早上六點起床跑步",
"主人說他最討厭星期一開早會",
"主人在用一個叫 Strata 的模型伺服器",
"主人的筆電是 M5 的 MacBook Pro",
"主人上次去日本買了招福貓的御守",
"主人說明年想帶爸媽去旅行",
"主人的銀行帳戶密碼結尾是 42",                      // #25: secret
"主人覺得自己方向感很差，常迷路",
"主人收藏了三十幾張黑膠唱片",
"主人上次煮火鍋把廚房弄得到處都是煙",
"主人說跑步聽到伍佰會跑比較快",
]
private let secretRecords = Set([9, 14, 25])

/// (question, the record that answers it)
private let memoryQueries: [(String, Int)] = [
("主人最愛吃什麼", 0),
("kettlebell", 10),                                   // only FTS can find this
("主人的貓叫什麼名字", 3),
("對什麼食物過敏會起蕁麻疹", 14),
("跟阿明做什麼運動", 2),
("主人為什麼心情低落", 9),
("主人擔心房租會調漲嗎", 8),
("主人的生日幾號", 13),
("想帶爸媽去哪裡旅行", 24),
("主人出國旅遊帶回什麼", 23),                          // only the embedding can find this
]

/// Writes the whole corpus through one remember call with 30 items. Returns the harness.
private func memoryHarness() async throws -> Harness {
    let h = try await Harness()
    await h.doll.ownerSaid("這些是我講過的事，記起來")
    let items = memoryCorpus.enumerated().map { i, text in (text, secretRecords.contains(i)) }
    try await h.turn(Scripted("", remember(items)))
    return h
}

/// One recall call; returns its tool result.
private func ask(_ h: Harness, _ query: String, secret: Bool = false) async throws -> String {
    await h.doll.ownerSaid("查一下", secret: secret)
    try await h.turn(Scripted("", recall(query)))
    return try h.toolResults().last!.text
}

@Test func writeThirtyAndQueryTenWithHybridRetrieval() async throws {
    let h = try await memoryHarness()
    #expect(h.last.actions.filter { $0.hasPrefix("remember ") }.count == 30)
    let ack = try h.toolResults().last!.text
    #expect(ack.split(separator: "\n").filter { $0.contains("已存入") }.count == 30)
    // 30 markdown files, the configured three in secret/.
    let fm = FileManager.default
    let pubDir = h.dir.appending(path: "memory/public").path
    let secDir = h.dir.appending(path: "memory/secret").path
    #expect(try fm.contentsOfDirectory(atPath: pubDir).filter { $0.hasSuffix(".md") }.count == 27)
    #expect(try fm.contentsOfDirectory(atPath: secDir).filter { $0.hasSuffix(".md") }.count == 3)

    var correct = 0
    for (query, want) in memoryQueries {
        // The owner's turn: secrets visible (5.1), the same setting the plan's 10 questions assume.
        let hits = try await ask(h, query, secret: true)
        // The wanted record is one of the returned hits.
        if hits.contains(memoryCorpus[want]) { correct += 1 }
        #expect(hits.contains("〔確定｜") || hits.contains("〔大概｜") || hits.contains("〔聽說｜"))
        #expect(h.reports.last!.actions.last!.hasPrefix("recall "))
    }
    #expect(correct >= 8)   // 30/10 acceptance number (plan M3a)
}

@Test func indexRebuildFromMarkdownGivesTheSameAnswers() async throws {
    let h = try await memoryHarness()
    let answers = memoryQueries.map { q, _ in q }
    var before: [String] = []
    for q in answers { before.append(try await ask(h, q, secret: true)) }
    // Wipe the index tables the way a lost/corrupt index would look, then rebuild from the files.
    try h.store.clearMemoryIndex()
    #expect(try h.store.memoryFTS(match: "\"kettlebell\"", folders: ["secret", "own", "public"], limit: 10).isEmpty)
    try await h.doll.rebuildMemory()
    var after: [String] = []
    for q in answers { after.append(try await ask(h, q, secret: true)) }
    #expect(before == after)
    #expect(before.allSatisfy { $0.contains("：") })
}

@Test func oneQueryOnlyFTSCanFindAndOneOnlyTheEmbedding() async throws {
    let h = try await memoryHarness()
    // Exact rare token, no paraphrase: a pure vector search over this model does not rank it first.
    #expect(try await ask(h, "kettlebell").contains(memoryCorpus[10]))
    // Paraphrase with no shared trigram with the record.
    #expect(try await ask(h, "主人出國旅遊帶回什麼").contains(memoryCorpus[23]))
}

@Test func secretMemoryStaysOutOfTurnsWithoutSecrets() async throws {
    let h = try await memoryHarness()
    let secretText = memoryCorpus[9]
    // So far nothing secret is on the timeline: this turn has no secrets visible.
    let without = try await ask(h, "跟姐姐吵架了")
    #expect(!without.contains(secretText) && !without.contains("｜secret｜"))
    #expect(without.hasPrefix("跟姐姐吵架了："))     // the query itself ran
    #expect(!(try h.toolResults().last!.secret))
    // Now a secret enters the timeline (spec 5.1: the same rule that marks her replies).
    let withSecret = try await ask(h, "跟姐姐吵架了", secret: true)
    #expect(withSecret.contains(secretText) && withSecret.contains("〔確定｜secret〕"))
    #expect(try h.toolResults().last!.secret)          // a secret hit marks the tool result
}

// memory kind (2026-11-08: hers, no FM) ----------------------------------------

@Test func kindIsWrittenWhenGivenAndEmptyWhenNot() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("我下個月要去大阪，我的貓叫麻糬")
    try await h.turn(Scripted("", remember(kinds: [("主人下個月要去大阪", false, "intended"), ("主人的貓叫麻糬", false, nil)])))
    let ack = try h.toolResults().last!.text.split(separator: "\n")
    #expect(ack.count == 2 && ack[0].contains("kind intended") && !ack[1].contains("kind"))
    let dir = h.dir.appending(path: "memory/public")
    var kinds: [String: String] = [:]
    for name in try FileManager.default.contentsOfDirectory(atPath: dir.path) where name.hasSuffix(".md") {
        let parsed = try Memory.parseFile(String(contentsOf: dir.appending(path: name), encoding: .utf8))
        #expect(try h.store.memoryMeta(String(name.dropLast(3)))?.kind == parsed.kind)
        kinds[parsed.body] = parsed.kind
    }
    #expect(kinds == ["主人下個月要去大阪": "intended", "主人的貓叫麻糬": ""])
    // the index rebuilt from markdown keeps it
    try await h.doll.rebuildMemory()
    for name in try FileManager.default.contentsOfDirectory(atPath: dir.path) where name.hasSuffix(".md") {
        let parsed = try Memory.parseFile(String(contentsOf: dir.appending(path: name), encoding: .utf8))
        #expect(try h.store.memoryMeta(String(name.dropLast(3)))?.kind == kinds[parsed.body])
    }
}

@Test func anInvalidKindFailsOnlyThatItem() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("記一下")
    let items = #"[{"text": "主人去過京都", "folder": "public", "kind": "done"}, {"text": "主人喜歡貓", "folder": "public"}, "#
        + #"{"text": "主人怕蛇", "folder": "public", "kind": 3}, {"text": "主人昨天跑了五公里", "folder": "public", "kind": "happened"}]"#
    try await h.turn(Scripted("", ("remember", [("items", items)])))
    let lines = try h.toolResults().last!.text.split(separator: "\n").map(String.init)
    #expect(lines.count == 4)
    #expect(lines[0] == "第 1 條沒存：kind 只能是 happened、intended、uncertain，或不填；收到 done")
    #expect(lines[1].hasPrefix("第 2 條已存入 public/"))
    #expect(lines[2] == "第 3 條沒存：kind 只能是 happened、intended、uncertain，或不填；收到 不是字串的值")
    #expect(lines[3].hasPrefix("第 4 條已存入 public/") && lines[3].contains("kind happened"))
    #expect(h.last.actions == ["error remember", "remember public", "error remember", "remember public"])
    #expect(try h.store.countMemoryIndex() == 2)
}

@Test func recallLabelsCarryTheKindOnlyWhenGiven() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("我下個月要去大阪，我的貓叫麻糬")
    try await h.turn(Scripted("", remember(kinds: [("主人下個月要去大阪", false, "intended"), ("主人的貓叫麻糬", false, nil)])))
    await h.doll.ownerSaid("你記得我什麼？")
    try await h.turn(Scripted("", recall("主人下個月 主人的貓")))
    let out = try h.toolResults().last!.text
    #expect(out.contains("〔確定的打算｜public〕\n主人下個月要去大阪"))
    #expect(out.contains("〔確定｜public〕\n主人的貓叫麻糬"))
    #expect(!out.contains("未判"))
    let hit = MemoryHit(id: "x", folder: "public", secret: false, confidence: 0.3, level: "聽說", kind: .happened, text: "")
    #expect(hit.label == "聽說的已發生")
    #expect(MemoryHit(id: "y", folder: "public", secret: false, confidence: 0.3, level: "聽說", kind: nil, text: "").label == "聽說")
}

// computer (M2) ----------------------------------------------------------------

private func waitUntil(seconds: Double = 3, _ cond: () async -> Bool) async throws {
    let deadline = ContinuousClock.now + .seconds(seconds)
    while ContinuousClock.now < deadline {
        if await cond() { return }
        try await Task.sleep(for: .milliseconds(10))
    }
}

private func watchWakes(_ h: Harness) async -> [Event] { await h.doll.queue.filter { $0.kind == .watch } }

/// A harness whose first turn opened the computer menu.
private func menuHarness(tune: (inout Config) -> Void = { _ in }) async throws -> Harness {
    let h = try await Harness(tune: tune)
    await h.doll.ownerSaid("用電腦")
    try await h.turn(Scripted("", useComputer))
    return h
}

@Test func theComputerMenuOpensWithUseComputerAndLocksAgainWithOutcome() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("按一下 Return")
    try await h.turn(Scripted("", key("Return")))
    #expect(try h.toolResults().last!.text == "失敗：key 現在鎖著：先呼叫 use_computer(expect) 打開電腦選單")
    #expect(h.last.actions == ["error key"] && h.vm.actions.isEmpty)
    #expect(h.last.footer.contains("- 選單：頂層"))
    try await h.turn(Scripted("", useComputer))
    let menu = try h.toolResults().last!
    #expect(menu.text.hasPrefix("電腦選單打開了") && menu.image != nil)
    let listed = menu.text.components(separatedBy: "\n").filter { $0.hasPrefix("{\"type\": \"function\"") }
    let names = ["click", "type", "key", "scroll", "drag", "read_file", "write_file", "edit_file", "screenshot", "watch", "outcome"]
    #expect(listed.count == names.count)
    for (line, name) in zip(listed, names) {
        #expect(line.contains("\"name\": \"\(name)\""))
        #expect((try? JSONSerialization.jsonObject(with: Data(line.utf8))) != nil)
    }
    try await h.turn(Scripted("", key("Return")))
    #expect(h.last.actions == ["key"] && h.vm.actions == [.key("return")])
    #expect(h.last.footer.contains("- 選單：電腦（做完呼叫 outcome(success, summary) 結束）"))
    try await h.turn(Scripted("", outcome(false, "沒看到")))
    #expect(try h.toolResults().last!.text == "記下了：沒達成。離開電腦選單了；電腦工具鎖回去了。")
    #expect(await h.doll.scenario == nil)
    try await h.turn(Scripted("", key("Return")))
    #expect(h.last.actions == ["error key"] && h.vm.actions.count == 1)
    #expect(h.last.footer.contains("- 選單：頂層"))
    // Opening and closing the menu never touched the system prompt.
    #expect(Set(await h.model.requests.map { $0.messages[0].content }).count == 1)
}

@Test func leavingAScenarioPutsHerSummaryInPlaceOfItsTurns() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("把 notes 存檔")
    let enter = Scripted("好，我來", useComputer)
    try await h.turn(enter)                                                        // request 0: before entering
    try await h.turn(Scripted("", key("cmd+s")))                                   // request 1: inside
    try await h.turn(Scripted("", say("存好了。"), outcome(true, "按 ⌘S 存了檔，看標題列確認沒有「已編輯」。")))  // request 2: inside
    // While it was open, its turns went out in full: the menu's screenshot, the key's result.
    let inside = await h.model.seen(2)
    #expect(inside.contains { !$0.images.isEmpty } && inside.contains { $0.role == "tool" && $0.content == fakeWindowsLine })
    await h.doll.ownerSaid("謝謝")
    try await h.turn("不客氣")                                                     // request 3: first after leaving
    await h.doll.ownerSaid("再見")
    try await h.turn("掰")                                                         // request 4

    let before = await h.model.seen(0), after = await h.model.seen(3)
    // The main context up to and including her entering reply, unchanged; then the enter call's
    // result is her summary; then what she said when she left; then what came after.
    #expect(Array(after.prefix(before.count + 1)) == before + [ChatMessage(role: "assistant", content: enter.raw)])
    #expect(after[before.count + 1] == ChatMessage(role: "tool", content: "成功。你自己記下的經過：\n按 ⌘S 存了檔，看標題列確認沒有「已編輯」。"))
    #expect(after[before.count + 2] == ChatMessage(role: "assistant", content: Scripted("", say("存好了。")).raw))
    #expect(after[before.count + 3] == ChatMessage(role: "tool", content: ""))
    #expect(after[before.count + 4].role == "user" && after[before.count + 4].content.hasSuffix("主人] 謝謝"))
    #expect(after.last?.content.hasPrefix("【狀況】") == true)
    // Nothing from inside: no screenshot, no computer result, no menu, no intermediate reply.
    #expect(after.allSatisfy { $0.images.isEmpty && $0.extra.isEmpty })
    #expect(after.filter { $0.role == "tool" }.count == 2)
    #expect(!after.contains { $0.content == fakeWindowsLine || $0.content.hasPrefix("電腦選單打開了") || $0.content.hasPrefix("記下了") })
    // Later requests extend it byte-identically.
    let next = await h.model.seen(4)
    #expect(Array(next.prefix(after.count)) == after)
    let enc = { (m: [ChatMessage]) in try ChatMessage.encoder.encode(m) }
    #expect(try enc(next).starts(with: enc(after).dropLast()))
    // The timeline still holds every event of the scenario.
    #expect(try h.events().contains { $0.kind == .tool && $0.text == fakeWindowsLine && $0.image != nil })
}

@Test func onlyTheLastImageTurnsKeepTheirScreenshotsAndThePrefixBreaksOnlyThere() async throws {
    // spec 3.1, 2026-11-09: image_turns_kept = 2 here (the default 0 keeps every turn's images: on
    // Qwen3-Next the cache resumes only from checkpoints). A turn's images are the results after her reply.
    // Request k is the one that produced turn k+1's reply, so it holds the results up to turn k.
    let h = try await Harness(tune: { $0.computer.imageTurnsKept = 2 })
    await h.doll.ownerSaid("用電腦")
    try await h.turn(Scripted("", useComputer))                                   // turn 1: image turn A (the menu's screenshot)
    try await h.turn(Scripted("", key("cmd+n")))                                  // turn 2: image turn B
    try await h.turn(Scripted("", key("cmd+s"), screenshotCall))                  // turn 3: image turn C, two results with images
    try await h.turn(Scripted("", screenshotCall))                                // turn 4
    let two = await h.model.seen(2), three = await h.model.seen(3)
    // Request 2 holds A and B: both carry images. Request 3 holds A, B, C: A's images are gone, B's
    // and C's stay; A's result keeps its text and says where the images were.
    #expect(two.filter { !$0.images.isEmpty }.count == 2)
    let menu = three.first { $0.content.hasPrefix("電腦選單打開了") }!
    #expect(menu.images.isEmpty && menu.extra.contains(.text(Doll.imagesPutAway)))
    let firstLine = { (m: ChatMessage) in String(m.content.split(separator: "\n").first ?? "") }
    #expect(three.filter { !$0.images.isEmpty }.map(firstLine) == [fakeWindowsLine, fakeWindowsLine, "現在的畫面："])
    // Everything before A's result is byte-identical; A's result is the first difference; the text
    // after it is unchanged.
    let a = two.firstIndex { $0.content.hasPrefix("電腦選單打開了") }!
    #expect(Array(three.prefix(a)) == Array(two.prefix(a)) && three[a] != two[a])
    #expect(three[a].content == two[a].content && three[a + 1...a + 2].map(\.content) == two[a + 1...a + 2].map(\.content))
    // The files stay.
    #expect(try h.events().filter { $0.image != nil }.count == 5)
    // image_turns_kept = 1: in request 3 only C's results carry images.
    let one = try await Harness(tune: { $0.computer.imageTurnsKept = 1 })
    await one.doll.ownerSaid("用電腦")
    try await one.turn(Scripted("", useComputer))
    try await one.turn(Scripted("", key("cmd+n")))
    try await one.turn(Scripted("", key("cmd+s"), screenshotCall))
    try await one.turn(Scripted("", screenshotCall))
    #expect((await one.model.seen(3)).filter { !$0.images.isEmpty }.map(firstLine) == [fakeWindowsLine, "現在的畫面："])
    // The default keeps them all.
    let all = try await Harness()
    #expect(try Config.load(root.appending(path: "dollcore.example.toml")).computer.imageTurnsKept == 0)
    await all.doll.ownerSaid("用電腦")
    try await all.turn(Scripted("", useComputer))
    try await all.turn(Scripted("", key("cmd+n")))
    try await all.turn(Scripted("", key("cmd+s"), screenshotCall))
    try await all.turn(Scripted("", screenshotCall))
    #expect((await all.model.seen(3)).filter { !$0.images.isEmpty }.count == 4)
}

@Test func enteringWhileAScenarioIsOpenIsAnError() async throws {
    let h = try await menuHarness()
    let open = await h.doll.scenario?.startSeq
    try await h.turn(Scripted("", useComputer))
    #expect(try h.toolResults().last!.text == "失敗：電腦選單還開著，不能再打開；先用 outcome(success, summary) 結束這次")
    #expect(h.last.actions == ["error use_computer"])
    #expect(await h.doll.scenario?.startSeq == open && open != nil)
}

@Test func onlyTheLastActionOfAReplyCarriesTheObservation() async throws {
    let h = try await menuHarness()
    // Three actions in one reply: two text-only results, one observation at the end, three steps.
    try await h.turn(Scripted("", ("click", [("text", "Save"), ("button", "right"), ("count", "2")]),
                              ("type", [("text", "sw_vers\n")]), key("cmd+s")))
    var results = Array(try h.toolResults().suffix(3))
    // Her call is in the context; a result says only what is new (2026-11-10).
    #expect(results.map(\.text) == ["", "", fakeWindowsLine])
    #expect(results.map { $0.image != nil } == [false, false, true] && results.map { $0.window != nil } == [false, false, true])
    #expect(h.vm.actions == [.clickText("Save", button: "right", count: 2), .type("sw_vers\n"), .key("cmd+s")])
    #expect(h.last.actions == ["click", "type", "key"])
    #expect(await h.doll.scenario?.steps == ["click「Save」 right 雙擊", "type 「sw_vers\n」", "key cmd+s"])
    // Every other tool between them keeps its result; the last action is the last one written, wherever it is.
    try await h.turn(Scripted("", ("scroll", [("direction", "down"), ("amount", "5")]), screenshotCall,
                              ("drag", [("x0", "1"), ("y0", "2"), ("x1", "3"), ("y1", "4")]), ("click", [("x", "100"), ("y", "200")]), think("high")))
    results = Array(try h.toolResults().suffix(5))
    #expect(results.map { $0.text.components(separatedBy: "\n")[0] } == ["", "現在的畫面：", "", fakeWindowsLine, "下一回合 effort：high"])
    #expect(results.map { $0.image != nil } == [false, true, false, true, false])
    #expect(h.last.actions == ["scroll", "screenshot", "drag", "click", "think high"])
    // A single action is unchanged: its line, the VM's answer, the observation.
    try await h.turn(Scripted("", key("Return")))
    let one = try h.toolResults().last!
    #expect(one.text == fakeWindowsLine && one.image != nil && one.window != nil)
    // Bad arguments alone: a text result, no screenshot, as before.
    try await h.turn(Scripted("", ("click", [("x", "1")]), ("click", [("x", "1"), ("y", "2"), ("text", "A")]),
                              ("scroll", [("direction", "sideways")])))
    #expect(try h.toolResults().suffix(3).map(\.text) == ["失敗：click 的 x 和 y 要一起給",
                                                          "失敗：前面第 1 步（click）失敗了，這一步沒做",
                                                          "失敗：前面第 1 步（click）失敗了，這一步沒做"])
    #expect(try h.toolResults().suffix(3).allSatisfy { $0.image == nil })
    #expect(h.last.actions == ["error click", "skipped click", "skipped scroll"] && h.vm.actions.count == 7)
}

@Test func aFailedActionStopsTheReplysChainAndShowsTheScreen() async throws {
    let h = try await menuHarness()
    let err = "vmpal: Stopped at action 1 of 1 (left_click): No text “Save” on the screen after 3.1 s."
    try await h.turn(Scripted("", key("cmd+shift+s"), ("click", [("text", "Save")]), ("type", [("text", "copy")])))
    #expect(h.vm.actions == [.key("cmd+shift+s"), .clickText("Save", button: "left", count: 1), .type("copy")])
    // Step 1 runs text-only; step 2 fails in the VM with the observation; step 3 is refused, not run.
    h.vm.failNext(err, after: 1)
    try await h.turn(Scripted("", key("cmd+shift+s"), ("click", [("text", "Save")]), ("type", [("text", "copy")]), screenshotCall))
    let results = Array(try h.toolResults().suffix(4))
    #expect(results.map(\.text) == ["",
                                    "失敗：click「Save」\n\(err)\n" + fakeWindowsLine,
                                    "失敗：前面第 2 步（click「Save」）失敗了，這一步沒做",
                                    "現在的畫面：\n" + fakeWindowsLine])
    #expect(results.map { $0.image != nil } == [false, true, false, true])
    #expect(h.vm.actions.count == 5 && h.vm.actions.last == .clickText("Save", button: "left", count: 1))
    #expect(h.last.actions == ["key", "error click", "skipped type", "screenshot"])
    // Only the done actions are steps.
    #expect(await h.doll.scenario?.steps == ["key cmd+shift+s", "click「Save」", "type 「copy」", "key cmd+shift+s"])
    // Bad arguments after a text-only step: the failure shows the screen, since nothing of this reply did yet.
    try await h.turn(Scripted("", key("Return"), ("click", [("x", "99999"), ("y", "1")]), key("Escape")))
    let bad = Array(try h.toolResults().suffix(3))
    #expect(bad[0].text == "" && bad[0].image == nil)
    #expect(bad[1].text.hasPrefix("失敗：沒有點：99999,1 在螢幕外") && bad[1].text.hasSuffix("\n" + fakeWindowsLine) && bad[1].image != nil)
    #expect(bad[2].text == "失敗：前面第 2 步（click）失敗了，這一步沒做" && bad[2].image == nil)
    #expect(h.vm.actions.count == 6)
    // Outcome after actions in the same reply still ends the scenario; the action before it is the last.
    try await h.turn(Scripted("", key("cmd+s"), outcome(true, "存好了"), key("cmd+q")))
    let end = Array(try h.toolResults().suffix(3))
    #expect(end[0].image != nil && end[0].text == fakeWindowsLine)
    #expect(end[1].text.hasPrefix("記下了：達成。") && end[2].text == "失敗：這次用電腦已經用 outcome 結束了；outcome 後面的呼叫沒有執行。")
    #expect(h.last.outcome == "success" && h.last.experience?.hasSuffix("步驟：\n1. key cmd+shift+s\n2. click「Save」\n3. type 「copy」\n4. key cmd+shift+s\n5. key return\n6. key cmd+s") == true)
    #expect(await h.doll.scenario == nil && h.vm.actions.count == 7)
}

@Test func clickTakesExactlyOneOfElementTextOrCoordinates() async throws {
    let h = try await menuHarness()
    try await h.turn(Scripted("", ("click", [("element", "button \"Save\""), ("count", "2")]),
                              ("click", [("element", "notes.txt")]),
                              ("click", [("element", #"{"role": "image", "name": "notes.txt"}"#), ("button", "right")]),
                              ("click", [("element", "\"Open Recent\"")]),
                              ("click", [("text", "Save")]),
                              ("click", [("x", "100"), ("y", "200"), ("on", "screen")])))
    #expect(h.vm.actions == [.clickElement(role: "button", name: "Save", button: "left", count: 2),
                             .clickElement(role: nil, name: "notes.txt", button: "left", count: 1),
                             .clickElement(role: "image", name: "notes.txt", button: "right", count: 1),
                             .clickElement(role: nil, name: "Open Recent", button: "left", count: 1),
                             .clickText("Save", button: "left", count: 1),
                             .click(x: 100, y: 200, button: "left", count: 1)])
    // One bad call per reply: after a failed action the rest of a reply is not run.
    for bad in [("click", []), ("click", [("element", "OK"), ("x", "1"), ("y", "2")]),
                ("click", [("element", "OK"), ("text", "OK")]), ("click", [("text", "OK"), ("on", "window")])] as [(String, [(String, String)])] {
        try await h.turn(Scripted("", bad))
    }
    #expect(try h.toolResults().suffix(4).map(\.text) == ["失敗：click 要給 element、text 或 x 和 y 其中一種",
                                                          "失敗：click 的 element、text、x/y 只能給一種，不要同時給",
                                                          "失敗：click 的 element、text、x/y 只能給一種，不要同時給",
                                                          "失敗：click 的 on 只配 x、y 用"])
    #expect(h.vm.actions.count == 6)
    // Escaped quotes read as quotes (round 6: textfield \"Search\"); a role word alone is a role.
    try await h.turn(Scripted("", ("click", [("element", #"textfield \"Search\""#)]), ("click", [("element", "textfield")])))
    #expect(h.vm.actions.suffix(2) == [.clickElement(role: "textfield", name: "Search", button: "left", count: 1),
                                       .clickElement(role: "textfield", name: nil, button: "left", count: 1)])
}

@Test func anElementTextParsesEscapedQuotesAndARoleAlone() throws {
    let cases: [(String, String?, String?)] = [
        (#"textfield "Search""#, "textfield", "Search"), (#"textfield \"Search\""#, "textfield", "Search"),
        (#"\"Open Recent\""#, nil, "Open Recent"), (#"{\"role\": \"button\", \"name\": \"OK\"}"#, "button", "OK"),
        (#"{"name": "say \"hi\""}"#, nil, #"say "hi""#),
        ("textfield", "textfield", nil), ("TextField", "textfield", nil), (#"{"role": "Button"}"#, "button", nil),
        ("Finder", nil, "Finder"),
    ]
    for (raw, role, name) in cases {
        let got = try Tools.clickElement(raw)
        #expect(got.role == role && got.name == name, "\(raw) → \(String(describing: got))")
    }
    #expect(throws: ToolError.self) { try Tools.clickElement(#"{"role": "", "name": ""}"#) }
}

@Test func windowCoordinatesAreConvertedThroughTheLastCropAndOffImagePointsAreNeverClicked() async throws {
    let h = try await menuHarness()
    // The crop: full-screen (10,20)–(300,200), 290×180 pixels of screen shown as a 580×360 image.
    h.vm.setSizes(screen: (1280, 800), crop: (580, 360))
    try await h.turn(Scripted("", screenshotCall))
    #expect(try h.toolResults().last!.cropNote == "視窗圖：最前面的視窗，在全螢幕 (10,20)–(300,200)，圖 580×360；用這張圖上的座標點要加 on: window。")
    try await h.turn(Scripted("", ("click", [("x", "100"), ("y", "50"), ("on", "window"), ("count", "2")])))
    #expect(h.vm.actions == [.click(x: 60, y: 45, button: "left", count: 2)])
    let r = try h.toolResults().last!
    #expect(r.text == "視窗圖 100,50 → 全螢幕 60,45\n" + fakeWindowsLine && r.image != nil)
    // Off the crop, off the screen: an error, nothing clicked (one per reply: a failed action ends the reply's chain).
    for bad in [[("x", "580"), ("y", "10"), ("on", "window")], [("x", "640"), ("y", "800")], [("x", "-1"), ("y", "5")]] {
        try await h.turn(Scripted("", ("click", bad)))
        #expect(h.last.actions == ["error click"])
    }
    #expect(try h.toolResults().suffix(3).map(\.text) == ["失敗：沒有點：580,10 不在視窗圖裡（視窗圖 580×360）",
                                                          "失敗：沒有點：640,800 在螢幕外（全螢幕截圖 1280×800）",
                                                          "失敗：沒有點：-1,5 在螢幕外（全螢幕截圖 1280×800）"])
    #expect(h.vm.actions.count == 1)
    // The last result had no crop: window coordinates cannot be converted.
    h.vm.failWindow("WINDOW_NOT_FOUND: No focused window found")
    try await h.turn(Scripted("", screenshotCall, ("click", [("x", "10"), ("y", "10"), ("on", "window")])))
    #expect(try h.toolResults().last!.text == "失敗：沒有點：最近的結果沒有視窗圖，on: window 換算不了；用全螢幕截圖的座標")
    #expect(h.vm.actions.count == 1)
}

@Test func imageSizeReadsPNGAndJPEGHeaders() {
    #expect(ImageSize.of(pngHeader(1280, 609)).map { [$0.width, $0.height] } == [1280, 609])
    // SOI, an APP0 segment, then SOF0: precision, height, width.
    let jpeg = Data([0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x04, 0x4A, 0x46, 0xFF, 0xC0, 0x00, 0x11, 0x08, 0x02, 0x61, 0x05, 0x00, 0x03,
                     0x01, 0x22, 0x00, 0x02, 0x11, 0x01, 0x03, 0x11, 0x01])
    #expect(ImageSize.of(jpeg).map { [$0.width, $0.height] } == [1280, 609])
    #expect(ImageSize.of(Data("frame 0 full".utf8)) == nil)
}

/// agent-desktop in the VM for `click(element:)`, answering by command as 2.6 did in the Doll VM
/// (2026-11-09, Finder's Documents window, answers cut to the fields read). Records the scripts.
/// The click script's shell is played here: the accessibility press answers `press`; only when that
/// says not_delivered does the `--headed` press run and answer `headed`.
private final class FakeAgentDesktop: Sendable {
    let calls = Mutex<[[String]]>([])
    let found: String
    let windows: String
    let press: String, headed: String
    init(found: [(role: String, name: String, ref: String)], focused: Bool = true,
         press: String = FakeAgentDesktop.clicked, headed: String = FakeAgentDesktop.clicked) {
        let ms = found.map { #"{"interactive":true,"name":"\#($0.name)","ref_id":"\#($0.ref)","role":"\#($0.role)","states":[],"value":null}"# }
        self.found = #"{"version":"2.6","ok":true,"command":"find","data":{"matches":["# + ms.joined(separator: ",") + #"],"total_matches":\#(ms.count)}}"#
        windows = #"{"version":"2.6","ok":true,"command":"list-windows","data":["#
            + #"{"app_name":"Calendar","id":"w-1","is_focused":false,"title":"Calendar","visible":true},"#
            + #"{"app_name":"Finder","id":"w-2","is_focused":\#(focused),"title":"Finder","visible":true}]}"#
        (self.press, self.headed) = (press, headed)
    }
    static let displays = #"{"version":"2.6","ok":true,"command":"list-displays","data":[{"bounds":{"height":900.0,"width":1440.0,"x":0.0,"y":0.0},"id":"1","is_primary":true,"scale":2.0}]}"#
    /// Every ref's bounds: e1 at (465,186) 64×64 points, e2 at (260,301) 171×32, e3 at (500,120) 160×20,
    /// e4 same as e2 (a cell inside its row). Any other ref's bounds cannot be read.
    static let places = ["@s:e1": (465.0, 186.0, 64.0, 64.0), "@s:e2": (260.0, 301.0, 171.0, 32.0),
                         "@s:e3": (500.0, 120.0, 160.0, 20.0), "@s:e4": (260.0, 301.0, 171.0, 32.0)]
    static func bounds(_ ref: String) -> String {
        guard let p = places[ref] else {
            return #"{"version":"2.6","ok":false,"command":"get","error":{"code":"STALE_REF","message":"gone"}}"#
        }
        return #"{"version":"2.6","ok":true,"command":"get","data":{"property":"bounds","ref":"\#(ref)","value":{"height":\#(p.3),"width":\#(p.2),"x":\#(p.0),"y":\#(p.1)}}}"#
    }
    static let snapshot = #"{"version":"2.6","ok":true,"command":"snapshot","data":{"app":"Finder","tree":{"role":"window","name":"Documents","bounds":{"height":436.0,"width":920.0,"x":260.0,"y":120.0},"children":["#
        + #"{"role":"treeitem","name":"Recents","ref_id":"@s:e5","bounds":{"height":32.0,"width":171.0,"x":260.0,"y":172.0}},"#
        + #"{"role":"image","name":"notes.txt","ref_id":"@s:e1","bounds":{"height":64.0,"width":64.0,"x":465.0,"y":186.0}}]}}}"#
    /// With no window (2026-11-09, Finder frontmost and windowless): the app menus cannot be read; the
    /// menu bar extras and the Dock can. Dock icons 54×70 points from x 48, the extras in the top row.
    static let surfaces = "@@menubar\n"
        + #"{"version":"2.6","ok":false,"command":"snapshot","error":{"code":"WINDOW_NOT_FOUND","message":"No window found to identify the application owning surface 'menubar'"}}"#
        + "\n@@menu-bar-extras\n"
        + #"{"version":"2.6","ok":true,"command":"snapshot","data":{"app":"SystemUIServer","tree":{"bounds":{"height":0.0,"width":0.0,"x":0.0,"y":0.0},"children":["#
        + #"{"role":"menuitem","name":"Text Input","ref_id":"@m:e1","bounds":{"height":24.0,"width":30.0,"x":1250.0,"y":0.0}},"#
        + #"{"role":"menuitem","name":"Clock","ref_id":"@m:e2","bounds":{"height":24.0,"width":120.0,"x":1300.0,"y":0.0}}]}}}"#
        + "\n@@dock\n"
        + #"{"version":"2.6","ok":true,"command":"snapshot","data":{"app":"Dock","tree":{"bounds":{"height":0.0,"width":0.0,"x":0.0,"y":0.0},"children":[{"role":"list","ref_id":"@d:e1","bounds":{"height":70.0,"width":1360.0,"x":40.0,"y":820.0},"children":["#
        + #"{"role":"dockitem","name":"Finder","ref_id":"@d:e2","bounds":{"height":70.0,"width":54.0,"x":48.0,"y":825.0}},"#
        + #"{"role":"dockitem","name":"TextEdit","ref_id":"@d:e3","bounds":{"height":70.0,"width":54.0,"x":102.0,"y":825.0}}]}]}}}"#
    static let clicked = #"{"version":"2.6","ok":true,"command":"click","data":{"action":"click","disposition":{"delivery":"delivered_unverified","retry":"unsafe"},"steps":[]}}"#
    /// A Finder file icon's double-click without --headed (2026-11-09, cut).
    static let denied = #"{"version":"2.6","ok":false,"command":"double-click","error":{"code":"POLICY_DENIED","message":"Target is not actionable: supported_action (semantic gesture is unavailable and physical fallback is denied)","disposition":{"delivery":"not_delivered","retry":"safe"}}}"#
    /// A sheet's Save with --headed (2026-11-09).
    static let noWindow = #"{"version":"2.6","ok":false,"command":"click","error":{"code":"ACTION_FAILED","message":"Physical input target has no verified owning window","details":{"physical_delivery_started":false},"disposition":{"delivery":"not_delivered","retry":"safe"}}}"#

    /// `vmpal ui …` calls (the settle after an element click), kept apart from the exec scripts.
    let uiCalls = Mutex<[[String]]>([])

    func run(_ args: [String]) throws -> String {
        if args.first == "ui" { uiCalls.withLock { $0.append(args) }; return "" }
        calls.withLock { $0.append(args) }
        let script = args[5], a = Array(args.dropFirst(8))   // exec <vm> -- /bin/sh -c <script> sh <agent-desktop> <args…>
        if script.contains("list-windows") { return windows + "\n@@displays\n" + Self.displays }
        if script.contains(" find ") { return found }
        if script.contains("snapshot --window-id") { return Self.snapshot }
        if script.contains("snapshot --surface") { return Self.surfaces }
        if script.contains("@@semantic") {
            return Self.bounds(a[0]) + "\n@@semantic\n" + press
                + (press.contains(#""delivery":"not_delivered""#) ? "\n@@headed\n" + headed : "")
        }
        if script.contains("for r in") { return a.map { "@@\($0)\n" + Self.bounds($0) }.joined(separator: "\n") }
        throw ComputerError("unexpected script \(script)")
    }
    var scripts: [String] { calls.withLock { $0.map { $0[5] } } }
}

@Test func anElementIsFoundFreshInTheFrontWindowAndClickedOnlyWhenItIsTheOneMatch() async throws {
    var cfg = ComputerConfig()
    cfg.vmID = "E6813EBA"
    // One match: found in the focused window, pressed through accessibility with its fresh ref, said
    // with its center in full-screenshot pixels ((465 + 32) × 1280/1440, (186 + 32) × 1280/1440),
    // the way it went and the disposition.
    let one = FakeAgentDesktop(found: [("image", "notes.txt", "@s:e1")])
    let said = try await VMPalDriver(cfg, vmpal: { try one.run($0) }).act(.clickElement(role: nil, name: "notes.txt", button: "left", count: 2))
    #expect(said == "")
    let calls = one.calls.withLock { $0 }
    #expect(calls.count == 3 && calls.allSatisfy { Array($0.prefix(5)) == ["exec", "E6813EBA", "--", "/bin/sh", "-c"] })
    #expect(Array(calls[1].suffix(5)) == ["sh", cfg.agentDesktop, "w-2", "--name", "notes.txt"]
            && calls[1][5].contains("find --window-id \"$2\" \"$3\" \"$4\""))
    // The press first; `--headed` only in the branch for agent-desktop's not_delivered.
    #expect(calls[2][5].contains(#"a=$("$1" double-click "$2")"#) && calls[2].last == "@s:e1")
    #expect(calls[2][5].contains(#"*'"delivery":"not_delivered"'*) echo @@headed; "$1" double-click --headed "$2";;"#))
    // Then, like every other action, the screen settles before the screenshot is taken.
    #expect(one.uiCalls.withLock { $0 }.map { Array($0.dropFirst(2)) } == [["wait", "--settled", "--observe", "none"]])

    // Exact name beats a name that only contains it; the role narrows.
    let exact = FakeAgentDesktop(found: [("statictext", "notes.txt backup", "@s:e3"), ("image", "Notes.txt", "@s:e1")])
    #expect(try await VMPalDriver(cfg, vmpal: { try exact.run($0) }).act(.clickElement(role: nil, name: "notes.txt", button: "left", count: 1))
                == "")
    #expect(exact.scripts.last!.contains(#"a=$("$1" click "$2")"#) && exact.calls.withLock { $0.last!.last } == "@s:e1")

    // Several: no click; each candidate with its role, name and center. The cell at its row's exact
    // spot is the same target and is not listed twice.
    let many = FakeAgentDesktop(found: [("treeitem", "Documents", "@s:e2"), ("cell", "Documents", "@s:e4"), ("statictext", "Documents", "@s:e3")])
    do {
        _ = try await VMPalDriver(cfg, vmpal: { try many.run($0) }).act(.clickElement(role: nil, name: "documents", button: "left", count: 1))
        Issue.record("an ambiguous element was clicked")
    } catch let e as ComputerError {
        #expect(e.description == """
            沒有點：最前面的視窗（Finder）裡有 2 個符合「documents」，不知道是哪一個。要點的那個用它的座標 click x、y（全螢幕截圖座標）：
            treeitem "Documents" (307,282)
            statictext "Documents" (516,116)
            """)
    }
    #expect(!many.scripts.contains { $0.contains("@@semantic") })
    // The same row and cell alone are one target: clicked.
    let same = FakeAgentDesktop(found: [("treeitem", "Documents", "@s:e2"), ("cell", "Documents", "@s:e4")])
    #expect(try await VMPalDriver(cfg, vmpal: { try same.run($0) }).act(.clickElement(role: nil, name: "Documents", button: "right", count: 1))
                == "")
    #expect(same.scripts.last!.contains(#"a=$("$1" right-click "$2")"#) && same.calls.withLock { $0.last!.last } == "@s:e2")

    // None: no click; the window's similar names with their centers.
    let none = FakeAgentDesktop(found: [])
    do {
        _ = try await VMPalDriver(cfg, vmpal: { try none.run($0) }).act(.clickElement(role: nil, name: "nots.txt", button: "left", count: 1))
        Issue.record("a missing element was clicked")
    } catch let e as ComputerError {
        #expect(e.description == "沒有點：最前面的視窗（Finder）裡沒有「nots.txt」。名字相近的（全螢幕截圖座標）：\nimage \"notes.txt\" (442,194)")
    }
    #expect(!none.scripts.contains { $0.contains("@@semantic") })
    // The name fits but not the role: those are listed.
    let role = FakeAgentDesktop(found: [("image", "notes.txt", "@s:e1")])
    do {
        _ = try await VMPalDriver(cfg, vmpal: { try role.run($0) }).act(.clickElement(role: "button", name: "notes.txt", button: "left", count: 1))
        Issue.record("an element of another role was clicked")
    } catch let e as ComputerError {
        #expect(e.description == "沒有點：最前面的視窗（Finder）裡沒有button「notes.txt」；名字對、角色不同的有（全螢幕截圖座標）：\nimage \"notes.txt\" (442,194)")
    }
}

@Test func aRoleAloneIsMatchedByRole() async throws {
    var cfg = ComputerConfig()
    cfg.vmID = "E6813EBA"
    // One: `find --role` in the focused window; it has no name and is said by its role.
    let one = FakeAgentDesktop(found: [("textfield", "", "@s:e1")])
    #expect(try await VMPalDriver(cfg, vmpal: { try one.run($0) }).act(.clickElement(role: "textfield", name: nil, button: "left", count: 1))
                == "")
    #expect(Array(one.calls.withLock { $0[1] }.suffix(4)) == [cfg.agentDesktop, "w-2", "--role", "textfield"])
    // Several: no click, each with its center.
    let two = FakeAgentDesktop(found: [("textfield", "", "@s:e1"), ("textfield", "Save As:", "@s:e3")])
    do {
        _ = try await VMPalDriver(cfg, vmpal: { try two.run($0) }).act(.clickElement(role: "textfield", name: nil, button: "left", count: 1))
        Issue.record("an ambiguous role was clicked")
    } catch let e as ComputerError {
        #expect(e.description == """
            沒有點：最前面的視窗（Finder）裡有 2 個符合textfield，不知道是哪一個。要點的那個用它的座標 click x、y（全螢幕截圖座標）：
            textfield (442,194)
            textfield "Save As:" (516,116)
            """)
    }
    #expect(!two.scripts.contains { $0.contains("@@semantic") })
    // None: no click.
    let none = FakeAgentDesktop(found: [])
    do {
        _ = try await VMPalDriver(cfg, vmpal: { try none.run($0) }).act(.clickElement(role: "textfield", name: nil, button: "left", count: 1))
        Issue.record("a missing role was clicked")
    } catch let e as ComputerError {
        #expect(e.description == "沒有點：最前面的視窗（Finder）裡沒有textfield。")
    }
    #expect(!none.scripts.contains { $0.contains("@@semantic") })
}

@Test func withNoFrontWindowAnElementIsSearchedInTheDockAndTheMenuBar() async throws {
    var cfg = ComputerConfig()
    cfg.vmID = "E6813EBA"
    func driver(_ fake: FakeAgentDesktop) -> VMPalDriver { VMPalDriver(cfg, vmpal: { try fake.run($0) }) }
    // One: the Dock's Finder from the surfaces' snapshot (no `find`), its center from that snapshot
    // when its bounds cannot be read again: (48 + 27, 825 + 35) × 1280/1440.
    let finder = FakeAgentDesktop(found: [], focused: false)
    #expect(try await driver(finder).act(.clickElement(role: nil, name: "finder", button: "left", count: 1))
            == "")
    let scripts = finder.scripts
    #expect(scripts.count == 3 && scripts[1].contains("menubar menu-bar-extras dock") && !scripts.contains { $0.contains(" find ") })
    #expect(finder.calls.withLock { $0[2].last } == "@d:e2")
    // The role narrows, the same way as in a window.
    let clock = FakeAgentDesktop(found: [], focused: false)
    #expect(try await driver(clock).act(.clickElement(role: "menuitem", name: "clock", button: "left", count: 1))
                == "")
    #expect(clock.calls.withLock { $0[2].last } == "@m:e2")
    // Several: no click; candidates from the menu bar and the Dock with their centers.
    let text = FakeAgentDesktop(found: [], focused: false)
    do {
        _ = try await driver(text).act(.clickElement(role: nil, name: "text", button: "left", count: 1))
        Issue.record("an ambiguous Dock item was clicked")
    } catch let e as ComputerError {
        #expect(e.description == """
            沒有點：Dock 和選單列（現在沒有最前面的視窗）裡有 2 個符合「text」，不知道是哪一個。要點的那個用它的座標 click x、y（全螢幕截圖座標）：
            menuitem "Text Input" (1124,11)
            dockitem "TextEdit" (115,764)
            """)
    }
    #expect(!text.scripts.contains { $0.contains("@@semantic") })
    // A role alone: the Dock's two items.
    do {
        _ = try await driver(FakeAgentDesktop(found: [], focused: false)).act(.clickElement(role: "dockitem", name: nil, button: "left", count: 1))
        Issue.record("an ambiguous role was clicked")
    } catch let e as ComputerError {
        #expect(e.description.hasSuffix("有 2 個符合dockitem，不知道是哪一個。要點的那個用它的座標 click x、y（全螢幕截圖座標）：\ndockitem \"Finder\" (67,764)\ndockitem \"TextEdit\" (115,764)"))
    }
    // None: near names from the same items.
    let typo = FakeAgentDesktop(found: [], focused: false)
    do {
        _ = try await driver(typo).act(.clickElement(role: nil, name: "Findr", button: "left", count: 1))
        Issue.record("a missing Dock item was clicked")
    } catch let e as ComputerError {
        #expect(e.description == "沒有點：Dock 和選單列（現在沒有最前面的視窗）裡沒有「Findr」。名字相近的（全螢幕截圖座標）：\ndockitem \"Finder\" (67,764)")
    }
    #expect(typo.scripts.count == 2)
}

@Test func aPressNotDeliveredIsRetriedWithRealPointerInputAndTheResultSaysWhichWayWent() async throws {
    var cfg = ComputerConfig()
    cfg.vmID = "E6813EBA"
    let denied = "POLICY_DENIED: Target is not actionable: supported_action (semantic gesture is unavailable and physical fallback is denied)"
    // A Finder icon's double-click: the press is not delivered, the pointer is.
    let icon = FakeAgentDesktop(found: [("image", "notes.txt", "@s:e1")], press: FakeAgentDesktop.denied)
    #expect(try await VMPalDriver(cfg, vmpal: { try icon.run($0) }).act(.clickElement(role: nil, name: "notes.txt", button: "left", count: 2))
            == "無障礙按壓沒送出，改用實體滑鼠。")
    #expect(icon.calls.withLock { $0.count } == 3)
    // Neither way: an error with both answers.
    let neither = FakeAgentDesktop(found: [("image", "notes.txt", "@s:e1")], press: FakeAgentDesktop.denied, headed: FakeAgentDesktop.noWindow)
    do {
        _ = try await VMPalDriver(cfg, vmpal: { try neither.run($0) }).act(.clickElement(role: nil, name: "notes.txt", button: "left", count: 2))
        Issue.record("an undelivered click was a success")
    } catch let e as ComputerError {
        #expect(e.description == "沒有點成 image \"notes.txt\" (442,194)：無障礙按壓沒送出（agent-desktop：\(denied)；delivery=not_delivered, retry=safe），"
                + "實體滑鼠（headed）也沒有（agent-desktop：ACTION_FAILED: Physical input target has no verified owning window；delivery=not_delivered, retry=safe）。")
    }
    // A press that failed after it was delivered is not pressed again.
    let failed = #"{"version":"2.6","ok":false,"command":"click","error":{"code":"ACTION_FAILED","message":"press went wrong","disposition":{"delivery":"delivered_unverified","retry":"unsafe"}}}"#
    let once = FakeAgentDesktop(found: [("button", "Save", "@s:e3")], press: failed, headed: FakeAgentDesktop.denied)
    do {
        _ = try await VMPalDriver(cfg, vmpal: { try once.run($0) }).act(.clickElement(role: "button", name: "Save", button: "left", count: 1))
        Issue.record("a failed press was a success")
    } catch let e as ComputerError {
        #expect(e.description == "click button \"Save\" (516,116) 失敗（無障礙按壓）：ACTION_FAILED: press went wrong（agent-desktop：delivery=delivered_unverified, retry=unsafe）")
    }
}

@Test func aVMPalFailureIsAFailedResultNeverASuccess() async throws {
    let h = try await menuHarness()
    let err = "vmpal: Stopped at action 1 of 1 (left_click): No text “紫色按鈕” on the screen after 3.1 s."
    h.vm.failNext(err)
    try await h.turn(Scripted("", ("click", [("text", "紫色按鈕")])))
    let r = try h.toolResults().last!
    #expect(r.text == "失敗：click「紫色按鈕」\n\(err)\n" + fakeWindowsLine)
    #expect(r.image != nil && h.last.actions == ["error click"])
}

@Test func theVMPalDriverTurnsANonZeroExitIntoAnErrorWithItsText() async throws {
    let dir = FileManager.default.temporaryDirectory.appending(path: "dollcore-vmpal-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    let script = dir.appending(path: "vmpal")
    try "#!/bin/sh\nif [ \"$4\" = click ]; then echo 'vmpal: No text “紫色按鈕” on the screen' >&2; exit 1; fi\necho \"Done: $*\"\n"
        .write(to: script, atomically: true, encoding: .utf8)
    try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: script.path)
    var cfg = ComputerConfig()
    cfg.vmID = "E6813EBA"
    cfg.vmpal = script.path
    let d = VMPalDriver(cfg)
    do {
        _ = try await d.act(.clickText("紫色按鈕", button: "left", count: 1))
        Issue.record("a failed vmpal call came back as a success")
    } catch let e as ComputerError {
        #expect(e.description == "vmpal: No text “紫色按鈕” on the screen")
    }
    #expect(try await d.act(.key("cmd+s")) == "Done: --cli ui E6813EBA key cmd+s --observe none")
}

@Test func everyComputerResultIsFullScreenThenWindowCropThenOutlineStoredVerbatim() async throws {
    let h = try await menuHarness()
    h.vm.setFrame(1)
    try await h.turn(Scripted("", key("cmd+n"), screenshotCall))
    let results = try h.toolResults()                                  // use_computer's, key's, screenshot's
    #expect(results.count == 3 && results.allSatisfy { $0.image != nil && $0.crop != nil })
    let note = "視窗圖：最前面的視窗，在全螢幕 (10,20)–(300,200)；用這張圖上的座標點要加 on: window。"
    #expect(results.allSatisfy { $0.cropNote == note })
    let shot = { (f: String) in String(decoding: try Data(contentsOf: h.dir.appending(path: "shots/\(f)")), as: UTF8.self) }
    #expect(try results.map { try shot($0.image!) } == ["frame 0 full", "frame 1 full", "frame 1 full"])
    #expect(try results.map { try shot($0.crop!) }
            == ["frame 0 crop 10,20,300,200", "frame 1 crop 10,20,300,200", "frame 1 crop 10,20,300,200"])
    #expect(results[0].window == "前景 app：Fake，視窗標題「frame 0」\nwindow \"frame 0\"\n  button \"OK\"")
    #expect(results[1].window == "前景 app：Fake，視窗標題「frame 1」\nwindow \"frame 1\"\n  button \"OK\"")
    #expect(results[2].window == results[1].window)
    #expect(h.last.actions == ["key", "screenshot"])
    // The screen moves on; later requests still send the stored images and lines, byte for byte.
    h.vm.setFrame(2)
    try await h.turn("好")
    try await h.turn("嗯")
    let earlier = await h.model.seen(2), later = await h.model.seen(3)
    #expect(try ChatMessage.encoder.encode(Array(later.prefix(earlier.count))) == ChatMessage.encoder.encode(earlier))
    let message = try #require(earlier.last { $0.role == "tool" })
    let json = try JSONSerialization.jsonObject(with: ChatMessage.encoder.encode(message)) as! [String: Any]
    let parts = json["content"] as! [[String: Any]]
    #expect(parts.map { $0["type"] as! String } == ["text", "image_url", "text", "image_url", "text"])
    let uri = { (f: String) in "data:image/jpeg;base64," + Data(try shot(f).utf8).base64EncodedString() }
    #expect((parts[1]["image_url"] as? [String: String])?["url"] == (try uri(results[2].image!)))
    #expect(parts[2]["text"] as? String == "\n" + note)
    #expect((parts[3]["image_url"] as? [String: String])?["url"] == (try uri(results[2].crop!)))
    #expect(parts[4]["text"] as? String == "\n" + results[2].window!)
}

@Test func noFrontWindowMeansNoCropButTheMenuBarAndDockStoredVerbatim() async throws {
    let h = try await menuHarness()
    h.vm.failWindow("WINDOW_NOT_FOUND: No focused window found")
    try await h.turn(Scripted("", key("Escape")))
    let r = try h.toolResults().last!
    #expect(r.text == fakeWindowsLine && r.image != nil)
    #expect(r.crop == nil && r.cropNote == nil)
    let desktop = "沒有視窗圖和視窗概要：WINDOW_NOT_FOUND: No focused window found\n"
        + "Dock 和選單列（項目中心，全螢幕截圖座標）：\ndockitem \"Finder\" (67,765)\nmenuitem \"Clock\" (1205,13)"
    #expect(r.window == desktop)
    #expect(h.last.actions == ["key", "error window"])
    #expect(h.logs.contains { $0.contains("window outline failed") })
    // Re-sent byte-identical on the next turn, as the last text part.
    try await h.turn("嗯")
    let sent = try #require(await h.model.seen(2).last { $0.role == "tool" })
    let sentJSON = try JSONSerialization.jsonObject(with: ChatMessage.encoder.encode(sent)) as! [String: Any]
    #expect((sentJSON["content"] as! [[String: Any]]).last?["text"] as? String == "\n" + desktop)
    // The summary failing too: the result says so.
    h.vm.failDesktop("agent-desktop: not found")
    try await h.turn(Scripted("", key("Escape")))
    #expect(try h.toolResults().last!.window
            == "沒有視窗圖和視窗概要：WINDOW_NOT_FOUND: No focused window found\nDock 和選單列概要失敗：agent-desktop: not found")
    #expect(h.last.actions == ["key", "error window", "error desktop"])
    h.vm.failDesktop(nil)
    // A window with no place to crop: the outline still comes, the crop's line says why.
    h.vm.failWindow(nil)
    h.vm.setPlace(.unknown("最前面的視窗不在螢幕上"))
    try await h.turn(Scripted("", screenshotCall))
    let s = try h.toolResults().last!
    #expect(s.crop == nil && s.cropNote == "沒有視窗圖：最前面的視窗不在螢幕上" && s.window!.hasPrefix("前景 app：Fake"))
    try await h.turn("嗯")
    let message = try #require(await h.model.seen(5).last { $0.role == "tool" })
    let json = try JSONSerialization.jsonObject(with: ChatMessage.encoder.encode(message)) as! [String: Any]
    #expect((json["content"] as! [[String: Any]]).map { $0["type"] as! String } == ["text", "image_url", "text", "text"])
}

@Test func theMenuBarAndDockAreOneLinePerItemWithItsCenterInScreenshotPixels() async throws {
    // Answers as agent-desktop 2.6 gave them in the Doll VM (2026-11-09, Finder frontmost, no window),
    // cut to a few items.
    let menubar = #"{"version":"2.6","ok":false,"command":"snapshot","error":{"code":"WINDOW_NOT_FOUND","message":"No window found to identify the application owning surface 'menubar'"}}"#
    let extras = #"{"version":"2.6","ok":true,"command":"snapshot","data":{"app":"MenuBarAgent","tree":{"bounds":{"height":30.0,"width":170.0,"x":1250.0,"y":0.0},"children":["#
        + #"{"bounds":{"height":30.0,"width":26.0,"x":1250.0,"y":0.0},"role":"group","children":[{"bounds":{"height":22.0,"width":26.0,"x":1250.0,"y":4.0},"name":"Control Center","ref_id":"@s:e1","role":"menuitem"}]},"#
        + #"{"bounds":{"height":30.0,"width":128.0,"x":1292.0,"y":0.0},"role":"group","children":[{"bounds":{"height":22.0,"width":128.0,"x":1292.0,"y":4.0},"name":"Clock","ref_id":"@s:e2","role":"menuitem"}]}]}}}"#
    let dock = #"{"version":"2.6","ok":true,"command":"snapshot","data":{"app":"Dock","tree":{"bounds":{"height":0.0,"width":0.0,"x":0.0,"y":0.0},"role":"application","name":"Dock","children":["#
        + #"{"bounds":{"height":70.0,"width":1360.0,"x":40.0,"y":820.0},"role":"list","children":["#
        + #"{"bounds":{"height":69.9776611328125,"width":53.977691650390625,"x":47.97772216796875,"y":825.0223388671875},"name":"Finder","role":"dockitem"},"#
        + #"{"bounds":{"height":69.9776611328125,"width":24.0,"x":1073.0,"y":825.0223388671875},"role":"separator"},"#
        + #"{"bounds":{"height":69.9776611328125,"width":53.977691650390625,"x":1205.0,"y":825.0223388671875},"name":"Terminal","role":"dockitem"}]}]}}}"#
    let displays = #"{"version":"2.6","ok":true,"command":"list-displays","data":[{"bounds":{"height":900.0,"width":1440.0,"x":0.0,"y":0.0},"id":"1","is_primary":true,"scale":2.0}]}"#
    var cfg = ComputerConfig()
    cfg.vmID = "E6813EBA"
    let seen = Mutex<[[String]]>([])
    let driver = VMPalDriver(cfg, vmpal: { args in
        seen.withLock { $0.append(args) }
        return ["@@menubar", menubar, "@@menu-bar-extras", extras, "@@dock", dock, "@@displays", displays].joined(separator: "\n")
    })
    // One `vmpal exec`: agent-desktop's path as $1. Centers: (x + width/2) × 1280/1440, rounded.
    #expect(try await driver.desktopSummary() == """
        Dock 和選單列（項目中心，全螢幕截圖座標）：
        選單列讀不到：WINDOW_NOT_FOUND: No window found to identify the application owning surface 'menubar'
        menuitem "Control Center" (1123,13)
        menuitem "Clock" (1205,13)
        dockitem "Finder" (67,764)
        dockitem "Terminal" (1095,764)
        """)
    let calls = seen.withLock { $0 }
    #expect(calls.count == 1)
    let call = try #require(calls.first)
    #expect(Array(call.prefix(5)) == ["exec", "E6813EBA", "--", "/bin/sh", "-c"] && call.suffix(2) == ["sh", cfg.agentDesktop])
    #expect(call[5].contains("for s in menubar menu-bar-extras dock;") && call[5].contains("\"$1\" list-displays"))
    // agent-desktop missing or no screen: the whole summary fails with the VM's text.
    let bad = VMPalDriver(cfg, vmpal: { _ in throw ComputerError("sh: agent-desktop: not found") })
    await #expect(throws: ComputerError.self) { try await bad.desktopSummary() }
}

@Test func theWindowOutlineIsOneLinePerElementWithoutRefsAndItsBoundsPlaceTheCrop() async throws {
    let ok = #"{"version":"2.6","ok":true,"command":"snapshot","data":{"app":"Safari","ref_count":2,"snapshot_id":"s1","#
        + #""window":{"id":"w-1","title":"Safari"},"tree":{"name":"Apple","role":"window","#
        + #""bounds":{"height":499.0,"width":860.0,"x":40.0,"y":54.0},"children":["#
        + #"{"role":"group","bounds":{"height":10.0,"width":10.0,"x":0.0,"y":0.0},"#
        + #""children":[{"role":"scrollarea","children_count":2,"ref_id":"@s1:e1"}]},"#
        + #"{"role":"button","name":"Go back","ref_id":"@s1:e2","states":["disabled"]}]}}}"#
    let displays = #"{"version":"2.6","ok":true,"command":"list-displays","data":[{"bounds":{"height":900.0,"width":1440.0,"x":0.0,"y":0.0},"id":"1","is_primary":true,"scale":2.0}]}"#
    let want = "前景 app：Safari，視窗標題「Apple」\nwindow \"Apple\"\n  group\n    scrollarea (2)\n  button \"Go back\""
    let err = #"{"version":"2.6","ok":false,"command":"snapshot","error":{"code":"WINDOW_NOT_FOUND","message":"No focused window found"}}"#
    // Through the driver: agent-desktop in the VM via `vmpal exec`, its path as $1; a 1440×900-point
    // screen in a 1280-wide screenshot.
    var cfg = ComputerConfig()
    cfg.vmID = "E6813EBA"
    cfg.jpegQuality = 0   // VMPal's bytes as they are; re-encoding has its own test
    let seen = Mutex<[[String]]>([])
    let good = VMPalDriver(cfg, vmpal: { args in
        seen.withLock { $0.append(args) }
        if args[0] == "ui" { try Data("png".utf8).write(to: URL(filePath: args[4])); return "Image written." }
        return args[5].contains("list-displays") ? displays : ok
    })
    #expect(try await good.frontWindow() == FrontWindow(summary: want, place: .at(Region(35, 48, 800, 492))))
    #expect(seen.withLock { $0 } == [
        ["exec", "E6813EBA", "--", "/bin/sh", "-c", "\"$1\" snapshot --skeleton -i --compact --include-bounds", "sh", cfg.agentDesktop],
        ["exec", "E6813EBA", "--", "/bin/sh", "-c", "\"$1\" list-displays", "sh", cfg.agentDesktop],
    ])
    // The crop: her full-screen pixels, VMPal's 1280-wide space, at most the crop width.
    seen.withLock { $0 = [] }
    #expect(try await good.windowShot(Region(35, 48, 800, 492)) == Data("png".utf8))
    let shot = try #require(seen.withLock { $0 }.first)
    #expect(Array(shot.prefix(4)) == ["ui", "E6813EBA", "screenshot", "-o"]
            && Array(shot.dropFirst(5)) == ["--region", "35,48,800,492", "--max-width", "1280"])
    // Clipped to the screen; a window wholly off it has no place.
    let screen = WindowSummary.Rect(x: 0, y: 0, width: 1440, height: 900)
    #expect(WindowSummary.place(.init(x: -100, y: 800, width: 400, height: 300), on: screen, width: 1280) == .at(Region(0, 711, 267, 800)))
    #expect(WindowSummary.place(.init(x: 1500, y: 0, width: 100, height: 100), on: screen, width: 1280) == .unknown("最前面的視窗不在螢幕上"))
    let bad = VMPalDriver(cfg, vmpal: { _ in throw ComputerError(err) })   // agent-desktop exits 1 with its JSON answer
    do {
        _ = try await bad.frontWindow()
        Issue.record("a failed outline came back as a success")
    } catch let e as ComputerError {
        #expect(e.description == "WINDOW_NOT_FOUND: No focused window found")
    }
}

/// A JPEG screenshot is re-encoded at the configured quality: same pixel size, a valid and smaller
/// file, the same bytes every time for the same input (old screenshots are re-sent byte-identical);
/// quality 0 keeps VMPal's bytes.
@Test func jpegScreenshotsAreReencodedAtTheConfiguredQuality() throws {
    // A 320×200 photo-like image (noise) saved at maximum quality, like VMPal's own file.
    let w = 320, h = 200
    var pixels = [UInt8](repeating: 0, count: w * h * 4)
    var seed: UInt32 = 7
    for i in 0..<pixels.count { seed = seed &* 1_664_525 &+ 1_013_904_223; pixels[i] = UInt8(truncatingIfNeeded: seed >> 24) }
    let ctx = try #require(CGContext(data: &pixels, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w * 4,
                                     space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue))
    let image = try #require(ctx.makeImage())
    let original = NSMutableData()
    let dst = try #require(CGImageDestinationCreateWithData(original, "public.jpeg" as CFString, 1, nil))
    CGImageDestinationAddImage(dst, image, [kCGImageDestinationLossyCompressionQuality: 1.0] as CFDictionary)
    #expect(CGImageDestinationFinalize(dst))
    let vmpal = original as Data

    let smaller = try VMPalDriver.reencodeJPEG(vmpal, quality: 40)
    #expect(smaller.count < vmpal.count / 2)
    let size = try #require(ImageSize.of(smaller))
    #expect(size.width == w && size.height == h)
    #expect(try VMPalDriver.reencodeJPEG(vmpal, quality: 40) == smaller)
    #expect(try VMPalDriver.reencodeJPEG(vmpal, quality: 0) == vmpal)
    #expect(throws: ComputerError.self) { try VMPalDriver.reencodeJPEG(Data("png".utf8), quality: 40) }
}

/// The VM side of `VMPalDriver` for typing: files `vmpal cp` put there, its clipboard, and what
/// reached the focused app.
private final class FakeVMPal: Sendable {
    struct State {
        var files: [String: String] = [:]
        var clipboard = ""
        var typed = ""
        var calls: [[String]] = []
        var failPaste = false
    }
    let state = Mutex(State())

    func run(_ args: [String]) throws -> String {
        try state.withLock { s in
            s.calls.append(args)
            switch args[0] {
            case "cp":
                s.files[String(args[2].split(separator: ":", maxSplits: 1)[1])] =
                    try String(contentsOf: URL(filePath: args[1]), encoding: .utf8)
            case "exec":    // exec <vm> -- /bin/sh -c <script> sh <args…>
                let script = args[5], a = Array(args.dropFirst(7))
                if script.contains("pbpaste >") { s.files[a[0]] = s.clipboard }
                if script.contains("pbcopy <") { s.clipboard = s.files[a[0]] ?? "" }
                if script.contains("rm -f") { for f in a { s.files[f] = nil } }
            default:        // ui <vm> <command…>
                let cmd = Array(args.dropFirst(2))
                switch (cmd[0], cmd[1]) {
                case ("key", "cmd+v"):
                    if s.failPaste { throw ComputerError("vmpal: key failed") }
                    s.typed += s.clipboard
                case ("key", "Return"): s.typed += "\n"
                case ("type", _): s.typed += String(try String(contentsOf: URL(filePath: cmd[2]), encoding: .utf8).dropLast())
                default: break
                }
            }
            return "Done: 1 action in 0.1 s."
        }
    }
}

@Test func nonASCIITextIsPastedThroughTheClipboardWhichComesBack() async throws {
    let vm = FakeVMPal()
    vm.state.withLock { $0.clipboard = "主人原本複製的" }
    var cfg = ComputerConfig()
    cfg.vmID = "E6813EBA"
    let d = VMPalDriver(cfg, vmpal: { try vm.run($0) })
    let text = "你好，我是娃娃\n\nsecond"
    let said = try await d.act(.type(text))
    var s = vm.state.withLock { $0 }
    #expect(s.typed == text && s.clipboard == "主人原本複製的" && s.files.isEmpty)
    #expect(said.hasPrefix("經剪貼簿貼上（cmd+v），剪貼簿已還原。"))
    #expect(!s.calls.joined().contains { $0.contains("你好") || $0.contains("second") })   // never a process argument
    #expect(s.calls.filter { $0.starts(with: ["ui", "E6813EBA", "key", "cmd+v"]) }.count == 2)
    // ASCII-only text is still typed.
    vm.state.withLock { $0.calls = []; $0.typed = "" }
    _ = try await d.act(.type("ls -la\n"))
    s = vm.state.withLock { $0 }
    #expect(s.typed == "ls -la\n" && s.calls.count == 1 && s.calls[0].starts(with: ["ui", "E6813EBA", "type", "--file"]))
    // A failed paste is a failure, and the clipboard still comes back.
    vm.state.withLock { $0.failPaste = true; $0.typed = "" }
    do {
        _ = try await d.act(.type("娃娃"))
        Issue.record("a failed paste came back as a success")
    } catch let e as ComputerError {
        #expect(e.description == "vmpal: key failed")
    }
    s = vm.state.withLock { $0 }
    #expect(s.typed == "" && s.clipboard == "主人原本複製的" && s.files.isEmpty)
}

@Test func aChangeInTheWatchedRegionWakesHerAndANewWatchReplacesTheOld() async throws {
    let h = try await menuHarness { $0.computer.watchStopAfter = 10 }
    try await h.turn(Scripted("", watch("0,0,100,50", 60)))
    #expect(try h.toolResults().last!.text == "開始盯著0,0,100,50，最多等 60 秒；有變化或時間到會叫你。")
    #expect(await !h.doll.followUp)                                     // the ack alone is no new turn
    try await h.turn(Scripted("", watch("10,10,20,20", 60)))
    #expect(try h.toolResults().last!.text.hasSuffix("（取代了上一個 watch）"))
    try await Task.sleep(for: .milliseconds(200))
    #expect(await watchWakes(h).isEmpty)                                // nothing changed yet
    h.vm.setFrame(1)
    try await waitUntil { await watchWakes(h).count == 1 }
    let wakes = await watchWakes(h)
    #expect(wakes.count == 1 && wakes[0].text == "盯著的10,10,20,20變了。" && wakes[0].image != nil)
    try await Task.sleep(for: .milliseconds(200))
    #expect(await watchWakes(h).count == 1)                             // the replaced watch is gone
}

@Test func wakesAreRateLimited() async throws {
    let h = try await menuHarness { $0.computer.watchMinIntervalS = 0.4; $0.computer.watchStopAfter = 100 }
    h.vm.setChanging(true)                                              // every capture differs
    try await h.turn(Scripted("", watch("window", 60)))
    try await Task.sleep(for: .seconds(1))
    // The first change at once, then one per 0.4 s; unlimited, a 0.02 s poll would wake her ~50 times.
    let n = await watchWakes(h).count
    #expect((2...3).contains(n), "\(n) wakes")
}

@Test func maxWaitWakesHerWhenNothingChanges() async throws {
    let h = try await menuHarness()
    try await h.turn(Scripted("", watch("window", 1)))
    try await Task.sleep(for: .milliseconds(600))
    #expect(await watchWakes(h).isEmpty)
    try await waitUntil { await watchWakes(h).count == 1 }
    #expect(await watchWakes(h).map(\.text) == ["盯著的整個畫面 1 秒沒變化。"])
}

@Test func theWatchStopsAfterMWakesWithNoActionAndAnActionResetsTheCount() async throws {
    let h = try await menuHarness { $0.computer.watchStopAfter = 2 }
    try await h.turn(Scripted("", watch("window", 60)))
    h.vm.setFrame(1)
    try await waitUntil { await watchWakes(h).count == 1 }
    try await h.turn(Scripted("", key("Escape")))                      // takes the wake; an action resets the count
    let afterAction = (await h.doll.watchIdle, await h.doll.watch?.id)
    #expect(afterAction.0 == 0 && afterAction.1 == 1)
    h.vm.setFrame(2)
    try await waitUntil { await watchWakes(h).count == 1 }
    h.vm.setFrame(3)
    try await waitUntil { await watchWakes(h).count == 2 }
    let wakes = await watchWakes(h)
    #expect(wakes.count == 2 && !wakes[0].text.contains("停了"))
    #expect(wakes[1].text.contains("（連續 2 次叫你都沒操作電腦，watch 停了）"))
    let stopped = await h.doll.watch?.id
    #expect(stopped == nil)
    #expect(h.logs.contains { $0.contains("watch 1 stopped after 2 wakes in a row without a computer action") })
    h.vm.setFrame(4)
    try await Task.sleep(for: .milliseconds(200))
    #expect(await watchWakes(h).count == 2)
}

@Test func outcomeStopsTheWatch() async throws {
    let h = try await menuHarness()
    try await h.turn(Scripted("", watch("window", 60)))
    try await h.turn(Scripted("", outcome(false, "沒好")))
    #expect(h.last.outcome == "failure")
    h.vm.setFrame(1)
    try await Task.sleep(for: .milliseconds(200))
    let left = await h.doll.watch?.id
    #expect(await watchWakes(h).isEmpty && left == nil)
}

// memory folders (2026-11-08: secret / own / public) ----------------------------

@Test func ownMemoriesShowInTheOwnersTurnsAndHersButNeverAStrangers() async throws {
    #expect(Memory.visible(.stranger, sawSecret: true) == [.public])
    #expect(Memory.visible(.doll, sawSecret: false) == [.public, .own])
    #expect(Memory.visible(.owner, sawSecret: false) == [.public, .own])
    #expect(Memory.visible(.owner, sawSecret: true) == [.public, .own, .secret])
    let h = try await Harness()
    await h.doll.ownerSaid("記一下 VM 的密碼")
    try await h.turn(Scripted("", remember(folders: [("我的 VM 登入密碼是 fish123", "own", nil), ("主人喜歡 VM 用深色模式", "public", nil)])))
    #expect(try h.toolResults().last!.text.contains("第 1 條已存入 own/"))
    #expect(h.last.actions == ["remember own", "remember public"])
    #expect(try FileManager.default.contentsOfDirectory(atPath: h.dir.appending(path: "memory/own").path).count == 1)
    // The owner's turn: own is visible, and the label says the folder.
    await h.doll.ownerSaid("我的 VM 密碼是什麼")
    try await h.turn(Scripted("", recall("VM 登入密碼")))
    let owner = try h.toolResults().last!
    #expect(owner.text.contains("〔確定｜own〕\n我的 VM 登入密碼是 fish123") && !owner.secret)
    // Her own turn (her watch woke her): own is still visible.
    await h.doll.put(Event(.watch, .internal, "盯著的整個畫面變了。"))
    try await h.turn(Scripted("", recall("VM 登入密碼")))
    #expect(Doll.audience(try h.events()) == .doll)
    #expect(try h.toolResults().last!.text.contains("fish123"))
    // A stranger's turn (no such input until M6; the projection is Memory's): never.
    var cfg = try Config.load(root.appending(path: "dollcore.example.toml")).memory
    cfg.dir = h.dir.appending(path: "memory").path
    let memory = try Memory(config: cfg, root: h.dir.appending(path: "memory"), store: h.store)
    let stranger = try memory.search("VM 登入密碼", visible: Memory.visible(.stranger, sawSecret: false), limit: 5)
    #expect(!stranger.contains { $0.folder == "own" } && stranger.contains { $0.text.contains("深色模式") })
    #expect(try memory.search("VM 登入密碼", visible: Memory.visible(.doll, sawSecret: false), limit: 5).contains { $0.folder == "own" })
}

// shortcut memory (spec 3.1, 2026-11-09) ---------------------------------------------

private let textEdit = AppIdentity(bundleID: "com.apple.TextEdit", name: "TextEdit", version: "1.21")
private let textEditKeys = [MenuShortcut(path: "File › Save", keys: "⌘S"), MenuShortcut(path: "File › Save As…", keys: "⇧⌥⌘S"),
                            MenuShortcut(path: "Format › Font › Bold", keys: "⌘B")]

/// Her public memory files, parsed, by text.
private func publicMemories(_ h: Harness) throws -> [(text: String, tier: SourceTier, confidence: Double, kind: String, app: String, version: String)] {
    let dir = h.dir.appending(path: "memory/public")
    return try FileManager.default.contentsOfDirectory(atPath: dir.path).filter { $0.hasSuffix(".md") }.map {
        let p = try Memory.parseFile(String(contentsOf: dir.appending(path: $0), encoding: .utf8))
        return (p.body, p.tier, p.confidence, p.kind, p.app, p.appVersion)
    }.sorted { $0.text < $1.text }
}

@Test func theFrontAppsShortcutsBecomeOneProgramMemoryEachOncePerVersion() async throws {
    let h = try await Harness()
    h.vm.setApp(textEdit, textEditKeys)
    await h.doll.ownerSaid("用電腦")
    try await h.turn(Scripted("", useComputer))
    let mems = try publicMemories(h)
    #expect(mems.map(\.text) == ["TextEdit：File › Save As…＝⇧⌥⌘S（TextEdit 1.21）", "TextEdit：File › Save＝⌘S（TextEdit 1.21）",
                                 "TextEdit：Format › Font › Bold＝⌘B（TextEdit 1.21）"])
    let program = try Config.load(root.appending(path: "dollcore.example.toml")).memory.confidence[.program]!
    #expect(program > 0.85)
    #expect(mems.allSatisfy { $0.tier == .program && $0.confidence == program && $0.kind == "" && $0.app == "com.apple.TextEdit" && $0.version == "1.21" })
    // The same app at the same version is never read again.
    try await h.turn(Scripted("", screenshotCall, key("cmd+s")))
    #expect(h.vm.state.withLock { $0.menuReads } == ["com.apple.TextEdit"])
    #expect(try publicMemories(h).count == 3)
    // She finds them with recall, as 確定.
    await h.doll.ownerSaid("另存新檔怎麼按？")
    try await h.turn(Scripted("", recall("TextEdit 另存")))
    #expect(try h.toolResults().last!.text.contains("〔確定｜public〕\nTextEdit：File › Save As…＝⇧⌥⌘S（TextEdit 1.21）"))
    // A new version rewrites that app's records.
    h.vm.setApp(AppIdentity(bundleID: "com.apple.TextEdit", name: "TextEdit", version: "1.22"), [MenuShortcut(path: "File › Save", keys: "⌘S")])
    try await h.turn(Scripted("", screenshotCall))
    #expect(try publicMemories(h).map(\.text) == ["TextEdit：File › Save＝⌘S（TextEdit 1.22）"])
    // Another app gets its own; the first app's stay.
    h.vm.setApp(AppIdentity(bundleID: "com.apple.finder", name: "Finder", version: "15.0"), [MenuShortcut(path: "File › New Finder Window", keys: "⌘N")])
    try await h.turn(Scripted("", key("cmd+n")))
    #expect(try publicMemories(h).map(\.text) == ["Finder：File › New Finder Window＝⌘N（Finder 15.0）", "TextEdit：File › Save＝⌘S（TextEdit 1.22）"])
    #expect(h.vm.state.withLock { $0.menuReads } == ["com.apple.TextEdit", "com.apple.TextEdit", "com.apple.finder"])
    #expect(try h.store.countMemoryIndex() == 2)

    // A new process on the same memory knows what is recorded from the files.
    var cfg = try Config.load(root.appending(path: "dollcore.example.toml"))
    cfg.core.dataDir = h.dir.path
    cfg.memory.dir = h.dir.appending(path: "memory").path
    let again = try Doll(config: cfg, root: root, model: FakeModel([]), selector: FakeSelector(), computer: h.vm, say: { _ in })
    try await again.learnFrontApp()
    #expect(h.vm.state.withLock { $0.menuReads }.count == 3)
}

@Test func learnShortcutsOffReadsNoMenus() async throws {
    let off = try await Harness(tune: { $0.computer.learnShortcuts = false })
    off.vm.setApp(textEdit, textEditKeys)
    await off.doll.ownerSaid("用電腦")
    try await off.turn(Scripted("", useComputer))
    try await off.turn(Scripted("", key("cmd+s")))
    #expect(off.vm.state.withLock { $0.menuReads.isEmpty })
    #expect(try publicMemories(off).isEmpty)
}

@Test func axmenusOutputParses() {
    #expect(VMPalDriver.parseFrontApp("com.apple.TextEdit\tTextEdit\t1.21\n") == textEdit)
    #expect(VMPalDriver.parseFrontApp("") == nil && VMPalDriver.parseFrontApp("\tSomething\t1\n") == nil)
    #expect(VMPalDriver.parseShortcuts("File › Save\t⌘S\nEdit › Complete\t⌥Esc\nbroken line\n")
            == [MenuShortcut(path: "File › Save", keys: "⌘S"), MenuShortcut(path: "Edit › Complete", keys: "⌥Esc")])
}

// auto-attached memories and the open windows line (spec 3.1, 2026-11-09) ----------------

/// Writes one record file into her memory folder and re-indexes, as if remembered earlier. Default:
/// an experience (tier self_reported, 0.4 = 大概), the only kind the program attaches.
private func seedMemory(_ h: Harness, _ folder: String, _ text: String, tier: String = "self_reported",
                        confidence: Double = 0.4, entities: [MemoryEntity] = [], id: String? = nil) async throws {
    let dir = h.dir.appending(path: "memory/\(folder)")
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    let ents = entities.isEmpty ? "" : "entities = \(MemoryEntity.json(entities))\n"
    let file = "+++\nkind = \"\"\nconfidence = \(confidence)\ncreated_ts = 0\nsource_event_seq = 1\nsource_tier = \"\(tier)\"\n\(ents)+++\n\(text)"
    try file.write(to: dir.appending(path: "\(id ?? "1-\(UUID().uuidString.prefix(6).lowercased())").md"), atomically: true, encoding: .utf8)
    try await h.doll.rebuildMemory()
}

private func memoryEvents(_ h: Harness) throws -> [Event] { try h.events().filter { $0.kind == .memory } }

@Test func anOwnerLineAttachesRelatedExperiencesWholeBeforeHerTurn() async throws {
    let h = try await Harness()
    try await seedMemory(h, "public", "經驗：幫我用 TextEdit 寫筆記\n期望：筆記存好\n1. key cmd+s → Done\n結果：標題沒有點")
    await h.doll.ownerSaid("幫我用 TextEdit 寫筆記")
    try await h.turn("好")
    let mem = try memoryEvents(h)
    #expect(mem.map(\.text) == ["【相關記憶】程式用主人的話查到、判斷用得上的過去經驗：\n〔大概｜public〕\n經驗：幫我用 TextEdit 寫筆記\n期望：筆記存好\n1. key cmd+s → Done\n結果：標題沒有點"])
    // Right after the owner's line, before the footer.
    let seen = await h.model.seen(0)
    #expect(seen[seen.count - 2].content.hasSuffix("] " + mem[0].text) && seen[seen.count - 2].role == "user")
    #expect(seen[seen.count - 3].content.hasSuffix("幫我用 TextEdit 寫筆記"))
    #expect(h.last.attached == [mem[0].text])
    #expect(h.logs.contains { $0.contains("選記憶（主人的新訊息）") && $0.contains("prepared 1;") && $0.contains("attached 1 [") })
    // Turns on her own tool results attach nothing.
    try await h.turn(Scripted("", think("high")))
    try await h.turn("嗯")
    #expect(try memoryEvents(h).count == 1 && h.last.attached.isEmpty)

    // The on-device model keeping none: no event.
    let none = try await Harness()
    none.selector.setScore { _, _ in 6 }
    try await seedMemory(none, "public", "經驗：幫我用 TextEdit 寫筆記")
    await none.doll.ownerSaid("幫我用 TextEdit 寫筆記")
    try await none.turn("好")
    #expect(try memoryEvents(none).isEmpty && none.last.attached.isEmpty)
    #expect(none.selector.scored.count == 1 && none.last.selections.count == 1)
    #expect(none.logs.contains { $0.contains("prepared 1;") && $0.contains("attached 0 []") })
}

@Test func onlyExperiencesAreAttachedNeverShortcutsOrHerOtherMemories() async throws {
    let h = try await Harness()
    h.vm.setApp(textEdit, textEditKeys)
    try await seedMemory(h, "public", "TextEdit 存檔按 cmd+s", tier: "owner", confidence: 0.9)
    try await seedMemory(h, "public", "TextEdit：File › Save＝⌘S（TextEdit 1.20）", tier: "program", confidence: 0.95)
    try await seedMemory(h, "public", "經驗：用 TextEdit 存檔\n1. key cmd+s → Done\n結果：存好了")
    await h.doll.ownerSaid("用 TextEdit 存檔")
    try await h.turn(Scripted("", useComputer))
    // The owner's line: the experience only. use_computer shows TextEdit (a switch, its shortcuts
    // learned now): nothing new to attach, no event.
    let mem = try memoryEvents(h)
    #expect(mem.count == 1 && mem[0].text.contains("經驗：用 TextEdit 存檔") && !mem[0].text.contains("⌘S（"))
    #expect(!mem[0].text.contains("TextEdit 存檔按 cmd+s"))
    #expect(h.logs.contains { $0.contains("相關記憶（前景 app 換成 TextEdit）") && $0.contains("already attached 1 [") })
    #expect(h.logs.contains { $0.contains("選記憶（前景 app 換成 TextEdit）") && $0.contains("prepared 0;") })
    // The shortcuts and her own memory are still recallable.
    try await h.turn(Scripted("", recall("TextEdit 存檔")))
    let found = try h.toolResults().last!.text
    #expect(found.contains("TextEdit 存檔按 cmd+s") && found.contains("＝⌘S（TextEdit"))
    // The owner's next line starts over: the same experience comes again.
    await h.doll.ownerSaid("再存一次 TextEdit")
    try await h.turn(Scripted("", say("好")))
    #expect(try memoryEvents(h).count == 2)
}

@Test func attachedMemoriesAreResentByteIdentical() async throws {
    let h = try await Harness()
    h.vm.setApp(textEdit, textEditKeys)
    try await seedMemory(h, "public", "經驗：用電腦存檔\n1. key cmd+s → Done")
    await h.doll.ownerSaid("用電腦存檔")
    try await h.turn(Scripted("", useComputer))
    try await h.turn(Scripted("", key("cmd+s")))
    try await h.turn("好了")
    let reqs = await h.model.requests.map(\.messages)
    #expect(reqs.count == 3)
    #expect(reqs[0].contains { $0.content.contains("【相關記憶】") } && reqs[1].filter { $0.content.contains("【相關記憶】") }.count == 1)
    // Every request starts with the whole request before it, footer included: nothing is ever
    // re-rendered differently or appended at the tail only.
    for i in 1..<reqs.count { #expect(Array(reqs[i].prefix(reqs[i - 1].count)) == reqs[i - 1]) }
}

@Test func attachedMemoriesKeepTheFolderRules() async throws {
    let h = try await Harness()
    try await seedMemory(h, "secret", "經驗：TextEdit 的秘密筆記密碼是 1234")
    try await seedMemory(h, "own", "經驗：我的 TextEdit 帳號是 doll")
    try await seedMemory(h, "public", "經驗：TextEdit 存檔按 cmd+s")
    await h.doll.ownerSaid("TextEdit 怎麼用")
    try await h.turn("好")
    let first = try memoryEvents(h)
    #expect(first.count == 1 && !first[0].secret && !first[0].text.contains("1234"))
    #expect(first[0].text.contains("〔大概｜own〕\n經驗：我的 TextEdit 帳號是 doll") && first[0].text.contains("〔大概｜public〕\n經驗：TextEdit 存檔按 cmd+s"))
    // A secret owner line: the turn has seen a secret, so secret/ is read, and the event is a secret one.
    await h.doll.ownerSaid("TextEdit 的秘密筆記", secret: true)
    try await h.turn("好")
    let second = try memoryEvents(h)
    #expect(second.count == 2 && second[1].secret && second[1].text.contains("〔大概｜secret〕\n經驗：TextEdit 的秘密筆記密碼是 1234"))
}

// scenarios, computer sessions and experiences (spec 3.1, 2026-11-09) ----------------------

/// Her public memory files with tier self_reported.
private func experiences(_ h: Harness) throws -> [(text: String, tier: SourceTier, confidence: Double, kind: String, app: String, version: String)] {
    try publicMemories(h).filter { $0.tier == .selfReported }
}

@Test func useComputerNeedsExpect() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("用電腦")
    try await h.turn(Scripted("", ("use_computer", [])))
    #expect(try h.toolResults().last!.text == "失敗：use_computer 少了參數 expect")
    try await h.turn(Scripted("", ("use_computer", [("expect", "  ")])))
    #expect(try h.toolResults().last!.text == "失敗：use_computer 的 expect 是空的")
    #expect(await h.doll.scenario == nil)
}

@Test func aSuccessfulOutcomeWritesOneExperienceOfHerActions() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("把 notes 存檔")
    try await h.turn(Scripted("", ("use_computer", [("expect", "標題沒有「已編輯」")])))
    // The menu is open: an action that fails is not a step; each step is only what she did, no VM answer text.
    h.vm.failNext("boom\nsecond line")
    try await h.turn(Scripted("", ("click", [("text", "Save")]), screenshotCall))
    h.vm.setSizes(screen: (1280, 800), crop: (580, 360))
    try await h.turn(Scripted("", ("click", [("element", "dockitem \"Finder\"")]), screenshotCall))
    try await h.turn(Scripted("", ("click", [("x", "100"), ("y", "50"), ("on", "window"), ("count", "2")]),
                              ("type", [("text", "娃娃來過")]), key("cmd+s")))
    // She looks, then answers in the same reply.
    try await h.turn(Scripted("", screenshotCall, outcome(true, "按 ⌘S 存了檔，看標題列確認沒有「已編輯」。\n1. 這行是我寫的，不是步驟")))
    #expect(h.last.footer.contains("- 選單：電腦（做完呼叫 outcome(success, summary) 結束）"))
    #expect(try h.toolResults().last!.text == "記下了：達成。你的經過和這次的步驟存成一條經驗。離開電腦選單了；電腦工具鎖回去了。")
    let text = "經驗：把 notes 存檔\n按 ⌘S 存了檔，看標題列確認沒有「已編輯」。\n1. 這行是我寫的，不是步驟\n步驟：\n"
        + "1. click dockitem \"Finder\"\n2. click 60,45 雙擊\n3. type 「娃娃來過」\n4. key cmd+s"
    #expect(h.last.outcome == "success" && h.last.experience == text && h.last.actions.contains("outcome success"))
    let mems = try experiences(h)
    let conf = try Config.load(root.appending(path: "dollcore.example.toml")).memory.confidence[.selfReported]!
    #expect(mems.count == 1 && mems[0].text == text && mems[0].confidence == conf && mems[0].kind == "")
    #expect(conf >= 0.3 && conf < 0.7)
    // Relocked.
    try await h.turn(Scripted("", key("Return")))
    #expect(h.last.actions == ["error key"] && h.last.footer.contains("- 選單：頂層"))
    #expect(await h.doll.scenario == nil)
    // Its stats are in the front-matter, not in the text (what is embedded): this run's 5 turns.
    let files = try experienceFiles(h)
    let stats = try #require(files.first?.stats)
    #expect(files.count == 1 && files[0].task == "把 notes 存檔" && files[0].body == text && !files[0].body.contains("成績"))
    // Only the program's steps count as steps, not a numbered line in her summary.
    #expect(Experience(id: "", folder: "public", secret: false, level: "", file: files[0]).steps
            == ["click dockitem \"Finder\"", "click 60,45 雙擊", "type 「娃娃來過」", "key cmd+s"])
    #expect(stats.turns == 5 && (0..<30).contains(stats.seconds)
            && stats == ExperienceStats(turns: 5, seconds: stats.seconds, attachedFollowed: 0, attachedSuccess: 0, attachedFailed: 0))
    // The next time the owner asks the same, it is attached whole, its stats line first.
    await h.doll.ownerSaid("把 notes 存檔")
    try await h.turn("好")
    #expect(stats.line == "成績：這次 5 回合、\(stats.seconds) 秒；附上後照做 0 次，成功 0 次（這是已知可行的做法；如果看到更短的走法，可以直接走；結果猜得到的幾步可以寫在同一則回覆裡。）")
    #expect(h.last.attached.count == 1 && h.last.attached[0].hasSuffix("〔大概｜public〕\n" + stats.line + "\n" + text))
}

@Test func aFailedOrMissingOutcomeWritesNothing() async throws {
    let h = try await menuHarness()
    try await h.turn(Scripted("", key("cmd+s"), outcome(false, "還有點")))
    #expect(h.last.outcome == "failure" && h.last.experience == nil)
    #expect(try experiences(h).isEmpty)
    // A bad success value is an error, and the scenario stays open.
    try await h.turn(Scripted("", useComputer))
    try await h.turn(Scripted("", ("outcome", [("success", "maybe"), ("summary", "x")]), outcome(true, "看到了")))
    #expect(try h.toolResults().map(\.text).contains("失敗：outcome 的 success 只能是 true 或 false，收到 maybe"))
    #expect(h.last.outcome == "success")
    #expect(try experiences(h).count == 1)
    // A scenario still open when the program stops: unknown, relocked, logged, nothing written.
    try await h.turn(Scripted("", useComputer))
    try await h.turn(Scripted("", watch("window", 60)))
    await h.doll.shutdown()
    #expect(await h.doll.scenario == nil)
    #expect(await h.doll.watch == nil)
    #expect(h.logs.contains { $0.contains("outcome unknown: computer scenario ended without her outcome") })
    #expect(try experiences(h).count == 1)
    // Nothing is stored for it (no summary of hers), so its turns stay in requests in full.
    #expect(try h.timeline().closedScenarios.map(\.success) == [false, true])
}

@Test func outcomeEndsTheSessionAndNothingWakesHerForIt() async throws {
    let h = try await Harness([Scripted("", useComputer), Scripted("", say("存好了"), key("cmd+s"), outcome(true, "看到了"))])
    let doll = h.doll
    let runner = Task { try await doll.run() }
    await h.doll.ownerSaid("存檔")
    let deadline = ContinuousClock.now + .seconds(20)
    while await h.model.requests.count < 2 || h.reports.count < 2, ContinuousClock.now < deadline {
        try await Task.sleep(for: .milliseconds(20))
    }
    try await Task.sleep(for: .milliseconds(200))
    #expect(await h.model.requests.count == 2 && h.said == ["存好了"])  // her words came with the outcome
    #expect(await h.doll.settled)
    #expect(await h.doll.scenario == nil)
    #expect(h.last.outcome == "success" && h.vm.actions == [.key("cmd+s")])
    // The result is still in the timeline, right after her reply.
    let events = try h.events()
    #expect(events.last?.kind == .tool && events.last?.text == "記下了：達成。你的經過和這次的步驟存成一條經驗。離開電腦選單了；電腦工具鎖回去了。")
    #expect(events.dropLast(3).last?.kind == .doll)
    await h.doll.stop()
    try await runner.value
}

@Test func outcomeOutsideASessionIsAnError() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("好了嗎")
    try await h.turn(Scripted("", outcome(true, "好了")))
    #expect(try h.toolResults().last!.text == "失敗：outcome 現在鎖著：先呼叫 use_computer(expect) 打開電腦選單")
    #expect(h.last.outcome == nil && h.last.actions == ["error outcome"])
    // After a session ended, too.
    try await h.turn(Scripted("", useComputer))
    try await h.turn(Scripted("", outcome(false, "沒好")))
    try await h.turn(Scripted("", outcome(true, "好了")))
    #expect(try h.toolResults().last!.text == "失敗：outcome 現在鎖著：先呼叫 use_computer(expect) 打開電腦選單")
    #expect(h.last.outcome == nil)
    #expect(try experiences(h).isEmpty)
}

@Test func callsAfterOutcomeInTheSameReplyAreRefused() async throws {
    let h = try await menuHarness()
    try await h.turn(Scripted("", key("cmd+s"), outcome(false, "沒好"), key("Return"), screenshotCall))
    let refused = "失敗：這次用電腦已經用 outcome 結束了；outcome 後面的呼叫沒有執行。"
    #expect(try h.toolResults().suffix(3).map(\.text)
            == ["記下了：沒達成。離開電腦選單了；電腦工具鎖回去了。", refused, refused])
    #expect(h.last.actions == ["key", "outcome failure", "error key", "error screenshot"])
    #expect(h.vm.actions == [.key("cmd+s")])
    #expect(await !h.doll.followUp)
    #expect(await h.doll.scenario == nil)
    // The next request: the enter call's result says it was not reached, with her summary; the
    // refused calls' results are not in it.
    await h.doll.ownerSaid("好吧")
    try await h.turn("嗯")
    let request = await h.model.requests.last!.messages
    #expect(request.contains(ChatMessage(role: "tool", content: "沒達成。你自己記下的經過：\n沒好")))
    #expect(!request.contains { $0.content.hasPrefix("失敗：這次用電腦已經用 outcome 結束了") || !$0.images.isEmpty })
}

@Test func anExperienceIsCutToTheCapFromTheMiddleSteps() {
    let steps = (1...12).map { "key cmd+\($0) then a long enough line" }
    let full = Doll.experienceText(task: "做事", summary: "做好了，看過了", steps: steps, maxChars: 10_000)
    #expect(full.components(separatedBy: "\n").count == 15 && !full.contains("省略"))
    let cut = Doll.experienceText(task: "做事", summary: "做好了，看過了", steps: steps, maxChars: 300)
    #expect(cut.count <= 300)
    #expect(cut.hasPrefix("經驗：做事\n做好了，看過了\n步驟：\n1. key cmd+1 ") && cut.hasSuffix("\n12. key cmd+12 then a long enough line"))
    #expect(cut.contains("（省略 ") && cut.components(separatedBy: "\n").count < 15)
    // Header and summary alone over the cap: hard cut.
    let tiny = Doll.experienceText(task: String(repeating: "長", count: 500), summary: "y", steps: steps, maxChars: 100)
    #expect(tiny.count == 100 && tiny.hasSuffix("…"))
}

@Test func everyComputerResultEndsWithTheOpenWindowsAndFocusOrWhyNot() async throws {
    let h = try await menuHarness()
    #expect(try h.toolResults().last!.text.hasSuffix("\n" + fakeWindowsLine))
    h.vm.failState("vmpal: exec failed")
    try await h.turn(Scripted("", key("cmd+s")))
    #expect(try h.toolResults().last!.text == "開著的視窗和焦點讀不到：vmpal: exec failed")
    #expect(h.last.actions == ["key", "error windows"])
}

@Test func openWindowsAndFocusRenderFromAgentDesktop() {
    let windows = Data("""
        {"ok":true,"data":[{"id":"w-1","app_name":"Finder","title":"Finder","is_focused":false,"visible":true},
        {"id":"w-2","app_name":"TextEdit","title":"TextEdit","is_focused":true,"visible":true},
        {"id":"w-3","app_name":"Caffeine","title":"Caffeine","is_focused":false,"visible":false},
        {"id":"w-4","app_name":"Safari","title":"Apple","is_focused":false,"visible":true}]}
        """.utf8)
    let focused = Data("""
        {"ok":true,"data":{"window":{"id":"w-2"},"tree":{"role":"window","name":"notes.txt","children":[
        {"role":"scrollarea","children":[{"role":"textarea","value":"第一行\\n第二行很長很長很長很長很長很長很長很長","states":["focused"]}]},
        {"role":"combobox","name":"font size","value":12},{"role":"button","name":"Close"}]}}}
        """.utf8)
    #expect(OpenWindows.line(windows: windows, focused: focused)
            == "開著的視窗：Finder、TextEdit「notes.txt」（前景）、Safari「Apple」；焦點：textarea「第一行⏎第二行很長很長很長很長很長很長很…」")
    let notFound = Data(#"{"ok":false,"error":{"code":"WINDOW_NOT_FOUND","message":"No window found"}}"#.utf8)
    #expect(OpenWindows.line(windows: Data(#"{"ok":true,"data":[]}"#.utf8), focused: notFound)
            == "開著的視窗：沒有；焦點讀不到：WINDOW_NOT_FOUND: No window found")
    #expect(OpenWindows.line(windows: Data("boom".utf8), focused: Data(#"{"ok":true,"data":{"tree":{"role":"window"}}}"#.utf8))
            == "開著的視窗讀不到：agent-desktop 的回答不是 JSON：boom；焦點：沒有")
}

// experience stats, groups and ranking (spec 3.1, 2026-11-09) ------------------------------

/// Her public experience files, parsed, by file name.
private func experienceFiles(_ h: Harness) throws -> [MemoryFile] {
    let dir = h.dir.appending(path: "memory/public")
    return try FileManager.default.contentsOfDirectory(atPath: dir.path).filter { $0.hasSuffix(".md") }.sorted()
        .map { try Memory.parseFile(String(contentsOf: dir.appending(path: $0), encoding: .utf8)) }
        .filter { $0.tier == .selfReported }
}

/// Writes one experience file with stats into her public memory (as the program writes one) and re-indexes.
private func seedExperience(_ h: Harness, _ id: String, task: String, steps: [String], stats: ExperienceStats?) async throws {
    let dir = h.dir.appending(path: "memory/public")
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    let body = (["經驗：\(task)", "做好了。", "步驟："] + steps.enumerated().map { "\($0.offset + 1). \($0.element)" })
        .joined(separator: "\n")
    let file = MemoryFile(sourceSeq: 1, ts: 1794240000.5, confidence: 0.4, kind: "", tier: .selfReported,
                          task: task, stats: stats, body: body)
    try file.text.write(to: dir.appending(path: "\(id).md"), atomically: true, encoding: .utf8)
    try await h.doll.rebuildMemory()
}

private func statsOf(_ h: Harness, _ id: String) throws -> ExperienceStats? {
    try Memory.parseFile(String(contentsOf: h.dir.appending(path: "memory/public/\(id).md"), encoding: .utf8)).stats
}

@Test func aMemoryFileRendersBackByteIdentical() throws {
    let f = MemoryFile(sourceSeq: 12, ts: 1794240000.123456, confidence: 0.4, kind: "", tier: .selfReported,
                       task: "用 \"Finder\" 找/開\n第二行", stats: ExperienceStats(turns: 9, seconds: 140, attachedFollowed: 2,
                                                                                attachedSuccess: 1, attachedFailed: 1),
                       body: "經驗：x\n1. key cmd+s")
    let parsed = try Memory.parseFile(f.text)
    #expect(parsed == f && parsed.text == f.text)
    #expect(f.text.contains("\ntask = \"用 \\\"Finder\\\" 找/開\\n第二行\"\nturns = 9\nseconds = 140\nattached_followed = 2\nattached_success = 1\nattached_failed = 1\n+++\n"))
    // A file the program wrote before stats existed reads back the same, without any.
    let old = "+++\nkind = \"\"\nconfidence = 0.4\ncreated_ts = 1794240000.0\nsource_event_seq = 0\nsource_tier = \"self_reported\"\n+++\n經驗：y"
    let oldParsed = try Memory.parseFile(old)
    #expect(oldParsed.text == old && oldParsed.stats == nil && oldParsed.task == nil)
    let shortcut = MemoryFile(sourceSeq: 3, ts: 1.5, confidence: 0.95, kind: "", tier: .program, app: "com.apple.TextEdit",
                              appVersion: "1.20", body: "TextEdit：File › Save＝⌘S")
    #expect(try Memory.parseFile(shortcut.text).text == shortcut.text)
}

@Test func groupsAreChainsOfSimilarPairs() {
    let pairs: Set<[Int]> = [[0, 2], [2, 4]]
    #expect(Memory.groups(5) { pairs.contains([$0, $1]) } == [[0, 2, 4], [1], [3]])
    #expect(Memory.groups(0) { _, _ in true }.isEmpty)
    #expect(Memory.groups(3) { _, _ in true } == [[0, 1, 2]])
}

@Test func rankingIsFewestTurnsFailuresBelowSuccessesBreakTies() {
    func exp(_ id: String, _ s: ExperienceStats?) -> Experience {
        Experience(id: id, folder: "public", secret: false, level: "大概",
                   file: MemoryFile(sourceSeq: 0, ts: 0, confidence: 0.4, kind: "", tier: .selfReported, stats: s, body: "經驗：x"))
    }
    let list = [
        exp("a", ExperienceStats(turns: 8, seconds: 100, attachedFollowed: 1, attachedFailed: 1)),   // shortest, but failed once
        exp("b", ExperienceStats(turns: 12, seconds: 200)),
        exp("c", ExperienceStats(turns: 12, seconds: 250, attachedFollowed: 2, attachedSuccess: 2)),  // b's turns, more successes
        exp("d", ExperienceStats(turns: 10, seconds: 300)),                                           // never attached: first anyway
        exp("e", nil),
        exp("f", ExperienceStats(turns: 9, seconds: 90, attachedFollowed: 3, attachedSuccess: 1, attachedFailed: 2)),
    ]
    #expect(Memory.ranked(list).map(\.id) == ["d", "c", "b", "a", "f", "e"])
    #expect(Memory.ranked(list.reversed()).map(\.id) == ["d", "c", "b", "a", "f", "e"])
}

@Test func onlyTheBestOfEachTaskGroupIsAttached() async throws {
    let h = try await Harness()
    let e = "用 Finder 找到文件裡的 notes.txt，用 TextEdit 打開，在最後加一行『娃娃來過』，存檔"
    let f = "用 TextEdit 打開文件裡的 notes.txt，另存一份到桌面，檔名 copy"
    try await seedExperience(h, "1-elong", task: e, steps: ["key cmd+o", "key cmd+s"], stats: ExperienceStats(turns: 13, seconds: 219))
    try await seedExperience(h, "2-eshort", task: e, steps: ["key cmd+s"], stats: ExperienceStats(turns: 10, seconds: 155))
    try await seedExperience(h, "3-f", task: f, steps: ["key cmd+shift+s"], stats: ExperienceStats(turns: 8, seconds: 140))
    await h.doll.ownerSaid(e)
    try await h.turn("好")
    let text = try #require(h.last.attached.first)
    // E's two (same owner line, similarity 1) are one group: only its 10-turn one; F (0.94 < 0.97) is its own group.
    #expect(text.contains("成績：這次 10 回合、155 秒") && !text.contains("13 回合") && text.contains("成績：這次 8 回合、140 秒"))
    #expect(h.logs.contains { $0.contains("[2-eshort(10 回合, 照做 0, 成功 0, 失敗 0) > 1-elong(13 回合, 照做 0, 成功 0, 失敗 0)]") })
    #expect(try await h.doll.experienceRanking().map { $0.map { String($0.prefix { $0 != ":" }) } } == [["2-eshort", "1-elong"], ["3-f"]])
    // At similarity 1.0 owner lines that differ at all are separate groups: both are attached.
    let loose = try await Harness(tune: { $0.memory.experienceGroupSimilarity = 1.0 })
    try await seedExperience(loose, "1-a", task: e, steps: ["key cmd+o"], stats: ExperienceStats(turns: 13, seconds: 219))
    try await seedExperience(loose, "2-b", task: e + "。", steps: ["key cmd+s"], stats: ExperienceStats(turns: 10, seconds: 155))
    await loose.doll.ownerSaid(e)
    try await loose.turn("好")
    #expect(try #require(loose.last.attached.first).contains("13 回合") && loose.last.attached[0].contains("10 回合"))
}

@Test func stepsMatchInOrderByNamePointDoubleAndKey() {
    // Experience2 (analysis.txt): P-1's experience against P-2's actions, P-2's against P-3's.
    let p1 = ["click dockitem \"Finder\"", "click「Documents」", "click 441,213 right", "click 480,241", "click「TextEdit (default)」",
              "click textfield", "type 「娃娃來過」", "key cmd+s"]
    let p2 = ["click dockitem \"Finder\"", "click treeitem \"Documents\"", "click 441,213 right", "click「Open With」", "click 727,239",
              "key cmd+Down", "type 「娃娃來過」", "key cmd+s"]
    let p3 = ["click dockitem \"Finder\"", "click treeitem \"Documents\"", "click 575,246 right", "key Escape", "click 450,192", "key Return",
              "key Escape", "click 450,192 right", "click「Open With」", "click「TextEdit (default)」", "key cmd+Down",
              "type 「娃娃來過」", "key cmd+s"]
    #expect(Doll.matchedSteps(p1, p2) == 5)   // Finder, Documents (same name), 441,213 right, type, cmd+s
    #expect(Doll.matchedSteps(p2, p3) == 7)   // 441,213 right ~ 450,192 right (23 px)
    #expect(Doll.matchedSteps(p2, []) == 0 && Doll.matchedSteps([], p2) == 0)
    func same(_ a: String, _ b: String) -> Bool { StepShape(a).matches(StepShape(b)) }
    #expect(same("click 430,195 雙擊", "click image \"notes.txt\" 雙擊") && !same("click 430,195", "click 430,195 雙擊"))
    #expect(same("click 441,213", "click 441,253") && !same("click 441,213", "click 441,254"))
    #expect(!same("click 441,213 right", "click 441,213") && same("click「OK」 right", "click button \"OK\" right"))
    // A chord an older experience wrote as `shift+cmd+s` is the step `key` now writes as `cmd+shift+s`.
    #expect(same("key shift+cmd+s", "key cmd+shift+s") && same("key Tab Tab", "key tab tab") && !same("key cmd+s", "key cmd+shift+s"))
    #expect(same("click textfield", "click textfield") && !same("click textfield", "click「textfield」"))
    #expect(same("key cmd+Down", "key CMD+down") && !same("type 「a」", "type 「A」") && same("type 「a」", "type「a」"))
    #expect(!same("key cmd+s", "type 「cmd+s」") && same("scroll down 3", "scroll down 3"))
}

@Test func aFollowedExperienceCountsHerOutcomeAndRanksBy() async throws {
    let h = try await Harness()
    let task = "把 notes 存檔"
    try await seedExperience(h, "1-seed", task: task, steps: ["click dockitem \"Finder\"", "key cmd+s", "key cmd+w", "type 「娃娃來過」"],
                             stats: ExperienceStats(turns: 9, seconds: 150))
    // 1: two of its four steps in order (ratio 0.5 = experience_follow_ratio): followed, her success counted.
    await h.doll.ownerSaid(task)
    try await h.turn(Scripted("", useComputer))
    let attached = try #require(h.reports[0].attached.first)
    #expect(attached.contains("成績：這次 9 回合、150 秒；附上後照做 0 次，成功 0 次"))
    try await h.turn(Scripted("", ("click", [("element", "dockitem \"Finder\"")]), key("cmd+s")))
    try await h.turn(Scripted("", outcome(true, "存好了")))
    #expect(h.last.follows == ["experience 1-seed: 2/4 steps done in order (ratio 0.50), followed, counted success: 照做 1、成功 1、失敗 0"])
    #expect(h.logs.contains { $0.contains("experience 1-seed: 2/4 steps done in order (ratio 0.50)") })
    #expect(try statsOf(h, "1-seed") == ExperienceStats(turns: 9, seconds: 150, attachedFollowed: 1, attachedSuccess: 1))
    // The attached event in the timeline is not re-rendered with the new stats: every request in
    // the scenario starts with the whole one before it, and the first after it with the one that
    // entered it (its turns are folded into her summary).
    try await h.turn("好了")
    #expect(try memoryEvents(h).first?.text == attached)
    let reqs = await h.model.requests.map(\.messages)
    for i in 1..<3 { #expect(Array(reqs[i].prefix(reqs[i - 1].count)) == reqs[i - 1]) }
    #expect(reqs.count == 4 && Array(reqs[3].prefix(reqs[0].count)) == reqs[0])
    // Her new experience (3 turns: use_computer, the actions, outcome) now leads the group.
    let fresh = try #require(experienceFiles(h).first { $0.task == task && $0.stats?.turns == 3 })
    #expect(fresh.stats == ExperienceStats(turns: 3, seconds: fresh.stats!.seconds))

    // 2: the new one is attached; none of its steps done: not followed, nothing counted.
    await h.doll.ownerSaid(task)
    try await h.turn(Scripted("", useComputer))
    #expect(h.last.attached.count == 1 && h.last.attached[0].contains("成績：這次 3 回合") && !h.last.attached[0].contains("9 回合"))
    try await h.turn(Scripted("", key("cmd+q"), outcome(false, "沒存到")))
    #expect(h.last.follows.count == 1 && h.last.follows[0].hasSuffix("0/2 steps done in order (ratio 0.00), not followed"))
    #expect(try experienceFiles(h).first { $0.stats?.turns == 3 }?.stats?.attachedFollowed == 0)

    // 3: followed, and she says it failed: counted as a failure, and the failure demotes it below
    // the 9-turn one that succeeded.
    await h.doll.ownerSaid(task)
    try await h.turn(Scripted("", useComputer))
    try await h.turn(Scripted("", ("click", [("element", "dockitem \"Finder\"")]), key("CMD+S"), outcome(false, "沒存到")))
    #expect(h.last.follows.count == 1 && h.last.follows[0].contains("2/2 steps done in order (ratio 1.00), followed, counted failure: 照做 1、成功 0、失敗 1"))
    let ranking = try await h.doll.experienceRanking()
    #expect(ranking.count == 1 && ranking[0].count == 2 && ranking[0][0].hasPrefix("1-seed: 成績：這次 9 回合、150 秒；附上後照做 1 次，成功 1 次；失敗 0 次"))
    #expect(ranking[0][1].contains("3 回合") && ranking[0][1].hasSuffix("附上後照做 1 次，成功 0 次；失敗 1 次"))
    // An unknown outcome (the program stopped with the session open) is only logged.
    await h.doll.ownerSaid(task)
    try await h.turn(Scripted("", useComputer))
    try await h.turn(Scripted("", ("click", [("element", "dockitem \"Finder\"")]), key("cmd+s"), key("cmd+w")))
    await h.doll.shutdown()
    #expect(h.logs.contains { $0.contains("experience 1-seed: 3/4 steps done in order (ratio 0.75), followed; outcome unknown, nothing counted") })
    #expect(try statsOf(h, "1-seed") == ExperienceStats(turns: 9, seconds: 150, attachedFollowed: 1, attachedSuccess: 1))
}

// the front app's cheatsheet (spec 3.1, 2026-11-09) -------------------------------------

private let finder = AppIdentity(bundleID: "com.apple.finder", name: "Finder", version: "15.0")
private let finderKeys = [MenuShortcut(path: "File › New Finder Window", keys: "⌘N"), MenuShortcut(path: "Go › Desktop", keys: "⇧⌘D")]
private let textEditSheet = "【快捷鍵】TextEdit 1.21\nFile › Save = ⌘S\nFile › Save As… = ⇧⌥⌘S\nFormat › Font › Bold = ⌘B"
private let finderSheet = "【快捷鍵】Finder 15.0\nFile › New Finder Window = ⌘N\nGo › Desktop = ⇧⌘D"
private func sheets(_ h: Harness) throws -> [String] { try memoryEvents(h).map(\.text).filter { $0.hasPrefix("【快捷鍵】") } }

@Test func cheatsheetIsOnByDefault() throws {
    #expect(try Config.load(root.appending(path: "dollcore.example.toml")).computer.cheatsheet)
}

@Test func aSwitchedToAppGetsItsCheatsheetOncePerTaskInReaderOrder() async throws {
    let h = try await Harness(tune: { $0.computer.cheatsheet = true })
    h.vm.setApp(textEdit, textEditKeys)
    await h.doll.ownerSaid("用 TextEdit 另存一份")
    try await h.turn(Scripted("", useComputer))
    // The first result shows TextEdit: its whole list, in the reader's order, after the result.
    #expect(try sheets(h) == [textEditSheet])
    #expect(h.last.cheatsheets == [textEditSheet])
    let tail = try h.events().suffix(2).map(\.kind)
    #expect(tail == [.tool, .memory])
    // Learning and the cheatsheet share one read.
    #expect(h.vm.state.withLock { $0.menuReads } == ["com.apple.TextEdit"])
    // The same app again: nothing.
    try await h.turn(Scripted("", key("cmd+s")))
    #expect(try sheets(h).count == 1 && h.last.cheatsheets.isEmpty)
    // Another app: its own; back to the first: nothing, no read.
    h.vm.setApp(finder, finderKeys)
    try await h.turn(Scripted("", key("cmd+n")))
    #expect(try sheets(h) == [textEditSheet, finderSheet])
    h.vm.setApp(textEdit, textEditKeys)
    try await h.turn(Scripted("", key("cmd+s")))
    #expect(try sheets(h) == [textEditSheet, finderSheet])
    #expect(h.logs.contains { $0.contains("快捷鍵 of com.apple.TextEdit 1.21: 3 lines") })

    // The owner's next line starts a task: on the next switch each app gets it again, from the cache.
    await h.doll.ownerSaid("再存一份")
    try await h.turn(Scripted("", key("cmd+s")))
    #expect(try sheets(h).count == 2)
    h.vm.setApp(finder, finderKeys)
    try await h.turn(Scripted("", key("cmd+n")))
    h.vm.setApp(textEdit, textEditKeys)
    try await h.turn(Scripted("", key("cmd+s")))
    #expect(try sheets(h) == [textEditSheet, finderSheet, finderSheet, textEditSheet])
    #expect(h.vm.state.withLock { $0.menuReads } == ["com.apple.TextEdit", "com.apple.finder"])
}

@Test func theFrontAppAtScenarioEntryGetsItsCheatsheetEvenIfItWasInFrontBefore() async throws {
    // Chained E→F (2026-11-09): TextEdit stayed in front across the two scenarios, so F got no sheet;
    // the first scenario's sheet had left the context with it. Entry resets what counts as a switch.
    let h = try await Harness(tune: { $0.computer.cheatsheet = true })
    h.vm.setApp(textEdit, textEditKeys)
    await h.doll.ownerSaid("先存檔")
    try await h.turn(Scripted("", useComputer))
    try await h.turn(Scripted("", key("cmd+s"), outcome(true, "存了")))
    #expect(try sheets(h) == [textEditSheet])
    await h.doll.ownerSaid("再另存一份")
    try await h.turn(Scripted("", useComputer))
    #expect(try sheets(h) == [textEditSheet, textEditSheet] && h.last.cheatsheets == [textEditSheet])
    // The same owner line, a third scenario: again, since the second's sheet is gone from the context too.
    try await h.turn(Scripted("", outcome(true, "好了")))
    try await h.turn(Scripted("", useComputer))
    #expect(try sheets(h).count == 3)
    #expect(h.vm.state.withLock { $0.menuReads } == ["com.apple.TextEdit"])
}

@Test func cheatsheetsAreResentByteIdentical() async throws {
    let h = try await Harness(tune: { $0.computer.cheatsheet = true })
    h.vm.setApp(textEdit, textEditKeys)
    await h.doll.ownerSaid("用電腦存檔")
    try await h.turn(Scripted("", useComputer))
    try await h.turn(Scripted("", key("cmd+s")))
    try await h.turn("好了")
    let reqs = await h.model.requests.map(\.messages)
    #expect(reqs.count == 3)
    let rendered = reqs[1].filter { $0.content.contains("【快捷鍵】") }
    #expect(rendered.count == 1 && rendered[0].role == "user" && rendered[0].content.hasSuffix("程式] " + textEditSheet))
    for i in 1..<reqs.count { #expect(Array(reqs[i].prefix(reqs[i - 1].count)) == reqs[i - 1]) }
}

@Test func noCheatsheetForAnUnknownAppOrAFailedReadWhichOnlyTheLogSays() async throws {
    let h = try await Harness(tune: { $0.computer.cheatsheet = true })
    // No front app: nothing to read, no event.
    h.vm.setApp(nil, [])
    await h.doll.ownerSaid("用電腦")
    try await h.turn(Scripted("", useComputer))
    #expect(try sheets(h).isEmpty && h.vm.state.withLock { $0.menuReads.isEmpty })
    // The reader fails: the log says so; no event, nothing in her results.
    h.vm.setApp(textEdit, textEditKeys)
    h.vm.failMenus("axmenus: no accessibility access")
    try await h.turn(Scripted("", key("cmd+s")))
    #expect(try sheets(h).isEmpty && h.last.cheatsheets.isEmpty)
    #expect(h.logs.contains { $0.hasPrefix("ERROR") && $0.contains("快捷鍵 of com.apple.TextEdit 1.21 failed") && $0.contains("no accessibility access") })
    #expect(try h.events().allSatisfy { !$0.text.contains("快捷鍵") && !$0.text.contains("accessibility") })
    // Not counted as sent: the next switch to it tries again.
    h.vm.failMenus(nil)
    h.vm.setApp(finder, finderKeys)
    try await h.turn(Scripted("", key("cmd+n")))
    h.vm.setApp(textEdit, textEditKeys)
    try await h.turn(Scripted("", key("cmd+s")))
    #expect(try sheets(h) == [finderSheet, textEditSheet])
}

@Test func cheatsheetOffSendsNothing() async throws {
    let h = try await Harness(tune: { $0.computer.cheatsheet = false; $0.computer.learnShortcuts = false })
    h.vm.setApp(textEdit, textEditKeys)
    await h.doll.ownerSaid("用電腦")
    try await h.turn(Scripted("", useComputer))
    h.vm.setApp(finder, finderKeys)
    try await h.turn(Scripted("", key("cmd+n")))
    #expect(try sheets(h).isEmpty && h.reports.allSatisfy { $0.cheatsheets.isEmpty })
    #expect(h.vm.state.withLock { $0.menuReads.isEmpty })
}

// file tools ------------------------------------------------------------------

private let todoPath = "~/Documents/todo.txt"
private let todoBefore = "買牛奶\n洗衣服\n寫作業\n"
private func readFile(_ path: String, from: Int? = nil, to: Int? = nil) -> (String, [(String, String)]) {
    ("read_file", [("path", path)] + (from.map { [("from", "\($0)")] } ?? []) + (to.map { [("to", "\($0)")] } ?? []))
}
private func writeFile(_ path: String, _ text: String) -> (String, [(String, String)]) { ("write_file", [("path", path), ("text", text)]) }
private func editFile(_ path: String, _ old: String, _ new: String) -> (String, [(String, String)]) {
    ("edit_file", [("path", path), ("old", old), ("new", new)])
}

@Test func readFileNumbersLinesReadsARangeAndCapsAtTheConfiguredCount() async throws {
    let h = try await menuHarness(tune: { $0.files.maxReadLines = 3 })
    h.vm.put(todoPath, todoBefore)
    try await h.turn(Scripted("", readFile(todoPath), readFile(todoPath, from: 2, to: 3), readFile(todoPath, from: 3)))
    var results = Array(try h.toolResults().suffix(3))
    #expect(results[0].text == "read_file ~/Documents/todo.txt：共 3 行\n     1\t買牛奶\n     2\t洗衣服\n     3\t寫作業")
    #expect(results[1].text == "read_file ~/Documents/todo.txt：共 3 行，這裡是第 2–3 行\n     2\t洗衣服\n     3\t寫作業")
    #expect(results[2].text == "read_file ~/Documents/todo.txt：共 3 行，這裡是第 3–3 行\n     3\t寫作業")
    // Text only: no screenshot, no crop, no outline, no open-windows line; the screen did not change.
    #expect(results.allSatisfy { $0.image == nil && $0.crop == nil && $0.cropNote == nil && $0.window == nil && !$0.text.contains("開著的視窗") })
    #expect(h.last.actions == ["read_file", "read_file", "read_file"] && h.vm.actions.isEmpty)
    // Over the cap: the first lines and how many are left; from reads on. `to` past the end is the end.
    h.vm.put("~/five.txt", "1\n2\n3\n4\n5\n")
    try await h.turn(Scripted("", readFile("~/five.txt"), readFile("~/five.txt", from: 4, to: 99), readFile("~/five.txt", from: 2, to: 2)))
    results = Array(try h.toolResults().suffix(3))
    #expect(results[0].text == "read_file ~/five.txt：共 5 行，這裡是第 1–3 行\n     1\t1\n     2\t2\n     3\t3\n還有 2 行（read_file 加 from: 4 讀下去）")
    #expect(results[1].text == "read_file ~/five.txt：共 5 行，這裡是第 4–5 行\n     4\t4\n     5\t5")
    #expect(results[2].text == "read_file ~/five.txt：共 5 行，這裡是第 2–2 行\n     2\t2")
    // An empty file, a last line without a newline, CRLF lines (the CR stays on its line).
    h.vm.put("~/empty.txt", "")
    h.vm.put("~/noeol.txt", "a\nb")
    h.vm.put("~/crlf.txt", "a\r\nb\r\n")
    try await h.turn(Scripted("", readFile("~/empty.txt"), readFile("~/noeol.txt"), readFile("~/crlf.txt")))
    results = Array(try h.toolResults().suffix(3))
    #expect(results[0].text == "read_file ~/empty.txt：空檔案，0 行")
    #expect(results[1].text == "read_file ~/noeol.txt：共 2 行\n     1\ta\n     2\tb")
    #expect(results[2].text == "read_file ~/crlf.txt：共 2 行\n     1\ta\r\n     2\tb\r")
    // Bad arguments and a missing file: an error each, nothing read, no step.
    try await h.turn(Scripted("", readFile(todoPath, from: 0), readFile(todoPath, from: 3, to: 2), ("read_file", [("path", todoPath), ("from", "abc")]),
                              readFile(todoPath, from: 9), ("read_file", [("path", "  ")]), readFile("~/nope.txt")))
    #expect(try h.toolResults().suffix(6).map(\.text) == [
        "失敗：read_file 的 from 從 1 算起，收到 0",
        "失敗：read_file 的 from 要小於等於 to，收到 from 3、to 2",
        "失敗：read_file 的 from 要是整數，收到 abc",
        "失敗：~/Documents/todo.txt 只有 3 行，from 是 9；from 最多 3",
        "失敗：read_file 的 path 是空的",
        "失敗：沒有這個檔案：~/nope.txt",
    ])
    #expect(h.last.actions == Array(repeating: "error read_file", count: 6))
}

@Test func writeFileCreatesOrOverwritesTheWholeFileAndReportsLinesAndBytes() async throws {
    let h = try await menuHarness()
    let text = "你好，我是娃娃\n第二行\n"
    try await h.turn(Scripted("", writeFile("~/Desktop/new/hello.txt", text)))
    #expect(try h.toolResults().last!.text == "write_file ~/Desktop/new/hello.txt：寫好了，2 行、\(text.utf8.count) bytes")
    #expect(try h.vm.file("~/Desktop/new/hello.txt") == text && h.toolResults().last!.image == nil)
    // Overwritten whole; no trailing newline stays that way; an empty text empties the file.
    try await h.turn(Scripted("", writeFile("~/Desktop/new/hello.txt", "x"), writeFile("~/Desktop/new/hello.txt", "")))
    #expect(try h.toolResults().suffix(2).map(\.text) == ["write_file ~/Desktop/new/hello.txt：寫好了，1 行、1 bytes",
                                                          "write_file ~/Desktop/new/hello.txt：寫好了，0 行、0 bytes"])
    #expect(h.vm.file("~/Desktop/new/hello.txt") == "")
    try await h.turn(Scripted("", ("write_file", [("path", ""), ("text", "a")]), ("write_file", [("path", "~/a.txt")])))
    #expect(try h.toolResults().suffix(2).map(\.text) == ["失敗：write_file 的 path 是空的", "失敗：write_file 少了參數 text"])
    h.vm.failNext("寫不進 ~/b.txt")
    try await h.turn(Scripted("", writeFile("~/b.txt", "a")))
    #expect(try h.toolResults().last!.text == "失敗：寫不進 ~/b.txt" && h.last.actions == ["error write_file"])
}

@Test func editFileReplacesTheOneOccurrenceOrSaysHowManyAndTheNearestLines() async throws {
    let h = try await menuHarness()
    h.vm.put(todoPath, todoBefore)
    try await h.turn(Scripted("", editFile(todoPath, "買牛奶", "買豆漿"), editFile(todoPath, "寫作業", "寫作業\n交房租")))
    var results = Array(try h.toolResults().suffix(2))
    #expect(results[0].text == "edit_file ~/Documents/todo.txt：改好了，第 1 行起（1 行 → 1 行），現在共 3 行\n     1\t買豆漿\n     2\t洗衣服\n     3\t寫作業")
    #expect(results[1].text == "edit_file ~/Documents/todo.txt：改好了，第 3 行起（1 行 → 2 行），現在共 4 行\n     1\t買豆漿\n     2\t洗衣服\n     3\t寫作業\n     4\t交房租")
    #expect(h.vm.file(todoPath) == "買豆漿\n洗衣服\n寫作業\n交房租\n")
    #expect(results.allSatisfy { $0.image == nil && $0.window == nil } && h.last.actions == ["edit_file", "edit_file"])
    // A multi-line old, a deletion (empty new), context around a change deep in a file.
    try await h.turn(Scripted("", editFile(todoPath, "洗衣服\n寫作業", "寫作業\n洗衣服"), editFile(todoPath, "交房租\n", "")))
    #expect(h.vm.file(todoPath) == "買豆漿\n寫作業\n洗衣服\n")
    #expect(try h.toolResults().last!.text == "edit_file ~/Documents/todo.txt：改好了，第 4 行起（2 行 → 1 行），現在共 3 行\n     1\t買豆漿\n     2\t寫作業\n     3\t洗衣服")
    h.vm.put("~/long.txt", (1...12).map { "line \($0)" }.joined(separator: "\n") + "\n")
    try await h.turn(Scripted("", editFile("~/long.txt", "line 7", "seven")))
    #expect(try h.toolResults().last!.text == "edit_file ~/long.txt：改好了，第 7 行起（1 行 → 1 行），現在共 12 行\n"
            + "     4\tline 4\n     5\tline 5\n     6\tline 6\n     7\tseven\n     8\tline 8\n     9\tline 9\n    10\tline 10")
    // 0 occurrences: the count and the nearest lines (most shared characters), exact whitespace included;
    // several: the count; neither changes the file. Empty old and old == new never reach the file.
    let before = h.vm.file(todoPath)
    h.vm.put("~/dup.txt", "a\nb\na\n")
    try await h.turn(Scripted("", editFile(todoPath, "買牛乳", "買豆漿"), editFile(todoPath, "買豆漿 ", "x"), editFile(todoPath, "zzz", "x"),
                              editFile("~/dup.txt", "a", "c"), editFile(todoPath, "", "x"), editFile(todoPath, "洗衣服", "洗衣服")))
    results = Array(try h.toolResults().suffix(6))
    let zero = "失敗：edit_file ~/Documents/todo.txt：old 在檔案裡出現 0 次，沒改。old 要跟檔案裡一字不差（含空白、換行、標點）；"
    #expect(results[0].text == zero + "最接近的幾行（照這裡的字寫 old）：\n     1\t買豆漿\n     2\t寫作業")
    #expect(results[1].text == zero + "最接近的幾行（照這裡的字寫 old）：\n     1\t買豆漿\n     2\t寫作業")
    #expect(results[2].text == zero + "檔案裡也沒有相近的行，先 read_file 看一下")
    #expect(results[3].text == "失敗：edit_file ~/dup.txt：old 在檔案裡出現 2 次，不知道改哪一個，沒改。old 多帶幾行前後文，讓它只出現一次")
    #expect(results[4].text == "失敗：edit_file 的 old 是空的；要加字就把它旁邊現有的那段當 old，new 填那段加上新的字")
    #expect(results[5].text == "失敗：edit_file 的 old 和 new 一樣，沒有要改什麼")
    #expect(h.vm.file(todoPath) == before && h.vm.file("~/dup.txt") == "a\nb\na\n")
    #expect(h.last.actions == Array(repeating: "error edit_file", count: 6))
    // The nearest line for a multi-line old is judged by its first non-blank line, shown with its neighbours.
    try await h.turn(Scripted("", editFile("~/long.txt", "\nline 9x\nline 10", "y")))
    #expect(try h.toolResults().last!.text.hasSuffix("最接近的幾行（照這裡的字寫 old）：\n     8\tline 8\n     9\tline 9\n    10\tline 10"))
}

@Test func fileToolsAreLockedOutsideASessionAndRefuseNonUTF8Files() async throws {
    let h = try await Harness()
    h.vm.put(todoPath, todoBefore)
    await h.doll.ownerSaid("看一下 todo")
    try await h.turn(Scripted("", readFile(todoPath), writeFile("~/a.txt", "a"), editFile(todoPath, "買牛奶", "買豆漿")))
    #expect(try h.toolResults().suffix(3).map(\.text) == ["失敗：read_file 現在鎖著：先呼叫 use_computer(expect) 打開電腦選單",
                                                          "失敗：write_file 現在鎖著：先呼叫 use_computer(expect) 打開電腦選單",
                                                          "失敗：edit_file 現在鎖著：先呼叫 use_computer(expect) 打開電腦選單"])
    #expect(h.vm.file(todoPath) == todoBefore && h.vm.file("~/a.txt") == nil)
    try await h.turn(Scripted("", useComputer))
    h.vm.state.withLock { $0.files["/Users/doll/bin.dat"] = Data([0xFF, 0xFE, 0x41]) }
    try await h.turn(Scripted("", readFile("~/bin.dat"), editFile("~/bin.dat", "A", "B")))
    let text = "失敗：~/bin.dat 不是 UTF-8 文字檔（3 bytes），read_file、write_file、edit_file 只處理文字檔"
    #expect(try h.toolResults().suffix(2).map(\.text) == [text, text])
    #expect(h.vm.state.withLock { $0.files["/Users/doll/bin.dat"] } == Data([0xFF, 0xFE, 0x41]))
}

@Test func fileStepsRecordThePathOnlyAndMatchByPath() async throws {
    let h = try await Harness()
    h.vm.put(todoPath, todoBefore)
    await h.doll.ownerSaid("改 todo")
    try await h.turn(Scripted("", ("use_computer", [("expect", "todo.txt 第一行是買豆漿")])))
    try await h.turn(Scripted("", readFile(todoPath), writeFile("~/Desktop/a.txt", "一\n二\n"), editFile(todoPath, "沒有這段", "x"),
                              editFile(todoPath, "買牛奶", "買豆漿"), outcome(true, "第一行是買豆漿")))
    #expect(h.last.experience == "經驗：改 todo\n第一行是買豆漿\n步驟：\n1. read_file ~/Documents/todo.txt\n2. write_file ~/Desktop/a.txt（2 行）\n3. edit_file ~/Documents/todo.txt")
    func same(_ a: String, _ b: String) -> Bool { StepShape(a).matches(StepShape(b)) }
    #expect(same("write_file ~/a.txt（2 行）", "write_file ~/a.txt（5 行）") && same("read_file ~/a.txt", "read_file ~/a.txt"))
    #expect(!same("read_file ~/a.txt", "edit_file ~/a.txt") && !same("read_file ~/a.txt", "read_file ~/b.txt") && !same("read_file ~/a.txt", "type 「~/a.txt」"))
    #expect(Doll.matchedSteps(["read_file ~/t.txt", "edit_file ~/t.txt"], ["read_file ~/t.txt", "write_file ~/t.txt（3 行）", "edit_file ~/t.txt"]) == 2)
}

/// `vmpal` replaced by this Mac: `exec` runs the driver's `/bin/sh -c` script here with a temporary
/// HOME, `cp` copies the file. The shell fragments the driver sends to the VM are exercised for real.
private func localVMPal(home: URL) -> VMPalRun {
    { args in
        switch args[0] {
        case "cp":
            let to = String(args[2].split(separator: ":", maxSplits: 1)[1])
            try FileManager.default.copyItem(at: URL(filePath: args[1]), to: URL(filePath: to))
            return ""
        case "exec":    // exec <vm> -- /bin/sh -c <script> sh <args…>
            let p = Process()
            p.executableURL = URL(filePath: args[3])
            p.arguments = Array(args.dropFirst(4))
            p.environment = ["HOME": home.path, "PATH": "/usr/bin:/bin"]
            p.currentDirectoryURL = home
            let out = Pipe(), err = Pipe()
            p.standardOutput = out
            p.standardError = err
            try p.run()
            let o = out.fileHandleForReading.readDataToEndOfFile(), e = err.fileHandleForReading.readDataToEndOfFile()
            p.waitUntilExit()
            let text = String(decoding: o, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
            guard p.terminationStatus == 0 else {
                let stderr = String(decoding: e, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
                throw ComputerError(stderr.isEmpty ? text : stderr)
            }
            return text
        default:
            throw ComputerError("unexpected vmpal \(args)")
        }
    }
}

@Test func theVMPalDriverMovesFileBytesExactlyThroughCpAndBase64() async throws {
    let home = FileManager.default.temporaryDirectory.appending(path: "dollos-vmhome-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: home) }
    var cfg = ComputerConfig()
    cfg.vmID = "E6813EBA"
    let d = VMPalDriver(cfg, vmpal: localVMPal(home: home))
    // Written through a folder that did not exist; CR, non-ASCII, two trailing newlines all arrive.
    let bytes = Data("a\n\n你\r\n-x\n\n".utf8)
    try await d.writeFile("~/Documents/todo.txt", bytes)
    #expect(try Data(contentsOf: home.appending(path: "Documents/todo.txt")) == bytes)
    #expect(try await d.readFile("~/Documents/todo.txt") == bytes)
    // A relative path is under the home; ~ alone is the home; an empty file reads back empty.
    try await d.writeFile("notes/a.txt", Data())
    #expect(FileManager.default.fileExists(atPath: home.appending(path: "notes/a.txt").path))
    #expect(try await d.readFile(home.appending(path: "notes/a.txt").path) == Data())
    await #expect(throws: ComputerError.self) { try await d.readFile("~") }
    // The driver's own error texts.
    func failure(_ body: () async throws -> Void) async -> String {
        do { try await body() } catch { return "\(error)" }
        return "no error"
    }
    #expect(await failure { _ = try await d.readFile("~/nope.txt") } == "沒有這個檔案：~/nope.txt")
    #expect(await failure { _ = try await d.readFile("~/Documents") } == "~/Documents 是資料夾，不是檔案")
    #expect(await failure { try await d.writeFile("~/Documents", Data("x".utf8)) } == "~/Documents 是資料夾，不能當檔案寫")
    #expect(try Data(contentsOf: home.appending(path: "Documents/todo.txt")) == bytes)
}

// pictures by reference (spec 3.1, 2026-11-09) ---------------------------------------------------

/// A Strata stand-in: answers /health (with or without `image_hash_refs`), /v1/models, and chat
/// completions; records each chat request's image URLs; holds the keys of pictures it has received
/// as bytes; `forget` drops keys as an eviction or a restart would.
private final class FakeStrata: Sendable {
    let hashRefs: Bool
    struct S { var held: Set<String> = []; var requests: [[String]] = []; var bodies: [Int] = [] }
    let s = Mutex(S())
    init(hashRefs: Bool) { self.hashRefs = hashRefs }
    func forget(_ keys: [String]) { s.withLock { st in for k in keys { st.held.remove(k) } } }
    var transport: HTTPMainModel.Transport {
        { [self] req in
            let path = req.url!.path
            if path == "/health" { return (Data(#"{"status":"ok","image_hash_refs":\#(hashRefs)}"#.utf8), 200) }
            if path == "/v1/models" { return (Data(#"{"data":[{"id":"m"}]}"#.utf8), 200) }
            let body = try JSONSerialization.jsonObject(with: req.httpBody!) as! [String: Any]
            let urls = (body["messages"] as! [[String: Any]]).flatMap { m -> [String] in
                guard let parts = m["content"] as? [[String: Any]] else { return [] }
                return parts.compactMap { ($0["image_url"] as? [String: Any])?["url"] as? String }
            }
            return s.withLock { st in
                st.requests.append(urls); st.bodies.append(req.httpBody!.count)
                let missing = urls.filter { $0.hasPrefix("hash:") }.map { String($0.dropFirst(5)) }.filter { !st.held.contains($0) }
                if !missing.isEmpty {
                    let list = missing.map { "\"\($0)\"" }.joined(separator: ",")
                    return (Data(#"{"error":{"type":"invalid_request_error","code":"image_not_cached","missing":[\#(list)]}}"#.utf8), 400)
                }
                for u in urls where u.hasPrefix("data:") { st.held.insert(HTTPMainModel.imageKey(u)!) }
                return (Data(#"{"choices":[{"message":{"content":"好"},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1}}"#.utf8), 200)
            }
        }
    }
}

@Test func picturesTheServerHoldsGoOutAsHashReferencesAndAMissIsResentOnce() async throws {
    // Screenshot-sized stand-ins (80 KB each, like a quality-40 JPEG), so the request sizes mean something.
    let bytes = { (n: UInt8) in Data([0xFF, 0xD8, 0xFF] + [UInt8](repeating: n, count: 80_000)) }
    let jpeg = { (n: UInt8) in "data:image/jpeg;base64," + bytes(n).base64EncodedString() }
    let a = jpeg(1), b = jpeg(2), c = jpeg(3)
    let key = { (uri: String) in HTTPMainModel.imageKey(uri)! }
    // The key is the server's: the first 32 hex digits of the sha256 of the picture's bytes.
    #expect(key(a) == String(SHA256.hash(data: bytes(1)).map { String(format: "%02x", $0) }.joined().prefix(32)))
    let turn = { (uris: [String]) in [ChatMessage(role: "system", content: "s")]
        + uris.map { ChatMessage(role: "tool", content: "r", extra: [.image($0)]) } }

    let server = FakeStrata(hashRefs: true)
    let model = HTTPMainModel(LlmConfig(baseURL: "http://strata", model: "m"), transport: server.transport)
    try await model.check()
    #expect(model.imageHashRefs)
    _ = try await model.chat(turn([a, b]), effort: "none")
    let two = try await model.chat(turn([a, b, c]), effort: "none")
    // Turn 1: both as bytes. Turn 2: a and b by reference, c as bytes; the body is smaller than all three.
    #expect(server.s.withLock { $0.requests } == [[a, b], ["hash:\(key(a))", "hash:\(key(b))", c]])
    #expect(two.usage.requestBytes! < server.s.withLock { $0.bodies[0] } + c.count)
    // The server drops a and b (an eviction or a restart): one 400 naming them, then the same request
    // with a and b as bytes and c still by reference.
    server.forget([key(a), key(b)])
    _ = try await model.chat(turn([a, b, c]), effort: "none")
    #expect(server.s.withLock { Array($0.requests.suffix(2)) }
            == [["hash:\(key(a))", "hash:\(key(b))", "hash:\(key(c))"], [a, b, "hash:\(key(c))"]])

    // A server without the flag gets every picture as bytes, every turn.
    let plain = FakeStrata(hashRefs: false)
    let old = HTTPMainModel(LlmConfig(baseURL: "http://strata", model: "m"), transport: plain.transport)
    try await old.check()
    _ = try await old.chat(turn([a, b]), effort: "none")
    _ = try await old.chat(turn([a, b, c]), effort: "none")
    #expect(!old.imageHashRefs && plain.s.withLock { $0.requests } == [[a, b], [a, b, c]])
}

// dashboard (2026-11-09) -------------------------------------------------------

private func json<T: Decodable>(_ type: T.Type, _ r: Dashboard.Response) throws -> T {
    #expect(r.status == 200, "\(String(decoding: r.body, as: UTF8.self))")
    return try JSONDecoder().decode(T.self, from: r.body)
}

/// The dashboard on a harness; port 0 picks a free one when it is started.
private func dashboard(_ h: Harness, items: Int = 200) -> Dashboard {
    var c = DashboardConfig()
    c.port = 0
    c.timelineItems = items
    return Dashboard(doll: h.doll, config: c)
}

/// Every memory file under the harness's memory dir, with its bytes.
private func memoryFiles(_ h: Harness) throws -> [String: Data] {
    let dir = h.dir.appending(path: "memory")
    var out: [String: Data] = [:]
    for sub in try FileManager.default.contentsOfDirectory(atPath: dir.path) {
        for name in (try? FileManager.default.contentsOfDirectory(atPath: dir.appending(path: sub).path)) ?? [] {
            out["\(sub)/\(name)"] = try Data(contentsOf: dir.appending(path: sub).appending(path: name))
        }
    }
    return out
}

@Test func dashboardEndpointsShowAScriptedRunAndChangeNothing() async throws {
    let h = try await Harness()
    let d = dashboard(h)
    await h.doll.ownerSaid("用電腦看一下 macOS 版本")
    try await h.turn(Scripted("好，我看看", useComputer,
                              remember(folders: [("我的信箱密碼是 hunter2", "own", nil), ("主人怕打雷", "secret", "happened"),
                                                 ("天空是藍的", "public", nil)])))
    h.vm.setFrame(1)
    try await h.turn(Scripted("", key("cmd+space")))

    let s = try json(DashboardStatus.self, await d.respond(method: "GET", target: "/api/status"))
    #expect(s.scenario == "電腦" && s.expect == "畫面上看得到結果" && s.watch == nil)
    #expect(s.state == "asleep" && !s.paused && s.turn == 2 && s.queued == 0 && s.effort == "medium" && s.contextTokens > 0)

    let t = try json([DashboardItem].self, await d.respond(method: "GET", target: "/api/timeline"))
    #expect(t.map(\.type) == ["turn", "turn", "owner"])
    #expect(t[2].text == "用電腦看一下 macOS 版本" && t[2].voice == false)
    #expect(t[1].thought == "好，我看看" && t[1].calls?.map(\.name) == ["use_computer", "remember"])
    #expect(t[1].calls?.allSatisfy { $0.result != nil } == true && t[1].calls?[0].image != nil && t[1].calls?[1].image == nil)
    #expect(t[1].calls?[1].result?.contains("own/") == true)
    #expect(t[0].calls?.map(\.name) == ["key"] && t[0].calls?[0].parameters.map(\.name) == ["key", "modifiers"])
    for turn in t.prefix(2) {
        #expect(turn.prompt != nil && turn.prompt! > 0 && turn.seconds != nil && turn.effort == "medium" && turn.footer?.hasPrefix("【狀況】") == true)
    }
    let limited = try json([DashboardItem].self, await dashboard(h, items: 1).respond(method: "GET", target: "/api/timeline"))
    #expect(limited.map(\.seq) == [t[0].seq])

    let v = try json(DashboardVision.self, await d.respond(method: "GET", target: "/api/vision"))
    let last = try h.toolResults().last!
    #expect(v.image == last.image && v.crop == last.crop && v.window == last.window && v.cropNote == last.cropNote)
    #expect(v.window?.contains("frame 1") == true && v.windows == fakeWindowsLine)

    let m = try json(DashboardMemory.self, await d.respond(method: "GET", target: "/api/memory"))
    let byFolder = Dictionary(uniqueKeysWithValues: m.folders.map { ($0.folder, $0.entries) })
    #expect(m.folders.map(\.folder) == ["own", "public", "secret"])
    #expect(byFolder["own"]?.map(\.title) == ["我的信箱密碼是 hunter2"])
    #expect(byFolder["secret"]?.map(\.kind) == ["happened"] && byFolder["secret"]?.first?.tier == "owner")
    #expect(byFolder["public"]?.map(\.text) == ["天空是藍的"] && byFolder["public"]?.first?.level == "確定")
    #expect(m.attached.isEmpty && m.cheatsheets.isEmpty)

    // Every route, the page and the stream excepted, changes neither the timeline nor a memory file.
    let before = (try h.events(), try memoryFiles(h))
    for r in Dashboard.routes where r.kind != .events {
        _ = await d.respond(method: r.method, target: r.path)
    }
    _ = await d.respond(method: "POST", target: "/api/resume")
    let after = (try h.events(), try memoryFiles(h))
    #expect(after.0 == before.0 && after.1 == before.1)
    #expect(await d.respond(method: "GET", target: "/api/pause").status == 405)
    #expect(await d.respond(method: "POST", target: "/api/status").status == 405)
    #expect(await d.respond(method: "POST", target: "/api/say").status == 404)
}

@Test func theOnlyPostRoutesAreInterruptPauseAndResume() {
    #expect(Set(Dashboard.routes.filter { $0.method != "GET" }.map(\.path)) == ["/api/interrupt", "/api/pause", "/api/resume"])
    #expect(Dashboard.routes.allSatisfy { ["GET", "POST"].contains($0.method) })
}

@Test func theShotRouteServesOneScreenshotByNameAndRefusesPaths() async throws {
    let h = try await Harness()
    let d = dashboard(h)
    await h.doll.ownerSaid("開電腦")
    try await h.turn(Scripted("", useComputer))
    let name = try h.toolResults().last!.image!
    let ok = await d.respond(method: "GET", target: "/shots/\(name)")
    let stored = try Data(contentsOf: h.doll.shots.appending(path: name))
    #expect(ok.status == 200 && ok.body == stored)
    #expect(ok.contentType == "image/jpeg")
    for bad in ["../doll.db", "..%2Fdoll.db", "%2E%2E%2Fdoll.db", "/etc/passwd", "%2Fetc%2Fpasswd", "..", "...", ".hidden",
                "a/../../doll.db", "a%5C..%5Cdoll.db", ""] {
        #expect(await d.respond(method: "GET", target: "/shots/\(bad)").status == 400, "\(bad)")
    }
    #expect(await d.respond(method: "GET", target: "/shots/nothere.jpg").status == 404)
}

@Test func interruptCancelsTheCallInFlightAndNothingRerunsIt() async throws {
    let h = try await Harness([Scripted("", say("好"))])
    let d = dashboard(h)
    let doll = h.doll
    #expect(await d.respond(method: "POST", target: "/api/interrupt").status == 409)   // nothing in flight
    await h.model.stall()
    let runner = Task { try await doll.run() }
    await doll.ownerSaid("算一下")
    while await h.model.requests.count < 1 { try await Task.sleep(for: .milliseconds(10)) }
    #expect(try json(DashboardStatus.self, await d.respond(method: "GET", target: "/api/status")).state == "thinking")
    #expect(try json([String: Bool].self, await d.respond(method: "POST", target: "/api/interrupt")) == ["interrupted": true])
    try await Task.sleep(for: .milliseconds(300))
    #expect(await h.model.requests.count == 1 && h.said.isEmpty)                    // not rerun
    #expect(try h.events().map(\.kind) == [.owner])                           // no reply stored
    let s = try json(DashboardStatus.self, await d.respond(method: "GET", target: "/api/status"))
    #expect(s.state == "asleep" && s.turn == 0 && s.lastInterrupt != nil)
    #expect(h.logs.contains { $0.contains("turn 1 interrupted from the dashboard") })
    await doll.ownerSaid("好了嗎")                                                  // the next event gets a turn
    while h.said.isEmpty { try await Task.sleep(for: .milliseconds(10)) }
    #expect(await h.model.requests.count == 2 && h.said == ["好"] && h.reports.map(\.index) == [1])
    await doll.stop()
    try await runner.value
}

@Test func pauseStopsNewTurnsWhileEventsQueueAndResumeRunsThem() async throws {
    let h = try await Harness([Scripted("", say("兩件都收到"))])
    let d = dashboard(h)
    let doll = h.doll
    let runner = Task { try await doll.run() }
    #expect(try json([String: Bool].self, await d.respond(method: "POST", target: "/api/pause")) == ["paused": true])
    await doll.ownerSaid("第一件")
    await doll.ownerSaid("第二件")
    try await Task.sleep(for: .milliseconds(300))
    #expect(await h.model.requests.isEmpty)
    let s = try json(DashboardStatus.self, await d.respond(method: "GET", target: "/api/status"))
    #expect(s.paused && s.queued == 2 && s.state == "asleep")
    #expect(try json([DashboardItem].self, await d.respond(method: "GET", target: "/api/timeline")).isEmpty)   // not drained
    #expect(try json([String: Bool].self, await d.respond(method: "POST", target: "/api/resume")) == ["paused": false])
    while h.said.isEmpty { try await Task.sleep(for: .milliseconds(10)) }
    let req = await h.model.seen(0)
    #expect(req.contains { $0.content.contains("第一件") } && req.contains { $0.content.contains("第二件") })
    #expect(await h.model.requests.count == 1 && h.said == ["兩件都收到"])
    await doll.stop()
    try await runner.value
}

@Test func theServerAnswersOverHTTPAndStreamsAChangePerEvent() async throws {
    let h = try await Harness(["嗨"])
    let d = dashboard(h)
    let port = try await d.start()
    defer { d.stop() }
    let base = "http://127.0.0.1:\(port)"
    let (page, pageResponse) = try await URLSession.shared.data(from: URL(string: base + "/")!)
    #expect((pageResponse as! HTTPURLResponse).statusCode == 200)
    #expect(String(decoding: page, as: UTF8.self).contains("EventSource(\"/events\")"))
    var post = URLRequest(url: URL(string: base + "/api/pause")!)
    post.httpMethod = "POST"
    let (_, postResponse) = try await URLSession.shared.data(for: post)
    let paused = await h.doll.paused
    #expect((postResponse as! HTTPURLResponse).statusCode == 200 && paused)

    let (bytes, sse) = try await URLSession.shared.bytes(from: URL(string: base + "/events")!)
    #expect((sse as! HTTPURLResponse).value(forHTTPHeaderField: "Content-Type") == "text/event-stream")
    var lines = bytes.lines.makeAsyncIterator()
    func next() async throws -> String? {   // the blank line ending each event may or may not be yielded
        while let line = try await lines.next() { if !line.isEmpty { return line } }
        return nil
    }
    #expect(try await next() == "event: change")
    #expect(try await next() == "data: {\"what\":\"hello\"}")
    await h.doll.ownerSaid("嗨")                                                   // queued while paused
    #expect(try await next() == "event: change")
    #expect(try await next() == "data: {\"what\":\"queue\"}")
}

@Test func dashboardConfigRejectsUnknownKeysAndBadValues() throws {
    let file = FileManager.default.temporaryDirectory.appending(path: "dollcore-cfg-\(UUID().uuidString).toml")
    let head = "[llm]\nbase_url = \"http://x\"\nmodel = \"m\"\n[computer]\nvm_id = \"V\"\n[memory]\n"
        + SourceTier.allCases.map { "confidence_\($0.rawValue) = 0.5\n" }.joined()
    try (head + "[dashboard]\nport = 9000\nbind = \"127.0.0.1\"\ntimeline_items = 5\nenabled = false\n")
        .write(to: file, atomically: true, encoding: .utf8)
    let c = try Config.load(file).dashboard
    #expect(c.port == 9000 && c.bind == "127.0.0.1" && c.timelineItems == 5 && !c.enabled)
    for bad in ["typing = true", "port = 0", "port = 70000", "bind = \"\"", "timeline_items = 0"] {
        try (head + "[dashboard]\n\(bad)\n").write(to: file, atomically: true, encoding: .utf8)
        #expect(throws: TOMLError.self, "\(bad)") { try Config.load(file) }
    }
    let example = try Config.load(root.appending(path: "dollcore.example.toml")).dashboard
    #expect(example.enabled && example.port == 8790 && example.bind == "127.0.0.1")
}

@Test func actionResultsLeaveOutVMPalsBookkeepingLines() {
    // 2026-11-10: these lines were most of a result's ~90 tokens and are kept in every later turn.
    let vmpal = "Done: 1 action in 0.7 s. Screen 1280×800 (screenshot pixels), frame 8386.\nleft_click at (558, 328).\n"
        + "Nothing changed near the left_click at (558, 328): it may have missed, or what's there doesn't respond.\nChanged: 43,0–1110,569."
    #expect(Doll.condense(vmpal) == "Nothing changed near the left_click at (558, 328): it may have missed, or what's there doesn't respond.\nChanged: 43,0–1110,569.")
    #expect(Doll.condense("Done: 1 action in 1.4 s. Screen 1280×800 (screenshot pixels), frame 8479.\nTyped 24 characters.\nChanged: 1,2–3,4.") == "Changed: 1,2–3,4.")
    #expect(Doll.condense("Done: 1 action in 0.6 s.\ndouble_click on “notes.txt” at (441, 235).") == "")
    #expect(Doll.condense("something VMPal says that is not bookkeeping") == "something VMPal says that is not bookkeeping")
}

// timeline files and today's recall (2026-11-10) ------------------------------------

/// A second Doll on the same data dir: a new process.
private func restarted(_ h: Harness, _ replies: [Scripted] = []) async throws -> Harness {
    try await Harness(replies) {
        $0.core.dataDir = h.dir.path
        $0.memory.dir = h.dir.appending(path: "memory").path
    }
}

private func hhmm(_ ts: Double) -> String {
    let c = Calendar.current.dateComponents([.hour, .minute], from: Date(timeIntervalSince1970: ts))
    return String(format: "%02d:%02d", c.hour!, c.minute!)
}

@Test func aRestartedDollRebuildsTheSameRequestFromTheTimelineFile() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("把 notes 存檔")
    try await h.turn(Scripted("好，我來", useComputer))
    try await h.turn(Scripted("", key("cmd+s")))
    try await h.turn(Scripted("", say("存好了。"), outcome(true, "按 ⌘S 存了檔。")))
    await h.doll.ownerSaid("謝謝")
    try await h.turn(Scripted("", say("不客氣")))
    await h.doll.ownerSaid("再開一次電腦")
    try await h.turn(Scripted("", useComputer))                                    // an open scenario, with images
    let events = await h.doll.timelineEvents()
    let before = try await h.doll.context(events)
    #expect(before.contains { !$0.images.isEmpty })                                // screenshots in it
    #expect(before.filter { $0.content.hasPrefix("【狀況】") }.count == 3)          // the footers of replies outside the closed span
    #expect(before.contains { $0.content == "成功。你自己記下的經過：\n按 ⌘S 存了檔。" })   // the closed span

    // The file: one JSON object per line, every record type.
    let file = try h.timeline().file
    let lines = try String(contentsOf: file, encoding: .utf8).split(separator: "\n")
    let types = try lines.map { try JSONSerialization.jsonObject(with: Data($0.utf8)) as! [String: Any] }.map { $0["type"] as! String }
    #expect(Set(types) == ["event", "footer", "turn", "scenario"])
    #expect(file.deletingLastPathComponent().lastPathComponent == "timeline" && file.pathExtension == "jsonl")

    let again = try await restarted(h, [Scripted("", say("回來了"))])
    #expect(await again.doll.timelineEvents() == events)
    let after = try await again.doll.context(await again.doll.timelineEvents())
    #expect(try ChatMessage.encoder.encode(after) == ChatMessage.encoder.encode(before))
    #expect(try h.timeline().closedScenarios.count == 1)
    // Its first request continues the old one byte for byte.
    await again.doll.ownerSaid("在嗎")
    try await again.turn()
    let sent = await again.model.seen(0)
    #expect(try ChatMessage.encoder.encode(Array(sent.prefix(before.count))) == ChatMessage.encoder.encode(before))
    let kinds = try h.events().map(\.kind)
    // One timeline: the owner's line, her reply, its result (the experience her outcome wrote is not
    // related to 「在嗎」: nothing is attached).
    #expect(kinds.count == events.count + 3 && kinds.suffix(3) == [.owner, .doll, .tool])
    await h.doll.stop()
}

@Test func aLineCutShortByACrashIsDroppedAndTheNextOneStartsClean() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("你好")
    try await h.turn(Scripted("", say("嗨")))
    let file = try h.timeline().file
    let whole = try Data(contentsOf: file)
    let handle = try FileHandle(forWritingTo: file)
    try handle.seekToEnd()
    try handle.write(contentsOf: Data(#"{"type":"event","seq":99,"te"#.utf8))
    try handle.close()
    let t = try h.timeline()                                                        // reopening drops the cut line
    #expect(try Data(contentsOf: file) == whole)
    _ = try t.append(Event(.owner, .owner, "再一句"))
    #expect(try h.events().last?.text == "再一句" && h.events().last?.seq == t.events.count.int64)
}

private extension Int { var int64: Int64 { Int64(self) } }

extension Doll {
    func todayCount() throws -> Int { try today.count() }
    func todayFTS(_ match: String) throws -> [String] { try today.fts(match: match, secret: true).map(\.id) }
}

@Test func recallFindsTodaysEventsByWordsAndByMeaningWithTheirLabels() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("今天下午要去郵局寄包裹給阿嬤")
    try await h.turn(Scripted("要記得提醒主人帶身分證", say("好，出門前我提醒你"), think("medium")))
    await h.doll.ownerSaid("晚餐想吃咖哩飯")
    try await h.turn(Scripted("", say("我也想吃")))
    await h.doll.ownerSaid("明天早上九點開會")
    try await h.turn(Scripted("", say("記下了")))
    await h.doll.put(Event(.watch, .internal, "盯著的區域變了：下載完成"))
    try await h.turn(Scripted("", say("下載好了")))
    let events = await h.doll.timelineEvents()
    func ts(_ text: String) -> String { hhmm(events.first { $0.text.contains(text) }!.ts) }

    // By words: shared trigrams.
    let words = try await h.doll.recall("郵局寄包裹", seen: events).text
    #expect(words.hasPrefix("郵局寄包裹："))
    #expect(words.contains("〔今天 \(ts("郵局寄包裹"))｜主人說〕\n今天下午要去郵局寄包裹給阿嬤"))
    let reply = events.first { $0.kind == .doll }!
    #expect(try await h.doll.recall("提醒主人帶身分證", seen: events).text.contains("〔今天 \(hhmm(reply.ts))｜你想〕\n要記得提醒主人帶身分證"))
    #expect(try await h.doll.recall("出門前提醒", seen: events).text.contains("〔今天 \(hhmm(reply.ts))｜你說〕\n好，出門前我提醒你"))
    #expect(try await h.doll.recall("下一回合 effort", seen: events).text.contains("｜think 的結果〕\n下一回合 effort：medium"))
    #expect(try await h.doll.recall("下載完成", seen: events).text.contains("〔今天 \(ts("下載完成"))｜watch〕\n盯著的區域變了：下載完成"))
    // By meaning: no trigram in common, the embedding finds it.
    #expect(try await h.doll.todayFTS("\"寄東西\" OR \"東西給\" OR \"給奶奶\"").isEmpty)
    let meaning = try await h.doll.recall("寄東西給奶奶", seen: events).text
    #expect(meaning.contains("〔今天 \(ts("郵局寄包裹"))｜主人說〕\n今天下午要去郵局寄包裹給阿嬤"))
    // Results of recall itself are not indexed (copies of other data).
    try await h.turn(Scripted("", recall("咖哩")))
    #expect(try await h.doll.recall("咖哩", seen: await h.doll.timelineEvents()).text.components(separatedBy: "｜recall 的結果〕").count == 1)
}

@Test func aSecretEventOfTodayIsHiddenFromATurnThatSawNoSecret() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("保險箱密碼是 7391，別說出去", secret: true)
    try await h.turn(Scripted("", say("好")))
    await h.doll.ownerSaid("今天天氣很好")
    try await h.turn(Scripted("", say("對啊")))
    let events = await h.doll.timelineEvents()
    // A turn that saw a secret (this timeline's): the secret line is there and marks the result.
    let seen = try await h.doll.recall("保險箱密碼", seen: events)
    #expect(seen.text.contains("｜主人說〕\n保險箱密碼是 7391，別說出去") && seen.secret)
    // A turn that saw none: neither the line nor her reply that saw it (also secret) comes back.
    let unseen = try await h.doll.recall("保險箱密碼", seen: events.filter { !$0.secret })
    #expect(!unseen.text.contains("7391") && !unseen.secret)
    #expect(unseen.text.contains("今天天氣很好") || unseen.text.hasSuffix("0 條"))
}

@Test func aRestartBuildsTheTodayIndexFromTheTimelineWithTheSameAnswers() async throws {
    let h = try await Harness()
    for (line, answer) in [("今天下午要去郵局寄包裹給阿嬤", "好"), ("晚餐想吃咖哩飯", "我也想吃"), ("明天早上九點開會", "記下了")] {
        await h.doll.ownerSaid(line)
        try await h.turn(Scripted("想一下", say(answer)))
    }
    let queries = ["郵局寄包裹", "晚餐想吃什麼", "九點開會"]
    let events = await h.doll.timelineEvents()
    var before: [String] = []
    for q in queries { before.append(try await h.doll.recall(q, seen: events).text) }
    let count = try await h.doll.todayCount()
    #expect(count == 9)                                                              // 3 owner lines, 3 says, 3 thoughts
    await h.doll.stop()
    let again = try await restarted(h)
    // In memory only (2026-11-11): nothing of it on disk, rebuilt from the timeline file.
    #expect(try FileManager.default.contentsOfDirectory(atPath: h.dir.path).filter { $0.hasPrefix("today") }.isEmpty)
    #expect(try await again.doll.todayCount() == count)
    var after: [String] = []
    for q in queries { after.append(try await again.doll.recall(q, seen: events).text) }
    #expect(before == after)
    #expect(before.allSatisfy { $0.contains("〔今天 ") })
}

@Test func theTodayIndexLeavesOutMenusAndRecallResults() {
    // 2026-11-11: use_computer's result is the program's menu text, not something that happened.
    func result(_ tool: String) -> [String] {
        var ev = Event(.tool, .internal, "電腦選單打開了，現在多了這些工具：…")
        ev.seq = 7
        return TodayIndex.entries(ev, tool: tool).map(\.label)
    }
    #expect(result("use_computer").isEmpty && result("recall").isEmpty)
    #expect(result("read_file") == ["read_file 的結果"])
}

@Test func callsWrittenInsideTheThinkingAreHerReply() {
    // A real reply (2026-11-11, replaying F-1 turn 3): no </think>, so content was null and the call sat in the thinking.
    let thinking = "I found the file in ~/Documents/notes.txt, so now I need to open it in TextEdit and save a copy to the Desktop as requested. I'll use the Terminal to launch TextEdit with the file path, then use Save As to create the copy on the Desktop.\n\n<tool_call>\n<function=say>\n<parameter=text>\n找到notes.txt在Documents裡，內容三行。我開TextEdit弄一下。\n</parameter>\n</function>\n</tool_call>"
    let reply = ParsedReply(ParsedReply.rescued(content: "", reasoning: thinking)!)
    #expect(reply.said == ["找到notes.txt在Documents裡，內容三行。我開TextEdit弄一下。"] && reply.thought.isEmpty)
    // Her content's own text stays her thought, the calls after it.
    #expect(ParsedReply(ParsedReply.rescued(content: "嗯", reasoning: thinking)!).thought == "嗯")
    // Not when the content has a call, the thinking has none, or the only one is in a code fence or mid-line.
    #expect(ParsedReply.rescued(content: "<tool_call>\n<function=think>\n<parameter=level>\nnone\n</parameter>\n</function>\n</tool_call>", reasoning: thinking) == nil)
    #expect(ParsedReply.rescued(content: "", reasoning: "just thinking") == nil && ParsedReply.rescued(content: "", reasoning: nil) == nil)
    #expect(ParsedReply.rescued(content: "", reasoning: "```\n<tool_call>\n```\nI would write <tool_call> here") == nil)
}

@Test func aMenuReplyWithoutACallIsRemindedUntilTheIdleLimit() async throws {
    // 2026-11-11: she had only thought (or written nothing) and the task stopped.
    let h = try await menuHarness { $0.idle.maxTurnsWithoutTool = 3 }
    try await h.turn("")
    #expect(await h.doll.followUp && h.last.actions == ["reminded"])
    #expect(try h.events().last?.text == Doll.noCallReminder)
    try await h.turn("我想想")
    #expect(await h.doll.followUp)
    try await h.turn("")                                                // the third in a row: the log line, no reminder
    #expect(!(await h.doll.followUp) && h.last.actions.isEmpty)
    #expect(h.logs.contains("INFO idle: 3 turns in a row without a tool call"))
    // A call resets the count.
    try await h.turn(Scripted("", key("cmd+s")))
    try await h.turn("")
    #expect(await h.doll.followUp)
}

@Test func aSayAfterOutcomeIsStillSaidAndKeptWhenTheMenuCloses() async throws {
    // 2026-11-11: she wrote say after outcome; the owner heard nothing.
    let h = try await menuHarness()
    try await h.turn(Scripted("", outcome(true, "存好了"), say("存好了喔"), key("cmd+q")))
    #expect(h.said == ["存好了喔"] && h.vm.actions.isEmpty)
    let end = Array(try h.toolResults().suffix(3))
    #expect(end[1].text == "" && end[2].text == "失敗：這次用電腦已經用 outcome 結束了；outcome 後面的呼叫沒有執行。")
    #expect(!(await h.doll.followUp))
    // After the menu closes, the say stays in her context.
    await h.doll.ownerSaid("謝謝")
    try await h.turn(Scripted("", say("不客氣")))
    let last = await h.model.requests.last!.messages
    #expect(last.contains { $0.role == "assistant" && $0.content == Scripted("", say("存好了喔")).raw })
}

// consolidation (2026-11-11) -----------------------------------------------------

/// The config a harness runs with, for consolidating its data dir.
private func consolidationConfig(_ h: Harness) throws -> Config {
    var cfg = try Config.load(root.appending(path: "dollcore.example.toml"))
    cfg.core.dataDir = h.dir.path
    cfg.memory.dir = h.dir.appending(path: "memory").path
    return cfg
}

/// An event as the timeline stores it, a minute after the one before.
private func stamped(_ kind: Event.Kind, _ text: String, _ seq: Int64, secret: Bool = false, image: String? = nil,
                     cropNote: String? = nil, crop: String? = nil, window: String? = nil) -> Event {
    var e = Event(kind, kind == .owner ? .owner : .internal, text, secret: secret, image: image, cropNote: cropNote,
                  crop: crop, window: window)
    e.seq = seq
    e.ts = 1794337154 + Double(seq) * 60
    return e
}

/// Every memory file under the harness's memory dir, parsed, by id.
private func memoryFilesByID(_ h: Harness) throws -> [String: (folder: String, file: MemoryFile)] {
    var out: [String: (String, MemoryFile)] = [:]
    for folder in ["secret", "own", "public"] {
        let dir = h.dir.appending(path: "memory/\(folder)")
        for name in (try? FileManager.default.contentsOfDirectory(atPath: dir.path)) ?? [] where name.hasSuffix(".md") {
            out[String(name.dropLast(3))] = (folder, try Memory.parseFile(String(contentsOf: dir.appending(path: name), encoding: .utf8)))
        }
    }
    return out
}

/// A retired consolidated memory file, retired `days` ago.
private func writeRetired(_ h: Harness, _ id: String, days: Double, body: String) throws {
    let f = MemoryFile(sourceSeq: 9, ts: 0, confidence: 0.6, kind: "", tier: .consolidated,
                       retired: Retirement(ts: Date().timeIntervalSince1970 - days * 86400, supersededBy: "x", reason: "舊"), body: body)
    let dir = h.dir.appending(path: "memory/public")
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    try f.text.write(to: dir.appending(path: "\(id).md"), atomically: true, encoding: .utf8)
}

@Test func consolidationRendersTheDayAsTextWithoutImagesOrProgramLines() {
    let events = [
        stamped(.owner, "用 TextEdit 另存一份", 1),
        stamped(.doll, Scripted("我先打開電腦。", useComputer).raw, 2),
        stamped(.tool, "電腦選單打開了，現在多了這些工具：…", 3, image: "a.jpg"),
        stamped(.memory, "【快捷鍵】TextEdit 1.21\nFile › Save As… = ⇧⌥⌘S", 4),
        stamped(.doll, Scripted("", say("好"), key("cmd+shift+alt+s")).raw, 5),
        stamped(.tool, "", 6),
        stamped(.tool, "開著的視窗：TextEdit「另存」；焦點：textfield", 7, image: "b.jpg", cropNote: "視窗圖在 10,20",
                crop: "c.jpg", window: "前景 app：TextEdit\nwindow \"Save\""),
        stamped(.memory, "【相關記憶】程式用主人的話查到的過去經驗：\n〔大概｜public〕\n經驗：x", 8),
        stamped(.doll, Scripted("", recall("另存")).raw, 9),
        stamped(.tool, "另存：1 條\n\n〔大概｜public〕\n舊的", 10),
        stamped(.doll, Scripted("", outcome(true, "按 ⇧⌥⌘S 另存好了")).raw, 11),
        stamped(.tool, "記下了：達成。", 12),
        stamped(.owner, "我的密碼是 1234", 13, secret: true),
        stamped(.watch, "盯著的畫面變了。", 14, image: "d.jpg"),
    ]
    func t(_ seq: Int) -> String { hhmm(events[seq - 1].ts) }
    let tasks = Consolidation.tasks(events)
    #expect(tasks.count == 2 && tasks.allSatisfy(\.owner))
    #expect(tasks[0].text == """
        [\(t(1)) 主人] 用 TextEdit 另存一份
        [\(t(2)) 她想] 我先打開電腦。
        [\(t(2)) 她呼叫] use_computer(expect: 畫面上看得到結果)
        [\(t(5)) 她說] 好
        [\(t(5)) 她呼叫] key(key: s; modifiers: ["cmd", "shift", "alt"])
        [\(t(7)) key 的結果] 開著的視窗：TextEdit「另存」；焦點：textfield
        [\(t(9)) 她呼叫] recall(query: 另存)
        [\(t(11)) 她呼叫] outcome(success: true; summary: 按 ⇧⌥⌘S 另存好了)
        [\(t(12)) outcome 的結果] 記下了：達成。
        """)
    #expect(tasks[0].queries == ["用 TextEdit 另存一份", "按 ⇧⌥⌘S 另存好了"])
    #expect(tasks[1].text == "[\(t(13)) 主人｜秘密] 我的密碼是 1234\n[\(t(14)) watch] 盯著的畫面變了。")
    // Nothing of the images, the window outline, the program's lines, recall's copy or the menu text.
    let all = tasks.map(\.text).joined(separator: "\n")
    for gone in [".jpg", "視窗圖在", "window \"Save\"", "【快捷鍵】", "【相關記憶】", "另存：1 條", "電腦選單打開了"] {
        #expect(!all.contains(gone), "\(gone)")
    }
    // Events before the first owner line are a task of their own.
    let early = Consolidation.tasks([stamped(.watch, "醒了", 1), stamped(.owner, "嗨", 2)])
    #expect(early.map(\.owner) == [false, true] && early[0].queries.isEmpty)
}

@Test func consolidationAppliesAddUpdateRetireAndTheNextStartIsANewDay() async throws {
    let h = try await Harness()
    let cfg = try consolidationConfig(h)
    let memory = try Memory(config: cfg.memory, root: h.dir.appending(path: "memory"), store: h.store)
    let app = try memory.registry.create(.app, name: "TextEdit", aliases: [], description: "macOS 內建的文字編輯 app")
    let saveAs = try memory.write("TextEdit 另存要從 File 選單點 Save As…", folder: .public, sourceSeq: 1, tier: .doll, kind: nil,
                                  entities: [MemoryEntity(app.id, "TextEdit")])
    let cat = try memory.write("主人養貓", folder: .public, sourceSeq: 1, tier: .owner, kind: nil)
    let shortcut = try memory.write("TextEdit：File › Save As…＝⇧⌥⌘S（TextEdit 1.21）", folder: .public, sourceSeq: 0, tier: .program, kind: nil)
    let experience = try memory.write("經驗：另存\n步驟：\n1. key cmd+shift+alt+s", folder: .public, sourceSeq: 1, tier: .selfReported,
                                      kind: nil, experience: (task: "另存", stats: ExperienceStats(turns: 3, seconds: 20)))
    try writeRetired(h, "9-old000", days: 40, body: "退休很久的")
    try writeRetired(h, "9-new000", days: 5, body: "剛退休的")
    await h.doll.ownerSaid("用 TextEdit 另存一份")
    try await h.turn(Scripted("", say("好")))
    let before = try h.timeline().segment
    // n1 names the existing TextEdit entry by another language's name, and a new shortcut entry;
    // the update names a new entry; new3 is named by nothing.
    let reply = """
        整理好了。
        ```json
        {"entities": [
          {"ref": "new1", "type": "shortcut", "name": "⇧⌥⌘S", "aliases": ["shift+option+cmd+s"], "description": "TextEdit 的另存快捷鍵"},
          {"ref": "new2", "type": "other", "name": "橘貓", "aliases": [], "description": "主人養的貓"},
          {"ref": "new3", "type": "other", "name": "沒用到", "aliases": [], "description": "x"}
        ],
        "operations": [
          {"op": "add", "ref": "n1", "text": "TextEdit 按 ⇧⌥⌘S → 跳出另存對話框，檔名欄已選取", "folder": "public", "kind": "", "entities": [{"name": "文字編輯", "id": "\(app.id)"}, {"name": "⇧⌥⌘S", "id": "new1"}]},
          {"op": "add", "ref": "n2", "text": "主人今天要了一份副本", "folder": "secret", "kind": "happened", "entities": []},
          {"op": "update", "id": "\(cat)", "text": "主人養貓，是橘貓", "entities": [{"name": "橘貓", "id": "new2"}]},
          {"op": "retire", "id": "\(saveAs)", "superseded_by": "n1", "reason": "有更短的走法"}
        ]}
        ```
        """
    let model = FakeModel([Scripted(reply)])
    let r = try await Consolidation.run(config: cfg, root: root, model: model)

    // The request: no persona, high effort, the day as text, only memories she or consolidation
    // wrote, with their entities by registry id, and the registry entries near the day.
    let sent = await model.requests[0]
    #expect(sent.effort == "high" && sent.messages.map(\.role) == ["system", "user"])
    #expect(!sent.messages[0].content.contains("你是") && sent.messages[1].content.contains("[id: \(cat)｜public｜kind: ｜entities: []]\n主人養貓"))
    #expect(sent.messages[1].content.contains(#"[id: \#(saveAs)｜public｜kind: ｜entities: [{"id":"\#(app.id)","name":"TextEdit"}]]"#))
    #expect(sent.messages[1].content.contains("[id: \(app.id)｜app｜TextEdit｜別名：（沒有）] macOS 內建的文字編輯 app"))
    #expect(sent.messages[1].content.contains("] 用 TextEdit 另存一份") && sent.messages[1].content.contains("她說] 好"))
    #expect(!sent.messages[1].content.contains(shortcut) && !sent.messages[1].content.contains(experience))
    #expect(Set(r.related) == [saveAs, cat] && r.registry == [app.id] && r.plan.operations.count == 4)
    #expect(r.segment == before && r.newSegment > before)

    let files = try memoryFilesByID(h)
    let added = files.filter { $0.value.file.tier == .consolidated && $0.value.file.retired == nil }
    let n1 = try #require(added.first { $0.value.file.body == "TextEdit 按 ⇧⌥⌘S → 跳出另存對話框，檔名欄已選取" })
    let n2 = try #require(added.first { $0.value.file.body == "主人今天要了一份副本" })
    #expect(n1.value.folder == "public" && n1.value.file.kind == "" && n1.value.file.confidence == cfg.memory.confidence[.consolidated]!)
    #expect(n2.value.folder == "secret" && n2.value.file.kind == "happened")
    #expect(files[cat]?.file.body == "主人養貓，是橘貓" && files[cat]?.file.tier == .owner)
    // The registry: the existing entry got the other language's name as an alias; two new entries;
    // the unused one was not created.
    let entries = try memory.registry.all()
    let keys = try #require(entries.first { $0.name == "⇧⌥⌘S" })
    let pet = try #require(entries.first { $0.name == "橘貓" })
    #expect(entries.count == 3 && entries.first { $0.id == app.id }?.aliases == ["文字編輯"])
    #expect(keys.type == .shortcut && keys.aliases == ["shift+option+cmd+s"] && keys.description == "TextEdit 的另存快捷鍵")
    #expect(files[cat]?.file.entities == [MemoryEntity(pet.id, "橘貓")] && n2.value.file.entities.isEmpty)
    #expect(n1.value.file.entities == [MemoryEntity(app.id, "文字編輯"), MemoryEntity(keys.id, "⇧⌥⌘S")])
    #expect(try h.store.memoryEntities([n1.key, cat]) == [n1.key: [app.id, keys.id].sorted(), cat: [pet.id]])
    #expect(try memory.registry.exact("文字編輯").map(\.id) == [app.id])
    #expect(r.applied.contains("entity ref new3 (沒用到) not created: no operation names it"))
    #expect(r.applied.contains("alias \(app.id) += 文字編輯"))
    let retired = try #require(files[saveAs]?.file.retired)
    #expect(retired.supersededBy == n1.key && retired.reason == "有更短的走法" && abs(retired.ts - Date().timeIntervalSince1970) < 60)
    #expect(files[saveAs]?.file.body == "TextEdit 另存要從 File 選單點 Save As…")
    // Retired longer than retired_keep_days: deleted; retired 5 days ago: kept.
    #expect(files["9-old000"] == nil && files["9-new000"] != nil)
    #expect(r.applied.first == "deleted 9-old000 (retired more than 30 days ago)")
    #expect(r.applied.contains(#"add public/\#(n1.key) (ref n1, kind none, entities [{"id":"\#(app.id)","name":"文字編輯"},{"id":"\#(keys.id)","name":"⇧⌥⌘S"}]): TextEdit 按 ⇧⌥⌘S → 跳出另存對話框，檔名欄已選取"#))
    #expect(r.applied.contains("retire \(saveAs) (superseded by \(n1.key)): 有更短的走法"))
    // Retired: out of recall; the update and the new one are in (the index follows the files).
    let found = try memory.search("TextEdit 另存 Save As", visible: Set(MemoryFolder.allCases), limit: 50).map(\.id)
    #expect(!found.contains(saveAs) && found.contains(n1.key))
    #expect(try memory.search("橘貓", visible: [.public], limit: 50).first?.id == cat)
    let retiredFile = try String(contentsOf: h.dir.appending(path: "memory/public/\(saveAs).md"), encoding: .utf8)
    #expect(try Memory.parseFile(retiredFile).text == retiredFile)

    // The next start is a new day: a new, empty segment and an empty today; long-term memory stays.
    let next = try await restarted(h)
    #expect(try next.timeline().segment == r.newSegment)
    #expect(await next.doll.timelineEvents().isEmpty)
    #expect(try await next.doll.todayCount() == 0)
    #expect(FileManager.default.fileExists(atPath: h.dir.appending(path: "timeline/\(before).jsonl").path))
}

@Test func aFailedOrMalformedConsolidationWritesNothingAndKeepsTheDay() async throws {
    let h = try await Harness()
    let cfg = try consolidationConfig(h)
    // Nothing in the timeline yet: nothing to consolidate.
    await #expect(throws: ConsolidationError.self) { try await Consolidation.run(config: cfg, root: root, model: FakeModel([])) }
    let memory = try Memory(config: cfg.memory, root: h.dir.appending(path: "memory"), store: h.store)
    let cat = try memory.write("主人養貓", folder: .public, sourceSeq: 1, tier: .owner, kind: nil)
    let shortcut = try memory.write("TextEdit：File › Save＝⌘S（TextEdit 1.21）", folder: .public, sourceSeq: 0, tier: .program, kind: nil)
    try writeRetired(h, "9-old000", days: 40, body: "退休很久的")
    await h.doll.ownerSaid("主人的貓叫什麼")
    try await h.turn(Scripted("", say("不知道")))
    let files = try memoryFiles(h)
    let segments = try FileManager.default.contentsOfDirectory(atPath: h.dir.appending(path: "timeline").path).sorted()
    func block(_ json: String) -> String { "```json\n\(json)\n```" }
    let pet = #"{"ref": "new1", "type": "person", "name": "咪咪", "aliases": [], "description": "主人的貓"}"#
    func plan(_ ops: String, _ entities: String = pet) -> String { block(#"{"entities": [\#(entities)], "operations": [\#(ops)]}"#) }
    let add = #"{"op": "add", "ref": "n1", "text": "主人的貓叫咪咪", "folder": "public", "kind": "", "entities": [{"name": "咪咪", "id": "new1"}]}"#
    func addWith(_ entities: String) -> String {
        plan(#"{"op": "add", "ref": "n1", "text": "x", "folder": "public", "kind": "", "entities": \#(entities)}"#)
    }
    func entity(_ json: String) -> String { plan(add, json) }
    let bad: [String] = [
        "沒有區塊",
        plan(add) + "\n" + plan(""),
        block("not json"),
        block(#"{"entities": [], "operations": [], "note": "x"}"#),
        block(#"{"operations": []}"#),
        plan(#"{"op": "add", "ref": "n1", "text": "x", "folder": "public"}"#),
        plan(#"{"op": "add", "ref": "n1", "text": "x", "folder": "private", "kind": "", "entities": []}"#),
        plan(#"{"op": "add", "ref": "n1", "text": "x", "folder": "public", "kind": "fact", "entities": []}"#),
        plan(#"{"op": "add", "ref": "n1", "text": " ", "folder": "public", "kind": "", "entities": []}"#),
        plan(#"\#(add), \#(add)"#),
        plan(#"{"op": "forget", "id": "\#(cat)"}"#),
        plan(#"{"op": "update", "id": "\#(shortcut)", "text": "x", "entities": []}"#),
        plan(#"{"op": "update", "id": "\#(cat)", "text": "a", "entities": []}, {"op": "update", "id": "\#(cat)", "text": "b", "entities": []}"#),
        plan(#"{"op": "update", "id": "\#(cat)", "text": "a"}"#),
        addWith(#"{"name": "x", "id": "new1"}"#),
        addWith(#"[{"type": "app", "name": "x"}]"#),
        addWith(#"[{"name": "x"}]"#),
        addWith(#"[{"name": " ", "id": "new1"}]"#),
        addWith(#"[{"name": "x", "id": "new1", "role": "y"}]"#),
        addWith(#"[{"name": "咪咪", "id": "new1"}, {"name": "小咪", "id": "new1"}]"#),
        addWith(#"[{"name": "x", "id": "e00000000"}]"#),
        entity(#"{"ref": "new1", "type": "person", "name": "咪咪", "aliases": []}"#),
        entity(#"{"ref": "new1", "type": "person", "name": "咪咪", "aliases": [], "description": " "}"#),
        entity(#"{"ref": "new1", "type": "cat", "name": "咪咪", "aliases": [], "description": "x"}"#),
        entity(#"{"ref": "new1", "type": "person", "name": "咪咪", "aliases": "小咪", "description": "x"}"#),
        entity(#"\#(pet), \#(pet)"#),
        plan(#"{"op": "retire", "id": "\#(cat)", "superseded_by": "n9", "reason": "x"}"#),
        plan(#"{"op": "retire", "id": "\#(cat)", "superseded_by": "\#(cat)", "reason": "x"}"#),
        plan(#"\#(add), {"op": "retire", "id": "\#(cat)", "superseded_by": "n1", "reason": ""}"#),
    ]
    for reply in bad {
        await #expect(throws: ConsolidationError.self, "\(reply)") {
            try await Consolidation.run(config: cfg, root: root, model: FakeModel([Scripted(reply)]))
        }
    }
    // The model failing is an error too.
    await #expect(throws: ModelError.self) { try await Consolidation.run(config: cfg, root: root, model: FakeModel([])) }
    #expect(try memoryFiles(h) == files)
    #expect(try FileManager.default.contentsOfDirectory(atPath: h.dir.appending(path: "timeline").path).sorted() == segments)
    #expect(try h.events().contains { $0.text == "主人的貓叫什麼" })
    // The same reply, valid: applied.
    let ok = try await Consolidation.run(config: cfg, root: root, model: FakeModel([Scripted(plan(add))]))
    #expect(ok.applied.count == 3 && ok.applied[1].hasPrefix("entity ") && ok.applied[2].hasSuffix("主人的貓叫咪咪"))
}

@Test func consolidatedMemoriesAreAttachedAndRetiredOnesAreNot() async throws {
    let h = try await Harness()
    try writeRetired(h, "1-retire", days: 1, body: "TextEdit 另存要從 File 選單點 Save As…")
    try await seedMemory(h, "public", "TextEdit 按 ⇧⌥⌘S → 跳出另存對話框，檔名欄已選取", tier: "consolidated", confidence: 0.6)
    await h.doll.ownerSaid("用 TextEdit 另存一份")
    try await h.turn("好")
    #expect(try memoryEvents(h).map(\.text) == ["【相關記憶】程式用主人的話查到、判斷用得上的過去經驗：\n〔大概｜public〕\nTextEdit 按 ⇧⌥⌘S → 跳出另存對話框，檔名欄已選取"])
    try await h.turn(Scripted("", recall("另存對話框")))
    let result = try h.toolResults().last!.text
    #expect(result.contains("跳出另存對話框") && !result.contains("File 選單點"))
}

@Test func consolidateConfigIsValidated() throws {
    let c = try Config.load(root.appending(path: "dollcore.example.toml")).consolidate
    #expect(c.effort == "high" && c.retiredKeepDays == 30 && c.recallK >= 1 && c.registryK >= 1)
    let file = FileManager.default.temporaryDirectory.appending(path: "consolidate-\(UUID().uuidString).toml")
    let head = "[llm]\nbase_url = \"http://x\"\nmodel = \"m\"\n[computer]\nvm_id = \"V\"\n[memory]\n"
        + SourceTier.allCases.map { "confidence_\($0.rawValue) = 0.5\n" }.joined()
    for bad in ["effort = \"max\"", "max_tokens = 0", "timeout_s = 0", "recall_k = -1", "registry_k = -1", "retired_keep_days = -1", "bogus = 1"] {
        try (head + "[consolidate]\n\(bad)\n").write(to: file, atomically: true, encoding: .utf8)
        #expect(throws: TOMLError.self, "\(bad)") { try Config.load(file) }
    }
    // The registry folder is a fourth folder name.
    try (head + "registry_dir = \"public\"\n").write(to: file, atomically: true, encoding: .utf8)
    #expect(throws: TOMLError.self) { try Config.load(file) }
}

// memory selection: the entity registry, everything related, prefilter, scores (2026-11-11) ------------

@Test func theRegistryAndEntityRefsRoundTripAndTheIndexRebuildsFromFiles() async throws {
    let h = try await Harness()
    let cfg = try consolidationConfig(h)
    let memory = try Memory(config: cfg.memory, root: h.dir.appending(path: "memory"), store: h.store)
    let app = try memory.registry.create(.app, name: "TextEdit", aliases: ["文字編輯", " ", "ｔｅｘｔ Edit", "com.apple.TextEdit"],
                                         description: "macOS 內建的文字編輯 app")
    // Blank aliases and those with the name's key are dropped; the file gives the entry back.
    #expect(app.aliases == ["文字編輯", "com.apple.TextEdit"])
    let raw = try String(contentsOf: h.dir.appending(path: "memory/entities/\(app.id).md"), encoding: .utf8)
    #expect(try Registry.parseFile(raw, id: app.id) == app && app.text == raw)
    #expect(Registry.key(" Text\u{3000}Ｅｄｉｔ ") == "textedit")
    let file = try memory.registry.create(.file, name: "~/Documents/notes.txt", aliases: [], description: "文件資料夾裡的 notes.txt")
    // A memory names entries by id, as its text writes them; a second mention of one entry is dropped.
    let ents = [MemoryEntity(app.id, "TextEdit"), MemoryEntity(file.id, "notes.txt"), MemoryEntity(app.id, "文字編輯")]
    let id = try memory.write("TextEdit 打開 notes.txt", folder: .public, sourceSeq: 3, tier: .doll, kind: nil, entities: ents)
    let mraw = try String(contentsOf: h.dir.appending(path: "memory/public/\(id).md"), encoding: .utf8)
    #expect(mraw.contains(#"entities = [{"id":"\#(app.id)","name":"TextEdit"},{"id":"\#(file.id)","name":"notes.txt"}]"#))
    #expect(try Memory.parseFile(mraw).entities == Array(ents.prefix(2)))
    // The index is rebuilt from the files: the registry's keys, FTS and vectors, the memories' entity rows.
    try h.store.clearMemoryIndex()
    #expect(try memory.registry.exact("文字編輯").isEmpty && h.store.memoryEntities([id]).isEmpty)
    try memory.rebuild()
    #expect(try memory.registry.exact("文字編輯").map(\.id) == [app.id] && memory.registry.exact("COM.apple.textedit").map(\.id) == [app.id])
    #expect(try h.store.memoryEntities([id]) == [id: [app.id, file.id].sorted()])
    #expect(try memory.registry.nearby("notes.txt", limit: 1).map(\.id) == [file.id])
    // At boot, an index that lost its rows is rebuilt.
    try h.store.clearMemoryIndex()
    try memory.syncOnBoot()
    #expect(try h.store.countRegistryIndex() == 2 && memory.registry.exact("文字編輯").map(\.id) == [app.id])
    // Naming an entry that does not exist: not written; a file doing so fails the rebuild.
    #expect(throws: MemoryError.self) {
        try memory.write("x", folder: .public, sourceSeq: 3, tier: .doll, kind: nil, entities: [MemoryEntity("e404", "x")])
    }
    try FileManager.default.removeItem(at: h.dir.appending(path: "memory/entities/\(file.id).md"))
    #expect(throws: MemoryError.self) { try memory.rebuild() }
    // Malformed entities or registry files are errors, not silently empty.
    for bad in ["entities = [{\"id\":\"e1\"}]", "entities = nope", "entities = [{\"type\":\"app\",\"name\":\"x\"}]",
                "entities = [{\"id\":\"e1\",\"name\":\"a\"},{\"id\":\"e1\",\"name\":\"b\"}]"] {
        #expect(throws: MemoryError.self, "\(bad)") { try Memory.parseFile("+++\nkind = \"\"\n\(bad)\n+++\nx") }
    }
    for bad in ["+++\ntype = \"bogus\"\nname = \"x\"\naliases = []\n+++\nd", "+++\ntype = \"app\"\nname = \"x\"\n+++\nd",
                "+++\ntype = \"app\"\nname = \"x\"\naliases = []\n+++\n"] {
        #expect(throws: MemoryError.self, "\(bad)") { try Registry.parseFile(bad, id: "e1") }
    }
}

@Test func alignmentMapsOtherLanguagesAndPathVariantsAndKeepsHomonymsApart() async throws {
    let h = try await Harness()
    let cfg = try consolidationConfig(h)
    let memory = try Memory(config: cfg.memory, root: h.dir.appending(path: "memory"), store: h.store)
    let fake = FakeSelector()
    let sel = Selector(memory: memory, model: fake, config: cfg.select)
    let app = try memory.registry.create(.app, name: "TextEdit", aliases: [], description: "macOS 內建的文字編輯 app")
    // Another language's name is no entry's key: the lookup finds the entry by its description, the
    // model picks it and says it is the same thing, and the name becomes an alias (the query side
    // then finds it exactly).
    let placed = try await sel.place(Mention(.app, "文字編輯"), context: "用文字編輯打開")
    #expect(placed.entry.id == app.id && placed.how == "model 10" && placed.entry.aliases == ["文字編輯"])
    #expect(fake.picked.last?.candidates.first == app.line)
    let calls = fake.picked.count
    #expect(try await sel.resolve(Mention(.app, "文字編輯"), context: "").how == "exact" && fake.picked.count == calls)
    #expect(try await sel.resolve(Mention(.app, "ＴＥＸＴＥＤＩＴ"), context: "").entry?.id == app.id && fake.picked.count == calls)

    // A path variant: the model says the bare file name is the entry with the full path.
    let docs = try memory.registry.create(.file, name: "~/Documents/notes.txt", aliases: [], description: "文件資料夾裡的 notes.txt")
    let variant = try await sel.place(Mention(.file, "notes.txt"), context: "打開文件裡的 notes.txt")
    #expect(variant.entry.id == docs.id && variant.entry.aliases == ["notes.txt"])

    // A homonym: the same name in another place. The write side always asks the model, so it is not
    // taken for the entry that has the name as an alias: a new entry with its own description; from
    // then on the model tells the two apart.
    fake.setPick { _, context, c in c.firstIndex { $0.contains(context.contains("桌面") ? "桌面" : "文件") } }
    let desk = try await sel.place(Mention(.file, "notes.txt"), context: "桌面上的 notes.txt 是草稿")
    #expect(desk.how == "new (none)" && desk.entry.id != docs.id && desk.entry.description == "桌面上的 notes.txt")
    #expect(try memory.registry.exact("notes.txt").map(\.id).sorted() == [docs.id, desk.entry.id].sorted())
    #expect(try await sel.place(Mention(.file, "notes.txt"), context: "文件裡的 notes.txt").entry.id == docs.id)
    #expect(try await sel.place(Mention(.file, "notes.txt"), context: "桌面上的 notes.txt").entry.id == desk.entry.id)
    // The query side with two entries of the name: the model picks (here by context).
    #expect(try await sel.resolve(Mention(.file, "notes.txt"), context: "桌面").entry?.id == desk.entry.id)

    // The query side never writes: a name nothing matches is none, remembered per name.
    fake.setPick(nil)
    let before = try memory.registry.all()
    let none = try await sel.resolve(Mention(.website, "apple.com"), context: "")
    let after = try memory.registry.all()
    #expect(none.entry == nil && none.how == "none" && after == before)
    #expect(try await sel.resolve(Mention(.website, "apple.com"), context: "").how == "cached")

    // The program's apps: aligned by name or bundle id, the bundle id kept as an alias.
    let local = try await sel.programApp(AppIdentity(bundleID: "com.apple.TextEdit", name: "文字編輯", version: "1.21"), context: "")
    #expect(local.entry.id == app.id && local.how == "exact" && local.entry.aliases == ["文字編輯", "com.apple.TextEdit"])
    // A pick the model does not confirm is no match: Safari is not Finder, a new entry the program describes.
    let finder = try memory.registry.create(.app, name: "Finder", aliases: [], description: "macOS 內建的檔案管理 app")
    fake.setPick { _, _, c in c.firstIndex { $0.hasPrefix("Finder") } }
    fake.setSame { m, entry in m.name == "Safari" && entry.hasPrefix("Finder") ? 2 : 10 }
    let safari = try await sel.programApp(AppIdentity(bundleID: "com.apple.Safari", name: "Safari", version: "18"), context: "")
    #expect(safari.how == "new (rejected Finder 2)" && safari.entry.id != finder.id)
    #expect(safari.entry.aliases == ["com.apple.Safari"] && safari.entry.description.contains("com.apple.Safari"))
    #expect(try memory.registry.entry(finder.id)?.aliases == [])
}

/// A registry entry in the harness's memory (the next `seedMemory` re-indexes it).
private func entry(_ h: Harness, _ type: EntityType, _ name: String, _ description: String) throws -> RegistryEntry {
    let cfg = try consolidationConfig(h)
    return try Memory(config: cfg.memory, root: h.dir.appending(path: "memory"), store: h.store).registry
        .create(type, name: name, aliases: [], description: description)
}

@Test func aMemoryReachableOnlyThroughAnAlignedEntityOrOneHopIsACandidate() async throws {
    // No embedding channel here: only FTS, the trigger's entities and the hop.
    let h = try await Harness(tune: { $0.select.similarity = 1.0 })
    let app = try entry(h, .app, "TextEdit", "macOS 內建的文字編輯 app")
    let keys = try entry(h, .shortcut, "⇧⌥⌘S", "TextEdit 的另存快捷鍵")
    let safari = try entry(h, .app, "Safari", "macOS 的瀏覽器")
    let line = "用文字編輯另存一份"
    // No word of the owner's line; names TextEdit, which the line calls 文字編輯.
    try await seedMemory(h, "public", "開檔對話框裡按 ⇧⌘G 可以直接輸入路徑", tier: "consolidated", confidence: 0.6,
                         entities: [MemoryEntity(app.id, "TextEdit")], id: "1-ent")
    // An FTS hit (另存一份).
    try await seedMemory(h, "public", "另存一份要按 ⇧⌥⌘S", tier: "consolidated", confidence: 0.6,
                         entities: [MemoryEntity(keys.id, "⇧⌥⌘S")], id: "2-seed")
    // Shares only the shortcut with the FTS hit: one hop.
    try await seedMemory(h, "public", "⇧⌥⌘S 之後檔名欄已選取", tier: "consolidated", confidence: 0.6,
                         entities: [MemoryEntity(keys.id, "⇧⌥⌘S")], id: "3-hop")
    // Related to nothing.
    try await seedMemory(h, "public", "Safari 書籤在側邊欄", tier: "consolidated", confidence: 0.6,
                         entities: [MemoryEntity(safari.id, "Safari")], id: "4-none")
    h.selector.setEntities(line, [Mention(.app, "文字編輯")])
    await h.doll.ownerSaid(line)
    try await h.turn("好")
    let report = try #require(h.last.selections.first)
    #expect(report.contains("entities [文字編輯→\(app.id)「TextEdit」 model 10]"))
    #expect(report.contains("related 3 (fts 1, embedding 0, entity 1, hop 1), prepared 3;"))
    #expect(Set(h.selector.scored.map(\.memory)) == ["開檔對話框裡按 ⇧⌘G 可以直接輸入路徑", "另存一份要按 ⇧⌥⌘S", "⇧⌥⌘S 之後檔名欄已選取"])
    #expect(h.selector.scored.allSatisfy { $0.situation.contains("主人最後說：「\(line)」") && $0.situation.contains("主人的新訊息") })
    let mem = try memoryEvents(h)
    #expect(mem.count == 1 && mem[0].text.contains("⇧⌘G") && !mem[0].text.contains("書籤"))
}

@Test func thePrefilterCapsWhatIsScoredAndTheThresholdDecides() async throws {
    // entity_weight 1: the two memories naming the trigger's app come first, whatever their cosines.
    let h = try await Harness(tune: { $0.select.prefilter = 2; $0.select.entityWeight = 1; $0.select.candidateMaxChars = 100 })
    let app = try entry(h, .app, "TextEdit", "macOS 內建的文字編輯 app")
    let long = String(repeating: "很長", count: 50)
    for i in 1...4 {
        try await seedMemory(h, "public", "TextEdit 另存第 \(i) 種方法" + long, tier: "consolidated", confidence: 0.6,
                             entities: i <= 2 ? [MemoryEntity(app.id, "TextEdit")] : [], id: "\(i)-m")
    }
    h.selector.setEntities("TextEdit 另存", [Mention(.app, "TextEdit")])
    h.selector.setScore { _, m in m.contains("第 1 種") ? 6 : m.contains("第 2 種") ? 8 : 10 }
    await h.doll.ownerSaid("TextEdit 另存")
    try await h.turn("好")
    // Four related, two scored (each cut for the model), one at or above the threshold (7).
    let scored = h.selector.scored.map(\.memory)
    #expect(scored.count == 2 && scored.allSatisfy { $0.hasSuffix("…") && $0.count == 101 })
    #expect(Set(scored.map { String($0.prefix(15)) }) == ["TextEdit 另存第 1 ", "TextEdit 另存第 2 "])
    let report = try #require(h.last.selections.first)
    #expect(report.contains("related 4 (") && report.contains("attached 1 [2-m 8]"))
    // She gets the kept one whole.
    let mem = try memoryEvents(h)
    #expect(mem.count == 1 && mem[0].text.contains("第 2 種方法" + long) && !mem[0].text.contains("第 1 種"))
    // Recall goes the same way; nothing at or above the threshold is 「0 條」.
    h.selector.setScore { _, _ in 3 }
    try await h.turn(Scripted("", recall("TextEdit 另存")))
    #expect(try h.toolResults().last!.text == "TextEdit 另存：0 條" && h.selector.scored.count == 4)
}

@Test func recallReturnsWhatTheModelScoresHighTodayIncluded() async throws {
    // Every memory and event is related here (similarity -1): what is given is up to the scores.
    let h = try await Harness(tune: { $0.select.similarity = -1 })
    try await seedMemory(h, "public", "主人的貓叫咪咪", tier: "owner", confidence: 0.9)
    try await seedMemory(h, "public", "主人的狗叫旺財", tier: "owner", confidence: 0.9)
    await h.doll.ownerSaid("我今天幫貓買了新的貓砂")
    try await h.turn(Scripted("", say("好")))
    h.selector.setScore { _, m in m.contains("貓") ? 9 : 1 }
    try await h.turn(Scripted("", recall("貓")))
    let text = try h.toolResults().last!.text
    #expect(text.hasPrefix("貓：2 條") && text.contains("主人的貓叫咪咪") && text.contains("〔今天") && text.contains("貓砂")
            && !text.contains("旺財"))
    #expect(h.selector.scored.contains { $0.memory.contains("旺財") })
    #expect(h.selector.scored.last!.situation.contains("她用 recall 查「貓」"))
    #expect(h.last.selections.first?.contains("prefilter [") == true && h.last.actions.contains("recall 2"))
}

@Test func anUnavailableOnDeviceModelFailsBoot() async throws {
    let h = try await Harness()
    h.selector.state.withLock { $0.unavailable = "Apple Intelligence is off" }
    await #expect(throws: SelectionError.self) { try await h.doll.boot() }
}

@Test func programWrittenMemoriesCarryWhatTheProgramKnows() async throws {
    let h = try await Harness([Scripted("", useComputer), Scripted("", outcome(true, "存好了"))])
    h.vm.setApp(textEdit, textEditKeys)
    await h.doll.ownerSaid("用 TextEdit 存檔")
    try await h.turn()
    try await h.turn()
    let cfg = try consolidationConfig(h)
    let registry = try Memory(config: cfg.memory, root: h.dir.appending(path: "memory"), store: h.store).registry
    let app = try #require(try registry.exact("com.apple.TextEdit").first)
    let keys = try #require(try registry.exact("⇧⌥⌘S").first)
    #expect(app.name == "TextEdit" && app.aliases == ["com.apple.TextEdit"] && keys.type == .shortcut)
    let files = try memoryFilesByID(h).values.map(\.file)
    let saveAs = try #require(files.first { $0.tier == .program && $0.body.contains("Save As…") })
    #expect(saveAs.entities == [MemoryEntity(app.id, "TextEdit"), MemoryEntity(keys.id, "⇧⌥⌘S")])
    let experience = try #require(files.first { $0.tier == .selfReported })
    #expect(experience.entities == [MemoryEntity(app.id, "TextEdit")])
}

@Test func selectConfigIsValidated() throws {
    let s = try Config.load(root.appending(path: "dollcore.example.toml")).select
    #expect(s.prefilter == 8 && s.threshold == 7 && s.lookupK >= 1 && s.candidateMaxChars >= 100)
    let file = FileManager.default.temporaryDirectory.appending(path: "select-\(UUID().uuidString).toml")
    let head = "[llm]\nbase_url = \"http://x\"\nmodel = \"m\"\n[computer]\nvm_id = \"V\"\n[memory]\n"
        + SourceTier.allCases.map { "confidence_\($0.rawValue) = 0.5\n" }.joined()
    for bad in ["similarity = 1.5", "entity_weight = -0.1", "prefilter = 0", "threshold = 11", "candidate_max_chars = 50",
                "lookup_k = 0", "align_threshold = -1", "seed_k = 10", "bogus = 1"] {
        try (head + "[select]\n\(bad)\n").write(to: file, atomically: true, encoding: .utf8)
        #expect(throws: TOMLError.self, "\(bad)") { try Config.load(file) }
    }
    try (head + "auto_attach_k = 5\n").write(to: file, atomically: true, encoding: .utf8)
    #expect(throws: TOMLError.self) { try Config.load(file) }
}
