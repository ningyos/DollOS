import Foundation
import Synchronization

/// The Discord bridge's process (2026-11-12): dollcore starts it at boot and watches it. Its stdout
/// and stderr lines go to dollcore's stderr prefixed `bridge: `.
///
/// Ready = it opened the owner stream on the local API (it does that once Discord says ready and
/// the owner's DM channel is known). Boot fails if it exits first or is not ready within
/// `[bridge] ready_timeout_s`. Once up, an exit is a system error: logged as ERROR, shown by
/// `dollctl status`, and every `discord.dm` from then on fails with the reason in her result.
/// It is not restarted: py-cord already reconnects to the gateway on its own, so an exit means
/// something a restart would not fix (bad token, missing intent, a crash), and a silent restart
/// would hide it. Restart dollcore after fixing the cause.
public final class Bridge: @unchecked Sendable {
    let config: BridgeConfig
    let api: LocalAPI
    let root: URL
    private let process = Process()
    private let stopping = Mutex(false)
    private let exited = Mutex<String?>(nil)
    /// The bridge's exit, for tests and the boot check.
    public var exit: String? { exited.withLock { $0 } }

    public init(config: BridgeConfig, api: LocalAPI, root: URL) {
        self.config = config
        self.api = api
        self.root = root
    }

    /// Launches it and waits until it is ready.
    public func start() async throws {
        api.setBridgeState("starting")
        process.executableURL = URL(filePath: config.command[0])
        process.arguments = Array(config.command.dropFirst())
        process.currentDirectoryURL = root
        var env = ProcessInfo.processInfo.environment
        env["DOLL_API"] = api.baseURL
        env["DOLL_BRIDGE_TOKEN"] = api.ownerKey
        process.environment = env
        process.standardInput = FileHandle.nullDevice
        let out = Pipe(), err = Pipe()
        process.standardOutput = out
        process.standardError = err
        Self.forward(out)
        Self.forward(err)
        process.terminationHandler = { [weak self] p in self?.ended(p) }
        do {
            try process.run()
        } catch {
            throw StoreError(description: "bridge: cannot start \(config.command[0]): \(error)")
        }
        let deadline = Date().addingTimeInterval(config.readyTimeoutS)
        while api.listening(.owner) == 0 {
            if let why = exit { throw StoreError(description: "bridge exited before it was ready: \(why)") }
            guard Date() < deadline else {
                stop()
                throw StoreError(description: "bridge not ready within \(config.readyTimeoutS) s ([bridge] ready_timeout_s); stopped it")
            }
            try await Task.sleep(for: .milliseconds(100))
        }
        api.setBridgeState("up (pid \(process.processIdentifier))")
        Log.write("INFO", "bridge up (pid \(process.processIdentifier))")
    }

    private func ended(_ p: Process) {
        let how = p.terminationReason == .uncaughtSignal ? "killed by signal \(p.terminationStatus)" : "exit status \(p.terminationStatus)"
        let at = Date().formatted(date: .omitted, time: .standard)
        exited.withLock { $0 = how }
        guard !stopping.withLock({ $0 }) else { return }
        api.setBridgeState("down: \(how) at \(at); not restarted (fix the cause, restart dollcore)")
        Log.write("ERROR", "bridge exited (\(how)); Discord is down; not restarted: fix the cause and restart dollcore")
    }

    /// Ends it (SIGTERM); its exit is not an error.
    public func stop() {
        stopping.withLock { $0 = true }
        if process.isRunning { process.terminate() }
    }

    /// Waits up to `seconds` for it to exit after `stop()`.
    public func waitUntilExit(seconds: Double) async {
        let deadline = Date().addingTimeInterval(seconds)
        while process.isRunning, Date() < deadline { try? await Task.sleep(for: .milliseconds(50)) }
    }

    /// Copies one of its pipes to stderr line by line.
    private static func forward(_ pipe: Pipe) {
        let pending = Mutex(Data())
        pipe.fileHandleForReading.readabilityHandler = { h in
            let chunk = h.availableData
            let lines: [Data] = pending.withLock { buf in
                if chunk.isEmpty {
                    // End of file: the rest, then stop reading.
                    h.readabilityHandler = nil
                    defer { buf = Data() }
                    return buf.isEmpty ? [] : [buf]
                }
                buf.append(chunk)
                var out: [Data] = []
                while let nl = buf.firstIndex(of: UInt8(ascii: "\n")) {
                    out.append(buf[buf.startIndex..<nl])
                    buf = Data(buf[buf.index(after: nl)...])
                }
                return out
            }
            for l in lines { FileHandle.standardError.write(Data("bridge: ".utf8) + l + Data("\n".utf8)) }
        }
    }
}
