import Foundation
import SQLite3

/// The memory index (M3a): rebuildable from the markdown files, never the source.
/// `tokenize='trigram'` because the records are Chinese: the default tokenizer would
/// not split them at all.
private let memorySchema = """
CREATE TABLE IF NOT EXISTS memory_index (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    folder TEXT NOT NULL,            -- the config directory name of secret | own | public
    confidence REAL NOT NULL,
    ts REAL NOT NULL,
    source_seq INTEGER NOT NULL,
    kind TEXT NOT NULL               -- happened | intended | uncertain, as she gave it; empty when she gave none
);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    text, id UNINDEXED, folder UNINDEXED, tokenize='trigram'
);
CREATE TABLE IF NOT EXISTS memory_vectors (
    id TEXT PRIMARY KEY,
    vector BLOB NOT NULL             -- Float32, L2-normalized
);
-- Before 2026-11-11 (registry): entity names per memory; the index rebuilds into memory_entity_refs.
DROP TABLE IF EXISTS memory_entities;
CREATE TABLE IF NOT EXISTS memory_entity_refs (
    id TEXT NOT NULL,                -- the memory's id
    entity TEXT NOT NULL,            -- the registry entry's id
    folder TEXT NOT NULL,
    tier TEXT NOT NULL,              -- the memory's source_tier
    PRIMARY KEY (id, entity)
);
CREATE INDEX IF NOT EXISTS memory_entity_refs_entity ON memory_entity_refs (entity);
CREATE TABLE IF NOT EXISTS registry_index (
    id TEXT PRIMARY KEY,             -- the registry entry's id (its file name)
    type TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS registry_keys (
    key TEXT NOT NULL,               -- Registry.key of the canonical name or an alias
    id TEXT NOT NULL,
    PRIMARY KEY (key, id)
);
CREATE VIRTUAL TABLE IF NOT EXISTS registry_fts USING fts5(
    text, id UNINDEXED, tokenize='trigram'
);
CREATE TABLE IF NOT EXISTS registry_vectors (
    id TEXT PRIMARY KEY,
    vector BLOB NOT NULL             -- Float32, L2-normalized, of RegistryEntry.line
);
CREATE TABLE IF NOT EXISTS memory_index_version (
    one INTEGER PRIMARY KEY CHECK (one = 1),
    version INTEGER NOT NULL         -- Memory.indexVersion of the code that built the index
);
"""

/// The memory tables again in the attached in-memory schema `sealed` (owner, 2026-11-21): the
/// rows of the encrypted folders (`secret`, `own`) live only here, built at boot from the decrypted
/// files (`Memory.syncOnBoot`); their text, trigram index and vectors never reach the disk.
private let sealedSchema = """
PRAGMA temp_store = MEMORY;
ATTACH DATABASE ':memory:' AS sealed;
CREATE TABLE sealed.memory_index (
    id TEXT PRIMARY KEY, path TEXT NOT NULL, folder TEXT NOT NULL, confidence REAL NOT NULL,
    ts REAL NOT NULL, source_seq INTEGER NOT NULL, kind TEXT NOT NULL
);
CREATE VIRTUAL TABLE sealed.memory_fts USING fts5(text, id UNINDEXED, folder UNINDEXED, tokenize='trigram');
CREATE TABLE sealed.memory_vectors (id TEXT PRIMARY KEY, vector BLOB NOT NULL);
CREATE TABLE sealed.memory_entity_refs (
    id TEXT NOT NULL, entity TEXT NOT NULL, folder TEXT NOT NULL, tier TEXT NOT NULL, PRIMARY KEY (id, entity)
);
CREATE INDEX sealed.memory_entity_refs_entity ON memory_entity_refs (entity);
"""

public struct StoreError: Error, CustomStringConvertible {
    public let description: String
}

public enum Principal: String, Sendable, Codable {
    case owner, developer, `internal`
}

/// A person she hears and answers: the owner (Discord) or the developer (`dollctl chat`).
public enum Addressee: String, Sendable, Codable, CaseIterable {
    case owner, developer

    /// How the prompt, the dashboard and the logs name them.
    public var label: String {
        switch self {
        case .owner: "主人"
        case .developer: "開發者"
        }
    }
}

/// Where a person's line came from (2026-11-12): shown to her with who said it and whether it was
/// spoken or typed, so she knows where each line comes from once there is more than one place.
public enum Channel: String, Sendable, Codable, CaseIterable {
    case discordDM = "discord_dm"
    /// The owner's speech in a Discord voice channel, transcribed (2026-11-13, M6).
    case discordVoice = "discord_voice"
    case dollctl

    public var label: String {
        switch self {
        case .discordDM: "Discord 私訊"
        case .discordVoice: "Discord 語音"
        case .dollctl: "終端機"
        }
    }
}

public struct Event: Sendable, Equatable {
    public enum Kind: String, Sendable, Codable {
        case owner      // the owner spoke
        case developer  // the developer typed in `dollctl chat` (2026-11-12): renders 開發者, never sees the secret folder
        case doll       // her reply: her thought (the text content) and her calls
        case tool       // the result of one of her tool calls, in call order
        case watch      // her watch woke her: the region changed, or max_wait passed
        case memory     // 【相關記憶】: records the program attached for her (spec 3.1, 2026-11-09)
        case calendar   // 【行事曆】: a timed calendar item's start arrived (2026-11-15); renders as 程式, gives her a turn
    }
    public var kind: Kind
    public var principal: Principal
    public var text: String
    public var secret: Bool
    /// Stored encrypted (2026-11-21): a secret event always is; `sealed` marks one that is not secret
    /// but carries the content of an encrypted memory folder (her `own` memories in a recall result,
    /// a `remember` into `own`, a fix of one). Visibility stays `secret`'s alone.
    public var sealed = false
    /// Only owner voice may cancel a model call in flight (voice itself is M6).
    public var voice: Bool
    /// A computer read's window screenshot: a file name under the data dir's `shots/`; rendered as an image part.
    public var image: String?
    /// A computer read's window element list (2026-11-16); rendered verbatim after the screenshot.
    public var window: String?
    /// For an owner or developer line: where it came from (2026-11-12). For the result of her
    /// `discord.dm` / `dollctl.reply` / `voice.say` call that went out: where it went (2026-11-14).
    public var channel: Channel?
    public var seq: Int64?
    public var ts: Double
    /// For her replies: the footer that preceded it in the prompt. Kept in the prompt so the
    /// server's prefix cache stays valid; a `footer` record in the timeline file, not an event.
    public var footer: String?
    /// For her replies: her calls in the order she wrote them (2026-11-14: the response's
    /// `tool_calls`, arguments text as returned, then the calls the server returned as text).
    public var calls: [ToolCall] = []

    public init(_ kind: Kind, _ principal: Principal, _ text: String, secret: Bool = false, sealed: Bool = false, voice: Bool = false,
                image: String? = nil, window: String? = nil) {
        self.kind = kind
        self.principal = principal
        self.text = text
        self.secret = secret
        self.sealed = sealed
        self.voice = voice
        self.image = image
        self.window = window
        self.seq = nil
        self.ts = Date().timeIntervalSince1970
    }

    public var priority: Priority {
        switch kind {
        case .owner: voice ? .ownerVoice : .ownerText
        case .developer: .developerText
        case .watch: .watch
        case .calendar: .calendar
        case .memory where text.hasPrefix(Concerns.wanderTag): .wander   // her wandering (m6-drives)
        case .doll, .tool, .memory: .watch   // only program notices (【語音】, M6) are queued; the rest go straight to the timeline
        }
    }

    /// The person this event is from, for owner and developer lines; nil for everything else.
    public var speaker: Addressee? {
        switch kind {
        case .owner: .owner
        case .developer: .developer
        case .doll, .tool, .watch, .memory, .calendar: nil
        }
    }
}


// timeline ---------------------------------------------------------------------

/// Her raw timeline (2026-11-10): one append-only JSONL file per segment, `<data_dir>/timeline/<segment>.jsonl`.
/// A segment is one day, from one maintenance to the next; a process continues the newest segment.
/// Consolidation (2026-11-11; since 2026-11-16 in the daily maintenance) ends the day: it starts a new, empty
/// segment (`startSegment`), so the next process begins a fresh today. The file is the only source of the events,
/// the footers, the turn stats, the todo list and the compactions; `TodayIndex` is a rebuildable index of it.
///
/// One JSON object per line, told apart by `type` (keys snake_case, absent optionals left out):
///
///     {"type":"event","seq":1,"ts":…,"kind":"owner|developer|doll|tool|watch|memory","source":"owner|developer|internal",
///      "secret":false,"voice":false,"text":"…","image":"…","window":"…","channel":"discord_dm|discord_voice|dollctl"}
///     {"type":"footer","seq":2,"text":"【狀況】…"}               the footer her reply `seq` answered
///     {"type":"todo","seq":5,"phase":"idle|doing|waiting|failed","current":"…","pending":["…"],"start_seq":4,
///      "rounds":3,"decide_at":15,"deciding":false,"go_on":["…"]}    the todo list after her reply `seq` changed it
///                                                                   (the last four from 2026-11-16; absent in older lines = 0/false/[])
///     {"type":"compact","kind":"done|failed|progress|forced","item":"…","from_seq":4,"to_seq":19,"text":"…"}
///                                                                   an item's events (from_seq, to_seq] left the request
///     {"type":"turn","seq":2,"started":…,"seconds":…,"prompt":…,"cached":…,"effort":"medium"}
///                                                                   what the turn of reply `seq` took
///
/// Secret events, and sealed ones (content of the encrypted memory folders, `Event.sealed`), keep
/// seq, ts, kind, source and flags plain and put text, window and calls into `"box":"dollbox1:…"`
/// (`SecretBox`, owner 2026-11-21); the footer of such a reply and a compaction summary over such
/// an event are boxes too. A secret event without a box is plain text that is no longer allowed:
/// opening the segment fails with its line. Every event a sealed turn appends is sealed (`Doll.sealing`),
/// and a todo line written for a sealed list is `{"type":"todo","seq":5,"box":"dollbox1:…"}`, the whole
/// line inside the box.
///
/// Her replies are stored raw (tool call text included) and re-sent byte-identical. Each line is
/// written and synced before `append` returns, so a crash loses at most the line being written; a
/// cut-off last line is dropped when the file is next opened. The whole file is read once at start
/// and kept in memory, appended to as lines are written.
public final class Timeline {
    public let file: URL
    /// The segment's id: the file name without `.jsonl` (local time it began, `yyyyMMdd-HHmmss`).
    public let segment: String
    /// Every event, oldest first, footers attached to her replies.
    public private(set) var events: [Event] = []
    /// The todo list as last recorded; idle for a new segment.
    public private(set) var todo = TodoState()
    /// The list set aside beside it (`ListAside`, 2026-11-20); nil when there is none.
    public private(set) var todoAside: ListAside?
    /// The last todo line was stored encrypted (`recordTodo`'s `sealed`).
    public private(set) var todoSealed = false
    /// Every compaction, oldest first.
    public private(set) var compactions: [Compaction] = []
    /// Every recorded turn by her reply's seq.
    public private(set) var turnStats: [Int64: TurnStats] = [:]
    private var position: [Int64: Int] = [:]
    private var handle: FileHandle?
    /// Encrypts secret and sealed events (nil: a timeline that may hold none).
    private let box: SecretBox?

    /// What one turn took, keyed by her reply's seq; for the dashboard, not the prompt.
    public struct TurnStats: Sendable, Equatable, Codable {
        public var seq: Int64
        public var started: Double
        public var seconds: Double
        public var prompt: Int
        public var cached: Int?
        public var effort: String
        /// The reply's completion tokens, `finish_reason` and reasoning characters (absent in lines
        /// written before 2026-11-16).
        public var completion: Int? = nil
        public var finish: String? = nil
        public var reasoningChars: Int? = nil
    }

    private struct Line: Decodable { var type: String }
    private struct EventLine: Codable {
        var type = "event"
        var seq: Int64
        var ts: Double
        var kind: Event.Kind
        var source: Principal
        var secret: Bool
        var voice: Bool
        var text: String
        var image: String?
        var window: String?
        var channel: Channel?
        /// Her reply's calls; present on every `doll` event since 2026-11-14.
        var calls: [StoredCall]?
        /// A secret or sealed event's `{text, window, calls}` as one `SecretBox` (2026-11-21); then
        /// `text` is empty and `window`/`calls` absent.
        var box: String?
    }
    /// What a box of an event holds.
    private struct SealedContent: Codable {
        var text: String
        var window: String?
        var calls: [StoredCall]?
    }
    /// One call as stored: `arguments` nil and `malformed` set for a call the server returned as text.
    private struct StoredCall: Codable {
        var name: String
        var arguments: String?
        var malformed: String?
    }
    private struct FooterLine: Codable {
        var type = "footer"
        var seq: Int64
        var text: String
    }
    private struct TodoLine: Codable {
        var type = "todo"
        var seq: Int64
        var phase: TodoPhase
        var current: String?
        var pending: [String]
        var startSeq: Int64
        var rounds: Int?
        var decideAt: Int?
        var deciding: Bool?
        var goOn: [String]?
        var plan: TodoPlan?
        var listRounds: Int?
        var listStart: Double?
        var listText: String?
        var estimateCheck: EstimateCheck?
        /// Absent in lines written before 2026-11-20 and when no list is set aside.
        var aside: ListAside?
    }
    /// A todo line written by a sealed turn (2026-11-21): the whole `TodoLine` as one `SecretBox`.
    private struct SealedTodoLine: Codable {
        var type = "todo"
        var seq: Int64
        var box: String
    }
    private struct BoxProbe: Codable { var box: String? }
    private struct CompactLine: Codable {
        var type = "compact"
        var kind: Compaction.Kind
        var item: String
        var fromSeq: Int64
        var toSeq: Int64
        var text: String?
    }
    private struct TurnLine: Codable {
        var type = "turn"
        var seq: Int64
        var started: Double
        var seconds: Double
        var prompt: Int
        var cached: Int?
        var effort: String
        var completion: Int?
        var finish: String?
        var reasoningChars: Int?
    }

    private static let encoder: JSONEncoder = {
        let e = JSONEncoder()
        e.keyEncodingStrategy = .convertToSnakeCase
        e.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        return e
    }()
    private static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    /// Opens the newest segment under `dir`, or starts one when there is none.
    public init(dir: URL, box: SecretBox?) throws {
        self.box = box
        let fm = FileManager.default
        try fm.createDirectory(at: dir, withIntermediateDirectories: true)
        segment = try Self.segments(dir).last ?? Self.newName(after: nil)
        file = dir.appending(path: segment + ".jsonl")
        if !fm.fileExists(atPath: file.path) {
            guard fm.createFile(atPath: file.path, contents: nil) else {
                throw StoreError(description: "cannot create \(file.path)")
            }
        }
        try load()
    }

    /// Opens the existing segment `segment` under `dir` to read it (the audit reads a past day,
    /// 2026-11-18). A segment that is not there is an error.
    public init(dir: URL, segment: String, box: SecretBox?) throws {
        self.box = box
        self.segment = segment
        file = dir.appending(path: segment + ".jsonl")
        guard FileManager.default.fileExists(atPath: file.path) else {
            throw StoreError(description: "no timeline segment \(segment) in \(dir.path)")
        }
        try load()
    }

    /// Every segment under `dir`, line by line without decrypting (boot, before the key): the first
    /// secret event stored in plain text (`<path> line <n>`), and the first encrypted line.
    public static func scan(dir: URL) throws -> (plain: String?, sealed: String?) {
        var plain: String?, sealed: String?
        guard FileManager.default.fileExists(atPath: dir.path) else { return (nil, nil) }
        for name in try segments(dir) {
            let url = dir.appending(path: name + ".jsonl")
            for (n, line) in try Data(contentsOf: url).split(separator: UInt8(ascii: "\n")).enumerated() {
                let text = String(decoding: line, as: UTF8.self)
                let boxed = text.contains("\"" + SecretBox.prefix)
                if boxed, sealed == nil { sealed = "\(url.path) line \(n + 1)" }
                if !boxed, plain == nil, text.contains("\"type\":\"event\""), text.contains("\"secret\":true") {
                    plain = "\(url.path) line \(n + 1)"
                }
            }
        }
        return (plain, sealed)
    }

    /// The local time now as `yyyyMMdd-HHmmss`; when that does not sort after `newest` (same second),
    /// `newest` with `-1` appended, which does.
    private static func newName(after newest: String?) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyyMMdd-HHmmss"
        let now = f.string(from: Date())
        guard let newest, now <= newest else { return now }
        return newest + "-1"
    }

    /// Starts a new, empty segment under `dir` (it becomes the newest, so the next `init` opens it);
    /// returns its id. A process holding the old segment open keeps writing there: call this only
    /// when none runs.
    @discardableResult
    public static func startSegment(dir: URL) throws -> String {
        let fm = FileManager.default
        try fm.createDirectory(at: dir, withIntermediateDirectories: true)
        let name = newName(after: try segments(dir).last)
        guard fm.createFile(atPath: dir.appending(path: name + ".jsonl").path, contents: nil) else {
            throw StoreError(description: "cannot create \(dir.appending(path: name + ".jsonl").path)")
        }
        return name
    }

    /// The segment ids under `dir`, oldest first. Sorted without the `.jsonl` suffix: with it,
    /// `x-1.jsonl` sorts before `x.jsonl` ('-' < '.') and a same-second new segment was not the newest.
    private static func segments(_ dir: URL) throws -> [String] {
        try FileManager.default.contentsOfDirectory(atPath: dir.path).filter { $0.hasSuffix(".jsonl") }
            .map { String($0.dropLast(".jsonl".count)) }.sorted()
    }

    deinit { try? handle?.close() }

    private func load() throws {
        var data = try Data(contentsOf: file)
        if let end = data.last, end != UInt8(ascii: "\n") {
            // A crash cut the last line short: drop it, so the next line starts on its own.
            let keep = (data.lastIndex(of: UInt8(ascii: "\n")).map { $0 + 1 }) ?? 0
            data = data.prefix(keep)
            let h = try FileHandle(forWritingTo: file)
            try h.truncate(atOffset: UInt64(keep))
            try h.close()
        }
        for (n, line) in data.split(separator: UInt8(ascii: "\n")).enumerated() {
            let raw = Data(line)
            let place = "\(file.path) line \(n + 1)"
            func bad(_ e: Error) -> StoreError { StoreError(description: "\(place): \(e)") }
            do {
                switch try Self.decoder.decode(Line.self, from: raw).type {
                case "event":
                    var l = try Self.decoder.decode(EventLine.self, from: raw)
                    if let boxed = l.box {
                        let p = try Self.decoder.decode(SealedContent.self, from: Data(try key().open(boxed, place: place).utf8))
                        (l.text, l.window, l.calls) = (p.text, p.window, p.calls)
                    } else if l.secret {
                        throw StoreError(description: "secret event \(l.seq) is stored in plain text; secret events are encrypted now and plain ones are not allowed (no conversion: remove it)")
                    }
                    if l.kind == .doll, l.calls == nil {
                        // Before 2026-11-14 her reply was stored as raw text with her calls inside it.
                        throw StoreError(description: "her reply \(l.seq) has no calls record: a timeline from before native tool calls (2026-11-14); start a new data dir")
                    }
                    take(l)
                case "footer":
                    let f = try Self.decoder.decode(FooterLine.self, from: raw)
                    guard let i = position[f.seq] else { throw StoreError(description: "footer for unknown seq \(f.seq)") }
                    events[i].footer = try opened(f.text, place: place)
                case "todo":
                    let probe = try Self.decoder.decode(BoxProbe.self, from: raw)
                    let t = try probe.box.map { try Self.decoder.decode(TodoLine.self, from: Data(try key().open($0, place: place).utf8)) }
                        ?? Self.decoder.decode(TodoLine.self, from: raw)
                    todo = Self.state(t)
                    todoAside = t.aside
                    todoSealed = probe.box != nil
                case "compact":
                    let c = try Self.decoder.decode(CompactLine.self, from: raw)
                    compactions.append(Compaction(kind: c.kind, item: c.item, fromSeq: c.fromSeq, toSeq: c.toSeq,
                                                  text: try c.text.map { try opened($0, place: place) }))
                case "turn":
                    let t = try Self.decoder.decode(TurnLine.self, from: raw)
                    turnStats[t.seq] = TurnStats(seq: t.seq, started: t.started, seconds: t.seconds, prompt: t.prompt,
                                                 cached: t.cached, effort: t.effort, completion: t.completion,
                                                 finish: t.finish, reasoningChars: t.reasoningChars)
                case let other: throw StoreError(description: "unknown record type \(other)")
                }
            } catch { throw bad(error) }
        }
    }

    /// The memory key; secret content is never written or read without it.
    private func key() throws -> SecretBox {
        guard let box else { throw StoreError(description: "the timeline holds or gets encrypted content, and no memory key was given") }
        return box
    }

    /// `text` decrypted when it is a box, else as it is.
    private func opened(_ text: String, place: String) throws -> String {
        SecretBox.isSealed(text) ? try key().open(text, place: place) : text
    }

    private func take(_ l: EventLine) {
        var e = Event(l.kind, l.source, l.text, secret: l.secret, voice: l.voice, image: l.image, window: l.window)
        e.sealed = l.box != nil && !l.secret
        e.channel = l.channel
        e.calls = (l.calls ?? []).map { c in
            if let arguments = c.arguments { ToolCall(name: c.name, arguments: arguments) }
            else { ToolCall(failed: c.name, c.malformed ?? ToolCall.unreadable) }
        }
        e.seq = l.seq
        e.ts = l.ts
        position[l.seq] = events.count
        events.append(e)
    }

    private static func state(_ l: TodoLine) -> TodoState {
        var s = TodoState()
        s.phase = l.phase
        s.current = l.current
        s.pending = l.pending
        s.startSeq = l.startSeq
        s.rounds = l.rounds ?? 0
        s.decideAt = l.decideAt ?? 0
        s.deciding = l.deciding ?? false
        s.goOn = l.goOn ?? []
        s.plan = l.plan
        s.listRounds = l.listRounds ?? 0
        s.listStart = l.listStart ?? 0
        s.listText = l.listText
        s.estimateCheck = l.estimateCheck
        return s
    }

    /// Writes one line and syncs it to disk.
    private func write<T: Encodable>(_ record: T) throws {
        if handle == nil {
            let h = try FileHandle(forWritingTo: file)
            try h.seekToEnd()
            handle = h
        }
        try handle!.write(contentsOf: try Self.encoder.encode(record) + Data([UInt8(ascii: "\n")]))
        try handle!.synchronize()
    }

    /// The newest event's seq; 0 for an empty timeline.
    public var lastSeq: Int64 { events.last?.seq ?? 0 }

    /// Appends one event and returns it with its seq. A secret or sealed event's text, window and
    /// calls are written as one box (`SecretBox`); its kind, seq, time and flags stay plain.
    @discardableResult
    public func append(_ ev: Event) throws -> Event {
        var line = EventLine(seq: lastSeq + 1, ts: ev.ts, kind: ev.kind, source: ev.principal, secret: ev.secret,
                             voice: ev.voice, text: ev.text, image: ev.image,
                             window: ev.window, channel: ev.channel,
                             calls: ev.kind == .doll ? ev.calls.map { StoredCall(name: $0.name, arguments: $0.arguments,
                                                                                  malformed: $0.arguments == nil ? $0.malformed : nil) } : nil)
        if ev.secret || ev.sealed {
            var stored = line
            let content = SealedContent(text: line.text, window: line.window, calls: line.calls)
            stored.box = try key().seal(String(decoding: try Self.encoder.encode(content), as: UTF8.self))
            (stored.text, stored.window, stored.calls) = ("", nil, nil)
            try write(stored)
            line.box = stored.box
        } else {
            try write(line)
        }
        take(line)
        return events[events.count - 1]
    }

    /// Whether any event in `(from, to]` is stored encrypted.
    private func sealedSpan(_ from: Int64, _ to: Int64) -> Bool {
        events.contains { ($0.secret || $0.sealed) && $0.seq! > from && $0.seq! <= to }
    }

    /// A footer of a secret or sealed reply is encrypted with it.
    public func attachFooter(_ seq: Int64, _ text: String) throws {
        guard let i = position[seq] else { throw StoreError(description: "no event \(seq) to attach a footer to") }
        try write(FooterLine(seq: seq, text: events[i].secret || events[i].sealed ? try key().seal(text) : text))
        events[i].footer = text
    }

    /// Records the todo list after her reply `seq` (or the program's change at the event `seq`),
    /// with the list set aside beside it (nil: none). `sealed`: the line is one box (its items may
    /// quote a secret); only `type` and `seq` stay plain.
    public func recordTodo(_ s: TodoState, aside: ListAside?, seq: Int64, sealed: Bool = false) throws {
        let line = TodoLine(seq: seq, phase: s.phase, current: s.current, pending: s.pending, startSeq: s.startSeq,
                            rounds: s.rounds, decideAt: s.decideAt, deciding: s.deciding, goOn: s.goOn,
                            plan: s.plan, listRounds: s.listRounds, listStart: s.listStart,
                            listText: s.listText, estimateCheck: s.estimateCheck, aside: aside)
        if sealed {
            try write(SealedTodoLine(seq: seq, box: try key().seal(String(decoding: try Self.encoder.encode(line), as: UTF8.self))))
        } else {
            try write(line)
        }
        todo = s
        todoAside = aside
        todoSealed = sealed
    }

    /// Records a compaction; requests render its span from then on. Its summary is encrypted when
    /// the span holds an encrypted event.
    public func compact(_ c: Compaction) throws {
        let text = try c.text.map { sealedSpan(c.fromSeq, c.toSeq) ? try key().seal($0) : $0 }
        try write(CompactLine(kind: c.kind, item: c.item, fromSeq: c.fromSeq, toSeq: c.toSeq, text: text))
        compactions.append(c)
    }

    public func recordTurn(_ t: TurnStats) throws {
        try write(TurnLine(seq: t.seq, started: t.started, seconds: t.seconds, prompt: t.prompt, cached: t.cached,
                           effort: t.effort, completion: t.completion, finish: t.finish, reasoningChars: t.reasoningChars))
        turnStats[t.seq] = t
    }
}

// SQLite -----------------------------------------------------------------------

enum Bind {
    case text(String), int(Int64), real(Double), null, blob(Data)
}

private let transient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)

struct Row {
    let stmt: OpaquePointer
    func text(_ i: Int32) -> String? {
        sqlite3_column_type(stmt, i) == SQLITE_NULL ? nil : String(cString: sqlite3_column_text(stmt, i))
    }
    func int(_ i: Int32) -> Int64? {
        sqlite3_column_type(stmt, i) == SQLITE_NULL ? nil : sqlite3_column_int64(stmt, i)
    }
    func real(_ i: Int32) -> Double { sqlite3_column_double(stmt, i) }
    func blob(_ i: Int32) -> Data {
        guard let p = sqlite3_column_blob(stmt, i) else { return Data() }
        return Data(bytes: p, count: Int(sqlite3_column_bytes(stmt, i)))
    }
}

/// One SQLite connection; only indexes live in SQLite (the memory index on disk, the today index in memory).
final class SQLite {
    private let db: OpaquePointer

    /// `path` nil: a private in-memory database, gone with the connection.
    init(path: URL?, schema: String) throws {
        if let path { try FileManager.default.createDirectory(at: path.deletingLastPathComponent(), withIntermediateDirectories: true) }
        let name = path?.path ?? ":memory:"
        var handle: OpaquePointer?
        guard sqlite3_open(name, &handle) == SQLITE_OK, let handle else {
            throw StoreError(description: "cannot open \(name)")
        }
        db = handle
        sqlite3_busy_timeout(db, 5000)   // a second connection (tests) may hold a write briefly
        guard sqlite3_exec(db, schema, nil, nil, nil) == SQLITE_OK else { throw error() }
    }

    deinit { sqlite3_close(db) }

    private func error() -> StoreError { StoreError(description: String(cString: sqlite3_errmsg(db))) }

    @discardableResult
    func query<T>(_ sql: String, _ args: [Bind] = [], _ each: (Row) -> T) throws -> [T] {
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK, let stmt else { throw error() }
        defer { sqlite3_finalize(stmt) }
        for (n, a) in args.enumerated() {
            let i = Int32(n + 1)
            switch a {
            case .text(let s): sqlite3_bind_text(stmt, i, s, -1, transient)
            case .int(let v): sqlite3_bind_int64(stmt, i, v)
            case .real(let v): sqlite3_bind_double(stmt, i, v)
            case .null: sqlite3_bind_null(stmt, i)
            case .blob(let d): sqlite3_bind_blob(stmt, i, (d as NSData).bytes, Int32(d.count), transient)
            }
        }
        var out: [T] = []
        while true {
            switch sqlite3_step(stmt) {
            case SQLITE_ROW: out.append(each(Row(stmt: stmt)))
            case SQLITE_DONE: return out
            default: throw error()
            }
        }
    }

    func exec(_ sql: String, _ args: [Bind] = []) throws {
        try query(sql, args) { _ in () }
    }

    /// Runs `body` in one transaction: all of it lands, or none.
    func transaction(_ body: () throws -> Void) throws {
        try exec("BEGIN")
        do {
            try body()
            try exec("COMMIT")
        } catch {
            try? exec("ROLLBACK")
            throw error
        }
    }
}

/// `<data_dir>/doll.db`: the memory index only (the timeline is in `Timeline`'s files). The rows of
/// `sealedFolders` go to the in-memory schema `sealed` instead (`sealedSchema`); every read covers both.
public final class Store {
    private let sql: SQLite
    public let path: URL
    /// The folder names (`Memory.sealedFolders`) whose rows never reach the disk; set by `Memory`.
    var sealedFolders: Set<String> = []

    public init(path: URL) throws {
        self.path = path
        sql = try SQLite(path: path, schema: memorySchema + "\n" + sealedSchema)
    }

    private func query<T>(_ s: String, _ args: [Bind] = [], _ each: (Row) -> T) throws -> [T] {
        try sql.query(s, args, each)
    }

    private func exec(_ s: String, _ args: [Bind]) throws { try sql.exec(s, args) }

    /// `select` (with `{s}` for the schema) over the on-disk and the in-memory tables, `UNION ALL`;
    /// `args` repeated for each.
    private func both<T>(_ select: String, _ args: [Bind], suffix: String = "", _ each: (Row) -> T) throws -> [T] {
        let s = ["main", "sealed"].map { select.replacingOccurrences(of: "{s}", with: $0) }.joined(separator: " UNION ALL ")
        return try query(s + suffix, args + args, each)
    }

    /// Rows of `folders` in the on-disk memory index (boot fails on any: `Memory.syncOnBoot`).
    func diskRows(folders: Set<String>) throws -> Int {
        guard !folders.isEmpty else { return 0 }
        let marks = folders.map { _ in "?" }.joined(separator: ", ")
        let args: [Bind] = folders.sorted().map { .text($0) }
        return try query("SELECT (SELECT count(*) FROM main.memory_index WHERE folder IN (\(marks)))"
                         + " + (SELECT count(*) FROM main.memory_fts WHERE folder IN (\(marks)))"
                         + " + (SELECT count(*) FROM main.memory_entity_refs WHERE folder IN (\(marks)))", args + args + args) {
            Int($0.int(0)!)
        }.first ?? 0
    }

    // memory index (M3a) ------------------------------------------------------
    public struct MemoryMeta {
        public let id: String
        public let path: String
        public let folder: String
        public let confidence: Double
        public let kind: String
    }

    public struct MemoryVectorRow {
        public let id: String
        public let vector: Data
    }

    /// Inserts or replaces one index entry (the markdown file was already written), its registry
    /// entity ids with it; in memory only for a folder of `sealedFolders`.
    public func putMemoryIndex(id: String, path: String, folder: String, confidence: Double,
                               ts: Double, sourceSeq: Int64, kind: String, tier: String, entities: [String],
                               text: String, vector: Data) throws {
        try deleteMemoryIndex(id: id)
        let s = sealedFolders.contains(folder) ? "sealed" : "main"
        try exec("INSERT INTO \(s).memory_index (id, path, folder, confidence, ts, source_seq, kind) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 [.text(id), .text(path), .text(folder), .real(confidence), .real(ts), .int(sourceSeq), .text(kind)])
        try exec("INSERT INTO \(s).memory_fts (text, id, folder) VALUES (?, ?, ?)", [.text(text), .text(id), .text(folder)])
        try exec("INSERT INTO \(s).memory_vectors (id, vector) VALUES (?, ?)", [.text(id), .blob(vector)])
        for entity in Set(entities) {
            try exec("INSERT INTO \(s).memory_entity_refs (id, entity, folder, tier) VALUES (?, ?, ?, ?)",
                     [.text(id), .text(entity), .text(folder), .text(tier)])
        }
    }

    /// Entries of the on-disk index (the plain folders).
    public func countMemoryIndex() throws -> Int {
        try query("SELECT count(*) FROM main.memory_index", []) { Int($0.int(0)!) }.first ?? 0
    }

    /// Removes one entry from every index table (its markdown file is already gone).
    public func deleteMemoryIndex(id: String) throws {
        for s in ["main", "sealed"] {
            for table in ["memory_index", "memory_fts", "memory_vectors", "memory_entity_refs"] {
                try exec("DELETE FROM \(s).\(table) WHERE id = ?", [.text(id)])
            }
        }
    }

    /// Empties the memory index (both schemas) and the registry index.
    public func clearMemoryIndex() throws {
        for table in ["main.memory_index", "main.memory_fts", "main.memory_vectors", "main.memory_entity_refs",
                      "sealed.memory_index", "sealed.memory_fts", "sealed.memory_vectors", "sealed.memory_entity_refs",
                      "registry_index", "registry_keys", "registry_fts", "registry_vectors"] {
            try exec("DELETE FROM \(table)", [])
        }
    }

    /// The registry entity ids of each of `ids` that has any.
    public func memoryEntities(_ ids: [String]) throws -> [String: [String]] {
        guard !ids.isEmpty else { return [:] }
        let marks = ids.map { _ in "?" }.joined(separator: ", ")
        var out: [String: [String]] = [:]
        for (id, entity) in try both("SELECT id, entity FROM {s}.memory_entity_refs WHERE id IN (\(marks))",
                                     ids.map { .text($0) }, suffix: " ORDER BY id, entity", { ($0.text(0)!, $0.text(1)!) }) {
            out[id, default: []].append(entity)
        }
        return out
    }

    /// Every memory in `folders` (and of `tiers`, when given) with any of the registry entities
    /// `entities`: id → the ones it has.
    public func memoriesWithEntities(_ entities: [String], folders: [String], tiers: [String]?) throws -> [String: [String]] {
        guard !entities.isEmpty, !folders.isEmpty else { return [:] }
        func marks(_ n: Int) -> String { Array(repeating: "?", count: n).joined(separator: ", ") }
        var sql = "SELECT id, entity FROM {s}.memory_entity_refs WHERE entity IN (\(marks(entities.count))) AND folder IN (\(marks(folders.count)))"
        var args: [Bind] = entities.map { .text($0) } + folders.map { .text($0) }
        if let tiers {
            sql += " AND tier IN (\(marks(tiers.count)))"
            args += tiers.map { .text($0) }
        }
        var out: [String: [String]] = [:]
        for (id, entity) in try both(sql, args, suffix: " ORDER BY id, entity", { ($0.text(0)!, $0.text(1)!) }) {
            out[id, default: []].append(entity)
        }
        return out
    }

    // entity registry index (2026-11-11) -------------------------------------------
    /// Inserts or replaces one registry entry's index rows: its keys (canonical name and aliases),
    /// its FTS text and its vector.
    public func putRegistryIndex(id: String, type: String, keys: [String], text: String, vector: Data) throws {
        try exec("INSERT INTO registry_index (id, type) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET type = excluded.type",
                 [.text(id), .text(type)])
        try exec("DELETE FROM registry_keys WHERE id = ?", [.text(id)])
        for key in Set(keys) {
            try exec("INSERT INTO registry_keys (key, id) VALUES (?, ?)", [.text(key), .text(id)])
        }
        try exec("DELETE FROM registry_fts WHERE id = ?", [.text(id)])
        try exec("INSERT INTO registry_fts (text, id) VALUES (?, ?)", [.text(text), .text(id)])
        try exec("INSERT INTO registry_vectors (id, vector) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET vector = excluded.vector",
                 [.text(id), .blob(vector)])
    }

    /// Removes one registry entry from every registry index table (its file is gone).
    public func deleteRegistryIndex(id: String) throws {
        for table in ["registry_index", "registry_keys", "registry_fts", "registry_vectors"] {
            try exec("DELETE FROM \(table) WHERE id = ?", [.text(id)])
        }
    }

    public func countRegistryIndex() throws -> Int {
        try query("SELECT count(*) FROM registry_index", []) { Int($0.int(0)!) }.first ?? 0
    }

    /// The ids of the entries with `key` as their canonical name's or an alias's key, by id.
    public func registryByKey(_ key: String) throws -> [String] {
        try query("SELECT id FROM registry_keys WHERE key = ? ORDER BY id", [.text(key)]) { $0.text(0)! }
    }

    /// Registry FTS5 hits by BM25 rank (lower is better), then id.
    public func registryFTS(match: String) throws -> [(id: String, rank: Double)] {
        try query("SELECT id, rank FROM registry_fts WHERE registry_fts MATCH ? ORDER BY rank, id", [.text(match)]) {
            ($0.text(0)!, $0.real(1))
        }
    }

    public func registryVectors() throws -> [MemoryVectorRow] {
        try query("SELECT id, vector FROM registry_vectors", []) { MemoryVectorRow(id: $0.text(0)!, vector: $0.blob(1)) }
    }

    /// The `Memory.indexVersion` the index was built with; nil for an index from before versions.
    public func memoryIndexVersion() throws -> Int? {
        try query("SELECT version FROM memory_index_version WHERE one = 1", []) { Int($0.int(0)!) }.first
    }

    public func setMemoryIndexVersion(_ v: Int) throws {
        try exec("INSERT INTO memory_index_version (one, version) VALUES (1, ?) ON CONFLICT(one) DO UPDATE SET version = excluded.version",
                 [.int(Int64(v))])
    }

    /// FTS5 hits ordered by BM25 rank (lower is better), from the given folders only; the id/folder
    /// columns are UNINDEXED so the MATCH only sees text. The id tiebreak matters: equal BM25 scores
    /// otherwise order arbitrarily, and RRF ranks would change between a run and an index rebuild.
    /// `limit` nil: every hit.
    public func memoryFTS(match: String, folders: [String], limit: Int?) throws -> [(id: String, rank: Double)] {
        let marks = folders.map { _ in "?" }.joined(separator: ", ")
        return try both("SELECT id, rank FROM {s}.memory_fts WHERE memory_fts MATCH ? AND folder IN (\(marks))",
                        [.text(match)] + folders.map { .text($0) }, suffix: " ORDER BY rank, id LIMIT \(limit ?? -1)") {
            ($0.text(0)!, $0.real(1))
        }
    }

    /// Memories in the given folders whose text contains one of `terms` (case-insensitive for
    /// ASCII), in id order. For query words shorter than a trigram (two characters: 過敏, 花生), which
    /// the FTS5 trigram index cannot match: a LIKE under 3 characters is a linear scan of the FTS
    /// table's text (2026-11-15). `terms` hold only letters, digits and CJK (no LIKE wildcards).
    public func memoryContaining(_ terms: [String], folders: [String]) throws -> [String] {
        guard !terms.isEmpty else { return [] }
        let marks = folders.map { _ in "?" }.joined(separator: ", ")
        let likes = terms.map { _ in "text LIKE ?" }.joined(separator: " OR ")
        return try both("SELECT id FROM {s}.memory_fts WHERE folder IN (\(marks)) AND (\(likes))",
                        folders.map { .text($0) } + terms.map { .text("%\($0)%") }, suffix: " ORDER BY id") { $0.text(0)! }
    }

    public func memoryVectors(folders: [String]) throws -> [MemoryVectorRow] {
        let marks = folders.map { _ in "?" }.joined(separator: ", ")
        return try both("""
            SELECT m.id, m.vector FROM {s}.memory_vectors m JOIN {s}.memory_index m2 ON m2.id = m.id WHERE m2.folder IN (\(marks))
            """, folders.map { .text($0) }) {
            MemoryVectorRow(id: $0.text(0)!, vector: $0.blob(1))
        }
    }

    public func memoryMeta(_ id: String) throws -> MemoryMeta? {
        try both("SELECT id, path, folder, confidence, kind FROM {s}.memory_index WHERE id = ?", [.text(id)]) {
            MemoryMeta(id: $0.text(0)!, path: $0.text(1)!, folder: $0.text(2)!,
                       confidence: $0.real(3), kind: $0.text(4)!)
        }.first
    }
}
