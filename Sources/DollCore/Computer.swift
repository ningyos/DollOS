import Foundation

/// Her computer is this Mac (2026-11-16, owner): she drives the owner's real apps in the background
/// through the open-source `cua-driver` (https://github.com/trycua/cua), without taking his pointer,
/// keyboard or frontmost app. She reads one window at a time (its accessibility tree with numbered
/// rows, and a screenshot of that window) and acts on a row of the latest read. Tests swap in a fake
/// driver.

public struct ComputerError: Error, CustomStringConvertible {
    public let description: String
    public init(_ description: String) { self.description = description }
}

/// One cua-driver tool call (`cua-driver <tool> '<json>'`): the JSON answer on success; a refusal or
/// failure throws `ComputerError` with the driver's own JSON answer (or its stderr) as text.
public protocol ComputerDriver: Sendable {
    func call(_ tool: String, _ json: String) async throws -> String
}

/// The real driver: one `cua-driver` process per call; the daemon it talks to is `CuaDaemon`'s.
public struct CuaDriverCLI: ComputerDriver {
    let path: String
    public init(path: String) { self.path = path }

    public func call(_ tool: String, _ json: String) async throws -> String {
        let r = try await runProcess(path, [tool, json])
        guard r.status == 0 else { throw ComputerError(r.stdout.isEmpty ? r.stderr : r.stdout) }
        return r.stdout
    }
}

/// stdout and stderr (trimmed) and the exit status. `terminationHandler`, not `waitUntilExit`:
/// waitUntilExit on a dispatch worker was seen to hang after the child had exited (2026-11-08).
func runProcess(_ path: String, _ args: [String]) async throws -> (status: Int32, stdout: String, stderr: String) {
    let p = Process()
    p.executableURL = URL(filePath: path)
    p.arguments = args
    let out = Pipe(), err = Pipe()
    p.standardOutput = out
    p.standardError = err
    p.standardInput = FileHandle.nullDevice
    let exited = DispatchSemaphore(value: 0)
    p.terminationHandler = { _ in exited.signal() }
    do { try p.run() } catch { throw ComputerError("cannot run \(path): \(error.localizedDescription)") }
    let (o, e) = await withCheckedContinuation { (c: CheckedContinuation<(Data, Data), Never>) in
        DispatchQueue.global().async {
            // stderr is drained on its own thread so a long stdout (a big tree) cannot block on it.
            var errData = Data()
            let errDone = DispatchSemaphore(value: 0)
            DispatchQueue.global().async { errData = err.fileHandleForReading.readDataToEndOfFile(); errDone.signal() }
            let o = out.fileHandleForReading.readDataToEndOfFile()
            errDone.wait()
            exited.wait()
            c.resume(returning: (o, errData))
        }
    }
    let trim = { (d: Data) in String(decoding: d, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines) }
    return (p.terminationStatus, trim(o), trim(e))
}

/// The driver's daemon at boot (2026-11-16): dollcore starts it when it is not running and stops the
/// one it started when it exits; one already running (started by someone else) is used and left
/// running. Missing driver, missing permission or a daemon that does not come up stops the boot with
/// the exact fix; there is no fallback.
public final class CuaDaemon: @unchecked Sendable {
    /// The `cua-driver serve` this process started; nil when the daemon was already running.
    let child: Process?
    let driver: String

    init(driver: String, child: Process?) { (self.driver, self.child) = (driver, child) }

    /// `startWait`: how long to wait for a daemon this process started to answer.
    public static func ensure(driver: String, startWait: Double) async throws -> CuaDaemon {
        guard FileManager.default.isExecutableFile(atPath: driver) else {
            throw ComputerError("沒有 cua-driver：\(driver) 不存在或不能執行。修法：安裝 CuaDriver.app（https://github.com/trycua/cua，"
                                + "/Applications/CuaDriver.app），把 [computer] driver 設成 /Applications/CuaDriver.app/Contents/MacOS/cua-driver。")
        }
        // Never asks for anything; when the daemon is down it starts one briefly to answer for itself.
        let perm = try await runProcess(driver, ["permissions", "status", "--json"])
        guard let j = try? JSONSerialization.jsonObject(with: Data(perm.stdout.utf8)) as? [String: Any] else {
            throw ComputerError("cua-driver permissions status 讀不懂（exit \(perm.status)）：\(perm.stdout) \(perm.stderr)")
        }
        var missing: [String] = []
        if j["accessibility"] as? Bool != true { missing.append("輔助使用（Accessibility）") }
        if j["screen_recording"] as? Bool != true { missing.append("螢幕錄製（Screen Recording）") }
        guard missing.isEmpty else {
            throw ComputerError("CuaDriver 沒有\(missing.joined(separator: "和"))權限。修法：執行 `\(driver) permissions grant`，"
                                + "或到 系統設定 › 隱私權與安全性 打開 CuaDriver 的這兩項，然後重開 dollcore。")
        }
        if try await running(driver) { return CuaDaemon(driver: driver, child: nil) }
        let p = Process()
        p.executableURL = URL(filePath: driver)
        p.arguments = ["serve"]
        p.standardInput = FileHandle.nullDevice
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { throw ComputerError("cua-driver serve 起不來：\(error.localizedDescription)") }
        let deadline = Date().addingTimeInterval(startWait)
        while Date() < deadline {
            if !p.isRunning { throw ComputerError("cua-driver serve 一啟動就結束了（exit \(p.terminationStatus)）。修法：在終端機跑 `\(driver) serve` 看它說什麼。") }
            if try await running(driver) { return CuaDaemon(driver: driver, child: p) }
            try await Task.sleep(for: .milliseconds(200))
        }
        p.terminate()
        throw ComputerError("cua-driver serve \(Int(startWait)) 秒內沒有回應。修法：在終端機跑 `\(driver) serve` 看它說什麼。")
    }

    static func running(_ driver: String) async throws -> Bool {
        try await runProcess(driver, ["status"]).status == 0
    }

    public var started: Bool { child != nil }

    /// Stops the daemon this process started (`cua-driver stop`, then the child itself); leaves one it found running.
    public func stop() async {
        guard let child, child.isRunning else { return }
        _ = try? await runProcess(driver, ["stop"])
        if child.isRunning { child.terminate() }
    }
}

/// The front app as an experience names it: one app at one version.
public struct AppIdentity: Sendable, Equatable {
    public var bundleID: String
    public var name: String
    public var version: String
    public init(bundleID: String, name: String, version: String) { (self.bundleID, self.name, self.version) = (bundleID, name, version) }
}

/// One element as the action log keeps it (2026-11-16, owner decision 5): what identifies it across
/// reads, not only its row in one read, so a recorded chain can be found again later.
public struct ElementIdentity: Codable, Sendable, Equatable {
    public var row: Int
    public var role: String
    public var subrole: String?
    /// The AX identifier (`id=` in the tree), when the app sets one: the most stable handle.
    public var axID: String?
    public var label: String?
    public var title: String?
    /// The ancestors from the window down, each `role "label"` (label omitted when empty).
    public var path: [String]
}

/// One action in her action log (`computer-actions.jsonl` under the data dir), one JSON line each.
public struct ActionRecord: Codable, Sendable, Equatable {
    public var ts: Double
    /// The todo item in progress, verbatim.
    public var item: String?
    public var tool: String
    public var app: String
    public var bundleID: String
    public var window: String?
    public var element: ElementIdentity?
    /// What else the action needs to be done again: text, key, modifiers, menu path, direction, x/y.
    public var args: [String: String]
    public var ok: Bool
    public var error: String?
}

/// What a computer tool hands back to the loop: her text, the window screenshot (a file under
/// `shots/`) and the window's element list (the latest read; collapsed when a newer one exists).
public struct MacResult: Sendable {
    public var text: String
    public var image: String?
    public var window: String?
    public var failed = false
    /// The app the call worked on, for experiences.
    public var app: AppIdentity?
    /// A success's piece of the item's action list (`click AXButton "7"`, `menu 檔案 › 新增`).
    public var step: String?
}

/// Her computer (2026-11-16). One session label per process; the latest read is what rows mean.
public actor Mac {
    let driver: any ComputerDriver
    let cfg: ComputerConfig
    let session: String
    let shots: URL
    let logURL: URL
    /// The todo item in progress, for the action log; set by the loop.
    var item: String?

    struct Read {
        var pid: Int
        var app: AppIdentity
        var windowID: Int
        var title: String
        var snapshot: String
        var rows: [Int: ElementIdentity]
        var maxRow: Int
        /// Screenshot pixel size (her x,y space).
        var size: (w: Int, h: Int)?
    }
    /// The latest read: rows and x,y mean this window.
    var current: Read?
    /// The read she saw when her reply was written (`beginReply`): a row in that reply means this
    /// read's element, also after an earlier action of the reply read the window again.
    var seen: Read?

    public init(driver: any ComputerDriver, config: ComputerConfig, session: String, shots: URL, log: URL) {
        self.driver = driver
        cfg = config
        self.session = session
        self.shots = shots
        logURL = log
    }

    /// A reply of hers starts: its rows mean the latest read as it is now.
    public func beginReply(item: String?) {
        self.item = item
        seen = current
    }

    /// The window of the latest read, read again (a chain failed before anything was sent).
    public func look() async throws -> MacResult {
        guard let c = current else { throw ComputerError("還沒讀過視窗") }
        return try await reread(c)
    }

    // driver plumbing --------------------------------------------------------
    func call(_ tool: String, _ args: [String: Any]) async throws -> [String: Any] {
        let data = try JSONSerialization.data(withJSONObject: args, options: [.sortedKeys])
        let out = try await driver.call(tool, String(decoding: data, as: UTF8.self))
        guard let j = try? JSONSerialization.jsonObject(with: Data(out.utf8)) as? [String: Any] else {
            throw ComputerError("cua-driver \(tool) 的回答讀不懂：\(out.prefix(300))")
        }
        return j
    }

    /// The driver's refusal in her words: its message and hint, without the JSON around them.
    static func said(_ error: Error) -> String {
        let text = "\(error)"
        guard let j = try? JSONSerialization.jsonObject(with: Data(text.utf8)) as? [String: Any] else { return text }
        let refusal = j["refusal"] as? [String: Any]
        let code = (refusal?["code"] ?? j["code"]) as? String
        let message = (refusal?["message"] ?? j["message"] ?? j["reason"] ?? j["error"]) as? String
        let parts = [code.map { "[\($0)]" }, message, j["hint"] as? String].compactMap { $0 }
        return parts.isEmpty ? text : parts.joined(separator: " ")
    }

    // apps -------------------------------------------------------------------
    struct AppInfo { var name: String; var bundleID: String; var pid: Int?; var path: String? }

    func listApps() async throws -> [AppInfo] {
        let j = try await call("list_apps", [:])
        return (j["apps"] as? [[String: Any]] ?? []).compactMap { a in
            guard let name = a["name"] as? String else { return nil }
            return AppInfo(name: name, bundleID: a["bundle_id"] as? String ?? "",
                           pid: (a["running"] as? Bool == true) ? a["pid"] as? Int : nil, path: a["launch_path"] as? String)
        }
    }

    /// An app by its name (any case), its window's app name (localized, e.g. 計算機) or its bundle id.
    func find(_ app: String, in apps: [AppInfo]) -> AppInfo? {
        let q = app.trimmingCharacters(in: .whitespaces).lowercased()
        let path = { (a: AppInfo) in ((a.path ?? "") as NSString).lastPathComponent.lowercased() }
        return apps.first { $0.bundleID.lowercased() == q } ?? apps.first { $0.name.lowercased() == q }
            ?? apps.first { path($0) == q + ".app" }
    }

    static func identity(_ a: AppInfo) -> AppIdentity {
        var version = ""
        if let p = a.path, let info = NSDictionary(contentsOfFile: p + "/Contents/Info.plist") {
            version = info["CFBundleShortVersionString"] as? String ?? ""
        }
        return AppIdentity(bundleID: a.bundleID, name: a.name, version: version)
    }

    struct Win { var id: Int; var title: String; var z: Int }

    /// The app's windows she can read: on screen, layer 0, front first.
    func windows(pid: Int) async throws -> [Win] {
        let j = try await call("list_windows", ["pid": pid])
        return (j["windows"] as? [[String: Any]] ?? []).compactMap { w -> Win? in
            guard w["layer"] as? Int == 0, w["is_on_screen"] as? Bool == true, let id = w["window_id"] as? Int else { return nil }
            return Win(id: id, title: w["title"] as? String ?? "", z: w["z_index"] as? Int ?? 0)
        }.sorted { $0.z > $1.z }
    }

    /// Running apps that have windows, each with its on-screen windows (the `apps` tool).
    public func apps() async throws -> String {
        let running = try await listApps().filter { $0.pid != nil }
        let j = try await call("list_windows", [:])
        var byPid: [Int: [(String, Int)]] = [:]
        for w in j["windows"] as? [[String: Any]] ?? [] where w["is_on_screen"] as? Bool == true {
            guard let pid = w["pid"] as? Int else { continue }
            byPid[pid, default: []].append((w["title"] as? String ?? "", w["z_index"] as? Int ?? 0))
        }
        var lines = ["開著的 app（有視窗的列出視窗標題，前面的在上）："]
        for a in running.sorted(by: { $0.name < $1.name }) {
            let ws = (byPid[a.pid!] ?? []).sorted { $0.1 > $1.1 }.map { "「\($0.0)」" }
            lines.append("- \(a.name)" + (ws.isEmpty ? "（沒有視窗）" : "：" + ws.joined(separator: "、")))
        }
        return lines.joined(separator: "\n")
    }

    // reading ------------------------------------------------------------------
    /// `open`: launches (or reaches) the app in the background, optionally handing it a file or URL,
    /// then reads its front window. No window (a document app launched in the background has none):
    /// says so and how to get one; nothing is guessed.
    public func open(_ app: String, target: String?) async throws -> MacResult {
        let apps = try await listApps()
        var args: [String: Any] = ["session": session]
        let known = find(app, in: apps)
        if let known { args["bundle_id"] = known.bundleID } else { args["name"] = app }
        if let target {
            if target.contains("://") { args["urls"] = [target] } else {
                let path = Self.expand(target)
                guard FileManager.default.fileExists(atPath: path) else {
                    throw ComputerError("沒有開：\(target) 不存在。要新檔案：先用 write_file 建一個（可以是空的），再 open 它。")
                }
                args["urls"] = [path]
            }
        }
        let j: [String: Any]
        do { j = try await call("launch_app", args) } catch {
            let names = known == nil ? "（找不到叫「\(app)」的 app；用 apps 看開著的 app 名字，或給 bundle id）" : ""
            throw ComputerError("開不了 \(app)：\(Self.said(error))\(names)")
        }
        guard let pid = j["pid"] as? Int else { throw ComputerError("開不了 \(app)：cua-driver 沒給 pid") }
        let info = known ?? AppInfo(name: j["name"] as? String ?? app, bundleID: j["bundle_id"] as? String ?? "", pid: pid, path: nil)
        let identity = Self.identity(info)
        // A window may take a moment after launch_app returns.
        var wins: [Win] = []
        for attempt in 0..<cfg.windowWaitTries {
            wins = try await windows(pid: pid)
            if !wins.isEmpty || attempt == cfg.windowWaitTries - 1 { break }
            try await Task.sleep(for: .seconds(cfg.windowWaitS))
        }
        guard let front = wins.first else {
            current = nil
            var r = MacResult(text: "\(identity.name) 開著，但沒有視窗（在背景開的 app 常常沒有開新視窗）。要有視窗：新文件用 menu(app: \"\(identity.name)\", path: [\"檔案\", \"新增\"])"
                              + "（英文選單是 File › New，選單名字以 app 自己的為準）；要開某個檔案就 open 帶 target。", image: nil, window: nil)
            r.app = identity
            r.step = "open \(identity.name)" + (target.map { " \($0)" } ?? "")
            try log(ActionRecord(ts: Date().timeIntervalSince1970, item: item, tool: "open", app: identity.name, bundleID: identity.bundleID, window: nil,
                             element: nil, args: target.map { ["target": $0] } ?? [:], ok: true, error: nil))
            return r
        }
        try log(ActionRecord(ts: Date().timeIntervalSince1970, item: item, tool: "open", app: identity.name, bundleID: identity.bundleID, window: front.title,
                         element: nil, args: target.map { ["target": $0] } ?? [:], ok: true, error: nil))
        var r = try await read(pid: pid, app: identity, window: front, of: wins.count)
        r.step = "open \(identity.name)" + (target.map { " \($0)" } ?? "")
        return r
    }

    /// `read_window`: the app's window (`window` = its title, or part of it; none = the front one).
    public func readWindow(_ app: String?, window: String?) async throws -> MacResult {
        let (pid, identity) = try await target(app)
        let wins = try await windows(pid: pid)
        guard !wins.isEmpty else {
            throw ComputerError("\(identity.name) 現在沒有視窗。新文件用 menu 選「檔案 › 新增」；開檔用 open 帶 target。")
        }
        var pick = wins[0]
        if let window, !window.isEmpty {
            guard let w = wins.first(where: { $0.title == window }) ?? wins.first(where: { $0.title.localizedCaseInsensitiveContains(window) }) else {
                throw ComputerError("\(identity.name) 沒有標題含「\(window)」的視窗；它的視窗：" + wins.map { "「\($0.title)」" }.joined(separator: "、"))
            }
            pick = w
        }
        return try await read(pid: pid, app: identity, window: pick, of: wins.count)
    }

    /// The app she names, or the one of the latest read.
    func target(_ app: String?) async throws -> (Int, AppIdentity) {
        if let app, !app.isEmpty {
            if let c = current, c.app.name.lowercased() == app.lowercased() || c.app.bundleID == app {
                return (c.pid, c.app)
            }
            guard let a = find(app, in: try await listApps()) else {
                throw ComputerError("找不到叫「\(app)」的 app；用 apps 看開著的 app 名字。")
            }
            guard let pid = a.pid else { throw ComputerError("\(a.name) 沒開；先用 open。") }
            return (pid, Self.identity(a))
        }
        guard let c = current else { throw ComputerError("還沒讀過任何視窗；給 app，或先 open / read_window。") }
        return (c.pid, c.app)
    }

    /// One read of a window: its tree with numbered rows and its screenshot.
    func read(pid: Int, app: AppIdentity, window: Win, of count: Int) async throws -> MacResult {
        let file = "\(UUID().uuidString.lowercased()).jpg"
        let j: [String: Any]
        do {
            j = try await call("get_window_state", [
                "pid": pid, "window_id": window.id, "session": session, "tree_format": "both",
                "max_elements": cfg.maxElements, "max_image_dimension": cfg.screenshotMaxDimension,
                "screenshot_format": "jpeg", "screenshot_out_file": shots.appending(path: file).path,
            ])
        } catch {
            current = nil
            throw ComputerError("讀不到 \(app.name) 的視窗「\(window.title)」：\(Self.said(error))" + (await otherWindows(pid, not: window.id)))
        }
        guard let snapshot = j["snapshot_id"] as? String, let tree = j["tree_markdown"] as? String else {
            current = nil
            // Seen 2026-11-16: Calculator with a second, untitled window in front that has no tree;
            // she found the real one only through `apps`. The other windows are named here.
            let why = (j["degraded_reason"] as? String).map { "（cua-driver：\($0)）" } ?? ""
            throw ComputerError("讀不到 \(app.name) 的視窗「\(window.title)」：回答裡沒有元素清單\(why)" + (await otherWindows(pid, not: window.id)))
        }
        let elements = j["elements"] as? [[String: Any]] ?? []
        let rows = Self.identities(elements, markdown: tree)
        let size = (j["screenshot_width"] as? Int).flatMap { w in (j["screenshot_height"] as? Int).map { (w: w, h: $0) } }
        let title = j["window_title"] as? String ?? window.title
        current = Read(pid: pid, app: app, windowID: window.id, title: title, snapshot: snapshot, rows: rows,
                       maxRow: rows.keys.max() ?? -1, size: size)
        let image = FileManager.default.fileExists(atPath: shots.appending(path: file).path) ? file : nil
        var head = "\(app.name) 的視窗「\(title)」" + (count > 1 ? "（這個 app 有 \(count) 個視窗；讀別的用 read_window 帶 window 標題）" : "")
        if let size { head += "；截圖 \(size.w)×\(size.h)" }
        var body = head + "\n" + tree
        if j["truncated"] as? Bool == true {
            body += "\n（清單在 \(cfg.maxElements) 個元素截斷了；要的東西不在清單裡就看截圖，用 click 的 x、y）"
        }
        if image == nil { body += "\n（這次沒有截圖：\(j["degraded_reason"] as? String ?? "cua-driver 沒寫出截圖檔")）" }
        if let warn = Self.passwordField(elements) { body += "\n" + warn }
        var r = MacResult(text: "", image: image, window: body)
        r.app = app
        if let auth = await authLine() { r.text = auth }
        return r
    }

    /// After a failed read: the app's other windows, for read_window(window:); empty when there are none.
    func otherWindows(_ pid: Int, not id: Int) async -> String {
        let wins: [Win]
        do { wins = try await windows(pid: pid).filter { $0.id != id } } catch { return "（它另外的視窗也沒列出來：\(Self.said(error))）" }
        guard !wins.isEmpty else { return "" }
        return "。這個 app 另外的視窗：" + wins.map { "「\($0.title)」" }.joined(separator: "、") + "；用 read_window 帶 window 標題讀"
    }

    /// Row → stable identity from the structured elements; the AX id comes from the markdown row (`id=`).
    static func identities(_ elements: [[String: Any]], markdown: String) -> [Int: ElementIdentity] {
        var ids: [Int: String] = [:]
        for line in markdown.split(separator: "\n") {
            guard let open = line.firstIndex(of: "["), let close = line[open...].firstIndex(of: "]"),
                  let row = Int(line[line.index(after: open)..<close]) else { continue }
            if let r = line.range(of: "[id=") ?? line.range(of: " id=") {
                let rest = line[r.upperBound...]
                let end = rest.firstIndex(where: { $0 == " " || $0 == "]" }) ?? rest.endIndex
                ids[row] = String(rest[..<end])
            }
        }
        var byIndex: [Int: [String: Any]] = [:]
        for e in elements { if let i = e["element_index"] as? Int { byIndex[i] = e } }
        func name(_ e: [String: Any]) -> String {
            let role = e["role"] as? String ?? "?"
            let label = (e["label"] as? String) ?? (e["title"] as? String) ?? ""
            return label.isEmpty ? role : "\(role) \"\(label)\""
        }
        var out: [Int: ElementIdentity] = [:]
        for (i, e) in byIndex {
            var path: [String] = []
            var p = e["parent_index"] as? Int
            var guardCount = 0
            while let pi = p, let pe = byIndex[pi], guardCount < 64 {
                path.insert(name(pe), at: 0)
                p = pe["parent_index"] as? Int
                guardCount += 1
            }
            out[i] = ElementIdentity(row: i, role: e["role"] as? String ?? "?", subrole: e["subrole"] as? String, axID: ids[i],
                                     label: e["label"] as? String, title: e["title"] as? String, path: path)
        }
        return out
    }

    /// A password field in the window: she does not type passwords herself.
    static func passwordField(_ elements: [[String: Any]]) -> String? {
        guard elements.contains(where: { ($0["role"] as? String) == "AXSecureTextField" || ($0["subrole"] as? String) == "AXSecureTextField" }) else { return nil }
        return "【注意】這個視窗有密碼欄位。密碼不要自己打：停下來，用 ask_owner 跟主人說要做什麼、在哪裡、為什麼。"
    }

    /// A system authentication dialog on screen (Touch ID, an administrator password): its owner
    /// process is one of the system's authentication agents (`ComputerConfig.authProcesses`).
    func authDialog() async throws -> String? {
        let j = try await call("list_windows", [:])
        for w in j["windows"] as? [[String: Any]] ?? [] where w["is_on_screen"] as? Bool == true {
            let owner = w["app_name"] as? String ?? ""
            if cfg.authProcesses.contains(owner) {
                return "【系統驗證】畫面上有系統的驗證視窗（\(owner)「\(w["title"] as? String ?? "")」：Touch ID 或密碼）。停下來，不要試著通過它；用 ask_owner 跟主人說是哪一步叫出它的。"
            }
        }
        return nil
    }

    /// `authDialog` as a line for her: the warning, nil when there is none, or why the check failed.
    func authLine() async -> String? {
        do { return try await authDialog() } catch { return "（沒查到畫面上有沒有系統驗證視窗：\(Self.said(error))）" }
    }

    // acting -------------------------------------------------------------------
    public enum Act: Sendable {
        case click(row: Int?, x: Int?, y: Int?, button: String, count: Int)
        case type(text: String, row: Int?)
        case key(key: String, modifiers: [String])
        case scroll(direction: String, amount: Int, row: Int?)
        case menu(app: String?, path: [String])
    }

    /// Does one action on the window of the latest read (a menu: on the app she names), then reads
    /// that window again (or the app's front window when that one closed) so she never acts on a stale
    /// read. A row not in the latest read is refused before anything is sent.
    public func act(_ a: Act) async throws -> MacResult {
        if case .menu(let app, let path) = a { return try await menu(app, path) }
        guard let c = current else { throw ComputerError("沒有做：還沒讀過視窗（或上一次讀失敗了）；先 read_window 或 open。") }
        var args: [String: Any] = ["pid": c.pid, "window_id": c.windowID, "session": session]
        var element: ElementIdentity?
        var logArgs: [String: String] = [:]
        func row(_ n: Int) throws {
            guard let e = c.rows[n] else {
                throw ComputerError("沒有做：第 \(n) 行不在最新一次讀到的清單裡（\(c.app.name)「\(c.title)」，行號 0–\(c.maxRow)）。重新 read_window，用新清單的行號。")
            }
            // An earlier action of this reply read the window again: the row must still be the
            // element she saw, or it is not hers to press.
            if let s = seen, s.snapshot != c.snapshot {
                guard s.windowID == c.windowID, let was = s.rows[n], Self.same(was, e) else {
                    let was = s.rows[n].map(Self.describe) ?? "沒有這一行"
                    throw ComputerError("沒有做：這則回覆前面的動作改變了視窗，第 \(n) 行現在是 \(Self.describe(e))，你看到的是 \(was)。看新清單再選。")
                }
            }
            element = e
            args["element_token"] = "\(c.snapshot):\(n)"
        }
        let tool: String
        switch a {
        case .click(let r, let x, let y, let button, let count):
            if let r { try row(r) } else if let x, let y {
                if let s = c.size, !(0..<s.w).contains(x) || !(0..<s.h).contains(y) {
                    throw ComputerError("沒有點：\(x),\(y) 在截圖外（截圖 \(s.w)×\(s.h)）")
                }
                args["x"] = x; args["y"] = y
                logArgs = ["x": "\(x)", "y": "\(y)"]
            } else { throw ComputerError("click 要給 row，或 x 和 y") }
            if button != "left" { args["button"] = button; logArgs["button"] = button }
            tool = count == 2 ? "double_click" : "click"
        case .type(let text, let r):
            if let r { try row(r) }
            args["text"] = text
            logArgs = ["text": text]
            tool = "type_text"
        case .key(let key, let mods):
            if mods.isEmpty { args["key"] = key; tool = "press_key" } else { args["keys"] = mods + [key]; tool = "hotkey" }
            logArgs = ["key": key, "modifiers": mods.joined(separator: "+")]
        case .scroll(let direction, let amount, let r):
            if let r { try row(r) }
            args["direction"] = direction; args["amount"] = amount
            logArgs = ["direction": direction, "amount": "\(amount)"]
            tool = "scroll"
        case .menu: fatalError("handled above")
        }
        var record = ActionRecord(ts: Date().timeIntervalSince1970, item: item, tool: Self.toolName(a), app: c.app.name, bundleID: c.app.bundleID,
                                  window: c.title, element: element, args: logArgs, ok: true, error: nil)
        var said = ""
        do {
            let j = try await call(tool, args)
            said = Self.effect(j)
        } catch {
            record.ok = false
            record.error = Self.said(error)
            try log(record)
            var r = try await reread(c)
            r.text = "失敗：\(Self.said(error))" + (r.text.isEmpty ? "" : "\n" + r.text)
            r.failed = true
            return r
        }
        try log(record)
        try await Task.sleep(for: .seconds(cfg.settleS))
        var r = try await reread(c)
        r.text = [said, r.text].filter { !$0.isEmpty }.joined(separator: "\n")
        r.step = Self.step(a, element)
        return r
    }

    /// The same element across two reads: everything but its row.
    static func same(_ a: ElementIdentity, _ b: ElementIdentity) -> Bool {
        a.role == b.role && a.subrole == b.subrole && a.axID == b.axID && a.label == b.label && a.title == b.title && a.path == b.path
    }

    static func describe(_ e: ElementIdentity) -> String {
        let name = e.label.flatMap { $0.isEmpty ? nil : $0 } ?? e.title.flatMap { $0.isEmpty ? nil : $0 } ?? e.axID
        return e.role + (name.map { " \"\($0)\"" } ?? "")
    }

    /// One piece of the item's action list: what was done to what, no row numbers.
    static func step(_ a: Act, _ e: ElementIdentity?) -> String {
        let on = e.map { " " + describe($0) } ?? ""
        switch a {
        case .click(_, let x, let y, let button, let count):
            let how = (button == "left" ? "" : " \(button)") + (count == 2 ? " 雙擊" : "")
            return "click" + (e == nil ? " \(x ?? 0),\(y ?? 0)" : on) + how
        case .type(let text, _): return "type「\(text)」" + (e == nil ? "" : " 進" + on)
        case .key(let key, let mods): return "key " + (mods + [key]).joined(separator: "+")
        case .scroll(let direction, let amount, _): return "scroll \(direction) \(amount)" + on
        case .menu(_, let path): return "menu " + path.joined(separator: " › ")
        }
    }

    static func toolName(_ a: Act) -> String {
        switch a {
        case .click: "click"
        case .type: "type"
        case .key: "key"
        case .scroll: "scroll"
        case .menu: "menu"
        }
    }

    /// What she needs from a success: only that the driver could not confirm it (her next read shows
    /// what happened), not its bookkeeping.
    static func effect(_ j: [String: Any]) -> String {
        guard let e = j["effect"] as? String else { return "" }
        return e == "unverifiable" ? "（送出了，cua-driver 沒法確認有沒有作用；看下面這次讀到的視窗）" : ""
    }

    /// After an action: the same window if it is still there, else the app's front window, else why none.
    func reread(_ c: Read) async throws -> MacResult {
        let wins = try await windows(pid: c.pid)
        if let same = wins.first(where: { $0.id == c.windowID }) {
            return try await read(pid: c.pid, app: c.app, window: same, of: wins.count)
        }
        if let front = wins.first {
            var r = try await read(pid: c.pid, app: c.app, window: front, of: wins.count)
            r.window = "（剛才那個視窗「\(c.title)」不見了，這是 \(c.app.name) 現在最前面的視窗）\n" + (r.window ?? "")
            return r
        }
        current = nil
        var r = MacResult(text: "\(c.app.name) 現在沒有視窗了（剛才的「\(c.title)」關了）。", image: nil, window: nil)
        r.app = c.app
        if let auth = await authLine() { r.text += "\n" + auth }
        return r
    }

    /// `menu`: an app-menu item by its labels, top level first, through accessibility in the
    /// background. Works on an app with no window (TextEdit 檔案 › 新增, 2026-11-16).
    func menu(_ app: String?, _ path: [String]) async throws -> MacResult {
        let (pid, identity) = try await target(app)
        var args: [String: Any] = ["pid": pid, "path": path, "session": session]
        let wins = try await windows(pid: pid)
        // The driver refuses a window that is not the app's key window; the front one is.
        if let c = current, c.pid == pid, wins.first?.id == c.windowID { args["window_id"] = c.windowID }
        var record = ActionRecord(ts: Date().timeIntervalSince1970, item: item, tool: "menu", app: identity.name, bundleID: identity.bundleID,
                                  window: wins.first?.title, element: nil, args: ["path": path.joined(separator: " › ")], ok: true, error: nil)
        let before = Set(wins.map(\.id))
        do { _ = try await call("invoke_menu", args) } catch {
            record.ok = false
            record.error = Self.said(error)
            try log(record)
            throw ComputerError("選單「\(path.joined(separator: " › "))」沒有執行：\(Self.said(error))")
        }
        try log(record)
        // A new window may take a moment.
        var now: [Win] = []
        for attempt in 0..<cfg.windowWaitTries {
            try await Task.sleep(for: .seconds(attempt == 0 ? cfg.settleS : cfg.windowWaitS))
            now = try await windows(pid: pid)
            if now.contains(where: { !before.contains($0.id) }) || (!now.isEmpty && !before.isEmpty) { break }
        }
        guard let front = now.first(where: { !before.contains($0.id) }) ?? now.first else {
            current = nil
            return MacResult(text: "選了「\(path.joined(separator: " › "))」，\(identity.name) 還是沒有視窗。", image: nil, window: nil,
                             app: identity, step: "menu " + path.joined(separator: " › "))
        }
        var r = try await read(pid: pid, app: identity, window: front, of: now.count)
        r.step = "menu " + path.joined(separator: " › ")
        return r
    }

    // the action log -------------------------------------------------------------
    /// Appends one line; a write that fails throws (the action already happened: she is told the log missed it).
    func log(_ r: ActionRecord) throws {
        let enc = JSONEncoder()
        enc.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        var line = try enc.encode(r)
        line.append(0x0A)
        if !FileManager.default.fileExists(atPath: logURL.path) {
            guard FileManager.default.createFile(atPath: logURL.path, contents: nil) else {
                throw ComputerError("動作紀錄寫不進 \(logURL.path)")
            }
        }
        let h = try FileHandle(forWritingTo: logURL)
        defer { try? h.close() }
        try h.seekToEnd()
        try h.write(contentsOf: line)
    }

    /// `~` and `~/…` are the owner's home; a relative path is under it.
    public static func expand(_ path: String) -> String {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        if path == "~" { return home }
        if path.hasPrefix("~/") { return home + path.dropFirst() }
        return path.hasPrefix("/") ? path : home + "/" + path
    }
}
