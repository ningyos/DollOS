# Agent Loop Frameworks Survey — 2026-05-05

**Purpose**: Inform design decisions for DollOS roadmap step 5+ (Inner Voice + cascade tool execution + multi-source event handling). Research conducted against official documentation and source code.

---

## 1. TL;DR Table

| Framework | Loop Shape | Cascade Mechanism | Concurrency Model | Termination | Non-User Events |
|---|---|---|---|---|---|
| **LangGraph** | Compiled graph (Pregel engine); nodes + edges form explicit cycles | `tools` node writes `ToolMessage` back to shared state; conditional edge routes back to `agent` node | `ToolNode` runs parallel tool calls; `Send` API for fan-out; subgraphs; per-thread checkpointing | Conditional edge returns `END`; no more tool calls in LLM response | No built-in timer/push; inject via `graph.invoke()` from any async caller; `Command` can route from any node |
| **OpenAI Agents SDK** | Sequential `while` loop inside `Runner.run()` | Tool results appended to message list; loop continues | Async (`Runner.run()`); tools run serially per turn by default; parallel tools possible | Model returns text with no tool calls, or `max_turns` exceeded | No native event sources; Temporal/Restate/DBOS recommended for durable/timer workflows |
| **Anthropic Tool Use / Tool Runner** | `while stop_reason == "tool_use"` loop (manual) or SDK `tool_runner` iterable | `tool_result` blocks appended as new `user` message; loop re-invokes model | Parallel tool calls within one turn supported; no multi-agent concurrency primitive | `stop_reason != "tool_use"` (model decides); no explicit turn cap in manual pattern | No native event sources; external events feed in as new conversation turns |
| **smolagents** | `for step in range(max_steps)` synchronous loop (ReAct); one thought + one action per step | Tool output appended as `observation` text in memory/log; model reads at next step | Mostly synchronous; `ToolCallingAgent` can execute multiple tool calls in parallel per step | `final_answer()` tool called, or `max_steps` reached | No native event sources; agent is invoked by outer caller per event |
| **pydantic-ai** | Finite-state machine via `pydantic-graph`; nodes: `UserPromptNode → ModelRequestNode → CallToolsNode` | `CallToolsNode` executes tools, returns results; FSM transitions back to `ModelRequestNode` | `max_concurrency` parameter; async throughout | Output type matches agent's declared output type; or `UsageLimits` exceeded | Streams `AgentStreamEvent` objects; no native timer/push sources |
| **AutoGen Core** | Actor model; `SingleThreadedAgentRuntime` processes messages via background queue; `RoutedAgent` dispatches to `@message_handler` methods by type | `ToolAgent` receives direct message, replies; calling agent receives reply and continues its own handler | True concurrency via `publish_message` (broadcast) and `send_message` (RPC); runtime queues all; distributed runtime for cross-process | No loop per se — agent handler returns; next message drives next step | **Best fit**: any external source (timer, webhook, drone result) posts a typed message to the runtime; runtime delivers it to subscribed agents |
| **CrewAI** | Sequential or hierarchical task list; `crew.kickoff()` iterates tasks; each task runs an inner per-agent ReAct loop | Tool result fed back into agent's next reasoning step within the task | Sequential by default; parallel tasks possible; hierarchical uses manager agent | Task `expected_output` matched; per-agent iteration cap | Flows (event-driven pipelines) as of 2025; still relatively high-level |

---

## 2. Per-Framework Detail

### 2.1 LangGraph

**Loop shape**: LangGraph compiles a `StateGraph` into a `CompiledGraph` backed by a Pregel message-passing engine. The graph is explicit: you add nodes (Python functions that read + write shared state), edges (unconditional `A → B`), and conditional edges (a function inspecting state returns the next node name). The typical ReAct agent loop is:

```
START → agent_node → [conditional] → tools_node → agent_node → ... → END
```

The conditional edge after `agent_node` checks whether the last `AIMessage` contains `tool_calls`; if yes it goes to `tools_node`, otherwise to `END`. Crucially, the "loop" is not a Python `while`; it is a graph cycle — LangGraph re-enters `agent_node` after `tools_node` via an ordinary edge (`graph.add_edge("tools", "agent")`).

**Tool cascade**: `ToolNode` (from `langgraph.prebuilt`) extracts tool calls from the latest message, runs them (concurrently), and writes `ToolMessage` objects back into the `messages` state channel. The state reducer (`add_messages`) appends them. Next time `agent_node` runs it sees the full history including tool results.

**Concurrency**: `ToolNode` runs parallel tool calls within one superstep. For fan-out to multiple independent sub-paths, `Send` objects returned from a conditional edge dispatch the same node with different state slices (map-reduce). Subgraphs allow encapsulating nested agents. Checkpointing via a `Checkpointer` saves state after every superstep, enabling human-in-the-loop pause/resume and durable long-running workflows.

**Non-user events**: LangGraph has no built-in timer or push event source. External events are fed in by calling `graph.invoke()` or `graph.astream()` from external code (e.g., a FastAPI endpoint receiving a webhook, or a scheduler calling `invoke` on a timer). The `Command` object can combine state mutation and routing in one return, enabling sophisticated injection mid-graph.

**Canonical reference**: `langchain-ai.github.io/langgraph` — graph-api overview, prebuilt ToolNode.

---

### 2.2 OpenAI Agents SDK

**Loop shape**: `Runner.run(agent, input)` executes a `while True` loop that:
1. Calls the LLM with current input.
2. Inspects the response: if final output → break; if handoff → switch active agent + update input; if tool calls → execute tools, append results, continue.

This is a straightforward agentic while-loop. The SDK offers both `Runner.run()` (async) and `Runner.run_sync()` (sync wrapper). Handoffs let the model delegate to a different `Agent` object mid-run, which LangGraph achieves via subgraphs or `Command(goto=...)`.

**Tool cascade**: Tool outputs are appended to the conversation message list as ordinary tool-result messages. The next LLM call receives the full updated history. There is no separate "event" concept — it is a linear accumulating conversation.

**Concurrency**: Per-run there is one active agent. Multiple independent runs can be launched as separate `asyncio` tasks. The SDK integrates with Temporal, Restate, and DBOS for durable, timer-driven, or distributed workflows — none of that is built in.

**Termination**: `max_turns` cap (raises `MaxTurnsExceeded`), or model returns a message with no tool calls and the declared output type is satisfied.

**Non-user events**: No native support. The recommendation in official docs is to use Temporal/Restate/DBOS as an outer scheduler that calls `Runner.run()`.

**Canonical reference**: `openai.github.io/openai-agents-python/running_agents/`

---

### 2.3 Anthropic Tool Use / Tool Runner

**Loop shape**: Anthropic's raw API pattern is a manual `while` loop:

```python
messages = [{"role": "user", "content": user_input}]
while True:
    response = client.messages.create(model=..., tools=tools, messages=messages)
    if response.stop_reason != "tool_use":
        break
    # execute tools, collect tool_result blocks
    messages.append({"role": "assistant", "content": response.content})
    messages.append({"role": "user", "content": [tool_result_blocks...]})
```

The SDK's `tool_runner` (beta) wraps this into an iterable that manages the loop automatically. Each iteration of the iterable is one LLM response; the runner detects tool calls, executes them, appends the `tool_result` user message, and calls the model again — all transparently.

**Tool cascade**: Tool results arrive as a new `user` message containing `tool_result` content blocks. The protocol requires these blocks to immediately follow the `assistant` message that requested them. Multiple parallel tool calls in one response are all collected and returned in one `user` message before the next model call.

**Concurrency**: Parallel tool calls within a single turn are fully supported — the model can emit multiple `tool_use` blocks in one response, and the caller executes them (concurrently if desired) and bundles all results into one reply. There is no multi-agent concurrency primitive; that is left to the application layer.

**Termination**: Model sets `stop_reason = "end_turn"` when it no longer needs tools. The manual loop breaks on that condition.

**Non-user events**: External events (timer, drone result) must be injected as new `user` messages into the conversation. The application is responsible for deciding whether to start a new conversation thread or append to an existing one.

**Canonical reference**: `platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls`, `platform.claude.com/docs/en/agents-and-tools/tool-use/tool-runner`

---

### 2.4 smolagents (HuggingFace)

**Loop shape**: `MultiStepAgent._run()` is a synchronous `for step in range(max_steps)` loop implementing ReAct. Each step: (1) serialize the agent's memory log to LLM-readable messages, (2) call the model, (3) parse the response to extract either a code block (`CodeAgent`) or structured tool call (`ToolCallingAgent`), (4) execute the tool/code, (5) append the observation back to memory. This continues until `final_answer()` is called or `max_steps` is reached.

**Tool cascade**: Tool output is appended as an `observation` string into the agent's in-memory log. On the next step, the model reads the full log. There is no separate event bus — the cascade is purely sequential within the same synchronous loop.

**Concurrency**: `ToolCallingAgent` can execute multiple tool calls per step in parallel. `CodeAgent` uses a sandboxed Python executor (E2B, Docker, Modal, Pyodide, or local). No cross-agent async concurrency at the `MultiStepAgent` level; multi-agent orchestration is done by composing agents as tools (one agent calls another as a `ManagedAgent` tool).

**Termination**: `final_answer(...)` tool call, or `max_steps` cap.

**Non-user events**: None. smolagents is a library, not a daemon. External events are fed in by calling `agent.run(task)` from outside code.

**Canonical reference**: `github.com/huggingface/smolagents/blob/main/src/smolagents/agents.py`

---

### 2.5 pydantic-ai

**Loop shape**: The agent uses `pydantic-graph`, a finite-state-machine library. Nodes are:
- `UserPromptNode` → processes initial input
- `ModelRequestNode` → calls the LLM
- `CallToolsNode` → executes any tool calls; transitions back to `ModelRequestNode` if more turns are needed

This is structurally similar to LangGraph but with a typed FSM instead of a compiled graph. The whole thing is async-native.

**Tool cascade**: `CallToolsNode` runs tools, packages results, and transitions back to `ModelRequestNode` for the model's next turn. By default, if the model produces a final output (type matches the declared output schema), the run ends even if tools were also called (behavior depends on `end_strategy`).

**Concurrency**: `max_concurrency` limits concurrent agent runs. The framework is async-native; tool execution within a run is sequential unless the user explicitly runs tools as async tasks.

**Termination**: First model output that matches the declared output type. `UsageLimits` (token budget, request count, tool call count) provide hard caps.

**Non-user events**: `AgentStreamEvent` objects (`PartStartEvent`, `PartDeltaEvent`, `FunctionToolCallEvent`, `FunctionToolResultEvent`) for observability during a run. No built-in event source for external pushes or timers.

**Canonical reference**: `pydantic.dev/docs/ai/core-concepts/agent/`

---

### 2.6 AutoGen Core (v0.4)

**Loop shape**: This is the most architecturally distinct framework. AutoGen Core v0.4 is a rewrite from scratch into an **actor model** with an async message-passing runtime. There is no per-agent while-loop; instead:

- The `SingleThreadedAgentRuntime` (or distributed runtime) maintains an internal message queue.
- `RoutedAgent` subclasses register `@message_handler` (or `@event` / `@rpc`) decorated async methods.
- When a message arrives, the runtime dispatches it to the matching handler method of the appropriate agent instance.
- Handlers can `await self.send_message(msg, target_agent_id)` (RPC — waits for reply) or `await self.publish_message(msg, topic_id)` (broadcast — fire and forget).

There is no top-level `while` loop visible to the agent author. The "loop" is emergent: handlers publish messages that trigger other agents' handlers.

**Tool cascade**: A common pattern is a `ToolAgent` that handles `FunctionCallMessage` via `@message_handler`, runs the tool, and replies with a `FunctionExecutionResult`. The calling agent's `@message_handler` receives this reply and continues reasoning. The `action-observation loop` is implemented as two separate handler invocations connected by a direct message pair.

**Concurrency**: Multiple messages in the queue can trigger multiple agents (or multiple instances of the same agent type keyed by `AgentId`). Topic-based broadcast naturally enables fan-out: one published message triggers all subscribed agents. The `SingleThreadedAgentRuntime` processes the queue one message at a time but agents await async calls, so I/O (LLM calls, tools) does not block other agent dispatch.

**Termination**: No concept of loop termination — the runtime runs until `stop()` or `stop_when_idle()` is called. Individual agents decide to stop publishing; the system goes idle.

**Non-user events — the key differentiator**: Any external event source (timer, system event, drone result, IPC socket data) can be injected simply by calling `runtime.publish_message(SomeMessage(...), topic_id)` from any async context. Agents subscribed to that topic type will receive the message exactly like a user-initiated one. There is **no architectural distinction** between event origins; the runtime is the unified bus.

**Canonical reference**: `microsoft.github.io/autogen/stable/user-guide/core-user-guide/framework/agent-and-agent-runtime.html`, `microsoft.github.io/autogen/stable/user-guide/core-user-guide/core-concepts/topic-and-subscription.html`

---

### 2.7 CrewAI (contrast)

**Loop shape**: `crew.kickoff()` iterates over a task list. Each task is executed by its assigned agent via an inner per-agent ReAct loop (thought → tool use → observation). Sequential process runs tasks one by one; hierarchical process uses a manager LLM to assign tasks dynamically. The newer "Flows" API adds event-driven pipeline semantics on top.

**Tool cascade**: Within a task's agent loop, tool results are fed back as observations in the next reasoning step. Between tasks, the previous task's output is passed as context to the next.

**Concurrency**: Limited. Parallel tasks require explicit configuration. No actor model; fundamentally task-list oriented.

**Termination**: Task `expected_output` matched (manager validates in hierarchical mode), or iteration cap.

**Non-user events**: Flows support event-driven triggers, but this is a higher-level abstraction than the raw message-passing in AutoGen Core.

**Verdict for DollOS**: CrewAI is not a good fit — it is designed for batch task pipelines, not daemons reacting to a continuous stream of heterogeneous events.

---

## 3. Cross-Cutting Patterns

### Pattern A: The Accumulating-Conversation While-Loop (dominant)
OpenAI Agents SDK, Anthropic Tool Runner, and smolagents all use a variation of:
```
messages = [user_turn]
while needs_more_turns:
    response = call_llm(messages)
    messages += [assistant_turn, tool_results_turn]
```
This is the simplest mental model. It works well for single-agent single-task flows. **Pain point**: it has no native concept of external event injection. Anything not already in the `messages` list must be handled by the outer caller before the next `call_llm`.

### Pattern B: Explicit Graph with Shared State (LangGraph)
The graph makes control flow auditable and replayable. State reducers handle fan-out merges. Checkpointing enables long-running workflows. **Pain point**: graph topology is fixed at compile time; dynamic routing requires `Command` objects or conditional edges which add indirection. External events still inject via `graph.invoke()` from outside.

### Pattern C: Finite State Machine (pydantic-ai)
Similar to LangGraph conceptually but typed and tighter. Less boilerplate for simple flows. **Pain point**: same as Pattern A for external events.

### Pattern D: Actor / Message-Passing Runtime (AutoGen Core)
No single agent "owns" a loop. The runtime is the event bus. Tool results, LLM responses, timers, and user input are all typed messages routed by the same mechanism. **Pain point**: higher conceptual overhead; no built-in "conversation history" accumulator — agent authors manage history themselves. Less turnkey than the while-loop frameworks.

### Who Distinguishes Event Origins? Almost Nobody.
The specific DollOS question: **does any framework distinguish user-initiated events from tool-result events from external async events?**

- **LangGraph, OpenAI SDK, Anthropic, smolagents, pydantic-ai**: No — all sources of input are unified into the conversation `messages` list. The distinction between a user turn and a tool result is encoded in the `role` field (`user` / `tool`) or message type, not in a separate event queue.
- **AutoGen Core**: Architecturally closest to what DollOS wants. It **unifies all event origins into one typed-message bus** (the runtime queue) but provides topic-based subscription as a first-class routing primitive. A user message and a drone result are both just typed messages published to different topics; agents subscribe to whichever topics they care about. Concurrency and ordering are the runtime's responsibility.
- **CrewAI Flows**: Some event-driven semantics, but primarily for pipeline orchestration, not daemon loops.

---

## 4. Recommendations for DollOS

DollOS has three requirements that are unusual relative to typical chatbot-style agent frameworks:

**(a) Multi-source events** — user text, voice input, timers (scheduled diary, morning greeting), drone/subagent results, system events — all need to flow into the same processing pipeline.

**(b) Small-model preprocessing per event** — Inner Voice runs on every raw event before the big model is woken. This is a per-event step that the frameworks above do not model at all (they assume one model per turn).

**(c) Cascade within a turn** — after the big model calls `Say` or `NoteMemory`, tool results may feed back into another model invocation in the same logical turn.

### Option 1: AutoGen Core Actor Model (closest architectural match)

Map DollOS concepts to AutoGen Core:

| DollOS Concept | AutoGen Core Concept |
|---|---|
| `EventDispatcher` | `SingleThreadedAgentRuntime` (or async queue) |
| `RawEvent` (any source) | Typed `dataclass` message published to runtime |
| Inner Voice preprocessing | `@message_handler` on `InnerVoiceAgent` that processes `RawEventMessage`, optionally publishes `WakeEvent` |
| Doll turn | `@message_handler` on `DollAgent` that handles `WakeEvent`, accumulates tool cascade internally |
| Tool execution | Direct message to `ToolAgent` (RPC pattern); result returned to `DollAgent` handler |
| Drone result | `DroneResultMessage` published to runtime; subscribed agent picks it up |
| Timer | External `asyncio` task calls `runtime.publish_message(TimerEvent(...), topic)` |

**Trade-offs**:
- Excellent fit for multi-source events and async tool results.
- Cascade (big model re-invocation after tools) is handled naturally: `DollAgent.handle_wake_event` accumulates its own conversation history and loops within the handler using an inner `while stop_reason == "tool_use"` pattern — exactly the Anthropic manual loop, but scoped inside one message handler invocation.
- AutoGen Core is a real Python dependency (`autogen-core` package). It is minimal — just the runtime, no LLM-specific code. You BYO LLM client.
- Drawback: you manage conversation history yourself; no automatic accumulator.

### Option 2: LangGraph with Custom Entry Points

Model DollOS as a `StateGraph` where all event sources feed in via `graph.ainvoke()` or `graph.astream()` from an outer dispatcher. The graph itself handles Inner Voice → conditional wake → Doll turn → tool cascade → done.

```
START (RawEvent)
  → inner_voice_node   (small model preprocessing)
  → [conditional: wake?]
      YES → doll_node  (big model + prefill)
           → [conditional: tool_calls?]
               YES → tools_node → doll_node (cycle)
               NO  → END
      NO  → END (drop event)
```

**Trade-offs**:
- Explicit graph makes the per-event flow auditable and replayable via checkpointing.
- Multiple concurrent events = multiple concurrent `graph.ainvoke()` calls, each managing its own state independently. This is correct for DollOS's current per-event isolation model.
- Cascade is handled by the `doll_node → tools_node → doll_node` cycle — clean and explicit.
- For async tool results (drone replies arriving later): harder. A drone result would need to start a new graph invocation, not continue an in-progress one. This is a mismatch with AutoGen's clean async message delivery.
- `Send` API for parallel tool execution within `tools_node`.

### Option 3: Keep Asyncio Tasks + Inner While-Loop (current approach, refined)

Rather than adopting a framework, refine the current architecture:

- `EventDispatcher` remains the fan-out layer (one `asyncio.Task` per `RawEvent`).
- Each task runs: `await inner_voice.perceive(event)` → if wake → `await doll.turn(event, instinct_result)`.
- `doll.turn()` contains the Anthropic-style `while stop_reason == "tool_use"` loop internally.
- Drone / async results are posted to an `asyncio.Queue` and a background task converts them into new `RawEvent` objects — feeding back into the dispatcher.

**Trade-offs**:
- Minimal new dependencies.
- The "wrong shape" feeling likely comes from the current cascade design (per-sink lock), not from the asyncio fan-out itself. The fan-out is actually correct — concurrent events should run concurrently.
- The inner while-loop for cascade is exactly what every framework uses under the hood.
- Drone result re-injection via a queue is what AutoGen Core does implicitly via its message queue — implementing it explicitly keeps DollOS simple.
- Drawback vs. AutoGen: no typed routing, no topic subscriptions — wiring is manual.

### Recommendation Summary

| Option | Best If | Avoid If |
|---|---|---|
| AutoGen Core | You want a clean event bus with typed routing, async tool results, and future distributed expansion | You want minimal dependencies and full control over every detail |
| LangGraph | You want auditable, replayable, checkpointable per-event flows with explicit control flow graphs | Drone/async results need to continue an in-progress turn (not just start a new one) |
| Refined asyncio (current) | You want zero new framework overhead and the current architecture is already almost right | The codebase grows to many event types and routing becomes spaghetti |

For DollOS step 5 (Inner Voice integration + cascade), **Option 3 (refined asyncio)** is likely the lowest-risk path: the current event loop is correct in shape; the cascade should be an inner while-loop inside `Doll.turn()`, not a separate concurrent task per tool. For step 7+ (drone results, complex routing), **Option 1 (AutoGen Core)** is worth re-evaluating — its typed message bus maps cleanly onto DollOS's heterogeneous event sources without requiring a graph topology change for each new event type.

---

## Sources

- LangGraph graph-api: `https://langchain-ai.github.io/langgraph/` — graph concepts, ToolNode, Send, Command
- OpenAI Agents SDK running agents: `https://openai.github.io/openai-agents-python/running_agents/`
- Anthropic handle tool calls: `https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls`
- Anthropic tool runner: `https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-runner`
- smolagents agents.py source: `https://github.com/huggingface/smolagents/blob/main/src/smolagents/agents.py`
- pydantic-ai agent core concepts: `https://pydantic.dev/docs/ai/core-concepts/agent/`
- AutoGen Core agent and runtime: `https://microsoft.github.io/autogen/stable/user-guide/core-user-guide/framework/agent-and-agent-runtime.html`
- AutoGen Core topic and subscription: `https://microsoft.github.io/autogen/stable/user-guide/core-user-guide/core-concepts/topic-and-subscription.html`
- AutoGen Core message and communication: `https://microsoft.github.io/autogen/stable/user-guide/core-user-guide/framework/message-and-communication.html`
- CrewAI sequential process: `https://docs.crewai.com/en/learn/sequential-process`
- CrewAI hierarchical process: `https://docs.crewai.com/how-to/hierarchical-process`
