# Boot, the persistent log, and the boot report

Sources: `Sources/DollCLI/main.swift` (process lifecycle), `Sources/DollCore/RunLog.swift` (log
file, run endings, crash handler, reading the last run), `Sources/DollCore/BootReport.swift`
(`Doll.announceBoot`, boot report). Config: `[log]`, `[dashboard] boot_reports`. What the doll is
told: `dolldocs/boot-and-logs.md`.

## The `dollcore` process

`dollcore --config <file.toml>` boots her and runs until SIGINT or SIGTERM. It reads no stdin. The
owner starts and stops it; there is no launchd service.

`dollcore --config <file.toml> consolidate <data_dir> [--memory <dir>]` consolidates a data dir's
newest timeline segment offline and exits. Run it only while her process is stopped. It opens her
history first (see [history.md](history.md)) and records the consolidation as one maintenance
change. It does not attach the log file.

### Boot order (`main.swift`)

1. SIGINT/SIGTERM handlers installed first (dispatch sources), so Ctrl-C during boot is handled.
2. `Config.load` — strict; any error stops the boot (see [config.md](config.md)).
3. `RunLog.lastRun(dir: <data_dir>/logs)` — the previous run is read **before** this run writes
   anything.
4. `Log.attach(LogFile(…))` — opens `dollcore.log`, writes this run's start line, installs the
   fatal-signal handlers. A log that cannot be opened stops the boot: no run without its log, or the
   next boot would have nothing to report.
5. `CuaDaemon.boot(cfg.computer)` — her computer driver; missing driver or permission stops the boot
   unless `[computer] enabled = false` (see [computer.md](computer.md)).
6. `MLXProcess.configure` (one MLX cache limit), then `MLXEmbedder.load` (no fallback).
7. `LocalAPI` and `Doll` are constructed (the data dir is opened, secrets unlocked, `maintenance.json`
   read). `doll.boot()`: memory index sync, `model.check()` (an unreachable model server is a boot
   error), owner lines received by the last process but not in the timeline are checked and queued
   again.
8. Voice, if `[voice] enabled` (see [voice-and-bridge.md](voice-and-bridge.md)).
9. Local API, dashboard if `[dashboard] enabled`, bridge if `[bridge] enabled`
   (see [local-api-and-dashboard.md](local-api-and-dashboard.md)).
10. `doll.announceBoot(previous:build:)` — calendar catch-up, boot report, the 【開機】 notice.

Any error in steps 2–10 stops the daemon it started and calls `fail`, which logs an ERROR and an end
line `boot_failed`, then exits 1. After boot, `doll.run()` runs in a task; an error thrown from it
ends the process with `fatal`.

### Shutdown

On the first signal: stop bridge, API and dashboard; `doll.stop()` (cancels the model call in
flight); wait up to 5 s for the bridge; cancel and await the runner; `doll.endComputer()` (her
cursor session ends before the daemon); stop the daemon it started; `Log.end(.clean, "signal N
(…)")`.

## The persistent log (`RunLog.swift`)

Every log line goes to stderr as `YYYY-MM-DD HH:MM:SS LEVEL msg` (`Log.write`) and, once attached,
to `<data_dir>/logs/dollcore.log` as one JSON object per line (`LogEntry`): `level`, `msg`, `time`,
`ts`, plus `run` (`RunMark`) on lines that delimit runs. Newlines in a message stay inside the JSON
string.

- `Doll.log` seals the message when `Doll.sealing` is set or the caller passes `sealed`: the line is
  a `SecretBox` box on disk (see [secrets.md](secrets.md)).
- The bridge's stderr lines go to the file only (`Log.fileOnly`) with the level taken from its
  Python logging prefix (`ERROR`/`CRITICAL` → ERROR, `WARNING` → WARN, else INFO).

### Run marks

| Line | Written by | `run` |
|---|---|---|
| start | `Log.attach` | `{"event":"start","pid","build"}` |
| clean end | `Log.end(.clean, …)` after a handled SIGINT/SIGTERM | `{"event":"end","how":"clean","detail"}` |
| fatal / boot failed | `fail` → `Log.end` (an ERROR line first) | `how: "fatal"` / `"boot_failed"` |
| crash | `CrashMarks` handler | `{"event":"crash","detail":"SIGSEGV"}`, no `ts` |

`build` (`buildText` in `main.swift`) is the repo commit of the current directory (`-dirty` when
tracked files differ) plus the executable's path and modification time. The commit is the checkout's
now, not necessarily the build's; the modification time shows a rebuild.

### Crash handler

`CrashMarks.install` sets `sigaction` handlers for SIGSEGV, SIGBUS, SIGILL, SIGTRAP (Swift runtime
traps), SIGABRT and SIGFPE. Each handler only calls async-signal-safe functions: it `write`s a
preformatted JSON line (built at install time, hence no timestamp) to the log fd, restores
`SIG_DFL`, and re-raises. `CrashMarks.moved` keeps the fd correct after rotation.

### Endings (`RunEnding.How`)

`clean`, `fatal`, `boot_failed`, `crashed` (with the signal), `no_end`. `no_end` means the run has
a start but no end or crash line: SIGKILL, SIGHUP, an uncaught crash, shutdown or power loss — the
program cannot tell which. Its time is only the last line written; the process may have lived
longer. A Mac that sleeps does not end the process, so sleep never appears as an ending.

### Rotation and write failures

- When a write would pass `[log] max_mb`, `dollcore.log` is renamed `dollcore.1.log` (the older
  `dollcore.1.log` is deleted) and the new file's first line says so and how many bytes were deleted.
  At most two files exist. A failed rotation is reported on stderr and the file keeps growing.
- A failed write is reported once on stderr; the next successful write is preceded by an ERROR line
  saying how many lines are missing from the file.

## Reading the last run

`RunLog.lastRun(dir:)` reads `dollcore.1.log` then `dollcore.log` and returns a `LastRun` for the
lines after the last start mark (or all lines, `complete = false`, when the start was rotated out).
Lines that do not decode (a line cut by a crash) are counted as `unreadable`. It returns nil when
the folder has no log (an older data dir, or a first boot).

`RunLog.kinds` groups WARN and ERROR lines into `LogKind`s by level and `RunLog.kindKey`: hex runs of
8+ characters containing a digit become `#`, then numbers (with decimals) become `#`, then
whitespace runs collapse. Sealed lines form one kind (`RunLog.sealedKind`). Order: ERROR first, then
by count, then by first appearance. Each kind keeps `count`, `first`, `last`, and the first line
verbatim as `example`.

## The boot report (`BootReport.swift`)

`Doll.announceBoot(previous:build:)` runs once at the end of boot:

1. `Doll.calendarBootText` — calendar catch-up and today's items, plus starts missed while down (see
   [calendar.md](calendar.md)).
2. If there was a previous run, builds `BootReport`:
   - `unfinished` — only what the program knows for certain: a todo list held at an unclean end
     (`Doll.todoHeld`), and a maintenance that was running (it resumes; see
     [maintenance.md](maintenance.md)).
   - `why` — `error` (any ERROR line), `ending` (not `clean`), `warnings` (WARN lines ≥
     `[log] turn_warns`). `BootReport.turn` is `!why.isEmpty`.
   - `notice` — `Doll.reportText`, prefixed `【上次執行的紀錄】` ("record of the last run"): start
     time and line count, the ending in plain words, up to `[log] report_kinds` kinds with examples
     cut to `[log] example_chars`, how many were left out, the unfinished items, and the JSON path.
     It is built only when there is a reason, any WARN, or anything unfinished.
3. Her history state at boot (`historyBoot`): first commit, or changes found with no commit (the
   latter is added to her notice; see [history.md](history.md)).
4. Writes `<data_dir>/boot-reports/<YYYYMMDD-HHMMSS>-<pid>.json` (`Doll.bootReportName`) every boot,
   clean ones too. Snake_case keys, sorted: `boot`, `boot_time`, `pid`, `build`, `previous` (with
   every kind), `unfinished`, `why`, `notice`, `kinds_listed`, `kinds_left_out`, `history`. Optional
   fields that are nil (`previous`, `notice`, `history`) are omitted by the synthesized encoder.
   Old reports are never deleted. When the unfinished todo is sealed (`todoSealed`), that entry and
   the notice are stored as boxes.
5. Appends one program event: `【開機】你剛被開機了。` ("you were just booted") + the calendar part +
   the report notice + the history finding.
6. Turn: if `why` is non-empty it sets `followUp` and `taskTurn` (she gets one turn at
   `[llm] task_turn_effort`). Otherwise she reads the notice with her next event. This is not a
   queued event, so `[core] priority` has no entry for it.

Not repeated in the report: calendar starts missed while down (in the calendar part), and a failed
maintenance (it appears as an ERROR kind).

## Decisions

- **Report only, no repair.** She may tell the owner, note a concern, or ignore it. That she must not
  try to repair or claim a repair is a prompt rule (`dolldocs/boot-and-logs.md`), not a code rule;
  she has no tool that reruns maintenance or edits config.
- **No turn for a clean previous run.** A clean run with no WARN/ERROR adds nothing to the boot
  notice. A real-model measurement over 30 boots showed no turn and no model request for clean or
  single-WARN previous runs, and she never tried to repair or claimed a repair.
- **`no_end` says its time is only the last log line.** A real run reported "stopped around
  <last line time>" when the stop came much later; the notice now states the true time is unknown.

## Invariants

- The previous run is read before this run's start line is written.
- No run without a log file: failing to open it is `boot_failed`.
- Every exit path the process controls writes an end line: `fail` (`boot_failed`/`fatal`) or
  `Log.end(.clean, …)`.
- The report states only facts from the log; unfinished items are only those the program knows.

## Known limitations

- `fail` before `Log.attach` (usage errors, config errors, an unreadable last log) writes no line to
  the file; `Log.end` is a no-op then.
- `no_end` cannot tell kill, uncaught crash, shutdown and power loss apart, nor when the stop
  happened.
- An error thrown by `doll.run()` ends the process (`fatal`); nothing restarts it.
- The `consolidate` subcommand does not attach the log.
- Boot reports accumulate without pruning.

## Tests

In `Tests/DollCoreTests/DollCoreTests.swift`:
`theLogFileHoldsOneJSONLinePerEntryAndRunsAreDelimited`, `theLogRotatesAtItsCapAndSaysWhatWasDeleted`,
`eachEndingIsToldApart`, `linesAreGroupedByKindWithNumbersAndIdsMasked`,
`aCleanRunGivesNoTurnAndNoReport`, `aRunWithErrorsGivesATurnWithTheReport`,
`theReportIsCappedAndSaysHowManyKindsWereLeftOut`,
`anUncleanEndInATodoIsReportedOnceWithTheMissedCalendarApart`,
`aSecondRestartReportsTheShortRunBeforeItNotTheOlderOne`,
`anOldDataDirWithoutALogBootsAndSaysNothing`,
`theBootNoticeIsInTheTimelineAndTakesNoTurnOfItsOwn`. The real-model run is
`Tests/DollCoreTests/M9BootReport.swift` (`m9BootReport`, opt-in). See [testing.md](testing.md).
