# Resonance contracts and the hidden/public boundary

Owner: lead. The authoritative definitions are the Pydantic models in
`services/api/resonance/contracts/`. Frontend types are generated from them
(`apps/web/src/api/schema.d.ts`, `npm run gen:api`). This document explains the rules
that the types alone cannot express. **Changing a contract requires the lead's approval.**

## Conventions

- Canonical SI floats everywhere. Field names carry units: `_hz _s _ms _v _ma _db _dbv _deg _fs _si`.
- Opaque random IDs with prefixes (`rev_ exp_ art_ cmp_ goal_ case_ obs_ dx_ rep_ prop_ run_`),
  created only by `contracts.common.new_id`. IDs never encode content, values, or seeds.
- Every resource is scoped to the anonymous session cookie `rs_session`. Another session's ID
  behaves exactly like a missing ID (404, identical message). Clients must complete one request
  (e.g. `GET /api/health`) and receive the cookie before issuing concurrent requests; concurrent
  cookie-less requests necessarily get separate sessions.
- Errors are `ErrorResponse{error: ApiError}` with a learner-safe message (no values, netlists,
  paths, raw simulator output, stack traces, or secrets).
- Source current uses the **delivered** convention: positive current leaves V1's + terminal
  (ngspice reports the opposite sign; the simulation layer negates it).
- AC analyses use a 1 V, 0° source, so `V(out)` equals the transfer function and `I(V1)` equals
  the input admittance × 1 V.

## Module ownership

| Path (code under `services/api/resonance/`; backend tests under `services/api/tests/`) | Owner |
|---|---|
| `contracts/**`, `config.py`, `app.py`, `openapi_export.py`, `store/**`, `api/deps.py`, `api/errors.py`, `api/routes_meta.py` | Lead |
| `circuits/**`, `simulation/**`, `api/routes_revisions.py`, `tests/circuits/**`, `tests/simulation/**` | Circuit & simulation engineer |
| `audio/**`, `explore/**`, `api/routes_experiments.py`, `tests/audio/**`, `tests/explore/**` | Audio & signal engineer |
| `challenges/**`, `agent/**`, `api/routes_cases.py`, `api/routes_agent.py`, `tests/challenges/**`, `tests/agent/**` | Investigation & AI engineer |
| `apps/web/**` except `apps/web/e2e/**`; owns `apps/web/package.json` + lockfile | Interface & accessibility engineer |
| `tests/verification/**`, `apps/web/e2e/**`, `docs/verification/**` | Independent verification engineer |
| `pyproject.toml`, `uv.lock` (Python deps) | Lead (ask to add a dependency) |

## Internal interfaces (not HTTP)

- `contracts.internal.CircuitSpec` — template + SI values (+ reserved `disconnected`). Public
  revisions convert to it; hidden cases build their own inside the evaluator.
- `contracts.internal.Simulator` protocol — `info()`, `ac(circuit, AcSweep|AcPoints)`,
  `transient(circuit, TransientRequest)`; cooperative `CancelToken`. Implemented by
  `resonance.simulation.create_simulator(settings)`. Only requested `probes` are written by
  ngspice and returned.
- Simulation failures raise `SimulatorUnavailable | SimulationFailed | SimulationTimeout |
  SimulationCancelled` whose `str()` is learner-safe.
- Feature modules may define `async startup(app)` / `async shutdown(app)` in
  `resonance.explore.service`, `resonance.challenges.service`, `resonance.agent.service`;
  `app.py` calls them and they attach singletons to `app.state`.

## Hidden/public boundary (the central rule)

Three separate things exist for every case:

1. **Intended design** — a frozen public `Revision` (`origin="case_intended"`, `frozen=true`)
   with nominal values. Visible to the browser and the agent.
2. **Hidden physical-model state** — the actual `CircuitSpec`, the fault, and the seed. Stored
   only by `resonance.challenges.evaluator` in its own SQLite file (`evaluator.sqlite3`).
3. **Sanitized observation history** — `Observation` records containing only the allowed
   quantities of each instrument, plus reference values from the intended design.

Rules:

- The evaluator is the only code that constructs hidden `CircuitSpec`s or reads the hidden
  database. It is not reachable from any agent tool. Agents get no file, shell, code-execution,
  general HTTP, or database tools.
- Nothing that leaves the evaluator may depend on hidden values except the declared measured
  quantities: no netlists, no actual values, no seeds, no fault labels, no ngspice logs, no
  timestep counts, no simulation durations, no cache keys or artifact names derived from
  content. Transient settings (settle time, step, clip length) are fixed public constants.
- Artifacts get random IDs; WAV files carry only standard PCM headers.
- Errors from hidden runs are mapped to generic learner-safe messages; logs record case ID and
  status only.
- Seeds are random, private until the case is closed, then published in `CaseReveal`.
  `POST /api/cases` accepts no seed or fault input. (A private evaluation harness may create
  cases from seeds in-process; it is not exposed over HTTP.)
- `PublicCase.reveal` is `null` until `status == "closed"`. Closing first stops any agent run.
- `EvidenceState.distinguishing_instruments` is withheld while the case is `ready`/`observing`
  with budget remaining (it would steer the investigation) and published afterwards.
- After reveal, the case is read-only: experiments, agent runs, diagnosis, and repair are refused (409).
- During a case the intended revision is frozen: it cannot be edited or used as a parent.
- Explore endpoints only ever simulate public session revisions. No endpoint other than the
  case experiment/repair endpoints simulates a hidden circuit.

## Investigation rules

- Standardized symptom (observation 0, uncounted, `requested_by="system"`): two-tone transient
  capture of the output voltage for the hidden circuit and the intended design, with tone
  levels and an Expected-vs-Observed audio comparison. Identical for user, agent, and baseline.
  If it fails, no case is created (hidden state discarded) and the simulator error is returned.
- Instruments (declared in `PublicCase.instruments`):
  - `output_voltage_sweep`: gain (dB) + phase (deg) of V(out)/V(in) on the fixed grid
    20 Hz–20 kHz, 10 points/decade (31 points).
  - `source_current_sweep`: |I(V1)| (mA, 1 V source) + phase on the same grid.
  - `transient_capture` with `probe` = `output_voltage` (repeats the symptom measurement) or
    `source_current` (two-tone current amplitudes in mA and phases; shown, never played as audio).
- Hypotheses (milestone 1): `no_fault`, `r1_increased` (2×), `r1_decreased` (0.5×),
  `c1_increased` (2×), `c1_decreased` (0.5×). Predictions are analytical (public).
  Status: `contradicted` if any observation deviates beyond tolerance; `supported` if consistent
  and the only consistent hypothesis; otherwise `unresolved`. No likelihood percentages.
- Identifiability: output voltage depends only on R·C, so `r1_increased`/`c1_increased` (and the
  two decreased variants) are indistinguishable with voltage-only observations; source current
  separates them. Honest consequence (V-08): with the symptom, one current measurement suffices,
  so milestone-1 cases are solvable in one experiment. Evaluation must say so; 100 %
  identification is not an achievement by itself.
- Case generation (V-05/V-07): seeds `secrets.token_hex(16)`; nominal design and fault drawn
  independently and i.i.d. (`policy.FAULT_PRIOR`, 20 % each incl. no_fault); nominal cutoff in
  `policy.CASE_NOMINAL_CUTOFF_RANGE_HZ` (40 Hz–5 kHz) so every variant stays inside the
  template's 20 Hz–20 kHz range. Balanced sets only in the private evaluation harness.

### Concurrency, budget, and idempotency (V-02, V-04)

- Exactly one mutating operation per case at a time (mode, experiment, diagnosis,
  repair-proposal, repair, agent start, hint). A second concurrent mutation → 409 `conflict`
  (retryable). Close is special: it cancels and awaits in-flight work (below).
- Budget: 3 successful experiments, reserved before simulating, released on failure/cancel,
  committed together with the observation. Validation errors (422) never consume budget, the
  single diagnosis, or the single repair verification.
- `client_request_id` is required and scoped to the case: same key + same body → stored
  response (no budget); same key + different body → 409; key still in flight → 409 retryable.
- Infrastructure failure: one automatic retry without consuming budget; if it fails again, a
  failed uncounted observation and a structured error; after `CASE_MAX_INFRA_FAILURES` → `failed`.
  A client disconnect cancels the experiment (not counted).
- While an agent run is active, every mutating case endpoint except close and agent cancel
  returns 409 `run_in_progress`. Agent tool calls execute sequentially.
- Hidden-circuit runs never read or write any cache and are never short-circuited by equality
  with another spec. The only permitted reuse is the per-case intended-design reference.
  Every instrument performs the same simulation work regardless of hidden values.

### Transition table (V-11)

| State | Allowed actions → next state |
|---|---|
| (no open case) | `POST /cases` → `ready` (409 if the session already has an unclosed case) |
| `ready` | choose mode → `observing`; diagnosis → `diagnosis_submitted`/`unresolved`; close → `closed` |
| `observing` | experiment (budget permitting); agent run (agent mode) / hint (guide mode; `hints_used` counts hints actually delivered, B-05); change mode (no run active; mixed control is recorded); diagnosis → `diagnosis_submitted` (fault/no_fault) or `unresolved` (insufficient_evidence); close |
| `diagnosis_submitted`, `unresolved` | propose/replace repair; verify repair (once) → `repair_verification`; close |
| `repair_verification` | close |
| `failed` | close |
| `closed` | read only; experiments, runs, diagnosis, repair → 409 |

Close (V-03): mark closing (all mutations and agent-facing reads refuse), cancel and await the
agent run (≤ `CASE_CLOSE_AGENT_WAIT_S`) and any in-flight experiment, then mark the evaluator
closed and attach the reveal. No agent event may be emitted after the reveal.

### Public-only computation (V-06, V-13)

- Before close, `status`, `available_actions`, `budget`, `evidence`, summaries, and transitions
  depend only on public data (intended design, recorded observations, submissions, the repair
  proposal, measured verification). Only `reveal` reads the truth.
- `evidence.distinguishing_instruments` is null while status ∈ {ready, observing} and budget
  remains; published once the budget is spent or the status is past observing. While withheld,
  `statement` and `Observation.summary` do not name instruments.

### Diagnosis and repair (V-09, V-10, V-15, V-16)

- Diagnosis: submitted once. `fault` ⇒ `hypothesis_id` + ≥1 citation; `no_fault` ⇒ ≥1 citation;
  `insufficient_evidence` may list candidates and a suggested instrument. Citations use the
  closed `QuantityName` vocabulary and must reference a succeeded observation of this case; a
  given `frequency_hz` must match a measured frequency within 0.1 %. Trusted code records an
  `EvidenceCheck`; it does not rewrite the submission.
- Repair = replace ONE component with its intended nominal value (`{component_id, rationale}`),
  replaceable until verification. `POST /cases/{id}/repair` takes no body and verifies the
  recorded proposal once, on the 31-point grid (±0.5 dB, ±3°) plus an Expected-vs-Repaired audio
  comparison. Allowed after any diagnosis. Functional recovery and exact identification are
  reported separately; identification is scored only at reveal (`CaseOutcome` docstring).
- Milestone 1 refuses everything on closed cases (no replay); `Observation.replay` stays false.

### Payload hygiene (V-17, V-18, V-20, A-01)

- Measurement quantization (verification finding A-01): every number that leaves the evaluator
  from a hidden-circuit run, and the matching intended-design reference, is rounded at the
  evaluator boundary: dB to 0.001 dB, phases to 0.01°, V/mA amplitudes, sweep magnitudes and
  waveform samples to 6 significant figures, audio `peak_fs` to 4 decimals. Solver round-off
  (~1e-15 relative) differs between voltage-equivalent R/C variants and would otherwise let
  Explore fingerprint the hidden circuit bit-for-bit; the resolutions are ≥ 500× finer than the
  evidence tolerances. Hypothesis evaluation runs on the rounded values.
- Observations echo their public settings; waveform windows ≤ 1024 samples per series.
- dB values are clamped at `policy.DB_FLOOR` (−200); no non-finite floats in any payload.
- Grid frequencies are matched by index or relative tolerance, never `==`.
- Case intended revisions are permanently frozen; explore them by copying values.

## Audio rules

- One global `PLAYBACK_GAIN_FS_PER_V` for every clip, so any two clips are comparable. No
  per-clip normalization or compression. A needed reduction is applied to the whole comparison
  and disclosed (`gain_adjustment_db`, `note`). Clipping is detected and reported.
- Settling is skipped using a fixed public `TRANSIENT_SETTLE_S` (template cutoff ≥ 20 Hz); matched raised-cosine fades
  are applied for playback only; tone amplitudes are fitted on the un-faded steady state and
  cross-checked against AC at the exact tone frequency (≤ 0.5 dB).
- Playback is user-initiated, single-source, stoppable, with a common user volume.

## Agent rules

- Tool sets: EXPLORE (inspect circuit, set goal, propose values, run experiment on an applied
  session revision, compare experiments) ≠ INVESTIGATE (get case, run allowed experiment, read
  observation, assess evidence, submit diagnosis, propose repair) ≠ HINT (get case, read
  observation). The user applies proposals and repair verifications. The loop executes at
  most one tool call per model reply; extra calls in the same reply get an error result.
- Agent-visible case data (V-01) is an explicit allow-list (`resonance/agent/views.py`): status,
  mode, budget, intended design with nominal values and topology text, instruments, hypothesis
  catalog, tolerances, compact observation numbers (no waveforms, audio, or artifact IDs),
  diagnosis/repair summaries. Never `reveal`. Hypothesis statuses come only from the explicit
  `assess_evidence` tool so their use is visible and countable. `distinguishing_instruments` is
  never given to the agent.
- Caps: model calls, tool calls, experiments, total tokens, wall time, retries (see
  `contracts.policy`). Cancellation stops inference and in-flight simulation. Tools refuse once
  the case is closing/closed.
- Events are genuine: status for real stage transitions, tool calls/results as executed (args
  capped at 2 KB), visible assistant text only (never hidden reasoning), measured usage only.
  `AgentRun.source` is `nebius` for real inference.
- Numerical claims reference `obs_`/`exp_` IDs; structured evidence is rendered from data.
- Missing credentials → `AiStatus.state = missing_key|missing_model` and `ai_unavailable` (503).
  A scripted fixture may exist only behind `RESONANCE_DEV_AGENT_FIXTURE=1`, labeled
  `source="dev_fixture"` (not built in milestone 1).

## Deployment constraints

- Middleware must be pure ASGI (never `BaseHTTPMiddleware` / `@app.middleware("http")`): it
  hides client disconnects, so cancelled requests would keep simulating (finding B-01).
- Run a single uvicorn worker: per-case locks, cancel tokens, and SSE run registries are
  in-process. The two SQLite files are written hidden-first; public writes are idempotent.

## Milestone 2 additions (high-pass, open capacitor, Learn, suggestions, evaluation)

Ownership additions: `explore/suggest.py`, `explore/learn*.py`, `api/routes_learn.py`,
`tests/learn/**` → audio & signal engineer; `evals/**` → investigation & AI engineer.

- **High-pass template** `rc_highpass`: V1 Input(+)→Ground, C1 Input→Output, R1 Output→Ground,
  same component bounds and 20 Hz–20 kHz cutoff range. Each template carries its own two-tone
  signal (`policy.TEMPLATE_TONES`: low-pass 440 Hz tone + 4 kHz whistle; high-pass 50 Hz mains
  hum + 440 Hz tone) and opening goal (`policy.TEMPLATE_DEFAULT_GOALS`, also served as
  `CircuitTemplate.default_goal`). Feasibility is always analysed for the template of the
  workbench's current revision. Switching family on the workbench creates a revision of the
  new template (its defaults overridden by any given values) linked to the previous revision
  for history/undo, with empty `changes`; the UI then sets that template's default goal.
  A family-switch proposal must list both R1 and C1.
- **Open capacitor** `c1_disconnected`: modeled by removing C1 from the netlist (an open
  circuit), for both families. Proven simulatable (real ngspice: low-pass passes the input with
  zero current; high-pass outputs nothing with zero current) and separable by the standardized
  symptom alone for every drawable design (≥ 7.3 tolerance units low-pass, ≥ 205 high-pass),
  so the generator (version `rc-m2-v1`) draws all six conditions at 1/6. High-pass nominal
  cutoffs stop near 1.6 kHz, where the 50/440 Hz current capture would become ambiguous.
- **No measurable signal**: a gain ≤ −120 dB or a current ≤ 1e-6 mA counts as no measurable
  output (`challenges/instruments.py`); its phase is published as 0° and never used as
  evidence. Non-finite raw simulator data is rejected before quantization. Replacing C1 with its nominal part also repairs an open capacitor.
  Non-finite results (e.g. 0 V or 0 mA) are clamped/floored per V-18, never a 500.
- **Case family**: `CaseCreateRequest.template_id` is public; null draws a family with
  `policy.CASE_FAMILY_PRIOR` (50/50).
- **Learn mode**: prompts are deterministic (component × multiplier inside the supported
  range). An attempt measures the base and changed revisions with ngspice, grades each
  per-tone prediction from the MEASURED exact-frequency gain change (|Δ| <
  `LEARN_SAME_THRESHOLD_DB` = about the same), and returns deterministic explanation and
  tradeoff text that cites experiment IDs. AI explanations are separate, labeled runs.
- **Design suggestions** (`POST /api/workbench/goal/suggest`): analytical, labeled as a
  prediction; target cutoff inside the feasible interval and supported range; nearest E12
  part for the varied component; a tradeoff statement when infeasible or when rounding misses.
  The learner applies it as an ordinary revision and measures it.
- **Template-switching proposals**: `EditProposal.template_id` names the target family;
  applying it creates a revision of that template linked to the base for undo.
- **Evaluation**: a frozen held-out set (≥ 24 balanced cases across both families and all
  conditions, nominal values unseen in development tests) run by a fixed-experiment baseline
  and, when credentials exist, by the Nemotron agent under identical symptoms, budgets and
  tolerances. Reports exact diagnosis, abstentions, unsupported certainty, verified repair,
  experiment count, failures and measured latency; costs only from measured usage.
