# Plan 5: Voice Scorecard

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A CLI tool — `uv run python scripts/voice_eval.py <pack>` — that synthesizes a fixed test corpus through the pack's TTS engine and runs five objective metrics: **WavLM speaker similarity** (timbre match), **UTMOSv2** (naturalness), **WER** (intelligibility), **NISQA** (perceived quality), **Prosody RMSE** (F0 contour). Output is a markdown scorecard.

**Why:** Powdur tuning took ~10 hours of ear-based A/B. With this scorecard a new character pack can be scored in ~5 minutes per candidate. Two candidates compared by their numbers, not by listening.

**Architecture:**
- Synthesize 10 fixed English sentences through the pack's engine — reuses `scripts/tune_voice_eq.py`'s pack-loading logic, refactored into shared `voice_eval/synth_driver.py`.
- Per metric: a thin runner module that loads the model once (lazy) and scores `(ref_wav, synth_wav)` or `(synth_wav, ref_text)` for WER.
- Aggregator: mean / std / range per metric across the corpus → markdown report.
- Pack-agnostic: works on any pack whose engine exposes a reference audio (qwen3-tts, fish-tts). Engines without a ref (piper) refuse to run.

**Tech Stack:** Python 3.13. New deps: `transformers` (WavLM), `faster-whisper`, `librosa` (F0), `jiwer` (WER), `utmosv2` (best-effort), `nisqa` (best-effort).

**Risk-graded scope:**
- **Core (must ship)**: WavLM sim, UTMOSv2, WER. ~80% of value, clean dependencies.
- **Best-effort**: NISQA + prosody RMSE. Time-box install at 2hr; if either is rough, mark `"skipped: install failed"` in the report and ship without.

**Out of scope:**
- Real-time scoring during cascade — offline dev tool.
- Two-pack comparison.
- Historical / regression tracking.

---

## File Structure

**New:**
- `src/dollos/voice_eval/__init__.py`
- `src/dollos/voice_eval/synth_driver.py` — pack discovery + corpus synth
- `src/dollos/voice_eval/wavlm_sim.py` — WavLM speaker-sim runner
- `src/dollos/voice_eval/utmos.py` — UTMOSv2 runner
- `src/dollos/voice_eval/wer.py` — faster-whisper + jiwer
- `src/dollos/voice_eval/nisqa.py` — best-effort
- `src/dollos/voice_eval/prosody.py` — best-effort
- `src/dollos/voice_eval/aggregator.py` — scores → markdown
- `scripts/voice_eval.py` — CLI
- `tests/test_voice_eval_synth_driver.py`
- `tests/test_voice_eval_wavlm_sim.py`
- `tests/test_voice_eval_wer.py`
- `tests/test_voice_eval_utmos.py`
- `tests/test_voice_eval_aggregator.py`
- `tests/test_voice_eval_prosody.py`

**Modified:**
- `scripts/tune_voice_eq.py` — import TEST_CORPUS from `voice_eval.synth_driver`
- `pyproject.toml` — add `voice-eval` extra

---

## Task 1: Shared synth driver

**Files:** create `src/dollos/voice_eval/__init__.py`, `src/dollos/voice_eval/synth_driver.py`, `tests/test_voice_eval_synth_driver.py`; modify `scripts/tune_voice_eq.py`.

**Public API of `synth_driver.py`:**

- `TEST_CORPUS: list[str]` — identical 10-sentence English list currently inlined in `scripts/tune_voice_eq.py` (lines 27-38). **Lift, don't copy.** Move it to `synth_driver`; `tune_voice_eq.py` imports from there.
- Path handling: `discover_engine_kwargs` should call `dollos.voice.pack.load_voice_config(pack_path)` which already resolves all `_PATH_KEYS` (including `ref_audio`, `voice_profile_paths`, etc.) to **absolute paths**. Use the absolute paths directly — don't re-resolve.
- `discover_engine_kwargs(pack: Path) -> tuple[str, dict, Path, str]` — returns `(engine_name, engine_kwargs, ref_audio_path, ref_text)`. Strips `eq_curve_path` from kwargs so the scorecard measures raw engine output, not EQ-corrected. Raises ValueError if engine has no reference (e.g. piper). qwen3-tts uses `ref_audio` + `ref_text` directly; fish-tts uses `voice_profile_paths[0]` and locates the matching `.wav` + `.txt` in `voice/transcripts/` by stem.
- `async synthesize_corpus(engine_name, engine_kwargs, out_dir: Path) -> list[Path]` — instantiate the TTS engine via `TTS_REGISTRY[engine_name](**engine_kwargs)`, synthesize each of the 10 sentences, write per-sentence WAV files to `out_dir`, return ordered list.

**Tests:**
- `test_test_corpus_constant`: 10 strings, all str
- `test_discover_qwen3_engine` with a fake pack TOML
- `test_discover_unknown_engine_raises`

**Steps:**
- [ ] Failing tests first
- [ ] Run `uv run pytest tests/test_voice_eval_synth_driver.py -v` → expect ModuleNotFoundError
- [ ] Implement (lift logic from tune_voice_eq.py lines 195-225 for engine instantiation; lines 25-38 for corpus)
- [ ] Refactor tune_voice_eq.py to import the corpus + helpers
- [ ] Pass + full suite green
- [ ] Commit: `feat(voice_eval): shared synth driver — corpus + pack discovery`

---

## Task 2: WavLM speaker similarity

**Files:** create `src/dollos/voice_eval/wavlm_sim.py`, `tests/test_voice_eval_wavlm_sim.py`.

**Public API:** `class WavLMSimRunner` with `score(ref_wav: Path, synth_wav: Path) -> float` returning cosine similarity in [-1, 1].

**Implementation outline:**
- Lazy-load `microsoft/wavlm-base-plus-sv` via `transformers.AutoFeatureExtractor` + `AutoModelForAudioXVector`
- Both audio paths resampled to 16 kHz mono before feature extraction
- Extract embeddings (`model(**inputs).embeddings`)
- Cosine sim helper: `np.dot(a, b) / (||a|| * ||b||)`

**Tests:**
- `test_cosine_sim_identical_vectors` (light, no model)
- `test_cosine_sim_orthogonal` (light)
- `test_wavlm_sim_real_model` — gated by `pytest.mark.skipif(not os.environ.get("VOICE_EVAL_HEAVY"))` to avoid 600 MB model download in CI

**Steps:**
- [ ] Failing tests
- [ ] Implement
- [ ] Pass light tests
- [ ] Commit: `feat(voice_eval): WavLM speaker similarity runner`

---

## Task 3: WER (faster-whisper + jiwer)

**Files:** create `src/dollos/voice_eval/wer.py`, `tests/test_voice_eval_wer.py`. Modify `pyproject.toml`.

**Public API:** `class WERRunner` with:
- `transcribe(wav_path) -> str` — uses `faster_whisper.WhisperModel("small", device="cpu", compute_type="int8")`
- `score(synth_wav, expected_text) -> float` — `jiwer.wer(normalize(expected), normalize(transcribe(synth_wav)))`. Range 0-1+ (insertions can push above 1).

**Normalization helper `_normalize_for_wer(text)`:**
- NFC unicode
- Lowercase
- Strip ASCII + CJK punctuation (`. , ! ? ; : ' " ( ) [ ] ， 。 ！ ？ ； ： 、 「 」 『 』`)
- Collapse whitespace

**Tests:**
- `test_normalize_drops_punct_and_lowercase` — light
- `test_normalize_collapses_whitespace` — light
- `test_wer_real_whisper` — heavy, gated

**pyproject.toml additions** under `[project.optional-dependencies]`:
```
voice-eval = ["transformers>=4.30", "faster-whisper>=0.10", "librosa>=0.10", "jiwer>=3.0"]
```

**Steps:**
- [ ] Failing tests
- [ ] Implement
- [ ] Pass + commit: `feat(voice_eval): WER via faster-whisper + jiwer`

---

## Task 4: UTMOSv2 naturalness (time-boxed 1.5hr)

**Files:** create `src/dollos/voice_eval/utmos.py`, `tests/test_voice_eval_utmos.py`.

**Time-box 1.5 hr on install.** If `pip install git+https://github.com/sarulab-speech/UTMOSv2.git` plus any minor fixes (resolving missing setup.py fields, etc.) don't yield a working `score()` within 1.5 hr, leave the runner module in place with `_ensure_loaded` raising and the CLI's try/except will skip the row in the report. Don't fight it longer.

**Implementation:** Lazy-load via `utmosv2` package. If the install eventually works, the runner exposes `class UTMOSRunner.score(wav_path) -> float` returning MOS in ~1-5.

**Tests:**
- Light import-only smoke
- `test_utmos_real_model` gated by `VOICE_EVAL_HEAVY`

**Steps:**
- [ ] Failing test
- [ ] Try install, with **1.5 hr cap**
- [ ] If install OK, implement + verify score on a sample wav
- [ ] If install fails, leave the runner module in place but its `_ensure_loaded` raises; the CLI's try/except will skip it
- [ ] Commit: `feat(voice_eval): UTMOSv2 naturalness runner` (or `feat(voice_eval): UTMOSv2 runner stub — install pending`)

---

## Task 5: Aggregator + report

**Files:** create `src/dollos/voice_eval/aggregator.py`, `tests/test_voice_eval_aggregator.py`.

**Public API:**

- `dataclass ScoreEntry(sentence_idx: int, sentence: str, metric: str, score: float)`
- `aggregate(entries: list[ScoreEntry]) -> dict[str, dict[str, float]]` — groups by metric, returns `{metric: {mean, std, min, max, n}}` using `statistics.fmean` + `statistics.pstdev`. Empty input → `{}`.
- `render_report(pack_path, engine, ref_audio, n_sentences, summary, per_sentence, skipped) -> str` — markdown with:
  - Header: pack path, engine name, ref audio path, corpus count
  - Summary table: metric / mean / std / min / max
  - Skipped table rows: metric / "skipped" / reason
  - Per-sentence pivot table: rows = sentences, cols = metrics

**Tests:**
- `test_aggregate_basic` — two metrics, two sentences
- `test_aggregate_handles_empty`
- `test_render_markdown_table` — verify metric name + score appears in output
- `test_render_marks_skipped_metrics` — verify skipped row + reason renders

**Steps:**
- [ ] Failing tests
- [ ] Implement
- [ ] Pass + commit: `feat(voice_eval): aggregator + markdown report`

---

## Task 6: NISQA + prosody (best-effort, time-boxed 2hr)

**Files:** create `src/dollos/voice_eval/nisqa.py`, `src/dollos/voice_eval/prosody.py`, `tests/test_voice_eval_prosody.py`.

**NISQA strategy:**
1. Try `uv pip install nisqa-tts`
2. If that fails: `uv pip install git+https://github.com/gabrielmittag/NISQA.git`
3. If both fail: leave runner module with `_ensure_loaded` raising. Don't fight install for longer than 1 hr.

**Prosody implementation — F0 stats distance (NOT contour RMSE):**

The ref and synth audio say **different sentences** (ref = the pack's reference clip; synth = our 10 English test corpus), so aligning F0 contours by length is meaningless. Instead compare **F0 distribution statistics** — captures whether the synth voice has the same overall pitch range / variability as the ref.

- `_f0_voiced_semitones(wav_path) -> np.ndarray` — `librosa.load` at 16 kHz mono, `librosa.pyin(fmin=C2, fmax=C7)`, drop NaN frames, convert to semitones above A4 (`12 * log2(f0 / 440)`)
- `_stats(semitones: np.ndarray) -> tuple[float, float, float, float]` — returns `(mean, p10, p90, std)` over the voiced frames
- `class ProsodyRunner.score(ref_wav, synth_wav) -> float`:
  - Compute stats for both
  - Euclidean distance between the two 4-D stat vectors in semitones
  - Lower = closer pitch profile. Range typically 0-20 semitones.

**Tests:**
- `test_to_semitones_math` — verify A4 → 0, A5 → 12
- `test_stats_handles_empty_voiced` — all-unvoiced input returns NaN distance, not crash
- `test_score_identical_refs_returns_zero` — same audio for both args → distance ≈ 0
- No heavy test (librosa is fast on CPU)

**Steps:**
- [ ] Try NISQA install (max 1 hr)
- [ ] Implement prosody runner (~1 hr)
- [ ] Tests + commit: `feat(voice_eval): NISQA + prosody runners (best-effort)`

---

## Task 7: CLI + Powdur smoke

**Files:** create `scripts/voice_eval.py`.

**CLI structure:**
- `argparse`: positional `pack`, optional `-o, --output report.md`
- `main_async(pack_dir, output)`:
  1. `discover_engine_kwargs(pack_dir)` → name, kwargs, ref_audio, ref_text
  2. `tempfile.TemporaryDirectory` for synth output
  3. `await synthesize_corpus(...)` → list of 10 WAV paths
  4. For each metric: try-construct + try-score; on failure record reason in `skipped` dict and continue
  5. `aggregate(entries)` + `render_report(...)` → print + optionally write to file
- Iterate metrics in order: wavlm_sim → utmos → wer → nisqa → prosody. Per metric: instantiate runner, loop over corpus, append `ScoreEntry` per sentence.

**Smoke GPU contention warning:** qwen3-tts loads transformers on GPU. If llama-server is running with `--tensor-split 1,1` (CLAUDE.md default), both GPUs are saturated and qwen3-tts will OOM during the smoke. **Stop llama-server before running the smoke** (`pkill -f llama-server`), or alternatively pass `device="cpu"` if the qwen3-tts engine config supports it (slow but works).

**Smoke:** After llama-server is stopped (or you have a free GPU):

```
pgrep -f llama-server  # confirm not running
uv run --extra voice-eval python scripts/voice_eval.py character_packs/powdur -o /tmp/powdur_eval.md
```

Expected outcome:
- WavLM sim score reasonable (>0.6 for EQ-corrected Powdur)
- WER low (<0.2 for English corpus)
- UTMOSv2 either runs or skipped with install reason
- NISQA / prosody either run or marked skipped

Inspect `/tmp/powdur_eval.md`. If healthy, optionally commit as baseline:

```
git add character_packs/powdur/voice/eval.md
git commit -m "chore(powdur): voice_eval baseline scorecard"
```

**Steps:**
- [ ] Write `scripts/voice_eval.py`
- [ ] Run on Powdur, inspect report
- [ ] Commit: `feat(voice_eval): CLI entrypoint`
- [ ] Optionally commit baseline scorecard

---

## Self-Review Checklist

**Spec coverage:**
- ✅ WavLM speaker sim → Task 2
- ✅ UTMOSv2 → Task 4
- ✅ WER → Task 3
- ✅ NISQA → Task 6 (best-effort)
- ✅ Prosody RMSE → Task 6 (best-effort)
- ✅ Markdown report → Task 5
- ✅ Pack-agnostic CLI → Task 7
- ✅ Cross-script corpus sharing → Task 1

**Type consistency:**
- All runners share signature `score(...) -> float` (some take `(ref, synth)`, WER takes `(synth, expected_text)`)
- `ScoreEntry` consistent across aggregator + CLI

**Out of scope (documented):**
- Real-time eval
- Two-pack comparison
- Historical tracking

**Known limitations:**
- WavLM ~600 MB; first run downloads (cached)
- faster-whisper `small` ~250 MB CPU
- UTMOSv2 install path may be rough — runner gracefully skips if install failed
- NISQA genuinely best-effort
- Prosody RMSE uses linear length-normalization not DTW; systematic timing offsets inflate the score. Adequate for relative ranking; not for absolute prosody quality

---

**Plan complete.**
