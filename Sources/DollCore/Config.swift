import Foundation

/// Every behavior number lives here, loaded from TOML. Unknown keys are errors.
public struct LlmConfig: Sendable {
    public var baseURL: String
    public var model: String
    /// `reasoning_effort` for a normal turn. "我想一下" raises it for one call.
    public var defaultEffort = "none"
    public var timeoutS = 300.0
    public var maxTokens = 4096
}

public struct CoreConfig: Sendable {
    public var dataDir = "data"
    public var pack = "character_packs/doll2/doll.toml"
    /// Event sources from highest to lowest priority when the queue is drained.
    public var priority = ["owner_voice", "owner_text", "job_result"]
}

public struct ShellConfig: Sendable {
    /// Where every command runs. The only place she touches until the VM (M2).
    public var scratchDir = "~/dollos-scratch"
    /// A command that finishes within this many seconds returns in the same turn.
    public var waitS = 10.0
    /// Hard limit for a background job.
    public var timeoutS = 3600.0
    /// How much of a command's output she gets to see (the tail).
    public var resultTailChars = 4000
}

public struct IdleConfig: Sendable {
    /// N: this many consecutive turns without a tool call writes one log line.
    public var maxTurnsWithoutTool = 3
}

/// M3a: remember / recall. The markdown files are the source; the SQLite tables are a
/// rebuildable index (spec 6).
public struct MemoryConfig: Sendable {
    public var dir = "memory"
    public var secretDir = "secret"
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
}

public struct Config: Sendable {
    public var llm: LlmConfig
    public var core: CoreConfig
    public var shell: ShellConfig
    public var idle: IdleConfig
    public var memory: MemoryConfig

    public static func load(_ url: URL) throws -> Config {
        let raw = try TOML.parse(String(contentsOf: url, encoding: .utf8))
        let unknown = Set(raw.keys).subtracting(["llm", "core", "shell", "idle", "memory"])
        guard unknown.isEmpty else { throw TOMLError(description: "unknown sections: \(unknown.sorted())") }
        guard let llmRaw = raw["llm"] else { throw TOMLError(description: "[llm] is required") }

        var l = Section(name: "llm", raw: llmRaw)
        var llm = LlmConfig(baseURL: try l.required("base_url"), model: try l.required("model"))
        try l.read("default_effort", &llm.defaultEffort)
        try l.read("timeout_s", &llm.timeoutS)
        try l.read("max_tokens", &llm.maxTokens)
        try l.finish()
        guard Effort.levels.contains(llm.defaultEffort) else {
            throw TOMLError(description: "[llm] default_effort must be one of \(Effort.levels)")
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

        var s = Section(name: "shell", raw: raw["shell"] ?? [:])
        var shell = ShellConfig()
        try s.read("scratch_dir", &shell.scratchDir)
        try s.read("wait_s", &shell.waitS)
        try s.read("timeout_s", &shell.timeoutS)
        try s.read("result_tail_chars", &shell.resultTailChars)
        try s.finish()

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
        try m.read("public_dir", &memory.publicDir)
        try m.read("top_k", &memory.topK)
        try m.read("rrf_k", &memory.rrfK)
        try m.read("max_candidates", &memory.maxCandidates)
        try m.read("confidence_weight", &memory.confidenceWeight)
        try m.read("high_confidence", &memory.highConfidence)
        try m.read("medium_confidence", &memory.mediumConfidence)
        try m.read("language", &memory.language)
        try m.read("max_query_phrases", &memory.maxQueryPhrases)
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
              memory.highConfidence > memory.mediumConfidence, !memory.language.isEmpty else {
            throw TOMLError(description: "[memory] top_k/rrf_k/max_candidates (max_candidates >= top_k), high_confidence > medium_confidence")
        }
        return Config(llm: llm, core: core, shell: shell, idle: idle, memory: memory)
    }
}

public enum Effort {
    public static let levels = ["none", "medium", "high"]
}

/// Event sources, named as in `[core] priority`.
public enum Priority: String, CaseIterable, Sendable {
    case ownerVoice = "owner_voice", ownerText = "owner_text", jobResult = "job_result"
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
