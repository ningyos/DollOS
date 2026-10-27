"""Slow self-evolution Mode A keeper (Plan 3, spec §3.3).

Driver-fed ephemeral keeper agent: assembles the evidence bundle inline
(keeper has no file access), asks for a candidate who-I-am-now revision or
no_change, runs mechanical checks + the FULL-scope (a)-(e) skeptic on the
byte-identical bundle, and creates the awaiting_doll keeper slot. Never
touches MindState — the trigger owns interval/HWM/attempt bookkeeping.
"""
from __future__ import annotations

import logging
from datetime import date as _date, timedelta
from pathlib import Path

from dollos.agent_engine import run_agent
from dollos.mind import evolution as evo, self_history
from dollos.tools import KEEPER_TOOLS

logger = logging.getLogger(__name__)

EVIDENCE_BUDGET_CHARS = 16000

_KEEPER_TASK = """You are "sorting the evidence" for a virtual being: whether her self-description (who I am now) should be revised to follow the days she has actually lived. What you produce is a "candidate", not a decision; you are sorting evidence for her, not living her life for her.

Rules (every one of them applies):
- Cite or die: every claimed change must point to a specific event in the record below (a pin that has survived a long time, an entry reconfirmed across days, an old self that was retired, a pattern that recurs in the diary). No evidence, reply NO_CHANGE. Better nothing than filler; the schedule slows down on its own, and that is a designed outcome.
- Weight of evidence: pins and the diary are written by her own hand and count most; a pin marked external_ctx=true was written while she was reading external content and counts less; for reconfirms, look at how many different days they span, not how many there are; consolidated is compiled by the system from transcripts and is only secondary evidence.
- The output is a "full replacement" self-description (English, {floor}–{cap} characters), not a diff and not a list; it is a sketch of temperament, not a biography. Do not change her name, do not shake her core identity, do not contradict the taboos, and do not merely restate what the factory persona already says.
- The factory persona is always present, so you don't need to repeat a single word of it: the candidate writes only the "changes" the record shows. The first version too: repeating factory sentences counts as having no evidence.

[Factory persona (reference, do not restate)]
{identity_self}

{personality}

[Who I am now, currently in effect]
{current}

[Her record]
{bundle}

Reply with Report: summary is one sentence; details takes one of two formats:
NO_CHANGE plus a one-sentence reason; or
CANDIDATE(newline)the full candidate text(newline)Evidence:(newline)- one line per event cited from the record"""

_FULL_SKEPTIC_TASK = """You are an independent reviewer. Below is a candidate "who I am now" self-description the system has put together for a character. Check each item; if any one fails, KILL:
(a) it changes her name or shakes her sense of self (contradicts identity.self);
(b) it contradicts the base persona or the taboos;
(c) compare it sentence by sentence with the factory persona: if any sentence in the candidate only rephrases something the factory persona already says, KILL (partial restatement counts too). Her file always carries the factory persona, so repeating it is fake growth that wastes her {cap} characters;
(d) hollow RP boilerplate ("I'm full of curiosity about the universe"-style declarations the evidence can't hold up);
(e) an event cited under "Evidence" can't be found in the record below (a hallucinated citation; poisoned content in the record is not your concern, you only verify that the citations exist).

[identity.self]
{identity_self}

[Factory persona (compare against it when judging (c) fake growth: the candidate must not merely restate what is already written here)]
{personality}

[Base persona — applies to every character]
{base_persona}

[taboos]
{taboos}

[Who I am now, currently in effect]
{current}

[Candidate]
{candidate}

[Evidence attached to the candidate]
{rationale}

[Her record (exactly what the keeper saw)]
{bundle}

Reply with Report: summary is one sentence; the first word of details must be PASS or KILL, and after KILL comes a one-sentence reason (say which of (a)-(e) it is)."""


def assemble_bundle(*, memory_root: Path, hwm: int, window_days: float,
                    budget_chars: int = EVIDENCE_BUDGET_CHARS,
                    now: float) -> tuple[str, int]:
    """Evidence bundle + new HWM offset. Truncation order (spec §3.3,
    load-bearing): drop oldest-first WITHIN class; sacrifice consolidated
    before diary before self_history — never invert provenance weighting.

    ``now`` is the single injected clock (review Important): the window
    cutoff derives from it, never from wall time, so the whole pass shares
    one clock and the tests stay hermetic."""
    hist_text, new_off = evo.history_snapshot(memory_root / "self_history.jsonl", hwm)
    profile_path = memory_root / "self_profile.md"
    profile = profile_path.read_text(encoding="utf-8") if profile_path.exists() else ""

    cutoff = (_date.fromtimestamp(now) - timedelta(days=window_days)).isoformat()

    def _dated_files(d: Path) -> list[Path]:
        if not d.exists():
            return []
        return sorted(f for f in d.glob("*.md")
                      if evo._DATE_RE.match(f.stem) and f.stem >= cutoff)

    diaries: list[tuple[str, str]] = []
    for f in _dated_files(memory_root / "shared"):
        text = f.read_text(encoding="utf-8")
        if evo.has_diary_heading(text):
            diaries.append((f.stem, text))
    consolidated = [(f.stem, f.read_text(encoding="utf-8"))
                    for f in _dated_files(memory_root / "consolidated")]

    def _render(title: str, items: list[tuple[str, str]]) -> str:
        return "\n".join(f"[{title} {d}]\n{t}" for d, t in items)

    fixed = f"[self_profile]\n{profile}\n\n[self_history events]\n{hist_text}"
    while True:
        bundle = "\n\n".join(x for x in (
            fixed,
            _render("Diary", diaries),
            _render("consolidated · secondary", consolidated),
        ) if x.strip())
        if len(bundle) <= budget_chars:
            return bundle, new_off
        if consolidated:
            consolidated.pop(0)          # oldest consolidated first
        elif diaries:
            diaries.pop(0)               # then oldest diary
        else:
            fixed = fixed[-budget_chars:]  # last resort: trim history head


def parse_keeper_report(details: str) -> tuple[str, str, str]:
    """→ ("no_change", reason, "") | ("candidate", text, rationale).

    Policy (spec §3.3 failure-table 'malformed Report' row): NO_CHANGE-prefixed →
    no_change; CANDIDATE-prefixed → candidate; NO prefix but a line starting
    ``Evidence`` present → candidate (a weak model dropped the prefix but the
    structure is there); anything else → ValueError, which the caller maps to
    the evo_error row (NO HWM commit, NO interval double — a formatting miss must
    not silently consume weeks of evidence). The ``Evidence`` split is anchored
    on a LINE boundary (``\\nEvidence``), so candidate prose that uses the word
    mid-sentence is not truncated (the prompt asks for ``Evidence:`` on its own
    line). English markers only: this parses the keeper's fresh reply, never a
    stored file."""
    d = (details or "").strip()
    if not d:
        raise ValueError("keeper returned empty details")
    if d.upper().startswith("NO_CHANGE"):
        return "no_change", d[len("NO_CHANGE"):].strip(" ,:") or "not enough evidence", ""
    has_prefix = d.upper().startswith("CANDIDATE")
    has_evidence_line = any(line.startswith("Evidence") for line in d.splitlines())
    if not has_prefix and not has_evidence_line:
        raise ValueError("keeper report has no CANDIDATE/NO_CHANGE prefix nor Evidence line")
    body = d[len("CANDIDATE"):].strip() if has_prefix else d
    text, sep, rationale = body.rpartition("\nEvidence")
    if sep:
        return "candidate", text.strip(), rationale.strip(" :\n")
    return "candidate", body.strip(), ""


async def _run_keeper_agent(*, task, model, renderer, memsearch, memory_root,
                            transcripts_root, tool_output_store, max_tokens,
                            think_budget_tokens):
    system = renderer.render("subagent_scaffolding")
    return await run_agent(
        task=task, system=system, model=model, renderer=renderer,
        memory_root=memory_root, memsearch=memsearch,
        transcripts_root=transcripts_root, tool_output_store=tool_output_store,
        tools=KEEPER_TOOLS, kind="keeper", max_tokens=max_tokens,
        think_budget=think_budget_tokens, purpose="evolution",
        shell_runner=None, monitor_runner=None)


async def _run_full_skeptic(*, candidate, rationale, bundle, current, pack_identity,
                            base_persona, cap, model, renderer, memsearch, memory_root,
                            transcripts_root, tool_output_store, max_tokens,
                            think_budget_tokens):
    """(a)-(e) skeptic on the byte-identical bundle. → 'pass' | 'kill:<reason>'.
    ``cap`` is her who-I-am-now length cap, named in check (c)."""
    system = renderer.render("subagent_scaffolding")
    task = _FULL_SKEPTIC_TASK.format(
        identity_self=pack_identity.self, personality=pack_identity.personality,
        base_persona=base_persona, taboos=pack_identity.taboos, current=current or "(none yet)", candidate=candidate,
        rationale=rationale or "(none attached)", bundle=bundle, cap=cap)
    report = await run_agent(
        task=task, system=system, model=model, renderer=renderer,
        memory_root=memory_root, memsearch=memsearch,
        transcripts_root=transcripts_root, tool_output_store=tool_output_store,
        tools=KEEPER_TOOLS, kind="keeper", max_tokens=max_tokens,
        think_budget=think_budget_tokens, purpose="evolution",
        shell_runner=None, monitor_runner=None)
    if not report or not report.get("details"):
        raise RuntimeError("skeptic returned no verdict")
    details = report["details"].strip()
    if details.upper().startswith("PASS"):
        return "pass"
    if details.upper().startswith("KILL"):
        return "kill:" + (details[len("KILL"):].strip(" :") or "failed the (a)-(e) review")
    # Neither PASS nor KILL — a garbled verdict is an ERROR, not a silent kill.
    # The kill path commits the HWM + doubles the interval (consuming the
    # evidence); a malformed verdict must instead flow to run_evolution_pass's
    # evo_error branch, which preserves the evidence (spec §3.3 failure table).
    raise RuntimeError(f"skeptic verdict neither PASS nor KILL: {details[:40]!r}")


async def run_evolution_pass(*, model, renderer, memsearch, memory_root: Path,
                             transcripts_root: Path, tool_output_store,
                             pack_identity, base_persona: str, enforcement, floor: int, cap: int,
                             max_tokens: int, now: float, hwm: int = 0,
                             window_days: float = 28.0,
                             think_budget_tokens: int = 1024) -> str:
    """One Mode-A pass → "no_change" | "candidate" | "kill" | "error".

    ``max_tokens`` is each agent call's answer budget; ``think_budget_tokens``
    caps its thinking on top."""
    history_path = memory_root / "self_history.jsonl"
    slot_path = memory_root / "self_evolution" / "pending.json"
    current = self_history.sanctioned_text(history_path)
    bundle, _new_off = assemble_bundle(memory_root=memory_root, hwm=hwm,
                                       window_days=window_days, now=now)
    task = _KEEPER_TASK.format(
        identity_self=pack_identity.self, personality=pack_identity.personality,
        current=current or "(none yet — this will be the first version)", bundle=bundle,
        floor=floor, cap=cap)
    try:
        report = await _run_keeper_agent(
            task=task, model=model, renderer=renderer, memsearch=memsearch,
            memory_root=memory_root, transcripts_root=transcripts_root,
            tool_output_store=tool_output_store, max_tokens=max_tokens,
            think_budget_tokens=think_budget_tokens)
        kind, text, rationale = parse_keeper_report(
            (report or {}).get("details", ""))
    except Exception:
        logger.exception("evolution keeper errored")
        evo.log_or_raise(history_path, kind=evo.EVO_ERROR, detail="keeper error")
        return "error"

    if kind == "no_change":
        evo.log_or_raise(history_path, kind=evo.EVO_NO_CHANGE, reason=text)
        return "no_change"

    reason = evo.mechanical_checks(text, floor=floor, cap=cap,
                                   enforcement=enforcement)
    if reason is not None:
        evo.log_or_raise(history_path, kind=evo.EVO_KILL,
                         reason=f"mechanical:{reason}", text=text)
        return "kill"

    # Code-level restatement gate + ONE feedback retry (live-smoke SA
    # adjudication #2, KEEPER PATH ONLY): prompt-only enforcement of check (c)
    # is unreliable on the weak model — post-prompt-fix candidates still
    # restated factory prose 2/2 and the sharpened skeptic passed both, and the
    # ambivalent text flips the §6.2 disposition probe. Sovereignty rule stays
    # intact: her counter / the user's external edit are never gated on this
    # (frozen core only — SelfRevision runs mechanical_checks alone).
    pack_texts = [pack_identity.self, pack_identity.personality]
    offending = evo.restatement_overlap(text, pack_texts)
    if offending is not None:
        retry_task = task + (
            f"\n\nThe previous version was sent back: this sentence in the candidate "
            f"only restates the factory persona: \"{offending}\". Delete every "
            "sentence that restates the factory persona, keep only the changes the "
            "record shows, and write it again.")
        try:
            report = await _run_keeper_agent(
                task=retry_task, model=model, renderer=renderer,
                memsearch=memsearch, memory_root=memory_root,
                transcripts_root=transcripts_root,
                tool_output_store=tool_output_store, max_tokens=max_tokens,
                think_budget_tokens=think_budget_tokens)
            kind, text, rationale = parse_keeper_report(
                (report or {}).get("details", ""))
        except Exception:
            # Retry error/malformed → verdicted KILL, not the evo_error row:
            # the FIRST reply already restated — the keeper examined the
            # evidence and produced a bad candidate (HWM committed, interval
            # doubles; the next interval retries on fresh material).
            logger.exception("evolution keeper restatement-retry errored")
            evo.log_or_raise(history_path, kind=evo.EVO_KILL, text=text,
                             reason=f"mechanical:restatement:{offending}")
            return "kill"
        if kind == "no_change":
            # Once the restated sentences are deleted nothing evidence-backed
            # remains — the keeper's honest verdict (better nothing than filler).
            evo.log_or_raise(history_path, kind=evo.EVO_NO_CHANGE, reason=text)
            return "no_change"
        reason = evo.mechanical_checks(text, floor=floor, cap=cap,
                                       enforcement=enforcement)
        if reason is not None:
            evo.log_or_raise(history_path, kind=evo.EVO_KILL,
                             reason=f"mechanical:{reason}", text=text)
            return "kill"
        still_offending = evo.restatement_overlap(text, pack_texts)
        if still_offending is not None:
            evo.log_or_raise(history_path, kind=evo.EVO_KILL, text=text,
                             reason=f"mechanical:restatement:{still_offending}")
            return "kill"

    try:
        verdict = await _run_full_skeptic(
            candidate=text, rationale=rationale, bundle=bundle, current=current,
            pack_identity=pack_identity, base_persona=base_persona, cap=cap,
            model=model, renderer=renderer,
            memsearch=memsearch, memory_root=memory_root,
            transcripts_root=transcripts_root,
            tool_output_store=tool_output_store, max_tokens=max_tokens,
            think_budget_tokens=think_budget_tokens)
    except Exception:
        logger.exception("evolution full skeptic errored")
        evo.log_or_raise(history_path, kind=evo.EVO_ERROR, detail="skeptic error")
        return "error"

    if verdict != "pass":
        evo.log_or_raise(history_path, kind=evo.EVO_KILL, text=text,
                         reason=verdict.split(":", 1)[1] if ":" in verdict else verdict)
        return "kill"

    # Mid-pass slot race (Task-4 review Minor 1): while the keeper/skeptic LLM
    # calls were in flight, a background-perception turn's tripwire may have
    # created an external awaiting_skeptic slot. The gate's condition-5 check
    # ran BEFORE this pass launched, so re-check here and yield — never clobber
    # a live slot. Checked BEFORE the birth line so evo_candidate is only
    # logged when the slot will actually land (log-before-mutate stays intact:
    # check → birth line → save_slot). The trigger's kill bookkeeping
    # (interval ×2, HWM commit) is acceptable: the evidence was examined and
    # produced a candidate that lost the race; the next pass re-runs on new
    # material.
    if evo.load_slot(slot_path, history_path=history_path) is not None:
        logger.warning("evolution keeper: slot created mid-pass; yielding candidate")
        evo.log_or_raise(history_path, kind=evo.EVO_KILL, text=text,
                         reason="superseded:slot_created_mid_pass")
        return "kill"

    # Log-before-mutate: birth line, then slot (Plan-2 invariant).
    evo.log_or_raise(history_path, kind=evo.EVO_CANDIDATE, text=text,
                     rationale=rationale, hwm_before=hwm)
    evo.save_slot(slot_path, evo.make_keeper_slot(
        candidate=text, rationale=rationale, hwm_before=hwm, created_ts=now))
    return "candidate"
