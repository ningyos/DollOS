import Foundation

/// M2: her computer (spec 3.1, 2026-11-08). She sees it through screenshots plus a short outline of
/// the front window, and acts on it with pointer and keyboard; the program drives it with
/// `vmpal ui <vm> …` and `vmpal exec`. Tests swap in a fake driver.

/// A rectangle in screenshot pixels, corners inclusive of x0,y0.
public struct Region: Sendable, Equatable, CustomStringConvertible {
    public var x0, y0, x1, y1: Int
    public init(_ x0: Int, _ y0: Int, _ x1: Int, _ y1: Int) { (self.x0, self.y0, self.x1, self.y1) = (x0, y0, x1, y1) }
    public var description: String { "\(x0),\(y0),\(x1),\(y1)" }
}

public enum ComputerAction: Sendable, Equatable {
    case click(x: Int, y: Int, button: String, count: Int)
    case clickText(String, button: String, count: Int)
    case type(String)
    case key(String)
    case scroll(direction: String, amount: Int)
    case drag(x0: Int, y0: Int, x1: Int, y1: Int)
}

public struct ComputerError: Error, CustomStringConvertible {
    public let description: String
    public init(_ description: String) { self.description = description }
}

public protocol ComputerDriver: Sendable {
    /// Does one action and waits until the screen settles. Returns the VM's own text about it;
    /// a failed call (non-zero exit, an error answer) throws with the VM's error text.
    func act(_ action: ComputerAction) async throws -> String
    /// One screenshot at the configured width and format. A region crops it; the bytes only need to
    /// be equal when that part of the screen did not change.
    func screenshot(region: Region?) async throws -> Data
    /// The front window's outline as she reads it (`WindowSummary.render`); throws with the reason
    /// when there is none (no front window, no accessibility access, agent-desktop missing).
    func windowSummary() async throws -> String
}

/// Runs the VMPal CLI as `<vmpal> --cli <args…>`: stdout on exit 0, else throws `ComputerError`
/// with the VM's error text.
public typealias VMPalRun = @Sendable ([String]) async throws -> String

/// `vmpal ui` / `vmpal exec` / `vmpal cp`, one process per call (each `ui` run is its own connection;
/// nothing carries over).
public struct VMPalDriver: ComputerDriver {
    let cfg: ComputerConfig
    let vmpal: VMPalRun
    /// Every `vmpal ui` run's coordinates are screenshot pixels in a 1280-wide space (`vmpal ui help`).
    static let space = 1280

    /// `vmpal` replaces the process runner (tests).
    public init(_ cfg: ComputerConfig, vmpal: VMPalRun? = nil) {
        self.cfg = cfg
        let path = cfg.vmpal
        self.vmpal = vmpal ?? { try await Self.process(path, $0) }
    }

    /// Her coordinates are in the configured screenshot width; VMPal's in its 1280-wide space.
    func scale(_ v: Int) -> Int {
        cfg.screenshotWidth == Self.space ? v : Int((Double(v) * Double(Self.space) / Double(cfg.screenshotWidth)).rounded())
    }

    func ui(_ args: [String]) async throws -> String { try await vmpal(["ui", cfg.vmID] + args) }

    /// A `/bin/sh -c` script in the VM; `args` arrive as $1, $2, … so the script text stays fixed.
    func exec(_ script: String, _ args: [String] = []) async throws -> String {
        try await vmpal(["exec", cfg.vmID, "--", "/bin/sh", "-c", script, "sh"] + args)
    }

    public func act(_ action: ComputerAction) async throws -> String {
        func clickFlags(_ button: String, _ count: Int) -> [String] {
            (button == "left" ? [] : ["--button", button]) + (count == 2 ? ["--double"] : [])
        }
        var tmp: URL?
        defer { tmp.map { try? FileManager.default.removeItem(at: $0) } }
        let args: [String]
        switch action {
        case .click(let x, let y, let button, let count):
            args = ["click", "\(scale(x))", "\(scale(y))"] + clickFlags(button, count)
        case .clickText(let text, let button, let count):
            args = ["click", "--text", text] + clickFlags(button, count)
        case .type(let text) where !text.allSatisfy(\.isASCII):
            return try await paste(text)
        case .type(let text):
            // A file, so text that starts with "-" is not read as an option. VMPal drops a file's last
            // newline, so one is added: her text goes exactly as written.
            let url = Self.tempFile("type")
            try Data((text + "\n").utf8).write(to: url)
            tmp = url
            args = ["type", "--file", url.path]
        case .key(let keys):
            args = ["key", keys]
        case .scroll(let direction, let amount):
            args = ["scroll", direction, "\(amount)"]
        case .drag(let x0, let y0, let x1, let y1):
            args = ["drag"] + [x0, y0, x1, y1].map { "\(scale($0))" }
        }
        // VMPal's default settle; no observation text (she gets a screenshot instead).
        return try await ui(args + ["--observe", "none"])
    }

    /// `vmpal ui type` only types what a US keyboard can ("A US keyboard can't type “你”", M2 medium
    /// round C): text with any non-ASCII character goes through the VM's clipboard instead. Her text
    /// only travels as a file (`vmpal cp`), never as a process argument. Each line is pasted with
    /// cmd+v and a newline is a Return key, the same as `type`. The clipboard's previous text is put
    /// back afterwards, also when a step fails; `pbpaste` only reads text, so a non-text clipboard
    /// (an image, files) comes back empty.
    func paste(_ text: String) async throws -> String {
        let id = UUID().uuidString
        let saved = "/tmp/dollos-clip-\(id)", staged = "/tmp/dollos-paste-\(id)"
        // pbcopy/pbpaste read and write the locale's encoding; vmpal exec gives no LANG.
        let utf8 = "export LC_ALL=en_US.UTF-8; "
        _ = try await exec(utf8 + "pbpaste > \"$1\"", [saved])
        var said = ""
        var failure: Error?
        do {
            for (i, line) in text.split(separator: "\n", omittingEmptySubsequences: false).enumerated() {
                if i > 0 { said = try await ui(["key", "Return", "--observe", "none"]) }
                guard !line.isEmpty else { continue }
                let local = Self.tempFile("paste")
                defer { try? FileManager.default.removeItem(at: local) }
                try Data(line.utf8).write(to: local)
                _ = try await vmpal(["cp", local.path, "\(cfg.vmID):\(staged)"])
                _ = try await exec(utf8 + "pbcopy < \"$1\"", [staged])
                said = try await ui(["key", "cmd+v", "--observe", "none"])
            }
        } catch { failure = error }
        do {
            _ = try await exec(utf8 + "pbcopy < \"$1\"; rm -f \"$1\" \"$2\"", [saved, staged])
        } catch {
            throw ComputerError((failure.map { "\($0)\n" } ?? "") + "剪貼簿沒還原：\(error)")
        }
        if let failure { throw failure }
        return "經剪貼簿貼上（cmd+v），剪貼簿已還原。\n\(said)"
    }

    public func screenshot(region: Region?) async throws -> Data {
        let ext = cfg.screenshotFormat == "png" ? "png" : "jpg"
        let url = Self.tempFile("shot", ext: ext)
        defer { try? FileManager.default.removeItem(at: url) }
        var args = ["screenshot", "-o", url.path] + (cfg.screenshotFormat == "png" ? ["--png"] : [])
        if let r = region {
            args += ["--region", [r.x0, r.y0, r.x1, r.y1].map { "\(scale($0))" }.joined(separator: ",")]
        } else {
            args += ["--max-width", "\(cfg.screenshotWidth)"]
        }
        _ = try await ui(args)
        do { return try Data(contentsOf: url) } catch {
            throw ComputerError("vmpal wrote no screenshot: \(error.localizedDescription)")
        }
    }

    /// agent-desktop in the VM (spec 3.1, 2026-11-08: screenshot plus the front window's skeleton).
    /// It answers JSON on stdout, an error answer included (with exit 1).
    public func windowSummary() async throws -> String {
        let out: String
        do {
            out = try await exec("\"$1\" snapshot --skeleton -i --compact", [cfg.agentDesktop])
        } catch let e as ComputerError {
            throw ComputerError(WindowSummary.errorText(e.description))
        }
        return try WindowSummary.render(Data(out.utf8))
    }

    static func tempFile(_ what: String, ext: String? = nil) -> URL {
        FileManager.default.temporaryDirectory.appending(path: "dollos-\(what)-\(UUID().uuidString)" + (ext.map { ".\($0)" } ?? ""))
    }

    /// stdout on exit 0; otherwise the VM's error text (stderr, else stdout) verbatim.
    static func process(_ vmpal: String, _ args: [String]) async throws -> String {
        let p = Process()
        p.executableURL = URL(filePath: vmpal)
        p.arguments = ["--cli"] + args
        let out = Pipe(), err = Pipe()
        p.standardOutput = out
        p.standardError = err
        p.standardInput = FileHandle.nullDevice
        // terminationHandler, not waitUntilExit: waitUntilExit on a dispatch worker was seen to hang
        // after the child had already exited (M2 medium runs, 2026-11-08).
        let exited = DispatchSemaphore(value: 0)
        p.terminationHandler = { _ in exited.signal() }
        do { try p.run() } catch { throw ComputerError("cannot run \(vmpal): \(error.localizedDescription)") }
        let (stdout, stderr) = await withCheckedContinuation { (c: CheckedContinuation<(Data, Data), Never>) in
            DispatchQueue.global().async {
                // Answers are a few kB; stderr cannot fill its pipe while stdout is read.
                let o = out.fileHandleForReading.readDataToEndOfFile()
                let e = err.fileHandleForReading.readDataToEndOfFile()
                exited.wait()
                c.resume(returning: (o, e))
            }
        }
        let text = String(decoding: stdout, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        guard p.terminationStatus == 0 else {
            let e = String(decoding: stderr, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
            throw ComputerError(e.isEmpty ? (text.isEmpty ? "vmpal exit \(p.terminationStatus)" : text) : e)
        }
        return text
    }
}

/// The front window as a few lines of text: `agent-desktop snapshot --skeleton -i --compact` JSON,
/// one line per element (role, "name", (children count) when the skeleton cut its children),
/// two spaces of indent per depth. No refs: she has no tool that takes one, and a ref only lives as
/// long as the snapshot that made it.
enum WindowSummary {
    struct Answer: Decodable {
        struct Failure: Decodable { let code: String?; let message: String? }
        struct Window: Decodable { let title: String? }
        struct Payload: Decodable { let app: String?; let window: Window?; let tree: Node? }
        let ok: Bool
        let data: Payload?
        let error: Failure?
    }

    final class Node: Decodable {
        let role: String?
        let name: String?
        let children_count: Int?
        let children: [Node]?
    }

    static func render(_ json: Data) throws -> String {
        guard let a = try? JSONDecoder().decode(Answer.self, from: json) else {
            throw ComputerError("agent-desktop 的回答不是 JSON：\(String(decoding: json.prefix(300), as: UTF8.self))")
        }
        guard a.ok, let d = a.data, let tree = d.tree else {
            throw ComputerError(a.error.map(describe) ?? "agent-desktop 沒有回視窗")
        }
        // `window.title` was the app's name in every sample (Safari, TextEdit, Terminal, 2026-11-08);
        // the window element's own name is the title on screen.
        let title = tree.name.flatMap { $0.isEmpty ? nil : $0 } ?? d.window?.title ?? ""
        var lines = ["前景 app：\(d.app ?? "?")，視窗標題「\(title)」"]
        func walk(_ n: Node, _ depth: Int) {
            var line = String(repeating: "  ", count: depth) + (n.role ?? "?")
            if let name = n.name, !name.isEmpty { line += " \"\(name)\"" }
            if let c = n.children_count { line += " (\(c))" }
            lines.append(line)
            for c in n.children ?? [] { walk(c, depth + 1) }
        }
        walk(tree, 0)
        return lines.joined(separator: "\n")
    }

    static func describe(_ f: Answer.Failure) -> String {
        [f.code, f.message].compactMap { $0 }.joined(separator: ": ")
    }

    /// A failed run's text: agent-desktop's JSON error answer when it is one, else as given.
    static func errorText(_ text: String) -> String {
        guard let a = try? JSONDecoder().decode(Answer.self, from: Data(text.utf8)), let f = a.error else { return text }
        return describe(f)
    }
}
