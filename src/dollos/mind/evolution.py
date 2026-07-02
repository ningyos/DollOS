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


def to_counter(slot: PendingSlot, *, new_text: str,
               created_ts_now: float) -> PendingSlot:
    """Doll's adopt-with-different-text replaces the current proposal with her
    counter (spec §3.4): ``awaiting_skeptic``, ``counter_round``+1,
    ``surfaced_count`` reset, ``fallback`` := the proposal she countered.
    Inherits ``created_ts`` + ``hwm_before`` from the originating pass (the
    evidence window belongs to it). ``created_ts_now`` is accepted for symmetry
    with future policies but the inherited window is authoritative."""
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


def _normalize_echo(text: str) -> str:
    """Strip surfacing markers + NFKC + punctuation + whitespace removal
    (spec §3.4 echo normalization). Marker stripping keeps an echoed old/new
    block from being mistaken for genuine new text. Punctuation removal and
    complete whitespace elimination ensure texts with identical content but
    different formatting/punctuation/spacing normalize identically."""
    from dollos.mind import surfacing_markers  # tiny module, avoids cycle
    from dollos.mind.persona_guard import _PUNCTUATION_TABLE, _WHITESPACE_RE
    for mark in surfacing_markers.ALL:
        text = text.replace(mark, " ")
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_PUNCTUATION_TABLE)
    text = _WHITESPACE_RE.sub("", text)
    return text


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
