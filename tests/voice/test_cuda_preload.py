"""Tests for dollos.voice._cuda.preload_cudnn.

Environment-agnostic: all tests pass whether or not a GPU / cudnn wheel is
present. The integration path (real libs loaded) is exercised only when the
nvidia-cudnn-cu12 wheel is installed and the .so files are present.
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch


def _reset_module():
    """Remove _cuda from sys.modules so the module-level flag resets."""
    for key in list(sys.modules):
        if key == "dollos.voice._cuda" or key.endswith("._cuda"):
            del sys.modules[key]


def test_preload_cudnn_returns_int():
    """preload_cudnn() must return an int and never raise."""
    from dollos.voice._cuda import preload_cudnn
    result = preload_cudnn()
    assert isinstance(result, int)


def test_preload_cudnn_no_raise_on_bad_environment():
    """Must not raise even when called in a weird environment."""
    from dollos.voice._cuda import preload_cudnn
    try:
        preload_cudnn()
    except Exception as exc:  # pragma: no cover
        raise AssertionError(f"preload_cudnn raised unexpectedly: {exc!r}") from exc


def test_preload_cudnn_no_op_when_nvidia_cudnn_absent():
    """Returns 0 and doesn't raise when nvidia.cudnn is not importable."""
    _reset_module()

    # Inject a marker that will cause ImportError when nvidia.cudnn is imported.
    # We patch builtins.__import__ via sys.modules sentinel.
    original = sys.modules.copy()
    # Prevent the real nvidia.cudnn from being importable.
    sys.modules["nvidia"] = None  # type: ignore[assignment]
    sys.modules["nvidia.cudnn"] = None  # type: ignore[assignment]
    try:
        from dollos.voice._cuda import preload_cudnn
        result = preload_cudnn()
        assert result == 0
    finally:
        # Restore sys.modules
        for key in ["nvidia", "nvidia.cudnn"]:
            if key in original:
                sys.modules[key] = original[key]
            elif key in sys.modules:
                del sys.modules[key]
        _reset_module()


def test_preload_cudnn_idempotent():
    """Second call returns the same value without re-invoking ctypes.CDLL."""
    _reset_module()

    cdll_call_count = 0
    real_cdll_class = None

    import ctypes as _ctypes
    real_cdll_class = _ctypes.CDLL

    class CountingCDLL:
        def __init__(self, name, mode=0):
            nonlocal cdll_call_count
            cdll_call_count += 1
            # Raise OSError so the test is environment-agnostic (no real .so needed).
            raise OSError(f"test stub: {name}")

    with patch("ctypes.CDLL", CountingCDLL):
        from dollos.voice._cuda import preload_cudnn
        first = preload_cudnn()
        calls_after_first = cdll_call_count

        second = preload_cudnn()
        calls_after_second = cdll_call_count

    assert second == first, "second call must return same value as first"
    assert calls_after_second == calls_after_first, (
        "CDLL must not be called again on second preload_cudnn() call"
    )

    _reset_module()


def test_preload_cudnn_idempotent_when_no_wheel():
    """Idempotency also holds when nvidia.cudnn is absent (both calls return 0)."""
    _reset_module()

    original = sys.modules.copy()
    sys.modules["nvidia"] = None  # type: ignore[assignment]
    sys.modules["nvidia.cudnn"] = None  # type: ignore[assignment]
    try:
        from dollos.voice._cuda import preload_cudnn
        assert preload_cudnn() == 0
        assert preload_cudnn() == 0
    finally:
        for key in ["nvidia", "nvidia.cudnn"]:
            if key in original:
                sys.modules[key] = original[key]
            elif key in sys.modules:
                del sys.modules[key]
        _reset_module()
