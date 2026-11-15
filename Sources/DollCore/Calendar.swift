import Foundation

/// Her calendar (2026-11-15, owner): future events and the day's timetable, hers, editable by the
/// owner from the dashboard. One file, `<data_dir>/calendar.json` (not part of a day's timeline, so
/// a new segment does not touch it).
///
/// An item is all-day (a date) or timed (a start, optionally an end), on one date or repeating
/// (`daily`, or ISO weekdays `1,3,5` with 1 = Monday, 7 = Sunday) from its date, optionally until a
/// date. A repeating item is the series; a change or removal for one day is an exception keyed by
/// the day it would have happened (`exceptions["2026-11-20"]`), so the series stays as it was.
///
/// Times are wall-clock local time (`TimeZone.current`). A start that does not exist on a DST day
/// is the one Foundation picks (the next valid instant); this machine's zone (Asia/Taipei) has none.
///
/// `fired_through`: every timed start at or before it has been dealt with (fired as a turn, or
/// listed as missed in the boot notice). A start fires when it falls in (fired_through, now]; the
/// mark moves to now, saved, before the events are queued. Edits first fire what is due, then
/// move the mark to now, so a start already past when it is added never fires and none is skipped.
///
/// Her past items stay (owner may ask what happened last week); nothing prunes them yet.

/// "Now", injectable (tests move it); `sleep(until:)` returns at or after that instant, or throws
/// when cancelled.
public protocol DollClock: Sendable {
    func now() -> Double
    func sleep(until t: Double) async throws
}

/// The machine's clock. The continuous clock keeps counting while the Mac sleeps, so a start that
/// passed during sleep fires on wake (once: the caller re-checks `now`).
public struct SystemClock: DollClock {
    public init() {}
    public func now() -> Double { Date().timeIntervalSince1970 }
    public func sleep(until t: Double) async throws {
        let d = t - now()
        if d > 0 { try await Task.sleep(for: .seconds(d), clock: .continuous) }
    }
}

/// A local calendar day.
public struct Day: Hashable, Comparable, Sendable, CustomStringConvertible {
    public let y: Int, m: Int, d: Int

    static var cal: Calendar {
        var c = Calendar(identifier: .gregorian)
        c.timeZone = .current
        return c
    }

    /// Strict `YYYY-MM-DD`, a real date.
    public init?(_ s: String) {
        let p = s.trimmingCharacters(in: .whitespaces).split(separator: "-", omittingEmptySubsequences: false)
        guard p.count == 3, p[0].count == 4, p[1].count == 2, p[2].count == 2,
              let y = Int(p[0]), let m = Int(p[1]), let d = Int(p[2]),
              let date = Self.cal.date(from: DateComponents(year: y, month: m, day: d)) else { return nil }
        let c = Self.cal.dateComponents([.year, .month, .day], from: date)
        guard c.year == y, c.month == m, c.day == d else { return nil }
        self.y = y; self.m = m; self.d = d
    }

    public init(ts: Double) {
        let c = Self.cal.dateComponents([.year, .month, .day], from: Date(timeIntervalSince1970: ts))
        y = c.year!; m = c.month!; d = c.day!
    }

    public var description: String { String(format: "%04d-%02d-%02d", y, m, d) }
    /// `2026-11-18（星期三）`
    public var label: String { "\(self)（星期\(Self.weekdayNames[weekday - 1])）" }
    static let weekdayNames = ["一", "二", "三", "四", "五", "六", "日"]

    /// ISO weekday: 1 = Monday … 7 = Sunday.
    public var weekday: Int {
        let w = Self.cal.component(.weekday, from: midnight)   // 1 = Sunday
        return w == 1 ? 7 : w - 1
    }

    var midnight: Date { Self.cal.date(from: DateComponents(year: y, month: m, day: d))! }

    public func adding(_ n: Int) -> Day {
        Day(ts: Self.cal.date(byAdding: .day, value: n, to: midnight)!.timeIntervalSince1970)
    }

    /// Seconds since 1970 of `t` on this day, local time.
    public func at(_ t: TimeOfDay) -> Double {
        Self.cal.date(from: DateComponents(year: y, month: m, day: d, hour: t.h, minute: t.m))!.timeIntervalSince1970
    }

    public static func < (a: Day, b: Day) -> Bool { (a.y, a.m, a.d) < (b.y, b.m, b.d) }
}

/// A wall-clock time, `HH:MM` (one-digit hours read too).
public struct TimeOfDay: Hashable, Comparable, Sendable, CustomStringConvertible {
    public let h: Int, m: Int
    public init?(_ s: String) {
        let p = s.trimmingCharacters(in: .whitespaces).split(separator: ":", omittingEmptySubsequences: false)
        guard p.count == 2, (1...2).contains(p[0].count), p[1].count == 2, let h = Int(p[0]), let m = Int(p[1]),
              (0...23).contains(h), (0...59).contains(m) else { return nil }
        self.h = h; self.m = m
    }
    public var description: String { String(format: "%02d:%02d", h, m) }
    var minutes: Int { h * 60 + m }
    public static func < (a: TimeOfDay, b: TimeOfDay) -> Bool { a.minutes < b.minutes }
}

/// How an item repeats: every day, or on ISO weekdays.
public enum Repeat: Equatable, Sendable, CustomStringConvertible {
    case daily
    case weekdays([Int])

    /// `daily`, or weekday numbers 1–7 separated by commas (`1,3,5`).
    public init?(_ s: String) {
        let t = s.trimmingCharacters(in: .whitespaces)
        if t == "daily" { self = .daily; return }
        let n = t.split(separator: ",").map { Int($0.trimmingCharacters(in: .whitespaces)) }
        guard !n.isEmpty, n.allSatisfy({ $0.map { (1...7).contains($0) } ?? false }) else { return nil }
        self = .weekdays(Array(Set(n.map { $0! })).sorted())
    }
    public var description: String {
        switch self {
        case .daily: "daily"
        case .weekdays(let w): w.map(String.init).joined(separator: ",")
        }
    }
    /// `每天` / `每週一、三、五`
    var label: String {
        switch self {
        case .daily: "每天"
        case .weekdays(let w): "每週" + w.map { Day.weekdayNames[$0 - 1] }.joined(separator: "、")
        }
    }
    func matches(_ day: Day) -> Bool {
        switch self {
        case .daily: true
        case .weekdays(let w): w.contains(day.weekday)
        }
    }
}

public struct CalendarError: Error, CustomStringConvertible {
    public let description: String
}

/// The file's shape (`calendar.json`, pretty-printed, keys sorted).
struct CalendarFile: Codable {
    struct Exception: Codable, Equatable {
        /// This day's occurrence does not happen.
        var skip: Bool?
        var title: String?
        /// Moved to another day.
        var date: String?
        var start: String?
        var end: String?
        /// This day it is all-day (no start, no end).
        var allDay: Bool?
        enum CodingKeys: String, CodingKey { case skip, title, date, start, end, allDay = "all_day" }
    }
    struct Item: Codable {
        var id: Int
        var title: String
        /// The day, or a series' first day.
        var date: String
        var start: String?
        var end: String?
        var `repeat`: String?
        var until: String?
        /// Original day → that day's change.
        var exceptions: [String: Exception]?
        /// Who added it: `doll`, `owner`, or `program` (the maintenance and owner-sleep items).
        var by: String
        /// `maintenance` (2026-11-16, `Maintenance.swift`): the daily 「系統維護」, at most one; daily
        /// and timed. `owner_sleep` (2026-11-16): the daily 「主人睡覺」 span, at most one, with an
        /// end; its end may be at or before its start, then it ends the next day (23:00–07:00); it
        /// gives no turn. Either can be moved like any item, not deleted or made all-day. (`sleep`
        /// and `wake`, the sleep's items before that, are read only to be removed: `Agenda.removedRoles`.)
        var role: String?
    }
    var firedThrough: String?
    var nextID: Int
    var items: [Item]
    enum CodingKeys: String, CodingKey { case firedThrough = "fired_through", nextID = "next_id", items }
}

/// One day's happening of an item.
public struct Occurrence: Sendable, Equatable {
    public let id: Int
    public let title: String
    public let day: Day
    /// The series day it stands for (== `day` unless moved).
    public let original: Day
    public let start: TimeOfDay?
    public let end: TimeOfDay?
    public let repeating: Repeat?
    public let changed: Bool
    /// The item's role (`maintenance`, `owner_sleep`), nil for an ordinary item.
    public var role: String? = nil

    /// An end not after the start (only the owner-sleep span has one) is the next day's.
    var overnight: Bool { start.flatMap { s in end.map { $0 <= s } } ?? false }
    public var startTS: Double? { start.map(day.at) }
    public var endTS: Double? { end.map((overnight ? day.adding(1) : day).at) }

    /// `10:00–11:00 看牙醫（#4，每週三）` / `全天 生日（#2）` / `23:00–隔天 07:00 主人睡覺（#3，每天）`
    public var line: String {
        let time = start.map { s in end.map { "\(s)–\(overnight ? "隔天 " : "")\($0)" } ?? "\(s)" } ?? "全天"
        let notes = ["#\(id)"] + [repeating?.label, changed ? "這天改過" : nil].compactMap { $0 }
        return "\(time) \(title)（\(notes.joined(separator: "，"))）"
    }
}

/// An edit or a question, from her tool or the dashboard (same parameters).
public enum CalendarRequest: Sendable, Equatable {
    case add(title: String, day: Day, start: TimeOfDay?, end: TimeOfDay?, repeat: Repeat?, until: Day?)
    /// `on`: a repeating item's one day; nil = the whole item.
    case change(id: Int, on: Day?, title: String?, day: Day?, start: TimeOfDay?, end: TimeOfDay?, allDay: Bool)
    case delete(id: Int, on: Day?)
    case list(from: Day, to: Day?)

    public static let names = ["calendar.add", "calendar.change", "calendar.delete", "calendar.list"]

    /// `name` is a tool name (`calendar.add` …); `p` its parameters as text. Throws what was wrong.
    public static func parse(_ name: String, _ p: [String: String]) throws -> CalendarRequest {
        func get(_ k: String) -> String? {
            p[k].map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.flatMap { $0.isEmpty ? nil : $0 }
        }
        func day(_ k: String) throws -> Day? {
            guard let v = get(k) else { return nil }
            guard let d = Day(v) else { throw CalendarError(description: "\(name) 的 \(k) 要是 YYYY-MM-DD 的日期，例：2026-11-18；收到 \(v)") }
            return d
        }
        func time(_ k: String) throws -> TimeOfDay? {
            guard let v = get(k) else { return nil }
            guard let t = TimeOfDay(v) else { throw CalendarError(description: "\(name) 的 \(k) 要是 24 小時制的 HH:MM，例：15:00；收到 \(v)") }
            return t
        }
        func id() throws -> Int {
            let v = (get("id") ?? "").trimmingCharacters(in: CharacterSet(charactersIn: "#"))
            guard let n = Int(v) else { throw CalendarError(description: "\(name) 的 id 要是項目的編號（#4 就填 4）；收到 \(p["id"] ?? "")") }
            return n
        }
        switch name {
        case "calendar.add":
            guard let title = get("title") else { throw CalendarError(description: "calendar.add 的 title 是空的") }
            guard let d = try day("date") else { throw CalendarError(description: "calendar.add 少了 date") }
            let start = try time("start"), end = try time("end")
            var rep: Repeat?
            if let r = get("repeat") {
                guard let parsed = Repeat(r) else {
                    throw CalendarError(description: "calendar.add 的 repeat 要是 daily，或星期幾的數字用逗號分開（1＝星期一…7＝星期日，例：1,3,5）；收到 \(r)")
                }
                rep = parsed
            }
            let until = try day("until")
            if until != nil, rep == nil { throw CalendarError(description: "calendar.add 的 until 只配 repeat 用") }
            return .add(title: title, day: d, start: start, end: end, repeat: rep, until: until)
        case "calendar.change":
            var allDay = false
            if let a = get("all_day") {
                guard a == "true" || a == "false" else { throw CalendarError(description: "calendar.change 的 all_day 只能是 true 或 false；收到 \(a)") }
                allDay = a == "true"
            }
            return .change(id: try id(), on: try day("on"), title: get("title"), day: try day("date"),
                           start: try time("start"), end: try time("end"), allDay: allDay)
        case "calendar.delete":
            return .delete(id: try id(), on: try day("on"))
        case "calendar.list":
            guard let from = try day("from") else { throw CalendarError(description: "calendar.list 少了 from") }
            return .list(from: from, to: try day("to"))
        default:
            throw CalendarError(description: "沒有 \(name) 這個行事曆動作")
        }
    }
}

/// The calendar file in memory; the actor that owns it (`Doll`) serializes every use.
final class Agenda {
    let url: URL
    private(set) var file: CalendarFile
    /// `fired_through`'s format: local time with its offset, to the millisecond.
    private static var stamp: ISO8601DateFormatter {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        f.timeZone = .current
        return f
    }

    /// A missing file is an empty calendar; one that does not read is an error naming the problem.
    init(url: URL) throws {
        self.url = url
        guard FileManager.default.fileExists(atPath: url.path) else {
            file = CalendarFile(firedThrough: nil, nextID: 1, items: [])
            return
        }
        do {
            file = try JSONDecoder().decode(CalendarFile.self, from: Data(contentsOf: url))
        } catch {
            throw CalendarError(description: "\(url.path): not a calendar file: \(error)")
        }
        try validate()
    }

    private func validate() throws {
        func bad(_ what: String) -> CalendarError { CalendarError(description: "\(url.path): \(what)") }
        if let f = file.firedThrough, Self.stamp.date(from: f) == nil { throw bad("fired_through \(f) is not an ISO 8601 time") }
        var ids = Set<Int>()
        for it in file.items {
            guard ids.insert(it.id).inserted else { throw bad("id \(it.id) appears twice") }
            guard it.id < file.nextID else { throw bad("id \(it.id) is not below next_id \(file.nextID)") }
            guard !it.title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { throw bad("item \(it.id): empty title") }
            guard Day(it.date) != nil else { throw bad("item \(it.id): date \(it.date) is not YYYY-MM-DD") }
            for t in [it.start, it.end].compactMap({ $0 }) where TimeOfDay(t) == nil { throw bad("item \(it.id): time \(t) is not HH:MM") }
            if let r = it.role {
                guard r == Agenda.maintenanceRole || r == Agenda.ownerSleepRole || Agenda.removedRoles.contains(r) else {
                    throw bad("item \(it.id): role \(r) is not maintenance or owner_sleep")
                }
                guard file.items.filter({ $0.role == r }).count == 1 else { throw bad("role \(r) appears on more than one item") }
                guard it.repeat != nil, it.start != nil else { throw bad("item \(it.id): a \(r) item repeats and has a start") }
                if r == Agenda.ownerSleepRole, it.end == nil { throw bad("item \(it.id): the owner_sleep item has an end") }
            }
            if it.end != nil, it.start == nil { throw bad("item \(it.id): end without start") }
            if let s = it.start.flatMap(TimeOfDay.init), let e = it.end.flatMap(TimeOfDay.init),
               it.role == Agenda.ownerSleepRole ? e == s : e <= s {
                throw bad("item \(it.id): end not after start")
            }
            if let r = it.repeat, Repeat(r) == nil { throw bad("item \(it.id): repeat \(r) is not daily or weekdays 1-7") }
            if let u = it.until { guard Day(u) != nil, it.repeat != nil else { throw bad("item \(it.id): until \(u) needs a repeat and a YYYY-MM-DD date") } }
            for (k, x) in it.exceptions ?? [:] {
                guard Day(k) != nil else { throw bad("item \(it.id): exception day \(k) is not YYYY-MM-DD") }
                guard it.repeat != nil else { throw bad("item \(it.id): exceptions on an item that does not repeat") }
                if let d = x.date, Day(d) == nil { throw bad("item \(it.id) exception \(k): date \(d)") }
                for t in [x.start, x.end].compactMap({ $0 }) where TimeOfDay(t) == nil { throw bad("item \(it.id) exception \(k): time \(t)") }
            }
        }
    }

    func save() throws {
        let enc = JSONEncoder()
        enc.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try enc.encode(file).write(to: url, options: .atomic)
    }

    var firedThrough: Double? { file.firedThrough.flatMap { Self.stamp.date(from: $0)?.timeIntervalSince1970 } }
    func setFiredThrough(_ t: Double) { file.firedThrough = Self.stamp.string(from: Date(timeIntervalSince1970: t)) }

    // reading ------------------------------------------------------------------

    /// Every occurrence on days `from`…`to`, by day, all-day first, then by start, then id.
    func occurrences(from: Day, to: Day) -> [Occurrence] {
        var out: [Occurrence] = []
        for it in file.items {
            let first = Day(it.date)!
            let start = it.start.flatMap(TimeOfDay.init), end = it.end.flatMap(TimeOfDay.init)
            guard let rep = it.repeat.flatMap(Repeat.init) else {
                if from <= first, first <= to { out.append(Occurrence(id: it.id, title: it.title, day: first, original: first, start: start, end: end, repeating: nil, changed: false, role: it.role)) }
                continue
            }
            let until = it.until.flatMap(Day.init)
            let ex = it.exceptions ?? [:]
            var d = max(from, first)
            let last = min(to, until ?? to)
            while d <= last {
                if rep.matches(d), ex[d.description] == nil {
                    out.append(Occurrence(id: it.id, title: it.title, day: d, original: d, start: start, end: end, repeating: rep, changed: false, role: it.role))
                }
                d = d.adding(1)
            }
            for (k, x) in ex where x.skip != true {
                let o = Day(k)!
                guard o >= first, until.map({ o <= $0 }) ?? true, rep.matches(o) else { continue }
                let day = x.date.flatMap(Day.init) ?? o
                guard from <= day, day <= to else { continue }
                let (s, e) = x.allDay == true ? (nil, nil) : (x.start.flatMap(TimeOfDay.init) ?? start, x.end.flatMap(TimeOfDay.init) ?? (x.start == nil ? end : nil))
                out.append(Occurrence(id: it.id, title: x.title ?? it.title, day: day, original: o, start: s, end: e, repeating: rep, changed: true, role: it.role))
            }
        }
        return out.sorted { a, b in
            (a.day, a.start?.minutes ?? -1, a.id) < (b.day, b.start?.minutes ?? -1, b.id)
        }
    }

    /// The last day anything in the file can land on, other than an endless series.
    var horizon: Day {
        var last = Day(ts: 0)
        for it in file.items {
            last = max(last, Day(it.date)!)
            for (k, x) in it.exceptions ?? [:] { last = max(last, x.date.flatMap(Day.init) ?? Day(k)!) }
        }
        return last
    }

    /// Timed occurrences whose start is in (`after`, `through`], oldest first.
    func starts(after: Double, through: Double) -> [Occurrence] {
        guard through > after else { return [] }
        return occurrences(from: Day(ts: after).adding(-1), to: Day(ts: through).adding(1))
            .filter { o in o.startTS.map { $0 > after && $0 <= through } ?? false }
            .sorted { ($0.startTS!, $0.id) < ($1.startTS!, $1.id) }
    }

    /// The first timed start after `t`; nil when nothing timed lies ahead.
    func nextStart(after t: Double) -> Double? {
        let from = Day(ts: t).adding(-1)
        // A repeating series recurs within a week; one-off items and moved days end at `horizon`.
        let to = max(horizon, Day(ts: t).adding(8))
        return occurrences(from: from, to: to).compactMap(\.startTS).filter { $0 > t }.min()
    }

    func item(_ id: Int) -> CalendarFile.Item? { file.items.first { $0.id == id } }

    // editing ------------------------------------------------------------------

    /// Applies one edit; returns the occurrence lines it produced, for the result. Throws a refusal.
    func apply(_ r: CalendarRequest, by who: String) throws -> String {
        // What the owner put there is his (owner, 2026-11-16; real runs: she deleted his 06:30 wake-up
        // as a duplicate of her own reminder without telling him): she reads it, she does not edit it.
        if who == "doll", let id = r.id, let it = item(id), it.by == "owner" {
            throw CalendarError(description: "#\(id)「\(it.title)」是主人自己加的，你不能改也不能刪。覺得該改就跟主人說，讓他自己改；你要另外提醒自己，就加一項你自己的。")
        }
        switch r {
        case .list:
            fatalError("list is not an edit")
        case .add(let title, let day, let start, let end, let rep, let until):
            try checkTimes(start, end, "calendar.add")
            if let until, until < day { throw CalendarError(description: "calendar.add 的 until（\(until.label)）早於 date（\(day.label)）") }
            let id = file.nextID
            file.nextID += 1
            file.items.append(CalendarFile.Item(id: id, title: title, date: day.description, start: start?.description, end: end?.description,
                                                repeat: rep?.description, until: until?.description, exceptions: nil, by: who))
            return describe(id)
        case .change(let id, let on, let title, let day, let start, let end, let allDay):
            guard let i = file.items.firstIndex(where: { $0.id == id }) else { throw Self.noItem(id) }
            guard title != nil || day != nil || start != nil || end != nil || allDay else {
                throw CalendarError(description: "calendar.change 沒有要改的：給 title、date、start、end 或 all_day")
            }
            if allDay, start != nil || end != nil { throw CalendarError(description: "calendar.change 的 all_day 和 start、end 不能一起給") }
            var it = file.items[i]
            if allDay, let r = it.role { throw Self.roleRefusal(it, r, "改成全天") }
            let overnight = it.role == Agenda.ownerSleepRole
            let rep = it.repeat.flatMap(Repeat.init)
            if let on, rep == nil, on.description != it.date {
                throw CalendarError(description: "#\(id) 不重複，只在 \(Day(it.date)!.label)；改它不用 on")
            }
            if let on, rep != nil {
                try checkOn(it, on, rep!)
                var x = it.exceptions?[on.description] ?? CalendarFile.Exception()
                let curStart = x.allDay == true ? nil : (x.start.flatMap(TimeOfDay.init) ?? it.start.flatMap(TimeOfDay.init))
                let curEnd = x.allDay == true ? nil : (x.end.flatMap(TimeOfDay.init) ?? (x.start == nil ? it.end.flatMap(TimeOfDay.init) : nil))
                let (s, e) = try newTimes(curStart, curEnd, start, end, allDay, overnight: overnight)
                if let title { x.title = title }
                if let day { x.date = day == on ? nil : day.description }
                x.start = s?.description; x.end = e?.description; x.allDay = s == nil ? true : nil
                it.exceptions = (it.exceptions ?? [:]).merging([on.description: x]) { $1 }
                file.items[i] = it
                return describe(id, day: x.date.flatMap(Day.init) ?? on)
            }
            let (s, e) = try newTimes(it.start.flatMap(TimeOfDay.init), it.end.flatMap(TimeOfDay.init), start, end, allDay, overnight: overnight)
            if let title { it.title = title }
            if let day {
                if let u = it.until.flatMap(Day.init), u < day { throw CalendarError(description: "#\(id) 重複到 \(u.label) 為止，date 不能晚於它") }
                it.date = day.description
            }
            it.start = s?.description; it.end = e?.description
            file.items[i] = it
            return describe(id)
        case .delete(let id, let on):
            guard let i = file.items.firstIndex(where: { $0.id == id }) else { throw Self.noItem(id) }
            var it = file.items[i]
            if on == nil, let r = it.role { throw Self.roleRefusal(it, r, "刪掉") }
            let rep = it.repeat.flatMap(Repeat.init)
            if let on, let rep {
                try checkOn(it, on, rep)
                let line = describe(id, day: it.exceptions?[on.description]?.date.flatMap(Day.init) ?? on)
                it.exceptions = (it.exceptions ?? [:]).merging([on.description: CalendarFile.Exception(skip: true)]) { $1 }
                file.items[i] = it
                return line
            }
            if let on, on.description != it.date { throw CalendarError(description: "#\(id) 不重複，只在 \(Day(it.date)!.label)；刪它不用 on") }
            let line = describe(id)
            file.items.remove(at: i)
            return line
        }
    }

    private static func noItem(_ id: Int) -> CalendarError {
        CalendarError(description: "沒有 #\(id) 這個行事曆項目；用 calendar.list 看現有的編號")
    }

    /// `overnight` (the owner-sleep span): an end at or before the start is the next day's; equal is refused.
    private func checkTimes(_ start: TimeOfDay?, _ end: TimeOfDay?, _ tool: String, overnight: Bool = false) throws {
        if end != nil, start == nil { throw CalendarError(description: "\(tool) 有 end 就要有 start；全天的事兩個都不給") }
        if overnight, let start, let end, end == start { throw CalendarError(description: "\(tool) 的 end（\(end)）不能和 start 一樣") }
        if !overnight, let start, let end, end <= start { throw CalendarError(description: "\(tool) 的 end（\(end)）要晚於 start（\(start)）") }
    }

    /// `on` must be a day the series happens (not already removed).
    private func checkOn(_ it: CalendarFile.Item, _ on: Day, _ rep: Repeat) throws {
        let first = Day(it.date)!
        let inSeries = on >= first && rep.matches(on) && (it.until.flatMap(Day.init).map { on <= $0 } ?? true)
        guard inSeries, it.exceptions?[on.description]?.skip != true else {
            throw CalendarError(description: "#\(it.id)（\(rep.label)，從 \(first.label) 起）在 \(on.label) 沒有；on 要填它原本會發生的那天")
        }
    }

    /// Times after a change: `start` alone keeps the duration; `allDay` drops both. `overnight`:
    /// the owner-sleep span, whose end may fall on the next day.
    private func newTimes(_ s0: TimeOfDay?, _ e0: TimeOfDay?, _ s: TimeOfDay?, _ e: TimeOfDay?, _ allDay: Bool,
                          overnight: Bool = false) throws -> (TimeOfDay?, TimeOfDay?) {
        if allDay { return (nil, nil) }
        var start = s0, end = e0
        if let s {
            if e == nil, let s0, let e0 {
                let len = overnight ? (e0.minutes - s0.minutes + 24 * 60) % (24 * 60) : e0.minutes - s0.minutes
                let m = overnight ? (s.minutes + len) % (24 * 60) : s.minutes + len
                end = m < 24 * 60 ? TimeOfDay(String(format: "%02d:%02d", m / 60, m % 60)) : nil
            }
            start = s
        }
        if let e { end = e }
        try checkTimes(start, end, "calendar.change", overnight: overnight)
        return (start, end)
    }

    /// `#4 2026-11-18（星期三）10:00–11:00 看牙醫（…）`: the item as it now stands on `day` (its
    /// first day by default), or the item alone when that day has no occurrence of it.
    func describe(_ id: Int, day: Day? = nil) -> String {
        guard let it = item(id) else { return "#\(id)" }
        // A series: its first day that happens (a weekday series may start after `date`).
        let from = day ?? Day(it.date)!
        let to = day ?? from.adding(it.repeat == nil ? 0 : 7)
        if let o = occurrences(from: from, to: to).first(where: { $0.id == id }) {
            let first = it.repeat != nil && day == nil ? "從 " : ""
            let until = it.until.flatMap(Day.init).map { "，到 \($0.label) 為止" } ?? ""
            return "\(first)\(o.day.label) \(o.line)\(until)"
        }
        return "#\(id) \(it.title)（\(it.repeat.flatMap(Repeat.init)?.label ?? "")，從 \(it.date) 起）"
    }
}

extension Agenda {
    /// Item `id`'s first timed start after `t` (nil: all-day, or nothing ahead).
    func nextStart(of id: Int, after t: Double) -> Double? {
        let to = max(horizon, Day(ts: t).adding(8))
        return occurrences(from: Day(ts: t).adding(-1), to: to).filter { $0.id == id }.compactMap(\.startTS).filter { $0 > t }.min()
    }

    /// The item with `role` (`maintenance`), if any.
    func item(role: String) -> CalendarFile.Item? { file.items.first { $0.role == role } }

    /// Adds the daily `role` item at `time` (to `end`) from `day` (by the program; not saved here).
    func addRole(_ role: String, title: String, day: Day, time: TimeOfDay, end: TimeOfDay? = nil) {
        let id = file.nextID
        file.nextID += 1
        file.items.append(CalendarFile.Item(id: id, title: title, date: day.description, start: time.description, end: end?.description,
                                            repeat: Repeat.daily.description, until: nil, exceptions: nil, by: "program", role: role))
    }

    static func roleRefusal(_ it: CalendarFile.Item, _ role: String, _ what: String) -> CalendarError {
        let kind = role == ownerSleepRole ? "主人平常睡覺的時間" : "每天系統維護的時間"
        let how = role == ownerSleepRole ? "start、end" : "start"
        return CalendarError(description: "#\(it.id)「\(it.title)」是\(kind)，不能\(what)；要換時間用 calendar.change 改 \(how)，只改一天就給 on")
    }

    static let maintenanceRole = "maintenance"
    static let maintenanceTitle = "系統維護"
    static let ownerSleepRole = "owner_sleep"
    static let ownerSleepTitle = "主人睡覺"
    /// The sleep's 「睡覺」／「起床」 (2026-11-15), replaced by the maintenance item on 2026-11-16.
    static let removedRoles: Set<String> = ["sleep", "wake"]

    /// The owner-sleep occurrence `t` falls in (start ≤ t < end), if any: yesterday's span may run
    /// over midnight into today.
    func ownerSleep(at t: Double) -> Occurrence? {
        let today = Day(ts: t)
        return occurrences(from: today.adding(-1), to: today).first { o in
            o.role == Self.ownerSleepRole && (o.startTS.map { $0 <= t } ?? false) && (o.endTS.map { t < $0 } ?? false)
        }
    }

    /// Deletes the items with these roles (not saved here); returns them as `title HH:MM`.
    func removeRoles(_ roles: Set<String>) -> [String] {
        let gone = file.items.filter { $0.role.map(roles.contains) ?? false }
        file.items.removeAll { $0.role.map(roles.contains) ?? false }
        return gone.map { "\($0.title) \($0.start ?? "")" }
    }
}

// The doll's side ------------------------------------------------------------------------------

/// The dashboard's calendar block: the next days' occurrences and every item as stored.
public struct DashboardCalendar: Codable, Sendable {
    public struct Entry: Codable, Sendable {
        public var id: Int
        public var day: String
        public var label: String
        public var original: String
        public var title: String
        public var start: String?
        public var end: String?
        public var `repeat`: String?
        public var changed: Bool
        public var past: Bool
    }
    public struct Item: Codable, Sendable {
        public var id: Int
        public var title: String
        public var date: String
        public var start: String?
        public var end: String?
        public var `repeat`: String?
        public var until: String?
        public var by: String
        public var exceptions: Int
    }
    public var today: String
    public var days: Int
    public var entries: [Entry]
    public var items: [Item]
}

extension Doll {
    /// The dashboard shows this many days from today.
    static let dashboardCalendarDays = 14

    /// Tests move "now" (the calendar's timer and 【狀況】).
    public func setClock(_ c: any DollClock) {
        wallClock = c
        if calendarTask != nil { scheduleCalendar() }
    }

    /// Starts in (fired_through, now] that no process fired (dollcore was not running): marked dealt
    /// with, not fired; the boot notice lists them.
    func calendarCatchUp() throws -> [Occurrence] {
        let now = wallClock.now()
        // The owner-sleep span never calls her, so a start of it passing is nothing to list.
        let missed = (agenda.firedThrough.map { agenda.starts(after: $0, through: now) } ?? []).filter { $0.role != Agenda.ownerSleepRole }
        agenda.setFiredThrough(max(now, agenda.firedThrough ?? now))
        try agenda.save()
        calendarCaughtUp = true
        if !missed.isEmpty { log("INFO", "calendar: \(missed.count) start(s) passed while not running, listed in the boot notice") }
        // Its time passed while not running: maintenance is due.
        if maintenanceConfig.enabled, maintenanceState.due == nil, missed.contains(where: { $0.role == Agenda.maintenanceRole }) {
            maintenanceState.due = now
            try saveMaintenance()
            log("INFO", "maintenance: its time passed while not running; due")
        }
        try ensureMaintenanceItem()
        try ensureOwnerSleepItem()
        if drives.state.droppedRemoved {
            drives.state.droppedRemoved = false
            try saveSignals()
            log("INFO", "drives: \(signalsURL.path) held 睡意 (removed 2026-11-16); dropped and saved without it")
        }
        if drivesConfig.enabled, signalsFresh {
            signalsFresh = false
            drives = Drives(config: drivesConfig, now: now)
            try saveSignals()
        }
        return missed
    }

    /// The boot notice's calendar part: today's items, and the starts missed while not running.
    func calendarBootText() throws -> String {
        let missed = try calendarCatchUp()
        let now = wallClock.now()
        var s = "今天的行事曆：" + calendarStatus(now: now, all: true) + "。"
        if !missed.isEmpty {
            let n = calendarConfig.statusItems
            let lines = missed.prefix(n).map { "\($0.day.label) \($0.line)" }
            s += "沒開機的時候過了這些（沒有叫你）：" + lines.joined(separator: "；") + (missed.count > n ? "；還有 \(missed.count - n) 件" : "") + "。"
        }
        return s
    }

    /// Today's items for 【狀況】: all-day ones and timed ones not over yet (`all`: every one of
    /// today's, for the boot notice); at most `status_items`, then how many more.
    func calendarStatus(now: Double, all: Bool = false) -> String {
        let today = Day(ts: now)
        let occ = agenda.occurrences(from: today, to: today).filter { o in
            all || o.start == nil || (o.endTS ?? o.startTS!) > now
        }
        guard !occ.isEmpty else { return "沒有" }
        let n = calendarConfig.statusItems
        let lines = occ.prefix(n).map { o in
            o.line + (!all && (o.startTS.map { $0 <= now } ?? false) ? "（進行中）" : "")
        }
        return lines.joined(separator: "；") + (occ.count > n ? "；還有 \(occ.count - n) 件（calendar.list 看全部）" : "")
    }

    /// Replaces the timer: one task sleeping until the next timed start after `fired_through`.
    func scheduleCalendar() {
        calendarTask?.cancel()
        calendarTask = nil
        calendarSerial += 1
        guard calendarCaughtUp, !stopped, let through = agenda.firedThrough, let next = agenda.nextStart(after: through) else { return }
        let serial = calendarSerial, clock = wallClock
        calendarTask = Task { [weak self] in
            do { try await clock.sleep(until: next) } catch { return }
            await self?.calendarWake(serial)
        }
    }

    func calendarWake(_ serial: Int) {
        guard serial == calendarSerial else { return }
        calendarTask = nil
        do { try fireDue() } catch { log("ERROR", "calendar: firing failed: \(error)") }
        scheduleCalendar()
    }

    /// Queues one 【行事曆】 event per timed start in (fired_through, now]; the mark moves to now and
    /// is saved first, so a restart never fires one again.
    func fireDue() throws {
        let now = wallClock.now()
        let through = agenda.firedThrough ?? now
        let due = agenda.starts(after: through, through: now)
        agenda.setFiredThrough(max(now, through))
        try agenda.save()
        for o in due {
            if o.role == Agenda.ownerSleepRole {
                log("INFO", "calendar: #\(o.id) \(o.day) \(o.start!) \(o.title) began (no turn)")
                continue
            }
            log("INFO", "calendar: #\(o.id) \(o.day) \(o.start!) fired")
            if o.role == Agenda.maintenanceRole { maintenanceFire() } else { put(Event(.calendar, .internal, Self.fireText(o))) }
        }
    }

    /// The 「主人睡覺」 item when the calendar has none (`[owner_sleep]` start, end). Runs once the
    /// calendar is caught up, so an old calendar gets it at boot.
    func ensureOwnerSleepItem() throws {
        guard ownerSleepConfig.enabled, agenda.item(role: Agenda.ownerSleepRole) == nil else { return }
        agenda.addRole(Agenda.ownerSleepRole, title: Agenda.ownerSleepTitle, day: Day(ts: wallClock.now()),
                       time: TimeOfDay(ownerSleepConfig.start)!, end: TimeOfDay(ownerSleepConfig.end)!)
        try agenda.save()
        log("INFO", "calendar: had no \(Agenda.ownerSleepTitle); added \(ownerSleepConfig.start)–\(ownerSleepConfig.end) (daily)")
    }

    /// Inside the owner-sleep span: what she is told (【狀況】, a wandering, the maintenance notice);
    /// nil outside it or when it is off. The owner's own line inside the span is named with its time
    /// (he was evidently awake then); she judges whether he still is.
    func ownerSleepLine(now: Double) -> String? {
        guard ownerSleepConfig.enabled, let o = agenda.ownerSleep(at: now) else { return nil }
        let span = "\(o.start!)–\(o.end!)"
        if ownerLastLine >= o.startTS! {
            return "主人平常這時候在睡（\(span)），不過他 \(Self.hhmm(ownerLastLine)) 還跟你說過話"
        }
        return "主人現在應該在睡（\(span)）；不急的事等他醒了再說"
    }

    /// `【行事曆】現在 10:00：看牙醫（#4，到 11:00）`.
    static func fireText(_ o: Occurrence) -> String {
        let notes = ["#\(o.id)"] + [o.end.map { "到 \($0)" }, o.repeating?.label].compactMap { $0 }
        return "【行事曆】現在 \(o.start!)：\(o.title)（\(notes.joined(separator: "，"))）"
    }

    /// One edit, hers or the owner's: what is due fires first (the mark then is now, so a start
    /// already past never fires), the file is saved, the timer replaced. Returns the item's line.
    func calendarEdit(_ r: CalendarRequest, by who: String) throws -> String {
        if calendarCaughtUp { try fireDue() }
        let line = try agenda.apply(r, by: who)
        try agenda.save()
        if let id = r.id, agenda.item(id)?.role == Agenda.maintenanceRole { maintenanceItemChanged() }
        scheduleCalendar()
        feed.notify(.calendar)
        log("INFO", "calendar: \(who) \(r.verb) \(line)")
        return line
    }

    /// Her `calendar.*` call: the result she reads.
    func calendarTool(_ r: CalendarRequest) throws -> String {
        let now = wallClock.now()
        switch r {
        case .list(let from, let to):
            return try calendarList(from, to ?? from)
        case .add, .change(_, .none, _, _, _, _, _):
            let line = try calendarEdit(r, by: "doll")
            return (r.isAdd ? "加好了：" : "改好了：") + line + "。" + wakeNote(r.isAdd ? agenda.file.nextID - 1 : r.id!, now: now)
        case .change(let id, let on?, _, _, _, _, _):
            let line = try calendarEdit(r, by: "doll")
            return "改好了：\(line)。" + (agenda.item(id)?.repeat != nil ? "只改 \(on.label) 這一次，其他天照舊。" : "") + wakeNote(id, now: now)
        case .delete(_, let on):
            let line = try calendarEdit(r, by: "doll")
            if let on, agenda.item(r.id!) != nil { return "\(on.label) 這一次取消了，其他天照舊：\(line)。" }
            return "刪掉了：\(line)。"
        }
    }

    private func wakeNote(_ id: Int, now: Double) -> String {
        guard let it = agenda.item(id) else { return "" }
        if it.start == nil, !(it.exceptions ?? [:]).values.contains(where: { $0.start != nil }) {
            return "全天的事不會叫你，那天的【狀況】會列出來。"
        }
        if it.role == Agenda.ownerSleepRole {
            return "開始和結束都不會叫你；在這段時間裡，【狀況】會寫主人應該在睡。"
        }
        guard let next = agenda.nextStart(of: id, after: now) else { return "時間已經過了，不會叫你。" }
        let c = Day.cal.dateComponents([.hour, .minute], from: Date(timeIntervalSince1970: next))
        let then = it.role == Agenda.maintenanceRole ? "到時候會先告訴你、給你一個回合，再過幾分鐘系統維護就開始" : "到時候會給你一個回合"
        return String(format: "下一次是 %@ %02d:%02d，%@。", Day(ts: next).label, c.hour!, c.minute!, then)
    }

    func calendarList(_ from: Day, _ to: Day) throws -> String {
        guard from <= to else { throw CalendarError(description: "calendar.list 的 to（\(to.label)）早於 from（\(from.label)）") }
        let span = Day.cal.dateComponents([.day], from: from.midnight, to: to.midnight).day! + 1
        guard span <= calendarConfig.listMaxDays else {
            throw CalendarError(description: "calendar.list 一次最多 \(calendarConfig.listMaxDays) 天，收到 \(span) 天；分段查")
        }
        let head = from == to ? from.label : "\(from.label) 到 \(to.label)"
        let occ = agenda.occurrences(from: from, to: to)
        guard !occ.isEmpty else { return "\(head)：沒有任何事。" }
        var out = ["\(head)："], last: Day?
        for o in occ {
            if o.day != last { out.append(o.day.label); last = o.day }
            out.append("- " + o.line)
        }
        return out.joined(separator: "\n")
    }

    /// Holds a no-turn program notice while a turn runs; appends it at once otherwise.
    func notice(_ ev: Event) throws {
        var ev = ev
        ev.ts = wallClock.now()
        if inTurn { held.append(ev) } else { try append(ev, keepTime: true) }
    }

    func flushHeld() throws {
        let evs = held
        held = []
        for ev in evs { try append(ev, keepTime: true) }
    }

    // dashboard ------------------------------------------------------------------------------

    public func dashboardCalendar() -> DashboardCalendar {
        let now = wallClock.now()
        let today = Day(ts: now)
        let last = today.adding(Self.dashboardCalendarDays - 1)
        let entries = agenda.occurrences(from: today, to: last).map { o in
            DashboardCalendar.Entry(id: o.id, day: o.day.description, label: o.day.label, original: o.original.description, title: o.title,
                                    start: o.start?.description, end: o.end?.description, repeat: o.repeating?.description,
                                    changed: o.changed, past: (o.endTS ?? o.startTS ?? o.day.adding(1).midnight.timeIntervalSince1970) <= now)
        }
        let items = agenda.file.items.map { it in
            DashboardCalendar.Item(id: it.id, title: it.title, date: it.date, start: it.start, end: it.end, repeat: it.repeat,
                                   until: it.until, by: it.by, exceptions: it.exceptions?.count ?? 0)
        }
        return DashboardCalendar(today: today.description, days: Self.dashboardCalendarDays, entries: entries, items: items)
    }

    /// The owner's edit from the dashboard (`add`, `change`, `delete`, parameters as her tool's): a
    /// program event in her timeline, no turn of its own (she reads it with the next event).
    public func dashboardCalendarEdit(_ op: String, _ params: [String: String]) throws -> String {
        let r = try CalendarRequest.parse("calendar." + op, params)
        if case .list = r { throw CalendarError(description: "list is not an edit") }
        let line = try calendarEdit(r, by: "owner")
        let what: String = switch r {
        case .add: "加了"
        case .change(_, let on?, _, _, _, _, _): "改了 \(on.label) 這一次"
        case .change: "改了"
        case .delete(_, let on?): "取消了 \(on.label) 這一次"
        case .delete: "刪掉了"
        case .list: ""
        }
        try notice(Event(.memory, .internal, "【行事曆】主人在 dashboard \(what)：\(line)"))
        return line
    }
}

extension CalendarRequest {
    var isAdd: Bool { if case .add = self { true } else { false } }
    var id: Int? {
        switch self {
        case .change(let id, _, _, _, _, _, _), .delete(let id, _): id
        case .add, .list: nil
        }
    }
    var verb: String {
        switch self {
        case .add: "added"
        case .change: "changed"
        case .delete: "deleted"
        case .list: "listed"
        }
    }
}
