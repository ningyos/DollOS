# Plan: Replace VoM prefill wire format with RAG context + Recall tool

**Worktree**: `.worktrees/memory-rag-tool/`
**Branch**: `memory-rag-tool`
**Date**: 2026-05-08

## Why

Step 11 砍掉 STATE/RECALL prefill 注入後，IV.recall 跑完結果丟掉、Instinct.process 同樣浪費。Wire format 死了但 calls 還在。同時 LLM 從沒被訓練過 ReAct-style 結構化記憶 prefill；業界主流走 RAG context（注入 user msg）+ tool-based recall（Anthropic memory tool 路線）。Self-First / 兩層模型 / memsearch 架構保留，只換 wire format。

## Out of scope

- 砍 InnerVoice / Instinct class（架構保留，純不被 dispatcher 呼叫的 calls 才停）
- 改 grammar generator（自動從 TOOLS 推 — 加新 tool 會自動納入）
- character pack / .doll v3
- character.jinja 內容（人格層不寫 wire format）

## Changes

### 1. `src/dollos/inner_voice.py`

`InnerVoice.recall()` 返回值改：
- 不再產 `RECALL:\n` 前綴
- 不再 wrap `(no relevant memories)` 標籤
- 純粹返回 small-LLM filtered 過的 facts string（bullet list 或自然段，由 jinja 決定）
- 空結果返回 `""`（空字串，dispatcher 自行判斷）

iv_recall.jinja 對應更新：output 不要加 `RECALL:` 前綴。

### 2. `src/dollos/dispatcher.py`

`_respond` 內：

```python
recall_text = await self._inner_voice.recall(doll_event.perception)
if recall_text:
    framed_user = (
        "[Memory context]\n"
        f"{recall_text}\n\n"
        "[Message]\n"
        f"{doll_event.perception}"
    )
else:
    framed_user = (
        "[Memory context]\n"
        "(no relevant memory)\n\n"
        "[Message]\n"
        f"{doll_event.perception}"
    )
```

然後：
```python
async for chunk in self._adapter.stream_completion(
    system=system,
    user=framed_user,
    prefill="",
    tools=TOOLS,
    max_tokens=4096,
    grammar=grammar,
):
```

**移除 Instinct.process() 兩個 call**（L108, L209）。`_respond` signature 拿掉 `summary` 參數。`_handle` 不再 call instinct。Instinct class 留著、kernel 仍 build（架構保留）。

### 3. `src/dollos/tools.py`

新增 `Recall` pydantic tool：

```python
class Recall(BaseModel):
    """Search Doll's memory for relevant facts. Use when you need
    deeper context than the [Memory context] block already provides
    in this turn's perception."""
    
    query: str = Field(
        description="What to search for in memory. Specific keywords work best."
    )
    
    async def run(self, ctx: ToolCtx) -> str:
        # Direct memsearch (no small-LLM filter — explicit recall returns
        # raw hits so Doll can judge relevance herself).
        hits = await ctx.memsearch.search(self.query, top_k=5)
        if not hits:
            return "[no relevant memory]"
        return "\n".join(f"- {h['content']}" for h in hits)
```

加進 `TOOLS` list 結尾。Grammar generator 自動納入。

**Recall tool 走 raw hits 不 filter**（理由：dispatcher 那層已經做過 small-LLM filter 給 baseline；explicit Recall 是 Doll 想要 raw 看更多，filter 反而矛盾）。

### 4. `src/dollos/prompts/templates/scaffolding.jinja`

L22 「看到 entry 出現在 RECALL → call InvokeSkill」改寫：
- 移除「RECALL」字眼
- 改成講「skill entry 會出現在 [Memory context] block 或可用 Recall tool 找」

加一段教 Doll：
- 每個 message 的 `[Memory context]` block 是自動帶的相關記憶
- 想深挖更具體的事，主動 call Recall tool

### 5. `src/dollos/prompts/templates/iv_recall.jinja`

更新 small-LLM filter 指示：
- 不要產 `RECALL:` 前綴
- 直接給 plain bullet list 或精煉過的自然語言段
- 沒結果就回空字串

### 6. Tests

- `test_inner_voice.py`: assert returned string 不含 `RECALL:` 前綴；空結果 = ""
- `test_dispatcher.py`:
  - 移除 instinct.process / summary 相關斷言
  - 新增：assert adapter 收到的 `user` 含 `[Memory context]` 跟 `[Message]`
  - 新增：empty recall 仍有 `[Memory context]\n(no relevant memory)` block
- `test_tools.py`: `Recall` tool 單元測試（mock memsearch 回 hits → assert 格式 / 空 → `[no relevant memory]`）
- `test_llm_grammar.py`: 確認 grammar 自動含 `Recall` tool（加 1 行 assertion）
- `test_e2e.py`: 確認 prompt 含 `[Memory context]` block，不含 `RECALL:`

### 7. Stale 處理

- `instinct.py:5` docstring 改成「Class kept for future wake-gating / reflex; not currently consumed by dispatcher」
- `inner_voice.py:50-54` docstring 改成「Returns filtered facts as plain text. Dispatcher wraps result in [Memory context] block; do not add own labels.」

## Risks

- **Empty block 仍可能誘發幻覺**：user 已選擇明說「(no relevant memory)」。若 smoke 仍見幻覺，可改成完全不插 block。
- **iv_recall.jinja filter 質量**：小模型過濾品質決定 RAG context 有用程度。若 filter 過頭把好的也丟，要調 jinja prompt。
- **Recall tool race with auto-RAG**：Doll 第一 turn 看到自動 RAG 後可能還是 call Recall（多一 turn cost）。可接受，不然會少很多 agency。

## Acceptance

- [ ] `uv run pytest` 全綠
- [ ] Smoke T1-T8：T2「我喜歡喝什麼」走 RAG context（fresh data 仍可能空），T7「我剛才說了什麼」用 RAG context 回答（cold start 沒 prior turn 可能仍弱）
- [ ] 至少 1 個 turn 看到 Doll 主動 call `Recall` tool
- [ ] log 不再有「STATE:」「RECALL:」字眼出現在 prompt
