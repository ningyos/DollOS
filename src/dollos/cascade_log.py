"""Cascade decision log — structured per-iter event capture for
dev observability.

Logs to data/cascade_log/{date}.jsonl via structlog. One line per
cascade iter, captures think fields + tool call + result.
"""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path

import structlog

logger = logging.getLogger(__name__)


_THINK_FIELD_RES = {
    "seen": re.compile(r"^SEEN:\s*(.+)$", re.MULTILINE),
    "intent": re.compile(r"^INTENT:\s*(.+)$", re.MULTILINE),
    "review": re.compile(r"^REVIEW:\s*(.+)$", re.MULTILINE),
    "mood": re.compile(r"^MOOD:\s*(.+)$", re.MULTILINE),
    "tool": re.compile(r"^TOOL:\s*(.+?)$", re.MULTILINE),
}


def _parse_think(assistant_text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for field, regex in _THINK_FIELD_RES.items():
        m = regex.search(assistant_text)
        if m:
            out[field] = m.group(1).strip()
    return out


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
        assistant_text: str,
        tool_calls: list[dict] | None = None,
        results: list | None = None,  # list[ToolResult]
        duration_ms: int | None = None,
    ) -> None:
        try:
            fields = _parse_think(assistant_text)
            self._log.info(
                "cascade_iter",
                turn_id=turn_id,
                iter=iter,
                duration_ms=duration_ms,
                **fields,
                tool_calls=[
                    {"name": tc.get("name"), "args": tc.get("arguments")}
                    for tc in (tool_calls or [])
                ],
                results=[
                    {
                        "tool_name": r.tool_name,
                        "success": r.success,
                        "detail": (r.detail or "")[:500],
                    }
                    for r in (results or [])
                ],
            )
        except Exception:
            logger.exception("cascade log_iter failed; continuing")
