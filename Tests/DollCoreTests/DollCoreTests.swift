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

private func key(_ keys: String) -> (String, [(String, String)]) { ("key", [("keys", keys)]) }
private func watch(_ target: String, _ maxWait: Int? = nil) -> (String, [(String, String)]) {
    ("watch", [("target", target)] + (maxWait.map { [("max_wait_s", "\($0)")] } ?? []))
}
private let useComputer: (String, [(String, String)]) = ("use_computer", [("expect", "畫面上看得到結果")])
private func outcome(_ success: Bool, _ evidence: String) -> (String, [(String, String)]) {
    ("outcome", [("success", "\(success)"), ("evidence", evidence)])
}
private let leaveComputer: (String, [(String, String)]) = ("leave_computer", [])
private let screenshotCall: (String, [(String, String)]) = ("screenshot", [])
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
    }
    let state = Mutex(State())

    func act(_ action: ComputerAction) async throws -> String {
        try state.withLock { s in
            s.actions.append(action)
            if let f = s.failNext {
                s.failNext = nil
                throw ComputerError(f)
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
        state.withLock { $0.menuReads.append(bundleID); return $0.shortcuts }
    }
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
    func failNext(_ text: String) { state.withLock { $0.failNext = text } }
    func failWindow(_ text: String?) { state.withLock { $0.windowFailure = text } }
    func failDesktop(_ text: String?) { state.withLock { $0.desktopFailure = text } }
    func setPlace(_ p: FrontWindow.Place) { state.withLock { $0.place = p } }
    func setSizes(screen: (Int, Int)?, crop: (Int, Int)?) { state.withLock { $0.screenSize = screen; $0.cropSize = crop } }
    var actions: [ComputerAction] { state.withLock { $0.actions } }
}

/// The fake's open windows and focus line, the last line of every computer result.
private let fakeWindowsLine = "開著的視窗：Finder、Fake「frame」（前景）；焦點：button \"OK\""

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
    let rec: Recorder
    let dir: URL
    let store: Store      // a second connection to the same database, for checks

    init(_ replies: [Scripted] = [], tune: (inout Config) -> Void = { _ in }) async throws {
        dir = FileManager.default.temporaryDirectory.appending(path: "dollcore-test-\(UUID().uuidString)")
        var cfg = try Config.load(root.appending(path: "dollcore.example.toml"))
        cfg.core.dataDir = dir.path
        cfg.computer.watchPollS = 0.02
        cfg.computer.watchMinIntervalS = 0
        cfg.memory.dir = dir.appending(path: "memory").path
        tune(&cfg)
        model = FakeModel(replies)
        let rec = Recorder()
        self.rec = rec
        let vm = FakeComputer()
        self.vm = vm
        doll = try Doll(config: cfg, root: root, model: model, computer: vm,
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

    /// Her tool results, oldest first.
    func toolResults() throws -> [Event] { try store.events().filter { $0.kind == .tool } }

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
    #expect(p.speech == "")
    #expect(p.calls == [ToolCall(name: "recall", parameters: [.init("query", "主人的貓")])])
}

@Test func parserKeepsCallsInTextualOrderAndSpeechBeforeThem() {
    let raw = "好，我查一下再跑。\n\n<tool_call>\n<function=recall>\n<parameter=query>\nA\n</parameter>\n</function>\n</tool_call>\n"
        + "<tool_call>\n<function=key>\n<parameter=keys>\nls\n</parameter>\n</function>\n</tool_call>\n"
        + "<tool_call><function=think><parameter=level>high</parameter></function></tool_call>"
    let p = ParsedReply(raw)
    #expect(p.speech == "好，我查一下再跑。")
    #expect(p.calls.map(\.name) == ["recall", "key", "think"])
    #expect(p.calls.map { $0.parameters[0].value } == ["A", "ls", "high"])
    #expect(p.calls.allSatisfy { $0.malformed == nil })
    #expect(ParsedReply("就聊聊，沒有呼叫。\n").calls.isEmpty && ParsedReply("就聊聊，沒有呼叫。\n").speech == "就聊聊，沒有呼叫。")
}

@Test func parserStripsExactlyOneNewlineEachSideAndKeepsTheRest() {
    let raw = "<tool_call>\n<function=type>\n<parameter=text>\n\n  echo a\n  echo b  \n\n</parameter>\n</function>\n</tool_call>"
    #expect(ParsedReply(raw).calls[0].parameters == [.init("text", "\n  echo a\n  echo b  \n")])
    let tight = "<tool_call><function=key><parameter=keys>Return</parameter></function></tool_call>"
    #expect(ParsedReply(tight).calls[0].parameters == [.init("keys", "Return")])
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
    #expect(a.speech == "嗯")
    #expect(a.calls.map(\.name) == ["key", "think"])
    #expect(a.calls[0].malformed == "<tool_call> 沒有 </tool_call> 收尾" && a.calls[1].malformed == nil)
    // <function> outside <tool_call>
    let b = ParsedReply("<function=key>\n<parameter=keys>\nls\n</parameter>\n</function>")
    #expect(b.calls == [ToolCall(name: "key", malformed: "<function=key> 沒有包在 <tool_call> 和 </tool_call> 裡")])
    #expect(b.speech == "")
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
    #expect(a.speech == "好，我開電腦看一下。")
    #expect(a.calls.map(\.name) == ["use_computer", "think"])
    #expect(a.calls[0].malformed == "<invoke name=\"use_computer\"> 不是工具呼叫的格式，沒有執行" && a.calls[1].malformed == nil)
    // a stray <parameter=…> with nothing around it: the rest is part of the broken call
    let b = ParsedReply("我開電腦。\n<parameter=level>\nmedium\n</parameter>\n")
    #expect(b.speech == "我開電腦。")
    #expect(b.calls == [ToolCall(name: "", malformed: "<parameter=level> 不是工具呼叫的格式，沒有執行")])
    let unclosed = ParsedReply("嗯<parameter=x>\nabc")
    #expect(unclosed.speech == "嗯" && unclosed.calls.count == 1)
    // <tool_call …> with attributes, lone closing tags, and two replies seen from iq2_xs at effort
    // medium (/tmp/m2why/latency.log): `<parameter name=…>…</invoke>`, and a <function=…> whose
    // only wrapper is a trailing </tool_call> (one mistake, one error)
    for raw in ["<tool_call name=\"key\">ls</tool_call>", "好</invoke>", "好\n</function>\n</tool_call>",
                "<parameter name=\"level\">\nmedium\n</parameter>\n\n</invoke>",
                "<function=use_computer>\n</function>\n</tool_call>"] {
        let p = ParsedReply(raw)
        #expect(p.calls.count == 1 && p.calls[0].malformed != nil && !p.speech.contains("<") , "\(raw)")
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
    #expect(sys.hasPrefix("# Tools\n\nYou have access to the following functions:\n\n<tools>\n{\"type\": \"function\", \"function\": {\"name\": \"think\""))
    #expect(sys.contains("\n</tools>\n\nIf you choose to call a function ONLY reply in the following format with NO suffix:"))
    #expect(sys.contains("</IMPORTANT>\n\n你是"))
    let listed = sys.components(separatedBy: "\n").filter { $0.hasPrefix("{\"type\": \"function\"") }
    #expect(listed.count == 4)
    for (line, name) in zip(listed, ["think", "remember", "recall", "use_computer"]) {
        #expect(line.contains("\"name\": \"\(name)\""))
        #expect((try? JSONSerialization.jsonObject(with: Data(line.utf8))) != nil)   // valid JSON
    }
    #expect(!sys.contains("\"name\": \"click\""))                       // the computer menu is not in it
}

// tool calls -----------------------------------------------------------------

@Test func everyCallInOneReplyRunsInOrderAndGetsItsOwnResult() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("做三件事")
    try await h.turn(Scripted("好，一次做完", recall("一"), think("medium"), recall("二")))
    #expect(h.said == ["好，一次做完"])                                  // only the speech is said
    #expect(h.last.actions == ["said", "recall 0", "think medium", "recall 0"] && h.last.usedTool)
    let results = try h.toolResults()
    #expect(results.map(\.text) == ["一：0 條", "下一回合 effort：medium", "二：0 條"])
    #expect(await h.doll.followUp)                                       // the results give her the next turn
    try await h.turn("都好了")
    let msgs = await h.model.seen(1)
    let a = try #require(msgs.firstIndex { $0.role == "assistant" })
    // her raw content, call text included, is what goes back
    #expect(msgs[a].content == Scripted("好，一次做完", recall("一"), think("medium"), recall("二")).raw)
    #expect(msgs[(a + 1)...(a + 3)].map(\.role) == ["tool", "tool", "tool"])
    #expect(msgs[(a + 1)...(a + 3)].map(\.content) == results.map(\.text))
    #expect(await h.model.requests.map(\.effort) == ["medium", "medium"])    // owner turn, then her think
}

@Test func aRawReplyRunsItsCallsInTheOrderSheWroteThem() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("查一下再列目錄")
    let raw = "我先查，再看目錄。\n\n<tool_call>\n<function=recall>\n<parameter=query>\n目錄\n</parameter>\n</function>\n</tool_call>\n"
        + "<tool_call>\n<function=think>\n<parameter=level>\nmedium\n</parameter>\n</function>\n</tool_call>"
    try await h.turn(Scripted(stringLiteral: raw))
    #expect(h.said == ["我先查，再看目錄。"])
    #expect(h.last.actions == ["said", "recall 0", "think medium"])
    #expect(try h.toolResults().map(\.text) == ["目錄：0 條", "下一回合 effort：medium"])
    let doll = try #require(try h.store.events().first { $0.kind == .doll })
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
    let h = try await Harness([Scripted("", useComputer), "好了", Scripted("盯著", watch("window", 1)), "沒變"])
    let doll = h.doll
    let runner = Task { try await doll.run() }
    // Poll instead of fixed sleeps: a busy parallel suite made the 500 ms waits race.
    func waitFor(_ n: Int, said s: Int, _ seconds: Double = 5) async throws {
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
    #expect(await h.model.requests.count == 3)                          // the watch ack did not wake her
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
    let events = try h.store.events()
    #expect(events.map(\.text) == ["voice", "text", "r", "好"])
    #expect(events.map(\.priority) == [.ownerVoice, .ownerText, .watch, .watch])
}

@Test func ownerVoiceCancelsTheCallAndTheRerunSeesTheVoiceWithTheSameEffort() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("這題難")
    try await h.turn(Scripted("等我", think("high")))                 // next call is high
    await h.model.stall()
    await h.model.add(["好"])
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
    let later = try h.store.events().filter { $0.kind == .owner }.map(\.text)
    #expect(later == ["這題難", "開始算", "等等不用算了"])
}

@Test func noEventMeansNoTurn() async throws {
    let h = try await Harness(["哈囉"])
    let doll = h.doll
    let runner = Task { try await doll.run() }
    try await Task.sleep(for: .milliseconds(500))
    #expect(await h.model.requests.isEmpty)
    await h.doll.ownerSaid("嗨")
    try await Task.sleep(for: .milliseconds(300))
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
    let seq = try h.store.events().last!.seq!
    #expect(try h.store.footer(seq) == first)
    await h.doll.ownerSaid("再來")
    try await h.turn("好")
    let msgs = await h.model.seen(1)
    let i = msgs.firstIndex { $0.content == first }!
    #expect(msgs[i].role == "user" && msgs[i + 1] == ChatMessage(role: "assistant", content: "哈囉"))
    #expect(msgs.last!.content == h.last.footer && msgs.last!.content != first)
    #expect(try h.store.events().filter { $0.text.contains("【狀況】") }.isEmpty)   // footers are not events
}

@Test func thinkRaisesEffortForOneCallAndShowsInTheFooter() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("這題難")
    try await h.turn(Scripted("等我", think("high")))
    let status = try await h.doll.status()
    #expect(h.last.actions == ["said", "think high"] && status.contains("effort：high"))
    await h.doll.ownerSaid("嗯")
    try await h.turn("想好了")
    await h.doll.ownerSaid("嗯")
    try await h.turn("嗯")
    #expect(await h.model.requests.map(\.effort) == ["medium", "high", "medium"])
    #expect(h.reports[1].footer.contains("effort：high") && h.reports[2].footer.contains("effort：medium"))
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
    #expect(again.last.actions == ["recall 1"])
    // the indexed row is the one that answers: FTS finds it, public, from the owner
    let ids = try h.store.memoryFTS(match: "\"Strata\"", folders: ["public"], limit: 5)
    #expect(ids.count == 1)
    let meta = try #require(try h.store.memoryMeta(ids[0]))
    #expect(meta.folder == "public" && meta.confidence > 0.8)
    // the tool result in her timeline carries the indexed text
    #expect(try h.toolResults().last!.text.contains("主人的伺服器叫 Strata"))
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
    try await h.turn(Scripted("", recall("主人")))
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

@Test func theComputerMenuOpensWithUseComputerAndLocksAgainWithLeave() async throws {
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
    let names = ["click", "type", "key", "scroll", "drag", "screenshot", "watch", "leave_computer", "outcome"]
    #expect(listed.count == names.count)
    for (line, name) in zip(listed, names) {
        #expect(line.contains("\"name\": \"\(name)\""))
        #expect((try? JSONSerialization.jsonObject(with: Data(line.utf8))) != nil)
    }
    try await h.turn(Scripted("", key("Return")))
    #expect(h.last.actions == ["key"] && h.vm.actions == [.key("Return")])
    #expect(h.last.footer.contains("- 選單：電腦（用完呼叫 leave_computer() 離開）"))
    try await h.turn(Scripted("", leaveComputer))
    #expect(try h.toolResults().last!.text == "你開始前說完成時應該看到：「畫面上看得到結果」。現在達成了嗎？檢查後用 outcome(success, evidence) 回答。")
    #expect(await h.doll.inComputer)
    try await h.turn(Scripted("", outcome(false, "沒看到")))
    #expect(try h.toolResults().last!.text == "記下了：沒達成。離開電腦選單了；電腦工具鎖回去了。")
    try await h.turn(Scripted("", key("Return")))
    #expect(h.last.actions == ["error key"] && h.vm.actions.count == 1)
    #expect(h.last.footer.contains("- 選單：頂層"))
    // Opening and closing the menu never touched the system prompt.
    #expect(Set(await h.model.requests.map { $0.messages[0].content }).count == 1)
}

@Test func everyComputerActionReturnsATextLineAndAScreenshot() async throws {
    let h = try await menuHarness()
    try await h.turn(Scripted("", ("click", [("x", "100"), ("y", "200")]),
                              ("click", [("text", "Save"), ("button", "right"), ("count", "2")]),
                              ("type", [("text", "sw_vers\n")]), key("cmd+s"),
                              ("scroll", [("direction", "down"), ("amount", "5")]),
                              ("drag", [("x0", "1"), ("y0", "2"), ("x1", "3"), ("y1", "4")]), screenshotCall))
    let results = Array(try h.toolResults().suffix(7))
    #expect(results.allSatisfy { $0.image != nil })
    #expect(results.map { $0.text.components(separatedBy: "\n")[0] }
            == ["click 100,200", "click「Save」 right 雙擊", "type「sw_vers", "key cmd+s", "scroll down 5", "drag 1,2 → 3,4", "現在的畫面："])
    #expect(results.prefix(6).allSatisfy { $0.text.hasSuffix("\nDone: 1 action in 0.1 s.\n" + fakeWindowsLine) })
    #expect(h.vm.actions == [.click(x: 100, y: 200, button: "left", count: 1), .clickText("Save", button: "right", count: 2),
                             .type("sw_vers\n"), .key("cmd+s"), .scroll(direction: "down", amount: 5),
                             .drag(x0: 1, y0: 2, x1: 3, y1: 4)])
    #expect(h.last.actions == ["click", "click", "type", "key", "scroll", "drag", "screenshot"])
    try await h.turn(Scripted("", ("click", [("x", "1")]), ("click", [("x", "1"), ("y", "2"), ("text", "A")]),
                              ("scroll", [("direction", "sideways")])))
    #expect(try h.toolResults().suffix(3).map(\.text) == ["失敗：click 的 x 和 y 要一起給",
                                                          "失敗：click 的 element、text、x/y 只能給一種，不要同時給",
                                                          "失敗：scroll 的 direction 只能是 up、down、left、right，收到 sideways"])
    #expect(h.vm.actions.count == 6)
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
    #expect(try h.toolResults().suffix(6).map { $0.text.components(separatedBy: "\n")[0] }
            == ["click 元素 button \"Save\" 雙擊", "click 元素 \"notes.txt\"", "click 元素 image \"notes.txt\" right",
                "click 元素 \"Open Recent\"", "click「Save」", "click 100,200"])
    try await h.turn(Scripted("", ("click", []), ("click", [("element", "OK"), ("x", "1"), ("y", "2")]),
                              ("click", [("element", "OK"), ("text", "OK")]), ("click", [("text", "OK"), ("on", "window")])))
    #expect(try h.toolResults().suffix(4).map(\.text) == ["失敗：click 要給 element、text 或 x 和 y 其中一種",
                                                          "失敗：click 的 element、text、x/y 只能給一種，不要同時給",
                                                          "失敗：click 的 element、text、x/y 只能給一種，不要同時給",
                                                          "失敗：click 的 on 只配 x、y 用"])
    #expect(h.vm.actions.count == 6)
    // Escaped quotes read as quotes (round 6: textfield \"Search\"); a role word alone is a role.
    try await h.turn(Scripted("", ("click", [("element", #"textfield \"Search\""#)]), ("click", [("element", "textfield")])))
    #expect(h.vm.actions.suffix(2) == [.clickElement(role: "textfield", name: "Search", button: "left", count: 1),
                                       .clickElement(role: "textfield", name: nil, button: "left", count: 1)])
    #expect(try h.toolResults().suffix(2).map { $0.text.components(separatedBy: "\n")[0] }
            == ["click 元素 textfield \"Search\"", "click 元素 textfield"])
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
    #expect(r.text == "click 視窗圖 100,50 → 全螢幕 60,45 雙擊\nDone: 1 action in 0.1 s.\n" + fakeWindowsLine && r.image != nil)
    // Off the crop, off the screen: an error, nothing clicked.
    try await h.turn(Scripted("", ("click", [("x", "580"), ("y", "10"), ("on", "window")]),
                              ("click", [("x", "640"), ("y", "800")]), ("click", [("x", "-1"), ("y", "5")])))
    #expect(try h.toolResults().suffix(3).map(\.text) == ["失敗：沒有點：580,10 不在視窗圖裡（視窗圖 580×360）",
                                                          "失敗：沒有點：640,800 在螢幕外（全螢幕截圖 1280×800）",
                                                          "失敗：沒有點：-1,5 在螢幕外（全螢幕截圖 1280×800）"])
    #expect(h.last.actions == ["error click", "error click", "error click"] && h.vm.actions.count == 1)
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
    #expect(said == "雙擊了 image \"notes.txt\" (442,194)（全螢幕截圖座標），用無障礙按壓（semantic）。agent-desktop：delivery=delivered_unverified, retry=unsafe；這不代表真的點到，看下面的截圖。")
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
                .hasPrefix("點了 image \"Notes.txt\" (442,194)"))
    #expect(exact.scripts.last!.contains(#"a=$("$1" click "$2")"#))

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
                .hasPrefix("右鍵點了 treeitem \"Documents\" (307,282)"))
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
                .hasPrefix("點了 textfield (442,194)（全螢幕截圖座標），用無障礙按壓（semantic）"))
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
            == "點了 dockitem \"Finder\" (67,764)（全螢幕截圖座標），用無障礙按壓（semantic）。agent-desktop：delivery=delivered_unverified, retry=unsafe；這不代表真的點到，看下面的截圖。")
    let scripts = finder.scripts
    #expect(scripts.count == 3 && scripts[1].contains("menubar menu-bar-extras dock") && !scripts.contains { $0.contains(" find ") })
    #expect(finder.calls.withLock { $0[2].last } == "@d:e2")
    // The role narrows, the same way as in a window.
    #expect(try await driver(FakeAgentDesktop(found: [], focused: false)).act(.clickElement(role: "menuitem", name: "clock", button: "left", count: 1))
                .hasPrefix("點了 menuitem \"Clock\" (1209,11)"))
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
            == "雙擊了 image \"notes.txt\" (442,194)（全螢幕截圖座標），用實體滑鼠（physical，headed）：無障礙按壓沒送出（agent-desktop：\(denied)；delivery=not_delivered, retry=safe）。"
            + "實體滑鼠 agent-desktop：delivery=delivered_unverified, retry=unsafe；這不代表真的點到，看下面的截圖。")
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
    #expect(r.text == "key Escape\nDone: 1 action in 0.1 s.\n" + fakeWindowsLine && r.image != nil)
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

@Test func leavingTheComputerStopsTheWatch() async throws {
    let h = try await menuHarness()
    try await h.turn(Scripted("", watch("window", 60)))
    try await h.turn(Scripted("", leaveComputer))
    #expect(try h.toolResults().last!.text.hasSuffix("回答。（watch 停了。）"))
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
    #expect(Doll.audience(try h.store.events()) == .doll)
    #expect(try h.toolResults().last!.text.contains("fish123"))
    // A stranger's turn (no such input until M6; the projection is Memory's): never.
    var cfg = try Config.load(root.appending(path: "dollcore.example.toml")).memory
    cfg.dir = h.dir.appending(path: "memory").path
    let memory = try Memory(config: cfg, root: h.dir.appending(path: "memory"), store: h.store)
    let stranger = try memory.search("VM 登入密碼", visible: Memory.visible(.stranger, sawSecret: false))
    #expect(!stranger.contains { $0.folder == "own" } && stranger.contains { $0.text.contains("深色模式") })
    #expect(try memory.search("VM 登入密碼", visible: Memory.visible(.doll, sawSecret: false)).contains { $0.folder == "own" })
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
    let again = try Doll(config: cfg, root: root, model: FakeModel([]), computer: h.vm, say: { _ in })
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
                        confidence: Double = 0.4) async throws {
    let dir = h.dir.appending(path: "memory/\(folder)")
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    let file = "+++\nkind = \"\"\nconfidence = \(confidence)\ncreated_ts = 0\nsource_event_seq = 1\nsource_tier = \"\(tier)\"\n+++\n\(text)"
    try file.write(to: dir.appending(path: "1-\(UUID().uuidString.prefix(6).lowercased()).md"), atomically: true, encoding: .utf8)
    try await h.doll.rebuildMemory()
}

private func memoryEvents(_ h: Harness) throws -> [Event] { try h.store.events().filter { $0.kind == .memory } }

@Test func anOwnerLineAttachesRelatedExperiencesWholeBeforeHerTurn() async throws {
    let h = try await Harness()
    try await seedMemory(h, "public", "經驗：幫我用 TextEdit 寫筆記\n期望：筆記存好\n1. key cmd+s → Done\n結果：標題沒有點")
    await h.doll.ownerSaid("幫我用 TextEdit 寫筆記")
    try await h.turn("好")
    let mem = try memoryEvents(h)
    #expect(mem.map(\.text) == ["【相關記憶】程式用主人的話查到的過去經驗：\n〔大概｜public〕\n經驗：幫我用 TextEdit 寫筆記\n期望：筆記存好\n1. key cmd+s → Done\n結果：標題沒有點"])
    // Right after the owner's line, before the footer.
    let seen = await h.model.seen(0)
    #expect(seen[seen.count - 2].content.hasSuffix("] " + mem[0].text) && seen[seen.count - 2].role == "user")
    #expect(seen[seen.count - 3].content.hasSuffix("幫我用 TextEdit 寫筆記"))
    #expect(h.last.attached == [mem[0].text])
    #expect(h.logs.contains { $0.contains("相關記憶（主人的新訊息）") && $0.contains("1 found") })
    // Turns on her own tool results attach nothing.
    try await h.turn(Scripted("", think("high")))
    try await h.turn("嗯")
    #expect(try memoryEvents(h).count == 1 && h.last.attached.isEmpty)

    // auto_attach_k = 0: off.
    let off = try await Harness(tune: { $0.memory.autoAttachK = 0 })
    try await seedMemory(off, "public", "經驗：幫我用 TextEdit 寫筆記")
    await off.doll.ownerSaid("幫我用 TextEdit 寫筆記")
    try await off.turn("好")
    #expect(try memoryEvents(off).isEmpty)
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
    #expect(h.logs.contains { $0.contains("相關記憶（前景 app 換成 TextEdit）") && $0.contains("attached [], already attached 1") })
    // The shortcuts and her own memory are still recallable.
    try await h.turn(Scripted("", recall("TextEdit 存檔")))
    let found = try h.toolResults().last!.text
    #expect(found.contains("TextEdit 存檔按 cmd+s") && found.contains("＝⌘S（TextEdit"))
    // The owner's next line starts over: the same experience comes again.
    await h.doll.ownerSaid("再存一次 TextEdit")
    try await h.turn("好")
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

// computer sessions and experiences (spec 3.1, 2026-11-09「先砍到最小」) -------------------

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
    #expect(await !h.doll.inComputer)
}

@Test func aSuccessfulOutcomeWritesOneExperienceOfHerActions() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("把 notes 存檔")
    try await h.turn(Scripted("", ("use_computer", [("expect", "標題沒有「已編輯」")])))
    // Before leave_computer, outcome is an error and nothing ends.
    try await h.turn(Scripted("", outcome(true, "好了")))
    #expect(try h.toolResults().last!.text == "失敗：outcome 要在 leave_computer() 之後回答")
    // A failed action is not a step; each step is only what she did, no VM answer text.
    h.vm.failNext("boom\nsecond line")
    try await h.turn(Scripted("", ("click", [("text", "Save")]), screenshotCall))
    h.vm.setSizes(screen: (1280, 800), crop: (580, 360))
    try await h.turn(Scripted("", ("click", [("element", "dockitem \"Finder\"")]), screenshotCall))
    try await h.turn(Scripted("", ("click", [("x", "100"), ("y", "50"), ("on", "window"), ("count", "2")]),
                              ("type", [("text", "娃娃來過")]), key("cmd+s")))
    try await h.turn(Scripted("", leaveComputer))
    #expect(h.last.outcome == nil)
    // The asked turn: the menu still open, the footer says to answer; she looks, then answers.
    try await h.turn(Scripted("", screenshotCall, outcome(true, "標題沒有點")))
    #expect(h.last.footer.contains("- 選單：電腦（這一回合用 outcome 回答期望達成了沒）"))
    #expect(try h.toolResults().last!.text == "記下了：達成。這次的步驟存成一條經驗。離開電腦選單了；電腦工具鎖回去了。")
    let text = "經驗：把 notes 存檔\n期望：標題沒有「已編輯」\n1. click dockitem \"Finder\"\n2. click 60,45 雙擊\n3. type 「娃娃來過」\n4. key cmd+s\n結果：標題沒有點"
    #expect(h.last.outcome == "success" && h.last.experience == text && h.last.actions.contains("outcome success"))
    let mems = try experiences(h)
    let conf = try Config.load(root.appending(path: "dollcore.example.toml")).memory.confidence[.selfReported]!
    #expect(mems.count == 1 && mems[0].text == text && mems[0].confidence == conf && mems[0].kind == "")
    #expect(conf >= 0.3 && conf < 0.7)
    // Relocked.
    try await h.turn(Scripted("", key("Return")))
    #expect(h.last.actions == ["error key"] && h.last.footer.contains("- 選單：頂層"))
    #expect(await !h.doll.inComputer)
    // The next time the owner asks the same, it is attached whole.
    await h.doll.ownerSaid("把 notes 存檔")
    try await h.turn("好")
    #expect(h.last.attached.count == 1 && h.last.attached[0].hasSuffix("〔大概｜public〕\n" + text))
}

@Test func aFailedOrMissingOutcomeWritesNothing() async throws {
    let h = try await menuHarness()
    try await h.turn(Scripted("", key("cmd+s"), leaveComputer))
    try await h.turn(Scripted("", outcome(false, "還有點")))
    #expect(h.last.outcome == "failure" && h.last.experience == nil)
    #expect(try experiences(h).isEmpty)

    // No outcome in the turn after leave_computer: unknown, relocked, said in the timeline.
    try await h.turn(Scripted("", useComputer))
    try await h.turn(Scripted("", watch("window", 60), leaveComputer))
    #expect(await h.doll.watch == nil)
    try await h.turn(Scripted("好了", screenshotCall))
    #expect(h.last.outcome == "unknown" && h.last.experience == nil)
    #expect(await !h.doll.inComputer)
    #expect(try memoryEvents(h).last?.text == "沒收到 outcome：這次的結果記成不明，沒有存經驗。離開電腦選單了；電腦工具鎖回去了。")
    #expect(h.logs.contains { $0.contains("outcome unknown") })
    #expect(try experiences(h).isEmpty)
    // A bad success value is an error, and the session stays asked.
    try await h.turn(Scripted("", useComputer))
    try await h.turn(Scripted("", leaveComputer))
    try await h.turn(Scripted("", ("outcome", [("success", "maybe"), ("evidence", "x")]), outcome(true, "看到了")))
    #expect(try h.toolResults().map(\.text).contains("失敗：outcome 的 success 只能是 true 或 false，收到 maybe"))
    #expect(h.last.outcome == "success")
    #expect(try experiences(h).count == 1)
}

@Test func anExperienceIsCutToTheCapFromTheMiddleSteps() {
    let steps = (1...12).map { "key cmd+\($0) then a long enough line" }
    let full = Doll.experienceText(task: "做事", expect: "做好", steps: steps, evidence: "看到了", maxChars: 10_000)
    #expect(full.components(separatedBy: "\n").count == 15 && !full.contains("省略"))
    let cut = Doll.experienceText(task: "做事", expect: "做好", steps: steps, evidence: "看到了", maxChars: 300)
    #expect(cut.count <= 300)
    #expect(cut.hasPrefix("經驗：做事\n期望：做好\n1. key cmd+1 ") && cut.hasSuffix("\n12. key cmd+12 then a long enough line\n結果：看到了"))
    #expect(cut.contains("（省略 ") && cut.components(separatedBy: "\n").count < 15)
    // Header and evidence alone over the cap: hard cut.
    let tiny = Doll.experienceText(task: String(repeating: "長", count: 500), expect: "x", steps: steps, evidence: "y", maxChars: 100)
    #expect(tiny.count == 100 && tiny.hasSuffix("…"))
}

@Test func everyComputerResultEndsWithTheOpenWindowsAndFocusOrWhyNot() async throws {
    let h = try await menuHarness()
    #expect(try h.toolResults().last!.text.hasSuffix("\n" + fakeWindowsLine))
    h.vm.failState("vmpal: exec failed")
    try await h.turn(Scripted("", key("cmd+s")))
    #expect(try h.toolResults().last!.text == "key cmd+s\nDone: 1 action in 0.1 s.\n開著的視窗和焦點讀不到：vmpal: exec failed")
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
            == "開著的視窗：Finder、TextEdit「notes.txt」（前景）、Safari「Apple」；焦點：textarea「第一行 第二行很長很長很長很長很長很長很…」")
    let notFound = Data(#"{"ok":false,"error":{"code":"WINDOW_NOT_FOUND","message":"No window found"}}"#.utf8)
    #expect(OpenWindows.line(windows: Data(#"{"ok":true,"data":[]}"#.utf8), focused: notFound)
            == "開著的視窗：沒有；焦點讀不到：WINDOW_NOT_FOUND: No window found")
    #expect(OpenWindows.line(windows: Data("boom".utf8), focused: Data(#"{"ok":true,"data":{"tree":{"role":"window"}}}"#.utf8))
            == "開著的視窗讀不到：agent-desktop 的回答不是 JSON：boom；焦點：沒有")
}
