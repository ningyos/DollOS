# Turns and the todo list

The core is one process (`dollcore`) holding one `Doll` actor: one in-memory event queue, one
consumer loop, one timeline (the day's JSONL segment). Durable state is the timeline and memory
files; SQLite only holds indexes (see [memory.md](memory.md)). A **turn** is one model request,
taken only when the queue holds an event or her tool results wait for her. No timer wakes her.

## The loop

`Doll.run` does, per iteration:

1. `Doll.waitForEvent`: sleep until the queue is non-empty or `Doll.followUp` is set, and she is
   neither paused (dashboard) nor in maintenance. `Doll.stop` ends the loop.
2. `Doll.drain`: move queued events into the timeline.
3. `Doll.turn`: one model request and all its tool calls.
4. `flushHeld` (program notices held during the turn), `maintenanceCheck`, `drivesCheck`
   (see [maintenance.md](maintenance.md), [drives-and-goals.md](drives-and-goals.md)).

### Events and priority

Inputs call `Doll.put`, which appends to `Doll.queue` and resumes the waiter:

- `Doll.ownerSaid`: the owner's text or voice (normally reached through `Doll.ownerLine`, below).
- `Doll.developerSaid`: a line typed in `dollctl chat`. Never secret, never cancels a request.
- `Doll.voiceNotice`: program notices from the voice channel.
- Calendar starts and her wandering come from their own modules.

`Doll.drain` sorts the batch by `[core] priority` (highest first; stable within one source) and
appends each event with `Timeline.append`. The ranking comes from `Event.priority`, which maps an
event to a `Priority` case. `Config` rejects a `priority` list that does not name every
`Priority` case exactly once.

The `watch` slot has no producer: there is no `watch` tool, nothing creates `Event.Kind.watch`, and
comments that mention "watch wakes" are stale. The slot stays so configs load; `Event.priority`
files queued program notices (`.memory` other than a wandering) under it.

Owner **voice** is the only input that cancels the request in flight (`Doll.ownerSaid`,
`Doll.hold`); the turn drains again (after `Doll.awaitHeldLines`) and reruns with the new line. A
dashboard `Doll.interrupt` cancels without a rerun.

## The owner's line is durable before it is acknowledged

`Doll.ownerLine` (in `SecretCheck.swift`) receives the owner's lines from the local API and voice:

1. `Inbox.put` writes the line as one `SecretBox` box to `<data_dir>/inbox/<received ms>-<serial>.line`
   (temp name, `fsync`, `rename`), sealed before the secret check has answered.
2. Only then does `ownerLine` return and the API answer; a failed write answers 500.
3. `Doll.hold` chains `Doll.checkAndQueue`: lines are checked (`SecretCheck.ask`) and queued in
   arrival order. A check that fails or exceeds `[llm] timeout_s` treats the line as secret.
4. `Doll.drain` deletes the file once the event is in the timeline (`Inbox.remove`); `Doll.boot`
   re-checks and queues leftovers (`Inbox.pending`). A kill between append and removal duplicates
   the line; it is never lost. Developer lines do not use the inbox.

## One turn

1. If a person's line arrived (`ownerArrived`), attach memories (`attachMemories`, see
   [memory.md](memory.md)); run concern association (`associate`).
2. `Doll.forceCompact` when warned too often, then `Doll.compactTalk` at the context limit.
3. Build `Doll.context` (system prompt + rendered timeline) plus the 【狀況】 footer (`Doll.footer`)
   as the last user message; call `model.chat(messages, tools: toolsJSON, effort:)`.
4. Append her reply as a `.doll` event with its footer (`Timeline.attachFooter`); run every call
   in order (`Doll.execute`), one `.tool` result event each, right after the reply.
5. Apply requested compaction, todo changes and the decision point; `Doll.recordTodo`.
6. Decide the next turn (todo phase), handle a call-less reply, log one `turn N …` line, report a
   `TurnReport`, write a `turn` record (`Timeline.recordTurn`, `Timeline.TurnStats`).

### What a turn sees

- **System prompt** (`Doll.systemPrompt`): `你是<name>。`, the character pack identity, her ideal
  self (only with `[drives]`), then `Instructions.text` (how she works: where turns come from, the
  todo states, compaction, which tool answers which source, that text outside calls is her own
  thought) and `Instructions.voice` when `[voice] enabled`. It names the absolute path of
  `[core] docs` for the rest (`dolldocs/`).
- **Tools**: `Doll.tools` / `Doll.toolsJSON`, built once per process by `Tools.specs` and
  `Tools.json`, sent as the request's `tools`. The chat template renders them ahead of the system
  text.
- **The timeline**, rendered by `Doll.render`:
  - owner/developer lines as `[<speaker tag>] text`. `Doll.speakerTag` builds
    `14:32 主人｜Discord 私訊｜文字` (time, who, channel, spoken 語音 or typed 文字);
  - her replies as assistant messages with `tool_calls`, preceded by the footer they answered;
  - tool results as `role: "tool"` messages in call order (no call ids; the template pairs by
    position). A computer read carries its screenshot and element list (see
    [computer.md](computer.md));
  - program notices as `[<time> 程式] …`.
- **【狀況】** (status block), `Doll.footer`: now with year and weekday (`nowLine`), the todo line
  (`Doll.todoLine` + `asideNote`), the felt line and goals (drives), today's calendar, owner sleep,
  maintenance lines, context used/limit and how much is the current item's process, the next
  turn's effort, and near the limit one advice line. It holds facts only.

In a developer turn (`Doll.audience` is `.developer`) each secret event keeps its place and call
names but its content becomes `Doll.hiddenSecret`. See [secrets.md](secrets.md).

### Prefix stability

The prefix must be byte-identical turn to turn (the model server's prefix cache depends on it):
her reply is stored and re-sent exactly as returned (text plus each call's `arguments` text); the
footer it answered is stored with it (`footer` record, not an event) and re-sent before it; `tools`
never changes during a process (an ideal-self revision costs one prefix miss); screenshots are
re-sent from stored bytes, and older reads are put away in batches of `[computer] read_batch`
(`Doll.fullReads`).

## Tool calls

Calls come from the response's `tool_calls`. The text content of a reply is her private thought;
nobody receives it. She reaches people only through the speaking tools (`Tools.speaking`:
`discord.dm`, `dollctl.reply`, `voice.say`).

**Why the text parser was removed.** Tool definitions used to live in the system prompt and DollOS
parsed calls from her text, with resend and rescue paths. A real-model measurement with `tools` in
the request gave 131 of 131 readable calls, so the parser and its retries were deleted.

Unreadable calls are never dropped silently: `ToolCall.unread` turns a rejected undeclared tool
into `沒有 X 這個工具，沒有執行` and each leftover opener (`ToolCall.markers`) into
`ToolCall.unreadable`, with a WARN line. A timeline whose replies lack `calls` fails to load.

`Tools.decode` is strict: unknown tool, unknown parameter (the error lists the real ones), missing
required parameter, empty text, bad enum value or non-integer each throw a `ToolError`. Tool names
are written once in `Tools.Name`.

**Execution rules** in `Doll.turn` / `Doll.execute`:

- Every call gets one result. Failures become result text starting `失敗：`; `execute` never throws.
- Gated tools (`Tools.gated`: the computer and file tools) run only while an item is 進行中 and no
  decision is pending. Otherwise they are refused with what to do first (`Doll.gateText`).
- After an ending call (`Tools.ending`: `todo.done`, `todo.fail`, `todo.progress`) the remaining
  calls of the reply are refused, except speaking tools. Words written after the end still go out.
- Chained computer actions (`Tools.actionNames`) stop at the first failure; later ones are skipped.
- A refused `todo.set` makes the gated calls after it in the same reply refused too.
- A result marked `background` (a line that went out, `todo.wait`) does not by itself give her
  another turn. Any other result sets `followUp`.

### Speaking: Traditional conversion and the claims check

For `discord.dm`, `dollctl.reply`, `voice.say`, and `ask_owner`:

1. **Traditional characters** (`Traditional.fix`): a CJK character Big5 cannot encode is taken as
   simplified and replaced by ICU's Hans→Hant form; shared characters stay, so traditional text is
   never changed; `么` becomes `麼` except after `老`. Her result then gains `Traditional.note`.
   Reason: about 6% of her real texts carried simplified characters despite the persona.
   `Tools.Request.inTraditional` converts kept texts (memories, goal nodes, concerns, ideal self),
   not quotes that must match existing text.
2. **Claims check** (`Doll.checkClaims` → `Claims.refusal`). `Claims.find` looks for a
   `[claims] verbs` verb followed within `[claims] reach` characters, same clause, by a
   `calendar_words` or `memory_words` word; it skips dated (`earlier`), planned (`planned`),
   negated (`negations`) and owner-done (`owner`, `for_owner`) claims. Each claim needs a backing
   call in `Doll.doneSinceLine` (her non-`失敗` calls since the person line she answers, earlier
   calls of this reply included): `calendar.*`, or `recall`/`remember`/`memory.fix`. Otherwise the
   line is **not sent** and her result names the tools to call. Conservative by design: a missed
   false claim costs nothing new; a refused true line costs a round. The window is "since the line
   she answers" because she once cited a check made for an earlier question as "just checked".

## Effort

Each request carries `reasoning_effort` (`Doll.nextEffort`):

- `effortNext` after `think(level: "medium")`, for one turn (`high` was removed: the template
  rewrote the prompt top and re-read everything);
- else `[llm] task_turn_effort` when `Doll.taskTurn` is set (a person line, a wandering, or her own
  tool results): at `none`, real runs often stopped mid-task after one sentence;
- else `[llm] default_effort`.

## The todo list

Todo = goals. Before doing anything she lists what "done" looks like (`todo.set`). One item at a
time, no nesting. The phase (`TodoPhase`, `Todo.swift`) controls turns, gated tools, and
compaction. Labels are what she sees:

| Phase | Label | Turns | Gated tools |
|---|---|---|---|
| `idle` | 待命中 | only events and her own tool results | refused |
| `doing` | 進行中 | one after every reply | run |
| `waiting` | 等待中 | only events and her own tool results; none after the reply that made it wait | a gated call takes the item up again (→ 進行中) |
| `done` | 完成 | transient: next item → 進行中, none → 待命中 (never stored) | — |
| `failed` | 失敗 | one turn to decide | refused until `todo.set` |

Transitions:

- `todo.set(items, …)` from idle/failed starts a list. A list that starts hands in a plan
  (`PlanInput` → `TodoPlan`: facts each with a quote, assumptions, limits, a round estimate, a
  check). `Doll.checkedPlan` rejects filler and facts whose quote is not in what she read or was
  told (`Doll.citable`); `Doll.estimateCheck` compares the estimate with like past lists in
  `<data_dir>/estimates.json`. While doing/waiting, `todo.set` only replaces the pending items. An
  empty `todo.set` ends the list.
- `todo.wait`, and `ask_owner` during an item: 等待中 after the reply's calls ran. A gated call or a
  todo call on the waiting item takes it up again (`Doll.resumeWaiting`). An event alone does not.
- `todo.done(result, evidence)`: refused unless the item saw a real action (`Doll.itemActed`);
  with the computer used, `evidence` must be quoted from the latest read (`Doll.evidenceRefusal`);
  the last item of a list that a person asked for must have been reported to that person
  (`listFor`, `listReported`, or a speaking call later in the same reply). Then the item is
  compacted and an experience may be written (see [memory.md](memory.md)).
- `todo.fail(cause, evidence, tried)`: only when sure; compacted; no automatic next item.
- `todo.progress(note)`: compacts the item so far; the item stays 進行中.

`TodoState` is stored after every change (`todo` records), so a restart continues the list. Its
in-memory parts (`itemSteps`, `itemChain`, `latestRead`, `listFor`) start empty in a new process.
Todo records are sealed while the list derives from secret content (`todoSealed`).

### The aside list

`TodoAside.swift`. Waiting is not busy: a list 等待中 counts as idle for her wandering
(`Doll.idleForWander`). A `todo.set` with a `subgoal` while the list in hand waits starts her own
list and sets the waiting one aside (`Doll.setAside`, `ListAside`). At most two lists exist: one in
hand, one aside. In-memory state goes with each list (`ListMemory`).

- Her own list ends → the aside list is back in hand (`Doll.takeBackAside`).
- A person's line or a watch wake while the waiting list is aside → it is put in hand for that
  turn (`Doll.drain`, `swapAside`, `swappedForLine`). If she takes it up, her own list waits aside.
  If not, her list is back in hand after the turn.
- In that turn, her `todo.done` / `todo.fail` / `todo.progress` / `todo.continue` is held once
  (`heldForAnswer`, `Doll.heldTools`), because it may be meant for her own list. The same call
  next turn goes to the waiting item; `todo.wait` there returns her own list.

Each list keeps its own rounds, decision point, plan, estimate record and goal link. Rejected
alternatives: no lists while waiting (wandering could think but not act), unlimited stacking
(orphan lists), ending her list when the owner speaks (half-done work lost).

### The decision point

Rounds count her replies written while an item is 進行中 (`TodoState.rounds`, summed over the list
as `TodoState.totalRounds`). When `totalRounds` reaches `TodoState.decideAt` (`[computer]
decide_after_rounds` from the list's start), the program appends `【該決定了】` ("time to decide")
with `Doll.decideText` and sets `TodoState.deciding`. Gated tools are refused until she chooses:

1. `todo.continue(missing, stuck, change)`: `change` may not repeat an earlier one. The reasons are
   kept in `TodoState.goOn` and are visible to the owner.
2. `ask_owner`.
3. `todo.fail`.

After a choice (`Doll.decided`) the next point is `[computer] decide_every_rounds` later. The count
is per list, not per item: a per-item count let a split list run far past the intended point.

## Compaction and the context limit

`Compaction` records (`Timeline.compact`) remove a span `(fromSeq, toSeq]` from the request; the
events stay in the timeline file, and `recall` still finds them. Kinds:

- `done` / `failed` / `progress`: her todo call; span from the item's start to the turn's last
  event.
- `forced`: `Doll.forceCompact`. 【狀況】 warns from `[context] warn_ratio` of `[context] limit`
  (advice `.compact` when the item's process is at least a quarter of the usage, `.conversation`
  otherwise). After `[context] force_after_turns` warned turns without `todo.progress`, the program
  compacts all but her last `[context] force_keep_turns` turns and adds a `【收起】` block with the
  action list.
- `talk`: `Doll.compactTalk`. At `[context] limit`, the oldest `[context] talk_share` of the request,
  conversation included, is replaced by one rolling summary written by a separate request
  (`Doll.talkInstructions`, no persona, no tools, effort `[context] talk_effort`). The cut sits just
  before a person's line and never inside an item 進行中 (in hand or aside). A waiting list does not
  bound it; `Doll.heldLists` / `Doll.contextPlan` keep that list's own todo calls and the line it
  answers. A failed summary request logs ERROR and is retried next turn.

In a compacted span, `Doll.survivesCompaction` keeps her speaking and todo calls (no thought, no
footer) with their results; person lines stay verbatim; work notices (`Doll.workNotice`:
`【相關記憶】`, `【快捷鍵】`, `【提醒】`) go. Token figures are estimates (`estimateTokens`) scaled
by the last real `prompt_tokens` (`Doll.tokenRatio`).

## Replies without tool calls

A call-less reply is **not** re-sent as an assistant turn: `Doll.render` sends its text as a user
line prefixed `Doll.thoughtOnlyTag` ("she only thought that turn"); an empty one sends nothing.
Reason: in a replayed real request, earlier call-less assistant turns made the owner's greeting get
an empty reply 5 of 5 times, 0 of 5 with this rendering. The model imitates call-less runs.

After a call-less reply (`Doll.idleTurns` counts them):

- `[idle] max_turns_without_tool` in a row: one log line, no further turn until an event.
- An item 進行中: append `Doll.noCallReminder` (`【提醒】`) and give another turn.
- No item, and a person line has had no line of hers since (`Doll.unansweredLine`): append
  `Doll.unansweredReminder` once per line.

Why the model stopped is recorded per turn: `ModelUsage.finish` (`finish_reason`),
`completionTokens`, and `reasoningChars` go to the log (`ModelUsage.stopLine`) and the `turn` record.
`finish_reason == "length"` is an error (`main model hit max_tokens`).

## Staying alive when the model is unreachable

`HTTPMainModel.send` repeats the same HTTP request until the server answers, for connection-class
failures only (`Retry.kind`): `URLError` `networkConnectionLost`, `timedOut`, `cannotConnectToHost`,
`notConnectedToInternet`, `cannotFindHost`, `dnsLookupFailed`, and HTTP 502/503/504. Waits follow
`Retry.wait`: `[llm] retry_first_s`, doubling, capped at `[llm] retry_max_s`. Each failure logs one
WARN; recovery logs one INFO; neither contains message text. `ModelConnection` tracks the outage for
the dashboard and `dollctl status`.

Every other failure throws `ModelError` and the process exits: no fallback. Only the HTTP exchange
repeats, so her tool calls never run twice; cancellation ends the wait. All model requests use
this path (turns, summaries, maintenance, the secret check, which still gives up after
`[llm] timeout_s`). The boot check (`MainModel.check`) is not retried. Reason: a sleeping laptop
dropped the connection, the process exited, and queued owner lines were lost.

## Pinned tool list and prompt

`withTheComputerOnTheToolsAndPromptAreByteIdenticalToBeforeTheSwitch` in
`Tests/DollCoreTests/DollCoreTests.swift` hashes (SHA-256, via `enabledRequestDigests`) the output
of `Tools.json(Tools.specs(…))` for every voice/drives combination, and `Instructions.text` for every
voice/drives/maintenance/owner-sleep combination plus `Instructions.voice`, against the example
config. Any change to a tool description, schema, order, or prompt sentence fails the test, by
design: these bytes are her prompt prefix and her behavior. Change them deliberately, update both
digests, and say in the comment what changed. Computer-off variants are checked by word lists
(`computerOnly`, `computerWords`).

## Known limitations

- The claims check covers calendar and memory claims only.
- Context sizes are heuristics corrected by one ratio (1.0 on a process's first turn).
