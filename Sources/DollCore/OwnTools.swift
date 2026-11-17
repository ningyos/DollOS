import Foundation

/// Her own tools (owner, 2026-11-16 and 2026-11-17): a chain of actions she actually performed,
/// saved under a name she gives, and replayed in one call. The program builds it from what she did
/// (the `open` and `act` calls of an item, with the element each step reached), never from text she
/// writes; what is kept of an element is its name and role, not its row. One file per tool under
/// `<data>/tools/`, plain JSON with sorted keys so it diffs line by line.
///
/// Rules (owner): it takes effect at once, with a record; only a chain that ran through and whose
/// result matched her stated expectation is saved; each tool counts its runs and successes, and one
/// that failed `[computer] own_tool_retire_failures` times in a row is no longer offered or run (it
/// stays, and the owner sees it); at most `[computer] own_tools_max` tools. A replay goes through
/// `Mac.act` like her own steps: the checks between steps, names resolved by the program with two
/// matches refused, the system-authentication stop, and her expectation checked at the end.
public struct OwnTool: Codable, Sendable, Equatable {
    /// A part that varies: its value is given at each run.
    public struct Param: Codable, Sendable, Equatable {
        public enum Kind: String, Codable, Sendable {
            /// Text she typed (a `type` step's text, or a piece of it).
            case text
            /// The file or URL an `open` handed to the app.
            case target
            /// The names of buttons pressed one after another, one character each (Calculator's
            /// digits): the value 「58」 presses 「5」 then 「8」.
            case buttons
        }
        public var name: String
        public var kind: Kind
        /// Her plain description of it.
        public var about: String
        /// The value it had in the run the tool was saved from.
        public var example: String
    }

    /// An element as a step finds it again: its name (label, title or AX id) and role. No name: the
    /// one element of its role in the window (its only name changed every time, or it had none).
    public struct Element: Codable, Sendable, Equatable {
        public var name: String?
        public var role: String?
        /// The AX identifier the element had when saved (for the reader; finding goes by name).
        public var axID: String?
        enum CodingKeys: String, CodingKey { case name, role, axID = "ax_id" }
    }

    /// One step of an `act` part; exactly one of click / press / type / key / scroll / menu is set.
    public struct Step: Codable, Sendable, Equatable {
        public var click: Element?
        public var count: Int?
        public var button: String?
        /// `{{名字}}`: each character of that parameter's value is the name of a button to press, in order.
        public var press: String?
        /// The role the pressed buttons have.
        public var role: String?
        public var type: String?
        public var into: Element?
        public var key: String?
        public var modifiers: [String]?
        public var scroll: String?
        public var amount: Int?
        public var menu: [String]?
        /// A menu step's app (bundle id).
        public var app: String?
        enum CodingKeys: String, CodingKey { case click, count, button, press, role, type, into = "in", key, modifiers, scroll, amount, menu, app }
        init(click: Element? = nil, count: Int? = nil, button: String? = nil, press: String? = nil, role: String? = nil, type: String? = nil,
             into: Element? = nil, key: String? = nil, modifiers: [String]? = nil, scroll: String? = nil, amount: Int? = nil,
             menu: [String]? = nil, app: String? = nil) {
            (self.click, self.count, self.button, self.press, self.role, self.type, self.into) = (click, count, button, press, role, type, into)
            (self.key, self.modifiers, self.scroll, self.amount, self.menu, self.app) = (key, modifiers, scroll, amount, menu, app)
        }
    }

    /// One call of the chain: an `open` (`open` = the app's name) or an `act` (`act` = its steps).
    public struct Part: Codable, Sendable, Equatable {
        public var open: String?
        public var target: String?
        public var act: [Step]?
        /// The app the part worked on: its name and bundle id.
        public var app: String
        public var bundleID: String
        /// The text the window showed after this part, as she expected it (`{{名字}}` = a parameter's value).
        public var expect: String?
        enum CodingKeys: String, CodingKey { case open, target, act, app, bundleID = "bundle_id", expect }
    }

    public struct Origin: Codable, Sendable, Equatable {
        /// The todo item the chain was done for, and the owner's line it answered.
        public var item: String
        public var task: String
        /// The `act` sequence ids (as in `computer-actions.jsonl`).
        public var sequences: [String]
        public var saved: String
    }

    public var name: String
    /// Her one line: when to use it.
    public var when: String
    public var params: [Param]
    public var parts: [Part]
    /// The last part's expectation was a fixed text that does not follow the parameters (Calculator's
    /// 「1,702」): she states what she expects at each run; this is what it was when saved.
    public var expectWas: String?
    public var origin: Origin
    public var uses: Int
    public var successes: Int
    public var failuresInRow: Int
    public var lastRun: String?
    enum CodingKeys: String, CodingKey {
        case name, when, params, parts, expectWas = "expect_was", origin, uses, successes, failuresInRow = "failures_in_row", lastRun = "last_run"
    }

    /// No longer offered or run: failed `limit` times in a row.
    public func retired(_ limit: Int) -> Bool { failuresInRow >= limit }

    static func placeholder(_ name: String) -> String { "{{\(name)}}" }

    /// The steps as one line each, for her and the dashboard (`open 計算機`, `按 {{a}}（AXButton）`, `click AXButton "乘"`).
    public var stepLines: [String] {
        var out: [String] = []
        for p in parts {
            if let app = p.open {
                out.append("open \(app)" + (p.target.map { " \($0)" } ?? ""))
                continue
            }
            for s in p.act ?? [] { out.append(Self.line(s)) }
            if let e = p.expect { out.append("（預期視窗裡有「\(e)」）") }
        }
        return out
    }

    static func line(_ s: Step) -> String {
        func el(_ e: Element) -> String { e.name.map { (e.role.map { "\($0) " } ?? "") + "\"\($0)\"" } ?? "唯一的 \(e.role ?? "?")" }
        if let e = s.click { return "click " + el(e) + (s.button.map { " \($0)" } ?? "") + (s.count == 2 ? " 雙擊" : "") }
        if let p = s.press { return "按 \(p) 的每一個字" + (s.role.map { "（\($0)）" } ?? "") }
        if let t = s.type { return "type「\(t)」" + (s.into.map { " 進 " + el($0) } ?? "") }
        if let k = s.key { return "key " + ((s.modifiers ?? []) + [k]).joined(separator: "+") }
        if let d = s.scroll { return "scroll \(d) \(s.amount ?? 3)" + (s.into.map { " " + el($0) } ?? "") }
        if let m = s.menu { return "menu " + m.joined(separator: " › ") }
        return "?"
    }

    /// One line for the 【相關記憶】 offer and for listing.
    public var offerLine: String {
        let ps = params.isEmpty ? "沒有參數" : "參數 " + params.map { "\($0.name)（\($0.about)；存的時候是「\($0.example)」）" }.joined(separator: "、")
        let args = params.isEmpty ? "{}" : "{" + params.map { "\"\($0.name)\": …" }.joined(separator: ", ") + "}"
        let expect = expectWas == nil ? "" : "，要給 expect"
        return "「\(name)」：\(when)（\(ps)；用了 \(uses) 次，成功 \(successes) 次）。用法：tool.run(name: \"\(name)\", args: \(args)\(expect))"
    }
}

/// Her tools on disk: `<data>/tools/<name>.json`. Read from disk at every use, so a restart (or the
/// owner deleting one from the dashboard) is seen at once.
public struct ToolStore: Sendable {
    public let dir: URL
    public init(dir: URL) { self.dir = dir }

    func url(_ name: String) -> URL { dir.appending(path: name + ".json") }

    public func all() throws -> [OwnTool] {
        guard FileManager.default.fileExists(atPath: dir.path) else { return [] }
        let files = try FileManager.default.contentsOfDirectory(atPath: dir.path).filter { $0.hasSuffix(".json") }.sorted()
        return try files.map { f in
            do { return try JSONDecoder().decode(OwnTool.self, from: Data(contentsOf: dir.appending(path: f))) } catch {
                throw ToolError(description: "工具檔 \(f) 讀不懂：\(error)")
            }
        }
    }

    public func get(_ name: String) throws -> OwnTool? {
        let u = url(name)
        guard FileManager.default.fileExists(atPath: u.path) else { return nil }
        do { return try JSONDecoder().decode(OwnTool.self, from: Data(contentsOf: u)) } catch {
            throw ToolError(description: "工具檔 \(u.lastPathComponent) 讀不懂：\(error)")
        }
    }

    public func write(_ t: OwnTool) throws {
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let enc = JSONEncoder()
        enc.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        var data = try enc.encode(t)
        data.append(0x0A)
        try data.write(to: url(t.name), options: .atomic)
    }

    /// True when there was such a tool.
    public func delete(_ name: String) throws -> Bool {
        guard OwnTools.validName(name) == nil else { return false }
        let u = url(name)
        guard FileManager.default.fileExists(atPath: u.path) else { return false }
        try FileManager.default.removeItem(at: u)
        return true
    }
}

/// One `open` or `act` call of the item, as it went: the material a tool is built from.
struct ChainPart: Sendable {
    enum Kind: Sendable { case open, act }
    var kind: Kind
    var app: AppIdentity
    var target: String?
    var steps: [Mac.Step]
    var records: [ActionRecord]
    var expect: String?
    /// Went through: an `open` that answered; an `act` whose steps all ran and whose expectation (if any) was in the window.
    var ok: Bool
    var sequence: String?
}

/// A parameter as she gives it to `tool.save`: its name, the value it had, and what it is.
struct ParamInput: Decodable, Sendable, Equatable {
    let name: String
    let value: String
    let about: String
}

/// A tool bound to this run's values: what `Mac.replay` runs.
struct BoundPart: Sendable {
    var open: String?
    var target: String?
    var steps: [Mac.Step]
    var app: String
    var bundleID: String
    var expect: String?
    /// The button presses a value expanded into: the step's index in `steps`, its name, the parameter and its value.
    var presses: [(index: Int, name: String, role: String?, param: String, value: String)]
}

enum OwnTools {
    /// A name she can give a tool: it is also its file's name.
    static func validName(_ name: String) -> String? {
        guard !name.isEmpty, name.count <= 40,
              name.allSatisfy({ $0.isLetter || $0.isNumber || $0 == "-" || $0 == "_" }) else {
            return "工具的名字只能用字、數字、- 和 _（不能有空白、標點、斜線），最多 40 個字；收到「\(name)」"
        }
        return nil
    }

    // building -----------------------------------------------------------------------
    /// The tool her chain makes, or why it cannot be saved (her words). Kept: every `open` that
    /// answered and every `act` that went through, in order; left out: reads (they change nothing),
    /// `act`s that failed or did not match her expectation (attempts she corrected afterwards).
    /// The chain must end in an `act` that went through with a stated expectation that matched.
    static func build(name: String, when: String, params: [ParamInput], chain: [ChainPart], item: String, task: String,
                      saved: String) throws -> (tool: OwnTool, left: [String]) {
        if let bad = validName(name) { throw ToolError(description: "沒存：" + bad) }
        let acts = chain.filter { $0.kind == .act }
        guard let last = acts.last else {
            throw ToolError(description: "沒存：「\(item)」裡沒有做成的 act。工具只能從真的做過、結果跟你預期一樣的動作存；先做一次，再存。")
        }
        guard last.ok else {
            throw ToolError(description: "沒存：「\(item)」最後一次 act（\(last.steps.map { Mac.step($0, nil) }.joined(separator: "、"))）沒做完或跟你預期的不一樣，這串動作沒有完整做成。做到跟預期一樣以後再存。")
        }
        guard let finalExpect = last.expect else {
            throw ToolError(description: "沒存：「\(item)」最後一次 act 沒寫 expect，程式沒辦法確認那次結果是對的。用寫了 expect 的 act 做到一樣，再存。")
        }
        var left: [String] = []
        var parts: [OwnTool.Part] = []
        var n = 0
        var actNo = 0
        for c in chain {
            switch c.kind {
            case .open:
                guard c.ok else { continue }
                n += 1
                // Two opens of the same app and target in a row are one.
                if let p = parts.last, p.open != nil, p.bundleID == c.app.bundleID, p.target == c.target { n -= 1; continue }
                parts.append(OwnTool.Part(open: c.app.name, target: c.target, act: nil, app: c.app.name, bundleID: c.app.bundleID, expect: nil))
            case .act:
                actNo += 1
                // Ran through but not as she expected: the whole act is an attempt she corrected, left
                // out. Stopped at a step (a refusal, a name not found): the steps before it went
                // through and changed the screen the next act built on, so they are kept.
                let failedAt = c.records.firstIndex { !$0.ok } ?? c.records.count
                let partial = !c.ok && failedAt < c.steps.count
                let keep = c.ok ? c.steps.count : partial ? failedAt : 0
                if !c.ok {
                    let all = c.steps.map { Mac.step($0, nil) }.joined(separator: "、")
                    left.append(partial
                        ? "第 \(actNo) 次 act 從第 \(failedAt + 1) 步（\(Mac.step(c.steps[failedAt], nil))）起沒做成，那幾步沒存" + (failedAt > 0 ? "（前面 \(failedAt) 步做了，存了）" : "")
                        : "第 \(actNo) 次 act（\(all)）做完了可是跟你預期的不一樣，整次沒存")
                }
                guard keep > 0 else { continue }
                guard c.records.count >= keep else {
                    throw ToolError(description: "沒存：第 \(actNo) 次 act 的動作紀錄少了步驟（\(c.records.count)／\(c.steps.count)），程式不知道每一步按到哪個元素。")
                }
                var steps: [OwnTool.Step] = []
                for (s, r) in zip(c.steps.prefix(keep), c.records.prefix(keep)) {
                    n += 1
                    steps.append(try OwnTools.saved(s, r, n: n))
                }
                parts.append(OwnTool.Part(open: nil, target: nil, act: steps, app: c.app.name, bundleID: c.app.bundleID, expect: c.ok ? c.expect : nil))
            }
        }
        // The parameters: each value must be found in exactly one kind of place.
        var out: [OwnTool.Param] = []
        var names = Set<String>()
        for p in params {
            let pname = p.name.trimmingCharacters(in: .whitespaces)
            guard !pname.isEmpty, !pname.contains("{"), !pname.contains("}"), !pname.contains("\"") else {
                throw ToolError(description: "沒存：參數的名字不能是空的，也不能有 { } 或引號；收到「\(p.name)」")
            }
            guard names.insert(pname).inserted else { throw ToolError(description: "沒存：參數「\(pname)」寫了兩次") }
            guard !p.value.isEmpty else { throw ToolError(description: "沒存：參數「\(pname)」的 value 是空的；寫它在這次做的時候的值（例：37）") }
            let about = p.about.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !about.isEmpty else { throw ToolError(description: "沒存：參數「\(pname)」要寫 about：它是什麼（例：第一個數）") }
            let ph = OwnTool.placeholder(pname)
            var typed: [(Int, Int)] = [], opened: [Int] = [], runs: [(part: Int, from: Int, to: Int)] = []
            for (pi, part) in parts.enumerated() {
                if part.open != nil, part.target == p.value { opened.append(pi) }
                let steps = part.act ?? []
                for (si, s) in steps.enumerated() {
                    if let t = s.type, t.contains(p.value) { typed.append((pi, si)) }
                }
                // Consecutive one-character button clicks whose names spell the value.
                for from in steps.indices {
                    var spelled = "", to = from
                    while to < steps.count, let e = steps[to].click, steps[to].count == nil, steps[to].button == nil, let name = e.name, name.count == 1,
                          e.role == steps[from].click?.role, spelled.count < p.value.count {
                        spelled += name
                        to += 1
                    }
                    if spelled == p.value { runs.append((pi, from, to)) }
                }
            }
            let kinds = [typed.isEmpty ? nil : "打的字", opened.isEmpty ? nil : "開的檔案或網址", runs.isEmpty ? nil : "連著按的按鈕"].compactMap { $0 }
            guard !kinds.isEmpty else {
                let all = parts.flatMap { OwnTool(name: "", when: "", params: [], parts: [$0], expectWas: nil,
                                                   origin: OwnTool.Origin(item: "", task: "", sequences: [], saved: ""),
                                                   uses: 0, successes: 0, failuresInRow: 0).stepLines }
                throw ToolError(description: "沒存：參數「\(pname)」的值「\(p.value)」不在要存的步驟裡（打的字、open 的檔案或網址、連著按的按鈕名字都沒有）。要存的步驟：" + all.joined(separator: "、"))
            }
            guard kinds.count == 1, runs.count <= 1 else {
                throw ToolError(description: "沒存：參數「\(pname)」的值「\(p.value)」在步驟裡不只一處（\(kinds.joined(separator: "、"))\(runs.count > 1 ? "，按鈕有 \(runs.count) 串" : "")），程式不知道哪一處會變。")
            }
            let kind: OwnTool.Param.Kind
            if !typed.isEmpty {
                kind = .text
                for (pi, si) in typed { parts[pi].act![si].type = parts[pi].act![si].type!.replacingOccurrences(of: p.value, with: ph) }
            } else if !opened.isEmpty {
                kind = .target
                for pi in opened { parts[pi].target = ph }
            } else {
                kind = .buttons
                let r = runs[0]
                let role = parts[r.part].act![r.from].click?.role
                parts[r.part].act!.replaceSubrange(r.from..<r.to, with: [OwnTool.Step(press: ph, role: role)])
            }
            // An expectation that shows typed text or a file's name follows it; pressed buttons do not.
            if kind != .buttons {
                for pi in parts.indices { parts[pi].expect = parts[pi].expect?.replacingOccurrences(of: p.value, with: ph) }
            }
            out.append(OwnTool.Param(name: pname, kind: kind, about: about, example: p.value))
        }
        var expectWas: String?
        if !out.isEmpty, let i = parts.lastIndex(where: { $0.act != nil }), let e = parts[i].expect, !e.contains("{{") {
            // A fixed text that does not follow the parameters (1,702 for 37×46): stated at each run.
            expectWas = e
            parts[i].expect = nil
        }
        _ = finalExpect
        let tool = OwnTool(name: name, when: when, params: out, parts: parts, expectWas: expectWas,
                           origin: OwnTool.Origin(item: item, task: task, sequences: chain.compactMap { $0.kind == .act && $0.ok ? $0.sequence : nil }, saved: saved),
                           uses: 0, successes: 0, failuresInRow: 0, lastRun: nil)
        return (tool, left)
    }

    /// One step as the tool keeps it: what it reached, by name and role. A name that is different
    /// every time (an AX id with a UUID, Notes' note body `Note[id=…]`: replayed, it would reach the
    /// old note) is not kept; an element left without a name is kept by its role alone.
    static func saved(_ s: Mac.Step, _ r: ActionRecord, n: Int) throws -> OwnTool.Step {
        func stable(_ e: ElementIdentity?, _ given: String?) -> String? {
            ([given] + [e?.label, e?.title, e?.axID]).compactMap { $0 }.first { !$0.isEmpty && !$0.contains(/[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}/) }
        }
        func element(_ t: Mac.Target) throws -> OwnTool.Element {
            switch t {
            case .point(let x, let y):
                throw ToolError(description: "沒存：第 \(n) 步點的是截圖上的點（\(x),\(y)），換了畫面就找不回來；工具只存用名字找得到的元素。這一步用名字再做一次，再存。")
            case .named(let name, let role):
                return OwnTool.Element(name: stable(r.element, name), role: r.element?.role ?? role, axID: r.element?.axID)
            case .row(let row):
                guard let e = r.element else {
                    throw ToolError(description: "沒存：第 \(n) 步按的是第 \(row) 行，動作紀錄裡沒有那個元素是什麼。")
                }
                return OwnTool.Element(name: stable(e, nil), role: e.role, axID: e.axID)
            }
        }
        switch s {
        case .click(let t, let button, let count):
            return OwnTool.Step(click: try element(t), count: count == 2 ? 2 : nil, button: button == "left" ? nil : button)
        case .type(let text, let t):
            return OwnTool.Step(type: text, into: try t.map(element))
        case .key(let key, let mods):
            return OwnTool.Step(key: key, modifiers: mods.isEmpty ? nil : mods)
        case .scroll(let d, let amount, let t):
            return OwnTool.Step(into: try t.map(element), scroll: d, amount: amount)
        case .menu(_, let path):
            return OwnTool.Step(menu: path, app: r.bundleID)
        }
    }

    // binding -------------------------------------------------------------------------
    /// The tool with this run's values, or why it cannot run (nothing has been done yet). `expect`:
    /// hers for this run; it replaces the saved one, and is needed when the saved one was fixed.
    static func bind(_ t: OwnTool, args: [String: String], expect: String?, maxSteps: Int) throws -> (parts: [BoundPart], expect: String) {
        let need = t.params.map(\.name)
        let usage = t.params.map { "\($0.name)：\($0.about)" }.joined(separator: "；")
        if let extra = args.keys.sorted().first(where: { !need.contains($0) }) {
            throw ToolError(description: "沒有做：工具「\(t.name)」沒有參數「\(extra)」；" + (need.isEmpty ? "它沒有參數，args 給 {}。" : "它的參數：\(usage)。"))
        }
        if let missing = need.first(where: { args[$0] == nil }) {
            throw ToolError(description: "沒有做：工具「\(t.name)」少了參數「\(missing)」；它的參數：\(usage)。")
        }
        if let empty = need.first(where: { args[$0]!.isEmpty }) {
            throw ToolError(description: "沒有做：工具「\(t.name)」的參數「\(empty)」是空的。")
        }
        func fill(_ s: String) -> String {
            var s = s
            for p in t.params { s = s.replacingOccurrences(of: OwnTool.placeholder(p.name), with: args[p.name]!) }
            return s
        }
        func target(_ e: OwnTool.Element?) -> Mac.Target? { e.map { .named(name: $0.name ?? "", role: $0.role) } }
        var out: [BoundPart] = []
        let lastAct = t.parts.lastIndex { $0.act != nil }
        for (pi, p) in t.parts.enumerated() {
            if let app = p.open {
                out.append(BoundPart(open: app, target: p.target.map(fill), steps: [], app: p.app, bundleID: p.bundleID, expect: nil, presses: []))
                continue
            }
            var steps: [Mac.Step] = []
            var presses: [(index: Int, name: String, role: String?, param: String, value: String)] = []
            for s in p.act ?? [] {
                if let e = s.click {
                    steps.append(.click(target(e)!, button: s.button ?? "left", count: s.count ?? 1))
                } else if let ph = s.press {
                    guard let param = t.params.first(where: { OwnTool.placeholder($0.name) == ph }) else {
                        throw ToolError(description: "沒有做：工具「\(t.name)」的檔案壞了：\(ph) 不是它的參數。")
                    }
                    let value = args[param.name]!
                    for ch in value {
                        presses.append((steps.count, String(ch), s.role, param.name, value))
                        steps.append(.click(.named(name: String(ch), role: s.role), button: "left", count: 1))
                    }
                } else if let text = s.type {
                    steps.append(.type(text: fill(text), into: target(s.into)))
                } else if let key = s.key {
                    steps.append(.key(key: key, modifiers: s.modifiers ?? []))
                } else if let d = s.scroll {
                    steps.append(.scroll(direction: d, amount: s.amount ?? 3, at: target(s.into)))
                } else if let path = s.menu {
                    steps.append(.menu(app: s.app ?? p.bundleID, path: path))
                } else {
                    throw ToolError(description: "沒有做：工具「\(t.name)」的檔案壞了：有一步沒有 click、press、type、key、scroll、menu。")
                }
            }
            guard steps.count <= maxSteps else {
                throw ToolError(description: "沒有做：這次的值讓工具「\(t.name)」的一段變成 \(steps.count) 步，一次最多 \(maxSteps) 步；值短一點，或自己用 act 分段做。")
            }
            let e = pi == lastAct ? (expect ?? p.expect.map(fill)) : p.expect.map(fill)
            out.append(BoundPart(open: nil, target: nil, steps: steps, app: p.app, bundleID: p.bundleID, expect: e, presses: presses))
        }
        guard let li = lastAct, let final = out[li].expect else {
            throw ToolError(description: "沒有做：工具「\(t.name)」的結果跟著值變，要給 expect：這次做完視窗裡會出現的字，照畫面的寫法"
                            + (t.expectWas.map { "（存的那次是「\($0)」）" } ?? "") + "。")
        }
        return (out, final)
    }
}

extension Mac {
    /// Runs a bound tool (`tool.run`): each `open` as `open` does, each `act` part through `act`
    /// (so every guard of a step done by hand holds), numbered on from the parts before it. Before an
    /// `act` part: the app's window is read when the latest read is another app's, and every button a
    /// value expands into must be named exactly once in that read, or the part is not started.
    /// An expectation of a part before the last is checked here; the last one is the caller's.
    /// Returns the result and the parts as they went, for her chain.
    func replay(_ name: String, _ parts: [BoundPart]) async -> (MacResult, [ChainPart]) {
        running = name
        defer { running = nil }
        var done: [String] = []
        var chain: [ChainPart] = []
        var last: MacResult?
        var n = 0
        func stop(_ why: String) async -> (MacResult, [ChainPart]) {
            var r: MacResult
            if let l = last, l.window != nil { r = l } else {
                do { r = try await look() } catch { r = MacResult(text: "", image: nil, window: nil) }
            }
            r.text = "工具「\(name)」停了：\(why)" + (done.isEmpty ? "" : "\n做過的：" + done.joined(separator: "、") + "。")
            r.failed = true
            r.steps = done
            return (r, chain)
        }
        for (pi, p) in parts.enumerated() {
            if let app = p.open {
                n += 1
                do {
                    let r = try await open(p.bundleID.isEmpty ? app : p.bundleID, target: p.target)
                    last = r
                    done += r.steps
                    chain.append(ChainPart(kind: .open, app: r.app ?? AppIdentity(bundleID: p.bundleID, name: app, version: ""), target: p.target,
                                           steps: [], records: r.records, expect: nil, ok: true, sequence: nil))
                } catch {
                    return await stop("第 \(n) 步（open \(app)）沒做成：\(Self.said(error))")
                }
                continue
            }
            // A part that starts with a menu step reaches its app itself (the app may have no window yet).
            let menuFirst: Bool = { if case .menu = p.steps.first { return true } else { return false } }()
            if current?.app.bundleID != p.bundleID, !menuFirst {
                do { last = try await readWindow(p.bundleID, window: nil) } catch {
                    return await stop("第 \(n + 1) 步要在 \(p.app) 做，讀不到它的視窗：\(Self.said(error))")
                }
            }
            if let c = current, c.app.bundleID == p.bundleID, !p.presses.isEmpty {
                let rows = c.rows
                for press in p.presses {
                    let hits = Self.match(press.name, role: press.role, in: rows)
                    guard hits.count == 1 else {
                        let what = hits.isEmpty
                            ? "\(c.app.name)「\(c.title)」裡沒有名字叫「\(press.name)」\(press.role.map { "的 \($0)" } ?? "的元素")" + Self.near(press.name, in: rows)
                            : "\(c.app.name)「\(c.title)」裡名字叫「\(press.name)」的有 \(hits.count) 個：" + hits.map(Self.candidate).joined(separator: "、")
                        return await stop("第 \(n + press.index + 1) 步要按「\(press.name)」（參數 \(press.param) 的值「\(press.value)」裡的字），可是\(what)。這一段（第 \(n + 1)–\(n + p.steps.count) 步）一步都沒按。")
                    }
                }
            }
            let r: MacResult
            do { r = try await act(p.steps, numberFrom: n + 1) } catch {
                return await stop("第 \(n + 1) 步沒做成：\(Self.said(error))")
            }
            last = r
            done += r.steps
            let isLast = !parts[(pi + 1)...].contains { $0.open == nil }
            var ok = !r.failed
            var mismatch: String?
            if ok, !isLast, let e = p.expect {
                let lines = (r.window ?? "").split(separator: "\n", omittingEmptySubsequences: false).dropFirst().joined(separator: "\n")
                if !Doll.quoted(e, in: lines) {
                    ok = false
                    mismatch = "做完第 \(n + 1)–\(n + p.steps.count) 步，預期視窗裡有「\(e)」，讀到的沒有：\n"
                        + Doll.valueLines(lines.split(separator: "\n").map(String.init))
                }
            }
            chain.append(ChainPart(kind: .act, app: r.app ?? AppIdentity(bundleID: p.bundleID, name: p.app, version: ""), target: nil,
                                   steps: p.steps, records: r.records, expect: p.expect, ok: ok, sequence: r.records.first?.sequence))
            if r.failed { return await stop(r.text) }
            if let mismatch { return await stop(mismatch) }
            n += p.steps.count
        }
        var r = last ?? MacResult(text: "", image: nil, window: nil)
        // The last read's own lines after the act's first (a system-authentication warning) stay.
        let extra = r.text.split(separator: "\n", omittingEmptySubsequences: false).dropFirst().joined(separator: "\n")
        r.text = "工具「\(name)」\(n) 步都做了：" + done.joined(separator: "、") + "。" + (extra.isEmpty ? "" : "\n" + extra)
        r.steps = done
        r.failed = false
        return (r, chain)
    }
}

extension Doll {
    /// The owner's (or developer's) last line before her reply: what a chain was done for.
    func ownerTask(before reply: Event, or item: String) -> String {
        timeline.events.last { $0.speaker != nil && $0.seq! < reply.seq! }?.text ?? item
    }

    static func stamp(_ date: Date = Date()) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd HH:mm:ss"
        return f.string(from: date)
    }

    static func kindWords(_ k: OwnTool.Param.Kind) -> String {
        switch k {
        case .text: "打的字"
        case .target: "open 的檔案或網址"
        case .buttons: "按的按鈕，一個字一個按鈕"
        }
    }

    /// `tool.save` (2026-11-17): the item in progress when it has an `act` of its own, else the
    /// item that ended last; refused when that item failed, when nothing went through, or as
    /// `OwnTools.build` says. Takes effect at once (owner decision 1); the file is the record.
    func toolSave(_ name: String, when: String, params: [ParamInput], reply: Event) throws -> Outcome {
        let source: (item: String, task: String, chain: [ChainPart])
        if todo.phase == .doing, let item = todo.current, itemChain.contains(where: { $0.kind == .act }) {
            source = (item, ownerTask(before: reply, or: item), itemChain)
        } else if let e = lastEnded {
            guard e.done else {
                throw ToolError(description: "沒存：上一項「\(e.item)」是用 todo.fail 收的；沒做成的事不能存成工具。")
            }
            source = (e.item, e.task, e.chain)
        } else {
            throw ToolError(description: "沒存：沒有可以存的動作。進行中的項目還沒有做成的 act，之前也沒有做完的項目（程式重開以前做的不算）。先做一次、做到跟預期一樣，再存。")
        }
        let existing = try toolStore.all()
        if let same = existing.first(where: { $0.name == name }) {
            throw ToolError(description: "沒存：已經有叫「\(name)」的工具了（\(same.when)）。換一個名字；要換掉它，請主人在 dashboard 刪掉舊的。")
        }
        guard existing.count < computerConfig.ownToolsMax else {
            throw ToolError(description: "沒存：工具已經有 \(existing.count) 個，到上限 \(computerConfig.ownToolsMax) 個了。請主人在 dashboard 刪掉用不到的。")
        }
        let (tool, left) = try OwnTools.build(name: name, when: when, params: params, chain: source.chain, item: source.item,
                                              task: source.task, saved: Self.stamp())
        try toolStore.write(tool)
        log("INFO", "turn \(turnIndex): tool.save「\(name)」from「\(source.item)」: \(tool.stepLines.count) lines, params \(tool.params.map { "\($0.name)=\($0.kind.rawValue)" }), left out \(left.count)")
        let ps = tool.params.isEmpty ? "沒有參數" : "參數：" + tool.params.map { "\($0.name)（\(Self.kindWords($0.kind))，這次是「\($0.example)」）" }.joined(separator: "、")
        let expect = tool.expectWas.map { "結果跟著值變（這次是「\($0)」），每次用的時候要給 expect。" }
            ?? (tool.parts.last(where: { $0.act != nil })?.expect.map { "做完會對「\($0)」。" } ?? "")
        let args = tool.params.isEmpty ? "" : ", args: {" + tool.params.map { "\"\($0.name)\": …" }.joined(separator: ", ") + "}"
        return Outcome(text: "存好了：工具「\(name)」（\(toolStore.dir.lastPathComponent)/\(name).json），從「\(source.item)」存的。步驟：" + tool.stepLines.joined(separator: "、")
                       + "。\(ps)。\(expect)" + (left.isEmpty ? "" : "沒存進去的：" + left.joined(separator: "；") + "。")
                       + "之後用 tool.run(name: \"\(name)\"\(args)\(tool.expectWas == nil ? "" : ", expect: …")) 叫它。",
                       actions: ["tool save"])
    }

    /// `tool.run` (2026-11-17): one call, one round; refused before anything is done when the tool
    /// is unknown, retired or its values do not fit. The replay's result is an `act`'s result, its
    /// expectation checked the same way; its parts join the item's chain; its counts are written.
    func toolRun(_ name: String, args: [String: String], expect: String?, chain: Chain) async throws -> Outcome {
        let limit = computerConfig.ownToolRetireFailures
        guard var tool = try toolStore.get(name) else {
            let have = try toolStore.all().filter { !$0.retired(limit) }
            throw ToolError(description: "沒有做：沒有叫「\(name)」的工具。" + (have.isEmpty ? "你還沒有存任何工具。" : "你的工具：" + have.map(\.offerLine).joined(separator: "；")))
        }
        guard !tool.retired(limit) else {
            throw ToolError(description: "沒有做：工具「\(name)」連續失敗 \(tool.failuresInRow) 次，停用了（沒刪，主人在 dashboard 看得到）。這次用 act 自己做。")
        }
        let bound = try OwnTools.bind(tool, args: args, expect: expect, maxSteps: computerConfig.sequenceMaxSteps)
        let (r, parts) = await mac.replay(tool.name, bound.parts)
        var out = computerOutcome(r, name: Tools.Name.toolRun, chain: chain, expect: r.failed ? nil : bound.expect)
        let ok = out.failedStep == nil
        var ran = parts
        if !ok, !r.failed, let i = ran.lastIndex(where: { $0.kind == .act }) { ran[i].ok = false }
        itemChain += ran
        tool.uses += 1
        if ok { tool.successes += 1; tool.failuresInRow = 0 } else { tool.failuresInRow += 1 }
        tool.lastRun = Self.stamp()
        do { try toolStore.write(tool) } catch { out.text += "\n（這次用的次數沒記上：\(error)）" }
        out.text += "\n工具「\(name)」用了 \(tool.uses) 次，成功 \(tool.successes) 次"
            + (tool.retired(limit) ? "；連續失敗 \(tool.failuresInRow) 次，停用了，之後不會再提它（主人在 dashboard 看得到）" : "") + "。"
        log("INFO", "turn \(turnIndex): tool.run「\(name)」args \(args.sorted { $0.key < $1.key }) \(ok ? "ok" : "failed") (\(tool.successes)/\(tool.uses), \(tool.failuresInRow) in a row)")
        return out
    }

    /// The tools to tell her of for this query: not retired, not offered since the owner's line,
    /// cosine of the query to the tool (name, `when`, the owner's line it was saved from) at least
    /// `[computer] own_tool_offer_similarity`, best first, at most `own_tool_offer_max`. A failure is
    /// logged and offers none.
    func offerTools(query: String, label: String) -> [OwnTool] {
        do {
            let limit = computerConfig.ownToolRetireFailures
            let tools = try toolStore.all().filter { !$0.retired(limit) && !offeredTools.contains($0.name) }
            guard !tools.isEmpty else { return [] }
            let q = try embedder.embed(query, as: .query)
            let docs = try embedder.embed(tools.map { "\($0.name)：\($0.when)\n\($0.origin.task)" }, as: .document)
            let scored = zip(tools, docs).map { t, d in (t, Double(zip(q, d).reduce(Float(0)) { $0 + $1.0 * $1.1 })) }
            log("INFO", "turn \(turnIndex): tools（\(label)）cosine " + scored.map { "\($0.0.name)=\(String(format: "%.3f", $0.1))" }.joined(separator: " "))
            return scored.filter { $0.1 >= computerConfig.ownToolOfferSimilarity }.sorted { $0.1 > $1.1 }
                .prefix(computerConfig.ownToolOfferMax).map(\.0)
        } catch {
            log("ERROR", "turn \(turnIndex): tools（\(label)）not offered: \(error)")
            return []
        }
    }
}

/// One of her tools as the dashboard shows it (2026-11-17, owner: he sees what each tool does).
public struct DashboardTool: Codable, Sendable, Equatable {
    public var name: String
    public var when: String
    public var params: [String]
    public var steps: [String]
    /// What a run is checked against: the saved text, or that she states it at each run.
    public var expect: String
    public var from: String
    public var saved: String
    public var uses: Int
    public var successes: Int
    public var failuresInRow: Int
    public var retired: Bool
    public var lastRun: String?
    public var file: String
}

extension Doll {
    public func dashboardTools() throws -> [DashboardTool] {
        let limit = computerConfig.ownToolRetireFailures
        return try toolStore.all().map { t in
            let last = t.parts.last { $0.act != nil }?.expect
            return DashboardTool(name: t.name, when: t.when,
                                 params: t.params.map { "\($0.name)（\(Self.kindWords($0.kind))）：\($0.about)；存的時候是「\($0.example)」" },
                                 steps: t.stepLines,
                                 expect: t.expectWas.map { "每次她說預期什麼（存的那次是「\($0)」）" } ?? last.map { "視窗裡有「\($0)」" } ?? "—",
                                 from: "「\(t.origin.item)」（主人：\(t.origin.task)）", saved: t.origin.saved, uses: t.uses, successes: t.successes,
                                 failuresInRow: t.failuresInRow, retired: t.retired(limit), lastRun: t.lastRun,
                                 file: toolStore.url(t.name).path)
        }
    }

    /// The owner deletes a tool (owner decision 1); false when there was none by that name.
    public func dashboardDeleteTool(_ name: String) throws -> Bool {
        let gone = try toolStore.delete(name)
        if gone { log("INFO", "dashboard: tool「\(name)」deleted by the owner") }
        return gone
    }
}
