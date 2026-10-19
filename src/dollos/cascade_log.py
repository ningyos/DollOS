"""Cascade decision log — structured per-iter event capture for
dev observability.

Logs to data/cascade_log/{date}.jsonl via structlog. One line per
cascade iter: the model's reasoning (raw, capped) and speech, its tool
calls and their results.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

import structlog

logger = logging.getLogger(__name__)

# Reasoning is stored raw but capped; the trace keeps the full text.
_REASONING_CAP = 4000


class CascadeLogger:
    """Wraps a structlog logger to write per-iter records to JSONL."""

    def __init__(self, log_root: Path | None = None):
        self._log = structlog.get_logger("cascade")

    def start_turn(self) -> str:
        return uuid.uuid4().hex[:8]

    def log_iter(
        self,
        *,
        turn_id: str,
        iter: int,
        reasoning: str,
        speech: str,
        tool_calls: list[dict] | None = None,
        results: list | None = None,  # list[ToolResult]
        duration_ms: int | None = None,
    ) -> None:
        try:
            self._log.info(
                "cascade_iter",
                turn_id=turn_id,
                iter=iter,
                duration_ms=duration_ms,
                reasoning=reasoning[:_REASONING_CAP],
                speech=speech,
                tool_calls=[
                    {"id": tc.get("id"), "name": tc.get("name"), "args": tc.get("arguments")}
                    for tc in (tool_calls or [])
                ],
                results=[
                    {
                        "tool_name": r.tool_name,
                        "success": r.success,
                        "status": r.status,
                        "detail": (r.detail or "")[:500],
                    }
                    for r in (results or [])
                ],
            )
        except Exception:
            logger.exception("cascade log_iter failed; continuing")
