# Inner Voice + VoM RECALL Utility — Design

**日期：** 2026-05-02
**狀態：** 草案（待使用者最終審閱）
**範圍：** main spec 「DollOS Pivot」§11.6 列出的 Plan 4 — 純文字 utility 層提供 `recall(query)` 能力，給未來 Plan 5（Conversation Engine）合成 VoM RECALL block 進大模型 prefill
**對齊主 spec：**
- `2026-05-01-dollos-pivot-to-computer-design.md`（§4 Inner Voice、§5 Doll Turn / VoM Prefill）
- `2026-05-02-llm-provider-template-design.md`（Plan 3 — Provider/Template 抽象，Plan 4 直接吃此抽象）
- `docs/research/grammar_injection_techreport.md`（VoM 技術源頭）

---

## §0 範圍

**Plan 4 做什麼**：建立純文字 utility — `InnerVoice` 類別，唯一公開方法 `recall(query)` 撈 Memory + 用小模型合成 RECALL block 字串給 caller 用。

**Plan 4 不做**：
- 不做 `digest` / `classify` / `extract` / `tag` / `compress` 其他 capability — 留給 Plan 11（Event Loop / Instinct）真有需要時再加
- 不接 Event Queue — Plan 11
- 不接 Conversation Engine — Plan 5
- 不寫 Memory（read-only consumer）— Memory write 由 Plan 5 turn 結束 hook 處理
- 不處理 mood / SELF_STATE / preferences — Plan 7
- 不讓 Inner Voice 自己跑後台（沒有事件源呼叫它）— Plan 5/11 是 caller
- 不修 daemon.py 的 request 流程 — 只加 factory 函數，Plan 5 才 wire 進 turn

**Plan 4 完成後**：
- daemon 有 `InnerVoice` 類別可 import
- 有 `build_inner_voice(settings, memory)` factory 把 (LlamaCppProvider, Qwen3PlainTemplate, Memory) 組起來
- Settings 多一個 `[inner_voice]` 段
- 跟 Plan 1 + 2 + 3 完全相容（無回歸）

---

## §1 為何要這個

DollOS 的招牌差異化是 **VoM（Voice of Mind）** — 小模型即時撈 memory + 合成 RECALL block，prefill 進大模型 thinking 區塊（見 grammar_injection_techreport §2.4）。Plan 5（Conversation Engine）會把 RECALL block 接到 Doll turn 的 prefill。

但 RECALL block 怎麼生？兩條路：

A. **Memory 撈出 top-K → 直接 bullet list 丟給大模型** — 簡單但沒 LLM 紅利。N 條 facts 直接全給，沒篩、沒合成
B. **小模型篩選 + 合成 RECALL** — Inner Voice 介於 Memory 跟大模型之間，filter 不相關的 candidate、合成精煉的 bullets

techreport E1+E2 驗證 B 比 A 強：think token 壓縮 6–22×、HumanEval 100% 維持。Plan 4 做 B。

Plan 3 已 ship Provider/Template 抽象 — Plan 4 直接組合 `(LlamaCppProvider, Qwen3PlainTemplate)` 不需要重新發明 transport 層。

---

## §2 系統架構

```
┌────────────────────────────────────────────────────┐
│  InnerVoice                                         │
│  __init__(memory: Memory, llm: LLMAdapter)          │
│                                                     │
│  async recall(query, character_id=None, top_k=10)   │
│    1. memory.search(query, ...) → top-K facts      │
│    2. build user prompt with facts as candidates   │
│    3. llm.stream_completion(...) → drain stream     │
│    4. return formatted "RECALL:\n- ...\n" string    │
└──────────┬──────────────────────────────┬───────────┘
           │                              │
           ▼                              ▼
┌──────────────────────┐    ┌────────────────────────┐
│  Memory              │    │  LLMAdapter (ABC)      │
│  (Plan 2)            │    │  Plan 1 介面            │
│  hybrid retrieval    │    │  ↓ 透過 Plan 3 構造:    │
└──────────────────────┘    │  ComposedLLMAdapter   │
                            │  + LlamaCppProvider   │
                            │  + Qwen3PlainTemplate │
                            └────────────────────────┘
```

**InnerVoice 對外只認兩個 ABC**：`Memory`（Plan 2）跟 `LLMAdapter`（Plan 1）。具體用什麼 (Provider, Template) 是 factory 的事。

**測試友善**：mock 任一個 ABC 就能單元測 `recall()` 行為。

---

## §3 檔案結構

```
src/dollos/
├── inner_voice.py             # NEW
└── llm/
    └── templates.py           # MODIFY — 加 Qwen3PlainTemplate

tests/
├── test_inner_voice.py        # NEW
└── test_llm_templates.py      # MODIFY — 加 Qwen3PlainTemplate 測試
```

`daemon.py` 加 `build_inner_voice()` factory（不修 Daemon class，不接進 handler）。`config.py` 加 `InnerVoiceConfig` schema 跟 `Settings.inner_voice`。

---

## §4 Qwen3PlainTemplate

加進 `src/dollos/llm/templates.py`（Plan 3 已建立的檔）。

```python
class Qwen3PlainTemplate(PromptTemplate):
    """Qwen3.x instruct (non-thinking) ChatML.

    Same envelope as Qwen3ThinkingTemplate but does NOT open a <think>
    block — Inner Voice's small models (Qwen3-0.6B/1.7B Instruct) are
    not trained to use <think>...</think>; opening one would confuse
    them. Prefill goes directly inside the assistant turn.
    """

    def render(self, *, system: str, user: str, prefill: str) -> str:
        parts = [
            "<|im_start|>system",
            system,
            "<|im_end|>",
            "<|im_start|>user",
            user,
            "<|im_end|>",
            "<|im_start|>assistant",
            "",
        ]
        rendered = "\n".join(parts)
        if prefill:
            rendered += prefill
        return rendered
```

差異於 `Qwen3ThinkingTemplate` — 沒有 `<think>` 那一行。

---

## §5 InnerVoice 類別

`src/dollos/inner_voice.py`：

```python
"""InnerVoice — small-model VoM RECALL block synthesizer."""

from dollos.llm.adapter import LLMAdapter
from dollos.memory.store import Memory


INNER_VOICE_SYSTEM_PROMPT = """\
You are Doll's memory recall helper. Read the query and candidate facts \
from memory, output ONLY the facts relevant to the query as bullets.

Rules:
- One bullet per relevant fact: "- <fact in concise prose>"
- If a candidate is irrelevant, skip it
- Do NOT add facts not in candidates
- Do NOT speculate or fill gaps
- Output bullets only. Don't repeat the query, don't add header.
- If no candidates are relevant, output a single line: (no relevant memories)
"""


class InnerVoice:
    """Synthesize VoM RECALL blocks from memory using a small LLM."""

    def __init__(self, memory: Memory, llm: LLMAdapter) -> None:
        self._memory = memory
        self._llm = llm

    async def recall(
        self,
        query: str,
        *,
        character_id: str | None = None,
        top_k: int = 10,
    ) -> str:
        """Return a RECALL block string for the given query.

        Always starts with "RECALL:\\n" — caller can embed verbatim into
        a Doll prefill or display to user.

        If memory has no candidates, returns
        "RECALL:\\n(no relevant memories)\\n" — caller can still embed
        without breaking prefill structure; the Doll model can read it
        and decide accordingly.
        """
        results = await self._memory.search(
            query,
            character_id=character_id,
            top_k=top_k,
            mode="hybrid",
        )
        if not results:
            return "RECALL:\n(no relevant memories)\n"

        candidates = "\n".join(
            f"{i + 1}. {r.fact.text}" for i, r in enumerate(results)
        )
        user_block = f"Query: {query}\n\nCandidates:\n{candidates}"

        chunks: list[str] = []
        async for chunk in self._llm.stream_completion(
            system=INNER_VOICE_SYSTEM_PROMPT,
            user=user_block,
            prefill="",
        ):
            if chunk.text:
                chunks.append(chunk.text)
            if chunk.done:
                break

        body = "".join(chunks).strip()
        return f"RECALL:\n{body}\n"
```

### 5.1 設計重點

- **Returns 一律以 `RECALL:\n` 起頭** — caller 拿到就直接能 embed 進 Doll prefill，不用拼接
- **Empty memory** 也回 `RECALL:` block（不是空字串、不是 None）— 結構穩定，prefill 模板不需 conditional
- **`mode="hybrid"`** 寫死 — Plan 2 的 RRF 混合檢索是預設好的選擇
- **`top_k=10` 預設** — 小模型 context 不大，10 條 candidates 約 500-1000 token，安全
- **`prefill=""`** — 不預先塞文字。System prompt 已經要求模型直接出 bullet list，不需 prefill 引導
- **Drain streaming**：InnerVoice 是消費者，呼叫 `LLMAdapter.stream_completion()` 然後把 chunks 串成單一字串回傳。non-streaming 對 ~50-150 token 輸出無實際差異
- **`character_id` 透傳到 Memory.search** — 跟 Plan 2 character scoping 一致

### 5.2 不做什麼（明確排除）

- 不重試（model 失敗 = exception bubble up）
- 不超時保護（用 LLMAdapter / Provider 的 timeout）
- 不快取結果（每次 recall 都重新 query）
- 不對 results 做後處理 / 校驗（信任小模型輸出）
- 不限制 query 長度（caller 自己控制）

---

## §6 System Prompt v1

`INNER_VOICE_SYSTEM_PROMPT` 內容釘在 spec 中（§5 程式碼裡）。理由：

1. 是系統行為 contract — 改它等於改 Inner Voice 行為
2. v1 簡單明確，不會產生「prompt drift」
3. 後續真要 tune（Plan 5 整合後實測 + 角色化）由獨立 plan 修改

### Prompt 設計理由

- **「Output ONLY the facts relevant」** — RRF 撈出來的 top-K 不一定都相關（vector 搜出語意接近但話題不同）。讓小模型篩
- **「Do NOT add facts not in candidates」** — 防止小模型幻覺。Inner Voice 是純 retrieval-grounded，不是 generative
- **「Don't repeat the query, don't add header」** — 控制輸出長度。Inner Voice 在 hot path 上，每 token 都重要
- **「(no relevant memories)」** — 明確訊號讓 caller / Doll 知道狀況

---

## §7 Settings + Factory

### 7.1 InnerVoiceConfig（minimal）

```python
class InnerVoiceConfig(BaseModel):
    base_url: str
    timeout_s: float = 30.0
```

刻意只兩個欄位。`provider` / `template` / `model_alias` 都不要 — v1 寫死 `(LlamaCppProvider, Qwen3PlainTemplate)`，將來真有需要換時再擴 schema。

加進 `Settings`：

```python
class Settings(BaseModel):
    llm: LLMConfig
    ipc: IPCConfig = ...
    log: LogConfig = ...
    memory: MemoryConfig
    embedder: EmbedderConfig
    inner_voice: InnerVoiceConfig   # NEW
```

`config.example.toml` 加：

```toml
[inner_voice]
base_url = "http://127.0.0.1:8003"   # separate llama-server with small model (Qwen3-0.6B-Instruct etc.)
timeout_s = 30.0
```

### 7.2 Factory

`daemon.py` 加（不在 `Daemon.__init__` 中呼叫）：

```python
def build_inner_voice(settings: Settings, memory: Memory) -> InnerVoice:
    provider = LlamaCppProvider(
        base_url=settings.inner_voice.base_url,
        timeout_s=settings.inner_voice.timeout_s,
    )
    template = Qwen3PlainTemplate()
    llm = ComposedLLMAdapter(provider=provider, template=template)
    return InnerVoice(memory=memory, llm=llm)
```

Plan 5（Conversation Engine）來時呼叫此 factory 構造 InnerVoice 並接進 Doll turn 流程。

---

## §8 邊界與錯誤路徑

| 情境 | 行為 |
|---|---|
| Memory 完全空 | `recall()` 回 `"RECALL:\n(no relevant memories)\n"` |
| Memory 有但全部無關 | 取決於小模型 — 它 should 輸出 `(no relevant memories)`，否則照模型輸出 bullets |
| 小模型 endpoint 連不上 | LlamaCppProvider 拋 httpx exception，bubble up；caller 處理 |
| 小模型超時 | timeout_s 觸發 httpx ReadTimeout，bubble up |
| 小模型輸出 invalid（非 bullet）| Inner Voice 不檢查 — 整段塞進 RECALL block，caller / Doll 自己看著辦 |
| query 是空字串 | Memory.search 會跑（embedding 空字串），通常 top-K 結果不相關，小模型應該回 (no relevant) |
| top_k=0 | Memory.search 回空 list，走 empty branch |

**v1 設計原則**：信任 Memory + 信任小模型輸出，不在 Inner Voice 層做防禦。失敗以 exception 形式 bubble up，caller 統一處理。

---

## §9 測試策略

| 測試檔 | 範圍 |
|---|---|
| `tests/test_llm_templates.py`（既有，加測試）| 新增 5 個測試對 Qwen3PlainTemplate：abstract（既有）、render 不含 `<think>`、ChatML envelope、empty prefill 結尾、preserves special chars |
| `tests/test_inner_voice.py` | mock LLMAdapter（fake yield bullets）+ 真 in-memory SQLite Memory + StubEmbedder：（a）寫 5 facts, recall returns block w/ "RECALL:" prefix（b）empty memory case（c）character_id 傳遞給 Memory.search（d）small model output 被 strip 後嵌入 |

InnerVoice 測試**不打真小模型**（用 fake LLMAdapter）。E2E 對真小模型留 manual smoke test，不入自動測試。

### 9.1 InnerVoice 測試 fake LLMAdapter 設計

```python
class _FakeLLMAdapter(LLMAdapter):
    """Yield canned chunks. Captures last call args."""
    def __init__(self, response: str):
        self._response = response
        self.last_system: str | None = None
        self.last_user: str | None = None
        self.last_prefill: str | None = None

    async def stream_completion(self, *, system, user, prefill="", stop=None, max_tokens=1024):
        self.last_system = system
        self.last_user = user
        self.last_prefill = prefill
        yield StreamChunk(text=self._response, done=False)
        yield StreamChunk(text="", done=True)
```

---

## §10 Non-goals（明確排除）

- 不做其他 capability（digest / classify / extract / tag / compress）— 留 Plan 11
- 不做 Inner Voice 結果快取（每次 recall 重撈 / 重 inference）— v1 簡單，未來看實測效能
- 不做小模型動態切換（換 model_alias 要重啟 daemon）— 自用環境，重啟即可
- 不做 prefix cache 顯式管理 — 依賴 llama-server 本身的 `cache_prompt: true`（Plan 3 LlamaCppProvider 已預設啟用）
- 不做 query rewriting / multi-step retrieval — 一次 query → 一次 recall
- 不做小模型 fallback 到大模型 — fail = bubble up exception
- 不做 system prompt 客製化 / per-character override — Plan 5 Character Pack 整合時再考慮
- 不做 context length 警告（candidates 太多撐爆 small model context）— 由 top_k 預設值控制，使用者調太大自己負責

---

## §11 Open Questions（留 plan 階段或後續）

- **小模型選型**：Qwen3-0.6B-Instruct vs Qwen3-1.7B-Instruct vs Llama-3.2-1B-Instruct vs Gemma-2-2B-it。Plan 4 plan 階段做 manual smoke test 比較幾個的 recall 品質後選一個（pin 在 README / config example）。Plan 4 程式碼不綁定特定 model，由 base_url 指向哪個 llama-server 決定
- **`top_k` 預設值**：v1 = 10。實測後可能調整到 5 / 15
- **System prompt 細節 tuning**：v1 寫好的版本若實測表現差，下一個 plan iterate
- **Empty result 處理**：`(no relevant memories)` 字串是給 Doll 看的，Doll 能正確處理嗎？Plan 5 整合後驗證

---

## §12 Plan Task 預估（5 tasks）

> writing-plans 會展開細節。

1. 加 `Qwen3PlainTemplate` 進 `src/dollos/llm/templates.py` + tests
2. 寫 `src/dollos/inner_voice.py`（InnerVoice + system prompt 常量）+ `tests/test_inner_voice.py`（mock LLMAdapter + 真 :memory: SQLite Memory + StubEmbedder）
3. `config.py` 加 `InnerVoiceConfig` + `Settings.inner_voice` + 對應 test 更新
4. `daemon.py` 加 `build_inner_voice(settings, memory)` factory（不接進 handler）+ basic test
5. `config.example.toml` 加 `[inner_voice]` 段；跑 full suite + 手動 smoke test

預估比 Plan 3 短（5 vs 7）— scope 小、無耦合 commit 需要。

---

## §13 後續 Plan 連動

- **Plan 5（Conversation Engine + Character Pack）** — 在 Doll turn 構造 prefill 時呼叫 `inner_voice.recall(user_input)` → 把回傳的 RECALL block 拼成完整 prefill `<think>\n{recall_block}LESSONS: ...\nGOAL: `
- **Plan 7（Self-First Design）** — 在 Inner Voice 之外**另外**有 self-state synthesizer（不是 Inner Voice 的職責），負責產生 SELF_STATE block 跟 RECALL block 並列
- **Plan 11（Event Loop / Instinct dispatcher）** — 加其他 capability（digest / classify / triage 等）— 可能擴 InnerVoice 介面，也可能另外開新類別。看 Plan 11 設計時決定
- **未來 small model 升級**：換 base_url 指向不同 llama-server 即可，無 code 變動
