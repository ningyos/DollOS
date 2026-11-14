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
