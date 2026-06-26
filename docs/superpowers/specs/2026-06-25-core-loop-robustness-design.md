# Core-Loop Robustness — Design

**Date**: 2026-06-25
**Status**: Design draft, pending approval
**Scope**: DollOS daemon, Doll cognition / turn-loop layer only. No
inference-engine change, no model swap, no new external dependency.

**Source**: 2026-06-25 robustness review (20-agent SOTA survey + 6-slice code
audit + completeness critic). This spec covers ONLY the recommendations that
touch Doll's turn loop: **P1, P2, P5, P6, P10**. The agent/memory
execution-substrate recommendations (P3 episodic capture, P4 sleep-time
consolidation, P9 inspectable agents) are a SEPARATE track and are listed under
§OS Out of scope. P7/P8 are deferred (§DEF).

Every claim below was re-verified against the current on-disk source in this
worktree (codegraph + Read). Where the review's prose was wrong against the real
code, the correction is called out inline and summarised in §WRONG.

---

## 1. Goal

Make Doll's always-on turn loop **robust** as a distinct engineered property,
decoupled from model capability (a bigger LLM does not fix any of these). Five
concrete outcomes:

1. **P5** — Stop two silent-data-loss bugs that violate the project's
   no-silent-degradation rule and threaten Self-First continuity.
2. **P10** — Make the codebase an honest self-description: delete the dead
   two-tier event vocabulary and sync the `Perception.kind` Literal with what the
   kernel actually emits.
3. **P2-grammar** — Reorder the voice-first think grammar so the tool choice is
   decoded **before** REVIEW, turning REVIEW from pre-hoc justification into
   post-hoc self-critique (zero latency cost).
4. **P2-capture** — Stop discarding the structured metacognition: capture REVIEW
   from the think block into `MindState` and surface recent REVIEWs back into the
   prompt, so Doll has a real (cheap, non-looping) metacognitive feedback signal.
   A SELECTIVE verifier is specified but deliberately kept OFF the voice-latency
   path.
5. **P1** — Wire the live turn so a **sync inline tool's return value re-feeds
   into the same turn** (think → act → observe → decide), without breaking the
   streaming voice path.
6. **P6** — Now that the live turn can loop, give it a deterministic
   tool-existence guard (prerequisite) + a bounded-severity read-only safe-mode,
   so an unattended daemon degrades by an explicit, announced boundary rather than
   silently or by a magic count.

Non-goals: any weight update / fine-tune; any small-model reintroduction; any
fallback/degradation logic; any activation-space technique (the daemon talks to
llama.cpp over HTTP — no in-process residual access).

---

## 2. Background — the three dominant gaps (code-grounded)

The review found three findings dominate. All three are inside the turn loop, all
three are this track.

### Gap A — the live turn has no in-turn re-feed of SYNC tool results

`MindLoop._llm_iterate` (`src/dollos/mind/mind_loop.py:195-242`) is a single
streaming LLM pass. Each tool call is dispatched by `_dispatch_tool`
(`mind_loop.py:268-281`), and the tool's return value is **discarded** — line 279
is `await tool.run(self._ctx)` with no capture. So within one turn Doll cannot
`Recall` → read the hits → decide, nor react to a `NoteMemory` confirmation.

The shared re-feed primitive `run_tool_cascade`
(`src/dollos/cascade/tool_loop.py:97-221`) DOES loop results back, but codegraph
confirms it has exactly one caller — `SubagentRunner._run_cascade`
(`subagent.py:221`). Doll herself never calls it.

**Correction to the review (critic was right, sharpened):** this is NOT a blanket
"no cascade" bug. Async external tools (`Shell`, `SpawnSubagent`, `SpawnMonitor`)
are **fire-and-forget by design** — they return `None`, spawn a background worker,
and re-enter as `ToolResultArrived` / `MonitorFired` etc. perceptions across
turns. That design is correct and must stay. The true in-turn gap is only the
**sync inline tools that return a string**: `Recall.run` returns the formatted
hits (`tools.py`, `class Recall` — `return result`) and `NoteMemory.run` returns
`"memory noted: …"` (`tools.py:135-137`). These return values are produced and
then dropped. P1 closes exactly this.

**Correction to the review (streaming-vs-cascade):** the review's "route
`_llm_iterate` through `run_tool_cascade`" is not a drop-in. The two paths have a
real impedance mismatch, verified in the source:

| Aspect | Live `_llm_iterate` | `run_tool_cascade` |
|---|---|---|
| LLM API | `stream_completion(system, user, prefill)` via `_MindLLMAdapter` (`kernel.py:152-181`) — single prompt | `adapter.stream_messages(system, messages=[…])` — multi-message |
| Parser | `ToolStreamParser(voice_mode=True)` — emits `SpeakChunk` + `ToolCallReady`, strips `<think>` mid-stream | `ToolStreamParser()` (legacy) — **drops naked text**, returns tool-call dicts only |
| Speech | naked-text `speak` segments → `SentenceChunker` → live `sink` (TTS) | buffered into `assistant_buf`; **no sink, no chunker** |

There is no `Say` tool in `MAIN_TOOLS` — speech in the live turn is naked text
emitted between tool calls. So literally swapping in `run_tool_cascade` would
**silence Doll** (legacy parser drops the speech) and lose voice streaming. P1
must therefore add re-feed **on the streaming path**, not import the
non-streaming loop. See §P1.

### Gap B — structured metacognition is generated, then thrown away

The voice-first grammar (`templates.py:339-348`, `build_voice_first_grammar`)
forces a `SEEN / INTENT / REVIEW / MOOD / TOOL` think block every turn. In voice
mode `ToolStreamParser._feed_voice` discards the entire think block up to
`</think>` (`tool_parser.py:119-138`). REVIEW is parsed in exactly one place —
`cascade_log.py:29` `_parse_think` (used by `CascadeLogger.log_iter`) — and
**`CascadeLogger` has ZERO non-test callers** (codegraph; it is instantiated at
`kernel.py:200` but `log_iter` is never invoked at runtime). MOOD never updates
`state.mood` — only `MoodTool` writes it (the think MOOD line is advisory log
text).

**Correction to the review:** rec #1/#9's phrasing "wire `on_iter_end` to the
existing `CascadeLogger` (already used by the main path)" is FALSE — there is no
existing wiring to reuse. In the live path REVIEW is never even captured (the
parser discards it before any logging). P2-capture wires capture from scratch.

The deeper issue (critic's additional rec, verified): the grammar emits `REVIEW`
**before** the tool call is chosen (`think` rule precedes `segments`), so REVIEW
is pre-hoc justification of an already-forming impulse — "The Reasoning Trap"
shows this *increases* tool hallucination. P2-grammar fixes the ordering.

### Gap C — two silent-data-loss bugs (no-fallback violations)

1. **Blank-state-on-drift.** `load_state` (`mind_state.py:120-174`) reconstructs
   nested dataclasses with `Perception(**p)`, `ActiveTask(**t)`, etc. The
   top-level scalar fields use `data.get(...)` (field-tolerant), but the nested
   `**`-kwargs constructors are NOT: any **additive** schema field present in the
   on-disk JSON (a future field the running dataclass doesn't know, or vice
   versa) raises `TypeError`, which is caught at line 172-174 and returns a
   **blank `MindState()`** — total amnesia presented as a clean start, only a log
   line. For a companion whose selfhood is that one file this is catastrophic and
   directly violates the no-silent-degradation rule.

2. **WAL-truncate-after-failed-save.** `save_state` (`mind_state.py:114-117`)
   swallows write exceptions and returns `None` either way. `iterate()` calls it
   unconditionally (`mind_loop.py:153`) and then truncates the WAL
   (`mind_loop.py:158-164`) **regardless** of whether the save succeeded. The
   in-code comment ("After save_state succeeds…") is aspirational — there is no
   gate. On a failed save, the just-consumed perceptions are purged from BOTH the
   state file AND the WAL → permanently lost.

**The two bugs interact (critic's open question — now traced).** The WAL replay
path (`PerceptionWAL.iter_pending`, `src/dollos/wal/perception_log.py:230-242`,
reconstructs `Perception` directly from JSON — independent of `MindState` schema)
is the recovery mechanism. If save fails but truncation were correctly gated, the
perceptions stay in the WAL and replay on next boot recovers them. But two things
defeat that today: (a) truncation is ungated, so they're already gone; and (b)
even if they survived, blank-state-on-drift would load a blank state and discard
the replayed perceptions' effect. **So P5 must fix blank-state-on-drift FIRST,
then gate truncation.** Sequencing is load-bearing.

---

## 3. Constraints honoured

- **No fallback / no silent degradation.** P5 surfaces/halts instead of
  blank-resetting; P6 safe-mode is an explicit announced boundary (narrow to
  read-only + tell the user), which is the recommended unattended-agent pattern,
  NOT a banned fallback.
- **Single big LLM, HTTP transport.** No small model, no activation-space work.
  All verification/metacognition is prompt-level and API-portable.
- **Latency is the #1 concern.** P2-grammar is zero-cost (reorder only).
  P2-capture is parse-only (no extra decode). The verifier is SELECTIVE and
  explicitly NOT placed on the spoken-turn path (a clean-context verifier turn
  before `NoteMemory`, which often fires mid-speech, would double decode
  latency). Bulk verification belongs to the separate sleep-time track.
- **Fire-and-forget stays.** P1 does not touch async external tools.
- **Upstream as-is.** GBNF via llama.cpp sampler; no parser library changes.
- **TDD.** Every change ships with string-assertion grammar tests / pytest units
  first (see per-rec Test strategy and the plan).

---

## 4. P5 — Fix the two silent-data-loss bugs (do this FIRST)

### 4.1 Blank-state-on-drift → field-tolerant reconstruct + surface-not-blank

**File:** `src/dollos/mind/mind_state.py` (`load_state`, and small helpers).

Two distinct failure classes must be separated — today they collapse into one
silent reset:

- **Additive / tolerable drift** (extra or missing fields on an otherwise
  well-formed record): reconstruct field-tolerantly, do NOT lose the rest of the
  self. Implement per-dataclass tolerant builders that select only the keys the
  dataclass declares (e.g. filter `p` to `Perception`'s field names before
  `Perception(**filtered)`; default any missing optional). This makes an additive
  schema change (the exact regression the smoke gate guards, §4.3) a no-op for
  data preservation.

- **Genuine corruption** (malformed JSON, or a record that cannot be coerced even
  field-tolerantly): do NOT silently return a blank self. Per no-fallback,
  surface the boundary explicitly. Concretely: **quarantine** the unreadable
  state file (rename to `mind_state.json.corrupt-<ts>`) and **raise** a clear
  `MindStateLoadError` that halts boot with a message naming the quarantined
  path. Booting with a fresh blank self is allowed ONLY when the file genuinely
  does not exist (true cold start) — that path is unchanged.

Note the existing malformed-JSON branch (`mind_state.py:135-137`) currently
returns blank too; it moves under the same surface-not-blank policy. The "missing
file → fresh `MindState()`" branch (line 129-130) is the one legitimate
fresh-start and stays.

**Data flow:** `kernel.py` constructs `DollOS` and calls `load_state` at
`kernel.py:212`. A raised `MindStateLoadError` there must abort startup with a
non-zero exit and the quarantine message, not be swallowed. The kernel change is
to let the exception propagate (it currently doesn't guard it, so this is mostly
ensuring no new try/except hides it) and to log the quarantine path.

**Error handling:** the only swallow that remains is "file absent → cold start".
Everything else is either preserved (tolerant) or surfaced (quarantine + halt).

### 4.2 WAL-truncate-after-failed-save → gate truncation on a durable save

**Files:** `src/dollos/mind/mind_state.py` (`save_state`),
`src/dollos/mind/mind_loop.py` (`iterate`).

- `save_state` MUST report success: change its signature to **return `bool`**
  (`True` iff the temp file was written and atomically renamed). Keep the atomic
  temp-write + `replace` (`mind_state.py:108-117`); on exception, clean up the
  temp file (as today), log, and return `False`. Do not raise — the loop decides
  what to do with a failed save (it must NOT crash the daemon mid-turn, but it
  MUST NOT truncate).
- `iterate` (`mind_loop.py:151-164`): capture `saved = save_state(...)` and gate
  the WAL truncation block on `saved is True`. If `saved is False`, **skip
  truncation** so the perceptions remain in the WAL and are replayed (and
  re-attempted) on next boot. Log a clear warning that the save failed and
  truncation was skipped.

**Data flow after fix:** failed save → WAL retains seqs → next boot replays them
via `iter_pending` (unaffected by `MindState` schema, §Gap C) → with P5.1's
tolerant load the replay now lands in a real (non-blank) state. The two fixes
together close the hole.

### 4.3 CI smoke gate (machine-enforce no-silent-degradation)

**File:** `tests/test_mind_state_durability.py` (new), runnable in the normal
pytest suite (no LLM, no network).

Three assertions, derived directly from the critic's additional rec:

1. **Additive-schema round-trip without data loss.** Write a `mind_state.json`
   that contains an EXTRA top-level key and an extra key inside a nested record
   (simulating a future additive field), plus real populated fields
   (mood/scratchpad/open_loops/recent_perceptions). Assert `load_state` returns a
   state whose known fields are fully preserved (NOT a blank `MindState()`), and
   that no exception escaped.
2. **Failed save does NOT truncate the WAL.** Drive `iterate()` with a
   `save_state` forced to fail (monkeypatch to return `False` / raise on write,
   e.g. unwritable path), having pre-populated the WAL via `queue.put`. Assert
   `wal.iter_pending()` is **non-empty** after the iteration (perceptions
   survived), inverting the green-path assertion in the crash-recovery plan
   (`tests/test_mind_loop.py::test_iterate_truncates_wal_after_state_save`).
3. **Corruption surfaces, never blanks.** A genuinely malformed state file makes
   `load_state` raise `MindStateLoadError` AND leaves a `*.corrupt-*` quarantine
   file — proving the surface-not-blank policy.

---

## 5. P10 — Delete the dead event vocabulary + sync the Perception.kind Literal

**Files:** `src/dollos/events.py`, `src/dollos/mind/mind_state.py`,
(`src/dollos/kernel.py` only if any stray import needs removing).

Verified: `src/dollos/events.py` defines `RawEvent` / `DollEvent` /
`UserTextEvent` / … with rich "two-tier event model" docstrings, and codegraph
finds **zero non-test callers** for `RawEvent` and `DollEvent`. The live system
uses the single `Perception` dataclass. The docstrings actively misdescribe the
architecture — a self-model hazard for a self-inspecting agent.

- **Delete `src/dollos/events.py` entirely.** First sweep for any import (`grep
  -rn "from dollos.events\|import events" src tests`) and remove dead imports. If
  a test imports it, delete/adjust that test (it is testing dead code). Do NOT
  keep "thin aliases" — there is nothing live to alias.
- **Sync the Literal.** `Perception.kind` (`mind_state.py:52-55`) lists
  `UserSpoke, ToolResultArrived, MonitorFired, MonitorEnded, ScheduledMoment,
  Awoke, ReflectionMoment` but the kernel emits `Perception(kind="Interrupted")`
  at `kernel.py:392`. Add `"Interrupted"` to the Literal. (The crash-recovery
  plan already flagged this at its §Known-limitations line 824 as a future
  cleanup — this is that cleanup.) While here, audit every `kind=` produced in
  `src/` against the Literal and add any other missing member so the documented
  taxonomy matches reality before any future move to pydantic validation, which
  would start rejecting live perceptions.

**Test strategy:** a tiny test asserting the kernel's interrupt path produces a
`kind` that is a member of the Literal's allowed set (string-membership check —
the dataclass does not enforce Literal at runtime, so the test is the
enforcement). Plus the suite must stay green after `events.py` deletion (proves
nothing depended on it).

This is pure hygiene (low impact) but cheap and safe — sequenced second so the
later, riskier changes land on an honest codebase.

---

## 6. P2 — Metacognition: grammar reorder + capture + selective verifier

Split into three independently-landable pieces, cheapest-and-safest first.

### 6.1 P2-grammar — REORDER think so TOOL precedes REVIEW (zero cost)

**File:** `src/dollos/llm/templates.py` (`build_voice_first_grammar`).

Today (`templates.py:340-348`):

```
root ::= think segments
think ::= "SEEN: " line "INTENT: " line "REVIEW: " line "MOOD: " line "TOOL: " line "</think>\n\n"
```

REVIEW is decoded before any tool call (`segments`) exists → pre-hoc
rationalisation. Reorder so the model commits SEEN/INTENT and the TOOL intent
first, then REVIEWs that committed choice, then MOOD:

```
root ::= think segments
think ::= "SEEN: " line "INTENT: " line "TOOL: " line "REVIEW: " line "MOOD: " line "</think>\n\n"
```

REVIEW now reads as "I have chosen X; does X match my goal/world-state?" —
post-hoc self-critique. This is the cheapest, highest-leverage metacognition fix:
a single grammar-string reorder, no new subsystem, **no extra decode, no latency
cost**. The five lines and the `</think>\n\n` terminator are unchanged in count
and shape, so the voice parser (which discards everything up to `</think>`) is
unaffected.

**Interaction with the held think-restructuring branch.** The branch at
`docs/superpowers/specs/2026-06-02-latency-compression-think-restructuring-design.md`
modifies the SAME function (adds a `reflex | deliberate` branch). Per the task
brief it is treated as **SUPERSEDED** for the purpose of this work: design the
reorder against the CURRENT main grammar. If that branch ever lands, the
`deliberate` arm must adopt this TOOL-before-REVIEW order — but this spec does not
depend on it and does not add the reflex branch.

**Out of scope here:** `build_qwen3_think_tool_grammar` (`templates.py:268-307`,
subagent-only) keeps its own order; do not "consistency-fix" it (it is
non-user-facing and out of this track's loop). It already happens to constrain
`TOOL:` to a tool-name, a separate concern.

**Test strategy (TDD, string-assertion — house style):** in the templates test,
assert the generated grammar string contains the substring `"INTENT: " line
"TOOL: " line "REVIEW: "` (TOOL before REVIEW) and does NOT contain the old
`"REVIEW: " line "MOOD: " line "TOOL: "`. Assert the `segments`/`speak`/
`tool-call` tail and `_JSON_STR_RULES` are byte-identical to before (no collateral
change). Existing voice-parser tests must pass unchanged (think still discarded up
to `</think>`).

### 6.2 P2-capture — parse REVIEW into MindState, surface recent REVIEWs

The think block is discarded by the streaming voice parser BEFORE it reaches any
consumer, so capture must happen where the full assistant emit is available. The
clean seam is to have `_llm_iterate` accumulate the raw assistant text it is
already streaming (a `list[str]` buffer, exactly as `run_tool_cascade` does at
`tool_loop.py:139,150`) and, at end-of-stream, run the existing
`cascade_log._parse_think` over it to extract `seen/intent/review/mood`.

**Files:** `src/dollos/mind/mind_loop.py` (accumulate + parse + persist),
`src/dollos/mind/mind_state.py` (new field),
`src/dollos/mind/mind_prompt.py` (surface), reuse `cascade_log._parse_think`.

**MindState addition** (`mind_state.py`):

- `recent_reviews: deque[str]` (small `maxlen`, e.g. 5) — the last N post-hoc
  REVIEW lines. Append-only ring buffer; serialised like the other deques in
  `save_state`/`load_state` (and covered by P5.1 tolerant load — a NEW deque
  field is exactly the additive-drift case the smoke gate asserts is safe).

**MOOD → state.mood: deliberately NOT auto-written here.** Today only `MoodTool`
writes mood. Updating `state.mood` directly from a free-text MOOD think line is
risky (no validation; it competes with `MoodTool`). **Decision: do NOT
auto-overwrite `state.mood` from the MOOD think line in this track.** Capturing
MOOD as part of the parsed think (for the cascade log, §6.4) is enough; making
MOOD authoritative is a persona-track concern (P8, deferred) where a
self-report-vs-behaviour consistency check is designed. This avoids
re-introducing the exact "model mis-reports mood with no external check" hazard
the review flagged. (This is a deliberate narrowing of the review's "set
state.mood from MOOD".)

**Surface back into the prompt** (`mind_prompt.py`): render a short `[Recent
self-review]` block (the `recent_reviews` entries, oldest→newest) into the mind
prompt, so the next turn's reflection has real material ("I keep making the same
mistake" becomes visible). Keep it terse (it is on the prompt-token budget that
drives latency); cap at N and truncate each line. Place it adjacent to the
existing self-state blocks, not inside `[Memory context]`.

**Data flow:** stream → accumulate raw assistant text → on stream end
`_parse_think(raw)` → if `review` present, `recent_reviews.append(review)` →
`save_state` persists it → next `render_mind` includes `[Recent self-review]`.

**Error handling:** `_parse_think` already tolerates missing fields (returns a
partial/empty dict). A turn with no REVIEW simply appends nothing. No raise.

**Test strategy:** unit on `mind_loop` — feed a scripted stream whose think block
carries a known `REVIEW:` line; assert it lands in `state.recent_reviews` and is
bounded by `maxlen`. Unit on `mind_prompt` — given a state with N reviews, assert
the rendered prompt contains a `[Recent self-review]` block with those lines and
respects the cap. Round-trip test: a state with `recent_reviews` survives
`save_state`→`load_state` (ties into P5.1).

### 6.3 P2-verifier — SELECTIVE, and explicitly OFF the voice path

**Decision (honouring the critic + latency rule): do NOT add an inline
clean-context verifier turn in this track.** A verifier turn before
`NoteMemory`/`Shell` doubles decode latency, and `NoteMemory` frequently fires
DURING a spoken turn — the tax lands squarely on the project's #1 concern. The
review itself flagged this for `Say`/`Recall`; the critic extended it to
`NoteMemory`. Bulk verify-before-write belongs in the SEPARATE sleep-time track
(out of scope here).

What this track DOES specify is the **design intent + seam** so the sleep-time
track can attach a verifier without re-architecture, and a precise definition of
"high-stakes" for when it is built:

- High-stakes / irreversible set = side-effecting `Shell`, `SpawnSubagent`, and
  durable-self `NoteMemory`. `Say`(naked speech)/`Recall`/scratchpad/focus/mood
  remain unverified (reversible, latency-sensitive, spontaneity-preserving).
- The verifier, when built, runs in a CLEAN context that sees the proposed action
  WITHOUT the original reasoning and emits a typed verdict (SAVeR-style:
  `Contradiction` / `Unjustified_Inference` / `Invalid_Precondition`). It is a
  separate-context judge (GenRM / Cross-Context Review), never a same-context
  "reflect harder" loop — the project's own Inner-Voice A/B and the SOTA both
  show intrinsic re-grading is net-negative.

This subsection is a documented design intent + a non-blocking seam, NOT an
implementation task in this plan. It is listed here so it is not silently
dropped.

### 6.4 (Optional, low-risk) actually call CascadeLogger

Because P2-capture already accumulates the raw assistant text and runs
`_parse_think`, the live loop is one call away from finally exercising the
already-instantiated `CascadeLogger` (`kernel.py:200`). Wiring `log_iter` from
`_llm_iterate` (per-pass: assistant_text + dispatched tool calls + results +
duration) gives the live turn real observability for the first time. This is
genuinely optional and gated on not adding latency (it is a local JSONL write via
structlog, already used elsewhere). If included, it is one task in the plan; if it
risks scope, it is dropped without affecting P2-capture.

---

## 7. P1 — In-turn re-feed of sync tool results on the streaming path

### 7.1 The decision (resolving streaming-vs-cascade)

**Do NOT import `run_tool_cascade` into the live loop.** As shown in §Gap A, it
uses the non-streaming `stream_messages` API + the legacy parser that drops naked
text → it would silence Doll and lose voice streaming.

**Instead: turn `_llm_iterate` into a streaming cascade in place.** Keep the
existing voice machinery (`ToolStreamParser(voice_mode=True)` + `SentenceChunker`
+ live `sink`) exactly as-is for each pass, and wrap it in an outer loop that
re-feeds sync-tool results:

```
turn:
  messages = [ {user: prompt} ]
  loop:
    stream one assistant pass (voice parser -> speech to sink; tool calls dispatched inline)
      - capture each sync tool's return value (Recall/NoteMemory -> str)
      - async tools (Shell/Subagent/Monitor) dispatch & return None as today (no capture)
    append the raw assistant emit as an assistant message
    if no sync-tool results this pass -> break        # nothing new to observe
    append one  user <tool_response>...  message per result
    continue   # next pass sees the results, can decide
```

The inner pass remains byte-for-byte the current streaming voice path, so:
- speech still streams sentence-by-sentence to TTS within each pass (no regression
  to TTFT / first-word latency);
- think is still stripped mid-stream;
- async fire-and-forget tools are untouched (they return `None`, do not produce a
  `tool_response`, do not extend the turn — they re-enter across turns as today).

The only new behaviour: when Doll calls a SYNC tool that returns a string, its
output is appended as a `<tool_response>` user message and a further streaming
pass is run, so she can read-then-decide in one turn. A turn with no sync-tool
result is exactly today's single pass.

**Scoping note (finding 2, post-review):** in-turn re-feed on SUCCESS is scoped
to `Recall` only (allowlist `IN_TURN_REFEED_TOOLS = {"Recall"}`). `NoteMemory`
success is deliberately EXCLUDED: it returns a `"memory noted: …"` confirmation
Doll does not need to read, so re-feeding it cost an extra full decode pass on
the project's #1-concern latency path. Tool FAILURES of ANY tool still re-feed
(external grounding so Doll can fix her mistake) — the allowlist gates SUCCESS
only.

### 7.2 What changes in code

**Files:** `src/dollos/mind/mind_loop.py` (the loop + capture),
`src/dollos/mind/mind_ctx.py` (confirm `MindCtx` already carries everything a tool
pass needs — it does; no shape change expected),
`src/dollos/kernel.py` (give `MindLoop` access to a messages-capable render).

1. **Capture sync results.** `_dispatch_tool` must return the tool's result
   instead of discarding it. Reuse the existing `ToolResult` semantics from
   `tool_loop.py:33-94` (success/detail; `None` return = side-effect tool, no
   cascade) rather than inventing a parallel type — i.e. `_dispatch_tool` returns
   `ToolResult | None`, and `_handle_stream_event` collects non-`None` results
   into a per-pass list. This keeps one definition of "cascade-worthy" across the
   codebase.

2. **Multi-pass prompt.** Each re-feed pass needs the prior assistant emit + the
   `<tool_response>` appended. The live loop currently renders a single `user`
   prompt and calls `stream_completion(user=prompt)`. For pass ≥ 2 it must send
   the running message list. `Qwen3ThinkingTemplate.render_messages`
   (`templates.py:380-407`) already produces exactly the
   `user → assistant(think+tool_call) → user(<tool_response>) → assistant`
   alternation, and `_MindLLMAdapter` currently only exposes `stream_completion`.
   **Decision:** widen `_MindLLMAdapter` (or pass the full `LLMAdapter` to
   `MindLoop`) so the loop can call `stream_messages` for pass ≥ 2 while pass 1
   stays on the existing `stream_completion`(prompt) path. Pass 1 unchanged keeps
   the first-word latency identical to today. (Implementation may unify both
   passes onto `stream_messages` IF a templates test proves the rendered pass-1
   prompt is equivalent to today's `render`; otherwise keep the two-API split —
   either is acceptable, the plan picks one and tests it.)

3. **Cancellation preserved.** The `CascadeCtx` cancel checks
   (`mind_loop.py:218-238`) must wrap the OUTER loop too: check
   `_cascade_ctx.cancelled` at each pass boundary and before each re-feed, so
   `cancel_current_cascade()` (used by the interrupt path, `kernel.py:379`) still
   returns within ~one chunk window. `self._cascade_ctx` lifetime extends across
   the whole multi-pass turn (set once at turn start, cleared in `finally`).

4. **Termination.** This loop terminates naturally: a pass with no sync-tool
   result breaks. But a model could, in principle, keep emitting sync tools
   forever — so P6 (next) supplies the deterministic guards (existence guard +
   3-strike + budget cap + safe-mode). **P1 must not ship without P6's
   tool-existence guard and a budget cap** (see §SEQ sequencing): an uncapped live
   cascade is the prerequisite hazard the critic named.

### 7.3 Error handling

- Sync tool raises → caught as today (`mind_loop.py:280-281`), turned into a
  `ToolResult(success=False, detail=…)` so the error re-feeds as a
  `<tool_response>` (external grounding, CRITIC-style) instead of vanishing.
- Async tool dispatch unchanged (its own failure paths already re-enter as
  perceptions).
- Stream cancelled mid-pass → existing clean-exit logic; the outer loop sees
  `cancelled` and stops without truncating speech.

### 7.4 Test strategy

- Unit: scripted stream where pass 1 emits a `Recall` tool call; assert the loop
  runs a SECOND pass whose input message list contains a `<tool_response>`
  carrying the Recall result, and that speech from both passes reached the sink in
  order.
- Unit: a turn with only a fire-and-forget `Shell` call runs exactly ONE pass (no
  `<tool_response>`, no second decode) — proving async tools don't extend the
  turn.
- Unit: `cancel_current_cascade()` during pass 2 exits cleanly within one chunk.
- Regression: a pure-speech turn (no tools) is a single pass identical to today.

---

## 8. P6 — Tool-existence guard (prerequisite) + safe-mode + bounded loop

### 8.1 Post-decode tool-existence guard (prerequisite for P1, build with/before it)

GBNF guarantees shape, not semantics: a grammar-valid call to a stale/renamed tool
passes structural checks. Today `_dispatch_tool` already logs-and-returns on an
unknown tool name (`mind_loop.py:269-272`) and `dispatch_tool_call` returns a
`ToolResult(success=False, detail="unknown tool")` (`tool_loop.py:73-75`). The gap
once P1 adds a real cascade: an unknown/invalid call must **re-enter as an error
`<tool_response>`** (external grounding) rather than a silent no-op, so the model
is told and can correct — and a *rotating set* of invalid calls must still
terminate (the existing 3-strike counter only catches the SAME tool repeated).

- Validate tool **name** against the live registry AND **args** against the
  pydantic model post-decode, BEFORE side-effects, producing a typed failed
  `ToolResult` that the §P1 re-feed loop sends back as `<tool_response>`. (Name +
  `model_validate` already exist in `dispatch_tool_call`; the live `_dispatch_tool`
  must adopt the same return-the-failure behaviour instead of silently returning.)
- This guard is a **hard prerequisite** for P1: ship it in the same plan, before
  or with the cascade wiring, never after.

### 8.2 Bounded loop — deterministic guards as the PRIMARY cap

**Correction to the review:** rec #6 lists an embedding-trajectory loop detector
(arXiv:2512.10350) as the *primary* termination mechanism. The critic (correct)
notes that paper is single-author/emerging ("pilot before trusting"), and
embedding every cascade step adds an embedding RTT per pass to the latency path —
NOT "nearly free" on a voice daemon. So:

- **Primary guard = deterministic, already proven.** Port the cascade's existing
  same-tool 3-strike abort (`tool_loop.py:189-219`) into the §P1 live loop, and
  add a **pass-budget cap** (a max sync-tool re-feed count per turn, generous —
  e.g. 8 — so legitimate multi-step plans run, but a rotating-invalid-call storm
  terminates). These are convergence criteria, not fallbacks.
- **Embedding-trajectory detection = explicitly deferred / speculative.** Note it
  as a future option behind a flag; do NOT make it the cap and do NOT put an
  embedding call on every pass. (Listed in §DEF Deferred too.)

### 8.3 Read-only safe-mode (bounded-severity, announced — NOT a fallback)

**Files:** `src/dollos/mind/mind_loop.py`, `src/dollos/mind/mind_state.py` (a
`safe_mode: bool` flag + reason), `src/dollos/mind/mind_prompt.py` (announce it),
`src/dollos/kernel.py` (enqueue the help perception).

Trigger: after **K consecutive tool failures** within the live loop OR the
3-strike stuck flag. Behaviour:

- Restrict the available tool set to read-only / reversible only — `Recall`,
  scratchpad reads, plus naked-text speech. Exclude `Shell`-writes,
  `SpawnSubagent`, `SpawnMonitor`, `NoteMemory`. Mechanically: build the pass's
  grammar/registry from a reduced tool list while `safe_mode` is set.
- Enqueue a `Perception` that tells Doll she has narrowed to read-only and asks
  the user for help, and render a one-line `[Safe mode]` banner in the prompt so
  the state is **visible every turn** (not edge-triggered — the review noted
  proprioception is edge-triggered only at `system_pulse.py:389`; safe-mode must
  persist its banner until cleared).
- **Exit:** a successful user turn / explicit clear resets `safe_mode`. This is an
  explicit, announced, bounded-severity boundary — the recommended
  unattended-agent pattern, and consistent with no-fallback (we surface and narrow
  loudly; we do not silently "degrade and pretend").

### 8.4 Test strategy

- Unit: a pass returning an unknown-tool call produces a failed `ToolResult` re-fed
  as `<tool_response>` (not a silent no-op).
- Unit: a rotating set of distinct invalid calls hits the pass-budget cap and
  terminates the turn (proving the budget catches what the same-tool 3-strike does
  not).
- Unit: K consecutive failures set `state.safe_mode`, the next pass's tool registry
  excludes the write tools, and a `[Safe mode]` banner renders; a help-perception
  is enqueued.
- Unit: a user turn clears `safe_mode`.

---

## 9. <a id="SEQ"></a>Sequencing & risk

Strict order — safest-first, each lands and stays green before the next:

1. **P5** (data-loss bugs) — highest impact, smallest surface, no behaviour change
   for Doll. Fix blank-state-on-drift BEFORE WAL truncation gating (they interact,
   §Gap C). Ship the durability smoke gate with it.
2. **P10** (hygiene) — delete `events.py`, sync the Literal. Pure cleanup on a
   now-safe state layer; lands the codebase as an honest self-description before
   riskier work.
3. **P2-grammar** (reorder) — zero-cost grammar change, string-assertion tested, no
   parser/loop impact.
4. **P2-capture** (REVIEW into state + surface) — additive `MindState` field (safe
   under P5.1), parse-only, no decode added. Optional CascadeLogger-wiring tucked
   here.
5. **P1** (in-turn sync re-feed) — the structural change, on the streaming path,
   shipped TOGETHER WITH P6.1 (existence guard) + a budget cap so the new live
   cascade can never run unbounded.
6. **P6** (safe-mode + deterministic bounds) — completes the robustness envelope
   around the now-real cascade.

**Risks & mitigations:**

- *P1 voice-latency regression.* Mitigation: pass 1 keeps the existing
  single-prompt streaming path verbatim; re-feed passes only occur after a
  sync-tool call (which the user already waited on). Regression test asserts a
  pure-speech turn is a single pass.
- *P1 unbounded live cascade.* Mitigation: P6.1 existence guard + 3-strike +
  pass-budget cap shipped in the same plan; P1 task explicitly depends on them.
- *P5 over-tolerant load hides real corruption.* Mitigation: tolerant path only
  filters/defaults fields; anything that cannot coerce raises + quarantines
  (surface-not-blank), asserted by the corruption smoke.
- *P2-grammar interaction with the held think-restructuring branch.* Mitigation:
  that branch is treated as superseded; reorder is designed on current main and is
  self-contained.
- *P10 deleting a file other code imports.* Mitigation: grep sweep + green-suite
  gate before delete.

---

## 10. <a id="WRONG"></a>Where the review was WRONG against the real code (for human sanity-check)

1. **"Reuse the existing CascadeLogger wiring" (rec #1/#9).** FALSE — `CascadeLogger`
   has zero non-test callers (instantiated at `kernel.py:200`, never invoked).
   Nothing to reuse; P2.4 wires it from scratch (and only optionally).
2. **"Route `_llm_iterate` through `run_tool_cascade`" is a drop-in (rec #1).**
   FALSE — `run_tool_cascade` uses `stream_messages` + the legacy
   `ToolStreamParser()` that DROPS naked text, with no `SentenceChunker`/sink. In
   the live voice path that would silence Doll. P1 instead makes the streaming path
   itself a cascade (§P1).
3. **"No in-turn cascade at all" overstates the gap (rec #1).** Async external
   tools (Shell/Subagent/Monitor) re-enter as perceptions across turns BY DESIGN
   and are correct. The real gap is ONLY sync inline tools that return strings
   (`Recall`, `NoteMemory`). P1 is scoped exactly to those.
4. **"Set state.mood from the MOOD think line" (rec #2).** Narrowed: doing so
   re-introduces an unvalidated self-report as authoritative mood — the exact
   hazard the review flags elsewhere. This track captures the think block but does
   NOT make MOOD authoritative; that belongs to the persona track (P8) with a
   consistency check. `MoodTool` stays the sole authoritative writer.
5. **Embedding-trajectory detector as the PRIMARY cap (rec #6).** Rejected as
   primary — emerging single-author result, and an embedding RTT per pass taxes
   voice latency. Deterministic 3-strike + pass-budget cap is primary; dynamics
   detection is deferred/speculative.
6. **save_state "swallows" but the comment claims gated truncation.** Confirmed:
   the `mind_loop.py:156-157` comment ("After save_state succeeds…") describes a
   gate that does not exist in code; truncation is unconditional. P5.2 makes the
   comment true.

---

## 11. <a id="DEF"></a>Deferred — needs its own design pass

These are real and in the broader robustness story but have open design questions
and/or overlap another track. Listed so they are not silently dropped; NOT
specified here.

- **P7 — persistent goal / open-loop actuation + a when-to-act ("Observe") gate.**
  `open_loops`/`pending_events` exist in `MindState` but are render-only
  (`mind_loop.py:106-107` TODOs; `_derive_memory_hits` stringifies a dict for
  non-`UserSpoke` turns, `mind_loop.py:166-183`). The cheap independently-valuable
  slice the critic identified (drive `_derive_memory_hits` from `open_loops` on
  reflection/scheduled turns) is attractive but still needs its own brief; the full
  rec (self-wake `PendingEvent` re-entry, drain-and-inject at turn boundaries, an
  explicit Observe/Stay-silent grammar option, success-predicate + budget
  convergence guards) is an L-effort plan that DEPENDS on P1 existing first.
  Deferred to its own design pass.

- **P8 — persona hardening.** Value-reasons constitution (pack change), guaranteed
  identity/relations retrieval slots in `[Memory context]`, IDENTITY_HASH drift
  baseline + persona-stability smoke test, and using the captured think block
  (§6.2) as a self-report-vs-behaviour consistency check. Activation-space
  techniques are ruled out (HTTP transport). Overlaps the persona/pack track; needs
  its own design pass. The MOOD-authoritative question (§WRONG.4) resolves here.

---

## 12. <a id="OS"></a>Out of scope (separate substrate / memory-as-service track)

Explicitly NOT this track — they are the agent/memory execution substrate, with
their own dependencies (containers/k3s/memory-as-service/sleep-time), and must not
be folded in:

- **P3 — episodic capture** (resurrect `append_transcript` from the live loop;
  provenance-tag writes). `append_transcript` (`src/dollos/memory_writer.py:22-47`)
  is verified dead (test-only callers) and the `transcripts/` root is registered
  but unwritten. This is the root memory blocker but it is the memory track's, not
  the turn loop's. **Dependency note:** P2-capture's `recent_reviews` and any
  future playbook want episodic capture to be durable beyond the in-memory deque —
  flag the coupling, do not build it here.
- **P4 — sleep-time consolidation pass** (quiet-pulse → memory-keeper subagent →
  append-only strategy playbook + `memsearch.compact()` + non-destructive conflict
  resolution). The SELECTIVE verifier (§6.3) is designed to live HERE, not inline.
  Separate track.
- **P9 — inspectable subagents / Shell result correlation / sandboxed
  subagent-spawned side-effects.** Subagent observability + the `task_id`
  correlation fix + monitor provenance. Execution-substrate concern; separate
  track.
