import Foundation

/// M2: her computer (spec 3.1, 2026-11-08). She sees it only through screenshots and acts on it with
/// pointer and keyboard; the program drives it with `vmpal ui <vm> …`. Tests swap in a fake driver.

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
}

/// `vmpal ui`, one process per call (each run is its own connection; nothing carries over).
public struct VMPalDriver: ComputerDriver {
    let cfg: ComputerConfig
    /// Every `vmpal ui` run's coordinates are screenshot pixels in a 1280-wide space (`vmpal ui help`).
    static let space = 1280

    public init(_ cfg: ComputerConfig) { self.cfg = cfg }

    /// Her coordinates are in the configured screenshot width; VMPal's in its 1280-wide space.
    func scale(_ v: Int) -> Int {
        cfg.screenshotWidth == Self.space ? v : Int((Double(v) * Double(Self.space) / Double(cfg.screenshotWidth)).rounded())
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
        case .type(let text):
            // A file, so text that starts with "-" is not read as an option. VMPal drops a file's last
            // newline, so one is added: her text goes exactly as written.
            let url = FileManager.default.temporaryDirectory.appending(path: "dollos-type-\(UUID().uuidString)")
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
        return try await run(args + ["--observe", "none"])
    }

    public func screenshot(region: Region?) async throws -> Data {
        let ext = cfg.screenshotFormat == "png" ? "png" : "jpg"
        let url = FileManager.default.temporaryDirectory.appending(path: "dollos-shot-\(UUID().uuidString).\(ext)")
        defer { try? FileManager.default.removeItem(at: url) }
        var args = ["screenshot", "-o", url.path] + (cfg.screenshotFormat == "png" ? ["--png"] : [])
        if let r = region {
            args += ["--region", [r.x0, r.y0, r.x1, r.y1].map { "\(scale($0))" }.joined(separator: ",")]
        } else {
            args += ["--max-width", "\(cfg.screenshotWidth)"]
        }
        _ = try await run(args)
        do { return try Data(contentsOf: url) } catch {
            throw ComputerError("vmpal wrote no screenshot: \(error.localizedDescription)")
        }
    }

    /// stdout on exit 0; otherwise the VM's error text (stderr, else stdout) verbatim.
    func run(_ args: [String]) async throws -> String {
        let p = Process()
        p.executableURL = URL(filePath: cfg.vmpal)
        p.arguments = ["--cli", "ui", cfg.vmID] + args
        let out = Pipe(), err = Pipe()
        p.standardOutput = out
        p.standardError = err
        p.standardInput = FileHandle.nullDevice
        do { try p.run() } catch { throw ComputerError("cannot run \(cfg.vmpal): \(error.localizedDescription)") }
        let (stdout, stderr) = await withCheckedContinuation { (c: CheckedContinuation<(Data, Data), Never>) in
            DispatchQueue.global().async {
                // Answers are a few lines; stderr cannot fill its pipe while stdout is read.
                let o = out.fileHandleForReading.readDataToEndOfFile()
                let e = err.fileHandleForReading.readDataToEndOfFile()
                p.waitUntilExit()
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
