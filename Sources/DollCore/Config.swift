import Foundation

/// Every behavior number lives here, loaded from TOML. Unknown keys are errors.
public struct LlmConfig: Sendable {
    public var baseURL: String
    public var model: String
    public var reasoningEffort = "high"
    public var timeoutS = 300.0
    public var maxTokens = 4096
}

public struct CoreConfig: Sendable {
    public var dataDir = "data"
    public var pack = "character_packs/doll2/doll.toml"
    public var maxInternalPasses = 4
    public var maxOpenLines = 5
    public var lineDailySteps = 20
    public var idleFirstS = 300.0
    public var idleEveryS = 600.0
    public var shellTimeoutS = 600.0
    public var jobResultInlineChars = 4000
    public var timelineWindowMessages = 200
    public var approveExternal = true
    public var approvalTimeoutS = 1800.0
}

public struct Config: Sendable {
    public var llm: LlmConfig
    public var core: CoreConfig

    public static func load(_ url: URL) throws -> Config {
        let raw = try TOML.parse(String(contentsOf: url, encoding: .utf8))
        let unknown = Set(raw.keys).subtracting(["llm", "core"])
        guard unknown.isEmpty else { throw TOMLError(description: "unknown sections: \(unknown.sorted())") }
        guard let llmRaw = raw["llm"] else { throw TOMLError(description: "[llm] is required") }

        var l = Section(name: "llm", raw: llmRaw)
        var llm = LlmConfig(baseURL: try l.required("base_url"), model: try l.required("model"))
        try l.read("reasoning_effort", &llm.reasoningEffort)
        try l.read("timeout_s", &llm.timeoutS)
        try l.read("max_tokens", &llm.maxTokens)
        try l.finish()

        var c = Section(name: "core", raw: raw["core"] ?? [:])
        var core = CoreConfig()
        try c.read("data_dir", &core.dataDir)
        try c.read("pack", &core.pack)
        try c.read("max_internal_passes", &core.maxInternalPasses)
        try c.read("max_open_lines", &core.maxOpenLines)
        try c.read("line_daily_steps", &core.lineDailySteps)
        try c.read("idle_first_s", &core.idleFirstS)
        try c.read("idle_every_s", &core.idleEveryS)
        try c.read("shell_timeout_s", &core.shellTimeoutS)
        try c.read("job_result_inline_chars", &core.jobResultInlineChars)
        try c.read("timeline_window_messages", &core.timelineWindowMessages)
        try c.read("approve_external", &core.approveExternal)
        try c.read("approval_timeout_s", &core.approvalTimeoutS)
        try c.finish()
        return Config(llm: llm, core: core)
    }
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
        default: throw bad(key, "a number")
        }
    }

    mutating func read(_ key: String, _ out: inout Bool) throws {
        guard let v = raw.removeValue(forKey: key) else { return }
        guard case .bool(let b) = v else { throw bad(key, "true or false") }
        out = b
    }

    func finish() throws {
        guard raw.isEmpty else { throw TOMLError(description: "[\(name)] unknown keys: \(raw.keys.sorted())") }
    }
}
