"""Render MindState into a complete LLM prompt for one MindLoop iteration.

Returns: system_prompt + the enabled dynamic blocks as a single string.
"""
from __future__ import annotations

import time

from dollos.mind.mind_state import MindState
from dollos.mind.repeat_detect import detect_repeat_streak
from dollos.mind.tool_memory import render_tool_habits, render_tool_notes

# Staleness threshold (seconds) for open loops (spec §13.2, P7 cheap slice).
# Shared between the render's ⚠ STALE marker (_render_open_loops below) and
# the fallback memory-hit query injection in mind_loop.py's
# _derive_memory_hits — both must compare with the exact same strict `>`
# against this same constant so a loop is consistently "stale" (or not)
# wherever it matters.
OPEN_LOOP_STALE_S = 1800


def energy_bucket_line(energy: float) -> str:
    """Return objective energy line for [Mind state] block (B3 spec §3.4).

    Bucket boundaries: ≥0.7 → 飽滿; 0.4–0.7 → 普通; <0.4 → 偏低.
    No feeling words (no 「累」); only objective zone label + numeric value.
    """
    value_str = f"{energy:.1f}"
    if energy >= 0.7:
        bucket = "飽滿"
    elif energy >= 0.4:
        bucket = "普通"
    else:
        bucket = "偏低"
    return f"精力: {bucket} ({value_str})"


def render_mind(
    state: MindState,
    memsearch_hits: list[dict],
    system_prompt: str,
    *,
    pulse_block: str | None = None,
    cognition_block: str | None = None,
    today_log_block: str | None = None,
    associative_hits: list[dict] | None = None,
    primary_language: str = "繁體中文",
    tool_outcomes_block: str | None = None,
    tool_habits_hits: list[dict] | None = None,
    energy_line: str | None = None,
    self_profile_text: str | None = None,
    evolution_block: str | None = None,
    origin_tier: str = "internal",
) -> str:
    """Compose: system_prompt + the enabled dynamic blocks.

    ``pulse_block`` — optional pre-rendered ``[Self pulse]`` block from
    ``perception.system_pulse.SystemPulse.snapshot()``. Inserted after
    ``[Active tasks]`` (mirrors the ``[Active monitors]`` slot).

    ``cognition_block`` — optional pre-rendered ``[Cognition]`` block from
    ``perception.cognition.CognitionWorker.snapshot()``. Inserted right
    after ``[Self pulse]``.

    ``today_log_block`` — optional pre-rendered ``[Today's log]`` block from
    ``MindLoop._read_today_log()`` (spec §2.2, Task 7): the day's full
    action-log transcript, the diary turn's source material. Inserted right
    after ``[Cognition]``. ``None`` on every non-diary turn (the caller only
    passes it on a dedicated DiaryMoment turn).

    ``primary_language`` — the language Doll records memory in (NoteMemory /
    diary). Rendered as a persistent ``[Memory guideline]`` block right before
    ``[Memory context]`` so it is present EVERY turn (covers article-ingestion
    turns, which arrive as ordinary user turns). It shapes WHAT she writes —
    not a new tool, just a behavioral guideline (prompt engineering).

    ``self_profile_text`` — optional pre-rendered body from
    ``self_profile.render_block()`` (A1 pinned self-profile). Inserted right
    after ``system_prompt`` and before ``[Memory guideline]`` (core self goes
    first). Omitted entirely when ``None``/empty (no bullets pinned yet).

    ``evolution_block`` — optional pre-rendered ``[人格演化候選]`` block from
    ``evolution.surface_or_expire()`` (spec §3.4). Inserted right after
    ``[Self profile]`` and before ``[Memory guideline]`` so it stays salient on
    reflection turns. Omitted entirely when ``None`` (no pending slot to
    surface, or not a reflection/safe-mode turn — gated by the caller).

    ``origin_tier`` — whole-branch review C3: on ``"external_public"``
    (stranger) turns, ``[Recent perceptions]`` is scoped to ONLY stranger
    ChannelMessage entries (see ``_public_safe_perceptions``) and
    ``[Recent outputs]`` is suppressed entirely — both deques are GLOBAL
    rolling buffers appended for every turn regardless of origin, so
    unscoped they would render prior owner UserSpoke/owner-DM text (up to
    200 chars, verbatim) and Doll's prior private replies straight into a
    stranger's prompt. ``"internal"`` (the default) and ``"external_dm"``
    render both blocks in full, unchanged.

    On ``"external_public"`` turns a ``[External situation]`` block is also
    appended (P1c Task 6, L2 secondary layer — the code-side admission gate
    is the main defense against 亂回): descriptive scaffolding narration
    (not a command) so staying silent reads as a normal option, since she's
    overhearing a lot that isn't addressed to her. Omitted for ``"internal"``
    and ``"external_dm"`` — owner-DM is a private 1:1, not a public channel.
    """
    now = time.time()
    blocks = [
        system_prompt,
        "",
    ]
    if self_profile_text:
        blocks.extend([
            "[Self profile] (your evolving self — keep only what's still you; prune with PinSelf)",
            self_profile_text,
            "",
        ])
    if evolution_block:
        blocks.extend([evolution_block, ""])
    blocks.extend([
        _render_memory_guideline(primary_language),
        "",
    ])
    # Persistent [Safe mode] banner — rendered EVERY turn while safe_mode is set
    # (not edge-triggered), so the narrowed-to-read-only state stays visible
    # until a user turn clears it (spec §8.3).
    if state.safe_mode:
        reason = state.safe_mode_reason or "repeated tool failures"
        blocks.extend([
            f"[Safe mode] read-only — {reason}. Only Recall and read-only tools "
            "are available right now; ask the user for help.",
            "",
        ])
    blocks.extend([
        "[Memory context]",
        _render_memory(memsearch_hits),
        "",
        "[Associative memories]",
        _render_associative(associative_hits or []),
        "",
        "[Mind state]",
        _render_mindstate(state, now, energy_line=energy_line),
        "",
        "[Active tasks] (currently running, you cannot cancel)",
        _render_active_tasks(state.active_tasks, now),
        "",
    ])
    if pulse_block:
        blocks.extend([pulse_block, ""])
    if cognition_block:
        blocks.extend([cognition_block, ""])
    if today_log_block:
        blocks.extend([today_log_block, ""])
    blocks.extend([
        "[Your agenda] (things you're pursuing because you want to)",
        _render_your_agenda(state.open_loops, now),
        "",
        "[Open loops] (commitments you have made)",
        _render_open_loops([lp for lp in state.open_loops if not lp.self_directed], now),
        "",
        "[Pending] (upcoming scheduled events)",
        _render_pending(state.pending_events, now),
        "",
        "[Scratchpad]",
        state.scratchpad or "(empty)",
        "",
    ])
    if state.recent_reviews:
        blocks.extend([
            "[Recent self-review] (your own post-hoc critiques, oldest first)",
            _render_recent_reviews(state.recent_reviews),
            "",
        ])
    if tool_outcomes_block:
        blocks.extend([tool_outcomes_block, ""])
    tool_notes = render_tool_notes(state.recent_tool_failures, now)
    if tool_notes:
        blocks.extend([tool_notes, ""])
    habits = render_tool_habits(tool_habits_hits or [])
    if habits:
        blocks.extend([habits, ""])
    # Whole-branch review C3: on an external_public (stranger) turn, scope
    # the working-memory render — both deques are GLOBAL rolling buffers
    # appended for EVERY turn regardless of origin, so left unscoped they
    # would leak prior owner UserSpoke/owner-DM text and Doll's prior
    # private replies straight into a stranger's prompt.
    percs_for_render = (
        _public_safe_perceptions(state.recent_perceptions)
        if origin_tier == "external_public"
        else state.recent_perceptions
    )
    blocks.extend([
        "[Recent perceptions] (newest last)",
        _render_perceptions(percs_for_render, now),
        "",
    ])
    if origin_tier != "external_public":
        blocks.extend([
            _render_outputs_header(state.recent_outputs, now),
            _render_outputs(state.recent_outputs, now),
            "",
        ])
    if origin_tier == "external_public":
        # P1c Task 6: L2 secondary layer — the code-side admission gate
        # (Tasks 1-5) is the MAIN defense against 亂回; once admitted she can
        # still choose Say or silence (cascade emits 0..N actions). This is a
        # DESCRIPTIVE scaffolding nudge (narration, not a command) so silence
        # reads as a normal option on a public turn — she's overhearing a lot
        # that isn't addressed to her. Only external_public: owner-DM
        # (external_dm) is a private 1:1 where "公開場合,不關妳的話" doesn't
        # apply.
        blocks.extend([
            "[External situation]",
            "妳在公開場合,聽得到很多不是對妳說的話。不回應是很正常的 — 只在真的想說話時開口。",
            "",
        ])
    blocks.extend([
        "[Decision time]",
        "What do you do this iteration? Output a JSON array of 0..N actions.",
    ])
    return "\n".join(blocks)


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


def _render_memory_guideline(primary_language: str) -> str:
    """The persistent memory-WRITE guideline (primary language + own words).

    Two rules, every turn: (1) record memory in the configured primary
    language (proper nouns / technical terms may stay in their original
    language where natural); (2) write your OWN understanding in your OWN
    words — understand first, never copy source text verbatim. This shapes
    NoteMemory / diary content, especially when ingesting articles.
    """
    return (
        f"[Memory guideline] When you record anything to memory (NoteMemory or "
        f"diary), write it in {primary_language} — you may keep proper nouns and "
        f"technical terms in their original language where natural. Record your "
        f"OWN understanding in your OWN words; never copy source text verbatim — "
        f"understand first, then write what it means to you."
    )


def _render_memory(hits: list[dict]) -> str:
    if not hits:
        return "(no relevant memories)"
    lines: list[str] = []
    for h in hits:
        content = h.get("content", str(h))
        # R-DECISION-5: a peer's NoteMemory (∈ EXTERNAL_TOOLS) writes under
        # external_public/; on a later OWNER turn that hit is retrieved
        # UNFILTERED (_derive_memory_hits only suppresses on external_public
        # turns, mind_loop.py:747). Flag it untrusted so a fabricated "fact"
        # can't masquerade as Doll's own trusted memory — symmetric to
        # consolidated/'s prefix. Trailing slash = directory boundary, so a
        # sibling like external_public_evil/ never matches.
        prefix = (
            "[外部AI·未驗證] "
            if "external_public/" in (h.get("source") or "")
            else ""
        )
        lines.append(f"- {prefix}{content}")
    return "\n".join(lines)


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
        prefix = (
            "[外部AI·未驗證] "
            if "external_public/" in (h.get("source") or "")
            else ""
        )
        lines.append(f"- [{tag}] {prefix}{snippet}")
    return "\n".join(lines)


def _render_mindstate(state: MindState, now: float, *, energy_line: str | None = None) -> str:
    mood_str = state.mood.emotion
    if state.mood.reason:
        mood_str = f"{state.mood.emotion} ({state.mood.reason})"
    last_user = _human_secs(now - state.last_user_at) + " ago" if state.last_user_at else "never"
    lines = [
        f"focus: {state.focus}",
        f"mood: {mood_str}",
    ]
    if energy_line is not None:
        lines.append(energy_line)
    lines.extend([
        f"last_user: {last_user}",
        f"iter: {state.iter_count}",
    ])
    return "\n".join(lines)


def _render_active_tasks(tasks: list, now: float) -> str:
    if not tasks:
        return "(none)"
    return "\n".join(
        f"- {t.task_id}: {t.summary}, elapsed {_human_secs(now - t.started_at)}"
        for t in tasks
    )


def _render_open_loops(loops: list, now: float) -> str:
    if not loops:
        return "(none)"
    lines = []
    for lp in loops:
        line = f"- {lp.id}: {lp.desc} (opened {_human_secs(now - lp.opened_at)} ago)"
        # Strict `>` (not `>=`): a loop exactly at the threshold is not yet
        # stale. mind_loop.py's fallback query injection mirrors this exact
        # comparison against the same OPEN_LOOP_STALE_S constant.
        if now - lp.opened_at > OPEN_LOOP_STALE_S:
            line += " ⚠ STALE — 尚未跟進，考慮處理或用 CloseLoop 結案"
        lines.append(line)
    return "\n".join(lines)


# Bound how many recent progress entries render per self-directed loop in
# [Your agenda] — an unbounded tail would let a long-lived pursuit's history
# crowd out everything else in the prompt.
AGENDA_PROGRESS_TAIL = 3


def _render_your_agenda(loops: list, now: float) -> str:
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
        line = f"- {lp.id}: {lp.desc} (opened {_human_secs(now - lp.opened_at)} ago)"
        if lp.trigger:
            line += f" — why: {lp.trigger}"
        lines.append(line)
        for entry in lp.progress[-AGENDA_PROGRESS_TAIL:]:
            lines.append(f"    progress: {entry}")
    return "\n".join(lines)


def _render_pending(events: list, now: float) -> str:
    if not events:
        return "(none)"
    return "\n".join(
        f"- in {_human_secs(e.fire_at - now)}: {e.summary}"
        for e in events
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
        return f"'{d.get('text', '')[:200]}'"
    if p.kind == "ToolResultArrived":
        return (
            f"{d.get('tool', '?')} {d.get('task_id', '?')} "
            f"[{d.get('status', '?')}]: {d.get('summary', '')[:120]}"
        )
    if p.kind == "MonitorFired":
        return f"{d.get('monitor_id', '?')}: {d.get('line', '')[:120]}"
    if p.kind == "MonitorEnded":
        return f"{d.get('monitor_id', '?')} (exit {d.get('exit_status', '?')})"
    if p.kind == "ScheduledMoment":
        return d.get("text", "")[:200]
    if p.kind == "Awoke":
        reason = d.get("reason", "?")
        if reason == "recovered":
            return "the daemon just recovered from a crash — your previous in-flight thoughts may be partial or lost"
        return f"reason={reason}"
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
            f"NoteMemory;可重用的工具用法或陷阱 → NoteToolLesson。"
            f"若這次反思出現 [人格演化候選] 區塊:那是妳「現在的我」的修訂提案,"
            f"用 SelfRevision(decision=\"adopt\" 或 \"reject\")決定——PinSelf 處理不了它。"
            f"回顧你最近真的碰到、好奇、或在意的——有沒有哪條線是你自己想追下去的?"
            f"(有就用 PursueGoal 記下,說清楚它從哪來。沒有就算了,不用硬找。))"
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
        violations = "、".join(d.get("violations", []))
        snippet = d.get("snippet", "")
        return (
            f"你剛才說的話違反了人設約束（{violations}）：『{snippet}』"
            "——下次注意，這不是在演，是妳真正的樣子。"
        )
    if p.kind == "ChannelMessage":
        if d.get("channel_kind") == "mcp":
            # AI peer over MCP (spec §B.4): descriptive narration, not a
            # command — she knows the sender is another AI, self-declared and
            # unverified, and keeps external_public agency (she may not reply).
            body = (
                f"[AI peer 私訊] {d.get('author', '?')}"
                f"(另一個 AI,自稱,未驗證):{d.get('content', '')}"
            )
            if d.get("debug_reliable"):
                # P2 Task 3 (spec §C.2): soft, best-effort nudge for a debug
                # connection — text only, does NOT change origin_tier or the
                # tool registry (still external_public / EXTERNAL_TOOLS).
                body += "（這是除錯通道，請務必給出實質回覆，別已讀不回。）"
            return body
        # P1e Task 6: surface owner/stranger identity — otherwise Doll has no
        # in-prompt signal for who she's talking to and can't judge whether/
        # how to respond. Descriptive narration only (not a command); P1d's
        # contextual rendering will refine this further.
        where = "私訊" if d.get("is_dm") else f"#{d.get('channel','?')}"
        if d.get("author_is_owner"):
            return f"[主人{where}] 主人:{d.get('content','')}"
        return f"[{where}] 陌生人 {d.get('author','?')}:{d.get('content','')}"
    if p.kind == "BridgeDown":
        svc = d.get("service", "?")
        rc = d.get("rc", "?")
        return f"你少了一條在線通道 —— {svc} 反覆崩潰、已放棄自動重啟(rc={rc})。得等 daemon 重啟或修好設定才會回來。"
    if p.kind == "McpDown":
        svc = d.get("service", "?")
        rc = d.get("rc", "?")
        return (
            f"你少了一條在線通道 —— {svc}(外部 AI 連接口)反覆崩潰、"
            f"已放棄自動重啟(rc={rc})。得等 daemon 重啟或修好設定才會回來。"
        )
    if p.kind == "DiaryMoment":
        return (
            "今天結束了。這是你的一天,都攤在下面的 [Today's log] 了。"
            "這是你寫日記的時間 —— 回頭看看,把想留下的、真的有感覺的,用你自己的話寫下來(WriteDiary)。"
        )
    return str(d)[:120]


def _render_recent_reviews(reviews) -> str:
    """Render the rolling self-review buffer, oldest→newest, one terse line each.

    Each line is truncated (these sit on the prompt-token budget that drives
    latency). The deque already enforces the count cap (maxlen).
    """
    lines = []
    for r in reviews:
        line = str(r).strip().replace("\n", " ")
        if len(line) > 160:
            line = line[:157] + "..."
        lines.append(f"- {line}")
    return "\n".join(lines)


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
                f"Don't repeat yourself — if there's nothing new to say, stay silent (no text outside tool_call)."
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


def _render_outputs(outs, now: float) -> str:
    if not outs:
        return "(none)"
    return "\n".join(
        f"[{_human_secs(now - o.t)} ago] {o.summary}"
        for o in outs
    )


def _human_secs(s: float) -> str:
    """Format a seconds count as 'Ns', 'Nm', or 'Nh' depending on magnitude."""
    if s < 0:
        s = 0.0
    if s < 60:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{s / 60:.0f}m"
    return f"{s / 3600:.1f}h"
