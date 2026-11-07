import Foundation
import Synchronization
import Testing
@testable import DollCore

private let root = URL(filePath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()

/// Returns scripted replies and records what each request saw.
private actor FakeModel: MainModel {
    var replies: [String]
    var requests: [(messages: [ChatMessage], effort: String)] = []

    init(_ replies: [String]) { self.replies = replies }

    func check() async throws {}

    /// When set, the next request hangs until the call is cancelled (a long model call).
    var stallNext = false

    func chat(_ messages: [ChatMessage], effort: String) async throws -> ModelReply {
        requests.append((messages, effort))
        if stallNext {
            stallNext = false
            try await Task.sleep(for: .seconds(30))
        }
        guard !replies.isEmpty else { throw ModelError("script ran out at request \(requests.count)") }
        let content = replies.removeFirst()
        let prompt = messages.reduce(0) { $0 + estimateTokens($1.content) + 4 }
        return ModelReply(content: content, usage: ModelUsage(promptTokens: prompt, completionTokens: 10))
    }

    func stall() { stallNext = true }

    func add(_ more: [String]) { replies += more }
    func seen(_ i: Int) -> [ChatMessage] { requests[i].messages }
}

private final class Recorder: Sendable {
    let said = Mutex<[String]>([])
    let reports = Mutex<[TurnReport]>([])
    let logs = Mutex<[String]>([])
}

private struct Harness {
    let doll: Doll
    let model: FakeModel
    let rec: Recorder
    let dir: URL
    let store: Store      // a second connection to the same database, for checks

    init(_ replies: [String] = [], tune: (inout Config) -> Void = { _ in }) async throws {
        dir = FileManager.default.temporaryDirectory.appending(path: "dollcore-test-\(UUID().uuidString)")
        var cfg = try Config.load(root.appending(path: "dollcore.example.toml"))
        cfg.core.dataDir = dir.path
        cfg.shell.scratchDir = dir.appending(path: "scratch").path
        cfg.shell.waitS = 0.3
        tune(&cfg)
        model = FakeModel(replies)
        let rec = Recorder()
        self.rec = rec
        doll = try Doll(config: cfg, root: root, model: model, say: { text in rec.said.withLock { $0.append(text) } })
        await doll.setObserver { r in rec.reports.withLock { $0.append(r) } }
        await doll.setLogSink { line in rec.logs.withLock { $0.append(line) } }
        store = try Store(path: dir.appending(path: "doll.db"))
    }

    var said: [String] { rec.said.withLock { $0 } }
    var reports: [TurnReport] { rec.reports.withLock { $0 } }
    var logs: [String] { rec.logs.withLock { $0 } }
    var last: TurnReport { reports.last! }

    func turn(_ replies: String...) async throws {
        await model.add(replies)
        try await doll.runOnce()
    }

    /// Waits until the queue has drained into the timeline `count` events of `kind`, or gives up.
    func waitFor(_ kind: Event.Kind, count: Int = 1, seconds: Double = 5) async throws {
        let deadline = ContinuousClock.now + .seconds(seconds)
        while ContinuousClock.now < deadline {
            if await doll.queue.filter({ $0.kind == kind }).count >= count { return }
            try await Task.sleep(for: .milliseconds(20))
        }
    }
}

// parsing --------------------------------------------------------------------

@Test func replyParsesCommandAndThinkAndStripsThemFromSpeech() {
    let r = Reply.parse("""
    【我想一下】high
    先弄這個
    ```sh
    mkdir -p data/raw
    ```
    """)
    #expect(r.think == "high" && r.command == "mkdir -p data/raw" && r.speech == "先弄這個" && r.usedTool)
    let plain = Reply.parse("【決定】線：主人\n哈囉")
    #expect(plain.command == nil && plain.think == nil && !plain.usedTool && plain.speech == "【決定】線：主人\n哈囉")
}

// queue and turns ------------------------------------------------------------

@Test func drainOrdersByConfiguredPriority() async throws {
    let h = try await Harness()
    await h.doll.put(Event(.result, .internal, "r"))
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
    try await h.turn("【我想一下】high\n等我")                 // next call is high
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

@Test func shortCommandResultIsAnEventAndLongOneBecomesAJob() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("做")
    try await h.turn("```sh\necho hi\n```")
    #expect(h.last.actions == ["ran"] && h.last.usedTool)
    try await h.waitFor(.result)
    try await h.turn("```sh\nsleep 1; echo done; exit 3\n```")
    #expect(h.last.actions.first!.hasPrefix("job j"))
    let id = String(h.last.actions.first!.dropFirst(4))
    let seen = await h.model.seen(1).map(\.content).joined(separator: "\n")
    #expect(seen.contains(" 結果]\n$ echo hi") && seen.contains("$ echo hi\nhi\n(exit 0)"))
    #expect(try h.store.events().last!.text.contains("轉背景 job \(id)"))
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
    try await h.turn("```sh\nmkdir -p data/raw && echo made > made.txt\n```")
    try await h.waitFor(.result)
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
    try await h.turn("【我想一下】high\n等我")
    let status = try await h.doll.status()
    #expect(h.last.actions == ["think high", "said"] && status.contains("effort：high"))
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
    try await h.turn("```sh\ntrue\n```")                  // a tool call resets the count
    try await h.waitFor(.result)
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
    try await h.turn("```sh\nsleep 30\n```")
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

@Test func configRejectsUnknownKeysAndBadPriority() throws {
    let file = FileManager.default.temporaryDirectory.appending(path: "dollcore-cfg-\(UUID().uuidString).toml")
    try "[llm]\nbase_url = \"http://x\"\nmodel = \"m\"\nbogus = 1\n".write(to: file, atomically: true, encoding: .utf8)
    #expect(throws: TOMLError.self) { try Config.load(file) }
    try "[llm]\nbase_url = \"http://x\"\nmodel = \"m\"\n[core]\npriority = [\"owner_text\"]\n".write(to: file, atomically: true, encoding: .utf8)
    #expect(throws: TOMLError.self) { try Config.load(file) }
    try "[llm]\nbase_url = \"http://x\"\nmodel = \"m\"\n[idle]\nmax_turns_without_tool = 0\n".write(to: file, atomically: true, encoding: .utf8)
    #expect(throws: TOMLError.self) { try Config.load(file) }
}
