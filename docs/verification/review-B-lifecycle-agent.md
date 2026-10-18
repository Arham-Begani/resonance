# Review B: case lifecycle, budget and concurrency, agent honesty

Owner: independent verification engineer (lens B). Date: 2026-10-16. Milestone 1 (low-pass).

Tests: `services/api/tests/verification/test_v_lifecycle_agent.py`. Each confirmed defect is an
`xfail(strict=True)` test named after its finding ID, so the suite documents the defect and
turns red when the defect is fixed (remove the marker then).

## Method and limits

- Every test drives the **real** FastAPI app through `httpx.AsyncClient` + `ASGITransport`, so
  concurrent requests really interleave on one event loop. That includes the lifespan, routes,
  error handlers, session cookie, CaseService, evaluator, AgentService, agent loop and
  `NebiusChatClient`.
- Two TEST-ONLY doubles are injected:
  - `VerifSimulator`: exact analytical RC results, plus gate, delay, non-cooperative and
    failure injection. It is built on the challenges kit.
  - `ScriptedProvider` (`tests/agent/agent_testkit.py`): scripted OpenAI-compatible replies
    served through `httpx.MockTransport`. **It is not AI.**
- One test repeats the close-during-agent-tool ordering against real ngspice 47.
- **No live Nemotron behavior is verified.** `NEBIUS_API_KEY` is not configured on this host.
  Everything said here about the agent concerns trusted code around the model: tool
  separation, validation, caps, cancellation, events, citations and secrets. It says nothing
  about model quality.
- Defect B-01 was confirmed a second way, outside the test suite. A real uvicorn server on
  127.0.0.1:8042 was run with a raw TCP client (scratch script
  `D:\tmp\verif-B\uvicorn_disconnect.py`).

## Findings

| ID | Severity | Summary |
|---|---|---|
| B-01 | major | Client disconnect never cancels an experiment (case or Explore). Case budget is consumed, and the Explore "Cancel" button says "cancelled" while the server finishes and stores the run |
| B-04 | major | During its own run the agent's `get_case` and `submit_diagnosis` results always say `available_actions: []` |
| B-02 | minor | After a crash or restart mid-run, the case stays owned by the dead run. Cancel cannot release it, so only close (which reveals the answer) is possible |
| B-03 | minor | While a run is active, user `repair-proposal`/`repair` return `conflict`, not the documented `run_in_progress` |
| B-05 | minor | A hint run that fails before showing any text still counts as a used hint, including in `CaseOutcome.hints_used` |

### B-01 (major): client disconnect does not cancel experiments

**Location:**
- `resonance/api/deps.py:24-44`: the session middleware is `@app.middleware("http")`, which
  is Starlette's `BaseHTTPMiddleware`.
- `resonance/api/routes_cases.py:106-112` and `resonance/api/routes_experiments.py:48-54` poll
  `request.is_disconnected()`.

**Cause:** in Starlette 1.8.0, `Request.is_disconnected()` reads `receive()` inside a cancel
scope that is already cancelled (`starlette/requests.py:332-344`). Behind `BaseHTTPMiddleware`,
`receive` is the middleware's `receive_or_disconnect`, which reads through an anyio task group
(`starlette/middleware/base.py:112-128`). The cancelled scope discards the message, so
`is_disconnected()` returns `False` forever.

**Evidence:**
- Scratch app with only the session middleware: 40/40 polls returned `False` after the
  disconnect. Without the middleware, the 5th poll returned `True`.
- Real uvicorn, case route: the TCP connection was closed mid-simulation. Two seconds later the
  simulation was still running and 0 simulator calls had been cancelled. After the gate was
  released: `budget used: 1`, observation `('source_current_sweep', 'succeeded', True)`.
- Real uvicorn, Explore route: same result. The experiment was recorded as `succeeded`.
- Test: `test_client_disconnect_cancels_experiment_without_using_budget` (xfail B-01).

**Impact:**
- Closing or reloading the tab mid-experiment consumes case budget. The contract says it does
  not.
- Explore's Cancel button (`apps/web/src/state/workbench.tsx:436-441`) tells the learner
  "The experiment was cancelled. Previous results are unchanged". In fact ngspice keeps
  running and the experiment is stored.
- The builders' test mocks `is_disconnected`, so it could not catch this.

**Fix (verified in scratch):** make the watcher await `request.receive()` until it returns
`http.disconnect`, instead of polling `is_disconnected()`. Through `BaseHTTPMiddleware` this
yields `http.disconnect`. Alternatively, rewrite the session middleware as pure ASGI.
`deps.py` belongs to the lead; the two routes belong to the investigation and audio engineers.

### B-04 (major): the agent is told it has no available actions

**Location:**
- `resonance/challenges/service.py:347-373`: with `active_run_id` set, `_available_actions`
  returns only `cancel_agent`/`close`.
- `resonance/agent/views.py:289` and `resonance/agent/case_tools.py:267-269` filter that list to
  `AGENT_CASE_ACTIONS`.

The agent only reads the case during its own run, so the filtered list is always `[]`.

**Repro:** `test_agent_view_lists_the_actions_the_agent_can_take` (xfail B-04). `get_case` sent
the model `available_actions: []`. The same run then executed `run_experiment` and
`submit_diagnosis` successfully. After the diagnosis it was again `[]`, although
`propose_repair` was allowed.

**Impact:**
- On every call, the model receives a false statement about what it may do. This can make a
  real model stop early or abstain.
- It also skews the Nemotron-versus-baseline comparison.
- The `FakeCaseService` in the agent tests shares the same blind spot.

**Fix:** compute the agent's actions from the run's own point of view. For example:
- `CaseService.available_actions_for(run_id)`; or
- derive them in the view: observing with budget remaining → `run_experiment`; observing →
  `submit_diagnosis`; after a diagnosis and before verification → `propose_repair`.

### B-02 (minor): a run lost in a crash keeps its case locked

**Location:**
- `CaseRecord.active_run_id` is persisted. `CaseService._load`
  (`challenges/service.py:294-300`) clears only a stale `in_flight`, not a stale owner.
- `AgentService.get_run`/`cancel` (`agent/service.py:404-443`) report and re-save the run as
  `failed` but never release the case.

**Repro:** `test_case_owned_by_a_run_lost_in_a_restart_can_be_taken_over` (xfail B-02). It
starts a second app instance on the same data files while the first app's run is still active,
which simulates a process crash.
- The run is reported as `failed`.
- After `POST /api/agent-runs/{id}/cancel`, the case still has `active_run_id` set, with
  `available_actions` `['cancel_agent','close']`.
- A user experiment returns `409 run_in_progress` with "The AI lab partner is working on this
  case. Cancel it to take over." That message is false.

**Scope:** a graceful shutdown releases correctly (`AgentService.shutdown` finalizes runs).
Only a crash, SIGKILL or OOM leaves the case locked.

**Fix:** treat an owner whose run is not in memory and not `running` as stale. Release it in
`_load` or `claim_for_agent`. Alternatively, have `get_run` and `cancel` release the case when
they re-save a run as failed.

### B-03 (minor): wrong error code on repair endpoints during a run

`propose_repair` and `verify_repair` check the case status before ownership
(`challenges/service.py:899-901`, `934-938`). While an agent run is active in `observing`, they
return `409 conflict` ("A repair can be proposed after a diagnosis..."). The contract says
`409 run_in_progress`.

The other mutations do return `run_in_progress`: mode, experiment, diagnosis, agent start and
hint (verified). Practical impact is small, because the action is not allowed in that state
anyway.

Repro: `test_active_run_repair_endpoints_also_report_run_in_progress` (xfail B-03).

**Fix:** check ownership first, or amend the contract text.

### B-05 (minor): failed hint runs are counted

`AgentService.start_case_run` calls `record_hint` before any inference
(`agent/service.py:179-180`). A hint run that fails at once (scripted 401) shows no text but
leaves `hints_used = 1`. `CaseOutcome.hints_used` reports the same.

Repro: `test_failed_hint_run_is_not_reported_as_a_used_hint` (xfail B-05). The test encodes
"not counted".

**Decision needed (lead):** the alternative is to keep counting at request time but label the
field "hints requested" in evaluation reports.

## Verified (passing)

### Transition table (V-11)

- Every one of 12 actions was tried in each of 10 states: ready, observing (guide), observing
  (agent), diagnosis_submitted, diagnosis_submitted+proposal, unresolved, unresolved+proposal,
  repair_verification, failed and closed.
- That covers 120 state×action pairs. Each gave exactly the expected code or next state.
- Refused actions left the case unchanged in every case (status, mode, budget, hints,
  observations, diagnosis, repair, reveal, actions) and created no agent run.
- `available_actions` never advertises a refused action.
- Close on a closed case is idempotent and keeps the same reveal.
- A failed case closes with `terminal_status: failed`.
- Starting a run from `ready` implicitly chooses the matching mode. This is documented in
  `docs/ai-integration.md` but not in the contract table, and `ready` does not advertise it.

### Active run

Mode, experiment, diagnosis (fault and insufficient), investigate and hint all returned
`409 run_in_progress` and changed nothing. Simultaneous agent starts created exactly 1 run out
of 4 requests. Close cancels the run.

### Budget

- Exactly 3 counted experiments; the 4th returns `budget_exhausted`.
- 8 invalid bodies returned 422 and made 0 simulator calls: missing or extra probe, unknown
  instrument or probe, missing or short key, `seed`, a free frequency range.
- `distinguishing_instruments` is published once the budget is spent.

### Infrastructure failures

- One failure → exactly one automatic retry (2 simulator calls) → a counted success.
- Two failures → a failed, uncounted observation with no partial data. The response is
  502/504, retryable, and budget is untouched.
- The 3rd failure → `failed` with `retryable: false`; only `close` remains.
- `SimulatorUnavailable` is not retried (1 call).

### Idempotency

- Same key + same body → the identical stored observation, with no new simulator call and no
  budget.
- Same key + different instrument or probe → `409 conflict`, `field: client_request_id`, not
  retryable.
- Key still in flight → `409`, retryable; afterwards the key replays the stored result.

### Concurrency

- Waves of 8 simultaneous experiments: exactly 3 successes in total, with `used` values
  {1,2,3}. At most 2 simulator calls ran at once (hidden + reference). Later bursts of 8 all
  returned `budget_exhausted`.
- 12 simultaneous diagnoses → exactly 1 recorded.
- 4 simultaneous `POST /cases` → exactly 1 case and 1 hidden row.
- A failed symptom leaves 0 hidden rows and no active case.
- A diagnosis or agent start while an experiment is in flight → 409 (no run created). The
  diagnosis then lands after the observation.

### Close ordering (V-03)

- Close during an in-flight experiment: the experiment returns `409 cancelled`, the
  observation is cancelled and uncounted, and nothing has `created_at` after `revealed_at`.
- With a non-cooperative simulator, close waits; the counted observation lands before the
  reveal and is included in the outcome.
- Close during a repair verification: the verification is cancelled, no repair is recorded,
  and recovery is `not_attempted`.
- 3 concurrent closes → one reveal; the others get a retryable 409.

### Close during an agent run

Each close below was asserted to return in under `CASE_CLOSE_AGENT_WAIT_S` (10 s). The
durations are for the whole test, including case creation; the close itself was not timed
separately.

| Run state at close | Test duration | Outcome |
|---|---|---|
| Blocked in inference | 1.2 s | Inference aborted |
| Mid-tool, cooperative simulator | 1.0 s | Run cancelled |
| Mid-tool, simulator ignoring the token | 5.8 s | Mostly the loop's 5 s grace period, then task cancel |
| Mid-transient, real ngspice | 3.1 s | Job dir empty afterwards |

In every case:
- the run ends `cancelled`;
- `seq` is contiguous and `run_finished` is the last event;
- no event has `at` > `revealed_at`, and the log does not grow afterwards;
- no observation was counted.
Agent tools (all 6 investigate and 2 hint tools) refuse fatally on a closed case and never
serialize the reveal.

### Agent loop honesty (scripted provider, real services)

- **Tool-set separation.**
  - Hint runs are offered only `{get_case, read_observation}`; `run_experiment`,
    `submit_diagnosis` and `assess_evidence` are refused and never executed.
  - Explore runs are offered only the 5 explore tools. Their case tools are refused, and the
    case revision gives `not_found` in both `run_experiment` and `inspect_circuit`.
  - Explore endpoints return 404 for case revisions.
- **Refused calls.** Unknown tool names (`read_file`, `evaluator.reveal`), invalid arguments
  and malformed JSON were all refused: 9/9, 0 simulator calls, and the events show `{}`
  arguments.
- **One tool call per reply.** Of 3 calls in one reply, only the first ran; all three call IDs
  received a tool result.
- **Caps.**
  - Model calls: stops after exactly 10 requests.
  - Tool calls: verified with a cap of 2.
  - Experiments: the 4th is refused with `limit_reached` and never simulated.
  - A CaseService `budget_exhausted` reaches the model and is not fatal.
  - Tokens are measured: the `max_tokens` sent were `[4096, 1000]`, then `limit_reached` at
    59,500 tokens.
  - A reply without `usage` fails closed and is not executed.
  - Wall time 1 s: stops inference (provider request cancelled) and stops simulation
    (uncounted).
- **Retries.** Only 429/5xx are retried, once per run. 400/401/404 are not retried. A tool
  failure is not retried by the loop and fails the run as `simulation_failed`.
- **Cancel endpoint.** Mid-inference it returns `cancelled` in <3 s and the case is released.
  Mid-tool the observation is cancelled and uncounted, and the learner can take over at once.
- **Reasoning.** `<think>`, `<THINK>`, unterminated and dangling think tags and
  `reasoning_content` never appear in events, runs, case payloads or later prompts.
- **Citations.** Fabricated, cross-case, failed-observation and `exp_` IDs in a case run are
  replaced by "unverified citation" (4 of 4). Only the valid ID becomes an `EvidenceRef`
  (`tone_gain_db`).
- **Diagnosis validation.** Three invalid diagnoses (schema, unknown citation, wrong quantity)
  did not consume the single diagnosis. The valid one was recorded once, by the agent, with a
  single `diagnosis` event; a second attempt got `409`. The agent proposes a repair but never
  verifies one.
- **Attribution.** Learner and agent experiments are attributed separately (outcome 3/2/1).
  `read_observation` of another case gives `not_found`. `assess_evidence` returns only
  `{hypothesis_status, remaining, statement}`, with no instrument names while budget remains.

### AI unavailable

Checked for 5 configurations: no key, placeholder key, blank key, no model, and the dev-fixture
flag. In each:
- Health shows the right state.
- Investigate, hint and explore return `503 ai_unavailable` with `NEBIUS_API_KEY`/`.env`
  instructions.
- No run record is created and the case is unchanged; the mode is not set as a side effect.
- The manual path still works.

`resonance_dev_agent_fixture` defaults to `False`.

### Secrets

With key `sk-verif-SECRET-123`:
- The key reached the provider only as `Bearer …`.
- It is absent from health, run, event-log and SSE payloads, the case and workbench, request
  bodies, and DEBUG logs (root, openai, httpx, httpcore; the capture was checked to be
  non-empty).
- It is absent from every file in the data directory (both SQLite databases and their WAL
  files).

Provider runs report `source: "nebius"` and the configured model.

### Explore proposals

- The agent created 3 pending proposals and 0 revisions.
- Predictions carry `basis: analytical_first_order_rc` and match an independent first-order
  formula within 1e-9 dB. `predicted_goal_pass` matches the goal constraints. The tool result
  says `applied: false` and "not measurements".
- Applying through `POST /api/revisions` creates the revision once (R1 = 2.2 kΩ) and marks the
  proposal `applied`. A second apply returns 409, and dismissing it afterwards returns 409.
- Dismiss is idempotent, and applying a dismissed proposal returns 409.
- A stale parent returns `409 stale_revision`; another session gets 404.

### Observations (not defects)

- With policy values, the tool-call cap (12) can never bind. Only one call per reply runs, so
  tool calls ≤ model calls ≤ 10. Tool executions are still bounded, at 10.
- When a tool that ignores cancellation is interrupted by task cancellation, no "cancelled"
  observation is recorded. The `CancelledError` path skips `_record_failure`. Budget is
  unaffected.

## Contract or ownership requests

- **B-01:** a change in `api/deps.py` (lead) or in both route watchers (investigation and
  audio engineers).
- **B-03:** a code change or a contract text change (lead's decision).
- **B-05:** a definition of `hints_used` (lead's decision).
