import Foundation
import NaturalLanguage

/// M3a memory (spec 6): the markdown files under `<memory.dir>/<secret_dir>/` and
/// `<memory.dir>/<public_dir>/` are the source of truth. The SQLite tables in `Store`
/// (FTS5 trigram + embedding blobs + metadata) are only an index, rebuildable with
/// `rebuild()`. No fallback: if the embedding model is unavailable, that is an error.
public struct MemoryError: Error, CustomStringConvertible {
    public let description: String
}

/// Where a record's content came from; decides its starting confidence (config `memory.confidence_*`).
public enum SourceTier: String, Sendable, CaseIterable {
    case owner       // the owner's own words
    case tool        // command / job output
    case stranger    // someone other than the owner (no such input until M6; reserved)
    case doll        // her own guess
}

public struct MemoryHit: Sendable {
    public var id: String
    public var folder: String       // the config directory name: "secret" | "public"
    public var secret: Bool
    public var confidence: Double
    public var level: String        // 確定 / 大概 / 聽說, from the confidence thresholds
    /// nil only for records written before M3b (front-matter `kind = ""`).
    public var kind: MemoryKind?
    public var text: String

    /// What she sees: the level and the kind together, 「確定的打算」「聽說的已發生」 (spec 6).
    public var label: String { kind.map { "\(level)的\($0.label)" } ?? "\(level)（kind 未判）" }
}

/// `NLContextualEmbedding`, mean-pooled over L2-normalized token vectors (Apple's
/// documented pooling for subword embeddings), then L2-normalized again.
final class Embedder {
    let language: NLLanguage
    private let model: NLContextualEmbedding

    init(language: String) throws {
        guard #available(macOS 13.0, *) else {
            throw MemoryError(description: "NLContextualEmbedding is unavailable on this system")
        }
        let lang = NLLanguage(rawValue: language)
        guard let m = NLContextualEmbedding(language: lang) else {
            throw MemoryError(description: "no contextual embedding model for \(language)")
        }
        guard m.hasAvailableAssets else {
            throw MemoryError(description: "embedding model assets for \(language) are not on this device")
        }
        do { try m.load() } catch {
            throw MemoryError(description: "cannot load the embedding model for \(language): \(error)")
        }
        self.language = lang
        model = m
    }

    func embed(_ s: String) throws -> [Float] {
        guard #available(macOS 13.0, *) else {
            throw MemoryError(description: "NLContextualEmbedding is unavailable on this system")
        }
        let result: NLContextualEmbeddingResult
        do { result = try model.embeddingResult(for: s, language: language) } catch {
            throw MemoryError(description: "embedding failed: \(error)")
        }
        var sums: [Double] = []
        result.enumerateTokenVectors(in: result.string.startIndex..<result.string.endIndex) { token, _ in
            let norm = token.reduce(0) { $0 + Double($1) * Double($1) }.squareRoot()
            guard norm > 0 else { return false }
            if sums.isEmpty { sums = token.map { Double($0) / norm } }
            else { for i in 0..<token.count { sums[i] += Double(token[i]) / norm } }
            return false
        }
        guard !sums.isEmpty else { throw MemoryError(description: "embedding of an empty string") }
        let n = Double(sums.count)
        let mean = sums.map { $0 / n }
        let norm = mean.reduce(0) { $0 + $1 * $1 }.squareRoot()
        guard norm > 0 else { throw MemoryError(description: "embedding of an unembeddable string") }
        return mean.map { Float($0 / norm) }
    }
}

public final class Memory {
    let config: MemoryConfig
    private let root: URL
    private let store: Store
    private let embedder: Embedder

    init(config: MemoryConfig, root: URL, store: Store) throws {
        self.config = config
        self.root = root
        self.store = store
        embedder = try Embedder(language: config.language)
        let fm = FileManager.default
        for dir in [config.secretDir, config.publicDir] {
            try fm.createDirectory(at: root.appending(path: dir), withIntermediateDirectories: true)
        }
    }

    static func blob(_ v: [Float]) -> Data {
        Data(bytes: v, count: v.count * MemoryLayout<Float>.size)
    }

    private func directory(_ secret: Bool) -> URL {
        root.appending(path: secret ? config.secretDir : config.publicDir)
    }

    private static func hex(_ n: Int) -> String {
        String(UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased().prefix(n))
    }

    // markdown files ----------------------------------------------------------
    /// One record = one file: a `+++` front-matter block, then the text verbatim.
    private static func fileText(sourceSeq: Int64, ts: Double, confidence: Double, kind: String, tier: SourceTier, body: String) -> String {
        """
        +++
        kind = "\(kind)"
        confidence = \(confidence)
        created_ts = \(ts)
        source_event_seq = \(sourceSeq)
        source_tier = "\(tier.rawValue)"
        +++
        \(body)
        """
    }

    static func parseFile(_ text: String) throws -> (sourceSeq: Int64, ts: Double, confidence: Double, kind: String, tier: SourceTier, body: String) {
        let lines = text.components(separatedBy: "\n")
        guard lines.first == "+++", let end = lines.dropFirst().firstIndex(of: "+++") else {
            throw MemoryError(description: "no +++ front-matter block")
        }
        var seq: Int64 = 0, ts = 0.0, confidence = 0.0, kind = "", tier = SourceTier.doll
        for line in lines[1..<end] {
            let parts = line.split(separator: "=", maxSplits: 1, omittingEmptySubsequences: false)
            guard parts.count == 2 else { continue }
            let key = parts[0].trimmingCharacters(in: .whitespaces)
            let value = parts[1].trimmingCharacters(in: .whitespaces)
            let bare = value.hasPrefix("\"") && value.hasSuffix("\"") && value.count >= 2
                ? String(value.dropFirst().dropLast()) : value
            switch key {
            case "kind": kind = bare
            case "confidence": confidence = Double(bare) ?? 0
            case "created_ts": ts = Double(bare) ?? 0
            case "source_event_seq": seq = Int64(bare) ?? 0
            case "source_tier": tier = SourceTier(rawValue: bare) ?? .doll
            default: break
            }
        }
        let body = lines[(end + 1)...].joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
        return (seq, ts, confidence, kind, tier, body)
    }

    @discardableResult
    public func write(_ text: String, secret: Bool, sourceSeq: Int64, tier: SourceTier, kind: MemoryKind) throws -> String {
        let body = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !body.isEmpty else { throw MemoryError(description: "【記】 with no text") }
        guard let confidence = config.confidence[tier] else {
            throw MemoryError(description: "no starting confidence configured for tier \(tier.rawValue)")
        }
        let ts = Date().timeIntervalSince1970
        let id = "\(sourceSeq)-\(Self.hex(6))"
        let folder = secret ? config.secretDir : config.publicDir
        let url = directory(secret).appending(path: "\(id).md")
        try Self.fileText(sourceSeq: sourceSeq, ts: ts, confidence: confidence, kind: kind.rawValue, tier: tier, body: body)
            .write(to: url, atomically: true, encoding: .utf8)
        let vector = try embedder.embed(body)
        try store.putMemoryIndex(id: id, path: url.path, folder: folder, secret: secret, confidence: confidence,
                                ts: ts, sourceSeq: sourceSeq, kind: kind.rawValue, text: body, vector: Self.blob(vector))
        return id
    }

    /// Number of records on disk (the source of truth).
    private func fileCount() -> Int {
        let fm = FileManager.default
        return [true, false].reduce(0) { n, secret in
            n + ((try? fm.contentsOfDirectory(atPath: directory(secret).path)) ?? [])
                .filter { $0.hasSuffix(".md") }.count
        }
    }

    /// Called at boot: files written by a previous process (or by hand) are not in the index,
    /// and a new process would not find them. If the counts disagree, the whole index is
    /// rebuilt — records are small and few, a full rebuild is cheaper than tracking which are missing.
    func syncOnBoot() throws {
        if fileCount() != (try store.countMemoryIndex()) { try rebuild() }
    }

    /// Drops every index row and re-reads the markdown files (spec 6: SQLite is a rebuildable index).
    public func rebuild() throws {
        try store.clearMemoryIndex()
        let fm = FileManager.default
        for secret in [true, false] {
            let dir = directory(secret)
            let folder = secret ? config.secretDir : config.publicDir
            guard let names = try? fm.contentsOfDirectory(atPath: dir.path) else { continue }
            for name in names.sorted() where name.hasSuffix(".md") {
                let url = dir.appending(path: name)
                let raw = try String(contentsOf: url, encoding: .utf8)
                let parsed = try Self.parseFile(raw)
                guard !parsed.body.isEmpty else { continue }
                let id = String(name.dropLast(3))
                let vector = try embedder.embed(parsed.body)
                try store.putMemoryIndex(id: id, path: url.path, folder: folder, secret: secret, confidence: parsed.confidence,
                                        ts: parsed.ts, sourceSeq: parsed.sourceSeq, kind: parsed.kind,
                                        text: parsed.body, vector: Self.blob(vector))
            }
        }
    }

    // retrieval ---------------------------------------------------------------
    /// Phrases for the FTS5 trigram index: contiguous CJK runs become every 3-character
    /// window (trigrams), Latin/digit runs of 3+ stay whole. Anything shorter cannot match
    /// a trigram index. Phrases are quoted, OR-joined at the call site.
    static func queryPhrases(_ query: String, maxPhrases: Int) -> [String] {
        var out: [String] = []
        var latin = ""
        var cjk = ""
        func flushLatin() { if latin.count >= 3 { out.append(latin.lowercased()) }; latin = "" }
        func flushCJK() {
            if cjk.count >= 3 {
                let chars = Array(cjk)
                for i in 0...(chars.count - 3) { out.append(String(chars[i..<(i + 3)])) }
            }
            cjk = ""
        }
        for scalar in query.unicodeScalars {
            if scalar.value >= 0x2E80 && scalar.value <= 0x9FFF {
                flushLatin()
                cjk.unicodeScalars.append(scalar)
            } else if scalar.isASCII, Character(scalar).isLetter || Character(scalar).isNumber {
                flushCJK()
                latin.unicodeScalars.append(scalar)
            } else {
                flushLatin()
                flushCJK()
            }
        }
        flushLatin()
        flushCJK()
        var seen = Set<String>()
        var unique: [String] = []
        for p in out where seen.insert(p).inserted { unique.append(p) }
        return Array(unique.prefix(maxPhrases))
    }

    public func search(_ query: String, secretVisible: Bool) throws -> [MemoryHit] {
        var ftsRanked: [String] = []
        let phrases = Self.queryPhrases(query, maxPhrases: config.maxQueryPhrases)
        if !phrases.isEmpty {
            let match = phrases.map { "\"\($0.replacingOccurrences(of: "\"", with: "\"\""))\"" }.joined(separator: " OR ")
            ftsRanked = try store.memoryFTS(match: match, includeSecret: secretVisible, limit: config.maxCandidates)
        }
        let qv = try embedder.embed(query)
        let scored = try store.memoryVectors(includeSecret: secretVisible).map { row -> (String, Double) in
            var dot = 0.0
            row.vector.withUnsafeBytes { raw in
                let v = raw.bindMemory(to: Float.self)
                for i in 0..<min(v.count, qv.count) { dot += Double(v[i]) * Double(qv[i]) }
            }
            return (row.id, dot)
        }
        let vectorRanked = scored.sorted { $0.1 > $1.1 }.prefix(config.maxCandidates).map(\.0)

        // RRF over both lists, then confidence as an extra weighted term (spec 6).
        var fused: [String: Double] = [:]
        for (rank, id) in ftsRanked.enumerated() { fused[id, default: 0] += 1.0 / Double(config.rrfK + rank + 1) }
        for (rank, id) in vectorRanked.enumerated() { fused[id, default: 0] += 1.0 / Double(config.rrfK + rank + 1) }
        var hits: [MemoryHit] = []
        for (id, var score) in fused {
            guard let meta = try store.memoryMeta(id) else { continue }
            if meta.secret && !secretVisible { continue }
            score += config.confidenceWeight * meta.confidence
            guard let body = try? String(contentsOfFile: meta.path, encoding: .utf8) else { continue }
            let parsed = try Self.parseFile(body)
            guard !parsed.body.isEmpty else { continue }
            let level = meta.confidence >= config.highConfidence ? "確定"
                : meta.confidence >= config.mediumConfidence ? "大概" : "聽說"
            hits.append(MemoryHit(id: id, folder: meta.folder, secret: meta.secret, confidence: meta.confidence,
                                  level: level, kind: MemoryKind(rawValue: parsed.kind), text: parsed.body))
            fused[id] = score
        }
        return hits.sorted {
            let a = fused[$0.id] ?? 0, b = fused[$1.id] ?? 0
            return a != b ? a > b : $0.id < $1.id
        }.prefix(config.topK).map { $0 }
    }
}
