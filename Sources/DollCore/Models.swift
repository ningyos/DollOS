import CryptoKit
import Foundation
import Synchronization

/// One chat-completions message. The timeline stores events, not messages;
/// messages are rendered from events every step.
public struct ChatMessage: Encodable, Sendable, Equatable {
    public var role: String
    /// Her assistant messages are her thought (the reply's text content) and her calls, each with the
    /// arguments text the server returned, so a re-render is byte-identical. A `role: "tool"` message
    /// is one call's result, in call order; the template renders it as a `<tool_response>` block and
    /// pairs results with calls by position, so no call id is sent.
    public var content: String
    /// Her calls in an assistant message, sent as `tool_calls`; only calls with arguments (the ones
    /// the server read as calls) are listed.
    public var toolCalls: [ToolCall]
    /// What follows `content` in the same message, in order: screenshots as `data:` URIs built from
    /// the stored files' bytes (so every turn re-sends the same bytes), and text (the crop's place,
    /// the front window's outline). With any, `content` goes out as a list of parts, text first.
    public var extra: [Extra]

    public enum Extra: Sendable, Equatable {
        case image(String)
        /// The template joins parts with nothing between them, so it goes out after a newline.
        case text(String)
    }

    public init(role: String, content: String, extra: [Extra] = [], toolCalls: [ToolCall] = []) {
        self.role = role
        self.content = content
        self.extra = extra
        self.toolCalls = toolCalls.filter { $0.arguments != nil }
    }

    /// The `data:` URIs among `extra`, in order.
    public var images: [String] { extra.compactMap { if case .image(let u) = $0 { u } else { nil } } }

    enum CodingKeys: String, CodingKey { case role, content, tool_calls }
    private struct Call: Encodable {
        struct Function: Encodable { let name: String; let arguments: String }
        let type = "function"
        let function: Function
    }
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
        if !toolCalls.isEmpty {
            try c.encode(toolCalls.map { Call(function: .init(name: $0.name, arguments: $0.arguments!)) }, forKey: .tool_calls)
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
    /// Why the server stopped generating (`choices[0].finish_reason`: "stop", "tool_calls", …); nil when
    /// the server sent none. Recorded per turn (2026-11-16: replies that shrank to nothing could not be
    /// explained because nothing kept this).
    public var finish: String?
    /// Characters of `reasoning_content` the server returned (0 when none): a reply whose visible text
    /// is empty may still have spent its tokens there.
    public var reasoningChars: Int

    public init(promptTokens: Int, completionTokens: Int, prefilled: Int? = nil, cached: Int? = nil,
                promptMs: Double? = nil, predictedMs: Double? = nil, requestBytes: Int? = nil,
                finish: String? = nil, reasoningChars: Int = 0) {
        self.promptTokens = promptTokens
        self.completionTokens = completionTokens
        self.prefilled = prefilled
        self.cached = cached
        self.promptMs = promptMs
        self.predictedMs = predictedMs
        self.requestBytes = requestBytes
        self.finish = finish
        self.reasoningChars = reasoningChars
    }

    /// One line for logs and transcripts: why it stopped and what it produced.
    public var stopLine: String {
        "finish=\(finish ?? "none") completion=\(completionTokens) reasoning_chars=\(reasoningChars)"
    }
}

public struct ModelReply: Sendable, Equatable {
    /// Her text content: her own thought (a call the server could not read stays in it as text).
    public var content: String
    public var reasoning: String?
    /// Her calls in the order she wrote them: the response's `tool_calls`, then one failed call per
    /// call the server returned as text (`ToolCall.unread`).
    public var calls: [ToolCall]
    public var usage: ModelUsage
    public init(content: String, reasoning: String? = nil, calls: [ToolCall] = [], usage: ModelUsage) {
        self.content = content
        self.reasoning = reasoning
        self.calls = calls
        self.usage = usage
    }
}

/// The main model: an OpenAI chat-completions endpoint.
public protocol MainModel: Sendable {
    func check() async throws
    /// `effort` is the Strata `reasoning_effort` value for this one call. `tools`: the request's
    /// `tools` as JSON text (an array), or nil for a request without tools.
    func chat(_ messages: [ChatMessage], tools: String?, effort: String) async throws -> ModelReply
    /// Whether its requests are waiting for the server to be reachable again (`ModelConnection`);
    /// nil for a model that never waits.
    var connection: ModelConnection? { get }
}

extension MainModel {
    public var connection: ModelConnection? { nil }
}

/// Whether the model server is reachable, as the requests found it (2026-11-22, owner: on a
/// connection problem she waits and retries; she does not exit). Shared by every request of one
/// `HTTPMainModel`: the first failure starts an outage, the next answer from the server ends it.
public final class ModelConnection: Sendable {
    private struct State {
        /// When the outage began, and its failed attempts so far; nil: no outage.
        var since: Double?
        var failures = 0
        /// Requests now waiting for their next attempt.
        var waiting = 0
        var onChange: (@Sendable () -> Void)?
    }
    private let state = Mutex(State())

    public init() {}

    /// While a request waits for the server: when the outage began; nil otherwise (an outage whose
    /// waiting requests were all cancelled shows nothing until the next request finds it).
    public var downSince: Double? { state.withLock { $0.waiting > 0 ? $0.since : nil } }

    /// Called when requests start or stop waiting (the dashboard refreshes).
    public func onChange(_ f: @escaping @Sendable () -> Void) { state.withLock { $0.onChange = f } }

    /// A request failed at `t`; `first`: its first failure (it starts waiting).
    func failed(at t: Double, first: Bool) {
        let changed = state.withLock { s -> (@Sendable () -> Void)? in
            s.failures += 1
            if s.since == nil { s.since = t }
            guard first else { return nil }
            s.waiting += 1
            return s.waiting == 1 ? s.onChange : nil
        }
        changed?()
    }

    /// A request that had failed stopped waiting (answered, cancelled or failed for good).
    func stoppedWaiting() {
        let changed = state.withLock { s -> (@Sendable () -> Void)? in
            s.waiting -= 1
            return s.waiting == 0 ? s.onChange : nil
        }
        changed?()
    }

    /// The server answered: the outage it ended (since when, failed attempts), or nil when there was none.
    func answered() -> (since: Double, failures: Int)? {
        state.withLock { s in
            guard let since = s.since else { return nil }
            defer { s.since = nil; s.failures = 0 }
            return (since, s.failures)
        }
    }
}

/// Which failures of a request mean the server cannot be reached for now (sent again after a
/// wait), as against a request the server refused or answered unreadably (an error, as before).
public enum Retry {
    /// The transport error's kind when it is a connection problem; nil otherwise. Connection refused
    /// is `cannotConnectToHost`; a Mac waking from sleep gives `notConnectedToInternet` or
    /// `networkConnectionLost` until its network is back.
    public static func kind(_ error: any Error) -> String? {
        guard let e = error as? URLError else { return nil }
        switch e.code {
        case .networkConnectionLost: return "connection lost"
        case .timedOut: return "timed out"
        case .cannotConnectToHost: return "cannot connect"
        case .notConnectedToInternet: return "no network"
        case .cannotFindHost, .dnsLookupFailed: return "host not found"
        default: return nil
        }
    }

    /// An HTTP status that says the server (or a proxy in front of it) is temporarily not answering:
    /// 502, 503, 504. Every other status is the server's answer to this request.
    public static func kind(status: Int) -> String? {
        [502, 503, 504].contains(status) ? "HTTP \(status)" : nil
    }

    /// The wait before attempt `failures + 1`: `first`, doubling, at most `max`.
    public static func wait(after failures: Int, first: Double, max: Double) -> Double {
        Swift.min(first * pow(2, Double(Swift.max(0, failures - 1))), max)
    }
}

/// DollOS does not know which model sits behind the URL. The only Strata dialect
/// field sent is `reasoning_effort`; `timings` and `strata_rejected_tool_calls` are read when present.
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
    /// The waits between attempts (tests: a manual clock).
    private let clock: any DollClock
    /// Its WARN and INFO lines (level, message); the process log by default.
    private let log: @Sendable (String, String) -> Void
    public let connection: ModelConnection? = ModelConnection()

    public init(_ cfg: LlmConfig, transport: Transport? = nil, clock: any DollClock = SystemClock(),
                log: (@Sendable (String, String) -> Void)? = nil) {
        self.cfg = cfg
        self.transport = transport ?? { req in
            let (d, resp) = try await URLSession.shared.data(for: req)
            return (d, (resp as! HTTPURLResponse).statusCode)
        }
        self.clock = clock
        self.log = log ?? { Log.write($0, $1) }
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
            struct Call: Decodable {
                struct Function: Decodable { let name: String; let arguments: String? }
                let function: Function
            }
            let content: String?
            let reasoning_content: String?
            let tool_calls: [Call]?
        }
        struct Rejected: Decodable { let name: String }
        struct Choice: Decodable { let message: Msg; let finish_reason: String? }
        struct Usage: Decodable { let prompt_tokens: Int; let completion_tokens: Int }
        struct Timings: Decodable {
            let prompt_n: Int?; let cache_n: Int?; let prompt_ms: Double?; let predicted_ms: Double?
        }
        let choices: [Choice]
        let usage: Usage?
        let timings: Timings?
        /// Strata (#3796): well-formed calls of a tool the request did not declare, returned as text.
        let strata_rejected_tool_calls: [Rejected]?
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

    /// Sends `req` until the server answers (2026-11-22): a connection problem (`Retry`) is followed
    /// by a wait (`[llm] retry_first_s`, doubling, at most `retry_max_s`) and the same request again,
    /// for as long as it lasts; cancellation (an interrupt, his voice, maintenance cut short, stop)
    /// ends the wait like the request. Only this exchange is repeated: the caller gets one reply, so
    /// nothing it did before the request (her tool calls) runs again. Each failure is one WARN line
    /// (its kind, attempt, next wait), the answer after an outage one INFO line; never message text.
    private func send(_ req: URLRequest) async throws -> (Data, Int) {
        var failures = 0
        defer { if failures > 0 { connection?.stoppedWaiting() } }
        while true {
            let kind: String
            do {
                let (data, status) = try await transport(req)
                if let k = Retry.kind(status: status) { kind = k } else {
                    if let ended = connection?.answered() {
                        log("INFO", String(format: "model connection back after %.0f s down (%d failed attempts)",
                                           clock.now() - ended.since, ended.failures))
                    }
                    return (data, status)
                }
            } catch {
                try Task.checkCancellation()
                guard let k = Retry.kind(error) else {
                    throw ModelError("main model request failed: \(error.localizedDescription)")
                }
                kind = k
            }
            failures += 1
            connection?.failed(at: clock.now(), first: failures == 1)
            let wait = Retry.wait(after: failures, first: cfg.retryFirstS, max: cfg.retryMaxS)
            log("WARN", String(format: "model request failed (%@), attempt %d; next try in %.0f s", kind, failures, wait))
            try await clock.sleep(until: clock.now() + wait)
        }
    }

    private func post(_ messages: [ChatMessage], tools: String?, effort: String, maxTokens: Int) async throws -> (Reply, Int) {
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
            var body = try ChatMessage.encoder.encode(Body(
                model: cfg.model, messages: sendable, max_tokens: maxTokens, reasoning_effort: effort))
            if let tools {
                // `tools` goes in as written (JSONValue keeps each tool's keys in their order); the
                // encoded body is one object, so it is spliced in before its closing brace.
                body = body.dropLast() + Data(",\"tools\":\(tools)}".utf8)
            }
            req.httpBody = body
            let bytes = req.httpBody!.count
            let (data, status) = try await send(req)
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

    public func chat(_ messages: [ChatMessage], tools: String?, effort: String) async throws -> ModelReply {
        let (reply, bytes) = try await post(messages, tools: tools, effort: effort, maxTokens: cfg.maxTokens)
        guard let choice = reply.choices.first else { throw ModelError("main model returned no choices") }
        if choice.finish_reason == "length" { throw ModelError("main model hit max_tokens") }
        guard let u = reply.usage else { throw ModelError("main model returned no usage") }
        let t = reply.timings
        let content = choice.message.content ?? ""
        let calls = (choice.message.tool_calls ?? []).map { ToolCall(name: $0.function.name, arguments: $0.function.arguments ?? "") }
            + ToolCall.unread(content: content, rejected: (reply.strata_rejected_tool_calls ?? []).map(\.name))
        return ModelReply(
            content: content,
            reasoning: choice.message.reasoning_content,
            calls: calls,
            usage: ModelUsage(promptTokens: u.prompt_tokens, completionTokens: u.completion_tokens,
                              prefilled: t?.prompt_n, cached: t?.cache_n, promptMs: t?.prompt_ms,
                              predictedMs: t?.predicted_ms, requestBytes: bytes, finish: choice.finish_reason,
                              reasoningChars: choice.message.reasoning_content?.count ?? 0))
    }
}
