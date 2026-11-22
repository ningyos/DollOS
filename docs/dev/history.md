# History: versioning and undo of what is hers

`History.swift` keeps a git history of the files that make up the doll's self: her ideal self, goal
tree, record, memory corrections, saved tools, and every memory file. Every change is committed when
it is made, with who made it and why. The owner can see diffs, undo any change, and return
everything to a point. She can list changes and undo her own.

This layer is version control and rollback only. It adds no new kind of content.

Related: [secrets.md](secrets.md) (sealed blobs, sealing), [memory.md](memory.md),
[drives-and-goals.md](drives-and-goals.md), [maintenance.md](maintenance.md),
[boot-and-logs.md](boot-and-logs.md), [local-api-and-dashboard.md](local-api-and-dashboard.md),
[config.md](config.md).

## What is versioned

Tree paths are `data/…` (mirroring `[core] data_dir`) and `memory/…` (mirroring `[memory] dir`).

- `data/`: `History.dataFiles` = `ideal-self.md`, `ideal-self-log.json`, `goals.json`,
  `record.json`, `memory-fixes.json`; plus `tools/*.json`.
- `memory/`: every `.md` one level below each folder of `[memory] dir`: `secret`, `own`, `public`,
  and the entity registry (`[memory] registry_dir`).

Not versioned:

- things that are their own history: timeline, logs, boot reports, the computer's action log,
  screenshots, voice audio, `signals.json`;
- program state: `calendar.json`, `concerns.json`, `estimates.json`, `curiosity.json`,
  `maintenance.json`, the SQLite index (rebuilt from files);
- tool run counters. `ToolStore` keeps `uses`, `successes`, `failures_in_row`, `last_run` in
  `<data_dir>/tool-runs.json`, outside the history. A run changes only counters, which is not a
  change of hers and makes no commit. The tool file keeps those keys at zero and is rewritten only
  when what the tool does changes. A tool file written before this split still holds counters until
  it is written again; that write makes one commit.

## The repository

- One bare repository at `[history] dir` (default `history`, under `[core] data_dir` unless
  absolute). It has no work tree. Permissions 0700. `git init --bare --initial-branch=main
  --shared=0600`; branch `History.branch` (`refs/heads/main`). No remote; nothing is pushed.
- One repo for both dirs, so a change touching both (a memory fix: new file, retired file,
  correction record) is one commit.
- `Git` (in `History.swift`) runs every call with `--git-dir` explicit, inside the repo, with
  `Git.environment`: every inherited `GIT_*` variable dropped, `GIT_CONFIG_NOSYSTEM=1`,
  `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_TERMINAL_PROMPT=0`. The owner's git config is neither read nor
  touched, and a data dir inside a project worktree is never adopted by the outer repo.
- Process I/O goes through files in a scratch dir (the repo itself), not pipes. Reason: with every
  cooperative thread blocked in such calls (tests run many at once), pipe readers never ran.
- `[history] git` names the executable (on `PATH` or a path). `Git.locate` checks it once per process
  by running it; missing or broken is a boot error that says how to fix it. On macOS it resolves
  the real binary behind the `/usr/bin/git` stub, which costs about as much again as git itself.
- Locking: `History.locked` takes a thread lock plus `flock` on `<repo>/doll.lock`, so the doll
  process and the offline `dollcore … consolidate` process never commit at once.

## Committing

`History.record(_:)` snapshots every kept file (`History.snapshot`: blob ids computed in process;
a file is rehashed only when size, inode or mtime changed). If the snapshot equals the last one, no
commit and no git call. Otherwise `History.commitLocked` builds one commit with a single
`git fast-import` stream, the changed files inline.

Cost: a call that writes no kept file costs a few `stat`s (about 0.2 ms). A call that does costs
three git processes; a measurement under full test load gave a median of about 63 ms, down from
about 99 ms with five plumbing calls. This runs on the `Doll` actor, so her call's result returns
that much later.

After fast-import, `History.head()` must equal the new commit. If another process moved the branch,
the commit is refused ("the history moved under this process").

### Commit message (`History.message`)

```
她：goal.add（audit）

Who: her
Task: audit
What: goal.add
Why: …
Args: name=value；…
Ref: ideal#3
Undoes: <commit id>
Restores: <commit id>
Unrecorded-Before: her remember
```

- First line: `HistoryWho.ownerWords` (`她`/`主人`/`系統維護`/`程式`), `：`, what, optional task.
- `Who:` is `HistoryWho` raw value: `her`, `owner`, `maintenance`, `program`.
- `Task:` the maintenance task (`audit`, `review`, `consolidate`, `concerns`).
- `Why:` the reason the feature collected. For her calls, `Doll.commitCall` takes the first
  non-empty of `why`, `reason`, `basis`, `based_on`, `when`, `cause`.
- `Args:` her call's parameters, at most 300 characters.
- `Ref:` feature records made since the last commit (`Doll.historyRefs`: `ideal#N`, `fix#N`).
- Newlines in fields become `⏎` (`History.line`).
- Author and committer are `dollcore <dollcore@localhost>`; time is the doll's clock.

`History.parse` reads these fields back into `HistoryEntry`; an unknown `Who:` reads as `program`.

### Who commits when

- Her: after each of her tool calls in `Doll.swift` (`Doll.commitCall`); nothing changed, no commit.
- Maintenance: one commit per task item (each audit correction is its own commit, so its button
  can undo exactly that one).
- Owner: one commit per dashboard action (delete a tool, edit a goal, evaluate, mark deep, undo,
  restore).
- Program: what the program writes on its own.

All go through `Doll.commitHistory`, which never throws.

### Secrets in the history

- Memory files of the secret and own folders are stored as the same ciphertext as on disk
  (`SecretBox`), so undo works on them as on anything else.
- A commit touching those folders (`History.isSealed`) gets no `Why` and no `Args`: the commit
  message is plain, and `remember`'s args are the memory's text.
- While `Doll.sealing` (a turn that saw a secret, see [secrets.md](secrets.md)), every commit drops
  `Why` and `Args` and keeps `What` (the tool name).
- `History.diff` opens sealed files in memory and shows them whole, each line marked ` `, `-`, `+`
  (`History.wholeDiff`), after git's patch of the rest. This includes any file stored as a box on
  either side (a document written in a sealing turn).

## Failure behavior

- Commit failure: the change stands (files and in-memory state are already new; per-feature inverse
  writes would be error-prone). `Doll.commitHistory` logs `ERROR` (the boot report lists it) and
  returns the failure. Her tool result gets
  `（這個改動生效了，但沒記進改動紀錄：…。主人會在開機報告看到。）` ("the change took effect but was
  not recorded; the owner will see it in the boot report"). `History.record` remembers the failed
  change; the next successful commit includes those files and lists them as `Unrecorded-Before:`.
- Crash between write and commit: `History.open` at boot.
  - No repo yet: everything as found becomes the first commit `開始記錄` by `program`
    (`History.Boot.adopted`); boot report and log only, she is not told.
  - Files differ from the head: one commit `開機時發現沒記錄的改動` by `program`
    (`History.Boot.found`); the boot report's `history` field and log `WARN` name the files.
- Owner undo/restore whose commit fails: the files are already changed; the dashboard gets an error
  saying so (`退回了，但沒記進改動紀錄：…`).

## Undo and restore

History is never rewritten.

- `History.undoWrites(_:)` writes the inverse of one change (not committed; the caller reloads with
  `Doll.reloadAfterHistory`, then commits). Per file:
  - unchanged since: back to before;
  - created by that change: removed (later edits of it go too);
  - changed since: three-way `History.merge`. JSON by structure (objects by key, lists of objects
    by `id`; `next_id` keeps its current value so no id is issued twice); front-matter markdown by
    key and body; anything else only if one side is unchanged. A conflict refuses the undo and names
    the later changes. Sealed files are opened for the merge and sealed again
    (`History.mergeFile`).
- `History.restoreWrites(_:)` writes every kept file as it was right after a change.
- `Doll.reloadAfterHistory` reloads in-memory state (ideal self, ideal log, goal tree, record,
  memory fixes) and reindexes memory files.

### The owner (dashboard)

- `GET /api/history`: `Doll.dashboardHistory`, newest first, up to `[history] dashboard_items`
  (default 200). Each `DashboardHistoryEntry` has the entry, plain-word `things`, and `secret`
  (touched the secret folder).
- `GET /api/history/diff/<id>`: `Doll.dashboardHistoryDiff`, secrets included (the dashboard is
  where his secrets show).
- `POST /api/history/undo/<id>`: `Doll.dashboardHistoryUndo`, any author's change.
- `POST /api/history/restore/<id>`: `Doll.dashboardHistoryRestore`.
- The older per-feature revert buttons (`/api/ideal/revert`, `/api/memory/revert/<id>`) are shortcuts:
  `Doll.undoRef` finds the commit carrying `Ref: ideal#N` / `fix#N` and undoes it. Records made
  before the history began have no such commit; the button says to return to a point instead.
- She is told in her next turn (no turn of its own) by a notice event: `【主人退回了一個改動】…` or
  `【主人把你的東西回到 … 的樣子】…` ("the owner undid a change" / "returned your things to …").
  The notice is a secret event when the change touched the secret folder (`Doll.touchesSecret`).

### Her tools

- `history.list(about?)` (`Doll.historyListTool`): at most `[history] list_items` (default 8)
  entries, one line each: `〔id〕MM-dd HH:mm who what：why｜動到：things`. `about` filters by
  `想成為的樣子`/`ideal`, `目標`/`goals`, `工具`/`tools`, `記憶`/`memory`, `紀錄`/`record`, a tool
  name, or a memory id (`Doll.historyPaths`). Entries touching a folder this turn's audience may
  not see (`Doll.hiddenFolders`, `Memory.visible`) only say
  `改了你現在看不到的記憶（主人的回合才看得到）`. She gets no diff.
- `history.undo(id, why)` (`Doll.historyUndoTool`): only changes with `Who: her`. Owner,
  maintenance and program changes are refused with a message telling her to talk to the owner. A
  change touching a folder she cannot see now is reported as not found. `why` is required.

Why only her own: changes by the owner or maintenance are not hers to reverse; she raises
disagreement with the owner instead.

## Invariants

- Every change of a kept file becomes a commit at the time it is made, or is named in the next one,
  or is found at boot.
- Undo and restore add commits; they never rewrite or delete history.
- The repo never reads the owner's git config and never touches anything outside `[history] dir`.
- Sealed content enters the repo only as ciphertext; commit messages for sealed changes carry no
  reason or arguments.

## Known limitations

- A commit runs on the actor: a changing call is slower by tens of milliseconds.
- Undo of a file changed since may refuse on conflict; the fallback is the owner's restore to a point.
- Two dollcore processes writing the same repo: the later commit is refused, the change stands, and
  it is recorded at the next commit or boot.
- The `History` type comment names `History.versioned`; no such symbol exists. The kept list is
  `History.dataFiles` plus `History.files()`.
