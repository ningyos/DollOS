import sys

import pytest


def test_nisqa_runner_imports():
    from dollos.voice_eval.nisqa import NISQARunner
    assert NISQARunner() is not None


def test_nisqa_unavailable_raises_clean_error():
    """If nisqa not installed, _ensure_loaded raises RuntimeError."""
    saved = sys.modules.get("nisqa")
    sys.modules["nisqa"] = None  # type: ignore[assignment]
    try:
        from dollos.voice_eval.nisqa import NISQARunner
        runner = NISQARunner()
        with pytest.raises(RuntimeError, match="not available"):
            runner._ensure_loaded()
    finally:
        if saved is not None:
            sys.modules["nisqa"] = saved
        else:
            sys.modules.pop("nisqa", None)
