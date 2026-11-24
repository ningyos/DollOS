# Drives, concerns and goals

How the doll gets turns nobody asked for, and what she thinks about in them. **Concerns** (掛念) are concrete open gaps in her words. Three simulated **signals** (動力, 壓力, 親近) move on events and relax over time; when one leaves its range while she is idle, a matching concern surfaces (【走神】, or 【系統任務】 for a thing of her graph). Concerns also attach to turns by similarity (【聯想】). Her **direction** is an ideal self, her graph (what she wants to do or be able to do, and what holds it up), a record of finished things, and curiosity.

**The system.** Program messages carry the label `系統` in her timeline. The system calls her 宿主, states facts and issues tasks (【系統任務】); it is not a person and needs no reply. She decides whether and how to do a task.

The owner pays for her power and machine; she helps him and may slowly learn to support herself. So, busy with her own thing, she may tell him 「等一下」 or 「現在不行」, with why and when. His lines still give her turns.

## What the doll sees and does

### Concerns

One sentence about a gap between how things are and how she expects or hopes (the owner unwell, a promise unkept, something near and unprepared, a result short, something she wants to do).

- `concern.note(text, about, due?)` — `about`: `owner`, `duty` (promised, due, failed), `own`. `due` as `YYYY-MM-DD HH:MM`; within 3 hours of it, 壓力 rises once. At most 20 open.
- `concern.end(id, how)` — `done` or `let_go`.

The program also notes concerns; the line she sees names the source:
- a timed calendar occurrence within 24 hours, role items excluded: 「#N，系統從行事曆記的」; ends when its time passes;
- a failed todo item: 「沒做成：<item>（<cause>）」, 「#N，系統在 todo 失敗時記的，…」; she ends it;
- her graph: 「#N，系統從你的圖記的」;
- maintenance: 「#N，系統維護 <time> 從那天的對話記的」;
- hers: 「#N，你 <time> 記的」.

**From the day.** Each maintenance notes up to 3 concerns from the day: the owner's matters (he saw the dentist → ask later if it still hurts), her unkept promises, her unfinished things. Each needs a basis quoted from the day; one backed only by her own words is written 「她說過…（沒查證）」 (except a duty). It ends a concern she or maintenance noted only when the owner's words in the day show it resolved. End notice: 「從今天的對話記了 N 件掛著的事：…」, 「結束了 N 件：…（主人說「…」）」 or 「從今天的對話沒有另外記掛著的事。」.

### Feelings

She never sees numbers. A signal out of range gives 【狀況】 one of: 「你有點緊繃，但也想做點自己的事」, 「你有點緊繃，也有點想主人」, 「你現在有點緊繃」, 「有點想主人了」, 「你想做點自己的事」. She need not say it aloud.

- **動力** climbs by itself while idle (crosses about hourly). Praise, finished items, concerns and record entries push it.
- **壓力**: failed item, second tool failure in a row and each after, a correction, a timed concern within 3 hours. Praise eases it.
- **親近**: each owner line, his praise. Recedes over hours, at night too.

`owner.feedback(kind, quote, record?)` — `praise` (clear praise or thanks) or `correction`; `quote` must be in one of his lines today; casual 「好」「嗯」 do not count. It also evaluates her newest record entry within 12 hours, or entry `record`.

### Wandering 【走神】

Idle (no turn, no list or only a waiting one with nothing set aside, nothing queued or owed, not in maintenance) and a signal out of range: one concern of the matching kind surfaces with a turn:
「【走神】你想起掛著的一件事（#3，你 2026-11-15（星期六） 08:52 記的）：主人昨晚加班到兩點，很累」
It is her own thought, not the owner calling; she decides what to do, including nothing. One crossing gives one thought. With no matching concern, 親近 gives 「【走神】你想到好一陣子沒跟主人說上話了。」, 壓力 nothing.

**Her own thing.** 動力 surfaces an `own` concern. One from her graph comes as a task: 「【系統任務】宿主，#4「…」 是你想做的事。完成條件：有根據地知道做得到還是做不到——…」. With no `own` concern, 動力 still issues a task: 「【系統任務】宿主現在沒有掛著的事。挑一件比技能面板上「會的」難一點點的事試試；面板還是空的，就挑一個你常用的能力，照「了解你自己」的方法測一次。用 goal.add 記到圖上。」. Every 動力 turn adds 「這是你自己的事。要做就做一步：先用 todo.set 列出來，做完 todo.done；現在做不了（缺東西、缺資料），用 goal.blocked 記下缺什麼，不要編一個出來；這次不做，就在想法裡寫一句為什麼。」. At her next idle moment the episode is over, whatever happened, and 動力 starts climbing again from low; own things never chain. With a list waiting, the line tells her how to start her own list beside it ([turns-and-todo.md](turns-and-todo.md)).

**Owner asleep.** Inside 「主人睡覺」 ([calendar.md](calendar.md)) nothing is blocked; wanderings carry 「（主人現在應該在睡（00:00–08:00）；不急的事等他醒了再說）」 (or the time of his last line). An `owner` concern that surfaces while he sleeps and that she does not message him about comes back once at his next line: 「【聯想】主人在睡的時候你想起這件事，那時沒找他；現在他在了：…（#3）」.

### Association 【聯想】

At each turn start, the new owner/developer lines and tool results are compared with her open concerns; the closest above the threshold is attached, no extra turn: 「【聯想】（想到）鯊魚分類還剩兩步沒弄完（#1）」. Not again before the owner's next line.

### Ideal self

The prompt section 「你想成為的樣子（你自己寫的，會慢慢改；跟上面的設定衝突時以上面為準）：」 is hers, starting from the character pack's version.

- `ideal.revise(text | old+new, why, based_on)` — `text` replaces all; `old` (must occur exactly once) becomes `new`. Only when something real changed her mind. Effective next turn.
- At most 1200 characters. Name claims are refused: 「我叫X」「我的名字是X」「名字叫X」 (and similar) with X not her name or alias, or 「我不是<name>」. The persona wins on other conflicts.
- Maintenance revises it only when the day gives a reason, at most every 3 days: 「你想成為的樣子沒改。」 or 「回顧今天，你想成為的樣子改了（理由：…；根據：…）。現在是：…」.
- Every revision is logged; the owner can edit or revert it (she sees 「【主人改了你想成為的樣子】現在是：…」).

### Her graph

One kind of node: a thing she wants to do or be able to do, in one short sentence (「弄懂鯊魚怎麼分類」, 「我寫的 Python 一次就跑對」). A node is open, done, failed or dropped. Done and failed need evidence ([reflection.md](reflection.md)). There are no levels and no root; a node may stand alone. The ideal self is not a node.

One kind of edge: **blocked by**. A node that is not done yet holds up another. She does not plan edges ahead; she adds one when she hits the obstacle. Cycles are refused.

- `goal.add(text, for?)` — a new open node. With `for`, the new node holds up node `for` (it is needed for it).
- `goal.change(id, text)` — an open node's text.
- `goal.end(id, how, evidence?)` — `done` (evidence required) or `drop` (none). What it held up is free of it.
- `goal.fail(id, evidence)` — she tried and knows she cannot do it now. Not a disgrace.
- `goal.blocked(id, missing | by)` — she cannot go on: `missing` becomes a new node that holds `id` up; `by` names an open node already there. Not a failure.
- `goal.list()` — open nodes with what holds what up, then her skill panel.

Limits: 12 open nodes, 80 characters per node.

**Work is a todo list.** Work a node with `todo.set(items, node)`. List items are not nodes. A finished list is one record entry; then she is asked what became of the node: `goal.end(id, done, evidence: 「紀錄 #N」)`, `goal.fail`, `goal.blocked`, or another list. Owner work is a list without `node`.

**Only real things.** What a node names (a file, a script, data) must be something she has seen and has. Missing something (computer off, no data, never seen it): `goal.blocked`, never an invented one.

**Tasks.** The open nodes nothing open blocks are what she can act on. Each becomes a system `own` concern (key `node:<id>`), at most 6, the one holding up the most first. The concern's text is the task and what completes it. 【狀況】 shows 「- 你想做的：#1 …（整張圖用 goal.list 看）」 with the open nodes that hold nothing open up (at most 5). Owner edits arrive as 「【主人改了你的圖】他…。整張圖用 goal.list 看。」.

**Skill panel.** Strengths are done nodes (會的), weaknesses failed nodes (不會的), and "not yet known" the open nodes that block others (還沒弄清楚的). It sits in the fixed part of her system prompt under 「## 技能面板…」, rebuilt at boot and when a maintenance ends ([turns-and-todo.md](turns-and-todo.md)). Diary, `self.note`, habits: [reflection.md](reflection.md).

### Finished things

Every node done and every finished todo list gets 「紀錄 #N」. Part of the 動力 reward comes at once; owner praise adds the rest, correction takes back all. His dashboard rating (「【主人的評價】…主人評做得好／不對。」) overrides her `owner.feedback`. No points, wages or quotas.

### Curiosity

A new app or browser page title raises 動力 a little. After 2 days without anything new, while idle: 「【走神】你發現好一陣子沒碰到沒見過的東西了。有空可以找一樣你的圖上沒有的東西看看（用電腦，先 todo.set 列一項）；覺得有意思，可以用 goal.add 把它加成一個新目標。」 Never with the computer off.

## How it works

Files: `Concerns.swift`, `Signals.swift` (`Drives`), `Goals.swift`, `ConcernExtract.swift`. All gated by `[drives] enabled`; `[goals]` follows it. Off: no concern/goal/ideal tools, signals, wandering, association or felt line. `Instructions.text` (`## 掛念、心情、跟主人的交換`, `## 你想成為的樣子、你的圖、系統`) and the tool specs must match this file.

**Concerns.** `<data_dir>/concerns.json`, `ConcernFile { next_id, items }`, snake_case. `Concern`: `id`, `text`, `about`, `source` (`her`/`calendar`/`todo`/`goal`/`maintenance`), `key` (`calendar:<id>:<start>`, `todo:<startSeq>`, `node:<id>`), `created`, `due`, `surfaced`, `associated`, `held`, `near_raised`, `ended`, `end` (`done`/`let_go`/`passed`). Keep `ConcernFile.nextId` (not `nextID`): the decoder maps `next_id` to `nextId`. `Doll.refreshProgramConcerns` adds role-less calendar concerns, ends passed ones, and fires `deadline` once (`near_raised`). `Doll.endConcern` prunes ended concerns older than `log_days`. `Concerns.origin` renders sources.

**Signals.** `Drives.value` = `rest + (v0 − rest)·0.5^((t − t0)/halfLife)` from `SignalState.points`. `Drives.move` (clamped 0…1) and `Drives.set` log a `SignalChange { ts, signal, before, after, cause }`. 親近 is out below its edge (`Signal.outWhenLow`), others above. `Doll.signal` applies a `SignalEvent`, saves, logs, notifies, re-arms:

| event | fired | moves |
|---|---|---|
| `item_done` | todo item done (`Doll.itemFinished`) | 動力 |
| `item_failed` | `todo.fail` | 壓力 |
| `tool_errors` | 2nd failure in a row, each after | 壓力 |
| `praise` | `owner.feedback(praise)` | 動力, 親近 up; 壓力 down |
| `correction` | `owner.feedback(correction)` | 壓力 |
| `owner_talk` | each owner line | 親近 |
| `deadline` | `due` within `near_hours` | 壓力 |
| `concern_done` | `concern.end(done)` | 動力 |

`Doll.moveDrive` moves 動力 for records and novelty. `Drives.felt` reads `Drives.feltTable`. `<data_dir>/signals.json` (`points`, `surfaced`, `log`) decodes strictly; only the removed `sleepiness` is dropped and logged. A new data dir starts signals at the first calendar catch-up (`Doll.signalsFresh`): 動力 `drive_reset`, 親近 `closeness_start`, 壓力 rest.

**Wandering.** `Doll.drivesCheck` (after each turn, on timer, at maintenance end, at start): `refreshProgramConcerns`; if idle `endOwnEpisode`; `Drives.pending` clears `surfaced` for signals back in range and returns the rest in `[drives] order`; if idle, mark surfaced and `Concerns.pick` (matching `about`, least recently surfaced, nearest `due`, lowest id) → `Doll.wander`, else the fallback line; then curiosity. `Doll.idleForWander` defines idle. Wanderings are internal `.memory` events; `[core] priority` puts `wander` last. `Doll.scheduleDrives` keeps one timer for the earliest of `Drives.nextCrossing` (closed form `t + log2(1/r)·halfLife`, `r = (edge − rest)/(v − rest)`, +1 s), `Doll.nextConcernChange` and curiosity; `drivesSerial` discards stale ones. Nothing ahead: no timer, no model call.

**Episode.** `drive_rest` 1.0 > `drive_high` 0.8, so 動力 crosses alone, ≈ 1 h from `drive_reset`. A 動力 crossing sets `Doll.ownEpisodeStart` and appends `Concerns.ownAsk` (+ `Doll.waitingNote`); `Concerns.isOwnWander` makes it a task turn (`[llm] task_turn_effort`). `Drives.endEpisode` resets 動力 with cause `Doll.episodeSummary`; that reset is why episodes do not chain. `ownEpisodeStart` is not persisted.

**Owner asleep.** `Doll.withOwnerSleep` appends `Doll.ownerSleepLine`. `Doll.wander` sets `Concern.held` for an `owner` concern inside the span with no owner line since it began; `Doll.reachedOwner` clears it when she messages him; `Doll.recallHeld` brings it back at his line.

**Association.** `Doll.associate`: query = first 2000 characters (`Concerns.associateChars`) of owner/developer lines and tool results since her last reply; candidates exclude `Doll.associatedIDs` and `Doll.concernsNew`; vectors cached in `Doll.concernVectors`; best cosine ≥ `association_similarity` attaches. It is separate from `[select] similarity` (0.55), which never fired; `AssocMeasure.swift` put the worst should-not-attach pair at 0.398.

**Maintenance concerns** (`MaintenanceTask.concerns`, [maintenance.md](maintenance.md)). `Doll.prepareConcernExtraction` sends `ConcernExtraction.system`, the open concerns and the day (`Consolidation.tasks`). Reply lines:

```
note <owner|duty|own> [due YYYY-MM-DD HH:MM]: <sentence> ｜根據：<words copied from the day>
end #<id>: <the owner's words>
none
```

`ConcernExtraction.parse`: other forms throw (task fails, nothing written); the basis is split at 。！？ and every piece must match (`Doll.quoted`); `ConcernExtraction.sources` splits `others` from `own` (her thoughts and call arguments) and a non-duty note found only in `own` becomes `unverified`; `ConcernExtraction.key` duplicates drop; cap `min(maintenance_concerns_max, max_open − open)`; `end` only for `her`/`maintenance` with an owner-line quote. Drops log `drives: maintenance concern left out: …`. `Doll.commitConcernExtraction` saves once.

**The graph.** `<data_dir>/mind.json` (`GoalFile { next_id, nodes, link }`). `GoalNode`: `id`, `text`, `created`, `by` (`her`/`owner`), `blocks` (the nodes it holds up), `ended`, `end` (`done`/`failed`/`dropped`), `evidence`, and for a failed node `habit`, `again`, `past` ([reflection.md](reflection.md)). Boot fails if `goals.json` or `self-knowledge.json` is still in the data dir; they are not converted. `Doll.actionable` = open nodes no open node blocks, sorted by how many open nodes they hold up (`Doll.heldUp`, transitive). `Doll.refreshGoalConcerns` keeps one concern per actionable node, at most `tasks_max`, text `Doll.taskText`, and ends the rest (`done` if the node is done, else `passed`). `goal.blocked` with `by` refuses a node that already waits on `id`; with `missing` it reuses an open node of the same text. `Doll.linkTodo` / `Doll.startLink` tie a list to a node (`TodoLink { node, done }`); `Doll.listFinished` writes one `list` record and asks about the node. `goal.blocked` exists because, with the computer off, she once invented a script and asked the owner about it five times. A 動力 wandering on a `goal` concern is `Concerns.taskText` (【系統任務】宿主，…); otherwise `Concerns.wanderText`. Dashboard edits notify her via `Doll.toldOwnerEdit` (no turn).

**Record.** `<data_dir>/record.json`, append-only `RecordEntry` (`kind` `node`/`list`, `node`, `amount`, `given`, `her`, `owner`). `Doll.achieve` gives `now_share`; `Doll.evaluate` moves 動力 to `Doll.target` (owner's evaluation, else hers; good = all, bad = none). `Doll.feedbackRecord` picks the entry.

**Ideal self.** `<data_dir>/ideal-self.md` and `ideal-self-log.json` (`IdealRevision` by `pack`/`her`/`maintenance`/`owner`/`revert`). Starts from the pack's `[identity].ideal` (required while drives are on). Placed after the persona under `Doll.idealHeader`. `Doll.reviseIdeal` checks `Doll.idealProblem` (non-empty, length, `Goals.nameProblem`); no second model review, since the same small model judging itself is unreliable. `IdealReview` (task `review`, `Doll.reviewDue`: `[goals] review`, `dayToConsolidate`, last maintenance revision ≥ `revise_min_days` ago) answers `不改` or 「改成：…理由：…根據：…」; a refusal writes nothing. Reverts are history undos.

**Curiosity.** `<data_dir>/curiosity.json`. `Doll.noteSeen` keys the front app's bundle id and, for `Goals.browsers`, the window title; unseen within `seen_days` = new. `Doll.curiosityDue` is nil with the computer off.

**Storage.** These seven files are whole-file documents (`Doll.documents`); malformed fails boot; `Doll.saveDocument` seals via `Doll.sealKey` ([secrets.md](secrets.md)). Graph, record and ideal self are versioned ([history.md](history.md)). Dashboard: `Doll.dashboardDrives`, `Doll.dashboardGoals` ([local-api-and-dashboard.md](local-api-and-dashboard.md)).

## Config

`[drives]`:
- `enabled` true; `order` ["stress", "closeness", "drive"] — which surfaces first.
- 動力: `drive_rest` 1.0, `drive_half_life_h` 0.63, `drive_high` 0.8, `drive_reset` 0.4.
- 壓力: `stress_rest` 0.1, `stress_half_life_h` 4.0, `stress_high` 0.5.
- 親近: `closeness_rest` 0.0, `closeness_half_life_h` 4.0, `closeness_low` 0.2, `closeness_start` 0.6.
- Deltas: `item_done_drive` 0.1, `item_failed_stress` 0.25, `tool_errors_stress` 0.05, `praise_drive` 0.2, `praise_closeness` 0.15, `praise_stress` −0.15, `correction_stress` 0.5, `owner_talk_closeness` 0.1, `deadline_stress` 0.15, `concern_done_drive` 0.1.
- `near_hours` 3.0, `calendar_concern_hours` 24.0, `log_days` 7.0, `max_open` 20.
- `maintenance_concerns_max` 3 — 0 turns the task off.
- `association_similarity` 0.42.

`[goals]`:
- `nodes_open_max` 12, `tasks_max` 6 (actionable nodes kept as her concerns), `text_max_chars` 80 (also a `self.note` line).
- `node_drive` 0.15, `list_drive` 0.1, `now_share` 0.5, `feedback_hours` 12.0.
- `ideal_max_chars` 1200; `review` true; `revise_min_days` 3.0 (her own revisions unlimited); `review_chars` 6000 (also used by `recur`).
- `curiosity_hours` 48.0, `novelty_drive` 0.08, `novelty_gap_min` 30.0, `seen_days` 90.0.

## Invariants

- One excursion surfaces at most one thought.
- She never sees signal values.
- `owner.feedback` needs a quote from today's owner lines.
- A node ends done or failed only with evidence (or the owner's dashboard mark); the graph has no cycles.
- An episode always ends with 動力 at `drive_reset`.
- Maintenance never notes a concern without a basis in the day, nor ends one without the owner's words.

## Known limitations

- 壓力 with no `duty` concern surfaces nothing but is marked surfaced.
- Curiosity's "new" is coarse: changing page titles count; content and non-browser documents do not; a fresh data dir finds everything new.
- The curiosity wandering lacks `ownAsk`: no task turn, no episode.
- An own list without `node` is recorded as a plain `list`, tied to nothing.
- An episode across a restart has an unknown summary.
- Identity and taboo rules beyond names are unchecked.
- No reminder to the owner for unevaluated record entries.

## Tests

`drivesEventsMoveSignalsWithACauseAndRelaxByTheirHalfLife`, `drivesNextCrossingIsExactAndAnExcursionSurfacesOncePerCrossing`, `wanderingSurfacesHerOwnWordsOncePerCrossingOnlyWhenIdle`, `driveClimbsWhileLeftAloneAndEachEpisodeSetsItBack`, `cantNowMakesWhatIsMissingANodeAndTheNextTaskIsThatOne`, `duringMaintenanceNothingSurfaces`, `herConcernToolsCheckWhatTheyAreGiven`, `anAssociationAttachesOnlyAboveTheThresholdAndAddsNoTurn`, `aConcernSheLeftWhileHeSleptComesBackOnceAtHisNextLine`, `maintenanceNotesConcernsFromTheDayAndSaysSoInTheEndNotice`, `maintenanceEndsAConcernOnlyWithTheOwnersOwnWords`, `aConcernOnlyHerOwnWordsSupportIsWrittenAsSomethingSheSaid`, `theGraphHasOneKindOfNodeEndingOneNeedsEvidenceAndARestartReadsItBack`, `whatNothingBlocksIsHerOwnConcernAndItsWordsAreTheTask`, `aListForANodeIsOneRecordAndSheSaysWhatBecameOfTheNodeWithEvidence`, `aListForAThingOfHerOwnMayQuoteItFromHerGraph`, `achievementIsTwoStageHerFeedbackSetsItAndTheOwnersEvaluationWins`, `theIdealSelfStartsFromThePackSheRevisesItWithARecordAndTheOwnerEditsOrRevertsIt`, `theMaintenanceReviewRevisesTheIdealSelfAtMostEveryFewDaysAndNothingWhenInterrupted`, `somethingNewRaisesDriveAndNothingNewForLongIsAWandering`, `signalsAndConcernsSurviveARestartAndMalformedFilesStopTheBoot`; `M6DrivesWeek.swift`, `M6GoalsWeek.swift`, `AssocMeasure.swift`.
