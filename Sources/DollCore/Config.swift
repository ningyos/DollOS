import Foundation

/// Every behavior number lives here, loaded from TOML. Unknown keys are errors.
public struct LlmConfig: Sendable {
    public var baseURL: String
    public var model: String
    /// `reasoning_effort` for a turn with nothing new in it: no owner line, no tool result, no watch
    /// wake. The loop takes no such turn today; this is what one would use.
    public var defaultEffort = "none"
    /// `reasoning_effort` for a turn that is part of a task: one whose drained events include a new
    /// owner line (text or voice) or a watch wake, or that answers her own tool results. Her `think`
    /// for that call still wins.
    public var taskTurnEffort = "medium"
    public var timeoutS = 300.0
    public var maxTokens = 4096
}

public struct CoreConfig: Sendable {
    public var dataDir = "data"
    public var pack = "character_packs/doll/doll.toml"
    /// Event sources from highest to lowest priority when the queue is drained.
    public var priority = ["owner_voice", "owner_text", "developer_text", "calendar", "watch", "wander"]
    /// MLX's buffer cache limit for the whole process (2026-11-13, M6): the embedding model and her
    /// voice share one MLX runtime, so one limit covers both. Set before either loads.
    public var mlxCacheLimitMB = 256
    /// Her DollOS documents (2026-11-14; on this Mac since 2026-11-16): she reads them in place with
    /// `read_file`, at this folder's absolute path; a relative path is under the config's folder.
    public var docs = "dolldocs"
}

/// The persistent log and the boot report (2026-11-18, `RunLog.swift`, `BootReport.swift`).
public struct LogConfig: Sendable {
    /// `dollcore.log` is rotated to `dollcore.1.log` past this size; at most two files.
    public var maxMB = 10.0
    /// The boot notice lists at most this many kinds of WARN/ERROR lines, then how many it left out.
    public var reportKinds = 8
    /// A kind's verbatim example line is cut after this many characters in the boot notice.
    public var exampleChars = 200
    /// WARN lines (all kinds together) from this count up give her a turn; fewer only ride on the notice.
    public var turnWarns = 5
}

/// How full her context may get and what happens near the limit (2026-11-14, owner).
public struct ContextConfig: Sendable {
    /// The context figure 【狀況】 measures against, in tokens.
    public var limit = 60_000
    /// At this share of `limit` 【狀況】 warns (one line, no earlier one).
    public var warnRatio = 0.85
    /// Turns that showed the warning before the program compacts the item itself.
    public var forceAfterTurns = 3
    /// Her latest turns a forced compaction leaves intact.
    public var forceKeepTurns = 2
    /// At `limit` the program collapses the oldest conversation into a summary written by a separate
    /// request (owner, 2026-11-15): this share of what is still in the request, oldest first.
    public var talkShare = 0.5
    /// `reasoning_effort` of that summary request.
    public var talkEffort = "medium"
}

/// Her computer is this Mac (2026-11-16): the owner's apps, driven in the background through
/// `cua-driver` (`Computer.swift`).
public struct ComputerConfig: Sendable {
    /// `false` (2026-11-19, owner: an unattended trial runs without them): no cua-driver at boot, no
    /// computer or file tools, no saved tools, and nothing in her prompt about them.
    public var enabled = true
    /// The cua-driver CLI (`/Applications/CuaDriver.app/Contents/MacOS/cua-driver`).
    public var driver = "/Applications/CuaDriver.app/Contents/MacOS/cua-driver"
    /// At boot, how long a daemon this process started may take to answer.
    public var daemonStartS = 10.0
    /// A read walks at most this many accessibility nodes (the driver's own default).
    public var maxElements = 250
    /// A window screenshot's long edge, in pixels, at most.
    public var screenshotMaxDimension = 1024
    /// Rough prompt tokens of one screenshot, for the footer's context figure only.
    public var screenshotTokens = 600
    /// After an action, before the window is read again.
    public var settleS = 0.4
    /// Window reads that keep their screenshot and list before the older ones are put away at once
    /// (`Doll.fullReads`, 2026-11-17): the request stays append-only for this many reads.
    public var readBatch = 4
    /// At most this many steps in one `act` sequence (2026-11-17, sequences): the driver allows 64
    /// and one batch at most 60 s; a step is about 0.5 s plus up to 3 s for a named target to
    /// appear, and a longer run done without looking is more likely to go off track unseen.
    public var sequenceMaxSteps = 20
    /// Her cursor hides after this many idle milliseconds; 0 never hides it (2026-11-17, owner: he
    /// wants to see where she is at all times; the driver's default 15000 hid it between most of her
    /// rounds, which take 5–80 s). The driver clamps to 0...60000.
    public var cursorIdleHideMs = 0
    /// After launching an app or choosing a menu item, how many times the app's windows are listed
    /// before saying it has none, and how long between two.
    public var windowWaitTries = 5
    public var windowWaitS = 0.6
    /// Owner process names (`list_windows` app_name) of the system's authentication dialogs.
    public var authProcesses = ["SecurityAgent", "coreautha", "LocalAuthenticationRemoteService", "AuthenticationServicesAgent"]
    /// After this many rounds of one todo list (all its items, from its `todo.set`; 2026-11-17, was per
    /// item), she must choose (go on with a reason, ask the owner, or fail) before another computer
    /// or file call runs (2026-11-16, design C).
    public var decideAfterRounds = 15
    /// After each choice to go on (or ask), the next decision point comes this many rounds later.
    public var decideEveryRounds = 10
    /// Her written fields (the plan at `todo.set`, the stuck step and the difference at
    /// `todo.continue`, the root cause at `todo.fail`; 2026-11-17) need at least this many letters or
    /// digits, punctuation and spaces not counted: 「沒有」「無」「同上」「N/A」 are refused.
    public var planMinChars = 4
    /// At most this many facts in a plan (2026-11-17, facts need a source): each is a statement and
    /// a copied quote, about 30–60 tokens in her call; the morning runs wrote 1–3 facts per plan.
    public var planMaxFacts = 5
    /// The estimate check (2026-11-18, m7-mac estcheck): a past list is like the new one when the
    /// cosine of their texts (the line the list answers, else her items; digits masked) is at least
    /// this. Measured over 109 finished lists, 7 kinds: different kinds at most 0.847 (multiply vs
    /// multiply-then-read-n.txt), the same kind at least 0.94 except the saved-tool phrasing (0.85).
    public var estimateSimilarity = 0.90
    /// The reference is the median of the actual rounds of at most this many most recent like lists.
    public var estimateSimilarMax = 3
    /// Her estimate is far from the reference when the larger is more than this many times the
    /// smaller and they differ by at least `estimateFarRounds`: 2 vs 7 is far, 2 vs 3 and 1 vs 3 are not.
    public var estimateFarRatio = 2.0
    public var estimateFarRounds = 3
    /// Her own tools (2026-11-17, `OwnTools.swift`): at most this many (owner decision 4).
    public var ownToolsMax = 30
    /// A tool is offered in the 【相關記憶】 when the cosine of the owner's line (and front app) to
    /// the tool's name, `when` and the owner's line it was saved from is at least this.
    public var ownToolOfferSimilarity = 0.45
    /// At most this many tools offered at once (2026-11-18: 1; the second best was never the right one).
    public var ownToolOfferMax = 1
    /// A tool that failed this many runs in a row is no longer offered or run (owner decision 3); it stays.
    public var ownToolRetireFailures = 3
}

/// Her file tools (2026-11-09; on this Mac since 2026-11-16): read_file, write_file, edit_file on text files.
public struct FilesConfig: Sendable {
    /// read_file returns at most this many lines at once; the rest is a count she reads with from/to.
    public var maxReadLines = 500
}

/// The owner's read-only web page, served by this process (2026-11-09): her vision, memory and
/// actions live, plus interrupt and pause/resume.
public struct DashboardConfig: Sendable {
    public var enabled = true
    public var port = 8790
    /// The address the page listens on. The page shows the owner's secrets and her passwords:
    /// loopback only unless the owner chooses otherwise.
    public var bind = "127.0.0.1"
    /// The timeline endpoint returns this many of the newest items (a turn, an owner line, a watch
    /// wake, a program event); the timeline file keeps every one.
    public var timelineItems = 200
    /// The boot reports panel shows this many of the newest (`<data_dir>/boot-reports/` keeps every one).
    public var bootReports = 10
}

/// Her calendar (2026-11-15, `Calendar.swift`).
public struct CalendarConfig: Sendable {
    /// 【狀況】 lists at most this many of today's items, then says how many more: it is re-sent
    /// every turn, so a long day must not grow it without bound; 5 covers an ordinary day.
    public var statusItems = 5
    /// `calendar.list` refuses a longer span: its result stays in her context, and a month is the
    /// longest span asked for in practice ("這個月").
    public var listMaxDays = 31
}

/// System maintenance (2026-11-16, `Maintenance.swift`): once a day the program tidies her memory
/// and writes the day's summary; she takes no turns meanwhile.
public struct MaintenanceConfig: Sendable {
    /// Off: no 「系統維護」 item, no maintenance, no day line in 【狀況】 (runners and tests that are
    /// not about a day's cycle). Her own process keeps it on.
    public var enabled = true
    /// The 「系統維護」 item's time when her calendar has none: 03:00, after the owner's usual late
    /// evening (he talks with her past midnight) and long before his morning.
    public var time = "03:00"
    /// Due while she is busy (a todo item in progress or waiting) this long: it starts when her
    /// current turn ends anyway, the todo kept; she resumes it after. 3: an ordinary task at night
    /// takes well under an hour; three hours busy means it would otherwise never start that day.
    public var busyLimitHours = 3.0
    /// Interrupted by the owner or the developer: it runs again once nobody has spoken to her for
    /// this long and she is idle (or, failing that, at the next day's time). 30: a short talk at
    /// night is over by then, and the day is not left unconsolidated until tomorrow.
    public var retryIdleMin = 30
    /// She is told before it starts (owner, 2026-11-16): at the item's time she gets a turn with the
    /// notice, and it starts this many minutes later (when she is idle). 10: enough to finish a
    /// sentence, tell someone, or move today's maintenance; short enough that 03:00 still means 03:00.
    public var noticeMin = 10
}

/// When the owner is usually asleep (2026-11-16, owner's option A: she is told, nothing is blocked;
/// `Calendar.swift`, `Doll.ownerSleepLine`). A daily 「主人睡覺」 span on her calendar; inside it
/// 【狀況】, a wandering and the maintenance notice say he should be asleep. No turn at its start or end.
public struct OwnerSleepConfig: Sendable {
    /// Off: no 「主人睡覺」 item and no line (runners and tests that are not about it).
    public var enabled = true
    /// The item's span when her calendar has none. 00:00–08:00: the owner's own figure (2026-11-16);
    /// he talks with her until about midnight on late evenings, and he moves the item (for good or
    /// for one day) when his night differs. An end not after the start runs over midnight
    /// (23:00–07:00 ends at 07:00 the next day).
    public var start = "00:00"
    public var end = "08:00"
}

/// Her concerns and four signals (m6-drives, 2026-11-15, `Signals.swift`, `Drives.swift`). Values
/// are 0…1. The reasons for each number are in `dollcore.example.toml` and
/// experiments/m6-drives/week.txt (the simulated week).
public struct DrivesConfig: Sendable, Equatable {
    /// Off: no concern tools, no signals, no wandering, association or felt line (runners and tests
    /// that are not about them). Her own process keeps it on.
    public var enabled = true
    /// Which out-of-range signal surfaces first when several are pending.
    public var order: [Signal] = [.stress, .closeness, .drive]
    /// 動力 (idle-drive, 2026-11-20): its rest is above `driveHigh`, so left alone it climbs; an
    /// episode of her own things sets it back to `driveReset`. Reset → high takes
    /// `driveHalfLifeH · log2((rest − reset) / (rest − high))` = 0.63 h · log2(3) ≈ 1.0 h.
    public var driveRest = 1.0, driveHalfLifeH = 0.63, driveHigh = 0.8, driveReset = 0.4
    public var stressRest = 0.1, stressHalfLifeH = 4.0, stressHigh = 0.5
    public var closenessRest = 0.0, closenessHalfLifeH = 4.0, closenessLow = 0.2, closenessStart = 0.6
    public var itemDoneDrive = 0.1
    public var itemFailedStress = 0.25
    public var toolErrorsStress = 0.05
    public var praiseDrive = 0.2, praiseCloseness = 0.15, praiseStress = -0.15
    public var correctionStress = 0.5
    public var ownerTalkCloseness = 0.1
    public var deadlineStress = 0.15
    public var concernDoneDrive = 0.1
    /// A concern with a time this close raises 壓力 once (`deadline`).
    public var nearHours = 3.0
    /// A timed calendar item this close becomes a program concern.
    public var calendarConcernHours = 24.0
    /// Signal changes older than this leave `signals.json`; ended concerns too.
    public var logDays = 7.0
    /// Most concerns open at once (her `concern.note` refused above it).
    public var maxOpen = 20
    /// Most concerns the maintenance notes from one day's conversation (`[drives] maintenance_concerns_max`).
    public var maintenanceConcernsMax = 3
    /// The cosine at or above which an open concern is attached to the turn it relates to
    /// (`[drives] association_similarity`; it was `[select] similarity` until 2026-11-16).
    public var associationSimilarity = 0.42

    public init() {}

    public func spec(_ s: Signal) -> SignalSpec {
        switch s {
        case .drive: SignalSpec(rest: driveRest, halfLifeH: driveHalfLifeH, edge: driveHigh)
        case .stress: SignalSpec(rest: stressRest, halfLifeH: stressHalfLifeH, edge: stressHigh)
        case .closeness: SignalSpec(rest: closenessRest, halfLifeH: closenessHalfLifeH, edge: closenessLow)
        }
    }

    /// What one event moves.
    public func deltas(_ e: SignalEvent) -> [(Signal, Double)] {
        switch e {
        case .itemDone: [(.drive, itemDoneDrive)]
        case .itemFailed: [(.stress, itemFailedStress)]
        case .toolErrors: [(.stress, toolErrorsStress)]
        case .praise: [(.drive, praiseDrive), (.closeness, praiseCloseness), (.stress, praiseStress)]
        case .correction: [(.stress, correctionStress)]
        case .ownerTalk: [(.closeness, ownerTalkCloseness)]
        case .deadline: [(.stress, deadlineStress)]
        case .concernDone: [(.drive, concernDoneDrive)]
        }
    }
}

/// Her own direction (m6-drives phase 2, 2026-11-16, `Goals.swift`): the ideal self, the goal tree,
/// the record of what she did, curiosity. On with `[drives] enabled` (they feed 動力 and the
/// concerns). The reasons for each number are in `dollcore.example.toml`.
public struct GoalsConfig: Sendable, Equatable {
    public var goalsOpenMax = 3
    public var subgoalsOpenMax = 3
    public var stepsOpenMax = 6
    public var textMaxChars = 60
    /// 動力 for finishing, full amount (the owner's good confirms it): a step, a sub-goal, a goal of
    /// the tree; a todo list that is not tree work (what the owner asked).
    public var stepDrive = 0.05, subgoalDrive = 0.15, goalDrive = 0.3, listDrive = 0.1
    /// The share given at once on completion; the rest when the evaluation is good.
    public var nowShare = 0.5
    /// `owner.feedback` without a record number lands on the newest record this recent.
    public var feedbackHours = 12.0
    public var idealMaxChars = 600
    /// The maintenance proposes a revision at most this often.
    public var reviseMinDays = 3.0
    /// Off: no `review` maintenance task (runners and tests not about it).
    public var review = true
    /// Characters of the day the maintenance's review reads (the newest).
    public var reviewChars = 6000
    /// No new thing for this long: a wandering toward something not on the tree.
    public var curiosityHours = 48.0
    public var noveltyDrive = 0.08
    /// At most one 動力 rise for new things in this span.
    public var noveltyGapMin = 30.0
    /// What she saw is remembered this long for "new".
    public var seenDays = 90.0
    /// Her 「現在做不了，缺 X」 on a node (`goal.blocked`) is shown with it this long.
    public var blockedHours = 24.0

    public init() {}
}

/// The local API (2026-11-12): what `dollctl` and the Discord bridge talk to. It listens on
/// 127.0.0.1 only (not configurable) and refuses any connection whose peer is not loopback.
public struct ApiConfig: Sendable {
    public var port = 8791

    public init() {}
}

/// The Discord bridge (2026-11-12): a Python process dollcore starts at boot and watches. It reads
/// its own secrets file (bot token, owner id); dollcore never sees them.
public struct BridgeConfig: Sendable {
    public var enabled = false
    /// The bridge's argv; the first element is an absolute path. Runs in the current directory with
    /// `DOLL_API` (the local API's base URL) and `DOLL_BRIDGE_TOKEN` (this launch's owner key) added.
    public var command: [String] = []
    /// Boot fails when the bridge has not connected its owner stream (it does so once Discord says
    /// ready) within this many seconds.
    public var readyTimeoutS = 60.0

    public init() {}
}

public struct IdleConfig: Sendable {
    /// N: this many consecutive turns without a tool call writes one log line. With an item in
    /// progress, the ones before it are answered by a reminder and another turn (2026-11-11); the
    /// N-th is not, and she gets no further turn until an event.
    public var maxTurnsWithoutTool = 3
}

/// M3a: remember / recall. The markdown files are the source; the SQLite tables are a
/// rebuildable index (spec 6).
public struct MemoryConfig: Sendable {
    public var dir = "memory"
    /// Three folders (spec 6, 2026-11-08): the owner's secrets, her own secrets (passwords, tokens), the rest.
    public var secretDir = "secret"
    public var ownDir = "own"
    public var publicDir = "public"
    /// The entity registry's files (2026-11-11), under `dir` like the three folders.
    public var registryDir = "entities"
    /// The login Keychain service of the memory key (`SecretBox`, 2026-11-21); the account is the
    /// data dir. A throwaway doll names its own so the real one's item is never touched.
    public var keychainService = "DollOS dollcore memory key"
    /// RRF's k: the constant in 1/(k + rank).
    public var rrfK = 60
    /// Per list: how many FTS hits and how many vector hits become candidates.
    public var maxCandidates = 50
    /// Confidence enters the fused score as this multiple of itself.
    public var confidenceWeight = 0.01
    /// What a hit from today's timeline counts as in place of a memory's confidence (2026-11-10).
    public var todayConfidence = 0.9
    /// The confidence at or above which a hit is shown as 確定 / 大概; below 大概 it is 聽說.
    public var highConfidence = 0.70
    public var mediumConfidence = 0.40
    /// Starting confidence per source tier (spec 6: evidence count stays at 1 in M3a).
    public var confidence: [SourceTier: Double] = [:]
    /// The embedding model (2026-11-11): a Hugging Face id (downloaded once into the Hugging Face
    /// cache) or an absolute path to its directory. Qwen3-Embedding: last-token pooling.
    public var embeddingModel = "Qwen/Qwen3-Embedding-0.6B"
    /// What a query's embedding is asked to find (Qwen3-Embedding's `Instruct:` line); documents
    /// (memories, registry entries, today's events) have none.
    public var embeddingQueryInstruction = "Given a web search query, retrieve relevant passages that answer the query"
    /// A text longer than this many tokens is cut to its first this-many before it is embedded.
    public var embeddingMaxTokens = 2048
    /// Cap on how many phrases one recall query turns into FTS MATCH terms.
    public var maxQueryPhrases = 16
    /// An experience memory (her actions in a computer session she said reached what she expected,
    /// spec 3.1, 2026-11-09) is cut to at most this many characters, steps from the middle first.
    public var experienceMaxChars = 1200
    /// Experiences whose owner-line embeddings are at least this similar are one group (the same
    /// task); from each group only the best-ranked is attached (spec 3.1, 2026-11-09).
    public var experienceGroupSimilarity = 0.9
    /// An attached experience counts as followed when at least this share of its steps appear, in
    /// order, among her actions of the session.
    public var experienceFollowRatio = 0.5
    /// Memory strength (2026-11-15, experiments/m6-memory-strength): ACT-R base-level activation
    /// ln Σ (1 + age_k / unit)^-d over the days the memory was seen (`Memory.activation`). `d`.
    public var strengthDecay = 0.5
    /// The unit of time, in days.
    public var strengthUnitDays = 7.0
    /// The settled line: a memory whose strength right after a piece of evidence is at least this
    /// no longer fades (stored in its file).
    public var strengthSettled = 0.75
}

/// Consolidation (2026-11-11): a separate, persona-free model request turns the day's timeline into
/// long-term memories, then a new timeline segment begins (as if she slept). Manual only.
public struct ConsolidateConfig: Sendable {
    /// `reasoning_effort` of the consolidation request.
    public var effort = "high"
    /// `max_tokens` of the consolidation request (thinking plus the operations).
    public var maxTokens = 16384
    /// Seconds before the consolidation request gives up.
    public var timeoutS = 900.0
    /// Per task, per query: how many related existing memories are recalled to show it.
    public var recallK = 8
    /// Per task, per query: how many registry entries (`Registry.nearby`) the request shows, besides
    /// those the shown memories name, so the reply can align its entities to them.
    public var registryK = 10
    /// A memory retired longer than this many days is deleted by the next consolidation.
    public var retiredKeepDays = 30
}

/// The maintenance's audit of the day's new memories (2026-11-18, `Audit.swift`). One request (the
/// consolidation's model, effort and max_tokens) per maintenance.
public struct AuditConfig: Sendable {
    /// Off: no `audit` maintenance task (runners and tests not about it).
    public var enabled = true
    /// `change`: a memory with a verified contradicting quote is replaced (or retired) at once;
    /// `flag`: it is only listed for the owner (dashboard, end notice). `flag` by default
    /// (experiments/m8-memory-fix, 2026-11-18): on short seeded days `change` changed 0 of 72 correct
    /// memories, but on realistic days (17–25k tokens, the 30,000-character cap cutting the morning)
    /// it changed a correct one in 3 of 3 runs of day C: 「主人上次心臟科回診是 11/4 早上十點」 lost
    /// 「心臟科」, which only the cut part supported, on a quote that does not contradict it. The
    /// owner's rule: one wrong automatic change on a real day, and the audit only proposes.
    public var mode = "flag"
    /// Most memories one audit checks: one request reads the day plus every memory, and a long list
    /// is checked less carefully; the rest stay unchecked for the next maintenance (oldest first).
    /// 20: a busy day in the m6 runs wrote 5 to 15.
    public var maxMemories = 20
    /// Most characters of source text (the day's lines from others and tool results, windows given
    /// compactly) one audit reads: about 15k prompt tokens, inside what the consolidation request
    /// already handles on the same server. A longer day is cut by `Audit.select`, not oldest first.
    /// With windows compact, no measured day passes it (m8 (d): computer-heavy days 11–22k, A–D 4–22k).
    public var maxSourceChars = 30000
    /// Over `maxSourceChars`, the share of each memory's part of the source given first to the lines
    /// just before it was written (its own moment; the rest goes to the lines sharing the most words
    /// with it). 0.5: the moment and the related lines weigh the same; m8 day C's false change came
    /// from a related line hours away (「心臟科」 in the morning's file), so neither may crowd out the other.
    public var originShare = 0.5
    /// A change (or flag) may not take out of the memory a run of this many letters or digits that its
    /// source holds: dropped (nothing in its place) anywhere in the source, replaced only where a line
    /// the request did not show holds it (`Audit.refusal`). 2: one character (a digit, 的, 一) is
    /// everywhere and a wrong value usually differs in one; a word such as 心臟科 is at least two.
    public var guardChars = 2
}

/// The check of her claims about her own recent actions before a line of hers goes to a person
/// (2026-11-22, `Claims.swift`). The m11 soak's two untrue lines: 11-20 09:15 「我剛查過行事曆確認過了」
/// (her last `calendar.list` was 08:40, before the owner's 09:15 question) and 14:00
/// 「我順手又去查了下週日程」 (that reply's calls: `owner.feedback`, `discord.dm`). A claim is a verb of
/// `verbs` followed within `reach` characters, in the same clause, by a word of `calendarWords` or
/// `memoryWords`; a sentence that dates it before the verb (`earlier`), a verb negated right before
/// it (`negations`), or one whose subject is the owner (`owner` before it, after any 我, and not
/// right after a character of `forOwner`) is not one. Rather miss a claim than refuse a true line:
/// over the soak's 32 lines of hers, with the calls of her replies since the owner's line before
/// each, exactly those two are refused.
public struct ClaimsConfig: Sendable {
    /// Off: her lines go out unchecked.
    public var enabled = true
    /// The verbs that say she did something (with 了／過／好／進／下 they are done, not planned).
    public var verbs = ["查過", "查了", "查好", "看過", "看了", "確認過", "確認了", "記好", "記了", "記下", "記進", "寫進",
                        "加好", "加了", "加進", "改好", "改了", "刪了", "刪掉"]
    /// The words that make a claim one about the calendar: a successful `calendar.*` call backs it.
    public var calendarWords = ["行事曆", "日程", "行程", "提醒"]
    /// The words that make a claim one about her memories: a successful `remember`, `recall` or `memory.fix` backs it.
    public var memoryWords = ["記憶"]
    /// Most characters between the verb and the object word: 「查了下週日程」 is 2; 6 leaves room for
    /// 「你下週的」 and not for a word further on in the clause.
    public var reach = 6
    /// Words before the verb in its sentence that date the action before the line she answers.
    public var earlier = ["之前", "早上", "上午", "下午", "昨天", "昨晚", "前天", "上次", "上週", "稍早", "那時", "當時", "早就"]
    /// Words right before the verb that negate it: 「我還沒查行事曆」 claims nothing.
    public var negations = ["沒", "不", "別", "沒有"]
    /// Words for the owner as the one who did it: 「你改了行事曆」 is his action, not hers.
    public var owner = ["你", "主人"]
    /// Characters right before an owner word that make him the one it was done for: 「幫你查了行事曆」 is hers.
    public var forOwner = ["幫", "替", "給", "跟", "讓", "叫", "陪"]
}

/// The history of what is hers (2026-11-19, layer 4, `History.swift`): every change to her ideal
/// self, goal tree, saved tools, memories, the entity registry and the records of what she did and
/// of corrections is a commit in one git repository of the program's own.
public struct HistoryConfig: Sendable {
    /// The `git` executable (a name looked up on `PATH`, or a path). Missing = boot error.
    public var git = "git"
    /// The repository, under `[core] data_dir` unless absolute. A bare repository: it has no work
    /// tree of its own, so it never adopts or touches anything around it.
    public var dir = "history"
    /// Changes her `history.list` shows at most: the result is read every turn it stays in view, so
    /// short. 8: a busy measured day (m10 (a)) made 5–10 changes of hers.
    public var listItems = 8
    /// Changes the dashboard lists at most (newest first). 200: weeks of days like m10 (a), and one
    /// `git log` call stays well under a second.
    public var dashboardItems = 200
}

/// Which memories she gets (2026-11-11 registry; 2026-11-15 no on-device model at all): the
/// 【相關記憶】 the program attaches and her `recall` results both come from one pipeline
/// (`Selector`): everything related (FTS and two-character words, embedding, the front app's
/// registry entry, one hop from an FTS/embedding hit through a shared entity), then
/// `Selector.Rule`: attach gives at most `attachMax` whose cosine is at least `similarity`, by the
/// cheap score; recall gives the `recallLimit` best by RRF, no floor.
public struct SelectConfig: Sendable {
    /// Auto-attach: the embedding channel and the floor; a candidate whose cosine to the query is
    /// below this is not attached, whatever else ties it to the trigger.
    public var similarity = 0.55
    /// Auto-attach's cheap score: cosine to the query plus this much when it has the front app's
    /// registry entry.
    public var entityWeight = 0.2
    /// Auto-attach: at most this many, best cheap score first.
    public var attachMax = 5
    /// Recall: this many best by cosine are the embedding channel, and this many are given.
    public var recallLimit = 3
    /// recall's embedding channel: this many best by cosine, no floor (what the 31/32 was measured with).
    public var recallCandidates = 5
    /// Auto-attach: a memory that fades (`Memory.fadingStrength`) and whose strength is below this
    /// is not attached (recall still finds it).
    public var strengthFloor = -0.75
    /// Auto-attach's cheap score: plus this × its strength when the strength is below 0 (a fresh,
    /// settled or deep memory, or an experience, adds nothing).
    public var strengthWeight = 0.1
}

/// Her voice in a Discord voice channel (2026-11-13, M6): the bridge streams the owner's audio over
/// `/api/voice`; Apple SpeechTranscriber hears him, Qwen3-TTS 0.6B (voice-mlx) speaks her `voice.say`.
public struct VoiceConfig: Sendable {
    public var enabled = false
    /// SpeechTranscriber's locale (its assets must be installed).
    public var asrLocale = "zh-TW"
    /// After this much owner silence the utterance ends (`finalize(through:)`).
    public var silenceMs = 700
    /// A 16-bit PCM frame whose RMS is below this is silence (the owner holds push-to-talk but says nothing).
    public var silenceRms = 300.0
    /// How often the endpointer checks the silence clock.
    public var endpointPollMs = 20
    /// After `finalize(through:)`, how long to wait for the final result before the utterance is
    /// taken as no words.
    public var asrFinalTimeoutMs = 1500
    /// Talker bundle: a Hugging Face id (cached under ~/Library/Caches/qwen3-speech) or an absolute directory.
    public var ttsModel = "aufklarer/Qwen3-TTS-12Hz-0.6B-Base-MLX-8bit"
    /// Codec bundle, same forms.
    public var ttsCodec = "Qwen/Qwen3-TTS-Tokenizer-12Hz"
    /// int8 | bf16 (voice-mlx `WeightPrecision`).
    public var ttsPrecision = "int8"
    /// The ICL reference voice and its transcript file (relative to the working directory or absolute).
    public var referenceAudio = "character_packs/doll/voice/qwen3/ref_plus_zh.wav"
    public var referenceText = "character_packs/doll/voice/qwen3/ref_plus_zh.txt"
    /// The language passed to the TTS per sentence: "detect" (Han → zh, kana → ja, else en) or one
    /// fixed code voice-mlx accepts.
    public var ttsLanguage = "detect"
    /// The line spoken once at startup: it compiles the talker and seeds the streaming decoder
    /// from the reference.
    public var warmupText = "嗯。"
    /// A sentence longer than this many characters is split again at commas, then hard.
    public var sentenceMaxChars = 60
    /// Where every spoken sentence is kept for the nightly fine-tune (wav + manifest.jsonl), under
    /// `[core] data_dir` unless absolute.
    public var dataDir = "voice"
    /// After `stop`, how long to keep taking `played` reports before the last one counts.
    public var stopSettleMs = 150
    /// The bridge must report `played` (and answer join/leave) within this.
    public var bridgeTimeoutS = 10.0
    /// A new `/api/voice` connection while one is open: the open one gets a ping and must show
    /// life within this, else it is closed as stale and the new one takes its place.
    public var bridgeProbeMs = 1000
}

public struct Config: Sendable {
    public var llm: LlmConfig
    public var core: CoreConfig
    public var context: ContextConfig
    public var computer: ComputerConfig
    public var files: FilesConfig
    public var idle: IdleConfig
    public var memory: MemoryConfig
    public var dashboard: DashboardConfig
    public var api: ApiConfig
    public var bridge: BridgeConfig
    public var consolidate: ConsolidateConfig
    public var select: SelectConfig
    public var voice: VoiceConfig
    public var calendar: CalendarConfig
    public var maintenance: MaintenanceConfig
    public var drives = DrivesConfig()
    public var ownerSleep = OwnerSleepConfig()
    public var goals = GoalsConfig()
    public var audit = AuditConfig()
    public var claims = ClaimsConfig()
    public var log = LogConfig()
    public var history = HistoryConfig()

    /// `[core] data_dir`, a relative one under `root`.
    public func dataDirectory(root: URL) -> URL {
        core.dataDir.hasPrefix("/") ? URL(filePath: core.dataDir) : root.appending(path: core.dataDir)
    }

    public static func load(_ url: URL) throws -> Config {
        let raw = try TOML.parse(String(contentsOf: url, encoding: .utf8))
        let unknown = Set(raw.keys).subtracting(["llm", "core", "context", "computer", "files", "idle", "memory", "dashboard", "api", "bridge",
                                                 "consolidate", "select", "voice", "calendar", "maintenance", "drives", "owner_sleep", "goals", "audit", "claims",
                                                 "log", "history"])
        guard unknown.isEmpty else { throw TOMLError(description: "unknown sections: \(unknown.sorted())") }
        guard let llmRaw = raw["llm"] else { throw TOMLError(description: "[llm] is required") }

        var l = Section(name: "llm", raw: llmRaw)
        var llm = LlmConfig(baseURL: try l.required("base_url"), model: try l.required("model"))
        try l.read("default_effort", &llm.defaultEffort)
        try l.read("task_turn_effort", &llm.taskTurnEffort)
        try l.read("timeout_s", &llm.timeoutS)
        try l.read("max_tokens", &llm.maxTokens)
        try l.finish()
        for (key, value) in [("default_effort", llm.defaultEffort), ("task_turn_effort", llm.taskTurnEffort)] {
            guard Effort.levels.contains(value) else {
                throw TOMLError(description: "[llm] \(key) must be one of \(Effort.levels)")
            }
        }

        var c = Section(name: "core", raw: raw["core"] ?? [:])
        var core = CoreConfig()
        try c.read("data_dir", &core.dataDir)
        try c.read("pack", &core.pack)
        try c.read("priority", &core.priority)
        try c.read("mlx_cache_limit_mb", &core.mlxCacheLimitMB)
        try c.read("docs", &core.docs)
        try c.finish()
        let known = Set(Priority.allCases.map(\.rawValue))
        guard Set(core.priority) == known, core.priority.count == known.count else {
            throw TOMLError(description: "[core] priority must list each of \(known.sorted()) exactly once")
        }
        guard core.mlxCacheLimitMB >= 0 else { throw TOMLError(description: "[core] mlx_cache_limit_mb must be 0 or more") }
        guard !core.docs.isEmpty else { throw TOMLError(description: "[core] docs must not be empty") }

        var x = Section(name: "context", raw: raw["context"] ?? [:])
        var context = ContextConfig()
        try x.read("limit", &context.limit)
        try x.read("warn_ratio", &context.warnRatio)
        try x.read("force_after_turns", &context.forceAfterTurns)
        try x.read("force_keep_turns", &context.forceKeepTurns)
        try x.read("talk_share", &context.talkShare)
        try x.read("talk_effort", &context.talkEffort)
        try x.finish()
        guard context.limit >= 1000, context.warnRatio > 0, context.warnRatio < 1, context.forceAfterTurns >= 1,
              context.forceKeepTurns >= 1, context.talkShare > 0, context.talkShare < 1, Effort.levels.contains(context.talkEffort) else {
            throw TOMLError(description: "[context] limit at least 1000, warn_ratio and talk_share between 0 and 1, force_after_turns and force_keep_turns at least 1, talk_effort one of \(Effort.levels)")
        }

        var v = Section(name: "computer", raw: raw["computer"] ?? [:])
        var computer = ComputerConfig()
        try v.read("enabled", &computer.enabled)
        try v.read("driver", &computer.driver)
        try v.read("daemon_start_s", &computer.daemonStartS)
        try v.read("max_elements", &computer.maxElements)
        try v.read("screenshot_max_dimension", &computer.screenshotMaxDimension)
        try v.read("screenshot_tokens", &computer.screenshotTokens)
        try v.read("settle_s", &computer.settleS)
        try v.read("read_batch", &computer.readBatch)
        try v.read("sequence_max_steps", &computer.sequenceMaxSteps)
        try v.read("cursor_idle_hide_ms", &computer.cursorIdleHideMs)
        try v.read("window_wait_tries", &computer.windowWaitTries)
        try v.read("window_wait_s", &computer.windowWaitS)
        try v.read("auth_processes", &computer.authProcesses)
        try v.read("decide_after_rounds", &computer.decideAfterRounds)
        try v.read("decide_every_rounds", &computer.decideEveryRounds)
        try v.read("plan_min_chars", &computer.planMinChars)
        try v.read("plan_max_facts", &computer.planMaxFacts)
        try v.read("estimate_similarity", &computer.estimateSimilarity)
        try v.read("estimate_similar_max", &computer.estimateSimilarMax)
        try v.read("estimate_far_ratio", &computer.estimateFarRatio)
        try v.read("estimate_far_rounds", &computer.estimateFarRounds)
        try v.read("own_tools_max", &computer.ownToolsMax)
        try v.read("own_tool_offer_similarity", &computer.ownToolOfferSimilarity)
        try v.read("own_tool_offer_max", &computer.ownToolOfferMax)
        try v.read("own_tool_retire_failures", &computer.ownToolRetireFailures)
        try v.finish()
        guard !computer.driver.isEmpty, computer.daemonStartS > 0, computer.maxElements >= 1, computer.screenshotMaxDimension >= 1,
              computer.screenshotTokens >= 0, computer.settleS >= 0, computer.readBatch >= 1, computer.sequenceMaxSteps >= 1, computer.windowWaitTries >= 1, computer.windowWaitS >= 0,
              (0...60000).contains(computer.cursorIdleHideMs),
              !computer.authProcesses.isEmpty, computer.decideAfterRounds >= 1, computer.decideEveryRounds >= 1,
              computer.planMinChars >= 1, computer.planMaxFacts >= 1,
              (-1.0...1.0).contains(computer.estimateSimilarity), computer.estimateSimilarMax >= 1,
              computer.estimateFarRatio >= 1, computer.estimateFarRounds >= 1,
              computer.ownToolsMax >= 1, (-1.0...1.0).contains(computer.ownToolOfferSimilarity), computer.ownToolOfferMax >= 1,
              computer.ownToolRetireFailures >= 1 else {
            throw TOMLError(description: "[computer] driver and auth_processes not empty; daemon_start_s, max_elements, screenshot_max_dimension, read_batch, sequence_max_steps, window_wait_tries, decide_after_rounds, decide_every_rounds, plan_min_chars, plan_max_facts, estimate_similar_max, estimate_far_rounds, own_tools_max, own_tool_offer_max, own_tool_retire_failures positive; screenshot_tokens, settle_s, window_wait_s 0 or more; estimate_far_ratio 1 or more; cursor_idle_hide_ms 0 to 60000; estimate_similarity, own_tool_offer_similarity from -1 to 1")
        }

        var f = Section(name: "files", raw: raw["files"] ?? [:])
        var files = FilesConfig()
        try f.read("max_read_lines", &files.maxReadLines)
        try f.finish()
        guard files.maxReadLines >= 1 else { throw TOMLError(description: "[files] max_read_lines must be at least 1") }

        var i = Section(name: "idle", raw: raw["idle"] ?? [:])
        var idle = IdleConfig()
        try i.read("max_turns_without_tool", &idle.maxTurnsWithoutTool)
        try i.finish()
        guard idle.maxTurnsWithoutTool >= 1 else {
            throw TOMLError(description: "[idle] max_turns_without_tool must be at least 1")
        }

        var m = Section(name: "memory", raw: raw["memory"] ?? [:])
        var memory = MemoryConfig()
        try m.read("dir", &memory.dir)
        try m.read("secret_dir", &memory.secretDir)
        try m.read("own_dir", &memory.ownDir)
        try m.read("public_dir", &memory.publicDir)
        try m.read("registry_dir", &memory.registryDir)
        try m.read("keychain_service", &memory.keychainService)
        try m.read("rrf_k", &memory.rrfK)
        try m.read("max_candidates", &memory.maxCandidates)
        try m.read("confidence_weight", &memory.confidenceWeight)
        try m.read("today_confidence", &memory.todayConfidence)
        try m.read("high_confidence", &memory.highConfidence)
        try m.read("medium_confidence", &memory.mediumConfidence)
        try m.read("embedding_model", &memory.embeddingModel)
        try m.read("embedding_query_instruction", &memory.embeddingQueryInstruction)
        try m.read("embedding_max_tokens", &memory.embeddingMaxTokens)
        try m.read("max_query_phrases", &memory.maxQueryPhrases)
        try m.read("experience_max_chars", &memory.experienceMaxChars)
        try m.read("experience_group_similarity", &memory.experienceGroupSimilarity)
        try m.read("experience_follow_ratio", &memory.experienceFollowRatio)
        try m.read("strength_decay", &memory.strengthDecay)
        try m.read("strength_unit_days", &memory.strengthUnitDays)
        try m.read("strength_settled", &memory.strengthSettled)
        for tier in SourceTier.allCases {
            var v: Double?
            try m.read("confidence_" + tier.rawValue, &v)
            guard let v, (0...1).contains(v) else {
                throw TOMLError(description: "[memory] confidence_\(tier.rawValue) is required and must be between 0 and 1")
            }
            memory.confidence[tier] = v
        }
        try m.finish()
        guard memory.rrfK >= 1, memory.maxCandidates >= 1, memory.experienceMaxChars >= 100,
              (0.0...1.0).contains(memory.experienceGroupSimilarity),
              memory.experienceFollowRatio > 0, memory.experienceFollowRatio <= 1,
              memory.highConfidence > memory.mediumConfidence, !memory.embeddingModel.isEmpty,
              !memory.embeddingQueryInstruction.isEmpty, memory.embeddingMaxTokens >= 1,
              (0.0...1.0).contains(memory.todayConfidence),
              Set([memory.secretDir, memory.ownDir, memory.publicDir, memory.registryDir]).count == 4,
              memory.strengthDecay > 0, memory.strengthUnitDays > 0, memory.strengthSettled > 0 else {
            throw TOMLError(description: "[memory] rrf_k and max_candidates at least 1, experience_max_chars at least 100, experience_group_similarity from 0 to 1, experience_follow_ratio above 0 up to 1, high_confidence > medium_confidence, embedding_model and embedding_query_instruction not empty, embedding_max_tokens at least 1, today_confidence from 0 to 1, four distinct folder names (secret, own, public, registry), strength_decay and strength_unit_days above 0, strength_settled above 0 (a memory seen once is 0 at most)")
        }
        var d = Section(name: "dashboard", raw: raw["dashboard"] ?? [:])
        var dashboard = DashboardConfig()
        try d.read("enabled", &dashboard.enabled)
        try d.read("port", &dashboard.port)
        try d.read("bind", &dashboard.bind)
        try d.read("timeline_items", &dashboard.timelineItems)
        try d.read("boot_reports", &dashboard.bootReports)
        try d.finish()
        guard (1...65535).contains(dashboard.port), !dashboard.bind.isEmpty, dashboard.timelineItems >= 1, dashboard.bootReports >= 1 else {
            throw TOMLError(description: "[dashboard] port 1…65535, bind not empty, timeline_items and boot_reports at least 1")
        }
        var a = Section(name: "api", raw: raw["api"] ?? [:])
        var api = ApiConfig()
        try a.read("port", &api.port)
        try a.finish()
        guard (1...65535).contains(api.port), !(dashboard.enabled && dashboard.port == api.port) else {
            throw TOMLError(description: "[api] port 1…65535 and not the dashboard's port")
        }
        var b = Section(name: "bridge", raw: raw["bridge"] ?? [:])
        var bridge = BridgeConfig()
        try b.read("enabled", &bridge.enabled)
        try b.read("command", &bridge.command)
        try b.read("ready_timeout_s", &bridge.readyTimeoutS)
        try b.finish()
        guard bridge.readyTimeoutS > 0, !bridge.enabled || bridge.command.first?.hasPrefix("/") == true else {
            throw TOMLError(description: "[bridge] ready_timeout_s positive; command (when enabled) an array whose first element is an absolute path")
        }
        var k = Section(name: "consolidate", raw: raw["consolidate"] ?? [:])
        var consolidate = ConsolidateConfig()
        try k.read("effort", &consolidate.effort)
        try k.read("max_tokens", &consolidate.maxTokens)
        try k.read("timeout_s", &consolidate.timeoutS)
        try k.read("recall_k", &consolidate.recallK)
        try k.read("registry_k", &consolidate.registryK)
        try k.read("retired_keep_days", &consolidate.retiredKeepDays)
        try k.finish()
        guard Effort.levels.contains(consolidate.effort), consolidate.maxTokens >= 1, consolidate.timeoutS > 0,
              consolidate.recallK >= 0, consolidate.registryK >= 0, consolidate.retiredKeepDays >= 0 else {
            throw TOMLError(description: "[consolidate] effort one of \(Effort.levels); max_tokens at least 1; timeout_s positive; recall_k, registry_k and retired_keep_days 0 or more")
        }
        var s = Section(name: "select", raw: raw["select"] ?? [:])
        var select = SelectConfig()
        try s.read("similarity", &select.similarity)
        try s.read("entity_weight", &select.entityWeight)
        try s.read("attach_max", &select.attachMax)
        try s.read("recall_limit", &select.recallLimit)
        try s.read("recall_candidates", &select.recallCandidates)
        try s.read("strength_floor", &select.strengthFloor)
        try s.read("strength_weight", &select.strengthWeight)
        try s.finish()
        guard (-1.0...1.0).contains(select.similarity), select.entityWeight >= 0, select.attachMax >= 1,
              select.recallLimit >= 1, select.recallCandidates >= select.recallLimit,
              select.strengthFloor < 0, select.strengthWeight >= 0 else {
            throw TOMLError(description: "[select] similarity from -1 to 1; entity_weight 0 or more; attach_max and recall_limit at least 1; recall_candidates at least recall_limit; strength_floor below 0 (a memory is 0 on the day it is seen); strength_weight 0 or more")
        }
        var o = Section(name: "voice", raw: raw["voice"] ?? [:])
        var voice = VoiceConfig()
        try o.read("enabled", &voice.enabled)
        try o.read("asr_locale", &voice.asrLocale)
        try o.read("silence_ms", &voice.silenceMs)
        try o.read("silence_rms", &voice.silenceRms)
        try o.read("endpoint_poll_ms", &voice.endpointPollMs)
        try o.read("asr_final_timeout_ms", &voice.asrFinalTimeoutMs)
        try o.read("tts_model", &voice.ttsModel)
        try o.read("tts_codec", &voice.ttsCodec)
        try o.read("tts_precision", &voice.ttsPrecision)
        try o.read("reference_audio", &voice.referenceAudio)
        try o.read("reference_text", &voice.referenceText)
        try o.read("tts_language", &voice.ttsLanguage)
        try o.read("warmup_text", &voice.warmupText)
        try o.read("sentence_max_chars", &voice.sentenceMaxChars)
        try o.read("data_dir", &voice.dataDir)
        try o.read("stop_settle_ms", &voice.stopSettleMs)
        try o.read("bridge_timeout_s", &voice.bridgeTimeoutS)
        try o.read("bridge_probe_ms", &voice.bridgeProbeMs)
        try o.finish()
        guard voice.silenceMs >= 100, voice.silenceRms >= 0, voice.endpointPollMs >= 1, voice.asrFinalTimeoutMs >= 1,
              ["int8", "bf16"].contains(voice.ttsPrecision), !voice.ttsModel.isEmpty, !voice.ttsCodec.isEmpty,
              !voice.referenceAudio.isEmpty, !voice.referenceText.isEmpty, !voice.ttsLanguage.isEmpty,
              !voice.warmupText.isEmpty, voice.sentenceMaxChars >= 10, !voice.dataDir.isEmpty, voice.stopSettleMs >= 0,
              voice.bridgeTimeoutS > 0, voice.bridgeProbeMs >= 1 else {
            throw TOMLError(description: "[voice] silence_ms at least 100; silence_rms 0 or more; endpoint_poll_ms and asr_final_timeout_ms at least 1; tts_precision int8 or bf16; tts_model, tts_codec, reference_audio, reference_text, tts_language, warmup_text, data_dir not empty; sentence_max_chars at least 10; stop_settle_ms 0 or more; bridge_timeout_s positive; bridge_probe_ms at least 1")
        }

        var cal = Section(name: "calendar", raw: raw["calendar"] ?? [:])
        var calendar = CalendarConfig()
        try cal.read("status_items", &calendar.statusItems)
        try cal.read("list_max_days", &calendar.listMaxDays)
        try cal.finish()
        guard calendar.statusItems >= 1, calendar.listMaxDays >= 1 else {
            throw TOMLError(description: "[calendar] status_items and list_max_days at least 1")
        }
        var mt = Section(name: "maintenance", raw: raw["maintenance"] ?? [:])
        var maintenance = MaintenanceConfig()
        try mt.read("enabled", &maintenance.enabled)
        try mt.read("time", &maintenance.time)
        try mt.read("busy_limit_hours", &maintenance.busyLimitHours)
        try mt.read("retry_idle_min", &maintenance.retryIdleMin)
        try mt.read("notice_min", &maintenance.noticeMin)
        try mt.finish()
        guard TimeOfDay(maintenance.time) != nil, maintenance.busyLimitHours > 0, maintenance.retryIdleMin >= 1, maintenance.noticeMin >= 0 else {
            throw TOMLError(description: "[maintenance] time HH:MM; busy_limit_hours above 0; retry_idle_min at least 1")
        }
        let drives = try Self.readDrives(raw["drives"] ?? [:])
        var os = Section(name: "owner_sleep", raw: raw["owner_sleep"] ?? [:])
        var ownerSleep = OwnerSleepConfig()
        try os.read("enabled", &ownerSleep.enabled)
        try os.read("start", &ownerSleep.start)
        try os.read("end", &ownerSleep.end)
        try os.finish()
        guard TimeOfDay(ownerSleep.start) != nil, TimeOfDay(ownerSleep.end) != nil, ownerSleep.start != ownerSleep.end else {
            throw TOMLError(description: "[owner_sleep] start and end HH:MM and not equal (an end before the start runs over midnight)")
        }
        var cfg = Config(llm: llm, core: core, context: context, computer: computer, files: files, idle: idle, memory: memory, dashboard: dashboard,
                         api: api, bridge: bridge, consolidate: consolidate, select: select, voice: voice, calendar: calendar, maintenance: maintenance,
                         drives: drives, ownerSleep: ownerSleep)
        cfg.goals = try Self.readGoals(raw["goals"] ?? [:])
        var au = Section(name: "audit", raw: raw["audit"] ?? [:])
        try au.read("enabled", &cfg.audit.enabled)
        try au.read("mode", &cfg.audit.mode)
        try au.read("max_memories", &cfg.audit.maxMemories)
        try au.read("max_source_chars", &cfg.audit.maxSourceChars)
        try au.read("origin_share", &cfg.audit.originShare)
        try au.read("guard_chars", &cfg.audit.guardChars)
        try au.finish()
        guard ["change", "flag"].contains(cfg.audit.mode), cfg.audit.maxMemories >= 1, cfg.audit.maxSourceChars >= 1000,
              (0...1).contains(cfg.audit.originShare), cfg.audit.guardChars >= 1 else {
            throw TOMLError(description: "[audit] mode change or flag; max_memories at least 1; max_source_chars at least 1000; "
                            + "origin_share 0 to 1; guard_chars at least 1")
        }
        var cl = Section(name: "claims", raw: raw["claims"] ?? [:])
        try cl.read("enabled", &cfg.claims.enabled)
        try cl.read("verbs", &cfg.claims.verbs)
        try cl.read("calendar_words", &cfg.claims.calendarWords)
        try cl.read("memory_words", &cfg.claims.memoryWords)
        try cl.read("reach", &cfg.claims.reach)
        try cl.read("earlier", &cfg.claims.earlier)
        try cl.read("negations", &cfg.claims.negations)
        try cl.read("owner", &cfg.claims.owner)
        try cl.read("for_owner", &cfg.claims.forOwner)
        try cl.finish()
        guard cfg.claims.reach >= 0, ([cfg.claims.verbs, cfg.claims.calendarWords, cfg.claims.memoryWords, cfg.claims.earlier,
                                       cfg.claims.negations, cfg.claims.owner, cfg.claims.forOwner].allSatisfy { !$0.contains("") }) else {
            throw TOMLError(description: "[claims] reach at least 0; no empty word in a list")
        }
        var hi = Section(name: "history", raw: raw["history"] ?? [:])
        try hi.read("git", &cfg.history.git)
        try hi.read("dir", &cfg.history.dir)
        try hi.read("list_items", &cfg.history.listItems)
        try hi.read("dashboard_items", &cfg.history.dashboardItems)
        try hi.finish()
        guard !cfg.history.git.isEmpty, !cfg.history.dir.isEmpty, cfg.history.listItems >= 1, cfg.history.dashboardItems >= 1 else {
            throw TOMLError(description: "[history] git and dir non-empty; list_items and dashboard_items at least 1")
        }
        var lg = Section(name: "log", raw: raw["log"] ?? [:])
        try lg.read("max_mb", &cfg.log.maxMB)
        try lg.read("report_kinds", &cfg.log.reportKinds)
        try lg.read("example_chars", &cfg.log.exampleChars)
        try lg.read("turn_warns", &cfg.log.turnWarns)
        try lg.finish()
        guard cfg.log.maxMB > 0, cfg.log.reportKinds >= 1, cfg.log.exampleChars >= 20, cfg.log.turnWarns >= 1 else {
            throw TOMLError(description: "[log] max_mb above 0; report_kinds at least 1; example_chars at least 20; turn_warns at least 1")
        }
        return cfg
    }

    static func readGoals(_ raw: [String: TOMLValue]) throws -> GoalsConfig {
        var s = Section(name: "goals", raw: raw)
        var g = GoalsConfig()
        try s.read("review", &g.review)
        let ints: [(String, WritableKeyPath<GoalsConfig, Int>)] = [
            ("goals_open_max", \.goalsOpenMax), ("subgoals_open_max", \.subgoalsOpenMax), ("steps_open_max", \.stepsOpenMax),
            ("text_max_chars", \.textMaxChars), ("ideal_max_chars", \.idealMaxChars), ("review_chars", \.reviewChars),
        ]
        let nums: [(String, WritableKeyPath<GoalsConfig, Double>)] = [
            ("step_drive", \.stepDrive), ("subgoal_drive", \.subgoalDrive), ("goal_drive", \.goalDrive), ("list_drive", \.listDrive),
            ("now_share", \.nowShare), ("feedback_hours", \.feedbackHours), ("revise_min_days", \.reviseMinDays),
            ("curiosity_hours", \.curiosityHours), ("novelty_drive", \.noveltyDrive), ("novelty_gap_min", \.noveltyGapMin),
            ("seen_days", \.seenDays), ("blocked_hours", \.blockedHours),
        ]
        for (k, p) in ints { try s.read(k, &g[keyPath: p]) }
        for (k, p) in nums { try s.read(k, &g[keyPath: p]) }
        try s.finish()
        guard ints.allSatisfy({ g[keyPath: $0.1] >= 1 }),
              [g.stepDrive, g.subgoalDrive, g.goalDrive, g.listDrive, g.noveltyDrive, g.nowShare].allSatisfy({ (0...1).contains($0) }),
              g.feedbackHours > 0, g.reviseMinDays >= 0, g.curiosityHours > 0, g.noveltyGapMin >= 0, g.seenDays > 0, g.blockedHours > 0 else {
            throw TOMLError(description: "[goals] counts and character caps at least 1; the drive amounts and now_share 0…1; feedback_hours, curiosity_hours, seen_days, blocked_hours above 0; revise_min_days and novelty_gap_min 0 or more")
        }
        return g
    }

    static func readDrives(_ raw: [String: TOMLValue]) throws -> DrivesConfig {
        var s = Section(name: "drives", raw: raw)
        var d = DrivesConfig()
        var order = d.order.map(\.rawValue)
        try s.read("enabled", &d.enabled)
        try s.read("order", &order)
        let nums: [(String, WritableKeyPath<DrivesConfig, Double>)] = [
            ("drive_rest", \.driveRest), ("drive_half_life_h", \.driveHalfLifeH), ("drive_high", \.driveHigh), ("drive_reset", \.driveReset),
            ("stress_rest", \.stressRest), ("stress_half_life_h", \.stressHalfLifeH), ("stress_high", \.stressHigh),
            ("closeness_rest", \.closenessRest), ("closeness_half_life_h", \.closenessHalfLifeH),
            ("closeness_low", \.closenessLow), ("closeness_start", \.closenessStart),
            ("item_done_drive", \.itemDoneDrive),
            ("item_failed_stress", \.itemFailedStress), ("tool_errors_stress", \.toolErrorsStress),
            ("praise_drive", \.praiseDrive), ("praise_closeness", \.praiseCloseness), ("praise_stress", \.praiseStress),
            ("correction_stress", \.correctionStress), ("owner_talk_closeness", \.ownerTalkCloseness),
            ("deadline_stress", \.deadlineStress), ("concern_done_drive", \.concernDoneDrive),
            ("near_hours", \.nearHours),
            ("calendar_concern_hours", \.calendarConcernHours), ("log_days", \.logDays),
        ]
        for (k, p) in nums { try s.read(k, &d[keyPath: p]) }
        try s.read("max_open", &d.maxOpen)
        try s.read("maintenance_concerns_max", &d.maintenanceConcernsMax)
        try s.read("association_similarity", &d.associationSimilarity)
        try s.finish()
        let signals = order.compactMap(Signal.init(rawValue:))
        guard signals.count == order.count, Set(signals).count == Signal.allCases.count, order.count == Signal.allCases.count else {
            throw TOMLError(description: "[drives] order must list each of \(Signal.allCases.map(\.rawValue)) exactly once")
        }
        d.order = signals
        let unit = [d.driveRest, d.driveHigh, d.driveReset, d.stressRest, d.stressHigh, d.closenessRest, d.closenessLow, d.closenessStart]
        guard unit.allSatisfy({ (0...1).contains($0) }), d.driveReset < d.driveHigh, d.driveHigh < d.driveRest, d.stressRest < d.stressHigh,
              d.closenessRest < d.closenessLow, d.closenessStart > d.closenessLow,
              d.driveHalfLifeH > 0, d.stressHalfLifeH > 0, d.closenessHalfLifeH > 0,
              d.nearHours > 0, d.calendarConcernHours > 0, d.logDays > 0, d.maxOpen >= 1,
              d.maintenanceConcernsMax >= 0, (0...1).contains(d.associationSimilarity) else {
            throw TOMLError(description: "[drives] values 0…1; drive_reset below drive_high below drive_rest (動力 climbs while idle; idle-drive, 2026-11-20); stress: rest below high; closeness: rest below low, start above it; half-lives, near_hours, calendar_concern_hours, log_days above 0; max_open at least 1; maintenance_concerns_max 0 or more; association_similarity 0…1")
        }
        return d
    }
}

public enum Effort {
    public static let levels = ["none", "medium", "high"]
}

/// Event sources, named as in `[core] priority`.
public enum Priority: String, CaseIterable, Sendable {
    case ownerVoice = "owner_voice", ownerText = "owner_text", developerText = "developer_text", calendar, watch
    /// Her wandering (m6-drives): a concern surfacing while she is idle; last by default.
    case wander
}

/// Reads typed keys out of one section and rejects keys nobody read.
private struct Section {
    let name: String
    var raw: [String: TOMLValue]

    func bad(_ key: String, _ want: String) -> TOMLError {
        TOMLError(description: "[\(name)] \(key) must be \(want)")
    }

    mutating func required(_ key: String) throws -> String {
        var v = ""
        guard raw[key] != nil else { throw TOMLError(description: "[\(name)] \(key) is required") }
        try read(key, &v)
        return v
    }

    mutating func read(_ key: String, _ out: inout String) throws {
        guard let v = raw.removeValue(forKey: key) else { return }
        guard case .string(let s) = v else { throw bad(key, "a string") }
        out = s
    }

    mutating func read(_ key: String, _ out: inout Bool) throws {
        guard let v = raw.removeValue(forKey: key) else { return }
        guard case .bool(let b) = v else { throw bad(key, "true or false") }
        out = b
    }

    mutating func read(_ key: String, _ out: inout Int) throws {
        guard let v = raw.removeValue(forKey: key) else { return }
        guard case .int(let n) = v else { throw bad(key, "an integer") }
        out = n
    }

    mutating func read(_ key: String, _ out: inout Double) throws {
        guard let v = raw.removeValue(forKey: key) else { return }
        switch v {
        case .double(let d): out = d
        case .int(let n): out = Double(n)
        default: throw bad(key, "a number") }
    }

    mutating func read(_ key: String, _ out: inout Double?) throws {
        guard let v = raw.removeValue(forKey: key) else { return }
        switch v {
        case .double(let d): out = d
        case .int(let n): out = Double(n)
        default: throw bad(key, "a number") }
    }

    mutating func read(_ key: String, _ out: inout [String]) throws {
        guard let v = raw.removeValue(forKey: key) else { return }
        guard case .array(let items) = v else { throw bad(key, "an array of strings") }
        out = try items.map {
            guard case .string(let s) = $0 else { throw bad(key, "an array of strings") }
            return s
        }
    }

    func finish() throws {
        guard raw.isEmpty else { throw TOMLError(description: "[\(name)] unknown keys: \(raw.keys.sorted())") }
    }
}
