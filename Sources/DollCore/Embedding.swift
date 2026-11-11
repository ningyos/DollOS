import Foundation
import HuggingFace
import MLX
import MLXEmbedders
import MLXLMCommon
import Tokenizers

/// What a text is to the embedding model: a query (what to find; it gets the instruction) or a
/// document (what is found; embedded as is).
public enum EmbeddingRole: Sendable {
    case query, document
}

/// The memory embedding (2026-11-11): unit vectors, so a dot product is the cosine. Memories,
/// registry entries and today's events are documents; what is searched for is a query.
public protocol Embedder: AnyObject, Sendable {
    /// One unit vector per text, in order.
    func embed(_ texts: [String], as role: EmbeddingRole) throws -> [[Float]]
}

extension Embedder {
    func embed(_ text: String, as role: EmbeddingRole) throws -> [Float] {
        try embed([text], as: role)[0]
    }
}

/// Qwen3-Embedding (`[memory] embedding_model`) on MLX: last-token pooling over the text and an
/// appended `<|endoftext|>`, L2-normalized; a query is `Instruct: <instruction>\nQuery:<text>`
/// (the model card's format), a document has no prefix. Weights come from the Hugging Face cache
/// (`~/.cache/huggingface/hub`, or `HF_HUB_CACHE` / `HF_HOME`), downloaded into it the first time.
/// No fallback: a model that does not load is a boot error.
public final class MLXEmbedder: Embedder, @unchecked Sendable {
    /// MLX state is not thread-safe: one embedding at a time.
    private let lock = NSLock()
    private let context: EmbedderModelContext
    private let instruction: String
    private let maxTokens: Int
    private let endToken: Int
    /// Texts per forward pass.
    private static let batch = 16

    private init(context: EmbedderModelContext, instruction: String, maxTokens: Int, endToken: Int) {
        self.context = context
        self.instruction = instruction
        self.maxTokens = maxTokens
        self.endToken = endToken
    }

    /// Loads the model named by `config.embeddingModel` (a Hugging Face id, or an absolute path to
    /// its directory) and runs one embedding, so the first real one does not pay for the warm-up.
    public static func load(_ config: MemoryConfig) async throws -> MLXEmbedder {
        // SwiftPM builds MLX's Metal kernels into mlx-swift_Cmlx.bundle beside this code's binary.
        // MLX finds it there for `swift run`, but not under `swift test` (the tests run inside the
        // toolchain's helper process; mlx-swift 0.32): name it explicitly whenever it is there.
        if GPU.metallib == nil, let lib = Bundle(for: MLXEmbedder.self).resourceURL?
            .appending(path: "mlx-swift_Cmlx.bundle/Contents/Resources/default.metallib"),
            FileManager.default.fileExists(atPath: lib.path) {
            GPU.metallib = lib
        }
        let name = config.embeddingModel
        let directory = try await modelDirectory(name)
        let context: EmbedderModelContext
        do {
            context = try await EmbedderModelFactory.shared.load(from: directory, using: TransformersTokenizerLoader())
        } catch {
            throw MemoryError(description: "cannot load the embedding model \(name): \(error)")
        }
        guard context.pooling.strategy == .last else {
            throw MemoryError(description: "embedding model \(name): pooling is \(context.pooling.strategy), want last-token (Qwen3-Embedding)")
        }
        guard let end = context.tokenizer.convertTokenToId("<|endoftext|>") else {
            throw MemoryError(description: "embedding model \(name): the tokenizer has no <|endoftext|>")
        }
        let embedder = MLXEmbedder(context: context, instruction: config.embeddingQueryInstruction,
                                   maxTokens: config.embeddingMaxTokens, endToken: end)
        _ = try embedder.embed(["暖機"], as: .query)
        return embedder
    }

    /// The model's files: `name` itself when it is a path, else its snapshot in the Hugging Face
    /// cache, downloaded when the cache does not have it.
    private static func modelDirectory(_ name: String) async throws -> URL {
        if name.hasPrefix("/") { return URL(filePath: name) }
        guard let repo = Repo.ID(rawValue: name) else {
            throw MemoryError(description: "embedding_model \(name) is neither an absolute path nor a Hugging Face id")
        }
        let files = ["*.safetensors", "*.json", "merges.txt", "vocab.json"]
        let hub = HubClient()
        if let cached = try? await hub.downloadSnapshot(of: repo, matching: files, localFilesOnly: true) {
            return cached
        }
        Log.write("INFO", "downloading the embedding model \(name) into the Hugging Face cache")
        do {
            return try await hub.downloadSnapshot(of: repo, matching: files)
        } catch {
            throw MemoryError(description: "cannot download the embedding model \(name): \(error)")
        }
    }

    /// The token ids of one text: at most `maxTokens` of its own, then `<|endoftext|>` (whose
    /// hidden state is the embedding).
    func tokens(_ text: String, _ role: EmbeddingRole) -> [Int] {
        let input = role == .query ? "Instruct: \(instruction)\nQuery:\(text)" : text
        var ids = context.tokenizer.encode(text: input, addSpecialTokens: false)
        if ids.count > maxTokens { ids.removeLast(ids.count - maxTokens) }
        return ids + [endToken]
    }

    public func embed(_ texts: [String], as role: EmbeddingRole) throws -> [[Float]] {
        lock.lock()
        defer { lock.unlock() }
        var out: [[Float]] = []
        out.reserveCapacity(texts.count)
        for start in stride(from: 0, to: texts.count, by: Self.batch) {
            let inputs = texts[start..<min(start + Self.batch, texts.count)].map { tokens($0, role) }
            let width = inputs.map(\.count).max()!
            // Padded at the end; the mask keeps padding out of attention and the pooling takes each
            // row's last real token.
            let ids = MLXArray(inputs.flatMap { $0 + Array(repeating: endToken, count: width - $0.count) }.map(Int32.init),
                               [inputs.count, width])
            let mask = MLXArray(inputs.flatMap { Array(repeating: Int32(1), count: $0.count)
                + Array(repeating: Int32(0), count: width - $0.count) }, [inputs.count, width])
            let pooled = context.pooling(context.model(ids, positionIds: nil, tokenTypeIds: nil, attentionMask: mask),
                                         mask: mask).asType(.float32)
            // Normalized in float32: in the weights' bfloat16 a text's cosine to itself came out 1.003.
            let unit = pooled / MLX.sqrt(MLX.sum(pooled * pooled, axis: -1, keepDims: true))
            do { try checkedEval(unit) } catch {
                throw MemoryError(description: "embedding failed: \(error)")
            }
            let flat = unit.asArray(Float.self)
            let dim = unit.dim(1)
            for row in 0..<inputs.count { out.append(Array(flat[(row * dim)..<((row + 1) * dim)])) }
        }
        return out
    }
}

/// swift-transformers' tokenizer for MLX (what mlx-swift-lm's `#huggingFaceTokenizerLoader` expands to).
private struct TransformersTokenizerLoader: TokenizerLoader {
    func load(from directory: URL) async throws -> any MLXLMCommon.Tokenizer {
        TokenizerBridge(upstream: try await AutoTokenizer.from(modelFolder: directory))
    }
}

private struct TokenizerBridge: MLXLMCommon.Tokenizer {
    let upstream: any Tokenizers.Tokenizer

    func encode(text: String, addSpecialTokens: Bool) -> [Int] { upstream.encode(text: text, addSpecialTokens: addSpecialTokens) }
    func decode(tokenIds: [Int], skipSpecialTokens: Bool) -> String { upstream.decode(tokens: tokenIds, skipSpecialTokens: skipSpecialTokens) }
    func convertTokenToId(_ token: String) -> Int? { upstream.convertTokenToId(token) }
    func convertIdToToken(_ id: Int) -> String? { upstream.convertIdToToken(id) }
    var bosToken: String? { upstream.bosToken }
    var eosToken: String? { upstream.eosToken }
    var unknownToken: String? { upstream.unknownToken }

    func applyChatTemplate(messages: [[String: any Sendable]], tools: [[String: any Sendable]]?,
                           additionalContext: [String: any Sendable]?) throws -> [Int] {
        try upstream.applyChatTemplate(messages: messages, tools: tools, additionalContext: additionalContext)
    }
}
