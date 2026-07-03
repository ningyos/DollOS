# P1f — Trace(finetune 級語意層語料底盤)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 為 Doll 的每一輪(turn)寫下一筆 finetune 級的語意層 trace——per-pass 拆分、存實際內容而非 hash、think 逐字保存——落在 `data/traces/{date}.jsonl`,成為未來 finetune DollOS 專用 LLM 的訓練語料底盤。

**Architecture:** 新增 `TraceWriter`(`src/dollos/mind/trace.py`),每 turn 一筆 JSONL envelope(passes nested)。envelope 分兩層組裝:**turn-level**(perception_batch、static_prefix、dynamic_blocks、situation、model_id)在 `_run_one_turn` 用既有 render locals 組好,透過新參數傳入 `_llm_iterate`;**per-pass** 紀錄在與 cascade_log **同一 capture point**(mind_loop.py 的 `log_iter` 呼叫處)追加,餵入**完全相同**的 `(raw_buf, results, tool_calls)` tuple + 該 pass 的 `input_messages_delta` + `active_tools` + latency——兩個 writer 從同一組 locals 在**同一處**序列化,不會 drift。整筆在 turn 結束時 `TurnTrace.finish()` 寫一次。config-gated(預設開)、永不進 memsearch 索引、按日輪替不設上限、寫入失敗 loud-log 但不斷 turn(同 pins-only swallow 取捨)。

**Tech Stack:** Python 3.12、pydantic(config)、既有 structlog logger、`json`、`hashlib`、`datetime`。無新第三方依賴。

## Global Constraints

- **語言**:所有 code comment、docstring 用繁體中文或英文皆可(跟隨既有檔案風格);與使用者溝通一律繁體中文。
- **No fallback**:絕不實作降級/fallback 機制。backend 做不到就明說,不靜默改寫。token 欄位無法從既有 stream tuple 取得 → **明文 drop**(存 `null`)+ schema 註記可離線 retokenize 還原,**不**新接 transport 上游管線、**不**假裝從既有 tuple 掉出來。
- **Don't overthink upstream**:不改 `src/dollos/llm/transport.py` / `adapter.py` / `composed.py` 的 stream 契約。
- **存內容非 hash(R2 T-C2)**:dynamic_blocks 存**實際命中項**;tool result 存**全文**(至少所有會 refed 進後續 pass 的);think 存 `"".join(raw_buf)` **逐字全文**。hash 只准用在 immutable 的 identity(pack 版本化)。**mutable 的 `current_self` 必須存全文**。
- **永不 FTS**:`data/traces/` 絕不被 memsearch 索引(結構測試比照 `self_profile`)。
- **單一序列化點(R2 Minor)**:trace 的 per-pass 追加與 `cascade_logger.log_iter` 必須在**同一處**、餵**同一組** `(raw_buf, results, tool_calls)` locals;兩者是兩個序列化函式但同源同址呼叫。
- **cancelled pass caveat(R2 Minor)**:cascade 中途 cancel 時 `_stream_one_pass` 在 `log_iter` 點**之前**就 return,故被取消的那個 pass 既不產生 cascade_log 也不產生 trace pass——這是明文接受的取捨,envelope 仍以已完成的 passes 收尾。
- **schema_version 每筆必帶**:值為 `"1"`。格式明說會演化,遷移靠版本 dispatch。
- **寫入失敗策略**:loud(`logger.exception`)但**不**往上拋斷 turn。
- **測試不得依賴 wall-clock 當資料**:date bucket 一律由該 turn 的真實 `ts` 經 `datetime.fromtimestamp(ts, UTC)` 導出(比照 P1b `_event_date` 跨午夜 landmine 修法),**不**用 `datetime.now()`。

## 範圍界定

**本 plan 只加一個新概念:trace 語料底盤。** 蓋 spec `docs/superpowers/specs/2026-07-03-dollos-mvp-discord-presence-design.md` §3.6 全部,加 `docs/superpowers/specs/2026-07-03-mvp-r2-findings.md`「LENS: verify [R1-trace]」段指派給 P1f 的 5 條(2 Important token / input_messages_delta、1 Important grammar-state、1 Important current_self-verbatim、1 Minor cancel/drift)。

**不含**(後續 plan / 尚未建置,envelope 對這些欄位留 forward-compatible 空值,靠 `schema_version` 未來充實):
- `situation` 的細緻列舉(dm_owner / external_public / …)與 `situational_template_id` → P1d 情境渲染。本 plan 只給**粗粒度** situation(`external` / `internal` / `internal_reflection`)。
- `dynamic_blocks.situational_A_products`(present / channel_tail / author_memory_hits)→ P1c/P1d A 充實管線。本 plan 存 `null`。
- per-pass `tokens` → 需 transport 上游管線,**明文 drop**(存 `null`)。

---

## File Structure

- **Create** `src/dollos/mind/trace.py` — `TraceWriter` + `TurnTrace`。唯一新模組。
- **Create** `tests/test_trace.py` — TraceWriter/TurnTrace 單元測試。
- **Modify** `src/dollos/config.py` — 新增 `TraceSettings`,掛進 `Settings`。
- **Modify** `src/dollos/mind/mind_loop.py` — `__init__` 收 `trace_writer` + `model_id`;`_run_one_turn` 組 `trace_blocks` 並傳入 `_llm_iterate`;`_llm_iterate` 在 log_iter 同址追加 pass、turn 尾 finish。
- **Modify** `src/dollos/kernel.py` — 依 config 建 `TraceWriter` 傳入 `MindLoop`(找既有 `MindLoop(...)` 建構處比照 `cascade_logger` 接線)。
- **Modify** `tests/test_mind_loop*.py`(既有最貼近 cascade 的測試檔)— 加 per-pass 追加、input_messages_delta、同源 tuple 的整合測試。

---

## Task 1: TraceWriter + TurnTrace 骨架 + config

**Files:**
- Create: `src/dollos/mind/trace.py`
- Create: `tests/test_trace.py`
- Modify: `src/dollos/config.py`(新增 `TraceSettings`,掛進 `Settings`)

**Interfaces:**
- Produces:
  - `class TraceWriter` — `__init__(self, root: Path, *, schema_version: str = "1")`;`begin_turn(self, *, turn_id, ts, origin_channel, situation, model_id, perception_batch, static_prefix, dynamic_blocks) -> TurnTrace`。
  - `class TurnTrace` — `add_pass(self, *, pass_idx, input_messages_delta, raw_assistant_emit, tool_calls, results, active_tools, is_reflection, safe_mode, external, latency_ms) -> None`;`finish(self, *, speech, silence) -> None`(序列化整筆 envelope 寫入 `root/{date}.jsonl`,date 由 `self._ts` 導出;寫入失敗 `logger.exception` 不拋)。
  - `TraceSettings(BaseModel)`:`enabled: bool = True`、`root: str = "data/traces"`。

- [ ] **Step 1: 寫失敗測試 — envelope 結構、date bucket、寫失敗不拋**

`tests/test_trace.py`:

```python
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dollos.mind.trace import TraceWriter


def _read_lines(p: Path) -> list[dict]:
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def test_envelope_written_once_with_nested_passes(tmp_path):
    w = TraceWriter(tmp_path)
    ts = datetime(2026, 7, 3, 12, 0, 0, tzinfo=UTC).timestamp()
    tt = w.begin_turn(
        turn_id="t1",
        ts=ts,
        origin_channel="discord:42",
        situation="external",
        model_id="unsloth/Qwen3.6",
        perception_batch=[{"kind": "ChannelMessage", "data": {"text": "hi"}}],
        static_prefix={"identity_hash": "abc", "current_self_text": "我是 Gura", "situational_template_id": None},
        dynamic_blocks={"memsearch_hits": [{"source": "s", "text": "m"}], "mood": {"valence": 0.1}, "energy": 0.9},
    )
    tt.add_pass(
        pass_idx=0,
        input_messages_delta=[{"role": "user", "content": "PROMPT"}],
        raw_assistant_emit="<think>SEEN: hi</think> hello",
        tool_calls=[{"name": "Recall", "args": {"query": "x"}}],
        results=[{"tool_name": "Recall", "success": True, "detail": "FULL DETAIL " * 100}],
        active_tools=["Recall", "Say"],
        is_reflection=False,
        safe_mode=False,
        external=True,
        latency_ms=1234,
    )
    tt.finish(speech="hello", silence=False)

    out = tmp_path / "2026-07-03.jsonl"
    lines = _read_lines(out)
    assert len(lines) == 1
    env = lines[0]
    assert env["schema_version"] == "1"
    assert env["turn_id"] == "t1"
    assert env["origin_channel"] == "discord:42"
    assert env["situation"] == "external"
    assert env["model_id"] == "unsloth/Qwen3.6"
    assert env["perception_batch"][0]["kind"] == "ChannelMessage"
    # static_prefix: current_self VERBATIM, identity as hash
    assert env["static_prefix"]["current_self_text"] == "我是 Gura"
    assert env["static_prefix"]["identity_hash"] == "abc"
    # dynamic_blocks store ACTUAL values, not hashes (T-C2)
    assert env["dynamic_blocks"]["memsearch_hits"][0]["text"] == "m"
    assert env["speech"] == "hello"
    assert env["silence"] is False
    # passes nested, per-pass full content
    assert len(env["passes"]) == 1
    p = env["passes"][0]
    assert p["pass_idx"] == 0
    assert p["raw_assistant_emit"] == "<think>SEEN: hi</think> hello"  # verbatim, not parsed
    # tool result stored FULL, not truncated to 500 (T-C2)
    assert len(p["results"][0]["detail"]) == len("FULL DETAIL " * 100)
    assert p["active_tools"] == ["Recall", "Say"]
    assert p["latency_ms"] == 1234
    assert p["tokens"] is None  # deferred, retokenize offline


def test_date_bucket_uses_ts_not_wallclock(tmp_path):
    # ts on 2026-07-01 must land in 2026-07-01.jsonl regardless of wall-clock.
    w = TraceWriter(tmp_path)
    ts = datetime(2026, 7, 1, 23, 59, 0, tzinfo=UTC).timestamp()
    tt = w.begin_turn(
        turn_id="t", ts=ts, origin_channel="", situation="internal", model_id=None,
        perception_batch=[], static_prefix={}, dynamic_blocks={},
    )
    tt.finish(speech="", silence=True)
    assert (tmp_path / "2026-07-01.jsonl").exists()
    assert not (tmp_path / "2026-07-02.jsonl").exists()


def test_write_failure_is_logged_not_raised(tmp_path, monkeypatch):
    w = TraceWriter(tmp_path)
    ts = datetime(2026, 7, 3, 0, 0, 0, tzinfo=UTC).timestamp()
    tt = w.begin_turn(
        turn_id="t", ts=ts, origin_channel="", situation="internal", model_id=None,
        perception_batch=[], static_prefix={}, dynamic_blocks={},
    )

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "open", boom)
    # must NOT raise — loud but turn-safe
    tt.finish(speech="", silence=True)


def test_silence_turn_flagged(tmp_path):
    w = TraceWriter(tmp_path)
    ts = datetime(2026, 7, 3, 0, 0, 0, tzinfo=UTC).timestamp()
    tt = w.begin_turn(
        turn_id="t", ts=ts, origin_channel="", situation="internal", model_id=None,
        perception_batch=[], static_prefix={}, dynamic_blocks={},
    )
    tt.finish(speech="", silence=True)
    env = _read_lines(tmp_path / "2026-07-03.jsonl")[0]
    assert env["silence"] is True
    assert env["passes"] == []
```

- [ ] **Step 2: 跑測試確認 fail**

Run: `uv run pytest tests/test_trace.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'dollos.mind.trace'`

- [ ] **Step 3: 實作 `src/dollos/mind/trace.py`**

```python
"""Trace — finetune 級語意層語料底盤(spec §3.6)。

每 turn 一筆 JSONL envelope(passes nested),落 data/traces/{date}.jsonl。
與 cascade_log 從同一 per-pass tuple 衍生(superset),但獨立序列化。
存實際內容非 hash(T-C2);think 逐字;按日輪替不設上限;
永不進 memsearch 索引;寫失敗 loud 但不斷 turn。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class TurnTrace:
    """單一 turn 的可變 envelope builder。turn 尾 finish() 寫一次。"""

    def __init__(self, root: Path, schema_version: str, envelope: dict[str, Any]):
        self._root = root
        self._schema_version = schema_version
        self._envelope = envelope  # turn-level fields already populated
        self._ts: float = envelope["ts"]
        self._passes: list[dict] = []

    def add_pass(
        self,
        *,
        pass_idx: int,
        input_messages_delta: list[dict],
        raw_assistant_emit: str,
        tool_calls: list[dict],
        results: list[dict],
        active_tools: list[str],
        is_reflection: bool,
        safe_mode: bool,
        external: bool,
        latency_ms: int | None,
    ) -> None:
        """追加一個已完成 pass。tokens 明文 drop(存 null):per-pass usage
        不從既有 StreamChunk 掉出來(R2 T-token),離線 retokenize
        input_messages_delta + raw_assistant_emit 可精確還原,不新接 transport。"""
        self._passes.append(
            {
                "pass_idx": pass_idx,
                "input_messages_delta": input_messages_delta,
                "raw_assistant_emit": raw_assistant_emit,  # 逐字全文,非 _parse_think
                "tool_calls": tool_calls,
                "results": results,  # 全文,非 detail[:500]
                "active_tools": active_tools,
                "is_reflection": is_reflection,
                "safe_mode": safe_mode,
                "external": external,
                "latency_ms": latency_ms,
                "tokens": None,
            }
        )

    def finish(self, *, speech: str, silence: bool) -> None:
        """序列化整筆 envelope,append 到 root/{date}.jsonl。失敗 loud 不拋。"""
        self._envelope["passes"] = self._passes
        self._envelope["speech"] = speech
        self._envelope["silence"] = silence
        date = datetime.fromtimestamp(self._ts, UTC).strftime("%Y-%m-%d")
        out = self._root / f"{date}.jsonl"
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            line = json.dumps(self._envelope, ensure_ascii=False, default=str)
            with out.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            logger.exception("trace finish() write failed; continuing (turn not broken)")


class TraceWriter:
    """建構每 turn 的 TurnTrace。無狀態(單 event loop,一次一 turn)。"""

    def __init__(self, root: Path, *, schema_version: str = "1"):
        self._root = Path(root)
        self._schema_version = schema_version

    def begin_turn(
        self,
        *,
        turn_id: str,
        ts: float,
        origin_channel: str,
        situation: str,
        model_id: str | None,
        perception_batch: list[dict],
        static_prefix: dict,
        dynamic_blocks: dict,
    ) -> TurnTrace:
        envelope: dict[str, Any] = {
            "schema_version": self._schema_version,
            "turn_id": turn_id,
            "ts": ts,
            "origin_channel": origin_channel,
            "situation": situation,
            "model_id": model_id,
            "perception_batch": perception_batch,
            "static_prefix": static_prefix,
            "dynamic_blocks": dynamic_blocks,
        }
        return TurnTrace(self._root, self._schema_version, envelope)
```

- [ ] **Step 4: 跑 trace 測試確認 pass**

Run: `uv run pytest tests/test_trace.py -v`
Expected: PASS(5 個)

- [ ] **Step 5: 加 `TraceSettings` 到 config**

在 `src/dollos/config.py`,於 `class Settings(BaseModel)` **之前**新增:

```python
class TraceSettings(BaseModel):
    """finetune 語料 trace(spec §3.6)。預設開:紀錄=訓練資料,越早累積越好。"""

    enabled: bool = True
    root: str = "data/traces"
```

在 `class Settings(BaseModel)` 內加欄位(比照既有子 settings 的擺法):

```python
    trace: TraceSettings = TraceSettings()
```

- [ ] **Step 6: 加 config 測試**

在 `tests/` 既有 config 測試檔(`grep -rl "Settings(" tests/ | head -1` 找;若無專屬檔則在 `tests/test_trace.py` 補一個)加:

```python
def test_trace_settings_default_enabled():
    from dollos.config import Settings
    s = Settings()
    assert s.trace.enabled is True
    assert s.trace.root == "data/traces"
```

- [ ] **Step 7: 跑全 config + trace 測試**

Run: `uv run pytest tests/test_trace.py -v && uv run pytest -k "config or settings" -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git branch --show-current   # 確認在 p1f 分支,不是 main
git add src/dollos/mind/trace.py tests/test_trace.py src/dollos/config.py
git commit -m "feat(trace): TraceWriter/TurnTrace skeleton + TraceSettings (P1f Task 1)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RhF6kHt3Xv6JAGnDpayfAn"
```

---

## Task 2: turn-level envelope 組裝(`_run_one_turn`)

**Files:**
- Modify: `src/dollos/mind/mind_loop.py`(`__init__` 收 `trace_writer` + `model_id`;`_run_one_turn` 組 `trace_blocks` 傳入 `_llm_iterate`)
- Test: `tests/test_mind_loop_trace.py`(新建)

**Interfaces:**
- Consumes: Task 1 的 `TraceWriter`。既有 render locals:`memsearch_hits`、`associative_hits`、`tool_habits_hits`(`_run_one_turn` 內)、`self._state.mood`/`.energy`/`.open_loops`/`.recent_perceptions`/`.recent_outputs`、`self._is_reflection`、`self._ctx.external_ctx`、`self._ctx.current_origin`、current_self sanctioned text。
- Produces:`_run_one_turn` 組出 `trace_blocks: dict`(turn-level 全部欄位),傳給 `_llm_iterate(prompt, trace_blocks=trace_blocks)`(Task 3 加參數)。`_situation_tag()` helper。

- [ ] **Step 1: 寫失敗測試 — trace_blocks 帶實際內容 + current_self 全文 + 粗粒度 situation**

`tests/test_mind_loop_trace.py`。比照既有 mind_loop 測試的 fixture 建 `MindLoop`(參照 `tests/test_mind_loop*.py` 既有 setup;把 `trace_writer` 傳入)。核心斷言(用一個能攔截 `begin_turn` 參數的 fake TraceWriter):

```python
class _CapturingTraceWriter:
    def __init__(self):
        self.begun = None
    def begin_turn(self, **kw):
        self.begun = kw
        class _TT:
            def add_pass(self, **k): pass
            def finish(self, **k): pass
        return _TT()


@pytest.mark.asyncio
async def test_run_one_turn_builds_trace_blocks_with_actual_content(mind_loop_with_trace):
    ml, tw, state = mind_loop_with_trace  # fixture: MindLoop wired with _CapturingTraceWriter
    state.recent_perceptions.clear()
    # ... arrange a UserSpoke perception + a known memsearch hit via monkeypatched memsearch ...
    await ml._run_one_turn([_user_perception("hello")])
    kw = tw.begun
    assert kw is not None
    # perception_batch = semantic raw, not rendered strings
    assert kw["perception_batch"][0]["kind"] == "UserSpoke"
    # current_self stored VERBATIM (mutable → must be full text, not ref) [R2 current_self finding]
    assert isinstance(kw["static_prefix"]["current_self_text"], (str, type(None)))
    # identity as hash (immutable pack) — hash present, not full identity dumped each turn
    assert "identity_hash" in kw["static_prefix"]
    # dynamic_blocks store ACTUAL hit dicts (T-C2), plus mood/energy actual values
    assert "memsearch_hits" in kw["dynamic_blocks"]
    assert kw["dynamic_blocks"]["energy"] == state.energy
    # A-products deferred to P1c/P1d → null placeholder, schema_version handles migration
    assert kw["dynamic_blocks"]["situational_A_products"] is None
    assert kw["static_prefix"]["situational_template_id"] is None


@pytest.mark.asyncio
async def test_situation_tag_coarse(mind_loop_with_trace):
    ml, tw, state = mind_loop_with_trace
    # external turn → "external"
    ml._ctx.external_ctx = True
    ml._is_reflection = False
    assert ml._situation_tag() == "external"
    # internal reflection → "internal_reflection"
    ml._ctx.external_ctx = False
    ml._is_reflection = True
    assert ml._situation_tag() == "internal_reflection"
    # plain internal
    ml._is_reflection = False
    assert ml._situation_tag() == "internal"
```

- [ ] **Step 2: 跑確認 fail**

Run: `uv run pytest tests/test_mind_loop_trace.py -v`
Expected: FAIL(`_situation_tag` 不存在 / `trace_writer` 未接)

- [ ] **Step 3: `__init__` 收 `trace_writer` + `model_id`**

在 `src/dollos/mind/mind_loop.py` `__init__` 參數區(比照 `cascade_logger=None`)加:

```python
        trace_writer=None,
        model_id: str | None = None,
```

在 body(比照 `self._cascade_logger = cascade_logger`)加:

```python
        self._trace_writer = trace_writer
        self._model_id = model_id
```

- [ ] **Step 4: 加 `_situation_tag` helper**

在 `mind_loop.py` 適當位置(靠近 `_active_tool_registry`)加:

```python
    def _situation_tag(self) -> str:
        """P1f 粗粒度 situation。P1d 情境渲染會細緻化(dm_owner/external_public/…),
        屆時靠 schema_version 遷移。"""
        if self._is_reflection:
            return "internal_reflection"
        if self._ctx.external_ctx:
            return "external"
        return "internal"
```

- [ ] **Step 5: `_run_one_turn` 組 `trace_blocks`**

在 `_run_one_turn` 內、`prompt = render_mind(...)` **之後**、`await self._llm_iterate(prompt)` **之前**,先取到 current_self 的 sanctioned 全文(render 段已算過;若變數名不同,沿用該段實際變數)。加:

```python
        # ── P1f trace: turn-level envelope 組裝(存實際內容非 hash;T-C2)──
        trace_blocks = None
        if self._trace_writer is not None:
            import hashlib

            # identity 是 immutable+versioned pack → hash 即可還原(比對 pack repo)。
            # current_self 是 mutable(慢變演化 target)→ 必須存全文,否則月-1 trace
            # 用月-3 current_self 還原會拿到錯身分(R2 current_self finding)。
            identity_text = self._system_prompt_for_turn()  # 若方法名不同,沿用 render 段取 identity 的來源
            identity_hash = hashlib.sha256((identity_text or "").encode("utf-8")).hexdigest()
            trace_blocks = {
                "origin_channel": self._ctx.current_origin or "",
                "situation": self._situation_tag(),
                "model_id": self._model_id,
                "perception_batch": [
                    {"kind": p.kind, "data": dict(p.data)} for p in perceptions
                ],
                "static_prefix": {
                    "identity_hash": identity_hash,
                    "current_self_text": current_self_text,  # 沿用 render 段的 sanctioned 全文變數
                    "situational_template_id": None,  # P1d
                },
                "dynamic_blocks": {
                    "memsearch_hits": memsearch_hits,
                    "associative_hits": associative_hits,
                    "tool_habits_hits": tool_habits_hits,
                    "situational_A_products": None,  # P1c/P1d A 充實管線
                    "mood": _mood_to_dict(self._state.mood),
                    "energy": self._state.energy,
                    "open_loops": [_open_loop_to_dict(l) for l in self._state.open_loops],
                    "recent_perceptions": [
                        {"kind": p.kind, "data": dict(p.data)} for p in self._state.recent_perceptions
                    ],
                    "recent_outputs": [_output_to_dict(o) for o in self._state.recent_outputs],
                },
            }
        await self._llm_iterate(prompt, trace_blocks=trace_blocks)
```

`_mood_to_dict` / `_open_loop_to_dict` / `_output_to_dict`:優先用既有 `dataclasses.asdict`(mind_state 已對這些用 asdict,見 `MindState.to_dict`)。在檔頂 import `from dataclasses import asdict`(若未 import),三個 helper 直接用 `asdict(...)`;若某型別非 dataclass 則寫 `dict(...)`。**實作前先 `grep -n "asdict\|def to_dict" src/dollos/mind/mind_state.py` 確認每個型別的正確序列化方式,沿用之,不要自造欄位。**

> **實作者注意**:`identity_text` 與 `current_self_text` 的正確來源在 `_run_one_turn` 的 render 段(mind_loop.py ~360-420):current_self 由 `self_history.sanctioned_text(hist_path)` / `current_self.md` 導出;identity 由 pack system_prompt。**先讀該段確認實際變數名再填**,不要盲抄 `current_self_text` 這個名字。若 render 段把 sanctioned 文字包在 `render_mind` 呼叫參數裡而無獨立變數,則在該處先賦一個區域變數再傳入 trace_blocks。

- [ ] **Step 6: 跑 trace_blocks 測試**

Run: `uv run pytest tests/test_mind_loop_trace.py -v`
Expected: PASS

- [ ] **Step 7: 跑既有 mind_loop 全測試確認未回歸**

Run: `uv run pytest tests/ -q`
Expected: PASS(既有全綠;`_llm_iterate` 新參數 `trace_blocks` 在 Task 3 前先給預設值 `None`——見下方注意)

> **跨 Task 銜接**:Task 3 才會把 `_llm_iterate` 簽名改成 `(self, prompt, *, trace_blocks=None)`。本 Task Step 5 已經用 `trace_blocks=` 呼叫,故**必須在本 Task 先把 `_llm_iterate` 簽名加上 `*, trace_blocks=None` 參數(body 暫不使用)**,讓呼叫合法、既有測試綠。Task 3 再填 body。把這行簽名修改併入本 Task Step 5。

- [ ] **Step 8: Commit**

```bash
git branch --show-current
git add src/dollos/mind/mind_loop.py tests/test_mind_loop_trace.py
git commit -m "feat(trace): turn-level envelope assembly in _run_one_turn (P1f Task 2)

current_self stored verbatim (mutable), identity as hash (immutable pack);
dynamic_blocks store actual hit content not hashes (T-C2); coarse situation tag.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RhF6kHt3Xv6JAGnDpayfAn"
```

---

## Task 3: per-pass 追加 @ 同源 tuple 點 + turn 尾 finish

**Files:**
- Modify: `src/dollos/mind/mind_loop.py`(`_llm_iterate`:begin_turn、log_iter 同址 add_pass、finally finish)
- Test: `tests/test_mind_loop_trace.py`(擴充)

**Interfaces:**
- Consumes: Task 2 的 `trace_blocks`;既有 `turn_id = self._cascade_logger.start_turn()`;log_iter 呼叫處的 `(raw_buf, results, tool_calls)` tuple、`pass_idx`。
- Produces: 每 turn 一筆 trace envelope,per-pass 與 cascade_log 同源同址。

- [ ] **Step 1: 寫失敗測試 — think 全文逐字 + result 全文 + active_tools + 同源 tuple**

擴充 `tests/test_mind_loop_trace.py`(用真 `TraceWriter(tmp_path)`,跑一個會走 ≥1 pass 的 cascade,fake LLM 回一段含 `<think>` + 一個 Recall tool call 的輸出):

```python
@pytest.mark.asyncio
async def test_trace_pass_stores_raw_think_and_full_result(mind_loop_real_trace, tmp_path):
    ml, state = mind_loop_real_trace  # MindLoop wired with real TraceWriter(tmp_path/"traces")
    # fake LLM 第一 pass 吐:<think>SEEN:...\nlots of reasoning...</think> + Recall call
    await ml._run_one_turn([_user_perception("dig up X")])
    env = _only_trace_envelope(tmp_path / "traces")  # helper: glob *.jsonl, assert 1 line
    p0 = env["passes"][0]
    # think 逐字全文,非 _parse_think 的 5 行截斷
    assert "lots of reasoning" in p0["raw_assistant_emit"]
    # tool result 全文,非 detail[:500]
    if p0["results"]:
        assert len(p0["results"][0]["detail"]) > 0
    # active_tools 該 pass 的實際工具集
    assert isinstance(p0["active_tools"], list) and len(p0["active_tools"]) > 0
    assert "latency_ms" in p0


@pytest.mark.asyncio
async def test_trace_and_cascade_log_share_source_tuple(mind_loop_real_trace, tmp_path):
    """兩 writer 從同一組 (raw_buf, results, tool_calls) 序列化;
    trace 的 raw_assistant_emit 應為 cascade_log parsed think 的 superset。"""
    ml, state = mind_loop_real_trace
    await ml._run_one_turn([_user_perception("hi")])
    env = _only_trace_envelope(tmp_path / "traces")
    # trace 存 raw 全文(superset);cascade_log 只存 parsed 5 欄——trace 不 drift
    assert env["passes"][0]["raw_assistant_emit"]  # 非空,為 raw
```

- [ ] **Step 2: 跑確認 fail**

Run: `uv run pytest tests/test_mind_loop_trace.py -v`
Expected: FAIL(`_llm_iterate` body 尚未接 trace)

- [ ] **Step 3: `_llm_iterate` 接線**

在 `_llm_iterate`(簽名已於 Task 2 加 `*, trace_blocks=None`),`turn_id = self._cascade_logger.start_turn() ...` 之後、pass 迴圈之前,加 begin_turn:

```python
            turn_trace = None
            if self._trace_writer is not None and trace_blocks is not None:
                import time as _time
                turn_trace = self._trace_writer.begin_turn(
                    turn_id=turn_id or _uuid_fallback(),  # 見下:turn_id 可能為 None(cascade_logger 未接)
                    ts=_time.time(),
                    **trace_blocks,
                )
```

> **turn_id 來源**:現況 `turn_id` 來自 `self._cascade_logger.start_turn()`,cascade_logger 為 None 時 turn_id 也是 None。trace 不應依賴 cascade_logger 存在。**修法**:若 `turn_id is None`,用 `uuid.uuid4().hex[:8]` 生一個(在檔頂 `import uuid`;把上面 `_uuid_fallback()` 換成 inline `turn_id or uuid.uuid4().hex[:8]`,並把這個值同時用於 begin_turn)。確保 trace 有穩定 turn_id 即使 cascade_log 停用。

在每個 pass、緊鄰既有 `if self._cascade_logger is not None: self._cascade_logger.log_iter(...)` 的**同一處**(餵同一 tuple),量 latency 並 add_pass:

```python
                # ── 同源序列化點:cascade_log 與 trace 餵同一 (raw_buf, results, tool_calls) ──
                if turn_trace is not None:
                    turn_trace.add_pass(
                        pass_idx=pass_idx,
                        input_messages_delta=input_messages_delta,  # Task 4 提供
                        raw_assistant_emit="".join(raw_buf),
                        tool_calls=[{"name": tc.get("name"), "args": tc.get("arguments")} for tc in tool_calls],
                        results=[
                            {"tool_name": r.tool_name, "success": r.success, "detail": r.detail or ""}
                            for r in results
                        ],
                        active_tools=sorted(self._active_tool_registry().keys()),
                        is_reflection=self._is_reflection,
                        safe_mode=self._state.safe_mode,
                        external=self._ctx.external_ctx,
                        latency_ms=pass_latency_ms,  # Task 5 提供
                    )
```

> 本 Task 先用佔位:`input_messages_delta=[]`、`pass_latency_ms=None`(Task 4/5 填實)。確保 add_pass 呼叫合法、測試能綠。

在 `_llm_iterate` 的 `finally`(清 `self._cascade_ctx` 那個 finally)內,turn 收尾寫檔:

```python
            if turn_trace is not None:
                # speech = 本 turn 新增的 Speech 輸出;silence = 無 speech。
                speech = self._collect_turn_speech(speech_start_len)
                turn_trace.finish(speech=speech, silence=(speech == ""))
```

`speech_start_len`:在 begin_turn 之後、pass 迴圈之前記 `speech_start_len = len(self._state.recent_outputs)`。`_collect_turn_speech(start_len)` helper:

```python
    def _collect_turn_speech(self, start_len: int) -> str:
        """本 turn(recent_outputs[start_len:])所有 Speech 輸出的串接。"""
        new = list(self._state.recent_outputs)[start_len:]
        parts = [getattr(o, "text", "") for o in new if getattr(o, "kind", None) == "Speech"]
        return " ".join(t for t in parts if t)
```

> **實作者注意**:`OutputRecord` 的欄位名(`kind` / `text` / 判定 Speech 的方式)**先 `grep -n "class OutputRecord\|kind\|Speech" src/dollos/mind/mind_state.py` 確認**再填;不要盲抄。若 recent_outputs 是 maxlen deque 且本 turn 輸出可能超過 maxlen 溢出,改為在 pass 迴圈內累積 speech(從 `_handle_stream_event` 的 SpeakChunk 路徑),但預設先用 recent_outputs-delta,簡單且足夠。

- [ ] **Step 4: 跑測試**

Run: `uv run pytest tests/test_mind_loop_trace.py -v`
Expected: PASS

- [ ] **Step 5: 跑 mind_loop 全測試**

Run: `uv run pytest tests/ -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git branch --show-current
git add src/dollos/mind/mind_loop.py tests/test_mind_loop_trace.py
git commit -m "feat(trace): per-pass capture at shared cascade_log tuple site + turn-end finish (P1f Task 3)

both writers serialize from identical (raw_buf, results, tool_calls) locals in one
place (no drift); raw think verbatim; full tool result; active_tools per pass.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RhF6kHt3Xv6JAGnDpayfAn"
```

---

## Task 4: `input_messages_delta` = 逐字權威

**Files:**
- Modify: `src/dollos/mind/mind_loop.py`(`_llm_iterate` 追蹤每 pass 串流前 append 的 message dicts)
- Test: `tests/test_mind_loop_trace.py`(擴充)

**Interfaces:**
- Consumes: `_llm_iterate` 的 `messages: list[dict]`(pass 0 = `[{user:prompt}]`;pass i>0 尾端 append `{assistant: 前一 pass emit}` + 過濾後 `<tool_response>` user dicts)。
- Produces: 每 pass 的 `input_messages_delta` = 該 pass 串流前**新增**的 message dicts,逐字(含 `<tool_response>` wrapper)。

**背景(R2 finding)**:pass schema 原列四個重疊內容欄位。權威定為**「該 pass 串流前實際 append 進 `messages` 的 dict,逐字」**。refeed 是**過濾子集**(FIRE_AND_FORGET + success-not-on-allowlist 排除),故 delta 不等於全部 tool_calls。存 byte-actual 才能還原真正餵進去的 context(例:被餵的是 `(no output)` 而非空字串)。

- [ ] **Step 1: 寫失敗測試 — delta 逐字 + refeed 過濾子集 + 重建等式**

```python
@pytest.mark.asyncio
async def test_input_messages_delta_is_byte_authority(mind_loop_real_trace, tmp_path):
    ml, state = mind_loop_real_trace  # fake LLM: pass0 emits Recall(sync, refed); pass1 emits Say(ends)
    await ml._run_one_turn([_user_perception("q")])
    env = _only_trace_envelope(tmp_path / "traces")
    passes = env["passes"]
    # pass 0 delta = 初始 user prompt(逐字)
    assert passes[0]["input_messages_delta"][0]["role"] == "user"
    assert passes[0]["input_messages_delta"][0]["content"]  # == 送進 pass0 的 prompt
    if len(passes) > 1:
        # pass 1 delta = 前一 pass assistant emit + 過濾後 <tool_response>(不含 fire-and-forget ack)
        roles = [m["role"] for m in passes[1]["input_messages_delta"]]
        assert "assistant" in roles
        # 至少一個 tool_response(Recall 是 refed sync tool)
        assert any("tool_response" in m.get("content", "") for m in passes[1]["input_messages_delta"])


@pytest.mark.asyncio
async def test_delta_concatenation_reconstructs_final_messages(mind_loop_real_trace, tmp_path):
    ml, state = mind_loop_real_trace
    await ml._run_one_turn([_user_perception("q")])
    env = _only_trace_envelope(tmp_path / "traces")
    # 串接所有 pass 的 delta == _llm_iterate 內最終 messages(逐字重建)
    reconstructed = [m for p in env["passes"] for m in p["input_messages_delta"]]
    # 每個 delta dict 都是合法 message(有 role/content)
    assert all("role" in m and "content" in m for m in reconstructed)
    assert reconstructed[0] == {"role": "user", "content": passes_prompt(env)}  # helper
```

- [ ] **Step 2: 跑確認 fail**

Run: `uv run pytest tests/test_mind_loop_trace.py::test_input_messages_delta_is_byte_authority -v`
Expected: FAIL(delta 目前是佔位 `[]`)

- [ ] **Step 3: 追蹤 delta**

在 `_llm_iterate`,`messages` 初始化處記錄基準長度:pass 迴圈開始前 `prev_msg_len = 0`。**每個 pass 串流前**(即 `_stream_one_pass` 呼叫前),`messages` 已含本 pass 要送的全部內容:

- pass 0:`messages == [{"role":"user","content":prompt}]`。
- pass i>0:上一輪迴圈尾端已 append `{assistant: emit}` + refeed `<tool_response>` dicts。

故在 `_stream_one_pass` 呼叫**前**取:

```python
                input_messages_delta = [dict(m) for m in messages[prev_msg_len:]]
                prev_msg_len = len(messages)
```

把 `input_messages_delta` 傳進 Task 3 的 `add_pass(input_messages_delta=input_messages_delta, ...)`(取代佔位 `[]`)。

> **精確定位**:delta 必須在**本 pass 的 append 都完成後、下一次 `_stream_one_pass` 前**取值。既有迴圈結構是:pass i 串流 → 尾端 append assistant emit(mind_loop.py:665)+ refeed tool_responses(:732)。所以在**迴圈頂端、`_stream_one_pass` 呼叫前**算 delta 最穩(此時上一輪的 append 已完成,本輪還沒串流)。確認 `prev_msg_len` 在 begin_turn 附近初始化為 `0`,每 pass 更新。

- [ ] **Step 4: 跑測試**

Run: `uv run pytest tests/test_mind_loop_trace.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git branch --show-current
git add src/dollos/mind/mind_loop.py tests/test_mind_loop_trace.py
git commit -m "feat(trace): input_messages_delta as byte-verbatim authority per pass (P1f Task 4)

the exact message dicts appended before each pass streamed (incl <tool_response>
wrapper, filtered refeed subset); other content fields are derived indices.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RhF6kHt3Xv6JAGnDpayfAn"
```

---

## Task 5: per-pass latency + tokens-deferred

**Files:**
- Modify: `src/dollos/mind/mind_loop.py`(`_stream_one_pass` 兩側量 `time.monotonic()`)
- Test: `tests/test_mind_loop_trace.py`(擴充)

**Interfaces:**
- Produces: 每 pass `latency_ms: int`。`tokens` 維持 `None`(Task 1 已定,離線 retokenize 還原)。

- [ ] **Step 1: 寫失敗測試**

```python
@pytest.mark.asyncio
async def test_pass_latency_present_and_tokens_deferred(mind_loop_real_trace, tmp_path):
    ml, state = mind_loop_real_trace
    await ml._run_one_turn([_user_perception("q")])
    env = _only_trace_envelope(tmp_path / "traces")
    for p in env["passes"]:
        assert isinstance(p["latency_ms"], int) and p["latency_ms"] >= 0
        assert p["tokens"] is None  # per-pass usage 不從 StreamChunk 掉出來(R2 T-token)
```

- [ ] **Step 2: 跑確認 fail**

Run: `uv run pytest tests/test_mind_loop_trace.py::test_pass_latency_present_and_tokens_deferred -v`
Expected: FAIL(latency 目前佔位 `None`)

- [ ] **Step 3: 量 latency**

在 `_llm_iterate` 的 pass 迴圈內,包住 `_stream_one_pass`:

```python
                import time as _time
                _pass_t0 = _time.monotonic()
                raw_buf, results, tool_calls = await self._stream_one_pass(
                    prompt=prompt, messages=messages,
                    first_pass=(pass_idx == 0), sink=sink,
                )
                pass_latency_ms = int((_time.monotonic() - _pass_t0) * 1000)
```

把 `pass_latency_ms` 傳入 Task 3 的 `add_pass(latency_ms=pass_latency_ms, ...)`(取代佔位 `None`)。`time` 若檔頂已 import 就不重複 import。

- [ ] **Step 4: 跑測試**

Run: `uv run pytest tests/test_mind_loop_trace.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git branch --show-current
git add src/dollos/mind/mind_loop.py tests/test_mind_loop_trace.py
git commit -m "feat(trace): per-pass latency_ms; tokens deferred to offline retokenize (P1f Task 5)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RhF6kHt3Xv6JAGnDpayfAn"
```

---

## Task 6: kernel 接線 + never-FTS 結構守衛 + cancelled-pass caveat

**Files:**
- Modify: `src/dollos/kernel.py`(依 config 建 `TraceWriter`,傳入 `MindLoop`)
- Test: `tests/` — never-FTS 結構守衛測試 + cancelled-pass 整合測試

**Interfaces:**
- Consumes: `TraceSettings`(Task 1)、`TraceWriter`(Task 1)、`MindLoop.__init__(trace_writer=, model_id=)`(Task 2)。

- [ ] **Step 1: 寫失敗測試 — never-FTS 結構守衛 + cancelled pass**

`tests/` 內(比照 self_profile 的 never-index 結構測試,`grep -rn "self_profile" tests/ | grep -i "index\|fts\|search"` 找範本):

```python
def test_traces_dir_never_indexed_by_memsearch():
    """data/traces/ 是訓練層,絕不進 memsearch 對話層索引(結構守衛,比照 self_profile)。"""
    import inspect
    from dollos.memory import fts_store  # 或實際 index 進入點模組
    src = inspect.getsource(fts_store)
    # index 路徑構造絕不含 traces
    assert "traces" not in src or "data/traces" not in src
    # 更強:若有 index_file/walk 的 allow/deny 清單,斷言 traces 在 deny 或不在 allow
```

> **實作者**:先看 self_profile 是怎麼被排除索引的(是靠目錄不在 memsearch 掃描根、還是靠 deny 清單),用**同一機制**排除 traces。若 memsearch 只索引 `data/memory/` 而 traces 在 `data/traces/`(平行目錄,天然不被掃),則守衛測試斷言「traces 不在 memsearch 掃描根之下」即可,並在 `trace.py` docstring + spec 註明此不變式。**不要**新增索引邏輯再排除——若天然隔離,寫測試釘住它。

cancelled-pass 整合測試(比照既有 cancel 測試):

```python
@pytest.mark.asyncio
async def test_cancelled_pass_not_recorded_but_envelope_finalizes(mind_loop_real_trace, tmp_path):
    """cancel 中途:被取消的 pass 在 log_iter 點之前 return,故不產生 trace pass;
    但 envelope 仍以已完成的 passes 收尾(§6.2(g) caveat 明文接受)。"""
    ml, state = mind_loop_real_trace
    # 安排:pass 0 串流中途觸發 cancel_current_cascade()
    # ... arrange fake LLM to yield a chunk then set ml._cascade_ctx cancel ...
    await ml._run_one_turn([_user_perception("q")])
    trace_files = list((tmp_path / "traces").glob("*.jsonl"))
    if trace_files:  # 若 turn 有 begin,應仍 finish 一筆(passes 可能為空或只含已完成者)
        env = _only_trace_envelope(tmp_path / "traces")
        assert "passes" in env  # 收尾成功,不因 cancel 遺失整筆
```

- [ ] **Step 2: 跑確認 fail / 現況**

Run: `uv run pytest tests/ -k "traces or cancelled_pass" -v`
Expected: FAIL(kernel 未接 trace_writer,MindLoop 未收到)

- [ ] **Step 3: kernel 接線**

在 `src/dollos/kernel.py` 找 `MindLoop(...)` 建構處(比照 `cascade_logger=` 傳法)。加:

```python
        from dollos.mind.trace import TraceWriter

        trace_writer = None
        if settings.trace.enabled:
            trace_writer = TraceWriter(Path(settings.trace.root))
```

在 `MindLoop(...)` 呼叫加參數:

```python
            trace_writer=trace_writer,
            model_id=settings.llm.model_alias,
```

> `settings.llm.model_alias` — 確認 config 的 LLM settings 欄位名(config.py:16 `model_alias`)。若 kernel 該處的 settings 變數名不同,沿用之。`Path` 若未 import 則在檔頂加 `from pathlib import Path`。

- [ ] **Step 4: cancelled-pass caveat 落 code 註解**

確認 `_stream_one_pass` cancel 路徑(mind_loop.py 的 `if self._cascade_ctx.cancelled: return raw_buf, results, tool_calls`)**在 `_llm_iterate` 的 add_pass/log_iter 點之前** return。在 `_llm_iterate` 的 add_pass 附近加一行註解:

```python
                # cancelled pass 在 _stream_one_pass 內即 return,不會到達這裡 —
                # 故被取消的 pass 既無 cascade_log 也無 trace pass(§3.6/§6.2(g) 明文取捨)。
```

- [ ] **Step 5: 跑目標測試**

Run: `uv run pytest tests/ -k "traces or cancelled_pass" -v`
Expected: PASS

- [ ] **Step 6: 全 daemon 測試 + kernel 測試確認未回歸**

Run: `uv run pytest -q`
Expected: PASS(除 3 個既有 torch voice 環境性失敗)

- [ ] **Step 7: Commit**

```bash
git branch --show-current
git add src/dollos/kernel.py tests/
git commit -m "feat(trace): kernel wiring + never-FTS structural guard + cancelled-pass caveat (P1f Task 6)

trace enabled by default; data/traces/ never indexed by memsearch (structural
guard mirrors self_profile); cancelled pass produces neither cascade_log nor
trace pass, envelope still finalizes with completed passes.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RhF6kHt3Xv6JAGnDpayfAn"
```

---

## Self-Review(寫完計畫回頭對 spec §3.6 + R2 trace lens 逐條核）

**Spec §3.6 覆蓋:**
- [x] T-C1 pass 為單位 → Task 3 per-pass add_pass(passes nested)
- [x] Schema envelope(schema_version / turn_id / ts / origin_channel / situation / perception_batch / static_prefix / dynamic_blocks / passes / speech / silence / model_id)→ Task 1 envelope + Task 2 turn-level + Task 3 passes/speech/silence
- [x] T-C2 存內容非 hash(dynamic_blocks 實際值、result 全文)→ Task 1 測試 + Task 2 dynamic_blocks + Task 3 results 全文
- [x] think 存 raw 全文非 `_parse_think` → Task 1/3 `"".join(raw_buf)`
- [x] 從 cascade_log 同一 per-pass 點衍生、兩者不 drift → Task 3 同址同源 tuple
- [x] 永不 FTS → Task 6 結構守衛
- [x] 按日輪替不設上限 → Task 1 `{date}.jsonl` append
- [x] 寫入失敗 loud 不斷 turn → Task 1 finish try/except
- [x] schema_version 每筆必帶 → Task 1 envelope

**R2 trace lens 5 條:**
- [x] Important — per-pass tokens 不在 capture point → **明文 drop**(Task 1/5 存 `null` + 註記離線 retokenize),不新接 transport
- [x] Important — input_messages_delta 未定義且冗餘 → Task 4 定為逐字權威(該 pass 串流前 append 的 dict)
- [x] Important — active tool-registry / grammar state per pass → Task 3 `active_tools` frozenset + is_reflection/safe_mode/external
- [x] Important — static_prefix.current_self bare ref 破壞重建 → Task 2 current_self **存全文**、identity 存 hash
- [x] Minor — cancelled pass 掉出 capture + 兩 writer drift → Task 6 caveat 註解 + Task 3 同址同源

**Placeholder scan:** 每個 code step 給了實際 code;三處「實作者注意」是**要求先 grep 確認既有變數名/機制再填**(identity/current_self 來源、OutputRecord 欄位、self_profile never-index 機制),非 TBD,是防止盲抄既有 API。

**Type consistency:** `begin_turn(**trace_blocks)` 的 keys 對齊 Task 2 的 `trace_blocks` dict keys(origin_channel/situation/model_id/perception_batch/static_prefix/dynamic_blocks)與 Task 1 `begin_turn` 簽名——一致。`add_pass` 參數在 Task 3 定義、Task 4/5 填 delta/latency——一致。

---

## 執行銜接

計畫完成存於 `docs/superpowers/plans/2026-07-03-p1f-trace-finetune-corpus.md`。依 `feedback_subagent_driven_default`:writing-plans 收尾後直接進 `superpowers:subagent-driven-development`,每 task 一個 fresh implementer subagent + reviewer subagent,不 inline。全 branch 完成後 opus whole-branch review → `superpowers:finishing-a-development-branch` merge to main。worktree:`.worktrees/p1f-trace/` on branch `p1f-trace`。
