# DollOS — Claude Code Instructions

## What is DollOS

DollOS is a personal AI ecosystem. **Your AI companion (Doll) lives on your phone. Computers are optional body extensions she can be Bridged into.**

**Product positioning (2026-04-20 repositioning — see `docs/superpowers/specs/2026-04-20-doll-repositioning-design.md`):**

- **Doll (phone)** — The AI companion herself. Memory, personality, decisions, identity all live here. She is the brain AND the body. A phone alone is a complete DollOS experience.
- **Bridge (daemon on computer)** — Doll's extension into a specific computer. Two modes:
  - **Transient Bridge** — USB-C only, untrusted machines, one-shot sessions (library PC, friend's laptop)
  - **Drone Bridge** — Network, paired, long-term residence on trusted machines (your home desktop, work laptop)
- **Drone** — A trusted machine with a Drone Bridge installed (e.g., "my home desktop is a Drone")
- **Doll Mesh** — Optional mesh VPN for power users to let Doll orchestrate a fleet of devices (homelab, smart home). Not required for basic use.
- **Character Packs (.doll files)** — Zip bundles containing 3D avatar model (glTF), personality prompts, voice config, scene config, animations. Users import/export/switch characters.

**The phone is the body AND the brain.** Computers are optional extensions she can temporarily inhabit (Transient) or permanently reside in (Drone). The old "computer = brain" model is DEAD.

## Repo Map

All repos live under `~/Projects/`. Run `./sync.sh` from this repo to clone/pull all.

| Repo | Path | What it is |
|------|------|------------|
| **DollOS** | `~/Projects/DollOS/` | THIS REPO — umbrella, all specs/plans/docs, sync script |
| **DollOS-Server** | `~/Projects/DollOS-Server/` | ⚠️ **Being retired** — was Python GuraOS microkernel (NATS + kmod). Code being mined for Bridge/Drone rewrite (see 2026-04-20 spec). Do not build new features here. |
| **DollOS-Bridge** | *(to be created)* | New repo for `libbridge-core` + `bridge-transient` + `bridge-drone` (Rust/Go, TBD) |
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

- **Phone-as-home**: Doll lives entirely on the phone. Memory source of truth, personality, identity vault, policy engine, and decision making are all on-device.
- **Bridge/Drone for computers**: Phone extends to computers via Bridge daemons (see 2026-04-20 spec). Transient Bridge uses USB-C only (physical trust); Drone Bridge uses encrypted network (paired trust). No more "server as brain."
- **SSH as a tool**: Doll can SSH into remote hosts as a regular capability; SSH keys live in the phone's Identity Vault and never leave the device.
- **Doll Mesh (optional)**: Optional mesh VPN with pluggable provider (direct / managed-mesh via Headscale-or-Netbird / adopted-mesh). For homelab power users.
- **AIDL IPC**: On Android, DollOSAIService ↔ DollOSService ↔ DollOSLauncher communicate via AIDL Binder.
- **Character Pack (.doll)**: Zip file with manifest.json, personality.json, voice.json, scene.json, model.glb, animations/, wake_word.onnx, voice_reference.wav, thumbnail.png. Managed by CharacterManager in DollOSAIService.
- **Memory**: Phone is source of truth. ObjectBox for vector search (brute-force cosine, no HNSW). Room FTS4 for keyword search. Per-model vector store (modelId field). Shared memory across characters + per-character private notes. Drone Bridges have local working memory, NOT a replicated SoT.
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

## DollOS-Server (retiring)

⚠️ The `DollOS-Server` repo implements the deprecated "server as brain" model. It is being retired and mined for parts — the code will not continue to be built or deployed as-is. Do not start new work here.

**What survives and where it's going (per 2026-04-20 spec §7):**

| From DollOS-Server | Destination |
|--------------------|-------------|
| GuraCore agent loop | Drone Bridge's `bridge-subagent` module |
| vLLM / Qwen3-VL | Drone Bridge's `bridge-llm` / `bridge-vision` optional modules |
| fish-tts / FunASR | Drone Bridge optional modules |
| memsearch (Markdown + sqlite-vec + FTS5) | Drone Bridge's local working memory (NOT SoT — SoT stays on phone) |
| GuraVerse / TinyGura concept | Drone subagent spawning (finally in the right home) |

**What dies:**
- NATS as central message bus
- kmod microkernel abstraction
- Docker compose infrastructure
- `dollos-server` CLI and bootstrap
- The 4/2 DollOS Protocol v1 spec + plan (superseded)

The old build instructions (`uv sync`, `docker compose up -d`, `dollos-server start`) still work for archival/reference purposes but are not the active development path.

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

## Current Status (2026-04-20)

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
- Wake Word (openWakeWord 3-stage ONNX pipeline, retrained with fish-tts data + ACAV100M negatives)
- Voice Pipeline (on-device: ASR sherpa-onnx, TTS Piper VITS, VAD silero, KWS openWakeWord, Speaker ID)
- TTS Distillation (fish-tts voice cloning → 3447 sentences → Piper VITS single-speaker model, no reference audio needed)
- Launcher voice UX (tap to cancel listening/speaking, state indicator in bubble)
- **Product repositioning (2026-04-20)** — Bridge/Drone architecture spec complete, "server as brain" retired

### In Progress
- Writing implementation plan from the 2026-04-20 repositioning spec

### Next Up (new direction)
- `libbridge-core` minimal library (body capabilities + encryption)
- `bridge-transient` USB-C prototype
- Phone-side Identity Vault + Drone Registry UI
- `bridge-drone` network service
- First Drone dogfood on user's home Linux desktop

### Deferred / reshaping
- Default character pack bundled in system image (still wanted, independent of repositioning)
- Memory distillation concept (still valuable, will be redesigned to run on-phone or via Drone)
- Server-side TTS (fish-tts) — will reappear as Drone Bridge's `bridge-tts` module on GPU Drones

## Voice Pipeline Architecture

On-device voice pipeline in DollOSAIService:

- **Wake Word**: openWakeWord 3-stage ONNX (mel → embedding → classifier). Per-character wake_word.onnx in .doll pack. Training: fish-tts generates positive samples, ACAV100M 2000hr negatives, DNN classifier. Threshold 0.7, 3s debounce, disabled when screen locked.
- **ASR**: sherpa-onnx paraformer (encoder.onnx + decoder.onnx). Always-on streaming, buffer reset on wake word trigger.
- **TTS**: Piper VITS single-speaker model (distilled from fish-tts voice cloning data). 22050Hz, ~real-time on Pixel 6a. No reference audio needed (voice baked in). Model at `/system_ext/dollos/models/voice/tts-vits/`. Requires `model.onnx` + `tokens.txt` + `espeak-ng-data/`.
- **VAD**: silero_vad.onnx for speech segment detection.
- **Speaker ID**: ECAPA-TDNN speaker embedding (512-dim) for speaker verification.

### Wake Word Training
Training data and scripts at `~/Projects/DollOS/wake_word_training/`:
- `train_gura.py` — custom training script (AudioFeatureExtractor → DNN → ONNX export)
- `generate_positive.py` / `generate_negative.py` — fish-tts sample generation
- `verify_voice.py` — speaker embedding cosine similarity verification
- ACAV100M features (17.3GB) for negative training data
- Correct embedding_model.onnx must match the one from openWakeWord Python package

### TTS Model Training
Piper VITS training at `~/Projects/DollOS/wake_word_training/`:
- `generate_tts_dataset.py` / `generate_en_dataset.py` — fish-tts dataset generation
- `filter_accent_whisper.py` — ASR-based accent quality filter
- Training via piper1-gpl (OHF fork) with PyTorch Lightning
- Export: `export_gura.sh` → ONNX + add metadata (sample_rate, n_speakers, language)
- ONNX metadata required by sherpa-onnx: sample_rate, n_speakers, language, noise_scale, noise_scale_w, length_scale
