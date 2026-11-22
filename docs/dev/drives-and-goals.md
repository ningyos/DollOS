# Drives, concerns and goals

How the doll gets turns nobody asked for, and what she thinks about in them. Four parts:

- **Concerns** (掛念): concrete open gaps, each one sentence in her words (`Concerns.swift`).
- **Signals**: three simulated values that move on events and relax over time (`Signals.swift`, type `Drives`).
- **Wandering** (【走神】) and **association** (【聯想】): the two ways a concern reaches her (`Concerns.swift`, `extension Doll`).
- **Direction**: ideal self, goal tree, record of finished things, curiosity (`Goals.swift`).

Plus one maintenance task that notes concerns from the day (`ConcernExtract.swift`) and one that reviews the ideal self (`IdealReview` in `Goals.swift`). Maintenance order and scheduling: [maintenance.md](maintenance.md).

All of it is switched by `[drives] enabled`; `[goals]` has no own switch and follows it. Off means: no concern/goal/ideal tools, no signals, no wandering or association, no felt line. Runners and tests not about drives turn it off. Her own process keeps it on.

What the doll herself is told about these mechanisms lives in `dolldocs/drives.md` and `dolldocs/goals.md`; keep them consistent when you change behavior.

## Concerns

`Concern` (one item of `<data_dir>/concerns.json`, `ConcernFile { next_id, items }`, snake_case JSON):

| field | meaning |
|---|---|
| `id`, `text` | stable number; one sentence, her words (or the program's fixed wording) |
| `about` | `Concern.About`: `owner` / `duty` / `own` — decides which signal surfaces it |
| `source` | `her`, `calendar`, `todo`, `goal`, `maintenance` |
| `key` | dedupe key for program concerns (`calendar:<id>:<start>`, `todo:<startSeq>`, `goal:<step>`, `goalsub:<subgoal>`, `goalnone:<goal>`) |
| `due` | optional time; drives the `deadline` event |
| `surfaced`, `associated` | timestamps of each wandering / association |
| `held` | surfaced during owner sleep and not acted on (see below) |
| `near_raised` | its `deadline` already raised 壓力 |
| `ended`, `end` | `done`, `let_go`, `passed` |

`ConcernFile.nextId` must stay `nextId`, not `nextID`: the snake-case coders write `next_id` and read back `nextId`; `nextID` broke restarts.

Sources:

- **She** notes one: `concern.note(text, about, due?)` → `Doll.concernNote`. `due` is `YYYY-MM-DD HH:MM`. Refused at `[drives] max_open` open concerns (she must end some first). She ends one with `concern.end(id, done|let_go)` → `Doll.concernEnd`; `done` fires `SignalEvent.concernDone`.
- **Calendar**: every timed calendar occurrence without a role, starting within `[drives] calendar_concern_hours`, becomes a `duty` concern with `due` = start (`Doll.refreshProgramConcerns`). It ends `passed` when its time passes. Role items (maintenance, owner sleep) are skipped.
- **Failed todo item**: `todo.fail` adds a `duty` concern 「沒做成：<item>（<cause>）」, key `todo:<startSeq>`.
- **Goal tree**: `Doll.refreshGoalConcerns`, see [Goal tree](#goal-tree).
- **Maintenance**: `ConcernExtraction`, see [Concerns from the day](#concerns-from-the-day-maintenance).

Ended concerns are dropped from the file after `[drives] log_days` (pruned in `Doll.endConcern`). `Concerns.origin` renders where a concern came from in the line she sees (「#3，你 … 記的」, 「#4，系統維護 … 從那天的對話記的」 …) so she always knows whose words it is.

## Signals

`Signal`: `drive` (動力), `stress` (壓力), `closeness` (親近). Values are in 0…1. She never sees a number.

| signal | rest | half-life | out of range when | surfaces |
|---|---|---|---|---|
| drive | `drive_rest` 1.0 | `drive_half_life_h` 0.63 h | above `drive_high` 0.8 | an `own` concern |
| stress | `stress_rest` 0.1 | `stress_half_life_h` 4 h | above `stress_high` 0.5 | a `duty` concern |
| closeness | `closeness_rest` 0.0 | `closeness_half_life_h` 4 h | below `closeness_low` 0.2 | an `owner` concern |

(`Signal.outWhenLow` is true only for closeness. Numbers are the example config's; every one has its reason comment in `dollcore.example.toml`.)

**Math** (`Drives`, a pure struct with no actor; tests and the simulated week drive it on their own clock). Each signal stores only its value and time at the last change (`SignalState.points`). The value at `t` is

```
v(t) = rest + (v0 - rest) · 0.5^((t - t0) / halfLife)
```

(`Drives.value`). `Drives.move` adds a delta clamped to 0…1; `Drives.set` assigns. Both append a `SignalChange { ts, signal, before, after, cause }` when the value actually changed. The cause is words (「todo 完成：寄出季報」), so the dashboard and the log show why every move happened. The log is trimmed to `[drives] log_days`.

**Events** (`SignalEvent`, deltas from `DrivesConfig.deltas`, applied by `Doll.signal` which also saves, logs, notifies the dashboard and re-arms the timer):

| event | fired where | moves |
|---|---|---|
| `item_done` | a todo item done that is not a tree step (`Doll.itemFinished`) | drive +`item_done_drive` |
| `item_failed` | `todo.fail` | stress +`item_failed_stress` |
| `tool_errors` | the second tool failure in a row, and each after | stress +`tool_errors_stress` |
| `praise` | `owner.feedback(praise)` | drive +`praise_drive`, closeness +`praise_closeness`, stress `praise_stress` (negative) |
| `correction` | `owner.feedback(correction)` | stress +`correction_stress` |
| `owner_talk` | every owner line | closeness +`owner_talk_closeness` |
| `deadline` | a concern with `due` comes within `near_hours` (once, `near_raised`) | stress +`deadline_stress` |
| `concern_done` | `concern.end(done)` | drive +`concern_done_drive` |

Drive also moves outside `SignalEvent` through `Doll.moveDrive`: achievements and evaluations of the record (`[goals] step_drive` etc.), and novelty (`[goals] novelty_drive`).

`owner.feedback(kind, quote, record?)` (`Doll.ownerFeedback`) only counts when `quote` is found in one of the owner's lines of the current timeline. Her claim that he praised her is not enough.

**Felt line.** `Drives.felt` picks one fixed sentence from `Drives.feltTable` by the set of out-of-range signals (pairs before singles), e.g. 「你有點緊繃，也有點想主人」 ("you are a bit tense and miss the owner a little"). It goes into 【狀況】 (`Doll.feltLine`). No line when all are in range.

**Storage.** `<data_dir>/signals.json`: `points`, `surfaced`, `log`. Decoding is strict: an unknown signal, a value outside 0…1, or a missing signal fails the boot. One exception: the removed signal `sleepiness` (`SignalState.removed`) is dropped on read, `SignalState.droppedRemoved` makes the boot log say so, and the file is saved without it. A data dir without the file starts its signals at the first calendar catch-up (`Doll.signalsFresh`): drive at `drive_reset`, closeness at `closeness_start`, stress at rest (`Drives.init(config:now:)`). Until then `Doll.signal` ignores events.

## Wandering (【走神】)

`Doll.drivesCheck` runs after every turn (run loop), on the drives timer, at the end of maintenance and at start. It requires `[drives] enabled`, the calendar caught up, and not stopped. In order:

1. `Doll.refreshProgramConcerns`: goal concerns, calendar concerns, calendar ends, `deadline` events.
2. If idle (`Doll.idleForWander`): end an open own-thing episode (`Doll.endOwnEpisode`, below).
3. `Drives.pending`: a signal back in range forgets its `surfaced` mark (even when she is busy); returns the out-of-range, not-yet-surfaced signals in `[drives] order` (default stress, closeness, drive: duties are more urgent).
4. Not idle → stop. Idle → for each pending signal: mark it surfaced and save; `Concerns.pick` takes the open concern of the matching `about` surfaced least recently (never-surfaced first), then nearest `due`, then oldest. Found → `Doll.wander` queues 「【走神】你想起掛著的一件事（<origin>）：<text>」 and returns. None found → closeness queues `Concerns.wanderOwner`, drive queues `Concerns.wanderOwnNone`, stress only logs and the loop goes on to the next pending signal.
5. If nothing surfaced and curiosity is due: `Doll.curiosityWander`.

**Once per crossing.** `SignalState.surfaced` holds the signals whose current excursion already surfaced. It clears only when the signal is back in range. So one excursion gives one thought, however long it lasts.

**Idle** (`Doll.idleForWander`): not in a turn, not paused, no maintenance running, queue empty, no unanswered tool results (`followUp`), and the todo is idle/done, or waiting with nothing set aside. A list that only waits for someone's reply does not block her own things (see [turns-and-todo.md](turns-and-todo.md)).

**Queueing.** The wandering is an internal `.memory` event put on the queue. `[core] priority` lists `wander` last: owner, developer, calendar and watch events go first.

**One timer, no polling.** `Doll.scheduleDrives` keeps one task sleeping until the earliest of: `Drives.nextCrossing` (next time a signal leaves its range, or comes back while marked surfaced, solved in closed form: `t + log2(1/r) · halfLife` with `r = (edge − rest)/(v − rest)`, plus 1 s so `isOut` agrees on wake); `Doll.nextConcernChange` (a calendar item entering the window, a `deadline` coming near, a calendar concern passing); curiosity due. `Doll.drivesSerial` discards stale timers. Signals all in range and no concern change ahead → no timer, no model call.

**Owner asleep.** Inside the 「主人睡覺」 span ([calendar.md](calendar.md), `[owner_sleep]`) nothing is blocked: signals keep moving (closeness keeps decaying overnight), wanderings still give turns, `discord.dm` still works. Two effects:

- Every wandering text gets the `Doll.ownerSleepLine` sentence appended (`Doll.withOwnerSleep`): 「主人現在應該在睡（…）；不急的事等他醒了再說」 ("he should be asleep; non-urgent things can wait until he wakes"), or, if he spoke inside the span, the time of his last line instead.
- An `owner` concern surfacing in the span when he has not spoken since it began is **held**: `Concern.held` is set. If she messages him in that turn, `Doll.reachedOwner` clears it. Otherwise his next line brings it back once, at the start of that turn, as 「【聯想】主人在睡的時候你想起這件事，那時沒找他；現在他在了：…」 (`Doll.recallHeld`). Reason: a real-model run had her decide "wait until he's up" at night and then nothing reminded her at his "morning".

## The own-thing episode (drive)

Drive's rest (1.0) is above its high edge (0.8), so left alone it climbs and crosses on its own. From `drive_reset` 0.4 that takes `0.63 h · log2((1.0−0.4)/(1.0−0.8)) ≈ 1 h`. Earlier, rest sat below the edge; only praise and finished todos pushed it, and a 9-hour real run had zero wanderings while two goals stayed untouched.

Crossing starts an **episode**: `Doll.ownEpisodeStart` is set, an `own` concern (usually a goal-tree concern) surfaces with `Concerns.ownAsk` appended. `ownAsk` asks for something handed in: list a step with `todo.set` and finish it with `todo.done`; if it cannot be done now, record what is missing with `goal.blocked`; if she does nothing, write one sentence why in her thought. While a list waits, `Doll.waitingNote` explains how to start her own step beside it. `Concerns.isOwnWander` makes that turn a task turn (`Doll.taskTurn`, `[llm] task_turn_effort`): at effort none the first real run ended in 28 tokens without a call.

**End.** At the first idle moment after the wandering (whatever happened: did a step, did nothing, the owner pulled her away), `Doll.endOwnEpisode` calls `Drives.endEpisode`: drive is set back to `drive_reset`, the surfaced mark clears, and the climb restarts. The cause recorded in `signals.json` and the log is `Doll.episodeSummary`: her calls, how many messages went out, her first thought (or 「開始於重啟之前…」 after a restart, since `ownEpisodeStart` is not persisted). An episode that did nothing is therefore visible.

**No chaining.** Anything that raised drive during the episode (finished steps, todo items, a concern done) is overwritten by the reset. Finishing one own thing never immediately surfaces the next. An earlier version did chain (ending an own concern while drive was high surfaced the next one at once); it was removed. Outside episodes the raises still count: one praise between episodes (0.4→0.6) brings the next crossing about 22 minutes sooner.

The episode runs the same with the computer off; she then has only thinking, memory, calendar and the tree.

## Association (【聯想】)

`Doll.associate`, at the start of every turn. The query is the new owner/developer lines and tool results since her last reply, first `Concerns.associateChars` (2000) characters. Candidates are open concerns not already surfaced/associated since the owner's last line (`Doll.associatedIDs`, reset on a new speaker line) and not noted since the last turn began (`Doll.concernsNew`; otherwise her own `concern.note` result would bring it straight back). Concern vectors are embedded once and cached (`Doll.concernVectors`). The best cosine at or above `[drives] association_similarity` (0.42) is appended to this turn as 「【聯想】（想到）<text>（#id）」. No extra turn. The threshold was split from `[select] similarity` (0.55), which never fired in a real run; a 128-pair measurement (`Tests/DollCoreTests/AssocMeasure.swift`) put the highest should-not-attach pair at 0.398. Short greetings ("morning", "just got home") still fall under it; lowering further attaches wrong concerns.

## Concerns from the day (maintenance)

Maintenance task `MaintenanceTask.concerns` (runs first, before `consolidate` closes the day's timeline; skipped when `[drives] maintenance_concerns_max = 0` or nothing happened). Reason: in owner-sleep runs he mentioned a dentist visit in the evening and she never noted it, so nothing brought it back next morning.

`Doll.prepareConcernExtraction` sends one request (maintenance model, `[consolidate]` effort, no tools) with `ConcernExtraction.system` and the open concerns plus the day as `Consolidation.tasks` renders it. The reply is lines of exactly:

```
note <owner|duty|own> [due YYYY-MM-DD HH:MM]: <sentence> ｜根據：<words copied from the day>
end #<id>: <the owner's words showing it is resolved>
none
```

`ConcernExtraction.parse` enforces in code:

- **Form**: any other line throws and the task fails, writing nothing. The separator is the first colon not inside a time like `12:30`.
- **Basis (根據)**: required. It is split at 。！？ and each sentence is matched with `Doll.quoted` (the same matcher `todo.done` evidence uses: whole, or every piece split at ；/;/newline, ignoring `read_file` decoration). Every sentence must be found in the day; otherwise the note is dropped. Sentence-by-sentence matching exists because a real model copied three sentences from the boot notice joined by 。 and the whole string was not found.
- **Her words are not facts**: `ConcernExtraction.sources` splits the day into `others` (people's lines, indexed tool results, watch, calendar, the boot notice) and `own` (her thoughts and every call argument). A note supported only by `own` is rewritten 「她說過…（沒查證）」 ("she said …, unverified", `ConcernExtraction.unverified`), except `duty`, whose source is her own promise. Origin: she told the owner a wrong outage time, and this task recorded it as his concern as fact.
- **Duplicates**: equal to an open concern or an earlier note after stripping spaces, punctuation and symbols (`ConcernExtraction.key`) → dropped.
- **Cap**: at most `min(maintenance_concerns_max, max_open − open)`, in written order.
- **End**: only concerns with source `her` or `maintenance`, and only when the quote appears in one of the owner's lines of the day.

Everything dropped is logged (`maintenance concern left out: …`). `Doll.commitConcernExtraction` writes all of it in one save of `concerns.json` (restored in memory on failure). The end notice says what was noted and ended (`ConcernExtraction.doneText`).

## Goal tree

`<data_dir>/goals.json` (`GoalFile { next_id, nodes, link }`). Exactly three levels (`GoalNode.Level`): goal (a direction) → sub-goal (a finishable result) → step (one sitting). Tools: `goal.add(text, parent?)`, `goal.change`, `goal.end(done|drop)`, `goal.blocked(id, missing)`, `goal.list`. The owner edits via the dashboard ([local-api-and-dashboard.md](local-api-and-dashboard.md)); she is told by a no-turn notice (`Doll.toldOwnerEdit`).

Limits (`[goals]`): `goals_open_max`, `subgoals_open_max`, `steps_open_max`, `text_max_chars` (a node is a name, not a description).

Invariants (`Doll.goalAdd`, `Doll.goalEnd`, `Doll.linkTodo`):

- A step is done **only through a todo item**. `todo.set(items, subgoal)` ties the list to a sub-goal (`TodoLink`); an item naming `#<step id>` (`Doll.stepRef`) or equal to a step's text is that step; other items are added as new steps (all or none, within `steps_open_max`). `todo.done` on it ends the step and records it (`Doll.itemFinished`). She cannot `goal.end done` a step. Items were tied by number because real runs rephrased 3 of 3 steps as "what done looks like" and the original stayed open beside a duplicate.
- `done` on a goal/sub-goal needs nothing open under it. `drop` ends it and everything open under it.

**Program concerns** (`Doll.refreshGoalConcerns`, after every tree change and in each `drivesCheck`), all `about: own`, `source: goal`: per open sub-goal, its first open step (`goal:<step>`) or "no next step yet" (`goalsub:<subgoal>`); per goal with no sub-goal, "nothing under it yet" (`goalnone:<goal>`). One per sub-goal keeps the list bounded (3×3). A concern whose node no longer qualifies ends (`done` if the node was done, else `passed`); an open one's text follows the node's blocked note.

**Blocked** (`goal.blocked` → `Doll.goalBlockedTool`, `GoalNode.blocked { missing, ts }`): the formal "can't now, missing X" wherever the program asks her for a sub-goal, a step or a step done. Not a failure: no todo, no stress, not a tool error; the episode ends normally. `Doll.blockedNote` appends 「（你 <time> 記下現在做不了，缺：…）」 to the node's concern and `goal.list` line for `[goals] blocked_hours`. Cleared when the node's text changes, a node is added under it, or it ends. Reason: in a three-day soak with the computer off, asked for a sub-goal, she invented a script she had never seen and asked the owner about it five times. The program cannot cheaply check whether a free-text sub-goal names something real, so the prompt asks for things she has seen and `goal.blocked` gives the honest exit.

【狀況】 carries only the open goals' names (`Doll.goalStatusLine`); the full tree is on demand via `goal.list`, since putting it in every footer would accumulate in context.

## Record and evaluation

`<data_dir>/record.json`, append-only (`RecordEntry`): one entry per finished step, sub-goal, goal, and per finished todo list not tied to the tree (`kind` `list`, what the owner asked). Each carries the full drive amount (`step_drive`, `subgoal_drive`, `goal_drive`, `list_drive`) and `given`. On finishing, `now_share` of it is given at once (`Doll.achieve`). Evaluation (`Doll.evaluate`) moves drive to what now stands (`Doll.target`): good = all, bad = none (even the initial share is taken back). Her evaluation comes from `owner.feedback` (with `record`, else the newest entry within `feedback_hours`, `Doll.feedbackRecord`). The owner's dashboard evaluation overrides hers. Tree steps give `step_drive` instead of `item_done_drive`.

## Ideal self

`<data_dir>/ideal-self.md` (plain text) and `ideal-self-log.json` (`IdealLog`, every `IdealRevision`: by `pack`/`her`/`maintenance`/`owner`/`revert`, before, after, why, based_on). Without the file it starts from the pack's `[identity].ideal` (boot fails if the key is missing while drives are on; the pack is never modified). The system prompt carries it after the persona under `Doll.idealHeader` (「…跟上面的設定衝突時以上面為準」: the persona above wins on conflict), so a revision changes the prompt prefix from that point.

`ideal.revise(text | old+new, why, based_on)` → `Doll.reviseIdeal`, effective immediately. Checks enforced in code (`Doll.idealProblem`): non-empty, ≤ `[goals] ideal_max_chars`, and `Goals.nameProblem` (「我叫X」「我的名字是X」「名字叫X」 with X not a pack name, or 「我不是<name>」). Other identity and taboo rules are prose; they rely on prompt order and the owner seeing every revision. A second model review was rejected: the same small model judging itself is unreliable and costs a request.

**`IdealReview`** (maintenance task `MaintenanceTask.review`, after `concerns`, before `consolidate`): one request reads pack, ideal, tree, today's record and the last `[goals] review_chars` of the day, and answers `不改` or 「改成：…理由：…根據：…」. A revision is applied at once through the same checks; refused means nothing written and the notice says why. Due (`Doll.reviewDue`) only when `[goals] review` is on, the day had any conversation, and the last maintenance revision is at least `revise_min_days` old (her own revisions are not limited). Its instructions also say her own words are not a basis.

## Curiosity

`<data_dir>/curiosity.json` (`CuriosityFile`). After each computer action `Doll.noteSeen` keys the front app's bundle id and, for browsers in `Goals.browsers`, the front window title (`Goals.frontTitle`). A key unseen within `seen_days` is new: drive +`novelty_drive`, at most once per `novelty_gap_min`. Nothing new for `curiosity_hours` → `Doll.curiosityWander` queues `Doll.curiosityText` (look at something not on the tree; add it as a goal if interesting). `Doll.curiosityDue` returns nil with the computer off: apps and pages are the only source of "new", and she could not reach them.

## Storage and sealing

`concerns.json`, `signals.json`, `goals.json`, `record.json`, `ideal-self.md`, `ideal-self-log.json`, `curiosity.json` are single whole-file documents (`Doll.documents`). Malformed → boot error. Writes go through `Doll.saveDocument`, which seals the file (`SecretBox`) when written by a turn that saw a secret, when derived from sealed data, or when the file is already sealed; once sealed it stays sealed (`Doll.sealKey`). See [secrets.md](secrets.md). Revisions of the tree, record and ideal self are versioned ([history.md](history.md)); concerns and signals are program state and are not.

## Dashboard

`Doll.dashboardDrives` (`DashboardDrives`): current values and edges, felt line, a 24 h curve sampled every 15 min and reconstructed from the change log (`Doll.replay`), changes, open/ended concerns, the last 30 wanderings/associations, next crossing. `Doll.dashboardGoals` for the tree, record and ideal log.

## Known limitations

- Stress with no `duty` concern surfaces nothing (logged only); the excursion is still marked surfaced.
- Curiosity's "new" is coarse: a changing page title (unread counts) counts as new, page content changes do not, non-browser documents do not, and a fresh data dir finds everything new.
- The curiosity wandering does not carry `ownAsk`, so it is not a task turn and does not start an own-thing episode.
- A todo list she makes for her own thing without `subgoal` is recorded as a `list` entry as if the owner had asked.
- `ownEpisodeStart` is memory-only; an episode open across a restart ends with an unknown summary.
- The ideal self's identity and taboo rules beyond names are not checked by code.
- No reminder to the owner when a record entry stays unevaluated.

## Tests

`Tests/DollCoreTests/DollCoreTests.swift` covers these (search for `drives`, `wandering`, `Episode`, `maintenanceNotesConcerns`, `aConcernOnlyHerOwnWords`, `goal`, `IdealSelf`, `somethingNewRaisesDrive`). Long-run simulations: `M6DrivesWeek.swift`, `M6GoalsWeek.swift`; association threshold measurement: `AssocMeasure.swift`. See [testing.md](testing.md).
