# Diary and self-knowledge

Her diary, her strengths and weaknesses, and the habits she sets from her weaknesses
(`Reflection.swift`, 2026-11-23). Owner's aim: she reflects, knows her strengths and weaknesses, and
her goal tree gets better, so that she grows in a reasonable way.

Switched by `[reflection] enabled`, and only with `[maintenance] enabled` and `[drives] enabled`
(`Doll.reflectionOn`): the diary turn opens the maintenance, and a weakness's goal is a node of her
tree. Off means: no diary turn, no `diary.*`/`self.*` tools, no habits in the prompt, no `recur` task,
no diary in `recall`. The test harness turns it off; the tests about it turn it on.

What she is told: the `## 日記、你的長處和短處` part of `Instructions.text`, the tool descriptions
(`Tools.reflectionSpecs`), the diary turn's notice (`Reflection.notice`) and `dolldocs/reflection.md`.
Keep them consistent.

## Owner decisions (2026-11-23)

| Decision | Meaning in code |
|---|---|
| The diary is hers; he reads it in the dashboard; it is never sent to him | no speaking tool in the diary turn; `/api/reflection` read only |
| Written as the first thing of the maintenance, by her (not a separate request imitating her) | `startDiaryTurn` before the tasks; a real turn with her whole context |
| A light guide, nothing required | `Reflection.notice` lists directions |
| She reads it any time; writes only in that turn | `Reflection.diaryOnly` refused outside the turn |
| Strengths and weaknesses are her judgment, each with evidence | `checkedEvidence` |
| A habit-type weakness becomes a habit; long work becomes a goal | `self.habit`, `self.goal` |
| Habits always in her prompt, capped | `habitsText()` after the ideal self, `[reflection] habits_max` |
| Nothing disappears by itself; she decides, with why | only `self.past` moves an item; `weakness_quiet_days` only marks |

## Files

- `<data_dir>/diary/YYYY-MM-DD.md`: one entry per maintenance, the day the timeline segment began. A
  second maintenance on the same day writes `YYYY-MM-DD-2.md`. Chosen once per maintenance
  (`MaintenanceFile.diaryFile`), so a retry after an interruption rewrites the same file. Written whole
  (`diary.write` replaces it), sealed when the turn saw a secret (`sealKey`), and then stays sealed.
- `<data_dir>/self-knowledge.json`: `SelfFile { next_id, items }`, each `SelfItem`:

| field | meaning |
|---|---|
| `kind` | `strength` or `weakness` |
| `text` | one sentence, at most `[reflection] item_max_chars` |
| `evidence` | where it showed: a quote, or `紀錄 #N：…` |
| `habit` | `{ when, then, since }` (weakness only) |
| `goal` | goal-tree node id (weakness only) |
| `again` | each day it showed again (the `recur` task; quote, `by`) |
| `past` | `{ why, ts }`: moved to 以前有 by her |

Both are in `Doll.documents` (the boot needs the key for a sealed one) and in her history
(`History.dataFiles`, `History.files` lists `data/diary/*.md`); `history.list` takes `日記` and
`長短處`; an undo reloads `selfFile` and rebuilds the diary index.

## Evidence

`Doll.checkedEvidence` accepts `紀錄 #N` of `record.json`, or a quote at least
`[reflection] quote_min_chars` long found verbatim in an event of the current segment that is not
hers (`notHers`): not her replies, not the results of tools that echo her own words
(`diary.*`, `self.*`, `recall`, `think`, `remember`, `history.list`), not 【相關記憶】, not the diary
notice. Surrounding 「」 or quotes are stripped. The same check guards the `recur` task's quotes.

## The diary turn

See [maintenance.md](maintenance.md) for where it sits. In the turn (`Doll.inDiaryTurn`):

- `Doll.diaryRefusal` refuses every call not in `Reflection.diaryTurnTools` (diary, self, `recall`,
  `think`, `goal.list`, `goal.add`, `goal.change`, `history.list`), before the todo and computer
  gates; outside the turn it refuses `Reflection.diaryOnly`.
- After each reply, `followUp` is set only when the reply called a tool and fewer than
  `[reflection] max_turns` replies ran; the todo phase does not keep the turn going and no
  no-call reminder is added.
- Wandering (`idleForWander`) waits while the turn runs.

## The `recur` task

One request to the maintenance model (`Reflection.recurSystem`/`recurUser`) with her current
weaknesses and the end of the day (`[goals] review_chars`). It answers `#id｜quote` lines or `沒有`.
Each line whose weakness exists and whose quote passes `checkedEvidence` is appended to `again`;
the rest are logged as refused. Sealed with the day like the other tasks.

## Recall

At boot and after the history wrote diary files, every paragraph of every diary file is embedded
in memory (`diaryIndex`). `recall` appends the `[reflection] recall_max` nearest passages the turn
may see (a sealed entry only where the secret folder is visible), marked as her own words. There is
no relevance gate, like the rest of `recall`.

## Known limitations

- The index is rebuilt by embedding every paragraph at boot; years of diaries will slow the boot.
- A weakness's `again` comes only from the `recur` task (one model request reading the end of the
  day): it can miss a recurrence, and only quotes that are not her own count.
- Strength items have no recurrence tracking.

## Tests

`maintenanceBeginsWithHerOwnDiaryTurnWhoseNotesNeedEvidenceThenItsTasksRun`,
`herDiaryTurnInterruptedByTheOwnerEndsAndComesBackWritingTheSameEntry`,
`outsideHerDiaryTurnSheReadsButCannotWriteAndRecallBringsHerDiaryAsHerOwnWords`.
