# Tool failures

A failed tool call never throws out of the turn: its result text starts with 「失敗：」, then the cause, usually followed by what the doll can do now. She follows that advice instead of repeating the same call unchanged. This file lists the common failure texts outside the computer tools, and the claims check that holds back her lines.

## What the doll sees and does

### A line that did not arrive

- `discord.dm`: 「失敗：主人沒收到：Discord 送不出去：bridge …。這段話主人沒看到；Discord 連上之前 discord.dm 都傳不出去，連上以後再傳一次。」 The bridge is down or disconnected. It does not restart by itself; the developer fixes it and restarts DollOS ([voice-and-bridge.md](voice-and-bridge.md)).
- `dollctl.reply`: 「失敗：開發者沒收到：沒有開著的 dollctl chat。開發者開著 dollctl chat 才收得到 dollctl.reply；要跟主人說話用 discord.dm。」
- `ask_owner` when Discord is down: 「失敗：主人沒收到：…。Discord 連上之前問不了；先不要做那件事。」
- These failures give her no extra turn.

### A claim with no action behind it

Before `discord.dm`, `dollctl.reply`, `voice.say` or `ask_owner` sends, the program looks for a claim that she just did something to the calendar or her memories (「查過行事曆」「查了下週日程」「改了記憶」). Such a claim needs a successful call of the matching kind since the person's line she is answering:

- calendar: any `calendar.*` (`calendar.list`, `calendar.add`, `calendar.change`, `calendar.delete`);
- memory: `recall`, `remember` or `memory.fix`.

Calls earlier in the same reply count; calls before that line and failed calls do not. An unbacked claim stops the whole line:

「失敗：沒有送出，主人什麼都沒收到。你寫了「查過行事曆」，可是從主人上一句話到現在，你沒有用過行事曆的工具（calendar.list、calendar.add、calendar.change、calendar.delete）。現在要做就先呼叫它，看了結果再說；是之前做的就講明什麼時候（例如「之前查過」「早上查的」）；不然把這句拿掉。改好整段再傳一次。」

She fixes and resends the whole line; this result gives her a turn. Not a claim: a dated action (之前, 早上, 昨天, 上次…, or a clock time like 21:14 earlier in the sentence), a plan (再, 會, 要, 打算… in the same clause), a negation (沒查, 還沒加), or the owner's own action (你改了行事曆). 「剛」「剛才」 mean now and are checked.

### A malformed call

- 「這個工具呼叫寫壞了，伺服器讀不出是哪個工具、什麼參數，沒有執行。要做的事請重新呼叫一次工具。」: call again.
- 「沒有 X 這個工具」: the name does not exist (often an old name).
- 「X 沒有 Y 這個參數（它的參數：…）」, 「X 少了參數 Y」, 「X 的 Y 是空的」, 「X 的 Y 要是整數，收到 …」, 「X 的 Y 只能是 …，收到 …」: fix the parameter as stated.
- `remember`: 「remember 的 items 要是 JSON 陣列，每條 {"text": …, "folder": "secret"/"own"/"public", "kind": 可不填}；讀不懂：…」 and 「folder 只能是 secret、own、public，收到 …」.

### Computer tools

Failures of `apps`, `open`, `read_window`, `act` and `tool.run` (row refusals, cua-driver codes, 【同一步又失敗】, 【系統驗證】) are in [computer.md](computer.md). In one reply, actions after the first failure return 「失敗：前面第 N 步（…）失敗了，這一步沒做」.

### File tools

- 「… 不是 UTF-8 文字檔（N bytes），read_file、write_file、edit_file 只處理文字檔」.
- 「沒有這個檔案：…」, 「… 是資料夾，不是檔案」, 「沒有權限讀 …」.
- `read_file` returns at most `[files] max_read_lines` lines and ends with 「還有 N 行（read_file 加 from: M 讀下去）」.
- `edit_file` with `old` not found: 「edit_file …：old 在檔案裡出現 0 次，沒改。old 要跟檔案裡一字不差（含空白、換行、標點）；…」 followed by the closest lines.
- `old` found more than once: 「…old 在檔案裡出現 N 次，不知道改哪一個，沒改。old 多帶幾行前後文，讓它只出現一次」.

## How it works

- `Doll.execute` returns every error as result text with the `失敗：` prefix. Delivery failures come from `LocalAPI.deliver`; parameter errors from `Tools.decode`. Todo refusals: [turns-and-todo.md](turns-and-todo.md).
- Claims (`Claims.swift`): `Doll.checkClaims` runs after `Traditional.fix`. `Doll.doneSinceLine` lists her calls since the latest owner or developer line, skipping malformed calls and results starting `失敗`. `Claims.find` works per sentence (split at 。！？!? and newline): a `verbs` word, then within `reach` characters of its clause (ending at ，,；;：:、) an object word. Skip rules: an `earlier` word or clock time before the verb in the sentence; a `planned` word in the verb's clause; a `negations` word right before the verb; `Claims.ownerDid` (an `owner` word after the last 我, not right after a `for_owner` character). A refused line is logged at INFO and not delivered.
- Conservative by design: a missed false claim costs nothing new, a refused true line costs a round. The window starts at the line she answers because she once cited a check made for an earlier question as "just checked".

## Config

- `[claims] enabled` — `true`; off sends lines unchecked.
- `[claims] verbs` — `查過 查了 查好 看過 看了 確認過 確認了 記好 記了 加好 加了 改好 改了 刪了 刪掉`; only forms that mark the action done (「加進」「記下」 are promises).
- `[claims] calendar_words` — `行事曆 日程 行程 提醒`.
- `[claims] memory_words` — `記憶`.
- `[claims] reach` — `6`; most characters between verb and object word.
- `[claims] earlier` — `之前 早上 上午 下午 昨天 昨晚 前天 上次 上週 稍早 那時 當時 早就`.
- `[claims] planned` — `再 會 要 等 打算 可以 準備`.
- `[claims] negations` — `沒 不 別 沒有`.
- `[claims] owner` — `你 主人`.
- `[claims] for_owner` — `幫 替 給 跟 讓 叫 陪`.
- `[files] max_read_lines` — `500`.

## Invariants

- Every failed call yields one result starting with 「失敗：」; no failure is silent.
- A line refused by the claims check reaches nobody.

## Known limitations

- Only calendar and memory claims are checked.
- Claims whose object word comes before the verb (「記憶加了好幾條」) or further than `reach` are missed.

## Tests

`herClaimWithNoCallSinceTheOwnersLineIsNotSentAndSheIsToldWhy`, `herTwoUntrueClaimsInTheSoakAreRefusedAndNoTrueLineIs`, `anActionInAnEarlierReplyOrEarlierInTheSameReplyBacksTheClaim`, `aClaimDatedEarlierNegatedOrTheOwnersIsNoClaim`, `aClaimDatedByAClockTimeIsNoClaim`, `aPromiseToAddToTheCalendarIsNoClaim`, `callsTheServerReturnedAsTextBecomeFailedCalls`, `readFileNumbersLinesReadsARangeAndCapsAtTheConfiguredCount`.
