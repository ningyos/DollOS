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

/// `key("cmd+s")` → key(key: "s", modifiers: ["cmd"]); a lone key has no modifiers.
private func key(_ chord: String) -> (String, [(String, String)]) {
    let parts = chord.split(separator: "+").map(String.init)
    let mods = parts.dropLast().map { "\"\($0)\"" }.joined(separator: ", ")
    return ("key", [("key", parts.last!)] + (mods.isEmpty ? [] : [("modifiers", "[\(mods)]")]))
}
private func watch(_ target: String, _ maxWait: Int? = nil) -> (String, [(String, String)]) {
    ("watch", [("target", target)] + (maxWait.map { [("max_wait_s", "\($0)")] } ?? []))
}
/// `todo.set(items)`; the default item stands for what `use_computer(expect)` said before 2026-11-14.
private func todoSet(_ items: String...) -> (String, [(String, String)]) {
    let list = items.isEmpty ? ["畫面上看得到結果"] : items
    return ("todo.set", [("items", String(decoding: try! JSONEncoder().encode(list), as: UTF8.self))])
}
private func done(_ result: String) -> (String, [(String, String)]) { ("todo.done", [("result", result)]) }
private func fail(_ reason: String, tried: String = "試過了") -> (String, [(String, String)]) {
    ("todo.fail", [("reason", reason), ("tried", tried)])
}
private func todoWait(_ reason: String) -> (String, [(String, String)]) { ("todo.wait", [("reason", reason)]) }
private func progress(_ note: String) -> (String, [(String, String)]) { ("todo.progress", [("note", note)]) }
private let screenshotCall: (String, [(String, String)]) = ("screenshot", [])
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
        if stallNext {
            stallNext = false
            try await Task.sleep(for: .seconds(30))
        }
        guard !replies.isEmpty else { throw ModelError("script ran out at request \(requests.count)") }
        let r = replies.removeFirst()
        let prompt = messages.reduce(0) { $0 + estimateTokens($1.content) + 4 + $1.images.count * imageTokens }
        return ModelReply(content: r.content, calls: r.toolCalls, usage: ModelUsage(promptTokens: prompt, completionTokens: 10))
    }

    func stall() { stallNext = true }

    func add(_ more: [Scripted]) { replies += more }
    func seen(_ i: Int) -> [ChatMessage] { requests[i].messages }
}

private final class Recorder: Sendable {
    let said = Mutex<[String]>([])
    /// Who each `said` was delivered to (`discord.dm` → owner, `dollctl.reply` → developer).
    let saidTo = Mutex<[Addressee]>([])
    /// Set: every delivery fails with this reason (the bridge is down, no chat open).
    let undelivered = Mutex<String?>(nil)
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
private let qwen3 = Task { try await MLXEmbedder.load(try Config.load(root.appending(path: "dollcore.example.toml")).memory) }

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

    init(_ replies: [Scripted] = [], embedder: (any Embedder)? = nil, tune: (inout Config) -> Void = { _ in }) async throws {
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
        doll = try Doll(config: cfg, root: root, model: model, selector: selector, embedder: embedder ?? FakeEmbedder(), computer: vm,
                        deliver: { text, to in
                            rec.said.withLock { $0.append(text) }
                            rec.saidTo.withLock { $0.append(to) }
                            return rec.undelivered.withLock { $0 }
                        })
        await doll.setObserver { r in rec.reports.withLock { $0.append(r) } }
        await doll.setLogSink { line in rec.logs.withLock { $0.append(line) } }
        store = try Store(path: dir.appending(path: "doll.db"))
    }

    var said: [String] { rec.said.withLock { $0 } }
    var saidTo: [Addressee] { rec.saidTo.withLock { $0 } }
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
    let cfg = try Config.load(root.appending(path: "dollcore.example.toml"))
    let specs = Tools.computerSpecs(cfg.computer, cfg.files)
    func decode(_ params: [(String, String)]) throws -> Tools.Request {
        try Tools.decode(call("key", params), specs: specs)
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
    let specs = Tools.specs(memory: try Config.load(root.appending(path: "dollcore.example.toml")).memory, voice: false, computer: ComputerConfig(), files: FilesConfig())
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
        guard case .todoSet(let got) = try Tools.decode(c, specs: specs) else { Issue.record("not todo.set"); return }
        #expect(got == ["a", "b"])
    }
}

@Test func aFailedCallsResultIsItsProblem() throws {
    let specs = Tools.specs(memory: try Config.load(root.appending(path: "dollcore.example.toml")).memory, voice: false, computer: ComputerConfig(), files: FilesConfig())
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
    #expect(names == ["discord.dm", "dollctl.reply", "think", "remember", "recall", "todo.set", "todo.done", "todo.fail",
                      "todo.wait", "todo.progress", "click", "type", "key", "scroll", "drag", "read_file", "write_file",
                      "edit_file", "screenshot", "watch"])
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
                              remember(kinds: [("會失敗的記憶", false, "done"), ("主人喜歡貓", false, nil)]), ("recall", []), todoSet(), screenshotCall,
                              ("remember", [("items", "主人養貓")])))
    let results = try h.toolResults()
    #expect(results.count == 8)
    #expect(results[0].text == "失敗：沒有 bogus 這個工具")
    #expect(results[1].text == "失敗：recall 沒有 q 這個參數")
    #expect(results[2].text == "失敗：think 的 level 只能是 medium 或 high，收到 low")
    #expect(results[3].text.hasPrefix("第 1 條沒存：kind 只能是 happened、intended、uncertain，或不填；收到 done\n第 2 條已存入 public/"))
    #expect(results[4].text == "失敗：recall 少了參數 query")
    #expect(results[5].text.hasPrefix("todo 列好了。"))                     // later calls still run
    #expect(results[6].text.hasPrefix("現在的畫面：\n"))
    #expect(results[7].text.hasPrefix("失敗：remember 的 items 要是 JSON 陣列"))
    #expect(h.last.actions == ["error bogus", "error recall", "error think", "error remember", "remember public",
                               "error recall", "todo set", "screenshot", "error remember"])
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
    try await h.turn(Scripted("好", remember([("主人養貓", false)]), todoSet(), screenshotCall))
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
    // The system prompt is the same on every turn, and so is `tools`.
    #expect(Set(reqs.map { $0[0] }.map { "\($0.role)|\($0.content)" }).count == 1)
    let toolLists = Set(await h.model.tools)
    #expect(reqs[0][0].role == "system" && reqs[0][0].content.hasPrefix("你是") && toolLists.count == 1)
}

@Test func toolResultsGiveHerTheNextTurnButAWatchAckAloneDoesNot() async throws {
    let h = try await Harness([Scripted("", todoSet(), screenshotCall), Scripted("", dm("盯著"), watch("window", 1)),
                               Scripted("", dm("沒變"), done("盯完了，畫面沒變"))])
    let doll = h.doll
    let runner = Task { try await doll.run() }
    // Poll instead of fixed sleeps: a busy parallel suite made the 500 ms waits race.
    func waitFor(_ n: Int, said s: Int, _ seconds: Double = 20) async throws {
        let deadline = ContinuousClock.now + .seconds(seconds)
        while (await h.model.requests.count < n || h.said.count < s), ContinuousClock.now < deadline {
            try await Task.sleep(for: .milliseconds(20))
        }
    }
    await h.doll.ownerSaid("開電腦盯著畫面")
    try await waitFor(2, said: 1)
    #expect(await h.model.requests.count == 2 && h.said == ["盯著"])     // the results woke her once
    try await Task.sleep(for: .milliseconds(200))                       // well inside max_wait (1 s)
    #expect(await h.model.requests.count == 2)                          // neither the watch ack nor say woke her: 等待中
    try await waitFor(3, said: 2)                                       // max_wait passing does
    #expect(await h.model.requests.count == 3 && h.said.last == "沒變")
    await h.doll.stop()
    try await runner.value
}

// queue and turns ------------------------------------------------------------

@Test func drainOrdersByConfiguredPriority() async throws {
    let h = try await Harness()
    await h.doll.put(Event(.watch, .internal, "r"))
    await h.doll.developerSaid("dev")
    await h.doll.ownerSaid("text")
    await h.doll.ownerSaid("voice", voice: true)
    try await h.turn("好")
    let events = try h.events()
    // Developer text sits just below owner text by default (2026-11-12).
    #expect(events.map(\.text) == ["voice", "text", "dev", "r", "好"])
    #expect(events.map(\.priority) == [.ownerVoice, .ownerText, .developerText, .watch, .watch])
}

@Test func ownerVoiceCancelsTheCallAndTheRerunSeesTheVoiceWithTheSameEffort() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("這題難")
    try await h.turn(Scripted("", dm("等我"), think("high")))         // next call is high
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
    #expect(reqs[1].effort == "high" && reqs[2].effort == "high")    // the think survives the cancelled call
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
            && first.contains("- watch：沒有") && first.contains("- context：") && first.contains("下一回合 effort：medium")
            && !first.contains("選單"))
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
    try await h.turn(Scripted("", dm("等我"), think("high")))
    let status = try await h.doll.status()
    #expect(h.last.actions == ["said discord_dm", "think high"] && status.contains("effort：high"))
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

/// A harness whose first turn listed one todo item (in progress) and took a screenshot.
private func menuHarness(tune: (inout Config) -> Void = { _ in }) async throws -> Harness {
    let h = try await Harness(tune: tune)
    await h.doll.ownerSaid("用電腦")
    try await h.turn(Scripted("", todoSet(), screenshotCall))
    return h
}

@Test func onlyTheLastImageTurnsKeepTheirScreenshotsAndThePrefixBreaksOnlyThere() async throws {
    // spec 3.1, 2026-11-09: image_turns_kept = 2 here (the default 0 keeps every turn's images: on
    // Qwen3-Next the cache resumes only from checkpoints). A turn's images are the results after her reply.
    // Request k is the one that produced turn k+1's reply, so it holds the results up to turn k.
    let h = try await Harness(tune: { $0.computer.imageTurnsKept = 2 })
    await h.doll.ownerSaid("用電腦")
    try await h.turn(Scripted("", todoSet(), screenshotCall))                     // turn 1: image turn A (its screenshot)
    try await h.turn(Scripted("", key("cmd+n")))                                  // turn 2: image turn B
    try await h.turn(Scripted("", key("cmd+s"), screenshotCall))                  // turn 3: image turn C, two results with images
    try await h.turn(Scripted("", screenshotCall))                                // turn 4
    let two = await h.model.seen(2), three = await h.model.seen(3)
    // Request 2 holds A and B: both carry images. Request 3 holds A, B, C: A's images are gone, B's
    // and C's stay; A's result keeps its text and says where the images were.
    #expect(two.filter { !$0.images.isEmpty }.count == 2)
    let shotA = three.first { $0.content.hasPrefix("現在的畫面：") }!
    #expect(shotA.images.isEmpty && shotA.extra.contains(.text(Doll.imagesPutAway)))
    let firstLine = { (m: ChatMessage) in String(m.content.split(separator: "\n").first ?? "") }
    #expect(three.filter { !$0.images.isEmpty }.map(firstLine) == [fakeWindowsLine, fakeWindowsLine, "現在的畫面："])
    // Everything before A's result is byte-identical; A's result is the first difference; the text
    // after it is unchanged.
    let a = two.firstIndex { $0.content.hasPrefix("現在的畫面：") }!
    #expect(Array(three.prefix(a)) == Array(two.prefix(a)) && three[a] != two[a])
    #expect(three[a].content == two[a].content && three[a + 1...a + 2].map(\.content) == two[a + 1...a + 2].map(\.content))
    // The files stay.
    #expect(try h.events().filter { $0.image != nil }.count == 5)
    // image_turns_kept = 1: in request 3 only C's results carry images.
    let one = try await Harness(tune: { $0.computer.imageTurnsKept = 1 })
    await one.doll.ownerSaid("用電腦")
    try await one.turn(Scripted("", todoSet(), screenshotCall))
    try await one.turn(Scripted("", key("cmd+n")))
    try await one.turn(Scripted("", key("cmd+s"), screenshotCall))
    try await one.turn(Scripted("", screenshotCall))
    #expect((await one.model.seen(3)).filter { !$0.images.isEmpty }.map(firstLine) == [fakeWindowsLine, "現在的畫面："])
    // The default keeps them all.
    let all = try await Harness()
    #expect(try Config.load(root.appending(path: "dollcore.example.toml")).computer.imageTurnsKept == 0)
    await all.doll.ownerSaid("用電腦")
    try await all.turn(Scripted("", todoSet(), screenshotCall))
    try await all.turn(Scripted("", key("cmd+n")))
    try await all.turn(Scripted("", key("cmd+s"), screenshotCall))
    try await all.turn(Scripted("", screenshotCall))
    #expect((await all.model.seen(3)).filter { !$0.images.isEmpty }.count == 4)
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
    #expect(await h.doll.itemSteps.map(\.line) == ["click「Save」 right 雙擊", "type 「sw_vers\n」", "key cmd+s"])
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
    #expect(await h.doll.itemSteps.map(\.line) == ["key cmd+shift+s", "click「Save」", "type 「copy」", "key cmd+shift+s"])
    // Bad arguments after a text-only step: the failure shows the screen, since nothing of this reply did yet.
    try await h.turn(Scripted("", key("Return"), ("click", [("x", "99999"), ("y", "1")]), key("Escape")))
    let bad = Array(try h.toolResults().suffix(3))
    #expect(bad[0].text == "" && bad[0].image == nil)
    #expect(bad[1].text.hasPrefix("失敗：沒有點：99999,1 在螢幕外") && bad[1].text.hasSuffix("\n" + fakeWindowsLine) && bad[1].image != nil)
    #expect(bad[2].text == "失敗：前面第 2 步（click）失敗了，這一步沒做" && bad[2].image == nil)
    #expect(h.vm.actions.count == 6)
    // todo.done after actions in the same reply still ends the item; the action before it is the last.
    try await h.turn(Scripted("", key("cmd+s"), done("存好了"), key("cmd+q")))
    let end = Array(try h.toolResults().suffix(3))
    #expect(end[0].image != nil && end[0].text == fakeWindowsLine)
    #expect(end[1].text.hasPrefix("完成，記下了。")
            && end[2].text == "失敗：這則回覆前面已經用 todo.done 收起這一項了，後面的呼叫沒有執行；看了結果下一回合再做。")
    #expect(h.last.itemEnd == "完成" && h.last.experience?.hasSuffix("步驟：\n1. key cmd+shift+s\n2. click「Save」\n3. type 「copy」\n4. key cmd+shift+s\n5. key return\n6. key cmd+s") == true)
    #expect(await h.doll.todo.phase != .doing && h.vm.actions.count == 7)
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
    let results = Array(try h.toolResults().suffix(3))                 // the first screenshot's, key's, screenshot's
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
    #expect(try h.toolResults().last!.text
            == "開始盯著0,0,100,50，最多等 60 秒；有變化或時間到會叫你。這則回覆的呼叫做完以後，這一項就是等待中。")
    #expect(await !h.doll.followUp)                                     // the ack alone is no new turn
    await h.doll.ownerSaid("換盯那裡")                                  // waiting: a line puts the item back in progress
    try await h.turn(Scripted("", watch("10,10,20,20", 60)))
    #expect(try h.toolResults().last!.text.contains("（取代了上一個 watch）"))
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

@Test func failingTheItemStopsTheWatch() async throws {
    let h = try await menuHarness()
    try await h.turn(Scripted("", watch("window", 60)))
    await h.doll.ownerSaid("不用等了")                                    // 等待中: his line puts the item back in progress
    try await h.turn(Scripted("", fail("沒好")))
    #expect(h.last.itemEnd == "失敗")
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
    let memory = try Memory(config: cfg, root: h.dir.appending(path: "memory"), store: h.store, embedder: FakeEmbedder())
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
    try await h.turn(Scripted("", todoSet(), screenshotCall))
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
    let again = try Doll(config: cfg, root: root, model: FakeModel([]), selector: FakeSelector(), embedder: FakeEmbedder(),
                         computer: h.vm, deliver: { _, _ in nil })
    try await again.learnFrontApp()
    #expect(h.vm.state.withLock { $0.menuReads }.count == 3)
}

@Test func learnShortcutsOffReadsNoMenus() async throws {
    let off = try await Harness(tune: { $0.computer.learnShortcuts = false })
    off.vm.setApp(textEdit, textEditKeys)
    await off.doll.ownerSaid("用電腦")
    try await off.turn(Scripted("", todoSet(), screenshotCall))
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
    try await h.turn(Scripted("", todoSet(), screenshotCall))
    // The owner's line: the experience only. The screenshot shows TextEdit (a switch, its shortcuts
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
    try await h.turn(Scripted("", dm("好")))
    #expect(try memoryEvents(h).count == 2)
}

@Test func attachedMemoriesAreResentByteIdentical() async throws {
    let h = try await Harness()
    h.vm.setApp(textEdit, textEditKeys)
    try await seedMemory(h, "public", "經驗：用電腦存檔\n1. key cmd+s → Done")
    await h.doll.ownerSaid("用電腦存檔")
    try await h.turn(Scripted("", todoSet(), screenshotCall))
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
    let h = try await Harness()
    await h.doll.ownerSaid("把 notes 存檔")
    try await h.turn(Scripted("", todoSet("標題沒有「已編輯」")))
    // An action that fails is not a step; each step is only what she did, no VM answer text.
    h.vm.failNext("boom\nsecond line")
    try await h.turn(Scripted("", ("click", [("text", "Save")]), screenshotCall))
    h.vm.setSizes(screen: (1280, 800), crop: (580, 360))
    try await h.turn(Scripted("", ("click", [("element", "dockitem \"Finder\"")]), screenshotCall))
    try await h.turn(Scripted("", ("click", [("x", "100"), ("y", "50"), ("on", "window"), ("count", "2")]),
                              ("type", [("text", "娃娃來過")]), key("cmd+s")))
    // She looks, then answers in the same reply.
    try await h.turn(Scripted("", screenshotCall, done("按 ⌘S 存了檔，看標題列確認沒有「已編輯」。\n1. 這行是我寫的，不是步驟")))
    #expect(h.last.footer.contains("- todo：進行中「標題沒有「已編輯」」"))
    #expect(try h.toolResults().last!.text.hasPrefix("完成，記下了。這次的經過和步驟存成一條經驗。"))
    let text = "經驗：把 notes 存檔\n這一項：標題沒有「已編輯」\n按 ⌘S 存了檔，看標題列確認沒有「已編輯」。\n1. 這行是我寫的，不是步驟\n步驟：\n"
        + "1. click dockitem \"Finder\"\n2. click 60,45 雙擊\n3. type 「娃娃來過」\n4. key cmd+s"
    #expect(h.last.itemEnd == "完成" && h.last.experience == text && h.last.actions.contains("todo done"))
    let mems = try experiences(h)
    let conf = try Config.load(root.appending(path: "dollcore.example.toml")).memory.confidence[.selfReported]!
    #expect(mems.count == 1 && mems[0].text == text && mems[0].confidence == conf && mems[0].kind == "")
    #expect(conf >= 0.3 && conf < 0.7)
    // The list is done: idle, the computer refused again.
    try await h.turn(Scripted("", key("Return")))
    #expect(h.last.actions == ["refused key"] && h.last.footer.contains("- todo：待命中"))
    // Its stats are in the front-matter, not in the text (what is embedded): this item's 5 turns.
    let files = try experienceFiles(h)
    let stats = try #require(files.first?.stats)
    #expect(files.count == 1 && files[0].task == "把 notes 存檔" && files[0].body == text && !files[0].body.contains("成績"))
    // Only the program's steps count as steps, not a numbered line in her result.
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

@Test func aFailedItemOrOneWithoutTheComputerWritesNoExperience() async throws {
    let h = try await menuHarness()
    try await h.turn(Scripted("", key("cmd+s"), fail("還有點")))
    #expect(h.last.itemEnd == "失敗" && h.last.experience == nil)
    #expect(try experiences(h).isEmpty)
    // An item that never used the computer: done, no experience.
    try await h.turn(Scripted("", todoSet("跟主人說好了")))
    try await h.turn(Scripted("", dm("好"), done("說了")))
    #expect(h.last.itemEnd == "完成" && h.last.experience == nil)
    #expect(try experiences(h).isEmpty)
    // An item still in progress when the program stops: the watch stops, it stays in the timeline's todo.
    try await h.turn(Scripted("", todoSet(), screenshotCall))
    try await h.turn(Scripted("", key("Return")))
    await h.doll.shutdown()
    #expect(await h.doll.watch == nil)
    #expect(h.logs.contains { $0.contains("todo: stopping with「畫面上看得到結果」進行中") })
    #expect(try h.timeline().todo.phase == .doing)
    #expect(try experiences(h).isEmpty)
}

@Test func doneEndsTheListAndNothingWakesHerForIt() async throws {
    let h = try await Harness([Scripted("", todoSet(), screenshotCall), Scripted("", dm("存好了"), key("cmd+s"), done("看到了"))])
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
    #expect(h.last.itemEnd == "完成" && h.vm.actions == [.key("cmd+s")])
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
    try await h.turn(Scripted("", key("cmd+s"), done("存好了"), key("Return"), screenshotCall))
    let refused = "失敗：這則回覆前面已經用 todo.done 收起這一項了，後面的呼叫沒有執行；看了結果下一回合再做。"
    let results = try h.toolResults().suffix(3).map(\.text)
    #expect(results.first!.hasPrefix("完成，記下了。") && Array(results.dropFirst()) == [refused, refused])
    #expect(h.last.actions == ["key", "todo done", "error key", "error screenshot"])
    #expect(h.vm.actions == [.key("cmd+s")])
    #expect(await !h.doll.followUp)
    #expect(await h.doll.todo.phase == .idle)
    // The next request: the item is compacted to her todo calls and their results; the refused
    // calls and the screenshots are not in it.
    await h.doll.ownerSaid("好吧")
    try await h.turn("嗯")
    let request = await h.model.requests.last!.messages
    #expect(request.contains { $0.role == "tool" && $0.content.hasPrefix("todo 列好了。") })
    #expect(request.contains { $0.role == "tool" && $0.content.hasPrefix("完成，記下了。") })
    #expect(!request.contains { $0.content.hasPrefix("失敗：這則回覆前面已經用") || !$0.images.isEmpty })
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

@Test func aFollowedExperienceCountsHowHerItemEndedAndRanksBy() async throws {
    let h = try await Harness()
    let task = "把 notes 存檔"
    try await seedExperience(h, "1-seed", task: task, steps: ["click dockitem \"Finder\"", "key cmd+s", "key cmd+w", "type 「娃娃來過」"],
                             stats: ExperienceStats(turns: 9, seconds: 150))
    // 1: two of its four steps in order (ratio 0.5 = experience_follow_ratio): followed, her success counted.
    await h.doll.ownerSaid(task)
    try await h.turn(Scripted("", todoSet(), screenshotCall))
    let attached = try #require(h.reports[0].attached.first)
    #expect(attached.contains("成績：這次 9 回合、150 秒；附上後照做 0 次，成功 0 次"))
    try await h.turn(Scripted("", ("click", [("element", "dockitem \"Finder\"")]), key("cmd+s")))
    try await h.turn(Scripted("", done("存好了")))
    #expect(h.last.follows == ["experience 1-seed: 2/4 steps done in order (ratio 0.50), followed, counted success: 照做 1、成功 1、失敗 0"])
    #expect(h.logs.contains { $0.contains("experience 1-seed: 2/4 steps done in order (ratio 0.50)") })
    #expect(try statsOf(h, "1-seed") == ExperienceStats(turns: 9, seconds: 150, attachedFollowed: 1, attachedSuccess: 1))
    // The attached event in the timeline is not re-rendered with the new stats: every request while
    // the item runs starts with the whole one before it, and the first after it with the one that
    // listed it, up to that reply's footer (the item's turns are compacted).
    try await h.turn("好了")
    #expect(try memoryEvents(h).first?.text == attached)
    let reqs = await h.model.requests.map(\.messages)
    for i in 1..<3 { #expect(Array(reqs[i].prefix(reqs[i - 1].count)) == reqs[i - 1]) }
    #expect(reqs.count == 4 && Array(reqs[3].prefix(reqs[0].count - 1)) == Array(reqs[0].dropLast()))
    // Her new experience (3 turns: todo.set, the actions, todo.done) now leads the group.
    let fresh = try #require(experienceFiles(h).first { $0.task == task && $0.stats?.turns == 3 })
    #expect(fresh.stats == ExperienceStats(turns: 3, seconds: fresh.stats!.seconds))

    // 2: the new one is attached; none of its steps done: not followed, nothing counted.
    await h.doll.ownerSaid(task)
    try await h.turn(Scripted("", todoSet(), screenshotCall))
    #expect(h.last.attached.count == 1 && h.last.attached[0].contains("成績：這次 3 回合") && !h.last.attached[0].contains("9 回合"))
    try await h.turn(Scripted("", key("cmd+q"), fail("沒存到")))
    #expect(h.last.follows.count == 1 && h.last.follows[0].hasSuffix("0/2 steps done in order (ratio 0.00), not followed"))
    #expect(try experienceFiles(h).first { $0.stats?.turns == 3 }?.stats?.attachedFollowed == 0)

    // 3: followed, and she says it failed: counted as a failure, and the failure demotes it below
    // the 9-turn one that succeeded.
    await h.doll.ownerSaid(task)
    try await h.turn(Scripted("", todoSet(), screenshotCall))
    try await h.turn(Scripted("", ("click", [("element", "dockitem \"Finder\"")]), key("CMD+S"), fail("沒存到")))
    #expect(h.last.follows.count == 1 && h.last.follows[0].contains("2/2 steps done in order (ratio 1.00), followed, counted failure: 照做 1、成功 0、失敗 1"))
    let ranking = try await h.doll.experienceRanking()
    #expect(ranking.count == 1 && ranking[0].count == 2 && ranking[0][0].hasPrefix("1-seed: 成績：這次 9 回合、150 秒；附上後照做 1 次，成功 1 次；失敗 0 次"))
    #expect(ranking[0][1].contains("3 回合") && ranking[0][1].hasSuffix("附上後照做 1 次，成功 0 次；失敗 1 次"))
    // An item still in progress when the program stops: only logged.
    await h.doll.ownerSaid(task)
    try await h.turn(Scripted("", todoSet(), screenshotCall))
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
    try await h.turn(Scripted("", todoSet(), screenshotCall))
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

@Test func theFrontAppAtAnItemsStartGetsItsCheatsheetEvenIfItWasInFrontBefore() async throws {
    // Chained E→F (2026-11-09): TextEdit stayed in front across the two tasks, so F got no sheet;
    // the first task's sheet had left the context with it. A new item resets what counts as a switch.
    let h = try await Harness(tune: { $0.computer.cheatsheet = true })
    h.vm.setApp(textEdit, textEditKeys)
    await h.doll.ownerSaid("先存檔")
    try await h.turn(Scripted("", todoSet(), screenshotCall))
    try await h.turn(Scripted("", key("cmd+s"), done("存了")))
    #expect(try sheets(h) == [textEditSheet])
    await h.doll.ownerSaid("再另存一份")
    try await h.turn(Scripted("", todoSet(), screenshotCall))
    #expect(try sheets(h) == [textEditSheet, textEditSheet] && h.last.cheatsheets == [textEditSheet])
    // The same owner line, a third item: again, since the second's sheet is gone from the context too.
    try await h.turn(Scripted("", done("好了")))
    try await h.turn(Scripted("", todoSet(), screenshotCall))
    #expect(try sheets(h).count == 3)
    #expect(h.vm.state.withLock { $0.menuReads } == ["com.apple.TextEdit"])
}

@Test func cheatsheetsAreResentByteIdentical() async throws {
    let h = try await Harness(tune: { $0.computer.cheatsheet = true })
    h.vm.setApp(textEdit, textEditKeys)
    await h.doll.ownerSaid("用電腦存檔")
    try await h.turn(Scripted("", todoSet(), screenshotCall))
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
    try await h.turn(Scripted("", todoSet(), screenshotCall))
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
    try await h.turn(Scripted("", todoSet(), screenshotCall))
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

@Test func fileToolsAreRefusedWithoutAnItemInProgressAndRefuseNonUTF8Files() async throws {
    let h = try await Harness()
    h.vm.put(todoPath, todoBefore)
    await h.doll.ownerSaid("看一下 todo")
    try await h.turn(Scripted("", readFile(todoPath), writeFile("~/a.txt", "a"), editFile(todoPath, "買牛奶", "買豆漿")))
    let refused = { (tool: String) in "失敗：現在沒有進行中的 todo，\(tool) 不能用。先用 todo.set 列 todo（小事就一項，每項寫做完的樣子），再做。" }
    #expect(try h.toolResults().suffix(3).map(\.text) == [refused("read_file"), refused("write_file"), refused("edit_file")])
    #expect(h.last.actions == ["refused read_file", "refused write_file", "refused edit_file"])
    #expect(h.vm.file(todoPath) == todoBefore && h.vm.file("~/a.txt") == nil)
    try await h.turn(Scripted("", todoSet(), screenshotCall))
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
    try await h.turn(Scripted("", todoSet("todo.txt 第一行是買豆漿")))
    try await h.turn(Scripted("", readFile(todoPath), writeFile("~/Desktop/a.txt", "一\n二\n"), editFile(todoPath, "沒有這段", "x"),
                              editFile(todoPath, "買牛奶", "買豆漿"), done("第一行是買豆漿")))
    #expect(h.last.experience == "經驗：改 todo\n這一項：todo.txt 第一行是買豆漿\n第一行是買豆漿\n步驟：\n1. read_file ~/Documents/todo.txt\n2. write_file ~/Desktop/a.txt（2 行）\n3. edit_file ~/Documents/todo.txt")
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
    let tools = Tools.json(Tools.todoSpecs)
    let history = [ChatMessage(role: "system", content: "s"),
                   ChatMessage(role: "assistant", content: "想", toolCalls: [ToolCall(name: "recall", arguments: "{\"query\": \"貓\"}"),
                                                                           ToolCall(failed: "", ToolCall.unreadable)]),
                   ChatMessage(role: "tool", content: "r")]
    let r = try await model.chat(history, tools: tools, effort: "none")
    #expect(r.content.hasPrefix("嗯\n<tool_call>"))
    #expect(r.calls.map(\.name) == ["discord.dm", "voice.join", "bogus", ""])
    #expect(r.calls[0].arguments == "{\"text\": \"在\"}" && r.calls[0].parameters == [.init("text", "在")])
    #expect(r.calls[2].malformed == "沒有 bogus 這個工具，沒有執行" && r.calls[3].malformed == ToolCall.unreadable)
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
    try await h.turn(Scripted("好，我看看", todoSet(), screenshotCall,
                              remember(folders: [("我的信箱密碼是 hunter2", "own", nil), ("主人怕打雷", "secret", "happened"),
                                                 ("天空是藍的", "public", nil)])))
    h.vm.setFrame(1)
    try await h.turn(Scripted("", key("cmd+space")))

    let s = try json(DashboardStatus.self, await d.respond(method: "GET", target: "/api/status"))
    #expect(s.todo == "進行中" && s.item == "畫面上看得到結果" && s.pending.isEmpty && s.watch == nil)
    #expect(s.state == "asleep" && !s.paused && s.turn == 2 && s.queued == 0 && s.effort == "medium" && s.contextTokens > 0)

    let t = try json([DashboardItem].self, await d.respond(method: "GET", target: "/api/timeline"))
    #expect(t.map(\.type) == ["turn", "turn", "owner"])
    #expect(t[2].text == "用電腦看一下 macOS 版本" && t[2].voice == false)
    #expect(t[1].thought == "好，我看看" && t[1].calls?.map(\.name) == ["todo.set", "screenshot", "remember"])
    #expect(t[1].calls?.allSatisfy { $0.result != nil } == true && t[1].calls?[0].image == nil && t[1].calls?[1].image != nil
            && t[1].calls?[2].image == nil)
    #expect(t[1].calls?[2].result?.contains("own/") == true)
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
    try await h.turn(Scripted("", todoSet(), screenshotCall))
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
    #expect(s.state == "asleep" && s.turn == 0 && s.lastInterrupt != nil)
    #expect(h.logs.contains { $0.contains("turn 1 interrupted from the dashboard") })
    await doll.ownerSaid("好了嗎")                                                  // the next event gets a turn
    while h.said.isEmpty { try await Task.sleep(for: .milliseconds(10)) }
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
    try await h.turn(Scripted("好，我來", todoSet(), screenshotCall))
    try await h.turn(Scripted("", key("cmd+s")))
    try await h.turn(Scripted("", dm("存好了。"), done("按 ⌘S 存了檔。")))
    await h.doll.ownerSaid("謝謝")
    try await h.turn(Scripted("", dm("不客氣")))
    await h.doll.ownerSaid("再開一次電腦")
    try await h.turn(Scripted("", todoSet(), screenshotCall))                                    // an item in progress, with images
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

@Test func aSayAfterDoneIsStillSaidAndKeptWhenTheItemIsCompacted() async throws {
    // 2026-11-11: she wrote her words after ending the work; the owner heard nothing.
    let h = try await menuHarness()
    try await h.turn(Scripted("", done("存好了"), dm("存好了喔"), key("cmd+q")))
    #expect(h.said == ["存好了喔"] && h.vm.actions.isEmpty)
    let end = Array(try h.toolResults().suffix(3))
    #expect(end[0].text.hasPrefix("完成，記下了。") && end[1].text == ""
            && end[2].text == "失敗：這則回覆前面已經用 todo.done 收起這一項了，後面的呼叫沒有執行；看了結果下一回合再做。")
    #expect(h.last.actions == ["todo done", "said discord_dm", "error key"])
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
        stamped(Scripted("我先列 todo。", todoSet()), 2),
        stamped(.tool, "todo 列好了。現在進行中：「畫面上看得到結果」；後面：（沒有）。", 3),
        stamped(.memory, "【快捷鍵】TextEdit 1.21\nFile › Save As… = ⇧⌥⌘S", 4),
        stamped(Scripted("", dm("好"), key("cmd+shift+alt+s")), 5),
        stamped(.tool, "", 6),
        stamped(.tool, "開著的視窗：TextEdit「另存」；焦點：textfield", 7, image: "b.jpg", cropNote: "視窗圖在 10,20",
                crop: "c.jpg", window: "前景 app：TextEdit\nwindow \"Save\""),
        stamped(.memory, "【相關記憶】程式用主人的話查到的過去經驗：\n〔大概｜public〕\n經驗：x", 8),
        stamped(Scripted("", recall("另存")), 9),
        stamped(.tool, "另存：1 條\n\n〔大概｜public〕\n舊的", 10),
        stamped(Scripted("", done("按 ⇧⌥⌘S 另存好了")), 11),
        stamped(.tool, "完成，記下了。", 12),
        stamped(.owner, "我的密碼是 1234", 13, secret: true),
        stamped(.watch, "盯著的畫面變了。", 14, image: "d.jpg"),
    ]
    func t(_ seq: Int) -> String { hhmm(events[seq - 1].ts) }
    let tasks = Consolidation.tasks(events)
    #expect(tasks.count == 2 && tasks.allSatisfy(\.owner))
    #expect(tasks[0].text == """
        [\(t(1)) 主人] 用 TextEdit 另存一份
        [\(t(2)) 她想] 我先列 todo。
        [\(t(2)) 她呼叫] todo.set(items: ["畫面上看得到結果"])
        [\(t(3)) todo.set 的結果] todo 列好了。現在進行中：「畫面上看得到結果」；後面：（沒有）。
        [\(t(5)) 她說｜Discord 私訊] 好
        [\(t(5)) 她呼叫] key(key: s; modifiers: ["cmd", "shift", "alt"])
        [\(t(7)) key 的結果] 開著的視窗：TextEdit「另存」；焦點：textfield
        [\(t(9)) 她呼叫] recall(query: 另存)
        [\(t(11)) 她呼叫] todo.done(result: 按 ⇧⌥⌘S 另存好了)
        [\(t(12)) todo.done 的結果] 完成，記下了。
        """)
    #expect(tasks[0].queries == ["用 TextEdit 另存一份", "按 ⇧⌥⌘S 另存好了"])
    #expect(tasks[1].text == "[\(t(13)) 主人｜秘密] 我的密碼是 1234\n[\(t(14)) watch] 盯著的畫面變了。")
    // Nothing of the images, the window outline, the program's lines or recall's copy.
    let all = tasks.map(\.text).joined(separator: "\n")
    for gone in [".jpg", "視窗圖在", "window \"Save\"", "【快捷鍵】", "【相關記憶】", "另存：1 條"] {
        #expect(!all.contains(gone), "\(gone)")
    }
    // Events before the first owner line are a task of their own.
    let early = Consolidation.tasks([stamped(.watch, "醒了", 1), stamped(.owner, "嗨", 2)])
    #expect(early.map(\.owner) == [false, true] && early[0].queries.isEmpty)
}

@Test func consolidationAppliesAddUpdateRetireAndTheNextStartIsANewDay() async throws {
    let h = try await Harness()
    let cfg = try consolidationConfig(h)
    let memory = try Memory(config: cfg.memory, root: h.dir.appending(path: "memory"), store: h.store, embedder: FakeEmbedder())
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
    let model = FakeModel([Scripted(reply)])
    let r = try await Consolidation.run(config: cfg, root: root, model: model, embedder: FakeEmbedder())

    // The request: no persona, high effort, the day as text, only memories she or consolidation
    // wrote, with their entities by registry id, and the registry entries near the day.
    let sent = await model.requests[0]
    #expect(sent.effort == "high" && sent.messages.map(\.role) == ["system", "user"])
    #expect(!sent.messages[0].content.contains("你是") && sent.messages[1].content.contains("[id: \(cat)｜public｜kind: ｜entities: []]\n主人養貓"))
    #expect(sent.messages[1].content.contains(#"[id: \#(saveAs)｜public｜kind: ｜entities: [{"id":"\#(app.id)","name":"TextEdit"}]]"#))
    #expect(sent.messages[1].content.contains("[id: \(app.id)｜app｜TextEdit｜別名：（沒有）] macOS 內建的文字編輯 app"))
    #expect(sent.messages[1].content.contains("] 用 TextEdit 另存一份") && sent.messages[1].content.contains("她說｜Discord 私訊] 好"))
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
    #expect(try h.timeline().segment == r.newSegment)   // `next` runs on h's data dir
    #expect(await next.doll.timelineEvents().isEmpty)
    #expect(try await next.doll.todayCount() == 0)
    #expect(FileManager.default.fileExists(atPath: h.dir.appending(path: "timeline/\(before).jsonl").path))
}

@Test func aFailedOrMalformedConsolidationWritesNothingAndKeepsTheDay() async throws {
    let h = try await Harness()
    let cfg = try consolidationConfig(h)
    // Nothing in the timeline yet: nothing to consolidate.
    await #expect(throws: ConsolidationError.self) {
        try await Consolidation.run(config: cfg, root: root, model: FakeModel([]), embedder: FakeEmbedder())
    }
    let memory = try Memory(config: cfg.memory, root: h.dir.appending(path: "memory"), store: h.store, embedder: FakeEmbedder())
    let cat = try memory.write("主人養貓", folder: .public, sourceSeq: 1, tier: .owner, kind: nil)
    let shortcut = try memory.write("TextEdit：File › Save＝⌘S（TextEdit 1.21）", folder: .public, sourceSeq: 0, tier: .program, kind: nil)
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
            try await Consolidation.run(config: cfg, root: root, model: FakeModel([Scripted(reply)]), embedder: FakeEmbedder())
        }
    }
    // The model failing is an error too.
    await #expect(throws: ModelError.self) {
        try await Consolidation.run(config: cfg, root: root, model: FakeModel([]), embedder: FakeEmbedder())
    }
    #expect(try memoryFiles(h) == files)
    #expect(try FileManager.default.contentsOfDirectory(atPath: h.dir.appending(path: "timeline").path).sorted() == segments)
    #expect(try h.events().contains { $0.text == "主人的貓叫什麼" })
    // The same reply, valid: applied.
    let ok = try await Consolidation.run(config: cfg, root: root, model: FakeModel([Scripted(plan(add))]), embedder: FakeEmbedder())
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
    let memory = try Memory(config: cfg.memory, root: h.dir.appending(path: "memory"), store: h.store, embedder: FakeEmbedder())
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
    let memory = try Memory(config: cfg.memory, root: h.dir.appending(path: "memory"), store: h.store, embedder: FakeEmbedder())
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
    return try Memory(config: cfg.memory, root: h.dir.appending(path: "memory"), store: h.store, embedder: FakeEmbedder()).registry
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
    try await h.turn(Scripted("", dm("好")))
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
    let h = try await Harness([Scripted("", todoSet(), screenshotCall), Scripted("", done("存好了"))])
    h.vm.setApp(textEdit, textEditKeys)
    await h.doll.ownerSaid("用 TextEdit 存檔")
    try await h.turn()
    try await h.turn()
    let cfg = try consolidationConfig(h)
    let registry = try Memory(config: cfg.memory, root: h.dir.appending(path: "memory"), store: h.store,
                              embedder: FakeEmbedder()).registry
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
    #expect(day.contains("她說｜Discord 私訊] 在") && day.contains("她說｜終端機] 收到"))
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
    let example = try Config.load(root.appending(path: "dollcore.example.toml"))
    #expect(example.api.port == 8791 && !example.bridge.enabled && example.bridge.command.first?.hasPrefix("/") == true)
    #expect(example.core.priority == ["owner_voice", "owner_text", "developer_text", "watch"])
    let file = FileManager.default.temporaryDirectory.appending(path: "api-\(UUID().uuidString).toml")
    let head = "[llm]\nbase_url = \"http://x\"\nmodel = \"m\"\n[computer]\nvm_id = \"v\"\n[memory]\n"
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
    try (head + "[core]\npriority = [\"owner_voice\", \"owner_text\", \"watch\"]\n").write(to: file, atomically: true, encoding: .utf8)
    #expect(throws: TOMLError.self) { try Config.load(file) }   // developer_text must be placed
    // The tracked experiment configs that loaded before (automem and shortcuts predate the
    // self_reported and consolidated tiers and did not load already).
    for tracked in ["experiments/dashboard/dollcore.toml", "experiments/m2/cheatsheet/cheatsheet.toml",
                    "experiments/m2/experience/experience.toml", "experiments/m2/experience2/experience2.toml",
                    "experiments/m2/expstats/expstats.toml"] {
        _ = try Config.load(root.appending(path: tracked))
    }
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
    let h = try await Harness()
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
    #expect(try await post(api, "/api/owner", "我是主人", key: "0" + api.ownerKey.dropFirst()) == 401)
    #expect(try await post(api, "/api/owner", "主人真的來了", key: api.ownerKey) == 200)
    #expect(await h.doll.queue.map(\.kind) == [.developer, .owner])
    #expect(await h.doll.queue.map(\.text) == ["嗨", "主人真的來了"])
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
    #expect(status.queued == 2 && status.state == "asleep" && status.footer.hasPrefix("【狀況】") && status.developerStreams == 0)
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
    try await waitUntil(seconds: 5) { api.bridge.hasPrefix("down") }
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
    let mem = try Config.load(root.appending(path: "dollcore.example.toml")).memory
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
    var cfg = try Config.load(root.appending(path: "dollcore.example.toml"))
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
    var cfg = try Config.load(root.appending(path: "dollcore.example.toml"))
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
    try await h.turn(Scripted("", key("Return"), screenshotCall))
    #expect(try h.toolResults().map(\.text) == ["失敗：現在沒有進行中的 todo，key 不能用。先用 todo.set 列 todo（小事就一項，每項寫做完的樣子），再做。",
                                                "失敗：現在沒有進行中的 todo，screenshot 不能用。先用 todo.set 列 todo（小事就一項，每項寫做完的樣子），再做。"])
    #expect(h.last.actions == ["refused key", "refused screenshot"] && h.vm.actions.isEmpty && h.last.todo == "待命中")
    #expect(h.last.footer.contains("- todo：待命中（沒有 todo；要做事先用 todo.set 列）"))
    // Her refused calls are results she has not seen: she gets the next turn to list the todo.
    #expect(await h.doll.followUp)
    // The list and the action in one reply: the item is in progress before the action runs.
    try await h.turn(Scripted("", todoSet("畫面上按過 Return", "存好檔"), key("Return")))
    #expect(h.vm.actions == [.key("return")] && h.last.todo == "進行中")
    #expect(try h.toolResults().suffix(2).first?.text == "todo 列好了。現在進行中：「畫面上按過 Return」；後面：「存好檔」。")
    // 進行中: a reply that only speaks still gets the next turn.
    try await h.turn(Scripted("", dm("按了")))
    #expect(await h.doll.followUp)
    #expect(h.last.footer.contains("- todo：進行中「畫面上按過 Return」；後面還有：「存好檔」"))
    // Not gated: speaking, remember, recall, think and the todo tools themselves work while idle (above).
    let t = try h.timeline().todo
    #expect(t.phase == .doing && t.current == "畫面上按過 Return" && t.pending == ["存好檔"])
}

@Test func doneCompactsTheItemKeepsTheConversationAndMovesOn() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("把 notes 存檔，再關掉")
    try await h.turn(Scripted("好，先看畫面", todoSet("notes 存好檔，標題沒有「已編輯」", "TextEdit 關掉了"), screenshotCall))  // request 0
    try await h.turn(Scripted("我按 ⌘S", key("cmd+s")))                                                                  // request 1
    await h.doll.ownerSaid("順便問，晚餐吃什麼？")
    try await h.turn(Scripted("先回他", dm("等我存完再想"), ("click", [("text", "Save")])))                             // request 2
    try await h.turn(Scripted("看起來好了", dm("存好了。"), done("按 ⌘S、點 Save 存檔，標題列沒有「已編輯」。")))         // request 3
    try await h.turn(Scripted("", key("cmd+q")))                                                                        // request 4
    try await h.turn(Scripted("", dm("關了")))                                                                          // request 5
    let before = await h.model.seen(3), after = await h.model.seen(4), next = await h.model.seen(5)
    // Up to the item's start (her reply that listed it) nothing changed.
    #expect(Array(after.prefix(2)) == Array(before.prefix(2)))
    #expect(before.contains { !$0.images.isEmpty } && before.contains { $0.content.hasPrefix("先回他") || $0.content == "我按 ⌘S" })
    // After: no screenshot, no computer result, no thought, no footer of the item's replies.
    let item = Array(after.dropFirst(2).dropLast())
    #expect(item.allSatisfy { $0.images.isEmpty && $0.extra.isEmpty && $0.content != fakeWindowsLine && !$0.content.hasPrefix("【狀況】") })
    #expect(item.filter { $0.role == "assistant" }.allSatisfy { $0.content.isEmpty })
    // What stays, in order: her todo.set (and its result), the owner's line verbatim, what she told him, her done with the action list.
    #expect(item.map(\.role) == ["assistant", "tool", "user", "assistant", "tool", "assistant", "tool", "tool"])
    #expect(item[0].toolCalls.map(\.name) == ["todo.set"] && item[1].content.hasPrefix("todo 列好了。"))
    #expect(item[2].content.hasSuffix("主人｜Discord 私訊｜文字] 順便問，晚餐吃什麼？"))
    #expect(item[3].toolCalls.map(\.name) == ["discord.dm"] && item[3].toolCalls[0].arguments!.contains("等我存完再想"))
    #expect(item[5].toolCalls.map(\.name) == ["discord.dm", "todo.done"])
    #expect(item[7].content.hasPrefix("完成，記下了。這次的經過和步驟存成一條經驗。")
            && item[7].content.contains("動作清單：key cmd+s、click「Save」。下一項「TextEdit 關掉了」現在進行中。"))
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
    #expect(try h.events().contains { $0.kind == .tool && $0.text == fakeWindowsLine && $0.image != nil })
}

@Test func failBlocksTheComputerUntilSheDecidesWhatIsLeft() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("買票、再訂餐廳")
    try await h.turn(Scripted("", todoSet("票買好了", "餐廳訂好了"), key("cmd+n")))
    try await h.turn(Scripted("", fail("要主人的信用卡驗證碼", tried: "填了卡號，跳出簡訊驗證"), key("Return")))
    #expect(try h.toolResults().suffix(2).map(\.text) == [
        "記下了：「票買好了」沒做成。這一項的過程收起來了，之後只留你寫的原因、試過的和這份動作清單：key cmd+n。電腦工具先鎖著。現在決定剩下的（剩下：「餐廳訂好了」）：用 todo.set 重寫剩下的項目；或照原樣列剩下的，接著做下一項；或跟主人說了以後用 todo.set 給空的陣列結束清單。",
        "失敗：這則回覆前面已經用 todo.fail 收起這一項了，後面的呼叫沒有執行；看了結果下一回合再做。"])
    #expect(h.last.itemEnd == "失敗" && h.last.compacted == ["failed"] && h.last.todo == "失敗")
    // She gets the turn in which she decides; the computer stays locked meanwhile.
    #expect(await h.doll.followUp)
    try await h.turn(Scripted("", key("Return")))
    #expect(try h.toolResults().last!.text.hasPrefix("失敗：「票買好了」失敗了，key 先鎖著。") && h.vm.actions == [.key("cmd+n")])
    #expect(h.last.footer.contains("- todo：失敗「票買好了」：電腦工具鎖著"))
    // The failed item's reason and what she tried stay (her call); the action list is its result.
    let msgs = await h.model.seen(2)
    #expect(msgs.contains { $0.toolCalls.first?.name == "todo.fail" && $0.toolCalls[0].arguments!.contains("填了卡號，跳出簡訊驗證") })
    #expect(!msgs.contains { $0.content == fakeWindowsLine })
    // She continues with the next item: in progress again, computer unlocked.
    try await h.turn(Scripted("", todoSet("餐廳訂好了"), key("cmd+t")))
    #expect(h.vm.actions.last == .key("cmd+t") && h.last.todo == "進行中")
}

@Test func anEmptyListAfterAFailureEndsItAndNothingWakesHer() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("買票")
    try await h.turn(Scripted("", todoSet("票買好了"), key("cmd+n")))
    try await h.turn(Scripted("", fail("沒票了")))
    try await h.turn(Scripted("", dm("沒票了"), ("todo.set", [("items", "[]")])))
    #expect(try h.toolResults().last!.text == "清單結束了，現在待命中。" && h.last.todo == "待命中")
    #expect(await !h.doll.followUp)
    #expect(h.last.itemEnd == nil)
    #expect(try h.timeline().todo == TodoState())
}

@Test func waitingGetsNoTurnsUntilSomethingHappens() async throws {
    let h = try await Harness()
    await h.doll.ownerSaid("幫我登入")
    try await h.turn(Scripted("", todoSet("登入好了"), key("cmd+n")))
    try await h.turn(Scripted("", dm("要你的驗證碼"), todoWait("主人的驗證碼"), key("Return")))
    // The wait applies once the reply's calls ran: the key after it still ran.
    #expect(h.vm.actions == [.key("cmd+n"), .key("return")])
    #expect(h.last.todo == "等待中")
    #expect(await !h.doll.followUp)
    #expect(try h.timeline().todo.phase == .waiting)
    await h.doll.ownerSaid("123456")
    try await h.turn(Scripted("", ("type", [("text", "123456")])))
    #expect(h.reports[2].footer.contains("- todo：進行中「登入好了」") && h.vm.actions.last == .type("123456"))
    // A watch waits too, and its wake brings the item back.
    try await h.turn(Scripted("", watch("window", 1)))
    #expect(h.last.todo == "等待中")
    try await h.waitFor(.watch)
    try await h.turn(Scripted("", key("Return")))
    #expect(h.last.todo == "進行中" && h.vm.actions.last == .key("return"))
}

/// A harness whose context is near full after a screenshot or two: a small limit and big screenshots.
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

@Test func nearFullSheCompactsWithProgressAndTheItemGoesOn() async throws {
    let h = try await tightHarness()
    await h.doll.ownerSaid("整理桌面")
    try await h.turn(Scripted("", todoSet("桌面只剩 notes.txt"), screenshotCall))
    try await h.turn(Scripted("", key("cmd+a")))
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
    try await h.turn(Scripted("", done("刪好了")))
    try await h.turn(Scripted("", dm("好了")))
    let comps = try h.timeline().compactions
    #expect(comps.map(\.kind) == [.progress, .done] && comps[0].fromSeq == comps[1].fromSeq)
    #expect(try h.toolResults().last { $0.text.hasPrefix("完成") }!.text.contains("動作清單：key cmd+a、key cmd+c、key delete。"))
}

@Test func nearFullWithoutHerCompactionTheProgramForcesItKeepingTheLastTurns() async throws {
    let h = try await tightHarness()
    await h.doll.ownerSaid("整理桌面")
    try await h.turn(Scripted("", todoSet("桌面只剩 notes.txt"), screenshotCall))   // turn 1
    try await h.turn(Scripted("", key("a")))                                          // 2: warned (0 → 1)?
    for k in ["b", "c", "d", "e", "f"] { try await h.turn(Scripted("", key(k))) }
    let warnings = h.reports.map { $0.footer.contains("context 快滿了：用 todo.progress") }
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

@Test func nearFullMostlyConversationSaysCompactingWillNotHelp() async throws {
    let h = try await Harness(tune: { $0.context.limit = 1_000 })
    await h.doll.ownerSaid("聊聊")
    try await h.turn(Scripted("", dm("好")))
    #expect(h.last.footer.contains("- context 快滿了，但大部分是對話和之前留下的結果，不是正在做的這一項的過程；收起過程也省不了多少。"))
    #expect(h.last.footer.contains("其中做事的過程 0（截圖 0）") && h.last.footer.contains("／1,000（"))
}

@Test func compactionsAndTheTodoListSurviveARestartByteForByte() async throws {
    let h = try await tightHarness()
    await h.doll.ownerSaid("兩件事")
    try await h.turn(Scripted("", todoSet("A 好了", "B 好了"), screenshotCall))
    try await h.turn(Scripted("", key("a")))
    try await h.turn(Scripted("", done("A 做完")))
    try await h.turn(Scripted("", key("b")))
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

@Test func theDocsGoIntoHerComputerAndAFailedCopyIsInTheBootNotice() async throws {
    let h = try await Harness()
    #expect(await h.doll.syncDocs() == nil)
    let docs = root.appending(path: "dolldocs")
    let names = try FileManager.default.contentsOfDirectory(atPath: docs.path).filter { $0.hasSuffix(".md") }.sorted()
    #expect(names.contains("README.md") && names.count >= 6)
    for n in names {
        #expect(h.vm.file("\(Doll.docsVMPath)/\(n)") == String(decoding: try Data(contentsOf: docs.appending(path: n)), as: UTF8.self))
    }
    h.vm.failNext("vm is asleep")
    let why = try #require(await h.doll.syncDocs())
    try await h.doll.announceBoot(docsFailure: why)
    #expect(try h.events().last!.text == "【開機】你剛被開機了。DollOS 文件這次沒有複製進你的電腦（\(why)）；~/DollOS/docs 裡可能是舊的或沒有。")
    #expect(h.logs.contains { $0.hasPrefix("ERROR docs: copying") })
}
