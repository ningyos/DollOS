"""Memory wrapper around memsearch (Milvus Lite + ONNX bge-m3).

For the prototype, memsearch is the single source of truth.
- write_fact(text) appends a bullet to today's markdown and re-indexes that file.
- recall(query, top_k) does a semantic search.

Layout (all under root):
  <root>/shared/YYYY-MM-DD.md   ← daily markdown of bullet facts
  <root>/.milvus.db             ← Milvus Lite store (excluded from indexed paths
                                   because it lives in root, not in shared/)
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

from memsearch import MemSearch


class Memory:
    def __init__(self, root: str | Path, *, collection: str = "persistent_mind") -> None:
        self.root = Path(root)
        self.shared = self.root / "shared"
        self.shared.mkdir(parents=True, exist_ok=True)
        self._mem = MemSearch(
            paths=[str(self.shared)],
            embedding_provider="onnx",
            milvus_uri=str(self.root / "milvus.db"),
            collection=collection,
        )
        self._indexed = False
        self._writes = 0

    async def ensure_indexed(self) -> int:
        if self._indexed:
            return 0
        n = await self._mem.index()
        self._indexed = True
        return n

    async def reindex(self) -> int:
        return await self._mem.index()

    def _today_file(self) -> Path:
        today = _dt.date.today().isoformat()
        return self.shared / f"{today}.md"

    async def write_fact(self, text: str) -> Path:
        """Append a single bullet to today's markdown and re-index that file."""
        text = text.strip()
        if not text:
            return self._today_file()
        f = self._today_file()
        # Ensure leading bullet
        line = text if text.startswith("- ") else f"- {text}"
        if f.exists():
            existing = f.read_text(encoding="utf-8")
            sep = "" if existing.endswith("\n") else "\n"
            f.write_text(existing + sep + line + "\n", encoding="utf-8")
        else:
            f.write_text(line + "\n", encoding="utf-8")
        await self._mem.index_file(f)
        self._writes += 1
        return f

    async def recall(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        if not query.strip():
            return []
        return await self._mem.search(query, top_k=top_k)

    @property
    def write_count(self) -> int:
        return self._writes

    def fact_count(self) -> int:
        """Rough count of bullets across all shared/*.md."""
        total = 0
        for md in self.shared.glob("*.md"):
            for line in md.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("- "):
                    total += 1
        return total

    def close(self) -> None:
        try:
            self._mem.close()
        except Exception:
            pass


def format_hits_as_bullets(hits: list[dict[str, Any]], max_chars: int = 240) -> str:
    """Render hits as concise bullets for the [Memory context] block."""
    if not hits:
        return "(no relevant memories)"
    out: list[str] = []
    for h in hits:
        content = (h.get("content") or "").strip()
        # If content is itself a markdown bullet list, keep the most relevant bit.
        # Truncate long lines.
        content = content.replace("\r", "")
        # Take the first non-empty line (the fact most likely lives there).
        lines = [l.strip() for l in content.splitlines() if l.strip()]
        snippet = " / ".join(lines)
        if len(snippet) > max_chars:
            snippet = snippet[:max_chars] + "..."
        score = h.get("score")
        if isinstance(score, (int, float)):
            out.append(f"- ({score:.2f}) {snippet}")
        else:
            out.append(f"- {snippet}")
    return "\n".join(out)
