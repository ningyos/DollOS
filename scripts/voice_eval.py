"""voice_eval — synthesize the test corpus through a character pack's TTS engine
and produce an objective scorecard (WavLM sim / UTMOSv2 / WER / NISQA / prosody).

Usage:
    uv run --extra voice-eval python scripts/voice_eval.py character_packs/<pack>
    uv run --extra voice-eval python scripts/voice_eval.py character_packs/<pack> -o /tmp/report.md
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from dollos.voice_eval.synth_driver import (  # noqa: E402
    TEST_CORPUS, discover_engine_kwargs, synthesize_corpus,
)
from dollos.voice_eval.aggregator import (  # noqa: E402
    ScoreEntry, aggregate, render_report,
)


async def main_async(pack_dir: Path, output: Path | None) -> int:
    print(f"[voice_eval] pack = {pack_dir}", flush=True)
    engine_name, engine_kwargs, ref_audio, ref_text = discover_engine_kwargs(pack_dir)
    print(f"[voice_eval] engine = {engine_name}, ref = {ref_audio.name}", flush=True)

    with tempfile.TemporaryDirectory(prefix="voice_eval_") as tmp:
        tmp_dir = Path(tmp)
        print(f"[voice_eval] synthesizing {len(TEST_CORPUS)} sentences ...", flush=True)
        wavs = await synthesize_corpus(engine_name, engine_kwargs, tmp_dir)

        entries: list[ScoreEntry] = []
        skipped: dict[str, str] = {}

        # WavLM speaker sim
        try:
            from dollos.voice_eval.wavlm_sim import WavLMSimRunner
            runner = WavLMSimRunner()
            print("[voice_eval] WavLM speaker sim ...", flush=True)
            for i, syn in enumerate(wavs):
                score = runner.score(ref_audio, syn)
                entries.append(ScoreEntry(i, TEST_CORPUS[i], "wavlm_sim", score))
                print(f"  [{i}] wavlm_sim = {score:.3f}", flush=True)
        except Exception as e:
            skipped["wavlm_sim"] = str(e)[:200]
            print(f"[voice_eval] wavlm_sim SKIPPED: {e}", flush=True)

        # UTMOSv2
        try:
            from dollos.voice_eval.utmos import UTMOSRunner
            runner = UTMOSRunner()
            print("[voice_eval] UTMOSv2 ...", flush=True)
            for i, syn in enumerate(wavs):
                score = runner.score(syn)
                entries.append(ScoreEntry(i, TEST_CORPUS[i], "utmos", score))
                print(f"  [{i}] utmos = {score:.3f}", flush=True)
        except Exception as e:
            skipped["utmos"] = str(e)[:200]
            print(f"[voice_eval] utmos SKIPPED: {e}", flush=True)

        # WER
        try:
            from dollos.voice_eval.wer import WERRunner
            runner = WERRunner()
            print("[voice_eval] WER (faster-whisper) ...", flush=True)
            for i, syn in enumerate(wavs):
                score = runner.score(syn, TEST_CORPUS[i])
                entries.append(ScoreEntry(i, TEST_CORPUS[i], "wer", score))
                print(f"  [{i}] wer = {score:.3f}", flush=True)
        except Exception as e:
            skipped["wer"] = str(e)[:200]
            print(f"[voice_eval] wer SKIPPED: {e}", flush=True)

        # NISQA (best-effort)
        try:
            from dollos.voice_eval.nisqa import NISQARunner
            runner = NISQARunner()
            print("[voice_eval] NISQA ...", flush=True)
            for i, syn in enumerate(wavs):
                score = runner.score(syn)
                entries.append(ScoreEntry(i, TEST_CORPUS[i], "nisqa", score))
        except Exception as e:
            skipped["nisqa"] = str(e)[:200]
            print(f"[voice_eval] nisqa SKIPPED: {e}", flush=True)

        # Prosody (best-effort)
        try:
            from dollos.voice_eval.prosody import ProsodyRunner
            runner = ProsodyRunner()
            print("[voice_eval] Prosody RMSE ...", flush=True)
            for i, syn in enumerate(wavs):
                score = runner.score(ref_audio, syn)
                entries.append(ScoreEntry(i, TEST_CORPUS[i], "prosody_distance", score))
                print(f"  [{i}] prosody_distance = {score:.3f}", flush=True)
        except Exception as e:
            skipped["prosody_distance"] = str(e)[:200]
            print(f"[voice_eval] prosody SKIPPED: {e}", flush=True)

        summary = aggregate(entries)
        try:
            ref_display = str(ref_audio.relative_to(pack_dir))
        except ValueError:
            ref_display = str(ref_audio)
        md = render_report(
            pack_path=str(pack_dir),
            engine=engine_name,
            ref_audio=ref_display,
            n_sentences=len(TEST_CORPUS),
            summary=summary,
            per_sentence=entries,
            skipped=skipped,
        )
        print("\n" + md)

        if output is not None:
            output.write_text(md, encoding="utf-8")
            print(f"[voice_eval] report written to {output}", flush=True)

    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("pack", type=Path, help="character pack directory")
    p.add_argument("-o", "--output", type=Path, default=None,
                   help="write markdown report to this path (default: stdout only)")
    args = p.parse_args()
    return asyncio.run(main_async(args.pack, args.output))


if __name__ == "__main__":
    sys.exit(main())
