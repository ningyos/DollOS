"""TurnLatencyRecorder — turn 級延遲遙測（append-only daily JSONL）。

每回合一筆，epoch = _llm_iterate 進入時刻。用於：Part 2 起手前讀真 chat-turn
think 大小與 first_speak 分佈；reflex A/B 驗收（spec §3.2/§7）。
無 fake fallback：模型沒回的欄位留 None；寫入失敗只 log 不 raise。
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
class TurnLatencyRecord:
    ts: float
    first_speak_ms: float | None
    think_chars: int
    speak_chars: int
    total_ms: float | None
    ttft_ms: float | None
    mode: str          # "deliberate" | "reflex"（Part 1 恆 "deliberate"）
    n_passes: int
    had_tool_call: bool
    # Whole-branch review Finding #1: turn-kind discriminator. Without this,
    # chat (UserSpoke/ChannelMessage) turns are indistinguishable in the
    # JSONL from idle Awoke / ReflectionMoment / AgendaMoment / DiaryMoment
    # turns — and Part 2's whole purpose (reading REAL chat-turn think-size
    # / first_speak distributions, spec §1.3/§3.2/§7.2) depends on being
    # able to filter them apart. Deduped + sorted `Perception.kind`s from
    # the batch that triggered this turn — self-sufficient, no join needed
    # to tell chat from non-chat.
    perception_kinds: list[str]
    # Precise join key back to the existing cascade_log / trace JSONL (same
    # turn_id minted by `_cascade_logger.start_turn()`). None when no
    # cascade_logger is wired.
    turn_id: str | None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


class TurnLatencyRecorder:
    def __init__(self, dir_path: Path) -> None:
        self._dir = Path(dir_path)
        self._lock = asyncio.Lock()

    async def record(self, rec: TurnLatencyRecord) -> None:
        async with self._lock:
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
                day = datetime.fromtimestamp(rec.ts).date()
                path = self._dir / f"turn_latency-{day:%Y-%m-%d}.jsonl"
                with path.open("a", encoding="utf-8") as f:
                    f.write(rec.to_json())
                    f.write("\n")
            except Exception:
                logger.exception("turn latency record failed (continuing)")
