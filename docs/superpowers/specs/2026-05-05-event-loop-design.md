# Event Loop — Design

**日期：** 2026-05-05
**狀態：** 草案（待使用者最終審閱）
**範圍：** Roadmap step 4 — 把 `kernel._handle_text_input` 從「同步 call recall + LLM」改成「event 進 dispatcher，並行 task 處理」的結構。對外行為（IPC 訊息序列）不變。同時引入 **兩層 event model**（RawEvent / DollEvent）為 step 5 Inner Voice 接入鋪路。
**對齊主 spec：**
- `2026-05-01-dollos-pivot-to-computer-design.md`（§3 Event Loop / §4 Inner Voice / §6 Event 系統）
- `2026-05-03-vom-integration-design.md`（step 3 — `_handle_text_input` 邏輯本 plan 要 lift 進並行 dispatcher）

---

## §1 設計原則（為什麼這樣做）

1. **多並行 + 單一狀態**。機器物理特性：可平行；資源限制是 LLM 單次 call 的 latency。Doll 的「單一狀態」不靠序列化執行達成（那是人腦物理限制），而靠單一 Memory + 單一 S 達成。
2. **事件流是 LLM 的 user 角色**。chat template 的 `user`/`assistant` alternation 對應 event/response 的 alternation。Doll 永遠是「對最新感知做出反應」，不是「跟某個人類對話」。
3. **兩層 event model**：原始事件層（結構化、分類）↔ Doll 事件層（自然語言、Inner Voice 翻譯後的「我感知到 X」）。Doll（大模型）只看自然語言層，但 perception 中**包含 source 語意**（例：「主人在手機上說 X」），所以 Doll 仍能依來源不同做不同反應。
4. **不做的（step 4 不在範圍）**：KV cache reuse / slot persistence / interrupt / 並發 cap / S lock（S 還沒人寫）/ Inner Voice 真實 perceive（step 5）/ history 持久化結構。

---

## §2 兩層 Event Model

```
原始事件層（structured）           Inner Voice perceive            Doll 事件層（natural language）
──────────────────────              ──────────────────              ──────────────────────────────
class RawEvent(ABC)                                                  @dataclass DollEvent
   ├── UserTextEvent          ──▶  perceive(raw)              ──▶      perception: str
   │     text, response_sink                                            raw: RawEvent
   ├── (future) VoiceInputEvent
   ├── (future) TimerFiredEvent
   ├── (future) ToolResultEvent
   ├── (future) DroneResultEvent
   ├── (future) SubagentResultEvent
   ├── (future) SystemEvent
   └── (future) DollSelfPokeEvent
```

### 角色對應

| Chat role | 過去（chatbot 模型） | DollOS（event 模型） |
|---|---|---|
| `system` | 「你是 helpful assistant」 | character profile（**我是誰**） |
| `user` | 人類使用者的話 | **DollEvent.perception** — Inner Voice 翻譯後的「我剛感知到什麼」 |
| `assistant` | 助手回應 | Doll 對該 event 的反應（step 6+ tool calls；step 4 純文字） |
| prefill | 通常空 | `<think>` 內：RECALL + S + DECISION |

### 「Doll 知道 source」怎麼實現

- DollEvent.perception 是自然語言敘述，**source 訊息在敘述中**：
  - 「主人在手機上對我說：嗨」
  - 「鬧鐘 'wake_up' 在早上 7 點響了」
  - 「我的 drone 'morning_report' 回報：…」
- Doll **不需要寫 isinstance / dispatch**；她讀自然語言就能依來源反應
- DollEvent.raw 是反向參照，工程層 routing 用（例：`raw.response_sink` 把回應送回對的 client；`raw.channel` future routing），Doll 看不到、也不該看

### 為什麼不在 DollEvent 加 `source: str` 結構欄位

避免「兩個事實來源」（perception 文字寫 phone / source 欄位寫 user_text_phone 對不起來）。需要結構訊息時從 `raw` 拿。

---

## §3 EventDispatcher 設計

```
RawEvent 進入
    ↓
EventDispatcher.dispatch(raw)
    ↓
asyncio.create_task(self._handle(raw))   ← 立刻 return，不等任何事
    ↓
[ 並行 task ]
    │
    ├─ doll_event = await self._perceive(raw)
    │       step 4: stub — UserTextEvent 直通成 DollEvent(perception=raw.text)
    │       step 5: 換成 await self.inner_voice.perceive(raw)
    │
    └─ await self._respond(doll_event)
          ├─ recall = await inner_voice.recall(doll_event.perception)
          ├─ system = render("scaffolding", character=...)
          ├─ prefill = f"{recall}DECISION: "
          ├─ async for chunk in adapter.stream_completion(
          │       system=system,
          │       user=doll_event.perception,   ← 餵 perception 不是 raw text
          │       prefill=prefill,
          │   ):
          │       doll_event.raw.response_sink.put_nowait(TextChunk(...))
          ├─ doll_event.raw.response_sink.put_nowait(TurnEnd())
          └─ doll_event.raw.response_sink.put_nowait(None)   # end-of-stream sentinel
```

### 結構（沒有 worker、沒有 queue）

- `EventDispatcher.dispatch(raw)`：spawn 一個 task。沒有阻塞、沒有排隊。
- 每 event 一個 `asyncio.Task`。多 event 自然並行。
- 大模型多 stream 並行由 llama.cpp `--parallel 2 --cont-batching` 處理（已在現行 launch script）。
- 沒有 `DollSpeaker singleton`、沒有「Doll turn 互斥」— 跟使用者明確要求對齊。

### 生命週期

- `start()`：no-op（沒有 worker）；保留方法以便未來加背景任務
- `stop()`：取消所有 in-flight task，await 完成；後續 `dispatch` 拋 `RuntimeError`
- 追蹤 in-flight：`self._tasks: set[asyncio.Task]`，task 完成時 callback 自動 discard

### 例外處理

- handler 內任何 exception → log + 把 `ErrorMsg` 推進 `raw.response_sink` + 推 `None` 哨兵 → IPC handler 端正常收尾
- handler **不再 raise** — task 是 fire-and-forget，沒人接 exception。所有錯誤路徑必須在 task 內收完、推進 sink。

### 並行性語意（明確）

- 多 client 同時送 `TextInput`：兩個 `dispatch()` 各自 spawn task，**真的並行**跑（recall 並行 + 大模型 stream 並行）。llama.cpp `--parallel 2` 上限後內部排隊。
- 同一 client 連續送兩個 `TextInput`：IPC server 對單 connection 是 `async for raw in ws` 序列收；兩個 dispatch 仍各自 spawn task；但 sink 是各自的，client 收到的順序仍然是先送先回（IPC handler 內 `async for item in sink` 序列 yield）。
- **沒有 raw event ordering 保證跨來源**：來自手機的 event 跟來自 UI 的 event 哪個先到 dispatcher，看調度。OK，因為 Inner Voice（step 5+）負責語意上的時序處理。

---

## §4 檔案改動

```
src/dollos/
├── events.py                       # NEW — RawEvent ABC + UserTextEvent + DollEvent
├── dispatcher.py                   # NEW — EventDispatcher
└── kernel.py                       # MODIFY — wire EventDispatcher + 薄化 IPC handler

tests/
├── test_events.py                  # NEW — RawEvent / UserTextEvent / DollEvent 結構
├── test_dispatcher.py              # NEW — dispatch 並行行為 + 例外處理 + 生命週期
└── test_kernel.py                  # MODIFY — 整合測試確認 IPC handler → dispatcher → sink 串通
```

不動：
- `src/dollos/ipc/*`（介面不變）
- `src/dollos/inner_voice.py`、`src/dollos/llm/*`、`src/dollos/prompts/*`
- `src/dollos/config.py`
- `config.example.toml`

---

## §5 `dollos.events` 設計

```python
"""Event types — two-tier model.

RawEvent: structured event from a source (IPC text, voice, timer, ...).
DollEvent: natural-language perception emitted by Inner Voice's perceive(),
           consumed by the big LLM as the `user` role.

Step 4 ships RawEvent + UserTextEvent + DollEvent dataclasses. The
RawEvent → DollEvent conversion is stubbed (passthrough). Step 5 will
replace the stub with InnerVoice.perceive().
"""

from __future__ import annotations

import asyncio
from abc import ABC
from dataclasses import dataclass

from dollos.ipc.messages import ServerMessage


class RawEvent(ABC):
    """Structured event from a source. Future subclasses: VoiceInputEvent,
    TimerFiredEvent, ToolResultEvent, DroneResultEvent, ...
    """


@dataclass
class UserTextEvent(RawEvent):
    """Text typed by the user via IPC.

    response_sink: per-event queue. Dispatcher pushes ServerMessage objects
    for streaming back to the IPC handler, then ``None`` sentinel.
    """

    text: str
    response_sink: asyncio.Queue[ServerMessage | None]


@dataclass
class DollEvent:
    """Natural-language perception consumed by the big LLM as `user` role.

    perception: free-form natural language including source semantics
        ("主人在手機上對我說 X", "鬧鐘響了", "drone 回報：...").
    raw: back-reference to the RawEvent for engineering routing
        (response_sink, source metadata). Doll itself does not see this.
    """

    perception: str
    raw: RawEvent
```

**設計重點**：
- `RawEvent` 是 ABC，留給未來 N 個 subclass
- `UserTextEvent` 是 step 4 唯一 concrete subclass
- `DollEvent` step 4 就定義（不延到 step 5），讓 dispatcher 流程命名一致；step 4 dispatcher 自己構造 `DollEvent`（stub），step 5 由 Inner Voice 構造
- `response_sink` 留在 `RawEvent` 子類別上而非 `DollEvent` 上：sink 是「回應送哪去」的 routing，屬工程細節

---

## §6 `dollos.dispatcher.EventDispatcher` 設計

```python
"""EventDispatcher — fan-out raw events to concurrent tasks."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from dollos.events import DollEvent, RawEvent, UserTextEvent
from dollos.ipc.messages import ErrorMsg, ServerMessage, TextChunk, TurnEnd
from dollos.inner_voice import InnerVoice
from dollos.llm.adapter import LLMAdapter
from dollos.prompts import PromptRenderer

logger = logging.getLogger(__name__)


class EventDispatcher:
    """Spawns one asyncio.Task per RawEvent. No worker, no queue.

    Step 4 ships a stubbed `_perceive` that turns a UserTextEvent's text
    directly into a DollEvent.perception. Step 5 will replace this with
    InnerVoice.perceive(raw).
    """

    def __init__(
        self,
        *,
        adapter: LLMAdapter,
        inner_voice: InnerVoice,
        renderer: PromptRenderer,
        character_profile: str,
    ) -> None:
        self._adapter = adapter
        self._inner_voice = inner_voice
        self._renderer = renderer
        self._character_profile = character_profile
        self._tasks: set[asyncio.Task[None]] = set()
        self._stopping = False

    def dispatch(self, raw: RawEvent) -> None:
        if self._stopping:
            raise RuntimeError("EventDispatcher is stopping")
        task = asyncio.create_task(self._handle(raw), name=f"event-{type(raw).__name__}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def stop(self) -> None:
        self._stopping = True
        if not self._tasks:
            return
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _handle(self, raw: RawEvent) -> None:
        sink = self._sink_of(raw)
        try:
            doll_event = await self._perceive(raw)
            await self._respond(doll_event, sink)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("dispatcher _handle error")
            sink.put_nowait(ErrorMsg(message=f"handler error: {e}"))
        finally:
            sink.put_nowait(None)

    async def _perceive(self, raw: RawEvent) -> DollEvent:
        # Step 4 stub: passthrough for UserTextEvent.
        # Step 5 will replace this with: return await self._inner_voice.perceive(raw)
        if isinstance(raw, UserTextEvent):
            return DollEvent(perception=raw.text, raw=raw)
        raise TypeError(f"no stub perceive for {type(raw).__name__}")

    async def _respond(
        self,
        doll_event: DollEvent,
        sink: asyncio.Queue[ServerMessage | None],
    ) -> None:
        recall = await self._inner_voice.recall(doll_event.perception)
        system = self._renderer.render(
            "scaffolding", character=self._character_profile
        )
        prefill = f"{recall}DECISION: "
        async for chunk in self._adapter.stream_completion(
            system=system,
            user=doll_event.perception,
            prefill=prefill,
        ):
            if chunk.text:
                sink.put_nowait(TextChunk(text=chunk.text))
            if chunk.done:
                break
        sink.put_nowait(TurnEnd())

    @staticmethod
    def _sink_of(raw: RawEvent) -> asyncio.Queue[ServerMessage | None]:
        # Step 4: only UserTextEvent has a sink. Future RawEvent types may
        # not (e.g. TimerFiredEvent has no client to respond to — output
        # via tool calls / IPC push). Centralize the access pattern here.
        if isinstance(raw, UserTextEvent):
            return raw.response_sink
        raise TypeError(f"no sink for {type(raw).__name__}")
```

**設計重點**：
- `dispatch()` **是 sync 方法**（不是 `async def`）— 不阻塞、不需要 await。IPC handler 呼叫一次後立刻 return。
- 每 task `add_done_callback(self._tasks.discard)` 自動清 set
- `stop()` 取消所有 in-flight，`return_exceptions=True` 不讓任一個 cancel 掛掉 gather
- 例外永遠在 task 內吞掉並推 `ErrorMsg + None` 進 sink
- `_perceive` 是 step 4 stub，明確標記 step 5 換掉的位置
- `_sink_of` 集中「raw → sink」的邏輯，未來 RawEvent 類型擴增時這裡是 routing 表
- 沒有任何 LLM call、recall call、render call 在 dispatch 路徑上同步阻塞（全 `await` 在 task 內）

---

## §7 Kernel Wiring

```python
class DollOS:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.adapter = build_adapter(settings)
        self.renderer = PromptRenderer()
        self.memsearch = build_memsearch(settings)
        self.inner_voice = build_inner_voice(settings, self.memsearch, self.renderer)
        self._character_profile = settings.character.profile_path.read_text()
        self.dispatcher = EventDispatcher(
            adapter=self.adapter,
            inner_voice=self.inner_voice,
            renderer=self.renderer,
            character_profile=self._character_profile,
        )
        self.server = WebSocketServer(
            host=settings.ipc.host,
            port=settings.ipc.port,
            handler=self._handle_text_input,
        )
        self._shutdown = asyncio.Event()

    async def _handle_text_input(
        self, msg: TextInput
    ) -> AsyncIterator[ServerMessage]:
        sink: asyncio.Queue[ServerMessage | None] = asyncio.Queue()
        self.dispatcher.dispatch(UserTextEvent(text=msg.text, response_sink=sink))
        while True:
            item = await sink.get()
            if item is None:
                return
            yield item

    async def run(self) -> None:
        await self.memsearch.index()
        try:
            await self.server.start()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self._shutdown.set)
            try:
                await self._shutdown.wait()
            finally:
                await self.server.stop()
                await self.dispatcher.stop()
        finally:
            pass
```

**改動細節**：
- `_handle_text_input` 從「跑邏輯」變「中介」— 構 sink、構 UserTextEvent、dispatch、drain sink
- 邏輯 lift 到 `EventDispatcher._respond`（從 kernel 搬出去）
- `run()` 多了 `self.dispatcher.stop()`（先停 server 再停 dispatcher：避免 server 還在收新 event 時 dispatcher 已停）
- 沒有 `dispatcher.start()` — dispatcher 是 stateless，不需要啟動

---

## §8 測試策略

| 測試檔 | 範圍 |
|---|---|
| `tests/test_events.py` | (a) `UserTextEvent` 構造正確 (b) `DollEvent` 構造正確、`raw` 反向參照 (c) `isinstance(UserTextEvent(...), RawEvent)` |
| `tests/test_dispatcher.py` | (a) `dispatch` 是 sync 且 return 後 task 還在跑 (b) handler 跑完 sink 收到正確 message 序列 (c) handler 內例外 → sink 收到 `ErrorMsg` + `None` (d) `stop()` cancel in-flight task；後續 `dispatch` 拋 `RuntimeError` (e) 多 dispatch 並行（兩個 event 同時 dispatch，兩個 sink 都收到完整序列） (f) 未支援 RawEvent 類型（如自定 `class FooEvent(RawEvent)`） → handler 內 `TypeError` → sink 收到 `ErrorMsg` + `None` |
| `tests/test_kernel.py` | （延伸既有）整合：mock InnerVoice + LLMAdapter，丟 `TextInput` 進 `_handle_text_input`，驗證收到的 `ServerMessage` 序列跟 step 3 完全一致（TextChunk* + TurnEnd） |

**不測**：
- 真大模型 / 小模型 inference（manual smoke test）
- IPC server 那層（不變）
- 多 client 並行壓測（v1 邏輯正確即可）
- llama.cpp `--parallel` 配置（外部基礎設施）

**Fake LLMAdapter / Fake InnerVoice**：沿用 step 3 既有 fake pattern。`test_dispatcher.py` 用 fake adapter/IV 直接構 `EventDispatcher`，避免重複 kernel 構造。

---

## §9 邊界與錯誤路徑

| 情境 | 行為 |
|---|---|
| recall 失敗（小模型 down / timeout） | task 內 except → sink 收到 `ErrorMsg` + `None` → IPC handler yield ErrorMsg 後 return |
| 大模型 stream 中途斷 | 同上 |
| `_perceive` 拋 `TypeError`（unsupported RawEvent） | 同上 — 但這代表 caller bug |
| `dispatch()` 在 `stop()` 之後 | `RuntimeError("EventDispatcher is stopping")` |
| `stop()` 中途 task 還在 streaming | task 被 cancel；CancelledError 在 task 內 raise；sink 可能留半段訊息；client 那邊 IPC connection 此時也在被關，看到斷線可接受 |
| client 在 turn 中途斷線 | IPC server 的 `_on_connect` 結束 `async for`；handler 的 `yield` 拋 exception；但 task 不知道，繼續 push 進無人讀的 sink → 訊息被 GC。Doll 把話講完，沒人聽，不破壞狀態 |
| 多 client 並行 | 每個 IPC connection 各自有 sink；dispatch 各自 task；llama.cpp `--parallel` 處理大模型並發 |
| `--parallel` 滿（v1 = 2） | 第三個 stream 被 llama-server 內部排隊（standard llama.cpp 行為，不在我們控制） |
| 同一 client 連續送 N 個 TextInput | IPC server 對 connection 是 `async for raw in ws` 序列收；每個進來時呼一次 `_handle_text_input`；它們各自 spawn task 並行；但 IPC handler 是 generator，client 收到的訊息順序仍然是先送先回 |

---

## §10 Non-goals（明確排除）

- **DollEvent 結構欄位 `source: str`** — perception 是 canonical 敘述，避免兩個事實來源
- **DollSpeaker singleton / 大模型 turn 互斥** — 物理上 llama.cpp `--parallel` 已處理；單一狀態不靠序列化執行達成
- **KV cache reuse / slot persistence** — 獨立的後續 plan
- **Interrupt（取消 in-flight Doll turn）** — Inner Voice 不存在前無人請求；step 5/7 才考慮
- **並發 cap** — v1 不做；llama.cpp `--parallel` 自然 bound
- **S lock** — S 還沒人寫；step 5 加 Inner Voice perceive/process 時連 S 容器 + lock 一起
- **History dataclass / 結構** — 等 Memory 自動寫（step 8）+ 大模型 chat history（step 5+）真有人讀時加
- **Reflex / cascade / pre-post bracket** — step 7
- **Backpressure** — v1 不做（task 沒上限，但 event source 物理上 bound — IPC 速度、timer 頻率）
- **Event 持久化 / replay** — 永遠不做

---

## §11 Open Questions

- **`stop()` 時 task cancel 後 sink 哨兵問題**：cancel 進來時 task 在 `async for chunk` 中，CancelledError raise；`finally` 仍會 push `None` 哨兵嗎？asyncio 的 finally 在 cancel 時會跑（除非 finally 自己又 await 什麼被打斷）。`put_nowait(None)` 不 await，安全。**結論：finally 內 push 哨兵 OK**。但 IPC handler 此時可能也在 cancel 中，沒人 read。可接受
- **`dispatch()` 的回傳值**：目前 sync void。要不要回 task 讓 caller await？v1 不需要（IPC handler 是 drain sink 而非 await task）。未來如果有「需要等 event 處理完才 return 的 caller」再考慮
- **stub `_perceive` 對非 UserTextEvent 拋 TypeError**：step 4 只有 UserTextEvent 一種，安全。step 5 換成 Inner Voice 後這裡就涵蓋全部類型

---

## §12 後續 Plan 連動

- **Step 5（Inner Voice full）**：
  - `_perceive` stub 換成 `await self._inner_voice.perceive(raw)`
  - Inner Voice perceive 同時產 first_instinct + emotion + S delta（main spec §4）
  - S 容器 + asyncio.Lock 出現；perceive 結束後 `async with S_lock: S = merge(S, delta)`
  - prefill 從 `<think>RECALL+DECISION` 變成 `<think>RECALL+S+DECISION`
  - 多 character pack 可能各自有 `iv_perceive.jinja`（step 10）
- **Step 6（tool calling）**：
  - `_respond` 內 `parse_stream` 解 tool call；`say` 變 streamable tool（chunk 推 sink 邏輯改成 say tool 自己負責）
  - tool 結果產 `ToolResultEvent(RawEvent)` → `dispatcher.dispatch(...)` cascade
- **Step 7（reflex + bracket）**：
  - Inner Voice review() 階段；reflex_calls 直接走 tool executor + 產 ToolResultEvent
  - cascade loop emerges from event 回流
- **Step 8（auto memory write）**：
  - DollEvent.perception + assistant utterance 寫進 Memory
  - 「history」這個概念的真實 carrier 是 Memory，不是 in-memory 結構
- **Step 9（subagent）**：
  - subagent 跑完 push `SubagentResultEvent(RawEvent)` → dispatcher
  - 跟 ToolResultEvent 路徑一致

---

## §13 Plan Task 預估（4 tasks）

> writing-plans 會展開細節。

1. `dollos/events.py` + `tests/test_events.py`
2. `dollos/dispatcher.py` + `tests/test_dispatcher.py`
3. `kernel.py` 改寫（wire dispatcher + 薄化 IPC handler）+ `tests/test_kernel.py` 更新
4. Manual smoke test（真大小模型，行為應跟 step 3 一致）

預估短 — 全結構搬移，無新外部依賴。
