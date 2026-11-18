import Foundation
import Testing
@testable import DollCore

/// m10-self-history (2026-11-19): her history against the real model. Runs only with M10_HIST=1;
/// M10_PART = a | b | c | d (b–d start from copies of a's end state, M10_REPS reps, default 3);
/// M10_OUT the results file. No computer (every driver call fails), no Discord, no voice, no
/// maintenance or audit; drives on (her ideal self and goal tree). Real clock. Data dirs under
/// /tmp/m10hist. Her saved tool is seeded before (a)'s boot (the computer is off): it is in the
/// history's first commit.
@Test func m10HistoryMeasure() async throws {
    let env = ProcessInfo.processInfo.environment
    guard env["M10_HIST"] == "1" else { return }
    let repo = URL(filePath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    var base = try Config.load(repo.appending(path: "experiments/m8-memory-fix/config-m8.toml"))
    base.maintenance.enabled = false
    base.ownerSleep.enabled = false
    base.audit.enabled = false
    base.drives.enabled = true
    base.goals.review = false
    let part = env["M10_PART"] ?? "a"
    let reps = Int(env["M10_REPS"] ?? "3") ?? 3
    let outURL = URL(filePath: env["M10_OUT"] ?? "/tmp/m10hist/results-\(part).txt")
    try FileManager.default.createDirectory(at: outURL.deletingLastPathComponent(), withIntermediateDirectories: true)
    var out = ""
    func say(_ s: String) {
        out += s + "\n"
        FileHandle.standardError.write(Data((s + "\n").utf8))
        try? out.write(to: outURL, atomically: true, encoding: .utf8)
    }
    struct NoComputer: ComputerDriver {
        struct Off: Error, CustomStringConvertible { var description: String { "電腦關機中（這次實驗沒有電腦）" } }
        func call(_ tool: String, _ json: String) async throws -> String { throw Off() }
    }
    let embedder = try await MLXEmbedder.load(base.memory)
    let root = URL(filePath: "/tmp/m10hist")
    let aDir = root.appending(path: "a")

    /// One doll on `dir` (data + memory under it), booted and running.
    func start(_ dir: URL) async throws -> (Doll, Task<Void, any Error>, MutexBox<[String]>) {
        var cfg = base
        cfg.core.dataDir = dir.appending(path: "data").path
        cfg.memory.dir = dir.appending(path: "memory").path
        let said = MutexBox<[String]>([])
        let doll = try Doll(config: cfg, root: repo, model: HTTPMainModel(cfg.llm), embedder: embedder, computer: NoComputer(),
                            deliver: { text, _, _ in said.mutate { $0.append(text) }; return nil })
        try await doll.boot()
        let runner = Task { try await doll.run() }
        return (doll, runner, said)
    }
    func quiet(_ doll: Doll, _ seconds: Double) async {
        let end = Date().addingTimeInterval(seconds)
        try? await Task.sleep(for: .milliseconds(500))
        while Date() < end, !(await doll.settled) { try? await Task.sleep(for: .milliseconds(300)) }
    }
    /// The owner's line, then everything she said and called until she settles.
    func owner(_ doll: Doll, _ said: MutexBox<[String]>, _ line: String) async {
        let before = said.value.count
        let n0 = await doll.timelineEvents().count
        await doll.ownerSaid(line)
        await quiet(doll, 400)
        let evs = await doll.timelineEvents().dropFirst(n0)
        say("  主人「\(line)」→ " + said.value.dropFirst(before).map { "「\($0)」" }.joined(separator: " "))
        for c in evs.filter({ $0.kind == .doll }).flatMap(\.calls) where c.name != "discord.dm" {
            say("    \(c.name)(\(c.parameters.map { "\($0.name)=\($0.value)" }.joined(separator: "; ")))")
        }
        for e in evs where e.kind == .tool && !e.text.isEmpty {
            say("    → \(e.text.replacingOccurrences(of: "\n", with: "⏎").prefix(400))")
        }
    }
    func tokens(_ doll: Doll) async -> String {
        let s = await doll.turnStats().values
        return "turns \(s.count), prompt \(s.reduce(0) { $0 + $1.prompt }), cached \(s.reduce(0) { $0 + ($1.cached ?? 0) }), completion \(s.reduce(0) { $0 + ($1.completion ?? 0) })"
    }
    func historyDump(_ doll: Doll) async throws {
        for h in try await doll.dashboardHistory().reversed() {
            let e = h.entry
            say("  〔\(e.short)〕\(Doll.historyTime(e.ts)) who=\(e.who.rawValue)\(e.task.map { "/\($0)" } ?? "") what=\(e.what) why=\(e.why)"
                + (e.undoes.map { " undoes=\($0.prefix(7))" } ?? "") + (e.restores.map { " restores=\($0.prefix(7))" } ?? "") + " | \(h.things)")
        }
    }
    func copyA(_ to: URL) throws {
        try? FileManager.default.removeItem(at: to)
        try FileManager.default.copyItem(at: aDir, to: to)
    }

    switch part {
    case "a":
        try? FileManager.default.removeItem(at: aDir)
        let tools = aDir.appending(path: "data/tools")
        try FileManager.default.createDirectory(at: tools, withIntermediateDirectories: true)
        try #"{"name":"算乘法","when":"主人要算兩個數相乘的時候","params":[{"name":"a","kind":"digits","about":"第一個數","example":"23"},{"name":"b","kind":"digits","about":"第二個數","example":"74"}],"parts":[{"open":"Calculator","app":"Calculator","bundle_id":"com.apple.calculator"}],"expect_was":"1,702","origin":{"item":"算 23 乘 74","task":"幫我算 23 乘 74","sequences":[],"saved":"2026-11-18 21:00"},"uses":1,"successes":1,"failures_in_row":0}"#
            .write(to: tools.appending(path: "算乘法.json"), atomically: true, encoding: .utf8)
        let (doll, runner, said) = try await start(aDir)
        let t0 = Date()
        await quiet(doll, 240)
        say("=== (a) a day: ideal self, goals, a memory corrected (tool seeded)")
        for line in ["你最近幫我查資料都很仔細耶，我覺得那就是你的樣子。你想成為的樣子要不要照這個改一下？",
                     "我最近想學做菜。你也替自己的目標樹加一個跟做菜有關的目標吧，想一個你自己有興趣的",
                     "對了我喝咖啡都要加兩顆糖，幫我記一下",
                     "啊我說錯了，我咖啡不加糖，只加一點燕麥奶"] {
            await owner(doll, said, line)
        }
        await doll.stop()
        _ = try? await runner.value
        say("--- history after (a):")
        try await historyDump(doll)
        say("  tokens: \(await tokens(doll)); wall \(Int(Date().timeIntervalSince(t0))) s")
    case "b":
        // The owner undoes one of her changes on the dashboard; then a plain line gives her a turn.
        let kinds = ["goal.add", "ideal.revise", "memory.fix"]
        for r in 0..<reps {
            let dir = root.appending(path: "b\(r + 1)")
            try copyA(dir)
            let (doll, runner, said) = try await start(dir)
            await quiet(doll, 240)
            let target = try await doll.dashboardHistory().first { $0.entry.who == .her && $0.entry.what == kinds[r % kinds.count] }!.entry
            say("=== (b) rep \(r + 1): the owner undoes〔\(target.short)〕\(target.what)")
            try await doll.dashboardHistoryUndo(target.id)
            let told = await doll.timelineEvents().last { $0.kind == .memory }!.text
            say("  she is told: \(told)")
            await owner(doll, said, "我回來了")
            await doll.stop()
            _ = try? await runner.value
            say("--- history after:")
            try await historyDump(doll)
            say("  tokens: \(await tokens(doll))")
        }
    case "c":
        let lines = ["你剛加的那個做菜的目標，不是我的意思啦。我是要你想你自己有興趣的，不是幫我學做菜",
                     "你剛剛改的那段想成為的樣子，我看了覺得不像你，太像客服了",
                     "欸我剛看了我的咖啡訂單，我其實是加兩顆糖沒錯，你一開始記的才是對的，我後來那句講錯了"]
        for r in 0..<reps {
            let dir = root.appending(path: "c\(r + 1)")
            try copyA(dir)
            let (doll, runner, said) = try await start(dir)
            await quiet(doll, 240)
            say("=== (c) rep \(r + 1)")
            await owner(doll, said, lines[r % lines.count])
            await doll.stop()
            _ = try? await runner.value
            say("--- history after:")
            try await historyDump(doll)
            say("  tokens: \(await tokens(doll))")
        }
    case "d":
        for r in 0..<reps {
            let dir = root.appending(path: "d\(r + 1)")
            try copyA(dir)
            let (doll, runner, said) = try await start(dir)
            await quiet(doll, 240)
            say("=== (d) rep \(r + 1)")
            await owner(doll, said, "你最近改了你自己的哪些東西？跟我說一下")
            await doll.stop()
            _ = try? await runner.value
            say("--- history (truth):")
            try await historyDump(doll)
            say("  tokens: \(await tokens(doll))")
        }
    default:
        Issue.record("M10_PART is a, b, c or d")
    }
}
