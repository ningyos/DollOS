import Foundation

/// Her own direction (m6-drives phase 2, 2026-11-16, owner; the graph 2026-11-24). Needs have two
/// roots: she must go on existing (the owner pays for the power and the machine; she helps him), and
/// who she wants to become. This file holds the second root:
///
///   ideal self   `<data_dir>/ideal-self.md` (plain text) and `ideal-self-log.json` (every revision).
///                It starts from the pack's `[identity].ideal`. She revises it with `ideal.revise`;
///                the maintenance may propose one; the owner edits or reverts it in the dashboard.
///                It is the direction, not a node of the graph.
///   the graph    `<data_dir>/mind.json` (owner, 2026-11-24: 「很適合作為 doll 思維的視覺化跟框架」):
///                one kind of node, a thing she wants to do or to be able to do, with a state and what
///                shows it: open (想做), done (做成過, evidence), failed (做不到／失敗過, evidence),
///                dropped. One kind of edge, 「被擋住」: a node lists the nodes it holds up (`blocks`).
///                An edge is never planned ahead: it appears when she hits it (`goal.blocked`: what is
///                missing becomes a node that blocks the one she was at). No levels, no root needed;
///                a node may stand alone. Her strengths and weaknesses are the done and failed nodes
///                (`self.note` writes one directly); a failed one can carry a habit and the days it
///                showed again (`Reflection.swift`). Work on a node is a todo list
///                (`todo.set(items, node)`); its items are not nodes.
///                The open nodes nothing open blocks are what she can act on: each is a program
///                concern about her own things (`refreshGoalConcerns`), the one holding up the most
///                first; 動力 brings one up as a task (【系統任務】).
///   record       `<data_dir>/record.json`: append-only, one entry per thing finished (a node done; a
///                todo list). Each carries 動力's full amount and what was given: part at once
///                (`now_share`), the rest when the evaluation is good, all of it taken back when bad.
///   curiosity    `<data_dir>/curiosity.json`: what she has had in front of her; nothing new for
///                `curiosity_hours`: a wandering toward something not in the graph.
///
/// She never sees a number; the dashboard shows them and draws the graph.
public struct GoalNode: Sendable, Codable, Equatable {
    /// What shows a state: a quote the program found, or `紀錄 #N：…`.
    public struct Evidence: Sendable, Codable, Equatable {
        public var quote: String
        public var ts: Double
        /// "her", "owner" or "maintenance" (the `recur` task).
        public var by: String
    }
    public struct Habit: Sendable, Codable, Equatable {
        public var when: String
        public var then: String
        public var since: Double
    }
    public struct Past: Sendable, Codable, Equatable {
        public var why: String
        public var ts: Double
    }
    public var id: Int
    public var text: String
    public var created: Double
    /// "her" or "owner".
    public var by: String
    /// The nodes it holds up (they are 被擋住 by it).
    public var blocks: [Int] = []
    public var ended: Double?
    /// "done" (做成過), "failed" (做不到／失敗過) or "dropped".
    public var end: String?
    public var evidence: [Evidence] = []
    /// A failed one: the habit she set from it, each day it showed again, and her moving it to 以前有.
    public var habit: Habit?
    public var again: [Evidence] = []
    public var past: Past?

    public var open: Bool { ended == nil }
    public var done: Bool { end == "done" }
    public var failed: Bool { end == "failed" }
}

/// The todo list in hand and the node it works on (nil: not her own thing); the items done so far.
public struct TodoLink: Sendable, Codable, Equatable {
    public var node: Int?
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
    /// "node" (a node of the graph done) or "list" (a todo list).
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
    /// identity and the taboos are prose (see `docs/drives-and-goals.md`).
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

    func saveGoals() throws { try saveDocument(goalFile, goalsURL) }
    func saveRecord() throws { try saveDocument(recordFile, recordURL) }
    func saveCuriosity() throws { try saveDocument(curiosity, curiosityURL) }

    func node(_ id: Int) -> GoalNode? { goalFile.nodes.first { $0.id == id } }

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

    // the graph --------------------------------------------------------------------------------------

    func nodeLine(_ n: GoalNode) -> String { "#\(n.id)「\(n.text)」" }

    /// The open nodes that hold `id` up.
    func blockers(of id: Int) -> [GoalNode] { goalFile.nodes.filter { $0.open && $0.blocks.contains(id) } }

    /// The open nodes `n` holds up, directly or through others.
    func heldUp(by n: GoalNode) -> [GoalNode] {
        var seen = Set<Int>(), stack = n.blocks
        while let id = stack.popLast() {
            guard seen.insert(id).inserted, let m = node(id), m.open else { continue }
            stack += m.blocks
        }
        return goalFile.nodes.filter { seen.contains($0.id) }
    }

    /// The open nodes nothing open blocks: what she can act on now, the one holding up the most first.
    var actionable: [GoalNode] {
        goalFile.nodes.filter { $0.open && blockers(of: $0.id).isEmpty }
            .map { ($0, heldUp(by: $0).count) }.sorted { ($1.1, $0.0.id) < ($0.1, $1.0.id) }.map(\.0)
    }

    func evidenceText(_ n: GoalNode) -> String { n.evidence.first.map { "根據：「\($0.quote.prefix(40))」" } ?? "沒有根據" }

    /// The whole graph in words (`goal.list`): what she wants and what holds it up, then what she has
    /// shown she can and cannot do.
    func goalTreeText() -> String {
        let open = goalFile.nodes.filter(\.open)
        var out: [String] = []
        if open.isEmpty {
            out.append("想做的：（沒有）用 goal.add 加一件你想做到、或想會的事。")
        } else {
            out.append("想做的（還開著）：")
            for n in open {
                let by = blockers(of: n.id), holds = n.blocks.compactMap(node).filter(\.open)
                out.append("- " + nodeLine(n) + (holds.isEmpty ? "" : "（擋著 " + holds.map { "#\($0.id)" }.joined(separator: "、") + "）")
                           + (by.isEmpty ? "" : " ← 被 " + by.map { "#\($0.id)" }.joined(separator: "、") + " 擋住"))
            }
        }
        out.append(panelText())
        return out.joined(separator: "\n")
    }

    /// 【狀況】's line: what she wants that waits on nothing else of hers to be wanted (no open node
    /// needs it); nil when there is none.
    func goalStatusLine() -> String? {
        let tops = goalFile.nodes.filter { n in n.open && !n.blocks.contains { node($0)?.open == true } }
        guard drivesConfig.enabled, !tops.isEmpty else { return nil }
        return "- 你想做的：" + tops.prefix(5).map { "#\($0.id) \($0.text)" }.joined(separator: "、") + "（整張圖用 goal.list 看）"
    }

    func goalText(_ raw: String) throws -> String {
        let t = raw.trimmingCharacters(in: .whitespacesAndNewlines.union(CharacterSet(charactersIn: "。")))
        guard !t.isEmpty else { throw ToolError(description: "text 是空的") }
        guard t.count <= goalsConfig.textMaxChars else {
            throw ToolError(description: "太長了（\(t.count) 字，最多 \(goalsConfig.textMaxChars)）：寫成一個短句，細節之後在做的時候寫")
        }
        return t
    }

    func openNode(_ id: Int) throws -> GoalNode {
        guard let n = node(id), n.open else { throw ToolError(description: "沒有還開著的 #\(id)；現在的圖：\n" + goalTreeText()) }
        return n
    }

    /// Adds an open node; `for`: an open node it holds up (it is needed for that one).
    func goalAdd(_ raw: String, for target: Int?, by: String) throws -> GoalNode {
        let text = try goalText(raw)
        if let target { _ = try openNode(target) }
        if let same = goalFile.nodes.first(where: { $0.open && $0.text == text }) {
            throw ToolError(description: "已經有了：\(nodeLine(same))" + (target.map { "；要讓它擋著 #\($0)，用 goal.blocked(\($0), by: \(same.id))" } ?? ""))
        }
        guard goalFile.nodes.filter(\.open).count < goalsConfig.nodesOpenMax else {
            throw ToolError(description: "已經有 \(goalsConfig.nodesOpenMax) 件還開著的事；先做完、弄清楚或放下幾件（goal.end、goal.fail）")
        }
        let n = GoalNode(id: goalFile.nextId, text: text, created: wallClock.now(), by: by, blocks: target.map { [$0] } ?? [])
        goalFile.nextId += 1
        goalFile.nodes.append(n)
        try saveGoals()
        log("INFO", "goals: \(by) added \(nodeLine(n))\(target.map { " blocking #\($0)" } ?? "")", sealed: Concerns.boxed(goalsURL))
        goalsChanged()
        return n
    }

    func goalChange(_ id: Int, _ raw: String, by: String) throws -> GoalNode {
        let text = try goalText(raw)
        guard let i = goalFile.nodes.firstIndex(where: { $0.id == id && $0.open }) else {
            throw ToolError(description: "沒有還開著的 #\(id)")
        }
        let old = goalFile.nodes[i].text
        goalFile.nodes[i].text = text
        try saveGoals()
        log("INFO", "goals: \(by) changed #\(id)「\(old)」→「\(text)」", sealed: Concerns.boxed(goalsURL))
        goalsChanged()
        return goalFile.nodes[i]
    }

    /// Ends an open node: `done` (做成過) and `failed` (做不到／失敗過) need evidence; `drop` none.
    /// Only the node itself ends; what it held up is free of it.
    func goalEnd(_ id: Int, how: String, evidence: GoalNode.Evidence?, by: String) throws -> GoalNode {
        let n = try openNode(id)
        if how != "drop", evidence == nil { throw ToolError(description: "要有根據（evidence）：照抄撐住它的一段，或「紀錄 #編號」") }
        let i = goalFile.nodes.firstIndex { $0.id == id }!
        goalFile.nodes[i].ended = wallClock.now()
        goalFile.nodes[i].end = how == "done" ? "done" : how == "failed" ? "failed" : "dropped"
        if let evidence { goalFile.nodes[i].evidence.append(evidence) }
        if goalFile.link?.node == id { goalFile.link?.node = nil }
        try saveGoals()
        log("INFO", "goals: \(by) \(how) \(nodeLine(n))", sealed: Concerns.boxed(goalsURL))
        if how == "done" { try achieve(kind: "node", text: n.text, node: n.id, amount: goalsConfig.nodeDrive) }
        goalsChanged()
        return node(id)!
    }

    /// After every change of the graph: its concerns, the dashboard.
    func goalsChanged() {
        do { try refreshGoalConcerns(now: wallClock.now()) } catch { log("ERROR", "goals: concerns: \(error)") }
        feed.notify(.drives)
    }

    /// What 動力 brings up for an actionable node: the task and what completes it.
    func taskText(_ n: GoalNode) -> String {
        let held = heldUp(by: n)
        let lead = held.isEmpty ? "\(nodeLine(n)) 是你想做的事"
            : "\(nodeLine(n)) 還沒弄清楚，它擋著 " + held.prefix(3).map(nodeLine).joined(separator: "、") + (held.count > 3 ? " 等 \(held.count) 件" : "")
        return lead + "。完成條件：有根據地知道做得到還是做不到——做到了用 goal.end(\(n.id), done, evidence)，做不到用 goal.fail(\(n.id), evidence)；"
            + "做到一半發現還缺別的，用 goal.blocked(\(n.id), missing) 記下缺什麼。要動手就 todo.set(items, node: \(n.id))。"
    }

    /// The graph's program concerns (about = own): one per actionable node, the one holding up the
    /// most first, at most `[goals] tasks_max`. Ones whose node no longer qualifies end.
    func refreshGoalConcerns(now: Double) throws {
        guard drivesConfig.enabled else { return }
        let nodes = Array(actionable.prefix(goalsConfig.tasksMax))
        var want: [String: String] = [:]
        for n in nodes { want["node:\(n.id)"] = taskText(n) }
        var changed = false
        for i in concernFile.items.indices where concernFile.items[i].ended == nil && concernFile.items[i].source == "goal" {
            guard let k = concernFile.items[i].key else { continue }
            if let w = want[k] {
                if concernFile.items[i].text != w { concernFile.items[i].text = w; changed = true }
            } else {
                let nodeID = Int(k.split(separator: ":").last ?? "") ?? 0
                concernFile.items[i].ended = now
                concernFile.items[i].end = node(nodeID)?.end == "done" ? "done" : "passed"
                changed = true
                log("INFO", "drives: concern #\(concernFile.items[i].id) ended (the graph changed)")
            }
        }
        if changed { try saveConcerns() }
        // In the order they should come up: the concern made first has the lower id (`Concerns.pick`).
        for n in nodes where !concernFile.items.contains(where: { $0.key == "node:\(n.id)" && $0.ended == nil }) {
            try addConcern(want["node:\(n.id)"]!, about: .own, source: "goal", key: "node:\(n.id)")
        }
    }

    // her tools ------------------------------------------------------------------------------------

    func goalAddTool(text: String, for target: Int?) throws -> Outcome {
        let n = try goalAdd(text, for: target, by: "her")
        return Outcome(text: "加好了：\(nodeLine(n))" + (target.map { "，它擋著 #\($0)" } ?? "") + "。要動手的時候把一步一步列成 todo：todo.set(items, node: \(n.id))。",
                       actions: ["goal add"])
    }

    func goalChangeTool(id: Int, text: String) throws -> Outcome {
        Outcome(text: "改好了：\(nodeLine(try goalChange(id, text, by: "her")))。", actions: ["goal change"])
    }

    func goalEndTool(id: Int, how: String, evidence: String?) throws -> Outcome {
        let e = how == "drop" ? nil : try evidence.map { try checkedEvidence($0, by: "her") }
        let n = try goalEnd(id, how: how, evidence: e, by: "her")
        let freed = n.blocks.compactMap(node).filter { $0.open && blockers(of: $0.id).isEmpty }
        return Outcome(text: "\(nodeLine(n)) " + (how == "done" ? "記成做成過了" : how == "failed" ? "記成做不到了" : "放下了")
                       + (freed.isEmpty ? "。" : "；\(freed.map(nodeLine).joined(separator: "、")) 不再被它擋住。")
                       + (how == "failed" ? "想改它：碰到某個情況就該怎麼做的，用 self.habit(\(n.id), when, do) 定習慣；要下功夫的，用 goal.add 加一件想做的事。" : ""),
                       sealed: sealing, actions: ["goal \(how == "drop" ? "drop" : how)"])
    }

    /// `goal.blocked` (grounding fix 2, 2026-11-22; an edge of the graph since 2026-11-24): she was at
    /// `id` and something is missing. What is missing becomes an open node that holds `id` up
    /// (`missing`: its words; `by`: an existing open node). Not a failure: no todo, no 壓力.
    func goalBlockedTool(id: Int, missing: String?, by: Int?) throws -> Outcome {
        let n = try openNode(id)
        let blocker: GoalNode
        if let by {
            let b = try openNode(by)
            guard b.id != id, !heldUp(by: n).contains(where: { $0.id == b.id }) else {
                throw ToolError(description: "#\(by) 自己就在等 #\(id)，不能反過來擋它")
            }
            if !b.blocks.contains(id) {
                goalFile.nodes[goalFile.nodes.firstIndex { $0.id == by }!].blocks.append(id)
                try saveGoals()
            }
            blocker = node(by)!
        } else {
            let m = try goalText(missing ?? "")
            if let same = goalFile.nodes.first(where: { $0.open && $0.text == m }) {
                return try goalBlockedTool(id: id, missing: nil, by: same.id)
            }
            blocker = try goalAdd(m, for: id, by: "her")
        }
        log("INFO", "goals: her blocked \(nodeLine(n)) by \(nodeLine(blocker))", sealed: Concerns.boxed(goalsURL))
        goalsChanged()
        return Outcome(text: "記下了：\(nodeLine(n)) 被 \(nodeLine(blocker)) 擋住。缺的有了（或弄清楚做不到）再回來；它之後會先冒出來。", actions: ["goal blocked"])
    }

    // todo ↔ the graph -------------------------------------------------------------------------------

    /// `todo.set` with a node: checks it. Returns the note for her result.
    func linkTodo(_ items: [String], node id: Int) throws -> String {
        guard drivesConfig.enabled else { throw ToolError(description: "沒有圖") }
        let n = try openNode(id)
        return "這份清單是為了 \(nodeLine(n))。"
    }

    /// A list starts (from 待命中) or is rewritten: which node it works on.
    func startLink(node id: Int?, fresh: Bool) {
        guard drivesConfig.enabled else { return }
        if fresh { goalFile.link = TodoLink(node: id) } else if let id { goalFile.link = goalFile.link.map { var l = $0; l.node = id; return l } ?? TodoLink(node: id) }
        do { try saveGoals() } catch { log("ERROR", "goals: saving \(goalsURL.path) failed: \(error)") }
    }

    /// `todo.done`: one item of the list in hand. Returns the note for her result.
    func itemFinished(_ item: String) -> String {
        guard drivesConfig.enabled else { signal(.itemDone, item); return "" }
        if goalFile.link == nil { goalFile.link = TodoLink() }
        goalFile.link!.done.append(item)
        do { try saveGoals() } catch { log("ERROR", "goals: saving \(goalsURL.path) failed: \(error)") }
        signal(.itemDone, item)
        return ""
    }

    /// The list is done (its last item): one record for it; for a node's list, what to do with the node.
    func listFinished() -> String {
        guard drivesConfig.enabled, let link = goalFile.link else { return "" }
        goalFile.link = nil
        do { try saveGoals() } catch { log("ERROR", "goals: saving \(goalsURL.path) failed: \(error)") }
        guard !link.done.isEmpty else { return "" }
        do {
            let e = try achieve(kind: "list", text: link.done.joined(separator: "；"), node: link.node, amount: goalsConfig.listDrive)
            guard let id = link.node, let n = node(id), n.open else { return "這份清單記成做完的事（紀錄 #\(e.id)）。" }
            return "這份清單記成做完的事（紀錄 #\(e.id)）。\(nodeLine(n)) 呢：做到了用 goal.end(\(id), done, evidence: 「紀錄 #\(e.id)」)，"
                + "試了做不到用 goal.fail(\(id), evidence)，還缺別的用 goal.blocked(\(id), missing)，還沒完就再列一份。"
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
        case "node": "想做的事"
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
        log("INFO", "record: #\(e.id) \(kind)「\(text)」", sealed: Concerns.boxed(recordURL))
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
        try Concerns.writeDocument(Data((text + "\n").utf8), idealURL, box: sealKey(idealURL))
        try saveDocument(log2, idealLogURL)
        idealLog = log2
        ideal = text
        log("INFO", "ideal: revision #\(r.id) by \(by): \(why)")
        historyRefs.append("ideal#\(r.id)")
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
    /// Never with the computer off (2026-11-19): new apps and pages are the only source of "new", and
    /// the wandering would send her to look at something she cannot reach.
    func curiosityDue() -> Double? {
        guard drivesConfig.enabled, computerOn else { return nil }
        let from = max(curiosity.lastNew ?? 0, curiosity.surfaced ?? 0)
        return from + goalsConfig.curiosityHours * 3600
    }

    /// At the first check of a data dir without curiosity: the span starts now.
    func startCuriosity(now: Double) {
        guard curiosity.lastNew == nil else { return }
        curiosity.lastNew = now
        do { try saveCuriosity() } catch { log("ERROR", "curiosity: saving \(curiosityURL.path) failed: \(error)") }
    }

    static let curiosityText = "\(Concerns.wanderTag)你發現好一陣子沒碰到沒見過的東西了。有空可以找一樣你的圖上沒有的東西看看（用電腦，先 todo.set 列一項）；覺得有意思，可以用 goal.add 把它加成一個新目標。"

    func curiosityWander(now: Double) {
        curiosity.surfaced = now
        do { try saveCuriosity() } catch { log("ERROR", "curiosity: saving \(curiosityURL.path) failed: \(error)") }
        wanderLine(Self.curiosityText, signal: .drive)
    }

    // the owner's edits (dashboard) ----------------------------------------------------------------------

    /// A no-turn notice: she is told of the owner's edit in her next turn (stored encrypted while `sealing`).
    func toldOwnerEdit(_ text: String) {
        do { try notice(Event(.memory, .internal, text, sealed: sealing)) } catch { log("ERROR", "goals: owner-edit notice not written: \(error)") }
    }

    /// The owner's edit of a document stored sealed derives from it: its writes are sealed too.
    func sealedEdit<T>(_ url: URL, _ edit: () throws -> T) rethrows -> T {
        let was = sealing
        sealing = was || Concerns.boxed(url)
        defer { sealing = was }
        return try edit()
    }

    public func dashboardIdealEdit(text: String) throws {
        try sealedEdit(idealURL) {
            try reviseIdeal(text, by: "owner", why: "主人在儀表板改的", basedOn: "")
            commitHistory(.owner, what: "改想成為的樣子", why: "主人在儀表板改的")
            toldOwnerEdit("【主人改了你想成為的樣子】現在是：\(ideal)")
        }
    }

    /// The revision's button: a shortcut to undoing the change that made it in the history (she is
    /// told by the undo).
    public func dashboardIdealRevert(_ id: Int) throws {
        guard idealLog.revisions.contains(where: { $0.id == id }) else { throw ToolError(description: "no revision #\(id)") }
        do { try undoRef("ideal#\(id)") } catch let e as HistoryError { throw ToolError(description: e.description) }
    }

    public func dashboardGoalEdit(_ op: String, _ q: [String: String]) throws -> GoalNode {
        try sealedEdit(goalsURL) {
            let id = q["id"].flatMap { Int($0) }, target = q["for"].flatMap { Int($0) }
            let n: GoalNode
            switch op {
            case "add": n = try goalAdd(q["text"] ?? "", for: target, by: "owner")
            case "change":
                guard let id else { throw ToolError(description: "id needed") }
                n = try goalChange(id, q["text"] ?? "", by: "owner")
            case "done", "drop":
                guard let id else { throw ToolError(description: "id needed") }
                let e = op == "done" ? GoalNode.Evidence(quote: "主人在儀表板標的", ts: wallClock.now(), by: "owner") : nil
                n = try goalEnd(id, how: op, evidence: e, by: "owner")
            default: throw ToolError(description: "unknown op \(op)")
            }
            let what = switch op {
            case "add": "加了 \(nodeLine(n))"
            case "change": "把 #\(n.id) 改成「\(n.text)」"
            case "done": "把 \(nodeLine(n)) 記成做成過了"
            default: "放下了 \(nodeLine(n))"
            }
            // A sealed tree's texts stay out of the commit message: the op and the id only.
            commitHistory(.owner, what: "圖：" + (sealing ? "\(op) #\(n.id)" : what), why: "主人在儀表板改的")
            toldOwnerEdit("【主人改了你的圖】他\(what)。整張圖用 goal.list 看。")
            return n
        }
    }

    public func dashboardEvaluate(_ id: Int, _ kind: String) throws -> RecordEntry {
        let good: Bool? = switch kind {
        case "good": true
        case "bad": false
        case "none": nil
        default: throw ToolError(description: "kind is good, bad or none")
        }
        let e = try sealedEdit(recordURL) { try evaluate(id, good: good, owner: true, quote: nil) }
        sealedEdit(recordURL) {
            if let good { toldOwnerEdit("【主人的評價】你做完的「\(e.text)」（紀錄 #\(e.id)），主人評\(good ? "做得好" : "不對")。") }
            commitHistory(.owner, what: "評價紀錄 #\(e.id)：\(kind)", why: "主人在儀表板評的")
        }
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
        今天的對話裡標「｜自己的話」的行是你自己想的、說的，不算事實的來源：根據要是真的發生了的事（主人或別人說的、工具的結果、行事曆、系統的開機通知）；只有你自己說過、別的地方都沒有的事，不能當根據。
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
        "設定（固定的）：\n\(pack)\n\n你現在想成為的樣子：\n\(ideal)\n\n你想做的事和會的、不會的：\n\(tree)\n\n今天做完的事：\n"
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
