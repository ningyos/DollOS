# Retired Repos

These repositories are part of DollOS history but **no longer maintained** as of the 2026-05-01 pivot (computer-as-soul, phone-as-app). Their concepts have been absorbed or replaced; do not modify them.

If you need to recover code or data from them, the directories still exist on disk under `~/Projects/`.

| Repo | Path on disk | What it was | Why retired | Where its concepts live now |
|---|---|---|---|---|
| **DollOSAIService** | `~/Projects/DollOSAIService/` | Android AI Service (Kotlin). LLM client, conversation engine, memory (ObjectBox + Room FTS4), personality, agent, character pack manager. Bound via AIDL. | Phone is no longer the brain. | All brain logic moves to `DollOS/daemon/` (Python). |
| **DollOSLauncher** | `~/Projects/DollOSLauncher/` | Android 3D Launcher (Kotlin, Filament). Full-screen 3D avatar scene, conversation bubble, app drawer, character picker. | No more custom launcher; Android app uses Cubism Java SDK in a normal app. | `DollOS-App` (future) — Android app with Cubism Java SDK. |
| **DollOSService** | (in AOSP tree, `~/Projects/DollOS-build/`) | Android system service (system UID). Executed agent actions, hosted TaskManagerActivity. | No more system UID; phone is a regular app via system assistant role. | Tier B/C/D phone adapters in `DollOS-App`. |
| **DollOSSetupWizard** | (in AOSP tree) | Android OOBE — theme picker, GMS toggle, API key setup. | App model has no OOBE; standard Android setup applies. | — |
| **DollOS-Android** | `~/Projects/DollOS-Android/` | AOSP overlay configs. | No custom ROM. | — |
| **DollOS-build** | `~/Projects/DollOS-build/` | Full AOSP build tree (GrapheneOS manifest + local_manifests). | No custom ROM. | — |

## Salvage policy

If a future plan needs code/concepts from these repos, the implementation plan should reference the specific file path (e.g. `~/Projects/DollOSAIService/.../MemorySearch.kt`) and port deliberately to the new daemon. Do not silently fork.

## What survives

These repos are **kept active** (independent of DollOS pivot):

| Repo | Path | Role |
|---|---|---|
| `fish-tts` | `~/Projects/fish-tts/` | TTS engine: DualARTransformer + DAC vocoder |
| `luxtts-onnx` | `~/Projects/luxtts-onnx/` | TTS engine: LuxTTS ONNX |
| `tuna` | `~/Projects/tuna/` | Fine-tuning tools |
| `wake_word_training/` (in this repo) | `~/Projects/DollOS/wake_word_training/` | Wake word + TTS distillation training scripts |

## Pivot reference

See `docs/superpowers/specs/2026-05-01-dollos-pivot-to-computer-design.md` (especially §11) for the full migration / death list and reasoning.
