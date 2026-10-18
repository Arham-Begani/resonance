"""Budget enforcement, idempotency, per-case serialization, retries, and cancellation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
from challenges_testkit import SESSION_A, AnalyticTestSimulator, Rig, seed_for
from fastapi.testclient import TestClient

from resonance.api.errors import ApiException
from resonance.contracts.common import ErrorCode
from resonance.contracts.internal import CancelToken, Simulator
from resonance.contracts.investigation import CaseExperimentRequest, CaseStatus, PublicCase

S = SESSION_A


def req(instrument: str, key: str, probe: str | None = None) -> CaseExperimentRequest:
    return CaseExperimentRequest.model_validate(
        {"instrument": instrument, "probe": probe, "client_request_id": key}
    )


async def observing_case(rig: Rig, fault: str = "c1_decreased") -> PublicCase:
    case = await rig.service.harness_create_case(S, seed_for(fault))  # type: ignore[arg-type]
    return await rig.service.set_mode(S, case.id, "guide")


async def test_exactly_three_counted_experiments(rig: Rig) -> None:
    case = await observing_case(rig)
    plan = [
        ("output_voltage_sweep", None),
        ("source_current_sweep", None),
        ("transient_capture", "source_current"),
    ]
    for i, (instrument, probe) in enumerate(plan):
        resp = await rig.service.run_experiment(
            S, case.id, req(instrument, f"budget-{i:04d}", probe), requested_by="user"
        )
        assert resp.observation.counted and resp.observation.sequence == i + 1
        assert resp.case.budget.used == i + 1
    assert resp.case.status == CaseStatus.OBSERVING
    assert "run_experiment" not in resp.case.available_actions
    # The budget is spent, so the distinguishing instruments are now published (V-13).
    assert resp.case.evidence.distinguishing_instruments is not None
    with pytest.raises(ApiException) as fourth:
        await rig.service.run_experiment(
            S, case.id, req("output_voltage_sweep", "budget-0004"), requested_by="user"
        )
    assert fourth.value.error.code == ErrorCode.BUDGET_EXHAUSTED
    after = rig.service.get_case(S, case.id)
    assert after.budget.used == 3 and len(after.observations) == 4


async def test_idempotency_keys(rig: Rig) -> None:
    case = await observing_case(rig)
    first = await rig.service.run_experiment(
        S, case.id, req("source_current_sweep", "same-key-1"), requested_by="user"
    )
    again = await rig.service.run_experiment(
        S, case.id, req("source_current_sweep", "same-key-1"), requested_by="user"
    )
    assert again.observation.id == first.observation.id
    assert again.case.budget.used == 1
    with pytest.raises(ApiException) as mismatch:
        await rig.service.run_experiment(
            S, case.id, req("output_voltage_sweep", "same-key-1"), requested_by="user"
        )
    assert mismatch.value.error.code == ErrorCode.CONFLICT
    assert mismatch.value.error.field == "client_request_id"
    for i in range(2):
        await rig.service.run_experiment(
            S, case.id, req("output_voltage_sweep", f"other-key-{i}"), requested_by="user"
        )
    # Budget spent, but a repeated key still returns its stored response.
    replayed = await rig.service.run_experiment(
        S, case.id, req("source_current_sweep", "same-key-1"), requested_by="user"
    )
    assert replayed.observation.id == first.observation.id
    assert rig.service.get_case(S, case.id).budget.used == 3


async def test_concurrent_requests_never_overspend(rig: Rig, double: AnalyticTestSimulator) -> None:
    case = await observing_case(rig)
    double.delay_s = 0.3

    async def attempt(i: int) -> str:
        try:
            await rig.service.run_experiment(
                S, case.id, req("output_voltage_sweep", f"conc-{i:04d}"), requested_by="user"
            )
            return "ok"
        except ApiException as exc:
            assert exc.error.code != ErrorCode.CONFLICT or exc.error.retryable
            return exc.error.code.value

    for round_no in range(3):
        results = await asyncio.gather(*(attempt(round_no * 10 + i) for i in range(5)))
        assert results.count("ok") == 1, results
        assert results.count("conflict") == 4, results
    results = await asyncio.gather(*(attempt(100 + i) for i in range(5)))
    assert results.count("ok") == 0
    final = rig.service.get_case(S, case.id)
    assert final.budget.used == 3
    assert sum(o.counted for o in final.observations) == 3


async def test_same_key_in_flight_is_a_retryable_conflict(
    rig: Rig, double: AnalyticTestSimulator
) -> None:
    case = await observing_case(rig)
    double.delay_s = 0.3
    task = asyncio.create_task(
        rig.service.run_experiment(
            S, case.id, req("output_voltage_sweep", "inflight-1"), requested_by="user"
        )
    )
    await asyncio.sleep(0.05)
    with pytest.raises(ApiException) as busy:
        await rig.service.run_experiment(
            S, case.id, req("output_voltage_sweep", "inflight-1"), requested_by="user"
        )
    assert busy.value.error.code == ErrorCode.CONFLICT and busy.value.error.retryable
    done = await task
    assert done.case.budget.used == 1


async def test_transient_failure_is_retried_once_without_extra_budget(
    rig: Rig, double: AnalyticTestSimulator
) -> None:
    case = await observing_case(rig)
    calls_before = len(double.calls)
    double.fail_next = 1
    resp = await rig.service.run_experiment(
        S, case.id, req("source_current_sweep", "flaky-0001"), requested_by="user"
    )
    assert resp.observation.status == "succeeded" and resp.case.budget.used == 1
    assert resp.case.budget.infrastructure_failures == 0
    assert len(double.calls) - calls_before == 4  # (hidden + reference) × 2 attempts


async def test_failed_retry_records_uncounted_failure_then_case_fails(
    rig: Rig, double: AnalyticTestSimulator
) -> None:
    case = await observing_case(rig)
    for n in range(1, 4):
        double.fail_next = 4  # both attempts fail (each attempt runs hidden + reference)
        with pytest.raises(ApiException) as err:
            await rig.service.run_experiment(
                S, case.id, req("source_current_sweep", f"fail-{n:04d}"), requested_by="user"
            )
        assert err.value.error.code == ErrorCode.SIMULATION_FAILED
        assert "No experiment budget was used" in err.value.error.message
        assert err.value.error.retryable is (n < 3)
        state = rig.service.get_case(S, case.id)
        assert state.budget.used == 0 and state.budget.infrastructure_failures == n
        failed = state.observations[-1]
        assert (failed.status, failed.counted, failed.sweep) == ("failed", False, None)
        assert failed.error is not None
    assert state.status == CaseStatus.FAILED
    assert state.available_actions == ["close"]
    double.fail_next = 0
    with pytest.raises(ApiException) as refused:
        await rig.service.run_experiment(
            S, case.id, req("source_current_sweep", "fail-0004"), requested_by="user"
        )
    assert refused.value.error.code == ErrorCode.CONFLICT
    closed = await rig.service.close_case(S, case.id)
    assert closed.reveal is not None
    assert closed.reveal.outcome.terminal_status == "failed"
    assert closed.reveal.outcome.experiments_used == 0


async def test_cancellation_consumes_no_budget(rig: Rig, double: AnalyticTestSimulator) -> None:
    case = await observing_case(rig)
    double.delay_s = 5.0
    token = CancelToken()
    task = asyncio.create_task(
        rig.service.run_experiment(
            S,
            case.id,
            req("output_voltage_sweep", "cancel-0001"),
            requested_by="user",
            cancel=token,
        )
    )
    await asyncio.sleep(0.05)
    token.cancel()
    with pytest.raises(ApiException) as err:
        await asyncio.wait_for(task, timeout=2.0)
    assert err.value.error.code == ErrorCode.CANCELLED
    state = rig.service.get_case(S, case.id)
    assert state.budget.used == 0 and state.budget.remaining == 3
    assert state.budget.infrastructure_failures == 0
    assert state.observations[-1].status == "cancelled"
    double.delay_s = 0.0  # the same key can run again after a cancellation
    resp = await rig.service.run_experiment(
        S, case.id, req("output_voltage_sweep", "cancel-0001"), requested_by="user"
    )
    assert resp.case.budget.used == 1


def test_http_validation_errors_do_not_consume_budget(
    app_client_factory: Callable[[Simulator], TestClient], double: AnalyticTestSimulator
) -> None:
    client = app_client_factory(double)
    created = client.post("/api/cases", json={})
    assert created.status_code == 201
    case_id = created.json()["id"]
    assert client.post(f"/api/cases/{case_id}/mode", json={"mode": "guide"}).status_code == 200
    bad_bodies = [
        {"instrument": "transient_capture", "client_request_id": "valid-key-1"},  # no probe
        {
            "instrument": "output_voltage_sweep",
            "probe": "output_voltage",
            "client_request_id": "valid-key-2",
        },  # probe on a sweep
        {"instrument": "output_voltage_sweep"},  # missing idempotency key
        {"instrument": "output_voltage_sweep", "client_request_id": "short"},
        {"instrument": "transient_capture", "probe": "v(in)", "client_request_id": "valid-key-3"},
        {"instrument": "output_voltage_sweep", "client_request_id": "valid-key-4", "node": "out"},
    ]
    for body in bad_bodies:
        r = client.post(f"/api/cases/{case_id}/experiments", json=body)
        assert r.status_code == 422, body
        assert r.json()["error"]["code"] == "validation_error"
    state = client.get(f"/api/cases/{case_id}").json()
    assert state["budget"]["used"] == 0 and len(state["observations"]) == 1
    ok = client.post(
        f"/api/cases/{case_id}/experiments",
        json={"instrument": "output_voltage_sweep", "client_request_id": "valid-key-1"},
    )
    assert ok.status_code == 201 and ok.json()["case"]["budget"]["used"] == 1


async def test_failure_messages_mention_the_retry_only_when_it_ran(
    rig: Rig, double: AnalyticTestSimulator
) -> None:
    """A-02: non-retryable errors are not retried, so the message must not claim a retry."""
    case = await observing_case(rig)
    double.unavailable = True
    calls_before = len(double.calls)
    with pytest.raises(ApiException) as unavailable:
        await rig.service.run_experiment(
            S, case.id, req("source_current_sweep", "noretry-01"), requested_by="user"
        )
    assert unavailable.value.error.code == ErrorCode.SIMULATOR_UNAVAILABLE
    assert "retry" not in unavailable.value.error.message
    assert len(double.calls) - calls_before <= 2  # one attempt (hidden + reference)
    failed = rig.service.get_case(S, case.id).observations[-1]
    assert failed.status == "failed" and "retry" not in failed.summary
    double.unavailable = False
    double.fail_next = 4  # retryable failure on both attempts
    with pytest.raises(ApiException) as retried:
        await rig.service.run_experiment(
            S, case.id, req("source_current_sweep", "noretry-02"), requested_by="user"
        )
    assert "even after one automatic retry" in retried.value.error.message
    assert "even after one automatic retry" in rig.service.get_case(S, case.id).observations[
        -1
    ].summary
