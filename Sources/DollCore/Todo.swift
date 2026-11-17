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
    /// The round count at which the next decision point comes (`[computer] decide_after_rounds`, then
    /// `decide_every_rounds` after each choice); 0 = none set (an item from an older timeline).
    public var decideAt = 0
    /// The decision point came and she has not chosen yet: the computer tools are refused.
    public var deciding = false
    /// Her `todo.continue` reasons for this item, oldest first: 「第 N 回合：還缺…；最卡的是…；跟之前不同的是…」.
    public var goOn: [String] = []
    /// Her plan for the list (2026-11-17, `TodoPlan`): set by the `todo.set` that started it, kept
    /// while its items run; nil for a list from an older timeline.
    public var plan: TodoPlan?
    /// Rounds of the list's items before the current one (the current one's are `rounds`).
    public var listRounds = 0
    /// When the `todo.set` that started the list was written (its reply's time).
    public var listStart = 0.0

    public init() {}

    /// Rounds of the whole list so far: what her estimate is compared with.
    public var totalRounds: Int { listRounds + rounds }
}

/// What she hands in with a `todo.set` that starts a list (2026-11-17, owner: the ten ways of
/// thinking in every doll; hook 1 = first principles, limits, order-of-magnitude estimate). The
/// program checks that each is written and is not filler (`Doll.planRefusal`); it cannot judge
/// whether her facts are right.
public struct TodoPlan: Sendable, Equatable, Codable {
    /// The items the list started with.
    public var items: [String]
    /// What is established: seen with her own eyes, or said by the owner.
    public var facts: String
    /// What she only assumes and has not checked.
    public var assumptions: String
    /// What she must not touch, what cannot be done.
    public var limits: String
    /// Rounds the whole list will take (one round = one reply of hers while an item is 進行中).
    public var estimate: Int
    /// What done looks like and how she will check it.
    public var check: String

    public init(items: [String], facts: String, assumptions: String, limits: String, estimate: Int, check: String) {
        self.items = items
        self.facts = facts
        self.assumptions = assumptions
        self.limits = limits
        self.estimate = estimate
        self.check = check
    }
}

/// One finished list, estimate against what it took (2026-11-17, hook 2 = feedback loop): written
/// when the list ends (its last `todo.done`, or a `todo.fail`), to `<data_dir>/estimates.json`.
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
}

/// The five `todo.set` fields as she gave them (trimmed; nil = not given), before `Doll.planRefusal`.
struct PlanInput: Sendable, Equatable {
    var facts: String?
    var assumptions: String?
    var limits: String?
    var estimate: Int?
    var check: String?
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
