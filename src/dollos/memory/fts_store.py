"""FtsMemory — SQLite FTS5 + jieba lexical memory backend.

Duck-typed drop-in for the parts of ``memsearch.MemSearch`` that DollOS call
sites use: ``search`` / ``index_file`` / ``index`` / ``close``. Markdown daily
files under the configured paths are the source of truth; the SQLite DB
(``<data.root>/memory/fts.db``) is a derived index.

CJK is load-bearing — the user writes 繁體中文. FTS5 ``unicode61`` does not
segment Chinese, so both the indexed content and the query are segmented with
``jieba`` (index side: ``cut_for_search`` for recall; query side: ``cut``) and
the resulting tokens drive a quoted OR-combined FTS5 MATCH expression.

See docs/superpowers/specs/2026-06-26-embedder-free-memory-design.md.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import sqlite3
from pathlib import Path

import jieba

logger = logging.getLogger(__name__)

# jieba prints its dictionary-load line to logging at INFO; quiet it.
jieba.setLogLevel(logging.WARNING)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
# Minimum meaningful body length (after removing heading lines) to index a chunk.
_MIN_MEANINGFUL_LEN = 2

_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
    tokens,
    content       UNINDEXED,
    source        UNINDEXED,
    heading       UNINDEXED,
    chunk_hash    UNINDEXED,
    heading_level UNINDEXED,
    start_line    UNINDEXED,
    end_line      UNINDEXED,
    tokenize = 'unicode61'
);
"""


class _Chunk:
    __slots__ = ("content", "source", "heading", "heading_level", "start_line", "end_line")

    def __init__(
        self,
        content: str,
        source: str,
        heading: str,
        heading_level: int,
        start_line: int,
        end_line: int,
    ) -> None:
        self.content = content
        self.source = source
        self.heading = heading
        self.heading_level = heading_level
        self.start_line = start_line
        self.end_line = end_line

    @property
    def chunk_hash(self) -> str:
        raw = f"{self.source}:{self.start_line}:{self.end_line}:{self.content}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _has_meaningful_body(section_text: str) -> bool:
    body = "\n".join(
        ln for ln in section_text.splitlines() if not _HEADING_RE.match(ln)
    ).strip()
    return len(body) >= _MIN_MEANINGFUL_LEN


def chunk_markdown(text: str, source: str) -> list[_Chunk]:
    """Split markdown into heading-delimited chunks (simple, deterministic).

    Splits on any markdown heading line (``#``..``######``). The preamble before
    the first heading becomes a chunk with ``heading=""``. Each chunk's
    ``content`` includes its heading line. Sections whose body (heading lines
    removed) is shorter than ``_MIN_MEANINGFUL_LEN`` are dropped.
    """
    lines = text.split("\n")
    heads: list[tuple[int, int, str]] = []  # (line_idx, level, title)
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m:
            heads.append((i, len(m.group(1)), m.group(2).strip()))

    sections: list[tuple[int, int, str, int]] = []  # (start, end, heading, level)
    if not heads or heads[0][0] > 0:
        end = heads[0][0] if heads else len(lines)
        sections.append((0, end, "", 0))
    for idx, (line_idx, level, title) in enumerate(heads):
        next_start = heads[idx + 1][0] if idx + 1 < len(heads) else len(lines)
        sections.append((line_idx, next_start, title, level))

    chunks: list[_Chunk] = []
    for start, end, heading, level in sections:
        section_text = "\n".join(lines[start:end]).strip()
        if not section_text or not _has_meaningful_body(section_text):
            continue
        chunks.append(
            _Chunk(
                content=section_text,
                source=source,
                heading=heading,
                heading_level=level,
                start_line=start + 1,
                end_line=end,
            )
        )
    return chunks


def _index_tokens(text: str) -> str:
    """jieba ``cut_for_search`` segmentation, space-joined, for the index side."""
    return " ".join(t.strip() for t in jieba.cut_for_search(text) if t.strip())


def _query_expr(query: str) -> str | None:
    """Build an FTS5 MATCH expression: OR of quoted jieba ``cut`` tokens.

    Each token is wrapped as an FTS5 quoted string (internal ``"`` doubled), so
    operator-like or punctuation-bearing user input cannot produce an FTS5 syntax
    error. Returns ``None`` when the query has no usable tokens.
    """
    toks = [t.strip() for t in jieba.cut(query) if t.strip()]
    if not toks:
        return None
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in toks)


def _like_prefix(prefix: str) -> str:
    """Escape LIKE wildcards in a path prefix (used with ``ESCAPE '\\'``)."""
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class FtsMemory:
    """SQLite FTS5 + jieba lexical memory store.

    Parameters
    ----------
    paths:
        Directories to scan for ``*.md`` files on ``index()``.
    db_path:
        Path to the derived SQLite DB file. Parent dirs are created.
    """

    def __init__(self, paths: list[str | Path], db_path: str | Path) -> None:
        self._paths = [Path(p) for p in paths]
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False + an asyncio.Lock lets us offload blocking
        # sqlite work to asyncio.to_thread while guaranteeing single-threaded
        # access to the connection.
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = asyncio.Lock()
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # public async interface (duck-typed against memsearch.MemSearch)      #
    # ------------------------------------------------------------------ #

    async def search(
        self,
        query: str,
        *,
        top_k: int,
        source_prefix: str | Path | None = None,
        exclude_prefixes: list[str | Path] | None = None,
    ) -> list[dict]:
        expr = _query_expr(query)
        if expr is None:
            return []
        async with self._lock:
            return await asyncio.to_thread(
                self._search_sync, expr, top_k, source_prefix, exclude_prefixes
            )

    async def index_file(self, path: str | Path) -> None:
        p = Path(path)
        async with self._lock:
            await asyncio.to_thread(self._index_file_sync, p)

    async def index(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._reindex_all_sync)

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ #
    # synchronous internals (run inside asyncio.to_thread, lock held)      #
    # ------------------------------------------------------------------ #

    def _search_sync(
        self,
        expr: str,
        top_k: int,
        source_prefix: str | Path | None,
        exclude_prefixes: list[str | Path] | None = None,
    ) -> list[dict]:
        sql = (
            "SELECT content, source, heading, chunk_hash, heading_level, "
            "start_line, end_line, bm25(chunks) AS _bm25 "
            "FROM chunks WHERE chunks MATCH ?"
        )
        params: list = [expr]
        if source_prefix is not None:
            prefix = str(Path(source_prefix).expanduser().resolve())
            sql += " AND source LIKE ? ESCAPE '\\'"
            params.append(_like_prefix(prefix) + "%")
        # P1e Task 4 (S3): real SQL-level exclusion (NOT a post-hoc Python
        # filter) — an external_public turn must not be able to retrieve
        # private-tier sources no matter how the caller shapes the query.
        # Each excluded prefix generates its own AND NOT LIKE clause (ANDed
        # together), reusing the same LIKE-metacharacter escaping as
        # source_prefix so a path containing `%`/`_` can't break the filter.
        # exclude_prefixes are always DIRECTORIES (private-tier dirs), so a
        # trailing separator makes each a proper directory-prefix match:
        # excluding ``.../shared`` must not also swallow a sibling
        # ``.../shared_backup/…``. (source_prefix above stays boundary-free —
        # it is also used with FILE paths, e.g. tool_playbook.md.)
        for exclude in exclude_prefixes or []:
            ex_prefix = str(Path(exclude).expanduser().resolve())
            sql += " AND source NOT LIKE ? ESCAPE '\\'"
            params.append(_like_prefix(ex_prefix) + "/%")
        sql += " ORDER BY _bm25 LIMIT ?"
        params.append(top_k)
        rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "content": r["content"],
                "source": r["source"],
                "heading": r["heading"],
                "chunk_hash": r["chunk_hash"],
                "heading_level": r["heading_level"],
                "start_line": r["start_line"],
                "end_line": r["end_line"],
                # memsearch convention: higher score = better. bm25 returns
                # more-negative = better, so negate for a positive ascending score.
                "score": -float(r["_bm25"]),
            }
            for r in rows
        ]

    def _index_file_sync(self, path: Path) -> None:
        source = str(path.resolve())
        self._conn.execute("DELETE FROM chunks WHERE source = ?", (source,))
        if path.exists():
            self._insert_file(path, source)
        self._conn.commit()

    def _reindex_all_sync(self) -> None:
        self._conn.execute("DELETE FROM chunks")
        for root in self._paths:
            if not root.exists():
                continue
            for md in sorted(root.rglob("*.md")):
                self._insert_file(md, str(md.resolve()))
        self._conn.commit()

    def _insert_file(self, path: Path, source: str) -> None:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            logger.warning("FtsMemory: could not read %s", path)
            return
        rows = [
            (
                _index_tokens(c.content),
                c.content,
                c.source,
                c.heading,
                c.chunk_hash,
                c.heading_level,
                c.start_line,
                c.end_line,
            )
            for c in chunk_markdown(text, source)
        ]
        if rows:
            self._conn.executemany(
                "INSERT INTO chunks("
                "tokens, content, source, heading, chunk_hash, "
                "heading_level, start_line, end_line) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
