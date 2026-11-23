import Foundation

/// Where the timeline's encrypted pieces live (owner, 2026-11-23: 「時間線的留下的就只有秘密索引」).
/// A segment line never holds ciphertext: every `"dollbox1:…"` string in it is replaced by
/// `"【秘密 #n】"` when written, and the box is appended to `<timeline dir>/secrets.store` (one JSON
/// line `{"n":…,"box":"dollbox1:…"}` each, synced). Reading a line puts the boxes back before it is
/// decoded, so the rest of `Timeline` sees what it wrote. The store is read whole at open (boxes only;
/// each is opened when its line is read).
final class SecretStore {
    static let refPrefix = "【秘密 #"
    let url: URL
    private var boxes: [Int: String] = [:]
    private var next = 1
    private var handle: FileHandle?

    private struct Entry: Codable { var n: Int; var box: String }

    init(dir: URL) throws {
        url = dir.appending(path: "secrets.store")
        guard FileManager.default.fileExists(atPath: url.path) else { return }
        var data = try Data(contentsOf: url)
        if let end = data.last, end != UInt8(ascii: "\n") {
            // A crash cut the last line short: its line in the segment was not written either.
            let keep = (data.lastIndex(of: UInt8(ascii: "\n")).map { $0 + 1 }) ?? 0
            data = data.prefix(keep)
            let h = try FileHandle(forWritingTo: url)
            try h.truncate(atOffset: UInt64(keep))
            try h.close()
        }
        for (i, line) in data.split(separator: UInt8(ascii: "\n")).enumerated() {
            do {
                let e = try JSONDecoder().decode(Entry.self, from: Data(line))
                boxes[e.n] = e.box
                next = max(next, e.n + 1)
            } catch { throw StoreError(description: "\(url.path) line \(i + 1): \(error)") }
        }
    }

    deinit { try? handle?.close() }

    private static let boxPattern = try! NSRegularExpression(pattern: "\"(" + NSRegularExpression.escapedPattern(for: SecretBox.prefix) + "[^\"]*)\"")
    private static let refPattern = try! NSRegularExpression(pattern: "\"" + refPrefix + "(\\d+)】\"")

    /// The line as written: each box moved into the store, a reference in its place.
    func extract(_ line: Data) throws -> Data {
        let s = String(decoding: line, as: UTF8.self)
        let matches = Self.boxPattern.matches(in: s, range: NSRange(s.startIndex..., in: s))
        guard !matches.isEmpty else { return line }
        var out = s
        for m in matches.reversed() {
            let box = String(s[Range(m.range(at: 1), in: s)!])
            let n = try put(box)
            out.replaceSubrange(Range(m.range, in: out)!, with: "\"\(Self.refPrefix)\(n)】\"")
        }
        return Data(out.utf8)
    }

    /// The line as read: each reference replaced by its box.
    func resolve(_ line: Data, place: String) throws -> Data {
        let s = String(decoding: line, as: UTF8.self)
        let matches = Self.refPattern.matches(in: s, range: NSRange(s.startIndex..., in: s))
        guard !matches.isEmpty else { return line }
        var out = s
        for m in matches.reversed() {
            let n = Int(s[Range(m.range(at: 1), in: s)!])!
            guard let box = boxes[n] else { throw StoreError(description: "\(place): \(Self.refPrefix)\(n)】 is not in \(url.path)") }
            out.replaceSubrange(Range(m.range, in: out)!, with: "\"\(box)\"")
        }
        return Data(out.utf8)
    }

    private func put(_ box: String) throws -> Int {
        let n = next
        if handle == nil {
            if !FileManager.default.fileExists(atPath: url.path) {
                guard FileManager.default.createFile(atPath: url.path, contents: nil, attributes: [.posixPermissions: 0o600]) else {
                    throw StoreError(description: "cannot create \(url.path)")
                }
            }
            let h = try FileHandle(forWritingTo: url)
            try h.seekToEnd()
            handle = h
        }
        try handle!.write(contentsOf: try JSONEncoder().encode(Entry(n: n, box: box)) + Data([UInt8(ascii: "\n")]))
        try handle!.synchronize()
        boxes[n] = box
        next += 1
        return n
    }
}

/// One of several record types written through the same call.
struct AnyEncodable: Encodable {
    let value: any Encodable
    init(_ value: any Encodable) { self.value = value }
    func encode(to encoder: any Encoder) throws { try value.encode(to: encoder) }
}
