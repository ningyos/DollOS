"""VitalsRecorder — per-turn metabolic vitals (append-only daily JSONL).

One row per turn at the energy-drain site = the (state, action, cost, state')
tuple for future RL. Code-captured only (model can never self-report effort —
spec §5 provenance rule). No fake fallback: absent ambient fields stay None;
write failure logs, never raises. Mirrors telemetry/turn_latency.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class VitalsRecord:
    ts: float
    turn_id: str | None
    tokens_total: int | None
    energy_cost: float
    energy_after: float
    cost_mode: str            # "measured" | "flat_legacy"
    # v1b ambient (Task 5); None until then
    gpu_hottest_c: float | None = None
    gpu_power_w: float | None = None
    battery_pct: float | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


class VitalsRecorder:
    def __init__(self, dir_path: Path) -> None:
        self._dir = Path(dir_path)
        self._lock = asyncio.Lock()

    async def record(self, rec: VitalsRecord) -> None:
        async with self._lock:
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
                day = datetime.fromtimestamp(rec.ts).date()
                path = self._dir / f"vitals-{day:%Y-%m-%d}.jsonl"
                with path.open("a", encoding="utf-8") as f:
                    f.write(rec.to_json())
                    f.write("\n")
            except Exception:
                logger.exception("vitals record failed (continuing)")
