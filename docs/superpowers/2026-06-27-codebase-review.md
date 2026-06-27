# DollOS Daemon — Whole-Codebase Review

Scope: `src/dollos` (~10.5k LOC, 9 subsystems). Findings below survived adversarial verification; minor items are flagged but not independently re-verified. Severities reflect the **adjusted** verdict, not the initial draft.

## 1. Executive Summary

**Overall health: solid.** No data-loss or crash bugs in the steady-state happy path. The defects cluster in two failure modes that the architecture is specifically meant to avoid: (a) **async task / subprocess lifecycle gaps** that leak resources and zombie sinks across connect-disconnect cycles, and (b) **silent failures** that violate the project's no-fallback rule — features that quietly stop working or swallow errors instead of surfacing them.

Counts (after de-duplication and severity adjustment):

| Severity | Count |
|---|---|
| Critical | 1 |
| Important | 9 |
| Minor | 27 |

De-duplicated: the SinkResolver pop-by-index bug was filed twice (mind-core "important" + ipc-wal "critical") → one **critical**. The TTSObservingSink untracked-task bug was filed twice (voice "critical" + ipc-wal "important") → one **important** (both verdicts downgraded the crash claim to a task leak). The `read_recent` dead assignment was filed twice (memory-observability + perception) → one minor.

**Recurring themes (cut across subsystems):**

1. **Async lifecycle hygiene is the dominant defect class.** Untracked `create_task`, `cancel()` without `await`, and missing `proc.wait()` / `stream.aclose()` appear in SinkResolver, TTSObservingSink, PerceptionQueue.drain, `_stream_one_pass`, MonitorRunner, `system_pulse._run_cmd`, the TTS producer threads, and `handle_offer`. The one critical and several important findings all trace to the same root: resources created per-connection/per-turn are not deterministically torn down on the disconnect/cancel path.
2. **Silent failure vs. no-fallback rule.** Multiple paths swallow or hide errors rather than raising loudly: turn-end sentinel not emitted on render failure, schedule runner dying silently on a bad file, Qwen3 `except TypeError` retry, `daily_token_quota` truthiness masking a zero-config, GrepToolOutput not surfacing `re.error`. These directly contradict the documented "No fallback mechanisms" convention.
3. **LLM-facing data silently dropped or wrong.** `slugify_focus` kills the entire mood axis, `_compact_schema` drops Optional/enum type info, ShellRunner emits the wrong `task_id`. The model is fed incomplete or ambiguous information without any error.
4. **Refactoring leftovers / dead code.** Several dead assignments, unused fields, and unreachable branches — low risk individually, but a few (e.g. `consecutive_fails` default, `save_state` deque handling) are latent traps that will bite when adjacent code changes.

---

## 2. Critical Findings

### C1 — SinkResolver.unregister() pop-by-index corrupts all handles when a non-last client disconnects first
- **Area:** ipc-wal
- **File:** `src/dollos/mind/sink_resolver.py:29-43` (handles used in `src/dollos/kernel.py:467-485`)
- **What:** `register()` returns `len(stack)-1` (a positional index) as the handle; `unregister(handle)` does `list.pop(handle)`. The kernel stores each connection's index at connect time and replays it unchanged at disconnect. When clients A (handle 0) and B (handle 1) are both live and **A disconnects first**, `pop(0)` shifts B from index 1 to index 0, but the kernel still holds B→1. B's later `unregister(1)` evaluates `0 <= 1 < len([B]) == 1` → False → silent no-op. B's sink is never removed.
- **Why it matters:** The orphaned dead sink stays at the top of the stack. `__call__()` returns `stack[-1]` = B's dead `asyncio.Queue` (its pump already cancelled), so **all of Doll's output is `put_nowait`-ed into an unbounded, unread queue** — the live client receives nothing, and memory grows without bound. The trigger (UI + phone overlapping, earlier one closes first) is a normal DollOS usage pattern, so this is reliably reproducible in production. There is no compensating mechanism anywhere in the chain.
- **Fix:** Replace the list+index scheme with an int→sink dict keyed by a monotonic counter. `register()` increments the counter and stores; `unregister()` does `dict.pop(handle, None)`; `__call__()` returns `self._sinks[max(self._sinks)]` (most-recently-registered) or the dummy. Handles become stable regardless of removal order.

---

## 3. Important Findings (by subsystem)

### mind-core

**I1 — turn-end `None` separator not emitted when `render_mind()` / `render_tool_outcomes()` raises**
- `src/dollos/mind/mind_loop.py:200-229`
- The try/finally that guarantees `put_nowait(None)` (the IPC TurnEnd sentinel) starts *after* `render_tool_outcomes()` (200-205) and `render_mind()` (208). Those two synchronous calls are the only pre-LLM calls without their own try/except. If either raises (e.g. `AttributeError` on a malformed state field), the exception propagates to `run()`'s bare except (line 133) and is swallowed — the finally never runs, and any connected IPC client blocks forever waiting for a TurnEnd that never arrives.
- **Fix:** Move the try/finally up to wrap from line 200 (or at minimum 208) so `put_nowait(None)` always fires once per real turn.

### mind-features

**I2 — `slugify_focus` strips all CJK → mood-associative recall axis is permanently dead**
- `src/dollos/mind/context_tags.py:51`
- `_SLUG_STRIP = re.compile(r"[^a-z0-9-]+")` removes every non-ASCII char. `Mood.emotion` is always Chinese (default `"平靜"`; the tool description instructs the model to write a Chinese word). `slugify_focus("平靜")` → `None`, so `build_context_filter` never adds a `mood` key, `build_heading` never writes a `mood:` tag, and `associative_search`'s `if "mood" in ctx_tags` is always False. The mood axis is silently non-functional for every real input.
- **Fix:** Do **not** apply `slugify_focus` to the emotion field. Mood is free-form Chinese, not a URL slug. Sanitize only for tag-boundary safety: `mood_tag = mood.replace(" ", "_").replace("]", "")`, add to `tags["mood"]` when non-empty. Keep `slugify_focus` for the ASCII `focus` field only.

### tools-cascade

**I3 — `consecutive_fails` dict replaced wholesale on a new-tool failure, defeating the 3-strike stuck-tool abort**
- `src/dollos/cascade/tool_loop.py:231-237`
- The `else` branch does `consecutive_fails = {r.tool_name: 1}`, replacing the entire dict. In `[fail(A), fail(B)]` within one iteration, A's count is wiped when B is processed. Across iterations, if A reaches count 2 and the next iteration is `[fail(B), fail(A)]`, A resets to 1 and the abort never fires. The cascade has **no wall-clock timeout** — the stuck-tool check (the only `break` besides empty-results) is the sole forced-exit. Alternating failing tools can run the cascade unbounded, burning tokens.
- **Fix:** Preserve per-tool counts; only reset on a new streak and increment with `get(name, 0) + 1`. Also change the dead `get(..., 1)` default to `0` so the first increment counts correctly once the dict is no longer replaced.

### kernel-runners

**I4 — ShellRunner emits wrong `task_id` in success and error Perceptions**
- `src/dollos/shell_runner.py:136,163`
- `spawn()` generates `task_id = f"shell-{uuid…}"` and passes it through. The timeout path uses it correctly, but the success (136) and error (163) paths override it with `f"shell-{command[:20]}"`. Two concurrent runs of the same command (or a shared prefix) produce identical `task_id`s in their `ToolResultArrived` Perceptions — indistinguishable to Doll — and the format diverges from the timeout path's uuid form.
- **Fix:** Use the `task_id` parameter in both Perceptions.

**I5 — `_schedule_runner` task dies permanently on a malformed schedule file**
- `src/dollos/kernel.py:565`
- The try/except in `_schedule_runner` (550-556) covers only the `asyncio.wait_for`. `load_schedule(path)` at 565 runs outside any handler and can raise `TOMLDecodeError`, `KeyError` (missing `time`/`intent`), or `ValueError` (bad time string). Any of these kills `_schedule_task` permanently and is swallowed silently at shutdown (`return_exceptions=True`, no log). No `ScheduledMoment` fires for the rest of the run, with zero diagnostics. This is the *opposite* of no-fallback — it's silent permanent loss, not a loud raise.
- **Fix:** Wrap the `load_schedule` + due-entries loop in `try/except Exception` with `logger.warning(...)` and `continue`.

### voice

**I6 — TTSObservingSink spawns untracked `asyncio.create_task` that races `session.close()` → speak-worker task leak + lost final chunk**
- `src/dollos/voice/sink.py:51`
- `put_nowait` calls `asyncio.create_task(session.enqueue_speak(...))` and discards the handle. If a TextChunk arrives just before disconnect, the task runs during `close()`'s await points. With the speak worker already done, `enqueue_speak` sees `_speak_worker_task.done()` and lazy-starts a *new* worker (no `is_open` guard, session.py:207). After `close()` nulls the engine internals (`_tts`/`_synth`/`_voice = None` in every TTS `aclose`), that worker calls `synthesize()` → `AttributeError`. The worker's blanket `except` logs it (no daemon crash — the original critical framing was overstated), then loops back to `_speak_queue.get()` on an empty queue and **blocks forever — one leaked task per disconnect**, plus the final chunk's TTS is silently dropped.
- **Fix:** Track in-flight speak tasks in a set on the sink; add `cancel_speak_tasks()` and call it in `_handle_disconnect` before `session.close()`. Alternatively gate on `session.is_open` before creating the task.

**I7 — TTSObservingSink docstring says `speak()`, code calls `enqueue_speak()`; the test mocks the wrong method → zero TTS coverage**
- `src/dollos/voice/sink.py:29` and `tests/voice/test_sink.py:16,22`
- The class docstring promises `voice_session.speak(text)`; the implementation (line 51) calls `enqueue_speak`. The test sets `session.speak = AsyncMock()` and asserts `session.speak.assert_awaited_once_with("hello")`. Since `session` is a spec-less MagicMock, `enqueue_speak()` returns a MagicMock (not a coroutine), `create_task` raises `TypeError` (caught + logged), TTS never fires, and the `speak` assertion fails outright. The test provides no evidence TTS fires on a TextChunk — it masks the I6 wiring bug.
- **Fix:** Correct the docstring to `enqueue_speak`; in the test use `session.enqueue_speak = AsyncMock()` and assert on it.

**I8 — `Qwen3TTSEngine.synthesize` silently ignores `instruction` in the `voice_clone_prompt` path**
- `src/dollos/voice/tts_qwen3.py:175`
- `prefixed_text = f"{instruction}. {text}"` is built (170-172) but the `voice_clone_prompt` branch (175-180) passes raw `text` with no `instruction` kwarg and returns early — `prefixed_text` is unreachable there. The `ref_audio` branch honors instruction. A user configuring `voice_clone_prompt_path` + `instruction` gets the instruction silently dropped (flat affect, no error).
- **Fix:** Pass `text=prefixed_text` in the `voice_clone_prompt` branch, matching the ref_audio path.

**I9 — `Qwen3TTSEngine` `except TypeError` retry catches *all* model TypeErrors, not just missing-kwarg; violates no-fallback**
- `src/dollos/voice/tts_qwen3.py:189`
- The `try/except TypeError` wraps the whole `generate_voice_clone` call to handle "older API without `instruction` kwarg". But any `TypeError` from inside the model (wrong tensor shape/dtype, `None` passed where a str is expected) is caught and silently retried with different args — masking the real cause and degrading silently at synth time.
- **Fix:** Remove the runtime try/except. At `__init__`, probe `inspect.signature(model.generate_voice_clone)` once, store `self._has_instruction_kwarg`, and branch on it — fail fast on unknown API shapes.

---

## 4. Minor Findings

- **`PerceptionQueue.drain()`** (`mind/perception_queue.py:57`) — cancels pending tasks but never awaits them → "Task was destroyed but it is pending!" leak per turn. Add `await asyncio.gather(*pending, return_exceptions=True)`.
- **`_stream_one_pass`** (`mind/mind_loop.py:515`) — LLM async generator not `aclose()`d on mid-stream cancel; HTTP SSE connection held until GC. Wrap in `try/finally: await stream.aclose()` (or `contextlib.aclosing`).
- **`save_state`** (`mind/mind_state.py:150`) — `asdict(state)` deep-copies deques only to override 4 fields; a future deque field without an override makes `json.dump` raise inside the swallow-and-return-False block → WAL grows unbounded silently. Build `state_dict` field-by-field instead.
- **`tool_stats` dict** (`mind/mind_state.py:115`) — only uncapped collection in MindState; grows one entry per distinct tool name across restarts. Add LRU cap (~50).
- **`_compact_schema` Optional/anyOf** (`llm/templates.py:47-59`) — drops `type` for `Optional[X]` fields (anyOf pattern); affects `SpawnMonitor.match_regex`, `Recall.since/until`. No live impact (grammar excludes optionals), but fix for schema completeness.
- **`_compact_schema` enum** (`llm/templates.py:47-59`) — drops `enum` for `Literal` fields (`Report.status`, `Scratchpad.op`). No live impact (GBNF enforces enums; descriptions name the values), fix for completeness.
- **`bm25()` computed twice** (`memory/fts_store.py:216`) — `ORDER BY bm25(chunks)` recomputes instead of reusing the `_bm25` alias; O(n) extra calls (not O(n log n)). Change to `ORDER BY _bm25`.
- **MonitorRunner CancelledError** (`monitor_runner.py:192-198`) — SIGKILLs the process group but doesn't `await proc.wait()` (unlike ShellRunner). Zombie/FD cleanup is actually handled by SIGCHLD + refcount in `stop()`, but align with ShellRunner for clarity.
- **`system_pulse._run_cmd`** (`perception/system_pulse.py:144`) — CancelledError (BaseException) escapes the `except Exception`, skipping `proc.kill()`. Short-lived query subprocesses self-terminate, so impact is negligible; add `proc = None` + `finally: proc.kill()` for correctness.
- **`wait_speak_idle`** (`voice/session.py:211`) — empty queue ≠ synthesis done; 50ms grace is unreliable for slow engines. All current callers are safe (use `_stop_speak_worker`), so dormant. Replace polling with a `_speak_idle` asyncio.Event for future callers.
- **`GrepToolOutput.run`** (`tools.py:574-578`) — doesn't catch `re.error` (SpawnMonitor does); LLM gets an opaque runtime error instead of an actionable "regex error" message.
- **`dispatch_one` error_sink** (`cascade/tool_loop.py:104-105`) — `ctx.sink` is hardcoded `None`, so the error_sink push is permanently dead code. Remove the parameter and dead branch.
- **`_flush_legacy`** (`tool_parser.py:193`) — `or self._inside_buf` clause is unreachable (`_inside_buf` always `""` when OUTSIDE). Simplify to `if self._state is _State.INSIDE:`.
- **`SubagentRunner._run_cascade`** (`subagent.py:225`) — rebuilds `sub_tool_registry` per call, identical to `self._tools_by_name`. Reuse the latter.
- **`_resolve_ref`** (`llm/templates.py:252`) — bare `schema["$defs"]` raises KeyError (not NotImplementedError) when `$defs` absent; confusing traceback. Use `.get()` + explicit NotImplementedError.
- **`read_recent` dead assignment** (`telemetry/llm_calls.py:108`) — `cutoff = recs[-1].ts - seconds` immediately overwritten by absolute-clock cutoff (comment admits it). Delete line 108; also hoist in-function `import time` to module level. *(Filed twice — same defect from memory-observability and perception areas.)*
- **`CascadeLogger._log_root`** (`cascade_log.py:42`) — assigned, never read (path controlled by `configure_cascade_logging`). Remove the field and ctor param.
- **`DataConfig`/`CharacterConfig` `_expand_user`** (`config.py:41,87`) — only expands `str`, skips `Path('~/...')`. Change guard to `isinstance(v, (str, Path))`.
- **`tool_outputs.line_count`** (`tool_outputs.py:56`) — `.__len__()` dunder call instead of `len()`. Cosmetic.
- **`_fired_today` / `_bootstrapped_dates`** (`kernel.py:344,569`) — one set/entry per calendar day, never pruned (unbounded over months). Prune keys `< today` in `_schedule_runner`.
- **`CognitionWorker.snapshot()`** (`perception/cognition.py:185`) — calls `read_today()` twice in one sync stack (always identical). Return records from `_compute()` and reuse.
- **`daily_token_quota` truthiness** (`perception/cognition.py:100,140`) — `if self.daily_token_quota` treats `0` as "disabled", silently masking a misconfig (and a would-be ZeroDivisionError). Validate `> 0 or None` in `__init__`; switch guards to `is not None`.
- **ipc pump task name** (`ipc/server.py:91`) — all pumps named `"ipc-pump"`; indistinguishable under multiple connections. Include `ws.remote_address` in the name.
- **fish-tts / piper producer threads** (`voice/tts_fish.py:116`, tts_piper) — daemon thread keeps `call_soon_threadsafe(queue.put_nowait, ...)` after generator cancel; full maxsize=64 queue → `QueueFull` as an unhandled event-loop exception. Pass a `threading.Event` stop flag or use an unbounded queue.
- **`handle_offer`** (`voice/session.py:107`) — re-offer overwrites `_inbound_consumer_task` without cancelling the prior one; old track keeps appending to `_utterance_buffer`. Cancel existing task and clear the buffer on new offer.

---

## 5. Cross-Cutting Observations & Recommended Next Actions

1. **Adopt a single "spawned task" discipline.** The critical + several important + many minor findings are the same anti-pattern: a task/subprocess/generator/queue created per connection or per turn, then not deterministically cancelled/closed on the teardown path. Recommend a small `TaskGroup`-style owner (or a tracked-set + `cancel_all()` helper) used uniformly by SinkResolver, TTSObservingSink, the runners, and the voice session. This is the highest-leverage refactor — it retires C1, I1, I6, and ~6 minor items at once.

2. **Enforce the no-fallback rule on error paths, not just on capability gaps.** I5, I9, and the `daily_token_quota` / GrepToolOutput minors all *silently* degrade. The convention is documented as "state boundaries clearly; raise loudly." Add a lint/review checklist item: every `except Exception` / truthiness-on-numeric-config / bare retry must either re-raise or log at WARNING+ with context.

3. **Treat the IPC TurnEnd sentinel and the sink stack as protocol invariants.** C1 and I1 both break the client's turn-boundary contract in ways the client cannot detect (it just hangs). Worth a focused integration test: two overlapping IPC connections with the earlier one closing first, plus a render-time exception, asserting the surviving client still receives `None`.

4. **The voice TTS dispatch path is under-tested.** I7 shows the one sink test asserts on a method the code never calls. Recommend a real coverage test (spec'd mock or a fake session) that proves `enqueue_speak` fires on a TextChunk, plus a disconnect-during-synthesis test that asserts no leaked speak-worker task — this guards I6/I7 together.

5. **Schema-rendering completeness (I-downgraded-to-minor I/enum + Optional).** Currently masked by GBNF. If you ever add an unconstrained tool-call path or expand the subagent grammar to include optionals, these silently-dropped types become real correctness bugs. Fix now while they're cheap.

**Suggested order:** C1 → I1/I6 (same task-lifecycle theme, do together) → I7 (locks in I6) → I3/I5/I8/I9 (independent correctness) → I2/I4 → minor cleanups batched by file.
