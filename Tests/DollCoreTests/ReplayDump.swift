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
