# B1: Episodic 逐字稿重接 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把死掉的 `append_transcript` 重新接回 live loop,讓 user↔doll 對話自動寫進可搜尋逐字稿。

**Architecture:** 兩個重接點都在 `MindLoop.iterate()`。user 端在 perception 迴圈直接寫(`p.data["text"]` 完整);doll 端因 `recent_outputs` 只存截斷摘要,需加 turn-local `_turn_speech` buffer 在兩個 emit 點累積完整句、turn 結束合併寫一條。

**Tech Stack:** Python 3 / asyncio,pytest + pytest-asyncio。helper `dollos.memory_writer.append_transcript` 與 `FtsMemory` 索引皆現成。

## Global Constraints

- **No-fallback**:transcript 寫入失敗用 `try/except Exception` + `logger.exception(...)` —— 不打斷對話、**不 silent**(必須 log)。不得加任何降級邏輯。
- **逐字稿語言**:沿用 helper 既有格式 `- HH:MM:SS {主人|我}說：{text}`,不改 helper。
- **只抓 user↔doll 對話**:系統 perception(schedule/monitor/subagent/reflection)不寫 transcript。
- **TDD**:每個 task 先寫失敗測試 → 跑失敗 → 最小實作 → 跑通過 → commit。
- **測試指令**:`uv run pytest <path> -v`(repo root 執行)。
- 既有 `tests/test_memory_writer.py` 5 個 helper 測試不得改動。

---

## File Structure

- **Modify**: `src/dollos/mind/mind_loop.py` — 唯一改動的 production 檔。
  - 加 import `append_transcript`
  - `__init__`:初始化 `self._turn_speech`
  - `iterate()`:perception 迴圈寫 user 行;cascade 前 clear buffer;cascade 後寫 doll 行
  - `_flush_chunker` / `_handle_stream_event`:emit 句子時 append 完整句到 buffer
- **Test**: `tests/test_episodic_transcript.py` — 新檔,所有 B1 整合測試集中於此(沿用 `tests/_dispatcher_helpers` 與 `tests/test_mind_loop.py` 的 `_FakeLLM` 模式)。

---

### Task 1: User-side transcript capture

把 `UserSpoke` perception 的完整文字寫進當日逐字稿。

**Files:**
- Modify: `src/dollos/mind/mind_loop.py`(import 區 + `iterate()` perception 迴圈,約 `mind_loop.py:145-159`)
- Test: `tests/test_episodic_transcript.py`(新檔)

**Interfaces:**
- Consumes: `append_transcript(*, transcripts_root, memsearch, role, text)`(`dollos.memory_writer`);`self._ctx.transcripts_root` / `self._ctx.memsearch`(`MindCtx`,`mind_ctx.py:41-43`);`p.data.get("text", "")`。
- Produces: 當日檔 `transcripts_root/{YYYY-MM-DD}.md` 新增 `- HH:MM:SS 主人說：{text}` 行。

- [ ] **Step 1: 寫失敗測試**

在新檔 `tests/test_episodic_transcript.py`:

```python
"""B1: episodic transcript recapture — live-loop integration tests."""
from __future__ import annotations

import asyncio
from datetime import date

import pytest

from dollos.mind.mind_loop import MindLoop
from dollos.mind.mind_state import MindState, Perception
from dollos.mind.perception_queue import PerceptionQueue
from tests._dispatcher_helpers import _make_mind_ctx, _FakeMemSearch
from tests.test_mind_loop import _FakeLLM
from dollos.tools import MAIN_TOOLS


def _today_transcript(ctx):
    return ctx.transcripts_root / f"{date.today():%Y-%m-%d}.md"


def _speak_only_stream(text: str) -> str:
    # voice_first wire: think block then spoken text, no tool call.
    return (
        "SEEN: x\nINTENT: y\nREVIEW: ok\nMOOD: warm\nTOOL: none\n"
        "</think>\n\n" + text
    )


def _make_loop(tmp_path, *, state, ctx, stream):
    return MindLoop(
        state=state,
        queue=_QUEUE_HOLDER.pop(),
        ctx=ctx,
        llm=_FakeLLM(stream),
        system_prompt="You are Doll.",
        state_persist_path=tmp_path / "mind_state.json",
        tool_registry={cls.__name__: cls for cls in MAIN_TOOLS},
    )


# Tiny indirection so each test builds its own queue before _make_loop.
_QUEUE_HOLDER: list = []


@pytest.mark.asyncio
async def test_user_turn_written_to_transcript(tmp_path):
    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "你好嗎"}))
    ms = _FakeMemSearch()
    sink: asyncio.Queue = asyncio.Queue()
    ctx = _make_mind_ctx(tmp_path, memsearch=ms, sink=sink, state=state)
    _QUEUE_HOLDER.append(queue)
    loop = _make_loop(tmp_path, state=state, ctx=ctx, stream=_speak_only_stream("好啊"))

    await loop.iterate()

    content = _today_transcript(ctx).read_text()
    assert "主人說：你好嗎" in content
    # transcript file was indexed
    assert _today_transcript(ctx) in [__import__("pathlib").Path(p) for p in ms.indexed]
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_episodic_transcript.py::test_user_turn_written_to_transcript -v`
Expected: FAIL(transcript 檔不存在 / `FileNotFoundError`)。

- [ ] **Step 3: 最小實作**

在 `src/dollos/mind/mind_loop.py` 頂部 import 區加(與其他 `from dollos...` import 並列):

```python
from dollos.memory_writer import append_transcript
```

在 `iterate()` 的 perception 迴圈(現為 `mind_loop.py:145-159`)把:

```python
        for p in perceptions:
            self._state.recent_perceptions.append(p)
            if p.kind == "UserSpoke":
                self._state.last_user_at = p.t
```

改為:

```python
        for p in perceptions:
            self._state.recent_perceptions.append(p)
            if p.kind == "UserSpoke":
                self._state.last_user_at = p.t
                user_text = p.data.get("text", "")
                if user_text:
                    try:
                        await append_transcript(
                            transcripts_root=self._ctx.transcripts_root,
                            memsearch=self._ctx.memsearch,
                            role="user",
                            text=user_text,
                        )
                    except Exception:
                        logger.exception(
                            "transcript write (user) failed; continuing"
                        )
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_episodic_transcript.py::test_user_turn_written_to_transcript -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add src/dollos/mind/mind_loop.py tests/test_episodic_transcript.py
git commit -m "feat(memory): B1 user-side transcript capture in iterate()"
```

---

### Task 2: Doll-side transcript capture(turn-speech buffer)

Doll 本 turn 說出的完整內容合併成單一逐字稿行。

**Files:**
- Modify: `src/dollos/mind/mind_loop.py`(`__init__` ~`:114`、`iterate()` cascade 前後、`_flush_chunker` `:594-602`、`_handle_stream_event` SpeakChunk 分支 `:615-625`)
- Test: `tests/test_episodic_transcript.py`

**Interfaces:**
- Consumes: 同 Task 1 的 `append_transcript` + ctx 欄位;cascade 入口 `self._llm_iterate(prompt)`(`mind_loop.py:229`);emit 點的區域變數 `sentence`。
- Produces: 當日檔新增單一 `- HH:MM:SS 我說：{本turn完整串接}` 行;新 instance 屬性 `self._turn_speech: list[str]`。

- [ ] **Step 1: 寫失敗測試**

附加到 `tests/test_episodic_transcript.py`:

```python
@pytest.mark.asyncio
async def test_doll_turn_written_as_single_joined_line(tmp_path):
    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "嗨"}))
    ms = _FakeMemSearch()
    sink: asyncio.Queue = asyncio.Queue()
    ctx = _make_mind_ctx(tmp_path, memsearch=ms, sink=sink, state=state)
    _QUEUE_HOLDER.append(queue)
    # Two full sentences in the spoken segment.
    loop = _make_loop(
        tmp_path, state=state, ctx=ctx,
        stream=_speak_only_stream("第一句話。第二句話。"),
    )

    await loop.iterate()

    content = _today_transcript(ctx).read_text()
    doll_lines = [ln for ln in content.split("\n") if "我說：" in ln]
    # exactly ONE doll line (turn-level, not per-sentence)
    assert len(doll_lines) == 1
    assert "第一句話。" in doll_lines[0]
    assert "第二句話。" in doll_lines[0]
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_episodic_transcript.py::test_doll_turn_written_as_single_joined_line -v`
Expected: FAIL(無 `我說：` 行 / `AssertionError: len 0`)。

- [ ] **Step 3: 最小實作**

3a. `__init__`(在 `self._cascade_ctx: CascadeCtx | None = None` 旁,`mind_loop.py:114`)加:

```python
        # Turn-local buffer of FULL spoken sentences (recent_outputs only keeps
        # a truncated summary, so transcript capture needs the complete text).
        self._turn_speech: list[str] = []
```

3b. `iterate()` 在呼叫 `self._llm_iterate(prompt)` 之前(現 `mind_loop.py:229`,即 `try:` 區塊內 render 之後、`_llm_iterate` 之前)加一行清空:

```python
            self._turn_speech.clear()
            # Call LLM (streams text → sink; dispatches tool calls inline)
            await self._llm_iterate(prompt)
```

3c. `_flush_chunker`(`mind_loop.py:594-602`)的迴圈內,在 `sink.put_nowait(TextChunk(text=sentence))` 之後加:

```python
                self._turn_speech.append(sentence)
```

3d. `_handle_stream_event` 的 `SpeakChunk` 分支(`mind_loop.py:615-625`)的迴圈內,在 `sink.put_nowait(TextChunk(text=sentence))` 之後加同一行:

```python
                self._turn_speech.append(sentence)
```

3e. `iterate()` 在 `finally` 區塊之後、`self._state.iter_count += 1`(`mind_loop.py:239`)之前加:

```python
        # Doll-side transcript: one line per turn, full text (B1).
        doll_text = "".join(self._turn_speech).strip().replace("\n", " ")
        if doll_text:
            try:
                await append_transcript(
                    transcripts_root=self._ctx.transcripts_root,
                    memsearch=self._ctx.memsearch,
                    role="doll",
                    text=doll_text,
                )
            except Exception:
                logger.exception("transcript write (doll) failed; continuing")
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_episodic_transcript.py -v`
Expected: PASS(Task 1 + Task 2 兩測試皆綠)。

- [ ] **Step 5: Commit**

```bash
git add src/dollos/mind/mind_loop.py tests/test_episodic_transcript.py
git commit -m "feat(memory): B1 doll-side transcript capture via turn-speech buffer"
```

---

### Task 3: 範圍與健壯性(scope + robustness)

驗證範圍界定(純系統 turn 不寫 user)、成對 ordering、寫入容錯不 crash loop。

**Files:**
- Test: `tests/test_episodic_transcript.py`
- (預期無 production 改動;若某測試失敗,回到 Task 1/2 修正)

**Interfaces:**
- Consumes: Task 1+2 的行為。
- Produces: 無新介面(純驗收)。

- [ ] **Step 1: 寫測試**

附加到 `tests/test_episodic_transcript.py`:

```python
@pytest.mark.asyncio
async def test_system_turn_writes_no_user_line(tmp_path):
    """純系統 perception(非 UserSpoke)+ Doll 主動說話 → 只有 doll 行,無 user 行。"""
    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="ScheduledMoment", t=1.0, data={"text": "鬧鐘"}))
    ms = _FakeMemSearch()
    sink: asyncio.Queue = asyncio.Queue()
    ctx = _make_mind_ctx(tmp_path, memsearch=ms, sink=sink, state=state)
    _QUEUE_HOLDER.append(queue)
    loop = _make_loop(tmp_path, state=state, ctx=ctx, stream=_speak_only_stream("早安"))

    await loop.iterate()

    content = _today_transcript(ctx).read_text()
    assert "主人說：" not in content      # no user line
    assert "我說：早安" in content        # doll line present


@pytest.mark.asyncio
async def test_user_and_doll_lines_paired_in_order(tmp_path):
    """一般對話 turn → user 行在先、doll 行在後。"""
    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "今天好嗎"}))
    ms = _FakeMemSearch()
    sink: asyncio.Queue = asyncio.Queue()
    ctx = _make_mind_ctx(tmp_path, memsearch=ms, sink=sink, state=state)
    _QUEUE_HOLDER.append(queue)
    loop = _make_loop(tmp_path, state=state, ctx=ctx, stream=_speak_only_stream("很好喔"))

    await loop.iterate()

    lines = [ln for ln in _today_transcript(ctx).read_text().split("\n") if ln]
    assert "主人說：今天好嗎" in lines[0]
    assert "我說：很好喔" in lines[1]


@pytest.mark.asyncio
async def test_transcript_write_failure_does_not_crash_loop(tmp_path):
    """index_file 拋例外 → iterate() 不 crash,turn 仍正常完成。"""
    class _RaisingMemSearch(_FakeMemSearch):
        async def index_file(self, path):
            raise RuntimeError("boom")

    state = MindState()
    queue = PerceptionQueue()
    queue.put(Perception(kind="UserSpoke", t=1.0, data={"text": "嗨"}))
    ms = _RaisingMemSearch()
    sink: asyncio.Queue = asyncio.Queue()
    ctx = _make_mind_ctx(tmp_path, memsearch=ms, sink=sink, state=state)
    _QUEUE_HOLDER.append(queue)
    loop = _make_loop(tmp_path, state=state, ctx=ctx, stream=_speak_only_stream("好啊"))

    await loop.iterate()  # must NOT raise

    assert state.iter_count == 1  # turn completed despite transcript failure
```

- [ ] **Step 2: 跑全部測試**

Run: `uv run pytest tests/test_episodic_transcript.py -v`
Expected: 全部 PASS。若 `test_system_turn_writes_no_user_line` 或 ordering 失敗,檢查 Task 1 的 `UserSpoke` 條件;若容錯測試失敗(iterate 拋例外),檢查 Task 1/2 的 `try/except` 是否包住 `await append_transcript`。

- [ ] **Step 3: 跑回歸(確認沒弄壞既有)**

Run: `uv run pytest tests/test_mind_loop.py tests/test_memory_writer.py -v`
Expected: 全部 PASS(既有 mind_loop 與 helper 測試不受影響)。

- [ ] **Step 4: Commit**

```bash
git add tests/test_episodic_transcript.py
git commit -m "test(memory): B1 scope + ordering + write-failure robustness"
```

---

## Self-Review

**1. Spec coverage:**
- §4.1 user 端 → Task 1 ✓
- §4.2 doll 端 buffer(__init__ 初始化 / clear / 兩 emit 點 / turn 結束寫)→ Task 2 ✓
- §4.3 範圍獨立判斷 → Task 3 `test_system_turn_writes_no_user_line` ✓
- §5 容錯不 silent → Task 1/2 `try/except + logger.exception`,Task 3 容錯測試 ✓
- §7 測試(user/doll/純系統/容錯/ordering)→ Task 1-3 全涵蓋 ✓
- §3 不改 helper → 僅改 mind_loop.py,helper 測試回歸(Task 3 Step 3)✓

**2. Placeholder scan:** 無 TBD/TODO;每步有完整碼與指令。✓

**3. Type consistency:** `append_transcript` 簽名(`transcripts_root/memsearch/role/text`)三處呼叫一致;`_turn_speech: list[str]` 定義(Task 2 3a)與使用(3c/3d append、3e join)一致;`self._ctx.transcripts_root` / `self._ctx.memsearch` 與 `MindCtx`(mind_ctx.py:41-43)一致。✓
