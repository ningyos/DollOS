# Boot and logs

`dollcore` is the doll's process; the owner starts and stops it (no launchd service). Every boot ends
with one 【開機】 program event in her timeline, carrying a report on the previous run when its log
shows a problem or unfinished work. Every log line goes to stderr and to a JSON-lines log file.

## What the doll sees and does

**The boot notice**: `【開機】你剛被開機了。` ("you were just booted"), then today's calendar and timed
items that started while the process was down (no turns for those; see [calendar.md](calendar.md)),
then the report, then a history finding if any ([history.md](history.md)). Boot continues the newest
timeline segment, so the earlier conversation stays in her context.

**The report** starts `【上次執行的紀錄】程式從上一次執行的 log 整理的：` ("record of the last run, from
its log") and covers only the run right before this one:

- its boot time and line count;
- how it ended: `正常關掉的` (clean: SIGINT or SIGTERM), `程式出錯停下來的` (an error stopped it),
  `開機到一半失敗，沒有起來` (boot failed), `程式當掉了` (a crash signal; the time is the last line
  before it), or `沒有結束的紀錄` (no end line: killed, uncaught crash, shutdown or power loss, not
  told apart; the time is only the last log line and the stop came later; sleep never ends the
  process);
- WARN and ERROR lines grouped into kinds (same text once numbers and ids are masked), ERROR first,
  then by count, each with count, times and its first line as example; at most `[log] report_kinds`,
  then `還有 N 種沒列（共 M 行）`. A sealed line shows only as
  `（加密的一行，內容只有主人在儀表板看得到）`;
- `沒做完的` (unfinished), only what the program knows: her todo list at an unclean end, and a
  maintenance cut off halfway (it resumes now);
- the path of the full JSON report.

Missed calendar starts are not repeated; a failed maintenance shows as an ERROR kind.

**Turn.** She gets one turn when the previous run had any ERROR, ended uncleanly, or had at least
`[log] turn_warns` WARN lines. Otherwise the report waits for her next event. A clean run without
WARN or ERROR adds nothing; a data dir with no log says nothing.

**What she may do.** Tell the owner (her call when; small things wait while he sleeps), note a
concern, or ignore it. She may quote the report, but must not state a cause the log does not give.
She must not try to repair anything, rerun maintenance, change config or claim a fix.

**Files.** With the computer on, `read_file` (needs an item in progress) reads these, but they are
long and rarely needed; when something looks wrong she tells the developer what and when.

- `<data_dir>/timeline/<segment>.jsonl`: about one day per segment, append-only, synced per line.
  Record `type`s: `event`, `footer` (the 【狀況】 a reply answered), `todo`, `compact` (`done`,
  `failed`, `progress`, `forced`, `talk`; see [turns-and-todo.md](turns-and-todo.md)), `turn`
  (seconds, tokens). Every request is built from it; compacted events stay in it.
- `<data_dir>/shots/` (screenshots), `<data_dir>/computer-actions.jsonl` ([computer.md](computer.md)).
- `<data_dir>/logs/dollcore.log`, `<data_dir>/boot-reports/*.json`.

## How it works

**Commands** (`Sources/DollCLI/main.swift`): `dollcore --config <file.toml>` runs until SIGINT or
SIGTERM, no stdin. `… log` prints `dollcore.log` (not `dollcore.1.log`) with sealed lines opened and
marked 🔒 (`RunLog.readable`), using the login-Keychain memory key and never creating one.
`… consolidate <data_dir> [--memory <dir>]` consolidates the newest segment offline while her
process is stopped, records it in her history, and does not attach the log.

**Boot order:** signal sources; `Config.load`; `RunLog.lastRun` (before anything is written);
`Log.attach` (start line, crash handlers; an unopenable log stops the boot); `CuaDaemon.boot`;
`MLXProcess.configure`, `MLXEmbedder.load`; `LocalAPI`, `Doll`, `doll.boot()` (index sync,
`model.check()`, inbox lines re-queued, see [secrets.md](secrets.md)); voice; API, dashboard, bridge;
`doll.announceBoot(previous:build:)`. Any error calls `fail` (stderr, `Log.end(.bootFailed, …)`,
exit 1); an error from `doll.run()` calls `fail(…, .fatal)`. On a signal: stop bridge, API,
dashboard; `doll.stop()`; wait ≤ 5 s for the bridge; cancel the runner; `doll.endComputer()`; stop the
daemon; `Log.end(.clean, "signal N (…)")`.

**The log** (`RunLog.swift`). `Log.write` prints `YYYY-MM-DD HH:MM:SS LEVEL msg` to stderr and
appends a sorted-key `LogEntry` (`level`, `msg`, `time`, `ts`, optional `run`) to `dollcore.log`.
`Doll.log` seals the message while `Doll.sealing` or with `sealed`. Voice lines start `voice: `; the
bridge's stderr goes to the file only (`Log.fileOnly`, `bridge: `, ERROR/CRITICAL → ERROR, WARNING →
WARN). Each turn logs `turn N effort=… todo=… actions=[…] prompt=… prefilled=… cached=…`.

Run marks (level `RUN`, `RunMark`): `started: pid …, build …` (`Log.attach`); `ended
(clean|fatal|boot_failed): …` (`Log.end`, preceded by an ERROR line unless clean); `crashed: SIGSEGV`
(`CrashMarks`, no `ts`). `buildText` = short commit of the current directory (`-dirty` if tracked
files differ) + executable path and mtime. `CrashMarks.install` covers SIGSEGV, SIGBUS, SIGILL,
SIGTRAP, SIGABRT, SIGFPE: write a preformatted line, restore `SIG_DFL`, re-raise.

Rotation: past `[log] max_mb`, `dollcore.log` → `dollcore.1.log` (older one deleted), and the new
file's first line names the deleted size; a failed rotation keeps growing the file. A failed write
is reported once on stderr; the next good write is preceded by an ERROR counting missed lines.

`RunLog.lastRun` reads `dollcore.1.log` then `dollcore.log`, takes lines after the last start mark
(`complete = false` if rotated out), counts undecodable lines as `unreadable`, returns nil without
files. Ending: last end/crash mark, else `no_end` at the last timed line. `RunLog.kindKey`: hex runs
of 8+ with a digit → `#`, numbers → `#`, whitespace collapsed; sealed lines → `RunLog.sealedKind`.

**The boot report** (`BootReport.swift`, `Doll.announceBoot`): `Doll.calendarBootText`; with a
previous run, `unfinished` (`Doll.todoHeld` at an unclean end; `maintenanceState.running`), `why`
(`error`, `ending`, `warnings`) and `notice` (`Doll.reportText`, only when `why`, a WARN or
`unfinished` exists); `historyBoot` (`.found` goes into the notice, `.adopted` only into JSON and
log); writes `<data_dir>/boot-reports/<YYYYMMDD-HHMMSS>-<pid>.json` every boot (snake_case, nil
fields omitted, every kind kept; a sealed list's todo entry and the notice stored sealed, and the
event sealed); appends the event; a non-empty `why` sets `followUp` and `taskTurn` (effort
`[llm] task_turn_effort`). No queued event, so `[core] priority` has no entry.
`Doll.dashboardBoots(limit:)` opens sealed parts for the dashboard.

## Config

- `[log] max_mb` (10): rotation size; two files at most.
- `[log] report_kinds` (8): kinds listed in her notice.
- `[log] example_chars` (200): cut for examples and the ending detail.
- `[log] turn_warns` (5): WARN lines that give her a turn.
- `[dashboard] boot_reports` (10): reports the dashboard shows.

## Invariants

- The previous run is read before this run's start line.
- No run without a log file.
- Every exit the process controls writes an end line.
- The report states only log facts; no code acts on it.

## Known limitations

- `fail` before `Log.attach` writes nothing to the file.
- `no_end` cannot tell causes apart or when the stop came.
- Nothing restarts the process after `fatal`.
- `log` prints only the current file; boot reports are never pruned.

## Tests

`theLogFileHoldsOneJSONLinePerEntryAndRunsAreDelimited`, `theLogRotatesAtItsCapAndSaysWhatWasDeleted`,
`eachEndingIsToldApart`, `linesAreGroupedByKindWithNumbersAndIdsMasked`,
`aCleanRunGivesNoTurnAndNoReport`, `aRunWithErrorsGivesATurnWithTheReport`,
`theReportIsCappedAndSaysHowManyKindsWereLeftOut`,
`anUncleanEndInATodoIsReportedOnceWithTheMissedCalendarApart`,
`aSecondRestartReportsTheShortRunBeforeItNotTheOlderOne`,
`anOldDataDirWithoutALogBootsAndSaysNothing`, `theBootNoticeIsInTheTimelineAndTakesNoTurnOfItsOwn`,
`aSealedLogLineIsReadableWithTheKeyAndSaysSoWithAnother`, opt-in `m9BootReport`.
