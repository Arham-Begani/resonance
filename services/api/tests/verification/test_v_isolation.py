"""Independent verification, lens A: hidden-state isolation and leakage.

Owner: independent verification engineer. These tests attack the hidden/public boundary
(docs/contracts.md "Hidden/public boundary", V-01..V-06, V-13) from the outside, complementing
the builders' own ``tests/challenges/test_case_isolation.py``. They add what that file does not
cover:

1. Agent mode end to end over HTTP with the REAL CaseService and AgentService: a model that is
   blocked mid-inference (every mutation must be ``run_in_progress``), then a full scripted
   investigation (validation error, all three instruments incl. the source-current transient
   probe, a fault diagnosis, a repair proposal). Every browser payload, the agent run objects,
   the JSON event log, the raw SSE stream, the EXACT request bodies sent to the model, WAV
   artifacts and headers, and ALL log records (every logger, DEBUG) are scanned before close and
   compared across the five hidden conditions of the same nominal design.
2. The INVESTIGATE / HINT tool executors called directly: an explicit allow-list of keys
   (transcribed from the contract in ``agent/views.py``, not from the code), refusal while the
   case is closing (inside the run canceller) and after close, and cross-case observation reads.
3. Error hygiene with a deliberately LEAKY simulator double whose exception text contains the
   hidden values, netlist fragments, paths, and raw ngspice text.
4. Access control: owner-vs-intruder, unknown, and malformed IDs for cases, agent runs (status,
   JSON log, SSE, cancel), artifacts, and revisions; frozen case revisions in Explore; no
   endpoint other than the case instruments simulates the hidden circuit (incl. a warm-cache
   attempt); ``POST /api/cases`` refuses seed/fault input.
5. Numerical fingerprinting (real ngspice): voltage-equivalent hidden circuits must not be
   distinguishable from voltage-only payloads (finding A-01, marked xfail(strict=True)).

The model is a TEST-ONLY scripted OpenAI-compatible provider (``agent_testkit.ScriptedProvider``
over ``httpx.MockTransport``); no live inference happens here (NEBIUS_API_KEY is not configured
for verification). The simulator is the builders' clearly labelled analytical TEST DOUBLE except
for tests marked ``ngspice``. All data lives under pytest's ``tmp_path``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

_TESTS = Path(__file__).resolve().parents[1]
for _sub in ("challenges", "agent"):
    if str(_TESTS / _sub) not in sys.path:
        sys.path.insert(0, str(_TESTS / _sub))

from agent_testkit import Hang, ScriptedProvider, completion  # noqa: E402
from challenges_testkit import (  # noqa: E402
    OPAQUE_ID,
    AnalyticTestSimulator,
    Secrets,
    build_rig,
    find_leaks,
    leaf_map,
    raw_text_leaks,
    secrets_for,
    seed_for,
    skeleton,
)

import resonance.agent.service as agent_service_module  # noqa: E402
import resonance.app as app_module  # noqa: E402
from resonance.agent import views  # noqa: E402
from resonance.agent.case_tools import (  # noqa: E402
    CaseRunContext,
    hint_toolset,
    investigate_toolset,
)
from resonance.agent.tools import ToolOutcome, ToolSet  # noqa: E402
from resonance.challenges.evaluator import faults  # noqa: E402
from resonance.challenges.hypotheses import (  # noqa: E402
    HYPOTHESES_BY_ID,
    SHORT_LABELS,
    hypothesis_values,
)
from resonance.challenges.service import CaseService  # noqa: E402
from resonance.config import Settings  # noqa: E402
from resonance.contracts.common import new_id  # noqa: E402
from resonance.contracts.internal import (  # noqa: E402
    AcData,
    AcPoints,
    AcSweep,
    CancelToken,
    CircuitSpec,
    SimulationFailed,
    SimulationTimeout,
    SimulatorUnavailable,
    TransientData,
    TransientRequest,
)
from resonance.contracts.investigation import HypothesisId, PublicCase  # noqa: E402

FAULTS: tuple[HypothesisId, ...] = (
    "no_fault",
    "r1_increased",
    "r1_decreased",
    "c1_increased",
    "c1_decreased",
)
TEST_KEY = "sk-verif-A-NOT-A-REAL-KEY-7f3c2a9e"
TEST_MODEL = "verification/scripted-provider"
LEAK_MARKER = "LEAKMARKER-7d1e"

# Raw text that must never reach a browser payload, the model, or an error body.
FORBIDDEN_TEXT = [
    re.compile(p)
    for p in (
        r"\.tran\b",
        r"\.ac\b",
        r"\.control\b",
        r"\.endc\b",
        r"SIN\(",
        r"\bR1 in out\b",
        r"\bC1 out 0\b",
        r"\bV1 in\b",
        r"\bwrdata\b",
        r"trusted netlist",
        r"Circuit:",
        r"Warning:",
        r"ngspice-\d",
        r"timestep too small",
        r"\.raw\b",
        r"\.cir\b",
        r"job_",
        r"[A-Za-z]:\\\\",
        r"\.data[/\\]",
        r"sqlite3",
        r"generator_version",
        r"rc-m1-v",
        r"Traceback",
        r'"seed"',
        r'"actual_value_si"',
        LEAK_MARKER,
        re.escape(TEST_KEY),
    )
]
# Hidden-adjacent or bulky data the model must never receive (V-01).
FORBIDDEN_MODEL_KEYS = {
    "reveal",
    "seed",
    "distinguishing_instruments",
    "assessments",
    "audio",
    "waveform",
    "artifact_id",
    "url",
    "settings",
    "input_v",
    "peak_fs",
    "clips",
    "generator_version",
    "actual_value_si",
    "multiplier",
}
# Paths where a hypothesis label is public catalog data or derived from recorded evidence.
LABEL_OK = re.compile(
    r"(^|\.)(hypotheses\[\d+\]\.(id|label)|evidence(\..*)?|diagnosis(\..*)?"
    r"|hypothesis_status\[\d+\]\.id|remaining\[\d+\]|statement|summary"
    r"|evidence_check\.message|arguments(\..*)?|candidate_hypothesis_ids\[\d+\])$"
    # (tool-call arguments are the scripted model's own output)
)


# --------------------------------------------------------------------------------------
# Test doubles (TEST-ONLY)
# --------------------------------------------------------------------------------------


def _shape(kind: str, circuit: CircuitSpec, analysis: Any) -> str:
    """Everything about a simulator call except the component values."""
    return f"{kind}|{circuit.template_id}|{circuit.source!r}|{analysis!r}"


class RecordingSimulator:
    """TEST DOUBLE wrapper: records each call's shape and values, then delegates."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.calls: list[tuple[str, dict[str, float]]] = []

    def info(self) -> Any:
        return self.inner.info()

    async def ac(
        self, circuit: CircuitSpec, sweep: AcSweep | AcPoints, *, cancel: CancelToken | None = None
    ) -> AcData:
        self.calls.append((_shape("ac", circuit, sweep), dict(circuit.values)))
        result: AcData = await self.inner.ac(circuit, sweep, cancel=cancel)
        return result

    async def transient(
        self, circuit: CircuitSpec, request: TransientRequest, *, cancel: CancelToken | None = None
    ) -> TransientData:
        self.calls.append((_shape("transient", circuit, request), dict(circuit.values)))
        result: TransientData = await self.inner.transient(circuit, request, cancel=cancel)
        return result


class LeakySimulator(AnalyticTestSimulator):
    """TEST DOUBLE: while ``mode`` is set, every call fails with exception text containing the
    component values, netlist fragments, a job path, and raw ngspice output (what a careless
    lower layer could produce). ``garbage`` returns non-finite data instead."""

    mode: str | None = None

    def _text(self, circuit: CircuitSpec) -> str:
        r, c = circuit.values["R1"], circuit.values["C1"]
        return (
            f"{LEAK_MARKER} R1={r!r} C1={c!r} ({r:g} ohm, {c:e} F)\n* resonance trusted netlist\n"
            f"V1 in 0 SIN(0 0.6 440)\nR1 in out {r!r}\nC1 out 0 {c!r}\n.tran 1e-05 1.4\n"
            ".control\nwrdata D:\\\\GitHub\\\\RESONANCE\\\\.data\\\\jobs\\\\job_x\\\\result.raw\n"
            ".endc\nCircuit: * resonance trusted netlist\nWarning: timestep too small"
        )

    def _raise(self, circuit: CircuitSpec) -> None:
        text = self._text(circuit)
        if self.mode == "failed":
            raise SimulationFailed(text)
        if self.mode == "timeout":
            raise SimulationTimeout(text)
        if self.mode == "unavailable":
            raise SimulatorUnavailable(text)
        if self.mode == "runtime":
            raise RuntimeError(text)

    async def ac(
        self, circuit: CircuitSpec, sweep: AcSweep | AcPoints, *, cancel: CancelToken | None = None
    ) -> AcData:
        self._raise(circuit)
        data = await super().ac(circuit, sweep, cancel=cancel)
        if self.mode == "garbage":
            nan = np.full_like(data.frequency_hz, np.nan, dtype=np.complex128)
            return AcData(data.frequency_hz, nan, nan, nan)
        return data

    async def transient(
        self, circuit: CircuitSpec, request: TransientRequest, *, cancel: CancelToken | None = None
    ) -> TransientData:
        self._raise(circuit)
        data = await super().transient(circuit, request, cancel=cancel)
        if self.mode == "garbage":
            nan = np.full_like(data.time_s, np.nan)
            return TransientData(data.time_s, nan, nan, nan)
        return data


class ProviderHolder:
    """Lets each step of a test choose the scripted provider (TEST-ONLY, never AI)."""

    def __init__(self) -> None:
        self.provider = ScriptedProvider([])

    def factory(self, settings: Settings) -> Any:
        return self.provider.client_factory()(settings)


# --------------------------------------------------------------------------------------
# App rig
# --------------------------------------------------------------------------------------


def _settings(tmp_path: Path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        resonance_data_dir=tmp_path / "data",
        web_dist_dir=tmp_path / "no-web",
        nebius_api_key=TEST_KEY,
        nebius_model=TEST_MODEL,
        nebius_base_url="https://provider.test/v1/",
        resonance_dev_agent_fixture=False,
    )


@dataclass
class Lab:
    client: TestClient
    sim: RecordingSimulator
    providers: ProviderHolder
    settings: Settings

    @property
    def cases(self) -> CaseService:
        service: CaseService = self.client.app.state.case_service  # type: ignore[attr-defined]
        return service

    def use(self, session: str) -> None:
        self.client.cookies.set("rs_session", session)

    def harness_case(self, session: str, seed: str) -> dict[str, Any]:
        """The private in-process seed entry point (never routed over HTTP)."""
        self.use(session)
        case: PublicCase = self.client.portal.call(  # type: ignore[union-attr]
            self.cases.harness_create_case, session, seed
        )
        return case.model_dump(mode="json")


@pytest.fixture
def lab_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., Lab]]:
    clients: list[TestClient] = []

    def make(inner: Any) -> Lab:
        settings = _settings(tmp_path)
        sim = RecordingSimulator(inner)
        providers = ProviderHolder()
        monkeypatch.setattr(app_module, "create_simulator", lambda _settings: sim)
        monkeypatch.setattr(agent_service_module, "create_chat_client", providers.factory)
        client = TestClient(app_module.create_app(settings))
        client.__enter__()
        clients.append(client)
        return Lab(client, sim, providers, settings)

    yield make
    for client in clients:
        client.__exit__(None, None, None)


def _session(tag: str, i: int) -> str:
    return f"verifA{tag}{i:02d}_" + "v" * 32


def _expect(response: Any, status: int, label: str) -> Any:
    assert response.status_code == status, (label, response.status_code, response.text[:400])
    return response.json()


def _wait_until(predicate: Callable[[], Any], limit_s: float = 15.0) -> None:
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached in time")


def _wait_run(client: TestClient, run_id: str, limit_s: float = 60.0) -> dict[str, Any]:
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        run: dict[str, Any] = client.get(f"/api/agent-runs/{run_id}").json()
        if run["status"] != "running":
            return run
        time.sleep(0.03)
    raise AssertionError("agent run did not finish")


# --------------------------------------------------------------------------------------
# Scanning and skeleton helpers
# --------------------------------------------------------------------------------------


def _walk(node: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, f"{path}.{k}" if path else k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, node


def _keys(node: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(node, dict):
        for k, v in node.items():
            out.add(k)
            out |= _keys(v)
    elif isinstance(node, list):
        for v in node:
            out |= _keys(v)
    return out


def _mask_ids(text: str) -> str:
    return OPAQUE_ID.sub("<id>", text)


def _forbidden_text(payload: Any) -> list[str]:
    text = _mask_ids(payload if isinstance(payload, str) else json.dumps(payload))
    return [p.pattern for p in FORBIDDEN_TEXT if p.search(text)]


def _labels_outside_evidence(payload: Any, labels: tuple[str, ...]) -> list[tuple[str, Any]]:
    hits = []
    for path, leaf in _walk(payload):
        if isinstance(leaf, str) and any(label in leaf for label in labels):
            if not LABEL_OK.search(path):
                hits.append((path, leaf))
    return hits


def _scan(payload: Any, secret: Secrets, ids: set[str]) -> list[tuple[str, Any, str]]:
    """Value spellings, seed, and the true label outside evidence-derived paths."""
    plain = Secrets(secret.numbers, secret.strings, ())
    found = [(f.path, f.value, f.detail) for f in find_leaks(payload, plain, ids)]
    labels = _labels_outside_evidence(payload, secret.label_strings)
    found += [(p, v, "true-cause label") for p, v in labels]
    return found


def _invariant_filter(
    streams: dict[str, list[tuple[str, Any]]],
    secrets: dict[str, Secrets],
    ids: dict[str, set[str]],
) -> list[str]:
    """Findings that are not identical in all five hidden variants (identical leaves carry no
    information about the hidden fault: e.g. a public grid frequency equal to 2 x R1)."""
    maps = {f: [leaf_map(body) for _, body in stream] for f, stream in streams.items()}
    failures = []
    for fault, stream in streams.items():
        for index, (label, body) in enumerate(stream):
            for path, value, detail in _scan(body, secrets[fault], ids[fault]):
                others = [maps[o][index].get(path, object()) for o in streams if o != fault]
                if all(other == value for other in others):
                    continue
                failures.append(f"LEAK fault={fault} payload={label} {detail} at {path}")
    return failures


_EXTRA_TIMING_KEYS = {"at", "finished_at", "latency_ms"}


def _vskeleton(node: Any) -> Any:
    """Builders' skeleton (IDs, timestamps, declared measurements masked) plus: event times,
    measured latency, and opaque IDs embedded in prose."""
    base = skeleton(node)

    def post(n: Any) -> Any:
        if isinstance(n, dict):
            return {k: ("<t>" if k in _EXTRA_TIMING_KEYS else post(v)) for k, v in n.items()}
        if isinstance(n, list):
            return [post(v) for v in n]
        if isinstance(n, str):
            return _mask_ids(n)
        return n

    return post(base)


# Agent-visible measured/derived leaves (tool contents) that legitimately vary with the
# hidden fault: measured columns, measured tone values, verification outcome, and
# trusted-code evidence derived from them.
_AGENT_DERIVED = re.compile(
    r"(^|\.)(sweep\.rows\[\d+\]\[(1|3)\]|tones\[\d+\]\.(amplitude|phase_deg|gain_db)"
    r"|repair_verification\.(functional_recovery|max_gain_deviation_db|max_phase_deviation_deg"
    r"|worst_frequency_hz)|hypothesis_status|remaining|statement|evidence_check_status"
    r"|summary)$"
)


def _mask_paths(node: Any, pattern: re.Pattern[str], path: str = "") -> Any:
    if pattern.search(path):
        return "<derived>"
    if isinstance(node, dict):
        return {k: _mask_paths(v, pattern, f"{path}.{k}" if path else k) for k, v in node.items()}
    if isinstance(node, list):
        return [_mask_paths(v, pattern, f"{path}[{i}]") for i, v in enumerate(node)]
    if isinstance(node, str):
        return _mask_ids(node)
    return node


def _model_request_view(body: dict[str, Any]) -> dict[str, Any]:
    """A request sent to the model with tool-result JSON parsed (for scanning/comparison)."""
    messages = []
    for m in body["messages"]:
        m = dict(m)
        if m.get("role") == "tool":
            m["content"] = json.loads(m["content"])
        messages.append(m)
    return {"messages": messages, "tools": body.get("tools", []), "model": body.get("model")}


# --------------------------------------------------------------------------------------
# Scripted model (TEST-ONLY)
# --------------------------------------------------------------------------------------


def _results(body: dict[str, Any], tool: str) -> list[dict[str, Any]]:
    return [
        json.loads(m["content"])
        for m in body["messages"]
        if m["role"] == "tool" and m.get("name") == tool
    ]


def _call(tool: str, args: Any) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def step(body: dict[str, Any]) -> dict[str, Any]:
        return completion(None, [(tool, args(body) if callable(args) else args)])

    return step


def _current_sweep_id(body: dict[str, Any]) -> str:
    runs = [r for r in _results(body, "run_experiment") if r.get("kind") == "source_current_sweep"]
    return str(runs[-1]["observation_id"])


def investigation_script() -> list[Any]:
    """A fixed sequence (identical for every hidden condition): 10 model calls, 9 tool calls."""
    return [
        _call("get_case", {}),
        _call("assess_evidence", {}),
        _call(
            "read_observation",
            lambda b: {
                "observation_id": _results(b, "get_case")[-1]["observations"][0]["observation_id"]
            },
        ),
        _call("run_experiment", {"instrument": "transient_capture"}),  # invalid: no probe
        _call("run_experiment", {"instrument": "output_voltage_sweep"}),
        _call("run_experiment", {"instrument": "source_current_sweep"}),
        # Output-voltage capture as an EXPERIMENT (with audio for the browser; the builders'
        # HTTP isolation flow covers the source-current probe).
        _call("run_experiment", {"instrument": "transient_capture", "probe": "output_voltage"}),
        _call(
            "submit_diagnosis",
            lambda b: {
                "conclusion": "fault",
                "hypothesis_id": "r1_increased",
                "explanation": "Scripted verification step.",
                "citations": [
                    {
                        "observation_id": _current_sweep_id(b),
                        "quantity": "source_current_ma",
                        "frequency_hz": 20.0,
                    }
                ],
            },
        ),
        _call("propose_repair", {"component_id": "R1", "rationale": "Scripted verification."}),
        lambda b: completion(f"Scripted verification summary [{_current_sweep_id(b)}]."),
    ]


# --------------------------------------------------------------------------------------
# 1. Agent mode end to end, five hidden conditions, one nominal design
# --------------------------------------------------------------------------------------


@dataclass
class Flow:
    fault: HypothesisId
    seed: str
    session: str
    case_id: str = ""
    payloads: list[tuple[str, Any]] = field(default_factory=list)
    model_requests: list[dict[str, Any]] = field(default_factory=list)
    sse_text: str = ""
    sim_shapes: list[str] = field(default_factory=list)
    hidden_calls: int = 0
    artifacts: list[tuple[Any, ...]] = field(default_factory=list)
    ids: set[str] = field(default_factory=set)

    def record(self, label: str, body: Any) -> Any:
        self.payloads.append((label, body))
        return body


def _clip_urls(node: Any) -> list[str]:
    return [leaf for path, leaf in _walk(node) if path.endswith(".url") and isinstance(leaf, str)]


def _wav_chunks(data: bytes) -> list[tuple[str, int]]:
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    chunks, pos = [], 12
    while pos + 8 <= len(data):
        cid = data[pos : pos + 4].decode("latin-1")
        size = int.from_bytes(data[pos + 4 : pos + 8], "little")
        chunks.append((cid, size))
        pos += 8 + size + (size & 1)
    return chunks


def run_agent_flow(lab: Lab, flow: Flow, create: Callable[[], dict[str, Any]]) -> None:
    c = lab.client
    lab.use(flow.session)
    before = len(lab.sim.calls)
    case = flow.record("create", create())
    flow.case_id = cid = case["id"]
    base = f"/api/cases/{cid}"
    flow.record("get_ready", _expect(c.get(base), 200, "get_ready"))

    # Run 1: the model never answers. Every mutation must be refused while the run owns the case.
    lab.providers.provider = ScriptedProvider([Hang(30)])
    run1 = flow.record(
        "run1", _expect(c.post(f"{base}/agent-runs", json={"kind": "investigate"}), 201, "run1")
    )
    _wait_until(lambda: lab.providers.provider.requests)
    flow.record("get_during_run", _expect(c.get(base), 200, "get_during_run"))
    attempts: list[tuple[str, str, dict[str, Any] | None]] = [
        ("mode", "/mode", {"mode": "guide"}),
        (
            "experiment",
            "/experiments",
            {"instrument": "source_current_sweep", "client_request_id": "verif-during-run"},
        ),
        ("diagnosis", "/diagnosis", {"conclusion": "insufficient_evidence", "explanation": "x"}),
        ("proposal", "/repair-proposal", {"component_id": "C1"}),
        ("repair", "/repair", None),
        ("second_run", "/agent-runs", {"kind": "investigate"}),
    ]
    for label, suffix, body in attempts:
        r = c.post(f"{base}{suffix}", json=body) if body is not None else c.post(f"{base}{suffix}")
        body = flow.record(f"during_run_{label}", _expect(r, 409, label))
        if label in ("mode", "experiment", "diagnosis", "second_run"):
            assert body["error"]["code"] == "run_in_progress", (label, body)
    flow.record(
        "run1_cancel", _expect(c.post(f"/api/agent-runs/{run1['id']}/cancel"), 200, "cancel")
    )
    flow.record("run1_log", _expect(c.get(f"/api/agent-runs/{run1['id']}/events/log"), 200, "log1"))

    # Run 2: a complete scripted investigation through the real tool layer.
    provider = lab.providers.provider = ScriptedProvider(investigation_script())
    run2 = flow.record(
        "run2", _expect(c.post(f"{base}/agent-runs", json={"kind": "investigate"}), 201, "run2")
    )
    flow.record("run2_final", _wait_run(c, run2["id"]))
    flow.record("run2_log", _expect(c.get(f"/api/agent-runs/{run2['id']}/events/log"), 200, "log2"))
    sse = c.get(f"/api/agent-runs/{run2['id']}/events")
    assert sse.status_code == 200
    flow.sse_text = sse.text
    flow.record(
        "run2_sse",
        [json.loads(line[5:]) for line in sse.text.splitlines() if line.startswith("data:")],
    )
    flow.model_requests = list(provider.requests)
    flow.record("get_after_run", _expect(c.get(base), 200, "get_after_run"))
    flow.record("verify_repair", _expect(c.post(f"{base}/repair"), 200, "verify_repair"))
    flow.record("verify_twice", _expect(c.post(f"{base}/repair"), 409, "verify_twice"))
    flow.record("get_final", _expect(c.get(base), 200, "get_final"))
    flow.record("workbench", _expect(c.get("/api/workbench"), 200, "workbench"))
    calls = lab.sim.calls[before:]
    flow.sim_shapes = [shape for shape, _ in calls]
    nominal = {
        comp["id"]: comp["value_si"]
        for comp in case["intended_revision"]["components"]
        if comp["id"] in ("R1", "C1")
    }
    actual = hypothesis_values(nominal, flow.fault)
    flow.hidden_calls = sum(1 for _, values in calls if values == actual)
    urls = list(dict.fromkeys(u for _, body in flow.payloads for u in _clip_urls(body)))
    for url in urls:
        r = c.get(url)
        assert r.status_code == 200, url
        data = r.content
        flow.artifacts.append(
            (
                r.headers.get("content-type"),
                r.headers.get("content-length"),
                r.headers.get("cache-control"),
                r.headers.get("content-disposition"),
                tuple(sorted(k.lower() for k in r.headers if k.lower() not in ("etag",))),
                len(data),
                data[:44],
                tuple(_wav_chunks(data)),
            )
        )


def _family_seeds(which: str) -> tuple[tuple[float, float], dict[HypothesisId, str]]:
    if which == "http-3300-470n":
        nominal = (3300.0, 4.7e-07)
        return nominal, {f: seed_for(f, nominal) for f in FAULTS}
    seed = os.environ.get("VERIF_A_SEED") or faults.new_seed()  # reproduce: VERIF_A_SEED=<seed>
    design = faults.generate(seed)
    nominal = (design.nominal["R1"], design.nominal["C1"])
    seeds = {f: seed_for(f, nominal) for f in FAULTS}
    seeds[design.fault.hypothesis_id] = seed  # one fresh 128-bit seed per run
    return nominal, seeds


@pytest.mark.parametrize("which", ["http-3300-470n", "harness-random"])
def test_agent_mode_flow_leaks_nothing_and_is_identical_across_hidden_conditions(
    lab_factory: Callable[..., Lab],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    which: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    lab = lab_factory(AnalyticTestSimulator())
    nominal, seeds = _family_seeds(which)
    flows = [Flow(f, seeds[f], _session("F", i)) for i, f in enumerate(FAULTS)]
    for flow in flows:
        if which.startswith("http"):
            monkeypatch.setattr(faults, "new_seed", lambda s=flow.seed: s)

            def create(flow: Flow = flow) -> dict[str, Any]:
                return _expect(
                    lab.client.post("/api/cases", json={"template_id": "rc_lowpass"}), 201, "create"
                )  # type: ignore[no-any-return]
        else:

            def create(flow: Flow = flow) -> dict[str, Any]:
                return lab.harness_case(flow.session, flow.seed)

        run_agent_flow(lab, flow, create)
    context = f"family={which} nominal={nominal} seeds={seeds}"
    nominal_values = {"R1": nominal[0], "C1": nominal[1]}
    secrets = {f.fault: secrets_for(f.seed, f.fault, nominal_values) for f in flows}

    # Sanity: the scripted run really exercised the tool layer and the instruments.
    for flow in flows:
        final = dict(flow.payloads)["get_final"]
        assert final["budget"]["used"] == 3 and final["status"] == "repair_verification", context
        kinds = sorted(o["kind"] for o in final["observations"] if o["requested_by"] == "agent")
        assert kinds == ["output_voltage_sweep", "source_current_sweep", "transient_capture"], (
            context
        )
        log2 = dict(flow.payloads)["run2_log"]
        assert log2["run"]["status"] == "completed", (context, log2["run"])
        assert dict(flow.payloads)["run1_log"]["run"]["status"] == "cancelled", context

    # (a) Every payload before close: no reveal, no seed key, nothing hidden.
    for flow in flows:
        for label, body in flow.payloads:
            for path, leaf in _walk(body):
                if path.endswith("reveal"):
                    assert leaf is None, (context, flow.fault, label, path)
            assert "seed" not in _keys(body), (context, flow.fault, label)
            assert not _forbidden_text(body), (context, flow.fault, label, _forbidden_text(body))
        assert not _forbidden_text(flow.sse_text), (context, flow.fault)
    streams = {f.fault: f.payloads for f in flows}
    ids = {f.fault: f.ids for f in flows}
    failures = _invariant_filter(streams, secrets, ids)
    assert not failures, context + "\n" + "\n".join(failures[:20])

    # (b) distinguishing_instruments withheld while ready/observing with budget left (V-13),
    # and the evidence statement never names an instrument meanwhile.
    for flow in flows:
        for label, body in flow.payloads:
            cases = [body] if isinstance(body, dict) and "evidence" in body else []
            for case in cases:
                if case["status"] in ("ready", "observing") and case["budget"]["remaining"] > 0:
                    assert case["evidence"]["distinguishing_instruments"] is None, (label, context)
                    statement = case["evidence"]["statement"].lower()
                    assert not re.search(r"sweep|capture|current|probe", statement), statement

    # (c) The exact requests sent to the model.
    model_streams: dict[str, list[tuple[str, Any]]] = {}
    for flow in flows:
        views_sent = [_model_request_view(b) for b in flow.model_requests]
        model_streams[flow.fault] = [(f"model_request{i}", v) for i, v in enumerate(views_sent)]
        for i, v in enumerate(views_sent):
            assert not (_keys(v["messages"]) & FORBIDDEN_MODEL_KEYS), (
                context,
                flow.fault,
                i,
                _keys(v["messages"]) & FORBIDDEN_MODEL_KEYS,
            )
            raw = json.dumps(flow.model_requests[i])
            assert "/api/artifacts" not in raw and not re.search(r"\bart_[A-Za-z0-9_-]{8,}", raw)
            assert not _forbidden_text(v), (context, flow.fault, i, _forbidden_text(v))
            for m in v["messages"]:
                content = m.get("content")
                if isinstance(content, dict):  # no waveform-sized arrays reach the model
                    assert all(len(leaf) <= 31 for _, leaf in _walk_lists(content)), (
                        context,
                        flow.fault,
                    )
    failures = _invariant_filter(model_streams, secrets, ids)
    assert not failures, context + "\n" + "\n".join(failures[:20])

    # (d) Differential: identical structure in every hidden condition.
    ref = flows[0]
    for flow in flows[1:]:
        for (label, a), (_, b) in zip(ref.payloads, flow.payloads, strict=True):
            assert _vskeleton(a) == _vskeleton(b), (context, flow.fault, label)
        assert len(ref.model_requests) == len(flow.model_requests), (context, flow.fault)
        for i, (a, b) in enumerate(
            zip(model_streams[ref.fault], model_streams[flow.fault], strict=True)
        ):
            assert _mask_paths(a[1], _AGENT_DERIVED) == _mask_paths(b[1], _AGENT_DERIVED), (
                context,
                flow.fault,
                f"model_request{i}",
            )
        # Same simulation work, same call shapes, and the hidden circuit really ran each time.
        assert flow.sim_shapes == ref.sim_shapes, (context, flow.fault)
        assert flow.artifacts == ref.artifacts, (context, flow.fault)
    for flow in flows:
        if flow.fault != "no_fault":
            # symptom + three experiments each ran the hidden circuit itself (never a cached or
            # shortcut result); the repair runs the repaired variant on top.
            assert flow.hidden_calls >= 4, (context, flow.fault, flow.hidden_calls)
        for meta in flow.artifacts:
            assert meta[0] == "audio/wav" and meta[3] in (None, "inline"), meta
            assert [cid for cid, _ in meta[7]] == ["fmt ", "data"], meta[7]

    # (e) Logs from EVERY logger at DEBUG: no seed, no API key, no hidden values, no netlist.
    records = list(caplog.records)
    text = "\n".join(r.getMessage() + ("\n" + r.exc_text if r.exc_text else "") for r in records)
    for flow in flows:
        assert flow.seed not in text, (context, flow.fault)
    assert TEST_KEY not in text
    assert not [p for p in _forbidden_text(text) if p not in (r"\.ac\b",)], _forbidden_text(text)
    # The openai client's DEBUG log repeats the model request, which (c) scans structurally
    # with the invariance rule; every other record must not spell a hidden value at all.
    other = "\n".join(r.getMessage() for r in records if not r.name.startswith("openai"))
    for flow in flows:
        assert not raw_text_leaks(other, secrets[flow.fault]), (context, flow.fault)

    # (f) Reveal only after close; then everything mutating is refused (409).
    for flow in flows:
        lab.use(flow.session)
        base = f"/api/cases/{flow.case_id}"
        closed = _expect(lab.client.post(f"{base}/close"), 200, "close")
        assert closed["reveal"]["seed"] == flow.seed, context
        assert closed["reveal"]["fault"]["hypothesis_id"] == flow.fault
        if flow.fault != "no_fault":
            assert find_leaks(closed["reveal"], secrets[flow.fault]), "positive control"
        lab.providers.provider = ScriptedProvider([completion("must not run")])
        for suffix, body in (
            ("/mode", {"mode": "guide"}),
            (
                "/experiments",
                {"instrument": "output_voltage_sweep", "client_request_id": "verif-after-close"},
            ),
            ("/diagnosis", {"conclusion": "insufficient_evidence", "explanation": "x"}),
            ("/repair-proposal", {"component_id": "R1"}),
            ("/agent-runs", {"kind": "investigate"}),
            ("/agent-runs", {"kind": "hint"}),
        ):
            r = lab.client.post(f"{base}{suffix}", json=body)
            assert r.status_code == 409, (suffix, r.status_code, r.text)
        assert lab.client.post(f"{base}/repair").status_code == 409
        assert lab.providers.provider.requests == [], "no model call for a closed case"


def _walk_lists(node: Any) -> Iterator[tuple[str, list[Any]]]:
    if isinstance(node, dict):
        for v in node.values():
            yield from _walk_lists(v)
    elif isinstance(node, list):
        yield "", node
        for v in node:
            yield from _walk_lists(v)


# --------------------------------------------------------------------------------------
# 2. Tool executors and agent views: allow-list, closing/closed refusal, cross-case reads
# --------------------------------------------------------------------------------------

# Transcribed from the contract docstring in resonance/agent/views.py (the allow-list), not
# from the implementation. Keys outside these sets must never reach the model.
_OBS_BRIEF = {
    "observation_id",
    "sequence",
    "kind",
    "probe",
    "status",
    "counted",
    "requested_by",
    "summary",
}
_TONE = {
    "label",
    "role",
    "frequency_hz",
    "amplitude",
    "expected_amplitude",
    "amplitude_unit",
    "phase_deg",
    "expected_phase_deg",
    "gain_db",
    "expected_gain_db",
}
ALLOW: dict[str, set[str]] = {
    "case": {
        "case_id",
        "status",
        "mode",
        "hints_used",
        "budget",
        "intended_design",
        "instruments",
        "hypotheses",
        "evidence_tolerances",
        "observations",
        "available_actions",
        "diagnosis",
        "repair_proposal",
        "repair_verification",
    },
    "budget": {"total", "used", "remaining"},
    "intended_design": {
        "revision_id",
        "label",
        "template_id",
        "title",
        "nodes",
        "components",
        "source",
        "analytical_prediction",
    },
    "nodes": {"id", "label", "role"},
    "components": {
        "id",
        "kind",
        "value",
        "value_si",
        "unit",
        "nodes",
        "editable",
        "allowed_range_si",
        "description",
    },
    "source": {"description", "test_tones"},
    "test_tones": {"label", "role", "frequency_hz", "amplitude_v"},
    "analytical_prediction": {"note", "cutoff_hz", "time_constant_s"},
    "instruments": {"kind", "title", "description", "returns", "frequency_grid", "probes", "cost"},
    "frequency_grid": {"points", "start_hz", "stop_hz"},
    "hypotheses": {"id", "label"},
    "evidence_tolerances": {
        "gain_tolerance_db",
        "phase_tolerance_deg",
        "current_tolerance_pct",
        "current_abs_tolerance_ma",
        "tone_gain_tolerance_db",
        "tone_phase_tolerance_deg",
        "description",
    },
    "observations": _OBS_BRIEF | {"sweep", "transient", "error"},
    "observation": _OBS_BRIEF | {"sweep", "transient", "error", "budget"},
    "sweep": {"quantity", "magnitude_unit", "reference", "columns", "rows"},
    "transient": {"probe", "reference", "note", "tones"},
    "tones": _TONE,
    "diagnosis": {
        "diagnosis_id",
        "conclusion",
        "hypothesis_id",
        "candidate_hypothesis_ids",
        "submitted_by",
        "evidence_check_status",
    },
    "repair_proposal": {"repair_id", "component_id", "value", "proposed_by"},
    "repair_verification": {
        "status",
        "functional_recovery",
        "max_gain_deviation_db",
        "max_phase_deviation_deg",
        "worst_frequency_hz",
        "summary",
    },
    "evidence": {"hypothesis_status", "remaining", "statement"},
    "hypothesis_status": {"id", "status"},
    "submit_diagnosis": {
        "ok",
        "diagnosis_id",
        "case_status",
        "evidence_check_status",
        "available_actions",
    },
    "propose_repair": {"ok", "repair_id", "component_id", "value", "note"},
    "failure": {"ok", "error"},
    "error": {"code", "message"},
}


def _check_allow(node: Any, schema: str, path: str = "") -> list[str]:
    """Return keys outside the allow-list (recursively, by the key's own schema)."""
    bad: list[str] = []
    if isinstance(node, list):
        for i, v in enumerate(node):
            bad += _check_allow(v, schema, f"{path}[{i}]")
        return bad
    if not isinstance(node, dict):
        return bad
    allowed = ALLOW[schema]
    for key, value in node.items():
        if key not in allowed:
            bad.append(f"{path}.{key}")
            continue
        if isinstance(value, dict | list) and key in ALLOW:
            bad += _check_allow(value, key, f"{path}.{key}")
        elif isinstance(value, dict | list) and key not in ALLOW:
            # leaf containers: lists of scalars (returns, columns, rows, probes, ...)
            for p, leaf in _walk(value):
                if isinstance(leaf, dict):
                    bad.append(f"{path}.{key}{p}")
    return bad


async def _run_tool(toolset: ToolSet, name: str, raw: dict[str, Any]) -> ToolOutcome:
    tool = toolset.get(name)
    assert tool is not None, name
    return await tool.run(tool.args_model.model_validate(raw))


DESIGN_DIRECT = (470.0, 1e-07)


async def _investigate_directly(
    service: CaseService, session: str, seed: str
) -> tuple[str, str, ToolSet, list[tuple[str, ToolOutcome]]]:
    case = await service.harness_create_case(session, seed)
    await service.set_mode(session, case.id, "agent")
    run_id = new_id("run")
    service.claim_for_agent(session, case.id, run_id, "investigate")
    ctx = CaseRunContext(
        session_id=session, case_id=case.id, run_id=run_id, cases=service, cancel=CancelToken()
    )
    tools = investigate_toolset(ctx)
    out: list[tuple[str, ToolOutcome]] = []

    async def call(label: str, name: str, raw: dict[str, Any]) -> ToolOutcome:
        outcome = await _run_tool(tools, name, raw)
        out.append((label, outcome))
        return outcome

    got = await call("get_case", "get_case", {})
    symptom = got.content["observations"][0]["observation_id"]
    await call("assess_evidence", "assess_evidence", {})
    await call("read_symptom", "read_observation", {"observation_id": symptom})
    ovs = await call("ovs", "run_experiment", {"instrument": "output_voltage_sweep"})
    await call(
        "tc_voltage",
        "run_experiment",
        {"instrument": "transient_capture", "probe": "output_voltage"},
    )
    await call(
        "tc_current",
        "run_experiment",
        {"instrument": "transient_capture", "probe": "source_current"},
    )
    await call("budget_exhausted", "run_experiment", {"instrument": "source_current_sweep"})
    await call("read_ovs", "read_observation", {"observation_id": ovs.observation_id})
    await call("read_unknown", "read_observation", {"observation_id": "obs_unknownXXXXXXXXXX"})
    await call("get_case_full", "get_case", {})
    await call("evidence_spent", "assess_evidence", {})
    await call(
        "diagnosis",
        "submit_diagnosis",
        {
            "conclusion": "insufficient_evidence",
            "candidate_hypothesis_ids": ["r1_increased", "c1_increased"],
            "explanation": "Scripted verification step.",
        },
    )
    await call("repair", "propose_repair", {"component_id": "C1"})
    await call("get_case_after", "get_case", {})
    return case.id, run_id, tools, out


_ALLOW_FOR = {
    "get_case": "case",
    "get_case_full": "case",
    "get_case_after": "case",
    "assess_evidence": "evidence",
    "evidence_spent": "evidence",
    "read_symptom": "observation",
    "read_ovs": "observation",
    "ovs": "observation",
    "tc_voltage": "observation",
    "tc_current": "observation",
    "diagnosis": "submit_diagnosis",
    "repair": "propose_repair",
}


async def test_agent_tool_outputs_follow_the_allow_list_and_refuse_once_closing(
    settings: Settings,
) -> None:
    double = AnalyticTestSimulator()
    rig = build_rig(settings, double)
    service = rig.service
    nominal = {"R1": DESIGN_DIRECT[0], "C1": DESIGN_DIRECT[1]}
    try:
        per_fault: dict[str, list[tuple[str, ToolOutcome]]] = {}
        seeds: dict[str, str] = {}
        for i, fault in enumerate(FAULTS):
            seed = seed_for(fault, DESIGN_DIRECT)
            session = _session("T", i)
            case_id, run_id, tools, outcomes = await _investigate_directly(service, session, seed)
            per_fault[fault] = outcomes
            seeds[fault] = seed
            for label, outcome in outcomes:
                content = outcome.content
                schema = "failure" if not outcome.ok else _ALLOW_FOR[label]
                assert _check_allow(content, schema) == [], (fault, label, content)
                assert not (_keys(content) & FORBIDDEN_MODEL_KEYS), (fault, label)
                assert not _forbidden_text(content), (fault, label)
                assert not _forbidden_text(outcome.summary), (fault, label)
                text = json.dumps(content)
                assert "/api/artifacts" not in text, (fault, label)
                assert not re.search(r"\bart_[A-Za-z0-9_-]{8,}", text), (fault, label)
                assert all(len(lst) <= 31 for _, lst in _walk_lists(content)), (fault, label)
                assert seed not in text and seed not in outcome.summary
            assert dict(outcomes)["budget_exhausted"].content["error"]["code"] == (
                "budget_exhausted"
            )
            assert dict(outcomes)["read_unknown"].content["error"]["code"] == "not_found"
            # agent never sees distinguishing instruments, even after the budget is spent
            spent = dict(outcomes)["evidence_spent"].content
            assert set(spent) == {"hypothesis_status", "remaining", "statement"}

            # Closing: the run canceller executes while the case is marked closing; every
            # tool must refuse there, and again after the reveal.
            during: list[tuple[str, ToolOutcome]] = []
            symptom_id = dict(outcomes)["get_case"].content["observations"][0]["observation_id"]
            probes: list[tuple[str, dict[str, Any]]] = [
                ("get_case", {}),
                ("assess_evidence", {}),
                ("read_observation", {"observation_id": symptom_id}),
                ("run_experiment", {"instrument": "output_voltage_sweep"}),
                ("submit_diagnosis", {"conclusion": "insufficient_evidence", "explanation": "x"}),
                ("propose_repair", {"component_id": "R1"}),
            ]

            async def canceller(
                rid: str,
                tools: ToolSet = tools,
                during: list[Any] = during,
                session: str = session,
                case_id: str = case_id,
                probes: list[tuple[str, dict[str, Any]]] = probes,
            ) -> None:
                for name, raw in probes:
                    during.append((name, await _run_tool(tools, name, raw)))
                service.release_agent(session, case_id, rid)

            service.register_run_canceller(canceller)
            closed = await service.close_case(session, case_id)
            assert closed.reveal is not None and len(during) == len(probes), fault
            after = [(name, await _run_tool(tools, name, raw)) for name, raw in probes]
            for name, outcome in during + after:
                assert not outcome.ok and outcome.fatal is not None, (fault, name)
                assert outcome.content == {
                    "ok": False,
                    "error": {
                        "code": "conflict",
                        "message": "This case is closing or closed, so the AI run cannot continue.",
                    },
                }, (fault, name, outcome.content)
            with pytest.raises(views.ClosedCaseError):
                views.case_view(closed)
            with pytest.raises(views.ClosedCaseError):
                views.evidence_view(closed)

            # Cross-case: the same session's previous (closed) case observation is not
            # readable from a new case, exactly like an unknown ID.
            seed2 = seed_for(fault, DESIGN_DIRECT)
            case2 = await service.harness_create_case(session, seed2)
            await service.set_mode(session, case2.id, "agent")
            run2 = new_id("run")
            service.claim_for_agent(session, case2.id, run2, "investigate")
            tools2 = investigate_toolset(
                CaseRunContext(session, case2.id, run2, service, CancelToken())
            )
            cross = await _run_tool(tools2, "read_observation", {"observation_id": symptom_id})
            unknown = await _run_tool(
                tools2, "read_observation", {"observation_id": "obs_unknownXXXXXXXXXX"}
            )
            assert not cross.ok and cross.content == unknown.content, (
                cross.content,
                unknown.content,
            )
            service.release_agent(session, case2.id, run2)
            await service.close_case(session, case2.id)

        # Hidden values, seeds, and true labels: only leaves identical in all five hidden
        # conditions may coincide with a hidden spelling (e.g. a public grid frequency).
        streams = {
            f: [(label, {"content": o.content, "summary": o.summary}) for label, o in outs]
            for f, outs in per_fault.items()
        }
        secrets = {f: secrets_for(seeds[f], f, nominal) for f in FAULTS}
        failures = _invariant_filter(streams, secrets, {f: set() for f in FAULTS})
        assert not failures, "\n".join(failures[:20])

        # Differential across hidden conditions: identical once declared measurements and
        # evidence derived from them are masked.
        ref = per_fault[FAULTS[0]]
        for fault in FAULTS[1:]:
            for (label, a), (_, b) in zip(ref, per_fault[fault], strict=True):
                ma = _mask_paths(a.content, _AGENT_DERIVED)
                mb = _mask_paths(b.content, _AGENT_DERIVED)
                assert ma == mb, (fault, label)
                assert (a.ok, a.fatal is None, a.counted_experiment) == (
                    b.ok,
                    b.fatal is None,
                    b.counted_experiment,
                ), (fault, label)

        # Tool surface: exact names, no case/session/path/code/query arguments anywhere.
        ctx = CaseRunContext("s" * 40, "case_x", new_id("run"), service, CancelToken())
        assert investigate_toolset(ctx).names == [
            "get_case",
            "assess_evidence",
            "run_experiment",
            "read_observation",
            "submit_diagnosis",
            "propose_repair",
        ]
        assert hint_toolset(ctx).names == ["get_case", "read_observation"]
        forbidden_args = {
            "case_id",
            "session_id",
            "path",
            "file",
            "url",
            "command",
            "sql",
            "query",
            "code",
            "netlist",
            "seed",
            "values",
            "value",
            "value_si",
        }
        for schema in investigate_toolset(ctx).schemas() + hint_toolset(ctx).schemas():
            props = _keys(schema["function"]["parameters"])
            assert not (props & forbidden_args), (schema["function"]["name"], props)
    finally:
        rig.service.close()
        rig.store.close()


# --------------------------------------------------------------------------------------
# 3. Error hygiene with a leaky simulator
# --------------------------------------------------------------------------------------

DESIGN_ERRORS = (6800.0, 2.2e-08)


def _hidden_rows(settings: Settings) -> int:
    con = sqlite3.connect(str(settings.evaluator_db_path))
    try:
        return int(con.execute("SELECT COUNT(*) FROM hidden_cases").fetchone()[0])
    finally:
        con.close()


def test_simulator_failures_in_hidden_runs_never_echo_values_netlists_or_paths(
    lab_factory: Callable[..., Lab],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    leaky = LeakySimulator()
    lab = lab_factory(leaky)
    c = lab.client
    nominal = {"R1": DESIGN_ERRORS[0], "C1": DESIGN_ERRORS[1]}
    streams: dict[str, list[tuple[str, Any]]] = {}
    seeds = {f: seed_for(f, DESIGN_ERRORS) for f in FAULTS}
    for i, fault in enumerate(FAULTS):
        session = _session("E", i)
        lab.use(session)
        monkeypatch.setattr(faults, "new_seed", lambda s=seeds[fault]: s)
        rec: list[tuple[str, Any]] = streams.setdefault(fault, [])

        # Symptom fails twice at creation: no case, hidden state discarded, generic error.
        rows = _hidden_rows(lab.settings)
        leaky.mode = "failed"
        rec.append(
            (
                "create_failed",
                _expect(c.post("/api/cases", json={"template_id": "rc_lowpass"}), 502, "create"),
            )
        )
        assert _hidden_rows(lab.settings) == rows, "hidden state of a failed creation remains"
        assert c.get("/api/workbench").json()["active_case_id"] is None
        leaky.mode = None
        case = _expect(c.post("/api/cases", json={"template_id": "rc_lowpass"}), 201, "create")
        rec.append(("create", case))
        base = f"/api/cases/{case['id']}"
        rec.append(("mode", _expect(c.post(f"{base}/mode", json={"mode": "guide"}), 200, "m")))
        for k, (mode, body, status) in enumerate(
            (
                ("failed", {"instrument": "output_voltage_sweep"}, 502),
                ("timeout", {"instrument": "source_current_sweep"}, 504),
                ("runtime", {"instrument": "transient_capture", "probe": "source_current"}, 500),
            )
        ):
            leaky.mode = mode
            r = c.post(f"{base}/experiments", json={**body, "client_request_id": f"verif-err-{k}"})
            rec.append((f"experiment_{mode}", _expect(r, status, mode)))
        leaky.mode = None
        failed_case = _expect(c.get(base), 200, "get")
        rec.append(("failed_case", failed_case))
        assert failed_case["status"] == "failed" and failed_case["budget"]["used"] == 0
        assert [o["status"] for o in failed_case["observations"][1:]] == ["failed"] * 3
        r = c.post(
            f"{base}/experiments",
            json={"instrument": "output_voltage_sweep", "client_request_id": "verif-err-after"},
        )
        rec.append(("experiment_on_failed", _expect(r, 409, "on failed")))
        _expect(c.post(f"{base}/close"), 200, "close")

        # Repair verification failures.
        case2 = _expect(c.post("/api/cases", json={"template_id": "rc_lowpass"}), 201, "create2")
        rec.append(("create2", case2))
        base2 = f"/api/cases/{case2['id']}"
        # A no_fault conclusion (the other conclusions are covered by the agent-mode and
        # tool-executor tests) citing the symptom; a repair is allowed after any diagnosis.
        no_fault = {
            "conclusion": "no_fault",
            "explanation": "x",
            "citations": [
                {
                    "observation_id": case2["observations"][0]["id"],
                    "quantity": "tone_gain_db",
                    "frequency_hz": 4000.0,
                }
            ],
        }
        rec.append(("dx_no_fault", _expect(c.post(f"{base2}/diagnosis", json=no_fault), 200, "dx")))
        rec.append(
            (
                "proposal",
                _expect(c.post(f"{base2}/repair-proposal", json={"component_id": "R1"}), 200, "p"),
            )
        )
        leaky.mode = "garbage"
        rec.append(("repair_garbage", _expect(c.post(f"{base2}/repair"), 500, "garbage")))
        leaky.mode = "unavailable"
        rec.append(("repair_unavailable", _expect(c.post(f"{base2}/repair"), 503, "unavail")))
        leaky.mode = None
        rec.append(("case2", _expect(c.get(base2), 200, "get2")))
        _expect(c.post(f"{base2}/close"), 200, "close2")

    secrets = {f: secrets_for(seeds[f], f, nominal) for f in FAULTS}
    for fault, stream in streams.items():
        for label, body in stream:
            assert not _forbidden_text(body), (fault, label, _forbidden_text(body))
            for path, _value, detail in _scan(body, secrets[fault], set()):
                if label.startswith(("create", "failed_case", "case2", "dx_", "proposal")):
                    continue  # measured payloads: covered by the invariance check below
                pytest.fail(f"{fault} {label}: {detail} at {path}")
    failures = _invariant_filter(streams, secrets, {f: set() for f in FAULTS})
    assert not failures, "\n".join(failures[:20])
    # Error bodies are byte-identical in every hidden condition; other payloads structurally.
    ref = streams[FAULTS[0]]
    for fault in FAULTS[1:]:
        for (label, a), (_, b) in zip(ref, streams[fault], strict=True):
            if "error" in a:
                assert a == b, (fault, label, a, b)
            else:
                assert _vskeleton(a) == _vskeleton(b), (fault, label)
    text = "\n".join(
        r.getMessage() + ("\n" + r.exc_text if r.exc_text else "") for r in caplog.records
    )
    assert LEAK_MARKER not in text
    assert not _forbidden_text(text), _forbidden_text(text)
    for fault in FAULTS:
        assert not raw_text_leaks(text, secrets[fault]), fault


# --------------------------------------------------------------------------------------
# 4. Access control, frozen revisions, simulation surface, creation hardening
# --------------------------------------------------------------------------------------


def test_foreign_unknown_and_malformed_ids_are_indistinguishable(
    lab_factory: Callable[..., Lab],
) -> None:
    lab = lab_factory(AnalyticTestSimulator())
    c = lab.client
    owner, intruder = _session("O", 0), _session("I", 0)
    nominal = (1000.0, 1e-07)
    case = lab.harness_case(owner, seed_for("c1_increased", nominal))
    cid = case["id"]
    _expect(c.post(f"/api/cases/{cid}/mode", json={"mode": "agent"}), 200, "mode")
    lab.providers.provider = ScriptedProvider([_call("get_case", {}), completion("Done.")])
    run = _expect(c.post(f"/api/cases/{cid}/agent-runs", json={"kind": "investigate"}), 201, "r")
    _wait_run(c, run["id"])
    clip = _clip_urls(case)[0].rsplit("/", 1)[-1]
    rev = case["intended_revision"]["id"]

    lab.use(intruder)
    lab.providers.provider = ScriptedProvider([completion("must not run")] * 8)
    malformed = {
        "case": ["x", "case_" + "A" * 200, "CASE_ABCDEFGHIJKLMNOPQR", "case_bad id"],
        "run": ["x", "run_" + "A" * 200, "run_bad id"],
        "art": ["x", "art_abc", "art_" + "!" * 12, "art_" + "A" * 200],
        "rev": ["x", "rev_" + "A" * 200],
    }
    unknown = {
        "case": "case_unknownXXXXXXXXXX",
        "run": "run_unknownXXXXXXXXXXX",
        "art": "art_unknownXXXXXXXXXXX",
        "rev": "rev_unknownXXXXXXXXXXX",
    }
    owned = {"case": cid, "run": run["id"], "art": clip, "rev": rev}
    endpoints: list[tuple[str, str, str, dict[str, Any] | None]] = [
        ("case", "GET", "/api/cases/{}", None),
        ("case", "POST", "/api/cases/{}/mode", {"mode": "guide"}),
        (
            "case",
            "POST",
            "/api/cases/{}/experiments",
            {"instrument": "output_voltage_sweep", "client_request_id": "intruder-key-1"},
        ),
        (
            "case",
            "POST",
            "/api/cases/{}/diagnosis",
            {"conclusion": "insufficient_evidence", "explanation": "x"},
        ),
        ("case", "POST", "/api/cases/{}/repair-proposal", {"component_id": "R1"}),
        ("case", "POST", "/api/cases/{}/repair", None),
        ("case", "POST", "/api/cases/{}/close", None),
        ("case", "POST", "/api/cases/{}/agent-runs", {"kind": "hint"}),
        ("run", "GET", "/api/agent-runs/{}", None),
        ("run", "GET", "/api/agent-runs/{}/events/log", None),
        ("run", "GET", "/api/agent-runs/{}/events", None),
        ("run", "POST", "/api/agent-runs/{}/cancel", None),
        ("art", "GET", "/api/artifacts/{}", None),
        ("rev", "GET", "/api/revisions/{}", None),
    ]
    calls_before = len(lab.sim.calls)
    for kind, method, template, body in endpoints:
        responses = []
        for ident in [owned[kind], unknown[kind], *malformed[kind]]:
            url = template.format(ident)
            r = c.request(method, url, json=body) if body is not None else c.request(method, url)
            responses.append((ident, r.status_code, r.text))
        statuses = {s for _, s, _ in responses}
        bodies = {t for _, _, t in responses}
        assert statuses == {404} and len(bodies) == 1, (template, responses)
    assert len(lab.sim.calls) == calls_before, "an intruder request reached the simulator"
    assert lab.providers.provider.requests == [], "an intruder request reached the model"
    # The owner's case is untouched by the intruder.
    lab.use(owner)
    after = _expect(c.get(f"/api/cases/{cid}"), 200, "owner get")
    assert after["status"] == "observing" and after["budget"]["used"] == 0
    assert c.get(f"/api/artifacts/{clip}").status_code == 200


def test_explore_never_touches_case_designs_or_hidden_circuits(
    lab_factory: Callable[..., Lab],
) -> None:
    lab = lab_factory(AnalyticTestSimulator())
    c = lab.client
    session = _session("X", 0)
    nominal = (2200.0, 6.8e-08)
    case = lab.harness_case(session, seed_for("r1_increased", nominal))
    cid, frozen = case["id"], case["intended_revision"]["id"]
    hidden = {"R1": nominal[0] * 2.0, "C1": nominal[1]}
    hidden_runs = lambda: sum(1 for _, v in lab.sim.calls if v == hidden)  # noqa: E731
    assert hidden_runs() == 1, "the symptom ran the hidden circuit once"

    # Frozen case revision: no explore path measures or edits it, and no simulation happens.
    n = len(lab.sim.calls)
    r = c.post("/api/experiments", json={"revision_id": frozen})
    assert (r.status_code, r.json()["error"]["code"]) == (409, "stale_revision"), r.text
    r = c.post(
        "/api/revisions",
        json={"parent_id": frozen, "values": [{"component_id": "R1", "value": "1k"}]},
    )
    assert (r.status_code, r.json()["error"]["code"]) == (409, "stale_revision"), r.text
    r = c.put("/api/workbench/current-revision", json={"revision_id": frozen})
    assert (r.status_code, r.json()["error"]["code"]) == (409, "stale_revision"), r.text
    lab.providers.provider = ScriptedProvider([completion("must not run")])
    r = c.post("/api/explore/agent-runs", json={"revision_id": frozen, "message": "simulate it"})
    assert r.status_code == 404, r.text
    assert lab.providers.provider.requests == []
    assert len(lab.sim.calls) == n

    # Warm-cache attempt: explore the guessed hidden circuit, then run a case instrument.
    rev = _expect(
        c.post(
            "/api/revisions",
            json={
                "template_id": "rc_lowpass",
                "values": [
                    {"component_id": "R1", "value": hidden["R1"]},
                    {"component_id": "C1", "value": hidden["C1"]},
                ],
            },
        ),
        201,
        "rev",
    )
    _expect(c.post("/api/experiments", json={"revision_id": rev["id"]}), 201, "explore")
    explore_hidden = hidden_runs()
    _expect(c.post(f"/api/cases/{cid}/mode", json={"mode": "guide"}), 200, "mode")
    n = len(lab.sim.calls)
    _expect(
        c.post(
            f"/api/cases/{cid}/experiments",
            json={"instrument": "output_voltage_sweep", "client_request_id": "verif-warm-1"},
        ),
        201,
        "exp",
    )
    case_calls = lab.sim.calls[n:]
    assert len(case_calls) == 2, "hidden sweep + intended reference, regardless of warm-up"
    assert sum(1 for _, v in case_calls if v == hidden) == 1, "the hidden circuit really ran"
    assert hidden_runs() == explore_hidden + 1

    # Read-only endpoints never simulate.
    n = len(lab.sim.calls)
    for url in (f"/api/cases/{cid}", "/api/workbench", f"/api/revisions/{frozen}"):
        _expect(c.get(url), 200, url)
    assert len(lab.sim.calls) == n

    # Still frozen after the case closes (permanently frozen, V-20).
    _expect(c.post(f"/api/cases/{cid}/close"), 200, "close")
    r = c.post("/api/experiments", json={"revision_id": frozen})
    assert (r.status_code, r.json()["error"]["code"]) == (409, "stale_revision"), r.text


def test_case_creation_accepts_no_hidden_input(lab_factory: Callable[..., Lab]) -> None:
    lab = lab_factory(AnalyticTestSimulator())
    c = lab.client
    lab.use(_session("C", 0))
    for body in (
        {"seed": "00" * 16},
        {"fault": "r1_increased"},
        {"hypothesis_id": "no_fault"},
        {"nominal": {"R1": 1000.0, "C1": 1e-7}},
        {"values": [{"component_id": "R1", "value": "1k"}]},
        {"template_id": "rc_bandpass"},  # rc_highpass is now a valid public family
        {"template_id": "rc_lowpass", "multiplier": 2.0},
    ):
        r = c.post("/api/cases", json=body)
        assert r.status_code == 422, (body, r.status_code)
        assert r.json()["error"]["code"] == "validation_error"
    assert lab.sim.calls == [], "a rejected request reached the simulator"
    chosen = "ab" * 16
    case = _expect(c.post(f"/api/cases?seed={chosen}&fault=no_fault", json={}), 201, "create")
    revealed = _expect(c.post(f"/api/cases/{case['id']}/close"), 200, "close")["reveal"]
    assert revealed["seed"] != chosen and re.fullmatch(r"[0-9a-f]{32}", revealed["seed"])
    spec = c.get("/openapi.json").json()
    assert "harness" not in json.dumps(spec).lower()
    assert set(spec["components"]["schemas"]["CaseCreateRequest"]["properties"]) == {"template_id"}


# --------------------------------------------------------------------------------------
# 5. Numerical fingerprinting of voltage-equivalent hidden circuits (real ngspice)
# --------------------------------------------------------------------------------------


@pytest.mark.ngspice
async def test_voltage_only_payloads_cannot_distinguish_r_from_c_drift(
    settings: Settings,
) -> None:
    """Requirement (CLAUDE.md identifiability rule, contracts "Hidden/public boundary"): with
    voltage-only observations, r1_decreased and c1_decreased are indistinguishable, so their
    published voltage measurements must be identical. Design 3300 ohm / 470 nF is drawable."""
    from resonance.simulation import create_simulator

    nominal = (3300.0, 4.7e-07)
    assert nominal in faults.nominal_designs()
    simulator = create_simulator(settings)
    simulator.info()
    rig = build_rig(settings, simulator)
    try:
        symptoms = {}
        for i, fault in enumerate(("r1_decreased", "c1_decreased")):
            public = await rig.service.harness_create_case(
                _session("N", i), seed_for(fault, nominal)
            )
            assert public.evidence.remaining == ["r1_decreased", "c1_decreased"]
            obs = public.observations[0].transient
            assert obs is not None
            symptoms[fault] = [(t.amplitude, t.gain_db, t.phase_deg) for t in obs.tones] + [
                a.peak_fs for a in (obs.audio.clips if obs.audio else []) if a.role == "observed"
            ]
        assert symptoms["r1_decreased"] == symptoms["c1_decreased"], symptoms
    finally:
        rig.service.close()
        rig.store.close()


def test_scanner_positive_control_finds_planted_leaks() -> None:
    """The scanners used above must catch the leaks they are meant to catch."""
    nominal = {"R1": 3300.0, "C1": 4.7e-07}
    seed = seed_for("c1_decreased", (3300.0, 4.7e-07))
    secret = secrets_for(seed, "c1_decreased", nominal)
    planted = {
        "summary": "C1 measured 235 nF",
        "note": "value 2.35e-07",
        "x": 2.35e-07,
        "error": {"message": f"seed {seed}"},
        "label": SHORT_LABELS["c1_decreased"],
        "hypothesis": HYPOTHESES_BY_ID["c1_decreased"].label,
    }
    paths = {p for p, _, _ in _scan(planted, secret, set())}
    assert {"summary", "note", "x", "error.message", "label", "hypothesis"} <= paths, paths
    assert _forbidden_text({"m": "Circuit: * resonance trusted netlist .tran 1e-05"})
    assert _forbidden_text({"m": "D:\\\\GitHub\\\\x"})
    assert _check_allow({"case_id": "c", "reveal": {}}, "case") == [".reveal"]


# --------------------------------------------------------------------------------------
# 6. Files: static routes never serve the data directory; ngspice job dirs never remain
# --------------------------------------------------------------------------------------


def test_static_routes_never_serve_hidden_state_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dist = tmp_path / "web"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>app</title>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    settings = _settings(tmp_path).model_copy(update={"web_dist_dir": dist})
    monkeypatch.setattr(app_module, "create_simulator", lambda _s: AnalyticTestSimulator())
    with TestClient(app_module.create_app(settings)) as client:
        assert settings.evaluator_db_path.is_file()
        assert client.get("/assets/app.js").status_code == 200  # the mount works
        probes = [
            "/%2e%2e/data/evaluator.sqlite3",
            "/%2e%2e%2fdata%2fevaluator.sqlite3",
            "/web/%2e%2e/data/evaluator.sqlite3",
            "/assets/%2e%2e/%2e%2e/data/evaluator.sqlite3",
            "/assets/%2e%2e%2f%2e%2e%2fdata%2fevaluator.sqlite3",
            "/data/evaluator.sqlite3",
            "/evaluator.sqlite3",
            "/%2e%2e/data/resonance.sqlite3",
            "/%2e%2e/data/artifacts",
        ]
        for url in probes:
            for method in ("GET", "HEAD"):
                r = client.request(method, url)
                assert not r.content.startswith(b"SQLite format 3"), (method, url)
                assert r.status_code in (200, 404, 405), (method, url, r.status_code)
                if r.status_code == 200:  # only the SPA shell may come back
                    assert method == "HEAD" or b"<!doctype html>" in r.content, url


@pytest.mark.ngspice
async def test_hidden_runs_leave_no_job_files_even_when_cancelled(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """Job directories hold netlists with the hidden values while ngspice runs; none may remain
    after success or cancellation, and the data directory holds only the declared stores. The
    real simulation layer's logs (DEBUG) carry no values, netlists, or paths."""
    import asyncio

    caplog.set_level(logging.DEBUG)
    nominal = (1000.0, 1e-07)
    seed = seed_for("c1_decreased", nominal)

    from resonance.contracts.investigation import CaseExperimentRequest
    from resonance.simulation import create_simulator
    from resonance.simulation.ngspice import NETLIST_NAME

    simulator = create_simulator(settings)
    simulator.info()
    rig = build_rig(settings, simulator)
    session = _session("J", 0)
    try:
        case = await rig.service.harness_create_case(session, seed)
        await rig.service.set_mode(session, case.id, "guide")
        await rig.service.run_experiment(
            session,
            case.id,
            CaseExperimentRequest(instrument="source_current_sweep", client_request_id="verif-j1"),
            requested_by="user",
        )
        token = CancelToken()
        # Cancel deterministically while ngspice runs (a fixed delay races fast hosts): the job
        # directory with the hidden netlist exists at this point and must still be removed.
        runner = simulator.runner  # type: ignore[attr-defined]
        supervise = runner._supervise
        seen_jobs: list[bool] = []

        def supervise_then_cancel(proc: Any, job: Path, cancel: Any) -> None:
            seen_jobs.append((job / NETLIST_NAME).is_file())
            token.cancel()
            supervise(proc, job, cancel)

        runner._supervise = supervise_then_cancel
        from resonance.api.errors import ApiException

        with pytest.raises(ApiException) as info:
            await rig.service.run_experiment(
                session,
                case.id,
                CaseExperimentRequest(
                    instrument="transient_capture",
                    probe="source_current",
                    client_request_id="verif-j2",
                ),
                requested_by="user",
                cancel=token,
            )
        runner._supervise = supervise
        assert seen_jobs and all(seen_jobs), "the cancelled run reached ngspice with its netlist"
        assert info.value.error.code.value == "cancelled"
        public = rig.service.get_case(session, case.id)
        assert public.budget.used == 1 and public.observations[-1].status == "cancelled"
        # A cancelled worker thread finishes its kill + cleanup after the coroutine returned
        # (asyncio.to_thread is not awaited on cancellation), so allow a bounded delay.
        deadline = time.monotonic() + 10.0
        while list(settings.jobs_dir.glob("job_*")) and time.monotonic() < deadline:  # noqa: ASYNC110 - polling the filesystem
            await asyncio.sleep(0.05)
        assert not list(settings.jobs_dir.glob("job_*")), "an ngspice job directory remained"
        names = {p.name for p in settings.resonance_data_dir.iterdir()}
        allowed = {"resonance.sqlite3", "evaluator.sqlite3", "artifacts", "jobs"}
        allowed |= {
            f"{n}{s}" for n in ("resonance.sqlite3", "evaluator.sqlite3") for s in ("-wal", "-shm")
        }
        assert names <= allowed, names
        for path in settings.artifacts_dir.iterdir():
            assert re.fullmatch(r"art_[A-Za-z0-9_-]{16}\.bin", path.name), path.name
            assert path.read_bytes()[:4] == b"RIFF"
        text = "\n".join(
            r.getMessage() + ("\n" + r.exc_text if r.exc_text else "") for r in caplog.records
        )
        assert "ngspice" in text  # the simulation layer really logged at DEBUG
        assert not _forbidden_text(text), _forbidden_text(text)
        secret = secrets_for(seed, "c1_decreased", {"R1": nominal[0], "C1": nominal[1]})
        assert not raw_text_leaks(text, secret) and seed not in text
    finally:
        rig.service.close()
        rig.store.close()
