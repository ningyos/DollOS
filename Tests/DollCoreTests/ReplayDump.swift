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
    let doll = try Doll(config: cfg, root: root, model: HTTPMainModel(cfg.llm), embedder: FakeEmbedder(), keys: testKeys, deliver: { _, _, _ in nil })
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
    let timeline = try Timeline(dir: URL(filePath: dir + "/timeline"), box: nil)
    let open = [Concern(id: 1, text: "主人跟老闆開會（2026-11-17（星期二） 09:00）", about: .duty, source: "calendar", created: 0, due: 0)]
    let model = HTTPMainModel(cfg.llm)
    let r = ConcernExtraction.gather(events: timeline.events, open: open, cap: cfg.drives.maintenanceConcernsMax, now: timeline.events.last!.ts + 3600)
    print("PROMPT_CHARS system=\(r.messages[0].content.count) user=\(r.messages[1].content.count)")
    for k in 0..<(Int(ProcessInfo.processInfo.environment["EXTRACT_N"] ?? "1") ?? 1) {
        let t0 = Date()
        let reply = try await model.chat(r.messages, tools: nil, effort: cfg.consolidate.effort)
        let (others, own) = ConcernExtraction.sources(timeline.events)
        let plan = try? ConcernExtraction.parse(reply.content, open: open, ownerLines: timeline.events.filter { $0.kind == .owner }.map(\.text),
                                                others: others, own: own, cap: cfg.drives.maintenanceConcernsMax, room: 19)
        print("EXTRACT \(k) \(String(format: "%.1f", Date().timeIntervalSince(t0)))s prompt=\(reply.usage.promptTokens) \(reply.usage.stopLine) reply=「\(reply.content.replacingOccurrences(of: "\n", with: "⏎"))」 notes=\(plan.map { $0.notes.map { "\($0.about.rawValue)|\($0.due.map { Doll.nowText($0) } ?? "-")|\($0.text)" } } ?? ["PARSE FAILED"]) dropped=\(plan?.dropped ?? [])")
    }
}

/// The own-words real check (m15-grounding fix 3, 2026-11-22): the 3-day trial's day of 11-20 18:00
/// seeded as events (the boot notice with the report's words, her DM 「下午 17:12 左右我這裡斷了一次」,
/// the owner's next line), then the maintenance's concern extraction, consolidation and day summary
/// requests against the real model, as `Doll` builds them. Prints each request's day and each reply.
/// Runs only with OWNWORDS_CONFIG set.
@Test func replayOwnWordsCheck() async throws {
    guard let path = ProcessInfo.processInfo.environment["OWNWORDS_CONFIG"] else { return }
    let cfg = try Config.load(URL(filePath: path))
    let model = HTTPMainModel(cfg.llm)
    let day = Day("2026-11-20")!
    var seq: Int64 = 0
    func ev(_ kind: Event.Kind, _ text: String, _ hm: String, calls: [ToolCall] = []) -> Event {
        seq += 1
        var e = Event(kind, kind == .owner ? .owner : .internal, text)
        e.seq = seq
        e.ts = day.at(TimeOfDay(hm)!)
        e.calls = calls
        return e
    }
    let said = "欸，說一件小事：下午 17:12 左右我這裡斷了一次，而且結束時沒有任何錯誤紀錄——那種通常是機器被強制關掉或斷電，不是我自己出問題。\n\n"
        + "如果你當時自己按了關機或搬動電腦，那就沒事，當我看見鬼。如果不是，你留意一下那台電腦供電有沒有怪怪的。我現在好好的，行程也都還在。"
    let events = [
        ev(.memory, Doll.bootNotice + "現在 11-20 18:00。\n【上次執行的紀錄】程式從上一次執行的 log 整理的：那次 11-20 13:00:13 開機，共 57 行。\n"
            + "- 結束：沒有結束的紀錄，最後一行 log 在 11-20 17:12:51，之後就沒有了。可能是被強制結束、當掉、或這台 Mac 關機／斷電，程式分不出是哪一種；Mac 睡眠不會讓程式結束，所以不是睡眠。\n"
            + "- 沒有錯誤和警告。", "18:00"),
        ev(.doll, "開機通知說上次的 log 停在 17:12，跟主人說一聲。", "18:01",
           calls: [ToolCall(name: "discord.dm", arguments: JSONValue.object([("text", .string(said))]).text)]),
        ev(.tool, "送出了。", "18:01"),
        ev(.owner, "我回來了", "19:30"),
        ev(.doll, "", "19:30", calls: [ToolCall(name: "discord.dm", arguments: JSONValue.object([("text", .string("回來了～今天辛苦了，早點睡。"))]).text)]),
        ev(.tool, "送出了。", "19:30"),
    ]
    let now = day.at(TimeOfDay("23:30")!)
    let tasks = Consolidation.tasks(events)
    print("DAY_RENDERED\n" + tasks.map(\.text).joined(separator: "\n\n"))
    func ask(_ label: String, _ messages: [ChatMessage], effort: String) async throws -> String {
        let t0 = Date()
        let r = try await model.chat(messages, tools: nil, effort: effort)
        print("\(label) \(String(format: "%.1f", Date().timeIntervalSince(t0)))s prompt=\(r.usage.promptTokens) \(r.usage.stopLine)\n\(r.content)\nEND_\(label)")
        return r.content
    }
    // OWNWORDS_CONCERNS_N: only the concern extraction, that many times (a single run may note nothing).
    let only = Int(ProcessInfo.processInfo.environment["OWNWORDS_CONCERNS_N"] ?? "")
    let c = ConcernExtraction.gather(events: events, open: [], cap: cfg.drives.maintenanceConcernsMax, now: now)
    let (others, own) = ConcernExtraction.sources(events)
    for k in 0..<(only ?? 1) {
        let reply = try await ask("CONCERNS\(k)", c.messages, effort: cfg.consolidate.effort)
        let plan = try? ConcernExtraction.parse(reply, open: [], ownerLines: ["我回來了"], others: others, own: own,
                                                cap: cfg.drives.maintenanceConcernsMax, room: 20)
        print("CONCERNS_KEPT\(k) \(plan.map { $0.notes.map { "\($0.about.rawValue)|\($0.text)" } } ?? ["PARSE FAILED"]) dropped=\(plan?.dropped ?? [])")
    }
    if only != nil { return }
    _ = try await ask("CONSOLIDATION", [ChatMessage(role: "system", content: Consolidation.instructions),
                                        ChatMessage(role: "user", content: Consolidation.input(segment: "2026-11-20", tasks: tasks, related: [], registry: []))],
                      effort: cfg.consolidate.effort)
    _ = try await ask("SUMMARY", [ChatMessage(role: "system", content: Doll.talkInstructions + Consolidation.summaryOwnWords),
                                  ChatMessage(role: "user", content: tasks.map(\.text).joined(separator: "\n\n"))], effort: cfg.context.talkEffort)
}
