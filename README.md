# DollOS

Personal AI Ecosystem — your AI companion lives on your computer. The phone is an optional body / system-assistant interface.

## Architecture

```
電腦端（DollOS）
  Event Loop ── Instinct（small model + rules + reflex）
                  ↓ wake / drop / fire
              Doll Turn（large model + VoM/SELF_STATE prefill）
                  ↓ tool calls
              Subagent（ephemeral）/ Drone（persistent）
              Memory SoT（sqlite-vec + FTS5）
              Character Pack Manager（.doll v3）
              Voice Pipeline Server（ASR/TTS）
              IPC Server (localhost WS / network WS)

UI（Tauri + Cubism Web）   ←→   localhost WS
DollOS-App（Android）       ←→   network WS
```

**Computer = soul + brain.** Memory, personality, decisions all live in DollOS on the user's computer.

**Phone = body / interface.** Android app registered as system assistant via `VoiceInteractionService`. Reaches Doll over network WebSocket. Optional — a computer alone is a complete experience.

**BYO big LLM.** DollOS only hosts a small Inner Voice model (0.6B–1.7B). Big model is user's choice (cloud API or self-hosted llama.cpp).

**Signature feature: VoM + grammar injection.** Inner Voice synthesizes a RECALL block and prefills it into the big model's `<think>` region. See `docs/research/grammar_injection_techreport.md`.

**Killer feature: Self-First Design.** Doll has a self (mood / preferences / habits / relations). Self emerges from architecture, not from prompt commands. See `docs/superpowers/specs/2026-05-01-dollos-pivot-to-computer-design.md` §8.

## Repo Layout

```
DollOS/
├── daemon/                # Python brain (Plans 1–7)
├── ui/                    # Tauri + Cubism Web (Plan 8)
├── protocol/              # shared schema (daemon ↔ ui ↔ app)
├── character_packs/       # .doll v3 examples
├── docs/
│   ├── superpowers/specs/ # design docs
│   ├── superpowers/plans/ # implementation plans
│   ├── superpowers/archive/  # superseded pre-pivot docs
│   ├── research/          # research outputs (e.g. grammar_injection_techreport.md)
│   └── RETIRED-REPOS.md
├── experiments/           # POC code
├── vendor/                # third-party SDK fetch instructions
└── wake_word_training/    # wake word + TTS distillation training scripts
```

## Related Repos

| Repo | Status | Role |
|------|--------|------|
| **DollOS-App** | Future | Android app (Cubism Java SDK + Assistant role). Not started. |
| [fish-tts](https://github.com/ningyos/fish-tts) | Active | TTS engine (DualARTransformer + DAC) |
| [luxtts-onnx](https://github.com/ningyos/luxtts-onnx) | Active | TTS engine (ONNX Runtime, no PyTorch) |
| [tuna](https://github.com/ningyos/tuna) | Active | Fine-tuning tools |

Pre-pivot Android-side repos (retired 2026-05-01) are documented in `docs/RETIRED-REPOS.md`.

## Setup

```bash
git clone https://github.com/ningyos/DollOS.git ~/Projects/DollOS
cd ~/Projects/DollOS
# Then follow the relevant plan in docs/superpowers/plans/ for the subsystem
# you want to build (Plan 1: DollOS Skeleton is the entry point).
```
