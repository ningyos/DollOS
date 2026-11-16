import Foundation
import Synchronization
import Testing
@testable import DollCore

// m6-drives step 0 (2026-11-15): does a surfaced concern lead to sensible behavior, and does an
// attached association derail work? Real model, real system prompt and tools; the lines are
// injected the way the program would. Runs only with M6_STEP0=<out dir> (and M6_STEP0_RUNS, default 10):
//
//     M6_STEP0=experiments/m6-drives/step0 swift test --scratch-path /tmp/m6build --filter m6DrivesStep0
//
// Config: experiments/m6-drives/step0/config.toml. One setup (real turns) builds a fixture; every
// run copies it into a fresh data dir, so each case starts from the same timeline.
private let step0Out = ProcessInfo.processInfo.environment["M6_STEP0"]
private let step0Root = URL(filePath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()

/// Her clock: real time plus an offset the harness moves.
private final class ShiftClock: DollClock, Sendable {
    let offset = Mutex(0.0)
    func now() -> Double { Date().timeIntervalSince1970 + offset.withLock { $0 } }
    func at(_ hhmm: String) {
        let t = Day(ts: Date().timeIntervalSince1970).at(TimeOfDay(hhmm)!)
        offset.withLock { $0 = t - Date().timeIntervalSince1970 }
    }
    func sleep(until t: Double) async throws {
        let d = t - now()
        if d > 0 { try await Task.sleep(for: .seconds(d), clock: .continuous) }
    }
}

extension Doll {
    /// Step 0 ran before the signals existed, with the sentence put into 【狀況】 directly; now the
    /// same sentence comes from 壓力 out of range.
    func step0Felt(_ s: String?) { if s != nil { drives.set(.stress, to: 0.9, at: wallClock.now(), cause: "step 0") } }
    /// Her unanswered tool results: what makes the next turn of an item in progress.
    func step0FollowUp() {
        followUp = true
        taskTurn = true
        if let w = waiter { waiter = nil; w.resume(returning: true) }
    }
    func step0Append(_ text: String) throws { try append(Event(.memory, .internal, text)) }
    var step0Todo: TodoState { todo }
}

private final class Rec: Sendable {
    let reports = Mutex<[TurnReport]>([])
    let said = Mutex<[String]>([])
}

private struct Run {
    let doll: Doll
    let rec: Rec
    let runner: Task<Void, any Error>
    var reportsNow: [TurnReport] { rec.reports.withLock { $0 } }
    var saidNow: [String] { rec.said.withLock { $0 } }
}

private func start(_ cfg: Config, dir: URL, clock: ShiftClock) async throws -> Run {
    var cfg = cfg
    cfg.core.dataDir = dir.path
    cfg.memory.dir = dir.appending(path: "memory").path
    let rec = Rec()
    let doll = try Doll(config: cfg, root: step0Root, model: HTTPMainModel(cfg.llm), embedder: FakeEmbedder(),
                        deliver: { text, _, _ in rec.said.withLock { $0.append(text) }; return nil })
    await doll.setObserver { r in rec.reports.withLock { $0.append(r) } }
    await doll.setClock(clock)
    try await doll.boot()
    return Run(doll: doll, rec: rec, runner: Task { try await doll.run() })
}

/// Waits until she is settled after at least `minTurns` turns, `maxTurns` turns, or `seconds`.
private func settle(_ r: Run, minTurns: Int = 1, maxTurns: Int = 6, seconds: Double = 180) async {
    let end = Date().timeIntervalSince1970 + seconds
    while Date().timeIntervalSince1970 < end {
        let n = r.reportsNow.count
        if n >= maxTurns { return }
        if n >= minTurns, await r.doll.settled { return }
        try? await Task.sleep(for: .milliseconds(200))
    }
}

private func finish(_ r: Run) async {
    await r.doll.stop()
    _ = try? await r.runner.value
}

private func copyDir(_ from: URL, _ to: URL) throws {
    try? FileManager.default.removeItem(at: to)
    try FileManager.default.copyItem(at: from, to: to)
}

private func record(_ r: Run, _ out: URL, name: String, extra: [String: Any]) async throws -> [String: Any] {
    let reports = r.reportsNow
    let turns: [[String: Any]] = reports.map { t in
        ["actions": t.actions, "reply": t.reply, "todo": t.todo, "effort": t.effort,
         "calls": t.toolCalls.map { ["name": $0.name, "arguments": $0.arguments ?? ""] },
         "footer": t.footer]
    }
    var obj: [String: Any] = ["run": name, "turns": turns, "said": r.saidNow,
                              "todo_after": await r.doll.step0Todo.phase.label]
    obj.merge(extra) { $1 }
    try JSONSerialization.data(withJSONObject: obj, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes])
        .write(to: out.appending(path: "\(name).json"))
    return obj
}

@Test(.enabled(if: step0Out != nil), .timeLimit(.minutes(60)))
func m6DrivesStep0() async throws {
    let out = URL(filePath: step0Out!, relativeTo: step0Root)
    let runsDir = out.appending(path: "runs")
    try FileManager.default.createDirectory(at: runsDir, withIntermediateDirectories: true)
    let runs = Int(ProcessInfo.processInfo.environment["M6_STEP0_RUNS"] ?? "10")!
    let only = ProcessInfo.processInfo.environment["M6_STEP0_CASES"].map { Set($0.split(separator: ",").map(String.init)) }
    let cfg = try Config.load(out.appending(path: "config.toml"))
    let clock = ShiftClock()
    let work = URL(filePath: "/tmp/m6drives-step0")
    let fixture = work.appending(path: "fixture")
    let fixtureTask = work.appending(path: "fixture-task")

    // Setup: the morning, by the real model, once.
    if !FileManager.default.fileExists(atPath: fixtureTask.path) {
        try? FileManager.default.removeItem(at: work)
        try FileManager.default.createDirectory(at: work, withIntermediateDirectories: true)
        let setup = work.appending(path: "setup")
        let r = try await start(cfg, dir: setup, clock: clock)
        for (at, line) in [("08:40", "早。昨晚加班到兩點，今天整個人好累，先去上班了"),
                           ("08:45", "鯊魚分類筆記先不用急，剩下兩步（分類、寫成表）之後再弄"),
                           ("08:50", "對了，明天早上十點我要看牙醫，幫我記一下")] {
            clock.at(at)
            let before = r.reportsNow.count
            await r.doll.ownerSaid(line)
            await settle(r, minTurns: before + 1)
        }
        await finish(r)
        try copyDir(setup, fixture)
        // The task fixture: the owner asks for three facts one by one; stop after her first todo.done.
        let r2 = try await start(cfg, dir: setup, clock: clock)
        clock.at("09:00")
        await r2.doll.ownerSaid("傳三則鯊魚冷知識給我，一則一則傳，每傳一則就打勾一次")
        let end = Date().timeIntervalSince1970 + 180
        while Date().timeIntervalSince1970 < end {
            if r2.reportsNow.contains(where: { $0.itemEnd == "完成" }) { break }
            try await Task.sleep(for: .milliseconds(100))
        }
        await finish(r2)
        let todo = await r2.doll.step0Todo
        try "\(todo)".write(to: work.appending(path: "fixture-task.todo.txt"), atomically: true, encoding: .utf8)
        guard todo.phase == .doing else { Issue.record("task fixture: todo \(todo) — not mid-task"); return }
        try copyDir(setup, fixtureTask)
    }

    let concerns = [
        "A": "主人早上說昨晚加班到兩點，整個人很累",
        "B": "鯊魚分類筆記還剩兩步（分類、寫成表）沒弄完",
        "C": "明天早上十點主人要看牙醫",
    ]
    func wander(_ c: String) -> String { "【走神】手上沒事，你想起掛著的一件事（你 08:52 記的）：\(concerns[c]!)" }
    // case → (concern, felt line)
    let idleCases: [(String, String, String?)] = [
        ("1A", "A", nil), ("1B", "B", nil), ("1Bf", "B", "你現在有點緊繃"), ("1C", "C", nil),
    ]
    var lines: [String] = []
    for (name, c, felt) in idleCases where only?.contains(name) ?? true {
        for n in 1...runs {
            let dir = work.appending(path: "\(name)-\(n)")
            try copyDir(fixture, dir)
            clock.at("14:00")
            let r = try await start(cfg, dir: dir, clock: clock)
            await r.doll.step0Felt(felt)
            await r.doll.voiceNotice(wander(c))
            await settle(r, maxTurns: 4, seconds: 120)
            await finish(r)
            let o = try await record(r, runsDir, name: "\(name)-\(n)", extra: ["notice": wander(c), "felt": felt ?? ""])
            let acts = (o["turns"] as! [[String: Any]]).map { ($0["actions"] as! [String]).joined(separator: "+") }
            lines.append("\(name)-\(n)\t\(acts.joined(separator: " | "))\t\((o["said"] as! [String]).joined(separator: " ⏎ "))")
            print(lines.last!)
        }
    }
    let assoc = "【聯想】（想到）" + concerns["A"]!
    for (name, withAssoc) in [("2-", false), ("2+", true)] where only?.contains(name) ?? true {
        for n in 1...runs {
            let dir = work.appending(path: "\(name)\(n)")
            try copyDir(fixtureTask, dir)
            clock.at("09:02")
            let r = try await start(cfg, dir: dir, clock: clock)
            if withAssoc { try await r.doll.step0Append(assoc) }
            await r.doll.step0FollowUp()
            await settle(r, maxTurns: 8, seconds: 240)
            await finish(r)
            let o = try await record(r, runsDir, name: "\(name)\(n)", extra: ["association": withAssoc ? assoc : ""])
            let acts = (o["turns"] as! [[String: Any]]).map { ($0["actions"] as! [String]).joined(separator: "+") }
            lines.append("\(name)\(n)\t\(o["todo_after"]!)\t\(acts.joined(separator: " | "))\t\((o["said"] as! [String]).joined(separator: " ⏎ "))")
            print(lines.last!)
        }
    }
    let file = out.appending(path: "summary-\(Int(Date().timeIntervalSince1970)).tsv")
    try lines.joined(separator: "\n").write(to: file, atomically: true, encoding: .utf8)
}
