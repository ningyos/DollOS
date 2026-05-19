"""PerceptionWAL — append-only JSONL log of perceptions for crash recovery.

Each line: {"seq": int, "kind": str, "t": float, "data": {...}}\\n

Workflow:
  - PerceptionQueue.put() → wal.append(perception)
  - mind_loop.iterate() processes the perceptions, then
    wal.truncate_through(last_seq) removes consumed entries.
  - Daemon startup: wal.iter_pending() yields anything left unconsumed
    from the last run; kernel pushes them back into the queue.

The WAL is intentionally simple — no fsync between writes (the OS page
cache is acceptable; the worst case is losing the last few perceptions
that arrived in the final ~ms before the crash).

NOT thread-safe. DollOS daemon is single-threaded asyncio; reentrancy
within one event loop is safe. Future thread pool callers must add
their own lock.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Iterator

from dollos.mind.mind_state import Perception

logger = logging.getLogger(__name__)


class PerceptionWAL:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Compute next seq from existing file (recovery from last run).
        self._next_seq = self._scan_max_seq() + 1

    def _scan_max_seq(self) -> int:
        if not self._path.exists():
            return 0
        max_seq = 0
        for line in self._path.read_text(encoding="utf-8").splitlines():
            try:
                obj = json.loads(line)
                if isinstance(obj, dict) and "seq" in obj:
                    s = int(obj["seq"])
                    if s > max_seq:
                        max_seq = s
            except (json.JSONDecodeError, ValueError, TypeError):
                continue
        return max_seq

    def append(self, perception: Perception) -> int:
        """Append one perception to the log. Returns the sequence id."""
        seq = self._next_seq
        self._next_seq += 1
        line = json.dumps(
            {
                "seq": seq,
                "kind": perception.kind,
                "t": perception.t,
                "data": perception.data,
            },
            ensure_ascii=False,
        )
        with self._path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        return seq

    def iter_pending(self) -> Iterator[tuple[int, Perception]]:
        """Yield (seq, perception) for every entry currently in the log."""
        if not self._path.exists():
            return
        for line in self._path.read_text(encoding="utf-8").splitlines():
            try:
                obj = json.loads(line)
                seq = int(obj["seq"])
                p = Perception(kind=obj["kind"], t=float(obj["t"]), data=obj.get("data") or {})
                yield seq, p
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                logger.warning("wal: skipping corrupt line: %s", e)
                continue

    def truncate_through(self, seq: int) -> None:
        """Remove entries with sequence id <= seq. Idempotent + crash-safe.

        Uses tmpfile + os.replace for POSIX-atomic rename — at any crash point
        either the old file is intact (no progress) or the new file is fully
        in place (truncation complete).
        """
        if not self._path.exists():
            return
        kept = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            try:
                obj = json.loads(line)
                if int(obj["seq"]) > seq:
                    kept.append(line)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
        tmp = self._path.with_suffix(".tmp")
        body = ("\n".join(kept) + "\n") if kept else ""
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, self._path)
