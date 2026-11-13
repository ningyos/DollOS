import Foundation
import Synchronization

// Her voice in a Discord voice channel (2026-11-13, M6). The bridge opens one WebSocket,
// `ws://127.0.0.1:<api port>/api/voice` (owner key as `Authorization: Bearer …`):
//
//   bridge → dollcore  binary  the owner's audio, 16 kHz mono s16le, ~20 ms frames, only while his packets arrive
//                      JSON    {"type":"hello","version":1} first; owner_joined {channel}, owner_left,
//                              owner_speaking (start of each talk burst), joined {channel}, left,
//                              played {utterance, samples} (≤ 100 ms apart, and a final one), error {message}
//   dollcore → bridge  JSON    join, leave, utterance_start {utterance}, utterance_end {utterance}, stop {utterance}
//                      binary  her audio for the open utterance, 24 kHz mono s16le
//
// Any protocol violation is an `error` message back plus an ERROR log line; a malformed frame or
// message closes the socket. Nothing is dropped silently.
extension Config {
    /// `[voice] data_dir`: under `[core] data_dir` unless absolute (both relative to `root`).
    public func voiceDirectory(root: URL) -> URL {
        if voice.dataDir.hasPrefix("/") { return URL(filePath: voice.dataDir) }
        let data = core.dataDir.hasPrefix("/") ? URL(filePath: core.dataDir) : root.appending(path: core.dataDir)
        return data.appending(path: voice.dataDir)
    }
}


// sentences --------------------------------------------------------------------

/// How `voice.say` text becomes the sentences the TTS speaks one after another.
///
/// - A sentence ends after 。！？!?；; or a newline, after an ellipsis (… or ... of any length), and
///   after a "." followed by whitespace or the end (so 3.14, v1.2 and example.com stay whole).
///   Runs of these marks stay together (「真的？！」, "wait..."), and closing quotes and brackets
///   right after them (」』）)"'”’) stay with the sentence they close.
/// - A piece with no letter or digit (a lone 「……」) joins the sentence before it.
/// - A sentence over `maxChars` characters is cut after its commas (，,、：:) into pieces of at most
///   `maxChars` where possible; a stretch with no comma that is still too long is cut every `maxChars`.
/// - Whitespace around sentences is trimmed; empty ones are dropped.
public enum Sentences {
    static let enders: Set<Character> = ["。", "！", "？", "!", "?", "；", ";", "\n", "…"]
    static let closers: Set<Character> = ["」", "』", "）", ")", "\"", "'", "”", "’", "】", "》"]
    static let commas: Set<Character> = ["，", ",", "、", "：", ":"]

    public static func split(_ text: String, maxChars: Int) -> [String] {
        let chars = Array(text)
        var pieces: [String] = []
        var cur = ""
        var i = 0
        func isEnd(_ j: Int) -> Bool {
            let c = chars[j]
            if enders.contains(c) { return true }
            guard c == "." else { return false }
            // "..." is an ellipsis; "." before whitespace or the end ends a sentence; "3.14" does not.
            if j + 1 < chars.count, chars[j + 1] == "." { return true }
            if j > 0, chars[j - 1] == "." { return true }
            return j + 1 == chars.count || chars[j + 1].isWhitespace
        }
        while i < chars.count {
            cur.append(chars[i])
            if isEnd(i) {
                var j = i + 1
                while j < chars.count, isEnd(j) || closers.contains(chars[j]) {
                    cur.append(chars[j])
                    j += 1
                }
                pieces.append(cur)
                cur = ""
                i = j
                continue
            }
            i += 1
        }
        pieces.append(cur)
        var out: [String] = []
        for p in pieces {
            let t = p.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !t.isEmpty else { continue }
            if !t.contains(where: { $0.isLetter || $0.isNumber }), let last = out.popLast() {
                out.append(last + t)
            } else {
                out.append(t)
            }
        }
        return out.flatMap { cut($0, maxChars: maxChars) }
    }

    /// One sentence in pieces of at most `maxChars`: after commas where possible, else hard.
    static func cut(_ s: String, maxChars: Int) -> [String] {
        guard s.count > maxChars else { return [s] }
        var clauses: [String] = []
        var cur = ""
        for c in s {
            cur.append(c)
            if commas.contains(c) {
                clauses.append(cur)
                cur = ""
            }
        }
        if !cur.isEmpty { clauses.append(cur) }
        var out: [String] = []
        var acc = ""
        for c in clauses {
            if !acc.isEmpty, acc.count + c.count > maxChars {
                out.append(acc)
                acc = ""
            }
            acc += c
            while acc.count > maxChars {
                out.append(String(acc.prefix(maxChars)))
                acc = String(acc.dropFirst(maxChars))
            }
        }
        if !acc.isEmpty { out.append(acc) }
        return out.map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
    }

    /// The TTS language for one sentence under `[voice] tts_language = "detect"`: kana → ja, else
    /// Han → zh, else a Latin letter → en, else auto.
    public static func language(_ s: String) -> String {
        let scalars = s.unicodeScalars
        if scalars.contains(where: { (0x3040...0x30FF).contains($0.value) }) { return "ja" }
        if scalars.contains(where: { (0x4E00...0x9FFF).contains($0.value) || (0x3400...0x4DBF).contains($0.value) }) { return "zh" }
        if scalars.contains(where: { ("a"..."z").contains(Character($0)) || ("A"..."Z").contains(Character($0)) }) { return "en" }
        return "auto"
    }
}

// endpointing ------------------------------------------------------------------

/// Where the owner's utterances end (SpeechTranscriber's own finals come only every ~12 s and
/// ignore pauses). Frames are fed as they arrive; `tick` closes the open utterance once no voiced
/// frame came for `silenceMs` of wall time (push-to-talk released = no frames at all; held but
/// quiet = frames under `silenceRms`).
public struct Endpointer: Sendable {
    public static let sampleRate = 16000
    let silence: Double
    let silenceRms: Double
    /// Samples given to the recognizer so far (its audio clock).
    public private(set) var fed = 0
    /// End of the last voiced frame in the open utterance, on the recognizer's clock (samples).
    var speechEnd: Int?
    var lastVoiced: Double = 0

    public struct End: Sendable, Equatable {
        /// Zeros to feed before finalizing, so the recognizer's audio covers the silence it ended on.
        public var pad: Int
        /// `finalize(through:)` here (seconds on the recognizer's clock): the end of the last voiced frame.
        public var through: Double
    }

    public init(silenceMs: Int, silenceRms: Double) {
        silence = Double(silenceMs) / 1000
        self.silenceRms = silenceRms
    }

    public static func rms(_ s: [Int16]) -> Double {
        guard !s.isEmpty else { return 0 }
        var sum = 0.0
        for v in s { sum += Double(v) * Double(v) }
        return (sum / Double(s.count)).squareRoot()
    }

    /// One frame at wall time `now` (seconds); true when it is voiced.
    @discardableResult
    public mutating func frame(_ samples: [Int16], now: Double) -> Bool {
        fed += samples.count
        guard Self.rms(samples) >= silenceRms else { return false }
        speechEnd = fed
        lastVoiced = now
        return true
    }

    /// The utterance's end when `silenceMs` passed since its last voiced frame; nil otherwise.
    public mutating func tick(now: Double) -> End? {
        guard let end = speechEnd, now - lastVoiced >= silence else { return nil }
        speechEnd = nil
        let want = end + Int(silence * Double(Self.sampleRate))
        let pad = max(0, want - fed)
        fed += pad
        return End(pad: pad, through: Double(end) / Double(Self.sampleRate))
    }
}

// what was heard -----------------------------------------------------------------

/// One `voice.say`: its sentences, how many samples each produced, how many the
/// bridge played, and what she is told.
public struct SpokenReport: Sendable, Equatable {
    public var sentences: [String]
    /// Samples generated per sentence, for the sentences generation reached.
    public var samples: [Int]
    public var played: Int
    public var interrupted: Bool
    /// Seconds from the call to the first audio chunk sent to the bridge; nil when none was.
    public var firstAudio: Double?

    public init(sentences: [String], samples: [Int], played: Int, interrupted: Bool, firstAudio: Double? = nil) {
        self.sentences = sentences
        self.samples = samples
        self.played = played
        self.interrupted = interrupted
        self.firstAudio = firstAudio
    }

    /// Sentences heard in full, then the cut one's index and about how many of its characters were
    /// heard (played samples over its samples, scaled to its length); nil cut = all heard.
    public var heard: (full: Int, cut: (index: Int, chars: Int)?) {
        var start = 0
        for (i, n) in samples.enumerated() {
            if played < start + n {
                let f = n == 0 ? 0 : Double(played - start) / Double(n)
                return (i, (i, Int((f * Double(sentences[i].count)).rounded())))
            }
            start += n
        }
        return samples.count == sentences.count ? (sentences.count, nil) : (samples.count, (samples.count, 0))
    }

    /// The text heard, for the timeline.
    public var heardText: String {
        let h = heard
        var s = sentences.prefix(h.full).joined()
        if let c = h.cut { s += sentences[c.index].prefix(c.chars) }
        return s
    }

    /// Her `voice.say` result: empty when everything was heard (like a delivered text); otherwise
    /// what was heard and what was not, the cut position marked 大約.
    public var resultText: String {
        let h = heard
        guard interrupted || h.cut != nil, let cut = h.cut else { return "" }
        let s = sentences[cut.index]
        let said = sentences.prefix(cut.index).joined() + s.prefix(cut.chars)
        let unsaid = String(s.dropFirst(cut.chars)) + sentences.dropFirst(cut.index + 1).joined()
        if said.isEmpty { return "還沒說出口就被主人打斷了：「\(unsaid)」主人都沒聽到。" }
        return "說到「\(said)」被主人打斷了（第 \(cut.index + 1) 句「\(s)」大約說到第 \(cut.chars) 個字），後面「\(unsaid)」沒說出口。"
    }
}

// persistence --------------------------------------------------------------------

/// Every spoken sentence for the nightly fine-tune: `<dir>/<file>.wav` (24 kHz mono float32) and one
/// line in `<dir>/manifest.jsonl` (append-only). Writing fails loudly.
public final class VoiceManifest: Sendable {
    public let dir: URL
    let writeWAV: @Sendable ([Float], Int, URL) throws -> Void
    let lock = Mutex(())

    public struct Entry: Codable, Sendable, Equatable {
        public var file: String
        public var text: String
        public var language: String
        public var seed: UInt64
        /// This process's start (epoch seconds): utterance ids restart with every process.
        public var session: Double
        public var utterance: Int
        public var sentence: Int
        /// Generation start and end, epoch seconds.
        public var startTs: Double
        public var endTs: Double
        public var sampleRate: Int
        public var samples: Int
        /// Generation ran to the end of the sentence (false: the owner cut it while it was generated).
        public var generationComplete: Bool
        /// Of this sentence's samples, how many the bridge played.
        public var playedSamples: Int
        /// The owner cut it before all of it was played.
        public var interrupted: Bool
    }

    public init(dir: URL, writeWAV: @escaping @Sendable ([Float], Int, URL) throws -> Void) throws {
        self.dir = dir
        self.writeWAV = writeWAV
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }

    public func append(_ e: Entry, audio: [Float]) throws {
        try lock.withLock { _ in
            try writeWAV(audio, e.sampleRate, dir.appending(path: e.file))
            let enc = JSONEncoder()
            enc.keyEncodingStrategy = .convertToSnakeCase
            enc.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
            let line = try enc.encode(e) + Data("\n".utf8)
            let url = dir.appending(path: "manifest.jsonl")
            if !FileManager.default.fileExists(atPath: url.path) { FileManager.default.createFile(atPath: url.path, contents: nil) }
            let h = try FileHandle(forWritingTo: url)
            defer { try? h.close() }
            try h.seekToEnd()
            try h.write(contentsOf: line)
        }
    }

    public func entries() throws -> [Entry] {
        let url = dir.appending(path: "manifest.jsonl")
        guard FileManager.default.fileExists(atPath: url.path) else { return [] }
        let dec = JSONDecoder()
        dec.keyDecodingStrategy = .convertFromSnakeCase
        return try String(contentsOf: url, encoding: .utf8).split(separator: "\n").map { try dec.decode(Entry.self, from: Data($0.utf8)) }
    }
}

// engines ------------------------------------------------------------------------

/// The owner's speech to text (Apple SpeechTranscriber; tests: a fake).
public protocol SpeechRecognizer: Sendable {
    /// 16 kHz mono samples, in order; the recognizer's clock is the count fed.
    func feed(_ samples: [Int16]) async throws
    /// Finalizes everything up to `through` seconds and returns its text (empty: no words).
    func finalize(through: Double) async throws -> String
}

/// Her text to speech (voice-mlx; tests: a fake). One call at a time.
public protocol SpeechSynthesizer: Sendable {
    var sampleRate: Int { get }
    /// Streams `text` as chunks to `onChunk` until done or `shouldStop()` (checked between chunks).
    func synthesize(_ text: String, language: String, shouldStop: @escaping @Sendable () -> Bool,
                    onChunk: @escaping @Sendable ([Float]) -> Void) async throws -> Synthesized
}

public struct Synthesized: Sendable {
    public var seed: UInt64
    public var samples: [Float]
    /// false: stopped by `shouldStop` before the end.
    public var complete: Bool

    public init(seed: UInt64, samples: [Float], complete: Bool) {
        self.seed = seed
        self.samples = samples
        self.complete = complete
    }
}

// the link ---------------------------------------------------------------------

/// What the voice channel tells her (Doll's inputs).
public struct VoiceEvents: Sendable {
    public var ownerSaid: @Sendable (String) async -> Void
    /// A program line for her timeline: the owner joined or left, she was removed, the link dropped.
    public var notice: @Sendable (String) async -> Void

    public init(ownerSaid: @escaping @Sendable (String) async -> Void, notice: @escaping @Sendable (String) async -> Void) {
        self.ownerSaid = ownerSaid
        self.notice = notice
    }
}

/// What Doll calls. A call in the wrong state throws a `VoiceFailure` that says why and what she
/// can do now; it becomes her tool result.
public protocol VoicePort: Sendable {
    /// The voice channel she is in; nil when she is not in one.
    func channel() async -> String?
    /// Refused when the owner is not in a voice channel, or she already is in one.
    func join() async throws -> String
    /// Refused when she is not in a voice channel.
    func leave() async throws
    /// Refused when she is not in a voice channel.
    func speak(_ text: String) async throws -> SpokenReport
}

public struct VoiceFailure: Error, CustomStringConvertible {
    public var description: String
    public init(_ d: String) { description = d }
}

/// One utterance's state the TTS thread touches: stop generating, and samples sent so far
/// (counted before each chunk is queued, so a `played` report never exceeds it).
final class UtteranceShared: Sendable {
    let stop = Mutex(false)
    let sent = Mutex(0)
    var stopped: Bool { stop.withLock { $0 } }
}

/// The `/api/voice` peer: one bridge connection at a time; the owner's audio into the recognizer
/// with our endpointing, her `voice.say` through the synthesizer to the bridge, and the interruption.
public actor VoiceLink: VoicePort {
    let config: VoiceConfig
    let recognizer: any SpeechRecognizer
    let synthesizer: any SpeechSynthesizer
    let manifest: VoiceManifest
    let session0 = Date().timeIntervalSince1970
    var events: VoiceEvents?
    var socket: WebSocketSession?
    var hello = false
    var ownerChannel: String?
    var herChannel: String?
    var pendingJoin: CheckedContinuation<String, Error>?
    var pendingLeave: CheckedContinuation<Void, Error>?
    var endpointer: Endpointer
    /// Finalizations run one after another, in utterance order.
    var finalizing: Task<Void, Never>?
    var ticker: Task<Void, Never>?
    var utteranceSerial = 0
    var current: Utterance?
    var waiters: [CheckedContinuation<Void, Never>] = []
    let logSink = Mutex<(@Sendable (String) -> Void)?>(nil)

    struct Utterance {
        let id: Int
        let shared = UtteranceShared()
        var sent: Int { shared.sent.withLock { $0 } }
        var ended = false
        var played = 0
        var playedReports = 0
        var stopSentAt: Double?
        var playedAfterStop = false
        var failure: String?
        var progressAt: Double

        init(id: Int, progressAt: Double) {
            self.id = id
            self.progressAt = progressAt
        }
    }

    public init(config: VoiceConfig, recognizer: any SpeechRecognizer, synthesizer: any SpeechSynthesizer, manifest: VoiceManifest) {
        self.config = config
        self.recognizer = recognizer
        self.synthesizer = synthesizer
        self.manifest = manifest
        endpointer = Endpointer(silenceMs: config.silenceMs, silenceRms: config.silenceRms)
    }

    public func setEvents(_ e: VoiceEvents) { events = e }
    public nonisolated func setLogSink(_ f: (@Sendable (String) -> Void)?) { logSink.withLock { $0 = f } }

    nonisolated func log(_ level: String, _ msg: String) {
        Log.write(level, "voice: " + msg)
        logSink.withLock { $0 }?("\(level) voice: \(msg)")
    }

    static func now() -> Double { Date().timeIntervalSince1970 }

    // connection -------------------------------------------------------------

    /// Takes the one bridge slot; false when a bridge is already connected.
    public func claim() -> Bool {
        guard socket == nil, !claimed else { return false }
        claimed = true
        return true
    }
    var claimed = false

    /// The upgraded connection (after `claim`).
    public func attach(_ s: WebSocketSession) {
        socket = s
        hello = false
        log("INFO", "bridge connected")
        if ticker == nil {
            let poll = config.endpointPollMs
            ticker = Task { [weak self] in
                while !Task.isCancelled {
                    try? await Task.sleep(for: .milliseconds(poll))
                    await self?.tick()
                }
            }
        }
        Task { await self.receive(s) }
    }

    func receive(_ s: WebSocketSession) async {
        do {
            for try await m in s.messages {
                switch m {
                case .text(let t): try await control(t)
                case .binary(let d): try await audio(d)
                case .close(let code, let reason): log("INFO", "bridge closed the socket (\(code.map(String.init) ?? "-") \(reason))")
                case .ping, .pong: break
                }
            }
            await detached(s, why: "bridge closed the voice socket")
        } catch {
            log("ERROR", "voice socket: \(error)")
            await detached(s, why: "\(error)")
        }
    }

    /// A violation by the bridge: told back, logged, the socket closed.
    func violation(_ msg: String) async -> Error {
        log("ERROR", "bridge protocol violation: \(msg)")
        if let s = socket {
            await s.send(.text(Self.json(["type": "error", "message": msg])))
            s.close(code: 1002, reason: String(msg.prefix(100)))
        }
        return VoiceFailure(msg)
    }

    func detached(_ s: WebSocketSession, why: String) async {
        guard socket === s else { return }
        socket = nil
        claimed = false
        hello = false
        let wasIn = herChannel
        herChannel = nil
        ownerChannel = nil
        pendingJoin?.resume(throwing: VoiceFailure("語音連線斷了：\(why)"))
        pendingJoin = nil
        pendingLeave?.resume(throwing: VoiceFailure("語音連線斷了：\(why)"))
        pendingLeave = nil
        if current != nil { current!.failure = "語音連線斷了：\(why)"; current!.shared.stop.withLock { $0 = true } }
        signal()
        log(wasIn != nil ? "ERROR" : "INFO", "bridge disconnected: \(why)")
        if let wasIn { await events?.notice("【語音】語音連線斷了（\(why)），你已經不在語音頻道「\(wasIn)」裡了。") }
    }

    static func json(_ obj: [String: Any]) -> String {
        String(decoding: try! JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys, .withoutEscapingSlashes]), as: UTF8.self)
    }

    func send(_ obj: [String: Any]) async throws {
        guard let s = socket, hello else { throw VoiceFailure("語音連線沒有接上（bridge 沒有連到 /api/voice）") }
        guard await s.send(.text(Self.json(obj))) else { throw VoiceFailure("語音連線送不出去") }
    }

    // bridge → here ------------------------------------------------------------

    /// One control message from the bridge; a field of the wrong type fails the whole decode.
    struct Control: Decodable, Sendable {
        var type: String
        var version: Int?
        var channel: String?
        var utterance: Int?
        var samples: Int?
        var message: String?
    }

    func control(_ text: String) async throws {
        let m: Control
        do {
            m = try JSONDecoder().decode(Control.self, from: Data(text.utf8))
        } catch {
            throw await violation("bad control message (\(error)): \(text.prefix(200))")
        }
        let type = m.type
        func need<T: Sendable>(_ v: T?, _ k: String) async throws -> T {
            guard let v else { throw await violation("\(type): \(k) missing") }
            return v
        }
        if type == "hello" {
            guard !hello else { throw await violation("hello twice") }
            guard m.version == 1 else { throw await violation("hello: version must be 1") }
            hello = true
            log("INFO", "bridge hello")
            return
        }
        guard hello else { throw await violation("\(type) before hello") }
        switch type {
        // The owner coming and going is not an event to her (owner, 2026-11-14: she joined every
        // time he did); `voice.join` says so when he is not there. Only his leaving while she
        // is inside reaches her, or she would stay in an empty channel without knowing.
        case "owner_joined":
            let ch = try await need(m.channel, "channel")
            ownerChannel = ch
            log("INFO", "owner joined voice channel \(ch)")
        case "owner_left":
            let was = ownerChannel
            ownerChannel = nil
            log("INFO", "owner left the voice channel")
            if let hers = herChannel {
                await events?.notice("【語音】主人離開語音頻道" + (was.map { "「\($0)」" } ?? "") + "了。你還在「\(hers)」裡；要出來用 \(Tools.Name.voiceLeave)。")
            }
        case "owner_speaking":
            ownerStarted()
        case "joined":
            let ch = try await need(m.channel, "channel")
            herChannel = ch
            log("INFO", "joined voice channel \(ch)")
            if let p = pendingJoin { pendingJoin = nil; p.resume(returning: ch) }
        case "left":
            let was = herChannel
            herChannel = nil
            log("INFO", "left the voice channel")
            if current != nil { current!.failure = "不在語音頻道裡了"; current!.shared.stop.withLock { $0 = true }; signal() }
            if let p = pendingLeave {
                pendingLeave = nil
                p.resume()
            } else if let was {
                await events?.notice("【語音】你被移出語音頻道「\(was)」了。")
            }
        case "played":
            let id = try await need(m.utterance, "utterance"), n = try await need(m.samples, "samples")
            guard var u = current, u.id == id else {
                // A late report for an utterance that already finished (the one just done, or an
                // earlier one) is a race, not a violation: `speak` ends when played reaches what was
                // sent, and the bridge's final `played` can still be on its way (2026-11-14, live).
                guard id <= utteranceSerial, id >= 1 else { throw await violation("played: unknown utterance \(id)") }
                return
            }
            guard n >= 0, n <= u.sent else { throw await violation("played: \(n) samples of utterance \(id), only \(u.sent) sent") }
            u.played = max(u.played, n)
            u.playedReports += 1
            u.progressAt = Self.now()
            if u.stopSentAt != nil { u.playedAfterStop = true }
            current = u
            signal()
        case "error":
            let msg = try await need(m.message, "message")
            log("ERROR", "bridge error: \(msg)")
            if let p = pendingJoin {
                pendingJoin = nil
                p.resume(throwing: VoiceFailure(msg))
            } else if let p = pendingLeave {
                pendingLeave = nil
                p.resume(throwing: VoiceFailure(msg))
            } else if current != nil {
                current!.failure = "bridge：\(msg)"
                current!.shared.stop.withLock { $0 = true }
                signal()
            }
        default:
            throw await violation("unknown type \(type)")
        }
    }

    func audio(_ d: Data) async throws {
        guard hello else { throw await violation("audio before hello") }
        guard d.count % 2 == 0 else { throw await violation("audio frame of \(d.count) bytes is not whole s16 samples") }
        let samples = d.withUnsafeBytes { p in (0..<(d.count / 2)).map { Int16(littleEndian: p.loadUnaligned(fromByteOffset: $0 * 2, as: Int16.self)) } }
        endpointer.frame(samples, now: Self.now())
        try await recognizer.feed(samples)
    }

    /// The owner started talking: whatever she is saying stops now.
    func ownerStarted() {
        guard var u = current, u.stopSentAt == nil, !(u.ended && u.played >= u.sent) else { return }
        u.shared.stop.withLock { $0 = true }
        u.stopSentAt = Self.now()
        current = u
        log("INFO", "owner spoke: stopping utterance \(u.id) at \(u.played)/\(u.sent) samples played")
        if let s = socket { s.enqueue(.text(Self.json(["type": "stop", "utterance": u.id]))) }
        signal()
    }

    /// Closes the owner's utterance after `silence_ms` and hands its text to her.
    func tick() async {
        guard let end = endpointer.tick(now: Self.now()) else { return }
        let rec = recognizer, events = events, previous = finalizing
        let started = Self.now()
        log("INFO", "owner silent \(config.silenceMs) ms: finalizing through \(String(format: "%.2f", end.through)) s (padded \(end.pad) samples)")
        finalizing = Task { [weak self] in
            await previous?.value
            do {
                if end.pad > 0 { try await rec.feed([Int16](repeating: 0, count: end.pad)) }
                let text = try await rec.finalize(through: end.through).trimmingCharacters(in: .whitespacesAndNewlines)
                self?.log("INFO", "owner utterance through \(String(format: "%.2f", end.through)) s: final after "
                          + "\(Int((Self.now() - started) * 1000)) ms「\(text)」")
                if !text.isEmpty { await events?.ownerSaid(text) }
            } catch {
                self?.log("ERROR", "speech recognition failed: \(error)")
            }
        }
    }

    // here → bridge ------------------------------------------------------------

    public func channel() -> String? { herChannel }

    public func join() async throws -> String {
        if let ch = herChannel {
            throw VoiceFailure("你已經在語音頻道「\(ch)」裡了，不用再進；在裡面說話用 \(Tools.Name.voiceSay)，要出來用 \(Tools.Name.voiceLeave)。")
        }
        guard ownerChannel != nil else {
            throw VoiceFailure("主人不在語音頻道裡，進不去。等他進了語音頻道、叫你進去再用 \(Tools.Name.voiceJoin)；現在跟他說話用 \(Tools.Name.dm)。")
        }
        guard pendingJoin == nil else { throw VoiceFailure("已經在進語音頻道了") }
        try await send(["type": "join"])
        return try await withCheckedThrowingContinuation { c in
            pendingJoin = c
            let t = config.bridgeTimeoutS
            Task { [weak self] in
                try? await Task.sleep(for: .seconds(t))
                await self?.timeoutJoin()
            }
        }
    }

    func timeoutJoin() {
        guard let p = pendingJoin else { return }
        pendingJoin = nil
        p.resume(throwing: VoiceFailure("bridge 沒有在 \(config.bridgeTimeoutS) 秒內回 joined"))
    }

    public func leave() async throws {
        guard herChannel != nil else {
            throw VoiceFailure("你不在語音頻道裡，不用離開；要跟主人說話用 \(Tools.Name.dm)。")
        }
        guard pendingLeave == nil else { throw VoiceFailure("已經在離開語音頻道了") }
        try await send(["type": "leave"])
        try await withCheckedThrowingContinuation { (c: CheckedContinuation<Void, Error>) in
            pendingLeave = c
            let t = config.bridgeTimeoutS
            Task { [weak self] in
                try? await Task.sleep(for: .seconds(t))
                await self?.timeoutLeave()
            }
        }
    }

    func timeoutLeave() {
        guard let p = pendingLeave else { return }
        pendingLeave = nil
        p.resume(throwing: VoiceFailure("bridge 沒有在 \(config.bridgeTimeoutS) 秒內回 left"))
    }

    func signal() {
        let w = waiters
        waiters = []
        for c in w { c.resume() }
    }

    /// Waits for the next change (played, stop, error) or `seconds`.
    func changed(within seconds: Double) async {
        await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
            waiters.append(c)
            Task { [weak self] in
                try? await Task.sleep(for: .seconds(seconds))
                await self?.signal()
            }
        }
    }

    static func pcm16(_ f: [Float]) -> Data {
        var d = Data(count: f.count * 2)
        d.withUnsafeMutableBytes { p in
            for (i, x) in f.enumerated() {
                let v = Int16(max(-32768, min(32767, (x * 32767).rounded())))
                p.storeBytes(of: v.littleEndian, toByteOffset: i * 2, as: Int16.self)
            }
        }
        return d
    }

    /// One `voice.say`: sentence by sentence through the TTS to the bridge, then
    /// until the bridge played it all or the owner cut it. Every sentence generated is kept.
    public func speak(_ text: String) async throws -> SpokenReport {
        guard herChannel != nil else {
            throw VoiceFailure("你不在語音頻道裡，這句沒說出口。主人叫你進去才用 \(Tools.Name.voiceJoin)；現在要跟主人說話用 \(Tools.Name.dm)。")
        }
        guard current == nil else { throw VoiceFailure("上一句還在說") }
        let sentences = Sentences.split(text, maxChars: config.sentenceMaxChars)
        guard !sentences.isEmpty else { throw VoiceFailure("沒有要念的字") }
        utteranceSerial += 1
        let id = utteranceSerial
        let utterance = Utterance(id: id, progressAt: Self.now())
        let shared = utterance.shared
        current = utterance
        defer { current = nil }
        try await send(["type": "utterance_start", "utterance": id])
        let t0 = Self.now()
        let first = Mutex<Double?>(nil)
        var made: [(text: String, language: String, synth: Synthesized, start: Double, end: Double)] = []
        for s in sentences {
            if shared.stopped { break }
            guard let socket else { throw VoiceFailure("語音連線斷了") }
            let lang = config.ttsLanguage == "detect" ? Sentences.language(s) : config.ttsLanguage
            let start = Self.now()
            let r = try await synthesizer.synthesize(s, language: lang, shouldStop: { shared.stopped }) { chunk in
                // Under the stop lock: a chunk is queued before a `stop` or not at all.
                shared.stop.withLock { stopped in
                    guard !stopped else { return }
                    first.withLock { if $0 == nil { $0 = Self.now() - t0 } }
                    shared.sent.withLock { $0 += chunk.count }
                    socket.enqueue(.binary(Self.pcm16(chunk)))
                }
            }
            made.append((s, lang, r, start, Self.now()))
        }
        if !shared.stopped {
            current!.ended = true
            try await send(["type": "utterance_end", "utterance": id])
        }
        // Until it was all played, or the owner cut it and the bridge reported its final count.
        var settleUntil: Double?
        while true {
            let u = current!
            if let f = u.failure { throw VoiceFailure(f) }
            // Everything sent was played: done, cut or not (a stop after the last sample needs no final report).
            if u.played >= u.sent, u.ended || u.stopSentAt != nil { break }
            if u.stopSentAt != nil, u.playedAfterStop {
                if settleUntil == nil { settleUntil = Self.now() + Double(config.stopSettleMs) / 1000 }
                if Self.now() >= settleUntil! { break }
                await changed(within: max(0.001, settleUntil! - Self.now()))
                continue
            }
            if Self.now() - u.progressAt > config.bridgeTimeoutS {
                if u.stopSentAt == nil {
                    shared.stop.withLock { $0 = true }
                    socket?.enqueue(.text(Self.json(["type": "stop", "utterance": id])))
                }
                throw VoiceFailure("bridge 超過 \(config.bridgeTimeoutS) 秒沒有回報播放進度（utterance \(id)，播了 \(u.played)/\(u.sent)）")
            }
            await changed(within: config.bridgeTimeoutS)
        }
        let u = current!
        let report = SpokenReport(sentences: sentences, samples: made.map { $0.synth.samples.count }, played: u.played,
                                  interrupted: u.stopSentAt != nil, firstAudio: first.withLock { $0 })
        // Keep every generated sentence for the night.
        var offset = 0
        let stamp = Self.stamp(session0)
        for (i, m) in made.enumerated() {
            let n = m.synth.samples.count
            let played = max(0, min(n, u.played - offset))
            offset += n
            try manifest.append(VoiceManifest.Entry(
                file: "\(stamp)-u\(id)-s\(i + 1).wav", text: m.text, language: m.language, seed: m.synth.seed, session: session0,
                utterance: id, sentence: i + 1, startTs: m.start, endTs: m.end, sampleRate: synthesizer.sampleRate, samples: n,
                generationComplete: m.synth.complete, playedSamples: played, interrupted: played < n), audio: m.synth.samples)
        }
        log("INFO", "utterance \(id): \(sentences.count) sentences, \(made.count) generated, played \(u.played)/\(u.sent)"
            + (report.firstAudio.map { String(format: ", first audio %.0f ms", $0 * 1000) } ?? "")
            + (report.interrupted ? ", cut by the owner: heard「\(report.heardText)」" : ""))
        return report
    }

    static func stamp(_ ts: Double) -> String {
        let c = Calendar.current.dateComponents([.year, .month, .day, .hour, .minute, .second], from: Date(timeIntervalSince1970: ts))
        return String(format: "%04d%02d%02d-%02d%02d%02d", c.year!, c.month!, c.day!, c.hour!, c.minute!, c.second!)
    }
}
