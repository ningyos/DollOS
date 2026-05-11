"""voice prepare CLI: encode a luxtts voice-clone prompt into a character pack.

Usage:
    python -m dollos.voice.prepare \\
        --pack character_packs/gura \\
        --ref reference_recording.wav \\
        --transcript "Reference transcript." \\
        --duration 15.0
"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from luxtts_onnx import LuxTTSOnnx


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dollos.voice.prepare",
        description="Encode a luxtts voice-clone prompt for a character pack",
    )
    p.add_argument("--pack", type=Path, required=True,
                   help="character pack directory")
    p.add_argument("--ref", type=Path, required=True,
                   help="reference audio wav file")
    p.add_argument("--transcript", type=str, required=True,
                   help="exact transcript of the reference audio")
    p.add_argument("--duration", type=float, default=15.0,
                   help="reference duration in seconds (default 15)")
    p.add_argument("--data-root", type=Path, default=Path("data"),
                   help="daemon data root (where models cache lives; default ./data)")
    return p


def run_prepare(
    *,
    pack: Path,
    ref: Path,
    transcript: str,
    duration: float,
    data_root: Path,
) -> Path:
    """Encode prompt + write into the pack. Returns the prompt.npz path."""
    if not pack.exists():
        raise FileNotFoundError(f"pack dir not found: {pack}")
    if not ref.exists():
        raise FileNotFoundError(f"reference audio not found: {ref}")

    model_dir = data_root / "voice" / "tts" / "luxtts"
    model_dir.mkdir(parents=True, exist_ok=True)

    tts = LuxTTSOnnx(model_dir=str(model_dir), provider="cpu")
    prompt = tts.encode_prompt(
        audio_path=str(ref), transcript=transcript, duration=duration,
    )
    out_dir = pack / "voice" / "luxtts"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "prompt.npz"
    tts.save_prompt(prompt, str(out_path))

    # Record provenance.
    meta = (
        f'# voice-clone prompt provenance\n'
        f'ref_path = "{ref}"\n'
        f'transcript = """{transcript}"""\n'
        f'duration_s = {duration}\n'
        f'encoded_at = "{datetime.now().isoformat(timespec="seconds")}"\n'
    )
    (out_dir / "ref.meta.toml").write_text(meta)
    return out_path


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    out = run_prepare(
        pack=args.pack,
        ref=args.ref,
        transcript=args.transcript,
        duration=args.duration,
        data_root=args.data_root,
    )
    print(f"wrote prompt to: {out}")


if __name__ == "__main__":
    main()
