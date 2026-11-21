import Foundation
import Testing
@testable import DollCore

@Test(.disabled("/tmp/emptyrepro holds a timeline from before native tool calls (2026-11-14); it no longer loads"))
func tmpDumpEmptyReplyRequest() async throws {
    let repo = URL(filePath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    var cfg = try Config.load(repo.appending(path: "experiments/m2/expstats/expstats.toml"))
    cfg.core.dataDir = "/tmp/emptyrepro/data"
    cfg.memory.dir = "/tmp/emptyrepro/data/memory"
    let doll = try Doll(config: cfg, root: repo, model: HTTPMainModel(cfg.llm), embedder: try await MLXEmbedder.load(cfg.memory), keys: testKeys, deliver: { _, _, _ in nil })
    var msgs = try await doll.context(await doll.timelineEvents())
    msgs.append(ChatMessage(role: "user", content: try String(contentsOfFile: "/tmp/emptyrepro/footer.txt", encoding: .utf8)))
    struct Body: Encodable { let model: String; let messages: [ChatMessage]; let max_tokens: Int; let reasoning_effort: String }
    let data = try ChatMessage.encoder.encode(Body(model: cfg.llm.model, messages: msgs, max_tokens: cfg.llm.maxTokens, reasoning_effort: "medium"))
    try data.write(to: URL(filePath: "/tmp/emptyrepro/request.json"))
    print("DUMPED \(msgs.count) messages \(data.count) bytes")
}
