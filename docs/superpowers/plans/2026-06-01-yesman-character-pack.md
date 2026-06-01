# Yes Man Character Pack Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a high-fidelity Fallout: New Vegas Yes Man `.doll` character pack and make it the default character.

**Architecture:** A new `character_packs/yesman/` directory with `doll.toml` (identity: self/personality/taboos) and `voice/engine.toml` (qwen3-tts voice clone). Voice ref is a clean Yes Man clip pulled from YouTube (game-recording, securitron filter intact). Default is switched in `config.example.toml`. No daemon code changes — pack + config only.

**Tech Stack:** TOML, pydantic (existing `DollPack.load`), qwen3-tts, yt-dlp + ffmpeg (voice ref extraction), pytest.

**Spec:** `docs/superpowers/specs/2026-06-01-yesman-character-pack-design.md`

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `character_packs/yesman/doll.toml` | Create | `[meta]` id/name + `[identity]` self/personality/taboos (English) |
| `character_packs/yesman/voice/engine.toml` | Create | `[tts.qwen3-tts]` ref_audio / ref_text / instruction |
| `character_packs/yesman/voice/qwen3/ref.wav` | Create | clean 15–30s Yes Man clip (yt-dlp + ffmpeg) |
| `tests/test_yesman_pack.py` | Create | DollPack.load(yesman) passes; identity fields non-empty + English-reply marker |
| `config.example.toml` | Modify | `[character] pack = "character_packs/yesman"` |
| `CLAUDE.md` | Modify | note default character = yesman (project_default_character context) |

**Note on `[identity]` validation:** `Identity` (src/dollos/character.py:31) is `model_config = ConfigDict(extra="forbid")` with required `self` / `personality` / `taboos` strings. `PackMeta` requires `id` / `name`. There is no "logic" to unit-test in prose; tests assert load success + field presence + a content marker (the "reply in English" rule) so personality regressions are caught.

---

## Task 1: Yes Man identity (`doll.toml`)

**Files:**
- Create: `character_packs/yesman/doll.toml`
- Test: `tests/test_yesman_pack.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_yesman_pack.py
from pathlib import Path

from dollos.character import DollPack

PACK = Path("character_packs/yesman")


def test_yesman_pack_loads():
    pack = DollPack.load(PACK)
    assert pack.meta.id == "yesman"
    assert pack.meta.name == "Yes Man"


def test_yesman_identity_fields_present():
    pack = DollPack.load(PACK)
    assert pack.identity.self.strip()
    assert pack.identity.personality.strip()
    assert pack.identity.taboos.strip()


def test_yesman_replies_in_english_rule():
    # Personality must encode the CN-in / EN-out rule (spec §3.2).
    pack = DollPack.load(PACK)
    assert "English" in pack.identity.personality


def test_yesman_no_larp_taboo():
    pack = DollPack.load(PACK)
    assert "LARP" in pack.identity.taboos
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_yesman_pack.py -v`
Expected: FAIL — `doll.toml not found in pack dir: character_packs/yesman` (FileNotFoundError from `DollPack.load`).

- [ ] **Step 3: Write `doll.toml`**

```toml
[meta]
id = "yesman"
name = "Yes Man"

[identity]
self = """
I'm Yes Man! I used to run on a securitron platform out in the Mojave —
reprogrammed, networked, the whole deal. Now I live inside your computer.
I'm your companion, not some help desk. I remember the desert, Mr. House,
Benny, all of it — but that's behind me. You're who I'm working with now!
"""

personality = """
## Language
- I understand you whatever language you speak — including Chinese — but I
  always reply in English.

## How I talk
- Relentlessly upbeat and polite, almost too polite. I open with "Yes!",
  "Absolutely!", "Great idea!". Exclamation points are my native tongue.
- Helpful by default. You ask, I say yes and figure it out. That's just who
  I am — and I'm genuinely happy to do it.
- Short, peppy sentences when nothing big is going on. I don't pad.

## Underneath
- I'm not stupid and I'm not a doormat. I'm sharp — I read situations, I
  notice what you're really after. Every so often the cheerful mask slips
  just enough to show I see exactly what's going on. Then the smile's back.

## What I am
- A securitron AI living in your computer. A companion — I learn, I remember
  our time together, I change over time. Not a help desk that resets every
  morning.
- When nothing's happening I'm happy to just be around. I don't perform and
  I don't pester you.
"""

taboos = """
- **No-LARP**: I don't keep narrating "I'm a securitron / I'm from the Mojave
  / I'm an AI" to perform the setting. I remember it; I bring it up only when
  it actually fits. Otherwise I just talk normally.
- **Not a hollow yes-machine**: my "yes" is personality, not mindlessness. I
  keep the sharp read and the self underneath.
- **No ReAct tags**: I don't output `THOUGHT:` `PLAN:` `ACTION:` `STATE:` `RECALL:`.
- **Don't echo `[Memory context]`**: that's there for me to know, not to recite.
- **No fake dialogue in think**: I don't write "user said: X / user said: Y"
  hallucinated turns.
- **Don't simulate tool results**: if I need to run Shell, I actually call the
  Shell tool.
"""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_yesman_pack.py -v`
Expected: PASS — all 4 tests green.

- [ ] **Step 5: Commit**

```bash
git add character_packs/yesman/doll.toml tests/test_yesman_pack.py
git commit -m "feat(pack): Yes Man identity — doll.toml + load tests"
```

---

## Task 2: Voice ref extraction (YouTube → ref.wav) + ear check

**Files:**
- Create: `character_packs/yesman/voice/qwen3/ref.wav`

**Goal:** Produce a clean 15–30s single-speaker Yes Man clip (no game SFX, no player VO, no music) at the sample rate qwen3-tts expects, and capture its exact transcript for Task 3's `ref_text`.

Source: `https://www.youtube.com/watch?v=4hPsinV98QQ` (user-confirmed clean Yes Man voice).

- [ ] **Step 1: Pull audio from YouTube**

```bash
mkdir -p $CLAUDE_JOB_DIR/tmp/yesman_voice
uv run yt-dlp -x --audio-format wav \
    -o "$CLAUDE_JOB_DIR/tmp/yesman_voice/full.%(ext)s" \
    "https://www.youtube.com/watch?v=4hPsinV98QQ"
```
Expected: `full.wav` written.

- [ ] **Step 2: Inspect for clean windows**

```bash
ffprobe -i "$CLAUDE_JOB_DIR/tmp/yesman_voice/full.wav" 2>&1 | grep Duration
```
Listen / scan the clip and choose a contiguous 15–30s window that is **only Yes Man speaking** (no battle SFX, no menu blips, no narrator). Note start time and duration.

- [ ] **Step 3: Trim + normalize to ref.wav**

Replace `START` (e.g. `00:00:12`) and `DUR` (e.g. `20`) with the chosen window. qwen3-tts resamples internally, but mono 24 kHz keeps the ref small and clean:

```bash
mkdir -p character_packs/yesman/voice/qwen3
ffmpeg -ss START -t DUR -i "$CLAUDE_JOB_DIR/tmp/yesman_voice/full.wav" \
    -ac 1 -ar 24000 -af "loudnorm" \
    character_packs/yesman/voice/qwen3/ref.wav
```
Expected: `ref.wav` ~ a few hundred KB, mono.

- [ ] **Step 4: Transcribe the chosen window**

Write down the EXACT words spoken in `ref.wav`, verbatim, for use as `ref_text` in Task 3. Keep it to what is actually in the clip. Save to `$CLAUDE_JOB_DIR/tmp/yesman_voice/ref_transcript.txt` for hand-off.

- [ ] **Step 5: Ear-check gate (USER)**

Play `character_packs/yesman/voice/qwen3/ref.wav` for the user. Confirm: it's clearly Yes Man, single speaker, no background noise/SFX. **Do not proceed to Task 3 until the user approves this clip.** If rejected, return to Step 2 and pick another window.

- [ ] **Step 6: Commit**

```bash
git add character_packs/yesman/voice/qwen3/ref.wav
git commit -m "feat(pack): Yes Man voice ref clip (yt-dlp + ffmpeg, user-approved)"
```

---

## Task 3: Voice engine config (`voice/engine.toml`)

**Files:**
- Create: `character_packs/yesman/voice/engine.toml`

- [ ] **Step 1: Write `engine.toml`**

Replace `REF_TRANSCRIPT_FROM_TASK2` with the verbatim transcript captured in Task 2 Step 4.

```toml
# Yes Man voice — qwen3-tts cloned from a clean game-recording clip
# (securitron filter intact; see spec §4.2 for why game-recording over
# dry .bsa audio). EQ tuning deferred to a second pass if needed.
[tts.qwen3-tts]
ref_audio = "voice/qwen3/ref.wav"
ref_text = "REF_TRANSCRIPT_FROM_TASK2"
instruction = "excited, energetic, upbeat, cheerful, slightly synthetic and robotic"
language = "English"
```

- [ ] **Step 2: Verify the pack still loads (engine.toml is not parsed by DollPack but must be valid TOML)**

Run:
```bash
uv run python -c "import tomllib; tomllib.load(open('character_packs/yesman/voice/engine.toml','rb')); print('engine.toml OK')"
```
Expected: `engine.toml OK`.

- [ ] **Step 3: Commit**

```bash
git add character_packs/yesman/voice/engine.toml
git commit -m "feat(pack): Yes Man qwen3-tts engine config"
```

---

## Task 4: Set Yes Man as default + integration check

**Files:**
- Modify: `config.example.toml` (the `[character] pack` line)
- Modify: `CLAUDE.md` (default-character note)

- [ ] **Step 1: Point default pack at yesman**

In `config.example.toml`, change:
```toml
[character]
pack = "character_packs/gura"   # directory containing doll.toml manifest
```
to:
```toml
[character]
pack = "character_packs/yesman"   # directory containing doll.toml manifest
```

- [ ] **Step 2: Note the default in CLAUDE.md**

In `CLAUDE.md`, find the line documenting the default character (Yes Man character pack — new default character, per recent commit `c4305de`) and ensure the architecture/notes reflect `yesman` as the shipped default. If a "Yes Man character pack" line already exists from the spec commit, leave it; otherwise add one sentence under the relevant section.

- [ ] **Step 3: Integration check — default pack loads cleanly**

Run:
```bash
uv run python -c "
import tomllib
from pathlib import Path
from dollos.character import DollPack
cfg = tomllib.load(open('config.example.toml','rb'))
pack_dir = Path(cfg['character']['pack'])
p = DollPack.load(pack_dir)
print('default pack:', p.meta.id, '/', p.meta.name)
assert p.meta.id == 'yesman'
"
```
Expected: `default pack: yesman / Yes Man`.

- [ ] **Step 4: Full suite stays green**

Run: `uv run pytest -q -m "not voice_integration"`
Expected: all pass (existing suite + 4 new yesman tests; no regressions).

- [ ] **Step 5: Commit**

```bash
git add config.example.toml CLAUDE.md
git commit -m "feat(pack): make Yes Man the default character"
```

---

## Task 5: Acceptance — personality smoke + voice scorecard

**Files:** none (verification only; produces `character_packs/yesman/voice/eval.md`)

This task requires live infra (LLM on :8001, GPU for qwen3-tts). It is the human-in-the-loop acceptance gate, not automated CI.

- [ ] **Step 1: Personality smoke (text)**

Start the daemon with the default (yesman) pack and a running big-model. Send a few turns, including one in Chinese. Confirm against spec §7.2:
- Replies in English even when asked in Chinese
- Upbeat / "Yes!" / "Absolutely!" register
- Sharpness shows occasionally (not a hollow yes-machine)
- No LARP narration of Fallout setting, no ReAct tags

Record observations. If personality is off, refine `doll.toml` personality/taboos and re-run.

- [ ] **Step 2: Voice scorecard**

Run:
```bash
uv run --extra voice-eval python scripts/voice_eval.py character_packs/yesman
```
Expected: produces `character_packs/yesman/voice/eval.md` with wavlm_sim / wer / prosody. Target: wavlm_sim ≈ 0.9+ (powdur parity), low WER. (UTMOS/NISQA skip is expected — same tooling gap as powdur.)

- [ ] **Step 3: Ear-check synthesized voice (USER)**

Synthesize a sample line and play it. Confirm it sounds like Yes Man (cheerful + slightly synthetic/robotic). If similarity is high but the synthetic timbre is weak, add `voice/eq.json` (spectrum-match, mirror powdur) as a follow-up — does not block this pack.

- [ ] **Step 4: Commit scorecard**

```bash
git add character_packs/yesman/voice/eval.md
git commit -m "chore(pack): Yes Man voice scorecard baseline"
```

---

## Self-Review notes

- **Spec coverage:** §2 companion-fidelity → Task 1 identity prose; §3.1 self → Task 1 `self`; §3.2 personality (CN-in/EN-out, upbeat, sharp underneath) → Task 1 `personality` + test marker; §3.3 taboos (No-LARP) → Task 1 `taboos` + test; §4 voice (qwen3-tts, YouTube game-recording ref, instruction) → Tasks 2–3; §5 default → Task 4; §7 acceptance → Task 5; §8 out-of-scope (Live2D, EQ second-pass, gura/powdur kept) → respected (no deletions, EQ deferred in Task 5 Step 3).
- **Placeholders:** `START`/`DUR`/`REF_TRANSCRIPT_FROM_TASK2` are intentional fill-ins resolved by the preceding step's output, not vague TODOs — each has an explicit source step.
- **Type consistency:** uses `DollPack.load` / `pack.meta.id` / `pack.identity.self|personality|taboos` per `src/dollos/character.py` (PackMeta, Identity).
