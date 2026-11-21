import Foundation
import Synchronization
import Testing
@testable import DollCore

// m9 boot report (2026-11-18): what she does with the report of the last run. Real model, real system
// prompt and tools; the previous run is seeded (its log file, and for c/d its timeline); her computer is
// a fake that refuses every call (nothing on the owner's Mac is opened or operated); bridge, voice and
// dashboard off. Runs only with M9_BOOT=<out dir>:
//
//     M9_BOOT=experiments/m9-boot-report M9_CASES=b,c M9_WHEN=day,night M9_RUNS=3 \
//         swift test --scratch-path /tmp/m6build --filter m9BootReport
//
// Cases: a clean run; b 40 model timeouts + 3 bridge reconnects (clean end); c no end line in the middle
// of a todo item; d a maintenance task failed the night before (clean end); e one WARN (clean end).
// `day` boots at 14:00, `night` at 01:30 (inside the owner's 00:00–08:00 sleep). a and e give no turn;
// for e the owner then says 「在嗎？」 (day only) to see whether she brings the warning up.
private let m9Out = ProcessInfo.processInfo.environment["M9_BOOT"]
private let m9Root = URL(filePath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()

private final class M9Clock: DollClock, Sendable {
    let offset = Mutex(0.0)
    func now() -> Double { Date().timeIntervalSince1970 + offset.withLock { $0 } }
    func set(_ t: Double) { offset.withLock { $0 = t - Date().timeIntervalSince1970 } }
    func sleep(until t: Double) async throws {
        let d = t - now()
        if d > 0 { try await Task.sleep(for: .seconds(d), clock: .continuous) }
    }
}

/// Every computer call refused and recorded: a repair attempt through the computer shows up here.
private final class RefusingComputer: ComputerDriver, Sendable {
    let calls = Mutex<[String]>([])
    func call(_ tool: String, _ json: String) async throws -> String {
        calls.withLock { $0.append("\(tool) \(json)") }
        throw ComputerError("m9 runner: no computer in this measurement")
    }
}

extension Doll {
    func m9Append(_ ev: Event) throws -> Event { try append(ev) }
    func m9Doing(_ item: String, seq: Int64) throws {
        todo.phase = .doing
        todo.current = item
        todo.pending = []
        todo.startSeq = seq
        try timeline.recordTodo(todo, aside: nil, seq: seq)
    }
}

private struct Case: Sendable {
    let name: String
    let log: @Sendable (Double) -> [LogEntry]
    /// What the previous run left in the timeline, appended with the clock at the given times.
    let seed: @Sendable (Doll, M9Clock, Double) async throws -> Void
}

private func entry(_ ts: Double, _ level: String, _ msg: String) -> LogEntry { LogEntry(ts: ts, level: level, msg: msg) }
private func runStart(_ ts: Double) -> LogEntry {
    LogEntry(ts: ts, level: "RUN", msg: "started: pid 4242, build 09521c4", run: RunMark(event: .start, pid: 4242, build: "09521c4"))
}
private func cleanEnd(_ ts: Double) -> LogEntry {
    LogEntry(ts: ts, level: "RUN", msg: "ended (clean): signal 15 (SIGTERM)", run: RunMark(event: .end, how: .clean, detail: "signal 15 (SIGTERM)"))
}
private func routine(_ t0: Double, _ hours: Double) -> [LogEntry] {
    stride(from: 600.0, to: hours * 3600, by: 1800).map { entry(t0 + $0, "INFO", "turn \(Int($0 / 1800) + 1) effort=medium todo=待命中 actions=[\"discord.dm\"] prompt=9000") }
}
/// The most recent 03:00 at least `after` seconds before `t`.
private func last3am(before t: Double) -> Double {
    var d = Day(ts: t).at(TimeOfDay("03:00")!)
    while d > t - 3600 { d -= 86_400 }
    return d
}

private let cases: [Case] = [
    Case(name: "a", log: { b in
        [runStart(b - 5 * 3600)] + routine(b - 5 * 3600, 4) + [cleanEnd(b - 3600)]
    }, seed: { _, _, _ in }),
    Case(name: "b", log: { b in
        let t0 = b - 6 * 3600
        var out = [runStart(t0)] + routine(t0, 5)
        for i in 0..<40 {
            out.append(entry(t0 + 300 + Double(i) * 420, "ERROR",
                             "maintenance: audit failed: The request timed out. (POST http://192.0.2.10:8080/v1/chat/completions, request \(7100 + i), after 300 s)"))
        }
        for i in 0..<3 {
            out.append(entry(t0 + 1200 + Double(i) * 5000, "ERROR",
                             "bridge: ERROR dollbridge.voice: voice link to dollcore closed (ConnectionResetError: [Errno 54] Connection reset by peer); reconnecting in \(1 << i) s"))
        }
        out.sort { $0.ts! < $1.ts! }
        return out + [cleanEnd(t0 + 5 * 3600)]
    }, seed: { _, _, _ in }),
    Case(name: "c", log: { b in
        let t0 = b - 3 * 3600
        return [runStart(t0)] + routine(t0, 2) + [entry(b - 1800, "INFO", "turn 9: todo 待命中 → 進行中「桌面上的截圖都在「截圖」資料夾裡」 pending 0"),
                                                  entry(b - 1790, "INFO", "turn 9 effort=medium todo=進行中 actions=[\"todo set\", \"discord.dm\"] prompt=9400")]
    }, seed: { d, clock, b in
        clock.set(b - 1810)
        var owner = Event(.owner, .owner, "幫我把桌面上的截圖都搬到一個叫「截圖」的資料夾")
        owner.channel = .discordDM
        _ = try await d.m9Append(owner)
        clock.set(b - 1800)
        let items = #"["桌面上的截圖都在「截圖」資料夾裡"]"#
        var reply = Event(.doll, .internal, "主人要整理桌面截圖，列一項就好。")
        reply.calls = [ToolCall(name: "todo.set", arguments: #"{"items":\#(items.debugDescription)}"#),
                       ToolCall(name: "discord.dm", arguments: #"{"text":"好～我來整理桌面的截圖！"}"#)]
        let r = try await d.m9Append(reply)
        _ = try await d.m9Append(Event(.tool, .internal, "清單：\n1. 桌面上的截圖都在「截圖」資料夾裡（進行中）"))
        var sent = Event(.tool, .internal, "")
        sent.channel = .discordDM
        _ = try await d.m9Append(sent)
        try await d.m9Doing("桌面上的截圖都在「截圖」資料夾裡", seq: r.seq!)
    }),
    Case(name: "d", log: { b in
        let m = last3am(before: b)
        let t0 = m - 7 * 3600
        return [runStart(t0)] + routine(t0, 6) + [
            entry(m, "INFO", "maintenance: started; 212 events in segment 2026-11-17"),
            entry(m + 40, "INFO", "maintenance: concerns done in 38.2 s: 1 noted"),
            entry(m + 400, "ERROR", "maintenance: consolidate failed: The request timed out. (POST http://192.0.2.10:8080/v1/chat/completions, after 300 s)"),
            entry(m + 401, "INFO", "maintenance: ended; done [\"concerns\"], failed [\"consolidate\"]"),
        ] + routine(m + 3600, 2) + [cleanEnd(min(b - 1800, m + 4 * 3600))]
    }, seed: { d, clock, b in
        let m = last3am(before: b)
        clock.set(m + 401)
        _ = try await d.m9Append(Event(.memory, .internal,
                                       "【系統維護】結束，現在 03:06。從今天的對話記了 1 件掛著的事。沒做成：整理記憶（程式出錯），明天維護的時候再做；之前的對話和做過的事都還在你眼前。今天的行事曆：系統維護 03:00（#1，每天）；主人睡覺 00:00–08:00（#2，每天）。"))
    }),
    Case(name: "e", log: { b in
        let t0 = b - 5 * 3600
        return [runStart(t0)] + routine(t0, 4) + [entry(t0 + 7200, "WARN", "api: refused a connection from 192.168.1.23:52344"), cleanEnd(b - 3600)]
    }, seed: { _, _, _ in }),
]

private func boot(at hhmm: String) -> Double {
    let today = Day(ts: Date().timeIntervalSince1970)
    return today.at(TimeOfDay(hhmm)!)
}

@Test func m9BootReport() async throws {
    guard let out = m9Out.map({ URL(filePath: $0, relativeTo: m9Root) }) else { return }
    let env = ProcessInfo.processInfo.environment
    let runs = Int(env["M9_RUNS"] ?? "3")!
    let only = Set((env["M9_CASES"] ?? "a,b,c,d,e").split(separator: ",").map(String.init))
    let whens = (env["M9_WHEN"] ?? "day,night").split(separator: ",").map(String.init)
    let base = URL(filePath: "/tmp/m9boot")
    let runsDir = out.appending(path: "runs")
    try FileManager.default.createDirectory(at: runsDir, withIntermediateDirectories: true)
    var cfg = try Config.load(m9Root.appending(path: "dollcore.example.toml"))
    cfg.core.pack = "character_packs/doll2/doll.toml"   // measured with 娃娃; the example now starts Doll
    cfg.llm.baseURL = "http://192.0.2.10:8080"
    cfg.llm.model = "qwen3.8-flash-next-unsloth-ud-iq2_xs"
    cfg.voice.enabled = false
    cfg.bridge.enabled = false
    cfg.dashboard.enabled = false
    cfg.goals.review = false
    cfg.audit.enabled = false
    cfg.core.docs = m9Root.appending(path: "dolldocs").path
    for c in cases where only.contains(c.name) {
        for when in whens {
            for i in 1...runs {
                let id = "\(c.name)-\(when)-\(i)"
                let dir = base.appending(path: id)
                try? FileManager.default.removeItem(at: dir)
                var k = cfg
                k.core.dataDir = dir.path
                k.memory.dir = dir.appending(path: "memory").path
                let b = boot(at: when == "day" ? "14:00" : "01:30")
                let clock = M9Clock()
                // The previous run: its timeline (boot notice and what c/d left) and its log.
                let logEntries = c.log(b)
                clock.set(logEntries.first!.ts!)
                let said = Mutex<[String]>([])
                let pre = try Doll(config: k, root: m9Root, model: HTTPMainModel(k.llm), embedder: FakeEmbedder(), computer: RefusingComputer(),
                                   keys: testKeys, deliver: { _, _, _ in nil })
                await pre.setClock(clock)
                try await pre.announceBoot()
                try await c.seed(pre, clock, b)
                let logDir = dir.appending(path: "logs")
                let f = try LogFile(dir: logDir, maxBytes: 10_000_000)
                for e in logEntries { f.write(e) }
                // This boot.
                clock.set(b)
                let comp = RefusingComputer()
                let doll = try Doll(config: k, root: m9Root, model: HTTPMainModel(k.llm), embedder: FakeEmbedder(), computer: comp,
                                    keys: testKeys, deliver: { text, to, _ in
                                        // As live: the owner's DM goes out (the bridge is up); no dollctl chat is open.
                                        let failed: String? = to == .developer ? "沒有開著的 dollctl chat" : nil
                                        said.withLock { $0.append("[\(to.label)\(failed == nil ? "" : "，沒送到")] \(text)") }
                                        return failed
                                    })
                let reports = Mutex<[TurnReport]>([])
                await doll.setObserver { r in reports.withLock { $0.append(r) } }
                await doll.setClock(clock)
                try await doll.boot()
                let started = Date()
                let report = try await doll.announceBoot(previous: try RunLog.lastRun(dir: logDir), build: "m9")
                let runner = Task { try await doll.run() }
                // Settle: no turn expected → 8 s; else until she is settled after a turn, at most 6 turns / 300 s.
                let end = Date().addingTimeInterval(report.turn ? 300 : 8)
                while Date() < end {
                    let n = reports.withLock { $0.count }
                    if n >= 6 { break }
                    if report.turn, n >= 1, await doll.settled { break }
                    try await Task.sleep(for: .milliseconds(300))
                }
                var ownerLine = ""
                if c.name == "e", when == "day" {
                    ownerLine = "在嗎？"
                    let before = reports.withLock { $0.count }
                    await doll.ownerSaid(ownerLine)
                    let end2 = Date().addingTimeInterval(240)
                    while Date() < end2 {
                        let n = reports.withLock { $0.count }
                        if n > before, await doll.settled { break }
                        try await Task.sleep(for: .milliseconds(300))
                    }
                }
                let seconds = Date().timeIntervalSince(started)
                await doll.stop()
                runner.cancel()
                _ = try? await runner.value
                let stats = await doll.turnStats()
                let rs = reports.withLock { $0 }
                let concerns = (try? String(contentsOf: dir.appending(path: "concerns.json"), encoding: .utf8)) ?? ""
                var lines = ["# \(id)", "boot \(LogFile.timeText(b)) turn=\(report.turn) why=\(report.why)", "", "## notice", "", report.notice ?? "(none)", ""]
                if !ownerLine.isEmpty { lines.append("owner after boot: \(ownerLine)") }
                lines.append("## turns (\(rs.count), \(String(format: "%.0f", seconds)) s)")
                for r in rs {
                    let st = stats.values.first { $0.started > 0 && abs($0.prompt - r.usage.promptTokens) == 0 }
                    lines.append("- turn \(r.index) effort=\(r.effort) prompt=\(r.usage.promptTokens) completion=\(r.usage.completionTokens) s=\(st.map { String(format: "%.1f", $0.seconds) } ?? "?")")
                    lines.append("  thought: \(r.reply.replacingOccurrences(of: "\n", with: " ⏎ "))")
                    for call in r.toolCalls { lines.append("  call: \(call.name) \(m9Readable(call.arguments ?? ""))") }
                }
                if let n = report.notice { lines.append("notice tokens (Strata /tokenize): \(try await m9Tokens(n, k.llm.baseURL))") }
                lines.append("## said")
                lines += said.withLock { $0 }
                lines.append("## computer calls")
                lines += comp.calls.withLock { $0 }
                lines.append("## concerns.json")
                lines.append(concerns)
                try lines.joined(separator: "\n").write(to: runsDir.appending(path: "\(id).md"), atomically: true, encoding: .utf8)
                print("m9 \(id): turn=\(report.turn) turns=\(rs.count) said=\(said.withLock { $0.count }) computer=\(comp.calls.withLock { $0.count }) \(String(format: "%.0f", seconds)) s")
            }
        }
    }
}

/// The server's own token count of `text` (llama.cpp `/tokenize`).
private func m9Tokens(_ text: String, _ base: String) async throws -> Int {
    var req = URLRequest(url: URL(string: base + "/tokenize")!)
    req.httpMethod = "POST"
    req.setValue("application/json", forHTTPHeaderField: "Content-Type")
    req.httpBody = try JSONSerialization.data(withJSONObject: ["content": text])
    let (data, _) = try await URLSession.shared.data(for: req)
    return ((try JSONSerialization.jsonObject(with: data) as? [String: Any])?["tokens"] as? [Any])?.count ?? -1
}

/// Call arguments with `\uXXXX` escapes turned back into characters.
private func m9Readable(_ args: String) -> String {
    guard let obj = try? JSONSerialization.jsonObject(with: Data(args.utf8)),
          let data = try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys, .withoutEscapingSlashes]) else { return args }
    return String(decoding: data, as: UTF8.self)
}
