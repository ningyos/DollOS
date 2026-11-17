import Foundation

/// Her todo list (2026-11-14, owner): todo = goals. Before doing anything she lists what done looks
/// like (`todo.set`; a small thing is one item); one item at a time, no nesting. The list is part of
/// the loop: its phase decides whether she gets the next turn, whether the computer tools run, and
/// when the program compacts an item's intermediate actions (`Compaction`).
///
///     待命中 idle     no item; turns only on events (and on her own tool results); computer tools refused
///     進行中 doing    one item; computer tools run; after every reply she gets the next turn
///     等待中 waiting  the item waits for the owner or a watch (`todo.wait`, `watch`); no turns until an event
///     完成   done     her `todo.done`: the item is compacted; the next item → 進行中, none → 待命中
///     失敗   failed   her `todo.fail` (only when sure): compacted; computer tools refused until she
///                     rewrites the rest (`todo.set`), or ends the list (`todo.set` with no items)
public enum TodoPhase: String, Sendable, Codable, CaseIterable {
    case idle, doing, waiting, done, failed

    /// The name she sees in 【狀況】 and in tool results.
    public var label: String {
        switch self {
        case .idle: "待命中"
        case .doing: "進行中"
        case .waiting: "等待中"
        case .done: "完成"
        case .failed: "失敗"
        }
    }
}

/// Where the list stands; the timeline stores it after every change (`Timeline.recordTodo`), so a
/// new process continues it. `done` is never stored: it moves on at once.
public struct TodoState: Sendable, Equatable, Codable {
    public var phase: TodoPhase = .idle
    /// The item in progress, waiting, or the one that failed; nil while idle.
    public var current: String?
    /// The items after it, in order.
    public var pending: [String] = []
    /// The current item's span starts after this seq: everything after it, up to its end, is what
    /// its compaction replaces.
    public var startSeq: Int64 = 0
    /// Rounds the current item has been 進行中 (2026-11-16, design C): one round = one model reply
    /// written while this item was 進行中 (an interrupted call that is rerun counts once). A new item
    /// starts at 0. Stored with the list, so a restart or a compaction keeps it.
    public var rounds = 0
    /// The list round count (`totalRounds`) at which the next decision point comes (`[computer]
    /// decide_after_rounds` from the list's `todo.set`, then `decide_every_rounds` after each choice or
    /// finished item; 2026-11-17: counted over the whole list, not per item); 0 = none set (an older timeline).
    public var decideAt = 0
    /// The decision point came and she has not chosen yet: the computer tools are refused.
    public var deciding = false
    /// Her `todo.continue` reasons for this list, oldest first: 「第 N 回合：還缺…；最卡的是…；跟之前不同的是…」 (N = list round).
    public var goOn: [String] = []
    /// Her plan for the list (2026-11-17, `TodoPlan`): set by the `todo.set` that started it, kept
    /// while its items run; nil for a list from an older timeline.
    public var plan: TodoPlan?
    /// Rounds of the list's items before the current one (the current one's are `rounds`).
    public var listRounds = 0
    /// When the `todo.set` that started the list was written (its reply's time).
    public var listStart = 0.0
    /// What later lists compare with this one (`EstimateRecord.text`), and the estimate check made
    /// at its `todo.set` (2026-11-18); nil for a list from an older timeline or with nothing alike.
    public var listText: String?
    public var estimateCheck: EstimateCheck?

    public init() {}

    /// Rounds of the whole list so far: what her estimate is compared with.
    public var totalRounds: Int { listRounds + rounds }
}

/// What she hands in with a `todo.set` that starts a list (2026-11-17, owner: the ten ways of
/// thinking in every doll; hook 1 = first principles, limits, order-of-magnitude estimate). The
/// program checks that each is written and is not filler, and that each fact quotes something she
/// read or was told (`Doll.planRefusal`); it cannot judge whether a fact follows from its quote.
public struct TodoPlan: Sendable, Equatable, Codable {
    /// The items the list started with.
    public var items: [String]
    /// What is established, each with the text it rests on (2026-11-17, owner: 第一性原理, a fact
    /// needs where she saw it).
    public var facts: [PlanFact]
    /// What she only assumes and has not checked.
    public var assumptions: String
    /// What she must not touch, what cannot be done.
    public var limits: String
    /// Rounds the whole list will take (one round = one reply of hers while an item is 進行中).
    public var estimate: Int
    /// What done looks like and how she will check it.
    public var check: String

    public init(items: [String], facts: [PlanFact], assumptions: String, limits: String, estimate: Int, check: String) {
        self.items = items
        self.facts = facts
        self.assumptions = assumptions
        self.limits = limits
        self.estimate = estimate
        self.check = check
    }

    enum CodingKeys: String, CodingKey { case items, facts, assumptions, limits, estimate, check }

    /// A plan stored before facts had sources (2026-11-17 morning: facts was one string) reads as one
    /// fact without a source.
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        items = try c.decode([String].self, forKey: .items)
        if let old = try? c.decode(String.self, forKey: .facts) {
            facts = [PlanFact(fact: old, quote: "", source: "")]
        } else {
            facts = try c.decode([PlanFact].self, forKey: .facts)
        }
        assumptions = try c.decode(String.self, forKey: .assumptions)
        limits = try c.decode(String.self, forKey: .limits)
        estimate = try c.decode(Int.self, forKey: .estimate)
        check = try c.decode(String.self, forKey: .check)
    }
}

/// One fact of her plan: what she states, the text she copied for it, and where the program found
/// that text (「主人說的」, 「工具的結果」, 「最新讀到的視窗」, 「檔案 <path>」, 「【狀況】」, 「行事曆」).
/// Quote and source are empty only for a plan stored before facts had sources.
public struct PlanFact: Sendable, Equatable, Codable {
    public var fact: String
    public var quote: String
    public var source: String

    public init(fact: String, quote: String, source: String) {
        self.fact = fact
        self.quote = quote
        self.source = source
    }
}

/// One finished list, estimate against what it took (2026-11-17, hook 2 = feedback loop): written
/// when the list ends (its last `todo.done`, or a `todo.fail`), to `<data_dir>/estimates.json`.
/// A record written before 2026-11-18 has no `text` and is never compared (`Doll.estimateCheck`).
public struct EstimateRecord: Sendable, Equatable, Codable {
    /// When the list ended.
    public var ts: Double
    public var plan: TodoPlan
    /// Rounds of the whole list while an item was 進行中, and seconds from its `todo.set` to its end.
    public var rounds: Int
    public var seconds: Int
    /// 完成 or 失敗 (`TodoPhase.label`).
    public var outcome: String
    /// A failed list: her root cause and the text she quoted for it (hook 4).
    public var cause: String?
    public var evidence: String?
    /// What later lists are compared by (2026-11-18): the line the list answered, else her items
    /// one per line (`Doll.listText`). Its vector is computed again when needed, never stored.
    public var text: String?
    /// What the program compared her estimate with at `todo.set`; nil when no past list was like it.
    public var check: EstimateCheck?
}

/// The estimate check of one list (2026-11-18, owner: the program compares): the like past lists,
/// the median of what they took, and what happened.
public struct EstimateCheck: Sendable, Equatable, Codable {
    /// The like past lists (oldest first): her items then, her estimate, the rounds they took.
    public struct Past: Sendable, Equatable, Codable {
        public var items: [String]
        public var estimate: Int
        public var rounds: Int
    }
    public var past: [Past]
    /// The median of `past` rounds, and the estimates that count as close to it.
    public var reference: Int
    public var low: Int
    public var high: Int
    /// Her estimate the program refused (nil: her first estimate was close).
    public var refused: Int?
    /// What she wrote, after the refusal, for keeping an estimate outside low...high.
    public var reason: String?
}

/// The five `todo.set` fields as she gave them (trimmed; nil = not given), before `Doll.planRefusal`.
struct PlanInput: Sendable, Equatable {
    /// Each fact as she wrote it: `{"fact": …, "quote": …}`; a missing key is nil.
    struct Fact: Sendable, Equatable, Decodable {
        var fact: String?
        var quote: String?
    }
    var facts: [Fact]?
    var assumptions: String?
    var limits: String?
    var estimate: Int?
    var check: String?
    /// Why this list will take a number of rounds the program called far from what like lists took
    /// (2026-11-18); counted only on the `todo.set` after that refusal.
    var estimateReason: String? = nil
}

public struct EstimateFile: Sendable, Equatable, Codable {
    public var records: [EstimateRecord] = []
    public init() {}
}

/// One compaction, as stored (`Timeline.compact`): events in (`fromSeq`, `toSeq`] leave the request
/// (`Doll.context`). Owner and developer lines stay verbatim; her replies keep only the calls that
/// speak to someone and her todo calls (with their results), without her thought or footer; tool
/// results of other calls, screenshots, watch wakes and the program's work notices go. A `forced`
/// one adds `text` (the program's block with the action list) after the span. Every compaction of
/// an item starts at the item's start, so the widest one covering an event decides how it renders.
public struct Compaction: Sendable, Equatable, Codable {
    public enum Kind: String, Sendable, Codable {
        /// Her `todo.done` / `todo.fail` ended the item.
        case done, failed
        /// Her `todo.progress` near the context limit; the item stays 進行中.
        case progress
        /// The program's, after she was warned `force_after_turns` times; the last turns stay.
        case forced
        /// The program's at the context limit (2026-11-15): everything from `fromSeq` to `toSeq`,
        /// conversation included, leaves the request; `text` is the summary block that takes its place.
        case talk
    }
    public var kind: Kind
    /// The item's text.
    public var item: String
    public var fromSeq: Int64
    public var toSeq: Int64
    /// The program's block after the span (forced and talk).
    public var text: String?

    public init(kind: Kind, item: String, fromSeq: Int64, toSeq: Int64, text: String? = nil) {
        self.kind = kind
        self.item = item
        self.fromSeq = fromSeq
        self.toSeq = toSeq
        self.text = text
    }
}
