import Foundation

/// The fixed part of her system prompt (2026-11-23, owner: 「把大多的 context length 都放在 system
/// prompt」; `[context] manual`): what changes only with the day goes in front of the timeline, so
/// it is computed once a day and cached after that.
///
///   panel      her skill panel (`Doll.panelText`): what she has shown she can and cannot do, and what she
///              has not found out; with `[drives]`, whether or not the manual is in.
///   summaries  the last `summary_days` maintenance summaries (`<data_dir>/summaries.json`, written by
///              the consolidation); a summary of a day that touched something sealed is left out (the
///              system prompt is in every scene, the developer's too) — she recalls those.
///   deep       her deep memories of the public folder, newest first, at most `deep_max`.
///   tools      her saved tools (name and when to use them), with `[computer] enabled`.
///   manual     every `.md` of `[core] docs` (not its subfolders): README first, then by name.
///
/// Rebuilt at boot and when a maintenance ends; nothing else changes it, so the bytes stay the same
/// between those (the prompt prefix stays cached).
public struct SummaryFile: Sendable, Codable, Equatable {
    public struct Entry: Sendable, Codable, Equatable {
        /// The day its timeline began (`YYYY-MM-DD`).
        public var day: String
        public var segment: String
        public var text: String
        /// The day touched something sealed: kept, never in the system prompt.
        public var sealed: Bool
    }
    public var entries: [Entry] = []
}

enum Fixed {
    static let summariesFile = "summaries.json"
    /// Entries kept in `summaries.json`.
    static let summariesKept = 60

    /// The manual's files: README first, then by name.
    static func manualFiles(_ dir: URL) -> [URL] {
        let names = ((try? FileManager.default.contentsOfDirectory(atPath: dir.path)) ?? [])
            .filter { $0.hasSuffix(".md") && !$0.hasPrefix(".") }
        return names.sorted { a, b in a == "README.md" ? b != "README.md" : b == "README.md" ? false : a < b }
            .map { dir.appending(path: $0) }
    }

    static func manual(_ dir: URL) throws -> String {
        try manualFiles(dir).map { url in
            "=== \(url.lastPathComponent) ===\n" + (try String(contentsOf: url, encoding: .utf8)).trimmingCharacters(in: .whitespacesAndNewlines)
        }.joined(separator: "\n\n")
    }

    static func text(panel: String?, summaries: [SummaryFile.Entry], deep: [String], tools: [(name: String, when: String)], manual: String) -> String {
        var parts: [String] = []
        if let panel { parts.append("## 技能面板（系統記的：你有根據地會的、不會的、還沒弄清楚的；每天系統維護時更新，現在的和每條的根據用 self.read 看）\n" + panel) }
        if !summaries.isEmpty {
            parts.append("## 最近幾天的摘要（每天系統維護另外寫的，不是你寫的；舊的在前）\n"
                         + summaries.map { "【\($0.day)】\($0.text.trimmingCharacters(in: .whitespacesAndNewlines))" }.joined(separator: "\n\n"))
        }
        if !deep.isEmpty {
            parts.append("## 你一直記得的事（標成深刻的記憶，公開的那些；秘密的不在這裡，用 recall 查）\n" + deep.map { "- \($0)" }.joined(separator: "\n"))
        }
        if !tools.isEmpty {
            parts.append("## 你存的工具（用 tool.run 叫）\n" + tools.map { "- \($0.name)：\($0.when)" }.joined(separator: "\n"))
        }
        if !manual.isEmpty {
            parts.append("## DollOS 說明書（英文；裡面的 \"the doll\" 就是你，\"the owner\" 是主人）\n\n" + manual)
        }
        return parts.isEmpty ? "" : "\n\n" + parts.joined(separator: "\n\n")
    }
}

extension Doll {
    func readSummaries() throws -> SummaryFile {
        try readDocument(SummaryFile.self, summariesURL, what: "summaries") ?? SummaryFile()
    }

    /// The consolidation's summary of the day just closed (sealed with the day).
    func saveSummary(day: String, segment: String, text: String) throws {
        var f = try readSummaries()
        f.entries.append(SummaryFile.Entry(day: day, segment: segment, text: text, sealed: sealing))
        if f.entries.count > Fixed.summariesKept { f.entries.removeFirst(f.entries.count - Fixed.summariesKept) }
        try saveDocument(f, summariesURL)
    }

    /// Rebuilds `fixedPrompt` (boot, end of maintenance). A failure keeps the one before and is an ERROR.
    func refreshFixed() {
        do {
            fixedPrompt = try buildFixed()
            fixedTokens = estimate([ChatMessage(role: "system", content: fixedPrompt)])
            log("INFO", "system prompt: fixed part rebuilt, \(fixedPrompt.count) characters (about \(fixedTokens) tokens before scaling)")
        } catch {
            log("ERROR", "system prompt: fixed part not rebuilt, the one before stays: \(error)")
        }
    }

    func buildFixed() throws -> String {
        // Her skill panel (2026-11-24) goes with or without the manual; empty graph: no panel.
        let shown = goalFile.nodes.contains { $0.done || $0.failed || !$0.blocks.isEmpty }
        let panel = drivesConfig.enabled && shown ? panelText(brief: true) : nil
        guard contextConfig.manual else { return Fixed.text(panel: panel, summaries: [], deep: [], tools: [], manual: "") }
        let summaries = Array(try readSummaries().entries.filter { !$0.sealed }.suffix(contextConfig.summaryDays))
        let deep = try memory.list(.public).filter { $0.file.deep && $0.file.retired == nil }.prefix(contextConfig.deepMax)
            .map { $0.file.body.trimmingCharacters(in: .whitespacesAndNewlines) }
        let tools = computerOn ? try toolStore.all().filter { !$0.retired(computerConfig.ownToolRetireFailures) }.map { ($0.name, $0.when) } : []
        return Fixed.text(panel: panel, summaries: summaries, deep: Array(deep), tools: tools, manual: try Fixed.manual(docsDir))
    }
}
