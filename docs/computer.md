# The computer

The doll's computer is the owner's own Mac (macOS, Traditional Chinese UI). She drives his apps in the background through the open-source [`cua-driver`](https://github.com/trycua/cua) without moving his pointer, using his keyboard or bringing an app to the front, so he can keep working. She reads one window at a time, acts on it with `act`, and can save a chain that worked as her own tool. The owner can switch it all off with `[computer] enabled = false` (e.g. unattended trials).

## What the doll sees and does

### Gating

`apps`, `open`, `read_window`, `act`, `tool.run`, `read_file`, `write_file`, `edit_file` run only while a todo item is 進行中; otherwise 「失敗：現在沒有進行中的 todo，<tool> 不能用。先用 todo.set 列 todo…」. After `todo.fail` they stay refused until the next `todo.set`, and at 【該決定了】 until she chooses ([turns-and-todo.md](turns-and-todo.md)). A gated call on a 等待中 item resumes it. `tool.save` and `ask_owner` are never gated. With the computer off, all of these except `ask_owner` are unknown tools.

### Reading

- `apps`: running apps and their window titles.
- `open(app, target?)`: open or reach an app (name such as TextEdit, Notes, Safari, Calculator, Finder, or bundle id). `target`: file path (`~` = owner's home) or URL. Then reads the front window; with a file, the window whose title contains the file name.
- `read_window(app?, window?)`: default app = the latest read's; default window = its front one; `window` matches part of a title.

A read is an **element list** and a **screenshot of that window only**. Actionable rows carry `[row]`; the name is in quotes or brackets, and the word after `id=` also counts: `[5] AXButton (7) [id=Seven actions=[press]]` is named `7` or `Seven`. AXButton = button, AXTextField/AXTextArea = text input, AXStaticText = text, AXMenuButton = drop-down. Use the screenshot for what the list lacks. A list cut at `max_elements` says so. Only the newest reads keep list and screenshot; past `[computer] read_batch` all older ones collapse at once to 「（較早讀到的：…；清單和截圖已收起）」.

### Acting: `act(steps, expect?)`

Owner's rule: actions with a predictable result finish in one round. So one `act` carries every step up to the next point where she must see the result, and `expect` is the text she expects in the window afterwards. Steps run in order; the window is read **once at the end**. Two or more steps need `expect`; at most `sequence_max_steps` (20).

Each step is exactly one of:

- `{"click": "儲存"}`: element named 儲存. **Prefer names**, copied exactly (case-insensitive, never a substring). Two matches: refused with each candidate's row; add a role, `{"click": {"name": "儲存", "role": "button"}}`. No match: refused with similar names. Elements that appear after an earlier step (menu, dialog) are also named; the program re-reads the front window for them (up to about 3 s).
- `{"click": 5}`: row 5, only for unnamed elements. If another element is *named* 「5」 it is refused with that element's row. `{"click": {"x": 100, "y": 50}}`: screenshot point, only if the list lacks it. Optional `"count": 2`, `"button": "right"`.
- `{"type": "text", "in": "收件人"}`: type into that field (`in` may be a row; omitted = focus). Text only.
- `{"key": "return"}`, `{"key": "s", "modifiers": ["cmd"]}`: one key; modifiers `cmd`, `shift`, `alt`, `ctrl`.
- `{"scroll": "down", "amount": 3, "in": "內容"}`.
- `{"menu": ["檔案", "儲存⋯"], "app": "TextEdit"}`: menu bar path from the top, names as shown; works with no window.

Example, Calculator 12×3: `[{"click": "1"}, {"click": "2"}, {"click": "乘"}, {"click": "3"}, {"click": "等於"}]`, expect `36`. Type whole texts, not key by key; a step with an unpredictable outcome ends the sequence.

Results:

- 「6 步都做了：…」 and 「跟你預期的一樣：視窗裡有「1,702」。」
- 「跟你預期的不一樣：你預期「…」，讀到的視窗裡沒有。現在是：…」 with the window's values: the call counts as failed. Matching is the evidence rule below.
- 「第 3 步（…）沒做成：<reason>」: later steps not sent; it says how many ran. Driver refusals read `[code] message hint` (e.g. `[stale_element_token]`, `[delivery_failed]`), then the window read again.
- A read failing after the action means the action was sent: do not repeat it.
- 【系統驗證】 stops the sequence (see Safety).

Rows mean the list she saw before writing this reply; names stay valid across re-reads. Refusals: 「沒有做：還沒讀過視窗（或上一次讀失敗了）；先 read_window 或 open。」; 「沒有做：第 N 行不在最新一次讀到的清單裡（…）。用名字指定，或重新 read_window 用新清單的行號。」; 「沒有做：第 N 行是這則回覆裡才讀到的清單，你還沒看過。…用名字指定。」; 「沒有做：這則回覆前面的呼叫改變了視窗，第 N 行現在是 …，你看到的是 …。看新清單再選。」

One reply runs up to the next point where a result must be seen. Several `act`/`tool.run` calls run in order; after a failure or mismatch the rest return 「失敗：前面第 N 步（…）失敗了，這一步沒做」; only the last one that ran carries the new read. Calls after a refused `todo.set` in the same reply do not run. A Calculator task is typically three replies: `todo.set` + `open`; one `act`; `discord.dm` + `todo.done`. With a saved tool the first two are one: `todo.set` + `tool.run`.

The same call failing twice in a row adds 【同一步又失敗】: change approach, or `todo.fail` / `ask_owner`. When two or three approaches to a step failed and nothing new comes to mind, it cannot be done; say so.

### Common situations

- A document app opened in the background often has no window (TextEdit); `open` says so. New document: menu step 檔案 › 新增; new file in a folder: `write_file` an empty file, then `open` it.
- Open files by path with `open(app, target)`: no Finder, foreground unchanged. A missing file: 「沒有開：… 不存在。要新檔案：先用 write_file 建一個（可以是空的），再 open 它。」 Finder list rows cannot be clicked in the background.
- Windows on another Space can be read and clicked without moving the owner.
- ⌘S reaches the app via the menu; a first save shows a save panel (in the list or as a new window).
- Web pages: `open(app: "Safari", target: url)`; lists may be incomplete; right-click on page content may fail.
- Canvas apps and games: almost no list; click by x, y; some cannot be clicked in the background.
- A front window that cannot be read: the result names the app's other windows for `read_window(app, window:)`.
- A missing or disabled menu item: 「選單「檔案 › …」沒有執行：…」.

### Finishing honestly

- For a task from the owner: `discord.dm` the result and how she checked, then `todo.done` (same reply allowed, DM first).
- **Evidence.** An item that used a computer or file tool needs `todo.done(evidence:)` copied from the **latest** read: the last window read, or the current content of the last file used. Another window counts only after reading it again. Ignored: whitespace, line breaks, `[n]`, quote style, `read_file`'s header and line numbers. Everything else matches exactly, punctuation included. Pieces joined by `；`, `;` or newlines each must match. A miss shows what the read holds: continue, `todo.fail`, or correct what she told the owner. Her own words are never the fact; the read is.
- `todo.fail(cause, evidence, tried)`: root cause, not symptom; evidence copied from what the item saw (latest read, its tool results except todo tools, what someone said). An item that saw nothing cannot fail.
- The latest read's screenshot goes once with her next `discord.dm` or `ask_owner`, adding 「（附上了你最後讀的視窗截圖給主人：…）」; `voice.say` cannot carry it. Dropped when the item ends.

### Estimated rounds

A new list's plan has `estimate` (plan fields: [turns-and-todo.md](turns-and-todo.md)). The program finds like past lists (the line the list answers, digits masked, else her items) and takes the median actual rounds of the latest `estimate_similar_max`. Far off (larger > `estimate_far_ratio` × smaller **and** gap ≥ `estimate_far_rounds`, e.g. 2 vs 7): refused once with those lists and the acceptable range. She changes `estimate`, or keeps it with `estimate_reason` (what differs, e.g. a saved tool); filler rules apply; the third try always passes. No like list: accepted. `todo.done` reports estimate vs actual. Runs whether or not the computer is on.

### Safety: ask the owner first

Nothing blocks any app. Before spending money, deleting (files, Trash, mail, notes, account data), sending in the owner's name (mail, messages, posts, forms), typing passwords or logging in, or changing system settings, she asks with `ask_owner(what, where, why)`. It DMs 「【<name>想先問你】」 with 要做／在哪裡／為什麼; the item becomes 等待中 after the reply and resumes on his answer. Undeliverable: 「失敗：主人沒收到：…。Discord 連上之前問不了；先不要做那件事。」 What he explicitly asked for is approved; another sensitive step found halfway is not. Overwriting his files: consider asking.

Enforced in code:

- 「【系統驗證】…停下來，不要試著通過它；用 ask_owner 跟主人說是哪一步叫出它的。」: a system Touch ID/password window on screen or a password sheet in the window. Stops a sequence.
- 「【注意】這個視窗有密碼欄位。密碼不要自己打：停下來，用 ask_owner 跟主人說要做什麼、在哪裡、為什麼。」

### File tools

`read_file(path, from?, to?)` (numbered lines, at most `[files] max_read_lines`), `write_file(path, text)` (whole file; creates folders), `edit_file(path, old, new)` (`old` must occur exactly once). UTF-8 text only; `~` = owner's home. Failure texts: [tool-failures.md](tool-failures.md).

### Her own tools

**`tool.save(name, when, params?)`**: `name` = letters, digits, `-`, `_`, ≤ 40 chars; `when` = one sentence; `params` = `[{"name", "value", "about"}]` for the varying parts.

- Source: the item in progress if it has an `act`, else the item that ended last (not before a restart, not one closed by `todo.fail`).
- Built from what ran, never her text: answered `open`s and `act`s in order. A missed-`expect` `act` is dropped whole; a stopped `act` keeps the steps before the stop. Reads are not saved. The result lists what was left out.
- Elements keep name and role (`AXButton "乘"`), never rows. A name that changes every time (`Note[id=…]`) or none: role only, replayed as "the only one" (two = stop). Points cannot be saved.
- Each param value must be found in exactly one place: typed text (whole or part), an `open` target, or consecutive one-character buttons (pressed 3, 7 → `"37"`; later `"58"` presses 5, 8).
- Refused (「沒存：…」) when the last `act` failed or missed, had no `expect`, a value is absent or ambiguous, the name exists, or `own_tools_max` is reached. Only the owner deletes, on the dashboard.

**`tool.run(name, args?, expect?)`**: one call, one round; may share a reply with `todo.set`. `expect` is required when the result depends on the values (a Calculator answer); a saved expectation containing typed text or a file name follows the new values. Every `act` safeguard applies. A button value with a character no button is named after (「.」 vs 小數點) presses nothing and says which. A stop: 「工具「name」停了：…」 with steps done and the current window; continue by hand with `act`. An unknown name lists her tools.

The closest matching tool may appear in 【相關記憶】 as a 〔工具〕 line with usage and counts, by textual similarity only. After `own_tool_retire_failures` failures in a row a tool is retired: not offered or runnable, kept and visible to the owner (dashboard 她的工具).

## How it works

Files: `Computer.swift` (`ComputerDriver`, `CuaDriverCLI`, `CuaDaemon`, actor `Mac`), `OwnTools.swift`, `Files.swift`, `Tools.swift` (`computerSpecs`, `askOwnerSpec`, `steps`, `gated`, `actionNames`), `Doll.swift`.

- **Call path.** `Doll.execute` → `Mac.apps/open/readWindow/act/replay` → `Mac.call` (adds the session label; first call sends `start_session`) → `CuaDriverCLI.call`: `cua-driver <tool> '<json>'` via `runProcess` (`terminationHandler`; stderr drained on its own thread). Non-zero exit → `ComputerError`; `Mac.said` extracts code/message/hint. `MacResult` → `Doll.computerOutcome`. Tests use `FakeCua`.
- **Boot.** `CuaDaemon.boot` prints one line. Off: `computer: disabled ([computer] enabled = false): no cua-driver, no computer or file tools`; specs, prompt text (`Doll.computerOn`), `Doll.gatedTools`, `Doll.actionTools` and `Doll.offerTools` are empty. On (`CuaDaemon.ensure`): driver executable, `permissions status --json` shows Accessibility and Screen Recording, else `cua-driver serve` within `daemon_start_s`; failure stops the boot with the fix. A daemon dollcore started is stopped at exit (`CuaDaemon.stop`), after `Doll.endComputer`.
- **Session.** One label `dollcore-<pid>-<epoch seconds>` on every call, so only one cursor shows; `cursor_motion.idle_hide_ms = cursor_idle_hide_ms`; `Mac.endSession` at exit.
- **Reads.** `Mac.read`: `get_window_state`, `tree_format: both`, JPEG in `<data_dir>/shots/`. `Mac.windows` keeps off-Space windows; `Mac.readFront` skips tree-less ones (`Mac.NoTree`); `Mac.identities` → `ElementIdentity`; `Mac.current` = latest read. Batched put-away (`Doll.fullReads`, `Doll.readPutAway`) keeps the request append-only so the model server's prefix cache resumes.
- **Acting.** Non-menu steps: one-step `run_steps` with `element_token` `<snapshot_id>:<row>` (`Mac.run`); menus: `invoke_menu` (`Mac.invokeMenu`, `window_id` only for the key window). Names: `Mac.resolve`/`Mac.match`; the driver's own search matched collapsed menus by prefix. Rows: `Mac.beginReply` sets `Mac.seen`. Between steps `Mac.authBetween`; its read (`Mac.between`) re-validates later rows. End: `settle_s`, then `Mac.readEnd`; `Mac.readAfter` separates read failures. `expect`: `Doll.quoted`, `Doll.valueLines`; ordering: `Doll.Chain`; repeats: `Doll.noteRepeats`.
- **Safety.** `Mac.authDialog` (window owner in `auth_processes`), `Mac.authSheet`, `Mac.passwordField`; an unmakeable check also stops. `Doll.askOwner`.
- **Evidence.** `Doll.evidenceRefusal` when `Doll.itemUsedComputer`; `Doll.quoted`, `Doll.evidenceKey`, `Doll.undecorated`; files re-read at `done`; fail evidence from `Doll.itemTexts`. Screenshot: `Doll.ownerShot`, `Doll.noteRead`, `Doll.shotSent`.
- **Estimates.** `Doll.estimateCheck` over `<data_dir>/estimates.json` (`EstimateRecord`), cosine ≥ `estimate_similarity`; `Doll.estimateFar`, `Doll.estimateLine`, `Doll.recordEstimate`. An embedding failure accepts and logs.
- **Experiences.** Each step that went through is a step line (`Mac.step`: `click AXButton "7"`). A `todo.done` item that used the computer writes an experience (`Doll.writeExperience`). A result on another app (`Doll.frontApp`) adds one 【相關記憶】 after the reply (`Doll.attachMemories`, which also offers tools); `Doll.noteSeen` feeds novelty. See [memory.md](memory.md), [drives-and-goals.md](drives-and-goals.md).
- **Action log.** Each action appends a sorted-key JSON line to `<data_dir>/computer-actions.jsonl` (`ActionRecord`, `Mac.log`): item, tool, app, bundle id, window, element identity, args, ok/error, `sequence`, `via`. A failed write throws.
- **Own tools.** Two fixed specs: a spec per tool would change the cached prompt prefix on every save. `Doll.toolSave` → `OwnTools.build` from `ChainPart`s (`Doll.itemChain`), elements via `OwnTools.saved`, params `ParamInput` → `OwnTool.Param.Kind` (`text`, `target`, `buttons`); a fixed final expectation with params becomes `expect_was`. `ToolStore`: `<data_dir>/tools/<name>.json` (sorted JSON, read at every use); counters in `<data_dir>/tool-runs.json`, so a run makes no history commit ([history.md](history.md)). `Doll.toolRun` → `OwnTools.bind` → `Mac.replay` (through `Mac.open`/`Mac.act`; buttons checked before pressing). `Doll.offerTools`: cosine of query to name + `when` + origin task, filtered as configured, not retired, not offered since the owner's last line; `OwnTool.offerLine`. Dashboard: `Doll.dashboardTools`, `Doll.dashboardDeleteTool`.

## Config

`[computer]` (reasons in `dollcore.example.toml`; unknown keys or bad values fail the boot):

- `enabled` = true; `driver` = `/Applications/CuaDriver.app/Contents/MacOS/cua-driver`; `daemon_start_s` = 10.0.
- `max_elements` = 250; `screenshot_max_dimension` = 1024 (px, long side); `screenshot_tokens` = 600 (context estimate only).
- `settle_s` = 0.4 (before the end read); `read_batch` = 4; `sequence_max_steps` = 20.
- `cursor_idle_hide_ms` = 0 (never hide); `window_wait_tries` = 5 × `window_wait_s` = 0.6.
- `auth_processes` = `["SecurityAgent", "coreautha", "LocalAuthenticationRemoteService", "AuthenticationServicesAgent"]`.
- `estimate_similarity` = 0.90; `estimate_similar_max` = 3; `estimate_far_ratio` = 2.0; `estimate_far_rounds` = 3.
- `own_tools_max` = 30; `own_tool_offer_similarity` = 0.45; `own_tool_offer_max` = 1; `own_tool_retire_failures` = 3.
- `decide_after_rounds`, `decide_every_rounds`, `plan_min_chars`, `plan_max_facts`: [turns-and-todo.md](turns-and-todo.md).

`[files] max_read_lines` = 500.

## Invariants

- Gated tools run only with an item 進行中.
- Names resolve by exact equality; ambiguity is refused, never guessed.
- A row is sent only if it is in the read she saw and still the same element.
- An auth window or password sheet stops a sequence, as does a check that cannot run.
- Evidence comes from the latest read, never her words.
- Every action is logged; a failed log write fails the call.
- A saved tool holds only steps that ran; a replay passes every guard of a hand-made step.

## Known limitations

- Opening a Finder folder brings Finder forward for under a second; Chrome came forward 1–2 s. Open files by path.
- TextEdit's save panel buttons have failed with `target_occluded`.
- Repeated misclicks of unverified cause (3, 清除, 3 again); row misclicks stopped once she clicked by name.
- At 250 elements a long web page body is missing, so screenshot-only evidence cannot pass.
- `act` per-step time rose from about 1.0 s to 1.5 s between identical runs; cause unknown.
- The model alone does not reliably ask before deletes and sends; only the auth dialog is enforced.

## Tests

`aSequenceRunsInOrderReadsOnceAtTheEndAndIsOneRound`, `aFailedStepStopsTheSequenceAndTheDoneStepsStay`, `aSequenceWhoseEndIsNotWhatSheExpectedSaysWhatIsThereAndStopsTheReply`, `moreStepsThanTheConfigAllowsOrSeveralWithoutExpectAreRefusedBeforeAnythingIsSent`, `stepsMatchInOrderByNamePointDoubleAndKey`, `aNameIsResolvedInHerLatestReadAndSentAsThatElementsToken`, `anAmbiguousNameIsRefusedWithItsCandidatesAndARoleSettlesIt`, `aNameThatMatchesNothingIsRefusedWithTheNearOnes`, `aNamedStepFindsAnElementAnEarlierStepOpenedAndTheEndReadIsTheNewWindow`, `aRowNotInTheLatestReadIsRefusedBeforeAnythingIsSent`, `aRowWrittenAsTheNumberAnotherElementIsNamedIsRefused`, `aRowAReadOfTheSameReplyChangedIsRefusedAndAnUnchangedOnePasses`, `aRowAfterTheCheckBetweenStepsGoesWithTheFreshTokenOnlyIfItIsTheSameElement`, `aDriverRefusalIsAFailedResultWithTheWindowReadAgainAndStopsTheChain`, `aReadThatFailsAfterAnActionSaysTheActionWasDone`, `onlyTheLastActionOfAReplyCarriesTheRead`, `theSameFailingComputerCallTheSecondTimeInARowSaysSo`, `requestsStayAppendOnlyBetweenBatchesAndOlderReadsAreOneLine`, `openWithAFileReadsThatFilesWindowOrSaysItIsNotThere`, `openWithNoWindowSaysHowToGetOneAndMenuOnTheAppReadsTheNewWindow`, `aWindowOnAnotherSpaceIsReadAndPressedAndAWindowWithoutATreeIsSkipped`, `aSystemAuthenticationWindowOrAPasswordFieldIsCalledOut`, `anAuthenticationWindowOrAPasswordSheetBetweenStepsStopsTheSequence`, `askOwnerDMsHimTheItemWaitsAndHisLineResumesIt`, `withoutAnItemInProgressTheComputerToolsAreRefused`, `doneNeedsEvidenceFromTheLatestReadAndARefusalShowsWhatItHolds`, `evidenceOfSeveralCopiedPiecesJoinedBySemicolonsOrLinesIsAccepted`, `herNextLineToTheOwnerCarriesTheLatestScreenshotOnce`, `aFarEstimateIsRefusedOnceWithWhatLikeListsTookAndACorrectedOnePasses`, `theEstimateIsNeverRefusedAThirdTime`, `everyActionIsOneLineOfTheActionLogWithTheElementsIdentity`, `herOneSessionStartsWithACursorThatNeverHidesIsOnEveryCallAndEndsAtExit`, `theDaemonIsStartedWhenDownStoppedAtTheEndAndAOneAlreadyRunningIsLeftAlone`, `withTheComputerOffHerToolsAreTheOthersAndAskOwnerStays`, `readFileNumbersLinesReadsARangeAndCapsAtTheConfiguredCount`, `aSavedChainIsBuiltFromWhatSheDidWithButtonNamesAsAParameter`, `theStepsBeforeAFailedStepAreKeptAndANameThatChangesEveryTimeIsKeptByItsRole`, `aMissingOrDoubledButtonStopsTheReplayWithoutPressingAnything`, `aToolRunChangesOnlyItsCountersAndMakesNoCommit`, `aToolThatKeepsFailingIsNoLongerOfferedOrRunButStaysForTheOwner`, `theOfferAttachTheCapDeletionAndARestartKeepTheToolsAndTheirCounts`.
