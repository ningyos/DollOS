#!/usr/bin/env python3
"""Download a YouTube clip, auto-pick a clean window, encode via fish-tts -> .npy.

Hands-free: yt-dlp pulls audio + auto-generated captions with per-word
timestamps; the script picks a contiguous 6-12s window; ffmpeg trims to
that; fish-tts encode_reference produces the voice profile. The matching
transcript is also written next to the .npy, and the pack's engine.toml
transcript field is auto-patched.

Required:
  - ffmpeg in PATH (`sudo apt install ffmpeg`)
  - yt-dlp (dev dep)
  - fish-tts (optional extra: `uv sync --extra fish`)

Usage:
  uv run python scripts/encode_voice_from_youtube.py \\
      --url https://www.youtube.com/watch?v=... \\
      --out character_packs/powdur/voice/fish/powdur_voice.npy
"""
from __future__ import annotations

import argparse
import html
import logging
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# VTT parsing - per-word timed
# ---------------------------------------------------------------------------

@dataclass
class Word:
    start: float
    text: str


def _ts_to_sec(ts: str) -> float:
    parts = ts.strip().split(":")
    if len(parts) == 2:
        h, m, s = "0", parts[0], parts[1]
    else:
        h, m, s = parts
    return int(h) * 3600 + int(m) * 60 + float(s)


def _clean(s: str) -> str:
    s = html.unescape(s)
    s = s.replace(" ", " ")  # nbsp
    s = re.sub(r"\[\s*_+\s*\]", "", s)
    return re.sub(r"\s+", " ", s).strip()


_BLOCK_RE = re.compile(
    r"(\d{1,2}:\d{2}:\d{2}\.\d{3}|\d{1,2}:\d{2}\.\d{3})\s+-->\s+"
    r"(\d{1,2}:\d{2}:\d{2}\.\d{3}|\d{1,2}:\d{2}\.\d{3})"
    r"[^\n]*\n((?:.+\n?)*?)(?:\n\n|\Z)",
    re.MULTILINE,
)
_INLINE_RE = re.compile(
    r"<(\d{1,2}:\d{2}:\d{2}\.\d{3}|\d{1,2}:\d{2}\.\d{3})>"
    r"<c>([^<]*)</c>"
)


def parse_vtt_words(path: Path) -> list[Word]:
    """Extract per-word timed stream from YouTube auto-caption VTT.

    YouTube cue body layout (rolling captions):
      [previous cue text on one or more lines]
      [FIRST NEW WORD]<00:00:00.200><c> word</c><00:00:00.440><c> word</c>...

    The previous-cue lines are duplicates we MUST skip. The first new word
    is the run of plain text on the SAME line as the first inline tag,
    BEFORE the tag itself. Inline-tag words are unambiguous.
    """
    text = path.read_text(encoding="utf-8")
    words: list[Word] = []

    for m in _BLOCK_RE.finditer(text):
        cue_start = _ts_to_sec(m.group(1))
        body = m.group(3)

        first_inline = body.find("<00:")
        if first_inline == -1:
            # No inline timestamps in this cue → it's the "carry-over" display
            # of the previous cue. Skip entirely.
            continue

        # First new word(s) sit on the same line as the first inline tag,
        # before it. Walk backwards to the previous newline.
        line_start = body.rfind("\n", 0, first_inline) + 1
        first_text = body[line_start:first_inline]
        first_clean = _clean(re.sub(r"<[^>]+>", "", first_text))
        for w in first_clean.split():
            words.append(Word(cue_start, w))

        for ts_str, w in _INLINE_RE.findall(body):
            w_clean = _clean(w)
            if w_clean:
                words.append(Word(_ts_to_sec(ts_str), w_clean))

    words.sort(key=lambda x: x.start)
    return words


def pick_window(
    words: list[Word],
    *,
    min_duration: float,
    max_duration: float,
    start_after: float,
) -> tuple[float, float, str]:
    """Greedy: start at first word past start_after, extend until next word
    would exceed max_duration.
    """
    if not words:
        raise RuntimeError("no caption words parsed")

    i = 0
    while i < len(words) and words[i].start < start_after:
        i += 1
    if i >= len(words):
        raise RuntimeError(f"no words after start_after={start_after}s")

    start_time = words[i].start
    picked = [words[i]]
    j = i + 1
    while j < len(words):
        if words[j].start - start_time > max_duration:
            break
        picked.append(words[j])
        j += 1

    duration = (picked[-1].start - start_time) + 0.5  # pad 0.5s tail
    if duration < min_duration:
        duration = min_duration
    if duration > max_duration:
        duration = max_duration

    transcript = " ".join(w.text for w in picked)
    return start_time, duration, transcript


# ---------------------------------------------------------------------------

def require_binary(name: str, install_hint: str) -> None:
    if shutil.which(name) is None:
        logger.error("%s not found in PATH. %s", name, install_hint)
        sys.exit(1)


def download_audio_and_subs(url: str, dest_stem: Path, lang: str) -> tuple[Path, Path]:
    import yt_dlp

    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(dest_stem) + ".%(ext)s",
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitlesformat": "vtt",
        "subtitleslangs": [lang, "en", "en-orig"],
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "wav", "preferredquality": "0"},
        ],
        "postprocessor_args": ["-ar", "44100", "-ac", "1"],
        "quiet": False,
    }
    logger.info("downloading audio + auto-captions from %s ...", url)
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

    wav = dest_stem.with_suffix(".wav")
    if not wav.exists():
        raise RuntimeError(f"audio extraction failed; expected {wav}")

    # Prefer the asked lang's vtt; fall back to any vtt that has inline timestamps.
    candidates = sorted(dest_stem.parent.glob(f"{dest_stem.name}.*.vtt"))
    if not candidates:
        raise RuntimeError("no captions found")
    # Reject *.en.vtt that has zero inline timestamps if a *.en-orig.vtt exists
    best = candidates[0]
    for c in candidates:
        text = c.read_text(encoding="utf-8", errors="replace")
        if "<c>" in text:
            best = c
            break
    return wav, best


def trim_audio(src: Path, dst: Path, *, start: float, duration: float) -> None:
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start:.3f}",
        "-i", str(src),
        "-t", f"{duration:.3f}",
        "-ar", "44100", "-ac", "1",
        "-c:a", "pcm_s16le",
        str(dst),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def encode_reference(wav_path: Path, transcript: str, out_npy: Path) -> None:
    from fish_tts import get_instance

    logger.info("loading fish-tts (first call triggers torch.compile ~4 min) ...")
    synth = get_instance()
    audio_bytes = wav_path.read_bytes()
    logger.info("encoding reference (%d chars transcript)", len(transcript))
    profile = synth.encode_reference(audio_bytes, transcript)
    out_npy.parent.mkdir(parents=True, exist_ok=True)
    profile.save(out_npy)
    out_npy.with_suffix(".transcript.txt").write_text(transcript, encoding="utf-8")
    logger.info("wrote %s  (codes shape=%s)", out_npy, profile.codes.shape)


def patch_engine_toml(out_npy: Path, transcript: str) -> None:
    pack_voice_toml = out_npy.parent.parent / "engine.toml"
    if not pack_voice_toml.exists():
        return
    original = pack_voice_toml.read_text(encoding="utf-8")
    escaped = transcript.replace("\\", "\\\\").replace('"', '\\"')
    patched, n = re.subn(
        r'^(\s*transcript\s*=\s*)"[^"]*"',
        lambda m: f'{m.group(1)}"{escaped}"',
        original, count=1, flags=re.MULTILINE,
    )
    if n == 1:
        pack_voice_toml.write_text(patched, encoding="utf-8")
        logger.info("patched transcript in %s", pack_voice_toml)
    else:
        logger.warning("no transcript line to patch in %s; update manually", pack_voice_toml)


# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", required=True)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--lang", default="en")
    p.add_argument("--min-duration", type=float, default=6.0)
    p.add_argument("--max-duration", type=float, default=12.0)
    p.add_argument("--start-after", type=float, default=None,
                   help="Skip first N s (default: 15 for normal, 0 for /shorts/)")
    p.add_argument("--keep-temp", action="store_true")
    args = p.parse_args()

    require_binary("ffmpeg", "Install via `sudo apt install ffmpeg`")

    is_short = "/shorts/" in args.url
    start_after = args.start_after if args.start_after is not None else (0.0 if is_short else 15.0)
    if is_short:
        logger.info("YouTube Short detected → start_after=%.1fs", start_after)

    with tempfile.TemporaryDirectory(prefix="fish-tts-encode-") as tmp_str:
        tmp = Path(tmp_str)
        stem = tmp / "src"

        wav_full, vtt = download_audio_and_subs(args.url, stem, args.lang)
        words = parse_vtt_words(vtt)
        logger.info("parsed %d caption words from %s", len(words), vtt.name)
        if len(words) < 5:
            raise RuntimeError(
                f"only {len(words)} caption words found — video may not have "
                "per-word timed auto-captions; try a different video"
            )

        start, duration, transcript = pick_window(
            words,
            min_duration=args.min_duration,
            max_duration=args.max_duration,
            start_after=start_after,
        )
        logger.info("window: t=%.2fs duration=%.2fs (%d words)",
                    start, duration, len(transcript.split()))
        logger.info("transcript: %r", transcript)

        wav_trim = tmp / "trimmed.wav"
        trim_audio(wav_full, wav_trim, start=start, duration=duration)

        encode_reference(wav_trim, transcript, args.out)

        if args.keep_temp:
            kept = args.out.with_suffix(".ref.wav")
            shutil.copy(wav_trim, kept)
            logger.info("kept ref wav: %s", kept)

    patch_engine_toml(args.out, transcript)
    logger.info("done.")


if __name__ == "__main__":
    main()
