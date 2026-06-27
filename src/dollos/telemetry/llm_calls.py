"""TelemetryRecorder — append-only JSONL log of every LLM call.

Persistent data substrate for cognition vitals. One JSONL file per day under
``<data.root>/telemetry/llm_calls-YYYY-MM-DD.jsonl``. Each line is a single
``LLMCallRecord`` serialised as JSON.

Rotation crosses midnight = daily quota naturally resets (the CognitionWorker
only reads today's file).

No fake fallback: when the model didn't return token counts, fields stay
``None``. Don't synthesize.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class LLMCallRecord:
    """One LLM call's accounting record.

    Fields with ``None`` mean "not reported by the backend"; render layers
    must omit lines that would expose a None.
    """

    ts: float
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ttft_ms: float | None = None
    latency_total_ms: float | None = None
    error: str | None = None
    context_pct: float | None = None
    call_purpose: str = "cascade"

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict) -> "LLMCallRecord":
        return cls(**d)


def _day_path(dir_path: Path, day: date) -> Path:
    return dir_path / f"llm_calls-{day:%Y-%m-%d}.jsonl"


class TelemetryRecorder:
    """Thread-safe, append-only daily-rotated JSONL recorder.

    `record()` is async because LLM calls are async — keeps the lock an
    asyncio.Lock rather than a threading lock (no cross-thread access in
    DollOS daemon).
    """

    def __init__(self, dir_path: Path) -> None:
        self._dir = Path(dir_path)
        self._lock = asyncio.Lock()
        # Cache parsed today records for cheap read_recent calls.
        # Source-of-truth is always the file — cache invalidates on day flip.
        self._cache_day: date | None = None
        self._cache_records: list[LLMCallRecord] = []

    async def record(self, rec: LLMCallRecord) -> None:
        """Append a record to today's JSONL file.

        Creates the directory if missing. Never raises on disk error — logs
        and continues, because telemetry must not bring down a cascade.
        """
        async with self._lock:
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
                day = datetime.fromtimestamp(rec.ts).date()
                path = _day_path(self._dir, day)
                with path.open("a", encoding="utf-8") as f:
                    f.write(rec.to_json())
                    f.write("\n")
                # Update cache if same day
                if self._cache_day == day:
                    self._cache_records.append(rec)
            except Exception:
                logger.exception("telemetry record failed (continuing)")

    def read_today(self) -> list[LLMCallRecord]:
        """Read all of today's records. Returns [] if file missing."""
        today = date.today()
        if self._cache_day != today:
            self._cache_day = today
            self._cache_records = self._load_day(today)
        return list(self._cache_records)

    def read_recent(self, seconds: float) -> list[LLMCallRecord]:
        """Records from the last `seconds` (across day boundaries if needed).

        Today is enough for current cognition needs — yesterday is not loaded.
        """
        recs = self.read_today()
        if not recs:
            return []
        cutoff = time.time() - seconds
        return [r for r in recs if r.ts >= cutoff]

    def _load_day(self, day: date) -> list[LLMCallRecord]:
        path = _day_path(self._dir, day)
        if not path.exists():
            return []
        out: list[LLMCallRecord] = []
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        out.append(LLMCallRecord.from_dict(d))
                    except (json.JSONDecodeError, TypeError):
                        logger.warning("skipping malformed telemetry line: %r", line[:200])
        except Exception:
            logger.exception("telemetry read failed")
        return out
