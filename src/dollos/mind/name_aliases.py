"""A1 name-alias store — Doll's self-learned wake-word nicknames.

Pure read-modify-write JSON store, mirroring self_profile.py's standalone,
unit-testable style: no indexing, no LLM, no imports from
kernel/mind_loop/attention. This sits on the read path of L0 name-wake
attention, so a missing/corrupt file must never raise — it resolves to an
empty store, logged loudly.

Base design (no ``pending``/``adopt`` state — that's the deferred D6
feature): every entry is written with ``state="active"``, ``origin="owner"``.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class AliasEntry:
    token: str
    state: str          # base design: always "active"
    origin: str          # base design: always "owner"
    added_at: float
    note: str | None = None


class NameAliasStore:
    """Read-modify-write store for the nicknames Doll has learned to answer to.

    Each mutating call re-reads the file, applies the change, and writes the
    full set back out — no in-memory cache, same idiom as self_profile.py's
    ``apply()``. ``active_tokens()`` is what wake-matching reads.
    """

    def __init__(self, path: Path):
        self._path = Path(path)

    def add(self, token: str, *, origin: str = "owner", note: str | None = None,
            now: float) -> None:
        """Idempotent: adding an existing token overwrites it in place (no
        duplicate rows)."""
        entries = self._load()
        entries[token] = AliasEntry(
            token=token, state="active", origin=origin, added_at=now, note=note,
        )
        self._save(entries)

    def remove(self, token: str) -> None:
        entries = self._load()
        entries.pop(token, None)
        self._save(entries)

    def active_tokens(self) -> frozenset[str]:
        """The set of ``state=="active"`` tokens — what wake-matching reads."""
        entries = self._load()
        return frozenset(t for t, e in entries.items() if e.state == "active")

    def _load(self) -> dict[str, AliasEntry]:
        """Missing or corrupt file -> empty store (logged, never raises) —
        this is on the read path of attention."""
        if not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            items = raw["aliases"]
            entries: dict[str, AliasEntry] = {}
            for item in items:
                entry = AliasEntry(
                    token=item["token"],
                    state=item.get("state", "active"),
                    origin=item.get("origin", "owner"),
                    added_at=item.get("added_at", 0.0),
                    note=item.get("note"),
                )
                entries[entry.token] = entry
        except Exception:
            logger.exception(
                "NameAliasStore: failed to read %s, treating as empty", self._path,
            )
            return {}
        return entries

    def _save(self, entries: dict[str, AliasEntry]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {"aliases": [asdict(e) for e in entries.values()]}
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        tmp_path.replace(self._path)
