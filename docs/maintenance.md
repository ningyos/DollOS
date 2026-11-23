# System maintenance

Once a day the program tidies the doll's memory and writes the day's summary. It opens with her own turn to write her diary; after that she takes no turns until it ends, and she is told before, at the start and at the end. It is *system maintenance*, not sleep: there is no `sleep` tool and no sleepiness.

## What the doll sees and does

**The item.** Her calendar holds one daily 「系統維護」 (03:00 when created). She or the owner can move it with `calendar.change` (one day with `on`, e.g. `calendar.change(id: 編號, on: "2026-11-17", start: "04:00")`) or skip a day; it cannot be deleted or made all-day ([calendar.md](calendar.md)).

**Before.** At its time she gets a turn with 「【系統維護】時間到了，10 分鐘後開始：程式要從今天的對話記下掛著的事、整理你的記憶、寫今天的摘要，要幾分鐘，這段時間你不在。要收尾、要跟誰說一聲，現在做；今天這個時間不方便，就用 calendar.change 把今天的系統維護往後移。你手上沒事它就開始，不會再另外通知。」 (plus the owner-sleep sentence inside that span). Moving the item calls this maintenance off until the new time.

**Start.** It starts once she is not in a turn, nothing is queued or owed, and no todo list is **in progress** (進行中), in hand or aside. A **waiting** list (等待中) does not hold it; it crosses into the new day unchanged. An in-progress list holds it at most 3 hours; then it starts after her current turn and the list pauses.

Her diary turn comes first ([reflection.md](reflection.md)). Then 「【系統維護】日記的回合結束了，接著做，現在 HH:MM。」 or, without a diary turn, 「【系統維護】開始，現在 HH:MM。」, followed by 「程式要從今天的對話記下掛著的事、整理你的記憶、寫今天的摘要，要幾分鐘；這段時間你不在，結束了會告訴你。」 At the busy limit: 「你手上的「…」先停著，結束後接著做。」

**During**: no turns; events queue. **Tasks**, in order:
1. 從今天的對話記掛念 — concerns from the day ([drives-and-goals.md](drives-and-goals.md)).
2. 回顧今天、看想成為的樣子要不要改 — at most every 3 days ([drives-and-goals.md](drives-and-goals.md)).
3. 查對新記的記憶 ([memory.md](memory.md)).
4. 看你的短處今天有沒有又出現 ([reflection.md](reflection.md)).
5. 整理記憶、寫摘要 — the timeline becomes long-term memory and a new segment starts; the old conversation leaves her context and `recall` no longer finds its raw text ([memory.md](memory.md)).

Her own lines are marked 「｜自己的話」 for these tasks and are never a source of fact.

**End.** The new segment opens with 「【系統維護】結束，現在 HH:MM。」, each task's words, the day's summary (written by a separate request, not by her), 「今天的行事曆：…。」, and a carried list: 「你手上的「…」還在，還是等待中。」 (no turn) or 「你手上的「…」還在，接著做。」 (a turn). Otherwise the notice gives no turn. A failed task adds 「沒做成：…（程式出錯），明天維護的時候再做」 (and 「；之前的對話和做過的事都還在你眼前」 if consolidation failed).

**Interrupted.** An owner line (DM or voice) or developer line stops it; the cut-off task writes nothing. Before the line: 「【系統維護】被主人打斷了，今天的還沒整理完，之前的對話和做過的事都還在你眼前；沒人跟你說話 30 分鐘、你手上也沒事的時候，會再做一次。」 (「被開發者打斷了」; in the diary turn 「日記的回合先停下來（寫了的留著），」 is inserted and it ends 「，那時候可以接著寫日記。」). She answers directly. It runs again after 30 quiet minutes with nothing in hand, else at the next day's time.

**【狀況】**: 「- 今天的時間線：別人跟你說了 N 句，你回了 M 則」 and 「- 系統維護：<day> HH:MM（還有 …）」, or when due 「- 系統維護：時間到了，等你手上沒事就開始（這段時間你不在）」.

Context compaction is separate ([boot-and-logs.md](boot-and-logs.md)).

## How it works

`Maintenance.swift`; diary turn and `recur` in `Reflection.swift`.

**Due.** The item has role `maintenance` (`Agenda.maintenanceRole`, `Agenda.maintenanceTitle`; `Agenda.roleRefusal`). `Doll.ensureMaintenanceItem` adds it after the boot calendar catch-up and removes old `sleep`/`wake` items (`Agenda.removedRoles`). Its start calls `Doll.maintenanceFire`: sets `due`, and with `notice_min` > 0 sets `notBefore` and queues `Doll.maintenanceNotice` (`.calendar`, through `Doll.withOwnerSleep`). A start missed while down makes it due at boot (`Doll.calendarCatchUp`) without a notice. `Doll.maintenanceItemChanged` (from `Doll.calendarEdit`) resets a due, not-running maintenance.

**Start.** `Doll.maintenanceCheck` runs after every turn, on its timer and on state changes. It needs enabled, calendar caught up, not stopped, due, not running, past `retryAt()` (after an interruption: `lastSpoke + retry_idle_min`), past `notBefore`, and `Doll.maintenanceIdle` (`!inTurn`, empty queue, `!followUp`, `!todoBusy`; `todoBusy` = phase `.doing` in hand or aside). Past `Doll.busyLimitAt` it starts `forced` once no turn runs and the queue is empty. Otherwise one timer (`armMaintenanceTimer`, `maintenanceTimerSerial`). `Doll.startMaintenance` with nothing due (`Doll.diaryDue`, `maintenanceDue`) resets and logs `maintenance: nothing to do; not started`; `Doll.dayToConsolidate` needs a person's line or her reply in the segment. Diary first (`startDiaryTurn` → `finishDiaryTurn`), then `beginMaintenanceTasks` sets `running`.

**Tasks.** `MaintenanceTask` order is run order:

| Case | Due | Why here |
|---|---|---|
| `concerns` | `[drives] enabled`, `maintenance_concerns_max` > 0, `dayToConsolidate` | reads the day before it closes |
| `review` | `reviewDue` | same |
| `audit` | `auditDue` (`Audit.swift`) | corrections before consolidation recalls memories |
| `recur` | `recurDue` | reads the day before it closes |
| `consolidate` | `dayToConsolidate` | opens a new segment, so last |

`Doll.maintenancePrepare` (async, cancellable, writes nothing) → `MaintenanceWork`; `Doll.maintenanceCommit` (synchronous on the actor) → `MaintenanceDone`. `maintenanceCycle` then records `commitHistory(.maintenance, …)` ([history.md](history.md)), appends to `done`/`words`, and saves. A throwing task logs ERROR, records what it wrote, stores `failed`, and the cycle continues. New task: a case with `label`, arms in `maintenanceDue`, `maintenancePrepare`, `maintenanceCommit`, `maintenanceSeals`, and a `MaintenanceWork` case.

`Doll.maintenanceSeals`: inputs with secret or sealed content make the commit run under `Doll.sealing` (writes, log, history, stored words and end notice sealed; [secrets.md](secrets.md)). The consolidate commit keeps the held todo and aside, applies `Consolidation.apply` (`Timeline.startSegment`), replaces `timeline`/`today`, calls `Doll.startDay`, and moves late program notices to the new segment.

**End.** `Doll.waitForEvent` blocks while `running`. `Doll.endMaintenance` resets state, writes the notice, re-records carried lists at its seq (`Doll.recordTodo`), sets `followUp` only for an in-progress list, and calls `drivesCheck`.

**Interruption.** `Doll.interruptMaintenance(by:)` from `Doll.ownerSaid`, `Doll.developerSaid` and the secret-check `hold`: bumps `maintenanceSerial`, cancels the task and model request, clears `running`/`diary`/`forced`, sets `interrupted`, writes the notice before the line. A late prepare is discarded. Committed tasks stay done.

**Restart.** `<data_dir>/maintenance.json` (`MaintenanceFile`: `due`, `running`, `notBefore`, `interrupted`, `forced`, `done`, `failed`, `words`, `diary`, `diaryTurns`, `diaryFile`). `Doll.resumeMaintenance`: a diary turn restarts; `running` continues with tasks not done; else `maintenanceCheck`. Unreadable file fails boot. The boot report lists unfinished maintenance ([boot-and-logs.md](boot-and-logs.md)). Status: `Doll.maintenanceStatusLines`; dashboard/`dollctl status` show state `maintenance` and `Doll.maintenanceLine`.

The model is `Consolidation.model(cfg)`, sharing the single model-server slot with her turns without contention. `dollcore --config <file> consolidate <data_dir>` runs only consolidation on a stopped data dir.

## Config

`[maintenance]`:
- `enabled` = true — off: no item, no maintenance, no diary turn, no 【狀況】 lines.
- `time` = "03:00" — the item's time when created.
- `busy_limit_hours` = 3.0 (> 0) — how long an in-progress list may hold it.
- `retry_idle_min` = 30 (≥ 1) — quiet minutes before a retry.
- `notice_min` = 10 (≥ 0) — lead of the notice turn; 0 = none.

## Invariants

- A task writes only in `maintenanceCommit`.
- At most one maintenance runs; she gets no turn while `running`.
- Its writes are in history as `who: maintenance`; she cannot undo them.
- `consolidate` stays last.

## Known limitations

- A failed task waits for the next day's maintenance.
- Events during maintenance, calendar starts included, reach her only after the end notice.
- A retry or a start missed at boot has no advance notice.

## Tests

`maintenanceStartsAtItsTimeWhenIdleWithoutATurnAndEndsWithWhatItDidTheSummaryAndTheCalendar`, `sheIsToldBeforeItStartsGetsATurnAndMayMoveTodaysMaintenance`, `aListWaitingDoesNotHoldMaintenanceItCrossesIntoTheNewDayAndHisAnswerResolvesIt`, `aListInProgressStillHoldsMaintenanceUntilTheBusyLimitThenItIsKeptAndSheGetsATurn`, `theOwnersLineInterruptsItNothingIsWrittenSheAnswersAndItRetriesAfterQuiet`, `theDevelopersLineInterruptsItToo`, `aRestartDuringMaintenanceGoesOnWithTheTasksNotDone`, `anOldCalendarWithTheSleepItemsAndOldSignalsWithSleepinessAreReplacedAtBoot`.
