# System maintenance

Source: `Sources/DollCore/Maintenance.swift`. Config: `[maintenance]`. What the doll is told:
`dolldocs/maintenance.md`.

## Purpose

Once a day the program tidies her memory and writes the day's summary. While it runs she takes no
turns ("she is not there"), and she is told plainly before, at the start and at the end. It is
framed as *system maintenance*, not sleep: there is no `sleep` tool, no sleepiness signal, no
"go to bed / wake up". Older data that still holds sleep items is migrated at boot (see
"Old data" below).

## When it comes due

- The trigger is a calendar item: one daily item 「系統維護」 ("system maintenance") with role
  `maintenance` (`Agenda.maintenanceRole`, title `Agenda.maintenanceTitle`, `by: program`). See
  [calendar.md](calendar.md).
- `Doll.ensureMaintenanceItem` creates it at `[maintenance] time` when the calendar has none. It runs
  once the calendar is caught up at boot (`Doll.calendarCatchUp`).
- The item can be moved (all days, or one day via `on`) and one day can be skipped, but it cannot be
  deleted or made all-day (`Agenda.roleRefusal`).
- Its start does **not** give her a turn. `Doll.maintenanceFire` sets `MaintenanceFile.due` and
  saves `maintenance.json`.
- A start missed while `dollcore` was down makes it due at boot (`Doll.calendarCatchUp`), with no
  extra notice; the boot notice's calendar part lists the missed start.

### The notice before the start

If `[maintenance] notice_min` > 0, `maintenanceFire` sets `MaintenanceFile.notBefore = now +
notice_min` and queues a `.calendar` event built by `Doll.maintenanceNotice` (prefixed
`【系統維護】時間到了，…`, "system maintenance: it is time, it starts in N minutes"). That event
gives her a turn to wrap up, tell someone, or move today's maintenance with `calendar.change`. The
text gets the owner-sleep line appended when the owner should be asleep (`Doll.withOwnerSleep`).

If the maintenance item is changed while maintenance is due but not running
(`Doll.maintenanceItemChanged`, called from `Doll.calendarEdit`), this maintenance is called off:
`maintenance.json` is reset and it comes due again at the item's next start.

## When it starts

`Doll.maintenanceCheck` runs after every turn (in `Doll.run`), on the maintenance timer, and on each
state change above. It starts maintenance when all hold:

- `[maintenance] enabled`, calendar caught up, not stopped, due, not running;
- past `retryAt()` (only after an interruption) and past `notBefore`;
- `Doll.maintenanceIdle`: no turn running (`inTurn`), queue empty, no tool result owed to her
  (`followUp`), and not `Doll.todoBusy`.

`Doll.todoBusy` is true only for a todo list **in progress** (`進行中`, `.doing`), in hand or set
aside. A list that is only **waiting** (`等待中`, `.waiting`) does not block maintenance. Reason: a
real run had a list waiting on the owner from the morning before; maintenance due at 03:00 only
started at the busy limit three hours later.

### Busy limit

If she stays busy, `Doll.busyLimitAt` = `due + [maintenance] busy_limit_hours`. Past it, maintenance
starts as soon as no turn runs and the queue is empty, even with a list in progress
(`startMaintenance(forced: true)`, `MaintenanceFile.forced`). The start notice then tells her the
list is paused.

When nothing time-dependent can change the outcome, `maintenanceCheck` arms one timer
(`armMaintenanceTimer`) for the earliest relevant instant (retry, notice end, busy limit). Timers use
`DollClock` and a serial (`maintenanceTimerSerial`) so a replaced timer is a no-op.

### Nothing to do

`Doll.startMaintenance` first checks whether any task is due (`Doll.maintenanceDue`). If none is, it
writes no notice, resets `maintenance.json`, and logs `maintenance: nothing to do; not started`.
`Doll.dayToConsolidate` is false when the segment holds no person's line and no reply of hers (boot
and maintenance notices alone are not a day).

## The tasks

`MaintenanceTask` cases run in declaration order (`CaseIterable`):

| Case | Due when (`maintenanceDue`) | Why this position |
|---|---|---|
| `concerns` | `[drives] enabled`, `[drives] maintenance_concerns_max` > 0, and `dayToConsolidate` | reads the day's timeline, which consolidation closes |
| `review` | `reviewDue`: `[drives] enabled`, `[goals] review`, `dayToConsolidate`, and the last maintenance revision at least `[goals] revise_min_days` ago | same |
| `audit` | `auditDue` (new memories checked against their sources, `Audit.swift`) | its corrections must be in place before consolidation recalls related memories |
| `consolidate` | `dayToConsolidate` | last: it opens a new timeline segment |

Details of each task belong to [memory.md](memory.md) (audit, consolidation) and
[drives-and-goals.md](drives-and-goals.md) (concerns, review).

### Prepare / commit

Each task is all-or-nothing:

- `Doll.maintenancePrepare` — async, cancellable, writes nothing. It returns a `MaintenanceWork`
  value (model answers, the consolidation request).
- `Doll.maintenanceCommit` — synchronous on the `Doll` actor, so no interruption can land between
  its writes. Returns a `MaintenanceDone` (log line, words for the end notice, optional summary, the
  todo carried over).

To add a task: add a `MaintenanceTask` case and its `label`, and arms in `maintenanceDue`,
`maintenancePrepare`, `maintenanceCommit` and `maintenanceSeals`, plus a `MaintenanceWork` case. The
flow does not change.

After each commit, `maintenanceCycle` records a history change (`commitHistory(.maintenance, …)`,
see [history.md](history.md)), appends the task to `MaintenanceFile.done`, appends its words to
`MaintenanceFile.words`, and saves `maintenance.json` before the next task starts.

A task that throws (not interrupted) logs an ERROR, records a history change for whatever it wrote
before failing, stores the error in `MaintenanceFile.failed`, and the cycle moves on to the next
task. The end notice says which tasks failed; the next day's maintenance tries again. There is no
retry the same night.

### Sealing

`Doll.maintenanceSeals` decides whether a task's inputs held secret or sealed content (secret or
sealed events in the day, boxed documents, encrypted memory folders). If so, the commit runs with
`Doll.sealing` set: everything it writes, logs and records in history is sealed, its words are kept
encrypted in `maintenance.json`, and the end notice is stored sealed. See [secrets.md](secrets.md).

### Consolidation and the segment switch

The `consolidate` commit (`maintenanceCommit`, case `.consolidate`):

1. Captures the todo she holds (`Doll.todoHeld`) and the list set aside.
2. `Consolidation.apply` writes memory and calls `Timeline.startSegment` to open a new segment.
3. Replaces `timeline` and `today` (a new `TodayIndex`), then `Doll.startDay` resets per-day state
   (todo, item steps, attachments, compaction, `followUp`, `taskTurn`, …).
4. Program notices appended after the request was gathered (for example the owner's dashboard
   calendar edits) are moved to the new segment.

The calendar, memory and the process are not touched.

## What she sees

- **Start** (`startMaintenance`, via `Doll.notice`): `【系統維護】開始，現在 HH:MM。…這段時間你不在，
  結束了會告訴你。` ("maintenance starts now; you are away meanwhile; you will be told when it
  ends"). With `forced`, one more sentence: the list in hand is paused and resumes afterwards.
- **During**: no turns. `Doll.waitForEvent` does not return while `MaintenanceFile.running` is set.
  Events (calendar starts, owner lines that do not interrupt, wanderings) keep queueing.
- **End** (`Doll.endMaintenance`): the first event of the new segment, `【系統維護】結束，現在
  HH:MM。` followed by each task's words, a list of failed tasks if any, the day's summary
  (`Consolidation.summaryNote` + the summary), today's calendar, and the carried list:
  - waiting: `…還在，還是等待中。` — no turn;
  - in progress: `…還在，接著做。` — `followUp = true`, she gets a turn.
  The carried lists are written into the new segment's todo record with `startSeq` = the end
  notice's seq (`Doll.recordTodo`).
- **【狀況】** (her status block) gets `Doll.maintenanceStatusLines`: the day's line/reply counts
  and the next maintenance time, or "due, starts when your hands are free".
- Dashboard and `dollctl status`: state `maintenance` and `Doll.maintenanceLine`.

## Interruption and retry

`Doll.interruptMaintenance(by:)` is called from `Doll.ownerSaid`, `Doll.developerSaid`, and the
secret-check hold path (`hold` in `SecretCheck.swift`). While running it:

- bumps `maintenanceSerial` and cancels the task (its model request is cancelled with it);
- clears `running`, sets `interrupted`, clears `forced`, saves `maintenance.json`;
- writes `【系統維護】被主人打斷了，…` ("maintenance was interrupted by the owner; …") **before** the
  line that interrupted, so that line gives her the turn.

A prepare that finishes after the serial changed is discarded (`… prepared after it was interrupted;
discarded, nothing written`). Tasks already committed stay done.

Retry: `Doll.retryAt` = last time anyone spoke (`lastSpoke`) + `[maintenance] retry_idle_min`. After
that, and when idle, it runs again without a new notice. If no quiet window comes, the state stays
due and the next day's start finds it still due. Reason: a short late-night conversation should not
leave the day unconsolidated, which would make the next day's timeline two days long.

## Restart

`<data_dir>/maintenance.json` (`MaintenanceFile`): `due`, `running`, `notBefore`, `interrupted`,
`forced`, `done`, `failed`, `words`. Written atomically, pretty-printed, keys sorted.

- `Doll.resumeMaintenance` (from `Doll.run`): if `running` was set at the restart, it goes on with
  the tasks not in `done` (the task cut off wrote nothing); otherwise `maintenanceCheck`.
- The boot report lists an unfinished maintenance among "unfinished" (see
  [boot-and-logs.md](boot-and-logs.md)).
- `words` survives the restart so the end notice still reports every task.
- An unreadable `maintenance.json` stops the boot (`Doll.readMaintenance` throws).

## Old data

`ensureMaintenanceItem` removes calendar items with roles in `Agenda.removedRoles` (`sleep`,
`wake`, from the earlier sleep design) and logs `calendar: removed the sleep items …`.

## Invariants

- A task's writes happen only in `maintenanceCommit`, synchronously; nothing is written by
  `maintenancePrepare`.
- At most one maintenance runs; `maintenanceFire` while running or due is a logged no-op.
- She never gets a turn while `running` is set.
- Maintenance writes are recorded in history as `who: maintenance` (she cannot undo them).
- The order of `MaintenanceTask.allCases` is the run order; consolidation must stay last.

## Config

`[maintenance]`: `enabled`, `time`, `busy_limit_hours`, `retry_idle_min`, `notice_min`. Validation
(`Config.load`): `time` is HH:MM, `busy_limit_hours` > 0, `retry_idle_min` ≥ 1, `notice_min` ≥ 0.
`enabled = false` means no item, no maintenance, no maintenance lines in 【狀況】. The model used is
`Doll.maintenanceModel` (`Consolidation.model(cfg)`), which shares the single model-server slot with
her turns; there is no contention because she takes no turns meanwhile.

The offline command `dollcore --config <file> consolidate <data_dir>` runs only the consolidation
for a stopped data dir (see [boot-and-logs.md](boot-and-logs.md)).

## Known limitations

- A failed task is retried only at the next day's maintenance.
- Events that arrive during maintenance wait; calendar starts that fire then give her a turn only
  after the end notice.
- Only the start notice before maintenance gives her a turn; a retry after interruption and a missed
  start at boot run without a notice.
- The header comment of `Maintenance.swift` and the `[maintenance] busy_limit_hours` comment in
  `dollcore.example.toml` still say a waiting list holds maintenance; the code (`todoBusy`) does not.

## Tests

`Tests/DollCoreTests/DollCoreTests.swift`:
`maintenanceStartsAtItsTimeWhenIdleWithoutATurnAndEndsWithWhatItDidTheSummaryAndTheCalendar`,
`sheIsToldBeforeItStartsGetsATurnAndMayMoveTodaysMaintenance`,
`aListWaitingDoesNotHoldMaintenanceItCrossesIntoTheNewDayAndHisAnswerResolvesIt`,
`aListInProgressStillHoldsMaintenanceUntilTheBusyLimitThenItIsKeptAndSheGetsATurn`,
`theOwnersLineInterruptsItNothingIsWrittenSheAnswersAndItRetriesAfterQuiet`,
`theDevelopersLineInterruptsItToo`, `aRestartDuringMaintenanceGoesOnWithTheTasksNotDone`,
`anOldCalendarWithTheSleepItemsAndOldSignalsWithSleepinessAreReplacedAtBoot`. See
[testing.md](testing.md).
