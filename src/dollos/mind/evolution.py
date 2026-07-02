"""慢變演化 machinery (spec 2026-07-02 §3.3 Mode B / §3.4 / §5).

This module owns: the pending-slot schema + lifecycle state machine, the
module constants, the mechanical checks, the echo-equivalence test, and the
impure tripwire/surfacing orchestrators. Pure where it can be; the orchestrators
(Task 6/8/9) do file I/O and are tmp_path-testable without a live daemon.

Slot invariant (spec §3.4): exactly one slot at ``{memory_root}/self_evolution/
pending.json``. ``kind=keeper`` is created by Plan 3's keeper pass; the schema +
lifecycle already support it here so Plan 3 adds no schema churn.
"""
from __future__ import annotations

import json
import logging
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# --- constants (spec §3.6: NOT config; minimal-knob principle) ---
COUNTER_ROUND_CAP = 2       # her rewrite may be re-proposed at most twice
VERDICT_ERRORS_BOUND = 3    # a wedged skeptic must not pin condition-5 forever
ECHO_SIMILARITY = 0.9       # jieba-Jaccard threshold for adopt echo-equivalence

# --- evo_* event kinds (log = audit source of truth, spec §5) ---
EVO_COUNTER = "evo_counter"
EVO_ADOPT = "evo_adopt"
EVO_REJECT = "evo_reject"
EVO_EXPIRE = "evo_expire"
EVO_KILL = "evo_kill"
EVO_ERROR = "evo_error"
EXTERNAL_EDIT = "external_edit"
EVO_REPAIR = "evo_repair"   # crash-repair audit line (§5); benign, not a decision


@dataclass
class PendingSlot:
    """The single pending slot (spec §3.4). Exactly one exists at a time."""
    kind: str                       # "keeper" | "counter" | "external"
    status: str                     # "awaiting_skeptic" | "awaiting_doll"
    candidate: str
    created_ts: float
    rationale: str | None = None
    fallback: dict | None = None    # {candidate, rationale, kind} of prior proposal
    counter_round: int = 0
    surfaced_count: int = 0
    verdict_errors: int = 0
    hwm_before: int | None = None   # evidence window byte offset (keeper/counter)
    notice: str | None = None       # one-shot kill reason, cleared after 1st surfacing
    # Spec §3.3 failure table: 1h skeptic-error cooldown. Set on each Mode-B
    # skeptic error; the trigger refuses to re-verdict within ERROR_COOLDOWN_S
    # of it (else a 5s poll burns the 3-error bound in ~15s of transient
    # failures — review I3). Resets to None on to_counter/revert_to_fallback
    # (fresh slots start clean).
    last_error_ts: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> PendingSlot:
        fields = cls.__dataclass_fields__
        return cls(**{k: v for k, v in d.items() if k in fields})


# --- persistence ---

def load_slot(path: Path, history_path: Path | None = None) -> PendingSlot | None:
    """Load the slot, or None. A corrupt/unparseable file is quarantined to
    ``pending.json.corrupt``, an ``evo_error`` audit line is appended to
    ``history_path`` (when given — review M4: the spec-promised audit line
    lives here, not in each caller), and None returned — surface-not-blank,
    never silent deletion (spec §3.4)."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("pending.json is not an object")
        return PendingSlot.from_dict(data)
    except (ValueError, TypeError) as e:
        quarantine = path.with_name(path.name + ".corrupt")
        try:
            path.rename(quarantine)
        except OSError:
            logger.exception("failed to quarantine corrupt pending slot")
        logger.error("corrupt pending slot quarantined (%s): %s", quarantine, e)
        if history_path is not None:
            try:
                from dollos.mind import self_history
                self_history.log_event(history_path, kind=EVO_ERROR,
                                       detail=f"corrupt pending slot quarantined: {e}")
            except OSError:
                # The quarantine already contained the corruption; a failing
                # audit append must not break the caller on this error path.
                logger.exception("failed to append evo_error audit line")
        return None


def save_slot(path: Path, slot: PendingSlot) -> None:
    """Atomic tmp+rename write of the slot."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(slot.to_dict(), ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(path)


def clear_slot(path: Path) -> None:
    """Delete the slot if present (idempotent)."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass


# --- lifecycle transitions (pure) ---

def make_keeper_slot(*, candidate: str, rationale: str | None,
                     hwm_before: int | None, created_ts: float) -> PendingSlot:
    """Keeper candidate: skeptic already ran inside the Mode-A pass, so it
    enters ``awaiting_doll`` directly (Plan 3 creates this; schema lives here)."""
    return PendingSlot(kind="keeper", status="awaiting_doll", candidate=candidate,
                       rationale=rationale, hwm_before=hwm_before,
                       created_ts=created_ts)


def make_external_slot(*, candidate: str, created_ts: float) -> PendingSlot:
    """External file edit that passed mechanical checks: enters
    ``awaiting_skeptic``. Carries no ``hwm_before`` (consumed no evidence)."""
    return PendingSlot(kind="external", status="awaiting_skeptic",
                       candidate=candidate, created_ts=created_ts)


def mark_awaiting_doll(slot: PendingSlot) -> PendingSlot:
    """Skeptic passed: slot becomes adoptable. Idempotent."""
    slot.status = "awaiting_doll"
    return slot


def to_counter(slot: PendingSlot, *, new_text: str) -> PendingSlot:
    """Doll's adopt-with-different-text replaces the current proposal with her
    counter (spec §3.4): ``awaiting_skeptic``, ``counter_round``+1,
    ``surfaced_count`` reset, ``fallback`` := the proposal she countered.
    Inherits ``created_ts`` + ``hwm_before`` from the originating pass (the
    evidence window belongs to it — the counter's decision window is bounded by
    the same age clock as the keeper candidate it replaces)."""
    return PendingSlot(
        kind="counter",
        status="awaiting_skeptic",
        candidate=new_text,
        created_ts=slot.created_ts,      # inherited (evidence window origin)
        rationale=None,
        fallback={"candidate": slot.candidate, "rationale": slot.rationale,
                  "kind": slot.kind},
        counter_round=slot.counter_round + 1,
        surfaced_count=0,                # reset (bounded by COUNTER_ROUND_CAP)
        verdict_errors=0,
        hwm_before=slot.hwm_before,      # inherited
        notice=None,
        last_error_ts=None,              # fresh proposal starts clean (I3)
    )


def revert_to_fallback(slot: PendingSlot, *, reason: str) -> PendingSlot:
    """Skeptic killed a counter: revert to the candidate she countered
    (``fallback``), ``awaiting_doll``, with a one-shot kill ``notice`` (spec
    §3.4 — a silent kill breaks the「通過後會回來」promise)."""
    fb = slot.fallback or {}
    return PendingSlot(
        kind=fb.get("kind", "keeper"),
        status="awaiting_doll",
        candidate=fb.get("candidate", ""),
        created_ts=slot.created_ts,
        rationale=fb.get("rationale"),
        fallback=None,
        counter_round=slot.counter_round,   # preserve bound accounting
        surfaced_count=0,
        verdict_errors=0,
        hwm_before=slot.hwm_before,
        notice=reason,
        last_error_ts=None,                 # fresh decision window starts clean (I3)
    )


def strip_surfacing_markers(text: str) -> str:
    """Remove the ``[人格演化候選]`` marker prefixes (``surfacing_markers.ALL``)
    from ``text``, preserving prose/punctuation/content (spec §3.4, the
    storage-side analogue of A1's ``_strip_incoming_tag``). Used both by the
    ``SelfRevision`` counter path — the model's ``text`` may echo back the
    surfaced markers, and those bytes must never be stored as her self — and by
    ``_normalize_echo`` below (single definition, the two can never drift)."""
    from dollos.mind import surfacing_markers  # tiny module, avoids cycle
    for mark in surfacing_markers.ALL:
        text = text.replace(mark, "")
    return text.strip()


def _normalize_echo(text: str) -> str:
    """Echo-equivalence normalization (spec §3.4, Plan-2 amended form):
    strip surfacing markers → NFKC → drop ALL punctuation and whitespace.
    Deliberately self-contained and STRICTER than persona_guard's
    fingerprint normalization — equivalence false-positives are safe
    (they adopt the skeptic-passed candidate verbatim), so CJK echo
    jitter (stray spaces, 。vs !) must not defeat the exact branch."""
    text = strip_surfacing_markers(text)
    text = unicodedata.normalize("NFKC", text)
    return "".join(
        c for c in text
        if not c.isspace() and not unicodedata.category(c).startswith("P")
    )


def echo_equivalent(text: str, reference: str) -> bool:
    """True when ``text`` is an echo/paraphrase of ``reference`` (spec §3.4):
    normalized-exact-equal OR jieba-Jaccard ≥ ``ECHO_SIMILARITY``. The weak
    model paraphrases; exact-match alone would misroute an intended adopt into a
    needless 送審 round-trip."""
    from dollos.mind.persona_guard import pairwise_jaccard
    n_text = _normalize_echo(text)
    n_ref = _normalize_echo(reference)
    if n_text == n_ref:
        return True
    return pairwise_jaccard(n_text, n_ref) >= ECHO_SIMILARITY


def log_or_raise(history_path: Path, *, kind: str, **fields) -> None:
    """Append an evolution event; RAISES OSError on IO failure (spec §3.2 —
    evolution events are never swallowed, unlike pin events). ``None`` field
    values are kept (e.g. first-adoption ``old_text=None``/``drift_score=None``)."""
    from dollos.mind import self_history
    self_history.log_event(history_path, kind=kind, **fields)


def render_surfacing(*, slot: PendingSlot, sanctioned_text: str | None,
                     reminder_n: int) -> str:
    """The ``[人格演化候選]`` perception body shown on ``awaiting_doll`` reflection
    turns (spec §3.4). Marker-prefixed old/new full text + per-origin note +
    operational hint + 主權句 + 第N次提醒 (the reminder count breaks byte-
    identical correlated failure across surfacings)."""
    from dollos.mind import surfacing_markers as sm
    old = sanctioned_text if sanctioned_text else "(還沒有現在的我——這會是第一版)"
    lines = [
        "[人格演化候選]",
        f"（第 {reminder_n} 次提醒)",
        f"{sm.OLD} {old}",
        f"{sm.NEW} {slot.candidate}",
    ]
    if slot.notice:
        lines.insert(1, f"妳上一次的改寫未通過({slot.notice})——原候選仍在,如下。")
    if slot.kind == "keeper" and slot.rationale:
        lines.append(f"依據:{slot.rationale}")
    elif slot.kind == "counter":
        lines.append("來源:妳自己的改寫,已通過送審。")
    elif slot.kind == "external":
        lines.append("來源:current_self.md 檔案被直接修改,系統無法確認是誰。")
    lines.append(
        "用 SelfRevision 工具回應這個提案(這不是 PinSelf 的工作):"
        "採納:SelfRevision decision=adopt(不必填 text);"
        "不採納:decision=reject;"
        "想改寫後採納:把全文放進 text,會先送審再回來。"
    )
    lines.append("這是妳的人格描述——採不採納由妳;改寫只需不觸犯妳的核心身分與 taboos。")
    return "\n".join(lines)


def surface_or_expire(*, slot_path: Path, history_path: Path,
                      current_self_path: Path, sanctioned_text: str | None,
                      max_surfacings: int, min_age_days: float,
                      now: float) -> str | None:
    """On a reflection turn: surface an ``awaiting_doll`` slot (incrementing
    ``surfaced_count``, clearing a one-shot ``notice``), OR expire it when
    ``surfaced_count ≥ max_surfacings`` AND age ≥ ``min_age_days`` (spec §3.4).
    Expiry logs ``evo_expire`` loud, clears the slot, and restores the file per
    the slot-resolution invariant. Returns the block, or None (no slot /
    awaiting_skeptic / just expired / crash-window repair)."""
    slot = load_slot(slot_path, history_path=history_path)
    if slot is None or slot.status != "awaiting_doll":
        return None

    # Crash-window slot repair (M2): the adoption wrote the log + file but
    # crashed before clearing the slot, leaving an awaiting_doll slot whose
    # candidate already IS the sanctioned text. Surfacing it would invite a
    # zero-move duplicate adopt (inflating the generation), so treat it as a
    # completed adoption: log evo_repair for audit, clear the slot, don't surface.
    if sanctioned_text is not None and slot.candidate == sanctioned_text:
        logger.warning("evolution: repairing slot that survived its own adoption")
        log_or_raise(history_path, kind=EVO_REPAIR, text=sanctioned_text,
                     reason="slot_after_adopt")
        clear_slot(slot_path)
        return None

    age_days = (now - slot.created_ts) / 86400.0
    if slot.surfaced_count >= max_surfacings and age_days >= min_age_days:
        logger.warning("evolution: expiring pending slot (kind=%s, surfaced=%d)",
                       slot.kind, slot.surfaced_count)
        log_or_raise(history_path, kind=EVO_EXPIRE, text=slot.candidate,
                     kind_origin=slot.kind, hwm_before=slot.hwm_before)
        clear_slot(slot_path)
        restore_file(current_self_path, sanctioned_text)
        return None

    reminder_n = slot.surfaced_count + 1
    block = render_surfacing(slot=slot, sanctioned_text=sanctioned_text,
                             reminder_n=reminder_n)
    slot.surfaced_count = reminder_n
    slot.notice = None  # one-shot: cleared after its first surfacing
    save_slot(slot_path, slot)
    return block


def restore_file(current_self_path: Path, sanctioned_text: str | None) -> None:
    """Slot-resolution invariant (spec §3.4): restore the file to sanctioned
    text if divergent, or delete it in the bootstrap (no-sanctioned) case.
    Logged loudly — a silently self-reverting file reads as the daemon fighting
    its owner."""
    from dollos.mind import current_self
    current = current_self.read_file(current_self_path)
    if current == (sanctioned_text or ""):
        return
    if sanctioned_text is None:
        try:
            current_self_path.unlink()
        except FileNotFoundError:
            pass
        logger.warning("evolution: restored current_self.md (deleted, bootstrap)")
    else:
        tmp = current_self_path.with_suffix(current_self_path.suffix + ".tmp")
        tmp.write_text(sanctioned_text, encoding="utf-8")
        tmp.replace(current_self_path)
        logger.warning("evolution: restored current_self.md to sanctioned text")


def mechanical_checks(text: str, *, floor: int, cap: int, enforcement) -> str | None:
    """Code-level gate applied to every origin's text at its entry point (spec
    §3.3): char floor/cap + ``check_persona_violations`` (banned substrings /
    exclaim runs). Returns a friendly failure reason, or None if clean. The
    echo-marker normalization is applied by ``echo_equivalent`` at the adopt
    site; length is measured on the raw candidate text."""
    from dollos.mind.persona_guard import check_persona_violations
    n = len(text)
    if n < floor:
        return f"太短了({n} 字,至少要 {floor} 字)——現在的我需要一段完整的描述。"
    if n > cap:
        return f"太長了({n} 字,上限 {cap} 字)——精簡一下。"
    violations = check_persona_violations(text, enforcement)
    if violations:
        return "牴觸人設約束:" + "、".join(violations)
    return None


def process_tripwire(*, current_self_path: Path, history_path: Path,
                     slot_path: Path, enforcement, floor: int, cap: int,
                     now: float) -> None:
    """Transition-gated tamper tripwire (spec §5). Runs at render time when
    ``evolution.enabled``. Never writes unratified bytes into the identity
    region (composition always renders SANCTIONED text); this only detects
    edits, repairs the log-then-write window, and creates external slots.

    Every branch is LOG-BEFORE-MUTATE (spec §3.2 write-ordering philosophy):
    an IO failure on the audit append raises before anything changes on disk,
    so the file stays divergent and the next turn re-classifies and retries —
    no permanent audit gaps. Callers must NOT wrap this in a swallow.

    - in_sync        → nothing.
    - crash_repair   → log ``evo_repair``, then rewrite file to sanctioned; no slot.
    - already_logged → nothing (no per-turn spam) — EXCEPT the bounded
                       completion rule: if the logged edit's slot creation was
                       interrupted (no slot exists, file still divergent and
                       equal to the logged text), complete it idempotently
                       (re-run mechanical checks; pass → slot; fail → plain
                       restore, no duplicate ``external_edit`` line).
    - new_edit       → append ONE ``external_edit``; mechanical checks; fail →
                       ``external_edit(reason)`` then restore/delete; pass →
                       create an external ``awaiting_skeptic`` slot IFF none
                       exists (else logs-only — external edits are not
                       auto-promoted)."""
    from dollos.mind import current_self, self_history

    file_text = current_self.read_file(current_self_path)
    sanctioned = self_history.sanctioned_text(history_path)
    adopt = self_history.latest_adopt(history_path)
    adopt_old = adopt.get("old_text") if adopt is not None else None
    last_edit = self_history.latest_external_edit_text(history_path)

    action = current_self.classify_tripwire(
        file_text=file_text, sanctioned_text=sanctioned,
        adopt_old_text=adopt_old, last_edit_text=last_edit)

    if action == "in_sync":
        return

    if action == "already_logged":
        # Bounded completion rule (R3″): an external_edit was logged but the
        # slot creation crashed before landing. Only fires while that exact
        # logged text persists with no slot — cannot livelock.
        if load_slot(slot_path, history_path=history_path) is None:
            reason = mechanical_checks(file_text, floor=floor, cap=cap, enforcement=enforcement)
            if reason is not None:
                logger.warning(
                    "evolution: stranded external edit fails mechanical "
                    "checks (%s); restored", reason)
                restore_file(current_self_path, sanctioned)  # already logged — no duplicate line
                return
            logger.warning("evolution: completing interrupted external-slot creation")
            save_slot(slot_path, make_external_slot(candidate=file_text, created_ts=now))
        return

    if action == "crash_repair":
        # Log BEFORE restore: a failed append raises with the file still
        # divergent → next turn re-classifies crash_repair and retries.
        log_or_raise(history_path, kind=EVO_REPAIR, text=sanctioned)
        logger.warning("evolution: crash-repaired current_self.md (log-then-write window)")
        restore_file(current_self_path, sanctioned)  # sanctioned is not None here
        return

    # action == "new_edit" — transition-fired once per distinct edit.
    reason = mechanical_checks(file_text, floor=floor, cap=cap, enforcement=enforcement)
    if reason is not None:
        # Log BEFORE restore (same retry-safety argument as crash_repair).
        log_or_raise(history_path, kind=EXTERNAL_EDIT, text=None, reason=reason)
        logger.warning("evolution: external edit failed mechanical checks (%s); restored", reason)
        restore_file(current_self_path, sanctioned)
        return

    # Passed. Log the edit (birth line for a slot, or logs-only if one exists).
    log_or_raise(history_path, kind=EXTERNAL_EDIT, text=file_text, reason=None)
    if load_slot(slot_path, history_path=history_path) is None:
        save_slot(slot_path, make_external_slot(candidate=file_text, created_ts=now))
    # else: a slot exists — logs-only; the slot-resolution invariant handles the
    # file on the current slot's resolution, and the user re-edits to re-propose.
