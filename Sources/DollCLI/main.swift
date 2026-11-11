// Terminal front end: runs the core and reads commands on stdin. Only the owner exists in M1.
//
//     say <text>          owner text
//     voice <text>        owner voice: interrupts a model call in flight, then higher priority (the real voice path is M6)
//     status              print the 【狀況】 footer
//     quit
//
// `dollcore --config <file.toml> consolidate <data_dir> [--memory <dir>]` (2026-11-11) consolidates
// that data dir's newest timeline segment into her memory (the config's `[memory] dir`, or `--memory`;
// relative paths from the current directory), starts a new segment, prints what it applied and exits.
// Run it only while her process is not running.

import DollCore
import Foundation

func out(_ text: String) {
    FileHandle.standardOutput.write(Data((text + "\n").utf8))
}

func fail(_ msg: String) -> Never {
    FileHandle.standardError.write(Data("dollcore: \(msg)\n".utf8))
    exit(1)
}

let argv = CommandLine.arguments
let usage = "usage: dollcore --config <file.toml> [consolidate <data_dir> [--memory <dir>]]"
guard argv.count >= 3, argv[1] == "--config" else { fail(usage) }

if argv.count > 3 {
    guard argv[3] == "consolidate", argv.count == 5 || (argv.count == 7 && argv[5] == "--memory") else { fail(usage) }
    do {
        var cfg = try Config.load(URL(filePath: argv[2]))
        cfg.core.dataDir = argv[4]
        if argv.count == 7 { cfg.memory.dir = argv[6] }
        let result = try await Consolidation.run(config: cfg, root: URL(filePath: FileManager.default.currentDirectoryPath),
                                                 model: Consolidation.model(cfg), embedder: try await MLXEmbedder.load(cfg.memory))
        out(result.report)
    } catch {
        fail("consolidate: \(error)")
    }
    exit(0)
}

let doll: Doll
let dashboard: Dashboard?
do {
    let cfg = try Config.load(URL(filePath: argv[2]))
    // Boot: the embedding model loads first (the today index embeds at start); no fallback.
    let embedder = try await MLXEmbedder.load(cfg.memory)
    doll = try Doll(
        config: cfg, root: URL(filePath: FileManager.default.currentDirectoryPath), model: HTTPMainModel(cfg.llm),
        selector: FoundationSelectionModel(), embedder: embedder, say: { text in out("\n[Doll] \(text)\n") })
    try await doll.boot()
    if cfg.dashboard.enabled {
        let d = Dashboard(doll: doll, config: cfg.dashboard)
        let port = try await d.start()
        out("dashboard: http://\(cfg.dashboard.bind):\(port)/")
        dashboard = d
    } else {
        dashboard = nil
    }
} catch {
    fail("\(error)")
}

out("\(doll.name) is up. say <text>, voice <text>, status, quit")
let runner = Task { try await doll.run() }

do {
    for try await raw in FileHandle.standardInput.bytes.lines {
        let line = raw.trimmingCharacters(in: .whitespaces)
        if line.isEmpty { continue }
        if line == "quit" { break }
        if line == "status" {
            out(try await doll.status())
        } else if line.hasPrefix("say ") {
            await doll.ownerSaid(String(line.dropFirst(4)))
        } else if line.hasPrefix("voice ") {
            await doll.ownerSaid(String(line.dropFirst(6)), voice: true)
        } else {
            out("unknown command; say <text>, voice <text>, status, quit")
        }
    }
} catch {
    fail("\(error)")
}

// quit or end of input: stop the consumer, interrupting a model call in flight.
dashboard?.stop()
await doll.stop()
runner.cancel()
do {
    try await runner.value
} catch is CancellationError {
} catch {
    fail("\(error)")
}
