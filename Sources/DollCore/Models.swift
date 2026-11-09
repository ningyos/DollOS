import CryptoKit
import Foundation
import Synchronization

/// One chat-completions message. The timeline stores events, not messages;
/// messages are rendered from events every step.
public struct ChatMessage: Encodable, Sendable, Equatable {
    public var role: String
    /// Her assistant messages are her raw reply, tool call text included, so a re-render is
    /// byte-identical. A `role: "tool"` message is one call's result; the template renders it as a
    /// `<tool_response>` block and needs no call id.
    public var content: String
    /// What follows `content` in the same message, in order: screenshots as `data:` URIs built from
    /// the stored files' bytes (so every turn re-sends the same bytes), and text (the crop's place,
    /// the front window's outline). With any, `content` goes out as a list of parts, text first.
    public var extra: [Extra]

    public enum Extra: Sendable, Equatable {
        case image(String)
        /// The template joins parts with nothing between them, so it goes out after a newline.
        case text(String)
    }

    public init(role: String, content: String, extra: [Extra] = []) {
        self.role = role
        self.content = content
        self.extra = extra
    }

    /// The `data:` URIs among `extra`, in order.
    public var images: [String] { extra.compactMap { if case .image(let u) = $0 { u } else { nil } } }

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
        if extra.isEmpty {
            try c.encode(content, forKey: .content)
        } else {
            try c.encode([Part(type: "text", text: content)] + extra.map {
                switch $0 {
                case .image(let uri): Part(type: "image_url", image_url: .init(url: uri))
                case .text(let t): Part(type: "text", text: "\n" + t)
                }
            }, forKey: .content)
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
    /// Bytes of the request body as sent (after `hash:` references replaced pictures already uploaded).
    public var requestBytes: Int?

    public init(promptTokens: Int, completionTokens: Int, prefilled: Int? = nil, cached: Int? = nil,
                promptMs: Double? = nil, predictedMs: Double? = nil, requestBytes: Int? = nil) {
        self.promptTokens = promptTokens
        self.completionTokens = completionTokens
        self.prefilled = prefilled
        self.cached = cached
        self.promptMs = promptMs
        self.predictedMs = predictedMs
        self.requestBytes = requestBytes
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
///
/// Pictures by reference (spec 3.1, 2026-11-09): when `/health` says `image_hash_refs`, a picture this
/// process has already uploaded goes out as `hash:<key>` (the server's cache key: the first 32 hex of
/// the sha256 of its bytes) instead of its `data:` URL, so a request stops growing by two screenshots
/// a turn. The messages the caller builds are unchanged (byte-identical renders keep the prefix
/// cache); only the wire form differs. A 400 `image_not_cached` names the keys the server no longer
/// holds: those pictures are sent as bytes again, once. A server without the flag gets every picture
/// as bytes, every turn.
public final class HTTPMainModel: MainModel {
    let cfg: LlmConfig
    private struct State {
        var hashRefs = false
        /// Keys the server has encoded (uploaded by this process and answered 200).
        var sent: Set<String> = []
        /// data: URI → key, so a picture is hashed once.
        var keys: [String: String] = [:]
    }
    private let state = Mutex(State())

    /// One HTTP exchange: the request → (body, status). Tests pass a fake; the default is URLSession.
    public typealias Transport = @Sendable (URLRequest) async throws -> (Data, Int)
    private let transport: Transport

    public init(_ cfg: LlmConfig, transport: Transport? = nil) {
        self.cfg = cfg
        self.transport = transport ?? { req in
            let (d, resp) = try await URLSession.shared.data(for: req)
            return (d, (resp as! HTTPURLResponse).statusCode)
        }
    }

    /// Whether the server takes `hash:` references (known after `check()`).
    public var imageHashRefs: Bool { state.withLock { $0.hashRefs } }

    private func get(_ path: String) async throws -> (Data, Int) {
        var req = URLRequest(url: URL(string: cfg.baseURL + path)!)
        req.timeoutInterval = 10
        do { return try await transport(req) } catch {
            throw ModelError("\(cfg.baseURL) unreachable: \(error.localizedDescription)")
        }
    }

    public func check() async throws {
        let (healthBody, health) = try await get("/health")
        guard health == 200 else { throw ModelError("\(cfg.baseURL)/health -> \(health)") }
        struct Health: Decodable { let image_hash_refs: Bool? }
        let refs = (try? JSONDecoder().decode(Health.self, from: healthBody))?.image_hash_refs == true
        state.withLock { $0.hashRefs = refs; $0.sent = []; $0.keys = [:] }
        struct Models: Decodable { struct M: Decodable { let id: String? }; let data: [M]? }
        let (body, _) = try await get("/v1/models")
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

    /// The server's cache key of a `data:` picture: the first 32 hex digits of the sha256 of its bytes
    /// (Strata normalizes JPEG/PNG/BMP/GIF by passing them through, so the key is of the bytes as sent).
    static func imageKey(_ dataURI: String) -> String? {
        guard dataURI.hasPrefix("data:"), let comma = dataURI.firstIndex(of: ","),
              let bytes = Data(base64Encoded: String(dataURI[dataURI.index(after: comma)...])) else { return nil }
        return SHA256.hash(data: bytes).prefix(16).map { String(format: "%02x", $0) }.joined()
    }

    /// The wire form of `messages`: pictures the server holds become `hash:<key>`; the keys of the
    /// pictures sent as bytes this time (to mark as held once the server answers 200).
    private func wire(_ messages: [ChatMessage]) -> (messages: [ChatMessage], uploaded: [String]) {
        state.withLock { s in
            guard s.hashRefs else { return (messages, []) }
            var uploaded: [String] = []
            let out = messages.map { m in
                var m = m
                m.extra = m.extra.map { part in
                    guard case .image(let uri) = part, uri.hasPrefix("data:") else { return part }
                    let key: String
                    if let k = s.keys[uri] { key = k } else if let k = Self.imageKey(uri) { s.keys[uri] = k; key = k } else { return part }
                    if s.sent.contains(key) { return .image("hash:\(key)") }
                    uploaded.append(key)
                    return part
                }
                return m
            }
            return (out, uploaded)
        }
    }

    private func post(_ messages: [ChatMessage], effort: String, maxTokens: Int) async throws -> (Reply, Int) {
        struct Body: Encodable {
            let model: String
            let messages: [ChatMessage]
            let max_tokens: Int
            let reasoning_effort: String
        }
        struct NotCached: Decodable { struct E: Decodable { let code: String?; let missing: [String]? }; let error: E }
        for attempt in 0..<2 {
            let (sendable, uploaded) = wire(messages)
            var req = URLRequest(url: URL(string: cfg.baseURL + "/v1/chat/completions")!)
            req.httpMethod = "POST"
            req.timeoutInterval = cfg.timeoutS
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try ChatMessage.encoder.encode(Body(
                model: cfg.model, messages: sendable, max_tokens: maxTokens, reasoning_effort: effort))
            let bytes = req.httpBody!.count
            let data: Data, status: Int
            do {
                (data, status) = try await transport(req)
            } catch {
                try Task.checkCancellation()
                throw ModelError("main model request failed: \(error.localizedDescription)")
            }
            if status == 400, attempt == 0, let e = try? JSONDecoder().decode(NotCached.self, from: data),
               e.error.code == "image_not_cached", let missing = e.error.missing {
                // The server no longer holds these pictures (evicted, or it restarted): send their bytes again.
                state.withLock { s in for k in missing { s.sent.remove(k) } }
                continue
            }
            guard status == 200 else {
                throw ModelError("main model HTTP \(status): \(String(decoding: data.prefix(300), as: UTF8.self))")
            }
            state.withLock { s in s.sent.formUnion(uploaded) }
            do { return (try JSONDecoder().decode(Reply.self, from: data), bytes) } catch {
                throw ModelError("main model returned an unreadable reply: \(error)")
            }
        }
        throw ModelError("main model still reports image_not_cached after the pictures were sent again")
    }

    public func chat(_ messages: [ChatMessage], effort: String) async throws -> ModelReply {
        let (reply, bytes) = try await post(messages, effort: effort, maxTokens: cfg.maxTokens)
        guard let choice = reply.choices.first else { throw ModelError("main model returned no choices") }
        if choice.finish_reason == "length" { throw ModelError("main model hit max_tokens") }
        guard let u = reply.usage else { throw ModelError("main model returned no usage") }
        let t = reply.timings
        return ModelReply(
            content: choice.message.content ?? "",
            reasoning: choice.message.reasoning_content,
            usage: ModelUsage(promptTokens: u.prompt_tokens, completionTokens: u.completion_tokens,
                              prefilled: t?.prompt_n, cached: t?.cache_n, promptMs: t?.prompt_ms,
                              predictedMs: t?.predicted_ms, requestBytes: bytes))
    }
}
