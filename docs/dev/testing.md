# Building and testing

How to build DollOS, run its tests, write a deterministic test, run the opt-in real-model
measurements, and run a throwaway doll without touching the owner's real one.

Related: [config.md](config.md), [secrets.md](secrets.md), [voice-and-bridge.md](voice-and-bridge.md).

## Toolchain

- macOS 26 or later (`platforms: [.macOS(.v26)]` in `Package.swift`).
- **Xcode 27 (Swift 6.4 or later).** Its default build system (Swift Build) compiles MLX's `.metal`
  kernels into `default.metallib` inside `mlx-swift_Cmlx.bundle`, next to the built products.
  Swift 6.2's native build system skips the kernels: the package builds, but MLX then has no
  GPU kernels for the embedding model or the voice. Swift 6.2's Swift Build preview does not link
  this package. CI enforces the version (the `Toolchain` step in
  `.github/workflows/ci.yml` fails below 6.4).
- **The Metal toolchain**, a separate component since Xcode 26. Install it once:
  `xcodebuild -downloadComponent MetalToolchain` (about 840 MB).
- Under `swift test`, MLX does not find the test bundle's `default.metallib` by itself;
  `MLXEmbedder.load` sets `GPU.metallib` to the copy in `mlx-swift_Cmlx.bundle` when it exists.

## Commands

```bash
swift build --scratch-path /tmp/m6build
swift test  --scratch-path /tmp/m6build                      # the whole suite
swift test  --scratch-path /tmp/m6build --filter <testName>  # one test
cd bridge && uv run --frozen pytest -q                       # the Python bridge
```

`--scratch-path /tmp/m6build` keeps the build outside the worktree; the measurement tests' header
comments use the same path. Plain `swift build` / `swift test` (as CI runs them) work too.

**Pitfall:** if `swift build/test --scratch-path /tmp/m6build` fails with "package manifest …
cannot be accessed", remove `/tmp/m6build/checkouts` and rerun (stale dependency checkouts in the
scratch dir).

The whole Swift suite takes roughly 11–19 s when nothing else loads the machine.

### What the suite needs on the machine

- The real embedding model (`[memory] embedding_model`, `Qwen/Qwen3-Embedding-0.6B`, about 1.2 GB)
  in the Hugging Face cache (`~/.cache/huggingface/hub`), or network to download it once. Only the
  tests about meaning use it (the private `qwen3` task in `DollCoreTests.swift`, loaded once per
  test process); all others use `FakeEmbedder`.
- `git` (the history repository, [history.md](history.md)).
- No model server, no Discord, no Keychain access, no GUI: the default suite touches none of them.

## CI

`.github/workflows/ci.yml`, on every push and pull request, on an `xcode-27` runner:

- job `swift`: prints and checks the toolchain (Swift ≥ 6.4), installs the Metal toolchain, caches
  `.build` and `~/.cache/huggingface` (keyed on `Package.resolved`), then `swift build` and
  `swift test`.
- job `bridge`: installs `opus` with Homebrew (for the real Opus round trip), then
  `uv run --frozen pytest -q` in `bridge/`.

## The Swift test harness (`Tests/DollCoreTests/DollCoreTests.swift`)

Almost every behavior test builds a real `Doll` with fakes around it:

| Piece | What it is |
|---|---|
| `Harness` | One doll in a fresh temp data dir (`dollcore-test-<UUID>`), built from `exampleConfig()` with maintenance, drives, owner sleep, goal review, audit and the estimate check switched off (each feature's own tests switch it back on through `tune:`). Exposes `said`, `saidTo`, `images`, `reports`, `logs`, `events()`, `toolResults()`, `turn(...)`, `waitFor(...)`. Pass `dir:` to reuse a data dir (a restart). |
| `exampleConfig()` | `dollcore.example.toml` with `[core] pack` set to the fixture `Tests/DollCoreTests/Fixtures/xiaodeng/doll.toml` (小燈). |
| `Scripted` / `FakeModel` | Scripted model replies: text content plus tool calls, returned the way the model server returns them when `tools` is sent (calls in `tool_calls`, every argument a JSON string). `FakeModel` records every request (messages, effort). Helpers build calls: `dm`, `reply`, `todoSet`, `done`, `fail`, `act`, `key`, `readCall`, `recall`, `remember`, `think`, … |
| `FakeCua` | A fake `ComputerDriver` (the cua-driver): records actions; screenshots are `frame <n> …` bytes; Calculator has a window from the start. |
| `FakeEmbedder` | Characters and adjacent pairs hashed into 512 dimensions, normalized: equal texts have cosine 1. |
| `testKeys` / `testBox(_:)` | One `MemoryKeyStore` for every test doll: memory keys live in process memory, never the login Keychain. |
| `ManualClock` | A `DollClock` whose time moves only by `set(_:)`; sleepers wake when the time passes their instant. Build times with `at("2026-11-16", "10:00")`. `goalsHarness(clock)` = a harness on that clock with drives on. |
| `Relay` | A fake model server behind the real `HTTPMainModel` (pass `relay:` to `Harness`): forwards to `FakeModel` while up, fails as configured while down. Used by the lost-connection tests. |
| `Recorder` | Collects deliveries, reports and log lines; `undelivered` simulates a failed delivery. |

`Harness.turn(_:)` queues replies and calls `Doll.runOnce()`: one turn, synchronously, so most
tests need no waiting at all. Tests that start background tasks (the API, the bridge, the
scheduler, the run loop) must wait for an observable result.

### Writing a deterministic test

1. Use `Harness` (or `goalsHarness`) and drive turns with `turn(...)`.
2. Control time with `ManualClock`, never with the wall clock: anything that reads "now" goes
   through `DollClock`. A clock-dependent string (e.g. 「0 秒」) read under load from the real clock
   once flaked.
3. Wait for an **event**, never for a fixed time: `waitUntil { … }` polls a condition every 10 ms;
   `Harness.waitFor(kind, count:)` waits until the queue holds that many events of a kind. Wait for
   the exact thing you will assert on (for example the turn's report, not just her words: the
   report exists only when the turn ends).
4. Leave the wait bound at `waitGuard` (60 s). It guards against a hang; it is not a speed claim.
   Running out throws `WaitTimeout`, which names the wait's source line.

Why: in the full parallel suite a test's wall time is set by its neighbours (one audit test took
0.41 s alone and 3.8–6 s in the suite). The old 3–5 s waits returned *as if* the condition held when
they ran out, and an unrelated-looking assertion failed later. All observed failures were "state
not reached yet", each in its own temp dir; no shared state or product race was found. After the
waits became 60 s guards that fail where they wait (and the history repository stopped spawning
most of its `git` processes, see [history.md](history.md)), 30 consecutive full runs passed, and 10
runs under extra CPU load passed.

Other rules the suite follows:

- Each test makes its own temp dir (`FileManager.default.temporaryDirectory` + UUID); never share
  data dirs between tests.
- Never use the login Keychain (use `testKeys`); never a real model, Discord or the owner's
  computer.
- Config tests write a temp TOML and `#expect(throws: TOMLError.self) { try Config.load(file) }`
  ([config.md](config.md)).
- Secret-at-rest tests use `plainHits(h, needles)`: it searches every file under the harness's data
  dir, every object of the history repository (`git cat-file --batch-all-objects`) and the log
  lines for plain-text needles. Each needle must contain a character base64 never writes, or it can
  match ciphertext by chance (it asserts this). `diskHits(dir, needles)` is the file-only variant.
  See [secrets.md](secrets.md).
- `TmpRepro` is `.disabled`: it reads an old `/tmp` timeline that no longer loads.

## Opt-in tests (environment-gated)

These do nothing unless their variable is set. Most talk to a real model server and read
configs and data from a git-ignored directory of private measurement data that is not part of
this repository; in a fresh clone they fail (a missing file) when switched on. Results are
written to the path the variable names.

| Variable(s) | Test(s) | Needs |
|---|---|---|
| `DOLL_VOICE_REAL=1` (+ `DOLL_VOICE_REF_AUDIO`, `DOLL_VOICE_REF_TEXT`, absolute paths to a reference WAV and its transcript) | `realVoiceThroughTheLoopback`, `realSpeechRecognizerFinalizesOnOurEndpoint` | real Qwen3-TTS, Apple SpeechTranscriber, the embedding model; DollOS ships no reference voice |
| `DOLL_VOICE_RECONNECT=1` | `realBridgeRecoversFromAViolationMidUtterance` | real TTS and a private bridge harness script |
| `DOLLCORE_KEYCHAIN_TEST=1` | `theLoginKeychainKeepsTheKeyWhenAskedTo` | the login Keychain; uses a throwaway service and deletes its item |
| `M6_WEEK=<out file>` | `m6DrivesWeek` | none (simulated week of the `[drives]` math, no model) |
| `M6_GOALS_WEEK=<out file>` | `m6GoalsWeek` | none (adds `[goals]`) |
| `M6_STEP0=<out dir>` (`M6_STEP0_RUNS`, `M6_STEP0_CASES`) | `m6DrivesStep0` | model server; `config.toml` in the out dir |
| `M8_AUDIT=1` / `M8_REAL=1` (`M8_DAYS`, `M8_REP`, `M8_OUT`, …) | `m8AuditMeasure`, `m8AuditRealDays` | model server; private config and recorded days |
| `M8_FIX=1` (`M8_SCEN`, `M8_OUT`) | `m8FixMeasure` | model server; private config |
| `M9_BOOT=<out dir>` (`M9_CASES`, `M9_WHEN`, `M9_RUNS`, `M9_PACK`, `M9_URL`, `M9_MODEL`) | `m9BootReport` | model server; computer is a refusing fake |
| `M10_HIST=1` (`M10_PART`, `M10_REPS`, `M10_OUT`) | `m10HistoryMeasure` | model server; private config |
| `M14_SECRET_LINES=1` (`M14_URL`, `M14_MODEL`, `M14_REPS`, `M14_OUT`) | `m14SecretLinesMeasure` | model server; private lines file |
| `ASSOC_MEASURE=1` | `assocMeasure` | embedding model; private config |
| `EST_MEASURE=1` (`EST_RECORDS`, `EST_OUT`) | `estimateMeasure` | embedding model; a records file |
| `OFFER_MEASURE=1` (`REPLAY_CONFIG`, `OFFER_OUT`) | `offerMeasure` | embedding model |
| `REPLAY_DIR`, `EXTRACT_DIR`, `OWNWORDS_CONFIG` (+ `REPLAY_CONFIG`) | `ReplayDump.swift` tests | model server; recorded data dirs |

Run one with its filter, for example
`M6_WEEK=/tmp/week.txt swift test --scratch-path /tmp/m6build --filter m6DrivesWeek`.
These runs take minutes to an hour; run them alone, not with the suite.

## Bridge tests (`bridge/tests/`)

`uv run --frozen pytest -q` in `bridge/`. `test_bridge.py` runs the bridge against a fake Discord
and a fake dollcore; `test_voice.py` covers the voice link and the sink against py-cord's real
pieces with no network. `test_real_opus_round_trip` is skipped when libopus is not at
`VoiceConfig().opus_library`.

## Running a throwaway doll

To try behavior against a real model without touching the owner's doll, give the throwaway one
everything of its own:

1. Copy `dollcore.example.toml` to a temp file and set:
   - `[core] data_dir` and `[memory] dir` to absolute paths under one new `/tmp/<name>` dir
     (relative paths resolve against the current directory, which is usually the real doll's);
   - `[core] pack` to an absolute pack path (the fixture pack is fine); `[core] docs` to the
     absolute `dolldocs` path;
   - `[api] port` and `[dashboard] port` to free ports, different from the real doll's (defaults
     8791 and 8790) and from each other;
   - `[memory] keychain_service` to a unique name, so the real doll's memory key is never read or
     overwritten;
   - `[computer] enabled = false`, `[voice] enabled = false`, `[maintenance] enabled = false`,
     `[bridge] enabled = false` (or a bridge with its own token and ports); `[drives] enabled = false`
     if the test is not about drives;
   - `[llm] base_url` / `[llm] model` to the model server. Remember the server may have one slot:
     the throwaway doll competes with any other user.
2. Run `swift run dollcore --config /tmp/<name>.toml` and talk to it with
   `swift run dollctl --config /tmp/<name>.toml chat` (it reads the port from the file).
3. Clean up: stop the process (Ctrl-C or SIGTERM), delete the `/tmp/<name>` dir, and delete its
   Keychain item. The item is a generic password with service `[memory] keychain_service` and
   account `SecretBox.account(data:)`: the data dir as an absolute path, symlinks resolved. List it
   first, then delete:

   ```bash
   security find-generic-password -s "<keychain_service>"            # shows the account ("acct")
   security delete-generic-password -s "<keychain_service>" -a "<account>"
   ```

   On macOS `/tmp` is a symlink to `/private/tmp`; take the account from the listing rather than
   typing it.

Never point a throwaway doll at the real data dir or reuse the default `keychain_service`: one key
per (service, data dir), and a lost key means its encrypted memories are lost
([secrets.md](secrets.md)).

## Known limitations

- The default suite cannot run without the embedding model in the Hugging Face cache or network.
- Opt-in measurement tests depend on private measurement data that is not in this repository.
- A few tests use real loopback HTTP and real child processes (`/bin/sh`, `curl`, `git`); they need
  free loopback ports and a normal macOS userland.
