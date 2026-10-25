# AI integration: Nemotron via Nebius Token Factory

Owner: investigation & AI engineer. Code: `services/api/resonance/agent/`,
`services/api/resonance/api/routes_agent.py`. Tests: `services/api/tests/agent/`.

## Status (2026-10-25)

| Item | Status |
|---|---|
| Bounded tool loop, three separated tool sets, event stream, cancellation, AI-unavailable path | Implemented and tested with scripted provider responses (TEST-ONLY `httpx.MockTransport`) |
| Integration with the real CaseService, evaluator and ngspice (provider scripted) | Tested (`tests/agent/test_agent_integration.py`, `tests/agent/test_agent_families.py`) |
| Milestone 2: both circuit families (low-pass, high-pass) and all six conditions incl. `c1_disconnected` in case views and prompts; honest rendering of readings at the measurement floor | Implemented; tested with the scripted provider, including one real-ngspice open-capacitor high-pass case |
| Milestone 2 explore tools: `suggest_design`, family-switch `propose_values(template_id)`, `read_learn_attempt` | Implemented; tested with the scripted provider |
| Live Nebius call, model-ID confirmation, real tool-call round trip, latency, usage | **Verified live on 2026-10-25** (3/3 `live_ai` tests passed; see [Live verification results](#live-verification-results-2026-10-25)) |
| Nemotron evaluation run on the held-out set | Not run yet |

Without credentials the API reports `AiStatus.state = "missing_key"` (`GET /api/health`) and
every agent-run endpoint returns `503 ai_unavailable` with setup instructions. No run is created
and nothing scripted is shown as AI. The simulator and the manual investigation keep working.

## Provider configuration

| Setting | Value | Source |
|---|---|---|
| Base URL | `https://api.tokenfactory.nebius.com/v1/` (`NEBIUS_BASE_URL`) | Token Factory quickstart, docs.tokenfactory.nebius.com/quickstart; official cookbook |
| Auth | `NEBIUS_API_KEY`, sent as a bearer token by the OpenAI SDK | same |
| Model | `NEBIUS_MODEL` only. The code has **no fallback model ID** | CLAUDE.md |
| Candidate model | `nvidia/nemotron-3-super-120b-a12b` (in `.env.example`) | Nebius blog "Nemotron 3 Super now available" (Playground link); cookbook list provided by the lead. **Not yet confirmed against `/v1/models` with a key.** |

Client: `openai.AsyncOpenAI(base_url=..., api_key=..., max_retries=0, timeout=90 s read /
10 s connect)` (`agent/client.py`). The SDK's own retries are off, so the loop owns the retry
policy and counts retries against the run cap. Tests inject an `httpx.AsyncClient` with a
`MockTransport`. openai 3.31 accepts a legacy `httpx.AsyncClient` as well as its `httpx2` client.

Request parameters actually sent on every model call:

| Parameter | Value | Why |
|---|---|---|
| `temperature`, `top_p` | 1.0, 0.95 | Nemotron 3 Super model card: recommended for reasoning on/off and tool calling (huggingface.co/nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-FP8) |
| `max_tokens` | min(4096, tokens left under the run cap) | Bounds one reply, including reasoning tokens |
| `tools`, `tool_choice` | JSON Schemas generated from the Pydantic argument models (`$ref`s inlined), `"auto"` | Token Factory function-calling guide |
| `parallel_tool_calls` | **not sent** | Not listed in the Token Factory chat-completions reference. Acceptance is checked by the live test. Sequential execution is enforced in code regardless (see below). |
| reasoning controls (`reasoning_effort`, `chat_template_kwargs`) | **not sent** | See "Reasoning" |

## Reasoning (what is documented, what is assumed)

Documented facts:
- The Nemotron 3 Super model card toggles reasoning with
  `extra_body={"chat_template_kwargs": {"enable_thinking": True|False}}` (default on). It adds
  `"low_effort": True` to reduce reasoning tokens. Those examples target self-hosted vLLM,
  SGLang and TRT-LLM with the model's reasoning parser.
- The Token Factory chat-completions reference lists `reasoning_effort` (`none` … `max`) and
  `max_completion_tokens` (which includes reasoning tokens). It documents `reasoning_content` /
  `reasoning` only on **streaming** deltas, and `usage.completion_tokens_details.reasoning_tokens`.

Not yet verified on Token Factory: which of these switches the hosted Nemotron honors, and
whether reasoning arrives in a separate field or inline as `<think>` text. Until the live test
answers this, no reasoning switch is sent and the model default applies.

Handling, independent of the answer:
- Provider reasoning fields (`reasoning_content`, `reasoning`) are never read (`parse_completion`
  copies only `content`, `tool_calls`, `finish_reason` and `usage`).
- `<think>…</think>` (and `<thinking>`/`<reasoning>`) blocks, a dangling `</think>` prefix, and an
  unterminated `<think>` suffix are stripped from `content` before anything is emitted
  (`agent/text.strip_reasoning`).
- Reasoning is never sent back to the model in later turns.
- Reasoning tokens still count toward the measured token cap through `usage.total_tokens`.

## Run kinds, tools and caps

One reasoning agent, one bounded loop (`agent/loop.py`), and a different tool set per run kind.
A tool name outside the run's own set is refused and never executed.

| Kind | Endpoint | Tools |
|---|---|---|
| `investigate` (Investigate for me) | `POST /api/cases/{id}/agent-runs {kind:"investigate", message?}` | `get_case`, `assess_evidence`, `run_experiment(instrument, probe?)`, `read_observation(observation_id)`, `submit_diagnosis(DiagnosisSubmission)`, `propose_repair(component_id, rationale)` |
| `hint` (Guide me) | `POST /api/cases/{id}/agent-runs {kind:"hint", message?}` | `get_case`, `read_observation` (read-only) |
| `explore` | `POST /api/explore/agent-runs {revision_id, message, goal_id?}` | `inspect_circuit(revision_id)`, `set_goal(GoalInput)`, `suggest_design(vary: R1 or C1)`, `propose_values(changes, rationale, template_id?)`, `run_experiment(revision_id)`, `compare_experiments(a_id, b_id)`, `read_learn_attempt(attempt_id?)` |

No tool gives file, shell, code-execution, HTTP, database or hidden-state access. Case tools call
only the public CaseService interface (`agent/ports.CaseServicePort`). Explore tools touch only
the session's public revisions and experiments. Case revisions (`origin="case_intended"`) are
treated as not found.

Caps (`agent/service.limits_for`). Investigate and explore use the declared policy values. Hint
runs are tighter but never looser than policy.

| Cap | investigate / explore | hint |
|---|---|---|
| model calls (each HTTP attempt, retries included) | 10 | 4 |
| tool calls | 12 | 4 |
| experiments | 3 | 0 |
| total tokens (measured `usage.total_tokens`) | 60,000 | 30,000 |
| wall time | 180 s | 90 s |
| retries (transient provider errors only) | 1 | 1 |

Loop rules:
- **Validation before execution.** Arguments are parsed as JSON and validated with the tool's
  Pydantic model, including cross-field validators such as `DiagnosisSubmission` and probe
  rules. Invalid calls are not executed. The validation message goes back to the model as the
  tool result. A rejected diagnosis therefore does not use up the single diagnosis.
- **Strictly sequential.** Only the first tool call in a reply is executed. Extra calls get an
  error result ("one tool call per reply") and are not executed. Every call counts toward the
  tool-call cap except these refused extras.
- **Measured tokens only.** If a response has no `usage`, the run stops as failed (fail closed),
  because the token cap could not be enforced.
- **Retries.** Timeouts, connection errors, 429 and 5xx are retried once after a 1 s backoff.
  400/401/403/404/422 are not retried. Errors become learner-safe messages, and SDK exception
  text is never logged or returned.
- **Fatal tool results.** A simulator failure after the CaseService's own automatic retry stops
  the run as `failed`. So does a refusal because the case is closing.
- **Final answer.** A reply with visible text and no tool calls completes the run. A reply with
  neither fails it ("no visible answer").

Prompts (`agent/prompts.py`) state the role, the tools, and honesty rules: cite result IDs,
label predictions, no certainty while more than one explanation fits, no likelihood percentages,
simulation is not a physical measurement, short answers. Case prompts (investigate and hint)
also carry public facts from trusted data, identical for every hidden condition of a family:
the intended design's family and topology (`views.family_text`, built from the template graph,
e.g. "RC high-pass filter: a capacitor (C1) from Input to Output and a resistor (R1) from Output
to Ground. …"), the hypothesis catalog from `PublicCase.hypotheses` (`id: label`, including
`c1_disconnected: C1 is disconnected (open)`), and how to read floored values (below). The
explore prompt describes both families from the same template data plus the current revision's
family, and tells the model to explain an infeasible goal with the numbers the tools returned
(predictions from `set_goal`/`suggest_design`, or a cited measured experiment), never invented
ones. Prompts do **not** contain an investigative strategy, such as which instrument separates
R from C: a test asserts that no instrument kind or instrument phrase appears in either case
prompt. The model has to choose from the instrument descriptions, so the comparison against the
fixed-experiment baseline stays valid. The learner's optional `message` is the user turn. Defaults are "Investigate this case."
and "Give me one useful hint." It is treated as untrusted text and cannot change tools, budget
or caps.

## What the model sees (verification V-01)

`agent/views.py` is an explicit allow-list, and its module docstring documents the exact schema.
In summary:
- `get_case` returns status, mode, hints used, budget, and the intended design (IDs, kinds,
  nominal values, trusted topology text, the family summary in its `label`, the family's own
  test tones, labeled analytical cutoff). It also returns
  instruments (kind, title, description, returns, grid summary, probes), the hypothesis catalog,
  the tolerances, observations (briefs, plus the symptom's tone levels), and summaries of the
  diagnosis and repair. Its `available_actions` (and those in the `submit_diagnosis` result)
  come from `CaseService.available_actions_for_run`, which lists what the owning run may do,
  filtered to actions the agent has tools for. The public `available_actions` describe the
  learner's options and are empty while a run owns the case (verification B-04).
- `read_observation` and `run_experiment` return sweeps (31 rows, at most 4 significant figures,
  with intended-design reference columns) and tone levels including expected values.
- **Measurement floors.** A magnitude at or below the declared floor (gain ≤ −120 dB, current
  ≤ 1e-6 mA; `challenges.instruments`) is rendered as the string `"below floor"` and its phase as
  `"undefined"`, in measured and reference cells alike. The model never sees the −200 dB clamp
  or the published 0° of a zero phasor as a number. Every sweep `reference` and transient `note`
  explains the markers in constant text. This happens with an open capacitor: a high-pass
  outputs nothing and draws no current; a low-pass passes the input (0 dB and 0° are real
  readings and stay numbers) but draws no current. A floored reading changes only measured cell
  values, never keys or list lengths, so it adds no structural side channel (tested; the
  verification differential masks exactly those cells). A repair verification without
  measurable output reports `max_gain_deviation_db` as "not defined: …" instead of a deviation
  from the clamp.
- `assess_evidence` (INVESTIGATE only) returns hypothesis statuses, `remaining`, and the
  deterministic statement. Because these come only from this tool, the event log and the
  evaluation can see that they were used.
- Never sent: `reveal`, seeds, `distinguishing_instruments`, waveform arrays, audio clips,
  artifact IDs/URLs, settings echoes. Views of a closed case raise `ClosedCaseError`, and every
  case tool first calls `CaseService.is_closing_or_closed` and refuses (fatally) when it is true.

## Events and streaming

Events are emitted only when something actually happens. They are `run_started`, then `status`
for real stage transitions ("Waiting for Nemotron", "Running source-current sweep", "retrying
once"), then `tool_call` (validated arguments as executed, at most 2 KB, long strings cut with
"…"; refused calls show `{}`) and `tool_result`. A run also emits `message` (visible text only,
with validated citations), `proposal`, `diagnosis`, `repair_proposal`, and `usage` (measured,
after every model call). It ends with `error` (failed or limit_reached) and finally
`run_finished`. `seq` starts at 1. The log is sealed after `run_finished`: anything a cancelled
task tries to emit afterwards is dropped.

- `GET /api/agent-runs/{id}/events` is SSE (`event` = type, `id` = seq, `data` = event JSON,
  `retry: 2000` on the first frame). It replays from `Last-Event-ID` and ends after
  `run_finished`. FastAPI inserts `: ping` keep-alive comments when idle. Unknown or foreign runs
  get a JSON 404 before streaming starts.
- `GET /api/agent-runs/{id}/events/log` returns the same events as JSON.
- `GET /api/agent-runs/{id}` returns the run, its limits and measured usage. `usage.latency_ms`
  is the summed wall time of model calls. A run persisted as `running` but no longer in memory
  (crash or restart) is reported, and re-saved, as `failed`, and its case is released
  (`release_agent`; idempotent). The service also registers `is_live(run_id)` with
  `CaseService.register_run_liveness`, so the CaseService releases a case owned by a run that
  is not alive in this process when it next loads it (verification B-02).
- Runs are session-scoped. Another session's run ID gets the same 404 as a missing one.

**Citations.** Every `obs_…`/`exp_…` token in visible text is checked against this case's
successful observations (case runs) or this session's successful experiments (explore runs).
Valid IDs become `EvidenceRef`s with a `QuantityName`: `output_gain_db`, `source_current_ma`,
`tone_gain_db` or `tone_current_ma` by observation kind and probe, and `output_gain_db` for
explore experiments. Unknown IDs are replaced in the text by "unverified citation" and a
`status` event says so. They never become evidence.

## Cancellation and close ordering

- `POST /api/agent-runs/{id}/cancel` sets the run's `CancelToken` and stop event. In-flight
  inference is cancelled immediately, which aborts the HTTP request. An in-flight tool gets up
  to 5 s to stop cooperatively: the CaseService and explore simulations honor the token and
  record nothing as counted. After that the task is cancelled. The endpoint waits up to 5 s and
  returns the run.
- The agent registers `cancel_and_wait(run_id)` with the CaseService at startup, and
  `close_case` awaits it before revealing. It requests a stop, waits about 6 s, then cancels the
  task and waits about 2.5 s. This stays inside the CaseService's own
  `CASE_CLOSE_AGENT_WAIT_S = 10 s` bound. If the run is still not finished, it is finalized as
  `cancelled` on the spot, even if the caller gives up, so that by the time the canceller
  returns:
  - the case has been released;
  - `run_finished` has been emitted;
  - the event log is sealed.

  No event of that run can follow the reveal.
- `hints_used` counts hints actually delivered (verification B-05, lead's definition). A hint
  run is checked at start (Guide me mode, `claim_for_agent(kind="hint")`), but
  `CaseService.record_hint` is called only after the run completes with a visible answer, before
  the case is released. Failed, cancelled or limit-stopped hint runs are not counted. Runs on
  closed cases are refused (409).

## Explore proposals, suggestions and Learn attempts

`propose_values` parses values with `circuits.templates.parse_component_value`. It validates
bounds and the joint cutoff by building, but not storing, a preview revision. It then stores an
`EditProposal` (kind `"proposal"`, status `pending`). Its `PredictedToneGain`s are analytical
(`simulation.analytic`, basis `analytical_first_order_rc`) for the test tones and goal
frequencies. `predicted_goal_pass` is computed against the session goal at proposal time (the
tool result names that goal). The agent never applies a proposal. The learner applies it through `POST /api/revisions` with `proposal_id`
(`circuits.revisions`), or dismisses it with `POST /api/proposals/{id}/dismiss`. Dismiss is
idempotent and returns 409 if the proposal was already applied.

Family switch: `propose_values(template_id="rc_highpass", …)` on a low-pass revision (or the
reverse) must list both R1 and C1; otherwise the tool returns a validation error and stores
nothing. The preview is a revision of the target template linked to the base, so predictions use
the target family's equations and its own test tones (50 Hz hum + 440 Hz tone for the
high-pass). The stored `EditProposal.template_id` names the target; each change's "from" value is
the base revision's part with the same designator. Applying it creates a revision of the target
template whose `parent_id` is the base (undo). A `template_id` equal to the current family is an
ordinary proposal (`template_id = null`).

`suggest_design(vary)` calls `explore.suggest.suggest_design(current revision, session goal,
vary=…)`: target cutoff, the nearest E12 part for the varied component (the other unchanged),
predicted gains and margins, and, when the goal is infeasible or rounding misses, the predicted
tradeoff text. The result is labeled an analytical prediction with `applied: false`; it creates
no proposal or revision. To offer it, the model calls `propose_values` with the suggested values.

`read_learn_attempt(attempt_id?)` reads a recorded Learn attempt of this session (the latest if
no ID is given) from the document store: the change, the learner's predictions and own
reasoning text, the measured per-tone deltas, both experiment IDs (citable), the score, and the
deterministic explanation and tradeoff. Its audio comparison is never included.

## Tests

Mock-transport and fake-service tests (TEST-ONLY doubles in `tests/agent/agent_testkit.py`).
Run from `services/api`:

```
RESONANCE_REQUIRE_NGSPICE=1 uv run pytest tests/agent
uv run ruff check resonance/agent resonance/api/routes_agent.py tests/agent
uv run mypy resonance
```

What the tests cover:

| Area | Covered |
|---|---|
| Caps | model calls, tool calls, measured tokens, experiments, wall time; missing usage fails closed |
| Retries | transient errors only; 400/401/404 are never retried |
| Tool calls | invalid arguments never executed (a 422 does not use the diagnosis); one tool call per reply |
| Tool-set separation | exact tool names per kind; hint cannot experiment or submit; explore cannot touch the case service; schemas are disjoint |
| Cancellation | mid-inference and mid-simulation (uncounted); close before reveal with sealed events; refusal while closing; 409 on closed cases |
| Text hygiene | reasoning never in events or later prompts; citation validation |
| Streaming | SSE framing, Last-Event-ID replay and keep-alives; session-scoped 404s |
| Unavailability | AI unavailable gives 503 with no run created; placeholder key treated as missing |
| Secrets | no secret in events, records, HTTP payloads or logs |
| Views | allow-list contents and rounding; family/topology text and the six-hypothesis catalog for both families; floors for open-capacitor sweeps and tone captures (high-pass and low-pass); no structural side channel; repair without measurable output |
| Prompts | family, topology and catalog from trusted data for both families and both case kinds; no instrument names (no embedded strategy) |
| Port typing | `CaseServicePort` matches `CaseService` (names, kinds, defaults, async-ness, resolved type hints; `claim_for_agent(kind)` is `investigate`/`hint` only); mypy also checks it statically via `ports._case_service_conforms` |
| Explore | previewed (never applied) proposals; `set_goal`; experiments only on the learner's revisions; `suggest_design` (feasible and infeasible goals, numbers checked against an independent formula); family-switch proposals (both components required, target-family predictions checked independently, applicable by the learner with an undo link); `read_learn_attempt` (latest, by ID, unknown; no audio; cited experiment) |
| Real open capacitor | real CaseService + evaluator + ngspice, high-pass `c1_disconnected`: only floor markers reach the model, no −200 dB anywhere in the requests |
| Integration | real CaseService, evaluator and ngspice: nothing hidden in model requests; close cancels the run before the reveal |
| Verification findings | B-04 (agent sees the actions its own run may take), B-05 (failed or cancelled hint runs not counted), B-02 (case of a lost run released; liveness predicate) |

## Live verification results (2026-10-25)

These come from `RESONANCE_LIVE_AI=1 uv run pytest tests/agent/test_agent_live.py -s -m live_ai`
on the development host, using the owner's key. The model is
`nvidia/nemotron-3-super-120b-a12b` at `https://api.tokenfactory.nebius.com/v1/`.

- **Model listing.** `/v1/models` returned 30 models, including the pinned
  `nvidia/nemotron-3-super-120b-a12b`. The other Nemotron models listed were
  `NVIDIA-Nemotron-3-Nano-30B-A3B`, `Nemotron-3-Ultra-550b-a55b`, `Nemotron-3.5-Super-VL` and
  `Nemotron-3_5-Lightning`.
- **Raw tool-call round trip:**
  - HTTP 200 in 1.61 s, with `finish_reason=tool_calls` and one `get_case` call;
  - 360 total tokens, of which 8 were reasoning tokens;
  - reasoning is returned in separate `reasoning`/`reasoning_content` fields, with no
    `<think>` tag in `content`;
  - `parallel_tool_calls=false` was accepted (HTTP 200).
- **Full investigate runs** against the TEST-ONLY fake case, whose hidden fault is R1
  doubled. These use genuine inference but not the real evaluator.

  | Run | Model calls | Tool calls | Total tokens | Wall time | Diagnosis |
  |---|---|---|---|---|---|
  | 1 | 7 | 6 | 39,945 | 17.7 s | `r1_increased` |
  | 2 | 9 | 8 | 59,212 | 26.7 s | `r1_increased` |

  - Run 1 measured voltage and then current, and assessed evidence once. It submitted a
    diagnosis citing three observations and proposed a repair.
  - Run 2 ran three experiments, assessing evidence after each, and submitted a diagnosis
    citing an output-gain reading and a source-current reading.
  - Both diagnoses match the fake case's hidden fault.

  Two runs on a single fixture are not an accuracy measurement. The held-out comparison with
  the baseline (`evals/run_nemotron.py`) has not been run yet.

## Live verification procedure

1. Put `NEBIUS_API_KEY` and `NEBIUS_MODEL=nvidia/nemotron-3-super-120b-a12b` in the repo `.env`
   (never commit it).
2. Check presence without printing the key:
   `uv run python -c "from resonance.config import Settings; s=Settings(); print(bool(s.nebius_api_key), s.nebius_model)"`
3. Run, in PowerShell: `$env:RESONANCE_LIVE_AI='1'; uv run pytest tests/agent/test_agent_live.py -s -m live_ai`.
   The tests do three things:
   - confirm the model ID is listed by `/v1/models`;
   - make one raw tool-call request and print the message field names (never reasoning text),
     whether `<think>` appears in `content`, `finish_reason`, `usage` and latency, and whether
     `parallel_tool_calls=false` is accepted;
   - run a full investigate run with genuine inference against the TEST-ONLY fake case and
     print status, tools used, usage and wall time.
4. Record the observed values here: model list, latency, usage, reasoning shape and
   parallel-tool-calls acceptance. If the reasoning shape requires it, set the reasoning
   switch, or decide `low_effort`, in `NebiusChatClient(extra_body=...)`, and pin the model ID
   in deployment configuration.

5. Then run the Nemotron evaluation from `services/api` (real inference and cost): the small
   development smoke run first, then the held-out set:
   `uv run python ../../evals/run_nemotron.py --set dev_v1 --limit 2`, then
   `uv run python ../../evals/run_nemotron.py`. See `evals/README.md` for reporting.

Results: **none yet**. No key was available when this was written; no live call, tool-call
round trip, latency, usage or Nemotron evaluation result exists.
