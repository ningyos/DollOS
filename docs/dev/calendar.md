# Calendar

Source: `Sources/DollCore/Calendar.swift`. Config: `[calendar]`, `[owner_sleep]` (and
`[maintenance] time` for the maintenance item). What the doll is told: `dolldocs/calendar.md`.

## Purpose

She has one calendar: future events and the day's timetable. It is hers, and the owner can edit it
from the dashboard. A timed item's start gives her a turn (a `calendar` event); she decides what to
do (remind the owner, start something, or nothing). All-day items and item ends never call her.

## Storage

- One file, `<data_dir>/calendar.json` (`CalendarFile`): pretty-printed, keys sorted,
  `fired_through`, `next_id`, `items`. It is not part of the timeline, so a new segment does not
  touch it.
- A missing file is an empty calendar. A file that does not decode or fails `Agenda.validate`
  stops the boot with `CalendarError` naming the item and the problem (strict, no repair).
- Written whole by `Agenda.save(box:)`. When a turn saw a secret, the file is boxed and stays boxed
  (`Doll.sealKey`, see [secrets.md](secrets.md)); a boxed calendar's 【行事曆】 events are stored
  sealed.
- Past items are kept; nothing prunes them.

## Items

`CalendarFile.Item`:

- `id` — integer shown as `#4`; from `next_id`, which only grows (ids are never reused).
- `title`, `date` (`YYYY-MM-DD`; a series' first day).
- `start`, `end` (`HH:MM`) — none: all-day; `start` only: a point in time; `end` requires `start`
  and must be after it (except the owner-sleep item, see below).
- `repeat` — `daily` or ISO weekdays `1,3,5` (1 = Monday, 7 = Sunday) (`Repeat`); `until` — last
  day, only with `repeat`.
- `exceptions` — map from the series day it would have happened to a `CalendarFile.Exception`:
  `skip`, or that day's `title` / `date` (moved) / `start` / `end` / `all_day`. A one-day change
  never edits the series.
- `by` — `doll`, `owner`, or `program` (role items).
- `role` — `maintenance` or `owner_sleep`; nil for ordinary items.

`Agenda.occurrences(from:to:)` expands items into `Occurrence`s (sorted by day, all-day first, then
start, then id). An occurrence of an exception has `changed = true` and keeps `original`.
`Occurrence.line` is the text she sees, e.g. `10:00–11:00 看牙醫（#4，每週三）`.

Times are local wall-clock time (`TimeZone.current`, via `Day.cal`). `Day` parses strict
`YYYY-MM-DD` real dates; `TimeOfDay` parses `HH:MM` (one-digit hours accepted). On a DST day a start
that does not exist is whatever Foundation picks.

## Firing: `fired_through` and the timer

`fired_through` (ISO 8601 with offset and milliseconds) means "every timed start at or before this
instant has been dealt with".

- `Doll.fireDue`: takes the starts in (`fired_through`, now] (`Agenda.starts(after:through:)`),
  moves `fired_through` to now and **saves first**, then handles each start:
  - `owner_sleep`: logged, no event, no turn;
  - `maintenance`: `Doll.maintenanceFire` (makes maintenance due; see
    [maintenance.md](maintenance.md));
  - otherwise: a queued `.calendar` event with `Doll.fireText`, e.g.
    `【行事曆】現在 10:00：看牙醫（#4，到 11:00）` ("calendar: it is now 10:00: dentist").
  Because the mark is saved before queueing, a restart never fires a start twice.
- `Doll.scheduleCalendar`: one `Task` sleeping until `Agenda.nextStart(after: fired_through)`, via
  `DollClock.sleep(until:)`. No polling. It is replaced after every edit and every wake; a serial
  (`calendarSerial`) makes a replaced task a no-op. It only runs once `calendarCaughtUp` is set.
- `DollClock` is the injectable "now": `SystemClock` uses the continuous clock, so a start that
  passed while the Mac slept fires once on wake. Tests use a manual clock via `Doll.setClock`.
- A `calendar` event queued while a turn runs waits like any event; its priority comes from
  `[core] priority` (the `calendar` entry is required there).
- During maintenance, fired events queue and give her a turn only after maintenance ends.

### Edits and the mark

`Doll.calendarEdit` (hers and the owner's) first calls `fireDue` (when caught up), then applies the
edit, saves, notifies the maintenance flow if the maintenance item changed
(`Doll.maintenanceItemChanged`), reschedules the timer, and notifies the dashboard feed. So a start
already past when an item is added never fires, and no start due at the moment of the edit is
skipped.

### Boot catch-up

`Doll.calendarCatchUp` (called from `Doll.announceBoot` via `Doll.calendarBootText`, or from
`Doll.run` if not yet done):

- Starts in (`fired_through`, now] that passed while `dollcore` was not running are marked dealt with,
  **not fired**. They are listed in the boot notice (`沒開機的時候過了這些（沒有叫你）`, "these
  passed while you were off (you were not called)"), at most `[calendar] status_items`. Owner-sleep
  starts are not listed.
- A missed maintenance start makes maintenance due.
- Then `Doll.ensureMaintenanceItem` and `Doll.ensureOwnerSleepItem` add the role items if missing.
- An event queued but not yet in the timeline when the process stopped is lost, like any queued
  event.

## Who may change what

`Agenda.apply(_:by:)` enforces:

- **Owner items are read-only for her.** If `by == "doll"` and the item's `by == "owner"`, change and
  delete are refused with a message telling her to ask the owner or add her own reminder. Reason: in
  real runs she deleted the owner's own wake-up item as a "duplicate" of her reminder without telling
  him.
- **Role items** (`maintenance`, `owner_sleep`) can be moved, for all days or one day, and one day
  can be skipped (`delete` with `on`). They cannot be deleted whole or made all-day
  (`Agenda.roleRefusal`). `Agenda.validate` also requires at most one item per role, a repeat and a
  start; `owner_sleep` also needs an end.
- `change` with `on` on a series writes an exception; `start` alone keeps the duration
  (`Agenda.newTimes`); `all_day` cannot be combined with `start`/`end`. `on` must be a day the series
  happens (`Agenda.checkOn`). `on` on a non-repeating item is refused with the reason.

Every refusal is a `CalendarError` in Traditional Chinese that names the problem and the fix.

## Role items

- **Maintenance** — 「系統維護」, `Agenda.maintenanceRole`, daily at `[maintenance] time` when
  missing. Its start makes maintenance due, with no turn of its own. See
  [maintenance.md](maintenance.md).
- **Owner sleep** — 「主人睡覺」, `Agenda.ownerSleepRole`, daily `[owner_sleep] start`–`end` when
  missing. An end at or before the start runs into the next day (`Occurrence.overnight`); equal
  times are refused. Its start and end never give a turn. Inside the span,
  `Doll.ownerSleepLine` adds "the owner should be asleep" to 【狀況】, wanderings and the
  maintenance notice (`Doll.withOwnerSleep`); if the owner spoke inside the span, the line names that
  time instead. It informs her; it blocks nothing (`discord.dm` still works).
- Items with roles `sleep` / `wake` (`Agenda.removedRoles`, from an older design) are accepted by
  validation only so that `ensureMaintenanceItem` can remove them at boot.

## Her tools

`CalendarRequest.names`: `calendar.add`, `calendar.change`, `calendar.delete`, `calendar.list`.
`CalendarRequest.parse` validates parameters as text (dates `YYYY-MM-DD`, times 24-hour `HH:MM`,
`id` with or without `#`). She must compute relative dates ("next Wednesday") herself from the date
and weekday in 【狀況】.

`Doll.calendarTool` returns the item as it now stands, with its weekday
(`Agenda.describe`), plus a note on when it will call her (`wakeNote`): all-day never calls; the
owner-sleep item never calls; a maintenance item gets "you will be told first and get a turn";
otherwise "you will get a turn then". `calendar.list` is limited to `[calendar] list_max_days`.

【狀況】 shows today's items via `Doll.calendarStatus` (all-day and not-yet-ended timed items, at
most `[calendar] status_items`, `（進行中）` for running ones).

## Dashboard edits

- `GET /api/calendar` → `Doll.dashboardCalendar` (`DashboardCalendar`: occurrences for
  `Doll.dashboardCalendarDays` days from today, and every stored item).
- `POST /api/calendar/add|change|delete?…` with the same parameters as her tools →
  `Doll.dashboardCalendarEdit` → `calendarEdit(by: "owner")`. A refusal returns HTTP 409 with the
  reason.
- Each owner edit writes a program event `【行事曆】主人在 dashboard …` ("the owner … on the
  dashboard") through `Doll.notice`: no turn of its own; if a turn runs it is held (`held`) and
  appended when the turn ends, so nothing lands between her reply and its tool results.

See [local-api-and-dashboard.md](local-api-and-dashboard.md).

## Invariants

- `fired_through` is saved before any start's event is queued.
- A start never fires twice across restarts; a start missed while down is listed, never fired.
- She cannot edit or delete an owner's item; nobody can delete a role item whole.
- At most one item per role.

## Config

`[calendar] status_items`, `[calendar] list_max_days` (both ≥ 1). `[owner_sleep] enabled`, `start`,
`end` (HH:MM, not equal). `[maintenance] enabled`, `time`. `[core] priority` must contain
`calendar`.

## Known limitations

- Starts are fired only on start; item ends do nothing.
- No import of external calendars.
- Past items are never pruned; `Agenda.occurrences` walks every item for every query.
- No DST handling beyond what Foundation does.

## Tests

`calendarDaysAndWeekdaysAreLocalAndStrict`,
`calendarDailyAndWeekdaySeriesWithOneDayChangesLeaveTheOtherDays`,
`missedStartsAreListedAtBootAndARestartNeverFiresAgain`, `aMalformedCalendarFileIsAnError`,
`sheCannotChangeOrDeleteWhatTheOwnerPutOnTheCalendar` (in `Tests/DollCoreTests/DollCoreTests.swift`).
