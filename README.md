# DollOS

DollOS is the home of **Doll**, a virtual being with her own memory, moods, agenda and diary. She is not a chatbot: she is an event-driven agent, and conversation is only one of the things that wake her up.

**Current direction (2026-10-17):** the daemon runs on a Mac first, with no UI yet; the big LLM (Qwen3.8-Flash-Next) is served by llama.cpp on another machine. A desktop-pet overlay comes later, and later still the iPhone becomes her physical body.

## Architecture

```
Inputs                          DollOS daemon (Python, one process)
本機 IPC (WS)      ─┐
Voice (WebRTC)     ─┤           Perception queue (with WAL)
Discord bridge     ─┼──────►          │
MCP (external AI)  ─┤                 ▼
Schedule, pulse    ─┤           MindLoop (single consumer, one batch = one turn)
Agenda, diary      ─┘             ├─ context: identity + memory + state
                                  ├─ big LLM (llama.cpp on another machine)
                                  ├─ speech → TTS / Discord / IPC
                                  └─ tools → external actions are fire-and-forget;
                                             results come back as perceptions
```

- **Memory**: markdown files are the source of truth, indexed with SQLite FTS5 + jieba. No vector store.
- **Self**: mood, a slowly evolving `current_self`, a self-directed agenda and a daily diary are her own inner state. Her self comes from the architecture and her own memories, not from behavior commands in the prompt (Self-First).
- **Voice**: ASR with sherpa-onnx (SenseVoice). TTS engines are pluggable (`luxtts-onnx`, `qwen3-tts`, `fish-tts`, `piper-vits`, `openai-compat-tts`).
- **Permissions**: what a turn may do depends on who triggered it. Strangers' turns only see memory written in strangers' turns.

## What's changing

- **Harness rebuild** (approved, `docs/superpowers/specs/2026-10-17-doll-harness-design.md`): split `MindLoop` into a designed harness with one turn runner, switch to native tool calling (no grammar), cut the tools down to about 15, extend her through MCP servers, and give each turn only the tools its trigger is allowed.
- **iPhone body** (draft, `docs/superpowers/specs/2026-10-17-dollos-v1-iphone-body-design.md`): senses and presentation only; no model on the phone.
- **Lighter voice install**: aim for ONNX Runtime only on the Mac, no torch.

## Repo layout

```
DollOS/
├── src/dollos/          # the daemon
├── tests/               # pytest suite
├── character_packs/     # Doll packs (doll.toml + voice/)
├── docs/                # roadmap, specs, plans, research
├── scripts/             # persistent tools
├── experiments/         # spikes
├── vendor/              # third-party SDK drop instructions
└── data/                # runtime data (gitignored)
```

## Related repos

| Repo | Status | Role |
|------|--------|------|
| [fish-tts](https://github.com/ningyos/fish-tts) | Active | TTS engine (DualARTransformer + DAC) |
| [luxtts-onnx](https://github.com/ningyos/luxtts-onnx) | Active | TTS engine (ONNX Runtime, no PyTorch) |
| [tuna](https://github.com/ningyos/tuna) | Active | Fine-tuning tools |
| piper-finetune | Bootstrap | Piper VITS fine-tuning for character voices |

Pre-pivot Android repos (retired 2026-05-01) are listed in `docs/RETIRED-REPOS.md`.

## Setup

```bash
uv sync                               # Python 3.13 (see .python-version)
cp config.example.toml config.toml    # point [llm].base_url at your llama.cpp server
uv run python -m dollos --config config.toml
uv run pytest
```

See `CLAUDE.md` for the full developer guide, including `dollosctl` (run as a service) and llama.cpp server requirements.
