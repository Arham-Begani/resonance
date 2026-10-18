# Review A — hidden-state isolation and leakage (milestone 1)

Reviewer: independent verification engineer (lens A). Date: 2026-10-16.
Tests: `services/api/tests/verification/test_v_isolation.py` (11 tests: 10 pass, 1 strict xfail
documenting A-01). Run with:

```
cd services/api
RESONANCE_REQUIRE_NGSPICE=1 uv run pytest tests/verification/test_v_isolation.py -rxX
```

Observed (final run): `10 passed, 1 xfailed in 85.22s`. Optional: `VERIF_A_SEED=<seed>` reproduces the
random-seed family.

**Limits of this review.** `NEBIUS_API_KEY` is not configured, so there was no live inference.
I drove agent behaviour with the TEST-ONLY scripted provider (`agent_testkit.ScriptedProvider`
over `httpx.MockTransport`), using the real `AgentService`, `CaseService`, and evaluator. HTTP
flows use the builders' analytical simulator double unless a test says ngspice. The tests
marked ngspice, and every scratch reproduction below, use real ngspice 47.

## Findings

| ID | Sev | Finding |
|---|---|---|
| A-01 | major | Full-precision voltage numbers show which component drifted (R1 or C1) for 15 of 83 drawable designs. This takes zero case experiments and bypasses the identifiability rule. |
| A-02 | minor | Error text says "even after one automatic retry" when no retry ran (non-retryable `SimulatorUnavailable`). |

### A-01 (major): numerical fingerprint of R1-vs-C1 drift in the free symptom

**What happens.** The standardized symptom is published as raw float64: observation 0, free and
voltage only. This covers `transient.tones[*].amplitude/gain_db/phase_deg`,
`audio.clips[observed].peak_fs`, and `evidence...checks[].observed`. The sweeps are published
the same way.

The ideal transfer function depends only on R·C. ngspice's results for (R/2, C) and (R, C/2) are
still not bit-identical when C/2 is 75 nF or 235 nF: they differ by about 1e-15 relative.
Example: tone gain −7.47963121690546 vs −7.479631216905477 dB. The likely cause is ngspice's
number parsing, which does not scale exactly by ½. ngspice is deterministic, and Explore runs the
same capture code on any public revision (same settings, netlist, and fit), so an Explore
experiment reproduces the symptom of the true variant bit for bit.

**Reproduction (HTTP only, real ngspice).** Script: `D:\tmp\verif-A\exploit_both.py`. It creates
a case, reads the free symptom, then explores R1/2 and C1/2 and compares the tone gains and
amplitudes exactly:

```
design 3300 ohm/4.7e-07 F hidden=r1_decreased: case budget used=0 evidence.remaining=['r1_decreased', 'c1_decreased'] exact-match {'R1/2': True, 'C1/2': False}
design 3300 ohm/4.7e-07 F hidden=c1_decreased: case budget used=0 evidence.remaining=['r1_decreased', 'c1_decreased'] exact-match {'R1/2': False, 'C1/2': True}
design 1000 ohm/1.5e-07 F hidden=r1_decreased: ... exact-match {'R1/2': True, 'C1/2': False}
design 1000 ohm/1.5e-07 F hidden=c1_decreased: ... exact-match {'R1/2': False, 'C1/2': True}
```

**Prevalence.** I surveyed every drawable design with `faults.nominal_designs()`:
- Transient survey: `D:\tmp\verif-A\survey_tran.py`, 653 s.
- AC survey: `survey_ac.py`.

Both show the same result: 15 of 166 voltage-equivalent pairs differ, and all 15 are R−/C− pairs
with C1 ∈ {150 nF, 470 nF}. R+/C+ was bit-identical everywhere. With the published 20 % priors,
about 15/83 × 0.4 ≈ 7 % of hosted cases can be solved this way.

The agent is not affected: its views round to 4 significant figures. The fingerprint is visible
only in raw browser JSON, or by running ngspice offline on the public netlist template.

**Requirement.** CLAUDE.md requires that output-only data cannot tell R from C. The contract says
nothing leaving the evaluator may carry hidden-dependent information beyond the declared
measured quantities.

**Test.** `test_voltage_only_payloads_cannot_distinguish_r_from_c_drift`, marked
`xfail(strict=True)`. Its failure:

```
At index 0 diff: (0.253611934264318, -7.47963121690546, -64.99568217121492) != (0.25361193426431755, -7.479631216905477, -64.99568217121472)
```

**Suggested fix.** This is a contract change for the lead. Quantize every measured value from a
hidden run at the evaluator boundary, and document the resolution in `docs/contracts.md`.
Examples: gains to 1e-6 dB, phases to 1e-5°, amplitudes and currents to 7 significant figures,
and `peak_fs` the same. These resolutions are far above solver noise and far below the declared
tolerances. Changing only Explore's settings is not enough, because offline ngspice reproduces
the numbers anyway. After the fix, the strict xfail turns into XPASS; then remove the marker.

### A-02 (minor): the failure message claims a retry that did not happen

**Location:**
- `challenges/service.py` `_record_failure`: "...even after one automatic retry. No experiment
  budget was used."
- `_record_repair_failure`: "...even after one automatic retry."

`_measure_with_retry` and `_repair_with_retry` do not retry non-retryable errors such as
`SimulatorUnavailable`.

**Reproduction.** `uv run python D:\tmp\verif-A\retry_wording.py`:

```
simulator attempts: 2 (1 attempt = 2 calls: hidden + reference)
status 503 message: The simulator could not complete this measurement, even after one automatic retry. ...
repair simulator attempts: 2 status 503 message: The simulator could not verify this repair, even after one automatic retry.
```

**Fix.** Mention the retry only when one actually ran.

## Verified (passes, with what was measured)

- **Agent mode end to end over HTTP**, five conditions × two families. One family is
  3300 Ω/470 nF created over HTTP with a patched `new_seed`. The other uses a fresh random seed
  through the private in-process `harness_create_case`.
  - The flow: create; run 1 blocked in inference, with 6 mutation attempts all 409 (4 of them
    `run_in_progress`); cancel; run 2 with 10 model calls and 9 tool calls (including an invalid
    call), three instruments, a fault diagnosis, and a repair proposal; user repair verification
    (and a second one, 409); workbench; 6 WAVs.
  - What was collected: every response, `AgentRun`, `/events/log`, raw SSE, the exact provider
    request bodies, and all log records from every logger at DEBUG.
  - Before close: no non-null `reveal`, no `seed` key, no netlist, path, or ngspice text, no API
    key, and no hidden value spelling or true label outside evidence-derived fields. Leaves
    identical in all five conditions are excused.
  - Skeletons (IDs, times, and declared measurements masked) were identical across conditions,
    for both browser payloads and model requests.
  - The simulator call sequence (kind, analysis, probes, source) was identical, and the hidden
    circuit ran at least 4 times per faulted case.
  - Artifact headers, sizes, and WAV chunks were identical (`fmt `/`data` only).
  - `distinguishing_instruments` stayed null while observing with budget left.
  - After close, the reveal holds the seed (positive control). Mode, experiment, diagnosis,
    repair-proposal, repair, and both agent-run kinds all return 409, with 0 model calls.
- **Agent tool executors called directly**, five conditions, 470 Ω/100 nF:
  - Every output stays within an explicit key allow-list copied from the contract text in
    `agent/views.py`.
  - The model never receives `reveal`, `distinguishing_instruments`, `waveform`, `audio`,
    `artifact_id`, `url`, `settings`, or `assessments`, and no list is longer than 31.
  - All 6 tools refuse with a fatal `conflict` inside the close-time run canceller (while the
    case is closing) and again after the reveal.
  - `case_view` and `evidence_view` raise on a closed case.
  - A cross-case `read_observation` returns the same result as an unknown ID.
  - Tool names are exactly 6 for INVESTIGATE and 2 for HINT, and no tool takes case, session,
    path, query, code, or value arguments.
- **Leaky simulator double.** Its exception text contains the values, netlist, path, and ngspice
  output. I triggered `failed`, `timeout`, `unavailable`, `RuntimeError`, and non-finite data
  during creation, experiments, and repair, across five conditions.
  - Error bodies were byte-identical across conditions and generic.
  - A failed creation leaves no hidden row.
  - Three failures put the case in `failed` with budget 0.
  - No marker reached any payload or log.
- **Access control.** I compared the owner's ID, an unknown ID, and 2–4 malformed IDs on each of
  14 endpoints: case GET and 7 POSTs, agent-run status, JSON log, SSE, and cancel, artifacts, and
  revisions. Every one returned 404 with identical bodies, with 0 simulator calls and 0 model
  calls.
- **Explore and frozen revisions.**
  - The frozen case revision is refused by explore experiments, revision parenting, and
    set-current (409 `stale_revision`), and the explore agent returns 404. All of these made
    0 simulator calls, and the revision stays frozen after close.
  - Warm-cache attempt: I explored the exact hidden circuit first. The case sweep still made
    2 calls and ran the hidden circuit itself.
  - Read-only endpoints never simulate.
- **Creation hardening.** `seed`, `fault`, `hypothesis_id`, `nominal`, `values`, `rc_highpass`,
  and `multiplier` → 422 with no simulation. Query `?seed=` is ignored; the revealed seed is a
  fresh 32-hex value. `CaseCreateRequest` has only `template_id`, and OpenAPI has no harness
  route.
- **Static routes.** Encoded `..` probes against the SPA route and `/assets` reach the handler
  decoded (`/../data/evaluator.sqlite3`) and are refused. No SQLite bytes are ever served.
- **ngspice job directories.** None remain after a success or after a cancelled hidden transient.
  The data directory holds only the declared stores, and artifacts are `art_*.bin` RIFF files.
  Real simulation-layer DEBUG logs contain no values, netlists, or paths.
- **Timing proxy.** Real ngspice gave 140,012 transient timepoints for all 50 runs (10 designs ×
  5 hidden variants). Wall time varied from 1.04 to 2.68 s with no fault-correlated pattern.
- **The tests themselves.** Planted defects make them fail:
  - `_sanitize` pass-through
  - waveform added to the agent view
  - `distinguishing_instruments` added to `case_view`
  - a no-fault reference shortcut in the evaluator
  - hidden R1 appended to error text
  - a 404 message that reveals other sessions' cases

  I used a scratch plugin (`D:\tmp\verif-A\mut\mut_plugin.py`); application code was not
  modified.

## Observations (not defects)

- A repeated `client_request_id` with the same body after close returns the stored 201
  response, now including the reveal. No simulation runs. This is consistent with the
  idempotency rule, and the reveal is public by then.
- After a cancelled experiment returns, the cancelled ngspice worker thread finishes killing
  ngspice and removing its job directory shortly afterwards, because `asyncio.to_thread` is not
  awaited on cancellation. Cleanup completed within the test's 10 s bound in 3 of 3 runs.
- ngspice DEBUG logs record run durations. They are not exposed through the UI, and the run
  length does not depend on hidden values (see the timing proxy above).

## Could not verify

- Live Nemotron behaviour: no API key.
- Timing statistics in the deployment container.
- How the frontend renders or stores full-precision values (frontend not reviewed in this lens).
- The hosted build.
