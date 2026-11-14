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
    public var pack = "character_packs/doll2/doll.toml"
    /// Event sources from highest to lowest priority when the queue is drained.
    public var priority = ["owner_voice", "owner_text", "developer_text", "watch"]
    /// MLX's buffer cache limit for the whole process (2026-11-13, M6): the embedding model and her
    /// voice share one MLX runtime, so one limit covers both. Set before either loads.
    public var mlxCacheLimitMB = 256
    /// Her DollOS documents (2026-11-14): the repo folder copied into her computer at
    /// `Doll.docsVMPath` at boot; relative paths are under the config's folder.
    public var docs = "dolldocs"
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

/// M2: her computer, a VMPal VM she drives with screenshots, pointer and keyboard (spec 3.1, 2026-11-08).
public struct ComputerConfig: Sendable {
    /// The VM's whole ID: `vmpal ui` resolves it without asking VMPal.
    public var vmID = ""
    /// The VMPal CLI; it runs as `<vmpal> --cli ui <vm_id> …` (and `exec`, `cp`).
    public var vmpal = "/Applications/VMPal.app/Contents/Helpers/VMPalMachine.app/Contents/MacOS/VMPalMachine"
    /// agent-desktop inside the VM (run with `vmpal exec`): the front window's outline after every action.
    public var agentDesktop = "/Users/doll/agent-desktop/agent-desktop"
    /// The menu-shortcut reader inside the VM (`scripts/axmenus.swift`): `--front` names the front
    /// app, `<bundle id>` lists that app's menu items with shortcuts.
    public var axMenus = "/Users/doll/agent-desktop/axmenus"
    /// When the front app is one not yet recorded at its version, the program writes one public
    /// memory per menu shortcut of it (spec 3.1, 2026-11-09).
    public var learnShortcuts = true
    /// When a computer result's front app differs from the last one's and that app has had no
    /// cheatsheet since the owner's last line, the program puts its whole menu-shortcut list into
    /// the timeline as one 【快捷鍵】 event (spec 3.1, 2026-11-09).
    public var cheatsheet = true
    /// Every screenshot she gets has this width; her coordinates are in this space.
    public var screenshotWidth = 1280
    /// "jpeg" or "png".
    public var screenshotFormat = "jpeg"
    /// The front window's crop (spec 3.1, 2026-11-09: full screen plus the front window): at most this
    /// wide (VMPal `--max-width`; a narrower window keeps the screen's own resolution).
    public var windowCropWidth = 1280
    /// The crop's format: "jpeg" or "png".
    public var windowCropFormat = "jpeg"
    /// JPEG screenshots (full and crop) are re-encoded at this quality, 1…100; 0 keeps VMPal's own
    /// bytes. Every old screenshot is re-sent each turn, so their size is upload time (spec 3.1, 2026-11-09).
    public var jpegQuality = 40
    /// Rough prompt tokens of one screenshot, for the footer's context figure only.
    public var screenshotTokens = 350
    /// How many of the most recent turns keep their screenshots in the request; earlier results go
    /// out as text only. 0 = every turn keeps them (the default): on Qwen3-Next Strata's prompt cache
    /// resumes only from checkpoints, so dropping an old image costs a re-prefill of everything after
    /// the owner's line (2026-11-09: 25-31 s a turn instead of 6.5). Useful only for models whose cache
    /// matches any prefix.
    public var imageTurnsKept = 0
    /// watch: how often the watched region is captured and compared.
    public var watchPollS = 1.0
    /// watch: at least this many seconds between two wakes.
    public var watchMinIntervalS = 10.0
    /// watch: after this many wakes in a row with no computer action from her, the watch stops.
    public var watchStopAfter = 3
    /// watch: max_wait when she gives none.
    public var watchDefaultMaxWaitS = 120.0
}

/// Her file tools in the computer menu (2026-11-09): read_file, write_file, edit_file on text files in the VM.
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

    public static func load(_ url: URL) throws -> Config {
        let raw = try TOML.parse(String(contentsOf: url, encoding: .utf8))
        let unknown = Set(raw.keys).subtracting(["llm", "core", "context", "computer", "files", "idle", "memory", "dashboard", "api", "bridge",
                                                 "consolidate", "select", "voice"])
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
        computer.vmID = try v.required("vm_id")
        try v.read("vmpal", &computer.vmpal)
        try v.read("agent_desktop", &computer.agentDesktop)
        try v.read("ax_menus", &computer.axMenus)
        try v.read("learn_shortcuts", &computer.learnShortcuts)
        try v.read("cheatsheet", &computer.cheatsheet)
        try v.read("screenshot_width", &computer.screenshotWidth)
        try v.read("screenshot_format", &computer.screenshotFormat)
        try v.read("window_crop_width", &computer.windowCropWidth)
        try v.read("window_crop_format", &computer.windowCropFormat)
        try v.read("jpeg_quality", &computer.jpegQuality)
        try v.read("screenshot_tokens", &computer.screenshotTokens)
        try v.read("image_turns_kept", &computer.imageTurnsKept)
        try v.read("watch_poll_s", &computer.watchPollS)
        try v.read("watch_min_interval_s", &computer.watchMinIntervalS)
        try v.read("watch_stop_after", &computer.watchStopAfter)
        try v.read("watch_default_max_wait_s", &computer.watchDefaultMaxWaitS)
        try v.finish()
        guard ["jpeg", "png"].contains(computer.screenshotFormat), ["jpeg", "png"].contains(computer.windowCropFormat),
              computer.screenshotWidth >= 1, computer.windowCropWidth >= 1, (0...100).contains(computer.jpegQuality),
              computer.imageTurnsKept >= 0,
              computer.watchPollS > 0, computer.watchMinIntervalS >= 0, computer.watchStopAfter >= 1,
              computer.watchDefaultMaxWaitS > 0 else {
            throw TOMLError(description: "[computer] screenshot_format and window_crop_format are jpeg or png; jpeg_quality 0…100; image_turns_kept 0 or more; screenshot_width, window_crop_width, watch_poll_s, watch_stop_after, watch_default_max_wait_s positive")
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
        try d.finish()
        guard (1...65535).contains(dashboard.port), !dashboard.bind.isEmpty, dashboard.timelineItems >= 1 else {
            throw TOMLError(description: "[dashboard] port 1…65535, bind not empty, timeline_items at least 1")
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
        return Config(llm: llm, core: core, context: context, computer: computer, files: files, idle: idle, memory: memory, dashboard: dashboard,
                      api: api, bridge: bridge, consolidate: consolidate, select: select, voice: voice)
    }
}

public enum Effort {
    public static let levels = ["none", "medium", "high"]
}

/// Event sources, named as in `[core] priority`.
public enum Priority: String, CaseIterable, Sendable {
    case ownerVoice = "owner_voice", ownerText = "owner_text", developerText = "developer_text", watch
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
