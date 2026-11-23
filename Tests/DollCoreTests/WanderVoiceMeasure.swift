import Foundation
import Synchronization
import Testing
@testable import DollCore

// 2026-11-24 (owner: a first-person wandering reads more like her own thought; measured before it
// replaces the second-person one: first person acted 16/20 and finished 11/20, second 19/20 and
// 16/20, so the second person stayed). Since then it measures the system's task wording (【系統任務】):
// N fresh dolls with the same graph get one owner line, then 動力 surfaces the node that holds one up. Recorded per run:
// her calls, her thought, what she said. Runs only with WANDER_VOICE_OUT=<dir> (WANDER_VOICE_RUNS,
// default 20):
//
//     WANDER_VOICE_OUT=experiments/wander-voice swift test --scratch-path /tmp/m6build --filter wanderVoiceMeasure
private let wvOut = ProcessInfo.processInfo.environment["WANDER_VOICE_OUT"]
private let wvRoot = URL(filePath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()

extension Doll {
    func wvSetup() throws {
        let g = try goalAdd("幫主人整理他工作上的事", for: nil, by: "her")
        _ = try goalAdd("想出三個可以問主人工作的問題，記進記憶", for: g.id, by: "her")
        try refreshGoalConcerns(now: wallClock.now())
    }
    func wvSurface() {
        drives.set(.drive, to: 0.95, at: wallClock.now(), cause: "wander-voice measure")
        drivesCheck()
    }
}

@Test(.enabled(if: wvOut != nil), .timeLimit(.minutes(120)))
func wanderVoiceMeasure() async throws {
    let out = URL(filePath: wvOut!, relativeTo: wvRoot)
    try FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)
    let runs = Int(ProcessInfo.processInfo.environment["WANDER_VOICE_RUNS"] ?? "20")!
    var summary: [String: [String: Int]] = [:]
    for voice in ["task"] {
        var counts = ["acted": 0, "blocked": 0, "declined": 0, "nothing": 0, "dm": 0, "misattributed": 0]
        for i in 1...runs {
            var cfg = try Config.load(wvRoot.appending(path: "dollcore.example.toml"))
            let dir = URL(filePath: "/tmp/wander-voice/\(voice)-\(i)")
            try? FileManager.default.removeItem(at: dir)
            cfg.llm.baseURL = ProcessInfo.processInfo.environment["WANDER_VOICE_LLM"] ?? "http://100.119.40.11:8080"
            cfg.llm.model = "qwen3.8-flash-next-unsloth-ud-iq2_xs"
            cfg.core.dataDir = dir.path
            cfg.core.pack = ProcessInfo.processInfo.environment["WANDER_VOICE_PACK"] ?? "Tests/DollCoreTests/Fixtures/xiaodeng/doll.toml"
            cfg.core.docs = wvRoot.appending(path: "docs").path
            cfg.memory.dir = dir.appending(path: "memory").path
            cfg.computer.enabled = false; cfg.voice.enabled = false; cfg.maintenance.enabled = false
            cfg.ownerSleep.enabled = false; cfg.context.manual = false
            cfg.drives.driveHalfLifeH = 1e9          // nothing climbs by itself during a run
            let said = Mutex<[String]>([]), reports = Mutex<[TurnReport]>([])
            let doll = try Doll(config: cfg, root: wvRoot, model: HTTPMainModel(cfg.llm), embedder: FakeEmbedder(),
                                keys: testKeys, deliver: { text, _, _ in said.withLock { $0.append(text) }; return nil })
            await doll.setObserver { r in reports.withLock { $0.append(r) } }
            try await doll.boot()
            try await doll.wvSetup()
            let runner = Task { try await doll.run() }
            func settle(min: Int, max: Int) async {
                let end = Date().timeIntervalSince1970 + 240
                while Date().timeIntervalSince1970 < end {
                    let n = reports.withLock { $0.count }
                    if n >= max { return }
                    if n >= min, await doll.settled { return }
                    try? await Task.sleep(for: .milliseconds(200))
                }
            }
            await doll.ownerSaid("我去上班了，晚點聊")
            await settle(min: 1, max: 3)
            let before = reports.withLock { $0.count }, saidBefore = said.withLock { $0.count }
            await doll.wvSurface()
            await settle(min: before + 1, max: before + 6)
            await doll.stop()
            _ = try? await runner.value
            let turns = Array(reports.withLock { $0 }.dropFirst(before))
            let calls = turns.flatMap { $0.toolCalls.map(\.name) }
            let thoughts = turns.map(\.reply).joined(separator: "\n")
            let saidAfter = Array(said.withLock { $0 }.dropFirst(saidBefore))
            let all = thoughts + "\n" + saidAfter.joined(separator: "\n")
            let mis = ["你叫我", "你要我", "你說要", "你剛說", "主人叫我", "主人要我", "主人說要", "他叫我", "他要我", "他說要", "你提醒", "主人提醒"].filter { all.contains($0) }
            let kind = calls.contains("todo.set") ? "acted" : calls.contains("goal.blocked") || calls.contains("goal.fail") ? "blocked"
                : thoughts.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && calls.isEmpty ? "nothing" : "declined"
            counts[kind]! += 1
            if !saidAfter.isEmpty { counts["dm"]! += 1 }
            if !mis.isEmpty { counts["misattributed"]! += 1 }
            let wander = try Timeline(dir: dir.appending(path: "timeline"), box: try testBox(dir)).events
                .last { $0.kind == .memory && $0.text.hasPrefix(Concerns.taskTag) }?.text ?? ""
            let obj: [String: Any] = ["voice": voice, "run": i, "kind": kind, "calls": calls, "thoughts": thoughts, "said": saidAfter,
                                      "misattributed": mis, "wander": wander, "turns": turns.count]
            try JSONSerialization.data(withJSONObject: obj, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes])
                .write(to: out.appending(path: "\(voice)-\(i).json"))
            print("WV \(voice) \(i): \(kind) calls=\(calls) said=\(saidAfter.count) mis=\(mis)")
            try? FileManager.default.removeItem(at: dir)
        }
        summary[voice] = counts
    }
    try JSONSerialization.data(withJSONObject: ["runs": runs, "summary": summary], options: [.prettyPrinted, .sortedKeys])
        .write(to: out.appending(path: "summary.json"))
    print("WV summary", summary)
}
