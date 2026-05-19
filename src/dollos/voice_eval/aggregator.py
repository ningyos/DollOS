"""Aggregate per-sentence per-metric scores; render as markdown."""
from __future__ import annotations

import statistics
from dataclasses import dataclass


@dataclass
class ScoreEntry:
    sentence_idx: int
    sentence: str
    metric: str
    score: float


def aggregate(entries: list[ScoreEntry]) -> dict[str, dict[str, float]]:
    """Group by metric; compute mean/std/min/max/n."""
    by_metric: dict[str, list[float]] = {}
    for e in entries:
        by_metric.setdefault(e.metric, []).append(e.score)
    out: dict[str, dict[str, float]] = {}
    for metric, scores in by_metric.items():
        out[metric] = {
            "mean": statistics.fmean(scores),
            "std": statistics.pstdev(scores) if len(scores) > 1 else 0.0,
            "min": min(scores),
            "max": max(scores),
            "n": len(scores),
        }
    return out


def render_report(
    pack_path: str,
    engine: str,
    ref_audio: str,
    n_sentences: int,
    summary: dict[str, dict[str, float]],
    per_sentence: list[ScoreEntry],
    skipped: dict[str, str],
) -> str:
    """Render markdown scorecard."""
    lines = [
        f"# Voice scorecard — {pack_path}",
        "",
        f"- Engine: `{engine}`",
        f"- Ref audio: `{ref_audio}`",
        f"- Corpus: {n_sentences} sentences",
        "",
        "## Summary",
        "",
        "| Metric | Mean | Std | Min | Max |",
        "|--------|------|-----|-----|-----|",
    ]
    for metric, stats in summary.items():
        lines.append(
            f"| {metric} | {stats['mean']:.3f} | {stats['std']:.3f} | "
            f"{stats['min']:.3f} | {stats['max']:.3f} |"
        )
    for metric, reason in skipped.items():
        lines.append(f"| {metric} | _skipped_ | _{reason}_ | — | — |")

    if per_sentence:
        lines.append("")
        lines.append("## Per-sentence")
        lines.append("")
        metrics = sorted({e.metric for e in per_sentence})
        idx_to_text: dict[int, str] = {}
        idx_to_scores: dict[int, dict[str, float]] = {}
        for e in per_sentence:
            idx_to_text[e.sentence_idx] = e.sentence
            idx_to_scores.setdefault(e.sentence_idx, {})[e.metric] = e.score

        header = "| # | Sentence | " + " | ".join(metrics) + " |"
        sep = "|---|----------|" + "|".join(["---"] * len(metrics)) + "|"
        lines.append(header)
        lines.append(sep)
        for idx in sorted(idx_to_scores.keys()):
            row = [str(idx), f"`{idx_to_text[idx][:40]}`"]
            for m in metrics:
                v = idx_to_scores[idx].get(m)
                row.append(f"{v:.3f}" if v is not None else "—")
            lines.append("| " + " | ".join(row) + " |")

    return "\n".join(lines) + "\n"
