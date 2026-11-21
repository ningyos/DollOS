import Foundation

/// Her claims about her own recent actions, checked before a line of hers goes to a person
/// (2026-11-22, `[claims]`, dolldocs/tool-failures.md). The m11 soak: at 11-20 09:15 she wrote
/// 「我剛查過行事曆確認過了」 with no calendar call since the owner's question (her last
/// `calendar.list` was at 08:40), and at 14:00 「我順手又去查了下週日程」 in a reply whose calls were
/// `owner.feedback` and `discord.dm`. A claim with no matching action of hers since the line she
/// answers refuses the send. Conservative by design: a missed claim costs nothing new, a true line
/// refused costs her a round and the owner a wait.
enum Claims {
    /// What a claim is about, and the calls that back it.
    enum Object: Sendable, Equatable {
        case calendar, memory

        var label: String {
            switch self {
            case .calendar: "行事曆"
            case .memory: "記憶"
            }
        }
        /// The tools that act on it, as her refusal names them.
        var tools: [String] {
            switch self {
            case .calendar: ["calendar.list", "calendar.add", "calendar.change", "calendar.delete"]
            case .memory: ["recall", "remember", Tools.Name.memoryFix]
            }
        }
        func backs(_ call: String) -> Bool {
            switch self {
            case .calendar: call.hasPrefix("calendar.")
            case .memory: tools.contains(call)
            }
        }
    }

    struct Claim: Sendable, Equatable {
        /// Her words, from the verb to the object word: 「查過行事曆」.
        var words: String
        var object: Object
    }

    /// The claims in her text: a verb of `verbs` followed within `reach` characters of its clause by
    /// an object word, not dated earlier, not negated, not the owner's action (`ClaimsConfig`).
    static func find(_ text: String, _ c: ClaimsConfig) -> [Claim] {
        let objects: [(String, Object)] = c.calendarWords.map { ($0, .calendar) } + c.memoryWords.map { ($0, .memory) }
        var out: [Claim] = []
        for sentence in text.split(whereSeparator: { "。！？!?\n".contains($0) }) {
            let s = Array(sentence)
            for verb in c.verbs {
                let v = Array(verb)
                guard s.count >= v.count else { continue }
                for i in 0...(s.count - v.count) where Array(s[i..<(i + v.count)]) == v {
                    let clause = Array(s[(i + v.count)...].prefix { !"，,；;：:、".contains($0) })
                    // The nearest object word starting within reach.
                    let hits = objects.compactMap { word, object in
                        start(of: Array(word), in: clause).flatMap { $0 <= c.reach ? (at: $0, word: word, object: object) : nil }
                    }
                    guard let hit = hits.min(by: { $0.at < $1.at }) else { continue }
                    let before = String(s[..<i])
                    if c.earlier.contains(where: before.contains) { continue }
                    if c.negations.contains(where: before.hasSuffix) { continue }
                    if ownerDid(before, c) { continue }
                    let claim = Claim(words: verb + String(clause[..<hit.at]) + hit.word, object: hit.object)
                    if !out.contains(claim) { out.append(claim) }
                }
            }
        }
        return out
    }

    /// The claims among `find`'s that no call in `done` backs.
    static func unsupported(_ text: String, done: [String], _ c: ClaimsConfig) -> [Claim] {
        find(text, c).filter { claim in !done.contains(where: claim.object.backs) }
    }

    /// Why a line with unsupported claims to `to` is not sent (her tool result after 「失敗：」), or nil when it may go out.
    static func refusal(_ text: String, done: [String], to: Addressee, _ c: ClaimsConfig) -> String? {
        guard c.enabled else { return nil }
        let bad = unsupported(text, done: done, c)
        guard !bad.isEmpty else { return nil }
        let words = bad.map { "「\($0.words)」" }.joined(separator: "、")
        var objects: [Object] = []
        for claim in bad where !objects.contains(claim.object) { objects.append(claim.object) }
        let tools = objects.map { "\($0.label)的工具（\($0.tools.joined(separator: "、"))）" }.joined(separator: "、")
        return "沒有送出，\(to.label)什麼都沒收到。你寫了\(words)，可是從\(to.label)上一句話到現在，你沒有用過\(tools)。"
            + "現在要做就先呼叫它，看了結果再說；是之前做的就講明什麼時候（例如「之前查過」「早上查的」）；不然把這句拿掉。改好整段再傳一次。"
    }

    /// The owner is the one who did it: an owner word after the last 我 before the verb, not right
    /// after a character of `forOwner` (「幫你查了」 is hers).
    static func ownerDid(_ before: String, _ c: ClaimsConfig) -> Bool {
        let b = Array(before)
        let me = b.lastIndex(of: "我") ?? -1
        for word in c.owner {
            let w = Array(word)
            guard b.count >= w.count else { continue }
            for i in stride(from: b.count - w.count, through: me + 1, by: -1) where Array(b[i..<(i + w.count)]) == w {
                if i > 0, c.forOwner.contains(String(b[i - 1])) { continue }
                return true
            }
        }
        return false
    }

    private static func start(of word: [Character], in text: [Character]) -> Int? {
        guard !word.isEmpty, text.count >= word.count else { return nil }
        return (0...(text.count - word.count)).first { Array(text[$0..<($0 + word.count)]) == word }
    }
}
