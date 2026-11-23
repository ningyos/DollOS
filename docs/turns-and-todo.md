# Turns and the todo list

The core is one process (`dollcore`) holding one `Doll` actor: one event queue, one consumer loop, one timeline (the day's JSONL segment). A **turn** is one model request plus all its tool calls. The doll gets a turn only when an event is queued or her own tool results wait for her; no timer wakes her. Her todo list's phase decides when she gets turns, which tools run, and when finished work is compacted.

## What the doll sees and does

- One turn = one reply. Text outside tool calls is her private thought; nobody sees it. She acts only through tools, and **every reply must call at least one tool**. Calls run in order, each with one result.
- A person's line arrives as `[14:32 主人｜Discord 私訊｜文字]` (time, who, channel, 語音 or 文字). The developer types from a terminal; he is not the owner ([secrets.md](secrets.md)).
- She answers where the line came from: `discord.dm`, `voice.say` ([voice-and-bridge.md](voice-and-bridge.md)), or `dollctl.reply` for the developer. No other way to speak exists.
- `think(level: "medium")` makes the next request think first, once.
- The last message of every turn is **【狀況】**, facts only: date, time, weekday, todo, today's calendar, owner sleep, maintenance, context used against the limit (and the current item's share), next turn's effort, and near the limit one advice line.

### The todo list

Before working she lists results with `todo.set`. One item is one result the person wants, written as what "done" looks like; open, edit, save and check are steps of one item. One item at a time, no nesting.

| Phase | Label | Turns | Computer and file tools |
|---|---|---|---|
| `idle` | 待命中 | on events and her tool results | refused (「失敗：…先用 todo.set 列 todo」) |
| `doing` | 進行中 | one after every reply | run |
| `waiting` | 等待中 | on events only | a gated or todo call takes the item up again |
| `done` | 完成 | transient: next item or 待命中 | — |
| `failed` | 失敗 | one turn to decide | refused until `todo.set` |

- `todo.set(items, facts, assumptions, limits, estimate, estimate_reason, check, subgoal?)`: a new list needs a plan; each fact has a `quote` from what she read or was told. Filler (「沒有」「同上」, a copy of the items) or an unfound quote is refused; empty limits get 「limits「無」沒寫出邊界：…」. During an item it replaces only items not yet started; `items: []` ends the list. `subgoal`: [drives-and-goals.md](drives-and-goals.md).
- `todo.done(result, evidence?)`: refused with no real action yet (「…這一項你還沒做任何動作，也沒看過結果，不能說完成。…」). After computer use, `evidence` is quoted from the latest read ([computer.md](computer.md)). The last item of a list a person asked for is refused until she told that person, on their channel, in a reply after the `todo.set` one (「還沒完成：這份清單是主人交代的，你列 todo 以後還沒跟主人說過話；…」). She tells the result in the same reply, before `todo.done`.
- `todo.fail(cause, evidence, tried)`: only when sure. Next turn she rewrites the rest, lists it again, or ends with `items: []` after telling the owner.
- `todo.wait(reason)`, or `ask_owner` during an item: the item waits; an event alone does not resume it.
- `todo.progress(note)`: when 【狀況】 says context is nearly full; compacts the item so far, the item stays 進行中.
- After `todo.done` / `todo.fail` / `todo.progress`, the rest of the reply is refused except speaking tools. After a refused `todo.set`, later computer and file calls in that reply are refused.

**Compaction.** When an item ends, its process (calls, results, screenshots, thoughts) leaves her view; her `result`, the program's action list and every line to and from people stay. So `result` is written for her future self. `recall` still finds compacted events. If she ignores the near-full warning for `[context] force_after_turns` turns, the program compacts all but her last `[context] force_keep_turns` turns and appends 【收起】; her progress note is lost. At the limit, the oldest part is replaced by a summary starting 【之前的對話】, which she did not write.

**Decision point.** After `[computer] decide_after_rounds` replies on one list, the program appends 【該決定了】 and refuses computer and file tools until she chooses `todo.continue(missing, stuck, change)` (`change` must be new; the owner sees the reasons), `ask_owner`, or `todo.fail`. The next point is `[computer] decide_every_rounds` later.

**Waiting is not busy** (with `[drives]`). While a list waits, `todo.set` with `subgoal` starts her own list and sets the waiting one aside; at most one list is aside. Her list ending brings it back. A person's line puts the waiting list in hand for that turn; if she does not take it up, her list returns. In that turn her ending or `todo.continue` call is held once, since it may be meant for her own list.

**Replies without a call.** During an item the program appends 「【提醒】你上一則回覆沒有呼叫工具，所以什麼都沒發生。…」 and gives another turn. With no item and a person's line unanswered, she is reminded once per line. After `[idle] max_turns_without_tool` such replies in a row, no turn until an event.

## How it works

`Doll.run` loops: `Doll.waitForEvent` (queue non-empty or `followUp` set, not paused, not in maintenance), `Doll.drain`, `Doll.turn`, then `flushHeld`, `maintenanceCheck`, `drivesCheck` ([maintenance.md](maintenance.md)).

**Events.** `Doll.put` queues `Doll.ownerSaid`, `Doll.developerSaid` (never secret, never cancels), `Doll.voiceNotice`, calendar starts and wandering. `Doll.drain` sorts a batch by `[core] priority` through `Event.priority`. Only owner voice cancels the request in flight (`Doll.hold`); the turn reruns with the new line and the same effort. A dashboard `Doll.interrupt` cancels without a rerun. `Doll.ownerLine` (`SecretCheck.swift`) writes each owner line sealed with `Inbox.put` to `<data_dir>/inbox/<ms>-<serial>.line` (temp, `fsync`, `rename`) before the API answers; `Doll.checkAndQueue` checks lines in arrival order ([secrets.md](secrets.md)); `Doll.drain` removes the file once the event is in the timeline, and `Doll.boot` requeues leftovers. A kill can duplicate a line, never lose one.

**A turn** (`Doll.turn`): on a person's line `attachMemories` and `associate` ([memory.md](memory.md)); `Doll.forceCompact` / `Doll.compactTalk`; `Doll.context` + `Doll.footer` → `model.chat(messages, tools: toolsJSON, effort:)`; store the reply with its footer (`Timeline.attachFooter`); `Doll.execute` each call; apply compaction, todo changes, decision point (`Doll.recordTodo`); log `turn N …` and write a `turn` record (`Timeline.recordTurn`, with `finish_reason`, completion tokens, reasoning characters). `finish_reason == "length"` throws `main model hit max_tokens`.

**System prompt** (`Doll.systemPrompt`): `你是<name>。`, pack identity ([config.md](config.md)), ideal self and habits ([reflection.md](reflection.md)), `Instructions.text`, `Instructions.voice` if `[voice] enabled`, then `fixedPrompt` (`Fixed.swift`): unsealed summaries of the last `[context] summary_days` days, deep public memories (≤ `[context] deep_max`), saved tools, and every `.md` in `[core] docs` (README first). `Doll.refreshFixed` rebuilds it at boot and when a maintenance ends; a failure keeps the old one and logs ERROR. 【狀況】 and `[context] limit` count only what lies on top of it. With `[context] manual = false` there is no fixed part and the instructions point to the files for `read_file`.

**Rendering** (`Doll.render`): person lines with `Doll.speakerTag`; replies as assistant messages with `tool_calls`, after the footer they answered; results as `role: "tool"` in call order; notices as `[<time> 程式] …`. A call-less reply goes as a user line prefixed `[她那回合只想了，沒呼叫工具] ` (`Doll.thoughtOnlyTag`): replayed call-less assistant turns made the model answer the owner with empty replies. In a developer turn secret events keep their shape with content `Doll.hiddenSecret`.

**Prefix stability.** The prefix must stay byte-identical for the server's cache: replies and their footers are re-sent exactly as stored, `tools` (`Tools.specs` / `Tools.json`) is built once per process, screenshots come from stored bytes, and older reads are put away in batches of `[computer] read_batch` (`Doll.fullReads`).

**Calls.** Only the response's `tool_calls` count; `ToolCall.unread` turns an undeclared tool into 「沒有 X 這個工具，沒有執行」 and a leftover opener into `ToolCall.unreadable`, with a WARN. `Tools.decode` is strict ([tool-failures.md](tool-failures.md)). `Doll.execute` never throws. `Tools.gated` (`apps open read_window act tool.run read_file write_file edit_file`) runs only while an item is 進行中 and no decision is pending (`Doll.gateText`). `Tools.ending` = `todo.done todo.fail todo.progress`; `Tools.speaking` = `discord.dm dollctl.reply voice.say`; `Tools.actionNames` (`act`, `tool.run`) stop at the first failure. A `background` result (a line sent, `todo.wait`) gives no turn; any other sets `followUp`. Speaking and `ask_owner` pass `Traditional.fix` (characters Big5 cannot encode become ICU Hant; `么` → `麼` except after `老`) and `Doll.checkClaims`; `Tools.Request.inTraditional` converts her kept texts, not quotes.

**Todo state.** `TodoState` (`Todo.swift`) is stored as `todo` records after each change, sealed while derived from secrets (`todoSealed`); in-memory parts (`itemSteps`, `itemChain`, `latestRead`, `listFor`) start empty after a restart. Plans: `Doll.checkedPlan`, `Doll.citable`, `Doll.estimateCheck` ([computer.md](computer.md)). `todo.done`: `Doll.itemActed`, `Doll.evidenceRefusal`, `listReported`. Rounds: `TodoState.rounds` / `totalRounds` against `decideAt`; `Doll.decideText`, `Doll.decided`. Counting per list, not per item, stops a split list from running past the point. Aside lists: `TodoAside.swift` (`Doll.setAside`, `takeBackAside`, `swapAside`, `heldTools`); each list keeps its rounds, plan, estimate record and goal link.

**Compaction records** (`Timeline.compact`) hide a span `(fromSeq, toSeq]` from the request: `done` / `failed` / `progress`; `forced` (`Doll.forceCompact`); `talk` (`Doll.compactTalk`: the oldest `[context] talk_share` becomes one rolling summary from `Doll.talkInstructions`, no persona, no tools, effort `[context] talk_effort`; cut just before a person's line, never inside an item 進行中; `Doll.contextPlan` keeps a waiting list's todo calls and its line; a failed summary logs ERROR and retries). `Doll.survivesCompaction` keeps speaking and todo calls with results and person lines; `Doll.workNotice` lines (【相關記憶】【快捷鍵】【提醒】) go. Tokens are `estimateTokens` scaled by `Doll.tokenRatio`.

**Effort** (`Doll.nextEffort`): `think`'s level for one turn; else `[llm] task_turn_effort` on a task turn (`Doll.taskTurn`: a person line, a wandering, her tool results), because at `none` real runs stopped mid-task; else `[llm] default_effort`.

**Outages.** `HTTPMainModel.send` repeats the HTTP request only for connection-class failures (`Retry.kind`: `URLError` connection lost, timed out, cannot connect, not connected, cannot find host, DNS; HTTP 502/503/504), waiting `[llm] retry_first_s` doubling to `retry_max_s`, one WARN per failure, one INFO on recovery. `ModelConnection` shows the outage on the dashboard and `dollctl status`. Any other failure throws `ModelError` and exits. Tools never run twice. The boot check `MainModel.check` is not retried. Reason: a sleeping laptop once dropped the connection and queued owner lines were lost.

## Config

- `[core] priority` — `owner_voice owner_text developer_text calendar watch wander`; drain order.
- `[llm] default_effort` / `task_turn_effort` — `none` / `medium`.
- `[llm] timeout_s` — `300`; also bounds the secret check.
- `[llm] max_tokens` — `4096`.
- `[llm] retry_first_s` / `retry_max_s` — `5` / `120`.
- `[context] limit` — `60000` tokens above the fixed part; `warn_ratio` `0.85`.
- `[context] force_after_turns` / `force_keep_turns` — `3` / `2`.
- `[context] talk_share` / `talk_effort` — `0.5` / `medium`.
- `[context] manual` / `summary_days` / `deep_max` — `true` / `7` / `40`; the fixed part.
- `[computer] decide_after_rounds` / `decide_every_rounds` — `15` / `10`.
- `[computer] plan_min_chars` / `plan_max_facts` — `4` / `5`.
- `[computer] read_batch` — `4`.
- `[idle] max_turns_without_tool` — `3`.

## Invariants

- No event and no pending results → no turn.
- Every call gets exactly one result.
- The prefix is byte-stable between fixed-part rebuilds.
- An owner line is on disk, sealed, before it is acknowledged.
- At most one list is aside.

## Known limitations

- The `watch` priority has no producer (no `watch` tool, no `Event.Kind.watch` creator); it stays so configs load, and `Event.priority` files queued program notices under it.
- Context sizes are heuristics corrected by one ratio (1.0 on a process's first turn).

## Tests

`noEventMeansNoTurn`, `drainOrdersByConfiguredPriority`, `ownerVoiceCancelsTheCallAndTheRerunSeesTheVoiceWithTheSameEffort`, `aSecretLineLeftInTheInboxStaysSealedAndIsCheckedAgainAtBoot`, `aPastToolRoundRendersByteIdenticallyOnLaterTurnsImagesIncluded`, `callsTheServerReturnedAsTextBecomeFailedCalls`, `aRefusedTodoSetStopsTheToolRunInTheSameReply`, `doneCompactsTheItemKeepsTheConversationAndMovesOn`, `failBlocksTheComputerUntilSheDecidesWhatIsLeft`, `waitingGetsNoTurnsUntilSomethingHappens`, `herOwnStepsEndingCallInTheTurnHisLineBroughtTheWaitingListIsHeldAndTodoWaitGivesHerStepBack`, `theRoundCountSurvivesACompactionAndARestart`, `nearFullWithoutHerCompactionTheProgramForcesItKeepingTheLastTurns`, `atTheLimitAListWaitingAsideKeepsItsPartAndHerOwnStepInProgressIsNotCutInto`, `idleTurnsWithoutAToolCallWriteOneLogLine`, `taskTurnsUseTaskTurnEffortAndOnlyAnEmptyTurnTheDefault`, `aTurnWaitsOutALostConnectionItsToolsRunOnceAndALineDuringItIsAnsweredAfter`, `whatSheSaysGoesOutInTraditionalCharacters`, `withTheComputerOnTheToolsAndPromptAreByteIdenticalToBeforeTheSwitch` (pins SHA-256 of tools and instruction texts; change those bytes deliberately and update the digests).
