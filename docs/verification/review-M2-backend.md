# Review M2-backend: high-pass, open capacitor, Learn, suggestions, evaluation

Reviewer: independent verification engineer (lens M2-backend). Dates: 2026-10-23/24.
Tests: `services/api/tests/verification/test_v_m2_backend.py` (32 tests: 29 pass, 3 strict
xfails documenting M2-01..M2-03). Run with:

```
cd services/api
RESONANCE_REQUIRE_NGSPICE=1 uv run pytest tests/verification/test_v_m2_backend.py -rxX
```

Observed (final run, 2026-10-24, shared 4-core host): `29 passed, 3 xfailed in 171.62s`. The
file passes `ruff check`, `ruff format --check` and `mypy`.

Earlier lenses (A–D, `contract-review.md`) are not re-reported. A-01 (full-precision
fingerprint) is fixed by evaluator quantization; this lens only re-checks shape and floors.

**Limits.** No `NEBIUS_API_KEY`: no live inference; agent behaviour is checked only through the
agent-visible view builders (`agent/views.py`), not through a model. Fast data-flow checks use
the builders' analytical test double (`AnalyticTestSimulator`); every physics and audio claim
below was checked on real ngspice 47. The agent-view floor markers were checked against the
working tree while that change was uncommitted; the lead reports it has since been committed.

## Findings

| ID | Severity | Finding | Test (strict xfail) |
|---|---|---|---|
| M2-01 | minor | Learn grades with the unrounded measured change, but the explanation, tradeoff and comparison show it rounded. A measured −0.997 dB is graded "about the same" while every number on screen says "1.00 dB quieter", so a learner who predicted "quieter" is marked wrong. | `test_learn_text_and_grade_agree_just_below_one_db` |
| M2-02 | minor | Measurement floors appear as measurements in browser payloads. The evidence check reports the −200 dB clamp as `observed`, but `deviation` is measured from the −120 dB floor. `max_gain_deviation_db` ≈ 200 dB for a still-silent repair. The UI prints these numbers as levels. | `test_evidence_check_numbers_are_arithmetically_consistent_at_the_floor` |
| M2-03 | minor | Low-pass open capacitor: solver round-off is published as a six-figure tone current (1.11987e-17 mA). The same observation's waveform is all zeros and its summary says "no measurable current". | `test_below_floor_magnitudes_are_published_as_exact_zero` |

No blocker or major finding. No hidden-state leak was found for the new family or conditions.

### M2-01 (minor): Learn grade contradicts the numbers shown at the 1 dB boundary

**Location.** `resonance/explore/learn.py`:
- `observed_change` grades with `abs(delta_db) < LEARN_SAME_THRESHOLD_DB` on the raw delta.
- `_change_text` and `attempt_tradeoff` print the delta with 2 decimals.
- `explore/experiments.py` `_delta_phrase` (the comparison summary in the same attempt) prints
  it with 1 decimal.

**Reproduction (real ngspice, HTTP).**
1. Create a low-pass revision with R1 = 1109.53 Ω (`"1109.530…"`) and C1 = 100 nF
   (fc ≈ 1.434 kHz).
2. Post a Learn attempt with C1 ×2, predicting "quieter" for both tones.
3. The response contains:
   - `graded[440 Hz]`: `delta_db = −0.99700`, `observed = about_the_same`, `correct = false`.
   - explanation: "Musical tone (440 Hz) -0.39 dB → -1.39 dB, **1.00 dB quieter, about the
     same (under 1 dB)** … (Musical tone: you predicted quieter, measured about the same)".
   - tradeoff: "… wanted Musical tone (440 Hz) 1.00 dB quieter … at a cost of 1.00 dB to the
     musical tone".
   - comparison summary: "(1.0 dB more attenuation)".

With R1 chosen for −1.003 dB, the displayed text is the same "1.00 dB quieter", but that
attempt is graded "quieter" (`test_learn_just_beyond_one_db_is_quieter` passes).

**Scope.**
- The explanation and tradeoff contradict the grade for |Δ| ∈ [0.995, 1.0) dB.
- The comparison summary contradicts it for |Δ| ∈ [0.95, 1.0) dB.

The grade itself is computed honestly from the measured exact-frequency AC gain. Only the text
contradicts it.

**Fix.** Grade on the value that is displayed: apply the threshold to `round(delta, 2)`. Or show
enough digits near the threshold, e.g. "0.997 dB, under the 1 dB threshold". Keep the
comparison phrase consistent with whichever you choose.

### M2-02 (minor): floor values published as measured numbers

**Location.**
- `challenges/hypotheses.py` `_check`: `HypothesisCheck.observed` is the published value
  (−200 dB clamp), while `deviation` uses `Prediction.deviation`, which clamps both sides at
  −120 dB.
- `challenges/service.py` `_verification`: `max_gain_deviation_db` is computed from clamped
  values.
- Contracts: `ObservedTone`, `SweepObservation` and `VerificationPoint` have no field saying a
  reading is below the floor. Every consumer has to re-derive it.

**Reproduction (real ngspice and the test double give the same result).** High-pass case,
1 kΩ / 100 nF, `c1_disconnected`, `output_voltage_sweep`:
- The `no_fault` check is `{quantity: tone_gain_db, observed: -200.0, predicted: -11.487,
  deviation: 108.513}`, but |observed − predicted| = 188.5.
- If C1 is still open after a repair (e.g. R1 was replaced), `max_gain_deviation_db` is about
  162–200 dB.

The browser shows these numbers as levels:
- `apps/web/src/features/investigate/InvestigatePanel.tsx:486`: "observed −200.00 vs predicted
  −11.49 dB".
- `RepairSection.tsx:59`: `formatDb(max_gain_deviation_db)`.
- `CaseInstruments.tsx:60-64`: tone gain "−200.0 dB" and the conventional phase "0°".

Text summaries are correct ("no measurable output"). The agent views now render
`"below floor"` / `"undefined"` (checked by `test_agent_views_never_quote_a_floor…`).

**Fix.** This is a contract change for the lead. Add `measurable: bool` (or a nullable value)
to `ObservedTone`, sweep points, `VerificationPoint` and `HypothesisCheck`. Or publish
`observed` clamped at the −120 dB floor so the arithmetic holds. Then the UI renders "no
measurable output" and "phase undefined" instead of numbers.

The 0° phase convention is declared in `docs/contracts.md`, so the backend complies with the
contract as written. The weakness is the contract's lack of a machine-readable flag.

### M2-03 (minor): sub-floor solver noise published as a tone current

**Location.** `challenges/evaluator/service.py` `_observed_tones.level`. When the reading is
not measurable it zeroes the phase but not the amplitude. Real ngspice gives about 1e-19 A
round-off for the low-pass with C1 open.

**Reproduction.** Low-pass, 1 kΩ / 100 nF, `c1_disconnected`, `transient_capture` probe
`source_current`:
- Tone amplitudes are 1.11987e-17 mA and 5.66165e-18 mA.
- `waveform.observed` is all 0.0.
- The summary says "no measurable current".
- The `c1_disconnected` evidence check shows `observed 1.11987e-17` vs `predicted 0`.

High-pass with C1 open gives exact zeros.

**Impact.** This is not a side channel. The open capacitor is identified by the free symptom,
and R1 is nominal in that condition. It is noise presented with six-figure precision.

**Fix.** Publish amplitude 0.0 whenever the reading is at or below the floor, for hidden and
reference runs alike.

## Verified (passed, with numbers)

1. **High-pass physics, real ngspice, formulas written in the test.**
   - Setup: 40 AC runs (4 random designs plus 4 bound corners, each × {nominal, R×2, R×0.5,
     C×2, C×0.5}), 30 points each across fc/30–30·fc.
   - |H| and |I| matched within 1 % (absolute floors 1e-9 and 1e-12), and phases within 1°.
   - Observed worst errors (final run): |H| relative error 5.6e-16, phase 0.0°, |I| 4.4e-16,
     3.7e-13°.
   - The high-pass output and the delivered current both lead by 0–90°.
2. **Open capacitor in both families, real ngspice.**
   - AC on the case grid: the low-pass gives H = 1 and the high-pass H = 0 (|error| ≤ 1e-12),
     with I = 0 (≤ 1e-15 A).
   - Transient (50 ms): the low-pass |v_out − v_in| ≤ 1e-9 V, the high-pass |v_out| ≤ 1e-12 V,
     and |i| ≤ 1e-12 A.
   - Through the evaluator, published values follow the declared convention: the high-pass
     gain is exactly −200 dB with phase 0, and currents are 0 with phase 0.
3. **Isolation, 2 families × 6 conditions over HTTP (test double), 1 kΩ / 100 nF.**
   - Each flow covers create, get, mode, 3 instruments, an over-budget 409, an invalid-citation
     diagnosis 422, a diagnosis, a repair proposal, a repair, the workbench, the revision, and
     4 WAVs. It also builds 8 agent-view payloads from the same pre-close states.
   - Before close, none of these contained:
     - the seed, the generator version, a netlist token, a path, or ngspice text;
     - any spelling of the hidden value outside measured paths;
     - a forbidden agent key.
   - After close, the reveal holds the seed and the true condition (positive control).
4. **Differential structure (c1_disconnected included).** IDs, times, URLs, hidden-run
   measurements and the derived text were masked. After that, all 13 payloads per flow were
   byte-identical across the six conditions of each family, and so were:
   - the status codes;
   - the error bodies;
   - `available_actions`;
   - the reference (intended-design) numbers, which were left unmasked;
   - the WAV sizes and 44-byte headers;
   - the agent views.

   The open capacitor has exactly the same keys and list lengths. Its floor phase is a
   published 0.0 in the same field, not a different field.
5. **Derived text is a pure function of public data.** For every pre-close snapshot, the
   following were recomputed from the published numbers and public design and matched exactly:
   - the evidence (`hypotheses.assess`, including the publish rule);
   - every observation summary;
   - the diagnosis evidence check;
   - the repair summary.

   So the "no measurable output" templates carry nothing beyond the measurement. Repair
   semantics: replacing C1 restores every C1 condition including the open capacitor, and
   restores no R1 drift.
6. **Real-ngspice case structure.** Both families × {no_fault, r1_decreased, c1_decreased,
   c1_disconnected}, with symptom, both sweeps and the current capture:
   - The masked payloads are identical within each family.
   - Floors classify only true zeros: every non-open reading is above −120 dB or 1e-6 mA, and
     every reference reading is too.
   - Published hidden sweeps match the formulas within 1 % / 1°.
   - The agent views of the real data contain no "-200".
7. **Agent views and floors.** Open-capacitor readings reach the model as `"below floor"` and
   `"undefined"` (current sweep rows, current tones, high-pass symptom gains). The value "-200"
   never appears in any agent view.
8. **Generator fairness.**
   - Setup: 12,000 deterministic opaque seeds per family through `faults.generate`.
   - Condition marginals are about 2,000 each (chi-square goodness of fit vs `FAULT_PRIOR`,
     z < 4.5).
   - Design × condition independence: low-pass chi2 ≈ 445 (df 410), high-pass chi2 ≈ 350
     (df 340), both |z| < 1.3.
   - Every drawable design (83 low-pass, 69 high-pass) is valid for all conditions.
   - Family draw with a seeded RNG gives 50/50 within 4.5σ; hosted mode uses `SystemRandom`.
   - Seeds: 2,000 unique 32-hex values.
   - The in-process `Evaluator.create_case` stores exactly the generated case and returns only
     `{template_id, values}`.
9. **Learn.**
   - Prompts: across 44 designs (random plus near the bounds), a prompt is offered exactly when
     the change stays inside the bounds and the 20 Hz–20 kHz range. Prompts are deterministic
     and name the revision's own tones.
   - Grades: `delta_db` equals the recorded `ac_gain_db` difference of the two cited
     experiments (and the independent formula), and `observed` follows the threshold rule.
   - Evidence IDs are the base and new experiments. Both are readable in the session and 404
     from another session.
   - Failure stores nothing: a simulator failure during the changed measurement leaves the
     revision and experiment counts unchanged. A later success adds exactly 1 revision and 2
     experiments.
   - Cancellation stores nothing: an in-process cancellation during the changed measurement
     returns `cancelled` and stores no experiment.
   - Refusals that never simulate: a frozen case revision gets 409 `stale_revision` for
     prompts, attempts and suggestions. An out-of-range ×4 and the non-offered ×3 get 422, and
     so does a 50 Hz prediction on a low-pass. All of these made 0 simulator calls and stored
     nothing.
10. **Suggestions.** Checked by independent brute force over every E12 value, on 220 random
    revisions × goals × {R1, C1} (440 suggestions; 6,000 more in a scratch run):
    - The feasibility flag and the geometric-centre target equal an independent interval.
    - Whenever any valid E12 value passes, the chosen one passes.
    - When none passes, the chosen value is the best compromise by minimum margin.
    - Predicted gains and margins match within 1e-9 dB.
    - A tradeoff appears exactly when the chosen part fails, and its "misses by X dB (predicted
      attenuation Y dB" numbers match.
    - Texts start with "Prediction" and never say "measured".
    - For feasible goals where both E12 neighbours fail, the "exact value" named really meets
      every constraint.
11. **Evaluation harness.**
    - Refusal: `run_nemotron.main` returns 2 and writes nothing, and creates no scratch data,
      with a missing key, the placeholder key, a missing model or dev-fixture mode. The key is
      never echoed.
    - Baseline protocol: matches the README on 6 dev cases with the test double. The sequence
      is output sweep then current sweep, the supported unique conclusion, a nominal repair of
      the named component, and closing.
    - Committed held-out results: the file sha256 equals the current `heldout_v1.json`. Every
      result's seed, design and truth match the frozen set. An independent recount reproduces
      the README (24/24 correct, 0 abstentions, 0 unsupported certainty, 20 restored, 4 not
      needed, 0 infrastructure failures; mean 4.3 s, median 3.9 s, max 8.5 s). No cost or
      price fields, and no Nemotron result file.
    - Held-out disjointness (broader than the builders' AST scan): numeric literals and unit
      spellings, R and C within 3 lines, across all backend tests, frontend sources, e2e and
      the agent package. No held-out design appears. The only file containing both numbers is
      a netlist-compiler test (10000.0 in a netlist string, 33 nF elsewhere), which is not a
      held-out fixture.
12. **High-pass audio, real ngspice.**
    - Two explore experiments (1 kΩ/100 nF and 2.2 kΩ/47 nF): the PCM input clip carries
      0.3 V hum and 0.6 V tone (±1e-4 FS). The PCM output/input ratio at 50 Hz and 440 Hz
      equals the reported AC gain within 0.1 dB, which equals the formula within 0.05 dB.
    - `peak_fs` equals the PCM peak, there is no gain adjustment, and the hum disclosure is
      present in experiments and comparisons.
    - The comparison's reused clips reproduce `delta_db` within 0.1 dB.
    - Case symptom audio (high-pass, 3 conditions): Expected and Observed share one gain (PCM
      ratio = gain difference within 0.1 dB), and the disclosure is present.
    - High-pass with C1 open: the observed clip is exact digital silence (`peak_fs` 0), not
      normalized noise.

## Observations (not defects)

- The suggestion picks the best of the E12 neighbours of the geometric-centre target, as
  documented. It is not the global maximum-margin part. With a one-sided feasible interval
  (e.g. one "preserve" constraint) a farther E12 value can have more margin, and 1,738 of
  6,000 random scratch cases showed this. The choice always passes when any part passes.
- Sweep summary formatting can print "-0.0 dB" for the intended maximum (e.g. "ranges from
  -38.0 dB to -0.0 dB").
- Drawable low-pass nominal cutoffs start at 102.6 Hz, not at the 40 Hz bound of
  `CASE_NOMINAL_CUTOFF_RANGE_HZ`. The identifiability filter removes lower designs. The
  contract range is an outer bound, so this is consistent.
- The builders' disjointness test scans only AST numeric constants in 3 test directories. My
  broader scan found nothing more, so the claim holds today. String spellings ("4.7k") in other
  directories would not be caught by the builders' test.

## Could not verify

- Live Nemotron behaviour and the Nemotron-vs-baseline comparison (no API key).
- How the frontend renders M2-02 / M2-03 beyond reading the cited lines (lens D owns UI).
- Timing side channels for the open capacitor. Not measured; the open capacitor is identified
  by the free symptom anyway.
- The hosted or container build.
