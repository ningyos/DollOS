"""Voice-similarity metrics for TTS quality evaluation.

Compare TTS-generated samples against a reference speaker wav. Computes
timbre (ECAPA-TDNN speaker embedding cosine sim), prosody (F0 stats + DTW),
spectral and energy metrics. One-off eval tooling.

Run:
    uv sync --extra eval
    uv run python scripts/voice_similarity.py --ref REF.wav --samples A.wav B.wav ...
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import traceback
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf
import torch
from speechbrain.inference.classifiers import EncoderClassifier


METRIC_COLS = [
    "speaker_sim",
    "f0_median_hz_diff",
    "f0_iqr_hz_diff",
    "f0_contour_dtw",
    "speech_rate_diff",
    "spectral_centroid_hz_diff",
    "mel_cosine",
    "rms_diff",
    "dynamic_range_db",
]

SR = 16000  # everything resampled to 16k mono for stable comparison
HOP = 512
N_MELS = 80


def load_audio(path: str, sr: int = SR) -> np.ndarray:
    y, file_sr = sf.read(path, dtype="float32", always_2d=False)
    if y.ndim > 1:
        y = y.mean(axis=1)
    if file_sr != sr:
        y = librosa.resample(y, orig_sr=file_sr, target_sr=sr)
    return y.astype(np.float32)


def speaker_embedding(model: EncoderClassifier, y: np.ndarray) -> np.ndarray:
    wav = torch.from_numpy(y).unsqueeze(0)
    with torch.no_grad():
        emb = model.encode_batch(wav).squeeze().cpu().numpy()
    return emb / (np.linalg.norm(emb) + 1e-12)


def f0_stats(y: np.ndarray) -> tuple[np.ndarray, float, float, float]:
    """Returns (voiced_f0_zscore_contour, median_hz, iqr_hz, voiced_rate_fps)."""
    f0, voiced_flag, _ = librosa.pyin(
        y, fmin=80, fmax=600, sr=SR, hop_length=HOP, fill_na=np.nan
    )
    voiced = f0[~np.isnan(f0)]
    if voiced.size < 4:
        raise ValueError(f"too few voiced frames ({voiced.size})")
    median = float(np.median(voiced))
    q1, q3 = np.percentile(voiced, [25, 75])
    iqr = float(q3 - q1)
    fps = SR / HOP
    voiced_rate = voiced.size / f0.size * fps  # voiced frames per second
    # z-score normalize voiced-only contour for DTW
    mu, sd = voiced.mean(), voiced.std() + 1e-9
    contour = (voiced - mu) / sd
    return contour.astype(np.float32), median, iqr, float(voiced_rate)


def dtw_distance(a: np.ndarray, b: np.ndarray) -> float:
    # librosa.sequence.dtw expects shape (features, frames) — here features=1
    D, _ = librosa.sequence.dtw(X=a[np.newaxis, :], Y=b[np.newaxis, :], metric="euclidean")
    # normalize by path length proxy (max of the two lengths)
    return float(D[-1, -1] / max(a.size, b.size))


def mean_log_mel(y: np.ndarray) -> np.ndarray:
    S = librosa.feature.melspectrogram(y=y, sr=SR, n_mels=N_MELS, hop_length=HOP)
    log_S = librosa.power_to_db(S + 1e-10)
    return log_S.mean(axis=1)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12))


def spectral_centroid_mean(y: np.ndarray) -> float:
    sc = librosa.feature.spectral_centroid(y=y, sr=SR, hop_length=HOP)
    return float(sc.mean())


def rms_mean(y: np.ndarray) -> float:
    return float(librosa.feature.rms(y=y, hop_length=HOP).mean())


def dynamic_range_db(y: np.ndarray) -> float:
    peak = float(np.max(np.abs(y)) + 1e-12)
    rms = float(np.sqrt(np.mean(y**2)) + 1e-12)
    return 20.0 * np.log10(peak / rms)


def compute_ref_features(model: EncoderClassifier, ref_path: str) -> dict:
    y = load_audio(ref_path)
    return {
        "y": y,
        "emb": speaker_embedding(model, y),
        "f0_contour": (c := f0_stats(y))[0],
        "f0_median": c[1] if False else f0_stats(y)[1],  # noqa: redundant clarity
    }


def metrics_for_sample(model: EncoderClassifier, ref: dict, sample_path: str) -> dict:
    """Compute all metrics. Each metric failure is caught individually and
    surfaced as 'err: <reason>' string in the result dict."""
    out: dict = {k: None for k in METRIC_COLS}

    try:
        y = load_audio(sample_path)
    except Exception as e:
        for k in METRIC_COLS:
            out[k] = f"err: load: {e}"
        return out

    def safe(key: str, fn):
        try:
            out[key] = fn()
        except Exception as e:
            out[key] = f"err: {type(e).__name__}: {e}"

    safe("speaker_sim", lambda: cosine(ref["emb"], speaker_embedding(model, y)))

    # F0 — compute once for sample, reuse
    try:
        s_contour, s_median, s_iqr, s_rate = f0_stats(y)
        out["f0_median_hz_diff"] = abs(s_median - ref["f0_median"])
        out["f0_iqr_hz_diff"] = abs(s_iqr - ref["f0_iqr"])
        out["speech_rate_diff"] = abs(s_rate - ref["voiced_rate"])
        try:
            out["f0_contour_dtw"] = dtw_distance(ref["f0_contour"], s_contour)
        except Exception as e:
            out["f0_contour_dtw"] = f"err: dtw: {e}"
    except Exception as e:
        msg = f"err: f0: {e}"
        out["f0_median_hz_diff"] = msg
        out["f0_iqr_hz_diff"] = msg
        out["f0_contour_dtw"] = msg
        out["speech_rate_diff"] = msg

    safe("spectral_centroid_hz_diff",
         lambda: abs(spectral_centroid_mean(y) - ref["spec_centroid"]))
    safe("mel_cosine", lambda: cosine(ref["mel"], mean_log_mel(y)))
    safe("rms_diff", lambda: abs(rms_mean(y) - ref["rms"]))
    safe("dynamic_range_db", lambda: dynamic_range_db(y))
    return out


def precompute_ref(model: EncoderClassifier, ref_path: str) -> dict:
    y = load_audio(ref_path)
    contour, median, iqr, rate = f0_stats(y)
    return {
        "y": y,
        "emb": speaker_embedding(model, y),
        "f0_contour": contour,
        "f0_median": median,
        "f0_iqr": iqr,
        "voiced_rate": rate,
        "spec_centroid": spectral_centroid_mean(y),
        "mel": mean_log_mel(y),
        "rms": rms_mean(y),
    }


def fmt_cell(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, str):
        return v  # error string passes through
    if isinstance(v, float):
        if abs(v) >= 100:
            return f"{v:.1f}"
        if abs(v) >= 1:
            return f"{v:.3f}"
        return f"{v:.4f}"
    return str(v)


def render_table(rows: list[tuple[str, dict]]) -> str:
    header = ["sample"] + METRIC_COLS
    lines = ["| " + " | ".join(header) + " |",
             "| " + " | ".join(["---"] * len(header)) + " |"]
    for label, m in rows:
        cells = [label] + [fmt_cell(m.get(k)) for k in METRIC_COLS]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def group_of(path: str) -> str:
    name = Path(path).stem
    # match prefix before trailing _<digits>
    m = re.match(r"^(?:powdur_)?([a-zA-Z]+)", name)
    return m.group(1) if m else name


def aggregate(group_rows: list[dict]) -> dict:
    """Mean of numeric values; skip error strings and Nones."""
    agg = {}
    for k in METRIC_COLS:
        vals = [r[k] for r in group_rows if isinstance(r.get(k), (int, float))]
        agg[k] = float(np.mean(vals)) if vals else None
    return agg


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", required=True)
    ap.add_argument("--samples", nargs="+", required=True)
    ap.add_argument("--label-by-group", action="store_true",
                    help="append per-group aggregate rows")
    ap.add_argument("--json-out", default="/tmp/voice_similarity_results.json")
    args = ap.parse_args()

    t0 = time.time()

    # Filter existing samples
    samples = []
    for s in args.samples:
        if Path(s).exists():
            samples.append(s)
        else:
            print(f"skip (missing): {s}", file=sys.stderr)
    if not samples:
        print("no samples found", file=sys.stderr)
        return 1
    if not Path(args.ref).exists():
        print(f"ref missing: {args.ref}", file=sys.stderr)
        return 1

    print(f"Loading ECAPA-TDNN (speechbrain/spkrec-ecapa-voxceleb) — "
          f"will auto-download to ~/.cache on first use...", flush=True)
    model = EncoderClassifier.from_hparams(
        source="speechbrain/spkrec-ecapa-voxceleb",
        savedir="/tmp/spkrec-ecapa-voxceleb",
        run_opts={"device": "cuda" if torch.cuda.is_available() else "cpu"},
    )
    print(f"Model loaded on {next(model.mods.embedding_model.parameters()).device}", flush=True)

    print(f"Precomputing ref features: {args.ref}", flush=True)
    ref = precompute_ref(model, args.ref)

    rows: list[tuple[str, dict]] = []
    json_samples: list[dict] = []
    for s in samples:
        label = Path(s).name
        print(f"  -> {label}", flush=True)
        try:
            m = metrics_for_sample(model, ref, s)
        except Exception as e:
            traceback.print_exc()
            m = {k: f"err: {type(e).__name__}: {e}" for k in METRIC_COLS}
        rows.append((label, m))
        json_samples.append({"path": s, "metrics": m})

    # Aggregate rows
    if args.label_by_group:
        groups: dict[str, list[dict]] = {}
        for s, (_, m) in zip(samples, rows):
            groups.setdefault(group_of(s), []).append(m)
        for g, gms in groups.items():
            agg = aggregate(gms)
            rows.append((f"**[{g} mean, n={len(gms)}]**", agg))

    print()
    print(render_table(rows))
    print()

    out_json = {"ref": args.ref, "samples": json_samples}
    Path(args.json_out).write_text(json.dumps(out_json, indent=2, default=str))
    print(f"Wrote {args.json_out}")

    # Per-group ranking on speaker_sim
    if args.label_by_group:
        groups: dict[str, list[float]] = {}
        for s, (_, m) in zip(samples, rows[: len(samples)]):
            v = m.get("speaker_sim")
            if isinstance(v, (int, float)):
                groups.setdefault(group_of(s), []).append(v)
        ranking = sorted(
            ((g, float(np.mean(v))) for g, v in groups.items()),
            key=lambda kv: kv[1], reverse=True,
        )
        print("\nspeaker_sim ranking (best -> worst):")
        for g, v in ranking:
            print(f"  {g:10s} {v:.4f}")

    print(f"\nTotal runtime: {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
