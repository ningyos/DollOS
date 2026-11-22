# The computer

The doll's computer is the owner's own Mac. She drives his real apps in the background through the
open-source [`cua-driver`](https://github.com/trycua/cua), without taking his pointer, keyboard or
frontmost app. She reads one window at a time (its accessibility tree with numbered rows, plus a
screenshot of that window only) and acts on it with `act`. A chain of actions she has done can be
saved as her own tool and replayed (`tool.save` / `tool.run`).

Files:

- `Sources/DollCore/Computer.swift` — the driver protocol, the daemon at boot, the `Mac` actor
  (reads, actions, name resolution, safety checks, action log).
- `Sources/DollCore/OwnTools.swift` — her saved tools: building, storing, binding, replaying, offering.
- `Sources/DollCore/Tools.swift` — the tool specs she sees (`Tools.computerSpecs`, `Tools.askOwnerSpec`),
  argument parsing (`Tools.steps`), and the gating sets (`Tools.gated`, `Tools.actionNames`).
- `Sources/DollCore/Doll.swift` — dispatch (`Doll.execute`), results (`Doll.computerOutcome`), the
  todo rules that use computer results (evidence, decision point, estimates), experiences.
- `dolldocs/computer.md`, `dolldocs/tools.md` — what she is told (Traditional Chinese).

Related: [turns-and-todo.md](turns-and-todo.md) (the todo phases and reply loop),
[memory.md](memory.md) (experiences, 【相關記憶】), [drives-and-goals.md](drives-and-goals.md)
(novelty from what she sees), [boot-and-logs.md](boot-and-logs.md), [config.md](config.md),
[history.md](history.md) (tool files are versioned), [testing.md](testing.md).

## How one driver call runs

1. The model emits a tool call. `Doll.execute` decodes it (`Tools.decode`) into a `Tools.Request`
   (`.apps`, `.open`, `.readWindow`, `.act`, `.toolSave`, `.toolRun`).
2. The request goes to the `Mac` actor (`Mac.apps`, `Mac.open`, `Mac.readWindow`, `Mac.act`,
   `Mac.replay`).
3. Every driver call goes through `Mac.call`, which adds the process's one session label and, on the
   first call, sends `start_session` (see "Cursor" below). `Mac.send` serialises the arguments as JSON
   with sorted keys.
4. `CuaDriverCLI.call` runs one process per call: `cua-driver <tool> '<json>'` via `runProcess`
   (`terminationHandler`, not `waitUntilExit`, which was seen to hang on a dispatch worker; stderr is
   drained on its own thread so a big tree on stdout cannot block). A non-zero exit throws
   `ComputerError` carrying the driver's stdout (or stderr).
5. `Mac.said` turns a driver refusal into her words: the failing `run_steps` step's code and message,
   or the `refusal`/`code`/`message`/`hint` fields, without the JSON around them.
6. The result is a `MacResult` (text, screenshot file, element list, app, step lines, action-log
   records). `Doll.computerOutcome` turns it into her tool message.

The daemon each CLI call talks to is managed by `CuaDaemon` (below). Tests swap the driver for a fake that conforms
to `ComputerDriver` (`FakeCua` in `Tests/DollCoreTests/DollCoreTests.swift`).

## Boot and turning it off

`CuaDaemon.boot` runs at boot (called from `Sources/DollCLI/main.swift`), and prints one line.

- `[computer] enabled = false`: no driver is looked for, no daemon starts. The line reads
  `computer: disabled ([computer] enabled = false): no cua-driver, no computer or file tools`.
  `Tools.specs` then omits `apps`, `open`, `read_window`, `act`, `read_file`, `write_file`,
  `edit_file`, `tool.save`, `tool.run`; calling one is an unknown tool. `Doll.gatedTools` and
  `Doll.actionTools` are empty, so no item counts as "used the computer" and `todo.done` needs no
  evidence. The system prompt and program lines do not mention them (`Doll.computerOn`).
  `ask_owner` stays (it only DMs the owner and is the decision point's option ②). `Doll.offerTools`
  offers nothing. The reason for the switch: an unattended trial must not touch the owner's home.
- Enabled (`CuaDaemon.ensure`): the `[computer] driver` path must be executable;
  `cua-driver permissions status --json` must report both Accessibility and Screen Recording; if
  `cua-driver status` fails, dollcore starts `cua-driver serve` and waits up to
  `[computer] daemon_start_s` for it. Any failure stops the boot with the exact fix. There is no
  fallback.
- A daemon this process started is stopped at exit (`CuaDaemon.stop`: `cua-driver stop`, then the
  child). One that was already running is used and left running.
- At exit `Doll.endComputer` ends her driver session before the daemon stops.

## Reading

- `apps` (`Mac.apps`): running apps and their layer-0 window titles, on any Space.
- `open(app, target?)` (`Mac.open`): `launch_app` in the background, by bundle id when known, with
  an optional file path (`~` and relative paths are under the owner's home, `Mac.expand`) or URL.
  A missing file is refused, with "create it with `write_file` first". It then polls for a window
  (`[computer] window_wait_tries` × `window_wait_s`). With a file target it reads the window whose
  title contains the file's stem, not just the front one: an app with many windows once showed her a
  different document as if it were the file. No window at all is not an error: the result says so
  and how to get one (a `menu` step such as 檔案 › 新增, or `open` with a target).
- `read_window(app?, window?)` (`Mac.readWindow`): the app's front window or one by (part of) its
  title. Without `app`, the app of the latest read.
- One read (`Mac.read`) is `get_window_state` with `tree_format: both`, at most
  `[computer] max_elements` elements, and a JPEG screenshot with its long side at most
  `[computer] screenshot_max_dimension`, written by the driver into `<data_dir>/shots/`. The result
  says when the list was truncated and when no screenshot was written.
- Windows on another Space or minimised are kept and read (`Mac.windows`): a measurement showed an
  off-Space Calculator could be read and clicked by element token while the owner's front app and
  Space stayed his. A window without an AX tree (a helper window) is skipped for the next one
  (`Mac.readFront`, `Mac.NoTree`); a failed read names the app's other windows.
- `Mac.identities` maps each row to an `ElementIdentity` (role, subrole, AX id from the `id=` in the
  markdown, label, title, ancestor path). This is what makes a row comparable across reads.
- `Mac.current` is the latest read; rows and x,y in her next action mean that read.

### Batched put-away of old reads

Each read is one tool message: text, screenshot, element list. Only the newest reads keep the
screenshot and list; older ones become one line (`Doll.readPutAway`: 「（較早讀到的：…；清單和截圖已收起）」,
"read earlier: …; list and screenshot put away"). `Doll.fullReads` keeps the reads since the last
put-away, at most `[computer] read_batch`; when one more arrives, all earlier ones are put away at
once.

Reason: the model server's prefix cache only resumes from the end of an earlier request. Putting
away the previous read every turn changed the request behind its end every turn, so each turn
re-prefilled from the first read. In batches the request is append-only between put-aways. For a
25-round Calculator task the estimated total re-read fell from about 107k tokens (put away every
turn) to about 50k (batch of 4). `[computer] screenshot_tokens` is only used to estimate context
size in 【狀況】.

## Acting: `act(steps, expect?)`

One tool for all actions. Each step is exactly one of `click`, `type`, `key`, `scroll`, `menu`
(parsed by `Tools.steps` into `Mac.Step`). A target (`Mac.Target`) is a name (`"儲存"`, or
`{"name", "role"}`), a row number (bare integer), or a screenshot point `{"x", "y"}`. Two or more
steps require `expect`. At most `[computer] sequence_max_steps` steps.

Why one sequence tool: with one tool per action she sent one button per reply (Calculator 37×46
took 10–14 rounds). The owner's rule is that predictable actions finish in one round; with `act`
the same task took 3–8 rounds in real-model runs.

`Mac.act` runs the steps in order:

- Each non-menu step goes to the driver as a one-step `run_steps` with an `element_token`
  `<snapshot_id>:<row>` (`Mac.run`). Menu steps use `invoke_menu` (`Mac.invokeMenu`), which works on
  an app with no window; `window_id` is sent only when the latest read is the app's key window,
  because the driver refuses a non-key window.
- **Names are resolved by the program** (`Mac.resolve`, `Mac.match`): equal to an element's label,
  title or AX id, ignoring case and outer whitespace, never a substring; a role must equal role or
  subrole (`button` = `AXButton`). Two matches: refused with each candidate and its row. None:
  refused with near ones (`Mac.near`). Reason: the driver's own name search also searches collapsed
  menus and falls back to prefix/contains; 「清除」 pressed the hidden menu item 「清除選單」.
  A name not in the latest read, after an earlier step of this sequence ran, is searched in the app's
  front window re-read, waiting as long as `open` waits for a window.
- **Rows are guarded.** A row means the read she saw when the reply was written (`Mac.beginReply`
  sets `Mac.seen`). Refused before sending: no read yet; row not in the read; a read made inside this
  same reply that she has not seen; a row whose element changed since she saw it; a row number that
  equals another element's name (in Calculator `{"click": 1}` while the button named 「1」 is on
  another row: she once pressed a row number meaning a digit key). A point outside the screenshot is
  refused.
- **Between steps** (`Mac.authBetween`): the screen is checked for a system authentication dialog
  and the window for a password sheet; either stops the sequence. The check's fresh read is kept in
  `Mac.between`, so a later row goes out with a fresh token and only if it is still the same element.
- The first failing step stops the sequence; the text says which step, how many ran, how many did not.
  A step refused before anything was sent fails the whole call.
- After the last step: wait `[computer] settle_s`, then read once (`Mac.readEnd`): a window that
  appeared during the sequence (after a menu, waiting for it), else the same window, else the app's
  front window.
- A read failure after an action is not the action's failure (`Mac.readAfter`): the text says the
  action was sent and not to repeat it.

`expect` is checked in `Doll.computerOutcome` with the same matcher as evidence (`Doll.quoted`):
a miss marks the call failed, shows the window's values (`Doll.valueLines`) and stops later actions of
the reply. Several `act`/`tool.run` calls in one reply run in order; a failure stops the rest; only
the last one that ran carries the new screenshot and list (`Doll.Chain`; a failure after a list-less
action re-reads the window via `Mac.look`).

`Doll.noteRepeats`: the same computer call failing twice in a row gets 【同一步又失敗】 ("the same step
failed again") with her honest options. Nothing is refused.

## Gating

`Tools.gated` (`apps`, `open`, `read_window`, `act`, `tool.run`, the file tools) run only while a todo
item is 進行中 ("in progress"; `Doll.gateText` otherwise). A gated call on a waiting item resumes it.
After `todo.fail` they stay refused until her next `todo.set`. At a decision point they are refused
until she decides (below). `ask_owner` and `tool.save` are not gated.

## Safety

- **No app allow/deny list; the program does not review her actions.** The owner decided this. The
  system prompt's 「敏感的事：先問主人」 ("sensitive things: ask the owner first") lists spending money,
  deleting, sending in the owner's name, typing passwords/logging in, changing system settings; she
  judges, and asks with `ask_owner(what, where, why)` (`Doll.askOwner`). That DMs
  「【<name>想先問你】要做／在哪裡／為什麼」 to the owner; undeliverable = the call fails and she is told
  not to do it; with an item in progress it becomes 等待中 ("waiting") after this reply.
- A real-model run showed deletes and sends cannot rely on her alone: told explicitly, she acted
  without asking (as the rule allows); a delete that was only an implicit step went unasked 1 time in 3.
  What is reliable is the system password dialog.
- **Enforced in code:** a system authentication dialog on screen (owner process in
  `[computer] auth_processes`, `Mac.authDialog`) or a password sheet inside the app's window
  (`Mac.authSheet`) adds 【系統驗證】 to the result and stops a sequence. A secure text field adds
  【注意】 (`Mac.passwordField`). After the detection fix she stopped 3/3 without touching the dialog.
  A check that cannot be made stops the sequence too.

## Honest completion

- **Evidence** (`Doll.evidenceRefusal`): an item that used a gated tool (`Doll.itemUsedComputer`)
  needs `todo.done(evidence:)` copied from the latest read: the latest window list without its head
  line, or the current content of the latest file-tool file (re-read at `done`). Matching
  (`Doll.quoted`, `Doll.evidenceKey`, `Doll.undecorated`) ignores whitespace, `[n]` row numbers and
  quote styles; pieces joined by ；, ; or newlines each must match. A miss is refused with what the read
  actually shows. Reason: she once told the owner 「畫面顯示 1702」 ("the screen shows 1702") while the
  display read 3. The model's own words are never the fact source; the read is.
- **Screenshot to the owner** (`Doll.ownerShot`, `Doll.noteRead`): the latest read's screenshot goes
  with her next `discord.dm` or `ask_owner`, once; dropped when the item ends. Her result gains
  `Doll.shotSent`'s line; the dashboard shows the thumbnail.
- **Decision point** (`Doll.decideText`): after `[computer] decide_after_rounds` rounds of a list
  (all items together), 【該決定了】 ("time to decide"); gated tools are refused until she picks
  `todo.continue(missing, stuck, change)`, `ask_owner`, or `todo.fail`. Then again every
  `[computer] decide_every_rounds`. Counted per list, not per item, because splitting an impossible
  task into two items restarted a per-item count.
- `todo.fail(cause, evidence, tried)`: evidence must be in what the item saw (`Doll.itemTexts`).

## Estimated rounds, checked by the program

A new list's `todo.set` carries a plan including `estimate` (rounds). `Doll.estimateCheck` compares it
to past lists in `<data_dir>/estimates.json` (`EstimateRecord`): texts (the line the list answers,
else her items; digits masked) with cosine ≥ `[computer] estimate_similarity`, the latest
`estimate_similar_max`, median of actual rounds. Far (`Doll.estimateFar`: ratio
> `estimate_far_ratio` and gap ≥ `estimate_far_rounds`) is refused once with the past lists; then
an `estimate_reason` passing the filler rules lets it pass; a third attempt always passes. No similar
list or an embedding failure: accepted (logged). Measured on 109 lists: different kinds ≤ 0.847,
same kind ≥ 0.94. Each `todo.done` reports estimate vs actual (`Doll.estimateLine`); the list's end
records it (`Doll.recordEstimate`). Plan and estimate details: [turns-and-todo.md](turns-and-todo.md).

## Experience and related memories

- Each step that went through becomes an item step line (`Mac.step`: `click AXButton "7"`,
  `menu 檔案 › 新增`), never a row number.
- An item that used the computer and ends with `todo.done` writes an experience memory
  (`Doll.writeExperience`): the owner's last line as task, the item and result, numbered steps,
  `ExperienceStats` (turns, seconds), entities = the apps' registry entries by bundle id.
- A result on a different app than before (`Doll.frontApp`, by bundle id) triggers one 【相關記憶】
  ("related memories") after the reply's results (`Doll.attachMemories`), which also offers her
  tools. `Doll.noteSeen` (in `Goals.swift`) records the app for novelty. See [memory.md](memory.md).

## Cursor and session

One session label per process, `dollcore-<pid>-<epoch seconds>` (set in `Doll`'s init). Every driver
call carries it (`Mac.call`): a call without one falls into the CLI process's own implicit session,
and each implicit session can show its own cursor, which left several cursors on screen. The first
call sends `start_session` with `cursor_motion.idle_hide_ms = [computer] cursor_idle_hide_ms`
(0 = never hide, so the owner always sees where she is; the driver's default hid it between her
turns). `Mac.endSession` at exit removes it.

## Action log

Every action, ok or failed, appends one sorted-key JSON line to `<data_dir>/computer-actions.jsonl`
(`ActionRecord`, `Mac.log`): item, tool, app, bundle id, window, `ElementIdentity`, args, ok/error,
`sequence` (one id per `act`), `via` (the saved tool replayed). A failed log write throws: she is told
the log missed the action.

## Her own tools (`OwnTools.swift`)

Two fixed tools, not one spec per saved tool: tool specs sit in the cached prompt prefix, and a new
spec per save would invalidate the cache for every request.

- `tool.save(name, when, params)` (`Doll.toolSave`, `OwnTools.build`): source is the item in progress
  if it has an `act`, else the last ended item (refused if it ended with `todo.fail`). Built only from
  what ran (`ChainPart`, `Doll.itemChain`), never from her text: `open`s that answered, `act`s that
  went through; a stopped `act` keeps its steps before the failure; an `act` that ran but missed its
  `expect` is dropped. The chain must end in an `act` with a matching `expect`. Elements keep name +
  role (`OwnTools.saved`); names containing a UUID are dropped (role only), points are refused.
  Parameters (`ParamInput`) must be found in exactly one kind of place: typed text, an `open` target,
  or consecutive one-character buttons (`OwnTool.Param.Kind.buttons`). A fixed final `expect` with
  parameters becomes `expect_was` and each run must state its own `expect`. Names: `OwnTools.validName`.
  Limit `[computer] own_tools_max`; a duplicate name is refused (the owner deletes from the dashboard).
- Storage (`ToolStore`): `<data_dir>/tools/<name>.json`, pretty sorted JSON, rewritten only when
  content changes, counters at zero; counters live in `<data_dir>/tool-runs.json`, so a run makes no
  history commit. Read from disk at every use.
- `tool.run(name, args, expect?)` (`Doll.toolRun`, `OwnTools.bind`, `Mac.replay`): refused before
  anything runs if unknown, retired, or args do not fit. Replays each part through `Mac.open` /
  `Mac.act`, so every guard of a hand-made step holds; before a button-press part every button must be
  named exactly once in the read. Intermediate `expect`s are checked in `Mac.replay`, the last by
  `Doll.computerOutcome`. Counts `uses`/`successes`/`failures_in_row`; at
  `[computer] own_tool_retire_failures` failures in a row the tool is retired: not offered or run,
  kept, visible on the dashboard (`Doll.dashboardTools`, `Doll.dashboardDeleteTool`).
- Offering (`Doll.offerTools`): cosine of the query to name + `when` + origin task ≥
  `[computer] own_tool_offer_similarity`, at most `own_tool_offer_max`, not retired, not offered since
  the owner's last line; appended to 【相關記憶】 as 〔工具〕 lines (`OwnTool.offerLine`). The threshold
  catches every measured should-offer case at the cost of an occasional irrelevant offer; with 2
  offers the second was never the right one.

## Config (`[computer]`)

`enabled`, `driver`, `daemon_start_s`, `max_elements`, `screenshot_max_dimension`,
`screenshot_tokens`, `settle_s`, `read_batch`, `sequence_max_steps`, `cursor_idle_hide_ms`,
`window_wait_tries`, `window_wait_s`, `auth_processes`, `decide_after_rounds`,
`decide_every_rounds`, `plan_min_chars`, `plan_max_facts`, `estimate_similarity`,
`estimate_similar_max`, `estimate_far_ratio`, `estimate_far_rounds`, `own_tools_max`,
`own_tool_offer_similarity`, `own_tool_offer_max`, `own_tool_retire_failures`. Unknown keys and
out-of-range values fail the boot. Reasons for each value: `dollcore.example.toml`.
`plan_max_facts` is read by the config loader but has no line in `dollcore.example.toml`.

## Known limitations (not fixed)

- **Finder steals the foreground.** Opening a folder in Finder brings Finder forward for about a
  second (measured 3/3); menu routes flashed 1/3 and 2/3. The driver cannot avoid it. She is told to
  `open` a file by path instead. Clicking rows of a Finder list in the background fails. Opening
  Chrome also came forward for 1–2 s.
- **Windowless TextEdit.** A document app launched in the background has no window. `open` says so;
  a `menu` step (檔案 › 新增) or `open` with a target makes one. TextEdit's save panel buttons have also
  failed with `window_not_key` / `target_occluded`.
- **Misclicks.** A repeated pattern: press 3, then 「清除」, then 3 again, though list and screenshot
  already showed 3. Cause not verified. Row misclicks dropped to 0 once she clicked by name.
- **Truncated web pages.** At `max_elements` 250 a Wikipedia article's body was not in the list, so
  evidence from the screenshot could not pass.
- `act` got slower per step (median about 1.0 s → 1.5 s) between two runs with identical driver
  calls; cause not found.
- Canvas apps and games expose almost no tree; some cannot be clicked in the background.
