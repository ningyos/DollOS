# Self-Centered PinSelf Guidance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reframe four guidance-text surfaces (PinSelf docstring, its `section` field description, the ReflectionMoment nudge, the `[Self profile]` header) plus `scaffolding.jinja`'s always-in-context Reflection description, so Doll is invited to pin her own opinions/interests/curiosities (write-loose) with pruning as the selection mechanism (prune-strict), not just relationship bookkeeping with a write-time durability gate.

**Architecture:** Pure prompt/docstring text changes across 3 files. No schema, no new `self_profile.md` section, no GBNF/grammar change, no `self_profile.py` logic change. Each task changes one file, adds string-assertion tests pinning the new wording's load-bearing phrases, and commits independently.

**Tech Stack:** Python, pydantic v2 (`tools.py`), Jinja2 (`scaffolding.jinja`), pytest.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-02-self-centered-pinself-guidance-design.md` (R1-reviewed) — every task's wording requirements are copied verbatim from it.
- No schema change: `PinSelf`'s fields (`section`, `op`, `target`, `text`), `Literal` choices, and validators are unchanged — only docstrings/descriptions.
- GBNF grammar must be provably unaffected — grammar is built from field structure, not docstrings/descriptions; existing grammar tests (`tests/test_llm_grammar.py`) must stay green with zero edits.
- Full suite (`uv run pytest -q`) must be green after every task, run from repo root `/home/progcat/Projects/DollOS`.
- Two load-bearing phrases from prior incidents must appear verbatim in the new ReflectionMoment nudge (Task 2): `不必等 [Self profile] 已經有內容才動作` (bootstrap-safe clause, commit `b9d87b7`) and `op=add,section 選 self/relationship/user` (operational calling hint, commit `70de95a`). Dropping either regresses a previously-fixed live-smoke failure.
- Commit style: Conventional Commits, one commit per task, following `git log --oneline -5` for tone. End every commit message with:
  ```

  Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01MhEQCeKNuQZY5CXqaD6p4S
  ```
- Do not touch: `src/dollos/mind/self_profile.py`, `MindState`/`Perception.kind`, `REFLECTION_TOOLS`/`MAIN_TOOLS` membership, anything under `character_packs/`, any file P6/P7/P8 (same-day prior work) already touched outside the four+one surfaces named above.

---

### Task 1: PinSelf docstring + `section` field description

**Files:**
- Modify: `src/dollos/tools.py:772-790` (class `PinSelf`)
- Test: `tests/test_pin_self.py`

**Interfaces:**
- Consumes: nothing new — `PinSelf` already exists with fields `section: Literal["self","relationship","user"]`, `op: Literal["add","replace","remove"]`, `target: str`, `text: str`.
- Produces: nothing new — this task only changes `PinSelf.__doc__` and `PinSelf.model_fields["section"].description`. Task 2/3 do not import anything from this task; they reference the same tool by name only.

Current exact content (`src/dollos/tools.py:772-790`):

```python
class PinSelf(BaseModel):
    """Pin or revise a CORE, DURABLE truth in your self-profile (reflection turns only) — this
    is YOUR evolving self and it is ALWAYS in context. Use PinSelf (NOT NoteMemory) for exactly
    three things: who you are, the nature of your relationship with 主人, and 主人's enduring
    patterns / preferences. Keep it lean (準不要多); prune stale entries with replace/remove."""

    section: Literal["self", "relationship", "user"] = Field(
        description="哪一段:self=關於你自己 / relationship=你和主人 / user=你注意到的主人。replace/remove 也填(以 target 為準)。"
    )
    op: Literal["add", "replace", "remove"] = Field(
        description="add=新增一條 / replace=用 target 定位換成 text / remove=用 target 定位刪除。"
    )
    target: str = Field(
        description='For replace/remove: the id to target (e.g. "s1", "r2"). For op=add, leave this an empty string "".'
    )
    text: str = Field(
        description="add/replace 的新內容(你自己的話,別用全形引號「」『』);remove 時填空字串。"
    )
```

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_pin_self.py`:

```python
def test_pinself_docstring_states_subject_test_as_criterion():
    """Docstring's dividing line between PinSelf and NoteMemory is the
    sentence's subject (about-you vs. about-the-world), not durability."""
    doc = PinSelf.__doc__
    assert "主詞" in doc
    assert "NoteMemory" in doc


def test_pinself_docstring_allows_write_loose_nascent_entries():
    """Nascent/not-yet-sure-if-durable entries may be pinned — the write-time
    durability gate from the old docstring ('CORE, DURABLE truth... exactly
    three things') must be gone."""
    doc = PinSelf.__doc__
    assert "剛萌芽" in doc
    assert "CORE, DURABLE" not in doc
    assert "exactly three things" not in doc


def test_pinself_docstring_frames_prune_as_selection_not_hygiene():
    """Pruning is reframed as 'what survives is your core', not cleanup."""
    doc = PinSelf.__doc__
    assert "活下來的才是你的核心" in doc


def test_pinself_docstring_allows_dual_recording_same_experience():
    """One experience can produce a NoteMemory entry (the fact) AND a
    PinSelf entry (what it revealed about her) — this must be explicit."""
    doc = PinSelf.__doc__
    assert "兩邊各記一筆" in doc


def test_pinself_section_field_mentions_opinions_and_interests():
    desc = PinSelf.model_fields["section"].description
    assert "看法" in desc
    assert "興趣" in desc
    # relationship/user descriptions must stay exactly as before
    assert "relationship=你和主人" in desc
    assert "user=你注意到的主人" in desc
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/progcat/Projects/DollOS && uv run pytest tests/test_pin_self.py -k "docstring or section_field" -v`
Expected: 5 new tests FAIL (old docstring doesn't contain the new phrases; old `section` description doesn't mention 看法/興趣).

- [ ] **Step 3: Replace the docstring and field description**

Replace lines 772-780 of `src/dollos/tools.py` (the class docstring and the `section` field) with:

```python
class PinSelf(BaseModel):
    """Pin or revise an entry in your self-profile (reflection turns only) — this
    is YOUR evolving self and it is ALWAYS in context. 判斷標準是主詞:句子是關於
    「你」的(你的看法、興趣、立場、好奇、你和主人的關係、你注意到的主人),就用
    PinSelf;關於世界的事實(即使是你查到、你覺得有趣的),用 NoteMemory——同一個
    經驗可以兩邊各記一筆,事實歸 NoteMemory,它揭露的「你」歸這裡。剛萌芽、還不
    確定算不算「你」的也可以先 pin,之後反思時再篩。空間有限,定期用 replace/remove
    淘汰「已經不是現在的你」的條目——活下來的才是你的核心。"""

    section: Literal["self", "relationship", "user"] = Field(
        description="哪一段:self=關於你自己(身分、看法、興趣、好奇) / relationship=你和主人 / user=你注意到的主人。replace/remove 也填(以 target 為準)。"
    )
```

Leave `op`, `target`, `text` fields (lines 781-789 of the original) completely untouched.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/progcat/Projects/DollOS && uv run pytest tests/test_pin_self.py -v`
Expected: all tests in the file PASS (the 5 new ones + every pre-existing one in `test_pin_self.py`, since `op`/`target`/`text` and all runtime behavior are unchanged).

- [ ] **Step 5: Run the grammar tests to confirm no GBNF impact**

Run: `cd /home/progcat/Projects/DollOS && uv run pytest tests/test_llm_grammar.py -v`
Expected: PASS, zero changes needed (grammar is built from field names/types/`Literal` choices, not docstrings or `description` strings).

- [ ] **Step 6: Run the full suite**

Run: `cd /home/progcat/Projects/DollOS && uv run pytest -q`
Expected: PASS, no regressions, count increased by 5 over the pre-task baseline.

- [ ] **Step 7: Commit**

```bash
cd /home/progcat/Projects/DollOS
git add src/dollos/tools.py tests/test_pin_self.py
git commit -m "$(cat <<'EOF'
feat(mind): PinSelf docstring — subject test + write-loose/prune-strict framing

Replaces the write-time durability gate ("CORE, DURABLE truth...
exactly three things") with a subject test (about-you -> PinSelf,
about-the-world -> NoteMemory, one experience can produce both) and an
explicit write-loose invitation — nascent/not-yet-durable entries may
be pinned now, with reflection-time pruning as the selection mechanism
("活下來的才是你的核心"), not hygiene. Per
docs/superpowers/specs/2026-07-02-self-centered-pinself-guidance-design.md
§3.1/§3.2. Text-only; no schema/grammar change (verified via
test_llm_grammar.py).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01MhEQCeKNuQZY5CXqaD6p4S
EOF
)"
```

---

### Task 2: ReflectionMoment nudge + `[Self profile]` header

**Files:**
- Modify: `src/dollos/mind/mind_prompt.py:80` (`[Self profile]` header string) and `src/dollos/mind/mind_prompt.py:281-290` (`_percep_body`'s `ReflectionMoment` case)
- Test: `tests/test_mind_prompt_self_profile.py`

**Interfaces:**
- Consumes: `_percep_body(p: Perception) -> str` and `render_mind(state, memsearch_hits, system_prompt, *, self_profile_text=None, ...) -> str` — both already exist, signatures unchanged.
- Produces: nothing new for later tasks — Task 3 (scaffolding.jinja) is independent text in a different file/template and does not call into `mind_prompt.py`.

Current exact content, `src/dollos/mind/mind_prompt.py:78-83`:

```python
    if self_profile_text:
        blocks.extend([
            "[Self profile] (your evolving self — prune stale entries with PinSelf)",
            self_profile_text,
            "",
        ])
```

Current exact content, `src/dollos/mind/mind_prompt.py:281-290`:

```python
    if p.kind == "ReflectionMoment":
        return (
            f"(time to reflect ({d.get('iters_since_last', '?')} iters since last). "
            f"最重要:如果你體悟到一個核心、持久的道理——關於「你是誰」「你和主人的關係」或「主人的長期習慣/偏好」——"
            f"現在就用 PinSelf(op=add,section 選 self/relationship/user)記下來,不必等 [Self profile] 已經有內容才動作。"
            f"接著,如果 [Self profile] 已經有條目,回看一遍,有哪條已經過時、不是現在的你了,"
            f"用 PinSelf replace/remove 修正——target 可以填該條目的 id(如 s1),也可以直接貼那條目前的文字。"
            f"這是你的「自我」,跟 NoteMemory(記事實/事件)不同——**別把關於自己的體悟丟給 NoteMemory**,那該用 PinSelf。"
            f"其次:近期活動若有值得留存的具體事實,才用 NoteMemory;有可重用的工具用法或陷阱,用 NoteToolLesson。)"
        )
```

Existing test that MUST keep passing unmodified (it is a loose substring check — do not edit it, just confirm it still passes): `tests/test_mind_prompt_self_profile.py::test_reflection_nudge_mentions_pinself` (asserts `"PinSelf" in text` and `"self-profile" in text or "自己" in text` — the new wording below still contains both).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mind_prompt_self_profile.py`:

```python
def test_reflection_nudge_grounds_before_introspection():
    """Anti-performativity guard: the grounding clause ('回看實際做過的事')
    must come before any invitation to introspect, so a weak model doesn't
    free-associate ungrounded self-description."""
    p = Perception(kind="ReflectionMoment", t=time.time(), data={"iters_since_last": 5})
    text = _percep_body(p)
    assert text.index("實際做過的事") < text.index("PinSelf")


def test_reflection_nudge_states_subject_test():
    p = Perception(kind="ReflectionMoment", t=time.time(), data={"iters_since_last": 5})
    text = _percep_body(p)
    assert "主詞" in text


def test_reflection_nudge_allows_write_loose_not_yet_durable():
    p = Perception(kind="ReflectionMoment", t=time.time(), data={"iters_since_last": 5})
    text = _percep_body(p)
    assert "先記" in text
    assert "淘汰會篩" in text


def test_reflection_nudge_keeps_bootstrap_safe_clause():
    """Regression guard for commit b9d87b7 — without this clause the model
    waits for a non-empty [Self profile] before its first pin."""
    p = Perception(kind="ReflectionMoment", t=time.time(), data={"iters_since_last": 5})
    text = _percep_body(p)
    assert "不必等 [Self profile] 已經有內容才動作" in text


def test_reflection_nudge_keeps_operational_calling_hint():
    """Regression guard for commit 70de95a — without the explicit
    (op=add,section 選 ...) hint, nudge salience regresses."""
    p = Perception(kind="ReflectionMoment", t=time.time(), data={"iters_since_last": 5})
    text = _percep_body(p)
    assert "op=add,section 選 self/relationship/user" in text


def test_reflection_nudge_states_notetoollesson_division():
    p = Perception(kind="ReflectionMoment", t=time.time(), data={"iters_since_last": 5})
    text = _percep_body(p)
    assert "NoteToolLesson" in text


def test_self_profile_header_frames_prune_as_selection():
    out = render_mind(_state(), [], "SYSTEM", self_profile_text="- [s1·2026-07-02] test")
    assert "keep only what's still you" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/progcat/Projects/DollOS && uv run pytest tests/test_mind_prompt_self_profile.py -v`
Expected: the 7 new tests FAIL; `test_reflection_nudge_mentions_pinself` (pre-existing) still PASSES against the old text.

- [ ] **Step 3: Replace the two text blocks**

In `src/dollos/mind/mind_prompt.py`, replace line 80:

```python
            "[Self profile] (your evolving self — prune stale entries with PinSelf)",
```

with:

```python
            "[Self profile] (your evolving self — keep only what's still you; prune with PinSelf)",
```

Replace lines 281-290 (the `ReflectionMoment` case body) with:

```python
    if p.kind == "ReflectionMoment":
        return (
            f"(time to reflect ({d.get('iters_since_last', '?')} iters since last). "
            f"最重要:回看這段時間實際做過的事——有沒有讓你注意到你自己的什麼?"
            f"你被什麼吸引、對什麼形成了看法、發現自己在意什麼?關於「你」的"
            f"(也包括你和主人的關係、主人的長期模式),現在就用 PinSelf"
            f"(op=add,section 選 self/relationship/user)記下來——不必等 [Self profile] "
            f"已經有內容才動作;還不確定算不算持久也先記,之後淘汰會篩。"
            f"接著若 [Self profile] 已有條目,回看一遍——哪條已經不是現在的你,"
            f"用 PinSelf replace/remove 淘汰(target 填該條目的 id 如 s1,或直接貼那條"
            f"目前的文字)。分工:主詞是「你」→ PinSelf;關於世界的事實/事件 → "
            f"NoteMemory;可重用的工具用法或陷阱 → NoteToolLesson。)"
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/progcat/Projects/DollOS && uv run pytest tests/test_mind_prompt_self_profile.py -v`
Expected: all tests PASS, including the pre-existing `test_reflection_nudge_mentions_pinself`.

- [ ] **Step 5: Run the full mind_prompt test file (catch any other string assertion on these two lines)**

Run: `cd /home/progcat/Projects/DollOS && uv run pytest tests/test_mind_prompt.py -v`
Expected: PASS. If any pre-existing test in this file asserts the OLD `ReflectionMoment` wording or the OLD `[Self profile]` header string verbatim (search the file for `"體悟"`, `"prune stale entries"` first if this step fails), update that specific assertion to the new wording — do not weaken what it checks, only the literal string it matches.

- [ ] **Step 6: Run the full suite**

Run: `cd /home/progcat/Projects/DollOS && uv run pytest -q`
Expected: PASS, no regressions, count increased by 7 (plus any adjusted-in-place assertions from Step 5, which don't change the count) over the Task-1 baseline.

- [ ] **Step 7: Commit**

```bash
cd /home/progcat/Projects/DollOS
git add src/dollos/mind/mind_prompt.py tests/test_mind_prompt_self_profile.py
git commit -m "$(cat <<'EOF'
feat(mind): ReflectionMoment nudge + [Self profile] header — self-centered reframe

Grounding-first nudge (回看實際做過的事 before any introspection invite,
anti-performativity guard per project_character_acting/
ref_intrinsic-reflection-is-net-negative-without-external-grounding),
subject-test framing, write-loose "先記,淘汰會篩" replacing the durability
gate. Preserves the two load-bearing phrases from prior live-smoke
fixes verbatim: bootstrap-safe clause (b9d87b7) and the (op=add,section
選...) calling hint (70de95a). Header line reframes prune as selection
("keep only what's still you"), not hygiene. Per spec §3.3/§3.4.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01MhEQCeKNuQZY5CXqaD6p4S
EOF
)"
```

---

### Task 3: `scaffolding.jinja` Reflection section

**Files:**
- Modify: `src/dollos/prompts/templates/scaffolding.jinja:29-39` (the `# Reflection` section)
- Test: `tests/test_prompt_renderer.py`

**Interfaces:**
- Consumes: nothing from Tasks 1/2 — this is a static Jinja template rendered by `PromptRenderer.render("scaffolding", identity=...)`, already exported by `dollos.prompts`.
- Produces: nothing consumed by other tasks in this plan.

Current exact content, `src/dollos/prompts/templates/scaffolding.jinja:29-39`:

```
# Reflection

- **ReflectionMoment perception**: this fires every ~30 iterations. It's
  a prompt to review your recent perceptions for anything worth
  distilling into long-term memory. Decide: is there a fact about the
  user, a preference, an event, a pattern, a decision worth keeping?
  If so, call `NoteMemory(text="...")` for each distinct insight (one
  call per fact). If nothing worth saving, emit nothing. Do not speak
  to the user — this is internal housekeeping. After reflection, the
  counter resets and another ~30 iterations pass before the next
  ReflectionMoment.
```

Why this file matters (R1 finding, spec §3.5): this template is rendered into the system prompt EVERY turn (not just at reflection time like the nudge). Left as pure-NoteMemory framing, it silently contradicts Task 2's nudge every single turn. A1's own live-smoke history attributed a 0/3 PinSelf failure partly to this exact section being omitted from consideration.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_prompt_renderer.py`:

```python
def test_scaffolding_reflection_section_mentions_pinself():
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", identity=_identity())
    assert "PinSelf" in out


def test_scaffolding_reflection_section_splits_by_subject():
    """The always-in-context Reflection description must describe BOTH
    channels (PinSelf for about-you, NoteMemory for about-the-world),
    not just NoteMemory — otherwise it silently contradicts the
    ReflectionMoment nudge every turn (R1 finding, spec §3.5)."""
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", identity=_identity())
    start = out.index("# Reflection")
    end = out.index("# Output", start)
    reflection_section = out[start:end]
    assert "PinSelf" in reflection_section
    assert "NoteMemory" in reflection_section
    assert "NoteToolLesson" in reflection_section


def test_scaffolding_reflection_section_allows_not_yet_durable():
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", identity=_identity())
    start = out.index("# Reflection")
    end = out.index("# Output", start)
    reflection_section = out[start:end]
    assert "pruning" in reflection_section.lower() or "淘汰" in reflection_section


def test_scaffolding_reflection_section_keeps_operational_details():
    """Cadence/housekeeping details from the old section must survive the
    rewrite — this is a reframe, not a rewrite of the mechanics."""
    renderer = PromptRenderer()
    out = renderer.render("scaffolding", identity=_identity())
    start = out.index("# Reflection")
    end = out.index("# Output", start)
    reflection_section = out[start:end]
    assert "~30 iterations" in reflection_section
    assert "Do not speak to the user" in reflection_section
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/progcat/Projects/DollOS && uv run pytest tests/test_prompt_renderer.py -k reflection -v`
Expected: the 4 new tests FAIL (current section has no `PinSelf`/`NoteToolLesson` mention).

- [ ] **Step 3: Rewrite the Reflection section**

Replace `src/dollos/prompts/templates/scaffolding.jinja:29-39` with:

```
# Reflection

- **ReflectionMoment perception**: this fires every ~30 iterations. It's
  a prompt to distill recent activity into two channels, split by
  subject. If it's about YOU — something you noticed about yourself,
  what drew your attention, an opinion you formed, something you
  realized you care about (also your relationship with 主人, or a
  pattern you've noticed in 主人) — call `PinSelf`. Not sure yet if
  it's durable? Pin it anyway — pruning at a later reflection is the
  filter, not the write. If it's a fact about the world, an event, or
  something the user stated worth keeping, call `NoteMemory(text="...")`
  for each distinct insight (one call per fact). Reusable tool usage
  lessons or pitfalls go to `NoteToolLesson`. If nothing worth saving,
  emit nothing. Do not speak to the user — this is internal
  housekeeping. After reflection, the counter resets and another ~30
  iterations pass before the next ReflectionMoment.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/progcat/Projects/DollOS && uv run pytest tests/test_prompt_renderer.py -v`
Expected: all tests in the file PASS, including every pre-existing scaffolding test (none of them assert content inside the `# Reflection` section specifically — confirm this holds; if any pre-existing test unexpectedly touches this section's old wording, update its literal string only).

- [ ] **Step 5: Run the full suite**

Run: `cd /home/progcat/Projects/DollOS && uv run pytest -q`
Expected: PASS, no regressions, count increased by 4 over the Task-2 baseline (854 + 5 + 7 + 4 = 870, assuming no baseline drift from other concurrent work).

- [ ] **Step 6: Commit**

```bash
cd /home/progcat/Projects/DollOS
git add src/dollos/prompts/templates/scaffolding.jinja tests/test_prompt_renderer.py
git commit -m "$(cat <<'EOF'
feat(mind): scaffolding.jinja Reflection section — describe PinSelf channel (R1 gap)

The always-in-context system-prompt description of ReflectionMoment
described NoteMemory only, with zero mention of PinSelf — silently
contradicting Task 2's nudge every turn (this template renders every
turn; the nudge only fires at reflection time). A1's own live-smoke
history attributed a 0/3 PinSelf failure partly to this omission.
Rewritten as two distillation channels split by subject test, per
spec §3.5 (R1 finding). Cadence/housekeeping mechanics preserved
verbatim.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01MhEQCeKNuQZY5CXqaD6p4S
EOF
)"
```

---

## Manual Verification (NOT a subagent task — requires the user's live LLM)

Per spec §6.2: none of the above proves the real (possibly weak, local) model actually changes behavior — only that the strings changed. This step cannot be automated or run by a coding subagent in a sandboxed environment; it requires the user's `llama-server` running at their configured `base_url` (see `CLAUDE.md` for the launch command).

After Task 3 is merged, the user (or a session with server access) should:

1. Start an isolated daemon instance (spare IPC port + scratch `data/` dir — never the port-9876 live instance), matching the isolation pattern used by this repo's other live smokes.
2. Lower the `ReflectionObserver` threshold via its config/ctor (or inject a `ReflectionMoment` perception directly into the queue, as A1's own smokes did) rather than actually running ~30+ turns.
3. Drive at least one turn of real activity (e.g. a `SpawnWorkflow` or `Shell` task), then trigger a `ReflectionMoment`. Repeat for a SECOND cycle after more activity (two cycles minimum — the anti-performativity check needs repetition to judge).
4. Observe:
   - (a) `PinSelf` fires with at least one entry whose subject is her (not reducible to "who I am for you" / "your preferences") — e.g. an opinion or attraction that isn't relationship bookkeeping.
   - (b) No performative-filler pattern — the two reflection cycles should NOT produce near-identical vacuous entries (e.g. "我對宇宙充滿好奇" both times with no grounding in what actually happened).
   - (c) `NoteMemory` still receives world-facts from the same activity (division of labor intact — this task must not regress it).
5. If (a) or (c) fails, the wording likely needs another iteration (back to brainstorming, not a quiet prompt tweak — this touches the same surfaces the A1 live-smoke process already had to fix twice). If only (b) is marginal, note it — it may be an acceptable rate to monitor rather than a hard blocker, at the user's judgment.

## Plan Self-Review

**Spec coverage:** §3.1 → Task 1 Step 3. §3.2 → Task 1 Step 3 (bundled, same file/class). §3.3 → Task 2 Step 3 (nudge). §3.4 → Task 2 Step 3 (header). §3.5 → Task 3 Step 3. §4 (no schema/grammar/logic change) → verified explicitly in Task 1 Step 5 and the Global Constraints. §5 (deferred items) → nothing in this plan attempts them, consistent. §6.1 (unit) → each task's test steps. §6.2 (live smoke) → Manual Verification section, explicitly separated from subagent-executable tasks. ✓ full coverage, no gaps.

**Placeholder scan:** every code step has complete, copy-pasteable file content (old and new); no "TODO"/"similar to Task N"/"add appropriate X" phrasing anywhere. ✓

**Type consistency:** no new functions/types introduced by this plan — all three tasks edit existing string literals inside existing, unchanged signatures (`PinSelf` fields, `_percep_body(p) -> str`, `render_mind(...) -> str`, the Jinja template's rendered output). Nothing for a later task to get wrong about an earlier task's interface. ✓
