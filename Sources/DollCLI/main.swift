// Terminal front end: runs the core and reads commands on stdin. Only the owner exists in M1.
//
//     say <text>          owner text
//     voice <text>        owner voice: interrupts a model call in flight, then higher priority (the real voice path is M6)
//     status              print the 【狀況】 footer
//     quit

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
guard argv.count == 3, argv[1] == "--config" else { fail("usage: dollcore --config <file.toml>") }

let doll: Doll
do {
    let cfg = try Config.load(URL(filePath: argv[2]))
    doll = try Doll(
        config: cfg, root: URL(filePath: FileManager.default.currentDirectoryPath), model: HTTPMainModel(cfg.llm),
        decision: AppleDecisionModel(),
        say: { text in out("\n[Doll] \(text)\n") })
    try await doll.boot()
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
await doll.stop()
runner.cancel()
do {
    try await runner.value
} catch is CancellationError {
} catch {
    fail("\(error)")
}
