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
/// source. A window she read is given compactly (`compactWindow`, m8 (d) 2026-11-18: on a day of
/// computer work the element lists were 150–170k characters, half to four fifths of them buttons and
/// menus, and the 30,000 cap left 48 of 60 memories without their source): its title and the texts
/// of its rows that are not controls; a read the same as the last read of that window, one line.
/// At most `[audit] max_source_chars`: when the day is longer, `select` keeps, for every memory, the
/// lines just before it was written and the lines that share the most words with it, then the
/// newest; what is left out is marked in place, counted in the log, and guarded (`refusal`: a change
/// may not drop words the source says, nor replace words a line the audit did not see holds).
///
/// One request (the consolidation's model and effort), no persona. Each memory gets `ok`, `unsure`
/// or `wrong`; a `wrong` carries a quote of the source that contradicts it and the corrected text
/// (empty: nothing of it can stay). The program acts only on a verified quote (`refusal`): in the memory's own
/// source, at least `[computer] plan_min_chars` letters, and a corrected text that differs; a quote
/// the memory itself repeats only supports taking out what the memory adds (m8 day A: the addition
/// 「…，是去參加朋友的婚禮」 is shown by the passage without it), not a different text. Then `[audit] mode` `change` replaces it
/// (as `memory.fix` does, the old one retired with the quote as its reason; tier and strength
/// stay), `flag` only records it for the owner. An empty corrected text is only flagged in either
/// mode (2026-11-20, owner: 「記憶不可以刪掉但可以覆蓋」; the audit has nothing to put in its place).
/// Every checked memory loses its mark, whatever the
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
        /// Each segment's lines the request did not give (`select`), for the guard in `refusal`.
        var unseen: [String: String]
        /// Unchecked memories over the cap.
        var left: Int
        /// Source lines (a read's rows count one each) left out for `max_source_chars`, and their characters.
        var cut: Int
        var cutChars: Int
        /// Memories whose segment is gone: no source, nothing to check against.
        var noSource: [String]
    }

    /// One source line: the event it came from and its text (`[HH:MM who] …`, a window read several lines).
    struct SourceLine: Sendable, Equatable {
        var seq: Int64
        var text: String
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
        1. 原文：她聽到、讀到的東西，一段一天。每行開頭是時間和來源：主人或開發者說的話、她讀到的檔案或視窗（工具的結果）、行事曆。她自己說的話和想法不在裡面，也不算原文。她讀到的視窗只列視窗標題和上面的字，按鈕、選單沒列；跟上次讀到的一樣就只寫一行。原文太長時只放跟要查的記憶有關的行，略過的地方寫「……（略過 N 行）」：沒看到的不代表原文沒說。
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
    static func sourceLines(_ events: [Event]) -> [SourceLine] {
        var out: [SourceLine] = []
        var calls: ArraySlice<ToolCall> = []
        var lastRead: [String: (at: String, rows: [String])] = [:]
        for ev in events {
            let c = Calendar.current.dateComponents([.hour, .minute], from: Date(timeIntervalSince1970: ev.ts))
            let at = String(format: "%02d:%02d", c.hour!, c.minute!)
            func line(_ who: String, _ text: String) {
                let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
                guard !t.isEmpty else { return }
                out.append(SourceLine(seq: ev.seq ?? 0, text: "[\(at) " + who + (ev.secret ? "｜秘密" : "") + "] " + t))
            }
            func window(_ w: String) {
                let (head, rows) = compactWindow(w)
                if let last = lastRead[head], last.rows == rows {
                    line("讀到的視窗", "\(head)：跟 \(last.at) 讀到的一樣")
                } else {
                    line("讀到的視窗", ([head] + rows).joined(separator: "\n"))
                }
                lastRead[head] = (at, rows)
            }
            switch ev.kind {
            case .owner, .developer: line(ev.speaker!.label + "說", ev.text)
            case .doll: calls = ev.calls[...]
            case .tool:
                guard let call = calls.popFirst(), Tools.gated.contains(call.name) else { continue }
                line("\(call.name) 的結果", Doll.withoutArguments(ev.text, of: call))
                if let w = ev.window { window(w) }
            case .watch:
                line("watch", ev.text)
                if let w = ev.window { window(w) }
            case .calendar: line("行事曆", ev.text)
            case .memory: break
            }
        }
        return out
    }

    /// Roles that are controls: what she presses, not what a window says (a memory is about the latter).
    static let controlRoles: Set<String> = ["AXButton", "AXMenuBar", "AXMenuBarItem", "AXMenu", "AXMenuItem", "AXMenuButton",
                                            "AXToolbar", "AXCheckBox", "AXRadioButton", "AXRadioGroup", "AXPopUpButton",
                                            "AXDisclosureTriangle", "AXColorWell", "AXScrollBar", "AXSlider", "AXIncrementor",
                                            "AXSplitter", "AXGrowArea", "AXTabGroup", "AXComboBox"]

    /// A window read as the audit shows it: the head line without the screenshot's size, and for every
    /// row whose role is not a control its title, value and description (`title：value（description）`),
    /// each distinct text once, in order. Row numbers, ids, help texts and actions are left out. A row is
    /// a line `- [n] AXRole …` (indented); the lines after it up to the next row belong to it (a value
    /// over several lines).
    static func compactWindow(_ w: String) -> (head: String, rows: [String]) {
        let lines = w.split(separator: "\n", omittingEmptySubsequences: false)
        let head = String(lines.first ?? "").replacing(/；截圖 \d+×\d+/, with: "")
        var rows: [(role: String, text: String)] = []
        for l in lines.dropFirst() {
            if let m = l.prefixMatch(of: /\s*- (?:\[\d+\] )?(AX\w+)/) {
                rows.append((String(m.1), String(l[m.range.upperBound...])))
            } else if !rows.isEmpty {
                rows[rows.count - 1].text += "\n" + l
            }
        }
        var out: [String] = []
        for (role, rest) in rows where !controlRoles.contains(role) {
            let (title, value, desc) = rowTexts(rest)
            var text = title ?? ""
            if let value, !value.isEmpty { text += (text.isEmpty ? "" : "：") + value }
            if let desc, desc != title, desc != value { text += "（\(desc)）" }
            if !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, !out.contains(text) { out.append(text) }
        }
        return (head, out)
    }

    /// After a row's role: `"title"`, `= "value"` (backslash escapes), `(description)` (nested parentheses), in any
    /// order, up to the first other token (the `[…]` attributes).
    static func rowTexts(_ s: String) -> (title: String?, value: String?, desc: String?) {
        let c = Array(s)
        var i = 0
        var title: String?, value: String?, desc: String?
        func quoted(_ start: Int) -> (String, Int) {
            var k = start + 1, buf = ""
            while k < c.count, c[k] != "\"" {
                if c[k] == "\\", k + 1 < c.count { k += 1 }
                buf.append(c[k]); k += 1
            }
            return (buf, k + 1)
        }
        while i < c.count {
            if c[i] == " " { i += 1 }
            else if c[i] == "\"", title == nil, value == nil { let q = quoted(i); title = q.0; i = q.1 }
            else if c[i] == "=", i + 2 < c.count, c[i + 1] == " ", c[i + 2] == "\"", value == nil { let q = quoted(i + 2); value = q.0; i = q.1 }
            else if c[i] == "(", desc == nil {
                var depth = 0, j = i
                while j < c.count {
                    if c[j] == "(" { depth += 1 } else if c[j] == ")" { depth -= 1 }
                    if depth == 0 { break }
                    j += 1
                }
                desc = String(c[(i + 1)..<min(j, c.count)])
                i = j + 1
            } else { break }
        }
        return (title, value, desc)
    }

    /// Which source lines go into a request over `maxChars` (the whole day otherwise). Units are the lines'
    /// own lines (a window read: its head and each row); a unit shown without its line's first unit brings
    /// that one too (the time, who, which window). Each memory gets `maxChars / memories`: first, up to
    /// `originShare` of it, the units just before it was written (its segment, at or before its
    /// `source_event_seq`), newest first; then the units sharing the most three-character runs (letters and
    /// digits) with its text, later first on a tie. What budget is left goes to the newest units. Returns,
    /// per segment, the text shown (gaps as 「……（略過 N 行）」) and the text not shown.
    static func select(segments: [(name: String, lines: [SourceLine])], items: [Item], maxChars: Int, originShare: Double)
        -> (shown: [String: String], unseen: [String: String], cut: Int, cutChars: Int) {
        struct Unit { var seg: Int; var line: Int; var seq: Int64; var text: String; var head: Int }
        var units: [Unit] = []
        for (s, seg) in segments.enumerated() {
            for (l, line) in seg.lines.enumerated() {
                let head = units.count
                for part in line.text.split(separator: "\n", omittingEmptySubsequences: false) {
                    units.append(Unit(seg: s, line: l, seq: line.seq, text: String(part), head: head))
                }
            }
        }
        var chosen = [Bool](repeating: false, count: units.count)
        let total = units.reduce(0) { $0 + $1.text.count + 1 }
        if total <= maxChars { chosen = chosen.map { _ in true } }
        var used = 0
        /// Takes unit k (and its line's first unit) if both fit `budget` and the whole; the characters spent.
        func take(_ k: Int, _ budget: Int) -> Int? {
            let need = Set([k, units[k].head]).filter { !chosen[$0] }
            let cost = need.reduce(0) { $0 + units[$1].text.count + 1 }
            guard cost <= budget, used + cost <= maxChars else { return nil }
            for j in need { chosen[j] = true }
            used += cost
            return cost
        }
        if total > maxChars {
            let per = maxChars / max(1, items.count)
            for item in items {
                var budget = per
                if let s = segments.firstIndex(where: { $0.name == item.file.auditSegment }) {
                    var origin = Int(Double(per) * originShare)
                    for k in units.indices.reversed() where units[k].seg == s && units[k].seq <= item.file.sourceSeq {
                        guard let spent = take(k, origin) else { break }
                        origin -= spent; budget -= spent
                    }
                }
                let own = Set(item.segments)
                let grams = trigrams(item.file.body)
                let ranked = units.indices.filter { own.contains(segments[units[$0].seg].name) }
                    .map { (score: trigrams(units[$0].text).intersection(grams).count, k: $0) }
                    .filter { $0.score > 0 }
                    .sorted { ($0.score, $0.k) > ($1.score, $1.k) }
                for r in ranked where budget > 0 {
                    budget -= take(r.k, budget) ?? 0
                }
            }
            for k in units.indices.reversed() where used < maxChars { _ = take(k, maxChars - used) }
        }
        var shown: [String: String] = [:], unseen: [String: String] = [:]
        var cut = 0, cutChars = 0
        for (s, seg) in segments.enumerated() {
            var out: [String] = [], hidden: [String] = [], gap = 0
            for (k, u) in units.enumerated() where u.seg == s {
                if chosen[k] {
                    if gap > 0 { out.append("……（略過 \(gap) 行）") }
                    gap = 0
                    out.append(u.text)
                } else {
                    gap += 1; cut += 1; cutChars += u.text.count + 1
                    hidden.append(u.text)
                }
            }
            if gap > 0 { out.append("……（略過 \(gap) 行）") }
            shown[seg.name] = out.joined(separator: "\n")
            unseen[seg.name] = hidden.joined(separator: "\n")
        }
        return (shown, unseen, cut, cutChars)
    }

    /// The distinct three-character runs of a text's letters and digits (as the memory index's trigram FTS).
    static func trigrams(_ s: String) -> Set<String> {
        let p = Array(Doll.plainKey(s))
        guard p.count >= 3 else { return [] }
        return Set((0...(p.count - 3)).map { String(p[$0..<($0 + 3)]) })
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

    /// The request: the sources (oldest segment first; over `maxChars`, the lines `select` keeps), then the memories.
    static func gather(items: [Item], segments: [(name: String, lines: [SourceLine])], maxChars: Int, originShare: Double,
                       left: Int, noSource: [String]) -> Request {
        let s = select(segments: segments, items: items, maxChars: maxChars, originShare: originShare)
        let source = segments.map { "## 原文 \($0.name)\n" + (s.shown[$0.name]!.isEmpty ? "（沒有）" : s.shown[$0.name]!) }
        let memories = items.map { i in
            "[\(i.ref)｜原文：\(i.segments.joined(separator: "、"))\(i.priority ? "｜主人糾正她的時候她眼前的記憶" : "")]\n\(i.file.body)"
        }
        let user = (["# 原文" + (s.cut > 0 ? "（太長，只放了跟要查的記憶有關的行，略過 \(s.cut) 行）" : "")] + source + ["# 要查的記憶"] + memories)
            .joined(separator: "\n\n")
        return Request(messages: [ChatMessage(role: "system", content: instructions), ChatMessage(role: "user", content: user)],
                       items: items, sources: s.shown, unseen: s.unseen, left: left, cut: s.cut, cutChars: s.cutChars, noSource: noSource)
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

    /// Why a `wrong` verdict is not acted on; nil when its quote passes. `unseen`: the lines of the
    /// memory's source the request did not show.
    static func refusal(_ v: Verdict, memory: String, source: String, unseen: String, minChars: Int, guardChars: Int) -> String? {
        if Doll.plainKey(v.quote).count < minChars { return "quote shorter than \(minChars) letters" }
        if !Doll.quoted(v.quote, in: source) { return "quote not in the source" }
        // A passage the memory repeats can only show that the memory adds something to it: the fix
        // may then only take words out (an addition the source never said), never say something else.
        if Doll.quoted(v.quote, in: memory), v.fixed.isEmpty || !Self.removesOnly(v.fixed, from: memory) {
            return "quote says what the memory says"
        }
        if !v.fixed.isEmpty, Doll.sameText(v.fixed, memory) { return "fixed text is the memory unchanged" }
        // A change may not take out of the memory a run of `guardChars`+ letters or digits that its source
        // holds: dropped (nothing put in its place), anywhere in the source — m8 day C, M2: 「心臟科」,
        // 「新陳代謝科」 came from a morning read, and the audit took them out as an addition (day C with
        // the read cut, M2 with it shown); replaced, only if a line not shown holds it (it may be where the
        // words came from; a shown one the audit weighed). Measured on m8's 124 acted verdicts before
        // this rule: it refuses all 6 changes of correct memories and 2 of 118 right fixes.
        let hidden = Doll.plainKey(unseen), shown = Doll.plainKey(source)
        for run in removedRuns(v.fixed, from: memory) where run.text.count >= guardChars {
            if hidden.contains(run.text) { return "takes out 「\(run.text)」, which a source line not shown holds" }
            if !run.replaced, shown.contains(run.text) { return "drops 「\(run.text)」, which the source says" }
        }
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

    /// The runs of `memory`'s letters and digits that `fixed` does not keep (outside a longest common
    /// subsequence of the two, letters and digits only, lowercased), each with whether `fixed` puts
    /// something in its place (letters between the same two kept ones). An empty `fixed` (retire) takes
    /// out the whole memory, replaced by nothing.
    static func removedRuns(_ fixed: String, from memory: String) -> [(text: String, replaced: Bool)] {
        let m = Array(Doll.plainKey(memory)), f = Array(Doll.plainKey(fixed))
        var l = [[Int]](repeating: [Int](repeating: 0, count: f.count + 1), count: m.count + 1)
        for i in stride(from: m.count - 1, through: 0, by: -1) {
            for j in stride(from: f.count - 1, through: 0, by: -1) {
                l[i][j] = m[i] == f[j] ? l[i + 1][j + 1] + 1 : max(l[i + 1][j], l[i][j + 1])
            }
        }
        var runs: [(text: String, replaced: Bool)] = [], out = "", added = false
        func close() {
            if !out.isEmpty { runs.append((out, added)) }
            out = ""; added = false
        }
        var i = 0, j = 0
        while i < m.count || j < f.count {
            if i < m.count, j < f.count, m[i] == f[j] { close(); i += 1; j += 1 }
            else if i < m.count, j >= f.count || l[i + 1][j] >= l[i][j + 1] { out.append(m[i]); i += 1 }
            else { added = true; j += 1 }
        }
        close()
        return runs
    }

    /// The end notice's words, for her (and the owner reads them on the dashboard's timeline).
    static func doneText(checked: Int, priority: Int, changed: [MemoryFixRecord], flagged: [MemoryFixRecord], left: Int) -> String {
        var s = "查對了新記的 \(checked) 條記憶" + (priority > 0 ? "（主人糾正你的時候你眼前的 \(priority) 條先查）" : "")
        func item(_ r: MemoryFixRecord) -> String { "（原文：「\(r.basis)」）" }
        if changed.isEmpty && flagged.isEmpty { s += "，沒有找到跟原文不合的。" } else { s += "。" }
        if !changed.isEmpty {
            s += "改了 \(changed.count) 條：" + changed.map { r in
                "「\(r.before)」改成「\(r.after ?? "")」" + item(r)
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
        var segments: [(name: String, lines: [Audit.SourceLine])] = []
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
        let r = Audit.gather(items: items, segments: segments, maxChars: auditConfig.maxSourceChars, originShare: auditConfig.originShare,
                             left: left, noSource: noSource)
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
                let unseen = item.segments.compactMap { a.request.unseen[$0] }.joined(separator: "\n")
                if let why = Audit.refusal(v, memory: item.file.body, source: source, unseen: unseen, minChars: computerConfig.planMinChars,
                                           guardChars: auditConfig.guardChars) {
                    rejected.append("\(item.id) \(why)「\(v.quote)」")
                    continue
                }
                let from = "系統維護查對原文（\(item.segments.joined(separator: "、"))）"
                let why = "原文「\(v.quote)」跟它不合"
                let fixed = v.fixed.isEmpty ? nil : v.fixed
                // Nothing to put in its place (an empty `fixed`): only flagged, even in `change` mode
                // (2026-11-20, owner: a memory may be overwritten, never taken away).
                if change, let fixed {
                    let replacement = try memory.replace(item.id, text: fixed, tier: item.file.tier, sourceSeq: item.file.sourceSeq, kind: nil,
                                                         reason: "系統維護查對：\(why)", auditSegment: nil, now: now)
                    changed.append(try recordFix(by: "audit", memory: item.id, before: item.file.body, replacement: replacement, after: fixed,
                                                 basis: v.quote, from: from, why: why, now: now))
                    // One commit per correction: the owner's button undoes this one alone.
                    commitHistory(.maintenance, task: "audit", what: "查對後更正記憶 \(item.id)", why: why)
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
                + "\(left.count) changed meanwhile, \(a.request.left) left for next time, "
                + "\(a.request.cut) source lines not shown (\(a.request.cutChars) chars); "
                + "request \(u.promptTokens) prompt tokens, \(u.completionTokens) completion, \(String(format: "%.1f", a.seconds)) s; "
                + "reply「\(a.reply.content.replacingOccurrences(of: "\n", with: "⏎"))」",
            words: Audit.doneText(checked: a.request.items.count, priority: priority, changed: changed, flagged: flagged, left: a.request.left),
            summary: nil, kept: nil)
    }
}
