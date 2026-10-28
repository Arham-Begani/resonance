"""Bounded tool loop through AgentService with scripted provider responses (TEST-ONLY).

Covers: validated tool execution, tool-set separation, caps (model calls, tool calls, tokens,
experiments, wall time), retries for transient errors only, cancellation mid-inference and
mid-tool, close ordering, hidden-reasoning removal, citation validation, and secrets.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

import pytest
from agent_testkit import (
    SECRET_SEED,
    SESSION,
    TEST_API_KEY,
    FakeCaseService,
    Hang,
    ScriptedProvider,
    Status,
    abstain,
    completion,
    last_tool_result,
    make_service,
    wait_finished,
)

from resonance.agent.service import AgentService, limits_for
from resonance.contracts.agent import AgentRun, AgentUsage, CaseAgentRunRequest
from resonance.store.db import DocumentStore

INVESTIGATE = CaseAgentRunRequest(kind="investigate")
HINT = CaseAgentRunRequest(kind="hint")


def events(service: AgentService, run_id: str) -> list[Any]:
    return list(service.handle_for(SESSION, run_id).events)


def types(service: AgentService, run_id: str) -> list[str]:
    return [e.type for e in events(service, run_id)]


def finished_run(service: AgentService, run_id: str) -> AgentRun:
    return service.get_run(SESSION, run_id)


def all_event_json(service: AgentService, run_id: str) -> str:
    return "\n".join(e.model_dump_json() for e in events(service, run_id))


async def start(
    service: AgentService, cases: FakeCaseService, body: CaseAgentRunRequest = INVESTIGATE
) -> str:
    run = await service.start_case_run(SESSION, cases.case_id, body)
    await wait_finished(service, run.id)
    return run.id


# --------------------------------------------------------------------------------------
# Happy path and genuine events
# --------------------------------------------------------------------------------------


async def test_investigation_runs_tools_and_records_measured_usage(settings, store) -> None:
    cases = FakeCaseService(SESSION, mode="agent")

    def diagnose(body: dict[str, Any]) -> dict[str, Any]:
        obs_id = last_tool_result(body)["observation_id"]
        return completion(
            None,
            [
                (
                    "submit_diagnosis",
                    {
                        "conclusion": "fault",
                        "hypothesis_id": "r1_increased",
                        "citations": [{"observation_id": obs_id, "quantity": "source_current_ma"}],
                        "explanation": "High-frequency current is half the expected value.",
                    },
                )
            ],
        )

    def summarize(body: dict[str, Any]) -> dict[str, Any]:
        obs_id = next(
            json.loads(m["content"])["observation_id"]
            for m in body["messages"]
            if m["role"] == "tool" and "observation_id" in json.loads(m["content"])
        )
        return completion(f"The source current is about half the expected value [{obs_id}].")

    provider = ScriptedProvider(
        [
            completion(None, [("get_case", {})]),
            completion(
                "I'll measure the source current.",
                [("run_experiment", {"instrument": "source_current_sweep"})],
            ),
            diagnose,
            completion(None, [("propose_repair", {"component_id": "R1"})]),
            summarize,
        ]
    )
    service = make_service(settings, store, provider, cases)
    run_id = await start(service, cases)

    run = finished_run(service, run_id)
    assert run.status == "completed", run.error
    assert run.source == "nebius"
    assert run.usage.model_calls == 5
    assert run.usage.tool_calls == 4
    assert run.usage.prompt_tokens == 5 * 120
    assert run.usage.total_tokens == 5 * 150
    assert cases.active_run_id is None, "the case must be released"
    # The experiment was requested as the agent, by this run, with a fresh idempotency key.
    request, requested_by, agent_run_id = cases.experiment_requests[0]
    assert (requested_by, agent_run_id) == ("agent", run_id)
    assert request.client_request_id and 8 <= len(request.client_request_id) <= 64

    seq = [e.seq for e in events(service, run_id)]
    assert seq == list(range(1, len(seq) + 1))
    kinds = types(service, run_id)
    assert kinds[0] == "run_started" and kinds[-1] == "run_finished"
    assert "diagnosis" in kinds and "repair_proposal" in kinds
    assert cases.repair_proposal is not None and cases.repair_proposal.component_id == "R1"
    assert not any("reminder" in e.text for e in events(service, run_id) if e.type == "status")
    assert "Running source-current sweep" in [
        e.text for e in events(service, run_id) if e.type == "status"
    ]
    final = [e for e in events(service, run_id) if e.type == "message"][-1]
    assert [c.result_id for c in final.citations] == [cases.observations[1].id]
    assert final.citations[0].quantity == "source_current_ma"
    assert run.final_message == final.text
    # Persisted record matches the in-memory one.
    persisted = store.get("agent_run", run_id, SESSION, AgentRun)
    assert persisted is not None and persisted.status == "completed"
    # Tools offered to the model are exactly the INVESTIGATE set.
    assert provider.tool_names() == {
        "get_case",
        "assess_evidence",
        "run_experiment",
        "read_observation",
        "submit_diagnosis",
        "propose_repair",
    }


async def test_investigate_run_chooses_agent_mode_when_unset(settings, store) -> None:
    cases = FakeCaseService(SESSION, mode=None)
    provider = ScriptedProvider([abstain(), completion("Ready.")])
    service = make_service(settings, store, provider, cases)
    run_id = await start(service, cases)
    assert "set_mode:agent" in cases.calls
    assert finished_run(service, run_id).status == "completed"


# --------------------------------------------------------------------------------------
# Validation and tool-set separation
# --------------------------------------------------------------------------------------


async def test_invalid_arguments_are_rejected_without_execution(settings, store) -> None:
    cases = FakeCaseService(SESSION, mode="agent")
    provider = ScriptedProvider(
        [
            completion(None, [("run_experiment", {"instrument": "transient_capture"})]),
            completion(None, [("run_experiment", "{not json")]),
            completion(None, [("run_experiment", {"instrument": "voltmeter"})]),
            completion(
                None,
                [("submit_diagnosis", {"conclusion": "fault", "explanation": "R1 doubled"})],
            ),
            completion("I could not run anything."),
            completion("I still cannot conclude anything."),  # ignores the one reminder
        ]
    )
    service = make_service(settings, store, provider, cases)
    run_id = await start(service, cases)

    assert "run_experiment" not in cases.calls
    assert "submit_diagnosis" not in cases.calls, "a 422 must not use the single diagnosis"
    results = [e for e in events(service, run_id) if e.type == "tool_result"]
    assert len(results) == 4 and not any(r.ok for r in results)
    assert all(r.error.code == "validation_error" for r in results)
    calls = [e for e in events(service, run_id) if e.type == "tool_call"]
    assert all(c.arguments == {} for c in calls), "refused calls echo no arguments"
    # The validation error went back to the model as the tool result.
    assert last_tool_result(provider.requests[1])["ok"] is False
    assert finished_run(service, run_id).status == "completed"


async def test_hint_run_cannot_experiment_or_submit(settings, store) -> None:
    cases = FakeCaseService(SESSION, mode="guide")
    provider = ScriptedProvider(
        [
            completion(None, [("run_experiment", {"instrument": "source_current_sweep"})]),
            completion(None, [("submit_diagnosis", {"conclusion": "no_fault"})]),
            completion("What do you expect the output sweep to show?"),
        ]
    )
    service = make_service(settings, store, provider, cases)
    run_id = await start(service, cases, HINT)

    assert provider.tool_names() == {"get_case", "read_observation"}
    assert "run_experiment" not in cases.calls and "submit_diagnosis" not in cases.calls
    assert cases.hints_used == 1 and "record_hint" in cases.calls
    results = [e for e in events(service, run_id) if e.type == "tool_result"]
    assert [r.ok for r in results] == [False, False]
    assert "not available in this mode" in results[0].error.message
    assert finished_run(service, run_id).status == "completed"


async def test_hint_refused_outside_guide_mode(settings, store) -> None:
    from resonance.api.errors import ApiException

    cases = FakeCaseService(SESSION, mode="agent")
    provider = ScriptedProvider([completion("unused")])
    service = make_service(settings, store, provider, cases)
    with pytest.raises(ApiException) as info:
        await service.start_case_run(SESSION, cases.case_id, HINT)
    assert info.value.status == 409
    assert provider.requests == [] and cases.active_run_id is None


async def test_only_one_tool_call_per_reply_is_executed(settings, store) -> None:
    cases = FakeCaseService(SESSION, mode="agent")
    provider = ScriptedProvider(
        [
            completion(
                None,
                [
                    ("run_experiment", {"instrument": "output_voltage_sweep"}),
                    ("run_experiment", {"instrument": "source_current_sweep"}),
                ],
            ),
            completion("Done."),
        ]
    )
    service = make_service(settings, store, provider, cases)
    run_id = await start(service, cases)

    assert cases.calls.count("run_experiment") == 1
    results = [e for e in events(service, run_id) if e.type == "tool_result"]
    assert [r.ok for r in results] == [True, False]
    assert "only one tool call per reply" in results[1].error.message
    tool_messages = [m for m in provider.requests[1]["messages"] if m["role"] == "tool"]
    assert len(tool_messages) == 2, "every tool call id gets a result"


# --------------------------------------------------------------------------------------
# Caps
# --------------------------------------------------------------------------------------


async def test_model_call_cap(settings, store) -> None:
    cases = FakeCaseService(SESSION, mode="agent")
    provider = ScriptedProvider([completion(None, [("get_case", {})]) for _ in range(10)])
    service = make_service(settings, store, provider, cases, limits={"max_model_calls": 3})
    run_id = await start(service, cases)
    run = finished_run(service, run_id)
    assert run.status == "limit_reached"
    assert run.error is not None and "3 model calls" in run.error.message
    assert len(provider.requests) == 3
    assert "error" in types(service, run_id)


async def test_tool_call_cap(settings, store) -> None:
    cases = FakeCaseService(SESSION, mode="agent")
    provider = ScriptedProvider([completion(None, [("get_case", {})]) for _ in range(10)])
    service = make_service(settings, store, provider, cases, limits={"max_tool_calls": 2})
    run_id = await start(service, cases)
    run = finished_run(service, run_id)
    assert run.status == "limit_reached" and "2 tool calls" in run.error.message
    assert run.usage.tool_calls == 2


async def test_token_cap_uses_measured_usage(settings, store) -> None:
    cases = FakeCaseService(SESSION, mode="agent")
    provider = ScriptedProvider(
        [completion(None, [("get_case", {})], usage=(30_000, 1_000)) for _ in range(5)]
    )
    service = make_service(settings, store, provider, cases)
    run_id = await start(service, cases)
    run = finished_run(service, run_id)
    assert run.status == "limit_reached" and "60000 tokens" in run.error.message
    assert run.usage.total_tokens == 62_000
    assert len(provider.requests) == 2
    # The per-call completion allowance never exceeds what is left of the token cap.
    assert provider.requests[1]["max_tokens"] <= 60_000 - 31_000


async def test_missing_usage_fails_closed(settings, store) -> None:
    cases = FakeCaseService(SESSION, mode="agent")
    provider = ScriptedProvider([completion("Hello", usage=None)])
    service = make_service(settings, store, provider, cases)
    run_id = await start(service, cases)
    run = finished_run(service, run_id)
    assert run.status == "failed" and "token usage" in run.error.message


async def test_experiment_cap_refuses_extra_experiments(settings, store) -> None:
    cases = FakeCaseService(SESSION, mode="agent")
    provider = ScriptedProvider(
        [
            completion(None, [("run_experiment", {"instrument": "output_voltage_sweep"})]),
            completion(None, [("run_experiment", {"instrument": "source_current_sweep"})]),
            completion("Stopping."),
        ]
    )
    service = make_service(settings, store, provider, cases, limits={"max_experiments": 1})
    run_id = await start(service, cases)
    assert cases.calls.count("run_experiment") == 1
    results = [e for e in events(service, run_id) if e.type == "tool_result"]
    assert results[1].error.code == "limit_reached"


async def test_wall_time_cap_stops_inference(settings, store) -> None:
    cases = FakeCaseService(SESSION, mode="agent")
    provider = ScriptedProvider([Hang(30)])
    service = make_service(settings, store, provider, cases, limits={"wall_time_s": 0.3})
    started = time.monotonic()
    run_id = await start(service, cases)
    assert time.monotonic() - started < 5
    run = finished_run(service, run_id)
    assert run.status == "limit_reached" and "time limit" in run.error.message
    assert provider.cancelled == 1, "the in-flight HTTP request was cancelled"
    assert cases.active_run_id is None


# --------------------------------------------------------------------------------------
# Retries
# --------------------------------------------------------------------------------------


async def test_transient_error_is_retried_once(settings, store, monkeypatch) -> None:
    monkeypatch.setattr("resonance.agent.loop.RETRY_BACKOFF_S", 0.0)
    cases = FakeCaseService(SESSION, mode="agent")
    provider = ScriptedProvider([Status(503), abstain(), completion("Recovered.")])
    service = make_service(settings, store, provider, cases)
    run_id = await start(service, cases)
    run = finished_run(service, run_id)
    assert run.status == "completed"
    assert len(provider.requests) == 3
    assert any("retrying" in e.text for e in events(service, run_id) if e.type == "status")


async def test_retry_cap_then_failure(settings, store) -> None:
    cases = FakeCaseService(SESSION, mode="agent")
    provider = ScriptedProvider([Status(429), Status(500), completion("never")])
    service = make_service(settings, store, provider, cases)
    run_id = await start(service, cases)
    run = finished_run(service, run_id)
    assert run.status == "failed" and run.error.code == "ai_error"
    assert len(provider.requests) == 2


@pytest.mark.parametrize("code", [400, 401, 404])
async def test_non_transient_errors_are_not_retried(settings, store, code: int) -> None:
    cases = FakeCaseService(SESSION, mode="agent")
    provider = ScriptedProvider([Status(code), completion("never")])
    service = make_service(settings, store, provider, cases)
    run_id = await start(service, cases)
    run = finished_run(service, run_id)
    assert run.status == "failed" and run.error.code == "ai_error"
    assert len(provider.requests) == 1
    assert TEST_API_KEY not in run.error.message


# --------------------------------------------------------------------------------------
# Cancellation and close ordering
# --------------------------------------------------------------------------------------


async def test_cancel_during_inference(settings, store) -> None:
    cases = FakeCaseService(SESSION, mode="agent")
    provider = ScriptedProvider([Hang(30)])
    service = make_service(settings, store, provider, cases)
    run = await service.start_case_run(SESSION, cases.case_id, INVESTIGATE)
    await asyncio.wait_for(provider.started.wait(), 5)
    cancelled = await service.cancel(SESSION, run.id)
    assert cancelled.status == "cancelled"
    assert provider.cancelled == 1
    assert cases.active_run_id is None
    assert types(service, run.id)[-1] == "run_finished"


async def test_cancel_during_simulation_stops_it_cooperatively(settings, store) -> None:
    cases = FakeCaseService(SESSION, mode="agent")
    cases.experiment_gate = asyncio.Event()  # never opened: the experiment runs until cancelled
    provider = ScriptedProvider(
        [completion(None, [("run_experiment", {"instrument": "source_current_sweep"})])]
    )
    service = make_service(settings, store, provider, cases)
    run = await service.start_case_run(SESSION, cases.case_id, INVESTIGATE)
    await asyncio.wait_for(cases.experiment_started.wait(), 5)
    cancelled = await service.cancel(SESSION, run.id)
    assert cancelled.status == "cancelled"
    assert cases.budget_used == 0, "a cancelled experiment is not counted"
    result = [e for e in events(service, run.id) if e.type == "tool_result"][-1]
    assert result.ok is False and result.error.code == "cancelled"


async def test_close_stops_run_before_reveal_and_seals_events(settings, store) -> None:
    cases = FakeCaseService(SESSION, mode="agent")
    provider = ScriptedProvider([Hang(30), completion(None, [("get_case", {})])])
    service = make_service(settings, store, provider, cases)
    run = await service.start_case_run(SESSION, cases.case_id, INVESTIGATE)
    await asyncio.wait_for(provider.started.wait(), 5)
    closed = await cases.close_case(SESSION, cases.case_id)
    assert closed.reveal is not None
    handle = service.handle_for(SESSION, run.id)
    assert not handle.running and handle.run.status == "cancelled"
    count = len(handle.events)
    revealed_at = closed.reveal.revealed_at
    assert all(e.at <= revealed_at for e in handle.events)
    await asyncio.sleep(0.05)
    assert len(handle.events) == count, "no event may follow the reveal"
    # The model never received anything from after the close.
    assert SECRET_SEED not in json.dumps(provider.requests)


async def test_tools_refuse_once_the_case_is_closing(settings, store) -> None:
    cases = FakeCaseService(SESSION, mode="agent")
    provider = ScriptedProvider([completion(None, [("get_case", {})]), completion("x")])
    service = make_service(settings, store, provider, cases)
    run = await service.start_case_run(SESSION, cases.case_id, INVESTIGATE)
    cases.closing = True  # close has begun (the canceller has not been called yet)
    await wait_finished(service, run.id)
    result = [e for e in events(service, run.id) if e.type == "tool_result"][0]
    assert result.ok is False and "closing or closed" in result.error.message
    assert finished_run(service, run.id).status == "failed"
    assert len(provider.requests) == 1


async def test_runs_on_closed_cases_are_refused(settings, store) -> None:
    from resonance.api.errors import ApiException

    cases = FakeCaseService(SESSION, mode="agent")
    await cases.close_case(SESSION, cases.case_id)
    provider = ScriptedProvider([completion("unused")])
    service = make_service(settings, store, provider, cases)
    with pytest.raises(ApiException) as info:
        await service.start_case_run(SESSION, cases.case_id, INVESTIGATE)
    assert info.value.status == 409
    assert provider.requests == []


# --------------------------------------------------------------------------------------
# Visible text hygiene
# --------------------------------------------------------------------------------------


async def test_hidden_reasoning_never_surfaces(settings, store) -> None:
    cases = FakeCaseService(SESSION, mode="agent")
    provider = ScriptedProvider(
        [
            completion(
                "<think>SECRET-REASONING-A</think>Let me read the case.",
                [("get_case", {})],
                reasoning="SECRET-REASONING-B",
            ),
            completion("SECRET-REASONING-C</think>Here is what we know.", reasoning="x"),
        ]
    )
    service = make_service(settings, store, provider, cases)
    run_id = await start(service, cases)
    dumped = all_event_json(service, run_id)
    assert "SECRET-REASONING" not in dumped
    messages = [e.text for e in events(service, run_id) if e.type == "message"]
    assert messages == ["Let me read the case.", "Here is what we know."]
    # Reasoning is not sent back to the model either.
    assert "SECRET-REASONING" not in json.dumps(provider.requests[1]["messages"])


async def test_citations_are_validated_against_recorded_results(settings, store) -> None:
    cases = FakeCaseService(SESSION, mode="agent")
    symptom = cases.observations[0].id
    provider = ScriptedProvider(
        [completion(f"The whistle is quieter [{symptom}], see also [obs_inventedXYZ123].")]
    )
    service = make_service(settings, store, provider, cases)
    run_id = await start(service, cases)
    message = next(e for e in events(service, run_id) if e.type == "message")
    assert [c.result_id for c in message.citations] == [symptom]
    assert message.citations[0].quantity == "tone_gain_db"
    assert "obs_inventedXYZ123" not in message.text
    assert "[unverified citation]" in message.text
    assert any("unverified" in e.text for e in events(service, run_id) if e.type == "status")


async def test_learner_message_is_the_user_turn(settings, store) -> None:
    cases = FakeCaseService(SESSION, mode="guide")
    provider = ScriptedProvider([completion("Think about what current tells you.")])
    service = make_service(settings, store, provider, cases)
    run = await service.start_case_run(
        SESSION, cases.case_id, CaseAgentRunRequest(kind="hint", message="Why would current help?")
    )
    await wait_finished(service, run.id)
    first = provider.requests[0]["messages"]
    assert first[0]["role"] == "system" and first[1] == {
        "role": "user",
        "content": "Why would current help?",
    }


# --------------------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------------------


async def test_no_secret_in_events_records_or_logs(
    settings, store: DocumentStore, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    cases = FakeCaseService(SESSION, mode="agent")
    provider = ScriptedProvider([Status(401)])
    service = make_service(settings, store, provider, cases)
    run_id = await start(service, cases)
    persisted = store.get("agent_run", run_id, SESSION, AgentRun)
    assert persisted is not None
    blobs = [all_event_json(service, run_id), persisted.model_dump_json(), caplog.text]
    for blob in blobs:
        assert TEST_API_KEY not in blob
    assert "Bearer" not in caplog.text


# --------------------------------------------------------------------------------------
# Verification findings B-04 / B-05 / B-02
# --------------------------------------------------------------------------------------


async def test_agent_is_told_the_actions_its_own_run_may_take(settings, store) -> None:
    """B-04: the public available_actions are the learner's (empty while a run owns the
    case); the agent sees what the CaseService says the owning run may do."""
    cases = FakeCaseService(SESSION, mode="agent")

    def diagnose(body: dict[str, Any]) -> dict[str, Any]:
        obs_id = last_tool_result(body)["observation_id"]
        citation = {"observation_id": obs_id, "quantity": "source_current_ma"}
        return completion(
            None,
            [
                (
                    "submit_diagnosis",
                    {
                        "conclusion": "fault",
                        "hypothesis_id": "r1_increased",
                        "citations": [citation],
                        "explanation": "Current is low.",
                    },
                )
            ],
        )

    provider = ScriptedProvider(
        [
            completion(None, [("get_case", {})]),
            completion(None, [("run_experiment", {"instrument": "source_current_sweep"})]),
            diagnose,
            completion("Done."),
        ]
    )
    service = make_service(settings, store, provider, cases)
    await start(service, cases)
    during = last_tool_result(provider.requests[1])["available_actions"]
    after_dx = last_tool_result(provider.requests[3])["available_actions"]
    assert during == ["run_experiment", "submit_diagnosis"]
    assert after_dx == ["propose_repair"]


async def test_failed_or_cancelled_hint_runs_are_not_counted(settings, store) -> None:
    """B-05: hints_used counts hints actually delivered (completed hint runs)."""
    cases = FakeCaseService(SESSION, mode="guide")
    service = make_service(settings, store, ScriptedProvider([Status(401)]), cases)
    run_id = await start(service, cases, HINT)
    assert finished_run(service, run_id).status == "failed"
    assert cases.hints_used == 0 and "record_hint" not in cases.calls

    provider = ScriptedProvider([Hang(30)])
    service = make_service(settings, store, provider, cases)
    run = await service.start_case_run(SESSION, cases.case_id, HINT)
    await asyncio.wait_for(provider.started.wait(), 5)
    assert (await service.cancel(SESSION, run.id)).status == "cancelled"
    assert cases.hints_used == 0

    service = make_service(settings, store, ScriptedProvider([completion("Predict it.")]), cases)
    await start(service, cases, HINT)
    assert cases.hints_used == 1


async def test_case_owned_by_a_lost_run_is_released(settings, store) -> None:
    """B-02: a run persisted as running but not live in this process (crash/restart) is
    reported as failed, its case is released, and the liveness predicate says it is dead."""
    cases = FakeCaseService(SESSION, mode="agent")
    lost = AgentRun(
        id="run_lostLOSTlost0001",
        kind="investigate",
        case_id=cases.case_id,
        status="running",
        model="test/model",
        created_at=datetime.now(UTC),
        limits=limits_for("investigate"),
        usage=AgentUsage(),
    )
    store.put("agent_run", lost.id, SESSION, lost)
    cases.active_run_id = lost.id
    service = make_service(settings, store, ScriptedProvider([Hang(30)]), cases)
    assert cases.liveness is not None and cases.liveness(lost.id) is False

    run = service.get_run(SESSION, lost.id)
    assert run.status == "failed" and "restarted" in run.error.message
    assert cases.active_run_id is None
    assert (await service.cancel(SESSION, lost.id)).status == "failed"

    live = await service.start_case_run(SESSION, cases.case_id, INVESTIGATE)
    assert cases.liveness(live.id) is True
    await service.cancel(SESSION, live.id)
    assert cases.liveness(live.id) is False
