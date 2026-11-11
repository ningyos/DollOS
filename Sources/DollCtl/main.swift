// dollctl (2026-11-12): talks to a running dollcore over its local API (127.0.0.1, `[api] port`).
//
//     dollctl [--config <file.toml>] chat     chat as the developer: each line you type is a developer
//                                             line ([HH:MM 開發者] in her timeline); what she says to the
//                                             developer is printed. Ctrl-D or Ctrl-C ends it.
//     dollctl [--config <file.toml>] status   her state, the bridge, and her 【狀況】 footer
//
// Without --config the port is `[api] port`'s default (8791).

import DollCore
import Foundation

func out(_ text: String) {
    FileHandle.standardOutput.write(Data((text + "\n").utf8))
}

func fail(_ msg: String) -> Never {
    FileHandle.standardError.write(Data("dollctl: \(msg)\n".utf8))
    exit(1)
}

let usage = "usage: dollctl [--config <file.toml>] chat | status"
var args = Array(CommandLine.arguments.dropFirst())
var port = ApiConfig().port
if args.first == "--config" {
    guard args.count >= 2 else { fail(usage) }
    do {
        port = try Config.load(URL(filePath: args[1])).api.port
    } catch {
        fail("\(args[1]): \(error)")
    }
    args.removeFirst(2)
}
guard args.count == 1, ["chat", "status"].contains(args[0]) else { fail(usage) }
let base = "http://127.0.0.1:\(port)"

/// The URLSession error when dollcore is not running reads poorly; say what it means.
func unreachable(_ error: Error) -> Never {
    fail("cannot reach dollcore at \(base) (is it running with this [api] port?): \(error.localizedDescription)")
}

func status() async -> ApiStatus {
    do {
        let (data, response) = try await URLSession.shared.data(from: URL(string: base + "/api/status")!)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else {
            fail("status: \(String(decoding: data, as: UTF8.self))")
        }
        let dec = JSONDecoder()
        dec.keyDecodingStrategy = .convertFromSnakeCase
        return try dec.decode(ApiStatus.self, from: data)
    } catch let e as DecodingError {
        fail("status: unexpected reply: \(e)")
    } catch {
        unreachable(error)
    }
}

func clock() -> String {
    let c = Calendar.current.dateComponents([.hour, .minute], from: Date())
    return String(format: "%02d:%02d", c.hour!, c.minute!)
}

if args[0] == "status" {
    out(await status().text)
    exit(0)
}

// chat -------------------------------------------------------------------------
let name = await status().name
let (lines, response): (URLSession.AsyncBytes, URLResponse)
do {
    var req = URLRequest(url: URL(string: base + "/api/listen?to=developer")!)
    req.timeoutInterval = 365 * 24 * 3600   // she may be quiet for a long time; the stream ends only when either side closes
    (lines, response) = try await URLSession.shared.bytes(for: req)
} catch {
    unreachable(error)
}
guard (response as? HTTPURLResponse)?.statusCode == 200 else { fail("listen: HTTP \((response as? HTTPURLResponse)?.statusCode ?? 0)") }

struct StreamLine: Decodable {
    var type: String
    var text: String?
    var on: Bool?
}

// What she says to the developer, as it comes.
let listener = Task {
    do {
        for try await raw in lines.lines {
            guard let line = try? JSONDecoder().decode(StreamLine.self, from: Data(raw.utf8)) else { continue }
            switch line.type {
            case "hello": out("connected to \(name) at \(base). You are the developer; type a line and press Enter. Ctrl-D ends.")
            case "say": out("\n[\(clock()) \(name)] \(line.text ?? "")\n")
            case "typing" where line.on == true: out("（\(name) 在想…）")
            default: break
            }
        }
        fail("dollcore closed the connection")
    } catch {
        fail("dollcore connection lost: \(error.localizedDescription)")
    }
}

do {
    for try await raw in FileHandle.standardInput.bytes.lines {
        let text = raw.trimmingCharacters(in: .whitespaces)
        if text.isEmpty { continue }
        var req = URLRequest(url: URL(string: base + "/api/developer")!)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: ["text": text])
        let (data, response) = try await URLSession.shared.data(for: req)
        if (response as? HTTPURLResponse)?.statusCode != 200 { out("（沒送出：\(String(decoding: data, as: UTF8.self))）") }
    }
} catch {
    unreachable(error)
}
listener.cancel()
