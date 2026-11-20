import Foundation

/// The boot report (2026-11-18, owner: after a boot she checks the log of the last run; for now she
/// only notices and reports, she repairs nothing).
///
/// At boot the program reads the last run from the log (`RunLog.lastRun`, that run only), groups its
/// WARN and ERROR lines by kind, says how it ended and what it left unfinished that the program knows
/// for certain, and writes it all to `<data_dir>/boot-reports/<boot>-<pid>.json` (every kind, for the
/// owner's dashboard and a later repair layer). The boot notice carries a capped text of it when it has
/// anything; it gives her a turn when there was an ERROR, the run did not end cleanly, or its WARN
/// lines reach `[log] turn_warns`. A clean run with no WARN or ERROR adds nothing: boot is as before.
///
/// Not repeated here: the calendar starts missed while down (the boot notice's calendar part lists
/// them, and a missed system maintenance with them); a maintenance that failed is in the ERROR kinds.
public struct BootReport: Codable, Sendable, Equatable {
    /// This boot (the doll's clock), its process and build.
    public var boot: Double
    public var bootTime: String
    public var pid: Int32
    public var build: String
    /// The run before; nil: the data dir has no log (older data dir, or the first boot).
    public var previous: LastRun?
    /// What the last run left unfinished that the program knows for certain, in her words.
    public var unfinished: [String]
    /// Why she got a turn: `error` (any ERROR line), `ending` (not a clean end), `warnings` (WARN lines
    /// reached `[log] turn_warns`); empty: no turn.
    public var why: [String]
    public var turn: Bool { !why.isEmpty }
    /// The report as her boot notice carries it; nil: it said nothing.
    public var notice: String?
    /// Kinds the notice listed and left out (the JSON's `previous.kinds` has every one).
    public var kindsListed: Int
    public var kindsLeftOut: Int
    /// Her history at this boot (`History.swift`): started now, or files found changed with no commit
    /// (committed now as found at boot); nil when it was in order.
    public var history: String? = nil
}

extension Doll {
    /// Tells her this process just started (owner, 2026-11-14): a program line in the timeline with
    /// today's calendar and, when the last run had something, its report (2026-11-18). It gives her
    /// a turn only when the report says so; otherwise she reads it with the next event.
    @discardableResult
    public func announceBoot(previous: LastRun? = nil, build: String = "") throws -> BootReport {
        let calendar = try calendarBootText()
        let now = wallClock.now()
        var r = BootReport(boot: now, bootTime: LogFile.timeText(now), pid: ProcessInfo.processInfo.processIdentifier, build: build,
                           previous: previous, unfinished: [], why: [], notice: nil, kindsListed: 0, kindsLeftOut: 0)
        let file = bootReports.appending(path: Self.bootReportName(now, pid: r.pid))
        if let p = previous {
            if p.ending.how != .clean, todoBusy {
                r.unfinished.append("你手上的「\(todo.current ?? "")」那時是\(todo.phase.label)，還在你的 todo 裡；最後一步做到哪裡，程式不確定")
            }
            if maintenanceConfig.enabled, maintenanceState.running != nil {
                let done = maintenanceState.done.compactMap { MaintenanceTask(rawValue: $0)?.label }
                r.unfinished.append("那時系統維護做到一半" + (done.isEmpty ? "" : "（做完了：\(done.joined(separator: "、"))）") + "，這次開機接著做沒做完的，做完會告訴你")
            }
            if p.errors > 0 { r.why.append("error") }
            if p.ending.how != .clean { r.why.append("ending") }
            if p.warnings >= config.log.turnWarns { r.why.append("warnings") }
            if !r.why.isEmpty || p.warnings > 0 || !r.unfinished.isEmpty {
                let shown = min(p.kinds.count, config.log.reportKinds)
                r.kindsListed = shown
                r.kindsLeftOut = p.kinds.count - shown
                r.notice = Self.reportText(p, unfinished: r.unfinished, file: file.path, log: config.log)
            }
        }
        // Her history at this boot (2026-11-19, `History.swift`): started (in the report and the log
        // only: nothing happened to her), or changes found with no commit (her notice says so).
        var found: String?
        switch historyBoot {
        case .adopted(let n):
            r.history = "開始記錄改動：照開機時的樣子記成第一筆，共 \(n) 個檔"
            log("INFO", "history: started, the dirs as found (\(n) files) are the first commit")
        case .found(let paths):
            r.history = "上次結束前有 \(paths.count) 個檔改了卻沒記進改動紀錄（程式當掉或記錄失敗），這次開機補記成一筆「開機時發現，沒有記錄」：\(history.things(paths))"
            found = r.history
            log("WARN", "history: found at boot, not recorded: \(paths)")
        case .clean: break
        }
        try FileManager.default.createDirectory(at: bootReports, withIntermediateDirectories: true)
        let enc = JSONEncoder()
        enc.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        enc.keyEncodingStrategy = .convertToSnakeCase
        try enc.encode(r).write(to: file, options: .atomic)
        try append(Event(.memory, .internal, Self.bootNotice + calendar + (r.notice.map { "\n" + $0 } ?? "") + (found.map { "\n" + $0 + "。" } ?? "")))
        if r.turn {
            followUp = true
            taskTurn = true
            log("INFO", "boot report: a turn for her (\(r.why.joined(separator: ", "))); \(file.path)")
        } else {
            log("INFO", "boot report: " + (r.notice == nil ? "nothing to say" : "in the boot notice, no turn") + "; \(file.path)")
        }
        return r
    }
    static let bootNotice = "【開機】你剛被開機了。"

    static func bootReportName(_ ts: Double, pid: Int32) -> String {
        let c = Calendar.current.dateComponents([.year, .month, .day, .hour, .minute, .second], from: Date(timeIntervalSince1970: ts))
        return String(format: "%04d%02d%02d-%02d%02d%02d-%d.json", c.year!, c.month!, c.day!, c.hour!, c.minute!, c.second!, pid)
    }

    /// `11-18 09:12:03`.
    static func reportTime(_ ts: Double?) -> String {
        guard let ts else { return "（沒有時間）" }
        let c = Calendar.current.dateComponents([.month, .day, .hour, .minute, .second], from: Date(timeIntervalSince1970: ts))
        return String(format: "%02d-%02d %02d:%02d:%02d", c.month!, c.day!, c.hour!, c.minute!, c.second!)
    }

    static func cut(_ s: String, _ n: Int) -> String { s.count > n ? String(s.prefix(n)) + "…" : s }

    /// The report part of the boot notice: facts from the log, in plain words; at most
    /// `[log] report_kinds` kinds, then how many were left out.
    static func reportText(_ p: LastRun, unfinished: [String], file: String, log: LogConfig) -> String {
        var lines = ["【上次執行的紀錄】程式從上一次執行的 log 整理的："
            + (p.complete ? "那次 \(reportTime(p.started)) 開機" : "那次開頭的 log 已經輪替刪掉了，只看得到 \(reportTime(p.firstLine)) 以後的")
            + "，共 \(p.lines) 行。"]
        let detail = p.ending.detail.map { cut($0, log.exampleChars) } ?? ""
        switch p.ending.how {
        case .clean: lines.append("- 結束：正常關掉的（\(detail)），\(reportTime(p.ending.ts))。")
        case .fatal: lines.append("- 結束：程式出錯停下來的，\(reportTime(p.ending.ts))：\(detail)")
        case .bootFailed: lines.append("- 結束：開機到一半失敗，沒有起來，\(reportTime(p.ending.ts))：\(detail)")
        case .crashed: lines.append("- 結束：程式當掉了（\(detail)）；當掉前最後一行 log 在 \(reportTime(p.ending.ts))。")
        case .noEnd:
            // The last line is only when it last wrote something (2026-11-20, soak run: killed at 18:00
            // with its last line at 17:12, she told the owner it had stopped "around 17:12").
            lines.append("- 結束：沒有結束的紀錄，最後一行 log 在 \(reportTime(p.ending.ts))，之後就沒有了（那只是它最後一次寫 log 的時間；停掉是在那之後、這次開機之前的某個時候，確切時間不知道）。可能是被強制結束、當掉、或這台 Mac 關機／斷電，程式分不出是哪一種；Mac 睡眠不會讓程式結束，所以不是睡眠。")
        }
        if p.kinds.isEmpty {
            lines.append("- 沒有錯誤和警告。")
        } else {
            let e = p.kinds.filter { $0.level == "ERROR" }.count, w = p.kinds.count - e
            lines.append("- 錯誤 \(e) 種、警告 \(w) 種（同一種＝數字和編號以外都一樣）：")
            for k in p.kinds.prefix(log.reportKinds) {
                let span = k.count == 1 ? reportTime(k.first) : "\(reportTime(k.first)) 到 \(reportTime(k.last))"
                lines.append("  \(k.level == "ERROR" ? "錯誤" : "警告") \(k.count) 次（\(span)），例：\(cut(k.example, log.exampleChars))")
            }
            if p.kinds.count > log.reportKinds {
                let left = p.kinds.dropFirst(log.reportKinds)
                lines.append("  還有 \(left.count) 種沒列（共 \(left.reduce(0) { $0 + $1.count }) 行）。")
            }
        }
        if !unfinished.isEmpty { lines.append("- 沒做完的：" + unfinished.joined(separator: "；") + "。") }
        lines.append("完整的紀錄在 \(file)。")
        return lines.joined(separator: "\n")
    }

    /// The latest boot reports, newest first (dashboard).
    public func dashboardBoots(limit: Int) throws -> [BootReport] {
        guard FileManager.default.fileExists(atPath: bootReports.path) else { return [] }
        let names = try FileManager.default.contentsOfDirectory(atPath: bootReports.path).filter { $0.hasSuffix(".json") }.sorted(by: >)
        let dec = JSONDecoder()
        dec.keyDecodingStrategy = .convertFromSnakeCase
        return try names.prefix(limit).map { name in
            do { return try dec.decode(BootReport.self, from: Data(contentsOf: bootReports.appending(path: name))) } catch {
                throw StoreError(description: "boot report \(name) unreadable: \(error)")
            }
        }
    }
}
