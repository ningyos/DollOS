# Secrets

The owner's secrets and the doll's own credentials never reach the disk in plain text. One key in the
owner's login Keychain seals the `secret` and `own` memory folders, every owner line until it is judged,
secret events, and everything written by a turn that saw a secret. A model call decides whether each
owner line is a secret; 🔒 forces it.

## What the doll sees and does

- She reads, recalls, remembers and fixes sealed content exactly like plain content; sealing is
  invisible to her. Which folders a turn sees: [memory.md](memory.md).
- An owner line (Discord text or a voice transcript) starting with 🔒 is a secret without a check; the
  🔒 stays in the text she sees. Otherwise the line reaches her only after the check answers.
- A secret event in a developer's turn shows as `（秘密：開發者在的回合看不到）`.
- In the boot report, a sealed log line shows as `（加密的一行，內容只有主人在儀表板看得到）`; in log
  summaries as `（加密的一行）`.
- The registry (names of people and things) is never sealed, even names learned from a secret
  conversation.

## How it works

### The key (`SecretBox.swift`)

- One random 256-bit AES-GCM key per doll, made on first boot by `SecretBox.unlock`.
- Production `KeyStore` is `KeychainKeyStore`: a generic password in the login Keychain, service
  `[memory] keychain_service`, account `SecretBox.account(data:)` (the data dir's absolute path,
  symlinks resolved). Not synchronizable, `kSecAttrAccessibleAfterFirstUnlock`, no prompt: the doll
  boots unattended.
- No backup key: losing the Keychain item loses all sealed content.
- Threat model: the disk and its backups read without the owner's login. It does not protect against a
  program running as the owner.

### The box

`SecretBox` is the only place that seals and opens. Format: `dollbox1:` (`SecretBox.prefix`) plus
base64 of AES-GCM's combined box (12-byte nonce, ciphertext, 16-byte tag), UTF-8 text, no associated
data. `SecretBox.seal(_:)`, `SecretBox.open(_:place:)` (error names `place`), `SecretBox.isSealed(_:)`
(prefix check). A new format gets a new prefix. File names, ids, seqs, timestamps, kinds and flags stay
plain.

### What is sealed

- `[memory] secret_dir`, `own_dir`: every memory file, as one box (`Memory.sealedFolders`).
- Timeline: secret and `Event.sealed` events (text, window, calls).
- `<data_dir>/inbox/`: every owner line until its check answers (`Inbox.put`).
- `memory-fixes.json`: texts of records about sealed folders (`sealed: true`).
- Single documents (`Doll.documents`) and `diary/*.md`: the whole file, when written while sealing,
  and forever after (`Doll.sealKey`). Diary not when `[reflection] seal_diary = false`.
- Todo lines while sealing; a sealed list stays sealed while it holds anything.
- Log messages written while sealing (`Doll.log`); maintenance words of a task that touched sealed
  content.
- History: the same ciphertext as on disk, no why/args.
- SQLite: the sealed folders' index lives only in memory, never in `doll.db`.

`Event.sealed` marks a non-secret event carrying `own` content (an `own` hit in `recall`, a `remember`
into `own`, a fix of one). Visibility is decided by `Event.secret` alone.

`Doll.documents`: `concerns.json`, `signals.json`, `estimates.json`, `goals.json`, `record.json`,
`ideal-self.md`, `ideal-self-log.json`, `curiosity.json`, `calendar.json`, `self-knowledge.json`, each
sealed whole. `Doll.sealKey` seals when
`sealing`, or when `Concerns.boxed` says the file already is a box.

### Boot check (`Doll.unlockSecrets`)

Runs before the key is used: in `Doll` init, the offline `consolidate` CLI path and `Consolidate.swift`.
No fallback, no migration of old plaintext (the owner chose deletion over conversion):

- a plain `.md` in `secret`/`own` fails boot:
  `<path> is a plain-text memory file in an encrypted folder (<dir>); plain secrets are no longer allowed (no conversion: remove it)`;
- a secret timeline event stored plain (`Timeline.scan`) fails boot;
- sealed-folder index rows in `doll.db` (`Store.diskRows`) fail boot;
- sealed content (memory file, timeline event, boxed document or diary file) with no key fails boot,
  and no new key is made: it could never open the old content;
- a key that is not 32 bytes fails boot;
- content that does not open fails where it is read (`SecretBox.open`).

### The owner's line (`SecretCheck.swift`)

1. `Doll.ownerLine` (bridge `POST /api/owner`, voice transcripts) writes the line sealed to the inbox
   (`Inbox.put`: temp file, sync, rename) before the API answers.
2. `Doll.hold` queues the check behind earlier lines (`Doll.lineChecks`): arrival order is kept.
3. `Doll.checkAndQueue` calls `SecretCheck.ask`, then `Doll.ownerSaid(_:secret:…)`.
4. `Doll.drain` puts the event on the timeline and removes the inbox file.

`SecretCheck.ask`: first scalar 🔒 (`SecretCheck.lock`) → `.locked`, no request. Otherwise one request
to the main model: system `SecretCheck.instruction`, user the line, no tools, effort `none`.
`SecretCheck.read` accepts only `是` or `否` (spaces and one closing punctuation allowed). Anything else
is `.failed(reason)`, and `Verdict.isSecret` is true: network error, timeout (`[llm] timeout_s` on the
doll's clock), unreadable answer. The reason keeps only the error kind (≤ 60 characters); the log never
holds the line or the answer.

While a line waits, her turn continues; owner voice cancels her model call and the rerun waits for
held lines (`Doll.awaitHeldLines`); maintenance is interrupted. At boot, leftover inbox files are
checked again in order (`Inbox.pending`). Measured: secrets 30/30, borderline 9/10, ordinary 40/40,
about 1.1 s per check.

### Sealing (`Doll.sealing`)

True while what is written derives from secret content:

- `Doll.turn`: any event in the segment is `secret` or `sealed`;
- `Doll.append`: a secret or sealed event is appended mid-turn; every later event of that turn is
  marked `sealed`;
- maintenance, per task: `Doll.maintenanceSeals`;
- `Doll.sealedEdit` (`Goals.swift`): the owner edits a boxed document on the dashboard.

While sealing, documents, timeline events, todo lines and log messages are sealed and history commits
keep only `What`. A sealed file stays sealed: later writes may still carry the secret.

### Reading the sealed log

`dollcore --config <file> log` prints `<data_dir>/logs/` current log with sealed messages opened, for
the owner or a developer. It loads the key from the login Keychain (never makes one) and runs each line
through `RunLog.readable(_:box:)`: `<time> <level> <msg>`, a sealed message as `🔒 <text>`, one that does
not open as `（打不開：不是這把金鑰加密的）`; a non-JSON line is printed as is. The dashboard also opens
sealed lines for the owner.

### Throwaway dolls

Give a throwaway doll its own `keychain_service` and delete its item afterwards
(`security delete-generic-password -s <service> -a <data dir>`). Never delete the real doll's item.

## Config

- `[memory] keychain_service` (`DollOS dollcore memory key`): Keychain service of the key.
- `[llm] timeout_s` (300): deadline of the secret check.
- `[reflection] seal_diary` (true): seal a diary written while sealing; false only for test dolls.

## Invariants

- Nothing of a secret, an `own` memory, or an unjudged owner line is on disk in plain text.
- A failed check is a secret. A log line that fails to seal is dropped as `（加密失敗，這一行沒記）`.
- No plaintext secret is accepted at boot; no migration path.
- Sealed content without its key is a boot error; a key is made only when nothing is sealed.
- Sealed stays sealed.

## Known limitations

- Any process running as the owner can read the key without a prompt.
- Losing the Keychain item loses every sealed memory.
- The model can judge a borderline line ordinary; 🔒 is the owner's override.
- File names, ids, timestamps and event kinds stay visible on disk.
- Sealing is coarse: one secret seals all of that turn's writes, and a sealed document stays sealed.

## Tests

Test dolls use `MemoryKeyStore`; the real Keychain runs only with `DOLLCORE_KEYCHAIN_TEST=1`. `plainHits`
scans the data dir, history objects and log for needles containing a non-base64 character.

`secretAndOwnMemoriesReachTheDiskOnlyEncryptedAndStillWorkEverywhere`,
`bootFailsOnPlainSecretsAndOnEncryptedSecretsWithoutTheirKey`,
`theModelDecidesWhetherAnOwnerLineIsASecretALockForcesItAndAFailureMakesItOne`,
`aSecretTurnsDerivedWritesAreSealedAndReadBackWhole`,
`theSecretCheckWaitsForTheConnectionOnlyForItsTimeThenTheLineIsASecret`,
`aSecretLineLeftInTheInboxStaysSealedAndIsCheckedAgainAtBoot`,
`anOwnerLineIsOnDiskSealedWhenAcceptedAndAfterTheProcessDiesItIsAnsweredOnTheNextBoot`,
`aSealedLogLineIsReadableWithTheKeyAndSaysSoWithAnother`,
`aTestDollWritesItsDiaryPlainEvenOnADayThatTouchedASecret`, `theLoginKeychainKeepsTheKeyWhenAskedTo`.
