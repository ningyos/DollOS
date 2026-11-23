import Foundation
import LocalAuthentication

/// The owner's fingerprint before the memory key is used (2026-11-23, owner: 「開機時 Touch ID 解鎖並
/// 按需解密到 RAM」; `[secrets] touch_id`). Asked once per process — at boot, and by `dollcore … log`
/// and `consolidate` — before the key is read from the login Keychain; the key then stays in memory.
/// Refused or unavailable: the process does not start. It gates this program's use of the key; any
/// other process running as the owner can still read the Keychain item (see docs/secrets.md).
public enum OwnerPresence {
    public static func require(reason: String) async throws {
        let ctx = LAContext()
        var err: NSError?
        guard ctx.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: &err) else {
            throw StoreError(description: "Touch ID is not available ([secrets] touch_id): \(err?.localizedDescription ?? "unknown")")
        }
        do {
            try await ctx.evaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, localizedReason: reason)
        } catch {
            throw StoreError(description: "Touch ID refused ([secrets] touch_id): \(error.localizedDescription)")
        }
    }
}
