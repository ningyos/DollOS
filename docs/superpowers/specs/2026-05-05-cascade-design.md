# Cascade — Design

**日期：** 2026-05-05
**狀態：** 草案（待使用者最終審閱）
**範圍：** Roadmap step 7 重切後 minimal scope —— 只做 **cascade**：tool 執行 fail → 同一 turn 內大模型重新進入 stream，看到自己呼叫的失敗訊息並修正。**不做**原 roadmap 寫的 reflex、review、`reflex_calls`、`approved_calls`、`continue_thread`、wake gating（reflex 延後到自己的 research+brainstorm；review 砍掉）。
**對齊：**
- `2026-05-05-event-loop-design.md`（步 4 dispatcher per-event task）
- `2026-05-05-tool-calling-design.md`（步 6 ToolStreamParser、`Say`/`NoteMemory`、`_dispatch_tool_call`）
- `docs/research/agent-loop-frameworks-2026-05-05.md`（推薦 Option 3 — refined asyncio，inner while-loop cascade）

---

## §1 設計原則

1. **Cascade = 同一 task 內 inner while-loop**。不 spawn 新 task、不走 `dispatch()`。對齊 Anthropic / OpenAI / smolagents / pydantic-ai 的主流形狀。Step 4 的 per-event 並行（多 client）保留——不同 client 不同 task，並發。
2. **只 cascade fail，不 cascade success**。Step 7 的兩個 tool（`Say` / `NoteMemory`）成功執行後 turn 結束；只有失敗才繼續 loop 讓 Doll 修正。Future step 9 加 returning tool（search / subagent result）時再加 success-cascade 機制。
3. **Tool fail 透明化**。Step 6 對 validation / unknown tool 是 silent skip + log。Step 7 改成**透過 cascade 把 fail 訊息餵回 Doll**——她看得到自己的呼叫失敗了，可以修正 args / 換 tool / 放棄。
4. **「沒達到目的」靠 system prompt 引導**。系統不嘗試判斷 Doll「達到目的」與否——加一句 prompt 元規則：「如果嘗試多次仍未達目的，考慮換方法、嘗試不同 tool、或停止 (不再 call tool)」。Doll 自決。
5. **iteration count 進 perception 自然語言**。每次 cascade re-invoke，perception 含「（這是 thread 的第 N 次重試）」。讓 Doll 自決 backtrack / 換方法 / 放棄。
6. **Doll 自決停止**。某輪 0 fail tool calls → 自然退出 while-loop → 推 TurnEnd。
7. **Hard sanity cap = 50**。純 runaway 防護（buggy model 跑飛時 daemon 不撐爆）。觸到 cap → ErrorMsg + TurnEnd 結束 turn。日常使用永遠觸不到。
8. **No reflex, no review, no Instinct 介入** ——整 step 不動 Instinct / Inner Voice。

---

## §2 Cascade flow

```
UserTextEvent → _handle (one task)
                   ↓
                _respond (inner while-loop):
                   iteration = 0
                   loop:
                     stream 大模型, parse tool_calls, execute each via _dispatch_tool_call
                     fails: list[ToolCallFailure]  ← 收集本輪 fail
                     if fails empty:
                       break  ← Doll 自決停止
                     iteration += 1
                     if iteration > MAX_CASCADE_DEPTH:
                       sink.put(ErrorMsg); break
                     # 把 fails 揉成自然語言 perception，重 perceive + summary，再 stream
                     doll_event = DollEvent(perception=_format_fail_perception(fails, iteration), raw=...)
                     summary = await self._instinct.process(doll_event)
                   sink.put(TurnEnd())
                   sink.put(None)  ← sentinel
```

**單一 task、單一 sink、零 race**。

未來 step 9 subagent 真需要「async result 後續再喚醒大模型」時——那不是 cascade，是新 RawEvent（`SubagentResultEvent`）走 dispatch。Step 7 不引入這個。

---

## §3 `ToolCallFailure` dataclass（dispatcher internal）

```python
# src/dollos/dispatcher.py
from dataclasses import dataclass

@dataclass
class ToolCallFailure:
    """Tool call could not execute. Internal only — not a RawEvent."""

    tool_name: str    # 大模型 emit 的 name（可能是 unknown / typo）
    error: str        # 人類可讀的錯誤描述（"unknown tool" / pydantic msg / exception repr）
```

**不是 RawEvent**。純粹 dispatcher 內部用來在一輪 stream 結束後 build cascade perception。

---

## §4 `_dispatch_tool_call` 改回傳 `ToolCallFailure | None`

```python
async def _dispatch_tool_call(
    self, call: dict, ctx: ToolCtx
) -> ToolCallFailure | None:
    name = call.get("name")
    if not isinstance(name, str):
        return ToolCallFailure(tool_name=str(name), error="missing or non-string 'name'")
    tool_cls = self._tools_by_name.get(name)
    if tool_cls is None:
        return ToolCallFailure(tool_name=name, error="unknown tool")
    try:
        tool = tool_cls.model_validate(call.get("arguments", {}))
    except ValidationError as e:
        return ToolCallFailure(tool_name=name, error=f"args validation: {e}")
    try:
        await tool.run(ctx)
    except Exception as e:
        logger.exception("tool %s raised", name)
        ctx.sink.put_nowait(ErrorMsg(message=f"tool {name} error: {e}"))
        return ToolCallFailure(tool_name=name, error=f"runtime error: {e}")
    return None  # success
```

Step 6 的「log warning + skip」行為合併進 `ToolCallFailure` 回傳。**runtime error** 同時推 `ErrorMsg` 進 sink（因為使用者該知道有錯）+ 回 failure 觸發 cascade。

---

## §5 `_respond` 重寫

```python
MAX_CASCADE_DEPTH = 50


async def _respond(
    self,
    doll_event: DollEvent,
    summary: str,
    sink: asyncio.Queue[ServerMessage | None],
) -> None:
    iteration = 0
    while True:
        recall = await self._inner_voice.recall(doll_event.perception)
        system = self._renderer.render(
            "scaffolding", character=self._character_profile
        )
        state_block = f"STATE:\n{summary}\n\n" if summary else ""
        prefill = f"{state_block}{recall}DECISION: "

        parser = ToolStreamParser()
        ctx = ToolCtx(
            sink=sink,
            memory_root=self._memory_root,
            memsearch=self._memsearch,
        )
        fails: list[ToolCallFailure] = []

        async for chunk in self._adapter.stream_completion(
            system=system,
            user=doll_event.perception,
            prefill=prefill,
            tools=TOOLS,
        ):
            for call in parser.feed(chunk.text):
                fail = await self._dispatch_tool_call(call, ctx)
                if fail is not None:
                    fails.append(fail)
            if chunk.done:
                break
        for call in parser.flush():
            fail = await self._dispatch_tool_call(call, ctx)
            if fail is not None:
                fails.append(fail)

        if not fails:
            break  # Doll 自決停止 / 全部 success / 空輪

        iteration += 1
        if iteration > MAX_CASCADE_DEPTH:
            sink.put_nowait(ErrorMsg(
                message=f"cascade exceeded MAX_CASCADE_DEPTH ({MAX_CASCADE_DEPTH})"
            ))
            break

        # 重新 perceive + summary 為下一輪
        doll_event = DollEvent(
            perception=self._format_fail_perception(fails, iteration),
            raw=doll_event.raw,
        )
        summary = await self._instinct.process(doll_event)

    sink.put_nowait(TurnEnd())


@staticmethod
def _format_fail_perception(
    fails: list[ToolCallFailure], iteration: int
) -> str:
    lines = [f"你 call 了 {f.tool_name} tool 失敗：{f.error}" for f in fails]
    lines.append(f"（這是 thread 的第 {iteration} 次重試）")
    return "\n".join(lines)
```

**注意**：`_handle` 仍像 step 6 一樣 finally 推 `None` sentinel。`_respond` 不推 sentinel（只推 TurnEnd / ErrorMsg）。

---

## §6 `scaffolding.jinja` 加一句

`src/dollos/prompts/templates/scaffolding.jinja` 末尾（character 之後）加：

```jinja
{{ character }}

如果你嘗試多次仍未達到目的，考慮換方法、嘗試不同 tool、或停止 (不再 call tool)。
```

這條是 **agent 行為元規則**，不是 character 屬性，也不是 tool calling 機制。Step 10 character pack 來時，character.jinja 覆寫 scaffolding 時要注意保留這段（或挪到主 spec 共通層）。

---

## §7 失敗模式

| 情境 | 處理 |
|---|---|
| Tool call args validation fail | `ToolCallFailure` 進 cascade fails；不 silent skip |
| Unknown tool name | `ToolCallFailure` 進 cascade fails |
| Tool `.run()` raise | 推 `ErrorMsg` 給 user + `ToolCallFailure` 進 cascade fails（user 看到 + Doll 看到） |
| 大模型一輪沒 emit tool call | fails 為空 → 跳出 loop → TurnEnd（自然結束） |
| Cascade depth 超 50 | `ErrorMsg` + TurnEnd（強制結束） |
| 大模型 stream 中斷（adapter raise） | 沿用 step 4 既有 `_handle` except 路徑（ErrorMsg + sentinel） |
| Inner Voice / recall 拋例外 | 同上 — `_handle` except 包住 |

---

## §8 不做的（明確 out-of-scope）

- ❌ Reflex（rule-match → whitelist tool）— 延後到自己的 research+brainstorm
- ❌ Review（Instinct 審大模型 calls）— 架構衝突，砍掉
- ❌ `reflex_calls` / `approved_calls` / `continue_thread` 欄位
- ❌ Wake gating（每 event 仍喚大模型）
- ❌ Success-cascade（Tool 成功不回大模型；step 9 returning tool 來時再加）
- ❌ `ToolExecutedEvent` as RawEvent（cascade 走 inner loop，不走 queue）
- ❌ `_sink_locks` dict（不 spawn 新 task → 不需要鎖）
- ❌ MAX_ITERATIONS as backstop concept；改 MAX_CASCADE_DEPTH 純 runaway 防護
- ❌ Doll 並行 tool execute（stream-order serial，沿用 step 6）

---

## §9 已知限制 / Follow-ups

1. **Cascade per-iteration 重做 recall**。每 cascade re-invoke 都重 recall + 重 summary。recall 撈相同 query 結果差不多——浪費 small-model call。Follow-up：cache recall per turn 或讓 perception query 演化。
2. **Multi-fail 一次摺起來**。`_format_fail_perception` 把同一輪所有 fails 摺進一個 perception。極端情況（10 個 fail）perception 很長。Follow-up：truncate / 摘要。
3. **沒 success cascade**。任何「需要 tool 結果回流大模型」的功能（search / web_fetch / spawn_subagent）都要等加 success-cascade。
4. **MAX_CASCADE_DEPTH = 50 hardcoded**。Follow-up：放 config（`config.toml [dispatcher] max_cascade_depth`）。
5. **Step 9 subagent 的 async result**：那是新 RawEvent（`SubagentResultEvent`）走 `dispatch()`，**不是 cascade**。Spec 預告但本 step 不實作。
6. **Reflex 真的需要嗎**：等 reflex research+brainstorm 才決定。可能根本不做（wake gating + 大模型 Say tool 已涵蓋大部分情境）。

---

## §10 Tests

### `tests/test_dispatcher.py`（擴）

1. **單輪無 fail → turn 結束**：fake adapter yield Say tool_call → 看到 TextChunk + TurnEnd，無 cascade。
2. **單輪有 fail → cascade 到下一輪**：第一輪 yield unknown tool → fake adapter 第二輪 yield Say tool_call。驗證：第二輪 adapter call 的 user message 含「失敗：unknown tool」+「第 1 次重試」。
3. **連續多輪 fail**：模擬 3 輪都 fail，驗證 perception 含「第 N 次重試」累加。
4. **MAX_CASCADE_DEPTH 觸發**：fake adapter 永遠 yield 同一個 unknown tool → cascade 到 51 輪 → ErrorMsg + TurnEnd。
5. **Tool runtime error**：fake tool raise → 同時 ErrorMsg 進 sink + cascade fail perception 給下一輪。
6. **Multi-fail 一輪**：fake adapter 一輪 yield 兩個 unknown tools → 下一輪 perception 含兩條 fail 訊息 + iteration count = 1。
7. **無 cascade 時行為跟 step 6 一致**：既有 step 6 tests 仍綠（行為向後相容）。

### `tests/test_e2e.py`（擴）

完整 trace：UserTextEvent → 大模型第一輪 emit invalid tool args → 第二輪 emit valid Say → user 端收到 TextChunk + TurnEnd。

---

## §11 Demo 驗證

打字「請寫進記憶」→
1. 大模型第一輪 emit `<tool_call>{"name":"NoteMmory","arguments":{"text":"主人想記事"}}</tool_call>`（typo）
2. Dispatcher 偵測 unknown tool「NoteMmory」→ ToolCallFailure
3. 第二輪 prefill 含 perception「你 call 了 NoteMmory tool 失敗：unknown tool（這是 thread 的第 1 次重試）」
4. 大模型修正 → emit `<tool_call>{"name":"NoteMemory",...}</tool_call>`
5. 成功執行 → fails=[]→ TurnEnd

User 端從頭到尾沒看到 typo / 重試過程，只看到最終 Say 輸出（如果有）+ TurnEnd。

**驗證點**：
- IPC 序列乾淨（不洩漏中間 cascade iteration）
- `data/memory/shared/{today}.md` 確實多一行（最終成功的 NoteMemory）
- daemon log 看得到「unknown tool: NoteMmory」warning
- Doll 自決何時停（不再 emit tool call → loop 結束）
