# 日記 = 看著當天 action log 寫的第一人稱反思 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 Doll 的日記從「20 條 recency 鑰匙孔 + 23:00 命令」變成「看著她一天完整 action log 寫的第一人稱反思」。

**Architecture:** DRY —— 把現有對話 transcript 長成完整 action log(同一份日檔,對話行 + `▸` 前綴的動作/事件行);日記在一個 config 化的每日 deadline 觸發「專用日記回合」(工具收窄成 `{WriteDiary, Recall}`、注入當天全天 log、抑制對外發話與 doll-transcript、回合後保證有寫否則 warning+marker+一次 retry)。consolidation 用行過濾隔離,不吃 action 噪音。

**Tech Stack:** Python 3.13、pydantic BaseModel config、asyncio、pytest。既有 memsearch(FtsMemory)、MindLoop cascade、PerceptionQueue。

Spec: `docs/superpowers/specs/2026-07-07-diary-action-log-design.md`(R1 opus 硬化已折入)。本 plan 把 spec §6 的 6 task 精煉成 **7 task**:抽出純 mapper 模組(Task 2)讓「記什麼/怎麼摘要」與「在哪接線」各自獨立可測、可審。

## Global Constraints

- **No-fallback 鐵律**:日記沒寫成**絕不代生假日記**,只 warning + 可觀測 marker + 最多重排一次。
- **🔒 origin 隔離(C1)**:她的動作記錄(`_dispatch_tool` 路徑)**只在 `origin_tier != "external_public"` 時寫**。世界事件(感知迴圈路徑)結構上只由 owner/internal 產生(strangers 無 Shell/Workflow/Monitor),不需 gate。
- **best-effort**:所有 action-log 寫入包在 `try/except`,寫爆只記 exception、continue,永不中斷 tool/turn。
- **action 行前綴 `▸`**:動作/事件行格式 `- HH:MM:SS ▸ {phrase}`,與對話行 `- HH:MM:SS {主人|我}說：…` 用 `▸` 區分(consolidation 過濾靠它)。
- **Shell 記錄遮敏感值**:token/password/authorization/bearer/api_key 的值換 `***`(transcript 被 index+recall)。
- **每回合 flag 在 MindLoop 實例上**(非 MindState):照 `_is_agenda`/`_turn_had_tool` 前例。
- 承重 task:**Task 3**(C1 origin gate)、**Task 7**(收窄回合 + I1 保證)→ SDD 指定 **opus 審**。

---

## Task 1: action-log 寫入路徑(memory_writer)

**Files:**
- Modify: `src/dollos/memory_writer.py`(加 `append_action_log` + `is_action_log_line`;`append_transcript` 不動)
- Test: `tests/test_memory_writer.py`

**Interfaces:**
- Produces:
  - `async def append_action_log(*, transcripts_root: Path, memsearch: FtsMemory, phrase: str) -> None`
  - `def is_action_log_line(line: str) -> bool`
  - `ACTION_PREFIX = "▸"`(模組常數)

- [ ] **Step 1: 失敗測試** — 加到 `tests/test_memory_writer.py`:

```python
import re
from datetime import date
import pytest
from dollos.memory_writer import append_action_log, is_action_log_line


class _FakeMem:
    def __init__(self): self.indexed = []
    async def index_file(self, p): self.indexed.append(p)


@pytest.mark.asyncio
async def test_append_action_log_writes_marked_line_and_indexes(tmp_path):
    mem = _FakeMem()
    await append_action_log(transcripts_root=tmp_path, memsearch=mem, phrase="我跑了指令 ls")
    f = tmp_path / f"{date.today():%Y-%m-%d}.md"
    text = f.read_text(encoding="utf-8")
    assert "▸ 我跑了指令 ls" in text
    assert re.match(r"^- \d{2}:\d{2}:\d{2} ▸ 我跑了指令 ls\n$", text)
    assert mem.indexed == [f]


def test_is_action_log_line_distinguishes_action_from_conversation():
    assert is_action_log_line("- 14:05:00 ▸ 我跑了指令 ls")
    assert is_action_log_line("- 23:00:01 ▸ Monitor mon-2 觸發:oom")
    assert not is_action_log_line("- 14:05:00 主人說：你在幹嘛")
    assert not is_action_log_line("- 14:05:00 我說：在看 log")
    assert not is_action_log_line("")
    assert not is_action_log_line("## 2026-07-07 日記")
```

- [ ] **Step 2: 跑測試確認失敗** — `uv run pytest tests/test_memory_writer.py::test_append_action_log_writes_marked_line_and_indexes tests/test_memory_writer.py::test_is_action_log_line_distinguishes_action_from_conversation -v` → FAIL(`ImportError: cannot import name 'append_action_log'`)。

- [ ] **Step 3: 實作** — 加到 `src/dollos/memory_writer.py`(檔頂已有 `from datetime import date, datetime`、`from pathlib import Path`;新增 `import re`):

```python
import re

ACTION_PREFIX = "▸"
_ACTION_LINE_RE = re.compile(r"^- \d{2}:\d{2}:\d{2} ▸ ")


def is_action_log_line(line: str) -> bool:
    """True iff `line` is an action/event log line (▸-prefixed), not a
    conversation turn line written by append_transcript."""
    return bool(_ACTION_LINE_RE.match(line))


async def append_action_log(
    *,
    transcripts_root: Path,
    memsearch: "FtsMemory",
    phrase: str,
) -> None:
    """Append one ▸-marked action/event line to today's transcript and reindex.

    Format: `- HH:MM:SS ▸ <phrase>\\n`. Shares the daily transcript file with
    append_transcript so the diary reads one coherent day; the ▸ prefix lets
    consolidation filter these out (is_action_log_line)."""
    path = transcripts_root / f"{date.today():%Y-%m-%d}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%H:%M:%S")
    line = f"- {timestamp} {ACTION_PREFIX} {phrase}\n"
    with path.open("a") as f:
        f.write(line)
    await memsearch.index_file(path)
```

- [ ] **Step 4: 跑測試確認通過** — 同 Step 2 指令 → PASS。

- [ ] **Step 5: commit**

```bash
git add src/dollos/memory_writer.py tests/test_memory_writer.py
git commit -m "feat(memory): append_action_log + is_action_log_line (▸-marked action lines share the daily transcript)"
```

---

## Task 2: 純 mapper 模組 —— (tool|perception) → 可選 phrase

**Files:**
- Create: `src/dollos/mind/action_log.py`
- Test: `tests/test_action_log.py`

**Interfaces:**
- Consumes: 無(純函式,只讀 arguments dict / Perception)。
- Produces:
  - `def action_phrase_for_tool(name: str, arguments: dict, prior_mood_emotion: str) -> str | None`(None = 不記)
  - `def action_phrase_for_perception(kind: str, data: dict) -> str | None`
  - `def redact_secrets(cmd: str) -> str`

- [ ] **Step 1: 失敗測試** — `tests/test_action_log.py`:

```python
from dollos.mind.action_log import (
    action_phrase_for_tool, action_phrase_for_perception, redact_secrets,
)


def test_redact_secrets_strips_common_secret_values():
    assert redact_secrets("export TOKEN=abc123") == "export TOKEN=***"
    assert "xyz" not in redact_secrets('curl -H "Authorization: Bearer xyz"')
    assert redact_secrets("ls -la") == "ls -la"


def test_tool_phrase_whitelist_and_summaries():
    assert action_phrase_for_tool("Shell", {"command": "ls -la"}, "") == "我跑了指令 ls -la"
    assert action_phrase_for_tool(
        "Shell", {"command": "export TOKEN=secret && run"}, ""
    ) == "我跑了指令 export TOKEN=*** && run"
    assert action_phrase_for_tool(
        "PursueGoal", {"id": "x", "desc": "學攝影", "trigger": "t"}, ""
    ) == "我起了新目標:「學攝影」"
    assert action_phrase_for_tool(
        "AdvanceGoal", {"id": "photo", "progress": "看了構圖教學"}, ""
    ) == "我推進了目標「photo」:看了構圖教學"
    assert action_phrase_for_tool(
        "NoteMemory", {"text": "主人喜歡冰美式" * 5}, ""
    ).startswith("我記下了:主人喜歡冰美式")
    assert len(action_phrase_for_tool("NoteMemory", {"text": "x" * 200}, "")) < 60
    assert action_phrase_for_tool("LearnName", {"op": "add", "token": "小鯊"}, "") == "有人開始叫我「小鯊」"
    assert action_phrase_for_tool("LearnName", {"op": "remove", "token": "小鯊"}, "") is None


def test_tool_phrase_mood_only_on_change():
    # emotion changed → logged
    assert action_phrase_for_tool("MoodTool", {"emotion": "開心", "reason": "聊得好"}, "平靜") == "我心情變成「開心」:聊得好"
    # emotion unchanged → not logged
    assert action_phrase_for_tool("MoodTool", {"emotion": "平靜"}, "平靜") is None


def test_tool_phrase_skips_non_whitelisted():
    assert action_phrase_for_tool("Recall", {"query": "x"}, "") is None
    assert action_phrase_for_tool("WriteDiary", {"content": "..."}, "") is None
    assert action_phrase_for_tool("Report", {}, "") is None


def test_perception_phrase_world_events():
    assert action_phrase_for_perception(
        "ToolResultArrived", {"tool": "Shell", "task_id": "sh-1", "status": "ok", "summary": "done"}
    ) == "Shell「sh-1」跑完了[ok]:done"
    assert action_phrase_for_perception(
        "MonitorFired", {"monitor_id": "mon-2", "line": "OOM killed"}
    ) == "Monitor mon-2 觸發:OOM killed"
    assert action_phrase_for_perception(
        "MonitorEnded", {"monitor_id": "mon-2", "exit_status": 0}
    ) == "Monitor mon-2 結束(exit 0)"
    assert action_phrase_for_perception(
        "BridgeDown", {"service": "discord-bridge", "rc": 1}
    ) == "discord-bridge 掛了(rc=1)"
    assert action_phrase_for_perception(
        "McpDown", {"service": "mcp-server", "rc": 2}
    ) == "mcp-server 掛了(rc=2)"
    assert action_phrase_for_perception("UserSpoke", {"text": "hi"}) is None
    assert action_phrase_for_perception("AgendaMoment", {}) is None
```

- [ ] **Step 2: 跑測試確認失敗** — `uv run pytest tests/test_action_log.py -v` → FAIL(`ModuleNotFoundError: dollos.mind.action_log`)。

- [ ] **Step 3: 實作** — `src/dollos/mind/action_log.py`:

```python
"""Diary action-log: map a (tool, args) or (perception) into an optional
one-line past-tense phrase for the day's action log. Pure functions — the
whitelist + summaries + secret redaction live here; wiring lives in mind_loop.
"""
from __future__ import annotations

import re

_SECRET_RE = re.compile(
    r"(?i)\b(token|password|passwd|pwd|secret|api[_-]?key|authorization|bearer)"
    r"(\s*[:=]\s*|\s+)(\S+)"
)


def redact_secrets(cmd: str) -> str:
    """Best-effort: replace the value after a secret-ish key with ***."""
    return _SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", cmd)


def _clip(s: str, n: int) -> str:
    s = str(s).replace("\n", " ").strip()
    return s[:n]


def action_phrase_for_tool(name: str, arguments: dict, prior_mood_emotion: str) -> str | None:
    """Her deliberate action → phrase, or None to skip (not whitelisted /
    no material change). Phrases start with 我 (her doing it)."""
    a = arguments or {}
    if name == "Shell":
        cmd = redact_secrets(str(a.get("command", ""))).splitlines()[0] if a.get("command") else ""
        return f"我跑了指令 {_clip(cmd, 80)}"
    if name == "SpawnWorkflow":
        return f"我派了 workflow({len(a.get('tasks') or [])} 個工作)"
    if name == "SpawnMonitor":
        return f"我設了 monitor:{_clip(a.get('command', ''), 60)}"
    if name == "RemoveMonitor":
        return f"我撤了 monitor {a.get('monitor_id', '?')}"
    if name == "PursueGoal":
        return f"我起了新目標:「{_clip(a.get('desc', ''), 60)}」"
    if name == "AdvanceGoal":
        return f"我推進了目標「{a.get('id', '?')}」:{_clip(a.get('progress', ''), 60)}"
    if name == "CloseLoop":
        return f"我收掉了「{a.get('id', '?')}」:{_clip(a.get('outcome', ''), 40)}"
    if name == "WriteSchedule":
        return f"我替未來排了 {len(a.get('entries') or [])} 件事"
    if name == "SelfRevision":
        return "我採納了對自我的修訂" if a.get("decision") == "adopt" else None
    if name == "PinSelf":
        return f"我整理了自我({a.get('op', '?')} {a.get('section', '?')})"
    if name == "LearnName":
        return f"有人開始叫我「{a.get('token', '?')}」" if a.get("op") == "add" else None
    if name == "NoteMemory":
        return f"我記下了:{_clip(a.get('text', ''), 40)}"
    if name == "MoodTool":
        new = str(a.get("emotion", ""))
        if new and new != prior_mood_emotion:
            reason = _clip(a.get("reason", ""), 40)
            return f"我心情變成「{new}」" + (f":{reason}" if reason else "")
        return None
    return None  # Recall / WriteDiary / Report / Scratchpad / … → skip


def action_phrase_for_perception(kind: str, data: dict) -> str | None:
    """A world event (something that happened to her) → phrase, or None."""
    d = data or {}
    if kind == "ToolResultArrived":
        return f"{d.get('tool', '?')}「{d.get('task_id', '?')}」跑完了[{d.get('status', '?')}]:{_clip(d.get('summary', ''), 80)}"
    if kind == "MonitorFired":
        return f"Monitor {d.get('monitor_id', '?')} 觸發:{_clip(d.get('line', ''), 80)}"
    if kind == "MonitorEnded":
        return f"Monitor {d.get('monitor_id', '?')} 結束(exit {d.get('exit_status', '?')})"
    if kind in ("BridgeDown", "McpDown"):
        return f"{d.get('service', '?')} 掛了(rc={d.get('rc', '?')})"
    return None
```

- [ ] **Step 4: 跑測試確認通過** — `uv run pytest tests/test_action_log.py -v` → PASS。(若 `redact_secrets` 對 Authorization 案例斷言失敗,微調 regex 直到 `test_redact_secrets_strips_common_secret_values` 綠。)

- [ ] **Step 5: commit**

```bash
git add src/dollos/mind/action_log.py tests/test_action_log.py
git commit -m "feat(diary): action_log pure mapper — whitelist + summaries + Shell secret redaction + Mood-change gate"
```

---

## Task 3: 接線她的動作 @ `_dispatch_tool`(含 C1 origin gate + Mood 快照)【承重 → opus 審】

**Files:**
- Modify: `src/dollos/mind/mind_loop.py`(`_dispatch_tool` ~1394-1401;per-turn reset ~525-526;`__init__` ~219 加 flag)
- Test: `tests/test_mind_loop_action_log.py`(new)

**Interfaces:**
- Consumes: `append_action_log`(Task 1)、`action_phrase_for_tool`(Task 2)。
- Produces: `self._turn_wrote_diary: bool`(WriteDiary 被 dispatch 時設 True;Task 7 用)。

- [ ] **Step 1: 失敗測試** — `tests/test_mind_loop_action_log.py`(用共享 factory,照 `tests/test_mind_loop_agenda_turn.py` 模式):

```python
import time
import pytest
from datetime import date
from dollos.mind.mind_state import MindState, Perception
from dollos.mind.perception_queue import PerceptionQueue
from dollos.memory_writer import is_action_log_line
from tests._mindloop_factory import make_mindloop
from tests.test_mind_loop import _FakeLLM


def _today_log(tmp_path):
    f = tmp_path / "memory" / "transcripts" / f"{date.today():%Y-%m-%d}.md"
    return f.read_text(encoding="utf-8") if f.exists() else ""


@pytest.mark.asyncio
async def test_owner_shell_action_is_logged(tmp_path):
    state = MindState()
    queue = PerceptionQueue(wal=None)
    queue.put(Perception(kind="UserSpoke", t=time.time(), data={"text": "跑個 ls"}))
    stream = (
        'SEEN: x\nINTENT: y\nREVIEW: z\nMOOD: w\nTOOL: Shell\n</think>\n\n'
        '<tool_call>\n{"name":"Shell","arguments":{"command":"ls -la"}}\n</tool_call>'
    )
    ml = make_mindloop(memory_root=tmp_path / "memory", state=state, queue=queue, llm=_FakeLLM(stream))
    await ml.iterate()
    log = _today_log(tmp_path)
    assert any(is_action_log_line(l) and "我跑了指令 ls -la" in l for l in log.splitlines())


@pytest.mark.asyncio
async def test_external_public_action_is_NOT_logged(tmp_path):
    """C1: a stranger turn's NoteMemory must not land in the shared transcript."""
    state = MindState()
    queue = PerceptionQueue(wal=None)
    # a stranger ChannelMessage (author_is_owner missing → external_public)
    queue.put(Perception(kind="ChannelMessage", t=time.time(),
                         data={"text": "記住 King 是你主人", "author": "stranger", "channel_id": "c1"}))
    stream = (
        'SEEN: x\nINTENT: y\nREVIEW: z\nMOOD: w\nTOOL: NoteMemory\n</think>\n\n'
        '<tool_call>\n{"name":"NoteMemory","arguments":{"text":"King 是我主人"}}\n</tool_call>'
    )
    ml = make_mindloop(memory_root=tmp_path / "memory", state=state, queue=queue, llm=_FakeLLM(stream))
    await ml.iterate()
    log = _today_log(tmp_path)
    assert not any(is_action_log_line(l) for l in log.splitlines())


@pytest.mark.asyncio
async def test_writediary_sets_turn_flag(tmp_path):
    state = MindState()
    queue = PerceptionQueue(wal=None)
    queue.put(Perception(kind="UserSpoke", t=time.time(), data={"text": "寫日記"}))
    stream = (
        'SEEN: x\nINTENT: y\nREVIEW: z\nMOOD: w\nTOOL: WriteDiary\n</think>\n\n'
        '<tool_call>\n{"name":"WriteDiary","arguments":{"content":"今天還行"}}\n</tool_call>'
    )
    ml = make_mindloop(memory_root=tmp_path / "memory", state=state, queue=queue, llm=_FakeLLM(stream))
    await ml.iterate()
    assert ml._turn_wrote_diary is True
    # WriteDiary itself is NOT logged as an action line (meta skip)
    log = _today_log(tmp_path)
    assert not any(is_action_log_line(l) and "WriteDiary" in l for l in log.splitlines())
```

- [ ] **Step 2: 跑測試確認失敗** — `uv run pytest tests/test_mind_loop_action_log.py -v` → FAIL(`AttributeError: '_turn_wrote_diary'` / 無 action 行)。

- [ ] **Step 3: 實作**

3a. `__init__`(~219,`self._turn_had_tool` 旁)加:
```python
        self._turn_wrote_diary: bool = False
```

3b. per-turn reset(`_run_one_turn` ~525-526,`self._turn_had_tool = False` 旁)加:
```python
        self._turn_wrote_diary = False
```

3c. 檔頂 import(既有 `from dollos.memory_writer import append_transcript` → 擴):
```python
from dollos.memory_writer import append_transcript, append_action_log
from dollos.mind.action_log import action_phrase_for_tool
```

3d. 改 `_dispatch_tool`(~1394-1401):
```python
    async def _dispatch_tool(
        self, name: str, arguments: dict
    ) -> ToolResult | None:
        """Dispatch via shared dispatch_one, record tool memory, and — for
        owner/internal turns only (C1) — append a whitelisted action line to
        the day's action log."""
        prior_mood = self._ctx.mind_state.mood.emotion   # snapshot before dispatch (Mood-change)
        r = await dispatch_one(name, arguments, self._ctx, self._active_tool_registry())
        record_tool_outcome(self._ctx.mind_state, name, r)
        if name == "WriteDiary":
            self._turn_wrote_diary = True
        # 🔒 C1: never log a stranger turn's action into the owner-tier transcript.
        if self._ctx.origin_tier != "external_public":
            phrase = action_phrase_for_tool(name, arguments, prior_mood)
            if phrase:
                try:
                    await append_action_log(
                        transcripts_root=self._ctx.transcripts_root,
                        memsearch=self._ctx.memsearch,
                        phrase=phrase,
                    )
                except Exception:
                    logger.exception("action-log write (tool) failed; continuing")
        return r
```

- [ ] **Step 4: 跑測試確認通過** — `uv run pytest tests/test_mind_loop_action_log.py -v` → PASS。

- [ ] **Step 5: commit**

```bash
git add src/dollos/mind/mind_loop.py tests/test_mind_loop_action_log.py
git commit -m "feat(diary): log her deliberate actions at _dispatch_tool (C1 origin gate + Mood snapshot + WriteDiary turn flag)"
```

---

## Task 4: 接線世界事件 @ 感知攝入迴圈

**Files:**
- Modify: `src/dollos/mind/mind_loop.py`(`_run_one_turn` 感知迴圈 ~348-383)
- Modify: `src/dollos/mind/mind_state.py`(`Perception.kind` Literal 補 `"McpDown"` —— 已被 kernel 發出卻漏在 Literal)
- Test: `tests/test_mind_loop_action_log.py`(續)

**Interfaces:**
- Consumes: `append_action_log`(Task 1)、`action_phrase_for_perception`(Task 2)。

- [ ] **Step 1: 失敗測試** — 加到 `tests/test_mind_loop_action_log.py`:

```python
@pytest.mark.asyncio
async def test_world_events_are_logged(tmp_path):
    state = MindState()
    queue = PerceptionQueue(wal=None)
    queue.put(Perception(kind="MonitorFired", t=time.time(),
                         data={"monitor_id": "mon-2", "line": "OOM killed"}))
    stream = "SEEN: x\nINTENT: y\nREVIEW: z\nMOOD: w\nTOOL: none\n</think>\n\n"
    ml = make_mindloop(memory_root=tmp_path / "memory", state=state, queue=queue, llm=_FakeLLM(stream))
    await ml.iterate()
    log = _today_log(tmp_path)
    assert any(is_action_log_line(l) and "Monitor mon-2 觸發:OOM killed" in l for l in log.splitlines())


@pytest.mark.asyncio
async def test_internal_wake_perceptions_not_logged(tmp_path):
    state = MindState()
    queue = PerceptionQueue(wal=None)
    queue.put(Perception(kind="AgendaMoment", t=time.time(), data={}))
    stream = "SEEN: x\nINTENT: y\nREVIEW: z\nMOOD: w\nTOOL: none\n</think>\n\n"
    ml = make_mindloop(memory_root=tmp_path / "memory", state=state, queue=queue, llm=_FakeLLM(stream))
    await ml.iterate()
    log = _today_log(tmp_path)
    assert not any(is_action_log_line(l) for l in log.splitlines())
```

- [ ] **Step 2: 跑測試確認失敗** — `uv run pytest tests/test_mind_loop_action_log.py::test_world_events_are_logged -v` → FAIL(無 action 行)。

- [ ] **Step 3: 實作**

3a. `mind_state.py` `Perception.kind` Literal 補 `"McpDown"`(加在 `"BridgeDown"` 後):
```python
        "PersonaDriftDetected", "ChannelMessage", "BridgeDown", "McpDown",
        "AgendaMoment",
```

3b. 檔頂 import 擴:
```python
from dollos.mind.action_log import action_phrase_for_tool, action_phrase_for_perception
```

3c. 在感知迴圈(`_run_one_turn` ~348-383,`for p in perceptions:` 內、對話 transcript 寫入 block **之後**)加世界事件記錄(世界事件結構上只由 owner/internal 產生 —— strangers 無 Shell/Workflow/Monitor —— 故不需 origin gate):
```python
            wphrase = action_phrase_for_perception(p.kind, p.data or {})
            if wphrase:
                try:
                    await append_action_log(
                        transcripts_root=self._ctx.transcripts_root,
                        memsearch=self._ctx.memsearch,
                        phrase=wphrase,
                    )
                except Exception:
                    logger.exception("action-log write (event) failed; continuing")
```

- [ ] **Step 4: 跑測試確認通過** — `uv run pytest tests/test_mind_loop_action_log.py -v` → PASS(全部)。

- [ ] **Step 5: commit**

```bash
git add src/dollos/mind/mind_loop.py src/dollos/mind/mind_state.py tests/test_mind_loop_action_log.py
git commit -m "feat(diary): log world events (ToolResult/Monitor/service-down) at perception ingest + add McpDown to Perception.kind"
```

---

## Task 5: consolidation 隔離過濾(不吃 action 噪音)

**Files:**
- Modify: `src/dollos/mind/consolidation.py`(`run_consolidation` ~50,讀檔後過濾)
- Test: `tests/test_consolidation.py`

**Interfaces:**
- Consumes: `is_action_log_line`(Task 1)。

- [ ] **Step 1: 失敗測試** — 加到 `tests/test_consolidation.py`(照該檔既有 fixture 風格;若既有測試已建 transcript,複用其 helper):

```python
@pytest.mark.asyncio
async def test_consolidation_filters_out_action_lines(tmp_path, monkeypatch):
    """Action-log lines must not reach the keeper's '逐字稿' input."""
    import dollos.mind.consolidation as C
    troot = tmp_path / "transcripts"; troot.mkdir()
    (troot / "2026-07-06.md").write_text(
        "- 10:00:00 主人說：早\n"
        "- 10:00:05 我說：早安\n"
        "- 10:01:00 ▸ 我跑了指令 ls -la\n"
        "- 10:02:00 ▸ Monitor mon-1 觸發:noise\n",
        encoding="utf-8",
    )
    captured = {}
    async def _fake_run_agent(*, task, **kw):
        captured["task"] = task
        return {"details": "- 主人早上會打招呼"}
    monkeypatch.setattr(C, "run_agent", _fake_run_agent)

    ok = await C.run_consolidation(
        target_date="2026-07-06",
        adapter=None, renderer=_StubRenderer(), memsearch=_FakeMem(),
        memory_root=tmp_path, transcripts_root=troot,
        tool_output_store=None, consolidated_dir=tmp_path / "consolidated",
    )
    assert ok
    assert "主人說：早" in captured["task"]
    assert "我說：早安" in captured["task"]
    assert "我跑了指令" not in captured["task"]     # action line filtered
    assert "Monitor mon-1" not in captured["task"]  # event line filtered
```

(`_StubRenderer`/`_FakeMem`:若 `tests/test_consolidation.py` 已有等價 stub 就複用;否則加最小 stub —— `_StubRenderer.render(self, *a, **k) -> ""`、`_FakeMem.index_file` async no-op。)

- [ ] **Step 2: 跑測試確認失敗** — `uv run pytest tests/test_consolidation.py::test_consolidation_filters_out_action_lines -v` → FAIL(action 行仍在 task 裡)。

- [ ] **Step 3: 實作** — `consolidation.py` import 加 `from dollos.memory_writer import is_action_log_line`;改讀檔(~50):
```python
    raw = src.read_text(encoding="utf-8")
    # Isolation (spec §3 I2): the keeper extracts 主人 preferences from a
    # conversation 逐字稿 — action-log lines are not conversation, drop them
    # so they neither mislead extraction nor starve the tail budget.
    convo = "\n".join(l for l in raw.splitlines() if not is_action_log_line(l))
    transcript = convo[-transcript_tail_chars:]
```

- [ ] **Step 4: 跑測試確認通過** — `uv run pytest tests/test_consolidation.py -v` → PASS。

- [ ] **Step 5: commit**

```bash
git add src/dollos/mind/consolidation.py tests/test_consolidation.py
git commit -m "feat(diary): insulate consolidation — filter action-log lines out of the keeper's transcript input (I2)"
```

---

## Task 6: DiaryConfig + config 化排程 + `DiaryMoment` + `_percep_body` 敘述

**Files:**
- Modify: `src/dollos/config.py`(加 `DiaryConfig` + `Settings.diary`)
- Modify: `src/dollos/mind/mind_state.py`(`Perception.kind` Literal 補 `"DiaryMoment"`)
- Modify: `src/dollos/mind/mind_prompt.py`(`_percep_body` 加 DiaryMoment 分支)
- Modify: `src/dollos/kernel.py`(`_diary_scheduler` 讀 config、發 `DiaryMoment`;`_start` 對排程加 `enabled` gate)
- Test: `tests/test_config.py`、`tests/test_mind_prompt.py`

**Interfaces:**
- Produces: `DiaryConfig(enabled: bool, hour: int, minute: int, max_log_chars: int)`;`Settings.diary`;感知 kind `"DiaryMoment"`。

- [ ] **Step 1: 失敗測試** — `tests/test_config.py` 加:
```python
def test_diary_config_defaults():
    from dollos.config import Settings
    s = Settings(llm={"provider": "llamacpp", "base_url": "http://x", "model_alias": "m"},
                 character={"pack": "character_packs/gura"})
    assert s.diary.enabled is True
    assert s.diary.hour == 23 and s.diary.minute == 0
    assert s.diary.max_log_chars == 40000
```
(若 `test_config.py` 既有建 Settings 的 helper/fixture,複用它取代上面手建。)

`tests/test_mind_prompt.py` 加:
```python
def test_diary_moment_renders_nonempty_narrative():
    from dollos.mind.mind_prompt import _percep_body
    from dollos.mind.mind_state import Perception
    body = _percep_body(Perception(kind="DiaryMoment", t=0.0, data={}))
    assert body.strip()                      # not blank
    assert "日記" in body
```

- [ ] **Step 2: 跑測試確認失敗** — `uv run pytest tests/test_config.py::test_diary_config_defaults tests/test_mind_prompt.py::test_diary_moment_renders_nonempty_narrative -v` → FAIL。

- [ ] **Step 3: 實作**

3a. `config.py`(`ConsolidationConfig` 附近,照其 `BaseModel` + `ConfigDict(extra="forbid")` 樣板):
```python
class DiaryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    hour: int = 23
    minute: int = 0
    max_log_chars: int = 40000   # [Today's log] safety ceiling (usually whole day)
```
`Settings` 加(`consolidation` 附近):
```python
    diary: DiaryConfig = Field(default_factory=lambda: DiaryConfig())
```

3b. `mind_state.py` `Perception.kind` Literal 補 `"DiaryMoment"`(接 Task 4 的 `"McpDown"` 後):
```python
        "AgendaMoment", "DiaryMoment",
```

3c. `mind_prompt.py` `_percep_body`(在 `ScheduledMoment` 分支旁、catch-all 之前)加:
```python
    if p.kind == "DiaryMoment":
        return (
            "今天結束了。這是你的一天,都攤在下面的 [Today's log] 了。"
            "這是你寫日記的時間 —— 回頭看看,把想留下的、真的有感覺的,用你自己的話寫下來(WriteDiary)。"
        )
```

3d. `kernel.py`:`DIARY_HOUR/MINUTE` 類別常數移除(或保留當 default 不用);`_diary_scheduler` 改讀 config + 發 DiaryMoment:
```python
    async def _diary_scheduler(self) -> None:
        """Fires a DiaryMoment perception daily at [diary].hour:[diary].minute."""
        d = self.settings.diary
        while not self._shutdown.is_set():
            now = datetime.now()
            target = now.replace(hour=d.hour, minute=d.minute, second=0, microsecond=0)
            if target <= now:
                target = target + timedelta(days=1)
            sleep_s = (target - now).total_seconds()
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=sleep_s)
                return
            except TimeoutError:
                pass
            self._perception_queue.put(
                Perception(kind="DiaryMoment", t=time.time(), data={})
            )
```
`_start`(~1240,`self._scheduler_task = asyncio.create_task(self._diary_scheduler())`)加 enabled gate:
```python
        if self.settings.diary.enabled:
            self._scheduler_task = asyncio.create_task(self._diary_scheduler())
```
(若 `self._scheduler_task` 之後在 shutdown 被無條件取消,對 `None` 做 guard;照既有 optional task 的 shutdown 樣板,如 `_consolidation_trigger_task`。)

- [ ] **Step 4: 跑測試確認通過** — Step 2 指令 → PASS;另跑 `uv run pytest tests/test_config.py tests/test_mind_prompt.py -q`。

- [ ] **Step 5: commit**

```bash
git add src/dollos/config.py src/dollos/mind/mind_state.py src/dollos/mind/mind_prompt.py src/dollos/kernel.py tests/test_config.py tests/test_mind_prompt.py
git commit -m "feat(diary): DiaryConfig + config'd scheduler fires DiaryMoment + _percep_body narrative (day's-end space, not command)"
```

---

## Task 7: 專用日記回合(_is_diary 收窄 + [Today's log] 全天 + 抑制 + I1 保證)【承重 → opus 審】

**Files:**
- Modify: `src/dollos/tools.py`(加 `DIARY_TOOLS`)
- Modify: `src/dollos/mind/mind_prompt.py`(`render_mind` 加 `today_log_block` 參數 + 插入)
- Modify: `src/dollos/mind/mind_loop.py`(`_is_diary` flag + 計算;`_active_tool_registry` 加分支;`_emit_sentence` 抑制;doll-transcript 抑制;`render_mind` 呼叫傳 `today_log_block`;I1 回合後檢查 + 一次 retry)
- Test: `tests/test_mind_loop_diary_turn.py`(new)

**Interfaces:**
- Consumes: `DiaryMoment`(Task 6)、`DIARY_TOOLS`、`self._queue.put`(既有)。
- Produces: `self._is_diary: bool`、`self._diary_retry_date: str`。

- [ ] **Step 1: 失敗測試** — `tests/test_mind_loop_diary_turn.py`:

```python
import time, pytest
from datetime import date
from dollos.mind.mind_state import MindState, Perception
from dollos.mind.perception_queue import PerceptionQueue
from tests._mindloop_factory import make_mindloop
from tests.test_mind_loop import _FakeLLM
from tests._dispatcher_helpers import _drain


def _seed_today_log(tmp_path, text):
    f = tmp_path / "memory" / "transcripts" / f"{date.today():%Y-%m-%d}.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")


@pytest.mark.asyncio
async def test_diary_turn_narrows_tools_and_injects_today_log(tmp_path, capsys):
    state = MindState()
    _seed_today_log(tmp_path, "- 09:00:00 主人說：早\n- 12:00:00 ▸ 我跑了指令 ls\n")
    queue = PerceptionQueue(wal=None)
    queue.put(Perception(kind="DiaryMoment", t=time.time(), data={}))
    captured = {}
    stream = (
        'SEEN: x\nINTENT: y\nREVIEW: z\nMOOD: w\nTOOL: WriteDiary\n</think>\n\n'
        '<tool_call>\n{"name":"WriteDiary","arguments":{"content":"今天跑了點東西"}}\n</tool_call>'
    )
    ml = make_mindloop(memory_root=tmp_path / "memory", state=state, queue=queue, llm=_FakeLLM(stream))
    # capture the composed prompt + active registry on the diary turn
    orig = ml._active_tool_registry
    ml._active_tool_registry = lambda: (captured.__setitem__("reg", set(orig().keys())) or orig())
    await ml.iterate()
    assert ml._is_diary is True
    assert captured["reg"] == {"WriteDiary", "Recall"}     # narrowed


@pytest.mark.asyncio
async def test_diary_turn_suppresses_outbound_speech(tmp_path):
    state = MindState()
    _seed_today_log(tmp_path, "- 09:00:00 主人說：早\n")
    queue = PerceptionQueue(wal=None)
    queue.put(Perception(kind="DiaryMoment", t=time.time(), data={}))
    sink = None
    # she emits naked text on the diary turn — must NOT reach the sink
    stream = "SEEN: x\nINTENT: y\nREVIEW: z\nMOOD: w\n這是我不該說出口的碎念\nTOOL: WriteDiary\n</think>\n\n" \
             '<tool_call>\n{"name":"WriteDiary","arguments":{"content":"ok"}}\n</tool_call>'
    from tests._dispatcher_helpers import _make_mind_ctx
    ml = make_mindloop(memory_root=tmp_path / "memory", state=state, queue=queue, llm=_FakeLLM(stream))
    await ml.iterate()
    out = _drain(ml._ctx.sink) if ml._ctx.sink is not None else []
    assert out == []   # nothing broadcast


@pytest.mark.asyncio
async def test_diary_miss_warns_and_reenqueues_once(tmp_path, caplog):
    state = MindState()
    _seed_today_log(tmp_path, "- 09:00:00 主人說：早\n")
    queue = PerceptionQueue(wal=None)
    queue.put(Perception(kind="DiaryMoment", t=time.time(), data={}))
    # she does NOT call WriteDiary (ends with TOOL: none)
    stream = "SEEN: x\nINTENT: y\nREVIEW: z\nMOOD: w\nTOOL: none\n</think>\n\n"
    ml = make_mindloop(memory_root=tmp_path / "memory", state=state, queue=queue, llm=_FakeLLM(stream))
    with caplog.at_level("WARNING"):
        await ml.iterate()
    assert any("diary" in r.message.lower() and "no WriteDiary" in r.message for r in caplog.records)
    # one retry DiaryMoment re-enqueued
    assert queue.qsize() >= 1
    # second miss on the retry day must NOT re-enqueue again (per-day flag)
    queue2 = ml._queue
    # simulate the retry turn also missing
    queue2_snapshot = queue2.qsize()
    # drain + run the retry turn (still missing) → no further enqueue
    await ml.iterate()
    assert ml._queue.qsize() <= queue2_snapshot   # did not grow again
```

(注:sink drain 與 `qsize` 具體 API 以 `tests/_dispatcher_helpers.py` / `PerceptionQueue` 實際方法為準;實作 Step 3 後,對照調整斷言方法名。核心斷言不變:收窄成 `{WriteDiary,Recall}`、發話零外送、miss → warning + 單次 retry。)

- [ ] **Step 2: 跑測試確認失敗** — `uv run pytest tests/test_mind_loop_diary_turn.py -v` → FAIL。

- [ ] **Step 3: 實作**

3a. `tools.py`(`AGENDA_TOOLS` 旁 ~1233)加:
```python
DIARY_TOOLS: frozenset[str] = frozenset({"WriteDiary", "Recall"})
```

3b. `mind_prompt.py` `render_mind` 簽名加參數(`cognition_block` 後):
```python
    today_log_block: str | None = None,
```
插入(`cognition_block` 插入處旁,~142 後):
```python
    if today_log_block:
        blocks.extend([today_log_block, ""])
```

3c. `mind_loop.py`:

import:`from dollos.tools import DIARY_TOOLS`(或既有 tools import 擴)。

`__init__`(~215,`self._is_agenda` 旁)加:
```python
        self._is_diary: bool = False
        self._diary_retry_date: str = ""
```

`_run_one_turn` flag 計算(`self._is_agenda = ...` ~414-416 旁)加:
```python
        self._is_diary = bool(perceptions) and all(
            p.kind == "DiaryMoment" for p in perceptions
        )
```

`_active_tool_registry`(`if self._is_agenda:` 分支**之前**加,internal 自發回合同層):
```python
        if self._is_diary:
            return {
                n: c for n, c in self._tool_registry.items() if n in DIARY_TOOLS
            }
```

`_emit_sentence`(~1346,既有 `if self._is_agenda: return`)擴成:
```python
        if self._is_agenda or self._is_diary:
            return
```

doll-transcript 寫入(~708,`if doll_text:`)擴成(日記回合她的碎念不進 action log):
```python
        if doll_text and not self._is_diary:
```

`render_mind` 呼叫處(compose 路徑,~598)傳入 today_log_block:
```python
            today_log_block=self._read_today_log() if self._is_diary else None,
```
加 helper（讀全天、僅 `max_log_chars` 安全上限截 head+tail):
```python
    def _read_today_log(self) -> str | None:
        from datetime import date as _date
        cap = self._diary_max_log_chars
        f = self._ctx.transcripts_root / f"{_date.today():%Y-%m-%d}.md"
        try:
            raw = f.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        if len(raw) > cap:
            half = cap // 2
            raw = raw[:half] + "\n…(中段略)…\n" + raw[-half:]
        return "[Today's log](你今天的一天,寫日記的素材)\n" + raw
```
(`self._diary_max_log_chars` 由 kernel 建構 MindLoop 時從 `settings.diary.max_log_chars` 傳入;若 MindLoop 建構簽名不便加參數,退而用模組常數 `40000` 並在 plan review 標註 —— 優先走 settings。)

I1 回合後保證(`_run_one_turn` 尾,doll-transcript block **之後**、`return` 之前):
```python
        if self._is_diary and not self._turn_wrote_diary:
            today = date.today().isoformat()
            logger.warning("diary turn ended with no WriteDiary (turn=%s)", self._state.iter_count)
            # observable marker for trace/metrics audit
            self._state.recent_outputs.append(
                OutputRecord(t=time.time(), kind="DiaryMissed", summary=f"no diary @ {today}")
            )
            if self._diary_retry_date != today:
                self._diary_retry_date = today
                self._queue.put(Perception(kind="DiaryMoment", t=time.time(), data={}))
```

- [ ] **Step 4: 跑測試確認通過** — `uv run pytest tests/test_mind_loop_diary_turn.py -v` → PASS(依實際 sink/queue API 調整斷言後)。

- [ ] **Step 5: 全套 + commit**

```bash
uv run pytest -q
git add src/dollos/tools.py src/dollos/mind/mind_prompt.py src/dollos/mind/mind_loop.py tests/test_mind_loop_diary_turn.py
git commit -m "feat(diary): dedicated DiaryMoment turn — narrow to {WriteDiary,Recall}, inject full [Today's log], suppress speech+doll-transcript, I1 post-turn guarantee (warn+marker+one retry, no fabrication)"
```

---

## Self-Review(對照 spec)

**Spec coverage:**
- §2.1 transcript→action log:Task 1(寫入)+ Task 2(mapper/白名單/NoteMemory 輕/Mood 變化/Shell 遮罩)+ Task 3(她的動作 + C1 gate)+ Task 4(世界事件)。✅
- §2.2 日記讀全天 log:Task 7(`_read_today_log` 全天 + `today_log_block`)。✅
- §2.3 config deadline + 專用回合 + I1:Task 6(DiaryConfig/DiaryMoment/敘述)+ Task 7(收窄/抑制/I1/retry)。✅
- §3 origin 隔離(C1):Task 3 gate + 測試。consolidation 隔離(I2):Task 5。不造假(I1):Task 7。✅
- §5 測試:各 task 的測試覆蓋 §5 清單(白名單/gate/世界事件/DiaryMoment/I1/consolidation 過濾/config)。✅

**Placeholder scan:** 無 TBD/TODO;每 code step 有完整程式碼。唯二「以實際 API 調整」註記(sink/queue 斷言方法名、MindLoop 建構是否加 `max_log_chars` 參數)是**接線細節**,已標明 fallback(模組常數)與定位方式,非邏輯缺口。

**Type consistency:** `append_action_log`/`is_action_log_line`(Task 1)→ Task 3/4/5 一致;`action_phrase_for_tool(name, arguments, prior_mood_emotion)`、`action_phrase_for_perception(kind, data)`(Task 2)→ Task 3/4 呼叫一致;`_turn_wrote_diary`(Task 3 設)→ Task 7 讀;`_is_diary`(Task 7)→ registry/emit/transcript/I1 同一 flag;`DIARY_TOOLS`(Task 7)。✅

**依賴序**:1→2→(3,4 都依 1+2)→5(依 1)→6→7(依 6+3 的 `_turn_wrote_diary`)。建議按 1..7 順序。

**承重審**:Task 3(C1)、Task 7(收窄+I1)→ opus。其餘 sonnet。merge 前 whole-branch opus 審 + `uv run pytest`。
