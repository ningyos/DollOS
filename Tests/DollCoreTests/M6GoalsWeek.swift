import Foundation
import Testing
@testable import DollCore

// m6-drives phase 2 (2026-11-16): the simulated week for her own direction, no model. The phase-1
// week (M6DrivesWeek.swift: the owner's schedule, the signals, the wandering rule) plus the tree,
// the record's two-stage achievement and curiosity, with the numbers in dollcore.example.toml.
// Runs only with M6_GOALS_WEEK=<out file>:
//
//     M6_GOALS_WEEK=experiments/m6-drives/goals-week.txt swift test --scratch-path /tmp/m6build --filter m6GoalsWeek
//
// Assumptions (the model is not in the loop):
// - Her tree always has a next step (a sub-goal's steps run out → she adds the next ones), so a
//   動力 wandering of her own always finds tree work; she picks it up at once (one step, 30 min,
//   done). Since idle-drive (2026-11-20) 動力 climbs while she is left alone and the episode ends at
//   the first idle minute after it (動力 back to drive_reset; what the step added is wiped): no chain.
// - Every third tree step meets something new on the way (a page she had not seen): novelty.
// - The owner's tasks are todo lists (one item, done): item_done + list_drive's share; on the praise
//   days he evaluates each good (the rest of list_drive), on the corrected day the 14:06 correction
//   is on the 10:00 task (its share taken back).
// - A curiosity wandering: she browses 30 min and meets something new.
// - 親近 and 壓力 wanderings are turns without tree work; the owner does not answer them.

private let goalsWeekOut = ProcessInfo.processInfo.environment["M6_GOALS_WEEK"]

private struct Ev { let minute: Int; let kind: Kind; enum Kind { case talk, task, failedTask, praise, correction, toolErrors } }

private func daySchedule(_ name: String) -> [Ev] {
    func at(_ h: Int, _ m: Int = 0) -> Int { h * 60 + m }
    func chat(_ from: Int, _ lines: Int, every: Int = 6) -> [Ev] { (0..<lines).map { Ev(minute: from + $0 * every, kind: .talk) } }
    let day = chat(at(8, 30), 3) + [Ev(minute: at(10), kind: .task)] + chat(at(12, 30), 2) + [Ev(minute: at(15), kind: .task)]
    switch name {
    case "normal": return day + chat(at(19), 6, every: 10) + chat(at(21, 30), 6, every: 8)
    case "late (23:00)": return day + chat(at(19), 6, every: 10) + chat(at(22), 7, every: 10)
    case "early (18:00)": return day + chat(at(17, 10), 6, every: 10)
    case "silent": return []
    case "corrected":
        return chat(at(8, 30), 3) + [Ev(minute: at(10), kind: .task)] + chat(at(12, 30), 2)
            + chat(at(14), 2) + [Ev(minute: at(14, 6), kind: .correction)] + chat(at(19), 6, every: 10) + chat(at(21, 30), 6, every: 8)
    case "praise":
        return chat(at(8, 30), 3) + [Ev(minute: at(9), kind: .task), Ev(minute: at(9, 35), kind: .praise)]
            + [Ev(minute: at(10), kind: .task), Ev(minute: at(11), kind: .task)] + chat(at(12, 30), 2)
            + [Ev(minute: at(12, 36), kind: .praise), Ev(minute: at(15), kind: .task)]
            + chat(at(19), 6, every: 10) + [Ev(minute: at(20), kind: .praise)] + chat(at(21, 30), 6, every: 8)
    case "failures":
        return chat(at(8, 30), 3) + [Ev(minute: at(10), kind: .failedTask), Ev(minute: at(10, 20), kind: .toolErrors),
                                     Ev(minute: at(11), kind: .failedTask), Ev(minute: at(15), kind: .failedTask)]
            + chat(at(19), 6, every: 10)
    default: fatalError(name)
    }
}

/// One week; returns the table rows. `tune` changes the numbers for a variant.
private func simulateWeek(_ c: DrivesConfig, _ g: GoalsConfig, curves: inout [String]) -> [String] {
    let days = ["normal", "late (23:00)", "early (18:00)", "silent", "corrected", "praise", "failures", "normal"]
    let start = Day("2026-11-16")!.at(TimeOfDay("08:00")!)
    var d = Drives(config: c, now: start)
    var busyUntil = start
    var lastNew = start, curiositySurfaced = 0.0
    var stepsTotal = 0
    var rows: [String] = []
    for (i, name) in days.enumerated() {
        let day0 = Day("2026-11-16")!.adding(i).at(TimeOfDay("00:00")!)
        let events = daySchedule(name)
        var causes: [String: Int] = [:]
        var steps = 0
        var lo = 1.0, hi = 0.0
        var stepEnds: [Double] = []
        var taskEnds: [(Double, Bool, Double)] = []   // end, ok, the share given (for the correction)
        var lastTask: Int?
        for m in (8 * 60)..<(32 * 60) {
            let t = day0 + Double(m) * 60
            for e in events where e.minute == m {
                switch e.kind {
                case .talk: d.apply(.ownerTalk, at: t, detail: "line"); busyUntil = max(busyUntil, t + 600)
                case .task: busyUntil = max(busyUntil, t + 1800); taskEnds.append((t + 1800, true, 0))
                case .failedTask: busyUntil = max(busyUntil, t + 1800); taskEnds.append((t + 1800, false, 0))
                case .praise:
                    d.apply(.praise, at: t, detail: "praise")
                    if lastTask != nil { d.move(.drive, by: g.listDrive * (1 - g.nowShare), at: t, cause: "good") }
                case .correction:
                    d.apply(.correction, at: t, detail: "correction")
                    if lastTask != nil { d.move(.drive, by: -g.listDrive * g.nowShare, at: t, cause: "taken back") }
                case .toolErrors: for _ in 0..<3 { d.apply(.toolErrors, at: t, detail: "err") }
                }
            }
            for (k, e) in taskEnds.enumerated() where e.0 == t {
                if e.1 {
                    d.apply(.itemDone, at: t, detail: "task")
                    d.move(.drive, by: g.listDrive * g.nowShare, at: t, cause: "list")
                    lastTask = k
                } else { d.apply(.itemFailed, at: t, detail: "task") }
            }
            for e in stepEnds where e == t {
                d.move(.drive, by: g.stepDrive * g.nowShare, at: t, cause: "step")
                stepsTotal += 1
                if stepsTotal % 3 == 0 { d.move(.drive, by: g.noveltyDrive, at: t, cause: "new"); lastNew = t }
                // Every third step finishes a sub-goal.
                if stepsTotal % 3 == 0 { d.move(.drive, by: g.subgoalDrive * g.nowShare, at: t, cause: "sub-goal") }
            }
            if m == 27 * 60 { busyUntil = max(busyUntil, t + 600) }   // maintenance 03:00–03:10
            let v = d.value(.drive, at: t)
            lo = min(lo, v); hi = max(hi, v)
            if m % 60 == 0 {
                let vs = d.values(at: t)
                curves.append(String(format: "  day %d %@  %.2f  %.2f  %.2f", i + 1, Doll.hhmm(t), vs[.drive]!, vs[.stress]!, vs[.closeness]!))
            }
            // Idle again after an episode of her own: 動力 back to drive_reset (Drives.endEpisode).
            if t >= busyUntil { _ = d.endEpisode(at: t, cause: "episode") }
            let pending = d.pending(at: t)
            guard t >= busyUntil else { continue }
            if let s = pending.first {
                d.state.surfaced.insert(s)
                if s == .drive {
                    causes["動力→樹", default: 0] += 1; steps += 1
                    stepEnds.append(t + 1800); busyUntil = t + 1800
                } else {
                    causes[s.label, default: 0] += 1
                    busyUntil = t + 300
                }
                continue
            }
            if t >= max(lastNew, curiositySurfaced) + g.curiosityHours * 3600 {
                curiositySurfaced = t
                causes["好奇", default: 0] += 1
                busyUntil = t + 1800
                d.move(.drive, by: g.noveltyDrive, at: t + 1800, cause: "new")
                lastNew = t + 1800
            }
        }
        let order = ["親近", "壓力", "動力→樹", "好奇"]
        let w = order.compactMap { k in causes[k].map { "\(k) \($0)" } }.joined(separator: "，")
        rows.append("\(i + 1) " + name.padding(toLength: 14, withPad: " ", startingAt: 0) + " | "
                    + (w.isEmpty ? "none" : w).padding(toLength: 44, withPad: " ", startingAt: 0) + " | "
                    + "\(steps)".padding(toLength: 10, withPad: " ", startingAt: 0) + " | "
                    + String(format: "%.2f–%.2f", lo, hi))
    }
    return rows
}

@Test(.enabled(if: goalsWeekOut != nil))
func m6GoalsWeek() throws {
    let cfg = try Config.load(URL(filePath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        .appending(path: "dollcore.example.toml"))
    var out = ["m6-drives phase 2 simulated week (dollcore.example.toml's [drives] and [goals]; assumptions in Tests/DollCoreTests/M6GoalsWeek.swift)",
               "Each day 08:00 → 08:00; maintenance 03:00 (10 min). Wanderings by cause; tree steps picked up; 動力 min–max over the day.", ""]
    let header = "day                | wanderings by cause                          | tree steps | 動力 range"
    var curves: [String] = []
    out += ["example.toml:", header] + simulateWeek(cfg.drives, cfg.goals, curves: &curves)
    var variants: [(String, DrivesConfig, GoalsConfig)] = []
    for h in [0.4, 0.9] {
        var v = cfg.drives; v.driveHalfLifeH = h
        variants.append(("drive_half_life_h \(h)", v, cfg.goals))
    }
    var reset = cfg.drives; reset.driveReset = 0.6
    variants.append(("drive_reset 0.6", reset, cfg.goals))
    for (label, dc, gc) in variants {
        var ignore: [String] = []
        out += ["", label + ":", header] + simulateWeek(dc, gc, curves: &ignore)
    }
    out += ["", "Hourly values with example.toml (動力 壓力 親近):"] + curves
    let url = URL(filePath: goalsWeekOut!, relativeTo: URL(filePath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent())
    try out.joined(separator: "\n").write(to: url, atomically: true, encoding: .utf8)
    print(out.prefix(50).joined(separator: "\n"))
}
