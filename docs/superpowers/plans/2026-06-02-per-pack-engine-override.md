# Per-Pack TTS Engine Override Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let each character pack declare its own TTS engine (overriding the global `[voice.tts] engine`), so e.g. gura uses qwen3-tts (Chinese-capable) and Yes Man uses luxtts-onnx (low-latency English) simultaneously.

**Architecture:** (1) `VoiceConfig` gains an optional `engine` field parsed from a top-level `engine = "..."` key in the pack's `voice/engine.toml`. (2) The DollOS config's `[voice.tts]` may carry per-engine infra in nested `[voice.tts.<engine>]` blocks (already accepted by the `extra="allow"` schema). (3) `resolve_voice_kwargs` picks `pack.engine or config.engine`, merges pack identity + the matching engine's infra block; flat (non-dict) infra keys stay back-compatible by applying only to the config default engine.

**Tech Stack:** Python, pydantic, tomllib, pytest.

**Spec:** design approved inline 2026-06-02 (this conversation). No separate spec file.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `src/dollos/voice/pack.py` | Modify | `VoiceConfig.engine` field; `load_voice_config` parses top-level `engine`; `resolve_voice_kwargs` does pack-override + per-engine infra split |
| `tests/voice/test_pack.py` | Modify | tests for the new parsing + resolution rules |
| `character_packs/gura/voice/engine.toml` | Modify | `engine = "qwen3-tts"` + `[tts.qwen3-tts]` ref block |
| `character_packs/gura/voice/qwen3/ref.wav` | Create | gura ref clip (from wake-up.mp3, already on disk) |
| `character_packs/yesman/voice/engine.toml` | Modify | `engine = "luxtts-onnx"` + `[tts.luxtts-onnx]` prompt + eq_curve_path |
| `character_packs/yesman/voice/luxtts/prompt.npz` | (exists, untracked) | yesman luxtts prompt — git add |
| `character_packs/yesman/voice/eq.json` | (exists, untracked) | yesman warm eq — git add |
| `config.example.toml` | Modify | per-engine infra blocks `[voice.tts.luxtts-onnx]` / `[voice.tts.qwen3-tts]`; keep `engine` as global default |

**Background facts (verified):**
- Current `resolve_voice_kwargs(voice_cfg, dollos_voice_tts)` (pack.py ~111): `engine_name = dollos_voice_tts["engine"]`; `merged = dict(voice_cfg.tts[engine_name])` then overlays every `dollos_voice_tts` key except `engine`.
- `VoiceConfig` (pack.py ~44): frozen dataclass, currently only `tts: dict[str,dict] | None = None`.
- `VoiceTTSSettings` (config.py ~93): `model_config = ConfigDict(extra="allow")`, `engine: str`. A TOML `[voice.tts.qwen3-tts]` table parses into an extra key `"qwen3-tts"` whose value is a dict — already accepted, no schema change needed.
- `_PATH_KEYS` in pack.py already includes `ref_audio`, `eq_curve_path`, `prompt_path` — these resolve to pack-absolute during `load_voice_config`.
- gura ref: `wake-up.mp3` (30.7s) at repo root → already converted to 24k mono wav during exploration; transcript via whisper is in this plan (Task 3).

---

## Task 1: `VoiceConfig.engine` + parse top-level `engine`

**Files:**
- Modify: `src/dollos/voice/pack.py`
- Test: `tests/voice/test_pack.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/voice/test_pack.py`:

```python
def test_voice_config_engine_defaults_none(tmp_path):
    # engine.toml without a top-level engine key → VoiceConfig.engine is None
    from dollos.voice.pack import load_voice_config
    pack = tmp_path
    vdir = pack / "voice"; vdir.mkdir(parents=True)
    (vdir / "engine.toml").write_text(
        '[tts.luxtts-onnx]\nprompt_path = "voice/luxtts/prompt.npz"\n'
    )
    cfg = load_voice_config(pack)
    assert cfg.engine is None
    assert "luxtts-onnx" in cfg.tts


def test_voice_config_parses_top_level_engine(tmp_path):
    from dollos.voice.pack import load_voice_config
    pack = tmp_path
    vdir = pack / "voice"; vdir.mkdir(parents=True)
    (vdir / "engine.toml").write_text(
        'engine = "qwen3-tts"\n'
        '[tts.qwen3-tts]\nref_audio = "voice/qwen3/ref.wav"\nref_text = "hi"\n'
    )
    cfg = load_voice_config(pack)
    assert cfg.engine == "qwen3-tts"
    assert "qwen3-tts" in cfg.tts
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/voice/test_pack.py -k "engine" -v`
Expected: FAIL (`VoiceConfig` has no `engine`, or parsing ignores top-level key).

- [ ] **Step 3: Implement**

In `pack.py`, add `engine` to `VoiceConfig`:

```python
@dataclass(frozen=True)
class VoiceConfig:
    tts: dict[str, dict] | None = None
    engine: str | None = None
```

In `load_voice_config`, after loading the TOML and building the `tts` dict, read the optional top-level `engine` key (a string sibling of the `[tts.*]` tables) and pass it to `VoiceConfig(tts=..., engine=...)`. The top-level `engine` is NOT a path key and NOT a `[tts.*]` table — read it as `raw.get("engine")` where `raw` is the parsed top-level dict, and ensure the `[tts]` table iteration ignores it.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/voice/test_pack.py -k "engine" -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add src/dollos/voice/pack.py tests/voice/test_pack.py
git commit -m "feat(voice): VoiceConfig.engine — parse pack-declared engine from engine.toml"
```

---

## Task 2: `resolve_voice_kwargs` pack-override + per-engine infra

**Files:**
- Modify: `src/dollos/voice/pack.py`
- Test: `tests/voice/test_pack.py`

- [ ] **Step 1: Write failing tests**

```python
from dollos.voice.pack import VoiceConfig, resolve_voice_kwargs


def test_resolve_uses_config_engine_when_pack_silent():
    # pack declares no engine → use config default; flat infra applies
    vc = VoiceConfig(tts={"luxtts-onnx": {"prompt_path": "p.npz"}}, engine=None)
    name, kw = resolve_voice_kwargs(vc, {"engine": "luxtts-onnx", "device": "cuda"})
    assert name == "luxtts-onnx"
    assert kw["prompt_path"] == "p.npz"
    assert kw["device"] == "cuda"


def test_resolve_pack_engine_overrides_config():
    # pack declares qwen3-tts; config default is luxtts-onnx → pack wins
    vc = VoiceConfig(
        tts={"qwen3-tts": {"ref_audio": "r.wav", "ref_text": "hi"}},
        engine="qwen3-tts",
    )
    cfg_tts = {
        "engine": "luxtts-onnx",
        "luxtts-onnx": {"model_dir": "data/luxtts", "device": "cuda"},
        "qwen3-tts": {"device": "cuda:0"},
    }
    name, kw = resolve_voice_kwargs(vc, cfg_tts)
    assert name == "qwen3-tts"
    assert kw["ref_audio"] == "r.wav"
    assert kw["device"] == "cuda:0"          # qwen3 infra block applied
    assert "model_dir" not in kw             # luxtts infra block NOT leaked


def test_resolve_per_engine_block_for_default_engine():
    vc = VoiceConfig(tts={"luxtts-onnx": {"prompt_path": "p.npz"}}, engine=None)
    cfg_tts = {
        "engine": "luxtts-onnx",
        "luxtts-onnx": {"model_dir": "data/luxtts", "device": "cuda"},
    }
    name, kw = resolve_voice_kwargs(vc, cfg_tts)
    assert name == "luxtts-onnx"
    assert kw["model_dir"] == "data/luxtts"
    assert kw["device"] == "cuda"


def test_resolve_raises_when_pack_lacks_variant():
    vc = VoiceConfig(tts={"luxtts-onnx": {"prompt_path": "p.npz"}}, engine="qwen3-tts")
    import pytest
    with pytest.raises(ValueError, match="qwen3-tts"):
        resolve_voice_kwargs(vc, {"engine": "luxtts-onnx"})
```

- [ ] **Step 2: Run to verify fail**

Run: `uv run pytest tests/voice/test_pack.py -k "resolve" -v`
Expected: FAIL (current impl ignores `voice_cfg.engine` and leaks flat/nested infra indiscriminately).

- [ ] **Step 3: Implement**

Replace the body of `resolve_voice_kwargs` with:

```python
def resolve_voice_kwargs(
    voice_cfg: VoiceConfig, dollos_voice_tts: dict
) -> tuple[str, dict]:
    default_engine = dollos_voice_tts.get("engine")
    engine_name = voice_cfg.engine or default_engine
    if engine_name is None:
        raise ValueError(
            "no TTS engine: pack declares none and [voice.tts] has no 'engine'"
        )
    if voice_cfg.tts is None or engine_name not in voice_cfg.tts:
        raise ValueError(f"character pack lacks [tts.{engine_name}] variant")

    # Split config infra: nested dicts are per-engine blocks; scalars are
    # flat infra that (for back-compat) apply only to the config default engine.
    per_engine = {
        k: v for k, v in dollos_voice_tts.items() if isinstance(v, dict)
    }
    flat_infra = {
        k: v
        for k, v in dollos_voice_tts.items()
        if k != "engine" and not isinstance(v, dict)
    }

    merged = dict(voice_cfg.tts[engine_name])      # pack identity
    if engine_name == default_engine:
        merged.update(flat_infra)                  # back-compat flat infra
    merged.update(per_engine.get(engine_name, {})) # this engine's infra block
    return engine_name, merged
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/voice/test_pack.py -k "resolve" -v`
Expected: PASS (all four).

- [ ] **Step 5: Full suite**

Run: `uv run pytest -q -m "not voice_integration"`
Expected: only the pre-existing `test_sink_fires_tts_on_text_chunk` fails; everything else green.

- [ ] **Step 6: Commit**

```bash
git add src/dollos/voice/pack.py tests/voice/test_pack.py
git commit -m "feat(voice): per-pack engine override + per-engine infra blocks in resolve_voice_kwargs"
```

---

## Task 3: Land gura → qwen3-tts

**Files:**
- Create: `character_packs/gura/voice/qwen3/ref.wav`
- Modify: `character_packs/gura/voice/engine.toml`

**Note:** `wake-up.mp3` (30.7s, repo root) is gura's ref source. whisper transcript (verified): "Wake up, wake up, wake up, yeah you, yeah you, I'm talking to you, that's right, open atom, come on, come on, feet on the floor, let's go, top chop, you're going to be late and then what's going to happen, you're not going to have time for breakfast, you're not going to have time for a snack, you can't pick a snack and then what, you're going to be grumpy, you're going to be hungry and whose fault is that, it's going to be your fault, alright, come on, get up, let's go, it's time to go."

- [ ] **Step 1: Produce ref.wav**

```bash
mkdir -p character_packs/gura/voice/qwen3
ffmpeg -y -i wake-up.mp3 -ac 1 -ar 24000 character_packs/gura/voice/qwen3/ref.wav
```
Verify: `ffprobe character_packs/gura/voice/qwen3/ref.wav 2>&1 | grep Duration` ≈ 30s.

- [ ] **Step 2: Rewrite gura engine.toml**

Replace `character_packs/gura/voice/engine.toml` with (keep luxtts block too so gura still loads under a luxtts default, but declare qwen3 as gura's engine):

```toml
engine = "qwen3-tts"

[tts.qwen3-tts]
ref_audio = "voice/qwen3/ref.wav"
ref_text = "Wake up, wake up, wake up, yeah you, yeah you, I'm talking to you, that's right, open atom, come on, come on, feet on the floor, let's go, top chop, you're going to be late and then what's going to happen, you're not going to have time for breakfast, you're not going to have time for a snack, you can't pick a snack and then what, you're going to be grumpy, you're going to be hungry and whose fault is that, it's going to be your fault, alright, come on, get up, let's go, it's time to go."
language = "Chinese"

[tts.luxtts-onnx]
prompt_path = "voice/luxtts/prompt.npz"
```

- [ ] **Step 3: Verify pack resolves to qwen3 regardless of config default**

```bash
uv run python -c "
from pathlib import Path
from dollos.voice.pack import load_voice_config, resolve_voice_kwargs
vc = load_voice_config(Path('character_packs/gura'))
name, kw = resolve_voice_kwargs(vc, {'engine':'luxtts-onnx','qwen3-tts':{'device':'cuda:0'}})
print('engine:', name); assert name == 'qwen3-tts'
print('OK, ref:', kw['ref_audio'])
"
```
Expected: `engine: qwen3-tts`.

- [ ] **Step 4: Commit**

```bash
git add character_packs/gura/voice/engine.toml character_packs/gura/voice/qwen3/ref.wav
git commit -m "feat(pack/gura): declare qwen3-tts engine + ref (Chinese-capable)"
```

---

## Task 4: Land Yes Man → luxtts-onnx + config per-engine infra

**Files:**
- Modify: `character_packs/yesman/voice/engine.toml`
- Add (untracked → tracked): `character_packs/yesman/voice/luxtts/prompt.npz`, `character_packs/yesman/voice/eq.json`
- Modify: `config.example.toml`

- [ ] **Step 1: Rewrite yesman engine.toml**

Yes Man declares luxtts; keep the qwen3 block as an alternate (so a qwen3 default still works). Replace `character_packs/yesman/voice/engine.toml`:

```toml
engine = "luxtts-onnx"

[tts.luxtts-onnx]
prompt_path = "voice/luxtts/prompt.npz"
eq_curve_path = "voice/eq.json"

[tts.qwen3-tts]
ref_audio = "voice/qwen3/ref.wav"
ref_text = "Hey, hi there! Good to meet you. What can I do for you today? Oh, hi again. Can I help you with something else? Hi, nice to see you again."
instruction = "excited, energetic, upbeat, cheerful, slightly synthetic and robotic"
language = "English"
```

- [ ] **Step 2: config per-engine infra blocks**

In `config.example.toml`, change the `[voice.tts]` section so the global default is luxtts and both engines have infra blocks. Replace the existing `[voice.tts]` block with:

```toml
[voice.tts]
engine = "luxtts-onnx"   # global default; packs may override via engine.toml

[voice.tts.luxtts-onnx]
model_dir = "data/voice/tts/luxtts"
device = "cuda"

[voice.tts.qwen3-tts]
model_id = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
device = "cuda:0"
```

Mirror the same change in the local `config.toml` (gitignored, for the smoke).

- [ ] **Step 3: Track yesman luxtts prompt + eq**

```bash
git add character_packs/yesman/voice/luxtts/prompt.npz character_packs/yesman/voice/eq.json character_packs/yesman/voice/engine.toml config.example.toml
```

- [ ] **Step 4: Integration check — both packs resolve to their own engine under one config**

```bash
uv run python -c "
import tomllib
from pathlib import Path
from dollos.voice.pack import load_voice_config, resolve_voice_kwargs
cfg_tts = tomllib.load(open('config.example.toml','rb'))['voice']['tts']
for pack in ('yesman','luxtts? no','gura'):
    pass
for pack, want in [('yesman','luxtts-onnx'),('gura','qwen3-tts')]:
    vc = load_voice_config(Path('character_packs')/pack)
    name, kw = resolve_voice_kwargs(vc, cfg_tts)
    print(pack, '->', name); assert name == want, (pack, name)
print('OK: per-pack engine override working')
"
```
Expected: `yesman -> luxtts-onnx`, `gura -> qwen3-tts`, `OK`.

- [ ] **Step 5: Full suite**

Run: `uv run pytest -q -m "not voice_integration"`
Expected: only pre-existing `test_sink` fails.

- [ ] **Step 6: Commit**

```bash
git commit -m "feat(pack): Yes Man luxtts + per-engine config infra; both packs self-select engine"
```

---

## Task 5: Acceptance — both engines synth under one config

**Files:** none (verification only)

- [ ] **Step 1: Synthesize each pack through the daemon's engine-build path**

Write a throwaway script that, for each pack, calls `build_engines`-equivalent (`load_voice_config` + `resolve_voice_kwargs` with the config's `[voice.tts]`) then constructs `TTS_REGISTRY[name](**kwargs)` and synthesizes one line. Confirm:
- yesman → luxtts engine object, RTF < 0.5 (GPU via cudnn preload), audio non-empty
- gura → qwen3 engine object, Chinese line synthesizes, audio non-empty

- [ ] **Step 2: Report results to the user for ear-check**

Send both wavs to the user (luxtts Yes Man + qwen3 gura) confirming each pack drives its declared engine end-to-end. This is the human acceptance gate.

---

## Self-Review notes

- **Spec coverage:** (1) pack-declared engine → Task 1 (`VoiceConfig.engine` + parse). (2) per-engine config infra blocks → Task 2 (resolve split) + Task 4 (config). (3) resolve precedence pack>config → Task 2. (4) back-compat flat infra → Task 2 `test_resolve_per_engine_block_for_default_engine` + `test_resolve_uses_config_engine_when_pack_silent`. (5) land gura=qwen3 → Task 3. (6) land yesman=luxtts → Task 4. (7) both-under-one-config → Task 4 Step 4 + Task 5.
- **Placeholders:** none — all infra blocks, transcripts, and resolve code are concrete.
- **Type consistency:** `VoiceConfig(tts=..., engine=...)`, `resolve_voice_kwargs(voice_cfg, dollos_voice_tts) -> (name, merged)` consistent across Tasks 1–4. `engine_name`/`default_engine` naming stable.
- **Note on eq.json sample_rate:** yesman eq.json is `sample_rate: 48000` (luxtts native) — correct, luxtts engine checks this. gura's earlier luxtts eq.json is now irrelevant (gura uses qwen3); leave it untracked / delete in Task 3 if desired (not required — qwen3 ignores it).
