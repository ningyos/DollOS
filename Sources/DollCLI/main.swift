// Terminal front end.
//
//     you> text                 the owner talks
//     you> /dm <who> <text>     someone else DMs Doll
//     you> /approve <job>       run a job Doll asked for
//     you> /deny <job> [why]    refuse it
//     you> /pending             list jobs waiting for approval
//     you> /lines               show work lines and jobs
//     you> /quit

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
        config: cfg, root: URL(filePath: FileManager.default.currentDirectoryPath),
        main: HTTPMainModel(cfg.llm), decision: FoundationDecisionModel(),
        say: { audience, text in
            let tag = audience == "private" ? "" : " → \(audience.dropFirst(3))"
            out("\n[Doll\(tag)] \(text)\n")
        },
        ask: { jobID, _, args, lineID in
            let line = lineID.map { " (line \($0))" } ?? ""
            out("\n[approval] \(jobID)\(line) Shell: \(args["command"]?.string ?? "")\n"
                + "           /approve \(jobID)   or   /deny \(jobID) [why]\n")
        })
    try await doll.boot()
} catch {
    fail("\(error)")
}

out("\(doll.name) is up. /dm <who> <text>, /approve <job>, /deny <job>, /pending, /lines, /quit")
let runner = Task { try await doll.run() }

do {
    for try await raw in FileHandle.standardInput.bytes.lines {
        let line = raw.trimmingCharacters(in: .whitespaces)
        if line.isEmpty { continue }
        if line == "/quit" { break }
        if line == "/lines" {
            out(try await doll.linesBlock())
        } else if line == "/pending" {
            let rows = try await doll.pending()
            out(rows.isEmpty ? "(none)" : rows.map { "\($0.id): \($0.command)" }.joined(separator: "\n"))
        } else if line.hasPrefix("/approve ") {
            out(try await doll.approve(String(line.split(separator: " ")[1])))
        } else if line.hasPrefix("/deny ") {
            let parts = line.split(separator: " ", maxSplits: 2).map(String.init)
            out(try await doll.deny(parts[1], reason: parts.count > 2 ? parts[2] : ""))
        } else if line.hasPrefix("/dm ") {
            let parts = line.split(separator: " ", maxSplits: 2).map(String.init)
            guard parts.count == 3 else { out("usage: /dm <who> <text>"); continue }
            await doll.dm(parts[1], parts[2])
        } else {
            await doll.ownerSaid(line)
        }
    }
} catch {
    fail("\(error)")
}

// /quit or end of input: stop the consumer, interrupting a model call in flight.
await doll.stop()
runner.cancel()
do {
    try await runner.value
} catch is CancellationError {
} catch {
    fail("\(error)")
}
