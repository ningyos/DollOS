# Secrets at rest

Owner secrets and the doll's own credentials never reach the disk in plain text. This file covers the
key, the box format, what is sealed, the boot check, the per-line secret judgment of the owner's
words, the "sealing" flag for turns that saw a secret, and how tests check all of it.

Related: [memory.md](memory.md) (folders and visibility), [history.md](history.md) (sealed blobs in
the history), [turns-and-todo.md](turns-and-todo.md), [maintenance.md](maintenance.md),
[boot-and-logs.md](boot-and-logs.md), [voice-and-bridge.md](voice-and-bridge.md),
[config.md](config.md), [testing.md](testing.md).

## Mechanism

### The key (`SecretBox.swift`)

- One random 256-bit AES-GCM key per doll, created on first boot by `SecretBox.unlock`.
- Stored by a `KeyStore`. Production uses `KeychainKeyStore`: a generic password in the login
  Keychain.
  - service = `[memory] keychain_service` (default `DollOS dollcore memory key`, `MemoryConfig.keychainService`);
  - account = `SecretBox.account(data:)`: the data dir as an absolute path, symlinks resolved. Two
    dolls of the same pack have different data dirs, so different keys.
  - not synchronizable (local to this Mac; it is backed up with the login keychain);
    `kSecAttrAccessibleAfterFirstUnlock`; the decrypt ACL allows any application without a prompt.
- Symmetric only. There is no backup key: if the Keychain item is lost, the sealed content is lost.
- Threat model: the disk and its backups read without the owner's login. It does not protect
  against a program running as the owner.

Why no prompt: the doll runs in the background and is rebuilt often; a Keychain prompt nobody answers
would stop her boot.

Why a configurable service: a throwaway or test doll run against the real Keychain uses its own
service name so it never touches the real doll's key.

### The box format

`SecretBox` is the one place that encrypts and decrypts.

- Version 1: the text `dollbox1:` (`SecretBox.prefix`) followed by standard base64 of AES-GCM's
  combined box (12-byte random nonce, ciphertext, 16-byte tag). No associated data. Text is UTF-8
  before sealing.
- `SecretBox.seal(_:)` returns the boxed string; `SecretBox.open(_:place:)` opens it and names
  `place` in its error. `SecretBox.isSealed(_:)` is a prefix check.
- A future format gets a new prefix.
- File names and ids (`<seq>-<hex>.md`, event seqs, timestamps, kinds, flags) stay plain. They carry
  no content.

### What is sealed

| Where | What | Code |
|---|---|---|
| `[memory] secret_dir`, `[memory] own_dir` | every memory file, front matter and body, as one box | `Memory.isSealed`, `Memory.sealedFolders` |
| timeline | secret events and `Event.sealed` events: text, window and calls in one box | `Event.sealed`, `Timeline` |
| `<data_dir>/inbox/` | every owner line, before its secret check answers | `Inbox.put` |
| `memory-fixes.json` | before/after/basis/why of records about the two sealed folders (`sealed: true`) | `MemoryFixFile` |
| her single documents | whole file as one box when written while sealing, and forever after | `Doll.saveDocument`, `Doll.sealKey`, `Doll.documents` |
| todo lines | sealed while sealing; a sealed list stays sealed while it holds anything | `Doll.recordTodo` (`todoSealed`) |
| log lines | a line written while sealing is one box | `Doll.log` |
| maintenance words | `maintenance.json` words of a task that touched sealed content | `Maintenance.swift` |
| history | the same ciphertext as on disk; no why/args in the commit message | `History.commitLocked` |
| SQLite | the sealed folders' index lives only in memory; never in `doll.db` | `Memory.syncOnBoot` |

`Event.sealed` marks an event that is not a secret but carries content of the `own` folder (an `own`
memory in a recall result, a `remember` into `own`, a fix of one). Visibility is still decided by
`Event.secret` alone.

`Doll.documents` lists the single-document files: `concerns.json`, `signals.json`, `estimates.json`,
`goals.json`, `record.json`, `ideal-self.md`, `ideal-self-log.json`, `curiosity.json`,
`calendar.json`. They are sealed whole, not per item: they are small, written whole each time, and
items refer to each other by id. `Concerns.boxed(_:)` tells whether a file already is a box.

Log lines that are boxes show in the run log as `（加密的一行）` ("an encrypted line",
`RunLog.sealedKind`) to her; the dashboard opens them for the owner.

### Boot check (`Doll.unlockSecrets`)

Runs before the key is used, in `Doll` init, the offline `dollcore … consolidate` path
(`Sources/DollCLI/main.swift`) and `Consolidate.swift`. No fallback, no migration of old plaintext:

- a plain `.md` file in the secret or own folder fails the boot:
  `<path> is a plain-text memory file in an encrypted folder (<dir>); plain secrets are no longer allowed (no conversion: remove it)`;
- a secret timeline event stored in plain text (`Timeline.scan`) fails the boot;
- index rows of the sealed folders in `doll.db` fail the boot (`Memory.syncOnBoot`, via `Store.diskRows`);
- sealed content (memory file, timeline event, or a boxed document) with no key in the store fails
  the boot (`SecretBox.unlock` with `sealedPlace`). No new key is made in that case: a new key
  could never open the old content;
- a key that is not 32 bytes fails the boot;
- content that does not open with the key fails where it is read (`SecretBox.open`: "a different
  key, or damaged").

Why no conversion: the owner chose to delete old plaintext secrets rather than convert them.

### The owner's line: secret or not (`SecretCheck.swift`)

The model decides; the owner can force it.

1. `Doll.ownerLine` (the bridge's `POST /api/owner` and voice transcripts) first writes the line to
   the inbox, sealed (`Inbox.put`: temp file, sync, rename). Only then does the API answer. A process
   death after that cannot lose the line.
2. `Doll.hold` queues the check behind earlier lines (`Doll.lineChecks`), so lines are checked and
   queued in arrival order.
3. `Doll.checkAndQueue` calls `SecretCheck.ask`, then `Doll.ownerSaid(_:secret:…)`.
4. The event reaches the timeline in `Doll.drain`; the inbox file is removed then.

`SecretCheck.ask`:

- A line whose first scalar is 🔒 (`SecretCheck.lock`, U+1F512) is `.locked`: secret, no request.
  The 🔒 stays in the text she sees.
- Otherwise one small request to the main model: system = `SecretCheck.instruction` (Traditional
  Chinese filter prompt), user = the line, no tools, effort `none`.
- `SecretCheck.read` accepts only a clean `是` (yes) or `否` (no), with surrounding spaces and one
  closing punctuation mark allowed.
- Anything else is `.failed(reason)`, and `Verdict.isSecret` is true for it: a network error, a
  timeout, an unreadable answer. A failure never leaves plaintext.
- The deadline is `[llm] timeout_s` on the doll's clock. The model's own requests wait out a lost
  connection; the check waits only this long, then the line is a secret.
- The failure reason is the error's kind only (text before the first `:`, at most 60 characters):
  an error text could quote the request. The log never holds the line or the model's answer.

While a line waits:

- nothing of it is in plain text anywhere (the inbox copy is sealed);
- her turn in flight continues; on a one-slot model server the check waits behind it;
- owner voice cancels her model call at once (`inflight?.cancel()`); the rerun first waits for held
  lines (`Doll.awaitHeldLines`);
- maintenance is interrupted (`interruptMaintenance(by:)`).

At boot, inbox files left by the last process are checked again and queued in order (`Inbox.pending`
in `Doll.boot`).

A secret event is shown in developer turns as `Doll.hiddenSecret`
(`（秘密：開發者在的回合看不到）`, "secret: not visible in a developer's turn"). Folder visibility per
audience is `Memory.visible` (see [memory.md](memory.md)).

A real-model measurement (40 lines, twice each) found secrets 30/30, borderline 9/10, ordinary
40/40, median about 1.1 s per check. One extra model call per owner line is the accepted cost.

### The turn that saw a secret (`Doll.sealing`)

`Doll.sealing` is true while what is being written derives from secret content:

- `Doll.turn` sets it when any event in the segment is `secret` or `sealed`;
- `Doll.append` sets it mid-turn when a secret or sealed event is appended (e.g. her recall of a
  secret memory), and marks every event appended in a sealing turn `sealed`;
- maintenance sets it per task with `Doll.maintenanceSeals` (the day's events, an encrypted memory,
  a boxed document);
- `Doll.sealedEdit` (`Goals.swift`) sets it while the owner edits a boxed document on the dashboard.

While sealing: documents are written as boxes (`Doll.sealKey`), timeline events are sealed, todo lines
are sealed, log lines are boxes, history commits keep `What` but drop `Why` and `Args`
(`Doll.commitHistory`). A file once sealed stays sealed: later writes may still carry the secret,
and that cannot be proven otherwise.

Turns without secret content write plain text as before.

## Invariants

- Nothing of a secret, of an `own` memory, or of an owner line not yet judged is on disk in plain text.
- A failed check is a secret. A failed seal of a log line drops the line (`（加密失敗，這一行沒記）`).
- No plaintext secret is accepted at boot; there is no migration path.
- Sealed content without its key is a boot error; a new key is made only when nothing is sealed.
- Sealed stays sealed (`Doll.sealKey` checks `Concerns.boxed`).

## Tests

- Test dolls use `MemoryKeyStore` (in memory). `testKeys` in `Tests/DollCoreTests/DollCoreTests.swift`
  holds every test doll's key; `testBox(_:)` returns it.
- The real Keychain is exercised only with `DOLLCORE_KEYCHAIN_TEST=1`: the test creates a
  `KeychainKeyStore` with a throwaway service (`DollOS dollcore keychain test <uuid>`) and deletes
  the item with `KeychainKeyStore.delete(account:)` in a `defer`.
- `plainHits` (private helper in `DollCoreTests.swift`) scans every regular file under the harness
  data dir (including `doll.db-wal`), every git object of the history repository
  (`git cat-file --batch-all-objects --batch`), and the captured log lines for "needle" strings.
  Each needle must contain a character base64 never writes; otherwise it could match ciphertext by
  chance (a pure-base64 needle once matched about one run in four).
- Key tests: `secretAndOwnMemoriesReachTheDiskOnlyEncryptedAndStillWorkEverywhere`,
  `bootFailsOnPlainSecretsAndOnEncryptedSecretsWithoutTheirKey`,
  `theModelDecidesWhetherAnOwnerLineIsASecretALockForcesItAndAFailureMakesItOne`,
  `aSecretTurnsDerivedWritesAreSealedAndReadBackWhole`,
  `theSecretCheckWaitsForTheConnectionOnlyForItsTimeThenTheLineIsASecret`,
  `aSecretLineLeftInTheInboxStaysSealedAndIsCheckedAgainAtBoot`.

### Cleaning up a throwaway doll

A doll run against the real Keychain (not a test harness) leaves a login-Keychain item: service
`[memory] keychain_service`, account = its data dir's absolute path. Give a throwaway doll its own
`keychain_service` and delete that item when done (Keychain Access, or
`security delete-generic-password -s <service> -a <data dir>`). Never delete the real doll's item:
its secrets would be unreadable forever.

## Known limitations

- Any process running as the owner can read the key without a prompt.
- Losing the Keychain item loses every sealed memory; there is no recovery key.
- The secret judgment is a model call and can misjudge a borderline line as ordinary; 🔒 is the
  owner's override.
- File names, ids, timestamps and event kinds remain visible on disk.
- Sealing is coarse: one secret in a turn seals all of that turn's writes, and a document once
  sealed stays sealed.
