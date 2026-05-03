# VoM Integration — Design

**日期：** 2026-05-03
**狀態：** 草案（待使用者最終審閱）
**範圍：** Roadmap step 3 — merge Plan 4 的 `InnerVoice.recall()`，把 hardcoded recall prompt 搬進 `iv_recall.jinja`，IPC handler 在送 user input 給大模型前先 call recall，結果接進 `<think>\n{recall}GOAL: ` prefill。
**對齊主 spec：**
- `2026-05-01-dollos-pivot-to-computer-design.md`（§4 Inner Voice、§5 Doll Turn / VoM Prefill）
- `2026-05-02-inner-voice-utility-design.md`（Plan 4 — InnerVoice utility，本 plan 從其 branch 延伸）
- `2026-05-03-prompt-rendering-design.md`（step 2 — PromptRenderer + jinja templates）
- `docs/research/grammar_injection_techreport.md`（VoM 技術源頭）

---

## §1 範圍

**做**：
- merge plan-4 branch（既有 `InnerVoice` / `Qwen3PlainTemplate` / `InnerVoiceConfig` / `build_inner_voice` factory）
- 把 plan-4 hardcoded `INNER_VOICE_SYSTEM_PROMPT` 搬進 `iv_recall.jinja`（system + user 兩 block）
- `PromptRenderer` 擴 `render_blocks()` 取多 block dict
- IPC handler 在 user input 進大模型前先 `await inner_voice.recall(user_text)`，結果接進 prefill `<think>\n{recall}GOAL: `

**不做**：
- character_id 真實 scoping（一律 None，等 step 10 Character Pack）
- 自動寫 memory（read-only，等 step 8 Memory 自動寫）
- SELF_STATE / GOAL 由小模型生成（等 step 5 Inner Voice full）
- empty memory 條件分支（恆定 prefill）
- recall 結果快取 / retry / fallback

**Demo**：手動往 memory 塞 facts → 透過 IPC 問 Doll 相關問題 → Doll 引用該 fact 回答。空 memory 時行為退化但不報錯（大模型看到 `(no relevant memories)` 自決）。

---

## §2 系統架構

```
TextInput (user 文字)
    ↓
DollOS._handle_text_input
    ↓
  ┌─ inner_voice.recall(user_text)
  │     ├─→ Memory.search (mode="hybrid", character_id=None, top_k=10)
  │     └─→ small LLM (iv_recall.jinja system + user blocks)
  │     回 "RECALL:\n- ...\n"  或  "RECALL:\n(no relevant memories)\n"
  ↓
  prefill = f"<think>\n{recall}GOAL: "
  ↓
  big LLM.stream_completion(system=scaffolding, user=user_text, prefill=prefill)
    ↓
  TextChunk 串流回 IPC → TurnEnd
```

**同步順序**：recall 必須先完整 drain 成字串才能組 prefill。串流只發生在大模型階段。使用者體感上會有一段「先停一下」的延遲（小模型 inference 約 100-500ms，視 candidates 數量而定）。

**新增依賴**：`DollOS` 多持有 `Memory` + `InnerVoice` 兩個 instance。kernel 啟動時 `build_memory(settings)` + `build_inner_voice(settings, memory, renderer)` 構造。

---

## §3 檔案改動

```
src/dollos/
├── kernel.py                      # MODIFY — wire Memory + InnerVoice + recall before big LLM
├── inner_voice.py                 # MODIFY — 移除 INNER_VOICE_SYSTEM_PROMPT 常量，改用 PromptRenderer
└── prompts/
    ├── renderer.py                # MODIFY — 加 render_blocks() 方法
    └── templates/
        └── iv_recall.jinja        # NEW — system + user blocks

tests/
├── test_kernel.py                 # MODIFY/NEW — kernel 整合測試
├── test_inner_voice.py            # MODIFY — 改用 jinja 後的測試
└── test_prompts_renderer.py       # MODIFY — 加 render_blocks() 測試
```

**plan-4 既有檔案**（merge 進 main 後直接動）：
- `src/dollos/llm/templates.py`（Qwen3PlainTemplate）— 不動
- `src/dollos/config.py`（InnerVoiceConfig）— 不動
- `tests/test_llm_templates.py` — 不動
- `config.example.toml`（[inner_voice] 段）— 不動

---

## §4 PromptRenderer 擴充

`src/dollos/prompts/renderer.py` 加 `render_blocks()`：

```python
def render_blocks(self, template_name: str, **ctx: object) -> dict[str, str]:
    """Render all `{% block %}` sections in the template, return as dict.

    Each block rendered with the same ctx; result keyed by block name.
    Trailing whitespace stripped per block.
    """
    template = self._env.get_template(f"{template_name}.jinja")
    ctx_obj = template.new_context(ctx)
    return {
        name: "".join(block(ctx_obj)).strip()
        for name, block in template.blocks.items()
    }
```

**設計重點**：
- 既有 `render()` 不動（single-block 模板繼續用）
- `new_context()` + `tmpl.blocks` 是 jinja2 公開 API
- `.strip()` 處理 jinja `{% block %}...{% endblock %}` 容易產生的前後空白
- 不檢查特定 block 名稱存在 — caller 拿 dict 自己取，缺了 `KeyError` bubble up

**`iv_recall.jinja`**：

```jinja
{%- block system -%}
You are Doll's memory recall helper. Read the query and candidate facts from memory, output ONLY the facts relevant to the query as bullets.

Rules:
- One bullet per relevant fact: "- <fact in concise prose>"
- If a candidate is irrelevant, skip it
- Do NOT add facts not in candidates
- Do NOT speculate or fill gaps
- Output bullets only. Don't repeat the query, don't add header.
- If no candidates are relevant, output a single line: (no relevant memories)
{%- endblock -%}

{%- block user -%}
Query: {{ query }}

Candidates:
{{ candidates }}
{%- endblock -%}
```

`{{ candidates }}` 由 InnerVoice 預先 join 成 `"1. ...\n2. ...\n"` 字串傳入。

---

## §5 InnerVoice 改造

`src/dollos/inner_voice.py`：

```python
"""InnerVoice — small-model VoM RECALL block synthesizer."""

from dollos.llm.adapter import LLMAdapter
from dollos.memory.store import Memory
from dollos.prompts import PromptRenderer


class InnerVoice:
    """Synthesize VoM RECALL blocks from memory using a small LLM."""

    def __init__(
        self,
        memory: Memory,
        llm: LLMAdapter,
        renderer: PromptRenderer,
    ) -> None:
        self._memory = memory
        self._llm = llm
        self._renderer = renderer

    async def recall(
        self,
        query: str,
        *,
        character_id: str | None = None,
        top_k: int = 10,
    ) -> str:
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
        blocks = self._renderer.render_blocks(
            "iv_recall",
            query=query,
            candidates=candidates,
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

**改動**：
- 移除 `INNER_VOICE_SYSTEM_PROMPT` 常量（搬進 `iv_recall.jinja`）
- 多一個依賴 `PromptRenderer`（建構子注入）
- `recall()` 內改用 `renderer.render_blocks("iv_recall", ...)` 取 system / user

**`build_inner_voice()` factory**（`kernel.py`）— plan-4 版本要加 renderer 參數：

```python
def build_inner_voice(
    settings: Settings, memory: Memory, renderer: PromptRenderer
) -> InnerVoice:
    provider = LlamaCppProvider(
        base_url=settings.inner_voice.base_url,
        timeout_s=settings.inner_voice.timeout_s,
    )
    llm = ComposedLLMAdapter(provider=provider, template=Qwen3PlainTemplate())
    return InnerVoice(memory=memory, llm=llm, renderer=renderer)
```

---

## §6 Kernel Wiring

`src/dollos/kernel.py`：

```python
class DollOS:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.adapter = build_adapter(settings)
        self.renderer = PromptRenderer()
        self.memory = build_memory(settings)
        self.inner_voice = build_inner_voice(
            settings, self.memory, self.renderer
        )
        self._character_profile = settings.character.profile_path.read_text()
        self.server = WebSocketServer(...)
        self._shutdown = asyncio.Event()

    async def _handle_text_input(self, msg: TextInput) -> AsyncIterator[ServerMessage]:
        try:
            system = self.renderer.render(
                "scaffolding", character=self._character_profile
            )
            recall = await self.inner_voice.recall(msg.text)
            prefill = f"<think>\n{recall}GOAL: "
            async for chunk in self.adapter.stream_completion(
                system=system,
                user=msg.text,
                prefill=prefill,
            ):
                if chunk.text:
                    yield TextChunk(text=chunk.text)
                if chunk.done:
                    break
            yield TurnEnd()
        except Exception as e:
            logger.exception("handler error")
            yield ErrorMsg(message=f"handler error: {e}")
```

**新增 `build_memory(settings)`**：Plan 2 已有 Memory + Embedder 構造邏輯但目前沒在 kernel wire。要新增 factory 把 `MemoryConfig` + `EmbedderConfig` → `Memory` instance。具體形式照 Plan 2 既有 API（embedder + sqlite path）。

**錯誤路徑**：
- recall 失敗（小模型 down / timeout）→ exception bubble up → 既有 `except Exception` 攔下 → 回 `ErrorMsg`
- recall 成功但大模型失敗 → 同上
- 沒有靜默退化（符合 no-fallback 原則）

**啟動成本**：kernel 構造時不打小模型 endpoint（lazy）— 只有第一次 user 訊息進來才連線。如果連不上會在那次 turn 失敗，可接受（避免啟動時硬綁兩個 llama-server 都活著）。

---

## §7 測試策略

| 測試檔 | 範圍 |
|---|---|
| `tests/test_prompts_renderer.py` | 加 4 個測試對 `render_blocks()`：(a) 多 block 模板回正確 dict (b) 單 block 模板也能用 (c) ctx 變數注入正確 (d) 空白 strip 正確 |
| `tests/test_inner_voice.py` | 改寫 plan-4 既有測試：(a) 寫 5 facts → recall 回 `"RECALL:\n..."` 起頭 (b) empty memory → `"RECALL:\n(no relevant memories)\n"` (c) character_id 透傳給 Memory.search (d) renderer 收到 query / candidates ctx (e) 大模型輸出被 strip 後嵌入 |
| `tests/test_kernel.py` | （新檔或擴既有）整合測試：mock InnerVoice.recall 回固定字串 + mock LLMAdapter → 驗證 prefill 確實是 `"<think>\n{recall}GOAL: "` 格式餵進大模型 |

**不測**：
- 真小模型 inference（manual smoke test，不入自動測）
- `iv_recall.jinja` 內容字面（信任 prompt 文案；只驗 ctx 注入機制）
- recall 失敗時 ErrorMsg 路徑（既有 generic except 已涵蓋，由 plan-4 既有路徑覆蓋）

**Fake LLMAdapter**（plan-4 已有，沿用）：yield 固定 chunks，捕捉 last system / user / prefill 用來 assert。

---

## §8 邊界與錯誤路徑

| 情境 | 行為 |
|---|---|
| Memory 完全空 | recall 回 `"RECALL:\n(no relevant memories)\n"` → 照常 prefill |
| Memory 有但小模型篩掉全部 | 取決於小模型輸出（應為 `(no relevant memories)`，否則照模型輸出 bullets） |
| 小模型 endpoint 連不上 | httpx exception bubble up → kernel 外層 except → ErrorMsg |
| 小模型超時 | `inner_voice.timeout_s` 觸發 → 同上 |
| 大模型 endpoint 連不上 | 同上（既有行為，無變化） |
| user 文字空字串 | recall 照跑（embedding 空字串 → top-K 多半無關 → 小模型回 (no relevant)）→ 照常 prefill |
| iv_recall.jinja 缺 system 或 user block | InnerVoice 拿 dict 時 `KeyError` bubble up → ErrorMsg。明確失敗，不靜默 |
| Memory schema 還沒 init（DB 不存在）| Plan 2 的 Memory init 階段失敗 → kernel 啟動就掛，不到 turn 階段 |

**設計原則**：
- 信任 Plan 2 Memory + Plan 4 InnerVoice 的契約，不在 kernel 層加防禦
- 所有失敗路徑統一走 `except Exception` → ErrorMsg
- 無 fallback / retry / 靜默退化

---

## §9 Non-goals（明確排除）

- character_id 真實 scoping — 等 step 10 Character Pack
- 自動寫 memory — 等 step 8 Memory 自動寫
- SELF_STATE block — 等 step 7（main spec §8 Self-First）
- GOAL 由小模型生成 — 等 step 5 Inner Voice full（first_instinct + emotion + summary）
- recall 結果快取 — v1 簡單，未來看實測
- empty memory 條件分支 — prefill 結構恆定
- recall 並行（在大模型 stream 開始的同時跑）— v1 串行；未來 step 4/5 event loop 化後再考慮
- per-character iv_recall prompt override — step 10 Character Pack 整合時再考慮

---

## §10 Open Questions（留 plan 階段或後續）

- **小模型輸出延遲**：實測 100-500ms 是估值。step 3 完成後做 manual smoke test，看真實延遲是否需要在 IPC 層加「思考中」訊號（目前沒有）— 留 step 4/5 處理
- **`(no relevant memories)` 大模型反應**：Doll 看到這串會怎麼處理？實測後若反應差，下個 plan iterate prompt 文案
- **`top_k=10` 是否合理**：plan-4 預設值，沿用。實測後可能調

---

## §11 Plan 整合策略

**重要前提**：`plan-4-inner-voice` branch 是在 step 2 merge 之前分出去的（merge-base 早於 step-2 的 `kernel.py` rename / `PromptRenderer` 等改動），需要先 rebase 到當前 main 才能繼續工作。

```bash
# 1. 先 rebase plan-4 到 main（解可能的衝突，主要是 daemon.py → kernel.py rename）
git checkout plan-4-inner-voice
git rebase main
# 解衝突，跑 pytest 確認 plan-4 重建後仍綠

# 2. 從 rebased plan-4 開 worktree
git worktree add .worktrees/vom-integration -b vom-integration plan-4-inner-voice
```

step 3 的 commits 累積在 `vom-integration` branch 上，跑完用 `superpowers:finishing-a-development-branch` 整條 merge 進 main（rebased plan-4 + step-3 一起進）。

**保留 plan-4 commit 歷史**（5 個 commit 都是合理拆分），不 squash。

---

## §12 Plan Task 預估（5 tasks）

> writing-plans 會展開細節。

0. （prep, 非 numbered task）rebase `plan-4-inner-voice` 到 main，解衝突，驗 pytest 綠 → 開 `vom-integration` worktree
1. `PromptRenderer.render_blocks()` + `tests/test_prompts_renderer.py` 擴充
2. `iv_recall.jinja` 模板（system + user blocks）
3. `InnerVoice` 改造（吃 PromptRenderer 依賴 + 用 render_blocks）+ `tests/test_inner_voice.py` 改寫
4. `kernel.py` wire `build_memory()` + `build_inner_voice()` + handler 改 prefill + `tests/test_kernel.py`
5. Manual smoke test（真小模型 + 真大模型 + 預先塞 memory）

預估比 Plan 4 短 — 多數工作是 wiring，無新概念。

---

## §13 後續 Plan 連動

- **Step 4（event loop）**：`_handle_text_input` 改 push `UserTextEvent` 進 queue，DollLoop pop 出來後跑同樣的「recall + LLM call + stream」邏輯。VoM 路徑不變
- **Step 5（Inner Voice full）**：Inner Voice 多 `process(event)` capability 產 first_instinct + emotion + summary。SELF_STATE block 跟 RECALL block 並列進 prefill；GOAL 可能改由小模型寫
- **Step 7（reflex + bracket loop）**：recall 變成可被大模型主動呼叫的 internal tool（`recall(query)`），不只開頭一次
- **Step 10（Character Pack）**：character_id 從 character pack 載入 → 透傳進 `inner_voice.recall(...)`；`iv_recall.jinja` 可能變成可被 character pack override 的模板
