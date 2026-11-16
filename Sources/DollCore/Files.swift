import Foundation

/// Her file tools (2026-11-09; on this Mac since 2026-11-16): read_file, write_file, edit_file on
/// text files. `LocalFiles` only moves bytes; everything about text is decided in `FileText`, so it
/// is exact and the words are ours: UTF-8 or an error, `cat -n` line numbers, the one occurrence
/// edit_file's `old` must have, the nearest lines when it has none.
enum LocalFiles {
    /// A file's bytes. `~` and `~/…` are the owner's home; a relative path is under it. Throws with
    /// the reason: no such file, a folder, no permission.
    static func read(_ path: String) throws -> Data {
        let p = Mac.expand(path)
        var dir: ObjCBool = false
        guard FileManager.default.fileExists(atPath: p, isDirectory: &dir) else { throw ToolError(description: "沒有這個檔案：\(path)") }
        guard !dir.boolValue else { throw ToolError(description: "\(path) 是資料夾，不是檔案") }
        guard FileManager.default.isReadableFile(atPath: p) else { throw ToolError(description: "沒有權限讀 \(path)") }
        return try Data(contentsOf: URL(filePath: p))
    }

    /// Writes `data` as the whole file (created or replaced), creating missing folders. Written in
    /// place, not replaced, so a file an app has open keeps its identity.
    static func write(_ path: String, _ data: Data) throws {
        let p = Mac.expand(path)
        var dir: ObjCBool = false
        if FileManager.default.fileExists(atPath: p, isDirectory: &dir), dir.boolValue {
            throw ToolError(description: "\(path) 是資料夾，不能當檔案寫")
        }
        do {
            try FileManager.default.createDirectory(at: URL(filePath: p).deletingLastPathComponent(), withIntermediateDirectories: true)
        } catch { throw ToolError(description: "開不出 \(path) 的資料夾：\(error.localizedDescription)") }
        if !FileManager.default.fileExists(atPath: p) {
            guard FileManager.default.createFile(atPath: p, contents: nil) else { throw ToolError(description: "寫不進 \(path)") }
        }
        do {
            let h = try FileHandle(forWritingTo: URL(filePath: p))
            defer { try? h.close() }
            try h.truncate(atOffset: 0)
            try h.write(contentsOf: data)
        } catch { throw ToolError(description: "寫不進 \(path)：\(error.localizedDescription)") }
    }
}

enum FileText {
    /// Strict UTF-8; anything else is an error naming the file (these tools are text only).
    static func decode(_ data: Data, path: String) throws -> String {
        guard let text = String(data: data, encoding: .utf8) else {
            throw ToolError(description: "\(path) 不是 UTF-8 文字檔（\(data.count) bytes），read_file、write_file、edit_file 只處理文字檔")
        }
        return text
    }

    /// The file's lines: split at every "\n" (a "\r" stays on its line), the last newline closing the
    /// last line rather than opening an empty one; an empty file has no lines.
    static func lines(_ text: String) -> [String] {
        var lines = text.components(separatedBy: "\n")
        if lines.last == "" { lines.removeLast() }
        return lines
    }

    /// `cat -n`: the number right-aligned in 6 columns, a tab, the line; `first` is the first line's number.
    static func numbered(_ lines: ArraySlice<String>, first: Int) -> String {
        zip(first..., lines).map { n, line in String(repeating: " ", count: max(0, 6 - String(n).count)) + "\(n)\t\(line)" }
            .joined(separator: "\n")
    }

    /// read_file's result: a header with the line count and the range shown, then the numbered lines.
    /// `from`/`to` are 1-based and inclusive; `to` past the end is the end. At most `max` lines are
    /// shown; the rest is a count and how to read it.
    static func read(_ text: String, path: String, from: Int?, to: Int?, max: Int) throws -> String {
        let all = lines(text)
        guard !all.isEmpty else { return "read_file \(path)：空檔案，0 行" }
        let start = from ?? 1
        guard start <= all.count else {
            throw ToolError(description: "\(path) 只有 \(all.count) 行，from 是 \(start)；from 最多 \(all.count)")
        }
        let end = min(to ?? all.count, all.count)
        let shownEnd = min(end, start + max - 1)
        let left = end - shownEnd
        var head = "read_file \(path)：共 \(all.count) 行"
        if start != 1 || shownEnd != all.count { head += "，這裡是第 \(start)–\(shownEnd) 行" }
        var out = head + "\n" + numbered(all[(start - 1)..<shownEnd], first: start)
        if left > 0 { out += "\n還有 \(left) 行（read_file 加 from: \(shownEnd + 1) 讀下去）" }
        return out
    }

    /// write_file's result line.
    static func written(_ text: String, path: String) -> String {
        "write_file \(path)：寫好了，\(lines(text).count) 行、\(text.utf8.count) bytes"
    }

    /// edit_file: `old` replaced by `new` when it occurs exactly once (literal match, whitespace and
    /// newlines included). Returns the new text and a result showing the changed lines with
    /// `context` lines around them. 0 or several occurrences is an error that says the count; with
    /// 0, the nearest lines (`nearest`) so she can copy the exact text.
    static func edit(_ text: String, path: String, old: String, new: String, context: Int = 3) throws -> (text: String, result: String) {
        let hits = occurrences(of: old, in: text)
        guard hits.count == 1 else {
            if hits.isEmpty {
                var msg = "edit_file \(path)：old 在檔案裡出現 0 次，沒改。old 要跟檔案裡一字不差（含空白、換行、標點）；"
                if let near = nearest(old, in: text) {
                    msg += "最接近的幾行（照這裡的字寫 old）：\n" + near
                } else {
                    msg += "檔案裡也沒有相近的行，先 read_file 看一下"
                }
                throw ToolError(description: msg)
            }
            throw ToolError(description: "edit_file \(path)：old 在檔案裡出現 \(hits.count) 次，不知道改哪一個，沒改。old 多帶幾行前後文，讓它只出現一次")
        }
        let hit = hits[0]
        let result = text.replacingCharacters(in: hit, with: new)
        // The first changed line (1-based) and how many lines the region spans before and after.
        let before = text[..<hit.lowerBound]
        let firstLine = before.components(separatedBy: "\n").count
        let oldLines = old.components(separatedBy: "\n").count, newLines = new.components(separatedBy: "\n").count
        let all = lines(result)
        let lo = max(1, firstLine - context), hi = min(all.count, firstLine + newLines - 1 + context)
        var out = "edit_file \(path)：改好了，第 \(firstLine) 行起（\(oldLines) 行 → \(newLines) 行），現在共 \(all.count) 行"
        if lo <= hi { out += "\n" + numbered(all[(lo - 1)..<hi], first: lo) }
        return (result, out)
    }

    /// Every non-overlapping literal occurrence of `needle`, in order.
    static func occurrences(of needle: String, in text: String) -> [Range<String.Index>] {
        var found: [Range<String.Index>] = []
        var from = text.startIndex
        while from < text.endIndex, let r = text.range(of: needle, options: .literal, range: from..<text.endIndex) {
            found.append(r)
            from = r.upperBound
        }
        return found
    }

    /// The lines nearest to `old`'s first non-blank line, numbered: the line sharing the most
    /// characters with it (each character counted as often as both have it), ties to the longer
    /// common prefix, then the earlier line; shown with one line before and after. nil when no line
    /// shares a character (blanks aside).
    static func nearest(_ old: String, in text: String) -> String? {
        let all = lines(text)
        guard let probe = old.components(separatedBy: "\n").map({ $0.trimmingCharacters(in: .whitespaces) }).first(where: { !$0.isEmpty }) else {
            return nil
        }
        var want: [Character: Int] = [:]
        for c in probe where !c.isWhitespace { want[c, default: 0] += 1 }
        var best: (shared: Int, prefix: Int, line: Int)?
        for (i, raw) in all.enumerated() {
            let line = raw.trimmingCharacters(in: .whitespaces)
            var have = want, shared = 0
            for c in line where (have[c] ?? 0) > 0 { have[c]! -= 1; shared += 1 }
            guard shared > 0 else { continue }
            let prefix = zip(probe, line).prefix { $0 == $1 }.count
            if best.map({ (shared, prefix) > ($0.shared, $0.prefix) }) ?? true { best = (shared, prefix, i + 1) }
        }
        guard let best else { return nil }
        let lo = max(1, best.line - 1), hi = min(all.count, best.line + 1)
        return numbered(all[(lo - 1)..<hi], first: lo)
    }
}
