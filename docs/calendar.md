# Calendar

The doll has one calendar for future events and the daily timetable. It is hers; the owner can also edit it from the dashboard. A timed item's start gives her a turn, and she decides what to do (remind the owner, start something, or nothing). All-day items and item ends never call her.

## What the doll sees and does

Items:

- **All-day**: a date only (the owner's birthday). Never calls her; listed in 【狀況】 every turn that day.
- **Timed**: date and start, optional end. At the start she gets a turn with the program line 「【行事曆】現在 10:00：主人看牙醫（#4，到 11:00）」.
- **Repeating**: `daily`, or ISO weekdays `1,3,5` (1 = Monday … 7 = Sunday), from `date`, optionally `until` a last day.

Each item has a number (`#4`) used by change and delete.

Tools (dates `YYYY-MM-DD`, times 24-hour `HH:MM`):

- `calendar.add(title, date, start?, end?, repeat?, until?)`. She works out relative dates ("next Wednesday at ten") from the date and weekday in 【狀況】. The result states the weekday; if it is wrong she fixes it with `calendar.change`.
- `calendar.change(id, on?, title?, date?, start?, end?, all_day?)`: only the fields to change. `start` alone keeps the duration (10:00–11:00 → start 15:00 = 15:00–16:00). `all_day: true` cannot be combined with `start`/`end`.
- `calendar.delete(id, on?)`.
- `calendar.list(from, to?)`: every day from–to inclusive, at most `[calendar] list_max_days`. Past days stay listable.

On a repeating item, `on` (the day it would have happened) changes or skips only that day: `calendar.change(id: 2, on: "2026-11-20", start: "15:00")`, `calendar.change(id: 2, on: "2026-11-20", date: "2026-11-21")`, `calendar.delete(id: 2, on: "2026-11-20")`. Without `on` the whole series changes. `on` on a non-repeating item is refused.

Every result shows the item as it now stands and when it will call her (「到時候會給你一個回合」; all-day 「全天的事不會叫你…」; past 「時間已經過了，不會叫你。」).

**The owner's items are read-only for her**: 「#N「…」是主人自己加的，你不能改也不能刪。覺得該改就跟主人說，讓他自己改；你要另外提醒自己，就加一項你自己的。」 (She once deleted his wake-up item as a "duplicate" of her reminder.)

**Early reminders**: the program calls her only at a start. To remind the owner half an hour early, she adds her own earlier item ("提醒主人出門看牙醫" 09:30).

Where she sees it:

- 【狀況】: today's all-day items and not-yet-ended timed ones, running ones marked 「（進行中）」; beyond `[calendar] status_items` it adds 「還有 N 件（calendar.list 看全部）」.
- 【行事曆】現在 …: a start; it queues behind a busy turn like any event and never fires twice.
- 【開機】: today's calendar, and starts missed while dollcore was down, not fired: 「沒開機的時候過了這些（沒有叫你）：…」.
- 「【行事曆】主人在 dashboard <what>：…」: an owner edit; no turn of its own, seen with the next event.

### Role items

- **系統維護** (maintenance), daily, created at `[maintenance] time` if missing. At its time she is told first and gets a turn; maintenance starts a few minutes later when she is idle. Moving it before it starts cancels this run. Starts that fall during maintenance queue until it ends. Details: [maintenance.md](maintenance.md).
- **主人睡覺** (owner sleep), daily, created at `[owner_sleep] start`–`end` if missing. Its start and end never call her. Inside the span, 【狀況】, wanderings and the maintenance notice carry 「主人現在應該在睡（00:00–08:00）；不急的事等他醒了再說」; if the owner spoke inside the span it reads 「主人平常這時候在睡（…），不過他 01:20 還跟你說過話」. It informs, never blocks: non-urgent thoughts go to `concern.note`; urgent ones (a wake-up call he asked for) still go to him. An end not after the start runs into the next day (23:00–07:00); equal times are refused. To move one night, `on` is the day the span starts (for a 00:00 start, the day after midnight); moving a start from before to after midnight on one night also needs `date` set to the next day.

Both can be moved for all days or one day, and one day can be skipped (`delete` with `on`). Neither can be deleted whole or made all-day.

## How it works

`Sources/DollCore/Calendar.swift`.

- **Storage**: `<data_dir>/calendar.json` (`CalendarFile`: `fired_through`, `next_id`, `items`), pretty, sorted keys, written whole by `Agenda.save(box:)`. Not part of the timeline. Missing = empty; undecodable or failing `Agenda.validate` stops the boot with `CalendarError` (no repair). When a turn saw a secret the file is boxed and stays boxed (`Doll.sealKey`), and its 【行事曆】 events are stored sealed ([secrets.md](secrets.md)). Past items are never pruned.
- **Item** (`CalendarFile.Item`): `id` (from `next_id`, never reused), `title`, `date`, `start`, `end`, `repeat` (`Repeat`), `until`, `exceptions` (day → `CalendarFile.Exception`: `skip`, or that day's `title`/`date`/`start`/`end`/`all_day`), `by` (`doll`/`owner`/`program`), `role` (`maintenance`/`owner_sleep`/nil).
- **Expansion**: `Agenda.occurrences(from:to:)` → `Occurrence` sorted by day, all-day first, start, id; `Occurrence.line` is her text. Local wall time (`Day.cal`); `Day` and `TimeOfDay` parse strictly.
- **Firing**: `fired_through` means every start at or before it was dealt with. `Doll.fireDue` takes starts in (`fired_through`, now], moves the mark to now and **saves first**, then: owner sleep → log only; maintenance → `Doll.maintenanceFire`; else a `.calendar` event with `Doll.fireText`. `Doll.scheduleCalendar` keeps one `Task` sleeping until `Agenda.nextStart` via `DollClock.sleep(until:)`; replaced after every edit and wake, `calendarSerial` voids old ones; runs only after `calendarCaughtUp`. `SystemClock` uses the continuous clock, so a start passed during Mac sleep fires once on wake; tests use `Doll.setClock`. Event priority: `[core] priority` `calendar`.
- **Edits**: `Doll.calendarEdit` runs `fireDue`, applies (`Agenda.apply(_:by:)`), saves, calls `Doll.maintenanceItemChanged` if needed, reschedules and notifies the dashboard feed. A start already past at add time never fires; none due at the edit is skipped.
- **Rules in `Agenda.apply`**: owner items refused for `by == "doll"`; `Agenda.roleRefusal`; `Agenda.newTimes` keeps duration; `Agenda.checkOn` requires `on` to be a series day. `Agenda.validate`: at most one item per role, role items need repeat and start, owner sleep an end. Old roles `sleep`/`wake` (`Agenda.removedRoles`) validate only so boot can remove them.
- **Boot**: `Doll.calendarCatchUp` (via `Doll.calendarBootText`) marks missed starts dealt with and lists them (not owner sleep, at most `status_items`); a missed maintenance start makes maintenance due; then `Doll.ensureMaintenanceItem`, `Doll.ensureOwnerSleepItem`.
- **Prompt text**: `Doll.calendarTool` + `Agenda.describe` + `wakeNote`; `Doll.calendarStatus`; `Doll.ownerSleepLine`, `Doll.withOwnerSleep`.
- **Dashboard**: `GET /api/calendar` → `Doll.dashboardCalendar` (`Doll.dashboardCalendarDays` of occurrences plus all items); `POST /api/calendar/add|change|delete` → `Doll.dashboardCalendarEdit` → `calendarEdit(by: "owner")`, refusal = HTTP 409. The owner edit's event goes through `Doll.notice` (held during a turn). See [local-api-and-dashboard.md](local-api-and-dashboard.md).

## Config

- `[calendar] status_items` = 5: today's items in 【狀況】 and missed starts at boot.
- `[calendar] list_max_days` = 31: span of one `calendar.list`.
- `[owner_sleep] enabled` = true; `start` = "00:00"; `end` = "08:00" (must differ).
- `[maintenance] enabled`, `time`: [maintenance.md](maintenance.md). `[core] priority` must contain `calendar`.

## Invariants

- `fired_through` is saved before any start's event is queued; a start never fires twice across restarts.
- A start missed while dollcore was down is listed at boot, never fired.
- She cannot change or delete an owner item; nobody deletes a role item whole; at most one item per role.

## Known limitations

- Only starts fire; ends do nothing.
- No import of external calendars.
- Past items are never pruned, and every query walks every item.
- No DST handling beyond Foundation's.

## Tests

`calendarDaysAndWeekdaysAreLocalAndStrict`, `calendarDailyAndWeekdaySeriesWithOneDayChangesLeaveTheOtherDays`, `missedStartsAreListedAtBootAndARestartNeverFiresAgain`, `aMalformedCalendarFileIsAnError`, `sheCannotChangeOrDeleteWhatTheOwnerPutOnTheCalendar`, `aCalendarItemMadeInASecretTurnIsStoredBoxedAndFiresSealed`, `theOwnerSleepItemIsCreatedOnceMovableOverMidnightNotDeletableOrAllDay`, `anOldCalendarGetsTheOwnerSleepItemAtBootAndItsPassedStartIsNotListed`, `theStatusSaysHeShouldBeAsleepOnlyInsideTheSpanAlsoOverMidnight`, `theSpanGivesNoTurnAndTheNoticeAndWanderingInsideItCarryTheFactAndADMStillGoes`, `ownerSleepConfigIsStrict`.
