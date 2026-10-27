# DollOS — Claude Code Instructions

## What is DollOS

DollOS is a personal AI ecosystem. **Doll lives in the DollOS daemon** (her brain). **Target platform (user decision 2026-10-27): macOS only.** The whole Doll — daemon and interface — runs on the user's **Mac**; no UI yet (a desktop-pet overlay comes later). The iPhone body is cancelled. Linux is no longer a supported target: zenith (this Linux box) is only for development and tests until the macOS port, and the port waits until the core works. The big LLM (**Qwen3.8-Flash-Next**) is served by **Strata** (OpenAI-compatible `/v1/chat/completions`) on **another machine**. The agent core is being rebuilt as a designed harness (see `docs/superpowers/specs/2026-10-17-doll-harness-design.md`, APPROVED).

**v1 focus (user, 2026-10-27):** v1 = "Doll chats using Qwen3.8, remembers the corrections you gave her, and can use tools to get things done." Frozen until then: macOS port, desktop-pet UI, 4B steering, Mimir/SNN, TTS tuning.

**Positioning (2026-07-01 — see `docs/superpowers/specs/2026-07-01-virtual-being-positioning.md`): Doll is a virtual being, not an "AI companion."** Self-centered — she has her own interiority, growth, and agenda; the relationship with the user is a thread in her life, not the reason her life exists. Companionship is what happens because two lives are entangled, not a role she performs. This sharpens (does not replace) Self-First below and the existing `project_companion_definition` framing ("companion = 學習/自更新能力，非服務性"). See the spec for the four concrete design implications (self_profile scope, real personality growth over time not just accumulation, self-directed agenda beyond tracked commitments, interaction language). **Status 2026-07-07: all four positioning implications resolved.** §2.1 (self_profile interiority nudge → PinSelf self-centered guidance, 2026-07-02 spec) and §2.2 (real personality growth → 慢變演化, `2026-07-02-slow-self-evolution-design.md`, roadmap step 30) IMPLEMENTED and live-smoke-verified. §2.3 (self-directed agenda → `2026-07-07-self-directed-agenda-design.md`, roadmap step 33) IMPLEMENTED (merged a672141): self_directed OpenLoop with code-captured auto-provenance + AgendaObserver 4-gate → AgendaMoment self-wake turn, AGENDA_TOOLS cognition-only, speech-suppressed, reactive-first. §2.4 (interaction language) — user call 2026-07-07 ("她講不講都沒差,我之後會要 doll 寫日記"): the conversational-surfacing design (daemon framing + freshness signal) was **cut** — a first-person self-written **diary** is the correct, anti-performance vehicle for inner life (deferred as its own feature). Only the Gura pack persona shift landed (reactive-only → present-alongside while keeping restraint; `2026-07-07-interaction-language-design.md`, marked SUPERSEDED-IN-PART).

**Product positioning (2026-05-01 pivot — see `docs/superpowers/specs/2026-05-01-dollos-pivot-to-computer-design.md`; technical architecture below is unchanged by the 2026-07-01 repositioning above, only *purpose* shifted):**

- **Doll** — the virtual being herself (not merely "AI companion" — see 2026-07-01 positioning above). Soul, memory, personality, decisions all live in DollOS on the computer.
- **DollOS** — Python process: event loop + Conversation Engine + Memory SoT + Voice Pipeline + IPC server.
- **DollOS UI** — not built. Eventual form: transparent desktop-pet overlay with Live2D. Tech not chosen yet (the 2026-05 plan said Tauri + Cubism Web; note AIRI moved from Tauri to Electron — check why before choosing).
- **DollOS Body (iOS)** — cancelled 2026-10-27 (macOS-only target). Old design: `docs/superpowers/specs/2026-10-17-dollos-v1-iphone-body-design.md` (DRAFT). Android deferred.
- **Big LLM** — `/v1/chat/completions` with native tool calling is the only path (`config.py` `provider: Literal["openai-chat"]`; plan `docs/superpowers/plans/2026-10-18-model-adapter-chat-api.md`). The old `/completion` + `qwen3-thinking` template + GBNF grammar path is deleted. Any OpenAI-compatible server works, which makes bring-your-own LLM real; today it is Strata (see "Big model server" below). No small model anywhere (Inner Voice removed 2026-05-16).
- **Memory pipeline** — markdown files are the source of truth; `FtsMemory` (SQLite FTS5 + jieba, `src/dollos/memory/`) is a derived index. **No vector store / embeddings** (removed 2026-06-26, `2026-06-26-embedder-free-memory-design.md`). Each turn auto-injects `[Memory context]` (top-K search) and `[Associative memories]`; `Recall` is the explicit deeper search.
- **Self-First Design** — killer feature: Doll has a self (mood / preferences / habits / relations). Self emerges from architecture (Memory's self-history + character description), not from prompt commands. See main spec §8.

**Two-layer intelligence:**
1. **Doll** — user's chosen large model, deliberative. Handles all events (conversation, schedule, monitors, workflow results).
2. **Workflow** (ephemeral — replaces the old "Subagent" name, 2026-06-27) and **Subagent** (persistent — replaces the old "Drone" name; decision recorded in `agent-service` branch `docs/superpowers/specs/2026-06-25-A2-agent-execution-model-design.md`, "Drop the name Drone entirely") — task-specific agents Doll dispatches.

## Repo Map

| Repo / Path | Status | Role |
|---|---|---|
| **DollOS** (this repo, `/home/seith/DollOS/DollOS/`) | Active | Daemon (`src/dollos/`) + tests + character_packs + docs |
| `fish-tts`, `luxtts-onnx`, `tuna` (siblings under `/home/seith/DollOS/`) | Active | TTS engines / fine-tuning tools |
| `piper-finetune` (`/home/seith/DollOS/piper-finetune/`) | Bootstrap | Piper VITS fine-tuning from synthetic clips (Stage 0 scripts only; no trained voice yet) |
| `snn-vlm`, `dollm`, `dollm-flybrain` (siblings) | Research | SNN research (Mimir). Not on the DollOS v1 critical path. |
| **`ios/`** | Cancelled 2026-10-27, never started | iPhone body app (Swift/SwiftUI); dropped with the macOS-only decision. |
| **DollOS-App** | Deferred | Android app — on hold. |

**Retired** (pre-pivot work, no longer maintained — see `docs/RETIRED-REPOS.md`; those repos are not on this machine):
`DollOSAIService`, `DollOSLauncher`, `DollOSService`, `DollOSSetupWizard`, `DollOS-Android`, `DollOS-build`.

Earlier development happened on another machine (home `/home/progcat/Projects/DollOS`). Runtime data (`data/`, telemetry, traces) and some experiment logs live there, not here.

### Repo layout

```
DollOS/
├── src/dollos/          # the daemon (kernel, mind/, llm/, memory/, voice/, ipc/, tools.py, …)
├── tests/               # pytest suite (~2,080 tests)
├── character_packs/     # Doll packs (doll.toml + voice/)
├── docs/                # roadmap, specs, plans, research
├── scripts/             # persistent tools (persona_stability_smoke.py, voice tools)
├── experiments/         # spikes (e.g. tauri-spike)
├── vendor/              # third-party SDK drop instructions
└── data/                # runtime data (gitignored)
```

## Key Architecture Decisions

- **External actions are fire-and-forget**: Shell, SpawnWorkflow, and SpawnMonitor all spawn background workers and return immediately; results re-enter the event queue as `{Tool}ResultEvent` (or `MonitorTriggeredEvent` / `MonitorExitedEvent`). There is no Doll-callable wait tool. "Wait" is implicit — Doll's cascade either keeps going or ends; results come back as new perceptions. Internal capabilities (Say / NoteMemory / Recall / Mood) are sync inline — Doll cannot await her own mouth.
- **Monitor vs Shell vs Workflow vs Subagent**: All four are external actions.
  - **Shell** — one-shot command, single result event.
  - **Workflow** (2026-06-27, replaces the old ephemeral "Subagent") — fire-and-forget dispatch of N parallel worker agents (each an isolated sub-LLM cascade) with an optional adversarial verify pass and an optional synthesis agent; exactly one `ToolResultArrived(tool="Workflow")` result event returns to Doll regardless of N. `mode="map_reduce"` (parallel fan-out) or `mode="verify"` (+ skeptic pass per worker). N=1 with no synthesis degrades to the old single-subagent behavior. See `docs/superpowers/plans/2026-06-27-doll-workflow.md`.
  - **Monitor** — long-running command, per-line trigger events (regex + rate-limit) + exit event. Stateless watcher; no LLM in the loop. Active state surfaces via `[Active monitors]` perception block.
  - **Subagent** (future, replaces the old "Drone") — persistent agent with its own LLM cascade, scheduled trigger, can call tools and Report back. Authoritative design is `agent-service` branch A2 (containerized k8s Job/CronJob); the k3s side of that is deferred until this track is picked up.
- **Computer-as-home**: Doll lives in DollOS on the user's computer. Memory SoT, personality, identity vault, decisions all on-device.
- **Phone as body — cancelled 2026-10-27** (macOS-only target). The old design is `docs/superpowers/specs/2026-10-17-dollos-v1-iphone-body-design.md` (DRAFT).
- **LLM coupling (today)**: every model call goes through one `ModelAdapter` (`llm/model.py`, `llm/chat_completions.py`): `/v1/chat/completions` with native tool calling, parallel calls allowed, no grammar. Tool schemas are generated from the pydantic tool classes and sorted by name (`llm/tool_schema.py`); the server does not validate arguments, `dispatch_one` (`harness/executor.py`) does. MindLoop turns and the workflow / keeper agents (`agent_engine.run_agent`) share one tool loop, `TurnRunner` (`src/dollos/harness/runner.py`). Thinking is capped per call with `reasoning_budget_tokens` (`[llm].think_budget_tokens`); `reasoning_effort` is never sent. **Each conversation only grows at the end** (`mind/context_builder.py`; `private`, plus one `ch:<channel_id>` per channel): fixed system → the page's snapshot → one new user message per turn → assistant and `tool` messages exactly as sent, so the server's cache resumes from the previous request. Each principal is **offered one fixed tool list** for the whole run (`ToolRegistry.offered`, `harness/turn_policy.py` `offered_tools`: every tool its origin tier may ever be bound to, MCP tools included). What a turn may run is **bound** per turn: the turn kind's tools ∩ the principal's row in `permissions.TOOL_MATRIX` (`turn_policy.bound_tools`; MindLoop's `_active_tool_registry` / `_offered_tools` now just call these). `ToolExecutor` (`src/dollos/harness/executor.py`) enforces it per call: a name outside the offered list is denied, an offered-but-unbound call gets a "not available in this turn" tool result. A new page (code-written snapshot, no LLM summary) opens only when the fixed prefix changes, the last call reached `[llm].conversation_budget_tokens` (24000), the conversation is not in memory (daemon start, LRU), or the last call got a server 4xx. Energy and the daily Cognition budget count only each call's new prompt tokens. There is no Anthropic adapter.
- **Memory wire format**: `FtsMemory.search` top-K → `[Memory context]` bullets; plus `[Associative memories]`; explicit `Recall` tool. Hits from `external_public/` are tagged `[external AI · unverified]` (R-DECISION-5). S1 (done, plan 2026-10-27) keeps them out of owner/internal auto-injection; `Recall` returns them only with `include_external` (which taints the turn, see below). Strangers' outputs are kept out of owner pages' `[Recent outputs]` too (commit 5f11b42).
- **Event-loop centric**: Doll is not a chatbot. She's an event-driven agent. Conversation is one event source among many (voice, text, schedule, system events, subagent results, self-initiated).
- **Workflow (ephemeral) vs Subagent (persistent)**: Workflow is a one-shot tool call, definition inline, dies after run (this is the renamed old "Subagent"). Subagent has persistent definition, scheduled trigger, runs in background, results re-enter the event queue (this is the renamed old "Drone").
- **Self-First**: `system_prompt` is identity description ("you are Gura, ..."), NOT behavior commands ("you should be self-first"). Self emerges from character description + Doll's own memory entries (self-history, preferences, mood) surfacing through the `[Memory context]` block + `Recall` tool. The 2026-05-08 smoke confirmed Self-First behavior emerges this way (T2 「我自己愛冰美式，你呢？」). The system prompt is now 基礎人設（兩版，`base_persona.{zh-TW,en}.jinja`，picked by the pack's `[meta].language`）+ the pack's identity: the base comes first, and the pack's own setup wins where they conflict.
- **Memory SoT**: markdown files under `data/memory/` (`shared/`, `transcripts/`, `skills/`, `external_public/`, `external_dm/`), indexed by `FtsMemory` (FTS5/BM25 + jieba). Origin is encoded by directory (`_ORIGIN_DIR` in `tools.py`).
- **Principals (permissions)** (`harness/permissions.py`, harness spec §6.3): each turn has the lowest-trust principal of its inputs — `owner` (local IPC / voice), `owner_dm` (the owner in a Discord DM; same tools as owner, user decision 2026-10-27), `internal` (Doll's own turns), `external_public` (everyone else, including non-owner DMs, the owner in a public channel, and MCP peers). `ctx.origin_tier` is kept as the compatibility view (owner → `internal`, owner_dm → `external_dm`); memory dirs, logs and speech routing still read it. Strangers' turns only see `external_public/` (fail-closed allowlist). Once a turn reads untrusted content (`Recall` with `include_external`, an `untrusted_output` MCP tool), the rest of it may only run `permissions.TAINTED_TOOLS` (Recall, Report); external_public turns are never tainted.
- **Harness (APPROVED 2026-10-17)**: 已實作 S1–S7（plan 2026-10-27，branch feat/harness-s2-s7），工具合併（spec §4，約 15 個工具）待做，另寫 plan. `MindLoop` is split into TurnPolicy / ContextBuilder / ModelAdapter / ToolRegistry / ToolExecutor with one `TurnRunner` (`src/dollos/harness/`); native tool calling (no grammar, parallel calls); each turn only runs the tools its principal and turn kind allow. MCP client (`harness/mcp_client.py`): outside stdio MCP servers listed in `mcp_clients.toml` (template `mcp_clients.example.toml`, pointer `[mcp_clients].config`), tools named `server__tool`, `min_principal` required, bound only on conversation turns. Shell and Python-REPL action interfaces were considered and rejected (spec §11).
- **Audio**: ASR (`sherpa-onnx` + SenseVoice) and TTS run in the daemon; engines are pluggable via `@register_asr` / `@register_tts` (`src/dollos/voice/engines.py`). The Mac install stays ONNX Runtime only (no torch). Gura speaks English first, through `luxtts-onnx` on the Mac CPU (`device = "auto"`: CUDA if present, else CPU; RTF 0.27 for English on an M5), so v1 needs no GPU machine for voice (plan `docs/superpowers/plans/2026-10-18-gura-english-luxtts-mac.md`). A Chinese voice comes later: either Piper for a local Gura voice (needs training via `piper-finetune`) or a TTS server on the GPU machine behind `openai-compat-tts`.
- **Discord bridge internalization (2026-07-06)**: the daemon spawns and supervises discord-bridge as a child subprocess via a generic `ServiceSupervisor` (`src/dollos/service_supervisor.py` — DollOS OS-level service manager: `PR_SET_PDEATHSIG` anti-orphan, `SIGINT` graceful stop escalating to `SIGKILL`, fatal-token errors exit non-zero so crash-loop detection actually catches them, crash-loop cap emits a `BridgeDown` perception). The bridge itself stays an independent OS process (`setsid` own process group) — a bridge crash never touches the daemon, and vice versa (crash-isolation, zero message loss on either side).
- **MCP server — external AI talks to Doll (2026-07-07 spec, 2nd `ServiceSupervisor` service)**: `dollos-mcp` (`src/dollos/mcp_server/`) is a FastMCP loopback server (`mcp.toml` `[server].bind_host`/`bind_port`, fail-closed to a loopback literal — spec §E) that lets an external AI (e.g. Claude Code) call a `talk(name, message)` tool and get Doll's reply, routed through the daemon's IPC as a per-call unique channel (`ChannelRegister`/`ChannelEvent` → `AddressedText`/`TurnEndAddressed`, spec §B). The peer is always treated as `origin_tier="external_public"` (never owner, never `external_dm`, regardless of the `is_dm` framing on the wire) — same memory scoping as a stranger: `[Recent perceptions]`/`[Recent outputs]` render through the `external_public` allowlist, retrieval is confined to `data/memory/external_public/`. **Debug mode** (P2, optional, `mcp.toml` `[server].debug_secret`) lets a connection call `authenticate(secret)` to unlock read-only introspection — `get_state()` (`{mood, current_self}`) and `get_recent(n)` (recent `external_public`-only interactions; `recent_outputs` is never exposed, no origin field to scope it fail-closed) — via a new authenticated IPC query protocol (`QueryState`/`QueryRecent`/`QueryResult` in `dollos/ipc/messages.py`; the daemon fail-closes on a `token` that doesn't match `config.toml`'s `[mcp].query_token`, since the IPC server itself has no per-connection auth) plus a best-effort prompt-level reliability nudge on `talk()` (`debug_reliable`, spec §C.2) — it changes tone, never the tool registry or origin_tier. Debug mode still grants no new tools and never makes the caller owner; both secrets (`debug_secret`, `query_token`) are OWNER-SENSITIVE and live only in gitignored `mcp.toml`/`config.toml`, never in the `.example.toml` templates.

## Implementation Plans

**增量開發**：每個 plan 只加一個新概念。Plans 不預先全列，做完才寫下一個。詳細能力目標清單見 spec §11.6。

### 已完成

| Plan | Status |
|---|---|
| 1 — DollOS Skeleton | Merged |
| 2 — Memory SoT 儲存層 | Merged |
| 3 — LLM Provider / Template decoupling | Merged |
| 4 — Inner Voice + VoM RECALL utility | Superseded by memsearch pivot |
| Roadmap step 1 — 確保 LLM 能用 | 確認既有 Plan 1 已涵蓋（無 code 改動）|
| Roadmap step 2 — Prompt rendering + DollOS rename | Merged |
| Roadmap step 3 — VoM (memsearch-backed) | Merged |
| Roadmap step 4 — Event Loop (concurrent dispatcher + two-tier event model) | Merged |
| Roadmap step 5 — Inner Voice (minimal, summary-only) | Merged |
| Roadmap step 6 — Tool calling (Say + NoteMemory, pydantic) | Merged |
| Roadmap step 7 — Cascade (inner while-loop on tool fails) | Merged |
| Roadmap step 8 — Memory auto-write + Diary | Merged |
| Roadmap step 9 — Success-cascade + Shell | Merged |
| Roadmap step 10 — Skills system | Merged |
| Roadmap step 11 — Prompt-compact + grammar wiring (B4 GBNF + CJK deny) | Merged |
| Roadmap step 12 — Memory wire format pivot (RAG context + Recall tool) | Merged |
| Roadmap step 13 — Cascade robustness (multi-message + skills audit + character trim) | Merged |
| Roadmap step 14 — Episodic memory + uncapped cascade + REVIEW think field | Merged |
| Roadmap step 15 — Subagent (ephemeral async worker + structured Report) | Merged |
| Roadmap step 16 — IPC pump (per-connection persistent sink) | Merged |
| Roadmap step 17 — Doll pack (directory + doll.toml manifest) | Merged |
| Roadmap step 18 — Time awareness ([Now] + HH:MM:SS + time-aware Recall) | Merged |
| Roadmap step 19 — Mood (Self-First emotional state via big-model think field) | Merged |
| Roadmap step 20 — Cascade decision log + structlog | Merged |
| Roadmap step 21 — Schedule + pending awareness (Phase 1 of 4) | Merged |
| Roadmap step 24 — External actions = fire-and-forget (Shell ≈ Subagent) | Merged |
| Roadmap step 25 — Monitor watcher (SpawnMonitor + rate-limit + [Active monitors]) | Merged |
| Roadmap step 26 — Voice engines + pack config (Voice Phase A) | Merged |
| Roadmap step 27 — Voice pipeline Phase B (WebRTC + VoiceSession + IPC) | Merged |
| Roadmap step 28 — Voice pipeline Phase C (local-audio-bridge + E2E) | Merged |
| Roadmap step 29 — Workflow (取代 ephemeral Subagent；map_reduce/verify fan-out + synthesis) | Merged |
| Roadmap step 30 — 慢變演化 (current_self「現在的我」：evidence layer + ratification + Mode A evolution pass；3 plans, spec `2026-07-02-slow-self-evolution-design.md`) | Merged, live-smoke-verified |
| Roadmap step 31 — Discord bridge internalization + generic ServiceSupervisor (bridge 變 daemon 子行程；PDEATHSIG 反孤兒、SIGINT graceful、crash-loop cap + BridgeDown perception；dollosctl 單服務化；spec `2026-07-06-bridge-internalization-design.md`) | Merged |
| Roadmap step 32 — MCP server（外部 AI ⇄ Doll，第二個 ServiceSupervisor 服務；P1 peer talk〔`talk` tool，origin_tier=external_public〕+ P2 debug mode〔`debug_secret` 閘 + `get_state`/`get_recent` 透過新的已驗證 IPC query 協定，daemon `query_token` fail-closed，`recent_outputs` 排除〕+ reliability nudge；spec `2026-07-06-mcp-server-design.md`） | Merged |
| Roadmap step 33 — 自主議程 self-directed agenda（positioning §2.3；self_directed OpenLoop + code-captured auto-provenance〔模型寫不到〕+ AgendaObserver 四閘〔idle+energy+active-loop+throttle〕→ AgendaMoment 自發 turn，AGENDA_TOOLS 認知子集〔無 Shell/自主外部〕、抑制對外發話、reactive 優先；spec `2026-07-07-self-directed-agenda-design.md`） | Merged |
| Roadmap step 34 — 日記 = 看著當天 action log 寫的第一人稱反思（把對話 transcript 長成完整 action log〔對話+白名單動作+世界事件，C1 origin-gated、NoteMemory 輕指標、Mood 只記變化、Shell 遮敏感值〕；consolidation 過濾隔離〔I2〕；config 化每日 DiaryMoment 專用回合〔收窄 `{WriteDiary,Recall}` + 注入 `[Today's log]` 全天 + 抑制發話/transcript + I1 不造假保證〔warn+marker+單次 retry，co-batch 容忍〕〕；spec `2026-07-07-diary-action-log-design.md`） | Merged |
| Roadmap step 35 — System Pulse 主動觸發（PulseObserver → PulseMoment 自發回合） | Merged |
| Roadmap step 36 — System Pulse 臨場感調校（Alert severity + `[Body signal]` 框架） | Merged |
| Roadmap step 37 — 代謝 vital 模型 v1a+v1b（energy 由 token 驅動 × GPU 熱乘數，VitalsRecorder） | Merged 2026-07-10 |
| 基礎人設模板（中英兩版，跟 pack 的 `[meta].language` 切換；BASE_BANNED 客服腔清單併入 enforcement；演化 skeptic 也看得到基礎人設；拿掉 Yes Man、Gura 英文、新增大肥魚〔本機自用〕；spec `2026-10-25-base-persona-template-design.md`） | Merged 2026-10-26 |
| Harness S1–S7（principal 四值 + `TOOL_MATRIX`、TurnPolicy、ToolRegistry/ToolExecutor、單一 TurnRunner、讀外部內容後降權、MCP client；另修 `[Recent outputs]` 和 transcript 的陌生人外洩；plan `2026-10-27-harness-s2-s7.md`，live 驗收 `docs/research/2026-10-27-harness-live-acceptance.md`） | Merged 2026-10-27 |

### 已歸檔（被後續 step 取代）

| Plan | Status |
|---|---|
| Roadmap step 22 — Async Shell + Monitor + ProcessRegistry (Phase 2 of 4) | Superseded by step 24 |
| Roadmap step 23 — Cancel + interrupt-aware Monitor (Phase 3 of 4) | Superseded by step 24 |

### 下一個

1. **記住糾正**：她記得主人給過的糾正。v1 的核心；要先寫 spec。
2. **工具合併 plan**：25 個工具收成約 15 個（harness spec §4）。
3. **macOS 移植**：spec `docs/superpowers/specs/2026-10-21-macos-port-design.md`，plan `docs/superpowers/plans/2026-10-21-macos-port.md`。核心能用之前凍結。

**已收的設計準則**：
- 偏好學習 / 習慣學習不是新 subsystem，是 prompt engineering — Doll 用 NoteMemory 自記
- Bootstrap 是 daemon-internal planning，用 dummy sink 不對外發送（用戶 greeting 由 scheduled entry 提供）

完整 roadmap：`docs/roadmap.md`。

## Build / Run

### DollOS (Python)

Python is pinned to 3.13 (`.python-version`, added in S1 Task 0) because `piper-phonemize` only ships cp313 wheels.

```bash
uv sync
cp config.example.toml config.toml
# edit [llm] base_url / model to point at your OpenAI-compatible server (Strata)
uv run python -m dollos --config config.toml
uv run pytest                   # tests
```

To also run the Discord bridge in dev, in a second terminal:

```bash
uv run python -m dollos.discord_bridge --daemon ws://127.0.0.1:9876 --config <bridge>.toml --data-root data
```

**Warning**: if `config.toml` has `[bridge].enabled = true`, the daemon
already spawns and supervises its own bridge subprocess — do NOT also run
the standalone command above. Two bridge processes sharing the same
Discord bot token race for the same gateway session and Discord's login
will reject the second one.

### `dollosctl` — run the full stack as systemd `--user` services (recommended)

**Linux only** — the macOS port replaces this with launchd.

The manual invocations above are for iterating on the code. To dogfood
DollOS running continuously (daemon auto-restarting on crash, surviving
terminal/session close), install it as a `systemd --user` service via the
`dollosctl` console script (`src/dollos/ctl/`, P1g). The Discord bridge is
no longer a separate unit — the daemon internalizes it as a supervised
subprocess (2026-07-06 `ServiceSupervisor`; see Key Architecture Decisions
below), spawned/watched from within the single daemon unit when
`config.toml`'s `[bridge].enabled = true`.

**Run `dollosctl install` from the DollOS repo root** — `WorkingDirectory`
is captured from your current directory, and the daemon's `data/` (memory,
traces, pid) resolves against it. Installing from elsewhere starts a fresh
empty data store. `install` prints the resolved `python` / `working_dir` /
`data_root` absolutes so you can catch a wrong-cwd install before it happens.

```bash
uv sync   # installs the dollosctl console script (pyproject [project.scripts])

uv run dollosctl install \
    --daemon-config config.toml \
    --data-root data
# writes ~/.config/systemd/user/dollos-daemon.service + daemon-reload
# (bridge path/enable now comes from config.toml's [bridge] block, not a flag)

uv run dollosctl start      # daemon (bridge comes up inside it, if enabled)
uv run dollosctl status     # daemon unit should show active (running)
uv run dollosctl logs daemon -f    # follow the daemon's journal (bridge output is in here too)
uv run dollosctl restart    # daemon
uv run dollosctl stop       # daemon
uv run dollosctl uninstall  # stop + remove the unit file
```

The unit sets `Restart=on-failure`. To also auto-start on boot / without an
active login session: `systemctl --user enable dollos-daemon.service` +
`loginctl enable-linger $USER`.

**Upgrading an existing install**: a pre-2026-07-06 install wrote a second
`dollos-bridge.service` unit. A bare `dollosctl restart` does NOT clean
that up — only `install` (or `uninstall`) runs the legacy-unit migration.
Re-run `uv run dollosctl install --daemon-config config.toml --data-root
data` (or `uninstall` then `install`) once after upgrading so the legacy
bridge unit gets disabled and deleted; leaving it enabled would auto-start
a second bridge process on boot that clashes with the daemon-internalized
one over the same Discord token.

Full human live-smoke checklist (real systemd start/stop, real Discord
bot, private test server — can't run in CI): `docs/dollosctl-smoke.md`.

### Big model server (Strata, runs on another machine)

The current model is **Qwen3.8-Flash-Next** (125B MoE, ~6B active), served by **Strata** at `http://100.110.158.36:8080` as `qwen3.8-flash-next-iq3_s` (context 131072) — it does not fit on the Linux box. Strata only speaks `/v1/chat/completions` (no `/completion`, no grammar), takes text only, has no API key, and has **one compute slot** shared with every client; its conversation cache parks at most 4 conversations across all clients, least recently used evicted first.

- Point `[llm].base_url` and `[llm].model` (an id from `/v1/models`) in `config.toml` at it. At boot the daemon checks `/health` and that `model` is listed in `/v1/models`, and refuses to start otherwise. An old `config.toml` with `template` / `model_alias` is rejected at load.
- `max_concurrency = 1` (one slot); `timeout_s = 300` covers a cold prefill plus queueing behind other clients (no keep-alive during prefill).
- Any other OpenAI-compatible server (e.g. llama-server with `--jinja`) should work too; `reasoning_budget_tokens` and `timings` are Strata/llama.cpp fields that other servers ignore or omit.

There is no embedding server any more (memory is embedder-free since 2026-06-26).

## Specs and Plans

- `docs/superpowers/specs/` — current design specs
- `docs/superpowers/plans/` — current implementation plans
- `docs/research/` — research outputs (e.g. `grammar_injection_techreport.md`)

Always read the relevant spec before implementing. Use `superpowers:brainstorming` to design, `superpowers:writing-plans` to plan, `superpowers:subagent-driven-development` to execute.

## Coding Rules

- **Language**: Respond in 繁體中文.
- **Subagents for coding**: dispatch one subagent per implementation task. Don't write code in the main session.
- **Worktree per plan**: each plan gets its own worktree under `.worktrees/<plan-name>/` on its own feature branch. Merge to `main` after the plan completes via `superpowers:finishing-a-development-branch`.
- **No fallback mechanisms**: never implement fallback / degradation logic. State boundaries clearly; if a backend can't do something the design needs (e.g. raw `prefill` for the previous VoM design), surface the limitation explicitly, don't silently rewrite the prompt.
- **Don't overthink upstream**: use upstream packages (llama.cpp, sqlite-vec, Cubism, etc.) as-is.
- **Specs before code**: update or write the spec before implementing. Get user approval on design.
- **Background commands**: don't pipe to `tail` on background commands; write to a file and read it.
- **Per-task user briefing**: before dispatching an implementation subagent, brief the user on the task's plan content; wait for OK; then dispatch.

## Architecture

```
輸入來源                        DollOS daemon（Python，單一 process）
本機 IPC（WS）     ─┐
語音（WebRTC）     ─┤           Perception 佇列（WAL）
Discord bridge     ─┼──────►          │
MCP（外部 AI）     ─┤                 ▼
排程、system pulse ─┤           MindLoop（單一消費者；harness 重構後 = Turn Runner）
自主議程、日記     ─┘             ├─ context：身份 + 記憶 + 狀態
                                  ├─ 大模型（Strata，/v1/chat/completions，另一台機器）
                                  ├─ 說話 → TTS / Discord / IPC
                                  └─ 工具 → 外部動作 fire-and-forget，結果再變成 perception

之後：桌寵 UI（Mac 本機）。iPhone 身體已取消（2026-10-27，只支援 macOS）
```

## Voice Pipeline (actual)

| Component | Where | Notes |
|---|---|---|
| VAD (silero) | Client side (`voice/bridge`) | Sends utterance boundaries only |
| Audio transport | Client ↔ daemon | WebRTC (aiortc), signaling over the IPC WS |
| ASR | Daemon | `sherpa-onnx` + SenseVoice (zh/en/ja/ko/yue), CPU |
| TTS | Daemon | Pluggable: `luxtts-onnx` (default; Gura uses it — English, Mac CPU), `qwen3-tts` (needs torch, GPU machine only; no longer Gura's), `fish-tts`, `piper-vits`, `openai-compat-tts` |
| Wake word / Speaker ID | — | Not in use |

### Wake word training & TTS distillation (old machine)

The earlier `wake_word_training/` scripts and the Piper voice distilled from fish-tts lived under `~/Projects/DollOS/` on the old machine and are **not in this checkout**.
