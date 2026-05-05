# Tool Calling — Design

**日期：** 2026-05-05
**狀態：** 草案（待使用者最終審閱）
**範圍：** Roadmap step 6 — 引入 tool calling 結構。大模型 `</think>` 後唯一合法輸出形式為 `<tool_call>{...}</tool_call>` JSON；dispatcher stream-parse 後 dispatch 到對應 pydantic-model tool 的 `run()`。第一批兩個 tool：`Say`（說話進 IPC sink）、`NoteMemory`（寫 daily markdown + memsearch `index_file`）。**單一大模型 round；tool 結果不回大模型**（cascade 留 step 7）。
**對齊主 spec：**
- `2026-05-01-dollos-pivot-to-computer-design.md`（§5 Tool / §6 Event 系統）
- `2026-05-05-event-loop-design.md`（dispatcher `_respond`，本 plan 在其上 fork tool 路徑）
- `2026-05-05-inner-voice-minimal-design.md`（STATE block 已存在，本 plan 不動）

---

## §1 設計原則

1. **Tool 結構統一**。大模型 `</think>` 後唯一合法輸出 = `<tool_call>` JSON。Naked text → log warning + 丟掉，不送進 IPC。「say 變 tool call」是這條的具體實現。
2. **Pydantic model = Tool**。Tool class 自己是 `BaseModel`，args 是 fields，docstring 是 description，schema 由 `model_json_schema()` derive。**單一 SoT**：name / schema / description / execution 全在一個 class。
3. **多 tool / round + stream-order execute**。大模型一個 round 可 emit 多個 `<tool_call>`；parser state machine 看到 `</tool_call>` 立刻 dispatch + `await` execute；後續 token 繼續 parse。對齊 Anthropic / OpenAI / Qwen3 native multi-tool 主流。
4. **單一大模型 round**。Tool 結果**不**回大模型。step 7 才做 cascade（tool result → ToolExecutedEvent → 大模型新一輪）。
5. **memsearch first-class coupling**。NoteMemory 寫完 markdown 立刻 `await memsearch.index_file(path)`——memsearch 公開 API，不寫自己的索引邏輯。
6. **YAGNI（最大化）**。無 ABC、無 ClassVar `permission`/`feedback`/`fast`/`streamable`、無 ToolRegistry class、無 `recall` tool（prefill RECALL 已自動）。step 7+ 真用時再加。
7. **No fallback**。失敗一律 log + 跳過該 tool；不重試、不偽造輸出、不阻擋後續 tool。

---

## §2 Tool 定義（pydantic P2 風格）

新檔 `src/dollos/tools.py`：

```python
import asyncio
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from memsearch import MemSearch
from pydantic import BaseModel, Field

from dollos.ipc.messages import ServerMessage, TextChunk


@dataclass
class ToolCtx:
    """Narrow execution context passed to Tool.run()."""

    sink: asyncio.Queue[ServerMessage | None]
    memory_root: Path
    memsearch: MemSearch


class Say(BaseModel):
    """Stream text to the user. Call this whenever Doll wants to speak."""

    text: str = Field(description="What Doll says to the user.")

    async def run(self, ctx: ToolCtx) -> None:
        ctx.sink.put_nowait(TextChunk(text=self.text))


class NoteMemory(BaseModel):
    """Record a fact into Doll's memory (daily markdown + memsearch index)."""

    text: str = Field(
        description="The fact to record. One sentence, declarative."
    )

    async def run(self, ctx: ToolCtx) -> None:
        path = ctx.memory_root / "shared" / f"{date.today():%Y-%m-%d}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        # Sync append + index inside async — append is a single small
        # write (microseconds), and index_file is the only slow op.
        # Wrapping the write in asyncio.to_thread is YAGNI for step 6.
        with path.open("a") as f:
            f.write(f"- {self.text}\n")
        await ctx.memsearch.index_file(path)


TOOLS: list[type[BaseModel]] = [Say, NoteMemory]
```

**為什麼 Tool 自己 = BaseModel**：
- Args / schema / description / execution 同一處——避免 schema 跟 handler 脫節
- pydantic 自然 validate 大模型 emit 的 JSON
- 加新 field（step 7 reflex 加 `permission: ClassVar[str]`）一行
- 對齊 Anthropic Agents SDK / pydantic-ai 主流

**為什麼 `ToolCtx` 是 dataclass 不是 dispatcher 注入**：narrow——Tool 只看到自己需要的 sink / memory_root / memsearch，不看到整個 kernel / character profile / adapter。

---

## §3 Template 擴展（Qwen3 native tool format）

`Qwen3ThinkingTemplate.render()` 加 `tools: list[type[BaseModel]] | None = None` 參數。

system prompt 渲染加一段（在原 character 之後）：

```
{character}

# Tools

You have tools. To call a tool, emit:
<tool_call>
{"name": "<tool_name>", "arguments": {<args>}}
</tool_call>

After </think>, output ONLY <tool_call> blocks. Plain text after </think> is invalid.

Available tools:
<tools>
{tool_schemas_json}
</tools>
```

`tool_schemas_json`：
```python
json.dumps([
    {
        "name": cls.__name__,
        "description": cls.__doc__,
        "parameters": cls.model_json_schema(),
    }
    for cls in tools
], ensure_ascii=False, indent=2)
```

當 `tools=None` 或 `[]` → 不渲染 Tools 區塊（向後相容測試）。

`Qwen3PlainTemplate`（小模型）**不支援 tools**——若呼叫者傳 `tools=` 給 plain template 應該 raise，避免誤用（Inner Voice / summary 不該用 tool）。

`PromptTemplate.render()` ABC 簽名擴一個 `tools=None` kwarg。

---

## §4 Stream parser

新模組 `src/dollos/tool_parser.py`：

```python
class ToolStreamParser:
    """State machine: accumulates stream chunks, yields parsed tool_call dicts.

    States:
      OUTSIDE         - not inside a tool_call (text dropped + DEBUG-logged)
      INSIDE          - between <tool_call> and </tool_call>; accumulating JSON

    Parser does NOT track `</think>` — all text outside <tool_call> is
    treated identically (dropped, logged at DEBUG). This includes the
    model's <think> reasoning content. Step 6 deliberately does not
    surface think content to the IPC sink.

    Rationale: a stateless drop-everything-outside policy is simpler than
    a think-aware logger and produces the same user-visible behavior.
    Operators wanting to see the model's raw stream enable DEBUG logs.
    """

    def feed(self, chunk: str) -> list[dict]:
        ...
```

Implementation: scan for `<tool_call>` open / `</tool_call>` close markers. Buffer between markers. On close → `json.loads()`，invalid JSON → log warning + skip。Output: list of parsed dicts (each with `name` + `arguments`).

**Pure sync, side-effect-free**（除 logging）。Dispatcher 是 caller。

### Edge cases

| 情境 | 行為 |
|---|---|
| `<tool_call>` 跨 chunk boundary | buffer + 等下個 chunk |
| 中文 / unicode 在 JSON | `json.loads` 處理（UTF-8） |
| Nested JSON 在 arguments | OK，`json.loads` 完整解析 |
| 兩個 `<tool_call>` 連著 | yield 兩個 dict |
| `<tool_call>` 開了沒關（stream 結束） | parser 提供 `flush()`，dispatcher 收尾時呼叫，未閉合 buffer log WARNING + drop |
| Naked text（含 `<think>` 內容）| state OUTSIDE 全 drop + DEBUG log |
| Malformed JSON in `<tool_call>` | log WARNING、跳過該 call、parser 重置回 OUTSIDE state |

---

## §5 Dispatcher 改 `_respond`

新流程：

```python
async def _respond(self, doll_event, summary, sink):
    recall = await self._inner_voice.recall(doll_event.perception)
    system = self._renderer.render("scaffolding", character=self._character_profile)
    state_block = f"STATE:\n{summary}\n\n" if summary else ""
    prefill = f"{state_block}{recall}DECISION: "

    parser = ToolStreamParser()
    ctx = ToolCtx(
        sink=sink,
        memory_root=self._memory_root,
        memsearch=self._memsearch,
    )
    tools_by_name = {cls.__name__: cls for cls in TOOLS}

    async for chunk in self._adapter.stream_completion(
        system=system,
        user=doll_event.perception,
        prefill=prefill,
        tools=TOOLS,
    ):
        for call in parser.feed(chunk.text):
            await self._dispatch_tool_call(call, tools_by_name, ctx)
        if chunk.done:
            break
    for call in parser.flush():
        await self._dispatch_tool_call(call, tools_by_name, ctx)
    ctx.sink.put_nowait(TurnEnd())


async def _dispatch_tool_call(self, call, tools_by_name, ctx):
    name = call.get("name")
    tool_cls = tools_by_name.get(name)
    if tool_cls is None:
        logger.warning("unknown tool: %s", name)
        return
    try:
        tool = tool_cls.model_validate(call.get("arguments", {}))
    except ValidationError as e:
        logger.warning("tool args validation failed for %s: %s", name, e)
        return
    try:
        await tool.run(ctx)
    except Exception as e:
        logger.exception("tool %s raised", name)
        ctx.sink.put_nowait(ErrorMsg(message=f"tool {name} error: {e}"))
```

EventDispatcher ctor 新增 `memory_root: Path` + `memsearch: MemSearch`（為了組 `ToolCtx`）。Kernel 在 build dispatcher 時注入。

---

## §6 LLMAdapter / Provider

`LLMAdapter.stream_completion` 簽名擴：

```python
async def stream_completion(
    self,
    *,
    system: str,
    user: str,
    prefill: str = "",
    stop: list[str] | None = None,
    max_tokens: int = 1024,
    tools: list[type[BaseModel]] | None = None,
) -> AsyncIterator[StreamChunk]:
```

`ComposedLLMAdapter` 把 `tools` 傳給 `template.render()`。`Qwen3PlainTemplate` 收到非 None / 非空 `tools` → raise `NotImplementedError`（plain template 不支援 tool calling）。

Transport 層（`LlamaCppProvider`）不變——只是把渲染好的 prompt 送進 `/completion`。

---

## §7 失敗模式

| 情境 | 處理 |
|---|---|
| 大模型 emit naked text（`</think>` 後）| parser log warning，drop |
| `<tool_call>` JSON malformed | parser log warning，跳過該 call，繼續 parse |
| Unknown tool name | dispatcher log warning，skip |
| Args validation fail（pydantic ValidationError）| dispatcher log warning，skip |
| Tool `.run()` raise | dispatcher log + ErrorMsg 進 sink，**後續 tool 繼續執行** |
| 大模型 stream 中斷（adapter raise）| 沿用 step 4 既有 `_handle` except 路徑（ErrorMsg + None sentinel） |
| `index_file()` raise | NoteMemory.run() 例外冒到 dispatcher → ErrorMsg。memory 檔已寫但 index 失敗——下個 daemon restart 重 index |

**全 surface 給 log**，無 fallback、無重試、無偽造輸出。

---

## §8 Tests

### `tests/test_tools.py`（新）
- `Say.run()`：`TextChunk(text=...)` 進 sink
- `NoteMemory.run()`：寫進 `{memory_root}/shared/{today}.md` 末尾 `- {text}\n`；`memsearch.index_file(path)` 被 await（fake memsearch 捕捉 calls）
- 兩 tool 的 pydantic schema 含 `text: str` field

### `tests/test_tool_parser.py`（新）
- 單一 `<tool_call>` → yield 一個 dict
- 兩個連續 `<tool_call>` → yield 兩個
- `<tool_call>` 跨 chunk → 第一個 chunk yield 空、第二個 chunk yield 完整
- Naked text → drop（捕 log）
- Malformed JSON → drop（捕 log）+ 後續 tool_call 不受影響
- 未閉合 → `flush()` log + drop

### `tests/test_llm_templates.py`（擴）
- `Qwen3ThinkingTemplate.render(..., tools=[Say, NoteMemory])` system prompt 含 `<tools>` 區塊 + JSON schema
- `Qwen3ThinkingTemplate.render(..., tools=None)` 不含 `<tools>` 區塊（向後相容）
- `Qwen3PlainTemplate.render(..., tools=[...])` raise `NotImplementedError`

### `tests/test_dispatcher.py`（擴）
- 一輪多 tool_call 順序執行、ctx 正確注入
- Naked text + tool_call 混雜——只 tool 執行
- Tool raise 不阻擋後續 tool
- 大模型 stream 端 → fake adapter 直接 yield 含 `<tool_call>` 的 chunks

### `tests/test_e2e.py`（擴）
- 完整 trace：UserTextEvent → 大模型 stream `<tool_call>Say</tool_call>` → IPC TextChunk + TurnEnd

---

## §9 不做的（明確 out-of-scope）

- ❌ ClassVar `permission` / `feedback` / `fast` / `streamable`（step 7+ 用時加）
- ❌ ToolRegistry class（一個 module-level list 夠）
- ❌ `recall` tool（prefill RECALL 已自動撈；step 8 自動寫 memory 後再評估是否需要主動 recall tool）
- ❌ Streaming JSON parser（say 文字 buffered，等完整 tool_call）
- ❌ Tool result cascade（step 7）
- ❌ Permission / approval flow（step 7 reflex review）
- ❌ Tool 並行 execute（stream-order 串行）
- ❌ Multi-character tool scoping（step 10）
- ❌ NoteMemory dedup / overwrite（step 8）
- ❌ NoteMemory metadata（timestamp / source / tags；step 8）

---

## §10 已知限制 / Follow-ups

1. **Buffered say latency**。大模型必須完整輸出 say 的 `text` JSON value 後 user 才看到。Qwen3.6 cont-batching 下對話典型 1–3 句通常 <2 秒；若實測延遲不可接受，follow-up：streaming JSON parser 或改用 grammar 強制 JSON 結構後做 token-level streaming。
2. **單一 markdown 檔 race**。多 client 並行 emit NoteMemory 寫同一個 daily 檔——append 模式 OS 層大致 atomic（小 write），但 race 可能造成 line interleave。step 6 接受；follow-up 用 lock 或寫 SQLite。
3. **memsearch index_file cost**。每 NoteMemory 都同步 reindex 該檔——目前小檔小 chunk 數可接受。檔案大時 latency 增。follow-up 評估 batch / debounce。
4. **Naked text 全丟**。模型訓練樣本若不嚴格遵守 tool_call 結構，user 會看到 Doll「沉默」。需在 system prompt 強化指示，並在 smoke test 觀察。
5. **Tools 是 module-level list**。step 10 character-scoped tools 來時要重構成 per-character registry。
6. **Tool args type 限制**。pydantic 支援 nested model；但目前兩 tool 都只用 `str`，未驗證 nested behavior。

---

## §11 Demo 驗證

打字 `「我等等想喝咖啡」` →
1. 大模型 stream（thinking 後）：
   ```
   <tool_call>
   {"name": "NoteMemory", "arguments": {"text": "主人提到等等想喝咖啡。"}}
   </tool_call>
   <tool_call>
   {"name": "Say", "arguments": {"text": "好的，要美式還是拿鐵？"}}
   </tool_call>
   ```
2. Parser 解析兩個 tool_calls
3. NoteMemory 寫 `data/memory/shared/2026-05-05.md` append `- 主人提到等等想喝咖啡。`，`index_file` 同步 reindex
4. Say 推 `好的，要美式還是拿鐵？` 進 sink
5. User 端看到回應

下個 turn 打字 `「你還記得我剛剛說什麼嗎」`：
- recall 撈到剛寫的 memory，prefill RECALL 含「主人提到等等想喝咖啡」
- 大模型回應引用該 memory

**驗證點**：
- IPC 序列只透過 `Say` tool 產出（`</think>` 後無 naked text）
- `data/memory/shared/{today}.md` 確實多一行 bullet
- 同一 daemon session 內下個 turn recall 撈得到（`index_file` 真有同步進索引）
