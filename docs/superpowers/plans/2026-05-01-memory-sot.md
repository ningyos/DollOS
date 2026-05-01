# Memory SoT Storage Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the daemon's facts memory subsystem — a sqlite-vec-backed store with hybrid (vector + FTS) retrieval, character-scoped privacy, and a pluggable embedder layer.

**Architecture:** SQLite (file or `:memory:`) with `sqlite-vec` extension for vector search and built-in FTS5 for keyword search. Two-stage init: sync `__init__` stores config; async `initialize()` discovers embedder dimensions and applies schema. SQL operations run on default thread via `asyncio.to_thread` (no new async-sqlite dependency). Hybrid retrieval uses Reciprocal Rank Fusion (k=60) to merge vector and FTS top-50 results.

**Tech Stack:**
- Python 3.12+
- `sqlite-vec` (new pip dep) for vec0 virtual table + KNN
- stdlib `sqlite3` for everything else (FTS5 built-in)
- `httpx` for embedder HTTP (already in)
- `pydantic` v2 for config (already in)
- `pytest` + `respx` for tests (already in)

**Spec reference:** `docs/superpowers/specs/2026-05-01-memory-sot-design.md`

---

## File Structure

```
DollOS/
├── daemon/
│   ├── pyproject.toml                                # MODIFY: add sqlite-vec
│   ├── config.example.toml                          # MODIFY: add [memory] [embedder]
│   ├── src/dollos/
│   │   ├── config.py                                 # MODIFY: MemoryConfig + EmbedderConfig
│   │   └── memory/                                   # CREATE: new package
│   │       ├── __init__.py
│   │       ├── embedder.py                           # Embedder ABC + StubEmbedder
│   │       ├── embedder_llamacpp.py                  # LlamaCppEmbedder
│   │       ├── scoring.py                            # rrf_merge
│   │       ├── store.py                              # Memory, Fact, FactWithScore
│   │       └── schema.sql                            # static DDL parts
│   └── tests/
│       ├── test_memory_embedder.py                   # StubEmbedder
│       ├── test_memory_embedder_llamacpp.py          # LlamaCppEmbedder
│       ├── test_memory_scoring.py                    # rrf_merge
│       ├── test_memory_store.py                      # Memory CRUD + search
│       └── test_memory_e2e.py                        # full integration
```

File responsibilities:
- `embedder.py` — abstract interface + a deterministic stub for tests
- `embedder_llamacpp.py` — concrete adapter to llama.cpp `/embedding` raw endpoint
- `scoring.py` — pure function: rrf_merge(vec_hits, fts_hits) → ranked
- `store.py` — `Memory` class: open SQLite, schema apply, CRUD, hybrid search, rebuild
- `schema.sql` — static DDL pieces (`facts`, `memory_meta`, `facts_fts`, triggers); `vec_facts` is built dynamically because dim is runtime-discovered

---

## Async/SQL Pattern

Decision (closes Open Question §10 of spec): **sync `sqlite3` wrapped via `asyncio.to_thread`**, no new dep. Each public Memory method is `async def`; internal helpers are sync; transitions happen at the public boundary.

```python
async def write(self, text, *, character_id=None, metadata=None) -> int:
    embedding = await self.embedder.embed(text)
    return await asyncio.to_thread(
        self._write_sync, text, character_id, metadata, embedding
    )

def _write_sync(self, text, character_id, metadata, embedding) -> int:
    # plain sqlite3 calls, single transaction
    ...
```

`enable_load_extension` requirement: documented in `daemon/README.md` Setup step (Plan 2 will not silently fail; the connection helper raises a clear error if extension loading is disabled).

---

## Task 1: Add Dependency + Create Package Skeleton

**Files:**
- Modify: `daemon/pyproject.toml`
- Create: `daemon/src/dollos/memory/__init__.py`

- [ ] **Step 1: Modify `daemon/pyproject.toml` — add `sqlite-vec` to dependencies**

Find the existing `dependencies = [...]` block and add `sqlite-vec>=0.1`:

```toml
dependencies = [
    "pydantic>=2.6",
    "httpx>=0.27",
    "websockets>=12.0",
    "sqlite-vec>=0.1",
    "tomli; python_version < '3.11'",
]
```

- [ ] **Step 2: Create `daemon/src/dollos/memory/__init__.py`** (placeholder; exports filled in later tasks)

```python
"""Memory subsystem — facts storage, embedder, hybrid retrieval."""
```

- [ ] **Step 3: Sync deps, verify install**

```bash
cd /home/progcat/Projects/DollOS/.worktrees/memory-sot/daemon
uv sync
uv run python -c "import sqlite_vec; print('sqlite-vec OK')"
```

Expected: `sqlite-vec OK`

- [ ] **Step 4: Verify existing test suite still passes**

```bash
uv run pytest -v
```

Expected: 15/15 tests still pass (Plan 1's test count).

- [ ] **Step 5: Commit**

```bash
cd /home/progcat/Projects/DollOS/.worktrees/memory-sot
git add daemon/pyproject.toml daemon/uv.lock daemon/src/dollos/memory/
git commit -m "chore(memory): add sqlite-vec dep + package skeleton"
```

---

## Task 2: Embedder ABC + StubEmbedder

**Files:**
- Create: `daemon/src/dollos/memory/embedder.py`
- Create: `daemon/tests/test_memory_embedder.py`

`StubEmbedder` is for tests — produces deterministic 32-dim vectors from SHA256 of input text. No external dependencies.

- [ ] **Step 1: Write the failing test `daemon/tests/test_memory_embedder.py`**

```python
"""Tests for Embedder ABC + StubEmbedder."""

import pytest

from dollos.memory.embedder import Embedder, StubEmbedder


@pytest.mark.asyncio
async def test_stub_initialize_idempotent():
    e = StubEmbedder()
    await e.initialize()
    await e.initialize()  # second call must not raise
    assert e.dimensions == 32
    assert e.model_id == "stub"


@pytest.mark.asyncio
async def test_stub_embed_deterministic():
    e = StubEmbedder()
    await e.initialize()
    v1 = await e.embed("hello")
    v2 = await e.embed("hello")
    assert v1 == v2
    assert len(v1) == 32
    assert all(isinstance(x, float) for x in v1)


@pytest.mark.asyncio
async def test_stub_embed_distinct_inputs_produce_distinct_vectors():
    e = StubEmbedder()
    await e.initialize()
    v1 = await e.embed("hello")
    v2 = await e.embed("world")
    assert v1 != v2


@pytest.mark.asyncio
async def test_stub_embed_batch_equals_single_calls():
    e = StubEmbedder()
    await e.initialize()
    texts = ["a", "b", "c"]
    batch = await e.embed_batch(texts)
    singles = [await e.embed(t) for t in texts]
    assert batch == singles


def test_stub_dimensions_before_initialize_raises():
    e = StubEmbedder()
    with pytest.raises(RuntimeError):
        _ = e.dimensions


def test_embedder_is_abstract():
    with pytest.raises(TypeError):
        Embedder()  # type: ignore[abstract]
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd /home/progcat/Projects/DollOS/.worktrees/memory-sot/daemon
uv run pytest tests/test_memory_embedder.py -v
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write `daemon/src/dollos/memory/embedder.py`**

```python
"""Embedder abstract interface and a deterministic stub for tests."""

import hashlib
import struct
from abc import ABC, abstractmethod


class Embedder(ABC):
    """Abstract embedder.

    Initialization is two-stage:
      1. __init__: sync, stores config; model_id is available immediately
      2. await initialize(): async, discovers dimensions from the backend

    Callers MUST await initialize() before reading dimensions or calling embed().
    """

    @abstractmethod
    async def initialize(self) -> None: ...

    @property
    @abstractmethod
    def model_id(self) -> str: ...

    @property
    @abstractmethod
    def dimensions(self) -> int: ...

    @abstractmethod
    async def embed(self, text: str) -> list[float]: ...

    @abstractmethod
    async def embed_batch(self, texts: list[str]) -> list[list[float]]: ...


class StubEmbedder(Embedder):
    """Deterministic embedder for tests.

    Hashes input text via SHA-256 and unpacks bytes as 8 IEEE-754 floats,
    repeated to fill 32 dimensions. Same text → same vector.
    """

    _DIM = 32

    def __init__(self) -> None:
        self._initialized = False

    async def initialize(self) -> None:
        self._initialized = True

    @property
    def model_id(self) -> str:
        return "stub"

    @property
    def dimensions(self) -> int:
        if not self._initialized:
            raise RuntimeError("StubEmbedder not initialized")
        return self._DIM

    async def embed(self, text: str) -> list[float]:
        if not self._initialized:
            raise RuntimeError("StubEmbedder not initialized")
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        # 32 bytes → 8 little-endian floats; tile to 32 floats
        floats_8 = list(struct.unpack("<8f", digest))
        return (floats_8 * 4)[: self._DIM]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [await self.embed(t) for t in texts]
```

- [ ] **Step 4: Run tests, verify they pass**

```bash
uv run pytest tests/test_memory_embedder.py -v
```

Expected: 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /home/progcat/Projects/DollOS/.worktrees/memory-sot
git add daemon/src/dollos/memory/embedder.py daemon/tests/test_memory_embedder.py
git commit -m "feat(memory): Embedder ABC + StubEmbedder for tests"
```

---

## Task 3: LlamaCppEmbedder

**Files:**
- Create: `daemon/src/dollos/memory/embedder_llamacpp.py`
- Create: `daemon/tests/test_memory_embedder_llamacpp.py`

Calls llama.cpp `/embedding` raw endpoint. `initialize()` does a probe call with text `"_dim_probe_"` and sets `dimensions` from the returned vector length. `model_id` is configured statically (not derived from the server).

- [ ] **Step 1: Write the failing test**

```python
"""Tests for LlamaCppEmbedder."""

import json

import httpx
import pytest
import respx

from dollos.memory.embedder_llamacpp import LlamaCppEmbedder


@pytest.mark.asyncio
async def test_initialize_sets_dimensions_via_probe():
    emb = LlamaCppEmbedder(
        base_url="http://test.local:8002",
        model_id="bge-base-en-v1.5",
        timeout_s=5.0,
    )
    with respx.mock(base_url="http://test.local:8002") as m:
        m.post("/embedding").mock(
            return_value=httpx.Response(
                200, json={"embedding": [0.1] * 768}
            )
        )
        await emb.initialize()
    assert emb.dimensions == 768
    assert emb.model_id == "bge-base-en-v1.5"


@pytest.mark.asyncio
async def test_embed_single():
    emb = LlamaCppEmbedder(
        base_url="http://test.local:8002",
        model_id="m",
        timeout_s=5.0,
    )
    with respx.mock(base_url="http://test.local:8002") as m:
        # initialize probe + actual embed share same mock
        m.post("/embedding").mock(
            return_value=httpx.Response(
                200, json={"embedding": [0.1, 0.2, 0.3]}
            )
        )
        await emb.initialize()
        v = await emb.embed("hello")
    assert v == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_embed_batch_makes_one_request_per_input():
    emb = LlamaCppEmbedder(
        base_url="http://test.local:8002",
        model_id="m",
        timeout_s=5.0,
    )
    captured_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"embedding": [0.5, 0.5, 0.5]})

    with respx.mock(base_url="http://test.local:8002") as m:
        m.post("/embedding").mock(side_effect=handler)
        await emb.initialize()
        results = await emb.embed_batch(["a", "b"])

    # 1 probe call + 2 batch calls = 3 captured
    assert len(captured_bodies) == 3
    assert captured_bodies[1]["content"] == "a"
    assert captured_bodies[2]["content"] == "b"
    assert results == [[0.5, 0.5, 0.5], [0.5, 0.5, 0.5]]


@pytest.mark.asyncio
async def test_dimensions_before_initialize_raises():
    emb = LlamaCppEmbedder(
        base_url="http://test.local:8002",
        model_id="m",
        timeout_s=5.0,
    )
    with pytest.raises(RuntimeError):
        _ = emb.dimensions
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd /home/progcat/Projects/DollOS/.worktrees/memory-sot/daemon
uv run pytest tests/test_memory_embedder_llamacpp.py -v
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

```python
"""llama.cpp /embedding raw endpoint adapter."""

import logging

import httpx

from dollos.memory.embedder import Embedder

logger = logging.getLogger(__name__)


class LlamaCppEmbedder(Embedder):
    """Adapter for self-hosted llama.cpp `/embedding` endpoint.

    The model_id is configured statically (used as identity in memory_meta).
    The server-reported dimension is discovered via a probe call in initialize().
    Per spec, batches are issued sequentially as one HTTP request per input.
    """

    def __init__(self, base_url: str, model_id: str, timeout_s: float = 30.0):
        self._base_url = base_url.rstrip("/")
        self._model_id = model_id
        self._timeout_s = timeout_s
        self._dimensions: int | None = None

    async def initialize(self) -> None:
        # Probe to discover dimensions
        v = await self._post_embedding("_dim_probe_")
        self._dimensions = len(v)
        logger.info(
            "LlamaCppEmbedder initialized: model_id=%s dim=%d",
            self._model_id,
            self._dimensions,
        )

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimensions(self) -> int:
        if self._dimensions is None:
            raise RuntimeError("LlamaCppEmbedder not initialized")
        return self._dimensions

    async def embed(self, text: str) -> list[float]:
        if self._dimensions is None:
            raise RuntimeError("LlamaCppEmbedder not initialized")
        return await self._post_embedding(text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if self._dimensions is None:
            raise RuntimeError("LlamaCppEmbedder not initialized")
        # llama.cpp /embedding batch behavior varies by version;
        # we issue one request per input for predictable behavior.
        return [await self._post_embedding(t) for t in texts]

    async def _post_embedding(self, text: str) -> list[float]:
        url = f"{self._base_url}/embedding"
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            resp = await client.post(url, json={"content": text})
            resp.raise_for_status()
            data = resp.json()
        return list(data["embedding"])
```

- [ ] **Step 4: Run tests, verify they pass**

```bash
uv run pytest tests/test_memory_embedder_llamacpp.py -v
```

Expected: 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /home/progcat/Projects/DollOS/.worktrees/memory-sot
git add daemon/src/dollos/memory/embedder_llamacpp.py daemon/tests/test_memory_embedder_llamacpp.py
git commit -m "feat(memory): LlamaCppEmbedder for /embedding raw endpoint"
```

---

## Task 4: RRF Scoring (Pure Function)

**Files:**
- Create: `daemon/src/dollos/memory/scoring.py`
- Create: `daemon/tests/test_memory_scoring.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for rrf_merge."""

from dollos.memory.scoring import rrf_merge


def test_empty_inputs_returns_empty():
    assert rrf_merge([], []) == []


def test_single_side_only_vector():
    hits = [(1, 0.1), (2, 0.2), (3, 0.3)]
    out = rrf_merge(hits, [])
    # Order preserved by rank; only vector contributes
    assert [fact_id for fact_id, _ in out] == [1, 2, 3]


def test_single_side_only_fts():
    hits = [(10, 1.0), (11, 2.0)]
    out = rrf_merge([], hits)
    assert [fact_id for fact_id, _ in out] == [10, 11]


def test_overlap_fact_gets_summed_score():
    vec = [(1, 0.0), (2, 0.0)]      # 1 ranked 0, 2 ranked 1
    fts = [(2, 0.0), (3, 0.0)]      # 2 ranked 0, 3 ranked 1
    out = rrf_merge(vec, fts, k=60)
    # fact 2 appears in both (vec rank 1, fts rank 0)
    # fact 1 appears once (vec rank 0)
    # fact 3 appears once (fts rank 1)
    # Score(1) = 1/61, Score(2) = 1/62 + 1/61, Score(3) = 1/62
    # 2 should rank highest
    assert out[0][0] == 2


def test_top_rank_in_one_beats_lower_ranks_in_both():
    # fact 1 is top in vector only
    # fact 2 is rank 5 in both
    vec = [(1, 0.0)] + [(99 + i, 0.0) for i in range(5)] + [(2, 0.0)]
    fts = [(99 + i, 0.0) for i in range(5)] + [(2, 0.0)]
    out = rrf_merge(vec, fts)
    assert out[0][0] == 1


def test_k_parameter_changes_score_magnitude_but_not_order():
    vec = [(1, 0), (2, 0), (3, 0)]
    fts = [(3, 0), (2, 0), (1, 0)]
    out_60 = rrf_merge(vec, fts, k=60)
    out_10 = rrf_merge(vec, fts, k=10)
    # All three appear in both; they should tie or differ predictably,
    # but the relative ordering of identical-rank-sums is stable.
    assert {fact_id for fact_id, _ in out_60} == {1, 2, 3}
    assert {fact_id for fact_id, _ in out_10} == {1, 2, 3}
```

- [ ] **Step 2: Run test to verify failure**

```bash
cd /home/progcat/Projects/DollOS/.worktrees/memory-sot/daemon
uv run pytest tests/test_memory_scoring.py -v
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write `daemon/src/dollos/memory/scoring.py`**

```python
"""Reciprocal Rank Fusion for hybrid retrieval scoring."""


def rrf_merge(
    vector_hits: list[tuple[int, float]],
    fts_hits: list[tuple[int, float]],
    k: int = 60,
) -> list[tuple[int, float]]:
    """Merge two ranked lists by Reciprocal Rank Fusion.

    Args:
        vector_hits: list of (fact_id, distance) ordered by ascending distance
            (i.e. best first).
        fts_hits: list of (fact_id, score) ordered best first.
        k: RRF constant. Default 60 is the industry-standard value.

    Returns:
        list of (fact_id, score) ordered by descending score.

    The score for a fact is the sum over both lists of 1 / (k + rank + 1),
    where rank is the 0-based position in each list. Facts appearing in
    only one list contribute one term.
    """
    scores: dict[int, float] = {}
    for rank, (fact_id, _) in enumerate(vector_hits):
        scores[fact_id] = scores.get(fact_id, 0.0) + 1.0 / (k + rank + 1)
    for rank, (fact_id, _) in enumerate(fts_hits):
        scores[fact_id] = scores.get(fact_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda p: -p[1])
```

- [ ] **Step 4: Run tests, verify they pass**

```bash
uv run pytest tests/test_memory_scoring.py -v
```

Expected: 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /home/progcat/Projects/DollOS/.worktrees/memory-sot
git add daemon/src/dollos/memory/scoring.py daemon/tests/test_memory_scoring.py
git commit -m "feat(memory): RRF scoring for hybrid retrieval"
```

---

## Task 5: Schema + Memory __init__/initialize

**Files:**
- Create: `daemon/src/dollos/memory/schema.sql`
- Create: `daemon/src/dollos/memory/store.py`
- Create: `daemon/tests/test_memory_store.py` (initial — only init tests; CRUD tests added in Task 6)

The static schema lives in `schema.sql`; the dynamic vec_facts CREATE happens in `Memory.initialize()` after embedder dim is known.

- [ ] **Step 1: Write `daemon/src/dollos/memory/schema.sql`**

```sql
-- Static schema. The vec_facts virtual table is created dynamically in
-- Memory.initialize() because its dimension comes from the embedder.

CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    character_id TEXT,
    created_at TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS memory_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    text, content='facts', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts
BEGIN
    INSERT INTO facts_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts
BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, text) VALUES('delete', old.id, old.text);
END;

CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts
BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, text) VALUES('delete', old.id, old.text);
    INSERT INTO facts_fts(rowid, text) VALUES (new.id, new.text);
END;
```

- [ ] **Step 2: Write the failing test `daemon/tests/test_memory_store.py`**

```python
"""Tests for Memory.__init__ and initialize()."""

from pathlib import Path

import pytest

from dollos.memory.embedder import StubEmbedder
from dollos.memory.store import Memory


@pytest.mark.asyncio
async def test_initialize_creates_schema_and_meta(tmp_path: Path):
    db_path = tmp_path / "memory.db"
    embedder = StubEmbedder()
    mem = Memory(db_path=db_path, embedder=embedder)
    await mem.initialize()

    # File exists
    assert db_path.exists()

    # memory_meta has the embedder model_id and dim
    assert mem.get_meta("embedding_model_id") == "stub"
    assert mem.get_meta("embedding_dim") == "32"
    assert mem.get_meta("schema_version") == "1"

    await mem.close()


@pytest.mark.asyncio
async def test_initialize_creates_parent_directory(tmp_path: Path):
    db_path = tmp_path / "nested" / "subdir" / "memory.db"
    mem = Memory(db_path=db_path, embedder=StubEmbedder())
    await mem.initialize()
    assert db_path.exists()
    await mem.close()


@pytest.mark.asyncio
async def test_initialize_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "memory.db"
    mem = Memory(db_path=db_path, embedder=StubEmbedder())
    await mem.initialize()
    await mem.close()

    # Reopen — should not error, meta unchanged
    mem2 = Memory(db_path=db_path, embedder=StubEmbedder())
    await mem2.initialize()
    assert mem2.get_meta("embedding_model_id") == "stub"
    await mem2.close()


@pytest.mark.asyncio
async def test_initialize_warns_on_model_mismatch(tmp_path: Path, caplog):
    db_path = tmp_path / "memory.db"
    mem = Memory(db_path=db_path, embedder=StubEmbedder())
    await mem.initialize()
    await mem.close()

    # Open with a different embedder model_id — must warn but not crash
    class OtherStub(StubEmbedder):
        @property
        def model_id(self) -> str:
            return "different-stub"

    import logging
    with caplog.at_level(logging.WARNING, logger="dollos.memory.store"):
        mem2 = Memory(db_path=db_path, embedder=OtherStub())
        await mem2.initialize()

    assert any("memory was built with model" in r.message for r in caplog.records)
    await mem2.close()


@pytest.mark.asyncio
async def test_methods_before_initialize_raise(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    with pytest.raises(RuntimeError):
        await mem.write("hello")
```

- [ ] **Step 3: Run test to verify failure**

```bash
cd /home/progcat/Projects/DollOS/.worktrees/memory-sot/daemon
uv run pytest tests/test_memory_store.py -v
```

Expected: FAIL (Memory not defined).

- [ ] **Step 4: Write `daemon/src/dollos/memory/store.py`** (Task 5 portion only — `__init__` + `initialize` + `get_meta` + `close`; CRUD methods are stubs raising NotImplementedError, filled in later tasks)

```python
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
        conn = sqlite3.connect(self._db_path)
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
        raise NotImplementedError("Task 6 will implement write")

    async def read(self, fact_id: int) -> Fact | None:
        raise NotImplementedError("Task 6 will implement read")

    async def delete(self, fact_id: int) -> bool:
        raise NotImplementedError("Task 6 will implement delete")

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
```

- [ ] **Step 5: Run tests, verify they pass**

```bash
uv run pytest tests/test_memory_store.py -v
```

Expected: 5 tests PASS.

- [ ] **Step 6: Verify full suite still passes**

```bash
uv run pytest -v
```

Expected: 30+ tests pass (15 from Plan 1 + new ones from Tasks 2–5).

- [ ] **Step 7: Commit**

```bash
cd /home/progcat/Projects/DollOS/.worktrees/memory-sot
git add daemon/src/dollos/memory/schema.sql daemon/src/dollos/memory/store.py daemon/tests/test_memory_store.py
git commit -m "feat(memory): schema + Memory init with two-stage async setup"
```

---

## Task 6: Memory.write / read / delete

**Files:**
- Modify: `daemon/src/dollos/memory/store.py` — replace the three NotImplementedError stubs
- Modify: `daemon/tests/test_memory_store.py` — append CRUD tests

- [ ] **Step 1: Append failing tests to `daemon/tests/test_memory_store.py`**

```python
@pytest.mark.asyncio
async def test_write_then_read_returns_same_fact(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()

    fact_id = await mem.write("the sky is blue", metadata={"source": "test"})
    fact = await mem.read(fact_id)

    assert fact is not None
    assert fact.id == fact_id
    assert fact.text == "the sky is blue"
    assert fact.character_id is None
    assert fact.metadata == {"source": "test"}
    assert isinstance(fact.created_at, dt.datetime)
    await mem.close()


@pytest.mark.asyncio
async def test_write_with_character_id(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()

    fact_id = await mem.write("private thought", character_id="gura")
    fact = await mem.read(fact_id)
    assert fact is not None
    assert fact.character_id == "gura"
    await mem.close()


@pytest.mark.asyncio
async def test_read_nonexistent_returns_none(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()
    assert await mem.read(99999) is None
    await mem.close()


@pytest.mark.asyncio
async def test_delete_removes_from_facts_and_vec_facts(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()

    fact_id = await mem.write("ephemeral")
    assert await mem.delete(fact_id) is True
    assert await mem.read(fact_id) is None

    # Confirm vec_facts row is gone too
    cur = mem._conn.execute("SELECT COUNT(*) FROM vec_facts WHERE fact_id = ?", (fact_id,))
    assert cur.fetchone()[0] == 0

    # Confirm FTS row is gone too (auto by trigger)
    cur = mem._conn.execute("SELECT COUNT(*) FROM facts_fts WHERE rowid = ?", (fact_id,))
    assert cur.fetchone()[0] == 0

    await mem.close()


@pytest.mark.asyncio
async def test_delete_nonexistent_returns_false(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()
    assert await mem.delete(99999) is False
    await mem.close()


@pytest.mark.asyncio
async def test_metadata_default_is_empty_dict(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()
    fact_id = await mem.write("no metadata")
    fact = await mem.read(fact_id)
    assert fact is not None
    assert fact.metadata == {}
    await mem.close()
```

Also add `import datetime as dt` at the top of `test_memory_store.py` if not already present.

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /home/progcat/Projects/DollOS/.worktrees/memory-sot/daemon
uv run pytest tests/test_memory_store.py -v
```

Expected: New tests fail with NotImplementedError.

- [ ] **Step 3: Replace the `write`, `read`, `delete` stubs in `daemon/src/dollos/memory/store.py`**

Replace these three methods (and add the helper methods below):

```python
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
```

Add this module-level helper near `_serialize_f32`:

```python
def _row_to_fact(row: tuple) -> Fact:
    return Fact(
        id=row[0],
        text=row[1],
        character_id=row[2],
        created_at=dt.datetime.fromisoformat(row[3]),
        metadata=json.loads(row[4]),
    )
```

- [ ] **Step 4: Run tests, verify they pass**

```bash
uv run pytest tests/test_memory_store.py -v
```

Expected: all CRUD tests + earlier init tests pass.

- [ ] **Step 5: Commit**

```bash
cd /home/progcat/Projects/DollOS/.worktrees/memory-sot
git add daemon/src/dollos/memory/store.py daemon/tests/test_memory_store.py
git commit -m "feat(memory): write/read/delete with vec_facts dual-ops in transaction"
```

---

## Task 7: Memory.search (vector / fts / hybrid)

**Files:**
- Modify: `daemon/src/dollos/memory/store.py` — replace `search` stub
- Modify: `daemon/tests/test_memory_store.py` — append search tests

- [ ] **Step 1: Append failing tests to `daemon/tests/test_memory_store.py`**

```python
@pytest.mark.asyncio
async def test_search_vector_mode_returns_facts(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()

    a = await mem.write("alpha")
    b = await mem.write("bravo")
    c = await mem.write("charlie")

    results = await mem.search("alpha", mode="vector", top_k=10)
    ids = {r.fact.id for r in results}
    assert {a, b, c}.issubset(ids)
    await mem.close()


@pytest.mark.asyncio
async def test_search_fts_mode_finds_keyword(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()

    target = await mem.write("the quick brown fox")
    other = await mem.write("nothing related here")

    results = await mem.search("brown fox", mode="fts", top_k=10)
    ids = [r.fact.id for r in results]
    assert target in ids
    # FTS may not return `other` at all
    await mem.close()


@pytest.mark.asyncio
async def test_search_hybrid_mode_combines_both(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()

    a = await mem.write("the quick brown fox")
    b = await mem.write("a slow green turtle")
    c = await mem.write("brown bears in the forest")

    results = await mem.search("brown", mode="hybrid", top_k=10)
    ids = {r.fact.id for r in results}
    assert a in ids and c in ids
    await mem.close()


@pytest.mark.asyncio
async def test_search_character_scope_none_excludes_private_facts(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()

    shared = await mem.write("shared knowledge")
    private = await mem.write("gura's secret", character_id="gura")

    results = await mem.search("knowledge secret", mode="hybrid", character_id=None)
    ids = {r.fact.id for r in results}
    assert shared in ids
    assert private not in ids
    await mem.close()


@pytest.mark.asyncio
async def test_search_character_scope_includes_private_for_that_character(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()

    shared = await mem.write("shared knowledge")
    private_gura = await mem.write("gura's secret", character_id="gura")
    private_rin = await mem.write("rin's secret", character_id="rin")

    results = await mem.search(
        "knowledge secret", mode="hybrid", character_id="gura"
    )
    ids = {r.fact.id for r in results}
    assert shared in ids
    assert private_gura in ids
    assert private_rin not in ids
    await mem.close()


@pytest.mark.asyncio
async def test_search_top_k_limits_results(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()
    for i in range(20):
        await mem.write(f"fact number {i}")

    results = await mem.search("fact", mode="hybrid", top_k=5)
    assert len(results) <= 5
    await mem.close()
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
cd /home/progcat/Projects/DollOS/.worktrees/memory-sot/daemon
uv run pytest tests/test_memory_store.py -v -k search
```

Expected: failures with NotImplementedError.

- [ ] **Step 3: Replace the `search` stub in `store.py`**

Add this import at the top:

```python
from dollos.memory.scoring import rrf_merge
```

Replace the `search` method:

```python
    async def search(
        self,
        query: str,
        *,
        character_id: str | None = None,
        top_k: int = 10,
        mode: Literal["vector", "fts", "hybrid"] = "hybrid",
    ) -> list[FactWithScore]:
        if self._conn is None:
            raise RuntimeError("Memory not initialized")

        if mode == "fts":
            hits = await asyncio.to_thread(
                self._fts_search, query, character_id, top_k
            )
            return await asyncio.to_thread(self._fetch_facts_with_scores, hits)

        # Both vector and hybrid need an embedding
        query_vec = await self._embedder.embed(query)

        if mode == "vector":
            hits = await asyncio.to_thread(
                self._vector_search, query_vec, character_id, top_k
            )
            return await asyncio.to_thread(self._fetch_facts_with_scores, hits)

        # hybrid
        vec_hits = await asyncio.to_thread(
            self._vector_search, query_vec, character_id, 50
        )
        fts_hits = await asyncio.to_thread(
            self._fts_search, query, character_id, 50
        )
        merged = rrf_merge(vec_hits, fts_hits)[:top_k]
        return await asyncio.to_thread(self._fetch_facts_with_scores, merged)
```

Add these private helpers in the same class:

```python
    def _vector_search(
        self,
        query_vec: list[float],
        character_id: str | None,
        limit: int,
    ) -> list[tuple[int, float]]:
        assert self._conn is not None
        scope_clause, params = _scope_clause(character_id)
        sql = (
            "SELECT v.fact_id, v.distance "
            "FROM vec_facts v JOIN facts f ON f.id = v.fact_id "
            f"WHERE v.embedding MATCH ? AND k = ? AND {scope_clause} "
            "ORDER BY v.distance"
        )
        rows = self._conn.execute(
            sql, (_serialize_f32(query_vec), limit, *params)
        ).fetchall()
        return [(int(r[0]), float(r[1])) for r in rows]

    def _fts_search(
        self,
        query: str,
        character_id: str | None,
        limit: int,
    ) -> list[tuple[int, float]]:
        assert self._conn is not None
        scope_clause, params = _scope_clause(character_id)
        sql = (
            "SELECT fts.rowid, fts.rank "
            "FROM facts_fts fts JOIN facts f ON f.id = fts.rowid "
            f"WHERE facts_fts MATCH ? AND {scope_clause} "
            "ORDER BY fts.rank LIMIT ?"
        )
        rows = self._conn.execute(sql, (query, *params, limit)).fetchall()
        return [(int(r[0]), float(r[1])) for r in rows]

    def _fetch_facts_with_scores(
        self, scored: list[tuple[int, float]]
    ) -> list[FactWithScore]:
        assert self._conn is not None
        if not scored:
            return []
        out: list[FactWithScore] = []
        for fact_id, score in scored:
            row = self._conn.execute(
                "SELECT id, text, character_id, created_at, metadata "
                "FROM facts WHERE id = ?",
                (fact_id,),
            ).fetchone()
            if row is not None:
                out.append(FactWithScore(fact=_row_to_fact(row), score=score))
        return out
```

Add this module-level helper:

```python
def _scope_clause(character_id: str | None) -> tuple[str, tuple]:
    if character_id is None:
        return "f.character_id IS NULL", ()
    return "(f.character_id IS NULL OR f.character_id = ?)", (character_id,)
```

- [ ] **Step 4: Run tests, verify they pass**

```bash
uv run pytest tests/test_memory_store.py -v
```

Expected: all search tests pass.

- [ ] **Step 5: Commit**

```bash
cd /home/progcat/Projects/DollOS/.worktrees/memory-sot
git add daemon/src/dollos/memory/store.py daemon/tests/test_memory_store.py
git commit -m "feat(memory): hybrid search (vector + FTS + RRF) with character scoping"
```

---

## Task 8: Memory.rebuild_embeddings

**Files:**
- Modify: `daemon/src/dollos/memory/store.py` — replace `rebuild_embeddings` stub
- Modify: `daemon/tests/test_memory_store.py` — append rebuild tests

- [ ] **Step 1: Append failing tests**

```python
@pytest.mark.asyncio
async def test_rebuild_returns_count(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()
    await mem.write("a")
    await mem.write("b")
    await mem.write("c")

    n = await mem.rebuild_embeddings()
    assert n == 3
    await mem.close()


@pytest.mark.asyncio
async def test_rebuild_overwrites_vec_facts(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()
    fid = await mem.write("hello world")

    # Original vector
    row = mem._conn.execute(
        "SELECT embedding FROM vec_facts WHERE fact_id = ?", (fid,)
    ).fetchone()
    original = row[0]

    # Rebuild — StubEmbedder is deterministic, so vector should be identical;
    # we still verify the row exists and is correct shape after rebuild.
    n = await mem.rebuild_embeddings()
    assert n == 1
    row = mem._conn.execute(
        "SELECT embedding FROM vec_facts WHERE fact_id = ?", (fid,)
    ).fetchone()
    assert row is not None
    assert len(row[0]) == len(original)
    await mem.close()


@pytest.mark.asyncio
async def test_rebuild_updates_meta(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()
    await mem.write("a")
    await mem.rebuild_embeddings()
    assert mem.get_meta("embedding_model_id") == "stub"
    assert mem.get_meta("embedding_dim") == "32"
    await mem.close()


@pytest.mark.asyncio
async def test_rebuild_on_empty_returns_zero(tmp_path: Path):
    mem = Memory(db_path=tmp_path / "memory.db", embedder=StubEmbedder())
    await mem.initialize()
    assert await mem.rebuild_embeddings() == 0
    await mem.close()
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
cd /home/progcat/Projects/DollOS/.worktrees/memory-sot/daemon
uv run pytest tests/test_memory_store.py -v -k rebuild
```

Expected: failures with NotImplementedError.

- [ ] **Step 3: Replace the `rebuild_embeddings` stub**

```python
    async def rebuild_embeddings(self) -> int:
        if self._conn is None:
            raise RuntimeError("Memory not initialized")

        rows = await asyncio.to_thread(
            lambda: self._conn.execute(  # type: ignore[union-attr]
                "SELECT id, text FROM facts ORDER BY id"
            ).fetchall()
        )
        if not rows:
            return 0

        ids = [r[0] for r in rows]
        texts = [r[1] for r in rows]
        new_vecs = await self._embedder.embed_batch(texts)
        await asyncio.to_thread(self._rebuild_apply_sync, ids, new_vecs)
        return len(rows)

    def _rebuild_apply_sync(
        self, ids: list[int], vecs: list[list[float]]
    ) -> None:
        assert self._conn is not None
        try:
            self._conn.execute("BEGIN")
            self._conn.execute("DELETE FROM vec_facts")
            for fid, vec in zip(ids, vecs, strict=True):
                self._conn.execute(
                    "INSERT INTO vec_facts(fact_id, embedding) VALUES (?, ?)",
                    (fid, _serialize_f32(vec)),
                )
            self._conn.commit()
            self._set_meta_sync("embedding_model_id", self._embedder.model_id)
            self._set_meta_sync("embedding_dim", str(self._embedder.dimensions))
        except Exception:
            self._conn.rollback()
            raise
```

- [ ] **Step 4: Run tests, verify they pass**

```bash
uv run pytest tests/test_memory_store.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
cd /home/progcat/Projects/DollOS/.worktrees/memory-sot
git add daemon/src/dollos/memory/store.py daemon/tests/test_memory_store.py
git commit -m "feat(memory): rebuild_embeddings for switching embedder model"
```

---

## Task 9: Config Schema + Example Update + Public Exports

**Files:**
- Modify: `daemon/src/dollos/config.py`
- Modify: `daemon/src/dollos/memory/__init__.py`
- Modify: `daemon/config.example.toml`
- Modify: `daemon/tests/test_config.py`

- [ ] **Step 1: Append failing tests to `daemon/tests/test_config.py`**

```python
def test_load_settings_includes_memory_and_embedder(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[llm]
backend = "llamacpp"
base_url = "http://127.0.0.1:8001"
model_alias = "test-model"

[ipc]
host = "127.0.0.1"
port = 9876

[log]
level = "INFO"

[memory]
db_path = "/tmp/dollos/memory.db"

[embedder]
backend = "llamacpp"
base_url = "http://127.0.0.1:8002"
model_id = "bge-base-en-v1.5"
timeout_s = 30.0
"""
    )

    settings = load_settings(config_path)

    assert str(settings.memory.db_path) == "/tmp/dollos/memory.db"
    assert settings.embedder.backend == "llamacpp"
    assert settings.embedder.base_url == "http://127.0.0.1:8002"
    assert settings.embedder.model_id == "bge-base-en-v1.5"
    assert settings.embedder.timeout_s == 30.0


def test_settings_db_path_expands_user(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[llm]
backend = "llamacpp"
base_url = "http://127.0.0.1:8001"
model_alias = "test-model"

[ipc]
host = "127.0.0.1"
port = 9876

[memory]
db_path = "~/dollos-memory.db"

[embedder]
backend = "llamacpp"
base_url = "http://127.0.0.1:8002"
model_id = "test-emb"
"""
    )
    settings = load_settings(config_path)
    assert "~" not in str(settings.memory.db_path)
    assert str(settings.memory.db_path).endswith("dollos-memory.db")
```

- [ ] **Step 2: Run tests to verify failure**

```bash
cd /home/progcat/Projects/DollOS/.worktrees/memory-sot/daemon
uv run pytest tests/test_config.py -v
```

Expected: new tests fail (no `memory` / `embedder` on Settings).

- [ ] **Step 3: Modify `daemon/src/dollos/config.py`**

Add these classes (after `LogConfig`, before `Settings`):

```python
class MemoryConfig(BaseModel):
    db_path: Path

    @field_validator("db_path", mode="before")
    @classmethod
    def _expand_user(cls, v: object) -> object:
        if isinstance(v, str):
            return Path(v).expanduser()
        return v


class EmbedderConfig(BaseModel):
    backend: Literal["llamacpp"] = "llamacpp"
    base_url: str
    model_id: str
    timeout_s: float = 30.0
```

Modify the `Settings` class to include them:

```python
class Settings(BaseModel):
    llm: LLMConfig
    ipc: IPCConfig = Field(default_factory=lambda: IPCConfig())
    log: LogConfig = Field(default_factory=lambda: LogConfig())
    memory: MemoryConfig
    embedder: EmbedderConfig
```

Add `field_validator` to imports at the top:

```python
from pydantic import BaseModel, Field, field_validator
```

- [ ] **Step 4: Modify `daemon/src/dollos/memory/__init__.py` to export public API**

```python
"""Memory subsystem — facts storage, embedder, hybrid retrieval."""

from dollos.memory.embedder import Embedder, StubEmbedder
from dollos.memory.embedder_llamacpp import LlamaCppEmbedder
from dollos.memory.store import Fact, FactWithScore, Memory

__all__ = [
    "Embedder",
    "Fact",
    "FactWithScore",
    "LlamaCppEmbedder",
    "Memory",
    "StubEmbedder",
]
```

- [ ] **Step 5: Modify `daemon/config.example.toml` — append `[memory]` and `[embedder]` sections**

Append these sections to the existing file (after the `[log]` section):

```toml

[memory]
db_path = "~/.local/share/dollos/memory.db"

[embedder]
backend = "llamacpp"
base_url = "http://127.0.0.1:8002"   # typically a separate llama-server started with --embedding
model_id = "bge-base-en-v1.5"
timeout_s = 30.0
```

- [ ] **Step 6: Run tests, verify they pass**

```bash
uv run pytest tests/test_config.py -v
```

Expected: 5 tests pass (3 existing + 2 new).

- [ ] **Step 7: Run the full suite to catch any regressions**

```bash
uv run pytest -v
```

Expected: all tests pass.

Note: any earlier `Settings(...)` constructions (e.g. in `test_e2e.py` from Plan 1) that did not provide `memory` / `embedder` fields will now fail. Fix them by adding plausible test values OR — preferred — make the Plan 1 e2e test still construct Settings with the new fields. If `tests/test_e2e.py` breaks:

Open `daemon/tests/test_e2e.py` and update the Settings construction:

```python
from pathlib import Path
from dollos.config import (
    EmbedderConfig,
    IPCConfig,
    LLMConfig,
    LogConfig,
    MemoryConfig,
    Settings,
)

settings = Settings(
    llm=LLMConfig(
        backend="llamacpp",
        base_url="http://test.local:8001",
        model_alias="mock",
    ),
    ipc=IPCConfig(host="127.0.0.1", port=0),
    log=LogConfig(level="WARNING"),
    memory=MemoryConfig(db_path=Path("/tmp/dollos-test.db")),
    embedder=EmbedderConfig(
        backend="llamacpp",
        base_url="http://test.local:8002",
        model_id="test-emb",
    ),
)
```

Re-run `uv run pytest -v`. All tests must pass.

- [ ] **Step 8: Commit**

```bash
cd /home/progcat/Projects/DollOS/.worktrees/memory-sot
git add daemon/src/dollos/config.py daemon/src/dollos/memory/__init__.py daemon/config.example.toml daemon/tests/test_config.py daemon/tests/test_e2e.py
git commit -m "feat(daemon): config sections for memory + embedder, public memory API exports"
```

---

## Task 10: End-to-End Integration Test

**Files:**
- Create: `daemon/tests/test_memory_e2e.py`

This test exercises the full memory subsystem against a real (file-backed) SQLite database with a real `StubEmbedder` (no mocks at the memory layer). Validates the full path: write 30 facts across 3 visibility tiers, verify hybrid retrieval respects character scoping and returns sensible top results.

- [ ] **Step 1: Write the failing test**

```python
"""End-to-end test: real SQLite + StubEmbedder, exercise the full Memory API."""

from pathlib import Path

import pytest

from dollos.memory import Memory, StubEmbedder


@pytest.mark.asyncio
async def test_full_memory_lifecycle(tmp_path: Path):
    db = tmp_path / "memory.db"
    mem = Memory(db_path=db, embedder=StubEmbedder())
    await mem.initialize()

    # Seed data: 10 shared, 10 char_a, 10 char_b
    shared_ids: list[int] = []
    a_ids: list[int] = []
    b_ids: list[int] = []
    for i in range(10):
        shared_ids.append(await mem.write(f"shared fact {i} about colors"))
        a_ids.append(await mem.write(f"private to a fact {i}", character_id="char_a"))
        b_ids.append(await mem.write(f"private to b fact {i}", character_id="char_b"))

    # === Scoping: None sees only shared ===
    res_none = await mem.search("fact", mode="hybrid", character_id=None, top_k=50)
    res_none_ids = {r.fact.id for r in res_none}
    assert set(shared_ids).issubset(res_none_ids)
    assert not (set(a_ids) & res_none_ids)
    assert not (set(b_ids) & res_none_ids)

    # === Scoping: char_a sees shared + a, not b ===
    res_a = await mem.search("fact", mode="hybrid", character_id="char_a", top_k=50)
    res_a_ids = {r.fact.id for r in res_a}
    assert set(shared_ids).issubset(res_a_ids)
    assert set(a_ids).issubset(res_a_ids)
    assert not (set(b_ids) & res_a_ids)

    # === Hybrid retrieval surfaces specific keyword ===
    target = await mem.write("the unique purple flamingo word")
    keyword_results = await mem.search("flamingo", mode="hybrid", top_k=5)
    assert target in {r.fact.id for r in keyword_results}

    # === Delete works end-to-end ===
    assert await mem.delete(target) is True
    assert await mem.read(target) is None

    # === Persistence: close and reopen, data still there ===
    await mem.close()
    mem2 = Memory(db_path=db, embedder=StubEmbedder())
    await mem2.initialize()
    fact = await mem2.read(shared_ids[0])
    assert fact is not None
    assert "shared fact 0" in fact.text
    await mem2.close()


@pytest.mark.asyncio
async def test_rebuild_after_reopen(tmp_path: Path):
    db = tmp_path / "memory.db"
    mem = Memory(db_path=db, embedder=StubEmbedder())
    await mem.initialize()
    for i in range(5):
        await mem.write(f"item {i}")
    await mem.close()

    mem2 = Memory(db_path=db, embedder=StubEmbedder())
    await mem2.initialize()
    n = await mem2.rebuild_embeddings()
    assert n == 5
    await mem2.close()
```

- [ ] **Step 2: Run test, verify it passes**

```bash
cd /home/progcat/Projects/DollOS/.worktrees/memory-sot/daemon
uv run pytest tests/test_memory_e2e.py -v
```

Expected: 2 tests PASS.

- [ ] **Step 3: Run the full test suite**

```bash
uv run pytest -v
```

Expected: all tests pass (Plan 1's 15 + Plan 2's new tests).

- [ ] **Step 4: Commit**

```bash
cd /home/progcat/Projects/DollOS/.worktrees/memory-sot
git add daemon/tests/test_memory_e2e.py
git commit -m "test(memory): end-to-end integration covering scoping + lifecycle + persistence"
```

---

## Done — What This Plan Produced

After all tasks complete you have:

- `dollos.memory` package: `Memory` class with `initialize / write / read / delete / search / rebuild_embeddings / close`
- Embedder abstraction: `Embedder` ABC, `StubEmbedder` (tests), `LlamaCppEmbedder` (production)
- Hybrid retrieval via Reciprocal Rank Fusion (k=60), supports `vector` / `fts` / `hybrid` modes
- Character scoping: `None` = shared only; given value = shared + that character's private facts
- sqlite-vec backed vector index + FTS5 keyword index, both auto-synced via triggers
- Schema versioned via `memory_meta`; embedder model identity tracked there with mismatch warning
- Config sections `[memory]` and `[embedder]` with `~` expansion in `db_path`
- Test coverage: ABC + StubEmbedder, LlamaCppEmbedder (mocked), RRF (pure), CRUD, search modes, scoping, rebuild, end-to-end

**What is NOT in this plan (deferred to later plans):**
- Self-memory schemas (preferences / habits / relations / emotional_residue) — Plan 7
- Conversation Engine integration (auto-write turns into memory) — Plan 5
- Phone-side ObjectBox + Room FTS4 import — Plan 8
- Importance / decay / recency boost in retrieval scoring — Plan 5 if needed
- OpenAI-compatible / ONNX embedder backends — separate plan
- Encryption at rest — out of scope (use OS-level disk encryption)

---

## Self-Review

**Spec coverage check** (each spec section → which task implements it):
- §0 scope (facts only, no self-memory) → respected throughout, Non-goals explicit at end
- §1 sqlite-vec backend → Task 1 (dep), Task 5 (load via `_open_conn`)
- §2.1 schema → Task 5 (`schema.sql` + dynamic vec_facts)
- §2.2 character_id semantics → Task 6 (write), Task 7 (search), Task 10 (e2e)
- §2.3 exclusions → no schema entries for importance/decay/etc.; metadata is JSON
- §3.1 Embedder ABC → Task 2
- §3.2 StubEmbedder → Task 2; LlamaCppEmbedder → Task 3
- §4 RRF → Task 4 (function), Task 7 (used in hybrid mode)
- §5.1 Memory API → Tasks 5–8
- §5.2 initialize() flow → Task 5
- §5.3 daemon startup order → documented in plan header (callers must `await initialize()`)
- §6 config sections → Task 9
- §7 file structure → matches plan's File Structure section exactly
- §8 testing strategy → Tasks 2/3/4/5/6/7/8/10 cover all listed test files
- §9 Non-goals → all preserved (no self-memory, no importance, no auto-rebuild, etc.)
- §10 open questions:
  - sqlite-vec API — used real syntax in Task 7 (`MATCH ? AND k = ?`)
  - llama.cpp /embedding batch shape — Task 3 sidesteps by issuing one request per input
  - db_path cross-platform — Task 9 expands `~`, parent created in initialize
  - SQLite async pattern — committed to sync + asyncio.to_thread in plan header
  - enable_load_extension availability — Task 5 raises clear error if disabled

**Placeholder scan:** searched for "TBD", "TODO", "implement later", "fill in details", "Add appropriate", "Similar to Task" — none present.

**Type consistency check:**
- `Embedder.embed → list[float]` consistent across ABC and both concrete classes
- `Memory.write → int` consistent in spec and impl
- `_serialize_f32(list[float]) → bytes` used in write, rebuild — same signature
- `_row_to_fact(tuple) → Fact` used in read, _fetch_facts_with_scores — same signature
- `_scope_clause(str | None) → tuple[str, tuple]` used in vector_search, fts_search — same shape
- `rrf_merge` parameter / return type matches scoring.py and store.py call site
- `Fact.created_at: datetime` populated via `dt.datetime.fromisoformat(...)` consistently

No inconsistencies found.
