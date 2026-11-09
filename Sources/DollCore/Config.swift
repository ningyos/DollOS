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
    public var priority = ["owner_voice", "owner_text", "watch"]
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
    /// watch: how often the watched region is captured and compared.
    public var watchPollS = 1.0
    /// watch: at least this many seconds between two wakes.
    public var watchMinIntervalS = 10.0
    /// watch: after this many wakes in a row with no computer action from her, the watch stops.
    public var watchStopAfter = 3
    /// watch: max_wait when she gives none.
    public var watchDefaultMaxWaitS = 120.0
}

public struct IdleConfig: Sendable {
    /// N: this many consecutive turns without a tool call writes one log line.
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
    /// How many records recall returns.
    public var topK = 5
    /// RRF's k: the constant in 1/(k + rank).
    public var rrfK = 60
    /// Per list: how many FTS hits and how many vector hits become candidates.
    public var maxCandidates = 50
    /// Confidence enters the fused score as this multiple of itself.
    public var confidenceWeight = 0.01
    /// The confidence at or above which a hit is shown as 確定 / 大概; below 大概 it is 聽說.
    public var highConfidence = 0.70
    public var mediumConfidence = 0.40
    /// Starting confidence per source tier (spec 6: evidence count stays at 1 in M3a).
    public var confidence: [SourceTier: Double] = [:]
    /// `NLContextualEmbedding` language tag.
    public var language = "zh-Hant"
    /// Cap on how many phrases one recall query turns into FTS MATCH terms.
    public var maxQueryPhrases = 16
    /// The program's own recall (spec 3.1, 2026-11-09, Qwen Code's shape): on a new owner line, and
    /// when a computer result's front app differs from the last one's, at most this many records not
    /// yet attached since the owner's last line go into the timeline as one 【相關記憶】 event. 0: off.
    public var autoAttachK = 5
    /// An experience memory (her actions in a computer session she said reached what she expected,
    /// spec 3.1, 2026-11-09) is cut to at most this many characters, steps from the middle first.
    public var experienceMaxChars = 1200
}

public struct Config: Sendable {
    public var llm: LlmConfig
    public var core: CoreConfig
    public var computer: ComputerConfig
    public var idle: IdleConfig
    public var memory: MemoryConfig

    public static func load(_ url: URL) throws -> Config {
        let raw = try TOML.parse(String(contentsOf: url, encoding: .utf8))
        let unknown = Set(raw.keys).subtracting(["llm", "core", "computer", "idle", "memory"])
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
        try c.finish()
        let known = Set(Priority.allCases.map(\.rawValue))
        guard Set(core.priority) == known, core.priority.count == known.count else {
            throw TOMLError(description: "[core] priority must list each of \(known.sorted()) exactly once")
        }

        var v = Section(name: "computer", raw: raw["computer"] ?? [:])
        var computer = ComputerConfig()
        computer.vmID = try v.required("vm_id")
        try v.read("vmpal", &computer.vmpal)
        try v.read("agent_desktop", &computer.agentDesktop)
        try v.read("ax_menus", &computer.axMenus)
        try v.read("learn_shortcuts", &computer.learnShortcuts)
        try v.read("screenshot_width", &computer.screenshotWidth)
        try v.read("screenshot_format", &computer.screenshotFormat)
        try v.read("window_crop_width", &computer.windowCropWidth)
        try v.read("window_crop_format", &computer.windowCropFormat)
        try v.read("jpeg_quality", &computer.jpegQuality)
        try v.read("screenshot_tokens", &computer.screenshotTokens)
        try v.read("watch_poll_s", &computer.watchPollS)
        try v.read("watch_min_interval_s", &computer.watchMinIntervalS)
        try v.read("watch_stop_after", &computer.watchStopAfter)
        try v.read("watch_default_max_wait_s", &computer.watchDefaultMaxWaitS)
        try v.finish()
        guard ["jpeg", "png"].contains(computer.screenshotFormat), ["jpeg", "png"].contains(computer.windowCropFormat),
              computer.screenshotWidth >= 1, computer.windowCropWidth >= 1, (0...100).contains(computer.jpegQuality),
              computer.watchPollS > 0, computer.watchMinIntervalS >= 0, computer.watchStopAfter >= 1,
              computer.watchDefaultMaxWaitS > 0 else {
            throw TOMLError(description: "[computer] screenshot_format and window_crop_format are jpeg or png; jpeg_quality 0…100; screenshot_width, window_crop_width, watch_poll_s, watch_stop_after, watch_default_max_wait_s positive")
        }

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
        try m.read("top_k", &memory.topK)
        try m.read("rrf_k", &memory.rrfK)
        try m.read("max_candidates", &memory.maxCandidates)
        try m.read("confidence_weight", &memory.confidenceWeight)
        try m.read("high_confidence", &memory.highConfidence)
        try m.read("medium_confidence", &memory.mediumConfidence)
        try m.read("language", &memory.language)
        try m.read("max_query_phrases", &memory.maxQueryPhrases)
        try m.read("auto_attach_k", &memory.autoAttachK)
        try m.read("experience_max_chars", &memory.experienceMaxChars)
        for tier in SourceTier.allCases {
            var v: Double?
            try m.read("confidence_" + tier.rawValue, &v)
            guard let v, (0...1).contains(v) else {
                throw TOMLError(description: "[memory] confidence_\(tier.rawValue) is required and must be between 0 and 1")
            }
            memory.confidence[tier] = v
        }
        try m.finish()
        guard memory.topK >= 1, memory.rrfK >= 1, memory.maxCandidates >= memory.topK,
              (0...memory.maxCandidates).contains(memory.autoAttachK), memory.experienceMaxChars >= 100,
              memory.highConfidence > memory.mediumConfidence, !memory.language.isEmpty,
              Set([memory.secretDir, memory.ownDir, memory.publicDir]).count == 3 else {
            throw TOMLError(description: "[memory] top_k/rrf_k/max_candidates (max_candidates >= top_k), auto_attach_k from 0 to max_candidates, experience_max_chars at least 100, high_confidence > medium_confidence, three distinct folder names")
        }
        return Config(llm: llm, core: core, computer: computer, idle: idle, memory: memory)
    }
}

public enum Effort {
    public static let levels = ["none", "medium", "high"]
}

/// Event sources, named as in `[core] priority`.
public enum Priority: String, CaseIterable, Sendable {
    case ownerVoice = "owner_voice", ownerText = "owner_text", watch
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
