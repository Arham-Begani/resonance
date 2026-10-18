"""Independent verification, lens B: case lifecycle, budget/concurrency, and agent honesty.

Owner: independent verification engineer (docs/verification/review-B-lifecycle-agent.md).

Everything here drives the REAL FastAPI app (lifespan, routes, error handlers, session cookie,
CaseService, evaluator, AgentService, agent loop, NebiusChatClient) over
``httpx.AsyncClient + ASGITransport`` so that concurrent requests really interleave on one event
loop. Two TEST-ONLY doubles are injected:

- ``VerifSimulator``: the challenges kit's exact analytical RC simulator with gate, delay,
  non-cooperative and failure injection. Fast and deterministic; physics is not under test here.
  One test (``ngspice`` marker) repeats the close-during-agent-tool ordering with real ngspice.
- ``ScriptedProvider`` (tests/agent/agent_testkit.py): scripted OpenAI-compatible responses
  served through ``httpx.MockTransport`` behind the real ``NebiusChatClient``. It is NOT AI.
  NEBIUS_API_KEY is not configured on this host, so no live Nemotron behavior is verified here.

Tests that document a confirmed defect are ``xfail(strict=True)`` with the finding ID.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import sqlite3
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

_TESTS = Path(__file__).resolve().parents[1]
for _sub in ("agent", "challenges"):
    _p = str(_TESTS / _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agent_testkit import Hang, ScriptedProvider, Status, completion  # noqa: E402
from challenges_testkit import AnalyticTestSimulator  # noqa: E402

import resonance.app as app_module  # noqa: E402
from resonance.agent.case_tools import (  # noqa: E402
    CaseRunContext,
    hint_toolset,
    investigate_toolset,
)
from resonance.agent.explore_tools import AppExplorePort  # noqa: E402
from resonance.agent.service import AgentService, limits_for  # noqa: E402
from resonance.config import Settings  # noqa: E402
from resonance.contracts.agent import AgentLimits, AgentRun, AgentRunKind, EditProposal  # noqa: E402
from resonance.contracts.internal import (  # noqa: E402
    CancelToken,
    SimulationCancelled,
    SimulationError,
    SimulationFailed,
    SimulationTimeout,
    SimulatorUnavailable,
)
from resonance.contracts.policy import (  # noqa: E402
    AGENT_MAX_MODEL_CALLS,
    AGENT_MAX_TOOL_CALLS,
    AGENT_MAX_TOTAL_TOKENS,
    CASE_CLOSE_AGENT_WAIT_S,
    CASE_EXPERIMENT_BUDGET,
    CASE_MAX_INFRA_FAILURES,
)

FAKE_KEY = "sk-verif-SECRET-123"
MODEL = "test/nemotron-scripted"
COOKIE = "rs_session"
SWEEPS = ("output_voltage_sweep", "source_current_sweep")


# ======================================================================================
# TEST-ONLY doubles and harness
# ======================================================================================


@dataclass
class VerifSimulator(AnalyticTestSimulator):
    """TEST DOUBLE (not ngspice): exact analytical RC results plus control knobs.

    - ``gate``: when set to an un-set Event, every simulator call blocks (polling the cancel
      token cooperatively) until the event is set.
    - ``ignore_cancel_s``: every call first sleeps this long WITHOUT looking at the token
      (a non-cooperative simulator; asyncio task cancellation still interrupts it).
    - ``fail_all``: every call raises ``fail_kind`` (after the wait).
    """

    gate: asyncio.Event | None = None
    ignore_cancel_s: float = 0.0
    fail_all: bool = False
    fail_kind: type[SimulationError] = SimulationFailed
    active: int = 0
    max_active: int = 0
    cancelled_calls: int = 0

    async def _wait(self, cancel: CancelToken | None) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.ignore_cancel_s > 0:
                await asyncio.sleep(self.ignore_cancel_s)
                return  # never looks at the token
            gate = self.gate
            if gate is not None:
                while not gate.is_set():
                    if cancel is not None and cancel.cancelled:
                        self.cancelled_calls += 1
                        raise SimulationCancelled("The simulation was cancelled.")
                    await asyncio.sleep(0.01)
            await super()._wait(cancel)
        finally:
            self.active -= 1

    def _maybe_fail(self) -> None:
        if self.fail_all:
            raise self.fail_kind("The simulator could not complete this run.")
        super()._maybe_fail()


class RecordingProvider(ScriptedProvider):
    """TEST-ONLY scripted provider that also records the Authorization header it received."""

    def __init__(self, steps: list[Any] | None = None) -> None:
        super().__init__(steps)
        self.auth_headers: list[str] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.auth_headers.append(request.headers.get("authorization", ""))
        return await super().handler(request)


def new_session() -> str:
    return "verifB_" + secrets.token_urlsafe(24)


def new_key() -> str:
    return "vk_" + secrets.token_hex(8)


@dataclass
class Lab:
    """One running app instance (lifespan entered) with injected doubles."""

    app: Any
    sim: VerifSimulator
    settings: Settings
    provider: ScriptedProvider | None
    clients: list[httpx.AsyncClient] = field(default_factory=list)

    def client(self, session: str | None = None) -> httpx.AsyncClient:
        sid = session or new_session()
        c = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://verif.test",
            cookies={COOKIE: sid},
            timeout=60.0,
        )
        c.session_id = sid  # type: ignore[attr-defined]
        self.clients.append(c)
        return c

    @property
    def store(self) -> Any:
        return self.app.state.store

    @property
    def cases(self) -> Any:
        return self.app.state.case_service

    def runs(self, session_id: str) -> list[AgentRun]:
        return list(self.store.list_docs("agent_run", session_id, AgentRun, limit=500))


LabFactory = Callable[..., Awaitable[Lab]]


@pytest.fixture
async def lab_factory(
    settings_factory: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[LabFactory]:
    stack = AsyncExitStack()
    labs: list[Lab] = []

    async def make(
        *,
        provider: ScriptedProvider | None = None,
        key: str | None = FAKE_KEY,
        model: str | None = MODEL,
        limits: dict[str, Any] | None = None,
        sim: VerifSimulator | None = None,
        settings: Settings | None = None,
        **overrides: Any,
    ) -> Lab:
        settings = settings or settings_factory(
            nebius_api_key=key,
            nebius_model=model,
            nebius_base_url="https://provider.test/v1/",
            **overrides,
        )
        simulator = sim or VerifSimulator()
        monkeypatch.setattr(app_module, "create_simulator", lambda _s: simulator)
        app = app_module.create_app(settings)
        await stack.enter_async_context(app.router.lifespan_context(app))
        if provider is not None:
            state = app.state

            def limits_fn(kind: AgentRunKind) -> AgentLimits:
                return limits_for(kind).model_copy(update=limits or {})

            service = AgentService(
                settings=state.settings,
                store=state.store,
                cases=state.case_service,
                explore=AppExplorePort(state),
                client_factory=provider.client_factory(),
                limits=limits_fn,
            )
            state.case_service.register_run_canceller(service.cancel_and_wait)
            state.agent_service = service
        lab = Lab(app, simulator, settings, provider)
        labs.append(lab)
        return lab

    try:
        yield make
    finally:
        for lab in labs:
            for c in lab.clients:
                await c.aclose()
        await stack.aclose()


# --------------------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------------------


async def create_case(c: httpx.AsyncClient) -> dict[str, Any]:
    r = await c.post("/api/cases", json={})
    assert r.status_code == 201, r.text
    case: dict[str, Any] = r.json()
    return case


async def get_case(c: httpx.AsyncClient, cid: str) -> dict[str, Any]:
    r = await c.get(f"/api/cases/{cid}")
    assert r.status_code == 200, r.text
    case: dict[str, Any] = r.json()
    return case


async def set_mode(c: httpx.AsyncClient, cid: str, mode: str) -> httpx.Response:
    return await c.post(f"/api/cases/{cid}/mode", json={"mode": mode})


def exp_body(instrument: str, probe: str | None = None, key: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"instrument": instrument, "client_request_id": key or new_key()}
    if probe is not None:
        body["probe"] = probe
    return body


async def experiment(
    c: httpx.AsyncClient,
    cid: str,
    instrument: str = "output_voltage_sweep",
    probe: str | None = None,
    key: str | None = None,
) -> httpx.Response:
    return await c.post(f"/api/cases/{cid}/experiments", json=exp_body(instrument, probe, key))


def symptom_id(case: dict[str, Any]) -> str:
    sid: str = case["observations"][0]["id"]
    assert case["observations"][0]["kind"] == "symptom"
    return sid


def dx_body(case: dict[str, Any], conclusion: str) -> dict[str, Any]:
    cite = [
        {"observation_id": symptom_id(case), "quantity": "tone_gain_db", "frequency_hz": 4000.0}
    ]
    if conclusion == "fault":
        return {
            "conclusion": "fault",
            "hypothesis_id": "r1_increased",
            "citations": cite,
            "explanation": "The whistle is quieter than expected.",
        }
    if conclusion == "no_fault":
        return {"conclusion": "no_fault", "citations": cite, "explanation": "Matches."}
    return {
        "conclusion": "insufficient_evidence",
        "candidate_hypothesis_ids": ["r1_increased", "c1_increased"],
        "suggested_instrument": "source_current_sweep",
        "explanation": "Voltage alone cannot separate R from C.",
    }


async def diagnose(c: httpx.AsyncClient, case: dict[str, Any], conclusion: str) -> httpx.Response:
    return await c.post(f"/api/cases/{case['id']}/diagnosis", json=dx_body(case, conclusion))


async def wait_run(c: httpx.AsyncClient, run_id: str, limit_s: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        r = await c.get(f"/api/agent-runs/{run_id}")
        assert r.status_code == 200, r.text
        run: dict[str, Any] = r.json()
        if run["status"] != "running":
            return run
        await asyncio.sleep(0.02)
    raise AssertionError(f"run {run_id} did not finish within {limit_s} s")


async def wait_until(cond: Callable[[], bool], limit_s: float = 10.0) -> None:
    deadline = time.monotonic() + limit_s
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


async def events(c: httpx.AsyncClient, run_id: str) -> list[dict[str, Any]]:
    r = await c.get(f"/api/agent-runs/{run_id}/events/log")
    assert r.status_code == 200, r.text
    evs: list[dict[str, Any]] = r.json()["events"]
    return evs


def err(r: httpx.Response) -> tuple[int, str]:
    body = r.json()
    return r.status_code, body["error"]["code"]


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


FINAL = completion("Hint: predict how the whistle level compares with the expected level.")


# ======================================================================================
# 1. Transition table conformance (docs/contracts.md "Transition table (V-11)")
# ======================================================================================

ACTIONS = (
    "mode_guide",
    "mode_agent",
    "experiment",
    "hint",
    "investigate",
    "dx_fault",
    "dx_no_fault",
    "dx_insufficient",
    "propose_repair",
    "verify_repair",
    "close",
    "new_case",
)

# CaseAction (PublicCase.available_actions) -> the actions of this matrix it advertises.
ADVERTISES = {
    "choose_mode": {"mode_guide", "mode_agent"},
    "run_experiment": {"experiment"},
    "start_agent": {"investigate"},
    "request_hint": {"hint"},
    "submit_diagnosis": {"dx_fault", "dx_no_fault", "dx_insufficient"},
    "propose_repair": {"propose_repair"},
    "verify_repair": {"verify_repair"},
    "close": {"close"},
    "cancel_agent": set(),
}


@dataclass(frozen=True)
class Ok:
    http: int
    status: str
    mode: str | None = None


REFUSED = (409, "conflict")

_COMMON_OBSERVING: dict[str, Any] = {
    "mode_guide": Ok(200, "observing", "guide"),
    "mode_agent": Ok(200, "observing", "agent"),
    "experiment": Ok(201, "observing"),
    "dx_fault": Ok(200, "diagnosis_submitted"),
    "dx_no_fault": Ok(200, "diagnosis_submitted"),
    "dx_insufficient": Ok(200, "unresolved"),
    "propose_repair": REFUSED,
    "verify_repair": REFUSED,
    "close": Ok(200, "closed"),
    "new_case": REFUSED,
}
_ONLY_CLOSE: dict[str, Any] = {a: REFUSED for a in ACTIONS} | {"close": Ok(200, "closed")}

EXPECTED: dict[str, dict[str, Any]] = {
    "ready": {
        "mode_guide": Ok(200, "observing", "guide"),
        "mode_agent": Ok(200, "observing", "agent"),
        "experiment": REFUSED,
        # Starting a run from `ready` implicitly chooses the matching mode (documented in
        # docs/ai-integration.md and agent/service._ready_case).
        "hint": Ok(201, "observing", "guide"),
        "investigate": Ok(201, "observing", "agent"),
        "dx_fault": Ok(200, "diagnosis_submitted"),
        "dx_no_fault": Ok(200, "diagnosis_submitted"),
        "dx_insufficient": Ok(200, "unresolved"),
        "propose_repair": REFUSED,
        "verify_repair": REFUSED,
        "close": Ok(200, "closed"),
        "new_case": REFUSED,
    },
    "observing_guide": _COMMON_OBSERVING | {"hint": Ok(201, "observing"), "investigate": REFUSED},
    "observing_agent": _COMMON_OBSERVING | {"hint": REFUSED, "investigate": Ok(201, "observing")},
    "diagnosis_submitted": _ONLY_CLOSE | {"propose_repair": Ok(200, "diagnosis_submitted")},
    "diagnosis_with_proposal": _ONLY_CLOSE
    | {
        "propose_repair": Ok(200, "diagnosis_submitted"),
        "verify_repair": Ok(200, "repair_verification"),
    },
    "unresolved": _ONLY_CLOSE | {"propose_repair": Ok(200, "unresolved")},
    "unresolved_with_proposal": _ONLY_CLOSE
    | {
        "propose_repair": Ok(200, "unresolved"),
        "verify_repair": Ok(200, "repair_verification"),
    },
    "repair_verification": _ONLY_CLOSE,
    "failed": _ONLY_CLOSE,
    "closed": {a: REFUSED for a in ACTIONS}
    | {"close": Ok(200, "closed"), "new_case": Ok(201, "ready")},
}


async def build_state(lab: Lab, state: str) -> tuple[httpx.AsyncClient, dict[str, Any]]:
    """A fresh session whose only case is in ``state`` (built through the public API)."""
    c = lab.client()
    case = await create_case(c)
    cid = case["id"]
    if state == "ready":
        return c, await get_case(c, cid)
    if state == "observing_agent":
        assert (await set_mode(c, cid, "agent")).status_code == 200
        return c, await get_case(c, cid)
    if state == "closed":
        assert (await c.post(f"/api/cases/{cid}/close")).status_code == 200
        return c, await get_case(c, cid)
    assert (await set_mode(c, cid, "guide")).status_code == 200
    if state == "observing_guide":
        return c, await get_case(c, cid)
    if state == "failed":
        lab.sim.fail_all = True
        try:
            for _ in range(CASE_MAX_INFRA_FAILURES):
                r = await experiment(c, cid, "transient_capture", "output_voltage")
                assert r.status_code == 502, r.text
        finally:
            lab.sim.fail_all = False
        case = await get_case(c, cid)
        assert case["status"] == "failed"
        return c, case
    conclusion = "insufficient" if state.startswith("unresolved") else "fault"
    assert (await diagnose(c, case, conclusion)).status_code == 200
    if state in ("diagnosis_submitted", "unresolved"):
        return c, await get_case(c, cid)
    r = await c.post(f"/api/cases/{cid}/repair-proposal", json={"component_id": "R1"})
    assert r.status_code == 200, r.text
    if state in ("diagnosis_with_proposal", "unresolved_with_proposal"):
        return c, await get_case(c, cid)
    assert state == "repair_verification"
    r = await c.post(f"/api/cases/{cid}/repair")
    assert r.status_code == 200, r.text
    return c, await get_case(c, cid)


async def perform(c: httpx.AsyncClient, case: dict[str, Any], action: str) -> httpx.Response:
    cid = case["id"]
    if action.startswith("mode_"):
        return await set_mode(c, cid, action.removeprefix("mode_"))
    if action == "experiment":
        return await experiment(c, cid, "output_voltage_sweep")
    if action in ("hint", "investigate"):
        return await c.post(f"/api/cases/{cid}/agent-runs", json={"kind": action})
    if action.startswith("dx_"):
        return await diagnose(c, case, action.removeprefix("dx_"))
    if action == "propose_repair":
        return await c.post(f"/api/cases/{cid}/repair-proposal", json={"component_id": "C1"})
    if action == "verify_repair":
        return await c.post(f"/api/cases/{cid}/repair")
    if action == "close":
        return await c.post(f"/api/cases/{cid}/close")
    assert action == "new_case"
    return await c.post("/api/cases", json={})


def snapshot(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": case["status"],
        "mode": case["mode"],
        "budget": case["budget"],
        "hints_used": case["hints_used"],
        "observations": [(o["id"], o["status"], o["counted"]) for o in case["observations"]],
        "diagnosis": case["diagnosis"]["id"] if case["diagnosis"] else None,
        "repair_proposal": case["repair_proposal"]["id"] if case["repair_proposal"] else None,
        "repair": case["repair"]["id"] if case["repair"] else None,
        "reveal": case["reveal"]["revealed_at"] if case["reveal"] else None,
        "active_run_id": case["active_run_id"],
        "available_actions": case["available_actions"],
    }


@pytest.mark.parametrize("state", list(EXPECTED))
async def test_transition_table_every_state_and_action(lab_factory: LabFactory, state: str) -> None:
    """Every action in every state gives exactly the documented next state or error code.
    Refused actions leave the case unchanged (and start no agent run)."""
    lab = await lab_factory(provider=ScriptedProvider([FINAL] * 10))
    table = EXPECTED[state]

    # Refused actions, all on one case: none may change anything.
    c, case = await build_state(lab, state)
    before = snapshot(case)
    allowed = {a for a, e in table.items() if isinstance(e, Ok)}
    for action in ACTIONS:
        if action in allowed:
            continue
        r = await perform(c, case, action)
        assert err(r) == table[action], (state, action, r.text)
        assert snapshot(await get_case(c, case["id"])) == before, (state, action)
    assert lab.runs(c.session_id) == [], "a refused agent start created a run record"

    # available_actions advertises only allowed actions (and every allowed action, except the
    # documented implicit mode choice when a run is started from `ready`).
    advertised: set[str] = set().union(*(ADVERTISES[a] for a in case["available_actions"]))
    assert advertised <= allowed, (state, case["available_actions"])
    implicit = {"hint", "investigate"} if state == "ready" else set()
    if state == "closed":
        implicit = {"close"}  # idempotent no-op on a closed case; correctly not advertised
    missing = allowed - advertised - implicit - {"new_case"}
    assert not missing, (state, missing)

    # Allowed actions, each on a fresh case in this state.
    for action in sorted(allowed):
        c, case = await build_state(lab, state)
        r = await perform(c, case, action)
        expected: Ok = table[action]
        assert r.status_code == expected.http, (state, action, r.text)
        if action == "new_case":
            assert r.json()["status"] == expected.status
            continue
        if action in ("hint", "investigate"):
            run = await wait_run(c, r.json()["id"])
            assert run["status"] == "completed", run
            assert run["source"] == "nebius" and run["kind"] == action
        after = await get_case(c, case["id"])
        assert after["status"] == expected.status, (state, action)
        if expected.mode is not None:
            assert after["mode"] == expected.mode, (state, action)
        assert after["active_run_id"] is None
        if action == "hint":
            assert after["hints_used"] == case["hints_used"] + 1
        if action == "experiment":
            assert after["budget"]["used"] == case["budget"]["used"] + 1
        if action.startswith("dx_"):
            sent = dx_body(case, action.removeprefix("dx_"))["conclusion"]
            assert after["diagnosis"]["submission"]["conclusion"] == sent
            assert after["diagnosis"]["submitted_by"] == "user"
        if action == "propose_repair":
            assert after["repair_proposal"]["component_id"] == "C1"
        if action == "verify_repair":
            assert after["repair"] is not None
            assert after["repair"]["proposal"]["id"] == case["repair_proposal"]["id"]
        if action == "close":
            assert after["reveal"] is not None and after["available_actions"] == []
            terminal = "failed" if state == "failed" else "closed"
            assert after["reveal"]["outcome"]["terminal_status"] == terminal
            if state == "closed":  # idempotent: the same reveal, not a second one
                assert after["reveal"] == case["reveal"]
        else:
            assert after["reveal"] is None


async def start_hanging_run(
    lab: Lab, kind: str = "investigate"
) -> tuple[httpx.AsyncClient, dict[str, Any], str]:
    """A case owned by an agent run that is blocked in inference (scripted Hang)."""
    assert lab.provider is not None
    lab.provider.steps[:] = [Hang(60)]
    lab.provider.started.clear()
    c = lab.client()
    case = await create_case(c)
    mode = "agent" if kind == "investigate" else "guide"
    assert (await set_mode(c, case["id"], mode)).status_code == 200
    r = await c.post(f"/api/cases/{case['id']}/agent-runs", json={"kind": kind})
    assert r.status_code == 201, r.text
    await asyncio.wait_for(lab.provider.started.wait(), 5)
    return c, await get_case(c, case["id"]), r.json()["id"]


async def test_active_run_blocks_every_user_mutation_except_close(lab_factory: LabFactory) -> None:
    """Contract: while an agent run is active, mode/experiment/diagnosis/agent start/hint return
    409 run_in_progress and change nothing; close still works and cancels the run."""
    lab = await lab_factory(provider=ScriptedProvider())
    c, case, run_id = await start_hanging_run(lab)
    before = snapshot(case)
    assert case["active_run_id"] == run_id
    assert case["available_actions"] == ["cancel_agent", "close"]
    for action in (
        "mode_guide",
        "mode_agent",
        "experiment",
        "dx_fault",
        "dx_insufficient",
        "investigate",
        "hint",
    ):
        r = await perform(c, case, action)
        assert err(r) == (409, "run_in_progress"), (action, r.text)
        assert snapshot(await get_case(c, case["id"])) == before, action
    assert err(await perform(c, case, "new_case")) == (409, "conflict")
    assert len(lab.runs(c.session_id)) == 1, "a refused start created another run"
    closed = await c.post(f"/api/cases/{case['id']}/close")
    assert closed.status_code == 200 and closed.json()["reveal"] is not None
    assert (await c.get(f"/api/agent-runs/{run_id}")).json()["status"] == "cancelled"


async def test_active_run_repair_endpoints_also_report_run_in_progress(
    lab_factory: LabFactory,
) -> None:
    """docs/contracts.md: while an agent run is active, every mutating case endpoint except
    close and agent cancel returns 409 run_in_progress."""
    lab = await lab_factory(provider=ScriptedProvider())
    c, case, _ = await start_hanging_run(lab)
    for action in ("propose_repair", "verify_repair"):
        r = await perform(c, case, action)
        assert err(r) == (409, "run_in_progress"), (action, r.text)


# ======================================================================================
# 2. Budget, infrastructure failures, idempotency, concurrency
# ======================================================================================


async def observing_case(lab: Lab, mode: str = "guide") -> tuple[httpx.AsyncClient, dict[str, Any]]:
    c = lab.client()
    case = await create_case(c)
    assert (await set_mode(c, case["id"], mode)).status_code == 200
    return c, await get_case(c, case["id"])


async def test_budget_exactly_three_and_validation_errors_are_free(lab_factory: LabFactory) -> None:
    lab = await lab_factory()
    c, case = await observing_case(lab)
    cid = case["id"]
    invalid_bodies: list[dict[str, Any]] = [
        {"instrument": "transient_capture", "client_request_id": new_key()},  # probe missing
        {
            "instrument": "output_voltage_sweep",
            "probe": "output_voltage",
            "client_request_id": new_key(),
        },  # probe on a sweep
        {
            "instrument": "transient_capture",
            "probe": "input_voltage",
            "client_request_id": new_key(),
        },  # undeclared probe
        {"instrument": "impedance_bridge", "client_request_id": new_key()},
        {"instrument": "output_voltage_sweep"},  # the idempotency key is required
        {"instrument": "output_voltage_sweep", "client_request_id": "short"},
        {"instrument": "output_voltage_sweep", "client_request_id": new_key(), "seed": "x"},
        {
            "instrument": "output_voltage_sweep",
            "client_request_id": new_key(),
            "frequency_hz": [1.0, 1e6],
        },  # no free sweep ranges
    ]
    calls = len(lab.sim.calls)
    for body in invalid_bodies:
        r = await c.post(f"/api/cases/{cid}/experiments", json=body)
        assert err(r) == (422, "validation_error"), (body, r.text)
    assert len(lab.sim.calls) == calls, "an invalid request reached the simulator"
    used = []
    for instrument, probe in (
        ("output_voltage_sweep", None),
        ("source_current_sweep", None),
        ("transient_capture", "source_current"),
    ):
        r = await experiment(c, cid, instrument, probe)
        assert r.status_code == 201, r.text
        used.append(r.json()["case"]["budget"]["used"])
        assert r.json()["observation"]["counted"] is True
    assert used == [1, 2, 3]
    r = await experiment(c, cid, "transient_capture", "output_voltage")
    assert err(r) == (409, "budget_exhausted")
    final = await get_case(c, cid)
    assert (final["budget"]["total"], final["budget"]["used"], final["budget"]["remaining"]) == (
        3,
        3,
        0,
    )
    assert sum(o["counted"] for o in final["observations"]) == CASE_EXPERIMENT_BUDGET
    assert "run_experiment" not in final["available_actions"]
    # Once the budget is spent, distinguishing_instruments is published (declared rule).
    assert final["evidence"]["distinguishing_instruments"] is not None


async def test_infrastructure_failures_retry_once_never_consume_budget_then_fail_case(
    lab_factory: LabFactory,
) -> None:
    lab = await lab_factory()
    c, case = await observing_case(lab)
    cid = case["id"]
    # The symptom cached this capture's intended-design reference, so each attempt of
    # transient_capture/output_voltage is exactly one simulator call.
    probe = ("transient_capture", "output_voltage")

    lab.sim.fail_next = 1  # one transient failure -> automatic retry -> counted success
    calls = len(lab.sim.calls)
    r = await experiment(c, cid, *probe)
    assert r.status_code == 201, r.text
    assert len(lab.sim.calls) - calls == 2, "exactly one automatic retry"
    body = r.json()
    assert body["case"]["budget"]["used"] == 1
    assert body["case"]["budget"]["infrastructure_failures"] == 0
    assert [o["status"] for o in body["case"]["observations"]] == ["succeeded", "succeeded"]

    expected_codes = {
        SimulationFailed: (502, "simulation_failed"),
        SimulationTimeout: (504, "simulation_timeout"),
    }
    kinds = (SimulationFailed, SimulationTimeout, SimulationFailed)
    for i, kind in enumerate(kinds, start=1):
        lab.sim.fail_all, lab.sim.fail_kind = True, kind
        calls = len(lab.sim.calls)
        r = await experiment(c, cid, *probe)
        lab.sim.fail_all = False
        assert err(r) == expected_codes[kind], r.text
        assert len(lab.sim.calls) - calls == 2, "one try plus exactly one automatic retry"
        now = await get_case(c, cid)
        last = now["observations"][-1]
        assert last["status"] == "failed" and last["counted"] is False
        assert last["sweep"] is None and last["transient"] is None, "no partial data"
        assert now["budget"]["used"] == 1, "a failure consumed budget"
        assert now["budget"]["infrastructure_failures"] == i
        if i < CASE_MAX_INFRA_FAILURES:
            assert r.json()["error"]["retryable"] is True
            assert now["status"] == "observing"
        else:
            assert r.json()["error"]["retryable"] is False
            assert now["status"] == "failed"
            assert now["available_actions"] == ["close"]
    closed = (await c.post(f"/api/cases/{cid}/close")).json()
    assert closed["reveal"]["outcome"]["terminal_status"] == "failed"
    assert closed["reveal"]["outcome"]["experiments_used"] == 1


async def test_simulator_unavailable_is_not_retried_and_not_counted(
    lab_factory: LabFactory,
) -> None:
    lab = await lab_factory()
    c, case = await observing_case(lab)
    lab.sim.fail_all, lab.sim.fail_kind = True, SimulatorUnavailable
    calls = len(lab.sim.calls)
    r = await experiment(c, case["id"], "transient_capture", "output_voltage")
    lab.sim.fail_all = False
    assert err(r) == (503, "simulator_unavailable")
    assert len(lab.sim.calls) - calls == 1, "a non-retryable error must not be retried"
    now = await get_case(c, case["id"])
    assert now["budget"]["used"] == 0 and now["observations"][-1]["counted"] is False


async def test_idempotency_key_rules(lab_factory: LabFactory) -> None:
    lab = await lab_factory()
    c, case = await observing_case(lab)
    cid = case["id"]
    key = new_key()
    first = await experiment(c, cid, "source_current_sweep", key=key)
    assert first.status_code == 201
    calls = len(lab.sim.calls)
    again = await experiment(c, cid, "source_current_sweep", key=key)
    assert again.status_code == 201
    assert again.json()["observation"] == first.json()["observation"]
    assert again.json()["case"]["budget"]["used"] == 1
    assert len(lab.sim.calls) == calls, "a replay must not simulate again"
    other = await experiment(c, cid, "output_voltage_sweep", key=key)
    assert err(other) == (409, "conflict")
    assert other.json()["error"]["field"] == "client_request_id"
    assert other.json()["error"]["retryable"] is False
    probe_changed = await experiment(c, cid, "transient_capture", "output_voltage", key=key)
    assert err(probe_changed) == (409, "conflict")
    assert (await get_case(c, cid))["budget"]["used"] == 1

    # A key still in flight -> 409 retryable; afterwards the same key replays the stored result.
    lab.sim.gate = asyncio.Event()
    key2 = new_key()
    task = asyncio.create_task(experiment(c, cid, "output_voltage_sweep", key=key2))
    await wait_until(lambda: lab.sim.active > 0)
    in_flight = await experiment(c, cid, "output_voltage_sweep", key=key2)
    assert err(in_flight) == (409, "conflict") and in_flight.json()["error"]["retryable"] is True
    lab.sim.gate.set()
    done = await task
    assert done.status_code == 201
    replay = await experiment(c, cid, "output_voltage_sweep", key=key2)
    assert replay.json()["observation"]["id"] == done.json()["observation"]["id"]
    assert (await get_case(c, cid))["budget"]["used"] == 2


async def test_concurrent_experiments_never_exceed_budget(lab_factory: LabFactory) -> None:
    lab = await lab_factory()
    lab.sim.delay_s = 0.3
    c, case = await observing_case(lab)
    cid = case["id"]
    successes = 0
    seen_used: list[int] = []
    for _wave in range(6):
        responses = await asyncio.gather(*(experiment(c, cid, SWEEPS[i % 2]) for i in range(8)))
        for r in responses:
            if r.status_code == 201:
                successes += 1
                seen_used.append(r.json()["case"]["budget"]["used"])
            else:
                code = err(r)
                assert code in ((409, "conflict"), (409, "budget_exhausted")), r.text
                if code == (409, "conflict"):
                    assert r.json()["error"]["retryable"] is True
        if successes >= CASE_EXPERIMENT_BUDGET:
            break
    assert successes == CASE_EXPERIMENT_BUDGET
    assert sorted(seen_used) == [1, 2, 3]
    assert lab.sim.max_active <= 2, "only one experiment (hidden + reference) ran at a time"
    burst = await asyncio.gather(*(experiment(c, cid) for _ in range(8)))
    assert all(err(r) == (409, "budget_exhausted") for r in burst)
    final = await get_case(c, cid)
    assert final["budget"]["used"] == CASE_EXPERIMENT_BUDGET
    assert sum(o["counted"] for o in final["observations"]) == CASE_EXPERIMENT_BUDGET


async def test_simultaneous_diagnoses_record_exactly_one(lab_factory: LabFactory) -> None:
    lab = await lab_factory()
    c, case = await observing_case(lab)
    conclusions = ["fault", "no_fault", "insufficient"] * 4
    responses = await asyncio.gather(*(diagnose(c, case, k) for k in conclusions))
    winners = [r for r in responses if r.status_code == 200]
    assert len(winners) == 1
    assert all(err(r) == (409, "conflict") for r in responses if r.status_code != 200)
    final = await get_case(c, case["id"])
    assert final["diagnosis"]["id"] == winners[0].json()["diagnosis"]["id"]
    conclusion = final["diagnosis"]["submission"]["conclusion"]
    expected = "unresolved" if conclusion == "insufficient_evidence" else "diagnosis_submitted"
    assert final["status"] == expected


async def test_diagnosis_during_in_flight_experiment_is_refused(lab_factory: LabFactory) -> None:
    """Evidence cannot land after a diagnosis was evaluated (V-02 d)."""
    lab = await lab_factory(provider=ScriptedProvider([FINAL] * 3))
    c, case = await observing_case(lab)
    lab.sim.gate = asyncio.Event()
    task = asyncio.create_task(experiment(c, case["id"], "source_current_sweep"))
    await wait_until(lambda: lab.sim.active > 0)
    r = await diagnose(c, case, "fault")
    assert err(r) == (409, "conflict") and r.json()["error"]["retryable"] is True
    for action in ("mode_agent", "hint"):
        r = await perform(c, case, action)
        assert r.status_code == 409, (action, r.text)
    lab.sim.gate.set()
    assert (await task).status_code == 201
    assert (await diagnose(c, case, "fault")).status_code == 200
    final = await get_case(c, case["id"])
    assert final["diagnosis"]["created_at"] >= final["observations"][-1]["created_at"]
    assert lab.runs(c.session_id) == []


async def test_experiment_racing_agent_start(lab_factory: LabFactory) -> None:
    """A user experiment in flight blocks an agent start (no run is created); an active run
    blocks a user experiment."""
    lab = await lab_factory(provider=ScriptedProvider([FINAL] * 3))
    c, case = await observing_case(lab, "agent")
    lab.sim.gate = asyncio.Event()
    task = asyncio.create_task(experiment(c, case["id"], "source_current_sweep"))
    await wait_until(lambda: lab.sim.active > 0)
    starts = await asyncio.gather(
        *(
            c.post(f"/api/cases/{case['id']}/agent-runs", json={"kind": "investigate"})
            for _ in range(3)
        )
    )
    assert all(err(r) == (409, "conflict") for r in starts), [r.text for r in starts]
    assert lab.runs(c.session_id) == []
    lab.sim.gate.set()
    assert (await task).status_code == 201
    # Several simultaneous starts: exactly one run is created.
    lab.provider.steps[:] = [Hang(60)] * 3  # type: ignore[union-attr]
    starts = await asyncio.gather(
        *(
            c.post(f"/api/cases/{case['id']}/agent-runs", json={"kind": "investigate"})
            for _ in range(4)
        )
    )
    assert sorted(r.status_code for r in starts) == [201, 409, 409, 409]
    assert len(lab.runs(c.session_id)) == 1
    r = await experiment(c, case["id"], "output_voltage_sweep")
    assert err(r) == (409, "run_in_progress")
    assert (await c.post(f"/api/cases/{case['id']}/close")).status_code == 200


async def test_validation_errors_never_consume_diagnosis_or_repair(lab_factory: LabFactory) -> None:
    lab = await lab_factory()
    c, other_case = await observing_case(lab)
    foreign_obs = symptom_id(other_case)
    assert (await c.post(f"/api/cases/{other_case['id']}/close")).status_code == 200
    case = await create_case(c)
    cid = case["id"]
    assert (await set_mode(c, cid, "guide")).status_code == 200
    lab.sim.fail_all = True
    assert (await experiment(c, cid, "transient_capture", "output_voltage")).status_code == 502
    lab.sim.fail_all = False
    case = await get_case(c, cid)
    failed_obs = case["observations"][-1]["id"]
    sym = symptom_id(case)

    def cite(obs: str, quantity: str = "tone_gain_db", f: float | None = None) -> dict[str, Any]:
        d: dict[str, Any] = {"observation_id": obs, "quantity": quantity}
        if f is not None:
            d["frequency_hz"] = f
        return d

    fault = {"conclusion": "fault", "hypothesis_id": "r1_increased"}
    invalid: list[dict[str, Any]] = [
        {"conclusion": "fault", "citations": [cite(sym)], "explanation": "x"},
        {**fault, "explanation": "no citation"},
        {"conclusion": "no_fault", "explanation": "no citation"},
        {
            "conclusion": "no_fault",
            "hypothesis_id": "r1_increased",
            "citations": [cite(sym)],
            "explanation": "x",
        },
        {
            "conclusion": "insufficient_evidence",
            "hypothesis_id": "c1_increased",
            "explanation": "x",
        },
        {
            "conclusion": "fault",
            "hypothesis_id": "no_fault",
            "citations": [cite(sym)],
            "explanation": "x",
        },
        {**fault, "citations": [cite("obs_doesnotexist0000")], "explanation": "unknown"},
        {**fault, "citations": [cite(foreign_obs)], "explanation": "cross-case citation"},
        {**fault, "citations": [cite(failed_obs)], "explanation": "failed observation"},
        {**fault, "citations": [cite(sym, "source_current_ma")], "explanation": "wrong quantity"},
        {**fault, "citations": [cite(sym, "tone_gain_db", 1000.0)], "explanation": "unmeasured"},
        {**fault, "citations": [cite(sym, "made_up_quantity")], "explanation": "x"},
        {**fault, "citations": [cite(sym)], "explanation": "x", "actual_value_si": 2000.0},
    ]
    for body in invalid:
        r = await c.post(f"/api/cases/{cid}/diagnosis", json=body)
        assert err(r) == (422, "validation_error"), (body, r.text)
    assert (await get_case(c, cid))["diagnosis"] is None
    assert (await diagnose(c, case, "fault")).status_code == 200

    for body in ({"component_id": "V1"}, {"component_id": "R1", "value": "2k"}, {}):
        r = await c.post(f"/api/cases/{cid}/repair-proposal", json=body)
        assert err(r) == (422, "validation_error"), (body, r.text)
    assert (await get_case(c, cid))["repair_proposal"] is None
    r = await c.post(f"/api/cases/{cid}/repair-proposal", json={"component_id": "R1"})
    assert r.status_code == 200
    assert (await c.post(f"/api/cases/{cid}/repair")).status_code == 200
    assert err(await c.post(f"/api/cases/{cid}/repair")) == (409, "conflict")


async def raw_post_with_disconnect(
    app: Any, path: str, body: dict[str, Any], session: str, disconnect: asyncio.Event
) -> list[dict[str, Any]]:
    """Call the ASGI app directly so the client can 'disconnect' mid-request (httpx's
    ASGITransport only reports a disconnect after the response is complete)."""
    payload = json.dumps(body).encode()
    sent: list[dict[str, Any]] = []
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"verif.test"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
            (b"cookie", f"{COOKIE}={session}".encode()),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("verif.test", 80),
    }
    await app(scope, receive, send)
    return sent


async def test_client_disconnect_cancels_experiment_without_using_budget(
    lab_factory: LabFactory,
) -> None:
    """Contract: 'A client disconnect cancels the experiment (not counted).' The harness's
    disconnect is detected by Request.is_disconnected() when the app has no BaseHTTPMiddleware
    (checked separately); the same failure reproduces on a real uvicorn server (see report)."""
    lab = await lab_factory()
    c, case = await observing_case(lab)
    lab.sim.gate = asyncio.Event()  # the simulation would run until cancelled
    disconnect = asyncio.Event()
    task = asyncio.create_task(
        raw_post_with_disconnect(
            lab.app,
            f"/api/cases/{case['id']}/experiments",
            exp_body("source_current_sweep"),
            c.session_id,  # type: ignore[attr-defined]
            disconnect,
        )
    )
    await wait_until(lambda: lab.sim.active > 0)
    disconnect.set()
    try:
        # The route's watcher polls every 0.25 s; give it ample time.
        await wait_until(lambda: lab.sim.cancelled_calls >= 1, limit_s=2.0)
    finally:
        lab.sim.gate.set()  # never leave the request hanging
    sent = await asyncio.wait_for(task, 5)
    assert sent[0]["status"] == 409
    assert json.loads(sent[-1]["body"])["error"]["code"] == "cancelled"
    assert lab.sim.cancelled_calls >= 1, "the simulation itself was stopped"
    now = await get_case(c, case["id"])
    assert now["budget"]["used"] == 0 and now["budget"]["remaining"] == 3
    last = now["observations"][-1]
    assert last["status"] == "cancelled" and last["counted"] is False
    assert now["budget"]["infrastructure_failures"] == 0, "a cancel is not an infra failure"


async def test_close_racing_in_flight_experiment(lab_factory: LabFactory) -> None:
    """Close cancels and awaits the in-flight experiment; nothing is committed after reveal."""
    lab = await lab_factory()
    c, case = await observing_case(lab)
    lab.sim.gate = asyncio.Event()
    task = asyncio.create_task(experiment(c, case["id"], "transient_capture", "source_current"))
    await wait_until(lambda: lab.sim.active > 0)
    closed = await c.post(f"/api/cases/{case['id']}/close")
    r = await task
    assert closed.status_code == 200
    assert err(r) == (409, "cancelled")
    final = await get_case(c, case["id"])
    revealed_at = ts(final["reveal"]["revealed_at"])
    assert final["budget"]["used"] == 0
    assert all(ts(o["created_at"]) <= revealed_at for o in final["observations"])
    assert [o["status"] for o in final["observations"]] == ["succeeded", "cancelled"]
    assert final["reveal"]["outcome"]["experiments_used"] == 0
    assert final["updated_at"] == closed.json()["updated_at"], "the record changed after close"


async def test_close_racing_non_cooperative_experiment_commits_before_reveal(
    lab_factory: LabFactory,
) -> None:
    """A simulator that ignores the cancel token: close waits for it, so the (counted)
    observation lands BEFORE the reveal and the outcome includes it."""
    lab = await lab_factory()
    c, case = await observing_case(lab)
    await experiment(c, case["id"], "output_voltage_sweep")  # cache the reference: 1 call
    lab.sim.ignore_cancel_s = 0.8
    task = asyncio.create_task(experiment(c, case["id"], "output_voltage_sweep"))
    await wait_until(lambda: lab.sim.active > 0)
    closed = (await c.post(f"/api/cases/{case['id']}/close")).json()
    r = await task
    assert r.status_code == 201
    revealed_at = ts(closed["reveal"]["revealed_at"])
    assert ts(r.json()["observation"]["created_at"]) <= revealed_at
    assert closed["reveal"]["outcome"]["experiments_used"] == 2
    final = await get_case(c, case["id"])
    assert len(final["observations"]) == 3 and final["status"] == "closed"


async def test_close_racing_repair_verification(lab_factory: LabFactory) -> None:
    lab = await lab_factory()
    c, case = await build_state(lab, "diagnosis_with_proposal")
    lab.sim.gate = asyncio.Event()
    task = asyncio.create_task(c.post(f"/api/cases/{case['id']}/repair"))
    await wait_until(lambda: lab.sim.active > 0)
    closed = await c.post(f"/api/cases/{case['id']}/close")
    r = await task
    assert closed.status_code == 200
    assert err(r) == (409, "cancelled")
    final = await get_case(c, case["id"])
    assert final["repair"] is None
    assert final["reveal"]["outcome"]["functional_recovery"] in ("not_attempted", "not_needed")


async def test_close_twice_concurrently(lab_factory: LabFactory) -> None:
    lab = await lab_factory()
    c, case = await observing_case(lab)
    lab.sim.gate = asyncio.Event()
    exp_task = asyncio.create_task(experiment(c, case["id"]))
    await wait_until(lambda: lab.sim.active > 0)
    responses = await asyncio.gather(*(c.post(f"/api/cases/{case['id']}/close") for _ in range(3)))
    await exp_task
    codes = sorted(r.status_code for r in responses)
    assert codes[0] == 200
    for r in responses:
        if r.status_code == 200:
            assert r.json()["reveal"] is not None
        else:
            assert err(r) == (409, "conflict") and r.json()["error"]["retryable"] is True
    final = await get_case(c, case["id"])
    assert final["status"] == "closed"
    reveals = {r.json()["reveal"]["revealed_at"] for r in responses if r.status_code == 200}
    assert reveals == {final["reveal"]["revealed_at"]}, "the reveal was written more than once"


# --------------------------------------------------------------------------------------
# Close while an agent run is active (V-03)
# --------------------------------------------------------------------------------------


def assert_sealed_before_reveal(evs: list[dict[str, Any]], revealed_at: datetime) -> None:
    assert evs[-1]["type"] == "run_finished"
    assert evs[-1]["run"]["status"] == "cancelled"
    assert [e["seq"] for e in evs] == list(range(1, len(evs) + 1))
    late = [e for e in evs if ts(e["at"]) > revealed_at]
    assert not late, f"events after the reveal: {late}"


async def test_close_during_inference_cancels_run_before_reveal(lab_factory: LabFactory) -> None:
    lab = await lab_factory(provider=ScriptedProvider())
    c, case, run_id = await start_hanging_run(lab)
    t0 = time.monotonic()
    closed = await c.post(f"/api/cases/{case['id']}/close")
    elapsed = time.monotonic() - t0
    assert closed.status_code == 200
    assert elapsed < CASE_CLOSE_AGENT_WAIT_S, elapsed
    assert lab.provider is not None and lab.provider.cancelled == 1, "inference was aborted"
    revealed_at = ts(closed.json()["reveal"]["revealed_at"])
    evs = await events(c, run_id)
    assert_sealed_before_reveal(evs, revealed_at)
    await asyncio.sleep(0.3)
    assert await events(c, run_id) == evs, "the event log grew after close"
    run = (await c.get(f"/api/agent-runs/{run_id}")).json()
    assert run["status"] == "cancelled" and ts(run["finished_at"]) <= revealed_at
    again = await c.post(f"/api/cases/{case['id']}/agent-runs", json={"kind": "investigate"})
    assert err(again) == (409, "conflict")
    assert len(lab.runs(c.session_id)) == 1


@pytest.mark.parametrize("cooperative", [True, False])
async def test_close_during_agent_experiment(lab_factory: LabFactory, cooperative: bool) -> None:
    """Close while the agent's tool is simulating. Cooperative simulator: the token stops it.
    Non-cooperative simulator (ignores the token for 30 s): the loop's grace period expires
    and the task is cancelled. Either way close stays within CASE_CLOSE_AGENT_WAIT_S, the run
    ends cancelled before the reveal, and no counted observation appears."""
    provider = ScriptedProvider(
        [completion(None, [("run_experiment", {"instrument": "source_current_sweep"})]), Hang(60)]
    )
    lab = await lab_factory(provider=provider)
    c, case = await observing_case(lab, "agent")
    if cooperative:
        lab.sim.gate = asyncio.Event()
    else:
        lab.sim.ignore_cancel_s = 30.0
    r = await c.post(f"/api/cases/{case['id']}/agent-runs", json={"kind": "investigate"})
    run_id = r.json()["id"]
    await wait_until(lambda: lab.sim.active > 0)
    t0 = time.monotonic()
    closed = await c.post(f"/api/cases/{case['id']}/close")
    elapsed = time.monotonic() - t0
    assert closed.status_code == 200, closed.text
    assert elapsed < CASE_CLOSE_AGENT_WAIT_S + 0.5, elapsed
    revealed_at = ts(closed.json()["reveal"]["revealed_at"])
    evs = await events(c, run_id)
    assert_sealed_before_reveal(evs, revealed_at)
    final = await get_case(c, case["id"])
    assert final["budget"]["used"] == 0
    assert not any(o["counted"] for o in final["observations"])
    assert all(ts(o["created_at"]) <= revealed_at for o in final["observations"])
    assert final["reveal"]["outcome"]["experiments_by_agent"] == 0
    results = [e for e in evs if e["type"] == "tool_result"]
    assert len(results) == 1 and results[0]["ok"] is False


async def test_agent_tools_refuse_once_case_is_closed(lab_factory: LabFactory) -> None:
    """Every INVESTIGATE/HINT tool refuses (fatally) on a closed case and changes nothing;
    agent views never serialize a closed case (its reveal)."""
    lab = await lab_factory()
    c, case = await observing_case(lab, "agent")
    closed = (await c.post(f"/api/cases/{case['id']}/close")).json()
    ctx = CaseRunContext(
        session_id=c.session_id,  # type: ignore[attr-defined]
        case_id=case["id"],
        run_id="run_verifBverifB01",
        cases=lab.cases,
        cancel=CancelToken(),
    )
    sym = symptom_id(case)
    args = {
        "get_case": {},
        "assess_evidence": {},
        "run_experiment": {"instrument": "source_current_sweep"},
        "read_observation": {"observation_id": sym},
        "submit_diagnosis": dx_body(case, "fault"),
        "propose_repair": {"component_id": "R1"},
    }
    for toolset in (investigate_toolset(ctx), hint_toolset(ctx)):
        for name in toolset.names:
            tool = toolset.get(name)
            assert tool is not None
            outcome = await tool.run(tool.args_model.model_validate(args[name]))
            assert outcome.ok is False and outcome.fatal is not None, name
            blob = json.dumps(outcome.content)
            assert closed["reveal"]["seed"] not in blob and "reveal" not in blob, name
    assert snapshot(await get_case(c, case["id"])) == snapshot(closed)


# ======================================================================================
# 3. Agent loop honesty (scripted provider; real app, CaseService and evaluator)
# ======================================================================================


def call(name: str, args: Any) -> dict[str, Any]:
    return completion(None, [(name, args)])


async def run_agent(
    lab: Lab,
    steps: list[Any],
    *,
    kind: str = "investigate",
    mode: str | None = None,
    c: httpx.AsyncClient | None = None,
    case: dict[str, Any] | None = None,
    message: str | None = None,
) -> tuple[httpx.AsyncClient, dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Start a case run with the given scripted provider steps and wait for it to finish.
    Returns (client, case after the run, run, events)."""
    assert lab.provider is not None
    if c is None or case is None:
        c, case = await observing_case(lab, mode or ("agent" if kind == "investigate" else "guide"))
    lab.provider.steps[:] = list(steps)
    body: dict[str, Any] = {"kind": kind}
    if message is not None:
        body["message"] = message
    r = await c.post(f"/api/cases/{case['id']}/agent-runs", json=body)
    assert r.status_code == 201, r.text
    run = await wait_run(c, r.json()["id"])
    evs = await events(c, run["id"])
    return c, await get_case(c, case["id"]), run, evs


def results(evs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in evs if e["type"] == "tool_result"]


def tool_calls(evs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in evs if e["type"] == "tool_call"]


async def test_hint_run_is_read_only(lab_factory: LabFactory) -> None:
    lab = await lab_factory(provider=ScriptedProvider())
    c, case = await observing_case(lab, "guide")
    calls = len(lab.sim.calls)
    c, after, run, evs = await run_agent(
        lab,
        [
            call("run_experiment", {"instrument": "source_current_sweep"}),
            call("submit_diagnosis", dx_body(case, "fault")),
            call("assess_evidence", {}),
            FINAL,
        ],
        kind="hint",
        c=c,
        case=case,
    )
    assert lab.provider is not None
    assert lab.provider.tool_names(0) == {"get_case", "read_observation"}
    assert run["status"] == "completed"
    assert [r["ok"] for r in results(evs)] == [False, False, False]
    assert all(r["error"]["code"] == "validation_error" for r in results(evs))
    assert all(tc["arguments"] == {} for tc in tool_calls(evs))
    assert len(lab.sim.calls) == calls, "a hint run reached the simulator"
    assert after["budget"]["used"] == 0 and after["diagnosis"] is None
    assert after["hints_used"] == 1 and after["active_run_id"] is None


async def test_explore_run_cannot_touch_investigations(lab_factory: LabFactory) -> None:
    lab = await lab_factory(provider=ScriptedProvider())
    c, case = await observing_case(lab, "agent")
    rev = (await c.post("/api/revisions", json={"template_id": "rc_lowpass"})).json()
    intended = case["intended_revision"]["id"]
    before = snapshot(case)
    calls = len(lab.sim.calls)
    lab.provider.steps[:] = [  # type: ignore[union-attr]
        call("get_case", {}),
        call("submit_diagnosis", dx_body(case, "fault")),
        call("run_experiment", {"revision_id": intended}),
        call("inspect_circuit", {"revision_id": intended}),
        call("run_experiment", {"instrument": "source_current_sweep"}),
        completion("I can only work on your own revisions."),
    ]
    r = await c.post(
        "/api/explore/agent-runs", json={"revision_id": rev["id"], "message": "Help me."}
    )
    assert r.status_code == 201, r.text
    run = await wait_run(c, r.json()["id"])
    evs = await events(c, run["id"])
    assert lab.provider is not None
    assert lab.provider.tool_names(0) == {
        "inspect_circuit",
        "set_goal",
        "propose_values",
        "run_experiment",
        "compare_experiments",
    }
    assert run["status"] == "completed" and run["kind"] == "explore"
    oks = [(r["tool"], r["ok"], (r["error"] or {}).get("code")) for r in results(evs)]
    assert oks == [
        ("get_case", False, "validation_error"),
        ("submit_diagnosis", False, "validation_error"),
        ("run_experiment", False, "not_found"),
        ("inspect_circuit", False, "not_found"),
        ("run_experiment", False, "validation_error"),
    ]
    assert len(lab.sim.calls) == calls, "an explore run simulated the case design"
    assert snapshot(await get_case(c, case["id"])) == before
    # Explore endpoints refuse case IDs as revisions too.
    r = await c.post(
        "/api/explore/agent-runs", json={"revision_id": intended, "message": "Help me."}
    )
    assert err(r) == (404, "not_found")


async def test_unknown_and_invalid_tool_calls_are_never_executed(lab_factory: LabFactory) -> None:
    lab = await lab_factory(provider=ScriptedProvider())
    c, case = await observing_case(lab, "agent")
    calls = len(lab.sim.calls)
    raw_bad = completion(None, [("run_experiment", '{"instrument": "output_voltage_sweep"')])
    steps = [
        call("read_file", {"path": "../../.env"}),
        call("evaluator.reveal", {"case_id": case["id"]}),
        call("run_experiment", {"instrument": "transient_capture"}),  # probe missing
        call("run_experiment", {"instrument": "output_voltage_sweep", "case_id": case["id"]}),
        call("run_experiment", {"instrument": "output_voltage_sweep", "probe": "output_voltage"}),
        raw_bad,
        completion(None, [("run_experiment", '["output_voltage_sweep"]')]),
        call("submit_diagnosis", {"conclusion": "fault", "explanation": "R1 doubled"}),
        call("propose_repair", {"component_id": "V1"}),
        completion("Nothing was run."),
    ]
    c, after, run, evs = await run_agent(lab, steps, c=c, case=case)
    assert run["status"] == "completed"
    res = results(evs)
    assert len(res) == 9 and not any(r["ok"] for r in res)
    assert all(r["error"]["code"] == "validation_error" for r in res)
    assert all(tc["arguments"] == {} for tc in tool_calls(evs)), "refused calls show {}"
    assert len(lab.sim.calls) == calls
    assert after["budget"]["used"] == 0 and after["diagnosis"] is None
    assert after["repair_proposal"] is None and len(after["observations"]) == 1
    # The model was told why (the refusal is its tool result), never executed.
    assert lab.provider is not None
    sent = json.dumps(lab.provider.requests[-1]["messages"])
    assert "is not available in this mode" in sent and "Invalid arguments" in sent


async def test_only_the_first_tool_call_of_a_reply_runs(lab_factory: LabFactory) -> None:
    lab = await lab_factory(provider=ScriptedProvider())
    c, case = await observing_case(lab, "agent")
    multi = completion(
        None,
        [
            ("run_experiment", {"instrument": "output_voltage_sweep"}),
            ("run_experiment", {"instrument": "source_current_sweep"}),
            ("submit_diagnosis", dx_body(case, "fault")),
        ],
    )
    c, after, run, evs = await run_agent(lab, [multi, completion("Done.")], c=c, case=case)
    assert run["status"] == "completed"
    assert after["budget"]["used"] == 1 and after["diagnosis"] is None
    assert [o["kind"] for o in after["observations"]] == ["symptom", "output_voltage_sweep"]
    assert [r["ok"] for r in results(evs)] == [True, False, False]
    assert run["usage"]["tool_calls"] == 1
    assert lab.provider is not None
    tool_msgs = [m for m in lab.provider.requests[1]["messages"] if m["role"] == "tool"]
    assert len(tool_msgs) == 3, "every call id gets a result (refusals included)"


async def test_model_call_and_tool_call_caps(lab_factory: LabFactory) -> None:
    lab = await lab_factory(provider=ScriptedProvider())
    c, after, run, evs = await run_agent(lab, [call("get_case", {})] * (AGENT_MAX_MODEL_CALLS + 3))
    assert run["status"] == "limit_reached"
    assert run["error"]["code"] == "limit_reached"
    assert lab.provider is not None and len(lab.provider.requests) == AGENT_MAX_MODEL_CALLS
    assert run["usage"]["model_calls"] == AGENT_MAX_MODEL_CALLS
    assert [e["type"] for e in evs][-2:] == ["error", "run_finished"]
    # Tool-call cap (forced low here: with the policy values one call per reply means the
    # model-call cap always binds first; see the report).
    assert AGENT_MAX_TOOL_CALLS >= AGENT_MAX_MODEL_CALLS
    lab2 = await lab_factory(provider=ScriptedProvider(), limits={"max_tool_calls": 2})
    _, _, run2, _ = await run_agent(lab2, [call("get_case", {})] * 5)
    assert run2["status"] == "limit_reached" and run2["usage"]["tool_calls"] == 2
    assert lab2.provider is not None and len(lab2.provider.requests) == 3


async def test_experiment_cap_and_budget_refusals(lab_factory: LabFactory) -> None:
    lab = await lab_factory(provider=ScriptedProvider())
    steps = [
        call("run_experiment", {"instrument": "output_voltage_sweep"}),
        call("run_experiment", {"instrument": "source_current_sweep"}),
        call("run_experiment", {"instrument": "transient_capture", "probe": "source_current"}),
        call("run_experiment", {"instrument": "transient_capture", "probe": "output_voltage"}),
        completion("Budget spent."),
    ]
    c, after, run, evs = await run_agent(lab, steps)
    calls_after = len(lab.sim.calls)
    assert run["status"] == "completed"
    assert after["budget"]["used"] == 3
    res = results(evs)
    assert [r["ok"] for r in res] == [True, True, True, False]
    assert res[3]["error"]["code"] == "limit_reached"
    assert sum(o["requested_by"] == "agent" and o["counted"] for o in after["observations"]) == 3
    assert all(o["agent_run_id"] == run["id"] for o in after["observations"][1:])
    assert len(lab.sim.calls) == calls_after

    # Budget partly spent by the learner first: the CaseService refusal reaches the model and
    # is not fatal.
    c2, case2 = await observing_case(lab, "agent")
    for instrument in SWEEPS:
        assert (await experiment(c2, case2["id"], instrument)).status_code == 201
    steps = [
        call("run_experiment", {"instrument": "transient_capture", "probe": "source_current"}),
        call("run_experiment", {"instrument": "transient_capture", "probe": "output_voltage"}),
        completion("The budget is spent."),
    ]
    _, after2, run2, evs2 = await run_agent(lab, steps, c=c2, case=case2)
    assert run2["status"] == "completed"
    assert [(r["ok"], (r["error"] or {}).get("code")) for r in results(evs2)] == [
        (True, None),
        (False, "budget_exhausted"),
    ]
    assert after2["budget"]["used"] == 3


async def test_token_cap_uses_measured_usage_and_missing_usage_fails_closed(
    lab_factory: LabFactory,
) -> None:
    lab = await lab_factory(provider=ScriptedProvider())
    steps = [
        completion(None, [("get_case", {})], usage=(59_000, 0)),
        completion(None, [("get_case", {})], usage=(500, 0)),
        completion("never requested"),
    ]
    _, _, run, _ = await run_agent(lab, steps)
    assert lab.provider is not None
    assert [r["max_tokens"] for r in lab.provider.requests] == [4096, 1000]
    assert run["status"] == "limit_reached"
    assert run["usage"]["total_tokens"] == 59_500 and run["usage"]["model_calls"] == 2

    _, _, run, _ = await run_agent(
        lab, [completion(None, [("get_case", {})], usage=(AGENT_MAX_TOTAL_TOKENS, 7))]
    )
    assert run["status"] == "limit_reached"
    assert run["usage"]["total_tokens"] == AGENT_MAX_TOTAL_TOKENS + 7

    _, after, run, evs = await run_agent(
        lab,
        [
            completion(
                None, [("run_experiment", {"instrument": "output_voltage_sweep"})], usage=None
            )
        ],
    )
    assert run["status"] == "failed" and run["error"]["code"] == "ai_error"
    assert "usage" in run["error"]["message"]
    assert after["budget"]["used"] == 0, "a reply without usage must not be executed"
    assert not tool_calls(evs)


async def test_wall_time_cap_stops_inference_and_simulation(lab_factory: LabFactory) -> None:
    lab = await lab_factory(provider=ScriptedProvider(), limits={"wall_time_s": 1.0})
    _, after, run, _ = await run_agent(lab, [Hang(30)])
    duration = (ts(run["finished_at"]) - ts(run["created_at"])).total_seconds()
    assert 0.9 <= duration < 3.0, duration
    assert run["status"] == "limit_reached" and "time limit" in run["error"]["message"]
    assert lab.provider is not None and lab.provider.cancelled == 1
    assert after["active_run_id"] is None

    c, case = await observing_case(lab, "agent")
    lab.sim.gate = asyncio.Event()  # the experiment would never finish
    _, after, run, evs = await run_agent(
        lab, [call("run_experiment", {"instrument": "source_current_sweep"})], c=c, case=case
    )
    lab.sim.gate.set()
    assert run["status"] == "limit_reached"
    assert after["budget"]["used"] == 0
    assert after["observations"][-1]["status"] == "cancelled"
    assert after["observations"][-1]["counted"] is False


async def test_retries_only_for_transient_provider_errors(lab_factory: LabFactory) -> None:
    lab = await lab_factory(provider=ScriptedProvider())
    assert lab.provider is not None
    cases: list[tuple[list[Any], str, int]] = [
        ([Status(503), completion("Recovered.")], "completed", 2),
        ([Status(429), completion("Recovered.")], "completed", 2),
        ([Status(500), Status(502)], "failed", 2),  # only one retry per run
        ([Status(400)], "failed", 1),
        ([Status(401)], "failed", 1),
        ([Status(404)], "failed", 1),
    ]
    for steps, status, n_requests in cases:
        before = len(lab.provider.requests)
        _, _, run, evs = await run_agent(lab, steps)
        assert run["status"] == status, (steps, run)
        assert len(lab.provider.requests) - before == n_requests, steps
        assert run["usage"]["model_calls"] == n_requests
        if status == "failed":
            assert run["error"]["code"] == "ai_error"
            assert FAKE_KEY not in json.dumps(run)
    # A simulator failure after the CaseService's own retry is fatal for the run; the loop
    # does not retry tools and makes no further model call.
    c, case = await observing_case(lab, "agent")
    lab.sim.fail_all = True
    before = len(lab.provider.requests)
    _, after, run, _ = await run_agent(
        lab,
        [
            call("run_experiment", {"instrument": "transient_capture", "probe": "output_voltage"}),
            completion("never requested"),
        ],
        c=c,
        case=case,
    )
    lab.sim.fail_all = False
    assert run["status"] == "failed" and run["error"]["code"] == "simulation_failed"
    assert len(lab.provider.requests) - before == 1
    assert after["budget"]["used"] == 0 and after["budget"]["infrastructure_failures"] == 1
    assert after["observations"][-1]["status"] == "failed"


async def test_cancel_endpoint_mid_inference_and_mid_tool(lab_factory: LabFactory) -> None:
    lab = await lab_factory(provider=ScriptedProvider())
    c, case, run_id = await start_hanging_run(lab)
    t0 = time.monotonic()
    r = await c.post(f"/api/agent-runs/{run_id}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert time.monotonic() - t0 < 3.0
    assert lab.provider is not None and lab.provider.cancelled == 1
    after = await get_case(c, case["id"])
    assert after["active_run_id"] is None and after["mode"] == "agent"
    evs = await events(c, run_id)
    assert evs[-1]["type"] == "run_finished" and not [e for e in evs if e["type"] == "message"]

    c2, case2 = await observing_case(lab, "agent")
    lab.sim.gate = asyncio.Event()
    lab.provider.steps[:] = [call("run_experiment", {"instrument": "source_current_sweep"})]
    r = await c2.post(f"/api/cases/{case2['id']}/agent-runs", json={"kind": "investigate"})
    run_id = r.json()["id"]
    await wait_until(lambda: lab.sim.active > 0)
    r = await c2.post(f"/api/agent-runs/{run_id}/cancel")
    assert r.json()["status"] == "cancelled"
    after = await get_case(c2, case2["id"])
    assert after["budget"]["used"] == 0 and after["active_run_id"] is None
    assert after["observations"][-1]["status"] == "cancelled"
    assert lab.sim.cancelled_calls >= 1
    # The learner can take over immediately.
    lab.sim.gate.set()
    assert (await experiment(c2, case2["id"], "source_current_sweep")).status_code == 201


async def test_hidden_reasoning_never_surfaces(lab_factory: LabFactory) -> None:
    lab = await lab_factory(provider=ScriptedProvider())
    c, case = await observing_case(lab, "agent")
    sym = symptom_id(case)
    steps = [
        completion(
            "<think>SECRET-THINK-1 maybe R1 doubled</think>",
            [("get_case", {})],
            reasoning="SECRET-REASONING-1",
        ),
        completion(
            "<THINK>SECRET-THINK-2</THINK>Let me re-read the symptom. <think>SECRET-THINK-3 cut",
            [("read_observation", {"observation_id": sym})],
        ),
        completion(
            "SECRET-THINK-4 dangling</think>Final visible answer [" + sym + "].",
            reasoning="SECRET-REASONING-2",
        ),
    ]
    c, after, run, evs = await run_agent(lab, steps, c=c, case=case)
    assert run["status"] == "completed"
    messages = [e["text"] for e in evs if e["type"] == "message"]
    assert messages == ["Let me re-read the symptom.", f"Final visible answer [{sym}]."]
    blob = json.dumps(evs) + json.dumps(run) + json.dumps(after)
    assert lab.provider is not None
    later_prompts = json.dumps([r["messages"] for r in lab.provider.requests[1:]])
    for secret in ("SECRET-THINK", "SECRET-REASONING", "<think", "</think"):
        assert secret.lower() not in blob.lower(), secret
        assert secret.lower() not in later_prompts.lower(), secret


async def test_citations_are_checked_never_invented(lab_factory: LabFactory) -> None:
    lab = await lab_factory(provider=ScriptedProvider())
    c, old = await observing_case(lab, "agent")
    foreign = symptom_id(old)  # same session, an earlier (closed) case
    assert (await c.post(f"/api/cases/{old['id']}/close")).status_code == 200
    case = await create_case(c)
    assert (await set_mode(c, case["id"], "agent")).status_code == 200
    lab.sim.fail_all = True
    assert (await experiment(c, case["id"], "transient_capture", "output_voltage")).status_code
    lab.sim.fail_all = False
    case = await get_case(c, case["id"])
    failed = case["observations"][-1]["id"]
    assert case["observations"][-1]["status"] == "failed"
    sym = symptom_id(case)
    fake = "obs_FAKEfakeFAKEfake1"
    text = (
        f"Whistle lower [{sym}], also [{fake}], [{foreign}], [{failed}] and "
        "[exp_FAKEfakeFAKEfake2]."
    )
    c, after, run, evs = await run_agent(lab, [completion(text)], c=c, case=case)
    msg = next(e for e in evs if e["type"] == "message")
    assert [(x["result_id"], x["quantity"]) for x in msg["citations"]] == [(sym, "tone_gain_db")]
    for bad in (fake, foreign, failed, "exp_FAKEfakeFAKEfake2"):
        assert bad not in msg["text"], bad
    assert msg["text"].count("unverified citation") == 4
    assert any(e["type"] == "status" and "unverified" in e["text"] for e in evs)
    assert run["final_message"] == msg["text"]


async def test_agent_diagnosis_validation_does_not_consume_the_diagnosis(
    lab_factory: LabFactory,
) -> None:
    lab = await lab_factory(provider=ScriptedProvider())
    c, case = await observing_case(lab, "agent")
    sym = symptom_id(case)
    good = dx_body(case, "fault")
    steps = [
        call("submit_diagnosis", {"conclusion": "fault", "explanation": "no hypothesis"}),
        call(
            "submit_diagnosis",
            {
                **good,
                "citations": [
                    {"observation_id": "obs_FAKEfakeFAKEfake1", "quantity": "tone_gain_db"}
                ],
            },
        ),
        call(
            "submit_diagnosis",
            {**good, "citations": [{"observation_id": sym, "quantity": "source_current_ma"}]},
        ),
        call("submit_diagnosis", good),
        call("submit_diagnosis", dx_body(case, "no_fault")),
        call("propose_repair", {"component_id": "R1", "rationale": "Restore R1."}),
        completion(f"I diagnosed a raised R1 from the symptom [{sym}]."),
    ]
    c, after, run, evs = await run_agent(lab, steps, c=c, case=case)
    assert run["status"] == "completed"
    oks = [(r["ok"], (r["error"] or {}).get("code")) for r in results(evs)]
    assert oks == [
        (False, "validation_error"),
        (False, "validation_error"),
        (False, "validation_error"),
        (True, None),
        (False, "conflict"),
        (True, None),
    ]
    assert after["status"] == "diagnosis_submitted"
    dx = after["diagnosis"]
    assert dx["submitted_by"] == "agent" and dx["agent_run_id"] == run["id"]
    assert dx["submission"]["hypothesis_id"] == "r1_increased"
    assert [e["diagnosis_id"] for e in evs if e["type"] == "diagnosis"] == [dx["id"]]
    assert after["repair_proposal"]["proposed_by"] == "agent"
    assert after["repair"] is None, "the agent never verifies a repair itself"


# --------------------------------------------------------------------------------------
# AI unavailable, secrets, run labeling
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "model", "fixture", "state"),
    [
        (None, MODEL, False, "missing_key"),
        ("your_nebius_api_key_here", MODEL, False, "missing_key"),
        ("   ", MODEL, False, "missing_key"),
        (FAKE_KEY, None, False, "missing_model"),
        (FAKE_KEY, MODEL, True, "dev_fixture"),
    ],
)
async def test_ai_unavailable_is_503_with_setup_hint_and_no_side_effects(
    lab_factory: LabFactory, key: str | None, model: str | None, fixture: bool, state: str
) -> None:
    """The REAL client factory (no scripted provider): no run is created, the case's mode is
    not changed, and every manual path still works."""
    lab = await lab_factory(key=key, model=model, resonance_dev_agent_fixture=fixture)
    c = lab.client()
    health = (await c.get("/api/health")).json()
    assert health["ai"]["available"] is False and health["ai"]["state"] == state
    case = await create_case(c)
    before = snapshot(case)
    for kind in ("investigate", "hint"):
        r = await c.post(f"/api/cases/{case['id']}/agent-runs", json={"kind": kind})
        assert err(r) == (503, "ai_unavailable"), r.text
        message = r.json()["error"]["message"]
        assert "NEBIUS_API_KEY" in message and ".env" in message
        assert FAKE_KEY not in r.text
    rev = (await c.post("/api/revisions", json={"template_id": "rc_lowpass"})).json()
    r = await c.post("/api/explore/agent-runs", json={"revision_id": rev["id"], "message": "hi"})
    assert err(r) == (503, "ai_unavailable")
    assert lab.runs(c.session_id) == []
    assert snapshot(await get_case(c, case["id"])) == before, "a 503 changed the case"
    # The manual investigation keeps working without AI.
    assert (await set_mode(c, case["id"], "guide")).status_code == 200
    assert (await experiment(c, case["id"], "source_current_sweep")).status_code == 201
    assert (await diagnose(c, case, "insufficient")).status_code == 200
    assert (await c.post(f"/api/cases/{case['id']}/close")).status_code == 200


def test_dev_fixture_is_off_by_default() -> None:
    assert Settings(_env_file=None).resonance_dev_agent_fixture is False  # type: ignore[call-arg]


async def test_api_key_never_leaks_and_runs_are_labeled_nebius(
    lab_factory: LabFactory, caplog: pytest.LogCaptureFixture
) -> None:
    for name in ("", "resonance", "openai", "httpx", "httpcore"):
        caplog.set_level(logging.DEBUG, logger=name or None)
    provider = RecordingProvider()
    lab = await lab_factory(provider=provider)
    c = lab.client()
    blobs: list[str] = [(await c.get("/api/health")).text]
    case = await create_case(c)
    assert (await set_mode(c, case["id"], "agent")).status_code == 200
    provider.steps[:] = [
        call("get_case", {}),
        call("run_experiment", {"instrument": "source_current_sweep"}),
        lambda body: call(
            "read_observation",
            {
                "observation_id": json.loads(
                    next(m for m in reversed(body["messages"]) if m["role"] == "tool")["content"]
                )["observation_id"]
            },
        ),
        call("submit_diagnosis", dx_body(case, "fault")),
        call("propose_repair", {"component_id": "R1"}),
        completion("Done."),
    ]
    r = await c.post(f"/api/cases/{case['id']}/agent-runs", json={"kind": "investigate"})
    blobs.append(r.text)
    run = await wait_run(c, r.json()["id"])
    assert run["status"] == "completed", run
    assert run["source"] == "nebius" and run["model"] == MODEL
    blobs += [json.dumps(run), json.dumps(await events(c, run["id"]))]
    blobs.append((await c.get(f"/api/agent-runs/{run['id']}/events")).text)  # SSE replay
    blobs.append(json.dumps(await get_case(c, case["id"])))
    blobs.append((await c.get("/api/workbench")).text)
    # A rejected key (401) on a second case.
    assert (await c.post(f"/api/cases/{case['id']}/close")).status_code == 200
    case2 = await create_case(c)
    provider.steps[:] = [Status(401)]
    r = await c.post(f"/api/cases/{case2['id']}/agent-runs", json={"kind": "investigate"})
    run2 = await wait_run(c, r.json()["id"])
    assert run2["status"] == "failed" and "NEBIUS_API_KEY" in run2["error"]["message"]
    assert run2["source"] == "nebius"
    blobs += [json.dumps(run2), json.dumps(await events(c, run2["id"]))]
    blobs.append(json.dumps(provider.requests))
    blobs.append(caplog.text)
    # The log capture is not vacuous: SDK debug and transport logs were recorded.
    assert "Building HTTP request" in caplog.text and "HTTP Request" in caplog.text
    # The key really was in use (sent only as the bearer token) ...
    assert provider.auth_headers and set(provider.auth_headers) == {f"Bearer {FAKE_KEY}"}
    # ... and appears nowhere else: payloads, events, SSE, request bodies, logs, databases.
    for blob in blobs:
        assert FAKE_KEY not in blob
    for path in lab.settings.resonance_data_dir.rglob("*"):
        if path.is_file():
            assert FAKE_KEY.encode() not in path.read_bytes(), path.name


# --------------------------------------------------------------------------------------
# 4. Explore agent: proposals are previews; the learner applies or dismisses them
# --------------------------------------------------------------------------------------


def lowpass_gain_db(f: float, r: float, c: float) -> float:
    """Independent first-order RC low-pass magnitude (not the project's analytic module)."""
    import math

    w = 2.0 * math.pi * f
    return -10.0 * math.log10(1.0 + (w * r * c) ** 2)


async def test_explore_proposals_are_previews_applied_once_or_dismissed(
    lab_factory: LabFactory,
) -> None:
    lab = await lab_factory(provider=ScriptedProvider())
    c = lab.client()
    base = (await c.post("/api/revisions", json={"template_id": "rc_lowpass"})).json()
    values = {comp["id"]: comp["value_si"] for comp in base["components"] if comp["editable"]}
    revisions_before = (await c.get("/api/workbench")).json()["revisions"]
    lab.provider.steps[:] = [  # type: ignore[union-attr]
        call(
            "propose_values",
            {
                "changes": [{"component_id": "R1", "value": "2.2k"}],
                "rationale": "Lower the cutoff.",
            },
        ),
        call(
            "propose_values",
            {"changes": [{"component_id": "C1", "value": "47n"}], "rationale": "Alternative."},
        ),
        call(
            "propose_values",
            {"changes": [{"component_id": "R1", "value": "1.5k"}], "rationale": "Milder."},
        ),
        call("run_experiment", {"revision_id": base["id"]}),
        lambda body: completion(
            "Measured the current design ["
            + json.loads(
                next(m for m in reversed(body["messages"]) if m["role"] == "tool")["content"]
            )["experiment_id"]
            + "]. Prediction: R1 = 2.2 kΩ lowers the cutoff."
        ),
    ]
    r = await c.post(
        "/api/explore/agent-runs", json={"revision_id": base["id"], "message": "Quieter whistle"}
    )
    run = await wait_run(c, r.json()["id"])
    assert run["status"] == "completed", run
    evs = await events(c, run["id"])
    workbench = (await c.get("/api/workbench")).json()
    assert workbench["revisions"] == revisions_before, "the agent created or applied a revision"
    proposals = [e["proposal"] for e in evs if e["type"] == "proposal"]
    assert len(proposals) == 3 and all(p["status"] == "pending" for p in proposals)
    assert {p["id"] for p in workbench["proposals"]} == {p["id"] for p in proposals}
    results_ = results(evs)
    assert all(r["ok"] for r in results_)
    msg = next(e for e in evs if e["type"] == "message")
    assert len(msg["citations"]) == 1 and msg["citations"][0]["result_id"].startswith("exp_")

    # Predictions are labeled analytical and agree with an independent formula.
    goal = workbench["goal"]
    for p in proposals:
        new = dict(values)
        for ch in p["changes"]:
            new[ch["component_id"]] = ch["to_value_si"]
        assert p["predicted"], p
        for pred in p["predicted"]:
            assert pred["basis"] == "analytical_first_order_rc"
            expected = lowpass_gain_db(pred["frequency_hz"], new["R1"], new["C1"])
            assert abs(pred["predicted_gain_db"] - expected) < 1e-9
        by_f = {x["frequency_hz"]: -x["predicted_gain_db"] for x in p["predicted"]}
        passes = all(
            by_f[k["frequency_hz"]] <= k["limit_db"]
            if k["kind"] == "preserve"
            else by_f[k["frequency_hz"]] >= k["limit_db"]
            for k in goal["constraints"]
        )
        assert p["predicted_goal_pass"] is passes
    first_content = json.loads(
        next(
            m
            for m in lab.provider.requests[1]["messages"]  # type: ignore[union-attr]
            if m["role"] == "tool"
        )["content"]
    )
    assert first_content["applied"] is False and "not measurements" in first_content["note"]

    p1, p2, p3 = (p["id"] for p in proposals)
    apply = {"parent_id": base["id"], "origin": "agent_proposal_applied", "proposal_id": p1}
    r = await c.post("/api/revisions", json=apply)
    assert r.status_code == 201, r.text
    applied = r.json()
    r1 = next(x for x in applied["components"] if x["id"] == "R1")
    assert abs(r1["value_si"] - 2200.0) < 1e-6 and applied["origin"] == "agent_proposal_applied"
    assert err(await c.post("/api/revisions", json=apply)) == (409, "conflict")
    assert err(await c.post(f"/api/proposals/{p1}/dismiss")) == (409, "conflict")
    stored = lab.store.get("proposal", p1, c.session_id, EditProposal)  # type: ignore[attr-defined]
    assert stored is not None and stored.status == "applied"

    r = await c.post(f"/api/proposals/{p2}/dismiss")
    assert r.status_code == 200 and r.json()["status"] == "dismissed"
    assert (await c.post(f"/api/proposals/{p2}/dismiss")).json()["status"] == "dismissed"
    r = await c.post("/api/revisions", json={**apply, "proposal_id": p2})
    assert err(r) == (409, "conflict")

    r = await c.post(
        "/api/revisions", json={**apply, "proposal_id": p3, "parent_id": applied["id"]}
    )
    assert err(r) == (409, "stale_revision")
    other = lab.client()
    assert err(await other.post("/api/revisions", json={**apply, "proposal_id": p3})) == (
        404,
        "not_found",
    )
    assert err(await other.post(f"/api/proposals/{p3}/dismiss")) == (404, "not_found")
    pending = {p["id"] for p in (await c.get("/api/workbench")).json()["proposals"]}
    assert pending == {p3}


# ======================================================================================
# 5. Robustness: a run lost in a crash/restart
# ======================================================================================


async def test_case_owned_by_a_run_lost_in_a_restart_can_be_taken_over(
    lab_factory: LabFactory,
) -> None:
    """After a crash mid-run (no graceful shutdown), the persisted run is reported as failed
    (documented). The case it owned must not stay locked: the learner must be able to cancel
    or take over without closing (and thereby revealing) the case."""
    lab1 = await lab_factory(provider=ScriptedProvider())
    c1, case, run_id = await start_hanging_run(lab1)
    # Simulate the crash: a new process (fresh in-memory registries) on the same data files.
    lab2 = await lab_factory(settings=lab1.settings)
    c2 = lab2.client(c1.session_id)  # type: ignore[attr-defined]
    run = (await c2.get(f"/api/agent-runs/{run_id}")).json()
    assert run["status"] == "failed"
    await c2.post(f"/api/agent-runs/{run_id}/cancel")
    after = await get_case(c2, case["id"])
    r = await experiment(c2, case["id"], "source_current_sweep")
    assert (after["active_run_id"], r.status_code) == (None, 201), (
        after["available_actions"],
        r.text,
    )


# ======================================================================================
# 6. Real ngspice: close while the agent's transient capture is running
# ======================================================================================


@pytest.mark.ngspice
async def test_ngspice_close_during_agent_transient(
    lab_factory: LabFactory, settings_factory: Callable[..., Settings]
) -> None:
    from resonance.simulation import create_simulator

    settings = settings_factory(
        nebius_api_key=FAKE_KEY, nebius_model=MODEL, nebius_base_url="https://provider.test/v1/"
    )
    simulator = create_simulator(settings)
    simulator.info()
    provider = ScriptedProvider(
        [
            call("run_experiment", {"instrument": "transient_capture", "probe": "source_current"}),
            Hang(60),
        ]
    )
    lab = await lab_factory(provider=provider, sim=simulator, settings=settings)  # type: ignore[arg-type]
    c = lab.client()
    case = await create_case(c)
    assert (await set_mode(c, case["id"], "agent")).status_code == 200
    r = await c.post(f"/api/cases/{case['id']}/agent-runs", json={"kind": "investigate"})
    run_id = r.json()["id"]

    async def capturing() -> bool:
        evs = await events(c, run_id)
        return any(e["type"] == "status" and e["text"].startswith("Capturing") for e in evs)

    deadline = time.monotonic() + 10
    while not await capturing():
        assert time.monotonic() < deadline
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.2)  # inside the ngspice job
    t0 = time.monotonic()
    closed = await c.post(f"/api/cases/{case['id']}/close")
    elapsed = time.monotonic() - t0
    assert closed.status_code == 200 and elapsed < CASE_CLOSE_AGENT_WAIT_S, elapsed
    revealed_at = ts(closed.json()["reveal"]["revealed_at"])
    evs = await events(c, run_id)
    assert_sealed_before_reveal(evs, revealed_at)
    final = await get_case(c, case["id"])
    assert final["budget"]["used"] == 0
    assert all(ts(o["created_at"]) <= revealed_at for o in final["observations"])
    assert not any(o["counted"] for o in final["observations"])
    await asyncio.sleep(0.5)
    jobs = settings.jobs_dir
    leftovers = [p for p in jobs.iterdir()] if jobs.exists() else []
    assert leftovers == [], f"job directories left behind: {leftovers}"


# ======================================================================================
# 7. What the agent is told about its own options (agent-visible case view)
# ======================================================================================


def tool_content(provider: ScriptedProvider, request_index: int) -> dict[str, Any]:
    """The JSON tool result the loop sent back in provider request ``request_index``."""
    msgs = provider.requests[request_index]["messages"]
    content: dict[str, Any] = json.loads(
        next(m for m in reversed(msgs) if m["role"] == "tool")["content"]
    )
    return content


async def test_agent_view_lists_the_actions_the_agent_can_take(lab_factory: LabFactory) -> None:
    """views.case_view documents ``available_actions: subset of [run_experiment,
    submit_diagnosis, propose_repair]``. During the agent's own run it must list what the agent
    can actually do; the run below then runs an experiment and submits a diagnosis."""
    lab = await lab_factory(provider=ScriptedProvider())
    c, case = await observing_case(lab, "agent")
    steps = [
        call("get_case", {}),
        call("run_experiment", {"instrument": "source_current_sweep"}),
        call("submit_diagnosis", dx_body(case, "fault")),
        completion("Done."),
    ]
    _, after, run, evs = await run_agent(lab, steps, c=c, case=case)
    assert run["status"] == "completed"
    assert [r["ok"] for r in results(evs)] == [True, True, True], "the actions were possible"
    assert lab.provider is not None
    seen = tool_content(lab.provider, 1)["available_actions"]
    after_dx = tool_content(lab.provider, 3)["available_actions"]
    assert {"run_experiment", "submit_diagnosis"} <= set(seen), seen
    assert "propose_repair" in after_dx, after_dx


async def test_failed_hint_run_is_not_reported_as_a_used_hint(lab_factory: LabFactory) -> None:
    """A hint is counted when the run is claimed, before any inference. A hint run that fails
    without showing any text must not appear in hints_used / CaseOutcome.hints_used as if the
    learner had received help."""
    lab = await lab_factory(provider=ScriptedProvider())
    c, case = await observing_case(lab, "guide")
    _, after, run, evs = await run_agent(lab, [Status(401)], kind="hint", c=c, case=case)
    assert run["status"] == "failed"
    assert not [e for e in evs if e["type"] == "message"]
    assert after["hints_used"] == 0
    closed = (await c.post(f"/api/cases/{case['id']}/close")).json()
    assert closed["reveal"]["outcome"]["hints_used"] == 0


async def test_mixed_control_is_recorded_and_agent_reads_stay_in_case(
    lab_factory: LabFactory,
) -> None:
    """Learner and agent experiments are attributed separately in the outcome; the agent can
    neither read another case's observation nor learn which instrument separates the
    explanations while the budget remains."""
    lab = await lab_factory(provider=ScriptedProvider())
    c, old = await observing_case(lab, "agent")
    foreign = symptom_id(old)
    assert (await c.post(f"/api/cases/{old['id']}/close")).status_code == 200
    case = await create_case(c)
    assert (await set_mode(c, case["id"], "agent")).status_code == 200
    assert (await experiment(c, case["id"], "output_voltage_sweep")).status_code == 201
    steps = [
        call("read_observation", {"observation_id": foreign}),
        call("assess_evidence", {}),
        call("run_experiment", {"instrument": "source_current_sweep"}),
        completion("Measured."),
    ]
    _, after, run, evs = await run_agent(lab, steps, c=c, case=case)
    assert run["status"] == "completed"
    res = results(evs)
    assert (res[0]["ok"], res[0]["error"]["code"]) == (False, "not_found")
    assert lab.provider is not None
    evidence = tool_content(lab.provider, 2)
    assert set(evidence) == {"hypothesis_status", "remaining", "statement"}
    statement = evidence["statement"].lower()
    for word in ("current", "sweep", "transient", "instrument"):
        assert word not in statement, (word, statement)
    sent = json.dumps(lab.provider.requests)
    assert "distinguishing_instruments" not in sent and old["id"] not in sent
    assert (await set_mode(c, case["id"], "guide")).status_code == 200  # learner takes over
    assert (await experiment(c, case["id"], "transient_capture", "source_current")).status_code
    closed = (await c.post(f"/api/cases/{case['id']}/close")).json()
    outcome = closed["reveal"]["outcome"]
    assert (
        outcome["experiments_used"],
        outcome["experiments_by_user"],
        outcome["experiments_by_agent"],
    ) == (3, 2, 1)


async def test_case_creation_is_single_per_session_and_failure_leaves_nothing(
    lab_factory: LabFactory,
) -> None:
    lab = await lab_factory()
    c = lab.client()

    def hidden_rows() -> int:
        con = sqlite3.connect(str(lab.settings.evaluator_db_path))
        try:
            n: int = con.execute("SELECT count(*) FROM hidden_cases").fetchone()[0]
            return n
        finally:
            con.close()

    lab.sim.fail_all = True
    r = await c.post("/api/cases", json={})
    lab.sim.fail_all = False
    assert err(r) == (502, "simulation_failed")
    assert hidden_rows() == 0, "hidden state of a case that never existed was kept"
    assert (await c.get("/api/workbench")).json()["active_case_id"] is None

    lab.sim.delay_s = 0.2
    responses = await asyncio.gather(*(c.post("/api/cases", json={}) for _ in range(4)))
    created = [r for r in responses if r.status_code == 201]
    assert len(created) == 1, [r.text for r in responses]
    assert all(r.status_code == 409 for r in responses if r.status_code != 201)
    assert hidden_rows() == 1
    assert (await c.get("/api/workbench")).json()["active_case_id"] == created[0].json()["id"]
