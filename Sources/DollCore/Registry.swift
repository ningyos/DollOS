import Foundation

/// The entity registry (2026-11-11): every concrete thing her memories name (an app, a file, a
/// shortcut, a person…) is one entry with a canonical name, aliases (other spellings: another
/// language, an abbreviation, a path variant, a bundle id) and a description (what it is, where).
/// A memory's front-matter names entries by id, so two memories about one thing share an entity
/// even when their texts spell it differently; two things with one name (homonyms) are two entries,
/// told apart by their descriptions.
///
/// Like the memories, the files `<memory.dir>/<registry_dir>/<id>.md` are the source of truth;
/// the SQLite tables (keys, FTS5 trigram, embedding) are an index rebuilt from them.
///
/// Entries are written by the program for what it knows exactly (`Selector.programApp`,
/// `Selector.programShortcut`: exact keys, never another type's entry) and by consolidation's reply.
/// Her `remember` writes none (2026-11-15).
public struct RegistryEntry: Sendable, Equatable {
    public var id: String
    public var type: EntityType
    /// The canonical name.
    public var name: String
    public var aliases: [String]
    public var description: String
    public var ts: Double

    /// The keys it is found by exactly: its name's and every alias's (`Registry.key`).
    public var keys: [String] {
        var seen = Set<String>()
        return ([name] + aliases).map(Registry.key).filter { !$0.isEmpty && seen.insert($0).inserted }
    }

    /// One line: what lookups index (FTS and embedding) and what a model is shown.
    public var line: String {
        let a = aliases.isEmpty ? "" : "；別名：" + aliases.joined(separator: "、")
        return "\(name)（\(type.rawValue)\(a)）：\(description)"
    }

    /// The file's bytes; `Registry.parseFile` of them gives this value back.
    public var text: String {
        (["+++", "type = \"\(type.rawValue)\"", "name = " + MemoryFile.json(name), "aliases = " + Registry.json(aliases),
          "created_ts = \(ts)", "+++", description]).joined(separator: "\n")
    }
}

public final class Registry {
    let dir: URL
    private let store: Store
    private let embedder: any Embedder
    private let maxQueryPhrases: Int
    private let rrfK: Int

    init(dir: URL, store: Store, embedder: any Embedder, config: MemoryConfig) throws {
        self.dir = dir
        self.store = store
        self.embedder = embedder
        maxQueryPhrases = config.maxQueryPhrases
        rrfK = config.rrfK
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }

    /// What names are matched exactly by: compatibility-normalized (full-width and half-width alike),
    /// lowercased, without any whitespace.
    public static func key(_ name: String) -> String {
        String(name.precomposedStringWithCompatibilityMapping.lowercased().unicodeScalars
            .filter { !CharacterSet.whitespacesAndNewlines.contains($0) }.map(Character.init))
    }

    static func json(_ list: [String]) -> String {
        let enc = JSONEncoder()
        enc.outputFormatting = .withoutEscapingSlashes
        return String(decoding: (try? enc.encode(list)) ?? Data("[]".utf8), as: UTF8.self)
    }

    static func parseFile(_ text: String, id: String) throws -> RegistryEntry {
        func bad(_ why: String) -> MemoryError { MemoryError(description: "registry entry \(id): \(why)") }
        let lines = text.components(separatedBy: "\n")
        guard lines.first == "+++", let end = lines.dropFirst().firstIndex(of: "+++") else { throw bad("no +++ front-matter block") }
        var type: EntityType?, name: String?, aliases: [String]?, ts = 0.0
        for line in lines[1..<end] {
            let parts = line.split(separator: "=", maxSplits: 1, omittingEmptySubsequences: false)
            guard parts.count == 2 else { continue }
            let value = parts[1].trimmingCharacters(in: .whitespaces)
            switch parts[0].trimmingCharacters(in: .whitespaces) {
            case "type": type = EntityType(rawValue: value.trimmingCharacters(in: CharacterSet(charactersIn: "\"")))
            case "name": name = try? JSONDecoder().decode(String.self, from: Data(value.utf8))
            case "aliases": aliases = try? JSONDecoder().decode([String].self, from: Data(value.utf8))
            case "created_ts": ts = Double(value) ?? 0
            default: break
            }
        }
        guard let type else { throw bad("type is missing or not one of \(EntityType.allCases.map(\.rawValue))") }
        guard let name, !key(name).isEmpty else { throw bad("name is missing or empty") }
        guard let aliases else { throw bad("aliases is missing or not a JSON array of strings") }
        let description = lines[(end + 1)...].joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
        guard !description.isEmpty else { throw bad("the description is empty") }
        return RegistryEntry(id: id, type: type, name: name, aliases: aliases, description: description, ts: ts)
    }

    private func url(_ id: String) -> URL { dir.appending(path: "\(id).md") }

    /// One entry by id; nil when there is no such file.
    public func entry(_ id: String) throws -> RegistryEntry? {
        let u = url(id)
        guard FileManager.default.fileExists(atPath: u.path) else { return nil }
        return try Self.parseFile(String(contentsOf: u, encoding: .utf8), id: id)
    }

    /// Every entry, by id.
    public func all() throws -> [RegistryEntry] {
        let names = (try? FileManager.default.contentsOfDirectory(atPath: dir.path)) ?? []
        return try names.filter { $0.hasSuffix(".md") }.sorted().map { name in
            try Self.parseFile(String(contentsOf: dir.appending(path: name), encoding: .utf8), id: String(name.dropLast(3)))
        }
    }

    func fileCount() -> Int {
        ((try? FileManager.default.contentsOfDirectory(atPath: dir.path)) ?? []).filter { $0.hasSuffix(".md") }.count
    }

    /// Re-indexes every file (the caller cleared the index), their lines embedded in one call.
    func rebuild() throws {
        let entries = try all()
        let vectors = try embedder.embed(entries.map(\.line), as: .document)
        for (e, v) in zip(entries, vectors) { try index(e, vector: v) }
    }

    private func index(_ e: RegistryEntry, vector: [Float]? = nil) throws {
        try store.putRegistryIndex(id: e.id, type: e.type.rawValue, keys: e.keys, text: e.line,
                                   vector: Memory.blob(try vector ?? embedder.embed(e.line, as: .document)))
    }

    /// One entry's index rows again from its file, or removed when the file is gone (the history put
    /// it back or took it away).
    func reindex(_ id: String) throws {
        if let e = try entry(id) { try index(e) } else { try store.deleteRegistryIndex(id: id) }
    }

    private func save(_ e: RegistryEntry) throws {
        try e.text.write(to: url(e.id), atomically: true, encoding: .utf8)
        try index(e)
    }

    /// A new entry. Aliases are trimmed; empty ones and those with the name's (or an earlier
    /// alias's) key are dropped.
    @discardableResult
    public func create(_ type: EntityType, name: String, aliases: [String], description: String) throws -> RegistryEntry {
        let name = name.trimmingCharacters(in: .whitespacesAndNewlines)
        let description = description.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !Self.key(name).isEmpty else { throw MemoryError(description: "registry entry with an empty name") }
        guard !description.isEmpty else { throw MemoryError(description: "registry entry \(name) with an empty description") }
        var seen: Set<String> = [Self.key(name)]
        let kept = aliases.map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { seen.insert(Self.key($0)).inserted && !Self.key($0).isEmpty }
        var id: String
        repeat { id = "e" + String(UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased().prefix(8)) }
        while FileManager.default.fileExists(atPath: url(id).path)
        let e = RegistryEntry(id: id, type: type, name: name, aliases: kept, description: description,
                              ts: Date().timeIntervalSince1970)
        try save(e)
        return e
    }

    /// Adds `alias` to entry `id` unless its name or an alias already has that key. Returns whether it did.
    @discardableResult
    public func addAlias(_ id: String, _ alias: String) throws -> Bool {
        guard var e = try entry(id) else { throw MemoryError(description: "no registry entry \(id)") }
        let a = alias.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !Self.key(a).isEmpty, !e.keys.contains(Self.key(a)) else { return false }
        e.aliases.append(a)
        try save(e)
        return true
    }

    /// The entries whose name or an alias has `name`'s key, by id.
    public func exact(_ name: String) throws -> [RegistryEntry] {
        let key = Self.key(name)
        guard !key.isEmpty else { return [] }
        return try store.registryByKey(key).compactMap { try entry($0) }
    }

    /// The entries `text` may mean, at most `limit`: those `exact` finds first, then the rest by
    /// RRF over FTS5 (trigram, over name, aliases and description) and cosine to `text`'s embedding.
    public func nearby(_ text: String, limit: Int) throws -> [RegistryEntry] {
        var out = try exact(text)
        guard out.count < limit, try store.countRegistryIndex() > 0 else { return Array(out.prefix(limit)) }
        var fused: [String: Double] = [:]
        let phrases = Memory.queryPhrases(text, maxPhrases: maxQueryPhrases)
        if !phrases.isEmpty {
            let match = phrases.map { "\"\($0.replacingOccurrences(of: "\"", with: "\"\""))\"" }.joined(separator: " OR ")
            for (rank, hit) in try store.registryFTS(match: match).enumerated() {
                fused[hit.id, default: 0] += 1.0 / Double(rrfK + rank + 1)
            }
        }
        let qv = try embedder.embed(text, as: .query)
        let byCosine = try store.registryVectors().map { ($0.id, Memory.dot($0.vector, qv)) }
            .sorted { $0.1 != $1.1 ? $0.1 > $1.1 : $0.0 < $1.0 }
        for (rank, (id, _)) in byCosine.enumerated() { fused[id, default: 0] += 1.0 / Double(rrfK + rank + 1) }
        let taken = Set(out.map(\.id))
        for (id, _) in fused.filter({ !taken.contains($0.key) }).sorted(by: { $0.value != $1.value ? $0.value > $1.value : $0.key < $1.key }) {
            guard out.count < limit else { break }
            if let e = try entry(id) { out.append(e) }
        }
        return out
    }
}
