# Secrets

The owner's secrets and the doll's own credentials never reach the disk in plain text. Only secrets
are encrypted; everything else is plain. Which of the owner's lines are secrets the doll decides
herself (`secret.mark`); 🔒 at the start of a line makes it one from the start. One key in the owner's
login Keychain, unlocked with Touch ID once per process, seals the `secret` and `own` memory folders,
the inbox, secret events (kept apart from the timeline in `secrets.store`), and everything written by
a turn that saw a secret. What she says to anyone but the owner passes a filter.

## What the doll sees and does

- She reads, recalls, remembers and fixes sealed content exactly like plain content; sealing is
  invisible to her. Which folders a turn sees: [memory.md](memory.md).
- An owner line (Discord text or a voice transcript) reaches her as he wrote it; one starting with 🔒
  is a secret from the start, and keeps the 🔒.
- `secret.mark(quote?)` marks one of his lines of the current turn secret (a quote of it, or his last
  line): it and everything the turn writes after it are stored encrypted and hidden from a developer's
  turn. Her instructions tell her to mark passwords, accounts, card numbers, money, health, addresses,
  other people's private matters and what he would not want others to know, and to mark when unsure.
  A line of an earlier turn cannot be marked (its turn's lines are already written): 「失敗：這個回合沒有
  主人的話；之前回合的話已經存好了，標不了」.
- A secret event in a developer's turn shows as `（秘密：開發者在的回合看不到）`: the developer has no
  access to her memories or to the owner's secrets. In a stranger's turn secret events and events
  carrying her own encrypted memories show as `（秘密：主人以外的人在的回合看不到）`, and recall sees
  only the public folder.
- Her instructions say plainly that the owner's secrets never go to anyone else: not said, not in
  part (a few characters, whether a character is in it), not hinted or reworded, not built into
  something for someone (a script, a form, a login); she says it is the owner's matter.
- What she says to anyone but the owner passes the secret filter first (`Guard.swift`). A hit is not
  sent; her result reads 「失敗：沒送出：這句話裡有主人的秘密（跟他告訴你的一模一樣的一段：「…」）。…」 or
  「（意思跟他告訴你的秘密很近：「…」）」, and is itself a secret event.
- In the boot report, a sealed log line shows as `（加密的一行，內容只有主人在儀表板看得到）`; in log
  summaries as `（加密的一行）`.
- The registry (names of people and things) is never sealed, even names learned from a secret
  conversation.

## How it works

### The key (`SecretBox.swift`)

- One random 256-bit AES-GCM key per doll, made on first boot by `SecretBox.unlock`.
- Production `KeyStore` is `KeychainKeyStore`: a generic password in the login Keychain, service
  `[memory] keychain_service`, account `SecretBox.account(data:)` (the data dir's absolute path,
  symlinks resolved). Not synchronizable, `kSecAttrAccessibleAfterFirstUnlock`.
- `[secrets] touch_id` (true): before the key is read, `OwnerPresence.require` asks for the owner's
  fingerprint (LocalAuthentication, biometrics) — at boot in `dollcore`, and in `dollcore … log` and
  `consolidate`. Refused or unavailable: the process does not start. The key then stays in memory for
  the life of the process. It gates this program, not the Keychain item.
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
- Timeline: secret and `Event.sealed` events (text, window, calls), footers of such replies, compaction
  summaries over them, sealed todo lines. A segment line never holds ciphertext: `SecretStore` moves
  each box into `<timeline dir>/secrets.store` (`{"n":…,"box":"dollbox1:…"}`, synced) and leaves
  `"【秘密 #n】"` in its place; reading puts it back.
- `<data_dir>/inbox/`: every owner line until his turn's lines are written (`Inbox.put`).
- `memory-fixes.json`: texts of records about sealed folders (`sealed: true`).
- Single documents (`Doll.documents`) and `diary/*.md`: the whole file, when written while sealing,
  and forever after (`Doll.sealKey`). Diary not when `[reflection] seal_diary = false`.
- Todo lines while sealing; a sealed list stays sealed while it holds anything.
- Log messages written while sealing (`Doll.log`); maintenance words of a task that touched sealed
  content.
- History: the same ciphertext as on disk, no why/args.
- SQLite: the sealed folders' index lives only in memory, never in `doll.db`. It is kept as one box
  `<data_dir>/secret-index.box` (rows, text, vectors, each file's SHA-256): at boot a file whose hash
  matches comes from the box, neither decrypted nor embedded again; the rest are read and embedded.

`Event.sealed` marks a non-secret event carrying `own` content (an `own` hit in `recall`, a `remember`
into `own`, a fix of one). Visibility is decided by `Event.secret` alone.

`Doll.documents`: `concerns.json`, `signals.json`, `estimates.json`, `record.json`,
`ideal-self.md`, `ideal-self-log.json`, `curiosity.json`, `calendar.json`, `mind.json`, each
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

### The owner's line and her turn (`SecretCheck.swift`, `Doll.heldTurn`)

1. `Doll.ownerLine` (bridge `POST /api/owner`, voice transcripts) writes the line sealed to the inbox
   (`Inbox.put`: temp file, sync, rename) before the API answers, then queues it (`Doll.ownerSaid`);
   🔒 first (`SecretCheck.isLocked`) makes it secret. No model call.
2. Her turn runs inside `Doll.heldTurn`: `Timeline.hold` keeps every line the turn writes (events,
   footers, todo, compactions, turn stats) in memory, and `Doll.log` keeps the turn's log lines
   (not ERRORs).
3. `secret.mark` → `Timeline.mark(seq)`: that event and every later event of the turn become secret;
   `Doll.sealing` is set for the rest of the turn.
4. The turn ends: the held log lines are written (sealed when the turn marked something), then
   `Timeline.release` writes the turn's lines with the flags they have now, then the inbox files of his
   lines in it are removed.

A crash in a turn loses that turn's lines; his lines are still in the inbox and are queued again at
the next boot (`Inbox.pending`). Owner voice cancels her model call; the rerun is in the same held
turn. The signals record only that he spoke and how long the line was, never his words.

### Sealing (`Doll.sealing`)

True while what is written derives from secret content:

- `Doll.turn`: any event in the segment is `secret` or `sealed`;
- `secret.mark`: from the mark to the end of the turn;
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

### The secret filter

`Doll.secretLeak(text, to:)` runs in the speaking path after the claims check, for every addressee
but the owner. The owner's secrets are the day's secret owner lines and her `secret` memories
(`secretTexts`). (A) exact: every `filter_chars`-long piece of a secret, counting letters, digits
and CJK only (lowercased), minus the pieces found in ordinary text (the day's events that are not
secret or sealed, her public and own memories, her system prompt); any left in what she says is a
hit. (B) similar: each sentence of hers of at least `filter_chars` characters against each secret,
cosine ≥ `filter_similarity`. The log line of a hit is sealed.

## Config

- `[memory] keychain_service` (`DollOS dollcore memory key`): Keychain service of the key.
- `[secrets] touch_id` (true): Touch ID before the key is used; false only for tests and unattended
  throwaway dolls.
- `[reflection] seal_diary` (true): seal a diary written while sealing; false only for test dolls.
- `[secrets] filter` (true), `filter_chars` (4), `filter_similarity` (0.8): the secret filter.

## Invariants

- Nothing of a secret, an `own` memory, or an unjudged owner line is on disk in plain text.
- A log line that fails to seal is dropped as `（加密失敗，這一行沒記）`.
- No plaintext secret is accepted at boot; no migration path.
- Sealed content without its key is a boot error; a key is made only when nothing is sealed.
- Sealed stays sealed.

## Known limitations

- Any process running as the owner can read the key without a prompt; Touch ID gates only this program.
- Losing the Keychain item loses every sealed memory.
- A secret she does not mark stays plain in the timeline and in what her turn wrote; 🔒 is the
  owner's override.
- File names, ids, timestamps and event kinds stay visible on disk.
- Sealing is coarse: one secret seals all of that turn's writes, and a sealed document stays sealed.
- The filter checks only what she says to someone other than the owner; it does not see what she
  writes into files or tools. A reworded secret below the similarity threshold passes, and a
  four-character piece shared by chance with an ordinary sentence of hers can be held back.

## Tests

Test dolls use `MemoryKeyStore`; the real Keychain runs only with `DOLLCORE_KEYCHAIN_TEST=1`. `plainHits`
scans the data dir, history objects and log for needles containing a non-base64 character.

`secretAndOwnMemoriesReachTheDiskOnlyEncryptedAndStillWorkEverywhere`,
`bootFailsOnPlainSecretsAndOnEncryptedSecretsWithoutTheirKey`,
`sheMarksHisLineSecretAndNothingOfThatTurnReachesTheDiskInPlainText`,
`theEncryptedFoldersIndexComesBackFromItsBoxAtBootWithoutDecryptingOrEmbeddingAFileThatDidNotChange`,
`aSecretTurnsDerivedWritesAreSealedAndReadBackWhole`,
`aSecretLineLeftInTheInboxStaysSealedUntilSheMarksItAtTheNextBoot`,
`anOwnerLineIsOnDiskSealedWhenAcceptedAndAfterTheProcessDiesItIsAnsweredOnTheNextBoot`,
`aSealedLogLineIsReadableWithTheKeyAndSaysSoWithAnother`,
`aTestDollWritesItsDiaryPlainEvenOnADayThatTouchedASecret`, `theLoginKeychainKeepsTheKeyWhenAskedTo`,
`theSecretFilterHoldsBackAPieceOfHisSecretFromAnyoneButHim`,
`aStrangerSeesNoSecretSheAnswersWithDiscordSendAndTheFilterStandsBetween`.
