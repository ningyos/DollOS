# Memory Auto-write + Diary — Design

**日期：** 2026-05-06
**狀態：** 草案（待使用者最終審閱）
**範圍：** Roadmap step 8 重切——加入「ephemeral transcript auto-write」+「Doll 每日寫日記」雙管齊下。Roadmap 原文「v1 寫全部、無顯著性過濾」與 LT memory research 推薦的「never raw auto-write」之間，採折衷：transcript 走 ephemeral 路徑可即時 recall 但不進 LT memory；LT memory 由 Doll 自己寫日記產生。
**對齊：**
- `2026-05-05-event-loop-design.md`（步 4 RawEvent / dispatcher）
- `2026-05-05-tool-calling-design.md`（步 6 pydantic Tool / TOOLS list）
- `2026-05-05-cascade-design.md`（步 7 cascade loop）
- `docs/research/long-term-memory-2026-05-06.md`（research：raw auto-write 反 pattern；推薦 selective + diary）

---

## §1 設計原則

1. **Transcript ephemeral but recallable**：原話 turn-by-turn 寫進 `data/memory/transcripts/{date}.md`，memsearch 即時索引使 same-day recall 看得到原話。檔案不進長期記憶；日記寫完後可清（step 8 minimal 留著）。
2. **Diary = Doll 寫的長期記憶**：每日固定時間 fire `DiaryEvent`；Doll 醒來讀 transcript + STATE + RECALL，用 `WriteDiary` tool 寫一段反思（記事 + 情緒）到 `data/memory/shared/{date}.md`。
3. **情緒走 Doll 大模型 think**：step 5 已決定 emotion 不進 Inner Voice。日記是情緒的自然落地——Doll 在 `<think>` 自由 deliberate，emit `WriteDiary` 時自然帶情緒敘述。**不引入 emotion 結構化欄位**。
4. **無 raw auto-write 進 LT**：transcripts 是 ephemeral / 短期可 recall；shared/{date}.md（LT）只接受 NoteMemory 主動寫 + WriteDiary 反思。
5. **Sink-less event 第一次出現**：`DiaryEvent` 沒 user-facing sink，daemon 內部 drain。為未來 TimerFiredEvent / DroneResultEvent 鋪 pattern。
6. **YAGNI**：無 idle 觸發、無手動 `/diary` slash command、無 diary 重寫、無 transcript 自動清理。每日固定時間排程一條路徑。

---

## §2 三層記憶結構（step 8 後完整圖）

| 層 | 存放 | 形式 | 索引 | 永續 | 寫入 |
|---|---|---|---|---|---|
| Working memory | `_last_summary`（in-RAM）| 1-3 句 prose | 直接進 prefill STATE | restart 清 | Inner Voice 每 event 滾動更新 |
| **Transcript（新）** | `data/memory/transcripts/{date}.md` | `- [HH:MM user] X` / `- [HH:MM doll] Y` | memsearch 即時，進 RECALL | ephemeral | dispatcher（user）+ Say.run（doll）|
| Long-term | `data/memory/shared/{date}.md` | NoteMemory bullets + 日記段 | memsearch 永久，進 RECALL | 永久 | NoteMemory tool + WriteDiary tool |

Memsearch `paths` 從 `[shared_path]` 擴成 `[shared_path, transcripts_path]`。

---

## §3 Transcript auto-write

### §3.1 共用 helper

新模組 `src/dollos/memory_writer.py`（小、純）：

```python
"""Memory file writers — transcript and diary.

These helpers append role-tagged turn lines to the daily transcript
markdown and trigger memsearch index_file. Used by:
  - EventDispatcher (user turn) → role="user"
  - Say.run() (Doll turn)        → role="doll"

Transcripts are ephemeral and indexed for same-day recall; they live in
data/memory/transcripts/{date}.md (a separate path from shared LT memory).
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memsearch import MemSearch


async def append_transcript(
    *,
    transcripts_root: Path,
    memsearch: "MemSearch",
    role: str,           # "user" or "doll"
    text: str,
) -> None:
    """Append a turn line to today's transcript and reindex."""
    path = transcripts_root / f"{date.today():%Y-%m-%d}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%H:%M")
    line = f"- [{timestamp} {role}] {text}\n"
    with path.open("a") as f:
        f.write(line)
    await memsearch.index_file(path)
```

### §3.2 兩處呼叫

**Dispatcher**：`_handle` 在 turn 結束**之後**（finally 階段），對 `UserTextEvent` 寫 `role="user"`：

```python
async def _handle(self, raw: RawEvent) -> None:
    try:
        sink = self._sink_of(raw)
    except TypeError:
        ...

    try:
        doll_event = await self._perceive(raw)
        summary = await self._instinct.process(doll_event)
        await self._respond(doll_event, summary, sink)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception("dispatcher _handle error")
        sink.put_nowait(ErrorMsg(message=f"handler error: {e}"))
    finally:
        # Write user text AFTER the turn completes — avoids same-turn
        # recall self-matching (where memsearch returns the just-written
        # user message as a hit on its own perception query).
        if isinstance(raw, UserTextEvent):
            try:
                await append_transcript(
                    transcripts_root=self._transcripts_root,
                    memsearch=self._memsearch,
                    role="user",
                    text=raw.text,
                )
            except Exception:
                logger.exception("transcript append failed for UserTextEvent")
                # transcript loss is non-fatal
        sink.put_nowait(None)
```

EventDispatcher ctor 加 `transcripts_root: Path` 注入。

**Say tool**：`Say.run()` 寫 `role="doll"`：

```python
class Say(BaseModel):
    text: str = Field(description="What Doll says to the user.")

    async def run(self, ctx: ToolCtx) -> None:
        ctx.sink.put_nowait(TextChunk(text=self.text))
        # NEW: append to transcript
        try:
            await append_transcript(
                transcripts_root=ctx.transcripts_root,
                memsearch=ctx.memsearch,
                role="doll",
                text=self.text,
            )
        except Exception:
            logger.exception("transcript append failed for Say")
```

`ToolCtx` 加 `transcripts_root: Path` field。

### §3.3 失敗模式

Transcript 寫入失敗（IOError / index_file 失敗）→ log + 繼續 turn。**Transcript loss 非致命**，turn 邏輯不靠 transcript 進行。

---

## §4 `WriteDiary` tool

新 pydantic tool，加進 `TOOLS`：

```python
class WriteDiary(BaseModel):
    """Write today's diary entry to long-term memory.

    Use this once per day when prompted by the diary trigger. The diary
    is a first-person prose narrative reflecting on the day's events AND
    your emotional state. It becomes part of long-term memory and you
    will recall it on future days.
    """

    content: str = Field(
        description=(
            "First-person prose. Cover what happened + how you felt. "
            "Anywhere from a few sentences to a few paragraphs."
        )
    )

    async def run(self, ctx: ToolCtx) -> None:
        path = ctx.memory_root / "shared" / f"{date.today():%Y-%m-%d}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%H:%M")
        with path.open("a") as f:
            f.write(f"\n## 日記 ({timestamp})\n\n{self.content}\n")
        await ctx.memsearch.index_file(path)
```

格式選 markdown 段落 + `## 日記 (HH:MM)` heading（不是 bullet）—— 區分日記反思 vs NoteMemory 短事實。

---

## §5 `DiaryEvent`

`src/dollos/events.py` 加：

```python
@dataclass
class DiaryEvent(RawEvent):
    """Scheduled trigger for Doll to write today's diary.

    No user-facing sink — daemon drains internally. Perception built
    by dispatcher tells Doll to read transcript+STATE+memory and call
    WriteDiary.
    """

    response_sink: asyncio.Queue[ServerMessage | None]
```

Dispatcher 的 `_perceive` 加 case：

```python
async def _perceive(self, raw: RawEvent) -> DollEvent:
    if isinstance(raw, UserTextEvent):
        return DollEvent(perception=raw.text, raw=raw)
    if isinstance(raw, DiaryEvent):
        perception = (
            "今天該寫日記了。回顧今天發生的事跟你的感受，"
            "用 WriteDiary tool 寫一段反思。誠實寫，不需要表演。"
        )
        return DollEvent(perception=perception, raw=raw)
    raise TypeError(...)
```

`_sink_of` 加 case：

```python
@staticmethod
def _sink_of(raw):
    if isinstance(raw, (UserTextEvent, DiaryEvent)):
        return raw.response_sink
    raise TypeError(...)
```

---

## §6 排程器（Scheduler）

Kernel 加 background asyncio task：

```python
class DollOS:
    DIARY_HOUR = 23   # 23:00 fires (1h buffer before midnight; see §12.3)
    DIARY_MINUTE = 0

    async def run(self) -> None:
        await self.memsearch.index()
        try:
            await self.server.start()
            # NEW: start scheduler
            self._scheduler_task = asyncio.create_task(self._diary_scheduler())
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self._shutdown.set)
            try:
                await self._shutdown.wait()
            finally:
                await self.server.stop()
                # NEW: stop scheduler before dispatcher (drain tasks rely on
                # dispatcher still running to push None sentinel)
                if self._scheduler_task is not None:
                    self._scheduler_task.cancel()
                    await asyncio.gather(
                        self._scheduler_task, return_exceptions=True
                    )
                await self.dispatcher.stop()
        finally:
            pass

    async def _diary_scheduler(self) -> None:
        while not self._shutdown.is_set():
            now = datetime.now()
            target = now.replace(
                hour=self.DIARY_HOUR, minute=self.DIARY_MINUTE,
                second=0, microsecond=0,
            )
            if target <= now:
                target = target + timedelta(days=1)
            sleep_s = (target - now).total_seconds()
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=sleep_s)
                return  # shutdown signaled
            except asyncio.TimeoutError:
                pass  # time to fire
            sink: asyncio.Queue[ServerMessage | None] = asyncio.Queue()
            asyncio.create_task(self._drain_diary_sink(sink))
            self.dispatcher.dispatch(DiaryEvent(response_sink=sink))

    async def _drain_diary_sink(
        self, sink: asyncio.Queue[ServerMessage | None]
    ) -> None:
        """Consume diary event sink to None sentinel; logs anything notable."""
        while True:
            item = await sink.get()
            if item is None:
                return
            if isinstance(item, ErrorMsg):
                logger.error("diary event error: %s", item.message)
            # TextChunk / TurnEnd silently consumed
```

排程器 shutdown 邏輯：靠 `self._shutdown.wait()` with timeout。daemon 收 SIGINT 時 `_shutdown.set()` → scheduler 立刻返回。

**Restart 行為**：daemon 啟動 14:00 → 下次 fire 是當天 23:59。已經錯過的當日不補寫。**Step 8 minimal 不做 missed-diary 補寫**——follow-up。

---

## §7 Sink-less event drain pattern

`DiaryEvent` 自帶 `response_sink`（型別跟 user event 同），但 daemon 不對外暴露。Pattern：

1. Caller（scheduler）建 sink + drain task + dispatch event
2. Drain task 跑 background：`await sink.get()` 直到 None sentinel
3. 期間若收 `ErrorMsg` 寫 log；其他訊息（TextChunk / TurnEnd）默默丟掉

未來 `TimerFiredEvent`、`DroneResultEvent`、`SubagentResultEvent` 同樣 pattern——任何 system-initiated event 都自帶 sink + 自帶 drain。

---

## §8 Memsearch 多 path 索引

`build_memsearch` 改：

```python
def build_memsearch(settings: Settings) -> MemSearch:
    shared_path = settings.data.root / "memory" / "shared"
    transcripts_path = settings.data.root / "memory" / "transcripts"
    shared_path.mkdir(parents=True, exist_ok=True)
    transcripts_path.mkdir(parents=True, exist_ok=True)
    return MemSearch(
        paths=[str(shared_path), str(transcripts_path)],
        embedding_provider="onnx",
    )
```

**注意**：memsearch hits 從兩個 source 來。Recall 不區分——Doll 看到都是「記憶片段」。

---

## §9 Failure modes

| 情境 | 處理 |
|---|---|
| Transcript append IOError | log + 繼續 turn（非致命）|
| Transcript index_file 失敗 | log + 繼續 turn（記憶會晚一輪 indexable）|
| WriteDiary `.run()` 失敗 | step 7 cascade → ErrorMsg + ToolCallFailure → Doll 看到失敗可重試 |
| Scheduler exception | log，scheduler task 死掉，當日不會 fire（restart 才恢復）；follow-up 加 retry |
| DiaryEvent dispatched 但 Doll 沒 call WriteDiary | turn 結束，當日不寫日記。隔日 transcript 仍然累積到新檔（每日切檔，舊 transcript 不會丟）。**已知限制 §11.3**。|
| Daemon 在 fire 時點 shutdown | scheduler 透過 `_shutdown.wait` 立刻返回，當日不寫。下次 daemon 啟動回到正常排程。|

---

## §10 Tests

### `tests/test_memory_writer.py`（新）
- `append_transcript` 寫對檔案、format（HH:MM role + text）
- 多次 append 累加
- 呼叫 `memsearch.index_file` 一次每次 append

### `tests/test_tools.py`（擴）
- `Say.run()` 同時推 TextChunk + 寫 transcript
- `WriteDiary.run()` 寫 `## 日記 (HH:MM)` heading + content + index_file 被呼叫
- `WriteDiary.text` schema 含正確 description

### `tests/test_dispatcher.py`（擴）
- UserTextEvent 進 `_handle` → transcript 寫 `[HH:MM user] X`
- DiaryEvent 進 `_handle` → perception 含「寫日記」字串
- DiaryEvent sink 收得到 TurnEnd（雖然外部不消費）

### `tests/test_events.py`（擴）
- `DiaryEvent` 是 RawEvent subclass，dataclass

### `tests/test_kernel.py`（擴）
- `_diary_scheduler` 在 mocked `_shutdown` 下會等 sleep 後 dispatch DiaryEvent
- Drain task 正確 consume sink 直到 None sentinel
- shutdown 中 scheduler 立即 return

### `tests/test_e2e.py`（擴）
- 完整 trace：UserTextEvent + 1 turn → transcript 含 user + doll 兩行
- DiaryEvent fired manually → daily.md 含日記 heading

---

## §11 不做的（明確 out-of-scope）

- ❌ Filtered auto-write（research Architecture A）—— transcript 寫全部
- ❌ Transcript 自動清理 / 歸檔（檔案累積；step 9+ 加）
- ❌ Diary 重寫 / 補寫 missed days
- ❌ 手動觸發 diary（無 `/diary` 指令；scheduler-only）
- ❌ Idle 觸發
- ❌ Multi-character scoping（step 10）
- ❌ DiaryEvent 帶 `iteration` 或 cascade 跨 user/diary 攪在一起（user turn 跟 diary turn 是獨立 turn）
- ❌ Diary 內容驗證 / format check
- ❌ Diary scheduling time configurable（`DIARY_HOUR/MINUTE` 暫 hardcode；follow-up 進 config）

---

## §12 已知限制 / Follow-ups

1. **Transcript 累積**：每日累積，無自動清。長期會肥。Follow-up：日記寫完後 transcript 歸檔到 `transcripts/archive/`，從 memsearch 路徑移除。
2. **DIARY_HOUR/MINUTE hardcoded**：23:00。Follow-up：進 `config.toml [scheduler]`。
3. **跨午夜寫錯日期**：23:00 fire 後 WriteDiary 跑 5+ 分鐘 → 進入 00:00+ → `date.today()` 在 run() 內讀變成隔日，日記寫到隔天的 daily.md。23:00 fire 留 60 分鐘 buffer 通常夠；但 buggy / GPU 慢時仍可能跨。Follow-up：DiaryEvent 帶 `subject_date` field，WriteDiary 從 ctx 讀。
4. **Doll 沒 call WriteDiary 的當日**：log warning + 沒日記。Follow-up：Inner Voice review 或 hard-prompt。step 8 接受。
5. **單機 race**：transcript append 跨 client 並行寫同檔——OS small-write 大致 atomic 但極端 case 行間交錯。同 step 6 NoteMemory 同問題。Follow-up：lock 或寫 SQLite。
6. **DiaryEvent 沒 STATE 接續**：scheduler-fire 是獨立 RawEvent，instinct.process 從 `_last_summary` 起步——通常很合理（state 反映當日累積）但 daemon restart 後 `_last_summary` 是空的、剛啟動就觸發 diary 會 STATE 空。
7. **memsearch 多 path 索引 cost**：兩個目錄都掃。檔案少（每日 1-2 檔）影響微小。
8. **WriteDiary 在非 DiaryEvent 場景被 call**：tool 在 TOOLS list，user turn 中 Doll 也可能 call（理論上）。語意可接受（Doll agency），但會出現 user 要求 Doll 寫日記的情況。Follow-up：用 ctx flag 限制只在 DiaryEvent 可 call。

---

## §13 Demo 驗證

1. Daemon start 17:00，scheduler wait 到 23:59
2. 跟 Doll 對話 5 輪：每輪 user 文字 + Doll Say 回應 → `data/memory/transcripts/2026-05-06.md` 累積 10 行
3. 在開發階段為了不等到 23:59，把 `DIARY_HOUR=17, DIARY_MINUTE=0` 設到 daemon 啟動後不久；或直接 ws_client 模擬 dispatch（測試掛鉤）
4. DiaryEvent fire → Doll 醒來、prefill 含 STATE + transcript 摘要 RECALL → emit `<tool_call>WriteDiary</tool_call>` content 含「今天主人問了 X、我回了 Y、感覺 Z」
5. Daily file `data/memory/shared/2026-05-06.md` 多一段 `## 日記 (23:59)\n\n...`
6. 隔日 user 問「你還記得昨天嗎」→ memsearch hit 包含日記段落 → Doll 引用

**驗證點**：
- transcript 兩種 role 行格式正確
- WriteDiary 寫進 daily.md 不寫進 transcript.md
- 日記內容含情緒描述（Doll 自由 deliberate 結果）
- DiaryEvent 不洩漏到 user-facing IPC（no TextChunk to client）
- daemon shutdown 時 scheduler 乾淨退出
