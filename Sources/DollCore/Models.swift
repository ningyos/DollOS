import Foundation

/// One chat-completions message. The timeline stores events, not messages;
/// messages are rendered from events every step.
public struct ChatMessage: Encodable, Sendable, Equatable {
    public var role: String
    /// Her assistant messages are her raw reply, tool call text included, so a re-render is
    /// byte-identical. A `role: "tool"` message is one call's result; the template renders it as a
    /// `<tool_response>` block and needs no call id.
    public var content: String
    /// A screenshot as a `data:` URI, built from the stored file's bytes, so every turn re-sends
    /// the same bytes. With one, `content` goes out as [text part, image_url part].
    public var image: String?
    /// Text that follows the image (the front window's outline), as one more text part. The template
    /// joins parts with nothing between them, so it goes out after a newline.
    public var after: String?

    public init(role: String, content: String, image: String? = nil, after: String? = nil) {
        self.role = role
        self.content = content
        self.image = image
        self.after = after
    }

    enum CodingKeys: String, CodingKey { case role, content }
    private struct Part: Encodable {
        struct URL: Encodable { let url: String }
        let type: String
        var text: String?
        var image_url: URL?
    }

    public func encode(to encoder: any Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(role, forKey: .role)
        if image == nil && after == nil {
            try c.encode(content, forKey: .content)
        } else {
            try c.encode([Part(type: "text", text: content)]
                         + (image.map { [Part(type: "image_url", image_url: .init(url: $0))] } ?? [])
                         + (after.map { [Part(type: "text", text: "\n" + $0)] } ?? []),
                         forKey: .content)
        }
    }

    static let encoder: JSONEncoder = {
        let e = JSONEncoder()
        // Sorted keys keep request bytes stable across steps, so the server's prompt cache can reuse them.
        e.outputFormatting = [.withoutEscapingSlashes, .sortedKeys]
        return e
    }()
}

public struct ModelError: Error, CustomStringConvertible {
    public let description: String
    public init(_ description: String) { self.description = description }
}

/// What one call cost. Strata reports `usage` and its own `timings`; other
/// servers may leave the timings nil.
public struct ModelUsage: Sendable, Equatable {
    public var promptTokens: Int
    public var completionTokens: Int
    /// Strata `timings.prompt_n`: prompt tokens actually computed this call.
    public var prefilled: Int?
    /// Strata `timings.cache_n`: prompt tokens served from its cache.
    public var cached: Int?
    public var promptMs: Double?
    public var predictedMs: Double?

    public init(promptTokens: Int, completionTokens: Int, prefilled: Int? = nil, cached: Int? = nil,
                promptMs: Double? = nil, predictedMs: Double? = nil) {
        self.promptTokens = promptTokens
        self.completionTokens = completionTokens
        self.prefilled = prefilled
        self.cached = cached
        self.promptMs = promptMs
        self.predictedMs = predictedMs
    }
}

public struct ModelReply: Sendable, Equatable {
    /// Her reply verbatim, tool call text included; `ParsedReply` reads the calls out of it.
    public var content: String
    public var reasoning: String?
    public var usage: ModelUsage
    public init(content: String, reasoning: String? = nil, usage: ModelUsage) {
        self.content = content
        self.reasoning = reasoning
        self.usage = usage
    }
}

/// The main model: an OpenAI chat-completions endpoint.
public protocol MainModel: Sendable {
    func check() async throws
    /// `effort` is the Strata `reasoning_effort` value for this one call. No `tools` are sent: her
    /// tools are listed in the system prompt and her calls come back as text in `content`.
    func chat(_ messages: [ChatMessage], effort: String) async throws -> ModelReply
}

/// DollOS does not know which model sits behind the URL. The only Strata dialect
/// field sent is `reasoning_effort`; `timings` is read when present.
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

    private struct Reply: Decodable {
        struct Msg: Decodable {
            let content: String?
            let reasoning_content: String?
        }
        struct Choice: Decodable { let message: Msg; let finish_reason: String? }
        struct Usage: Decodable { let prompt_tokens: Int; let completion_tokens: Int }
        struct Timings: Decodable {
            let prompt_n: Int?; let cache_n: Int?; let prompt_ms: Double?; let predicted_ms: Double?
        }
        let choices: [Choice]
        let usage: Usage?
        let timings: Timings?
    }

    private func post(_ messages: [ChatMessage], effort: String, maxTokens: Int) async throws -> Reply {
        struct Body: Encodable {
            let model: String
            let messages: [ChatMessage]
            let max_tokens: Int
            let reasoning_effort: String
        }
        var req = URLRequest(url: URL(string: cfg.baseURL + "/v1/chat/completions")!)
        req.httpMethod = "POST"
        req.timeoutInterval = cfg.timeoutS
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try ChatMessage.encoder.encode(Body(
            model: cfg.model, messages: messages, max_tokens: maxTokens, reasoning_effort: effort))
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
        do { return try JSONDecoder().decode(Reply.self, from: data) } catch {
            throw ModelError("main model returned an unreadable reply: \(error)")
        }
    }

    public func chat(_ messages: [ChatMessage], effort: String) async throws -> ModelReply {
        let reply = try await post(messages, effort: effort, maxTokens: cfg.maxTokens)
        guard let choice = reply.choices.first else { throw ModelError("main model returned no choices") }
        if choice.finish_reason == "length" { throw ModelError("main model hit max_tokens") }
        guard let u = reply.usage else { throw ModelError("main model returned no usage") }
        let t = reply.timings
        return ModelReply(
            content: choice.message.content ?? "",
            reasoning: choice.message.reasoning_content,
            usage: ModelUsage(promptTokens: u.prompt_tokens, completionTokens: u.completion_tokens,
                              prefilled: t?.prompt_n, cached: t?.cache_n, promptMs: t?.prompt_ms,
                              predictedMs: t?.predicted_ms))
    }
}
