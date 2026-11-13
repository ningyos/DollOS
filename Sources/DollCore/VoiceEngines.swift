import AVFoundation
import CoreMedia
import Foundation
import MLX
import Speech
import Synchronization
import VoiceAudio
import VoiceMLX

/// One MLX runtime per process (2026-11-13, M6): the embedding model and her voice share it.
public enum MLXProcess {
    /// `[core] mlx_cache_limit_mb`; call before any model loads.
    public static func configure(cacheLimitMB: Int) {
        locateKernels()
        MLX.Memory.cacheLimit = cacheLimitMB * 1024 * 1024
    }

    /// SwiftPM builds MLX's Metal kernels into mlx-swift_Cmlx.bundle beside this code's binary; MLX
    /// finds it there for `swift run` but not under `swift test` (see `MLXEmbedder.load`).
    static func locateKernels() {
        if GPU.metallib == nil, let lib = Bundle(for: QwenSynthesizer.self).resourceURL?
            .appending(path: "mlx-swift_Cmlx.bundle/Contents/Resources/default.metallib"),
            FileManager.default.fileExists(atPath: lib.path) {
            GPU.metallib = lib
        }
    }

    /// Current and lifetime-peak physical footprint of this process in bytes (what Activity Monitor
    /// shows as Memory; includes MLX's Metal buffers and cache).
    public static func footprint() -> (current: UInt64, peak: UInt64) {
        var info = rusage_info_v4()
        let rc = withUnsafeMutablePointer(to: &info) {
            $0.withMemoryRebound(to: rusage_info_t?.self, capacity: 1) { proc_pid_rusage(getpid(), RUSAGE_INFO_V4, $0) }
        }
        guard rc == 0 else { return (0, 0) }
        return (info.ri_phys_footprint, info.ri_lifetime_max_phys_footprint)
    }
}

/// The owner's speech through Apple's SpeechTranscriber (SpeechAnalyzer), volatile + fast results.
/// Its own finals come only every ~12 s; `finalize(through:)` (from our endpointing) forces one.
public actor AppleSpeechRecognizer: SpeechRecognizer {
    let analyzer: SpeechAnalyzer
    let input: AsyncStream<AnalyzerInput>.Continuation
    let source: AVAudioFormat
    let target: AVAudioFormat
    let converter: AVAudioConverter?
    let finalTimeout: Duration
    var finals: [String] = []
    var finalCount = 0
    var results: Task<Void, Never>?
    var failure: String?

    init(analyzer: SpeechAnalyzer, input: AsyncStream<AnalyzerInput>.Continuation, target: AVAudioFormat, finalTimeoutMs: Int) throws {
        self.analyzer = analyzer
        self.input = input
        self.target = target
        source = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: Double(Endpointer.sampleRate), channels: 1, interleaved: true)!
        converter = source == target ? nil : AVAudioConverter(from: source, to: target)
        if source != target, converter == nil { throw VoiceFailure("speech: cannot convert 16 kHz s16 to \(target)") }
        finalTimeout = .milliseconds(finalTimeoutMs)
    }

    /// Checks the locale, installs its assets if the system offers them, and starts the analyzer.
    public static func start(locale identifier: String, finalTimeoutMs: Int) async throws -> AppleSpeechRecognizer {
        guard let locale = await SpeechTranscriber.supportedLocale(equivalentTo: Locale(identifier: identifier)) else {
            throw VoiceFailure("speech: SpeechTranscriber does not support \(identifier) ([voice] asr_locale)")
        }
        let transcriber = SpeechTranscriber(locale: locale, transcriptionOptions: [],
                                            reportingOptions: [.volatileResults, .fastResults], attributeOptions: [])
        if let request = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
            Log.write("INFO", "voice: installing SpeechTranscriber assets for \(locale.identifier)")
            try await request.downloadAndInstall()
        }
        let analyzer = SpeechAnalyzer(modules: [transcriber])
        guard let format = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [transcriber]) else {
            throw VoiceFailure("speech: no audio format for SpeechTranscriber \(locale.identifier)")
        }
        let (stream, cont) = AsyncStream.makeStream(of: AnalyzerInput.self)
        let r = try AppleSpeechRecognizer(analyzer: analyzer, input: cont, target: format, finalTimeoutMs: finalTimeoutMs)
        await r.collect(transcriber)
        try await analyzer.start(inputSequence: stream)
        return r
    }
    func collect(_ t: SpeechTranscriber) {
        results = Task { [weak self] in
            do {
                for try await r in t.results where r.isFinal {
                    await self?.final(String(r.text.characters))
                }
            } catch {
                await self?.failed("\(error)")
            }
        }
    }

    func final(_ text: String) {
        finals.append(text)
        finalCount += 1
    }

    func failed(_ why: String) {
        failure = why
        Log.write("ERROR", "voice: SpeechTranscriber results ended: \(why)")
    }

    public func feed(_ samples: [Int16]) throws {
        if let failure { throw VoiceFailure("speech recognizer stopped: \(failure)") }
        guard !samples.isEmpty else { return }
        let buf = AVAudioPCMBuffer(pcmFormat: source, frameCapacity: AVAudioFrameCount(samples.count))!
        buf.frameLength = AVAudioFrameCount(samples.count)
        samples.withUnsafeBufferPointer { buf.int16ChannelData![0].update(from: $0.baseAddress!, count: samples.count) }
        guard let converter else {
            input.yield(AnalyzerInput(buffer: buf))
            return
        }
        let cap = AVAudioFrameCount(Double(samples.count) * target.sampleRate / source.sampleRate) + 64
        let out = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: cap)!
        var given = false
        var err: NSError?
        converter.convert(to: out, error: &err) { _, status in
            if given { status.pointee = .noDataNow; return nil }
            given = true
            status.pointee = .haveData
            return buf
        }
        if let err { throw VoiceFailure("speech: audio conversion failed: \(err)") }
        if out.frameLength > 0 { input.yield(AnalyzerInput(buffer: out)) }
    }

    /// The final for everything up to `seconds`. The wait is bounded by `asr_final_timeout_ms`
    /// whether or not `analyzer.finalize` returned: measured, a finalize over silence alone never
    /// returns, and one stuck call must not hold every later utterance. No final in time = no
    /// words (logged; a stuck finalize is logged as an ERROR).
    public func finalize(through seconds: Double) async throws -> String {
        if let failure { throw VoiceFailure("speech recognizer stopped: \(failure)") }
        let before = finalCount
        // nil: still running; "": returned; else the error.
        final class Call: Sendable { let outcome = Mutex<String?>(nil) }
        let call = Call()
        let analyzer = analyzer
        Task {
            do {
                try await analyzer.finalize(through: CMTime(seconds: seconds, preferredTimescale: CMTimeScale(Endpointer.sampleRate)))
                call.outcome.withLock { $0 = "" }
            } catch {
                call.outcome.withLock { $0 = "\(error)" }
            }
        }
        let deadline = ContinuousClock.now + finalTimeout
        while finalCount == before, ContinuousClock.now < deadline {
            if let e = call.outcome.withLock({ $0 }), !e.isEmpty { throw VoiceFailure("speech: finalize failed: \(e)") }
            try await Task.sleep(for: .milliseconds(5))
        }
        if finalCount == before {
            if call.outcome.withLock({ $0 }) == nil {
                Log.write("ERROR", "voice: SpeechTranscriber finalize(through: \(seconds)) did not return within \(finalTimeout)")
            } else {
                Log.write("INFO", "voice: no final within \(finalTimeout) (no words)")
            }
        }
        let text = finals.joined()
        finals = []
        return text
    }
}

/// Her voice: Qwen3-TTS 0.6B Base through voice-mlx, ICL with one reference encoded at startup;
/// the warm-up line compiles the talker and seeds the streaming decoder from the reference, so
/// the first real sentence pays neither.
public final class QwenSynthesizer: SpeechSynthesizer, @unchecked Sendable {
    let model: Qwen3Voice
    let reference: VoiceReference
    /// One generation at a time (voice-mlx is not reentrant).
    let busy = Mutex(false)
    public var sampleRate: Int { 24000 }

    init(model: Qwen3Voice, reference: VoiceReference) {
        self.model = model
        self.reference = reference
    }

    static func source(_ s: String) -> ModelSource {
        s.hasPrefix("/") ? .directory(URL(filePath: s)) : .hub(s)
    }

    /// Loads the bundles, encodes the reference, speaks `warmup_text` once (discarded).
    public static func load(_ c: VoiceConfig, root: URL) async throws -> QwenSynthesizer {
        MLXProcess.locateKernels()
        func path(_ p: String) -> URL { p.hasPrefix("/") ? URL(filePath: p) : root.appending(path: p) }
        var options = LoadOptions()
        guard let precision = WeightPrecision(rawValue: c.ttsPrecision) else {
            throw VoiceFailure("[voice] tts_precision \(c.ttsPrecision) is not one of \(WeightPrecision.allCases.map(\.rawValue))")
        }
        options.precision = precision
        let model = try await Qwen3Voice.load(model: source(c.ttsModel), codec: source(c.ttsCodec), options: options)
        let transcript = try String(contentsOf: path(c.referenceText), encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !transcript.isEmpty else { throw VoiceFailure("[voice] reference_text \(c.referenceText) is empty") }
        let reference = try model.encodeReference(contentsOf: path(c.referenceAudio), transcript: transcript)
        let s = QwenSynthesizer(model: model, reference: reference)
        let warm = try await s.synthesize(c.warmupText, language: c.ttsLanguage == "detect" ? Sentences.language(c.warmupText) : c.ttsLanguage,
                                          shouldStop: { false }, onChunk: { _ in })
        guard warm.complete, !warm.samples.isEmpty else { throw VoiceFailure("voice: the warm-up line produced no audio") }
        return s
    }

    public func synthesize(_ text: String, language: String, shouldStop: @escaping @Sendable () -> Bool,
                           onChunk: @escaping @Sendable ([Float]) -> Void) async throws -> Synthesized {
        guard busy.withLock({ let was = $0; $0 = true; return !was }) else { throw VoiceFailure("voice: the TTS is already generating") }
        defer { busy.withLock { $0 = false } }
        let model = model, reference = reference
        let seed = UInt64.random(in: 0...UInt64(UInt32.max))
        return try await Task.detached {
            var options = GenerationOptions()
            options.seed = seed
            let samples = Mutex<[Float]>([])
            struct Stopped: Error {}
            do {
                let out = try model.generateStreaming(text: text, language: language, voice: .reference(reference), options: options) { chunk in
                    if shouldStop() { throw Stopped() }
                    samples.withLock { $0 += chunk.samples }
                    onChunk(chunk.samples)
                }
                return Synthesized(seed: out.seed, samples: samples.withLock { $0 }, complete: true)
            } catch is Stopped {
                return Synthesized(seed: seed, samples: samples.withLock { $0 }, complete: false)
            }
        }.value
    }
}

/// Writes one sentence's wav (24 kHz mono float32, voice-mlx's writer).
public let writeVoiceWAV: @Sendable ([Float], Int, URL) throws -> Void = { samples, rate, url in
    try AudioIO.writeWAV(samples, sampleRate: rate, to: url)
}
