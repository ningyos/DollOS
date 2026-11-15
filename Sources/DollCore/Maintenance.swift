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
/// tool result waiting for her, no todo item in progress or waiting. So a todo in hand or a turn
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
    /// Interrupted since it came due: waits for `retry_idle_min` of quiet.
    var interrupted = false
    /// The busy limit made it start with a todo in hand: the end notice gives her a turn.
    var forced = false
    /// This maintenance's tasks already done (their writes stand).
    var done: [String] = []
    /// This maintenance's tasks that failed, with the error (the next maintenance tries again).
    var failed: [String: String] = [:]
}

/// The maintenance's tasks, run in this order.
public enum MaintenanceTask: String, CaseIterable, Sendable {
    /// The timeline so far into long-term memory, then a new segment opened by the end notice and
    /// the day's summary.
    case consolidate

    /// How the notices name it.
    var label: String {
        switch self {
        case .consolidate: "整理記憶、寫摘要"
        }
    }
}

/// One committed task's report: a line for the log; for the end notice what was done in words and
/// the day's summary; the todo carried into the new day (busy limit).
struct MaintenanceDone {
    var log: String
    var words: String
    var summary: String
    var kept: TodoState?
}

/// One task's prepared work: what `commit` writes.
enum MaintenanceWork: Sendable {
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
        try agenda.save()
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

    var todoBusy: Bool { todo.phase == .doing || todo.phase == .waiting }

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
        maintenanceState.due = wallClock.now()
        saveMaintenanceLogged()
        log("INFO", "maintenance: due")
        maintenanceCheck()
    }

    /// Idle for maintenance: no turn running, nothing queued, no tool result waiting, no todo in hand.
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
        if let r = retryAt(), now < r { return armMaintenanceTimer(r) }
        if maintenanceIdle { return startMaintenance(forced: false) }
        let limit = busyLimitAt()!
        if now >= limit, !inTurn, queue.isEmpty {
            log("INFO", "maintenance: busy \(maintenanceConfig.busyLimitHours) h since due (「\(todo.current ?? "")」\(todo.phase.label)); starts now, the todo kept")
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

    /// Starts it: the start notice, then the tasks. Nothing to do: no notice, no longer due.
    func startMaintenance(forced: Bool) {
        let now = wallClock.now()
        guard MaintenanceTask.allCases.contains(where: { !maintenanceState.done.contains($0.rawValue) && maintenanceDue($0) }) else {
            log("INFO", "maintenance: nothing to do; not started")
            maintenanceState = MaintenanceFile()
            return saveMaintenanceLogged()
        }
        if forced { stopWatch() }
        taskTurn = false
        maintenanceState.running = now
        maintenanceState.forced = forced
        saveMaintenanceLogged()
        let text = "【系統維護】開始，現在 \(Self.hhmm(now))。程式要整理你的記憶、寫今天的摘要，要幾分鐘；這段時間你不在，結束了會告訴你。"
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
                let d = try maintenanceCommit(work)
                done = d
                maintenanceState.done.append(t.rawValue)
                try saveMaintenance()
                log("INFO", String(format: "maintenance: %@ done in %.1f s: %@", t.rawValue, Date().timeIntervalSince(started), d.log))
            } catch {
                guard serial == maintenanceSerial else {
                    log("INFO", "maintenance: \(t.rawValue) cancelled (interrupted) after \(String(format: "%.1f", Date().timeIntervalSince(started))) s; nothing written")
                    return
                }
                log("ERROR", "maintenance: \(t.rawValue) failed: \(error)")
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
        let kept = done?.kept ?? (ended.forced && todoBusy ? todo : nil)
        var text = "【系統維護】結束，現在 \(Self.hhmm(now))。"
        if let w = done?.words { text += w }
        let failed = MaintenanceTask.allCases.filter { ended.failed[$0.rawValue] != nil }
        if !failed.isEmpty {
            text += "沒做成：\(failed.map(\.label).joined(separator: "、"))（程式出錯），明天維護的時候再做"
                + (failed.contains(.consolidate) ? "；之前的對話和做過的事都還在你眼前" : "") + "。"
        }
        if let s = done?.summary { text += "\n" + s + "\n" }
        text += "今天的行事曆：" + calendarStatus(now: now, all: true) + "。"
        if let kept { text += "你手上的「\(kept.current ?? "")」還在，接著做。" }
        do {
            let ev = try append(Event(.memory, .internal, text))
            // A todo in hand goes on in the new day, its steps starting at the notice.
            if var k = done?.kept {
                k.startSeq = ev.seq!
                todo = k
                try timeline.recordTodo(k, seq: ev.seq!)
            }
        } catch { log("ERROR", "maintenance: end notice not written: \(error)") }
        log("INFO", "maintenance: ended; done \(ended.done), failed \(Array(ended.failed.keys))")
        feed.notify(.maintenance)
        if kept != nil { followUp = true }
        drivesCheck()
        if let w = waiter {
            waiter = nil
            w.resume(returning: true)
        }
    }

    func maintenanceDue(_ t: MaintenanceTask) -> Bool {
        switch t {
        case .consolidate: dayToConsolidate
        }
    }

    /// Writes nothing; cancellable (its model requests end with the task).
    func maintenancePrepare(_ t: MaintenanceTask) async throws -> MaintenanceWork {
        switch t {
        case .consolidate:
            let request = try Consolidation.gather(timeline, memory: memory, config: config.consolidate)
            return .consolidate(request, try await Consolidation.ask(request, model: maintenanceModel, config: config))
        }
    }

    /// Synchronous on the actor: all of the task's writes, with nothing in between.
    func maintenanceCommit(_ w: MaintenanceWork) throws -> MaintenanceDone {
        switch w {
        case .consolidate(let request, let answer):
            let old = timeline
            let kept = todoBusy ? todo : nil
            let result = try Consolidation.apply(request, answer, memory: memory, config: config.consolidate, now: wallClock.now()) {
                try Timeline.startSegment(dir: timelineDir)
            }
            // The new day: its own timeline, today index, todo list and compactions; the calendar,
            // the memory and the process stay.
            timeline = try Timeline(dir: timelineDir)
            today = try TodayIndex(timeline, embedder: embedder)
            startDay()
            // Program notices that came after the request was gathered (the owner's calendar
            // edits meanwhile) move to the new day.
            for ev in old.events where ev.seq! > request.lastSeq { try append(ev, keepTime: true) }
            return MaintenanceDone(
                log: "segment \(result.segment) → \(result.newSegment), \(result.applied.count) applied, request \(result.promptTokens) prompt tokens",
                words: Consolidation.doneText(result.plan), summary: Consolidation.summaryNote + answer.summary, kept: kept)
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
        warnedTurns = 0
        attachedIDs = []
        cheatsheetSent = []
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
    func interruptMaintenance(by who: String) {
        guard maintenanceState.running != nil else { return }
        maintenanceSerial += 1
        maintenanceTask?.cancel()
        maintenanceTask = nil
        maintenanceTimer?.cancel()
        maintenanceTimer = nil
        maintenanceState.running = nil
        maintenanceState.interrupted = true
        maintenanceState.forced = false
        saveMaintenanceLogged()
        let left = MaintenanceTask.allCases.filter { !maintenanceState.done.contains($0.rawValue) && maintenanceDue($0) }
        let text = "【系統維護】被\(who)打斷了，" + (left.contains(.consolidate) ? "今天的還沒整理完，之前的對話和做過的事都還在你眼前" : "還沒做完")
            + "；沒人跟你說話 \(Self.minutes(maintenanceConfig.retryIdleMin))、你手上也沒事的時候，會再做一次。"
        do { try notice(Event(.memory, .internal, text)) } catch { log("ERROR", "maintenance: interrupt notice not written: \(error)") }
        log("INFO", "maintenance: interrupted by \(who); left \(left.map(\.rawValue)); retries after \(maintenanceConfig.retryIdleMin) min of quiet")
        feed.notify(.maintenance)
    }

    /// At boot: running before the restart goes on with the tasks not done; due waits for the check.
    func resumeMaintenance() {
        guard maintenanceConfig.enabled else { return }
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
        if let d = maintenanceState.due {
            return "系統維護 \(Self.nowText(d)) 到了，" + (maintenanceState.interrupted ? "被打斷過，等安靜 \(maintenanceConfig.retryIdleMin) 分鐘" : "等她手上沒事")
        }
        return nil
    }
}
