"""Independent verification, milestone 2 backend (lens M2-backend).

Owner: independent verification engineer. Report: ``docs/verification/review-M2-backend.md``.

Scope: the high-pass family, the open-capacitor condition (``c1_disconnected``), measurement
floors, the six-condition generator, Learn mode, design suggestions, the evaluation harness and
high-pass audio. Every expected physics number comes from first-order RC formulas written in
THIS file (``resonance.simulation.analytic`` is deliberately not imported). Tests that expose a
confirmed defect are ``xfail(strict=True)`` with the finding ID; remove the marker when fixed.

Fast paths use the builders' analytical TEST DOUBLE (``AnalyticTestSimulator``) where the check
is about data flow (payload shape, isolation, atomicity); everything that is a physics or audio
claim runs on real ngspice (``@pytest.mark.ngspice``). No AI is involved (no NEBIUS_API_KEY).
This file deliberately contains no literal of a held-out C1 value, so the harness disjointness
scan (``tests/challenges/test_evals_harness.py``) cannot mistake it for a held-out fixture.

Run: ``RESONANCE_REQUIRE_NGSPICE=1 uv run pytest tests/verification/test_v_m2_backend.py -rxX``
"""

from __future__ import annotations

import ast
import asyncio
import cmath
import hashlib
import io
import json
import math
import random
import re
import secrets
import sys
import wave
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

_TESTS = Path(__file__).resolve().parents[1]
_REPO = _TESTS.parents[2]
for _p in (_TESTS / "challenges", _REPO / "evals"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import casesets  # noqa: E402
import harness  # noqa: E402
import report  # noqa: E402
import run_nemotron  # noqa: E402
from challenges_testkit import (  # noqa: E402
    FREQUENCY_KEY,
    MEASURED_PATH,
    AnalyticTestSimulator,
    build_rig,
    iter_leaves,
    seed_for,
    value_spellings,
)

from resonance.agent import views  # noqa: E402
from resonance.api.errors import ApiException  # noqa: E402
from resonance.app import create_app  # noqa: E402
from resonance.challenges import families, hypotheses, summaries  # noqa: E402
from resonance.challenges.evaluator import Evaluator, EvaluatorStore, faults  # noqa: E402
from resonance.circuits.revisions import build_revision  # noqa: E402
from resonance.circuits.templates import default_source  # noqa: E402
from resonance.config import Settings  # noqa: E402
from resonance.contracts import policy  # noqa: E402
from resonance.contracts.circuits import TemplateId  # noqa: E402
from resonance.contracts.experiments import ExperimentRecord, Goal, GoalConstraint  # noqa: E402
from resonance.contracts.internal import (  # noqa: E402
    AcSweep,
    CircuitSpec,
    SimulationFailed,
    TransientRequest,
)
from resonance.contracts.investigation import (  # noqa: E402
    CaseExperimentRequest,
    HypothesisId,
    PublicCase,
)
from resonance.contracts.learn import LearnAttemptRequest  # noqa: E402
from resonance.explore import learn as learn_mod  # noqa: E402
from resonance.explore.goals import default_goal  # noqa: E402
from resonance.explore.suggest import suggest_design  # noqa: E402
from resonance.simulation import create_simulator  # noqa: E402
from resonance.store.artifacts import ArtifactStore  # noqa: E402
from resonance.store.db import DocumentStore  # noqa: E402

FAMILIES: tuple[TemplateId, ...] = ("rc_lowpass", "rc_highpass")
CONDITIONS: tuple[HypothesisId, ...] = (
    "no_fault",
    "r1_increased",
    "r1_decreased",
    "c1_increased",
    "c1_decreased",
    "c1_disconnected",
)
# Development design (drawable in both families; also used by the builders' dev set).
DESIGN = (1000.0, 1e-7)
TWO_PI = 2.0 * math.pi
GAIN_FLOOR_DB = -120.0  # declared in docs/contracts.md "No measurable signal"
CURRENT_FLOOR_MA = 1e-6
DB_FLOOR = -200.0


def _settings(root: Path, **extra: Any) -> Settings:
    base: dict[str, Any] = {
        "resonance_data_dir": root / "data",
        "web_dist_dir": root / "no-web",
        "nebius_api_key": None,
        "nebius_model": None,
        "resonance_dev_agent_fixture": False,
    }
    base.update(extra)
    return Settings(_env_file=None, **base)


# ======================================================================================
# Independent first-order RC model (ideal source, ideal R and C, no load)
# ======================================================================================


def zc(f: float, c: float) -> complex:
    return 1.0 / (1j * TWO_PI * f * c)


def h_model(family: str, f: float, r: float, c: float, open_c: bool = False) -> complex:
    """V(out)/V(in). Open C1: low-pass output follows the input (no current through R1);
    high-pass output is held at ground by R1."""
    if open_c:
        return 1.0 + 0j if family == "rc_lowpass" else 0j
    if family == "rc_lowpass":
        return zc(f, c) / (r + zc(f, c))
    return r / (r + zc(f, c))


def i_model(f: float, r: float, c: float, open_c: bool = False) -> complex:
    """Current delivered by a 1 V source into the series R + C path (amperes)."""
    return 0j if open_c else 1.0 / (r + zc(f, c))


def db(x: complex | float) -> float:
    return 20.0 * math.log10(abs(x))


def deg(z: complex) -> float:
    return math.degrees(cmath.phase(z))


def angle_diff(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def fc_of(r: float, c: float) -> float:
    return 1.0 / (TWO_PI * r * c)


def attenuation(family: str, f: float, fc: float) -> float:
    ratio = f / fc if family == "rc_lowpass" else fc / f
    return 10.0 * math.log10(1.0 + ratio * ratio)


VARIANTS = {
    "no_fault": (1.0, 1.0),
    "r1_increased": (2.0, 1.0),
    "r1_decreased": (0.5, 1.0),
    "c1_increased": (1.0, 2.0),
    "c1_decreased": (1.0, 0.5),
}
CASE_GRID = [20.0 * 10.0 ** (k / 10.0) for k in range(31)]


def random_designs(n: int, seed: int) -> list[tuple[float, float]]:
    rng = random.Random(seed)
    out: list[tuple[float, float]] = []
    while len(out) < n:
        r = 10.0 ** rng.uniform(2.0, 5.0)
        c = 10.0 ** rng.uniform(-9.0, -5.0)
        if 20.0 <= fc_of(r, c) <= 20_000.0:
            out.append((r, c))
    return out


# ======================================================================================
# 1. Physics on real ngspice: high-pass across the bounds, open capacitor in both families
# ======================================================================================

HP_CORNERS = [(100.0, 1e-5), (1e5, 1e-9), (1e5, 79.5e-9), (100.0, 79.6e-9)]


@pytest.mark.ngspice
async def test_highpass_ac_gain_phase_and_current_match_independent_formulas(
    tmp_path: Path,
) -> None:
    """|H|, |I| within 1 % (tiny absolute floors) and phases within 1 deg (wrapped) for random
    high-pass designs and the bound corners, each with its 2x/0.5x R and C variants, on a
    30-point log grid around each variant's own cutoff."""
    sim = create_simulator(_settings(tmp_path))
    sim.info()
    source = default_source("rc_highpass")
    jobs: list[tuple[float, float]] = []
    for r0, c0 in random_designs(4, seed=20261023) + HP_CORNERS:
        for mr, mc in VARIANTS.values():
            jobs.append((r0 * mr, c0 * mc))
    # Two at a time: the runner's slot queue times out if dozens of jobs wait at once on a
    # shared host (that is the declared busy behaviour, not a physics failure).
    gate = asyncio.Semaphore(2)

    async def sweep(r: float, c: float) -> Any:
        fc = fc_of(r, c)
        spec = CircuitSpec("rc_highpass", {"R1": r, "C1": c}, source)
        async with gate:
            return await sim.ac(spec, AcSweep(fc / 30.0, fc * 30.0, 10))

    results = await asyncio.gather(*(sweep(r, c) for r, c in jobs))
    worst = [0.0, 0.0, 0.0, 0.0]
    for (r, c), data in zip(jobs, results, strict=True):
        assert data.v_in is not None and data.v_out is not None and data.i_source is not None
        assert data.frequency_hz.size == 30
        for k, f in enumerate(data.frequency_hz):
            h = complex(data.v_out[k] / data.v_in[k])
            i = complex(data.i_source[k])
            he, ie = h_model("rc_highpass", f, r, c), i_model(f, r, c)
            assert abs(abs(h) - abs(he)) <= max(0.01 * abs(he), 1e-9), (r, c, f)
            assert abs(abs(i) - abs(ie)) <= max(0.01 * abs(ie), 1e-12), (r, c, f)
            assert angle_diff(deg(h), deg(he)) <= 1.0, (r, c, f)
            assert angle_diff(deg(i), deg(ie)) <= 1.0, (r, c, f)
            # The high-pass output and the delivered current both lead the source by 0..90 deg.
            assert 0.0 < deg(h) < 90.0 and 0.0 < deg(i) < 90.0
            worst = [
                max(worst[0], abs(abs(h) / abs(he) - 1)),
                max(worst[1], angle_diff(deg(h), deg(he))),
                max(worst[2], abs(abs(i) / abs(ie) - 1)),
                max(worst[3], angle_diff(deg(i), deg(ie))),
            ]
    print(f"\n[M2 physics] {len(jobs)} high-pass AC runs x 30 points; worst rel/deg: {worst}")


@pytest.mark.ngspice
@pytest.mark.parametrize("family", FAMILIES)
async def test_open_capacitor_is_an_exact_open_circuit_in_ac_and_transient(
    tmp_path: Path, family: TemplateId
) -> None:
    sim = create_simulator(_settings(tmp_path))
    sim.info()
    spec = CircuitSpec(
        family, {"R1": DESIGN[0], "C1": DESIGN[1]}, default_source(family), frozenset({"C1"})
    )
    ac = await sim.ac(spec, AcSweep(20.0, 20_000.0, 10))
    assert ac.v_in is not None and ac.v_out is not None and ac.i_source is not None
    assert ac.frequency_hz.size == 31
    h = ac.v_out / ac.v_in
    target = 1.0 if family == "rc_lowpass" else 0.0
    assert float(np.max(np.abs(h - target))) <= 1e-12
    assert float(np.max(np.abs(ac.i_source))) <= 1e-15
    tr = await sim.transient(spec, TransientRequest(0.05, 1e-5, ("v(in)", "v(out)", "i(V1)")))
    assert tr.v_in is not None and tr.v_out is not None and tr.i_source is not None
    assert float(np.max(np.abs(tr.v_in))) > 0.5  # the source really ran
    if family == "rc_lowpass":
        assert float(np.max(np.abs(tr.v_out - tr.v_in))) <= 1e-9
    else:
        assert float(np.max(np.abs(tr.v_out))) <= 1e-12
    assert float(np.max(np.abs(tr.i_source))) <= 1e-12


# ======================================================================================
# 2. Hidden-state isolation, 2 families x 6 conditions (HTTP, analytical test double)
# ======================================================================================

ID_VALUE = re.compile(r"^(rev|exp|art|cmp|goal|case|obs|dx|rep|prop|run|learn)_[A-Za-z0-9_-]{8,}$")
TIME_KEYS = {"created_at", "updated_at", "revealed_at", "started_at", "finished_at"}
# Hidden-run measurements and values derived ONLY from them (recomputed separately below).
DERIVED = re.compile(
    r"(sweep\.(magnitude|phase_deg)\[\d+\]$"
    r"|transient\.tones\[\d+\]\.(amplitude|gain_db|phase_deg)$"
    r"|transient\.waveform\.observed\[\d+\]$"
    r"|audio\.clips\[\d+\]\.peak_fs$"
    r"|(^|\.)summary$"
    r"|(^|\.)evidence$"
    r"|evidence_check$"
    r"|repair\.(points\[\d+\]\.(repaired_gain_db|repaired_phase_deg|within_tolerance)"
    r"|functional_recovery|max_gain_deviation_db|max_phase_deviation_deg|worst_frequency_hz"
    r"|tones\[\d+\]\.(amplitude|gain_db|phase_deg)))"
)
NETLIST_TOKENS = (".tran", ".ac ", ".control", "R1 in out", "C1 out 0", "SIN(", "result.raw")
NGSPICE_TEXT = ("Circuit:", "Note:", "Warning:", "Error:", "Doing analysis")
PATH_TOKENS = (".data", "jobs", "evaluator.sqlite3", ":\\", "Temp")
FORBIDDEN_AGENT_KEYS = {
    "reveal",
    "distinguishing_instruments",
    "waveform",
    "audio",
    "artifact_id",
    "url",
    "settings",
    "assessments",
    "seed",
}


def mask(node: Any, path: str = "") -> Any:
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            sub = f"{path}.{k}" if path else k
            if k in TIME_KEYS:
                out[k] = "<time>"
            elif k == "url":
                out[k] = "<url>"
            elif DERIVED.search(sub):
                out[k] = "<derived>"
            else:
                out[k] = mask(v, sub)
        return out
    if isinstance(node, list):
        return [
            "<derived>" if DERIVED.search(f"{path}[{i}]") else mask(v, f"{path}[{i}]")
            for i, v in enumerate(node)
        ]
    if isinstance(node, str) and ID_VALUE.match(node):
        return "<id>"
    return node


@dataclass
class Flow:
    family: str
    condition: str
    seed: str
    payloads: list[tuple[str, int, str]] = field(default_factory=list)  # (label, status, text)
    wavs: list[bytes] = field(default_factory=list)
    cases: list[PublicCase] = field(default_factory=list)  # pre-close snapshots
    agent: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    closed: dict[str, Any] | None = None


def _record(flow: Flow, label: str, resp: Any) -> Any:
    flow.payloads.append((label, resp.status_code, resp.text))
    body = resp.json()
    case = body.get("case") if isinstance(body, dict) and "observation" in body else body
    if isinstance(case, dict) and str(case.get("id", "")).startswith("case_"):
        flow.cases.append(PublicCase.model_validate(case))
    return body


def _collect_wavs(client: TestClient, flow: Flow, body: Any) -> None:
    for path, leaf in iter_leaves(body):
        if path.endswith(".url") and isinstance(leaf, str) and leaf.startswith("/api/artifacts"):
            resp = client.get(leaf)
            assert resp.status_code == 200 and resp.headers["content-type"].startswith("audio/wav")
            flow.wavs.append(resp.content)


def run_http_flow(client: TestClient, mp: pytest.MonkeyPatch, family: str, condition: str) -> Flow:
    seed = seed_for(condition, DESIGN, family)
    flow = Flow(family, condition, seed)
    mp.setattr(faults, "new_seed", lambda: seed)
    client.cookies.clear()  # a fresh anonymous session per case
    assert client.get("/api/health").status_code == 200
    created = _record(flow, "create", client.post("/api/cases", json={"template_id": family}))
    assert flow.payloads[-1][1] == 201, flow.payloads[-1][2]
    cid = created["id"]
    _collect_wavs(client, flow, created)
    _record(flow, "get", client.get(f"/api/cases/{cid}"))
    _record(flow, "mode", client.post(f"/api/cases/{cid}/mode", json={"mode": "guide"}))
    plan = [
        ("output_voltage_sweep", None),
        ("source_current_sweep", None),
        ("transient_capture", "source_current"),
    ]
    for i, (instrument, probe) in enumerate(plan):
        body: dict[str, Any] = {"instrument": instrument, "client_request_id": f"verif-m2-{i:04d}"}
        if probe:
            body["probe"] = probe
        resp = client.post(f"/api/cases/{cid}/experiments", json=body)
        assert resp.status_code == 201, resp.text
        _record(flow, f"exp{i}", resp)
    over = client.post(
        f"/api/cases/{cid}/experiments",
        json={"instrument": "output_voltage_sweep", "client_request_id": "verif-m2-over"},
    )
    flow.payloads.append(("over_budget", over.status_code, over.text))
    bad = client.post(
        f"/api/cases/{cid}/diagnosis",
        json={
            "conclusion": "no_fault",
            "citations": [{"observation_id": "obs_doesnotexist0000", "quantity": "output_gain_db"}],
            "explanation": "verification",
        },
    )
    flow.payloads.append(("bad_diagnosis", bad.status_code, bad.text))
    dx = client.post(
        f"/api/cases/{cid}/diagnosis",
        json={
            "conclusion": "insufficient_evidence",
            "candidate_hypothesis_ids": [],
            "explanation": "verification: identical submission for every condition",
        },
    )
    assert dx.status_code == 200, dx.text
    _record(flow, "diagnosis", dx)
    rp = client.post(
        f"/api/cases/{cid}/repair-proposal", json={"component_id": "C1", "rationale": "verify"}
    )
    assert rp.status_code == 200, rp.text
    _record(flow, "repair_proposal", rp)
    rv = client.post(f"/api/cases/{cid}/repair")
    assert rv.status_code == 200, rv.text
    repaired = _record(flow, "repair", rv)
    _collect_wavs(client, flow, repaired["repair"])
    flow.payloads.append(("workbench", 200, client.get("/api/workbench").text))
    rev_id = created["intended_revision"]["id"]
    flow.payloads.append(("revision", 200, client.get(f"/api/revisions/{rev_id}").text))
    # Agent-visible views of the same pre-close states (the explicit allow-list builders).
    actions = ("run_experiment", "submit_diagnosis", "propose_repair")
    for snap in (flow.cases[0], flow.cases[-1]):
        flow.agent.append(("case_view", views.case_view(snap, run_actions=actions)))
        flow.agent.append(("evidence_view", views.evidence_view(snap)))
    for obs in flow.cases[-1].observations:
        flow.agent.append(("observation_view", views.observation_view(obs)))
    flow.closed = client.post(f"/api/cases/{cid}/close").json()
    return flow


@pytest.fixture(scope="module")
def http_flows(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[tuple[TemplateId, HypothesisId], Flow]]:
    import resonance.app as app_module

    root = tmp_path_factory.mktemp("verif_m2_http")
    double = AnalyticTestSimulator()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(app_module, "create_simulator", lambda _s: double)
        with TestClient(app_module.create_app(_settings(root))) as client:
            flows = {(f, c): run_http_flow(client, mp, f, c) for f in FAMILIES for c in CONDITIONS}
    yield flows


def _hidden_markers(flow: Flow) -> list[str]:
    markers = [flow.seed, faults.GENERATOR_VERSION]
    return markers


def _actual_values(flow: Flow) -> list[tuple[float, str]]:
    r0, c0 = DESIGN
    mr, mc = VARIANTS.get(flow.condition, (1.0, 1.0))
    out = []
    if mr != 1.0:
        out.append((r0 * mr, "ohm"))
    if mc != 1.0:
        out.append((c0 * mc, "farad"))
    return out


_SPELLING_CACHE: dict[tuple[tuple[float, str], ...], re.Pattern[str] | None] = {}


def _spelling_pattern(actual: list[tuple[float, str]]) -> re.Pattern[str] | None:
    key = tuple(actual)
    if key not in _SPELLING_CACHE:
        spellings = [s for v, u in actual for s in value_spellings(v, u)]
        _SPELLING_CACHE[key] = (
            re.compile(
                r"(?<![0-9.])(" + "|".join(re.escape(s) for s in spellings) + r")(?![0-9.A-Za-zΩ])"
            )
            if spellings
            else None
        )
    return _SPELLING_CACHE[key]


def _scan_for_hidden(flow: Flow, label: str, text: str) -> None:
    for marker in _hidden_markers(flow) + list(NETLIST_TOKENS) + list(NGSPICE_TEXT):
        assert marker not in text, (flow.family, flow.condition, label, marker)
    for token in PATH_TOKENS:
        assert token not in text, (flow.family, flow.condition, label, token)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return
    actual = _actual_values(flow)
    pattern = _spelling_pattern(actual)
    for path, leaf in iter_leaves(data):
        if path.endswith("seed") or path.endswith("reveal"):
            assert leaf is None, (label, path)
        if isinstance(leaf, str) and pattern is not None:
            found = pattern.search(leaf)
            assert found is None, (flow.condition, label, path, found and found.group())
        for value, _unit in actual:
            if isinstance(leaf, int | float) and not isinstance(leaf, bool):
                if MEASURED_PATH.search(path) or FREQUENCY_KEY.search(path):
                    continue
                if re.search(r"rows\[\d+\]\[0\]$", path):  # agent sweep rows: frequency column
                    continue
                if DERIVED.search(path):
                    continue
                assert not math.isclose(float(leaf), value, rel_tol=1e-9), (label, path, leaf)


def test_pre_close_payloads_and_agent_views_carry_no_hidden_state(
    http_flows: dict[tuple[TemplateId, HypothesisId], Flow],
) -> None:
    for flow in http_flows.values():
        for label, _status, text in flow.payloads:
            _scan_for_hidden(flow, label, text)
        for label, view in flow.agent:
            text = json.dumps(view, ensure_ascii=False)
            _scan_for_hidden(flow, label, text)
            keys = set(_all_keys(view))
            assert not keys & FORBIDDEN_AGENT_KEYS, (label, keys & FORBIDDEN_AGENT_KEYS)
            assert all(len(v) <= 31 for p, v in _lists(view)), label
        # Positive control: after close the reveal names the truth and publishes the seed.
        assert flow.closed is not None and flow.closed["reveal"]["seed"] == flow.seed
        assert flow.closed["reveal"]["fault"]["hypothesis_id"] == flow.condition


def _all_keys(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        for k, v in node.items():
            yield k
            yield from _all_keys(v)
    elif isinstance(node, list):
        for v in node:
            yield from _all_keys(v)


def _lists(node: Any, path: str = "") -> Iterator[tuple[str, list[Any]]]:
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _lists(v, f"{path}.{k}")
    elif isinstance(node, list):
        yield path, node
        for i, v in enumerate(node):
            yield from _lists(v, f"{path}[{i}]")


def test_payload_structure_is_identical_across_all_six_conditions(
    http_flows: dict[tuple[TemplateId, HypothesisId], Flow],
) -> None:
    """Differential test (V-06): with IDs, times, URLs and hidden-run measurements masked,
    every browser payload, error body, WAV header/size and agent view is byte-identical for the
    six conditions of a family, c1_disconnected included. Reference (intended-design) numbers
    are NOT masked, so they must match too."""
    for family in FAMILIES:
        skeletons: dict[str, Any] = {}
        for condition in CONDITIONS:
            flow = http_flows[(family, condition)]
            payloads = [
                (label, status, json.dumps(mask(json.loads(text)), sort_keys=True))
                for label, status, text in flow.payloads
            ]
            agent = [
                (label, json.dumps(_mask_agent(view), sort_keys=True)) for label, view in flow.agent
            ]
            wav = [(len(b), b[:44]) for b in flow.wavs]
            skeletons[condition] = (payloads, agent, wav)
        base = skeletons["no_fault"]
        for condition in CONDITIONS[1:]:
            other = skeletons[condition]
            for (la, sa, ta), (lb, sb, tb) in zip(base[0], other[0], strict=True):
                assert (la, sa) == (lb, sb), (family, condition)
                assert ta == tb, (family, condition, la, _first_diff(ta, tb))
            assert base[1] == other[1], (family, condition, "agent views differ")
            assert base[2] == other[2], (family, condition, "WAV size/header differ")
        assert len(base[2]) == 4  # symptom expected/observed + repair expected/repaired


AGENT_MEASURED_KEYS = {"amplitude", "gain_db", "phase_deg"}


def _mask_agent(node: Any, key: str = "") -> Any:
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k in {"summary", "statement", "hypothesis_status", "remaining"}:
                out[k] = "<derived>"
            elif k == "repair_verification":
                out[k] = {kk: "<derived>" for kk in v}
            elif k == "evidence_check_status":
                out[k] = "<derived>"
            elif k in AGENT_MEASURED_KEYS:
                out[k] = "<m>"
            elif k == "rows":
                out[k] = [[r[0], "<m>", r[2], "<m>", r[4]] for r in v]
            else:
                out[k] = _mask_agent(v, k)
        return out
    if isinstance(node, list):
        return [_mask_agent(v, key) for v in node]
    if isinstance(node, str) and ID_VALUE.match(node):
        return "<id>"
    return node


def _first_diff(a: str, b: str) -> str:
    for i, (x, y) in enumerate(zip(a, b, strict=False)):
        if x != y:
            return f"...{a[max(0, i - 80) : i + 80]} | {b[max(0, i - 80) : i + 80]}"
    return "length differs"


def test_derived_fields_are_pure_functions_of_public_data(
    http_flows: dict[tuple[TemplateId, HypothesisId], Flow],
) -> None:
    """Everything masked above as 'derived' is recomputed here from the PUBLISHED numbers and
    public design data only, so the masked text cannot carry anything else (V-06/V-13)."""
    for flow in http_flows.values():
        for case in flow.cases:
            values: dict[str, float] = {
                c.id: float(c.value_si)
                for c in case.intended_revision.components
                if c.id in ("R1", "C1") and c.value_si
            }
            tones = case.intended_revision.source.tones
            remaining = case.budget.total - case.budget.used
            publish = case.status.value not in ("ready", "observing") or remaining <= 0
            recomputed = hypotheses.assess(
                case.intended_revision.template_id,
                values,
                case.observations,
                publish_distinguishing=publish,
                tones=tones,
            )
            assert recomputed == case.evidence, (flow.condition, case.status)
            for obs in case.observations:
                if obs.sweep is not None:
                    assert obs.summary == summaries.sweep_summary(obs.sweep)
                else:
                    assert obs.transient is not None
                    expect = summaries.transient_summary(
                        obs.transient, symptom=obs.kind == "symptom"
                    )
                    assert obs.summary == expect
            if case.diagnosis is not None:
                state = hypotheses.assess(
                    case.intended_revision.template_id,
                    values,
                    case.observations,
                    publish_distinguishing=False,
                    tones=tones,
                )
                check = hypotheses.evidence_check(case.diagnosis.submission, state)
                assert check == case.diagnosis.evidence_check
            if case.repair is not None:
                rv = case.repair
                assert rv.summary == summaries.repair_summary(
                    rv.proposal.component_id,
                    rv.proposal.display_value,
                    rv.points,
                    bool(rv.functional_recovery),
                    policy.REPAIR_GAIN_TOLERANCE_DB,
                    policy.REPAIR_PHASE_TOLERANCE_DEG,
                )
                # Replacing C1 with its nominal part restores every C1 condition (including the
                # open capacitor) and no R1 drift; no-fault stays restored.
                expected = not flow.condition.startswith("r1_")
                assert rv.functional_recovery is expected, (flow.family, flow.condition)


def test_agent_views_never_quote_a_floor_as_a_level_or_phase(
    http_flows: dict[tuple[TemplateId, HypothesisId], Flow],
) -> None:
    """Below-floor readings (open capacitor) reach the model as markers, never as -200 dB or a
    published 0 deg phase."""
    for flow in http_flows.values():
        for label, view in flow.agent:
            text = json.dumps(view)
            assert "-200" not in text, (flow.family, flow.condition, label)
        if flow.condition != "c1_disconnected":
            continue
        obs_views = [v for label, v in flow.agent if label == "observation_view"]
        current = [v for v in obs_views if v.get("kind") == "source_current_sweep"][0]
        assert all(
            row[1] == views.BELOW_FLOOR and row[3] == views.UNDEFINED_PHASE
            for row in current["sweep"]["rows"]
        )
        tones = [v for v in obs_views if v.get("probe") == "source_current"][0]["transient"][
            "tones"
        ]
        assert all(
            t["amplitude"] == views.BELOW_FLOOR and t["phase_deg"] == views.UNDEFINED_PHASE
            for t in tones
        )
        if flow.family == "rc_highpass":
            symptom = [v for v in obs_views if v.get("kind") == "symptom"][0]
            assert all(t["gain_db"] == views.BELOW_FLOOR for t in symptom["transient"]["tones"])


def test_evidence_check_numbers_are_arithmetically_consistent_at_the_floor(
    http_flows: dict[tuple[TemplateId, HypothesisId], Flow],
) -> None:
    case = http_flows[("rc_highpass", "c1_disconnected")].cases[-1]
    for assessment in case.evidence.assessments:
        for check in assessment.checks:
            if check.unit == "dB":
                assert check.observed is not None and check.predicted is not None
                assert math.isclose(
                    abs(check.observed - check.predicted), check.deviation, abs_tol=1e-6
                ), (assessment.hypothesis_id, check.observed, check.predicted, check.deviation)


# ======================================================================================
# 2b. The same differential and floor checks on REAL ngspice (in-process case service)
# ======================================================================================


# Real-ngspice subset (all six conditions run through the HTTP double above): the control, the
# historically fingerprintable R-/C- pair (finding A-01) and the open capacitor, in both families.
NG_CONDITIONS: tuple[HypothesisId, ...] = (
    "no_fault",
    "r1_decreased",
    "c1_decreased",
    "c1_disconnected",
)


@dataclass
class NgCase:
    family: str
    condition: str
    case: PublicCase
    session: str


@pytest.fixture(scope="module")
def ngspice_cases(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Any, list[NgCase]]]:
    root = tmp_path_factory.mktemp("verif_m2_ng")
    settings = _settings(root)
    sim = create_simulator(settings)
    sim.info()
    rig = build_rig(settings, sim)

    async def one(family: str, condition: str, n: int) -> NgCase:
        session = f"verifm2ng_{n:02d}_" + "x" * 24
        seed = seed_for(condition, DESIGN, family)
        case = await rig.service.harness_create_case(session, seed, family)
        await rig.service.set_mode(session, case.id, "guide")
        plan = [
            ("output_voltage_sweep", None),
            ("source_current_sweep", None),
            ("transient_capture", "source_current"),
        ]
        for i, (instrument, probe) in enumerate(plan):
            resp = await rig.service.run_experiment(
                session,
                case.id,
                CaseExperimentRequest(
                    instrument=instrument, probe=probe, client_request_id=f"verif-ng-{i:04d}"
                ),
                requested_by="user",
            )
            case = resp.case
        return NgCase(family, condition, case, session)

    async def run_all() -> list[NgCase]:
        out = []
        n = 0
        for family in FAMILIES:
            for condition in NG_CONDITIONS:
                out.append(await one(family, condition, n))
                n += 1
        return out

    cases = asyncio.run(run_all())
    yield rig, cases
    rig.service.close()
    rig.store.close()


@pytest.mark.ngspice
def test_ngspice_case_payloads_share_one_shape_and_floors_classify_only_true_zeros(
    ngspice_cases: tuple[Any, list[NgCase]],
) -> None:
    _rig, cases = ngspice_cases
    by_family: dict[str, dict[str, str]] = {f: {} for f in FAMILIES}
    for item in cases:
        case = item.case
        by_family[item.family][item.condition] = json.dumps(
            mask(json.loads(case.model_dump_json())), sort_keys=True
        )
        open_c = item.condition == "c1_disconnected"
        for obs in case.observations:
            if obs.sweep is not None:
                floor = GAIN_FLOOR_DB if obs.sweep.quantity == "output_gain" else CURRENT_FLOOR_MA
                for m, p in zip(obs.sweep.magnitude, obs.sweep.phase_deg, strict=True):
                    below = m <= floor
                    if open_c and (
                        obs.sweep.quantity == "source_current" or item.family == "rc_highpass"
                    ):
                        assert below and p == 0.0, (item.family, obs.kind, m, p)
                        if obs.sweep.quantity == "output_gain":
                            assert m == GAIN_FLOOR_DB
                    else:
                        assert not below, (item.family, item.condition, obs.kind, m)
                assert all(e > floor for e in obs.sweep.expected_magnitude)
            else:
                assert obs.transient is not None
                for t in obs.transient.tones:
                    level = t.gain_db if t.gain_db is not None else t.amplitude
                    floor = GAIN_FLOOR_DB if t.gain_db is not None else CURRENT_FLOOR_MA
                    zero = open_c and (t.gain_db is None or item.family == "rc_highpass")
                    assert (level <= floor) is zero, (item.family, item.condition, t)
                    if zero:
                        assert t.phase_deg == 0.0
        # Views of real-ngspice data never quote a floor either.
        for obs in case.observations:
            assert "-200" not in json.dumps(views.observation_view(obs))
    for family, skel in by_family.items():
        base = skel["no_fault"]
        for condition, text in skel.items():
            assert text == base, (family, condition, _first_diff(base, text))


@pytest.mark.ngspice
def test_ngspice_case_sweeps_match_independent_formulas(
    ngspice_cases: tuple[Any, list[NgCase]],
) -> None:
    """Published (quantized) hidden-circuit sweeps vs the formulas in this file: 1 % / 1 deg."""
    _rig, cases = ngspice_cases
    r0, c0 = DESIGN
    for item in cases:
        open_c = item.condition == "c1_disconnected"
        mr, mc = VARIANTS.get(item.condition, (1.0, 1.0))
        r, c = r0 * mr, c0 * mc
        for obs in item.case.observations:
            if obs.sweep is None:
                continue
            for k, f in enumerate(obs.sweep.frequency_hz):
                assert math.isclose(f, CASE_GRID[k], rel_tol=1e-6)
                m, p = obs.sweep.magnitude[k], obs.sweep.phase_deg[k]
                if obs.sweep.quantity == "output_gain":
                    he = h_model(item.family, f, r, c, open_c)
                    if abs(he) == 0.0:
                        assert m == GAIN_FLOOR_DB
                        continue
                    assert abs(m - db(he)) <= 20 * math.log10(1.01), (item.condition, f)
                    assert angle_diff(p, deg(he)) <= 1.0
                else:
                    ie = i_model(f, r, c, open_c) * 1e3
                    if abs(ie) == 0.0:
                        assert m == 0.0
                        continue
                    assert abs(m - abs(ie)) <= 0.01 * abs(ie)
                    assert angle_diff(p, deg(ie)) <= 1.0


@pytest.mark.ngspice
def test_below_floor_magnitudes_are_published_as_exact_zero(
    ngspice_cases: tuple[Any, list[NgCase]],
) -> None:
    _rig, cases = ngspice_cases
    for item in cases:
        for obs in item.case.observations:
            if obs.transient is None:
                continue
            for t in obs.transient.tones:
                if t.gain_db is None and t.amplitude <= CURRENT_FLOOR_MA:
                    assert t.amplitude == 0.0, (item.family, item.condition, t.amplitude)


# ======================================================================================
# 3. Generator fairness (no simulator)
# ======================================================================================


def _chi2_z(chi2: float, df: int) -> float:
    """Wilson-Hilferty normal approximation of a chi-square statistic."""
    a = 2.0 / (9.0 * df)
    return ((chi2 / df) ** (1.0 / 3.0) - (1.0 - a)) / math.sqrt(a)


def test_condition_prior_and_design_independence_over_many_draws() -> None:
    """12 000 deterministic opaque seeds per family through the hosted generator: the condition
    marginal matches policy.FAULT_PRIOR, and the (public) nominal design is statistically
    independent of the (hidden) condition. Thresholds: |z| < 4.5 (p ~ 3e-6 per test)."""
    assert pytest.approx(1.0) == sum(policy.FAULT_PRIOR.values())
    assert set(policy.FAULT_PRIOR) == set(CONDITIONS)
    n = 12_000
    for family in FAMILIES:
        designs = faults.nominal_designs(family)
        table: Counter[tuple[int, str]] = Counter()
        cond: Counter[str] = Counter()
        for i in range(n):
            seed = hashlib.sha256(f"verif-m2|{family}|{i}".encode()).hexdigest()[:32]
            hidden = faults.generate(seed, family)
            k = designs.index((hidden.nominal["R1"], hidden.nominal["C1"]))
            table[(k, hidden.fault.hypothesis_id)] += 1
            cond[hidden.fault.hypothesis_id] += 1
        gof = sum((cond[h] - n * p) ** 2 / (n * p) for h, p in policy.FAULT_PRIOR.items())
        assert _chi2_z(gof, len(CONDITIONS) - 1) < 4.5, (family, dict(cond))
        rows: Counter[int] = Counter()
        for (k, _h), v in table.items():
            rows[k] += v
        chi2 = sum(
            (table[(k, h)] - rows[k] * cond[h] / n) ** 2 / (rows[k] * cond[h] / n)
            for k in range(len(designs))
            for h in CONDITIONS
        )
        df = (len(designs) - 1) * (len(CONDITIONS) - 1)
        z = _chi2_z(chi2, df)
        assert abs(z) < 4.5, (family, chi2, df, z)
        # Every drawable design is valid for every condition (no conditional design sets).
        for r, c in designs:
            for h, (mr, mc) in VARIANTS.items():
                assert 20.0 <= fc_of(r * mr, c * mc) <= 20_000.0, (family, r, c, h)
                assert 100.0 <= r * mr <= 1e5 and 1e-9 <= c * mc <= 1e-5
        print(
            f"\n[M2 generator] {family}: {len(designs)} designs, condition counts {dict(cond)}, "
            f"independence chi2={chi2:.1f} df={df} z={z:.2f}"
        )


def test_family_draw_follows_its_prior_and_hosted_seeds_are_opaque(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert isinstance(families._SYSTEM_RANDOM, secrets.SystemRandom)  # OS entropy when hosted
    monkeypatch.setattr(families, "_SYSTEM_RANDOM", random.Random(20261023))
    draws = Counter(families.draw_family() for _ in range(8000))
    p = policy.CASE_FAMILY_PRIOR["rc_lowpass"]
    sigma = math.sqrt(8000 * p * (1 - p))
    assert abs(draws["rc_lowpass"] - 8000 * p) < 4.5 * sigma, draws
    assert set(draws) == set(FAMILIES)
    seeds = {faults.new_seed() for _ in range(2000)}
    assert len(seeds) == 2000 and all(re.fullmatch(r"[0-9a-f]{32}", s) for s in seeds)
    # The in-process evaluator entry point stores exactly what the generator draws and returns
    # only the public nominal design.
    planned = [hashlib.sha256(f"verif-m2-ev|{i}".encode()).hexdigest()[:32] for i in range(120)]
    feed = iter(planned)
    monkeypatch.setattr(faults, "new_seed", lambda: next(feed))
    store = EvaluatorStore(tmp_path / "ev.sqlite3")
    ev = Evaluator(store, None, None)  # type: ignore[arg-type]  # creation never simulates
    try:
        for i, seed in enumerate(planned):
            family = FAMILIES[i % 2]
            design = ev.create_case(f"case_verifm2{i:08d}", family)
            expected = faults.generate(seed, family)
            assert design.values == dict(expected.nominal) and design.template_id == family
            assert set(vars(design)) == {"template_id", "values"}
            hidden = store.get(f"case_verifm2{i:08d}")
            assert hidden is not None and hidden.seed == seed
            assert hidden.fault.hypothesis_id == expected.fault.hypothesis_id
    finally:
        store.close()


# ======================================================================================
# 4. Learn mode
# ======================================================================================


def _in_range(values: dict[str, float]) -> bool:
    r, c = values["R1"], values["C1"]
    return 100.0 <= r <= 1e5 and 1e-9 <= c <= 1e-5 and 20.0 <= fc_of(r, c) <= 20_000.0


def test_learn_prompts_stay_inside_the_supported_range_and_are_complete() -> None:
    designs = random_designs(40, seed=7) + [
        (100.0, 1e-5),
        (1e5, 1e-9),
        (99_000.0, 8e-8),
        (101.0, 7.9e-8),
    ]
    offered_total = 0
    for i, (r, c) in enumerate(designs):
        family = FAMILIES[i % 2]
        rev = build_revision(family, {"R1": r, "C1": c}, origin="user_edit")
        prompts = learn_mod.learn_prompts(rev)
        assert prompts == learn_mod.learn_prompts(rev)  # deterministic (ids, text, order)
        offered = {(p.change.component_id, p.change.multiplier) for p in prompts}
        for comp in ("R1", "C1"):
            for mult in policy.LEARN_MULTIPLIERS:
                values: dict[str, float] = {
                    c_.id: float(c_.value_si)
                    for c_ in rev.components
                    if c_.id in ("R1", "C1") and c_.value_si
                }
                values[comp] *= mult
                assert ((comp, mult) in offered) == _in_range(values), (family, r, c, comp, mult)
        for p in prompts:
            names = [t.label.lower() for t in rev.source.tones]
            assert all(n in p.question.lower() for n in names), p.question
            assert p.tones == list(rev.source.tones)
        offered_total += len(prompts)
    assert offered_total > 100


class Scripted:
    """Wraps the analytical TEST DOUBLE: counts calls, optionally fails or cancels at call N."""

    def __init__(self) -> None:
        self.inner = AnalyticTestSimulator()
        self.calls = 0
        self.fail_from: int | None = None
        self.on_call: Any = None

    def info(self) -> Any:
        return self.inner.info()

    def _tick(self) -> None:
        self.calls += 1
        if self.on_call is not None:
            self.on_call(self.calls)
        if self.fail_from is not None and self.calls >= self.fail_from:
            raise SimulationFailed("The simulator could not complete this run.")

    async def ac(self, circuit: Any, sweep: Any, *, cancel: Any = None) -> Any:
        self._tick()
        return await self.inner.ac(circuit, sweep, cancel=cancel)

    async def transient(self, circuit: Any, request: Any, *, cancel: Any = None) -> Any:
        self._tick()
        return await self.inner.transient(circuit, request, cancel=cancel)


@pytest.fixture
def scripted_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, Scripted]]:
    import resonance.app as app_module

    sim = Scripted()
    monkeypatch.setattr(app_module, "create_simulator", lambda _s: sim)
    with TestClient(app_module.create_app(_settings(tmp_path))) as client:
        assert client.get("/api/health").status_code == 200
        yield client, sim


def _revision(client: TestClient, family: str, r: str, c: str) -> dict[str, Any]:
    resp = client.post(
        "/api/revisions",
        json={
            "template_id": family,
            "values": [{"component_id": "R1", "value": r}, {"component_id": "C1", "value": c}],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _predictions(rev: dict[str, Any], choice: str = "quieter") -> list[dict[str, Any]]:
    return [
        {"frequency_hz": t["frequency_hz"], "expectation": choice} for t in rev["source"]["tones"]
    ]


@pytest.mark.parametrize("family", FAMILIES)
def test_learn_grades_from_recorded_measurements_and_cites_owned_experiments(
    scripted_client: tuple[TestClient, Scripted], family: str
) -> None:
    client, _sim = scripted_client
    rev = _revision(client, family, "1k", "100n")
    resp = client.post(
        "/api/learn/attempts",
        json={
            "revision_id": rev["id"],
            "change": {"component_id": "R1", "multiplier": 0.5},
            "predictions": _predictions(rev, "louder"),
        },
    )
    assert resp.status_code == 201, resp.text
    attempt = resp.json()
    base = client.get(f"/api/experiments/{attempt['base_experiment_id']}").json()
    new = client.get(f"/api/experiments/{attempt['new_experiment_id']}").json()
    assert base["revision_id"] == rev["id"] and new["revision_id"] == attempt["new_revision_id"]
    new_rev = client.get(f"/api/revisions/{attempt['new_revision_id']}").json()
    r_new = [c for c in new_rev["components"] if c["id"] == "R1"][0]["value_si"]
    assert math.isclose(r_new, 500.0, rel_tol=1e-12)
    for g in attempt["graded"]:
        a = [t for t in base["tones"] if t["frequency_hz"] == g["frequency_hz"]][0]["ac_gain_db"]
        b = [t for t in new["tones"] if t["frequency_hz"] == g["frequency_hz"]][0]["ac_gain_db"]
        assert math.isclose(g["delta_db"], b - a, abs_tol=1e-12)
        expected = "about_the_same" if abs(b - a) < 1.0 else ("louder" if b > a else "quieter")
        assert g["observed"] == expected and g["correct"] == (g["predicted"] == expected)
        assert {e["result_id"] for e in g["evidence"]} == {base["id"], new["id"]}
        assert f"{a:.2f} dB → {b:.2f} dB" in attempt["explanation"]
        # Independent physics: halving R1 doubles the cutoff.
        fc0 = fc_of(1000.0, 1e-7)
        f = g["frequency_hz"]
        want = attenuation(family, f, fc0) - attenuation(family, f, 2 * fc0)
        assert math.isclose(g["delta_db"], want, abs_tol=1e-6)
    assert attempt["score"] == sum(g["correct"] for g in attempt["graded"])
    assert base["id"] in attempt["explanation"] and new["id"] in attempt["tradeoff"]
    # Cited IDs are owned by this session only.
    client.cookies.clear()
    client.get("/api/health")
    for eid in (base["id"], new["id"]):
        assert client.get(f"/api/experiments/{eid}").status_code == 404
    assert client.get(f"/api/learn/attempts/{attempt['id']}").status_code == 404


def _counts(client: TestClient) -> tuple[int, int]:
    wb = client.get("/api/workbench").json()
    return len(wb["revisions"]), len(wb["experiments"])


def test_learn_attempt_failure_stores_nothing(scripted_client: tuple[TestClient, Scripted]) -> None:
    client, sim = scripted_client
    rev = _revision(client, "rc_highpass", "2.2k", "47n")
    before = _counts(client)
    sim.fail_from = sim.calls + 4  # the base bundle (3 jobs) succeeds, the changed one fails
    resp = client.post(
        "/api/learn/attempts",
        json={
            "revision_id": rev["id"],
            "change": {"component_id": "C1", "multiplier": 2.0},
            "predictions": _predictions(rev),
        },
    )
    assert resp.status_code in (502, 503, 504), resp.text
    assert "dB" not in resp.text and "47" not in resp.text
    assert _counts(client) == before
    sim.fail_from = None
    ok = client.post(
        "/api/learn/attempts",
        json={
            "revision_id": rev["id"],
            "change": {"component_id": "C1", "multiplier": 2.0},
            "predictions": _predictions(rev),
        },
    )
    assert ok.status_code == 201
    assert _counts(client) == (before[0] + 1, before[1] + 2)


async def test_learn_attempt_cancelled_mid_measurement_stores_nothing(tmp_path: Path) -> None:
    store = DocumentStore(tmp_path / "db.sqlite3")
    artifacts = ArtifactStore(tmp_path / "art", store, max_count=100, max_bytes=100 << 20)
    session = "verifm2learn_" + "c" * 24
    rev = build_revision("rc_lowpass", {"R1": 1000.0, "C1": 1e-7}, origin="user_edit")
    store.put("revision", rev.id, session, rev)
    from resonance.contracts.internal import CancelToken

    token = CancelToken()
    sim = Scripted()
    sim.on_call = lambda n: token.cancel() if n == 4 else None
    body = LearnAttemptRequest.model_validate(
        {
            "revision_id": rev.id,
            "change": {"component_id": "R1", "multiplier": 2.0},
            "predictions": [
                {"frequency_hz": 440.0, "expectation": "quieter"},
                {"frequency_hz": 4000.0, "expectation": "quieter"},
            ],
        }
    )
    with pytest.raises(ApiException) as info:
        await learn_mod.run_learn_attempt(
            simulator=sim,
            store=store,
            artifacts=artifacts,
            session_id=session,
            revision=rev,
            body=body,
            goal=None,
            cancel=token,
        )
    assert info.value.error.code.value == "cancelled"
    assert store.list_docs("experiment", session, ExperimentRecord, limit=10) == []
    assert len(store.list_docs("revision", session, type(rev), limit=10)) == 1
    store.close()


def test_frozen_and_out_of_range_learn_and_suggest_requests_never_simulate(
    scripted_client: tuple[TestClient, Scripted],
) -> None:
    client, sim = scripted_client
    created = client.post("/api/cases", json={"template_id": "rc_highpass"})
    assert created.status_code == 201
    frozen = created.json()["intended_revision"]
    calls = sim.calls
    r = client.get("/api/learn/prompts", params={"revision_id": frozen["id"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "stale_revision"
    r = client.post(
        "/api/learn/attempts",
        json={
            "revision_id": frozen["id"],
            "change": {"component_id": "R1", "multiplier": 2.0},
            "predictions": _predictions(frozen),
        },
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "stale_revision"
    r = client.post("/api/workbench/goal/suggest", json={"revision_id": frozen["id"], "vary": "C1"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "stale_revision"
    near = _revision(client, "rc_lowpass", "60k", "10n")  # R1 x 4 = 240 kOhm > 100 kOhm
    before = _counts(client)
    r = client.post(
        "/api/learn/attempts",
        json={
            "revision_id": near["id"],
            "change": {"component_id": "R1", "multiplier": 4.0},
            "predictions": _predictions(near),
        },
    )
    assert r.status_code == 422 and r.json()["error"]["field"] == "change.multiplier"
    r = client.post(
        "/api/learn/attempts",
        json={
            "revision_id": near["id"],
            "change": {"component_id": "R1", "multiplier": 3.0},
            "predictions": _predictions(near),
        },
    )
    assert r.status_code == 422
    r = client.post(
        "/api/learn/attempts",
        json={
            "revision_id": near["id"],
            "change": {"component_id": "C1", "multiplier": 2.0},
            "predictions": [{"frequency_hz": 50.0, "expectation": "louder"}],
        },
    )
    assert r.status_code == 422  # the low-pass signal has no 50 Hz tone
    prompts = client.get("/api/learn/prompts", params={"revision_id": near["id"]}).json()
    assert ("R1", 4.0) not in {
        (p["change"]["component_id"], p["change"]["multiplier"]) for p in prompts
    }
    s = client.post("/api/workbench/goal/suggest", json={"revision_id": near["id"], "vary": "R1"})
    assert s.status_code == 200 and s.json()["explanation"].startswith("Prediction")
    assert sim.calls == calls and _counts(client) == before  # nothing simulated or stored


# Learn at the 1 dB "about the same" boundary on REAL ngspice. Low-pass, C1 = 100 nF, R1 chosen
# so that doubling C1 changes the 440 Hz gain by -0.997 dB (below the threshold) or -1.003 dB.
def _r_for_delta(delta_db: float, f: float = 440.0, c: float = 1e-7) -> float:
    y = 10 ** (delta_db / 10.0)
    fc = f / math.sqrt((y - 1.0) / (4.0 - y))
    return 1.0 / (TWO_PI * fc * c)


@pytest.fixture(scope="module")
def learn_boundary(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[float, dict[str, Any]]]:
    root = tmp_path_factory.mktemp("verif_m2_learn")
    out: dict[float, dict[str, Any]] = {}
    with TestClient(create_app(_settings(root))) as client:
        client.get("/api/health")
        for delta in (0.997, 1.003):
            rev = _revision(client, "rc_lowpass", f"{_r_for_delta(delta):.6f}", "100n")
            resp = client.post(
                "/api/learn/attempts",
                json={
                    "revision_id": rev["id"],
                    "change": {"component_id": "C1", "multiplier": 2.0},
                    "predictions": _predictions(rev, "quieter"),
                },
            )
            assert resp.status_code == 201, resp.text
            out[delta] = resp.json()
    yield out


def _shown_change(attempt: dict[str, Any], label: str) -> tuple[float, str]:
    m = re.search(
        re.escape(label)
        + r" \([^)]*\) -?[\d.]+ dB → -?[\d.]+ dB, (unchanged|([\d.]+) dB (quieter|louder))",
        attempt["explanation"],
    )
    assert m, attempt["explanation"]
    if m.group(1) == "unchanged":
        return 0.0, "unchanged"
    return float(m.group(2)), m.group(3)


@pytest.mark.ngspice
def test_learn_just_beyond_one_db_is_quieter(learn_boundary: dict[float, dict[str, Any]]) -> None:
    attempt = learn_boundary[1.003]
    tone = [g for g in attempt["graded"] if g["frequency_hz"] == 440.0][0]
    assert math.isclose(tone["delta_db"], -1.003, abs_tol=1e-6)  # measured, ngspice
    assert tone["observed"] == "quieter" and tone["correct"] is True


@pytest.mark.ngspice
def test_learn_text_and_grade_agree_just_below_one_db(
    learn_boundary: dict[float, dict[str, Any]],
) -> None:
    attempt = learn_boundary[0.997]
    tone = [g for g in attempt["graded"] if g["frequency_hz"] == 440.0][0]
    assert math.isclose(tone["delta_db"], -0.997, abs_tol=1e-6)
    shown, _direction = _shown_change(attempt, "Musical tone")
    shown_grade = "about_the_same" if shown < policy.LEARN_SAME_THRESHOLD_DB else "quieter"
    assert shown_grade == tone["observed"], (shown, tone["observed"], attempt["explanation"])


# ======================================================================================
# 5. Design suggestions (independent brute force; analytical, no simulator)
# ======================================================================================

E12 = (1.0, 1.2, 1.5, 1.8, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2)


def _e12(lo: float, hi: float) -> list[float]:
    return [
        float(f"{m}e{e}")
        for e in range(-10, 7)
        for m in E12
        if lo * (1 - 1e-12) <= float(f"{m}e{e}") <= hi * (1 + 1e-12)
    ]


def _margin(family: str, c: GoalConstraint, fc: float) -> float:
    a = attenuation(family, c.frequency_hz, fc)
    return c.limit_db - a if c.kind == "preserve" else a - c.limit_db


def _feasible_interval(family: str, goal: Goal) -> tuple[float, float] | None:
    lo, hi = 20.0, 20_000.0
    for c in goal.constraints:
        k = 10 ** (c.limit_db / 10.0) - 1.0
        if family == "rc_lowpass":
            bound = math.inf if k == 0 else c.frequency_hz / math.sqrt(k)
            lo, hi = (max(lo, bound), hi) if c.kind == "preserve" else (lo, min(hi, bound))
        else:
            bound = c.frequency_hz * math.sqrt(k)
            lo, hi = (lo, min(hi, bound)) if c.kind == "preserve" else (max(lo, bound), hi)
    return (lo, hi) if lo <= hi else None


def test_suggestions_pick_a_passing_e12_part_whenever_one_exists_and_report_honest_numbers() -> (
    None
):
    rng = random.Random(20261023)
    checked = infeasible = 0
    for trial in range(220):
        family = FAMILIES[trial % 2]
        r, c = random_designs(1, seed=rng.randrange(1 << 30))[0]
        rev = build_revision(family, {"R1": r, "C1": c}, origin="user_edit")
        if trial % 4 == 0:
            goal = default_goal(family)
        else:
            goal = Goal(
                id="goal_verifm2goal00",
                title="verification goal",
                origin="user",
                constraints=[
                    GoalConstraint(
                        kind=rng.choice(("preserve", "attenuate")),
                        frequency_hz=round(
                            10 ** rng.uniform(math.log10(20), math.log10(20_000)), 1
                        ),
                        limit_db=round(rng.uniform(0.5, 30.0), 1),
                        label=f"Tone {j}",
                    )
                    for j in range(rng.randint(1, 3))
                ],
            )
        interval = _feasible_interval(family, goal)
        for vary in ("R1", "C1"):
            s = suggest_design(rev, goal, vary=vary)
            fixed = c if vary == "R1" else r
            assert s.values[1].component_id != vary and s.values[1].value_si == fixed
            assert s.feasible == (interval is not None)
            if interval is not None:
                assert math.isclose(
                    s.target_cutoff_hz, math.sqrt(interval[0] * interval[1]), rel_tol=1e-9
                )
            bounds = (100.0, 1e5) if vary == "R1" else (1e-9, 1e-5)
            scored = []
            for v in _e12(*bounds):
                fcv = fc_of(v, fixed)
                if 20.0 <= fcv <= 20_000.0:
                    m = min(_margin(family, k, fcv) for k in goal.constraints)
                    scored.append(((m >= 0.0, m), v))
            best = max(scored)
            chosen = s.values[0].value_si
            assert s.values[0].series == "E12" and chosen in [v for _, v in scored]
            mine = [key for key, v in scored if v == chosen][0]
            if best[0][0]:
                assert mine[0], (
                    "a passing E12 part exists but the suggestion fails",
                    family,
                    vary,
                    r,
                    c,
                )
            else:
                assert mine[1] >= best[0][1] - 1e-6, ("not the best compromise", family, vary)
            fcc = fc_of(chosen, fixed)
            assert math.isclose(s.predicted_cutoff_hz, fcc, rel_tol=1e-9)
            for p, k in zip(s.predicted, goal.constraints, strict=True):
                assert math.isclose(
                    -p.predicted_gain_db, attenuation(family, k.frequency_hz, fcc), abs_tol=1e-9
                )
                assert math.isclose(p.margin_db, _margin(family, k, fcc), abs_tol=1e-9)
            assert s.predicted_goal_pass == mine[0] == (s.tradeoff is None)
            assert s.basis == "analytical_first_order_rc"
            assert s.explanation.startswith(
                "Prediction from first-order RC equations, not a measurement."
            )
            assert "measured" not in s.explanation.lower()
            if s.tradeoff is not None:
                assert s.tradeoff.startswith("Prediction")
                assert "measured" not in s.tradeoff.lower()
                for p, k in zip(s.predicted, goal.constraints, strict=True):
                    if p.passes:
                        continue
                    shortfall = -_margin(family, k, fcc)
                    att = attenuation(family, k.frequency_hz, fcc)
                    text = f"misses by {shortfall:.2f} dB (predicted attenuation {att:.2f} dB"
                    assert text in s.tradeoff, (text, s.tradeoff)
                infeasible += not s.feasible
            checked += 1
    assert checked == 440 and infeasible > 10, infeasible


def test_rounding_tradeoff_names_an_exact_value_that_really_meets_the_goal() -> None:
    """Feasible goal, but neither E12 neighbour of the ideal part passes: the tradeoff must
    say so with correct shortfalls, and its 'exact value' must meet every constraint."""
    goal = default_goal("rc_lowpass")
    lo, hi = _feasible_interval("rc_lowpass", goal) or (0.0, 0.0)
    found = 0
    for k in range(400):
        r = 10 ** (2.2 + 2.6 * k / 400)
        c_lo, c_hi = 1 / (TWO_PI * hi * r), 1 / (TWO_PI * lo * r)
        if any(c_lo <= v <= c_hi for v in _e12(1e-9, 1e-5)) or not 1e-9 < c_lo < c_hi < 1e-5:
            continue
        rev = build_revision("rc_lowpass", {"R1": r, "C1": 1e-7}, origin="user_edit")
        s = suggest_design(rev, goal, vary="C1")
        assert s.feasible and not s.predicted_goal_pass and s.tradeoff is not None
        assert "rounding to the nearest standard part" in s.tradeoff
        ideal = 1 / (TWO_PI * math.sqrt(lo * hi) * r)
        assert all(_margin("rc_lowpass", k_, fc_of(r, ideal)) >= 0 for k_ in goal.constraints)
        found += 1
    assert found >= 3


# ======================================================================================
# 6. Evaluation harness honesty
# ======================================================================================


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"nebius_api_key": "your_nebius_api_key_here", "nebius_model": "some-model"},
        {"nebius_api_key": "sk-verification-not-a-real-key", "nebius_model": None},
        {
            "nebius_api_key": "sk-verification-not-a-real-key",
            "nebius_model": "m",
            "resonance_dev_agent_fixture": True,
        },
    ],
    ids=["missing_key", "placeholder_key", "missing_model", "dev_fixture"],
)
def test_nemotron_runner_refuses_and_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    extra: dict[str, Any],
) -> None:
    def forbidden(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the refusing runner must not create scratch data or run cases")

    monkeypatch.setattr(run_nemotron.tempfile, "mkdtemp", forbidden)
    monkeypatch.setattr(run_nemotron.harness, "run_set", forbidden)
    results = tmp_path / "results"
    code = run_nemotron.main(["--results-dir", str(results)], settings=_settings(tmp_path, **extra))
    assert code == 2
    assert not results.exists()
    err = capsys.readouterr().err
    assert "NOT run" in err and "No results were written" in err
    assert "sk-verification" not in err


async def test_baseline_protocol_matches_the_documented_protocol(tmp_path: Path) -> None:
    case_set = casesets.load("dev_v1")
    keys = [
        c.key
        for c in case_set.cases
        if c.expected_condition in ("c1_disconnected", "r1_increased", "no_fault")
    ]
    specs = casesets.select(case_set, keys, None)
    run = await harness.run_set(
        "baseline",
        case_set,
        specs,
        _settings(tmp_path),
        simulator=AnalyticTestSimulator(),
        progress=lambda _m: None,
    )
    readme = (_REPO / "evals" / "README.md").read_text(encoding="utf-8")
    assert "Run `output_voltage_sweep`, then `source_current_sweep`" in readme
    assert harness.BASELINE_PLAN == ("output_voltage_sweep", "source_current_sweep")
    for res in run["results"]:
        spec = [s for s in specs if s.key == res["key"]][0]
        assert res["completed"] and res["error"] is None
        assert res["experiment_sequence"] == ["output_voltage_sweep", "source_current_sweep"]
        assert res["experiments_used"] == 2 and res["true_condition"] == spec.expected_condition
        assert res["evidence_check"] == "supported" and res["exact_identification"] == "correct"
        if spec.expected_condition == "no_fault":
            assert res["conclusion"] == "no_fault" and res["functional_recovery"] == "not_needed"
            assert res["repair_component"] is None
        else:
            assert res["repair_component"] == (
                "C1" if spec.expected_condition.startswith("c1") else "R1"
            )
            assert res["functional_recovery"] == "restored"
        assert set(res["latency_s"]) <= {
            "symptom",
            "output_voltage_sweep",
            "source_current_sweep",
            "diagnosis",
            "verify_repair",
            "close",
            "total",
        }
        assert res["agent"] is None
    assert run["model"] is None and run["runner"] == "baseline"
    assert report.summarize(run["results"])["totals"]["cases"] == len(specs)


def test_committed_baseline_results_match_the_frozen_set_and_the_readme() -> None:
    results_dir = _REPO / "evals" / "results"
    path = results_dir / "baseline-heldout_v1-20261021T174452Z.json"
    run = json.loads(path.read_text(encoding="utf-8"))
    heldout_bytes = (_REPO / "evals" / "cases" / "heldout_v1.json").read_bytes()
    assert run["case_set"]["sha256"] == hashlib.sha256(heldout_bytes).hexdigest()
    frozen = {c.key: c for c in casesets.load("heldout_v1").cases}
    assert len(run["results"]) == len(frozen) == 24
    for res in run["results"]:
        spec = frozen[res["key"]]
        assert res["seed"] == spec.seed and res["template_id"] == spec.template_id
        assert (res["nominal"]["R1"], res["nominal"]["C1"]) == spec.expected_nominal
        assert res["true_condition"] == spec.expected_condition
        assert res["experiment_sequence"] == ["output_voltage_sweep", "source_current_sweep"]
        assert all(v > 0 for v in res["latency_s"].values())
    # Independent recount (not report.summarize) of the README claims.
    rs = run["results"]
    assert sum(r["exact_identification"] == "correct" for r in rs) == 24
    assert sum(r["exact_identification"] == "abstained" for r in rs) == 0
    assert sum(bool(r["unsupported_certainty"]) for r in rs) == 0
    assert sum(r["functional_recovery"] == "restored" for r in rs) == 20
    assert sum(r["functional_recovery"] == "not_needed" for r in rs) == 4
    assert sum(r["infrastructure_failures"] for r in rs) == 0
    totals = sorted(r["latency_s"]["total"] for r in rs)
    mean = sum(totals) / len(totals)
    median = (totals[11] + totals[12]) / 2
    readme = " ".join((_REPO / "evals" / "README.md").read_text(encoding="utf-8").split())
    assert (
        f"mean wall time was {mean:.1f} s per case (median {median:.1f} s, max {totals[-1]:.1f} s)"
        in readme
    )
    keys = {k.lower() for k in _all_keys(run)}
    assert not {k for k in keys if "cost" in k or "price" in k or "usd" in k}, keys
    assert run["runner"] == "baseline" and run["model"] is None
    # Committed Nemotron results must be genuine runs of the pinned model on the frozen sets,
    # carry no cost figures, and match what the README reports (independent recount).
    pinned = "nvidia/nemotron-3-super-120b-a12b"
    for path in results_dir.glob("nemotron-*.json"):
        nem = json.loads(path.read_text(encoding="utf-8"))
        assert nem["runner"] == "nemotron" and nem["model"] == pinned, path.name
        assert nem["case_set"]["name"] in casesets.SET_NAMES, path.name
        assert all(r["runner"] == "nemotron" for r in nem["results"]), path.name
        nem_keys = {k.lower() for k in _all_keys(nem)}
        assert not {k for k in nem_keys if "cost" in k or "price" in k or "usd" in k}, path.name
        assert path.with_suffix(".md").is_file(), path.name
    heldout = sorted(results_dir.glob("nemotron-heldout_v1-*.json"))
    if heldout:
        nem = json.loads(heldout[0].read_text(encoding="utf-8"))
        nrs = nem["results"]
        assert len(nrs) == 24
        assert sum(r["exact_identification"] == "correct" for r in nrs) == 20
        assert sum(r["exact_identification"] == "incorrect" for r in nrs) == 1
        assert sum(r["exact_identification"] == "not_submitted" for r in nrs) == 3
        assert sum(bool(r["unsupported_certainty"]) for r in nrs) == 1
        assert sum(r["functional_recovery"] == "restored" for r in nrs) == 9
        root_readme = " ".join((_REPO / "README.md").read_text(encoding="utf-8").split())
        assert "| Correct identification | 24/24 | 20/24 |" in root_readme
        assert "| Faulty cases restored by a verified repair | 20/20 | 9/20" in root_readme


def _r_spellings(r: float) -> set[str]:
    k = f"{r / 1000:g}"
    out = {f"{k}k", f"{k} k", f"{k}kΩ", f"{k} kΩ"}
    if "." in k:
        a, b = k.split(".")
        out.add(f"{a}k{b}")
    if r < 1000:
        out |= {f"{r:g} Ω", f"{r:g}Ω"}
    return out


def _c_spellings(c: float) -> set[str]:
    n, u = f"{c * 1e9:g}", f"{c * 1e6:g}"
    return {f"{n}n", f"{n} n", f"{n}nF", f"{n} nF", f"{u}u", f"{u} µF", f"{u}µF", f"{u} uF"}


_NUM = re.compile(r"(?<![\w.])-?\d[\d_]*(?:\.\d+)?(?:e[+-]?\d+)?(?![\w.])", re.IGNORECASE)


def _file_numbers(path: Path, text: str) -> set[float]:
    if path.suffix == ".py":
        tree = ast.parse(text)
        return {
            float(n.value)
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant)
            and isinstance(n.value, int | float)
            and not isinstance(n.value, bool)
        }
    out: set[float] = set()
    for m in _NUM.finditer(text):
        try:
            out.add(float(m.group().replace("_", "")))
        except ValueError:
            pass
    return out


def test_heldout_designs_never_appear_as_a_design_in_any_test_or_agent_prompt() -> None:
    """Broader than the builders' scan: every backend test directory, the frontend sources and
    e2e tests, and the agent prompt module; numeric literals AND unit spellings ('4.7k', '220 nF').
    A file counts only if it holds BOTH the R and the C of a held-out design (R and C on the
    same line for non-Python files, to avoid counting unrelated numbers)."""
    roots = [
        _TESTS,
        _REPO / "apps" / "web" / "src",
        _REPO / "apps" / "web" / "e2e",
        _REPO / "services" / "api" / "resonance" / "agent",
    ]
    hits = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if (
                path.suffix not in (".py", ".ts", ".tsx")
                or "node_modules" in path.parts
                or path.name == Path(__file__).name
            ):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            nums = _file_numbers(path, text)
            for family, designs in casesets.HELDOUT_DESIGNS.items():
                for r, c in designs:
                    has_r = any(math.isclose(x, r, rel_tol=1e-9) for x in nums) or any(
                        s in text for s in _r_spellings(r)
                    )
                    has_c = any(math.isclose(x, c, rel_tol=1e-9) for x in nums) or any(
                        s in text for s in _c_spellings(c)
                    )
                    if not (has_r and has_c):
                        continue
                    # Same-statement check: an actual design pairs R and C closely.
                    lines = text.splitlines()
                    for i in range(len(lines)):
                        window = " ".join(lines[i : i + 3])
                        wn = _file_numbers(Path("x.txt"), window)
                        wr = any(math.isclose(x, r, rel_tol=1e-9) for x in wn) or any(
                            s in window for s in _r_spellings(r)
                        )
                        wc = any(math.isclose(x, c, rel_tol=1e-9) for x in wn) or any(
                            s in window for s in _c_spellings(c)
                        )
                        if wr and wc:
                            hits.append((str(path), family, r, c, i + 1))
    assert hits == [], hits


# ======================================================================================
# 7. High-pass audio honesty (real ngspice)
# ======================================================================================

FS = 48_000
FADE_N = 720
HP_PERIOD_N = 4_800  # 1 / gcd(50 Hz, 440 Hz) = 100 ms
HP_WINDOW = (FADE_N, FADE_N + 11 * HP_PERIOD_N)


def _pcm(raw: bytes) -> np.ndarray:
    with wave.open(io.BytesIO(raw), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, FS)
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype="<i2").astype(np.float64)


def _fit(pcm: np.ndarray, freqs: tuple[float, ...]) -> list[float]:
    seg = pcm[HP_WINDOW[0] : HP_WINDOW[1]]
    t = np.arange(seg.size) / FS
    cols = []
    for f in freqs:
        cols += [np.sin(TWO_PI * f * t), np.cos(TWO_PI * f * t)]
    cols.append(np.ones_like(t))
    coef, *_ = np.linalg.lstsq(np.stack(cols, axis=1), seg, rcond=None)
    return [math.hypot(coef[2 * k], coef[2 * k + 1]) for k in range(len(freqs))]


HUM_NOTE = "Mains hum (50 Hz) is in every clip at its measured level"


@pytest.mark.ngspice
def test_highpass_explore_audio_levels_equal_measured_gains_at_one_shared_gain(
    tmp_path: Path,
) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        client.get("/api/health")
        records = []
        for r_text, c_text in (("1k", "100n"), ("2.2k", "47n")):
            rev = _revision(client, "rc_highpass", r_text, c_text)
            resp = client.post("/api/experiments", json={"revision_id": rev["id"]})
            assert resp.status_code == 201, resp.text
            records.append(resp.json())
        for rec, (r, c) in zip(records, ((1000.0, 1e-7), (2200.0, 47e-9)), strict=True):
            audio = rec["audio"]
            assert audio["playback_gain_fs_per_v"] == policy.PLAYBACK_GAIN_FS_PER_V
            assert audio["gain_adjustment_db"] == 0.0 and HUM_NOTE in audio["note"]
            clips = {clip["role"]: clip for clip in audio["clips"]}
            pcm = {role: _pcm(client.get(clip["url"]).content) for role, clip in clips.items()}
            for role, clip in clips.items():
                assert not clip["clipped"]
                assert abs(float(np.max(np.abs(pcm[role]))) / 32767 - clip["peak_fs"]) <= 2e-4
            amp_in = _fit(pcm["input"], (50.0, 440.0))
            amp_out = _fit(pcm["output"], (50.0, 440.0))
            # The input clip is the source itself at the shared gain: 0.3 V hum, 0.6 V tone.
            assert abs(amp_in[0] / 32767 - 0.3) < 1e-4 and abs(amp_in[1] / 32767 - 0.6) < 1e-4
            for k, f in enumerate((50.0, 440.0)):
                tone = [t for t in rec["tones"] if t["frequency_hz"] == f][0]
                pcm_db = db(amp_out[k] / amp_in[k])
                assert abs(pcm_db - tone["ac_gain_db"]) <= 0.1, (f, pcm_db, tone["ac_gain_db"])
                assert abs(tone["ac_gain_db"] - db(h_model("rc_highpass", f, r, c))) <= 0.05
        cmp = client.get(
            "/api/experiments/compare", params={"a": records[0]["id"], "b": records[1]["id"]}
        )
        assert cmp.status_code == 200, cmp.text
        comparison = cmp.json()
        assert HUM_NOTE in comparison["audio"]["note"]
        assert comparison["audio"]["gain_adjustment_db"] == 0.0
        clips = {
            c["role"]: _pcm(client.get(c["url"]).content) for c in comparison["audio"]["clips"]
        }
        prev, cur = _fit(clips["previous"], (50.0, 440.0)), _fit(clips["output"], (50.0, 440.0))
        for k, d in enumerate(comparison["tones"]):
            assert abs(db(cur[k] / prev[k]) - d["delta_db"]) <= 0.1, d


@pytest.mark.ngspice
def test_highpass_case_audio_shares_one_gain_and_discloses_the_low_hum(
    ngspice_cases: tuple[Any, list[NgCase]],
) -> None:
    rig, cases = ngspice_cases
    for item in cases:
        if item.family != "rc_highpass":
            continue
        symptom = item.case.observations[0]
        assert symptom.kind == "symptom" and symptom.transient is not None
        audio = symptom.transient.audio
        assert audio is not None and HUM_NOTE in audio.note and audio.gain_adjustment_db == 0.0
        pcm = {}
        for clip in audio.clips:
            opened = rig.artifacts.open(clip.artifact_id, item.session)
            assert opened is not None
            pcm[clip.role] = _pcm(Path(opened[0]).read_bytes())
            assert abs(float(np.max(np.abs(pcm[clip.role]))) / 32767 - clip.peak_fs) <= 2e-4
        if item.condition == "c1_disconnected":
            # An open high-pass is silent: the observed clip is exact digital silence, not a
            # normalized-up noise floor, while the expected clip keeps its level.
            observed_clip = [c for c in audio.clips if c.role == "observed"][0]
            assert not np.any(pcm["observed"]) and observed_clip.peak_fs == 0.0
            assert float(np.max(np.abs(pcm["expected"]))) > 1000
            continue
        exp, obs = _fit(pcm["expected"], (50.0, 440.0)), _fit(pcm["observed"], (50.0, 440.0))
        for k, tone in enumerate(symptom.transient.tones):
            assert tone.gain_db is not None and tone.expected_gain_db is not None
            # Same shared gain on both clips: their PCM ratio equals the measured gain ratio.
            assert abs(db(obs[k] / exp[k]) - (tone.gain_db - tone.expected_gain_db)) <= 0.1
            assert abs(exp[k] / 32767 - tone.expected_amplitude) <= 2e-4
