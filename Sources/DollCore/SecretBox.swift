import CryptoKit
import Foundation
import Security

/// The memory key (owner, 2026-11-21): the owner's `secret` and her `own` folder are encrypted on disk
/// with one random 256-bit AES-GCM key per doll, kept in the login Keychain and so unlocked by his
/// macOS login. Symmetric only: there is no backup key, so if the Keychain item is lost the secrets
/// are lost.
///
/// Where the key lives: a generic password in the login Keychain, service `[memory] keychain_service`
/// (default `DollOS dollcore memory key`), account the doll's data dir as an absolute path (one key per
/// doll; two dolls of the same pack have different data dirs). Not synchronizable (local to this Mac,
/// it goes into Time Machine with the login keychain); `kSecAttrAccessibleAfterFirstUnlock`. Any
/// application of the logged-in user may read it without a prompt: the doll runs in the background and
/// is rebuilt often, and a prompt nobody answers would stop her boot. What it protects against is the
/// disk and its backups read without his login, not a program running as him.
public protocol KeyStore: AnyObject, Sendable {
    /// The key stored for `account`; nil when there is none.
    func load(account: String) throws -> Data?
    func save(_ key: Data, account: String) throws
    func delete(account: String) throws
}

public struct SecretBoxError: Error, CustomStringConvertible {
    public let description: String
    public init(_ description: String) { self.description = description }
}

/// The login Keychain (`KeyStore`).
public final class KeychainKeyStore: KeyStore {
    public let service: String

    public init(service: String) { self.service = service }

    private func base(_ account: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
         kSecAttrAccount as String: account, kSecAttrSynchronizable as String: false]
    }

    public func load(account: String) throws -> Data? {
        var query = base(account)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &out)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = out as? Data else {
            throw SecretBoxError("cannot read the memory key from the login Keychain (service \(service), account \(account)): \(Self.message(status))")
        }
        return data
    }

    public func save(_ key: Data, account: String) throws {
        var item = base(account)
        item[kSecValueData as String] = key
        item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        item[kSecAttrLabel as String] = "\(service) (\(account))"
        item[kSecAttrAccess as String] = try Self.anyApplication(label: service)
        let status = SecItemAdd(item as CFDictionary, nil)
        guard status == errSecSuccess else {
            throw SecretBoxError("cannot store the memory key in the login Keychain (service \(service), account \(account)): \(Self.message(status))")
        }
    }

    public func delete(account: String) throws {
        let status = SecItemDelete(base(account) as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw SecretBoxError("cannot delete the memory key (service \(service), account \(account)): \(Self.message(status))")
        }
    }

    /// An access whose decrypt ACL names no application list: any application, no prompt.
    private static func anyApplication(label: String) throws -> SecAccess {
        var access: SecAccess?
        guard SecAccessCreate(label as CFString, [] as CFArray, &access) == errSecSuccess, let access else {
            throw SecretBoxError("cannot create the Keychain access for the memory key")
        }
        for acl in SecAccessCopyMatchingACLList(access, kSecACLAuthorizationDecrypt) as? [SecACL] ?? [] {
            guard SecACLSetContents(acl, nil, label as CFString, SecKeychainPromptSelector()) == errSecSuccess else {
                throw SecretBoxError("cannot open the Keychain access for the memory key")
            }
        }
        return access
    }

    private static func message(_ status: OSStatus) -> String {
        (SecCopyErrorMessageString(status, nil) as String?) ?? "OSStatus \(status)"
    }
}

/// Keys in memory: for tests, never the login Keychain.
public final class MemoryKeyStore: KeyStore, @unchecked Sendable {
    private var keys: [String: Data] = [:]
    private let lock = NSLock()

    public init() {}

    public func load(account: String) throws -> Data? { lock.withLock { keys[account] } }
    public func save(_ key: Data, account: String) throws { lock.withLock { keys[account] = key } }
    public func delete(account: String) throws { lock.withLock { keys[account] = nil } }
}

/// The one place that encrypts and decrypts secret content (memory files of the `secret` and `own`
/// folders, secret timeline events, their history blobs).
///
/// Format, version 1: the text `dollbox1:` followed by the standard base64 of AES-GCM's combined
/// box — a 12-byte random nonce, the ciphertext, the 16-byte tag — with no associated data. A new
/// version gets a new prefix. Text is UTF-8 before sealing. File names and ids (`<seq>-<hex>.md`,
/// event seqs) stay plain: they carry no content.
public struct SecretBox: Sendable {
    public static let prefix = "dollbox1:"
    private let key: SymmetricKey

    public init(key: SymmetricKey) { self.key = key }

    public static func isSealed(_ s: String) -> Bool { s.hasPrefix(prefix) }

    public func seal(_ text: String) throws -> String {
        guard let combined = try AES.GCM.seal(Data(text.utf8), using: key).combined else {
            throw SecretBoxError("AES-GCM gave no combined box")
        }
        return Self.prefix + combined.base64EncodedString()
    }

    /// `boxed` opened; `place` names it in the error (a path, a timeline line).
    public func open(_ boxed: String, place: String) throws -> String {
        let trimmed = boxed.trimmingCharacters(in: .whitespacesAndNewlines)
        guard Self.isSealed(trimmed) else { throw SecretBoxError("\(place) is not encrypted (no \(Self.prefix) header)") }
        guard let data = Data(base64Encoded: String(trimmed.dropFirst(Self.prefix.count))),
              let box = try? AES.GCM.SealedBox(combined: data),
              let plain = try? AES.GCM.open(box, using: key), let text = String(data: plain, encoding: .utf8) else {
            throw SecretBoxError("\(place) cannot be decrypted with the memory key from the Keychain (a different key, or damaged)")
        }
        return text
    }

    /// The Keychain account of a doll's key: its data dir as an absolute path, symlinks resolved.
    public static func account(data: URL) -> String { data.resolvingSymlinksInPath().standardizedFileURL.path }

    /// The doll's box: her key from `store` under `account` (her data dir), made on first boot.
    /// `sealedPlace`: where encrypted secrets already are (nil: none). With encrypted secrets and no
    /// key, there is nothing to make: boot fails, no new key.
    public static func unlock(store: KeyStore, account: String, sealedPlace: String?) throws -> SecretBox {
        if let data = try store.load(account: account) {
            guard data.count == 32 else { throw SecretBoxError("the memory key for \(account) in the Keychain is \(data.count) bytes, not 32") }
            return SecretBox(key: SymmetricKey(data: data))
        }
        if let sealedPlace {
            throw SecretBoxError("encrypted secrets exist (\(sealedPlace)) but the memory key for \(account) is not in the Keychain; without it they cannot be read")
        }
        let key = SymmetricKey(size: .bits256)
        try store.save(key.withUnsafeBytes { Data($0) }, account: account)
        return SecretBox(key: key)
    }
}
