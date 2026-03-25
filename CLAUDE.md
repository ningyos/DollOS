# DollOS — Claude Code Instructions

## What is DollOS

DollOS is a personal AI ecosystem. Your AI companion lives across your phone and computer.

- **DollOS-Server** (computer) — GuraOS microkernel AI operating system. Unified agent loop (GuraCore), cognitive stack, tool system, memory (memsearch: Markdown + sqlite-vec + FTS5). Kernel modules (kmod) are out-of-process NATS services: LLM (Grok cloud + local Qwen3-VL via vLLM), TTS (fish-tts), STT (FunASR), Vision (Qwen3-VL), Desktop UI. NATS is the central message bus (replaced RabbitMQ). Storage: sqlite-vec for vectors (replaced Milvus), RustFS for files. Python, Docker, uv workspaces.
- **DollOS-Android** (phone) — Custom Android OS based on GrapheneOS Android 16 for Pixel 6a (bluejay). Deep AOSP integration: custom Launcher with 3D avatar (Filament), AI system services, power menu AI controls, hardware security.
- **Character Packs** (.doll files) — Zip bundles containing 3D avatar model (glTF), personality prompts, voice config, scene config, animations. Users import/export/switch characters.

The phone is the body (always with you, sensors, system control). The computer is the brain (heavy compute, source of truth for memory).

## Repo Map

All repos live under `~/Projects/`. Run `./sync.sh` from this repo to clone/pull all.

| Repo | Path | What it is |
|------|------|------------|
| **DollOS** | `~/Projects/DollOS/` | THIS REPO — umbrella, all specs/plans/docs, sync script |
| **DollOS-Server** | `~/Projects/DollOS-Server/` | Server backend (Python, GuraOS microkernel, NATS, kmod plugins, Docker). Branch: `dev` |
| **DollOS-Android** | `~/Projects/DollOS-Android/` | AOSP overlay configs (was the old DollOS repo) |
| **DollOSAIService** | `~/Projects/DollOSAIService/` | Android AI Service app (Kotlin, Gradle). LLM client, conversation engine, memory (ObjectBox + Room FTS4), personality, agent system, background workers, character pack manager. Binds via AIDL. |
| **DollOSLauncher** | `~/Projects/DollOSLauncher/` | Android 3D Launcher app (Kotlin, Gradle, Filament). Full-screen 3D avatar scene, conversation bubble, app drawer, character picker. |
| **DollOSService** | in AOSP tree | Android system service (system UID). Executes agent actions (open app, set alarm, toggle WiFi/BT), hosts TaskManagerActivity for emergency stop. |
| **DollOSSetupWizard** | in AOSP tree | Android OOBE (theme picker, GMS toggle, API key setup) |
| **DollOS-build** | `~/Projects/DollOS-build/` | Full AOSP build tree (GrapheneOS manifest + local_manifests). `lunch dollos_bluejay-bp2a-userdebug` |
| **fish-tts** | `~/Projects/fish-tts/` | TTS engine: DualARTransformer + DAC vocoder |
| **luxtts-onnx** | `~/Projects/luxtts-onnx/` | TTS engine: LuxTTS ONNX (no PyTorch) |
| **tuna** | `~/Projects/tuna/` | Fine-tuning tools |

## Key Architecture Decisions

- **Client-Server**: Phone ↔ Computer via DollOS Protocol (to be designed). Phone sends audio/text, computer returns TTS audio/LLM responses. Server uses NATS as central message bus; kernel modules (LLM, TTS, STT, Vision) are out-of-process NATS services.
- **AIDL IPC**: On Android, DollOSAIService ↔ DollOSService ↔ DollOSLauncher communicate via AIDL Binder.
- **Character Pack (.doll)**: Zip file with manifest.json, personality.json, voice.json, scene.json, model.glb, animations/, wake_word.bin, thumbnail.png. Managed by CharacterManager in DollOSAIService.
- **Memory**: Markdown is source of truth. ObjectBox for vector search (brute-force cosine, no HNSW). Room FTS4 for keyword search. Per-model vector store (modelId field). Shared memory across characters + per-character private notes.
- **Event-driven AI**: Foreground AI has an EventQueue. Events piggyback on sendMessage() or process during idle. Background workers use background LLM model with skill-based action whitelists.
- **3D Avatar**: Google Filament on TextureView. glTF 2.0 models. Animation states: IDLE → THINKING → TALKING. Character assets loaded via AIDL ParcelFileDescriptor.
- **Embedding**: Cloud (any OpenAI-compatible endpoint) + Local (ONNX Runtime). Dynamic dimensions, per-model storage, auto-rebuild.
- **Testing**: Real device (Pixel 6a bluejay), not emulator. `adb` at `~/Android/Sdk/platform-tools/adb`.

## Build Commands

### DollOSAIService (Gradle → prebuilt → AOSP)
```bash
cd ~/Projects/DollOSAIService
./gradlew assembleRelease
cp app/build/outputs/apk/release/app-release-unsigned.apk prebuilt/DollOSAIService.apk
rsync -av --delete . ~/Projects/DollOS-build/external/DollOSAIService/
cd ~/Projects/DollOS-build
source build/envsetup.sh && lunch dollos_bluejay-bp2a-userdebug
m DollOSAIService -j$(nproc)
```

### DollOSLauncher (same pattern)
```bash
cd ~/Projects/DollOSLauncher
./gradlew assembleRelease
cp app/build/outputs/apk/release/app-release-unsigned.apk ~/Projects/DollOS-build/packages/apps/DollOSLauncher/prebuilt/DollOSLauncher.apk
cd ~/Projects/DollOS-build
source build/envsetup.sh && lunch dollos_bluejay-bp2a-userdebug
m DollOSLauncher -j$(nproc)
```

### DollOSService (built directly in AOSP tree)
```bash
cd ~/Projects/DollOS-build
source build/envsetup.sh && lunch dollos_bluejay-bp2a-userdebug
m DollOSService -j$(nproc)
```

### Settings app
```bash
cd ~/Projects/DollOS-build
m Settings -j$(nproc)
```

### Deploy to device
```bash
export PATH="$HOME/Android/Sdk/platform-tools:$PATH"
adb root && adb remount
adb push <apk/odex/vdex> /system_ext/priv-app/<AppName>/
adb reboot
```

## DollOS-Server (Python)
```bash
cd ~/Projects/DollOS-Server
git checkout dev              # latest code is on dev branch
uv sync
docker compose up -d          # NATS, Milvus, RustFS
uv run dollos-server start    # boots GuraOS + kmod services
```
Requires: Docker (NATS, RustFS), NVIDIA GPU with CUDA for kmod workers (vLLM, FunASR, fish-tts).
**Note**: `main` branch is outdated. All active development is on `dev`.

### Server Architecture (GuraOS)
- **NATS** — central message bus (IPC, kmod routing, driver communication)
- **GuraCore** — unified agent loop, cognitive stack, triage
- **Kernel Modules (kmod)** — out-of-process NATS services: grok (LLM), qwen3-vl (Vision+LLM), fun-asr (STT), fish-speech (TTS), audio-speaker, desktop
- **Memory** — memsearch: Markdown source of truth + sqlite-vec (vectors) + FTS5 (keywords)
- **Drivers** — Discord, PC, Phone, Desktop (via NATS or in-process WebSocket)
- **GuraVerse** — sub-agent spawning (TinyGura)

## Specs and Plans

All design specs and implementation plans live in `~/Projects/DollOS/docs/superpowers/`:
- `specs/` — design documents (what to build)
- `plans/` — implementation plans (how to build, task-by-task with checkboxes)

Read the relevant spec before starting any work.

## Coding Rules

- **Language**: Respond in Traditional Chinese (繁體中文)
- **Subagents for coding**: Always use subagents for implementation. Dispatch one subagent per task.
- **Phone operations in subagents**: All adb, screenshots, device interaction must run in subagents to avoid images consuming context.
- **No fallback mechanisms**: Never implement fallback/degradation logic.
- **Don't overthink base**: Don't tear apart upstream packages to reassemble yourself. Use upstream as-is.
- **Background commands**: Don't use tail pipes on background commands.
- **Specs before code**: Always write/update the spec before implementing. Get user approval on design.

## Current Status (2026-03-25)

### Completed
- DollOS Base (AOSP 16, OOBE, theme, GMS, system defaults)
- AI Core Plan A (LLM client, personality, usage tracking)
- AI Core Plan B (Memory system, conversation engine, context compression)
- AI Core Plan C (Agent system, tool calling, emergency stop)
- AI Core Plan D v1 (Event queue, background workers, schedules, system events)
- AI Core Plan D v2 (UI operation via AccessibilityService + VirtualDisplay, smart notification, programmable events)
- Embedding System (Cloud + Local ONNX, per-model vector store, retrieval modes)
- Settings UI (restructured: Stats + Personality main page, LLM / Memory / Budget sub-pages)
- Character Pack System (.doll format, import/export/switch)
- AI Launcher (Filament 3D, conversation bubble, app drawer, character picker)

### In Progress
- DollOS Protocol design (phone ↔ computer communication)

### Next Up
- DollOS Protocol spec + implementation
- Voice Pipeline (STT + TTS via DollOS-Server)
- Default character pack (bundled in system image)
- Wake word
