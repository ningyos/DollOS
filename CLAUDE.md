# DollOS — Claude Code Instructions

## What is DollOS

DollOS is a personal AI ecosystem. **Doll lives on your computer.** The computer is her brain (daemon process). The phone is an optional body / system-assistant interface to reach her on the go.

**Positioning (2026-07-01 — see `docs/superpowers/specs/2026-07-01-virtual-being-positioning.md`): Doll is a virtual being, not an "AI companion."** Self-centered — she has her own interiority, growth, and agenda; the relationship with the user is a thread in her life, not the reason her life exists. Companionship is what happens because two lives are entangled, not a role she performs. This sharpens (does not replace) Self-First below and the existing `project_companion_definition` framing ("companion = 學習/自更新能力，非服務性"). See the spec for the four concrete design implications (self_profile scope, real personality growth over time not just accumulation, self-directed agenda beyond tracked commitments, interaction language). **Status 2026-07-03:** §2.1 (self_profile interiority nudge → PinSelf self-centered guidance, 2026-07-02 spec) and §2.2 (real personality growth → 慢變演化, `2026-07-02-slow-self-evolution-design.md`, roadmap step 30) are IMPLEMENTED and live-smoke-verified; §2.3 (self-directed agenda) and §2.4 (interaction language — explicitly depends on §2.2/§2.3 existing first) remain, each needing its own brainstorm→spec→plan pass.

**Product positioning (2026-05-01 pivot — see `docs/superpowers/specs/2026-05-01-dollos-pivot-to-computer-design.md`; technical architecture below is unchanged by the 2026-07-01 repositioning above, only *purpose* shifted):**

- **Doll** — the virtual being herself (not merely "AI companion" — see 2026-07-01 positioning above). Soul, memory, personality, decisions all live in DollOS on the computer.
- **DollOS** — Python process: event loop + Conversation Engine + Memory SoT + Voice Pipeline + IPC server.
- **DollOS UI** — Tauri (Rust shell + Web frontend) with Cubism Web SDK rendering Live2D. Win/Mac get transparent overlay desktop pet; Linux gets a normal window.
- **DollOS-App** — Android app. Registers as system assistant via `VoiceInteractionService`. Cubism Java SDK. Audio I/O streams to/from DollOS.
- **Big LLM** — user's choice (cloud API / self-host llama.cpp). Single LLM dependency (port 8001). No small model.
- **Memory pipeline** — `memsearch.search(query, top_k)` → format as bullet list → `[Memory context]` block prepended to the user message + explicit `Recall` tool for on-demand deeper search. **2026-05-16**: Inner Voice (small-LLM filter) removed — A/B showed net-negative (slower + worse). Direct memsearch top-K is now the wire format. See `docs/superpowers/plans/2026-05-08-memory-rag-tool.md`.
- **Self-First Design** — killer feature: Doll has a self (mood / preferences / habits / relations). Self emerges from architecture (Memory's self-history + character description), not from prompt commands. See main spec §8.

**Two-layer intelligence:**
1. **Doll** — user's chosen large model, deliberative. Handles all events (conversation, schedule, monitors, workflow results).
2. **Workflow** (ephemeral — replaces the old "Subagent" name, 2026-06-27) and **Subagent** (persistent — replaces the old "Drone" name; decision recorded in `agent-service` branch `docs/superpowers/specs/2026-06-25-A2-agent-execution-model-design.md`, "Drop the name Drone entirely") — task-specific agents Doll dispatches.

## Repo Map

| Repo / Path | Status | Role |
|---|---|---|
| **DollOS** (this repo, `~/Projects/DollOS/`) | Active | Umbrella + daemon + UI + protocol + character_packs + docs + wake_word_training |
| **DollOS-App** | Future | Android app (Cubism Java SDK + Assistant role). Not started. |
| `fish-tts` (`~/Projects/fish-tts/`) | Active | TTS engine: DualARTransformer + DAC vocoder |
| `luxtts-onnx` (`~/Projects/luxtts-onnx/`) | Active | TTS engine: LuxTTS ONNX |
| `tuna` (`~/Projects/tuna/`) | Active | Fine-tuning tools |

**Retired** (pre-pivot work, no longer maintained — see `docs/RETIRED-REPOS.md`):
`DollOSAIService`, `DollOSLauncher`, `DollOSService`, `DollOSSetupWizard`, `DollOS-Android`, `DollOS-build`.

### Future structure of this repo

```
DollOS/
├── daemon/                        # Python brain (Plans 1–7)
├── ui/                            # Tauri + Cubism Web (Plan 8)
├── protocol/                      # shared schema between daemon/ui/app
├── character_packs/               # .doll v3 examples
├── docs/                          # specs, plans, research
├── experiments/                   # POC code (e.g. lesson_injector.py)
├── vendor/                        # third-party SDK drop instructions
└── wake_word_training/            # existing, untouched
```

## Key Architecture Decisions

- **External actions are fire-and-forget**: Shell, SpawnWorkflow, and SpawnMonitor all spawn background workers and return immediately; results re-enter the event queue as `{Tool}ResultEvent` (or `MonitorTriggeredEvent` / `MonitorExitedEvent`). There is no Doll-callable wait tool. "Wait" is implicit — Doll's cascade either keeps going or ends; results come back as new perceptions. Internal capabilities (Say / NoteMemory / Recall / Mood) are sync inline — Doll cannot await her own mouth.
- **Monitor vs Shell vs Workflow vs Subagent**: All four are external actions.
  - **Shell** — one-shot command, single result event.
  - **Workflow** (2026-06-27, replaces the old ephemeral "Subagent") — fire-and-forget dispatch of N parallel worker agents (each an isolated sub-LLM cascade) with an optional adversarial verify pass and an optional synthesis agent; exactly one `ToolResultArrived(tool="Workflow")` result event returns to Doll regardless of N. `mode="map_reduce"` (parallel fan-out) or `mode="verify"` (+ skeptic pass per worker). N=1 with no synthesis degrades to the old single-subagent behavior. See `docs/superpowers/plans/2026-06-27-doll-workflow.md`.
  - **Monitor** — long-running command, per-line trigger events (regex + rate-limit) + exit event. Stateless watcher; no LLM in the loop. Active state surfaces via `[Active monitors]` perception block.
  - **Subagent** (future, replaces the old "Drone") — persistent agent with its own LLM cascade, scheduled trigger, can call tools and Report back. Authoritative design is `agent-service` branch A2 (containerized k8s Job/CronJob); the k3s side of that is deferred until this track is picked up.
- **Computer-as-home**: Doll lives in DollOS on the user's computer. Memory SoT, personality, identity vault, decisions all on-device.
- **Phone as remote**: Phone app talks to DollOS over network WS. Phone never holds memory.
- **BYO big LLM**: DollOS has a single LLM dependency (port 8001). No small model (Inner Voice removed 2026-05-16). Large model picked by user (Anthropic / OpenAI / OpenAI-compat / self-host llama.cpp).
- **Memory wire format**: `memsearch.search(query, top_k)` → bullet list → `[Memory context]` block prepended to the user message. Explicit `Recall` pydantic tool for on-demand deeper search. No prefill into `<think>`. Backend-portable.
- **Event-loop centric**: Doll is not a chatbot. She's an event-driven agent. Conversation is one event source among many (voice, text, schedule, system events, subagent results, self-initiated).
- **Workflow (ephemeral) vs Subagent (persistent)**: Workflow is a one-shot tool call, definition inline, dies after run (this is the renamed old "Subagent"). Subagent has persistent definition, scheduled trigger, runs in background, results re-enter the event queue (this is the renamed old "Drone").
- **Self-First**: `system_prompt` is identity description ("you are Gura, ..."), NOT behavior commands ("you should be self-first"). Self emerges from character description + Doll's own memory entries (self-history, preferences, mood) surfacing through the `[Memory context]` block + `Recall` tool. The 2026-05-08 smoke confirmed Self-First behavior emerges this way (T2 「我自己愛冰美式，你呢？」).
- **Memory SoT**: memsearch (Milvus Lite + ONNX bge-m3 + markdown daily summary files). `data/memory/shared/` for shared facts, `data/memory/{character_id}/` for per-character private (step 10). Hybrid retrieval (dense + BM25 + RRF) provided by memsearch.
- **Audio**: KWS optional on phone (opt-in). ASR / TTS run in DollOS. Phone streams audio over WS.

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

### 已歸檔（被後續 step 取代）

| Plan | Status |
|---|---|
| Roadmap step 22 — Async Shell + Monitor + ProcessRegistry (Phase 2 of 4) | Superseded by step 24 |
| Roadmap step 23 — Cancel + interrupt-aware Monitor (Phase 3 of 4) | Superseded by step 24 |

### 下一個

- **Virtual-being 定位剩餘兩項**（positioning spec §2.3 self-directed agenda、§2.4 interaction language — §2.4 明文依賴 §2.2/§2.3 先存在；慢變演化 spec §7 另記 deferred：mood history、trajectory Recall surface、Shell sandboxing track）
- **Subagent**（persistent agents，取代舊名 Drone — 跟 Workflow 對偶；Monitor 是無大腦版，Subagent 是有大腦版。權威設計在 `agent-service` branch A2〔containerized k8s Job/CronJob〕，k3s 化本身暫緩，撿起這條線時才處理）
- **Zero-shot wake word + Speaker ID**（取代 train-per-character KWS；研究 CLAP-like embedding）
- **回應延遲壓縮**（LLM-side 工程，見 memory）
- **Wake gating** — 等 voice / subagent events 進來才有 ROI

**已收的設計準則**：
- 偏好學習 / 習慣學習不是新 subsystem，是 prompt engineering — Doll 用 NoteMemory 自記
- Bootstrap 是 daemon-internal planning，用 dummy sink 不對外發送（用戶 greeting 由 scheduled entry 提供）

完整 roadmap：`docs/roadmap.md`。

## Build / Run

### DollOS (Python)

Once Plan 1 is implemented:

```bash
cd daemon
uv sync
cp config.example.toml config.toml
# edit config.toml to point at your llama-server / model_id
uv run python -m dollos --config config.toml
uv run pytest                   # tests
```

To also run the Discord bridge in dev, in a second terminal:

```bash
uv run python -m dollos.discord_bridge --daemon ws://127.0.0.1:9876 --config <bridge>.toml --data-root data
```

### `dollosctl` — run the full stack as systemd `--user` services (recommended)

The manual invocations above are for iterating on the code. To dogfood
DollOS running continuously (daemon + Discord bridge, both auto-restarting
on crash, both surviving terminal/session close), install them as
`systemd --user` services via the `dollosctl` console script
(`src/dollos/ctl/`, P1g):

```bash
uv sync   # installs the dollosctl console script (pyproject [project.scripts])

uv run dollosctl install \
    --daemon-config config.toml \
    --bridge-config <bridge>.toml \
    --data-root data
# writes ~/.config/systemd/user/dollos-{daemon,bridge}.service + daemon-reload

uv run dollosctl start      # daemon, then bridge
uv run dollosctl status     # both units should show active (running)
uv run dollosctl logs daemon -f    # follow the daemon's journal
uv run dollosctl logs bridge -f    # follow the bridge's journal
uv run dollosctl restart    # daemon, then bridge
uv run dollosctl stop       # bridge, then daemon
uv run dollosctl uninstall  # stop both + remove the unit files
```

The bridge unit soft-depends on the daemon unit (`Wants=`+`After=`, never
`Requires=`) since the bridge already auto-reconnects to the daemon's WS
server — restarting the daemon does not take the bridge down with it. Both
units set `Restart=on-failure`. To also auto-start on boot / without an
active login session: `systemctl --user enable dollos-daemon.service
dollos-bridge.service` + `loginctl enable-linger $USER`.

Full human live-smoke checklist (real systemd start/stop, real Discord
bot, private test server — can't run in CI): `docs/dollosctl-smoke.md`.

### Self-host llama.cpp big model (recommended for grammar / think structure)

```bash
./llama.cpp/llama-server \
    -hf HauhauCS/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:Q4_K_M \
    --alias "unsloth/Qwen3.6" \
    --jinja \
    --reasoning-format none \
    --chat-template-kwargs '{"enable_thinking": true}' \
    --temp 0.6 --top-p 0.95 --top-k 20 --min-p 0.00 \
    --ctx-size 131072 --fit on \
    --cache-type-k q8_0 --cache-type-v q8_0 \
    --flash-attn on --cont-batching --parallel 2 \
    -ngl 99 --tensor-split 1,1 \
    --batch-size 2048 --ubatch-size 512 \
    --threads 8 --keep -1 \
    --port 8001 --host 0.0.0.0
```

`--reasoning-format none` is REQUIRED for grammar to apply inside `<think>` blocks (B4-typed think structure).

### Local embedding server (separate llama-server with `--embedding`)

```bash
./llama.cpp/llama-server \
    -hf <embedding-model-gguf> \
    --embedding \
    --port 8002 --host 0.0.0.0
```

## Specs and Plans

- `docs/superpowers/specs/` — current design specs
- `docs/superpowers/plans/` — current implementation plans
- `docs/superpowers/archive/` — superseded specs/plans (pre-2026-05-01 pivot)
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

## Architecture (post-pivot, target state)

```
電腦端（DollOS）
  Event Loop ── Doll Turn（大模型 + [Memory context] block + Recall tool）
                  ↓ tool calls
              Workflow（即時）/ Subagent（持久，取代舊名 Drone）
              Memory SoT（memsearch: Milvus Lite + ONNX bge-m3）
              Character Pack Manager（.doll v3）
              Voice Pipeline Server（ASR/TTS）
              IPC Server (localhost WS / network WS)

UI（Tauri + Cubism Web）— 透過 localhost WS
Phone App（Android, system assistant）— 透過 network WS
```

## Voice Pipeline Architecture (target)

| Component | Where | Notes |
|---|---|---|
| KWS (openWakeWord) | Phone, opt-in | Per-character `wake_word.onnx` in `.doll` pack |
| VAD (silero) | Phone | Endpoint detection |
| Audio streaming | Phone ↔ DollOS, WS binary | Opus encoding |
| ASR | DollOS | whisper.cpp / sherpa-onnx |
| TTS | DollOS | Piper VITS (per-character voice in `.doll`) |
| Speaker ID | Phone | ECAPA-TDNN |
| Lip sync | DollOS → UI/App | phoneme / viseme stream |

### Wake Word Training (existing, kept)

`~/Projects/DollOS/wake_word_training/`:
- `train_gura.py` — custom training (AudioFeatureExtractor → DNN → ONNX export)
- `generate_positive.py` / `generate_negative.py` — fish-tts sample generation
- `verify_voice.py` — speaker embedding cosine similarity verification
- ACAV100M features (17.3GB) for negative training data
- Correct `embedding_model.onnx` must match the openWakeWord Python package version

### TTS Distillation (existing, kept)

Piper VITS distilled from fish-tts voice cloning data. Training scripts at `~/Projects/DollOS/wake_word_training/`.
