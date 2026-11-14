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
