# History

`History.swift` keeps a git history of the files that make up the doll's self: ideal self, goal tree,
record, memory corrections, saved tools, diary, self-knowledge and every memory file. Each change is
committed when it is made, with who made it and why. The owner sees diffs, undoes any change and returns
everything to a point; the doll lists changes and undoes her own. History is never rewritten: an undo is
a new commit.

## What the doll sees and does

- `history.list(about?)`: her recent changes, newest first, at most 8, one line each:
  `〔id〕MM-dd HH:mm who what：why｜動到：things`. `about` filters by 想成為的樣子/`ideal`, 目標/`goals`,
  工具/`tools`, 記憶/`memory`, 紀錄/`record`, 日記/`diary`, 長短處/`self`, a tool name, or a memory id.
  Changes touching a folder her turn may not read show only
  `改了你現在看不到的記憶（主人的回合才看得到）`. She gets no diff. When the owner asks what she changed,
  she answers from this list and claims nothing it does not show.
- `history.undo(id, why)`: undoes one change of hers (copy the 〔〕 id; `why` required). Effective at
  once. When she finds she changed something wrongly, she undoes it rather than changing it again.
  Changes by the owner, maintenance or the program are refused; she tells the owner if she disagrees. A
  change touching a folder she cannot see now is reported as not found. If the file changed since in the
  same place, the refusal names the later change: undo that first, or ask the owner to restore a point.
- When the owner undoes or restores, her next turn carries a notice event:
  `【主人退回了一個改動】<time> <who>的「<what>」…` or
  `【主人把你的東西回到 <time> 的樣子】那之後的改動都退回了，動到：…`. It is the owner's decision: she
  does not change it back at once and asks him first if she disagrees.
- If a commit fails, her tool result ends with
  `（這個改動生效了，但沒記進改動紀錄：…。主人會在開機報告看到。）`.

## How it works

### What is versioned

Tree paths are `data/…` (`[core] data_dir`) and `memory/…` (`[memory] dir`):

- `data/`: `History.dataFiles` = `ideal-self.md`, `ideal-self-log.json`, `goals.json`, `record.json`,
  `memory-fixes.json`, `self-knowledge.json` (`Reflection.selfFileName`); `tools/*.json`; and
  `diary/*.md` (`Reflection.diaryDir`).
- `memory/`: every `.md` one level below each folder: `secret`, `own`, `public`, and the registry.

Not versioned: records that are their own history (timeline, logs, boot reports, the computer's action
log, screenshots, voice audio, `signals.json`); program state (`calendar.json`, `concerns.json`,
`estimates.json`, `curiosity.json`, `maintenance.json`, the SQLite index); tool run counters
(`ToolStore` keeps `uses`, `successes`, `failures_in_row`, `last_run` in `<data_dir>/tool-runs.json`,
so a run makes no commit).

### The repository

- One bare repo at `[history] dir` (under `data_dir` unless absolute), mode 0700, branch
  `History.branch` (`refs/heads/main`), no remote. One repo for both trees, so a change touching both (a
  memory fix) is one commit.
- `Git` runs every call with explicit `--git-dir` and `Git.environment`: inherited `GIT_*` dropped,
  `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_TERMINAL_PROMPT=0`. The owner's git config
  is never read and an outer repo never adopts the data dir.
- `Git.locate` checks `[history] git` once per process; missing or broken is a boot error with the fix.
- `History.locked`: thread lock plus `flock` on `<repo>/doll.lock`, so the doll and the offline
  `dollcore … consolidate` never commit at once.

### Committing

`History.record(_:)` snapshots kept files (`History.snapshot`); unchanged → no git call. Otherwise
`History.commitLocked` writes one commit via one `git fast-import` stream (about 63 ms, on the `Doll`
actor). If the branch moved under it, the commit is refused.

`History.message`: first line `<who>：<what>（<task>）` with `HistoryWho.ownerWords` (她/主人/系統維護/程式),
then trailer fields `Who:` (`her`/`owner`/`maintenance`/`program`; unknown parses as `program`),
`Task:`, `What:`, `Why:`, `Args:`, `Ref:`, `Undoes:`, `Restores:`, `Unrecorded-Before:`. `Task:` is the
maintenance task. `Why:` for her calls is the first non-empty of `why`, `reason`, `basis`, `based_on`,
`when`, `cause` (`Doll.commitCall`). `Args:` at most 300 characters. `Ref:` `ideal#N`/`fix#N` records
since the last commit (`Doll.historyRefs`).
Newlines become `⏎`. Author `dollcore <dollcore@localhost>`, time from the doll's clock.

Who commits: her, after each tool call (`Doll.commitCall`); maintenance, one commit per task item (each
audit correction separately); the owner, one per dashboard action; the program, its own writes. All go
through `Doll.commitHistory`, which never throws.

### Sealed content

Files of the sealed folders and documents written as boxes are stored as the same ciphertext as on disk.
A commit touching `secret`/`own` (`History.isSealed`), and every commit while `Doll.sealing`, drops
`Why` and `Args` and keeps `What`. `History.diff` opens sealed files in memory and shows them whole
(`History.wholeDiff`). See [secrets.md](secrets.md).

### Failures

- Commit failure: the change stands; `Doll.commitHistory` logs `ERROR` (listed in the boot report) and
  returns the failure; `History.record` names the files in the next commit as `Unrecorded-Before:`.
- Crash between write and commit, found by `History.open` at boot: no repo yet → first commit `開始記錄`
  by `program` (`History.Boot.adopted`); files differ from head → commit `開機時發現沒記錄的改動`
  (`History.Boot.found`), named in the boot report's `history` field and a log `WARN`. She is not told.
- Owner undo/restore whose commit fails: the dashboard says `退回了，但沒記進改動紀錄：…`.

### Undo and restore

- `History.undoWrites(_:)` writes the inverse of one change per file: unchanged since → back; created by
  it → removed; changed since → three-way `History.merge` (JSON by structure, lists of objects by `id`,
  `next_id` kept; front-matter markdown by key and body; else only if one side is unchanged). A conflict
  refuses and names the later changes. Sealed files are opened and resealed (`History.mergeFile`).
- `History.restoreWrites(_:)` writes every kept file as it was right after a change.
- `Doll.reloadAfterHistory` reloads ideal self, its log, goals, record, memory fixes, self-knowledge,
  rebuilds the diary index if a diary file changed, and reindexes memory files; then the caller commits.

Dashboard routes `GET /api/history`, `GET /api/history/diff/<id>`, `POST /api/history/undo/<id>`,
`POST /api/history/restore/<id>`: [local-api-and-dashboard.md](local-api-and-dashboard.md). `/api/ideal/revert` and `/api/memory/revert/<id>` find the commit with
`Ref: ideal#N`/`fix#N` (`Doll.undoRef`). The notice to her is a secret event when the change touched the
secret folder (`Doll.touchesSecret`). Her tools: `Doll.historyListTool` (paths from `Doll.historyPaths`,
hidden folders from `Doll.hiddenFolders`), `Doll.historyUndoTool`.

## Config

- `[history] git` (`git`): executable name or path.
- `[history] dir` (`history`): the repo, under `data_dir` unless absolute.
- `[history] list_items` (8): entries `history.list` shows.
- `[history] dashboard_items` (200): entries the dashboard lists.

## Invariants

- Every change of a kept file is committed when made, named in the next commit, or found at boot.
- Undo and restore add commits; history is never rewritten or deleted.
- The repo never reads the owner's git config or touches anything outside `[history] dir`.
- Sealed content enters the repo only as ciphertext; sealed commits carry no reason or arguments.
- She can undo only `Who: her` changes.

## Known limitations

- A changing call is tens of milliseconds slower (commit on the actor).
- Undo of a file changed since may refuse on conflict; the fallback is the owner's restore.
- With two processes on one repo the later commit is refused; the change is recorded at the next commit
  or boot.

## Tests

`eachChangeOfHersIsOneCommitWithWhoWhatAndWhy`, `aToolRunChangesOnlyItsCountersAndMakesNoCommit`,
`aFailedCommitKeepsTheChangeSaysSoAndTheNextCommitNamesIt`,
`aChangeWithNoCommitIsFoundAtBootCommittedAndInTheBootReport`,
`theHistoryAdoptsTheDirsAsFoundKeepsOnlyHersAndNeverTouchesAParentRepository`,
`sheUndoesHerOwnChangeWithAReasonButNotTheOwnersAndAConflictNamesTheLaterChange`,
`theOwnerUndoesAChangeOfHersOnTheDashboardAndSheIsToldInPlainWords`,
`aSecretChangeIsNotShownOutsideTheOwnersTurnsButTheDashboardDiffShowsIt`,
`sheOverwritesAMemorySheWroteHerselfWithoutABasisAndTheUndoBringsItBack`.
