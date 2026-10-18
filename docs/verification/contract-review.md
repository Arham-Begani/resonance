# Contract review — milestone 1 (low-pass workflow)

Reviewer: independent verification engineer. Date: 2026-10-15.
Status of the code at review time: contracts and route signatures exist; every feature route body is
a `not_implemented` stub and `create_simulator` returns a placeholder. These findings are about
**what the contracts allow or leave undefined**. None of them are observed implementation bugs yet.

Inputs reviewed: `CLAUDE.md`, `idea.md`, `docs/contracts.md`, `services/api/resonance/contracts/*.py`,
`api/*.py`, `config.py`, `store/*.py`, `explore/goals.py`, `agent/status.py`, `app.py`.
Reproduction: `cd services/api && uv run python ../../docs/verification/scripts/contract_numerics.py`
(analytical only), plus the ngspice runs listed in the appendix.

Severity: **blocker** = change the contract before builders go further; **major** = must be
resolved in milestone 1, or the central requirement, evaluation honesty, or the demo is at risk;
**minor** = fix when convenient. Ambiguities are included where builders would otherwise implement
different behavior.

## Summary

| ID | Sev | Finding |
|---|---|---|
| V-01 | blocker | No contract for what the agent sees. `PublicCase` contains `reveal`, raw waveforms, and evaluator assessments |
| V-02 | blocker | No per-case serialization. Budget, diagnosis-once, repair-once, and close are race-prone. `client_request_id` is optional |
| V-03 | major | Reveal ordering: close must cancel **and await** the agent; agent tools must refuse after close |
| V-04 | major | A shared simulation cache or "hidden == intended" shortcut becomes a timing oracle that bypasses R-vs-C identifiability |
| V-05 | major | Seed entropy and generator independence are unspecified. Public nominal values can leak the fault |
| V-06 | major | Everything in `PublicCase` before close, including `available_actions` and status transitions, must depend only on public state |
| V-07 | major | No joint R·C (cutoff) bound. The settle constant and the case design range are unsound without one |
| V-08 | major | `transient_capture` is uninformative: `in` is the ideal source and `out` duplicates the symptom. The challenge is solvable in one experiment |
| V-09 | major | Repair semantics: `/repair` re-takes a body, and free values let a compensating wrong-component repair pass as "restored" |
| V-10 | major | Quantity names for citations, `EvidenceRef`, and `HypothesisCheck` are free strings. Evidence validation will diverge |
| V-11 | major | State machine underspecified: `unresolved`, mode switching, a new case while one is open, actions per state |
| V-12 | major | Transient cost: 280,010 timepoints take 2.9–8.0 s per run. Timeouts on a small container would turn into "failed" cases |
| V-13 | minor | `distinguishing_instruments` timing contradicts itself. `statement` and hints can steer anyway |
| V-14 | minor | Replay after reveal is "labeled or refused". Replays need exclusion rules, and agent runs need a source label |
| V-15 | minor | Cross-field request rules are not in the models (diagnosis, node). A 422 must not use up the single diagnosis |
| V-16 | minor | `CaseOutcome` and evaluation inputs lack definitions and fields (baseline actor, hints, mixed control, failed case) |
| V-17 | minor | Observation payload gaps: no settings echo, no expected tone phase, unbounded waveform window |
| V-18 | minor | Non-finite values (−inf dB, NaN) would become a 500 error only for some hidden states. Declare floors |
| V-19 | minor | Operational: global artifact eviction across sessions, single-worker requirement, two-database atomicity |
| V-20 | minor | Small items: grid matching tolerance, frozen-revision lifecycle, current-revision pointer, manual cancel, errors map |

What I verified as **correct**: the identifiability claim, the current sign convention, the 31-point
grid, clip-free headroom, settling sufficiency for the tone fit, AC-instrument tolerance margins, and
that the timestep count does not depend on hidden values (see "Verified claims" below).

---

## Blockers

### V-01 (blocker): the agent-visible case payload is undefined
**Location:** `docs/contracts.md` "Agent rules" (INVESTIGATE: "get case, … read observation");
`contracts/investigation.py` `PublicCase` (L392-410), `TransientObservation`/`ObservedWaveform`
(L133-147).
**Scenario:** the AI engineer's natural implementation of `get_case` serializes `PublicCase`.
1. After close (or in any hint run on a closed case), `reveal` contains the fault, actual values, and
   seed. Any tool call that runs after close hands the answer to the model, and the follow-up text
   then looks like a model decision.
2. Observation 0 alone carries `ObservedWaveform` with three float arrays (window size unbounded,
   see V-17). At 480 samples × 3 series it is roughly 30 kB of JSON, about 10k tokens per read, so
   `AGENT_MAX_TOTAL_TOKENS = 60_000` runs out after a few `get_case` calls.
3. `evidence.assessments`/`remaining` from the deterministic evaluator are given to the agent.
   Diagnosis then reduces to reading `remaining`, and the Nemotron-vs-baseline comparison measures
   only experiment choice. That is legitimate, but it must be a declared decision.

The leakage scan (plan §1) also needs an exact, enumerable agent surface.

**Proposed change:** add `contracts/agent_views.py` with `AgentCaseView` and `AgentObservationView`.
They should include: case status, budget, intended revision summary (IDs, nominal values,
topology text), instruments, hypotheses, and per-observation compact numbers (sweeps rounded to 4
significant figures; tone levels; no waveform arrays, audio, or artifact IDs). They must never
include `reveal`, `repair.audio`, or anything not in this list. Decide and document whether
`evidence` (assessments) is included. My recommendation: include `remaining` only after the agent
calls an explicit `assess_evidence` tool, so its use is visible in the event log and reported in
evaluation. Agent tools build only these views, never `PublicCase`.

### V-02 (blocker): per-case serialization and atomic budget reservation
**Location:** `docs/contracts.md` "Investigation rules" (budget, idempotency, 409 while agent owns);
`investigation.py` `CaseExperimentRequest.client_request_id` (L96-102, optional);
`store/db.py` `DocumentStore.put` (L967-980, last-writer-wins upsert, no version check).
**Scenarios:**
- (a) Budget remaining = 1. Two manual requests with different keys, or two agent parallel tool
  calls, both read `remaining=1` and both simulate for 3–8 s (V-12). Both commit, so `used=4`.
- (b) A double click or network retry without `client_request_id` consumes budget twice. The
  contract allows the key to be omitted.
- (c) The same key reused with a different instrument returns the earlier result (or runs a second
  experiment). Undefined.
- (d) A manual experiment is in flight, and then `start_agent` succeeds because the agent-owns check
  ran first. Or the user submits a diagnosis while the experiment is in flight, and the observation
  lands after the diagnosis, so `EvidenceCheck` was computed on stale evidence.
- (e) Agent `submit_diagnosis` races a manual `POST /diagnosis`. Both pass "no diagnosis yet", so
  "submitted once" is violated.
- (f) Close runs while an experiment is in flight. The observation is committed after reveal with
  `replay=false`.

**Proposed change** (contract text, plus one field):
1. Exactly one mutating operation per case at a time: mode, experiment, diagnosis,
   repair-proposal, repair, agent start. Implement with an in-process per-case `asyncio.Lock` and a
   persisted `in_flight` marker. A second mutating request returns 409 `conflict`.
2. Budget is reserved before simulating and released if the run fails or is cancelled. Commit the
   observation and the budget in one step, before responding.
3. Make `client_request_id` **required**. Scope it to (case, key). Same key + same body returns the
   stored response. Same key + different body returns 409. A key whose request is still in flight
   returns 409 `conflict` (retryable).
4. While an agent run is active, **all** mutating case endpoints except `close` and `cancel` return
   409 `run_in_progress`, not only experiments. The agent loop executes tool calls sequentially
   (`parallel_tool_calls=false`).
5. Close is serialized behind the lock: it cancels in-flight work and awaits it (see V-03).

---

## Major

### V-03 (major): reveal ordering against the agent loop
**Location:** `docs/contracts.md` L74 ("Closing first stops any agent run"); `routes_cases.close_case`;
`AgentRun`/`cancel`.
**Scenario:** close calls `token.cancel()` and immediately writes `reveal`. The agent task is still
awaiting a Nemotron completion. When the completion returns, the loop executes one more tool call
(`get_case` now has `reveal`) or emits a `message`/`diagnosis` event after reveal. Cooperative
cancellation only takes effect at the next check.
**Proposed change:** specify the order: (1) set the case to `closing`, which refuses new tool
executions; (2) cancel the token; (3) await the agent task's termination, with a bounded timeout
(e.g. 10 s), after which the run is marked `cancelled`; (4) write the reveal. Every INVESTIGATE/HINT
tool checks the case status at execution time and refuses when it is `closing`/`closed`. Events with
`at` later than the reveal time must not exist for that run. Agent runs on a closed case are
refused (see V-14).

### V-04 (major): simulation caching and shortcuts as a timing oracle
**Location:** `docs/contracts.md` L65-67 (forbids content-derived cache *keys/names* in outputs, but
says nothing about internal result caches or latency); `contracts/internal.py` `Simulator`.
**Scenario:** a transient costs 3–8 s (V-12), so a builder adds an LRU cache keyed by
`CircuitSpec`. A learner simulates the four hypothesis circuits in Explore (public revisions with
R1 or C1 doubled or halved), then runs `transient_capture`/`output_voltage_sweep` on the case. A
fast response means the hidden circuit equals one of the warmed specs. Because voltage already
narrows to {R+, C+}, the timing distinguishes R from C **without** the current measurement, which
defeats the identifiability rule CLAUDE.md calls central. The same applies to a "hidden equals
intended, reuse the reference" shortcut, which leaks `no_fault`.
**Proposed change:** add a rule. Hidden-circuit runs never read or write any cache and are never
short-circuited by equality with another spec. The only permitted reuse is the per-case
intended-design reference, keyed by case ID. Every instrument always performs the same simulation
work (hidden run + reference run or cached reference) regardless of hidden values. Verification
will time each fault type (plan §2).

### V-05 (major): seed entropy and generator independence
**Location:** `investigation.py` `CaseReveal.seed: str` (L378); `docs/contracts.md` L70-73.
**Scenarios:**
- (a) Small seed space (e.g. `randint(0, 2**32)` or sequential eval seeds reused by the hosted app).
  Nominal values are public and derived from the seed, so brute-forcing seeds that reproduce the
  public nominal R/C reveals the fault.
- (b) Conditional generation. Rejection sampling such as "redraw nominal values if the *drawn
  fault's* variant leaves the allowed range" makes the public nominal values informative about the
  fault (e.g. near the lower bound only `*_decreased` faults survive).
- (c) Balanced round-robin per session ("each fault once") makes the fifth case predictable after
  four reveals.

**Proposed change:** hosted cases use `secrets.token_hex(16)` (≥128-bit) seeds. Nominal design and
fault are drawn independently, with nominal-range checks applied against **all** hypotheses (every
variant must be valid for every nominal design that can be drawn). Hosted sampling is i.i.d. Balanced
sets are allowed only in the private evaluation harness, with seeds never reused in hosted mode.
Document fault prior probabilities publicly (including the no-fault share).

### V-06 (major): every pre-close field must depend only on public state
**Location:** `PublicCase.available_actions`, `status`, `updated_at` (L392-410);
`CaseOutcome.functional_recovery="not_needed"` (L371).
**Scenario:** an implementation hides `propose_repair` (or jumps straight to `closed`) when the
hidden truth is `no_fault`, because "there is nothing to repair". Or it computes `not_needed` early
and changes the state machine. Then `available_actions` or the status leaks the answer right after
diagnosis.
**Proposed change:** add a rule. Before close, `status`, `available_actions`, `budget`, `evidence`,
`summary` strings, and transitions are computed only from public data: intended design, recorded
observations, the submission, the repair proposal, and the measured verification. Only `reveal`
(after close) may read the truth. Verification will run a differential test: the same nominal
design under each fault must give structurally identical payloads (plan §1b).

### V-07 (major): no joint cutoff bound; settle and case range rest on it
**Location:** `policy.py` L24-27 (`TRANSIENT_SETTLE_S = 0.2`, "≥ 7 τ for the slowest allowed
circuit, including faulted variants"); `circuits.py` `ValueBounds` (per-component only, L46-49).
**Evidence:** the 7τ rule gives τ_max = 28.57 ms, so fc ≥ 5.57 Hz for the slowest *faulted*
circuit and nominal case fc ≥ 11.14 Hz. Per-component bounds of the usual kind (R up to 100 kΩ,
C up to 1 µF) allow fc = 1.6 Hz. Analytically the tone fit is still accurate there (error < 0.001 dB
even at fc = 1 Hz, because the fit spans an integer number of periods), so the settle constant is
**not** unsafe for the fit. However, the policy comment's claim is false unless R·C is bounded, and
the case generator range is undeclared. Symptom separability (tone gains at 440 Hz and 4 kHz, 0.5 dB):
- Nominal fc in [11.2, 6000] Hz: minimum separation of non-equivalent hypotheses is 2.28× tolerance.
- Nominal fc in [100, 5000] Hz: minimum separation is 3.01×.
- Around 7–31 kHz: pairs sit at 0.5–2× tolerance, so transient-fit error could flip consistency.

**Proposed change:** add `MIN_CUTOFF_HZ`/`MAX_CUTOFF_HZ` (explore revisions, validated at revision
creation as a 422 naming the edited value) and `CASE_NOMINAL_CUTOFF_RANGE_HZ` (suggest (20, 5000)
so the 2× variant is ≥ 10 Hz and every symptom pair is ≥ 3× tolerance apart). Fix the policy comment
so the 7τ claim is stated over that range. Add a generator test: for every drawable nominal design,
every pair's deviation/tolerance ratio on every instrument is < 0.5 (equivalent) or > 2 (separable).

### V-08 (major, experiment design): `transient_capture` adds no information; one experiment always suffices
**Location:** `policy.py` `CASE_TRANSIENT_NODES = ("in", "out")` (L41); `docs/contracts.md` L84-92.
**Evidence** (`contract_numerics.py` §1; groups are hypotheses that a given instrument cannot
separate):

| nominal fc | symptom (tone gains) | voltage sweep | current sweep |
|---|---|---|---|
| 10 Hz – 8 kHz | {N} {R+,C+} {R−,C−} | {N} {R+,C+} {R−,C−} (same) | all 5 separated |
| 10 kHz | {N,R−,C−} {R+,C+} | {N} {R+,C+} {R−,C−} | all 5 separated |
| ≥ 20 kHz | one group | {N} {R+,C+} {R−,C−} | all 5 separated |

- `in` is the node of an ideal voltage source, so V(in) is identical under every hypothesis. A
  transient capture there carries zero information by construction and spends one experiment.
- `out` repeats the standardized symptom exactly (same capture). For an LTI circuit it is also
  strictly less informative than the voltage sweep.
- For the design range proposed in V-07, the voltage sweep adds nothing beyond the symptom.
  `source_current_sweep` alone separates all five hypotheses (deviation/tolerance ≥ 6.98 over
  20 Hz–8 kHz, ≥ 13.5 over 50 Hz–8 kHz). An
  optimal investigator therefore always needs exactly one experiment. Any fixed baseline that
  includes the current sweep scores 100 % exact identification. The 3-experiment budget never
  binds, and the Nemotron-vs-baseline comparison can only differ in efficiency and in calibration
  (abstention and unsupported certainty).

**Proposed change** (choose one, and state it in the evaluation write-up):
- (A, recommended) Replace the node choice with a probe choice, `transient_capture` probe ∈
  {`v(out)`, `i(V1)`}. This makes the transient instrument meaningful (two-tone source-current
  amplitudes separate R from C: current ratio C+/R+ = 2.000 at every frequency) and satisfies
  CLAUDE.md's "transient voltage/current measurements".
- (B) Keep `in` deliberately as a teaching example. `InstrumentSpec.description` must then say "V(in)
  is set by the ideal source and is the same for every explanation", and evaluation counts it as a
  wasted experiment.

Either way, document that milestone-1 cases are solvable in one experiment. Do not present 100 %
identification as an achievement. The real difficulty arrives with `c1_disconnected` and the
high-pass family.

### V-09 (major): repair semantics
**Location:** `routes_cases.py` `verify_repair(body: RepairProposalInput)` (L409-418) alongside
`propose_repair(body: RepairProposalInput)`; `investigation.py` `RepairProposalInput.value`
(L305-308); `VerificationCriteria` (voltage gain/phase only, L321-325).
**Scenarios:**
- (a) `/repair` takes a second body, so the verified value can differ from the recorded proposal.
  Builders will disagree on which one wins.
- (b) Truth is C1 ×2, the diagnosis is R1 ×2, and the proposed repair is R1 = 0.5 × nominal. Then
  R′C′ = RC, every voltage point passes, and `functional_recovery = true` with the wrong component.
  Source current is still 2× nominal (6 dB off) at every frequency. This inflates "verified repair"
  for both Nemotron and the baseline, and teaches a misleading lesson.

**Proposed change:** `/repair` takes no body (or `{repair_id}`) and verifies the recorded proposal.
Proposals may be replaced until verification, which happens once. Preferred: a repair is "replace
component X with a part of its intended nominal value". `RepairProposalInput = {component_id,
rationale}`, and the value comes from the frozen intended revision. If free values are kept,
verification must also check source current on the same grid (±3 %) and report
`impedance_recovery` separately. Also state whether a repair is allowed after `no_fault` or
`insufficient_evidence`, and what the agent may do (`propose` only, or also `verify`; CLAUDE.md
says "request verification after submission").

### V-10 (major): closed vocabulary for evidence quantities
**Location:** `common.py` `EvidenceRef.quantity: str` (L106); `investigation.py`
`EvidenceCitation.quantity: str` (L254), `HypothesisCheck.quantity` (L205),
`SweepObservation.quantity: Literal["output_gain","source_current"]` with a separate unit (L111-114).
**Scenario:** the agent cites `output_gain`, the evaluator expects `output_gain_db`, and the UI
renders `gain_db`. Citations fail validation, or pass unchecked. "Every measured numerical claim
must reference a result ID and relevant quantity" becomes unenforceable.
**Proposed change:** `QuantityName = Literal["output_gain_db","output_phase_deg","source_current_ma",
"source_current_phase_deg","tone_gain_db","tone_phase_deg","tone_amplitude_v"]`, used in all three
places. Validation rule: `result_id` belongs to this case (or session for explore) and has status
`succeeded`. The quantity exists in that observation's kind. `frequency_hz`, if given, matches a
returned frequency within 0.1 %.

### V-11 (major): state machine underspecified
**Location:** `CaseStatus` docstring (L36-46); `CaseAction`; routes docstring.
**Undefined:** what enters `unresolved` (an `insufficient_evidence` submission? budget spent?).
Whether experiments are allowed in `ready`. Whether the mode can change (AI unavailable after
choosing `agent`; switching after a cancelled run, which mixes agent and user control and
contaminates evaluation). Whether `hint` runs are allowed in `agent` mode, whether they block
manual experiments, and whether they are counted. Whether close is allowed from every state.
What happens to an open case when `POST /cases` is called again: it is abandoned but stays open and
unrevealed, and it may be resumable. Symptom failure at creation.
**Proposed change:** add a transition table (state × action → next state | error code) to
`docs/contracts.md`. My suggestion:
- `POST /cases` with an open case → 409 (or auto-close and reveal, explicitly).
- Experiments only in `observing`.
- Mode is fixed once chosen, except `agent` → `guide` when no run has executed an experiment.
- Hints allowed in `guide` only, recorded and counted (`hints_used`).
- `insufficient_evidence` → `unresolved`. A repair is still allowed from `unresolved`.
- Close is allowed from any non-closed state.
- If the symptom fails, the case is not created (the evaluator state is discarded) and the request
  returns 502/503.

### V-12 (major, operational): transient cost and timeouts
**Location:** `policy.py` `TRANSIENT_MAX_STEP_S = 5e-6`, `AUDIO_CLIP_S`, `TRANSIENT_SETTLE_S`;
`config.py` `ngspice_timeout_s = 20`, `ngspice_max_concurrency = 2`.
**Evidence (this host, 4 cores shared with other specialists):** a 1.4 s transient with 5 µs max
step produces 280,010 points and takes 2.9–8.0 s wall time per run. The same run with a 10 µs max
step takes 1.9 s (140,011 points). The 31-point AC sweep takes 0.35 s. Tone-fit accuracy against
analytical, after linear resampling to 48 kHz over [0.2, 1.4] s:

| circuit | 5 µs max step | 10 µs max step |
|---|---|---|
| fc = 1.59 kHz, 4 kHz tone | −0.004 dB | −0.042 dB |
| fc = 50 Hz, 4 kHz tone at −38 dB | −0.008 dB | −0.051 dB |

The symptom needs two transients (hidden + intended), and so do `transient_capture` and repair.
On a 1–2 vCPU deployment container with two concurrent jobs, runs could exceed 20 s, which counts
as an infrastructure failure. After `CASE_MAX_INFRA_FAILURES` the case becomes `failed`, and judges
see a broken case.
**Proposed change:** the audio owner evaluates `TRANSIENT_MAX_STEP_S = 1e-5` (measured error
≤ 0.051 dB, against a 0.5 dB budget), caches the intended-design reference per case (allowed by
V-04), and measures in the deployment container before fixing the timeout. Report latency honestly
in the UI (genuine stage status only).

---

## Minor

**V-13 `distinguishing_instruments` timing and steering.** `docs/contracts.md` L75-76 says it is
withheld in `ready`/`observing`. The field docstring (investigation.py L235-237) says it is published
when "the budget is spent", which is still `observing`. Separately, `EvidenceState.statement`,
`Observation.summary`, and hint runs can name the resolving instrument (idea.md's example sentence
does exactly that). Note that this is not hidden state: it is computable from public predictions.
*Change:* pick one rule. Suggested: published when `remaining_budget == 0` or status ∉
{ready, observing}. While it is withheld, `statement` and `summary` must not name instruments, and
only an explicit, recorded hint may suggest one.

**V-14 Replay after reveal and run labeling.** "labeled `replay=true` (or refused)" invites
divergent behavior, and the contract does not say whether replays alter `evidence`, `budget`, or
`outcome`. `AgentRun` has no field distinguishing Nebius inference from `dev_fixture`, or a
re-rendered past run from a live one. *Change:* in M1, refuse experiments, agent runs, diagnosis,
and repair on closed cases (409). The outcome is frozen at reveal. Add `AgentRun.source:
Literal["nebius","dev_fixture"]`. The UI labels any event log not streamed live as "Recorded run".

**V-15 Cross-field validation missing from the models.** Checked with pydantic directly:
`DiagnosisSubmission{conclusion:"fault"}` with no `hypothesis_id` or citations is **accepted**.
`{conclusion:"no_fault", hypothesis_id:"r1_increased"}` is **accepted**.
`CaseExperimentRequest{instrument:"output_voltage_sweep", node:"out"}` and
`{instrument:"transient_capture"}` with no node are **accepted**. *Change:* add `model_validator`s
(fault ⇒ hypothesis + ≥ 1 citation; otherwise no hypothesis; candidates only for
`insufficient_evidence`; `node` required iff transient). These are request-model-only changes, so
they don't alter the OpenAPI shape materially. State that a 422 never consumes the single
diagnosis, repair, or budget.

**V-16 Outcome and evaluation definitions.** Define:
- `unsupported_certainty`: a `fault`/`no_fault` conclusion with `EvidenceCheck` ∈ {ambiguous,
  contradicted}.
- `functional_recovery="not_needed"`: truth is `no_fault`, regardless of action.
- Outcome for a `failed` case.

Add `CaseOutcome.terminal_status` (failed vs normal), `hints_used`, and
`experiments_by: {user, agent}` (mixed control). `requested_by`/`submitted_by`/`proposed_by` have no
`baseline` value. Either add it, or require that the harness never writes to the user-facing store.
Define an evaluation-result schema (case ID, generator version, seed, solver, model ID, outcome,
measured usage, wall ms, failure count) before Days 9–11.

**V-17 Observation payload gaps.** `Observation` lacks the settings echo that CLAUDE.md requires of
an experiment record (grid, settle, clip, max step, sample rate, fade). They are public constants,
so the echo is safe. `ObservedTone` has `phase_deg` but no `expected_phase_deg`, and the policy
has no tone-phase tolerance. Phase is the more discriminating quantity at high fc: with tone phase
added, separability extends from 8 kHz to 20 kHz. `WaveformWindow`/`ObservedWaveform` length is
unbounded (browser payload and agent tokens). *Change:* add `settings`, `expected_phase_deg`,
optional `EVIDENCE_TONE_PHASE_TOLERANCE_DEG`, and a declared window cap (e.g. ≤ 1024 samples per
series).

**V-18 Non-finite numbers as a side channel.** `20·log10(0)` gives −inf. Starlette's JSON encoder
rejects non-finite floats, which becomes a 500 error. In M1 this is possible in `Spectrum.input_dbv`
bins. In M2, `c1_disconnected` (zero current for the low-pass, zero output for the high-pass) and
relative current tolerance against a predicted 0 would produce a 500 error **only for that hidden
state**, so the error itself is the leak. *Change:* declare floors (e.g. dB values clamped to ≥ −200
with a `below_floor` flag) and an absolute current-tolerance floor now.

**V-19 Operational.**
- `ArtifactStore._evict` (store/artifacts.py L870-881) evicts the globally oldest files. One busy
  session can delete another session's open-case audio, which then returns 404 on playback.
  *Change:* evict per session, or protect artifacts of open cases.
- In-process locks, cancel tokens, and SSE state require a **single uvicorn worker**. State this in
  deployment docs.
- Hidden state lives in `evaluator.sqlite3` and the public case in `resonance.sqlite3`, so
  multi-step writes aren't atomic. Write hidden state first, make the public write idempotent, and
  reconcile on startup.

**V-20 Small items.**
- ngspice returns grid frequencies 2.7e-9 relative off `20·10^(k/10)`, so match points by index or
  with a relative tolerance, never `==`.
- `Revision.frozen` is an immutable field, but "frozen while the case is open" implies mutation.
  Make it permanently frozen and copy values to explore later.
- `WorkbenchState` has no current-revision pointer. Undo and reload need one.
- Case experiments have no cancel path. Specify that a client disconnect cancels the run (polling
  `request.is_disconnected()`) and that it is not counted.
- `routes_meta.ERRORS` lacks 429 (`limit_reached`), and the `CANCELLED` comment says "499-ish" while
  the code maps it to 409.
- `DiagnosisEvent.diagnosis_id` should be `DiagnosisId`, and `EvidenceRef.result_id` should be
  pattern-checked (`exp_`/`obs_`).
- `ToolCallEvent.arguments` echoes model output to the browser. Cap its size.
- The ownership table in `docs/contracts.md` is headed "under `services/api/resonance/`" but lists
  `tests/...`, while `pyproject.toml` has `testpaths = ["tests"]` (i.e. `services/api/tests/`).
  State the real test root.

---

## Verified claims (passes)

- **Identifiability:**
  - ngspice 47 AC, `ac dec 10 20 20000`: R = 2 kΩ/C = 100 nF and R = 1 kΩ/C = 200 nF give
    identical V(out) gain and phase (max difference 0.000 dB / 0.000° at printed precision).
  - The |I(V1)| ratio C+/R+ is 2.000000 at all 31 points.
  - The analytical predictions are bit-identical too (×2 and ×0.5 are exact in binary floating
    point). Solver noise cannot split equivalent hypotheses.
  - ngspice against analytical (nominal): 6.7e-8 dB, 1.9e-7°, 3.4e-9 relative |I|.
- **Current sign:** ngspice's `i(V1)` is the negative of the delivered current (phase offset ±180°
  at every point). The contract's negation rule is correct.
- **Grid:** `ac dec 10 20 20000` returns exactly 31 points; the last is 20000.0000000000 Hz.
- **Tolerance margins (AC instruments):** the smallest deviation/tolerance ratio for any
  non-equivalent pair is ≥ 4.18 for nominal fc 10 Hz–40 kHz (voltage sweep 11.1–12.0× for 10 Hz–4 kHz;
  current sweep ≥ 13.5× for 50 Hz–8 kHz). No borderline pairs.
- **Headroom:** input peak over the 1.2 s clip on the 48 kHz grid is 0.89993 FS. The worst low-pass
  output peak for fc in [5 Hz, 50 kHz] is 0.89805 FS. Gain 1.0 is clip-free for passive RC in steady
  state. The weakest audible tone (fc = 10 Hz, 4 kHz) is −62.5 dBFS, about 25 LSB in 16-bit.
- **Settling:** tone-fit error is < 0.001 dB analytically even at fc = 1 Hz (1.26 τ settled). The
  residual DC tail at clip start is 0.41 mV at fc = 1 Hz and 0.007 mV at 5.57 Hz. With ngspice at
  5 µs the measured fit error is ≤ 0.008 dB.
- **Timestep side channel:** the number of transient timepoints is 280,010 for R/C =
  1k/100n, 2k/100n, 1k/200n, 500/100n, 1k/50n, 4.7k/10n, and 100k/1µ. Only the extreme 100 Ω/1 nF
  (fc = 1.6 MHz) gives 280,009. Raw output is the same size (8,960,583 B) for every plausible value.
  Wall-time variation was dominated by host load, not values.
- **Request hardening:** `CaseCreateRequest` rejects `seed`, `fault`, and `rc_highpass`
  (`extra="forbid"` works). OpenAPI mentions `seed`/`actual_value` only inside `CaseReveal`/
  `FaultReveal`.

## Appendix: commands run

```
cd services/api && uv run python ../../docs/verification/scripts/contract_numerics.py
# ngspice cross-checks (scratch netlists in %TEMP%\rsv, not in the repo):
ngspice_con.exe -b ac_<R>_<C>.cir      # ac dec 10 20 20000; wrdata vdb(out) vp(out) mag(i(V1)) ph(i(V1))
ngspice_con.exe -b tr.cir              # V1 in mid SIN(0 0.6 440); V2 mid 0 SIN(0 0.3 4000); tran 20.8333u 1.4 0 5u
ngspice_con.exe -b fit.cir             # tran 20.8333u 1.4 0.2 {5u|10u}; wrdata v(in) v(out) → numpy DFT fit
uv run python -c "...pydantic model_validate probes and app.openapi() scan..."
```

Not verified (no implementation exists yet): actual payloads, the agent tool surface, logs, artifact
headers, UI flow, Nebius behavior, container performance. These are scheduled in `plan.md`.
