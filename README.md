# DollOS

Personal AI Ecosystem — your AI companion lives across your phone and computer.

## Architecture

```
DollOS
├── DollOS-Server    — Computer (GPU): LLM, TTS, STT, Vision, Web UI
├── DollOS-Android   — Phone (AOSP 16): Launcher, Avatar, System Control
└── Character Packs  — .doll files: personality, 3D model, voice, scene
```

**Computer = Brain.** Runs heavy AI inference (LLM via vLLM, TTS via luxTTS/fish-tts, STT, Vision). Provides a Web UI for management and interaction.

**Phone = Body.** Deep Android OS integration (custom Launcher with 3D avatar, system control, hardware security). Always with you.

**Memory syncs between both.** Same AI, same memory, regardless of which device you're talking to.

## Repos

| Repo | Description |
|------|-------------|
| [DollOS](https://github.com/ningyos/DollOS) | This repo — docs, specs, plans, sync script |
| [DollOS-Server](https://github.com/ningyos/DollOS-Server) | Server-side AI (vLLM, TTS, STT, Web UI) |
| [DollOS-Android](https://github.com/ningyos/DollOS-Android) | Android OS customization (AOSP 16 + GrapheneOS) |
| [DollOSAIService](https://github.com/ningyos/DollOSAIService) | Android AI Service (conversation, memory, agents) |
| [DollOSLauncher](https://github.com/ningyos/DollOSLauncher) | Android 3D AI Launcher (Filament) |
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
