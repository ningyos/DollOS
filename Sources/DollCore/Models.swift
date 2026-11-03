import Foundation
import FoundationModels

/// Any JSON value: tool schemas and tool-call arguments.
public enum JSONValue: Codable, Sendable, Equatable {
    case null
    case bool(Bool)
    case number(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    public init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let b = try? c.decode(Bool.self) { self = .bool(b) }
        else if let n = try? c.decode(Double.self) { self = .number(n) }
        else if let s = try? c.decode(String.self) { self = .string(s) }
        else if let a = try? c.decode([JSONValue].self) { self = .array(a) }
        else { self = .object(try c.decode([String: JSONValue].self)) }
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .null: try c.encodeNil()
        case .bool(let b): try c.encode(b)
        case .number(let n): try c.encode(n)
        case .string(let s): try c.encode(s)
        case .array(let a): try c.encode(a)
        case .object(let o): try c.encode(o)
        }
    }

    public var string: String? {
        if case .string(let s) = self { return s }
        return nil
    }
}

public struct ToolCall: Codable, Sendable, Equatable {
    public struct Function: Codable, Sendable, Equatable {
        public var name: String
        public var arguments: String?
        public init(name: String, arguments: String?) { self.name = name; self.arguments = arguments }
    }
    public var id: String
    public var type: String?
    public var function: Function
    public init(id: String, type: String? = "function", function: Function) {
        self.id = id; self.type = type; self.function = function
    }
}

/// One chat-completions message, stored verbatim in the timeline.
public struct ChatMessage: Codable, Sendable, Equatable {
    public var role: String
    public var content: String?
    public var reasoningContent: String?
    public var toolCalls: [ToolCall]?
    public var toolCallID: String?

    enum CodingKeys: String, CodingKey {
        case role, content
        case reasoningContent = "reasoning_content"
        case toolCalls = "tool_calls"
        case toolCallID = "tool_call_id"
    }

    public init(role: String, content: String?, reasoningContent: String? = nil,
                toolCalls: [ToolCall]? = nil, toolCallID: String? = nil) {
        self.role = role
        self.content = content
        self.reasoningContent = reasoningContent
        self.toolCalls = toolCalls
        self.toolCallID = toolCallID
    }

    static let encoder: JSONEncoder = {
        let e = JSONEncoder()
        // Sorted keys keep request bytes stable across turns, so the server's prompt cache can reuse them.
        e.outputFormatting = [.withoutEscapingSlashes, .sortedKeys]
        return e
    }()
}

public struct ModelError: Error, CustomStringConvertible {
    public let description: String
    public init(_ description: String) { self.description = description }
}

/// The main model: an OpenAI chat-completions endpoint.
public protocol MainModel: Sendable {
    func check() async throws
    func chat(_ messages: [ChatMessage], tools: [JSONValue]) async throws -> ChatMessage
}

/// Small structured judgments. Self-reported picks, not calibrated probabilities.
public protocol DecisionModel: Sendable {
    func check() async throws
    /// Returns one of `choices` or "none".
    func pick(instructions: String, prompt: String, choices: [String]) async throws -> String
}

/// DollOS does not know which model sits behind the URL. The only Strata dialect
/// field sent is `reasoning_effort`.
public struct HTTPMainModel: MainModel {
    let cfg: LlmConfig

    public init(_ cfg: LlmConfig) { self.cfg = cfg }

    private func get(_ path: String, _ session: URLSession) async throws -> (Data, Int) {
        do {
            let (data, resp) = try await session.data(from: URL(string: cfg.baseURL + path)!)
            return (data, (resp as! HTTPURLResponse).statusCode)
        } catch {
            throw ModelError("\(cfg.baseURL) unreachable: \(error.localizedDescription)")
        }
    }

    public func check() async throws {
        let conf = URLSessionConfiguration.ephemeral
        conf.timeoutIntervalForRequest = 10
        let session = URLSession(configuration: conf)
        let (_, health) = try await get("/health", session)
        guard health == 200 else { throw ModelError("\(cfg.baseURL)/health -> \(health)") }
        struct Models: Decodable { struct M: Decodable { let id: String? }; let data: [M]? }
        let (body, _) = try await get("/v1/models", session)
        let ids = ((try? JSONDecoder().decode(Models.self, from: body))?.data ?? []).compactMap(\.id)
        guard ids.contains(cfg.model) else {
            throw ModelError("model \"\(cfg.model)\" not served by \(cfg.baseURL) (has \(ids))")
        }
    }

    public func chat(_ messages: [ChatMessage], tools: [JSONValue]) async throws -> ChatMessage {
        struct Body: Encodable {
            let model: String
            let messages: [ChatMessage]
            let max_tokens: Int
            let reasoning_effort: String
            let tools: [JSONValue]?
        }
        struct Reply: Decodable {
            struct Choice: Decodable { let message: ChatMessage; let finish_reason: String? }
            let choices: [Choice]
        }
        var req = URLRequest(url: URL(string: cfg.baseURL + "/v1/chat/completions")!)
        req.httpMethod = "POST"
        req.timeoutInterval = cfg.timeoutS
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try ChatMessage.encoder.encode(Body(
            model: cfg.model, messages: messages, max_tokens: cfg.maxTokens,
            reasoning_effort: cfg.reasoningEffort, tools: tools.isEmpty ? nil : tools))
        let data: Data, status: Int
        do {
            let (d, resp) = try await URLSession.shared.data(for: req)
            (data, status) = (d, (resp as! HTTPURLResponse).statusCode)
        } catch {
            try Task.checkCancellation()
            throw ModelError("main model request failed: \(error.localizedDescription)")
        }
        guard status == 200 else {
            throw ModelError("main model HTTP \(status): \(String(decoding: data.prefix(300), as: UTF8.self))")
        }
        let reply: Reply
        do { reply = try JSONDecoder().decode(Reply.self, from: data) } catch {
            throw ModelError("main model returned an unreadable reply: \(error)")
        }
        guard let choice = reply.choices.first else { throw ModelError("main model returned no choices") }
        if choice.finish_reason == "length" { throw ModelError("main model hit max_tokens") }
        return choice.message
    }
}

/// Apple's on-device Foundation Models, called directly, with guided generation
/// constrained to the candidate ids plus "none".
public struct FoundationDecisionModel: DecisionModel {
    public init() {}

    public func check() async throws {
        let availability = SystemLanguageModel.default.availability
        guard case .available = availability else {
            throw ModelError("Apple Foundation Models unavailable: \(availability)")
        }
    }

    public func pick(instructions: String, prompt: String, choices: [String]) async throws -> String {
        do {
            let root = DynamicGenerationSchema(name: "Decision", properties: [
                DynamicGenerationSchema.Property(
                    name: "line_id", schema: DynamicGenerationSchema(name: "LineID", anyOf: choices + ["none"])),
            ])
            let schema = try GenerationSchema(root: root, dependencies: [])
            let session = LanguageModelSession(instructions: instructions)
            let reply = try await session.respond(
                to: prompt, schema: schema, options: GenerationOptions(samplingMode: .greedy))
            return try reply.content.value(String.self, forProperty: "line_id")
        } catch {
            try Task.checkCancellation()
            throw ModelError("decision model failed: \(error)")
        }
    }
}
