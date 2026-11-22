import Foundation

/// Her diary and what she knows of herself (2026-11-23, owner: 「我想要她有反省能力，知道自己的長短處，
/// 目標樹的品質會提升，這樣 doll 就可以更合理的成長」). Follows `[maintenance]` and `[drives]`; off
/// with either, or with `[reflection] enabled = false`.
///
///   diary           `<data_dir>/diary/YYYY-MM-DD.md` (a second maintenance on the same day: `-2`, …):
///                   written by her, for herself, in her own turn at the start of the maintenance
///                   (`diary.write`, the whole entry; owner decisions 1–3). The owner reads it in the
///                   dashboard; it is never sent to him. A day that touched a secret stores it sealed.
///                   She reads it any time (`diary.read`); `recall` adds the nearest passages, marked as
///                   her own words (never a source of fact: the audit and the consolidation already
///                   treat her own lines that way).
///   self-knowledge  `<data_dir>/self-knowledge.json`: her strengths and weaknesses, each with evidence
///                   the program finds (a quote of the day that is not hers, or `紀錄 #N`). A weakness she
///                   works on gets a habit (「什麼情況→怎麼做」, always in her system prompt after the
///                   ideal self, at most `habits_max`) or a goal of her tree (long work). The maintenance
///                   task `recur` records each day a weakness showed again (quoted). Only she moves one
///                   to 以前有 (`self.past`, with why); nothing disappears by itself (owner, 2026-11-23).
///
/// She changes all of it only in the diary turn (`diaryOnly`); reading is any time.
public struct SelfItem: Sendable, Codable, Equatable {
    public struct Evidence: Sendable, Codable, Equatable {
        public var quote: String
        public var ts: Double
        /// "her" (her note) or "maintenance" (the `recur` task).
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
    /// "strength" or "weakness".
    public var kind: String
    public var text: String
    public var evidence: [Evidence]
    public var created: Double
    public var habit: Habit?
    /// The goal-tree node she works on it with.
    public var goal: Int?
    /// A weakness: each day it showed again after it was noted.
    public var again: [Evidence] = []
    /// Moved to 以前有 by her.
    public var past: Past?
}

public struct SelfFile: Sendable, Codable, Equatable {
    var nextId = 1
    public var items: [SelfItem] = []
}

/// One passage of her diary in the recall index.
struct DiaryPassage {
    var file: String
    var text: String
    var vector: [Float]
    var sealed: Bool
}

enum Reflection {
    static let diaryDir = "diary"
    static let selfFileName = "self-knowledge.json"
    /// The task name in `maintenance.json`'s `done` once her diary turn is over.
    static let diaryTask = "diary"
    static let habitsHeader = "你的習慣（你從自己的短處定的；碰到那個情況就照做）："

    /// Allowed in the diary turn; everything else is refused there.
    static let diaryTurnTools: Set<String> = ["diary.write", "diary.read", "self.read", "self.note", "self.habit", "self.goal", "self.past",
                                              "recall", "think", "goal.list", "goal.add", "goal.change", Tools.Name.historyList]
    /// Only in the diary turn.
    static let diaryOnly: Set<String> = ["diary.write", "self.note", "self.habit", "self.goal", "self.past"]

    /// The diary files of a data dir, oldest first.
    static func diaryFiles(_ data: URL) -> [URL] {
        let dir = data.appending(path: diaryDir)
        return ((try? FileManager.default.contentsOfDirectory(atPath: dir.path)) ?? [])
            .filter { $0.hasSuffix(".md") && !$0.hasPrefix(".") }.sorted(by: fileOrder).map { dir.appending(path: $0) }
    }

    /// `2026-11-22.md` before `2026-11-22-2.md` before `2026-11-23.md`.
    static func fileOrder(_ a: String, _ b: String) -> Bool {
        func key(_ s: String) -> (String, Int) {
            let base = s.replacingOccurrences(of: ".md", with: "")
            return (String(base.prefix(10)), base.count > 11 ? Int(base.dropFirst(11)) ?? 0 : 1)
        }
        return key(a) < key(b)
    }

    /// The day a diary file is of.
    static func fileDay(_ name: String) -> String { String(name.prefix(10)) }

    static func notice(now: String, list: String, habitsMax: Int, forced: String) -> String {
        "【系統維護】開始，現在 \(now)。維護之前先寫今天的日記：用 diary.write 寫一篇，寫給你自己的（主人在後台看得到，但不會傳給他）。"
            + "可以寫的方向，不用每個都寫：今天發生了什麼、你的感受、你學到什麼、明天在想什麼。"
            + "寫完看看你的長處和短處（在下面）：要加就用 self.note，每一條都要有根據：今天對話裡一字不差的一段（主人或別人說的、工具的結果、程式的通知；不能是你自己說的），或「紀錄 #編號」（goal.list 下面那些做完的事）。已經記過的短處今天又出現了，不要另記一條：系統維護會自己記它又出現。"
            + "想改的短處：是碰到某個情況就該怎麼做的，用 self.habit 定一個習慣（最多 \(habitsMax) 條，會一直在你眼前）；要長期下功夫的，用 goal.add 加目標，再用 self.goal 連起來。"
            + "覺得一個短處已經改掉了，用 self.past 說理由（只有你自己能移，不會自己消失）。"
            + "這個回合只能寫日記、看自己、改習慣和目標，不能傳訊息、不能用電腦。寫完了就不要再呼叫工具，維護接著做，那段時間你不在。"
            + forced + "\n\n你現在的長處和短處：\n" + list
    }

    // the maintenance task `recur` ------------------------------------------------------------------

    struct RecurAnswer: Sendable {
        var found: [(id: Int, quote: String)]
        var reply: ModelReply
        var seconds: Double
    }

    static func recurSystem(name: String) -> String {
        """
        你是\(name)。現在是每天的系統維護，你看你自己記下的短處，今天有沒有又出現。
        只算今天對話裡真的看得到的：主人或別人說的、工具的結果（像「失敗：…」）、程式的通知。標「｜自己的話」的行是你自己說的，不能當根據。
        一個短處今天又出現了，就寫一行：
        #編號｜今天對話裡一字不差的一段（撐住它的那段，十幾個字）
        一個短處只寫一行。都沒有就只回：
        沒有
        """
    }

    static func recurUser(weaknesses: [SelfItem], day: String) -> String {
        "你的短處：\n" + weaknesses.map { "#\($0.id) \($0.text)" + ($0.habit.map { "（習慣：\($0.when)→\($0.then)）" } ?? "") }.joined(separator: "\n")
            + "\n\n今天的對話（最後一段）：\n\(day)"
    }

    static func parseRecur(_ reply: String) -> [(id: Int, quote: String)] {
        var out: [(Int, String)] = []
        for line in reply.split(separator: "\n") {
            let t = line.trimmingCharacters(in: .whitespaces)
            guard t.hasPrefix("#"), let bar = t.firstIndex(where: { $0 == "｜" || $0 == "|" }),
                  let id = Int(t[t.index(after: t.startIndex)..<bar].trimmingCharacters(in: .whitespaces)) else { continue }
            var quote = t[t.index(after: bar)...].trimmingCharacters(in: .whitespaces)
            for (o, c) in [("「", "」"), ("\"", "\""), ("“", "”")] where quote.hasPrefix(o) && quote.hasSuffix(c) && quote.count > 2 {
                quote = String(quote.dropFirst().dropLast())
            }
            if !quote.isEmpty, !out.contains(where: { $0.0 == id }) { out.append((id, quote)) }
        }
        return out
    }
    /// Every passage (a paragraph) of every diary file of a data dir, embedded.
    static func index(data: URL, box: SecretBox, embedder: any Embedder) throws -> [DiaryPassage] {
        var passages: [(String, String, Bool)] = []
        for url in diaryFiles(data) {
            let sealed = Concerns.boxed(url)
            guard let d = try Concerns.readDocument(url, box: box) else { continue }
            for p in Doll.paragraphs(String(decoding: d, as: UTF8.self)) { passages.append((url.lastPathComponent, p, sealed)) }
        }
        let vectors = passages.isEmpty ? [] : try embedder.embed(passages.map(\.1), as: .document)
        return zip(passages, vectors).map { DiaryPassage(file: $0.0, text: $0.1, vector: $1, sealed: $0.2) }
    }
}


extension Doll {
    // storage -----------------------------------------------------------------------------------------

    var reflectionOn: Bool { reflectionConfig.enabled && maintenanceConfig.enabled && drivesConfig.enabled }

    func saveSelf() throws { try saveDocument(selfFile, selfURL) }

    /// Every passage (a paragraph) of every diary file, embedded: at boot and after the history wrote files.
    func rebuildDiaryIndex() throws {
        diaryIndex = reflectionOn ? try Reflection.index(data: dataDir, box: memory.box, embedder: embedder) : []
    }

    func diaryURL(_ name: String) -> URL { diaryDirURL.appending(path: name) }

    /// The text of a diary file, opened when sealed; and whether it was.
    func readDiary(_ url: URL) throws -> (text: String, sealed: Bool) {
        let sealed = Concerns.boxed(url)
        guard let data = try Concerns.readDocument(url, box: memory.box) else { throw StoreError(description: "\(url.path) is gone") }
        return (String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines), sealed)
    }

    func indexDiary(_ name: String, _ text: String, sealed: Bool) throws {
        diaryIndex.removeAll { $0.file == name }
        let ps = Self.paragraphs(text)
        guard !ps.isEmpty else { return }
        for (p, v) in zip(ps, try embedder.embed(ps, as: .document)) { diaryIndex.append(DiaryPassage(file: name, text: p, vector: v, sealed: sealed)) }
    }

    static func paragraphs(_ text: String) -> [String] {
        text.components(separatedBy: "\n\n").map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
    }

    /// `recall`'s part from her diary: the `[reflection] recall_max` passages nearest the query that
    /// the turn may see (a sealed one only where the secret folder is visible). Empty: none.
    func diaryRecall(_ query: String, visible: Set<MemoryFolder>) throws -> (text: String, sealed: Bool) {
        guard reflectionOn, reflectionConfig.recallMax > 0 else { return ("", false) }
        let pool = diaryIndex.filter { !$0.sealed || visible.contains(.secret) }
        guard !pool.isEmpty else { return ("", false) }
        let q = try embedder.embed(query, as: .query)
        func cos(_ v: [Float]) -> Double { zip(v, q).reduce(0) { $0 + Double($1.0) * Double($1.1) } }
        let best = pool.map { ($0, cos($0.vector)) }.sorted { $0.1 > $1.1 }.prefix(reflectionConfig.recallMax).map(\.0)
        let text = "\n\n你的日記裡最接近的 \(best.count) 段（你自己寫的話，不是事實的來源）："
            + best.map { "\n\n【日記 \(Reflection.fileDay($0.file))】\($0.text)" }.joined()
        return (text, best.contains(where: \.sealed))
    }

    // the diary turn ----------------------------------------------------------------------------------

    var inDiaryTurn: Bool { maintenanceState.diary != nil }

    /// Her diary turn is still to come in this maintenance.
    var diaryDue: Bool { reflectionOn && dayToConsolidate && !maintenanceState.done.contains(Reflection.diaryTask) }

    /// The diary file of this maintenance: chosen once (kept across an interruption), the day the
    /// timeline began; a day that already has one gets `-2`, `-3`, ….
    func diaryFileName() -> String {
        if let f = maintenanceState.diaryFile { return f }
        let day = Day(ts: timeline.events.first?.ts ?? wallClock.now()).description
        var name = "\(day).md", n = 2
        while FileManager.default.fileExists(atPath: diaryURL(name).path) {
            name = "\(day)-\(n).md"
            n += 1
        }
        return name
    }

    /// The maintenance begins with her turn: the notice gives it.
    func startDiaryTurn(forced: Bool) {
        let now = wallClock.now()
        maintenanceState.diary = now
        maintenanceState.diaryTurns = 0
        maintenanceState.diaryFile = diaryFileName()
        maintenanceState.forced = forced
        saveMaintenanceLogged()
        let kept = forced ? "你手上的「\(todo.current ?? "")」先停著，維護結束後接著做。" : ""
        put(Event(.memory, .internal, Reflection.notice(now: Self.hhmm(now), list: selfText(), habitsMax: reflectionConfig.habitsMax, forced: kept),
                  sealed: Concerns.boxed(selfURL)))
        log("INFO", "maintenance: started with her diary turn\(forced ? " (busy limit)" : ""); diary file \(maintenanceState.diaryFile!)")
        feed.notify(.maintenance)
    }

    /// Her diary turn is over (a reply with no call, or `[reflection] max_turns`): the tasks begin.
    func finishDiaryTurn() {
        let file = maintenanceState.diaryFile ?? ""
        let wrote = !file.isEmpty && FileManager.default.fileExists(atPath: diaryURL(file).path)
        log("INFO", "maintenance: diary turn over after \(maintenanceState.diaryTurns ?? 0) turns; " + (wrote ? "diary \(file) written" : "no diary today"))
        maintenanceState.diary = nil
        maintenanceState.done.append(Reflection.diaryTask)
        beginMaintenanceTasks(forced: maintenanceState.forced)
    }

    /// Why a call is refused for where she is (diary turn or not); nil: it runs.
    func diaryRefusal(_ name: String) -> String? {
        guard reflectionOn else { return nil }
        if inDiaryTurn, !Reflection.diaryTurnTools.contains(name) {
            return "失敗：系統維護中，這個回合只寫日記、看自己、改習慣和目標，\(name) 沒有執行。寫完了就不要再呼叫工具，維護接著做。"
        }
        if !inDiaryTurn, Reflection.diaryOnly.contains(name) {
            return "失敗：\(name) 只在系統維護開始、寫日記的那個回合能用。現在想到的，" + (drivesConfig.enabled ? "用 concern.note 記著，" : "先記著，") + "到時候再寫。"
        }
        return nil
    }

    // the tools -----------------------------------------------------------------------------------------

    func diaryWriteTool(_ text: String) throws -> Outcome {
        guard text.count <= reflectionConfig.diaryMaxChars else {
            throw ToolError(description: "沒寫：太長了（\(text.count) 字，最多 \(reflectionConfig.diaryMaxChars)）")
        }
        let name = maintenanceState.diaryFile ?? diaryFileName()
        try FileManager.default.createDirectory(at: diaryDirURL, withIntermediateDirectories: true)
        let url = diaryURL(name)
        let again = FileManager.default.fileExists(atPath: url.path)
        let key = sealKey(url)
        try Concerns.writeDocument(Data((text + "\n").utf8), url, box: key)
        try indexDiary(name, text, sealed: key != nil)
        feed.notify(.drives)
        return Outcome(text: (again ? "日記整篇換成這次寫的了" : "日記寫好了") + "（\(Reflection.fileDay(name))，\(text.count) 字）。", actions: ["diary write"])
    }

    func diaryReadTool(_ date: String?, seen events: [Event]) throws -> Outcome {
        let all = Reflection.diaryFiles(dataDir)
        let picked: [URL]
        if let date {
            guard Day(date) != nil else { throw ToolError(description: "date 要寫成 2026-11-22 這樣") }
            picked = all.filter { Reflection.fileDay($0.lastPathComponent) == date }
            guard !picked.isEmpty else {
                let days = Array(Set(all.map { Reflection.fileDay($0.lastPathComponent) })).sorted().suffix(10)
                return Outcome(text: "\(date) 沒有日記。" + (days.isEmpty ? "你還沒有寫過日記。" : "有日記的日子（最近的）：\(days.joined(separator: "、"))"), actions: ["diary read"])
            }
        } else {
            picked = Array(all.suffix(reflectionConfig.readDays))
            guard !picked.isEmpty else { return Outcome(text: "你還沒有寫過日記。", actions: ["diary read"]) }
        }
        let visible = Memory.visible(Self.audience(events), sawSecret: events.contains(where: \.secret))
        var parts: [String] = [], sealed = false, hidden = 0
        for url in picked {
            let (text, s) = try readDiary(url)
            if s && !visible.contains(.secret) { hidden += 1; continue }
            sealed = sealed || s
            parts.append("【日記 \(Reflection.fileDay(url.lastPathComponent))】\n\(text)")
        }
        var out = parts.joined(separator: "\n\n")
        if hidden > 0 { out += (out.isEmpty ? "" : "\n\n") + "（另外 \(hidden) 篇有秘密，這個回合看不到）" }
        return Outcome(text: out, secret: sealed, sealed: sealed, actions: ["diary read"])
    }

    func selfReadTool() -> Outcome {
        Outcome(text: "你的長處和短處：\n" + selfText(), sealed: Concerns.boxed(selfURL), actions: ["self read"])
    }

    /// Evidence she gives: `紀錄 #N` of her record, or a quote found in an event of this day that is
    /// not her own (her replies, her diary and self-knowledge read back, 【相關記憶】, the diary notice).
    /// Its time is the event's (the newest one holding the quote) or the record's.
    func checkedEvidence(_ raw: String, by: String) throws -> SelfItem.Evidence {
        let t = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if t.hasPrefix("紀錄"), let n = Int(t.dropFirst(2).trimmingCharacters(in: .whitespaces).trimmingCharacters(in: CharacterSet(charactersIn: "#＃ "))) {
            guard let e = recordFile.entries.first(where: { $0.id == n }) else { throw ToolError(description: "沒有紀錄 #\(n)（goal.list 下面列著做完的事和編號）") }
            return SelfItem.Evidence(quote: "紀錄 #\(n)：\(e.text)", ts: e.ts, by: by)
        }
        var quote = t
        for (o, c) in [("「", "」"), ("\"", "\""), ("“", "”")] where quote.hasPrefix(o) && quote.hasSuffix(c) && quote.count > 2 {
            quote = String(quote.dropFirst().dropLast())
        }
        guard quote.count >= reflectionConfig.quoteMinChars else {
            throw ToolError(description: "根據太短了：照抄今天對話裡撐住它的一段（至少 \(reflectionConfig.quoteMinChars) 個字），或寫「紀錄 #編號」")
        }
        guard let ev = notHers(timeline.events).last(where: { $0.text.contains(quote) }) else {
            throw ToolError(description: "今天的對話裡找不到「\(quote.prefix(40))」：要一字不差照抄主人或別人說的、工具的結果、程式的通知（你自己說的不算）；或寫「紀錄 #編號」")
        }
        return SelfItem.Evidence(quote: quote, ts: ev.ts, by: by)
    }

    /// The events of the day that can be evidence.
    func notHers(_ events: [Event]) -> [Event] {
        let names = Self.callNames(events)
        let echo: Set<String> = ["diary.read", "diary.write", "self.read", "self.note", "self.habit", "self.goal", "self.past", "recall", "think", "remember", "history.list"]
        return events.filter { ev in
            switch ev.kind {
            case .doll: return false
            case .tool: return !echo.contains(names[ev.seq ?? -1] ?? "")
            case .memory: return !ev.text.hasPrefix("【相關記憶】") && !ev.text.hasPrefix("【系統維護】開始")
            case .owner, .developer, .watch, .calendar: return true
            }
        }
    }

    func selfItem(_ id: Int) throws -> SelfItem {
        guard let it = selfFile.items.first(where: { $0.id == id }) else { throw ToolError(description: "沒有 #\(id)（self.read 看編號）") }
        return it
    }

    func updateSelf(_ id: Int, _ change: (inout SelfItem) -> Void) throws {
        var f = selfFile
        change(&f.items[f.items.firstIndex { $0.id == id }!])
        try saveDocument(f, selfURL)
        selfFile = f
        feed.notify(.drives)
    }

    func selfNoteTool(kind: String, text: String, evidence: String) throws -> Outcome {
        guard text.count <= reflectionConfig.itemMaxChars else {
            throw ToolError(description: "太長了（\(text.count) 字，最多 \(reflectionConfig.itemMaxChars)）：寫成一句")
        }
        let current = selfFile.items.filter { $0.past == nil }
        guard current.count < reflectionConfig.itemsMax else {
            throw ToolError(description: "已經有 \(current.count) 條了（最多 \(reflectionConfig.itemsMax)）：改掉的用 self.past 移到以前有，再加")
        }
        if let same = current.first(where: { $0.text == text }) { throw ToolError(description: "已經有了（#\(same.id)）") }
        let e = try checkedEvidence(evidence, by: "her")
        var f = selfFile
        let item = SelfItem(id: f.nextId, kind: kind, text: text, evidence: [e], created: wallClock.now())
        f.nextId += 1
        f.items.append(item)
        try saveDocument(f, selfURL)
        selfFile = f
        feed.notify(.drives)
        return Outcome(text: "記下了：#\(item.id)〔\(kind == "strength" ? "長處" : "短處")〕\(text)。", actions: ["self note"])
    }

    func selfHabitTool(id: Int, when: String?, then: String?, why: String?) throws -> Outcome {
        let it = try selfItem(id)
        guard it.kind == "weakness", it.past == nil else { throw ToolError(description: "#\(id) 不是還在的短處；習慣是從短處定的") }
        let w = (when ?? "").trimmingCharacters(in: .whitespacesAndNewlines), d = (then ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if w.isEmpty && d.isEmpty {
            guard it.habit != nil else { throw ToolError(description: "#\(id) 沒有習慣；要定一個就給 when 和 do") }
            guard let why, !why.trimmingCharacters(in: .whitespaces).isEmpty else { throw ToolError(description: "拿掉習慣要寫 why") }
            try updateSelf(id) { $0.habit = nil }
            return Outcome(text: "#\(id) 的習慣拿掉了，下一回合起就不在你眼前了。", actions: ["self habit off"])
        }
        guard !w.isEmpty, !d.isEmpty else { throw ToolError(description: "when（什麼情況）和 do（怎麼做）都要寫") }
        guard w.count + d.count <= reflectionConfig.habitMaxChars else {
            throw ToolError(description: "太長了（\(w.count + d.count) 字，最多 \(reflectionConfig.habitMaxChars)）：寫成一句")
        }
        let habits = selfFile.items.filter { $0.past == nil && $0.habit != nil && $0.id != id }.count
        guard habits < reflectionConfig.habitsMax else {
            throw ToolError(description: "已經有 \(habits) 條習慣了（最多 \(reflectionConfig.habitsMax)）：先拿掉一條（self.habit 只給 id 和 why），或把改掉的短處用 self.past 移走")
        }
        try updateSelf(id) { $0.habit = SelfItem.Habit(when: w, then: d, since: wallClock.now()) }
        return Outcome(text: "#\(id) 的習慣定好了：\(w)→\(d)。從下一回合起一直在你眼前。", actions: ["self habit"])
    }

    func selfGoalTool(id: Int, goal: Int) throws -> Outcome {
        let it = try selfItem(id)
        guard it.kind == "weakness", it.past == nil else { throw ToolError(description: "#\(id) 不是還在的短處") }
        guard let n = node(goal), n.ended == nil else { throw ToolError(description: "目標樹上沒有還開著的 #\(goal)（goal.list 看）") }
        try updateSelf(id) { $0.goal = goal }
        return Outcome(text: "短處 #\(id) 連到目標樹的 \(nodeLine(n))。", actions: ["self goal"])
    }

    func selfPastTool(id: Int, why: String) throws -> Outcome {
        let it = try selfItem(id)
        guard it.past == nil else { throw ToolError(description: "#\(id) 已經在以前有了") }
        try updateSelf(id) { $0.past = SelfItem.Past(why: why, ts: wallClock.now()); $0.habit = nil }
        return Outcome(text: "#\(id)「\(it.text)」移到以前有了（\(selfStats(it))）。" + (it.habit != nil ? "它的習慣也拿掉了。" : ""), actions: ["self past"])
    }

    // words ---------------------------------------------------------------------------------------------

    /// A weakness's record since it was noted (or since its habit): how often it showed again, and
    /// how long since it last did.
    func selfStats(_ it: SelfItem) -> String {
        guard it.kind == "weakness" else { return "從 \(Self.historyTime(it.created)) 記的" }
        let from = it.habit?.since ?? it.created
        let after = it.again.filter { $0.ts >= from }
        let lastSeen = it.again.last?.ts ?? it.created
        let days = Int((wallClock.now() - lastSeen) / 86400)
        var s = it.habit != nil ? "定了習慣以後又出現 \(after.count) 次" : "記下以後又出現 \(after.count) 次"
        if let last = it.again.last { s += "，最近一次 \(Self.historyTime(last.ts))「\(last.quote.prefix(30))」" }
        if Double(days) >= reflectionConfig.weaknessQuietDays { s += "；已經 \(days) 天沒再出現" }
        return s
    }

    func selfText() -> String {
        let cur = selfFile.items.filter { $0.past == nil }, old = selfFile.items.filter { $0.past != nil }
        func line(_ it: SelfItem) -> String {
            var s = "- #\(it.id) \(it.text)（根據：\(it.evidence.first.map { "「\($0.quote.prefix(40))」" } ?? "—")；\(selfStats(it))）"
            if let h = it.habit { s += "\n  習慣：\(h.when)→\(h.then)" }
            if let g = it.goal { s += "\n  目標：" + (node(g).map { nodeLine($0) + ($0.ended == nil ? "" : "（\($0.end == "done" ? "做完了" : "放下了")）") } ?? "#\(g)（不在樹上了）") }
            return s
        }
        let strengths = cur.filter { $0.kind == "strength" }, weaknesses = cur.filter { $0.kind == "weakness" }
        var out = "長處：\n" + (strengths.isEmpty ? "（還沒有）" : strengths.map(line).joined(separator: "\n"))
        out += "\n短處：\n" + (weaknesses.isEmpty ? "（還沒有）" : weaknesses.map(line).joined(separator: "\n"))
        if !old.isEmpty {
            out += "\n以前有（你移過來的）：\n" + old.suffix(10).map { "- #\($0.id) \($0.text)（\(Self.historyTime($0.past!.ts))：\($0.past!.why)）" }.joined(separator: "\n")
        }
        return out
    }

    /// The system prompt's part after the ideal self; empty when she has none.
    func habitsText() -> String {
        let hs = selfFile.items.filter { $0.past == nil && $0.habit != nil }
        guard reflectionOn, !hs.isEmpty else { return "" }
        return "\(Reflection.habitsHeader)\n" + hs.map { "- \($0.habit!.when)→\($0.habit!.then)" }.joined(separator: "\n") + "\n\n"
    }

    // the maintenance task `recur` ----------------------------------------------------------------------

    var recurDue: Bool { reflectionOn && dayToConsolidate && selfFile.items.contains { $0.kind == "weakness" && $0.past == nil } }

    func prepareRecur() async throws -> Reflection.RecurAnswer {
        let weak = selfFile.items.filter { $0.kind == "weakness" && $0.past == nil }
        var day = Consolidation.tasks(timeline.events).map(\.text).joined(separator: "\n\n")
        if day.count > goalsConfig.reviewChars { day = String(day.suffix(goalsConfig.reviewChars)) }
        let messages = [ChatMessage(role: "system", content: Reflection.recurSystem(name: name)),
                        ChatMessage(role: "user", content: Reflection.recurUser(weaknesses: weak, day: day))]
        let started = Date()
        let reply = try await maintenanceModel.chat(messages, tools: nil, effort: config.consolidate.effort)
        return Reflection.RecurAnswer(found: Reflection.parseRecur(reply.content), reply: reply, seconds: Date().timeIntervalSince(started))
    }

    func commitRecur(_ a: Reflection.RecurAnswer) throws -> MaintenanceDone {
        var f = selfFile, kept: [String] = [], refused: [String] = []
        for (id, quote) in a.found {
            guard let i = f.items.firstIndex(where: { $0.id == id && $0.kind == "weakness" && $0.past == nil }) else { refused.append("#\(id) 不是還在的短處"); continue }
            guard let e = try? checkedEvidence(quote, by: "maintenance") else { refused.append("#\(id) 的根據找不到「\(quote.prefix(30))」"); continue }
            // Showing again means after it was noted (m18, 2026-11-23: the line she noted it from was
            // counted as its first recurrence the same night), and not the same event twice.
            guard e.ts >= f.items[i].created, e.ts > f.items[i].again.last?.ts ?? 0 else {
                refused.append("#\(id) 的「\(quote.prefix(30))」是記下它之前（或已經算過）的事"); continue
            }
            f.items[i].again.append(e)
            kept.append("#\(id)")
        }
        if !kept.isEmpty {
            try saveDocument(f, selfURL)
            selfFile = f
        }
        let u = a.reply.usage
        let stats = "request \(u.promptTokens) prompt tokens, \(u.completionTokens) completion, \(String(format: "%.1f", a.seconds)) s"
        return MaintenanceDone(log: "again \(kept)\(refused.isEmpty ? "" : ", refused \(refused)"); \(stats)",
                               words: kept.isEmpty ? "你的短處今天沒有看到又出現的。" : "你的短處今天又出現的：\(kept.joined(separator: "、"))（self.read 看）。",
                               summary: nil, kept: nil)
    }

    // the dashboard -------------------------------------------------------------------------------------

    public func dashboardReflection() throws -> DashboardReflection {
        guard reflectionOn else { return DashboardReflection(enabled: false, diary: [], items: [], habitsMax: 0, quietDays: 0) }
        let diary = try Reflection.diaryFiles(dataDir).reversed().prefix(reflectionConfig.dashboardDays).map { url in
            let (text, sealed) = try readDiary(url)
            return DashboardReflection.Entry(day: Reflection.fileDay(url.lastPathComponent), file: url.lastPathComponent, text: text, sealed: sealed)
        }
        let items = selfFile.items.map { DashboardReflection.Item(item: $0, stats: selfStats($0), goal: $0.goal.flatMap(node).map(nodeLine)) }
        return DashboardReflection(enabled: true, diary: Array(diary), items: items, habitsMax: reflectionConfig.habitsMax,
                                   quietDays: reflectionConfig.weaknessQuietDays)
    }
}

/// The dashboard's view of her diary and self-knowledge (read only: the owner undoes in the history).
public struct DashboardReflection: Sendable, Codable {
    public struct Entry: Sendable, Codable {
        public var day: String
        public var file: String
        public var text: String
        public var sealed: Bool
    }
    public struct Item: Sendable, Codable {
        public var item: SelfItem
        public var stats: String
        public var goal: String?
    }
    public var enabled: Bool
    public var diary: [Entry]
    public var items: [Item]
    public var habitsMax: Int
    public var quietDays: Double
}
