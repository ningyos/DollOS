# Embedder-Free Memory — SQLite FTS5 + jieba (2026-06-26)

## Why

The memory backend was `memsearch` (Milvus Lite + bge-m3 ONNX dense vectors +
BM25 + RRF). That pulls in `onnxruntime`, `pymilvus`, and a ~1GB embedding model
just to retrieve from a handful of small markdown daily files. For a single-user,
on-device companion this is disproportionate weight, startup cost, and a second
heavyweight dependency tree alongside the big LLM.

**Decision**: drop the embedder entirely. Replace dense retrieval with a purely
*lexical* index — SQLite **FTS5 (BM25)** with **jieba** CJK tokenization. No
embeddings, no Milvus, no onnxruntime. Markdown daily files stay the
human-readable source of truth; the SQLite DB is a derived, disposable index.

This is **Phase 1: episodic lexical retrieval only**. It is a deliberate
capability trade: we lose semantic (synonym / paraphrase) matching in exchange
for zero model weight and instant startup. Phase 2 (below) buys back the gap
with cheap, big-LLM-side techniques — *not* by re-adding a dense index.

> No-fallback rule: if FTS5/CJK cannot do something the design needs, we surface
> it explicitly. We do **not** silently re-introduce a dense index as a fallback.

## Module

`src/dollos/memory/fts_store.py` — `class FtsMemory`. Duck-typed drop-in for the
parts of memsearch the call sites use, so call sites are unchanged.

```python
async def search(self, query, *, top_k, source_prefix=None) -> list[dict]
async def index_file(self, path) -> None     # idempotent: delete this file's rows, re-insert
async def index(self) -> None                # full rebuild: scan all markdown under configured paths
def close(self) -> None
```

Hit dicts carry the keys consumers read today (verified against
`mind_prompt._render_memory`, `tools.py` Recall date-filter, and
`associative_search`):

| key | consumer |
|---|---|
| `content` | `mind_prompt._render_memory`, `_render_associative`, `tools._format_hit` |
| `source` | `tools._hit_date` (regex `YYYY-MM-DD.md$` → date filter) |
| `heading` | `associative_search._match_axis` → `context_tags.parse_heading` |
| `chunk_hash` | `associative_search` dedupe |
| `score` | convention (higher = better); not currently sorted on by consumers |

Plus `heading_level`, `start_line`, `end_line` for parity (cheap, not load-bearing).

## SQLite store

- One DB file derived from `data.root`: `<data.root>/memory/fts.db` (config-derived,
  constructed in `kernel.build_memsearch`; never hardcoded `~`).
- One FTS5 virtual table over markdown **chunks**:

```sql
CREATE VIRTUAL TABLE chunks USING fts5(
    tokens,                 -- jieba-segmented, space-joined; the ONLY indexed column
    content    UNINDEXED,   -- original chunk text (incl. its heading line)
    source     UNINDEXED,   -- absolute markdown file path (ends YYYY-MM-DD.md for dailies)
    heading    UNINDEXED,   -- H2 heading text, sans leading "## "
    chunk_hash UNINDEXED,
    heading_level UNINDEXED,
    start_line UNINDEXED,
    end_line   UNINDEXED,
    tokenize = 'unicode61'
);
```

- **Chunking** (simple, deterministic): split each file on markdown heading lines
  (`^#{1,6}\s+`). Preamble before the first heading is a chunk with `heading=""`.
  Each section's `content` includes its heading line (matches memsearch). Sections
  whose body (heading lines removed) is < 2 chars are dropped. No size-splitting —
  daily notes are small (Phase 1).
- `chunk_hash = sha256(f"{source}:{start}:{end}:{content}")[:16]` — deterministic.
- **Idempotency**: `index_file` does `DELETE FROM chunks WHERE source=?` then
  re-inserts, so re-indexing never duplicates. `index()` does a full
  `DELETE FROM chunks` then re-scans every configured path (handles deletions too).
- Access serialized via an `asyncio.Lock`; blocking sqlite work runs in
  `asyncio.to_thread` (connection opened `check_same_thread=False`).

## CJK (load-bearing — the user writes 繁體中文)

FTS5 `unicode61` does not segment Chinese: a run of Han characters becomes one
token, so `美式` would never match inside `冰美式咖啡`. Fix: **jieba-segment both
sides**.

- **Index side**: `jieba.cut_for_search` (finer, overlapping sub-words → higher
  recall), space-joined, stored in `tokens`. unicode61 then splits on the inserted
  spaces, preserving word boundaries.
- **Query side**: `jieba.cut`, each token wrapped as an FTS5 quoted string and
  OR-combined (`"美式" OR "咖啡"`). OR (not AND) maximizes recall, which is the
  right default when replacing fuzzy semantic match. BM25 ranks best-first.
- English is unaffected: jieba leaves ASCII words intact; unicode61 case-folds
  them, so English recall stays case-insensitive.

Ordering: `ORDER BY bm25(chunks)` (ascending — most negative = best). Returned
`score = -bm25` (positive, higher = better, matching memsearch convention).

Verified prototype: 美式/咖啡/散步 recall their 中文 notes; americano/coffee recall
the English note; a nonsense query returns `[]`.

## Wiring

- `kernel.build_memsearch` returns an `FtsMemory(paths=[shared, transcripts,
  skills], db_path=data.root/memory/fts.db)`. Still creates the three dirs. Name
  kept (`build_memsearch`) to minimize churn; the 1 caller is unchanged.
- All other call sites unchanged (duck type matches): `mind_loop` search top_k=10,
  `associative_search` search, `Recall` search top_k=5, `NoteMemory`/`WriteDiary`/
  `WriteSchedule` index_file, kernel startup `index()`.
- `kernel` shutdown now calls `memsearch.close()`.
- `pyproject.toml`: removed `memsearch[onnx]` + `onnxruntime`; added `jieba` as a
  direct dependency (it was transitive via memsearch).
- `[memsearch] top_k` config section kept as-is for config compatibility.

## Phase 2 follow-ups (OUT OF SCOPE here — do NOT build now)

1. **Facts table** — a structured key/value (or subject-predicate-object) table
   for durable self/user facts, queried exactly rather than lexically. Lets
   "what's my coffee order" resolve deterministically instead of via BM25.
2. **Self-profile** — a maintained per-character profile document (mood history,
   stable preferences, relations) surfaced every turn, distinct from episodic
   recall. Grounds Self-First without relying on a recall hit landing.
3. **LLM query expansion** — before searching, have the big LLM emit a few
   synonym/related query terms (cheap, single call) to recover the paraphrase/
   synonym recall that dense embeddings used to provide — buying back the semantic
   gap *without* an embedder or a second model.
