import Foundation

/// The owner's lines kept on disk from the moment they arrive until they are in her timeline
/// (2026-11-22, owner: a line, once received, is written before "received" is answered; a process
/// death after that cannot lose it, and on the next boot she sees and answers it).
///
/// `<data_dir>/inbox/<received ms>-<n>.line`: one file per line, the line (text, spoken or typed,
/// channel, when received) as one `SecretBox` box, written to a temporary name, synced, then renamed.
/// It is sealed whatever the secret check will say, since the check has not answered yet: nothing of
/// his line is on disk in plain text before it does. The file goes when the line's event is in the
/// timeline (`Doll.drain`); at boot the files left are checked again and queued in their order.
struct Inbox {
    struct Line: Codable, Equatable {
        var text: String
        var voice: Bool
        var channel: Channel
        var received: Double
    }

    let dir: URL
    let box: SecretBox

    init(dir: URL, box: SecretBox) throws {
        self.dir = dir
        self.box = box
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }

    /// Writes `line` and returns its file name; `serial` orders lines received in the same millisecond.
    func put(_ line: Line, serial: Int) throws -> String {
        let name = String(format: "%015lld-%06d.line", Int64(line.received * 1000), serial)
        let sealed = try box.seal(String(decoding: try JSONEncoder().encode(line), as: UTF8.self))
        let tmp = dir.appending(path: "." + name)
        guard FileManager.default.createFile(atPath: tmp.path, contents: nil) else {
            throw InboxError(description: "cannot create \(tmp.path)")
        }
        let h = try FileHandle(forWritingTo: tmp)
        defer { try? h.close() }
        try h.write(contentsOf: Data(sealed.utf8))
        try h.synchronize()
        guard rename(tmp.path, dir.appending(path: name).path) == 0 else {
            throw InboxError(description: "cannot rename \(tmp.path): \(String(cString: strerror(errno)))")
        }
        return name
    }

    func remove(_ name: String) throws {
        try FileManager.default.removeItem(at: dir.appending(path: name))
    }

    /// The lines still here, oldest first.
    func pending() throws -> [(name: String, line: Line)] {
        try FileManager.default.contentsOfDirectory(atPath: dir.path).filter { $0.hasSuffix(".line") && !$0.hasPrefix(".") }.sorted().map { name in
            let url = dir.appending(path: name)
            let json = try box.open(try String(contentsOf: url, encoding: .utf8), place: url.path)
            return (name, try JSONDecoder().decode(Line.self, from: Data(json.utf8)))
        }
    }
}

struct InboxError: Error, CustomStringConvertible {
    let description: String
}
