// dollcore (2026-11-12): her process. `dollcore --config <file.toml>` boots her and keeps running
// until SIGINT (Ctrl-C) or SIGTERM; no stdin. The owner starts and stops it (no launchd).
//
// Front ends: the local API on 127.0.0.1 (`[api] port`): `dollctl chat` (the developer) and the
// Discord bridge (the owner), which this process starts and watches when `[bridge] enabled`; the
// dashboard (`[dashboard]`) as before.
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

// SIGINT / SIGTERM end the process cleanly; set up before boot so Ctrl-C during boot is handled too.
let (signals, signalled) = AsyncStream.makeStream(of: Int32.self)
let signalSources = [SIGINT, SIGTERM].map { sig in
    signal(sig, SIG_IGN)
    let s = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    s.setEventHandler { signalled.yield(sig) }
    s.resume()
    return s
}

let root = URL(filePath: FileManager.default.currentDirectoryPath)
let doll: Doll
let api: LocalAPI
let bridge: Bridge?
let dashboard: Dashboard?
do {
    let cfg = try Config.load(URL(filePath: argv[2]))
    // Boot: the embedding model loads first (the today index embeds at start); no fallback.
    let embedder = try await MLXEmbedder.load(cfg.memory)
    let a = LocalAPI(config: cfg.api)
    api = a
    doll = try Doll(
        config: cfg, root: root, model: HTTPMainModel(cfg.llm), selector: FoundationSelectionModel(), embedder: embedder,
        say: { text, to in
            let failed = a.deliver(text, to: to)
            out("[say → \(to.label)] \(text)" + (failed.map { "  （沒送到：\($0)）" } ?? ""))
            return failed
        })
    try await doll.boot()
    let port = try await api.start(doll: doll)
    out("api: http://127.0.0.1:\(port)/ (dollctl, bridge)")
    if cfg.dashboard.enabled {
        let d = Dashboard(doll: doll, config: cfg.dashboard)
        let port = try await d.start()
        out("dashboard: http://\(cfg.dashboard.bind):\(port)/")
        dashboard = d
    } else {
        dashboard = nil
    }
    if cfg.bridge.enabled {
        let b = Bridge(config: cfg.bridge, api: api, root: root)
        bridge = b
        try await b.start()
        out("bridge: \(api.bridge)")
    } else {
        bridge = nil
        out("bridge: disabled ([bridge] enabled = false)")
    }
} catch {
    fail("\(error)")
}

out("\(doll.name) is up. Ctrl-C stops her.")
let runner = Task {
    do {
        try await doll.run()
    } catch is CancellationError {
    } catch {
        fail("\(error)")
    }
}

for await sig in signals {
    out("signal \(sig): stopping")
    break
}
// Stop the front ends, then the consumer, interrupting a model call in flight.
bridge?.stop()
api.stop()
dashboard?.stop()
await doll.stop()
await bridge?.waitUntilExit(seconds: 5)
runner.cancel()
await runner.value
_ = signalSources
