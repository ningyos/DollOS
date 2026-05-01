# Memory SoT Storage Layer — Design

**日期：** 2026-05-01
**狀態：** 草案（待使用者最終審閱）
**範圍：** main spec 「DollOS Pivot」§11.6 列出的 Plan 2 — daemon 端 facts memory 儲存層
**對齊主 spec：** `2026-05-01-dollos-pivot-to-computer-design.md`（§3.3 Memory SoT、§13 Open question 後端選型）

---

## §0 範圍

**Plan 2 做什麼**：在 daemon 內建立 **Facts memory** 子系統 — Doll 「她記得什麼」的儲存與檢索。

**Plan 2 不做**：
- Self-memory（preferences / habits / relations / emotional_residue）— 這些 update semantics 是 read-modify-write + decay，跟 facts 的 append-and-retrieve 完全不同，留 Plan 7（Self-First Design）一起做演化規則
- Memory 與 Conversation Engine 的整合（自動把對話寫進 memory）— Plan 5
- Memory 資料從手機端 ObjectBox + Room FTS4 遷入 — Plan 8
- 進階檢索 policy（importance / decay / recency boost）— Plan 5 整合時再依實際 access pattern 加

**Plan 2 完成後**：
- daemon 可以讀寫 / 檢索 facts
- 是 Plan 3（Inner Voice + VoM recall）的前置條件 — VoM 從 facts memory 撈出 RECALL block 給 Doll prefill

---

## §1 後端選型

**選用：sqlite-vec**（main spec §13 候選之一）

| 候選 | 結論 |
|---|---|
| **sqlite-vec** ✅ | 單一檔案、SQLite + FTS5 built-in、SQLite 工具鏈直接通、零外部 service。Plan A → B portability story 對齊（單檔即 memory volume） |
| LanceDB | 自家 dataset 目錄結構，hybrid 內建但對自用 over-engineered |
| DuckDB | OLAP 取向，agent memory 模式不貼合 |

**規模假設**：Doll 個人 memory 量級 10K–100K 筆，sqlite-vec 的 brute-force vector search（vec0 virtual table）一次掃描 < 50ms，dim 768 / 1536 都不痛。

**外部依賴新增**：`sqlite-vec`（pip）。其他全部 stdlib（sqlite3 + FTS5）。

---

## §2 Schema

### 2.1 表結構

```sql
-- 主表：facts
CREATE TABLE facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    character_id TEXT,                  -- NULL = 共用，否則綁特定角色
    created_at TEXT NOT NULL,           -- ISO 8601 UTC
    metadata TEXT NOT NULL DEFAULT '{}' -- JSON blob，存 source / conversation_id / 自訂 tag 等
);

-- meta 表：embedder 模型追蹤
CREATE TABLE memory_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- 預期 key：
--   embedding_model_id   (e.g. "bge-base-en-v1.5")
--   embedding_dim        (e.g. "768")
--   schema_version       (e.g. "1")

-- 向量索引：sqlite-vec virtual table，dim 由 embedder 動態填入
CREATE VIRTUAL TABLE vec_facts USING vec0(
    fact_id INTEGER PRIMARY KEY,
    embedding FLOAT[<dim>]
);

-- FTS5 keyword 索引
CREATE VIRTUAL TABLE facts_fts USING fts5(
    text, content='facts', content_rowid='id'
);

-- FTS 同步觸發器
CREATE TRIGGER facts_ai AFTER INSERT ON facts
  BEGIN INSERT INTO facts_fts(rowid, text) VALUES (new.id, new.text); END;
CREATE TRIGGER facts_ad AFTER DELETE ON facts
  BEGIN INSERT INTO facts_fts(facts_fts, rowid, text) VALUES('delete', old.id, old.text); END;
CREATE TRIGGER facts_au AFTER UPDATE ON facts
  BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, text) VALUES('delete', old.id, old.text);
    INSERT INTO facts_fts(rowid, text) VALUES (new.id, new.text);
  END;
```

### 2.2 欄位語意

- **`character_id`**：NULL = 任何角色都看得到；給值 = 該角色私房，其他角色 search 不到
- **`metadata`**：JSON。任何輔助欄位（`source`、`conversation_id`、`turn_id`、自訂 tag 等）都塞這裡。v1 不 promote 任何欄位到 first-class column — 等 Plan 5 整合完看實際 query pattern 再決定
- **`created_at`**：ISO 8601 UTC，純文字。SQLite 對 TEXT 排序就是時序

### 2.3 不在 schema 裡的東西（明確排除 v1）

- ❌ importance / priority 欄位 — retrieval policy 問題，後續加
- ❌ deleted_at / soft delete — hard delete only，要保留歷史走外部備份
- ❌ updated_at — facts 是 append-mostly，update 罕見
- ❌ source / conversation_id / turn_id 獨立欄位 — 全進 metadata JSON
- ❌ 多模型並存的 vec_facts 多表 — 單一 active 模型，切模型 = rebuild

---

## §3 Embedder 介面

### 3.1 ABC

```python
# daemon/src/dollos/memory/embedder.py

class Embedder(ABC):
    """Abstract embedder interface.

    Initialization is two-stage: __init__ stores config (sync), initialize()
    discovers dimensions (async). Callers MUST await initialize() before
    accessing dimensions or calling embed().
    """

    @abstractmethod
    async def initialize(self) -> None:
        """Async initialization. After this call, model_id and dimensions
        are valid and embed() may be called.
        """

    @property
    @abstractmethod
    def model_id(self) -> str:
        """Stable identifier for the embedding model (used in memory_meta).
        Available immediately after __init__ (configured statically)."""

    @property
    @abstractmethod
    def dimensions(self) -> int:
        """Vector dimensionality. Only valid after initialize()."""

    @abstractmethod
    async def embed(self, text: str) -> list[float]: ...

    @abstractmethod
    async def embed_batch(self, texts: list[str]) -> list[list[float]]: ...
```

### 3.2 Concrete 實作

| 類別 | 用途 | 來源 |
|---|---|---|
| **StubEmbedder** | 測試用。`embed(text)` 回傳由 SHA256(text) 推出的 deterministic 假向量（固定 dim，例：32）。`model_id="stub"`。`initialize()` no-op | 自製 |
| **LlamaCppEmbedder** | 對 llama.cpp `/embedding` raw endpoint。跟 Plan 1 LlamaCppAdapter 相同風格。`initialize()` 對 dummy 文字 embed 一次取得 dim | Plan 2 寫 |

llama.cpp `/embedding` 端點：
```
POST /embedding
{ "content": "text to embed" }   # 單筆
→ { "embedding": [...] }

POST /embedding
{ "content": ["t1", "t2"] }       # 批次
→ [{ "embedding": [...] }, { "embedding": [...] }]
```

`model_id` 由 TOML config 靜態設定（例：`"bge-base-en-v1.5"`），不從 server 自動推。`dimensions` 由 `initialize()` 時對 dummy 文字打一次 `/embedding` 拿 response vector 長度得到。

### 3.3 為什麼選 llama.cpp raw 而非 OpenAI-compat

- 跟 Plan 1 LLM adapter 風格一致（都用 raw endpoint）
- llama.cpp 同時支援 chat + embedding（不同 server instance），單一工具鏈
- 不需要 prefill 之類特殊機制，標準 raw endpoint 就夠

未來可加 OpenAICompatibleEmbedder、LocalONNXEmbedder 等 — 是新 plan 範圍。

---

## §4 Hybrid Retrieval — RRF

### 4.1 演算法

**Reciprocal Rank Fusion**（k=60，業界事實標準）。無 tuning 參數。

```python
def rrf_merge(
    vector_hits: list[tuple[int, float]],   # [(fact_id, distance), ...]
    fts_hits: list[tuple[int, float]],       # [(fact_id, bm25_rank), ...]
    k: int = 60,
) -> list[tuple[int, float]]:
    """Returns [(fact_id, score), ...] sorted desc."""
    scores: dict[int, float] = {}
    for rank, (fact_id, _) in enumerate(vector_hits):
        scores[fact_id] = scores.get(fact_id, 0) + 1 / (k + rank + 1)
    for rank, (fact_id, _) in enumerate(fts_hits):
        scores[fact_id] = scores.get(fact_id, 0) + 1 / (k + rank + 1)
    return sorted(scores.items(), key=lambda p: -p[1])
```

### 4.2 hybrid search 流程

`Memory.search(query, mode="hybrid")`：

1. `embedder.embed(query)` → query_vec
2. 平行：
   - vec0 query：`SELECT fact_id, distance FROM vec_facts WHERE embedding MATCH :query_vec ORDER BY distance LIMIT 50`
   - FTS query：`SELECT rowid, rank FROM facts_fts WHERE facts_fts MATCH :query LIMIT 50`
3. 兩邊分別過 character_id filter（透過 JOIN facts 表 + WHERE character_id IS NULL OR character_id = :char）
4. `rrf_merge(...)` → 取 top_k
5. 補回 facts 內容回傳

### 4.3 Mode `vector` / `fts`

- `mode="vector"`：只跑 vec0 query，按 distance 升序回傳
- `mode="fts"`：只跑 FTS5 query，按 rank 升序回傳
- 都要套同樣的 character_id filter

---

## §5 API 介面

### 5.1 公開介面

```python
# daemon/src/dollos/memory/store.py

@dataclass(frozen=True)
class Fact:
    id: int
    text: str
    character_id: str | None
    created_at: datetime
    metadata: dict[str, Any]

@dataclass(frozen=True)
class FactWithScore:
    fact: Fact
    score: float


class Memory:
    def __init__(self, db_path: Path, embedder: Embedder):
        """Sync init — store config only. Schema setup deferred to initialize()."""

    async def initialize(self) -> None:
        """Open SQLite, load extension, ensure embedder is initialized,
        apply schema (with embedder.dimensions filled into vec_facts DDL),
        sync memory_meta. MUST be called before any other method.
        """

    async def write(
        self,
        text: str,
        *,
        character_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """Insert a fact. Computes embedding, inserts into facts AND vec_facts
        atomically (transaction). Returns the new fact id."""

    async def read(self, fact_id: int) -> Fact | None:
        """Read a single fact by id."""

    async def delete(self, fact_id: int) -> bool:
        """Delete a fact. Removes from BOTH facts AND vec_facts inside a
        single transaction (vec0 virtual tables don't support cross-table
        triggers, so the dual delete is implemented in Python).
        Returns True if deleted, False if not found."""

    async def search(
        self,
        query: str,
        *,
        character_id: str | None = None,
        top_k: int = 10,
        mode: Literal["vector", "fts", "hybrid"] = "hybrid",
    ) -> list[FactWithScore]:
        """
        Returns facts ordered by relevance score (desc).

        character_id semantics:
          - None  → 只回共用 facts（character_id IS NULL）
          - 給值  → 共用 + 該角色私房
        """

    async def rebuild_embeddings(self) -> int:
        """
        重跑全 facts embedding。用於切換 embedder model 後。
        Returns 處理過的 fact 數。
        """
```

### 5.2 Memory.initialize() 行為

```python
async def initialize(self):
    1. await self.embedder.initialize()       # 拿到 dim
    2. open SQLite connection (with sqlite-vec extension loaded)
    3. apply schema (CREATE IF NOT EXISTS) — vec_facts DDL 用 embedder.dimensions
       填入 FLOAT[<dim>]；schema_version 寫入 memory_meta（首次）或比對
    4. 比對 memory_meta 的 embedding_model_id vs embedder.model_id：
       - 沒紀錄 → 寫進去（首次啟動）
       - 一致 → 正常
       - 不一致 → logger.warning(
           "memory was built with model X but configured Y; "
           "search results may be inaccurate. Call rebuild_embeddings()."
         )
    5. 比對 dim 同上邏輯
    6. 不阻擋啟動 — 使用者明確呼叫 rebuild_embeddings() 才動
```

### 5.3 Daemon 啟動順序

```python
embedder = LlamaCppEmbedder(...)          # sync 構造
memory = Memory(db_path, embedder)         # sync 構造
await memory.initialize()                  # async 整套 setup（含 embedder.initialize()）
# 然後才開 IPC server / 接受 client
```

---

## §6 配置（追加到 config.toml）

```toml
[memory]
db_path = "~/.local/share/dollos/memory.db"   # ~ 展開使用者 home

[embedder]
backend = "llamacpp"
base_url = "http://127.0.0.1:8002"            # 建議另起 llama-server with --embedding
timeout_s = 30.0
```

對應 pydantic：

```python
class MemoryConfig(BaseModel):
    db_path: Path

class EmbedderConfig(BaseModel):
    backend: Literal["llamacpp"] = "llamacpp"
    base_url: str
    timeout_s: float = 30.0

class Settings(BaseModel):
    llm: LLMConfig
    ipc: IPCConfig = ...
    log: LogConfig = ...
    memory: MemoryConfig                                # ← 新加
    embedder: EmbedderConfig                            # ← 新加
```

`db_path` 接受 `~` 開頭時 `Path.expanduser()`。父目錄不存在時 Memory 啟動時自動建立。

---

## §7 檔案結構

```
daemon/src/dollos/memory/
├── __init__.py              # exports: Memory, Fact, FactWithScore, Embedder, StubEmbedder
├── embedder.py              # Embedder ABC + StubEmbedder
├── embedder_llamacpp.py     # LlamaCppEmbedder
├── scoring.py               # rrf_merge
├── store.py                 # Memory, Fact, FactWithScore
└── schema.sql               # DDL（CREATE TABLE / VIRTUAL TABLE / TRIGGER）
```

每檔案職責單一。

---

## §8 測試策略

| 檔案 | 測試 |
|---|---|
| `tests/test_memory_embedder.py` | StubEmbedder deterministic、批次 ≡ 多次單筆、dim 一致 |
| `tests/test_memory_embedder_llamacpp.py` | respx mock POST /embedding，單筆 / 批次 / dimensions 取得 |
| `tests/test_memory_scoring.py` | rrf_merge 純函數：空入、單邊、重疊、k 邊界 |
| `tests/test_memory_store.py` | 用 `:memory:` SQLite + StubEmbedder：CRUD、character scoping、search 三 mode、rebuild |
| `tests/test_memory_e2e.py`（v2 加？或合進 store） | write 30 facts（10 共用 / 10 char_a / 10 char_b），驗證 hybrid retrieval + scoping |

`:memory:` SQLite 跟 file SQLite 行為一致，但 `sqlite-vec` extension 需要透過 `enable_load_extension` 載入。測試 fixture 會處理。

---

## §9 Non-goals（明確排除 v1）

- 不做 self-memory schema（preferences / habits / relations / emotional_residue）— Plan 7
- 不做 importance / decay / recency boost — Plan 5 整合時再依需要加
- 不做多 embedding model 並存（per-model 多 vec_facts 表）— 單一 active，切模型 = 明確 rebuild
- 不做自動 rebuild — 必須使用者呼叫 `rebuild_embeddings()`，避免大量雲 embedder token 開銷意外
- 不做 OpenAI-compatible embedder — Plan 2 只做 llama.cpp raw，OpenAI-compat 是後續 plan
- 不做 ONNX / 本地 embedder — 同上
- 不做 schema migration framework（alembic / yoyo）— 手刻簡單版本檢查
- 不做 connection pool / 多執行緒寫入優化 — 自用單 process，SQLite 預設行為夠
- 不做 backup / replication — 使用者自己備份 .db 檔
- 不做 encryption at rest — 自用機，OS 層加密足夠

---

## §10 Open Questions（留 plan 階段或後續）

- `sqlite-vec` Python binding 確切 API — Plan 階段 prototype 時驗證
- llama.cpp `/embedding` 端點對批次的 response shape — Plan 階段對真 server 確認後決定 LlamaCppEmbedder 解析邏輯
- `db_path` 預設位置適不適合（Linux XDG / Mac / Windows 跨平台）— v1 預設 Linux pattern，跨平台留後續
- FTS5 中文 tokenizer — 預設 unicode61 對中文 byte-level 切，能跑但不理想；後續可換 jieba / 自製
- **SQLite async 模式選型** — `aiosqlite`（新 dependency，async-native）還是 sync `sqlite3` 包 `asyncio.to_thread`（無新 dep）— Plan 階段二選一
- **`enable_load_extension` 可用性** — Python `sqlite3` 模組預設可能 disable extension loading（依系統 build），需 prototype 時驗證 sqlite-vec 載入路徑；fallback 是 build Python with extension support 或用替代 SQLite binding

---

## §11 Plan Task 預估（10 tasks）

> writing-plans 會展開細節。這裡只列骨架。

1. Project scaffold — 加 `memory/` 目錄、加 `sqlite-vec` 到 pyproject、更新 lock
2. `Fact` / `FactWithScore` dataclass + `Embedder` ABC + `StubEmbedder` + tests
3. `LlamaCppEmbedder` + tests（respx mock）
4. `scoring.py` rrf_merge + tests
5. Schema (`schema.sql`) + `Memory.__init__` (sync, store config) + `Memory.initialize()` (async setup, dim discovery, schema apply, meta sync)
6. `Memory.write` / `read` / `delete` + tests
7. `Memory.search` 三 mode + character scoping + tests
8. `Memory.rebuild_embeddings` + tests
9. Config 新增 `[memory]` `[embedder]` 段 + tests + 範例 config 更新
10. 整合測試（character scoping 覆蓋、hybrid 完整路徑、`Memory.initialize()` 與真 SQLite 檔互動）

---

## §12 後續 Plan 連動

- **Plan 3（Inner Voice + VoM）** 直接消費這份 API 的 `Memory.search()` 撈 RECALL 來源
- **Plan 5（Conversation Engine）** 把 `Memory.write()` 接進 turn 結束 hook，自動寫入顯著對話
- **Plan 7（Self-First Design）** 在這份 schema 旁邊加 self-memory 表 + 演化規則
- **Plan 8（資料遷移）** 從手機 ObjectBox + Room FTS4 把舊資料 import 進來，會走 `Memory.write()` API（embedder 跑一次跑滿）
