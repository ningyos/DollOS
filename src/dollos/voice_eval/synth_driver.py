"""Shared synth driver — corpus + pack discovery + corpus synthesis.

Both ``scripts/tune_voice_eq.py`` (EQ derivation) and the ``voice_eval`` CLI
(scorecard) drive synthesis the same way: load the pack's ``voice/engine.toml``,
pick the first registered engine, locate the matching reference clip (the
voice we're trying to clone), then synthesize a fixed English test corpus.

The functions here strip ``eq_curve_path`` from the engine kwargs so callers
always get raw, un-EQ'd engine output — both EQ tuning and scorecard eval
want to measure the engine itself, not the corrective post-filter.
"""
from __future__ import annotations

import wave
from pathlib import Path


TEST_CORPUS: list[str] = [
    "Okay so let me tell you what happened today.",
    "Hey, look at my hat. Isn't it beautiful?",
    "Wait, why would you even do that? That makes zero sense.",
    "Dude this is actually crazy, like genuinely insane.",
    "I had the weirdest dream last night, you would not believe it.",
    "Stop, stop, stop. We are not doing that today.",
    "I genuinely cannot believe people are still arguing about this on Twitter.",
    "Hold on, that sentence was about to be weird.",
    "Honestly, I think I might just go to bed early tonight.",
    "Oh my god, did you see what they posted? I cannot.",
]


def _find_fish_ref(npy_path: Path) -> tuple[Path, str]:
    """Locate ref wav + transcript next to a fish-tts ``.npy`` voice profile.

    Heuristic: pack convention puts wav + matching ``.txt`` transcript under
    ``<pack>/voice/transcripts/<id>.{wav,txt}``. The ``.npy`` filename stem
    often carries a ``powdur_voice_`` (or similar) prefix on the youtube ID;
    we try the trailing token first, then the full stem.
    """
    npy_path = Path(npy_path)
    stem = npy_path.stem
    if "_" in stem:
        candidate_id = stem.rsplit("_", 1)[-1]
    else:
        candidate_id = stem
    # voice/fish/x.npy → pack/voice/
    voice_dir = npy_path.parent.parent
    transcripts_dir = voice_dir / "transcripts"
    wav: Path | None = None
    for cand in (
        transcripts_dir / f"{candidate_id}.wav",
        transcripts_dir / f"{stem}.wav",
    ):
        if cand.exists():
            wav = cand
            break
    if wav is None:
        raise FileNotFoundError(
            f"could not locate reference wav next to {npy_path}; "
            f"looked in {transcripts_dir} for {candidate_id}.wav / {stem}.wav"
        )
    txt = wav.with_suffix(".txt")
    ref_text = txt.read_text(encoding="utf-8").strip() if txt.exists() else ""
    return wav, ref_text


def discover_engine_kwargs(
    pack_path: Path,
) -> tuple[str, dict, Path, str]:
    """Pick the first registered TTS engine in the pack; return ref clip + kwargs.

    Returns ``(engine_name, engine_kwargs, ref_audio_path, ref_text)``.

    ``engine_kwargs`` has ``eq_curve_path`` stripped — callers evaluate the
    raw engine output, not the EQ-corrected output.

    Raises ``ValueError`` if the engine has no reference clip to evaluate
    against (e.g. piper — it's a pre-trained voice, not a cloner).
    """
    from dollos.voice.pack import load_voice_config

    pack_path = Path(pack_path)
    cfg = load_voice_config(pack_path)
    if not cfg.tts:
        raise ValueError(
            f"{pack_path} has no [tts.*] section in voice/engine.toml"
        )
    engine_name, raw_kwargs = next(iter(cfg.tts.items()))
    kwargs = {k: v for k, v in raw_kwargs.items() if k != "eq_curve_path"}

    if engine_name == "qwen3-tts":
        ref_audio = kwargs.get("ref_audio")
        ref_text = kwargs.get("ref_text")
        if ref_audio is None or ref_text is None:
            raise ValueError(
                "engine qwen3-tts: pack must define ref_audio + ref_text "
                "in [tts.qwen3-tts] to evaluate against"
            )
        return engine_name, kwargs, Path(ref_audio), str(ref_text)

    if engine_name == "fish-tts":
        paths = kwargs.get("voice_profile_paths") or (
            [kwargs["voice_profile_path"]]
            if kwargs.get("voice_profile_path") is not None
            else None
        )
        if not paths:
            raise ValueError(
                "engine fish-tts: pack must define voice_profile_paths "
                "(or voice_profile_path) to evaluate against"
            )
        ref_wav, ref_text = _find_fish_ref(Path(paths[0]))
        return engine_name, kwargs, ref_wav, ref_text

    raise ValueError(
        f"engine {engine_name!r} has no reference audio to evaluate against"
    )


async def synthesize_corpus(
    engine_name: str,
    engine_kwargs: dict,
    out_dir: Path,
) -> list[Path]:
    """Synthesize ``TEST_CORPUS`` through the given engine; return WAV paths.

    Side-effect: imports ``tts_qwen3`` and ``tts_fish`` to register them in
    ``TTS_REGISTRY``.

    Output files are mono 16-bit WAV at the engine's native sample rate,
    named ``sentence_00.wav`` … ``sentence_09.wav``.
    """
    from dollos.voice import tts_fish, tts_qwen3  # noqa: F401  (register engines)
    from dollos.voice.engines import TTS_REGISTRY

    if engine_name not in TTS_REGISTRY:
        raise ValueError(f"engine {engine_name!r} not registered in TTS_REGISTRY")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    engine = TTS_REGISTRY[engine_name](**engine_kwargs)
    try:
        wavs: list[Path] = []
        for i, text in enumerate(TEST_CORPUS):
            pcm = bytearray()
            async for chunk in engine.synthesize(text):
                pcm.extend(chunk)
            p = out_dir / f"sentence_{i:02d}.wav"
            with wave.open(str(p), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(engine.sample_rate)
                w.writeframes(bytes(pcm))
            wavs.append(p)
        return wavs
    finally:
        try:
            await engine.aclose()
        except Exception:
            pass
