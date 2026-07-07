# 延遲壓縮 Part 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 綁定 deliberate think 行長度以物理消滅實測 8s+ idle 長尾，並加 turn 級 think/speak/first-speak 分離遙測，讓 Part 2（reflex）有真 chat 資料且 ≤2s 可驗收。

**Architecture:** 三個獨立可 merge 的改動：(1) `build_voice_first_grammar` 的 `line` 由無上限改綁定，`REVIEW` 給較寬的 `rline`（因它持久化進 `recent_reviews`）；(2) 新 `TurnLatencyRecorder` + 在 `_llm_iterate` 量 turn 級開口時刻與 think/speak 字元數；(3) 啟動時對 llama-server 送一次 `{1,n}` grammar 探測，過舊即 fail-closed，不讓每回合請求級 HTTP error 假扮成功。

**Tech Stack:** Python 3.12、GBNF（llama.cpp bounded repetition `{m,n}`）、pytest、既有 `TelemetryRecorder` daily-JSONL 模式。

## Global Constraints

- **只綁 think，絕不綁 `speak`**：`speak ::= [^<]+` 維持無上限（不綁她講多少話）。— spec §3.1
- **REVIEW 不可硬切**：它寫進 `self._state.recent_reviews` 被回注，用較寬 `_REVIEW_LINE_CAP=120`，其餘 think 行 `_THINK_LINE_CAP=64`。— spec §3.1 / R1-8
- **不動 `build_qwen3_think_tool_grammar`**（subagent 專用，非 live 延遲路徑）。— spec §2.1
- **no-fallback**：grammar build 失敗 raise；遙測寫入失敗**只 log 不 raise**（遙測不可拖垮 cascade，沿用 `TelemetryRecorder.record` 既有語意）。— spec §11
- **遙測 epoch 明確**：`first_speak_ms` 相對 `_llm_iterate` 進入時刻，turn 級（非 per-call）。— spec §3.2 / R1-4
- **line-cap 為 module 常數**，不做 config。— spec §2.1
- 全套測試須綠：`uv run pytest`（現況 1598 passed）。

---

### Task 1: deliberate think 行長綁定（`line` + 較寬 `rline`）

**Files:**
- Modify: `src/dollos/llm/templates.py`（`build_voice_first_grammar` 約 379-421 的 `head`）
- Test: `tests/test_voice_first_grammar.py`（若不存在則建立；先 grep 確認既有測試檔名）

**Interfaces:**
- Consumes: 既有 `build_voice_first_grammar(tools: list[type[BaseModel]]) -> str`、`_build_tool_call_rule`、`_JSON_STR_RULES`。
- Produces: 同簽名不變的 `build_voice_first_grammar`（Part 1 **不加 mode 參數** — 留給 Part 2）；新 module 常數 `_THINK_LINE_CAP = 64`、`_REVIEW_LINE_CAP = 120`。

- [ ] **Step 1: 先看現況並找測試檔**

Run: `grep -rn "build_voice_first_grammar" tests/ && sed -n '379,421p' src/dollos/llm/templates.py`
確認：目前 `line ::= [^\n]+ "\n"`、`think` 五行都用 `line`；找到既有的 voice grammar 測試檔（放新測試於此，無則建 `tests/test_voice_first_grammar.py`）。

- [ ] **Step 2: 寫 failing test**

```python
# tests/test_voice_first_grammar.py
from pydantic import BaseModel, Field
from dollos.llm.templates import build_voice_first_grammar, _THINK_LINE_CAP, _REVIEW_LINE_CAP

class _Dummy(BaseModel):
    text: str = Field(...)

def test_think_lines_are_length_bounded():
    g = build_voice_first_grammar([_Dummy])
    # 一般 think 行綁 _THINK_LINE_CAP
    assert f'line ::= [^\\n]{{1,{_THINK_LINE_CAP}}} "\\n"' in g
    # 舊的無上限規則不得殘留
    assert 'line ::= [^\\n]+ "\\n"' not in g

def test_review_uses_wider_rline_rule():
    g = build_voice_first_grammar([_Dummy])
    assert f'rline ::= [^\\n]{{1,{_REVIEW_LINE_CAP}}} "\\n"' in g
    # think 規則裡 REVIEW 用 rline、其餘用 line
    assert '"REVIEW: " rline "MOOD: " line' in g
    assert '"SEEN: " line "INTENT: " line "TOOL: " line' in g

def test_speak_stays_unbounded():
    g = build_voice_first_grammar([_Dummy])
    assert 'speak ::= [^<]+' in g   # 絕不綁口語長度

def test_caps_are_sane_defaults():
    assert _THINK_LINE_CAP == 64
    assert _REVIEW_LINE_CAP == 120
```

- [ ] **Step 3: 跑測試確認 fail**

Run: `uv run pytest tests/test_voice_first_grammar.py -v`
Expected: FAIL（`_THINK_LINE_CAP` import 不存在 / 斷言不符）

- [ ] **Step 4: 實作**

在 `templates.py` 適當處（其他 module 常數附近）加：
```python
# Deliberate think 行長上限（codepoint）。斬 idle-turn 的 think 空轉長尾。
# 一般行 64 對正常推理夠用；REVIEW 較寬（會持久化進 recent_reviews，避免語意殘缺）。
_THINK_LINE_CAP = 64
_REVIEW_LINE_CAP = 120
```
把 `build_voice_first_grammar` 的 `head` 改為：
```python
    head = (
        "root ::= think segments\n"
        'think ::= "SEEN: " line "INTENT: " line "TOOL: " line '
        '"REVIEW: " rline "MOOD: " line "</think>\\n\\n"\n'
        f'line ::= [^\\n]{{1,{_THINK_LINE_CAP}}} "\\n"\n'
        f'rline ::= [^\\n]{{1,{_REVIEW_LINE_CAP}}} "\\n"\n'
        "segments ::= segment*\n"
        "segment ::= speak | tool-call\n"
        "speak ::= [^<]+\n"
        f"tool-call ::= {tool_call_alts}\n"
    )
```

- [ ] **Step 5: 跑測試確認 pass + 全套回歸**

Run: `uv run pytest tests/test_voice_first_grammar.py -v && uv run pytest -q`
Expected: 新測試 PASS；全套仍綠（注意：若有既有測試硬編 `[^\n]+ "\n"` 斷言，更新它以反映綁定——那是預期的行為變更，非破壞）。

- [ ] **Step 6: Commit**

```bash
git add src/dollos/llm/templates.py tests/test_voice_first_grammar.py
git commit -m "feat(grammar): bound deliberate think line length (kill idle think tail)"
```

---

### Task 2: turn 級 think/speak/first-speak 分離遙測

**Files:**
- Create: `src/dollos/telemetry/turn_latency.py`
- Modify: `src/dollos/telemetry/__init__.py`（export 新類別）
- Modify: `src/dollos/mind/mind_loop.py`（`__init__` 收 recorder；`_llm_iterate` 量測與記錄；`_emit_sentence` 記第一句時刻）
- Modify: `src/dollos/kernel.py`（建 recorder 並注入 MindLoop — 先 grep 確認 MindLoop 建構點與既有 `TelemetryRecorder` 如何注入）
- Test: `tests/test_turn_latency.py`（新）

**Interfaces:**
- Consumes: 既有 `time.time()`、`SpeakChunk`、`self._turn_speech`、`_llm_iterate`。
- Produces:
  - `TurnLatencyRecord`（dataclass，欄位見下）+ `TurnLatencyRecorder`（`__init__(dir_path: Path)`；`async record(rec: TurnLatencyRecord) -> None`，寫 `turn_latency-YYYY-MM-DD.jsonl`，失敗只 log）。
  - MindLoop 上：turn 起始 `self._turn_t0: float | None`、`self._turn_first_speak_ms: float | None`、accumulator `self._turn_think_chars: int`、`self._turn_speak_chars: int`。

- [ ] **Step 1: 寫 recorder 的 failing test**

```python
# tests/test_turn_latency.py
import json
from pathlib import Path
import pytest
from dollos.telemetry.turn_latency import TurnLatencyRecord, TurnLatencyRecorder

@pytest.mark.asyncio
async def test_record_appends_jsonl(tmp_path: Path):
    rec = TurnLatencyRecorder(tmp_path)
    r = TurnLatencyRecord(ts=1780000000.0, first_speak_ms=1840.0,
                          think_chars=90, speak_chars=40, total_ms=2100.0,
                          ttft_ms=1600.0, mode="deliberate", n_passes=1,
                          had_tool_call=False)
    await rec.record(r)
    files = list(tmp_path.glob("turn_latency-*.jsonl"))
    assert len(files) == 1
    d = json.loads(files[0].read_text(encoding="utf-8").strip())
    assert d["first_speak_ms"] == 1840.0
    assert d["think_chars"] == 90 and d["speak_chars"] == 40
    assert d["mode"] == "deliberate"

@pytest.mark.asyncio
async def test_record_never_raises_on_bad_dir(tmp_path: Path):
    # 指向一個是檔案的路徑，mkdir 會失敗——record 必須吞掉、不 raise
    bad = tmp_path / "afile"
    bad.write_text("x")
    rec = TurnLatencyRecorder(bad)
    r = TurnLatencyRecord(ts=1.0, first_speak_ms=None, think_chars=0,
                          speak_chars=0, total_ms=1.0, ttft_ms=None,
                          mode="deliberate", n_passes=1, had_tool_call=False)
    await rec.record(r)   # 不得 raise
```

- [ ] **Step 2: 跑測試確認 fail**

Run: `uv run pytest tests/test_turn_latency.py -v`
Expected: FAIL（module 不存在）

- [ ] **Step 3: 實作 recorder（仿 `telemetry/llm_calls.py` 模式）**

```python
# src/dollos/telemetry/turn_latency.py
"""TurnLatencyRecorder — turn 級延遲遙測（append-only daily JSONL）。

每回合一筆，epoch = _llm_iterate 進入時刻。用於：Part 2 起手前讀真 chat-turn
think 大小與 first_speak 分佈；reflex A/B 驗收（spec §3.2/§7）。
無 fake fallback：模型沒回的欄位留 None；寫入失敗只 log 不 raise。
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class TurnLatencyRecord:
    ts: float
    first_speak_ms: float | None
    think_chars: int
    speak_chars: int
    total_ms: float | None
    ttft_ms: float | None
    mode: str          # "deliberate" | "reflex"（Part 1 恆 "deliberate"）
    n_passes: int
    had_tool_call: bool

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


class TurnLatencyRecorder:
    def __init__(self, dir_path: Path) -> None:
        self._dir = Path(dir_path)
        self._lock = asyncio.Lock()

    async def record(self, rec: TurnLatencyRecord) -> None:
        async with self._lock:
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
                day = datetime.fromtimestamp(rec.ts).date()
                path = self._dir / f"turn_latency-{day:%Y-%m-%d}.jsonl"
                with path.open("a", encoding="utf-8") as f:
                    f.write(rec.to_json())
                    f.write("\n")
            except Exception:
                logger.exception("turn latency record failed (continuing)")
```
在 `src/dollos/telemetry/__init__.py` 加 export：
```python
from dollos.telemetry.turn_latency import TurnLatencyRecord, TurnLatencyRecorder
```
並把 `"TurnLatencyRecord", "TurnLatencyRecorder"` 加進 `__all__`。

- [ ] **Step 4: 跑測試確認 pass**

Run: `uv run pytest tests/test_turn_latency.py -v`
Expected: PASS

- [ ] **Step 5: 寫 mind_loop 量測的 failing test**

先 grep 現有 mind_loop 測試如何建 MindLoop / 送一個 UserSpoke turn（重用同 fixture）。測試斷言：一個會講話的 turn 跑完後，注入的 `TurnLatencyRecorder` 收到一筆 `first_speak_ms is not None`、`speak_chars > 0`、`mode == "deliberate"`。

```python
# tests/test_mind_loop_turn_latency.py（新；沿用既有 mind_loop 測試的 MindLoop 建構 helper）
import pytest
# ... 依既有 test_mind_loop.py 的 fixture 建 MindLoop，注入一個假 recorder：
class _CapturingRecorder:
    def __init__(self): self.records = []
    async def record(self, rec): self.records.append(rec)

@pytest.mark.asyncio
async def test_turn_emits_latency_record_with_first_speak(...):
    rec = _CapturingRecorder()
    mind = _make_mind_loop(..., turn_latency_recorder=rec)   # 見 Step 6 建構參數
    # 餵一個會讓 Doll 講話的 UserSpoke turn（LLM stub 吐 "<think>...</think>\n\n你好。"）
    await mind._run_one_turn([_user_spoke("hi")])
    assert len(rec.records) == 1
    r = rec.records[0]
    assert r.first_speak_ms is not None and r.first_speak_ms >= 0
    assert r.speak_chars > 0
    assert r.think_chars > 0
    assert r.mode == "deliberate"
    assert r.n_passes >= 1
```

- [ ] **Step 6: 跑測試確認 fail**

Run: `uv run pytest tests/test_mind_loop_turn_latency.py -v`
Expected: FAIL（MindLoop 尚無 `turn_latency_recorder` 參數）

- [ ] **Step 7: 實作 mind_loop 量測**

1. `MindLoop.__init__` 加參數 `turn_latency_recorder: TurnLatencyRecorder | None = None`，存 `self._turn_latency_recorder`。初始化 turn-scoped 欄位：`self._turn_t0 = None`、`self._turn_first_speak_ms = None`、`self._turn_think_chars = 0`、`self._turn_speak_chars = 0`。
2. 在 `_llm_iterate` 進入處（`try:` 之前設 `self._cascade_ctx` 附近）記 `self._turn_t0 = time.time()`，並重置上述 turn 欄位為 0/None。
3. 在 `_emit_sentence`（約 1481，實際把整句送 sink 之處）第一次送出時：
```python
if self._turn_first_speak_ms is None and self._turn_t0 is not None:
    self._turn_first_speak_ms = (time.time() - self._turn_t0) * 1000.0
self._turn_speak_chars += len(sentence)
```
（放在確定會送進 sink 的分支後；suppressed/whitespace-only 不算——沿用該函式既有的 guard 邏輯。）
4. think_chars：在 `_stream_one_pass` 取得 `raw_buf` 後，累加 think 段長度：
```python
raw = "".join(raw_buf)
head = raw.split("</think>", 1)[0]
self._turn_think_chars += len(head)
```
（每 pass 累加。）
5. 在 `_llm_iterate` 的 `finally`（turn 結束、cascade_ctx 清除處）計算並記錄：
```python
if self._turn_latency_recorder is not None and self._turn_t0 is not None:
    total_ms = (time.time() - self._turn_t0) * 1000.0
    rec = TurnLatencyRecord(
        ts=time.time(),
        first_speak_ms=self._turn_first_speak_ms,
        think_chars=self._turn_think_chars,
        speak_chars=self._turn_speak_chars,
        total_ms=total_ms,
        ttft_ms=None,                       # pass1 TTFT 由 llm_calls 已有；此處不重複量
        mode="deliberate",                  # Part 2 改為 self._think_mode
        n_passes=pass_idx + 1,
        had_tool_call=self._turn_had_tool,
    )
    await self._turn_latency_recorder.record(rec)
```
（`pass_idx` 在迴圈內；把它存成 turn 欄位或在 finally 前記下最後值。若 `finally` 看不到 `pass_idx`，在迴圈末更新 `self._turn_passes = pass_idx + 1`，finally 讀它。）

- [ ] **Step 8: 跑測試確認 pass**

Run: `uv run pytest tests/test_mind_loop_turn_latency.py tests/test_turn_latency.py -v`
Expected: PASS

- [ ] **Step 9: kernel 接線**

grep `TelemetryRecorder(` 於 `kernel.py` 找既有遙測 dir 解析，比照建 `TurnLatencyRecorder(<data.root>/<telemetry_subpath>)` 並傳入 `MindLoop(...)`。加一個 kernel/整合測試或手動確認 daemon 啟動不報錯。

Run: `uv run pytest -q`
Expected: 全套綠。

- [ ] **Step 10: Commit**

```bash
git add src/dollos/telemetry/ src/dollos/mind/mind_loop.py src/dollos/kernel.py tests/test_turn_latency.py tests/test_mind_loop_turn_latency.py
git commit -m "feat(telemetry): turn-level think/speak/first-speak latency record"
```

---

### Task 3: 啟動 GBNF `{1,n}` 能力探測（fail-closed）

**Files:**
- Create: `src/dollos/llm/grammar_probe.py`
- Modify: `src/dollos/kernel.py`（啟動序列，LLM client 建好、開始服務前呼叫）
- Test: `tests/test_grammar_probe.py`（新）

**Interfaces:**
- Consumes: LLM client 的 completion 介面（grep `stream_completion` / `complete` 於 transport 確認確切方法與簽名；探測用最小 `max_tokens`）。
- Produces: `async def assert_bounded_repetition_supported(llm) -> None`（成功 return None；server 拒絕 `{1,n}` 或回錯 → `raise RuntimeError(<清楚訊息，含升級 llama.cpp 指示>)`）。

- [ ] **Step 1: 寫 failing test（mock LLM）**

```python
# tests/test_grammar_probe.py
import pytest
from dollos.llm.grammar_probe import assert_bounded_repetition_supported

class _OkLLM:
    async def stream_completion(self, **kw):
        # 模擬正常：吐一個 chunk 再 done（依實際 chunk 型別調整）
        class _C:
            text = "a"; done = False
        class _D:
            text = ""; done = True
        yield _C()
        yield _D()

class _RejectLLM:
    async def stream_completion(self, **kw):
        raise RuntimeError("grammar parse error: unexpected '{'")
        yield  # pragma: no cover

@pytest.mark.asyncio
async def test_probe_passes_on_capable_server():
    await assert_bounded_repetition_supported(_OkLLM())   # 不得 raise

@pytest.mark.asyncio
async def test_probe_fails_closed_on_old_server():
    with pytest.raises(RuntimeError, match="bounded repetition"):
        await assert_bounded_repetition_supported(_RejectLLM())
```

- [ ] **Step 2: 跑測試確認 fail**

Run: `uv run pytest tests/test_grammar_probe.py -v`
Expected: FAIL（module 不存在）

- [ ] **Step 3: 實作探測（先 grep 確認 `stream_completion` 真實簽名與 chunk 屬性）**

```python
# src/dollos/llm/grammar_probe.py
"""啟動時探測 llama-server 是否支援 GBNF bounded repetition `{m,n}`。

本專案的 think 行長綁定（spec §3.1）依賴 `[^\n]{1,64}`。過舊的 llama.cpp
不支援 `{m,n}`，會在**每一回合**的請求級吐 HTTP error——那不是 Python
build-time raise（grammar 只是字串組裝），no-fallback 章節接不到。故在啟動
時送一次最小探測，fail-closed：拒絕啟動勝過每回合對話崩潰。（spec §11 / R1-7）
"""
from __future__ import annotations

from contextlib import aclosing

_PROBE_GRAMMAR = 'root ::= [a]{1,2}\n'

async def assert_bounded_repetition_supported(llm) -> None:
    try:
        stream = llm.stream_completion(
            system="", user="a", prefill="",
            max_tokens=2, grammar=_PROBE_GRAMMAR, purpose="startup_probe",
        )
        async with aclosing(stream) as s:
            async for chunk in s:
                if getattr(chunk, "done", False):
                    break
    except Exception as e:
        raise RuntimeError(
            "llama-server 不支援 GBNF bounded repetition `{m,n}`；"
            "DollOS 的 think 行長綁定需要它。請升級 llama.cpp "
            f"（原始錯誤：{e!r}）"
        ) from e
```
（注意：`stream_completion` 的實際簽名以 grep 為準；若它非 async-generator 而是回傳 awaitable，據實調整 test stub 與呼叫。）

- [ ] **Step 4: 跑測試確認 pass**

Run: `uv run pytest tests/test_grammar_probe.py -v`
Expected: PASS

- [ ] **Step 5: kernel 啟動序列接線**

在 kernel LLM client 建好、進入服務迴圈前，`await assert_bounded_repetition_supported(llm)`。失敗讓它往上拋（daemon 啟動失敗、log 清楚原因）。grep kernel 找 LLM client 變數名與啟動點。

- [ ] **Step 6: 全套回歸 + commit**

Run: `uv run pytest -q`
Expected: 全套綠。
```bash
git add src/dollos/llm/grammar_probe.py src/dollos/kernel.py tests/test_grammar_probe.py
git commit -m "feat(llm): fail-closed startup probe for GBNF bounded repetition"
```

---

## 驗收（Part 1 完成後，人工）

1. `uv run pytest -q` 全綠。
2. graceful 重啟 daemon（SIGINT→確認 down→setsid 起→確認 up），確認啟動探測通過、無錯。
3. 跟 Doll 講幾句 + 讓她閒置觸發 idle 回合，讀 `data/telemetry/turn_latency-<today>.jsonl`：確認有記錄、`think_chars` 有值、會講話的回合 `first_speak_ms` 有值。
4. 觀察 idle 回合的 `total_ms` / `think_chars`：確認不再出現先前 13.6s/730-char 級的 think 空轉（行長綁定生效）。
5. 收集數日 chat-turn 資料 → 餵 Part 2 的 reflex 定案（真 think 大小 + first_speak 分佈）。

## Self-Review

- **Spec coverage**：spec §3.1（行長綁定，REVIEW 寬 cap，speak 不綁）→ Task 1 ✓；§3.2（turn 級 think/speak/first-speak，epoch）→ Task 2 ✓；§10 Part 1 unit 3 + §11 R1-7（啟動能力探測 fail-closed）→ Task 3 ✓。Part 2（reflex/routing/scaffolding note/smoke）刻意不在本 plan。
- **Placeholder scan**：無 TBD；grep 指示為「確認既有簽名」而非佔位，屬必要的 codebase 對照。
- **Type consistency**：`_THINK_LINE_CAP`/`_REVIEW_LINE_CAP`（Task 1）、`TurnLatencyRecord`/`TurnLatencyRecorder` 欄位與 `mode="deliberate"`（Task 2）、`assert_bounded_repetition_supported`（Task 3）跨 task 一致；Task 2 的 `mode` 欄位預留 Part 2 改 `self._think_mode`。
