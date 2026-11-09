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
    /// Full-screenshot pixels.
    case click(x: Int, y: Int, button: String, count: Int)
    case clickText(String, button: String, count: Int)
    /// An element of the front window, found fresh at click time (spec 3.1, M2 round 6): its name
    /// exactly (case-insensitive), else containing `name`; `role` narrows it when given. No name:
    /// the role alone (M2 round 7). No front window: the Dock and the menu bar instead.
    case clickElement(role: String?, name: String?, button: String, count: Int)
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

/// The front app as the program keys her shortcut memories (spec 3.1, 2026-11-09): one app at one version.
public struct AppIdentity: Sendable, Equatable {
    public var bundleID: String
    public var name: String
    public var version: String
    public init(bundleID: String, name: String, version: String) { (self.bundleID, self.name, self.version) = (bundleID, name, version) }
}

/// One menu item with a shortcut: its menu path (`File › Save As…`) and its keys (`⇧⌥⌘S`).
public struct MenuShortcut: Sendable, Equatable {
    public var path: String
    public var keys: String
    public init(path: String, keys: String) { (self.path, self.keys) = (path, keys) }
}

/// After every computer action (spec 3.1, 2026-11-09): the line naming the open windows and the
/// focused element, as she reads it, and the front app.
public struct FrontState: Sendable, Equatable {
    /// `開著的視窗：Finder、TextEdit「notes.txt」（前景）；焦點：textfield`; a part that could not be
    /// read says so in its place.
    public var line: String
    /// nil when there is none or it could not be read.
    public var app: AppIdentity?
    public init(line: String, app: AppIdentity?) { (self.line, self.app) = (line, app) }
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
    /// The front app; nil when there is none.
    func frontApp() async throws -> AppIdentity?
    /// Every menu item of the running app `bundleID` that has a shortcut, in menu order.
    func menuShortcuts(_ bundleID: String) async throws -> [MenuShortcut]
    /// The open windows, the focused element and the front app, in one VM command; throws only when
    /// that command could not run at all.
    func frontState() async throws -> FrontState
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
        case .clickElement(let role, let name, let button, let count):
            return try await clickElement(role: role, name: name, button: button, count: count)
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
        let parts = Self.sections(try await agentDesktop("""
            for s in \(DesktopSummary.surfaces.map(\.flag).joined(separator: " ")); do
            echo "@@$s"; "$1" snapshot --surface "$s" -i --compact --include-bounds; done
            echo "@@displays"; "$1" list-displays
            """, [cfg.agentDesktop]))
        let screen = try WindowSummary.display(Data((parts["displays"] ?? "").utf8))
        return DesktopSummary.render(DesktopSummary.surfaces.map { ($0, Data((parts[$0.flag] ?? "").utf8)) },
                                     on: screen, width: cfg.screenshotWidth)
    }

    /// `axmenus --front` (`scripts/axmenus.swift`, in the VM): bundle id, name, version, tab-separated.
    public func frontApp() async throws -> AppIdentity? {
        Self.parseFrontApp(try await exec("\"$1\" --front", [cfg.axMenus]))
    }

    /// `axmenus <bundle id>`: one `path<TAB>keys` line per menu item with a shortcut.
    public func menuShortcuts(_ bundleID: String) async throws -> [MenuShortcut] {
        Self.parseShortcuts(try await exec("\"$1\" \"$2\"", [cfg.axMenus, bundleID]))
    }

    /// agent-desktop `list-windows` and `snapshot --surface focused`, then `axmenus --front`, in one
    /// `vmpal exec` (VMPal runs at most two CLI commands per VM at once). Each part after a `@@<name>`
    /// marker; a part that fails answers its error and the script goes on.
    public func frontState() async throws -> FrontState {
        let parts = Self.sections(try await exec("""
            "$1" list-windows; echo; echo @@focused; "$1" snapshot --surface focused -i --compact; echo
            echo @@front; "$2" --front; exit 0
            """, [cfg.agentDesktop, cfg.axMenus]), first: "windows")
        return FrontState(line: OpenWindows.line(windows: Data((parts["windows"] ?? "").utf8),
                                                 focused: Data((parts["focused"] ?? "").utf8)),
                          app: Self.parseFrontApp(parts["front"] ?? ""))
    }

    /// nil when there is no front app or it has no bundle id (nothing to key memories by).
    static func parseFrontApp(_ out: String) -> AppIdentity? {
        let f = out.trimmingCharacters(in: .newlines).components(separatedBy: "\t")
        guard f.count == 3, !f[0].isEmpty else { return nil }
        return AppIdentity(bundleID: f[0], name: f[1].isEmpty ? f[0] : f[1], version: f[2])
    }

    static func parseShortcuts(_ out: String) -> [MenuShortcut] {
        out.split(separator: "\n").compactMap { line in
            let f = line.components(separatedBy: "\t")
            return f.count == 2 && !f[0].isEmpty && !f[1].isEmpty ? MenuShortcut(path: f[0], keys: f[1]) : nil
        }
    }

    /// `click` by element (spec 3.1, M2 round 6). At click time: the focused window from
    /// `list-windows`, then `find --window-id` in it, so the ref is fresh and a second window of the
    /// same app is never searched; with no focused window (round 7), the menu bar's, menu bar
    /// extras' and Dock's items instead. One match is pressed through accessibility first (a sheet's
    /// Save has no physical owning window for `--headed`, 2026-11-09); when agent-desktop says that
    /// press was not delivered (Finder's file icons have no semantic double-click), with real
    /// pointer input (`--headed`). The result says what was clicked, where, which way got through
    /// and agent-desktop's disposition as given. Several matches are never guessed between and no
    /// match is never a click on something else: both fail with candidates and their centers in her
    /// full-screenshot pixels, so she can pick one by coordinates.
    func clickElement(role: String?, name: String?, button: String, count: Int) async throws -> String {
        let verb: String
        switch (button, count) {
        case ("left", 1): verb = "click"
        case ("left", _): verb = "double-click"
        case (_, 1): verb = "right-click"
        default: throw ComputerError("element 不能右鍵雙擊；右鍵 count 給 1")
        }
        guard name != nil || role != nil else { throw ComputerError("element 要有名字或角色") }
        let ad = cfg.agentDesktop
        let info = Self.sections(try await agentDesktop("\"$1\" list-windows; echo @@displays; \"$1\" list-displays", [ad]),
                                 first: "windows")
        let screen = try WindowSummary.display(Data((info["displays"] ?? "").utf8))
        let width = cfg.screenshotWidth
        let scope: ElementClick.Window, found: [ElementClick.Match]
        /// Every item of the Dock and the menu bar when that is where the search went (near names come from here).
        var desktopItems: [ElementClick.Match]?
        if let window = try ElementClick.frontWindow(Data((info["windows"] ?? "").utf8)) {
            scope = window
            let query = name.map { ["--name", $0] } ?? ["--role", role!]
            found = try ElementClick.matches(Data(
                try await agentDesktop("\"$1\" find --window-id \"$2\" \"$3\" \"$4\"", [ad, window.id] + query).utf8),
                unnamed: name == nil)
        } else {
            let parts = Self.sections(try await agentDesktop("""
                for s in \(DesktopSummary.surfaces.map(\.flag).joined(separator: " ")); do
                echo "@@$s"; "$1" snapshot --surface "$s" -i --compact --include-bounds; done; exit 0
                """, [ad]))
            var items: [ElementClick.Match] = [], unread: [String] = []
            for s in DesktopSummary.surfaces {
                do { items += try ElementClick.elements(Data((parts[s.flag] ?? "").utf8), unnamed: true) } catch {
                    unread.append("\(s.label)：\(error)")
                }
            }
            guard !items.isEmpty else {
                throw ComputerError("沒有點：現在沒有最前面的視窗，Dock 和選單列也讀不到。" + unread.joined(separator: "；"))
            }
            scope = ElementClick.desktop
            found = items
            desktopItems = items
        }
        func listed(_ ms: [ElementClick.Match]) async throws -> String {
            let at = try await bounds(ms.filter { $0.bounds == nil }.map(\.ref))
            return ms.map { m in ElementClick.line(m, (m.bounds ?? at[m.ref]).map { ElementClick.center($0, on: screen, width: width) }) }
                .joined(separator: "\n")
        }
        let target = (role ?? "") + (name.map { "「\($0)」" } ?? "")
        var pick = ElementClick.pick(found, role: role, name: name)
        if case .several(let ms) = pick {
            // An element and its own cell or label (Finder's sidebar: treeitem and cell "Documents",
            // 2026-11-09) are one spot on the screen: the same name at the same center is one target.
            let placed = Array(ms.prefix(ElementClick.maxPlaced))
            let at = try await bounds(placed.filter { $0.bounds == nil }.map(\.ref))
            pick = ElementClick.collapse(ms.map { m in var m = m; m.bounds = m.bounds ?? at[m.ref]; return m })
        }
        switch pick {
        case .one(let m):
            // One run: the accessibility press, and only when agent-desktop says it was not delivered,
            // the same with real pointer input. printf, not echo: the answer may hold backslashes.
            let out = Self.sections(try await agentDesktop("""
                "$1" get "$2" --property bounds; echo @@semantic; a=$("$1" \(verb) "$2"); printf '%s\\n' "$a"
                case "$a" in *'"delivery":"not_delivered"'*) echo @@headed; "$1" \(verb) --headed "$2";; esac; exit 0
                """, [ad, m.ref]), first: "bounds")
            let point = (ElementClick.bounds(Data((out["bounds"] ?? "").utf8)) ?? m.bounds).map { ElementClick.center($0, on: screen, width: width) }
            let said = try ElementClick.clicked(m, verb: verb, at: point, semantic: Data((out["semantic"] ?? "").utf8),
                                                headed: out["headed"].map { Data($0.utf8) })
            // Like every other action, wait for the screen to settle before the screenshot: a Dock press
            // returns before the app's window exists (round 7: 8/8 screenshots showed no window yet).
            do {
                _ = try await ui(["wait", "--settled", "--observe", "none"])
            } catch {
                return said + "（畫面沒等到穩定：\(error)）"
            }
            return said
        case .several(let ms):
            let shown = Array(ms.prefix(ElementClick.maxListed))
            throw ComputerError("沒有點：\(scope.label)裡有 \(ms.count) 個符合\(target)，不知道是哪一個。"
                                + "要點的那個用它的座標 click x、y（全螢幕截圖座標）：\n" + (try await listed(shown))
                                + (ms.count > shown.count ? "\n（另外 \(ms.count - shown.count) 個沒列）" : ""))
        case .none(let named) where !named.isEmpty:
            let shown = Array(named.prefix(ElementClick.maxListed))
            throw ComputerError("沒有點：\(scope.label)裡沒有\(target)；名字對、角色不同的有（全螢幕截圖座標）：\n"
                                + (try await listed(shown)))
        case .none:
            guard let name else { throw ComputerError("沒有點：\(scope.label)裡沒有\(target)。") }
            // `find` with no name times out on a whole window (TIMEOUT, 2026-11-09); its snapshot is 0.4 s.
            let all: [ElementClick.Match]
            if let desktopItems { all = desktopItems } else {
                all = try ElementClick.elements(Data(try await agentDesktop(
                    "\"$1\" snapshot --window-id \"$2\" -i --compact --include-bounds", [ad, scope.id]).utf8))
            }
            let near = ElementClick.similar(all, to: name)
            guard !near.isEmpty else { throw ComputerError("沒有點：\(scope.label)裡沒有\(target)，也沒有名字相近的元素。") }
            throw ComputerError("沒有點：\(scope.label)裡沒有\(target)。名字相近的（全螢幕截圖座標）：\n" + (try await listed(near)))
        }
    }

    /// Each ref's bounds (screen points), one `vmpal exec`; a ref whose bounds could not be read is absent.
    func bounds(_ refs: [String]) async throws -> [String: WindowSummary.Rect] {
        guard !refs.isEmpty else { return [:] }
        let out = Self.sections(try await agentDesktop(
            "a=\"$1\"; shift; for r in \"$@\"; do echo \"@@$r\"; \"$a\" get \"$r\" --property bounds; done; exit 0",
            [cfg.agentDesktop] + refs))
        return out.compactMapValues { ElementClick.bounds(Data($0.utf8)) }
    }

    /// An agent-desktop script through `vmpal exec`; a failed run throws agent-desktop's JSON error as text.
    func agentDesktop(_ script: String, _ args: [String]) async throws -> String {
        do { return try await exec(script, args) } catch let e as ComputerError {
            throw ComputerError(WindowSummary.errorText(e.description))
        }
    }

    /// A script's output split at `@@<name>` marker lines; lines before the first marker go to `first`.
    static func sections(_ out: String, first: String = "") -> [String: String] {
        var parts: [String: String] = [:], key = first
        for line in out.split(separator: "\n", omittingEmptySubsequences: true) {
            if line.hasPrefix("@@") { key = String(line.dropFirst(2)) } else { parts[key, default: ""] += line }
        }
        return parts
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
        let ref_id: String?
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

/// `click` by element (spec 3.1, M2 round 6): agent-desktop's answers read on this Mac (never
/// python in the VM: it pops the developer-tools installer), the pick, and what she reads back.
enum ElementClick {
    /// One `find` match she could click: one with a ref (and a name, unless she gave only a role).
    struct Match: Equatable {
        let role: String
        let name: String
        let ref: String
        /// Screen points, when the answer had them (a snapshot does, `find` does not).
        var bounds: WindowSummary.Rect? = nil
    }

    struct Window: Equatable {
        let id: String
        /// How results name it: 最前面的視窗（App「title」）.
        let label: String
    }

    /// Where the search goes with no front window (spec 3.1, 2026-11-09).
    static let desktop = Window(id: "", label: "Dock 和選單列（現在沒有最前面的視窗）")

    enum Pick: Equatable {
        case one(Match)
        case several([Match])
        /// No match; the ones whose name fits but whose role does not (empty without a role).
        case none([Match])
    }

    /// At most this many candidates are listed in a failed click.
    static let maxListed = 8
    /// Several matches: the first this many get their bounds read, to find ones at the same spot.
    static let maxPlaced = 20

    /// `list-windows`: the focused one; nil when none is.
    static func frontWindow(_ json: Data) throws -> Window? {
        struct W: Decodable { let id: String; let app_name: String?; let title: String?; let is_focused: Bool? }
        struct Answer: Decodable { let ok: Bool; let data: [W]?; let error: WindowSummary.Answer.Failure? }
        guard let a = try? JSONDecoder().decode(Answer.self, from: json) else {
            throw ComputerError("agent-desktop list-windows 的回答看不懂：\(String(decoding: json.prefix(300), as: UTF8.self))")
        }
        guard a.ok, let all = a.data else { throw ComputerError(a.error.map(WindowSummary.describe) ?? "agent-desktop 沒有回視窗") }
        guard let w = all.first(where: { $0.is_focused == true }) else { return nil }
        // `title` was the app's name in every sample (Finder, 2026-11-09); then only the app is said.
        let app = w.app_name ?? "?", title = w.title ?? ""
        return Window(id: w.id, label: "最前面的視窗（\(app)\(title.isEmpty || title == app ? "" : "「\(title)」")）")
    }

    /// `find`: the matches with a ref and a name (`unnamed`: also those without one), in agent-desktop's order.
    static func matches(_ json: Data, unnamed: Bool = false) throws -> [Match] {
        struct M: Decodable { let role: String?; let name: String?; let ref_id: String? }
        struct Payload: Decodable { let matches: [M] }
        struct Answer: Decodable { let ok: Bool; let data: Payload?; let error: WindowSummary.Answer.Failure? }
        guard let a = try? JSONDecoder().decode(Answer.self, from: json) else {
            throw ComputerError("agent-desktop find 的回答看不懂：\(String(decoding: json.prefix(300), as: UTF8.self))")
        }
        guard a.ok, let d = a.data else { throw ComputerError(a.error.map(WindowSummary.describe) ?? "agent-desktop find 沒有回結果") }
        return d.matches.compactMap { m in
            guard let ref = m.ref_id, unnamed || !(m.name ?? "").isEmpty else { return nil }
            return Match(role: m.role ?? "?", name: m.name ?? "", ref: ref)
        }
    }

    /// A `snapshot -i --include-bounds` (a window or a surface): every element with a ref and a name
    /// (`unnamed`: also those without one), in tree order.
    static func elements(_ json: Data, unnamed: Bool = false) throws -> [Match] {
        guard let a = try? JSONDecoder().decode(WindowSummary.Answer.self, from: json) else {
            throw ComputerError("agent-desktop snapshot 的回答看不懂：\(String(decoding: json.prefix(300), as: UTF8.self))")
        }
        guard a.ok, let tree = a.data?.tree else { throw ComputerError(a.error.map(WindowSummary.describe) ?? "agent-desktop 沒有回視窗") }
        var out: [Match] = []
        func walk(_ n: WindowSummary.Node) {
            if let ref = n.ref_id, unnamed || !(n.name ?? "").isEmpty {
                out.append(Match(role: n.role ?? "?", name: n.name ?? "", ref: ref, bounds: n.bounds))
            }
            n.children?.forEach(walk)
        }
        walk(tree)
        return out
    }

    /// Role first (case-insensitive) when given; then the names equal to `name` (case-insensitive),
    /// else the ones containing it. No name: the role alone.
    static func pick(_ found: [Match], role: String?, name: String?) -> Pick {
        let hits: [Match], named: [Match]
        if let name {
            let q = name.lowercased()
            named = found.filter { !$0.name.isEmpty && $0.name.lowercased().contains(q) }
            let fit = role.map { r in named.filter { $0.role.lowercased() == r.lowercased() } } ?? named
            let exact = fit.filter { $0.name.lowercased() == q }
            hits = exact.isEmpty ? fit : exact
        } else {
            named = []
            hits = found.filter { $0.role.lowercased() == role?.lowercased() }
        }
        switch hits.count {
        case 1: return .one(hits[0])
        case 0: return .none(named)
        default: return .several(hits)
        }
    }

    /// Several matches with their bounds where read: the same name (case-insensitive) at the same
    /// center is one target, the first (outermost) kept. One left is `.one`; a match without bounds
    /// always stays its own.
    static func collapse(_ ms: [Match]) -> Pick {
        var kept: [Match] = []
        for m in ms {
            let same = { (k: Match) -> Bool in
                guard let a = k.bounds, let b = m.bounds else { return false }
                return k.name.lowercased() == m.name.lowercased()
                    && abs((a.x + a.width / 2) - (b.x + b.width / 2)) < 1 && abs((a.y + a.height / 2) - (b.y + b.height / 2)) < 1
            }
            if !kept.contains(where: same) { kept.append(m) }
        }
        return kept.count == 1 ? .one(kept[0]) : .several(kept)
    }

    /// For a name with no match: the elements sharing the longest run of characters with it (at
    /// least half the name, and two unless the name is shorter), best first, at most `maxListed`.
    static func similar(_ all: [Match], to name: String) -> [Match] {
        let q = Array(name.lowercased())
        func run(_ s: String) -> Int {
            let t = Array(s.lowercased())
            var best = 0, prev = [Int](repeating: 0, count: t.count + 1)
            for a in q {
                var cur = [Int](repeating: 0, count: t.count + 1)
                for (j, b) in t.enumerated() where a == b {
                    cur[j + 1] = prev[j] + 1
                    best = max(best, cur[j + 1])
                }
                prev = cur
            }
            return best
        }
        // Half the name at least: "nots.txt" finds "notes.txt" (s.txt), not "Recents" (ts).
        let need = max(min(2, q.count), (q.count + 1) / 2)
        let scored = all.map { ($0, run($0.name)) }.filter { $0.1 >= need }
        return Array(scored.enumerated().sorted { ($0.1.1, -$0.0) > ($1.1.1, -$1.0) }.map(\.1.0).prefix(maxListed))
    }

    /// `get --property bounds`: screen points.
    static func bounds(_ json: Data) -> WindowSummary.Rect? {
        struct Payload: Decodable { let value: WindowSummary.Rect? }
        struct Answer: Decodable { let ok: Bool; let data: Payload? }
        guard let a = try? JSONDecoder().decode(Answer.self, from: json), a.ok else { return nil }
        return a.data?.value
    }

    /// A rectangle's center in her full-screenshot pixels, converted like the crop and the Dock.
    static func center(_ b: WindowSummary.Rect, on screen: WindowSummary.Rect, width: Int) -> (x: Int, y: Int) {
        func px(_ points: Double) -> Int { Int((points * Double(width) / screen.width).rounded()) }
        return (px(b.x + b.width / 2 - screen.x), px(b.y + b.height / 2 - screen.y))
    }

    /// `role "name" (x,y)`, the outline's and the Dock summary's form; `role (x,y)` without a name.
    static func line(_ m: Match, _ at: (x: Int, y: Int)?) -> String {
        "\(m.role) " + (m.name.isEmpty ? "" : "\"\(m.name)\" ") + (at.map { "(\($0.x),\($0.y))" } ?? "(位置讀不到)")
    }

    /// One press's answer: whether agent-desktop said ok, and its words (code: message, or the raw
    /// answer) and disposition as given.
    static func answer(_ data: Data) -> (ok: Bool, words: String, disposition: String) {
        let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        let ok = obj?["ok"] as? Bool ?? false
        let body = (ok ? obj?["data"] : obj?["error"]) as? [String: Any]
        let disposition = (body?["disposition"] as? [String: Any]).map { d in
            d.keys.sorted { k1, k2 in
                let order = ["delivery", "retry"]
                return (order.firstIndex(of: k1) ?? 2, k1) < (order.firstIndex(of: k2) ?? 2, k2)
            }.map { "\($0)=\(d[$0].map { "\($0)" } ?? "")" }.joined(separator: ", ")
        } ?? "沒有 disposition"
        let why = [body?["code"] as? String, body?["message"] as? String].compactMap { $0 }.joined(separator: ": ")
        return (ok, why.isEmpty && !ok ? String(decoding: data.prefix(300), as: UTF8.self) : why, disposition)
    }

    /// The click's answer: what was clicked, where, which way got through (the accessibility press,
    /// or real pointer input after agent-desktop said the press was not delivered) and agent-desktop's
    /// words and disposition as given. `delivered_unverified` is not proof; the screenshots after it
    /// are. A click neither way got through throws.
    static func clicked(_ m: Match, verb: String, at: (x: Int, y: Int)?, semantic: Data, headed: Data?) throws -> String {
        let what = line(m, at)
        let done = ["click": "點了", "double-click": "雙擊了", "right-click": "右鍵點了"][verb] ?? verb
        let press = answer(semantic)
        let pressSaid = (press.words.isEmpty ? "" : "\(press.words)；") + press.disposition
        guard let headed else {
            guard press.ok else { throw ComputerError("\(verb) \(what) 失敗（無障礙按壓）：\(press.words)（agent-desktop：\(press.disposition)）") }
            return "\(done) \(what)（全螢幕截圖座標），用無障礙按壓（semantic）。agent-desktop：\(press.disposition)；這不代表真的點到，看下面的截圖。"
        }
        let mouse = answer(headed)
        guard mouse.ok else {
            throw ComputerError("沒有點成 \(what)：無障礙按壓沒送出（agent-desktop：\(pressSaid)），實體滑鼠（headed）也沒有（agent-desktop：\((mouse.words.isEmpty ? "" : "\(mouse.words)；") + mouse.disposition)）。")
        }
        return "\(done) \(what)（全螢幕截圖座標），用實體滑鼠（physical，headed）：無障礙按壓沒送出（agent-desktop：\(pressSaid)）。"
            + "實體滑鼠 agent-desktop：\(mouse.disposition)；這不代表真的點到，看下面的截圖。"
    }
}

/// An image's pixel size from its PNG or JPEG header, for turning crop coordinates into the
/// screen's (spec 3.1, M2 round 6: she clicked crop positions as if they were full-screen ones).
enum ImageSize {
    static func of(_ data: Data) -> (width: Int, height: Int)? {
        let b = [UInt8](data.prefix(64 * 1024))
        func be16(_ i: Int) -> Int { Int(b[i]) << 8 | Int(b[i + 1]) }
        func be32(_ i: Int) -> Int { be16(i) << 16 | be16(i + 2) }
        if b.count >= 24, b.starts(with: [0x89, 0x50, 0x4E, 0x47]) { return (be32(16), be32(20)) }
        guard b.count >= 4, b[0] == 0xFF, b[1] == 0xD8 else { return nil }
        var i = 2
        while i + 9 < b.count {
            guard b[i] == 0xFF else { return nil }
            let marker = b[i + 1]
            if marker == 0xFF { i += 1; continue }
            if marker == 0xD8 || marker == 0x01 || (0xD0...0xD7).contains(marker) { i += 2; continue }
            // SOF0–SOF15 except DHT (C4), JPG (C8), DAC (CC): height, then width.
            if (0xC0...0xCF).contains(marker), ![0xC4, 0xC8, 0xCC].contains(marker) { return (be16(i + 7), be16(i + 5)) }
            i += 2 + be16(i + 2)
        }
        return nil
    }
}

/// The open windows and the focused element as one line (spec 3.1, 2026-11-09), from agent-desktop's
/// `list-windows` and `snapshot --surface focused`. Only windows agent-desktop calls visible are
/// listed (the rest are menu-bar and helper windows: Caffeine, Siri, loginwindow), in its order.
/// `list-windows` gives the app's name as `title` (every sample, 2026-11-09), so a window is named
/// by its app alone unless the title differs; the focused one takes its title from the snapshot's
/// window element. The focused element: role, "name", and the start of its value.
enum OpenWindows {
    /// At most this many characters of the focused element's value are shown.
    static let maxValue = 20

    struct Window: Decodable { let id: String; let app_name: String?; let title: String?; let is_focused: Bool?; let visible: Bool? }
    struct Windows: Decodable { let ok: Bool; let data: [Window]?; let error: WindowSummary.Answer.Failure? }

    final class Node: Decodable {
        let role: String?
        let name: String?
        let value: String?
        let states: [String]?
        let children: [Node]?
        enum CodingKeys: String, CodingKey { case role, name, value, states, children }
        init(from d: Decoder) throws {
            let c = try d.container(keyedBy: CodingKeys.self)
            role = try c.decodeIfPresent(String.self, forKey: .role)
            name = try c.decodeIfPresent(String.self, forKey: .name)
            value = try? c.decodeIfPresent(String.self, forKey: .value)   // a number or flag is not shown
            states = try c.decodeIfPresent([String].self, forKey: .states)
            children = try c.decodeIfPresent([Node].self, forKey: .children)
        }
    }
    struct Focused: Decodable {
        struct Win: Decodable { let id: String? }
        struct Payload: Decodable { let window: Win?; let tree: Node? }
        let ok: Bool
        let data: Payload?
        let error: WindowSummary.Answer.Failure?
    }

    static func line(windows: Data, focused: Data) -> String {
        let f = try? JSONDecoder().decode(Focused.self, from: focused)
        let focusedTitle = f?.data?.tree?.name.flatMap { $0.isEmpty ? nil : $0 }
        let list: String
        if let w = try? JSONDecoder().decode(Windows.self, from: windows) {
            if w.ok, let all = w.data {
                let shown = all.filter { $0.visible == true }.map { win in
                    let app = win.app_name ?? "?"
                    let own = win.is_focused == true ? focusedTitle ?? win.title : win.title
                    let title = own.flatMap { $0.isEmpty || $0 == app ? nil : $0 }
                    return app + (title.map { "「\($0)」" } ?? "") + (win.is_focused == true ? "（前景）" : "")
                }
                list = "開著的視窗：" + (shown.isEmpty ? "沒有" : shown.joined(separator: "、"))
            } else {
                list = "開著的視窗讀不到：" + (w.error.map(WindowSummary.describe) ?? "agent-desktop 沒有回視窗")
            }
        } else {
            list = "開著的視窗讀不到：agent-desktop 的回答不是 JSON：\(String(decoding: windows.prefix(300), as: UTF8.self))"
        }
        let focus: String
        if let f {
            if f.ok, let tree = f.data?.tree {
                focus = find(tree).map { "焦點：" + describe($0) } ?? "焦點：沒有"
            } else {
                focus = "焦點讀不到：" + (f.error.map(WindowSummary.describe) ?? "agent-desktop 沒有回內容")
            }
        } else {
            focus = "焦點讀不到：agent-desktop 的回答不是 JSON：\(String(decoding: focused.prefix(300), as: UTF8.self))"
        }
        return list + "；" + focus
    }

    /// The first element, depth first, whose states include `focused`.
    static func find(_ n: Node) -> Node? {
        if n.states?.contains("focused") == true { return n }
        for c in n.children ?? [] { if let hit = find(c) { return hit } }
        return nil
    }

    static func describe(_ n: Node) -> String {
        var out = n.role ?? "?"
        if let name = n.name, !name.isEmpty { out += " \"\(name)\"" }
        if let v = n.value?.replacingOccurrences(of: "\n", with: " "), !v.isEmpty {
            out += "「" + (v.count > maxValue ? String(v.prefix(maxValue)) + "…" : v) + "」"
        }
        return out
    }
}
