import Foundation
import FoundationModels

/// The on-device model the memory pipeline uses (2026-11-11): entities out of a line of text, and
/// which candidate memories are useful in a situation. Production is Apple's Foundation Models
/// (`FoundationSelectionModel`); tests use a fake. No fallback: unavailable is a boot error.
public protocol SelectionModel: Sendable {
    /// Throws with the reason when the model cannot run.
    func check() async throws
    /// The entities `text` names (`EntityType`), names as written.
    func entities(in text: String) async throws -> [MemoryEntity]
    /// The positions (0-based) in `candidates` judged useful for `situation`, ascending.
    func useful(situation: String, candidates: [String]) async throws -> [Int]
}

public struct SelectionError: Error, CustomStringConvertible {
    public let description: String
    init(_ description: String) { self.description = description }
}

@Generable
struct FMEntity {
    @Guide(description: "種類", .anyOf(EntityType.allCases.map(\.rawValue)))
    var type: String
    @Guide(description: "名字，照原文寫")
    var name: String
}

@Generable
struct FMEntities {
    @Guide(description: "文字裡提到的具體東西；沒有就是空列表")
    var entities: [FMEntity]
}

@Generable
struct FMPicks {
    @Guide(description: "有用的候選的編號；都沒用就是空列表")
    var useful: [Int]
}

/// Apple's on-device `SystemLanguageModel.default`, one fresh session per request, greedy sampling,
/// guided generation.
public struct FoundationSelectionModel: SelectionModel {
    public init() {}

    static let entityInstructions = """
        從使用者給的一段文字，找出它提到的具體東西，每個標上種類：
        - app：應用程式，例如 TextEdit、Finder、Safari
        - ui：畫面上的元素、對話框、選單項目，例如「另存新檔」對話框、Save 按鈕、File 選單
        - shortcut：快捷鍵，例如 ⌘S、⇧⌘G
        - file：檔案、資料夾或路徑，例如 notes.txt、~/Documents、桌面
        - website：網站或網址
        - person：人
        - other：其他具體的東西
        名字照原文寫，不翻譯、不改寫。抽象的動作（打開、存檔）不算。沒有就回空列表。
        """

    /// Measured 2026-11-11 on the merged E/F memory: a first version (「幾條說同一件事的，只挑說得最完整
    /// 的一條」) kept exactly one candidate in every situation; this one keeps the skill, the
    /// experience and the record of the same task.
    static let selectInstructions = """
        你在幫一個 AI 助理挑記憶。她在用電腦幫主人做事。下面先是她現在的情況，然後是從她的記憶裡找到的候選，每條有編號。
        逐條判斷每一條：如果她接下來要做的事會用到它（做法、步驟、快捷鍵、要避開的失敗、之前做過的同一件事的經驗），就算有用；有用的常常不只一條。只有跟這件事無關的、或跟另一條有用的說的完全一樣的，才不算。
        回答所有有用的候選編號；都沒用就回空列表。
        """

    public func check() async throws {
        switch SystemLanguageModel.default.availability {
        case .available: return
        case .unavailable(let reason):
            throw SelectionError("Apple Foundation Models (on-device) is unavailable: \(reason); the memory pipeline needs it")
        }
    }

    static let options = GenerationOptions(samplingMode: .greedy)

    public func entities(in text: String) async throws -> [MemoryEntity] {
        let session = LanguageModelSession(instructions: Self.entityInstructions)
        let reply: FMEntities
        do {
            reply = try await session.respond(to: text, generating: FMEntities.self, options: Self.options).content
        } catch {
            throw SelectionError("on-device entity extraction failed: \(error)")
        }
        return MemoryEntity.unique(reply.entities.compactMap { e in
            EntityType(rawValue: e.type).map { MemoryEntity($0, e.name.trimmingCharacters(in: .whitespacesAndNewlines)) }
        })
    }

    public func useful(situation: String, candidates: [String]) async throws -> [Int] {
        let prompt = Selection.prompt(situation: situation, candidates: candidates)
        let session = LanguageModelSession(instructions: Self.selectInstructions)
        let reply: FMPicks
        do {
            reply = try await session.respond(to: prompt, generating: FMPicks.self, options: Self.options).content
        } catch {
            throw SelectionError("on-device selection failed: \(error)")
        }
        // Numbers in the prompt start at 1; any outside the batch is dropped.
        return Array(Set(reply.useful.map { $0 - 1 }.filter { candidates.indices.contains($0) })).sorted()
    }
}

/// Running the selection over a candidate list: batches by `[select] batch_chars`, each candidate
/// cut to `candidate_max_chars` for the model only.
public enum Selection {
    /// The user message of one selection request.
    static func prompt(situation: String, candidates: [String]) -> String {
        (["# 她現在的情況", situation, "# 候選"] + candidates.enumerated().map { "[\($0.offset + 1)] \($0.element)" })
            .joined(separator: "\n\n")
    }

    /// Consecutive groups of positions whose texts total at most `maxChars` (a single longer text is
    /// a group of its own).
    static func batches(_ texts: [String], maxChars: Int) -> [[Int]] {
        var out: [[Int]] = [], current: [Int] = [], size = 0
        for (i, t) in texts.enumerated() {
            if !current.isEmpty, size + t.count > maxChars {
                out.append(current)
                current = []
                size = 0
            }
            current.append(i)
            size += t.count
        }
        if !current.isEmpty { out.append(current) }
        return out
    }

    /// The positions in `texts` the model judged useful, ascending; with the wall-clock seconds the
    /// model took and the number of requests. No candidates, no request.
    static func run(_ model: any SelectionModel, situation: String, texts: [String], config: SelectConfig)
        async throws -> (picked: [Int], seconds: Double, batches: Int) {
        let shown = texts.map { $0.count > config.candidateMaxChars ? String($0.prefix(config.candidateMaxChars)) + "…" : $0 }
        let groups = batches(shown, maxChars: config.batchChars)
        let start = Date()
        var picked: [Int] = []
        for g in groups {
            picked += try await model.useful(situation: situation, candidates: g.map { shown[$0] }).map { g[$0] }
        }
        return (picked.sorted(), Date().timeIntervalSince(start), groups.count)
    }
}

/// One run of the pipeline, for the log and the turn report.
public struct SelectionReport: Sendable {
    /// What started it: 主人的新訊息 / 前景 app 換成 X / recall「query」.
    public var trigger: String
    /// The trigger's entity keys.
    public var entityKeys: [String]
    /// The pool by source, before filtering and the cap.
    public var seeds: Int
    public var entitySeeds: Int
    public var expanded: Int
    /// What went to the model, in order, and what it kept.
    public var candidates: [PoolEntry]
    public var selected: [PoolEntry]
    public var seconds: Double
    public var batches: Int
    /// The other stages' times (`PoolTiming.line`, the trigger's entities, task grouping).
    public var stages: String

    public var line: String {
        func ids(_ list: [PoolEntry]) -> String { "[" + list.map { "\($0.key.dropFirst(2))(\($0.source.rawValue))" }.joined(separator: " ") + "]" }
        return "選記憶（\(trigger)）entities \(entityKeys): seeds \(seeds), entity seeds \(entitySeeds), expanded \(expanded), "
            + "candidates \(candidates.count) \(ids(candidates)), selected \(selected.count) \(ids(selected)), "
            + "FM \(String(format: "%.2f", seconds)) s in \(batches) request(s); \(stages)"
    }
}
