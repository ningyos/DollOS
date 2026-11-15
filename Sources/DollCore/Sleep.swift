import Foundation

/// Her sleep (2026-11-15, owner). She decides when to sleep: her `sleep` tool, refused while a todo
/// item is in progress or waiting. Her calendar holds two daily items the program creates when it has
/// none (`[sleep] bed_time`, `wake_time`): 「睡覺」 fires like any timed item (a turn; she decides),
/// 「起床」 ends her rest. The backstop: awake, nobody (owner or developer) has spoken to her for
/// `[sleep] idle_backstop_min`, nothing in hand and something to consolidate, the program puts her to
/// sleep.
///
/// Asleep she takes no turns; events still queue. First the sleep's tasks run in order
/// (`SleepTask`), then she rests until the next 「起床」 (at most `rest_max_hours` away; further means
/// today's wake time has passed and she wakes once the tasks are done). A 「起床」 that comes during
/// the tasks lets them finish, then wakes her. Waking writes a program notice with the time, what the
/// sleep did and today's calendar; the morning wake gives no turn of its own.
///
/// Woken early (the owner's line, written or spoken; the developer's line; a timed calendar item
/// other than 「睡覺」): the running task is cancelled, its model request with it, and it writes
/// nothing; the tasks done stay; the ones not done wait for the next sleep. The notice
/// (【睡覺】你睡到一半被…叫醒) lands before the line that woke her, which gives her the turn.
///
/// A task is all-or-nothing: `prepare` (async, cancellable, writes nothing) then `commit`
/// (synchronous on the actor, so no wake can come between its writes). Adding a task: a `SleepTask`
/// case, its `due`, `prepare` and `commit` arms; the flow does not change.
///
/// `<data_dir>/sleep.json` holds awake/asleep, since when, and the tasks this sleep finished: a
/// restart while asleep boots her still asleep and goes on with the tasks not done.

/// What `sleep.json` holds.
struct SleepFile: Codable, Equatable {
    var asleep: Bool
    /// Since when she is awake, or asleep.
    var since: Double
    /// Asleep: `self` (her `sleep`) or `backstop`.
    var by: String?
    /// Asleep: this sleep's tasks already done (their writes stand).
    var done: [String] = []
    /// Asleep: this sleep's tasks that failed, with the error (not done; the next sleep tries again).
    var failed: [String: String] = [:]
}

/// The sleep's tasks, run in this order.
public enum SleepTask: String, CaseIterable, Sendable {
    /// Today's timeline into long-term memory, then a new segment opened by the day's summary.
    case consolidate

    /// How the wake notice names it.
    var label: String {
        switch self {
        case .consolidate: "把今天整理進長期記憶、寫今天的摘要"
        }
    }
}

/// One task's prepared work: what `commit` writes.
enum SleepWork: Sendable {
    case consolidate(Consolidation.Request, Consolidation.Answer)
}

/// What woke her.
enum Waker: Equatable {
    case owner, developer
    /// A timed calendar item (its title).
    case calendar(String)
    /// The 「起床」 item, or the tasks ended with no rest to take.
    case morning

    var label: String {
        switch self {
        case .owner: "主人"
        case .developer: "開發者"
        case .calendar(let t): "行事曆上的「\(t)」"
        case .morning: ""
        }
    }
}

extension Doll {
    public var isAsleep: Bool { sleepState.asleep }

    static func readSleep(_ url: URL) throws -> SleepFile? {
        guard FileManager.default.fileExists(atPath: url.path) else { return nil }
        do { return try JSONDecoder().decode(SleepFile.self, from: Data(contentsOf: url)) } catch {
            throw StoreError(description: "\(url.path): not a sleep file: \(error)")
        }
    }

    func saveSleep() throws {
        let enc = JSONEncoder()
        enc.outputFormatting = [.prettyPrinted, .sortedKeys]
        try enc.encode(sleepState).write(to: sleepURL, options: .atomic)
    }

    /// The 「睡覺」／「起床」 items when the calendar has none, and the first "awake since" (a new data
    /// dir). Runs once the calendar is caught up, so a bedtime already past today never fires.
    func ensureSleepItems() throws {
        guard sleepConfig.enabled else { return }
        let now = wallClock.now()
        if sleepFresh {
            sleepFresh = false
            sleepState = SleepFile(asleep: false, since: now)
            try saveSleep()
        }
        var added: [String] = []
        for (role, title, time) in [("sleep", "睡覺", sleepConfig.bedTime), ("wake", "起床", sleepConfig.wakeTime)]
        where agenda.item(role: role) == nil {
            agenda.addRole(role, title: title, day: Day(ts: now), time: TimeOfDay(time)!)
            added.append("\(title) \(time)")
        }
        guard !added.isEmpty else { return }
        try agenda.save()
        log("INFO", "sleep: calendar had no \(added.joined(separator: ", ")); added (daily)")
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
    /// began (the boot, wake and 【睡前】 notices alone are not a day).
    var dayToConsolidate: Bool { let c = dayCounts(); return c.lines + c.replies > 0 }

    var todoBusy: Bool { todo.phase == .doing || todo.phase == .waiting }

    /// 【狀況】's line: how long she has been awake and how much the day holds.
    func sleepStatusLine(now: Double) -> String {
        let m = max(0, Int((now - sleepState.since) / 60))
        let c = dayCounts()
        let since = Self.hhmm(sleepState.since)
        let day = Day(ts: sleepState.since) == Day(ts: now) ? since : "\(Day(ts: sleepState.since).label) \(since)"
        return "- 醒著：\(m / 60) 小時 \(m % 60) 分（從 \(day) 起）；今天的時間線：別人跟你說了 \(c.lines) 句，你回了 \(c.replies) 則"
    }

    static func hhmm(_ ts: Double) -> String {
        let c = Day.cal.dateComponents([.hour, .minute], from: Date(timeIntervalSince1970: ts))
        return String(format: "%02d:%02d", c.hour!, c.minute!)
    }

    // falling asleep -------------------------------------------------------------------------------

    /// Her `sleep` call: refused with an item in hand; otherwise she sleeps once the reply's calls ran.
    func sleepTool() throws -> Outcome {
        if todoBusy {
            throw ToolError(description: "todo 還有「\(todo.current ?? "")」\(todo.phase.label)，不能睡；先做完（todo.done）、確定做不到（todo.fail），或用 todo.set 給空的陣列結束清單，再睡")
        }
        sleepAsked = true
        return Outcome(text: "好，這則回覆的呼叫做完以後你就睡了。", actions: ["sleep"], background: true)
    }

    /// After each turn of `run()`: her `sleep` takes effect, or the backstop is re-armed.
    func afterTurn() throws {
        if sleepAsked {
            sleepAsked = false
            if todoBusy {
                try append(Event(.memory, .internal, "【睡覺】你同一則回覆裡又開了 todo（「\(todo.current ?? "")」），所以沒有睡；做完再睡。"))
            } else if queue.contains(where: { $0.speaker != nil }) {
                try append(Event(.memory, .internal, "【睡覺】你要睡的時候又有人跟你說話，所以先沒睡；回完要睡再呼叫 sleep。"))
            } else {
                try beginSleep(by: "self")
                return
            }
        }
        scheduleBackstop()
    }

    func beginSleep(by: String) throws {
        let now = wallClock.now()
        stopWatch()
        followUp = false
        taskTurn = false
        backstopTask?.cancel()
        backstopTask = nil
        if by == "backstop" {
            try append(Event(.memory, .internal, "【睡覺】已經 \(Self.minutes(sleepConfig.idleBackstopMin))沒有人跟你說話，你手上也沒事，程式讓你去睡了。"))
        }
        sleepState = SleepFile(asleep: true, since: now, by: by)
        try saveSleep()
        wakeDue = false
        log("INFO", "sleep: asleep (\(by)); \(timeline.events.count) events in segment \(timeline.segment)")
        startSleepCycle()
    }

    static func minutes(_ m: Int) -> String { m % 60 == 0 ? "\(m / 60) 小時" : m >= 60 ? "\(m / 60) 小時 \(m % 60) 分" : "\(m) 分鐘" }

    func startSleepCycle() {
        sleepSerial += 1
        sleepWorking = true
        feed.notify(.sleep)
        let serial = sleepSerial
        sleepTask = Task { [weak self] in await self?.sleepCycle(serial) }
    }

    /// The tasks not done yet, in order; then rest or wake.
    func sleepCycle(_ serial: Int) async {
        for t in SleepTask.allCases where !sleepState.done.contains(t.rawValue) {
            guard serial == sleepSerial else { return }
            guard sleepDue(t) else {
                log("INFO", "sleep: \(t.rawValue): nothing to do")
                continue
            }
            let started = Date()
            do {
                let work = try await sleepPrepare(t)
                guard serial == sleepSerial, !Task.isCancelled else {
                    log("INFO", "sleep: \(t.rawValue) prepared after she woke; discarded, nothing written")
                    return
                }
                let report = try sleepCommit(work)
                sleepState.done.append(t.rawValue)
                try saveSleep()
                log("INFO", String(format: "sleep: %@ done in %.1f s: %@", t.rawValue, Date().timeIntervalSince(started), report))
            } catch {
                guard serial == sleepSerial else {
                    log("INFO", "sleep: \(t.rawValue) cancelled (she woke) after \(String(format: "%.1f", Date().timeIntervalSince(started))) s; nothing written")
                    return
                }
                log("ERROR", "sleep: \(t.rawValue) failed: \(error)")
                sleepState.failed[t.rawValue] = "\(error)"
                do { try saveSleep() } catch { log("ERROR", "sleep: saving \(sleepURL.path) failed: \(error)") }
            }
        }
        guard serial == sleepSerial else { return }
        sleepWorking = false
        feed.notify(.sleep)
        let now = wallClock.now()
        if wakeDue {
            wakeUp(.morning)
        } else if let until = restUntil(now) {
            log("INFO", "sleep: tasks over; resting until \(Day(ts: until).label) \(Self.hhmm(until))")
        } else {
            log("INFO", "sleep: tasks over; the next 起床 is more than \(sleepConfig.restMaxHours) h away, so she wakes now")
            wakeUp(.morning)
        }
    }

    /// The next 「起床」 when she rests until it, nil when it is too far (today's has passed).
    func restUntil(_ now: Double) -> Double? {
        guard let w = agenda.item(role: "wake"), let next = agenda.nextStart(of: w.id, after: now),
              next - now <= sleepConfig.restMaxHours * 3600 else { return nil }
        return next
    }

    func sleepDue(_ t: SleepTask) -> Bool {
        switch t {
        case .consolidate: dayToConsolidate
        }
    }

    /// Writes nothing; cancellable (its model requests end with the task).
    func sleepPrepare(_ t: SleepTask) async throws -> SleepWork {
        switch t {
        case .consolidate:
            let request = try Consolidation.gather(timeline, memory: memory, config: config.consolidate)
            return .consolidate(request, try await Consolidation.ask(request, model: sleepModel, config: config))
        }
    }

    /// Synchronous on the actor: all of the task's writes, with nothing in between. Returns a line for the log.
    func sleepCommit(_ w: SleepWork) throws -> String {
        switch w {
        case .consolidate(let request, let answer):
            let old = timeline
            let result = try Consolidation.apply(request, answer, memory: memory, config: config.consolidate, now: wallClock.now()) {
                try Timeline.startSegment(dir: timelineDir)
            }
            // The new day: its own timeline, today index, todo list and compactions; the calendar,
            // the memory and the process stay.
            timeline = try Timeline(dir: timelineDir)
            today = try TodayIndex(timeline, embedder: embedder)
            startDay()
            try append(Event(.memory, .internal, Consolidation.wakeNote + answer.summary))
            // Program notices that came after the request was gathered (the owner's calendar edits
            // while she slept) move to the new day; nothing else can arrive while she sleeps.
            for ev in old.events where ev.seq! > request.lastSeq { try append(ev) }
            return "segment \(result.segment) → \(result.newSegment), \(result.applied.count) applied, request \(result.promptTokens) prompt tokens"
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

    // waking -------------------------------------------------------------------------------------

    /// Wakes her (no-op when awake): the running task is cancelled and writes nothing; a notice says
    /// when, what the sleep did and did not do, and today's calendar.
    func wakeUp(_ by: Waker) {
        guard sleepState.asleep else { return }
        let working = sleepWorking
        sleepSerial += 1
        sleepTask?.cancel()
        sleepTask = nil
        sleepWorking = false
        wakeDue = false
        let slept = sleepState
        let now = wallClock.now()
        sleepState = SleepFile(asleep: false, since: now)
        do { try saveSleep() } catch { log("ERROR", "sleep: saving \(sleepURL.path) failed: \(error)") }
        let time = Self.nowText(now)
        var text: String
        switch (by, working) {
        case (.morning, _): text = "【起床】你醒了，現在 \(time)。"
        case (_, true): text = "【睡覺】你睡到一半被\(by.label)叫醒，現在 \(time)。"
        case (_, false): text = "【起床】你被\(by.label)叫醒了，現在 \(time)。"
        }
        let done = SleepTask.allCases.filter { slept.done.contains($0.rawValue) }
        let failed = SleepTask.allCases.filter { slept.failed[$0.rawValue] != nil }
        let left = SleepTask.allCases.filter { !slept.done.contains($0.rawValue) && slept.failed[$0.rawValue] == nil && sleepDue($0) }
        if !done.isEmpty {
            text += "睡著的時候做完了：\(done.map(\.label).joined(separator: "、"))"
                + (done.contains(.consolidate) ? "，今天從上面的【睡前】摘要開始" : "") + "。"
        }
        if !failed.isEmpty { text += "沒做成：\(failed.map(\.label).joined(separator: "、"))（程式出錯），下次睡覺再做。" }
        if !left.isEmpty {
            text += "還沒做完：\(left.map(\.label).joined(separator: "、"))，下次睡覺再做"
                + (left.contains(.consolidate) ? "；今天的事還沒整理完，睡前的對話和做過的事都還在你眼前" : "") + "。"
        }
        text += "今天的行事曆：" + calendarStatus(now: now, all: true) + "。"
        do { try notice(Event(.memory, .internal, text)) } catch { log("ERROR", "sleep: wake notice not written: \(error)") }
        log("INFO", "sleep: woke (\(by == .morning ? "morning" : by.label), \(working ? "during the tasks" : "resting")); done \(slept.done), left \(left.map(\.rawValue))")
        feed.notify(.sleep)
        scheduleBackstop()
        if let w = waiter {
            waiter = nil
            w.resume(returning: true)
        }
    }

    static func nowText(_ ts: Double) -> String { "\(Day(ts: ts).label) \(hhmm(ts))" }

    /// A timed start while she sleeps or is awake: 「起床」 ends her rest (or, during the tasks,
    /// ends the sleep once they are done) and gives no turn; 「睡覺」 gives a turn while awake only;
    /// any other item wakes her and gives the turn it always gives.
    func fire(_ o: Occurrence) {
        switch o.role {
        case "wake":
            guard sleepState.asleep else { log("INFO", "calendar: 起床 while awake; nothing"); return }
            if sleepWorking { wakeDue = true; log("INFO", "sleep: 起床 came during the tasks; she wakes when they are done") } else { wakeUp(.morning) }
        case "sleep" where sleepState.asleep:
            log("INFO", "calendar: 睡覺 while asleep; nothing")
        default:
            if sleepState.asleep { wakeUp(.calendar(o.title)) }
            put(Event(.calendar, .internal, Self.fireText(o)))
        }
    }

    // the backstop ---------------------------------------------------------------------------------

    /// When the backstop may fire: the last person's line (or since she woke, if later) plus the idle time.
    func backstopDeadline() -> Double {
        max(lastSpoke, sleepState.since) + Double(sleepConfig.idleBackstopMin) * 60
    }

    func scheduleBackstop() {
        backstopTask?.cancel()
        backstopTask = nil
        backstopSerial += 1
        guard sleepConfig.enabled, !sleepState.asleep, !stopped, calendarCaughtUp else { return }
        let serial = backstopSerial, clock = wallClock, deadline = backstopDeadline()
        backstopTask = Task { [weak self] in
            do { try await clock.sleep(until: deadline) } catch { return }
            await self?.backstopFire(serial)
        }
    }

    /// Not while a turn runs or something waits for one (the loop re-arms it after the turn), not
    /// with an item in hand, not with nothing to consolidate.
    func backstopFire(_ serial: Int) {
        guard serial == backstopSerial, !sleepState.asleep else { return }
        backstopTask = nil
        guard !inTurn, !paused, queue.isEmpty, !followUp else { return }
        guard wallClock.now() >= backstopDeadline() else { return scheduleBackstop() }
        if todoBusy { return log("INFO", "sleep: idle \(sleepConfig.idleBackstopMin) min but「\(todo.current ?? "")」is \(todo.phase.label); not put to sleep") }
        if !dayToConsolidate { return log("INFO", "sleep: idle \(sleepConfig.idleBackstopMin) min with nothing to consolidate; not put to sleep") }
        do { try beginSleep(by: "backstop") } catch { log("ERROR", "sleep: the backstop could not put her to sleep: \(error)") }
    }

    // status ---------------------------------------------------------------------------------------

    /// The dashboard's and `dollctl status`'s line while she sleeps; nil awake.
    public func sleepLine() -> String? {
        guard sleepState.asleep else { return nil }
        let rest = restUntil(wallClock.now()).map { "，睡到 \(Self.nowText($0))" } ?? ""
        return "睡著，從 \(Self.nowText(sleepState.since)) 起（\(sleepState.by == "backstop" ? "程式讓她睡的" : "自己去睡的")）；"
            + (sleepWorking ? "整理中" : "整理完了\(rest)")
    }
}
