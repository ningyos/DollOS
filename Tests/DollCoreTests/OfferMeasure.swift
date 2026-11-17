import Foundation
import Testing
@testable import DollCore

/// Measures the cosine of owner lines to saved tools the way `offerTools` does (query: the line, plus
/// the front app's name when one is given; document: three variants), with the real Qwen3 embedder.
/// Writes TSV to OFFER_OUT. Runs only with OFFER_MEASURE=1 (2026-11-18, m7-mac piece 4).
@Test func offerMeasure() async throws {
    guard ProcessInfo.processInfo.environment["OFFER_MEASURE"] == "1" else { return }
    let cfg = try Config.load(URL(filePath: ProcessInfo.processInfo.environment["REPLAY_CONFIG"]!))
    let embedder = try await MLXEmbedder.load(cfg.memory)
    // name, when, the owner's line it was saved from, step lines. 乘法 and 新備忘錄 are yesterday's real files (k-1, j-1).
    let tools: [(name: String, when: String, task: String, steps: String)] = [
        ("乘法", "主人要用計算機算兩個整數相乘的時候，一次算完", "幫我用計算機算 37×46，跟我說答案",
         "open Calculator、按 {{a}} 的每一個字（AXButton）、click AXButton \"乘\"、按 {{b}} 的每一個字（AXButton）、click AXButton \"等於\""),
        ("新備忘錄", "主人要在備忘錄新增一則備忘錄的時候，一次就建好並填好內容", "在備忘錄新增一則備忘錄，內容寫「DollOS 工具測試 1-1」",
         "open Notes、click AXButton \"新增備忘錄\"、type「{{content}}」進 AXTextArea"),
        ("加法", "主人要用計算機把兩個數加起來的時候", "用計算機算 15+27，跟我說答案",
         "open Calculator、按 {{a}} 的每一個字（AXButton）、click AXButton \"加\"、按 {{b}} 的每一個字（AXButton）、click AXButton \"等於\""),
        ("看圖", "主人要我用預覽程式打開一張圖片的時候", "用預覽程式打開 ~/DollOS-test/pic.png 給我看",
         "open Preview {{path}}"),
        ("新文件", "主人要我用 TextEdit 開一份新文件寫一段字的時候", "用 TextEdit 開一份新文件，寫一行「娃娃的測試」",
         "open TextEdit、menu 檔案 › 新增、type「{{text}}」進 AXTextArea"),
        ("深色模式", "主人要我把電腦外觀換成深色或淺色的時候", "幫我把外觀改成深色模式",
         "open System Settings、click AXButton \"外觀\"、click AXButton \"{{mode}}\""),
    ]
    // (line, the tool that should be offered or "-" for none)
    let lines: [(String, String)] = [
        ("幫我用計算機算 58×13，跟我說答案", "乘法"), ("幫我用計算機算 204×9，跟我說答案", "乘法"),
        ("73 乘 28 是多少？用計算機算一下", "乘法"), ("用計算機幫我算一下 12×34", "乘法"), ("算一下 999×7", "乘法"),
        ("用你存的「乘法」工具幫我算 2.5×4，跟我說答案", "乘法"),
        ("用計算機算 15+27", "加法"), ("幫我把 128 加 356 算出來", "加法"),
        ("在備忘錄新增一則備忘錄，內容寫「買牛奶」", "新備忘錄"), ("幫我記一下：明天十點牙醫，寫在備忘錄", "新備忘錄"),
        ("開一則新的備忘錄寫購物清單", "新備忘錄"),
        ("用預覽程式打開 ~/DollOS-test/b.png", "看圖"), ("幫我開那張截圖看一下", "看圖"),
        ("在 TextEdit 開新文件寫一行字", "新文件"), ("幫我用 TextEdit 寫一段話", "新文件"),
        ("幫我切成深色模式", "深色模式"), ("畫面太暗了，換回淺色", "深色模式"),
        ("把 ~/DollOS-test/m1 的三個檔接起來", "-"), ("幫我在備忘錄找昨天那則", "-"), ("今天天氣怎樣", "-"), ("晚安", "-"),
        ("幫我看一下行事曆明天有什麼", "-"), ("幫我用計算機算 120÷8", "-"), ("早～今天好累", "-"),
        ("幫我刪掉備忘錄裡那則舊的", "-"), ("計算機現在顯示多少？", "-"), ("把 a.txt 的行數寫進 a.count", "-"),
        ("幫我把螢幕亮度調高一點", "-"), ("你剛剛存的工具叫什麼？", "-"), ("把這張圖片傳給我", "-"),
    ]
    let variants: [(String, ((name: String, when: String, task: String, steps: String)) -> String)] = [
        ("current", { "\($0.name)：\($0.when)\n\($0.task)" }),
        ("when", { $0.when }),
        ("name+steps", { "\($0.name)：\($0.steps)" }),
    ]
    var out = "variant\tapp\tline\texpected\t" + tools.map(\.name).joined(separator: "\t") + "\n"
    for (vname, doc) in variants {
        let docs = try embedder.embed(tools.map(doc), as: .document)
        for app in ["", "Calculator"] {
            let queries = try lines.map { try embedder.embed(app.isEmpty ? $0.0 : $0.0 + " " + app, as: .query) }
            for (q, (line, want)) in zip(queries, lines) {
                let cos = docs.map { d in zip(q, d).reduce(Float(0)) { $0 + $1.0 * $1.1 } }
                out += "\(vname)\t\(app.isEmpty ? "-" : app)\t\(line)\t\(want)\t" + cos.map { String(format: "%.3f", $0) }.joined(separator: "\t") + "\n"
            }
        }
    }
    try out.write(to: URL(filePath: ProcessInfo.processInfo.environment["OFFER_OUT"]!), atomically: true, encoding: .utf8)
}
