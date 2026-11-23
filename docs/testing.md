# Building and testing

How the developer builds DollOS, runs the tests, writes a deterministic test, runs the opt-in
measurements, and runs a throwaway doll without touching the owner's real one.

## How it works

### Toolchain

- macOS 26 or later (`platforms: [.macOS(.v26)]`).
- Xcode 27 (Swift 6.4 or later): only its build system compiles MLX's `.metal` kernels into
  `default.metallib`; without them the embedding model and voice have no GPU kernels.
- The Metal toolchain, installed once: `xcodebuild -downloadComponent MetalToolchain`.
- Under `swift test`, `MLXEmbedder.load` points `GPU.metallib` at the bundle's copy.

### Commands

```bash
swift build --scratch-path /tmp/m6build
swift test  --scratch-path /tmp/m6build                      # the whole suite
swift test  --scratch-path /tmp/m6build --filter <testName>  # one test
cd bridge && uv run --frozen pytest -q                       # the Python bridge
```

The scratch path keeps the build outside the worktree; plain `swift build` / `swift test` work too.
If the build says "package manifest … cannot be accessed", delete `/tmp/m6build/checkouts` and
rerun. The default suite needs the real embedding model (`Qwen/Qwen3-Embedding-0.6B`, ~1.2 GB) in
`~/.cache/huggingface/hub` or network to fetch it, and `git` (the history repository). It needs no
model server, Discord, Keychain or GUI.

**CI** (`.github/workflows/ci.yml`, every push and PR, `xcode-27` runner): job `swift` checks
Swift ≥ 6.4, installs the Metal toolchain, caches `.build/repositories` and `~/.cache/huggingface`
(never the build: its plan names a machine-specific Metal path), builds and tests. Job `bridge`
installs `opus` and runs pytest.

### The test harness (`Tests/DollCoreTests/DollCoreTests.swift`)

| Piece | What it is |
|---|---|
| `Harness` | One real `Doll` in a fresh temp dir (`dollcore-test-<UUID>`) from `exampleConfig()`, with maintenance, drives, owner sleep, goal review, audit, reflection and the estimate check off (`tune:` turns one back on). `dir:` reuses a data dir (a restart); `relay:` puts the real `HTTPMainModel` in front of the fake. Exposes `said`, `saidTo`, `images`, `reports`, `logs`, `turn(...)`, `waitFor(...)`, `timeline()`. |
| `exampleConfig()` | `dollcore.example.toml` with `[core] pack` = `Tests/DollCoreTests/Fixtures/xiaodeng/doll.toml`. |
| `Scripted` / `FakeModel` | Scripted replies (text plus tool calls, arguments as JSON strings, as the server returns them); `FakeModel` records every request. Call builders: `dm`, `reply`, `todoSet`, `done`, `fail`, `act`, `key`, `readCall`, `recall`, `remember`, `think`, … |
| `FakeCua` | Fake cua-driver: records actions; screenshots are `frame <n> …` bytes. |
| `FakeEmbedder` | Characters and pairs hashed into 512 normalized dimensions. `qwen3` loads the real model once per process for tests about meaning. |
| `testKeys` / `testBox(_:)` | In-memory key store; never the login Keychain. |
| `ManualClock` | Time moves only by `set(_:)`; `at("2026-11-16", "10:00")`; `goalsHarness(clock)` adds drives. |
| `Relay` | Fake model server for the lost-connection tests. |
| `Recorder` | Collects deliveries, reports and log lines; `undelivered` fakes a failed delivery. |

`Harness.turn(_:)` queues replies and calls `Doll.runOnce()`: one synchronous turn.

### Writing a deterministic test

1. Use `Harness` (or `goalsHarness`) and drive turns with `turn(...)`.
2. Control time with `ManualClock`; everything that reads "now" goes through `DollClock`.
3. Wait for the exact thing asserted, never a fixed time: `waitUntil { … }` (polls every 10 ms) or
   `Harness.waitFor(kind, count:)`.
4. Keep the bound at `waitGuard` (60 s): it guards against a hang and throws `WaitTimeout` naming
   the source line. Short waits that silently ran out caused every past flake, because neighbours in
   the parallel suite slow a test several-fold.
5. One temp dir per test; never the login Keychain, a real model, Discord or the owner's computer.
6. Config tests write a temp TOML and expect `TOMLError` ([config.md](config.md)).
7. Secret-at-rest tests use `plainHits(h, needles)` (every file in the data dir, every object of the
   history repository, the log lines) or `diskHits(dir, needles)` (files only). Each needle needs a
   character base64 never writes ([secrets.md](secrets.md)).

`TmpRepro` is `.disabled`: its old `/tmp` timeline no longer loads.

### Opt-in tests

They do nothing unless their variable is set. Most need a model server and private measurement data
that is not in the repository; results go to the path the variable names. Run each alone, e.g.
`M6_WEEK=/tmp/week.txt swift test --scratch-path /tmp/m6build --filter m6DrivesWeek`.

| Variable(s) | Test(s) | Needs |
|---|---|---|
| `DOLL_VOICE_REAL=1` + `DOLL_VOICE_REF_AUDIO`, `DOLL_VOICE_REF_TEXT` | `realVoiceThroughTheLoopback`, `realSpeechRecognizerFinalizesOnOurEndpoint` | real TTS, SpeechTranscriber, a reference WAV and transcript |
| `DOLL_VOICE_RECONNECT=1` | `realBridgeRecoversFromAViolationMidUtterance` | real TTS, private bridge script |
| `DOLLCORE_KEYCHAIN_TEST=1` | `theLoginKeychainKeepsTheKeyWhenAskedTo` | login Keychain (throwaway service, deleted after) |
| `M6_WEEK=<file>` / `M6_GOALS_WEEK=<file>` | `m6DrivesWeek` / `m6GoalsWeek` | nothing (simulated week) |
| `M6_STEP0=<dir>` (`M6_STEP0_RUNS`, `M6_STEP0_CASES`) | `m6DrivesStep0` | model server |
| `M8_AUDIT=1` / `M8_REAL=1` (`M8_DAYS`, `M8_REP`, `M8_OUT`, `M8_MAXSRC`) | `m8AuditMeasure`, `m8AuditRealDays` | model server, recorded days |
| `M8_FIX=1` (`M8_SCEN`, `M8_OUT`) | `m8FixMeasure` | model server |
| `M9_BOOT=<dir>` (`M9_CASES`, `M9_WHEN`, `M9_RUNS`, `M9_PACK`, `M9_URL`, `M9_MODEL`) | `m9BootReport` | model server |
| `M10_HIST=1` (`M10_PART`, `M10_REPS`, `M10_OUT`) | `m10HistoryMeasure` | model server |
| `M14_SECRET_LINES=1` (`M14_URL`, `M14_MODEL`, `M14_REPS`, `M14_OUT`) | `m14SecretLinesMeasure` | model server, lines file |
| `ASSOC_MEASURE=1` | `assocMeasure` | embedding model |
| `EST_MEASURE=1` (`EST_RECORDS`, `EST_OUT`) | `estimateMeasure` | embedding model, records file |
| `OFFER_MEASURE=1` (`REPLAY_CONFIG`, `OFFER_OUT`) | `offerMeasure` | embedding model |
| `REPLAY_DIR`, `EXTRACT_DIR` (`EXTRACT_N`), `OWNWORDS_CONFIG` (`OWNWORDS_CONCERNS_N`), `REPLAY_CONFIG` | `replayDumpRequests`, `replayConcernExtraction`, `replayOwnWordsCheck` | model server, recorded data dirs |

### Bridge tests (`bridge/tests/`)

`test_bridge.py` runs the bridge against a fake Discord and a fake dollcore; `test_voice.py` covers
the voice link and sink against py-cord's real pieces with no network. `test_real_opus_round_trip`
is skipped without libopus.

### A throwaway doll

To try behavior against a real model, give the throwaway doll everything of its own:

1. Copy `dollcore.example.toml` and set: `[core] data_dir` and `[memory] dir` to absolute paths
   under one new `/tmp/<name>` dir (relative paths resolve against the current directory);
   `[core] pack` and `[core] docs` to absolute paths; `[api] port` and `[dashboard] port` to free
   ports unlike the real doll's and each other; `[memory] keychain_service` to a unique name;
   `[computer]`, `[voice]`, `[maintenance]`, `[bridge]` `enabled = false` (and `[drives]` unless
   testing drives); `[llm] base_url` / `model` to the server (it may have one slot).
2. `swift run dollcore --config /tmp/<name>.toml`, then
   `swift run dollctl --config /tmp/<name>.toml chat`.
3. Stop it, delete `/tmp/<name>`, and delete its Keychain item (service `[memory] keychain_service`,
   account `SecretBox.account(data:)` = the data dir's absolute path with symlinks resolved, so
   `/private/tmp/…`). List it first, then delete:

   ```bash
   security find-generic-password -s "<keychain_service>"
   security delete-generic-password -s "<keychain_service>" -a "<account>"
   ```

Never point it at the real data dir or reuse the default `keychain_service`: a lost key loses its
encrypted memories ([secrets.md](secrets.md)).

## Known limitations

- The default suite needs the embedding model in the Hugging Face cache or network.
- Opt-in measurements depend on private data not in the repository.
- Some tests use real loopback HTTP and child processes (`/bin/sh`, `curl`, `git`).
