import CryptoKit
import Foundation
import Network
import Synchronization

/// RFC 6455 framing for the local API's one WebSocket route, `/api/voice` (2026-11-13, M6).
/// Hand-rolled because the API's listener speaks plain HTTP on the same port; Network.framework's
/// WebSocket protocol would own the whole listener. No extensions (no permessage-deflate), no
/// subprotocols. A client's frames must be masked and a server's must not (RFC 6455 5.1); anything
/// else is a protocol error that ends the connection.
public enum WebSocket {
    public enum Opcode: UInt8, Sendable {
        case continuation = 0x0, text = 0x1, binary = 0x2, close = 0x8, ping = 0x9, pong = 0xA
    }

    /// One complete message (fragments joined) or control frame.
    public enum Message: Sendable, Equatable {
        case text(String)
        case binary(Data)
        case close(code: UInt16?, reason: String)
        case ping(Data)
        case pong(Data)
    }

    public struct ProtocolError: Error, CustomStringConvertible, Equatable {
        public var description: String
        public init(_ d: String) { description = d }
    }

    /// `Sec-WebSocket-Accept` for a client's `Sec-WebSocket-Key`.
    public static func accept(key: String) -> String {
        let digest = Insecure.SHA1.hash(data: Data((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").utf8))
        return Data(digest).base64EncodedString()
    }

    /// The handshake response, or why the request is not a WebSocket upgrade.
    public static func handshake(headers: [String: String]) throws -> Data {
        guard headers["upgrade"]?.lowercased() == "websocket" else { throw ProtocolError("Upgrade: websocket required") }
        guard headers["connection"]?.lowercased().split(separator: ",").contains(where: {
            $0.trimmingCharacters(in: .whitespaces) == "upgrade"
        }) == true else { throw ProtocolError("Connection: Upgrade required") }
        guard headers["sec-websocket-version"] == "13" else { throw ProtocolError("Sec-WebSocket-Version: 13 required") }
        guard let key = headers["sec-websocket-key"], Data(base64Encoded: key)?.count == 16 else {
            throw ProtocolError("Sec-WebSocket-Key missing or not 16 bytes of base64")
        }
        return Data(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            + "Sec-WebSocket-Accept: \(accept(key: key))\r\n\r\n").utf8)
    }

    /// One frame. `mask` nil: unmasked (server → client); a client passes its 4 masking bytes.
    public static func frame(_ op: Opcode, _ payload: Data, fin: Bool = true, mask: [UInt8]? = nil) -> Data {
        var out = Data()
        out.reserveCapacity(payload.count + 14)
        out.append((fin ? 0x80 : 0) | op.rawValue)
        let m: UInt8 = mask == nil ? 0 : 0x80
        switch payload.count {
        case ..<126:
            out.append(m | UInt8(payload.count))
        case ..<65536:
            out.append(m | 126)
            out.append(UInt8(payload.count >> 8))
            out.append(UInt8(payload.count & 0xFF))
        default:
            out.append(m | 127)
            for shift in stride(from: 56, through: 0, by: -8) { out.append(UInt8((UInt64(payload.count) >> UInt64(shift)) & 0xFF)) }
        }
        if let mask {
            precondition(mask.count == 4)
            out.append(contentsOf: mask)
            var i = 0
            for b in payload {
                out.append(b ^ mask[i & 3])
                i += 1
            }
        } else {
            out.append(payload)
        }
        return out
    }

    public static func message(_ m: Message, mask: [UInt8]? = nil) -> Data {
        switch m {
        case .text(let s): frame(.text, Data(s.utf8), mask: mask)
        case .binary(let d): frame(.binary, d, mask: mask)
        case .ping(let d): frame(.ping, d, mask: mask)
        case .pong(let d): frame(.pong, d, mask: mask)
        case .close(let code, let reason):
            frame(.close, code.map { Data([UInt8($0 >> 8), UInt8($0 & 0xFF)]) + Data(reason.utf8) } ?? Data(), mask: mask)
        }
    }

    /// Incremental parser: feed bytes as they arrive, take whole messages out.
    public struct Parser: Sendable {
        /// Frames from a client must be masked (server side); from a server must not (client side).
        let expectMasked: Bool
        let maxMessage: Int
        var buffer = Data()
        var fragments: (op: Opcode, data: Data)?

        public init(expectMasked: Bool, maxMessage: Int) {
            self.expectMasked = expectMasked
            self.maxMessage = maxMessage
        }

        public mutating func feed(_ data: Data) throws -> [Message] {
            buffer.append(data)
            var out: [Message] = []
            while let m = try next() { out.append(m) }
            return out
        }

        mutating func next() throws -> Message? {
            while true {
                let b = [UInt8](buffer.prefix(14))
                guard b.count >= 2 else { return nil }
                let fin = b[0] & 0x80 != 0
                guard b[0] & 0x70 == 0 else { throw ProtocolError("reserved bits set (no extension was negotiated)") }
                guard let op = Opcode(rawValue: b[0] & 0x0F) else { throw ProtocolError("unknown opcode \(b[0] & 0x0F)") }
                let masked = b[1] & 0x80 != 0
                guard masked == expectMasked else {
                    throw ProtocolError(expectMasked ? "client frame not masked" : "server frame masked")
                }
                var len = Int(b[1] & 0x7F)
                var head = 2
                if len == 126 {
                    guard b.count >= 4 else { return nil }
                    len = Int(b[2]) << 8 | Int(b[3])
                    head = 4
                } else if len == 127 {
                    guard b.count >= 10 else { return nil }
                    var n: UInt64 = 0
                    for i in 2..<10 { n = n << 8 | UInt64(b[i]) }
                    guard n <= UInt64(maxMessage) else { throw ProtocolError("frame of \(n) bytes exceeds \(maxMessage)") }
                    len = Int(n)
                    head = 10
                }
                guard len <= maxMessage else { throw ProtocolError("frame of \(len) bytes exceeds \(maxMessage)") }
                let maskAt = head
                if masked { head += 4 }
                guard buffer.count >= head + len else { return nil }
                let start = buffer.startIndex
                var payload = Data(buffer[(start + head)..<(start + head + len)])
                if masked {
                    let key = [UInt8](buffer[(start + maskAt)..<(start + maskAt + 4)])
                    payload.withUnsafeMutableBytes { p in
                        for i in 0..<p.count { p[i] ^= key[i & 3] }
                    }
                }
                buffer.removeSubrange(start..<(start + head + len))
                buffer = Data(buffer)   // re-base indices

                switch op {
                case .close, .ping, .pong:
                    guard fin, len <= 125 else { throw ProtocolError("control frame fragmented or over 125 bytes") }
                    if op == .ping { return .ping(payload) }
                    if op == .pong { return .pong(payload) }
                    guard payload.count != 1 else { throw ProtocolError("close frame with a 1-byte payload") }
                    let code = payload.count >= 2 ? UInt16(payload[payload.startIndex]) << 8 | UInt16(payload[payload.startIndex + 1]) : nil
                    let reason = payload.count > 2 ? String(decoding: payload.dropFirst(2), as: UTF8.self) : ""
                    return .close(code: code, reason: reason)
                case .text, .binary:
                    guard fragments == nil else { throw ProtocolError("new data frame inside a fragmented message") }
                    if fin { return try Self.whole(op, payload) }
                    fragments = (op, payload)
                case .continuation:
                    guard var f = fragments else { throw ProtocolError("continuation frame without a message") }
                    f.data.append(payload)
                    guard f.data.count <= maxMessage else { throw ProtocolError("message exceeds \(maxMessage) bytes") }
                    if fin {
                        fragments = nil
                        return try Self.whole(f.op, f.data)
                    }
                    fragments = f
                }
            }
        }

        static func whole(_ op: Opcode, _ data: Data) throws -> Message {
            if op == .binary { return .binary(data) }
            guard let s = String(data: data, encoding: .utf8) else { throw ProtocolError("text frame is not UTF-8") }
            return .text(s)
        }
    }
}

/// The server side of one upgraded connection: sends frames, and hands every received message to
/// `messages` (pings are answered here; a close is answered and ends the stream). A protocol error
/// closes with 1002 and ends the stream with that error.
public final class WebSocketSession: @unchecked Sendable {
    let connection: NWConnection
    public let messages: AsyncThrowingStream<WebSocket.Message, Error>
    private let continuation: AsyncThrowingStream<WebSocket.Message, Error>.Continuation
    private var parser: WebSocket.Parser
    private let closed = Mutex(false)

    /// `leftover`: bytes that came after the handshake request in the same read.
    init(connection: NWConnection, leftover: Data, maxMessage: Int) {
        self.connection = connection
        parser = WebSocket.Parser(expectMasked: true, maxMessage: maxMessage)
        (messages, continuation) = AsyncThrowingStream.makeStream(of: WebSocket.Message.self)
        if !leftover.isEmpty { handle(leftover) }
        receive()
    }

    public var isClosed: Bool { closed.withLock { $0 } }

    /// Sends one message; false when the connection is gone.
    @discardableResult
    public func send(_ m: WebSocket.Message) async -> Bool {
        guard !isClosed else { return false }
        return await withCheckedContinuation { cont in
            connection.send(content: WebSocket.message(m), completion: .contentProcessed { cont.resume(returning: $0 == nil) })
        }
    }

    /// Queues one message without waiting for it to be written (audio chunks from the TTS thread).
    public func enqueue(_ m: WebSocket.Message) {
        guard !isClosed else { return }
        connection.send(content: WebSocket.message(m), completion: .idempotent)
    }

    public func close(code: UInt16 = 1000, reason: String = "") {
        guard closed.withLock({ let was = $0; $0 = true; return !was }) else { return }
        connection.send(content: WebSocket.message(.close(code: code, reason: reason)), completion: .contentProcessed { [connection] _ in
            connection.cancel()
        })
        continuation.finish()
    }

    private func fail(_ error: WebSocket.ProtocolError) {
        guard closed.withLock({ let was = $0; $0 = true; return !was }) else { return }
        connection.send(content: WebSocket.message(.close(code: 1002, reason: String(error.description.prefix(100)))),
                        completion: .contentProcessed { [connection] _ in connection.cancel() })
        continuation.finish(throwing: error)
    }

    private func handle(_ data: Data) {
        do {
            for m in try parser.feed(data) {
                switch m {
                case .ping(let d): enqueue(.pong(d))
                case .close(let code, _):
                    continuation.yield(m)
                    close(code: code ?? 1000)
                    return
                default: continuation.yield(m)
                }
            }
        } catch let e as WebSocket.ProtocolError {
            fail(e)
        } catch {
            fail(WebSocket.ProtocolError("\(error)"))
        }
    }

    private func receive() {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 1 << 16) { [weak self] data, _, done, error in
            guard let self else { return }
            if let data, !data.isEmpty { self.handle(data) }
            if self.isClosed { return }
            if done || error != nil {
                self.closed.withLock { $0 = true }
                self.connection.cancel()
                self.continuation.finish(throwing: error.map { WebSocket.ProtocolError("connection lost: \($0)") })
                return
            }
            self.receive()
        }
    }
}
