import Foundation
import Testing
@testable import DollCore

/// Rebuilds the main-model requests of a stored run (the messages of each turn, as the turn loop
/// renders them) so they can be replayed against the server. Runs only with REPLAY_DIR set.
@Test func replayDumpRequests() async throws {
    guard let dir = ProcessInfo.processInfo.environment["REPLAY_DIR"] else { return }
    let root = URL(filePath: "~/Desktop/DollOS/DollOS/.worktrees/rewrite")
    var cfg = try Config.load(URL(filePath: ProcessInfo.processInfo.environment["REPLAY_CONFIG"]!))
    cfg.core.dataDir = dir + "/data"
    cfg.memory.dir = dir + "/memory"
    cfg.voice.enabled = false
    let doll = try Doll(config: cfg, root: root, model: HTTPMainModel(cfg.llm), embedder: FakeEmbedder(), deliver: { _, _ in nil })
    let events = await doll.timelineEvents()
    var out: [[String: Any]] = []
    for reply in events where reply.kind == .doll {
        let seq = reply.seq!
        let before = events.filter { $0.seq! < seq }
        var msgs = try await doll.context(before)
        msgs.append(ChatMessage(role: "user", content: reply.footer ?? ""))
        let data = try ChatMessage.encoder.encode(msgs)
        out.append(["seq": seq, "messages": try JSONSerialization.jsonObject(with: data), "reply": reply.text])
    }
    let tools = try JSONSerialization.jsonObject(with: Data(await doll.toolsJSON.utf8))
    let all = try JSONSerialization.data(withJSONObject: ["tools": tools, "turns": out], options: [.prettyPrinted])
    try all.write(to: URL(filePath: dir + "/requests.json"))
    print("DUMPED \(out.count) turns")
}

/// Asks the server for the concern extraction of a stored day (EXTRACT_DIR/timeline holds the one
/// segment), EXTRACT_N times, and prints each reply and what the program kept. Runs only with EXTRACT_DIR set.
@Test func replayConcernExtraction() async throws {
    guard let dir = ProcessInfo.processInfo.environment["EXTRACT_DIR"] else { return }
    let cfg = try Config.load(URL(filePath: ProcessInfo.processInfo.environment["REPLAY_CONFIG"]!))
    let timeline = try Timeline(dir: URL(filePath: dir + "/timeline"))
    let open = [Concern(id: 1, text: "主人跟老闆開會（2026-11-17（星期二） 09:00）", about: .duty, source: "calendar", created: 0, due: 0)]
    let model = HTTPMainModel(cfg.llm)
    let r = ConcernExtraction.gather(events: timeline.events, open: open, cap: cfg.drives.maintenanceConcernsMax, now: timeline.events.last!.ts + 3600)
    print("PROMPT_CHARS system=\(r.messages[0].content.count) user=\(r.messages[1].content.count)")
    for k in 0..<(Int(ProcessInfo.processInfo.environment["EXTRACT_N"] ?? "1") ?? 1) {
        let t0 = Date()
        let reply = try await model.chat(r.messages, tools: nil, effort: cfg.consolidate.effort)
        let plan = try? ConcernExtraction.parse(reply.content, open: open, ownerLines: timeline.events.filter { $0.kind == .owner }.map(\.text),
                                                cap: cfg.drives.maintenanceConcernsMax, room: 19)
        print("EXTRACT \(k) \(String(format: "%.1f", Date().timeIntervalSince(t0)))s prompt=\(reply.usage.promptTokens) \(reply.usage.stopLine) reply=「\(reply.content.replacingOccurrences(of: "\n", with: "⏎"))」 notes=\(plan.map { $0.notes.map { "\($0.about.rawValue)|\($0.due.map { Doll.nowText($0) } ?? "-")|\($0.text)" } } ?? ["PARSE FAILED"]) dropped=\(plan?.dropped ?? [])")
    }
}
