import Foundation

/// The maintenance's audit of her new memories (owner, 2026-11-05 and 2026-11-18): nothing checked
/// what was written into memory against where it came from.
///
/// What it checks: every memory not yet audited (front-matter `audit_pending`, set when her
/// `remember`, her `memory.fix` or consolidation's add/update writes it, with the segment it came
/// from), so a day the maintenance skipped or was interrupted is checked the next time; first, the
/// memories that were in front of her when the owner corrected her today (the 【相關記憶】 and
/// `recall` results between his line before and her `owner.feedback` correction), whether new or
/// not. At most `[audit] max_memories` (priority first, then oldest first); the rest wait.
///
/// Where it sits: after `review`, before `consolidate`. It reads the day while it is still the open
/// segment, and its corrections are in place before consolidation recalls related memories, so
/// consolidation does not build on a wrong one. What tonight's consolidation writes is audited at
/// the next maintenance, against its segment (kept on disk).
///
/// The source: the segment(s) the memories came from, as lines from others: people's lines, the
/// computer and file tools' results (her argument values cut, as `Doll.citable`), watch wakes, the
/// calendar. Never her replies, thoughts, memories or recall results: her own words are never a
/// source. At most `[audit] max_source_chars`, the newest kept.
///
/// One request (the consolidation's model and effort), no persona. Each memory gets `ok`, `unsure`
/// or `wrong`; a `wrong` carries a quote of the source that contradicts it and the corrected text
/// (empty: retire it). The program acts only on a verified quote (`refusal`): in the memory's own
/// source, at least `[computer] plan_min_chars` letters, and a corrected text that differs; a quote
/// the memory itself repeats only supports taking out what the memory adds (m8 day A: the addition
/// 「…，是去參加朋友的婚禮」 is shown by the passage without it), not a different text. Then `[audit] mode` `change` replaces or retires it
/// (as `memory.fix` does, the old one retired with the quote as its reason; tier and strength
/// stay), `flag` only records it for the owner. Every checked memory loses its mark, whatever the
/// verdict: an `unsure` one, or one whose quote failed the check, is left as it is and not asked
/// again. The end notice lists what was changed or flagged and the quote; the dashboard too.
enum Audit {
    struct Item: Sendable {
        var ref: String
        var id: String
        var folder: MemoryFolder
        var file: MemoryFile
        /// In front of her when the owner corrected her.
        var priority: Bool
        /// The segments whose lines are its source.
        var segments: [String]
    }

    struct Request: Sendable {
        var messages: [ChatMessage]
        var items: [Item]
        /// Each segment's source text as the request gave it.
        var sources: [String: String]
        /// Unchecked memories over the cap.
        var left: Int
        /// Source lines left out for `max_source_chars`.
        var cut: Int
        /// Memories whose segment is gone: no source, nothing to check against.
        var noSource: [String]
    }

    struct Verdict: Sendable, Equatable {
        var ref: String
        var verdict: String
        var quote: String
        var fixed: String
    }

    struct Answer: Sendable {
        var request: Request
        var verdicts: [Verdict]
        var reply: ModelReply
        var seconds: Double
    }

    static let instructions = """
        你在幫一個 AI 助理查對她的記憶有沒有記錯。你不是她，不用扮演誰，只輸出查對結果。

        輸入有兩部分：
        1. 原文：她聽到、讀到的東西，一段一天。每行開頭是時間和來源：主人或開發者說的話、她讀到的檔案或視窗（工具的結果）、行事曆。她自己說的話和想法不在裡面，也不算原文。
        2. 要查的記憶：每條有代號（m1、m2…）和它的原文是哪一段。

        每條記憶判斷一個結果：
        - ok：原文支持它；或換了說法、摘要、把幾句合在一起，但意思沒變；或原文沒提到相關的事。
        - wrong：原文裡有一段話明明白白跟它不合：日期、時間、數字、名字、地點不一樣；原文說的是可能、還沒確定，記憶寫成確定；或記憶多了原文沒說的事，而原文講同一件事的那段話沒有這部分。
        - unsure：原文有相關的話，可是看不出它對不對。
        後來的話改掉先前的話時（例如主人先說週三、後來說「不對，是週四」；檔案後來讀到的內容跟先前不一樣），以後來的為準。

        wrong 一定要給：
        - quote：一字不差照抄原文裡跟它不合的那一段（不要抄行首的時間和來源，不要改字、不要用…省略）。抄不出原文的字，就不能判 wrong。
        - fixed：照原文改好的整條記憶，其他寫對的部分照留；整條都不對、沒有可以留的，給空字串。

        輸出格式：只輸出一個 ```json 區塊：
        ```json
        {"results": [{"m": "m1", "verdict": "ok"}, {"m": "m2", "verdict": "wrong", "quote": "…", "fixed": "…"}, {"m": "m3", "verdict": "unsure"}]}
        ```
        每條記憶剛好一個結果。
        """

    /// One segment's source lines: what others said and what she read, never her own words.
    static func sourceLines(_ events: [Event]) -> [String] {
        var out: [String] = []
        var calls: ArraySlice<ToolCall> = []
        for ev in events {
            let c = Calendar.current.dateComponents([.hour, .minute], from: Date(timeIntervalSince1970: ev.ts))
            func line(_ who: String, _ text: String) {
                let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
                guard !t.isEmpty else { return }
                out.append(String(format: "[%02d:%02d ", c.hour!, c.minute!) + who + (ev.secret ? "｜秘密" : "") + "] " + t)
            }
            switch ev.kind {
            case .owner, .developer: line(ev.speaker!.label + "說", ev.text)
            case .doll: calls = ev.calls[...]
            case .tool:
                guard let call = calls.popFirst(), Tools.gated.contains(call.name) else { continue }
                line("\(call.name) 的結果", Doll.withoutArguments(ev.text, of: call))
                if let w = ev.window { line("讀到的視窗", w) }
            case .watch:
                line("watch", ev.text)
                if let w = ev.window { line("讀到的視窗", w) }
            case .calendar: line("行事曆", ev.text)
            case .memory: break
            }
        }
        return out
    }

    /// The memory ids that were in front of her when the owner corrected her: in 【相關記憶】 and
    /// tool results (recall) from his line before the corrected one up to her `owner.feedback`
    /// correction call, in order of appearance.
    static func shownAtCorrections(_ events: [Event]) -> [String] {
        var out: [String] = []
        for (i, ev) in events.enumerated() where ev.kind == .doll {
            for call in ev.calls where call.name == "owner.feedback"
                && call.parameters.contains(where: { $0.name == "kind" && $0.value == "correction" }) {
                let q = call.parameters.first { $0.name == "quote" }?.value.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
                guard !q.isEmpty, let line = events[..<i].lastIndex(where: { $0.kind == .owner && $0.text.contains(q) }) else { continue }
                let start = events[..<line].lastIndex(where: { $0.speaker != nil }).map { $0 + 1 } ?? 0
                for e in events[start..<i] where e.kind == .memory || e.kind == .tool {
                    for m in e.text.matches(of: /｜記憶 ([0-9]+-[0-9a-f]{6})〕/) where !out.contains(String(m.1)) {
                        out.append(String(m.1))
                    }
                }
            }
        }
        return out
    }

    /// The request: the sources (oldest segment first, the oldest lines cut over `maxChars`), then the memories.
    static func gather(items: [Item], segments: [(name: String, lines: [String])], maxChars: Int, left: Int, noSource: [String]) -> Request {
        var total = segments.reduce(0) { $0 + $1.lines.reduce(0) { $0 + $1.count + 1 } }
        var cut = 0
        var kept = segments
        for s in kept.indices {
            while total > maxChars, !kept[s].lines.isEmpty {
                total -= kept[s].lines.removeFirst().count + 1
                cut += 1
            }
        }
        var sources: [String: String] = [:]
        for s in kept { sources[s.name] = s.lines.joined(separator: "\n") }
        let source = kept.map { "## 原文 \($0.name)\n" + ($0.lines.isEmpty ? "（沒有）" : $0.lines.joined(separator: "\n")) }
        let memories = items.map { i in
            "[\(i.ref)｜原文：\(i.segments.joined(separator: "、"))\(i.priority ? "｜主人糾正她的時候她眼前的記憶" : "")]\n\(i.file.body)"
        }
        let user = (["# 原文" + (cut > 0 ? "（太長，最前面 \(cut) 行沒放進來）" : "")] + source + ["# 要查的記憶"] + memories).joined(separator: "\n\n")
        return Request(messages: [ChatMessage(role: "system", content: instructions), ChatMessage(role: "user", content: user)],
                       items: items, sources: sources, left: left, cut: cut, noSource: noSource)
    }

    /// The reply: exactly one ```json block {"results": [...]}, one result per memory ref; each with
    /// `m` and `verdict` (ok, unsure, wrong), a wrong one also `quote` (not empty) and `fixed`
    /// (strings). Anything else is an error naming what is wrong.
    static func parse(_ reply: String, refs: [String]) throws -> [Verdict] {
        let blocks = reply.components(separatedBy: "```json")
        guard blocks.count == 2, let close = blocks[1].range(of: "```") else {
            throw ConsolidationError("the audit reply must hold exactly one ```json block; got \(blocks.count - 1)")
        }
        guard let top = (try? JSONSerialization.jsonObject(with: Data(blocks[1][..<close.lowerBound].utf8))) as? [String: Any],
              Set(top.keys) == ["results"], let list = top["results"] as? [Any] else {
            throw ConsolidationError("the audit's ```json block must be an object with exactly one key, results, an array")
        }
        var out: [Verdict] = []
        for (n, item) in list.enumerated() {
            guard let o = item as? [String: Any], let ref = o["m"] as? String, let verdict = o["verdict"] as? String,
                  Set(o.keys).isSubset(of: ["m", "verdict", "quote", "fixed"]) else {
                throw ConsolidationError("audit result \(n + 1) must be an object with m and verdict (strings), and quote and fixed for wrong")
            }
            guard refs.contains(ref), !out.contains(where: { $0.ref == ref }) else {
                throw ConsolidationError("audit result \(n + 1): m \(ref) is not a memory of the request, or is given twice")
            }
            switch verdict {
            case "ok", "unsure": out.append(Verdict(ref: ref, verdict: verdict, quote: "", fixed: ""))
            case "wrong":
                guard let quote = (o["quote"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines), !quote.isEmpty,
                      let fixed = (o["fixed"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines) else {
                    throw ConsolidationError("audit result \(n + 1) (\(ref)): wrong needs a quote (not empty) and fixed (a string, empty to retire)")
                }
                out.append(Verdict(ref: ref, verdict: verdict, quote: quote, fixed: fixed))
            default:
                throw ConsolidationError("audit result \(n + 1) (\(ref)): verdict must be ok, unsure or wrong; got \(verdict)")
            }
        }
        let missing = refs.filter { r in !out.contains { $0.ref == r } }
        guard missing.isEmpty else { throw ConsolidationError("the audit reply has no result for \(missing.joined(separator: ", "))") }
        return out
    }

    /// Why a `wrong` verdict is not acted on; nil when its quote passes.
    static func refusal(_ v: Verdict, memory: String, source: String, minChars: Int) -> String? {
        if Doll.plainKey(v.quote).count < minChars { return "quote shorter than \(minChars) letters" }
        if !Doll.quoted(v.quote, in: source) { return "quote not in the source" }
        // A passage the memory repeats can only show that the memory adds something to it: the fix
        // may then only take words out (an addition the source never said), never say something else.
        if Doll.quoted(v.quote, in: memory), v.fixed.isEmpty || !Self.removesOnly(v.fixed, from: memory) {
            return "quote says what the memory says"
        }
        if !v.fixed.isEmpty, Doll.sameText(v.fixed, memory) { return "fixed text is the memory unchanged" }
        return nil
    }

    /// `fixed` is `memory` with some letters and digits taken out and none added or changed (in order).
    static func removesOnly(_ fixed: String, from memory: String) -> Bool {
        let f = Array(Doll.plainKey(fixed)), m = Array(Doll.plainKey(memory))
        guard f.count < m.count else { return false }
        var i = 0
        for c in m where i < f.count && c == f[i] { i += 1 }
        return i == f.count
    }

    /// The end notice's words, for her (and the owner reads them on the dashboard's timeline).
    static func doneText(checked: Int, priority: Int, changed: [MemoryFixRecord], flagged: [MemoryFixRecord], left: Int) -> String {
        var s = "查對了新記的 \(checked) 條記憶" + (priority > 0 ? "（主人糾正你的時候你眼前的 \(priority) 條先查）" : "")
        func item(_ r: MemoryFixRecord) -> String { "（原文：「\(r.basis)」）" }
        if changed.isEmpty && flagged.isEmpty { s += "，沒有找到跟原文不合的。" } else { s += "。" }
        if !changed.isEmpty {
            s += "改了 \(changed.count) 條：" + changed.map { r in
                (r.after.map { "「\(r.before)」改成「\($0)」" } ?? "「\(r.before)」收起來了") + item(r)
            }.joined(separator: "；") + "。舊的留著，主人在儀表板看得到、可以還原。"
        }
        if !flagged.isEmpty {
            s += "有 \(flagged.count) 條可能記錯了，先沒改，留給主人看：" + flagged.map { r in
                "「\(r.before)」（原文：「\(r.basis)」" + (r.after.map { "，可能該是「\($0)」" } ?? "") + "）"
            }.joined(separator: "；") + "。"
        }
        if left > 0 { s += "還有 \(left) 條這次沒查，下次維護再查。" }
        return s
    }
}

extension Doll {
    /// What the audit checks now: the memories in front of her at the owner's corrections today,
    /// then the unchecked ones, oldest first; at most `[audit] max_memories`.
    func auditCandidates() throws -> (items: [Audit.Item], left: Int) {
        var items: [Audit.Item] = []
        var taken = Set<String>()
        for id in Audit.shownAtCorrections(timeline.events) {
            guard let (folder, f) = try memory.file(id), f.retired == nil, Memory.strengthTiers.contains(f.tier) else { continue }
            taken.insert(id)
            items.append(Audit.Item(ref: "", id: id, folder: folder, file: f, priority: true,
                                    segments: Set([f.auditSegment, timeline.segment].compactMap { $0 }).sorted()))
        }
        for p in try memory.pendingAudit() where !taken.contains(p.id) {
            items.append(Audit.Item(ref: "", id: p.id, folder: p.folder, file: p.file, priority: false, segments: [p.file.auditSegment!]))
        }
        var kept = Array(items.prefix(auditConfig.maxMemories))
        for i in kept.indices { kept[i].ref = "m\(i + 1)" }
        return (kept, items.count - kept.count)
    }

    var auditDue: Bool {
        guard auditConfig.enabled else { return false }
        do { return !(try auditCandidates().items.isEmpty) } catch {
            log("ERROR", "maintenance: audit: listing the memories failed: \(error)")
            return true   // `prepare` fails with the same error, so it is reported
        }
    }

    /// Writes nothing; cancellable with its request.
    func prepareAudit() async throws -> Audit.Answer {
        let (all, left) = try auditCandidates()
        var segments: [(name: String, lines: [String])] = []
        var gone = Set<String>()
        for name in Set(all.flatMap(\.segments)).sorted() {
            if name == timeline.segment {
                segments.append((name, Audit.sourceLines(timeline.events)))
            } else if FileManager.default.fileExists(atPath: timelineDir.appending(path: name + ".jsonl").path) {
                segments.append((name, Audit.sourceLines(try Timeline(dir: timelineDir, segment: name).events)))
            } else {
                gone.insert(name)
            }
        }
        // A memory whose every source segment is gone has nothing to be checked against.
        let noSource = all.filter { $0.segments.allSatisfy(gone.contains) }.map(\.id)
        var items = all.filter { !noSource.contains($0.id) }
        for i in items.indices { items[i].segments.removeAll(where: gone.contains) }
        let r = Audit.gather(items: items, segments: segments, maxChars: auditConfig.maxSourceChars, left: left, noSource: noSource)
        guard !items.isEmpty else { return Audit.Answer(request: r, verdicts: [], reply: ModelReply(content: "", usage: ModelUsage(promptTokens: 0, completionTokens: 0)), seconds: 0) }
        let started = Date()
        let reply = try await maintenanceModel.chat(r.messages, tools: nil, effort: config.consolidate.effort)
        let verdicts = try Audit.parse(reply.content, refs: items.map(\.ref))
        return Audit.Answer(request: r, verdicts: verdicts, reply: reply, seconds: Date().timeIntervalSince(started))
    }

    /// Synchronous: the changes (or flags), the records, the marks cleared.
    func commitAudit(_ a: Audit.Answer) throws -> MaintenanceDone {
        let now = wallClock.now()
        let change = auditConfig.mode == "change"
        var changed: [MemoryFixRecord] = [], flagged: [MemoryFixRecord] = [], rejected: [String] = [], left: [String] = []
        var ok = 0, unsure = 0
        for item in a.request.items {
            let v = a.verdicts.first { $0.ref == item.ref }!
            // Changed since the request was gathered (the owner's revert): left as it is.
            guard let current = try memory.file(item.id), current.file.retired == nil, current.file.body == item.file.body else {
                left.append(item.id)
                continue
            }
            switch v.verdict {
            case "ok": ok += 1
            case "unsure": unsure += 1
            default:
                let source = item.segments.compactMap { a.request.sources[$0] }.joined(separator: "\n")
                if let why = Audit.refusal(v, memory: item.file.body, source: source, minChars: computerConfig.planMinChars) {
                    rejected.append("\(item.id) \(why)「\(v.quote)」")
                    continue
                }
                let from = "系統維護查對原文（\(item.segments.joined(separator: "、"))）"
                let why = "原文「\(v.quote)」跟它不合"
                let fixed = v.fixed.isEmpty ? nil : v.fixed
                if change {
                    var replacement: String?
                    if let fixed {
                        replacement = try memory.replace(item.id, text: fixed, tier: item.file.tier, sourceSeq: item.file.sourceSeq, kind: nil,
                                                         reason: "系統維護查對：\(why)", auditSegment: nil, now: now)
                    } else {
                        try memory.retire(item.id, Retirement(ts: now, supersededBy: "", reason: "系統維護查對：\(why)"))
                    }
                    changed.append(try recordFix(by: "audit", memory: item.id, before: item.file.body, replacement: replacement, after: fixed,
                                                 basis: v.quote, from: from, why: why, now: now))
                } else {
                    flagged.append(try recordFix(by: "flag", memory: item.id, before: item.file.body, replacement: nil, after: fixed,
                                                 basis: v.quote, from: from, why: why, now: now))
                }
            }
        }
        for id in a.request.items.map(\.id) + a.request.noSource where !left.contains(id) { try memory.clearAudit(id) }
        for r in changed { log("INFO", "audit: changed \(r.memory) → \(r.replacement ?? "retired")「\(r.before)」⇒「\(r.after ?? "")」quote「\(r.basis)」") }
        for r in flagged { log("INFO", "audit: flagged \(r.memory)「\(r.before)」quote「\(r.basis)」proposed「\(r.after ?? "")」") }
        for r in rejected { log("INFO", "audit: quote rejected: \(r)") }
        let u = a.reply.usage
        let priority = a.request.items.filter(\.priority).count
        return MaintenanceDone(
            log: "\(a.request.items.count) checked (\(priority) priority), \(ok) ok, \(unsure) unsure, \(changed.count) changed, "
                + "\(flagged.count) flagged, \(rejected.count) quotes rejected, \(a.request.noSource.count) without a source, "
                + "\(left.count) changed meanwhile, \(a.request.left) left for next time, \(a.request.cut) source lines cut; "
                + "request \(u.promptTokens) prompt tokens, \(u.completionTokens) completion, \(String(format: "%.1f", a.seconds)) s; "
                + "reply「\(a.reply.content.replacingOccurrences(of: "\n", with: "⏎"))」",
            words: Audit.doneText(checked: a.request.items.count, priority: priority, changed: changed, flagged: flagged, left: a.request.left),
            summary: nil, kept: nil)
    }
}
