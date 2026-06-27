# Plan: Doll Workflow capability — replace Subagent with a single Workflow concept

## Context

This session the user watched Claude Code's **Workflow** tool repeatedly deliver value (multi-agent code review, fix orchestration, adversarial verification) — deterministic fan-out of sub-agents + synthesis, where only the *conclusion* returns to the controller and the intermediate noise stays out of its context.

DollOS today has the **building blocks** but no orchestration layer: `SubagentRunner` is a fine `agent()` primitive (isolated sub-cascade → structured `Report` → result re-enters as a perception), and subagents already run concurrently — but Doll must *manually* spawn N and juggle N noisy `ToolResultArrived` perceptions herself (model-driven, non-deterministic, context-polluting). There is no deterministic "fan-out → collect → synthesize → return one clean result" verb.

**Decision (user-confirmed):** give Doll a Workflow capability, and **consolidate to it as the SINGLE background-orchestration concept** — remove the standalone `SpawnSubagent` tool; a single worker becomes the N=1 degenerate case of a workflow. The proven per-agent engine is **reused, not rewritten**.

**Intended outcome:** Doll dispatches `SpawnWorkflow(tasks=[...], mode=..., synthesis=...)` fire-and-forget; a background `WorkflowRunner` deterministically fans the tasks out as parallel worker agents (reusing the subagent cascade engine), optionally runs an adversarial verify pass, runs a synthesis agent, and re-enters **one** `ToolResultArrived(tool="Workflow")` perception with the combined result. Honors every DollOS invariant (fire-and-forget / no-wait / event-loop / pydantic+GBNF).

## Scope

**v1:** `map_reduce` (parallel fan-out + optional synthesis) and `verify` (each worker result gets an independent adversarial skeptic pass before synthesis). **Deferred (note as future):** pipeline (multi-stage per item), loop-until-dry.

---

## Design

### 1. `SpawnWorkflow` tool — grammar-safe schema (`src/dollos/tools.py`)

The GBNF builder (`llm/templates.py::_build_tool_call_rule`) supports only `string` / `integer` / string-`Literal` enum / `array`-of-`$ref`(string|int fields). **No `bool`/`float`/`dict`.** `MAIN_TOOLS` uses the voice-first grammar (`include_optional=True`), so defaulted top-level fields are emittable. `WriteSchedule.entries: list[ScheduleEntryArg]` (`tools.py`) is the array-of-`$ref` precedent.

```python
class WorkflowTask(BaseModel):
    task: str = Field(description="Concrete instruction for one worker agent "
                      "(no character/memory context — just SUB_TOOLS + this string).")

class SpawnWorkflow(BaseModel):
    """Dispatch a background workflow: N parallel worker agents → optional synthesis.
    Fire-and-forget; the combined result returns as ONE new perception."""
    tasks: list[WorkflowTask] = Field(min_length=1, max_length=16)
    mode: Literal["map_reduce", "verify"] = "map_reduce"
    synthesis: str = Field(default="", description="Instruction for a final agent that "
                           "combines all worker results. Empty = return results as-is "
                           "(N=1 returns that worker's report raw).")
    timeout_s: int = Field(600, ge=1, le=1800)
```
- `tasks` → required array-of-`$ref` (only required string sub-field `task`; **optional sub-fields are dropped by the grammar**, so the runner uses the task index as label). `min/max_length` are pydantic-only → out-of-range yields a friendly validation error to Doll.
- `mode` enum, `synthesis` string, `timeout_s` integer → all emittable as optional suffixes. No bool/float/dict.
- `SpawnWorkflow.run(ctx)` mirrors today's `SpawnSubagent.run`: generate `workflow_id = "wf-"+uuid4()[:8]`, call `ctx.workflow_runner.spawn(workflow_id=..., tasks=[t.task for t in self.tasks], mode=self.mode, synthesis=self.synthesis or None, timeout_s=self.timeout_s, response_sink=None)`, `_record(ctx, "SpawnWorkflow", ...)`, return a dispatch-ack string ("結果會以新事件回來").

### 2. Extract the per-agent engine (`src/dollos/agent_engine.py`, new)

Move the body of `SubagentRunner._run_cascade` (`subagent.py:221-269`) verbatim into a reusable async function reused for task-agents, verify-skeptics, and the synthesis agent:

```python
async def run_agent(*, task, system, adapter, renderer, memory_root, memsearch,
                    transcripts_root, tool_output_store, shell_runner=None,
                    monitor_runner=None, tools=SUB_TOOLS, max_tokens=4096,
                    on_iter_end=None) -> dict | None:
    # fresh MindState(focus=task); MindCtx(workflow_runner=None) blocks nesting;
    # run_tool_cascade(grammar=build_qwen3_think_tool_grammar(tools), tools=tools,
    #   sink=None, on_iter_end=on_iter_end, check_early_exit=lambda i,c: c.agent_report is not None)
    # returns ctx.agent_report
```
`on_iter_end` is threaded into `run_tool_cascade` so worker cascades log to `CascadeLogger` (today they don't) — needed so the live smoke can observe the fan-out. (Builds on the cascade_log-in-live-loop fix just merged.)

### 3. `WorkflowRunner` (`src/dollos/workflow.py`, new)

Same construction/lifecycle shape as `SubagentRunner` (drop-in for kernel wiring + `stop()`). Constants: `MAX_WORKFLOW_CONCURRENCY = 8`, `MAX_AGENT_TIMEOUT_S = 300`, `WORKFLOW_PREVIEW_LINES = 15`.

- **`spawn(*, workflow_id, tasks, synthesis, mode, timeout_s, response_sink=None)`** — fire-and-forget `asyncio.create_task(self._run_with_timeout(...))`, tracked in `_tasks` set with `add_done_callback`. (Copy `subagent.py:87-102`.)
- **`_run_with_timeout`** — `asyncio.wait_for(self._run_workflow(...), timeout_s)`; `CancelledError` re-raises (stop path, no enqueue); `TimeoutError`/`Exception`/`None` → one `ToolResultArrived` perception, `tool="Workflow"`, `status` in `timeout|error|no_report`. Reuse the perception-build + `tool_output_store.write` + 15-line preview logic from `subagent.py:125-204`, changing `tool="Workflow"`, `task_id=workflow_id`, `task=f"workflow: {n} tasks, mode={mode}"`.
- **`_run_workflow`** — `sem = asyncio.Semaphore(8)`; `results = await asyncio.gather(*[self._run_one_task(i, t, mode, sem) for ...])`. Then: **N=1 + no synthesis** → return `results[0].report` raw (= old single-subagent behavior). **synthesis set** → run synthesis agent (§4), return its Report. **N>1, no synthesis** → deterministic roll-up Report (no extra LLM call): `status=ok` iff all tasks ok else `incomplete`; details = concatenated per-task summaries.
- **`_run_one_task(index, task, mode, sem)`** — `async with sem:` wrap each `run_agent` in `asyncio.wait_for(..., timeout=MAX_AGENT_TIMEOUT_S)` (per-agent cap so one hung agent can't starve the rest); on per-agent timeout/error → degraded report dict (`status="timeout"/"error"`) so partial failure is first-class. If `mode=="verify"` and report present → second `run_agent` whose task is an **adversarial** prompt ("Try to REFUTE this result … default to refuted if unsubstantiated"); attach as `report["verify"]`. Skeptics share the same semaphore.
- **`stop()`** — copy `subagent.py:104-110`.
- **No nesting** (two guards): workers run `SUB_TOOLS` (no `SpawnWorkflow` → no grammar path) AND `run_agent` sets `ctx.workflow_runner=None`.

### 4. Synthesis stage

Synthesis is another `run_agent` whose **task is built from the collected Reports** — each rendered as `[task i] status | summary | details-preview(15 lines) | ReadToolOutput id=out-xxxx`. Full per-task `details` are written to the **shared** `ToolOutputStore` (the synthesis agent's `ReadToolOutput` reads the same instance), so synthesis input stays bounded but pageable. Synthesis agent gets full `SUB_TOOLS` and must `Report`; its Report becomes the workflow result. System prompt: render existing `subagent_scaffolding` + a one-line synthesis framing prepended to the task (no new template needed; dedicated block optional).

### 5. Result → perception

One `ToolResultArrived` perception, `tool="Workflow"`, `task_id=workflow_id`, `status`, `summary`, `details`(synthesis) + `details_output_id`/`details_line_count` for paging — identical shape/keys to subagent's today, so `mind_prompt._percep_body` already renders it (enrich line to include `[status]`).

---

## Migration (file-level)

| File | Change |
|---|---|
| `src/dollos/tools.py` | Delete `SpawnSubagent`; add `WorkflowTask` + `SpawnWorkflow`; `MAIN_TOOLS` swap `SpawnSubagent`→`SpawnWorkflow`; `Report.run` writes `ctx.agent_report` (renamed); `SUB_TOOLS` unchanged (keeps `Report`, no spawn verb). |
| `src/dollos/mind/mind_ctx.py` | Rename `subagent_runner`→`workflow_runner` (type `WorkflowRunner`), `subagent_report`→`agent_report`; update docstring + TYPE_CHECKING import. |
| `src/dollos/agent_engine.py` | **New** — `run_agent(...)` (§2). |
| `src/dollos/workflow.py` | **New** — `WorkflowRunner` (§3). |
| `src/dollos/subagent.py` | **Delete** (engine relocated to agent_engine; runner replaced by workflow). |
| `src/dollos/kernel.py` | Import + build `self.workflow_runner = WorkflowRunner(... cascade_logger=self._cascade_logger)` (replaces SubagentRunner at ~249-259); `MindCtx(... workflow_runner=...)`; shutdown `await self.workflow_runner.stop()`. |
| `src/dollos/mind/mind_prompt.py` | `_percep_body` ToolResultArrived → `f"{tool} {task_id} [{status}]: {summary[:120]}"`. |
| `src/dollos/prompts/templates/scaffolding.jinja` | Replace `SpawnSubagent`/`Subagent` references with the Workflow concept (fire-and-forget N parallel agents + optional synthesis → one combined perception; N=1 = single worker; keep "Scratchpad-before-dispatch" + "no wait tool"). |
| `src/dollos/prompts/templates/subagent_scaffolding.jinja` | Reframe as "a worker agent inside one of Doll's workflows"; fix "(no SpawnSubagent tool)" → "(no SpawnWorkflow tool)". (Filename kept; rename to `agent_scaffolding.jinja` optional polish.) |

**Rename ripple:** every reader of `ctx.subagent_runner`/`ctx.subagent_report` → `workflow_runner`/`agent_report` (confirmed readers: tools.py, mind_ctx.py, kernel.py, tests; `mind_loop.py` only has an unrelated TODO).

## Concurrency & safety caps
- Per-workflow fan-out: `asyncio.Semaphore(8)` (shared by task + verify agents).
- Total tasks: `max_length=16` on `tasks` → friendly validation error (no silent truncation).
- Per-agent timeout `MAX_AGENT_TIMEOUT_S=300` (inside `_run_one_task`) + whole-workflow `timeout_s` (default 600, max 1800) in `_run_with_timeout`.
- No nesting (SUB_TOOLS + `workflow_runner=None`). Graceful `stop()` cancels all `_tasks`.

---

## Testing

- **`tests/test_workflow.py`** (migrate `tests/test_subagent.py` helpers `_ScriptedAdapter`/`_FakeMemSearch`/`_wait_for_tool_result`): N=1 paths (Report→result, timeout, error, no_report, stuck-tool, details-paging, `stop()` cancels) recast as 1-task workflows; **new:** (a) N tasks run concurrently, all reports collected; (b) semaphore caps in-flight ≤ 8 (spawn 12, count peak via counting fake adapter); (c) synthesis → synthesis task text contains each task summary, exactly one `ToolResultArrived(tool="Workflow")`; (d) `mode="verify"` → agent invocations == N + N + 1; (e) N=1 no-synthesis → raw single report; (f) partial failure → degraded task feeds synthesis, status roll-up correct; (g) no-nesting guard (`ctx.workflow_runner is None` in workers); (h) per-agent timeout degrades one task without killing others; (i) exactly one perception per workflow.
- **`tests/test_tools.py`**: `SpawnWorkflow in MAIN_TOOLS`, `SpawnSubagent` gone; schema fields/defaults/`max_length`; `.run` delegates to `ctx.workflow_runner.spawn`; `Report.run` writes `ctx.agent_report`; rename `_FakeSubagentRunner`→`_FakeWorkflowRunner`, `_make_ctx(subagent_runner=)`→`workflow_runner=`.
- **`tests/test_llm_grammar.py`**: `spawn-subagent-call`→`spawn-workflow-call`; voice-first grammar over `MAIN_TOOLS` builds (no `NotImplementedError`); assert `spawn-workflow-tasks-array`/`-item` rules + `\"task\":` + optional `mode`/`synthesis` suffixes; sub grammar excludes `SpawnWorkflow`.
- **`tests/test_kernel.py`**: kernel sets `workflow_runner` + `mind_ctx.workflow_runner`; shutdown awaits `workflow_runner.stop()`.

## Verification (end-to-end)
1. `uv run pytest tests/test_workflow.py tests/test_tools.py tests/test_llm_grammar.py tests/test_kernel.py -q` then full `uv run pytest -q`.
2. Grammar build smoke: `build_voice_first_grammar(MAIN_TOOLS)` succeeds (proves `SpawnWorkflow` is grammar-expressible).
3. **Live smoke** (`scripts/smoke_workflow.py`, isolated daemon on a spare IPC port + own data dir, real LLM at `http://127.0.0.1:8001`, **never the user's running 9876 instance**): drive a workflow-eliciting turn (e.g. "並行比較兩個檔再合併成總結"), assert exactly one `ToolResultArrived(tool="Workflow")` re-enters, and confirm `data/<smoke>/cascade_log/{date}.jsonl` shows multiple worker cascades (the fan-out). Tear down the daemon + clean the smoke data dir afterward.

## Risks / locked decisions
- **Synthesis context size** → bounded via summary+preview+`output_id` paging through shared `ToolOutputStore`.
- **Synthesis latency** → serial extra cascade after fan-out; covered by whole-workflow timeout; acceptable (fully background).
- **Verify cost** ~2N+1 cascades; semaphore bounds concurrency, not total tokens — acceptable for v1.
- **Status roll-up** (locked): workflow `status=ok` iff synthesis (or all tasks, no-synthesis) returned ok; any task timeout/error → `incomplete`. Single documented helper.
- **Array-item optionals unsupported** → `WorkflowTask` keeps only required `task: str`; label = task index.
- **Coordination note:** `.worktrees/agent-service` holds future A0–A4 "agent-service / fork-subagent" specs; the `workflow_runner`/`agent_report` rename should be noted there to avoid a future merge clash.
- **Deferred:** pipeline, loop-until-dry (future workflow modes).
