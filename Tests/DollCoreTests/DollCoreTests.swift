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

private func shell(_ cmd: String) -> (String, [(String, String)]) { ("shell", [("cmd", cmd)]) }
private func think(_ level: String) -> (String, [(String, String)]) { ("think", [("level", level)]) }
private func recall(_ q: String) -> (String, [(String, String)]) { ("recall", [("query", q)]) }
private func remember(_ items: [(String, Bool)]) -> (String, [(String, String)]) {
    struct Item: Encodable { let text: String; let secret: Bool }
    return ("remember", [("items", String(decoding: try! JSONEncoder().encode(items.map { Item(text: $0.0, secret: $0.1) }), as: UTF8.self))])
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

/// Stands in for Apple FM: kinds by memory text (default happened), candidates scripted per
/// extraction call, and an optional delay that makes extraction slow.
private actor FakeDecision: DecisionModel {
    var kinds: [String: MemoryKind] = [:]
    var scripted: [[String]] = []
    var delay: Duration = .zero
    var judged: [(source: String, memory: String)] = []
    var extracted: [[Utterance]] = []
    var extractStarts: [ContinuousClock.Instant] = []
    var extractEnds: [ContinuousClock.Instant] = []

    func check() async throws {}

    /// Memory texts whose kind judgment fails (an FM error).
    var failing: Set<String> = []

    func judgeKind(source: String, memory: String) async throws -> KindJudgment {
        judged.append((source, memory))
        if failing.contains(memory) { throw ModelError("FM unavailable") }
        return KindJudgment(kind: kinds[memory] ?? .happened, reason: "fake")
    }

    func extractCandidates(_ utterances: [Utterance], max: Int) async throws -> [Candidate] {
        extracted.append(utterances)
        extractStarts.append(.now)
        try await Task.sleep(for: delay)
        extractEnds.append(.now)
        let texts = scripted.isEmpty ? [] : scripted.removeFirst()
        return texts.prefix(max).map { Candidate(text: $0, sourceSeq: utterances.last!.seq) }
    }

    func set(kind: MemoryKind, for memory: String) { kinds[memory] = kind }
    func fail(on memory: String) { failing.insert(memory) }
    func script(_ calls: [[String]]) { scripted += calls }
    func slow(_ d: Duration) { delay = d }
}

private final class Recorder: Sendable {
    let said = Mutex<[String]>([])
    let reports = Mutex<[TurnReport]>([])
    let logs = Mutex<[String]>([])
}

private struct Harness {
    let doll: Doll
    let model: FakeModel
    let decision: FakeDecision
    let rec: Recorder
    let dir: URL
    let store: Store      // a second connection to the same database, for checks

    init(_ replies: [Scripted] = [], tune: (inout Config) -> Void = { _ in }) async throws {
        dir = FileManager.default.temporaryDirectory.appending(path: "dollcore-test-\(UUID().uuidString)")
        var cfg = try Config.load(root.appending(path: "dollcore.example.toml"))
        cfg.core.dataDir = dir.path
        cfg.shell.scratchDir = dir.appending(path: "scratch").path
        cfg.memory.dir = dir.appending(path: "memory").path
        cfg.shell.waitS = 0.3
        tune(&cfg)
        model = FakeModel(replies)
        let decision = FakeDecision()
        self.decision = decision
        let rec = Recorder()
        self.rec = rec
        doll = try Doll(config: cfg, root: root, model: model, decision: decision,
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
        + "<tool_call>\n<function=shell>\n<parameter=cmd>\nls\n</parameter>\n</function>\n</tool_call>\n"
        + "<tool_call><function=think><parameter=level>high</parameter></function></tool_call>"
    let p = ParsedReply(raw)
    #expect(p.speech == "好，我查一下再跑。")
    #expect(p.calls.map(\.name) == ["recall", "shell", "think"])
    #expect(p.calls.map { $0.parameters[0].value } == ["A", "ls", "high"])
    #expect(p.calls.allSatisfy { $0.malformed == nil })
    #expect(ParsedReply("就聊聊，沒有呼叫。\n").calls.isEmpty && ParsedReply("就聊聊，沒有呼叫。\n").speech == "就聊聊，沒有呼叫。")
}

@Test func parserStripsExactlyOneNewlineEachSideAndKeepsTheRest() {
    let raw = "<tool_call>\n<function=shell>\n<parameter=cmd>\n\n  echo a\n  echo b  \n\n</parameter>\n</function>\n</tool_call>"
    #expect(ParsedReply(raw).calls[0].parameters == [.init("cmd", "\n  echo a\n  echo b  \n")])
    let tight = "<tool_call><function=shell><parameter=cmd>pwd</parameter></function></tool_call>"
    #expect(ParsedReply(tight).calls[0].parameters == [.init("cmd", "pwd")])
}

@Test func jsonParametersDecodeFromTheirText() throws {
    let specs = Tools.specs(shell: try Config.load(root.appending(path: "dollcore.example.toml")).shell,
                            memory: try Config.load(root.appending(path: "dollcore.example.toml")).memory)
    let raw = "<tool_call>\n<function=remember>\n<parameter=items>\n"
        + "[{\"text\": \"主人養貓\", \"secret\": false},\n {\"text\": \"姐姐的事\", \"secret\": \"true\"}]\n</parameter>\n</function>\n</tool_call>"
    guard case .remember(let items) = try Tools.decode(ParsedReply(raw).calls[0], specs: specs) else {
        Issue.record("not remember"); return
    }
    #expect(items.map(\.text) == ["主人養貓", "姐姐的事"] && items.map(\.secret) == [false, true])
    let bad = ParsedReply("<tool_call>\n<function=remember>\n<parameter=items>\n主人養貓\n</parameter>\n</function>\n</tool_call>")
    #expect(throws: ToolError.self) { try Tools.decode(bad.calls[0], specs: specs) }
}

@Test func malformedBlocksBecomeCallsThatSayWhatIsWrong() {
    // unclosed <tool_call>, then a good one
    let a = ParsedReply("嗯\n<tool_call>\n<function=shell>\n<parameter=cmd>\nls\n</parameter>\n</function>\n"
                        + "<tool_call>\n<function=think>\n<parameter=level>\nhigh\n</parameter>\n</function>\n</tool_call>")
    #expect(a.speech == "嗯")
    #expect(a.calls.map(\.name) == ["shell", "think"])
    #expect(a.calls[0].malformed == "<tool_call> 沒有 </tool_call> 收尾" && a.calls[1].malformed == nil)
    // <function> outside <tool_call>
    let b = ParsedReply("<function=shell>\n<parameter=cmd>\nls\n</parameter>\n</function>")
    #expect(b.calls == [ToolCall(name: "shell", malformed: "<function=shell> 沒有包在 <tool_call> 和 </tool_call> 裡")])
    #expect(b.speech == "")
    // missing </function>, missing </parameter>, junk inside, no function, duplicate parameter
    let cases: [(String, String)] = [
        ("<tool_call>\n<function=shell>\n<parameter=cmd>\nls\n</parameter>\n</tool_call>", "shell：<function=shell> 沒有 </function> 收尾"),
        ("<tool_call>\n<function=shell>\n<parameter=cmd>\nls\n</function>\n</tool_call>", "shell：參數 cmd 沒有 </parameter> 收尾"),
        ("<tool_call>\n<function=shell>\ncmd: ls\n</function>\n</tool_call>", "shell：<function=shell> 裡除了 <parameter=…>…</parameter> 還有別的字：cmd: ls"),
        ("<tool_call>\n{\"name\": \"shell\"}\n</tool_call>", "<tool_call> 裡面要先寫 <function=工具名>"),
        ("<tool_call><function=shell><parameter=cmd>a</parameter><parameter=cmd>b</parameter></function></tool_call>", "shell：參數 cmd 寫了兩次"),
    ]
    for (raw, problem) in cases {
        #expect(ParsedReply(raw).calls.map(\.malformed) == [problem], "\(raw)")
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
    for (line, name) in zip(listed, ["think", "remember", "recall", "shell"]) {
        #expect(line.contains("\"name\": \"\(name)\""))
        #expect((try? JSONSerialization.jsonObject(with: Data(line.utf8))) != nil)   // valid JSON
    }
}

// tool calls -----------------------------------------------------------------

@Test func everyCallInOneReplyRunsInOrderAndGetsItsOwnResult() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("做三件事")
    try await h.turn(Scripted("好，一次做完", shell("echo one"), think("medium"), shell("echo two")))
    #expect(h.said == ["好，一次做完"])                                  // only the speech is said
    #expect(h.last.actions == ["said", "ran", "think medium", "ran"] && h.last.usedTool)
    let results = try h.toolResults()
    #expect(results.map(\.text) == ["$ echo one\none\n(exit 0)", "下一回合 effort：medium", "$ echo two\ntwo\n(exit 0)"])
    #expect(await h.doll.followUp)                                       // the results give her the next turn
    try await h.turn("都好了")
    let msgs = await h.model.seen(1)
    let a = try #require(msgs.firstIndex { $0.role == "assistant" })
    // her raw content, call text included, is what goes back
    #expect(msgs[a].content == Scripted("好，一次做完", shell("echo one"), think("medium"), shell("echo two")).raw)
    #expect(msgs[(a + 1)...(a + 3)].map(\.role) == ["tool", "tool", "tool"])
    #expect(msgs[(a + 1)...(a + 3)].map(\.content) == results.map(\.text))
    #expect(await h.model.requests.map(\.effort) == ["none", "medium"])
}

@Test func aRawReplyRunsItsCallsInTheOrderSheWroteThem() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("查一下再列目錄")
    let raw = "我先查，再看目錄。\n\n<tool_call>\n<function=recall>\n<parameter=query>\n目錄\n</parameter>\n</function>\n</tool_call>\n"
        + "<tool_call>\n<function=shell>\n<parameter=cmd>\necho listed\n</parameter>\n</function>\n</tool_call>"
    try await h.turn(Scripted(stringLiteral: raw))
    #expect(h.said == ["我先查，再看目錄。"])
    #expect(h.last.actions == ["said", "recall 0", "ran"])
    #expect(try h.toolResults().map(\.text) == ["目錄：0 條", "$ echo listed\nlisted\n(exit 0)"])
    let doll = try #require(try h.store.events().first { $0.kind == .doll })
    #expect(doll.text == raw)                                            // stored byte-identical
}

@Test func failedCallsStillGetAToolResult() async throws {
    let h = try await Harness()
    await h.decision.fail(on: "會失敗的記憶")
    await h.doll.ownerSaid("試試")
    try await h.turn(Scripted("", ("bogus", []), ("shell", [("command", "ls")]), think("low"),
                              remember([("會失敗的記憶", false), ("主人喜歡貓", false)]), ("recall", []), shell("echo ok"),
                              ("remember", [("items", "主人養貓")])))
    let results = try h.toolResults()
    #expect(results.count == 7)
    #expect(results[0].text == "失敗：沒有 bogus 這個工具")
    #expect(results[1].text == "失敗：shell 沒有 command 這個參數")
    #expect(results[2].text == "失敗：think 的 level 只能是 medium 或 high，收到 low")
    #expect(results[3].text.hasPrefix("第 1 條沒存：FM unavailable\n第 2 條已存入 public/"))
    #expect(results[4].text == "失敗：recall 少了參數 query")
    #expect(results[5].text == "$ echo ok\nok\n(exit 0)")                // later calls still run
    #expect(results[6].text.hasPrefix("失敗：remember 的 items 要是 JSON 陣列"))
    #expect(h.last.actions == ["error bogus", "error shell", "error think", "error remember", "remember public",
                               "error recall", "ran", "error remember"])
    #expect(await h.doll.effortNext == nil)
}

@Test func aMalformedCallGetsAResultSayingWhatWasWrong() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("試試")
    try await h.turn("<function=shell>\n<parameter=cmd>\nls\n</parameter>\n</function>")
    #expect(h.said.isEmpty)
    #expect(try h.toolResults().map(\.text) == ["失敗：<function=shell> 沒有包在 <tool_call> 和 </tool_call> 裡"])
    #expect(h.last.actions == ["error shell"] && h.last.usedTool)
}

@Test func aPastToolRoundRendersByteIdenticallyOnLaterTurns() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("記一下我養貓，然後看目錄")
    try await h.turn(Scripted("好", remember([("主人養貓", false)]), shell("ls")))
    try await h.turn("記好了，目錄是空的")                                 // her follow-up turn
    await h.doll.ownerSaid("謝謝")
    try await h.turn("不客氣")
    let reqs = await h.model.requests.map(\.messages)
    let enc = { (m: [ChatMessage]) in try ChatMessage.encoder.encode(m) }
    // Each request is the previous one plus new messages: same bytes, footer included.
    for i in 1..<reqs.count {
        #expect(try enc(Array(reqs[i].prefix(reqs[i - 1].count))) == (try enc(reqs[i - 1])))
    }
    #expect(reqs[2].contains { $0.role == "tool" && $0.content.hasPrefix("$ ls\n") })
    // The system prompt, tool block included, is the same on every turn.
    #expect(Set(reqs.map { $0[0] }.map { "\($0.role)|\($0.content)" }).count == 1)
    #expect(reqs[0][0].role == "system" && reqs[0][0].content.hasPrefix("# Tools\n"))
}

@Test func toolResultsGiveHerTheNextTurnButABackgroundAckAloneDoesNot() async throws {
    let h = try await Harness([Scripted("", shell("echo hi")), "好了", Scripted("跑久一點", shell("sleep 1; echo late")), "跑完了"])
    let doll = h.doll
    let runner = Task { try await doll.run() }
    await h.doll.ownerSaid("跑")
    try await Task.sleep(for: .milliseconds(500))
    #expect(await h.model.requests.count == 2 && h.said == ["好了"])     // the result woke her once
    await h.doll.ownerSaid("再跑一個久的")
    try await Task.sleep(for: .milliseconds(600))
    #expect(await h.model.requests.count == 3)                          // the background ack did not wake her
    try await Task.sleep(for: .seconds(1.5))
    #expect(await h.model.requests.count == 4 && h.said.last == "跑完了") // the job event did
    await h.doll.stop()
    try await runner.value
}

// queue and turns ------------------------------------------------------------

@Test func drainOrdersByConfiguredPriority() async throws {
    let h = try await Harness()
    await h.doll.put(Event(.job, .internal, "r"))
    await h.doll.ownerSaid("text")
    await h.doll.ownerSaid("voice", voice: true)
    try await h.turn("好")
    let events = try h.store.events()
    #expect(events.map(\.text) == ["voice", "text", "r", "好"])
    #expect(events.map(\.priority) == [.ownerVoice, .ownerText, .jobResult, .jobResult])
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

@Test func shortCommandResultIsAToolResultAndLongOneBecomesAJob() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("做")
    try await h.turn(Scripted("", shell("echo hi")))
    #expect(h.last.actions == ["ran"] && h.last.usedTool)
    try await h.turn(Scripted("", shell("sleep 1; echo done; exit 3")))
    #expect(h.last.actions.first!.hasPrefix("job j"))
    #expect(await !h.doll.followUp)                                     // the ack alone is no new turn
    let id = String(h.last.actions.first!.dropFirst(4))
    let seen = await h.model.seen(1)
    #expect(seen.contains(ChatMessage(role: "tool", content: "$ echo hi\nhi\n(exit 0)")))
    let ack = try h.toolResults().last!
    #expect(ack.text.contains("轉背景 job \(id)"))
    #expect(try h.store.runningJobs().map(\.id) == [id])
    let status = try await h.doll.status()
    #expect(status.contains("背景 job：\(id)（sleep 1; echo done; exit 3）跑了"))
    try await h.waitFor(.job, seconds: 5)
    try await h.turn("跑完了，exit 3")
    let after = await h.model.seen(2).map(\.content).joined(separator: "\n")
    #expect(after.contains(" job 跑完]\n") && after.contains("\(id)（sleep 1; echo done; exit 3）\ndone\n(exit 3)"))
    #expect(try h.store.job(id)?.status == "failed" && (try h.store.runningJobs()).isEmpty)
    #expect(try h.store.events().filter { $0.kind == .job }.count == 1)
}

@Test func commandsRunInTheScratchDir() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("做")
    try await h.turn(Scripted("", shell("mkdir -p data/raw && echo made > made.txt")))
    try await h.turn("ok")
    let seen = await h.model.seen(1).map(\.content).joined(separator: "\n")
    #expect(seen.contains("$ mkdir -p data/raw && echo made > made.txt\n(exit 0)"))
    #expect(FileManager.default.fileExists(atPath: h.dir.appending(path: "scratch/data/raw").path))
    #expect(try String(contentsOf: h.dir.appending(path: "scratch/made.txt"), encoding: .utf8) == "made\n")
}

// footer ---------------------------------------------------------------------

@Test func footerIsStoredAndReplayedVerbatimBeforeHerReply() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("嗨")
    try await h.turn("哈囉")
    let first = h.last.footer
    #expect(first.hasPrefix("【狀況】\n- 現在 ") && first.contains("- 背景 job：沒有") && first.contains("下一回合 effort：none"))
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
    #expect(await h.model.requests.map(\.effort) == ["none", "high", "none"])
    #expect(h.reports[1].footer.contains("effort：high") && h.reports[2].footer.contains("effort：none"))
}

// idle -----------------------------------------------------------------------

@Test func idleTurnsWithoutAToolCallWriteOneLogLine() async throws {
    let h = try await Harness { $0.idle.maxTurnsWithoutTool = 2 }
    let idle = { h.logs.filter { $0.contains("idle:") } }
    await h.doll.ownerSaid("1")
    try await h.turn("嗯")
    #expect(idle().isEmpty)
    await h.doll.ownerSaid("2")
    try await h.turn(Scripted("", shell("true")))                     // a tool call resets the count
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

@Test func bootKillsJobsLeftByThePreviousProcess() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("做")
    try await h.turn(Scripted("", shell("sleep 30")))
    let id = String(h.last.actions.first!.dropFirst(4))
    let pgid = try h.store.job(id)!.pgid!
    #expect(kill(pgid, 0) == 0)
    let again = try await Harness { $0.core.dataDir = h.dir.path }
    try await again.doll.boot()
    try await Task.sleep(for: .milliseconds(300))
    #expect(kill(pgid, 0) != 0 && errno == ESRCH)
    #expect(again.logs.contains { $0.hasPrefix("WARN job \(id) (sleep 30) was running") })
    #expect(try again.store.runningJobs().isEmpty)
    await h.doll.stop()
}

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
    let ids = try h.store.memoryFTS(match: "\"Strata\"", includeSecret: false, limit: 5)
    #expect(ids.count == 1)
    let meta = try #require(try h.store.memoryMeta(ids[0]))
    #expect(!meta.secret && meta.confidence > 0.8)
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
        #expect(hits.contains("〔確定的") || hits.contains("〔大概的") || hits.contains("〔聽說的"))
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
    #expect(try h.store.memoryFTS(match: "\"kettlebell\"", includeSecret: true, limit: 10).isEmpty)
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
    #expect(withSecret.contains(secretText) && withSecret.contains("〔確定的已發生｜secret〕"))
    #expect(try h.toolResults().last!.secret)          // a secret hit marks the tool result
}

// memory (M3b) ---------------------------------------------------------------

@Test func rememberedKindComesFromTheDecisionModelIntoFileAndIndex() async throws {
    let h = try await Harness()
    await h.decision.set(kind: .intended, for: "主人下週要去東京")
    await h.doll.ownerSaid("我下週要去東京")
    try await h.turn(Scripted("", remember([("主人下週要去東京", false)])))
    let judged = await h.decision.judged
    #expect(judged.count == 1 && judged[0].source == "[主人] 我下週要去東京" && judged[0].memory == "主人下週要去東京")
    let dir = h.dir.appending(path: "memory/public")
    let files = try FileManager.default.contentsOfDirectory(atPath: dir.path).filter { $0.hasSuffix(".md") }
    #expect(files.count == 1)
    let text = try String(contentsOf: dir.appending(path: files[0]), encoding: .utf8)
    #expect(text.contains("kind = \"intended\""))
    let meta = try #require(try h.store.memoryMeta(String(files[0].dropLast(3))))
    #expect(meta.kind == "intended")
    #expect(try h.toolResults().last!.text.contains("kind intended"))
    // the index rebuilt from markdown keeps it
    try await h.doll.rebuildMemory()
    #expect(try h.store.memoryMeta(String(files[0].dropLast(3)))?.kind == "intended")
}

@Test func recallShowsConfidenceLevelAndKindTogether() async throws {
    let h = try await Harness()
    await h.decision.set(kind: .intended, for: "主人下週要去東京")
    await h.doll.ownerSaid("我下週要去東京")
    try await h.turn(Scripted("", remember([("主人下週要去東京", false)])))
    await h.doll.ownerSaid("我要去哪？")
    try await h.turn(Scripted("", recall("主人要去東京")))
    let out = try h.toolResults().last!.text
    #expect(out.contains("〔確定的打算｜public〕\n主人下週要去東京"))
    let hit = MemoryHit(id: "x", folder: "public", secret: false, confidence: 0.3, level: "聽說", kind: .happened, text: "")
    #expect(hit.label == "聽說的已發生")
}

@Test func candidatesShowOnceInTheNextFooterAndAreNeverWritten() async throws {
    let h = try await Harness()
    await h.decision.script([["主人對花生過敏"], []])
    await h.doll.ownerSaid("我對花生過敏")
    try await h.turn("好，我知道了")                            // turn 1: extraction runs beside the call
    let owner = try h.store.events().first { $0.kind == .owner }!
    #expect(await h.decision.extracted.map { $0.map(\.seq) } == [[owner.seq!]])
    #expect(!h.reports[0].footer.contains("【可能值得記】"))
    await h.doll.ownerSaid("嗯")
    try await h.turn("嗯嗯")                                    // turn 2: shows turn 1's candidate
    #expect(h.reports[1].footer.contains("【可能值得記】") && h.reports[1].footer.contains("- 主人對花生過敏"))
    await h.doll.put(Event(.job, .internal, "r"))
    try await h.turn("好")                                      // turn 3: gone (turn 2 extracted nothing)
    #expect(!h.reports[2].footer.contains("【可能值得記】") && !h.reports[2].footer.contains("花生"))
    // proposals only; nothing reaches memory unless she calls remember
    #expect(try h.store.countMemoryIndex() == 0)
    let pub = (try? FileManager.default.contentsOfDirectory(atPath: h.dir.appending(path: "memory/public").path)) ?? []
    #expect(pub.filter { $0.hasSuffix(".md") }.isEmpty)
    #expect(h.logs.contains { $0.contains("candidates from turn 1: #\(owner.seq!) 主人對花生過敏") })
}

@Test func slowExtractionDoesNotDelayTheModelCall() async throws {
    let h = try await Harness { $0.memory.candidateWaitS = 5 }
    await h.decision.slow(.seconds(1.5))
    await h.decision.script([["主人下週要去東京"]])
    await h.doll.ownerSaid("我下週要去東京")
    let t0 = ContinuousClock.now
    try await h.turn("好")
    let turnTime = ContinuousClock.now - t0
    #expect(turnTime < .seconds(1))                             // the turn did not wait for it
    let callStart = await h.model.starts[0]
    #expect(await h.decision.extractEnds.isEmpty)               // still extracting when the turn ended
    #expect(callStart < (await h.decision.extractStarts[0]) + .seconds(1.5))
    await h.doll.put(Event(.job, .internal, "r"))
    try await h.turn("嗯")                                      // next turn waits (within candidate_wait_s)
    #expect(h.reports[1].footer.contains("- 主人下週要去東京"))
}

@Test func extractionNotDoneWithinTheWaitIsSkippedAndLogged() async throws {
    let h = try await Harness { $0.memory.candidateWaitS = 0.2 }
    await h.decision.slow(.seconds(3))
    await h.decision.script([["主人下週要去東京"]])
    await h.doll.ownerSaid("我下週要去東京")
    try await h.turn("好")
    await h.doll.put(Event(.job, .internal, "r"))
    let t0 = ContinuousClock.now
    try await h.turn("嗯")
    #expect(ContinuousClock.now - t0 < .seconds(1.5))
    #expect(!h.reports[1].footer.contains("【可能值得記】"))
    #expect(h.logs.contains { $0.contains("candidate extraction for turn 1 not done after 0.2 s; skipped") })
}

@Test func noExtractionOnTurnsWithoutOwnerEvents() async throws {
    let h = try await Harness()
    await h.doll.put(Event(.job, .internal, "$ ls\n(exit 0)"))
    try await h.turn("好")
    #expect(await h.decision.extracted.isEmpty)
    await h.doll.ownerSaid("早安")
    try await h.turn("早")
    #expect(await h.decision.extracted.count == 1)
}
