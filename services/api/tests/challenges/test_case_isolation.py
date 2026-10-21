"""Hidden-state isolation across every public surface (V-01/V-04/V-06 checks).

For one nominal design of one circuit family (RC low-pass or RC high-pass), a full
investigation runs over HTTP through the real app under EVERY hidden condition (four drift
faults, the open capacitor, and the no-fault control). Every response body, error body,
artifact header, app log record, and public database document produced BEFORE close is scanned
structurally for the hidden values (raw floats, scientific and SI-prefixed spellings), the seed,
and the true-cause label outside public catalog/evidence/submission paths.

Deterministic by construction (no false positives from randomness, no weakened checks):
- Opaque IDs (``new_id``: prefix + 16 random base64url characters) can contain short spellings
  such as "5k" or "20n" by chance (measured ≈ 2e-4 per ID). They are masked before the value
  scan and checked more strongly instead: exact ``new_id`` format, never shared between flows
  (so not derived from content), and challenge code never builds IDs itself.
- A numeric/string coincidence with a hidden value is a leak unless the leaf is a declared
  measurement, a public frequency, or IDENTICAL in all six variants of the same nominal
  design (a value that does not change with the hidden fault carries no information about it).
- Families: fixed nominal designs of both circuit families, plus one family drawn from a fresh
  random seed and a random circuit family per run (``RESONANCE_ISOLATION_SEED`` and
  ``RESONANCE_ISOLATION_FAMILY`` reproduce it); failures report seed, fault, payload, and field.

A positive control proves the scanner finds the same values in the reveal after close, and a
differential test requires the six payload streams to be identical once declared
measurements, IDs, and timestamps are masked.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Any

import pytest
from challenges_testkit import (
    OPAQUE_ID,
    AnalyticTestSimulator,
    Secrets,
    find_leaks,
    leaf_map,
    raw_text_leaks,
    secrets_for,
    seed_for,
    skeleton,
)
from fastapi.testclient import TestClient

from resonance.challenges import hypotheses
from resonance.challenges.evaluator import Evaluator, EvaluatorStore, faults
from resonance.challenges.families import draw_family
from resonance.config import Settings
from resonance.contracts.circuits import TemplateId, Tone
from resonance.contracts.internal import Simulator
from resonance.contracts.investigation import HypothesisId, Observation
from resonance.simulation import create_simulator

FAULTS: tuple[HypothesisId, ...] = hypotheses.HYPOTHESIS_IDS  # all six conditions
assert "c1_disconnected" in FAULTS and "no_fault" in FAULTS
# 10 kΩ/10 nF: R1×2 = 20 000 equals the public 20 kHz grid endpoint; R1×0.5 → "5k", C1×0.5 →
# "5n" (short spellings). 1 kΩ/100 nF: R1×2 = 2000 equals a grid point; C1×2 = 2e-7. Both are
# drawable in both families.
NOMINALS: dict[str, tuple[TemplateId, tuple[float, float]]] = {
    "lowpass-10k-10n": ("rc_lowpass", (10_000.0, 10e-9)),
    "highpass-1k-100n": ("rc_highpass", (1000.0, 100e-9)),
    "highpass-10k-10n": ("rc_highpass", (10_000.0, 10e-9)),
}
UNKNOWN_CASE = "case_unknownXXXXXXXX"
SEED_ENV = "RESONANCE_ISOLATION_SEED"
FAMILY_ENV = "RESONANCE_ISOLATION_FAMILY"


@dataclass
class Flow:
    fault: HypothesisId
    seed: str
    session: str
    template_id: TemplateId
    case_id: str = ""
    payloads: list[tuple[str, Any]] = field(default_factory=list)  # (label, JSON)
    artifact_meta: list[tuple[str, str, int, bytes]] = field(default_factory=list)
    sim_calls: int = 0
    ids: set[str] = field(default_factory=set)


def _session(i: int) -> str:
    return f"isolation{i:02d}_" + "s" * 32


def _use(client: TestClient, session: str) -> None:
    client.cookies.set("rs_session", session)


def family(
    template_id: TemplateId,
    nominal: tuple[float, float],
    override: tuple[HypothesisId, str] | None = None,
) -> list[Flow]:
    """One flow per hidden condition for the same circuit family and nominal design
    (optionally with one seed supplied, e.g. a fresh random seed)."""
    flows = []
    for i, fault in enumerate(FAULTS):
        if override and override[0] == fault:
            seed = override[1]
        else:
            seed = seed_for(fault, nominal, template_id)
        flows.append(Flow(fault=fault, seed=seed, session=_session(i), template_id=template_id))
    return flows


def run_flow(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    flow: Flow,
    calls: Callable[[], int],
) -> None:
    """Everything a learner can do before close, recording every public response."""
    monkeypatch.setattr(faults, "new_seed", lambda: flow.seed)
    _use(client, flow.session)
    before = calls()

    def record(label: str, response: Any, status: int) -> Any:
        assert response.status_code == status, (label, response.status_code, response.text)
        body = response.json()
        flow.payloads.append((label, body))
        return body

    case = record(
        "create", client.post("/api/cases", json={"template_id": flow.template_id}), 201
    )
    assert case["intended_revision"]["template_id"] == flow.template_id
    cid = flow.case_id = case["id"]
    base = f"/api/cases/{cid}"
    record("mode", client.post(f"{base}/mode", json={"mode": "guide"}), 200)
    experiments = [
        {"instrument": "output_voltage_sweep", "client_request_id": "iso-key-1"},
        {"instrument": "source_current_sweep", "client_request_id": "iso-key-2"},
        {
            "instrument": "transient_capture",
            "probe": "source_current",
            "client_request_id": "iso-key-3",
        },
    ]
    for i, body in enumerate(experiments):
        record(f"experiment{i}", client.post(f"{base}/experiments", json=body), 201)
    record(
        "budget_error",
        client.post(
            f"{base}/experiments",
            json={
                "instrument": "transient_capture",
                "probe": "output_voltage",
                "client_request_id": "iso-key-4",
            },
        ),
        409,
    )
    record(
        "validation_error",
        client.post(
            f"{base}/experiments",
            json={"instrument": "transient_capture", "client_request_id": "iso-key-5"},
        ),
        422,
    )
    record("idempotent_repeat", client.post(f"{base}/experiments", json=experiments[0]), 201)
    record(
        "mismatched_key",
        client.post(
            f"{base}/experiments",
            json={"instrument": "source_current_sweep", "client_request_id": "iso-key-1"},
        ),
        409,
    )
    symptom_id = case["observations"][0]["id"]
    record(
        "bad_citation",
        client.post(
            f"{base}/diagnosis",
            json={
                "conclusion": "fault",
                "hypothesis_id": "r1_increased",
                "explanation": "x",
                "citations": [{"observation_id": symptom_id, "quantity": "source_current_ma"}],
            },
        ),
        422,
    )
    record(
        "diagnosis",
        client.post(
            f"{base}/diagnosis",
            json={
                "conclusion": "insufficient_evidence",
                "candidate_hypothesis_ids": ["r1_increased", "c1_increased"],
                "explanation": "Both fit the voltage evidence.",
                "suggested_instrument": "source_current_sweep",
            },
        ),
        200,
    )
    record("repair_before_proposal", client.post(f"{base}/repair"), 409)
    record(
        "proposal",
        client.post(
            f"{base}/repair-proposal", json={"component_id": "R1", "rationale": "R1 drift"}
        ),
        200,
    )
    record("repair", client.post(f"{base}/repair"), 200)
    record("repair_twice", client.post(f"{base}/repair"), 409)
    record("get", client.get(base), 200)
    record("workbench", client.get("/api/workbench"), 200)
    flow.sim_calls = calls() - before
    # Artifact metadata: headers and the WAV header bytes (the PCM data is the measurement).
    urls = [clip["url"] for _, body in flow.payloads for clip in _clips(body)]
    for url in dict.fromkeys(urls):
        r = client.get(url)
        assert r.status_code == 200
        flow.artifact_meta.append(
            (
                r.headers["content-type"],
                r.headers.get("content-disposition", ""),
                len(r.content),
                r.content[:44],
            )
        )


def _clips(body: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(body, dict):
        if "clips" in body and isinstance(body["clips"], list):
            out += [c for c in body["clips"] if isinstance(c, dict) and "url" in c]
        for v in body.values():
            out += _clips(v)
    elif isinstance(body, list):
        for v in body:
            out += _clips(v)
    return out


def _public_db(settings: Settings) -> tuple[dict[str, dict[str, Any]], str]:
    """Public documents grouped by session → {kind: parsed JSON}, and the raw dump text."""
    con = sqlite3.connect(str(settings.db_path))
    try:
        docs = con.execute("SELECT kind, id, session_id, data FROM docs").fetchall()
        artifacts = con.execute("SELECT id, session_id, media_type, size FROM artifacts").fetchall()
    finally:
        con.close()
    grouped: dict[str, dict[str, Any]] = {}
    for kind, _doc_id, session_id, data in docs:
        by_kind = grouped.setdefault(session_id, {})
        assert kind not in by_kind, f"unexpected second {kind} document in a session"
        by_kind[kind] = json.loads(data)
    raw = "\n".join(" ".join(str(x) for x in row) for row in docs + artifacts)
    return grouped, raw


def _unexplained(
    flows: list[Flow],
    secrets: dict[str, Secrets],
    streams: dict[str, list[tuple[str, Any]]],
) -> tuple[list[str], int]:
    """Findings that are not fault-invariant coincidences. ``streams`` maps fault → aligned
    (label, payload) list. Returns (failure messages, number of invariant coincidences)."""
    failures: list[str] = []
    invariant = 0
    maps = {f: [leaf_map(body) for _, body in stream] for f, stream in streams.items()}
    for flow in flows:
        for index, (label, body) in enumerate(streams[flow.fault]):
            for finding in find_leaks(body, secrets[flow.fault], flow.ids):
                others = [
                    maps[o.fault][index].get(finding.path, object())
                    for o in flows
                    if o.fault != flow.fault
                ]
                if all(other == finding.value for other in others):
                    invariant += 1  # identical under every hidden fault: carries no information
                    continue
                failures.append(
                    f"LEAK seed={flow.seed} fault={flow.fault} payload={label} {finding}"
                )
    return failures, invariant


def _check_family(
    client: TestClient,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    flows: list[Flow],
    calls: Callable[[], int],
) -> None:
    caplog.set_level(logging.DEBUG)
    seeds = {f.fault: f.seed for f in flows}
    for flow in flows:
        run_flow(client, monkeypatch, flow, calls)
    intended = dict(flows[0].payloads)["create"]["intended_revision"]
    template_id: TemplateId = intended["template_id"]
    tones = [Tone.model_validate(t) for t in intended["source"]["tones"]]
    nominal_values = {
        c["id"]: c["value_si"] for c in intended["components"] if c["id"] in ("R1", "C1")
    }
    secrets: dict[str, Secrets] = {
        f.fault: secrets_for(f.seed, f.fault, nominal_values, template_id) for f in flows
    }
    labels = [label for label, _ in flows[0].payloads]
    for flow in flows:
        assert [label for label, _ in flow.payloads] == labels

    # 1) Structural scan of every public payload, before close.
    failures, invariant = _unexplained(flows, secrets, {f.fault: f.payloads for f in flows})
    assert not failures, f"seeds={seeds}\n" + "\n".join(failures[:20])

    # 2) Opaque IDs: exact new_id format and never shared between flows (not content-derived).
    for flow in flows:
        assert flow.ids, flow.fault
        assert all(OPAQUE_ID.fullmatch(i) for i in flow.ids)
    for a, b in combinations(flows, 2):
        assert not (a.ids & b.ids), (a.fault, b.fault, sorted(a.ids & b.ids)[:3])

    # 3) Evidence is a pure function of public data: recompute it from the payload.
    for flow in flows:
        final = dict(flow.payloads)["get"]
        observations = [Observation.model_validate(o) for o in final["observations"]]
        recomputed = hypotheses.assess(
            template_id, nominal_values, observations, publish_distinguishing=True, tones=tones
        )
        assert recomputed.model_dump(mode="json") == final["evidence"]

    # 4) Cross-session access is indistinguishable from unknown IDs.
    owner, intruder = flows[0], flows[1]
    _use(client, intruder.session)
    case_paths = [
        "",
        "/mode",
        "/experiments",
        "/diagnosis",
        "/repair-proposal",
        "/repair",
        "/close",
    ]
    bodies = {
        "/mode": {"mode": "agent"},
        "/experiments": {"instrument": "output_voltage_sweep", "client_request_id": "intruder-1"},
        "/diagnosis": {"conclusion": "insufficient_evidence", "explanation": "x"},
        "/repair-proposal": {"component_id": "R1"},
    }
    for suffix in case_paths:
        method = client.get if suffix == "" else client.post
        kwargs = {"json": bodies[suffix]} if suffix in bodies else {}
        theirs = method(f"/api/cases/{owner.case_id}{suffix}", **kwargs)
        missing = method(f"/api/cases/{UNKNOWN_CASE}{suffix}", **kwargs)
        assert theirs.status_code == missing.status_code == 404, suffix
        assert theirs.json() == missing.json(), suffix
    owner_clip = _clips(dict(owner.payloads)["create"])[0]["url"]
    assert client.get(owner_clip).status_code == 404
    assert client.get(owner_clip).json() == client.get("/api/artifacts/art_unknownXXXXX").json()

    # 5) Artifact metadata and WAV headers do not depend on the fault.
    assert len({tuple(f.artifact_meta) for f in flows}) == 1, "artifact headers/sizes differ"

    # 6) The same simulation work for every fault (no shortcuts or hidden-run caching, V-04).
    assert len({f.sim_calls for f in flows}) == 1, [f.sim_calls for f in flows]

    # 7) Differential: same nominal design, different hidden faults → identical skeletons.
    reference = [(label, skeleton(body)) for label, body in flows[0].payloads]
    for flow in flows[1:]:
        for (label, a), (_, b) in zip(
            reference, [(lb, skeleton(bd)) for lb, bd in flow.payloads], strict=True
        ):
            assert a == b, (flow.fault, label, seeds)

    # 8) App logs and the public database hold no hidden values, seeds, or hidden-field names.
    app_log = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("resonance"))
    db_docs, dump = _public_db(settings)
    db_streams = {f.fault: sorted(db_docs[f.session].items()) for f in flows}
    for flow in flows:
        assert not raw_text_leaks(app_log, secrets[flow.fault]), (flow.fault, flow.seed)
        assert flow.seed not in dump and flow.seed not in app_log
    db_failures, _ = _unexplained(flows, secrets, db_streams)
    assert not db_failures, f"seeds={seeds}\n" + "\n".join(db_failures[:20])
    for hidden_key in ('"seed"', '"actual_value_si"', '"generator_version"', '"fault"'):
        assert hidden_key not in dump
    assert settings.evaluator_db_path.resolve() != settings.db_path.resolve()
    assert settings.evaluator_db_path.is_file() and settings.db_path.is_file()
    con = sqlite3.connect(str(settings.evaluator_db_path))
    try:
        stored = {row[0] for row in con.execute("SELECT seed FROM hidden_cases")}
    finally:
        con.close()
    assert set(seeds.values()) <= stored
    state_values = vars(client.app.state)["_state"].values()  # type: ignore[attr-defined]
    assert not any(isinstance(v, Evaluator | EvaluatorStore) for v in state_values)

    # 9) Positive control: after close, the reveal carries exactly what the scan looks for.
    for flow in flows:
        _use(client, flow.session)
        assert dict(flow.payloads)["get"]["reveal"] is None
        closed = client.post(f"/api/cases/{flow.case_id}/close")
        assert closed.status_code == 200
        body = closed.json()
        assert body["reveal"]["seed"] == flow.seed
        assert body["reveal"]["fault"]["hypothesis_id"] == flow.fault
        if flow.fault != "no_fault":
            assert find_leaks(body["reveal"], secrets[flow.fault])
    print(f"isolation family seeds={seeds} fault-invariant coincidences={invariant}")


def _random_family() -> list[Flow]:
    template_id: TemplateId = os.environ.get(FAMILY_ENV) or draw_family()  # type: ignore[assignment]
    seed = os.environ.get(SEED_ENV) or faults.new_seed()
    design = faults.generate(seed, template_id)
    nominal = (design.nominal["R1"], design.nominal["C1"])
    return family(template_id, nominal, override=(design.fault.hypothesis_id, seed))


@pytest.mark.parametrize("which", [*NOMINALS, "random-seed"])
def test_no_hidden_state_reaches_any_public_surface(
    app_client_factory: Callable[[Simulator], TestClient],
    double: AnalyticTestSimulator,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    which: str,
) -> None:
    flows = _random_family() if which == "random-seed" else family(*NOMINALS[which])
    client = app_client_factory(double)
    _check_family(client, settings, monkeypatch, caplog, flows, lambda: len(double.calls))


@pytest.mark.ngspice
@pytest.mark.parametrize("which", ["lowpass-10k-10n", "highpass-10k-10n"])
def test_no_hidden_state_reaches_any_public_surface_ngspice(
    app_client_factory: Callable[[Simulator], TestClient],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    which: str,
) -> None:
    simulator = create_simulator(settings)
    counter = {"n": 0}
    real_ac, real_tran = simulator.ac, simulator.transient

    async def ac(*args: Any, **kwargs: Any) -> Any:
        counter["n"] += 1
        return await real_ac(*args, **kwargs)

    async def transient(*args: Any, **kwargs: Any) -> Any:
        counter["n"] += 1
        return await real_tran(*args, **kwargs)

    simulator.ac = ac  # type: ignore[method-assign]
    simulator.transient = transient  # type: ignore[method-assign]
    client = app_client_factory(simulator)
    flows = family(*NOMINALS[which])
    _check_family(client, settings, monkeypatch, caplog, flows, lambda: counter["n"])


def test_scanner_ignores_random_ids_but_not_spelled_values() -> None:
    """Regression for the intermittent failure: a random ID that happens to end in "5k"."""
    nominal = {"R1": 10_000.0, "C1": 10e-9}
    secrets = secrets_for(seed_for("r1_decreased", (10_000.0, 10e-9)), "r1_decreased", nominal)
    unlucky = "obs_-d1pJ3EANkm4VM5k"  # a real new_id() output observed while measuring
    assert raw_text_leaks.__module__  # the old raw scan matched inside such IDs:
    assert re.search(r"(?<![0-9.])5k(?![0-9.A-Za-zΩ])", unlucky)
    ids: set[str] = set()
    clean = {"id": unlucky, "summary": f"Observation {unlucky} (tone_gain_db) differs."}
    assert find_leaks(clean, secrets, ids) == [] and ids == {unlucky}
    leaky = {"summary": f"Observation {unlucky}: R1 is 5k now", "x": 5000.0}
    assert {f.path for f in find_leaks(leaky, secrets)} == {"summary", "x"}


def test_challenge_code_never_builds_ids_itself() -> None:
    """IDs must come only from contracts.common.new_id (random), never from content."""
    root = Path(__file__).resolve().parents[2] / "resonance" / "challenges"
    pattern = re.compile(r"""f?["'](case|obs|rev|art|cmp|dx|rep|run|goal|exp|prop)_["'{]""")
    offenders = [
        f"{p.name}:{n}"
        for p in root.rglob("*.py")
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert offenders == []
