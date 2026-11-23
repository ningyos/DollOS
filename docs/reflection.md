# Diary and self-knowledge

The doll keeps a diary, a list of her strengths and weaknesses with evidence, and habits set from her weaknesses, so that she reflects and grows in a reasonable way. She writes all of it herself in a turn of her own at the start of each [maintenance](maintenance.md), and reads it any time. The owner reads it in the dashboard; it is never sent to him.

## What the doll sees and does

**The diary turn.** Maintenance opens with 「【系統維護】開始，現在 HH:MM。維護之前先寫今天的日記：用 diary.write 寫一篇，寫給你自己的（主人在後台看得到，但不會傳給他）。…」, which suggests directions (what happened, feelings, what she learned, tomorrow; none required) and lists her strengths and weaknesses. Only `diary.*`, `self.*`, `recall`, `think`, `goal.list`, `goal.add`, `goal.change`, `history.list` run; anything else fails with 「失敗：系統維護中，這個回合只寫日記、看自己、改習慣和目標，<tool> 沒有執行。…」. No messages, no computer. The turn ends with a reply that calls no tool, or after 8 replies. Not writing is allowed; nothing writes for her. An owner or developer line stops it (what she wrote stays); at the retry she continues the same entry.

**Tools.** Marked * work only in the diary turn; outside it they fail with 「失敗：<tool> 只在系統維護開始、寫日記的那個回合能用。現在想到的，用 concern.note 記著，到時候再寫。」
- `diary.write(text)`* — the whole entry, ≤ 3000 characters; writing again replaces it.
- `diary.read(date?)` — `date` like `2026-11-22`; without it the newest 3. Sealed entries are hidden where secrets may not be seen.
- `self.read()` — items with evidence, habits, linked goals, recurrences, and 以前有.
- `self.note(kind, text, evidence)`* — `kind` `strength` or `weakness`; one sentence ≤ 80 characters; ≤ 20 current items.
- `self.habit(id, when, do)`* — a habit for a current weakness; `when` + `do` ≤ 80 characters; ≤ 8 habits. `self.habit(id, why)` removes it.
- `self.goal(id, goal)`* — link a weakness needing long work to an open tree node (add it with `goal.add` first).
- `self.past(id, why)`* — move an item to 以前有 (its habit goes too).

**Evidence** is required and checked: 「紀錄 #N」 (a record entry, [drives-and-goals.md](drives-and-goals.md)), or a verbatim quote of at least 4 characters from today that is not hers: people's lines, tool results (e.g. 「失敗：…」), program notices. Her replies, diary, self-knowledge and other echoes of her words do not count.

**Habits** stay in her system prompt after the ideal self under 「你的習慣（你從自己的短處定的；碰到那個情況就照做）：」 as `- when→do` lines.

**Recurrence.** She does not note a known weakness again: each maintenance records when one showed again, with a quote. End notice: 「你的短處今天又出現的：#N（self.read 看）。」 or 「你的短處今天沒有看到又出現的。」. `self.read` shows 「定了習慣以後又出現 N 次」 (or 「記下以後…」), the latest, and 「已經 N 天沒再出現」 after 14 quiet days. Nothing leaves by itself; only she moves an item, with a reason.

**Recall.** `recall` appends the 2 nearest diary passages under 「你的日記裡最接近的 N 段（你自己寫的話，不是事實的來源）：」. Facts come from memory, the timeline and the record, not the diary.

## How it works

`Reflection.swift`. Active only when `Doll.reflectionOn`: `[reflection]`, `[maintenance]` and `[drives]` all enabled. Off: no diary turn, tools, habits, `recur` or diary recall. Test harnesses turn it off except its own tests. `Instructions.text` (`## 日記、你的長處和短處`), `Tools.reflectionSpecs` and `Reflection.notice` must match this file.

**Files.** `<data_dir>/diary/YYYY-MM-DD.md`: one per maintenance, named for the day the segment began (`-2` for a second that day); `Doll.diaryFileName` picks it once into `MaintenanceFile.diaryFile` so a retry rewrites it. Sealed via `sealKey` when the turn saw something sealed, then stays sealed; `seal_diary = false` writes plain. `<data_dir>/self-knowledge.json`: `SelfFile { next_id, items }`; `SelfItem` has `kind`, `text`, `evidence` (`{ quote, ts, by }`), `created`, `habit` (`{ when, then, since }`), `goal`, `again`, `past` (`{ why, ts }`). Both are in `Doll.documents` and history (`History.dataFiles`, `data/diary/*.md`; `history.list` accepts `日記`, `長短處`); an undo reloads `selfFile` and the diary index ([history.md](history.md)).

**Diary turn.** `Doll.startDiaryTurn` sets `diary`, `diaryTurns`, `diaryFile` and queues the notice. `Doll.diaryRefusal` refuses outside `Reflection.diaryTurnTools` in the turn and `Reflection.diaryOnly` outside it. After each reply, `followUp` = called a tool and `diaryTurns < max_turns`. When she is idle, `maintenanceCheck` calls `finishDiaryTurn` (logs `diary … written` or `no diary today`). `idleForWander` waits during the turn.

**Evidence check.** `Doll.checkedEvidence`: 「紀錄 #N」 from `record.json`, or a quote ≥ `quote_min_chars` (outer quotes stripped) in the newest matching event of `Doll.notHers`, which excludes her replies, results of `diary.*`, `self.*`, `recall`, `think`, `remember`, `history.list`, and memory events starting 【相關記憶】 or 【系統維護】開始.

**`recur` task.** Due (`recurDue`) with reflection on, `dayToConsolidate` and a current weakness. One maintenance-model request (`Reflection.recurSystem`/`recurUser`) with weaknesses, habits and the last `[goals] review_chars` of the day; answer `#id｜quote` lines or `沒有`. A line is kept if the weakness is current, the quote passes `checkedEvidence`, and its event is after the item was noted and after its last recurrence; others are logged refused.

**Recall index.** `Doll.rebuildDiaryIndex` embeds every diary paragraph at boot and after history writes; `diary.write` re-indexes its file. `Doll.diaryRecall` returns the `recall_max` nearest visible passages; no relevance gate.

**Dashboard.** `Doll.dashboardReflection` at `/api/reflection`, read only.

## Config

`[reflection]`: `enabled` true; `diary_max_chars` 3000; `max_turns` 8 (diary-turn replies); `read_days` 3; `dashboard_days` 30; `recall_max` 2 (0 = none); `items_max` 20; `item_max_chars` 80; `habits_max` 8; `habit_max_chars` 80; `quote_min_chars` 4; `weakness_quiet_days` 14 (only shown, never moves an item); `seal_diary` true (false only for test dolls, else secrets stay plain).

## Invariants

- Diary and self-knowledge change only in the diary turn or by history undo.
- Every item and recurrence has checked evidence that is not her own words.
- Only `self.past` moves an item.
- A sealed diary stays sealed.

## Known limitations

- Boot embeds every diary paragraph; years of diaries will slow it.
- Recurrence comes from one request reading the end of the day; it can miss one.
- Strengths have no recurrence tracking.

## Tests

`maintenanceBeginsWithHerOwnDiaryTurnWhoseNotesNeedEvidenceThenItsTasksRun`, `herDiaryTurnInterruptedByTheOwnerEndsAndComesBackWritingTheSameEntry`, `outsideHerDiaryTurnSheReadsButCannotWriteAndRecallBringsHerDiaryAsHerOwnWords`, `aTestDollWritesItsDiaryPlainEvenOnADayThatTouchedASecret`.
