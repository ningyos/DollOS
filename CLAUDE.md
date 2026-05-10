# DollOS — Claude Code Instructions

## What is DollOS

DollOS is a personal AI ecosystem. **Doll lives on your computer.** The computer is her brain (daemon process). The phone is an optional body / system-assistant interface to reach her on the go.

**Product positioning (2026-05-01 pivot — see `docs/superpowers/specs/2026-05-01-dollos-pivot-to-computer-design.md`):**

- **Doll** — the AI companion herself. Soul, memory, personality, decisions all live in DollOS on the computer.
- **DollOS** — Python process: event loop + Inner Voice (Instinct) + Conversation Engine + Memory SoT + Voice Pipeline + IPC server.
- **DollOS UI** — Tauri (Rust shell + Web frontend) with Cubism Web SDK rendering Live2D. Win/Mac get transparent overlay desktop pet; Linux gets a normal window.
- **DollOS-App** — Android app. Registers as system assistant via `VoiceInteractionService`. Cubism Java SDK. Audio I/O streams to/from DollOS.
- **Big LLM** — user's choice (cloud API / self-host llama.cpp). DollOS only hosts the **small Inner Voice model** (0.6B–1.7B) locally.
- **VoM (Voice of Mind) + grammar injection** — small model digests memory + events; result reaches the big model via a `[Memory context]` block prepended to the user message + an explicit `Recall` tool. **Wire format pivoted 2026-05-08**: structured STATE/RECALL prefill into `<think>` was abandoned (LLM training distribution has no precedent for ReAct-style memory labels → mimicry / list-continuation failure). Two-tier architecture preserved; only the wire format between layers changed. See `docs/research/grammar_injection_techreport.md` (think-block grammar) and `docs/superpowers/plans/2026-05-08-memory-rag-tool.md` (current wire format).
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

- **External actions are fire-and-forget**: Shell and SpawnSubagent both spawn background workers and return immediately; results re-enter the event queue as `{Tool}ResultEvent`. There is no Doll-callable wait/cancel tool. "Wait" is implicit — Doll's cascade either keeps going (and the result triggers a new turn after) or ends (and the result triggers a new turn). Internal capabilities (Say / NoteMemory / Recall / Mood) are sync inline — Doll cannot await her own mouth.
- **Computer-as-home**: Doll lives in DollOS on the user's computer. Memory SoT, personality, identity vault, decisions all on-device.
- **Phone as remote**: Phone app talks to DollOS over network WS. Phone never holds memory.
- **BYO big LLM**: DollOS hosts only the small Inner Voice model. Large model picked by user (Anthropic / OpenAI / OpenAI-compat / self-host llama.cpp).
- **VoM wire format**: Inner Voice's small-LLM-filtered recall result is wrapped in a `[Memory context]` block prepended to the user message; the explicit `Recall` pydantic tool gives Doll on-demand deeper search (raw memsearch hits). No prefill into `<think>`. Backend-portable (works on any provider that accepts a user message).
- **Event-loop centric**: Doll is not a chatbot. She's an event-driven agent. Conversation is one event source among many (voice, text, schedule, system events, drone results, self-initiated).
- **Subagent (ephemeral) vs Drone (persistent)**: Subagent is a one-shot tool call, definition inline, dies after run. Drone has persistent definition, scheduled trigger, runs in background, results re-enter the event queue.
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
| Roadmap step 22 — Async Shell + Monitor + ProcessRegistry (Phase 2 of 4) | Superseded by step 24 |
| Roadmap step 23 — Cancel + interrupt-aware Monitor (Phase 3 of 4) | Superseded by step 24 |
| Roadmap step 24 — External actions = fire-and-forget (Shell ≈ Subagent) | Merged |

### 下一個

- **真 Monitor watcher**（fire-and-forget command runner with stdout-line-as-event）— 用戶原本構想的 Monitor，跟 Drone 對偶
- **Voice pipeline**（基礎建設，跟 Doll 行為無關）
- **Drone**（persistent agents — 跟 Subagent 對偶）
- **Wake gating** — 等 voice / drone events 進來才有 ROI

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

### Self-host llama.cpp big model (recommended for grammar / think structure)

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
  Event Loop ── Instinct（Inner Voice 小模型 + 規則 + reflex）
                  ↓ wake / drop / fire
              Doll Turn（大模型 + [Memory context] block + Recall tool）
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
