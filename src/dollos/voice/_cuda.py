"""CUDA setup helper for DollOS voice engines.

Preloads libcudnn*.so.9 into the global symbol table so that
onnxruntime's CUDAExecutionProvider can dlopen them by soname.

Background: pip's nvidia-cudnn-cu12 wheel places libcudnn*.so.9 under
site-packages/nvidia/cudnn/lib/, which is not on LD_LIBRARY_PATH.
onnxruntime's CUDA provider dlopen()s cudnn by soname at session-create
time and fails with "libcudnn.so.9: cannot open shared object file".
Preloading via ctypes.CDLL(..., RTLD_GLOBAL) before the first InferenceSession
puts the libs in the process symbol table so the soname lookup succeeds.
"""
from __future__ import annotations

import ctypes
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_preloaded: int | None = None  # None = not yet run; int = result of first run


def preload_cudnn() -> int:
    """Preload libcudnn*.so.9 from the nvidia-cudnn-cu12 wheel into RTLD_GLOBAL.

    Must be called before any onnxruntime InferenceSession that uses
    CUDAExecutionProvider. Idempotent — subsequent calls return the count
    from the first run without re-loading.

    Returns:
        Number of .so files successfully preloaded (0 if wheel absent or on
        error; never raises).
    """
    global _preloaded
    if _preloaded is not None:
        return _preloaded

    # Locate the cudnn lib directory via the wheel's __path__.
    # (nvidia.cudnn is a PEP-420 namespace package, so __file__ is None;
    # use __path__[0] instead.)
    try:
        import nvidia.cudnn  # type: ignore[import]
        cudnn_path = nvidia.cudnn.__path__
        if not cudnn_path:
            raise ImportError("nvidia.cudnn.__path__ is empty")
        lib_dir = Path(cudnn_path[0]) / "lib"
        if not lib_dir.exists():
            raise ImportError(f"cudnn lib dir not found: {lib_dir}")
    except (ImportError, AttributeError, TypeError, IndexError):
        logger.debug("nvidia.cudnn wheel not found — skipping cudnn preload")
        _preloaded = 0
        return 0

    # Glob all libcudnn*.so.9, sort sub-libs before the dispatcher.
    # The bare libcudnn.so.9 is the dispatcher and must load last so that its
    # sub-library dlopens hit the already-loaded symbols.
    so_files = sorted(
        lib_dir.glob("libcudnn*.so.9"),
        key=lambda p: (1 if p.name == "libcudnn.so.9" else 0, p.name),
    )

    if not so_files:
        logger.debug("no libcudnn*.so.9 found in %s — skipping preload", lib_dir)
        _preloaded = 0
        return 0

    count = 0
    for so in so_files:
        try:
            ctypes.CDLL(str(so), mode=ctypes.RTLD_GLOBAL)
            count += 1
        except OSError as exc:
            logger.warning("cudnn preload: could not load %s: %s", so.name, exc)

    if count:
        logger.info("preloaded %d cudnn libs from %s", count, lib_dir)
    else:
        logger.debug("cudnn preload attempted but no libs loaded from %s", lib_dir)

    _preloaded = count
    return count
