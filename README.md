# DollOS

Personal AI Ecosystem — your AI companion lives on your phone. Computers are optional body extensions.

## Architecture

```
DollOS
├── DollOS-Android   — Phone (AOSP 16): AI companion (brain + body)
├── DollOSAIService  — AI: LLM, memory, conversation, agents, character packs
├── DollOSLauncher   — 3D avatar Launcher (Filament)
├── DollOSObserver   — Sensor host: sensors, VAD, placement, sleep detection
├── Bridge           — (future) Computer extension daemon
└── Character Packs  — .doll files: personality, 3D model, voice, scene
```

**Phone = Brain + Body.** All AI intelligence, memory, personality, and identity live on the phone. A phone alone is a complete experience.

**Bridge = Computer extension.** Optional daemon that lets Doll inhabit your computer. Two modes: Transient (USB-C, untrusted) and Drone (network, trusted, long-term).

**Character Packs = Replaceable identity.** .doll files bundle personality, 3D model, voice, and scene. Switch characters without losing your memory.

## Repos

| Repo | Description |
|------|-------------|
| [DollOS](https://github.com/ningyos/DollOS) | This repo — docs, specs, plans, sync script |
| [DollOS-Android](https://github.com/ningyos/DollOS-Android) | Android OS customization (AOSP 16 + GrapheneOS) |
| [DollOSAIService](https://github.com/ningyos/DollOSAIService) | Android AI Service (conversation, memory, agents) |
| [DollOSLauncher](https://github.com/ningyos/DollOSLauncher) | Android 3D AI Launcher (Filament) |
| [DollOSObserver](https://github.com/ningyos/DollOSObserver) | Sensor host (sensors, VAD, placement, sleep) |
| [DollOSService](https://github.com/ningyos/DollOSService) | Android system service |
| [DollOSSetupWizard](https://github.com/ningyos/DollOSSetupWizard) | Android OOBE |
| [fish-tts](https://github.com/ningyos/fish-tts) | TTS engine (DualARTransformer + DAC) |
| [luxtts-onnx](https://github.com/ningyos/luxtts-onnx) | TTS engine (ONNX Runtime, no PyTorch) |
| [tuna](https://github.com/ningyos/tuna) | Fine-tuning tools |

## Setup

```bash
git clone https://github.com/ningyos/DollOS.git ~/Projects/DollOS
cd ~/Projects/DollOS
./sync.sh
```

This clones all repos into `~/Projects/`.
