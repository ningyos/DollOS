import pytest


def test_aggregate_basic():
    from dollos.voice_eval.aggregator import aggregate, ScoreEntry
    entries = [
        ScoreEntry(0, "hello.", "wavlm_sim", 0.82),
        ScoreEntry(1, "bye.",   "wavlm_sim", 0.85),
        ScoreEntry(0, "hello.", "wer",        0.10),
        ScoreEntry(1, "bye.",   "wer",        0.00),
    ]
    agg = aggregate(entries)
    assert set(agg.keys()) == {"wavlm_sim", "wer"}
    assert agg["wavlm_sim"]["mean"] == pytest.approx(0.835, abs=1e-3)
    assert agg["wer"]["mean"] == pytest.approx(0.05, abs=1e-3)
    assert agg["wavlm_sim"]["n"] == 2


def test_aggregate_empty_returns_empty():
    from dollos.voice_eval.aggregator import aggregate
    assert aggregate([]) == {}


def test_aggregate_single_score_zero_std():
    from dollos.voice_eval.aggregator import aggregate, ScoreEntry
    agg = aggregate([ScoreEntry(0, "x", "wer", 0.5)])
    assert agg["wer"]["std"] == 0.0
    assert agg["wer"]["mean"] == 0.5


def test_render_basic():
    from dollos.voice_eval.aggregator import render_report
    summary = {
        "wavlm_sim":  {"mean": 0.82, "std": 0.04, "min": 0.75, "max": 0.88, "n": 10},
        "wer":        {"mean": 0.08, "std": 0.05, "min": 0.0,  "max": 0.15, "n": 10},
    }
    md = render_report(
        pack_path="character_packs/powdur",
        engine="qwen3-tts",
        ref_audio="voice/transcripts/j3DAXXUiGJw.wav",
        n_sentences=10,
        summary=summary,
        per_sentence=[],
        skipped={},
    )
    assert "character_packs/powdur" in md
    assert "qwen3-tts" in md
    assert "wavlm_sim" in md
    assert "0.82" in md
    assert "wer" in md


def test_render_skipped_rows():
    from dollos.voice_eval.aggregator import render_report
    md = render_report(
        pack_path="p", engine="e", ref_audio="r.wav", n_sentences=10,
        summary={},
        per_sentence=[],
        skipped={"nisqa": "install failed", "utmos": "module not found"},
    )
    assert "nisqa" in md
    assert "install failed" in md
    assert "utmos" in md


def test_render_per_sentence_pivot():
    """Per-sentence table has rows=sentence_idx, cols=metrics."""
    from dollos.voice_eval.aggregator import render_report, ScoreEntry
    per_sent = [
        ScoreEntry(0, "Hello there.", "wavlm_sim", 0.81),
        ScoreEntry(0, "Hello there.", "wer", 0.0),
        ScoreEntry(1, "Bye.", "wavlm_sim", 0.85),
        ScoreEntry(1, "Bye.", "wer", 0.0),
    ]
    md = render_report(
        pack_path="p", engine="e", ref_audio="r.wav", n_sentences=2,
        summary={},
        per_sentence=per_sent,
        skipped={},
    )
    assert "Hello there" in md
    assert "0.81" in md
    assert "Bye" in md
