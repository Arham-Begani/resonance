# Lens C — physics, audio honesty, explore and hypotheses

**Provenance.** The lens C verification agent wrote `services/api/tests/verification/test_v_physics_audio.py`
(1 114 lines) and then terminated on an account error before writing this report. This summary was
written by the lead from that test file and from re-running it; it is not the verifier's own report.

## What the tests check (all independent of `resonance.simulation.analytic`)

1. ngspice AC gain/phase and delivered source current against first-order RC formulas written in
   the test file, for a seeded random sample across the template bounds plus the corners, each
   with 2×/0.5× R and C variants, on a log grid around each variant's cutoff (1 % / 1°).
2. The Explore bundle over HTTP at the bound corners: transient tone levels against exact AC
   (0.5 dB, 1°), settling sufficiency at the slowest allowed design.
3. Audio honesty: WAVs are downloaded and decoded in the test, tones are fitted from PCM, and the
   PCM output/input ratio must equal the reported tone gain within 0.1 dB; identical
   format/length/fades; the compare endpoint's clips share one gain; an over-range comparison
   (reachable only through the renderer) gets one shared, disclosed reduction.
4. Goal pass/fail against measured gain; feasibility interval equals the analytical interval for
   the default goal; contradictory goals are infeasible.
5. Units parser and netlist safety through the public API (battery of valid, ambiguous, unicode,
   RKM, injection, non-finite and non-positive inputs).
6. Hypothesis evaluation: an independent reference partition for every drawable nominal design,
   the service's `assess` on synthetic observations (including deviations just beyond tolerance),
   and real-ngspice cases at the extreme drawable designs with correct and wrong repairs.
7. Spectrum peaks against fitted tone levels; dB floors (no non-finite JSON).

## Findings

| ID | Severity | Finding | Status |
|---|---|---|---|
| C-01 | minor | Goal feasibility ignored the lab's 20 Hz–20 kHz cutoff range, so a goal needing e.g. fc ≥ 124.5 kHz was reported feasible although no accepted design could meet it | Fixed by lead in `explore/goals.py` (intersect with the template range; explain when only an unsupported cutoff would work). Strict xfail flipped to pass; marker removed. |
| C-03 | minor | JSON `true` in a component value was coerced to 1.0 (1 Ω) before the units parser | Fixed by lead: `ComponentValueInput` rejects booleans. Strict xfail flipped to pass; marker removed. |

No C-02 was recorded in the test file or elsewhere.

## Result

After the fixes, the full backend suite (including this file) passed: 544 passed, 3 skipped
(opt-in live AI tests), `RESONANCE_REQUIRE_NGSPICE=1`, 2026-10-18.
