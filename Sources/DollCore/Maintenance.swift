import Foundation

/// System maintenance (2026-11-16, owner: 「直接說是系統維護，進行期間跟睡著一下」). Once a day the
/// program tidies her memory and writes the day's summary; she takes no turns meanwhile and is told
/// plainly before and after. It replaces her sleep (2026-11-15): no `sleep` tool, no 睡意, no
/// 「睡覺」／「起床」, no resting until morning, no idle backstop.
///
/// When: her calendar holds one daily item 「系統維護」 (role `maintenance`, `[maintenance] time`
/// when the calendar has none). It can be moved like any item, also for one day; not deleted or
/// made all-day. Its start makes maintenance *due*; it gives her no turn.
///
/// Due, it starts as soon as she is idle (`maintenanceIdle`): no turn running, nothing queued, no
/// tool result waiting for her, no todo item in progress (one that only waits does not count, `todoBusy`). So a list in progress or a turn
/// running only defers it; the check runs after every turn and on the timer. Busy past
/// `busy_limit_hours` since it came due, it starts when the current turn ends anyway (nothing
/// queued): the todo is carried into the new day and the end notice gives her a turn to resume it.
///
/// Interrupted (the owner's line, written or spoken, or the developer's): the running task is
/// cancelled, its model request with it, and writes nothing; the notice
/// 【系統維護】被打斷了… lands before the line, which gives her the turn. It stays due and runs
/// again once nobody has spoken to her for `retry_idle_min` and she is idle; failing that, the next
/// day's time finds it still due. Why retry the same night: a short talk at 03:00 should not leave
/// the day unconsolidated, doubling tomorrow's timeline.
///
/// A task is all-or-nothing: `prepare` (async, cancellable, writes nothing) then `commit`
/// (synchronous on the actor, so no interruption can come between its writes). Adding a task: a
/// `MaintenanceTask` case, its `due`, `prepare` and `commit` arms; the flow does not change.
///
/// `<data_dir>/maintenance.json` holds whether it is due (since when), running, interrupted, and
/// the tasks this maintenance finished: a restart while running goes on with the tasks not done.

/// What `maintenance.json` holds.
struct MaintenanceFile: Codable, Equatable {
    /// Since when it is due (its time came); nil: not due.
    var due: Double?
    /// Running since this time; nil: not running.
    var running: Double?
    /// Not before this time: she was told at its time and has `notice_min` minutes first.
    var notBefore: Double?
    /// Interrupted since it came due: waits for `retry_idle_min` of quiet.
    var interrupted = false
    /// The busy limit made it start with a todo in hand: the end notice gives her a turn.
    var forced = false
    /// This maintenance's tasks already done (their writes stand).
    var done: [String] = []
    /// This maintenance's tasks that failed, with the error (the next maintenance tries again).
    var failed: [String: String] = [:]
    /// What the tasks done so far did, in her words, for the end notice (kept across a restart).
    var words: [String]?
    /// Her diary turn (2026-11-23, `Reflection.swift`) runs since this time; nil: not in it.
    var diary: Double?
    /// Her replies in the diary turn so far.
    var diaryTurns: Int?
    /// This maintenance's diary file (kept across an interruption, so a retry writes the same one).
    var diaryFile: String?
}

/// The maintenance's tasks, run in this order.
public enum MaintenanceTask: String, CaseIterable, Sendable {
    /// Concerns from the day's conversation (`ConcernExtraction`); before consolidation, which
    /// closes the day's timeline.
    case concerns
    /// The day against her ideal self and tree (`IdealReview`, phase 2): a revision only when the
    /// day gives a reason, at most every `[goals] revise_min_days`; before consolidation, which
    /// closes the day's timeline.
    case review
    /// Her new memories checked against where they came from (`Audit.swift`); before
    /// consolidation, so its corrections are in place before consolidation recalls related memories.
    case audit
    /// Her weaknesses against the day (`Reflection.swift`, 2026-11-23): each one that showed again,
    /// with the quote; before consolidation, which closes the day's timeline.
    case recur
    /// The timeline so far into long-term memory, then a new segment opened by the end notice and
    /// the day's summary.
    case consolidate

    /// How the notices name it.
    var label: String {
        switch self {
        case .concerns: "從今天的對話記掛念"
        case .review: "回顧今天、看想成為的樣子要不要改"
        case .audit: "查對新記的記憶"
        case .recur: "看你的短處今天有沒有又出現"
        case .consolidate: "整理記憶、寫摘要"
        }
    }
}

/// One committed task's report: a line for the log; for the end notice what was done in words and
/// the day's summary; the todo carried into the new day (busy limit).
struct MaintenanceDone {
    var log: String
    var words: String
    var summary: String?
    var kept: TodoState?
    /// The list set aside beside `kept`, carried with it.
    var keptAside: ListAside? = nil
    /// Its words or summary carry content of secrets or of the encrypted folders (2026-11-21): the
    /// words are kept encrypted in `maintenance.json`, the end notice is stored encrypted.
    var sealed = false
}

/// One task's prepared work: what `commit` writes.
enum MaintenanceWork: Sendable {
    case concerns(ConcernExtraction.Answer)
    case review(IdealReview.Answer)
    case audit(Audit.Answer)
    case recur(Reflection.RecurAnswer)
    case consolidate(Consolidation.Request, Consolidation.Answer)
}

extension Doll {
    public var inMaintenance: Bool { maintenanceState.running != nil }

    static func readMaintenance(_ url: URL) throws -> MaintenanceFile? {
        guard FileManager.default.fileExists(atPath: url.path) else { return nil }
        do { return try JSONDecoder().decode(MaintenanceFile.self, from: Data(contentsOf: url)) } catch {
            throw StoreError(description: "\(url.path): not a maintenance file: \(error)")
        }
    }

    func saveMaintenance() throws {
        let enc = JSONEncoder()
        enc.outputFormatting = [.prettyPrinted, .sortedKeys]
        try enc.encode(maintenanceState).write(to: maintenanceURL, options: .atomic)
    }

    func saveMaintenanceLogged() {
        do { try saveMaintenance() } catch { log("ERROR", "maintenance: saving \(maintenanceURL.path) failed: \(error)") }
    }

    /// The 「系統維護」 item when the calendar has none. A calendar from before 2026-11-16 holds the
    /// sleep's 「睡覺」／「起床」 (roles `sleep`, `wake`): they are deleted here and the maintenance
    /// item takes their place; the boot log says so. Runs once the calendar is caught up.
    func ensureMaintenanceItem() throws {
        let removed = agenda.removeRoles(Agenda.removedRoles)
        if !removed.isEmpty {
            log("INFO", "calendar: removed the sleep items \(removed.joined(separator: ", ")) (sleep became system maintenance, 2026-11-16)")
        }
        var added: String?
        if maintenanceConfig.enabled, agenda.item(role: Agenda.maintenanceRole) == nil {
            agenda.addRole(Agenda.maintenanceRole, title: Agenda.maintenanceTitle, day: Day(ts: wallClock.now()),
                           time: TimeOfDay(maintenanceConfig.time)!)
            added = "\(Agenda.maintenanceTitle) \(maintenanceConfig.time)"
        }
        guard !removed.isEmpty || added != nil else { return }
        try agenda.save(box: sealKey(agenda.url))
        if let added { log("INFO", "maintenance: calendar had no \(added)\(removed.isEmpty ? "" : " (it replaces them)"); added (daily)") }
    }

    // her day --------------------------------------------------------------------------------------

    /// People's lines and her replies in the segment: what 【狀況】 counts and what consolidation needs.
    func dayCounts() -> (lines: Int, replies: Int) {
        var lines = 0, replies = 0
        for ev in timeline.events {
            if ev.speaker != nil { lines += 1 } else if ev.kind == .doll { replies += 1 }
        }
        return (lines, replies)
    }

    /// "The day has something to consolidate": a person's line or a reply of hers since the segment
    /// began (the boot and maintenance notices alone are not a day).
    var dayToConsolidate: Bool { let c = dayCounts(); return c.lines + c.replies > 0 }

    /// A list 進行中, in hand or set aside: maintenance waits for it (2026-11-21, owner: a list that
    /// only waits does not hold maintenance back; soak run: due at 03:00 with a list 等待中 since 07:01
    /// the day before, it started only at the busy limit).
    var todoBusy: Bool { todo.phase == .doing || aside?.todo.phase == .doing }

    /// A list she holds, in hand (進行中 or 等待中) or set aside: maintenance carries it into the new day.
    var todoHeld: Bool { todo.phase == .doing || todo.phase == .waiting || aside != nil }

    /// 【狀況】's lines: the day's size, and when maintenance comes (time left) or that it waits.
    func maintenanceStatusLines(now: Double) -> [String] {
        let c = dayCounts()
        var out = ["- 今天的時間線：別人跟你說了 \(c.lines) 句，你回了 \(c.replies) 則"]
        if maintenanceState.due != nil {
            out.append("- 系統維護：時間到了，等你手上沒事就開始（這段時間你不在）")
        } else if let it = agenda.item(role: Agenda.maintenanceRole), let next = agenda.nextStart(of: it.id, after: now) {
            let m = max(0, Int((next - now) / 60))
            out.append("- 系統維護：\(Self.nowText(next))（還有 \(Self.minutes(m))）")
        }
        return out
    }

    static func hhmm(_ ts: Double) -> String {
        let c = Day.cal.dateComponents([.hour, .minute], from: Date(timeIntervalSince1970: ts))
        return String(format: "%02d:%02d", c.hour!, c.minute!)
    }

    static func nowText(_ ts: Double) -> String { "\(Day(ts: ts).label) \(hhmm(ts))" }

    static func minutes(_ m: Int) -> String { m % 60 == 0 ? "\(m / 60) 小時" : m >= 60 ? "\(m / 60) 小時 \(m % 60) 分" : "\(m) 分鐘" }

    // when it starts -------------------------------------------------------------------------------

    /// The item's start: maintenance is due (no turn for her).
    func maintenanceFire() {
        guard maintenanceConfig.enabled else { return }
        if maintenanceState.running != nil { return log("INFO", "maintenance: its time came while it runs; nothing") }
        if maintenanceState.due != nil { return log("INFO", "maintenance: its time came while still due; nothing") }
        let now = wallClock.now()
        maintenanceState.due = now
        // She is told first and gets a turn (owner, 2026-11-16): to wrap up, tell someone, or move
        // today's maintenance (a change to the item while it has not started calls this one off).
        let lead = maintenanceConfig.noticeMin
        if lead > 0 {
            maintenanceState.notBefore = now + Double(lead) * 60
            put(Event(.calendar, .internal, withOwnerSleep(Self.maintenanceNotice(lead))))
        }
        saveMaintenanceLogged()
        log("INFO", "maintenance: due" + (lead > 0 ? "; she is told, it starts in \(lead) min" : ""))
        maintenanceCheck()
    }

    static func maintenanceNotice(_ lead: Int) -> String {
        "【系統維護】時間到了，\(minutes(lead))後開始：程式要從今天的對話記下掛著的事、整理你的記憶、寫今天的摘要，要幾分鐘，這段時間你不在。"
            + "要收尾、要跟誰說一聲，現在做；今天這個時間不方便，就用 calendar.change 把今天的系統維護往後移。"
            + "你手上沒事它就開始，不會再另外通知。"
    }

    /// The 「系統維護」 item was changed while this one was due and not started: it is called off and
    /// comes due again at the item's next time.
    func maintenanceItemChanged() {
        guard maintenanceState.due != nil, maintenanceState.running == nil else { return }
        maintenanceState = MaintenanceFile()
        saveMaintenanceLogged()
        log("INFO", "maintenance: its item was changed before it started; called off until the item's next time")
        maintenanceCheck()
    }

    /// Idle for maintenance: no turn running, nothing queued, no tool result waiting, no list 進行中
    /// (one 等待中 is carried into the new day).
    var maintenanceIdle: Bool { !inTurn && queue.isEmpty && !followUp && !todoBusy }

    /// Quiet long enough after an interruption.
    func retryAt() -> Double? {
        maintenanceState.interrupted ? lastSpoke + Double(maintenanceConfig.retryIdleMin) * 60 : nil
    }

    func busyLimitAt() -> Double? { maintenanceState.due.map { $0 + maintenanceConfig.busyLimitHours * 3600 } }

    /// After every turn, on its time, on the timer: starts it when it is due and she is idle (or
    /// busy past the limit); else arms the timer for the moment time alone can change that.
    func maintenanceCheck() {
        maintenanceTimerSerial += 1
        maintenanceTimer?.cancel()
        maintenanceTimer = nil
        guard maintenanceConfig.enabled, calendarCaughtUp, !stopped, maintenanceState.due != nil,
              maintenanceState.running == nil else { return }
        let now = wallClock.now()
        // Her diary turn: over once she is idle (her last reply called nothing, or its turns ran out).
        if maintenanceState.diary != nil {
            if !inTurn, queue.isEmpty, !followUp { finishDiaryTurn() }
            return
        }
        if let r = retryAt(), now < r { return armMaintenanceTimer(r) }
        if let n = maintenanceState.notBefore, now < n { return armMaintenanceTimer(n) }
        if maintenanceIdle { return startMaintenance(forced: false) }
        let limit = busyLimitAt()!
        if now >= limit, !inTurn, queue.isEmpty {
            log("INFO", "maintenance: busy \(maintenanceConfig.busyLimitHours) h since due (「\(todo.current ?? "")」\(todo.phase.label)); starts now, the todo kept", sealed: todoSealed)
            return startMaintenance(forced: true)
        }
        if now < limit { armMaintenanceTimer(limit) }
    }

    func armMaintenanceTimer(_ at: Double) {
        let serial = maintenanceTimerSerial, clock = wallClock
        maintenanceTimer = Task { [weak self] in
            do { try await clock.sleep(until: at) } catch { return }
            await self?.maintenanceTimerFired(serial)
        }
    }

    func maintenanceTimerFired(_ serial: Int) {
        guard serial == maintenanceTimerSerial else { return }
        maintenanceCheck()
    }

    /// Starts it: her diary turn first when it is due (2026-11-23), else the start notice and the
    /// tasks. Nothing to do: no notice, no longer due.
    func startMaintenance(forced: Bool) {
        guard diaryDue || MaintenanceTask.allCases.contains(where: { !maintenanceState.done.contains($0.rawValue) && maintenanceDue($0) }) else {
            log("INFO", "maintenance: nothing to do; not started")
            maintenanceState = MaintenanceFile()
            return saveMaintenanceLogged()
        }
        if diaryDue { return startDiaryTurn(forced: forced) }
        beginMaintenanceTasks(forced: forced)
    }

    /// The start notice, then the tasks.
    func beginMaintenanceTasks(forced: Bool) {
        let now = wallClock.now()
        taskTurn = false
        maintenanceState.running = now
        maintenanceState.forced = forced
        saveMaintenanceLogged()
        let wrote = maintenanceState.done.contains(Reflection.diaryTask)
        let text = (wrote ? "【系統維護】日記的回合結束了，接著做，現在 \(Self.hhmm(now))。" : "【系統維護】開始，現在 \(Self.hhmm(now))。")
            + "程式要從今天的對話記下掛著的事、整理你的記憶、寫今天的摘要，要幾分鐘；這段時間你不在，結束了會告訴你。"
            + (forced ? "你手上的「\(todo.current ?? "")」先停著，結束後接著做。" : "")
        do { try notice(Event(.memory, .internal, text)) } catch { log("ERROR", "maintenance: start notice not written: \(error)") }
        log("INFO", "maintenance: started\(forced ? " (busy limit)" : ""); \(timeline.events.count) events in segment \(timeline.segment)")
        runMaintenance()
    }

    func runMaintenance() {
        maintenanceSerial += 1
        feed.notify(.maintenance)
        let serial = maintenanceSerial
        maintenanceTask = Task { [weak self] in await self?.maintenanceCycle(serial) }
    }

    /// The tasks not done yet, in order; then the end notice.
    func maintenanceCycle(_ serial: Int) async {
        var done: MaintenanceDone?
        for t in MaintenanceTask.allCases where !maintenanceState.done.contains(t.rawValue) {
            guard serial == maintenanceSerial else { return }
            guard maintenanceDue(t) else {
                log("INFO", "maintenance: \(t.rawValue): nothing to do")
                continue
            }
            let started = Date()
            do {
                let work = try await maintenancePrepare(t)
                guard serial == maintenanceSerial, !Task.isCancelled else {
                    log("INFO", "maintenance: \(t.rawValue) prepared after it was interrupted; discarded, nothing written")
                    return
                }
                // A task over secret or sealed content writes its results sealed (documents, log, history).
                let was = sealing
                sealing = was || maintenanceSeals(work)
                defer { sealing = was }
                var d = try maintenanceCommit(work)
                d.sealed = d.sealed || sealing
                commitHistory(.maintenance, task: t.rawValue, what: t.label, why: String(d.log.prefix(300)))
                done = d
                maintenanceState.done.append(t.rawValue)
                maintenanceState.words = (maintenanceState.words ?? []) + [d.sealed ? try memory.box.seal(d.words) : d.words]
                try saveMaintenance()
                log("INFO", String(format: "maintenance: %@ done in %.1f s: %@", t.rawValue, Date().timeIntervalSince(started), d.log))
            } catch {
                guard serial == maintenanceSerial else {
                    log("INFO", "maintenance: \(t.rawValue) cancelled (interrupted) after \(String(format: "%.1f", Date().timeIntervalSince(started))) s; nothing written")
                    return
                }
                log("ERROR", "maintenance: \(t.rawValue) failed: \(error)")
                // Whatever it wrote before failing stands, and is recorded as its.
                commitHistory(.maintenance, task: t.rawValue, what: t.label + "（出錯，做到一半）", why: "\(error)")
                maintenanceState.failed[t.rawValue] = "\(error)"
                saveMaintenanceLogged()
            }
        }
        guard serial == maintenanceSerial else { return }
        endMaintenance(done)
    }

    /// The end notice: what was done (in words), the summary, today's calendar. A todo carried over
    /// (busy limit) is recorded at the notice and she gets a turn to resume it; otherwise no turn.
    func endMaintenance(_ done: MaintenanceDone?) {
        let ended = maintenanceState
        let now = wallClock.now()
        maintenanceTask = nil
        maintenanceState = MaintenanceFile()
        saveMaintenanceLogged()
        let kept = done?.kept ?? (ended.forced && todoHeld ? todo : nil)
        var text = "【系統維護】結束，現在 \(Self.hhmm(now))。"
        for w in ended.words ?? [] {
            do { text += SecretBox.isSealed(w) ? try memory.box.open(w, place: maintenanceURL.path) : w } catch {
                log("ERROR", "maintenance: a task's words not shown: \(error)")
            }
        }
        let sealed = done?.sealed == true || (ended.words ?? []).contains(where: SecretBox.isSealed)
        let failed = MaintenanceTask.allCases.filter { ended.failed[$0.rawValue] != nil }
        if !failed.isEmpty {
            text += "沒做成：\(failed.map(\.label).joined(separator: "、"))（程式出錯），明天維護的時候再做"
                + (failed.contains(.consolidate) ? "；之前的對話和做過的事都還在你眼前" : "") + "。"
        }
        if let s = done?.summary { text += "\n" + s + "\n" }
        text += "今天的行事曆：" + calendarStatus(now: now, all: true) + "。"
        if let kept {
            text += kept.phase == .waiting ? "你手上的「\(kept.current ?? "")」還在，還是等待中。" : "你手上的「\(kept.current ?? "")」還在，接著做。"
        }
        do {
            let ev = try append(Event(.memory, .internal, text, sealed: sealed))
            // The lists she holds go on in the new day, their steps starting at the notice.
            if var k = done?.kept {
                k.startSeq = ev.seq!
                todo = k
                aside = done?.keptAside.map { a in
                    var a = a
                    a.todo.startSeq = ev.seq!
                    return a
                }
                try recordTodo(seq: ev.seq!)
            }
        } catch { log("ERROR", "maintenance: end notice not written: \(error)") }
        log("INFO", "maintenance: ended; done \(ended.done), failed \(Array(ended.failed.keys))")
        feed.notify(.maintenance)
        // A turn only for a list 進行中: one 等待中 waits on for its answer.
        if kept != nil, todoBusy { followUp = true }
        drivesCheck()
        if let w = waiter {
            waiter = nil
            w.resume(returning: true)
        }
    }

    func maintenanceDue(_ t: MaintenanceTask) -> Bool {
        switch t {
        case .concerns: drivesConfig.enabled && drivesConfig.maintenanceConcernsMax > 0 && dayToConsolidate
        case .review: reviewDue
        case .audit: auditDue
        case .recur: recurDue
        case .consolidate: dayToConsolidate
        }
    }

    /// Writes nothing; cancellable (its model requests end with the task).
    func maintenancePrepare(_ t: MaintenanceTask) async throws -> MaintenanceWork {
        switch t {
        case .concerns:
            return .concerns(try await prepareConcernExtraction())
        case .review:
            return .review(try await prepareIdealReview())
        case .audit:
            return .audit(try await prepareAudit())
        case .recur:
            return .recur(try await prepareRecur())
        case .consolidate:
            let request = try Consolidation.gather(timeline, memory: memory, config: config.consolidate)
            return .consolidate(request, try await Consolidation.ask(request, model: maintenanceModel, config: config))
        }
    }

    /// Synchronous on the actor: all of the task's writes, with nothing in between.
    func maintenanceCommit(_ w: MaintenanceWork) throws -> MaintenanceDone {
        switch w {
        case .review(let answer):
            return try commitIdealReview(answer)
        case .audit(let answer):
            return try commitAudit(answer)
        case .recur(let answer):
            return try commitRecur(answer)
        case .concerns(let answer):
            return try commitConcernExtraction(answer)
        case .consolidate(let request, let answer):
            let old = timeline
            let kept = todoHeld ? todo : nil, keptAside = aside
            let result = try Consolidation.apply(request, answer, memory: memory, config: config.consolidate, now: wallClock.now()) {
                try Timeline.startSegment(dir: timelineDir)
            }
            // The new day: its own timeline, today index, todo list and compactions; the calendar,
            // the memory and the process stay.
            timeline = try Timeline(dir: timelineDir, box: memory.box)
            today = try TodayIndex(timeline, embedder: embedder)
            startDay()
            // Program notices that came after the request was gathered (the owner's calendar
            // edits meanwhile) move to the new day.
            for ev in old.events where ev.seq! > request.lastSeq { try append(ev, keepTime: true) }
            // The summary may tell of the day's secrets or of the encrypted memories it saw: stored
            // encrypted then (`maintenanceSeals`, the cycle marks it sealed).
            return MaintenanceDone(
                log: "segment \(result.segment) → \(result.newSegment), \(result.applied.count) applied, request \(result.promptTokens) prompt tokens",
                words: Consolidation.doneText(result.plan), summary: Consolidation.summaryNote + answer.summary, kept: kept, keptAside: keptAside)
        }
    }

    /// Whether a task's work read secret or sealed content (the day's events, an encrypted memory, a
    /// document stored sealed), so that everything its commit writes is sealed (`sealing`).
    func maintenanceSeals(_ w: MaintenanceWork) -> Bool {
        let day = timeline.events.contains { $0.secret || $0.sealed }
        switch w {
        case .concerns: return day || Concerns.boxed(concernsURL)
        case .review: return day || [goalsURL, recordURL, idealURL].contains(where: Concerns.boxed)
        case .audit(let a): return a.request.items.contains { sealedFolders.contains(memory.dirName($0.folder)) }
            || a.request.sources.values.contains { $0.contains("｜秘密]") }
        case .recur: return day || Concerns.boxed(selfURL)
        case .consolidate(let request, _): return day || request.related.contains { sealedFolders.contains($0.folder) }
        }
    }

    /// Per-day state after a new segment: what belonged to the old day's items and attachments.
    func startDay() {
        todo = timeline.todo
        itemSteps = []
        itemApps = []
        itemUsedComputer = false
        itemActed = false
        compacting = nil
        // The list aside keeps its place, not its steps (their seqs were the old day's).
        asideMemory.itemSteps = []
        asideMemory.itemApps = []
        asideMemory.itemUsedComputer = false
        asideMemory.itemActed = false
        warnedTurns = 0
        attachedIDs = []
        imageURIs = [:]
        idleTurns = 0
        effortNext = nil
        followUp = false
        taskTurn = false
        ownerArrived = false
    }

    // interrupted ----------------------------------------------------------------------------------

    /// The owner's or the developer's line while it runs (no-op otherwise): the running task is
    /// cancelled and writes nothing; it stays due for the retry; the notice lands before the line.
    /// In her diary turn: the turn is over (what she wrote stays) and comes again at the retry.
    func interruptMaintenance(by who: String) {
        let diary = maintenanceState.diary != nil
        guard maintenanceState.running != nil || diary else { return }
        maintenanceSerial += 1
        maintenanceTask?.cancel()
        maintenanceTask = nil
        maintenanceTimer?.cancel()
        maintenanceTimer = nil
        maintenanceState.running = nil
        maintenanceState.diary = nil
        maintenanceState.interrupted = true
        maintenanceState.forced = false
        saveMaintenanceLogged()
        let left = MaintenanceTask.allCases.filter { !maintenanceState.done.contains($0.rawValue) && maintenanceDue($0) }
        let text = "【系統維護】被\(who)打斷了，" + (diary ? "日記的回合先停下來（寫了的留著），" : "")
            + (left.contains(.consolidate) ? "今天的還沒整理完，之前的對話和做過的事都還在你眼前" : "還沒做完")
            + "；沒人跟你說話 \(Self.minutes(maintenanceConfig.retryIdleMin))、你手上也沒事的時候，會再做一次"
            + (diary ? "，那時候可以接著寫日記。" : "。")
        do { try notice(Event(.memory, .internal, text)) } catch { log("ERROR", "maintenance: interrupt notice not written: \(error)") }
        log("INFO", "maintenance: interrupted by \(who)\(diary ? " in her diary turn" : ""); left \(left.map(\.rawValue)); retries after \(maintenanceConfig.retryIdleMin) min of quiet")
        feed.notify(.maintenance)
    }

    /// At boot: running before the restart goes on with the tasks not done; due waits for the check.
    func resumeMaintenance() {
        guard maintenanceConfig.enabled else { return }
        // A restart in her diary turn starts it again (the notice gives her the turn).
        if maintenanceState.diary != nil {
            log("INFO", "maintenance: was in her diary turn at the restart; it starts again")
            maintenanceState.diary = nil
            saveMaintenanceLogged()
        }
        if maintenanceState.running != nil {
            log("INFO", "maintenance: was running at the restart; going on with the tasks not done (\(maintenanceState.done) done)")
            runMaintenance()
        } else {
            maintenanceCheck()
        }
    }

    // status ---------------------------------------------------------------------------------------

    /// The dashboard's and `dollctl status`'s line while maintenance runs or waits; nil otherwise.
    public func maintenanceLine() -> String? {
        if let r = maintenanceState.running { return "系統維護中，從 \(Self.nowText(r)) 起" }
        if let d = maintenanceState.diary { return "系統維護：她在寫日記，從 \(Self.nowText(d)) 起" }
        if let d = maintenanceState.due {
            return "系統維護 \(Self.nowText(d)) 到了，" + (maintenanceState.interrupted ? "被打斷過，等安靜 \(maintenanceConfig.retryIdleMin) 分鐘" : "等她手上沒事")
        }
        return nil
    }
}
