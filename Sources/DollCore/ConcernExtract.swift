import Foundation

/// The maintenance task that notes concerns from the day's conversation (2026-11-16). In 5 owner-sleep
/// runs the owner told her about a dentist visit in the evening and she never noted a concern, so the
/// next morning nothing brought it back. One request (the consolidation's model and effort) reads the
/// day and the concerns already held, and answers with lines:
///
///     note <owner|duty|own> [due YYYY-MM-DD HH:MM]: <the concern, one sentence>
///     end #<id>: <the owner's words that show it is resolved>
///     none
///
/// Rules, kept by the program and not only asked of the model:
/// - Duplicates: the request lists every open concern; a note whose text, without spaces and
///   punctuation, equals an open concern's or an earlier note's is dropped (logged). Whether two
///   differently worded concerns are the same thing is left to the model, which is told to skip them.
/// - Cap: at most `[drives] maintenance_concerns_max` notes a day, and never above `max_open` open;
///   the rest are dropped in the order written (the model is told to write the important ones first).
/// - Ending: only a concern she or the maintenance noted (not the calendar's or a failed todo's,
///   which the program ends itself), and only with the owner's own words, found verbatim in one of
///   his lines of the day; otherwise it stays open (logged).
/// - Basis (2026-11-22): a note carries 「｜根據：」 and words copied from the day, split at 。！？ and
///   each sentence matched as `todo.done`'s evidence (`Doll.quoted`). Her own words (thoughts, what
///   she said, her call arguments: `sources`) are never a source of facts, as in the audit and
///   `memory.fix`: a note whose sentences are all only in them is written 「她說過…（沒查證）」
///   (`unverified`), except a duty, whose source is her promise; one sentence in another's line keeps
///   it as written. A note without a basis, or with a sentence found nowhere, is dropped (logged). The
///   3-day trial: she told the owner 「下午 17:12 左右我這裡斷了一次」 after a boot report that only said
///   the last log line was at 17:12, and this task wrote 「今天17:12那台電腦沒有錯誤紀錄地斷掉」 as his
///   concern, which she repeated the next day.
/// - A reply that is not in this form fails the task (nothing written; the notice says so).
enum ConcernExtraction {
    struct Note: Sendable, Equatable {
        var about: Concern.About
        var due: Double?
        var text: String
    }
    struct End: Sendable, Equatable {
        var id: Int
        var quote: String
    }
    struct Plan: Sendable, Equatable {
        var notes: [Note] = []
        var ends: [End] = []
        /// What the program left out and why, for the log.
        var dropped: [String] = []
    }
    struct Request: Sendable {
        var messages: [ChatMessage]
        var lastSeq: Int64
    }
    struct Answer: Sendable {
        var plan: Plan
        var reply: ModelReply
        var seconds: Double
    }

    static func system(cap: Int) -> String {
        """
        你在幫一個 AI 角色（下面的「她」）看完她一天的對話，找出之後值得她掛著、過幾天再想起來的事（掛念）。只記對話裡真的出現的事，不要猜。
        三種：
        - owner：主人的事，之後值得問一聲、關心一下的（例：他看了牙醫→之後問牙齒還痛不痛；他說明天要考試→之後問考得怎樣）
        - duty：她答應要做、還沒做完的事
        - own：她自己的、還沒了結的事
        不要記：
        - 已經掛著的（下面列出）。換句話說的同一件事也算同一件。
        - 只是到時間要提醒的事：行事曆上有了，時間到行事曆會叫她。除了時間以外還有要關心的（例：看完牙醫之後問感覺），只記要關心的那部分。
        - 已經做完、說完的事。
        結束一件掛著的：只有主人在今天的對話裡明明白白說它已經解決了，才寫，而且照抄他那句話；不確定就不要結束。行事曆的和程式記的不用你結束。
        \(Consolidation.ownWords)
        每件都要寫根據：照抄今天的對話裡支持它的那一句（或其中一段）。

        每行一件，只用這三種格式，不要寫別的：
        note owner|duty|own: 用她的口吻寫這件事，一句 ｜根據：照抄的話
        note owner|duty|own due YYYY-MM-DD HH:MM: 同上，有確定的時間才寫 due ｜根據：照抄的話
        end #編號: 主人的原話
        一件都沒有就只寫一行：none
        """
    }

    /// The request: the open concerns with their ids, then the day as consolidation renders it.
    static func gather(events: [Event], open: [Concern], cap: Int, now: Double) -> Request {
        let held = open.isEmpty ? "（沒有）" : open.map { "#\($0.id)〔\($0.about.rawValue)〕\($0.text)" + ($0.source == "her" || $0.source == "maintenance" ? "" : "（\($0.source == "calendar" ? "行事曆" : "程式")的）") }.joined(separator: "\n")
        let day = Consolidation.tasks(events).map(\.text).joined(separator: "\n\n")
        let user = "現在 \(Doll.nowText(now))。\n\n已經掛著的：\n\(held)\n\n今天的對話：\n\(day)"
        return Request(messages: [ChatMessage(role: "system", content: system(cap: cap)), ChatMessage(role: "user", content: user)],
                       lastSeq: events.last?.seq ?? 0)
    }

    /// The day's texts a basis is looked up in, as `Consolidation.tasks` shows them: `others` are
    /// people's lines, tool results (not recall's and the others `TodayIndex` leaves out), watch wakes,
    /// the calendar and the boot notice; `own` are her thoughts and every argument of her calls.
    static func sources(_ events: [Event]) -> (others: [String], own: [String]) {
        let names = Doll.callNames(events)
        var others: [String] = [], own: [String] = []
        for ev in events {
            switch ev.kind {
            case .owner, .developer, .stranger, .watch, .calendar: others.append(ev.text)
            case .tool: if !TodayIndex.unindexedResults.contains(names[ev.seq!] ?? "") { others.append(ev.text) }
            case .memory: if ev.text.hasPrefix(Doll.bootNotice) { others.append(ev.text) }
            case .doll: own += [ev.text] + ev.calls.flatMap { $0.parameters.map(\.value) }
            }
        }
        return (others, own)
    }

    /// A concern only her own words support, as the maintenance writes it.
    static func unverified(_ text: String) -> String {
        text.hasPrefix("她說過") && text.contains("沒查證") ? text : "她說過" + text + "（沒查證）"
    }

    /// Text without spaces and punctuation, for the duplicate rule.
    static func key(_ s: String) -> String {
        String(s.unicodeScalars.filter { !CharacterSet.whitespacesAndNewlines.contains($0) && !CharacterSet.punctuationCharacters.contains($0)
            && !CharacterSet.symbols.contains($0) }.map(Character.init))
    }

    /// Parses the reply and applies the rules; throws on a line not in the form. `others` and `own`:
    /// `sources`.
    static func parse(_ reply: String, open: [Concern], ownerLines: [String], others: [String], own: [String],
                      cap: Int, room: Int) throws -> Plan {
        var plan = Plan()
        var seen = Set(open.map { key($0.text) })
        let endable = Set(open.filter { $0.source == "her" || $0.source == "maintenance" }.map(\.id))
        let lines = reply.split(separator: "\n").map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty && !$0.hasPrefix("```") }
        if lines == ["none"] { return plan }
        for line in lines {
            // The separator is the first colon (half- or full-width) not inside a time like 12:30.
            let chars = Array(line)
            let at = chars.indices.first { i in
                (chars[i] == ":" || chars[i] == "：") && !(i > 0 && i + 1 < chars.count && chars[i - 1].isNumber && chars[i + 1].isNumber)
            }
            guard let at else { throw ModelError("concern extraction: unreadable line「\(line)」") }
            let colon = line.index(line.startIndex, offsetBy: at)
            let head = line[..<colon].split(separator: " ").map(String.init)
            var body = line[line.index(after: colon)...].trimmingCharacters(in: .whitespaces)
            guard !body.isEmpty else { throw ModelError("concern extraction: empty text in「\(line)」") }
            switch head.first {
            case "note":
                guard head.count == 2 || (head.count == 5 && head[2] == "due"), let about = Concern.About(rawValue: head[1]) else {
                    throw ModelError("concern extraction: unreadable line「\(line)」")
                }
                var due: Double?
                if head.count == 5 {
                    guard let d = Day(head[3]), let t = TimeOfDay(head[4]) else { throw ModelError("concern extraction: unreadable due in「\(line)」") }
                    due = d.at(t)
                }
                guard let b = body.range(of: "根據[：:]", options: [.regularExpression, .backwards]) else {
                    plan.dropped.append("no basis「\(body)」")
                    continue
                }
                let basis = body[b.upperBound...].trimmingCharacters(in: CharacterSet(charactersIn: "「」\"“” "))
                body = body[..<b.lowerBound].trimmingCharacters(in: CharacterSet(charactersIn: "｜| ").union(.whitespaces))
                guard !body.isEmpty else { throw ModelError("concern extraction: empty text in「\(line)」") }
                if body.hasSuffix("。") { body = String(body.dropLast()) }
                // Each sentence of the basis on its own (2026-11-22, m15 fix 3 real check: a basis copied
                // from the boot notice as three sentences joined by 。 was not found whole): every piece
                // must be in the day; only her own words when none of them is in anyone else's.
                let pieces = basis.split(whereSeparator: { "。！？".contains($0) }).map(String.init).filter { !key($0).isEmpty }
                let inOthers = pieces.map { p in others.contains { Doll.quoted(p, in: $0) } }
                guard !pieces.isEmpty, pieces.indices.allSatisfy({ i in inOthers[i] || own.contains { Doll.quoted(pieces[i], in: $0) } }) else {
                    plan.dropped.append("basis「\(basis)」 is not in the day「\(body)」")
                    continue
                }
                if !inOthers.contains(true), about != .duty { body = unverified(body) }
                let k = key(body)
                if seen.contains(k) { plan.dropped.append("duplicate「\(body)」"); continue }
                if plan.notes.count >= min(cap, room) {
                    plan.dropped.append("over the cap (\(min(cap, room)))「\(body)」")
                    continue
                }
                seen.insert(k)
                // Without a closing 。: the notice puts its own after the list (real run: 「…再問一聲。。」).
                plan.notes.append(Note(about: about, due: due, text: body))
            case "end":
                guard head.count == 2, head[1].hasPrefix("#"), let id = Int(head[1].dropFirst()) else {
                    throw ModelError("concern extraction: unreadable line「\(line)」")
                }
                let quote = body.trimmingCharacters(in: CharacterSet(charactersIn: "「」\"“” "))
                guard endable.contains(id) else { plan.dropped.append("end #\(id): not one she or the maintenance noted, or not open"); continue }
                guard !quote.isEmpty, ownerLines.contains(where: { $0.contains(quote) }) else {
                    plan.dropped.append("end #\(id): 「\(quote)」 is not in the owner's lines")
                    continue
                }
                guard !plan.ends.contains(where: { $0.id == id }) else { continue }
                plan.ends.append(End(id: id, quote: quote))
            default:
                throw ModelError("concern extraction: unreadable line「\(line)」")
            }
        }
        return plan
    }

    /// The end notice's words: what was noted and ended, plainly, so she knows where they came from.
    static func doneText(added: [Concern], ended: [(Concern, String)]) -> String {
        var parts: [String] = []
        if added.isEmpty && ended.isEmpty { return "從今天的對話沒有另外記掛著的事。" }
        if !added.isEmpty {
            parts.append("從今天的對話記了 \(added.count) 件掛著的事：" + added.map { "#\($0.id)〔\($0.about.label)〕\($0.text)" }.joined(separator: "；") + "。")
        }
        if !ended.isEmpty {
            parts.append("結束了 \(ended.count) 件：" + ended.map { "#\($0.0.id) \($0.0.text)（主人說「\($0.1)」）" }.joined(separator: "；") + "。")
        }
        return parts.joined()
    }
}

extension Doll {
    /// Writes nothing; cancellable with its request.
    func prepareConcernExtraction() async throws -> ConcernExtraction.Answer {
        let events = timeline.events
        let open = openConcerns
        let cap = drivesConfig.maintenanceConcernsMax
        let r = ConcernExtraction.gather(events: events, open: open, cap: cap, now: wallClock.now())
        let started = Date()
        let reply = try await maintenanceModel.chat(r.messages, tools: nil, effort: config.consolidate.effort)
        let (others, own) = ConcernExtraction.sources(events)
        let plan = try ConcernExtraction.parse(reply.content, open: open, ownerLines: events.filter { $0.kind == .owner }.map(\.text),
                                               others: others, own: own, cap: cap, room: max(0, drivesConfig.maxOpen - open.count))
        return ConcernExtraction.Answer(plan: plan, reply: reply, seconds: Date().timeIntervalSince(started))
    }

    /// All of it in one write of `concerns.json`.
    func commitConcernExtraction(_ a: ConcernExtraction.Answer) throws -> MaintenanceDone {
        let before = concernFile
        let now = wallClock.now()
        var added: [Concern] = [], ended: [(Concern, String)] = []
        for n in a.plan.notes {
            let c = Concern(id: concernFile.nextId, text: n.text, about: n.about, source: "maintenance", created: now, due: n.due)
            concernFile.nextId += 1
            concernFile.items.append(c)
            added.append(c)
        }
        for e in a.plan.ends {
            guard let i = concernFile.items.firstIndex(where: { $0.id == e.id && $0.ended == nil }) else { continue }
            concernFile.items[i].ended = now
            concernFile.items[i].end = "done"
            ended.append((concernFile.items[i], e.quote))
        }
        do { try saveConcerns() } catch {
            concernFile = before
            throw error
        }
        for c in added {
            concernsNew.insert(c.id)
            log("INFO", "drives: concern #\(c.id) (\(c.about.rawValue), maintenance) 「\(c.text)」")
        }
        for (c, q) in ended { log("INFO", "drives: concern #\(c.id) ended (maintenance: the owner said「\(q)」)") }
        for d in a.plan.dropped { log("INFO", "drives: maintenance concern left out: \(d)") }
        feed.notify(.drives)
        let u = a.reply.usage
        return MaintenanceDone(
            log: "\(added.count) noted, \(ended.count) ended, \(a.plan.dropped.count) left out; request \(u.promptTokens) prompt tokens, "
                + "\(u.completionTokens) completion, \(String(format: "%.1f", a.seconds)) s, \(u.stopLine); reply「\(a.reply.content.replacingOccurrences(of: "\n", with: "⏎"))」",
            words: ConcernExtraction.doneText(added: added, ended: ended), summary: nil, kept: nil)
    }
}
