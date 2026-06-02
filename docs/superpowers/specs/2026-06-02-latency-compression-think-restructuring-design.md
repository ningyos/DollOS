# Latency Compression — Think Restructuring (Reflex / Deliberate)

**Date**: 2026-06-02
**Status**: Design approved, pending implementation plan
**Scope**: DollOS-side. Voice-first grammar + one prompt line + cascade-log
parse. No inference-engine change, no model swap.

**Supersedes the approach in**: `2026-06-02-latency-compression-design.md`
(speculative decoding — abandoned, no vocab-compatible draft for Qwen3.6-35B-A3B).

## 1. Problem

User-perceived latency is dominated by an invisible think block that blocks
the first spoken word every turn. Baseline (live telemetry 2026-06-02,
`Qwen3.6-35B-A3B-UD-Q4_K_XL`, 2× RTX 4060 Ti): prompt ~4,500 tok, LLM TTFT
0.5–1.7 s, decode 40–53 tps.

In voice mode the tool-stream parser (`src/dollos/tool_parser.py`) starts in
state `IN_THINK` and **discards all `<think>` content** until `</think>`, only
then streaming spoken Say text. The voice-first grammar
(`build_voice_first_grammar`, `src/dollos/llm/templates.py:310`) forces **five
mandatory think lines every turn**:

```
<think>
SEEN: …
INTENT: …
REVIEW: …
MOOD: …
TOOL: …
</think>\n\n
(speak | tool-call)*
```

So the user waits `TTFT + full think (~150–300 tok @ 40 tps ≈ 4–7 s) + first
Say sentence` on **every** turn — including a bare "你好".

### Key insight
The think structure is uniform across turns, but its value is not. On a simple
conversational reply, `REVIEW` is vacuous (no prior attempt to review) and
`MOOD` is usually unchanged. Note: the MOOD and REVIEW *think lines are
log-only* — they are parsed solely by `_parse_think` for the cascade log and
drive no state; mood state is mutated only by the `MoodTool` call. The five
mandatory lines are a fixed latency tax that often buys nothing.

### Decision
Keep Qwen3.6 (user choice). Make **think depth vary by situation**, decided by
the model itself, realised as an in-grammar branch. This preserves Self-First
("how deeply to think" is itself a self behaviour) and changes no inference
infrastructure.

## 2. Architecture

A single grammar offers the model a two-way branch at the very first token of
the assistant turn (which already opens with a prefilled `<think>\n`). The
model picks depth; the grammar enforces the rest accordingly.

**Blast radius is minimal**: the voice parser already discards think until
`</think>` and then streams speech, so a reflex turn (tiny think) flows through
the existing parser unchanged. No parser state-machine change.

## 3. Grammar (`build_voice_first_grammar`)

```
root        ::= reflex | deliberate
reflex      ::= "REFLEX\n</think>\n\n" speak-only
deliberate  ::= think segments
think       ::= "SEEN: " line "INTENT: " line "REVIEW: " line "MOOD: " line "TOOL: " line "</think>\n\n"
line        ::= [^\n]+ "\n"
speak-only  ::= speak*
segments    ::= segment*
segment     ::= speak | tool-call
speak       ::= [^<]+
tool-call   ::= <existing per-tool rules>
```

- The model's first emitted token is either `REFLEX` (→ speak immediately) or
  `SEEN: ` (→ full think). `deliberate` is byte-for-byte the current grammar,
  so the deliberate path is a pure superset — no regression.
- **`reflex` is speak-only: no `tool-call`.** If a tool is needed, the model
  must take the deliberate branch (which plans it in the TOOL line). This keeps
  reflex a clean pure-conversation fast path.
- Zero speak segments remain permitted (silent finish), matching current
  behaviour.

## 4. Mood under reflex

Mood state is mutated **only** by the `MoodTool` call (`src/dollos/tools.py`),
never by the MOOD think line (which is log-only). A reflex turn is speak-only,
so it cannot call `MoodTool` and therefore inherits the current persistent mood
from `MindState` unchanged. Mood updates continue to happen on deliberate turns
(where the model can call `MoodTool`). Self-First emotional continuity is
preserved — mood simply isn't re-derived when Doll answers reflexively.

## 5. Prompt guidance (`src/dollos/mind/mind_prompt.py`)

The grammar exposes the REFLEX option but the model needs to know when to pick
it. Append one concise, description-style line to the system block (not a
behavioural command — consistent with Self-First prompt style):

> Before you speak, choose your depth: when the message is simple, purely
> conversational, and needs no planning or tool, answer with REFLEX
> immediately; when it needs thought, your mood shifts, or a tool is required,
> think fully (SEEN / INTENT / REVIEW / MOOD / TOOL).

No new prompt block; appended to the existing system description.

## 6. Cascade-log / parse impact (`src/dollos/cascade_log.py`)

`_parse_think` regexes for SEEN/INTENT/REVIEW/MOOD. On a reflex turn these
fields are absent, so it returns an empty dict — this must degrade gracefully
(it already tolerates missing fields). Add a `mode` field to the parsed result:
`"reflex"` when the think body is the REFLEX marker, `"deliberate"` otherwise,
so reflex-rate is analysable from the cascade log. No other cascade logic
changes.

## 7. Testing

- **Grammar** (`tests/` for templates): assert `root` yields both branches;
  reflex branch forbids `tool-call`; deliberate branch retains the exact
  five-line `think` structure.
- **Parser** (`tests/` for tool_parser): feed `REFLEX\n</think>\n\n你好！` →
  think discarded, `你好！` emitted as a SpeakChunk; feed a deliberate sample →
  unchanged behaviour.
- **Log** (`tests/test_cascade_log.py`): `_parse_think` on reflex text returns
  empty fields + `mode="reflex"` without raising; on deliberate text returns
  the fields + `mode="deliberate"`.
- **Regression**: existing deliberate-path tests pass unchanged.

## 8. Success criteria

- A bare greeting ("你好") takes the reflex branch; user-perceived TTFT drops
  from ~5–7 s (full think) to ~LLM TTFT + first sentence (~1–1.5 s).
- Emotionally charged, planning, or tool-requiring turns ("炸水壩?", "規劃今天")
  take the deliberate branch; behaviour matches today.
- Mood is still tracked across deliberate turns; persona is not eroded.
- **Eval**: a small labelled prompt set (reflex-able vs deliberate-needed)
  measures the reflex hit-rate to confirm the model neither over- nor
  under-uses either branch. Report the rate; if the model collapses to one
  branch, tune the §5 guidance before shipping.

## 9. Scope

**Touch**: `src/dollos/llm/templates.py` (grammar), `src/dollos/mind/mind_prompt.py`
(one guidance line), `src/dollos/cascade_log.py` (reflex parse + `mode`), and
their tests.

**Do not touch**: the cascade loop, the parser state machine, tool dispatch,
IPC, the model, or the inference engine.

**Explicitly out of scope**: `build_qwen3_think_tool_grammar`
(`src/dollos/llm/templates.py:268`) is used **only** by `subagent.py` for
ephemeral background subagents, which are not user-facing real-time and gain
nothing from reflex. It keeps its current full-think structure unchanged; do
not "consistency-fix" it. Only `build_voice_first_grammar` (the live mind_loop
voice/text path) gets the reflex branch.

## 10. Risks

- **Model over-uses reflex** (never thinks) → persona/quality erosion.
  Mitigation: §8 eval + §5 guidance tuning; deliberate remains the default for
  anything non-trivial.
- **Model under-uses reflex** (always thinks) → no latency gain. Same
  mitigation; worst case is status-quo latency, not a regression.
- **GBNF first-token branch ambiguity**: `REFLEX` vs `SEEN: ` share no prefix,
  so the branch is unambiguous to the sampler.
