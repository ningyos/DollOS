import Foundation

/// Consolidation (2026-11-11): the day's timeline becomes long-term memories, then a new timeline
/// segment begins, as if she slept. Manual only (`dollcore --config … consolidate <data_dir>`, the
/// m2 runner's `--consolidate on`); run it only while her process is not running.
///
/// One separate model request, not in her context and without her persona: the program renders the
/// current segment as plain text (`tasks`: no images, no program 【快捷鍵】／【相關記憶】, split at each
/// owner line), recalls related existing memories per task (`revisable` tiers, never retired, never
/// today's events), and asks for a fixed-format list of operations (`instructions`): add, update,
/// retire. The reply is validated whole (`parse`); a failed request or any invalid operation is an
/// error that writes nothing and keeps the segment. Then, in order: files retired longer than
/// `[consolidate] retired_keep_days` are deleted, the operations are applied, a new segment starts
/// whose first event is the day's summary (a second clean request, 2026-11-15).
public struct ConsolidationError: Error, CustomStringConvertible {
    public let description: String
    init(_ description: String) { self.description = description }
}

public enum ConsolidationOp: Sendable, Equatable {
    /// `ref`: the reply's own name for it, so a retire in the same reply can point at it. Each
    /// entity's id: a registry entry the request showed, or a `NewEntity`'s ref.
    case add(ref: String, text: String, folder: MemoryFolder, kind: MemoryKind?, entities: [MemoryEntity])
    /// The new text and the new entities (they replace the old).
    case update(id: String, text: String, entities: [MemoryEntity])
    /// `supersededBy`: an existing memory's id or an add's ref, as the reply gave it.
    case retire(id: String, supersededBy: String, reason: String)
}

/// A registry entry the reply creates (2026-11-11): its operations' entities name it by `ref`.
public struct NewEntity: Sendable, Equatable {
    public var ref: String
    public var type: EntityType
    public var name: String
    public var aliases: [String]
    public var description: String
}

/// A validated reply: the new registry entries, then the operations.
public struct ConsolidationPlan: Sendable, Equatable {
    public var entities: [NewEntity]
    public var operations: [ConsolidationOp]
}

public struct ConsolidationResult: Sendable {
    /// The segment consolidated, and the one started after it.
    public var segment: String
    public var newSegment: String
    public var plan: ConsolidationPlan
    /// One line per created registry entry, added alias, applied operation and deleted file, in
    /// order, texts verbatim.
    public var applied: [String]
    /// The existing memories the request showed, by id.
    public var related: [String]
    /// The registry entries the request showed, by id.
    public var registry: [String]
    public var promptTokens: Int
    public var completionTokens: Int
    public var seconds: Double
    /// The model's reply verbatim (the operations block).
    public var reply: String

    public var report: String {
        (["segment \(segment) → new segment \(newSegment)",
          "request: \(promptTokens) prompt tokens, \(completionTokens) completion tokens, \(String(format: "%.1f", seconds)) s",
          "related existing memories shown: \(related.count) \(related)",
          "registry entries shown: \(registry.count) \(registry)",
          "applied (\(applied.count)):"] + applied.map { "- " + $0 }).joined(separator: "\n")
    }
}

public enum Consolidation {
    /// Tiers consolidation may see and change: what she or consolidation wrote. The program's own
    /// records stay the program's: shortcuts (re-read per app version) and experiences (their stats
    /// are kept by the program, and the experience mechanism is unchanged).
    static let revisable: Set<SourceTier> = [.owner, .developer, .tool, .stranger, .doll, .consolidated]

    /// The main model with `[consolidate]`'s max_tokens and timeout.
    public static func model(_ config: Config) -> HTTPMainModel {
        var llm = config.llm
        llm.maxTokens = config.consolidate.maxTokens
        llm.timeoutS = config.consolidate.timeoutS
        return HTTPMainModel(llm)
    }

    /// Consolidates the newest segment of `config.core.dataDir` into `config.memory.dir` (both
    /// relative to `root` unless absolute), embedding with `embedder`.
    public static func run(config: Config, root: URL, model: any MainModel,
                           embedder: any Embedder) async throws -> ConsolidationResult {
        func path(_ p: String) -> URL { p.hasPrefix("/") ? URL(filePath: p) : root.appending(path: p) }
        let data = path(config.core.dataDir)
        let timelineDir = data.appending(path: "timeline")
        let timeline = try Timeline(dir: timelineDir)
        guard !timeline.events.isEmpty else {
            throw ConsolidationError("segment \(timeline.segment) is empty: nothing to consolidate")
        }
        let memory = try Memory(config: config.memory, root: path(config.memory.dir), store: try Store(path: data.appending(path: "doll.db")),
                                embedder: embedder)
        try memory.syncOnBoot()
        try await model.check()

        let tasks = Self.tasks(timeline.events)
        var related: [MemoryHit] = []
        var seen = Set<String>()
        for task in tasks {
            for q in task.queries {
                let hits = try memory.search(q, visible: Set(MemoryFolder.allCases), limit: config.consolidate.recallK, tiers: revisable)
                for h in hits where seen.insert(h.id).inserted { related.append(h) }
            }
        }
        let shown = try related.map { h in (h, try memory.file(h.id)?.file.entities ?? []) }
        // The registry entries the shown memories name, then those near each task's queries.
        var entries: [RegistryEntry] = []
        var taken = Set<String>()
        for (_, list) in shown {
            for e in list where taken.insert(e.id).inserted {
                if let entry = try memory.registry.entry(e.id) { entries.append(entry) }
            }
        }
        for task in tasks {
            for q in task.queries {
                for e in try memory.registry.nearby(q, limit: config.consolidate.registryK) where taken.insert(e.id).inserted {
                    entries.append(e)
                }
            }
        }
        let messages = [ChatMessage(role: "system", content: instructions),
                        ChatMessage(role: "user", content: input(segment: timeline.segment, tasks: tasks, related: shown, registry: entries))]
        let started = Date()
        let reply = try await model.chat(messages, tools: nil, effort: config.consolidate.effort)
        let seconds = Date().timeIntervalSince(started)
        let plan = try parse(reply.content, known: Set(related.map(\.id)), registry: Set(entries.map(\.id)))
        // What she wakes up with (owner, 2026-11-15: sleep is not a compaction, and it does not empty
        // her context): the day, as a summary a second clean request writes. Asked for before anything
        // is applied, so a failure here still writes nothing.
        let day = try await model.chat([ChatMessage(role: "system", content: Doll.talkInstructions),
                                        ChatMessage(role: "user", content: tasks.map(\.text).joined(separator: "\n\n"))],
                                       tools: nil, effort: config.context.talkEffort)
        let summary = day.content.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !summary.isEmpty else { throw ConsolidationError("the summary request for the new segment returned no text") }

        // Valid: from here on it is applied.
        let now = Date().timeIntervalSince1970
        var applied = try memory.deleteRetired(before: now - Double(config.consolidate.retiredKeepDays) * 86400)
            .map { "deleted \($0) (retired more than \(config.consolidate.retiredKeepDays) days ago)" }
        // New registry entries: only those an operation names.
        let used = Set(plan.operations.flatMap { op -> [String] in
            switch op {
            case .add(_, _, _, _, let entities), .update(_, _, let entities): entities.map(\.id)
            case .retire: []
            }
        })
        var entityIDs: [String: String] = [:]
        for n in plan.entities {
            guard used.contains(n.ref) else {
                applied.append("entity ref \(n.ref) (\(n.name)) not created: no operation names it")
                continue
            }
            let e = try memory.registry.create(n.type, name: n.name, aliases: n.aliases, description: n.description)
            entityIDs[n.ref] = e.id
            applied.append("entity \(e.id) (ref \(n.ref), \(e.type.rawValue), aliases \(Registry.json(e.aliases))) \(e.name): \(e.description)")
        }
        /// The operation's entities with registry ids; a name an entry does not have yet becomes its alias.
        func aligned(_ list: [MemoryEntity]) throws -> [MemoryEntity] {
            try list.map { m in
                let id = entityIDs[m.id] ?? m.id
                if try memory.registry.addAlias(id, m.name) { applied.append("alias \(id) += \(m.name)") }
                return MemoryEntity(id, m.name)
            }
        }
        var refs: [String: String] = [:]
        for op in plan.operations {
            switch op {
            case .add(let ref, let text, let folder, let kind, let entities):
                let list = try aligned(entities)
                let id = try memory.write(text, folder: folder, sourceSeq: timeline.lastSeq, tier: .consolidated, kind: kind,
                                          entities: list)
                refs[ref] = id
                applied.append("add \(memory.dirName(folder))/\(id) (ref \(ref), kind \(kind?.rawValue ?? "none"), entities \(MemoryEntity.json(list))): \(text)")
            case .update(let id, let text, let entities):
                let list = try aligned(entities)
                let before = try memory.file(id)?.file.body ?? ""
                try memory.update(id, text: text, entities: list)
                applied.append("update \(id) (entities \(MemoryEntity.json(list))): \(before) ⇒ \(text)")
            case .retire(let id, let by, let reason):
                let target = refs[by] ?? by
                try memory.retire(id, Retirement(ts: now, supersededBy: target, reason: reason))
                applied.append("retire \(id) (superseded by \(target)): \(reason)")
            }
        }
        let newSegment = try Timeline.startSegment(dir: timelineDir)
        try Timeline(dir: timelineDir).append(Event(.memory, .internal, Self.wakeNote + summary))
        return ConsolidationResult(segment: timeline.segment, newSegment: newSegment, plan: plan, applied: applied,
                                   related: related.map(\.id), registry: entries.map(\.id), promptTokens: reply.usage.promptTokens,
                                   completionTokens: reply.usage.completionTokens, seconds: seconds, reply: reply.content)
    }

    /// Opens the first event of the segment after a consolidation; the day's summary follows.
    public static let wakeNote = "【睡前】你睡了一覺，睡前的事整理進長期記憶了。睡前的對話和做過的事不在眼前了，換成下面這段另外寫的摘要（不是你寫的）；要細節用 recall 查記憶。\n"

    // rendering ---------------------------------------------------------------

    /// One task of the day: its rendered lines, whether an owner or developer line began it, and what
    /// to recall for it (that line, and her `todo.done` results and `todo.fail` reasons).
    public struct DayTask: Sendable, Equatable {
        public var text: String
        public var owner: Bool
        public var queries: [String]
    }

    /// The segment as plain text, a new task at each owner line (events before the first one are a
    /// task of their own). Each line starts with the local time `HH:MM` and who: the owner's words, her
    /// thought, what she said and where (她說｜Discord 私訊 / 她說｜終端機 / 她說｜Discord 語音), her other calls (name and arguments), tool results' text, watch wakes;
    /// 「｜秘密」 marks a secret event. Left out: images (only the result text is shown, not the window
    /// outline), the program's 【快捷鍵】 and 【相關記憶】, and `recall` results (copies of other
    /// data, as in `TodayIndex`).
    public static func tasks(_ events: [Event]) -> [DayTask] {
        let names = Doll.callNames(events)
        var out: [DayTask] = []
        var lines: [String] = [], queries: [String] = [], owner = false
        func flush() {
            if !lines.isEmpty { out.append(DayTask(text: lines.joined(separator: "\n"), owner: owner, queries: queries)) }
            lines = []
            queries = []
            owner = false
        }
        for ev in events {
            let c = Calendar.current.dateComponents([.hour, .minute], from: Date(timeIntervalSince1970: ev.ts))
            let mark = ev.secret ? "｜秘密" : ""
            func line(_ who: String, _ text: String) {
                let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
                if !t.isEmpty { lines.append(String(format: "[%02d:%02d ", c.hour!, c.minute!) + who + mark + "] " + t) }
            }
            switch ev.kind {
            case .owner, .developer:
                flush()
                owner = true
                line(ev.kind == .owner ? "主人" : "開發者", ev.text)
                queries.append(ev.text)
            case .doll:
                line("她想", ev.text.trimmingCharacters(in: .whitespacesAndNewlines))
                for call in ev.calls {
                    if call.malformed == nil, let channel = Tools.speaking[call.name] {
                        line("她說｜\(channel.label)", call.parameters.first { $0.name == "text" }?.value ?? "")
                        continue
                    }
                    let args = call.parameters.map { "\($0.name): \($0.value.trimmingCharacters(in: .whitespacesAndNewlines).replacingOccurrences(of: "\n", with: "⏎"))" }
                    line("她呼叫", "\(call.name.isEmpty ? "tool_call" : call.name)(\(args.joined(separator: "; ")))"
                         + (call.malformed == nil ? "" : "（格式錯）"))
                    if let key = ["todo.done": "result", "todo.fail": "reason"][call.name],
                       let s = call.parameters.first(where: { $0.name == key }) { queries.append(s.value) }
                }
            case .tool:
                let name = (names[ev.seq!] ?? "").isEmpty ? "tool_call" : names[ev.seq!]!
                if !TodayIndex.unindexedResults.contains(name) { line("\(name) 的結果", ev.text) }
            case .watch: line("watch", ev.text)
            case .memory: break
            }
        }
        flush()
        return out
    }

    /// The request's user message: the related existing memories with their ids and entities, the
    /// registry entries that may be the things today names, then the day by task.
    static func input(segment: String, tasks: [DayTask], related: [(hit: MemoryHit, entities: [MemoryEntity])],
                      registry: [RegistryEntry]) -> String {
        let memories = related.isEmpty ? ["（沒有）"] : related.map { h, entities in
            "[id: \(h.id)｜\(h.folder)｜kind: \(h.kind?.rawValue ?? "")｜entities: \(MemoryEntity.json(entities))]\n\(h.text)"
        }
        let entries = registry.isEmpty ? ["（沒有）"] : registry.map { e in
            "[id: \(e.id)｜\(e.type.rawValue)｜\(e.name)｜別名：\(e.aliases.isEmpty ? "（沒有）" : e.aliases.joined(separator: "、"))] \(e.description)"
        }
        var n = 0
        let days = tasks.map { t in
            if t.owner { n += 1 }
            return (t.owner ? "## 任務 \(n)" : "## 主人說話之前") + "\n" + t.text
        }
        return (["# 既有的長期記憶（跟今天的任務有關的）"] + memories + ["# 登記簿裡可能相關的東西"] + [entries.joined(separator: "\n")]
                + ["# 今天的時間線（\(segment)）"] + days)
            .joined(separator: "\n\n")
    }

    /// The consolidation request's instructions (no persona: the model is not her).
    static let instructions = """
        你負責整理一個 AI 助理一天的經歷：把今天時間線裡值得長期記住的東西，寫成她的長期記憶。你不是她，不用扮演誰，只輸出整理結果。

        輸入有三部分：
        1. 既有的長期記憶：每條有 id、資料夾、kind、entities 和內容。
        2. 登記簿裡可能相關的東西：登記簿記著記憶提到的每個具體東西，每條有 id、種類、正式名字、別名和說明（它是什麼、在哪裡）。
        3. 今天的時間線，依主人的每一句話分成任務。每行開頭是時間和誰：主人說的話、她想的、她說的（「她說｜」後面是她說在哪裡：Discord 私訊、終端機、Discord 語音）、她呼叫的工具和參數、工具的結果、watch 叫醒。標「｜秘密」的是含主人秘密的事件。截圖沒有放進來，只有文字。

        要寫的記憶：
        - 技能：一條一個片段：情境＋動作＋結果，例如「TextEdit 按 ⇧⌥⌘S → 跳出另存對話框，檔名欄已選取」。寫具體的 app、按鍵或點的元素、之後畫面或檔案變成什麼。只寫時間線上真的做了、結果看得到的。走了冤枉路的，寫走通的那條路；從今天的經過看得出更短的走法，也可以把一串動作寫成一條（情境＋一串動作＋結果）。值得避開的失敗寫成「做 X → 失敗：原因」。
        - 其他記憶：主人的事實、偏好、今天發生的事、說好要做的事。kind：已經發生的事 happened，打算或約好要做的 intended，沒證實的 uncertain，事實和偏好給空字串。
        - 資料夾 folder：主人的秘密、和在含秘密的事件裡得知的事放 secret；她自己的密碼、帳號、token 放 own；其他放 public。
        - 不要記她自己的工具怎麼用（discord.dm、voice.join、voice.say、todo.set、remember、recall 這些怎麼呼叫、呼叫了會怎樣：她本來就知道），也不要把這些工具登記成東西。系統自己出的錯（連線斷了、bridge 掛了、程式報的錯誤訊息和數字）不是值得長期記的事，除非主人為了它交代了什麼。
        - 不要寫時間線上沒有的東西，不要猜。不要抄快捷鍵清單（程式另外記了）。既有記憶已經寫了的不要再寫一次。用繁體中文。
        - 既有記憶：今天的經歷讓它更完整或要修正它時用 update，給新的全文和新的 entities；被今天的新記憶取代時（例如找到更短的走法、事實變了）用 retire，superseded_by 填取代它的那條：既有記憶的 id，或你這次 add 的 ref。跟今天無關的不要動。
        - entities：每條 add 和 update 都列出這條記憶提到的具體東西（app、畫面元素／對話框／選單項目、快捷鍵、檔案／資料夾／路徑、網站、人、其他具體的東西；抽象的動作不算），之後用來把提到同一個東西的記憶連起來。每個是 {"name": 記憶內文裡的寫法, "id": 它在登記簿裡的 id}。
        - 對齊登記簿：同一個東西只能有一條登記。它就是登記簿裡的某一條時（別的語言的名字、縮寫、完整路徑和只寫檔名、大小寫不同都算同一個），id 填那一條的 id。登記簿裡沒有的，在最上層的 entities 新增一條：ref（你取的代號，new1、new2…）、type（app、ui、shortcut、file、website、person、other）、name（正式名字）、aliases（你知道的其他寫法，例如另一種語言的名字、完整路徑；沒有就 []）、description（一句話：它是什麼、在哪裡），記憶的 entities 裡 id 就填這個 ref。名字一樣但其實是不同的東西（例如不同資料夾裡同名的檔案），要新增另一條，用 description 分清楚。

        輸出格式：只輸出一個 ```json 區塊，內容是一個物件：
        ```json
        {"entities": [
          {"ref": "new1", "type": "app", "name": "TextEdit", "aliases": ["文字編輯"], "description": "macOS 內建的文字編輯 app"}
        ],
        "operations": [
          {"op": "add", "ref": "n1", "text": "…", "folder": "public", "kind": "", "entities": [{"name": "TextEdit", "id": "new1"}, {"name": "⇧⌥⌘S", "id": "登記簿裡的 id"}]},
          {"op": "update", "id": "既有記憶的 id", "text": "新的全文", "entities": [{"name": "notes.txt", "id": "登記簿裡的 id"}]},
          {"op": "retire", "id": "既有記憶的 id", "superseded_by": "n1", "reason": "…"}
        ]}
        ```
        每個操作和每個新登記只能有上面那些欄位。ref 不能重複。update 和 retire 只能用既有記憶列出來的 id，同一個 id 只能出現一次；entities 的 id 只能是登記簿列出來的 id 或你新增的 ref，一條記憶裡同一個 id 只能出現一次。沒有值得記的就輸出 {"entities": [], "operations": []}。
        """

    // the reply -----------------------------------------------------------------

    /// The plan in `reply`: exactly one ```json block holding {"entities": [...], "operations": [...]},
    /// each new entity and operation with exactly its fields. `known`: the memory ids the request
    /// showed; update and retire may name only those, each id at most once. `registry`: the registry
    /// ids it showed; an operation's entity names one of those or a new entity's ref. Anything else
    /// is an error naming what is wrong.
    public static func parse(_ reply: String, known: Set<String>, registry: Set<String>) throws -> ConsolidationPlan {
        let blocks = reply.components(separatedBy: "```json")
        guard blocks.count == 2, let close = blocks[1].range(of: "```") else {
            throw ConsolidationError("the reply must hold exactly one ```json block; got \(blocks.count - 1)")
        }
        let body = Data(blocks[1][..<close.lowerBound].utf8)
        guard let top = (try? JSONSerialization.jsonObject(with: body)) as? [String: Any] else {
            throw ConsolidationError("the ```json block is not a JSON object")
        }
        guard Set(top.keys) == ["entities", "operations"], let newList = top["entities"] as? [Any],
              let list = top["operations"] as? [Any] else {
            throw ConsolidationError("the object must have exactly two keys, entities and operations, both arrays; got keys \(top.keys.sorted())")
        }
        var news: [NewEntity] = []
        var entityRefs = Set<String>()
        for (i, item) in newList.enumerated() {
            let n = i + 1
            guard let o = item as? [String: Any], Set(o.keys) == ["ref", "type", "name", "aliases", "description"],
                  let ref = o["ref"] as? String, let typeName = o["type"] as? String, let name = o["name"] as? String,
                  let aliases = o["aliases"] as? [String], let description = o["description"] as? String else {
                throw ConsolidationError("entity \(n) must be an object with exactly ref, type, name, description (strings) and aliases (an array of strings)")
            }
            guard !ref.isEmpty, !registry.contains(ref), entityRefs.insert(ref).inserted else {
                throw ConsolidationError("entity \(n): ref \(ref) is empty, repeated, or a registry id")
            }
            guard let type = EntityType(rawValue: typeName) else {
                throw ConsolidationError("entity \(n): type must be one of \(EntityType.allCases.map(\.rawValue)); got \(typeName)")
            }
            let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
            let what = description.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !Registry.key(trimmed).isEmpty, !what.isEmpty else {
                throw ConsolidationError("entity \(n): name and description must not be empty")
            }
            news.append(NewEntity(ref: ref, type: type, name: trimmed, aliases: aliases, description: what))
        }
        var ops: [ConsolidationOp] = []
        var refs = Set<String>(), touched = Set<String>()
        func string(_ o: [String: Any], _ key: String, _ n: Int) throws -> String {
            guard let s = o[key] as? String else { throw ConsolidationError("operation \(n): \(key) must be a string") }
            return s
        }
        func entities(_ o: [String: Any], _ n: Int) throws -> [MemoryEntity] {
            let list: [MemoryEntity]
            do { list = try MemoryEntity.validated(o["entities"] as Any) } catch {
                throw ConsolidationError("operation \(n): \(error)")
            }
            for e in list where !registry.contains(e.id) && !entityRefs.contains(e.id) {
                throw ConsolidationError("operation \(n): entity \(e.name) has id \(e.id), neither a registry id shown nor a new entity's ref")
            }
            return list
        }
        for (i, item) in list.enumerated() {
            let n = i + 1
            guard let o = item as? [String: Any] else { throw ConsolidationError("operation \(n) is not an object") }
            let op = try string(o, "op", n)
            let fields: Set<String> = switch op {
            case "add": ["op", "ref", "text", "folder", "kind", "entities"]
            case "update": ["op", "id", "text", "entities"]
            case "retire": ["op", "id", "superseded_by", "reason"]
            default: throw ConsolidationError("operation \(n): op must be add, update or retire; got \(op)")
            }
            guard Set(o.keys) == fields else {
                throw ConsolidationError("operation \(n) (\(op)) must have exactly \(fields.sorted()); got \(o.keys.sorted())")
            }
            switch op {
            case "add":
                let ref = try string(o, "ref", n), text = try string(o, "text", n).trimmingCharacters(in: .whitespacesAndNewlines)
                let folderName = try string(o, "folder", n), kindName = try string(o, "kind", n)
                guard !ref.isEmpty, !known.contains(ref), refs.insert(ref).inserted else {
                    throw ConsolidationError("operation \(n): ref \(ref) is empty, repeated, or an existing id")
                }
                guard !text.isEmpty else { throw ConsolidationError("operation \(n): text is empty") }
                guard let folder = MemoryFolder(rawValue: folderName) else {
                    throw ConsolidationError("operation \(n): folder must be secret, own or public; got \(folderName)")
                }
                let kind = MemoryKind(rawValue: kindName)
                guard kind != nil || kindName.isEmpty else {
                    throw ConsolidationError("operation \(n): kind must be happened, intended, uncertain or empty; got \(kindName)")
                }
                ops.append(.add(ref: ref, text: text, folder: folder, kind: kind, entities: try entities(o, n)))
            case "update":
                let id = try string(o, "id", n), text = try string(o, "text", n).trimmingCharacters(in: .whitespacesAndNewlines)
                guard known.contains(id), touched.insert(id).inserted else {
                    throw ConsolidationError("operation \(n): id \(id) is not one of the existing memories shown, or is used twice")
                }
                guard !text.isEmpty else { throw ConsolidationError("operation \(n): text is empty") }
                ops.append(.update(id: id, text: text, entities: try entities(o, n)))
            default:
                let id = try string(o, "id", n), by = try string(o, "superseded_by", n)
                let reason = try string(o, "reason", n).trimmingCharacters(in: .whitespacesAndNewlines)
                guard known.contains(id), touched.insert(id).inserted else {
                    throw ConsolidationError("operation \(n): id \(id) is not one of the existing memories shown, or is used twice")
                }
                guard !reason.isEmpty else { throw ConsolidationError("operation \(n): reason is empty") }
                ops.append(.retire(id: id, supersededBy: by, reason: reason))
            }
        }
        // superseded_by: an add's ref (anywhere in the list) or an existing id that is not retired here.
        let retired = Set(ops.compactMap { if case .retire(let id, _, _) = $0 { id } else { nil } })
        for (i, op) in ops.enumerated() {
            guard case .retire(let id, let by, _) = op else { continue }
            guard by != id, refs.contains(by) || (known.contains(by) && !retired.contains(by)) else {
                throw ConsolidationError("operation \(i + 1): superseded_by \(by) must be an add's ref or an existing id not retired here, not the retired one itself")
            }
        }
        return ConsolidationPlan(entities: news, operations: ops)
    }
}
