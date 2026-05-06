# Success-cascade + Shell — Design

**日期：** 2026-05-06
**狀態：** 草案（待使用者最終審閱）
**範圍：** Roadmap 重切後的 step 9——擴展 step 7 cascade 機制以涵蓋 **success path**（tool 成功且回傳內容時也 cascade），加入第一個 returning tool **`Shell`**（fresh subprocess 執行 bash command、stdout+stderr 合併回流到 Doll perception）。為後續 step 10 Skills system / InvokeSkill 等 returning tool 鋪基礎。
**對齊：**
- `2026-05-05-tool-calling-design.md`（步 6 pydantic Tool）
- `2026-05-05-cascade-design.md`（步 7 fail-cascade — 本 spec 擴它）
- `docs/research/reflex-2026-05-06.md`（reflex 已 deferred；step 9 不做）
- `2026-05-06-memory-autowrite-diary-design.md`（步 8 transcript / diary）

---

## §1 設計原則

1. **Cascade unification**：step 7 用 `ToolCallFailure` 只表達失敗。step 9 升級成 `ToolResult`，**success / fail 都是同形狀的 cascade event**——dispatcher 一視同仁處理。
2. **Tool 自決是否 cascade**：透過 `Tool.run` 簽名 `-> str | None` 表達——`None` = 不 cascade（純 side-effect tool 如 Say / NoteMemory / WriteDiary）；`str` = cascade with content（returning tool 如 Shell）。空字串 `""` 也 cascade（讓 Doll 看到「跑了但沒輸出」是有意義訊號）。
3. **完全信任 Doll**——選了 Q1 = A，**無 permission gate / 無 sandbox**。Shell 直接 `subprocess.run`，daemon 跑哪個 user 就是哪個 user 的權限。風險（prompt injection 等）由使用者自己承擔。
4. **Fresh subprocess（非持久 shell）**——每次 Shell call 開新 subprocess。`cd` 不 persist。簡單；持久 shell（像 Claude Code Bash tool）等之後 step 升級。
5. **Output 截斷**——避免單個巨大 output 把 prefill 撐爆。預設 8000 chars cap（head 4000 + `\n...[truncated N chars]...\n` + tail 4000）。
6. **No new tool ABC / no permission ClassVar**——step 6 砍掉的 `ClassVar permission` 仍不復活。Tool 抽象維持「pydantic BaseModel + run」。

---

## §2 `ToolResult` dataclass（取代 `ToolCallFailure`）

`src/dollos/dispatcher.py`：

```python
@dataclass
class ToolResult:
    """Tool execution result. Internal cascade primitive (not a RawEvent).

    success=False: mechanical fail (validation / unknown / runtime exception).
    success=True:  ran cleanly. detail = the str returned by Tool.run().
                   May be empty string (Tool ran but had no content to return).

    Failures always cascade (Doll should fix). Successes cascade iff
    Tool.run() returned a str (not None) — i.e., the tool author opted in.
    """

    tool_name: str
    success: bool
    detail: str
```

Step 7 的 `ToolCallFailure` rename + 加 `success` field。**所有現有 cascade test 改用 `ToolResult(success=False, ...)`**——行為不變。

---

## §3 `Tool.run` 簽名更新

```python
# 既有 tools (Say / NoteMemory / WriteDiary) 全部加顯式 None 回傳：

class Say(BaseModel):
    text: str = ...
    async def run(self, ctx: ToolCtx) -> None:
        ctx.sink.put_nowait(TextChunk(text=self.text))
        # transcript append (step 8) — 行為不變
        ...
        return None   # explicit; side-effect tool, no cascade

class NoteMemory(BaseModel):
    text: str = ...
    async def run(self, ctx: ToolCtx) -> None:
        # write + index_file — 行為不變
        ...
        return None

class WriteDiary(BaseModel):
    content: str = ...
    async def run(self, ctx: ToolCtx) -> None:
        # write daily — 行為不變
        ...
        return None
```

Returning tools（step 9 的 Shell、step 10 的 InvokeSkill 等）的 signature：

```python
class Shell(BaseModel):
    command: str = ...
    async def run(self, ctx: ToolCtx) -> str:
        # 跑 subprocess、回傳 output
        return combined_stdout_stderr_truncated
```

**不引入 `ReturningTool` ABC / 不加 ClassVar**——signature 自身表達語意。

---

## §4 `_dispatch_tool_call` 重寫

```python
async def _dispatch_tool_call(
    self, call: dict, ctx: ToolCtx
) -> ToolResult | None:
    """Execute a tool call. Returns ToolResult if cascade-worthy, None otherwise.

    Returns None when:
      - tool.run() returned None (side-effect tool, no cascade)
    Returns ToolResult when:
      - validation/unknown error (success=False, error in detail)
      - runtime exception (success=False, error in detail) — also pushes ErrorMsg to sink
      - tool.run() returned str (success=True, str in detail; may be empty)
    """
    name = call.get("name")
    if not isinstance(name, str):
        return ToolResult(
            tool_name=str(name), success=False,
            detail="missing or non-string 'name' field in tool_call",
        )
    tool_cls = self._tools_by_name.get(name)
    if tool_cls is None:
        logger.warning("unknown tool: %r", name)
        return ToolResult(tool_name=name, success=False, detail="unknown tool")
    try:
        tool = tool_cls.model_validate(call.get("arguments", {}))
    except ValidationError as e:
        logger.warning("tool args validation failed for %s: %s", name, e)
        return ToolResult(
            tool_name=name, success=False, detail=f"args validation: {e}"
        )
    try:
        returned = await tool.run(ctx)
    except Exception as e:
        logger.exception("tool %s raised", name)
        ctx.sink.put_nowait(ErrorMsg(message=f"tool {name} error: {e}"))
        return ToolResult(
            tool_name=name, success=False, detail=f"runtime error: {e}"
        )
    if returned is None:
        return None   # side-effect tool, no cascade
    return ToolResult(tool_name=name, success=True, detail=returned)
```

---

## §5 `_respond` cascade loop 更新

step 7 收 `fails: list[ToolCallFailure]`。step 9 改 `results: list[ToolResult]`：

```python
async def _respond(self, doll_event, summary, sink) -> None:
    iteration = 0
    while True:
        # ... build prompt + stream big model 同 step 7 ...

        results: list[ToolResult] = []

        async for chunk in self._adapter.stream_completion(...):
            for call in parser.feed(chunk.text):
                result = await self._dispatch_tool_call(call, ctx)
                if result is not None:
                    results.append(result)
            if chunk.done:
                break
        for call in parser.flush():
            result = await self._dispatch_tool_call(call, ctx)
            if result is not None:
                results.append(result)

        if not results:
            break   # 全 None（純 side-effect tool）→ turn 結束

        iteration += 1
        if iteration > MAX_CASCADE_DEPTH:
            sink.put_nowait(ErrorMsg(message=f"cascade exceeded MAX_CASCADE_DEPTH ({MAX_CASCADE_DEPTH})"))
            break

        doll_event = DollEvent(
            perception=self._format_results_perception(results, iteration),
            raw=doll_event.raw,
        )
        summary = await self._instinct.process(doll_event)

    sink.put_nowait(TurnEnd())
```

---

## §6 `_format_results_perception`（取代 `_format_fail_perception`）

```python
@staticmethod
def _format_results_perception(
    results: list[ToolResult], iteration: int
) -> str:
    lines = []
    for r in results:
        if r.success:
            if r.detail:
                lines.append(
                    f"你 call 了 {r.tool_name} tool 成功，回傳：\n{r.detail}"
                )
            else:
                lines.append(
                    f"你 call 了 {r.tool_name} tool 成功，無輸出。"
                )
        else:
            lines.append(f"你 call 了 {r.tool_name} tool 失敗：{r.detail}")
    lines.append(f"（這是 thread 的第 {iteration} 次重試）")
    return "\n\n".join(lines)
```

兩個 results 之間用空行分隔（避免 success detail 中含換行混淆 cascade）。

---

## §7 `Shell` tool

```python
# src/dollos/tools.py

import shlex
import subprocess

SHELL_DEFAULT_TIMEOUT_S = 30
SHELL_MAX_TIMEOUT_S = 300
SHELL_OUTPUT_MAX_CHARS = 8000


class Shell(BaseModel):
    """Execute a shell command. Returns combined stdout+stderr.

    Subprocess runs with the daemon's user permissions. Working directory
    starts at settings.data.root each call (cd does NOT persist between
    calls — each Shell invocation is a fresh subprocess).

    Use this for any system inspection (ls, cat, find, ps, ...) or any
    command-line task. Output is truncated to 8000 chars total if longer.
    """

    command: str = Field(
        description="The shell command to run (will be passed to bash -c)."
    )
    timeout_s: int = Field(
        default=SHELL_DEFAULT_TIMEOUT_S,
        ge=1,
        le=SHELL_MAX_TIMEOUT_S,
        description=(
            f"Seconds before timeout. Default {SHELL_DEFAULT_TIMEOUT_S}, "
            f"max {SHELL_MAX_TIMEOUT_S}."
        ),
    )

    async def run(self, ctx: ToolCtx) -> str:
        cwd = ctx.memory_root.parent   # = settings.data.root (data/)
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                ["bash", "-c", self.command],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired:
            return f"[shell timeout after {self.timeout_s}s]"

        combined = proc.stdout
        if proc.stderr:
            combined += proc.stderr
        prefix = f"[exit {proc.returncode}]\n"
        body = _truncate(combined, SHELL_OUTPUT_MAX_CHARS)
        return prefix + body


def _truncate(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    half = cap // 2
    head = text[:half]
    tail = text[-half:]
    dropped = len(text) - 2 * half
    return f"{head}\n...[truncated {dropped} chars]...\n{tail}"
```

加進 `TOOLS = [Say, NoteMemory, WriteDiary, Shell]`。

### 設計細節

- **Subprocess via `asyncio.to_thread`**：`subprocess.run` 是 sync API，包進 thread 不阻塞 event loop。
- **`bash -c command`**：完整 shell 解析（pipes、redirect、glob 都支援）。
- **`cwd = ctx.memory_root.parent`**：`memory_root` 已知是 `settings.data.root / "memory"`，所以 `parent` = `settings.data.root` = `data/`。Doll 想出去用 `cd /elsewhere && cmd`。
- **Timeout**：default 30s，Doll 可帶（通常複雜任務）。pydantic `ge=1, le=300` 限制 1–300 秒。
- **Combined stdout + stderr**：簡單；某些情境 stderr 重要（error message），合進來給 Doll 看。
- **`exit N` 前綴**：給 Doll 知道 exit code（0 成功 / 非 0 失敗），不必另外回 success=False。Tool 自身永遠 success=True 除非 timeout/raise；非零 exit 是 shell 自己的事。
- **Truncate 8000 chars**：head 4000 + tail 4000 + 中間 marker。避免巨大 output（`find /` 之類）撐爆 prefill。

### 失敗模式

| 情境 | 行為 |
|---|---|
| Command 執行成功 | `[exit 0]\n<output>` |
| Command 執行失敗（非 0 exit）| `[exit N]\n<output incl stderr>` — tool 仍 success=True |
| Timeout | `[shell timeout after Ns]` — tool 仍 success=True（不算 mechanical fail）|
| `subprocess.run` 自己 raise（極罕）| 走 `_dispatch_tool_call` 的 runtime exception 路徑 → success=False + ErrorMsg |
| Pydantic validation（command empty / timeout 越界）| success=False，cascade 給 Doll |

---

## §8 失敗模式總結

| 情境 | 處理 |
|---|---|
| Pure side-effect tool（Say/Note/Diary）run 完 | `return None` → 不 cascade |
| Returning tool（Shell）run 完且回傳 str | cascade with success=True |
| Returning tool 回傳 `""` | cascade with success=True，detail="" → perception 寫「成功，無輸出」 |
| Tool args validation fail | cascade with success=False |
| Unknown tool | cascade with success=False |
| Tool runtime exception | cascade with success=False + ErrorMsg 進 sink |
| Cascade depth > 50 | ErrorMsg + TurnEnd（同 step 7）|

---

## §9 `_dispatch_tool_call` direct-test 行為改變

step 7 加的 5 個 `test_dispatch_tool_call_*` tests：

| Test | step 7 行為 | step 9 行為 |
|---|---|---|
| Success（Say）| 回 `None` | 仍回 `None`（Say returns None → no cascade）|
| Unknown tool | 回 `ToolCallFailure` | 回 `ToolResult(success=False, detail="unknown tool")` |
| Validation error | 回 `ToolCallFailure` | 回 `ToolResult(success=False, detail="args validation: ...")` |
| Runtime error | 回 `ToolCallFailure` + ErrorMsg | 回 `ToolResult(success=False, detail="runtime error: ...")` + ErrorMsg |
| Non-string name | 回 `ToolCallFailure` | 回 `ToolResult(success=False, detail="missing or non-string ...")` |

Tests 改 import `ToolResult` 取代 `ToolCallFailure`，`isinstance(fail, ToolCallFailure)` 改 `isinstance(result, ToolResult) and not result.success`。

---

## §10 Tests

### `tests/test_dispatcher.py`（擴）

1. **既有 5 個 `_dispatch_tool_call` tests** — rename `ToolCallFailure` → `ToolResult` + 改 assertion 為 `success=False`
2. **既有 cascade tests** — `_format_fail_perception` 改 `_format_results_perception`；assertions 仍適用（fail-cascade 行為不變）
3. **新 success-cascade 測試**：fake adapter 第一輪 emit returning tool（fake `_TestReturning`）回 `"hello"` → 第二輪用 perception 含「成功，回傳：hello」
4. **空字串 cascade**：fake tool 回 `""` → perception 含「成功，無輸出」
5. **None return → 不 cascade**：fake tool 回 None → results 空、turn 結束（不重 invoke big model）

### `tests/test_tools.py`（擴）

1. `Shell` schema 含 `command: str` + `timeout_s: int`
2. `Shell.run("echo hi")` 在 tmp_path 跑、回 `"[exit 0]\nhi\n"`
3. `Shell.run("false")` → `"[exit 1]\n"` (or similar)
4. `Shell.run` cwd = `ctx.memory_root.parent`（驗：`pwd` 回 data root）
5. Output 超 cap → truncate marker 出現
6. Timeout（fake `command="sleep 5", timeout_s=1`）→ 回「shell timeout after 1s」
7. Pydantic timeout 越界（`timeout_s=500`）→ ValidationError
8. `Shell` 在 `TOOLS` list 中

### `tests/test_e2e.py`（擴）

完整 trace：UserTextEvent → 大模型 emit Shell → Shell exec → cascade → 大模型用結果接續 Say。

---

## §11 不做的（明確 out-of-scope）

- ❌ 持久 shell（每次 fresh subprocess；之後升級）
- ❌ Permission gate / approval flow（trust-only）
- ❌ Sandbox / chroot / nsjail
- ❌ Per-tool ClassVar metadata（permission / cascade flag — 都用 signature / 預設行為）
- ❌ ReadFile tool（Shell 的 `cat` 涵蓋；之後若加 Edit/Vision/permission scope 才需要）
- ❌ Skills system（step 10 排）
- ❌ Subagent / SearchWeb（之後 step）
- ❌ Output streaming during tool exec（buffered；shell 跑完才回）

---

## §12 已知限制 / Follow-ups

1. **Subprocess.run 是 blocking I/O via `to_thread`**——event loop 不被擋住但 thread pool 會佔；多 user 同時跑 long shell 可能耗 thread。Follow-up：用 `asyncio.create_subprocess_exec`。
2. **No persistence between Shell calls**——`cd /elsewhere` 第二次 call 不在那。Follow-up：升級成持久 shell（step 9b 或之後）。
3. **No env var control**——subprocess 繼承 daemon env。Follow-up：tool 加 `env: dict | None`。
4. **Output truncation 8000 字寫死**——Follow-up 進 config。
5. **Timeout cap 300s 寫死**——同上。
6. **Shell 跑長任務 daemon 可能 shutdown 中**——沒做 cancel；subprocess 繼續跑。Follow-up：在 daemon shutdown 時 kill 所有 in-flight Shell。
7. **Returning tool 沒 streaming**——一次 buffered 回。長 output 等很久才 cascade。
8. **Doll 可能不知道 cd 不 persist**——system prompt 沒講。Follow-up：在 Shell tool description 強調 / 加 system prompt 一句。

---

## §13 Demo 驗證

1. 跟 Doll 說「列一下 data/memory/transcripts/ 有什麼檔案」
2. Doll：emit `<tool_call>{"name":"Shell","arguments":{"command":"ls data/memory/transcripts/"}}</tool_call>`
3. Shell exec → return `"[exit 0]\n2026-05-06.md\n"`
4. Cascade：perception「你 call 了 Shell tool 成功，回傳：[exit 0]\n2026-05-06.md\n（這是 thread 的第 1 次重試）」
5. Doll 第二輪：emit `<tool_call>{"name":"Say","arguments":{"text":"今天 transcript 有 1 個檔案。"}}</tool_call>`
6. User 看到「今天 transcript 有 1 個檔案。」

**驗證點**：
- success-cascade 路徑通（perception 含「成功，回傳」）
- Shell exit code + stdout 正確回傳
- 一次 cascade 後 Doll 自然停（不再 emit tool call）
- IPC 序列乾淨
- transcript 累積 user input + Doll 最終 Say
