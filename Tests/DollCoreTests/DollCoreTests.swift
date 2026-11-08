import Foundation
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
private let useComputer: (String, [(String, String)]) = ("use_computer", [])
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

/// The VM: records actions; a screenshot is "frame <n> <region or full>", so bytes change only when
/// the test moves the frame (or `changing` moves it on every capture). The window outline names
/// the frame too, or fails with `windowFailure`.
private final class FakeComputer: ComputerDriver, Sendable {
    struct State {
        var actions: [ComputerAction] = []
        var failNext: String?
        var frame = 0
        var changing = false
        var captures = 0
        var windowFailure: String?
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
            return Data("frame \(s.frame) \(region.map { "\($0)" } ?? "full")".utf8)
        }
    }

    func windowSummary() async throws -> String {
        try state.withLock { s in
            if let f = s.windowFailure { throw ComputerError(f) }
            return "前景 app：Fake，視窗標題「frame \(s.frame)」\nwindow \"frame \(s.frame)\"\n  button \"OK\""
        }
    }

    func setFrame(_ n: Int) { state.withLock { $0.frame = n } }
    func setChanging(_ on: Bool) { state.withLock { $0.changing = on } }
    func failNext(_ text: String) { state.withLock { $0.failNext = text } }
    func failWindow(_ text: String?) { state.withLock { $0.windowFailure = text } }
    var actions: [ComputerAction] { state.withLock { $0.actions } }
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
    // Two screenshots, each the stored file's exact bytes, as data URIs in the tool messages.
    let shots = try h.toolResults().compactMap(\.image)
    #expect(shots.count == 2)
    let bytes = try shots.map { try Data(contentsOf: h.dir.appending(path: "shots/\($0)")) }
    #expect(bytes.map { String(decoding: $0, as: UTF8.self) } == ["frame 0 full", "frame 7 full"])
    let uris = bytes.map { "data:image/jpeg;base64," + $0.base64EncodedString() }
    #expect(reqs[3].filter { $0.image != nil }.map(\.image!) == uris)
    #expect(reqs[3].filter { $0.image != nil }.allSatisfy { $0.role == "tool" })
    // On the wire: [text part, image_url part, window outline text part].
    let wire = String(decoding: try enc([reqs[3].first { $0.image != nil }!]), as: UTF8.self)
    #expect(wire.contains(#""content":[{"text":"#)
            && wire.contains(#"{"image_url":{"url":"\#(uris[0])"},"type":"image_url"},{"text":"\n前景 app：Fake"#))
    // The system prompt, tool block included, is the same on every turn.
    #expect(Set(reqs.map { $0[0] }.map { "\($0.role)|\($0.content)" }).count == 1)
    #expect(reqs[0][0].role == "system" && reqs[0][0].content.hasPrefix("# Tools\n"))
}

@Test func toolResultsGiveHerTheNextTurnButAWatchAckAloneDoesNot() async throws {
    let h = try await Harness([Scripted("", useComputer), "好了", Scripted("盯著", watch("window", 1)), "沒變"])
    let doll = h.doll
    let runner = Task { try await doll.run() }
    await h.doll.ownerSaid("開電腦")
    try await Task.sleep(for: .milliseconds(500))
    #expect(await h.model.requests.count == 2 && h.said == ["好了"])     // the result woke her once
    await h.doll.ownerSaid("盯著畫面")
    try await Task.sleep(for: .milliseconds(500))
    #expect(await h.model.requests.count == 3)                          // the watch ack did not wake her
    // max_wait (1 s) passing wakes her; poll instead of a fixed sleep so a busy parallel suite can't race it.
    let deadline = ContinuousClock.now + .seconds(5)
    while await h.model.requests.count < 4, ContinuousClock.now < deadline { try await Task.sleep(for: .milliseconds(50)) }
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
    #expect(first.hasPrefix("【狀況】\n- 現在 ") && first.contains("- 選單：頂層（電腦工具鎖著；use_computer() 打開）")
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
    #expect(try h.toolResults().last!.text == "失敗：key 現在鎖著：先呼叫 use_computer() 打開電腦選單")
    #expect(h.last.actions == ["error key"] && h.vm.actions.isEmpty)
    #expect(h.last.footer.contains("- 選單：頂層"))
    try await h.turn(Scripted("", useComputer))
    let menu = try h.toolResults().last!
    #expect(menu.text.hasPrefix("電腦選單打開了") && menu.image != nil)
    let listed = menu.text.components(separatedBy: "\n").filter { $0.hasPrefix("{\"type\": \"function\"") }
    let names = ["click", "type", "key", "scroll", "drag", "screenshot", "watch", "leave_computer"]
    #expect(listed.count == names.count)
    for (line, name) in zip(listed, names) {
        #expect(line.contains("\"name\": \"\(name)\""))
        #expect((try? JSONSerialization.jsonObject(with: Data(line.utf8))) != nil)
    }
    try await h.turn(Scripted("", key("Return")))
    #expect(h.last.actions == ["key"] && h.vm.actions == [.key("Return")])
    #expect(h.last.footer.contains("- 選單：電腦（用完呼叫 leave_computer() 離開）"))
    try await h.turn(Scripted("", leaveComputer))
    #expect(try h.toolResults().last!.text == "離開電腦選單了；電腦工具鎖回去了。")
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
    #expect(results.prefix(6).allSatisfy { $0.text.hasSuffix("\nDone: 1 action in 0.1 s.") })
    #expect(h.vm.actions == [.click(x: 100, y: 200, button: "left", count: 1), .clickText("Save", button: "right", count: 2),
                             .type("sw_vers\n"), .key("cmd+s"), .scroll(direction: "down", amount: 5),
                             .drag(x0: 1, y0: 2, x1: 3, y1: 4)])
    #expect(h.last.actions == ["click", "click", "type", "key", "scroll", "drag", "screenshot"])
    try await h.turn(Scripted("", ("click", [("x", "1")]), ("click", [("x", "1"), ("y", "2"), ("text", "A")]),
                              ("scroll", [("direction", "sideways")])))
    #expect(try h.toolResults().suffix(3).map(\.text) == ["失敗：click 要給 x 和 y，或給 text",
                                                          "失敗：click 給 x、y 或 text 其中一種，不要兩種都給",
                                                          "失敗：scroll 的 direction 只能是 up、down、left、right，收到 sideways"])
    #expect(h.vm.actions.count == 6)
}

@Test func aVMPalFailureIsAFailedResultNeverASuccess() async throws {
    let h = try await menuHarness()
    let err = "vmpal: Stopped at action 1 of 1 (left_click): No text “紫色按鈕” on the screen after 3.1 s."
    h.vm.failNext(err)
    try await h.turn(Scripted("", ("click", [("text", "紫色按鈕")])))
    let r = try h.toolResults().last!
    #expect(r.text == "失敗：click「紫色按鈕」\n\(err)")
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

@Test func everyComputerResultCarriesTheWindowOutlineAfterItsScreenshotStoredVerbatim() async throws {
    let h = try await menuHarness()
    h.vm.setFrame(1)
    try await h.turn(Scripted("", key("cmd+n"), screenshotCall))
    let results = try h.toolResults()                                  // use_computer's, key's, screenshot's
    #expect(results.count == 3 && results.allSatisfy { $0.image != nil })
    #expect(results[0].window == "前景 app：Fake，視窗標題「frame 0」\nwindow \"frame 0\"\n  button \"OK\"")
    #expect(results[1].window == "前景 app：Fake，視窗標題「frame 1」\nwindow \"frame 1\"\n  button \"OK\"")
    #expect(results[2].window == results[1].window)
    #expect(h.last.actions == ["key", "screenshot"])
    // The screen moves on; later requests still send the stored outline, byte for byte.
    h.vm.setFrame(2)
    try await h.turn("好")
    try await h.turn("嗯")
    let earlier = await h.model.seen(2), later = await h.model.seen(3)
    #expect(try ChatMessage.encoder.encode(Array(later.prefix(earlier.count))) == ChatMessage.encoder.encode(earlier))
    let message = try #require(earlier.last { $0.role == "tool" })
    let json = try JSONSerialization.jsonObject(with: ChatMessage.encoder.encode(message)) as! [String: Any]
    let parts = json["content"] as! [[String: Any]]
    #expect(parts.map { $0["type"] as! String } == ["text", "image_url", "text"])
    #expect(parts[2]["text"] as? String == "\n" + results[2].window!)
}

@Test func aFailedWindowOutlineIsSaidInTheResultNotDropped() async throws {
    let h = try await menuHarness()
    h.vm.failWindow("WINDOW_NOT_FOUND: No focused window found")
    try await h.turn(Scripted("", key("Escape")))
    let r = try h.toolResults().last!
    #expect(r.text == "key Escape\nDone: 1 action in 0.1 s." && r.image != nil)
    #expect(r.window == "前景視窗概要失敗：WINDOW_NOT_FOUND: No focused window found")
    #expect(h.last.actions == ["key", "error window"])
    #expect(h.logs.contains { $0.contains("window outline failed") })
}

@Test func theWindowOutlineIsOneLinePerElementWithoutRefs() async throws {
    let ok = #"{"version":"2.6","ok":true,"command":"snapshot","data":{"app":"Safari","ref_count":2,"snapshot_id":"s1","#
        + #""window":{"id":"w-1","title":"Safari"},"tree":{"name":"Apple","role":"window","children":["#
        + #"{"role":"group","children":[{"role":"scrollarea","children_count":2,"ref_id":"@s1:e1"}]},"#
        + #"{"role":"button","name":"Go back","ref_id":"@s1:e2","states":["disabled"]}]}}}"#
    let want = "前景 app：Safari，視窗標題「Apple」\nwindow \"Apple\"\n  group\n    scrollarea (2)\n  button \"Go back\""
    #expect(try WindowSummary.render(Data(ok.utf8)) == want)
    let err = #"{"version":"2.6","ok":false,"command":"snapshot","error":{"code":"WINDOW_NOT_FOUND","message":"No focused window found"}}"#
    // Through the driver: agent-desktop in the VM via `vmpal exec`, its path as $1.
    var cfg = ComputerConfig()
    cfg.vmID = "E6813EBA"
    let seen = Mutex<[String]>([])
    let good = VMPalDriver(cfg, vmpal: { args in seen.withLock { $0 = args }; return ok })
    #expect(try await good.windowSummary() == want)
    #expect(seen.withLock { $0 } == ["exec", "E6813EBA", "--", "/bin/sh", "-c", "\"$1\" snapshot --skeleton -i --compact",
                                     "sh", cfg.agentDesktop])
    let bad = VMPalDriver(cfg, vmpal: { _ in throw ComputerError(err) })   // agent-desktop exits 1 with its JSON answer
    do {
        _ = try await bad.windowSummary()
        Issue.record("a failed outline came back as a success")
    } catch let e as ComputerError {
        #expect(e.description == "WINDOW_NOT_FOUND: No focused window found")
    }
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
    #expect(try h.toolResults().last!.text == "離開電腦選單了；電腦工具鎖回去了，watch 也停了。")
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
