# AI integration: Nemotron via Nebius Token Factory

Owner: investigation & AI engineer. Code: `services/api/resonance/agent/`,
`services/api/resonance/api/routes_agent.py`. Tests: `services/api/tests/agent/`.

## Status (2026-10-15)

| Item | Status |
|---|---|
| Bounded tool loop, three separated tool sets, event stream, cancellation, AI-unavailable path | Implemented and tested with scripted provider responses (TEST-ONLY `httpx.MockTransport`) |
| Integration with the real CaseService, evaluator and ngspice (provider scripted) | Tested (`tests/agent/test_agent_integration.py`) |
| Live Nebius call, model-ID confirmation, real tool-call round trip, latency, usage | **Pending: `NEBIUS_API_KEY` and `NEBIUS_MODEL` are not set in the project `.env`.** No live result is claimed anywhere in this document. |

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
| `explore` | `POST /api/explore/agent-runs {revision_id, message, goal_id?}` | `inspect_circuit(revision_id)`, `set_goal(GoalInput)`, `propose_values(changes, rationale)`, `run_experiment(revision_id)`, `compare_experiments(a_id, b_id)` |

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
simulation is not a physical measurement, short answers. They do **not** contain an
investigative strategy, such as which instrument separates R from C. The model has to choose
from the instrument descriptions, so the comparison against the fixed-experiment baseline stays
valid. The learner's optional `message` is the user turn. Defaults are "Investigate this case."
and "Give me one useful hint." It is treated as untrusted text and cannot change tools, budget
or caps.

## What the model sees (verification V-01)

`agent/views.py` is an explicit allow-list, and its module docstring documents the exact schema.
In summary:
- `get_case` returns status, mode, hints used, budget, and the intended design (IDs, kinds,
  nominal values, trusted topology text, test tones, labeled analytical cutoff). It also returns
  instruments (kind, title, description, returns, grid summary, probes), the hypothesis catalog,
  the tolerances, observations (briefs, plus the symptom's tone levels), and summaries of the
  diagnosis and repair. Its `available_actions` (and those in the `submit_diagnosis` result)
  come from `CaseService.available_actions_for_run`, which lists what the owning run may do,
  filtered to actions the agent has tools for. The public `available_actions` describe the
  learner's options and are empty while a run owns the case (verification B-04).
- `read_observation` and `run_experiment` return sweeps (31 rows, at most 4 significant figures,
  with intended-design reference columns) and tone levels including expected values.
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

## Explore proposals

`propose_values` parses values with `circuits.templates.parse_component_value`. It validates
bounds and the joint cutoff by building, but not storing, a preview revision. It then stores an
`EditProposal` (kind `"proposal"`, status `pending`). Its `PredictedToneGain`s are analytical
(`simulation.analytic`, basis `analytical_first_order_rc`) for the test tones and goal
frequencies. `predicted_goal_pass` is computed against the session goal. The agent never applies
a proposal. The learner applies it through `POST /api/revisions` with `proposal_id`
(`circuits.revisions`), or dismisses it with `POST /api/proposals/{id}/dismiss`. Dismiss is
idempotent and returns 409 if the proposal was already applied.

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
| Tool-set separation | hint cannot experiment or submit; explore cannot touch the case service; schemas are disjoint |
| Cancellation | mid-inference and mid-simulation (uncounted); close before reveal with sealed events; refusal while closing; 409 on closed cases |
| Text hygiene | reasoning never in events or later prompts; citation validation |
| Streaming | SSE framing, Last-Event-ID replay and keep-alives; session-scoped 404s |
| Unavailability | AI unavailable gives 503 with no run created; placeholder key treated as missing |
| Secrets | no secret in events, records, HTTP payloads or logs |
| Views | allow-list contents and rounding |
| Explore | previewed (never applied) proposals; `set_goal`; experiments only on the learner's revisions |
| Integration | real CaseService, evaluator and ngspice: nothing hidden in model requests; close cancels the run before the reveal |
| Verification findings | B-04 (agent sees the actions its own run may take), B-05 (failed or cancelled hint runs not counted), B-02 (case of a lost run released; liveness predicate) |

## Live verification procedure (pending a key)

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

Results: **none yet**. No key was available when this was written.
