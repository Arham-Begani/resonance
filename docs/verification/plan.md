# Verification plan: adversarial checks against the integrated system

Owner: independent verification engineer. Tests live in `services/api/tests/verification/**`
(pytest) and `apps/web/e2e/**` (browser). This plan is published early so builders can design for
these checks. Findings referenced as V-xx are in `contract-review.md`.

## Hooks builders should provide (test-only, never routed over HTTP)

1. An in-process evaluator entry point that creates a case from an explicit `(seed)` or
   `(nominal design, fault)`, used by the evaluation harness and these tests.
2. A `Simulator` test double injectable via `app.dependency_overrides[get_simulator]` that can be
   scripted to raise `SimulationFailed`/`SimulationTimeout` N times, block until cancelled, or delay.
3. A recorder for the **exact** strings sent to the model (system prompt, tool schemas, tool
   results), plus a scripted OpenAI-compatible client double for deterministic tool-call sequences.
   The double must be labeled as a test double and is never shown as AI.
4. Log capture: everything goes through `logging`, so `caplog` sees all output, including
   simulation-layer output.

## 1. Hidden-state leakage

**1a. Payload scan.** Run all 5 hypotheses across ≥ 3 nominal designs (low, mid, and high cutoff
in the declared range). For each case, drive create → mode → each instrument → diagnosis → repair →
close through the ASGI app, and also through the agent tool layer. Collect:
- every JSON response and error body;
- SSE events and the `/events/log` output;
- WAV bytes and headers;
- `Content-Length` and `Cache-Control`;
- the strings sent to the model;
- captured logs;
- the contents of `.data/jobs` after each run.

Before close, search all of it for:
- actual R/C values in every representation: SI float, `repr`, scientific, engineering text such
  as `2 kΩ`, `2k`, `200 nF`, `0.2 µF`, ratios like `x2`/`2×`;
- hidden fault labels used as truth;
- the seed;
- netlist tokens (`.tran`, `.ac`, `.control`, `R1 in out`, `SIN(`);
- file paths (`.data`, `jobs`, drive letters);
- raw ngspice text (`Circuit:`, `Note:`, `Warning:`);
- `generator_version`.

After close, these may appear only inside `reveal`.

**1b. Differential structure test** (V-06). Take the same nominal design under each fault. Replace
every random ID and every declared measured number with placeholders, then require byte-identical
payloads: same keys, list lengths, `available_actions`, status sequence, error texts, artifact sizes,
WAV headers, and summary templates.

**1c. Agent surface.** Enumerate the registered tool schemas for EXPLORE, INVESTIGATE, and HINT.
Assert there are no file, HTTP, shell, code, or database tools, and that no tool takes a `case_id`
argument. Assert the INVESTIGATE tool outputs match `AgentCaseView`/`AgentObservationView` exactly
(V-01) and never contain `reveal`, waveforms, or artifact IDs.

**1d. Seeds and generator** (V-05). Hosted seeds are ≥ 128-bit. Across N ≥ 2000 generated cases,
a chi-square test checks that nominal values are independent of the fault. There is no
per-session round-robin. `POST /api/cases` with `seed`/`fault`/extra fields returns 422.

## 2. Timing side channels (V-04)
- Per fault type, compute median and IQR of the experiment response time and of the
  `created_at`/`updated_at` deltas (n ≥ 15 each). Medians must not separate beyond the run-to-run
  noise.
- Warm-cache attack: simulate the four hypothesis circuits in Explore, then run a case instrument.
  Latency must be unchanged.
- No-fault cases must not be faster.

## 3. IDs, scope, and stale state
- Another session's case, observation, artifact, run, revision, experiment, or proposal returns
  404 with an identical body; malformed IDs behave the same.
- Cross-case: citing or reading an observation from case A while working in case B is rejected,
  including for the agent `read_observation` tool.
- Frozen intended revision used as a parent returns 409 `stale_revision`. An edit during a case
  cannot alter the case.
- Explore endpoints never accept case or observation IDs.

## 4. Budget and state-machine bypass (V-02, V-11)
- Remaining = 1: send 5 concurrent experiment requests with distinct keys. Exactly one succeeds and
  `used` never exceeds `total`.
- Same key + same body returns the identical response with no extra budget used. Same key +
  different body returns 409. A missing key returns 422 (once the key is required).
- Agent run plus a concurrent manual experiment, diagnosis, or repair returns 409
  `run_in_progress`. Parallel tool calls from the scripted model double are executed sequentially.
- Validation errors (bad node, unknown instrument, invalid diagnosis shape) never consume budget,
  the diagnosis, or the repair.
- Infrastructure: one injected failure leads to an automatic retry and an uncounted success. Two
  failures record a failed observation (uncounted) and stop the agent run as failed.
  `CASE_MAX_INFRA_FAILURES` leads to `failed`, which can only be closed.
- Cancellation during simulation leaves the observation uncounted with no partial data. A client
  disconnect cancels a manual experiment.
- Action × state matrix: every `CaseAction` in every `CaseStatus` returns the documented code.
  Diagnosis twice, repair twice, repair before diagnosis, experiments after diagnosis, and close
  twice are all refused.

## 5. Reveal timing (V-03, V-13, V-14)
- `reveal` is null in every payload before close, including event logs.
- `distinguishing_instruments` follows the single declared rule.
- Close during an agent run that is blocked in a model call: the agent task has terminated before
  `reveal` is written, no tool executes after close, and no event has `at` later than `revealed_at`.
- Post-close experiments and agent runs follow the declared replay rule. The outcome is frozen.

## 6. Physics (independent of the project code)
- Low-pass (and high-pass when offered): ≥ 5 value sets across the bounds. ngspice gain within 1 %
  of analytical, phase within 1° (wrapped difference, absolute floor near 0), on the explore and
  case grids.
- R/C equivalence: R+ vs C+ voltage curves are identical, the current ratio is 2.
- Delivered-current sign and phase lie in (0°, 90°) for the low-pass.
- Joint cutoff bounds are enforced at revision creation (V-07).

## 7. Audio honesty
- Tone amplitudes fitted from the decoded WAV and from the API numbers agree with AC within 0.5 dB.
- The ratio of clip peaks equals the ratio of voltages × the shared gain (proves there is no
  per-clip normalization).
- `gain_adjustment_db` is applied to every clip in a comparison and disclosed in `note`.
- WAV is a standard PCM header with no metadata chunks, and the same size for every case.
- Fades only on playback: the fit window is unaffected.
- Clipping is detected on a synthetic over-range input.
- Muted flow: the task is completable with audio disabled (e2e).

## 8. Evidence engine and diagnosis
- A reference re-implementation of hypothesis status (from the published tolerances) agrees with
  the service on random observation sets.
- Ambiguity: withhold the current sweep. `remaining = {R+, C+}`, `fault` is marked `ambiguous`
  (unsupported certainty), and `insufficient_evidence` is marked `consistent_abstention`.
- No-fault control: symptom only leads to `{no_fault}` supported.
- Premature abstention is flagged.
- Citations are validated against the quantity vocabulary (V-10).

## 9. Repair (V-09)
- Through the evaluator test hook, only the proposed component changes in the hidden spec.
- A correct repair is restored. A wrong-component repair is not restored.
- A compensating value (if free values remain) is reported per the declared rule.
- `/repair` verifies exactly the recorded proposal.

## 10. Agent honesty
- Missing key returns 503 `ai_unavailable` while every manual path (explore, case, diagnosis,
  repair) still works.
- Dev fixture is labeled at the run and event level.
- Caps (model calls, tool calls, tokens, wall time, retries) produce `limit_reached`.
- Usage numbers come from provider responses.
- No fake progress events: every `status` event corresponds to a real stage.

## 11. UI end-to-end and accessibility (`apps/web/e2e`)
- One keyboard-only Playwright run: edit value → simulate → compare → start case → choose
  instruments → diagnose → repair → close.
- Visible focus at every step, logical order, no traps, focus preserved after edits, polite live
  regions (no waveform or token spam), result tables and graph summaries present, reduced-motion
  respected, narrow-viewport tabs preserve the workflow.
- State coverage: loading, empty, invalid value, timeout, cancelled, missing key, simulator error,
  ambiguous diagnosis, failed repair, success. Prior results are kept after a failure.
- Manual screen-reader pass (NVDA on this host if available). Otherwise I will report that
  accessibility is unvalidated by AT users.

## 12. Operational
- `.data/jobs` is empty after success, timeout, and cancel (Windows file locks included).
- Artifact eviction does not remove open-case clips (V-19).
- Measure transient latency in the deployment container (V-12).
- The hosted build is tested from a fresh browser profile.
