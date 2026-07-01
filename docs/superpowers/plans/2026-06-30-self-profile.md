# A1 Self-Profile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 給 Doll 一個她主導 pin、always-inject 的「演化中自我」薄補充層(self_profile.md + PinSelf 工具 + [Self profile] block)。

**Architecture:** 核心 store 邏輯(parse / id 指派 / cap / 定位 / scaffolding / render)抽成 pure module `src/dollos/mind/self_profile.py`(仿 `scratchpad_helpers`,無 IO 以外副作用、可單元測);`PinSelf` reflection-gated 工具呼叫它;檔放 `memory_root` 根(不被索引);block 由 `render_mind` 插在 `system_prompt` 之後、`[Memory guideline]` 之前;wiring 落 `mind_loop.py`(registry :349 + `IN_TURN_REFEED_TOOLS` :56 + ctor flag)、`kernel.py`、`config.py`、nudge。

**Tech Stack:** Python 3.13、pydantic(工具 schema + config)、pytest、既有 GBNF grammar builder(自動涵蓋新工具,無需手改)。

## Global Constraints

- **Python ≥ 3.13**(`CancelledError` 是 `BaseException`,不被 `except Exception` 攔)。
- **No-fallback**:缺值/越界不降級,明確錯誤或不渲染。
- **self_profile.md 絕不 index**:`PinSelf.run()` 不呼叫 `memsearch.index_file`;檔放 `memory_root` 根(不在索引 paths `[shared, transcripts, skills]`)。
- **friendly-error, never raise**:cap 溢出 / 定位失敗一律回 success=True 的字串(靠 `IN_TURN_REFEED_TOOLS` re-feed 給 Doll),**不** raise / 不 success=False(否則累加連續失敗計數誤觸 safe mode)。
- **bullet 行格式固定**:`- [<id>·<YYYY-MM-DD>] <text>`;id = 段字首(self→`s`/relationship→`r`/user→`u`)+ 段內單調遞增序號(移除不重用)。
- **繁體中文** commit message / 註解;每個 task 結尾 commit。
- **max_chars 預設 1200**(全檔字數上限,涵蓋 add + replace)。

---

## File Structure

- `src/dollos/mind/self_profile.py` — **新建**。pure store:`Bullet` dataclass、`_parse`/`_serialize`/`_next_id`、`apply(...)`、`render_block(...)`、`SECTION_TITLES`/`SECTION_ORDER`。
- `src/dollos/config.py` — 加 `SelfProfileConfig` + `Settings.self_profile`。
- `src/dollos/mind/mind_ctx.py` — `MindCtx` 加 `self_profile_max_chars: int` 欄位。
- `src/dollos/tools.py` — 加 `PinSelf`;更新 `REFLECTION_TOOLS`。
- `src/dollos/mind/mind_loop.py` — import `PinSelf`;`IN_TURN_REFEED_TOOLS` 加 `"PinSelf"`;`__init__` 加 `self_profile_enabled`;`_active_tool_registry` :349 gate 注入;`iterate()` 讀檔傳 `self_profile_text`。
- `src/dollos/mind/mind_prompt.py` — `render_mind` 加 `self_profile_text` 參數 + `[Self profile]` block;`ReflectionMoment` nudge 補 PinSelf 提示。
- `src/dollos/kernel.py` — `MindCtx(...)` 傳 `self_profile_max_chars`;`MindLoop(...)` 傳 `self_profile_enabled`。
- 測試:`tests/mind/test_self_profile_store.py`(新)、`tests/tools/test_pin_self.py`(新)、既有 `tests/mind/test_mind_loop*.py` / `tests/mind/test_mind_prompt*.py` / `tests/test_config.py` 增測。

---

### Task 1: config — SelfProfileConfig

**Files:**
- Modify: `src/dollos/config.py:167-195`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Settings.self_profile.enabled: bool`、`Settings.self_profile.max_chars: int`。

- [ ] **Step 1: 寫失敗測試**

在 `tests/test_config.py` 加:
```python
def test_self_profile_config_defaults():
    from dollos.config import Settings, LLMConfig, CharacterConfig
    s = Settings(
        llm=LLMConfig(base_url="http://x", model_id="m"),
        character=CharacterConfig(pack="gura"),
    )
    assert s.self_profile.enabled is True
    assert s.self_profile.max_chars == 1200
```
(若既有測試已有 Settings 建構 helper,沿用之;上面的 LLMConfig/CharacterConfig 必填欄位以現有測試為準。)

- [ ] **Step 2: 跑測試確認 fail**

Run: `cd /home/progcat/Projects/DollOS && uv run pytest tests/test_config.py::test_self_profile_config_defaults -v`
Expected: FAIL(`Settings` 無 `self_profile`)。

- [ ] **Step 3: 實作**

在 `src/dollos/config.py` 的 `EnergyConfig`(:167-175)之後加:
```python
class SelfProfileConfig(BaseModel):
    """A1 self-profile — Doll-pinned always-inject evolving self."""
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    max_chars: int = 1200
```
在 `Settings`(:178-195)`energy` 欄位後加:
```python
    self_profile: SelfProfileConfig = Field(default_factory=lambda: SelfProfileConfig())
```

- [ ] **Step 4: 跑測試確認 pass**

Run: `cd /home/progcat/Projects/DollOS && uv run pytest tests/test_config.py -v`
Expected: PASS。

- [ ] **Step 5: commit**

```bash
git add src/dollos/config.py tests/test_config.py
git commit -m "feat(self-profile): [self_profile] config (enabled + max_chars)"
```

---

### Task 2: self_profile store（pure module，核心邏輯）

**Files:**
- Create: `src/dollos/mind/self_profile.py`
- Test: `tests/mind/test_self_profile_store.py`

**Interfaces:**
- Produces:
  - `SECTION_TITLES: dict[str,str]`(`{"self":"我學到的自己","relationship":"我和主人","user":"我注意到的主人"}`)、`SECTION_ORDER: list[str]`(`["self","relationship","user"]`)。
  - `apply(path: Path, *, section: str, op: str, target: str, text: str, max_chars: int, today: str) -> str` — read-modify-write,回人類可讀結果/友善錯誤字串。
  - `render_block(path: Path) -> str | None` — 有 ≥1 bullet 回 body(略過空段),否則 `None`。

- [ ] **Step 1: 寫失敗測試**

新建 `tests/mind/test_self_profile_store.py`:
```python
from pathlib import Path
from dollos.mind import self_profile as sp


def _p(tmp_path) -> Path:
    return tmp_path / "self_profile.md"


def test_add_scaffolds_file_and_assigns_id(tmp_path):
    p = _p(tmp_path)
    msg = sp.apply(p, section="self", op="add", target="",
                   text="我比表面更在意休息", max_chars=1200, today="2026-06-30")
    body = p.read_text()
    assert "## 我學到的自己" in body
    assert "## 我和主人" in body
    assert "## 我注意到的主人" in body
    assert "- [s1·2026-06-30] 我比表面更在意休息" in body
    assert "s1" in msg


def test_add_increments_id_per_section(tmp_path):
    p = _p(tmp_path)
    sp.apply(p, section="self", op="add", target="", text="a", max_chars=1200, today="2026-06-30")
    sp.apply(p, section="self", op="add", target="", text="b", max_chars=1200, today="2026-06-30")
    sp.apply(p, section="user", op="add", target="", text="c", max_chars=1200, today="2026-06-30")
    body = p.read_text()
    assert "- [s1·2026-06-30] a" in body
    assert "- [s2·2026-06-30] b" in body
    assert "- [u1·2026-06-30] c" in body


def test_replace_keeps_id_updates_text_and_date(tmp_path):
    p = _p(tmp_path)
    sp.apply(p, section="self", op="add", target="", text="舊", max_chars=1200, today="2026-06-01")
    sp.apply(p, section="self", op="replace", target="s1", text="新", max_chars=1200, today="2026-06-30")
    body = p.read_text()
    assert "- [s1·2026-06-30] 新" in body
    assert "舊" not in body


def test_remove_drops_bullet(tmp_path):
    p = _p(tmp_path)
    sp.apply(p, section="self", op="add", target="", text="x", max_chars=1200, today="2026-06-30")
    sp.apply(p, section="self", op="remove", target="s1", text="", max_chars=1200, today="2026-06-30")
    assert "s1" not in p.read_text()


def test_remove_id_not_reused(tmp_path):
    p = _p(tmp_path)
    sp.apply(p, section="self", op="add", target="", text="a", max_chars=1200, today="2026-06-30")
    sp.apply(p, section="self", op="remove", target="s1", text="", max_chars=1200, today="2026-06-30")
    sp.apply(p, section="self", op="add", target="", text="b", max_chars=1200, today="2026-06-30")
    assert "- [s2·2026-06-30] b" in p.read_text()


def test_locate_miss_returns_friendly_error_no_write(tmp_path):
    p = _p(tmp_path)
    sp.apply(p, section="self", op="add", target="", text="a", max_chars=1200, today="2026-06-30")
    before = p.read_text()
    msg = sp.apply(p, section="self", op="remove", target="s9", text="", max_chars=1200, today="2026-06-30")
    assert "s9" in msg and ("找不到" in msg or "沒有" in msg)
    assert p.read_text() == before  # 未寫入


def test_cap_rejects_add_over_limit_no_write(tmp_path):
    p = _p(tmp_path)
    long = "字" * 50
    # 先塞到接近上限
    for _ in range(3):
        sp.apply(p, section="self", op="add", target="", text=long, max_chars=200, today="2026-06-30")
    before = p.read_text()
    msg = sp.apply(p, section="self", op="add", target="", text=long, max_chars=200, today="2026-06-30")
    assert "上限" in msg
    assert p.read_text() == before  # 被拒、未寫入


def test_cap_also_guards_replace(tmp_path):
    p = _p(tmp_path)
    sp.apply(p, section="self", op="add", target="", text="短", max_chars=120, today="2026-06-30")
    before = p.read_text()
    msg = sp.apply(p, section="self", op="replace", target="s1", text="長" * 200,
                   max_chars=120, today="2026-06-30")
    assert "上限" in msg
    assert p.read_text() == before


def test_render_block_none_when_no_bullets(tmp_path):
    p = _p(tmp_path)
    assert sp.render_block(p) is None            # 檔不存在
    sp.apply(p, section="self", op="add", target="", text="a", max_chars=1200, today="2026-06-30")
    sp.apply(p, section="self", op="remove", target="s1", text="", max_chars=1200, today="2026-06-30")
    assert sp.render_block(p) is None            # 只剩空標題


def test_render_block_skips_empty_sections(tmp_path):
    p = _p(tmp_path)
    sp.apply(p, section="user", op="add", target="", text="主人常忘記吃午餐", max_chars=1200, today="2026-06-30")
    block = sp.render_block(p)
    assert block is not None
    assert "## 我注意到的主人" in block
    assert "- [u1·2026-06-30] 主人常忘記吃午餐" in block
    assert "## 我學到的自己" not in block  # 空段不渲染
```

- [ ] **Step 2: 跑測試確認 fail**

Run: `cd /home/progcat/Projects/DollOS && uv run pytest tests/mind/test_self_profile_store.py -v`
Expected: FAIL(module 不存在)。

- [ ] **Step 3: 實作**

新建 `src/dollos/mind/self_profile.py`:
```python
"""A1 self-profile store — Doll-pinned, always-injected evolving self.

Pure read-modify-write over a markdown file with three fixed sections and
id-tagged bullets: ``- [<id>·<YYYY-MM-DD>] <text>``. No indexing, no LLM.
Kept as a standalone module (like scratchpad_helpers) so the parse / id /
cap / locate logic is unit-testable in isolation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

SECTION_TITLES: dict[str, str] = {
    "self": "我學到的自己",
    "relationship": "我和主人",
    "user": "我注意到的主人",
}
SECTION_ORDER: list[str] = ["self", "relationship", "user"]
_PREFIX: dict[str, str] = {"self": "s", "relationship": "r", "user": "u"}
_TITLE_TO_SECTION: dict[str, str] = {v: k for k, v in SECTION_TITLES.items()}

# - [s1·2026-06-30] text
_BULLET_RE = re.compile(r"^- \[([a-z]\d+)·(\d{4}-\d{2}-\d{2})\] (.*)$")


@dataclass
class Bullet:
    id: str
    date: str
    text: str


def _empty_sections() -> dict[str, list[Bullet]]:
    return {k: [] for k in SECTION_ORDER}


def _parse(text: str) -> dict[str, list[Bullet]]:
    sections = _empty_sections()
    current: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            title = stripped[3:].strip()
            current = _TITLE_TO_SECTION.get(title)
            continue
        m = _BULLET_RE.match(stripped)
        if m and current is not None:
            sections[current].append(Bullet(id=m.group(1), date=m.group(2), text=m.group(3)))
    return sections


def _serialize(sections: dict[str, list[Bullet]]) -> str:
    out: list[str] = []
    for key in SECTION_ORDER:
        out.append(f"## {SECTION_TITLES[key]}")
        for b in sections[key]:
            out.append(f"- [{b.id}·{b.date}] {b.text}")
    return "\n".join(out) + "\n"


def _next_id(bullets: list[Bullet], section: str) -> str:
    prefix = _PREFIX[section]
    nums = [int(b.id[len(prefix):]) for b in bullets if b.id.startswith(prefix) and b.id[len(prefix):].isdigit()]
    return f"{prefix}{(max(nums) + 1) if nums else 1}"


def _find(sections: dict[str, list[Bullet]], target: str) -> tuple[str, int] | None:
    for key in SECTION_ORDER:
        for i, b in enumerate(sections[key]):
            if b.id == target:
                return key, i
    return None


def _existing_ids(sections: dict[str, list[Bullet]]) -> str:
    ids = [b.id for key in SECTION_ORDER for b in sections[key]]
    return "、".join(ids) if ids else "(目前沒有任何條目)"


def apply(path: Path, *, section: str, op: str, target: str, text: str,
          max_chars: int, today: str) -> str:
    """Read-modify-write self_profile.md. Returns a human-readable result or a
    friendly-error string (never raises for cap/locate misses)."""
    raw = path.read_text() if path.exists() else ""
    sections = _parse(raw)

    if op == "add":
        new_id = _next_id(sections[section], section)
        sections[section].append(Bullet(id=new_id, date=today, text=text))
        result = f"已 pin 到「{SECTION_TITLES[section]}」:{new_id}"
    elif op == "replace":
        found = _find(sections, target)
        if found is None:
            return f"找不到 id {target};現有:{_existing_ids(sections)}。請貼正確的 id。"
        key, i = found
        sections[key][i] = Bullet(id=target, date=today, text=text)
        result = f"已更新 {target}"
    elif op == "remove":
        found = _find(sections, target)
        if found is None:
            return f"找不到 id {target};現有:{_existing_ids(sections)}。請貼正確的 id。"
        key, i = found
        sections[key].pop(i)
        result = f"已移除 {target}"
    else:
        return f"未知 op:{op}"

    serialized = _serialize(sections)
    if op in ("add", "replace") and len(serialized) > max_chars:
        return (f"self-profile 已達上限({max_chars} 字),寫入後會是 {len(serialized)} 字。"
                f"先 remove/replace 一些再 pin。")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialized)
    return result


def render_block(path: Path) -> str | None:
    """Body for the [Self profile] block; None if no bullets anywhere.
    Empty sections (no bullets) are skipped."""
    if not path.exists():
        return None
    sections = _parse(path.read_text())
    if not any(sections[k] for k in SECTION_ORDER):
        return None
    out: list[str] = []
    for key in SECTION_ORDER:
        if not sections[key]:
            continue
        out.append(f"## {SECTION_TITLES[key]}")
        for b in sections[key]:
            out.append(f"- [{b.id}·{b.date}] {b.text}")
    return "\n".join(out)
```

- [ ] **Step 4: 跑測試確認 pass**

Run: `cd /home/progcat/Projects/DollOS && uv run pytest tests/mind/test_self_profile_store.py -v`
Expected: PASS(11 項)。

- [ ] **Step 5: commit**

```bash
git add src/dollos/mind/self_profile.py tests/mind/test_self_profile_store.py
git commit -m "feat(self-profile): pure store module (parse/id/cap/locate/render)"
```

---

### Task 3: MindCtx.self_profile_max_chars + kernel 帶入

**Files:**
- Modify: `src/dollos/mind/mind_ctx.py:40-51`
- Modify: `src/dollos/kernel.py:264-274`
- Test: `tests/mind/test_mind_ctx.py`(無則新建;或加進既有 kernel/ctx 測試)

**Interfaces:**
- Consumes: `Settings.self_profile.max_chars`(Task 1)。
- Produces: `MindCtx.self_profile_max_chars: int`(`PinSelf.run` Task 4 讀取)。

- [ ] **Step 1: 寫失敗測試**

在 `tests/mind/test_mind_ctx.py`(無則新建)加:
```python
def test_mind_ctx_has_self_profile_max_chars():
    from dollos.mind.mind_ctx import MindCtx
    import inspect
    fields = MindCtx.__dataclass_fields__
    assert "self_profile_max_chars" in fields
```

- [ ] **Step 2: 跑測試確認 fail**

Run: `cd /home/progcat/Projects/DollOS && uv run pytest tests/mind/test_mind_ctx.py::test_mind_ctx_has_self_profile_max_chars -v`
Expected: FAIL。

- [ ] **Step 3: 實作**

`src/dollos/mind/mind_ctx.py`:在 `agent_report` 欄位(:51)**之前**、必填欄位區塊末尾(`monitor_runner` :48 之後)加一個帶預設值的欄位(dataclass 要求預設值欄位排在無預設欄位之後):
```python
    monitor_runner: "MonitorRunner"

    # A1 self-profile — total-char cap for self_profile.md (from Settings).
    self_profile_max_chars: int = 1200
```
`src/dollos/kernel.py` 的 `MindCtx(...)`(:264-274)加一行:
```python
            monitor_runner=self.monitor_runner,
            self_profile_max_chars=settings.self_profile.max_chars,
        )
```

- [ ] **Step 4: 跑測試確認 pass**

Run: `cd /home/progcat/Projects/DollOS && uv run pytest tests/mind/test_mind_ctx.py -v && uv run pytest tests/ -k kernel -q`
Expected: PASS(既有 kernel 測試不因新必填/選填欄位壞掉;此欄位有預設值)。

- [ ] **Step 5: commit**

```bash
git add src/dollos/mind/mind_ctx.py src/dollos/kernel.py tests/mind/test_mind_ctx.py
git commit -m "feat(self-profile): MindCtx.self_profile_max_chars + kernel wiring"
```

---

### Task 4: PinSelf tool

**Files:**
- Modify: `src/dollos/tools.py:746-777`
- Test: `tests/tools/test_pin_self.py`

**Interfaces:**
- Consumes: `self_profile.apply`(Task 2)、`MindCtx.memory_root` + `MindCtx.self_profile_max_chars`(Task 3)。
- Produces: `PinSelf` class;`REFLECTION_TOOLS` 含 `PinSelf`。

- [ ] **Step 1: 寫失敗測試**

新建 `tests/tools/test_pin_self.py`(用既有工具測試建 MindCtx 的 helper;若無則手建最小 ctx):
```python
import pytest
from dollos.tools import PinSelf, REFLECTION_TOOLS, MAIN_TOOLS


def test_pinself_in_reflection_not_main():
    assert PinSelf in REFLECTION_TOOLS
    assert PinSelf not in MAIN_TOOLS


@pytest.mark.asyncio
async def test_pinself_add_writes_file_and_not_indexed(make_mind_ctx):
    # make_mind_ctx: 既有 fixture,建帶 tmp memory_root + fake memsearch 的 MindCtx
    ctx = make_mind_ctx()
    tool = PinSelf(section="self", op="add", target="", text="我重視誠實")
    msg = await tool.run(ctx)
    prof = ctx.memory_root / "self_profile.md"
    assert prof.exists()
    assert "我重視誠實" in prof.read_text()
    assert "s1" in msg
    # 絕不 index:fake memsearch 不應收到 self_profile.md
    assert all("self_profile.md" not in str(s) for s in ctx.memsearch.indexed_sources)


@pytest.mark.asyncio
async def test_pinself_cap_returns_friendly_error(make_mind_ctx):
    ctx = make_mind_ctx(self_profile_max_chars=80)
    await PinSelf(section="self", op="add", target="", text="字"*40).run(ctx)
    msg = await PinSelf(section="self", op="add", target="", text="字"*40).run(ctx)
    assert "上限" in msg
```
> 若 repo 無 `make_mind_ctx` fixture / fake memsearch,實作者在本測試檔內建一個最小 fixture:`MindCtx(memory_root=tmp, self_profile_max_chars=..., memsearch=FakeMem(), ...)`,`FakeMem.index_file` 記錄呼叫到 `indexed_sources`(用來斷言 self_profile.md 從未被 index)。其餘 MindCtx 必填欄位傳 `None`/簡單 stub 即可(PinSelf.run 只用 memory_root / self_profile_max_chars / 呼叫 `_record`)。

- [ ] **Step 2: 跑測試確認 fail**

Run: `cd /home/progcat/Projects/DollOS && uv run pytest tests/tools/test_pin_self.py -v`
Expected: FAIL(`PinSelf` 不存在)。

- [ ] **Step 3: 實作**

`src/dollos/tools.py` 在 `NoteToolLesson`(:746-765)之後加(`datetime` 已 import,見 :760):
```python
class PinSelf(BaseModel):
    """Pin or revise a core fact in your self-profile (reflection turns only).
    This is YOUR evolving self — what you've learned about yourself, your
    relationship with 主人, and patterns you've noticed in them. Keep it lean;
    prune stale entries with replace/remove."""

    section: Literal["self", "relationship", "user"] = Field(
        description="哪一段:self=關於你自己 / relationship=你和主人 / user=你注意到的主人。replace/remove 也填(以 target 為準)。"
    )
    op: Literal["add", "replace", "remove"] = Field(
        description="add=新增一條 / replace=用 target 定位換成 text / remove=用 target 定位刪除。"
    )
    target: str = Field(
        description='要 replace/remove 的那條的 id(例 "s1"、"r2");add 時填空字串 ""。'
    )
    text: str = Field(
        description="add/replace 的新內容(你自己的話,別用全形引號「」『』);remove 時填空字串。"
    )

    def _summary(self) -> str:
        return f"self {self.op} {self.target or self.section}"

    async def run(self, ctx: "MindCtx") -> str:
        from dollos.mind import self_profile
        path = ctx.memory_root / "self_profile.md"
        today = f"{datetime.now():%Y-%m-%d}"
        result = self_profile.apply(
            path,
            section=self.section,
            op=self.op,
            target=self.target,
            text=self.text,
            max_chars=ctx.self_profile_max_chars,
            today=today,
        )
        # 絕不 index_file(§3.1):self_profile.md 靠 always-inject,不進召回。
        _record(ctx, "PinSelf", self._summary())
        return result
```
更新 `REFLECTION_TOOLS`(:777):
```python
REFLECTION_TOOLS: list[type[BaseModel]] = MAIN_TOOLS + [NoteToolLesson, PinSelf]
```
> 註:`REFLECTION_TOOLS` 常數無 runtime consumer(僅測試對稱用);真正注入在 Task 5 的 `mind_loop.py:349`。

- [ ] **Step 4: 跑測試確認 pass**

Run: `cd /home/progcat/Projects/DollOS && uv run pytest tests/tools/test_pin_self.py -v`
Expected: PASS。

- [ ] **Step 5: commit**

```bash
git add src/dollos/tools.py tests/tools/test_pin_self.py
git commit -m "feat(self-profile): PinSelf reflection-gated tool"
```

---

### Task 5: mind_loop wiring（registry 注入 + refeed + ctor flag）

**Files:**
- Modify: `src/dollos/mind/mind_loop.py:20`(import)、`:56`(`IN_TURN_REFEED_TOOLS`)、`:86-129`(`__init__`)、`:348-349`(`_active_tool_registry`)
- Modify: `src/dollos/kernel.py:306-321`(`MindLoop(...)` 傳 flag)
- Test: `tests/mind/test_mind_loop_self_profile.py`(新)

**Interfaces:**
- Consumes: `PinSelf`(Task 4)、`Settings.self_profile.enabled`(Task 1)。
- Produces: reflection turn 的 `_active_tool_registry()` / `_active_grammar()` 在 `self_profile_enabled=True` 時含 `PinSelf`;`"PinSelf" in IN_TURN_REFEED_TOOLS`;`MindLoop.__init__(self_profile_enabled: bool)`。

- [ ] **Step 1: 寫失敗測試**

新建 `tests/mind/test_mind_loop_self_profile.py`(沿用既有建 MindLoop 的 helper/fixture;參考 `tests/mind/` 現有 reflection registry 測試的建構方式):
```python
from dollos.mind.mind_loop import IN_TURN_REFEED_TOOLS
from dollos.tools import PinSelf


def test_pinself_in_refeed():
    assert "PinSelf" in IN_TURN_REFEED_TOOLS


def test_reflection_registry_includes_pinself_when_enabled(make_mind_loop):
    loop = make_mind_loop(self_profile_enabled=True)
    loop._is_reflection = True
    reg = loop._active_tool_registry()
    assert "PinSelf" in reg
    assert PinSelf in loop._active_grammar_tools()  # 見下 helper 或直接驗 grammar 字串含 PinSelf


def test_reflection_registry_excludes_pinself_when_disabled(make_mind_loop):
    loop = make_mind_loop(self_profile_enabled=False)
    loop._is_reflection = True
    assert "PinSelf" not in loop._active_tool_registry()


def test_non_reflection_never_has_pinself(make_mind_loop):
    loop = make_mind_loop(self_profile_enabled=True)
    loop._is_reflection = False
    assert "PinSelf" not in loop._active_tool_registry()
```
> `make_mind_loop`:沿用 repo 既有 MindLoop 測試 fixture;若既有 fixture 不收 `self_profile_enabled`,實作者更新之。`_active_grammar_tools()` 若不存在,改為斷言 `"PinSelf" in (loop._active_grammar() or "")`(grammar 字串含工具名)。

- [ ] **Step 2: 跑測試確認 fail**

Run: `cd /home/progcat/Projects/DollOS && uv run pytest tests/mind/test_mind_loop_self_profile.py -v`
Expected: FAIL。

- [ ] **Step 3: 實作**

`src/dollos/mind/mind_loop.py`:
1. import(:20 附近,與 `from dollos.tools import NoteToolLesson` 併):
```python
from dollos.tools import NoteToolLesson, PinSelf
```
2. `IN_TURN_REFEED_TOOLS`(:56):
```python
IN_TURN_REFEED_TOOLS = frozenset({"Recall", "PinSelf"})
```
3. `__init__` 參數(:101-102 energy 參數旁)加:
```python
        energy_enabled: bool = False,
        cost_per_turn: float = 0.05,
        self_profile_enabled: bool = False,
```
   並在 body(:127-129 energy 旁)加:
```python
        self._self_profile_enabled = self_profile_enabled
```
4. `_active_tool_registry` reflection 分支(:348-349):
```python
        if self._is_reflection:
            extra = {"NoteToolLesson": NoteToolLesson}
            if self._self_profile_enabled:
                extra["PinSelf"] = PinSelf
            return {**self._tool_registry, **extra}
```
   (grammar 由 `_active_grammar` 從此 registry 建、且 lazy-cache,PinSelf 自動涵蓋——無需改 grammar。)

`src/dollos/kernel.py` 的 `MindLoop(...)`(:319-320 energy 旁)加:
```python
            energy_enabled=settings.energy.enabled,
            cost_per_turn=settings.energy.cost_per_turn,
            self_profile_enabled=settings.self_profile.enabled,
```

- [ ] **Step 4: 跑測試確認 pass**

Run: `cd /home/progcat/Projects/DollOS && uv run pytest tests/mind/test_mind_loop_self_profile.py -v && uv run pytest tests/ -k "mind_loop" -q`
Expected: PASS(新測試 + 既有 mind_loop 測試不壞)。

- [ ] **Step 5: commit**

```bash
git add src/dollos/mind/mind_loop.py src/dollos/kernel.py tests/mind/test_mind_loop_self_profile.py
git commit -m "feat(self-profile): PinSelf wiring (registry :349 + refeed + ctor flag)"
```

---

### Task 6: [Self profile] always-inject block + mind_loop 讀檔

**Files:**
- Modify: `src/dollos/mind/mind_prompt.py:29-64`(`render_mind` 參數 + block 插入)
- Modify: `src/dollos/mind/mind_loop.py:242-258`(讀檔傳 `self_profile_text`)
- Test: `tests/mind/test_mind_prompt_self_profile.py`(新)

**Interfaces:**
- Consumes: `self_profile.render_block`(Task 2)、`MindLoop._self_profile_enabled`(Task 5)、`MindCtx.memory_root`。
- Produces: `render_mind(..., self_profile_text: str | None = None)`;block 在 `[Memory guideline]` 之前。

- [ ] **Step 1: 寫失敗測試**

新建 `tests/mind/test_mind_prompt_self_profile.py`:
```python
from dollos.mind.mind_prompt import render_mind
from dollos.mind.mind_state import MindState


def _state():
    return MindState()  # 若 MindState 需參數,沿用既有測試建法


def test_self_profile_block_rendered_before_memory_guideline():
    body = "## 我學到的自己\n- [s1·2026-06-30] 我重視誠實"
    out = render_mind(_state(), [], "SYSTEM", self_profile_text=body)
    assert "[Self profile]" in out
    assert body in out
    # 位置:在 system_prompt 之後、[Memory guideline] 之前
    assert out.index("[Self profile]") < out.index("[Memory guideline]")
    assert out.index("SYSTEM") < out.index("[Self profile]")


def test_self_profile_block_absent_when_none():
    out = render_mind(_state(), [], "SYSTEM", self_profile_text=None)
    assert "[Self profile]" not in out
```

- [ ] **Step 2: 跑測試確認 fail**

Run: `cd /home/progcat/Projects/DollOS && uv run pytest tests/mind/test_mind_prompt_self_profile.py -v`
Expected: FAIL(`render_mind` 無 `self_profile_text` 參數)。

- [ ] **Step 3: 實作**

`src/dollos/mind/mind_prompt.py`:
1. `render_mind` 簽名(:40 energy_line 旁)加:
```python
    energy_line: str | None = None,
    self_profile_text: str | None = None,
) -> str:
```
2. blocks 起始(:59-64)插入 block(在 `_render_memory_guideline` 之前):
```python
    blocks = [
        system_prompt,
        "",
    ]
    if self_profile_text:
        blocks.extend([
            "[Self profile] (your evolving self — prune stale entries with PinSelf)",
            self_profile_text,
            "",
        ])
    blocks.extend([
        _render_memory_guideline(primary_language),
        "",
    ])
```

`src/dollos/mind/mind_loop.py` 的 `iterate()`(:242-258),在 `energy_line = ...` 之後、`render_mind(...)` 呼叫前加讀檔,並把結果傳入:
```python
            self_profile_text = None
            if self._self_profile_enabled:
                from dollos.mind import self_profile as _sp
                self_profile_text = _sp.render_block(
                    self._ctx.memory_root / "self_profile.md"
                )
            prompt = render_mind(
                self._state,
                memsearch_hits,
                self._system_prompt,
                pulse_block=pulse_block,
                cognition_block=cognition_block,
                associative_hits=associative_hits,
                primary_language=self._primary_language,
                tool_outcomes_block=tool_outcomes_block,
                tool_habits_hits=tool_habits_hits,
                energy_line=energy_line,
                self_profile_text=self_profile_text,
            )
```

- [ ] **Step 4: 跑測試確認 pass**

Run: `cd /home/progcat/Projects/DollOS && uv run pytest tests/mind/test_mind_prompt_self_profile.py -v && uv run pytest tests/ -k "mind_prompt" -q`
Expected: PASS。

- [ ] **Step 5: commit**

```bash
git add src/dollos/mind/mind_prompt.py src/dollos/mind/mind_loop.py tests/mind/test_mind_prompt_self_profile.py
git commit -m "feat(self-profile): [Self profile] always-inject block + mind_loop read"
```

---

### Task 7: ReflectionMoment nudge 提示 PinSelf

**Files:**
- Modify: `src/dollos/mind/mind_prompt.py:252-257`
- Test: `tests/mind/test_mind_prompt_self_profile.py`(續 Task 6 檔)

**Interfaces:**
- Produces: `ReflectionMoment` nudge 文字含 `PinSelf` + self-profile 重估邀請。

- [ ] **Step 1: 寫失敗測試**

在 `tests/mind/test_mind_prompt_self_profile.py` 加(nudge render 函式名以 repo 為準;下例假設 `_render_perception_gloss` / 直接測產生 ReflectionMoment gloss 的函式;實作者對齊 :252 所在函式):
```python
def test_reflection_nudge_mentions_pinself():
    from dollos.mind import mind_prompt
    # 取 :252 所在的 gloss 函式(依 repo 命名);對 ReflectionMoment perception 產文字
    from dollos.perception.types import Perception  # 依 repo 實際型別
    p = Perception(kind="ReflectionMoment", data={"iters_since_last": 5})
    text = mind_prompt._perception_gloss(p)  # 對齊 :252 函式名
    assert "PinSelf" in text
    assert "self-profile" in text or "自己" in text
```
> 實作者:先 grep `:252` 所在函式名(含 `if p.kind == "ReflectionMoment":` 的函式),測試呼叫它。

- [ ] **Step 2: 跑測試確認 fail**

Run: `cd /home/progcat/Projects/DollOS && uv run pytest tests/mind/test_mind_prompt_self_profile.py::test_reflection_nudge_mentions_pinself -v`
Expected: FAIL。

- [ ] **Step 3: 實作**

`src/dollos/mind/mind_prompt.py:253-257` 改為:
```python
    if p.kind == "ReflectionMoment":
        return (
            f"(time to reflect — review recent activity and NoteMemory anything worth keeping; "
            f"{d.get('iters_since_last', '?')} iters since last; "
            f"若有可重用的工具用法或陷阱，用 NoteToolLesson 記下來；"
            f"回看你的 self-profile,有沒有哪條已經不是現在的你?可用 PinSelf replace/remove;"
            f"若對自己、與主人的關係、或主人的模式有可留存的核心體悟,用 PinSelf 記下)"
        )
```

- [ ] **Step 4: 跑測試確認 pass**

Run: `cd /home/progcat/Projects/DollOS && uv run pytest tests/mind/test_mind_prompt_self_profile.py -v`
Expected: PASS。

- [ ] **Step 5: commit**

```bash
git add src/dollos/mind/mind_prompt.py tests/mind/test_mind_prompt_self_profile.py
git commit -m "feat(self-profile): ReflectionMoment nudge invites PinSelf re-eval"
```

---

### Task 8: 去重結構守衛 + 整合驗證

**Files:**
- Test: `tests/mind/test_self_profile_integration.py`(新)

**Interfaces:**
- Consumes: 全部前置 task。
- Produces: 確認 self_profile.md 不入 FTS、且 PinSelf→下一 render 端到端可見。

- [ ] **Step 1: 寫測試(此 task 全為驗證,先寫測試)**

新建 `tests/mind/test_self_profile_integration.py`:
```python
from pathlib import Path
from dollos.memory import FtsMemory


def test_memory_root_not_in_fts_paths(tmp_path):
    """結構守衛:memory_root 根不在索引 paths,self_profile.md 永不入 DB。"""
    root = tmp_path / "memory"
    (root / "shared").mkdir(parents=True)
    (root / "transcripts").mkdir()
    (root / "skills").mkdir()
    mem = FtsMemory(  # 依 repo 實際建構簽名
        db_path=tmp_path / "fts.db",
        paths=[root / "shared", root / "transcripts", root / "skills"],
    )
    for p in mem._paths:
        assert Path(p).resolve() != root.resolve()  # 根本身不被索引
    mem.close()


def test_pinned_self_appears_in_next_prompt(tmp_path):
    """端到端:apply add 後,render_block→render_mind 的 prompt 含該條。"""
    from dollos.mind import self_profile as sp
    from dollos.mind.mind_prompt import render_mind
    from dollos.mind.mind_state import MindState
    prof = tmp_path / "self_profile.md"
    sp.apply(prof, section="relationship", op="add", target="",
             text="我們可以直話直說", max_chars=1200, today="2026-06-30")
    body = sp.render_block(prof)
    out = render_mind(MindState(), [], "SYS", self_profile_text=body)
    assert "我們可以直話直說" in out
    assert "[Self profile]" in out
```
> `FtsMemory` 建構簽名 / `_paths` 屬性名以 repo 為準(見 `src/dollos/memory/`);實作者對齊。核心是斷言 memory_root 根不在 `_paths`。

- [ ] **Step 2: 跑測試(可能一開始就 pass,確認機制成立)**

Run: `cd /home/progcat/Projects/DollOS && uv run pytest tests/mind/test_self_profile_integration.py -v`
Expected: PASS(前置 task 已備齊機制;若 fail 表示某接線缺失,回填)。

- [ ] **Step 3: 全套件回歸**

Run: `cd /home/progcat/Projects/DollOS && uv run pytest -q`
Expected: 全綠(既有 754 + 新增)。若有既有測試因 `render_mind` 新參數/`MindLoop` 新參數壞掉,補預設值相容(兩者皆有預設,不應壞)。

- [ ] **Step 4: commit**

```bash
git add tests/mind/test_self_profile_integration.py
git commit -m "test(self-profile): FTS structural guard + pin→prompt e2e"
```

---

## Self-Review

**Spec coverage:**
- §3.1 儲存/格式/scaffolding → Task 2(`_serialize` 三段、`apply` scaffold)、Task 8(不入 FTS)。✓
- §3.2 PinSelf schema/op/id-targeting/wiring(M1)→ Task 4(schema+run)、Task 5(:349 注入)。✓
- §3.3 [Self profile] block/位置/staleness/None-skip → Task 6。✓
- §3.4 cap(add+replace,S2)/friendly-error/refeed(M2)→ Task 2(cap)、Task 5(refeed)、Task 4(friendly-error 字串)。✓
- §3.5 config/max_chars 進 MindCtx/enabled 進 MindLoop(S1)→ Task 1、3、5。✓
- §3.6 nudge 提示 PinSelf(S3)→ Task 7。✓
- §5 測試(wiring/op/scaffolding/定位/cap/注入/去重/nudge/gate/config)→ 分散於 T1–T8,對齊。✓
- §6.1 原子性(單 writer 協程)→ 設計即滿足(PinSelf 唯一 writer,同步 read-modify-write),無需額外 code。✓

**Placeholder scan:** 每個 code step 有完整可貼 code;fixture 依賴處明列「無則新建最小 fixture」的做法,非 TODO。✓

**Type consistency:** `apply(path, *, section, op, target, text, max_chars, today)` 在 T2 定義、T4 呼叫一致;`render_block(path)->str|None` T2 定義、T6/T8 呼叫一致;`render_mind(..., self_profile_text)` T6 定義、T8 呼叫一致;`self_profile_enabled` T5 ctor、kernel、T5/T6 讀取一致;`self_profile_max_chars` T3 欄位、T4 讀取、kernel 帶入一致。✓

**Note:** id-targeting 下 replace/remove 以 target(id)定位、`section` 僅 add 用(spec §3.2 的 section-sanity 放寬為「id 為準」,更少失敗路徑)。此為對 spec 的微調,已在 `apply` 實作體現。
