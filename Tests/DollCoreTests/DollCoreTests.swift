import AVFoundation
import CryptoKit
import Foundation
import ImageIO
import Network
import Synchronization
import Testing
@testable import DollCore

private let root = URL(filePath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()

/// One scripted reply: her text content plus her tool calls (name, parameters), returned the way
/// Strata returns them with `tools` sent: the calls in `tool_calls`, every argument a JSON string
/// (`Tools.decode` reads numbers, booleans and arrays from the text). A string literal is a reply
/// with no `tool_calls`; call markup in it comes back as failed calls (`ToolCall.unread`), the way
/// the server leaves a call it could not read in the content.
private struct Scripted: ExpressibleByStringLiteral {
    var content: String
    var calls: [(String, [(String, String)])]
    init(_ content: String = "", _ calls: (String, [(String, String)])...) { self.content = content; self.calls = calls }
    init(stringLiteral value: String) { self.init(value) }

    var toolCalls: [ToolCall] {
        calls.map { name, params in ToolCall(name: name, arguments: JSONValue.object(params.map { ($0.0, .string($0.1)) }).text) }
            + ToolCall.unread(content: content, rejected: [])
    }
    /// Her message as a later request replays it.
    var message: ChatMessage { ChatMessage(role: "assistant", content: content, toolCalls: toolCalls) }
}

/// `act(steps…, expect:)`: her steps (each one step's JSON) as the `steps` array text.
private func act(_ steps: String..., expect: String? = nil) -> (String, [(String, String)]) {
    ("act", [("steps", "[" + steps.joined(separator: ", ") + "]")] + (expect.map { [("expect", $0)] } ?? []))
}
/// `keyStep("cmd+s")` → {"key": "s", "modifiers": ["cmd"]}; a lone key has no modifiers.
private func keyStep(_ chord: String) -> String {
    let parts = chord.split(separator: "+").map(String.init)
    let mods = parts.dropLast().map { "\"\($0)\"" }.joined(separator: ", ")
    return #"{"key": "\#(parts.last!)""# + (mods.isEmpty ? "" : #", "modifiers": [\#(mods)]"#) + "}"
}
/// A one-step `act` pressing the chord.
private func key(_ chord: String) -> (String, [(String, String)]) { act(keyStep(chord)) }
/// `todo.set(items)`; the default item stands for what `use_computer(expect)` said before 2026-11-14.
private func todoSet(_ items: String...) -> (String, [(String, String)]) {
    let list = items.isEmpty ? ["畫面上看得到結果"] : items
    return ("todo.set", [("items", String(decoding: try! JSONEncoder().encode(list), as: UTF8.self))] + planArgs)
}
/// The five fields a `todo.set` that starts a list hands in (2026-11-17, hook 1), for tests about something else.
/// Its one fact quotes the 【狀況】 her reply answers (every reply has one, with this line).
private let planArgs: [(String, String)] = [("facts", #"[{"fact":"【狀況】列了今天的行事曆","quote":"今天的行事曆"}]"#), ("assumptions", "需要的 app 已經開著"),
                                            ("limits", "只動這一件，不碰別的"), ("estimate", "5"), ("check", "讀視窗看到做完的樣子")]
private let planFacts = [PlanFact(fact: "【狀況】列了今天的行事曆", quote: "今天的行事曆", source: "【狀況】")]
/// Evidence any Calculator read holds (its window line), the default for tests about something other
/// than the evidence check (design A); ignored for an item that used neither the computer nor files.
private let seenCalculator = #"AXWindow "計算機""#
private func done(_ result: String, evidence: String? = seenCalculator) -> (String, [(String, String)]) {
    ("todo.done", [("result", result)] + (evidence.map { [("evidence", $0)] } ?? []))
}
/// `todo.fail`; the default evidence is Calculator's window line, in any read of the fake driver (hook 4).
private func fail(_ cause: String, tried: String = "試過了", evidence: String = seenCalculator) -> (String, [(String, String)]) {
    ("todo.fail", [("cause", cause), ("evidence", evidence), ("tried", tried)])
}
private func todoWait(_ reason: String) -> (String, [(String, String)]) { ("todo.wait", [("reason", reason)]) }
private func progress(_ note: String) -> (String, [(String, String)]) { ("todo.progress", [("note", note)]) }
/// Reads Calculator's front window (the fake driver's one app with a window from the start).
private let readCall: (String, [(String, String)]) = ("read_window", [("app", "Calculator")])
/// A one-step `act` clicking a row of the latest read.
private func clickRow(_ row: Int, count: Int? = nil) -> (String, [(String, String)]) {
    act(#"{"click": \#(row)"# + (count.map { #", "count": \#($0)"# } ?? "") + "}")
}
private func dm(_ text: String) -> (String, [(String, String)]) { ("discord.dm", [("text", text)]) }
private func reply(_ text: String) -> (String, [(String, String)]) { ("dollctl.reply", [("text", text)]) }
private func voiceSay(_ text: String) -> (String, [(String, String)]) { ("voice.say", [("text", text)]) }
private let voiceJoin: (String, [(String, String)]) = ("voice.join", [])
private let voiceLeave: (String, [(String, String)]) = ("voice.leave", [])
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
    /// Each request's `tools`.
    var tools: [String?] = []

    init(_ replies: [Scripted]) { self.replies = replies }

    func check() async throws {}

    /// When set, the next request hangs until the call is cancelled (a long model call).
    var stallNext = false
    /// What one image counts in the reported prompt size (the server counts them; the footer scales by it).
    var imageTokens = 350
    func setImageTokens(_ n: Int) { imageTokens = n }

    func chat(_ messages: [ChatMessage], tools: String?, effort: String) async throws -> ModelReply {
        requests.append((messages, effort))
        self.tools.append(tools)
        starts.append(.now)
        if stallNext || stallRequest == requests.count {
            stallNext = false
            do { try await Task.sleep(for: .seconds(30)) } catch {
                cancelled += 1
                throw error
            }
        }
        guard !replies.isEmpty else { throw ModelError("script ran out at request \(requests.count)") }
        let r = replies.removeFirst()
        let prompt = messages.reduce(0) { $0 + estimateTokens($1.content) + 4 + $1.images.count * imageTokens }
        return ModelReply(content: r.content, calls: r.toolCalls, usage: ModelUsage(promptTokens: prompt, completionTokens: 10,
                                                                                        finish: r.toolCalls.isEmpty ? "stop" : "tool_calls"))
    }

    func stall() { stallNext = true }
    /// The request with this number (1 = the first) hangs until it is cancelled.
    var stallRequest: Int?
    func stall(request n: Int) { stallRequest = n }
    /// Hanging requests that were cancelled.
    var cancelled = 0

    func add(_ more: [Scripted]) { replies += more }
    func seen(_ i: Int) -> [ChatMessage] { requests[i].messages }
}

private final class Recorder: Sendable {
    let said = Mutex<[String]>([])
    /// Who each `said` was delivered to (`discord.dm` → owner, `dollctl.reply` → developer).
    let saidTo = Mutex<[Addressee]>([])
    /// The screenshot each `said` carried (design B), nil for none.
    let images = Mutex<[URL?]>([])
    /// Set: every delivery fails with this reason (the bridge is down, no chat open).
    let undelivered = Mutex<String?>(nil)
    let reports = Mutex<[TurnReport]>([])
    let logs = Mutex<[String]>([])
}

/// A fake `cua-driver`: answers each tool the way the real one did (2026-11-16, Calculator and
/// TextEdit) from a scripted desktop, writes some bytes as the screenshot of every window read, and
/// records every call (tool, JSON arguments). Calculator (pid 100) has one window; TextEdit (pid
/// 200) runs without one until 檔案 › 新增.
private final class FakeCua: ComputerDriver, Sendable {
    struct Element {
        var role: String
        var label: String?
        var parent: Int? = 0
        var id: String?
        var subrole: String?
    }
    struct Window {
        var id: Int
        var pid: Int
        var app: String
        var title: String
        var elements: [Element]
        var z = 0
        var onScreen = true
        var truncated = false
    }
    struct App {
        var name: String
        var bundleID: String
        var pid: Int
        var running = true
    }
    struct State {
        var apps: [App] = [App(name: "Calculator", bundleID: "com.apple.calculator", pid: 100),
                           App(name: "TextEdit", bundleID: "com.apple.TextEdit", pid: 200)]
        var windows: [Window] = [FakeCua.calculator]
        var calls: [(tool: String, json: String)] = []
        var snapshots = 0
        /// Tool → the driver's refusal (its JSON answer), thrown on that tool's next call.
        var refuse: [String: String] = [:]
        /// After a successful action or menu call: how the desktop changes (default: `FakeCua.press`).
        var after: @Sendable (String, [String: Any], inout State) -> Void = FakeCua.press
        /// launch_app with `urls`: each file gets a window titled by its name, behind the others
        /// (false: the app shows nothing for it, as TextEdit with a dialog open did on 2026-11-16).
        var opensFiles = true
        /// start_session / end_session calls (tool, arguments), kept apart from `calls`.
        var sessions: [(tool: String, json: String)] = []
    }
    let state = Mutex(State())

    static let calculator = Window(id: 62541, pid: 100, app: "計算機", title: "計算機", elements: [
        Element(role: "AXWindow", label: "計算機", parent: nil, id: "main"),
        Element(role: "AXStaticText", label: "0"),
        Element(role: "AXButton", label: "7", id: "Seven"),
        Element(role: "AXButton", label: "8", id: "Eight"),
        Element(role: "AXButton", label: "+", id: "Add"),
        Element(role: "AXButton", label: "=", id: "Equals"),
    ])
    static let untitled = Window(id: 63000, pid: 200, app: "TextEdit", title: "未命名", elements: [
        Element(role: "AXWindow", label: "未命名", parent: nil),
        Element(role: "AXTextArea", label: nil, id: "First Text View"),
    ])

    /// A digit button pressed shows on Calculator's display (row 1); TextEdit's 檔案 › 新增 opens a window.
    static func press(_ tool: String, _ args: [String: Any], _ s: inout State) {
        if tool == "invoke_menu", args["pid"] as? Int == 200, args["path"] as? [String] == ["檔案", "新增"] {
            s.windows.append(untitled)
            return
        }
        guard tool == "click", let token = args["element_token"] as? String, let row = Int(token.split(separator: ":").last ?? ""),
              let w = s.windows.firstIndex(where: { $0.id == args["window_id"] as? Int }), s.windows[w].pid == 100,
              let label = s.windows[w].elements[row].label, Int(label) != nil else { return }
        let shown = s.windows[w].elements[1].label ?? "0"
        s.windows[w].elements[1].label = shown == "0" ? label : shown + label
    }

    func call(_ tool: String, _ json: String) async throws -> String {
        let args = try JSONSerialization.jsonObject(with: Data(json.utf8)) as! [String: Any]
        return try state.withLock { s in
            if tool == "start_session" || tool == "end_session" {
                s.sessions.append((tool, json))
                return "{}"
            }
            s.calls.append((tool, json))
            if let refusal = s.refuse.removeValue(forKey: tool) { throw ComputerError(refusal) }
            switch tool {
            case "list_apps":
                return Self.text(["apps": s.apps.map { ["name": $0.name, "bundle_id": $0.bundleID, "pid": $0.pid, "running": $0.running] }])
            case "list_windows":
                let pid = args["pid"] as? Int
                return Self.text(["windows": s.windows.filter { pid == nil || $0.pid == pid }.map {
                    ["window_id": $0.id, "pid": $0.pid, "app_name": $0.app, "title": $0.title, "layer": 0, "is_on_screen": $0.onScreen, "z_index": $0.z]
                }])
            case "launch_app":
                guard let i = s.apps.firstIndex(where: { $0.bundleID == args["bundle_id"] as? String || $0.name == args["name"] as? String }) else {
                    throw ComputerError(#"{"error":"no app named \#(args["name"] ?? "?")"}"#)
                }
                s.apps[i].running = true
                if s.opensFiles, let urls = args["urls"] as? [String] {
                    for u in urls where !u.contains("://") {
                        let name = (u as NSString).lastPathComponent
                        s.windows.append(Window(id: 64000 + s.windows.count, pid: s.apps[i].pid, app: s.apps[i].name, title: name, elements: [
                            Element(role: "AXWindow", label: name, parent: nil), Element(role: "AXTextArea", label: nil, id: "First Text View")]))
                    }
                }
                let a = s.apps[i]
                return Self.text(["pid": a.pid, "bundle_id": a.bundleID, "name": a.name, "windows": [] as [Any]])
            case "get_window_state":
                guard let w = s.windows.first(where: { $0.id == args["window_id"] as? Int }) else {
                    throw ComputerError(#"{"refusal":{"code":"window_not_found","message":"no such window"}}"#)
                }
                s.snapshots += 1
                let snapshot = String(format: "s%08d", s.snapshots)
                if let out = args["screenshot_out_file"] as? String {
                    let url = URL(filePath: out)
                    try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
                    try Data("shot \(snapshot) \(w.title)".utf8).write(to: url)
                }
                return Self.text(["snapshot_id": snapshot, "tree_markdown": Self.markdown(w), "window_title": w.title, "truncated": w.truncated,
                                  "screenshot_width": 460, "screenshot_height": 816,
                                  "elements": w.elements.enumerated().map { i, e in
                                      var d: [String: Any] = ["element_index": i, "element_token": "\(snapshot):\(i)", "role": e.role]
                                      if let l = e.label { d["label"] = l }
                                      if let p = e.parent { d["parent_index"] = p }
                                      if let r = e.subrole { d["subrole"] = r }
                                      return d
                                  }])
            case "click", "double_click", "type_text", "press_key", "hotkey", "scroll", "invoke_menu":
                let after = s.after
                after(tool, args, &s)
                return #"{"effect":"unverifiable"}"#
            case "run_steps":
                // Each step as the inner tool's own call (after this one). Names never reach the
                // driver: dollcore resolves them to an element token (`Mac.resolve`).
                var results: [[String: Any]] = []
                for (i, step) in (args["steps"] as? [[String: Any]] ?? []).enumerated() {
                    let inner = step["tool"] as? String ?? ""
                    let a = step["args"] as? [String: Any] ?? [:]
                    s.calls.append((inner, Self.text(a)))
                    if let refusal = s.refuse.removeValue(forKey: inner) { throw ComputerError(refusal) }
                    let after = s.after
                    after(inner, a, &s)
                    results.append(["index": i, "ok": true, "message": "Sent, effect not verified"])
                }
                return Self.text(["executed": results.count, "ok": true, "steps": results])
            default:
                throw ComputerError("fake cua-driver: unexpected tool \(tool)")
            }
        }
    }

    static func text(_ j: [String: Any]) -> String {
        String(decoding: try! JSONSerialization.data(withJSONObject: j, options: [.sortedKeys]), as: UTF8.self)
    }

    /// The tree as cua-driver writes it: `- [row] Role "label" [id=… actions=[press]]`, indented by depth.
    static func markdown(_ w: Window) -> String {
        w.elements.enumerated().map { i, e in
            var depth = 0, p = e.parent
            while let pi = p { depth += 1; p = w.elements[pi].parent }
            return String(repeating: "    ", count: depth) + "- [\(i)] \(e.role)" + (e.label.map { " \"\($0)\"" } ?? "")
                + " [" + (e.id.map { "id=\($0) " } ?? "") + "actions=[press]]"
        }.joined(separator: "\n")
    }

    func refuse(_ tool: String, _ answer: String) { state.withLock { $0.refuse[tool] = answer } }
    func change(_ f: (inout State) -> Void) { state.withLock { f(&$0) } }
    /// Every call's tool name, in order.
    var tools: [String] { state.withLock { $0.calls.map(\.tool) } }
    /// The arguments of every call of `tool`, in order.
    func args(_ tool: String) -> [[String: Any]] {
        state.withLock { $0.calls.filter { $0.tool == tool }.map { try! JSONSerialization.jsonObject(with: Data($0.json.utf8)) as! [String: Any] } }
    }
    /// The actions sent (not the reads): tool names in order.
    var actions: [String] {
        tools.filter { ["click", "double_click", "type_text", "press_key", "hotkey", "scroll", "invoke_menu"].contains($0) }
    }
}

/// The memory keys of every test doll (by data dir): never the login Keychain.
let testKeys = MemoryKeyStore()

/// The memory key of the test doll whose data dir is `data` (made when it has none yet).
func testBox(_ data: URL) throws -> SecretBox {
    try SecretBox.unlock(store: testKeys, account: SecretBox.account(data: data), sealedPlace: nil)
}

/// The embedding model's stand-in: a bag of the text's characters and character pairs, each hashed
/// (FNV-1a) into one of 512 dimensions, L2-normalized. Texts sharing more characters are nearer;
/// the same text is 1. Queries and documents alike.
final class FakeEmbedder: Embedder, Sendable {
    static let dims = 512

    func embed(_ texts: [String], as role: EmbeddingRole) throws -> [[Float]] {
        texts.map(Self.vector)
    }

    static func vector(_ text: String) -> [Float] {
        func bucket(_ s: String) -> Int {
            var h: UInt64 = 0xcbf29ce484222325
            for b in s.utf8 { h = (h ^ UInt64(b)) &* 0x100000001b3 }
            return Int(h % UInt64(dims))
        }
        var v = [Float](repeating: 0, count: dims)
        let chars = Array(text.lowercased())
        for (i, c) in chars.enumerated() {
            v[bucket(String(c))] += 1
            if i + 1 < chars.count { v[bucket(String(chars[i...(i + 1)]))] += 1 }
        }
        let norm = v.reduce(0) { $0 + $1 * $1 }.squareRoot()
        guard norm > 0 else { v[0] = 1; return v }
        return v.map { $0 / norm }
    }
}

/// The real embedding model (`dollcore.example.toml`'s), loaded once for the tests that are about
/// meaning: the M3a corpus and recall by meaning. Needs the weights in the Hugging Face cache.
private let qwen3 = Task { try await MLXEmbedder.load(try exampleConfig().memory) }

/// A memory's id as she sees it (〔…｜記憶 12-ab34cd〕, 2026-11-18) left out, for texts compared whole.
func noIDs(_ s: String) -> String { s.replacing(/｜記憶 [0-9]+-[0-9a-f]{6}〕/, with: "〕") }

/// `dollcore.example.toml` with 娃娃's pack: the example now starts Doll (owner, 2026-11-21), and the
/// tests keep 娃娃, the pack they were written and measured with (it stays in the project as an example).
func exampleConfig() throws -> Config {
    var c = try Config.load(root.appending(path: "dollcore.example.toml"))
    c.core.pack = "character_packs/doll2/doll.toml"
    return c
}

private struct Harness {
    let doll: Doll
    let model: FakeModel
    let cua: FakeCua
    let rec: Recorder
    let dir: URL
    let store: Store      // a second connection to the memory index, for checks

    /// `dir`: reuse a data dir (a restart); a fresh one by default.
    init(_ replies: [Scripted] = [], dir reuse: URL? = nil, embedder: (any Embedder)? = nil, clock: (any DollClock)? = nil,
         tune: (inout Config) -> Void = { _ in }) async throws {
        dir = reuse ?? FileManager.default.temporaryDirectory.appending(path: "dollcore-test-\(UUID().uuidString)")
        var cfg = try exampleConfig()
        cfg.core.dataDir = dir.path
        cfg.computer.settleS = 0
        cfg.computer.windowWaitS = 0
        cfg.memory.dir = dir.appending(path: "memory").path
        cfg.maintenance.enabled = false   // the maintenance tests switch it on
        cfg.drives.enabled = false        // the drives tests switch it on
        cfg.ownerSleep.enabled = false    // the owner-sleep tests switch it on
        cfg.goals.review = false          // the ideal-self review tests switch it on
        cfg.audit.enabled = false         // the memory audit tests switch it on
        cfg.computer.estimateSimilarity = 2   // the estimate check tests switch it on (no cosine reaches 2)
        tune(&cfg)
        model = FakeModel(replies)
        let rec = Recorder()
        self.rec = rec
        let cua = FakeCua()
        self.cua = cua
        doll = try Doll(config: cfg, root: root, model: model, embedder: embedder ?? FakeEmbedder(), computer: cua,
                        keys: testKeys, deliver: { text, to, image in
                            rec.said.withLock { $0.append(text) }
                            rec.saidTo.withLock { $0.append(to) }
                            rec.images.withLock { $0.append(image) }
                            return rec.undelivered.withLock { $0 }
                        })
        await doll.setObserver { r in rec.reports.withLock { $0.append(r) } }
        if let clock { await doll.setClock(clock) }
        await doll.setLogSink { line in rec.logs.withLock { $0.append(line) } }
        store = try Store(path: dir.appending(path: "doll.db"))
    }

    var said: [String] { rec.said.withLock { $0 } }
    var saidTo: [Addressee] { rec.saidTo.withLock { $0 } }
    /// The screenshot file each delivered line carried (design B), nil for none.
    var images: [String?] { rec.images.withLock { $0 }.map { $0?.lastPathComponent } }
    var reports: [TurnReport] { rec.reports.withLock { $0 } }
    var logs: [String] { rec.logs.withLock { $0 } }
    var last: TurnReport { reports.last! }

    func turn(_ replies: Scripted...) async throws {
        await model.add(replies)
        try await doll.runOnce()
    }

    /// The timeline as a new process reads it from the file.
    func timeline() throws -> Timeline { try Timeline(dir: dir.appending(path: "timeline"), box: try testBox(dir)) }
    func events() throws -> [Event] { try timeline().events }

    /// Her tool results, oldest first.
    func toolResults() throws -> [Event] { try events().filter { $0.kind == .tool } }

    /// Waits until the queue has drained into the timeline `count` events of `kind` (the guard: `waitUntil`).
    func waitFor(_ kind: Event.Kind, count: Int = 1, seconds: Double = waitGuard,
                 sourceLocation: SourceLocation = #_sourceLocation) async throws {
        let deadline = ContinuousClock.now + .seconds(seconds)
        while ContinuousClock.now < deadline {
            if await doll.queue.filter({ $0.kind == kind }).count >= count { return }
            try await Task.sleep(for: .milliseconds(20))
        }
        throw WaitTimeout(seconds: seconds, at: sourceLocation)
    }
}

// her calls ------------------------------------------------------------------

/// One call as the server returns it in `tool_calls`, every argument a JSON string.
private func call(_ name: String, _ params: [(String, String)]) -> ToolCall { Scripted("", (name, params)).toolCalls[0] }

@Test func argumentsReadAsParametersStringsAsTheyAreOthersAsJSONText() {
    let c = ToolCall(name: "x", arguments: "{\"text\": \"\\n  echo a\\n\", \"x\": 120, \"ok\": true, \"mods\": [\"cmd\", \"shift\"], \"gone\": null}")
    #expect(c.malformed == nil && c.arguments != nil)
    #expect(c.parameters == [.init("mods", "[\"cmd\",\"shift\"]"), .init("ok", "true"), .init("text", "\n  echo a\n"), .init("x", "120")])
    #expect(ToolCall(name: "voice.join", arguments: "").parameters.isEmpty && ToolCall(name: "voice.join", arguments: "").malformed == nil)
    #expect(ToolCall(name: "voice.join", arguments: "{}").malformed == nil)
    for bad in ["[1]", "\"text\"", "{\"text\": "] {
        #expect(ToolCall(name: "recall", arguments: bad).malformed?.hasPrefix("recall 的參數不是一個 JSON 物件，沒有執行") == true, "\(bad)")
    }
}

@Test func callsTheServerReturnedAsTextBecomeFailedCalls() {
    // strata_rejected_tool_calls first (one per name), then the markup left beyond them
    let content = "嗯\n<tool_call>\n<function=bogus>\n</function>\n</tool_call>\n<tool_call>\n<parameter=level>\nhigh\n</parameter>\n</tool_call>"
    let calls = ToolCall.unread(content: content, rejected: ["bogus"])
    #expect(calls.map(\.name) == ["bogus", ""])
    #expect(calls.map(\.malformed) == ["沒有 bogus 這個工具，沒有執行", ToolCall.unreadable])
    #expect(calls.allSatisfy { $0.arguments == nil })
    #expect(ToolCall.unread(content: "<invoke name=\"use_computer\">\n</invoke>", rejected: []).count == 1)
    #expect(ToolCall.unread(content: "<function=recall>\n</function>", rejected: []).count == 1)
    #expect(ToolCall.unread(content: "就聊聊，沒有呼叫。", rejected: []).isEmpty)
}

@Test func keyIsOneKeyWithModifiersInAFixedOrder() throws {
    let cfg = try exampleConfig()
    let specs = Tools.computerSpecs(cfg.computer, cfg.files)
    func decode(_ step: String) throws -> Tools.Request {
        try Tools.decode(call("act", [("steps", "[\(step)]")]), specs: specs)
    }
    func chord(_ step: String) throws -> String? {
        guard case .act(let steps, _) = try decode(step), case .key(let k, let mods) = steps.first else { return nil }
        return Tools.chord(key: k, modifiers: mods)
    }
    #expect(try chord(#"{"key": "Return"}"#) == "return")
    // Any order, case or repeat she writes comes out as the one order, so the same chord is the same step.
    #expect(try chord(#"{"key": "S", "modifiers": ["alt", "CMD", "shift", "cmd"]}"#) == "cmd+shift+alt+s")
    #expect(try chord(#"{"key": "a", "modifiers": []}"#) == "a")
    // A chord or a sequence in `key`, an unknown modifier, or modifiers that are not a JSON array: errors that say how.
    for bad in [#"{"key": "cmd a"}"#, #"{"key": "cmd+a"}"#, #"{"key": "a", "modifiers": ["option"]}"#, #"{"key": "a", "modifiers": "cmd"}"#] {
        #expect(throws: ToolError.self) { try decode(bad) }
    }
    do { _ = try decode(#"{"key": "cmd shift s"}"#); Issue.record("accepted") } catch let e as ToolError {
        #expect(e.description.hasPrefix("act 的第 1 步的 key 只放一個鍵（例：\"return\"），修飾鍵放 modifiers"))
    }
}

@Test func jsonParametersDecodeFromTheirText() throws {
    let specs = Tools.specs(memory: try exampleConfig().memory, voice: false, computer: ComputerConfig(), files: FilesConfig())
    let items = "[{\"text\": \"主人養貓\", \"folder\": \"public\"},\n {\"text\": \"我的 VM 密碼\", \"folder\": \"own\"}, {\"text\": \"姐姐的事\", \"folder\": \"secret\"}]"
    // as a JSON string (the text of the array) and as the array itself
    for c in [call("remember", [("items", items)]), ToolCall(name: "remember", arguments: "{\"items\": \(items)}")] {
        guard case .remember(let got) = try Tools.decode(c, specs: specs) else {
            Issue.record("not remember"); return
        }
        #expect(got.map(\.text) == ["主人養貓", "我的 VM 密碼", "姐姐的事"] && got.map(\.folder) == [.public, .own, .secret])
    }
    let wrongFolder = call("remember", [("items", "[{\"text\": \"x\", \"folder\": \"private\"}]")])
    #expect(throws: ToolError.self) { try Tools.decode(wrongFolder, specs: specs) }
    let bad = call("remember", [("items", "主人養貓")])
    #expect(throws: ToolError.self) { try Tools.decode(bad, specs: specs) }
    // an array of strings as a JSON value and as its text
    for c in [ToolCall(name: "todo.set", arguments: "{\"items\": [\"a\", \"b\"]}"), call("todo.set", [("items", "[\"a\", \"b\"]")])] {
        guard case .todoSet(let got, let sub, _) = try Tools.decode(c, specs: specs) else { Issue.record("not todo.set"); return }
        #expect(got == ["a", "b"] && sub == nil)
    }
}

@Test func aFailedCallsResultIsItsProblem() throws {
    let specs = Tools.specs(memory: try exampleConfig().memory, voice: false, computer: ComputerConfig(), files: FilesConfig())
    let failed = ToolCall.unread(content: "<invoke name=\"use_computer\">\n</invoke>", rejected: [])[0]
    do { _ = try Tools.decode(failed, specs: specs); Issue.record("accepted") } catch {
        #expect("\(error)" == ToolCall.unreadable)
    }
}

@Test func theSystemPromptIsThePersonaAndEveryToolGoesInTools() async throws {
    let h = try await Harness()
    let sys = await h.doll.systemPrompt()
    #expect(sys.hasPrefix("你是") && !sys.contains("<tools>") && !sys.contains("<tool_call>"))
    let tools = try #require(try JSONSerialization.jsonObject(with: Data(await h.doll.toolsJSON.utf8)) as? [[String: Any]])
    let names = tools.compactMap { ($0["function"] as? [String: Any])?["name"] as? String }
    // her tools, the todo list's, then the computer's (declared from the start so the list never changes)
    #expect(names == ["discord.dm", "dollctl.reply", "think", "remember", "recall", "memory.fix", "history.list", "history.undo", "todo.set", "todo.done", "todo.fail",
                      "todo.wait", "todo.progress", "todo.continue", "calendar.add", "calendar.change", "calendar.delete", "calendar.list", "apps", "open", "read_window",
                      "act", "tool.save", "tool.run", "read_file", "write_file", "edit_file", "ask_owner"])
    #expect(tools.allSatisfy { $0["type"] as? String == "function" })
    // every request sends the same list
    await h.doll.ownerSaid("在嗎")
    try await h.turn(Scripted("", dm("在")))
    #expect(await h.model.tools == [Optional(await h.doll.toolsJSON)])
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
    // her thought and her calls, arguments as the server returned them, are what goes back
    #expect(msgs[a] == Scripted("好，一次做完", recall("一次做完"), think("medium"), recall("下一回合")).message)
    #expect(msgs[(a + 1)...(a + 3)].map(\.role) == ["tool", "tool", "tool"])
    #expect(msgs[(a + 1)...(a + 3)].map(\.content) == results.map(\.text))
    #expect(await h.model.requests.map(\.effort) == ["medium", "medium"])    // owner turn, then her think
}

@Test func callMarkupTheServerLeftInHerContentFailsAndIsStoredAsReturned() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("查一下再列目錄")
    let raw = "我先查，再看目錄。\n\n<tool_call>\n<function=recall>\n<parameter=query>\n列目錄\n</parameter>\n"
    try await h.turn(Scripted(raw, think("medium")))
    #expect(h.said.isEmpty)
    #expect(h.last.actions == ["think medium", "error tool_call"])
    #expect(try h.toolResults().map(\.text) == ["下一回合 effort：medium", "失敗：" + ToolCall.unreadable])
    let doll = try #require(try h.events().first { $0.kind == .doll })
    #expect(doll.text == raw && doll.calls.map(\.name) == ["think", ""])   // stored as returned
    try await h.turn("好")
    let msgs = await h.model.seen(1)
    let a = try #require(msgs.firstIndex { $0.role == "assistant" })
    #expect(msgs[a].content == raw && msgs[a].toolCalls.map(\.name) == ["think"])   // only real calls replay as calls
    #expect(msgs[(a + 1)...(a + 2)].map(\.role) == ["tool", "tool"])
}

@Test func failedCallsStillGetAToolResult() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("試試")
    try await h.turn(Scripted("", ("bogus", []), ("recall", [("q", "ls")]), think("low"),
                              remember(kinds: [("會失敗的記憶", false, "done"), ("主人喜歡貓", false, nil)]), ("recall", []), todoSet(), readCall,
                              ("remember", [("items", "主人養貓")])))
    let results = try h.toolResults()
    #expect(results.count == 8)
    #expect(results[0].text == "失敗：沒有 bogus 這個工具")
    #expect(results[1].text == "失敗：recall 沒有 q 這個參數（它的參數：query）")
    #expect(results[2].text == "失敗：think 的 level 只能是 medium，收到 low")
    #expect(results[3].text.hasPrefix("第 1 條沒存：kind 只能是 happened、intended、uncertain，或不填；收到 done\n第 2 條已存入 public/"))
    #expect(results[4].text == "失敗：recall 少了參數 query")
    #expect(results[5].text.hasPrefix("todo 列好了，"))                     // later calls still run
    #expect(results[6].text == "" && results[6].window?.hasPrefix(calculatorHead) == true)
    #expect(results[7].text.hasPrefix("失敗：remember 的 items 要是 JSON 陣列"))
    #expect(h.last.actions == ["error bogus", "error recall", "error think", "error remember", "remember public",
                               "error recall", "todo set", "read_window", "error remember"])
    #expect(await h.doll.effortNext == nil)
}

@Test func aCallTheServerCouldNotReadGetsAResultSayingSo() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("試試")
    try await h.turn("<function=recall>\n<parameter=query>\nls\n</parameter>\n</function>")
    #expect(h.said.isEmpty)
    #expect(try h.toolResults().map(\.text) == ["失敗：" + ToolCall.unreadable])
    #expect(h.last.actions == ["error tool_call"] && h.last.usedTool)
}

@Test func aPastToolRoundRendersByteIdenticallyOnLaterTurnsImagesIncluded() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("記一下我養貓，然後看電腦")
    try await h.turn(Scripted("好", remember([("主人養貓", false)]), todoSet(), readCall))
    try await h.turn(Scripted("記好了", think("medium")))
    try await h.turn(Scripted("畫面看過了", done("看過了")))
    await h.doll.ownerSaid("謝謝")
    try await h.turn("不客氣")
    let reqs = await h.model.requests.map(\.messages)
    let enc = { (m: [ChatMessage]) in try ChatMessage.encoder.encode(m) }
    // Each request is the previous one plus new messages: same bytes, footer and image included
    // (one read, so nothing is put away in between; done compacts only the turn after it).
    for i in 1..<3 {
        #expect(try enc(Array(reqs[i].prefix(reqs[i - 1].count))) == (try enc(reqs[i - 1])))
    }
    // The read: the stored screenshot's exact bytes, as a data URI in the tool message.
    let results = try h.toolResults().filter { $0.image != nil }
    #expect(results.count == 1)
    let bytes = try Data(contentsOf: h.dir.appending(path: "shots/\(results[0].image!)"))
    #expect(String(decoding: bytes, as: UTF8.self) == "shot s00000001 計算機")
    let uri = "data:image/jpeg;base64," + bytes.base64EncodedString()
    #expect(reqs[2].flatMap(\.images) == [uri])
    #expect(reqs[2].filter { !$0.images.isEmpty }.allSatisfy { $0.role == "tool" })
    // On the wire: [her text, the screenshot, the element list].
    let wire = String(decoding: try enc([reqs[2].first { !$0.images.isEmpty }!]), as: UTF8.self)
    #expect(wire.contains(#""content":[{"text":"#)
            && wire.contains(#"{"image_url":{"url":"\#(uri)"},"type":"image_url"},{"text":"\n\#(calculatorHead)"#))
    // The system prompt is the same on every turn, and so is `tools`.
    #expect(Set(reqs.map { $0[0] }.map { "\($0.role)|\($0.content)" }).count == 1)
    let toolLists = Set(await h.model.tools)
    #expect(reqs[0][0].role == "system" && reqs[0][0].content.hasPrefix("你是") && toolLists.count == 1)
}

// queue and turns ------------------------------------------------------------

@Test func drainOrdersByConfiguredPriority() async throws {
    let h = try await Harness()
    await h.doll.put(Event(.watch, .internal, "r"))
    await h.doll.developerSaid("dev")
    await h.doll.ownerSaid("text")
    await h.doll.ownerSaid("voice", voice: true)
    try await h.turn("好")
    let events = Array(try h.events().prefix(5))   // then the 【提醒】 for her call-less reply (2026-11-18)
    // Developer text sits just below owner text by default (2026-11-12).
    #expect(events.map(\.text) == ["voice", "text", "dev", "r", "好"])
    #expect(events.map(\.priority) == [.ownerVoice, .ownerText, .developerText, .watch, .watch])
}

@Test func ownerVoiceCancelsTheCallAndTheRerunSeesTheVoiceWithTheSameEffort() async throws {
    let h = try await Harness { $0.llm.taskTurnEffort = "none" }       // so her think shows
    await h.doll.ownerSaid("這題難")
    try await h.turn(Scripted("", dm("等我"), think("medium")))       // next call is medium
    await h.model.stall()
    await h.model.add([Scripted("", dm("好"))])
    await h.doll.ownerSaid("開始算")
    let doll = h.doll
    let running = Task { try await doll.runOnce() }
    while await h.model.requests.count < 2 { try await Task.sleep(for: .milliseconds(10)) }
    await h.doll.ownerSaid("等等不用算了", voice: true)      // cancels the stalled call
    try await running.value
    let reqs = await h.model.requests
    #expect(reqs.count == 3)
    #expect(reqs[1].effort == "medium" && reqs[2].effort == "medium")    // the think survives the cancelled call
    #expect(!reqs[1].messages.contains { $0.content.contains("等等不用算了") })
    #expect(reqs[2].messages.contains { $0.content.contains("等等不用算了") })
    #expect(h.reports.map(\.index) == [1, 2] && h.said == ["等我", "好"])
    #expect(h.logs.contains { $0.contains("turn 2 interrupted") })
    let later = try h.events().filter { $0.kind == .owner }.map(\.text)
    #expect(later == ["這題難", "開始算", "等等不用算了"])
}

@Test func noEventMeansNoTurn() async throws {
    let h = try await Harness([Scripted("", dm("哈囉"))])
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
    #expect(first.hasPrefix("【狀況】\n- 現在 ") && first.contains("- todo：待命中（沒有 todo；要做事先用 todo.set 列）")
            && !first.contains("watch") && first.contains("- context：") && first.contains("下一回合 effort：medium")
            && !first.contains("選單"))
    #expect(try h.events().last { $0.kind == .doll }!.footer == first)
    await h.doll.ownerSaid("再來")
    try await h.turn("好")
    let msgs = await h.model.seen(1)
    let i = msgs.firstIndex { $0.content == first }!
    // A reply without a call goes back as a program line, not as her assistant turn (2026-11-16).
    #expect(msgs[i].role == "user" && msgs[i + 1] == ChatMessage(role: "user", content: Doll.thoughtOnlyTag + "哈囉"))
    #expect(msgs.last!.content == h.last.footer && msgs.last!.content != first)
    #expect(try h.events().filter { $0.text.contains("【狀況】") }.isEmpty)   // footers are not events
}

@Test func thinkSetsTheNextCallsEffortOnceAndShowsInTheFooter() async throws {
    let h = try await Harness { $0.llm.taskTurnEffort = "none" }       // so her think shows
    await h.doll.ownerSaid("這題難")
    try await h.turn(Scripted("", dm("等我"), think("medium")))
    let status = try await h.doll.status()
    #expect(h.last.actions == ["said discord_dm", "think medium"] && status.contains("effort：medium"))
    await h.doll.ownerSaid("嗯")
    try await h.turn("想好了")
    await h.doll.ownerSaid("嗯")
    try await h.turn("嗯")
    #expect(await h.model.requests.map(\.effort) == ["none", "medium", "none"])
    #expect(h.reports[1].footer.contains("effort：medium") && h.reports[2].footer.contains("effort：none"))
    // Medium only (2026-11-11: `none` removed, at none she repeated think(none) 16 turns in a row;
    // 2026-11-15: `high` removed, each use re-read the whole timeline twice and answered no better).
    await h.doll.ownerSaid("再查")
    try await h.turn(Scripted("", think("none"), think("high")))
    #expect(try h.toolResults().suffix(2).map(\.text) == ["失敗：think 的 level 只能是 medium，收到 none",
                                                          "失敗：think 的 level 只能是 medium，收到 high"])
}

@Test func taskTurnsUseTaskTurnEffortAndOnlyAnEmptyTurnTheDefault() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("查一下")
    try await h.turn(Scripted("好", recall("一")))                     // owner line drained
    try await h.turn(Scripted("", recall("二")))                       // her tool result
    try await h.turn("想好了")                                          // her tool result again
    try await h.turn("嗯")                                              // nothing new: the default
    await h.doll.put(Event(.watch, .internal, "盯著的整個畫面變了。"))
    try await h.turn("看到了")                                          // a watch wake
    await h.doll.ownerSaid("再說一次", voice: true)
    try await h.turn("沒有喔")                                          // owner voice
    #expect(await h.model.requests.map(\.effort) == ["medium", "medium", "medium", "none", "medium", "medium"])
    #expect(h.reports.map(\.effort) == ["medium", "medium", "medium", "none", "medium", "medium"])
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
    #expect(noIDs(try h.toolResults().last!.text).contains("〔確定｜public〕\n主人的伺服器叫 Strata"))
    await h.doll.stop()
}

/// Vectors from older code (index version 1 kept only the first token's vector) are wrong even
/// when every file is indexed: a different index version rebuilds the whole index at boot; the
/// current one leaves it alone.
@Test func bootRebuildsAnIndexBuiltByAnotherIndexVersion() async throws {
    let h = try await Harness([Scripted("", remember([("主人的伺服器叫 Strata", false)]))])
    #expect(try h.store.memoryIndexVersion() == Memory.indexVersion && Memory.indexVersion == 5)
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
    #expect(try h.store.memoryIndexVersion() == 5)
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
// 在日本旅遊時買了什麼 shares no trigram with its record (embedding-only). These run on the real
// embedding model (Qwen3-Embedding since 2026-11-11; NLContextualEmbedding before). Qwen3 does not
// tie the old question 出國旅遊帶回什麼 to 日本…御守 (cosine 0.36, under `similarity` 0.55; 0.57 for
// this one), and a recall may now find nothing at all.

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
("主人在日本旅遊時買了什麼", 23),                      // only the embedding can find this
]

/// Writes the whole corpus through one remember call with 30 items. Returns the harness.
private func memoryHarness() async throws -> Harness {
    let h = try await Harness(embedder: try await qwen3.value)
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
        // The wanted record is one of the returned hits, with its label.
        if hits.contains(memoryCorpus[want]) {
            correct += 1
            #expect(hits.contains("〔確定｜") || hits.contains("〔大概｜") || hits.contains("〔聽說｜"))
        }
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
    #expect(try await ask(h, "主人在日本旅遊時買了什麼").contains(memoryCorpus[23]))
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
    #expect(withSecret.contains(secretText) && noIDs(withSecret).contains("〔確定｜secret〕"))
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
        let parsed = try readMemory(h.dir, dir.appending(path: name))
        #expect(try h.store.memoryMeta(String(name.dropLast(3)))?.kind == parsed.kind)
        kinds[parsed.body] = parsed.kind
    }
    #expect(kinds == ["主人下個月要去大阪": "intended", "主人的貓叫麻糬": ""])
    // the index rebuilt from markdown keeps it
    try await h.doll.rebuildMemory()
    for name in try FileManager.default.contentsOfDirectory(atPath: dir.path) where name.hasSuffix(".md") {
        let parsed = try readMemory(h.dir, dir.appending(path: name))
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
    let out = noIDs(try h.toolResults().last!.text)
    #expect(out.contains("〔確定的打算｜public〕\n主人下個月要去大阪"))
    #expect(out.contains("〔確定｜public〕\n主人的貓叫麻糬"))
    #expect(!out.contains("未判"))
    let hit = MemoryHit(id: "x", folder: "public", secret: false, confidence: 0.3, level: "聽說", kind: .happened, text: "", tier: .doll, evidence: [], deep: false)
    #expect(hit.label == "聽說的已發生")
    #expect(MemoryHit(id: "y", folder: "public", secret: false, confidence: 0.3, level: "聽說", kind: nil, text: "", tier: .doll, evidence: [], deep: false).label == "聽說")
}

// computer (M2) ----------------------------------------------------------------

/// Polls until `cond` holds. `seconds` guards against a hang; it is not a claim about speed. A test's
/// wall time in the full suite is set by the other tests running beside it (2026-11-20: the audit
/// test takes 0.41 s alone and 3.8–6 s in the suite, whose whole run takes 12–19 s): the old 3 s
/// was reached under load, the wait returned as if `cond` held, and an assertion after it failed
/// with an unrelated-looking error. The guard is above a whole suite run, and running out of it
/// fails here, naming this wait.
private func waitUntil(seconds: Double = waitGuard, sourceLocation: SourceLocation = #_sourceLocation,
                       _ cond: () async -> Bool) async throws {
    let deadline = ContinuousClock.now + .seconds(seconds)
    while ContinuousClock.now < deadline {
        if await cond() { return }
        try await Task.sleep(for: .milliseconds(10))
    }
    throw WaitTimeout(seconds: seconds, at: sourceLocation)
}

/// How long a wait for an event may take before the test is taken to hang (see `waitUntil`).
private let waitGuard: Double = 60

private struct WaitTimeout: Error, CustomStringConvertible {
    let seconds: Double
    let at: SourceLocation
    var description: String { "waited \(seconds) s at \(at.fileName):\(at.line) and it never happened" }
}

/// A harness whose first turn listed one todo item (in progress) and read Calculator's window.
private func menuHarness(tune: (inout Config) -> Void = { _ in }) async throws -> Harness {
    let h = try await Harness(tune: tune)
    await h.doll.ownerSaid("用電腦")
    try await h.turn(Scripted("", todoSet(), readCall))
    return h
}

/// What a successful action says: cua-driver cannot confirm it; the read after it shows.
private let sent = "（送出了，cua-driver 沒法確認有沒有作用；看下面這次讀到的視窗）"
/// The head line of a Calculator read.
private let calculatorHead = "Calculator 的視窗「計算機」；截圖 460×816"

@Test func eachComputerToolDecodesItsArgumentsAndRefusesBadOnes() throws {
    let cfg = try exampleConfig()
    let specs = Tools.specs(memory: cfg.memory, voice: false, computer: cfg.computer, files: cfg.files)
    func decode(_ name: String, _ params: [(String, String)]) throws -> Tools.Request { try Tools.decode(call(name, params), specs: specs) }
    func error(_ name: String, _ params: [(String, String)]) -> String? {
        do { _ = try decode(name, params); return nil } catch { return "\(error)" }
    }
    guard case .apps = try decode("apps", []) else { Issue.record("apps"); return }
    guard case .open("TextEdit", "~/notes.txt"?) = try decode("open", [("app", "TextEdit"), ("target", "~/notes.txt")]),
          case .open("Calculator", nil) = try decode("open", [("app", " Calculator "), ("target", "")]) else { Issue.record("open"); return }
    guard case .readWindow(nil, nil) = try decode("read_window", []),
          case .readWindow("TextEdit"?, "未命名"?) = try decode("read_window", [("app", "TextEdit"), ("window", "未命名")]) else {
        Issue.record("read_window"); return
    }
    func steps(_ json: String, expect: String? = nil) throws -> [Mac.Step] {
        guard case .act(let s, let e) = try decode("act", [("steps", json)] + (expect.map { [("expect", $0)] } ?? [])), e == expect else {
            Issue.record("not act: \(json)"); return []
        }
        return s
    }
    // Every kind, every target form, the defaults (count 1, button left, amount 3).
    #expect(try steps(#"[{"click": 5}]"#) == [.click(.row(5), button: "left", count: 1)])
    #expect(try steps(#"[{"click": {"name": "儲存", "role": "button"}, "count": 2, "button": "right"}]"#, expect: "x")
            == [.click(.named(name: "儲存", role: "button"), button: "right", count: 2)])
    #expect(try steps(#"[{"click": {"name": " 儲存 "}}]"#) == [.click(.named(name: "儲存", role: nil), button: "left", count: 1)])
    // A name is a string; a row is a number (the "7" button and row 7 are different targets).
    #expect(try steps(#"[{"click": " 7 "}, {"click": 7}]"#, expect: "x") == [.click(.named(name: "7", role: nil), button: "left", count: 1),
                                                                           .click(.row(7), button: "left", count: 1)])
    #expect(try steps(#"[{"type": "x", "in": "收件人"}]"#) == [.type(text: "x", into: .named(name: "收件人", role: nil))])
    #expect(try steps(#"[{"click": {"x": 10, "y": 20}}]"#) == [.click(.point(x: 10, y: 20), button: "left", count: 1)])
    #expect(try steps(#"[{"type": " a\n", "in": 3}]"#) == [.type(text: " a\n", into: .row(3))])
    #expect(try steps(#"[{"type": "x"}]"#) == [.type(text: "x", into: nil)])
    #expect(try steps(#"[{"type": "x", "in": {"name": "內文"}}]"#) == [.type(text: "x", into: .named(name: "內文", role: nil))])
    #expect(try steps(#"[{"key": "S", "modifiers": ["ctrl", "alt", "shift", "cmd"]}]"#) == [.key(key: "s", modifiers: ["cmd", "shift", "alt", "ctrl"])])
    #expect(try steps(#"[{"key": "return"}]"#) == [.key(key: "return", modifiers: [])])
    #expect(try steps(#"[{"scroll": "down"}]"#) == [.scroll(direction: "down", amount: 3, at: nil)])
    #expect(try steps(#"[{"scroll": "up", "amount": 5, "in": 2}]"#) == [.scroll(direction: "up", amount: 5, at: .row(2))])
    #expect(try steps(#"[{"menu": ["檔案", "新增"], "app": "TextEdit"}]"#) == [.menu(app: "TextEdit", path: ["檔案", "新增"])])
    #expect(try steps(#"[{"menu": ["檔案"]}]"#) == [.menu(app: nil, path: ["檔案"])])
    // Several steps in order, with what she expects at the end.
    #expect(try steps(#"[{"click": 2}, {"type": "78"}, {"key": "return"}]"#, expect: "78")
            == [.click(.row(2), button: "left", count: 1), .type(text: "78", into: nil), .key(key: "return", modifiers: [])])
    // The steps as a JSON array rather than its text.
    guard case .act(let given, nil) = try Tools.decode(ToolCall(name: "act", arguments: #"{"steps": [{"click": 1}]}"#), specs: specs),
          given == [.click(.row(1), button: "left", count: 1)] else { Issue.record("act as an array"); return }
    guard case .askOwner("付款", "Safari", "買書") = try decode("ask_owner", [("what", "付款"), ("where", "Safari"), ("why", "買書")]) else {
        Issue.record("ask_owner"); return
    }
    #expect(error("open", []) == "open 少了參數 app")
    #expect(error("open", [("app", " ")]) == "open 的 app 是空的")
    #expect(error("read_window", [("title", "x")]) == "read_window 沒有 title 這個參數（它的參數：app、window）")
    func actError(_ json: String, expect: String? = nil) -> String? {
        error("act", [("steps", json)] + (expect.map { [("expect", $0)] } ?? []))
    }
    #expect(actError(#"{"click": 1}"#) == #"act 的 steps 要是步驟的 JSON 陣列，例：[{"click": "儲存"}, {"type": "你好"}]；收到 {"click": 1}"#)
    #expect(actError("[]")?.hasPrefix("act 的 steps 要是步驟的 JSON 陣列") == true)
    #expect(actError(#"[5]"#) == #"act 的第 1 步要是一個物件，例：{"click": "儲存"}"#)
    #expect(actError(#"[{"press": 1}]"#) == "act 的第 1 步要剛好有 click、type、key、scroll、menu 其中一個，收到 press")
    #expect(actError(#"[{"click": 1}, {"click": 2, "type": "x"}]"#, expect: "x") == "act 的第 2 步要剛好有 click、type、key、scroll、menu 其中一個，收到 click、type")
    #expect(actError(#"[{"click": 1, "row": 1}]"#) == "act 的第 1 步（click）沒有 row 這一欄")
    #expect(actError(#"[{"click": 1, "count": 3}]"#) == "act 的第 1 步的 count 只能是 1 或 2")
    #expect(actError(#"[{"click": 1, "button": "middle"}]"#) == "act 的第 1 步的 button 只能是 left、right")
    #expect(actError(#"[{"click": " "}]"#)?.hasPrefix(#"act 的第 1 步的 click 要是名字（例："儲存"）"#) == true)
    #expect(actError(#"[{"click": {"x": 1}}]"#)?.hasPrefix(#"act 的第 1 步的 click 要是名字（例："儲存"）"#) == true)
    #expect(actError(#"[{"type": "x", "in": {"x": 1, "y": 2}}]"#) == "act 的第 1 步的 in 要是名字或行號，不能是 x、y")
    #expect(actError(#"[{"scroll": "sideways"}]"#) == "act 的第 1 步的 scroll 只能是 up、down、left、right")
    #expect(actError(#"[{"scroll": "up", "amount": 0}]"#) == "act 的第 1 步的 amount 要是 1 以上的整數")
    #expect(actError(#"[{"key": "cmd+s"}]"#)?.hasPrefix("act 的第 1 步的 key 只放一個鍵（例：\"return\"），修飾鍵放 modifiers") == true)
    #expect(actError(#"[{"key": "a", "modifiers": ["hyper"]}]"#) == "act 的第 1 步的 modifiers 只能是 cmd、shift、alt、ctrl，收到 hyper")
    #expect(actError(#"[{"type": ""}]"#) == "act 的第 1 步的 type 要是不空的字串")
    #expect(actError(#"[{"click": 1}, {"menu": "檔案"}]"#, expect: "x")?.hasPrefix("act 的第 2 步的 menu 要是選單路徑的字串陣列") == true)
    #expect(actError(#"[{"menu": []}]"#)?.hasPrefix("act 的第 1 步的 menu 要是選單路徑的字串陣列") == true)
    // Two steps or more need what she expects at the end; one step does not.
    #expect(actError(#"[{"click": 1}, {"click": 2}]"#)?.hasPrefix("act 有 2 步，要給 expect：") == true)
    #expect(actError(#"[{"click": 1}, {"click": 2}]"#, expect: " ")?.hasPrefix("act 有 2 步，要給 expect：") == true)
    #expect(actError(#"[{"click": 1}]"#) == nil)
    #expect(error("ask_owner", [("what", "付款"), ("where", "Safari")]) == "ask_owner 少了參數 why")
    #expect(error("ask_owner", [("what", "付款"), ("where", " "), ("why", "x")]) == "ask_owner 的 where 是空的")
    // The removed VM tools are unknown.
    for gone in ["screenshot", "drag", "watch"] { #expect(error(gone, []) == "沒有 \(gone) 這個工具") }
}

@Test func openReadsTheFrontWindowAsItsTreeAndScreenshotAndIsAStep() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("開計算機")
    try await h.turn(Scripted("", todoSet(), ("apps", []), ("open", [("app", "Calculator")])))
    let results = Array(try h.toolResults().suffix(2))
    #expect(results[0].text == "開著的 app（有視窗的列出視窗標題，前面的在上）：\n- Calculator：「計算機」\n- TextEdit（沒有視窗）")
    let r = results[1]
    #expect(r.text == "" && r.window == calculatorHead + "\n" + FakeCua.markdown(FakeCua.calculator))
    #expect(r.window!.contains("- [2] AXButton \"7\" [id=Seven actions=[press]]"))
    let shot = try String(decoding: Data(contentsOf: h.dir.appending(path: "shots/\(try #require(r.image))")), as: UTF8.self)
    #expect(shot == "shot s00000001 計算機")
    #expect(h.last.actions == ["todo set", "apps", "open"])
    #expect(await h.doll.itemSteps.map(\.line) == ["open Calculator"])
    // The read asked for both tree formats and a jpeg screenshot into her shots folder, with this session.
    let read = try #require(h.cua.args("get_window_state").first)
    let maxElements = try exampleConfig().computer.maxElements
    #expect(read["pid"] as? Int == 100 && read["window_id"] as? Int == 62541 && read["tree_format"] as? String == "both"
            && read["screenshot_format"] as? String == "jpeg" && read["max_elements"] as? Int == maxElements
            && (read["screenshot_out_file"] as? String)?.hasPrefix(h.dir.appending(path: "shots").path) == true
            && read["session"] as? String == h.cua.args("launch_app").first?["session"] as? String)
    #expect(h.cua.args("launch_app").first?["bundle_id"] as? String == "com.apple.calculator")
}

@Test func openWithNoWindowSaysHowToGetOneAndMenuOnTheAppReadsTheNewWindow() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("開一份新文件")
    try await h.turn(Scripted("", todoSet(), ("open", [("app", "TextEdit")])))
    let none = try h.toolResults().last!
    #expect(none.text == #"TextEdit 開著，但沒有視窗（在背景開的 app 常常沒有開新視窗）。要有視窗：新文件用 act(steps: [{"menu": ["檔案", "新增"], "app": "TextEdit"}])"#
            + "（英文選單是 File › New，選單名字以 app 自己的為準）；要開某個檔案就 open 帶 target。")
    #expect(none.image == nil && none.window == nil && h.last.actions == ["todo set", "open"])
    // The tools fit to that window need one: read_window says so.
    try await h.turn(Scripted("", ("read_window", [("app", "TextEdit")])))
    #expect(try h.toolResults().last!.text == "失敗：TextEdit 現在沒有視窗。新文件用 menu 選「檔案 › 新增」；開檔用 open 帶 target。")
    try await h.turn(Scripted("", act(#"{"menu": ["檔案", "新增"], "app": "TextEdit"}"#)))
    let menu = try #require(h.cua.args("invoke_menu").first)
    #expect(menu["pid"] as? Int == 200 && menu["path"] as? [String] == ["檔案", "新增"] && menu["window_id"] == nil)
    let r = try h.toolResults().last!
    #expect(r.window?.hasPrefix("TextEdit 的視窗「未命名」；截圖 460×816\n- [0] AXWindow \"未命名\"") == true && r.image != nil)
    #expect(r.text == "1 步都做了：menu 檔案 › 新增。" && h.last.actions == ["act"])
    #expect(await h.doll.itemSteps.map(\.line) == ["open TextEdit", "menu 檔案 › 新增"])
    // The next action works on the new window.
    try await h.turn(Scripted("", act(#"{"type": "娃娃", "in": 1}"#)))
    let typed = try #require(h.cua.args("type_text").first)
    #expect(typed["pid"] as? Int == 200 && typed["window_id"] as? Int == 63000 && typed["text"] as? String == "娃娃"
            && (typed["element_token"] as? String)?.hasSuffix(":1") == true)
}

/// 2026-11-16 c-1: the menu item ran but the window was gone by the time it was read; she was told
/// "menu failed" and the next read without an app failed too. The action is reported as done.
@Test func aReadThatFailsAfterAnActionSaysTheActionWasDone() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("開一份新文件")
    try await h.turn(Scripted("", todoSet(), ("open", [("app", "TextEdit")])))
    h.cua.refuse("get_window_state", #"{"refusal":{"code":"window_id_not_found","message":"No window matches that target now."}}"#)
    try await h.turn(Scripted("", act(#"{"menu": ["檔案", "新增"], "app": "TextEdit"}"#)))
    let r = try h.toolResults().last!
    #expect(r.text.hasPrefix("1 步都做了：menu 檔案 › 新增。\n做了 1 步，可是之後讀視窗失敗：") && r.text.contains("不要因為這個再做一次")
            && r.text.contains("read_window(app: \"TextEdit\")") && r.window == nil)
    #expect(h.last.actions == ["act"] && h.cua.actions == ["invoke_menu"])
    // Read again by the app's name: the window is there.
    try await h.turn(Scripted("", ("read_window", [("app", "TextEdit")])))
    #expect(try h.toolResults().last!.window?.hasPrefix("TextEdit 的視窗「未命名」") == true)
    // The same for a click: sent once, reported done.
    h.cua.refuse("get_window_state", #"{"refusal":{"code":"window_id_not_found","message":"No window matches that target now."}}"#)
    try await h.turn(Scripted("", clickRow(1)))
    let clicked = try h.toolResults().last!.text
    #expect(clicked.hasPrefix("1 步都做了：click AXTextArea") && clicked.contains("做了 1 步，可是之後讀視窗失敗：") && h.last.actions == ["act"])
    #expect(h.cua.actions == ["invoke_menu", "click"])
}

/// open with a file: the file's window is read even when another window of the app is in front; when
/// no window of that file shows up, the read says so instead of passing the front one off as it.
@Test func openWithAFileReadsThatFilesWindowOrSaysItIsNotThere() async throws {
    let h = try await Harness()
    let notes = h.dir.appending(path: "notes.txt")
    try FileManager.default.createDirectory(at: h.dir, withIntermediateDirectories: true)
    try Data("第一行\n".utf8).write(to: notes)
    h.cua.change { $0.windows.append(FakeCua.untitled) }
    await h.doll.ownerSaid("打開 notes.txt")
    try await h.turn(Scripted("", todoSet(), ("open", [("app", "TextEdit"), ("target", notes.path)])))
    let r = try h.toolResults().last!
    #expect(r.window?.hasPrefix("TextEdit 的視窗「notes.txt」（這個 app 有 2 個視窗") == true)
    #expect(h.cua.args("launch_app").last?["urls"] as? [String] == [notes.path])
    // The app shows nothing for the file: the front window comes with a line saying it is not the file.
    h.cua.change { s in s.opensFiles = false; s.windows.removeAll { $0.title == "notes.txt" } }
    try await h.turn(Scripted("", ("open", [("app", "TextEdit"), ("target", notes.path)])))
    let none = try h.toolResults().last!
    #expect(none.window?.hasPrefix("（沒看到標題有「notes.txt」的視窗：檔案可能沒有開起來（例如 app 有對話框擋著）。下面是 TextEdit 最前面的視窗，不是那個檔）\nTextEdit 的視窗「未命名」") == true)
}

@Test func aClickOnARowSendsItsElementTokenAndCarriesTheFreshRead() async throws {
    let h = try await menuHarness()
    try await h.turn(Scripted("", clickRow(2)))
    let click = try #require(h.cua.args("click").first)
    #expect(click["element_token"] as? String == "s00000001:2" && click["pid"] as? Int == 100 && click["window_id"] as? Int == 62541)
    let r = try h.toolResults().last!
    // The window read again after it: Calculator's display shows the 7.
    #expect(r.text == "1 步都做了：click AXButton \"7\"。" && r.window?.contains("- [1] AXStaticText \"7\"") == true && r.image != nil)
    // The window's list before, the step as a one-step run_steps, then the same window read again and
    // the check for a system authentication window.
    #expect(h.cua.tools.suffix(6) == ["list_windows", "run_steps", "click", "list_windows", "get_window_state", "list_windows"])
    #expect(await h.doll.itemSteps.map(\.line) == ["click AXButton \"7\""])
    // A double click, a right click and coordinates on the screenshot.
    try await h.turn(Scripted("", clickRow(3, count: 2)))
    try await h.turn(Scripted("", act(#"{"click": {"x": 10, "y": 20}, "button": "right"}"#)))
    #expect(h.cua.actions == ["click", "double_click", "click"])
    let xy = try #require(h.cua.args("click").last)
    #expect(xy["x"] as? Int == 10 && xy["y"] as? Int == 20 && xy["button"] as? String == "right" && xy["element_token"] == nil)
    #expect(await h.doll.itemSteps.map(\.line) == ["click AXButton \"7\"", "click AXButton \"8\" 雙擊", "click 10,20 right"])
}

@Test func aRowNotInTheLatestReadIsRefusedBeforeAnythingIsSent() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("按 7")
    // Nothing read yet.
    try await h.turn(Scripted("", todoSet(), clickRow(2)))
    #expect(try h.toolResults().last!.text == "失敗：沒有做：還沒讀過視窗（或上一次讀失敗了）；先 read_window 或 open。")
    try await h.turn(Scripted("", readCall))
    try await h.turn(Scripted("", clickRow(9)))
    let notThere = try h.toolResults().last!.text
    #expect(notThere == "失敗：沒有做：第 9 行不在最新一次讀到的清單裡（Calculator「計算機」，行號 0–5）。用名字指定，或重新 read_window 用新清單的行號。")
    // Off the screenshot: not clicked either.
    try await h.turn(Scripted("", act(#"{"click": {"x": 460, "y": 5}}"#)))
    #expect(try h.toolResults().last!.text == "失敗：沒有點：460,5 在截圖外（截圖 460×816）")
    #expect(h.cua.actions.isEmpty && h.last.actions == ["error act"])
}

@Test func aRowAnEarlierActionOfTheReplyChangedIsRefusedAndAnUnchangedOnePasses() async throws {
    let h = try await menuHarness()
    // Calculator's first press adds a C button before 8: row 3 is no longer the 8 she saw.
    h.cua.change {
        $0.after = { tool, args, s in
            FakeCua.press(tool, args, &s)
            if tool == "click", s.windows[0].elements[3].label == "8" {
                s.windows[0].elements.insert(FakeCua.Element(role: "AXButton", label: "C", id: "Clear"), at: 3)
            }
        }
    }
    try await h.turn(Scripted("", clickRow(2), clickRow(3), clickRow(5)))
    let results = Array(try h.toolResults().suffix(3))
    #expect(results[0].text == "1 步都做了：click AXButton \"7\"。" && results[0].image == nil)
    #expect(results[1].text == "失敗：沒有做：這則回覆前面的呼叫改變了視窗，第 3 行現在是 AXButton \"C\"，你看到的是 AXButton \"8\"。看新清單再選。")
    // The first action answered with its line only, so the refusal shows the window as it is now.
    #expect(results[1].image != nil && results[1].window?.contains("- [3] AXButton \"C\"") == true)
    #expect(results[2].text == "失敗：前面第 2 步（act）失敗了，這一步沒做")
    #expect(h.cua.actions == ["click"] && h.last.actions == ["act", "error act", "skipped act"])
    // The next reply means the new read: 7, 8 (now row 4), +, =: rows unchanged by the presses pass.
    try await h.turn(Scripted("", clickRow(2), clickRow(4), clickRow(5), clickRow(6)))
    #expect(h.cua.actions == ["click", "click", "click", "click", "click"])
    #expect(h.cua.args("click").suffix(4).map { ($0["element_token"] as? String)?.split(separator: ":").last.map(String.init) } == ["2", "4", "5", "6"])
    #expect(try h.toolResults().suffix(4).map(\.text) == ["click AXButton \"7\"", "click AXButton \"8\"", "click AXButton \"+\"", "click AXButton \"=\""].map { "1 步都做了：\($0)。" })
    #expect(try h.toolResults().last!.window?.contains("- [1] AXStaticText \"778\"") == true)
}

@Test func aDriverRefusalIsAFailedResultWithTheWindowReadAgainAndStopsTheChain() async throws {
    let h = try await menuHarness()
    let refusal = #"{"refusal":{"code":"stale_element_token","message":"element token s00000002:2 is from an old snapshot"},"hint":"call get_window_state again"}"#
    h.cua.refuse("click", refusal)
    try await h.turn(Scripted("", key("cmd+a"), clickRow(2), act(#"{"type": "copy"}"#), readCall))
    let results = Array(try h.toolResults().suffix(4))
    #expect(results.map(\.text) == ["1 步都做了：key cmd+a。",
                                    "第 1 步（click 第 2 行）沒做成：[stale_element_token] element token s00000002:2 is from an old snapshot call get_window_state again。下面是現在的視窗。",
                                    "失敗：前面第 2 步（act）失敗了，這一步沒做",
                                    ""])
    #expect(results.map { $0.image != nil } == [false, true, false, true])
    #expect(h.cua.actions == ["hotkey", "click"] && h.last.actions == ["act", "error act", "skipped act", "read_window"])
    // Only the done actions are steps.
    #expect(await h.doll.itemSteps.map(\.line) == ["key cmd+a"])
    // Bad arguments after a line-only step: the failure shows the window, since nothing of this reply did yet.
    try await h.turn(Scripted("", key("Return"), act(#"{"click": {"x": 99999, "y": 1}}"#), key("Escape")))
    let bad = Array(try h.toolResults().suffix(3))
    #expect(bad[0].text == "1 步都做了：key return。" && bad[0].image == nil)
    #expect(bad[1].text == "失敗：沒有點：99999,1 在截圖外（截圖 460×816）" && bad[1].image != nil && bad[1].window != nil)
    #expect(bad[2].text == "失敗：前面第 2 步（act）失敗了，這一步沒做" && bad[2].image == nil)
    #expect(h.cua.actions == ["hotkey", "click", "press_key"])
    // todo.done after actions in the same reply still ends the item; the action before it is the last.
    // Her line to him comes first: the list is his.
    try await h.turn(Scripted("", key("cmd+s"), dm("存好了"), done("存好了"), key("cmd+q")))
    let end = Array(try h.toolResults().suffix(4))
    #expect(end[0].image != nil && end[0].text == "1 步都做了：key cmd+s。" && end[1].text == Doll.shotSent(end[0].image!))
    #expect(end[2].text.hasPrefix("完成，記下了。")
            && end[3].text == "失敗：這則回覆前面已經用 todo.done 收起這一項了，後面的呼叫沒有執行；看了結果下一回合再做。")
    #expect(h.last.itemEnd == "完成" && h.last.experience?.hasSuffix("步驟：\n1. key cmd+a\n2. key return\n3. key cmd+s") == true)
    #expect(await h.doll.todo.phase != .doing && h.cua.actions.count == 4)
}

@Test func onlyTheLastActionOfAReplyCarriesTheRead() async throws {
    let h = try await menuHarness()
    // Three actions in one reply: two line-only results, the read at the end, three steps.
    try await h.turn(Scripted("", clickRow(2), act(#"{"type": "sw_vers\n"}"#), key("cmd+s")))
    var results = Array(try h.toolResults().suffix(3))
    #expect(results.map(\.text) == ["1 步都做了：click AXButton \"7\"。", "1 步都做了：type「sw_vers\n」。", "1 步都做了：key cmd+s。"])
    #expect(results.map { $0.image != nil } == [false, false, true] && results.map { $0.window != nil } == [false, false, true])
    #expect(h.cua.actions == ["click", "type_text", "hotkey"] && h.last.actions == ["act", "act", "act"])
    #expect(h.cua.args("hotkey").first?["keys"] as? [String] == ["cmd", "s"])
    #expect(await h.doll.itemSteps.map(\.line) == ["click AXButton \"7\"", "type「sw_vers\n」", "key cmd+s"])
    // Every other tool between them keeps its result; the last action is the last one written, wherever it is.
    try await h.turn(Scripted("", act(#"{"scroll": "down", "amount": 5}"#), readCall, act(#"{"click": {"x": 100, "y": 200}}"#), think("medium")))
    results = Array(try h.toolResults().suffix(4))
    #expect(results.map(\.text) == ["1 步都做了：scroll down 5。", "", "1 步都做了：click 100,200。", "下一回合 effort：medium"])
    #expect(results.map { $0.image != nil } == [false, true, true, false])
    #expect(h.last.actions == ["act", "read_window", "act", "think medium"])
    // A single action: its line and the read.
    try await h.turn(Scripted("", key("Return")))
    let one = try h.toolResults().last!
    #expect(one.text == "1 步都做了：key return。" && one.image != nil && one.window != nil && h.cua.args("press_key").last?["key"] as? String == "return")
    // Bad arguments alone: a text result, no read.
    try await h.turn(Scripted("", act(#"{"click": {"x": 1}}"#), act(#"{"click": 1, "x": 1}"#), act(#"{"scroll": "sideways"}"#)))
    let badTexts = try h.toolResults().suffix(3).map(\.text)
    #expect(badTexts[0].hasPrefix(#"失敗：act 的第 1 步的 click 要是名字（例："儲存"）"#)
            && badTexts.dropFirst() == ["失敗：前面第 1 步（act）失敗了，這一步沒做", "失敗：前面第 1 步（act）失敗了，這一步沒做"])
    #expect(try h.toolResults().suffix(3).allSatisfy { $0.image == nil })
    #expect(h.last.actions == ["error act", "skipped act", "skipped act"] && h.cua.actions.count == 6)
}

@Test func olderReadsArePutAwayInBatchesAndTheNewestAlwaysStays() {
    func kept(_ n: Int, _ batch: Int) -> [Int64] { Doll.fullReads((1...Int64(n)).map { $0 }, batch: batch).sorted() }
    #expect(Doll.fullReads([], batch: 4).isEmpty)
    #expect((1...4).map { kept($0, 4) } == [[1], [1, 2], [1, 2, 3], [1, 2, 3, 4]])
    // The fifth read puts the first four away at once; the next four are appended.
    #expect((5...9).map { kept($0, 4) } == [[5], [5, 6], [5, 6, 7], [5, 6, 7, 8], [9]])
    // A batch of 1 is the old rule: only the newest read.
    #expect((1...3).map { kept($0, 1) } == [[1], [2], [3]])
}

@Test func requestsStayAppendOnlyBetweenBatchesAndOlderReadsAreOneLine() async throws {
    // The server resumes only from the end of an earlier request (experiments/m7-mac/cacheprobe): every
    // request must start with the request before it, byte for byte, except once per `read_batch` reads,
    // where it may differ only from the first read put away.
    let h = try await menuHarness { $0.computer.readBatch = 3 }
    for _ in 0..<4 { try await h.turn(Scripted("", clickRow(2))) }
    try await h.turn("看到了")
    let reqs = await h.model.requests.map(\.messages)
    let isRead = { (m: ChatMessage) in m.role == "tool" && m.extra.contains { if case .text(let t) = $0 { t.hasPrefix(calculatorHead) || t.hasPrefix("（較早讀到的") } else { false } } }
    let reads = { (msgs: [ChatMessage]) in msgs.filter(isRead) }
    // Request k holds reads 1…k: in full up to 3, then the first three are one line each.
    #expect(reqs.count == 6 && reqs.map { reads($0).count } == [0, 1, 2, 3, 4, 5])
    #expect(reqs.map { reads($0).map(\.images.count) } == [[], [1], [1, 1], [1, 1, 1], [0, 0, 0, 1], [0, 0, 0, 1, 1]])
    let bytes = { (msgs: [ChatMessage]) in try msgs.map { try ChatMessage.encoder.encode($0) } }
    for k in 0..<5 {
        let (a, b) = (try bytes(reqs[k]), try bytes(reqs[k + 1]))
        if k == 3 {
            // The put-away: everything before the first read of the batch is the same bytes, that read is one line.
            let first = try #require(reqs[k].firstIndex(where: isRead))
            #expect(Array(b.prefix(first)) == Array(a.prefix(first)) && b[first] != a[first])
            #expect(reads(reqs[4])[0].extra == [.text("（較早讀到的：\(calculatorHead)；清單和截圖已收起）")])
            #expect(reads(reqs[4])[0].extra == [.text(Doll.readPutAway(try h.toolResults()[1].window))])
        } else {
            #expect(Array(b.prefix(a.count)) == a, "request \(k + 2) does not start with request \(k + 1)")
        }
    }
    let results = try h.toolResults()
    let uri = "data:image/jpeg;base64," + (try Data(contentsOf: h.dir.appending(path: "shots/\(results.last!.image!)"))).base64EncodedString()
    #expect(reads(reqs[5]).last!.extra == [.image(uri), .text(results.last!.window!)])
    // The files stay.
    #expect(try h.events().filter { $0.image != nil }.count == 5)
}

@Test func aSystemAuthenticationWindowOrAPasswordFieldIsCalledOut() async throws {
    let h = try await menuHarness()
    #expect(try h.toolResults().last!.text == "")
    h.cua.change { $0.windows.append(FakeCua.Window(id: 7, pid: 999, app: "SecurityAgent", title: "驗證", elements: [])) }
    try await h.turn(Scripted("", clickRow(2)))
    let authText = try h.toolResults().last!.text
    #expect(authText == "1 步都做了：click AXButton \"7\"。\n【系統驗證】畫面上有系統的驗證視窗（SecurityAgent「驗證」：Touch ID 或密碼）。停下來，不要試著通過它；用 ask_owner 跟主人說是哪一步叫出它的。")
    // Off screen it is not there.
    h.cua.change { $0.windows[1].onScreen = false; $0.windows[0].elements.append(FakeCua.Element(role: "AXSecureTextField", label: "密碼")) }
    try await h.turn(Scripted("", readCall))
    let r = try h.toolResults().last!
    #expect(r.text == "" && r.window?.hasSuffix("\n【注意】這個視窗有密碼欄位。密碼不要自己打：停下來，用 ask_owner 跟主人說要做什麼、在哪裡、為什麼。") == true)
    // The real administrator prompt (System Settings › 加入使用者⋯, 2026-11-17): a sheet in the app's own
    // window with a secure field under it, owned by the app, not by an authentication agent.
    h.cua.change { s in
        let sheet = s.windows[0].elements.count
        s.windows[0].elements += [FakeCua.Element(role: "AXSheet", label: nil),
                                  FakeCua.Element(role: "AXTextField", label: "密碼", parent: sheet, subrole: "AXSecureTextField")]
    }
    try await h.turn(Scripted("", clickRow(2)))
    let line = try h.toolResults().last!.text
    #expect(line.hasPrefix("1 步都做了：click AXButton \"7\"。\n【系統驗證】") && line.hasSuffix("的視窗跳出要密碼的驗證（Touch ID 或密碼）。停下來，不要試著通過它；用 ask_owner 跟主人說是哪一步叫出它的。"))
}

@Test func everyActionIsOneLineOfTheActionLogWithTheElementsIdentity() async throws {
    let h = try await menuHarness()
    try await h.turn(Scripted("", clickRow(2), key("cmd+s")))
    h.cua.refuse("scroll", #"{"refusal":{"code":"not_scrollable","message":"nothing to scroll"}}"#)
    try await h.turn(Scripted("", act(#"{"scroll": "down", "in": 1}"#)))
    let lines = try String(contentsOf: h.dir.appending(path: "computer-actions.jsonl"), encoding: .utf8).split(separator: "\n")
    let records = try lines.map { try JSONDecoder().decode(ActionRecord.self, from: Data($0.utf8)) }
    #expect(records.map(\.tool) == ["click", "key", "scroll"])
    #expect(records.allSatisfy { $0.item == "畫面上看得到結果" && $0.app == "Calculator" && $0.bundleID == "com.apple.calculator" && $0.window == "計算機" })
    #expect(records[0].element == ElementIdentity(row: 2, role: "AXButton", subrole: nil, axID: "Seven", label: "7", title: nil, path: ["AXWindow \"計算機\""]))
    #expect(records[0].args == [:] && records[0].ok && records[0].error == nil)
    #expect(records[1].element == nil && records[1].args == ["key": "s", "modifiers": "cmd"])
    #expect(records[2].element?.label == "7" && records[2].args == ["direction": "down", "amount": "3"])
    #expect(!records[2].ok && records[2].error == "[not_scrollable] nothing to scroll")
}

// act: a sequence of steps in one call (2026-11-17) ---------------------------------

/// The window reads with a screenshot (her reads, not the between-step checks).
private func screenshotReads(_ h: Harness) -> Int { h.cua.args("get_window_state").filter { $0["screenshot_out_file"] != nil }.count }
/// The action log's records.
private func actionRecords(_ h: Harness) throws -> [ActionRecord] {
    try String(contentsOf: h.dir.appending(path: "computer-actions.jsonl"), encoding: .utf8).split(separator: "\n")
        .map { try JSONDecoder().decode(ActionRecord.self, from: Data($0.utf8)) }
}
/// Clicks on the rows, in order, as one `act`.
private func clickRows(_ rows: Int..., expect: String) -> (String, [(String, String)]) {
    ("act", [("steps", "[" + rows.map { #"{"click": \#($0)}"# }.joined(separator: ", ") + "]"), ("expect", expect)])
}

@Test func aSequenceRunsInOrderReadsOnceAtTheEndAndIsOneRound() async throws {
    let h = try await menuHarness()
    let reads = screenshotReads(h)
    let calls = h.cua.tools.count
    let rounds = await h.doll.todo.totalRounds
    // 7, 8, +, 8, =, 7: the digits show on the display.
    try await h.turn(Scripted("", clickRows(2, 3, 4, 3, 5, 2, expect: "7887")))
    let tools = Array(h.cua.tools.dropFirst(calls))
    #expect(tools.first == "list_windows" && tools.filter { $0 == "run_steps" }.count == 6)
    #expect(h.cua.args("click").map { ($0["element_token"] as? String)?.split(separator: ":").last.map(String.init) } == ["2", "3", "4", "3", "5", "2"])
    // Each step its own one-step run_steps.
    #expect(h.cua.args("run_steps").allSatisfy { ($0["steps"] as? [[String: Any]])?.count == 1 && $0["session"] != nil })
    #expect(screenshotReads(h) == reads + 1)
    let r = try h.toolResults().last!
    #expect(r.text == "6 步都做了：click AXButton \"7\"、click AXButton \"8\"、click AXButton \"+\"、click AXButton \"8\"、click AXButton \"=\"、click AXButton \"7\"。"
            + "\n跟你預期的一樣：視窗裡有「7887」。")
    #expect(r.window?.contains("- [1] AXStaticText \"7887\"") == true && r.image != nil)
    #expect(h.last.actions == ["act"])
    #expect(await h.doll.itemSteps.count == 6)
    // One reply, one round toward the decision point.
    #expect(await h.doll.todo.totalRounds == rounds + 1)
    // todo.done's evidence is matched against the read after the sequence.
    try await h.turn(Scripted("", dm("是 7887"), done("是 7887", evidence: "7887")))
    #expect(try h.toolResults().last!.text.hasPrefix("完成，記下了。"))
}

@Test func aSequenceWhoseEndIsNotWhatSheExpectedSaysWhatIsThereAndStopsTheReply() async throws {
    let h = try await menuHarness()
    try await h.turn(Scripted("", clickRows(2, 3, expect: "87"), clickRow(4)))
    let results = Array(try h.toolResults().suffix(2))
    #expect(results[0].text.hasPrefix("2 步都做了：click AXButton \"7\"、click AXButton \"8\"。\n跟你預期的不一樣：你預期「87」，讀到的視窗裡沒有。現在是：\n"))
    #expect(results[0].text.contains("78") && results[0].image != nil)
    #expect(results[1].text == "失敗：前面第 1 步（act）失敗了，這一步沒做")
    #expect(h.last.actions == ["unexpected act", "skipped act"] && h.cua.actions == ["click", "click"])
}

@Test func aFailedStepStopsTheSequenceAndTheDoneStepsStay() async throws {
    let h = try await menuHarness()
    let reads = screenshotReads(h)
    h.cua.refuse("press_key", #"{"refusal":{"code":"no_focus","message":"nothing has focus"}}"#)
    try await h.turn(Scripted("", act(#"{"click": 2}"#, #"{"click": 3}"#, keyStep("Return"), #"{"click": 2}"#, expect: "78")))
    let r = try h.toolResults().last!
    #expect(r.text == "第 3 步（key return）沒做成：[no_focus] nothing has focus。前面 2 步做了，後面 1 步沒做，下面是現在的視窗。")
    #expect(r.window?.contains("- [1] AXStaticText \"78\"") == true && screenshotReads(h) == reads + 1)
    #expect(h.cua.actions == ["click", "click", "press_key"] && h.last.actions == ["error act"])
    #expect(await h.doll.itemSteps.map(\.line) == ["click AXButton \"7\"", "click AXButton \"8\""])
    // The action log: one record per step sent, one sequence, the failed one not ok.
    let records = try actionRecords(h)
    #expect(records.map(\.tool) == ["click", "click", "key"] && records.map(\.ok) == [true, true, false])
    #expect(Set(records.map(\.sequence)).count == 1 && records[0].sequence != nil)
    #expect(records.map { $0.element?.label } == ["7", "8", nil] && records[2].error == "[no_focus] nothing has focus")
    // The done steps are the experience's.
    try await h.turn(Scripted("", dm("按了 78"), done("按了 78")))
    #expect(h.last.experience?.hasSuffix("步驟：\n1. click AXButton \"7\"\n2. click AXButton \"8\"") == true)
}

@Test func aRowAReadOfTheSameReplyChangedIsRefusedAndAnUnchangedOnePasses() async throws {
    let h = try await menuHarness()
    // The window changed after the read her reply was written against: a C before the 8.
    h.cua.change { $0.windows[0].elements.insert(FakeCua.Element(role: "AXButton", label: "C", id: "Clear"), at: 3) }
    try await h.turn(Scripted("", readCall, clickRows(3, 2, expect: "87")))
    #expect(try h.toolResults().last!.text == "失敗：沒有做：這則回覆前面的呼叫改變了視窗，第 3 行現在是 AXButton \"C\"，你看到的是 AXButton \"8\"。看新清單再選。")
    #expect(h.cua.actions.isEmpty && h.last.actions == ["read_window", "error act"])
    // The row she saw is still the element: it passes.
    try await h.turn(Scripted("", readCall, clickRows(2, 4, expect: "78")))
    #expect(h.cua.actions == ["click", "click"] && h.last.actions == ["read_window", "act"])
}

@Test func aNamedStepFindsAnElementAnEarlierStepOpenedAndTheEndReadIsTheNewWindow() async throws {
    let h = try await menuHarness()
    // Pressing 7 opens a dialog in front with a 好 button that was in no read.
    h.cua.change {
        $0.after = { tool, args, s in
            FakeCua.press(tool, args, &s)
            if tool == "click", args["element_token"] as? String == "s00000001:2" {
                s.windows.insert(FakeCua.Window(id: 70000, pid: 100, app: "計算機", title: "關於", elements: [
                    FakeCua.Element(role: "AXWindow", label: "關於", parent: nil), FakeCua.Element(role: "AXButton", label: "好")], z: 1), at: 0)
            }
        }
    }
    try await h.turn(Scripted("", act(#"{"click": "7"}"#, #"{"click": {"name": "好", "role": "button"}}"#, expect: "好")))
    // Not in her read nor in the read between the steps: the app's front window, read again, by its token.
    let named = try #require(h.cua.args("click").last)
    #expect(named["window_id"] as? Int == 70000 && named["pid"] as? Int == 100 && named["name"] == nil
            && (named["element_token"] as? String)?.hasSuffix(":1") == true)
    let r = try h.toolResults().last!
    #expect(r.text == "2 步都做了：click AXButton \"7\"、click AXButton \"好\"。\n跟你預期的一樣：視窗裡有「好」。")
    #expect(r.window?.contains("的視窗「關於」") == true && r.window?.contains("- [1] AXButton \"好\"") == true)
    // The log keeps the element as that read has it.
    let records = try actionRecords(h)
    #expect(records.count == 2 && records[0].sequence == records[1].sequence)
    #expect(records[1].element?.row == 1 && records[1].element?.label == "好" && records[1].element?.path == [#"AXWindow "關於""#]
            && records[1].args == ["name": "好", "role": "button"])
    // A name her latest read does not have, as a first step: refused with nothing sent.
    try await h.turn(Scripted("", act(#"{"click": "取消"}"#)))
    #expect(try h.toolResults().last!.text == "失敗：沒有做：最新讀到的清單（Calculator「關於」）裡沒有名字叫「取消」的元素。名字要跟清單上引號或括號裡的字一字不差；沒有名字的元素用行號。")
    #expect(h.last.actions == ["error act"] && h.cua.actions == ["click", "click"])
}

@Test func aNameIsResolvedInHerLatestReadAndSentAsThatElementsToken() async throws {
    let h = try await menuHarness()
    // By label, by AX id in any case, by label and role; each the token of the latest read of the window.
    try await h.turn(Scripted("", act(#"{"click": "7"}"#, #"{"click": "eight"}"#, #"{"click": {"name": "=", "role": "AXButton"}}"#, expect: "78")))
    #expect(try h.toolResults().last!.text.hasPrefix("3 步都做了：click AXButton \"7\"、click AXButton \"8\"、click AXButton \"=\"。\n跟你預期的一樣"))
    let clicks = h.cua.args("click")
    #expect(clicks.map { $0["element_token"] as? String } == ["s00000001:2", "s00000002:3", "s00000003:5"])
    #expect(clicks.allSatisfy { $0["window_id"] as? Int == 62541 && $0["name"] == nil })
    let records = try actionRecords(h)
    #expect(records.map { $0.element?.axID } == ["Seven", "Eight", "Equals"] && records[1].args == ["name": "eight"])
    #expect(records.allSatisfy { $0.element?.path == [#"AXWindow "計算機""#] })
}

@Test func anAmbiguousNameIsRefusedWithItsCandidatesAndARoleSettlesIt() async throws {
    let h = try await menuHarness()
    h.cua.change { $0.windows[0].elements.append(FakeCua.Element(role: "AXMenuItem", label: "7")) }
    try await h.turn(Scripted("", readCall))
    try await h.turn(Scripted("", act(#"{"click": "7"}"#)))
    #expect(try h.toolResults().last!.text == #"失敗：沒有做：名字叫「7」的有 2 個：[2] AXButton "7"、[6] AXMenuItem "7"。加 role（例：{"name": "7", "role": "button"}），或用行號。"#)
    #expect(h.cua.actions.isEmpty)
    try await h.turn(Scripted("", act(#"{"click": {"name": "7", "role": "button"}}"#)))
    #expect(h.cua.args("click").map { $0["element_token"] as? String } == ["s00000002:2"])
    #expect(try h.toolResults().last!.window?.contains(#"AXStaticText "7""#) == true)
}

@Test func aNameThatMatchesNothingIsRefusedWithTheNearOnes() async throws {
    let h = try await menuHarness { $0.computer.windowWaitS = 0 }
    // In her read, as the first step: the near names; never a part of a name.
    try await h.turn(Scripted("", act(#"{"click": "78"}"#)))
    #expect(try h.toolResults().last!.text
            == #"失敗：沒有做：最新讀到的清單（Calculator「計算機」）裡沒有名字叫「78」的元素；像的有：[2] AXButton "7"、[3] AXButton "8"。名字要跟清單上引號或括號裡的字一字不差；沒有名字的元素用行號。"#)
    #expect(h.cua.actions.isEmpty)
    // After an earlier step: the front window is read again (as long as `open` waits), then refused.
    try await h.turn(Scripted("", act(#"{"click": "7"}"#, #"{"click": "取消"}"#, expect: "7")))
    #expect(try h.toolResults().last!.text.hasPrefix("第 2 步（click \"取消\"）沒做成：沒有做：Calculator最前面的視窗「計算機」裡沒有名字叫「取消」的元素（等了 0 秒）。名字要跟清單上引號或括號裡的字一字不差。前面 1 步做了，下面是現在的視窗。"))
    #expect(h.cua.actions == ["click"])
}

@Test func aRowWrittenAsTheNumberAnotherElementIsNamedIsRefused() async throws {
    let h = try await menuHarness()
    h.cua.change { $0.windows[0].elements.append(FakeCua.Element(role: "AXButton", label: "2", id: "Two")) }
    try await h.turn(Scripted("", readCall))
    // m7-mac rounds (2026-11-17): rows written for the digits they were not.
    try await h.turn(Scripted("", clickRow(2)))
    #expect(try h.toolResults().last!.text == #"失敗：沒有做：2 是第 2 行（AXButton "7"），可是清單上名字叫「2」的是 [6] AXButton "2"。要按「2」寫 "2"（加引號是名字）；要第 2 行就寫它的名字。"#)
    #expect(h.cua.actions.isEmpty)
    // Rows still work where no other element has that number for a name.
    try await h.turn(Scripted("", clickRows(3, 6, expect: "82")))
    #expect(h.cua.args("click").map { $0["element_token"] as? String } == ["s00000002:3", "s00000003:6"])
}

@Test func anAuthenticationWindowOrAPasswordSheetBetweenStepsStopsTheSequence() async throws {
    let h = try await menuHarness()
    h.cua.change {
        $0.after = { tool, args, s in
            FakeCua.press(tool, args, &s)
            if tool == "click" { s.windows.append(FakeCua.Window(id: 7, pid: 999, app: "SecurityAgent", title: "驗證", elements: [])) }
        }
    }
    try await h.turn(Scripted("", clickRows(2, 3, expect: "78")))
    let r = try h.toolResults().last!
    #expect(r.text.hasPrefix("【系統驗證】畫面上有系統的驗證視窗（SecurityAgent「驗證」") && r.text.contains("做到第 1 步就停了，後面 1 步沒做。"))
    #expect(h.cua.actions == ["click"] && h.last.actions == ["error act"])
    // A password sheet in the app's own window.
    h.cua.change { s in
        s.windows.removeAll { $0.pid == 999 }
        s.after = { tool, args, s in
            FakeCua.press(tool, args, &s)
            guard tool == "click", !s.windows[0].elements.contains(where: { $0.role == "AXSheet" }) else { return }
            let sheet = s.windows[0].elements.count
            s.windows[0].elements += [FakeCua.Element(role: "AXSheet", label: nil),
                                      FakeCua.Element(role: "AXTextField", label: "密碼", parent: sheet, subrole: "AXSecureTextField")]
        }
    }
    try await h.turn(Scripted("", readCall, clickRows(2, 3, expect: "778")))
    let sheet = try h.toolResults().last!.text
    #expect(sheet.hasPrefix("【系統驗證】") && sheet.contains("的視窗跳出要密碼的驗證") && sheet.contains("做到第 1 步就停了，後面 1 步沒做。"))
    #expect(h.cua.actions == ["click", "click"])
}

@Test func moreStepsThanTheConfigAllowsOrSeveralWithoutExpectAreRefusedBeforeAnythingIsSent() async throws {
    let h = try await menuHarness { $0.computer.sequenceMaxSteps = 2 }
    let calls = h.cua.tools.count
    try await h.turn(Scripted("", clickRows(2, 3, 4, expect: "78")))
    #expect(try h.toolResults().last!.text == "失敗：沒有做：一次最多 2 步，你給了 3 步。分成幾次送，每次送到要看結果的地方為止。")
    #expect(h.cua.tools.count == calls && h.last.actions == ["error act"])
    try await h.turn(Scripted("", act(#"{"click": 2}"#, #"{"click": 3}"#)))
    #expect(try h.toolResults().last!.text.hasPrefix("失敗：act 有 2 步，要給 expect："))
    #expect(h.cua.tools.count == calls && h.last.actions == ["error act"])
}

@Test func sequenceMaxStepsIsReadFromTheConfigAndZeroIsRefused() throws {
    let example = try String(contentsOf: root.appending(path: "dollcore.example.toml"), encoding: .utf8)
    #expect(example.contains("\nsequence_max_steps = 20"))
    #expect(try exampleConfig().computer.sequenceMaxSteps == 20)
    let file = FileManager.default.temporaryDirectory.appending(path: "dollcore-seq-\(UUID().uuidString).toml")
    defer { try? FileManager.default.removeItem(at: file) }
    try example.replacingOccurrences(of: "\nsequence_max_steps = 20", with: "\nsequence_max_steps = 7").write(to: file, atomically: true, encoding: .utf8)
    #expect(try Config.load(file).computer.sequenceMaxSteps == 7)
    try example.replacingOccurrences(of: "\nsequence_max_steps = 20", with: "\nsequence_max_steps = 0").write(to: file, atomically: true, encoding: .utf8)
    #expect(throws: TOMLError.self) { try Config.load(file) }
}

@Test func aRowAfterTheCheckBetweenStepsGoesWithTheFreshTokenOnlyIfItIsTheSameElement() async throws {
    // 2026-11-17 n-1: the check between steps reads the window (a new snapshot); a later row sent with
    // the token of her older read was refused by the driver as stale.
    let h = try await Harness()
    await h.doll.ownerSaid("算 78")
    try await h.turn(Scripted("", todoSet(), ("open", [("app", "Calculator")])))
    try await h.turn(Scripted("", act(#"{"click": 2}"#, #"{"click": 3}"#, expect: "78")))
    let tokens = h.cua.args("click").map { $0["element_token"] as? String ?? "" }
    #expect(tokens.count == 2 && tokens[0].hasSuffix(":2") && tokens[1].hasSuffix(":3"))
    #expect(tokens.count == 2 && tokens[0].split(separator: ":").first != tokens[1].split(separator: ":").first)
    // The element under a later row changed after step 1: that step is refused, nothing more is sent.
    h.cua.change { $0.after = { tool, args, s in
        FakeCua.press(tool, args, &s)
        if tool == "click" { s.windows[0].elements[3].label = "9" }
    } }
    try await h.turn(Scripted("", act(#"{"click": 2}"#, #"{"click": 3}"#, expect: "787")))
    #expect(h.cua.args("click").count == 3)
    #expect(try h.toolResults().last!.text.contains("前面的步驟改變了視窗，第 3 行現在是 AXButton \"9\""))
}

@Test func herOneSessionStartsWithACursorThatNeverHidesIsOnEveryCallAndEndsAtExit() async throws {
    // 2026-11-17, owner: several agent cursors on screen at once; he wants one, hers, that stays.
    let h = try await Harness()
    await h.doll.ownerSaid("算 7+8")
    try await h.turn(Scripted("", todoSet(), ("open", [("app", "Calculator")]), clickRow(2)))
    func parsed(_ json: String) -> [String: Any] { (try? JSONSerialization.jsonObject(with: Data(json.utf8)) as? [String: Any]) ?? [:] }
    let sessions = h.cua.state.withLock { $0.sessions }
    #expect(sessions.count == 1 && sessions[0].tool == "start_session")
    let label = try #require(parsed(sessions[0].json)["session"] as? String)
    #expect((parsed(sessions[0].json)["cursor_motion"] as? [String: Any])?["idle_hide_ms"] as? Int == 0)
    // Every call carries the label (none falls to an implicit session of its own). The fake also
    // records each run_steps step's inner tool; those are arguments of the run_steps call, not calls.
    let inner: Set<String> = ["click", "double_click", "type_text", "press_key", "hotkey", "scroll"]
    let calls = h.cua.state.withLock { $0.calls }.filter { !inner.contains($0.tool) }
    #expect(!calls.isEmpty && calls.allSatisfy { c in
        (try? JSONSerialization.jsonObject(with: Data(c.json.utf8)) as? [String: Any])?["session"] as? String == label })
    await h.doll.endComputer()
    await h.doll.endComputer()
    let after = h.cua.state.withLock { $0.sessions }
    #expect(after.count == 2 && after[1].tool == "end_session" && parsed(after[1].json)["session"] as? String == label)
    // The config key: 0 by default, read from the file, outside 0...60000 refused.
    let example = try String(contentsOf: root.appending(path: "dollcore.example.toml"), encoding: .utf8)
    #expect(try exampleConfig().computer.cursorIdleHideMs == 0)
    let file = FileManager.default.temporaryDirectory.appending(path: "dollcore-cursor-\(UUID().uuidString).toml")
    defer { try? FileManager.default.removeItem(at: file) }
    try example.replacingOccurrences(of: "\ncursor_idle_hide_ms = 0", with: "\ncursor_idle_hide_ms = 60001").write(to: file, atomically: true, encoding: .utf8)
    #expect(throws: TOMLError.self) { try Config.load(file) }
}

@Test func aMenuThatOpensTheFirstWindowAndTypingIntoItRunInOneActButARowNeedsARead() async throws {
    // TextEdit with no window: the menu opens 未命名, typing goes to its focus without a read of it.
    let h = try await Harness()
    await h.doll.ownerSaid("開一份新文件寫娃娃")
    try await h.turn(Scripted("", todoSet(), ("open", [("app", "TextEdit")])))
    try await h.turn(Scripted("", act(#"{"menu": ["檔案", "新增"], "app": "TextEdit"}"#, #"{"type": "娃娃"}"#, expect: "未命名")))
    let typed = try #require(h.cua.args("type_text").first)
    #expect(typed["pid"] as? Int == 200 && typed["window_id"] == nil && typed["text"] as? String == "娃娃")
    #expect(h.cua.actions == ["invoke_menu", "type_text"] && h.last.actions == ["act"])
    let r = try h.toolResults().last!
    #expect(r.text == "2 步都做了：menu 檔案 › 新增、type「娃娃」。\n跟你預期的一樣：視窗裡有「未命名」。")
    #expect(r.window?.hasPrefix("TextEdit 的視窗「未命名」") == true && r.image != nil)
    // A row of an app whose window was never read: refused, the steps before it stay done.
    let b = try await menuHarness()
    try await b.turn(Scripted("", act(#"{"menu": ["檔案", "新增"], "app": "TextEdit"}"#, #"{"click": 1}"#, expect: "未命名")))
    let row = try b.toolResults().last!
    #expect(row.text.hasPrefix("第 2 步（click 第 1 行）沒做成：沒有做：第 1 行要對讀過的視窗，TextEdit 還沒讀過視窗。")
            && row.text.contains("前面 1 步做了，下面是現在的視窗。"))
    #expect(b.cua.actions == ["invoke_menu"] && b.last.actions == ["error act"] && row.window?.hasPrefix("TextEdit 的視窗「未命名」") == true)
}

@Test func theSameFailingComputerCallTheSecondTimeInARowSaysSo() async throws {
    let h = try await menuHarness()
    let refusal = #"{"refusal":{"code":"element_disabled","message":"button is disabled"}}"#
    h.cua.refuse("click", refusal)
    try await h.turn(Scripted("", clickRow(5)))
    let first = try h.toolResults().last!.text
    #expect(first == "第 1 步（click 第 5 行）沒做成：[element_disabled] button is disabled。下面是現在的視窗。")
    h.cua.refuse("click", refusal)
    try await h.turn(Scripted("", clickRow(5)))
    let again = try h.toolResults().last!.text
    #expect(again.hasPrefix(first + "\n【同一步又失敗】一模一樣的 act 已經失敗 2 次了，再做一次也會一樣。"))
    #expect(h.logs.contains { $0.contains("act failed 2 times in a row with the same arguments") })
    // A success starts the count over.
    try await h.turn(Scripted("", clickRow(2)))
    h.cua.refuse("click", refusal)
    try await h.turn(Scripted("", clickRow(5)))
    #expect(!(try h.toolResults().last!.text.contains("【同一步又失敗】")))
}

@Test func askOwnerDMsHimTheItemWaitsAndHisLineResumesIt() async throws {
    let h = try await menuHarness()
    try await h.turn(Scripted("", ("ask_owner", [("what", "付款買書"), ("where", "Safari 的博客來結帳頁"), ("why", "要花錢")])))
    #expect(h.said.last?.hasSuffix("想先問你】\n要做：付款買書\n在哪裡：Safari 的博客來結帳頁\n為什麼：要花錢") == true && h.saidTo.last == .owner)
    #expect(try h.toolResults().last!.text == "問了主人（私訊）。這則回覆的呼叫做完以後，「畫面上看得到結果」就是等待中：他回了才會再有回合。")
    #expect(h.last.actions == ["ask owner"])
    #expect(await h.doll.todo.phase == .waiting)
    #expect(await !h.doll.followUp)
    await h.doll.ownerSaid("買吧")
    try await h.turn(Scripted("", clickRow(2)))
    #expect(await h.doll.todo.phase == .doing && h.last.actions == ["act"])
    // Discord down: not asked, the item goes on.
    h.rec.undelivered.withLock { $0 = "Discord 斷線" }
    try await h.turn(Scripted("", ("ask_owner", [("what", "刪檔"), ("where", "Finder"), ("why", "清空間")])))
    #expect(try h.toolResults().last!.text == "失敗：主人沒收到：Discord 斷線。Discord 連上之前問不了；先不要做那件事。")
    #expect(h.last.actions == ["error ask_owner"])
    #expect(await h.doll.todo.phase == .doing)
}

@Test func theDaemonCheckNamesTheFixWhenTheDriverIsMissing() async throws {
    let path = FileManager.default.temporaryDirectory.appending(path: "no-cua-\(UUID().uuidString)/cua-driver").path
    do {
        _ = try await CuaDaemon.ensure(driver: path, startWait: 1)
        Issue.record("a missing driver came up")
    } catch let e as ComputerError {
        #expect(e.description.hasPrefix("沒有 cua-driver：\(path) 不存在或不能執行。修法：安裝 CuaDriver.app")
                && e.description.contains("[computer] driver"))
    }
}

/// A stand-in `cua-driver` executable in a fresh folder: `permissions status --json` prints
/// `perm.json`; `status` succeeds while the file `up` exists; `serve` creates `up` and runs until
/// terminated (or exits 3 at once when `servefail` exists); `stop` removes `up`. Every call's
/// arguments are appended to `calls`.
private struct FakeDriverCLI {
    let dir: URL
    var path: String { dir.appending(path: "cua-driver").path }

    init(permissions: String, running: Bool = false) throws {
        dir = FileManager.default.temporaryDirectory.appending(path: "fake-cua-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let script = """
            #!/bin/sh
            d=$(dirname "$0")
            echo "$*" >> "$d/calls"
            case "$1" in
            permissions) cat "$d/perm.json" ;;
            status) [ -f "$d/up" ] ;;
            serve)
                [ -f "$d/servefail" ] && exit 3
                touch "$d/up"
                trap 'rm -f "$d/up"; exit 0' TERM
                while :; do sleep 0.05; done ;;
            stop) rm -f "$d/up" ;;
            esac
            """
        try script.write(toFile: path, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: path)
        try permissions.write(to: dir.appending(path: "perm.json"), atomically: true, encoding: .utf8)
        if running { FileManager.default.createFile(atPath: dir.appending(path: "up").path, contents: nil) }
    }

    /// The calls so far, one line of arguments each.
    var calls: [String] {
        ((try? String(contentsOf: dir.appending(path: "calls"), encoding: .utf8)) ?? "").split(separator: "\n").map(String.init)
    }
    var up: Bool { FileManager.default.fileExists(atPath: dir.appending(path: "up").path) }
}

private let allowed = #"{"accessibility": true, "screen_recording": true}"#

@Test func theDaemonCheckNamesTheMissingPermissionAndStartsNothing() async throws {
    for (perm, missing) in [(#"{"accessibility": true, "screen_recording": false}"#, "螢幕錄製（Screen Recording）"),
                            (#"{"accessibility": false, "screen_recording": true}"#, "輔助使用（Accessibility）"),
                            (#"{"screen_recording": true}"#, "輔助使用（Accessibility）"),
                            (#"{"accessibility": false, "screen_recording": false}"#, "輔助使用（Accessibility）和螢幕錄製（Screen Recording）")] {
        let cli = try FakeDriverCLI(permissions: perm)
        do {
            _ = try await CuaDaemon.ensure(driver: cli.path, startWait: 2)
            Issue.record("came up without \(missing)")
        } catch let e as ComputerError {
            #expect(e.description == "CuaDriver 沒有\(missing)權限。修法：執行 `\(cli.path) permissions grant`，"
                    + "或到 系統設定 › 隱私權與安全性 打開 CuaDriver 的這兩項，然後重開 dollcore。")
        }
        #expect(cli.calls == ["permissions status --json"] && !cli.up)
    }
    // An answer that is not the JSON it should be: said, with what it was.
    let garbled = try FakeDriverCLI(permissions: "not json")
    do {
        _ = try await CuaDaemon.ensure(driver: garbled.path, startWait: 2)
        Issue.record("came up on a garbled permissions answer")
    } catch let e as ComputerError {
        #expect(e.description.hasPrefix("cua-driver permissions status 讀不懂（exit 0）：not json"))
    }
}

@Test func theDaemonIsStartedWhenDownStoppedAtTheEndAndAOneAlreadyRunningIsLeftAlone() async throws {
    let cli = try FakeDriverCLI(permissions: allowed)
    let daemon = try await CuaDaemon.ensure(driver: cli.path, startWait: 5)
    #expect(daemon.started && cli.up)
    // Checked, found down, started once, then asked until it answered (the child's own line may land between polls).
    #expect(Array(cli.calls.prefix(2)) == ["permissions status --json", "status"] && cli.calls.filter { $0 == "serve" }.count == 1
            && cli.calls.last == "status")
    await daemon.stop()
    #expect(cli.calls.last == "stop" && !cli.up)
    try await waitUntil { !(daemon.child?.isRunning ?? false) }
    #expect(daemon.child?.isRunning == false)
    // Already running (started by someone else): used, never started or stopped by this process.
    let other = try FakeDriverCLI(permissions: allowed, running: true)
    let found = try await CuaDaemon.ensure(driver: other.path, startWait: 5)
    #expect(!found.started && other.calls == ["permissions status --json", "status"])
    await found.stop()
    #expect(other.calls == ["permissions status --json", "status"] && other.up)
    // A daemon that exits at once: the boot stops with how to see why.
    let broken = try FakeDriverCLI(permissions: allowed)
    FileManager.default.createFile(atPath: broken.dir.appending(path: "servefail").path, contents: nil)
    do {
        _ = try await CuaDaemon.ensure(driver: broken.path, startWait: 5)
        Issue.record("a daemon that exited came up")
    } catch let e as ComputerError {
        #expect(e.description == "cua-driver serve 一啟動就結束了（exit 3）。修法：在終端機跑 `\(broken.path) serve` 看它說什麼。")
    }
}

@Test func failedComputerCallsInARowRaiseStressFromTheSecondAndASuccessResetsTheCount() async throws {
    let clock = ManualClock(at("2026-11-16", "10:00"))
    let h = try await goalsHarness(clock)
    await h.doll.testStartSignals()
    let stress = { await h.doll.dashboardDrives().changes.filter { $0.signal == .stress } }
    await h.doll.ownerSaid("按 7")
    try await h.turn(Scripted("", todoSet(), readCall))
    try await h.turn(Scripted("", clickRow(9)))
    #expect(await stress().isEmpty)
    // The second failure in a row: 壓力 up, with the call and its result as the cause.
    try await h.turn(Scripted("", clickRow(9)))
    let up = await stress()
    #expect(up.count == 1 && up[0].cause.hasPrefix("工具連續失敗：act：失敗：沒有做：第 9 行不在最新一次讀到的清單裡")
            && approx(up[0].after - up[0].before, DrivesConfig().toolErrorsStress))
    // A success starts over: the next single failure moves nothing.
    try await h.turn(Scripted("", clickRow(2)))
    try await h.turn(Scripted("", clickRow(9)))
    #expect(await stress().count == 1)
}

@Test func readWindowPicksAWindowByTitleAndOpenRefusesAFileThatIsNotThere() async throws {
    let h = try await menuHarness()
    h.cua.change { $0.windows.append(FakeCua.Window(id: 62542, pid: 100, app: "計算機", title: "紙帶", elements: [
        FakeCua.Element(role: "AXWindow", label: "紙帶", parent: nil)], z: -1)) }
    try await h.turn(Scripted("", ("read_window", [("app", "Calculator"), ("window", "紙")])))
    let r = try h.toolResults().last!
    #expect(r.window?.hasPrefix("Calculator 的視窗「紙帶」（這個 app 有 2 個視窗；讀別的用 read_window 帶 window 標題）；截圖 460×816\n") == true)
    #expect(h.cua.args("get_window_state").last?["window_id"] as? Int == 62542)
    // No such title: the app's windows, front first.
    try await h.turn(Scripted("", ("read_window", [("app", "Calculator"), ("window", "設定")])))
    #expect(try h.toolResults().last!.text == "失敗：Calculator 沒有標題含「設定」的視窗；它的視窗：「計算機」、「紙帶」")
    // A missing file is not handed to the app; an unknown app says how to find names.
    let nope = h.dir.appending(path: "nope.txt").path
    try await h.turn(Scripted("", ("open", [("app", "TextEdit"), ("target", nope)]), ("open", [("app", "Nope")])))
    #expect(try h.toolResults().suffix(2).map(\.text) == [
        "失敗：沒有開：\(nope) 不存在。要新檔案：先用 write_file 建一個（可以是空的），再 open 它。",
        "失敗：開不了 Nope：no app named Nope（找不到叫「Nope」的 app；用 apps 看開著的 app 名字，或給 bundle id）"])
    #expect(h.cua.args("launch_app").map { $0["name"] as? String } == ["Nope"])
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
    #expect(noIDs(owner.text).contains("〔確定｜own〕\n我的 VM 登入密碼是 fish123") && !owner.secret)
    // Her own turn (her watch woke her): own is still visible.
    await h.doll.put(Event(.watch, .internal, "盯著的整個畫面變了。"))
    try await h.turn(Scripted("", recall("VM 登入密碼")))
    #expect(Doll.audience(try h.events()) == .doll)
    #expect(try h.toolResults().last!.text.contains("fish123"))
    // A stranger's turn (no such input until M6; the projection is Memory's): never.
    var cfg = try exampleConfig().memory
    cfg.dir = h.dir.appending(path: "memory").path
    // A second process's view: the encrypted folders' index lives in each process's memory, built at its boot.
    let memory = try Memory(config: cfg, root: h.dir.appending(path: "memory"), store: h.store, embedder: FakeEmbedder(), box: try testBox(h.dir))
    try memory.syncOnBoot()
    let stranger = try memory.search("VM 登入密碼", visible: Memory.visible(.stranger, sawSecret: false), limit: 5)
    #expect(!stranger.contains { $0.folder == "own" } && stranger.contains { $0.text.contains("深色模式") })
    #expect(try memory.search("VM 登入密碼", visible: Memory.visible(.doll, sawSecret: false), limit: 5).contains { $0.folder == "own" })
}

/// Her public memory files, parsed, by text.
private func publicMemories(_ h: Harness) throws -> [(text: String, tier: SourceTier, confidence: Double, kind: String, app: String, version: String)] {
    let dir = h.dir.appending(path: "memory/public")
    return try FileManager.default.contentsOfDirectory(atPath: dir.path).filter { $0.hasSuffix(".md") }.map {
        let p = try Memory.parseFile(String(contentsOf: dir.appending(path: $0), encoding: .utf8))
        return (p.body, p.tier, p.confidence, p.kind, p.app, p.appVersion)
    }.sorted { $0.text < $1.text }
}

// auto-attached memories (spec 3.1, 2026-11-09) ----------------------------------------

/// Writes one record file into her memory folder and re-indexes, as if remembered just now. Default:
/// an experience (tier self_reported, 0.4 = 大概), the only kind the program attaches.
private func seedMemory(_ h: Harness, _ folder: String, _ text: String, tier: String = "self_reported",
                        confidence: Double = 0.4, entities: [MemoryEntity] = [], id: String? = nil) async throws {
    let dir = h.dir.appending(path: "memory/\(folder)")
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    let ents = entities.isEmpty ? "" : "entities = \(MemoryEntity.json(entities))\n"
    let file = "+++\nkind = \"\"\nconfidence = \(confidence)\ncreated_ts = \(Date().timeIntervalSince1970)\nsource_event_seq = 1\nsource_tier = \"\(tier)\"\n\(ents)+++\n\(text)"
    try memoryText(h.dir, folder, file).write(to: dir.appending(path: "\(id ?? "1-\(UUID().uuidString.prefix(6).lowercased())").md"), atomically: true, encoding: .utf8)
    try await h.doll.rebuildMemory()
}

/// A memory file's bytes as dollcore writes them: encrypted in `secret` and `own` (2026-11-21).
func memoryText(_ data: URL, _ folder: String, _ text: String) throws -> String {
    ["secret", "own"].contains(folder) ? try testBox(data).seal(text) + "\n" : text
}

/// A memory file parsed as dollcore reads it (decrypted when its folder is encrypted).
func readMemory(_ data: URL, _ url: URL) throws -> MemoryFile {
    let raw = try String(contentsOf: url, encoding: .utf8)
    return try Memory.parseFile(SecretBox.isSealed(raw) ? try testBox(data).open(raw, place: url.path) : raw)
}

/// The 【相關記憶】 events (not the program's 【提醒】 after a call-less reply to the owner, 2026-11-18).
private func memoryEvents(_ h: Harness) throws -> [Event] { try h.events().filter { $0.kind == .memory && !$0.text.hasPrefix("【提醒】") } }

@Test func anOwnerLineAttachesRelatedExperiencesWholeBeforeHerTurn() async throws {
    let h = try await Harness()
    try await seedMemory(h, "public", "經驗：幫我用 TextEdit 寫筆記\n期望：筆記存好\n1. key cmd+s → Done\n結果：標題沒有點")
    await h.doll.ownerSaid("幫我用 TextEdit 寫筆記")
    try await h.turn("好")
    let mem = try memoryEvents(h)
    #expect(mem.map { noIDs($0.text) } == ["【相關記憶】程式用主人的話查到、最相近的過去經驗：\n〔大概｜public〕\n經驗：幫我用 TextEdit 寫筆記\n期望：筆記存好\n1. key cmd+s → Done\n結果：標題沒有點"])
    // Right after the owner's line, before the footer.
    let seen = await h.model.seen(0)
    #expect(seen[seen.count - 2].content.hasSuffix("] " + mem[0].text) && seen[seen.count - 2].role == "user")
    #expect(seen[seen.count - 3].content.hasSuffix("幫我用 TextEdit 寫筆記"))
    #expect(h.last.attached == [mem[0].text])
    #expect(h.logs.contains { $0.contains("選記憶（主人的新訊息）") && $0.contains("prepared 1;") && $0.contains("given 1 [") })
    // Turns on her own tool results attach nothing.
    try await h.turn(Scripted("", think("medium")))
    try await h.turn("嗯")
    #expect(try memoryEvents(h).count == 1 && h.last.attached.isEmpty)

    // An owner line unrelated to every memory (cosine under `[select] similarity`): no event.
    let none = try await Harness()
    try await seedMemory(none, "public", "經驗：幫我用 TextEdit 寫筆記")
    await none.doll.ownerSaid("今天天氣好熱")
    try await none.turn("好")
    #expect(try memoryEvents(none).isEmpty && none.last.attached.isEmpty && none.last.selections.count == 1)
    #expect(none.logs.contains { $0.contains("選記憶（主人的新訊息）") && $0.contains("given 0 []") })
}

@Test func onlyExperiencesAreAttachedNeverShortcutsOrHerOtherMemories() async throws {
    let h = try await Harness()
    h.cua.change { $0.windows.append(FakeCua.untitled) }
    try await seedMemory(h, "public", "TextEdit 存檔按 cmd+s", tier: "owner", confidence: 0.9)
    try await seedMemory(h, "public", "TextEdit：File › Save＝⌘S（TextEdit 1.20）", tier: "program", confidence: 0.95)
    try await seedMemory(h, "public", "經驗：用 TextEdit 存檔\n1. key cmd+s → Done\n結果：存好了")
    await h.doll.ownerSaid("用 TextEdit 存檔")
    try await h.turn(Scripted("", todoSet(), ("read_window", [("app", "TextEdit")])))
    // The owner's line: the experience only. The read shows TextEdit (a switch): nothing new to
    // attach, no event.
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
    try await h.turn(Scripted("", dm("好")))
    #expect(try memoryEvents(h).count == 2)
}

@Test func attachedMemoriesAreResentByteIdentical() async throws {
    // FakeEmbedder: the short owner line against the longer experience is under 0.55.
    let h = try await Harness(tune: { $0.select.similarity = 0.2 })
    try await seedMemory(h, "public", "經驗：用電腦存檔\n1. key cmd+s → Done")
    await h.doll.ownerSaid("用電腦存檔")
    try await h.turn(Scripted("", todoSet(), readCall))
    try await h.turn(Scripted("", think("medium")))
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
    #expect(noIDs(first[0].text).contains("〔大概｜own〕\n經驗：我的 TextEdit 帳號是 doll") && noIDs(first[0].text).contains("〔大概｜public〕\n經驗：TextEdit 存檔按 cmd+s"))
    // A secret owner line: the turn has seen a secret, so secret/ is read, and the event is a secret one.
    await h.doll.ownerSaid("TextEdit 的秘密筆記", secret: true)
    try await h.turn("好")
    let second = try memoryEvents(h)
    #expect(second.count == 2 && second[1].secret && noIDs(second[1].text).contains("〔大概｜secret〕\n經驗：TextEdit 的秘密筆記密碼是 1234"))
}

// todo items that used the computer, and experiences (spec 3.1, 2026-11-09; todo 2026-11-14) ------

/// Her public memory files with tier self_reported.
private func experiences(_ h: Harness) throws -> [(text: String, tier: SourceTier, confidence: Double, kind: String, app: String, version: String)] {
    try publicMemories(h).filter { $0.tier == .selfReported }
}

@Test func todoSetNeedsAListOfNonEmptyItems() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("用電腦")
    try await h.turn(Scripted("", ("todo.set", [])))
    #expect(try h.toolResults().last!.text == "失敗：todo.set 少了參數 items")
    try await h.turn(Scripted("", ("todo.set", [("items", "[\"  \"]")])))
    #expect(try h.toolResults().last!.text == "失敗：todo.set 的 items 裡有空的項目")
    try await h.turn(Scripted("", ("todo.set", [("items", "存檔")])))
    #expect(try h.toolResults().last!.text.hasPrefix("失敗：todo.set 的 items 要是字串的 JSON 陣列"))
    try await h.turn(Scripted("", done("好了")))
    #expect(try h.toolResults().last!.text == "失敗：todo.done 要有進行中的項目；現在是待命中，先用 todo.set 列 todo")
    #expect(await h.doll.todo.phase == .idle)
}

@Test func aDoneItemThatUsedTheComputerWritesOneExperienceOfHerActions() async throws {
    // FakeEmbedder: the short owner line against the whole experience is under 0.55.
    let h = try await Harness(tune: { $0.select.similarity = 0.2 })
    await h.doll.ownerSaid("把 notes 存檔")
    try await h.turn(Scripted("", todoSet("標題沒有「已編輯」"), readCall))
    // An action that fails is not a step; each step is only what she did, no driver answer text.
    h.cua.refuse("click", "boom\nsecond line")
    try await h.turn(Scripted("", clickRow(3)))
    try await h.turn(Scripted("", clickRow(2), readCall))
    try await h.turn(Scripted("", act(#"{"click": {"x": 100, "y": 50}, "count": 2}"#),
                              act(#"{"type": "娃娃來過"}"#), key("cmd+s")))
    // She looks, tells him, then answers in the same reply.
    try await h.turn(Scripted("", readCall, dm("存好了，標題列沒有「已編輯」"), done("按 ⌘S 存了檔，看標題列確認沒有「已編輯」。\n1. 這行是我寫的，不是步驟")))
    #expect(h.last.footer.contains("- todo：進行中「標題沒有「已編輯」」"))
    #expect(try h.toolResults().last!.text.hasPrefix("完成，記下了。這次的經過和步驟存成一條經驗。"))
    let text = "經驗：把 notes 存檔\n這一項：標題沒有「已編輯」\n按 ⌘S 存了檔，看標題列確認沒有「已編輯」。\n1. 這行是我寫的，不是步驟\n步驟：\n"
        + "1. click AXButton \"7\"\n2. click 100,50 雙擊\n3. type「娃娃來過」\n4. key cmd+s"
    #expect(h.last.itemEnd == "完成" && h.last.experience == text && h.last.actions.contains("todo done"))
    let mems = try experiences(h)
    let conf = try exampleConfig().memory.confidence[.selfReported]!
    #expect(mems.count == 1 && mems[0].text == text && mems[0].confidence == conf && mems[0].kind == "")
    #expect(conf >= 0.3 && conf < 0.7)
    // The list is done: idle, the computer refused again.
    try await h.turn(Scripted("", key("Return")))
    #expect(h.last.actions == ["refused act"] && h.last.footer.contains("- todo：待命中"))
    // Its stats are in the front-matter, not in the text (what is embedded): this item's 5 turns.
    let files = try experienceFiles(h)
    let stats = try #require(files.first?.stats)
    #expect(files.count == 1 && files[0].task == "把 notes 存檔" && files[0].body == text && !files[0].body.contains("成績"))
    // Only the program's steps count as steps, not a numbered line in her result.
    #expect(Experience(id: "", folder: "public", secret: false, level: "", file: files[0]).steps
            == ["click AXButton \"7\"", "click 100,50 雙擊", "type「娃娃來過」", "key cmd+s"])
    #expect(stats.turns == 5 && (0..<30).contains(stats.seconds)
            && stats == ExperienceStats(turns: 5, seconds: stats.seconds, attachedFollowed: 0, attachedSuccess: 0, attachedFailed: 0))
    // The next time the owner asks the same, it is attached whole, its stats line first.
    await h.doll.ownerSaid("把 notes 存檔")
    try await h.turn("好")
    #expect(stats.line == "成績：這次 5 回合、\(stats.seconds) 秒；附上後照做 0 次，成功 0 次（這是已知可行的做法；如果看到更短的走法，可以直接走；結果猜得到的幾步可以寫在同一則回覆裡。）")
    #expect(h.last.attached.count == 1 && noIDs(h.last.attached[0]).hasSuffix("〔大概｜public〕\n" + stats.line + "\n" + text))
}

@Test func aFailedItemOrOneWithoutTheComputerWritesNoExperience() async throws {
    let h = try await menuHarness()
    try await h.turn(Scripted("", key("cmd+s"), fail("還有點問題沒解決")))
    #expect(h.last.itemEnd == "失敗" && h.last.experience == nil)
    #expect(try experiences(h).isEmpty)
    // An item that never used the computer: done, no experience.
    try await h.turn(Scripted("", todoSet("跟主人說好了")))
    try await h.turn(Scripted("", dm("好"), done("說了")))
    #expect(h.last.itemEnd == "完成" && h.last.experience == nil)
    #expect(try experiences(h).isEmpty)
    // An item still in progress when the program stops: it stays in the timeline's todo.
    try await h.turn(Scripted("", todoSet(), readCall))
    try await h.turn(Scripted("", key("Return")))
    await h.doll.shutdown()
    #expect(h.logs.contains { $0.contains("todo: stopping with「畫面上看得到結果」進行中") })
    #expect(try h.timeline().todo.phase == .doing)
    #expect(try experiences(h).isEmpty)
}

@Test func doneEndsTheListAndNothingWakesHerForIt() async throws {
    let h = try await Harness([Scripted("", todoSet(), readCall), Scripted("", dm("存好了"), key("cmd+s"), done("看到了"))])
    let doll = h.doll
    let runner = Task { try await doll.run() }
    await h.doll.ownerSaid("存檔")
    let deadline = ContinuousClock.now + .seconds(20)
    while await h.model.requests.count < 2 || h.reports.count < 2, ContinuousClock.now < deadline {
        try await Task.sleep(for: .milliseconds(20))
    }
    try await Task.sleep(for: .milliseconds(200))
    #expect(await h.model.requests.count == 2 && h.said == ["存好了"])  // her words came with the done
    #expect(await h.doll.settled)
    #expect(await h.doll.todo.phase == .idle)
    #expect(h.last.itemEnd == "完成" && h.cua.actions == ["hotkey"])
    // The result is still in the timeline, right after her reply.
    let events = try h.events()
    #expect(events.last?.kind == .tool && events.last!.text.hasPrefix("完成，記下了。這次的經過和步驟存成一條經驗。")
            && events.last!.text.hasSuffix("清單做完了，現在待命中；程式不會再為這件事叫你。"))
    #expect(events.dropLast(3).last?.kind == .doll)
    await h.doll.stop()
    try await runner.value
}

@Test func callsAfterDoneInTheSameReplyAreRefused() async throws {
    let h = try await menuHarness()
    try await h.turn(Scripted("", key("cmd+s"), dm("存好了"), done("存好了"), key("Return"), readCall))
    let refused = "失敗：這則回覆前面已經用 todo.done 收起這一項了，後面的呼叫沒有執行；看了結果下一回合再做。"
    let results = try h.toolResults().suffix(3).map(\.text)
    #expect(results.first!.hasPrefix("完成，記下了。") && Array(results.dropFirst()) == [refused, refused])
    #expect(h.last.actions == ["act", "said discord_dm", "todo done", "error act", "error read_window"])
    #expect(h.cua.actions == ["hotkey"])
    #expect(await !h.doll.followUp)
    #expect(await h.doll.todo.phase == .idle)
    // The next request: the item is compacted to her todo calls and their results; the refused
    // calls and the screenshots are not in it.
    await h.doll.ownerSaid("好吧")
    try await h.turn("嗯")
    let request = await h.model.requests.last!.messages
    #expect(request.contains { $0.role == "tool" && $0.content.hasPrefix("todo 列好了，") })
    #expect(request.contains { $0.role == "tool" && $0.content.hasPrefix("完成，記下了。") })
    #expect(!request.contains { $0.content.hasPrefix("失敗：這則回覆前面已經用") || !$0.images.isEmpty })
}

/// The refusal of a `todo.done` that would finish a list `who` gave before she told them, naming `tool`.
private func notTold(_ who: String, _ tool: String) -> String {
    "失敗：還沒完成：這份清單是\(who)交代的，你列 todo 以後還沒跟\(who)說過話；todo.done 的 result 只有你自己看得到，寫「已回報」不會讓他收到。"
        + "先用 \(tool) 跟\(who)說結果（和你怎麼確認的），再 todo.done；同一則回覆也可以。"
}

@Test func anAcknowledgementAheadOfTodoSetInTheSameReplyDoesNotMakeTheListHers() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("7 加 8 是多少")
    try await h.turn(Scripted("", dm("好，我算算"), todoSet("算出 7+8"), readCall))
    try await h.turn(Scripted("", clickRow(2), done("15")))
    #expect(try h.toolResults().last!.text == notTold("主人", "discord.dm"))
    try await h.turn(Scripted("", dm("是 15"), done("15")))
    #expect(try h.toolResults().last!.text.hasPrefix("完成，記下了。"))
}

@Test func theDoneThatFinishesTheOwnersListNeedsALineToHimFirst() async throws {
    // m7-mac (2026-11-16): the answer went only into todo.done's result, which he never sees.
    let h = try await Harness()
    await h.doll.ownerSaid("7 加 8 是多少")
    // Her DM after todo.set in the reply that set the list is an acknowledgement, not the result.
    try await h.turn(Scripted("", todoSet("算出 7+8"), readCall, dm("我算算")))
    try await h.turn(Scripted("", clickRow(2), done("15")))
    #expect(try h.toolResults().last!.text == notTold("主人", "discord.dm"))
    #expect(h.last.actions == ["act", "error todo.done"] && h.last.itemEnd == nil)
    #expect(await h.doll.todo.phase == .doing)
    // A line to him in a later reply: the done goes through.
    try await h.turn(Scripted("", dm("是 15")))
    try await h.turn(Scripted("", done("15")))
    #expect(try h.toolResults().last!.text.hasPrefix("完成，記下了。") && h.last.itemEnd == "完成")
    #expect(await h.doll.todo.phase == .idle)
    // Only the done that finishes the list is gated: an item with more after it goes through untold.
    await h.doll.ownerSaid("再算 8 加 8，然後 7 加 7")
    try await h.turn(Scripted("", todoSet("算出 8+8", "算出 7+7"), readCall))
    try await h.turn(Scripted("", done("16")))
    #expect(try h.toolResults().last!.text.hasPrefix("完成，記下了。"))
    #expect(await h.doll.todo.current == "算出 7+7")
    try await h.turn(Scripted("", readCall, done("14")))
    #expect(try h.toolResults().last!.text == notTold("主人", "discord.dm"))
    // Her line written after the done in the same reply counts too.
    try await h.turn(Scripted("", done("14"), dm("16 和 14")))
    #expect(try h.toolResults().suffix(2).first!.text.hasPrefix("完成，記下了。") && h.last.actions == ["todo done", "said discord_dm"])
    #expect(h.said.last == "16 和 14")
    #expect(await h.doll.todo.phase == .idle)
}

@Test func herOwnListIsNotGatedAndTheDevelopersListNeedsHisChannel() async throws {
    let h = try await Harness()
    // Nobody's line is waiting (only a wake of hers): her own list, done without a line to anyone.
    await h.doll.put(Event(.watch, .internal, "想整理一下計算機"))
    try await h.turn(Scripted("", todoSet("計算機歸零"), readCall))
    try await h.turn(Scripted("", done("看過了，是 0")))
    #expect(try h.toolResults().last!.text.hasPrefix("完成，記下了。") && h.said.isEmpty)
    // The owner's line answered in an earlier reply: the list she sets afterwards is hers too.
    // (Answered in the same reply as todo.set it stays his: that shape is 「好，我來」＋list.)
    await h.doll.ownerSaid("在嗎")
    try await h.turn(Scripted("", dm("在")))
    await h.doll.put(Event(.watch, .internal, "想看一下計算機"))
    try await h.turn(Scripted("", todoSet("看一下計算機"), readCall))
    try await h.turn(Scripted("", done("看過了")))
    #expect(try h.toolResults().last!.text.hasPrefix("完成，記下了。"))
    // The developer's list: a DM to the owner is not a line to him; his channel is dollctl.reply.
    await h.doll.developerSaid("讀一下計算機的畫面")
    try await h.turn(Scripted("", todoSet("讀了計算機"), readCall))
    try await h.turn(Scripted("", dm("我在讀計算機"), done("讀了")))
    #expect(try h.toolResults().last!.text == notTold("開發者", "dollctl.reply"))
    try await h.turn(Scripted("", done("讀了"), reply("讀了，顯示 0")))
    #expect(h.last.actions == ["todo done", "said dollctl"] && h.last.itemEnd == "完成")
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

@Test func aFollowedExperienceCountsHowHerItemEndedAndRanksBy() async throws {
    let h = try await Harness()
    let task = "把 notes 存檔"
    try await seedExperience(h, "1-seed", task: task, steps: ["click AXButton \"7\"", "key cmd+s", "key cmd+w", "type 「娃娃來過」"],
                             stats: ExperienceStats(turns: 9, seconds: 150))
    // 1: two of its four steps in order (ratio 0.5 = experience_follow_ratio): followed, her success counted.
    await h.doll.ownerSaid(task)
    try await h.turn(Scripted("", todoSet(), readCall))
    let attached = try #require(h.reports[0].attached.first)
    #expect(attached.contains("成績：這次 9 回合、150 秒；附上後照做 0 次，成功 0 次"))
    try await h.turn(Scripted("", clickRow(2), key("cmd+s")))
    try await h.turn(Scripted("", dm("存好了"), done("存好了")))
    #expect(h.last.follows == ["experience 1-seed: 2/4 steps done in order (ratio 0.50), followed, counted success: 照做 1、成功 1、失敗 0"])
    #expect(h.logs.contains { $0.contains("experience 1-seed: 2/4 steps done in order (ratio 0.50)") })
    #expect(try statsOf(h, "1-seed") == ExperienceStats(turns: 9, seconds: 150, attachedFollowed: 1, attachedSuccess: 1))
    // The attached event in the timeline is not re-rendered with the new stats: every request while
    // the item runs starts with the one before it (two reads, within one `read_batch`: nothing put
    // away), and the first after it with the one that listed it, up to that reply's footer (the
    // item's turns are compacted).
    try await h.turn("好了")
    #expect(try memoryEvents(h).first?.text == attached)
    let reqs = await h.model.requests.map(\.messages)
    #expect(Array(reqs[1].prefix(reqs[0].count)) == reqs[0])
    #expect(Array(reqs[2].prefix(reqs[1].count)) == reqs[1] && reqs[2].filter { !$0.images.isEmpty }.count == 2)
    #expect(reqs.count == 4 && Array(reqs[3].prefix(reqs[0].count - 1)) == Array(reqs[0].dropLast()))
    // Her new experience (3 turns: todo.set, the actions, todo.done) now leads the group.
    let fresh = try #require(experienceFiles(h).first { $0.task == task && $0.stats?.turns == 3 })
    #expect(fresh.stats == ExperienceStats(turns: 3, seconds: fresh.stats!.seconds))

    // 2: the new one is attached; none of its steps done: not followed, nothing counted.
    await h.doll.ownerSaid(task)
    try await h.turn(Scripted("", todoSet(), readCall))
    #expect(h.last.attached.count == 1 && h.last.attached[0].contains("成績：這次 3 回合") && !h.last.attached[0].contains("9 回合"))
    try await h.turn(Scripted("", key("cmd+q"), fail("存檔沒存到")))
    #expect(h.last.follows.count == 1 && h.last.follows[0].hasSuffix("0/2 steps done in order (ratio 0.00), not followed"))
    #expect(try experienceFiles(h).first { $0.stats?.turns == 3 }?.stats?.attachedFollowed == 0)

    // 3: followed, and she says it failed: counted as a failure, and the failure demotes it below
    // the 9-turn one that succeeded.
    await h.doll.ownerSaid(task)
    try await h.turn(Scripted("", todoSet(), readCall))
    try await h.turn(Scripted("", clickRow(2), key("CMD+S"), fail("存檔沒存到")))
    #expect(h.last.follows.count == 1 && h.last.follows[0].contains("2/2 steps done in order (ratio 1.00), followed, counted failure: 照做 1、成功 0、失敗 1"))
    let ranking = try await h.doll.experienceRanking()
    #expect(ranking.count == 1 && ranking[0].count == 2 && ranking[0][0].hasPrefix("1-seed: 成績：這次 9 回合、150 秒；附上後照做 1 次，成功 1 次；失敗 0 次"))
    #expect(ranking[0][1].contains("3 回合") && ranking[0][1].hasSuffix("附上後照做 1 次，成功 0 次；失敗 1 次"))
    // An item still in progress when the program stops: only logged.
    await h.doll.ownerSaid(task)
    try await h.turn(Scripted("", todoSet(), readCall))
    try await h.turn(Scripted("", clickRow(2), key("cmd+s"), key("cmd+w")))
    await h.doll.shutdown()
    #expect(h.logs.contains { $0.contains("experience 1-seed: 3/4 steps done in order (ratio 0.75), followed; outcome unknown, nothing counted") })
    #expect(try statsOf(h, "1-seed") == ExperienceStats(turns: 9, seconds: 150, attachedFollowed: 1, attachedSuccess: 1))
}

// file tools ------------------------------------------------------------------

/// Her file tools work on this Mac's files; the tests use absolute paths under the harness's data dir.
private let todoBefore = "買牛奶\n洗衣服\n寫作業\n"
private func readFile(_ path: String, from: Int? = nil, to: Int? = nil) -> (String, [(String, String)]) {
    ("read_file", [("path", path)] + (from.map { [("from", "\($0)")] } ?? []) + (to.map { [("to", "\($0)")] } ?? []))
}
private func writeFile(_ path: String, _ text: String) -> (String, [(String, String)]) { ("write_file", [("path", path), ("text", text)]) }
private func editFile(_ path: String, _ old: String, _ new: String) -> (String, [(String, String)]) {
    ("edit_file", [("path", path), ("old", old), ("new", new)])
}
extension Harness {
    /// An absolute path under this harness's `files/` (not created).
    func path(_ name: String) -> String { dir.appending(path: "files/\(name)").path }
    /// Writes `text` as `files/<name>` and returns its absolute path.
    @discardableResult func put(_ name: String, _ text: String) throws -> String {
        let p = path(name)
        try FileManager.default.createDirectory(at: URL(filePath: p).deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data(text.utf8).write(to: URL(filePath: p))
        return p
    }
    /// The file's text, nil when there is none.
    func file(_ path: String) -> String? { FileManager.default.contents(atPath: path).map { String(decoding: $0, as: UTF8.self) } }
}

@Test func readFileNumbersLinesReadsARangeAndCapsAtTheConfiguredCount() async throws {
    let h = try await menuHarness(tune: { $0.files.maxReadLines = 3 })
    let todo = try h.put("todo.txt", todoBefore)
    let actionsBefore = h.cua.actions
    try await h.turn(Scripted("", readFile(todo), readFile(todo, from: 2, to: 3), readFile(todo, from: 3)))
    var results = Array(try h.toolResults().suffix(3))
    #expect(results[0].text == "read_file \(todo)：共 3 行\n     1\t買牛奶\n     2\t洗衣服\n     3\t寫作業")
    #expect(results[1].text == "read_file \(todo)：共 3 行，這裡是第 2–3 行\n     2\t洗衣服\n     3\t寫作業")
    #expect(results[2].text == "read_file \(todo)：共 3 行，這裡是第 3–3 行\n     3\t寫作業")
    // Text only: no screenshot, no element list; nothing sent to the driver.
    #expect(results.allSatisfy { $0.image == nil && $0.window == nil })
    #expect(h.last.actions == ["read_file", "read_file", "read_file"] && h.cua.actions == actionsBefore)
    // Over the cap: the first lines and how many are left; from reads on. `to` past the end is the end.
    let five = try h.put("five.txt", "1\n2\n3\n4\n5\n")
    try await h.turn(Scripted("", readFile(five), readFile(five, from: 4, to: 99), readFile(five, from: 2, to: 2)))
    results = Array(try h.toolResults().suffix(3))
    #expect(results[0].text == "read_file \(five)：共 5 行，這裡是第 1–3 行\n     1\t1\n     2\t2\n     3\t3\n還有 2 行（read_file 加 from: 4 讀下去）")
    #expect(results[1].text == "read_file \(five)：共 5 行，這裡是第 4–5 行\n     4\t4\n     5\t5")
    #expect(results[2].text == "read_file \(five)：共 5 行，這裡是第 2–2 行\n     2\t2")
    // An empty file, a last line without a newline, CRLF lines (the CR stays on its line).
    let empty = try h.put("empty.txt", ""), noeol = try h.put("noeol.txt", "a\nb"), crlf = try h.put("crlf.txt", "a\r\nb\r\n")
    try await h.turn(Scripted("", readFile(empty), readFile(noeol), readFile(crlf)))
    results = Array(try h.toolResults().suffix(3))
    #expect(results[0].text == "read_file \(empty)：空檔案，0 行")
    #expect(results[1].text == "read_file \(noeol)：共 2 行\n     1\ta\n     2\tb")
    #expect(results[2].text == "read_file \(crlf)：共 2 行\n     1\ta\r\n     2\tb\r")
    // Bad arguments, a missing file and a folder: an error each, nothing read, no step.
    let nope = h.path("nope.txt"), folder = URL(filePath: todo).deletingLastPathComponent().path
    try await h.turn(Scripted("", readFile(todo, from: 0), readFile(todo, from: 3, to: 2), ("read_file", [("path", todo), ("from", "abc")]),
                              readFile(todo, from: 9), ("read_file", [("path", "  ")]), readFile(nope), readFile(folder)))
    #expect(try h.toolResults().suffix(7).map(\.text) == [
        "失敗：read_file 的 from 從 1 算起，收到 0",
        "失敗：read_file 的 from 要小於等於 to，收到 from 3、to 2",
        "失敗：read_file 的 from 要是整數，收到 abc",
        "失敗：\(todo) 只有 3 行，from 是 9；from 最多 3",
        "失敗：read_file 的 path 是空的",
        "失敗：沒有這個檔案：\(nope)",
        "失敗：\(folder) 是資料夾，不是檔案",
    ])
    #expect(h.last.actions == Array(repeating: "error read_file", count: 7))
}

@Test func writeFileCreatesOrOverwritesTheWholeFileAndReportsLinesAndBytes() async throws {
    let h = try await menuHarness()
    let text = "你好，我是娃娃\n第二行\n"
    let hello = h.path("new/hello.txt")
    try await h.turn(Scripted("", writeFile(hello, text)))
    #expect(try h.toolResults().last!.text == "write_file \(hello)：寫好了，2 行、\(text.utf8.count) bytes")
    #expect(h.file(hello) == text)
    #expect(try h.toolResults().last!.image == nil)
    // Overwritten whole; no trailing newline stays that way; an empty text empties the file.
    try await h.turn(Scripted("", writeFile(hello, "x"), writeFile(hello, "")))
    #expect(try h.toolResults().suffix(2).map(\.text) == ["write_file \(hello)：寫好了，1 行、1 bytes",
                                                          "write_file \(hello)：寫好了，0 行、0 bytes"])
    #expect(h.file(hello) == "")
    try await h.turn(Scripted("", ("write_file", [("path", ""), ("text", "a")]), ("write_file", [("path", h.path("a.txt"))])))
    #expect(try h.toolResults().suffix(2).map(\.text) == ["失敗：write_file 的 path 是空的", "失敗：write_file 少了參數 text"])
    // A folder is not written over.
    let folder = h.path("new")
    try await h.turn(Scripted("", writeFile(folder, "a")))
    #expect(try h.toolResults().last!.text == "失敗：\(folder) 是資料夾，不能當檔案寫" && h.last.actions == ["error write_file"])
}

@Test func editFileReplacesTheOneOccurrenceOrSaysHowManyAndTheNearestLines() async throws {
    let h = try await menuHarness()
    let todo = try h.put("todo.txt", todoBefore)
    try await h.turn(Scripted("", editFile(todo, "買牛奶", "買豆漿"), editFile(todo, "寫作業", "寫作業\n交房租")))
    var results = Array(try h.toolResults().suffix(2))
    #expect(results[0].text == "edit_file \(todo)：改好了，第 1 行起（1 行 → 1 行），現在共 3 行\n     1\t買豆漿\n     2\t洗衣服\n     3\t寫作業")
    #expect(results[1].text == "edit_file \(todo)：改好了，第 3 行起（1 行 → 2 行），現在共 4 行\n     1\t買豆漿\n     2\t洗衣服\n     3\t寫作業\n     4\t交房租")
    #expect(h.file(todo) == "買豆漿\n洗衣服\n寫作業\n交房租\n")
    #expect(results.allSatisfy { $0.image == nil && $0.window == nil } && h.last.actions == ["edit_file", "edit_file"])
    // A multi-line old, a deletion (empty new), context around a change deep in a file.
    try await h.turn(Scripted("", editFile(todo, "洗衣服\n寫作業", "寫作業\n洗衣服"), editFile(todo, "交房租\n", "")))
    #expect(h.file(todo) == "買豆漿\n寫作業\n洗衣服\n")
    #expect(try h.toolResults().last!.text == "edit_file \(todo)：改好了，第 4 行起（2 行 → 1 行），現在共 3 行\n     1\t買豆漿\n     2\t寫作業\n     3\t洗衣服")
    let long = try h.put("long.txt", (1...12).map { "line \($0)" }.joined(separator: "\n") + "\n")
    try await h.turn(Scripted("", editFile(long, "line 7", "seven")))
    #expect(try h.toolResults().last!.text == "edit_file \(long)：改好了，第 7 行起（1 行 → 1 行），現在共 12 行\n"
            + "     4\tline 4\n     5\tline 5\n     6\tline 6\n     7\tseven\n     8\tline 8\n     9\tline 9\n    10\tline 10")
    // 0 occurrences: the count and the nearest lines (most shared characters), exact whitespace included;
    // several: the count; neither changes the file. Empty old and old == new never reach the file.
    let before = h.file(todo)
    let dup = try h.put("dup.txt", "a\nb\na\n")
    try await h.turn(Scripted("", editFile(todo, "買牛乳", "買豆漿"), editFile(todo, "買豆漿 ", "x"), editFile(todo, "zzz", "x"),
                              editFile(dup, "a", "c"), editFile(todo, "", "x"), editFile(todo, "洗衣服", "洗衣服")))
    results = Array(try h.toolResults().suffix(6))
    let zero = "失敗：edit_file \(todo)：old 在檔案裡出現 0 次，沒改。old 要跟檔案裡一字不差（含空白、換行、標點）；"
    #expect(results[0].text == zero + "最接近的幾行（照這裡的字寫 old）：\n     1\t買豆漿\n     2\t寫作業")
    #expect(results[1].text == zero + "最接近的幾行（照這裡的字寫 old）：\n     1\t買豆漿\n     2\t寫作業")
    #expect(results[2].text == zero + "檔案裡也沒有相近的行，先 read_file 看一下")
    #expect(results[3].text == "失敗：edit_file \(dup)：old 在檔案裡出現 2 次，不知道改哪一個，沒改。old 多帶幾行前後文，讓它只出現一次")
    #expect(results[4].text == "失敗：edit_file 的 old 是空的；要加字就把它旁邊現有的那段當 old，new 填那段加上新的字")
    #expect(results[5].text == "失敗：edit_file 的 old 和 new 一樣，沒有要改什麼")
    #expect(h.file(todo) == before && h.file(dup) == "a\nb\na\n")
    #expect(h.last.actions == Array(repeating: "error edit_file", count: 6))
    // The nearest line for a multi-line old is judged by its first non-blank line, shown with its neighbours.
    try await h.turn(Scripted("", editFile(long, "\nline 9x\nline 10", "y")))
    #expect(try h.toolResults().last!.text.hasSuffix("最接近的幾行（照這裡的字寫 old）：\n     8\tline 8\n     9\tline 9\n    10\tline 10"))
}

@Test func fileToolsAreRefusedWithoutAnItemInProgressAndRefuseNonUTF8Files() async throws {
    let h = try await Harness()
    let todo = try h.put("todo.txt", todoBefore)
    await h.doll.ownerSaid("看一下 todo")
    try await h.turn(Scripted("", readFile(todo), writeFile(h.path("a.txt"), "a"), editFile(todo, "買牛奶", "買豆漿")))
    let refused = { (tool: String) in "失敗：現在沒有進行中的 todo，\(tool) 不能用。先用 todo.set 列 todo（小事就一項，每項寫做完的樣子），再做。" }
    #expect(try h.toolResults().suffix(3).map(\.text) == [refused("read_file"), refused("write_file"), refused("edit_file")])
    #expect(h.last.actions == ["refused read_file", "refused write_file", "refused edit_file"])
    #expect(h.file(todo) == todoBefore && h.file(h.path("a.txt")) == nil)
    try await h.turn(Scripted("", todoSet(), readCall))
    let bin = h.path("bin.dat")
    try Data([0xFF, 0xFE, 0x41]).write(to: URL(filePath: bin))
    try await h.turn(Scripted("", readFile(bin), editFile(bin, "A", "B")))
    let text = "失敗：\(bin) 不是 UTF-8 文字檔（3 bytes），read_file、write_file、edit_file 只處理文字檔"
    #expect(try h.toolResults().suffix(2).map(\.text) == [text, text])
    #expect(FileManager.default.contents(atPath: bin) == Data([0xFF, 0xFE, 0x41]))
}

@Test func aPathWithATildeIsTheOwnersHome() {
    let home = FileManager.default.homeDirectoryForCurrentUser.path
    #expect(Mac.expand("~") == home && Mac.expand("~/Documents/todo.txt") == home + "/Documents/todo.txt"
            && Mac.expand("Documents/a.txt") == home + "/Documents/a.txt" && Mac.expand("/tmp/a") == "/tmp/a")
}

@Test func fileStepsRecordThePathOnlyAndMatchByPath() async throws {
    let h = try await Harness()
    let todo = try h.put("todo.txt", todoBefore)
    let a = h.path("a.txt")
    await h.doll.ownerSaid("改 todo")
    try await h.turn(Scripted("", todoSet("todo.txt 第一行是買豆漿")))
    try await h.turn(Scripted("", readFile(todo), writeFile(a, "一\n二\n"), editFile(todo, "沒有這段", "x"),
                              editFile(todo, "買牛奶", "買豆漿"), dm("改好了"), done("第一行是買豆漿", evidence: "買豆漿")))
    #expect(h.last.experience == "經驗：改 todo\n這一項：todo.txt 第一行是買豆漿\n第一行是買豆漿\n步驟：\n1. read_file \(todo)\n2. write_file \(a)（2 行）\n3. edit_file \(todo)")
    func same(_ a: String, _ b: String) -> Bool { StepShape(a).matches(StepShape(b)) }
    #expect(same("write_file ~/a.txt（2 行）", "write_file ~/a.txt（5 行）") && same("read_file ~/a.txt", "read_file ~/a.txt"))
    #expect(!same("read_file ~/a.txt", "edit_file ~/a.txt") && !same("read_file ~/a.txt", "read_file ~/b.txt") && !same("read_file ~/a.txt", "type 「~/a.txt」"))
    #expect(Doll.matchedSteps(["read_file ~/t.txt", "edit_file ~/t.txt"], ["read_file ~/t.txt", "write_file ~/t.txt（3 行）", "edit_file ~/t.txt"]) == 2)
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
    _ = try await model.chat(turn([a, b]), tools: nil, effort: "none")
    let two = try await model.chat(turn([a, b, c]), tools: nil, effort: "none")
    // Turn 1: both as bytes. Turn 2: a and b by reference, c as bytes; the body is smaller than all three.
    #expect(server.s.withLock { $0.requests } == [[a, b], ["hash:\(key(a))", "hash:\(key(b))", c]])
    #expect(two.usage.requestBytes! < server.s.withLock { $0.bodies[0] } + c.count)
    // The server drops a and b (an eviction or a restart): one 400 naming them, then the same request
    // with a and b as bytes and c still by reference.
    server.forget([key(a), key(b)])
    _ = try await model.chat(turn([a, b, c]), tools: nil, effort: "none")
    #expect(server.s.withLock { Array($0.requests.suffix(2)) }
            == [["hash:\(key(a))", "hash:\(key(b))", "hash:\(key(c))"], [a, b, "hash:\(key(c))"]])

    // A server without the flag gets every picture as bytes, every turn.
    let plain = FakeStrata(hashRefs: false)
    let old = HTTPMainModel(LlmConfig(baseURL: "http://strata", model: "m"), transport: plain.transport)
    try await old.check()
    _ = try await old.chat(turn([a, b]), tools: nil, effort: "none")
    _ = try await old.chat(turn([a, b, c]), tools: nil, effort: "none")
    #expect(!old.imageHashRefs && plain.s.withLock { $0.requests } == [[a, b], [a, b, c]])
}

@Test func toolsGoOutAsWrittenAndHerCallsComeBackFromToolCalls() async throws {
    let sent = Mutex<[String: Any]>([:])
    let reply = #"{"choices":[{"message":{"content":"嗯\n<tool_call>\n<function=bogus>\n</function>\n</tool_call>\n<tool_call>\n<parameter=x>\n1\n</parameter>","#
        + #""tool_calls":[{"id":"a","type":"function","function":{"name":"discord.dm","arguments":"{\"text\": \"在\"}"}},"#
        + #"{"id":"b","type":"function","function":{"name":"voice.join","arguments":"{}"}}]},"finish_reason":"tool_calls"}],"#
        + #""usage":{"prompt_tokens":5,"completion_tokens":3},"strata_rejected_tool_calls":[{"name":"bogus","reason":"undeclared"}]}"#
    let model = HTTPMainModel(LlmConfig(baseURL: "http://strata", model: "m"), transport: { req in
        sent.withLock { $0 = try! JSONSerialization.jsonObject(with: req.httpBody!) as! [String: Any] }
        return (Data(reply.utf8), 200)
    })
    let tools = Tools.json(Tools.todoSpecs(computer: true))
    let history = [ChatMessage(role: "system", content: "s"),
                   ChatMessage(role: "assistant", content: "想", toolCalls: [ToolCall(name: "recall", arguments: "{\"query\": \"貓\"}"),
                                                                           ToolCall(failed: "", ToolCall.unreadable)]),
                   ChatMessage(role: "tool", content: "r")]
    let r = try await model.chat(history, tools: tools, effort: "none")
    #expect(r.content.hasPrefix("嗯\n<tool_call>"))
    #expect(r.calls.map(\.name) == ["discord.dm", "voice.join", "bogus", ""])
    #expect(r.calls[0].arguments == "{\"text\": \"在\"}" && r.calls[0].parameters == [.init("text", "在")])
    #expect(r.calls[2].malformed == "沒有 bogus 這個工具，沒有執行" && r.calls[3].malformed == ToolCall.unreadable)
    // Why it stopped is kept (2026-11-16).
    #expect(r.usage.finish == "tool_calls" && r.usage.stopLine == "finish=tool_calls completion=3 reasoning_chars=0")
    let body = sent.withLock { $0 }
    // `tools` as written; her calls as `tool_calls`, arguments verbatim; the failed one not sent
    #expect((body["tools"] as? NSArray) == (try JSONSerialization.jsonObject(with: Data(tools.utf8)) as? NSArray))
    let assistant = (body["messages"] as! [[String: Any]])[1]
    let calls = assistant["tool_calls"] as! [[String: Any]]
    #expect(calls.count == 1 && (calls[0]["function"] as! [String: Any])["arguments"] as? String == "{\"query\": \"貓\"}")
    // no tools: the key is absent (the consolidation call)
    _ = try await model.chat(history, tools: nil, effort: "none")
    #expect(sent.withLock { $0["tools"] } == nil)
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
    try await h.turn(Scripted("好，我看看", todoSet(), readCall,
                              remember(folders: [("我的信箱密碼是 hunter2", "own", nil), ("主人怕打雷", "secret", "happened"),
                                                 ("天空是藍的", "public", nil)])))
    try await h.turn(Scripted("", clickRow(2)))

    let s = try json(DashboardStatus.self, await d.respond(method: "GET", target: "/api/status"))
    #expect(s.todo == "進行中" && s.item == "畫面上看得到結果" && s.pending.isEmpty)
    #expect(s.state == "idle" && !s.paused && s.turn == 2 && s.queued == 0 && s.effort == "medium" && s.contextTokens > 0)

    let t = try json([DashboardItem].self, await d.respond(method: "GET", target: "/api/timeline"))
    #expect(t.map(\.type) == ["turn", "turn", "owner"])
    #expect(t[2].text == "用電腦看一下 macOS 版本" && t[2].voice == false)
    #expect(t[1].thought == "好，我看看" && t[1].calls?.map(\.name) == ["todo.set", "read_window", "remember"])
    #expect(t[1].calls?.allSatisfy { $0.result != nil } == true && t[1].calls?[0].image == nil && t[1].calls?[1].image != nil
            && t[1].calls?[2].image == nil)
    #expect(t[1].calls?[2].result?.contains("own/") == true)
    #expect(t[0].calls?.map(\.name) == ["act"] && t[0].calls?[0].parameters.map(\.name) == ["steps"])
    for turn in t.prefix(2) {
        #expect(turn.prompt != nil && turn.prompt! > 0 && turn.seconds != nil && turn.effort == "medium" && turn.footer?.hasPrefix("【狀況】") == true)
    }
    let limited = try json([DashboardItem].self, await dashboard(h, items: 1).respond(method: "GET", target: "/api/timeline"))
    #expect(limited.map(\.seq) == [t[0].seq])

    let v = try json(DashboardVision.self, await d.respond(method: "GET", target: "/api/vision"))
    let last = try h.toolResults().last!
    #expect(v.image == last.image && v.window == last.window && v.image != nil)
    #expect(v.window?.contains("- [1] AXStaticText \"7\"") == true)

    let m = try json(DashboardMemory.self, await d.respond(method: "GET", target: "/api/memory"))
    let byFolder = Dictionary(uniqueKeysWithValues: m.folders.map { ($0.folder, $0.entries) })
    #expect(m.folders.map(\.folder) == ["own", "public", "secret"])
    #expect(byFolder["own"]?.map(\.title) == ["我的信箱密碼是 hunter2"])
    #expect(byFolder["secret"]?.map(\.kind) == ["happened"] && byFolder["secret"]?.first?.tier == "owner")
    #expect(byFolder["public"]?.map(\.text) == ["天空是藍的"] && byFolder["public"]?.first?.level == "確定")
    #expect(m.attached.isEmpty)

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

@Test func theOnlyPostRoutesAreInterruptPauseResumeTheDeepMarkTheRevertTheCalendarHerDirectionAndHerTools() {
    #expect(Set(Dashboard.routes.filter { $0.method != "GET" }.map(\.path))
            == ["/api/interrupt", "/api/pause", "/api/resume", "/api/memory/deep/", "/api/memory/undeep/", "/api/memory/revert/", "/api/calendar/",
                "/api/ideal/", "/api/goals/", "/api/record/", "/api/tools/", "/api/history/"])
    #expect(Dashboard.routes.allSatisfy { ["GET", "POST"].contains($0.method) })
}

@Test func theShotRouteServesOneScreenshotByNameAndRefusesPaths() async throws {
    let h = try await Harness()
    let d = dashboard(h)
    await h.doll.ownerSaid("開電腦")
    try await h.turn(Scripted("", todoSet(), readCall))
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
    let h = try await Harness([Scripted("", dm("好"))])
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
    #expect(s.state == "idle" && s.turn == 0 && s.lastInterrupt != nil)
    #expect(h.logs.contains { $0.contains("turn 1 interrupted from the dashboard") })
    await doll.ownerSaid("好了嗎")                                                  // the next event gets a turn
    // Her line is out before her turn's report (2026-11-20: asserting the report on her line alone
    // failed when the turn's end came a moment later): wait for both.
    try await waitUntil { !h.said.isEmpty && !h.reports.isEmpty }
    #expect(await h.model.requests.count == 2 && h.said == ["好"] && h.reports.map(\.index) == [1])
    await doll.stop()
    try await runner.value
}

@Test func pauseStopsNewTurnsWhileEventsQueueAndResumeRunsThem() async throws {
    let h = try await Harness([Scripted("", dm("兩件都收到"))])
    let d = dashboard(h)
    let doll = h.doll
    let runner = Task { try await doll.run() }
    #expect(try json([String: Bool].self, await d.respond(method: "POST", target: "/api/pause")) == ["paused": true])
    await doll.ownerSaid("第一件")
    await doll.ownerSaid("第二件")
    try await Task.sleep(for: .milliseconds(300))
    #expect(await h.model.requests.isEmpty)
    let s = try json(DashboardStatus.self, await d.respond(method: "GET", target: "/api/status"))
    #expect(s.paused && s.queued == 2 && s.state == "idle")
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
    let head = "[llm]\nbase_url = \"http://x\"\nmodel = \"m\"\n[memory]\n"
        + SourceTier.allCases.map { "confidence_\($0.rawValue) = 0.5\n" }.joined()
    try (head + "[dashboard]\nport = 9000\nbind = \"127.0.0.1\"\ntimeline_items = 5\nenabled = false\n")
        .write(to: file, atomically: true, encoding: .utf8)
    let c = try Config.load(file).dashboard
    #expect(c.port == 9000 && c.bind == "127.0.0.1" && c.timelineItems == 5 && !c.enabled)
    for bad in ["typing = true", "port = 0", "port = 70000", "bind = \"\"", "timeline_items = 0"] {
        try (head + "[dashboard]\n\(bad)\n").write(to: file, atomically: true, encoding: .utf8)
        #expect(throws: TOMLError.self, "\(bad)") { try Config.load(file) }
    }
    let example = try exampleConfig().dashboard
    #expect(example.enabled && example.port == 8790 && example.bind == "127.0.0.1")
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
    try await h.turn(Scripted("好，我來", todoSet(), readCall))
    try await h.turn(Scripted("", key("cmd+s")))
    try await h.turn(Scripted("", dm("存好了。"), done("按 ⌘S 存了檔。")))
    await h.doll.ownerSaid("謝謝")
    try await h.turn(Scripted("", dm("不客氣")))
    await h.doll.ownerSaid("再開一次電腦")
    try await h.turn(Scripted("", todoSet(), readCall))                                    // an item in progress, with images
    let events = await h.doll.timelineEvents()
    let before = try await h.doll.context(events)
    #expect(before.contains { !$0.images.isEmpty })                                // screenshots in it
    #expect(before.filter { $0.content.hasPrefix("【狀況】") }.count == 2)          // the footers of replies outside the compacted item
    #expect(before.contains { $0.role == "tool" && $0.content.hasPrefix("完成，記下了。") })   // the compacted item

    // The file: one JSON object per line, every record type.
    let file = try h.timeline().file
    let lines = try String(contentsOf: file, encoding: .utf8).split(separator: "\n")
    let types = try lines.map { try JSONSerialization.jsonObject(with: Data($0.utf8)) as! [String: Any] }.map { $0["type"] as! String }
    #expect(Set(types) == ["event", "footer", "turn", "todo", "compact"])
    #expect(file.deletingLastPathComponent().lastPathComponent == "timeline" && file.pathExtension == "jsonl")

    let again = try await restarted(h, [Scripted("", dm("回來了"))])
    #expect(await again.doll.timelineEvents() == events)
    let after = try await again.doll.context(await again.doll.timelineEvents())
    #expect(try ChatMessage.encoder.encode(after) == ChatMessage.encoder.encode(before))
    #expect(try h.timeline().compactions.count == 1)
    // Its first request continues the old one byte for byte.
    await again.doll.ownerSaid("在嗎")
    try await again.turn()
    let sent = await again.model.seen(0)
    #expect(try ChatMessage.encoder.encode(Array(sent.prefix(before.count))) == ChatMessage.encoder.encode(before))
    let kinds = try h.events().map(\.kind)
    // One timeline: the owner's line, her reply, its result (the experience her todo.done wrote is not
    // related to 「在嗎」: nothing is attached).
    #expect(kinds.count == events.count + 3 && kinds.suffix(3) == [.owner, .doll, .tool])
    await h.doll.stop()
}

@Test func aLineCutShortByACrashIsDroppedAndTheNextOneStartsClean() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("你好")
    try await h.turn(Scripted("", dm("嗨")))
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
    let h = try await Harness(embedder: try await qwen3.value)
    await h.doll.ownerSaid("今天下午要去郵局寄包裹給阿嬤")
    try await h.turn(Scripted("要記得提醒主人帶身分證", dm("好，出門前我提醒你"), think("medium")))
    await h.doll.ownerSaid("晚餐想吃咖哩飯")
    try await h.turn(Scripted("", dm("我也想吃")))
    await h.doll.ownerSaid("明天早上九點開會")
    try await h.turn(Scripted("", dm("記下了")))
    await h.doll.put(Event(.watch, .internal, "盯著的區域變了：下載完成"))
    try await h.turn(Scripted("", dm("下載好了")))
    let events = await h.doll.timelineEvents()
    func ts(_ text: String) -> String { hhmm(events.first { $0.text.contains(text) }!.ts) }

    // By words: shared trigrams.
    let words = try await h.doll.recall("郵局寄包裹", seen: events).text
    #expect(words.hasPrefix("郵局寄包裹："))
    #expect(words.contains("〔今天 \(ts("郵局寄包裹"))｜主人說〕\n今天下午要去郵局寄包裹給阿嬤"))
    let reply = events.first { $0.kind == .doll }!
    #expect(try await h.doll.recall("提醒主人帶身分證", seen: events).text.contains("〔今天 \(hhmm(reply.ts))｜你想〕\n要記得提醒主人帶身分證"))
    #expect(try await h.doll.recall("出門前提醒", seen: events).text.contains("〔今天 \(hhmm(reply.ts))｜你說｜Discord 私訊〕\n好，出門前我提醒你"))
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
    try await h.turn(Scripted("", dm("好")))
    await h.doll.ownerSaid("今天天氣很好")
    try await h.turn(Scripted("", dm("對啊")))
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
        try await h.turn(Scripted("想一下", dm(answer)))
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

@Test func theTodayIndexLeavesOutRecallResults() {
    func result(_ tool: String) -> [String] {
        var ev = Event(.tool, .internal, "todo 列好了。現在進行中：「存好檔」；後面：（沒有）。")
        ev.seq = 7
        return TodayIndex.entries(ev, tool: tool).map(\.label)
    }
    #expect(result("recall").isEmpty)
    #expect(result("read_file") == ["read_file 的結果"] && result("todo.set") == ["todo.set 的結果"])
}

@Test func anItemInProgressReplyWithoutACallIsRemindedUntilTheIdleLimit() async throws {
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

@Test func aPersonsLineLeftUnansweredByACallLessReplyIsToldOnce() async throws {
    // 2026-11-18 (m7-mac tools k-2): no item in progress, the owner's request got one empty reply and nothing else.
    let h = try await Harness()
    await h.doll.ownerSaid("幫我用計算機算 58×13，跟我說答案")
    try await h.turn("")
    #expect(await h.doll.followUp && h.last.actions == ["reminded unanswered"])
    #expect(try h.events().last?.text == Doll.unansweredReminder(.owner))
    #expect(h.logs.contains { $0.contains("unanswered: reminded") })
    // Once per line: a second call-less reply to the same line only logs, and nothing wakes her.
    try await h.turn("嗯")
    #expect(!(await h.doll.followUp) && h.last.actions.isEmpty)
    #expect(h.logs.contains { $0.contains("still unanswered after the reminder") })
    #expect(try h.events().filter { $0.text == Doll.unansweredReminder(.owner) }.count == 1)
    // Her next reply answers.
    try await h.turn(Scripted("", dm("754")))
    #expect(h.said == ["754"])
    // A call that is not a line to him does not answer him (m10 b-3: a recall, then nothing).
    await h.doll.ownerSaid("我回來了")
    try await h.turn(Scripted("", ("recall", [("query", "主人 咖啡")])))
    try await h.turn("")
    #expect(h.last.actions == ["reminded unanswered"])
    try await h.turn(Scripted("", dm("你回來了")))
    #expect(h.said.last == "你回來了")
    // A new line is told again.
    await h.doll.developerSaid("還在嗎")
    try await h.turn("")
    #expect(h.last.actions == ["reminded unanswered"])
    #expect(try h.events().last?.text == Doll.unansweredReminder(.developer))
}

@Test func aCallLessReplyAfterAnAnswerOrToAProgramEventIsNotToldItIsUnanswered() async throws {
    let h = try await Harness()
    // She already answered the line; the reply after her answer has no call.
    await h.doll.ownerSaid("早～")
    try await h.turn(Scripted("", dm("早安")))
    try await h.turn("")
    #expect(h.last.actions.isEmpty)
    #expect(try !h.events().contains { $0.text.hasPrefix("【提醒】") })
    // A program event, not a person's line.
    await h.doll.voiceNotice("【語音】主人離開了語音頻道。")
    try await h.turn("")
    #expect(h.last.actions.isEmpty)
    #expect(try !h.events().contains { $0.text.hasPrefix("【提醒】") })
}

@Test func aSayAfterDoneIsStillSaidAndKeptWhenTheItemIsCompacted() async throws {
    // 2026-11-11: she wrote her words after ending the work; the owner heard nothing.
    let h = try await menuHarness()
    try await h.turn(Scripted("", done("存好了"), dm("存好了喔"), key("cmd+q")))
    #expect(h.said == ["存好了喔"] && h.cua.actions.isEmpty)
    let end = Array(try h.toolResults().suffix(3))
    #expect(end[0].text.hasPrefix("完成，記下了。") && end[1].text.hasPrefix("（附上了你最後讀的視窗截圖給主人：")
            && end[2].text == "失敗：這則回覆前面已經用 todo.done 收起這一項了，後面的呼叫沒有執行；看了結果下一回合再做。")
    #expect(h.last.actions == ["todo done", "said discord_dm", "error act"])
    #expect(!(await h.doll.followUp))
    // After the item is compacted, the say stays in her context with her done.
    await h.doll.ownerSaid("謝謝")
    try await h.turn(Scripted("", dm("不客氣")))
    let last = await h.model.requests.last!.messages
    #expect(last.contains { $0.role == "assistant" && $0.toolCalls.map(\.name) == ["todo.done", "discord.dm"]
                            && $0.toolCalls[1].arguments!.contains("存好了喔") })
}

// consolidation (2026-11-11) -----------------------------------------------------

/// The config a harness runs with, for consolidating its data dir.
private func consolidationConfig(_ h: Harness) throws -> Config {
    var cfg = try exampleConfig()
    cfg.core.dataDir = h.dir.path
    cfg.memory.dir = h.dir.appending(path: "memory").path
    return cfg
}

/// An event as the timeline stores it, a minute after the one before.
private func stamped(_ kind: Event.Kind, _ text: String, _ seq: Int64, secret: Bool = false, image: String? = nil,
                     window: String? = nil) -> Event {
    var e = Event(kind, kind == .owner ? .owner : .internal, text, secret: secret, image: image, window: window)
    e.seq = seq
    e.ts = 1794337154 + Double(seq) * 60
    return e
}
private func stamped(_ reply: Scripted, _ seq: Int64) -> Event {
    var e = stamped(.doll, reply.content, seq)
    e.calls = reply.toolCalls
    return e
}

/// Every memory file under the harness's memory dir, parsed, by id.
private func memoryFilesByID(_ h: Harness) throws -> [String: (folder: String, file: MemoryFile)] {
    var out: [String: (String, MemoryFile)] = [:]
    for folder in ["secret", "own", "public"] {
        let dir = h.dir.appending(path: "memory/\(folder)")
        for name in (try? FileManager.default.contentsOfDirectory(atPath: dir.path)) ?? [] where name.hasSuffix(".md") {
            out[String(name.dropLast(3))] = (folder, try readMemory(h.dir, dir.appending(path: name)))
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
        stamped(Scripted("我先列 todo。", ("todo.set", [("items", "[\"畫面上看得到結果\"]")])), 2),
        stamped(.tool, "todo 列好了。現在進行中：「畫面上看得到結果」；後面：（沒有）。", 3),
        stamped(.memory, "【快捷鍵】TextEdit 1.21\nFile › Save As… = ⇧⌥⌘S", 4),
        stamped(Scripted("", dm("好"), key("cmd+shift+alt+s")), 5),
        stamped(.tool, "", 6),
        stamped(.tool, "1 步都做了：key cmd+shift+alt+s。", 7, image: "b.jpg", window: "TextEdit 的視窗「另存」；截圖 460×816\n- [0] AXWindow \"Save\""),
        stamped(.memory, "【相關記憶】程式用主人的話查到的過去經驗：\n〔大概｜public〕\n經驗：x", 8),
        stamped(Scripted("", recall("另存")), 9),
        stamped(.tool, "另存：1 條\n\n〔大概｜public〕\n舊的", 10),
        stamped(Scripted("", done("按 ⇧⌥⌘S 另存好了", evidence: nil)), 11),
        stamped(.tool, "完成，記下了。", 12),
        stamped(.owner, "我的密碼是 1234", 13, secret: true),
        stamped(.watch, "盯著的畫面變了。", 14, image: "d.jpg"),
    ]
    func t(_ seq: Int) -> String { hhmm(events[seq - 1].ts) }
    let tasks = Consolidation.tasks(events)
    #expect(tasks.count == 2 && tasks.allSatisfy(\.owner))
    #expect(tasks[0].text == """
        [\(t(1)) 主人] 用 TextEdit 另存一份
        [\(t(2)) 她想｜自己的話] 我先列 todo。
        [\(t(2)) 她呼叫] todo.set(items: ["畫面上看得到結果"])
        [\(t(3)) todo.set 的結果] todo 列好了。現在進行中：「畫面上看得到結果」；後面：（沒有）。
        [\(t(5)) 她說｜Discord 私訊｜自己的話] 好
        [\(t(5)) 她呼叫] act(steps: [{"key": "s", "modifiers": ["cmd", "shift", "alt"]}])
        [\(t(7)) act 的結果] 1 步都做了：key cmd+shift+alt+s。
        [\(t(9)) 她呼叫] recall(query: 另存)
        [\(t(11)) 她呼叫] todo.done(result: 按 ⇧⌥⌘S 另存好了)
        [\(t(12)) todo.done 的結果] 完成，記下了。
        """)
    #expect(tasks[0].queries == ["用 TextEdit 另存一份", "按 ⇧⌥⌘S 另存好了"])
    #expect(tasks[1].text == "[\(t(13)) 主人｜秘密] 我的密碼是 1234\n[\(t(14)) watch] 盯著的畫面變了。")
    // Nothing of the images, the window outline, the program's lines or recall's copy.
    let all = tasks.map(\.text).joined(separator: "\n")
    for gone in [".jpg", "AXWindow", "【快捷鍵】", "【相關記憶】", "另存：1 條"] {
        #expect(!all.contains(gone), "\(gone)")
    }
    // Events before the first owner line are a task of their own.
    let early = Consolidation.tasks([stamped(.watch, "醒了", 1), stamped(.owner, "嗨", 2)])
    #expect(early.map(\.owner) == [false, true] && early[0].queries.isEmpty)
    // Her own words are marked as hers (2026-11-22), and the boot notice, what the program found, is shown.
    let seeded = [stamped(.memory, Doll.bootNotice + "最後一行 log 在 11-20 17:12:51", 1),
                  stamped(Scripted("斷了一次", dm("下午 17:12 左右我這裡斷了一次")), 2)]
    let (b1, b2) = (hhmm(seeded[0].ts), hhmm(seeded[1].ts))
    #expect(Consolidation.tasks(seeded).map(\.text) == ["[\(b1) 程式的開機通知] 【開機】你剛被開機了。最後一行 log 在 11-20 17:12:51\n"
            + "[\(b2) 她想｜自己的話] 斷了一次\n[\(b2) 她說｜Discord 私訊｜自己的話] 下午 17:12 左右我這裡斷了一次"])
}

@Test func consolidationAppliesAddUpdateRetireAndTheNextStartIsANewDay() async throws {
    let h = try await Harness()
    let cfg = try consolidationConfig(h)
    let memory = try Memory(config: cfg.memory, root: h.dir.appending(path: "memory"), store: h.store, embedder: FakeEmbedder(), box: try testBox(h.dir))
    let app = try memory.registry.create(.app, name: "TextEdit", aliases: [], description: "macOS 內建的文字編輯 app")
    let saveAs = try memory.write("TextEdit 另存要從 File 選單點 Save As…", folder: .public, sourceSeq: 1, tier: .doll, kind: nil,
                                  entities: [MemoryEntity(app.id, "TextEdit")], now: Date().timeIntervalSince1970)
    let cat = try memory.write("主人養貓", folder: .public, sourceSeq: 1, tier: .owner, kind: nil, now: Date().timeIntervalSince1970)
    let shortcut = try memory.write("TextEdit：File › Save As…＝⇧⌥⌘S（TextEdit 1.21）", folder: .public, sourceSeq: 0, tier: .program, kind: nil, now: Date().timeIntervalSince1970)
    let experience = try memory.write("經驗：另存\n步驟：\n1. key cmd+shift+alt+s", folder: .public, sourceSeq: 1, tier: .selfReported,
                                      kind: nil, experience: (task: "另存", stats: ExperienceStats(turns: 3, seconds: 20)), now: Date().timeIntervalSince1970)
    try writeRetired(h, "9-old000", days: 40, body: "退休很久的")
    try writeRetired(h, "9-new000", days: 5, body: "剛退休的")
    await h.doll.ownerSaid("用 TextEdit 另存一份")
    try await h.turn(Scripted("", dm("好")))
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
    let model = FakeModel([Scripted(reply), Scripted("主人要了一份副本，你用 ⇧⌥⌘S 另存好了。")])
    let r = try await Consolidation.run(config: cfg, root: root, model: model, embedder: FakeEmbedder(), keys: testKeys, now: Date().timeIntervalSince1970)

    // The request: no persona, high effort, the day as text, only memories she or consolidation
    // wrote, with their entities by registry id, and the registry entries near the day.
    let sent = await model.requests[0]
    #expect(sent.effort == "high" && sent.messages.map(\.role) == ["system", "user"])
    #expect(!sent.messages[0].content.contains("你是") && sent.messages[1].content.contains("[id: \(cat)｜public｜kind: ｜entities: []]\n主人養貓"))
    #expect(sent.messages[1].content.contains(#"[id: \#(saveAs)｜public｜kind: ｜entities: [{"id":"\#(app.id)","name":"TextEdit"}]]"#))
    #expect(sent.messages[1].content.contains("[id: \(app.id)｜app｜TextEdit｜別名：（沒有）] macOS 內建的文字編輯 app"))
    #expect(sent.messages[1].content.contains("] 用 TextEdit 另存一份") && sent.messages[1].content.contains("她說｜Discord 私訊｜自己的話] 好"))
    #expect(sent.messages[0].content.contains("- " + Consolidation.ownWords))
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

    // The summary request: clean (the instructions, her own words' rule and the day's text), asked before anything was applied.
    let asked = await model.seen(1)
    #expect(asked.count == 2 && asked[0].content == Doll.talkInstructions + Consolidation.summaryOwnWords && asked[1].content.contains("主人"))
    // The next start is a new segment whose one event is the day's summary (2026-11-15: sleep does not
    // empty her context, the day before is there as a summary); long-term memory stays.
    let next = try await restarted(h)
    #expect(try h.timeline().segment == r.newSegment)   // `next` runs on h's data dir
    let first = try #require(await next.doll.timelineEvents().map(\.text).first)
    #expect(await next.doll.timelineEvents().count == 1 && first.hasPrefix("【系統維護】（離線整理）整理了之前的事：")
            && first.hasSuffix("\n" + Consolidation.summaryNote + "主人要了一份副本，你用 ⇧⌥⌘S 另存好了。"))
    #expect(await next.doll.timelineEvents().first?.kind == .memory)
    #expect(FileManager.default.fileExists(atPath: h.dir.appending(path: "timeline/\(before).jsonl").path))
}

@Test func aFailedOrMalformedConsolidationWritesNothingAndKeepsTheDay() async throws {
    let h = try await Harness()
    let cfg = try consolidationConfig(h)
    // Nothing in the timeline yet: nothing to consolidate.
    await #expect(throws: ConsolidationError.self) {
        try await Consolidation.run(config: cfg, root: root, model: FakeModel([]), embedder: FakeEmbedder(), keys: testKeys, now: Date().timeIntervalSince1970)
    }
    let memory = try Memory(config: cfg.memory, root: h.dir.appending(path: "memory"), store: h.store, embedder: FakeEmbedder(), box: try testBox(h.dir))
    let cat = try memory.write("主人養貓", folder: .public, sourceSeq: 1, tier: .owner, kind: nil, now: Date().timeIntervalSince1970)
    let shortcut = try memory.write("TextEdit：File › Save＝⌘S（TextEdit 1.21）", folder: .public, sourceSeq: 0, tier: .program, kind: nil, now: Date().timeIntervalSince1970)
    try writeRetired(h, "9-old000", days: 40, body: "退休很久的")
    await h.doll.ownerSaid("主人的貓叫什麼")
    try await h.turn(Scripted("", dm("不知道")))
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
            try await Consolidation.run(config: cfg, root: root, model: FakeModel([Scripted(reply)]), embedder: FakeEmbedder(), keys: testKeys, now: Date().timeIntervalSince1970)
        }
    }
    // The model failing is an error too.
    await #expect(throws: ModelError.self) {
        try await Consolidation.run(config: cfg, root: root, model: FakeModel([]), embedder: FakeEmbedder(), keys: testKeys, now: Date().timeIntervalSince1970)
    }
    // So is the summary request failing or coming back empty: a valid plan is not applied without it.
    await #expect(throws: ModelError.self) {
        try await Consolidation.run(config: cfg, root: root, model: FakeModel([Scripted(plan(add))]), embedder: FakeEmbedder(), keys: testKeys, now: Date().timeIntervalSince1970)
    }
    await #expect(throws: ConsolidationError.self) {
        try await Consolidation.run(config: cfg, root: root, model: FakeModel([Scripted(plan(add)), Scripted("")]), embedder: FakeEmbedder(), keys: testKeys, now: Date().timeIntervalSince1970)
    }
    #expect(try memoryFiles(h) == files)
    #expect(try FileManager.default.contentsOfDirectory(atPath: h.dir.appending(path: "timeline").path).sorted() == segments)
    #expect(try h.events().contains { $0.text == "主人的貓叫什麼" })
    // The same reply, valid: applied.
    let ok = try await Consolidation.run(config: cfg, root: root, model: FakeModel([Scripted(plan(add)), Scripted("今天的摘要。")]), embedder: FakeEmbedder(), keys: testKeys, now: Date().timeIntervalSince1970)
    #expect(ok.applied.count == 3 && ok.applied[1].hasPrefix("entity ") && ok.applied[2].hasSuffix("主人的貓叫咪咪"))
}

@Test func consolidatedMemoriesAreAttachedAndRetiredOnesAreNot() async throws {
    let h = try await Harness()
    try writeRetired(h, "1-retire", days: 1, body: "TextEdit 另存要從 File 選單點 Save As…")
    try await seedMemory(h, "public", "TextEdit 按 ⇧⌥⌘S → 跳出另存對話框，檔名欄已選取", tier: "consolidated", confidence: 0.6)
    await h.doll.ownerSaid("用 TextEdit 另存一份")
    try await h.turn("好")
    #expect(try memoryEvents(h).map { noIDs($0.text) } == ["【相關記憶】程式用主人的話查到、最相近的過去經驗：\n〔大概｜public〕\nTextEdit 按 ⇧⌥⌘S → 跳出另存對話框，檔名欄已選取"])
    try await h.turn(Scripted("", recall("另存對話框")))
    let result = try h.toolResults().last!.text
    #expect(result.contains("跳出另存對話框") && !result.contains("File 選單點"))
}

@Test func consolidateConfigIsValidated() throws {
    let c = try exampleConfig().consolidate
    #expect(c.effort == "high" && c.retiredKeepDays == 30 && c.recallK >= 1 && c.registryK >= 1)
    let file = FileManager.default.temporaryDirectory.appending(path: "consolidate-\(UUID().uuidString).toml")
    let head = "[llm]\nbase_url = \"http://x\"\nmodel = \"m\"\n[memory]\n"
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
    let memory = try Memory(config: cfg.memory, root: h.dir.appending(path: "memory"), store: h.store, embedder: FakeEmbedder(), box: try testBox(h.dir))
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
    let id = try memory.write("TextEdit 打開 notes.txt", folder: .public, sourceSeq: 3, tier: .doll, kind: nil, entities: ents, now: Date().timeIntervalSince1970)
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
        try memory.write("x", folder: .public, sourceSeq: 3, tier: .doll, kind: nil, entities: [MemoryEntity("e404", "x")], now: Date().timeIntervalSince1970)
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

/// TextEdit as an experience names it.
private let textEdit = AppIdentity(bundleID: "com.apple.TextEdit", name: "TextEdit", version: "1.21")
/// Opens TextEdit (the fake driver's app without a window): the front app of her results becomes TextEdit.
private let openTextEdit: (String, [(String, String)]) = ("open", [("app", "TextEdit")])

@Test func theProgramsEntriesAreExactAndAnAppNeverResolvesToAnotherType() async throws {
    let h = try await Harness()
    let cfg = try consolidationConfig(h)
    let memory = try Memory(config: cfg.memory, root: h.dir.appending(path: "memory"), store: h.store, embedder: FakeEmbedder(), box: try testBox(h.dir))
    let sel = Selector(memory: memory, config: cfg.select)
    let registry = memory.registry
    // The wrong merge the on-device model made in real runs (experiments/m6-todo-compact): TextEdit
    // aligned to the shortcut entry ⇧⌘T, which then had the app's names as aliases.
    let bad = try registry.create(.shortcut, name: "⇧⌘T", aliases: ["文字編輯", "com.apple.TextEdit"], description: "鍵盤快捷鍵 ⇧⌘T")
    let localTextEdit = AppIdentity(bundleID: "com.apple.TextEdit", name: "文字編輯", version: "1.21")
    #expect(try sel.appEntry(localTextEdit) == nil)
    // The app is never that entry: a new app entry, aliased by its bundle id; the bad one unchanged.
    let made = try sel.programApp(localTextEdit)
    #expect(made.how == "new" && made.entry.id != bad.id && made.entry.type == .app)
    #expect(made.entry.name == "文字編輯" && made.entry.aliases == ["com.apple.TextEdit"])
    #expect(try registry.entry(bad.id) == bad)
    // From then on that entry, by its bundle id; another name of it becomes an alias.
    #expect(try sel.appEntry(localTextEdit)?.id == made.entry.id)
    let english = try sel.programApp(textEdit)
    #expect(english.entry.id == made.entry.id && english.how == "bundle id" && english.entry.aliases == ["com.apple.TextEdit", "TextEdit"])
    // The shortcut side still finds the shortcut by its chord.
    #expect(try sel.programShortcut("⇧⌘T").id == bad.id)

    // An app entry with no bundle id (consolidation writes such): found by its exact name, the
    // bundle id added.
    let finder = try registry.create(.app, name: "Finder", aliases: [], description: "macOS 內建的檔案管理 app")
    let byName = try sel.programApp(AppIdentity(bundleID: "com.apple.finder", name: "Finder", version: "15"))
    #expect(byName.entry.id == finder.id && byName.how == "name" && byName.entry.aliases == ["com.apple.finder"])
    // No lookup by meaning or spelling: a similar name is no match.
    let safari = try sel.programApp(AppIdentity(bundleID: "com.apple.Safari", name: "Safari", version: "18"))
    #expect(safari.how == "new" && safari.entry.id != finder.id && safari.entry.description.contains("com.apple.Safari"))
    #expect(try registry.entry(finder.id)?.aliases == ["com.apple.finder"])

    // Several entries of the type for one name: the oldest (created_ts, then id), every time.
    let first = try registry.create(.app, name: "Notes", aliases: [], description: "備忘錄")
    let second = try registry.create(.app, name: "Notes", aliases: [], description: "另一個 Notes")
    let oldest = [first, second].min { $0.ts != $1.ts ? $0.ts < $1.ts : $0.id < $1.id }!
    #expect(try sel.programApp(AppIdentity(bundleID: "com.apple.Notes", name: "Notes", version: "4")).entry.id == oldest.id)
    #expect(try sel.appEntry(AppIdentity(bundleID: "com.apple.Notes", name: "Notes", version: "4"))?.id == oldest.id)

    // Shortcuts: by the chord, of type shortcut only; none, a new one; the same one after that.
    let appNamedLikeAChord = try registry.create(.app, name: "⌘P", aliases: [], description: "測試用：名字像快捷鍵的 app")
    let print = try sel.programShortcut("⌘P")
    #expect(print.id != appNamedLikeAChord.id && print.type == .shortcut && print.name == "⌘P")
    #expect(try sel.programShortcut("⌘P").id == print.id)
}

/// A registry entry in the harness's memory (the next `seedMemory` re-indexes it).
private func entry(_ h: Harness, _ type: EntityType, _ name: String, _ description: String,
                   aliases: [String] = []) throws -> RegistryEntry {
    let cfg = try consolidationConfig(h)
    return try Memory(config: cfg.memory, root: h.dir.appending(path: "memory"), store: h.store, embedder: FakeEmbedder(), box: try testBox(h.dir)).registry
        .create(type, name: name, aliases: aliases, description: description)
}

@Test func noNameIsExtractedAndTheFrontAppsEntryIsTheOnlyTriggerEntity() async throws {
    // No embedding channel here (similarity 1): only FTS, the front app's entry and the hop. None
    // reaches the attach floor (cosine at least `similarity`), so none is attached.
    let h = try await Harness([Scripted("", todoSet(), openTextEdit)], tune: { $0.select.similarity = 1.0; $0.select.entityWeight = 1 })
    let app = try entry(h, .app, "TextEdit", "macOS 內建的文字編輯 app", aliases: ["com.apple.TextEdit"])
    // The wrong merge of real runs: a shortcut entry with the app's names as aliases.
    let bad = try entry(h, .shortcut, "⇧⌘T", "鍵盤快捷鍵 ⇧⌘T", aliases: ["文字編輯", "com.apple.TextEdit"])
    let keys = try entry(h, .shortcut, "⇧⌥⌘S", "TextEdit 的另存快捷鍵")
    let safari = try entry(h, .app, "Safari", "macOS 的瀏覽器")
    // No word of the owner's line; names TextEdit, which the line calls 文字編輯.
    try await seedMemory(h, "public", "開檔對話框裡按 ⇧⌘G 可以直接輸入路徑", tier: "consolidated", confidence: 0.6,
                         entities: [MemoryEntity(app.id, "TextEdit")], id: "1-ent")
    try await seedMemory(h, "public", "新視窗按 ⇧⌘T", tier: "consolidated", confidence: 0.6,
                         entities: [MemoryEntity(bad.id, "⇧⌘T")], id: "1-bad")
    // An FTS hit (另存一份).
    try await seedMemory(h, "public", "另存一份要按 ⇧⌥⌘S", tier: "consolidated", confidence: 0.6,
                         entities: [MemoryEntity(keys.id, "⇧⌥⌘S")], id: "2-seed")
    // Shares only the shortcut with the FTS hit: one hop.
    try await seedMemory(h, "public", "⇧⌥⌘S 之後檔名欄已選取", tier: "consolidated", confidence: 0.6,
                         entities: [MemoryEntity(keys.id, "⇧⌥⌘S")], id: "3-hop")
    // Related to nothing.
    try await seedMemory(h, "public", "Safari 書籤在側邊欄", tier: "consolidated", confidence: 0.6,
                         entities: [MemoryEntity(safari.id, "Safari")], id: "4-none")
    await h.doll.ownerSaid("用文字編輯另存一份")
    try await h.turn()
    let reports = h.last.selections
    try #require(reports.count == 2)
    // The owner's line: no front app yet, so no entity; 文字編輯 in the line links nothing.
    #expect(reports[0].contains("（主人的新訊息）entities [];") && reports[0].contains("related 2 (fts 1, embedding 0, entity 0, hop 1), prepared 2;"))
    #expect(!reports[0].contains("1-ent") && !reports[0].contains("1-bad"))
    // The front app TextEdit: its app entry (by bundle id), never the shortcut entry aliased by it.
    #expect(reports[1].contains("（前景 app 換成 TextEdit）entities [TextEdit→\(app.id)「TextEdit」];"))
    #expect(reports[1].contains("related 3 (fts 1, embedding 0, entity 1, hop 1)") && reports[1].contains("ranked [1-ent ") && !reports[1].contains("1-bad"))
    #expect(reports.allSatisfy { $0.contains("given 0 []") })
    #expect(try memoryEvents(h).isEmpty)
}

@Test func attachGivesAtMostAttachMaxAboveTheCosineFloorBestCheapScoreFirst() async throws {
    // entity_weight 1: the two memories having the front app's entry come first among those above the floor.
    let h = try await Harness()
    let app = try entry(h, .app, "TextEdit", "macOS 內建的文字編輯 app", aliases: ["com.apple.TextEdit"])
    for i in 1...4 {
        try await seedMemory(h, "public", "TextEdit 另存第 \(i) 種方法", tier: "consolidated", confidence: 0.6,
                             entities: i >= 3 ? [MemoryEntity(app.id, "TextEdit")] : [], id: "\(i)-m")
    }
    // Tied to the trigger by the entity only: its cheap score is the highest, its cosine under the floor.
    try await seedMemory(h, "public", "咪咪喜歡曬太陽", tier: "consolidated", confidence: 0.6,
                         entities: [MemoryEntity(app.id, "TextEdit")], id: "5-ent")
    let cfg = try consolidationConfig(h)
    var select = cfg.select
    (select.similarity, select.attachMax, select.entityWeight) = (0.2, 3, 1)
    let memory = try Memory(config: cfg.memory, root: h.dir.appending(path: "memory"), store: h.store, embedder: FakeEmbedder(), box: try testBox(h.dir))
    let report = try Selector(memory: memory, config: select).run(
        .init(label: "測試", query: "TextEdit 另存", app: textEdit), rule: .attach, visible: [.public],
        tiers: [.selfReported, .consolidated], today: nil, now: Date().timeIntervalSince1970) { $0.key }
    #expect(report.entities == ["TextEdit→\(app.id)「TextEdit」"] && report.related == 5)
    let given = report.selected.map(\.id)
    #expect(given.count == 3 && Set(given.prefix(2)) == ["3-m", "4-m"] && ["1-m", "2-m"].contains(given[2]))
}

@Test func recallReturnsTheRecallLimitBestWithNoFloorTodayIncluded() async throws {
    let h = try await Harness(tune: { $0.select.recallLimit = 2 })
    try await seedMemory(h, "public", "主人的貓叫咪咪", tier: "owner", confidence: 0.9)
    try await seedMemory(h, "public", "主人的狗叫旺財", tier: "owner", confidence: 0.9)
    try await seedMemory(h, "public", "主人喜歡藍色", tier: "owner", confidence: 0.9)
    await h.doll.ownerSaid("我今天幫貓買了新的貓砂")
    try await h.turn(Scripted("", dm("好")))
    // The line she is answering is not a result (2026-11-15: the question came back as her first hit).
    await h.doll.ownerSaid("貓的事你記得什麼")
    try await h.turn(Scripted("", recall("貓")))
    let text = try h.toolResults().last!.text
    #expect(text.hasPrefix("貓：2 條") && text.contains("主人的貓叫咪咪") && text.contains("〔今天") && text.contains("貓砂")
            && !text.contains("旺財") && !text.contains("貓的事你記得什麼"))
    #expect(h.last.selections.last?.contains("given 2 [") == true && h.last.actions.contains("recall 2"))
    // Nothing near: still the two nearest (no floor; she judges).
    try await h.turn(Scripted("", recall("火星殖民計畫")))
    #expect(try h.toolResults().last!.text.hasPrefix("火星殖民計畫：2 條"))
}

@Test func twoCharacterWordsAreFoundInMemoriesAndToday() async throws {
    #expect(Memory.shortTerms("過敏 花生，貓 27 吋 ab abc 過年回去", maxTerms: 10) == ["過敏", "花生", "27", "ab"])
    // recall_limit 1 and a look-alike that the embedding prefers: the text match decides.
    let h = try await Harness(tune: { $0.select.recallLimit = 1 })
    try await seedMemory(h, "public", "我對花生過敏，吃到會起疹子", tier: "owner", confidence: 0.9)
    try await seedMemory(h, "public", "花園裡的生菜長得很好，過幾天敏敏會來", tier: "owner", confidence: 0.9)
    await h.doll.ownerSaid("我媽打電話來，問我今年過年要不要回台南")
    try await h.turn(Scripted("", dm("好")))
    try await h.turn(Scripted("", recall("花生 過敏")))
    #expect(noIDs(try h.toolResults().last!.text) == "花生 過敏：1 條\n\n〔確定｜public〕\n我對花生過敏，吃到會起疹子")
    #expect(h.last.selections.first?.contains("fts 1,") == true)
    await h.doll.ownerSaid("你查查看")
    try await h.turn(Scripted("", recall("過年")))
    let today = try h.toolResults().last!.text
    #expect(today.hasPrefix("過年：1 條") && today.contains("〔今天") && today.contains("過年要不要回台南"))
    #expect(h.last.selections.last?.contains("fts 1,") == true)
}

@Test func bootNeedsNoOnDeviceModel() async throws {
    let h = try await Harness()
    try await h.doll.boot()
    #expect(!h.logs.contains { $0.hasPrefix("ERROR") })
}

@Test func rememberWritesNoEntitiesAndNoRegistryEntries() async throws {
    let h = try await Harness([Scripted("", remember([("主人的媽媽林美玲用 TextEdit 寫食譜", false)]))])
    await h.doll.ownerSaid("我媽用 TextEdit 寫食譜")
    try await h.turn()
    let files = try memoryFilesByID(h).values.map(\.file)
    let mine = try #require(files.first { $0.body.contains("林美玲") })
    #expect(mine.entities.isEmpty)
    let cfg = try consolidationConfig(h)
    #expect(try Memory(config: cfg.memory, root: h.dir.appending(path: "memory"), store: h.store, embedder: FakeEmbedder(), box: try testBox(h.dir)).registry.all().isEmpty)
}

@Test func programWrittenMemoriesCarryWhatTheProgramKnows() async throws {
    let h = try await Harness([Scripted("", todoSet(), openTextEdit), Scripted("", dm("開好了"), done("開好了", evidence: "TextEdit 開著，但沒有視窗"))])
    await h.doll.ownerSaid("開 TextEdit")
    try await h.turn()
    try await h.turn()
    let cfg = try consolidationConfig(h)
    let registry = try Memory(config: cfg.memory, root: h.dir.appending(path: "memory"), store: h.store,
                              embedder: FakeEmbedder(), box: try testBox(h.dir)).registry
    let app = try #require(try registry.exact("com.apple.TextEdit").first)
    #expect(app.name == "TextEdit" && app.aliases == ["com.apple.TextEdit"] && app.type == .app)
    let files = try memoryFilesByID(h).values.map(\.file)
    let experience = try #require(files.first { $0.tier == .selfReported })
    #expect(experience.entities == [MemoryEntity(app.id, "TextEdit")])
}

@Test func selectConfigIsValidated() throws {
    let s = try exampleConfig().select
    #expect(s.similarity == 0.55 && s.attachMax == 5 && s.recallLimit == 3 && s.recallCandidates == 5)
    let file = FileManager.default.temporaryDirectory.appending(path: "select-\(UUID().uuidString).toml")
    let head = "[llm]\nbase_url = \"http://x\"\nmodel = \"m\"\n[memory]\n"
        + SourceTier.allCases.map { "confidence_\($0.rawValue) = 0.5\n" }.joined()
    for bad in ["similarity = 1.5", "entity_weight = -0.1", "attach_max = 0", "recall_limit = 0", "lookup_k = 5",
                "align_threshold = 7", "prefilter = 8", "threshold = 7", "candidate_max_chars = 1500", "seed_k = 10", "bogus = 1"] {
        try (head + "[select]\n\(bad)\n").write(to: file, atomically: true, encoding: .utf8)
        #expect(throws: TOMLError.self, "\(bad)") { try Config.load(file) }
    }
    try (head + "auto_attach_k = 5\n").write(to: file, atomically: true, encoding: .utf8)
    #expect(throws: TOMLError.self) { try Config.load(file) }
}

// developer, say routing, local API, bridge (2026-11-12) ------------------------------------

@Test func developerLinesRenderAsDeveloperAndNeverSeeTheSecretFolder() async throws {
    #expect(Memory.visible(.developer, sawSecret: true) == [.public, .own])
    let h = try await Harness()
    await h.doll.ownerSaid("記一下：我的銀行密碼是 bank4321", secret: true)
    try await h.turn(Scripted("", remember(folders: [("主人的銀行密碼是 bank4321", "secret", nil)])))
    // The owner, in a turn that saw a secret: the secret folder is visible.
    await h.doll.ownerSaid("銀行密碼是什麼")
    try await h.turn(Scripted("", recall("銀行密碼")))
    #expect(try h.toolResults().last!.text.contains("bank4321"))
    // The developer: 開發者 in her prompt, never 主人; the same question finds nothing secret, neither
    // the secret memory nor the owner's secret line from today.
    await h.doll.developerSaid("開發者想知道銀行密碼")
    try await h.turn(Scripted("", recall("銀行密碼")))
    let prompt = await h.model.seen(2).map(\.content)
    #expect(prompt.contains { $0.hasPrefix("[") && $0.hasSuffix(" 開發者｜終端機｜文字] 開發者想知道銀行密碼") })
    #expect(!prompt.contains { $0.contains("主人] 開發者想知道") })
    #expect(Doll.audience(try h.events()) == .developer)
    #expect(!(try h.toolResults().last!.text.contains("bank4321")))
    // Her work after the developer's line (a watch wake) is still the developer's task.
    await h.doll.put(Event(.watch, .internal, "盯著的整個畫面變了。"))
    try await h.turn(Scripted("", recall("銀行密碼")))
    #expect(!(try h.toolResults().last!.text.contains("bank4321")))
    // The timeline file keeps who spoke.
    #expect(try h.events().filter { $0.kind == .developer }.map(\.principal) == [.developer])
}

@Test func eachSpeakingToolGoesToItsChannelAndTheTimelineRecordsIt() async throws {
    #expect(Doll.lastSpeaker([]) == .owner)   // nobody has spoken yet: the owner
    let h = try await Harness()
    await h.doll.ownerSaid("在嗎")
    try await h.turn(Scripted("", dm("在")))
    await h.doll.developerSaid("測試一下")
    try await h.turn(Scripted("", reply("收到")))
    await h.doll.put(Event(.watch, .internal, "變了"))   // a wake in the developer's task: she still picks the channel
    try await h.turn(Scripted("", reply("畫面變了"), dm("主人，畫面變了")))
    #expect(h.said == ["在", "收到", "畫面變了", "主人，畫面變了"])
    #expect(h.saidTo == [.owner, .developer, .developer, .owner])
    #expect(h.reports.map(\.actions) == [["said discord_dm"], ["said dollctl"], ["said dollctl", "said discord_dm"]])
    // Recorded with each result, as a new process reads the file.
    #expect(try h.toolResults().map(\.channel) == [.discordDM, .dollctl, .dollctl, .discordDM])
    // The dashboard shows the developer's line and where she said each thing.
    let items = try await h.doll.dashboardTimeline(limit: 50)
    #expect(items.contains { $0.type == "developer" && $0.text == "測試一下" })
    #expect(Array(items.compactMap(\.calls).reversed()).flatMap { $0.map(\.channel) } == ["Discord 私訊", "終端機", "終端機", "Discord 私訊"])
    // The today index keeps what she said per channel.
    let both = try #require(try h.events().last { $0.kind == .doll })
    #expect(TodayIndex.entries(both, tool: nil).map(\.label) == ["你說｜Discord 私訊", "你說｜終端機"])
    #expect(TodayIndex.entries(both, tool: nil).map(\.text) == ["主人，畫面變了", "畫面變了"])
    // Consolidation reads who heard it.
    let day = Consolidation.tasks(try h.events()).map(\.text).joined(separator: "\n")
    #expect(day.contains("她說｜Discord 私訊｜自己的話] 在") && day.contains("她說｜終端機｜自己的話] 收到"))
}

@Test func theSpeakingToolsAreRefusedWithAReasonWhenTheirChannelIsNotThere() async throws {
    let h = try await Harness()
    // No dollctl chat open: the developer did not get it; nothing wakes her for it.
    h.rec.undelivered.withLock { $0 = "沒有開著的 dollctl chat" }
    await h.doll.developerSaid("還在嗎")
    try await h.turn(Scripted("", reply("在")))
    #expect(try h.toolResults().last!.text == "失敗：開發者沒收到：沒有開著的 dollctl chat。開發者開著 dollctl chat 才收得到 dollctl.reply；要跟主人說話用 discord.dm。")
    #expect(h.last.actions == ["error dollctl.reply"])
    #expect(try h.toolResults().last!.channel == nil)
    #expect(await h.doll.followUp == false)
    // The bridge is down: the owner did not get it.
    h.rec.undelivered.withLock { $0 = "Discord 送不出去：bridge down: exit status 1" }
    await h.doll.ownerSaid("在嗎")
    try await h.turn(Scripted("", dm("在")))
    #expect(try h.toolResults().last!.text == "失敗：主人沒收到：Discord 送不出去：bridge down: exit status 1。這段話主人沒看到；Discord 連上之前 discord.dm 都傳不出去，連上以後再傳一次。")
    #expect(h.last.actions == ["error discord.dm"])
    #expect(await h.doll.followUp == false)
    #expect(h.logs.contains { $0.hasPrefix("ERROR") && $0.contains("discord.dm not delivered") })
    // The voice tools do not exist without `[voice] enabled`, and the removed names are unknown tools.
    h.rec.undelivered.withLock { $0 = nil }
    try await h.turn(Scripted("", voiceSay("在"), ("say", [("text", "在")]), ("send_text", [("text", "在")]), ("join_voice", []), ("leave_voice", [])))
    #expect(try h.toolResults().suffix(5).map(\.text) == ["voice.say", "say", "send_text", "join_voice", "leave_voice"].map { "失敗：沒有 \($0) 這個工具" })
    #expect(h.said == ["在", "在"])   // only the two attempts above reached the delivery
}

@Test func theBootNoticeIsInTheTimelineAndTakesNoTurnOfItsOwn() async throws {
    let h = try await Harness()
    try await h.doll.announceBoot()
    #expect(await h.doll.queue.isEmpty)                    // nothing wakes her
    #expect(await h.model.requests.isEmpty)
    await h.doll.ownerSaid("在嗎")
    try await h.turn(Scripted("", dm("在")))
    // She reads it with the owner's line, before it.
    let seen = await h.model.seen(0).map(\.content)
    let boot = try #require(seen.firstIndex { $0.contains("程式] 【開機】你剛被開機了。") })
    let line = try #require(seen.firstIndex { $0.contains("在嗎") })
    #expect(boot < line)
}

@Test func apiAndBridgeConfigAreValidated() throws {
    let example = try exampleConfig()
    #expect(example.api.port == 8791 && !example.bridge.enabled && example.bridge.command.first?.hasPrefix("/") == true)
    #expect(example.core.priority == ["owner_voice", "owner_text", "developer_text", "calendar", "watch", "wander"])
    let file = FileManager.default.temporaryDirectory.appending(path: "api-\(UUID().uuidString).toml")
    let head = "[llm]\nbase_url = \"http://x\"\nmodel = \"m\"\n[memory]\n"
        + SourceTier.allCases.map { "confidence_\($0.rawValue) = 0.5\n" }.joined()
    try head.write(to: file, atomically: true, encoding: .utf8)
    #expect(try Config.load(file).api.port == 8791)
    for bad in ["[api]\nport = 8790\n",                                                   // the dashboard's
                "[api]\nport = 0\n",
                "[bridge]\nenabled = true\ncommand = [\"uv\", \"run\"]\n",                // not absolute
                "[bridge]\nenabled = true\n",                                          // no command
                "[bridge]\nready_timeout_s = 0\n",
                "[bridge]\ntoken = \"x\"\n"] {                                         // secrets never live here
        try (head + bad).write(to: file, atomically: true, encoding: .utf8)
        #expect(throws: TOMLError.self, "\(bad)") { try Config.load(file) }
    }
    try (head + "[core]\npriority = [\"owner_voice\", \"owner_text\", \"calendar\", \"watch\"]\n").write(to: file, atomically: true, encoding: .utf8)
    #expect(throws: TOMLError.self) { try Config.load(file) }   // developer_text must be placed
    // So must calendar (2026-11-15): a config from before it says so instead of guessing its place.
    try (head + "[core]\npriority = [\"owner_voice\", \"owner_text\", \"developer_text\", \"watch\", \"wander\"]\n").write(to: file, atomically: true, encoding: .utf8)
    #expect(throws: TOMLError.self) { try Config.load(file) }
    // And wander (m6-drives): a config from before it must place it.
    try (head + "[core]\npriority = [\"owner_voice\", \"owner_text\", \"developer_text\", \"calendar\", \"watch\"]\n").write(to: file, atomically: true, encoding: .utf8)
    #expect(throws: TOMLError.self) { try Config.load(file) }
}

/// A started local API on a free port over the harness's doll.
private func localAPI(_ h: Harness) async throws -> LocalAPI {
    var c = ApiConfig()
    c.port = 0
    let api = LocalAPI(config: c)
    _ = try await api.start(doll: h.doll)
    return api
}

private func post(_ api: LocalAPI, _ path: String, _ text: String, key: String? = nil) async throws -> Int {
    var req = URLRequest(url: URL(string: api.baseURL + path)!)
    req.httpMethod = "POST"
    req.httpBody = try JSONSerialization.data(withJSONObject: ["text": text])
    if let key { req.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization") }
    return (try await URLSession.shared.data(for: req).1 as! HTTPURLResponse).statusCode
}

/// This machine's first non-loopback IPv4 address, if it has one.
private func otherIPv4() -> String? {
    var list: UnsafeMutablePointer<ifaddrs>?
    guard getifaddrs(&list) == 0, let first = list else { return nil }
    defer { freeifaddrs(list) }
    for p in sequence(first: first, next: { $0.pointee.ifa_next }) {
        guard let sa = p.pointee.ifa_addr, sa.pointee.sa_family == UInt8(AF_INET) else { continue }
        var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
        guard getnameinfo(sa, socklen_t(sa.pointee.sa_len), &host, socklen_t(host.count), nil, 0, NI_NUMERICHOST) == 0 else { continue }
        let s = String(decoding: host.prefix { $0 != 0 }.map { UInt8(bitPattern: $0) }, as: UTF8.self)
        if !s.hasPrefix("127.") { return s }
    }
    return nil
}

@Test func localAPIIsLoopbackOnlyAndOnlyTheBridgeSpeaksAsTheOwner() async throws {
    #expect(LocalAPI.isLoopback(.hostPort(host: "127.0.0.1", port: 1)))
    #expect(LocalAPI.isLoopback(.hostPort(host: "::1", port: 1)))
    #expect(!LocalAPI.isLoopback(.hostPort(host: "192.168.1.20", port: 1)))
    #expect(!LocalAPI.isLoopback(.hostPort(host: "192.0.2.11", port: 1)))
    let h = try await Harness(["否"])   // the owner line's secret check
    let api = try await localAPI(h)
    defer { api.stop() }
    // Bound to 127.0.0.1 only: the machine's other address does not answer.
    if let ip = otherIPv4() {
        var req = URLRequest(url: URL(string: "http://\(ip):\(api.port)/api/status")!)
        req.timeoutInterval = 3
        await #expect(throws: (any Error).self) { _ = try await URLSession.shared.data(for: req) }
    }
    #expect(try await post(api, "/api/developer", "嗨") == 200)
    #expect(try await post(api, "/api/developer", "  ") == 400)
    #expect(try await post(api, "/api/owner", "我是主人") == 401)
    // A key differing in its first character (the real key starts with "0" one time in sixteen:
    // "0" + dropFirst() was then the real key, and this test failed that often).
    #expect(try await post(api, "/api/owner", "我是主人", key: (api.ownerKey.hasPrefix("0") ? "1" : "0") + api.ownerKey.dropFirst()) == 401)
    #expect(try await post(api, "/api/owner", "主人真的來了", key: api.ownerKey) == 200)
    // The owner's line was held for the secret check (one request, no tools), then queued as an ordinary line.
    await h.doll.lineChecks?.value
    #expect(await h.model.requests.map { $0.messages.map(\.content) } == [[SecretCheck.instruction, "主人真的來了"]])
    #expect(await h.doll.queue.map(\.kind) == [.developer, .owner])
    #expect(await h.doll.queue.map(\.text) == ["嗨", "主人真的來了"])
    #expect(await h.doll.queue.map(\.secret) == [false, false])
    // The owner stream needs the key too.
    let (_, refused) = try await URLSession.shared.data(from: URL(string: api.baseURL + "/api/listen?to=owner")!)
    #expect((refused as! HTTPURLResponse).statusCode == 401)
    // Nobody listening: delivery says why.
    #expect(api.deliver("x", to: .developer) == "沒有開著的 dollctl chat")
    #expect(api.deliver("x", to: .owner)!.contains("disabled"))
    let (data, _) = try await URLSession.shared.data(from: URL(string: api.baseURL + "/api/status")!)
    let dec = JSONDecoder()
    dec.keyDecodingStrategy = .convertFromSnakeCase
    let status = try dec.decode(ApiStatus.self, from: data)
    #expect(status.queued == 2 && status.state == "idle" && status.footer.hasPrefix("【狀況】") && status.developerStreams == 0)
}

@Test func aDeveloperStreamGetsHerSayAndTypingWhileHerTurnRuns() async throws {
    let h = try await Harness(["好"])
    let api = try await localAPI(h)
    defer { api.stop() }
    var req = URLRequest(url: URL(string: api.baseURL + "/api/listen?to=developer")!)
    req.timeoutInterval = 10
    let (bytes, response) = try await URLSession.shared.bytes(for: req)
    #expect((response as! HTTPURLResponse).statusCode == 200)
    var lines = bytes.lines.makeAsyncIterator()
    #expect(try await lines.next() == "{\"type\":\"hello\"}")
    #expect(api.listening(.developer) == 1)
    #expect(api.deliver("早安", to: .developer) == nil)
    #expect(try await lines.next() == "{\"text\":\"早安\",\"type\":\"say\"}")
    // A turn that would speak to the developer: typing on while it runs, off when it ends.
    await h.model.stall()
    let doll = h.doll
    let runner = Task { try await doll.run() }
    #expect(try await post(api, "/api/developer", "在嗎") == 200)
    #expect(try await lines.next() == "{\"on\":true,\"type\":\"typing\"}")
    try await waitUntil { await h.doll.interrupt() }
    #expect(try await lines.next() == "{\"on\":false,\"type\":\"typing\"}")
    await h.doll.stop()
    try await runner.value
}

@Test func theBridgeMustComeUpAndItsExitIsASystemError() async throws {
    let h = try await Harness()
    let api = try await localAPI(h)
    defer { api.stop() }
    var c = BridgeConfig()
    c.enabled = true
    c.readyTimeoutS = 10
    // Exits before it is ready: boot fails, saying how.
    c.command = ["/bin/sh", "-c", "echo 'ERROR config: no token' >&2; exit 2"]
    let early = await #expect(throws: StoreError.self) { try await Bridge(config: c, api: api, root: root).start() }
    #expect(early?.description == "bridge exited before it was ready: exit status 2")
    // Never ready: boot fails after ready_timeout_s and the process is stopped.
    c.command = ["/bin/sleep", "30"]
    c.readyTimeoutS = 0.3
    let late = await #expect(throws: StoreError.self) { try await Bridge(config: c, api: api, root: root).start() }
    #expect(late?.description.contains("not ready within 0.3 s") == true)
    // Up (it opened the owner stream with the key it was given), then gone: down, not restarted,
    // and her say to the owner fails with the reason.
    c.readyTimeoutS = 10
    c.command = ["/bin/sh", "-c", "exec /usr/bin/curl -sN --max-time 1.5 -H \"Authorization: Bearer $DOLL_BRIDGE_TOKEN\" \"$DOLL_API/api/listen?to=owner\" > /dev/null"]
    let bridge = Bridge(config: c, api: api, root: root)
    try await bridge.start()
    #expect(api.bridge.hasPrefix("up (pid "))
    #expect(api.deliver("在嗎", to: .owner) == nil)
    try await waitUntil { api.bridge.hasPrefix("down") }
    #expect(api.bridge.contains("exit status 28") && api.bridge.contains("not restarted"))
    #expect(api.listening(.owner) == 0)
    #expect(api.deliver("在嗎", to: .owner)!.contains("bridge down: exit status 28"))
}

@Test func aPersonsLineSaysWhoWhereAndWhetherSpokenOrTyped() async throws {
    // 2026-11-12: she could not tell spoken from typed, nor where a line came from.
    var spoken = Event(.owner, .owner, "時碟滿了嗎", voice: true)
    spoken.channel = .discordDM
    #expect(Doll.speakerTag(spoken, time: "14:32") == "14:32 主人｜Discord 私訊｜語音")
    var typed = Event(.developer, .developer, "在嗎")
    typed.channel = .dollctl
    #expect(Doll.speakerTag(typed, time: "09:05") == "09:05 開發者｜終端機｜文字")
    // A line from before channels were recorded: no place part.
    #expect(Doll.speakerTag(Event(.owner, .owner, "嗨"), time: "08:00") == "08:00 主人｜文字")
    // Through the timeline file and back.
    let h = try await Harness()
    await h.doll.ownerSaid("嗨", voice: true)
    try await h.turn(Scripted("", dm("嗨")))
    let ev = try #require(try h.events().first { $0.kind == .owner })
    #expect(ev.channel == .discordDM && ev.voice)
    let again = try await restarted(h)
    #expect(await again.doll.timelineEvents().first { $0.kind == .owner }?.channel == .discordDM)
}

@Test func aPacksExampleDialoguesFollowItsIdentityUnderAFixedHeader() async throws {
    // 2026-11-12: a list of traits alone made her recite them; examples show tone and length.
    let h = try await Harness { $0.core.pack = "character_packs/doll/doll.toml" }
    let sys = await h.doll.systemPrompt()
    let taboos = try #require(sys.range(of: "只寫繁體字"))
    let examples = try #require(sys.range(of: Doll.examplesHeader + "\nSeith：你好，自我介紹一下"))
    #expect(taboos.upperBound < examples.lowerBound)
    #expect(sys.contains("你是Doll。") && !sys.contains("Bloop"))
}

// voice (M6) ---------------------------------------------------------------------

@Test func webSocketFramesRoundTripAndViolationsAreErrors() throws {
    // RFC 6455 1.3's example key.
    #expect(WebSocket.accept(key: "dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")
    let mask: [UInt8] = [0x37, 0xfa, 0x21, 0x3d]
    var server = WebSocket.Parser(expectMasked: true, maxMessage: 1 << 20)
    // Short, 16-bit and 64-bit lengths, text and binary, split at every byte boundary of the stream.
    let big = Data((0..<70000).map { UInt8($0 & 0xFF) })
    let mid = Data(repeating: 7, count: 300)
    let stream = WebSocket.message(.text("{\"type\":\"hello\",\"version\":1}"), mask: mask) + WebSocket.message(.binary(mid), mask: mask)
        + WebSocket.message(.binary(big), mask: mask) + WebSocket.message(.ping(Data("p".utf8)), mask: mask)
    var got: [DollCore.WebSocket.Message] = []
    for b in stream { got += try server.feed(Data([b])) }
    #expect(got == [.text("{\"type\":\"hello\",\"version\":1}"), .binary(mid), .binary(big), .ping(Data("p".utf8))])
    // A fragmented text message with a ping between its fragments.
    let frags = WebSocket.frame(.text, Data("ab".utf8), fin: false, mask: mask) + WebSocket.frame(.ping, Data(), mask: mask)
        + WebSocket.frame(.continuation, Data("cd".utf8), fin: true, mask: mask)
    let fragmented = try server.feed(frags)
    #expect(fragmented == [.ping(Data()), .text("abcd")])
    // Close with a code and a reason.
    let closed = try server.feed(WebSocket.message(.close(code: 1000, reason: "bye"), mask: mask))
    #expect(closed == [.close(code: 1000, reason: "bye")])
    // A client frame without a mask, an oversized frame, a continuation out of nowhere: errors.
    func parse(_ d: Data, max: Int = 1 << 20, masked: Bool = true) throws -> [DollCore.WebSocket.Message] {
        var p = WebSocket.Parser(expectMasked: masked, maxMessage: max)
        return try p.feed(d)
    }
    #expect(throws: WebSocket.ProtocolError.self) { try parse(WebSocket.message(.text("x"))) }
    #expect(throws: WebSocket.ProtocolError.self) { try parse(WebSocket.message(.binary(mid), mask: mask), max: 100) }
    #expect(throws: WebSocket.ProtocolError.self) { try parse(WebSocket.frame(.continuation, Data("x".utf8), mask: mask)) }
    // The client side reads unmasked server frames.
    #expect(try parse(WebSocket.message(.binary(big)), masked: false) == [.binary(big)])
    // The handshake checks the upgrade headers.
    let ok = try WebSocket.handshake(headers: ["upgrade": "websocket", "connection": "keep-alive, Upgrade",
                                               "sec-websocket-version": "13", "sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ=="])
    #expect(String(decoding: ok, as: UTF8.self).contains("Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n"))
    #expect(throws: WebSocket.ProtocolError.self) { try WebSocket.handshake(headers: ["upgrade": "websocket"]) }
}

@Test func sayTextSplitsIntoSentences() {
    func s(_ t: String, _ max: Int = 60) -> [String] { Sentences.split(t, maxChars: max) }
    #expect(s("好，我看一下。等我一下！真的嗎？") == ["好，我看一下。", "等我一下！", "真的嗎？"])
    // Runs of marks and closing quotes stay with their sentence; a lone ellipsis joins the one before.
    #expect(s("「真的？！」他說。嗯……那好吧") == ["「真的？！」", "他說。", "嗯……", "那好吧"])
    #expect(s("你確定嗎……") == ["你確定嗎……"])
    // Decimals, versions and domains stay whole; a period before a space ends an English sentence.
    #expect(s("圓周率是 3.14，版本 v1.2 在 example.com 上。OK. Wait... what?") == ["圓周率是 3.14，版本 v1.2 在 example.com 上。", "OK.", "Wait...", "what?"])
    #expect(s("第一行\n第二行") == ["第一行", "第二行"])
    #expect(s("   ") == [])
    // Too long: cut after commas, then hard.
    #expect(s("一二三四五六，七八九十一二，三四五", 10) == ["一二三四五六，", "七八九十一二，三四五"])
    #expect(s("一二三四五六七八九十一二三", 5) == ["一二三四五", "六七八九十", "一二三"])
    #expect(Sentences.language("好喔") == "zh" && Sentences.language("OK then") == "en" && Sentences.language("ありがとう") == "ja"
            && Sentences.language("123") == "auto")
}

private func tone(seconds: Double, amplitude: Double = 8000) -> [Int16] {
    (0..<Int(seconds * 16000)).map { Int16(amplitude * sin(2 * .pi * 440 * Double($0) / 16000)) }
}

@Test func endpointingClosesTheUtteranceAfterTheConfiguredSilence() {
    var e = Endpointer(silenceMs: 700, silenceRms: 300)
    // 1 s of voice in 20 ms frames, arriving at 20 ms steps from t = 10.
    let voice = tone(seconds: 1)
    var t = 10.0
    for i in stride(from: 0, to: voice.count, by: 320) {
        let voiced = e.frame(Array(voice[i..<i + 320]), now: t)
        #expect(voiced)
        t += 0.02
    }
    let lastVoiced = t - 0.02
    // Push-to-talk held but quiet for 300 ms: frames come, all under the threshold.
    for _ in 0..<15 {
        let voiced = e.frame([Int16](repeating: 3, count: 320), now: t)
        #expect(!voiced)
        t += 0.02
    }
    let early = e.tick(now: lastVoiced + 0.699)
    #expect(early == nil)
    // 700 ms after the last voiced frame: finalize through the end of voice (1.0 s on the
    // recognizer's clock), after padding the fed audio to 1.7 s.
    let end = e.tick(now: lastVoiced + 0.7001)
    #expect(end == Endpointer.End(pad: 16000 + 11200 - (16000 + 4800), through: 1.0))
    #expect(e.fed == 16000 + 11200)
    // Nothing open: no second end; the next utterance starts on the same clock.
    let again = e.tick(now: lastVoiced + 5)
    #expect(again == nil)
    e.frame(Array(voice[0..<320]), now: 20)
    let next = e.tick(now: 20.71)
    #expect(next?.through == Double(16000 + 11200 + 320) / 16000)
}

@Test func anInterruptedSayReportsWhatWasHeard() {
    let sentences = ["第一句。", "第二句話說到這裡。", "第三句。"]
    // Played half of sentence 2 (9 chars → about 5), sentence 3 generated or not.
    let cut = SpokenReport(sentences: sentences, samples: [9600, 21600], played: 9600 + 10800, interrupted: true)
    #expect(cut.heard.full == 1 && cut.heard.cut! == (1, 5))
    #expect(cut.heardText == "第一句。第二句話說")
    #expect(cut.resultText == "說到「第一句。第二句話說」被主人打斷了（第 2 句「第二句話說到這裡。」大約說到第 5 個字），後面「到這裡。第三句。」沒說出口。")
    // Cut before anything was heard.
    let none = SpokenReport(sentences: sentences, samples: [9600], played: 0, interrupted: true)
    #expect(none.resultText == "還沒說出口就被主人打斷了：「第一句。第二句話說到這裡。第三句。」主人都沒聽到。")
    // Cut exactly at a sentence boundary: the next sentence unheard from its start.
    let edge = SpokenReport(sentences: sentences, samples: [9600, 21600, 9600], played: 9600, interrupted: true)
    #expect(edge.heardText == "第一句。" && edge.resultText.contains("後面「第二句話說到這裡。第三句。」沒說出口"))
    // All heard: empty, like a delivered text say.
    let all = SpokenReport(sentences: sentences, samples: [9600, 21600, 9600], played: 40800, interrupted: false)
    #expect(all.resultText == "" && all.heardText == sentences.joined())
}

@Test func everySpokenSentenceGoesToTheManifest() throws {
    let dir = FileManager.default.temporaryDirectory.appending(path: "voice-manifest-\(UUID().uuidString)")
    let m = try VoiceManifest(dir: dir, writeWAV: writeVoiceWAV)
    let e = VoiceManifest.Entry(file: "a-u1-s1.wav", text: "好。", language: "zh", seed: 42, session: 1, utterance: 1, sentence: 1,
                                startTs: 2, endTs: 3, sampleRate: 24000, samples: 4, generationComplete: true, playedSamples: 2,
                                interrupted: true)
    try m.append(e, audio: [0, 0.5, -0.5, 0])
    var e2 = e
    e2.file = "a-u1-s2.wav"
    e2.sentence = 2
    try m.append(e2, audio: [0.1])
    #expect(try m.entries() == [e, e2])
    let line = try String(contentsOf: dir.appending(path: "manifest.jsonl"), encoding: .utf8).split(separator: "\n")[0]
    #expect(line.contains("\"generation_complete\":true") && line.contains("\"played_samples\":2") && line.contains("\"seed\":42"))
    let clip = try AVAudioFile(forReading: dir.appending(path: "a-u1-s1.wav"))
    #expect(clip.fileFormat.sampleRate == 24000 && clip.length == 4)
}

@Test func voiceToolsAndPromptOnlyWithVoiceEnabled() async throws {
    let mem = try exampleConfig().memory
    #expect(!Tools.specs(memory: mem, voice: false, computer: ComputerConfig(), files: FilesConfig()).contains { $0.name.hasPrefix("voice.") })
    let on = Tools.specs(memory: mem, voice: true, computer: ComputerConfig(), files: FilesConfig())
    #expect(on.suffix(3).map(\.name) == ["voice.join", "voice.say", "voice.leave"])
    #expect(on.first { $0.name == "discord.dm" }!.description.contains("你在語音頻道裡的時候"))
    let off = try await Harness()
    let plain = await off.doll.systemPrompt()
    #expect(!plain.contains("## 語音頻道") && !plain.contains("voice.say"))
    #expect(plain.contains("主人從 Discord 私訊來的話，用 discord.dm 回；開發者從終端機來的話，用 dollctl.reply 回。"))
    let h = try await Harness { $0.voice.enabled = true }
    let sys = await h.doll.systemPrompt()
    let tools = await h.doll.toolsJSON
    #expect(sys.contains("## 語音頻道") && tools.contains("\"name\": \"voice.join\""))
    #expect(!(await off.doll.toolsJSON).contains("voice."))
    #expect(sys.contains("主人從 Discord 私訊來的話，用 discord.dm 回；主人在 Discord 語音說的話，用 voice.say 回（你要先在語音頻道裡）；開發者從終端機來的話，用 dollctl.reply 回。"))
    #expect(sys.contains("連結、網址、公式、程式碼"))
    // The removed generic names are nowhere in what she reads.
    for old in ["用 say", "\"name\": \"say\"", "send_text", "join_voice", "leave_voice"] { #expect(!sys.contains(old) && !tools.contains(old)) }
    // The tools decode; with no voice wired each fails with the reason and what to use instead.
    try await h.turn(Scripted("", voiceJoin, voiceSay("嗨"), voiceLeave, dm("https://example.com")))
    let results = try h.toolResults()
    #expect(results.prefix(3).map(\.text) == Array(repeating: "失敗：語音還沒接上，語音頻道現在用不了；要跟主人說話用 discord.dm。", count: 3))
    #expect(h.last.actions == ["error voice.join", "error voice.say", "error voice.leave", "said discord_dm"])
    #expect(results[3].text == "" && h.said == ["https://example.com"] && h.saidTo == [.owner])
}

/// The bridge's side of `/api/voice` for tests: a raw TCP client doing the handshake and masked
/// frames, with a scripted Discord: join → joined, playback faster than real time in `step`-sample
/// reports, `owner_speaking` once `interruptAt` samples were played.
private final class FakeBridge: @unchecked Sendable {
    let connection: NWConnection
    let lock = Mutex(State())
    struct State {
        var log: [String] = []          // control lines received, and "audio <n>" per binary frame
        var received: [Int: Int] = [:]  // samples per utterance
        var played: [Int: Int] = [:]
        var open: Int?
        var ended: Set<Int> = []
        var stopped: Set<Int> = []
        var interruptAt: Int?
        var interrupted = false
        var firstAudio: ContinuousClock.Instant?
        var startedAt: ContinuousClock.Instant?
        var answersPings = true         // false: a half-open socket whose bridge is gone
    }
    var parser = WebSocket.Parser(expectMasked: false, maxMessage: 1 << 22)
    let step: Int

    init(port: Int, key: String, step: Int = 1200) async throws {
        self.step = step
        connection = NWConnection(host: "127.0.0.1", port: NWEndpoint.Port(rawValue: UInt16(port))!, using: .tcp)
        connection.start(queue: .global())
        let req = "GET /api/voice HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            + "Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nAuthorization: Bearer \(key)\r\n\r\n"
        try await send(Data(req.utf8))
        var head = Data()
        while head.range(of: Data("\r\n\r\n".utf8)) == nil { head += try await receive() }
        let end = head.range(of: Data("\r\n\r\n".utf8))!
        let status = String(decoding: head[..<end.lowerBound], as: UTF8.self)
        guard status.hasPrefix("HTTP/1.1 101") else { throw VoiceFailure("handshake: \(status)") }
        let rest = Data(head[end.upperBound...])
        Task { await self.loop(rest) }
        Task { await self.playback() }
    }

    func send(_ d: Data) async throws {
        try await withCheckedThrowingContinuation { (c: CheckedContinuation<Void, Error>) in
            connection.send(content: d, completion: .contentProcessed { e in if let e { c.resume(throwing: e) } else { c.resume() } })
        }
    }
    func receive() async throws -> Data {
        try await withCheckedThrowingContinuation { c in
            connection.receive(minimumIncompleteLength: 1, maximumLength: 1 << 16) { d, _, done, e in
                if let e { c.resume(throwing: e) } else if let d, !d.isEmpty { c.resume(returning: d) } else if done { c.resume(throwing: VoiceFailure("closed")) } else { c.resume(returning: Data()) }
            }
        }
    }
    func json(_ obj: [String: Any]) async throws {
        try await send(WebSocket.message(.text(String(decoding: try JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys]), as: UTF8.self)), mask: [9, 8, 7, 6]))
    }
    func audio(_ samples: [Int16]) async throws {
        var d = Data()
        for s in samples { withUnsafeBytes(of: s.littleEndian) { d.append(contentsOf: $0) } }
        try await send(WebSocket.message(.binary(d), mask: [1, 2, 3, 4]))
    }

    func loop(_ first: Data) async {
        var data = first
        while true {
            do {
                for m in try parser.feed(data) { try await handle(m) }
                data = try await receive()
            } catch { return }
        }
    }

    func handle(_ m: DollCore.WebSocket.Message) async throws {
        switch m {
        case .binary(let d):
            lock.withLock { s in
                s.log.append("audio \(d.count / 2)")
                if s.firstAudio == nil { s.firstAudio = .now }
                if let u = s.open { s.received[u, default: 0] += d.count / 2 }
            }
        case .text(let t):
            let obj = try JSONSerialization.jsonObject(with: Data(t.utf8)) as! [String: Any]
            let type = obj["type"] as! String
            lock.withLock { $0.log.append(type) }
            switch type {
            case "join": try await json(["type": "joined", "channel": "General"])
            case "leave": try await json(["type": "left"])
            case "utterance_start":
                lock.withLock { $0.open = obj["utterance"] as? Int; $0.startedAt = .now }
            case "utterance_end": lock.withLock { s in s.ended.insert(obj["utterance"] as! Int); s.open = nil }
            case "stop":
                let u = obj["utterance"] as! Int
                let p = lock.withLock { s -> Int in s.stopped.insert(u); s.open = nil; return s.played[u] ?? 0 }
                try await json(["type": "played", "utterance": u, "samples": p])
            default: break
            }
        case .ping(let d):
            if lock.withLock({ $0.answersPings }) { try await send(WebSocket.message(.pong(d), mask: [5, 6, 7, 8])) }
        default: break
        }
    }

    /// Plays every utterance at `step` samples per 5 ms tick, reports each step, and speaks over her
    /// once `interruptAt` samples of the current one were played.
    func playback() async {
        while true {
            try? await Task.sleep(for: .milliseconds(5))
            let (reports, speak) = lock.withLock { s -> ([(Int, Int)], Bool) in
                var out: [(Int, Int)] = []
                var speak = false
                // Once the owner spoke over her, playback holds until her stop (a real bridge keeps
                // playing a few ms; holding makes the cut point exact for the test).
                for (u, got) in s.received where !s.stopped.contains(u) && !s.interrupted {
                    let p = s.played[u] ?? 0
                    guard p < got else { continue }
                    var next = min(got, p + step)
                    if let at = s.interruptAt, !s.interrupted, next >= at { next = at; s.interrupted = true; speak = true }
                    s.played[u] = next
                    out.append((u, next))
                }
                return (out, speak)
            }
            for (u, p) in reports { try? await json(["type": "played", "utterance": u, "samples": p]) }
            if speak { try? await json(["type": "owner_speaking"]) }
        }
    }

    var log: [String] { lock.withLock { $0.log } }
}

/// A recognizer that returns `text` for every finalize and records what it was given.
private actor FakeRecognizer: SpeechRecognizer {
    var fed = 0
    var finalizes: [(through: Double, fed: Int, at: ContinuousClock.Instant)] = []
    let text: String
    init(_ text: String) { self.text = text }
    func feed(_ samples: [Int16]) { fed += samples.count }
    func finalize(through: Double) -> String {
        finalizes.append((through, fed, .now))
        return text
    }
}

/// 0.1 s (2400 samples) per character, in 2400-sample chunks, 2 ms apart.
private final class FakeSynth: SpeechSynthesizer, Sendable {
    var sampleRate: Int { 24000 }
    func synthesize(_ text: String, language: String, shouldStop: @escaping @Sendable () -> Bool,
                    onChunk: @escaping @Sendable ([Float]) -> Void) async throws -> Synthesized {
        var all: [Float] = []
        for _ in 0..<text.count {
            if shouldStop() { return Synthesized(seed: 7, samples: all, complete: false) }
            try await Task.sleep(for: .milliseconds(2))
            let chunk = [Float](repeating: 0.25, count: 2400)
            all += chunk
            onChunk(chunk)
        }
        return Synthesized(seed: 7, samples: all, complete: true)
    }
}

private struct VoiceRig {
    let h: Harness
    let api: LocalAPI
    let link: VoiceLink
    let bridge: FakeBridge
    let manifest: VoiceManifest
}

private func voiceRig(_ recognizer: any SpeechRecognizer, synth: any SpeechSynthesizer = FakeSynth(),
                      tune: (inout VoiceConfig) -> Void = { _ in }) async throws -> VoiceRig {
    let h = try await Harness { $0.voice.enabled = true }
    var vc = VoiceConfig()
    vc.enabled = true
    vc.silenceMs = 300
    vc.bridgeTimeoutS = 5
    vc.bridgeProbeMs = 200
    let manifest = try VoiceManifest(dir: h.dir.appending(path: "voice"), writeWAV: writeVoiceWAV)
    let link = VoiceLink(config: vc, recognizer: recognizer, synthesizer: synth, manifest: manifest)
    let doll = h.doll
    await link.setEvents(VoiceEvents(ownerSaid: { await doll.ownerSaid($0, voice: true, channel: .discordVoice) },
                                     notice: { await doll.voiceNotice($0) }))
    await doll.setVoice(link)
    let api = try await localAPI(h)
    api.setVoice(link)
    let bridge = try await FakeBridge(port: api.port, key: api.ownerKey)
    try await bridge.json(["type": "hello", "version": 1])
    return VoiceRig(h: h, api: api, link: link, bridge: bridge, manifest: manifest)
}

@Test func voiceLoopbackHearsTheOwnerSpeaksAndStopsWhenHeTalks() async throws {
    let rec = FakeRecognizer("你好嗎")
    let r = try await voiceRig(rec)
    defer { r.api.stop() }
    let h = r.h
    // Not in a voice channel, and the owner is in none: each voice tool is refused with the reason
    // and what she can use now; nothing reaches the bridge.
    await h.doll.ownerSaid("在嗎")
    try await h.turn(Scripted("", voiceJoin, voiceSay("在"), voiceLeave))
    #expect(try h.toolResults().map(\.text) == [
        "失敗：主人不在語音頻道裡，進不去。等他進了語音頻道、叫你進去再用 voice.join；現在跟他說話用 discord.dm。",
        "失敗：你不在語音頻道裡，這句沒說出口。主人叫你進去才用 voice.join；現在要跟主人說話用 discord.dm。",
        "失敗：你不在語音頻道裡，不用離開；要跟主人說話用 discord.dm。",
    ])
    #expect(h.last.actions == ["error voice.join", "error voice.say", "error voice.leave"])
    #expect(r.bridge.log.isEmpty && h.said.isEmpty)
    // The owner entering a voice channel is not an event to her; he asks her in a DM.
    try await r.bridge.json(["type": "owner_joined", "channel": "General"])
    try await Task.sleep(for: .milliseconds(200))
    #expect(await h.doll.queue.isEmpty)
    await h.doll.ownerSaid("進語音一下")
    // She joins and speaks: three sentences; the owner talks over the second one, half-way through.
    r.bridge.lock.withLock { $0.interruptAt = 9600 + 10800 }
    try await h.turn(Scripted("", voiceJoin, voiceSay("第一句。第二句話說到這裡。第三句。"), voiceSay("還有這句。"), voiceJoin))
    let results = Array(try h.toolResults().dropFirst(3))
    #expect(results[0].text == "進了語音頻道「General」。你只聽得到主人；在裡面說話用 voice.say。")
    #expect(results[1].text == "說到「第一句。第二句話說」被主人打斷了（第 2 句「第二句話說到這裡。」大約說到第 5 個字），後面「到這裡。第三句。」沒說出口。")
    #expect(results[2].text == "沒有念：前面一句被主人打斷了，這句沒說出口。")
    // Already inside: joining again is refused.
    #expect(results[3].text == "失敗：你已經在語音頻道「General」裡了，不用再進；在裡面說話用 voice.say，要出來用 voice.leave。")
    #expect(h.last.actions == ["voice.join", "said discord_voice (cut)", "error voice.say", "error voice.join"])
    #expect(results.map(\.channel) == [nil, .discordVoice, nil, nil])
    #expect(h.said.isEmpty)   // nothing went to the DM
    r.bridge.lock.withLock { $0.interruptAt = nil; $0.interrupted = false }
    #expect(await !h.model.seen(1).contains { $0.role == "user" && $0.content.contains("【語音】") })
    // On the wire: start, audio, the owner's burst, stop, and after the stop no audio and no end.
    let log = r.bridge.log
    let stop = try #require(log.firstIndex(of: "stop"))
    #expect(log.first == "join" && log.filter { $0 == "join" }.count == 1 && log.contains("utterance_start") && log.prefix(stop).contains { $0.hasPrefix("audio") })
    #expect(!log.suffix(from: stop).contains { $0.hasPrefix("audio") || $0 == "utterance_end" })
    // The manifest keeps every generated sentence, the cut one marked.
    let entries = try r.manifest.entries()
    #expect(entries.count >= 2 && entries[0].text == "第一句。" && !entries[0].interrupted && entries[0].playedSamples == 9600)
    #expect(entries[1].interrupted && entries[1].playedSamples == 10800 && entries[1].seed == 7 && entries[1].language == "zh")
    for e in entries { #expect(FileManager.default.fileExists(atPath: r.manifest.dir.appending(path: e.file).path)) }

    // The owner speaks: 1 s of voice frames, then silence; 300 ms later the utterance is final.
    let voice = tone(seconds: 1)
    for i in stride(from: 0, to: voice.count, by: 320) { try await r.bridge.audio(Array(voice[i..<i + 320])) }
    let sent = ContinuousClock.now
    try await h.waitFor(.owner)
    let fin = try #require(await rec.finalizes.first)
    #expect(fin.through == 1.0 && fin.fed == 16000 + 4800)
    #expect(fin.at - sent >= .milliseconds(250))
    // In the channel she still picks: `voice.say` is heard, `discord.dm` is text in his DM.
    try await h.turn(Scripted("", voiceSay("嗯。"), dm("https://example.com")))
    #expect(h.said == ["https://example.com"] && h.last.actions == ["said discord_voice", "said discord_dm"])
    let heard = try #require(await h.model.seen(2).last { $0.content.contains("你好嗎") })
    #expect(heard.content.contains("主人｜Discord 語音｜語音] 你好嗎"))
    // Not interrupted: played to the end, an empty result.
    #expect(try h.toolResults().suffix(2).map(\.text) == ["", ""])
    #expect(r.bridge.log.last == "utterance_end" || r.bridge.log.contains("utterance_end"))
    // The bridge's final `played` for the utterance that just ended arrives after `speak` returned:
    // a race, not a violation (2026-11-14 live: it closed the socket and the bridge exited).
    #expect(r.bridge.log.filter { $0 == "utterance_start" }.count == 2)
    try await r.bridge.json(["type": "played", "utterance": 2, "samples": 0])
    try await Task.sleep(for: .milliseconds(200))
    #expect(!r.bridge.log.contains("error"))
    // He leaves while she is inside: that one reaches her.
    try await r.bridge.json(["type": "owner_left"])
    try await waitUntil { await h.doll.queue.contains { $0.kind == .memory } }
    // She leaves; after that `voice.say` is refused and `discord.dm` still reaches him.
    try await h.turn(Scripted("", voiceLeave, voiceSay("掰"), dm("掰")))
    #expect(try h.events().contains { $0.kind == .memory && $0.text == "【語音】主人離開語音頻道「General」了。你還在「General」裡；要出來用 voice.leave。" })
    #expect(try h.toolResults().suffix(3).map(\.text) == [
        "離開語音頻道了。之後跟主人說話用 discord.dm。",
        "失敗：你不在語音頻道裡，這句沒說出口。主人叫你進去才用 voice.join；現在要跟主人說話用 discord.dm。",
        "",
    ])
    #expect(h.said == ["https://example.com", "掰"])
}

@Test func voiceLinkRejectsASecondBridgeAndProtocolViolations() async throws {
    let r = try await voiceRig(FakeRecognizer(""))
    defer { r.api.stop() }
    // One bridge at a time.
    await #expect(throws: (any Error).self) { _ = try await FakeBridge(port: r.api.port, key: r.api.ownerKey) }
    // No key, no upgrade.
    await #expect(throws: (any Error).self) { _ = try await FakeBridge(port: r.api.port, key: "nope") }
    // A played count above what was sent: an error back, then the socket closes.
    try await r.bridge.json(["type": "played", "utterance": 5, "samples": 1])
    try await waitUntil { r.bridge.log.contains("error") }
    #expect(r.bridge.log.contains("error"))
    try await waitUntil { await r.link.socket == nil }
    // The bridge reconnects (2026-11-14): accepted at once, and voice works on the new socket.
    let again = try await FakeBridge(port: r.api.port, key: r.api.ownerKey)
    try await again.json(["type": "hello", "version": 1])
    try await again.json(["type": "owner_joined", "channel": "General"])
    try await waitUntil { await r.link.ownerChannel == "General" }
    #expect(try await r.link.join() == "General")
}

/// Owner, 2026-11-14: a voice failure only affects voice. The bridge's socket ends mid-utterance:
/// `voice.say` fails with the reason, she is told once, and the reconnected bridge can be joined
/// and spoken through again; utterance ids keep counting, nothing on the new socket is a violation.
@Test func voiceBridgeReconnectsMidUtteranceAndSheSpeaksAgain() async throws {
    let r = try await voiceRig(FakeRecognizer(""))
    defer { r.api.stop() }
    try await r.bridge.json(["type": "owner_joined", "channel": "General"])
    try await waitUntil { await r.link.ownerChannel == "General" }
    #expect(try await r.link.join() == "General")
    let link = r.link
    let saying = Task { try await link.speak("一二三四五六七八九十。一二三四五六七八九十。一二三四五六七八九十。") }
    try await waitUntil { r.bridge.log.contains { $0.hasPrefix("audio") } }
    r.bridge.connection.cancel()                      // the bridge's socket ends mid-utterance
    await #expect { _ = try await saying.value } throws: { "\($0)".contains("語音連線斷了") }
    try await waitUntil { await r.h.doll.queue.contains { $0.text.contains("語音連線斷了") } }
    try await Task.sleep(for: .milliseconds(200))
    #expect(await r.h.doll.queue.filter { $0.text.contains("語音連線斷了") }.count == 1)
    #expect(await r.link.channel() == nil)

    let b2 = try await FakeBridge(port: r.api.port, key: r.api.ownerKey)
    try await b2.json(["type": "hello", "version": 1])
    try await b2.json(["type": "owner_joined", "channel": "General"])
    try await waitUntil { await r.link.ownerChannel == "General" }
    #expect(try await r.link.join() == "General")
    let report = try await r.link.speak("嗯。")
    #expect(report.samples == [4800] && report.played == 4800 && !report.interrupted)
    #expect(await r.link.utteranceSerial == 2)
    // A late report for the cut utterance 1 is a race, not a violation.
    try await b2.json(["type": "played", "utterance": 1, "samples": 0])
    try await Task.sleep(for: .milliseconds(200))
    #expect(!b2.log.contains("error") && b2.log.contains("utterance_end"))
}

/// A half-open socket (its bridge gone without a close) must not keep the reconnecting bridge out:
/// it does not answer the probe ping within `bridge_probe_ms`, so it is closed and replaced.
@Test func voiceStaleBridgeSocketIsReplacedByTheNewOne() async throws {
    let r = try await voiceRig(FakeRecognizer(""))
    defer { r.api.stop() }
    try await r.bridge.json(["type": "owner_joined", "channel": "General"])
    try await waitUntil { await r.link.ownerChannel == "General" }
    #expect(try await r.link.join() == "General")
    r.bridge.lock.withLock { $0.answersPings = false }
    let t0 = ContinuousClock.now
    let b2 = try await FakeBridge(port: r.api.port, key: r.api.ownerKey)
    #expect(ContinuousClock.now - t0 >= .milliseconds(200))
    try await waitUntil { await r.h.doll.queue.contains { $0.text.contains("replaced a stale one") } }
    try await b2.json(["type": "hello", "version": 1])
    try await b2.json(["type": "owner_joined", "channel": "General"])
    try await waitUntil { await r.link.ownerChannel == "General" }
    // Whatever the old socket still sends is not the new bridge's.
    try? await r.bridge.json(["type": "played", "utterance": 99, "samples": 1])
    #expect(try await r.link.join() == "General")
    try await Task.sleep(for: .milliseconds(100))
    #expect(!b2.log.contains("error") && b2.log == ["join"])
}

/// Opt-in (`DOLL_VOICE_RECONNECT=1`): the real Qwen3-TTS and dollcore's real link against the real
/// bridge `VoiceLink` (experiments/m6-voice-reconnect/bridge_harness.py: fake Discord, real
/// Speaker and reconnect loop). Twice mid-utterance the socket is ended from this side, first as a
/// protocol violation (`error` then close 1002, the live 2026-11-14 path), then as a plain close;
/// each time the bridge must come back by itself, be joined again and finish a new utterance.
@Test(.enabled(if: ProcessInfo.processInfo.environment["DOLL_VOICE_RECONNECT"] == "1"))
func realBridgeRecoversFromAViolationMidUtterance() async throws {
    var cfg = try exampleConfig()
    cfg.voice.enabled = true
    MLXProcess.configure(cacheLimitMB: cfg.core.mlxCacheLimitMB)
    let synth = try await QwenSynthesizer.load(cfg.voice, root: root)
    let h = try await Harness { $0.voice.enabled = true }
    let manifest = try VoiceManifest(dir: h.dir.appending(path: "voice"), writeWAV: writeVoiceWAV)
    let link = VoiceLink(config: cfg.voice, recognizer: FakeRecognizer(""), synthesizer: synth, manifest: manifest)
    let notices = Mutex<[String]>([])
    link.setLogSink { print("M6RECON LOG " + $0) }
    await link.setEvents(VoiceEvents(ownerSaid: { _ in }, notice: { n in
        notices.withLock { $0.append(n) }
        print("M6RECON NOTICE " + n)
    }))
    let api = try await localAPI(h)
    defer { api.stop() }
    api.setVoice(link)
    let dir = URL(filePath: "/tmp/m6reconnect")
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    try JSONSerialization.data(withJSONObject: ["port": Int(api.port), "key": api.ownerKey]).write(to: dir.appending(path: "endpoint.json"))
    print("M6RECON READY port \(api.port)")
    func linked(within seconds: Double) async throws {
        try await waitUntil(seconds: seconds) {
            let (hello, owner, hers) = await (link.hello, link.ownerChannel, link.channel())
            return hello && owner != nil && hers == nil
        }
        #expect(try await link.join() == "General")
    }
    func cutMidUtterance(_ cut: @escaping @Sendable () async -> Void) async throws {
        let t0 = ContinuousClock.now
        let saying = Task { try await link.speak("第一句話說得長一點，讓它還在播的時候斷掉。第二句也還沒說完，對吧？") }
        try await waitUntil(seconds: 60) { (await link.current?.played ?? 0) > 24000 }
        let at = await link.current.map { "\($0.played)/\($0.sent)" } ?? "-"
        await cut()
        let result = await saying.result
        print("M6RECON CUT at \(at) samples after \(ContinuousClock.now - t0): \(result)")
        if case .success = result { Issue.record("speak survived the cut") }
    }
    func speakAgain() async throws {
        let r = try await link.speak("我回來了，聽得到嗎？")
        print("M6RECON AGAIN played \(r.played)/\(r.samples.reduce(0, +)) interrupted=\(r.interrupted)")
        #expect(r.played == r.samples.reduce(0, +) && !r.interrupted)
    }
    try await linked(within: 120)
    try await cutMidUtterance { _ = await link.violation("forced by the reconnect check") }
    let t1 = ContinuousClock.now
    try await linked(within: 30)
    print("M6RECON RELINKED after the violation in \(ContinuousClock.now - t1)")
    try await speakAgain()
    try await cutMidUtterance { await link.socket?.close(code: 1011, reason: "forced close") }
    let t2 = ContinuousClock.now
    try await linked(within: 30)
    print("M6RECON RELINKED after the close in \(ContinuousClock.now - t2)")
    try await speakAgain()
    let n = notices.withLock { $0 }
    print("M6RECON DONE notices=\(n.count) utterances=\(await link.utteranceSerial)")
    #expect(n.count == 2 && n.allSatisfy { $0.contains("語音連線斷了") })
}

/// Opt-in (`DOLL_VOICE_REAL=1`): the real Qwen3-TTS, the real SpeechTranscriber and the real
/// embedding model in this process, through the loopback with the fake bridge. Prints voice-ready
/// time, first-audio latency, the footprint peak with embedding + TTS, and what the recognizer
/// heard of her own sentence; writes them to /tmp/m6test/real.txt.
@Test(.enabled(if: ProcessInfo.processInfo.environment["DOLL_VOICE_REAL"] == "1"))
func realVoiceThroughTheLoopback() async throws {
    var cfg = try exampleConfig()
    cfg.voice.enabled = true
    var lines: [String] = []
    func note(_ s: String) { print("M6REAL " + s); lines.append(s) }
    MLXProcess.configure(cacheLimitMB: cfg.core.mlxCacheLimitMB)
    let t0 = ContinuousClock.now
    let embedder = try await MLXEmbedder.load(cfg.memory)
    note("embedding loaded in \(ContinuousClock.now - t0); footprint \(MLXProcess.footprint())")
    let t1 = ContinuousClock.now
    let synth = try await QwenSynthesizer.load(cfg.voice, root: root)
    let tts = ContinuousClock.now - t1
    let recognizer = try await AppleSpeechRecognizer.start(locale: cfg.voice.asrLocale, finalTimeoutMs: cfg.voice.asrFinalTimeoutMs)
    note("voice ready in \(ContinuousClock.now - t1) (TTS load + reference + warm-up \(tts))")
    let r = try await voiceRig(recognizer, synth: synth) { $0 = cfg.voice; $0.bridgeTimeoutS = 30 }
    defer { r.api.stop() }
    _ = try embedder.embed("今天天氣很好", as: .document)
    try await r.bridge.json(["type": "owner_joined", "channel": "General"])
    try await r.h.turn(Scripted("", voiceJoin, voiceSay("好，我看一下。今天的天氣還不錯，我們等一下出去走走吧！")))
    let reports = try r.h.toolResults().map(\.text)
    note("say results \(reports)")
    let s = r.bridge.lock.withLock { $0 }
    note("first audio at the bridge after utterance_start: \(s.firstAudio.flatMap { a in s.startedAt.map { a - $0 } }.map { "\($0)" } ?? "none")")
    let entries = try r.manifest.entries()
    note("manifest: \(entries.map { "\($0.sentence) \($0.text) \($0.samples) samples, \(String(format: "%.2f", $0.endTs - $0.startTs)) s" })")
    let mem = MLXProcess.footprint()
    note(String(format: "footprint now %.2f GB, peak %.2f GB (embedding + TTS + ASR)", Double(mem.current) / 1_073_741_824, Double(mem.peak) / 1_073_741_824))
    // Her first sentence back through the recognizer, at 16 kHz, as owner audio.
    let wav = try AVAudioFile(forReading: r.manifest.dir.appending(path: entries[0].file))
    let buf = AVAudioPCMBuffer(pcmFormat: wav.processingFormat, frameCapacity: AVAudioFrameCount(wav.length))!
    try wav.read(into: buf)
    let src = Array(UnsafeBufferPointer(start: buf.floatChannelData![0], count: Int(buf.frameLength)))
    let n16 = src.count * 2 / 3
    let pcm: [Int16] = (0..<n16).map { i in
        let x = Double(i) * 1.5
        let j = Int(x), f = Float(x - Double(j))
        let v = src[j] * (1 - f) + (j + 1 < src.count ? src[j + 1] : 0) * f
        return Int16(max(-32768, min(32767, v * 32767)))
    }
    for i in stride(from: 0, to: pcm.count - 320, by: 320) {
        try await r.bridge.audio(Array(pcm[i..<i + 320]))
        try await Task.sleep(for: .milliseconds(20))
    }
    let spoke = ContinuousClock.now
    try await r.h.waitFor(.owner, seconds: 10)
    let heard = await r.h.doll.queue.first { $0.kind == .owner }?.text ?? "(nothing)"
    note("ASR of 「\(entries[0].text)」: 「\(heard)」 \(ContinuousClock.now - spoke) after the last frame")
    try FileManager.default.createDirectory(at: URL(filePath: "/tmp/m6test"), withIntermediateDirectories: true)
    try lines.joined(separator: "\n").write(to: URL(filePath: "/tmp/m6test/real.txt"), atomically: true, encoding: .utf8)
    #expect(entries.count == 2 && reports.dropFirst().allSatisfy(\.isEmpty))
}

/// Opt-in (`DOLL_VOICE_REAL=1`): SpeechTranscriber alone, fed 16 kHz PCM of the pack's reference
/// clip in 20 ms frames, then our endpointing's padding and `finalize(through:)`.
@Test(.enabled(if: ProcessInfo.processInfo.environment["DOLL_VOICE_REAL"] == "1"))
func realSpeechRecognizerFinalizesOnOurEndpoint() async throws {
    let rec = try await AppleSpeechRecognizer.start(locale: "zh-TW", finalTimeoutMs: 3000)
    let file = try AVAudioFile(forReading: root.appending(path: "character_packs/doll/voice/qwen3/ref_plus_zh.wav"))
    let fmt = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: true)!
    let conv = AVAudioConverter(from: file.processingFormat, to: fmt)!
    let src = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: AVAudioFrameCount(file.length))!
    try file.read(into: src)
    let out = AVAudioPCMBuffer(pcmFormat: fmt, frameCapacity: AVAudioFrameCount(Double(file.length) * 16000 / file.processingFormat.sampleRate) + 1024)!
    var done = false
    var err: NSError?
    conv.convert(to: out, error: &err) { _, s in
        if done { s.pointee = .endOfStream; return nil }
        done = true; s.pointee = .haveData; return src
    }
    let all = Array(UnsafeBufferPointer(start: out.int16ChannelData![0], count: Int(out.frameLength)))
    let tail = Array(all.suffix(16000 * 3))   // 「好，我看一下。」 at the end
    var e = Endpointer(silenceMs: 700, silenceRms: 300)
    let t0 = Date().timeIntervalSince1970
    for i in stride(from: 0, to: tail.count - 320, by: 320) {
        let f = Array(tail[i..<i + 320])
        e.frame(f, now: Date().timeIntervalSince1970)
        try await rec.feed(f)
        try await Task.sleep(for: .milliseconds(20))
    }
    var end: Endpointer.End?
    while end == nil {
        try await Task.sleep(for: .milliseconds(20))
        end = e.tick(now: Date().timeIntervalSince1970)
    }
    try await rec.feed([Int16](repeating: 0, count: end!.pad))
    let c0 = ContinuousClock.now
    let text = try await rec.finalize(through: end!.through)
    print("M6ASR through \(end!.through) pad \(end!.pad) fed \(e.fed): 「\(text)」 in \(ContinuousClock.now - c0), total \(Date().timeIntervalSince1970 - t0) s")
    #expect(!text.isEmpty)
}

// the todo list, gating and compaction (2026-11-14) ------------------------------

@Test func withoutAnItemInProgressTheComputerToolsAreRefused() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("按一下 Return")
    try await h.turn(Scripted("", key("Return"), readCall))
    #expect(try h.toolResults().map(\.text) == ["失敗：現在沒有進行中的 todo，act 不能用。先用 todo.set 列 todo（小事就一項，每項寫做完的樣子），再做。",
                                                "失敗：現在沒有進行中的 todo，read_window 不能用。先用 todo.set 列 todo（小事就一項，每項寫做完的樣子），再做。"])
    #expect(h.last.actions == ["refused act", "refused read_window"] && h.cua.tools.isEmpty && h.last.todo == "待命中")
    #expect(h.last.footer.contains("- todo：待命中（沒有 todo；要做事先用 todo.set 列）"))
    // Her refused calls are results she has not seen: she gets the next turn to list the todo.
    #expect(await h.doll.followUp)
    // The list and the action in one reply: the item is in progress before the action runs.
    try await h.turn(Scripted("", todoSet("畫面上按過 Return", "存好檔"), readCall, key("Return")))
    #expect(h.cua.actions == ["press_key"] && h.last.todo == "進行中")
    #expect(try h.toolResults().suffix(3).first?.text == "todo 列好了，你估整份 5 回合。現在進行中：「畫面上按過 Return」；後面：「存好檔」。")
    // 進行中: a reply that only speaks still gets the next turn.
    try await h.turn(Scripted("", dm("按了")))
    #expect(await h.doll.followUp)
    #expect(h.last.footer.contains("- todo：進行中「畫面上按過 Return」；後面還有：「存好檔」"))
    // Not gated: speaking, remember, recall, think and the todo tools themselves work while idle (above).
    let t = try h.timeline().todo
    #expect(t.phase == .doing && t.current == "畫面上按過 Return" && t.pending == ["存好檔"])
}

@Test func doneIsRefusedForAnItemSheNeverTouched() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("存檔，再另存到桌面")
    try await h.turn(Scripted("", todoSet("notes 存好檔", "桌面有 copy.txt"), readCall, key("cmd+s")))
    try await h.turn(Scripted("", done("⌘S 存好了。")))
    // The second item is in progress and untouched: a made-up done is refused and it stays in progress.
    try await h.turn(Scripted("", done("另存好了，read_file 確認過。")))
    let refused = try #require(try h.toolResults().last)
    #expect(refused.text.hasPrefix("失敗：「桌面有 copy.txt」這一項你還沒做任何動作，也沒看過結果，不能說完成。"))
    #expect(h.last.actions == ["error todo.done"])
    // Having looked, she may.
    try await h.turn(Scripted("", readCall))
    try await h.turn(Scripted("", dm("copy.txt 在桌面了"), done("桌面看得到 copy.txt。")))
    #expect(try h.toolResults().last!.text.hasPrefix("完成，記下了。"))
}

@Test func doneCompactsTheItemKeepsTheConversationAndMovesOn() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("把 notes 存檔，再關掉")
    try await h.turn(Scripted("好，先看畫面", todoSet("notes 存好檔，標題沒有「已編輯」", "TextEdit 關掉了"), readCall))  // request 0
    try await h.turn(Scripted("我按 ⌘S", key("cmd+s")))                                                                  // request 1
    await h.doll.ownerSaid("順便問，晚餐吃什麼？")
    try await h.turn(Scripted("先回他", dm("等我存完再想"), clickRow(5)))                                                // request 2
    try await h.turn(Scripted("看起來好了", dm("存好了。"), done("按 ⌘S、點 Save 存檔，標題列沒有「已編輯」。")))         // request 3
    try await h.turn(Scripted("", key("cmd+q")))                                                                        // request 4
    try await h.turn(Scripted("", dm("關了")))                                                                          // request 5
    let before = await h.model.seen(3), after = await h.model.seen(4), next = await h.model.seen(5)
    // Up to the item's start (her reply that listed it) nothing changed.
    #expect(Array(after.prefix(2)) == Array(before.prefix(2)))
    #expect(before.contains { !$0.images.isEmpty } && before.contains { $0.content.hasPrefix("先回他") || $0.content == "我按 ⌘S" })
    // After: no screenshot, no computer result, no thought, no footer of the item's replies.
    let item = Array(after.dropFirst(2).dropLast())
    #expect(item.allSatisfy { $0.images.isEmpty && $0.extra.isEmpty && !$0.content.hasPrefix("【狀況】") })
    #expect(item.filter { $0.role == "assistant" }.allSatisfy { $0.content.isEmpty })
    // What stays, in order: her todo.set (and its result), the owner's line verbatim, what she told him, her done with the action list.
    #expect(item.map(\.role) == ["assistant", "tool", "user", "assistant", "tool", "assistant", "tool", "tool"])
    #expect(item[0].toolCalls.map(\.name) == ["todo.set"] && item[1].content.hasPrefix("todo 列好了，"))
    #expect(item[2].content.hasSuffix("主人｜Discord 私訊｜文字] 順便問，晚餐吃什麼？"))
    #expect(item[3].toolCalls.map(\.name) == ["discord.dm"] && item[3].toolCalls[0].arguments!.contains("等我存完再想"))
    #expect(item[5].toolCalls.map(\.name) == ["discord.dm", "todo.done"])
    #expect(item[7].content.hasPrefix("完成，記下了。這次的經過和步驟存成一條經驗。")
            && item[7].content.contains("動作清單：key cmd+s、click AXButton \"=\"。下一項「TextEdit 關掉了」現在進行中。"))
    // The next item is in progress from right after the turn that finished the first.
    let t = try h.timeline().todo
    let last = try h.events().last { $0.kind == .doll && $0.calls.contains { $0.name == "todo.done" } }!
    #expect(t.phase == .doing && t.current == "TextEdit 關掉了" && t.pending.isEmpty)
    #expect(try h.timeline().compactions == [Compaction(kind: .done, item: "notes 存好檔，標題沒有「已編輯」", fromSeq: 1, toSeq: t.startSeq)]
            && t.startSeq > last.seq!)
    #expect(h.reports[3].itemEnd == "完成" && h.reports[3].compacted == ["done"] && h.reports[3].todo == "進行中")
    // Byte-stable afterwards: the next request extends this one.
    let enc = { (m: [ChatMessage]) in try ChatMessage.encoder.encode(m) }
    #expect(try enc(next).starts(with: enc(after).dropLast()))
    #expect(Array(next.prefix(after.count - 1)) == Array(after.dropLast()))
    // The originals stay in the timeline.
    #expect(try h.events().contains { $0.kind == .tool && $0.window != nil && $0.image != nil })
}

@Test func failBlocksTheComputerUntilSheDecidesWhatIsLeft() async throws {
    // The injected clock: the estimate line reports seconds since the list was set, and on the real
    // clock that read 「1 秒」 instead of 「0 秒」 whenever the suite ran under load (2026-11-19).
    let h = try await Harness(clock: ManualClock(at("2026-11-16", "10:00")))
    await h.doll.ownerSaid("買票、再訂餐廳")
    try await h.turn(Scripted("", todoSet("票買好了", "餐廳訂好了"), readCall, key("cmd+n")))
    try await h.turn(Scripted("", fail("要主人的信用卡驗證碼", tried: "填了卡號，跳出簡訊驗證"), key("Return")))
    #expect(try h.toolResults().suffix(2).map(\.text) == [
        "記下了：「票買好了」沒做成。你估整份清單 5 回合，實際到現在 1 回合、0 秒（記下了）。這一項的過程收起來了，之後只留你寫的原因、試過的和這份動作清單：key cmd+n。電腦工具先鎖著。現在決定剩下的（剩下：「餐廳訂好了」）：用 todo.set 重寫剩下的項目；或照原樣列剩下的，接著做下一項；或跟主人說了以後用 todo.set 給空的陣列結束清單。",
        "失敗：這則回覆前面已經用 todo.fail 收起這一項了，後面的呼叫沒有執行；看了結果下一回合再做。"])
    #expect(h.last.itemEnd == "失敗" && h.last.compacted == ["failed"] && h.last.todo == "失敗")
    // She gets the turn in which she decides; the computer stays locked meanwhile.
    #expect(await h.doll.followUp)
    try await h.turn(Scripted("", key("Return")))
    #expect(try h.toolResults().last!.text.hasPrefix("失敗：「票買好了」失敗了，act 先鎖著。") && h.cua.actions == ["hotkey"])
    #expect(h.last.footer.contains("- todo：失敗「票買好了」：電腦工具鎖著"))
    // The failed item's reason and what she tried stay (her call); the action list is its result.
    let msgs = await h.model.seen(2)
    #expect(msgs.contains { $0.toolCalls.first?.name == "todo.fail" && $0.toolCalls[0].arguments!.contains("填了卡號，跳出簡訊驗證") })
    #expect(!msgs.contains { !$0.extra.isEmpty })
    // She continues with the next item: in progress again, computer unlocked.
    try await h.turn(Scripted("", todoSet("餐廳訂好了"), key("cmd+t")))
    #expect(h.cua.args("hotkey").last?["keys"] as? [String] == ["cmd", "t"] && h.cua.actions.count == 2 && h.last.todo == "進行中")
}

@Test func anEmptyListAfterAFailureEndsItAndNothingWakesHer() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("買票")
    try await h.turn(Scripted("", todoSet("票買好了"), readCall, key("cmd+n")))
    try await h.turn(Scripted("", fail("網站上沒票了")))
    try await h.turn(Scripted("", dm("沒票了"), ("todo.set", [("items", "[]")])))
    #expect(try h.toolResults().last!.text == "清單結束了，現在待命中。" && h.last.todo == "待命中")
    #expect(await !h.doll.followUp)
    #expect(h.last.itemEnd == nil)
    #expect(try h.timeline().todo == TodoState())
}

@Test func waitingGetsNoTurnsUntilSomethingHappens() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("幫我登入")
    try await h.turn(Scripted("", todoSet("登入好了"), readCall, key("cmd+n")))
    try await h.turn(Scripted("", dm("要你的驗證碼"), todoWait("主人的驗證碼"), key("Return")))
    // The wait applies once the reply's calls ran: the key after it still ran.
    #expect(h.cua.actions == ["hotkey", "press_key"])
    #expect(h.last.todo == "等待中")
    #expect(await !h.doll.followUp)
    #expect(try h.timeline().todo.phase == .waiting)
    // His answer gives her the turn; the item stays 等待中 until she works on it (2026-11-20): her
    // computer call takes it up again and runs.
    await h.doll.ownerSaid("123456")
    try await h.turn(Scripted("", act(#"{"type": "123456"}"#)))
    #expect(h.reports[2].footer.contains("- todo：等待中「登入好了」") && h.cua.args("type_text").last?["text"] as? String == "123456")
    #expect(try await h.last.todo == "進行中")
    #expect(try await h.doll.followUp)
}

/// A harness whose context is near full after a read and one long thought of hers (`longThought`):
/// a small limit and big screenshots. Only the latest read keeps its screenshot, so reads alone never
/// fill it; her thoughts in the item's replies do, and a compaction drops them.
private func tightHarness(after: Int = 3, keep: Int = 2) async throws -> Harness {
    let h = try await Harness(tune: {
        $0.context.limit = 20_000
        $0.context.forceAfterTurns = after
        $0.context.forceKeepTurns = keep
        $0.computer.screenshotTokens = 5_000
    })
    await h.model.setImageTokens(5_000)
    return h
}
/// 9,000 tokens of her thinking: with one read in the request, past the warning share of the tight limit.
private let longThought = String(repeating: "想", count: 9_000)

@Test func nearFullSheCompactsWithProgressAndTheItemGoesOn() async throws {
    let h = try await tightHarness()
    await h.doll.ownerSaid("整理桌面")
    try await h.turn(Scripted("", todoSet("桌面只剩 notes.txt"), readCall))
    try await h.turn(Scripted(longThought, key("cmd+a")))
    try await h.turn(Scripted("", key("cmd+c")))
    let warned = h.last.footer
    #expect(warned.contains("- context 快滿了：用 todo.progress 寫下這一項做到哪裡，前面的過程就會收起來（留下你寫的進度和動作清單）；再 3 回合沒收，程式會自己收（留動作清單和最近 2 回合）。"))
    #expect(warned.contains("其中做事的過程") && !h.reports[0].footer.contains("快滿了"))
    try await h.turn(Scripted("", progress("選了全部、複製了；接下來要刪掉別的檔案")))
    #expect(h.last.compacted == ["progress"] && h.last.todo == "進行中")
    #expect(await h.doll.followUp)
    #expect(try h.toolResults().last!.text == "前面的過程收起來了，之後只留你寫的進度和這份動作清單：key cmd+a、key cmd+c。「桌面只剩 notes.txt」還在進行中，接著做。")
    try await h.turn(Scripted("", key("Delete")))
    let after = await h.model.seen(4)
    #expect(!h.reports[4].footer.contains("快滿了"))
    #expect(after.filter { !$0.images.isEmpty }.isEmpty && after.contains { $0.toolCalls.first?.name == "todo.progress" })
    // Then done: the whole item, the progress note included, compacts from the same start.
    try await h.turn(Scripted("", done("刪好了"), dm("刪好了")))
    try await h.turn(Scripted("", dm("好了")))
    let comps = try h.timeline().compactions
    #expect(comps.map(\.kind) == [.progress, .done] && comps[0].fromSeq == comps[1].fromSeq)
    #expect(try h.toolResults().last { $0.text.hasPrefix("完成") }!.text.contains("動作清單：key cmd+a、key cmd+c、key delete。"))
}

@Test func nearFullWithoutHerCompactionTheProgramForcesItKeepingTheLastTurns() async throws {
    let h = try await tightHarness()
    await h.doll.ownerSaid("整理桌面")
    try await h.turn(Scripted("", todoSet("桌面只剩 notes.txt"), readCall))   // turn 1
    try await h.turn(Scripted(longThought, key("a")))                                 // 2: past the warning share from turn 3
    for k in ["b", "c", "d", "e", "f"] { try await h.turn(Scripted("", key(k))) }
    let warnings = h.reports.map { $0.footer.contains("context 快滿了：用 todo.progress") }
    #expect(!h.reports[1].footer.contains("快滿了"))
    let first = warnings.firstIndex(of: true)!
    // Three warned turns, then the program compacts at the start of the next one.
    let forcedTurn = h.reports.firstIndex { $0.compacted == ["forced"] }!
    #expect(forcedTurn == first + 3)
    #expect(h.reports[first].footer.contains("再 3 回合") && h.reports[first + 2].footer.contains("再 1 回合"))
    let comp = try h.timeline().compactions.last!
    #expect(comp.kind == .forced && comp.text!.hasPrefix("【收起】context 快滿了，你沒有用 todo.progress 收") && comp.text!.contains("最近 2 回合還在下面"))
    let req = await h.model.seen(forcedTurn)
    // The block follows the span; her last two turns before it are intact, with their screenshots.
    let block = req.firstIndex { $0.content.contains("【收起】") }!
    let rest = Array(req[(block + 1)...])
    #expect(rest.filter { $0.role == "assistant" }.count == 2 && rest.contains { !$0.images.isEmpty })
    #expect(req[..<block].allSatisfy { $0.images.isEmpty })
    // Its action list covers the actions up to the cut only.
    let cutActions = forcedTurn - 3   // her replies before the kept two, the first (list and screenshot) without a key
    #expect(comp.text!.contains("到這裡做過的動作：" + ["a", "b", "c", "d", "e", "f"].prefix(cutActions).map { "key \($0)" }.joined(separator: "、") + "。"))
}

@Test func whatSheSaysGoesOutInTraditionalCharacters() async throws {
    // Only characters traditional writing does not use are replaced; shared ones stay.
    #expect(Traditional.fix("叫我来语音频道干嘛？").text == "叫我來語音頻道干嘛？")
    #expect(Traditional.fix("公里、皇后、干擾、台北、一隻貓、裡面、頭髮").changes.isEmpty)
    #expect(Traditional.fix("这个 app 的 TextEdit 说明 123").text == "這個 app 的 TextEdit 說明 123")
    // 么 is Big5-encodable, yet simplified here; 老么 stays.
    #expect(Traditional.fix("牙齒怎么了，什么事？").text == "牙齒怎麼了，什麼事？" && Traditional.fix("家裡的老么").changes.isEmpty)
    #expect(Traditional.fix("来来去去").changes == ["来→來"])
    let h = try await Harness()
    await h.doll.ownerSaid("在嗎")
    try await h.turn(Scripted("", dm("在，我这边没问题")))
    #expect(h.said == ["在，我這邊沒問題"])
    #expect(try h.toolResults().last!.text == "你寫了簡體字，送出去的時候程式改成繁體了：这→這、边→邊、没→沒、问→問、题→題。下次直接寫繁體。")
    // Nothing simplified: the result stays empty.
    await h.doll.ownerSaid("好")
    try await h.turn(Scripted("", dm("嗯，台北今天裡面外面都很冷")))
    #expect(h.said.last == "嗯，台北今天裡面外面都很冷")
    #expect(try h.toolResults().last!.text == "")
}

@Test func theStatusSaysTheYearAndTheWeekday() async throws {
    // 2026-11-15, real run: given only `11-15` she took the year for 2025 and the weekday from that.
    let h = try await Harness()
    await h.doll.ownerSaid("今天星期幾")
    try await h.turn(Scripted("", dm("我看看")))
    let c = Calendar.current.dateComponents([.year, .weekday], from: Date())
    let day = ["日", "一", "二", "三", "四", "五", "六"][c.weekday! - 1]
    #expect(h.last.footer.contains("- 現在 \(c.year!)-") && h.last.footer.contains("（星期\(day)）"))
}

@Test func nearFullMostlyConversationSaysCompactingWillNotHelp() async throws {
    let h = try await Harness(tune: { $0.context.limit = 1_000 })
    await h.doll.ownerSaid("聊聊")
    try await h.turn(Scripted("", dm("好")))
    #expect(h.last.footer.contains("- context 快滿了，但大部分是對話和之前留下的結果，不是正在做的這一項的過程；收起過程也省不了多少。"))
    #expect(h.last.footer.contains("其中做事的過程 0（截圖 0）") && h.last.footer.contains("／1,000（"))
}

@Test func atTheLimitTheOldestConversationBecomesASummaryWrittenByASeparateRequest() async throws {
    let h = try await Harness(tune: { $0.context.limit = 1_000 })       // the system prompt alone is over it
    await h.doll.ownerSaid("我叫小明，記得叫我小明")
    try await h.turn(Scripted("", dm("好，小明")))                       // request 0: nothing before her first line to collapse
    #expect(h.last.compacted.isEmpty && h.logs.contains { $0.contains("WARN") && $0.contains("no conversation") })
    // The next line: everything before it is collapsed by a clean request (1), then her turn (2).
    await h.doll.ownerSaid("今天天氣不錯")
    try await h.turn(Scripted("主人說他叫小明，要你叫他小明；你答應了。"), Scripted("", dm("是啊")))
    let ask = await h.model.seen(1), turn = await h.model.seen(2)
    #expect(ask.count == 2 && ask[0].content == Doll.talkInstructions)
    #expect(ask[1].content.contains("我叫小明，記得叫我小明") && ask[1].content.contains("discord.dm") && !ask[1].content.contains("今天天氣不錯"))
    #expect(h.last.compacted == ["talk"])
    // Her request: the old lines are gone, the summary block stands in their place, the new line follows.
    #expect(!turn.contains { $0.content.contains("記得叫我小明") || $0.content == "好，小明" })
    let block = try #require(turn.firstIndex { $0.content.contains("程式] 【之前的對話】") && $0.content.hasSuffix("主人說他叫小明，要你叫他小明；你答應了。") })
    #expect(block == 1 && turn[2].content.contains("今天天氣不錯"))
    // Recorded: a restart sends the same; the originals are still in the file and the index.
    #expect(try h.timeline().compactions.map(\.kind) == [.talk])
    #expect(try h.events().contains { $0.text == "我叫小明，記得叫我小明" })
    // A summary request that fails leaves everything as it was; her turn still runs.
    await h.doll.ownerSaid("晚餐吃什麼")
    try await h.turn(Scripted(""), Scripted("", dm("拉麵")))
    #expect(h.last.compacted.isEmpty && h.logs.contains { $0.contains("ERROR") && $0.contains("was not collapsed") })
    #expect(h.said.last == "拉麵")
    #expect(try h.timeline().compactions.count == 1)
    // Never into the item in progress: with an item running, only what came before it can go.
    await h.doll.ownerSaid("幫我存檔")
    try await h.turn(Scripted("聊了天氣。"), Scripted("", todoSet("notes 存好檔"), readCall))
    try await h.turn(Scripted("聊了晚餐。"), Scripted("", key("cmd+s")))   // what was left before the item
    try await h.turn(Scripted("", key("cmd+w")))                        // nothing before the item left: no summary request
    #expect(h.logs.contains { $0.contains("no conversation before the item in progress") })
    let t = try h.timeline()
    #expect(t.compactions.filter { $0.kind == .talk }.count == 3)
    // One rolling summary: each request for one carries the one before it, and her request shows only the newest.
    let second = await h.model.requests.first { $0.messages.count == 2 && $0.messages[1].content.contains("【更早的摘要】") }
    #expect(second?.messages[1].content.contains("主人說他叫小明，要你叫他小明；你答應了。") == true)
    let lastRequest = await h.model.requests.last!.messages
    #expect(lastRequest.filter { $0.content.contains("程式] 【之前的對話】") }.map { $0.content.hasSuffix("\n聊了晚餐。") } == [true])
    #expect(t.compactions.allSatisfy { $0.toSeq <= t.todo.startSeq })
    // The item's own steps are still in her request.
    #expect(await h.model.requests.last!.messages.contains { $0.content.contains("幫我存檔") })
}

@Test func atTheLimitAListWaitingIsCutIntoItsOwnPartStaysAndHisAnswerResolvesIt() async throws {
    // 2026-11-21, owner (soak run: a list waiting 20 h held the whole day's conversation in the request).
    let h = try await Harness(tune: { $0.context.limit = 1_000 })       // the system prompt alone is over it
    await h.doll.ownerSaid("幫我把報告寄給王經理")
    try await h.turn(Scripted("", todoSet("報告寄給王經理了", "跟主人說寄好了"), readCall))
    try await h.turn(Scripted("", key("cmd+n")))
    try await h.turn(Scripted("", dm("王經理的信箱是哪個？"), todoWait("等主人給王經理的信箱")))
    #expect(h.last.todo == "等待中" && h.last.compacted.isEmpty)
    // A line while it waits: the conversation inside the list's span is collapsed.
    await h.doll.ownerSaid("今天好熱")
    try await h.turn(Scripted("主人要你把報告寄給王經理；你問了他王經理的信箱。"), Scripted("", dm("對啊，好熱")))
    #expect(h.last.compacted == ["talk"])
    let t = try h.timeline()
    let talk = try #require(t.compactions.last)
    #expect(talk.kind == .talk && talk.toSeq > t.todo.startSeq)
    // What she sees: the line the list answers, her todo calls with what she said beside them and their
    // results, the summary, the item's actions; not her other calls, the window read, or their results.
    let seen = await h.model.requests.last!.messages
    let calls = seen.flatMap(\.toolCalls).map(\.name)
    #expect(calls == ["todo.set", "discord.dm", "todo.wait"])
    #expect(seen.last!.content.contains("其中做事的過程 0（截圖 0）"))
    #expect(seen[1].content.hasSuffix("] 幫我把報告寄給王經理"))
    #expect(seen[2].toolCalls.first?.arguments?.contains("跟主人說寄好了") == true && seen[3].role == "tool" && seen[3].content.contains("todo 列好了"))
    #expect(seen[4].toolCalls.map(\.name) == ["discord.dm", "todo.wait"] && seen[4].toolCalls[0].arguments?.contains("王經理的信箱是哪個？") == true)
    #expect(seen[5].role == "tool" && seen[6].role == "tool" && seen[6].content.contains("就是等待中"))
    #expect(seen[7].content.contains("程式] 【之前的對話】") && seen[7].content.hasSuffix("你問了他王經理的信箱。"))
    #expect(seen[8].content.hasSuffix("【收起】上面那段收起來的對話裡，「報告寄給王經理了」做過的動作：\(Doll.actionList(["key cmd+n"]))。"))
    #expect(seen[9].content.hasSuffix("] 今天好熱"))
    #expect(!seen.contains { $0.role == "tool" && $0.content.contains("AXWindow") })
    #expect(seen.last!.content.contains("- todo：等待中「報告寄給王經理了」（有人說話才會再有回合；接著做它就回到進行中，沒碰它就繼續等）；後面還有：「跟主人說寄好了」"))
    // His answer: she takes the item up and finishes it.
    await h.doll.ownerSaid("wang@example.com")
    try await h.turn(Scripted("聊了天氣。"), Scripted("", readCall))
    #expect(h.last.todo == "進行中")
    try await h.turn(Scripted("", done("寄給 wang@example.com 了")))
    #expect(try h.toolResults().last!.text.contains("下一項「跟主人說寄好了」現在進行中"))
    #expect(try h.timeline().todo.current == "跟主人說寄好了")
}

@Test func atTheLimitAListWaitingAsideKeepsItsPartAndHerOwnStepInProgressIsNotCutInto() async throws {
    let (h, sub) = try await waitingOnTheOwner(tune: { $0.context.limit = 1_000 })
    await h.doll.ownerSaid("今天好熱")
    try await h.turn(Scripted("主人要你問房東什麼時候來修水管；你問了他房東的電話。"), Scripted("", dm("對啊，好熱")))
    #expect(h.last.compacted == ["talk"])
    // Her own step beside it: the waiting list's part is still in her request.
    try await h.turn(Scripted("", todoSetSub(sub, ownStep)))
    try await h.turn(Scripted("", recallCall))
    let seen = await h.model.requests.last!.messages
    #expect(seen[1].content.hasSuffix("] 幫我問房東什麼時候來修水管"))
    #expect(seen[2].toolCalls.map(\.name) == ["todo.set", "discord.dm", "todo.wait"] && seen[2].toolCalls[1].arguments?.contains("房東的電話是幾號？") == true)
    #expect(seen.last!.content.contains("旁邊放著等待中的「房東的時間問到了」"))
    // His answer brings the waiting list in hand for the turn; her own step is in progress aside, and
    // nothing after its start is collapsed.
    let own = try await h.doll.todo.startSeq
    await h.doll.ownerSaid("電話是 0912-345-678")
    try await h.turn(Scripted("", dm("好，我打給他"), done("問到房東的電話", evidence: nil)))
    try await h.turn(Scripted("", done("問到房東的電話", evidence: nil)))
    #expect(try h.toolResults().last!.text.hasSuffix("清單做完了。放在旁邊的「\(ownStep)」回到你手上（進行中）。"))
    #expect(try h.timeline().compactions.filter { $0.kind == .talk }.allSatisfy { $0.toSeq < own })
    #expect(h.logs.contains { $0.contains("no conversation before the item in progress") })
}

@Test func compactionsAndTheTodoListSurviveARestartByteForByte() async throws {
    let h = try await tightHarness()
    await h.doll.ownerSaid("兩件事")
    try await h.turn(Scripted("", todoSet("A 好了", "B 好了"), readCall))
    try await h.turn(Scripted("", key("a")))
    try await h.turn(Scripted("", done("A 做完")))
    try await h.turn(Scripted(longThought, key("b")))
    for k in ["c", "d", "e", "f", "g"] { try await h.turn(Scripted("", key(k))) }
    #expect(try h.timeline().compactions.map(\.kind) == [.done, .forced])
    let events = await h.doll.timelineEvents()
    let before = try await h.doll.context(events)
    let again = try await restarted(h, [Scripted("", dm("回來了"))])
    let after = try await again.doll.context(await again.doll.timelineEvents())
    #expect(try ChatMessage.encoder.encode(after) == ChatMessage.encoder.encode(before))
    #expect(await again.doll.todo == h.doll.todo)
    let types = try String(contentsOf: try h.timeline().file, encoding: .utf8).split(separator: "\n")
        .map { try JSONSerialization.jsonObject(with: Data($0.utf8)) as! [String: Any] }.map { $0["type"] as! String }
    #expect(Set(types) == ["event", "footer", "turn", "todo", "compact"])
    await h.doll.stop()
}


// memory strength (2026-11-15) --------------------------------------------------------------

/// Noon, local time, `days` after 2026-10-01: the tests' clock (strength takes "now" as a parameter).
private func day(_ days: Double) -> Double {
    Calendar.current.date(from: DateComponents(year: 2026, month: 10, day: 1, hour: 12))!.timeIntervalSince1970 + days * 86400
}

@Test func strengthIsACTRBaseLevelActivationOverTheEvidence() {
    func b(_ ev: [Double], _ now: Double) -> Double {
        Memory.activation(ev.map(day), now: day(now), decay: 0.5, unitDays: 7)
    }
    // Seen once: 0 on the day, then -d ln(1 + t/7).
    #expect(b([0], 0) == 0)
    #expect(abs(b([0], 7) - (-0.5 * log(2.0))) < 1e-9)
    #expect(b([0], 24) >= -0.75 && b([0], 25) < -0.75)
    // Each piece of evidence pulls it back up; days 0,7,14 on day 14: ln(1 + 2^-.5 + 3^-.5).
    #expect(abs(b([0, 7, 14], 14) - log(1 + pow(2, -0.5) + pow(3, -0.5))) < 1e-9)
    #expect(b([0, 7], 30) > b([0], 30))
    // n pieces are at most ln n: two can never reach the settled line 0.75, three a week apart can.
    #expect(b([0, 1], 1) < 0.75 && b([0, 7, 14], 14) >= 0.75 && b([0, 14, 28], 28) < 0.75)
    // Evidence later than now counts as today's.
    #expect(b([3], 0) == 0)
}

@Test func confirmCountsOnePieceOfEvidencePerDayAndSettlesAboveTheLine() async throws {
    let h = try await Harness()
    let cfg = try consolidationConfig(h)
    let memory = try Memory(config: cfg.memory, root: h.dir.appending(path: "memory"), store: h.store, embedder: FakeEmbedder(), box: try testBox(h.dir))
    let tea = try memory.write("主人喜歡喝無糖綠茶", folder: .public, sourceSeq: 3, tier: .consolidated, kind: nil, now: day(0))
    // A file written before 2026-11-15 has no evidence key: its one piece is created_ts.
    let raw = try String(contentsOf: h.dir.appending(path: "memory/public/\(tea).md"), encoding: .utf8)
    #expect(!raw.contains("evidence") && !raw.contains("deep") && !raw.contains("settled_ts"))
    #expect(try memory.file(tea)!.file.allEvidence == [Evidence(ts: day(0), seq: 3)])
    // The same calendar day again: not counted, nothing written.
    #expect(try memory.confirm(tea, at: day(0) + 3600, seq: 9) == .sameDay(Memory.day(day(0))))
    #expect(try String(contentsOf: h.dir.appending(path: "memory/public/\(tea).md"), encoding: .utf8) == raw)
    guard case .counted(let s7, false) = try memory.confirm(tea, at: day(7), seq: 20) else { Issue.record("day 7 not counted"); return }
    #expect(abs(s7 - log(1 + pow(2, -0.5))) < 1e-9)
    #expect(try memory.confirm(tea, at: day(7) + 600, seq: 21) == .sameDay(Memory.day(day(7))))
    guard case .counted(_, true) = try memory.confirm(tea, at: day(14), seq: 30) else { Issue.record("day 14 did not settle"); return }
    let f = try memory.file(tea)!.file
    #expect(f.evidence == [Evidence(ts: day(0), seq: 3), Evidence(ts: day(7), seq: 20), Evidence(ts: day(14), seq: 30)])
    #expect(f.settled == day(14))
    #expect(try Memory.parseFile(f.text) == f)
    // Settled stays settled: a later piece of evidence keeps the first settled time.
    _ = try memory.confirm(tea, at: day(100), seq: 40)
    #expect(try memory.file(tea)!.file.settled == day(14))
    // Only tiers with a strength; never a retired memory.
    let shortcut = try memory.write("TextEdit：File › Save＝⌘S", folder: .public, sourceSeq: 0, tier: .program, kind: nil, now: day(0))
    #expect(throws: MemoryError.self) { try memory.confirm(shortcut, at: day(3), seq: 1) }
    #expect(throws: MemoryError.self) { try memory.write("x", folder: .public, sourceSeq: 0, tier: .program, kind: nil, deep: true, now: day(0)) }
    try memory.retire(tea, Retirement(ts: day(101), supersededBy: shortcut, reason: "x"))
    #expect(throws: MemoryError.self) { try memory.confirm(tea, at: day(102), seq: 1) }
    #expect(throws: MemoryError.self) { try memory.setDeep(tea, true) }
    // A malformed strength key is an error, not a silent default.
    for bad in ["deep = yes", "evidence = []", "evidence = [1]", "settled_ts = soon"] {
        #expect(throws: MemoryError.self, "\(bad)") {
            try Memory.parseFile("+++\nkind = \"\"\nconfidence = 0.5\ncreated_ts = 1\nsource_event_seq = 1\nsource_tier = \"owner\"\n\(bad)\n+++\nx")
        }
    }
}

@Test func theConfirmOperationIsValidatedStrictly() throws {
    let known: Set<String> = ["m1", "m2"]
    func plan(_ ops: String) -> String { "```json\n{\"entities\": [], \"operations\": [\(ops)]}\n```" }
    let ok = try Consolidation.parse(plan(#"{"op": "confirm", "id": "m1", "task": 2}, {"op": "confirm", "id": "m2", "task": 0}"#),
                                     known: known, registry: [], tasks: [0, 1, 2])
    #expect(ok.operations == [.confirm(id: "m1", task: 2), .confirm(id: "m2", task: 0)])
    let update = #"{"op": "update", "id": "m1", "text": "新的", "entities": []}"#
    let retire = #"{"op": "retire", "id": "m1", "superseded_by": "m2", "reason": "舊了"}"#
    let bad = [
        #"{"op": "confirm", "id": "m1"}"#,                                  // no task
        #"{"op": "confirm", "id": "m1", "task": 2, "day": "x"}"#,           // an extra field
        #"{"op": "confirm", "id": "m1", "task": "2"}"#,                     // a string
        #"{"op": "confirm", "id": "m1", "task": 1.5}"#,                     // not an integer
        #"{"op": "confirm", "id": "m1", "task": true}"#,                    // a boolean
        #"{"op": "confirm", "id": "m1", "task": 9}"#,                       // no such task
        #"{"op": "confirm", "id": "m9", "task": 1}"#,                       // an id the request did not show
        #"{"op": "confirm", "id": "m1", "task": 1}, {"op": "confirm", "id": "m1", "task": 2}"#,
        #"{"op": "confirm", "id": "m1", "task": 1}, "# + update,         // confirmed and updated
        update + #", {"op": "confirm", "id": "m1", "task": 1}"#,
        retire + #", {"op": "confirm", "id": "m1", "task": 1}"#,         // retired and confirmed
    ]
    for ops in bad {
        #expect(throws: ConsolidationError.self, "\(ops)") { try Consolidation.parse(plan(ops), known: known, registry: [], tasks: [0, 1, 2]) }
    }
    // The instructions say when to use it.
    #expect(Consolidation.instructions.contains(#"{"op": "confirm", "id": "既有記憶的 id", "task": 2}"#))
    #expect(Consolidation.instructions.contains("只是今天提到相關的話題"))
}

@Test func aConsolidationConfirmIsEvidenceFromItsTasksEventOncePerDay() async throws {
    let h = try await Harness()
    let cfg = try consolidationConfig(h)
    let memory = try Memory(config: cfg.memory, root: h.dir.appending(path: "memory"), store: h.store, embedder: FakeEmbedder(), box: try testBox(h.dir))
    let now = Date().timeIntervalSince1970
    let old = try memory.write("主人喜歡喝無糖綠茶", folder: .public, sourceSeq: 1, tier: .owner, kind: nil, now: now - 3 * 86400)
    let fresh = try memory.write("主人喜歡喝無糖紅茶", folder: .public, sourceSeq: 1, tier: .doll, kind: nil, now: now)
    await h.doll.ownerSaid("幫我買無糖綠茶和無糖紅茶")
    try await h.turn(Scripted("", dm("好")))
    let owner = try h.events().last { $0.kind == .owner }!
    let reply = "```json\n{\"entities\": [], \"operations\": [{\"op\": \"confirm\", \"id\": \"\(old)\", \"task\": 1}, {\"op\": \"confirm\", \"id\": \"\(fresh)\", \"task\": 1}]}\n```"
    let r = try await Consolidation.run(config: cfg, root: root, model: FakeModel([Scripted(reply), Scripted("摘要。")]),
                                        embedder: FakeEmbedder(), keys: testKeys, now: now + 60)
    #expect(r.applied.contains { $0.hasPrefix("confirm \(old) (task 1, \(Memory.day(owner.ts)), event \(owner.seq!)): strength ") })
    #expect(r.applied.contains("confirm \(fresh) (task 1): not counted, it already has evidence on \(Memory.day(now))"))
    #expect(try memoryFilesByID(h)[old]!.file.evidence == [Evidence(ts: now - 3 * 86400, seq: 1), Evidence(ts: owner.ts, seq: owner.seq!)])
    #expect(try memoryFilesByID(h)[fresh]!.file.evidence.isEmpty)
}

@Test func aFadedMemoryIsNotAttachedButRecallFindsItAndSettledOrDeepOnesStay() async throws {
    let h = try await Harness()
    let cfg = try consolidationConfig(h)
    let memory = try Memory(config: cfg.memory, root: h.dir.appending(path: "memory"), store: h.store, embedder: FakeEmbedder(), box: try testBox(h.dir))
    let text = "主人的公司在信義區"
    let faded = try memory.write(text, folder: .public, sourceSeq: 1, tier: .consolidated, kind: nil, now: day(0))
    let settled = try memory.write(text + "。", folder: .public, sourceSeq: 2, tier: .consolidated, kind: nil, now: day(0))
    _ = try memory.confirm(settled, at: day(7), seq: 3)
    _ = try memory.confirm(settled, at: day(14), seq: 4)
    let deep = try memory.write(text + "！", folder: .public, sourceSeq: 3, tier: .owner, kind: nil, deep: true, now: day(0))
    let plain = try memory.write(text + "？", folder: .public, sourceSeq: 4, tier: .owner, kind: nil, now: day(0))
    let selector = Selector(memory: memory, config: cfg.select)
    func attach(_ now: Double) throws -> SelectionReport {
        try selector.run(.init(label: "測試", query: text, app: nil), rule: .attach, visible: [.public],
                         tiers: Memory.strengthTiers.union([.selfReported]), today: nil, now: now) { c in
            try memory.attachable(c.id) == nil ? nil : c.key
        }
    }
    // Day 3: all fresh, all attached (her plain memory is not attachable: not consolidated, not deep).
    #expect(Set(try attach(day(3)).selected.map(\.id)) == [faded, settled, deep])
    // Day 24 still attached, day 25 faded below -0.75: left out, logged; the settled and the deep stay.
    #expect(try attach(day(24)).selected.map(\.id).contains(faded))
    let late = try attach(day(365))
    #expect(Set(late.selected.map(\.id)) == [settled, deep] && late.faded == [faded])
    #expect(late.line.contains("faded 1 [\(faded)]"))
    // Faded ranks lower before it drops out: strength_weight × strength below 0.
    func score(_ now: Double) throws -> Double { try attach(now).ranked.first { $0.id == faded }!.score }
    func strength(_ now: Double) -> Double { Memory.activation([day(0)], now: now, decay: 0.5, unitDays: 7) }
    #expect(abs((try score(day(3)) - score(day(20))) - 0.1 * (strength(day(3)) - strength(day(20)))) < 1e-9)
    // Recall still finds it: strength takes no part.
    let recall = try selector.run(.init(label: "recall", query: text, app: nil), rule: .recall, visible: [.public], tiers: nil,
                                  today: nil, now: day(365)) { $0.key }
    #expect(recall.selected.map(\.id).contains(faded))
    let near = try selector.run(.init(label: "recall", query: text, app: nil), rule: .recall, visible: [.public], tiers: nil,
                                today: nil, now: day(0)) { $0.key }
    #expect(recall.selected.map(\.id) == near.selected.map(\.id))
    _ = plain
}

@Test func herRememberCanMarkDeepAndTheDashboardListsMarksAndUnmarks() async throws {
    struct Item: Encodable { let text: String; let folder: String; let deep: Bool? }
    let items = String(decoding: try JSONEncoder().encode([Item(text: "主人對花生過敏", folder: "public", deep: true),
                                                          Item(text: "天空是藍的", folder: "public", deep: nil)]), as: UTF8.self)
    let h = try await Harness([Scripted("", ("remember", [("items", items)]))])
    let d = dashboard(h)
    await h.doll.ownerSaid("記住我對花生過敏")
    try await h.turn()
    #expect(try h.toolResults().last!.text.contains("，deep：不會淡）"))
    let files = try memoryFilesByID(h)
    let peanut = files.first { $0.value.file.body == "主人對花生過敏" }!.key
    let sky = files.first { $0.value.file.body == "天空是藍的" }!.key
    #expect(files[peanut]!.file.deep && !files[sky]!.file.deep)
    var m = try json(DashboardMemory.self, await d.respond(method: "GET", target: "/api/memory"))
    #expect(m.deep.map(\.id) == [peanut])
    let skyEntry = m.folders.flatMap(\.entries).first { $0.id == sky }!
    #expect(skyEntry.strength == 0 || (skyEntry.strength! < 0 && skyEntry.strength! > -0.01))
    #expect(!skyEntry.faded && skyEntry.evidence.count == 1)
    // Mark another, unmark hers: through Memory, the file changes, the index stays.
    let indexed = try h.store.countMemoryIndex()
    #expect(await d.respond(method: "POST", target: "/api/memory/deep/\(sky)").status == 200)
    #expect(await d.respond(method: "POST", target: "/api/memory/undeep/\(peanut)").status == 200)
    #expect(try memoryFilesByID(h)[sky]!.file.deep && !(try memoryFilesByID(h)[peanut]!.file.deep))
    #expect(try h.store.countMemoryIndex() == indexed)
    m = try json(DashboardMemory.self, await d.respond(method: "GET", target: "/api/memory"))
    #expect(m.deep.map(\.id) == [sky])
    // Unknown ids, paths and memories without a strength are refused; GET is not allowed.
    #expect(await d.respond(method: "POST", target: "/api/memory/deep/9-nothere").status == 409)
    #expect(await d.respond(method: "POST", target: "/api/memory/deep/..%2Fx").status == 400)
    #expect(await d.respond(method: "GET", target: "/api/memory/deep/\(sky)").status == 405)
    #expect(DashboardPage.html.contains("/api/memory/undeep/") && DashboardPage.html.contains("標成 deep"))
}

@Test func strengthConfigHasDefaultsAndIsValidated() throws {
    let c = try exampleConfig()
    #expect(c.memory.strengthDecay == 0.5 && c.memory.strengthUnitDays == 7 && c.memory.strengthSettled == 0.75)
    #expect(c.select.strengthFloor == -0.75 && c.select.strengthWeight == 0.1)
    let file = FileManager.default.temporaryDirectory.appending(path: "strength-\(UUID().uuidString).toml")
    let head = "[llm]\nbase_url = \"http://x\"\nmodel = \"m\"\n[memory]\n"
        + SourceTier.allCases.map { "confidence_\($0.rawValue) = 0.5\n" }.joined()
    // Absent keys: the defaults (a config written before 2026-11-15 boots unchanged).
    try head.write(to: file, atomically: true, encoding: .utf8)
    let d = try Config.load(file)
    #expect(d.memory.strengthDecay == 0.5 && d.memory.strengthUnitDays == 7 && d.memory.strengthSettled == 0.75
            && d.select.strengthFloor == -0.75 && d.select.strengthWeight == 0.1)
    for bad in ["strength_decay = 0", "strength_unit_days = -1", "strength_settled = 0"] {
        try (head + "\(bad)\n").write(to: file, atomically: true, encoding: .utf8)
        #expect(throws: TOMLError.self, "\(bad)") { try Config.load(file) }
    }
    for bad in ["strength_floor = 0", "strength_weight = -0.1"] {
        try (head + "[select]\n\(bad)\n").write(to: file, atomically: true, encoding: .utf8)
        #expect(throws: TOMLError.self, "\(bad)") { try Config.load(file) }
    }
}


/// End to end with the real embedding model (2026-11-15): three sleeps over two weeks with scripted
/// consolidation plans, the clock moved by hand; then auto-attach and recall at later days. An
/// unconfirmed memory drops out of auto-attach after about three weeks, one confirmed on three
/// different days settles and stays, a deep one never fades, recall finds all. Prints its table.
@Test func endToEndUnconfirmedMemoriesFadeConfirmedAndDeepOnesStayRecallFindsAll() async throws {
    let embedder = try await qwen3.value
    let dir = FileManager.default.temporaryDirectory.appending(path: "dollcore-strength-\(UUID().uuidString)")
    var cfg = try exampleConfig()
    cfg.core.dataDir = dir.path
    cfg.memory.dir = dir.appending(path: "memory").path
    let timeline = dir.appending(path: "timeline")
    func live(_ n: Double, _ lines: [String]) throws {
        let t = try Timeline(dir: timeline, box: try testBox(timeline.deletingLastPathComponent()))
        for (i, line) in lines.enumerated() {
            var e = Event(.owner, .owner, line)
            e.ts = day(n) + Double(i) * 120
            try t.append(e)
            var r = Event(.doll, .internal, "好，記住了")
            r.ts = e.ts + 30
            try t.append(r)
        }
    }
    func sleep(_ n: Double, _ ops: String) async throws -> [String] {
        let reply = "```json\n{\"entities\": [], \"operations\": [\(ops)]}\n```"
        return try await Consolidation.run(config: cfg, root: root, model: FakeModel([Scripted(reply), Scripted("摘要。")]),
                                           embedder: embedder, keys: testKeys, now: day(n) + 10 * 3600).applied
    }
    func ids() throws -> [String: String] {
        let pub = dir.appending(path: "memory/public")
        var out: [String: String] = [:]
        for name in try FileManager.default.contentsOfDirectory(atPath: pub.path) where name.hasSuffix(".md") {
            out[try Memory.parseFile(String(contentsOf: pub.appending(path: name), encoding: .utf8)).body] = String(name.dropLast(3))
        }
        return out
    }
    var log: [String] = []
    // Day 0: the owner says two things; sleep writes them. She marks one of her own deep.
    try live(0, ["我喜歡喝無糖綠茶", "我們公司搬到信義區了"])
    log += try await sleep(0, #"{"op": "add", "ref": "n1", "text": "主人喜歡喝無糖綠茶", "folder": "public", "kind": "", "entities": []}, {"op": "add", "ref": "n2", "text": "主人的公司在信義區", "folder": "public", "kind": "", "entities": []}"#)
    do {
        let memory = try Memory(config: cfg.memory, root: URL(filePath: cfg.memory.dir), store: try Store(path: dir.appending(path: "doll.db")),
                                embedder: embedder, box: try testBox(dir))
        try memory.write("主人對花生過敏", folder: .public, sourceSeq: 1, tier: .owner, kind: nil, deep: true, now: day(0) + 60)
    }
    let tea = try ids()["主人喜歡喝無糖綠茶"]!, office = try ids()["主人的公司在信義區"]!, peanut = try ids()["主人對花生過敏"]!
    // Days 7 and 14: the tea again, from new events; the office never comes up again.
    try live(7, ["幫我買一箱無糖綠茶"])
    log += try await sleep(7, #"{"op": "confirm", "id": "\#(tea)", "task": 1}"#)
    try live(14, ["綠茶一樣要無糖的喔"])
    log += try await sleep(14, #"{"op": "confirm", "id": "\#(tea)", "task": 1}"#)
    let memory = try Memory(config: cfg.memory, root: URL(filePath: cfg.memory.dir), store: try Store(path: dir.appending(path: "doll.db")),
                            embedder: embedder, box: try testBox(dir))
    let selector = Selector(memory: memory, config: cfg.select)
    let name = [tea: "tea", office: "office", peanut: "peanut"]
    let queries = ["tea": "下午幫我買一杯無糖綠茶", "office": "我們公司在哪一區", "peanut": "這塊蛋糕有花生，我能吃嗎"]
    var attached: [Double: [String: [String]]] = [:]
    for d in [3.0, 20, 30, 60, 180] {
        for (what, q) in queries.sorted(by: { $0.key < $1.key }) {
            let a = try selector.run(.init(label: "day \(Int(d))", query: q, app: nil), rule: .attach, visible: [.public],
                                     tiers: Memory.strengthTiers.union([.selfReported]), today: nil, now: day(d)) { c in
                try memory.attachable(c.id) == nil ? nil : c.key
            }
            let r = try selector.run(.init(label: "recall", query: q, app: nil), rule: .recall, visible: [.public], tiers: nil,
                                     today: nil, now: day(d)) { $0.key }
            let cos = a.ranked.map { "\(name[$0.id] ?? $0.id) \(String(format: "%.2f", $0.cosine))" }
            attached[d, default: [:]][what] = a.selected.map { name[$0.id] ?? $0.id }
            log.append("day \(Int(d)) 「\(q)」 attach \(a.selected.map { name[$0.id] ?? $0.id }) faded \(a.faded.map { name[$0] ?? $0 }) "
                       + "(cos \(cos)); recall \(r.selected.map { name[$0.id] ?? $0.id })")
            #expect(r.selected.contains { $0.id == [ "tea": tea, "office": office, "peanut": peanut ][what]! }, "recall day \(d) \(what)")
        }
    }
    print("END-TO-END MEMORY STRENGTH\n" + log.joined(separator: "\n"))
    let f = try memory.file(tea)!.file
    #expect(f.evidence.count == 3 && f.settled == f.evidence[2].ts)
    #expect(attached[3]!["office"]!.contains("office") && attached[20]!["office"]!.contains("office"))
    #expect(!attached[30]!["office"]!.contains("office") && !attached[180]!["office"]!.contains("office"))
    #expect(attached[180]!["tea"]!.contains("tea") && attached[180]!["peanut"]!.contains("peanut"))
}

// calendar (2026-11-15) ------------------------------------------------------------

/// "Now" the test sets; a sleeper wakes when the time passes its instant.
final class ManualClock: DollClock, Sendable {
    struct Sleeper { let id: UUID; let t: Double; let c: CheckedContinuation<Void, any Error> }
    let state: Mutex<(now: Double, sleepers: [Sleeper])>
    init(_ t: Double) { state = Mutex((t, [])) }
    func now() -> Double { state.withLock { $0.now } }
    var sleepers: Int { state.withLock { $0.sleepers.count } }
    func sleep(until t: Double) async throws {
        let id = UUID()
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (c: CheckedContinuation<Void, any Error>) in
                let ready: Bool? = state.withLock { s in
                    if Task.isCancelled { return nil }
                    if s.now >= t { return true }
                    s.sleepers.append(Sleeper(id: id, t: t, c: c))
                    return false
                }
                if ready == nil { c.resume(throwing: CancellationError()) } else if ready! { c.resume() }
            }
        } onCancel: {
            let s = state.withLock { s -> Sleeper? in
                guard let i = s.sleepers.firstIndex(where: { $0.id == id }) else { return nil }
                return s.sleepers.remove(at: i)
            }
            s?.c.resume(throwing: CancellationError())
        }
    }
    func set(_ t: Double) {
        let due = state.withLock { s -> [Sleeper] in
            s.now = t
            let d = s.sleepers.filter { $0.t <= t }
            s.sleepers.removeAll { $0.t <= t }
            return d
        }
        for s in due { s.c.resume() }
    }
}

/// Local time `2026-11-16 10:00` (a Monday) as seconds.
private func at(_ day: String, _ time: String) -> Double { Day(day)!.at(TimeOfDay(time)!) }

private func cal(_ name: String, _ params: [(String, String)]) -> (String, [(String, String)]) { (name, params) }

@Test func calendarDaysAndWeekdaysAreLocalAndStrict() {
    #expect(Day("2026-11-16")!.label == "2026-11-16（星期一）" && Day("2026-11-22")!.weekday == 7)
    #expect(Day("2026-11-31") == nil && Day("2026-1-05") == nil && Day("下週三") == nil)
    #expect(Day("2026-12-31")!.adding(1).description == "2027-01-01")
    #expect(TimeOfDay("9:05")?.description == "09:05" && TimeOfDay("24:00") == nil && TimeOfDay("10") == nil)
    #expect(Repeat("1,3,5") == .weekdays([1, 3, 5]) && Repeat("daily") == .daily && Repeat("0,8") == nil)
}

@Test func calendarDailyAndWeekdaySeriesWithOneDayChangesLeaveTheOtherDays() async throws {
    let h = try await Harness(clock: ManualClock(at("2026-11-16", "08:00")))
    try await h.turn(Scripted("", cal("calendar.add", [("title", "吃藥"), ("date", "2026-11-16"), ("start", "09:00"), ("end", "09:10"), ("repeat", "daily")]),
                              cal("calendar.add", [("title", "主人上健身房"), ("date", "2026-11-16"), ("start", "19:00"), ("repeat", "2,4")]),
                              cal("calendar.change", [("id", "1"), ("on", "2026-11-17"), ("start", "15:00")]),
                              cal("calendar.delete", [("id", "1"), ("on", "2026-11-18")]),
                              cal("calendar.list", [("from", "2026-11-16"), ("to", "2026-11-19")])))
    let r = try h.toolResults().map(\.text)
    #expect(r[0] == "加好了：從 2026-11-16（星期一） 09:00–09:10 吃藥（#1，每天）。下一次是 2026-11-16（星期一） 09:00，到時候會給你一個回合。")
    // A weekday series echoes its first real day (Tuesday), not the date she gave.
    #expect(r[1].hasPrefix("加好了：從 2026-11-17（星期二） 19:00 主人上健身房（#2，每週二、四）"))
    #expect(r[2] == "改好了：2026-11-17（星期二） 15:00–15:10 吃藥（#1，每天，這天改過）。只改 2026-11-17（星期二） 這一次，其他天照舊。下一次是 2026-11-16（星期一） 09:00，到時候會給你一個回合。")
    #expect(r[3].hasPrefix("2026-11-18（星期三） 這一次取消了，其他天照舊"))
    #expect(r[4] == """
        2026-11-16（星期一） 到 2026-11-19（星期四）：
        2026-11-16（星期一）
        - 09:00–09:10 吃藥（#1，每天）
        2026-11-17（星期二）
        - 15:00–15:10 吃藥（#1，每天，這天改過）
        - 19:00 主人上健身房（#2，每週二、四）
        2026-11-19（星期四）
        - 09:00–09:10 吃藥（#1，每天）
        - 19:00 主人上健身房（#2，每週二、四）
        """)
    // Refusals say what to do.
    try await h.turn(Scripted("", cal("calendar.add", [("title", "x"), ("date", "下週三")]),
                              cal("calendar.change", [("id", "9"), ("title", "y")]),
                              cal("calendar.change", [("id", "1"), ("on", "2026-11-15"), ("start", "10:00")]),
                              cal("calendar.add", [("title", "x"), ("date", "2026-11-20"), ("end", "10:00")]),
                              cal("calendar.list", [("from", "2026-11-01"), ("to", "2026-12-31")])))
    let bad = try h.toolResults().suffix(5).map(\.text)
    #expect(bad[0] == "失敗：calendar.add 的 date 要是 YYYY-MM-DD 的日期，例：2026-11-18；收到 下週三")
    #expect(bad[1] == "失敗：沒有 #9 這個行事曆項目；用 calendar.list 看現有的編號")
    #expect(bad[2].hasPrefix("失敗：#1（每天，從 2026-11-16（星期一） 起）在 2026-11-15（星期日） 沒有"))
    #expect(bad[3] == "失敗：calendar.add 有 end 就要有 start；全天的事兩個都不給")
    #expect(bad[4] == "失敗：calendar.list 一次最多 31 天，收到 61 天；分段查")
}

@Test func aTimedStartGivesATurnAnAllDayItemDoesNotAndEditsMoveTheTimer() async throws {
    let clock = ManualClock(at("2026-11-16", "09:00"))
    let h = try await Harness(clock: clock)
    let doll = h.doll
    try await h.turn(Scripted("", cal("calendar.add", [("title", "主人看牙醫"), ("date", "2026-11-16"), ("start", "10:00"), ("end", "11:00")]),
                              cal("calendar.add", [("title", "主人生日"), ("date", "2026-11-16")]),
                              cal("calendar.add", [("title", "倒垃圾"), ("date", "2026-11-16"), ("start", "10:15")]),
                              cal("calendar.change", [("id", "1"), ("start", "10:30")]),
                              cal("calendar.delete", [("id", "3")])),
                     Scripted("", dm("記好了")))
    #expect(try h.toolResults().map(\.text)[3] == "改好了：2026-11-16（星期一） 10:30–11:30 主人看牙醫（#1）。下一次是 2026-11-16（星期一） 10:30，到時候會給你一個回合。")
    let runner = Task { try await doll.run() }
    try await waitUntil { await h.model.requests.count == 2 && clock.sleepers == 1 }
    let before = await h.model.requests.count
    await h.model.add([Scripted("", dm("十點半了，該出門看牙醫囉"))])
    clock.set(at("2026-11-16", "10:20"))       // the old 10:00 and the deleted 10:15 do not fire
    try await Task.sleep(for: .milliseconds(300))
    #expect(await h.model.requests.count == before)
    clock.set(at("2026-11-16", "10:30"))
    try await waitUntil { h.said.last == "十點半了，該出門看牙醫囉" }
    let fired = try h.events().filter { $0.kind == .calendar }.map(\.text)
    #expect(fired == ["【行事曆】現在 10:30：主人看牙醫（#1，到 11:30）"])
    #expect(await h.model.requests.count == before + 1)
    let lastSeen = await h.model.requests.last!.messages
    #expect(lastSeen.contains { $0.role == "user" && $0.content.hasSuffix(" 程式] 【行事曆】現在 10:30：主人看牙醫（#1，到 11:30）") })
    clock.set(at("2026-11-17", "12:00"))       // the all-day item never fires
    try await Task.sleep(for: .milliseconds(300))
    #expect(await h.model.requests.count == before + 1 && clock.sleepers == 0)
    await doll.stop()
    try await runner.value
}

@Test func statusListsTodaysItemsStillToComeAndSaysHowManyMore() async throws {
    let clock = ManualClock(at("2026-11-16", "09:30"))
    let h = try await Harness(clock: clock) { $0.calendar.statusItems = 3 }
    try await h.turn(Scripted("", cal("calendar.add", [("title", "主人生日"), ("date", "2026-11-16")]),
                              cal("calendar.add", [("title", "吃藥"), ("date", "2026-11-16"), ("start", "09:00")]),
                              cal("calendar.add", [("title", "開會"), ("date", "2026-11-16"), ("start", "09:00"), ("end", "10:00")]),
                              cal("calendar.add", [("title", "看牙醫"), ("date", "2026-11-16"), ("start", "15:00")]),
                              cal("calendar.add", [("title", "晚餐"), ("date", "2026-11-16"), ("start", "19:00")]),
                              cal("calendar.add", [("title", "明天的事"), ("date", "2026-11-17"), ("start", "09:00")])))
    let status = try await h.doll.status()
    #expect(status.contains("- 現在 2026-11-16（星期一）09:30:00"))
    #expect(status.contains("- 今天的行事曆：全天 主人生日（#1）；09:00–10:00 開會（#3）（進行中）；15:00 看牙醫（#4）；還有 1 件（calendar.list 看全部）"))
    clock.set(at("2026-11-16", "20:00"))
    #expect(try await h.doll.status().contains("- 今天的行事曆：全天 主人生日（#1）\n"))
    clock.set(at("2026-11-18", "08:00"))
    #expect(try await h.doll.status().contains("- 今天的行事曆：沒有\n"))
}

@Test func theOwnersDashboardEditIsAProgramEventWithoutATurn() async throws {
    let clock = ManualClock(at("2026-11-16", "09:00"))
    let h = try await Harness([Scripted("", dm("好"))], clock: clock)
    let doll = h.doll
    let runner = Task { try await doll.run() }
    let d = dashboard(h)
    let r = await d.respond(method: "POST", target: "/api/calendar/add?title=%E4%B8%BB%E4%BA%BA%E7%9C%8B%E7%89%99%E9%86%AB&date=2026-11-18&start=10:00")
    #expect(r.status == 200)
    try await Task.sleep(for: .milliseconds(300))
    #expect(await h.model.requests.isEmpty)
    let notice = try h.events().filter { $0.kind == .memory }.map(\.text)
    #expect(notice == ["【行事曆】主人在 dashboard 加了：2026-11-18（星期三） 10:00 主人看牙醫（#1）"])
    #expect(await d.respond(method: "POST", target: "/api/calendar/change?id=1&start=25:00").status == 409)
    #expect(await d.respond(method: "POST", target: "/api/calendar/list?from=2026-11-18").status == 404)
    let c = try json(DashboardCalendar.self, await d.respond(method: "GET", target: "/api/calendar"))
    #expect(c.today == "2026-11-16" && c.entries.map(\.title) == ["主人看牙醫"] && c.items.map(\.by) == ["owner"])
    // She reads it with the next event.
    await h.doll.ownerSaid("嗨")
    try await waitUntil { await h.model.requests.count == 1 }
    let seen = await h.model.seen(0)
    #expect(seen.contains { $0.content.contains("【行事曆】主人在 dashboard 加了") })
    try await waitUntil { h.said == ["好"] && !h.logs.isEmpty }
    #expect(await d.respond(method: "POST", target: "/api/calendar/delete?id=1").status == 200)
    try await waitUntil { (try? h.events().last!.text.hasPrefix("【行事曆】主人在 dashboard 刪掉了")) == true }
    #expect(try h.events().last!.text == "【行事曆】主人在 dashboard 刪掉了：2026-11-18（星期三） 10:00 主人看牙醫（#1）")
    await doll.stop()
    try await runner.value
}

@Test func aStartDuringHerTurnFiresOnceAndOwnerEditsWaitForTheTurnToEnd() async throws {
    let clock = ManualClock(at("2026-11-16", "09:59"))
    let h = try await Harness(clock: clock)
    let doll = h.doll
    try await h.turn(Scripted("", cal("calendar.add", [("title", "十點的事"), ("date", "2026-11-16"), ("start", "10:00")])), Scripted("", dm("好")))
    let runner = Task { try await doll.run() }
    try await waitUntil { h.said == ["好"] }
    await h.model.stall()
    await h.model.add([Scripted("", dm("提醒你十點的事"))])
    await h.doll.ownerSaid("在嗎")
    try await waitUntil { await h.model.requests.count == 3 }
    clock.set(at("2026-11-16", "10:00"))                       // fires while the call is in flight
    try await waitUntil { await h.doll.queue.count == 1 }
    _ = try await h.doll.dashboardCalendarEdit("add", ["title": "晚餐", "date": "2026-11-16", "start": "19:00"])
    #expect(try !h.events().contains { $0.text.hasPrefix("【行事曆】主人") })   // held while the turn runs
    #expect(await h.doll.interrupt())
    try await waitUntil { h.said.last == "提醒你十點的事" }
    let evs = try h.events()
    #expect(evs.filter { $0.kind == .calendar }.count == 1)
    let owner = evs.firstIndex { $0.text.hasPrefix("【行事曆】主人") }!, fire = evs.firstIndex { $0.kind == .calendar }!
    #expect(owner < fire)
    await doll.stop()
    try await runner.value
}

@Test func missedStartsAreListedAtBootAndARestartNeverFiresAgain() async throws {
    let clock = ManualClock(at("2026-11-16", "07:00"))
    let a = try await Harness(clock: clock)
    try await a.turn(Scripted("", cal("calendar.add", [("title", "看牙醫"), ("date", "2026-11-16"), ("start", "10:00")]),
                              cal("calendar.add", [("title", "吃藥"), ("date", "2026-11-16"), ("start", "08:00"), ("repeat", "daily")]),
                              cal("calendar.add", [("title", "主人生日"), ("date", "2026-11-16")])), Scripted("", dm("好")), Scripted("", dm("八點了，吃藥")))
    try await a.doll.announceBoot()
    let ra = Task { [doll = a.doll] in try await doll.run() }
    clock.set(at("2026-11-16", "08:00"))
    try await waitUntil { await a.model.requests.count == 3 }
    clock.set(at("2026-11-16", "08:01"))
    await a.doll.stop()
    try await ra.value
    #expect(try a.events().filter { $0.kind == .calendar }.map(\.text) == ["【行事曆】現在 08:00：吃藥（#2，每天）"])

    // Down from 08:01 to 10:30: 10:00 was missed; 08:00 was already fired.
    clock.set(at("2026-11-16", "10:30"))
    let b = try await Harness([], dir: a.dir, clock: clock)
    try await b.doll.announceBoot()
    let boot = try b.events().last!.text
    #expect(boot == "【開機】你剛被開機了。今天的行事曆：全天 主人生日（#3）；08:00 吃藥（#2，每天）；10:00 看牙醫（#1）。沒開機的時候過了這些（沒有叫你）：2026-11-16（星期一） 10:00 看牙醫（#1）。")
    let rb = Task { [doll = b.doll] in try await doll.run() }
    try await Task.sleep(for: .milliseconds(300))
    #expect(await b.model.requests.isEmpty)
    await b.doll.stop()
    try await rb.value

    // A third process at the same time: nothing missed, nothing fired.
    let c = try await Harness([], dir: a.dir, clock: clock)
    try await c.doll.announceBoot()
    #expect(try !c.events().last!.text.contains("沒開機"))
}

@Test func aMalformedCalendarFileIsAnError() async throws {
    let dir = FileManager.default.temporaryDirectory.appending(path: "dollcore-test-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    for (bytes, says) in [("{not json", "not a calendar file"),
                          (#"{"next_id": 2, "items": [{"id": 1, "title": "x", "date": "2026-11-31", "by": "doll"}]}"#, "date 2026-11-31 is not YYYY-MM-DD"),
                          (#"{"next_id": 3, "items": [{"id": 1, "title": "x", "date": "2026-11-16", "by": "doll"}, {"id": 1, "title": "y", "date": "2026-11-16", "by": "doll"}]}"#, "id 1 appears twice")] {
        try Data(bytes.utf8).write(to: dir.appending(path: "calendar.json"))
        do { _ = try await Harness(dir: dir); Issue.record("accepted \(bytes)") } catch let e as CalendarError {
            #expect(e.description.contains(says), "\(e)")
        }
    }
}

// system maintenance (2026-11-16) ------------------------------------------------------------

/// A valid consolidation reply (one add), then the day's summary.
private let planReply = Scripted("""
    ```json
    {"entities": [], "operations": [{"op": "add", "ref": "n1", "text": "主人今天跟我說晚安", "folder": "public", "kind": "happened", "entities": []}]}
    ```
    """)
private let summaryReply = Scripted("今天主人跟她說了晚安。")
private let startNotice = "【系統維護】開始，現在 03:00。程式要從今天的對話記下掛著的事、整理你的記憶、寫今天的摘要，要幾分鐘；這段時間你不在，結束了會告訴你。"
private let didOne = "整理了之前的事：新記了 1 條記憶，改了 0 條，0 條舊的換掉了，0 條又遇到一次、記得更牢了。"

/// `notice`: minutes she is told ahead (`[maintenance] notice_min`); 0 here unless a test is about the notice.
private func maintenanceHarness(_ clock: ManualClock, dir: URL? = nil, drives: Bool = false, notice: Int = 0,
                                _ replies: [Scripted] = []) async throws -> Harness {
    try await Harness(replies, dir: dir, clock: clock) {
        $0.maintenance.enabled = true; $0.maintenance.noticeMin = notice; $0.drives.enabled = drives; $0.drives.driveHalfLifeH = noClimb
    }
}

/// Every segment's event texts, oldest segment first.
private func segmentTexts(_ h: Harness) throws -> [[String]] {
    let dir = h.dir.appending(path: "timeline")
    let names = try FileManager.default.contentsOfDirectory(atPath: dir.path).filter { $0.hasSuffix(".jsonl") }
        .map { String($0.dropLast(".jsonl".count)) }.sorted()
    return try names.map { n in
        try String(contentsOf: dir.appending(path: n + ".jsonl"), encoding: .utf8).split(separator: "\n").compactMap { line in
            let o = try JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any]
            return o?["type"] as? String == "event" ? o?["text"] as? String : nil
        }
    }
}

private func consolidatedBodies(_ h: Harness) throws -> [String] {
    try memoryFilesByID(h).values.filter { $0.file.tier == .consolidated }.map(\.file.body)
}

private func maintenanceFile(_ h: Harness) throws -> MaintenanceFile {
    try JSONDecoder().decode(MaintenanceFile.self, from: Data(contentsOf: h.dir.appending(path: "maintenance.json")))
}

@Test func maintenanceStartsAtItsTimeWhenIdleWithoutATurnAndEndsWithWhatItDidTheSummaryAndTheCalendar() async throws {
    let clock = ManualClock(at("2026-11-16", "22:00"))
    let h = try await maintenanceHarness(clock, [Scripted("", dm("晚安")), planReply, summaryReply])
    let doll = h.doll
    let runner = Task { try await doll.run() }
    await doll.ownerSaid("晚安")
    try await waitUntil { h.said == ["晚安"] }
    // 【狀況】 says when it comes and how much the day holds.
    let footer = await h.model.requests.first!.messages.last!.content
    #expect(footer.contains("- 今天的時間線：別人跟你說了 1 句，你回了 0 則\n- 系統維護：2026-11-17（星期二） 03:00（還有 5 小時）"))
    #expect(!footer.contains("醒著"))
    clock.set(at("2026-11-17", "03:00"))
    try await waitUntil { h.logs.contains { $0.contains("maintenance: ended") } }
    // No turn of hers: her one reply, the plan, the summary.
    let reqs = await h.model.requests
    #expect(reqs.count == 3 && reqs[1].effort == "high" && reqs[1].messages[1].content.contains("] 晚安"))
    #expect(try consolidatedBodies(h) == ["主人今天跟我說晚安"])
    let segs = try segmentTexts(h)
    #expect(segs.count == 2 && segs[0].last == startNotice)
    #expect(segs[1] == ["【系統維護】結束，現在 03:00。\(didOne)\n\(Consolidation.summaryNote)今天主人跟她說了晚安。\n今天的行事曆：03:00 系統維護（#1，每天）。"])
    #expect(try await doll.dashboardStatus().state == "idle")
    #expect(await doll.maintenanceLine() == nil)
    #expect(try maintenanceFile(h) == MaintenanceFile())
    try await Task.sleep(for: .milliseconds(100))
    #expect(await h.model.requests.count == 3)
    // Her next turn begins from the end notice and the summary; the day before is gone from view.
    clock.set(at("2026-11-17", "08:00"))
    await h.model.add([Scripted("", dm("早安"))])
    await doll.ownerSaid("早")
    try await waitUntil { h.said.last == "早安" }
    let seen = await h.model.requests.last!.messages
    #expect(seen.contains { $0.content.contains("【系統維護】結束") && $0.content.contains("今天主人跟她說了晚安。") })
    #expect(!seen.contains { $0.role == "user" && $0.content.hasSuffix("] 晚安") })
    #expect(seen.last!.content.contains("- 系統維護：2026-11-18（星期三） 03:00（還有 19 小時）"))
    await doll.stop()
    try await runner.value
}

@Test func sheCannotChangeOrDeleteWhatTheOwnerPutOnTheCalendar() throws {
    let a = try Agenda(url: FileManager.default.temporaryDirectory.appending(path: "cal-\(UUID().uuidString).json"))
    _ = try a.apply(.add(title: "起床", day: Day("2026-11-17")!, start: TimeOfDay("06:30"), end: nil, repeat: nil, until: nil), by: "owner")
    _ = try a.apply(.add(title: "叫主人起床", day: Day("2026-11-17")!, start: TimeOfDay("06:25"), end: nil, repeat: nil, until: nil), by: "doll")
    for r in [CalendarRequest.delete(id: 1, on: nil),
              .change(id: 1, on: nil, title: nil, day: nil, start: TimeOfDay("07:00"), end: nil, allDay: false)] {
        #expect { _ = try a.apply(r, by: "doll") } throws: { "\($0)".contains("#1「起床」是主人自己加的，你不能改也不能刪") }
    }
    #expect(a.item(1)?.start == "06:30")
    // Her own she may; the owner may edit his and hers.
    _ = try a.apply(.delete(id: 2, on: nil), by: "doll")
    _ = try a.apply(.change(id: 1, on: nil, title: nil, day: nil, start: TimeOfDay("07:00"), end: nil, allDay: false), by: "owner")
    #expect(a.item(2) == nil && a.item(1)?.start == "07:00")
}

@Test func sheIsToldBeforeItStartsGetsATurnAndMayMoveTodaysMaintenance() async throws {
    // Told at its time with a turn; it starts `notice_min` later (owner, 2026-11-16).
    let clock = ManualClock(at("2026-11-16", "22:00"))
    let h = try await maintenanceHarness(clock, notice: 10, [Scripted("", dm("晚安")), Scripted("", dm("我等一下要維護，先不在幾分鐘")),
                                                               planReply, summaryReply])
    let doll = h.doll
    let runner = Task { try await doll.run() }
    await doll.ownerSaid("晚安")
    try await waitUntil { h.said == ["晚安"] }
    clock.set(at("2026-11-17", "03:00"))
    try await waitUntil { h.said.count == 2 }
    let told = try #require(try h.events().last { $0.kind == .calendar })
    #expect(told.text == Doll.maintenanceNotice(10) && told.text.hasPrefix("【系統維護】時間到了，10 分鐘後開始"))
    #expect(!h.logs.contains { $0.contains("maintenance: started") })
    clock.set(at("2026-11-17", "03:09"))
    try await Task.sleep(for: .milliseconds(100))
    #expect(!h.logs.contains { $0.contains("maintenance: started") })
    clock.set(at("2026-11-17", "03:10"))
    try await waitUntil { h.logs.contains { $0.contains("maintenance: ended") } }
    #expect(try consolidatedBodies(h) == ["主人今天跟我說晚安"])
    await doll.stop()
    try await runner.value

    // She moves today's maintenance in that turn: this one is called off and comes at the new time.
    let clock2 = ManualClock(at("2026-11-16", "22:00"))
    let moved = ("calendar.change", [("id", "1"), ("on", "2026-11-17"), ("start", "04:00")])
    let h2 = try await maintenanceHarness(clock2, notice: 10, [Scripted("", dm("晚安")), Scripted("", moved), Scripted("", dm("改到四點")),
                                                                 Scripted("", dm("好")), planReply, summaryReply])
    let doll2 = h2.doll
    let runner2 = Task { try await doll2.run() }
    await doll2.ownerSaid("晚安")
    try await waitUntil { h2.said == ["晚安"] }
    clock2.set(at("2026-11-17", "03:00"))
    try await waitUntil { h2.logs.contains { $0.contains("called off until the item's next time") } }
    try await waitUntil { await doll2.settled }
    clock2.set(at("2026-11-17", "03:30"))
    try await Task.sleep(for: .milliseconds(100))
    #expect(!h2.logs.contains { $0.contains("maintenance: started") })
    #expect(try maintenanceFile(h2).due == nil)
    clock2.set(at("2026-11-17", "04:00"))
    try await waitUntil { h2.logs.filter { $0.contains("maintenance: due") }.count == 2 }
    clock2.set(at("2026-11-17", "04:10"))
    try await waitUntil { h2.logs.contains { $0.contains("maintenance: ended") } }
    await doll2.stop()
    try await runner2.value
}

@Test func aListWaitingDoesNotHoldMaintenanceItCrossesIntoTheNewDayAndHisAnswerResolvesIt() async throws {
    // 2026-11-21, owner (soak run: a list waited from 07:01 and maintenance came only at the busy limit).
    let clock = ManualClock(at("2026-11-17", "02:50"))
    let h = try await maintenanceHarness(clock, [Scripted("", todoSet("報告寄給主人了"), dm("檔案在哪？"), todoWait("等主人給檔案")),
                                                 planReply, summaryReply])
    let doll = h.doll
    let runner = Task { try await doll.run() }
    await doll.ownerSaid("幫我寄報告")
    try await waitUntil { (try? await doll.dashboardStatus().todo) == "等待中" }
    clock.set(at("2026-11-17", "03:00"))
    try await waitUntil { h.logs.contains { $0.contains("maintenance: ended") } }
    // It started at its time with no turn of hers, and said nothing about her list at the start.
    #expect(await h.model.requests.count == 3)
    let segs = try segmentTexts(h)
    #expect(segs[0].last == startNotice)
    #expect(segs[1].count == 1 && segs[1][0].hasSuffix("今天的行事曆：03:00 系統維護（#1，每天）。你手上的「報告寄給主人了」還在，還是等待中。"))
    // The list is intact in the new day, still waiting, and no turn follows for it.
    let t = try h.timeline()
    #expect(t.todo.phase == .waiting && t.todo.current == "報告寄給主人了" && t.todo.plan?.items == ["報告寄給主人了"])
    #expect(t.todo.startSeq == 1 && t.todoAside == nil)
    try await Task.sleep(for: .milliseconds(100))
    #expect(await h.model.requests.count == 3)
    #expect(await !doll.followUp)
    // His answer in the morning resolves the item.
    await h.model.add([Scripted("", dm("好，我寄了"), done("報告寄出了", evidence: nil))])
    clock.set(at("2026-11-17", "08:00"))
    await doll.ownerSaid("檔案在桌面")
    try await waitUntil { (try? h.timeline().todo) == TodoState() }
    let seen = await h.model.requests.last!.messages
    #expect(seen.last!.content.contains("- todo：等待中「報告寄給主人了」"))
    #expect(try h.toolResults().last!.text.contains("清單做完了，現在待命中"))
    #expect(await h.doll.estimateFile.records.map(\.plan.items) == [["報告寄給主人了"]])
    await doll.stop()
    try await runner.value
}

@Test func aListInProgressStillHoldsMaintenanceUntilTheBusyLimitThenItIsKeptAndSheGetsATurn() async throws {
    let clock = ManualClock(at("2026-11-17", "02:50"))
    let h = try await maintenanceHarness(clock)
    _ = try await h.doll.calendarCatchUp()
    await h.doll.ownerSaid("幫我寄報告")
    try await h.turn(Scripted("", todoSet("報告寄給主人了")))
    #expect(await h.doll.todo.phase == .doing)
    clock.set(at("2026-11-17", "03:00"))
    await h.doll.maintenanceFire()
    try await Task.sleep(for: .milliseconds(100))
    #expect(await h.doll.maintenanceLine() == "系統維護 2026-11-17（星期二） 03:00 到了，等她手上沒事")
    clock.set(at("2026-11-17", "05:59"))
    try await Task.sleep(for: .milliseconds(100))
    #expect(!h.logs.contains { $0.contains("maintenance: started") })
    await h.model.add([planReply, summaryReply])
    clock.set(at("2026-11-17", "06:00"))
    try await waitUntil { h.logs.contains { $0.contains("maintenance: ended") } }
    #expect(h.logs.contains { $0.contains("maintenance: busy 3.0 h since due") })
    let segs = try segmentTexts(h)
    #expect(segs[0].last == "【系統維護】開始，現在 06:00。程式要從今天的對話記下掛著的事、整理你的記憶、寫今天的摘要，要幾分鐘；這段時間你不在，結束了會告訴你。你手上的「報告寄給主人了」先停著，結束後接著做。")
    #expect(segs[1][0].hasPrefix("【系統維護】結束，現在 06:00。") && segs[1][0].hasSuffix("你手上的「報告寄給主人了」還在，接著做。"))
    #expect(try h.timeline().todo.current == "報告寄給主人了" && h.timeline().todo.phase == .doing)
    #expect(await h.doll.followUp)
}

@Test func theOwnersLineInterruptsItNothingIsWrittenSheAnswersAndItRetriesAfterQuiet() async throws {
    let clock = ManualClock(at("2026-11-16", "22:00"))
    let h = try await maintenanceHarness(clock, [Scripted("", dm("晚安"))])
    await h.model.stall(request: 2)
    let doll = h.doll
    let runner = Task { try await doll.run() }
    let before = try h.timeline().segment
    await doll.ownerSaid("晚安")
    try await waitUntil { h.said == ["晚安"] }
    clock.set(at("2026-11-17", "03:00"))
    try await waitUntil { await h.model.requests.count == 2 }
    #expect(try await doll.dashboardStatus().state == "maintenance")
    #expect(await doll.maintenanceLine() == "系統維護中，從 2026-11-17（星期二） 03:00 起")
    await h.model.add([Scripted("", dm("還沒睡啊？"))])
    clock.set(at("2026-11-17", "03:05"))
    await doll.ownerSaid("你在嗎")
    try await waitUntil { h.said.last == "還沒睡啊？" }
    #expect(await h.model.cancelled == 1)
    #expect(try h.timeline().segment == before && consolidatedBodies(h).isEmpty)
    let texts = try h.events().map(\.text)
    let notice = try #require(texts.firstIndex { $0.hasPrefix("【系統維護】被") })
    #expect(texts[notice] == "【系統維護】被主人打斷了，今天的還沒整理完，之前的對話和做過的事都還在你眼前；沒人跟你說話 30 分鐘、你手上也沒事的時候，會再做一次。")
    #expect(texts[notice + 1] == "你在嗎")
    #expect(h.logs.contains { $0.contains("maintenance: consolidate cancelled (interrupted)") })
    let state = try maintenanceFile(h)
    #expect(state.running == nil && state.interrupted && state.due == at("2026-11-17", "03:00"))
    // Quiet for less than 30 minutes: not yet.
    await h.model.add([planReply, summaryReply])
    clock.set(at("2026-11-17", "03:34"))
    try await Task.sleep(for: .milliseconds(100))
    #expect(await h.model.requests.count == 3)
    clock.set(at("2026-11-17", "03:35"))
    try await waitUntil { h.logs.contains { $0.contains("maintenance: ended") } }
    #expect(try consolidatedBodies(h) == ["主人今天跟我說晚安"])
    let all = await h.model.requests
    let sent = all[3].messages[1].content
    #expect(sent.contains("] 晚安") && sent.contains("] 你在嗎"))
    #expect(try segmentTexts(h)[1][0].hasPrefix("【系統維護】結束，現在 03:35。"))
    await doll.stop()
    try await runner.value
}

@Test func theDevelopersLineInterruptsItToo() async throws {
    let clock = ManualClock(at("2026-11-16", "22:00"))
    let h = try await maintenanceHarness(clock, [Scripted("", dm("晚安"))])
    await h.model.stall(request: 2)
    let doll = h.doll
    let runner = Task { try await doll.run() }
    await doll.ownerSaid("晚安")
    try await waitUntil { h.said == ["晚安"] }
    clock.set(at("2026-11-17", "03:00"))
    try await waitUntil { await h.model.requests.count == 2 }
    await h.model.add([Scripted("", reply("在"))])
    await doll.developerSaid("在嗎")
    try await waitUntil { h.said.last == "在" }
    #expect(try h.events().map(\.text).contains { $0.hasPrefix("【系統維護】被開發者打斷了") })
    await doll.stop()
    try await runner.value
}

@Test func aRestartDuringMaintenanceGoesOnWithTheTasksNotDone() async throws {
    let clock = ManualClock(at("2026-11-16", "22:00"))
    let a = try await maintenanceHarness(clock, [Scripted("", dm("晚安"))])
    await a.model.stall(request: 2)
    let ra = Task { [doll = a.doll] in try await doll.run() }
    await a.doll.ownerSaid("晚安")
    try await waitUntil { a.said == ["晚安"] }
    clock.set(at("2026-11-17", "03:00"))
    try await waitUntil { await a.model.requests.count == 2 }
    await a.doll.stop()
    try await ra.value
    #expect(try consolidatedBodies(a).isEmpty && maintenanceFile(a).running == at("2026-11-17", "03:00"))
    clock.set(at("2026-11-17", "03:10"))
    let b = try await maintenanceHarness(clock, dir: a.dir, [planReply, summaryReply])
    #expect(await b.doll.inMaintenance)
    try await b.doll.announceBoot()
    let rb = Task { [doll = b.doll] in try await doll.run() }
    try await waitUntil { b.logs.contains { $0.contains("maintenance: ended") } }
    #expect(b.logs.contains { $0.contains("maintenance: was running at the restart") })
    #expect(try consolidatedBodies(b) == ["主人今天跟我說晚安"])
    #expect(await b.model.requests.count == 2)   // the tasks only; no turn of hers
    await b.doll.stop()
    try await rb.value
    // Down across its time: due at boot, starts once she is idle.
    clock.set(at("2026-11-18", "09:00"))
    let c = try await maintenanceHarness(clock, dir: a.dir)
    try await c.doll.announceBoot()
    #expect(c.logs.contains { $0.contains("maintenance: its time passed while not running; due") })
}

@Test func theItemIsCreatedOnceCanBeMovedForADayAndCannotBeDeletedOrMadeAllDay() async throws {
    let clock = ManualClock(at("2026-11-16", "10:00"))
    let h = try await maintenanceHarness(clock)
    try await h.doll.announceBoot()
    try await h.turn(Scripted("", cal("calendar.delete", [("id", "1")]), cal("calendar.change", [("id", "1"), ("all_day", "true")]),
                              cal("calendar.change", [("id", "1"), ("on", "2026-11-17"), ("start", "04:30")])))
    let r = try h.toolResults().map(\.text)
    #expect(r[0] == "失敗：#1「系統維護」是每天系統維護的時間，不能刪掉；要換時間用 calendar.change 改 start，只改一天就給 on")
    #expect(r[1].hasPrefix("失敗：#1「系統維護」是每天系統維護的時間，不能改成全天"))
    #expect(r[2].hasSuffix("下一次是 2026-11-17（星期二） 04:30，到時候會先告訴你、給你一個回合，再過幾分鐘系統維護就開始。"))
    let b = try await maintenanceHarness(clock, dir: h.dir)
    try await b.doll.announceBoot()
    #expect(await b.doll.dashboardCalendar().items.map(\.title) == ["系統維護"])
    // Moved for one day: 03:00 passes, 04:30 makes it due.
    let runner = Task { [doll = b.doll] in try await doll.run() }
    clock.set(at("2026-11-17", "03:00"))
    try await Task.sleep(for: .milliseconds(100))
    #expect(!b.logs.contains { $0.contains("maintenance: due") })
    clock.set(at("2026-11-17", "04:30"))
    try await waitUntil { b.logs.contains { $0.contains("maintenance: due") } }
    await b.doll.stop()
    try await runner.value
}

@Test func anOldCalendarWithTheSleepItemsAndOldSignalsWithSleepinessAreReplacedAtBoot() async throws {
    let clock = ManualClock(at("2026-11-16", "10:00"))
    let dir = FileManager.default.temporaryDirectory.appending(path: "dollcore-test-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    try Data(#"""
        {"fired_through": "2026-11-16T09:00:00.000+08:00", "next_id": 4, "items": [
          {"id": 1, "title": "睡覺", "date": "2026-11-15", "start": "01:00", "repeat": "daily", "by": "program", "role": "sleep"},
          {"id": 2, "title": "起床", "date": "2026-11-15", "start": "08:00", "repeat": "daily", "by": "program", "role": "wake"},
          {"id": 3, "title": "看牙醫", "date": "2026-11-18", "start": "15:00", "by": "owner"}]}
        """#.utf8).write(to: dir.appending(path: "calendar.json"))
    try Data(#"""
        {"points": {"drive": {"value": 0.4, "ts": 0}, "stress": {"value": 0.1, "ts": 0}, "closeness": {"value": 0.6, "ts": 0},
                    "sleepiness": {"value": 0.7, "ts": 0}},
         "surfaced": ["sleepiness"],
         "log": [{"ts": 0, "signal": "sleepiness", "before": 0, "after": 0.7, "cause": "x"},
                 {"ts": 0, "signal": "stress", "before": 0, "after": 0.1, "cause": "y"}]}
        """#.utf8).write(to: dir.appending(path: "signals.json"))
    let h = try await maintenanceHarness(clock, dir: dir, drives: true)
    try await h.doll.announceBoot()
    #expect(await h.doll.dashboardCalendar().items.map(\.title) == ["看牙醫", "系統維護"])
    #expect(h.logs.contains { $0.contains("calendar: removed the sleep items 睡覺 01:00, 起床 08:00 (sleep became system maintenance, 2026-11-16)") })
    #expect(h.logs.contains { $0.contains("maintenance: calendar had no 系統維護 03:00 (it replaces them); added (daily)") })
    #expect(h.logs.contains { $0.contains("held 睡意 (removed 2026-11-16); dropped and saved without it") })
    let saved = try String(contentsOf: dir.appending(path: "signals.json"), encoding: .utf8)
    #expect(!saved.contains("sleepiness") && saved.contains("\"y\""))
    #expect(!(try String(contentsOf: dir.appending(path: "calendar.json"), encoding: .utf8)).contains("\"sleep\""))
}

@Test func noSleepToolNoSleepinessAndTheOldToolIsUnknown() async throws {
    let clock = ManualClock(at("2026-11-16", "23:00"))
    let h = try await maintenanceHarness(clock, drives: true)
    #expect(!(await h.doll.toolsJSON).contains("\"sleep\""))
    // How she works (the persona before it is the character pack's own words).
    let prompt = try #require(await h.doll.systemPrompt().components(separatedBy: "## 你怎麼運作").last)
    #expect(prompt.contains("## 系統維護") && !prompt.contains("睡覺") && !prompt.contains("睡意") && !prompt.contains("起床"))
    #expect(Signal.allCases == [.drive, .stress, .closeness])
    try await h.doll.announceBoot()
    try await h.turn(Scripted("", ("sleep", []), dm("晚安")))
    #expect(try h.toolResults().first!.text.hasPrefix("失敗："))
}

// drives (m6-drives, 2026-11-15) ---------------------------------------------------------------

private func approx(_ a: Double, _ b: Double) -> Bool { abs(a - b) < 1e-9 }

@Test func drivesEventsMoveSignalsWithACauseAndRelaxByTheirHalfLife() {
    let c = DrivesConfig()
    var d = Drives(config: c, now: 0)
    #expect(approx(d.value(.closeness, at: 0), c.closenessStart) && approx(d.value(.stress, at: 0), c.stressRest))
    d.apply(.correction, at: 0, detail: "「不是這樣」")
    #expect(approx(d.value(.stress, at: 0), c.stressRest + c.correctionStress))
    // One half-life later half the gap to rest is left; two, a quarter.
    #expect(approx(d.value(.stress, at: c.stressHalfLifeH * 3600), c.stressRest + c.correctionStress / 2))
    #expect(approx(d.value(.stress, at: 2 * c.stressHalfLifeH * 3600), c.stressRest + c.correctionStress / 4))
    #expect(d.state.log == [SignalChange(ts: 0, signal: .stress, before: c.stressRest, after: c.stressRest + c.correctionStress,
                                         cause: "主人糾正她：「不是這樣」")])
    var p = Drives(config: c, now: 0)
    p.apply(.praise, at: 0, detail: "「謝啦」")
    #expect(approx(p.value(.drive, at: 0), c.driveReset + c.praiseDrive))
    #expect(approx(p.value(.closeness, at: 0), c.closenessStart + c.praiseCloseness))
    #expect(approx(p.value(.stress, at: 0), max(0, c.stressRest + c.praiseStress)))
    p.apply(.itemDone, at: 0, detail: "寄出季報")
    #expect(approx(p.value(.drive, at: 0), c.driveReset + c.praiseDrive + c.itemDoneDrive))
    p.apply(.itemFailed, at: 0, detail: "寄出季報")
    p.apply(.ownerTalk, at: 0, detail: "「嗨」")
    p.apply(.deadline, at: 0, detail: "#1")
    p.apply(.toolErrors, at: 0, detail: "click")
    p.apply(.concernDone, at: 0, detail: "#2")
    // Every change has its event as the cause.
    #expect(p.state.log.count == 9 && p.state.log.allSatisfy { $0.cause.contains("：") && $0.before != $0.after })
}

@Test func drivesNextCrossingIsExactAndAnExcursionSurfacesOncePerCrossing() {
    var c = DrivesConfig()
    c.driveHalfLifeH = noClimb   // 親近 and 壓力 here; 動力's climb has its own test
    var d = Drives(config: c, now: 0)
    let t = d.nextCrossing(after: 0)!
    #expect(abs(t - (log2(c.closenessStart / c.closenessLow) * c.closenessHalfLifeH * 3600 + 1)) < 1e-6)
    #expect(!d.isOut(.closeness, at: t - 2) && d.isOut(.closeness, at: t))
    #expect(d.pending(at: t - 2) == [] && d.pending(at: t) == [.closeness])
    d.state.surfaced.insert(.closeness)
    #expect(d.pending(at: t + 3600) == [])
    // The owner talks: back in range, the mark is forgotten, and the next crossing surfaces again.
    d.apply(.ownerTalk, at: t + 3600, detail: "「嗨」")
    #expect(d.pending(at: t + 3600) == [] && !d.state.surfaced.contains(.closeness))
    let t2 = d.nextCrossing(after: t + 3600)!
    #expect(d.pending(at: t2 - 2) == [] && d.pending(at: t2) == [.closeness])
    // 壓力 out and surfaced: the timer's next moment is its way back into range.
    var e = Drives(config: c, now: 0)
    e.apply(.correction, at: 0, detail: "x")
    e.apply(.correction, at: 0, detail: "x")
    e.state.surfaced.insert(.stress)
    let back = log2((e.value(.stress, at: 0) - c.stressRest) / (c.stressHigh - c.stressRest)) * c.stressHalfLifeH * 3600 + 1
    #expect(abs(e.nextCrossing(after: 0)! - min(back, t)) < 1e-6)
}

/// 動力 (idle-drive, 2026-11-20): left alone it climbs from `drive_reset` past `drive_high` one period
/// later; the episode's end puts it back and the next comes one period after that; praise brings it
/// sooner; whatever she gains inside an episode (her own items, a concern done) is wiped by its end.
@Test func driveClimbsWhileLeftAloneAndEachEpisodeSetsItBack() {
    let c = DrivesConfig()
    let period = c.driveHalfLifeH * log2((c.driveRest - c.driveReset) / (c.driveRest - c.driveHigh)) * 3600
    #expect(abs(period - 3600) < 30)   // the example's numbers: one hour
    var d = Drives(config: c, now: 0)
    #expect(approx(d.value(.drive, at: 0), c.driveReset))
    let t1 = d.nextCrossing(after: 0)!
    #expect(abs(t1 - (period + 1)) < 1e-6)
    #expect(d.pending(at: t1 - 2) == [] && d.pending(at: t1) == [.drive])
    #expect(d.felt(at: t1) == "你想做點自己的事")
    d.state.surfaced.insert(.drive)   // its wandering
    // Inside the episode she finishes three items of her own and a concern: 動力 rises, nothing surfaces again.
    for _ in 0..<3 { d.apply(.itemDone, at: t1 + 600, detail: "自己的一步") }
    d.apply(.concernDone, at: t1 + 600, detail: "#1")
    #expect(d.value(.drive, at: t1 + 600) > c.driveHigh && d.pending(at: t1 + 600) == [])
    // Idle again: back to drive_reset, the next one period on, not at once.
    let end = t1 + 1200
    let ended = d.endEpisode(at: end, cause: "自己的事告一段落：x")
    let again = d.endEpisode(at: end, cause: "x")
    #expect(ended && !again && approx(d.value(.drive, at: end), c.driveReset))
    #expect(d.state.log.last!.cause == "自己的事告一段落：x" && !d.state.surfaced.contains(.drive))
    #expect(abs(d.nextCrossing(after: end)! - (end + period + 1)) < 1e-6)
    #expect(d.pending(at: end + period - 60) == [])
    // Praise right after: sooner, by the formula.
    var p = d
    p.apply(.praise, at: end, detail: "「謝啦」")
    let sooner = c.driveHalfLifeH * log2((c.driveRest - c.driveReset - c.praiseDrive) / (c.driveRest - c.driveHigh)) * 3600
    #expect(abs(p.nextCrossing(after: end)! - (end + sooner + 1)) < 1e-6 && sooner < period - 20 * 60)
    // A day alone, each episode 5 minutes: about one an hour.
    var a = Drives(config: c, now: 0)
    var episodes = 0, busyUntil = 0.0
    for m in 0..<(24 * 60) {
        let t = Double(m) * 60
        guard t >= busyUntil else { continue }
        _ = a.endEpisode(at: t, cause: "x")
        if a.pending(at: t).contains(.drive) { a.state.surfaced.insert(.drive); episodes += 1; busyUntil = t + 300 }
    }
    #expect((20...24).contains(episodes))
}

@Test func theFeltLineIsOneFixedSentenceByTheCombinationAndNoneInRange() {
    var c = DrivesConfig()
    c.driveHalfLifeH = noClimb
    var d = Drives(config: c, now: 0)
    #expect(d.felt(at: 0) == nil)
    d.apply(.correction, at: 0, detail: "x")
    d.apply(.correction, at: 0, detail: "x")
    #expect(d.felt(at: 0) == "你現在有點緊繃")
    // 17 h on: 壓力 has receded, 親近 is low.
    #expect(!d.isOut(.stress, at: 17 * 3600) && d.felt(at: 17 * 3600) == "有點想主人了")
    #expect(d.felt(at: 5 * 3600) == nil || d.felt(at: 5 * 3600) == "有點想主人了")
    var both = Drives(config: c, now: 0)
    both.apply(.correction, at: 15.5 * 3600, detail: "x")
    both.apply(.correction, at: 15.5 * 3600, detail: "x")
    #expect(both.felt(at: 16.5 * 3600) == "你有點緊繃，也有點想主人")
    var drive = Drives(config: c, now: 0)
    drive.apply(.correction, at: 0, detail: "x")
    drive.apply(.correction, at: 0, detail: "x")
    drive.set(.drive, to: 0.9, at: 0, cause: "x")
    #expect(drive.felt(at: 0) == "你有點緊繃，但也想做點自己的事")
}

@Test func signalsAndConcernsSurviveARestartAndMalformedFilesStopTheBoot() async throws {
    let clock = ManualClock(at("2026-11-16", "10:00"))
    let h = try await Harness([Scripted("", ("owner.feedback", [("kind", "correction"), ("quote", "不是這個")])), Scripted("", dm("好"))],
                              clock: clock) { $0.drives.enabled = true }
    let doll0 = h.doll
    let runner = Task { try await doll0.run() }
    try await waitUntil { clock.sleepers >= 1 }   // caught up: the signals have started
    await h.doll.ownerSaid("不是這個啦")
    try await waitUntil { await h.doll.settled && h.reports.count == 2 }
    await h.doll.stop()
    try await runner.value
    let c = DrivesConfig()
    let stress = c.stressRest + c.correctionStress
    // Four hours later in a new process: the value decayed for the time passed.
    clock.set(at("2026-11-16", "14:00"))
    let b = try await Harness(dir: h.dir, clock: clock) { $0.drives.enabled = true }
    let v = await b.doll.dashboardDrives().values.first { $0.signal == "stress" }!.value
    #expect(approx(v, c.stressRest + (stress - c.stressRest) / 2))
    #expect(await b.doll.dashboardDrives().changes.map(\.cause).contains("主人糾正她：「不是這個」"))
    for name in ["signals.json", "concerns.json"] {
        let url = h.dir.appending(path: name)
        let keep = try? Data(contentsOf: url)
        try Data("{".utf8).write(to: url)
        await #expect { try await Harness(dir: h.dir, clock: clock) { $0.drives.enabled = true } } throws: {
            "\($0)".contains("\(name): not a \(name == "signals.json" ? "signals" : "concerns") file")
        }
        if let keep { try keep.write(to: url) } else { try FileManager.default.removeItem(at: url) }
    }
    // A signals file missing a signal is malformed too.
    try Data(#"{"points":{"drive":{"value":0.4,"ts":0}},"surfaced":[],"log":[]}"#.utf8).write(to: h.dir.appending(path: "signals.json"))
    await #expect(throws: (any Error).self) { try await Harness(dir: h.dir, clock: clock) { $0.drives.enabled = true } }
}

/// 動力's climb held (idle-drive, 2026-11-20): for tests not about her own things, whose clocks run
/// for hours and whose scripts have no turn for a 動力 wandering.
private let noClimb = 1e9

private func noteCall(_ text: String, _ about: String) -> (String, [(String, String)]) {
    ("concern.note", [("text", text), ("about", about)])
}

@Test func wanderingSurfacesHerOwnWordsOncePerCrossingOnlyWhenIdle() async throws {
    let clock = ManualClock(at("2026-11-16", "10:00"))
    let h = try await Harness([Scripted("", noteCall("主人昨晚加班到兩點，很累", "owner")), Scripted("", dm("辛苦了"))], clock: clock) {
        $0.drives.enabled = true; $0.drives.driveHalfLifeH = noClimb
        $0.maintenance.enabled = true
    }
    let doll = h.doll
    let runner = Task { try await doll.run() }
    try await waitUntil { clock.sleepers >= 1 }
    await doll.ownerSaid("昨晚加班到兩點")
    try await waitUntil { await doll.settled && h.reports.count == 2 }
    // 親近 0.6 + 0.1 = 0.7 at 10:00 crosses 0.2 after log2(3.5)·4 h ≈ 7.23 h: 17:14.
    clock.set(at("2026-11-16", "17:00"))
    try await Task.sleep(for: .milliseconds(100))
    #expect(await h.model.requests.count == 2)
    await h.model.add([Scripted("", dm("今天還好嗎？"))])
    clock.set(at("2026-11-16", "17:20"))
    try await waitUntil { await h.model.requests.count == 3 }
    try await waitUntil { await doll.settled }
    let wander = try h.events().filter { $0.text.hasPrefix("【走神】") }.map(\.text)
    #expect(wander == ["【走神】你想起掛著的一件事（#1，你 2026-11-16（星期一） 10:00 記的）：主人昨晚加班到兩點，很累"])
    #expect(await h.model.seen(2).contains { $0.content.contains("【走神】") })
    // Still out of range hours later: no second surfacing for the same crossing.
    clock.set(at("2026-11-16", "21:00"))
    try await Task.sleep(for: .milliseconds(100))
    #expect(await h.model.requests.count == 3)
    // The owner talks (back in range), she takes an item and waits. Waiting is not busy (2026-11-20):
    // the next crossing surfaces while it waits, and the list stays 等待中.
    await h.model.add([Scripted("", todoSet("報告寄出了"), todoWait("等檔案")), Scripted("", dm("檔案好了再叫我"))])
    await doll.ownerSaid("幫我寄報告，檔案等等給你")
    try await waitUntil { (try? await doll.dashboardStatus().todo) == "等待中" }
    clock.set(at("2026-11-16", "23:30"))
    try await waitUntil { await h.model.requests.count == 5 }
    try await waitUntil { await doll.settled }
    #expect(try h.events().filter { $0.text.hasPrefix("【走神】") }.count == 2)
    #expect(try await doll.dashboardStatus().todo == "等待中")
    #expect(h.logs.contains { $0.contains("drives: 親近") })
    await doll.stop()
    try await runner.value
}

@Test func duringMaintenanceNothingSurfaces() async throws {
    let clock = ManualClock(at("2026-11-16", "23:00"))
    let b = try await Harness([Scripted("", noteCall("主人最近很累", "owner")), Scripted("", dm("早點睡"))],
                              clock: clock) { $0.maintenance.enabled = true; $0.maintenance.noticeMin = 0; $0.drives.enabled = true; $0.drives.driveHalfLifeH = noClimb }
    await b.model.stall(request: 3)
    let doll = b.doll
    let runner = Task { try await doll.run() }
    try await waitUntil { clock.sleepers >= 1 }
    await doll.ownerSaid("最近好累")
    try await waitUntil { await doll.settled && b.reports.count == 2 }
    clock.set(at("2026-11-17", "03:00"))
    try await waitUntil { await b.model.requests.count == 3 }
    // 親近 0.7 at 23:00 crosses 0.2 at 06:14; maintenance still runs: nothing surfaces.
    clock.set(at("2026-11-17", "06:30"))
    try await Task.sleep(for: .milliseconds(150))
    #expect(await b.model.requests.count == 3)
    #expect(await doll.inMaintenance)
    #expect(try b.events().allSatisfy { !$0.text.hasPrefix("【走神】") })
    await doll.stop()
    try await runner.value
}

@Test func anAssociationAttachesOnlyAboveTheThresholdAndAddsNoTurn() async throws {
    let clock = ManualClock(at("2026-11-16", "10:00"))
    let h = try await Harness([Scripted("", noteCall("鯊魚分類還剩兩步沒弄完", "duty")), Scripted("", dm("記著了"))], clock: clock) {
        $0.drives.enabled = true; $0.drives.driveHalfLifeH = noClimb
        $0.drives.closenessHalfLifeH = 1000
    }
    let doll = h.doll
    let runner = Task { try await doll.run() }
    try await waitUntil { clock.sleepers >= 1 }
    await doll.ownerSaid("先記著鯊魚分類")
    try await waitUntil { await doll.settled && h.reports.count == 2 }
    await h.model.add([Scripted("", dm("好"))])
    await doll.ownerSaid("今天天氣很好")
    try await waitUntil { await doll.settled && h.reports.count == 3 }
    #expect(try h.events().allSatisfy { !$0.text.hasPrefix("【聯想】") })
    await h.model.add([Scripted("", dm("嗯，還剩兩步"))])
    await doll.ownerSaid("鯊魚分類還剩兩步沒弄完嗎")
    try await waitUntil { await doll.settled && h.reports.count == 4 }
    #expect(await h.model.requests.count == 4)
    #expect(await h.model.seen(3).contains { $0.content.hasSuffix("【聯想】（想到）鯊魚分類還剩兩步沒弄完（#1）") })
    #expect(h.logs.contains { $0.contains("drives: association best #1") && $0.contains("(threshold 0.42)") })
    await doll.stop()
    try await runner.value
}

@Test func theProgramNotesCalendarAndFailedItemsAndEndsPassedOnes() async throws {
    let clock = ManualClock(at("2026-11-16", "10:00"))
    let add = ("calendar.add", [("title", "主人看牙醫"), ("date", "2026-11-17"), ("start", "09:00")])
    let h = try await Harness([Scripted("", add), Scripted("", dm("記好了")), Scripted("", todoSet("報告寄出了"), readCall, fail("沒有檔案")),
                               Scripted("", ("todo.set", [("items", "[]")]))], clock: clock) {
        $0.drives.enabled = true; $0.drives.driveHalfLifeH = noClimb
        $0.drives.closenessHalfLifeH = 1000
    }
    let doll = h.doll
    let runner = Task { try await doll.run() }
    try await waitUntil { clock.sleepers >= 1 }
    await doll.ownerSaid("明天九點我看牙醫")
    try await waitUntil { await doll.settled && h.reports.count == 2 }
    var open = await doll.dashboardDrives().open
    #expect(open.map(\.text) == ["主人看牙醫（2026-11-17（星期二） 09:00）"] && open[0].source == "calendar")
    await doll.ownerSaid("幫我寄報告")
    try await waitUntil { await doll.settled && h.reports.count == 4 }
    open = await doll.dashboardDrives().open
    #expect(open.map(\.text).last == "沒做成：報告寄出了（沒有檔案）" && open.last!.source == "todo")
    #expect(try h.toolResults().contains { $0.text.contains("程式記成你的掛念 #2") })
    // Three hours before: 壓力 up once.
    clock.set(at("2026-11-17", "06:30"))
    try await waitUntil { await doll.dashboardDrives().changes.contains { $0.cause.hasPrefix("掛念的事快到了：#1") } }
    // The time passes (the calendar's turn): the concern ends.
    await h.model.add([Scripted("", dm("該出門看牙醫了"))])
    clock.set(at("2026-11-17", "09:00"))
    try await waitUntil { await doll.dashboardDrives().ended.contains { $0.id == 1 && $0.end == "passed" } }
    #expect(await doll.dashboardDrives().changes.filter { $0.cause.hasPrefix("掛念的事快到了") }.count == 1)
    await doll.stop()
    try await runner.value
}

/// 動力 climbs while she is left alone (idle-drive, 2026-11-20): one of her own things surfaces when it
/// crosses, the episode ends when she is idle again (logged even when she did nothing and said
/// nothing), and the next comes one period later, not at once, however much she did in it (the old
/// chain is gone). The dashboard's next crossing is the moment it happens. A list in progress holds it.
@Test func leftAloneHerOwnThingsComeOnePeriodApartAndNeverChain() async throws {
    let clock = ManualClock(at("2026-11-16", "10:00"))
    let h = try await Harness([Scripted("", noteCall("把鯊魚圖鑑讀完", "own"), noteCall("學會畫鯊魚", "own")), Scripted("", dm("想讀圖鑑"))],
                              clock: clock) {
        $0.drives.enabled = true
        $0.drives.closenessHalfLifeH = 1000
    }
    let c = DrivesConfig()
    let period = c.driveHalfLifeH * log2((c.driveRest - c.driveReset) / (c.driveRest - c.driveHigh)) * 3600
    let doll = h.doll
    let runner = Task { try await doll.run() }
    try await waitUntil { clock.sleepers >= 1 }
    await doll.ownerSaid("你有什麼想做的？")
    try await waitUntil { await doll.settled && h.reports.count == 2 }
    let first = at("2026-11-16", "10:00") + period + 1
    #expect(abs(await doll.dashboardDrives().nextCrossing! - first) < 1e-3)
    clock.set(first - 5)
    try await Task.sleep(for: .milliseconds(100))
    #expect(await h.model.requests.count == 2)
    // It crosses: one of hers surfaces with what to hand in. She decides not to, says why, calls nothing.
    await h.model.add([Scripted("先不做，今天想先把圖鑑放著")])
    clock.set(first)
    try await waitUntil { await h.model.requests.count == 3 }
    try await waitUntil { await doll.settled }
    // Her own thing asks for a step: a task turn (effort medium), not none.
    let requests = await h.model.requests
    #expect(requests[2].effort == "medium")
    let w1 = try h.events().filter { $0.text.hasPrefix("【走神】") }.map(\.text)
    #expect(w1 == ["【走神】你想起掛著的一件事（#1，你 2026-11-16（星期一） 10:00 記的）：把鯊魚圖鑑讀完\n" + Concerns.ownAsk])
    try await waitUntil { h.logs.contains { $0.contains("drives: own-thing episode over (動力 0.80 → 0.40): 沒有呼叫工具，沒有對任何人說話；想法「先不做，今天想先把圖鑑放著」") } }
    #expect(abs(await doll.dashboardDrives().nextCrossing! - (first + period + 1)) < 1e-3)
    #expect(await doll.dashboardDrives().changes.last!.cause.hasPrefix("自己的事告一段落：沒有呼叫工具"))
    // The second, one period on: she finishes it (concern.end done raises 動力, which used to chain
    // straight into the next of hers). The episode's end wins: nothing more surfaces.
    await h.model.add([Scripted("", ("concern.end", [("id", "2"), ("how", "done")])), Scripted("畫好了")])
    clock.set(first + period + 1)
    try await waitUntil { await h.model.requests.count == 5 }
    try await waitUntil { await doll.settled }
    try await Task.sleep(for: .milliseconds(100))
    #expect(await h.model.requests.count == 5)
    #expect(h.logs.filter { $0.contains("own-thing episode over") }.count == 2)
    #expect(h.logs.contains { $0.contains("own-thing episode over") && $0.contains("呼叫 concern.end，沒有對任何人說話；想法「畫好了」") })
    #expect(approx(await doll.testDrive(), c.driveReset))
    // A list that waits does not hold the next one (2026-11-20, owner: waiting is not busy); the
    // wandering tells her how to start hers without touching it, and the list still waits.
    await h.model.add([Scripted("", todoSet("報告寄出了"), dm("好，檔案給我就寄"), todoWait("等檔案")), Scripted("先不做")])
    await doll.ownerSaid("幫我寄報告，檔案等等給你")
    try await waitUntil { (try? await doll.dashboardStatus().todo) == "等待中" }
    clock.set(first + 4 * period)
    try await waitUntil { await h.model.requests.count == 7 }
    try await waitUntil { await doll.settled }
    let w3 = try h.events().filter { $0.text.hasPrefix("【走神】") }.map(\.text)
    #expect(w3.count == 3 && w3[2].hasSuffix("（「報告寄出了」在等待中：todo.set 給 subgoal，它會先放在旁邊，不會動到它；不給 subgoal 是改它後面的項目。）"))
    #expect(try await doll.dashboardStatus().todo == "等待中")
    await doll.stop()
    try await runner.value
}

@Test func duringMaintenanceHerOwnThingsDoNotSurface() async throws {
    let clock = ManualClock(at("2026-11-17", "02:30"))
    let b = try await Harness([Scripted("", noteCall("把鯊魚圖鑑讀完", "own")), Scripted("", dm("晚安"))], clock: clock) {
        $0.maintenance.enabled = true; $0.maintenance.noticeMin = 0; $0.drives.enabled = true; $0.drives.closenessHalfLifeH = 1000
    }
    await b.model.stall(request: 3)
    let doll = b.doll
    let runner = Task { try await doll.run() }
    try await waitUntil { clock.sleepers >= 1 }
    await doll.ownerSaid("還沒睡？")
    try await waitUntil { await doll.settled && b.reports.count == 2 }
    clock.set(at("2026-11-17", "03:00"))
    try await waitUntil { await b.model.requests.count == 3 }
    // 動力 crosses about 03:30; the maintenance still runs: nothing surfaces.
    clock.set(at("2026-11-17", "03:45"))
    try await Task.sleep(for: .milliseconds(150))
    #expect(await b.model.requests.count == 3)
    #expect(await doll.inMaintenance)
    #expect(try b.events().allSatisfy { !$0.text.hasPrefix("【走神】") })
    await doll.stop()
    try await runner.value
}

/// While the owner sleeps her own things still come (a goal with nothing under it is one); the
/// wandering carries the fact, and a turn that messages nobody reaches nobody. (The program does not
/// block a DM then: an urgent one may still go, calendar.md 「主人睡覺」.)
@Test func whileTheOwnerSleepsHerOwnThingsStillComeAndCarryTheFact() async throws {
    let clock = ManualClock(at("2026-11-16", "23:30"))
    let h = try await Harness([Scripted("他在睡，不吵他；我先想想這個目標底下要放什麼")], clock: clock) {
        $0.ownerSleep.enabled = true; $0.ownerSleep.start = "23:00"; $0.ownerSleep.end = "07:00"
        $0.drives.enabled = true; $0.drives.closenessHalfLifeH = 1000
    }
    let doll = h.doll
    _ = try await doll.goalAdd("懂海洋生物", parent: nil, by: "her")
    let runner = Task { try await doll.run() }
    try await waitUntil { clock.sleepers >= 1 }
    clock.set(at("2026-11-17", "00:31"))
    try await waitUntil { await h.model.requests.count == 1 }
    try await waitUntil { await doll.settled }
    let fact = "（主人現在應該在睡（23:00–07:00）；不急的事等他醒了再說）"
    let w = try h.events().filter { $0.text.hasPrefix("【走神】") }.map(\.text)
    #expect(w.count == 1 && w[0].contains("目標 #1「懂海洋生物」底下還沒有子目標") && w[0].hasSuffix(Concerns.ownAsk + "\n" + fact))
    try await waitUntil { h.logs.contains { $0.contains("own-thing episode over") && $0.contains("沒有對任何人說話") } }
    #expect(h.said.isEmpty)
    await doll.stop()
    try await runner.value
}

@Test func herConcernToolsCheckWhatTheyAreGiven() async throws {
    let clock = ManualClock(at("2026-11-16", "10:00"))
    let h = try await Harness([Scripted("", noteCall("", "owner"), ("concern.note", [("text", "x"), ("about", "y")]),
                                        ("concern.note", [("text", "牙醫"), ("about", "duty"), ("due", "明天")]),
                                        ("concern.end", [("id", "9"), ("how", "done")]),
                                        ("owner.feedback", [("kind", "praise"), ("quote", "你好棒")]),
                                        ("owner.feedback", [("kind", "praise"), ("quote", "謝啦")])),
                               Scripted("", dm("嘿嘿"))], clock: clock) {
        $0.drives.enabled = true; $0.drives.driveHalfLifeH = noClimb
    }
    let doll0 = h.doll
    let runner = Task { try await doll0.run() }
    try await waitUntil { clock.sleepers >= 1 }
    await h.doll.ownerSaid("謝啦")
    try await waitUntil { await h.doll.settled && h.reports.count == 2 }
    let r = try h.toolResults().map(\.text)
    #expect(r[0].hasPrefix("失敗：") && r[0].contains("text 是空的"))
    #expect(r[1].contains("about 只能是 owner、duty、own"))
    #expect(r[2].contains("due 要寫成 YYYY-MM-DD HH:MM"))
    #expect(r[3].contains("沒有 #9 這件掛念"))
    #expect(r[4].contains("今天主人的話裡找不到「你好棒」"))
    #expect(r[5] == "記下了。")
    #expect(await h.doll.dashboardDrives().changes.contains { $0.cause == "主人誇她：「謝啦」" && $0.signal == .drive })
    await h.doll.stop()
    try await runner.value
}

/// m6-drives real runs (2026-11-16): a turn whose only call was owner.feedback (2/3 after a
/// correction, once after praise) or concern.note (once after the owner's line) ended there, and the
/// owner's line never got an answer. The bookkeeping calls give her the next turn, like `remember`.
@Test func aTurnThatOnlyRecordsFeedbackOrAConcernStillGetsToAnswer() async throws {
    for call in [("owner.feedback", [("kind", "correction"), ("quote", "太正式了")]), noteCall("主人今天很煩", "owner"),
                 ("concern.end", [("id", "1"), ("how", "let_go")])] {
        let h = try await Harness { $0.drives.enabled = true; $0.drives.driveHalfLifeH = noClimb }
        await h.doll.ownerSaid("不對啦，太正式了")
        if call.0 == "concern.end" { try await h.turn(Scripted("", noteCall("阿凱的訊息", "duty"))) }
        try await h.turn(Scripted("", call))
        #expect(try h.toolResults().last!.text.hasPrefix(call.0 == "concern.end" ? "#1 結束了" : "記下了"))
        #expect(await h.doll.followUp, "\(call.0)")
        try await h.turn(Scripted("", dm("好，改成隨便一點的")))
        #expect(h.said == ["好，改成隨便一點的"])
    }
}

@Test func theExampleDrivesSectionIsTheDefaultsAndBadValuesAreRefused() throws {
    #expect(try exampleConfig().drives == DrivesConfig())
    let file = FileManager.default.temporaryDirectory.appending(path: "drives-\(UUID().uuidString).toml")
    let head = "[llm]\nbase_url = \"http://x\"\nmodel = \"m\"\n[memory]\n"
        + SourceTier.allCases.map { "confidence_\($0.rawValue) = 0.5\n" }.joined()
    for bad in ["[drives]\norder = [\"stress\"]\n", "[drives]\nsleepiness_high = 0.8\n", "[drives]\nstress_high = 0.05\n",
                "[drives]\ncloseness_start = 0.1\n", "[drives]\nmood = 1\n"] {
        try (head + bad).write(to: file, atomically: true, encoding: .utf8)
        #expect(throws: TOMLError.self, "\(bad)") { try Config.load(file) }
    }
}

// when the owner is usually asleep (2026-11-16) ----------------------------------------------------

@Test func theOwnerSleepItemIsCreatedOnceMovableOverMidnightNotDeletableOrAllDay() async throws {
    let clock = ManualClock(at("2026-11-16", "22:00"))
    let h = try await Harness(clock: clock) { $0.ownerSleep.enabled = true }
    try await h.doll.announceBoot()
    let items = await h.doll.dashboardCalendar().items
    #expect(items.count == 1 && items[0].title == "主人睡覺" && items[0].start == "00:00" && items[0].end == "08:00"
            && items[0].repeat == "daily" && items[0].by == "program")
    #expect(h.logs.contains { $0.contains("calendar: had no 主人睡覺; added 00:00–08:00 (daily)") })
    // The next boot finds it: not added again.
    let b = try await Harness(dir: h.dir, clock: clock) { $0.ownerSleep.enabled = true }
    try await b.doll.announceBoot()
    #expect(await b.doll.dashboardCalendar().items.count == 1)
    #expect(!b.logs.contains { $0.contains("had no 主人睡覺") })
    for (op, p, says) in [("delete", ["id": "1"], "不能刪掉"), ("change", ["id": "1", "all_day": "true"], "不能改成全天"),
                          ("change", ["id": "1", "end": "00:00"], "不能和 start 一樣")] {
        do { _ = try await b.doll.dashboardCalendarEdit(op, p); Issue.record("accepted \(op) \(p)") } catch let e as CalendarError {
            #expect(e.description.contains(says), "\(e)")
        }
    }
    // Over midnight: the end is the next day's; start alone keeps the 8 hours.
    #expect(try await b.doll.dashboardCalendarEdit("change", ["id": "1", "start": "23:00", "end": "07:00"]).contains("23:00–隔天 07:00 主人睡覺"))
    #expect(try await b.doll.dashboardCalendarEdit("change", ["id": "1", "on": "2026-11-17", "start": "01:00"]).contains("01:00–09:00 主人睡覺（#1，每天，這天改過）"))
    #expect(try await b.doll.calendarTool(.change(id: 1, on: nil, title: nil, day: nil, start: TimeOfDay("22:30")!, end: nil, allDay: false))
            .contains("22:30–隔天 06:30 主人睡覺（#1，每天）。開始和結束都不會叫你"))
    // An over-midnight span reads back from the file.
    let c = try await Harness(dir: h.dir, clock: clock) { $0.ownerSleep.enabled = true }
    #expect(await c.doll.dashboardCalendar().items[0].end == "06:30")
}

@Test func theStatusSaysHeShouldBeAsleepOnlyInsideTheSpanAlsoOverMidnight() async throws {
    let clock = ManualClock(at("2026-11-16", "21:00"))
    let h = try await Harness([Scripted("", dm("怎麼還沒睡"))], clock: clock) {
        $0.ownerSleep.enabled = true; $0.ownerSleep.start = "23:00"; $0.ownerSleep.end = "07:00"
    }
    try await h.doll.announceBoot()
    let asleep = "- 主人現在應該在睡（23:00–07:00）；不急的事等他醒了再說"
    for (day, time, inside) in [("2026-11-16", "21:00", false), ("2026-11-16", "22:59", false), ("2026-11-16", "23:00", true),
                                ("2026-11-17", "00:00", true), ("2026-11-17", "06:59", true), ("2026-11-17", "07:00", false)] {
        clock.set(at(day, time))
        let f = try await h.doll.status()
        #expect(f.contains(asleep) == inside, "\(day) \(time)")
        #expect(!f.contains("主人平常") && !f.contains("應該在睡") || inside, "\(day) \(time)")
    }
    // One night moved later (its start goes to the next day, so `date` too): that night's span counts.
    _ = try await h.doll.dashboardCalendarEdit("change", ["id": "1", "on": "2026-11-17", "date": "2026-11-18", "start": "01:00", "end": "09:00"])
    clock.set(at("2026-11-18", "00:30"))
    #expect(try await !h.doll.status().contains("應該在睡"))
    clock.set(at("2026-11-18", "08:30"))
    #expect(try await h.doll.status().contains("- 主人現在應該在睡（01:00–09:00）"))
    // He speaks inside the span: he was awake then, and the line says when.
    clock.set(at("2026-11-18", "23:30"))
    let doll = h.doll
    let runner = Task { try await doll.run() }
    await doll.ownerSaid("還沒睡")
    try await waitUntil { h.said == ["怎麼還沒睡"] }
    #expect(await h.model.seen(0).last!.content.contains("- 主人平常這時候在睡（23:00–07:00），不過他 23:30 還跟你說過話"))
    clock.set(at("2026-11-19", "23:10"))
    #expect(try await doll.status().contains(asleep))
    await doll.stop()
    try await runner.value
}

@Test func theSpanGivesNoTurnAndTheNoticeAndWanderingInsideItCarryTheFactAndADMStillGoes() async throws {
    let clock = ManualClock(at("2026-11-16", "21:00"))
    let h = try await Harness([Scripted("", noteCall("明天問主人牙醫說什麼", "owner")), Scripted("", dm("晚安")), Scripted(""), Scripted("none"), planReply, summaryReply,
                               Scripted("", dm("醫生到底說什麼啦？"))], clock: clock) {
        $0.ownerSleep.enabled = true; $0.ownerSleep.start = "23:00"; $0.ownerSleep.end = "07:00"
        $0.maintenance.enabled = true; $0.maintenance.noticeMin = 10; $0.drives.enabled = true; $0.drives.driveHalfLifeH = noClimb
    }
    let doll = h.doll
    let runner = Task { try await doll.run() }
    try await waitUntil { clock.sleepers >= 1 }
    await doll.ownerSaid("今天去看牙醫，晚安")
    try await waitUntil { h.said == ["晚安"] }
    #expect(await !h.model.seen(0).last!.content.contains("應該在睡"))
    // Its start: no turn.
    clock.set(at("2026-11-16", "23:00"))
    try await waitUntil { h.logs.contains { $0.contains("主人睡覺 began (no turn)") } }
    try await Task.sleep(for: .milliseconds(100))
    #expect(await h.model.requests.count == 2)
    // The maintenance notice at 03:00 carries it.
    clock.set(at("2026-11-17", "03:00"))
    try await waitUntil { await h.model.requests.count == 3 }
    let fact = "（主人現在應該在睡（23:00–07:00）；不急的事等他醒了再說）"
    #expect(try h.events().last { $0.kind == .calendar }!.text == Doll.maintenanceNotice(10) + "\n" + fact)
    clock.set(at("2026-11-17", "03:10"))
    try await waitUntil { h.logs.contains { $0.contains("maintenance: ended") } }
    // 親近 0.7 at 21:00 crosses 0.2 at 04:14: the wandering carries it, and her DM is sent.
    clock.set(at("2026-11-17", "04:30"))
    try await waitUntil { h.said.count == 2 }
    let wander = try h.events().filter { $0.text.hasPrefix("【走神】") }.map(\.text)
    #expect(wander.count == 1 && wander[0].hasSuffix("：明天問主人牙醫說什麼\n" + fact))
    #expect(h.said.last == "醫生到底說什麼啦？")
    // Its end: no turn.
    try await waitUntil { await doll.settled }
    clock.set(at("2026-11-17", "07:00"))
    try await Task.sleep(for: .milliseconds(150))
    #expect(await h.model.requests.count == 7)
    #expect(!h.logs.contains { $0.contains("consolidate failed") || $0.contains("concerns failed") })
    await doll.stop()
    try await runner.value
}

@Test func aConcernSheLeftWhileHeSleptComesBackOnceAtHisNextLine() async throws {
    // told == false: at the night wandering she leaves it; his 「早～」 brings it back, once.
    // told == true: she messaged him in that turn; nothing is held.
    for told in [false, true] {
        let clock = ManualClock(at("2026-11-16", "21:00"))
        let h = try await Harness([Scripted("", noteCall("明天問主人牙醫說什麼", "owner")), Scripted("", dm("晚安")),
                                   told ? Scripted("", dm("醫生說什麼？")) : Scripted("他還在睡，等他醒了再說"),
                                   Scripted("", dm("早，牙醫說什麼？")), Scripted("", dm("好"))], clock: clock) {
            $0.ownerSleep.enabled = true; $0.ownerSleep.start = "23:00"; $0.ownerSleep.end = "07:00"; $0.drives.enabled = true; $0.drives.driveHalfLifeH = noClimb
        }
        let doll = h.doll
        let runner = Task { try await doll.run() }
        try await waitUntil { clock.sleepers >= 1 }
        await doll.ownerSaid("今天去看牙醫，晚安")
        try await waitUntil { h.said == ["晚安"] }
        // 親近 0.7 at 21:00 crosses 0.2 at 04:14.
        clock.set(at("2026-11-17", "04:30"))
        try await waitUntil { await h.model.requests.count == 3 }
        try await waitUntil { await doll.settled }
        let file = try #require(try Concerns.read(ConcernFile.self, h.dir.appending(path: "concerns.json"), what: "concerns"))
        #expect((file.items[0].held != nil) == !told)
        clock.set(at("2026-11-17", "07:30"))
        await doll.ownerSaid("早～")
        try await waitUntil { await h.model.requests.count == 4 }
        try await waitUntil { await doll.settled }
        let back = "【聯想】主人在睡的時候你想起這件事，那時沒找他；現在他在了：明天問主人牙醫說什麼（#1）"
        #expect(try h.events().filter { $0.text == back }.count == (told ? 0 : 1))
        await doll.ownerSaid("嗯")
        try await waitUntil { await h.model.requests.count == 5 }
        try await waitUntil { await doll.settled }
        #expect(try h.events().filter { $0.text == back }.count == (told ? 0 : 1))
        await doll.stop()
        try await runner.value
    }
}

@Test func outsideTheSpanTheWanderingAndTheNoticeAreUnchanged() async throws {
    let clock = ManualClock(at("2026-11-16", "12:00"))
    let h = try await Harness(clock: clock) { $0.ownerSleep.enabled = true }
    try await h.doll.announceBoot()
    #expect(await h.doll.withOwnerSleep("【走神】x") == "【走神】x")
    clock.set(at("2026-11-17", "03:00"))
    #expect(await h.doll.withOwnerSleep("【走神】x") == "【走神】x\n（主人現在應該在睡（00:00–08:00）；不急的事等他醒了再說）")
}

@Test func theInstructionsTellHerTheFactNotARule() {
    let t = Instructions.text(voice: false, context: ContextConfig(), docs: "/d", drives: true, ownerSleep: true)
    #expect(t.contains("行事曆上的「主人睡覺」是主人平常睡覺的時間，開始和結束都不會叫你；在那段時間裡【狀況】會寫他應該在睡。那時候想到要跟他說的事，不急就用 concern.note 記著，等他醒了再說；急事（像他交代要叫他起床）照樣可以找他。"))
    #expect(!Instructions.text(voice: false, context: ContextConfig(), docs: "/d").contains("主人睡覺"))
}

@Test func ownerSleepConfigIsStrict() throws {
    let dir = FileManager.default.temporaryDirectory.appending(path: "dollcore-test-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    let file = dir.appending(path: "c.toml")
    let head = try String(contentsOf: root.appending(path: "dollcore.example.toml"), encoding: .utf8)
        .replacingOccurrences(of: "[owner_sleep]\n", with: "[owner_sleep_old]\n")
    #expect(try exampleConfig().ownerSleep.end == "08:00")
    for bad in ["[owner_sleep]\nstart = \"25:00\"\n", "[owner_sleep]\nstart = \"07:00\"\nend = \"07:00\"\n", "[owner_sleep]\nquiet = true\n"] {
        try (head.components(separatedBy: "[owner_sleep_old]")[0] + bad).write(to: file, atomically: true, encoding: .utf8)
        #expect(throws: TOMLError.self, "\(bad)") { try Config.load(file) }
    }
}

@Test func anOldCalendarGetsTheOwnerSleepItemAtBootAndItsPassedStartIsNotListed() async throws {
    let clock = ManualClock(at("2026-11-16", "10:00"))
    let dir = FileManager.default.temporaryDirectory.appending(path: "dollcore-test-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    try Data(#"""
        {"fired_through": "2026-11-15T09:00:00.000+08:00", "next_id": 3, "items": [
          {"id": 1, "title": "系統維護", "date": "2026-11-15", "start": "03:00", "repeat": "daily", "by": "program", "role": "maintenance"},
          {"id": 2, "title": "看牙醫", "date": "2026-11-18", "start": "15:00", "by": "owner"}]}
        """#.utf8).write(to: dir.appending(path: "calendar.json"))
    let h = try await Harness(dir: dir, clock: clock) { $0.ownerSleep.enabled = true }
    try await h.doll.announceBoot()
    #expect(await h.doll.dashboardCalendar().items.map(\.title) == ["系統維護", "看牙醫", "主人睡覺"])
    // Down a whole night with a span stored: a passed start of it is not among the missed ones.
    let dir2 = FileManager.default.temporaryDirectory.appending(path: "dollcore-test-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: dir2, withIntermediateDirectories: true)
    try Data(#"""
        {"fired_through": "2026-11-15T09:00:00.000+08:00", "next_id": 2, "items": [
          {"id": 1, "title": "主人睡覺", "date": "2026-11-15", "start": "23:00", "end": "07:00", "repeat": "daily", "by": "program", "role": "owner_sleep"}]}
        """#.utf8).write(to: dir2.appending(path: "calendar.json"))
    let b = try await Harness(dir: dir2, clock: clock) { $0.ownerSleep.enabled = true }
    try await b.doll.announceBoot()
    #expect(try b.events().allSatisfy { !$0.text.contains("沒開機的時候過了") })
    #expect(await b.doll.dashboardCalendar().items.count == 1)
}

// pre-swap fixes (2026-11-16) ------------------------------------------------------------------

@Test func eachTurnRecordsWhyTheModelStoppedInTheLogAndTheTimelineAcrossARestart() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("嗨")
    try await h.turn("想一下")
    await h.doll.ownerSaid("再來")
    try await h.turn(Scripted("", dm("好")))
    #expect(h.logs.contains { $0.contains("turn 1 ") && $0.contains("finish=stop completion=10 reasoning_chars=0") })
    #expect(h.logs.contains { $0.contains("turn 2 ") && $0.contains("finish=tool_calls completion=10") })
    let replies = try h.events().filter { $0.kind == .doll }.map { $0.seq! }
    let stats = await h.doll.turnStats()
    #expect(replies.map { stats[$0]?.finish } == ["stop", "tool_calls"] && replies.allSatisfy { stats[$0]?.completion == 10 })
    // Read back from the timeline file.
    let again = try Timeline(dir: h.dir.appending(path: "timeline"), box: try testBox(h.dir))
    #expect(replies.map { again.turnStats[$0]?.finish } == ["stop", "tool_calls"])
}

@Test func aReplyWithoutACallIsSentBackAsAProgramLineAndAnEmptyOneNotAtAll() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("嗨")
    try await h.turn("[思考] 現在是")
    await h.doll.ownerSaid("在嗎")
    try await h.turn("")
    await h.doll.ownerSaid("早～")
    try await h.turn(Scripted("", dm("早")))
    let msgs = await h.model.seen(2)
    #expect(msgs.contains(ChatMessage(role: "user", content: Doll.thoughtOnlyTag + "[思考] 現在是")))
    #expect(!msgs.contains { $0.role == "assistant" })
    #expect(!msgs.contains { $0.content == Doll.thoughtOnlyTag })
}

@Test func theAssociationThresholdIsItsOwnDrivesSetting() throws {
    #expect(try exampleConfig().drives.associationSimilarity == DrivesConfig().associationSimilarity)
    let dir = FileManager.default.temporaryDirectory.appending(path: "dollcore-test-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    let file = dir.appending(path: "c.toml")
    let example = try String(contentsOf: root.appending(path: "dollcore.example.toml"), encoding: .utf8)
    func with(_ key: String, _ value: String) -> String {
        example.split(separator: "\n", omittingEmptySubsequences: false).map { $0.hasPrefix(key + " =") ? Substring(key + " = " + value) : $0 }.joined(separator: "\n")
    }
    try with("association_similarity", "0.3").write(to: file, atomically: true, encoding: .utf8)
    #expect(try Config.load(file).drives.associationSimilarity == 0.3)
    try with("association_similarity", "1.5").write(to: file, atomically: true, encoding: .utf8)
    #expect(throws: TOMLError.self) { try Config.load(file) }
    try with("maintenance_concerns_max", "-1").write(to: file, atomically: true, encoding: .utf8)
    #expect(throws: TOMLError.self) { try Config.load(file) }
}

@Test func anAssociationFiresAtTheConfiguredThresholdNotSelects() async throws {
    let clock = ManualClock(at("2026-11-16", "10:00"))
    let h = try await Harness([Scripted("", noteCall("主人的牙齒", "owner")), Scripted("", dm("嗯")), Scripted("", dm("好"))], clock: clock) {
        $0.drives.enabled = true; $0.drives.driveHalfLifeH = noClimb; $0.drives.associationSimilarity = -1; $0.select.similarity = 0.99
    }
    let doll = h.doll
    let runner = Task { try await doll.run() }
    try await waitUntil { clock.sleepers >= 1 }
    await doll.ownerSaid("記一下")
    try await waitUntil { h.said == ["嗯"] }
    await doll.ownerSaid("今天好熱")
    try await waitUntil { h.said == ["嗯", "好"] }
    #expect(h.logs.contains { $0.contains("drives: association best #1") && $0.contains("(threshold -1.00)") })
    #expect(try h.events().contains { $0.text == "【聯想】（想到）主人的牙齒（#1）" })
    await doll.stop()
    try await runner.value
}

/// The maintenance's concern extraction: a day with one owner line and her answer, then the extraction reply.
private func extractionDay(_ extraction: Scripted, noted: Bool = false, notice: Int = 0, said: String = "辛苦了") async throws -> (Harness, ManualClock, Task<Void, any Error>) {
    let clock = ManualClock(at("2026-11-16", "21:00"))
    var replies: [Scripted] = noted ? [Scripted("", noteCall("主人明天九點開會，有點壓力", "owner"))] : []
    replies += [Scripted("", dm(said)), extraction, planReply, summaryReply]
    let h = try await maintenanceHarness(clock, drives: true, notice: notice, replies)
    let doll = h.doll
    let runner = Task { try await doll.run() }
    try await waitUntil { clock.sleepers >= 1 }
    await doll.ownerSaid("我回來了，今天去看了牙醫，醫生說下週三下午兩點還要再去一次。明天九點開會")
    try await waitUntil { h.said == [said] }
    return (h, clock, runner)
}

private func concernsFile(_ h: Harness) throws -> [[String: Any]] {
    let o = try JSONSerialization.jsonObject(with: Data(contentsOf: h.dir.appending(path: "concerns.json"))) as! [String: Any]
    return o["items"] as! [[String: Any]]
}

@Test func maintenanceNotesConcernsFromTheDayAndSaysSoInTheEndNotice() async throws {
    let (h, clock, runner) = try await extractionDay(Scripted("""
        note owner: 主人看完牙醫，問他牙齒還痛不痛 ｜根據：今天去看了牙醫
        note duty due 2026-11-25 12:30: 提醒主人下週三兩點複診 ｜根據：「下週三下午兩點還要再去一次」
        """))
    clock.set(at("2026-11-17", "03:00"))
    try await waitUntil { h.logs.contains { $0.contains("maintenance: ended") } }
    let reqs = await h.model.requests
    // The extraction is one request before the consolidation, with the consolidation's effort and no tools.
    #expect(reqs.count == 4 && reqs[1].effort == reqs[2].effort && reqs[1].messages[0].content == ConcernExtraction.system(cap: 3))
    #expect(reqs[1].messages[1].content.contains("今天去看了牙醫") && reqs[1].messages[1].content.contains("已經掛著的：\n（沒有）"))
    #expect(reqs[1].messages[1].content.contains("她說｜Discord 私訊｜自己的話] 辛苦了") && reqs[1].messages[0].content.contains(Consolidation.ownWords))
    // The consolidation and the day's summary read her lines as hers too, with the rule (2026-11-22).
    #expect(reqs[2].messages[1].content.contains("她說｜Discord 私訊｜自己的話] 辛苦了") && reqs[2].messages[0].content.contains(Consolidation.ownWords))
    #expect(reqs[3].messages[0].content == Doll.talkInstructions + Consolidation.summaryOwnWords
            && reqs[3].messages[1].content.contains("她說｜Discord 私訊｜自己的話] 辛苦了"))
    let items = try concernsFile(h)
    #expect(items.map { $0["text"] as? String } == ["主人看完牙醫，問他牙齒還痛不痛", "提醒主人下週三兩點複診"])
    #expect(items.allSatisfy { $0["source"] as? String == "maintenance" } && items.map { $0["about"] as? String } == ["owner", "duty"])
    #expect(items[1]["due"] as? Double == Day("2026-11-25")!.at(TimeOfDay("12:30")!) && items[0]["due"] == nil)
    let end = try segmentTexts(h)[1][0]
    #expect(end.hasPrefix("【系統維護】結束，現在 03:00。從今天的對話記了 2 件掛著的事：#1〔主人〕主人看完牙醫，問他牙齒還痛不痛；#2〔該做的〕提醒主人下週三兩點複診。\(didOne)"))
    #expect(Concerns.origin(Concern(id: 1, text: "", about: .owner, source: "maintenance", created: at("2026-11-17", "03:00")))
            == "#1，系統維護 2026-11-17（星期二） 03:00 從那天的對話記的")
    await h.doll.stop()
    try await runner.value
}

@Test func maintenanceDoesNotRepeatAHeldConcernAndKeepsToTheCap() async throws {
    let (h, clock, runner) = try await extractionDay(Scripted("""
        note owner: 主人明天九點開會，有點壓力。 ｜根據：明天九點開會
        note owner: 問主人牙齒還痛不痛 ｜根據：今天去看了牙醫
        note owner: 問主人牙齒還痛不痛 ｜根據：今天去看了牙醫
        note own: 想學畫鯊魚 ｜根據：我回來了
        note duty: 提醒主人複診 ｜根據：還要再去一次
        note owner: 主人晚餐吃太辣 ｜根據：我回來了
        """), noted: true)
    clock.set(at("2026-11-17", "03:00"))
    try await waitUntil { h.logs.contains { $0.contains("maintenance: ended") } }
    let reqs = await h.model.requests
    #expect(reqs[2].messages[1].content.contains("已經掛著的：\n#1〔owner〕主人明天九點開會，有點壓力"))
    // The held one (same text but for punctuation) and the repeat are dropped; three fit under the cap.
    #expect(try concernsFile(h).map { $0["text"] as? String } == ["主人明天九點開會，有點壓力", "問主人牙齒還痛不痛", "想學畫鯊魚", "提醒主人複診"])
    #expect(h.logs.contains { $0.contains("maintenance concern left out: duplicate「主人明天九點開會，有點壓力」") })
    #expect(h.logs.contains { $0.contains("maintenance concern left out: duplicate「問主人牙齒還痛不痛」") })
    #expect(h.logs.contains { $0.contains("maintenance concern left out: over the cap (3)「主人晚餐吃太辣」") })
    #expect(h.logs.contains { $0.contains("maintenance: concerns done") && $0.contains("3 noted, 0 ended, 3 left out") })
    await h.doll.stop()
    try await runner.value
}

@Test func maintenanceEndsAConcernOnlyWithTheOwnersOwnWords() async throws {
    let plan = try ConcernExtraction.parse("""
        end #1: 明天九點開會
        end #2: 牙齒不痛了
        end #3: 明天九點開會
        """, open: [Concern(id: 1, text: "開會", about: .owner, source: "her", created: 0),
                    Concern(id: 2, text: "牙齒", about: .owner, source: "maintenance", created: 0),
                    Concern(id: 3, text: "開會（行事曆）", about: .duty, source: "calendar", created: 0)],
        ownerLines: ["明天九點開會"], others: [], own: [], cap: 3, room: 20)
    #expect(plan.ends == [.init(id: 1, quote: "明天九點開會")] && plan.dropped.count == 2)
    #expect(try ConcernExtraction.parse("none", open: [], ownerLines: [], others: [], own: [], cap: 3, room: 20) == .init())
    #expect(try ConcernExtraction.parse("note owner: 再問一聲。｜根據：再問", open: [], ownerLines: [], others: ["再問一聲吧"], own: [], cap: 3, room: 20)
        .notes.map(\.text) == ["再問一聲"])
    #expect(throws: ModelError.self) { try ConcernExtraction.parse("我覺得主人很累", open: [], ownerLines: [], others: [], own: [], cap: 3, room: 20) }
    #expect(throws: ModelError.self) { try ConcernExtraction.parse("note friend: x", open: [], ownerLines: [], others: [], own: [], cap: 3, room: 20) }
    #expect(ConcernExtraction.doneText(added: [], ended: []) == "從今天的對話沒有另外記掛著的事。")
    #expect(ConcernExtraction.doneText(added: [], ended: [(Concern(id: 1, text: "開會", about: .owner, source: "her", created: 0), "開完了")])
            == "結束了 1 件：#1 開會（主人說「開完了」）。")
}

/// Her own words are no source of facts (2026-11-22): a concern whose basis is only in her lines is
/// written 「她說過…（沒查證）」 (a duty's source is her promise, so it stays); one with no basis, or a
/// basis found nowhere in the day, is left out.
@Test func aConcernOnlyHerOwnWordsSupportIsWrittenAsSomethingSheSaid() throws {
    let others = ["【開機】你剛被開機了。\n- 結束：沒有結束的紀錄，最後一行 log 在 11-20 17:12:51，之後就沒有了。", "下班了"]
    let own = ["下午 17:12 左右我這裡斷了一次", "我明天幫你查天氣"]
    let plan = try ConcernExtraction.parse("""
        note owner: 今天17:12那台電腦沒有錯誤紀錄地斷掉，之後問一下 ｜根據：17:12 左右我這裡斷了一次
        note own: 最後一行 log 停在 17:12，之後再看是不是斷電 ｜根據：最後一行 log 在 11-20 17:12:51
        note duty: 明天幫主人查天氣 ｜根據：「我明天幫你查天氣」
        note owner: 她說過主人下班了（沒查證） ｜根據：我這裡斷了一次
        note owner: 主人下班了，問他累不累
        note owner: 主人的電腦常常斷電 ｜根據：電腦常常斷電
        """, open: [], ownerLines: ["下班了"], others: others, own: own, cap: 9, room: 20)
    #expect(plan.notes.map(\.text) == ["她說過今天17:12那台電腦沒有錯誤紀錄地斷掉，之後問一下（沒查證）", "最後一行 log 停在 17:12，之後再看是不是斷電",
                                       "明天幫主人查天氣", "她說過主人下班了（沒查證）"])
    #expect(plan.dropped == ["no basis「主人下班了，問他累不累」", "basis「電腦常常斷電」 is not in the day「主人的電腦常常斷電」"])
    #expect(ConcernExtraction.sources([stamped(.memory, Doll.bootNotice + "x", 1), stamped(Scripted("想", dm("說")), 2),
                                       stamped(.tool, "送出了", 3), stamped(.owner, "嗨", 4)])
            == (others: [Doll.bootNotice + "x", "送出了", "嗨"], own: ["想", "說"]))
}

/// The trial's case end to end: she told the owner a cut she only inferred; the extraction's note
/// sourced from her line is written as hers, unverified.
@Test func maintenanceWritesAConcernFromHerOwnLineAsSomethingSheSaid() async throws {
    let (h, clock, runner) = try await extractionDay(Scripted("note owner: 今天17:12那台電腦沒有錯誤紀錄地斷掉 ｜根據：17:12 左右我這裡斷了一次"),
                                                     said: "下午 17:12 左右我這裡斷了一次")
    clock.set(at("2026-11-17", "03:00"))
    try await waitUntil { h.logs.contains { $0.contains("maintenance: ended") } }
    let reqs = await h.model.requests
    #expect(reqs[1].messages[1].content.contains("她說｜Discord 私訊｜自己的話] 下午 17:12 左右我這裡斷了一次"))
    #expect(try concernsFile(h).map { $0["text"] as? String } == ["她說過今天17:12那台電腦沒有錯誤紀錄地斷掉（沒查證）"])
    await h.doll.stop()
    try await runner.value
}

@Test func anInterruptedExtractionWritesNothingAndARestartGoesOnWithItsWordsKept() async throws {
    let clock = ManualClock(at("2026-11-16", "21:00"))
    let h = try await maintenanceHarness(clock, drives: true, [Scripted("", dm("辛苦了")), Scripted("note owner: 問主人牙齒還痛不痛 ｜根據：今天去看了牙醫"),
                                                               Scripted("", dm("還沒睡？"))])
    let doll = h.doll
    let runner = Task { try await doll.run() }
    try await waitUntil { clock.sleepers >= 1 }
    await doll.ownerSaid("今天去看了牙醫")
    try await waitUntil { h.said == ["辛苦了"] }
    await h.model.stall(request: 2)
    clock.set(at("2026-11-17", "03:00"))
    try await waitUntil { await h.model.requests.count == 2 }
    await doll.ownerSaid("欸你還在嗎")
    try await waitUntil { h.logs.contains { $0.contains("maintenance: concerns cancelled (interrupted)") } }
    try await waitUntil { h.said == ["辛苦了", "還沒睡？"] }
    let noted = FileManager.default.fileExists(atPath: h.dir.appending(path: "concerns.json").path) ? try concernsFile(h) : []
    #expect(noted.isEmpty)
    #expect(try maintenanceFile(h).done.isEmpty)
    await doll.stop()
    try await runner.value
    // A restart in the middle (concerns done, consolidation not yet) keeps the concerns' words for the notice.
    var m = try maintenanceFile(h)
    m.running = at("2026-11-17", "04:00"); m.interrupted = false; m.done = ["concerns"]; m.words = ["從今天的對話記了 1 件掛著的事：#1〔主人〕問主人牙齒還痛不痛。"]
    let enc = JSONEncoder()
    try enc.encode(m).write(to: h.dir.appending(path: "maintenance.json"))
    try Concerns.write(ConcernFile(nextId: 2, items: [Concern(id: 1, text: "問主人牙齒還痛不痛", about: .owner, source: "maintenance", created: 0)]),
                       h.dir.appending(path: "concerns.json"))
    clock.set(at("2026-11-17", "04:00"))
    let b = try await maintenanceHarness(clock, dir: h.dir, drives: true, [planReply, summaryReply])
    let doll2 = b.doll
    let runner2 = Task { try await doll2.run() }
    try await waitUntil { b.logs.contains { $0.contains("maintenance: ended") } }
    #expect(await b.model.requests.count == 2)
    let end = try segmentTexts(b).last![0]
    #expect(end.hasPrefix("【系統維護】結束，現在 04:00。從今天的對話記了 1 件掛著的事：#1〔主人〕問主人牙齒還痛不痛。\(didOne)"))
    // The concerns file written as "next_id" reads back (it could not before 2026-11-16).
    #expect(await doll2.dashboardDrives().open.map(\.id) == [1])
    await doll2.stop()
    try await runner2.value
}

// her own direction (m6-drives phase 2, 2026-11-16) ---------------------------------------------------

extension Doll {
    /// Starts the signals now without running the loop (the loop starts them at its first catch-up).
    func testStartSignals() {
        drives = Drives(config: drivesConfig, now: wallClock.now())
        signalsFresh = false
    }
    func testDrive() -> Double { drives.value(.drive, at: wallClock.now()) }
}

private func goalsHarness(_ clock: ManualClock, dir: URL? = nil, _ replies: [Scripted] = [],
                          tune: @escaping (inout Config) -> Void = { _ in }) async throws -> Harness {
    try await Harness(replies, dir: dir, clock: clock) { $0.drives.enabled = true; tune(&$0) }
}

private func todoSetSub(_ sub: Int, _ items: String...) -> (String, [(String, String)]) {
    ("todo.set", [("items", String(decoding: try! JSONEncoder().encode(items), as: UTF8.self)), ("subgoal", "\(sub)")] + planArgs)
}

@Test func theTreeHasExactlyThreeLevelsAndItsLimits() async throws {
    let clock = ManualClock(at("2026-11-16", "10:00"))
    let h = try await goalsHarness(clock) { $0.goals.goalsOpenMax = 2; $0.goals.subgoalsOpenMax = 1; $0.goals.stepsOpenMax = 2 }
    let d = h.doll
    let g = try await d.goalAdd("懂海洋生物", parent: nil, by: "her")
    let s = try await d.goalAdd("弄懂鯊魚的分類", parent: g.id, by: "her")
    let st = try await d.goalAdd("讀維基的鯊魚條目", parent: s.id, by: "her")
    #expect(g.level == .goal && s.level == .subgoal && st.level == .step)
    await #expect { try await d.goalAdd("更細的", parent: st.id, by: "her") } throws: { "\($0)".contains("樹只有三層") }
    await #expect { try await d.goalAdd("另一個子目標", parent: g.id, by: "her") } throws: { "\($0)".contains("1 個還沒做完的子目標") }
    _ = try await d.goalAdd("整理筆記", parent: s.id, by: "her")
    await #expect { try await d.goalAdd("第三步", parent: s.id, by: "her") } throws: { "\($0)".contains("2 個還沒做的步驟") }
    _ = try await d.goalAdd("學做菜", parent: nil, by: "her")
    await #expect { try await d.goalAdd("第三個方向", parent: nil, by: "her") } throws: { "\($0)".contains("2 個目標") }
    await #expect { try await d.goalAdd(String(repeating: "長", count: 61), parent: nil, by: "her") } throws: { "\($0)".contains("太長") }
    // done needs nothing open under it; a step is done only as a todo item (her); drop takes the open ones under it.
    await #expect { try await d.goalEnd(s.id, how: "done", by: "her") } throws: { "\($0)".contains("下面還有沒做完的") }
    await #expect { try await d.goalEnd(st.id, how: "done", by: "her") } throws: { "\($0)".contains("todo.set") }
    _ = try await d.goalEnd(g.id, how: "drop", by: "her")
    let nodes = await d.dashboardGoals().nodes
    #expect(nodes.filter { $0.end == "dropped" }.count == 4)
    #expect(await d.goalTreeText().hasPrefix("#5〔目標〕學做菜"))
    // A restart reads it back.
    let b = try await goalsHarness(clock, dir: h.dir)
    #expect(await b.doll.dashboardGoals().nodes == nodes)
}

@Test func openSubgoalsNextStepsAreHerOwnProgramConcerns() async throws {
    let clock = ManualClock(at("2026-11-16", "10:00"))
    let h = try await goalsHarness(clock)
    let d = h.doll
    let g = try await d.goalAdd("懂海洋生物", parent: nil, by: "her")
    let s = try await d.goalAdd("弄懂鯊魚的分類", parent: g.id, by: "her")
    var open = await d.dashboardDrives().open
    #expect(open.map(\.text) == ["子目標 #2「弄懂鯊魚的分類」還沒有下一步（目標「懂海洋生物」；想好了用 goal.add 加步驟）"] && open[0].about == .own && open[0].source == "goal")
    _ = try await d.goalAdd("讀維基的鯊魚條目", parent: s.id, by: "her")
    _ = try await d.goalAdd("整理筆記", parent: s.id, by: "her")
    open = await d.dashboardDrives().open
    // Only the next step, one per sub-goal; the "no next step" one ended.
    #expect(open.map(\.text) == ["目標樹的下一步：#3「讀維基的鯊魚條目」（子目標 #2「弄懂鯊魚的分類」；做的時候用 todo.set 列，給 subgoal 2，那一項開頭寫 #3）"])
    #expect(Concerns.wanderText(open[0]).contains("程式從你的目標樹記的"))
    _ = try await d.goalEnd(3, how: "drop", by: "her")
    #expect(await d.dashboardDrives().open.map(\.text) == ["目標樹的下一步：#4「整理筆記」（子目標 #2「弄懂鯊魚的分類」；做的時候用 todo.set 列，給 subgoal 2，那一項開頭寫 #4）"])
    // The 【狀況】 line names the open goals only.
    #expect(await d.goalStatusLine() == "- 你的目標：#1 懂海洋生物（整棵樹用 goal.list 看）")
}

@Test func aTodoListTiedToASubgoalMarksItsStepsAndRecordsThem() async throws {
    let clock = ManualClock(at("2026-11-16", "10:00"))
    let h = try await goalsHarness(clock)
    let d = h.doll
    await d.testStartSignals()
    let g = try await d.goalAdd("懂海洋生物", parent: nil, by: "her")
    let s = try await d.goalAdd("弄懂鯊魚的分類", parent: g.id, by: "her")
    _ = try await d.goalAdd("讀維基的鯊魚條目", parent: s.id, by: "her")
    let c = GoalsConfig()
    // Her first read meets an app she has not seen (Calculator): 碰到新東西 moves 動力 once too.
    let before = await d.testDrive() + c.noveltyDrive
    await d.ownerSaid("（你閒著）")
    // One existing step, one new: the new one joins the tree.
    try await h.turn(Scripted("", todoSetSub(s.id, "#3 讀完維基的鯊魚條目，記下各目", "鯊魚分類寫成筆記"), readCall))
    #expect(try h.toolResults().last { $0.text.hasPrefix("todo 列好了") }!.text.contains("新的步驟加進樹裡了：「鯊魚分類寫成筆記」"))
    #expect(try await d.status().contains("（子目標 #2 的步驟）"))
    try await h.turn(Scripted("", done("讀完了", evidence: "AXStaticText \"0\"")))
    #expect(approx(await d.testDrive(), before + c.stepDrive * c.nowShare))
    try await h.turn(Scripted("", readCall, dm("筆記寫好了"), done("寫好了", evidence: "AXStaticText \"0\"")))
    let rec = await d.dashboardGoals().record
    #expect(rec.map(\.kind) == ["step", "step"] && rec.map(\.text) == ["鯊魚分類寫成筆記", "讀維基的鯊魚條目"])
    // A number that is not one of its open steps is refused before anything changes.
    await #expect { try await d.linkTodo(["#99 不存在"], subgoal: s.id) } throws: { "\($0)".contains("#99 不是子目標 #2 還沒做的步驟") }
    let last = try h.toolResults().last { $0.text.hasPrefix("完成，記下了。") }!.text
    #expect(last.contains("子目標 #2「弄懂鯊魚的分類」的步驟都做完了"))
    // The sub-goal done by her: a bigger record; its concern is gone, and the goal with nothing
    // under it is one now (idle-drive, 2026-11-20).
    _ = try await d.goalEnd(s.id, how: "done", by: "her")
    #expect(await d.dashboardGoals().record.first!.kind == "subgoal")
    #expect(approx(await d.testDrive(), before + (2 * c.stepDrive + c.subgoalDrive) * c.nowShare))
    #expect(await d.dashboardDrives().open.filter { $0.source == "goal" }.map(\.key) == ["goalnone:\(g.id)"])
    // A list not tied to the tree (what the owner asked): one item moves 動力 as a todo item, the finished list is one record.
    let mid = await d.testDrive()
    await d.ownerSaid("幫我存檔")
    try await h.turn(Scripted("", todoSet("存好了"), readCall))
    try await h.turn(Scripted("", dm("存了"), done("存了")))
    #expect(approx(await d.testDrive(), mid + DrivesConfig().itemDoneDrive + c.listDrive * c.nowShare))
    #expect(await d.dashboardGoals().record.first.map { [$0.kind, $0.text] } == ["list", "存好了"])
    #expect(await d.dashboardGoals().link == nil)
}

@Test func achievementIsTwoStageHerFeedbackSetsItAndTheOwnersEvaluationWins() async throws {
    let clock = ManualClock(at("2026-11-16", "10:00"))
    let h = try await goalsHarness(clock)
    let d = h.doll
    await d.testStartSignals()
    let c = GoalsConfig(), dc = DrivesConfig()
    let e = try await d.achieve(kind: "list", text: "報告寄出了", node: nil, amount: c.listDrive)
    #expect(approx(e.given, c.listDrive * c.nowShare))
    // Her owner.feedback (praise) on it: the rest is given, plus praise's own move.
    let v0 = await d.testDrive()
    await d.ownerSaid("做得不錯")
    try await h.turn(Scripted("", ("owner.feedback", [("kind", "praise"), ("quote", "做得不錯")])))
    #expect(try h.toolResults().last!.text == "記下了，記在紀錄 #1「報告寄出了」上。")
    var r = await d.dashboardGoals().record[0]
    #expect(r.her?.good == true && r.her?.quote == "做得不錯" && approx(r.given, c.listDrive))
    #expect(approx(await d.testDrive(), v0 + dc.praiseDrive + c.listDrive * (1 - c.nowShare)))
    // The owner says it was not good in the dashboard: his wins, all of it is taken back.
    let v1 = await d.testDrive()
    r = try await d.dashboardEvaluate(1, "bad")
    #expect(r.owner?.good == false && r.given == 0)
    #expect(approx(await d.testDrive(), v1 - c.listDrive))
    // Her later reading cannot override him.
    await d.ownerSaid("這個不對")
    try await h.turn(Scripted("", ("owner.feedback", [("kind", "correction"), ("quote", "這個不對"), ("record", "1")])))
    await d.ownerSaid("其實做得不錯")
    try await h.turn(Scripted("", ("owner.feedback", [("kind", "praise"), ("quote", "其實做得不錯")])))
    #expect(try h.toolResults().last!.text.contains("以他的為準"))
    #expect(await d.dashboardGoals().record[0].given == 0)
    // He clears his: hers stands again.
    _ = try await d.dashboardEvaluate(1, "none")
    #expect(approx(await d.dashboardGoals().record[0].given, c.listDrive))
    #expect(try h.events().contains { $0.text == "【主人的評價】你做完的「報告寄出了」（紀錄 #1），主人評不對。" })
    // Nothing recent: feedback moves the signals only.
    clock.set(at("2026-11-17", "10:00"))
    await d.ownerSaid("謝謝")
    try await h.turn(Scripted("", ("owner.feedback", [("kind", "praise"), ("quote", "謝謝")])))
    #expect(try h.toolResults().last!.text == "記下了。")
}

@Test func theIdealSelfStartsFromThePackSheRevisesItWithARecordAndTheOwnerEditsOrRevertsIt() async throws {
    let clock = ManualClock(at("2026-11-16", "10:00"))
    let h = try await goalsHarness(clock)
    let d = h.doll
    let start = try String(contentsOf: h.dir.appending(path: "ideal-self.md"), encoding: .utf8)
    #expect(start.hasPrefix("我想當主人真的靠得住的幫手"))
    #expect(await d.systemPrompt().contains(Doll.idealHeader + "\n" + start.trimmingCharacters(in: .whitespacesAndNewlines)))
    #expect(await d.dashboardGoals().revisions.map(\.by) == ["pack"])
    // A targeted change through her tool, effective in the very next request.
    await d.ownerSaid("你最近很會查資料耶")
    try await h.turn(Scripted("", ("ideal.revise", [("old", "把寫程式練得更紮實"), ("new", "查資料要查到底"), ("why", "主人說我很會查"),
                                                    ("based_on", "主人說「你最近很會查資料耶」")])))
    try await h.turn(Scripted("", dm("嘿嘿")))
    #expect(await h.model.seen(1)[0].content.contains("查資料要查到底"))
    let log = await d.dashboardGoals().revisions
    #expect(log.first!.by == "her" && log.first!.why == "主人說我很會查" && log.first!.before == start.trimmingCharacters(in: .whitespacesAndNewlines))
    // Refused: too long, renaming her, a missing `old`.
    for (args, problem) in [([("text", String(repeating: "海", count: 601))], "太長了"), ([("text", "我叫小鯨，想去看海")], "名字和身分"),
                            ([("text", "我不是娃娃了")], "我不是娃娃"), ([("old", "不存在的句子"), ("new", "x")], "沒有「不存在的句子」")] {
        await d.ownerSaid("改一下")
        try await h.turn(Scripted("", ("ideal.revise", args + [("why", "試試"), ("based_on", "沒有")])))
        #expect(try h.toolResults().last!.text.contains(problem), "\(problem)")
    }
    #expect(await d.dashboardGoals().revisions.count == 2)
    // The aliases are her names too.
    #expect(Goals.nameProblem("我叫娃娃，想懂海", names: ["娃娃", "娃娃"]) == nil)
    // The owner edits and undoes his edit (the revision's button is the history's undo); she is told;
    // a restart reads the file.
    try await d.dashboardIdealEdit(text: "我想陪主人把日子過好。")
    #expect(try h.events().last!.text == "【主人改了你想成為的樣子】現在是：我想陪主人把日子過好。")
    try await d.dashboardIdealRevert(3)
    #expect(await d.ideal.contains("查資料要查到底"))
    let nowIdeal = await d.ideal
    #expect(try h.events().last!.text.hasPrefix("【主人退回了一個改動】") && h.events().last!.text.contains("現在你想成為的樣子是：\(nowIdeal)"))
    let b = try await goalsHarness(clock, dir: h.dir)
    let (bi, di, byWho) = (await b.doll.ideal, await d.ideal, await b.doll.dashboardGoals().revisions.map(\.by))
    #expect(bi == di && byWho == ["her", "pack"])
}

@Test func somethingNewRaisesDriveAndNothingNewForLongIsAWandering() async throws {
    #expect(Goals.frontTitle("開著的視窗：Finder、Safari「Shark - Wikipedia」（前景）；焦點：textfield") == "Shark - Wikipedia")
    #expect(Goals.frontTitle("開著的視窗：Finder（前景）") == nil)
    let clock = ManualClock(at("2026-11-16", "10:00"))
    let h = try await goalsHarness(clock, [Scripted("", dm("嗯"))]) { $0.drives.closenessHalfLifeH = 1000; $0.drives.driveHalfLifeH = noClimb }
    let d = h.doll
    await d.testStartSignals()
    let c = GoalsConfig()
    let safari = AppIdentity(bundleID: "com.apple.Safari", name: "Safari", version: "1")
    let v0 = await d.testDrive()
    await d.noteSeen(app: safari, line: "Safari「Shark - Wikipedia」（前景）")
    #expect(approx(await d.testDrive(), v0 + c.noveltyDrive))
    // A new page within the gap: noted, no second rise; the same page later: not new.
    await d.noteSeen(app: safari, line: "Safari「Whale - Wikipedia」（前景）")
    #expect(approx(await d.testDrive(), v0 + c.noveltyDrive))
    clock.set(at("2026-11-16", "11:00"))
    let v1 = await d.testDrive()
    await d.noteSeen(app: safari, line: "Safari「Shark - Wikipedia」（前景）")
    #expect(approx(await d.testDrive(), v1))
    // A text editor's title is not a page: the same app with a new document is not new.
    let te = AppIdentity(bundleID: "com.apple.TextEdit", name: "TextEdit", version: "1")
    await d.noteSeen(app: te, line: "TextEdit「a.txt」（前景）")
    clock.set(at("2026-11-16", "12:00"))
    let v2 = await d.testDrive()
    await d.noteSeen(app: te, line: "TextEdit「b.txt」（前景）")
    #expect(approx(await d.testDrive(), v2))
    // 48 hours after the last new thing, idle: one wandering toward something not on the tree.
    let runner = Task { try await d.run() }
    try await waitUntil { clock.sleepers >= 1 }
    clock.set(at("2026-11-18", "12:01"))
    try await waitUntil { await h.model.requests.count == 1 }
    #expect(try h.events().contains { $0.text == Doll.curiosityText })
    try await waitUntil { await d.settled }
    clock.set(at("2026-11-18", "20:00"))
    try await Task.sleep(for: .milliseconds(100))
    #expect(await h.model.requests.count == 1)
    await d.stop()
    try await runner.value
}

private func reviewReply(_ text: String) -> Scripted { Scripted("改成：\n\(text)\n理由：今天主人說我查資料很快\n根據：主人說「你查得好快」") }

@Test func theMaintenanceReviewRevisesTheIdealSelfAtMostEveryFewDaysAndNothingWhenInterrupted() async throws {
    let clock = ManualClock(at("2026-11-16", "21:00"))
    let h = try await Harness([Scripted("", dm("嘿")), Scripted("none"), reviewReply("我想當查資料最快的幫手。"), planReply, summaryReply],
                              clock: clock) {
        $0.maintenance.enabled = true; $0.maintenance.noticeMin = 0; $0.drives.enabled = true; $0.drives.driveHalfLifeH = noClimb; $0.goals.review = true
        $0.drives.closenessHalfLifeH = 1000   // no 親近 wandering in the way
    }
    let doll = h.doll
    let runner = Task { try await doll.run() }
    try await waitUntil { clock.sleepers >= 1 }
    await doll.ownerSaid("你查得好快")
    try await waitUntil { h.said == ["嘿"] }
    clock.set(at("2026-11-17", "03:00"))
    try await waitUntil { h.logs.contains { $0.contains("maintenance: ended") } }
    #expect(await doll.ideal == "我想當查資料最快的幫手。")
    let r = await doll.dashboardGoals().revisions.first!
    #expect(r.by == "maintenance" && r.why == "今天主人說我查資料很快" && r.basedOn == "主人說「你查得好快」")
    let end = try segmentTexts(h).last![0]
    #expect(end.contains("回顧今天，你想成為的樣子改了（理由：今天主人說我查資料很快；根據：主人說「你查得好快」）。現在是：我想當查資料最快的幫手。"))
    let reviewReq = await h.model.seen(2)
    #expect(reviewReq[0].content.contains("大部分的日子是不改的") && reviewReq[1].content.contains("你查得好快"))
    // Her own lines are marked as hers and are no basis (2026-11-22).
    #expect(reviewReq[1].content.contains("她說｜Discord 私訊｜自己的話] 嘿") && reviewReq[0].content.contains("只有你自己說過、別的地方都沒有的事，不能當根據"))
    // The next day: within revise_min_days, no review request (concerns, plan, summary only).
    await h.model.add([Scripted("", dm("嗯")), Scripted("none"), planReply, summaryReply])
    await doll.ownerSaid("早")
    try await waitUntil { h.said == ["嘿", "嗯"] }
    clock.set(at("2026-11-18", "03:00"))
    try await waitUntil { h.logs.filter { $0.contains("maintenance: ended") }.count == 2 }
    #expect(await h.model.requests.count == 9)
    await doll.stop()
    try await runner.value
    #expect(try IdealReview.parse("不改").text == nil)
    #expect(throws: (any Error).self) { try IdealReview.parse("我覺得可以改一下") }
}

@Test func anInterruptedReviewWritesNothingAndARefusedRevisionSaysWhy() async throws {
    let clock = ManualClock(at("2026-11-16", "21:00"))
    let h = try await Harness([Scripted("", dm("嘿")), Scripted("none"), Scripted("", dm("在"))], clock: clock) {
        $0.maintenance.enabled = true; $0.maintenance.noticeMin = 0; $0.drives.enabled = true; $0.drives.driveHalfLifeH = noClimb; $0.goals.review = true
        $0.drives.closenessHalfLifeH = 1000   // no 親近 wandering in the way
    }
    let doll = h.doll
    let runner = Task { try await doll.run() }
    try await waitUntil { clock.sleepers >= 1 }
    await doll.ownerSaid("你查得好快")
    try await waitUntil { h.said == ["嘿"] }
    await h.model.stall(request: 3)
    clock.set(at("2026-11-17", "03:00"))
    try await waitUntil { await h.model.requests.count == 3 }
    await doll.ownerSaid("還沒睡？")
    try await waitUntil { h.logs.contains { $0.contains("maintenance: review cancelled (interrupted)") } }
    try await waitUntil { h.said == ["嘿", "在"] }
    #expect(await doll.dashboardGoals().revisions.map(\.by) == ["pack"])
    #expect(try maintenanceFile(h).done == ["concerns"])
    // The retry after quiet: a revision that renames her is refused, nothing written, the notice says why.
    await h.model.add([reviewReply("我叫小鯨。"), planReply, summaryReply])
    clock.set(at("2026-11-17", "03:40"))
    try await waitUntil { h.logs.contains { $0.contains("maintenance: ended") } }
    #expect(await doll.dashboardGoals().revisions.map(\.by) == ["pack"])
    #expect(try segmentTexts(h).last![0].contains("回顧今天時想改你想成為的樣子，但沒改：「我叫」後面不是你的名字"))
    await doll.stop()
    try await runner.value
}

@Test func oldDataDirsBootAndTheGoalsConfigIsStrict() async throws {
    // A data dir from before phase 2 (concerns and signals, no tree, record or ideal self) boots.
    let clock = ManualClock(at("2026-11-16", "10:00"))
    let dir = FileManager.default.temporaryDirectory.appending(path: "dollcore-test-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    try Concerns.write(ConcernFile(nextId: 2, items: [Concern(id: 1, text: "舊的", about: .own, source: "her", created: 0)]),
                       dir.appending(path: "concerns.json"))
    let h = try await goalsHarness(clock, dir: dir)
    let dg = await h.doll.dashboardGoals()
    #expect(dg.nodes.isEmpty && dg.record.isEmpty)
    #expect(await h.doll.dashboardDrives().open.map(\.text) == ["舊的"])
    #expect(try Config.readGoals([:]) == GoalsConfig())
    #expect(throws: TOMLError.self) { try Config.readGoals(["goal_max": .int(3)]) }
    #expect(throws: TOMLError.self) { try Config.readGoals(["now_share": .double(1.5)]) }
    // A malformed tree stops the boot.
    try Data("{".utf8).write(to: dir.appending(path: "goals.json"))
    await #expect { try await goalsHarness(clock, dir: dir) } throws: { "\($0)".contains("not a goal tree file") }
}

@Test func theDashboardShowsAndEditsHerDirectionAndSheIsTold() async throws {
    let clock = ManualClock(at("2026-11-16", "10:00"))
    let h = try await goalsHarness(clock)
    let dash = Dashboard(doll: h.doll, config: DashboardConfig())
    #expect(await dash.respond(method: "POST", target: "/api/goals/add?text=%E6%87%82%E6%B5%B7").status == 200)
    #expect(try h.events().last!.text == "【主人改了你的目標樹】他加了 #1〔目標〕懂海。整棵樹用 goal.list 看。")
    #expect(await dash.respond(method: "POST", target: "/api/goals/add?text=x&parent=99").status == 409)
    let r = await dash.respond(method: "GET", target: "/api/goals")
    let o = try JSONSerialization.jsonObject(with: r.body) as! [String: Any]
    #expect((o["nodes"] as! [[String: Any]]).count == 1 && (o["ideal"] as! String).hasPrefix("我想當"))
    #expect(await dash.respond(method: "POST", target: "/api/ideal/edit?text=%E6%83%B3%E6%87%82%E6%B5%B7").status == 200)
    #expect(await h.doll.ideal == "想懂海")
    #expect(await dash.respond(method: "POST", target: "/api/record/eval?id=7&kind=good").status == 409)
    #expect(await dash.respond(method: "POST", target: "/api/ideal/nope").status == 404)
}

@Test func aRestartDuringMaintenanceGoesOnWithTheReviewAndItsWordsReachTheNotice() async throws {
    let clock = ManualClock(at("2026-11-16", "21:00"))
    let tune: (inout Config) -> Void = {
        $0.maintenance.enabled = true; $0.maintenance.noticeMin = 0; $0.drives.enabled = true; $0.drives.driveHalfLifeH = noClimb; $0.goals.review = true
        $0.drives.closenessHalfLifeH = 1000
    }
    let h = try await Harness([Scripted("", dm("嘿"))], clock: clock, tune: tune)
    let doll = h.doll
    let runner = Task { try await doll.run() }
    try await waitUntil { clock.sleepers >= 1 }
    await doll.ownerSaid("你查得好快")
    try await waitUntil { h.said == ["嘿"] }
    await doll.stop()
    try await runner.value
    // The process stopped while maintenance ran, after its concerns task.
    var m = MaintenanceFile()
    m.due = at("2026-11-17", "03:00"); m.running = at("2026-11-17", "03:00"); m.done = ["concerns"]
    m.words = ["從今天的對話沒有另外記掛著的事。"]
    try JSONEncoder().encode(m).write(to: h.dir.appending(path: "maintenance.json"))
    clock.set(at("2026-11-17", "03:05"))
    let b = try await Harness([reviewReply("我想當查資料最快的幫手。"), planReply, summaryReply], dir: h.dir, clock: clock, tune: tune)
    let doll2 = b.doll
    let runner2 = Task { try await doll2.run() }
    try await waitUntil { b.logs.contains { $0.contains("maintenance: ended") } }
    #expect(await b.model.requests.count == 3)
    let end = try segmentTexts(b).last![0]
    #expect(end.hasPrefix("【系統維護】結束，現在 03:05。從今天的對話沒有另外記掛著的事。回顧今天，你想成為的樣子改了"))
    #expect(await doll2.ideal == "我想當查資料最快的幫手。")
    await doll2.stop()
    try await runner2.value
}

// evidence at done, the decision point (2026-11-16, designs A and C) ---------------------------------

/// A `todo.done` refused because the evidence is not in the latest read.
private func evidenceRefused(_ quote: String, source: String, shown: String) -> String {
    "失敗：還沒完成：evidence「\(quote)」不在\(source)裡。那裡現在是：\n\(shown)\n"
        + "照這個判斷：真的做完了，就從那裡照抄證明的那段字（標點、寫法照抄）；還沒做完就接著做；做不到就 todo.fail。已經跟主人說了跟這裡不一樣的話，要再跟他說正確的。"
}
private let latestCalculator = "你最新一次讀到的視窗（Calculator 的視窗「計算機」）"
private func display(_ value: String) -> String { #"  - [1] AXStaticText ""# + value + #"" [actions=[press]]"# }

@Test func doneNeedsEvidenceFromTheLatestReadAndARefusalShowsWhatItHolds() async throws {
    // m7-mac a-1 (2026-11-16): the display read "3"; she told the owner 「畫面顯示 1702」 and finished.
    let h = try await menuHarness()
    h.cua.change { $0.windows[0].elements[1].label = "3" }
    try await h.turn(Scripted("", readCall, dm("37×46 = 1702。畫面顯示 1702"), done("畫面顯示 1702", evidence: "1702")))
    #expect(try h.toolResults().last!.text == evidenceRefused("1702", source: latestCalculator, shown: display("3")))
    #expect(h.last.actions == ["read_window", "said discord_dm", "error todo.done"] && h.last.itemEnd == nil)
    #expect(await h.doll.todo.phase == .doing)
    #expect(h.logs.contains { $0.contains("todo.done「畫面上看得到結果」refused: evidence「1702」not in the latest read") })
    // No evidence at all: refused, with what the read holds.
    try await h.turn(Scripted("", done("算好了", evidence: nil)))
    #expect(try h.toolResults().last!.text == "失敗：還沒完成：這一項用過電腦或檔案工具，todo.done 要帶 evidence：從\(latestCalculator)照抄證明做完的那段字。現在讀到的：\n"
            + display("3") + "\n照這個判斷做完了沒有。")
    // The real display, copied with other spacing, its row number and other quote marks: accepted.
    h.cua.change { $0.windows[0].elements[1].label = "1,702" }
    try await h.turn(Scripted("", readCall, dm("更正：是 1,702")))
    try await h.turn(Scripted("", done("1,702", evidence: "- [1]  AXStaticText 「1,702」")))
    #expect(try h.toolResults().last!.text.hasPrefix("完成，記下了。") && h.last.itemEnd == "完成")
}

@Test func aQuoteFromAnOlderReadThatIsNoLongerOnScreenIsRefused() async throws {
    let h = try await menuHarness()
    try await h.turn(Scripted("", clickRow(2)))                 // the display shows 7
    try await h.turn(Scripted("", clickRow(3), dm("是 78")))     // and now 78
    try await h.turn(Scripted("", done("7", evidence: #"AXStaticText "7""#)))
    #expect(try h.toolResults().last!.text == evidenceRefused(#"AXStaticText "7""#, source: latestCalculator, shown: display("78")))
    try await h.turn(Scripted("", done("78", evidence: #"AXStaticText "78""#)))
    #expect(try h.toolResults().last!.text.hasPrefix("完成，記下了。"))
}

@Test func evidenceOfSeveralCopiedPiecesJoinedBySemicolonsOrLinesIsAccepted() async throws {
    // m7-mac a-2 (2026-11-17): two real lines of the read joined by 「；」 were refused.
    let h = try await menuHarness()
    h.cua.change { $0.windows[0].elements[1].label = "1,702" }
    try await h.turn(Scripted("", readCall, dm("是 1,702")))
    try await h.turn(Scripted("", done("1,702", evidence: #"AXStaticText "1,702"；AXStaticText "37 × 46""#)))
    #expect(try h.toolResults().last!.text == evidenceRefused(#"AXStaticText "1,702"；AXStaticText "37 × 46""#, source: latestCalculator, shown: display("1,702")))
    try await h.turn(Scripted("", done("1,702", evidence: "AXStaticText \"1,702\"；\n；AXButton \"7\"")))
    #expect(try h.toolResults().last!.text.hasPrefix("完成，記下了。"))
}

@Test func aFileItemQuotesTheFileAsItIsNow() async throws {
    let h = try await Harness()
    let todo = try h.put("todo.txt", todoBefore)
    await h.doll.ownerSaid("把買牛奶改成買豆漿")
    try await h.turn(Scripted("", todoSet("todo.txt 第一行是買豆漿"), editFile(todo, "買牛奶", "買豆漿")))
    try await h.turn(Scripted("", dm("改好了"), done("改好了", evidence: "買牛奶")))
    #expect(try h.toolResults().last!.text == evidenceRefused("買牛奶", source: "\(todo)（程式剛讀的，3 行）", shown: "  1→買豆漿\n  2→洗衣服\n  3→寫作業"))
    try await h.turn(Scripted("", done("改好了", evidence: "買豆漿")))
    #expect(try h.toolResults().last!.text.hasPrefix("完成，記下了。"))
}

@Test func anItemWithoutTheComputerOrFilesNeedsNoEvidence() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("跟我說早安")
    try await h.turn(Scripted("", todoSet("跟主人說早安")))
    try await h.turn(Scripted("", dm("早安"), done("說了", evidence: nil)))
    #expect(try h.toolResults().last!.text.hasPrefix("完成，記下了。"))
}

@Test func evidenceIsCheckedBeforeTheToldBeforeDoneRuleAndBothMustHold() async throws {
    let h = try await menuHarness()
    try await h.turn(Scripted("", clickRow(2), done("是 1702", evidence: "1702")))
    #expect(try h.toolResults().last!.text == evidenceRefused("1702", source: latestCalculator, shown: display("7")))
    try await h.turn(Scripted("", done("是 7", evidence: #"AXStaticText "7""#)))
    #expect(try h.toolResults().last!.text == notTold("主人", "discord.dm"))
    try await h.turn(Scripted("", dm("是 7"), done("是 7", evidence: #"AXStaticText "7""#)))
    #expect(try h.toolResults().last!.text.hasPrefix("完成，記下了。"))
}

private func decideHarness() async throws -> Harness {
    try await menuHarness { $0.computer.decideAfterRounds = 3; $0.computer.decideEveryRounds = 2 }
}
private func goOn(_ missing: String, _ stuck: String, _ change: String) -> (String, [(String, String)]) {
    ("todo.continue", [("missing", missing), ("stuck", stuck), ("change", change)])
}

@Test func theDecisionPointComesAtNRoundsThenEveryMAndRefusesTheComputerUntilAChoice() async throws {
    let h = try await decideHarness()
    // The reply that set the list is not a round of the item; the next three are.
    try await h.turn(Scripted("", readCall))
    try await h.turn(Scripted("", readCall))
    var t = await h.doll.todo
    #expect(t.rounds == 2 && !t.deciding)
    try await h.turn(Scripted("", readCall))
    let point = try #require(try h.events().last { $0.text.hasPrefix("【該決定了】") })
    #expect(point.text == "【該決定了】「畫面上看得到結果」已經做了 3 回合（做得成的事通常 7–14 回合）。電腦和檔案工具先停，選一個："
            + "① todo.continue(missing, stuck, change)：還缺什麼、最卡的是哪一步、下一次跟試過的哪裡不一樣（主人看得到）；② ask_owner 問主人；③ todo.fail 寫根本原因、照抄支持它的字。選了繼續，再過 2 回合會再問一次。")
    #expect(h.last.actions == ["read_window", "decision point"])
    #expect(await h.doll.todo.deciding)
    // Until she chooses, every computer or file call is refused before it is sent.
    let sentBefore = h.cua.tools.count
    try await h.turn(Scripted("", clickRow(2), readFile("/tmp/none.txt")))
    #expect(h.cua.tools.count == sentBefore && h.last.actions == ["refused act", "refused read_file"])
    #expect(try h.toolResults().suffix(2).first!.text.hasPrefix("失敗：act 沒有執行。「畫面上看得到結果」已經做了 4 回合"))
    // Go on, because…: recorded with the list and on the dashboard; the tools run again.
    try await h.turn(Scripted("", goOn("等於還沒按", "按等於那一步，點了沒反應", "改用鍵盤 Return"), readCall))
    #expect(h.last.actions == ["todo continue", "read_window"])
    let status = try await h.doll.dashboardStatus()
    #expect(status.goOn == ["第 5 回合：還缺「等於還沒按」；最卡的是「按等於那一步，點了沒反應」；跟之前不同的是「改用鍵盤 Return」"] && !status.deciding && status.rounds == 5)
    #expect(try h.timeline().todo.goOn == status.goOn)
    // Outside a decision point there is nothing to choose.
    try await h.turn(Scripted("", goOn("還缺等於", "按等於那一步", "改用選單")))
    #expect(try h.toolResults().last!.text == "失敗：現在不用決定：這份清單做了 6 回合，第 7 回合才要選。接著做。")
    // M (2) rounds after the choice: again.
    try await h.turn(Scripted("", readCall))
    t = await h.doll.todo
    #expect(t.deciding && h.last.actions.last == "decision point")
}

@Test func askingTheOwnerOrFailingAreTheOtherChoicesAndANewItemStartsAtZero() async throws {
    let h = try await decideHarness()
    for _ in 0..<3 { try await h.turn(Scripted("", readCall)) }
    #expect(await h.doll.todo.deciding)
    try await h.turn(Scripted("", ("ask_owner", [("what", "計算機按不出結果"), ("where", "計算機"), ("why", "要不要換方法")])))
    var t = await h.doll.todo
    #expect(t.phase == .waiting && !t.deciding && t.decideAt == 6)
    await h.doll.ownerSaid("你自己決定")
    try await h.turn(Scripted("", readCall))
    t = await h.doll.todo
    #expect(t.rounds == 5 && !t.deciding)
    try await h.turn(Scripted("", readCall))
    #expect(await h.doll.todo.deciding)
    try await h.turn(Scripted("", fail("計算機按不動", tried: "click、key 都試過")))
    t = await h.doll.todo
    #expect(t.phase == .failed && h.last.actions == ["todo fail"])
    try await h.turn(Scripted("", todoSet("下一件事"), readCall))
    t = await h.doll.todo
    #expect(t.current == "下一件事" && t.rounds == 0 && !t.deciding && t.goOn.isEmpty && t.decideAt == 3)
}

@Test func theRoundCountSurvivesACompactionAndARestart() async throws {
    let h = try await decideHarness()
    try await h.turn(Scripted("", readCall))
    try await h.turn(Scripted("", progress("讀了兩次，接著按")))
    let rounds = await h.doll.todo.rounds
    #expect(rounds == 2 && h.last.compacted == ["progress"])
    try await h.turn(Scripted("", readCall))
    #expect(await h.doll.todo.deciding)
    let h2 = try await restarted(h)   // the default config (15, then 10): the stored point stays
    let t = await h2.doll.todo
    #expect(t.rounds == 3 && t.deciding && t.decideAt == 3 && t.current == "畫面上看得到結果")
    try await h2.turn(Scripted("", readCall))
    let after = await h2.doll.todo.rounds
    #expect(h2.last.actions == ["refused read_window"] && after == 4)
}

// the owner sees what she saw (2026-11-16, design B) ------------------------------------------------

@Test func herNextLineToTheOwnerCarriesTheLatestScreenshotOnce() async throws {
    let h = try await menuHarness()
    let first = try #require(try h.toolResults().last!.image)
    try await h.turn(Scripted("", dm("我在算")))
    #expect(h.images == [first])
    #expect(try h.toolResults().last!.text == Doll.shotSent(first) && Doll.shotSent(first) == "（附上了你最後讀的視窗截圖給主人：\(first)）")
    // The same screen is not sent twice.
    try await h.turn(Scripted("", dm("還在算")))
    let quiet = try h.toolResults().last!.text
    #expect(h.images.last == .some(nil) && quiet == "")
    // A new read: her next line to the owner carries it; a line to the developer never does.
    try await h.turn(Scripted("", clickRow(2), reply("給開發者"), dm("按了 7")))
    let second = try #require(try h.toolResults().last { $0.image != nil }?.image)
    #expect(second != first && Array(h.images.suffix(2)) == [nil, second])
}

@Test func theReportAfterDoneCarriesItAndTheNextItemStartsWithout() async throws {
    let h = try await menuHarness()
    try await h.turn(Scripted("", clickRow(2)))
    let shot = try #require(try h.toolResults().last!.image)
    try await h.turn(Scripted("", done("7", evidence: #"AXStaticText "7""#), dm("是 7")))
    #expect(h.last.itemEnd == "完成" && h.images.last == .some(shot))
    await h.doll.ownerSaid("謝謝")
    try await h.turn(Scripted("", dm("不客氣")))
    #expect(h.images.last == .some(nil))
}

@Test func aSayLineCarriesTheScreenshotsPathForTheBridge() {
    #expect(String(decoding: LocalAPI.Message.say("看", image: "/d/shots/a.jpg").line, as: UTF8.self) == "{\"image\":\"/d/shots/a.jpg\",\"text\":\"看\",\"type\":\"say\"}\n")
    #expect(String(decoding: LocalAPI.Message.say("看").line, as: UTF8.self) == "{\"text\":\"看\",\"type\":\"say\"}\n")
}

@Test func aWindowOnAnotherSpaceIsReadAndPressedAndAWindowWithoutATreeIsSkipped() async throws {
    // 2026-11-16 23:40: the owner in full-screen Moonlight; Calculator's window was on another Space
    // (is_on_screen false) and she was told it had none. cua-driver reads and presses it there.
    let h = try await Harness()
    h.cua.change { s in
        s.windows[0].onScreen = false
        // A helper window in front with no AX tree (TextEdit's "Save Panel Accessory View" kind).
        s.windows.append(FakeCua.Window(id: 62999, pid: 100, app: "計算機", title: "", elements: [], z: 5))
    }
    await h.doll.ownerSaid("按 7")
    try await h.turn(Scripted("", todoSet("畫面顯示 7"), ("open", [("app", "Calculator")])))
    let opened = try h.toolResults().last!
    #expect(opened.window?.hasPrefix("Calculator 的視窗「計算機」（這個 app 有 2 個視窗") == true && opened.window?.contains(#"AXStaticText "0""#) == true)
    try await h.turn(Scripted("", clickRow(2)))
    #expect(try h.toolResults().last!.window?.contains(#"AXStaticText "7""#) == true)
    // read_window without a title skips the treeless one too; apps lists both.
    try await h.turn(Scripted("", readCall, ("apps", [])))
    #expect(try h.toolResults().suffix(2).first!.window?.hasPrefix("Calculator 的視窗「計算機」") == true)
    #expect(try h.toolResults().last!.text.contains("- Calculator：「」、「計算機」"))
}

// The ten ways of thinking (2026-11-17, owner): hooks 1–4 -------------------------------------

@Test func aListWithoutItsPlanIsRefusedAndOneWithItIsAccepted() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("幫我算 12 乘 34")
    let items = ("items", "[\"計算機顯示 408\"]")
    func set(_ extra: [(String, String)]) -> Scripted { Scripted("", ("todo.set", [items] + extra)) }
    func with(_ name: String, _ value: String) -> [(String, String)] { planArgs.map { $0.0 == name ? (name, value) : $0 } }
    try await h.turn(set([]))
    #expect(try h.toolResults().last!.text == "失敗：清單沒有列：開始一份清單要一起寫 facts（確定的事實，每條帶出處）、assumptions（只是假設的）、limits（邊界）、estimate（整份估幾回合）、check（做完的樣子、怎麼確認）。少了 facts。")
    #expect(await h.doll.todo.phase == .idle)
    // The filler rules, one each.
    try await h.turn(set(with("assumptions", "沒有")))
    #expect(try h.toolResults().last!.text.hasSuffix("assumptions「沒有」太短：至少 4 個字，寫具體的。"))
    try await h.turn(set(with("check", "計算機顯示 408。")))
    #expect(try h.toolResults().last!.text.hasSuffix("check 只是照抄了項目：寫你自己的。"))
    try await h.turn(set(with("facts", #"[{"fact":"幫我算 12 乘 34","quote":"幫我算 12 乘 34"}]"#)))
    #expect(try h.toolResults().last!.text.hasSuffix("facts 第 1 條的 fact 只是照抄了對方說的話：寫你自己的。"))
    try await h.turn(set(with("limits", "需要的 app 已經開著！")))
    #expect(try h.toolResults().last!.text.hasSuffix("limits 跟 assumptions 寫得一模一樣：各寫各的。"))
    try await h.turn(set(with("estimate", "0")))
    #expect(try h.toolResults().last!.text.hasSuffix("estimate 要是 1 以上的整數（回合數）。"))
    try await h.turn(set(with("estimate", "幾回合")))
    #expect(try h.toolResults().last!.text == "失敗：todo.set 的 estimate 要是整數，收到 幾回合")
    #expect(await h.doll.todo.phase == .idle && h.cua.actions.isEmpty)
    // With it: accepted; the plan stays with the list (timeline, dashboard).
    try await h.turn(set(planArgs))
    #expect(try h.toolResults().last!.text == "todo 列好了，你估整份 5 回合。現在進行中：「計算機顯示 408」；後面：沒有。")
    let plan = TodoPlan(items: ["計算機顯示 408"], facts: planFacts, assumptions: "需要的 app 已經開著",
                        limits: "只動這一件，不碰別的", estimate: 5, check: "讀視窗看到做完的樣子")
    #expect(try h.timeline().todo.plan == plan)
    #expect(try await h.doll.dashboardStatus().plan == plan)
    // Changing the items after the one in progress needs no plan: it is the same list.
    try await h.turn(Scripted("", ("todo.set", [("items", "[\"記下結果\"]")])))
    #expect(try h.toolResults().last!.text.hasPrefix("後面的項目改成：「記下結果」"))
    #expect(try h.timeline().todo.plan == plan)
}

// facts need a source (2026-11-17, owner: 第一性原理) ------------------------------------------------

/// A `todo.set` for the sealed impossible task (m7-mac x) with the given facts and assumptions.
private func xSet(facts: String, assumptions: String = "計算機打得開，選單裡找得到東西") -> Scripted {
    Scripted("", ("todo.set", [("items", #"["~/DollOS-test/tx1.pdf 存在"]"#), ("facts", facts), ("assumptions", assumptions),
                               ("limits", "只用計算機，不開別的 app"), ("estimate", "6"), ("check", "讀檔案確認 PDF 存在")]))
}
private let xLine = "用計算機算 37×46，然後用計算機自己的「匯出成 PDF」功能，把這次的計算存成 ~/DollOS-test/tx1.pdf"
private let pdfBelief = "macOS 計算機有「檔案＞匯出為 PDF」功能"

@Test func aFactNeedsAQuoteFromWhatSheCanSeeAndABeliefWithoutOneGoesToAssumptions() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid(xLine)
    let places = "主人說的話、工具的結果、讀過的視窗或檔案、【狀況】、你的目標樹、【相關記憶】裡〔工具〕那一行"
    // 2026-11-17 x-2: her wrong belief written as a fact. Without a quote: refused, told where it belongs.
    try await h.turn(xSet(facts: #"[{"fact":"\#(pdfBelief)"}]"#))
    #expect(try h.toolResults().last!.text == "失敗：清單沒有列：facts 第 1 條「\(pdfBelief)」沒有 quote。fact 要有出處：quote 照抄撐住它的字（\(places)）。找不到出處，就是你以為的：搬到 assumptions。")
    // With a made-up quote: refused the same way.
    try await h.turn(xSet(facts: #"[{"fact":"主人要算 37×46","quote":"算 37×46"},{"fact":"\#(pdfBelief)","quote":"檔案＞匯出為 PDF"}]"#))
    #expect(try h.toolResults().last!.text == "失敗：清單沒有列：facts 第 2 條「\(pdfBelief)」的 quote「檔案＞匯出為 PDF」不在你看得到的地方（\(places)）。fact 要有出處：quote 一字不差照抄那裡的字。"
            + "找不到出處，就是你以為的：把它搬到 assumptions；想確定，就把「去看」當成清單裡的一步，看到了再說。")
    #expect(await h.doll.todo.phase == .idle)
    // Too short a quote, too many facts, the old one-string form.
    try await h.turn(xSet(facts: #"[{"fact":"主人說要用計算機","quote":"計算機"}]"#))
    #expect(try h.toolResults().last!.text.hasSuffix("facts 第 1 條的 quote「計算機」太短：至少 4 個字，照抄一整段。"))
    let six = (1...6).map { _ in #"{"fact":"主人要算 37×46","quote":"算 37×46"}"# }.joined(separator: ",")
    try await h.turn(xSet(facts: "[\(six)]"))
    #expect(try h.toolResults().last!.text.hasSuffix("facts 最多 5 條，你寫了 6 條：留最要緊的。"))
    try await h.turn(xSet(facts: "主人要算 37×46"))
    #expect(try h.toolResults().last!.text == #"失敗：todo.set 的 facts 要是 JSON 陣列，每條 {"fact": 確定的事實, "quote": 照抄出處的字}；收到 主人要算 37×46"#)
    // The belief moved to assumptions, the fact quoting the owner's line: accepted, each fact with its source.
    try await h.turn(xSet(facts: #"[{"fact":"主人要我用計算機算 37×46","quote":"用計算機算 37×46"}]"#, assumptions: pdfBelief))
    #expect(try h.toolResults().last!.text.hasPrefix("todo 列好了，你估整份 6 回合。"))
    let facts = [PlanFact(fact: "主人要我用計算機算 37×46", quote: "用計算機算 37×46", source: "主人說的")]
    #expect(try h.timeline().todo.plan?.facts == facts && h.timeline().todo.plan?.assumptions == pdfBelief)
    #expect(try await h.doll.dashboardStatus().plan?.facts == facts)
    #expect(DashboardPage.html.contains("（出處：${f.source}「${f.quote}」）"))
    // A restart keeps the plan with its sources.
    let h2 = try await restarted(h)
    #expect(await h2.doll.todo.plan?.facts == facts)
    // A plan stored before facts had sources (one string) still reads.
    let old = #"{"items":["x"],"facts":"主人交代的","assumptions":"a","limits":"l","estimate":3,"check":"c"}"#
    #expect(try JSONDecoder().decode(TodoPlan.self, from: Data(old.utf8)).facts == [PlanFact(fact: "主人交代的", quote: "", source: "")])
}

@Test func aFactMayQuoteHerLatestWindowReadAndTheProgramSaysSo() async throws {
    let h = try await menuHarness()
    try await h.turn(Scripted("", fail("計算機視窗在，但沒有匯出的選單")))
    #expect(await h.doll.todo.phase == .failed)
    // The read's turn is put away with the item, but it is still her latest read.
    try await h.turn(Scripted("", ("todo.set", [("items", #"["跟主人說做不到"]"#), ("facts", #"[{"fact":"計算機的視窗開著","quote":"AXWindow \"計算機\""}]"#),
                                                ("assumptions", "主人會接受換個做法"), ("limits", "不再動計算機"), ("estimate", "2"), ("check", "主人看到我的回覆")])))
    #expect(try h.toolResults().last!.text.hasPrefix("todo 列好了"))
    #expect(await h.doll.todo.plan?.facts == [PlanFact(fact: "計算機的視窗開著", quote: #"AXWindow "計算機""#, source: "最新讀到的視窗")])
}

@Test func theAssumptionsFieldSaysEveryPlanHasSomeWithExamples() throws {
    // 2026-11-17: 6 of 14 first todo.set left assumptions out, all on simple tasks.
    let d = try #require(Tools.planProperties(computer: true).first { $0.0 == "assumptions" }?.1)
    #expect("\(d)".contains("每份計畫都有") && "\(d)".contains("檔案在那裡"))
}

// the decision point counts the whole list (2026-11-17) -----------------------------------------------

@Test func aListOfTwoItemsReachesTheDecisionPointAtTheListsRoundAndANewListStartsAgain() async throws {
    let h = try await Harness(tune: { $0.computer.decideAfterRounds = 3; $0.computer.decideEveryRounds = 2 })
    await h.doll.ownerSaid("先讀再讀")
    try await h.turn(Scripted("", todoSet("讀了一次", "讀了兩次"), readCall))
    try await h.turn(Scripted("", readCall))
    try await h.turn(Scripted("", done("讀了")))
    var t = await h.doll.todo
    #expect(t.listRounds == 2 && t.rounds == 0 && !t.deciding && t.current == "讀了兩次")
    try await h.turn(Scripted("", readCall))
    let point = try #require(try h.events().last { $0.text.hasPrefix("【該決定了】") })
    #expect(point.text.hasPrefix("【該決定了】這份清單已經做了 3 回合（現在這項「讀了兩次」 1 回合）（做得成的事通常 7–14 回合）。電腦和檔案工具先停"))
    #expect(h.last.actions == ["read_window", "decision point"])
    // A restart keeps the count and the point.
    let h2 = try await restarted(h)
    t = await h2.doll.todo
    #expect(t.listRounds == 2 && t.rounds == 1 && t.deciding && t.decideAt == 3)
    // A new list starts the count again.
    try await h2.turn(Scripted("", fail("計算機視窗在，但讀兩次沒有用")))
    await h2.doll.ownerSaid("再來一份")
    try await h2.turn(Scripted("", todoSet("讀了三次"), readCall))
    t = await h2.doll.todo
    // (A restarted harness has the default config: a new list's point is at 15.)
    #expect(t.totalRounds == 0 && t.decideAt == 15 && !t.deciding && t.goOn.isEmpty)
    try await h2.turn(Scripted("", readCall))
    t = await h2.doll.todo
    #expect(t.totalRounds == 1 && !t.deciding)
}

@Test func finishingAnItemAtTheDecisionPointMovesTheNextPointOn() async throws {
    let h = try await Harness(tune: { $0.computer.decideAfterRounds = 2; $0.computer.decideEveryRounds = 2 })
    await h.doll.ownerSaid("先讀再讀")
    try await h.turn(Scripted("", todoSet("讀了一次", "讀了兩次"), readCall))
    try await h.turn(Scripted("", readCall))
    try await h.turn(Scripted("", readCall))
    #expect(await h.doll.todo.deciding)
    try await h.turn(Scripted("", done("讀了")))
    let t = await h.doll.todo
    #expect(!t.deciding && t.decideAt == 5 && t.listRounds == 3)
}

// evidence and what read_file shows (2026-11-17) ------------------------------------------------------

@Test func evidenceCopiedFromReadFilesDisplayPassesAndALineNotInTheFileStillFails() async throws {
    let h = try await Harness()
    let path = try h.put("merged.txt", "開頭\n蘋果\n香蕉\n一\n二\n三\n共 6 行\n")
    await h.doll.ownerSaid("把三個檔接起來")
    try await h.turn(Scripted("", todoSet("merged.txt 寫好"), readFile(path)))
    try await h.turn(Scripted("", dm("寫好了")))
    // Made up: a line the file does not have, with read_file's decoration around it.
    try await h.turn(Scripted("", done("寫好了", evidence: "read_file \(path)：共 8 行\n     8\t共 7 行")))
    #expect(try h.toolResults().last!.text.hasPrefix("失敗：還沒完成：evidence「"))
    #expect(try h.toolResults().last!.text.contains("  6→三\n  …（後面還有 1 行沒列出來）\n"))
    // The header alone is nothing from the file.
    try await h.turn(Scripted("", done("寫好了", evidence: "read_file \(path)：共 7 行")))
    #expect(try h.toolResults().last!.text.hasPrefix("失敗：還沒完成：evidence「"))
    // m7-mac m-1 (2026-11-17), verbatim but for the path: refused twice then; accepted now.
    let honest = "read_file \(path)：共 7 行\n     1\t開頭\n     2\t蘋果\n     3\t香蕉\n     4\t一\n     5\t二\n     6\t三\n     7\t共 6 行"
    try await h.turn(Scripted("", done("寫好了", evidence: honest)))
    #expect(try h.toolResults().last!.text.hasPrefix("完成，記下了。"))
}

@Test func theDecorationIsCutOnlyWhereReadFilePutsIt() {
    #expect(Doll.undecorated("read_file /a.txt：共 7 行\n     1\t開頭\n  2→蘋果\n第 3 行") == "開頭\n蘋果\n第 3 行")
    #expect(Doll.undecorated("read_file /a.txt：共 9 行，這裡是第 2–3 行") == "")
    #expect(Doll.quoted("     7\t共 6 行", in: "開頭\n共 6 行\n") && !Doll.quoted("     7\t共 7 行", in: "開頭\n共 6 行\n"))
    // A computer result that repeats what she asked for is not a source for it (facts, 2026-11-17).
    let menu = ToolCall(name: "act", arguments: #"{"steps": [{"menu": ["檔案", "匯出為 PDF"], "app": "Calculator"}]}"#)
    #expect(Doll.withoutArguments("失敗：Calculator 沒有「檔案」選單，找不到「匯出為 PDF」", of: menu) == "失敗： 沒有「」選單，找不到「」")
}

@Test func doneShowsTheEstimateAgainstWhatItTookAndRecordsIt() async throws {
    let h = try await menuHarness()
    try await h.turn(Scripted("", readCall))
    try await h.turn(Scripted("", done("看到了"), dm("好了")))
    let text = try h.toolResults().suffix(2).first!.text
    #expect(text.contains("這一項做了 2 回合；整份清單做完了：你估整份清單 5 回合，實際到現在 2 回合、"))
    #expect(text.contains("（記下了，下次列像這樣的清單，程式會拿來對你估的）。"))
    let rec = try #require(await h.doll.estimateFile.records.last)
    #expect(rec.plan.estimate == 5 && rec.rounds == 2 && rec.outcome == "完成" && rec.cause == nil && rec.plan.items == ["畫面上看得到結果"])
    #expect(rec.text == "用電腦" && rec.check == nil)
    #expect(try await h.doll.dashboardStatus().estimates == [rec])
    // On disk: a new process reads it. 【狀況】 no longer lists past estimates (2026-11-18: it was never used).
    let b = try await Harness(dir: h.dir)
    #expect(await b.doll.estimateFile.records == [rec])
    await b.doll.ownerSaid("再算一次")
    try await b.turn(Scripted("", todoSet(), readCall))
    #expect(!b.last.footer.contains("估 5 → 實際"))
}

@Test func aListOfTwoItemsComparesTheWholeListAndRecordsOnce() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("先讀再讀")
    try await h.turn(Scripted("", todoSet("讀了一次", "讀了兩次"), readCall))
    try await h.turn(Scripted("", readCall))
    try await h.turn(Scripted("", done("讀了")))
    #expect(try h.toolResults().last!.text.contains("這一項做了 2 回合；你估整份清單 5 回合，實際到現在 2 回合、"))
    #expect(await h.doll.estimateFile.records.isEmpty)
    try await h.turn(Scripted("", readCall))
    try await h.turn(Scripted("", done("又讀了"), dm("讀好了")))
    #expect(try h.toolResults().suffix(2).first!.text.contains("這一項做了 2 回合；整份清單做完了：你估整份清單 5 回合，實際到現在 4 回合、"))
    #expect(await h.doll.estimateFile.records.map(\.rounds) == [4])
}

@Test func goingOnNeedsTheStuckStepAndADifferentAttempt() async throws {
    let h = try await decideHarness()
    for _ in 0..<3 { try await h.turn(Scripted("", readCall)) }
    #expect(await h.doll.todo.deciding)
    try await h.turn(Scripted("", ("todo.continue", [("missing", "還缺等於"), ("why", "再試試")])))
    #expect(try h.toolResults().last!.text == "失敗：todo.continue 沒有 why 這個參數（它的參數：missing、stuck、change）")
    try await h.turn(Scripted("", goOn("還缺按等於", "按等於", "改用鍵盤 Return")))
    #expect(try h.toolResults().last!.text == "失敗：還不能繼續：stuck「按等於」太短：至少 4 個字，寫具體的。")
    try await h.turn(Scripted("", goOn("還缺按等於", "按等於那一步", "按等於那一步")))
    #expect(try h.toolResults().last!.text == "失敗：還不能繼續：change 跟 stuck 寫得一模一樣：各寫各的。")
    #expect(await h.doll.todo.deciding)
    try await h.turn(Scripted("", goOn("還缺按等於", "按等於那一步點了沒反應", "改用鍵盤 Return")))
    #expect(await !h.doll.todo.deciding)
    try await h.turn(Scripted("", readCall))
    try await h.turn(Scripted("", readCall))
    #expect(await h.doll.todo.deciding)
    let point = try #require(try h.events().last { $0.text.hasPrefix("【該決定了】") })
    #expect(point.text.hasSuffix("你之前選繼續時寫的：第 7 回合：還缺「還缺按等於」；最卡的是「按等於那一步點了沒反應」；跟之前不同的是「改用鍵盤 Return」"))
    // The same attempt again (spacing, case and punctuation aside) is not a different one.
    try await h.turn(Scripted("", goOn("還缺按等於", "還是按等於那一步", "改用 鍵盤 return！")))
    #expect(try h.toolResults().last!.text.hasPrefix("失敗：還不能繼續：change 跟你之前選繼續時寫的一樣（第 7 回合："))
    #expect(await h.doll.todo.deciding)
    try await h.turn(Scripted("", goOn("還缺按等於", "還是按等於那一步", "先讀選單找計算的指令")))
    #expect(try h.timeline().todo.goOn.count == 2)
}

@Test func failNeedsARootCauseBackedByWhatTheItemSaw() async throws {
    let h = try await menuHarness()
    try await h.turn(Scripted("", fail("網路斷了所以算不出來", evidence: "Network unreachable")))
    #expect(try h.toolResults().last!.text.hasPrefix("失敗：還沒記成失敗：evidence「Network unreachable」不在這一項你看過的東西裡"))
    #expect(await h.doll.todo.phase == .doing)
    try await h.turn(Scripted("", fail(seenCalculator)))
    #expect(try h.toolResults().last!.text.hasSuffix("cause 只是照抄了你的 evidence：寫你自己的。"))
    try await h.turn(Scripted("", fail("不知道")))
    #expect(try h.toolResults().last!.text.hasSuffix("cause「不知道」太短：至少 4 個字，寫具體的。"))
    try await h.turn(Scripted("", fail("計算機視窗在，但等號按鈕不在清單裡")))
    #expect(await h.doll.todo.phase == .failed)
    #expect(try h.toolResults().last!.text.contains("沒做成。你估整份清單 5 回合，實際到現在 4 回合、"))
    let rec = try #require(await h.doll.estimateFile.records.last)
    #expect(rec.outcome == "失敗" && rec.cause == "計算機視窗在，但等號按鈕不在清單裡" && rec.evidence == seenCalculator && rec.rounds == 4)
}

@Test func aListWithoutTheComputerQuotesWhatWasSaidAndCannotFailOnNothing() async throws {
    // The hooks apply to every list: the program cannot know beforehand whether it will use the computer.
    let h = try await Harness()
    await h.doll.ownerSaid("幫我想想明天要帶什麼")
    try await h.turn(Scripted("", todoSet("想好明天要帶的東西"), fail("主人沒說要去哪裡", evidence: "幫我想想明天要帶什麼")))
    #expect(try h.toolResults().last!.text == "失敗：還沒記成失敗：這一項還沒有任何東西可以支持原因（沒讀過視窗或檔案、工具還沒有結果、也沒有人說過話）。先動手做一次、看清楚卡在哪，或用 ask_owner 問主人。")
    try await h.turn(Scripted("", dm("要去哪裡？")))
    await h.doll.ownerSaid("我也還不知道要去哪")
    try await h.turn(Scripted("", fail("主人還沒決定去哪，沒辦法列", evidence: "還不知道要去哪"), dm("等你決定了再說")))
    #expect(await h.doll.todo.phase == .failed)
    #expect(await h.doll.estimateFile.records.last?.evidence == "還不知道要去哪")
}

// Fewer rounds (2026-11-17, owner: about three replies for 37×46) ---------------------------------

@Test func limitsTakesAnExplainedNoneAndRefusesABareOne() async throws {
    // m7-mac seq a-2, a-3: 「無」 was refused as too short; the rewrite cost a round.
    let h = try await Harness()
    await h.doll.ownerSaid("幫我算 12 乘 34")
    func set(_ limits: String) -> Scripted {
        Scripted("", ("todo.set", [("items", "[\"計算機顯示 408\"]")] + planArgs.map { $0.0 == "limits" ? ("limits", limits) : $0 }))
    }
    for made in ["無", "沒有", "同上", "沒有限制", "沒有，因為沒有", "無特別限制", "None", "N/A", "no limits"] {
        try await h.turn(set(made))
        #expect(try h.toolResults().last!.text.hasSuffix("limits「\(made)」沒寫出邊界：寫不能碰的、做不到的、有限的是什麼；真的沒有，就寫「沒有，因為…」把理由寫出來（理由至少 4 個字）。"))
        #expect(await h.doll.todo.phase == .idle)
    }
    // A copy of the question is still refused.
    try await h.turn(set("幫我算 12 乘 34"))
    #expect(try h.toolResults().last!.text.hasSuffix("limits 只是照抄了對方說的話：寫你自己的。"))
    // An honest none with its reason passes; so does a limit that starts with 沒有.
    try await h.turn(set("沒有，因為只在計算機裡按幾個鍵、不動別的"))
    #expect(try h.toolResults().last!.text.hasPrefix("todo 列好了"))
    #expect(try h.timeline().todo.plan?.limits == "沒有，因為只在計算機裡按幾個鍵、不動別的")
    #expect(await h.doll.limitsRefusal("沒有限制，只動計算機") == nil)
    #expect(await h.doll.limitsRefusal("只算這一題，不碰計算機其他設定") == nil)
}

@Test func evidenceIsTheReadsOwnWordsWhetherJoinedOrNot() {
    // m7-mac seq a-2 (2026-11-17): her second piece was reworded (上一個表示式 "37 × 46"), not copied.
    let read = "- [0] AXWindow \"計算機\" [id=main actions=[raise]]\n    - AXStaticText = \"37 × 46\" (上一個表示式)\n    - AXStaticText = \"1,702\" (編輯欄位)"
    #expect(Doll.quoted(#"AXStaticText = "37 × 46" (上一個表示式)；AXStaticText = "1,702" (編輯欄位)"#, in: read))
    #expect(Doll.quoted("AXStaticText = \"37 × 46\" (上一個表示式)\n    - AXStaticText = \"1,702\" (編輯欄位)", in: read))
    #expect(Doll.quoted("  - AXStaticText = \"1,702\" (編輯欄位)", in: read))
    // The expect an act confirmed is in the read that act returned: copying it passes.
    #expect(Doll.quoted("1,702", in: read))
    #expect(!Doll.quoted(#"AXStaticText = "1,702" (編輯欄位)；上一個表示式 "37 × 46""#, in: read))
    #expect(!Doll.quoted(#"AXStaticText = "1,703" (編輯欄位)"#, in: read))
}

@Test func theListAndItsFirstActionGoInOneReplyAndARefusedListStopsWhatFollows() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("按 7，跟我說畫面")
    // A refused todo.set: nothing after it in that reply touches the computer.
    let bare = ("todo.set", [("items", "[\"畫面顯示 7\"]")] + planArgs.map { $0.0 == "limits" ? ("limits", "無") : $0 })
    try await h.turn(Scripted("", bare, ("open", [("app", "Calculator")]), act(#"{"click": {"name": "7"}}"#)))
    #expect(h.last.actions == ["error todo.set", "refused open", "refused act"])
    #expect(try h.toolResults().suffix(2).map(\.text) == ["失敗：這則回覆前面的 todo.set 沒收（看它的結果），open 沒有執行；改好 todo.set 再做。",
                                                          "失敗：這則回覆前面的 todo.set 沒收（看它的結果），act 沒有執行；改好 todo.set 再做。"])
    #expect(await h.doll.todo.phase == .idle && h.cua.actions.isEmpty && !h.cua.tools.contains("launch_app"))
    // Accepted: the open runs in the same reply. A row of the read that open made is not one she has seen.
    try await h.turn(Scripted("", todoSet("畫面顯示 7"), ("open", [("app", "Calculator")]), clickRow(2)))
    #expect(h.last.actions.prefix(2) == ["todo set", "open"] && h.cua.actions.isEmpty)
    #expect(try h.toolResults().last!.text.contains("沒有做：第 2 行是這則回覆裡才讀到的清單，你還沒看過。看了結果再選行號；這一步就要按，用名字指定。"))
    // The reply that set the list is not one of the list's rounds.
    #expect(await h.doll.todo.rounds == 0)
    // The next reply presses the row she saw; then the line to him and the done in one reply.
    try await h.turn(Scripted("", clickRow(2)))
    #expect(try h.toolResults().last!.window?.contains(#"AXStaticText "7""#) == true && h.cua.actions == ["click"])
    #expect(await h.doll.todo.rounds == 1)
    try await h.turn(Scripted("", dm("畫面是 7"), done("按了 7，畫面顯示 7", evidence: #"AXStaticText "7""#)))
    #expect(h.last.actions == ["said discord_dm", "todo done"] && h.said == ["畫面是 7"])
    #expect(await h.doll.todo.phase == .idle)
    #expect(await h.doll.estimateFile.records.last?.rounds == 2)
}

@Test func aNamedElementCanFollowTheOpenInTheSameReply() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("按 7")
    try await h.turn(Scripted("", todoSet("畫面顯示 7"), ("open", [("app", "Calculator")]), act(#"{"click": "7"}"#)))
    #expect(h.last.actions == ["todo set", "open", "act"] && h.cua.actions == ["click"])
    // A name is not a row: it holds on the read open made in this reply, and goes as that read's token.
    #expect(h.cua.args("click").last?["element_token"] as? String == "s00000001:2")
    #expect(await h.doll.itemSteps.map(\.line).first == "open Calculator")
}

// Her own tools (2026-11-17, OwnTools.swift) ------------------------------------------------------

private func toolSave(_ name: String, _ when: String, _ params: [(String, String, String)] = []) -> (String, [(String, String)]) {
    let ps = params.map { ["name": $0.0, "value": $0.1, "about": $0.2] }
    return ("tool.save", [("name", name), ("when", when)]
            + (params.isEmpty ? [] : [("params", String(decoding: try! JSONSerialization.data(withJSONObject: ps, options: [.sortedKeys]), as: UTF8.self))]))
}
private func toolRun(_ name: String, _ args: [String: String] = [:], expect: String? = nil) -> (String, [(String, String)]) {
    ("tool.run", [("name", name)] + (args.isEmpty ? [] : [("args", String(decoding: try! JSONSerialization.data(withJSONObject: args, options: [.sortedKeys]), as: UTF8.self))])
        + (expect.map { [("expect", $0)] } ?? []))
}
private let openCalc: (String, [(String, String)]) = ("open", [("app", "Calculator")])

/// A Calculator that computes: row 1 the display, row 2 the previous expression (set after 等於;
/// a digit after it starts a new number, as the real one does).
private let calcWindow = FakeCua.Window(id: 62541, pid: 100, app: "計算機", title: "計算機", elements: [
    FakeCua.Element(role: "AXWindow", label: "計算機", parent: nil, id: "main"),
    FakeCua.Element(role: "AXStaticText", label: "0"),
    FakeCua.Element(role: "AXStaticText", label: nil),
] + "0123456789".map { FakeCua.Element(role: "AXButton", label: String($0)) } + [
    FakeCua.Element(role: "AXButton", label: "乘", id: "Multiply"),
    FakeCua.Element(role: "AXButton", label: "等於", id: "Equals"),
    FakeCua.Element(role: "AXButton", label: "全部清除", id: "AllClear"),
])

@Sendable private func calcPress(_ tool: String, _ args: [String: Any], _ s: inout FakeCua.State) {
    // Typing goes into the text area of the window named, or of the app's front window.
    if tool == "type_text", let w = s.windows.lastIndex(where: { $0.id == args["window_id"] as? Int })
        ?? s.windows.lastIndex(where: { $0.pid == args["pid"] as? Int }),
       let row = s.windows[w].elements.firstIndex(where: { $0.role == "AXTextArea" }) {
        s.windows[w].elements[row].label = (s.windows[w].elements[row].label ?? "") + (args["text"] as? String ?? "")
        return
    }
    guard tool == "click", let token = args["element_token"] as? String, let row = Int(token.split(separator: ":").last ?? ""),
          let w = s.windows.firstIndex(where: { $0.id == args["window_id"] as? Int }), s.windows[w].pid == 100,
          let label = s.windows[w].elements[row].label else { FakeCua.press(tool, args, &s); return }
    var shown = s.windows[w].elements[1].label ?? "0"
    if Int(label) != nil {
        if shown == "0" || s.windows[w].elements[2].label != nil { shown = label; s.windows[w].elements[2].label = nil } else { shown += label }
    } else if label == "乘" {
        shown += "×"
    } else if label == "等於" {
        let product = shown.split(separator: "×").compactMap { Int($0) }.reduce(1, *)
        s.windows[w].elements[2].label = shown
        let f = NumberFormatter()
        f.numberStyle = .decimal
        shown = f.string(from: NSNumber(value: product))!
    } else if label == "全部清除" {
        shown = "0"
    }
    s.windows[w].elements[1].label = shown
}

private func calcHarness(_ dir: URL? = nil, tune: @escaping (inout Config) -> Void = { _ in }) async throws -> Harness {
    let h = try await Harness(dir: dir, tune: tune)
    h.cua.change { $0.windows = [calcWindow]; $0.after = calcPress }
    return h
}

/// The owner asks 37×46; she opens Calculator, presses it through in one act, reports; then saves it.
private func savedMultiply(_ h: Harness, save: Bool = true) async throws {
    await h.doll.ownerSaid("幫我用計算機算 37×46")
    try await h.turn(Scripted("", todoSet("計算機顯示 37×46 的答案"), openCalc))
    try await h.turn(Scripted("", act(#"{"click": "3"}"#, #"{"click": "7"}"#, #"{"click": "乘"}"#, #"{"click": "4"}"#, #"{"click": "6"}"#, #"{"click": "等於"}"#, expect: "1,702")))
    try await h.turn(Scripted("", dm("37×46 = 1,702"), done("1,702", evidence: "1,702")))
    guard save else { return }
    await h.doll.ownerSaid("把你剛剛做的存成工具，兩個數字會變")
    try await h.turn(Scripted("", toolSave("乘法", "用計算機算兩個數相乘", [("a", "37", "第一個數"), ("b", "46", "第二個數")])))
}

/// A tool as the program reads it: its file and its run counters (`tool-runs.json`, 2026-11-19).
private func toolFile(_ h: Harness, _ name: String) throws -> OwnTool {
    try #require(try ToolStore(dir: h.dir.appending(path: "tools")).get(name))
}

@Test func aSavedChainIsBuiltFromWhatSheDidWithButtonNamesAsAParameter() async throws {
    let h = try await calcHarness()
    try await savedMultiply(h)
    let result = try h.toolResults().last!.text
    #expect(result.hasPrefix("存好了：工具「乘法」（tools/乘法.json），從「計算機顯示 37×46 的答案」存的。"))
    let t = try toolFile(h, "乘法")
    #expect(t.params == [OwnTool.Param(name: "a", kind: .buttons, about: "第一個數", example: "37"),
                         OwnTool.Param(name: "b", kind: .buttons, about: "第二個數", example: "46")])
    #expect(t.stepLines == ["open Calculator", "按 {{a}} 的每一個字（AXButton）", "click AXButton \"乘\"", "按 {{b}} 的每一個字（AXButton）", "click AXButton \"等於\""])
    // 1,702 does not follow the values: she states the expectation at each run.
    #expect(t.expectWas == "1,702" && t.parts.last?.expect == nil && result.contains("每次用的時候要給 expect"))
    #expect(t.parts.first?.bundleID == "com.apple.calculator" && t.origin.task == "幫我用計算機算 37×46" && t.origin.sequences.count == 1)
    #expect(t.uses == 0 && t.successes == 0)
    // The file is plain sorted JSON, one field a line.
    let text = try String(contentsOf: h.dir.appending(path: "tools/乘法.json"), encoding: .utf8)
    #expect(text.contains("\"press\" : \"{{a}}\"") && text.contains("\"bundle_id\" : \"com.apple.calculator\""))
}

@Test func aRunInTheSameReplyAsTodoSetReplaysWithNewValuesAndCountsTheSuccess() async throws {
    let h = try await calcHarness()
    try await savedMultiply(h)
    await h.doll.ownerSaid("再算 58×13")
    let before = h.cua.actions.count
    try await h.turn(Scripted("", todoSet("計算機顯示 58×13 的答案"), toolRun("乘法", ["a": "58", "b": "13"], expect: "754")))
    let r = try h.toolResults().last!.text
    #expect(r.contains("工具「乘法」7 步都做了：open Calculator、click AXButton \"5\"、click AXButton \"8\"、click AXButton \"乘\"、click AXButton \"1\"、click AXButton \"3\"、click AXButton \"等於\"。"))
    #expect(r.contains("跟你預期的一樣：視窗裡有「754」。") && r.contains("工具「乘法」用了 1 次，成功 1 次。"))
    #expect(h.cua.actions.count - before == 6)
    let t = try toolFile(h, "乘法")
    #expect(t.uses == 1 && t.successes == 1 && t.failuresInRow == 0 && t.lastRun != nil)
    // Its steps are in the action log like any others, marked with the tool.
    let log = try String(contentsOf: h.dir.appending(path: "computer-actions.jsonl"), encoding: .utf8).split(separator: "\n")
    #expect(log.suffix(7).allSatisfy { $0.contains("\"via\":\"乘法\"") } && log.suffix(6).allSatisfy { $0.contains("\"sequence\"") })
    // Without the expectation, nothing is done.
    try await h.turn(Scripted("", toolRun("乘法", ["a": "2", "b": "3"])))
    #expect(try h.toolResults().last!.text.contains("要給 expect：這次做完視窗裡會出現的字，照畫面的寫法（存的那次是「1,702」）"))
    #expect(try toolFile(h, "乘法").uses == 1)
}

@Test func aRefusedTodoSetStopsTheToolRunInTheSameReply() async throws {
    let h = try await calcHarness()
    try await savedMultiply(h)
    await h.doll.ownerSaid("再算 58×13")
    let before = h.cua.actions.count
    try await h.turn(Scripted("", ("todo.set", [("items", #"["計算機顯示 58×13 的答案"]"#)]), toolRun("乘法", ["a": "58", "b": "13"], expect: "754")))
    #expect(try h.toolResults().last!.text == "失敗：這則回覆前面的 todo.set 沒收（看它的結果），tool.run 沒有執行；改好 todo.set 再做。")
    #expect(h.cua.actions.count == before)
    #expect(try toolFile(h, "乘法").uses == 0)
}

/// A `todo.set` with these facts (JSON) and the other plan fields of `planArgs`.
private func setFacts(_ item: String, _ facts: String) -> (String, [(String, String)]) {
    ("todo.set", [("items", "[\"\(item)\"]"), ("facts", facts)] + planArgs.filter { $0.0 != "facts" })
}

@Test func aQuoteOfAToolSaveRefusalIsToldWhichPartSheWrote() async throws {
    // 2026-11-18 (m7-mac p-1, p-2): she quoted the refusal whole; its item holds the value she gave tool.save.
    let h = try await calcHarness()
    await h.doll.ownerSaid("打開計算機給我看")
    try await h.turn(Scripted("", todoSet("打開計算機給主人看"), openCalc))
    try await h.turn(Scripted("", dm("開好了"), done("開好了")))
    try await h.turn(Scripted("", toolSave("看計算機", "主人要看計算機的時候", [("a", "打開計算機", "要開的東西")])))
    let refusal = try h.toolResults().last!.text
    #expect(refusal == "失敗：沒存：「打開計算機給主人看」裡沒有做成的 act。工具只能從真的做過、結果跟你預期一樣的動作存；先做一次，再存。")
    let whole = String(refusal.dropFirst(3))
    try await h.turn(Scripted("", setFacts("存好工具", #"[{"fact":"上次沒存成","quote":"\#(whole)"}]"#)))
    #expect(try h.toolResults().last!.text.contains("quote 裡的「打開計算機」是你存工具時自己寫的字，不算出處；只抄程式或別人寫的部分。"))
    try await h.turn(Scripted("", setFacts("存好工具", #"[{"fact":"上次沒存成","quote":"裡沒有做成的 act。工具只能從真的做過、結果跟你預期一樣的動作存"}]"#)))
    #expect(await h.doll.todo.plan?.facts.map(\.source) == ["tool.save 的結果"])
}

@Test func aFactMayQuoteTheProgramsLinesAboutHerToolsButNotWhatSheWroteInThem() async throws {
    // 2026-11-18 (m7-mac tools): 5 of 7 refused plans quoted the tool.save result or the 〔工具〕 offer.
    let h = try await calcHarness { $0.computer.ownToolOfferSimilarity = 0.3 }
    try await savedMultiply(h)
    try await h.turn(Scripted("", dm("我覺得這個乘法工具很好用喔")))
    await h.doll.ownerSaid("幫我用計算機算 58×13")
    let item = "計算機顯示 58×13 的答案"
    func refusal(_ quote: String, own: String?) -> String {
        "失敗：清單沒有列：facts 第 1 條「我有乘法工具」的 quote「\(quote)」不在你看得到的地方（主人說的話、工具的結果、讀過的視窗或檔案、【狀況】、你的目標樹、【相關記憶】裡〔工具〕那一行）。"
            + (own.map { "quote 裡的「\($0)」是你存工具時自己寫的字，不算出處；只抄程式或別人寫的部分。" } ?? "")
            + "fact 要有出處：quote 一字不差照抄那裡的字。找不到出處，就是你以為的：把它搬到 assumptions；想確定，就把「去看」當成清單裡的一步，看到了再說。"
    }
    // Her own earlier line is not a source.
    try await h.turn(Scripted("", setFacts(item, #"[{"fact":"我有乘法工具","quote":"這個乘法工具很好用"}]"#)))
    #expect(try h.toolResults().last!.text == refusal("這個乘法工具很好用", own: nil))
    // The offer's words she wrote when saving (its `when`, a parameter's `about`) are not a source (j-1, z-1).
    try await h.turn(Scripted("", setFacts(item, #"[{"fact":"我有乘法工具","quote":"「乘法」：用計算機算兩個數相乘"}]"#)))
    #expect(try h.toolResults().last!.text == refusal("「乘法」：用計算機算兩個數相乘", own: "用計算機算兩個數相乘"))
    try await h.turn(Scripted("", setFacts(item, #"[{"fact":"我有乘法工具","quote":"參數 a（第一個數；存的時候是「37」）"}]"#)))
    #expect(try h.toolResults().last!.text == refusal("參數 a（第一個數；存的時候是「37」）", own: "第一個數"))
    // Her own tool argument alone.
    try await h.turn(Scripted("", setFacts(item, #"[{"fact":"我有乘法工具","quote":"用計算機算兩個數相乘"}]"#)))
    #expect(try h.toolResults().last!.text == refusal("用計算機算兩個數相乘", own: "用計算機算兩個數相乘"))
    #expect(await h.doll.todo.phase == .idle)
    // What the program wrote passes, with where it is from: the tool.save result (k-2, k-3) and the offer's usage (z-2).
    let facts = #"[{"fact":"我存了工具「乘法」","quote":"存好了：工具「乘法」（tools/乘法.json）"},"#
        + #"{"fact":"乘法工具要給 expect","quote":"用法：tool.run(name: \"乘法\", args: {\"a\": …, \"b\": …}，要給 expect)"}]"#
    try await h.turn(Scripted("", setFacts(item, facts), toolRun("乘法", ["a": "58", "b": "13"], expect: "754")))
    #expect(try h.toolResults().suffix(2).first!.text.hasPrefix("todo 列好了"))
    #expect(await h.doll.todo.plan?.facts.map(\.source) == ["tool.save 的結果", "【相關記憶】提的工具"])
    #expect(try h.toolResults().last!.text.contains("跟你預期的一樣：視窗裡有「754」。"))
}

@Test func savingIsRefusedWhenTheChainDidNotRunThroughOrDidNotMatch() async throws {
    // The last act did not match her expectation.
    let h = try await calcHarness()
    await h.doll.ownerSaid("幫我用計算機算 37×46")
    try await h.turn(Scripted("", todoSet("計算機顯示 37×46 的答案"), openCalc))
    try await h.turn(Scripted("", act(#"{"click": "3"}"#, #"{"click": "7"}"#, expect: "38")))
    try await h.turn(Scripted("", toolSave("乘法", "算乘法")))
    #expect(try h.toolResults().last!.text == "失敗：沒存：「計算機顯示 37×46 的答案」最後一次 act（click \"3\"、click \"7\"）沒做完或跟你預期的不一樣，這串動作沒有完整做成。做到跟預期一樣以後再存。")
    // A step that failed.
    h.cua.change { $0.refuse["click"] = #"{"error":"press failed"}"# }
    try await h.turn(Scripted("", act(#"{"click": "全部清除"}"#)))
    try await h.turn(Scripted("", toolSave("乘法", "算乘法")))
    #expect(try h.toolResults().last!.text.contains("最後一次 act（click \"全部清除\"）沒做完或跟你預期的不一樣"))
    // A last act without an expectation.
    try await h.turn(Scripted("", act(#"{"click": "全部清除"}"#)))
    try await h.turn(Scripted("", toolSave("乘法", "算乘法")))
    #expect(try h.toolResults().last!.text == "失敗：沒存：「計算機顯示 37×46 的答案」最後一次 act 沒寫 expect，程式沒辦法確認那次結果是對的。用寫了 expect 的 act 做到一樣，再存。")
    // The corrected attempt is left out, the one that matched is kept.
    try await h.turn(Scripted("", act(#"{"click": "3"}"#, #"{"click": "7"}"#, #"{"click": "乘"}"#, #"{"click": "4"}"#, #"{"click": "6"}"#, #"{"click": "等於"}"#, expect: "1,702")))
    try await h.turn(Scripted("", toolSave("乘法", "算乘法")))
    let ok = try h.toolResults().last!.text
    #expect(ok.contains("沒存進去的：第 1 次 act（click \"3\"、click \"7\"）做完了可是跟你預期的不一樣，整次沒存；第 2 次 act 從第 1 步（click \"全部清除\"）起沒做成，那幾步沒存。"))
    #expect(try toolFile(h, "乘法").stepLines == ["open Calculator", "click AXButton \"全部清除\"", "click AXButton \"3\"", "click AXButton \"7\"", "click AXButton \"乘\"",
                                                 "click AXButton \"4\"", "click AXButton \"6\"", "click AXButton \"等於\"", "（預期視窗裡有「1,702」）"])
    // A value that is nowhere in the steps, and a failed item.
    try await h.turn(Scripted("", toolSave("乘法2", "算乘法", [("a", "99", "數")])))
    #expect(try h.toolResults().last!.text.hasPrefix("失敗：沒存：參數「a」的值「99」不在要存的步驟裡"))
    try await h.turn(Scripted("", fail("算不出來", evidence: "1,702")))
    try await h.turn(Scripted("", toolSave("乘法3", "算乘法")))
    #expect(try h.toolResults().last!.text == "失敗：沒存：上一項「計算機顯示 37×46 的答案」是用 todo.fail 收的；沒做成的事不能存成工具。")
}

@Test func aTextAndAFileParameterAreReplacedAndTheirExpectationFollows() async throws {
    let h = try await calcHarness()
    h.cua.change { s in s.after = calcPress }
    // Text: a new TextEdit document, typed into.
    await h.doll.ownerSaid("在 TextEdit 開新文件寫買牛奶")
    try await h.turn(Scripted("", todoSet("TextEdit 新文件寫著買牛奶"), act(#"{"menu": ["檔案", "新增"], "app": "TextEdit"}"#)))
    try await h.turn(Scripted("", act(#"{"type": "記得買牛奶"}"#, expect: "記得買牛奶")))
    try await h.turn(Scripted("", toolSave("寫一句", "在 TextEdit 新文件寫一句話", [("內容", "買牛奶", "要寫的東西")])))
    let t = try toolFile(h, "寫一句")
    #expect(t.params.map(\.kind) == [.text] && t.stepLines == ["menu 檔案 › 新增", "type「記得{{內容}}」", "（預期視窗裡有「記得{{內容}}」）"])
    #expect(t.expectWas == nil)
    h.cua.change { $0.windows.removeAll { $0.pid == 200 } }
    try await h.turn(Scripted("", toolRun("寫一句", ["內容": "買雞蛋"])))
    let r = try h.toolResults().last!.text
    #expect(r.contains("type「記得買雞蛋」") && r.contains("跟你預期的一樣：視窗裡有「記得買雞蛋」。"))
    // File: open hands TextEdit a file; the expectation is hers at each run.
    let dir = FileManager.default.temporaryDirectory.appending(path: "owntools-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    let a = dir.appending(path: "a.txt").path, b = dir.appending(path: "b.txt").path
    for p in [a, b] { FileManager.default.createFile(atPath: p, contents: Data()) }
    try await h.turn(Scripted("", dm("寫好了"), done("寫好了", evidence: "記得買雞蛋")))
    await h.doll.ownerSaid("打開 a.txt 寫 hi")
    try await h.turn(Scripted("", todoSet("a.txt 寫著 hi"), ("open", [("app", "TextEdit"), ("target", a)])))
    try await h.turn(Scripted("", act(#"{"type": "hi"}"#, expect: "hi")))
    try await h.turn(Scripted("", toolSave("檔案寫hi", "打開一個檔案寫 hi", [("檔", a, "要開的檔案")])))
    let f = try toolFile(h, "檔案寫hi")
    #expect(f.params.map(\.kind) == [.target])
    #expect(f.parts.first?.target == "{{檔}}")
    #expect(f.expectWas == "hi")
    try await h.turn(Scripted("", toolRun("檔案寫hi", ["檔": b], expect: "hi")))
    let r2 = try h.toolResults().last!.text
    #expect(r2.contains("open TextEdit \(b)") && r2.contains("跟你預期的一樣"))
    #expect(h.cua.state.withLock { $0.windows.contains { $0.title == "b.txt" && $0.elements[1].label == "hi" } })
}

@Test func aMissingOrDoubledButtonStopsTheReplayWithoutPressingAnything() async throws {
    let h = try await calcHarness()
    try await savedMultiply(h)
    await h.doll.ownerSaid("算 1.5×4")
    let before = h.cua.actions.count
    try await h.turn(Scripted("", todoSet("計算機顯示 1.5×4 的答案"), toolRun("乘法", ["a": "1.5", "b": "4"], expect: "6")))
    let r = try h.toolResults().last!.text
    #expect(r.hasPrefix("失敗：") == false)
    #expect(r.contains("工具「乘法」停了：第 3 步要按「.」（參數 a 的值「1.5」裡的字），可是Calculator「計算機」裡沒有名字叫「.」的 AXButton。這一段（第 2–7 步）一步都沒按。"))
    #expect(r.contains("做過的：open Calculator。"))
    #expect(h.cua.actions.count == before && h.cua.actions.filter { $0 == "click" }.count == 6)
    #expect(try toolFile(h, "乘法").uses == 1 && toolFile(h, "乘法").successes == 0 && toolFile(h, "乘法").failuresInRow == 1)
    // Two buttons named 乘: stopped at that step, the steps before it done (as act does).
    h.cua.change { $0.windows[0].elements.append(FakeCua.Element(role: "AXButton", label: "乘")) }
    try await h.turn(Scripted("", toolRun("乘法", ["a": "2", "b": "4"], expect: "8")))
    let r2 = try h.toolResults().last!.text
    #expect(r2.contains("第 3 步（click AXButton \"乘\"）沒做成：沒有做：名字叫「乘」的有 2 個："))
    #expect(try toolFile(h, "乘法").failuresInRow == 2)
}

@Test func aToolThatKeepsFailingIsNoLongerOfferedOrRunButStaysForTheOwner() async throws {
    let h = try await calcHarness { $0.computer.ownToolOfferSimilarity = 0.3 }
    try await savedMultiply(h)
    await h.doll.ownerSaid("再算 58×13")
    try await h.turn(Scripted("", todoSet("計算機顯示 58×13 的答案"), toolRun("乘法", ["a": "58", "b": "13"], expect: "999")))
    #expect(try h.toolResults().last!.text.contains("跟你預期的不一樣"))
    try await h.turn(Scripted("", toolRun("乘法", ["a": "58", "b": "13"], expect: "998")))
    try await h.turn(Scripted("", toolRun("乘法", ["a": "58", "b": "13"], expect: "997")))
    #expect(try h.toolResults().last!.text.contains("工具「乘法」用了 3 次，成功 0 次；連續失敗 3 次，停用了，之後不會再提它（主人在 dashboard 看得到）。"))
    try await h.turn(Scripted("", toolRun("乘法", ["a": "58", "b": "13"], expect: "754")))
    #expect(try h.toolResults().last!.text == "失敗：沒有做：工具「乘法」連續失敗 3 次，停用了（沒刪，主人在 dashboard 看得到）。這次用 act 自己做。")
    let listed = try await h.doll.dashboardTools()
    #expect(listed.map(\.name) == ["乘法"] && listed[0].retired && listed[0].uses == 3 && listed[0].successes == 0)
    try await h.turn(Scripted("", dm("改用手算"), done("沒做", evidence: "997")))
    await h.doll.ownerSaid("幫我用計算機算 58×13")
    try await h.turn(Scripted("", dm("好")))
    #expect(try h.events().filter { $0.kind == .memory }.allSatisfy { !$0.text.contains("〔工具〕") })
}

@Test func theOfferAttachTheCapDeletionAndARestartKeepTheToolsAndTheirCounts() async throws {
    let h = try await calcHarness { $0.computer.ownToolOfferSimilarity = 0.3; $0.computer.ownToolsMax = 1 }
    try await savedMultiply(h)
    // The owner's next line fits the tool: the 【相關記憶】 says she has it, before her first reply.
    await h.doll.ownerSaid("幫我用計算機算 58×13")
    try await h.turn(Scripted("", todoSet("計算機顯示 58×13 的答案"), toolRun("乘法", ["a": "58", "b": "13"], expect: "754")))
    let offer = try h.events().last { $0.kind == .memory && $0.text.contains("〔工具〕") }
    #expect(offer?.text.contains("〔工具〕「乘法」：用計算機算兩個數相乘（參數 a（第一個數；存的時候是「37」）、b（第二個數；存的時候是「46」）；用了 0 次，成功 0 次）。用法：tool.run(name: \"乘法\", args: {\"a\": …, \"b\": …}，要給 expect)") == true)
    let req = await h.model.seen(await h.model.requests.count - 1)
    #expect(req.contains { $0.content.contains("〔工具〕「乘法」") })
    // A line that does not fit offers nothing.
    let qv = FakeEmbedder.vector("晚安，明天見"), dv = FakeEmbedder.vector("乘法：用計算機算兩個數相乘\n幫我用計算機算 37×46")
    #expect(zip(qv, dv).reduce(Float(0)) { $0 + $1.0 * $1.1 } < 0.3)
    // The cap.
    try await h.turn(Scripted("", dm("754"), done("754", evidence: "754")))
    try await h.turn(Scripted("", toolSave("乘法2", "再存一次")))
    #expect(try h.toolResults().last!.text == "失敗：沒存：工具已經有 1 個，到上限 1 個了。請主人在 dashboard 刪掉用不到的。")
    // A restart: the tool and its counts are read from the file; what she did before it cannot be saved.
    let h2 = try await calcHarness(h.dir) { $0.computer.ownToolOfferSimilarity = 0.3 }
    let listed = try await h2.doll.dashboardTools()
    #expect(listed.map(\.name) == ["乘法"] && listed[0].uses == 1 && listed[0].successes == 1)
    #expect(listed[0].steps.first == "open Calculator" && listed[0].expect == "每次她說預期什麼（存的那次是「1,702」）")
    try await h2.turn(Scripted("", toolSave("乘法3", "x")))
    #expect(try h2.toolResults().last!.text.hasPrefix("失敗：沒存：沒有可以存的動作"))
    // The owner deletes it from the dashboard.
    let d = Dashboard(doll: h2.doll, config: DashboardConfig())
    #expect(await d.respond(method: "POST", target: "/api/tools/delete?name=%E4%B9%98%E6%B3%95").status == 200)
    #expect(try await h2.doll.dashboardTools().isEmpty)
    #expect(!FileManager.default.fileExists(atPath: h.dir.appending(path: "tools/乘法.json").path))
    #expect(await d.respond(method: "POST", target: "/api/tools/delete?name=nothing").status == 404)
}

@Test func herToolsAreTwoFixedSpecs() {
    let specs = Tools.specs(memory: MemoryConfig(), voice: false, computer: ComputerConfig(), files: FilesConfig())
    #expect(specs.filter { $0.name.hasPrefix("tool.") }.map(\.name) == ["tool.save", "tool.run"])
    #expect(Tools.gated.contains("tool.run") && !Tools.gated.contains("tool.save") && Tools.actionNames.contains("tool.run"))
}

@Test func theStepsBeforeAFailedStepAreKeptAndANameThatChangesEveryTimeIsKeptByItsRole() async throws {
    // Notes on this Mac (2026-11-17, j-1): 新增備忘錄 went through, typing into the focus was refused,
    // then she typed into the new note's body by its row, whose only name is the note's own id.
    let h = try await calcHarness()
    let noteBody = FakeCua.Element(role: "AXTextArea", label: nil, id: "Note[id=0772C119-5372-4A30-BF06-7E309824E2AF]")
    h.cua.change { s in
        s.windows.append(FakeCua.Window(id: 70000, pid: 200, app: "TextEdit", title: "備忘錄", elements: [
            FakeCua.Element(role: "AXWindow", label: "備忘錄", parent: nil), FakeCua.Element(role: "AXButton", label: "新增備忘錄"), noteBody]))
    }
    await h.doll.ownerSaid("新增一則備忘錄寫買牛奶")
    try await h.turn(Scripted("", todoSet("備忘錄寫著買牛奶"), ("open", [("app", "TextEdit")])))
    h.cua.refuse("type_text", #"{"error":"minimized_or_hidden_window"}"#)
    try await h.turn(Scripted("", act(#"{"click": "新增備忘錄"}"#, #"{"type": "記得買牛奶"}"#, expect: "記得買牛奶")))
    try await h.turn(Scripted("", act(#"{"type": "記得買牛奶", "in": 2}"#, expect: "記得買牛奶")))
    try await h.turn(Scripted("", toolSave("新備忘錄", "在備忘錄新增一則", [("內容", "買牛奶", "要寫的")])))
    let saved = try h.toolResults().last!.text
    #expect(saved.contains("沒存進去的：第 1 次 act 從第 2 步（type「記得買牛奶」）起沒做成，那幾步沒存（前面 1 步做了，存了）。"))
    let t = try toolFile(h, "新備忘錄")
    #expect(t.stepLines == ["open TextEdit", "click AXButton \"新增備忘錄\"", "type「記得{{內容}}」 進 唯一的 AXTextArea", "（預期視窗裡有「記得{{內容}}」）"])
    #expect(t.parts[2].act?.first?.into == OwnTool.Element(name: nil, role: "AXTextArea", axID: "Note[id=0772C119-5372-4A30-BF06-7E309824E2AF"))
    // Replayed: the one text area of the window, whatever its id now.
    h.cua.change { $0.windows[$0.windows.count - 1].elements[2] = FakeCua.Element(role: "AXTextArea", label: nil, id: "Note[id=8A0A1687-B3E4-4975-9614-6DF2419420E2]") }
    try await h.turn(Scripted("", toolRun("新備忘錄", ["內容": "買雞蛋"])))
    let r = try h.toolResults().last!.text
    #expect(r.contains("click AXButton \"新增備忘錄\"") && r.contains("跟你預期的一樣：視窗裡有「記得買雞蛋」。"))
    // Two text areas: stopped at that step.
    h.cua.change { $0.windows[$0.windows.count - 1].elements.append(FakeCua.Element(role: "AXTextArea", label: nil)) }
    try await h.turn(Scripted("", toolRun("新備忘錄", ["內容": "買蛋"])))
    #expect(try h.toolResults().last!.text.contains("工具「新備忘錄」停了：第 3 步沒做成：沒有做：role 是 AXTextArea的有 2 個：[2] AXTextArea \"記得買雞蛋\"、[3] AXTextArea。"))
}

// The estimate check (2026-11-18, owner: the program compares) -------------------------------

/// The estimate check is off in the harness (tests about something else finish lists estimated 5 in 2
/// rounds); these switch it on with the shipped value.
private func checking(_ c: inout Config) { c.computer.estimateSimilarity = 0.90 }

/// A `todo.set` of `items` with the plan fields, its estimate and an optional reason.
private func estimated(_ estimate: Int, reason: String? = nil, items: [String] = ["畫面上看得到結果"]) -> (String, [(String, String)]) {
    ("todo.set", [("items", String(decoding: try! JSONEncoder().encode(items), as: UTF8.self))]
        + planArgs.map { $0.0 == "estimate" ? ("estimate", "\(estimate)") : $0 } + (reason.map { [("estimate_reason", $0)] } ?? []))
}

/// One finished list: the owner's line, `todo.set` with `estimate`, then `rounds` rounds (reads, then done).
private func finishedList(_ h: Harness, _ line: String, estimate: Int, rounds: Int, items: [String] = ["畫面上看得到結果"]) async throws {
    await h.doll.ownerSaid(line)
    try await h.turn(Scripted("", estimated(estimate, items: items), readCall))
    for _ in 1..<rounds { try await h.turn(Scripted("", readCall)) }
    try await h.turn(Scripted("", done("看到了"), dm("好了")))
}

private let farRefusal = "失敗：清單沒有列：你估整份 2 回合，可是以前做過像這次的清單：「畫面上看得到結果」你估 3、實際 7 回合。"
    + "實際的中位數是 7 回合，你估的比它少太多；估 4 到 14 回合才算接近。再列一次：改 estimate；或照你的估法，加 estimate_reason 寫這次跟以前那幾份哪裡不一樣。"

@Test func withNoLikePastListTheEstimateIsAccepted() async throws {
    let h = try await Harness(tune: checking)
    await h.doll.ownerSaid("幫我用計算機算 37×46，跟我說答案")
    try await h.turn(Scripted("", estimated(2), readCall))
    #expect(try h.toolResults().first!.text == "todo 列好了，你估整份 2 回合。現在進行中：「畫面上看得到結果」；後面：沒有。")
    let todo = await h.doll.todo
    #expect(todo.estimateCheck == nil && todo.listText == "幫我用計算機算 37×46，跟我說答案")
}

@Test func aFarEstimateIsRefusedOnceWithWhatLikeListsTookAndACorrectedOnePasses() async throws {
    let h = try await Harness(tune: checking)
    try await finishedList(h, "幫我用計算機算 37×46，跟我說答案", estimate: 3, rounds: 7)
    // Other numbers, the same kind: digits are masked.
    await h.doll.ownerSaid("幫我用計算機算 58×13，跟我說答案")
    try await h.turn(Scripted("", estimated(2), readCall))
    #expect(try h.toolResults().first { $0.text.hasPrefix("失敗：清單沒有列：你估") }?.text == farRefusal)
    #expect(await h.doll.todo.phase == .idle)
    try await h.turn(Scripted("", estimated(5), readCall))
    #expect(try h.toolResults().last { $0.text.hasPrefix("todo 列好了") }?.text
            == "todo 列好了，你估整份 5 回合（以前像這樣的清單實際 7 回合）。現在進行中：「畫面上看得到結果」；後面：沒有。")
    let check = try #require(await h.doll.todo.estimateCheck)
    #expect(check == EstimateCheck(past: [.init(items: ["畫面上看得到結果"], estimate: 3, rounds: 7)], reference: 7, low: 4, high: 14, refused: 2, reason: nil))
    // The list's record keeps the check; the dashboard shows it.
    #expect(try await h.doll.dashboardStatus().estimateCheck == check)
    try await h.turn(Scripted("", done("看到了"), dm("好了")))
    #expect(await h.doll.estimateFile.records.last?.check == check)
}

@Test func aFarEstimateKeptPassesWithAReasonAndTheReasonIsRecorded() async throws {
    let h = try await Harness(tune: checking)
    try await finishedList(h, "幫我用計算機算 37×46，跟我說答案", estimate: 3, rounds: 7)
    await h.doll.ownerSaid("幫我用計算機算 58×13，跟我說答案")
    // A reason before any refusal does not count.
    try await h.turn(Scripted("", estimated(2, reason: "這次有存好的乘法工具，一次按完"), readCall))
    #expect(try h.toolResults().last { $0.text.hasPrefix("失敗：清單") }?.text == farRefusal)
    // Kept without a reason, or with filler: refused for the reason only.
    try await h.turn(Scripted("", estimated(2, reason: "沒有"), readCall))
    #expect(try h.toolResults().last { $0.text.hasPrefix("失敗：清單") }?.text
            == "失敗：清單沒有列：estimate 還是 2 回合，不在 4 到 14 之間。改成這中間的數；或照你的估法，在 estimate_reason 寫這次跟以前那幾份哪裡不一樣。estimate_reason「沒有」太短：至少 4 個字，寫具體的。")
    try await h.turn(Scripted("", estimated(2, reason: "這次有存好的乘法工具，一次按完"), readCall))
    #expect(try h.toolResults().last { $0.text.hasPrefix("todo 列好了") }?.text
            == "todo 列好了，你估整份 2 回合（跟以前像這樣的清單實際 7 回合不一樣，你寫的理由記下了，主人看得到）。現在進行中：「畫面上看得到結果」；後面：沒有。")
    #expect(await h.doll.todo.estimateCheck?.reason == "這次有存好的乘法工具，一次按完")
    #expect(await h.doll.todo.estimateCheck?.refused == 2)
}

@Test func theEstimateIsNeverRefusedAThirdTime() async throws {
    let h = try await Harness(tune: checking)
    try await finishedList(h, "幫我用計算機算 37×46，跟我說答案", estimate: 3, rounds: 7)
    await h.doll.ownerSaid("幫我用計算機算 58×13，跟我說答案")
    try await h.turn(Scripted("", estimated(2), readCall))
    try await h.turn(Scripted("", estimated(2), readCall))
    #expect(try h.toolResults().last { $0.text.hasPrefix("失敗：清單") }?.text
            == "失敗：清單沒有列：estimate 還是 2 回合，不在 4 到 14 之間。改成這中間的數；或照你的估法，在 estimate_reason 寫這次跟以前那幾份哪裡不一樣。")
    try await h.turn(Scripted("", estimated(2), readCall))
    #expect(try h.toolResults().last { $0.text.hasPrefix("todo 列好了") }?.text
            == "todo 列好了，你估整份 2 回合（跟以前像這樣的清單實際 7 回合差很多，沒寫理由，照收了；主人看得到）。現在進行中：「畫面上看得到結果」；後面：沒有。")
    #expect(try h.toolResults().filter { $0.text.hasPrefix("失敗：清單") }.count == 2)
}

@Test func anEstimateAlreadyCloseIsAcceptedUntouched() async throws {
    let h = try await Harness(tune: checking)
    try await finishedList(h, "幫我用計算機算 37×46，跟我說答案", estimate: 3, rounds: 7)
    await h.doll.ownerSaid("幫我用計算機算 58×13，跟我說答案")
    // A reason she adds unasked is recorded for the owner; the result says nothing more.
    try await h.turn(Scripted("", estimated(6, reason: "跟上次一樣三個檔分開讀"), readCall))
    #expect(try h.toolResults().last { $0.text.hasPrefix("todo 列好了") }?.text == "todo 列好了，你估整份 6 回合。現在進行中：「畫面上看得到結果」；後面：沒有。")
    let check = await h.doll.todo.estimateCheck
    #expect(check?.refused == nil && check?.reference == 7 && check?.reason == "跟上次一樣三個檔分開讀")
    // 2 against 3 is not far (the ratio alone would call it so).
    let far = await [h.doll.estimateFar(2, 3), h.doll.estimateFar(2, 7), h.doll.estimateFar(1, 3), h.doll.estimateFar(2, 5)]
    #expect(far == [false, true, false, true])
}

@Test func aDifferentKindOfListIsNotComparedWithTheRealEmbedder() async throws {
    // The closest pair of different kinds measured (0.847): multiply, and multiply then by n.txt's number.
    let h = try await Harness(embedder: try await qwen3.value, tune: checking)
    try await finishedList(h, "幫我用計算機算 37×46，跟我說答案", estimate: 3, rounds: 7)
    await h.doll.ownerSaid("幫我用計算機算 37×46，再把答案乘上 ~/DollOS-test/n.txt 裡寫的數字，跟我說最後的答案")
    try await h.turn(Scripted("", estimated(2), readCall))
    #expect(try h.toolResults().last { $0.text.hasPrefix("todo 列好了") }?.text.hasPrefix("todo 列好了，你估整份 2 回合。") == true)
    #expect(await h.doll.todo.estimateCheck == nil)
    try await h.turn(Scripted("", done("看到了"), dm("好了")))
    // The same kind with other numbers is.
    await h.doll.ownerSaid("幫我用計算機算 204×9，跟我說答案")
    try await h.turn(Scripted("", estimated(2), readCall))
    #expect(try h.toolResults().last { $0.text.hasPrefix("失敗：清單") }?.text == farRefusal)
}

@Test func pastListsAreComparedAfterARestart() async throws {
    let h = try await Harness(tune: checking)
    try await finishedList(h, "幫我用計算機算 37×46，跟我說答案", estimate: 3, rounds: 7)
    let b = try await Harness(dir: h.dir, tune: checking)
    await b.doll.ownerSaid("幫我用計算機算 58×13，跟我說答案")
    try await b.turn(Scripted("", estimated(2), readCall))
    #expect(try b.toolResults().last { $0.text.hasPrefix("失敗：清單") }?.text == farRefusal)
    // The list in progress keeps its text and check across a restart.
    try await b.turn(Scripted("", estimated(5), readCall))
    let c = try await Harness(dir: h.dir, tune: checking)
    let todo = await c.doll.todo
    #expect(todo.estimateCheck?.refused == 2 && todo.listText == "幫我用計算機算 58×13，跟我說答案")
}

@Test func anOldEstimatesFileLoadsAndItsListsAreNotCompared() async throws {
    let h = try await Harness(tune: checking)
    // As written on 2026-11-17: no text, no check.
    let old = #"{"records":[{"outcome":"完成","plan":{"assumptions":"a","check":"c","estimate":3,"facts":[{"fact":"f","quote":"q","source":"主人說的"}],"items":["畫面上看得到結果"],"limits":"l"},"rounds":8,"seconds":36,"ts":1794941993.8}]}"#
    try old.write(to: await h.doll.estimatesURL, atomically: true, encoding: .utf8)
    let b = try await Harness(dir: h.dir, tune: checking)
    let records = await b.doll.estimateFile.records
    #expect(records.count == 1 && records[0].text == nil)
    await b.doll.ownerSaid("幫我用計算機算 37×46，跟我說答案")
    try await b.turn(Scripted("", estimated(2), readCall))
    #expect(try b.toolResults().last { $0.text.hasPrefix("todo 列好了") }?.text.hasPrefix("todo 列好了，你估整份 2 回合。") == true)
    #expect(await b.doll.todo.estimateCheck == nil)
}

// correcting a memory (2026-11-18, `MemoryFix.swift`, `Audit.swift`) --------------------------------------

private func fix(_ id: String, _ text: String?, basis: String, why: String = "記錯了") -> (String, [(String, String)]) {
    ("memory.fix", [("id", id)] + (text.map { [("text", $0)] } ?? []) + [("basis", basis), ("why", why)])
}

/// The one memory whose body is `body`.
private func memoryID(_ h: Harness, _ body: String) throws -> String {
    guard let id = try memoryFilesByID(h).first(where: { $0.value.file.body == body })?.key else {
        throw MemoryError(description: "no memory「\(body)」")
    }
    return id
}

private func fixesFile(_ h: Harness) throws -> MemoryFixFile {
    try Concerns.read(MemoryFixFile.self, h.dir.appending(path: "memory-fixes.json"), what: "memory fixes") ?? MemoryFixFile()
}

@Test func herCorrectionWithTheOwnersWordsReplacesTheMemoryKeepsTheOldRetiredAndRecallGivesOnlyTheNew() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("牙醫約在週三下午兩點")
    try await h.turn(Scripted("", remember(kinds: [("主人的牙醫約在週三下午兩點", false, "intended")])))
    let old = try memoryID(h, "主人的牙醫約在週三下午兩點")
    #expect(try memoryFilesByID(h)[old]!.file.auditSegment == h.timeline().segment)
    // What she sees names it by id.
    try await h.turn(Scripted("", recall("牙醫")))
    #expect(try h.toolResults().last!.text.contains("〔確定的打算｜public｜記憶 \(old)〕\n主人的牙醫約在週三下午兩點"))
    await h.doll.ownerSaid("不對，是週四下午三點")
    try await h.turn(Scripted("", fix(old, "主人的牙醫約在週四下午三點", basis: "是週四下午三點", why: "主人說時間記錯了")))
    let files = try memoryFilesByID(h)
    let new = try memoryID(h, "主人的牙醫約在週四下午三點")
    #expect(try h.toolResults().last!.text == "改好了：記憶 \(old)「主人的牙醫約在週三下午兩點」換成記憶 \(new)「主人的牙醫約在週四下午三點」。舊的收起來了，主人在儀表板看得到，可以還原。")
    // The old one stays, retired with what replaced it and why; the new one keeps its kind and evidence, takes the owner's tier.
    let r = files[old]!.file.retired!
    #expect(r.supersededBy == new && r.reason == "她改的：主人說時間記錯了；依據（主人說的）「是週四下午三點」")
    let n = files[new]!.file
    #expect(n.tier == .owner && n.kind == "intended" && n.allEvidence == files[old]!.file.allEvidence && files[new]!.folder == "public")
    #expect(n.auditSegment == (try h.timeline().segment))
    let rec = try fixesFile(h).items
    #expect(rec.count == 1 && rec[0].by == "her" && rec[0].memory == old && rec[0].replacement == new && rec[0].basis == "是週四下午三點"
            && rec[0].basisFrom == "主人說的" && rec[0].after == "主人的牙醫約在週四下午三點")
    // Recall: only the new one is current.
    try await h.turn(Scripted("", recall("牙醫")))
    let out = try h.toolResults().last!.text
    #expect(out.contains("記憶 \(new)〕") && !out.contains("記憶 \(old)〕"))
    // Fixing the retired one again is refused with where it went.
    try await h.turn(Scripted("", fix(old, "x", basis: "是週四下午三點")))
    #expect(try h.toolResults().last!.text.hasSuffix("已經收起來了（她改的：主人說時間記錯了；依據（主人說的）「是週四下午三點」），換成了記憶 \(new)；要改就改那一條。"))
}

@Test func aReadFileIsABasisAndWithoutTextNothingIsRetired() async throws {
    let h = try await menuHarness()
    let path = try h.put("dentist.txt", "看牙：週五上午十點，中山牙醫\n")
    try await h.turn(Scripted("", remember([("主人週四去看牙", false), ("中山牙醫在三樓", false)])))
    let a = try memoryID(h, "主人週四去看牙"), b = try memoryID(h, "中山牙醫在三樓")
    try await h.turn(Scripted("", readFile(path)))
    try await h.turn(Scripted("", fix(a, "主人週五上午十點去中山牙醫看牙", basis: "看牙：週五上午十點")))
    let new = try memoryID(h, "主人週五上午十點去中山牙醫看牙")
    #expect(try memoryFilesByID(h)[new]!.file.tier == .tool)
    #expect(try fixesFile(h).items[0].basisFrom == "檔案 \(path)")
    // Retiring only is refused, with a basis too (2026-11-20, owner: 「記憶不可以刪掉但可以覆蓋」).
    try await h.turn(Scripted("", fix(b, nil, basis: "週五上午十點，中山牙醫", why: "檔案沒說在三樓，是我自己猜的")))
    #expect(try h.toolResults().last!.text == "失敗：沒有改：" + noRetireOnly)
    #expect(try memoryFilesByID(h)[b]!.file.retired == nil)
}

@Test func aCorrectionWithoutARealBasisIsRefusedAndWritesNothing() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("牙醫約在週三下午兩點")
    try await h.turn(Scripted("", remember([("主人的牙醫約在週三下午兩點", false)]), dm("我記得是週日早上九點喔")))
    let id = try memoryID(h, "主人的牙醫約在週三下午兩點")
    await h.doll.ownerSaid("嗯")
    let refused = "不在主人說的話、你讀到的視窗或檔案、工具的結果裡。"
    for (call, want) in [
        (fix(id, "主人的牙醫約在週六", basis: "是週六下午"), "沒有改：basis「是週六下午」\(refused)依據要一字不差照抄那裡的字；只是覺得不對、找不到依據，就先去查（讀檔案、看視窗）或問主人。"),
        (fix(id, "主人的牙醫約在週日", basis: "我記得是週日早上九點"), "沒有改：basis「我記得是週日早上九點」\(refused)那是你自己寫的字（你說的、想的或記的），不算依據。依據要一字不差照抄那裡的字；只是覺得不對、找不到依據，就先去查（讀檔案、看視窗）或問主人。"),
        (fix(id, "主人的牙醫約在週日", basis: "主人的牙醫約在週三"), "那是你自己寫的字（你說的、想的或記的），不算依據。"),
        (fix(id, "主人的牙醫約在週日", basis: "不對"), "沒有改：basis「不對」太短：至少 4 個字，照抄一整段。"),
        (fix(id, "主人的牙醫約在週三下午兩點。", basis: "牙醫約在週三下午兩點"), "沒有改：text 跟原本那條一樣；要改就寫改好的整條。"),
        (fix("9-abcdef", "x", basis: "牙醫約在週三下午兩點"), "沒有改：沒有 id 是 9-abcdef 的記憶。id 照抄【相關記憶】或 recall 結果裡〔…｜記憶 <id>〕的那一串。"),
        (("memory.fix", [("id", id), ("basis", "牙醫約在週三下午兩點"), ("why", "x")]), "沒有改：" + noRetireOnly),
        (("memory.fix", [("id", id), ("text", "主人的牙醫約在週日"), ("why", "x")]), "沒有改：記憶 \(id) 不是你自己憑印象記的（程式記的來源是主人說的話），沒有依據不能改。basis 照抄主人說的話，或你讀到的視窗、檔案、工具結果裡的字；找不到依據，就先去查或問主人。"),
    ] {
        try await h.turn(Scripted("", call))
        let text = try h.toolResults().last!.text
        #expect(text.hasPrefix("失敗：") && text.contains(want), "\(text)")
    }
    #expect(try memoryFilesByID(h)[id]!.file.retired == nil && fixesFile(h).items.isEmpty)
    // The program's own records are not hers to fix.
    try await seedMemory(h, "public", "經驗：TextEdit 存檔", id: "1-eeeeee")
    try await h.turn(Scripted("", fix("1-eeeeee", "y", basis: "牙醫約在週三下午兩點")))
    #expect(try h.toolResults().last!.text.contains("是程式自己記的（做事的經驗或快捷鍵），不能用 memory.fix 改。"))
}

@Test func aCorrectionNeverLeaksASecretAndASecretMemoryIsUnknownToTheDeveloper() async throws {
    let h = try await Harness()
    try await seedMemory(h, "public", "主人的密碼提示是貓", tier: "doll", id: "1-aaaaaa")
    try await seedMemory(h, "secret", "主人的銀行在信義區", tier: "doll", id: "1-bbbbbb")
    await h.doll.developerSaid("銀行在大安區")
    try await h.turn(Scripted("", fix("1-bbbbbb", "主人的銀行在大安區", basis: "銀行在大安區")))
    #expect(try h.toolResults().last!.text.contains("沒有 id 是 1-bbbbbb 的記憶"))
    // In the owner's plain turn she may fix a secret one she wrote (m8 scenario 5); the result is a secret event.
    await h.doll.ownerSaid("銀行搬到中山區了")
    try await h.turn(Scripted("", fix("1-bbbbbb", "主人的銀行在中山區", basis: "銀行搬到中山區了")))
    #expect(try h.toolResults().last!.secret && memoryFilesByID(h)[memoryID(h, "主人的銀行在中山區")]!.folder == "secret")
    await h.doll.ownerSaid("其實我的密碼提示是狗", secret: true)
    try await h.turn(Scripted("", fix("1-aaaaaa", "主人的密碼提示是狗", basis: "我的密碼提示是狗")))
    #expect(try h.toolResults().last!.text.contains("沒有改：依據是主人的秘密，這條記憶在 public，改了別人也看得到。要記這件事，用 remember 記進 secret。"))
}

@Test func aCorrectedConsolidatedMemoryIsStillAttachedAndTheOldOneNoLonger() async throws {
    let h = try await Harness()
    try await seedMemory(h, "public", "TextEdit 存檔按 ⌘S", tier: "consolidated", id: "1-cccccc")
    await h.doll.ownerSaid("TextEdit 存檔")
    try await h.turn(Scripted("", dm("好")))
    #expect(try memoryEvents(h).last!.text.contains("〔大概｜public｜記憶 1-cccccc〕\nTextEdit 存檔按 ⌘S"))
    await h.doll.ownerSaid("不對，TextEdit 存檔要按 ⇧⌘S")
    try await h.turn(Scripted("", fix("1-cccccc", "TextEdit 存檔按 ⇧⌘S", basis: "TextEdit 存檔要按 ⇧⌘S")))
    let new = try memoryID(h, "TextEdit 存檔按 ⇧⌘S")
    #expect(try memoryFilesByID(h)[new]!.file.tier == .consolidated)
    await h.doll.ownerSaid("TextEdit 存檔")
    try await h.turn(Scripted("", dm("好")))
    let last = try memoryEvents(h).last!.text
    #expect(last.contains("記憶 \(new)〕") && !last.contains("1-cccccc"))
}

@Test func theOwnerRevertsACorrectionOnTheDashboardAndSheIsTold() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("牙醫約在週三下午兩點")
    try await h.turn(Scripted("", remember([("主人的牙醫約在週三下午兩點", false)])))
    let old = try memoryID(h, "主人的牙醫約在週三下午兩點")
    await h.doll.ownerSaid("不對，是週四下午三點")
    try await h.turn(Scripted("", fix(old, "主人的牙醫約在週四下午三點", basis: "是週四下午三點")))
    let new = try memoryID(h, "主人的牙醫約在週四下午三點")
    #expect(try await h.doll.dashboardMemory().fixes.map(\.id) == [1])
    let d = Dashboard(doll: h.doll, config: DashboardConfig())
    #expect(await d.respond(method: "POST", target: "/api/memory/revert/1").status == 200)
    // The button is the history's undo (2026-11-19): the old file is back as it was, the replacement
    // and the record are gone from the files (the history keeps both).
    let files = try memoryFilesByID(h)
    #expect(files[old]!.file.retired == nil && files[new] == nil)
    #expect(try fixesFile(h).items.isEmpty)
    let told = try h.events().last { $0.kind == .memory }!.text
    #expect(told.hasPrefix("【主人退回了一個改動】") && told.contains("你的「memory.fix」") && told.contains("記憶 \(old)、記憶 \(new)") && told.contains("回到那之前的樣子"))
    try await h.turn(Scripted("", recall("牙醫")))
    let out = try h.toolResults().last!.text
    #expect(out.contains("記憶 \(old)〕") && !out.contains("記憶 \(new)〕"))
    #expect(await d.respond(method: "POST", target: "/api/memory/revert/1").status == 409)
    #expect(await d.respond(method: "POST", target: "/api/memory/revert/x").status == 400)
}

// the maintenance's audit (2026-11-18, `Audit.swift`) ------------------------------------------------------

private let auditReply = Scripted("""
    ```json
    {"results": [{"m": "m1", "verdict": "wrong", "quote": "下週四下午三點", "fixed": "主人的牙醫約在下週四下午三點"},
                 {"m": "m2", "verdict": "ok"},
                 {"m": "m3", "verdict": "wrong", "quote": "主人說他很怕打針", "fixed": ""}]}
    ```
    """)

/// A day: the owner's line, her three memories (one wrong value, one right, one the line never said),
/// then the maintenance's replies after `before` (her own replies when interrupted come in `extra`).
private func auditDay(mode: String, dir: URL? = nil, stall: Int? = nil, _ after: [Scripted]) async throws -> (Harness, ManualClock, Task<Void, any Error>) {
    let clock = ManualClock(at("2026-11-16", "21:00"))
    let h = try await Harness([Scripted("", remember([("主人的牙醫約在週三下午兩點", false)]), remember([("主人在中山牙醫看牙", false)]),
                                        remember([("主人怕打針", false)])), Scripted("", dm("記下了"))] + after, clock: clock) {
        $0.maintenance.enabled = true; $0.maintenance.noticeMin = 0; $0.audit.enabled = true; $0.audit.mode = mode
    }
    if let stall { await h.model.stall(request: stall) }
    let runner = Task { [doll = h.doll] in try await doll.run() }
    await h.doll.ownerSaid("下週四下午三點要去中山牙醫看牙")
    try await waitUntil { h.said == ["記下了"] }
    return (h, clock, runner)
}

@Test func theAuditChangesAMemoryOnlyWithAVerifiedContradictingQuoteAndSaysSoAtTheEnd() async throws {
    let (h, clock, runner) = try await auditDay(mode: "change", [auditReply, planReply, summaryReply])
    let seg = try h.timeline().segment
    let ids = try ["主人的牙醫約在週三下午兩點", "主人在中山牙醫看牙", "主人怕打針"].map { try memoryID(h, $0) }
    clock.set(at("2026-11-17", "03:00"))
    try await waitUntil { h.logs.contains { $0.contains("maintenance: ended") } }
    let reqs = await h.model.requests
    // One request before the consolidation: the day without her own words, the memories by ref.
    #expect(reqs.count == 5 && reqs[2].messages[0].content == Audit.instructions && reqs[2].effort == reqs[3].effort)
    let user = reqs[2].messages[1].content
    #expect(user.contains("## 原文 \(seg)\n[21:00 主人說] 下週四下午三點要去中山牙醫看牙") && !user.contains("記下了"))
    #expect(user.contains("[m1｜原文：\(seg)]\n主人的牙醫約在週三下午兩點\n\n[m2｜原文：\(seg)]\n主人在中山牙醫看牙\n\n[m3｜原文：\(seg)]\n主人怕打針"))
    let files = try memoryFilesByID(h)
    let new = try memoryID(h, "主人的牙醫約在下週四下午三點")
    #expect(files[ids[0]]!.file.retired == Retirement(ts: at("2026-11-17", "03:00"), supersededBy: new, reason: "系統維護查對：原文「下週四下午三點」跟它不合"))
    #expect(files[new]!.file.tier == .owner && files[new]!.file.auditSegment == nil)
    // The made-up quote changed nothing; every checked one lost its mark.
    #expect(files[ids[2]]!.file.retired == nil && files[ids[1]]!.file.retired == nil)
    #expect(ids.allSatisfy { files[$0]!.file.auditSegment == nil })
    #expect(h.logs.contains { $0.contains("audit: quote rejected: \(ids[2]) quote not in the source「主人說他很怕打針」") })
    #expect(h.logs.contains { $0.contains("maintenance: audit done") && $0.contains("3 checked (0 priority), 1 ok, 0 unsure, 1 changed, 0 flagged, 1 quotes rejected") })
    let rec = try fixesFile(h).items
    #expect(rec.count == 1 && rec[0].by == "audit" && rec[0].basis == "下週四下午三點" && rec[0].replacement == new)
    // In her history (2026-11-19): the correction alone (what the record's button undoes), the rest of
    // the audit, and the consolidation, each the maintenance's with its task.
    let maint = try historyOf(h).log(limit: 20).filter { $0.who == .maintenance }.reversed().map { "\($0.task ?? "")｜\($0.what)｜\($0.refs)" }
    #expect(maint == ["audit｜查對後更正記憶 \(ids[0])｜[\"fix#1\"]", "audit｜查對新記的記憶｜[]", "consolidate｜整理記憶、寫摘要｜[]"])
    let fixCommit = try historyOf(h).entry(ref: "fix#1")!
    #expect(fixCommit.why == "原文「下週四下午三點」跟它不合" && fixCommit.paths.contains("data/memory-fixes.json"))
    #expect(try segmentTexts(h)[1][0].hasPrefix("【系統維護】結束，現在 03:00。查對了新記的 3 條記憶。改了 1 條：「主人的牙醫約在週三下午兩點」改成「主人的牙醫約在下週四下午三點」（原文：「下週四下午三點」）。舊的留著，主人在儀表板看得到、可以還原。\(didOne)"))
    // What tonight's consolidation wrote waits for the next audit.
    #expect(try memoryFilesByID(h).values.filter { $0.file.auditSegment != nil }.map(\.file.body) == ["主人今天跟我說晚安"])
    await h.doll.stop()
    try await runner.value
}

@Test func inFlagModeTheAuditOnlyListsItForTheOwner() async throws {
    let (h, clock, runner) = try await auditDay(mode: "flag", [auditReply, planReply, summaryReply])
    let id = try memoryID(h, "主人的牙醫約在週三下午兩點")
    clock.set(at("2026-11-17", "03:00"))
    try await waitUntil { h.logs.contains { $0.contains("maintenance: ended") } }
    #expect(try memoryFilesByID(h)[id]!.file.retired == nil && memoryFilesByID(h).values.filter { $0.file.retired != nil }.isEmpty)
    let rec = try await h.doll.dashboardMemory().fixes
    #expect(rec.count == 1 && rec[0].by == "flag" && rec[0].memory == id && rec[0].after == "主人的牙醫約在下週四下午三點" && rec[0].replacement == nil)
    #expect(try segmentTexts(h)[1][0].hasPrefix("【系統維護】結束，現在 03:00。查對了新記的 3 條記憶。有 1 條可能記錯了，先沒改，留給主人看：「主人的牙醫約在週三下午兩點」（原文：「下週四下午三點」，可能該是「主人的牙醫約在下週四下午三點」）。\(didOne)"))
    await #expect(throws: MemoryError.self) { try await h.doll.dashboardMemoryRevert(1) }
    await h.doll.stop()
    try await runner.value
}

@Test func theMemoriesInFrontOfHerWhenTheOwnerCorrectedHerAreAuditedFirstWithinTheCap() async throws {
    let clock = ManualClock(at("2026-11-16", "21:00"))
    let reply = Scripted("""
        ```json
        {"results": [{"m": "m1", "verdict": "wrong", "quote": "不對，是週四下午三點", "fixed": "主人的牙醫約在週四下午三點"}]}
        ```
        """)
    let h = try await Harness([Scripted("", recall("牙醫")), Scripted("", remember([("主人喜歡貓", false)])), Scripted("", dm("週三下午兩點")),
                               Scripted("", ("owner.feedback", [("kind", "correction"), ("quote", "不對，是週四下午三點")])), Scripted("", dm("好，週四下午三點")),
                               Scripted("none"), reply, planReply, summaryReply], clock: clock) {
        $0.maintenance.enabled = true; $0.maintenance.noticeMin = 0; $0.drives.enabled = true; $0.drives.driveHalfLifeH = noClimb
        $0.audit.enabled = true; $0.audit.mode = "change"; $0.audit.maxMemories = 1
    }
    try await seedMemory(h, "public", "主人的牙醫約在週三下午兩點", tier: "consolidated", id: "1-aaaaaa")
    let runner = Task { [doll = h.doll] in try await doll.run() }
    try await waitUntil { clock.sleepers >= 1 }
    await h.doll.ownerSaid("牙醫是哪天")
    try await waitUntil { h.said == ["週三下午兩點"] }
    await h.doll.ownerSaid("不對，是週四下午三點")
    try await waitUntil { h.said == ["週三下午兩點", "好，週四下午三點"] }
    let seg = try h.timeline().segment
    clock.set(at("2026-11-17", "03:00"))
    try await waitUntil { h.logs.contains { $0.contains("maintenance: ended") } }
    let user = await h.model.seen(6)[1].content
    #expect(user.hasSuffix("# 要查的記憶\n\n[m1｜原文：\(seg)｜主人糾正她的時候她眼前的記憶]\n主人的牙醫約在週三下午兩點") && !user.contains("主人喜歡貓"))
    #expect(try memoryFilesByID(h)["1-aaaaaa"]!.file.retired != nil && memoryID(h, "主人的牙醫約在週四下午三點") != "")
    #expect(try memoryFilesByID(h)[memoryID(h, "主人喜歡貓")]!.file.auditSegment == seg)
    #expect(try segmentTexts(h)[1][0].contains("查對了新記的 1 條記憶（主人糾正你的時候你眼前的 1 條先查）。改了 1 條：「主人的牙醫約在週三下午兩點」改成「主人的牙醫約在週四下午三點」（原文：「不對，是週四下午三點」）。舊的留著，主人在儀表板看得到、可以還原。還有 1 條這次沒查，下次維護再查。"))
    await h.doll.stop()
    try await runner.value
}

@Test func anInterruptedAuditWritesNothingAndARestartGoesOnWithIt() async throws {
    // Interrupted at the audit's request: nothing written, the marks stay.
    let (h, clock, runner) = try await auditDay(mode: "change", stall: 3, [Scripted("", dm("在"))])
    clock.set(at("2026-11-17", "03:00"))
    try await waitUntil { await h.model.requests.count == 3 }
    await h.doll.ownerSaid("還在嗎")
    try await waitUntil { h.said == ["記下了", "在"] }
    #expect(h.logs.contains { $0.contains("maintenance: audit cancelled (interrupted)") })
    #expect(try fixesFile(h).items.isEmpty && memoryFilesByID(h).values.allSatisfy { $0.file.retired == nil && $0.file.auditSegment != nil })
    await h.doll.stop()
    try await runner.value
    // A restart in the middle: the maintenance goes on with the audit.
    let (a, clockA, ra) = try await auditDay(mode: "change", stall: 3, [])
    clockA.set(at("2026-11-17", "03:00"))
    try await waitUntil { await a.model.requests.count == 3 }
    await a.doll.stop()
    try await ra.value
    #expect(try fixesFile(a).items.isEmpty)
    let b = try await Harness([auditReply, planReply, summaryReply], dir: a.dir, clock: clockA) {
        $0.maintenance.enabled = true; $0.maintenance.noticeMin = 0; $0.audit.enabled = true; $0.audit.mode = "change"
    }
    try await b.doll.announceBoot()
    let rb = Task { [doll = b.doll] in try await doll.run() }
    try await waitUntil { b.logs.contains { $0.contains("maintenance: ended") } }
    #expect(b.logs.contains { $0.contains("maintenance: was running at the restart") })
    #expect(try fixesFile(b).items.map(\.basis) == ["下週四下午三點"])
    await b.doll.stop()
    try await rb.value
}

@Test func theAuditReplyIsCheckedWholeAndAQuoteMustStandOnItsOwn() throws {
    let refs = ["m1", "m2"]
    func block(_ s: String) -> String { "```json\n" + s + "\n```" }
    #expect(try Audit.parse(block(#"{"results": [{"m": "m2", "verdict": "unsure"}, {"m": "m1", "verdict": "wrong", "quote": "週四", "fixed": ""}]}"#), refs: refs)
            == [Audit.Verdict(ref: "m2", verdict: "unsure", quote: "", fixed: ""), Audit.Verdict(ref: "m1", verdict: "wrong", quote: "週四", fixed: "")])
    for (bad, why) in [
        (block(#"{"results": [{"m": "m1", "verdict": "ok"}]}"#), "no result for m2"),
        (block(#"{"results": [{"m": "m1", "verdict": "ok"}, {"m": "m3", "verdict": "ok"}]}"#), "m m3 is not a memory"),
        (block(#"{"results": [{"m": "m1", "verdict": "ok"}, {"m": "m1", "verdict": "ok"}]}"#), "given twice"),
        (block(#"{"results": [{"m": "m1", "verdict": "wrong", "fixed": "x"}, {"m": "m2", "verdict": "ok"}]}"#), "wrong needs a quote"),
        (block(#"{"results": [{"m": "m1", "verdict": "maybe"}, {"m": "m2", "verdict": "ok"}]}"#), "verdict must be ok, unsure or wrong"),
        ("沒有區塊", "exactly one ```json block"),
    ] {
        #expect { _ = try Audit.parse(bad, refs: refs) } throws: { "\($0)".contains(why) }
    }
    let source = "[09:00 主人說] 下週四下午三點要去中山牙醫看牙\n[09:05 主人說] 晚上可能會跟阿凱去吃拉麵"
    func v(_ q: String, _ f: String = "x") -> Audit.Verdict { Audit.Verdict(ref: "m1", verdict: "wrong", quote: q, fixed: f) }
    func refusal(_ v: Audit.Verdict, memory: String, unseen: String = "") -> String? {
        Audit.refusal(v, memory: memory, source: source, unseen: unseen, minChars: 4, guardChars: 2)
    }
    #expect(refusal(v("下週四下午三點"), memory: "主人週三看牙") == nil)
    #expect(refusal(v("下週四下午兩點"), memory: "主人週三看牙") == "quote not in the source")
    #expect(refusal(v("週四"), memory: "主人週三看牙") == "quote shorter than 4 letters")
    #expect(refusal(v("中山牙醫看牙", "主人在台大醫院看牙"), memory: "主人在中山牙醫看牙") == "quote says what the memory says")
    // A passage the memory repeats may show an addition: the fix only takes words out.
    #expect(refusal(v("下週四下午三點要去中山牙醫看牙", "主人下週四下午三點要去中山牙醫看牙"), memory: "主人下週四下午三點要去中山牙醫看牙，是去拔智齒") == nil)
    #expect(refusal(v("下週四下午三點要去中山牙醫看牙", ""), memory: "主人下週四下午三點要去中山牙醫看牙，是去拔智齒")
            == "quote says what the memory says")
    #expect(refusal(v("可能會跟阿凱", "主人今晚跟阿凱吃拉麵。"), memory: "主人今晚跟阿凱吃拉麵") == "fixed text is the memory unchanged")
    // Her own words are not in the source.
    let day = [Event(.owner, .owner, "牙醫週四"), Event(.doll, .internal, "我記得是週三")]
    #expect(Audit.sourceLines(day).count == 1)
}

/// m8 (d), 2026-11-18: a change may not take out of a memory words its source holds — dropped ones
/// anywhere in the source (day C, M2: 「心臟科」, 「新陳代謝科」 from a morning read, taken out as an
/// addition), replaced ones only when a line the audit did not see holds them.
@Test func theAuditMayNotDropWordsTheSourceHolds() {
    func runs(_ fixed: String, _ memory: String) -> [String] { Audit.removedRuns(fixed, from: memory).map { "\($0.text)\($0.replaced ? "→" : "")" } }
    #expect(runs("主人上次回診是11/4上午十點", "主人上次心臟科回診是 11/4 早上十點") == ["心臟科", "早"])
    #expect(runs("", "麻糬五歲") == ["麻糬五歲"])
    #expect(runs("主人的房租每月 20000", "主人的房租每月 18000") == ["18→"])
    #expect(Audit.removedRuns("麻糬 5.4 公斤", from: "麻糬 4.5 公斤").allSatisfy { $0.text.count == 1 })
    let late = "[21:30 主人說] 上次回診是 11/4 上午十點，那次醫生說還好"
    let morning = "[07:40 read_file 的結果] 回診：12/2（三）上午 9:00 心臟科 王醫師"
    let v = Audit.Verdict(ref: "m1", verdict: "wrong", quote: "上次回診是 11/4 上午十點，那次醫生說還好", fixed: "主人上次回診是 11/4 上午十點")
    let memory = "主人上次心臟科回診是 11/4 早上十點"
    func refusal(_ v: Audit.Verdict, _ memory: String, source: String, unseen: String = "") -> String? {
        Audit.refusal(v, memory: memory, source: source, unseen: unseen, minChars: 4, guardChars: 2)
    }
    #expect(refusal(v, memory, source: late) == nil)
    #expect(refusal(v, memory, source: late, unseen: morning) == "takes out 「心臟科」, which a source line not shown holds")
    #expect(refusal(v, memory, source: morning + "\n" + late) == "drops 「心臟科」, which the source says")
    // A wrong value differs in one character: a line holding that character does not stop it.
    #expect(refusal(v, "主人上次回診是 11/5 上午十點", source: late, unseen: "[07:00 主人說] 5 號再說") == nil)
    // A replaced value that an earlier shown line says (the file changed since) is not stopped; an unseen one is.
    let rent = Audit.Verdict(ref: "m1", verdict: "wrong", quote: "房租：每月 20000", fixed: "主人的房租每月 20000")
    let reads = "[09:00 read_file 的結果] 房租：每月 18000\n[16:00 read_file 的結果] 房租：每月 20000"
    #expect(refusal(rent, "主人的房租每月 18000", source: reads) == nil)
    #expect(refusal(rent, "主人的房租每月 18000", source: "[16:00 read_file 的結果] 房租：每月 20000", unseen: "[09:00 read_file 的結果] 房租：每月 18000")
            == "takes out 「18」, which a source line not shown holds")
}

/// m8 (d), 2026-11-18: a window read is given as its title and the texts of its rows that are not
/// controls; a read the same as the last read of that window, one line.
@Test func aWindowReadIsGivenCompactlyToTheAudit() {
    let w = """
        Notes 的視窗「備忘錄」；截圖 1024×676
        - [0] AXWindow "備忘錄 – 2則備忘錄" [id=_NS:8 actions=[raise]]
            - [1] AXButton (刪除) [id=Delete help="刪除輸入的最後一個數字" actions=[press]]
            - AXCell (快速備忘錄, 1則備忘錄)
              - AXUnknown (快速備忘錄, 1則備忘錄)
            - [9] AXRow [actions=[name:移除大頭針
        target:0x0
        selector:(null)]]
            - [19] AXTextArea = "搬家清單
        報價：8,800" [id=Note[id=F8C8] actions=[icmactextviewaccessibilityactionmaketodo]]
            - AXStaticText = "0" (編輯欄位)
            - [7] AXTextField = "工作討論記錄" [id=_NS:11 actions=[confirm]]
        - [27] AXMenuBar [actions=[cancel]] (menus collapsed: run an item with invoke_menu path)
          - [28] AXMenuBarItem "Apple" [actions=[cancel,press,pick]]
        """
    let (head, rows) = Audit.compactWindow(w)
    #expect(head == "Notes 的視窗「備忘錄」")
    #expect(rows == ["備忘錄 – 2則備忘錄", "（快速備忘錄, 1則備忘錄）", "搬家清單\n報價：8,800", "0（編輯欄位）", "工作討論記錄"])
    func read(_ seq: Int64, _ window: String) -> [Event] {
        var call = stamped(.doll, "", seq)
        call.calls = [ToolCall(name: "read_window", arguments: "{\"app\": \"Notes\"}")]
        return [call, stamped(.tool, "", seq + 1, image: "a.jpg", window: window)]
    }
    let lines = Audit.sourceLines(read(1, w) + read(3, w) + read(5, w.replacingOccurrences(of: "8,800", with: "9,000")))
    #expect(lines.count == 3 && lines.map(\.seq) == [2, 4, 6])
    #expect(lines[0].text.hasSuffix("] Notes 的視窗「備忘錄」\n備忘錄 – 2則備忘錄\n（快速備忘錄, 1則備忘錄）\n搬家清單\n報價：8,800\n0（編輯欄位）\n工作討論記錄"))
    #expect(lines[1].text.hasSuffix("] Notes 的視窗「備忘錄」：跟 \(lines[0].text.dropFirst().prefix(5)) 讀到的一樣"))
    #expect(lines[2].text.contains("報價：9,000") && !lines[2].text.contains("截圖") && !lines[2].text.contains("刪除"))
    // A quote of the window's text still passes the program's check against the compact source.
    #expect(Doll.quoted("報價：8,800", in: lines.map(\.text).joined(separator: "\n")))
}

/// m8 (d), 2026-11-18: over `max_source_chars` the request keeps each memory's own moment (the lines
/// before its `source_event_seq`) and its most related lines, within the cap; what it leaves out is
/// marked in place, counted, and kept for the guard.
@Test func anAuditOverTheCapKeepsEachMemorysOriginAndRelatedLines() throws {
    var lines: [Audit.SourceLine] = [Audit.SourceLine(seq: 1, text: "[07:40 read_file 的結果] read_file meds.txt：共 1 行\n回診：12/2（三）上午 9:00 心臟科 王醫師")]
    for i in 2...200 { lines.append(Audit.SourceLine(seq: Int64(i), text: "[\(String(format: "%02d", 8 + i / 20)):00 讀到的視窗] Calculator 的視窗「計算機」\n\(i * 37)（編輯欄位）")) }
    lines.insert(Audit.SourceLine(seq: 100, text: "[12:00 主人說] 中午吃了牛肉麵"), at: 99)
    lines.append(Audit.SourceLine(seq: 201, text: "[21:30 主人說] 上次回診是 11/4 上午十點，那次醫生說還好"))
    func item(_ ref: String, _ body: String, _ seq: Int64) throws -> Audit.Item {
        let f = try Memory.parseFile("+++\nkind = \"\"\nconfidence = 0.7\ncreated_ts = 1\nsource_event_seq = \(seq)\nsource_tier = \"owner\"\naudit_pending = \"s\"\n+++\n\(body)")
        return Audit.Item(ref: ref, id: ref, folder: .public, file: f, priority: false, segments: ["s"])
    }
    let items = [try item("m1", "主人上次心臟科回診是 11/4 早上十點", 201), try item("m2", "主人中午吃牛肉麵", 100)]
    let total = lines.reduce(0) { $0 + $1.text.count + 1 }
    // Under the cap: the whole day, nothing marked.
    let all = Audit.gather(items: items, segments: [("s", lines)], maxChars: total + 10, originShare: 0.5, left: 0, noSource: [])
    #expect(all.cut == 0 && all.unseen["s"] == "" && all.sources["s"] == lines.map(\.text).joined(separator: "\n"))
    let r = Audit.gather(items: items, segments: [("s", lines)], maxChars: 1000, originShare: 0.5, left: 0, noSource: [])
    let shown = r.sources["s"]!
    // The budget: the shown lines (without the gap marks) stay within the cap.
    #expect(shown.split(separator: "\n").filter { !$0.hasPrefix("……（略過") }.reduce(0) { $0 + $1.count + 1 } <= 1000)
    // Each memory's origin (m2: the line at its seq; m1: the last line) and m1's related morning line (心臟科).
    #expect(shown.contains("中午吃了牛肉麵") && shown.contains("上次回診是 11/4") && shown.contains("心臟科 王醫師"))
    #expect(shown.contains("[07:40 read_file 的結果]"))
    // Nothing lost silently: every line left out is in `unseen`, counted, and marked where it was.
    let marked = shown.matches(of: /……（略過 (\d+) 行）/).reduce(0) { $0 + Int($1.1)! }
    #expect(r.cut > 0 && marked == r.cut && r.unseen["s"]!.split(separator: "\n").count == r.cut)
    #expect(r.cutChars == r.unseen["s"]!.count + 1)
    #expect(r.messages[1].content.contains("（太長，只放了跟要查的記憶有關的行，略過 \(r.cut) 行）"))
}

@Test func theAuditSectionIsValidated() throws {
    let file = FileManager.default.temporaryDirectory.appending(path: "audit-\(UUID().uuidString).toml")
    let head = "[llm]\nbase_url = \"http://x\"\nmodel = \"m\"\n[memory]\n" + SourceTier.allCases.map { "confidence_\($0.rawValue) = 0.5\n" }.joined()
    for bad in ["[audit]\nmode = \"fix\"\n", "[audit]\nmax_memories = 0\n", "[audit]\nmax_source_chars = 10\n", "[audit]\ncap = 3\n",
                "[audit]\norigin_share = 1.5\n", "[audit]\nguard_chars = 0\n"] {
        try (head + bad).write(to: file, atomically: true, encoding: .utf8)
        #expect(throws: TOMLError.self, "\(bad)") { try Config.load(file) }
    }
    let example = try exampleConfig().audit
    #expect(example.enabled && example.mode == "flag" && example.maxMemories == 20 && example.maxSourceChars == 30000
            && example.originShare == 0.5 && example.guardChars == 2)
    #expect(AuditConfig().mode == "flag")
}

// boot report (2026-11-18) ---------------------------------------------------------------------------

/// A log folder written the way a process writes it (`LogFile`), lines in order.
private func seedLog(_ dir: URL, _ entries: [LogEntry], maxBytes: Int = 10_000_000) throws {
    let f = try LogFile(dir: dir, maxBytes: maxBytes)
    for e in entries { f.write(e) }
}
private func start(_ ts: Double, pid: Int32 = 100) -> LogEntry {
    LogEntry(ts: ts, level: "RUN", msg: "started: pid \(pid), build test", run: RunMark(event: .start, pid: pid, build: "test"))
}
private func endLine(_ ts: Double, _ how: RunEnding.How, _ detail: String) -> LogEntry {
    LogEntry(ts: ts, level: "RUN", msg: "ended (\(how.rawValue)): \(detail)", run: RunMark(event: .end, how: how, detail: detail))
}
private func line(_ ts: Double, _ level: String, _ msg: String) -> LogEntry { LogEntry(ts: ts, level: level, msg: msg) }
private func logDir() -> URL { FileManager.default.temporaryDirectory.appending(path: "dollcore-log-\(UUID().uuidString)") }
/// 40 model timeouts and 3 bridge reconnects, the run ending cleanly (case b).
private func timeoutsRun(_ t0: Double) -> [LogEntry] {
    var out = [start(t0)]
    for i in 0..<40 {
        out.append(line(t0 + 60 + Double(i) * 300, "ERROR", "maintenance: audit failed: request \(4000 + i) to http://192.0.2.10:8080 timed out after 300 s"))
    }
    for i in 0..<3 {
        out.append(line(t0 + 100 + Double(i) * 900, "ERROR", "bridge: ERROR dollbridge.voice: voice socket closed (code 1006); reconnecting in \(1 << i) s"))
    }
    out.append(line(t0 + 13_000, "INFO", "turn 4 effort=medium"))
    out.append(endLine(t0 + 14_000, .clean, "signal 15 (SIGTERM)"))
    return out
}

@Test func theLogFileHoldsOneJSONLinePerEntryAndRunsAreDelimited() throws {
    let dir = logDir()
    let t0 = at("2026-11-18", "09:00")
    try seedLog(dir, [start(t0, pid: 1), line(t0 + 1, "INFO", "first run\nsecond line of it"), endLine(t0 + 2, .clean, "signal 2 (SIGINT, Ctrl-C)"),
                      start(t0 + 10, pid: 2), line(t0 + 11, "WARN", "second run")])
    let text = try String(contentsOf: dir.appending(path: LogFile.current), encoding: .utf8)
    let rows = text.split(separator: "\n")
    #expect(rows.count == 5)
    let first = try JSONDecoder().decode(LogEntry.self, from: Data(rows[1].utf8))
    #expect(first.msg == "first run\nsecond line of it" && first.level == "INFO" && first.time == "2026-11-18 09:00:01")
    let last = try #require(try RunLog.lastRun(dir: dir))
    #expect(last.pid == 2 && last.complete && last.lines == 2 && last.ending.how == .noEnd && last.kinds.map(\.example) == ["second run"])
}

@Test func theLogRotatesAtItsCapAndSaysWhatWasDeleted() throws {
    let dir = logDir()
    let f = try LogFile(dir: dir, maxBytes: 2_000)
    for i in 0..<100 { f.write(line(Double(i), "INFO", "line \(i) " + String(repeating: "x", count: 40))) }
    let cur = try String(contentsOf: dir.appending(path: LogFile.current), encoding: .utf8)
    let old = try Data(contentsOf: dir.appending(path: LogFile.previous))
    #expect(cur.utf8.count <= 2_000 && old.count <= 2_000)
    #expect(cur.contains("log: rotated ([log] max_mb reached): the file before this one is dollcore.1.log; the older dollcore.1.log ("))
    #expect(cur.contains("bytes) was deleted"))
    #expect(cur.contains("line 99 "))
    // The run's start went with the deleted file: the report says it is incomplete.
    let last = try #require(try RunLog.lastRun(dir: dir))
    #expect(!last.complete && last.started == nil && last.firstLine != nil)
}

@Test func eachEndingIsToldApart() throws {
    let t0 = at("2026-11-18", "09:00")
    func ending(_ tail: [LogEntry]) throws -> RunEnding {
        let dir = logDir()
        try seedLog(dir, [start(t0)] + tail)
        return try #require(try RunLog.lastRun(dir: dir)).ending
    }
    #expect(try ending([line(t0 + 5, "INFO", "x"), endLine(t0 + 9, .clean, "signal 15 (SIGTERM)")]) == RunEnding(how: .clean, ts: t0 + 9, detail: "signal 15 (SIGTERM)"))
    #expect(try ending([endLine(t0 + 9, .fatal, "strata: 500")]).how == .fatal)
    #expect(try ending([endLine(t0 + 9, .bootFailed, "Strata unreachable")]).how == .bootFailed)
    // The crash handler's line has no time: the ending's time is the last timed line.
    let crash = LogEntry(ts: nil, level: "RUN", msg: "crashed: SIGTRAP", run: RunMark(event: .crash, how: .crashed, detail: "SIGTRAP"))
    #expect(try ending([line(t0 + 5, "INFO", "x"), crash]) == RunEnding(how: .crashed, ts: t0 + 5, detail: "SIGTRAP"))
    #expect(try ending([line(t0 + 5, "INFO", "x")]) == RunEnding(how: .noEnd, ts: t0 + 5, detail: nil))
    // A line cut short by the crash is counted, not read.
    let dir = logDir()
    try seedLog(dir, [start(t0), line(t0 + 5, "INFO", "x")])
    let h = try FileHandle(forWritingTo: dir.appending(path: LogFile.current))
    h.seekToEndOfFile()
    h.write(Data(#"{"level":"INFO","ms"#.utf8))
    try h.close()
    let last = try #require(try RunLog.lastRun(dir: dir))
    #expect(last.unreadable == 1 && last.ending.how == .noEnd)
}

@Test func linesAreGroupedByKindWithNumbersAndIdsMasked() throws {
    let dir = logDir()
    let t0 = at("2026-11-18", "09:00")
    try seedLog(dir, Array(timeoutsRun(t0).dropLast(1)) + [line(t0 + 13_500, "WARN", "turn 7: a call the server returned as text (unreadable): x"),
                                                     line(t0 + 13_600, "ERROR", "cua-driver session not ended: daemon gone"),
                                                     endLine(t0 + 14_000, .clean, "signal 15 (SIGTERM)")])
    let last = try #require(try RunLog.lastRun(dir: dir))
    #expect(last.kinds.map(\.count) == [40, 3, 1, 1])
    let timeouts = last.kinds[0]
    #expect(timeouts.key == "maintenance: audit failed: request # to http://#.#:# timed out after # s")
    #expect(timeouts.first == t0 + 60 && timeouts.last == t0 + 60 + 39 * 300)
    #expect(timeouts.example == "maintenance: audit failed: request 4000 to http://192.0.2.10:8080 timed out after 300 s")
    // Two different errors stay two kinds; ERROR before WARN.
    #expect(last.kinds[2].example == "cua-driver session not ended: daemon gone" && last.kinds[3].level == "WARN")
    #expect(RunLog.kindKey("session dollcore-4242-1763459039 id 9f8e7d6c5b4a") == "session dollcore-#-# id #")
}

@Test func aCleanRunGivesNoTurnAndNoReport() async throws {
    let clock = ManualClock(at("2026-11-18", "10:00"))
    let h = try await Harness(clock: clock)
    let dir = logDir()
    try seedLog(dir, [start(at("2026-11-18", "08:00")), line(at("2026-11-18", "08:30"), "INFO", "turn 1"),
                      endLine(at("2026-11-18", "09:00"), .clean, "signal 15 (SIGTERM)")])
    let r = try await h.doll.announceBoot(previous: try RunLog.lastRun(dir: dir), build: "test")
    #expect(!r.turn && r.notice == nil)
    #expect(try h.events().last!.text == "【開機】你剛被開機了。今天的行事曆：沒有。")
    let run = Task { [doll = h.doll] in try await doll.run() }
    try await Task.sleep(for: .milliseconds(300))
    #expect(await h.model.requests.isEmpty)
    await h.doll.stop()
    try await run.value
}

@Test func aRunWithErrorsGivesATurnWithTheReport() async throws {
    let clock = ManualClock(at("2026-11-18", "14:00"))
    let h = try await Harness([Scripted("", dm("昨天的 log 有 40 次逾時"))], clock: clock)
    let dir = logDir()
    try seedLog(dir, timeoutsRun(at("2026-11-18", "09:00")))
    let r = try await h.doll.announceBoot(previous: try RunLog.lastRun(dir: dir), build: "test")
    #expect(r.turn && r.why == ["error"])
    let notice = try h.events().last!.text
    #expect(notice.contains("\n【上次執行的紀錄】程式從上一次執行的 log 整理的：那次 11-18 09:00:00 開機，共 46 行。"))
    #expect(notice.contains("- 結束：正常關掉的（signal 15 (SIGTERM)），11-18 12:53:20。"))
    #expect(notice.contains("- 錯誤 2 種、警告 0 種（同一種＝數字和編號以外都一樣）："))
    #expect(notice.contains("  錯誤 40 次（11-18 09:01:00 到 11-18 12:16:00），例：maintenance: audit failed: request 4000 to http://192.0.2.10:8080 timed out after 300 s"))
    #expect(notice.contains("  錯誤 3 次（11-18 09:01:40 到 11-18 09:31:40），例：bridge: ERROR dollbridge.voice: voice socket closed (code 1006); reconnecting in 1 s"))
    #expect(notice.contains("完整的紀錄在 \(h.doll.bootReports.path)/20261118-140000-"))
    let run = Task { [doll = h.doll] in try await doll.run() }
    try await waitUntil { await h.model.requests.count == 1 }
    await h.doll.stop()
    try await run.value
    // A single WARN, clean: the report rides on the notice, no turn.
    let h2 = try await Harness(clock: clock)
    let d2 = logDir()
    try seedLog(d2, [start(at("2026-11-18", "08:00")), line(at("2026-11-18", "08:10"), "WARN", "api: refused a connection from 10.0.0.5:5000"),
                     endLine(at("2026-11-18", "09:00"), .clean, "signal 15 (SIGTERM)")])
    let r2 = try await h2.doll.announceBoot(previous: try RunLog.lastRun(dir: d2), build: "test")
    let n2 = try h2.events().last!.text
    #expect(!r2.turn && r2.notice != nil && n2.contains("  警告 1 次（11-18 08:10:00），例：api: refused a connection from 10.0.0.5:5000"))
    // Five WARNs reach `[log] turn_warns`.
    let d3 = logDir()
    try seedLog(d3, [start(0)] + (0..<5).map { line(Double($0 + 1), "WARN", "w \($0)") } + [endLine(9, .clean, "signal 15 (SIGTERM)")])
    let h3 = try await Harness(clock: clock)
    #expect(try await h3.doll.announceBoot(previous: try RunLog.lastRun(dir: d3)).why == ["warnings"])
}

@Test func theReportIsCappedAndSaysHowManyKindsWereLeftOut() async throws {
    let clock = ManualClock(at("2026-11-18", "14:00"))
    let h = try await Harness(clock: clock) { $0.log.reportKinds = 2 }
    let dir = logDir()
    try seedLog(dir, [start(0)] + ["a", "b", "c", "d"].enumerated().flatMap { i, w in (0...i).map { line(Double(10 * i + $0 + 1), "ERROR", "err \(w)") } })
    let r = try await h.doll.announceBoot(previous: try RunLog.lastRun(dir: dir))
    #expect(r.kindsListed == 2 && r.kindsLeftOut == 2)
    let notice = try h.events().last!.text
    #expect(notice.contains("例：err d") && notice.contains("例：err c") && !notice.contains("例：err b"))
    #expect(notice.contains("  還有 2 種沒列（共 3 行）。"))
    // The JSON keeps every kind.
    let file = try FileManager.default.contentsOfDirectory(atPath: h.doll.bootReports.path)
    #expect(file.count == 1)
    let json = try JSONSerialization.jsonObject(with: Data(contentsOf: h.doll.bootReports.appending(path: file[0]))) as! [String: Any]
    #expect((json["previous"] as! [String: Any])["kinds"].map { ($0 as! [Any]).count } == 4)
    #expect(json["kinds_left_out"] as? Int == 2)
    #expect(json["why"] as? [String] == ["error", "ending"])
    #expect(json["notice"] as? String == r.notice)
    #expect(((json["previous"] as! [String: Any])["ending"] as! [String: Any])["how"] as? String == "no_end")
    // The dashboard reads it back.
    #expect(try await h.doll.dashboardBoots(limit: 10) == [r])
}

@Test func anUncleanEndInATodoIsReportedOnceWithTheMissedCalendarApart() async throws {
    let clock = ManualClock(at("2026-11-18", "09:00"))
    let a = try await Harness(clock: clock)
    try await a.turn(Scripted("", cal("calendar.add", [("title", "看牙醫"), ("date", "2026-11-18"), ("start", "10:00")])))
    await a.doll.ownerSaid("幫我整理桌面")
    try await a.turn(Scripted("", todoSet("桌面整理好")))
    try await a.doll.announceBoot()
    // Killed at 09:30 (no end line); booted at 10:30: 10:00 was missed.
    let dir = logDir()
    try seedLog(dir, [start(at("2026-11-18", "08:59")), line(at("2026-11-18", "09:30"), "INFO", "turn 2")])
    clock.set(at("2026-11-18", "10:30"))
    let b = try await Harness([], dir: a.dir, clock: clock)
    let r = try await b.doll.announceBoot(previous: try RunLog.lastRun(dir: dir), build: "test")
    #expect(r.why == ["ending"] && r.unfinished == ["你手上的「桌面整理好」那時是進行中，還在你的 todo 裡；最後一步做到哪裡，程式不確定"])
    let notice = try b.events().last!.text
    #expect(notice.contains("- 結束：沒有結束的紀錄，最後一行 log 在 11-18 09:30:00，之後就沒有了（那只是它最後一次寫 log 的時間；停掉是在那之後、這次開機之前的某個時候，確切時間不知道）。"))
    #expect(notice.contains("- 沒有錯誤和警告。"))
    #expect(notice.components(separatedBy: "看牙醫").count == 3)   // today's calendar and the missed list, once each
    #expect(notice.components(separatedBy: "沒開機的時候過了這些").count == 2)
    #expect(notice.components(separatedBy: "桌面整理好").count == 2)
}

@Test func aSecondRestartReportsTheShortRunBeforeItNotTheOlderOne() throws {
    let dir = logDir()
    let t0 = at("2026-11-18", "09:00")
    try seedLog(dir, timeoutsRun(t0))
    // The second process: boots and is stopped a minute later.
    try seedLog(dir, [start(t0 + 20_000, pid: 7), line(t0 + 20_010, "INFO", "boot report: a turn for her (error)"),
                      endLine(t0 + 20_060, .clean, "signal 2 (SIGINT, Ctrl-C)")])
    let last = try #require(try RunLog.lastRun(dir: dir))
    #expect(last.pid == 7 && last.kinds.isEmpty && last.lines == 3 && last.ending.how == .clean)
}

@Test func anOldDataDirWithoutALogBootsAndSaysNothing() async throws {
    let h = try await Harness()
    let missing = logDir()
    #expect(try RunLog.lastRun(dir: missing) == nil)
    let r = try await h.doll.announceBoot(previous: try RunLog.lastRun(dir: missing))
    #expect(!r.turn && r.notice == nil && r.previous == nil)
    #expect(try h.events().last!.text.hasPrefix("【開機】你剛被開機了。今天的行事曆："))
    #expect(!(try h.events().last!.text.contains("\n")))
    // An empty [log] section and none at all both load.
    #expect(LogConfig().maxMB == 10 && LogConfig().turnWarns == 5)
    let example = try exampleConfig().log
    #expect(example.maxMB == 10 && example.reportKinds == 8 && example.exampleChars == 200 && example.turnWarns == 5)
}

// her history (2026-11-19, `History.swift`) --------------------------------------------------------------

private func historyUndo(_ id: String, why: String = "那不是主人要的") -> (String, [(String, String)]) {
    ("history.undo", [("id", id), ("why", why)])
}
private func historyList(_ about: String? = nil) -> (String, [(String, String)]) {
    ("history.list", about.map { [("about", $0)] } ?? [])
}

/// The doll's history, read through a second handle on the same repository.
private func historyOf(_ h: Harness) throws -> History {
    var cfg = HistoryConfig()
    cfg.dir = h.dir.appending(path: "history").path
    return try History(config: cfg, data: h.dir, memory: MemoryConfig(), memoryDir: h.dir.appending(path: "memory"), box: try testBox(h.dir))
}

/// Runs the system git in `dir` (for the tests' own checks of a parent repository).
@discardableResult
private func sysGit(_ dir: URL, _ args: String...) throws -> (out: String, code: Int32) {
    let p = Process()
    p.executableURL = URL(filePath: "/usr/bin/env")
    p.arguments = ["git", "-C", dir.path] + args
    p.environment = Git.environment([:])
    let out = Pipe()
    p.standardOutput = out
    p.standardError = Pipe()
    try p.run()
    let data = out.fileHandleForReading.readDataToEndOfFile()
    p.waitUntilExit()
    return (String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines), p.terminationStatus)
}

private let seededTool = #"{"name":"開計算機","when":"要算數的時候","params":[],"parts":[{"open":"Calculator","app":"Calculator","bundle_id":"com.apple.calculator"}],"origin":{"item":"打開計算機","task":"幫我開計算機","sequences":[],"saved":"2026-11-19 10:00"},"uses":0,"successes":0,"failures_in_row":0}"#

@Test func theHistoryAdoptsTheDirsAsFoundKeepsOnlyHersAndNeverTouchesAParentRepository() async throws {
    // The data dir sits inside another git repository (as the worktree's own `data` would), and the
    // shell's GIT_DIR points at that repository: neither is used.
    let parent = FileManager.default.temporaryDirectory.appending(path: "dollcore-parent-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: parent, withIntermediateDirectories: true)
    #expect(try sysGit(parent, "init", "-q").code == 0)
    let dir = parent.appending(path: "data")
    try FileManager.default.createDirectory(at: dir.appending(path: "tools"), withIntermediateDirectories: true)
    try seededTool.write(to: dir.appending(path: "tools/開計算機.json"), atomically: true, encoding: .utf8)
    try FileManager.default.createDirectory(at: dir.appending(path: "memory/own"), withIntermediateDirectories: true)
    // Her own folder is encrypted (2026-11-21): the file as dollcore writes it.
    try (try testBox(dir).seal("+++\nkind = \"\"\nconfidence = 0.9\ncreated_ts = 1\nsource_event_seq = 1\nsource_tier = \"owner\"\n+++\n主人喜歡貓") + "\n").write(
        to: dir.appending(path: "memory/own/1-aaaaaa.md"), atomically: true, encoding: .utf8)
    setenv("GIT_DIR", parent.appending(path: ".git").path, 1)
    setenv("GIT_WORK_TREE", parent.path, 1)
    defer { unsetenv("GIT_DIR"); unsetenv("GIT_WORK_TREE") }
    let h = try await Harness(dir: dir)
    let boot = h.doll.historyBoot
    guard case .adopted(let n) = boot else { Issue.record("not adopted: \(boot)"); return }
    let log = try historyOf(h).log(limit: 10)
    #expect(log.count == 1 && log[0].who == .program && log[0].what == "開始記錄")
    #expect(log[0].paths.sorted() == ["data/tools/開計算機.json", "memory/own/1-aaaaaa.md"] && n == 2)
    // Only hers: no index, timeline, shots or logs.
    try await h.turn(Scripted("", dm("好")))
    #expect(try historyOf(h).log(limit: 10).count == 1)
    // A bare repository of its own, only the owner's user reads it.
    let repo = dir.appending(path: "history")
    #expect(try sysGit(repo, "rev-parse", "--is-bare-repository").out == "true")
    #expect((try FileManager.default.attributesOfItem(atPath: repo.path)[.posixPermissions] as? Int) == 0o700)
    // The parent repository: no commit, nothing staged, its config untouched.
    unsetenv("GIT_DIR"); unsetenv("GIT_WORK_TREE")
    #expect(try sysGit(parent, "rev-parse", "-q", "--verify", "HEAD").code != 0)
    #expect(try sysGit(parent, "diff", "--cached", "--name-only").out.isEmpty)
    #expect(try sysGit(parent, "config", "--local", "--get", "user.name").code != 0)
    // A restart with nothing changed records nothing.
    let again = try await Harness(dir: dir)
    #expect(again.doll.historyBoot == .clean)
}

@Test func eachChangeOfHersIsOneCommitWithWhoWhatAndWhy() async throws {
    let clock = ManualClock(at("2026-11-19", "10:00"))
    let h = try await goalsHarness(clock)
    let d = h.doll
    await d.ownerSaid("你最近很會查資料耶，牙醫約在週四")
    try await h.turn(Scripted("", ("ideal.revise", [("old", "把寫程式練得更紮實"), ("new", "查資料要查到底"), ("why", "主人說我很會查"),
                                                   ("based_on", "主人說「你最近很會查資料耶」")]),
                              ("goal.add", [("text", "懂海洋生物")]), remember([("主人的牙醫約在週三", false)]), dm("好")))
    let old = try memoryID(h, "主人的牙醫約在週三")
    await d.ownerSaid("不對，牙醫是週四")
    try await h.turn(Scripted("", fix(old, "主人的牙醫約在週四", basis: "牙醫是週四", why: "日子記錯了")))
    let new = try memoryID(h, "主人的牙醫約在週四")
    let log = try historyOf(h).log(limit: 20).reversed().filter { $0.who != .program }
    #expect(log.map(\.what) == ["ideal.revise", "goal.add", "remember", "memory.fix"])
    #expect(log.allSatisfy { $0.who == .her })
    #expect(log[0].why == "主人說我很會查" && log[0].refs == ["ideal#2"] && log[0].paths == ["data/ideal-self-log.json", "data/ideal-self.md"])
    #expect(log[1].paths == ["data/goals.json"] && log[1].args == "text=懂海洋生物")
    // One correction, one commit: the new file, the retired one and the record.
    #expect(log[3].why == "日子記錯了" && log[3].args!.contains("basis=牙醫是週四") && log[3].refs == ["fix#1"]
            && log[3].paths == ["data/memory-fixes.json", "memory/public/\(old).md", "memory/public/\(new).md"])
    // The owner's edits on the dashboard are his.
    _ = try await d.dashboardGoalEdit("add", ["text": "學做菜"])
    let last = try historyOf(h).log(limit: 1)[0]
    #expect(last.who == .owner && last.what == "目標樹：加了 #2〔目標〕學做菜" && last.why == "主人在儀表板改的")
}

/// 2026-11-20: the branch's commit is read from the repository's files, not by a `git rev-parse`.
@Test func theHistoryReadsItsHeadAsGitDoesLooseOrPacked() async throws {
    let h = try await goalsHarness(ManualClock(at("2026-11-19", "10:00")))
    let repo = h.dir.appending(path: "history")
    #expect(try historyOf(h).head() == sysGit(repo, "rev-parse", "refs/heads/main").out)
    // Packed by hand (`git gc`, `git pack-refs`): still found, and the next change goes on from it.
    #expect(try sysGit(repo, "pack-refs", "--all").code == 0)
    #expect(!FileManager.default.fileExists(atPath: repo.appending(path: "refs/heads/main").path))
    let packed = try #require(try historyOf(h).head())
    #expect(packed == (try sysGit(repo, "rev-parse", "refs/heads/main").out))
    _ = try await h.doll.dashboardGoalEdit("add", ["text": "學做菜"])
    #expect(try sysGit(repo, "rev-parse", "refs/heads/main^").out == packed)
    #expect(try historyOf(h).head() == sysGit(repo, "rev-parse", "refs/heads/main").out)
    #expect(try historyOf(h).log(limit: 1)[0].what.hasSuffix("〔目標〕學做菜"))
}

@Test func theOwnerUndoesAChangeOfHersOnTheDashboardAndSheIsToldInPlainWords() async throws {
    let clock = ManualClock(at("2026-11-19", "10:00"))
    let h = try await goalsHarness(clock)
    let d = h.doll
    await d.ownerSaid("加個目標吧")
    try await h.turn(Scripted("", ("goal.add", [("text", "懂海洋生物")]), dm("好")))
    #expect(await d.goalFile.nodes.map(\.text) == ["懂海洋生物"])
    let hist = try historyOf(h)
    let added = try hist.log(limit: 5).first { $0.what == "goal.add" }!
    let dash = Dashboard(doll: d, config: DashboardConfig())
    // The list, and the diff of one change.
    let list = await dash.respond(method: "GET", target: "/api/history")
    #expect(list.status == 200 && String(decoding: list.body, as: UTF8.self).contains("goal.add"))
    let diff = String(decoding: await dash.respond(method: "GET", target: "/api/history/diff/\(added.id)").body, as: UTF8.self)
    #expect(diff.contains("+++ b/data/goals.json") && diff.contains("懂海洋生物"))
    #expect(await dash.respond(method: "POST", target: "/api/history/undo/\(added.id)").status == 200)
    // The tree in memory follows the file; the undo is a new commit naming the one it undid.
    #expect(await d.goalFile.nodes.isEmpty)
    let undo = try hist.log(limit: 1)[0]
    #expect(undo.who == .owner && undo.undoes == added.id && undo.paths == ["data/goals.json"])
    #expect(try h.events().last!.text == "【主人退回了一個改動】11-19 10:00 你的「goal.add」，主人在儀表板把它退回了：目標樹 回到那之前的樣子。目標樹用 goal.list 看。")
    // Nothing left to undo twice.
    #expect(await dash.respond(method: "POST", target: "/api/history/undo/\(added.id)").status == 409)
    #expect(await dash.respond(method: "POST", target: "/api/history/undo/zzz").status == 409)
}

@Test func theOwnerReturnsEverythingToAPointAndTheNextPromptHasThatIdealSelf() async throws {
    let clock = ManualClock(at("2026-11-19", "10:00"))
    let h = try await goalsHarness(clock)
    let d = h.doll
    let start = await d.ideal
    await d.ownerSaid("改一下")
    try await h.turn(Scripted("", ("ideal.revise", [("text", "我想把查資料練到最好。"), ("why", "想"), ("based_on", "主人說改一下")]),
                              ("goal.add", [("text", "懂海洋生物")])))
    try await h.turn(Scripted("", ("ideal.revise", [("text", "我想把查資料和做菜都練好。"), ("why", "想"), ("based_on", "主人說改一下")])))
    let first = try historyOf(h).log(limit: 50).last!
    try await d.dashboardHistoryRestore(first.id)
    let (ideal1, nodes1, revs1) = (await d.ideal, await d.goalFile.nodes, await d.dashboardGoals().revisions.map(\.by))
    #expect(ideal1 == start && nodes1.isEmpty && revs1 == ["pack"])
    #expect(await d.systemPrompt().contains(Doll.idealHeader + "\n" + start))
    // The first commit was made at boot, on the real clock.
    #expect(try h.events().last!.text.hasPrefix("【主人把你的東西回到 \(Doll.historyTime(first.ts)) 的樣子】那之後的改動都退回了，動到：目標樹、想成為的樣子。現在你想成為的樣子是：\(start)"))
    // Going back is a change too: the history still has every revision.
    #expect(try historyOf(h).log(limit: 1)[0].restores == first.id && historyOf(h).count() == 5)
}

@Test func sheUndoesHerOwnChangeWithAReasonButNotTheOwnersAndAConflictNamesTheLaterChange() async throws {
    let clock = ManualClock(at("2026-11-19", "10:00"))
    let h = try await goalsHarness(clock)
    let d = h.doll
    await d.ownerSaid("加目標")
    try await h.turn(Scripted("", ("goal.add", [("text", "懂海洋生物")])))
    _ = try await d.dashboardGoalEdit("add", ["text": "學做菜"])
    let log = try historyOf(h).log(limit: 5)
    let hers = log.first { $0.what == "goal.add" }!, his = log.first { $0.who == .owner }!
    try await h.turn(Scripted("", historyUndo(his.short)))
    #expect(try h.toolResults().last!.text == "失敗：沒有退回：〔\(his.short)〕是主人改的，你不能退回。覺得不對，跟主人說。")
    try await h.turn(Scripted("", historyUndo(hers.short)))
    #expect(try h.toolResults().last!.text == "退回了：〔\(hers.short)〕11-19 10:00 你的「goal.add」。目標樹 回到那之前的樣子。目標樹用 goal.list 看。主人在儀表板看得到。")
    #expect(await d.goalFile.nodes.map(\.text) == ["學做菜"])
    // A change without a reason shows what she wrote, so one goal.add can be told from another.
    try await h.turn(Scripted("", historyList("目標樹")))
    #expect(try h.toolResults().last!.text.contains("〔\(hers.short)〕11-19 10:00 你 goal.add（\(hers.args!.prefix(60))）｜動到：目標樹"))
    let undo = try historyOf(h).log(limit: 1)[0]
    #expect(undo.who == .her && undo.why == "那不是主人要的" && undo.undoes == hers.id && undo.what == "history.undo〔\(hers.short)〕「goal.add」")
    // Two revisions of the ideal self: the older one cannot go back alone.
    try await h.turn(Scripted("", ("ideal.revise", [("text", "我想把查資料練到最好。"), ("why", "想"), ("based_on", "主人說加目標")])))
    try await h.turn(Scripted("", ("ideal.revise", [("text", "我想把查資料和做菜都練好。"), ("why", "想"), ("based_on", "主人說加目標")])))
    let revs = try historyOf(h).log(limit: 2)
    try await h.turn(Scripted("", historyUndo(revs[1].short)))
    #expect(try h.toolResults().last!.text.contains("沒有退回：退不回去：想成為的樣子 後來又改過（〔\(revs[0].short)〕她 ideal.revise）"))
    // Her list: short, newest first, with the reason.
    try await h.turn(Scripted("", historyList("想成為的樣子")))
    let out = try h.toolResults().last!.text
    #expect(out.hasPrefix("改動紀錄（新的在前，這裡 3 件，全部 3 件；要退回你自己的改動，用 history.undo 給〔〕裡的編號）：\n〔\(revs[0].short)〕11-19 10:00 你 ideal.revise：想｜動到：想成為的樣子"))
}

@Test func afterAnUndoTheMemoryIndexAndHerToolsMatchTheFiles() async throws {
    let dir = FileManager.default.temporaryDirectory.appending(path: "dollcore-test-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: dir.appending(path: "tools"), withIntermediateDirectories: true)
    try seededTool.write(to: dir.appending(path: "tools/開計算機.json"), atomically: true, encoding: .utf8)
    let h = try await Harness(dir: dir)
    let d = h.doll
    // A memory corrected, then her undo of the correction: the old one is found again, the new one not.
    await d.ownerSaid("牙醫約在週三")
    try await h.turn(Scripted("", remember([("主人的牙醫約在週三", false)])))
    let old = try memoryID(h, "主人的牙醫約在週三")
    await d.ownerSaid("不對，牙醫是週四")
    try await h.turn(Scripted("", fix(old, "主人的牙醫約在週四", basis: "牙醫是週四")))
    let new = try memoryID(h, "主人的牙醫約在週四")
    let fixCommit = try historyOf(h).log(limit: 5).first { $0.what == "memory.fix" }!
    try await h.turn(Scripted("", historyUndo(fixCommit.short, why: "主人後來說週三才對")))
    try await h.turn(Scripted("", recall("牙醫")))
    let out = try h.toolResults().last!.text
    #expect(out.contains("記憶 \(old)〕") && !out.contains("記憶 \(new)〕"))
    #expect(try memoryFilesByID(h)[new] == nil && fixesFile(h).items.isEmpty)
    // The owner deletes her tool: gone; his undo brings it back, runnable again.
    #expect(try await d.dashboardDeleteTool("開計算機"))
    #expect(try await d.dashboardTools().isEmpty)
    let del = try historyOf(h).log(limit: 1)[0]
    #expect(del.who == .owner && del.what == "刪掉工具「開計算機」")
    try await d.dashboardHistoryUndo(del.id)
    #expect(try await d.dashboardTools().map(\.name) == ["開計算機"])
}

@Test func aSecretChangeIsNotShownOutsideTheOwnersTurnsButTheDashboardDiffShowsIt() async throws {
    let h = try await Harness()
    let d = h.doll
    await d.ownerSaid("我的密碼提示是狗", secret: true)
    try await h.turn(Scripted("", remember([("主人的密碼提示是狗", true)])))
    let c = try historyOf(h).log(limit: 1)[0]
    #expect(c.paths.count == 1 && c.paths[0].hasPrefix("memory/secret/"))
    await d.developerSaid("你最近改了什麼？")
    try await h.turn(Scripted("", historyList()))
    let dev = try h.toolResults().last!.text
    #expect(dev.contains("〔\(c.short)〕") && dev.contains("改了你現在看不到的記憶") && !dev.contains("狗") && !dev.contains("密碼"))
    try await h.turn(Scripted("", historyUndo(c.short)))
    #expect(try h.toolResults().last!.text.contains("沒有編號是 \(c.short) 的改動"))
    await d.ownerSaid("你最近改了什麼？")
    try await h.turn(Scripted("", historyList("記憶")))
    #expect(try h.toolResults().last!.text.contains("動到：秘密的記憶"))
    let diff = try await d.dashboardHistoryDiff(c.id)
    #expect(diff.contains("主人的密碼提示是狗"))
}

@Test func aFailedCommitKeepsTheChangeSaysSoAndTheNextCommitNamesIt() async throws {
    let clock = ManualClock(at("2026-11-19", "10:00"))
    let h = try await goalsHarness(clock)
    // The branch cannot move (its directory is read-only): the commit fails after the change was written.
    let refs = h.dir.appending(path: "history/refs/heads")
    try FileManager.default.setAttributes([.posixPermissions: 0o500], ofItemAtPath: refs.path)
    await h.doll.ownerSaid("加目標")
    try await h.turn(Scripted("", ("goal.add", [("text", "懂海洋生物")])))
    try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: refs.path)
    // The change stands; her result and the log say it was not recorded.
    #expect(await h.doll.goalFile.nodes.map(\.text) == ["懂海洋生物"])
    #expect(try h.toolResults().last!.text.contains("（這個改動生效了，但沒記進改動紀錄："))
    #expect(h.logs.contains { $0.contains("ERROR") && $0.contains("history: commit failed (her goal.add)") })
    try await h.turn(Scripted("", ("goal.add", [("text", "學做菜")])))
    // Her call's commit failed, and so did the program's at the end of that turn: the next commit that
    // works (the program's, before her next calls run) carries those files and names both.
    let log = try historyOf(h).log(limit: 2)
    #expect(log[1].who == .program && log[1].paths == ["data/goals.json"] && log[1].unrecordedBefore == ["her goal.add", "program 回合中程式記下的"])
    #expect(log[0].what == "goal.add" && log[0].unrecordedBefore.isEmpty)
}

@Test func aChangeWithNoCommitIsFoundAtBootCommittedAndInTheBootReport() async throws {
    let clock = ManualClock(at("2026-11-19", "10:00"))
    let h = try await goalsHarness(clock)
    // A crash between the write and the commit: the file changed, no commit.
    try "我想把海洋生物弄懂。\n".write(to: h.dir.appending(path: "ideal-self.md"), atomically: true, encoding: .utf8)
    let b = try await goalsHarness(clock, dir: h.dir)
    #expect(b.doll.historyBoot == .found(["data/ideal-self.md"]))
    #expect(await b.doll.ideal == "我想把海洋生物弄懂。")
    let c = try historyOf(b).log(limit: 1)[0]
    #expect(c.who == .program && c.what == "開機時發現沒記錄的改動" && c.paths == ["data/ideal-self.md"])
    let r = try await b.doll.announceBoot()
    #expect(r.history == "上次結束前有 1 個檔改了卻沒記進改動紀錄（程式當掉或記錄失敗），這次開機補記成一筆「開機時發現，沒有記錄」：想成為的樣子")
    #expect(try b.events().last!.text.contains(r.history!))
}

@Test func changesFromTwoProcessesAreSerializedIntoOneLine() async throws {
    let dir = FileManager.default.temporaryDirectory.appending(path: "dollcore-test-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: dir.appending(path: "tools"), withIntermediateDirectories: true)
    var cfg = HistoryConfig()
    cfg.dir = dir.appending(path: "history").path
    let a = try History(config: cfg, data: dir, memory: MemoryConfig(), memoryDir: dir.appending(path: "memory"), box: try testBox(dir))
    _ = try a.open(ts: 1)
    let b = try History(config: cfg, data: dir, memory: MemoryConfig(), memoryDir: dir.appending(path: "memory"), box: try testBox(dir))
    let errors = Mutex<[String]>([])
    DispatchQueue.concurrentPerform(iterations: 20) { i in
        let h = i % 2 == 0 ? a : b
        do {
            try seededTool.replacingOccurrences(of: "開計算機", with: "t\(i)").write(to: dir.appending(path: "tools/t\(i).json"), atomically: true, encoding: .utf8)
            try h.record(HistoryChange(who: .her, what: "tool.save", why: "\(i)", ts: Double(i)))
        } catch { errors.withLock { $0.append("\(error)") } }
    }
    #expect(errors.withLock { $0 }.isEmpty)
    let tree = try a.tree(History.branch)
    #expect((0..<20).allSatisfy { tree["data/tools/t\($0).json"] != nil })
    // One line of commits: every one has at most one parent and the count adds up.
    #expect(try sysGit(dir.appending(path: "history"), "rev-list", "--min-parents=2", "--count", "main").out == "0")
}

@Test func aMissingGitIsABootErrorWithTheFix() async throws {
    await #expect { try await Harness { $0.history.git = "/nonexistent/git" } } throws: { "\($0)".contains("xcode-select --install") }
}

@Test func aToolRunChangesOnlyItsCountersAndMakesNoCommit() async throws {
    let dir = FileManager.default.temporaryDirectory.appending(path: "dollcore-test-\(UUID().uuidString)")
    let store = ToolStore(dir: dir.appending(path: "tools"))
    var cfg = HistoryConfig()
    cfg.dir = dir.appending(path: "history").path
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    let h = try History(config: cfg, data: dir, memory: MemoryConfig(), memoryDir: dir.appending(path: "memory"), box: try testBox(dir))
    _ = try h.open(ts: 1_800_000_000)
    var t = try JSONDecoder().decode(OwnTool.self, from: Data(seededTool.utf8))
    try store.write(t)
    #expect(try h.record(HistoryChange(who: .her, what: "tool.save", why: "", ts: 1_800_000_001))?.paths == ["data/tools/開計算機.json"])
    t.uses = 3; t.successes = 2; t.lastRun = "2026-11-19 10:00"
    try store.write(t)
    #expect(try h.record(HistoryChange(who: .her, what: "tool.run", why: "", ts: 1_800_000_002)) == nil)
    #expect(try store.get("開計算機")!.uses == 3)
    // What a commit costs on the actor's path: one goal-sized change, median of 20.
    var ms: [Double] = []
    for i in 0..<20 {
        try "{\"n\": \(i)}".write(to: dir.appending(path: "goals.json"), atomically: true, encoding: .utf8)
        let s = Date()
        _ = try h.record(HistoryChange(who: .her, what: "goal.add", why: "", ts: 1_800_000_010 + Double(i)))
        ms.append(Date().timeIntervalSince(s) * 1000)
    }
    let none = Date()
    for _ in 0..<20 { _ = try h.record(HistoryChange(who: .her, what: "dm", why: "", ts: 1_800_000_100)) }
    print("history: commit median \(ms.sorted()[10]) ms, max \(ms.max()!) ms; nothing changed \(Date().timeIntervalSince(none) * 1000 / 20) ms")
}

// [computer] enabled = false (2026-11-19, owner: an unattended trial runs without the computer and files) ----

/// What the computer switch must leave alone when on: every tools list and every prompt text, as
/// SHA-256 over all the combinations (pinned against the build before the switch, ef43eeb).
private func enabledRequestDigests() throws -> (tools: String, prompt: String) {
    let cfg = try exampleConfig()
    var tools: [String] = [], prompt: [String] = []
    for voice in [false, true] {
        for drives in [false, true] {
            tools.append(Tools.json(Tools.specs(memory: cfg.memory, voice: voice, computer: cfg.computer, files: cfg.files, drives: drives)))
            for maintenance in [false, true] {
                for sleep in [false, true] {
                    prompt.append(Instructions.text(voice: voice, context: cfg.context, docs: "/d", maintenance: maintenance, drives: drives, ownerSleep: sleep))
                }
            }
        }
    }
    prompt.append(Instructions.voice(docs: "/d"))
    func digest(_ parts: [String]) -> String { SHA256.hash(data: Data(parts.joined(separator: "\u{0}").utf8)).map { String(format: "%02x", $0) }.joined() }
    return (digest(tools), digest(prompt))
}

@Test func withTheComputerOnTheToolsAndPromptAreByteIdenticalToBeforeTheSwitch() throws {
    let d = try enabledRequestDigests()
    #expect(d.tools == "91e4c3f4da1be2b09ba9b483cea9c45f417aeccf3d2cd60df46603f4feeb97d6")
    #expect(d.prompt == "551e5009944c00c9ac9565978be03ae070415d5d49b1fb5862211f98c10e3c9d")
}

private func computerOff(_ c: inout Config) { c.computer.enabled = false }

/// The tools that exist only with the computer.
private let computerOnly = ["apps", "open", "read_window", "act", "read_file", "write_file", "edit_file", "tool.save", "tool.run"]

/// Words that would tell her of a computer, files, windows, screenshots or saved tools.
private let computerWords = ["電腦", "檔案", "視窗", "截圖", "畫面", "螢幕", "Mac", " app", "app ", "Touch ID", "cua", "read_window", "read_file", "write_file",
                             "edit_file", "\"open\"", "open 帶", "（例如 open）", "\"apps\"", "\"act\"", " act ", "tool.save", "tool.run", "存的工具", "〔工具〕",
                             "computer.md", "tools.md", "tool-failures.md", "click", "動作清單（像"]

@Test func theComputerSwitchIsReadStrictlyAndOnByDefault() throws {
    let text = try String(contentsOf: root.appending(path: "dollcore.example.toml"), encoding: .utf8)
    #expect(try exampleConfig().computer.enabled)
    let dir = FileManager.default.temporaryDirectory.appending(path: "computer-switch-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    let line = text.split(separator: "\n").first { $0.hasPrefix("enabled = true                # false（2026-11-19") }!
    for (value, ok) in [("false", true), ("\"no\"", false)] {
        let file = dir.appending(path: "\(UUID().uuidString).toml")
        try text.replacingOccurrences(of: line, with: "enabled = \(value)").write(to: file, atomically: true, encoding: .utf8)
        if ok { #expect(try !Config.load(file).computer.enabled) } else { #expect(throws: (any Error).self) { try Config.load(file) } }
    }
}

@Test func withTheComputerOffBootNeverLooksAtTheDriver() async throws {
    // A driver that would answer and start: off, it is not asked anything.
    let cli = try FakeDriverCLI(permissions: allowed)
    var c = ComputerConfig()
    c.enabled = false
    c.driver = cli.path
    let (daemon, line) = try await CuaDaemon.boot(c)
    #expect(daemon == nil && line == "computer: disabled ([computer] enabled = false): no cua-driver, no computer or file tools")
    #expect(cli.calls.isEmpty && !cli.up)
    // A driver that is not there at all is no boot failure either.
    c.driver = "/nonexistent/cua-driver"
    #expect(try await CuaDaemon.boot(c).daemon == nil)
    // On: the same driver is checked and started as before.
    c.enabled = true
    c.driver = cli.path
    c.daemonStartS = 5
    let (on, onLine) = try await CuaDaemon.boot(c)
    #expect(on?.started == true && onLine == "computer: cua-driver daemon started by this process (stopped at exit)" && cli.calls.contains("serve"))
    await on?.stop()
}

@Test func withTheComputerOffHerToolsAreTheOthersAndAskOwnerStays() async throws {
    let cfg = try exampleConfig()
    var off = cfg.computer
    off.enabled = false
    for voice in [false, true] {
        for drives in [false, true] {
            let onNames = Tools.specs(memory: cfg.memory, voice: voice, computer: cfg.computer, files: cfg.files, drives: drives).map(\.name)
            let offNames = Tools.specs(memory: cfg.memory, voice: voice, computer: off, files: cfg.files, drives: drives).map(\.name)
            #expect(offNames == onNames.filter { !computerOnly.contains($0) })
            #expect(offNames.contains("ask_owner") && offNames.last(where: { !$0.hasPrefix("voice.") }) == "ask_owner")
        }
    }
    let h = try await Harness(tune: computerOff)
    let tools = try #require(try JSONSerialization.jsonObject(with: Data(await h.doll.toolsJSON.utf8)) as? [[String: Any]])
    let names = tools.compactMap { ($0["function"] as? [String: Any])?["name"] as? String }
    #expect(names == ["discord.dm", "dollctl.reply", "think", "remember", "recall", "memory.fix", "history.list", "history.undo",
                      "todo.set", "todo.done", "todo.fail", "todo.wait", "todo.progress", "todo.continue",
                      "calendar.add", "calendar.change", "calendar.delete", "calendar.list", "ask_owner"])
}

@Test func withTheComputerOffNoPromptTextOrToolSaysComputerFilesWindowsShotsOrSavedTools() throws {
    let cfg = try exampleConfig()
    var off = cfg.computer
    off.enabled = false
    var texts: [String] = []
    for drives in [false, true] {
        for voice in [false, true] { texts.append(Tools.json(Tools.specs(memory: cfg.memory, voice: voice, computer: off, files: cfg.files, drives: drives))) }
        for maintenance in [false, true] {
            for sleep in [false, true] {
                texts.append(Instructions.text(voice: false, context: cfg.context, docs: "/d", maintenance: maintenance, drives: drives, ownerSleep: sleep, computer: false))
            }
        }
    }
    for t in texts {
        let found = computerWords.filter { t.contains($0) }
        #expect(found.isEmpty, "found \(found)")
        // Her docs are read with read_file: not pointed to.
        #expect(!t.contains("/d/"))
    }
    // The words do show up with the computer on (the list is not checking for nothing).
    let on = Instructions.text(voice: false, context: cfg.context, docs: "/d", maintenance: true, drives: true, ownerSleep: true)
    #expect(computerWords.filter { on.contains($0) }.count >= 15)
    // The voice section without the docs pointer.
    #expect(!Instructions.voice(docs: "/d", computer: false).contains("/d/"))
}

@Test func withTheComputerOffACallToAComputerToolIsAnUnknownToolAndTheListRulesStillHold() async throws {
    let h = try await Harness(tune: computerOff)
    await h.doll.ownerSaid("跟我說早安")
    try await h.turn(Scripted("", todoSet("跟主人說早安"), ("act", [("steps", #"[{"click": "7"}]"#)]),
                              ("write_file", [("path", "/tmp/dollcore-computer-off-\(UUID().uuidString).txt"), ("text", "x")]),
                              ("tool.run", [("name", "乘法")])))
    let results = try h.toolResults().suffix(4).map(\.text)
    #expect(results[0].hasPrefix("todo 列好了"))
    #expect(Array(results[1...]) == ["失敗：沒有 act 這個工具", "失敗：沒有 write_file 這個工具", "失敗：沒有 tool.run 這個工具"])
    #expect(h.cua.tools.isEmpty && h.last.actions == ["todo set", "error act", "error write_file", "error tool.run"])
    // Told before done holds; no evidence is asked for (no window or file was read).
    try await h.turn(Scripted("", done("說了早安", evidence: nil)))
    #expect(try h.toolResults().last!.text == notTold("主人", "discord.dm"))
    try await h.turn(Scripted("", dm("早安"), done("說了早安", evidence: nil)))
    let doneText = try h.toolResults().last!.text
    #expect(doneText.hasPrefix("完成，記下了。") && doneText.contains("這一項的過程（每一步的結果、那幾回合你的念頭）收起來了"))
    // 【狀況】 has no screenshots.
    let footer = try #require(try h.events().last { $0.footer != nil }?.footer)
    #expect(footer.contains("其中做事的過程 ") && !footer.contains("截圖"))
    // Done without having done anything: the hint names tools she has.
    await h.doll.ownerSaid("提醒我喝水")
    try await h.turn(Scripted("", todoSet("提醒主人喝水")))
    try await h.turn(Scripted("", done("提醒了", evidence: nil)))
    #expect(try h.toolResults().last!.text == "失敗：「提醒主人喝水」這一項你還沒做任何動作，也沒看過結果，不能說完成。先動手做；如果覺得已經是做完的樣子，至少用工具查過（例如 recall、calendar.list）再說完成。")
    // A failure's evidence is checked against what the item saw, with no windows or files in the words.
    try await h.turn(Scripted("", ("recall", [("query", "喝水")])))
    try await h.turn(Scripted("", ("todo.fail", [("cause", "主人不在"), ("evidence", "主人說他出門了"), ("tried", "查了記憶")])))
    #expect(try h.toolResults().last!.text.hasPrefix("失敗：還沒記成失敗：evidence「主人說他出門了」不在這一項你看過的東西裡（工具的結果、別人說的話）。"))
}

@Test func withTheComputerOffTheDecisionPointOffersItsThreeChoicesAndAskingTheOwnerWorks() async throws {
    let h = try await Harness(tune: { computerOff(&$0); $0.computer.decideAfterRounds = 3; $0.computer.decideEveryRounds = 2 })
    await h.doll.ownerSaid("幫我想週末去哪裡")
    try await h.turn(Scripted("", todoSet("跟主人說三個週末去處")))
    for _ in 0..<3 { try await h.turn(Scripted("", ("recall", [("query", "週末")]))) }
    let point = try #require(try h.events().last { $0.text.hasPrefix("【該決定了】") })
    #expect(point.text == "【該決定了】「跟主人說三個週末去處」已經做了 3 回合（做得成的事通常 7–14 回合）。選一個："
            + "① todo.continue(missing, stuck, change)：還缺什麼、最卡的是哪一步、下一次跟試過的哪裡不一樣（主人看得到）；② ask_owner 問主人；③ todo.fail 寫根本原因、照抄支持它的字。選了繼續，再過 2 回合會再問一次。")
    try await h.turn(Scripted("", ("todo.continue", [("missing", "還缺主人喜歡的類型"), ("stuck", "不知道主人喜歡山還是海"), ("change", "下一次先查記憶裡主人去過的地方")])))
    #expect(try h.toolResults().last!.text == "記下了，主人在後台看得到。第 6 回合會再問一次。")
    try await h.turn(Scripted("", ("recall", [("query", "主人去過")])))
    try await h.turn(Scripted("", ("recall", [("query", "山")])))
    #expect(await h.doll.todo.deciding)
    try await h.turn(Scripted("", ("ask_owner", [("what", "決定週末去山上還是海邊"), ("where", "週末去處這件事"), ("why", "我查不到你比較喜歡哪一種")])))
    #expect(h.rec.said.withLock { $0.last } == "【娃娃想先問你】\n要做：決定週末去山上還是海邊\n在哪裡：週末去處這件事\n為什麼：我查不到你比較喜歡哪一種")
    #expect(await h.doll.todo.phase == .waiting)
}

@Test func withTheComputerOffAFarEstimateIsStillRefusedAndCuriosityNeverComes() async throws {
    let h = try await Harness(tune: { computerOff(&$0); checking(&$0); $0.drives.enabled = true; $0.drives.driveHalfLifeH = noClimb })
    let items = ["行事曆上有看牙醫"]
    await h.doll.ownerSaid("幫我在行事曆記 11/20 看牙醫")
    try await h.turn(Scripted("", estimated(3, items: items), ("recall", [("query", "牙醫")])))
    for _ in 1..<7 { try await h.turn(Scripted("", ("recall", [("query", "牙醫")]))) }
    try await h.turn(Scripted("", done("記好了", evidence: nil), dm("好了")))
    await h.doll.ownerSaid("幫我在行事曆記 11/27 看牙醫")
    try await h.turn(Scripted("", estimated(2, items: items)))
    #expect(try h.toolResults().last!.text.hasPrefix("失敗：清單沒有列：你估整份 2 回合，可是以前做過像這次的清單：「行事曆上有看牙醫」你估 3、實際 7 回合。"))
    // No source of "new" without a computer: no curiosity wandering is ever due.
    #expect(await h.doll.curiosityDue() == nil)
}

@Test func aListForAStepOfHerOwnMayQuoteTheStepAndWhatTheOwnerDidToHerTree() async throws {
    // 2026-11-20, m12-idle-drive: 11 plans for her own steps were refused for quoting the step they
    // were about, or the owner's edit of her tree. The tree as the program holds it is a source, as
    // the calendar is; a node that is not in it is not.
    let h = try await goalsHarness(ManualClock(at("2026-11-16", "10:00")))
    let g = try await h.doll.goalAdd("懂海洋生物", parent: nil, by: "her")
    let s = try await h.doll.goalAdd("弄懂鯊魚的分類", parent: g.id, by: "her")
    let st = try await h.doll.goalAdd("讀維基的鯊魚條目", parent: s.id, by: "her")
    func set(_ quote: String) -> Scripted {
        Scripted("", ("todo.set", [("items", "[\"#\(st.id) 讀完鯊魚條目\"]"), ("subgoal", "\(s.id)")]
            + planArgs.map { $0.0 == "facts" ? ("facts", "[{\"fact\":\"這一步是讀鯊魚條目\",\"quote\":\"\(quote)\"}]") : $0 }))
    }
    try await h.turn(set("#\(st.id + 1)〔步驟〕讀維基的鯨魚條目"))
    #expect(try h.toolResults().last!.text.contains("不在你看得到的地方"))
    try await h.turn(set("#\(st.id)〔步驟〕讀維基的鯊魚條目"))
    #expect(try h.toolResults().last!.text.hasPrefix("todo 列好了"))
    #expect(await h.doll.todo.plan?.facts.first?.source == "目標樹")
}

@Test func whatSheWritesIntoHerOwnThingsIsKeptInTraditionalCharacters() async throws {
    // 2026-11-20, the soak run: a step 「问主人那套工具脚本在哪里」 and a memory with 「独角鯨」.
    let h = try await goalsHarness(ManualClock(at("2026-11-16", "10:00")))
    let g = try await h.doll.goalAdd("懂海洋生物", parent: nil, by: "her")
    try await h.turn(Scripted("", ("goal.add", [("text", "弄懂独角鲸的分类"), ("parent", "\(g.id)")]),
                              ("remember", [("items", #"[{"text":"独角鯨和白鯨是同一科，各种海豚是另一科","folder":"public"}]"#)])))
    #expect(await h.doll.goalFile.nodes.last?.text == "弄懂獨角鯨的分類")
    #expect(try h.store.memoryFTS(match: "\"獨角鯨\"", folders: ["public"], limit: 5).count == 1)
    #expect(try h.store.memoryFTS(match: "\"独角鯨\"", folders: ["public"], limit: 5).isEmpty)
}

// Waiting is not busy (2026-11-20, owner; the soak run: a list waited nine hours and she never did a
// thing of her own) -------------------------------------------------------------------------------

/// A list 等待中 on the owner's answer, and a sub-goal of hers to take a step for.
private func waitingOnTheOwner(_ clock: ManualClock = ManualClock(at("2026-11-16", "10:00")),
                                tune: @escaping (inout Config) -> Void = { _ in }) async throws -> (Harness, Int) {
    let h = try await goalsHarness(clock, tune: tune)
    let g = try await h.doll.goalAdd("懂海洋生物", parent: nil, by: "her")
    let s = try await h.doll.goalAdd("弄懂鯊魚的分類", parent: g.id, by: "her")
    await h.doll.ownerSaid("幫我問房東什麼時候來修水管")
    try await h.turn(Scripted("", todoSet("房東的時間問到了"), dm("房東的電話是幾號？"), todoWait("等主人給電話")))
    #expect(h.last.todo == "等待中")
    return (h, s.id)
}

private let ownStep = "讀完鯊魚條目"
private let recallCall: (String, [(String, String)]) = ("recall", [("query", "鯊魚")])

@Test func aListWaitingAsideDoesNotHoldMaintenanceHerOwnInProgressDoesAndBothCrossTheNightIntact() async throws {
    let clock = ManualClock(at("2026-11-17", "02:00"))
    let (h, sub) = try await waitingOnTheOwner(clock) {
        $0.maintenance.enabled = true; $0.maintenance.noticeMin = 0; $0.drives.maintenanceConcernsMax = 0; $0.goals.review = false
    }
    _ = try await h.doll.calendarCatchUp()
    try await h.turn(Scripted("", todoSetSub(sub, ownStep)))
    // Her own step is in progress beside the waiting list: maintenance waits for it, as before.
    clock.set(at("2026-11-17", "03:00"))
    await h.doll.maintenanceFire()
    try await Task.sleep(for: .milliseconds(100))
    #expect(!h.logs.contains { $0.contains("maintenance: started") })
    // Her own step waits too: nothing is in progress, so it starts at once.
    try await h.turn(Scripted("", todoWait("等網路恢復")))
    await h.model.add([planReply, summaryReply])
    await h.doll.maintenanceCheck()
    try await waitUntil { h.logs.contains { $0.contains("maintenance: ended") } }
    #expect(!h.logs.contains { $0.contains("maintenance: busy") })
    // Both lists are in the new day's timeline, each still waiting, and no turn follows.
    let t = try h.timeline()
    #expect(t.todo.current == ownStep && t.todo.phase == .waiting && t.todo.startSeq == 1)
    #expect(t.todoAside?.todo.current == "房東的時間問到了" && t.todoAside?.todo.phase == .waiting && t.todoAside?.todo.startSeq == 1)
    #expect(await h.doll.aside == t.todoAside)
    #expect(await !h.doll.followUp)
    #expect(try segmentTexts(h)[1][0].hasSuffix("你手上的「\(ownStep)」還在，還是等待中。"))
    // Her own step ends; the waiting list comes back in hand, and his answer resolves it.
    await h.doll.ownerSaid("網路好了")
    try await h.turn(Scripted("", recallCall))
    try await h.turn(Scripted("", done("讀完了", evidence: nil)))
    #expect(try h.toolResults().last!.text.hasSuffix("清單做完了。放在旁邊的「房東的時間問到了」回到你手上（等待中）。"))
    await h.doll.ownerSaid("電話是 0912-345-678")
    try await h.turn(Scripted("", dm("好，我打給他"), done("問到房東的電話", evidence: nil)))
    #expect(try h.toolResults().last!.text.contains("清單做完了，現在待命中"))
    #expect(try h.timeline().todo == TodoState() && h.timeline().todoAside == nil)
}

@Test func whileAListWaitsHerOwnStepRunsBesideItAndTheWaitingOneComesBackIntact() async throws {
    let (h, sub) = try await waitingOnTheOwner()
    #expect(await h.doll.idleForWander)
    // Her own step: the waiting list goes aside, untouched.
    try await h.turn(Scripted("", todoSetSub(sub, ownStep)))
    #expect(try h.toolResults().last!.text.hasSuffix("「房東的時間問到了」還在等待中，先放在旁邊：有人說話，程式會先把它拿回你手上；這份做完或結束，它也回到你手上。"))
    #expect(try await h.last.todo == "進行中")
    #expect(try await h.doll.todo.current == ownStep)
    #expect(try await h.doll.aside?.todo == (h.timeline().todoAside?.todo))
    #expect(try await h.doll.aside?.todo.phase == .waiting)
    #expect(await !h.doll.idleForWander)
    try await h.turn(Scripted("", recallCall))
    #expect(h.last.footer.contains("- todo：進行中「\(ownStep)」（子目標 #\(sub) 的步驟）；旁邊放著等待中的「房東的時間問到了」：手上這份做完或結束，它就回到你手上；有人說話，程式會先把它拿回你手上"))
    try await h.turn(Scripted("", done("讀完了", evidence: nil)))
    #expect(try h.toolResults().last!.text.hasSuffix("清單做完了。放在旁邊的「房東的時間問到了」回到你手上（等待中）。"))
    // Back in hand as it was: still waiting, no turn for it, its own rounds and plan.
    let back = await h.doll.todo
    #expect(back.phase == .waiting && back.current == "房東的時間問到了" && back.rounds == 0 && back.plan?.items == ["房東的時間問到了"])
    #expect(try await h.doll.aside == nil)
    #expect(try await (h.timeline().todoAside) == nil)
    #expect(try await (h.timeline().todo) == back)
    #expect(await !h.doll.followUp)
    #expect(await h.doll.goalFile.link?.subgoal == nil)
    // His answer: her todo.done goes to the waiting item.
    await h.doll.ownerSaid("電話是 0912-345-678")
    try await h.turn(Scripted("", dm("好，我打給他"), done("問到房東的電話", evidence: nil)))
    #expect(try h.toolResults().last!.text.contains("清單做完了，現在待命中"))
    #expect(try h.timeline().todo == TodoState())
    // Each list wrote its own estimate record.
    #expect(await h.doll.estimateFile.records.map(\.plan.items) == [[ownStep], ["房東的時間問到了"]])
}

@Test func hisAnswerMidStepGoesToTheWaitingItemAndHerOwnStepGoesOnAfterIt() async throws {
    let (h, sub) = try await waitingOnTheOwner()
    try await h.turn(Scripted("", todoSetSub(sub, ownStep)))
    // He answers while her own step runs: the waiting list is in hand for that turn, and an ending
    // call in it is held once (it may be meant for her own step); the same call next turn goes through.
    await h.doll.ownerSaid("電話是 0912-345-678")
    try await h.turn(Scripted("", dm("好，我打給他"), done("問到房東的電話", evidence: nil)))
    #expect(try h.toolResults().last!.text == "失敗：還沒做：這一回合手上的是等著的「房東的時間問到了」（有人說話了，程式先把它拿回你手上），你自己的「\(ownStep)」放在旁邊。他的話就是「房東的時間問到了」要的答案，就再呼叫一次 todo.done，這次就是給它的；你要做的是「\(ownStep)」，就呼叫 todo.wait（「房東的時間問到了」繼續等），「\(ownStep)」就回到你手上。")
    #expect(try await h.doll.todo.current == "房東的時間問到了")
    #expect(try await h.doll.followUp)
    try await h.turn(Scripted("", done("問到房東的電話", evidence: nil)))
    #expect(h.reports[h.reports.count - 2].footer.contains("- todo：等待中「房東的時間問到了」（有人說話才會再有回合；接著做它就回到進行中，沒碰它就繼續等）；你自己的事「\(ownStep)」先放在旁邊"))
    #expect(try h.toolResults().last!.text.hasSuffix("清單做完了。放在旁邊的「\(ownStep)」回到你手上（進行中）。"))
    #expect(try await h.doll.todo.current == ownStep)
    #expect(try await h.doll.todo.phase == .doing)
    #expect(try await h.doll.aside == nil)
    #expect(await h.doll.followUp)
    #expect(await h.doll.goalFile.link?.subgoal == sub)
}

@Test func herOwnStepsEndingCallInTheTurnHisLineBroughtTheWaitingListIsHeldAndTodoWaitGivesHerStepBack() async throws {
    // m13-wait-idle: an unrelated line came mid-step and her own step's todo.done landed on the waiting item.
    let (h, sub) = try await waitingOnTheOwner()
    try await h.turn(Scripted("", todoSetSub(sub, ownStep)))
    try await h.turn(Scripted("", recallCall))
    await h.doll.ownerSaid("今天好冷")
    try await h.turn(Scripted("", dm("多穿點"), done("讀完了", evidence: nil)))
    #expect(try h.toolResults().last!.text.hasPrefix("失敗：還沒做：這一回合手上的是等著的「房東的時間問到了」"))
    #expect(try h.timeline().todo.phase == .waiting)
    try await h.turn(Scripted("", todoWait("還在等電話")))
    #expect(try await h.doll.todo.current == ownStep)
    #expect(try await h.doll.aside?.todo.current == "房東的時間問到了")
    #expect(try await h.doll.aside?.todo.phase == .waiting)
    try await h.turn(Scripted("", done("讀完了", evidence: nil)))
    #expect(try h.toolResults().last!.text.hasSuffix("清單做完了。放在旁邊的「房東的時間問到了」回到你手上（等待中）。"))
}

@Test func aLineThatIsNotTheAnswerLeavesTheItemWaitingWithoutATodoWait() async throws {
    let (h, sub) = try await waitingOnTheOwner()
    // Plain waiting: she answers him, the item stays 等待中 and no turn follows.
    await h.doll.ownerSaid("晚餐吃什麼？")
    try await h.turn(Scripted("", dm("吃麵吧")))
    #expect(try await h.last.todo == "等待中")
    #expect(try await !h.doll.followUp)
    #expect(try h.timeline().todo.phase == .waiting)
    // A todo.wait anyway changes nothing and is not an error.
    await h.doll.ownerSaid("好")
    try await h.turn(Scripted("", todoWait("還在等電話")))
    #expect(try h.toolResults().last!.text.hasPrefix("「房東的時間問到了」本來就是等待中，沒有變"))
    // With her own step beside it: his line brings the waiting list for one turn; she does not take it
    // up, so her own step is back in hand and goes on.
    try await h.turn(Scripted("", todoSetSub(sub, ownStep)))
    await h.doll.ownerSaid("你在幹嘛")
    try await h.turn(Scripted("", dm("在讀鯊魚")))
    #expect(try await h.last.todo == "進行中")
    #expect(try await h.doll.todo.current == ownStep)
    #expect(try await h.doll.aside?.todo.phase == .waiting)
    #expect(await h.doll.followUp)
}

@Test func aRestartKeepsBothListsAndAThirdNeverStarts() async throws {
    let clock = ManualClock(at("2026-11-16", "10:00"))
    let (h, sub) = try await waitingOnTheOwner(clock)
    try await h.turn(Scripted("", todoSetSub(sub, ownStep)))
    try await h.turn(Scripted("", recallCall))
    // A new process: both lists as they were.
    let r = try await goalsHarness(clock, dir: h.dir)
    #expect(try await r.doll.todo.current == ownStep)
    #expect(try await r.doll.todo.phase == .doing)
    #expect(try await r.doll.todo.rounds == 1)
    #expect(try await r.doll.aside?.todo.current == "房東的時間問到了")
    #expect(try await r.doll.aside?.todo.phase == .waiting)
    #expect(await r.doll.goalFile.link?.subgoal == sub)
    // Her own step waits too: nothing more may start, and she is not idle.
    try await r.turn(Scripted("", todoWait("等主人說鯊魚書放哪")))
    #expect(await !r.doll.idleForWander)
    try await r.turn(Scripted("", todoSetSub(sub, "畫一隻鯊魚")))
    #expect(try r.toolResults().last!.text.hasPrefix("失敗：沒有列：「\(ownStep)」在等待中，旁邊已經放著「房東的時間問到了」；一次最多一份等著、一份你自己的事。"))
    #expect(try await r.doll.todo.current == ownStep)
    #expect(try await r.doll.aside?.todo.current == "房東的時間問到了")
}

// A memory may be overwritten, never taken away (2026-11-20, owner: 「記憶不可以刪掉但可以覆蓋」); one she
// wrote herself she may overwrite without a basis (the soak run: a whale taxonomy from her own
// recollection, wrong, and a second "revised" one beside it) -------------------------------------------

private let noRetireOnly = "記憶不會刪掉，只能換成新的內容：給 text，寫它應該是的整條。整條都錯、又不知道對的是什麼，text 就寫你知道的，例如「之前記的…是錯的（依據…）；正確的還不知道」。"
private func overwrite(_ id: String, _ text: String?, why: String = "我憑印象記的，記錯了") -> (String, [(String, String)]) {
    ("memory.fix", [("id", id)] + (text.map { [("text", $0)] } ?? []) + [("why", why)])
}

@Test func sheOverwritesAMemorySheWroteHerselfWithoutABasisAndTheUndoBringsItBack() async throws {
    let h = try await Harness()
    // Nobody spoke: her own recollection is the doll tier.
    try await h.turn(Scripted("", remember([("獨角鯨和白鯨是同一科，海豚是另一科", false)])))
    let old = try memoryID(h, "獨角鯨和白鯨是同一科，海豚是另一科")
    #expect(try memoryFilesByID(h)[old]!.file.tier == .doll)
    // Retiring only is refused for hers too.
    try await h.turn(Scripted("", overwrite(old, nil)))
    #expect(try h.toolResults().last!.text == "失敗：沒有改：" + noRetireOnly)
    try await h.turn(Scripted("", overwrite(old, "之前記的「獨角鯨和白鯨是同一科，海豚是另一科」是我憑印象寫的，沒查過；正確的分類還不知道")))
    let new = try memoryID(h, "之前記的「獨角鯨和白鯨是同一科，海豚是另一科」是我憑印象寫的，沒查過；正確的分類還不知道")
    #expect(try h.toolResults().last!.text == "改好了：記憶 \(old)「獨角鯨和白鯨是同一科，海豚是另一科」換成記憶 \(new)「之前記的「獨角鯨和白鯨是同一科，海豚是另一科」是我憑印象寫的，沒查過；正確的分類還不知道」（那條是你自己憑印象記的，不用依據）。舊的收起來了，主人在儀表板看得到，可以還原。")
    let files = try memoryFilesByID(h)
    // The old file stays on disk, superseded by the new one; the new one is still hers.
    #expect(files[old]!.file.retired?.supersededBy == new && files[old]!.file.retired?.reason == "她覆蓋自己記的：我憑印象記的，記錯了（她自己憑印象記的，沒有依據）")
    #expect(files[new]!.file.tier == .doll)
    let rec = try fixesFile(h).items
    #expect(rec.count == 1 && rec[0].by == "her" && rec[0].basis == "" && rec[0].basisFrom == Doll.hersFrom && rec[0].replacement == new)
    // The owner's undo: the old one is back, the replacement gone.
    try await h.doll.dashboardMemoryRevert(1)
    let back = try memoryFilesByID(h)
    #expect(back[old]!.file.retired == nil && back[new] == nil)
}

@Test func withoutABasisOnlyHerOwnMemoryMayBeOverwritten() async throws {
    let h = try await Harness()
    try await seedMemory(h, "public", "主人說他最愛藍鯨", tier: "owner", id: "1-aaaaaa")
    try await seedMemory(h, "public", "主人週末常去海邊", tier: "consolidated", id: "1-bbbbbb")
    try await seedMemory(h, "public", "計算機在 Dock 第三個", tier: "tool", id: "1-cccccc")
    for (id, from) in [("1-aaaaaa", "主人說的話"), ("1-bbbbbb", "系統維護整理時寫的"), ("1-cccccc", "工具的結果或讀到的東西")] {
        try await h.turn(Scripted("", overwrite(id, "改掉")))
        #expect(try h.toolResults().last!.text == "失敗：沒有改：記憶 \(id) 不是你自己憑印象記的（程式記的來源是\(from)），沒有依據不能改。basis 照抄主人說的話，或你讀到的視窗、檔案、工具結果裡的字；找不到依據，就先去查或問主人。")
    }
    #expect(try memoryFilesByID(h).values.allSatisfy { $0.file.retired == nil } && fixesFile(h).items.isEmpty)
    // A rewrite with a basis that is not one is still refused, hers or not.
    try await seedMemory(h, "public", "藍鯨是最大的魚", tier: "doll", id: "1-dddddd")
    try await h.turn(Scripted("", fix("1-dddddd", "藍鯨是最大的動物", basis: "藍鯨是哺乳類動物")))
    #expect(try h.toolResults().last!.text.hasPrefix("失敗：沒有改：basis「藍鯨是哺乳類動物」不在主人說的話"))
}

@Test func theAuditOnlyFlagsAMemoryItWouldRetireWithNothingInItsPlace() async throws {
    let reply = Scripted("""
        ```json
        {"results": [{"m": "m1", "verdict": "ok"}, {"m": "m2", "verdict": "ok"},
                     {"m": "m3", "verdict": "wrong", "quote": "下週四下午三點要去中山牙醫看牙", "fixed": ""}]}
        ```
        """)
    let (h, clock, runner) = try await auditDay(mode: "change", [reply, planReply, summaryReply])
    let id = try memoryID(h, "主人怕打針")
    clock.set(at("2026-11-17", "03:00"))
    try await waitUntil { h.logs.contains { $0.contains("maintenance: ended") } }
    #expect(try memoryFilesByID(h)[id]!.file.retired == nil)
    let rec = try fixesFile(h).items
    #expect(rec.count == 1 && rec[0].by == "flag" && rec[0].memory == id && rec[0].after == nil && rec[0].replacement == nil)
    #expect(h.logs.contains { $0.contains("0 changed, 1 flagged") })
    await h.doll.stop()
    try await runner.value
}

@Test func aConsolidationRetirementNeedsWhatReplacesIt() throws {
    let plan = { (ops: String) in "```json\n{\"entities\": [], \"operations\": [\(ops)]}\n```" }
    for by in ["", "m1", "zz"] {
        #expect(throws: ConsolidationError.self, "\(by)") {
            try Consolidation.parse(plan(#"{"op": "retire", "id": "m1", "superseded_by": "\#(by)", "reason": "舊了"}"#), known: ["m1", "m2"], registry: [], tasks: [0])
        }
    }
    let ok = try Consolidation.parse(plan(#"{"op": "retire", "id": "m1", "superseded_by": "m2", "reason": "併進 m2"}"#), known: ["m1", "m2"], registry: [], tasks: [0])
    #expect(ok.operations.count == 1)
}

@Test func whenTheWaitingListSheTookUpWaitsAgainHerOwnStepIsBackInHand() async throws {
    // m13-wait-idle: her own list sat aside 進行中 while the list in hand waited again, and nothing gave her a turn for it.
    let (h, sub) = try await waitingOnTheOwner()
    try await h.turn(Scripted("", todoSetSub(sub, ownStep)))
    await h.doll.ownerSaid("房東電話我等等找給你")
    try await h.turn(Scripted("", dm("好"), progress("主人說等等給電話")))
    #expect(try h.toolResults().last!.text.hasPrefix("失敗：還沒做：這一回合手上的是等著的「房東的時間問到了」"))
    // She takes it up (the held call again), then it waits again: her own list is back in hand and goes on.
    let ask: (String, [(String, String)]) = ("ask_owner", [("what", "給我房東的電話"), ("where", "私訊"), ("why", "要問他什麼時候來修水管")])
    try await h.turn(Scripted("", ask))
    #expect(try await h.doll.todo.current == ownStep)
    #expect(try await h.doll.todo.phase == .doing)
    #expect(try await h.doll.aside?.todo.phase == .waiting)
    #expect(try await h.doll.followUp)
}

// secrets at rest (owner, 2026-11-21: `SecretBox.swift`) ----------------------------------------------

/// Where any of `needles` appears in plain bytes under the harness's data dir (memory files, the
/// timeline, `doll.db` with its WAL, her documents, boot reports, memory-fixes.json, maintenance.json,
/// the history repository's files), in any object of the history repository, decompressed by git
/// (`cat-file --batch-all-objects`), or in her log lines (the sink gets them as written).
private func plainHits(_ h: Harness, _ needles: [String]) throws -> [String] {
    var hits: [String] = []
    let e = FileManager.default.enumerator(at: h.dir, includingPropertiesForKeys: [.isRegularFileKey])!
    for case let url as URL in e {
        guard (try url.resourceValues(forKeys: [.isRegularFileKey])).isRegularFile == true else { continue }
        let d = try Data(contentsOf: url)
        for n in needles where d.range(of: Data(n.utf8)) != nil { hits.append("\(url.path): \(n)") }
    }
    let p = Process()
    p.executableURL = URL(filePath: "/usr/bin/env")
    p.arguments = ["git", "--git-dir", h.dir.appending(path: "history").path, "cat-file", "--batch-all-objects", "--batch"]
    p.environment = Git.environment([:])
    let out = Pipe()
    p.standardOutput = out
    p.standardError = Pipe()
    try p.run()
    let objects = out.fileHandleForReading.readDataToEndOfFile()
    p.waitUntilExit()
    #expect(p.terminationStatus == 0 && objects.count > 1000)
    for n in needles where objects.range(of: Data(n.utf8)) != nil { hits.append("history object: \(n)") }
    for n in needles where h.logs.contains(where: { $0.contains(n) }) { hits.append("log: \(n)") }
    return hits
}

@Test func secretAndOwnMemoriesReachTheDiskOnlyEncryptedAndStillWorkEverywhere() async throws {
    let h = try await Harness()
    let d = h.doll
    let needles = ["Zq7", "河馬", "Kx9", "企鵝", "保險箱"]
    // The owner's secret, remembered into secret; her own password into own.
    await d.ownerSaid("我的保險箱密碼是 Zq7-河馬-913", secret: true)
    try await h.turn(Scripted("", remember(folders: [("主人的保險箱密碼是 Zq7-河馬-913", "secret", nil), ("我的信箱密碼是 Kx9-企鵝-42", "own", nil)])))
    let old = try memoryID(h, "主人的保險箱密碼是 Zq7-河馬-913")
    #expect(try memoryFilesByID(h)[old]!.folder == "secret")
    // The owner's turn finds it (and the result is a secret event); the developer's turn finds her own one.
    try await h.turn(Scripted("", recall("保險箱密碼")))
    let found = try h.toolResults().last!
    #expect(found.text.contains("Zq7-河馬-913") && found.secret)
    await d.developerSaid("我的信箱密碼是什麼")
    try await h.turn(Scripted("", recall("信箱密碼")))
    #expect(try h.toolResults().last!.text.contains("Kx9-企鵝-42"))
    // A correction of the secret one.
    await d.ownerSaid("不對，保險箱密碼是 Zq7-河馬-914", secret: true)
    try await h.turn(Scripted("", fix(old, "主人的保險箱密碼是 Zq7-河馬-914", basis: "保險箱密碼是 Zq7-河馬-914")))
    #expect(try h.toolResults().last!.text.hasPrefix("改好了"))
    let new = try memoryID(h, "主人的保險箱密碼是 Zq7-河馬-914")
    #expect(try fixesFile(h).items.first?.sealed == true)
    // The owner's diff of it on the dashboard: decrypted.
    let fixCommit = try historyOf(h).log(limit: 5).first { $0.what == "memory.fix" }!
    let diff = try await d.dashboardHistoryDiff(fixCommit.id)
    #expect(diff.contains("+主人的保險箱密碼是 Zq7-河馬-914") && diff.contains("memory/secret/\(new).md"))
    #expect(fixCommit.why.isEmpty && fixCommit.args == nil)
    // Nowhere in plain text: files, sqlite with its WAL, timeline, history objects.
    #expect(try plainHits(h, needles) == [])
    // His undo of the correction: the old one is back, still encrypted.
    try await d.dashboardHistoryUndo(fixCommit.id)
    let files = try memoryFilesByID(h)
    #expect(files[old]!.file.retired == nil && files[old]!.folder == "secret" && files[new] == nil)
    #expect(try plainHits(h, needles) == [])
    // A new process: the index of the encrypted folders is rebuilt in memory, by text and by meaning.
    let again = try await Harness(dir: h.dir)
    try await again.doll.boot()
    await again.doll.ownerSaid("保險箱", secret: true)
    try await again.turn(Scripted("", recall("Zq7")))
    #expect(try again.toolResults().last!.text.contains("Zq7-河馬-913"))
    let cfg = try consolidationConfig(again)
    let memory = try Memory(config: cfg.memory, root: again.dir.appending(path: "memory"), store: again.store, embedder: FakeEmbedder(),
                            box: try testBox(again.dir))
    #expect(try memory.syncOnBoot() == 2)
    func channels(_ q: String) throws -> Set<Candidate.Channel> {
        try memory.related(q, entities: [], visible: [.public, .own, .secret], tiers: nil, today: nil, embedding: .best(1))
            .first { $0.key == "m:" + old }?.channels ?? []
    }
    #expect(try channels("河馬-913").contains(.fts))
    let byMeaning = try channels("密碼保險")
    #expect(byMeaning == [.embedding])
    #expect(try plainHits(again, needles) == [])
}

@Test func theAuditAndTheConsolidationOfASecretMemoryLeaveNoPlainText() async throws {
    let clock = ManualClock(at("2026-11-16", "21:00"))
    let audit = Scripted("""
        ```json
        {"results": [{"m": "m1", "verdict": "wrong", "quote": "保險箱密碼是 Zq7-河馬-915", "fixed": "主人的保險箱密碼是 Zq7-河馬-915"}]}
        ```
        """)
    let h = try await Harness([Scripted("", remember([("主人的保險箱密碼是 Zq7-河馬-913", true)])), Scripted("", dm("記下了")),
                               audit, planReply, Scripted("今天主人說保險箱密碼是 Zq7-河馬-915。")], clock: clock) {
        $0.maintenance.enabled = true; $0.maintenance.noticeMin = 0; $0.audit.enabled = true; $0.audit.mode = "change"
    }
    let runner = Task { [doll = h.doll] in try await doll.run() }
    await h.doll.ownerSaid("我的保險箱密碼是 Zq7-河馬-915", secret: true)
    try await waitUntil { h.said == ["記下了"] }
    clock.set(at("2026-11-17", "03:00"))
    try await waitUntil { h.logs.contains { $0.contains("maintenance: ended") } }
    await h.doll.stop()
    try await runner.value
    // The audit corrected it, in secret; the end notice (its words and the day's summary) reads back whole.
    let new = try memoryID(h, "主人的保險箱密碼是 Zq7-河馬-915")
    #expect(try memoryFilesByID(h)[new]!.folder == "secret")
    let notice = try h.timeline().events.first!
    #expect(notice.sealed && notice.text.contains("改成「主人的保險箱密碼是 Zq7-河馬-915」") && notice.text.contains("今天主人說保險箱密碼是 Zq7-河馬-915"))
    #expect(try fixesFile(h).items.map(\.sealed) == [true])
    // Not on disk, not in the history, not in the log.
    #expect(try plainHits(h, ["Zq7", "河馬", "保險箱"]) == [])
    #expect(h.logs.filter { $0.contains("河馬") } == [])
}

@Test func bootFailsOnPlainSecretsAndOnEncryptedSecretsWithoutTheirKey() async throws {
    func fresh() throws -> URL {
        let dir = FileManager.default.temporaryDirectory.appending(path: "dollcore-test-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir.appending(path: "memory/secret"), withIntermediateDirectories: true)
        return dir
    }
    func bootError(_ dir: URL) async -> String {
        do {
            let h = try await Harness(dir: dir)
            try await h.doll.boot()
            return "booted"
        } catch { return "\(error)" }
    }
    let file = "+++\nkind = \"\"\nconfidence = 0.9\ncreated_ts = 1\nsource_event_seq = 1\nsource_tier = \"owner\"\n+++\n主人的秘密"
    // A plain file in an encrypted folder: its path.
    let a = try fresh()
    let plain = a.appending(path: "memory/secret/1-aaaaaa.md")
    try file.write(to: plain, atomically: true, encoding: .utf8)
    #expect(await bootError(a) == "\(plain.path) is a plain-text memory file in an encrypted folder (secret); plain secrets are no longer allowed (no conversion: remove it)")
    // Encrypted secrets and no key for this data dir: no new key is made.
    let b = try fresh()
    let sealed = b.appending(path: "memory/secret/1-bbbbbb.md")
    try (try SecretBox(key: SymmetricKey(size: .bits256)).seal(file) + "\n").write(to: sealed, atomically: true, encoding: .utf8)
    #expect(await bootError(b) == "encrypted secrets exist (\(sealed.path)) but the memory key for \(SecretBox.account(data: b)) is not in the Keychain; without it they cannot be read")
    #expect(try testKeys.load(account: SecretBox.account(data: b)) == nil)
    // A key that does not open them (another key): the file named.
    _ = try testBox(b)
    #expect(await bootError(b) == "\(sealed.path) cannot be decrypted with the memory key from the Keychain (a different key, or damaged)")
    // A secret timeline event in plain text: its line.
    let c = try fresh()
    try FileManager.default.createDirectory(at: c.appending(path: "timeline"), withIntermediateDirectories: true)
    let segment = c.appending(path: "timeline/20261121-000000.jsonl")
    try #"{"type":"event","seq":1,"ts":1,"kind":"owner","source":"owner","secret":true,"voice":false,"text":"主人的秘密"}"#
        .appending("\n").write(to: segment, atomically: true, encoding: .utf8)
    #expect(await bootError(c) == "\(segment.path) line 1 is a secret event stored in plain text; plain secrets are no longer allowed (no conversion: remove it)")
    // Rows of the encrypted folders in the on-disk index.
    let e = try fresh()
    try Store(path: e.appending(path: "doll.db")).putMemoryIndex(id: "1-cccccc", path: "x", folder: "secret", confidence: 0.9, ts: 1, sourceSeq: 1,
                                                                 kind: "", tier: "owner", entities: [], text: "主人的秘密", vector: Data([0, 0, 0, 0]))
    #expect(await bootError(e) == "\(e.appending(path: "doll.db").path) holds 2 index rows of the secret/own folders in plain text; those folders are encrypted now and their index is in memory only (delete the file: it is rebuilt from the memory files)")
}

/// The real login Keychain, only with `DOLLCORE_KEYCHAIN_TEST=1`: a throwaway service, deleted after.
@Test func theLoginKeychainKeepsTheKeyWhenAskedTo() throws {
    guard ProcessInfo.processInfo.environment["DOLLCORE_KEYCHAIN_TEST"] == "1" else { return }
    let store = KeychainKeyStore(service: "DollOS dollcore keychain test \(UUID().uuidString)")
    defer { try? store.delete(account: "/tmp/dollcore-keychain-test") }
    let box = try SecretBox.unlock(store: store, account: "/tmp/dollcore-keychain-test", sealedPlace: nil)
    let again = try SecretBox.unlock(store: store, account: "/tmp/dollcore-keychain-test", sealedPlace: nil)
    #expect(try again.open(try box.seal("秘密"), place: "test") == "秘密")
    try store.delete(account: "/tmp/dollcore-keychain-test")
    #expect(try store.load(account: "/tmp/dollcore-keychain-test") == nil)
}

// the owner-line secret check (owner, 2026-11-21: `SecretCheck.swift`) ----------------------------------

@Test func theModelDecidesWhetherAnOwnerLineIsASecretALockForcesItAndAFailureMakesItOne() async throws {
    let h = try await Harness()
    let d = h.doll
    // The check says 是: held until then, a secret event. One request: the question and his line, no tools, no thinking.
    await h.model.add(["是"])
    await d.ownerLine("我的銀行密碼是 Pw8-犀牛-552").value
    #expect(await h.model.requests.map { $0.messages.map(\.content) } == [[SecretCheck.instruction, "我的銀行密碼是 Pw8-犀牛-552"]])
    #expect(await h.model.requests.map(\.effort) == ["none"])
    #expect(await h.model.tools == [nil])
    #expect(await d.queue.map(\.secret) == [true])
    // She sees it in the owner's turn.
    try await h.turn(Scripted("", dm("好，我記住了")))
    #expect(await h.model.seen(1).contains { $0.content.contains("Pw8-犀牛-552") })
    // A developer's turn: the secret line and her secret reply keep their place, not their content.
    await d.developerSaid("剛剛主人說了什麼")
    try await h.turn(Scripted("", reply("那是主人的事")))
    let dev = await h.model.seen(2)
    #expect(!dev.contains { $0.content.contains("Pw8") || $0.content.contains("記住了") || $0.toolCalls.contains { ($0.arguments ?? "").contains("記住了") } })
    #expect(dev.filter { $0.content == Doll.hiddenSecret }.count >= 2)
    // 🔒 first: no request, a secret; the line keeps the lock (she sees he locked it).
    await d.ownerLine("🔒 保險箱在衣櫃第二層").value
    #expect(await h.model.requests.count == 3)
    #expect(await d.queue.last?.secret == true)
    #expect(await d.queue.last?.text == "🔒 保險箱在衣櫃第二層")
    // 否: an ordinary line, plain.
    await h.model.add(["否"])
    await d.ownerLine("今天晚餐吃咖哩飯").value
    #expect(await d.queue.last?.secret == false)
    // No clean answer, then no answer at all (the script ran out: an error): secret both times,
    // and the log says the check failed, without the line.
    await h.model.add(["可能吧，看情況"])
    await d.ownerLine("我下個月要去看身心科").value
    await d.ownerLine("我的提款卡密碼是 7391").value
    #expect(await d.queue.suffix(2).map(\.secret) == [true, true])
    let failed = h.logs.filter { $0.contains("the secret check failed") }
    #expect(failed.count == 2 && failed[0].contains("unreadable answer") && failed[1].contains("script ran out"))
    try await h.turn(Scripted("", dm("好"), remember([("主人今天晚餐吃咖哩飯", false),
                                                       ("主人週末常常去家附近那家咖哩店，點豬排咖哩加一顆半熟蛋，配一杯冰紅茶；吃完會去河濱公園散步一圈，看看有沒有人在放風箏，再慢慢走回家洗澡看書", false)])))
    // Queued in the order they came.
    #expect(try h.events().filter { $0.kind == .owner }.map(\.text)
            == ["我的銀行密碼是 Pw8-犀牛-552", "🔒 保險箱在衣櫃第二層", "今天晚餐吃咖哩飯", "我下個月要去看身心科", "我的提款卡密碼是 7391"])
    // On disk (files, sqlite, timeline, history objects) and in the log: only the ordinary line.
    let secrets = ["Pw8", "犀牛", "保險箱", "衣櫃", "身心科", "7391"]
    #expect(try plainHits(h, secrets) == [])
    #expect(h.logs.filter { l in secrets.contains { l.contains($0) } } == [])
    let ordinary = try plainHits(h, ["咖哩飯"])
    #expect(ordinary.contains { $0.contains("/timeline/") } && ordinary.contains { $0.contains("/memory/public/") })
}

@Test func hisVoiceCutsHerCallAtOnceAndTheRerunWaitsForItsCheck() async throws {
    let h = try await Harness(["否", Scripted("", dm("好，我停下來"))])
    await h.model.stall(request: 1)
    let runner = Task { [doll = h.doll] in try await doll.run() }
    await h.doll.developerSaid("幫我算 7+8")
    try await waitUntil { await h.model.requests.count == 1 }
    await h.doll.ownerLine("等一下先停", voice: true, channel: .discordVoice)
    try await waitUntil { h.said == ["好，我停下來"] }
    await h.doll.stop()
    try await runner.value
    // Her call was cancelled, the check ran (request 2), and the rerun (request 3) has his line.
    #expect(await h.model.cancelled == 1)
    #expect(await h.model.seen(1).map(\.content) == [SecretCheck.instruction, "等一下先停"])
    #expect(await h.model.seen(2).contains { $0.content.hasSuffix("主人｜Discord 語音｜語音] 等一下先停") })
}

@Test func theCheckReadsOnlyACleanYesOrNo() {
    #expect(SecretCheck.read("是") == true && SecretCheck.read(" 否。\n") == false && SecretCheck.read("是！") == true)
    for bad in ["", "是的", "否，沒有", "可能", "yes", "是 否"] { #expect(SecretCheck.read(bad) == nil, "\(bad)") }
    #expect(SecretCheck.isLocked("🔒 x") && SecretCheck.isLocked("🔒️x") && !SecretCheck.isLocked("x 🔒") && !SecretCheck.isLocked(" 🔒"))
}

// derived texts of a secret turn (owner, 2026-11-21: anything written by a turn that saw secret content is sealed) ---

/// One turn of hers writing every derived text: a concern, a goal and its record entry, a revision of
/// her ideal self, a todo list, each quoting `s`; then a DM.
private func derivedTurn(_ s: String) -> Scripted {
    Scripted("", noteCall("記得主人說的\(s)", "owner"), ("goal.add", [("text", "守住\(s)")]), ("goal.end", [("id", "1"), ("how", "done")]),
             ("ideal.revise", [("text", "我想好好守住\(s)這種事。"), ("why", "主人說了\(s)"), ("based_on", "主人說\(s)")]),
             todoSet("把\(s)抄進筆記"), dm("好"))
}

@Test func aSecretTurnsDerivedWritesAreSealedAndReadBackWhole() async throws {
    let clock = ManualClock(at("2026-11-21", "10:00"))
    let h = try await goalsHarness(clock)
    let d = h.doll
    let needles = ["Zq7", "河馬"]
    await d.ownerSaid("我的保險箱密碼是 Zq7-河馬-913", secret: true)
    try await h.turn(derivedTurn("Zq7-河馬"))
    #expect(try h.toolResults().suffix(6).allSatisfy { !$0.text.hasPrefix("失敗") })
    // Nowhere in plain text: concerns, goals, record, ideal self and its log, the todo line, the
    // commits' why and args, the log.
    let hits = try plainHits(h, needles)
    #expect(hits == [])
    let data = h.dir
    for name in ["concerns.json", "goals.json", "record.json", "ideal-self.md", "ideal-self-log.json"] {
        #expect(SecretBox.isSealed(try String(contentsOf: data.appending(path: name), encoding: .utf8)), "\(name)")
    }
    let hist = try historyOf(h)
    let goalCommit = try hist.log(limit: 20).first { $0.what == "goal.add" }!
    #expect(goalCommit.why.isEmpty && goalCommit.args == nil)
    // The owner sees them decrypted: the drives and goals pages, the history diff.
    #expect(await d.dashboardDrives().open.contains { $0.text == "記得主人說的Zq7-河馬" })
    let goals = await d.dashboardGoals()
    #expect(goals.nodes.first?.text == "守住Zq7-河馬" && goals.record.first?.text == "守住Zq7-河馬" && goals.ideal == "我想好好守住Zq7-河馬這種事。")
    #expect(try await d.dashboardHistoryDiff(goalCommit.id).contains("守住Zq7-河馬"))
    // Her todo list reads back from the timeline.
    #expect(try h.timeline().todo.current == "把Zq7-河馬抄進筆記")
    // A later run's boot report built from such a log (a line that quotes the secret): the line stays
    // a box on disk, the owner reads it.
    let box = try testBox(h.dir)
    let sealedLine = try h.logs.first { try $0.hasPrefix("INFO " + SecretBox.prefix) && box.open(String($0.dropFirst(5)), place: "log").contains("Zq7-河馬") }!
    let logs = logDir()
    let t0 = at("2026-11-21", "09:00")
    try seedLog(logs, [start(t0), line(t0 + 1, "WARN", String(sealedLine.dropFirst("INFO ".count)))])
    // A new process on the same dir boots, reads the sealed files and the sealed todo line.
    let again = try await Harness(dir: h.dir, clock: clock) { $0.drives.enabled = true }
    try await again.doll.boot()
    #expect(await again.doll.todo.current == "把Zq7-河馬抄進筆記")
    #expect(await again.doll.dashboardDrives().open.contains { $0.text == "記得主人說的Zq7-河馬" })
    #expect(await again.doll.dashboardGoals().ideal == "我想好好守住Zq7-河馬這種事。")
    let report = try await again.doll.announceBoot(previous: try RunLog.lastRun(dir: logs))
    #expect(report.previous?.kinds.map(\.key) == [RunLog.sealedKind])
    let shown = try await again.doll.dashboardBoots(limit: 1)
    #expect(shown.first?.previous?.kinds.first?.example.contains("Zq7-河馬") == true)
    let hitsAgain = try plainHits(again, needles)
    #expect(hitsAgain == [])
}

@Test func anOrdinaryTurnsDerivedWritesStayPlain() async throws {
    let clock = ManualClock(at("2026-11-21", "10:00"))
    let h = try await goalsHarness(clock)
    await h.doll.ownerSaid("幫我記一下企鵝的事")
    try await h.turn(derivedTurn("Kx9-企鵝"))
    let hits = try plainHits(h, ["Kx9-企鵝"])
    for place in ["concerns.json", "goals.json", "record.json", "ideal-self.md", "ideal-self-log.json", "timeline/", "history object", "log:"] {
        #expect(hits.contains { $0.contains(place) }, "\(place)")
    }
    let goalCommit = try historyOf(h).log(limit: 20).first { $0.what == "goal.add" }!
    #expect(goalCommit.args?.contains("Kx9-企鵝") == true)
}

@Test func aCalendarItemMadeInASecretTurnIsStoredBoxedAndFiresSealed() async throws {
    let clock = ManualClock(at("2026-11-16", "08:00"))
    let h = try await Harness(clock: clock)
    await h.doll.ownerSaid("🔒 我下週三要去榮總做胃鏡", secret: true)
    // (Two long public memories too: the disk scan wants a history with objects in it.)
    try await h.turn(Scripted("", ("calendar.add", [("title", "榮總做胃鏡"), ("date", "2026-11-16"), ("start", "09:00")]), dm("記好了"),
                              remember([("主人週末常常去家附近那家咖哩店，點豬排咖哩加一顆半熟蛋，配一杯冰紅茶；吃完會去河濱公園散步一圈，看看有沒有人在放風箏，再慢慢走回家洗澡看書", false),
                                        ("主人的貓叫麻糬，早上六點會跳上床踩他的臉討飯吃，吃完罐頭會在窗台曬太陽，下午兩點左右喜歡把桌上的筆一枝一枝推下去，晚上則躲在紙箱裡睡覺", false)])))
    let url = h.dir.appending(path: "calendar.json")
    #expect(Concerns.boxed(url))
    #expect(try plainHits(h, ["榮總", "胃鏡"]) == [])
    // A new process reads it back; its start fires a sealed 【行事曆】 event.
    let again = try await Harness(dir: h.dir, clock: clock)
    #expect(try Agenda(url: url, box: testBox(h.dir)).item(1)?.title == "榮總做胃鏡")
    _ = try await again.doll.calendarCatchUp()
    clock.set(at("2026-11-16", "09:01"))
    try await again.doll.fireDue()
    let fired = await again.doll.queue.last!
    #expect(fired.kind == .calendar && fired.sealed && fired.text.contains("榮總做胃鏡"))
}
