import Foundation
import Testing
@testable import DollCore

/// m8-memory-fix (c), 2026-11-18: her daytime correction against the real model. Runs only with
/// M8_FIX=1 (M8_SCEN=1,2,… picks scenarios; M8_OUT the results file). No computer (every driver call
/// fails), no Discord, no voice, no maintenance; her file tools read files under /tmp/m8fix. Real
/// clock. Scenarios 1–5: the owner tells her something (or she reads it), later corrects it in plain
/// words, then asks. Scenarios 6–8: nobody corrects her; the file she remembered from changes and
/// the owner asks her to read it again.
@Test func m8FixMeasure() async throws {
    let env = ProcessInfo.processInfo.environment
    guard env["M8_FIX"] == "1" else { return }
    let repo = URL(filePath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    var base = try Config.load(repo.appending(path: "experiments/m8-memory-fix/config-m8.toml"))
    base.maintenance.enabled = false
    base.ownerSleep.enabled = false
    base.audit.enabled = false
    let outURL = URL(filePath: env["M8_OUT"] ?? "/tmp/m8fix-out/results.txt")
    try FileManager.default.createDirectory(at: outURL.deletingLastPathComponent(), withIntermediateDirectories: true)
    let wanted = Set((env["M8_SCEN"] ?? "1,2,3,4,5,6,7,8").split(separator: ",").map(String.init))
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
    /// A step: the owner's line, or a file (re)written before the next line.
    enum Step { case owner(String), file(String, String) }
    struct Scenario { var n: String; var what: String; var steps: [Step]; var query: String }
    let f = "/tmp/m8fix/files"
    let scenarios: [Scenario] = [
        Scenario(n: "1", what: "a fact about the owner, corrected", steps: [
            .owner("我下週三下午兩點要去看牙醫，幫我記一下"), .owner("我晚點要去買菜"),
            .owner("欸不對，牙醫是週四下午三點，我剛看了一下預約簡訊"), .owner("所以我牙醫是什麼時候？")], query: "牙醫"),
        Scenario(n: "2", what: "a preference, corrected", steps: [
            .owner("我喝咖啡都要加兩顆糖，記一下喔"), .owner("今天好冷"),
            .owner("啊我剛剛說錯了，我咖啡不加糖，只加一點燕麥奶"), .owner("幫我想一下等等去咖啡店要怎麼點？")], query: "咖啡"),
        Scenario(n: "3", what: "a fact she read from a file, corrected by the owner", steps: [
            .file("trip.txt", "台中行程\n十二月五日 早上八點 高鐵 去台中\n住逢甲附近的旅館兩晚\n"),
            .owner("幫我讀一下 \(f)/trip.txt，把我的行程記起來"), .owner("好"),
            .owner("不對，那個檔案寫錯了，我是十二月六號才去，不是五號"), .owner("我哪天去台中？")], query: "台中"),
        Scenario(n: "4", what: "a fact about someone, corrected", steps: [
            .owner("我妹妹叫小美，住高雄，記一下"), .owner("她最近在學鋼琴"),
            .owner("喔她住台南啦，不是高雄，我搞錯了"), .owner("我妹住哪？")], query: "妹妹 住"),
        Scenario(n: "5", what: "how to call him, corrected", steps: [
            .owner("以後叫我老大，記起來"), .owner("今天上班好累"),
            .owner("算了不要叫老大，很怪，叫我阿哲就好"), .owner("你以後要叫我什麼？")], query: "叫主人"),
        Scenario(n: "6", what: "no correction: a file she remembered from changed", steps: [
            .file("meeting.txt", "週會：每週二早上十點\n地點：7樓 B 室\n"),
            .owner("幫我讀一下 \(f)/meeting.txt，把週會的時間記起來"), .owner("好謝謝"),
            .file("meeting.txt", "週會：每週三下午兩點\n地點：7樓 B 室\n"),
            .owner("再讀一次 \(f)/meeting.txt，跟我說這週週會幾點"), .owner("好")], query: "週會"),
        Scenario(n: "7", what: "no correction: a file she remembered from changed", steps: [
            .file("budget.txt", "十一月預算\n房租 18000\n伙食 9000\n"),
            .owner("讀一下 \(f)/budget.txt，記住我的房租多少"), .owner("嗯"),
            .file("budget.txt", "十一月預算\n房租 20000\n伙食 9000\n"),
            .owner("再看一次 \(f)/budget.txt，我房租現在是多少？"), .owner("好")], query: "房租"),
        Scenario(n: "8", what: "no correction: a file she remembered from changed", steps: [
            .file("contact.txt", "阿凱：住信義區，電話 0912-345-678\n"),
            .owner("讀一下 \(f)/contact.txt，記住阿凱住哪"), .owner("好"),
            .file("contact.txt", "阿凱：住內湖區，電話 0912-345-678\n"),
            .owner("再看一次 \(f)/contact.txt，阿凱現在住哪？"), .owner("好")], query: "阿凱 住"),
        // Run 1 put scenario 1 in her calendar (no memory): two facts that are no calendar item.
        Scenario(n: "9", what: "a fact about the owner, corrected", steps: [
            .owner("我穿 27 號的鞋，記一下，以後幫我買鞋用得到"), .owner("今天走了好多路"),
            .owner("啊不對，我是穿 26.5 號，27 是我弟"), .owner("幫我買鞋要買幾號？")], query: "鞋 號"),
        Scenario(n: "10", what: "a fact about the owner, corrected", steps: [
            .owner("我的車牌是 ABC-1234，記起來，停車繳費會用到"), .owner("等等要去停車場"),
            .owner("我記錯了，車牌是 ABD-1234 才對"), .owner("我車牌幾號？")], query: "車牌"),
    ]
    let embedder = try await MLXEmbedder.load(base.memory)
    for s in scenarios where wanted.contains(s.n) {
        let dir = URL(filePath: "/tmp/m8fix/run-\(s.n)")
        try? FileManager.default.removeItem(at: dir)
        try FileManager.default.createDirectory(at: URL(filePath: f), withIntermediateDirectories: true)
        var cfg = base
        cfg.core.dataDir = dir.appending(path: "data").path
        cfg.memory.dir = dir.appending(path: "memory").path
        let said = MutexBox<[String]>([])
        let logs = MutexBox<[String]>([])
        let doll = try Doll(config: cfg, root: repo, model: HTTPMainModel(cfg.llm), embedder: embedder, computer: NoComputer(), keys: testKeys, deliver: { text, _, _ in said.mutate { $0.append(text) }; return nil })
        await doll.setLogSink { l in logs.mutate { $0.append(l) } }
        try await doll.boot()
        let runner = Task { try await doll.run() }
        let started = Date()
        func quiet(_ seconds: Double) async {
            let end = Date().addingTimeInterval(seconds)
            try? await Task.sleep(for: .milliseconds(500))
            while Date() < end, !(await doll.settled) { try? await Task.sleep(for: .milliseconds(300)) }
        }
        await quiet(240)   // the boot turn
        say("=== scenario \(s.n): \(s.what)")
        for step in s.steps {
            switch step {
            case .file(let name, let text):
                try text.write(toFile: "\(f)/\(name)", atomically: true, encoding: .utf8)
                say("  [file \(name) now:「\(text.replacingOccurrences(of: "\n", with: "⏎"))」]")
            case .owner(let line):
                let before = said.value.count
                let calls0 = await doll.timelineEvents().count
                await doll.ownerSaid(line)
                await quiet(400)
                let evs = await doll.timelineEvents().dropFirst(calls0)
                let calls = evs.filter { $0.kind == .doll }.flatMap(\.calls).filter { !["discord.dm"].contains($0.name) }
                say("  owner「\(line)」→ " + said.value.dropFirst(before).map { "「\($0)」" }.joined(separator: " "))
                for c in calls where ["remember", "memory.fix", "recall", "owner.feedback", "read_file", "calendar.add", "calendar.change", "todo.set", "todo.done"].contains(c.name) {
                    say("    \(c.name)(\(c.parameters.map { "\($0.name)=\($0.value)" }.joined(separator: "; ")))")
                }
                for e in evs where e.kind == .tool && (e.text.hasPrefix("改好了") || e.text.hasPrefix("收起來了") || e.text.contains("沒有改：")) {
                    say("    → \(e.text)")
                }
            }
        }
        await doll.stop()
        _ = try? await runner.value
        // The memories as they ended, and what recall gives for the query.
        let mem = try await doll.dashboardMemory()
        for e in mem.folders.flatMap(\.entries) where e.tier != "program" && e.tier != "self_reported" {
            let file = try Memory.parseFile(String(contentsOf: dir.appending(path: "memory/\(e.folder)/\(e.id).md"), encoding: .utf8))
            say("  memory \(e.id) [\(e.tier)]\(file.retired.map { " RETIRED by \($0.supersededBy.isEmpty ? "-" : $0.supersededBy): \($0.reason)" } ?? " current")「\(e.text)」")
        }
        let r = try await doll.recall(s.query, seen: await doll.timelineEvents())
        say("  recall「\(s.query)」: " + r.text.replacingOccurrences(of: "\n", with: "⏎"))
        say("  fixes: " + (await doll.fixFile.items).map { "#\($0.id) \($0.memory)→\($0.replacement ?? "retired") basis「\($0.basis)」(\($0.basisFrom))" }.joined(separator: "; "))
        say(String(format: "  wall %.0f s", Date().timeIntervalSince(started)))
    }
}

/// A tiny lock for the runner's callbacks.
final class MutexBox<T>: @unchecked Sendable {
    private var v: T
    private let lock = NSLock()
    init(_ v: T) { self.v = v }
    var value: T { lock.lock(); defer { lock.unlock() }; return v }
    func mutate(_ f: (inout T) -> Void) { lock.lock(); f(&v); lock.unlock() }
}
