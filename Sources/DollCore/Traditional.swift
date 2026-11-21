import Foundation

/// What she says to people goes out in traditional characters (owner, 2026-11-15): over two days
/// of real runs 21 of 344 of her texts carried simplified characters although her persona forbids
/// them. Only characters that traditional writing does not use are converted: a CJK character Big5
/// cannot encode is taken for simplified and replaced by ICU's Hans→Hant form; characters both
/// scripts use (里, 后, 干, 面, 台…) stay as they are, so traditional text is never changed. A
/// simplified character with several traditional forms (发: 發／髮, 历: 歷／曆) gets ICU's one choice.
enum Traditional {
    private static let big5 = String.Encoding(rawValue: CFStringConvertEncodingToNSStringEncoding(
        CFStringEncoding(CFStringEncodings.big5.rawValue)))
    private static let hansToHant = StringTransform("Hans-Hant")

    /// `text` with its simplified-only characters replaced, and each replacement once, in order
    /// of first appearance (「来→來」).
    static func fix(_ text: String) -> (text: String, changes: [String]) {
        var out = "", changes: [String] = [], seen = Set<Character>()
        out.reserveCapacity(text.utf8.count)
        for ch in text {
            // 么 is in Big5 (a form of 幺, as in 老么), so the rule below takes it for traditional; in
            // her texts it is the simplified 麼 (「牙齒怎么了」 reached the owner, 2026-11-16).
            if ch == "么", out.last != "老" {
                out += "麼"
                if seen.insert(ch).inserted { changes.append("么→麼") }
                continue
            }
            guard let s = ch.unicodeScalars.first, ch.unicodeScalars.count == 1, (0x4E00...0x9FFF).contains(s.value),
                  String(ch).data(using: big5) == nil,
                  let t = String(ch).applyingTransform(hansToHant, reverse: false), t != String(ch) else {
                out.append(ch)
                continue
            }
            out += t
            if seen.insert(ch).inserted { changes.append("\(ch)→\(t)") }
        }
        return (out, changes)
    }

    /// The line added to a speaking tool's result when something was replaced.
    static func note(_ changes: [String]) -> String {
        "你寫了簡體字，送出去的時候程式改成繁體了：\(changes.joined(separator: "、"))。下次直接寫繁體。"
    }
}

extension Tools.Request {
    /// What she writes into what is hers is kept in traditional characters too (2026-11-20, the
    /// soak run: 「脚本」「读」 in a step of her goal tree, 「独角鯨」「各种」 in a memory). Only the texts
    /// that are kept: a memory, a goal-tree node, a concern, her ideal self. Quotes that must match
    /// something existing (`old`, `basis`, evidence) stay as she wrote them.
    func inTraditional() -> (Tools.Request, [String]) {
        var changes: [String] = []
        func fix(_ t: String) -> String {
            let r = Traditional.fix(t)
            for c in r.changes where !changes.contains(c) { changes.append(c) }
            return r.text
        }
        let out: Tools.Request
        switch self {
        case .remember(let items): out = .remember(items.map { var i = $0; i.text = fix(i.text); return i })
        case .memoryFix(let id, let text, let basis, let why): out = .memoryFix(id: id, text: text.map(fix), basis: basis, why: why)
        case .goalAdd(let text, let parent): out = .goalAdd(text: fix(text), parent: parent)
        case .goalChange(let id, let text): out = .goalChange(id: id, text: fix(text))
        case .goalBlocked(let id, let missing): out = .goalBlocked(id: id, missing: fix(missing))
        case .concernNote(let text, let about, let due): out = .concernNote(text: fix(text), about: about, due: due)
        case .idealRevise(let text, let old, let new, let why, let basedOn):
            out = .idealRevise(text: text.map(fix), old: old, new: new.map(fix), why: why, basedOn: basedOn)
        default: out = self
        }
        return (out, changes)
    }
}
