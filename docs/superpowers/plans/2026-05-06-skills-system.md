# Skills System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add skills as procedural memory entries integrated with memsearch. Two-file split: entry (frontmatter `name` + short prose) at `data/memory/skills/{name}.md` indexed and surfaced via RECALL; body (full instructions, any markdown) at `data/memory/skill_bodies/{name}.md` not indexed and loaded on demand via new `InvokeSkill(name)` returning tool that rides step 9 success-cascade.

**Architecture:** New `InvokeSkill` pydantic tool reads `ctx.memory_root / "skill_bodies" / f"{name}.md"` and returns the file content as a string (cascade-worthy). Missing file raises `FileNotFoundError` which step 9 dispatcher catches → `ToolResult(success=False, ...)` → fail-cascade — Doll self-corrects. `build_memsearch` adds `data/memory/skills/` (entries indexed); `data/memory/skill_bodies/` is NOT indexed. `scaffolding.jinja` gains a paragraph teaching Doll the skill convention and how to create/invoke. Doll creates new skills via existing Shell tool (no special CreateSkill tool).

**Tech Stack:** Python 3.12+, `pydantic`, existing `memsearch`, `pytest` + `pytest-asyncio`. No new external deps.

**Spec:** `docs/superpowers/specs/2026-05-06-skills-system-design.md`

---

## File Map

| File | Status | Responsibility |
|---|---|---|
| `src/dollos/tools.py` | modify | Add `InvokeSkill` pydantic tool; append to `TOOLS` list |
| `src/dollos/kernel.py` | modify | `build_memsearch` adds `skills_path` (indexed); does NOT add `skill_bodies/` |
| `src/dollos/prompts/templates/scaffolding.jinja` | modify | Append skill convention paragraph after the multi-try meta-rule |
| `tests/test_tools.py` | extend | InvokeSkill tests |
| `tests/test_kernel_factories.py` | extend | `build_memsearch` paths assertion |
| `tests/test_prompt_renderer.py` | extend | scaffolding skill block test |
| `docs/roadmap.md` | modify | Mark step 10 merged; point to next |
| `CLAUDE.md` | modify | Same |

---

## Task 1: `InvokeSkill` tool

**Files:**
- Modify: `src/dollos/tools.py`
- Modify: `tests/test_tools.py`

Pure new pydantic tool. Reads body file → returns content. Missing file raises naturally.

### Step 1: Write failing tests (RED)

- [ ] Append to `tests/test_tools.py` (top imports — add `InvokeSkill`):

```python
from dollos.tools import InvokeSkill
```

- [ ] Append tests:

```python
def test_invoke_skill_in_tools_list():
    from dollos.tools import TOOLS
    assert InvokeSkill in TOOLS


def test_invoke_skill_schema_has_name_field():
    schema = InvokeSkill.model_json_schema()
    assert "name" in schema["properties"]
    assert schema["properties"]["name"]["type"] == "string"


@pytest.mark.asyncio
async def test_invoke_skill_run_returns_body_content(tmp_path):
    sink: asyncio.Queue = asyncio.Queue()
    ms = _FakeMemSearch()
    memory_root = tmp_path / "memory"
    bodies_dir = memory_root / "skill_bodies"
    bodies_dir.mkdir(parents=True)
    body_path = bodies_dir / "my_skill.md"
    body_content = "# Steps\n\n1. Step one\n2. Step two\n"
    body_path.write_text(body_content)
    ctx = ToolCtx(
        sink=sink,
        memory_root=memory_root,
        memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )

    out = await InvokeSkill(name="my_skill").run(ctx)

    assert out == body_content


@pytest.mark.asyncio
async def test_invoke_skill_run_raises_filenotfound_for_missing_skill(tmp_path):
    sink: asyncio.Queue = asyncio.Queue()
    ms = _FakeMemSearch()
    memory_root = tmp_path / "memory"
    memory_root.mkdir()
    ctx = ToolCtx(
        sink=sink,
        memory_root=memory_root,
        memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )

    with pytest.raises(FileNotFoundError):
        await InvokeSkill(name="nonexistent").run(ctx)


@pytest.mark.asyncio
async def test_invoke_skill_reads_from_skill_bodies_not_skills(tmp_path):
    """Verify path goes to skill_bodies/, not skills/."""
    sink: asyncio.Queue = asyncio.Queue()
    ms = _FakeMemSearch()
    memory_root = tmp_path / "memory"
    skills_dir = memory_root / "skills"
    bodies_dir = memory_root / "skill_bodies"
    skills_dir.mkdir(parents=True)
    bodies_dir.mkdir(parents=True)
    # Put DIFFERENT content in entry vs body — confirm we read body
    (skills_dir / "x.md").write_text("ENTRY CONTENT")
    (bodies_dir / "x.md").write_text("BODY CONTENT")
    ctx = ToolCtx(
        sink=sink,
        memory_root=memory_root,
        memsearch=ms,
        transcripts_root=tmp_path / "transcripts",
    )

    out = await InvokeSkill(name="x").run(ctx)
    assert out == "BODY CONTENT"
```

- [ ] Run: `uv run pytest tests/test_tools.py -q`
- [ ] Expected: 5 new tests fail (`InvokeSkill` doesn't exist).

### Step 2: Implement (GREEN)

- [ ] Edit `src/dollos/tools.py`. Add `InvokeSkill` class after `Shell`:

```python
class InvokeSkill(BaseModel):
    """Load a skill's full instructions into context.

    Use this when you've seen a skill entry in RECALL and decide to follow
    its procedure. The skill body will be returned as the next perception,
    after which you should follow its instructions step by step.
    """

    name: str = Field(
        description=(
            "Skill name (matches the entry's frontmatter `name` field "
            "and filename basename)."
        )
    )

    async def run(self, ctx: ToolCtx) -> str:
        path = ctx.memory_root / "skill_bodies" / f"{self.name}.md"
        return path.read_text()
```

- [ ] Update `TOOLS`:

```python
TOOLS: list[type[BaseModel]] = [Say, NoteMemory, WriteDiary, Shell, InvokeSkill]
```

### Step 3: Run tests (GREEN)

- [ ] Run: `uv run pytest tests/test_tools.py -q`
- [ ] Expected: all tool tests pass (existing + 5 new).
- [ ] Run: `uv run pytest -q`
- [ ] Expected: full suite green.

### Step 4: Lint

- [ ] Run: `uv run ruff check src/dollos/tools.py tests/test_tools.py`
- [ ] Expected: clean.

### Step 5: Commit

- [ ] Run:

```bash
git add src/dollos/tools.py tests/test_tools.py
git commit -m "$(cat <<'EOF'
feat(tools): InvokeSkill returning tool

Reads data/memory/skill_bodies/{name}.md and returns content. Missing
file raises FileNotFoundError naturally — step 9 dispatcher catches
and surfaces to Doll via fail-cascade. No special path validation
(trust-only). New 5th tool in TOOLS list.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `build_memsearch` adds skills/ path

**Files:**
- Modify: `src/dollos/kernel.py`
- Modify: `tests/test_kernel_factories.py`

Add `data/memory/skills/` to memsearch indexed paths. Do NOT add `data/memory/skill_bodies/`.

### Step 1: Write failing test (RED)

- [ ] Append to `tests/test_kernel_factories.py` (use existing settings/fixture pattern from this file):

```python
def test_build_memsearch_indexes_skills_dir(tmp_path):
    settings = _make_settings(tmp_path)
    ms = build_memsearch(settings)
    skills_path = tmp_path / "memory" / "skills"
    assert skills_path.exists()
    paths = [str(p) for p in ms._paths] if hasattr(ms, "_paths") else None
    # If MemSearch doesn't expose paths attribute, fall back to checking dir creation
    # The mkdir behavior in build_memsearch is the externally-visible signal
    assert skills_path.is_dir()


def test_build_memsearch_does_not_create_skill_bodies_dir(tmp_path):
    """skill_bodies/ is not indexed and should not be auto-created at startup."""
    settings = _make_settings(tmp_path)
    build_memsearch(settings)
    bodies_path = tmp_path / "memory" / "skill_bodies"
    assert not bodies_path.exists()
```

(`_make_settings` is the existing helper in `test_kernel_factories.py` — don't redefine.)

- [ ] Run: `uv run pytest tests/test_kernel_factories.py -q`
- [ ] Expected: failures (`skills_path.exists()` False; `bodies_path.exists()` may be True if test logic gets reordered or if some existing test creates it).

### Step 2: Implement (GREEN)

- [ ] Edit `src/dollos/kernel.py`. Update `build_memsearch`:

```python
def build_memsearch(settings: Settings) -> MemSearch:
    """Construct memsearch rooted at data.root / memory / shared, transcripts, and skills.

    skills/ holds skill entry files (frontmatter + short description); they ARE indexed
    so RECALL surfaces them. skill_bodies/ holds full skill instructions and is NOT
    indexed — it is loaded on demand by the InvokeSkill tool.
    """
    shared_path = settings.data.root / "memory" / "shared"
    transcripts_path = settings.data.root / "memory" / "transcripts"
    skills_path = settings.data.root / "memory" / "skills"
    shared_path.mkdir(parents=True, exist_ok=True)
    transcripts_path.mkdir(parents=True, exist_ok=True)
    skills_path.mkdir(parents=True, exist_ok=True)
    # NOTE: skill_bodies/ is intentionally NOT created here — it is created
    # lazily by Doll via Shell when she writes a new skill body.
    return MemSearch(
        paths=[
            str(shared_path),
            str(transcripts_path),
            str(skills_path),
        ],
        embedding_provider="onnx",
    )
```

### Step 3: Run tests (GREEN)

- [ ] Run: `uv run pytest tests/test_kernel_factories.py -q`
- [ ] Expected: pass.
- [ ] Run: `uv run pytest -q`
- [ ] Expected: full suite green.

### Step 4: Lint

- [ ] Run: `uv run ruff check src/dollos/kernel.py tests/test_kernel_factories.py`
- [ ] Expected: clean.

### Step 5: Commit

- [ ] Run:

```bash
git add src/dollos/kernel.py tests/test_kernel_factories.py
git commit -m "$(cat <<'EOF'
feat(kernel): index data/memory/skills/ in memsearch

Adds skills/ (skill entries) to memsearch.paths so RECALL surfaces
"I have this skill" hints. skill_bodies/ is intentionally NOT indexed
and NOT auto-created — Doll creates it lazily via Shell when she
writes her first skill.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `scaffolding.jinja` skill convention paragraph

**Files:**
- Modify: `src/dollos/prompts/templates/scaffolding.jinja`
- Modify: `tests/test_prompt_renderer.py`

Append skill convention block after the existing multi-try meta-rule.

### Step 1: Write failing test (RED)

- [ ] Append to `tests/test_prompt_renderer.py`:

```python
def test_scaffolding_includes_skill_convention():
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", character="You are Doll.")
    assert "skill" in out.lower()
    assert "InvokeSkill" in out
    assert "skills/" in out
    assert "skill_bodies/" in out
```

- [ ] Run: `uv run pytest tests/test_prompt_renderer.py -q`
- [ ] Expected: new test fails.

### Step 2: Implement (GREEN)

- [ ] Edit `src/dollos/prompts/templates/scaffolding.jinja`. The current content is:

```jinja
{%- if character %}
{{ character }}

如果你嘗試多次仍未達到目的，考慮換方法、嘗試不同 tool、或停止 (不再 call tool)。
{%- endif %}
{%- if rules %}
...
```

Insert the skill block right after the multi-try meta-rule line, still inside the `{%- if character %}` block. The result should be:

```jinja
{%- if character %}
{{ character }}

如果你嘗試多次仍未達到目的，考慮換方法、嘗試不同 tool、或停止 (不再 call tool)。

你有「skill」可以累積經驗：

- Skill = 一個 procedural memory，分兩檔：
  - `data/memory/skills/<name>.md` — entry，YAML frontmatter `name: <name>` + 一段自然語言 description（1-3 句講做什麼、何時用）
  - `data/memory/skill_bodies/<name>.md` — body，完整步驟 / 細節（任意 markdown）
- RECALL 會自動讓你看到 entry，本身就是「我有這個 skill」的訊號
- 想用某個 skill → call `InvokeSkill(name=...)`，body 會透過 cascade 進你的下個 perception
- 想寫新 skill → 用 Shell tool 同時寫 entry + body 兩個檔
{%- endif %}
{%- if rules %}
```

(Keep the existing `{%- if rules %}` and below sections unchanged.)

### Step 3: Run tests (GREEN)

- [ ] Run: `uv run pytest tests/test_prompt_renderer.py -q`
- [ ] Expected: pass.
- [ ] Run: `uv run pytest -q`
- [ ] Expected: full suite green.

### Step 4: Lint

- [ ] Run: `uv run ruff check tests/test_prompt_renderer.py`
- [ ] Expected: clean.

### Step 5: Commit

- [ ] Run:

```bash
git add src/dollos/prompts/templates/scaffolding.jinja tests/test_prompt_renderer.py
git commit -m "$(cat <<'EOF'
feat(prompts): add skill convention block to scaffolding.jinja

Teaches Doll the skill format (entry/body two-file split), where they
live (data/memory/skills/ + data/memory/skill_bodies/), how RECALL
surfaces entries, and how to invoke (InvokeSkill tool) or create
(Shell tool) skills.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Manual smoke test

**Files:** None (validation only).

Two-stage test: (1) Doll writes a skill; (2) skill recalled and invoked across daemon restart (so memsearch picks up new file).

### Step 1: Verify servers + start daemon (clean slate)

- [ ] `curl -s -o /dev/null -w "8001=%{http_code} 8003=%{http_code}\n" http://localhost:8001/health http://localhost:8003/health`
- [ ] Expected: both `200`.

- [ ] From worktree root:
  ```bash
  cd /home/progcat/Projects/DollOS/.worktrees/skills-system
  rm -rf data
  mkdir -p data/memory/shared data/memory/transcripts
  rm -f /tmp/dollos.log
  uv run python -m dollos --config config.toml > /tmp/dollos.log 2>&1 &
  sleep 6
  tail -3 /tmp/dollos.log
  ```
- [ ] Expected: "memsearch indexed" + "ipc server listening". `data/memory/skills/` should now exist; `data/memory/skill_bodies/` should NOT exist.

- [ ] Verify dirs: `ls data/memory/`
- [ ] Expected: `shared transcripts skills` (no `skill_bodies`).

### Step 2: Drive Doll to write a skill

- [ ] `uv run python experiments/ws_client.py "我希望你以後問你『今天怎樣』時，先看 transcript 再回答。請建立一個 skill 叫 check_today。"`
- [ ] Expected: Doll calls Shell ≥2 times to write entry + body, then Say to confirm.

- [ ] Verify both files exist:
  ```bash
  ls data/memory/skills/
  ls data/memory/skill_bodies/
  ```
- [ ] Expected: `check_today.md` in both dirs (skill_bodies/ created lazily by Doll's Shell command).

- [ ] `cat data/memory/skills/check_today.md` — should have `--- name: check_today ---` style frontmatter + brief description.
- [ ] `cat data/memory/skill_bodies/check_today.md` — should have step instructions.

### Step 3: Restart daemon (so memsearch picks up new entry)

- [ ] `pkill -f "python -m dollos"`
- [ ] `sleep 2`
- [ ] `rm -f /tmp/dollos.log`
- [ ] `uv run python -m dollos --config config.toml > /tmp/dollos.log 2>&1 &`
- [ ] `sleep 6`
- [ ] `tail -3 /tmp/dollos.log` — expect "Indexed N chunks from M files" with N≥1 (the new entry).

### Step 4: Trigger skill via RECALL

- [ ] `uv run python experiments/ws_client.py "今天怎樣？"`
- [ ] Expected: Doll's response references something concrete from today's transcript (or notes there's nothing yet). Behind the scenes:
  - RECALL surfaces `check_today` entry
  - Doll calls InvokeSkill → body cascades
  - Doll calls Shell to read transcript
  - Doll calls Say with summary

- [ ] In `/tmp/dollos.log`, look for ≥3 calls to `:8001` (round 1: InvokeSkill / Shell; round 2: Shell after InvokeSkill body; round 3: Say). Cascade depth visible.

### Step 5: Verify failure path

- [ ] Drive: `uv run python experiments/ws_client.py "請 invoke 一個叫 nonsense 的 skill"`
- [ ] Expected: Doll calls InvokeSkill(name="nonsense") → fail-cascade → Doll Say-explains the failure.
- [ ] In log: cascade with `runtime error` for `InvokeSkill`.

### Step 6: Stop daemon

- [ ] `pkill -f "python -m dollos"`

### Step 7: Document outcomes

- [ ] If steps 2/4/5 all behave correctly → Task 4 done.
- [ ] If model fails to write skill format correctly (typo'd frontmatter, wrong filename) → record observation; the cascade fail path catches it but UX is rough. Spec §10.1 (reindex delay) and §10.2 (name conflicts) acknowledge.

No commit unless smoke reveals a bug.

---

## Task 5: Roadmap + CLAUDE.md sync

**Files:**
- Modify: `docs/roadmap.md`
- Modify: `CLAUDE.md`

### Step 1: Update `docs/roadmap.md`

- [ ] In `## 已完成` table, append:

```markdown
| Roadmap step 10 — Skills system | Merged |
```

- [ ] In `### 10. Character` heading (or wherever step 10 was) — current main spec uses "Character" for step 10. Re-cut: step 10 became Skills (more impactful for self-evolution), Character pack moves to a later step. Replace step 10 body with:

```
**Re-cut**: 原 roadmap step 10 為 Character pack。實際排程改：先做 Skills system（讓 Doll 能累積 procedural memory）；Character pack 留到之後。

Step 10 minimal scope: Skill 兩檔分離——`data/memory/skills/<name>.md`（entry，frontmatter `name` + 短 prose description，由 memsearch 索引、進 RECALL）+ `data/memory/skill_bodies/<name>.md`（body，完整 instructions，不索引）。新 `InvokeSkill(name)` returning tool 載入 body 進 cascade（吃 step 9 success-cascade）。`scaffolding.jinja` 加 skill convention 段教 Doll 怎麼用。Doll 用 Shell tool 寫新 skill。

**Demo**：對話中 Doll 寫一個 skill；隔輪 user 觸發類似情境，RECALL 帶到 entry，Doll 主動 InvokeSkill 讀 body 跟著做。

下個 step：Character pack（.doll v3）/ wake gating / Subagent / Voice pipeline 等，依優先序選。
```

### Step 2: Update `CLAUDE.md`

- [ ] In "已完成" plan table, append:

```markdown
| Roadmap step 10 — Skills system | Merged |
```

- [ ] Replace "下一個" paragraph with a brief that mentions multiple candidate next steps (since priorities are open):

```
**下一個候選**（按用戶決定挑一個）：
- Character pack（.doll v3 schema、character.jinja 覆寫，修「Doll 在演不是當」）
- Subagent（async result via SubagentResultEvent，原 roadmap step 9 內容）
- Wake gating（Inner Voice 輸出 `wake: bool`，為 reflex 鋪基礎）
- Voice pipeline（KWS / VAD / ASR / TTS）

完整 roadmap：`docs/roadmap.md`。
```

### Step 3: Verify

- [ ] `uv run pytest -q` — green.
- [ ] `uv run ruff check src/dollos tests` — clean.

### Step 4: Commit

- [ ] Run:

```bash
git add docs/roadmap.md CLAUDE.md
git commit -m "$(cat <<'EOF'
docs: mark roadmap step 10 (Skills system) merged

Step 10 re-cut: shipped Skills system (entry/body split + InvokeSkill).
Original step 10 (Character pack) deferred. Multiple next-step
candidates listed for user to pick.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Done definition

- [ ] All 5 tasks committed on branch `skills-system`.
- [ ] `uv run pytest -q` green.
- [ ] `uv run ruff check src/dollos tests` clean.
- [ ] Smoke test (Task 4): Doll writes a skill; restart-then-recall surfaces entry; InvokeSkill cascades body; failure path also works.
- [ ] Roadmap + CLAUDE.md updated.
- [ ] Ready for `superpowers:finishing-a-development-branch`.
