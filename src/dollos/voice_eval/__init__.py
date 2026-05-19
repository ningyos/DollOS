"""Voice quality evaluation — shared synth driver + scorecard CLI.

Pack-agnostic infrastructure: load a character pack's TTS config, synthesize
a fixed English corpus, and evaluate the output against the pack's reference
clip. Used both by ``scripts/tune_voice_eq.py`` (EQ derivation) and the
``voice_eval`` CLI (scorecard).
"""

from dollos.voice_eval.synth_driver import (
    TEST_CORPUS,
    discover_engine_kwargs,
    synthesize_corpus,
)

__all__ = ["TEST_CORPUS", "discover_engine_kwargs", "synthesize_corpus"]
