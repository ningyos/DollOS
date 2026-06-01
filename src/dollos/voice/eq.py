"""Shared EQ-curve loader for TTS engines.

Exports ``load_eq_curve(path, engine_sample_rate, n_taps=511)``
which reads a JSON EQ-curve file and returns ``(fir_coeffs, peak_normalize)``.

Schema::

    {
      "name": "...",
      "sample_rate": 24000,
      "bands": [{"freq_hz": 93, "gain_db": -0.10}, ...],
      "peak_normalize": 0.95
    }

Designs a linear-phase FIR via ``scipy.signal.firwin2`` matching the band
gains.  Raises ``ValueError`` if the JSON sample_rate doesn't match the
engine, and ``FileNotFoundError`` if the path doesn't exist.
"""
from __future__ import annotations

import json
from pathlib import Path


def load_eq_curve(path: Path | str, engine_sample_rate: int, n_taps: int = 511):
    """Load JSON EQ curve and return ``(fir_coeffs, peak_normalize)``.

    Parameters
    ----------
    path:
        Path to the EQ JSON file.
    engine_sample_rate:
        The TTS engine's sample rate in Hz.  The JSON ``sample_rate`` field
        must match; if it doesn't a ``ValueError`` is raised.
    n_taps:
        Number of FIR filter taps (must be odd for type-I linear phase; if
        even, incremented by 1 automatically).

    Returns
    -------
    fir_coeffs : np.ndarray, dtype float32
        FIR filter coefficients array of length *n_taps* (possibly adjusted).
    peak_normalize : float
        Target peak amplitude value read from the JSON (default 0.95).
    """
    import numpy as np
    from scipy.signal import firwin2

    payload = json.loads(Path(path).read_text())
    sr = int(payload["sample_rate"])
    if sr != engine_sample_rate:
        raise ValueError(
            f"EQ curve sample_rate={sr} mismatches engine sample_rate="
            f"{engine_sample_rate} (path={path})"
        )
    bands = payload["bands"]
    if not bands:
        raise ValueError(f"EQ curve has no bands: {path}")
    peak_normalize = float(payload.get("peak_normalize", 0.95))

    freqs_hz = [float(b["freq_hz"]) for b in bands]
    gains_db = [float(b["gain_db"]) for b in bands]
    nyq = sr / 2.0
    # firwin2 needs freqs strictly increasing, starting at 0 ending at nyq.
    pts: list[tuple[float, float]] = [(0.0, gains_db[0])]
    last = 0.0
    for f, g in zip(freqs_hz, gains_db):
        f_clamped = min(max(f, last + 1.0), nyq - 1.0)
        if f_clamped <= last:
            continue
        pts.append((f_clamped, g))
        last = f_clamped
    pts.append((nyq, gains_db[-1]))

    freqs_norm = np.array([p[0] / nyq for p in pts], dtype=np.float64)
    gains_lin = np.array([10.0 ** (p[1] / 20.0) for p in pts], dtype=np.float64)
    # firwin2 requires odd numtaps for type-I linear phase across full band.
    if n_taps % 2 == 0:
        n_taps += 1
    fir = firwin2(n_taps, freqs_norm, gains_lin)
    return fir.astype(np.float32), peak_normalize
