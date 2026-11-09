import Foundation
import NaturalLanguage

/// M3a memory (spec 6): the markdown files under `<memory.dir>/<secret_dir>/`, `<own_dir>/` and
/// `<public_dir>/` are the source of truth. The SQLite tables in `Store`
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
    case program     // read by the program itself, no model in between (an app's menu shortcuts)
    case selfReported = "self_reported"   // her computer actions, kept after she said the session reached what she expected
}

/// What a memory says about its event (spec 6), given by her on `remember`. Facts and
/// preferences have none. Only `happened` counts as already happened.
public enum MemoryKind: String, Sendable, CaseIterable {
    case happened, intended, uncertain

    /// How recall shows it after the confidence level: 「確定的打算」「聽說的已發生」.
    public var label: String {
        switch self {
        case .happened: "已發生"
        case .intended: "打算"
        case .uncertain: "未證實"
        }
    }
}

/// Which layer a memory is in (spec 6, 2026-11-08). The directory names come from config.
public enum MemoryFolder: String, Sendable, CaseIterable {
    /// The owner's secrets.
    case secret
    /// Her own secrets: her passwords, accounts, tokens.
    case own
    case `public`
}

/// Whose turn a recall runs in; decides the folders it reads (`Memory.visible`).
public enum Audience: Sendable {
    /// Answering the owner.
    case owner
    /// Her own work, woken by something she set up (a watch).
    case doll
    /// Answering someone other than the owner (no such input until M6).
    case stranger
}

public struct MemoryHit: Sendable {
    public var id: String
    public var folder: String       // the config directory name of secret | own | public
    public var secret: Bool         // in the owner's secret folder
    public var confidence: Double
    public var level: String        // 確定 / 大概 / 聽說, from the confidence thresholds
    /// nil when she gave none (front-matter `kind = ""`): a fact or a preference.
    public var kind: MemoryKind?
    public var text: String

    /// What she sees: the level and the kind together, 「確定的打算」「聽說的已發生」, or the level alone (spec 6).
    public var label: String { kind.map { "\(level)的\($0.label)" } ?? level }
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
        for dir in [config.secretDir, config.ownDir, config.publicDir] {
            try fm.createDirectory(at: root.appending(path: dir), withIntermediateDirectories: true)
        }
    }

    static func blob(_ v: [Float]) -> Data {
        Data(bytes: v, count: v.count * MemoryLayout<Float>.size)
    }

    func dirName(_ f: MemoryFolder) -> String {
        switch f {
        case .secret: config.secretDir
        case .own: config.ownDir
        case .public: config.publicDir
        }
    }

    private func directory(_ f: MemoryFolder) -> URL { root.appending(path: dirName(f)) }

    /// Which folders a turn may read (spec 6, 2026-11-08). `secret` keeps the 5.1 rule: only a turn
    /// that already saw a secret event, and never a stranger's. `own` (her own passwords, tokens) is
    /// for the owner's turns and her own, never a stranger's. `public` is for everyone.
    public static func visible(_ audience: Audience, sawSecret: Bool) -> Set<MemoryFolder> {
        switch audience {
        case .stranger: [.public]
        case .owner, .doll: sawSecret ? [.public, .own, .secret] : [.public, .own]
        }
    }

    private static func hex(_ n: Int) -> String {
        String(UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased().prefix(n))
    }

    // markdown files ----------------------------------------------------------
    /// One record = one file: a `+++` front-matter block, then the text verbatim. A record about one
    /// app at one version (a menu shortcut the program read) also names the app's bundle id and version.
    private static func fileText(sourceSeq: Int64, ts: Double, confidence: Double, kind: String, tier: SourceTier,
                                 app: AppIdentity?, body: String) -> String {
        let appLines = app.map { "app = \"\($0.bundleID)\"\napp_version = \"\($0.version)\"\n" } ?? ""
        return """
        +++
        kind = "\(kind)"
        confidence = \(confidence)
        created_ts = \(ts)
        source_event_seq = \(sourceSeq)
        source_tier = "\(tier.rawValue)"
        \(appLines)+++
        \(body)
        """
    }

    static func parseFile(_ text: String) throws -> (sourceSeq: Int64, ts: Double, confidence: Double, kind: String, tier: SourceTier,
                                                     app: String, appVersion: String, body: String) {
        let lines = text.components(separatedBy: "\n")
        guard lines.first == "+++", let end = lines.dropFirst().firstIndex(of: "+++") else {
            throw MemoryError(description: "no +++ front-matter block")
        }
        var seq: Int64 = 0, ts = 0.0, confidence = 0.0, kind = "", tier = SourceTier.doll, app = "", appVersion = ""
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
            case "app": app = bare
            case "app_version": appVersion = bare
            default: break
            }
        }
        let body = lines[(end + 1)...].joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
        return (seq, ts, confidence, kind, tier, app, appVersion, body)
    }

    /// Bundle id → version of every app whose shortcuts are in her public memories (front-matter `app`).
    func learnedApps() throws -> [String: String] {
        var out: [String: String] = [:]
        for (_, parsed) in try appRecords() { out[parsed.app] = parsed.appVersion }
        return out
    }

    /// Deletes every public record about `bundleID` (all versions), files and index rows. Returns how many.
    @discardableResult
    func forgetApp(_ bundleID: String) throws -> Int {
        let gone = try appRecords().filter { $0.1.app == bundleID }
        for (url, _) in gone {
            try FileManager.default.removeItem(at: url)
            try store.deleteMemoryIndex(id: String(url.lastPathComponent.dropLast(3)))
        }
        return gone.count
    }

    private func appRecords() throws -> [(URL, (sourceSeq: Int64, ts: Double, confidence: Double, kind: String, tier: SourceTier,
                                                app: String, appVersion: String, body: String))] {
        let dir = directory(.public)
        let names = (try? FileManager.default.contentsOfDirectory(atPath: dir.path)) ?? []
        return try names.sorted().filter { $0.hasSuffix(".md") }.compactMap { name in
            let url = dir.appending(path: name)
            let parsed = try Self.parseFile(String(contentsOf: url, encoding: .utf8))
            return parsed.app.isEmpty ? nil : (url, parsed)
        }
    }


    @discardableResult
    public func write(_ text: String, folder f: MemoryFolder, sourceSeq: Int64, tier: SourceTier, kind: MemoryKind?,
                      app: AppIdentity? = nil) throws -> String {
        let body = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !body.isEmpty else { throw MemoryError(description: "remember item with no text") }
        guard let confidence = config.confidence[tier] else {
            throw MemoryError(description: "no starting confidence configured for tier \(tier.rawValue)")
        }
        let ts = Date().timeIntervalSince1970
        let id = "\(sourceSeq)-\(Self.hex(6))"
        let folder = dirName(f)
        let url = directory(f).appending(path: "\(id).md")
        try Self.fileText(sourceSeq: sourceSeq, ts: ts, confidence: confidence, kind: kind?.rawValue ?? "", tier: tier, app: app, body: body)
            .write(to: url, atomically: true, encoding: .utf8)
        let vector = try embedder.embed(body)
        try store.putMemoryIndex(id: id, path: url.path, folder: folder, confidence: confidence,
                                ts: ts, sourceSeq: sourceSeq, kind: kind?.rawValue ?? "", text: body, vector: Self.blob(vector))
        return id
    }

    /// Number of records on disk (the source of truth).
    private func fileCount() -> Int {
        let fm = FileManager.default
        return MemoryFolder.allCases.reduce(0) { n, f in
            n + ((try? fm.contentsOfDirectory(atPath: directory(f).path)) ?? [])
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
        for f in MemoryFolder.allCases {
            let dir = directory(f)
            let folder = dirName(f)
            guard let names = try? fm.contentsOfDirectory(atPath: dir.path) else { continue }
            for name in names.sorted() where name.hasSuffix(".md") {
                let url = dir.appending(path: name)
                let raw = try String(contentsOf: url, encoding: .utf8)
                let parsed = try Self.parseFile(raw)
                guard !parsed.body.isEmpty else { continue }
                let id = String(name.dropLast(3))
                let vector = try embedder.embed(parsed.body)
                try store.putMemoryIndex(id: id, path: url.path, folder: folder, confidence: parsed.confidence,
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

    /// Searches only the folders `visible` lets this turn read (`Memory.visible`); at most `limit`
    /// hits (default `top_k`). With `tiers`, only records of those source tiers: every record is
    /// ranked by vector, not just the first `max_candidates`, so a few among many others are not lost.
    public func search(_ query: String, visible: Set<MemoryFolder>, limit: Int? = nil,
                       tiers: Set<SourceTier>? = nil) throws -> [MemoryHit] {
        let names = MemoryFolder.allCases.filter(visible.contains).map(dirName)
        guard !names.isEmpty else { return [] }
        var ftsRanked: [String] = []
        let phrases = Self.queryPhrases(query, maxPhrases: config.maxQueryPhrases)
        if !phrases.isEmpty {
            let match = phrases.map { "\"\($0.replacingOccurrences(of: "\"", with: "\"\""))\"" }.joined(separator: " OR ")
            ftsRanked = try store.memoryFTS(match: match, folders: names, limit: config.maxCandidates)
        }
        let qv = try embedder.embed(query)
        let scored = try store.memoryVectors(folders: names).map { row -> (String, Double) in
            var dot = 0.0
            row.vector.withUnsafeBytes { raw in
                let v = raw.bindMemory(to: Float.self)
                for i in 0..<min(v.count, qv.count) { dot += Double(v[i]) * Double(qv[i]) }
            }
            return (row.id, dot)
        }
        let vectorRanked = scored.sorted { $0.1 > $1.1 }.prefix(tiers == nil ? config.maxCandidates : scored.count).map(\.0)

        // RRF over both lists, then confidence as an extra weighted term (spec 6).
        var fused: [String: Double] = [:]
        for (rank, id) in ftsRanked.enumerated() { fused[id, default: 0] += 1.0 / Double(config.rrfK + rank + 1) }
        for (rank, id) in vectorRanked.enumerated() { fused[id, default: 0] += 1.0 / Double(config.rrfK + rank + 1) }
        var hits: [MemoryHit] = []
        for (id, var score) in fused {
            guard let meta = try store.memoryMeta(id) else { continue }
            if !names.contains(meta.folder) { continue }
            score += config.confidenceWeight * meta.confidence
            guard let body = try? String(contentsOfFile: meta.path, encoding: .utf8) else { continue }
            let parsed = try Self.parseFile(body)
            guard !parsed.body.isEmpty, tiers?.contains(parsed.tier) ?? true else { continue }
            let level = meta.confidence >= config.highConfidence ? "確定"
                : meta.confidence >= config.mediumConfidence ? "大概" : "聽說"
            hits.append(MemoryHit(id: id, folder: meta.folder, secret: meta.folder == config.secretDir,
                                  confidence: meta.confidence, level: level,
                                  kind: MemoryKind(rawValue: parsed.kind), text: parsed.body))
            fused[id] = score
        }
        return hits.sorted {
            let a = fused[$0.id] ?? 0, b = fused[$1.id] ?? 0
            return a != b ? a > b : $0.id < $1.id
        }.prefix(limit ?? config.topK).map { $0 }
    }
}
