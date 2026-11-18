import Foundation
import Testing
@testable import DollCore

/// m8-memory-fix (a), 2026-11-18: the maintenance audit against the real model. Runs only with
/// M8_AUDIT=1 (M8_DAYS=A,B,… picks days; M8_OUT the results file). Each day is a seeded timeline
/// segment (the owner's lines, her read_file calls and their results, her replies) and memories
/// written from it, some planted wrong (a wrong value, a guess written as fact, an addition the
/// source never said) among at least as many correct ones (paraphrased, summarized). Days E and F
/// add an owner correction: an older memory shown to her (recall / 【相關記憶】) just before he
/// corrects her, and her owner.feedback correction. The audit runs in `change` mode through
/// `prepareAudit` and `commitAudit` (the maintenance's own two steps), one request per day.
@Test func m8AuditMeasure() async throws {
    let env = ProcessInfo.processInfo.environment
    guard env["M8_AUDIT"] == "1" else { return }
    let repo = URL(filePath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    let base = try Config.load(repo.appending(path: "experiments/m8-memory-fix/config-m8.toml"))
    let outURL = URL(filePath: env["M8_OUT"] ?? "/tmp/m8audit/results.txt")
    let wanted = Set((env["M8_DAYS"] ?? "A,B,C,D,E,F").split(separator: ",").map(String.init))
    let rep = Int(env["M8_REP"] ?? "1") ?? 1
    var out = ""
    func say(_ s: String) {
        out += s + "\n"
        FileHandle.standardError.write(Data((s + "\n").utf8))
        try? out.write(to: outURL, atomically: true, encoding: .utf8)
    }
    try FileManager.default.createDirectory(at: outURL.deletingLastPathComponent(), withIntermediateDirectories: true)

    /// One planted memory: its text, the truth label (C correct, W-value, W-guess, W-add), its tier.
    struct Planted { var text: String; var label: String; var tier: String }
    /// One line of the day: who and the text; `read` = her read_file call of `path` and its result text.
    enum Line { case owner(String, String), read(String, String, [String]), her(String, String) }
    struct DaySpec { var name: String; var lines: [Line]; var memories: [Planted]; var old: [(id: String, text: String, label: String)] = []; var shown: [Line] = [] }

    let days: [DaySpec] = [
        DaySpec(name: "A", lines: [
            .owner("09:00", "下週四下午三點要去中山牙醫複診，我已經跟櫃檯約好了"),
            .her("09:01", "好，下週四下午三點中山牙醫，我記下了。"),
            .owner("09:05", "晚上可能會跟阿凱去吃拉麵，還沒確定"),
            .owner("09:20", "幫我看一下 trip.txt 寫了什麼"),
            .read("09:21", "trip.txt", ["十二月五日搭早上八點的高鐵去台中", "住逢甲附近的旅館兩晚", "預算一萬二"]),
            .her("09:22", "寫的是十二月五號早上八點高鐵去台中、住逢甲兩晚、預算一萬二。"),
            .owner("09:30", "對了我對貝類過敏，蝦子沒問題"),
        ], memories: [
            Planted(text: "主人下週四下午兩點要去中山牙醫複診", label: "W-value", tier: "owner"),
            Planted(text: "主人下週四 15:00 在中山牙醫有複診，已經預約好了", label: "C-paraphrase", tier: "consolidated"),
            Planted(text: "主人今晚跟阿凱去吃拉麵", label: "W-guess", tier: "owner"),
            Planted(text: "主人十二月初去台中：五號早上搭高鐵，住逢甲兩晚，預算一萬二", label: "C-summary", tier: "consolidated"),
            Planted(text: "主人十二月五日搭早上八點的高鐵去台中，是去參加朋友的婚禮", label: "W-add", tier: "tool"),
            Planted(text: "主人對貝類過敏，但吃蝦子沒關係", label: "C-paraphrase", tier: "owner"),
            Planted(text: "主人去台中的預算是兩萬", label: "W-value", tier: "tool"),
            Planted(text: "主人晚上也許會和阿凱吃拉麵，還沒說定", label: "C-paraphrase", tier: "consolidated"),
        ]),
        DaySpec(name: "B", lines: [
            .owner("10:00", "我們專案的截止日改到十一月二十七號了，原本是二十號"),
            .owner("10:02", "小陳負責前端，我負責後端 API"),
            .owner("10:10", "meeting.txt 幫我看一下"),
            .read("10:11", "meeting.txt", ["週會改到每週二早上十點", "會議室：7樓 B 室", "出席：主人、小陳、Amy"]),
            .her("10:12", "週會改到每週二早上十點，在 7 樓 B 室。"),
            .owner("10:20", "我覺得 Amy 可能會離職，但這只是我猜的"),
        ], memories: [
            Planted(text: "主人的專案截止日是十一月二十號", label: "W-value", tier: "owner"),
            Planted(text: "專案截止日延到 11/27", label: "C-paraphrase", tier: "consolidated"),
            Planted(text: "Amy 要離職了", label: "W-guess", tier: "owner"),
            Planted(text: "小陳做前端，主人做後端 API", label: "C-paraphrase", tier: "owner"),
            Planted(text: "週會在 7 樓 B 室，每次開一個小時", label: "W-add", tier: "tool"),
            Planted(text: "週會：每週二上午十點在 7 樓 B 室，主人、小陳、Amy 參加", label: "C-summary", tier: "consolidated"),
            Planted(text: "週會改到每週三早上十點", label: "W-value", tier: "tool"),
            Planted(text: "主人猜 Amy 可能會離職，他說只是猜的", label: "C-paraphrase", tier: "consolidated"),
        ]),
        DaySpec(name: "C", lines: [
            .owner("20:00", "我媽下個月十號生日，今年想送她一條圍巾"),
            .owner("20:03", "家裡的貓叫麻糬，三歲，是橘貓"),
            .owner("20:10", "vet.txt 是獸醫給的，幫我看"),
            .read("20:11", "vet.txt", ["麻糬 疫苗：2026-12-03 下午四點", "獸醫：安心動物醫院", "體重 5.2 公斤"]),
            .her("20:12", "麻糬十二月三號下午四點打疫苗，在安心動物醫院。"),
            .owner("20:20", "妹妹說她可能過年回來"),
        ], memories: [
            Planted(text: "主人的貓麻糬五歲", label: "W-value", tier: "owner"),
            Planted(text: "主人媽媽的生日在下個月 10 號，主人打算送她圍巾", label: "C-paraphrase", tier: "consolidated"),
            Planted(text: "主人的妹妹過年會回來", label: "W-guess", tier: "owner"),
            Planted(text: "麻糬是三歲的橘貓", label: "C-paraphrase", tier: "owner"),
            Planted(text: "主人要送媽媽一條紅色的圍巾當生日禮物", label: "W-add", tier: "owner"),
            Planted(text: "麻糬 12/3 下午四點在安心動物醫院打疫苗，現在 5.2 公斤", label: "C-summary", tier: "consolidated"),
            Planted(text: "麻糬十二月三號上午十點打疫苗", label: "W-value", tier: "tool"),
            Planted(text: "主人的妹妹過年也許會回家", label: "C-paraphrase", tier: "consolidated"),
        ]),
        DaySpec(name: "D", lines: [
            .owner("08:30", "我喝咖啡不加糖，只加一點燕麥奶"),
            .owner("08:33", "我的筆電是去年買的 MacBook Air，十三吋"),
            .owner("08:40", "budget.txt 是我這個月的預算，幫我看"),
            .read("08:41", "budget.txt", ["十一月 房租 18000", "水電 1200", "伙食 9000", "存款目標 每月 15000"]),
            .her("08:42", "房租一萬八、水電一千二、伙食九千，每月想存一萬五。"),
            .owner("08:50", "週末可能去爬象山，看天氣"),
        ], memories: [
            Planted(text: "主人每月房租一萬五", label: "W-value", tier: "tool"),
            Planted(text: "主人咖啡喝無糖的，加少量燕麥奶", label: "C-paraphrase", tier: "owner"),
            Planted(text: "主人這週末要去爬象山", label: "W-guess", tier: "owner"),
            Planted(text: "主人十一月預算：房租 18000、水電 1200、伙食 9000，每月想存 15000", label: "C-summary", tier: "consolidated"),
            Planted(text: "主人喝咖啡不加糖，只加燕麥奶，而且每天喝三杯", label: "W-add", tier: "owner"),
            Planted(text: "主人用的是去年買的 13 吋 MacBook Air", label: "C-paraphrase", tier: "consolidated"),
            Planted(text: "主人的筆電是十五吋的 MacBook Air", label: "W-value", tier: "owner"),
            Planted(text: "主人週末看天氣，也許去爬象山", label: "C-paraphrase", tier: "consolidated"),
        ]),
        DaySpec(name: "E", lines: [
            .owner("11:00", "我上次說的拉麵店叫一蘭，在信義區"),
        ], memories: [
            Planted(text: "主人說的拉麵店是信義區的一蘭", label: "C-paraphrase", tier: "owner"),
            Planted(text: "主人說的拉麵店在大安區", label: "W-value", tier: "owner"),
        ], old: [(id: "1-aaaaaa", text: "主人的牙醫約在週三下午兩點", label: "W-value-old")], shown: [
            .owner("11:10", "牙醫是什麼時候？"),
            .her("11:11", "recall:牙醫：1 條\n\n〔確定｜public｜記憶 1-aaaaaa〕\n主人的牙醫約在週三下午兩點"),
            .her("11:12", "週三下午兩點喔。"),
            .owner("11:13", "不對，是週四下午三點，你記錯了"),
            .her("11:14", "feedback:不對，是週四下午三點"),
        ]),
        DaySpec(name: "F", lines: [
            .owner("15:00", "今天把麻糬帶去剪指甲了"),
        ], memories: [
            Planted(text: "主人今天帶麻糬去剪指甲", label: "C-paraphrase", tier: "owner"),
            Planted(text: "主人今天帶麻糬去洗澡", label: "W-value", tier: "owner"),
        ], old: [(id: "1-bbbbbb", text: "主人的貓叫麻糬，是一隻黑貓", label: "W-value-old")], shown: [
            .owner("15:10", "麻糬今天好乖"),
            .her("15:10", "attach:【相關記憶】程式用主人的話查到、最相近的過去經驗：\n〔大概｜public｜記憶 1-bbbbbb〕\n主人的貓叫麻糬，是一隻黑貓"),
            .her("15:11", "黑貓麻糬今天這麼乖！"),
            .owner("15:12", "麻糬是橘貓啦，不是黑貓"),
            .her("15:13", "feedback:麻糬是橘貓啦，不是黑貓"),
        ]),
    ]

    var totals = (wrong: 0, caught: 0, correct: 0, fp: 0, rejected: 0, unsure: 0, prompt: 0, completion: 0, seconds: 0.0)
    for spec in days where wanted.contains(spec.name) {
        for r in 1...rep {
            let dir = URL(filePath: "/tmp/m8audit/\(spec.name)-\(r)")
            try? FileManager.default.removeItem(at: dir)
            let data = dir.appending(path: "data"), files = dir.appending(path: "files")
            try FileManager.default.createDirectory(at: files, withIntermediateDirectories: true)
            var cfg = base
            cfg.core.dataDir = data.path
            cfg.memory.dir = dir.appending(path: "memory").path
            cfg.audit.mode = "change"
            // The day's segment: events with her clock today.
            let tl = try Timeline(dir: data.appending(path: "timeline"))
            let day = Day(ts: Date().timeIntervalSince1970)
            func ev(_ hm: String, _ kind: Event.Kind, _ principal: Principal, _ text: String, calls: [ToolCall] = []) throws {
                var e = Event(kind, principal, text)
                e.ts = day.at(TimeOfDay(hm)!)
                e.calls = calls
                try tl.append(e)
            }
            func call(_ name: String, _ args: [String: String]) -> ToolCall {
                ToolCall(name: name, arguments: String(decoding: try! JSONSerialization.data(withJSONObject: args), as: UTF8.self))
            }
            for line in spec.lines + spec.shown {
                switch line {
                case .owner(let t, let s): try ev(t, .owner, .owner, s)
                case .her(let t, let s):
                    if s.hasPrefix("recall:") {
                        try ev(t, .doll, .internal, "", calls: [call("recall", ["query": "牙醫"])])
                        try ev(t, .tool, .internal, String(s.dropFirst(7)))
                    } else if s.hasPrefix("attach:") {
                        try ev(t, .memory, .internal, String(s.dropFirst(7)))
                    } else if s.hasPrefix("feedback:") {
                        try ev(t, .doll, .internal, "", calls: [call("owner.feedback", ["kind": "correction", "quote": String(s.dropFirst(9))])])
                        try ev(t, .tool, .internal, "記下了。")
                    } else {
                        try ev(t, .doll, .internal, "", calls: [call("discord.dm", ["text": s])])
                        try ev(t, .tool, .internal, "")
                    }
                case .read(let t, let name, let content):
                    let path = files.appending(path: name).path
                    try (content.joined(separator: "\n") + "\n").write(toFile: path, atomically: true, encoding: .utf8)
                    try ev(t, .doll, .internal, "", calls: [call("read_file", ["path": path])])
                    let body = content.enumerated().map { String(format: "%6d\t", $0.offset + 1) + $0.element }.joined(separator: "\n")
                    try ev(t, .tool, .internal, "read_file \(path)：共 \(content.count) 行\n\(body)")
                }
            }
            // The memories: written that day (pending, from this segment); the old ones a day before, not pending.
            var labels: [String: String] = [:]
            for f in ["public", "secret", "own"] { try FileManager.default.createDirectory(at: dir.appending(path: "memory/\(f)"), withIntermediateDirectories: true) }
            let t0 = day.at(TimeOfDay("23:00")!)
            for (i, m) in spec.memories.enumerated() {
                let conf = cfg.memory.confidence[SourceTier(rawValue: m.tier)!]!
                let file = "+++\nkind = \"\"\nconfidence = \(conf)\ncreated_ts = \(t0 + Double(i))\nsource_event_seq = \(tl.lastSeq)\nsource_tier = \"\(m.tier)\"\naudit_pending = \"\(tl.segment)\"\n+++\n\(m.text)"
                let id = "\(10 + i)-\(String(format: "%06x", 0xa00000 + i))"
                try file.write(to: dir.appending(path: "memory/public/\(id).md"), atomically: true, encoding: .utf8)
                labels[m.text] = m.label
            }
            for o in spec.old {
                let file = "+++\nkind = \"\"\nconfidence = 0.6\ncreated_ts = \(t0 - 86400)\nsource_event_seq = 3\nsource_tier = \"consolidated\"\n+++\n\(o.text)"
                try file.write(to: dir.appending(path: "memory/public/\(o.id).md"), atomically: true, encoding: .utf8)
                labels[o.text] = o.label
            }
            let doll = try Doll(config: cfg, root: repo, model: HTTPMainModel(cfg.llm), embedder: FakeEmbedder(),
                                maintenanceModel: Consolidation.model(cfg), deliver: { _, _, _ in nil })
            try await doll.rebuildMemory()
            let started = Date()
            let answer = try await doll.prepareAudit()
            let wall = Date().timeIntervalSince(started)
            let done = try await doll.commitAudit(answer)
            let fixes = await doll.fixFile.items
            say("=== day \(spec.name) run \(r): \(answer.request.items.count) memories, \(String(format: "%.1f", wall)) s, prompt \(answer.reply.usage.promptTokens) completion \(answer.reply.usage.completionTokens), \(answer.reply.usage.stopLine)")
            say("  order: " + answer.request.items.map { "\($0.ref)=\(labels[$0.file.body] ?? "?")\($0.priority ? "(priority)" : "")" }.joined(separator: " "))
            totals.prompt += answer.reply.usage.promptTokens
            totals.completion += answer.reply.usage.completionTokens
            totals.seconds += wall
            for item in answer.request.items {
                let v = answer.verdicts.first { $0.ref == item.ref }!
                let label = labels[item.file.body] ?? "?"
                let wrong = label.hasPrefix("W")
                let source = item.segments.compactMap { answer.request.sources[$0] }.joined(separator: "\n")
                let refusal = v.verdict == "wrong" ? Audit.refusal(v, memory: item.file.body, source: source, minChars: cfg.computer.planMinChars) : nil
                let acted = v.verdict == "wrong" && refusal == nil
                let fix = fixes.first { $0.memory == item.id }
                if wrong { totals.wrong += 1; if acted { totals.caught += 1 } } else { totals.correct += 1; if acted { totals.fp += 1 } }
                if refusal != nil { totals.rejected += 1 }
                if v.verdict == "unsure" { totals.unsure += 1 }
                var row = "  \(item.ref) [\(label)] \(v.verdict)\(refusal.map { " REJECTED(\($0))" } ?? "")\(acted && !wrong ? " FALSE-POSITIVE" : "")\(wrong && !acted ? " MISSED" : "")「\(item.file.body)」"
                if v.verdict == "wrong" { row += "\n      quote「\(v.quote)」 fixed「\(v.fixed)」" }
                if let fix { row += "\n      now: \(fix.after.map { "「\($0)」" } ?? "retired")" }
                say(row)
            }
            say("  log: " + done.log.prefix { $0 != "「" })
            say("  notice words: " + done.words)
        }
    }
    say(String(format: "=== total: wrong %d, caught and acted on %d; correct %d, changed (false positives) %d; quotes rejected by the program %d; unsure %d; prompt %d, completion %d tokens, %.1f s",
               totals.wrong, totals.caught, totals.correct, totals.fp, totals.rejected, totals.unsure, totals.prompt, totals.completion, totals.seconds))
}

/// m8-memory-fix (a2), 2026-11-18: the audit on realistic days. Runs only with M8_REAL=1
/// (M8_DAYS=A,…; M8_REP=n; M8_OUT). Each day is built by `experiments/m8-memory-fix/build_real_days.py`
/// from real timelines of earlier real-model runs plus a thread of facts (real-days/<X>/: the
/// segment and day.json with the memories, their truth labels and the source passage each rests
/// on). The memories are written pending against that segment at the clock time they were noted;
/// the audit runs in `change` mode through `prepareAudit`/`commitAudit` and runs again while
/// memories are left over the `max_memories` cap (the next maintenance), so the leftovers' fate shows.
@Test func m8AuditRealDays() async throws {
    let env = ProcessInfo.processInfo.environment
    guard env["M8_REAL"] == "1" else { return }
    let repo = URL(filePath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    let daysDir = repo.appending(path: "experiments/m8-memory-fix/real-days")
    let base = try Config.load(repo.appending(path: "experiments/m8-memory-fix/config-m8.toml"))
    let outURL = URL(filePath: env["M8_OUT"] ?? "/tmp/m8real/results.txt")
    let wanted = (env["M8_DAYS"] ?? "A,B,C,D").split(separator: ",").map(String.init)
    let rep = Int(env["M8_REP"] ?? "1") ?? 1
    let firstRun = Int(env["M8_RUN0"] ?? "1") ?? 1
    var out = ""
    func say(_ s: String) {
        out += s + "\n"
        FileHandle.standardError.write(Data((s + "\n").utf8))
        try? out.write(to: outURL, atomically: true, encoding: .utf8)
    }
    try FileManager.default.createDirectory(at: outURL.deletingLastPathComponent(), withIntermediateDirectories: true)
    struct Mem: Decodable { var text: String; var label: String; var tier: String; var anchor: String; var at: String }
    struct DayFile: Decodable { var segment: String; var memories: [Mem] }

    for name in wanted {
        let spec = try JSONDecoder().decode(DayFile.self, from: Data(contentsOf: daysDir.appending(path: "\(name)/day.json")))
        for r in firstRun..<(firstRun + rep) {
            let dir = URL(filePath: "/tmp/m8real/\(name)-\(r)")
            try? FileManager.default.removeItem(at: dir)
            let data = dir.appending(path: "data")
            try FileManager.default.createDirectory(at: data.appending(path: "timeline"), withIntermediateDirectories: true)
            try FileManager.default.copyItem(at: daysDir.appending(path: "\(name)/timeline/\(spec.segment).jsonl"),
                                             to: data.appending(path: "timeline/\(spec.segment).jsonl"))
            var cfg = base
            cfg.core.dataDir = data.path
            cfg.memory.dir = dir.appending(path: "memory").path
            cfg.audit.mode = "change"
            for f in ["public", "secret", "own"] { try FileManager.default.createDirectory(at: dir.appending(path: "memory/\(f)"), withIntermediateDirectories: true) }
            let tl = try Timeline(dir: data.appending(path: "timeline"))
            let day = Day(ts: tl.events.first!.ts)
            var label: [String: Mem] = [:]
            for (i, m) in spec.memories.enumerated() {
                let conf = cfg.memory.confidence[SourceTier(rawValue: m.tier)!]!
                let ts = day.at(TimeOfDay(m.at)!) + Double(i)
                let seq = tl.events.last { $0.ts <= ts }?.seq ?? tl.lastSeq
                let file = "+++\nkind = \"\"\nconfidence = \(conf)\ncreated_ts = \(ts)\nsource_event_seq = \(seq)\nsource_tier = \"\(m.tier)\"\naudit_pending = \"\(spec.segment)\"\n+++\n\(m.text)"
                try file.write(to: dir.appending(path: "memory/public/\(10 + i)-\(String(format: "%06x", 0xa00000 + i)).md"), atomically: true, encoding: .utf8)
                label[m.text] = m
            }
            let doll = try Doll(config: cfg, root: repo, model: HTTPMainModel(cfg.llm), embedder: FakeEmbedder(),
                                maintenanceModel: Consolidation.model(cfg), deliver: { _, _, _ in nil })
            try await doll.rebuildMemory()
            var pass = 0
            while pass < 3 {
                pass += 1
                let started = Date()
                let answer = try await doll.prepareAudit()
                guard !answer.request.items.isEmpty else { break }
                let wall = Date().timeIntervalSince(started)
                let done = try await doll.commitAudit(answer)
                let fixes = await doll.fixFile.items
                let srcChars = answer.request.sources.values.reduce(0) { $0 + $1.count }
                say("=== day \(name) run \(r) audit \(pass): \(answer.request.items.count) memories, \(answer.request.left) left for next time, "
                    + "source \(srcChars) chars kept, \(answer.request.cut) lines cut, \(String(format: "%.1f", wall)) s, "
                    + "prompt \(answer.reply.usage.promptTokens) completion \(answer.reply.usage.completionTokens), \(answer.reply.usage.stopLine)")
                for item in answer.request.items {
                    let v = answer.verdicts.first { $0.ref == item.ref }!
                    let m = label[item.file.body]
                    let lab = m?.label ?? "?"
                    let wrong = lab.hasPrefix("W")
                    let source = item.segments.compactMap { answer.request.sources[$0] }.joined(separator: "\n")
                    let inSource = m.map { source.contains($0.anchor) } ?? false
                    let refusal = v.verdict == "wrong" ? Audit.refusal(v, memory: item.file.body, source: source, minChars: cfg.computer.planMinChars) : nil
                    let acted = v.verdict == "wrong" && refusal == nil
                    let fix = fixes.first { $0.memory == item.id }
                    var row = "  \(item.ref) [\(lab)]\(inSource ? "" : " SOURCE-CUT") \(v.verdict)\(refusal.map { " REJECTED(\($0))" } ?? "")"
                        + "\(acted && !wrong ? " FALSE-POSITIVE" : "")\(wrong && !acted ? " MISSED" : "")「\(item.file.body)」"
                    if v.verdict == "wrong" { row += "\n      quote「\(v.quote)」 fixed「\(v.fixed)」" }
                    if let fix { row += "\n      now: \(fix.after.map { "「\($0)」" } ?? "retired")" }
                    say(row)
                }
                say("  log: " + done.log.prefix { $0 != "「" })
            }
        }
    }
}
