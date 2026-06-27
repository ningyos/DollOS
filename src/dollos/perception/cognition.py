"""CognitionWorker — Doll's awareness of her own LLM consumption.

Mind-state proprioception, parallel to ``system_pulse`` (which is body-state).
The LLM call IS Doll's thinking; this module surfaces:
- context fullness (mind room)
- last-call latency vs trailing average (thought weight)
- cumulative tokens today vs daily quota (quota / stamina)
- recent error rate (strain)

The output is a multi-line first-person `[Cognition]` block, emitted only when
a bucket transitions (or a new error appears) since the last emit.

No fake values, no fallbacks. Missing telemetry fields → omit lines.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from dollos.telemetry.llm_calls import LLMCallRecord, TelemetryRecorder

logger = logging.getLogger(__name__)


# Bucketing ------------------------------------------------------------

def bucket_mind_room(pct: float) -> str:
    if pct < 50.0:
        return "plenty"
    if pct < 75.0:
        return "room"
    if pct < 90.0:
        return "getting full"
    return "near limit"


def bucket_thought_weight(last_ms: float, avg_ms: float | None) -> str:
    """Compare last call latency to trailing average. avg None → 'steady'."""
    if avg_ms is None or avg_ms <= 0:
        return "steady"
    ratio = last_ms / avg_ms
    if ratio < 0.7:
        return "light"
    if ratio < 1.5:
        return "steady"
    if ratio < 2.0:
        return "heavy"
    return "very heavy"


def bucket_quota(pct: float) -> str:
    if pct < 25.0:
        return "fresh"
    if pct < 50.0:
        return "warmed up"
    if pct < 75.0:
        return "used"
    if pct < 90.0:
        return "strained"
    return "almost spent"


def bucket_strain(error_count: int) -> str:
    if error_count <= 0:
        return "calm"
    if error_count <= 2:
        return "minor"
    return "notable"


# Snapshot model -------------------------------------------------------

@dataclass
class CognitionSnapshot:
    taken_at: datetime
    # mind room
    context_pct: float | None = None       # last call's context_pct
    last_prompt_tokens: int | None = None
    max_context_tokens: int | None = None
    # thought weight
    last_latency_ms: float | None = None
    trailing_avg_ms: float | None = None
    # quota
    tokens_used_today: int | None = None   # prompt + completion summed
    daily_token_quota: int | None = None
    # strain
    recent_error_count: int = 0
    last_error: str | None = None

    def signature(self) -> tuple:
        mind_b = bucket_mind_room(self.context_pct) if self.context_pct is not None else None
        thought_b = (
            bucket_thought_weight(self.last_latency_ms, self.trailing_avg_ms)
            if self.last_latency_ms is not None
            else None
        )
        if self.daily_token_quota is not None and self.tokens_used_today is not None:
            quota_b = bucket_quota(self.tokens_used_today / self.daily_token_quota * 100.0)
        else:
            quota_b = None
        strain_b = bucket_strain(self.recent_error_count)
        return (mind_b, thought_b, quota_b, strain_b, self.last_error)


# Rendering ------------------------------------------------------------

def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def render_block(snap: CognitionSnapshot) -> str:
    ts = snap.taken_at.strftime("%H:%M:%S")
    lines: list[str] = [f"[Cognition {ts}]"]

    # mind room
    if snap.context_pct is not None and snap.last_prompt_tokens is not None and snap.max_context_tokens:
        b = bucket_mind_room(snap.context_pct)
        used = _fmt_tokens(snap.last_prompt_tokens)
        total = _fmt_tokens(snap.max_context_tokens)
        lines.append(f"- mind room: {b} ({used} of {total} tokens)")

    # thought weight
    if snap.last_latency_ms is not None:
        b = bucket_thought_weight(snap.last_latency_ms, snap.trailing_avg_ms)
        last_s = snap.last_latency_ms / 1000.0
        if snap.trailing_avg_ms is not None and snap.trailing_avg_ms > 0:
            avg_s = snap.trailing_avg_ms / 1000.0
            lines.append(f"- thought weight: {b} (last {last_s:.1f}s, normally {avg_s:.1f}s)")
        else:
            lines.append(f"- thought weight: {b} (last {last_s:.1f}s)")

    # quota
    if snap.daily_token_quota is not None and snap.tokens_used_today is not None:
        pct = snap.tokens_used_today / snap.daily_token_quota * 100.0
        b = bucket_quota(pct)
        used = _fmt_tokens(snap.tokens_used_today)
        cap = _fmt_tokens(snap.daily_token_quota)
        lines.append(f"- quota: {b} ({used} of {cap} today)")

    # strain
    b = bucket_strain(snap.recent_error_count)
    if snap.recent_error_count > 0:
        lines.append(f"- strain: {b} ({snap.recent_error_count} errors in last 10 calls)")
    else:
        lines.append(f"- strain: {b}")

    return "\n".join(lines)


# Worker ---------------------------------------------------------------

class CognitionWorker:
    """Computes cognition snapshots on demand from telemetry.

    Unlike SystemPulse, no background polling — ``snapshot()`` reads the
    recorder each call. Emits a block only when any bucket transitions
    (or a new error happened since last snapshot).
    """

    def __init__(
        self,
        *,
        recorder: TelemetryRecorder,
        enabled: bool = True,
        daily_token_quota: int | None = 500_000,
        max_context_tokens: int = 131_072,
        trailing_window: int = 20,
    ) -> None:
        if daily_token_quota is not None and daily_token_quota <= 0:
            raise ValueError(
                f"daily_token_quota must be > 0 or None, got {daily_token_quota!r}"
            )
        self._recorder = recorder
        self._enabled = enabled
        self._daily_quota = daily_token_quota
        self._max_ctx = max_context_tokens
        self._trailing_window = trailing_window
        self._last_emitted_sig: tuple | None = None
        self._last_seen_error_ts: float = 0.0

    def _compute(self) -> tuple[CognitionSnapshot | None, list[LLMCallRecord]]:
        records = self._recorder.read_today()
        if not records:
            return None, records

        last = records[-1]

        # tokens used today: sum prompt + completion where present
        tokens_used = 0
        any_token_seen = False
        for r in records:
            if r.prompt_tokens is not None:
                tokens_used += r.prompt_tokens
                any_token_seen = True
            if r.completion_tokens is not None:
                tokens_used += r.completion_tokens
                any_token_seen = True

        # trailing avg of latency_total_ms over last N (excluding the very last)
        prior = [r for r in records[:-1] if r.latency_total_ms is not None][-self._trailing_window:]
        trailing_avg = (
            sum(r.latency_total_ms for r in prior) / len(prior)
            if prior
            else None
        )

        # recent errors over last 10 calls
        last10 = records[-10:]
        recent_errors = [r for r in last10 if r.error]
        recent_error_count = len(recent_errors)
        # detect a "new" error: any error in last10 with ts > _last_seen_error_ts
        last_error_str: str | None = None
        if recent_errors:
            newest_err = max(recent_errors, key=lambda r: r.ts)
            last_error_str = newest_err.error
            # don't update _last_seen_error_ts here — done in snapshot() so
            # signature() can use the value too.

        snap = CognitionSnapshot(
            taken_at=datetime.fromtimestamp(last.ts),
            context_pct=last.context_pct,
            last_prompt_tokens=last.prompt_tokens,
            max_context_tokens=self._max_ctx,
            last_latency_ms=last.latency_total_ms,
            trailing_avg_ms=trailing_avg,
            tokens_used_today=tokens_used if any_token_seen else None,
            daily_token_quota=self._daily_quota,
            recent_error_count=recent_error_count,
            last_error=last_error_str,
        )
        return snap, records

    def snapshot(self) -> str | None:
        """Return a rendered `[Cognition]` block if a bucket has shifted; else None."""
        if not self._enabled:
            return None
        snap, records = self._compute()
        if snap is None:
            return None

        # Detect new-error signal: any error record newer than what we've
        # acknowledged should force an emit. Reuse records already fetched by
        # _compute() to avoid a second read_today() call.
        newest_err_ts = 0.0
        for r in records[-10:]:
            if r.error and r.ts > newest_err_ts:
                newest_err_ts = r.ts
        new_error_event = newest_err_ts > self._last_seen_error_ts

        sig = snap.signature()
        if sig == self._last_emitted_sig and not new_error_event:
            return None

        self._last_emitted_sig = sig
        if newest_err_ts > self._last_seen_error_ts:
            self._last_seen_error_ts = newest_err_ts
        return render_block(snap)
