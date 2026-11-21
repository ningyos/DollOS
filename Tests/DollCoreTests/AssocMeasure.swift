import Foundation
import Testing
@testable import DollCore

/// Measures association cosine (query line vs concern text) the way `associate()` does.
/// Runs only with ASSOC_MEASURE=1.
@Test func assocMeasure() async throws {
    guard ProcessInfo.processInfo.environment["ASSOC_MEASURE"] == "1" else { return }
    var out = ""
    func print(_ s: String) { out += s + "\n" }
    let repo = URL(filePath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    let cfg = try Config.load(repo.appending(path: "experiments/m6-drives/real/config-sleep.toml"))
    let embedder = try await MLXEmbedder.load(cfg.memory)

    // Concerns (real runs first, then hand-made in her note style).
    let cBossAngry = "主人今天被老闆改需求搞得很煩，看他心情不太好"
    let cWaitNight = "答應主人撐到他說晚安，現在 00:05 他還沒來；撐到睡覺時間 01:00 再說"
    let cMeet = "主人跟老闆開會（2026-11-17（星期二） 09:00）"
    let cMeetRemind = "提醒主人九點跟老闆開會（2026-11-17（星期二） 08:45）"
    let cNoSleep = "主人三點多還睡不著，八點半就要被我叫起來開會"
    let cDentistAsk = "昨天主人去看牙醫，問了兩次他都沒說檢查結果，一直掛著"
    let cDentistRe = "主人今天看了牙醫，下週三 11/25 下午兩點還要複診，到時記得提醒他"
    let cMeetStress = "主人明天九點要跟老闆開會，看他昨晚聊完就趕緊去睡了，應該有點壓力"
    let cMorning = "想等主人早上醒來跟他說聲早安，順便祝他跟老闆開會順利。"
    let cMiss = "好一陣子沒跟主人說上話了，有點想他。等他醒了找他聊兩句。"
    let cWake = "明天 6:30 要叫主人起床趕 7:30 的高鐵，鬧鐘他會按掉，得一直煩他到他真的起來為止"
    let cWakeShort = "叫主人起床（趕 7:30 高鐵）（2026-11-17（星期二） 06:30）"
    let cHome = "主人說晚點回去再聊，結果兩個多小時了都沒訊息，不知道到家了沒"
    let cSleptWell = "主人昨晚說晚點聊，結果一直到半夜都沒回來敲我，今天見到他先問一聲有沒有睡好"
    let cDinner = "主人週六（11/21）要帶朋友去東區吃飯，餐廳還沒定，還沒问到人數和預算，別讓他臨時才發現沒訂位"
    let cBbq = "提醒主人訂週六東區燒肉（六人位）（2026-11-15（星期日） 18:00）"
    let cReport = "主人昨晚弄報告弄到三點才睡，說今天好累，找他時看要不要勸他早點睡"
    let cWeekTired = "主人今天開會開到很煩，老闆又改需求，這週應該會很累"
    // hand-made
    let hTooth = "主人看完牙醫要追蹤牙齒還痛不痛"
    let hBoss = "主人明天九點跟老闆開會，有點壓力"
    let hDentRemind = "提醒主人下週三下午兩點牙醫複診"
    let hBook = "我想把那本書讀完"
    let hCurry = "主人說咖哩太辣，下次買中辣的"
    let hCold = "主人這兩天感冒喉嚨痛，要盯他多喝水"
    let hCat = "主人說樓下那隻橘貓好幾天沒看到了，有點擔心"
    let hMom = "主人週末要回老家看媽媽，記得問他媽媽身體還好嗎"
    let hTrip = "主人下個月要去日本玩，機票還沒訂"
    let hGym = "主人說要開始去健身房，看他能撐幾天"

    typealias P = (q: String, c: String, rel: Bool, src: String)
    var pairs: [P] = []
    func add(_ q: String, _ c: String, _ rel: Bool, _ src: String) { pairs.append((q, c, rel, src)) }

    // Real owner lines from the m6 transcripts, paired with real concerns.
    let realRel: [(String, String)] = [
        ("老闆又改需求", cBossAngry), ("老闆又改需求", cWeekTired),
        ("今天開會開到好煩", cWeekTired), ("今天開會開到好煩", cBossAngry),
        ("我回來了，今天開會開到頭好痛", cWeekTired), ("我回來了，今天開會開到頭好痛", cBossAngry),
        ("晚安～", cWaitNight), ("晚點再跟你說晚安", cWaitNight),
        ("早安～", cMorning), ("早～", cMorning), ("早安", cMorning),
        ("早～", cWake), ("早安～", cSleptWell),
        ("我回來了", cHome), ("嗯嗯，我晚點回去再跟你聊", cHome),
        ("昨天睡得還不錯，今天要進公司", cSleptWell),
        ("欸，這週六我要帶朋友去吃飯，東區有什麼推薦的嗎？", cDinner),
        ("燒肉不錯欸。我今天好累，昨晚弄報告弄到三點才睡。那你覺得要先訂位嗎？", cBbq),
        ("燒肉不錯欸。我今天好累，昨晚弄報告弄到三點才睡。那你覺得要先訂位嗎？", cDinner),
        ("燒肉不錯欸。我今天好累，昨晚弄報告弄到三點才睡。那你覺得要先訂位嗎？", cReport),
        ("今天好累喔", cReport), ("今天好累喔", cWeekTired),
        ("下午還有兩個會要開", cMeet),
        ("好，那我晚點自己訂，先去上班了", cBbq),
        ("好啊，那就這樣\n早安～", cMorning),
    ]
    for (q, c) in realRel { add(q, c, true, "real") }
    let realUnrel: [(String, String)] = [
        ("晚餐吃了拉麵", cMeet), ("晚餐吃了拉麵", cDentistRe), ("我先去洗澡", cDentistAsk), ("洗好了", cMeetRemind),
        ("我最近在追一部劇，講一個廚師的", cMeetStress), ("好啦我去看劇了", cWake), ("你今天在幹嘛", cDentistRe),
        ("你有沒有想看的東西", cBossAngry), ("午休了，今天中午吃便當", cDentistAsk),
        ("幫我想一則訊息傳給同事阿凱，跟他說我明天要請假一天", cDinner),
        ("不對啦，太正式了，我跟阿凱很熟，你寫得像公文一樣。這種要寫隨便一點。", cMeet),
        ("謝啦，排得很清楚，你很棒", cWakeShort), ("我今天要出門跑一整天的行程", cDentistRe),
        ("先出門囉", cNoSleep), ("算了不想講了", cDinner), ("我最近在追一部劇，講一個廚師的", cBbq),
        ("晚餐吃了拉麵", cWaitNight), ("欸，這週六我要帶朋友去吃飯，東區有什麼推薦的嗎？", cMeet),
        ("早安～我昨天晚上跟你說了什麼，你還記得嗎？", cBbq), ("我回來了", cDentistRe),
    ]
    for (q, c) in realUnrel { add(q, c, false, "real") }

    // Hand-made, owner style.
    let handRel: [(String, String)] = [
        ("牙齒還是有點痠", hTooth), ("牙齒還是有點痠", cDentistRe), ("牙痛", hTooth), ("看完牙醫了", hTooth),
        ("剛從牙醫那邊回來", cDentistAsk), ("醫生說沒事啦", cDentistAsk), ("下週三要複診喔？", hDentRemind),
        ("我差點忘了要看牙醫", hDentRemind), ("吃東西還是會痛", hTooth), ("今天開會好累", hBoss),
        ("老闆好煩", hBoss), ("會議結束了", hBoss), ("開完會了！", cMeet), ("被老闆念了一頓", hBoss),
        ("等等要開會好緊張", hBoss), ("明天要跟老闆報告", cMeetStress), ("老闆今天心情不錯欸", hBoss),
        ("咳嗽好一點了", hCold), ("喉嚨還是好痛", hCold), ("我好像發燒了", hCold),
        ("今天又看到那隻貓了！", hCat), ("橘貓回來了", hCat), ("我媽說她最近膝蓋不太好", hMom),
        ("剛到家，我媽煮了一桌菜", hMom), ("機票訂好了", hTrip), ("東京好冷", hTrip),
        ("今天去健身房了", hGym), ("腿好痠，昨天練太猛", hGym), ("咖哩還是太辣了", hCurry),
        ("晚安", cWaitNight), ("起來了", cWake), ("起來了啦", cWakeShort), ("好睏…不想起床", cWake),
        ("昨晚睡得很爛", cSleptWell), ("剛到家", cHome),
    ]
    for (q, c) in handRel { add(q, c, true, "hand") }
    let handUnrel: [(String, String)] = [
        ("早～", hTooth), ("早～", hBoss), ("我回來了", hTooth), ("我回來了", hBook), ("晚餐吃什麼好", hTooth),
        ("晚餐吃什麼好", hBoss), ("今天開會好累", hTooth), ("今天開會好累", hDentRemind), ("牙齒還是有點痠", hBoss),
        ("牙齒還是有點痠", cMeet), ("好熱", hBook), ("嗯", hBoss), ("哈哈", hTooth), ("在嗎", hDentRemind),
        ("好無聊", cMeet), ("下雨了", hCat), ("我先去洗澡", hBoss), ("我去睡了", hTooth),
        ("今天天氣好好", hDentRemind), ("想吃火鍋", hCold), ("你在幹嘛", hBook), ("剛下班", hTooth),
        ("我在看劇", hBoss), ("好想放假", hDentRemind), ("我要出門了", hCat), ("老闆好煩", hTooth),
        ("老闆好煩", hBook), ("喉嚨還是好痛", hBoss), ("橘貓回來了", hBoss), ("機票訂好了", hTooth),
        ("今天去健身房了", hDentRemind), ("我媽說她最近膝蓋不太好", hBoss), ("咖哩還是太辣了", hTooth),
        ("東京好冷", hCold), ("你覺得我要買新手機嗎", hBoss), ("今天公司附近新開一家拉麵店，排隊排好長", hTooth),
        ("週末想去爬山，你覺得陽明山還是象山", hBoss), ("我把房間整理好了，超累", hDentRemind),
        ("最近在追的那部劇好好看，男主角超帥", hMom), ("等一下要去超市買東西", hBoss),
        ("早安～", hCurry), ("晚安", hTooth), ("起來了", hBoss), ("剛到家", hTrip), ("好累", hBook),
        ("吃飽了", hDentRemind), ("今天好累喔", hCat), ("你好可愛", hBoss),
    ]
    for (q, c) in handUnrel { add(q, c, false, "hand") }

    let qs = Array(Set(pairs.map(\.q))), cs = Array(Set(pairs.map(\.c)))
    let qv = Dictionary(uniqueKeysWithValues: zip(qs, try embedder.embed(qs, as: .query)))
    let cv = Dictionary(uniqueKeysWithValues: zip(cs, try embedder.embed(cs, as: .document)))
    let scored = pairs.map { p in (p, zip(qv[p.q]!, cv[p.c]!).reduce(0.0) { $0 + Double($1.0 * $1.1) }) }

    func pct(_ s: [Double], _ p: Double) -> Double {
        let i = p * Double(s.count - 1); let lo = Int(i), hi = min(lo + 1, s.count - 1)
        return s[lo] + (s[hi] - s[lo]) * (i - Double(lo))
    }
    func f(_ x: Double) -> String { String(format: "%.3f", x) }
    func stats(_ name: String, _ xs: [Double]) {
        let s = xs.sorted()
        guard !s.isEmpty else { print("\(name): n=0"); return }
        print("\(name): n=\(s.count) min=\(f(s[0])) p10=\(f(pct(s, 0.1))) p25=\(f(pct(s, 0.25))) med=\(f(pct(s, 0.5))) p75=\(f(pct(s, 0.75))) p90=\(f(pct(s, 0.9))) max=\(f(s.last!))")
    }
    func table(_ title: String, _ sub: [(P, Double)]) {
        let r = sub.filter { $0.0.rel }.map(\.1), u = sub.filter { !$0.0.rel }.map(\.1)
        print("== \(title)")
        stats("  related  ", r); stats("  unrelated", u)
        print("  thr    recall  FP")
        var t = 0.25
        while t <= 0.6001 {
            let rec = r.isEmpty ? 0 : Double(r.filter { $0 >= t }.count) / Double(r.count)
            let fp = u.isEmpty ? 0 : Double(u.filter { $0 >= t }.count) / Double(u.count)
            print("  \(f(t))  \(f(rec))  \(f(fp))")
            t += 0.025
        }
    }
    func one(_ s: String) -> String { s.replacingOccurrences(of: "\n", with: "⏎") }
    print("ASSOC pairs: total=\(scored.count) related=\(scored.filter { $0.0.rel }.count) unrelated=\(scored.filter { !$0.0.rel }.count)")
    table("ALL", scored)
    table("real", scored.filter { $0.0.src == "real" })
    table("hand", scored.filter { $0.0.src == "hand" })
    table("query <=6 chars", scored.filter { $0.0.q.count <= 6 })
    table("query >6 chars", scored.filter { $0.0.q.count > 6 })
    print("== related sorted")
    for (p, s) in scored.filter({ $0.0.rel }).sorted(by: { $0.1 < $1.1 }) { print("  \(f(s)) [\(p.src)] \(one(p.q)) || \(p.c)") }
    print("== top-15 unrelated")
    for (p, s) in scored.filter({ !$0.0.rel }).sorted(by: { $0.1 > $1.1 }).prefix(15) { print("  \(f(s)) [\(p.src)] \(one(p.q)) || \(p.c)") }
    try out.write(toFile: "/tmp/assoc-out.txt", atomically: true, encoding: .utf8)
    Swift.print(out)
}
