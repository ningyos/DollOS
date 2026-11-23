# AGENTS.md — working on DollOS

This file is for coding agents (and people) who change DollOS: fix a bug, add a feature, change a
behavior. Read it first; the manual per subsystem is in [docs/](docs/README.md).
The manual is one English set for developers **and for the doll herself**: with `[context] manual`
every `docs/*.md` is placed whole in her system prompt. Keep it true and short; a wrong sentence
there is a wrong belief of hers.

## What DollOS is

DollOS runs one character — "the doll", "she" — as a long-running Swift process (`dollcore`) on an
Apple-silicon Mac, for one person, "the owner". She keeps a timeline, memories, a calendar, goals
and moods, talks to the owner over Discord (text and voice) and to a developer over `dollctl`, and
can use the Mac through cua-driver. Her thinking is an OpenAI-compatible chat model with tool
calling on a separate server; everything else (memory embedding, TTS, ASR) runs in this process.

```
 owner (Discord DM / voice)      developer (dollctl chat)      calendar timer, wander timer
          │  bridge/ (Python)           │                              │
          ▼                             ▼                              ▼
   LocalAPI (127.0.0.1) ──────► event queue ([core] priority) ◄────────┘
   + /api/voice WebSocket              │  (inbox: owner lines are durable before "200")
                                       ▼
                       turn = one model request (system prompt + timeline + 【狀況】)
                                       │  tool calls (`tools` field)
                                       ▼
          tools: discord.dm, voice.say, dollctl.reply, recall, remember, memory.fix,
          todo.*, calendar.*, concern.*, goal.*, history.*, computer (act/read_window/…)
                                       │
                                       ▼
                    timeline (JSONL segments) ──► dashboard, dollctl status
                                       │
          daily maintenance (calendar item): concerns → review → audit → consolidate
          → new segment; drives/signals and goals produce 【走神】 turns when she is idle
```

Everything runs in one process around one actor, `Doll` (`Sources/DollCore/Doll.swift`). There is
one turn at a time; events queue while she is in a turn or in maintenance.

## Map

`Sources/DollCore/` (the library; almost all behavior):

| File | Owns |
|---|---|
| `Doll.swift` | The `Doll` actor: boot, event queue and `drain`, the turn loop (`turn`), tool dispatch (`execute`), todo rules, evidence and claims checks, `quoted`/`citable`. |
| `Tools.swift` | Tool names (`Tools.Name`), tool specs sent as `tools`, call decoding, gating, `Instructions` (her system prompt). |
| `Todo.swift`, `TodoAside.swift` | The todo list's states, plans and estimates; the one list she can set aside. |
| `Claims.swift` | Refuses a reply that says she did something (checked the calendar, wrote a memory) without a successful call since the last human line. |
| `Traditional.swift` | Converts Simplified characters in what she says to Traditional before it is sent. |
| `Models.swift` | The model client (`HTTPMainModel`), request/reply types, `Retry` (which failures are waited out). |
| `Store.swift` | `Event`, `Timeline` (JSONL segments), the SQLite index `doll.db`. |
| `Inbox.swift` | Sealed, fsynced owner lines kept until they reach the timeline. |
| `Memory.swift`, `Embedding.swift`, `Registry.swift`, `Select.swift` | Memory files and index, MLX embedding, the entity registry, choosing memories to attach/recall. |
| `MemoryFix.swift`, `Audit.swift`, `Consolidate.swift` | `memory.fix`; the maintenance audit; nightly consolidation into memory and the day summary. |
| `Concerns.swift`, `Signals.swift`, `ConcernExtract.swift` | Concerns, the three signals (`Drives`), wander/association, concerns extracted at maintenance. |
| `Goals.swift` | Goal tree, record, the ideal self and its review (`IdealReview`), curiosity. |
| `Maintenance.swift` | The daily system maintenance: due/idle, tasks (`MaintenanceTask`), interruption, restart. |
| `Calendar.swift` | Her calendar (`Agenda`), `DollClock`, the start-time timer, owner-sleep item. |
| `Computer.swift`, `OwnTools.swift`, `Files.swift` | The Mac via cua-driver (`Mac`, `CuaDaemon`); her saved tools; `read_file`/`write_file`/`edit_file`. |
| `SecretBox.swift`, `SecretCheck.swift` | AES-GCM sealing with a Keychain key; the owner's 🔒; `Guard.swift`: her secret mark and the secret filter. |
| `History.swift` | A git history of her own files (goals, ideal self, memories, tools), undo/restore. |
| `RunLog.swift`, `BootReport.swift` | The JSON log file, run endings, crash marks; the boot report she gets about the last run. |
| `LocalAPI.swift`, `WebSocket.swift`, `Bridge.swift` | Loopback HTTP API, RFC 6455 framing, launching/watching the Discord bridge. |
| `Voice.swift`, `VoiceEngines.swift` | Voice protocol, sentence splitting, endpointing, interruption; Qwen3-TTS (MLX) and Apple SpeechTranscriber. |
| `Dashboard.swift`, `DashboardPage.swift` | The dashboard HTTP server and its single HTML page. |
| `Config.swift`, `TOML.swift` | Strict config loading; the TOML subset parser. |

Other:

- `Sources/DollCLI/main.swift` — the `dollcore` executable: boot order, signals, `consolidate` subcommand.
- `Sources/DollCtl/main.swift` — `dollctl chat` / `dollctl status` against the local API.
- `bridge/` — Python Discord bridge (py-cord, `uv`); DMs and voice. Tests in `bridge/tests`.
- `docs/` — the manual (hers and yours; `docs/private/` is the owner's, never tracked). `character_packs/template` — the blank character.
- `dollcore.example.toml` — every config key with its value **and the reason for it**.
- `Tests/DollCoreTests/DollCoreTests.swift` — the deterministic suite; the other files there are
  env-gated real-model measurements. `Fixtures/xiaodeng` is the test character (小燈).

## The project's rules

Each rule is enforced in code, not only in a prompt. Keep them when you change things.

1. **No fallbacks.** A required part that fails stops the boot or ends the step with a visible
   error; nothing switches silently to a second path. Boot: `Doll.boot` (model check),
   `MLXEmbedder.load`, `CuaDaemon.ensure`, `Bridge.start`, the log file (`LogFile`); run time: the
   bridge is never restarted (`Bridge`), a failed tool returns 「失敗：…」 with the reason and what
   she can do now.
2. **Retry only connection-class failures.** `Retry.kind` names the `URLError` codes and HTTP
   502/503/504 that are waited out (`HTTPMainModel.send`, backoff `Retry.wait`,
   `[llm] retry_first_s`/`retry_max_s`). Every other failure (400, 500, unreadable reply,
   `finish_reason == "length"`) is an error.
3. **Every behavior number is in config, with a reason.** `dollcore.example.toml` has each key, its
   value and a comment saying why that value. Never hard-code a threshold; add a key (see
   [docs/config.md](docs/config.md)).
4. **Strict config.** Unknown section or key, wrong type or out-of-range value = boot error
   (`Config.load`, `Section.finish`). `[core] priority` must list every `Priority` exactly once.
5. **Her own words are never a source of facts.** Facts come from the owner's and others' lines,
   tool results, the calendar and program notices. Enforced by `Doll.citable` + `Doll.quoted`
   (`memory.fix` basis, plan facts, `todo.done` evidence), `Audit.sourceLines` (audit sources omit
   her lines), `Consolidation.ownWords`/`summaryOwnWords` (her lines marked 「自己的話」, written
   only as "she said…, unverified"), `ConcernExtraction.sources`/`unverified`.
6. **What is shown to her she often ignores; what the program requires her to hand in, she does.**
   So rules live in code as refusals with a reason, not only in the prompt: `Claims.refusal`
   (claimed actions need a call since the last human line, `Doll.doneSinceLine`),
   `Doll.evidenceRefusal` (`todo.done` evidence must be in the latest read), `Tools.gated` (computer
   tools only while an item is in progress), the decision point (`TodoState.deciding`),
   `Doll.estimateCheck`. A refusal always says why and what to do instead.
7. **Memories are replaced, never deleted.** `memory.fix` without new text is refused
   (`Doll.memoryFix`); `Memory.replace`/`Memory.retire` keep the old file; the audit only flags
   unless it has corrected text (`[audit] mode`). Retired files go only after
   `[consolidate] retired_keep_days` (`Memory.deleteRetired`).
8. **Secrets are sealed at rest; only secrets.** `secret` and `own` memory files, secret timeline
   events (their boxes in `secrets.store`, the segment keeps 【秘密 #n】), inbox lines, todo lines of a
   secret list, and everything a turn that saw a secret writes are AES-GCM sealed (`SecretBox`,
   `Memory.sealedFolders`, `Doll.sealing`, `Inbox.put`). She decides which of his lines are secrets
   (`secret.mark`); her turn's lines are written only when it ends (`Doll.heldTurn`). The key is in
   the login Keychain, used after Touch ID (`OwnerPresence`). Plaintext in a sealed place, or sealed
   data without its key, fails the boot (`Doll.unlockSecrets`); there is no migration.
9. **The owner's line is durable before it is acknowledged.** `Doll.ownerLine` writes it sealed and
   fsynced to `<data_dir>/inbox/` (`Inbox.put`) before the API answers 200; the file goes when her
   turn's lines are written (`Doll.heldTurn`); leftovers are re-queued at boot.
10. **Her prompt and tool list are pinned by hash.** The test
    `withTheComputerOnTheToolsAndPromptAreByteIdenticalToBeforeTheSwitch` (`enabledRequestDigests`)
    pins SHA-256 over every tools list and prompt variant. Changing a sentence of `Instructions` or
    a tool spec breaks it on purpose: re-pin deliberately and say why in the test's doc comment.
    The request prefix must stay byte-stable for the server's prompt cache (`Doll.tools` is built
    once; replies and footers are re-sent verbatim).
11. **Her texts are Traditional Chinese.** Prompt, tool results, refusals and notices are written in
    Traditional Chinese; what she says (`discord.dm`, `voice.say`, `dollctl.reply`, `ask_owner`) is
    converted by `Traditional.fix` before it is sent; typed text (`act`) is not touched
    (`Tools.Request.inTraditional`).
12. **History is never rewritten.** Her own files are committed to a git history
    (`Doll.commitHistory`); undo and restore are new commits (`History.undoWrites`,
    `History.restoreWrites`); she can undo only her own changes (`Doll.historyUndoTool`).
13. **Only the owner's channel speaks as the owner.** The local API listens on loopback only
    (`LocalAPI.isLoopback`); owner routes need the per-launch key that only the bridge receives
    (`LocalAPI.ownerKey`); the developer (`dollctl`) never sees secrets (`Audience.developer`).
14. **State files are strict.** Each subsystem owns its file under `<data_dir>` (`calendar.json`,
    `signals.json`, `concerns.json`, `maintenance.json`, …); an unreadable or invalid file fails the
    boot with the file named (`Agenda.validate`, `SignalState`, `Concerns.read`,
    `Doll.readMaintenance`) instead of being reset.

## How to work

**Toolchain.** macOS 26+, Apple silicon, Xcode 27 (Swift 6.4). Install the Metal toolchain once:
`xcodebuild -downloadComponent MetalToolchain`. Swift 6.2 builds this package without MLX's GPU
kernels (no `default.metallib`), and its Swift Build preview does not link it; CI checks for
Swift ≥ 6.4.

```bash
swift build --scratch-path /tmp/m6build
swift test  --scratch-path /tmp/m6build                  # whole suite
swift test  --scratch-path /tmp/m6build --filter <name>  # one test
(cd bridge && uv sync --frozen && uv run --frozen pytest -q)  # bridge tests
```

The suite loads the real embedding model (Qwen3-Embedding-0.6B, ~1.2 GB) from the Hugging Face
cache or downloads it once. Everything else is faked. Wrap long runs with a time limit.

**Reading her log.** `dollcore --config <file.toml> log` prints `<data_dir>/logs/dollcore.log` with
the lines of turns that saw a secret opened (they are sealed on disk; marked 🔒).

**Writing a test** ([docs/testing.md](docs/testing.md)): build a `Harness` (a real `Doll`
in a temp data dir with the fixture character, `Scripted` model replies via `FakeModel`, `FakeCua`
for cua-driver, `FakeEmbedder`, `testKeys` = an in-memory key store), drive turns with
`Harness.turn`, control time with `ManualClock` (every "now" goes through `DollClock`), and wait for
an observable event with `waitUntil`/`Harness.waitFor` — never a fixed sleep. Waits are bounded by
`waitGuard` (60 s) only to catch hangs. Reason: in the parallel suite a test's wall time is set by
its neighbours; short timed waits passed alone and failed in the suite.

**Secrets in tests.** Never use the login Keychain (use `testKeys`). To prove nothing leaks in plain
text, use `plainHits(h, needles)` (scans the data dir, the history repository's objects and the
log); each needle needs a non-base64 character.

**Real-model measurements.** Files like `M8AuditMeasure.swift` are env-gated (`M8_AUDIT=1`, …) and
do nothing by default. They need a model server and private data that is not in this repository;
see the table in [docs/testing.md](docs/testing.md). Run them alone.

**A throwaway doll** (to try a behavior against a real model): its own config with `[core] data_dir`
and `[memory] dir` under one `/tmp/<name>` dir (absolute paths), its own `[api] port` and
`[dashboard] port`, its own `[memory] keychain_service`, and `[computer]`, `[voice]`, `[bridge]`,
`[maintenance]` `enabled = false`. Talk to it with `swift run dollctl --config <file> chat`.
Clean up: stop it, delete the dir, and delete its Keychain item
(`security delete-generic-password -s <service> -a <account>`; the account is the data dir's
resolved absolute path — `/tmp` resolves to `/private/tmp`).

**Changing behavior.** Read the code comments at the place you change: decisions and their reasons
are written there. Keep the comment true when you change the code. If her tools or rules change,
update `Instructions` (re-pin the hash test) and the matching `docs/` file. A new config key goes into the struct, `Config.load`, and
`dollcore.example.toml` with its reason.

**Git.** Commit focused changes with a message that says why. Do not commit `dollcore.toml`,
`bridge/discord.toml`, `data/` or anything under `docs/private/`.

## Pitfalls learned the hard way

- **Blind substring replacement corrupts text.** A short name can sit inside an English word (a
  four-letter name inside "configuration"), and Chinese has no word boundaries. Edit by hand or
  match whole tokens; check the diff.
- **Base64 needles in disk scans.** A needle made only of base64 characters matches ciphertext by
  chance (about one run in four for a three-letter needle). `plainHits` refuses such needles.
- **Swift 6.2's native build skips MLX's `.metal` kernels** without failing the build. Use Xcode 27.
- **A stale scratch dir.** "package manifest … cannot be accessed" → remove
  `/tmp/m6build/checkouts` and rebuild.
- **Fixed sleeps in tests.** They flake under the parallel suite; wait for the event.
- **Wall-clock time in tests.** Strings like 「0 秒」 computed from the real clock flaked under load;
  use `ManualClock`.
- **One Discord bot.** There is one bot token per doll; never start a second bridge with the real
  `bridge/discord.toml` (keep `[bridge] enabled = false` on throwaway dolls).
- **The model server may have one slot.** Her turns and maintenance requests share it, and a request
  with another prompt start evicts her cached prompt; a throwaway doll or a measurement on the same server waits behind the real doll and slows it.
- **A waiting todo list is not "busy".** Maintenance once waited hours behind a list waiting for the
  owner. Use `Doll.todoBusy` (in-progress only) for "is she busy", `Doll.todoHeld` for "is there a
  list to carry over".
- **Changing a prompt sentence or tool description** breaks the hash pin; that is the point.
- **Relative paths resolve against the current directory**, not the config file's folder
  (`data_dir`, `pack`, `docs`, `[memory] dir`).
- **The Keychain account is the data dir path.** Moving or renaming a data dir, or deleting its
  Keychain item, makes her sealed data unreadable — and the boot then fails by design.
- **`[voice] enabled = false` with `[bridge] enabled = true`**: the bridge's voice link gets 503 at
  startup and the bridge exits.
- **The voice protocol is defined twice** (the header of `Voice.swift` and `bridge/src/dollbridge/voice.py`);
  change both together.
- **Memory index changes** (vectors, rows) need a bump of `Memory.indexVersion` or the old index stays.
- **The `watch` priority** still exists in `[core] priority` although no tool produces it; old
  config files must keep booting.
- **Developer lines are not durable** (they skip the inbox); only the owner's are.
- **The dashboard has no authentication** and shows decrypted secrets; it relies on
  `[dashboard] bind`. Keep it on loopback.

## Deeper docs

| File | Topic |
|---|---|
| [turns-and-todo.md](docs/turns-and-todo.md) | Events, queue, turns, the request, todo states, aside list, decision point, compaction, effort, claims, inbox, retries |
| [memory.md](docs/memory.md) | Memory files, index, embedding, registry, selection, strength, `memory.fix`, consolidation, audit |
| [tool-failures.md](docs/tool-failures.md) | What each common 「失敗：」 result means |
| [drives-and-goals.md](docs/drives-and-goals.md) | Concerns, the three signals, wander, the idle drive, association, goals, ideal self, curiosity |
| [reflection.md](docs/reflection.md) | Her diary turn, strengths and weaknesses with evidence, habits, the `recur` task |
| [maintenance.md](docs/maintenance.md) | The daily system maintenance |
| [calendar.md](docs/calendar.md) | Her calendar, the timer, owner sleep |
| [boot-and-logs.md](docs/boot-and-logs.md) | Boot order, the log file, run endings, the boot report |
| [computer.md](docs/computer.md) | The Mac via cua-driver, `act`/`read_window`, safety, honest completion, her own tools |
| [voice-and-bridge.md](docs/voice-and-bridge.md) | The Discord bridge, the voice WebSocket, TTS/ASR |
| [local-api-and-dashboard.md](docs/local-api-and-dashboard.md) | The local API, `dollctl`, the dashboard |
| [secrets.md](docs/secrets.md) | Sealing, the key, Touch ID, her secret mark, the filter |
| [history.md](docs/history.md) | The git history of her files, undo/restore |
| [config.md](docs/config.md) | Config loading, adding a key, the character pack |
| [testing.md](docs/testing.md) | Toolchain, harness, deterministic tests, opt-in measurements, throwaway dolls |

Also: [SETUP.md](SETUP.md) (installing for a user), [docs/README.md](docs/README.md) (the manual's index).
The design history (dated decisions and measurements) is kept privately; the reasons that matter
are in code comments and in `docs/`.
