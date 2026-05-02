"""Memory store: facts CRUD + hybrid retrieval over sqlite-vec + FTS5."""

import asyncio
import datetime as dt
import json
import logging
import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import sqlite_vec

from dollos.memory.embedder import Embedder

logger = logging.getLogger(__name__)


SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class Fact:
    id: int
    text: str
    character_id: str | None
    created_at: dt.datetime
    metadata: dict[str, Any]


@dataclass(frozen=True)
class FactWithScore:
    fact: Fact
    score: float


def _serialize_f32(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _row_to_fact(row: tuple) -> "Fact":
    return Fact(
        id=row[0],
        text=row[1],
        character_id=row[2],
        created_at=dt.datetime.fromisoformat(row[3]),
        metadata=json.loads(row[4]),
    )


class Memory:
    """Facts memory store.

    Sync __init__ stores config; async initialize() opens SQLite, loads
    sqlite-vec, applies schema, and synchronizes memory_meta with the
    embedder's identity. Public methods are async; SQLite work runs on the
    default executor via asyncio.to_thread.
    """

    def __init__(self, db_path: Path, embedder: Embedder) -> None:
        self._db_path = Path(db_path).expanduser()
        self._embedder = embedder
        self._conn: sqlite3.Connection | None = None

    async def initialize(self) -> None:
        await self._embedder.initialize()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await asyncio.to_thread(self._open_conn)
        await asyncio.to_thread(self._apply_schema)
        await asyncio.to_thread(self._sync_meta)

    def _open_conn(self) -> sqlite3.Connection:
        # check_same_thread=False is required because _open_conn() runs in
        # asyncio.to_thread (worker thread) but get_meta() is a sync helper
        # called from the main thread (in tests). Safe because all writes
        # are serialized through asyncio.to_thread; reads are safe.
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        try:
            conn.enable_load_extension(True)
        except AttributeError as e:
            raise RuntimeError(
                "Python sqlite3 module was built without extension loading "
                "support; cannot load sqlite-vec. Rebuild Python with "
                "--enable-loadable-sqlite-extensions or use a distribution "
                "Python that ships with it."
            ) from e
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _apply_schema(self) -> None:
        assert self._conn is not None
        # Static portion
        schema_path = Path(__file__).parent / "schema.sql"
        sql = schema_path.read_text()
        self._conn.executescript(sql)
        # Dynamic vec_facts
        dim = self._embedder.dimensions
        self._conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_facts USING vec0("
            f"  fact_id INTEGER PRIMARY KEY, "
            f"  embedding FLOAT[{dim}]"
            f")"
        )
        self._conn.commit()

    def _sync_meta(self) -> None:
        assert self._conn is not None
        configured_model = self._embedder.model_id
        configured_dim = str(self._embedder.dimensions)

        existing_model = self._get_meta_sync("embedding_model_id")
        existing_dim = self._get_meta_sync("embedding_dim")
        existing_version = self._get_meta_sync("schema_version")

        if existing_version is None:
            self._set_meta_sync("schema_version", SCHEMA_VERSION)
        elif existing_version != SCHEMA_VERSION:
            logger.warning(
                "memory schema_version is %s but code expects %s",
                existing_version,
                SCHEMA_VERSION,
            )

        if existing_model is None:
            self._set_meta_sync("embedding_model_id", configured_model)
            self._set_meta_sync("embedding_dim", configured_dim)
        else:
            if existing_model != configured_model or existing_dim != configured_dim:
                logger.warning(
                    "memory was built with model %s (dim %s) but configured "
                    "%s (dim %s); search results may be inaccurate. "
                    "Call rebuild_embeddings() to switch.",
                    existing_model,
                    existing_dim,
                    configured_model,
                    configured_dim,
                )

    def _get_meta_sync(self, key: str) -> str | None:
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT value FROM memory_meta WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def _set_meta_sync(self, key: str, value: str) -> None:
        assert self._conn is not None
        self._conn.execute(
            "INSERT INTO memory_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

    def get_meta(self, key: str) -> str | None:
        """Sync helper for tests / inspection."""
        return self._get_meta_sync(key)

    async def close(self) -> None:
        if self._conn is not None:
            await asyncio.to_thread(self._conn.close)
            self._conn = None

    # ===== Public API (later tasks) =====

    async def write(
        self,
        text: str,
        *,
        character_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        if self._conn is None:
            raise RuntimeError("Memory not initialized")
        embedding = await self._embedder.embed(text)
        return await asyncio.to_thread(
            self._write_sync, text, character_id, metadata or {}, embedding
        )

    def _write_sync(
        self,
        text: str,
        character_id: str | None,
        metadata: dict[str, Any],
        embedding: list[float],
    ) -> int:
        assert self._conn is not None
        created_at = dt.datetime.now(dt.UTC).isoformat()
        try:
            cur = self._conn.execute(
                "INSERT INTO facts(text, character_id, created_at, metadata) "
                "VALUES (?, ?, ?, ?)",
                (text, character_id, created_at, json.dumps(metadata)),
            )
            fact_id = cur.lastrowid
            assert fact_id is not None
            self._conn.execute(
                "INSERT INTO vec_facts(fact_id, embedding) VALUES (?, ?)",
                (fact_id, _serialize_f32(embedding)),
            )
            self._conn.commit()
            return fact_id
        except Exception:
            self._conn.rollback()
            raise

    async def read(self, fact_id: int) -> Fact | None:
        if self._conn is None:
            raise RuntimeError("Memory not initialized")
        return await asyncio.to_thread(self._read_sync, fact_id)

    def _read_sync(self, fact_id: int) -> Fact | None:
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT id, text, character_id, created_at, metadata "
            "FROM facts WHERE id = ?",
            (fact_id,),
        ).fetchone()
        if row is None:
            return None
        return _row_to_fact(row)

    async def delete(self, fact_id: int) -> bool:
        if self._conn is None:
            raise RuntimeError("Memory not initialized")
        return await asyncio.to_thread(self._delete_sync, fact_id)

    def _delete_sync(self, fact_id: int) -> bool:
        assert self._conn is not None
        try:
            # vec_facts is virtual and not connected via foreign key; delete
            # explicitly inside the same transaction. FTS sync is handled by
            # the AFTER DELETE trigger on facts.
            self._conn.execute("BEGIN")
            self._conn.execute("DELETE FROM vec_facts WHERE fact_id = ?", (fact_id,))
            cur = self._conn.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
            self._conn.commit()
            return cur.rowcount > 0
        except Exception:
            self._conn.rollback()
            raise

    async def search(
        self,
        query: str,
        *,
        character_id: str | None = None,
        top_k: int = 10,
        mode: Literal["vector", "fts", "hybrid"] = "hybrid",
    ) -> list[FactWithScore]:
        raise NotImplementedError("Task 7 will implement search")

    async def rebuild_embeddings(self) -> int:
        raise NotImplementedError("Task 8 will implement rebuild_embeddings")
