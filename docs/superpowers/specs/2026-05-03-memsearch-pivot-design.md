# memsearch Pivot — Design

**日期：** 2026-05-03
**狀態：** 草案（待使用者最終審閱）
**範圍：** 砍掉 Plan 2 的 sqlite-vec / FTS5 / 自寫 Memory SoT，整個 memory 層改用 [memsearch](https://github.com/zilliztech/memsearch) 套件（Milvus Lite embedded + ONNX bge-m3 預設 + markdown SoT）。Roadmap step 3 (VoM) 的 prompt-side 工作保留 cherry-pick 過來，重寫 memory 存取部分。
**對齊主 spec：**
- `2026-05-01-dollos-pivot-to-computer-design.md`（§3 Memory SoT、§4 Inner Voice、§5 Doll Turn / VoM Prefill）
- `2026-05-03-vom-integration-design.md`（被本 spec 部分取代 — VoM 流程不變，memory 後端改）

---

## §1 範圍

**做**：
- 刪掉 `dollos.memory` 整個模組（store / scoring / schema.sql / embedder / embedder_llamacpp）
- 加 memsearch 為 dependency（pip + Milvus Lite embedded + ONNX bge-m3 預設）
- DollOS 啟動時 `await memsearch.index()`（一次性對齊 markdown ↔ Milvus）
- `data/` 為系統資料根（不存在 = fresh launch；進 .gitignore），`data/memory/shared/` 放共享 markdown，`data/memory/{character_id}/` 留給 step 10
- daily summary 模型：每天一個 `YYYY-MM-DD.md`，新事件 append 進當天檔
- `InnerVoice` 改 call `memsearch.search()`，`character_id` 簽章保留但 step 3 忽略
- 砍對應 Plan 2 測試（test_memory_*.py 全刪）
- 保留所有 prompt-side 改動（`render_blocks()` / `iv_recall.jinja` / `Qwen3PlainTemplate` / kernel handler prefill 格式 `<think>\n{recall}GOAL: `）

**不做**：
- character_id 真實啟用（step 10 才接 `source_prefix=character/`）
- 自動寫 memory（step 8 才做 — assistant turn 結束 append 當天檔）
- file watcher 背景同步（用 startup-time `index()` 即可）
- 非 ONNX embedder 選項（寫死 `embedding_provider="onnx"`）
- `memsearch.expand()` L2/L3 漸進檢索（v1 只用 L1 search）
- ONNX 模型 prefetch / 下載進度（memsearch 自己處理）
- per-character 寫入路由（step 8 + step 10 一起時再做）
- Milvus Lite 之外的 vector backend 切換（YAGNI）

**Demo**：手動 vim 編一個 `data/memory/shared/2026-05-03.md` 寫入 fact → 啟動 DollOS（`memsearch.index()` 跑完）→ 透過 IPC 問相關問題 → Doll 引用該 fact 回答。

---

## §2 系統架構

```
TextInput (user 文字)
    ↓
DollOS._handle_text_input
    ↓
  ┌─ inner_voice.recall(user_text)
  │     ├─→ memsearch.search(query, top_k=10)
  │     │       ↓ (Milvus Lite + ONNX bge-m3)
  │     │       hits = [{content, score, source}, ...]
  │     └─→ small LLM (iv_recall.jinja system + user blocks)
  │     回 "RECALL:\n- ...\n"  或  "RECALL:\n(no relevant memories)\n"
  ↓
  prefill = f"<think>\n{recall}GOAL: "
  ↓
  big LLM.stream_completion(...)
    ↓
  TextChunk → TurnEnd
```

**啟動 lifecycle**：

```python
DollOS.run():
    await self.memsearch.index()      # Milvus 對齊 markdown；SHA-256 跳過未變動
    try:
        await self.server.start()
        await self._shutdown.wait()
    finally:
        await self.server.stop()
        # memsearch 沒 close()；Milvus Lite 是 file-based
```

**character_id 對映（step 3 / step 10）**：
- step 3：InnerVoice.recall 收到 `character_id` 一律忽略，memsearch.search 不傳 `source_prefix`，搜全部 paths
- step 10：`paths=[shared_dir, active_character_dir]`；search 時 `source_prefix=f"{character_id}/"` 過濾

**data/ root**：所有系統產物的根。預設 repo-relative `data/`；可在 `[data]` config 改。`data/memory/.milvus/` 之類由 memsearch 自己管。

---

## §3 檔案改動

**刪除**：
```
src/dollos/memory/                       # 整個模組
├── __init__.py
├── embedder.py                          # Embedder ABC + StubEmbedder
├── embedder_llamacpp.py                 # LlamaCppEmbedder
├── schema.sql                           # sqlite-vec schema
├── scoring.py                           # rrf_merge
└── store.py                             # Memory class

tests/test_memory_e2e.py
tests/test_memory_embedder.py
tests/test_memory_embedder_llamacpp.py
tests/test_memory_scoring.py
tests/test_memory_store.py
```

**修改**：
```
src/dollos/
├── kernel.py                            # build_memsearch + DollOS lifecycle
├── inner_voice.py                       # MemSearch 取代 Memory
└── config.py                            # 砍 EmbedderConfig + MemoryConfig；加 DataConfig + MemsearchConfig

tests/
├── test_inner_voice.py                  # mock MemSearch
├── test_kernel_factories.py             # build_memsearch + build_inner_voice 新簽章
├── test_e2e.py                          # monkeypatch MemSearch.search/index
└── test_config.py                       # 砍 [memory]/[embedder]，加 [data]/[memsearch]

config.example.toml                       # 砍 [memory]/[embedder]，加 [data]/[memsearch]
pyproject.toml                            # 加 memsearch 依賴；砍 sqlite-vec
.gitignore                                # 加 data/
data/.gitkeep                             # NEW
docs/superpowers/specs/2026-05-03-memsearch-pivot-design.md   # 本檔
docs/superpowers/plans/2026-05-03-memsearch-pivot.md          # writing-plans 階段
```

**保留不動**：
- `src/dollos/llm/` 整個（含 `Qwen3PlainTemplate`）
- `src/dollos/prompts/` 整個（含 `render_blocks()` / `iv_recall.jinja`）
- `src/dollos/ipc/`
- prompts / llm / ipc 的所有測試

---

## §4 Config 改動

`src/dollos/config.py`：

```python
# DELETE MemoryConfig + EmbedderConfig

class DataConfig(BaseModel):
    """Root for all DollOS-generated data. data/ 不存在 = fresh launch."""
    model_config = ConfigDict(extra="forbid")

    root: Path = Path("data")

    @field_validator("root", mode="before")
    @classmethod
    def _expand_user(cls, v: object) -> object:
        if isinstance(v, str):
            return Path(v).expanduser()
        return v


class MemsearchConfig(BaseModel):
    """memsearch-specific knobs (paths derived from data.root)."""
    model_config = ConfigDict(extra="forbid")

    top_k: int = 10


class Settings(BaseModel):
    llm: LLMConfig
    ipc: IPCConfig = Field(default_factory=lambda: IPCConfig())
    log: LogConfig = Field(default_factory=lambda: LogConfig())
    data: DataConfig = Field(default_factory=lambda: DataConfig())
    memsearch: MemsearchConfig = Field(default_factory=lambda: MemsearchConfig())
    character: CharacterConfig
    inner_voice: InnerVoiceConfig
    # 砍 memory + embedder 欄位
```

`config.example.toml`：

```toml
[data]
root = "data"

[memsearch]
top_k = 10
```

設計重點：
- 不暴露 `embedding_provider`（寫死 ONNX bge-m3）
- 不暴露 Milvus 路徑（memsearch 自己管，會放在 `{data.root}/memory/.milvus/` 之類）
- `[data]` / `[memsearch]` 都有預設值，使用者可省略
- `top_k` 暴露因為這是 InnerVoice 行為，使用者可能想調

---

## §5 InnerVoice 改造

`src/dollos/inner_voice.py`：

```python
"""InnerVoice — small-model VoM RECALL block synthesizer.

Reads from memsearch (markdown SoT + Milvus shadow index) and uses a
small LLM to filter / synthesize a RECALL block. Pure utility — no
state, no event handling, no writes.

Prompt content lives in `dollos/prompts/templates/iv_recall.jinja`.
"""

from memsearch import MemSearch

from dollos.llm.adapter import LLMAdapter
from dollos.prompts import PromptRenderer


class InnerVoice:
    """Synthesize VoM RECALL blocks from memsearch using a small LLM."""

    def __init__(
        self,
        memsearch: MemSearch,
        llm: LLMAdapter,
        renderer: PromptRenderer,
        default_top_k: int = 10,
    ) -> None:
        self._memsearch = memsearch
        self._llm = llm
        self._renderer = renderer
        self._default_top_k = default_top_k

    async def recall(
        self,
        query: str,
        *,
        character_id: str | None = None,    # ignored in step 3; reserved for step 10
        top_k: int | None = None,
    ) -> str:
        """Return a RECALL block string for the given query.

        Always starts with "RECALL:\\n" so the caller can embed verbatim
        into a Doll prefill.

        If memsearch returns no hits, returns
        "RECALL:\\n(no relevant memories)\\n" without invoking the LLM.
        """
        k = top_k if top_k is not None else self._default_top_k
        hits = await self._memsearch.search(query, top_k=k)
        if not hits:
            return "RECALL:\n(no relevant memories)\n"

        candidates = "\n".join(
            f"{i + 1}. {h['content']}" for i, h in enumerate(hits)
        )
        blocks = self._renderer.render_blocks(
            "iv_recall", query=query, candidates=candidates
        )

        chunks: list[str] = []
        async for chunk in self._llm.stream_completion(
            system=blocks["system"],
            user=blocks["user"],
            prefill="",
        ):
            if chunk.text:
                chunks.append(chunk.text)
            if chunk.done:
                break

        body = "".join(chunks).strip()
        return f"RECALL:\n{body}\n"
```

改動重點：
- 建構子吃 `MemSearch` 取代 `Memory`；移除 embedder 依賴（memsearch 自帶）
- `recall()` 的 `character_id` 簽章保留但忽略（step 10 才用）
- `top_k` 從 settings 注入；caller 可單次 override
- 用 `h['content']` 取 chunk 文字（memsearch hit 結構）
- 不再傳 `mode="hybrid"`（memsearch 預設就是 hybrid）

step 10 預告：未來會變成 `await self._memsearch.search(query, top_k=k, source_prefix=f"{character_id}/")`，外部 caller 不用改。

---

## §6 Kernel Wiring

`src/dollos/kernel.py`：

```python
"""DollOS kernel: wires LLM adapter, memsearch, and IPC server together."""

import asyncio
import logging
import signal
from collections.abc import AsyncIterator

from memsearch import MemSearch

from dollos.config import Settings
from dollos.inner_voice import InnerVoice
from dollos.ipc.messages import ErrorMsg, ServerMessage, TextChunk, TextInput, TurnEnd
from dollos.ipc.server import WebSocketServer
from dollos.llm.adapter import LLMAdapter
from dollos.llm.composed import ComposedLLMAdapter
from dollos.llm.templates import Qwen3PlainTemplate, Qwen3ThinkingTemplate
from dollos.llm.transport import LlamaCppProvider
from dollos.prompts import PromptRenderer

logger = logging.getLogger(__name__)


def build_adapter(settings: Settings) -> LLMAdapter:
    # unchanged
    ...


def build_memsearch(settings: Settings) -> MemSearch:
    """Construct memsearch rooted at data.root / memory / shared.

    step 10 will extend `paths` to include the active character's
    private directory. v1 only has shared.
    """
    shared_path = settings.data.root / "memory" / "shared"
    shared_path.mkdir(parents=True, exist_ok=True)
    return MemSearch(paths=[str(shared_path)], embedding_provider="onnx")


def build_inner_voice(
    settings: Settings, memsearch: MemSearch, renderer: PromptRenderer
) -> InnerVoice:
    provider = LlamaCppProvider(
        base_url=settings.inner_voice.base_url,
        timeout_s=settings.inner_voice.timeout_s,
    )
    llm = ComposedLLMAdapter(provider=provider, template=Qwen3PlainTemplate())
    return InnerVoice(
        memsearch=memsearch,
        llm=llm,
        renderer=renderer,
        default_top_k=settings.memsearch.top_k,
    )


class DollOS:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.adapter = build_adapter(settings)
        self.renderer = PromptRenderer()
        self.memsearch = build_memsearch(settings)
        self.inner_voice = build_inner_voice(settings, self.memsearch, self.renderer)
        self._character_profile = settings.character.profile_path.read_text()
        self.server = WebSocketServer(
            host=settings.ipc.host,
            port=settings.ipc.port,
            handler=self._handle_text_input,
        )
        self._shutdown = asyncio.Event()

    async def _handle_text_input(self, msg: TextInput) -> AsyncIterator[ServerMessage]:
        try:
            system = self.renderer.render(
                "scaffolding", character=self._character_profile
            )
            recall = await self.inner_voice.recall(msg.text)
            prefill = f"<think>\n{recall}GOAL: "
            async for chunk in self.adapter.stream_completion(
                system=system, user=msg.text, prefill=prefill,
            ):
                if chunk.text:
                    yield TextChunk(text=chunk.text)
                if chunk.done:
                    break
            yield TurnEnd()
        except Exception as e:
            logger.exception("handler error")
            yield ErrorMsg(message=f"handler error: {e}")

    async def run(self) -> None:
        await self.memsearch.index()
        try:
            await self.server.start()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self._shutdown.set)
            try:
                await self._shutdown.wait()
            finally:
                await self.server.stop()
        finally:
            pass   # memsearch 無 close()
```

設計重點：
- `build_memsearch` 自動建 `shared/` 目錄（fresh launch 友善）
- `index()` 在 `server.start()` 之前 — IPC 接到第一個 request 時 memsearch 已就緒
- handler 邏輯不變（VoM prefill 格式從 step 3 保留）
- 沒 `memsearch.close()`（套件未提供，Milvus Lite 是 file-based）

---

## §7 測試策略

| 測試檔 | 動作 | 範圍 |
|---|---|---|
| `tests/test_memory_*.py`（5 個檔）| 刪 | Plan 2 sqlite-vec / embedder / scoring 不再相關 |
| `tests/test_inner_voice.py` | 改寫 | mock `MemSearch.search()` 取代真 Memory + StubEmbedder |
| `tests/test_kernel_factories.py` | 改寫 | 測 `build_memsearch()` + 新簽章 `build_inner_voice(settings, memsearch, renderer)` |
| `tests/test_e2e.py` | 改寫 | monkeypatch `InnerVoice.recall` + `MemSearch.index` no-op |
| `tests/test_config.py` | 改寫 | 砍 `[memory]`/`[embedder]` 測試；加 `[data]`/`[memsearch]` 測試 |

**新 `test_inner_voice.py` 的 fake**：

```python
class _FakeMemSearch:
    """Stub: returns canned hits, captures last query / top_k."""
    def __init__(self, hits: list[dict]) -> None:
        self._hits = hits
        self.last_query: str | None = None
        self.last_top_k: int | None = None

    async def search(self, query: str, top_k: int = 3) -> list[dict]:
        self.last_query = query
        self.last_top_k = top_k
        return self._hits
```

7 個測試（沿用 step 3 意圖）：

1. has hits → recall returns `"RECALL:\n..."`
2. system prompt 來自 iv_recall.jinja（"memory recall helper" 子字串）
3. user block 含 query + numbered candidates
4. prefill="" 進小模型
5. empty hits → `"RECALL:\n(no relevant memories)\n"`（fake_llm.call_count == 0）
6. 大模型輸出被 strip
7. `top_k` 從 settings 透傳到 memsearch.search

**`test_e2e.py`**：保留 step 3 的 monkeypatch `InnerVoice.recall`（行為不變）；多一個 `monkeypatch.setattr("memsearch.MemSearch.index", _noop)` 避免測試中下載 ONNX 模型。

**不測**：
- 真 memsearch / Milvus Lite / ONNX 推理（manual smoke test 範疇）
- memsearch 內部行為（信任套件）
- markdown 解析 / chunk / 索引（memsearch 自己測）

---

## §8 邊界與錯誤路徑

| 情境 | 行為 |
|---|---|
| `data/memory/shared/` 不存在 | `build_memsearch` 用 `mkdir(parents=True, exist_ok=True)` 自動建 |
| `data/memory/shared/` 存在但空 | `index()` 跑空集合；`search()` 回 `[]` → InnerVoice 回 `(no relevant memories)` |
| 第一次啟動，ONNX bge-m3 未下載 | memsearch 第一次 index/search 時下載 ~558MB；阻塞 startup（首啟慢，後續快）|
| 網路斷線 + 模型未 cached | 下載失敗 → exception bubble up → DollOS 啟動失敗 |
| `memsearch.search()` 失敗 | exception bubble up → handler `except Exception` → ErrorMsg |
| `memsearch.index()` 在 startup 失敗 | exception bubble up → DollOS 啟動失敗（IPC 還沒 start，使用者連不上）|
| user 文字空字串 | memsearch 行為由套件決定；通常回 [] → 流程不破 |
| markdown 檔語法異常 | memsearch 自己處理（chunk 失敗或跳過）|

設計原則：
- 信任 memsearch 套件契約，不在 DollOS 層加防禦
- 失敗統一 bubble up；handler 內 → ErrorMsg；handler 外（startup）→ daemon 啟動失敗
- 無 fallback / retry / 靜默退化

---

## §9 Plan 整合策略

`vom-integration` branch（step 3 完成的工作）**不刪、不 merge**，留作歷史參考。將來若用到再 cherry-pick。

**新 `memsearch-pivot` branch 從 main 開**，cherry-pick 純 prompt-side commits 過來：

```bash
git worktree add .worktrees/memsearch-pivot -b memsearch-pivot main
cd .worktrees/memsearch-pivot

# Cherry-pick prompt-side commits from vom-integration branch
git cherry-pick 5ea5aaf      # Qwen3PlainTemplate
git cherry-pick 1b46e10      # PromptRenderer.render_blocks() + tests
git cherry-pick ae25f54      # iv_recall.jinja
# 若有衝突解掉；其中可能會碰到 inner_voice.py 因 plan-4 的 InnerVoice 帶 Memory 依賴
# — 直接 git checkout main -- src/dollos/inner_voice.py 即可（後面 task 3 會重寫）

uv sync
uv run pytest                # 確認 cherry-pick 後仍綠
```

cherry-pick 完應有 3 個額外 commits 在 main 之上。然後從 task 1 開始做 memsearch pivot。

---

## §10 Plan Task 預估（7 tasks）

> writing-plans 階段展開細節。

0. 開 `memsearch-pivot` worktree from main，cherry-pick prompt-side commits（Qwen3PlainTemplate / render_blocks / iv_recall.jinja），跑 pytest
1. 加 memsearch 依賴 + 砍 sqlite-vec 依賴；刪 `dollos.memory/` 整個模組 + 對應 5 個 test_memory_*.py + `data/.gitkeep` + `.gitignore` data/
2. `config.py` — 砍 MemoryConfig + EmbedderConfig；加 DataConfig + MemsearchConfig；改 Settings；改 test_config.py
3. `inner_voice.py` 重寫成吃 MemSearch；test_inner_voice.py 全改用 _FakeMemSearch（7 tests）
4. `kernel.py` — `build_memsearch` + `build_inner_voice(settings, memsearch, renderer)` + DollOS 構造 + handler prefill + run lifecycle；`test_kernel_factories.py` 改寫
5. `tests/test_e2e.py` 改寫（monkeypatch InnerVoice.recall + MemSearch.index）
6. Manual smoke test — 起小模型 + 大模型 server，vim 編 `data/memory/shared/2026-05-03.md`，IPC 問問題；roadmap.md / CLAUDE.md 標 step 3 為 memsearch-completed

預估比 step 3 大（7 vs 5 tasks），因為要砍掉 + 重做 memory 層。

---

## §11 後續 Plan 連動

- **Step 4（event loop）**：handler push UserTextEvent 進 queue；DollLoop 跑同樣 recall + LLM call。VoM 路徑不變
- **Step 5（Inner Voice full）**：multi-capability（first_instinct / emotion / summary）；SELF_STATE block 並列 RECALL；可能改用 memsearch.expand() L2/L3 取背景
- **Step 7（reflex + bracket loop）**：recall 變成大模型可主動呼叫的 internal tool
- **Step 8（auto-write memory）**：assistant turn 結束後 append 到 `data/memory/{shared|character_id}/YYYY-MM-DD.md`；memsearch file watcher 或下次啟動 index() 重新對齊
- **Step 10（Character Pack）**：character_id 從 character pack 載入 → `MemSearch(paths=[shared_dir, character_dir])`；search 時 `source_prefix=f"{character_id}/"`
- **未來換 embedder**：擴 `MemsearchConfig` 加 `embedding_provider` 欄位，`build_memsearch` 帶過去
