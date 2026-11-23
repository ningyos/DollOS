import Foundation

/// The owner's lines and his lock (2026-11-21; 2026-11-23, owner: 「secret check 換成輸出檢查後就可以
/// 拿掉了，只靠 doll 自律跟輸出 filtering」, 「哪些算秘密由 doll 去判斷」). No program judges his line:
/// it reaches her as he wrote it; she marks a line secret herself (`secret.mark`), and her turn's
/// timeline lines are written only when the turn ends (`Timeline.hold`), so a line she marks is never
/// in the file in plain text. A line whose first character is 🔒 is a secret from the start (his own
/// mark). The log never holds his words.
public enum SecretCheck {
    /// The owner's lock: a line whose first character is this is a secret. The line keeps it.
    public static let lock: Unicode.Scalar = "\u{1F512}"

    public static func isLocked(_ text: String) -> Bool { text.unicodeScalars.first == lock }
}

extension Doll {
    /// The owner's line from the bridge (Discord text) or his voice (a transcript): written to the
    /// inbox first (`Inbox.swift`, sealed; an error when it cannot be, and the caller says so), then
    /// queued (`ownerSaid`). His voice cancels her call in flight there.
    public func ownerLine(_ text: String, voice: Bool = false, channel: Channel = .discordDM) throws {
        inboxSerial += 1
        let name = try inbox.put(Inbox.Line(text: text, voice: voice, channel: channel, received: wallClock.now()), serial: inboxSerial)
        ownerSaid(text, secret: SecretCheck.isLocked(text), voice: voice, channel: channel, inbox: name)
    }
}
