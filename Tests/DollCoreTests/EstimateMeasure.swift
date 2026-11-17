import Foundation
import Testing
@testable import DollCore

/// Measures how alike finished lists are, with the real Qwen3 embedder, for the estimate check
/// (2026-11-18, m7-mac estcheck piece 1). Reads EST_RECORDS (records.jsonl from
/// experiments/m7-mac/2026-11-18/estcheck/collect.py) and writes, per text variant, the full cosine
/// matrix (every text a document) as TSV to EST_OUT. Runs only with EST_MEASURE=1.
@Test func estimateMeasure() async throws {
    let env = ProcessInfo.processInfo.environment
    guard env["EST_MEASURE"] == "1" else { return }
    struct Rec: Decodable { var owner: String; var items: [String] }
    let recs = try String(contentsOfFile: env["EST_RECORDS"]!, encoding: .utf8).split(separator: "\n")
        .map { try JSONDecoder().decode(Rec.self, from: Data($0.utf8)) }
    let embedder = try await MLXEmbedder.load(try Config.load(URL(filePath: env["REPLAY_CONFIG"]!)).memory)
    // Digits (numbers, and the numbers in paths) masked: a list's kind is not its numbers.
    func masked(_ s: String) -> String { s.replacing(/[0-9０-９]+([.,][0-9]+)*/, with: "#") }
    let variants: [(String, (Rec) -> String)] = [
        ("items", { $0.items.joined(separator: "\n") }),
        ("owner", { $0.owner }),
        ("both", { $0.owner + "\n" + $0.items.joined(separator: "\n") }),
        ("items#", { masked($0.items.joined(separator: "\n")) }),
        ("owner#", { masked($0.owner) }),
        ("both#", { masked($0.owner + "\n" + $0.items.joined(separator: "\n")) }),
    ]
    var out = "variant\ti\tj\tcos\n"
    for (name, text) in variants {
        let v = try embedder.embed(recs.map(text), as: .document)
        for i in v.indices {
            for j in v.indices where j > i {
                out += "\(name)\t\(i)\t\(j)\t" + String(format: "%.4f", zip(v[i], v[j]).reduce(Float(0)) { $0 + $1.0 * $1.1 }) + "\n"
            }
        }
    }
    try out.write(to: URL(filePath: env["EST_OUT"]!), atomically: true, encoding: .utf8)
}
