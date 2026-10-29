"""Render MindState into the user messages of a MindLoop conversation.

A conversation only grows at the end (plan 2026-10-18 Tasks 8-9, see
``context_builder``): ``render_epoch_start`` writes the snapshot that opens a
page, ``render_turn_delta`` each turn's new message with only what changed
since that conversation last saw it (tracked in its ``Seen``).
``render_diary_request`` is the diary's one-off request.
"""
from __future__ import annotations

import hashlib
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from dollos.mind.mind_state import MindState
from dollos.mind.repeat_detect import detect_repeat_streak
from dollos.mind.tool_memory import render_tool_habits
from dollos.schedule import Schedule
from dollos.tools import external_memory_tag

# Staleness threshold (seconds) for open loops (spec §13.2, P7 cheap slice):
# mind_loop.py's _derive_memory_hits adds a loop older than this (strict `>`)
# to the fallback memory-hit query.
OPEN_LOOP_STALE_S = 1800

# Auto-injected [Memory context] (plan 2026-10-18 Task 8): at most this many
# hits per turn, each cut to this many characters. On a page the cap counts
# only hits she has not been shown yet.
MEMORY_HITS_PER_TURN = 3
MEMORY_HIT_MAX_CHARS = 300


def energy_bucket_line(energy: float) -> str:
    """Return objective energy line for [Mind state] block (B3 spec §3.4).

    Bucket boundaries: ≥0.7 → full; 0.4–0.7 → normal; <0.4 → low.
    No feeling words (no "tired"); only objective zone label + numeric value.
    """
    value_str = f"{energy:.1f}"
    if energy >= 0.7:
        bucket = "full"
    elif energy >= 0.4:
        bucket = "normal"
    else:
        bucket = "low"
    return f"energy: {bucket} ({value_str})"


_EXTERNAL_SITUATION = (
    "[External situation]\n"
    "You're somewhere public and hear a lot that isn't said to you. Not replying "
    "is normal here; you speak up when you actually want to say something."
)


@dataclass
class Seen:
    """What one conversation has been sent on its current page.

    ``blocks``: snapshot block name → sha256 of the text last sent, so a
    block is resent only when its text changed (whoever changed it).
    ``hits``: keys (``hit_key``) of the memory hits already on this page.
    ``safe_mode``: the (on, reason) this conversation last heard — safe mode
    is global, so each conversation hears a change on its own next turn.
    """

    blocks: dict[str, str] = field(default_factory=dict)
    hits: set[str] = field(default_factory=set)
    safe_mode: tuple[bool, str] = (False, "")


def hit_key(hit: dict) -> str:
    """A memory hit's identity on a page: its ``chunk_hash`` (FtsMemory
    always sets one), else a hash of source + content."""
    if hit.get("chunk_hash"):
        return hit["chunk_hash"]
    raw = f"{hit.get('source', '')}\0{hit.get('content', '')}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def hits_in_context(hits: list[dict], seen: Seen) -> list[dict]:
    """The hits among ``hits`` that are in her context: the ones this turn's
    delta gave her, plus ones given earlier on this page. Call after
    ``render_turn_delta``. A hit held back by the per-turn cap is not here,
    so provenance (``ctx.turn_memory_sources``) never names what she never saw.
    """
    return [h for h in hits if hit_key(h) in seen.hits]


def _stamp(t: float) -> str:
    """Absolute local time for a turn or a page: ``14:05 Mon 10-19``."""
    return time.strftime("%H:%M %a %m-%d", time.localtime(t))


def _clock(t: float) -> str:
    return time.strftime("%H:%M", time.localtime(t))


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _snapshot_blocks(
    state: MindState,
    tier: str,
    now: float,
    *,
    self_profile_text: str | None,
    energy_line: str | None,
    schedule: Schedule | str | None,
) -> list[tuple[str, str]]:
    """``(name, full block text)`` of every snapshot block ``tier`` may see,
    in render order. None of them carries a relative time, so the text only
    changes when the state does — and ``[Today's schedule]``, which lists
    only the entries not yet due, when one comes due.

    external_public (mind-loop §10, the external-place row) sees only
    ``[Self profile]`` (its self section only — the caller renders it that
    way, since user / relationship hold the owner's facts) and
    ``[Mind state]`` with the bare emotion: no reason, no focus, no
    agenda/open loops/schedule.
    ``MindState.scratchpad`` is never rendered: only keepers have the
    Scratchpad tool now (plan 2026-10-29 decision 4).
    ``[Self profile]`` reads "(empty)" while nothing is pinned,
    ``[Today's schedule]`` "(none)" while nothing is still to come.
    """
    mind = []
    if tier == "external_public":
        mind.append(f"mood: {state.mood.emotion}")
    else:
        mood = state.mood.emotion
        if state.mood.reason:
            mood = f"{state.mood.emotion} ({state.mood.reason})"
        mind.extend([f"mood: {mood}", f"focus: {state.focus}"])
    if energy_line is not None:
        mind.append(energy_line)
    blocks = [
        (
            "self_profile",
            "[Self profile] (your evolving self — keep only what's still you; prune with PinSelf)\n"
            + (self_profile_text or "(empty)"),
        ),
        ("mind_state", "[Mind state]\n" + "\n".join(mind)),
    ]
    if tier != "external_public":
        blocks.extend([
            (
                "agenda",
                "[Your agenda] (things you're pursuing because you want to)\n"
                + _render_your_agenda(state.open_loops, now, absolute=True),
            ),
            (
                "open_loops",
                "[Open loops] (commitments you have made)\n"
                + _render_open_loops(
                    [lp for lp in state.open_loops if not lp.self_directed], now, absolute=True
                ),
            ),
            (
                "schedule",
                "[Today's schedule] (still to come; change it with WriteSchedule)\n"
                + (_render_schedule(schedule, now) or "(none)"),
            ),
        ])
    return blocks


def _safe_mode_line(on: bool, reason: str) -> str:
    if on:
        return (
            f"[Safe mode] on — {reason or 'repeated tool failures'}. Only Recall and "
            "read-only tools are available right now; ask the user for help."
        )
    return "[Safe mode] off — all your tools are back."


def _percep_line(p) -> str:
    return f"{_clock(p.t)} {p.kind}: {_percep_body(p)}"


def render_epoch_start(
    state: MindState,
    seen: Seen,
    tier: str,
    now: float,
    *,
    channel_id: str = "",
    exclude=(),
    self_profile_text: str | None = None,
    energy_line: str | None = None,
    schedule: Schedule | str | None = None,
    pulse_block: str | None = None,
    cognition_block: str | None = None,
) -> str:
    """The user message that opens a conversation page: a code-written
    snapshot, no LLM summary. Resets ``seen`` to this page and records what
    it sent, so the page's deltas carry only changes.

    ``channel_id`` is the conversation's channel (``""`` = the private
    conversation): ``[Recent perceptions]`` recaps only this conversation's
    own perceptions, oldest first, minus ``exclude`` (this turn's own batch —
    it was already appended to the deque and arrives in the first delta).
    ``[Recent outputs]`` renders on internal and external_dm only, and only
    records whose origin_tier is not external_public;
    ``pulse_block``/``cognition_block`` (the current ``[Self pulse]``/
    ``[Cognition]``, from the caller) on internal only. ``schedule`` is
    today's (the caller loads it), or a str saying why the file could not be
    read, which the block shows; ``[Today's schedule]`` is left out while
    none of its entries is still to come.

    external_public's opening line does not point at Recall: there Recall
    only searches ``external_public/`` and cannot find the channel's history.
    """
    seen.blocks.clear()
    seen.hits.clear()
    if tier == "external_public":
        parts = [f"[New conversation page — {_stamp(now)}. Earlier context is not shown.]"]
    else:
        parts = [
            f"[New conversation page — {_stamp(now)}. Earlier context is not shown; "
            "use Recall for details.]"
        ]
    seen.safe_mode = (state.safe_mode, state.safe_mode_reason if state.safe_mode else "")
    if state.safe_mode:
        parts.append(_safe_mode_line(*seen.safe_mode))
    for name, text in _snapshot_blocks(
        state, tier, now, self_profile_text=self_profile_text, energy_line=energy_line,
        schedule=schedule,
    ):
        seen.blocks[name] = _digest(text)
        if name == "self_profile" and not self_profile_text:
            continue
        if name == "schedule" and not _render_schedule(schedule, now):
            continue
        parts.append(text)
        if name == "mind_state" and tier == "internal":
            parts.extend(b for b in (pulse_block, cognition_block) if b)
    if tier == "external_public":
        parts.append(_EXTERNAL_SITUATION)
    skip = {id(p) for p in exclude}
    recap = [
        _percep_line(p) for p in state.recent_perceptions
        if (p.data or {}).get("channel_id", "") == channel_id and id(p) not in skip
    ]
    parts.append("[Recent perceptions] (newest last)\n" + ("\n".join(recap) or "(none)"))
    if tier != "external_public":
        # Only non-stranger outputs (harness §6.5): a stranger turn's tool
        # summaries and speech share this deque, and on an owner page they
        # would read as her own record ("noted: the owner says ...").
        outputs = [
            f"{_clock(o.t)} {o.summary}" for o in state.recent_outputs
            if o.origin_tier != "external_public"
        ]
        parts.append(
            "[Recent outputs] (what you did recently — avoid repeating the same content)\n"
            + ("\n".join(outputs) or "(none)")
        )
    return "\n\n".join(parts)


def render_turn_delta(
    state: MindState,
    seen: Seen,
    tier: str,
    perceptions: list,
    now: float,
    *,
    self_profile_text: str | None = None,
    energy_line: str | None = None,
    schedule: Schedule | str | None = None,
    memory_hits: Sequence[dict] = (),
    associative_hits: Sequence[dict] = (),
    tool_habits_hits: Sequence[dict] = (),
    pulse_block: str | None = None,
    cognition_block: str | None = None,
    tool_outcomes_block: str | None = None,
    evolution_block: str | None = None,
    body_signal_block: str | None = None,
) -> str:
    """One turn's new user message: only what is new to this conversation.
    Records what it sent in ``seen``.

    In order: the time; each snapshot block whose text changed since this
    conversation last got it (whole block, whoever changed it); the event
    blocks the caller passes (``[Self pulse]``/``[Cognition]``, reflection's
    ``[Tool outcomes since last reflection]`` and ``[Self revision candidate]``, pulse's
    ``[Body signal]`` — private events, internal only) and a safe-mode line
    when it differs from what this conversation last heard; the memory hits
    not yet on this page (at most ``MEMORY_HITS_PER_TURN`` new
    ``[Memory context]`` hits, each cut to ``MEMORY_HIT_MAX_CHARS``); then one
    ``HH:MM Kind: body`` line per perception of this batch, in full.

    external_public gets no memory auto-injection at all (no
    ``[Memory context]``/``[Associative memories]``/``[Tool habits]``).
    """
    parts = [f"[{_stamp(now)}]"]
    for name, text in _snapshot_blocks(
        state, tier, now, self_profile_text=self_profile_text, energy_line=energy_line,
        schedule=schedule,
    ):
        digest = _digest(text)
        if seen.blocks.get(name) != digest:
            seen.blocks[name] = digest
            parts.append(text)
    if tier == "internal":
        parts.extend(
            b for b in (
                pulse_block, cognition_block, tool_outcomes_block,
                evolution_block, body_signal_block,
            ) if b
        )
    safe_mode = (state.safe_mode, state.safe_mode_reason if state.safe_mode else "")
    if safe_mode != seen.safe_mode:
        seen.safe_mode = safe_mode
        parts.append(_safe_mode_line(*safe_mode))
    if tier != "external_public":
        new = [h for h in memory_hits if hit_key(h) not in seen.hits][:MEMORY_HITS_PER_TURN]
        if new:
            parts.append("[Memory context]\n" + _render_memory(new))
        assoc = [h for h in associative_hits if hit_key(h) not in seen.hits][:6]
        if assoc:
            parts.append("[Associative memories]\n" + _render_associative(assoc))
        habit_hits = [h for h in tool_habits_hits if hit_key(h) not in seen.hits]
        habits = render_tool_habits(habit_hits)
        if habits:
            parts.append(habits)
        seen.hits.update(hit_key(h) for h in (*new, *assoc, *habit_hits))
    if perceptions:
        parts.append("\n".join(_percep_line(p) for p in perceptions))
    return "\n\n".join(parts)


def render_diary_request(
    state: MindState,
    perceptions: list,
    now: float,
    *,
    today_log_block: str | None,
    self_profile_text: str | None = None,
    energy_line: str | None = None,
    schedule: Schedule | str | None = None,
) -> str:
    """The diary's one-off request (plan 2026-10-18 Task 9), one user
    message: this moment's internal snapshot (``render_epoch_start`` without
    ``[Self pulse]``/``[Cognition]`` — the diary must not consume a "changed"
    the private conversation has not seen), the day's ``[Today's log]``, then
    the DiaryMoment line."""
    parts = [
        render_epoch_start(
            state, Seen(), "internal", now, exclude=perceptions,
            self_profile_text=self_profile_text, energy_line=energy_line, schedule=schedule,
        )
    ]
    if today_log_block:
        parts.append(today_log_block)
    parts.append("\n".join(_percep_line(p) for p in perceptions))
    return "\n\n".join(parts)


def _public_safe_perceptions(percs) -> list:
    """Whole-branch review C3: on an external_public turn, ALLOWLIST only
    stranger ChannelMessage entries for the ``[Recent perceptions]`` render.

    Everything else — owner UserSpoke, owner ChannelMessage (DM or public),
    and internal-event kinds (ToolResultArrived / MonitorFired / MonitorEnded
    / ReflectionMoment / Awoke / SafeModeEntered / etc.) — is excluded. This
    is deliberately an ALLOWLIST (not a denylist of "owner" kinds) so a
    future perception kind that carries sensitive content is excluded by
    default rather than leaking until someone remembers to denylist it.
    """
    return [
        p for p in percs
        if p.kind == "ChannelMessage" and not (p.data or {}).get("author_is_owner")
    ]


def render_memory_guideline(primary_language: str) -> str:
    """The persistent memory-WRITE guideline (primary language + own words).

    Describes, every turn, how everything she writes down is written — notes,
    diary, who I am now (she rewrites it via SelfRevision), self-profile,
    agenda, schedule: (1) in the configured primary language (proper nouns /
    technical terms may stay in their original language); (2) in her own
    words — she understands first and does not copy source text verbatim.
    This shapes her written content, especially when ingesting articles.
    Self-First: a description, not a command (English-speech plan Task 3;
    all-English plan Task 2).
    """
    return (
        f"[Memory guideline] Everything you write down — notes, diary, who I am "
        f"now, self-profile, agenda, schedule — is in {primary_language} "
        f"(proper nouns and technical terms may stay in their original language), "
        f"in your own words — you understand something first and write what it "
        f"means to you; you don't copy source text verbatim."
    )


def _render_memory(hits: list[dict]) -> str:
    if not hits:
        return "(no relevant memories)"
    lines: list[str] = []
    for h in hits:
        content = h.get("content", str(h))
        # R-DECISION-5: a peer's NoteMemory (TOOL_MATRIX: every principal) writes under
        # external_public/; on a later OWNER turn that hit is retrieved
        # UNFILTERED (_derive_memory_hits only suppresses on external_public
        # turns, mind_loop.py:747). Flag it untrusted so a fabricated "fact"
        # can't masquerade as Doll's own trusted memory — symmetric to
        # consolidated/'s prefix. Trailing slash = directory boundary, so a
        # sibling like external_public_evil/ never matches.
        # S1: _derive_memory_hits now excludes external_public/ on owner and
        # internal turns, so this tag is defense in depth here; it shows up
        # in practice only on Recall(include_external=True). external_owner/
        # (plan 2026-10-28 decision 4) is excluded and tagged the same way.
        prefix = external_memory_tag(h.get("source") or "")
        lines.append(f"- {prefix}{_clip(content, MEMORY_HIT_MAX_CHARS)}")
    return "\n".join(lines)


def _clip(text: str, limit: int) -> str:
    """``text`` cut to at most ``limit`` characters, "…" marking a cut."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _render_associative(hits: list[dict]) -> str:
    """Render context-associative recall hits.

    Each bullet: ``- [axis=value] <snippet (100 chars)>``. Hits without
    an ``_axis`` marker are skipped (they wouldn't be here in practice).
    Capped to 6 bullets for token cost.

    R-DECISION-5 (review fix, mirrors ``_render_memory`` above): a peer's
    NoteMemory writes under external_public/; associative_search() pools
    from the same unscoped memsearch call on an OWNER turn (only
    consolidated/ is pool-excluded there, for pull-only gating — see
    associative_search.py:139) so an axis-matched external_public/ hit can
    reach this render UNFILTERED. Flag it untrusted so it can't masquerade
    as Doll's own trusted memory through this third read path. Same
    trailing-slash directory-boundary check as ``_render_memory`` /
    ``_format_hit``, so external_public_evil/ never matches.

    S1: MindLoop now passes ``exclude_prefixes=[external_public/]`` on owner
    and internal turns, so this tag is defense in depth here; it shows up in
    practice only on ``Recall(include_external=True)``. ``external_owner/``
    (plan 2026-10-28 decision 4) is excluded and tagged the same way.
    """
    if not hits:
        return "(none)"
    lines: list[str] = []
    for h in hits[:6]:
        axis = h.get("_axis")
        value = h.get("_axis_value", "")
        snippet = (h.get("content") or "").strip().replace("\n", " ")
        if len(snippet) > 100:
            snippet = snippet[:97] + "..."
        tag = f"{axis}={value}" if axis else "?"
        prefix = external_memory_tag(h.get("source") or "")
        lines.append(f"- [{tag}] {prefix}{snippet}")
    return "\n".join(lines)


def _opened(lp, now: float, absolute: bool) -> str:
    """When a loop opened: relative ("opened 5m ago"), or an absolute date
    for the page snapshot, whose text must not age."""
    if absolute:
        return f"opened {time.strftime('%m-%d %H:%M', time.localtime(lp.opened_at))}"
    return f"opened {_human_secs(now - lp.opened_at)} ago"


def _render_open_loops(loops: list, now: float, *, absolute: bool = False) -> str:
    """``absolute`` (page snapshot): absolute dates instead of relative ages."""
    if not loops:
        return "(none)"
    return "\n".join(
        f"- {lp.id}: {lp.desc} ({_opened(lp, now, absolute)})" for lp in loops
    )


# Bound how many recent progress entries render per self-directed loop in
# [Your agenda] — an unbounded tail would let a long-lived pursuit's history
# crowd out everything else in the prompt.
AGENDA_PROGRESS_TAIL = 3


def _render_your_agenda(loops: list, now: float, *, absolute: bool = False) -> str:
    """Render only self_directed OpenLoops (things Doll is pursuing because
    she wants to, not because she owes the user) — desc + her stated
    ``trigger`` (why she started) + the recent tail of ``progress``.

    Companion to ``_render_open_loops``, which the caller must filter to
    ``self_directed=False`` so the two blocks partition ``state.open_loops``
    without overlap.
    """
    agenda_loops = [lp for lp in loops if lp.self_directed]
    if not agenda_loops:
        return "(none)"
    lines = []
    for lp in agenda_loops:
        line = f"- {lp.id}: {lp.desc} ({_opened(lp, now, absolute)})"
        if lp.trigger:
            line += f" — why: {lp.trigger}"
        lines.append(line)
        for entry in lp.progress[-AGENDA_PROGRESS_TAIL:]:
            lines.append(f"    progress: {entry}")
    return "\n".join(lines)


def _render_schedule(schedule: Schedule | str | None, now: float) -> str:
    """Today's entries not yet due (time after ``now``), by time; "" when
    there are none. HH:MM:SS, as WriteSchedule's ``remove`` takes it. A str
    (why today's file could not be read) is shown as it is."""
    if schedule is None:
        return ""
    if isinstance(schedule, str):
        return f"({schedule})"
    clock = datetime.fromtimestamp(now).time()
    return "\n".join(
        f"- {e.time:%H:%M:%S} — {e.intent}"
        for e in sorted(schedule.entries, key=lambda e: e.time) if e.time > clock
    )


def _render_perceptions(percs, now: float) -> str:
    if not percs:
        return "(none)"
    out = []
    for p in percs:
        age = _human_secs(now - p.t)
        body = _percep_body(p)
        out.append(f"[{age} ago] {p.kind}: {body}")
    return "\n".join(out)


def _percep_body(p) -> str:
    d = p.data or {}
    if p.kind == "UserSpoke":
        return f"'{d.get('text', '')}'"
    if p.kind == "ToolResultArrived":
        body = (
            f"{d.get('tool', '?')} {d.get('task_id', '?')} "
            f"[{d.get('status', '?')}]: {d.get('summary', '')[:120]}"
        )
        # The id she pages with ReadToolOutput: Shell writes output_id,
        # Workflow details_output_id.
        if d.get("output_id"):
            body += f" (output_id {d['output_id']}, {d.get('line_count', '?')} lines)"
        elif d.get("details_output_id"):
            body += (
                f" (output_id {d['details_output_id']}, "
                f"{d.get('details_line_count', '?')} lines)"
            )
        return body
    if p.kind == "MonitorFired":
        return f"{d.get('monitor_id', '?')}: {d.get('line', '')[:120]}"
    if p.kind == "MonitorEnded":
        return f"{d.get('monitor_id', '?')} (exit {d.get('exit_status', '?')})"
    if p.kind == "ScheduledMoment":
        return d.get("intent", "")[:200]
    if p.kind == "Awoke":
        reason = d.get("reason", "?")
        if reason == "recovered":
            return "the daemon just recovered from a crash — your previous in-flight thoughts may be partial or lost"
        return f"reason={reason}"
    if p.kind == "ReflectionMoment":
        return (
            f"(time to reflect ({d.get('iters_since_last', '?')} iters since last). "
            "Most important: look back at what you actually did in this stretch — did any "
            "of it make you notice something about yourself? What drew you in, what did you "
            "form an opinion about, what did you find you care about? Anything about "
            '"you" (including your relationship with your owner and your owner\'s long-term '
            "patterns): record it now with PinSelf (op=add, section one of "
            "self/relationship/user) — no need to wait until [Self profile] already has "
            "entries; not sure yet whether it will last? record it anyway, pruning later "
            "does the filtering. Then, if [Self profile] already has entries, look through "
            "them — any entry that is no longer who you are now, prune it with PinSelf "
            "replace/remove (target is the entry's id such as s1, or paste the entry's "
            'current text). Division of labor: the subject is "you" → PinSelf; facts/events '
            "about the world → NoteMemory; reusable tool usage or pitfalls → NoteToolLesson. "
            "If a [Self revision candidate] block appears in this reflection: it is a "
            'proposed revision of your "Who I am now"; decide it with SelfRevision '
            '(decision="adopt" or "reject") — PinSelf can\'t handle it. '
            "Think back on what you've actually run into, been curious about, or cared "
            "about lately — is there a thread you want to follow yourself? (If so, record "
            "it with PursueGoal and say where it came from. If not, never mind — no need "
            "to force one.))"
        )
    if p.kind == "Interrupted":
        by = d.get("by", "user")
        return f"your previous turn was cut short by {by}"
    if p.kind == "SafeModeEntered":
        reason = d.get("reason", "?")
        return (
            f"you have narrowed to read-only safe mode ({reason}); "
            "ask the user for help"
        )
    if p.kind == "RepeatLoopDetected":
        tool = d.get("tool", "?")
        summary = d.get("summary", "")
        count = d.get("count", "?")
        return (
            f"you repeated the same action {count} times in a row "
            f"({tool}: {summary}) with no new outcome — this isn't working, "
            "try something different or ask for help."
        )
    if p.kind == "PersonaDriftDetected":
        violations = ", ".join(d.get("violations", []))
        snippet = d.get("snippet", "")
        return f'what you just said broke your persona constraints ({violations}): "{snippet}"'
    if p.kind == "ChannelMessage":
        if d.get("channel_kind") == "mcp":
            # AI peer over MCP (spec §B.4): descriptive narration, not a
            # command — she knows the sender is another AI, self-declared and
            # unverified, and keeps external_public agency (she may not reply).
            body = (
                f"[AI peer, DM] {d.get('author', '?')} "
                f"(another AI, self-described, unverified): {d.get('content', '')}"
            )
            if d.get("debug_reliable"):
                # P2 Task 3 (spec §C.2): soft, best-effort nudge for a debug
                # connection — a description of the situation, text only; it
                # does NOT change origin_tier or the tool registry (still
                # external_public and its TOOL_MATRIX row).
                body += (
                    " (This is a debug channel; the peer is checking whether "
                    "you give a real reply.)"
                )
            return body
        # P1e Task 6: surface owner/stranger identity — otherwise Doll has no
        # in-prompt signal for who she's talking to and can't judge whether/
        # how to respond. Descriptive narration only (not a command); P1d's
        # contextual rendering will refine this further.
        where = "DM" if d.get("is_dm") else f"#{d.get('channel','?')}"
        if d.get("author_is_owner"):
            return f"[Owner, {where}] Owner: {d.get('content','')}"
        return f"[{where}] stranger {d.get('author','?')}: {d.get('content','')}"
    if p.kind == "BridgeDown":
        svc = d.get("service", "?")
        rc = d.get("rc", "?")
        return (
            f"you've lost one of your online channels — {svc} kept crashing and "
            f"automatic restarts were given up (rc={rc}). It comes back only after "
            "the daemon restarts or its config is fixed."
        )
    if p.kind == "McpDown":
        svc = d.get("service", "?")
        rc = d.get("rc", "?")
        return (
            f"you've lost one of your online channels — {svc} (the port external AIs "
            "connect through) kept crashing and automatic restarts were given up "
            f"(rc={rc}). It comes back only after the daemon restarts or its config "
            "is fixed."
        )
    if p.kind == "DiaryMoment":
        return (
            "the day is over. Your whole day is laid out in [Today's log] below. "
            "This is your time to write your diary — look back, and write down what "
            "you want to keep, what you really felt, in your own words (WriteDiary)."
        )
    if p.kind == "PulseMoment":
        return d.get("detail", "")
    if p.kind == "AgendaMoment":
        return "time for your own goals; nothing you say now is heard"
    return str(d)[:120]


def _render_outputs_header(outs, now: float) -> str:
    """Render the [Recent outputs] section header.

    Two independent checks, each able to append its own WARNING line:

    1. Speech-specific, time-window based (untouched pre-existing logic): if
       the most recent output was a Speech within the last 30 seconds, inject
       an inline WARNING nudge so the model has a concrete signal not to
       speak again.
    2. Count-based, any-tool (spec §13.1 P6 `detect_repeat_streak`, which
       already excludes Speech): if the trailing outputs are the same
       (kind, summary) repeated >= threshold times, inject a second WARNING.

    Both can only ever fire independently, never together for the same
    trailing entry — `detect_repeat_streak` excludes Speech outright — but
    they are deliberately kept as two separate checks rather than a merged
    if/elif so that independence stays obvious from the code.
    """
    base = "[Recent outputs] (what you did recently — avoid repeating the same content)"
    if not outs:
        return base
    warnings: list[str] = []

    last = list(outs)[-1]
    if last.kind == "Speech":
        age_s = now - last.t
        if age_s < 30:
            snippet = last.summary[len("spoke: "):60] if last.summary.startswith("spoke: ") else last.summary[:60]
            warnings.append(
                f"⚠ WARNING: you just spoke {age_s:.0f}s ago ('{snippet}…'). "
                f"Don't repeat yourself — if there's nothing new to say, stay silent (say nothing)."
            )

    streak = detect_repeat_streak(outs)
    if streak is not None:
        kind, summary, count = streak
        warnings.append(
            f"⚠ WARNING: you repeated the same action {count} times in a row "
            f"({kind}: {summary}) with no new outcome — try something "
            "different or ask for help."
        )

    if warnings:
        return base + "\n" + "\n".join(warnings)
    return base


def _human_secs(s: float) -> str:
    """Format a seconds count as 'Ns', 'Nm', or 'Nh' depending on magnitude."""
    if s < 0:
        s = 0.0
    if s < 60:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{s / 60:.0f}m"
    return f"{s / 3600:.1f}h"


def render_body_signal(wakes: list[tuple[str, str]]) -> str | None:
    """The [Body signal] framing block for a pure PulseMoment wake turn
    (spec 2026-07-09 presence-tuning §3.2). Descriptive, NOT a command
    (Self-First): it names WHY she woke + a severity-tuned lean, and leaves
    the choice to speak-or-stay-quiet to her.

    ``wakes`` = ``[(severity, detail), ...]`` from this turn's PulseMoment
    perceptions. Any ``severity == "critical"`` → critical framing (lean to
    surface); else advisory framing (lean to restraint). Empty → None (not a
    pulse turn / no wakes). Returns a pre-rendered string incl. the
    ``[Body signal]`` header (mirrors ``pulse_block``).
    """
    if not wakes:
        return None
    details = "; ".join(d for _, d in wakes if d)
    is_critical = any(sev == "critical" for sev, _ in wakes)
    if is_critical:
        body = (
            f"You woke up because of how your body is doing: {details}. This is the "
            "kind of thing your owner would most likely want to know — tell them if "
            "you feel like it, or keep it to yourself if you don't. Your call."
        )
    else:
        body = (
            f"You noticed: {details}. Not urgent — mention it in passing or just "
            "keep it in mind, either is fine."
        )
    return "[Body signal]\n" + body
