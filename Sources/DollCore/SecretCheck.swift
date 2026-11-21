import Foundation

/// Whether an owner line is a secret (owner, 2026-11-21): the model decides, he can force it.
/// Before his line is written anywhere (timeline, log, signals) the program asks the main model one
/// separate small question about it, without tools and without thinking; 是 makes the line a secret
/// event (stored encrypted, kept out of the log, seen only in turns with him). A line that starts
/// with 🔒 is a secret without asking. Anything but a clean 是 or 否 (a network error, a timeout, an
/// unreadable answer) also makes it a secret: a failure never leaves plaintext.
public enum SecretCheck {
    /// The owner's lock: a line whose first character is this is a secret, no question asked. The
    /// line keeps it (she sees he locked it).
    public static let lock: Unicode.Scalar = "\u{1F512}"

    /// The question, sent as the system message; his line is the user message.
    public static let instruction = """
        你是一個過濾器，只判斷，不回話。使用者訊息是主人說的一句話。
        如果裡面有秘密——密碼、PIN、驗證碼、帳號或卡號、銀行或財務資料、薪水、病情或健康狀況、住址、別人的私事，或他不想讓別人知道的事——回「是」。
        沒有就回「否」。
        只回一個字：是 或 否。
        """

    /// What the check found, with how long it took.
    public enum Verdict: Equatable, Sendable {
        /// The line starts with 🔒: no request.
        case locked
        case secret
        case ordinary
        /// No clean answer (the reason, never the line or the answer's text): treated as secret.
        case failed(String)

        public var isSecret: Bool { self != .ordinary }
    }

    public static func isLocked(_ text: String) -> Bool { text.unicodeScalars.first == lock }

    public static func messages(_ text: String) -> [ChatMessage] {
        [ChatMessage(role: "system", content: instruction), ChatMessage(role: "user", content: text)]
    }

    /// 是 → true, 否 → false; anything else (other words, both, empty) nil. Surrounding spaces and
    /// one closing punctuation mark are allowed.
    public static func read(_ answer: String) -> Bool? {
        var a = answer.trimmingCharacters(in: .whitespacesAndNewlines)
        if let last = a.last, "。.!！".contains(last) { a.removeLast() }
        switch a {
        case "是": return true
        case "否": return false
        default: return nil
        }
    }

    /// The check of one line through `model` (the same call the doll makes), answered within
    /// `seconds` on `clock` or failed. The model's requests wait out a lost connection
    /// (`HTTPMainModel.send`); the check waits with them only this long, the one request's own
    /// `[llm] timeout_s` as before the retries (2026-11-22): a short outage still gets a real answer,
    /// a longer one makes the line a secret, and the lines behind it are not held without end.
    public static func ask(_ text: String, model: any MainModel, within seconds: Double, clock: any DollClock) async -> Verdict {
        if isLocked(text) { return .locked }
        let deadline = clock.now() + seconds
        return await withTaskGroup(of: Verdict?.self) { group in
            group.addTask { await answer(text, model: model) }
            group.addTask {
                do { try await clock.sleep(until: deadline) } catch { return nil }
                return .failed(String(format: "no answer within %.0f s", seconds))
            }
            var verdict = Verdict.failed("cancelled")
            while let v = await group.next() {
                guard let v else { continue }
                verdict = v
                break
            }
            group.cancelAll()
            return verdict
        }
    }

    static func answer(_ text: String, model: any MainModel) async -> Verdict {
        do {
            let reply = try await model.chat(messages(text), tools: nil, effort: "none")
            guard let secret = read(reply.content) else {
                return .failed("unreadable answer (\(reply.content.count) characters, finish \(reply.usage.finish ?? "?"))")
            }
            return secret ? .secret : .ordinary
        } catch is CancellationError {
            return .failed("cancelled")
        } catch {
            // The reason's kind only: an error text could quote the request.
            let text = "\(error)"
            return .failed(String(text.prefix { $0 != ":" }.prefix(60)))
        }
    }
}

extension Doll {
    /// The owner's line from the bridge (Discord text) or his voice (a transcript): written to the
    /// inbox first (`Inbox.swift`, sealed; an error when it cannot be, and the caller says so), then
    /// checked (`SecretCheck`), then queued (`ownerSaid`). Returns once it is on disk, with the check's
    /// task (the bridge's request is answered then, without waiting for the check); the line is held
    /// until the check returns: nothing of it is written in plain text before, and lines are checked
    /// and queued in the order they came. She is not stopped meanwhile (a turn in flight goes on; the
    /// check waits behind it on a one-slot server), except by his voice: it cancels her call at once,
    /// and the rerun waits for the held lines.
    @discardableResult
    public func ownerLine(_ text: String, voice: Bool = false, channel: Channel = .discordDM) throws -> Task<Void, Never> {
        inboxSerial += 1
        let name = try inbox.put(Inbox.Line(text: text, voice: voice, channel: channel, received: wallClock.now()), serial: inboxSerial)
        return hold(text, voice: voice, channel: channel, inbox: name)
    }

    /// Holds a line (on disk in the inbox) until its check returns, behind the lines before it.
    @discardableResult
    func hold(_ text: String, voice: Bool, channel: Channel, inbox name: String) -> Task<Void, Never> {
        interruptMaintenance(by: "主人")
        if voice { inflight?.cancel() }
        heldLines += 1
        let previous = lineChecks
        let task = Task {
            await previous?.value
            await self.checkAndQueue(text, voice: voice, channel: channel, inbox: name)
        }
        lineChecks = task
        return task
    }

    func checkAndQueue(_ text: String, voice: Bool, channel: Channel, inbox name: String) async {
        let t0 = ContinuousClock.now
        let verdict = await SecretCheck.ask(text, model: model, within: llm.timeoutS, clock: wallClock)
        let ms = (ContinuousClock.now - t0).components
        let took = Int(ms.seconds * 1000 + ms.attoseconds / 1_000_000_000_000_000)
        switch verdict {
        case .locked: log("INFO", "owner line: 🔒, secret without a check")
        case .secret: log("INFO", "owner line: secret (check \(took) ms)")
        case .ordinary: log("INFO", "owner line: ordinary (check \(took) ms)")
        case .failed(let why): log("WARN", "owner line: the secret check failed after \(took) ms (\(why)); the line is treated as secret")
        }
        heldLines -= 1
        ownerSaid(text, secret: verdict.isSecret, voice: voice, channel: channel, inbox: name)
    }

    /// Before a rerun cancelled by his voice: the lines still being checked are queued first.
    func awaitHeldLines() async {
        while heldLines > 0, let t = lineChecks { await t.value }
    }
}
