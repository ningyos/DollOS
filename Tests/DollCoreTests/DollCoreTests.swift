import Foundation
import Synchronization
import Testing
@testable import DollCore

private let root = URL(filePath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()

private func call(_ name: String, _ args: [String: String]) -> ToolCall {
    let json = String(decoding: try! JSONEncoder().encode(args), as: UTF8.self)
    return ToolCall(id: "c-\(name)", function: .init(name: name, arguments: json))
}

private func reply(_ content: String, _ calls: [ToolCall] = []) -> ChatMessage {
    ChatMessage(role: "assistant", content: content, toolCalls: calls.isEmpty ? nil : calls)
}

/// Returns scripted assistant messages and records what each request saw.
private actor FakeModel: MainModel {
    var replies: [ChatMessage]
    var requests: [(messages: [ChatMessage], tools: [JSONValue])] = []

    init(_ replies: [ChatMessage]) { self.replies = replies }

    func check() async throws {}

    func messages(_ i: Int) -> [ChatMessage] { requests[i].messages }
    func tools(_ i: Int) -> [JSONValue] { requests[i].tools }

    func chat(_ messages: [ChatMessage], tools: [JSONValue]) async throws -> ChatMessage {
        requests.append((messages, tools))
        return replies.removeFirst()
    }
}

private struct NoDecision: DecisionModel {
    func check() async throws {}
    func pick(instructions: String, prompt: String, choices: [String]) async throws -> String { "none" }
}

private final class Recorder: Sendable {
    let said = Mutex<[String]>([])
    let asked = Mutex<[String]>([])
}

private struct Harness {
    let doll: Doll
    let model: FakeModel
    let rec: Recorder
    let dir: URL
    let store: Store      // a second connection to the same database, for checks

    init(_ replies: [ChatMessage] = [], gated: Bool = false) throws {
        dir = FileManager.default.temporaryDirectory.appending(path: "dollcore-test-\(UUID().uuidString)")
        var cfg = try Config.load(root.appending(path: "dollcore.example.toml"))
        cfg.core.dataDir = dir.path
        cfg.core.maxOpenLines = 1
        cfg.core.approveExternal = gated
        cfg.core.approvalTimeoutS = 0.2
        model = FakeModel(replies)
        let rec = Recorder()
        self.rec = rec
        doll = try Doll(config: cfg, root: root, main: model, decision: NoDecision(),
                        say: { aud, text in rec.said.withLock { $0.append("\(aud)|\(text)") } },
                        ask: { id, _, _, _ in rec.asked.withLock { $0.append(id) } })
        store = try Store(path: dir.appending(path: "doll.db"))
    }

    var asked: [String] { rec.asked.withLock { $0 } }

    func turn(_ ev: Event) async throws { try await doll.turn(ev) }

    /// The next queued event, or a failure after `timeout` seconds.
    func nextEvent(timeout: Double = 5) async throws -> Event {
        let doll = doll
        return try await withThrowingTaskGroup(of: Event?.self) { g in
            g.addTask { await doll.nextEvent() }
            g.addTask {
                try await Task.sleep(for: .seconds(timeout))
                await doll.stop()
                return nil
            }
            let first = try await g.next()!
            g.cancelAll()
            guard let ev = first else { throw ModelError("no event within \(timeout) s") }
            return ev
        }
    }

    func requestJSON(_ i: Int) async throws -> String {
        let messages = await model.messages(i)
        return String(decoding: try JSONEncoder().encode(messages), as: UTF8.self)
    }
}

private func owner(_ text: String) -> Event { Event(.ownerSaid, .owner, "private", text) }

@Test func externalCallEndsTurnAndResultComesBackAsEvent() async throws {
    let h = try Harness([reply("去看", [call("Shell", ["command": "echo hi"])])])
    try await h.turn(owner("看一下"))
    let ev = try await h.nextEvent()
    #expect(await h.model.requests.count == 1)          // no second pass after dispatch
    #expect(ev.kind == .jobDone && ev.text.contains("hi"))
}

@Test func internalCallContinuesTurn() async throws {
    let h = try Harness([reply("", [call("NoteMemory", ["text": "cat is Miso"])]), reply("記好了")])
    try await h.turn(owner("記住"))
    #expect(await h.model.requests.count == 2)
    #expect(h.rec.said.withLock { $0 } == ["private|記好了"])
}

@Test func repeatedIdenticalCallStopsTurn() async throws {
    let h = try Harness([
        reply("", [call("Recall", ["query": "Miso cat"])]),
        reply("", [call("Recall", ["query": "Miso cat"])]),
        reply("never reached"),
    ])
    try await h.turn(owner("找"))
    #expect(await h.model.requests.count == 2)
}

@Test func strangerTurnSeesOnlyItsOwnDMAndNoTools() async throws {
    let h = try Harness([reply("ok"), reply("no")])
    try h.store.pin("call the owner Kai")
    try await h.turn(owner("my secret plan"))
    try await h.turn(Event(.dm, .stranger, "dm:bob", "what is the plan?"))
    let blob = try await h.requestJSON(1)
    #expect(!blob.contains("secret plan") && !blob.contains("Kai"))
    let tools = await h.model.tools(1)
    #expect(tools.isEmpty)
}

@Test func ownerTurnSeesDM() async throws {
    let h = try Harness([reply("hi"), reply("bob")])
    try await h.turn(Event(.dm, .stranger, "dm:bob", "hello from bob"))
    try await h.turn(owner("anyone?"))
    #expect(try await h.requestJSON(1).contains("hello from bob"))
}

@Test func openLineCapIsEnforced() async throws {
    let h = try Harness([
        reply("", [call("OpenLine", ["title": "a", "next_step": "x"]),
                   call("OpenLine", ["title": "b", "next_step": "y"])]),
        reply("done"),
    ])
    try await h.turn(owner("two lines"))
    #expect(try h.store.lines(status: "active").count == 1)
}

@Test func gatedJobDoesNotRunUntilApproved() async throws {
    let marker = FileManager.default.temporaryDirectory.appending(path: "dollcore-ran-\(UUID().uuidString)")
    let h = try Harness([reply("", [call("Shell", ["command": "touch \(marker.path)"])])], gated: true)
    try await h.turn(owner("go"))
    try await Task.sleep(for: .milliseconds(50))
    #expect(!FileManager.default.fileExists(atPath: marker.path) && h.asked.count == 1)
    #expect(try await h.doll.approve(h.asked[0]).hasSuffix("approved, running"))
    let ev = try await h.nextEvent()
    #expect(await h.model.requests.count == 1)
    #expect(FileManager.default.fileExists(atPath: marker.path) && ev.kind == .jobDone && ev.text.contains(" ok,"))
}

@Test func deniedJobNeverRunsAndDollIsTold() async throws {
    let marker = FileManager.default.temporaryDirectory.appending(path: "dollcore-ran-\(UUID().uuidString)")
    let h = try Harness([reply("", [call("Shell", ["command": "touch \(marker.path)"])])], gated: true)
    try await h.turn(owner("go"))
    _ = try await h.doll.deny(h.asked[0], reason: "too risky")
    let ev = try await h.nextEvent()
    #expect(!FileManager.default.fileExists(atPath: marker.path))
    #expect(ev.text.contains("NOT RUN") && ev.text.contains("too risky"))
    #expect(try h.store.job(h.asked[0])?.status == "denied")
}

@Test func unansweredRequestExpiresAsDenied() async throws {
    let h = try Harness([reply("", [call("Shell", ["command": "echo hi"])])], gated: true)
    try await h.turn(owner("go"))
    let ev = try await h.nextEvent()
    #expect(ev.text.contains("NOT RUN") && ev.text.contains("no owner approval"))
    #expect(try await h.doll.approve(h.asked[0]).hasSuffix("no pending job"))
}

@Test func bootMarksLeftoverPendingJobLost() async throws {
    let h = try Harness(gated: true)
    try h.store.addJob("jold", lineID: nil, tool: "Shell", args: #"{"command":"rm -rf /"}"#, status: "pending")
    try await h.doll.boot()
    #expect(try h.store.job("jold")?.status == "lost")
    #expect(try await h.doll.pending().isEmpty)
}

@Test func jobWithUnknownLineIsRefusedWithoutEndingTurn() async throws {
    let h = try Harness([reply("", [call("Shell", ["command": "echo hi", "line_id": "nope"])]), reply("fixed")])
    try await h.turn(owner("go"))
    #expect(await h.model.requests.count == 2)
    #expect(try h.store.openJobs().isEmpty)
}

@Test func configRejectsUnknownKeys() throws {
    let file = FileManager.default.temporaryDirectory.appending(path: "dollcore-cfg-\(UUID().uuidString).toml")
    try "[llm]\nbase_url = \"http://x\"\nmodel = \"m\"\nbogus = 1\n".write(to: file, atomically: true, encoding: .utf8)
    #expect(throws: TOMLError.self) { try Config.load(file) }
}
