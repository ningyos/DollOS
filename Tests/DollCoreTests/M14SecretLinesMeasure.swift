import Foundation
import Testing
@testable import DollCore

/// m14-secret-lines, 2026-11-21: the owner-line secret check (`SecretCheck.ask`, the call the doll
/// makes) against the real model. Runs only with M14_SECRET_LINES=1 (M14_URL, M14_MODEL, M14_REPS,
/// M14_OUT). Each line of `experiments/m14-secret-lines/lines.tsv` goes through the check M14_REPS
/// times (default 2, the set in order each time); per line and run: the verdict and its latency.
@Test func m14SecretLinesMeasure() async throws {
    let env = ProcessInfo.processInfo.environment
    guard env["M14_SECRET_LINES"] == "1" else { return }
    let repo = URL(filePath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    let dir = repo.appending(path: "experiments/m14-secret-lines")
    let llm = LlmConfig(baseURL: env["M14_URL"] ?? "http://192.0.2.10:8080", model: env["M14_MODEL"] ?? "qwen3.8-flash-next-unsloth-ud-iq2_xs")
    let model = HTTPMainModel(llm)
    try await model.check()
    let reps = Int(env["M14_REPS"] ?? "2") ?? 2
    let outURL = URL(filePath: env["M14_OUT"] ?? dir.appending(path: "results.tsv").path)
    struct Line { var id: String; var group: String; var expected: Bool; var text: String }
    let lines = try String(contentsOf: dir.appending(path: "lines.tsv"), encoding: .utf8).split(separator: "\n")
        .filter { !$0.hasPrefix("#") && !$0.isEmpty }
        .map { l -> Line in
            let f = l.split(separator: "\t", maxSplits: 3).map(String.init)
            return Line(id: f[0], group: f[1], expected: f[2] == "是", text: f[3])
        }
    var out = "# m14-secret-lines results: \(llm.model) at \(llm.baseURL), \(Date())\n# id\tgroup\texpected\trun\tverdict\tms\n"
    func say(_ s: String) {
        out += s + "\n"
        FileHandle.standardError.write(Data((s + "\n").utf8))
        try? out.write(to: outURL, atomically: true, encoding: .utf8)
    }
    for run in 1...reps {
        for l in lines {
            let t0 = ContinuousClock.now
            let v = await SecretCheck.ask(l.text, model: model)
            let d = (ContinuousClock.now - t0).components
            let ms = Int(d.seconds * 1000 + d.attoseconds / 1_000_000_000_000_000)
            let verdict = switch v {
            case .locked: "locked"
            case .secret: "是"
            case .ordinary: "否"
            case .failed(let why): "failed(\(why))"
            }
            say("\(l.id)\t\(l.group)\t\(l.expected ? "是" : "否")\t\(run)\t\(verdict)\t\(ms)")
        }
    }
}
