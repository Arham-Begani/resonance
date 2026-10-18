"""Independent numeric checks of the milestone-1 contract policy (verification engineer).

Analytical first-order RC formulas only (no project code), so the results are independent of
the builders' implementations. Run from services/api:

    uv run python ../../docs/verification/scripts/contract_numerics.py

Checks:
  1. Hypothesis separability per instrument vs. nominal cutoff, using the declared tolerances
     (EVIDENCE_* in contracts/policy.py) on the declared 31-point grid and the two demo tones.
  2. Settling: residual start-up transient after TRANSIENT_SETTLE_S and its effect on the
     tone fit, for slow circuits.
  3. Audio headroom: peak of the two-tone input / outputs at PLAYBACK_GAIN_FS_PER_V = 1.0.
"""

from __future__ import annotations

import itertools
import math

import numpy as np

# --- Declared policy (copied, so a later contract change shows up as a diff here) ---------
GAIN_TOL_DB = 0.5
PHASE_TOL_DEG = 3.0
CURRENT_TOL_PCT = 3.0
TONE_TOL_DB = 0.5
SETTLE_S = 0.2
CLIP_S = 1.2
FS = 48_000
TONES = ((440.0, 0.6), (4000.0, 0.3))
GRID = 20.0 * 10 ** (np.arange(31) / 10.0)  # 20 Hz .. 20 kHz, 10/decade

HYP = {
    "no_fault": (1.0, 1.0),
    "r1_increased": (2.0, 1.0),
    "r1_decreased": (0.5, 1.0),
    "c1_increased": (1.0, 2.0),
    "c1_decreased": (1.0, 0.5),
}


def h_v(f: np.ndarray, r: float, c: float) -> np.ndarray:
    return 1.0 / (1.0 + 2j * np.pi * f * r * c)


def h_i(f: np.ndarray, r: float, c: float) -> np.ndarray:
    """Delivered source current for a 1 V source (A)."""
    w = 2 * np.pi * f
    return 1j * w * c / (1.0 + 1j * w * r * c)


def db(x: np.ndarray) -> np.ndarray:
    return 20 * np.log10(np.abs(x))


def wrap_deg(d: np.ndarray) -> np.ndarray:
    return (d + 180.0) % 360.0 - 180.0


def separation(kind: str, fc: float, a: str, b: str, tone_phase: bool = False) -> float:
    """max(|deviation| / tolerance) between hypotheses a and b for one instrument.
    > 1 means b is contradicted when a is true (and vice versa)."""
    r0 = 1000.0
    c0 = 1.0 / (2 * np.pi * r0 * fc)
    ra, ca = r0 * HYP[a][0], c0 * HYP[a][1]
    rb, cb = r0 * HYP[b][0], c0 * HYP[b][1]
    if kind == "voltage_sweep":
        ha, hb = h_v(GRID, ra, ca), h_v(GRID, rb, cb)
        g = np.max(np.abs(db(ha) - db(hb))) / GAIN_TOL_DB
        p = np.max(np.abs(wrap_deg(np.angle(ha, True) - np.angle(hb, True)))) / PHASE_TOL_DEG
        return float(max(g, p))
    if kind == "current_sweep":
        ia, ib = h_i(GRID, ra, ca), h_i(GRID, rb, cb)
        m = np.max(np.abs(np.abs(ia) - np.abs(ib)) / np.abs(ib)) * 100 / CURRENT_TOL_PCT
        p = np.max(np.abs(wrap_deg(np.angle(ia, True) - np.angle(ib, True)))) / PHASE_TOL_DEG
        return float(max(m, p))
    if kind == "symptom":
        f = np.array([t[0] for t in TONES])
        ha, hb = h_v(f, ra, ca), h_v(f, rb, cb)
        g = np.max(np.abs(db(ha) - db(hb))) / TONE_TOL_DB
        if not tone_phase:
            return float(g)
        p = np.max(np.abs(wrap_deg(np.angle(ha, True) - np.angle(hb, True)))) / PHASE_TOL_DEG
        return float(max(g, p))
    raise ValueError(kind)


def classes(kind: str, fc: float, tone_phase: bool = False) -> list[list[str]]:
    """Partition of hypotheses that remain mutually consistent (connected components)."""
    names = list(HYP)
    parent = {n: n for n in names}

    def find(x: str) -> str:
        while parent[x] != x:
            x = parent[x]
        return x

    for a, b in itertools.combinations(names, 2):
        if separation(kind, fc, a, b, tone_phase) <= 1.0:
            parent[find(a)] = find(b)
    groups: dict[str, list[str]] = {}
    for n in names:
        groups.setdefault(find(n), []).append(n)
    return sorted(groups.values(), key=lambda g: names.index(g[0]))


def short(groups: list[list[str]]) -> str:
    abbrev = {"no_fault": "N", "r1_increased": "R+", "r1_decreased": "R-",
              "c1_increased": "C+", "c1_decreased": "C-"}
    return " ".join("{" + ",".join(abbrev[n] for n in g) + "}" for g in groups)


def borderline(kind: str, fc: float, lo: float = 0.5, hi: float = 2.0,
               tone_phase: bool = False) -> list[str]:
    out = []
    for a, b in itertools.combinations(HYP, 2):
        s = separation(kind, fc, a, b, tone_phase)
        if lo < s < hi:
            out.append(f"{a}/{b}={s:.2f}")
    return out


def check_separability() -> None:
    print("== 1. Separability (groups = hypotheses NOT separable by that instrument) ==")
    print("   N=no_fault R+/R-=R1 x2/x0.5 C+/C-=C1 x2/x0.5; tolerances as declared")
    fcs = [10, 20, 50, 100, 200, 400, 1000, 2000, 4000, 8000, 10000, 20000, 40000, 80000]
    for fc in fcs:
        print(f"fc={fc:>6} Hz | symptom(tone gain) {short(classes('symptom', fc)):<26}"
              f"| +tone phase {short(classes('symptom', fc, True)):<26}"
              f"| V-sweep {short(classes('voltage_sweep', fc)):<22}"
              f"| I-sweep {short(classes('current_sweep', fc))}")
    print("\n   Equivalence check (should be ~0): max |dB| / |deg| between R+ and C+ voltage:")
    for fc in (400, 4000):
        r0 = 1000.0
        c0 = 1 / (2 * np.pi * r0 * fc)
        a, b = h_v(GRID, 2 * r0, c0), h_v(GRID, r0, 2 * c0)
        print(f"   fc={fc}: {np.max(np.abs(db(a) - db(b))):.3e} dB, "
              f"{np.max(np.abs(np.angle(a, True) - np.angle(b, True))):.3e} deg")
        ia, ib = h_i(GRID, 2 * r0, c0), h_i(GRID, r0, 2 * c0)
        print(f"   fc={fc}: current ratio C+/R+ min {np.min(np.abs(ib) / np.abs(ia)):.4f} "
              f"max {np.max(np.abs(ib) / np.abs(ia)):.4f}")
    print("\n   Borderline pairs (0.5 < deviation/tolerance < 2) on the SYMPTOM (transient fit,"
          " where fit noise matters):")
    for fc in np.round(np.geomspace(100, 40000, 25)):
        b = borderline("symptom", float(fc))
        if b:
            print(f"   fc={fc:>7.0f}: {', '.join(b)}")
    print("\n   Smallest separation ratio of any NON-equivalent pair per instrument:")
    for fc in fcs:
        row = []
        for kind in ("voltage_sweep", "current_sweep"):
            vals = []
            for a, b in itertools.combinations(HYP, 2):
                s = separation(kind, fc, a, b)
                if s > 1e-6:
                    vals.append(s)
            row.append(f"{kind}={min(vals):.2f}")
        print(f"   fc={fc:>6}: {'  '.join(row)}")


def two_tone_lowpass(t: np.ndarray, tau: float) -> tuple[np.ndarray, np.ndarray]:
    """Exact zero-state response of an RC low-pass to sum of sines starting at t = 0."""
    vin = np.zeros_like(t)
    vout = np.zeros_like(t)
    for f, a in TONES:
        w = 2 * np.pi * f
        h = 1 / (1 + 1j * w * tau)
        mag, ph = abs(h), np.angle(h)
        vin += a * np.sin(w * t)
        steady = a * mag * np.sin(w * t + ph)
        k = -a * mag * np.sin(ph)  # v_out(0) = 0
        vout += steady + k * np.exp(-t / tau)
    return vin, vout


def fit_amp(x: np.ndarray, f: float) -> float:
    n = len(x)
    t = np.arange(n) / FS
    return float(2 * abs(np.sum(x * np.exp(-2j * np.pi * f * t))) / n)


def check_settling() -> None:
    print("\n== 2. Settling with TRANSIENT_SETTLE_S = 0.2 s ==")
    tau_max = SETTLE_S / 7
    print(f"   7 tau rule -> tau_max = {tau_max * 1e3:.2f} ms -> fc_min = "
          f"{1 / (2 * np.pi * tau_max):.2f} Hz for the slowest circuit INCLUDING x2 faults;"
          f" nominal case fc_min = {2 / (2 * np.pi * tau_max):.2f} Hz")
    t = SETTLE_S + np.arange(int(CLIP_S * FS)) / FS
    for fc in (1.0, 2.0, 5.0, 5.57, 10.0, 20.0):
        tau = 1 / (2 * np.pi * fc)
        _, vout = two_tone_lowpass(t, tau)
        errs = []
        for f, a in TONES:
            expect = a * abs(1 / (1 + 2j * np.pi * f * tau))
            errs.append(20 * math.log10(fit_amp(vout, f) / expect))
        tail = sum(-a * abs(1 / (1 + 2j * np.pi * f * tau))
                   * math.sin(np.angle(1 / (1 + 2j * np.pi * f * tau))) for f, a in TONES)
        print(f"   fc={fc:>5} Hz (tau={tau * 1e3:6.1f} ms, settle={SETTLE_S / tau:5.2f} tau): "
              f"tone-fit error 440 Hz {errs[0]:+.4f} dB, 4 kHz {errs[1]:+.4f} dB; "
              f"residual DC tail at clip start {tail * math.exp(-SETTLE_S / tau) * 1e3:.4f} mV")


def check_headroom() -> None:
    print("\n== 3. Audio headroom at PLAYBACK_GAIN_FS_PER_V = 1.0 ==")
    t = SETTLE_S + np.arange(int(CLIP_S * FS)) / FS
    vin, _ = two_tone_lowpass(t, 1e-6)
    print(f"   input peak over the clip on the 48 kHz grid: {np.max(np.abs(vin)):.5f} FS")
    worst = 0.0
    for fc in np.geomspace(5, 50000, 60):
        _, vout = two_tone_lowpass(t, 1 / (2 * np.pi * fc))
        worst = max(worst, float(np.max(np.abs(vout))))
    print(f"   worst low-pass output peak for fc in [5 Hz, 50 kHz]: {worst:.5f} FS (<1 => no clip)")
    lsb = 1 / 32768
    for fc in (10, 50):
        a4k = 0.3 * abs(1 / (1 + 1j * 4000 / fc))
        print(f"   fc={fc} Hz: 4 kHz output amplitude {a4k * 1e3:.3f} mV = {a4k / lsb:.1f} LSB "
              f"(16-bit) -> {20 * math.log10(a4k):.1f} dBFS")


if __name__ == "__main__":
    check_separability()
    check_settling()
    check_headroom()
