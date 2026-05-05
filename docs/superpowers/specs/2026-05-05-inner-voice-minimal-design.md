# Inner Voice (minimal) — Design

**日期：** 2026-05-05
**狀態：** 草案（待使用者最終審閱）
**範圍：** Roadmap step 5 — 引入 `Instinct` ABC + `SmallModelInstinct`，每 event 一次小模型 call 產 **rolling summary**，summary 進大模型 prefill 的 STATE 區塊。**只做 summary**——`first_instinct` / `emotion` 都不做（YAGNI）。
**對齊主 spec：**
- `2026-05-01-dollos-pivot-to-computer-design.md`（§4 Inner Voice / §8 Self-First）
- `2026-05-05-event-loop-design.md`（step 4 — `_perceive` stub、`EventDispatcher`、prefill 結構，本 plan 在其上加 STATE block）

---

## §1 設計原則

1. **System 1 / System 2 雙層架構**。小模型 = System 1（always-on、reactive、reflex）；大模型 = System 2（on-wake、deliberative）。step 5 在 System 1 加上「working memory」職能（rolling summary），summary 是 System 1 對 System 2 唯一合法的「資訊通道」之一。
2. **YAGNI**。roadmap 原寫的 `first_instinct + emotion + summary` 三項，step 5 砍到只剩 summary：
   - `first_instinct` 沒有 step 5 消費者（reflex 在 step 7），管道留到 step 7 真用時再開
   - `emotion` 交給大模型 think 自己處理，理由：情緒可被 deliberate，是 System 2 的事
3. **Self-First**：summary 是 System 1 維持的「持續存在感」，注入 prefill 是讓 System 2 *觀察* 自己的內在狀態，不是 prompt 寫死「你應該有這種情緒」。
4. **In-memory state**。`_last_summary` 純 in-memory，restart 後歸零。持久化留 follow-up。
5. **Step 4 `_perceive` stub 不動**。roadmap 寫「step 4 _perceive stub 換成真 perceive」這部分縮到只 own summary——perception 留下個 step 處理非文字 event 時再升級。step 5 的 perception 仍是 passthrough text。

---

## §2 認知架構（System 1 / System 2 邊界）

| 角色 | System 1（小模型，Inner Voice / Instinct） | System 2（大模型，Doll） |
|---|---|---|
| 何時跑 | always-on，每 event 一次 | on-wake（step 5：每 event 都 wake；step 7+ wake gating）|
| 職責 | 文本偵測、perception、rolling summary、reflex 規則匹配（step 7）、recall（已存在）、wake gating（未來）| Deliberate response、tool calling、reflect on emotion / self-state |
| 對 System 2 的合法通道 | ✅ summary（進 prefill STATE）/ ✅ recall（進 prefill RECALL）/ ✅ ToolExecutedEvent（reflex 動過了 → cascade）/ ✅ wake decision | n/a |
| **不該** verbalize 給 System 2 | first_instinct（衝動屬 System 1 內部，由 reflex action 表現，不對 System 2 報告）；emotion（屬 System 2 deliberation 範疇）| n/a |

step 5 只實作這張表的一格：**summary 通道**。其他通道 step 4 已有（recall）或留給未來 step（ToolExecutedEvent / wake / reflex）。

---

## §3 Instinct 介面

```python
# src/dollos/instinct.py
from abc import ABC, abstractmethod
from dollos.events import DollEvent


class Instinct(ABC):
    """Per-event small-model preprocessing layer (System 1)."""

    @abstractmethod
    async def process(self, event: DollEvent) -> str:
        """Return updated rolling summary for this event.

        Implementations may maintain in-memory state across calls.
        Returned string becomes the STATE block in big-model prefill.
        Empty string means "no STATE block" (caller skips injection).
        """
```

### `SmallModelInstinct(Instinct)`

```python
class SmallModelInstinct(Instinct):
    def __init__(
        self,
        adapter: LLMAdapter,           # small-model adapter (CPU Qwen3.5-0.8B)
        renderer: PromptRenderer,
    ) -> None:
        self._adapter = adapter
        self._renderer = renderer
        self._last_summary = ""

    async def process(self, event: DollEvent) -> str:
        blocks = self._renderer.render_blocks(
            "iv_summary",
            prev_summary=self._last_summary,
            perception=event.perception,
        )
        chunks: list[str] = []
        async for chunk in self._adapter.stream_completion(
            system=blocks["system"],
            user=blocks["user"],
            prefill="",
        ):
            if chunk.text:
                chunks.append(chunk.text)
            if chunk.done:
                break
        self._last_summary = "".join(chunks).strip()
        return self._last_summary
```

`SmallModelInstinct` 沿用 step 4 已有的 small-model adapter（CPU Qwen3.5-0.8B + `Qwen3PlainTemplate`，per step 4 IV thinking-revert 決定）。**不開 thinking**——小模型在 CPU 上 think 會永遠收不掉 `</think>`，step 4 已驗證。

---

## §4 Prompt template `prompts/iv_summary.jinja`

```jinja
{%- block system -%}
You are Doll's inner voice. Maintain a continuous summary of what is happening
in Doll's interaction. The summary is Doll's working memory across events.

Rules:
- Output ONLY the new summary as plain prose, 1–3 sentences.
- Carry forward relevant context from the previous summary.
- Drop details that are no longer load-bearing.
- Do NOT add commentary, headers, or bullets.
- Do NOT roleplay. You are not Doll; you are her working memory.
- If the new perception adds nothing meaningful, return the previous summary unchanged.
{%- endblock -%}

{%- block user -%}
Previous summary:
{{ prev_summary if prev_summary else "(none — this is the first event)" }}

New perception:
{{ perception }}
{%- endblock -%}
```

格式：純文字（無 bullet），1–3 句。理由：summary 進 prefill 的 STATE 區塊要簡潔；bullet 形式跟 RECALL 視覺重複；散文形式對大模型 think 友善。

---

## §5 EventDispatcher 接線

```python
# src/dollos/dispatcher.py — relevant changes
class EventDispatcher:
    def __init__(
        self,
        *,
        adapter: LLMAdapter,
        inner_voice: InnerVoice,
        instinct: Instinct,            # NEW
        renderer: PromptRenderer,
        character_profile: str,
    ) -> None:
        ...
        self._instinct = instinct      # NEW

    async def _handle(self, raw: RawEvent) -> None:
        ...
        try:
            doll_event = await self._perceive(raw)
            summary = await self._instinct.process(doll_event)    # NEW
            await self._respond(doll_event, summary, sink)        # signature change
        except ...

    async def _respond(
        self,
        doll_event: DollEvent,
        summary: str,                                              # NEW
        sink: asyncio.Queue[ServerMessage | None],
    ) -> None:
        recall = await self._inner_voice.recall(doll_event.perception)
        system = self._renderer.render(
            "scaffolding", character=self._character_profile
        )
        state_block = f"STATE:\n{summary}\n\n" if summary else ""  # NEW
        prefill = f"{state_block}{recall}DECISION: "
        ...
```

### Prefill shape change

| 階段 | Prefill |
|---|---|
| step 4（現況）| `<think>\n{recall}DECISION: ` |
| step 5 (summary 非空) | `<think>\nSTATE:\n{summary}\n\n{recall}DECISION: ` |
| step 5 (首次 event，summary 空) | `<think>\n{recall}DECISION: `（同 step 4）|

`<think>\n` 前綴由 `Qwen3ThinkingTemplate` 注入（step 4 既有），dispatcher 給的 prefill 是 `<think>\n` 之後的內容。

### STATE 在 RECALL 前的理由

- STATE = Doll 當下內在狀態（self），比 RECALL（外部 memory）更貼近主體
- DECISION 在做決定時先看「我現在是誰／怎麼了」再看「我記得什麼」
- 跟 Self-First 一致

---

## §6 Kernel wiring

`src/dollos/kernel.py`：
- 新增 factory（或直接在 `Kernel.__init__` 裡組）：用既有的 small-model `LLMAdapter` + `PromptRenderer` 構造 `SmallModelInstinct`
- `EventDispatcher` 構造時傳 `instinct=...`

不新增 config 欄位。沿用 step 4 已有的：
- 小模型 adapter（`config.small_llm.*`）
- `PromptRenderer`（templates 目錄）

---

## §7 Tests

### `tests/test_instinct.py`（新）

1. **Instinct ABC 不可直接實例化**：`Instinct()` raises `TypeError`。
2. **SmallModelInstinct.process()**:
   - 第一次 call：`prev_summary` 用 `(none — this is the first event)`、輸出寫進 `_last_summary`、回傳同字串
   - 第二次 call：`prev_summary` = 上次回傳值
   - 用 fake `LLMAdapter`（yield 固定 chunks）+ fake `PromptRenderer`，驗證 prompt 內容含 prev_summary + perception
3. **空輸出 edge**：adapter yield 空 chunks → `_last_summary` = `""`，`process` 回傳 `""`。

### `tests/test_dispatcher.py`（擴充）

1. `_handle` 呼叫 `instinct.process(doll_event)` 一次。
2. `_respond` 收到非空 summary → prefill 含 `STATE:\n{summary}\n\n` 在 `RECALL:` 之前。
3. `_respond` 收到空 summary → prefill 不含 `STATE:` 字串（與 step 4 行為一致）。
4. instinct 拋例外 → 走 `_handle` 的 `except Exception` 分支，sink 收 ErrorMsg + None sentinel（沿用 step 4 流程）。

### `tests/test_e2e.py`（擴充）

完整 trace：UserTextEvent → instinct.process → adapter prompt 含 STATE block。Mock 大小模型；驗證大模型收到的 prefill string 結構。

---

## §8 Edge cases

| 情境 | 行為 |
|---|---|
| 首次 event（`_last_summary` = ""）| template 顯示 `(none — this is the first event)`；輸出寫進 state |
| 小模型 timeout / error | 例外冒到 `_handle`，沿用 step 4 ErrorMsg 流程；`_last_summary` 不更新 |
| 小模型回空字串 | summary = ""，prefill 跳過 STATE block；下次 event 從空繼續 |
| 並發 events（兩個 client 同時打字）| 兩個 task 各自 await `instinct.process` → `SmallModelInstinct` 內部 `_last_summary` **有 race**（見 §10）|
| restart | `_last_summary` 歸零（in-memory only）|

---

## §9 不做的（明確 out-of-scope）

- ❌ `first_instinct`、`emotion`（§1.2）
- ❌ Wake gating（每 event 都跑 System 2，跟 step 4 一致）
- ❌ Reflex（step 7）
- ❌ Summary 持久化 / restart 恢復
- ❌ 兩階段 perceive/process 拆分（step 5 perception 仍 stub）
- ❌ Instinct 內部用 tools / 撈 memory（撈 memory 是 InnerVoice.recall 的事，留在大模型 prefill 路徑）
- ❌ Summary truncation / token budget（短 summary 模板自然限制 1–3 句；正式 budget 留 follow-up）
- ❌ Multi-character / character-scoped instinct（step 10）

---

## §10 已知限制 / Follow-ups

1. **`_last_summary` race**。並發 events 下兩個 `process` call 同時讀寫 `_last_summary`：A 讀 prev=S0、B 讀 prev=S0、A 寫 S1、B 寫 S2 → S1 丟失。step 5 接受這個 race（單機單使用者場景罕見；丟一次 summary 不影響功能）。Follow-up：用 `asyncio.Lock` 或改成「每 event 從 perception 重生 summary 不依賴 prev」（但後者違反 rolling 精神）。
2. **Summary 持久化**。restart 後失憶。Follow-up：寫進 daily summary markdown 或單獨 state file。
3. **Summary 多人 / 多角色 scoping**。目前單一 `_last_summary`。Multi-character（step 10）需要 per-character state。
4. **Token budget**。1–3 句靠模板自律；模型不聽話可能爆。Follow-up：grammar 限制 / 截斷。
5. **Latency 觀察**。step 4 已驗證 IV plain ~3s（CPU Qwen3.5-0.8B Q4）；summary template 應同量級。實測後決定要不要為 step 5 切換更小模型 / GPU。

---

## §11 Demo 驗證

打字「我等等想喝咖啡」→
1. perception = `"我等等想喝咖啡"`
2. instinct.process → `_last_summary` 變 `"主人提到等等想喝咖啡。"`
3. 大模型 prefill 含 `STATE:\n主人提到等等想喝咖啡。\n\nRECALL:\n...\nDECISION: `
4. 大模型回應自然帶上下文

第二輪「那我先去燒水」→
1. perception = `"那我先去燒水"`
2. instinct.process（prev = "主人提到等等想喝咖啡。"）→ `"主人在準備喝咖啡，剛剛說要去燒水。"`
3. 大模型 prefill 含新 STATE，能延續話題

**驗證點**：連續對話下 summary 合理累積、大模型回應有「我們剛在聊咖啡」的延續感。
