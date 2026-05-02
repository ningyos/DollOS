# DollOS — Claude Code Instructions

## What is DollOS

DollOS is a personal AI ecosystem. **Doll lives on your computer.** The computer is her brain (daemon process). The phone is an optional body / system-assistant interface to reach her on the go.

**Product positioning (2026-05-01 pivot — see `docs/superpowers/specs/2026-05-01-dollos-pivot-to-computer-design.md`):**

- **Doll** — the AI companion herself. Soul, memory, personality, decisions all live in the daemon on the computer.
- **DollOS Daemon** — Python process: event loop + Inner Voice (Instinct) + Conversation Engine + Memory SoT + Voice Pipeline + IPC server.
- **DollOS UI** — Tauri (Rust shell + Web frontend) with Cubism Web SDK rendering Live2D. Win/Mac get transparent overlay desktop pet; Linux gets a normal window.
- **DollOS-App** — Android app. Registers as system assistant via `VoiceInteractionService`. Cubism Java SDK. Audio I/O streams to/from daemon.
- **Big LLM** — user's choice (cloud API / self-host llama.cpp). DollOS only hosts the **small Inner Voice model** (0.6B–1.7B) locally.
- **VoM (Voice of Mind) + grammar injection** — the signature feature: small model digests memory + events, prefills the result into the big model's `<think>` block. See `docs/research/grammar_injection_techreport.md`.
- **Self-First Design** — killer feature: Doll has a self (mood / preferences / habits / relations). Self emerges from architecture (Instinct's involuntary state + Memory's self-history + character description), not from prompt commands. See main spec §8.

**Three-layer intelligence:**
1. **Instinct (Inner Voice)** — small model, always-on, reactive. Digest / classify / triage / decide-to-wake / reflex / VoM recall. Per-event preprocessing.
2. **Doll** — user's chosen large model, deliberative. Wakes when Instinct decides this event needs her conscious attention.
3. **Subagent / 分身** (ephemeral) and **Drone** (persistent) — task-specific agents Doll dispatches.

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

- **Computer-as-home**: Doll lives in the daemon on the user's computer. Memory SoT, personality, identity vault, decisions all on-device.
- **Phone as remote**: Phone app talks to daemon over network WS. Phone never holds memory.
- **BYO big LLM**: Daemon hosts only the small Inner Voice model. Large model picked by user (Anthropic / OpenAI / OpenAI-compat / self-host llama.cpp).
- **VoM prefill**: Inner Voice synthesizes a RECALL block and prefills it into the big model's `<think>` region. Backend support varies (full on Anthropic + llama.cpp `/completion`; degraded on strict OpenAI).
- **Event-loop centric**: Doll is not a chatbot. She's an event-driven agent. Conversation is one event source among many (voice, text, schedule, system events, drone results, self-initiated).
- **Subagent (ephemeral) vs Drone (persistent)**: Subagent is a one-shot tool call, definition inline, dies after run. Drone has persistent definition, scheduled trigger, runs in background, results re-enter the event queue.
- **Self-First**: `system_prompt` is identity description ("you are Gura, ..."), NOT behavior commands ("you should be self-first"). Self emerges from prefilled `SELF_STATE` block + self-memory in RECALL.
- **Memory SoT**: sqlite-vec + FTS5, hybrid retrieval via Reciprocal Rank Fusion (k=60). Single active embedding model; switching = explicit rebuild. Character scoping: `character_id` NULL = shared, otherwise private to that character.
- **Audio**: KWS optional on phone (opt-in). ASR / TTS run in daemon. Phone streams audio over WS.

## Implementation Plans (12 total)

See `docs/superpowers/plans/` for written plans, main spec §11.6 for the full list.

| # | Plan | Status |
|---|---|---|
| 1 | Daemon Skeleton | Plan written |
| 2 | Memory SoT 儲存層 | Plan written |
| 3 | Inner Voice + Instinct + VoM | Concept |
| 4 | Multi-LLM adapter | Concept |
| 5 | Conversation Engine + Character Pack | Concept |
| 6 | Subagent | Concept |
| 7 | Self-First Design | Concept |
| 8 | DollOS UI MVP (Tauri + Cubism Web) | Concept |
| 9 | DollOS-App MVP (Android) | Concept |
| 10 | Voice pipeline integration | Concept |
| 11 | Phone Tier B/C/D adapter | Concept |
| 12 | Drone | Concept |

## Build / Run

### DollOS Daemon (Python — `daemon/`)

Once Plan 1 is implemented:

```bash
cd daemon
uv sync
cp config.example.toml config.toml
# edit config.toml to point at your llama-server / model_id
uv run python -m dollos --config config.toml
uv run pytest                   # tests
```

### Self-host llama.cpp big model (recommended for full VoM)

```bash
./llama.cpp/llama-server \
    -hf unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_XL \
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

`--reasoning-format none` is REQUIRED for grammar / prefill to apply inside `<think>` blocks.

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
- **No fallback mechanisms**: never implement fallback / degradation logic. State boundaries clearly; if a backend can't do prefill, surface "VoM degraded" to the user, don't silently rewrite the prompt.
- **Don't overthink upstream**: use upstream packages (llama.cpp, sqlite-vec, Cubism, etc.) as-is.
- **Specs before code**: update or write the spec before implementing. Get user approval on design.
- **Background commands**: don't pipe to `tail` on background commands; write to a file and read it.
- **Per-task user briefing**: before dispatching an implementation subagent, brief the user on the task's plan content; wait for OK; then dispatch.

## Architecture (post-pivot, target state)

```
電腦端（DollOS daemon）
  Event Loop ── Instinct（Inner Voice 小模型 + 規則 + reflex）
                  ↓ wake / drop / fire
              Doll Turn（大模型 + VoM/SELF_STATE prefill）
                  ↓ tool calls
              Subagent（即時）/ Drone（持久）
              Memory SoT（sqlite-vec + FTS5）
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
| Audio streaming | Phone ↔ Daemon, WS binary | Opus encoding |
| ASR | Daemon | whisper.cpp / sherpa-onnx |
| TTS | Daemon | Piper VITS (per-character voice in `.doll`) |
| Speaker ID | Phone | ECAPA-TDNN |
| Lip sync | Daemon → UI/App | phoneme / viseme stream |

### Wake Word Training (existing, kept)

`~/Projects/DollOS/wake_word_training/`:
- `train_gura.py` — custom training (AudioFeatureExtractor → DNN → ONNX export)
- `generate_positive.py` / `generate_negative.py` — fish-tts sample generation
- `verify_voice.py` — speaker embedding cosine similarity verification
- ACAV100M features (17.3GB) for negative training data
- Correct `embedding_model.onnx` must match the openWakeWord Python package version

### TTS Distillation (existing, kept)

Piper VITS distilled from fish-tts voice cloning data. Training scripts at `~/Projects/DollOS/wake_word_training/`.
