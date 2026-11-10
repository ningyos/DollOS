import Foundation
import Testing
@testable import DollCore

@Test func tmpDumpEmptyReplyRequest() async throws {
    var cfg = try Config.load(URL(filePath: "~/Desktop/DollOS/DollOS/.worktrees/rewrite/experiments/m2/expstats/expstats.toml"))
    cfg.core.dataDir = "/tmp/emptyrepro/data"
    cfg.memory.dir = "/tmp/emptyrepro/data/memory"
    let doll = try Doll(config: cfg, root: URL(filePath: "~/Desktop/DollOS/DollOS/.worktrees/rewrite"), model: HTTPMainModel(cfg.llm), say: { _ in })
    var msgs = try await doll.context(await doll.timelineEvents())
    msgs.append(ChatMessage(role: "user", content: try String(contentsOfFile: "/tmp/emptyrepro/footer.txt", encoding: .utf8)))
    struct Body: Encodable { let model: String; let messages: [ChatMessage]; let max_tokens: Int; let reasoning_effort: String }
    let data = try ChatMessage.encoder.encode(Body(model: cfg.llm.model, messages: msgs, max_tokens: cfg.llm.maxTokens, reasoning_effort: "medium"))
    try data.write(to: URL(filePath: "/tmp/emptyrepro/request.json"))
    print("DUMPED \(msgs.count) messages \(data.count) bytes")
}
