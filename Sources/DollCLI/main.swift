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
// Run it only while her process is not running. Her running process does the same in its daily
// system maintenance (2026-11-16, `Maintenance.swift`); this command stays for a data dir whose process is stopped.

import DollCore
import Foundation

func out(_ text: String) {
    FileHandle.standardOutput.write(Data((text + "\n").utf8))
}

/// Ends the process on an error; after the log file is attached, its end line says how (`RunLog.swift`).
func fail(_ msg: String, _ how: RunEnding.How = .bootFailed) -> Never {
    FileHandle.standardError.write(Data("dollcore: \(msg)\n".utf8))
    Log.end(how, msg)
    exit(1)
}

/// What the log's start line names as the build: the repo's commit at the current directory (with
/// `-dirty` when tracked files differ) and the executable's modification time. The commit is the
/// checkout's now, which is the build's only if it was built from it; the time tells a rebuild.
func buildText(root: URL) -> String {
    func git(_ args: [String]) -> String? {
        let p = Process(), out = Pipe()
        p.executableURL = URL(filePath: "/usr/bin/git")
        p.arguments = ["-C", root.path] + args
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        guard (try? p.run()) != nil else { return nil }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return p.terminationStatus == 0 ? String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines) : nil
    }
    let commit = git(["rev-parse", "--short", "HEAD"]).map { $0 + ((git(["status", "--porcelain", "--untracked-files=no"]) ?? "").isEmpty ? "" : "-dirty") }
        ?? "no git commit at \(root.path)"
    let exe = URL(filePath: CommandLine.arguments[0]).resolvingSymlinksInPath()
    let built = (try? FileManager.default.attributesOfItem(atPath: exe.path)[.modificationDate] as? Date).map { LogFile.timeText($0.timeIntervalSince1970) }
    return "\(commit), \(exe.path) built \(built ?? "unknown")"
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
        let root = URL(filePath: FileManager.default.currentDirectoryPath)
        // Her history (2026-11-19): opened first (what changed with no commit is recorded as found),
        // then the consolidation's writes are one change of the maintenance's.
        let memDir = cfg.memory.dir.hasPrefix("/") ? URL(filePath: cfg.memory.dir) : root.appending(path: cfg.memory.dir)
        let history = try History(config: cfg.history, data: cfg.dataDirectory(root: root), memory: cfg.memory, memoryDir: memDir)
        _ = try history.open(ts: Date().timeIntervalSince1970)
        let result = try await Consolidation.run(config: cfg, root: root,
                                                 model: Consolidation.model(cfg), embedder: try await MLXEmbedder.load(cfg.memory),
                                                 now: Date().timeIntervalSince1970)
        try history.record(HistoryChange(who: .maintenance, task: "consolidate", what: "整理記憶、寫摘要（手動）",
                                         why: "dollcore consolidate，她的程序沒開的時候手動跑的", ts: Date().timeIntervalSince1970))
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
/// Her computer's driver daemon (2026-11-16): started here when it was not running, stopped at exit.
nonisolated(unsafe) var daemon: CuaDaemon?
do {
    let cfg = try Config.load(URL(filePath: argv[2]))
    // The persistent log (2026-11-18): the last run is read before this one writes its start line; a
    // log that cannot be opened stops the boot (no run without its log).
    let logDir = cfg.dataDirectory(root: root).appending(path: "logs")
    let previous = try RunLog.lastRun(dir: logDir)
    let build = buildText(root: root)
    Log.attach(try LogFile(dir: logDir, maxBytes: Int(cfg.log.maxMB * 1_048_576)), pid: ProcessInfo.processInfo.processIdentifier, build: build)
    out("log: \(logDir.appending(path: LogFile.current).path)")
    // Her computer (this Mac through cua-driver): missing driver, permission or daemon stops the boot;
    // `[computer] enabled = false` skips it (no driver looked for, no daemon started).
    let (dm, computerLine) = try await CuaDaemon.boot(cfg.computer)
    daemon = dm
    out(computerLine)
    // One MLX runtime for the embedding model and her voice: one cache limit, set before either loads.
    MLXProcess.configure(cacheLimitMB: cfg.core.mlxCacheLimitMB)
    // Boot: the embedding model loads first (the today index embeds at start); no fallback.
    let embedder = try await MLXEmbedder.load(cfg.memory)
    let a = LocalAPI(config: cfg.api)
    api = a
    let d = try Doll(
        config: cfg, root: root, model: HTTPMainModel(cfg.llm), embedder: embedder, maintenanceModel: Consolidation.model(cfg),
        deliver: { text, to, image in
            let failed = a.deliver(text, to: to, image: image)
            out("[→ \(to.label)] \(text)" + (failed.map { "  （沒送到：\($0)）" } ?? ""))
            return failed
        })
    doll = d
    try await doll.boot()
    // Her voice (M6): TTS loaded, reference encoded and the streaming decoder seeded, then the
    // speech recognizer started; any failure stops the boot.
    if cfg.voice.enabled {
        let t0 = Date()
        let synth = try await QwenSynthesizer.load(cfg.voice, root: root)
        let tts = Date().timeIntervalSince(t0)
        let recognizer = try await AppleSpeechRecognizer.start(locale: cfg.voice.asrLocale, finalTimeoutMs: cfg.voice.asrFinalTimeoutMs)
        let link = VoiceLink(config: cfg.voice, recognizer: recognizer, synthesizer: synth,
                             manifest: try VoiceManifest(dir: cfg.voiceDirectory(root: root), writeWAV: writeVoiceWAV))
        await link.setEvents(VoiceEvents(ownerSaid: { await d.ownerSaid($0, voice: true, channel: .discordVoice) },
                                         notice: { await d.voiceNotice($0) }))
        await d.setVoice(link)
        a.setVoice(link)
        let mem = MLXProcess.footprint()
        out(String(format: "voice: ready in %.1f s (TTS load + reference + warm-up %.1f s, speech recognizer %.1f s); footprint %.2f GB, peak %.2f GB",
                   Date().timeIntervalSince(t0), tts, Date().timeIntervalSince(t0) - tts,
                   Double(mem.current) / 1_073_741_824, Double(mem.peak) / 1_073_741_824))
    } else {
        out("voice: disabled ([voice] enabled = false)")
    }
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
    out("docs: \(cfg.core.docs) " + (cfg.computer.enabled ? "(she reads them in place)" : "(not pointed to: she has no read_file with [computer] enabled = false)"))
    try await doll.announceBoot(previous: previous, build: build)
} catch {
    await daemon?.stop()
    fail("\(error)")
}

out("\(doll.name) is up. Ctrl-C stops her.")
let runner = Task {
    do {
        try await doll.run()
    } catch is CancellationError {
    } catch {
        fail("\(error)", .fatal)
    }
}

var stopSignal: Int32 = 0
for await sig in signals {
    out("signal \(sig): stopping")
    stopSignal = sig
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
// Her cursor goes with her: the session ends before the daemon stops (a daemon left running keeps no cursor of hers).
await doll.endComputer()
await daemon?.stop()
Log.end(.clean, "signal \(stopSignal) (\(stopSignal == SIGINT ? "SIGINT, Ctrl-C" : "SIGTERM"))")
_ = signalSources
