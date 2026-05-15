#!/usr/bin/env python3
"""Build Powdur's Qwen3-TTS voice_clone_prompt via Voice Design then Clone.

Workflow:
  1) Generate a hype reference clip with VoiceDesign + natural-language instruct.
  2) Save the designed clip as designed_ref.wav (for human preview).
  3) Feed designed clip into Base.create_voice_clone_prompt -> List[VoiceClonePromptItem].
  4) Save items as voice_clone_prompt.pt so Qwen3TTSEngine can reuse them.

Run from worktree root:
    uv run python scripts/build_powdur_voice_prompt.py
"""
from __future__ import annotations

import gc
import wave
from dataclasses import asdict
from pathlib import Path

import numpy as np
import torch

from qwen_tts import Qwen3TTSModel

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "character_packs" / "powdur" / "voice" / "qwen3"
DESIGNED_REF = OUT_DIR / "designed_ref.wav"
PROMPT_PT = OUT_DIR / "voice_clone_prompt.pt"

DEVICE = "cuda:0"
DESIGN_MODEL_ID = "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"
BASE_MODEL_ID = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"

REF_TEXT = "I still need to find myself. Yeah, okay. I need to find myself is some"
INSTRUCT = (
    "young energetic female streamer in her early 20s, hype delivery with "
    "rising intonation, bright clear timbre, wide pitch range from chest voice "
    "to head voice, fast pacing, occasional excited exclamations, like an "
    "entertainer reacting on stream"
)


def write_wav(path: Path, wav: np.ndarray, sr: int) -> None:
    if wav.ndim == 2:
        wav = wav[0] if wav.shape[0] < wav.shape[1] else wav.mean(axis=1)
    if np.issubdtype(wav.dtype, np.floating):
        wav = np.clip(wav, -1.0, 1.0)
        pcm = (wav * 32767.0).astype(np.int16)
    else:
        pcm = wav.astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(pcm.tobytes())


def acoustic(wav: np.ndarray) -> tuple[float, float]:
    if wav.ndim == 2:
        wav = wav.mean(axis=0)
    a = wav.astype(np.float32)
    if np.issubdtype(wav.dtype, np.integer):
        a = a / 32768.0
    return float(np.sqrt(np.mean(a ** 2))), float(np.max(np.abs(a)))


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[1/4] loading VoiceDesign {DESIGN_MODEL_ID} on {DEVICE} ...", flush=True)
    design = Qwen3TTSModel.from_pretrained(DESIGN_MODEL_ID, device_map=DEVICE)

    print("[2/4] generating designed reference clip ...", flush=True)
    wavs, sr = design.generate_voice_design(
        text=REF_TEXT, language="English", instruct=INSTRUCT
    )
    ref_wav = wavs[0]
    write_wav(DESIGNED_REF, ref_wav, sr)
    rms, peak = acoustic(ref_wav)
    duration = (ref_wav.shape[-1] if ref_wav.ndim > 1 else len(ref_wav)) / sr
    print(
        f"  designed_ref.wav -> {DESIGNED_REF}  sr={sr}  "
        f"dur={duration:.2f}s  RMS={rms:.4f}  peak={peak:.4f}",
        flush=True,
    )

    # Free VoiceDesign before loading Base (both 1.7B; tight on 24GB if concurrent).
    del design
    gc.collect()
    torch.cuda.empty_cache()

    print(f"[3/4] loading Base {BASE_MODEL_ID} on {DEVICE} ...", flush=True)
    base = Qwen3TTSModel.from_pretrained(BASE_MODEL_ID, device_map=DEVICE)

    print("[4/4] building voice_clone_prompt from designed ref ...", flush=True)
    # Pass waveform directly so we use the exact in-memory clip (not WAV roundtrip).
    if ref_wav.ndim == 2:
        ref_wav = ref_wav.mean(axis=0)
    items = base.create_voice_clone_prompt(
        ref_audio=(ref_wav, sr), ref_text=REF_TEXT
    )
    payload = {"items": [asdict(it) for it in items]}
    torch.save(payload, PROMPT_PT)
    print(f"  voice_clone_prompt.pt -> {PROMPT_PT}  items={len(items)}", flush=True)

    print("\nDone.")
    print(f"  designed_ref:  {DESIGNED_REF}")
    print(f"  prompt:        {PROMPT_PT}")


if __name__ == "__main__":
    main()
