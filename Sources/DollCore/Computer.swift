import Foundation

/// M2: her computer (spec 3.1, 2026-11-08). She sees it through a full-screen screenshot, a crop of
/// the front window and a short outline of that window (2026-11-09), and acts on it with pointer and
/// keyboard; the program drives it with `vmpal ui <vm> …` and `vmpal exec`. Tests swap in a fake driver.

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

/// The front window: its outline as she reads it (`WindowSummary.render`) and where it is.
public struct FrontWindow: Sendable, Equatable {
    public enum Place: Sendable, Equatable {
        /// In her full screenshot's pixels, clipped to the screen.
        case at(Region)
        /// Why there is no place to crop (said to her instead of a crop).
        case unknown(String)
    }
    public var summary: String
    public var place: Place
    public init(summary: String, place: Place) { (self.summary, self.place) = (summary, place) }
}

public protocol ComputerDriver: Sendable {
    /// Does one action and waits until the screen settles. Returns the VM's own text about it;
    /// a failed call (non-zero exit, an error answer) throws with the VM's error text.
    func act(_ action: ComputerAction) async throws -> String
    /// One full-screen screenshot at the configured width and format, or a region of it (watch). A
    /// region crops it; the bytes only need to be equal when that part of the screen did not change.
    func screenshot(region: Region?) async throws -> Data
    /// The front window's crop: `region` in full-screenshot pixels, at the configured crop width and
    /// format.
    func windowShot(_ region: Region) async throws -> Data
    /// The front window's outline and place; throws with the reason when there is none (no front
    /// window, no accessibility access, agent-desktop missing).
    func frontWindow() async throws -> FrontWindow
    /// With no front window (spec 3.1, 2026-11-09): the menu bar's and the Dock's items, one line
    /// each, `role "name" (x,y)` with the item's center in full-screenshot pixels. A part that could
    /// not be read is a line saying why; throws when none could (agent-desktop missing, no screen).
    func desktopSummary() async throws -> String
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
        if let r = region { return try await shot(cfg.screenshotFormat, ["--region", vmRegion(r)]) }
        return try await shot(cfg.screenshotFormat, ["--max-width", "\(cfg.screenshotWidth)"])
    }

    /// VMPal reads `--region` in its own 1280-wide space whatever `--max-width` is (2026-11-09: region
    /// 0,0,1280,800 with --max-width 1719 was the whole screen); `--max-width` only caps the output.
    public func windowShot(_ region: Region) async throws -> Data {
        try await shot(cfg.windowCropFormat, ["--region", vmRegion(region), "--max-width", "\(cfg.windowCropWidth)"])
    }

    func vmRegion(_ r: Region) -> String { [r.x0, r.y0, r.x1, r.y1].map { "\(scale($0))" }.joined(separator: ",") }

    func shot(_ format: String, _ extra: [String]) async throws -> Data {
        let png = format == "png"
        let url = Self.tempFile("shot", ext: png ? "png" : "jpg")
        defer { try? FileManager.default.removeItem(at: url) }
        _ = try await ui(["screenshot", "-o", url.path] + (png ? ["--png"] : []) + extra)
        do { return try Data(contentsOf: url) } catch {
            throw ComputerError("vmpal wrote no screenshot: \(error.localizedDescription)")
        }
    }

    /// agent-desktop in the VM (spec 3.1, 2026-11-08: the front window's skeleton; 2026-11-09: and
    /// its place, for the crop). It answers JSON on stdout, an error answer included (with exit 1).
    /// The window's bounds are screen points; `list-displays` gives the screen's, to turn them into
    /// her screenshot's pixels.
    public func frontWindow() async throws -> FrontWindow {
        let out: String
        do {
            out = try await exec("\"$1\" snapshot --skeleton -i --compact --include-bounds", [cfg.agentDesktop])
        } catch let e as ComputerError {
            throw ComputerError(WindowSummary.errorText(e.description))
        }
        let (summary, bounds) = try WindowSummary.parse(Data(out.utf8))
        guard let bounds else { return FrontWindow(summary: summary, place: .unknown("agent-desktop 沒給這個視窗的位置")) }
        let screen: WindowSummary.Rect
        do {
            screen = try WindowSummary.display(Data(try await exec("\"$1\" list-displays", [cfg.agentDesktop]).utf8))
        } catch let e as ComputerError {
            return FrontWindow(summary: summary, place: .unknown("不知道螢幕多大：\(WindowSummary.errorText(e.description))"))
        }
        return FrontWindow(summary: summary, place: WindowSummary.place(bounds, on: screen, width: cfg.screenshotWidth))
    }

    /// agent-desktop's menu bar, menu bar extras and Dock surfaces, then the screen's size, in one
    /// `vmpal exec`. Each answer is one JSON line after a `@@<name>` marker; a surface that fails
    /// answers its JSON error and the script goes on. The script's status is `list-displays`'s.
    public func desktopSummary() async throws -> String {
        let out: String
        do {
            out = try await exec("""
                for s in \(DesktopSummary.surfaces.map(\.flag).joined(separator: " ")); do
                echo "@@$s"; "$1" snapshot --surface "$s" -i --compact --include-bounds; done
                echo "@@displays"; "$1" list-displays
                """, [cfg.agentDesktop])
        } catch let e as ComputerError {
            throw ComputerError(WindowSummary.errorText(e.description))
        }
        var parts: [String: String] = [:], key = ""
        for line in out.split(separator: "\n", omittingEmptySubsequences: true) {
            if line.hasPrefix("@@") { key = String(line.dropFirst(2)) } else { parts[key, default: ""] += line }
        }
        let screen = try WindowSummary.display(Data((parts["displays"] ?? "").utf8))
        return DesktopSummary.render(DesktopSummary.surfaces.map { ($0, Data((parts[$0.flag] ?? "").utf8)) },
                                     on: screen, width: cfg.screenshotWidth)
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
/// long as the snapshot that made it. The tree's root is the window; its bounds (screen points,
/// with `--include-bounds`) are where the crop goes.
enum WindowSummary {
    struct Answer: Decodable {
        struct Failure: Decodable { let code: String?; let message: String? }
        struct Window: Decodable { let title: String? }
        struct Payload: Decodable { let app: String?; let window: Window?; let tree: Node? }
        let ok: Bool
        let data: Payload?
        let error: Failure?
    }

    struct Rect: Decodable, Equatable { let x, y, width, height: Double }

    final class Node: Decodable {
        let role: String?
        let name: String?
        let bounds: Rect?
        let children_count: Int?
        let children: [Node]?
    }

    /// The outline she reads, and the window's bounds when agent-desktop gave them.
    static func parse(_ json: Data) throws -> (summary: String, bounds: Rect?) {
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
        return (lines.joined(separator: "\n"), tree.bounds)
    }

    /// `agent-desktop list-displays`: the primary display's bounds in points (the first one when
    /// none says primary).
    static func display(_ json: Data) throws -> Rect {
        struct Display: Decodable { let bounds: Rect; let is_primary: Bool? }
        struct Displays: Decodable { let ok: Bool; let data: [Display]? }
        guard let a = try? JSONDecoder().decode(Displays.self, from: json), a.ok, let all = a.data,
              let d = all.first(where: { $0.is_primary == true }) ?? all.first else {
            throw ComputerError("agent-desktop list-displays 的回答看不懂：\(String(decoding: json.prefix(300), as: UTF8.self))")
        }
        return d.bounds
    }

    /// A window's points as pixels of a `width`-wide screenshot of `screen`, widened to whole pixels
    /// and clipped to the screen.
    static func place(_ w: Rect, on screen: Rect, width: Int) -> FrontWindow.Place {
        // Multiply before dividing: 900 points of a 1440-point screen are exactly 800 of 1280 pixels.
        func px(_ points: Double) -> Double { points * Double(width) / screen.width }
        let height = Int(px(screen.height).rounded())
        let x0 = max(0, Int(px(w.x - screen.x).rounded(.down)))
        let y0 = max(0, Int(px(w.y - screen.y).rounded(.down)))
        let x1 = min(width, Int(px(w.x + w.width - screen.x).rounded(.up)))
        let y1 = min(height, Int(px(w.y + w.height - screen.y).rounded(.up)))
        guard x1 > x0, y1 > y0 else { return .unknown("最前面的視窗不在螢幕上") }
        return .at(Region(x0, y0, x1, y1))
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

/// The menu bar and the Dock as text, for when there is no front window to crop (spec 3.1,
/// 2026-11-09: round 4's six coordinate misses were all on the bare desktop, three below the screen
/// and three on the wrong Dock icon). One line per item: role, "name", its center in her
/// full-screenshot pixels, converted from agent-desktop's points the same way as the crop.
enum DesktopSummary {
    struct Surface: Sendable, Equatable {
        /// agent-desktop's `--surface` value.
        let flag: String
        /// What she reads it as.
        let label: String
    }

    /// Top to bottom. The app menus need a window of the app on macOS 27 (agent-desktop 2.6:
    /// WINDOW_NOT_FOUND with Finder frontmost and no window); that line then says so.
    static let surfaces = [
        Surface(flag: "menubar", label: "選單列"),
        Surface(flag: "menu-bar-extras", label: "選單列右側"),
        Surface(flag: "dock", label: "Dock"),
    ]

    static func render(_ answers: [(Surface, Data)], on screen: WindowSummary.Rect, width: Int) -> String {
        func px(_ points: Double) -> Int { Int((points * Double(width) / screen.width).rounded()) }
        var lines = ["Dock 和選單列（項目中心，全螢幕截圖座標）："]
        for (surface, json) in answers {
            guard let a = try? JSONDecoder().decode(WindowSummary.Answer.self, from: json) else {
                lines.append("\(surface.label)讀不到：agent-desktop 的回答不是 JSON：\(String(decoding: json.prefix(300), as: UTF8.self))")
                continue
            }
            guard a.ok, let tree = a.data?.tree else {
                lines.append("\(surface.label)讀不到：\(a.error.map(WindowSummary.describe) ?? "agent-desktop 沒有回內容")")
                continue
            }
            // Leaves with a name and a size: the clickable items, not the list or group around them.
            var items: [String] = []
            func walk(_ n: WindowSummary.Node) {
                if let kids = n.children, !kids.isEmpty { kids.forEach(walk); return }
                guard let name = n.name, !name.isEmpty, let b = n.bounds, b.width > 0, b.height > 0 else { return }
                items.append("\(n.role ?? "?") \"\(name)\" (\(px(b.x + b.width / 2 - screen.x)),\(px(b.y + b.height / 2 - screen.y)))")
            }
            walk(tree)
            lines += items.isEmpty ? ["\(surface.label)：沒有項目"] : items
        }
        return lines.joined(separator: "\n")
    }
}
