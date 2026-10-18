"""Declared resolutions for every measured value that leaves the evaluator (finding A-01).

ngspice round-off differs by ~1e-15 relative between voltage-equivalent hidden circuits (e.g.
R1×0.5 vs C1×0.5 for some nominal designs), and the computation is reproducible on any public
circuit, so full-precision numbers would fingerprint which component drifted. Every value
derived from a hidden-circuit run — and the matching intended-design reference values, for
consistency — is therefore quantized at the evaluator boundary to these resolutions. They are
≥ 500× finer than the declared evidence tolerances (0.5 dB, 3°, 3 %), so hypothesis
evaluation on the quantized values is unaffected.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

QUANT_DB_STEP = 0.001
"""dB values (sweep gain, tone gain, audio gain adjustment) are rounded to 0.001 dB."""

QUANT_PHASE_STEP_DEG = 0.01
"""Phases (sweep, tone) are rounded to 0.01°."""

QUANT_AMPLITUDE_SIG_FIGS = 6
"""Voltage/current amplitudes and sweep current magnitudes (V or mA): 6 significant figures."""

QUANT_WAVEFORM_SIG_FIGS = 6
"""Waveform window samples (V or mA): 6 significant figures."""

QUANT_PEAK_FS_DECIMALS = 4
"""Audio clip ``peak_fs`` (fraction of full scale): 4 decimal places."""

_DB_DECIMALS = 3
_PHASE_DECIMALS = 2


def q_db(x: float) -> float:
    return round(float(x), _DB_DECIMALS) + 0.0  # + 0.0 turns -0.0 into 0.0


def q_phase(x: float) -> float:
    """Round to 0.01° and keep the (−180, 180] convention."""
    y = round(float(x), _PHASE_DECIMALS) + 0.0
    return 180.0 if y <= -180.0 else y


def q_sig(x: float, figures: int = QUANT_AMPLITUDE_SIG_FIGS) -> float:
    x = float(x)
    if x == 0.0 or not math.isfinite(x):
        return x + 0.0
    return float(f"{x:.{figures}g}") + 0.0


def q_series(values: Iterable[float]) -> list[float]:
    return [q_sig(v, QUANT_WAVEFORM_SIG_FIGS) for v in values]


def q_peak(x: float) -> float:
    return round(float(x), QUANT_PEAK_FS_DECIMALS) + 0.0
