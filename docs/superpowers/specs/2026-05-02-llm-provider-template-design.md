# LLM Provider / Template 解耦層 — Design

**日期：** 2026-05-02
**狀態：** 草案（待使用者最終審閱）
**範圍：** main spec 「DollOS Pivot」§11.6 列出的 Plan 3 — 把現有 LlamaCppAdapter 拆成 Provider × Template 兩個正交抽象，為後續多 provider / 多 model 支援鋪路
**對齊主 spec：** `2026-05-01-dollos-pivot-to-computer-design.md`（§5 Doll Turn / VoM Prefill、§5.2 後端能力 adapter）

---

## §0 範圍

**Plan 3 做什麼**：把 Plan 1 的 `LlamaCppAdapter` 拆解成兩個正交抽象 — `Provider`（HTTP 傳輸 + endpoint 慣例）跟 `PromptTemplate`（model 特定格式），讓未來新增 provider（vLLM / OpenAI-compat / Anthropic）跟新 template（Qwen3-plain / Llama / Gemma）互相獨立、組合自由。

**Plan 3 不做**：
- 不新增任何 Provider — v1 只 ship `LlamaCppProvider`（既有）
- 不新增任何 Template — v1 只 ship `Qwen3ThinkingTemplate`（既有）
- 不做 prefill capability detection 的複雜邏輯（v1 唯一的 provider 就支援 prefill）
- 不重新設計 `LLMAdapter` ABC 介面 — 對外 API 不變
- 不做 BYO 後端設定 UI（Plan 5 / 8 處理）

**Plan 3 完成後**：
- daemon 對外行為 100% 不變（既有 15 個 tests 全綠）
- 內部抽象到位 — 加新 (provider, template) 組合時不用動既有程式碼
- Plan 4（Inner Voice）可直接組合 `(LlamaCppProvider, Qwen3PlainTemplate)` — 後者由 Plan 4 自己加
- 為 Plan 5 / Plan 7 在不同後端跑大模型鋪路

---

## §1 為何要拆

**問題狀**：Plan 1 的 `LlamaCppAdapter` 把兩件事綁一起：
1. **HTTP 傳輸** — POST llama-server 的 `/completion`，解析 SSE
2. **Prompt 格式** — Qwen3.x ChatML + `<think>\n` 預先打開

兩件事是 **正交的**：
- 同一 model（Qwen3.6）在不同 provider 上 prompt 格式可能不同（llama.cpp 要手刻 ChatML，vLLM 可能 server 自己 apply）
- 同一 provider（llama.cpp）跑不同 model（Qwen3-plain / Llama / Gemma）需要不同 ChatML / 不同 template

這次 pivot 觸發點：Plan 4（Inner Voice）會用 Qwen3-0.6B Instruct（**非 thinking**，無 `<think>\n`）— LlamaCppAdapter 直接接給它會炸（小模型沒被訓練要 close `<think>`，行為奇怪）。

**v1 範圍刻意收斂**：使用者選 minimal — 只 ship 既有的 (llama.cpp, Qwen3.6-thinking) 組合，但 **抽象在那裡**。Plan 4 / 5 / 7 真的需要新組合時，加新 Provider / Template 變成「寫一個檔」級別的工程。

---

## §2 三層架構

```
                            ┌─────────────────────────┐
        Caller (daemon.py,  │  LLMAdapter (ABC)       │
        Conversation        │  既有，介面不變           │
        Engine, Inner Voice)│  stream_completion(     │
                            │    system, user,        │
                            │    prefill, stop,       │
                            │    max_tokens)          │
                            └────────────┬────────────┘
                                         │ 實作
                                         ▼
                            ┌─────────────────────────┐
                            │  ComposedLLMAdapter     │
                            │  __init__(provider,     │
                            │           template)     │
                            │                          │
                            │  stream_completion:     │
                            │    prompt = template.\\  │
                            │      render(...)        │
                            │    yield from \\        │
                            │      provider.stream(\\ │
                            │        prompt, ...)     │
                            └─────┬───────────────┬───┘
                                  │               │
                ┌─────────────────┘               └─────────────────┐
                ▼                                                   ▼
   ┌──────────────────────────┐                    ┌──────────────────────────┐
   │  Provider (ABC)          │                    │  PromptTemplate (ABC)    │
   │  stream(prompt, stop,    │                    │  render(system, user,    │
   │         max_tokens)      │                    │         prefill) -> str  │
   │  supports_prefill: bool  │                    └────────────┬─────────────┘
   └──────────┬───────────────┘                                 │
              │                                                  │
              ▼                                                  ▼
   ┌──────────────────────────┐                    ┌──────────────────────────┐
   │  LlamaCppProvider        │                    │  Qwen3ThinkingTemplate   │
   │  POST /completion        │                    │  ChatML + <think>\n      │
   │  SSE 解析                │                    │  prefill 在 <think> 後    │
   │  supports_prefill = True │                    │                          │
   └──────────────────────────┘                    └──────────────────────────┘
```

### 三層職責

| 層 | 名字 | 職責 |
|---|---|---|
| 高階 | `LLMAdapter` ABC | 對 caller 的穩定介面。**不動**，沿用 Plan 1 |
| 組合 | `ComposedLLMAdapter` | 把 provider × template 組起來，實作 `LLMAdapter` |
| 低階 - 傳輸 | `Provider` ABC + concrete | HTTP / endpoint / SSE 解析。輸入 raw prompt 字串，輸出 `StreamChunk` |
| 低階 - 格式 | `PromptTemplate` ABC + concrete | model-specific prompt 格式化。輸入 system/user/prefill，輸出單一字串 |

兩個低階抽象**互不認識** — Provider 只認 `prompt: str`，Template 只產 `str`。組合是 ComposedLLMAdapter 的職責。

---

## §3 介面定義

### 3.1 Provider ABC

```python
# src/dollos/llm/transport.py

class Provider(ABC):
    """LLM transport — HTTP / endpoint conventions / response parsing.

    Concrete implementations talk to a specific LLM server (llama.cpp,
    vLLM, OpenAI-compat, Anthropic) and yield StreamChunk objects.
    They take a fully-rendered prompt string; prompt formatting is
    PromptTemplate's job.
    """

    @property
    @abstractmethod
    def supports_prefill(self) -> bool:
        """True if this provider's endpoint can take an open assistant
        turn (i.e. you can give a partial assistant message and have
        the model continue from there). Critical for VoM."""

    @abstractmethod
    async def stream(
        self,
        *,
        prompt: str,
        stop: list[str] | None = None,
        max_tokens: int = 1024,
    ) -> AsyncIterator[StreamChunk]:
        """Stream tokens. Caller is responsible for prompt formatting."""
        ...
```

### 3.2 PromptTemplate ABC

```python
# src/dollos/llm/templates.py

class PromptTemplate(ABC):
    """Model-family-specific prompt rendering.

    Takes high-level (system, user, prefill) and produces the single
    prompt string the model expects (with role markers, special tokens,
    etc.). For "server-applied" templates (e.g. Anthropic, OpenAI chat
    completions where the API takes messages instead of a raw prompt),
    a concrete implementation may be a no-op stub — the corresponding
    Provider would talk in messages directly. Plan 3 v1 doesn't ship
    such a Provider, but the interface allows it.
    """

    @abstractmethod
    def render(
        self,
        *,
        system: str,
        user: str,
        prefill: str,
    ) -> str:
        ...
```

### 3.3 ComposedLLMAdapter

```python
# src/dollos/llm/composed.py

class ComposedLLMAdapter(LLMAdapter):
    """Combine a Provider with a PromptTemplate to satisfy LLMAdapter."""

    def __init__(self, provider: Provider, template: PromptTemplate):
        self._provider = provider
        self._template = template

    async def stream_completion(
        self,
        *,
        system: str,
        user: str,
        prefill: str = "",
        stop: list[str] | None = None,
        max_tokens: int = 1024,
    ) -> AsyncIterator[StreamChunk]:
        prompt = self._template.render(
            system=system, user=user, prefill=prefill
        )
        async for chunk in self._provider.stream(
            prompt=prompt, stop=stop, max_tokens=max_tokens
        ):
            yield chunk
```

注意 ComposedLLMAdapter 對 prefill 不做檢查 — 那是 caller / build_adapter 的責任。如果使用者把 prefill 用在不支援 prefill 的 provider 上，行為由 Provider 決定（v1 唯一 provider 支援，不是問題；後續 plan 加新 provider 時要在 caller 層擋）。

---

## §4 v1 Concrete 實作（只有兩個）

### 4.1 LlamaCppProvider

從現有 `LlamaCppAdapter` 拆出 HTTP / SSE 部分。功能不變。

```python
# src/dollos/llm/transport.py

class LlamaCppProvider(Provider):
    """POST /completion to a llama-server with SSE streaming."""

    def __init__(self, base_url: str, timeout_s: float = 60.0):
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s

    @property
    def supports_prefill(self) -> bool:
        return True   # llama.cpp /completion always supports prefill

    async def stream(self, *, prompt, stop=None, max_tokens=1024):
        body = {
            "prompt": prompt,
            "stream": True,
            "n_predict": max_tokens,
            "stop": stop or ["<|im_end|>"],
            "cache_prompt": True,
        }
        url = f"{self._base_url}/completion"
        timeout = httpx.Timeout(self._timeout_s, connect=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, json=body) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    # ... SSE 解析（既有邏輯不動）
                    yield StreamChunk(text=..., done=...)
                    if data.get("stop"):
                        return
```

### 4.2 Qwen3ThinkingTemplate

從現有 `_render_chatml` 拆出來。功能不變。

```python
# src/dollos/llm/templates.py

class Qwen3ThinkingTemplate(PromptTemplate):
    """Qwen3.x thinking-model ChatML.

    Opens <think>\n inside the assistant turn so prefill content goes
    inside the thinking block (Plan 1 review decision).
    """

    def render(self, *, system, user, prefill):
        parts = [
            "<|im_start|>system",
            system,
            "<|im_end|>",
            "<|im_start|>user",
            user,
            "<|im_end|>",
            "<|im_start|>assistant",
            "<think>",
            "",
        ]
        rendered = "\n".join(parts)
        if prefill:
            rendered += prefill
        return rendered
```

---

## §5 Settings 變動

### 5.1 LLMConfig schema

```python
class LLMConfig(BaseModel):
    provider: Literal["llamacpp"] = "llamacpp"   # rename: backend → provider
    template: Literal["qwen3-thinking"] = "qwen3-thinking"   # NEW
    base_url: str
    model_alias: str
    timeout_s: float = 60.0
```

`model_alias` 保留 — 純 metadata（log / debug 用），不影響行為。

### 5.2 config.example.toml

```toml
[llm]
provider = "llamacpp"          # was: backend = "llamacpp"
template = "qwen3-thinking"    # NEW
base_url = "http://127.0.0.1:8001"
model_alias = "unsloth/Qwen3.6"
timeout_s = 60.0
```

### 5.3 不向後相容

舊 config.toml 寫 `backend = "llamacpp"` 會失敗。Plan 3 是 self-host 自用 daemon，使用者改一行就好。沒有 migration shim。

### 5.4 build_adapter() 變動

```python
def build_adapter(settings: Settings) -> LLMAdapter:
    provider = _build_provider(settings)
    template = _build_template(settings)
    return ComposedLLMAdapter(provider=provider, template=template)


def _build_provider(settings) -> Provider:
    if settings.llm.provider == "llamacpp":
        return LlamaCppProvider(
            base_url=settings.llm.base_url,
            timeout_s=settings.llm.timeout_s,
        )
    raise ValueError(f"unknown provider: {settings.llm.provider}")


def _build_template(settings) -> PromptTemplate:
    if settings.llm.template == "qwen3-thinking":
        return Qwen3ThinkingTemplate()
    raise ValueError(f"unknown template: {settings.llm.template}")
```

未來新增 (provider, template) 只動這兩個 `_build_*` 函式 — 變數展開，加 elif。

---

## §6 檔案結構（Plan 3 完成後）

```
src/dollos/llm/
├── __init__.py             # 既有 — exports 變動：移除 LlamaCppAdapter，加 ComposedLLMAdapter / LlamaCppProvider / Qwen3ThinkingTemplate / Provider / PromptTemplate
├── adapter.py              # 既有 — LLMAdapter ABC + StreamChunk（不動）
├── transport.py            # NEW — Provider ABC + LlamaCppProvider
├── templates.py            # NEW — PromptTemplate ABC + Qwen3ThinkingTemplate
├── composed.py             # NEW — ComposedLLMAdapter
└── llamacpp.py             # DELETE — 內容拆進 transport.py + templates.py
```

每檔職責單一：
- `adapter.py` — 對外介面契約
- `transport.py` — HTTP / endpoint 層
- `templates.py` — model-specific 格式化
- `composed.py` — 把上面兩個串起來

---

## §7 測試策略

| 測試檔 | 範圍 |
|---|---|
| `tests/test_llm_transport.py` | LlamaCppProvider 的 SSE 解析、stop / max_tokens 轉發、`supports_prefill` 屬性。**從 test_llm_llamacpp.py 拆過來，去掉 prompt-rendering 部分**（mock POST /completion，斷言收到的 `prompt` 字串 = caller 給的字串）|
| `tests/test_llm_templates.py` | Qwen3ThinkingTemplate.render() 的 output 結構：含 system / user / `<think>\n` / prefill 結尾。**從 test_llm_llamacpp.py 的 prompt assertions 拆過來** |
| `tests/test_llm_composed.py` | ComposedLLMAdapter 把 template.render() 結果丟給 provider.stream()；用 stub Provider + stub Template 驗證 wiring 正確 |
| `tests/test_llm_llamacpp.py` | DELETE — 內容已拆 |
| `tests/test_e2e.py` | 既有，不動。end-to-end 仍走 `Settings → build_adapter → ComposedLLMAdapter → LlamaCppProvider → mocked /completion`，行為與 Plan 1 一致 |
| `tests/test_config.py` | 加一個 test，驗證 `provider = "llamacpp"` + `template = "qwen3-thinking"` 載入正確；驗證舊 `backend = "llamacpp"` 寫法 raises ValidationError |

預估完成後 test 數：原本 15 → 約 18-20（新增 transport / templates / composed 各 2-3 個 test，扣掉 llamacpp 的 3 個被拆走）。

---

## §8 Migration 風險與步驟

### 風險清單

| 風險 | 緩解 |
|---|---|
| 對既有 daemon 行為產生 regression | Task 結尾跑 full suite 包含 test_e2e 確保 round-trip 不變 |
| 既有 `backend` 寫法的 config.toml 突然炸 | 文件清楚標註 break，使用者改一行 |
| `LlamaCppAdapter` 還有外部 import | grep 整個 src/，所有 import 改成 `ComposedLLMAdapter` 或具體 Provider/Template |
| 測試重排順序 | 先寫新 test 全綠，再刪 `test_llm_llamacpp.py` |

### 遷移步驟（plan 階段會展開）

1. 寫 `transport.py`（Provider ABC + LlamaCppProvider）+ test
2. 寫 `templates.py`（PromptTemplate ABC + Qwen3ThinkingTemplate）+ test
3. 寫 `composed.py`（ComposedLLMAdapter）+ test
4. 改 `config.py`（rename backend → provider，加 template）+ test
5. 改 `daemon.py`（重寫 build_adapter）+ 跑 e2e 驗證行為一致
6. 改 `__init__.py` exports
7. 刪 `llamacpp.py` + `test_llm_llamacpp.py`
8. 改 `config.example.toml`

每步可獨立 commit。順序確保 build 從來不破。

---

## §9 Non-goals（明確排除）

- 不在 v1 ship 任何**新** Provider（vLLM / OpenAI-compat / Anthropic 都留給後續 plan）
- 不在 v1 ship 任何**新** PromptTemplate（Qwen3-plain / Llama / Gemma 都留給後續 plan）
- 不做 prefill capability runtime warning（如「你選的 provider 不支援 prefill，VoM 會降級」這種 UX）— 等真有不支援的 provider 加進來才有意義
- 不做 prompt template `/apply-template` 端點呼叫（直接 inline ChatML，跟 Plan 1 一樣）
- 不向後相容舊 config — 一刀切換，使用者改一行
- 不重新設計 `LLMAdapter` 介面 — 對 caller 100% 不變
- 不做 streaming vs non-streaming 抽象（Plan 4 Inner Voice 自己決定要不要 buffer 整段；Provider 一律 stream）

---

## §10 Open Questions（留 plan）

- `Provider.stream()` 跟 `LLMAdapter.stream_completion()` 簽名 90% 一樣（差異只在 `prompt: str` vs `system / user / prefill`）— 是否該重用 `StreamChunk` 之外還抽一個共同 base？v1 不做，重複 boilerplate 可接受
- `Qwen3ThinkingTemplate` 的 stop 預設 `["<|im_end|>"]` — 寫死在 Provider（v1）還是搬到 Template？v1 寫死 Provider，後續 plan 真需要 model-specific stop 再考慮
- 多個 (provider, template) 組合需要的 mock 用 stub class 還是 fixture？plan 階段決定
- `LLMConfig` 用 Literal 列舉 provider / template 還是 free-form str？v1 用 Literal，加 provider / template 時加 enum value

---

## §11 Plan Task 預估（8 tasks）

> writing-plans 會展開細節。這裡只列骨架。

1. 建立 `Provider` ABC 在 `src/dollos/llm/transport.py` + 簡單 test
2. 從 `llamacpp.py` 拆 `LlamaCppProvider` 進 `transport.py` + test（包含 SSE 解析、stop/max_tokens、supports_prefill）
3. 建立 `PromptTemplate` ABC + `Qwen3ThinkingTemplate` 在 `templates.py` + test
4. 建立 `ComposedLLMAdapter` 在 `composed.py` + test（用 stub Provider/Template）
5. 改 `config.py`：rename backend → provider，加 template 欄位 + 更新既有 config tests
6. 改 `daemon.py` build_adapter 用 ComposedLLMAdapter；刪 LlamaCppAdapter import
7. 刪 `llamacpp.py` + `tests/test_llm_llamacpp.py`；改 `__init__.py` exports
8. 改 `config.example.toml`；跑 full suite 驗證 e2e 仍綠

---

## §12 後續 Plan 連動

- **Plan 4（Inner Voice utility）** — 加 `Qwen3PlainTemplate` 在 `templates.py`，用 `(LlamaCppProvider, Qwen3PlainTemplate)` 組 InnerVoice 的 LLMAdapter（雖然 Inner Voice 可能不直接用 LLMAdapter 介面，端看 Plan 4 設計）
- **Plan 5（Conversation Engine + Character Pack）** — Settings 加 character-pack-driven provider/template override 機制（角色可指定自己的 LLM 後端）
- **Plan 7（Self-First Design）** — 不直接動 Plan 3 的東西，但會利用 LLMAdapter 介面跑 Doll turn
- **後續加 provider**：每加一個就一個 (Provider concrete class + Settings literal value + `_build_provider` 一個 elif)
- **後續加 template**：同上 pattern
