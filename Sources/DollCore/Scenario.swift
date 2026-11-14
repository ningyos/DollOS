import Foundation

/// A scenario (spec 3.1, 2026-11-09), like Python's `with`: a bounded piece of work she enters with
/// its enter tool and leaves with `outcome(success, summary)`, one at a time (no nesting). While it is
/// open its tools work, she still sees the main context, and its turns follow it in every request in
/// full. Once she leaves, the request keeps the main context up to and including her reply that
/// entered it, the enter call's result becomes her summary (`Scenario.result`), and her speaking calls in
/// the reply that left follow as her message; the turns in between stay in the timeline only
/// (`Doll.context`). The computer is the one kind so far.
public enum ScenarioKind: String, Sendable, CaseIterable {
    case computer

    /// The top-level tool that enters it; listed in the system prompt.
    var enterTool: String {
        switch self {
        case .computer: "use_computer"
        }
    }

    /// The menu's name in the 【狀況】 footer and in errors.
    var menuName: String {
        switch self {
        case .computer: "電腦"
        }
    }

    /// Its own tools, usable only while it is open (declared in `tools` from the start); `outcome` is added after them.
    func tools(_ c: ComputerConfig, _ f: FilesConfig) -> [Tools.Spec] {
        switch self {
        case .computer: Tools.computerSpecs(c, f)
        }
    }
}

/// The open scenario.
struct Scenario {
    let kind: ScenarioKind
    /// Her reply that called the enter tool: the main context runs up to and including it.
    let startSeq: Int64
    /// The enter call's result event; set once it is stored.
    var enterResultSeq: Int64?
    /// Its tools, `outcome` last; they work only while it is open.
    let tools: [Tools.Spec]
    /// What she said she should see when done.
    let expect: String
    /// The owner's last line when she entered.
    let task: String
    /// Her actions that went through, one short line each (`Doll.step`); no answer text. Kinds
    /// without actions leave it empty.
    var steps: [String] = []
    /// The front apps her computer results showed while it was open, first seen first: an
    /// experience's entities (2026-11-11).
    var apps: [AppIdentity] = []

    /// What the enter call's result says once she has left: how it ended, then her summary, as a
    /// function's return value. Fixed bytes, so every later request re-sends it identically.
    static func result(success: Bool, summary: String) -> String {
        (success ? "成功。" : "沒達成。") + "你自己記下的經過：\n" + summary
    }
}

/// A scenario she left with `outcome`, as stored: the span of the timeline its summary replaces.
public struct ClosedScenario: Sendable, Equatable {
    public var kind: ScenarioKind
    /// Her reply that entered it.
    public var startSeq: Int64
    /// The enter call's result: rendered as `Scenario.result` instead of what it said.
    public var enterResultSeq: Int64
    /// Her reply that called `outcome`; its speaking calls stay.
    public var endSeq: Int64
    /// The last event of the turn that left it (the exit reply's results, anything the program
    /// appended that turn). Events after `enterResultSeq` up to here are not rendered, except owner lines.
    public var lastSeq: Int64
    public var success: Bool
    public var summary: String

    public init(kind: ScenarioKind, startSeq: Int64, enterResultSeq: Int64, endSeq: Int64, lastSeq: Int64,
                success: Bool, summary: String) {
        self.kind = kind
        self.startSeq = startSeq
        self.enterResultSeq = enterResultSeq
        self.endSeq = endSeq
        self.lastSeq = lastSeq
        self.success = success
        self.summary = summary
    }
}
