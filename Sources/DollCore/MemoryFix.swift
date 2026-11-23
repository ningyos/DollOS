import Foundation

/// Correcting a memory (owner, 2026-11-05 and 2026-11-18). Before this she could not fix a memory she
/// got wrong: she could only `remember` another one and hope consolidation merged them.
///
/// - During the day, `memory.fix(id, text, basis?, why)`: the memory named by the id she sees in
///   【相關記憶】 and `recall` (〔…｜記憶 <id>〕) is replaced by `text` at once. A memory never leaves
///   what she can recall without something in its place (2026-11-20, owner: 「記憶不可以刪掉但可以覆蓋」):
///   `text` is required, retiring only is refused. A basis is required: text copied from what she
///   can cite (`Doll.citable`: people's lines, window and file reads, the computer and file tools'
///   results, the calendar), matched as `todo.done`'s evidence; her own words, thoughts, memories and
///   call arguments never count. Except for a memory she wrote herself (`hers`): she may overwrite it
///   without a basis.
/// - At maintenance, the audit (`Audit.swift`) changes or only flags the day's new memories, each only
///   with a verified quote of its source; one it would retire with nothing in its place it only flags.
///
/// The old memory is never deleted: it is retired with what replaced it and why (consolidation's
/// `retire`), so it leaves recall and attaching. The new one stays in the old one's folder (a
/// correction never moves a memory, so nothing secret becomes public), keeps its kind, entities,
/// evidence, deep mark and settled time (it is about the same thing: how often it came up stays),
/// and takes its basis's tier and confidence (a consolidated one stays consolidated, so it is still
/// attached). Every change is a record in
/// `<data_dir>/memory-fixes.json` and a commit in her history (`History.swift`); the owner sees them on
/// the dashboard and can undo one there (the history's undo), and she is told.
public struct MemoryFixRecord: Codable, Sendable, Equatable {
    public var id: Int
    public var ts: Double
    /// `her` (`memory.fix`), `audit` (the maintenance changed it), `flag` (the maintenance only flagged it).
    public var by: String
    /// The memory corrected (or flagged).
    public var memory: String
    /// Its text then.
    public var before: String
    /// The memory that took its place; nil when it was only retired, or flagged.
    public var replacement: String?
    /// The new text; for a flag, the text the audit proposed (nil: it proposed retiring it).
    public var after: String?
    /// The copied basis (hers) or the audit's quote, verified in `basisFrom`.
    public var basis: String
    public var basisFrom: String
    public var why: String
    /// The memory is in an encrypted folder (2026-11-21): on disk `before`, `after`, `basis` and `why`
    /// are `SecretBox` texts. Absent in records written before.
    public var sealed: Bool?
}

struct MemoryFixFile: Codable, Equatable {
    var nextId = 1
    var items: [MemoryFixRecord] = []

    /// `body` on every text of the sealed records (seal to write, open after reading).
    func mapSealed(_ body: (String) throws -> String) throws -> MemoryFixFile {
        var f = self
        for i in f.items.indices where f.items[i].sealed == true {
            f.items[i].before = try body(f.items[i].before)
            f.items[i].after = try f.items[i].after.map(body)
            f.items[i].basis = try body(f.items[i].basis)
            f.items[i].why = try body(f.items[i].why)
        }
        return f
    }

    /// The file at `url` with its sealed records opened; nil when there is none.
    static func read(_ url: URL, box: SecretBox) throws -> MemoryFixFile? {
        try Concerns.read(MemoryFixFile.self, url, what: "memory fixes")?.mapSealed { try box.open($0, place: url.path) }
    }
}

extension Doll {
    /// Two texts that differ only in spacing and punctuation say the same (symbols such as ⌘ count).
    static func sameText(_ a: String, _ b: String) -> Bool {
        func key(_ s: String) -> String { String(s.filter { !$0.isWhitespace && !$0.isPunctuation }).lowercased() }
        return key(a) == key(b)
    }

    func saveFixes() throws { try Concerns.write(try fixFile.mapSealed { try memory.box.seal($0) }, fixesURL) }

    /// Appends one record and saves the file.
    @discardableResult
    func recordFix(by: String, memory id: String, before: String, replacement: String?, after: String?, basis: String,
                   from: String, why: String, now: Double) throws -> MemoryFixRecord {
        guard let folder = try memory.file(id)?.folder else { throw MemoryError(description: "no memory file \(id)") }
        let r = MemoryFixRecord(id: fixFile.nextId, ts: now, by: by, memory: id, before: before, replacement: replacement,
                                after: after, basis: basis, basisFrom: from, why: why,
                                sealed: Memory.sealedFolders(memoryConfig).contains(memory.dirName(folder)) ? true : nil)
        fixFile.nextId += 1
        fixFile.items.append(r)
        try saveFixes()
        historyRefs.append("fix#\(r.id)")
        return r
    }

    /// `memory.fix`. Refusals say what is wrong and what to do instead; nothing is written then.
    func memoryFix(id: String, text: String?, basis: String, why: String, seen events: [Event], reply: Event) throws -> Outcome {
        func refuse(_ s: String) -> ToolError { ToolError(description: "沒有改：" + s) }
        let notFound = "沒有 id 是 \(id) 的記憶。id 照抄【相關記憶】或 recall 結果裡〔…｜記憶 <id>〕的那一串。"
        guard let (folder, file) = try memory.file(id) else { throw refuse(notFound) }
        // Whose turn it is decides the folders, not whether a secret was seen yet (m8 scenarios 5, 6:
        // she wrote her memory into secret herself, then could not fix it in the next plain turn). A
        // memory of a folder this audience never reads is one she has no id for: the same answer as
        // none. A secret one makes the result a secret event (5.1, as a secret recall hit does).
        let visible = Self.audience(events) == .developer && !secretsConfig.hideFromDeveloper
            ? Memory.visible(.owner, sawSecret: true) : Memory.visible(Self.audience(events), sawSecret: true)
        guard visible.contains(folder) else { throw refuse(notFound) }
        let secret = folder == .secret
        let sealed = sealedFolders.contains(memory.dirName(folder))
        if let r = file.retired {
            throw refuse("記憶 \(id) 已經收起來了（\(r.reason)）" + (r.supersededBy.isEmpty ? "。" : "，換成了記憶 \(r.supersededBy)；要改就改那一條。"))
        }
        guard Memory.strengthTiers.contains(file.tier) else {
            throw refuse("記憶 \(id) 是程式自己記的（做事的經驗或快捷鍵），不能用 \(Tools.Name.memoryFix) 改。")
        }
        // Never only retired (2026-11-20, owner): what she could recall is replaced, not taken away.
        guard let text else {
            throw refuse("記憶不會刪掉，只能換成新的內容：給 text，寫它應該是的整條。整條都錯、又不知道對的是什麼，text 就寫你知道的，例如「之前記的…是錯的（依據…）；正確的還不知道」。")
        }
        let w = why.trimmingCharacters(in: .whitespacesAndNewlines)
        if Self.sameText(text, file.body) {
            throw refuse("text 跟原本那條一樣；要改就寫改好的整條。")
        }
        let b = basis.trimmingCharacters(in: .whitespacesAndNewlines)
        if b.isEmpty {
            return try overwriteHers(id: id, file: file, text: text, why: w, secret: secret, sealed: sealed, reply: reply)
        }
        guard Self.plainKey(b).count >= computerConfig.planMinChars else {
            throw refuse("basis「\(b)」太短：至少 \(computerConfig.planMinChars) 個字，照抄一整段。")
        }
        guard let source = citable(before: reply).first(where: { Self.quoted(b, in: $0.text) }) else {
            let mine = Self.quoted(b, in: file.body)
                || events.contains { $0.kind == .doll && $0.seq! <= reply.seq! && (Self.quoted(b, in: $0.text) || $0.calls.contains { Self.quoted(b, in: $0.arguments ?? "") }) }
            throw refuse("basis「\(b)」不在主人說的話、你讀到的視窗或檔案、工具的結果裡。"
                         + (mine ? "那是你自己寫的字（你說的、想的或記的），不算依據。" : "")
                         + "依據要一字不差照抄那裡的字；只是覺得不對、找不到依據，就先去查（讀檔案、看視窗）或問主人。")
        }
        // A basis from the owner's secret may not land in a folder others can read.
        if folder != .secret, events.contains(where: { $0.secret && $0.seq! < reply.seq! && (Self.quoted(b, in: $0.text) || $0.window.map { Self.quoted(b, in: $0) } == true) }) {
            throw refuse("依據是主人的秘密，這條記憶在 \(memory.dirName(folder))，改了別人也看得到。要記這件事，用 remember 記進 secret。")
        }
        // A consolidated memory stays consolidated (so it is still attached); any other takes its basis's tier.
        let tier: SourceTier = file.tier == .consolidated ? .consolidated
            : source.source.hasPrefix(Addressee.owner.label) ? .owner : source.source.hasPrefix(Addressee.developer.label) ? .developer : .tool
        let seq = events.last { e in
            e.seq! < reply.seq! && e.kind != .doll && (Self.quoted(b, in: e.text) || e.window.map { Self.quoted(b, in: $0) } == true)
        }?.seq ?? reply.seq!
        let now = wallClock.now()
        let reason = "她改的：\(w)；依據（\(source.source)）「\(b)」"
        let replacement = try memory.replace(id, text: text, tier: tier, sourceSeq: seq, kind: nil, reason: reason,
                                             auditSegment: timeline.segment, now: now)
        let r = try recordFix(by: "her", memory: id, before: file.body, replacement: replacement, after: text, basis: b,
                              from: source.source, why: w, now: now)
        log("INFO", "turn \(turnIndex): memory.fix #\(r.id) \(id) → \(replacement) (\(tier.rawValue)) "
            + (sealed ? "basis from \(source.source) (encrypted folder: not logged)" : "basis「\(b)」from \(source.source)"))
        return Outcome(text: "改好了：記憶 \(id)「\(file.body)」換成記憶 \(replacement)「\(text)」。舊的收起來了，主人在儀表板看得到，可以還原。",
                       secret: secret, sealed: sealed, actions: ["memory fix"])
    }

    /// What "she wrote it herself" is (2026-11-20, owner: a memory she wrote herself she may overwrite
    /// herself): `remember` gave it the `doll` tier, because no person's line, watch wake or
    /// observing tool result was before her reply to be its source. A memory with any other tier is
    /// not hers to overwrite without a basis: the owner's or developer's words, a tool result, the
    /// maintenance's consolidation (written from the day's lines, often from the owner's), even when
    /// she was the one who called `remember` (the tier then names the line she was answering, and
    /// the program cannot tell her recollection from his words).
    static func hers(_ f: MemoryFile) -> Bool { f.tier == .doll }

    /// Her own memory overwritten without a basis: the new one stays hers (`doll`), so she may
    /// overwrite it again; the rest is as a correction with a basis.
    private func overwriteHers(id: String, file: MemoryFile, text: String, why w: String, secret: Bool, sealed: Bool, reply: Event) throws -> Outcome {
        guard Self.hers(file) else {
            let from = switch file.tier {
            case .owner: "主人說的話"
            case .developer: "開發者說的話"
            case .tool: "工具的結果或讀到的東西"
            case .consolidated: "系統維護整理時寫的"
            default: "別人說的話"
            }
            throw ToolError(description: "沒有改：記憶 \(id) 不是你自己憑印象記的（程式記的來源是\(from)），沒有依據不能改。basis 照抄主人說的話，或你讀到的\(computerOn ? "視窗、檔案、" : "")工具結果裡的字；找不到依據，就先去查或問主人。")
        }
        let now = wallClock.now()
        let reason = "她覆蓋自己記的：\(w)（她自己憑印象記的，沒有依據）"
        let replacement = try memory.replace(id, text: text, tier: .doll, sourceSeq: reply.seq!, kind: nil, reason: reason,
                                             auditSegment: timeline.segment, now: now)
        let r = try recordFix(by: "her", memory: id, before: file.body, replacement: replacement, after: text, basis: "",
                              from: Self.hersFrom, why: w, now: now)
        log("INFO", "turn \(turnIndex): memory.fix #\(r.id) \(id) → \(replacement) (doll) without a basis: her own")
        return Outcome(text: "改好了：記憶 \(id)「\(file.body)」換成記憶 \(replacement)「\(text)」（那條是你自己憑印象記的，不用依據）。舊的收起來了，主人在儀表板看得到，可以還原。",
                       secret: secret, sealed: sealed, actions: ["memory fix"])
    }

    /// `basisFrom` of an overwrite without a basis.
    static let hersFrom = "她自己憑印象記的"

    /// The record's button on the dashboard: a shortcut to undoing, in the history, the change that made
    /// it (the old memory counts again, the replacement goes; she is told by the undo). A flag changed
    /// nothing and has nothing to undo.
    public func dashboardMemoryRevert(_ fixID: Int) throws {
        guard let r = fixFile.items.first(where: { $0.id == fixID }) else { throw MemoryError(description: "no memory fix #\(fixID)") }
        guard r.by != "flag" else { throw MemoryError(description: "#\(fixID) is a flag: nothing was changed") }
        do { try undoRef("fix#\(fixID)") } catch let e as HistoryError { throw MemoryError(description: e.description) }
    }
}
