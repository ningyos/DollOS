import Foundation
import Testing
@testable import DollCore

// m6-drives (2026-11-15): the simulated week the [drives] numbers were tuned on. No model: a
// scripted schedule of the owner's events on an injected clock, the signals' math (`Drives`) and
// the wandering rule (idle + out of range + not yet surfaced for this crossing), printing curves
// and wander counts. Runs only with M6_WEEK=<out file>:
//
//     M6_WEEK=experiments/m6-drives/week.txt swift test --scratch-path /tmp/m6build --filter m6DrivesWeek
//
// Assumptions (the model is not in the loop): she always has a concern of each kind, so every
// surfacing is a turn; a 親近 surfacing makes her message the owner and he does not answer it (his
// next line comes only when the schedule says); a 睡意 surfacing makes her go to sleep at once; on
// the calendar's 睡覺 (01:00) she sleeps if still awake; she wakes at 08:00. A task keeps her busy
// for 30 minutes and ends done (or failed); a conversation keeps her busy from its first line to
// 10 minutes after its last.

private let weekOut = ProcessInfo.processInfo.environment["M6_WEEK"]

private struct WeekEvent { let minute: Int; let kind: Kind; enum Kind { case talk, task, failedTask, praise, correction, toolErrors } }

/// One day's owner schedule (minutes from 00:00) by scenario.
private func schedule(_ name: String) -> [WeekEvent] {
    func at(_ h: Int, _ m: Int = 0) -> Int { h * 60 + m }
    func chat(_ from: Int, _ lines: Int, every: Int = 6) -> [WeekEvent] { (0..<lines).map { WeekEvent(minute: from + $0 * every, kind: .talk) } }
    switch name {
    case "normal":
        return chat(at(8, 30), 3) + [WeekEvent(minute: at(10), kind: .task)] + chat(at(12, 30), 2)
            + [WeekEvent(minute: at(15), kind: .task)] + chat(at(19), 6, every: 10) + chat(at(21, 30), 6, every: 8)
    case "silent":
        return []
    case "corrected":
        return chat(at(8, 30), 3) + [WeekEvent(minute: at(10), kind: .task)] + chat(at(12, 30), 2)
            + chat(at(14), 2) + [WeekEvent(minute: at(14, 6), kind: .correction)]
            + chat(at(19), 6, every: 10) + chat(at(21, 30), 6, every: 8)
    case "praise":
        return chat(at(8, 30), 3) + [WeekEvent(minute: at(9), kind: .task), WeekEvent(minute: at(9, 35), kind: .praise)]
            + [WeekEvent(minute: at(10), kind: .task), WeekEvent(minute: at(11), kind: .task)] + chat(at(12, 30), 2)
            + [WeekEvent(minute: at(12, 36), kind: .praise), WeekEvent(minute: at(15), kind: .task)]
            + chat(at(19), 6, every: 10) + [WeekEvent(minute: at(20), kind: .praise)] + chat(at(21, 30), 6, every: 8)
    case "failures":
        return chat(at(8, 30), 3) + [WeekEvent(minute: at(10), kind: .failedTask), WeekEvent(minute: at(10, 20), kind: .toolErrors),
                                     WeekEvent(minute: at(11), kind: .failedTask), WeekEvent(minute: at(15), kind: .failedTask)]
            + chat(at(19), 6, every: 10)
    case "quiet evening":
        return chat(at(8, 30), 3) + [WeekEvent(minute: at(10), kind: .task)] + chat(at(12, 30), 2)
    default:
        fatalError(name)
    }
}

@Test(.enabled(if: weekOut != nil))
func m6DrivesWeek() throws {
    let c = DrivesConfig()
    let days = ["normal", "silent", "corrected", "praise", "failures", "quiet evening", "normal"]
    let start = Day("2026-11-16")!.at(TimeOfDay("08:00")!)
    var d = Drives(config: c, now: start)
    var asleep = false
    var busyUntil = start
    var out: [String] = []
    var curves: [String] = []
    out.append("m6-drives simulated week (DrivesConfig defaults; see the assumptions in Tests/DollCoreTests/M6DrivesWeek.swift)")
    out.append("")
    out.append("day                 wanderings (time signal)                                  slept   壓力 back in range")
    for (i, name) in days.enumerated() {
        let day0 = Day("2026-11-16")!.adding(i).at(TimeOfDay("00:00")!)
        let events = schedule(name)
        var wanders: [String] = []
        var slept = "-"
        var stressBack = "-"
        var correctionAt: Double?
        curves.append("\(name) (day \(i + 1)): hour  動力  壓力  親近  睡意")
        var ends: [(minute: Int, ok: Bool)] = []
        // Minute by minute from 08:00 to 08:00 next day.
        for m in (8 * 60)..<(32 * 60) {
            let t = day0 + Double(m) * 60
            // Waking at 08:00 (the first day starts awake).
            if asleep, m == 8 * 60 {
                asleep = false
                d.apply(.wake, at: t, detail: "睡到起床")
            }
            for e in events where e.minute == m {
                switch e.kind {
                case .talk: d.apply(.ownerTalk, at: t, detail: "line"); busyUntil = max(busyUntil, t + 600)
                case .task: busyUntil = max(busyUntil, t + 1800); ends.append((m + 30, true))
                case .failedTask: busyUntil = max(busyUntil, t + 1800); ends.append((m + 30, false))
                case .praise: d.apply(.praise, at: t, detail: "praise")
                case .correction: d.apply(.correction, at: t, detail: "correction"); correctionAt = t
                case .toolErrors: for _ in 0..<3 { d.apply(.toolErrors, at: t, detail: "err") }
                }
            }
            for e in ends where e.minute == m { d.apply(e.ok ? .itemDone : .itemFailed, at: t, detail: "task") }
            if let ca = correctionAt, stressBack == "-", t > ca, !d.isOut(.stress, at: t) {
                stressBack = String(format: "%.1f h after", (t - ca) / 3600)
            }
            if !asleep, m == 25 * 60 {   // the calendar's 睡覺 at 01:00
                asleep = true
                slept = "01:00 (睡覺)"
                d.apply(.sleep, at: t, detail: "睡覺")
            }
            let pending = d.pending(at: t)
            if !asleep, t >= busyUntil, let s = pending.first {
                d.state.surfaced.insert(s)
                wanders.append("\(Doll.hhmm(t)) \(s.label)")
                busyUntil = t + 300
                if s == .sleepiness {
                    asleep = true
                    slept = "\(Doll.hhmm(t)) (睡意)"
                    d.apply(.sleep, at: t, detail: "睡意")
                }
            }
            if m % 60 == 0 {
                var v = d.values(at: t)
                // Asleep, 親近 is pinned at its value when she fell asleep (it resumes from there at the wake).
                if asleep { v[.closeness] = d.state.points[.closeness]!.value }
                curves.append(String(format: "  %@  %.2f  %.2f  %.2f  %.2f%@", Doll.hhmm(t), v[.drive]!, v[.stress]!, v[.closeness]!, v[.sleepiness]!,
                                     asleep ? "  (asleep)" : ""))
            }
        }
        let w = wanders.isEmpty ? "none" : "\(wanders.count): " + wanders.joined(separator: ", ")
        out.append(name.padding(toLength: 20, withPad: " ", startingAt: 0) + w.padding(toLength: 58, withPad: " ", startingAt: 0)
                   + " " + slept.padding(toLength: 14, withPad: " ", startingAt: 0) + " " + stressBack)
    }
    out.append("")
    out.append("Hourly values (one line per hour, 08:00 → 07:00 next day):")
    out += curves
    let url = URL(filePath: weekOut!, relativeTo: URL(filePath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent())
    try out.joined(separator: "\n").write(to: url, atomically: true, encoding: .utf8)
    print(out.prefix(12).joined(separator: "\n"))
}
