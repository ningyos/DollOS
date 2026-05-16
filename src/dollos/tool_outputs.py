"""Ephemeral file-backed store for tool outputs (Shell stdout, Subagent details).

Lifecycle: created at daemon startup with a tempdir, every tool runner
calls `write()` with the full output and gets back an ID. Doll's
`ReadToolOutput` / `GrepToolOutput` tools call `read()` / `grep()`
against the same store. `cleanup()` runs at daemon shutdown.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass
class ToolOutputSlice:
    """A slice of a stored output: zero-indexed line range, no trailing newlines."""

    output_id: str
    lines: list[str]
    start_offset: int
    end_offset: int
    total_lines: int


@dataclass
class ToolOutputMatch:
    """A grep match: line index + the matched line text."""

    line_index: int
    line: str


class ToolOutputStore:
    """File-backed store keyed by short opaque ID.

    Each tool output is written to `<root>/<id>.txt` verbatim (preserves
    trailing whitespace; line endings normalized to LF on read).
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def write(self, content: str) -> str:
        output_id = f"out-{uuid.uuid4().hex[:8]}"
        path = self._root / f"{output_id}.txt"
        path.write_text(content, encoding="utf-8")
        return output_id

    def line_count(self, output_id: str) -> int:
        path = self._path(output_id)
        text = path.read_text(encoding="utf-8")
        return _split_lines(text).__len__()

    def read(self, output_id: str, *, offset: int, limit: int) -> ToolOutputSlice:
        path = self._path(output_id)
        text = path.read_text(encoding="utf-8")
        all_lines = _split_lines(text)
        if offset < 0:
            offset = max(0, len(all_lines) + offset)
        end = min(len(all_lines), offset + max(0, limit))
        return ToolOutputSlice(
            output_id=output_id,
            lines=all_lines[offset:end],
            start_offset=offset,
            end_offset=end,
            total_lines=len(all_lines),
        )

    def grep(
        self,
        output_id: str,
        *,
        pattern: str,
        max_matches: int = 20,
    ) -> list[ToolOutputMatch]:
        regex = re.compile(pattern)
        path = self._path(output_id)
        text = path.read_text(encoding="utf-8")
        all_lines = _split_lines(text)
        out: list[ToolOutputMatch] = []
        for i, line in enumerate(all_lines):
            if regex.search(line):
                out.append(ToolOutputMatch(line_index=i, line=line))
                if len(out) >= max_matches:
                    break
        return out

    def cleanup(self) -> None:
        """Delete the entire root dir. Idempotent."""
        import shutil

        shutil.rmtree(self._root, ignore_errors=True)

    def _path(self, output_id: str) -> Path:
        # Strict allowlist: prevent path traversal via id.
        if not output_id.startswith("out-") or not output_id[4:].isalnum():
            raise ValueError(f"invalid tool output id: {output_id!r}")
        p = self._root / f"{output_id}.txt"
        if not p.exists():
            raise FileNotFoundError(f"tool output not found: {output_id}")
        return p


def _split_lines(text: str) -> list[str]:
    # splitlines() drops a final trailing newline; we want one line per logical line.
    return text.splitlines()
