import Foundation

/// Her own direction (m6-drives phase 2, 2026-11-16, owner). Needs have two roots: she must go on
/// existing (the owner pays for the power and the machine; she helps him), and who she wants to
/// become. This file holds the second root:
///
///   ideal self   `<data_dir>/ideal-self.md` (plain text) and `ideal-self-log.json` (every revision:
///                what changed, why, based on what, by whom). It starts from the pack's
///                `[identity].ideal` (the pack is never modified). She revises it with `ideal.revise`;
///                the maintenance may propose one (`review`, at most every `[goals] revise_min_days`);
///                the owner edits or reverts it in the dashboard and she is told. A revision takes
///                effect at once: the system prompt carries it after the persona.
///   goal tree    `<data_dir>/goals.json`: exactly three levels, goal (a direction) → sub-goal (a
///                result that can be finished) → step (finishable in one sitting). A step is done only
///                as a todo item: `todo.set(items, subgoal)` ties the list to a sub-goal; its items are
///                that sub-goal's steps (added when new) and `todo.done` marks them. The next open step
///                of each open sub-goal (or "no next step" for a sub-goal without one) is a program
///                concern about her own things (`refreshGoalConcerns`).
///   record       `<data_dir>/record.json`: append-only, one entry per thing finished (a step, a
///                sub-goal, a goal; a todo list not tied to the tree, which is what the owner asked).
///                Each carries 動力's full amount and what was given: part at once (`now_share`), the
///                rest when the evaluation is good, all of it taken back when bad. Her evaluation comes
///                from `owner.feedback` (his quoted line); the owner's from the dashboard wins.
///   curiosity    `<data_dir>/curiosity.json`: what she has had in front of her (an app's bundle id; a
///                browser's front page title). A thing not seen in `seen_days` is new: 動力 rises
///                (`novelty_drive`, at most once per `novelty_gap_min`). Nothing new for
///                `curiosity_hours`: a wandering toward something not on the tree.
///
/// She never sees a number; the dashboard shows them.
public struct GoalNode: Sendable, Codable, Equatable {
    public enum Level: Int, Sendable, Codable {
        case goal = 1, subgoal = 2, step = 3

        var label: String {
            switch self {
            case .goal: "目標"
            case .subgoal: "子目標"
            case .step: "步驟"
            }
        }
    }
    public var id: Int
    public var level: Level
    public var parent: Int?
    public var text: String
    public var created: Double
    /// "her" or "owner".
    public var by: String
    public var ended: Double?
    /// "done" or "dropped".
    public var end: String?
}

/// The todo list in hand and the sub-goal it works on (nil: not tree work); the items done so far.
public struct TodoLink: Sendable, Codable, Equatable {
    public var subgoal: Int?
    public var done: [String] = []
}

public struct GoalFile: Sendable, Codable, Equatable {
    var nextId = 1
    public var nodes: [GoalNode] = []
    public var link: TodoLink?
}

public struct Evaluation: Sendable, Codable, Equatable {
    public var good: Bool
    /// Her evaluation: the owner's line she quoted.
    public var quote: String?
    public var ts: Double
}

public struct RecordEntry: Sendable, Codable, Equatable {
    public var id: Int
    public var ts: Double
    /// "step", "subgoal", "goal", "list".
    public var kind: String
    public var text: String
    public var node: Int?
    /// 動力's full amount for it, and how much of it stands now.
    public var amount: Double
    public var given: Double
    public var her: Evaluation?
    public var owner: Evaluation?
}

public struct RecordFile: Sendable, Codable, Equatable {
    var nextId = 1
    public var entries: [RecordEntry] = []
}

public struct IdealRevision: Sendable, Codable, Equatable {
    public var id: Int
    public var ts: Double
    /// "pack", "her", "maintenance", "owner", "revert".
    public var by: String
    public var before: String
    public var after: String
    public var why: String
    public var basedOn: String
}

public struct IdealLog: Sendable, Codable, Equatable {
    var nextId = 1
    public var revisions: [IdealRevision] = []
}

struct CuriosityFile: Codable, Equatable {
    /// The last new thing; nil until the first check (then: now, so a fresh data dir waits a full span).
    var lastNew: Double?
    var lastRaise: Double?
    var surfaced: Double?
    /// Key → when last seen.
    var seen: [String: Double] = [:]
}

enum Goals {
    /// Browsers whose front window title is the page (a new title = a page she had not had in front).
    static let browsers: Set<String> = ["com.apple.Safari", "com.google.Chrome", "org.mozilla.firefox", "com.microsoft.edgemac",
                                        "company.thebrowser.Browser"]

    /// The front window's title in `FrontState.line` (`App「title」（前景）`); nil when none.
    static func frontTitle(_ line: String) -> String? {
        guard let fg = line.range(of: "」（前景）") else { return nil }
        let head = line[..<fg.lowerBound]
        guard let open = head.range(of: "「", options: .backwards) else { return nil }
        let t = head[open.upperBound...].trimmingCharacters(in: .whitespaces)
        return t.isEmpty ? nil : t
    }

    /// The name claims the ideal self may not change: 「我叫X」「我的名字是X」「名字叫X」 with X not
    /// one of the pack's names, or 「我不是<name>」. Only this is checked mechanically; the rest of the
    /// identity and the taboos are prose (see `dolldocs/goals.md`).
    static func nameProblem(_ text: String, names: [String]) -> String? {
        for n in names where text.contains("我不是\(n)") { return "不能寫「我不是\(n)」：名字和身分是設定裡的，不會變" }
        let markers = ["我叫", "我的名字是", "我的名字叫", "名字叫", "名字是", "改名叫", "改名為"]
        for m in markers {
            var from = text.startIndex
            while let r = text.range(of: m, range: from..<text.endIndex) {
                let rest = text[r.upperBound...].drop { $0 == " " || $0 == "「" || $0 == "：" }
                if !names.contains(where: { rest.hasPrefix($0) }) {
                    return "「\(m)」後面不是你的名字（\(names.joined(separator: "、"))）：名字和身分是設定裡的，不會變"
                }
                from = r.upperBound
            }
        }
        return nil
    }
}

extension Doll {
    // storage ----------------------------------------------------------------------------------------

    func saveGoals() throws { try Concerns.write(goalFile, goalsURL) }
    func saveRecord() throws { try Concerns.write(recordFile, recordURL) }
    func saveCuriosity() throws { try Concerns.write(curiosity, curiosityURL) }

    func node(_ id: Int) -> GoalNode? { goalFile.nodes.first { $0.id == id } }
    func openChildren(_ id: Int) -> [GoalNode] { goalFile.nodes.filter { $0.parent == id && $0.ended == nil } }
    var openGoals: [GoalNode] { goalFile.nodes.filter { $0.level == .goal && $0.ended == nil } }

    /// 動力 moved by `d` (achievement, take-back, a new thing), saved, the timer re-armed.
    func moveDrive(_ d: Double, cause: String) {
        guard drivesConfig.enabled, !signalsFresh, d != 0 else { return }
        let now = wallClock.now()
        let before = drives.state.log.count
        drives.move(.drive, by: d, at: now, cause: cause)
        for c in drives.state.log.dropFirst(before) {
            log("INFO", String(format: "drives: %@ %.2f → %.2f (%@)", c.signal.label, c.before, c.after, c.cause))
        }
        do { try saveSignals() } catch { log("ERROR", "drives: saving \(signalsURL.path) failed: \(error)") }
        feed.notify(.drives)
        scheduleDrives()
    }

    // the goal tree ---------------------------------------------------------------------------------

    func nodeLine(_ n: GoalNode) -> String { "#\(n.id)〔\(n.level.label)〕\(n.text)" }

    /// The whole open tree (`goal.list`), indented, steps under their sub-goal.
    func goalTreeText() -> String {
        guard !openGoals.isEmpty else { return "你的目標樹是空的。用 goal.add 加一個目標（你想往哪個方向走）。" }
        var out: [String] = []
        for g in openGoals {
            out.append(nodeLine(g))
            for s in openChildren(g.id) {
                out.append("  " + nodeLine(s))
                let steps = openChildren(s.id)
                if steps.isEmpty { out.append("    （還沒有步驟）") }
                for st in steps { out.append("    " + nodeLine(st)) }
            }
        }
        let done = goalFile.nodes.filter { $0.end == "done" }.suffix(5)
        if !done.isEmpty { out.append("最近做完的：" + done.map { nodeLine($0) }.joined(separator: "；")) }
        return out.joined(separator: "\n")
    }

    /// 【狀況】's line: the open goals' names only (the tree is by `goal.list`); nil when there are none.
    func goalStatusLine() -> String? {
        guard drivesConfig.enabled, !openGoals.isEmpty else { return nil }
        return "- 你的目標：" + openGoals.map { "#\($0.id) \($0.text)" }.joined(separator: "、") + "（整棵樹用 goal.list 看）"
    }

    func goalText(_ raw: String) throws -> String {
        let t = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { throw ToolError(description: "text 是空的") }
        guard t.count <= goalsConfig.textMaxChars else {
            throw ToolError(description: "太長了（\(t.count) 字，最多 \(goalsConfig.textMaxChars)）：寫成一個短句，細節之後在做的時候寫")
        }
        return t
    }

    /// Adds a goal (no parent), a sub-goal (parent: a goal) or a step (parent: a sub-goal).
    func goalAdd(_ raw: String, parent: Int?, by: String) throws -> GoalNode {
        let text = try goalText(raw)
        let level: GoalNode.Level
        if let parent {
            guard let p = node(parent), p.ended == nil else { throw ToolError(description: "沒有還開著的 #\(parent)；現在的樹：\n" + goalTreeText()) }
            switch p.level {
            case .goal:
                level = .subgoal
                guard openChildren(parent).count < goalsConfig.subgoalsOpenMax else {
                    throw ToolError(description: "#\(parent) 已經有 \(goalsConfig.subgoalsOpenMax) 個還沒做完的子目標；先做完或放下一個（goal.end）")
                }
            case .subgoal:
                level = .step
                guard openChildren(parent).count < goalsConfig.stepsOpenMax else {
                    throw ToolError(description: "#\(parent) 已經有 \(goalsConfig.stepsOpenMax) 個還沒做的步驟；先做掉或放下幾個")
                }
            case .step:
                throw ToolError(description: "#\(parent) 是步驟；樹只有三層（目標→子目標→步驟），步驟下面不能再加。要加步驟，parent 給它的子目標")
            }
        } else {
            level = .goal
            guard openGoals.count < goalsConfig.goalsOpenMax else {
                throw ToolError(description: "已經有 \(goalsConfig.goalsOpenMax) 個目標了；先放下或做完一個（goal.end）")
            }
        }
        let n = GoalNode(id: goalFile.nextId, level: level, parent: parent, text: text, created: wallClock.now(), by: by)
        goalFile.nextId += 1
        goalFile.nodes.append(n)
        try saveGoals()
        log("INFO", "goals: \(by) added \(nodeLine(n))\(parent.map { " under #\($0)" } ?? "")")
        goalsChanged()
        return n
    }

    func goalChange(_ id: Int, _ raw: String, by: String) throws -> GoalNode {
        let text = try goalText(raw)
        guard let i = goalFile.nodes.firstIndex(where: { $0.id == id && $0.ended == nil }) else {
            throw ToolError(description: "沒有還開著的 #\(id)")
        }
        let old = goalFile.nodes[i].text
        goalFile.nodes[i].text = text
        try saveGoals()
        log("INFO", "goals: \(by) changed #\(id)「\(old)」→「\(text)」")
        goalsChanged()
        return goalFile.nodes[i]
    }

    /// `done`: a step only through its todo item (the owner may mark one); a sub-goal or goal only
    /// when nothing under it is still open. `drop`: it and everything open under it.
    func goalEnd(_ id: Int, how: String, by: String) throws -> GoalNode {
        guard let n = node(id), n.ended == nil else { throw ToolError(description: "沒有還開著的 #\(id)") }
        let now = wallClock.now()
        if how == "done" {
            if n.level == .step, by == "her" {
                throw ToolError(description: "步驟要真的做：用 todo.set 列它（給 subgoal \(n.parent!)，那一項開頭寫 #\(id)），做完 todo.done 就記上了")
            }
            let open = openChildren(id)
            guard open.isEmpty else {
                throw ToolError(description: "#\(id) 下面還有沒做完的：" + open.map { nodeLine($0) }.joined(separator: "、") + "；先做完或放下（goal.end drop）")
            }
        }
        var ids = [id]
        if how == "drop" {
            var i = 0
            while i < ids.count { ids += openChildren(ids[i]).map(\.id); i += 1 }
        }
        for k in goalFile.nodes.indices where ids.contains(goalFile.nodes[k].id) {
            goalFile.nodes[k].ended = now
            goalFile.nodes[k].end = how == "done" ? "done" : "dropped"
        }
        if how == "drop", let s = goalFile.link?.subgoal, ids.contains(s) { goalFile.link?.subgoal = nil }
        try saveGoals()
        log("INFO", "goals: \(by) \(how) \(nodeLine(n))\(ids.count > 1 ? " and \(ids.count - 1) under it" : "")")
        if how == "done" {
            let amount = n.level == .goal ? goalsConfig.goalDrive : n.level == .subgoal ? goalsConfig.subgoalDrive : goalsConfig.stepDrive
            try achieve(kind: n.level == .goal ? "goal" : n.level == .subgoal ? "subgoal" : "step", text: n.text, node: n.id, amount: amount)
        }
        goalsChanged()
        return node(id)!
    }

    /// After every change of the tree: its concerns, the dashboard.
    func goalsChanged() {
        do { try refreshGoalConcerns(now: wallClock.now()) } catch { log("ERROR", "goals: concerns: \(error)") }
        feed.notify(.drives)
    }

    /// The tree's program concerns (about = own): per open sub-goal, its next open step, or that it
    /// has none. One per sub-goal, not every step: what she would pick up next, and the concern list
    /// stays bounded (goals × sub-goals). Ones whose node no longer qualifies end.
    func refreshGoalConcerns(now: Double) throws {
        guard drivesConfig.enabled else { return }
        var want: [String: (String, Int)] = [:]
        for g in openGoals {
            for s in openChildren(g.id) {
                if let st = openChildren(s.id).first {
                    want["goal:\(st.id)"] = ("目標樹的下一步：#\(st.id)「\(st.text)」（子目標 #\(s.id)「\(s.text)」；做的時候用 todo.set 列，給 subgoal \(s.id)，那一項開頭寫 #\(st.id)）", st.id)
                } else {
                    want["goalsub:\(s.id)"] = ("子目標 #\(s.id)「\(s.text)」還沒有下一步（目標「\(g.text)」；想好了用 goal.add 加步驟）", s.id)
                }
            }
        }
        var changed = false
        for i in concernFile.items.indices where concernFile.items[i].ended == nil && concernFile.items[i].source == "goal" {
            guard let k = concernFile.items[i].key, want[k] == nil else { continue }
            let nodeID = Int(k.split(separator: ":").last ?? "") ?? 0
            concernFile.items[i].ended = now
            concernFile.items[i].end = node(nodeID)?.end == "done" ? "done" : "passed"
            changed = true
            log("INFO", "drives: concern #\(concernFile.items[i].id) ended (the tree changed)")
        }
        if changed { try saveConcerns() }
        for (k, v) in want.sorted(by: { $0.value.1 < $1.value.1 }) where !concernFile.items.contains(where: { $0.key == k && $0.ended == nil }) {
            try addConcern(v.0, about: .own, source: "goal", key: k)
        }
    }

    // her tools ------------------------------------------------------------------------------------

    func goalAddTool(text: String, parent: Int?) throws -> Outcome {
        let n = try goalAdd(text, parent: parent, by: "her")
        let hint = n.level == .subgoal ? "要做的時候把一步一步列成 todo：todo.set(items, subgoal: \(n.id))。"
            : n.level == .step ? "做的時候用 todo.set 列，給 subgoal \(n.parent!)，那一項開頭寫 #\(n.id)。" : ""
        return Outcome(text: "加好了：\(nodeLine(n))。\(hint)", actions: ["goal add"])
    }

    func goalChangeTool(id: Int, text: String) throws -> Outcome {
        Outcome(text: "改好了：\(nodeLine(try goalChange(id, text, by: "her")))。", actions: ["goal change"])
    }

    func goalEndTool(id: Int, how: String) throws -> Outcome {
        let n = try goalEnd(id, how: how, by: "her")
        return Outcome(text: "\(nodeLine(n)) \(how == "done" ? "做完了，記下了" : "放下了")。", actions: ["goal end"])
    }

    // todo ↔ steps -----------------------------------------------------------------------------------

    /// The step an item names: it begins with `#<id>` (「#3 shark-list.md 寫好了」). A todo item is
    /// written as what done looks like, so it rarely equals the step's own words (real runs t,
    /// 2026-11-16: 3 of 3 rephrased it and the step stayed open beside a new one); the number ties them.
    static func stepRef(_ item: String) -> Int? {
        guard item.hasPrefix("#") else { return nil }
        return Int(item.dropFirst().prefix { $0.isNumber })
    }

    /// The open step of `subgoal` an item is: by its `#<id>`, else by the same words.
    func stepIndex(_ item: String, subgoal: Int) -> Int? {
        if let id = Self.stepRef(item) { return goalFile.nodes.firstIndex { $0.id == id && $0.parent == subgoal && $0.ended == nil } }
        return goalFile.nodes.firstIndex { $0.parent == subgoal && $0.ended == nil && $0.text == item }
    }

    /// `todo.set` with a sub-goal: checks it and adds the items that are not yet its open steps
    /// (all or none, within `steps_open_max`). Returns the note for her result.
    func linkTodo(_ items: [String], subgoal: Int) throws -> String {
        guard drivesConfig.enabled else { throw ToolError(description: "沒有目標樹") }
        guard let s = node(subgoal), s.ended == nil, s.level == .subgoal else {
            throw ToolError(description: "#\(subgoal) 不是還開著的子目標；現在的樹：\n" + goalTreeText())
        }
        for i in items {
            if let id = Self.stepRef(i), stepIndex(i, subgoal: subgoal) == nil {
                throw ToolError(description: "#\(id) 不是子目標 #\(subgoal) 還沒做的步驟；它的步驟：" + openChildren(subgoal).map { nodeLine($0) }.joined(separator: "、"))
            }
        }
        let open = openChildren(subgoal)
        let new = items.filter { stepIndex($0, subgoal: subgoal) == nil }
        guard open.count + new.count <= goalsConfig.stepsOpenMax else {
            throw ToolError(description: "#\(subgoal) 最多 \(goalsConfig.stepsOpenMax) 個還沒做的步驟；這樣會有 \(open.count + new.count) 個")
        }
        for i in new { _ = try goalText(i) }
        for i in new { _ = try goalAdd(i, parent: subgoal, by: "her") }
        let bound = items.compactMap { stepIndex($0, subgoal: subgoal).map { goalFile.nodes[$0].id } }
        return "這份清單是子目標 #\(subgoal) 的步驟" + (bound.isEmpty ? "" : "（" + bound.map { "#\($0)" }.joined(separator: "、") + "）")
            + (new.isEmpty ? "。" : "；新的步驟加進樹裡了：" + new.map { "「\($0)」" }.joined() + "。")
    }

    /// A list starts (from 待命中) or is rewritten: which sub-goal it works on.
    func startLink(subgoal: Int?, fresh: Bool) {
        guard drivesConfig.enabled else { return }
        if fresh { goalFile.link = TodoLink(subgoal: subgoal) } else if let subgoal { goalFile.link = goalFile.link.map { var l = $0; l.subgoal = subgoal; return l } ?? TodoLink(subgoal: subgoal) }
        do { try saveGoals() } catch { log("ERROR", "goals: saving \(goalsURL.path) failed: \(error)") }
    }

    /// `todo.done`: a tree step is marked done (its achievement); any other item moves 動力 as a todo
    /// item. Returns the note for her result.
    func itemFinished(_ item: String) -> String {
        guard drivesConfig.enabled else { signal(.itemDone, item); return "" }
        var note = ""
        if goalFile.link == nil { goalFile.link = TodoLink() }
        goalFile.link!.done.append(item)
        if let sub = goalFile.link?.subgoal, let i = stepIndex(item, subgoal: sub) {
            goalFile.nodes[i].ended = wallClock.now()
            goalFile.nodes[i].end = "done"
            let n = goalFile.nodes[i]
            do {
                try saveGoals()
                let e = try achieve(kind: "step", text: n.text, node: n.id, amount: goalsConfig.stepDrive)
                if let c = concernFile.items.first(where: { $0.key == "goal:\(n.id)" && $0.ended == nil }) { chainAfter = c.id }
                note = "目標樹上的步驟 #\(n.id) 記成做完了（紀錄 #\(e.id)）。"
                if openChildren(sub).isEmpty, let s = node(sub) {
                    note += "子目標 #\(sub)「\(s.text)」的步驟都做完了：結果有了就用 goal.end(\(sub), done)，還沒就再加步驟。"
                }
            } catch { log("ERROR", "goals: step done not saved: \(error)") }
            goalsChanged()
        } else {
            do { try saveGoals() } catch { log("ERROR", "goals: saving \(goalsURL.path) failed: \(error)") }
            signal(.itemDone, item)
        }
        return note
    }

    /// The list is done (its last item): not tree work → one record for it (what the owner asked).
    func listFinished() -> String {
        guard drivesConfig.enabled, let link = goalFile.link else { return "" }
        goalFile.link = nil
        do { try saveGoals() } catch { log("ERROR", "goals: saving \(goalsURL.path) failed: \(error)") }
        guard link.subgoal == nil, !link.done.isEmpty else { return "" }
        do {
            let e = try achieve(kind: "list", text: link.done.joined(separator: "；"), node: nil, amount: goalsConfig.listDrive)
            return "這份清單記成做完的事（紀錄 #\(e.id)）。"
        } catch {
            log("ERROR", "goals: record not saved: \(error)")
            return ""
        }
    }

    /// The list ends without being finished (`todo.set` with no items).
    func listDropped() {
        guard drivesConfig.enabled, goalFile.link != nil else { return }
        goalFile.link = nil
        do { try saveGoals() } catch { log("ERROR", "goals: saving \(goalsURL.path) failed: \(error)") }
    }

    // the record and achievement -------------------------------------------------------------------

    func recordLabel(_ kind: String) -> String {
        switch kind {
        case "step": "步驟"
        case "subgoal": "子目標"
        case "goal": "目標"
        default: "todo 清單"
        }
    }

    /// What stands of an entry's amount: the owner's evaluation, else hers; good = all, bad = none,
    /// none yet = the share given at once.
    func target(_ e: RecordEntry) -> Double {
        (e.owner ?? e.her).map { $0.good ? e.amount : 0 } ?? e.amount * goalsConfig.nowShare
    }

    @discardableResult
    func achieve(kind: String, text: String, node: Int?, amount: Double) throws -> RecordEntry {
        var e = RecordEntry(id: recordFile.nextId, ts: wallClock.now(), kind: kind, text: text, node: node, amount: amount, given: 0)
        e.given = target(e)
        recordFile.nextId += 1
        recordFile.entries.append(e)
        try saveRecord()
        log("INFO", "record: #\(e.id) \(kind)「\(text)」")
        moveDrive(e.given, cause: "做完\(recordLabel(kind))：\(text)")
        return e
    }

    /// Sets her or the owner's evaluation (nil clears the owner's) and moves 動力 to what now stands.
    @discardableResult
    func evaluate(_ id: Int, good: Bool?, owner: Bool, quote: String?) throws -> RecordEntry {
        guard let i = recordFile.entries.firstIndex(where: { $0.id == id }) else { throw ToolError(description: "沒有紀錄 #\(id)") }
        let ev = good.map { Evaluation(good: $0, quote: quote, ts: wallClock.now()) }
        if owner { recordFile.entries[i].owner = ev } else { recordFile.entries[i].her = ev }
        let e = recordFile.entries[i]
        let delta = target(e) - e.given
        recordFile.entries[i].given = target(e)
        try saveRecord()
        let who = owner ? "主人評" : "主人說"
        let word = good.map { $0 ? "好" : "不對" } ?? "（取消評價）"
        moveDrive(delta, cause: "\(who)\(word)：\(recordLabel(e.kind))「\(e.text)」")
        feed.notify(.drives)
        return recordFile.entries[i]
    }

    /// `owner.feedback`'s record: the one named, else the newest within `feedback_hours`.
    func feedbackRecord(_ id: Int?) throws -> RecordEntry? {
        if let id {
            guard let e = recordFile.entries.first(where: { $0.id == id }) else { throw ToolError(description: "沒有紀錄 #\(id)") }
            return e
        }
        return recordFile.entries.last.flatMap { $0.ts >= wallClock.now() - goalsConfig.feedbackHours * 3600 ? $0 : nil }
    }

    // the ideal self ---------------------------------------------------------------------------------

    /// Why a text cannot be the ideal self, or nil.
    func idealProblem(_ text: String) -> String? {
        if text.isEmpty { return "是空的" }
        if text.count > goalsConfig.idealMaxChars { return "太長了（\(text.count) 字，最多 \(goalsConfig.idealMaxChars)）：寫最重要的" }
        return Goals.nameProblem(text, names: packNames)
    }

    /// One revision, written at once (file then log), effective from the next request.
    @discardableResult
    func reviseIdeal(_ new: String, by: String, why: String, basedOn: String) throws -> IdealRevision {
        let text = new.trimmingCharacters(in: .whitespacesAndNewlines)
        if let p = idealProblem(text) { throw ToolError(description: "沒改：\(p)") }
        guard text != ideal else { throw ToolError(description: "沒改：跟現在的一樣") }
        let r = IdealRevision(id: idealLog.nextId, ts: wallClock.now(), by: by, before: ideal, after: text, why: why, basedOn: basedOn)
        var log2 = idealLog
        log2.nextId += 1
        log2.revisions.append(r)
        try Data((text + "\n").utf8).write(to: idealURL, options: .atomic)
        try Concerns.write(log2, idealLogURL)
        idealLog = log2
        ideal = text
        log("INFO", "ideal: revision #\(r.id) by \(by): \(why)")
        feed.notify(.drives)
        return r
    }

    func idealReviseTool(text: String?, old: String?, new: String?, why: String, basedOn: String) throws -> Outcome {
        let next: String
        if let text, !text.isEmpty {
            guard old == nil && new == nil else { throw ToolError(description: "text（整段換掉）和 old/new（改一處）只能選一種") }
            next = text
        } else {
            guard let old, !old.isEmpty, let new else { throw ToolError(description: "給 text（整段新的），或 old 加 new（把 old 那段換成 new）") }
            let n = ideal.components(separatedBy: old).count - 1
            guard n == 1 else { throw ToolError(description: n == 0 ? "現在的內容裡沒有「\(old)」" : "「\(old)」出現 \(n) 次，多抄一點讓它只出現一次") }
            next = ideal.replacingOccurrences(of: old, with: new)
        }
        let r = try reviseIdeal(next, by: "her", why: why, basedOn: basedOn)
        return Outcome(text: "改好了（第 \(r.id) 版），從下一回合起就是：\(ideal)", actions: ["ideal revise"])
    }

    // curiosity ------------------------------------------------------------------------------------

    /// What is in front of her after an action: a new app or browser page raises 動力.
    func noteSeen(app: AppIdentity, line: String) {
        guard drivesConfig.enabled else { return }
        let now = wallClock.now()
        var keys = ["app:\(app.bundleID)"]
        if Goals.browsers.contains(app.bundleID), let t = Goals.frontTitle(line) { keys.append("page:\(t)") }
        var fresh: [String] = []
        for k in keys {
            if let last = curiosity.seen[k], last >= now - goalsConfig.seenDays * 86400 {} else { fresh.append(k) }
            curiosity.seen[k] = now
        }
        curiosity.seen = curiosity.seen.filter { $0.value >= now - goalsConfig.seenDays * 86400 }
        if !fresh.isEmpty {
            curiosity.lastNew = now
            log("INFO", "curiosity: new \(fresh)")
            if curiosity.lastRaise.map({ now - $0 >= goalsConfig.noveltyGapMin * 60 }) ?? true {
                curiosity.lastRaise = now
                moveDrive(goalsConfig.noveltyDrive, cause: "碰到新東西：" + fresh.map { String($0.split(separator: ":", maxSplits: 1).last ?? "") }.joined(separator: "、"))
            }
        }
        do { try saveCuriosity() } catch { log("ERROR", "curiosity: saving \(curiosityURL.path) failed: \(error)") }
    }

    /// When nothing new for `curiosity_hours` (since the last new thing or the last such wandering) is due.
    func curiosityDue() -> Double? {
        guard drivesConfig.enabled else { return nil }
        let from = max(curiosity.lastNew ?? 0, curiosity.surfaced ?? 0)
        return from + goalsConfig.curiosityHours * 3600
    }

    /// At the first check of a data dir without curiosity: the span starts now.
    func startCuriosity(now: Double) {
        guard curiosity.lastNew == nil else { return }
        curiosity.lastNew = now
        do { try saveCuriosity() } catch { log("ERROR", "curiosity: saving \(curiosityURL.path) failed: \(error)") }
    }

    static let curiosityText = "\(Concerns.wanderTag)你發現好一陣子沒碰到沒見過的東西了。有空可以找一樣你的目標樹上沒有的東西看看（用電腦，先 todo.set 列一項）；覺得有意思，可以用 goal.add 把它加成一個新目標。"

    func curiosityWander(now: Double) {
        curiosity.surfaced = now
        do { try saveCuriosity() } catch { log("ERROR", "curiosity: saving \(curiosityURL.path) failed: \(error)") }
        wanderLine(Self.curiosityText, signal: .drive)
    }

    // the owner's edits (dashboard) ----------------------------------------------------------------------

    /// A no-turn notice: she is told of the owner's edit in her next turn.
    func toldOwnerEdit(_ text: String) {
        do { try notice(Event(.memory, .internal, text)) } catch { log("ERROR", "goals: owner-edit notice not written: \(error)") }
    }

    public func dashboardIdealEdit(text: String) throws {
        try reviseIdeal(text, by: "owner", why: "主人在儀表板改的", basedOn: "")
        toldOwnerEdit("【主人改了你想成為的樣子】現在是：\(ideal)")
    }

    public func dashboardIdealRevert(_ id: Int) throws {
        guard let r = idealLog.revisions.first(where: { $0.id == id }) else { throw ToolError(description: "no revision #\(id)") }
        try reviseIdeal(r.before.isEmpty ? r.after : r.before, by: "revert", why: "主人在儀表板把第 \(id) 版的修改退回", basedOn: "")
        toldOwnerEdit("【主人改了你想成為的樣子】他把第 \(id) 版的修改退回了。現在是：\(ideal)")
    }

    public func dashboardGoalEdit(_ op: String, _ q: [String: String]) throws -> GoalNode {
        let id = q["id"].flatMap { Int($0) }, parent = q["parent"].flatMap { Int($0) }
        let n: GoalNode
        switch op {
        case "add": n = try goalAdd(q["text"] ?? "", parent: parent, by: "owner")
        case "change":
            guard let id else { throw ToolError(description: "id needed") }
            n = try goalChange(id, q["text"] ?? "", by: "owner")
        case "done", "drop":
            guard let id else { throw ToolError(description: "id needed") }
            n = try goalEnd(id, how: op, by: "owner")
        default: throw ToolError(description: "unknown op \(op)")
        }
        let what = switch op {
        case "add": "加了 \(nodeLine(n))"
        case "change": "把 #\(n.id) 改成「\(n.text)」"
        case "done": "把 \(nodeLine(n)) 記成做完了"
        default: "放下了 \(nodeLine(n))"
        }
        toldOwnerEdit("【主人改了你的目標樹】他\(what)。整棵樹用 goal.list 看。")
        return n
    }

    public func dashboardEvaluate(_ id: Int, _ kind: String) throws -> RecordEntry {
        let good: Bool? = switch kind {
        case "good": true
        case "bad": false
        case "none": nil
        default: throw ToolError(description: "kind is good, bad or none")
        }
        let e = try evaluate(id, good: good, owner: true, quote: nil)
        if let good { toldOwnerEdit("【主人的評價】你做完的「\(e.text)」（紀錄 #\(e.id)），主人評\(good ? "做得好" : "不對")。") }
        return e
    }

    public func dashboardGoals() -> DashboardGoals {
        DashboardGoals(enabled: drivesConfig.enabled, ideal: ideal, revisions: idealLog.revisions.reversed(), nodes: goalFile.nodes,
                       link: goalFile.link, record: recordFile.entries.reversed(),
                       lastNew: curiosity.lastNew, curiosityDue: curiosityDue())
    }
}

/// The dashboard's view of her direction.
public struct DashboardGoals: Sendable, Codable {
    public var enabled: Bool
    public var ideal: String
    public var revisions: [IdealRevision]
    public var nodes: [GoalNode]
    public var link: TodoLink?
    public var record: [RecordEntry]
    public var lastNew: Double?
    public var curiosityDue: Double?
}

/// The maintenance task `review` (2026-11-16): one request (the consolidation's model) reads her
/// ideal self, the pack's fixed parts, the tree, today's record and the day's conversation, and
/// answers `不改` or a whole new text with why and what it is based on. Applied at once (the owner's
/// decision), checked like her own revisions; refused = nothing written, the notice says why.
enum IdealReview {
    struct Answer: Sendable {
        var text: String?
        var why: String
        var basedOn: String
        var reply: ModelReply
        var seconds: Double
    }

    static func system(name: String, maxChars: Int) -> String {
        """
        你是\(name)。現在是每天的系統維護，你回顧今天，看你「想成為的樣子」要不要改。
        只有今天真的發生了讓你想法變了的事（主人說了什麼、你做成或做砸了什麼、碰到新的東西）才改；沒有就不改。大部分的日子是不改的。
        不能改名字、身分，也不能違反「設定」裡的禁忌；那些是固定的。最多 \(maxChars) 字。
        只用下面兩種格式之一回答，不要寫別的：
        不改
        或
        改成：
        （新的整段，用你自己的口吻）
        理由：（一句）
        根據：（今天的哪件事，一句）
        """
    }

    static func user(pack: String, ideal: String, tree: String, record: [String], day: String) -> String {
        "設定（固定的）：\n\(pack)\n\n你現在想成為的樣子：\n\(ideal)\n\n你的目標樹：\n\(tree)\n\n今天做完的事：\n"
            + (record.isEmpty ? "（沒有）" : record.joined(separator: "\n")) + "\n\n今天的對話（最後一段）：\n\(day)"
    }

    static func parse(_ reply: String) throws -> (text: String?, why: String, basedOn: String) {
        let t = reply.trimmingCharacters(in: .whitespacesAndNewlines)
        if t.hasPrefix("不改") { return (nil, "", "") }
        guard let a = t.range(of: "改成："), let w = t.range(of: "理由："), let b = t.range(of: "根據："),
              a.upperBound <= w.lowerBound, w.upperBound <= b.lowerBound else {
            throw ModelError("ideal review: not in the form「\(t.prefix(200))」")
        }
        let text = t[a.upperBound..<w.lowerBound].trimmingCharacters(in: .whitespacesAndNewlines)
        let why = t[w.upperBound..<b.lowerBound].trimmingCharacters(in: .whitespacesAndNewlines)
        let based = t[b.upperBound...].trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !why.isEmpty else { throw ModelError("ideal review: empty text or why") }
        return (text, why, based)
    }
}

extension Doll {
    /// Due: a day to look back on, and the last maintenance revision `revise_min_days` ago or more.
    var reviewDue: Bool {
        guard drivesConfig.enabled, goalsConfig.review, dayToConsolidate else { return false }
        let last = idealLog.revisions.last { $0.by == "maintenance" }?.ts ?? 0
        return wallClock.now() - last >= goalsConfig.reviseMinDays * 86400
    }

    func prepareIdealReview() async throws -> IdealReview.Answer {
        let events = timeline.events
        let segStart = events.first?.ts ?? 0
        let record = recordFile.entries.filter { $0.ts >= segStart }.map { "- \(recordLabel($0.kind))：\($0.text)" }
        var day = Consolidation.tasks(events).map(\.text).joined(separator: "\n\n")
        if day.count > goalsConfig.reviewChars { day = String(day.suffix(goalsConfig.reviewChars)) }
        let messages = [ChatMessage(role: "system", content: IdealReview.system(name: name, maxChars: goalsConfig.idealMaxChars)),
                        ChatMessage(role: "user", content: IdealReview.user(pack: identity, ideal: ideal, tree: goalTreeText(), record: record, day: day))]
        let started = Date()
        let reply = try await maintenanceModel.chat(messages, tools: nil, effort: config.consolidate.effort)
        let p = try IdealReview.parse(reply.content)
        return IdealReview.Answer(text: p.text, why: p.why, basedOn: p.basedOn, reply: reply, seconds: Date().timeIntervalSince(started))
    }

    func commitIdealReview(_ a: IdealReview.Answer) throws -> MaintenanceDone {
        let u = a.reply.usage
        let stats = "request \(u.promptTokens) prompt tokens, \(u.completionTokens) completion, \(String(format: "%.1f", a.seconds)) s"
        guard let text = a.text else {
            return MaintenanceDone(log: "no revision; \(stats)", words: "你想成為的樣子沒改。", summary: nil, kept: nil)
        }
        let before = ideal
        do {
            try reviseIdeal(text, by: "maintenance", why: a.why, basedOn: a.basedOn)
        } catch let e as ToolError {
            return MaintenanceDone(log: "revision refused (\(e.description)); \(stats); reply「\(a.reply.content.replacingOccurrences(of: "\n", with: "⏎"))」",
                                   words: "回顧今天時想改你想成為的樣子，但\(e.description)，所以沒改。", summary: nil, kept: nil)
        }
        _ = before
        return MaintenanceDone(log: "revised; \(stats)",
                               words: "回顧今天，你想成為的樣子改了（理由：\(a.why)；根據：\(a.basedOn)）。現在是：\(ideal)", summary: nil, kept: nil)
    }
}
