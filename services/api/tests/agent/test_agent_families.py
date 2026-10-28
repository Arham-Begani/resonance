"""Milestone 2 for case runs: both circuit families, the open-capacitor hypothesis, honest
rendering of readings at the declared measurement floor, prompt contents, and the CaseService
port typing. The model is the TEST-ONLY scripted provider throughout; no live inference."""

from __future__ import annotations

import inspect
import json
import os
import re
import typing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from agent_testkit import (
    SESSION,
    FakeCaseService,
    ScriptedProvider,
    completion,
    make_service,
    make_settings,
    wait_finished,
)

from resonance.agent import prompts, views
from resonance.agent.ports import CaseServicePort
from resonance.agent.service import AgentService
from resonance.challenges.evaluator import Evaluator, EvaluatorStore, faults
from resonance.challenges.service import CaseService
from resonance.config import resolve_ngspice_bin
from resonance.contracts.agent import CaseAgentRunRequest
from resonance.contracts.circuits import TemplateId
from resonance.contracts.common import SimulatorInfo, new_id
from resonance.contracts.investigation import (
    Observation,
    ObservationSettings,
    ObservedTone,
    ObservedWaveform,
    RepairProposal,
    RepairVerification,
    SweepObservation,
    TransientObservation,
    VerificationCriteria,
    VerificationPoint,
)
from resonance.contracts.policy import DB_FLOOR, TEMPLATE_TONES
from resonance.simulation import analytic, create_simulator
from resonance.store.artifacts import ArtifactStore
from resonance.store.db import DocumentStore

GRID = [20.0 * 10 ** (k / 10) for k in range(31)]
NOMINAL = {"R1": 1000.0, "C1": 1e-7}
INSTRUMENT_KINDS = ("output_voltage_sweep", "source_current_sweep", "transient_capture")
FAMILY_TOPOLOGY = {
    "rc_lowpass": ("RC low-pass filter", "a resistor (R1) from Input to Output"),
    "rc_highpass": ("RC high-pass filter", "a capacitor (C1) from Input to Output"),
}


def _obs(**fields: Any) -> Observation:
    return Observation(
        id=new_id("obs"),
        case_id=new_id("case"),
        sequence=1,
        counted=True,
        requested_by="agent",
        status="succeeded",
        created_at=datetime.now(UTC),
        simulator=SimulatorInfo(version="47"),
        settings=ObservationSettings(),
        summary="Deterministic summary.",
        **fields,
    )


def _sweep_obs(
    quantity: str, magnitude: list[float], phase: list[float], template_id: TemplateId
) -> Observation:
    """A sweep observation whose reference columns come from the nominal intended design."""
    if quantity == "output_gain":
        e = analytic.transfer(template_id, GRID, NOMINAL)
        expected = [float(x) for x in analytic.gain_db(e)]
        unit = "dB"
    else:
        e = analytic.source_current(template_id, GRID, NOMINAL)
        expected = [float(abs(x)) * 1000 for x in e]
        unit = "mA"
    sweep = SweepObservation(
        quantity=quantity,  # type: ignore[arg-type]
        frequency_hz=GRID,
        magnitude=magnitude,
        magnitude_unit=unit,  # type: ignore[arg-type]
        phase_deg=phase,
        expected_magnitude=expected,
        expected_phase_deg=[float(x) for x in analytic.phase_deg(e)],
    )
    kind = "output_voltage_sweep" if quantity == "output_gain" else "source_current_sweep"
    return _obs(kind=kind, sweep=sweep)


def _transient_obs(probe: str, template_id: TemplateId, *, silent: bool) -> Observation:
    """A two-tone capture; ``silent`` publishes what the evaluator publishes for a true zero:
    0 amplitude, gains clamped at policy.DB_FLOOR, and the (undefined) phase as 0°."""
    tones = []
    for tone in TEMPLATE_TONES[template_id]:
        f, amp = tone.frequency_hz, tone.amplitude_v
        h = analytic.transfer(template_id, f, NOMINAL)
        i = analytic.source_current(template_id, f, NOMINAL, amp)
        voltage = probe == "output_voltage"
        expected_amp = float(abs(h)) * amp if voltage else float(abs(i)) * 1000
        tones.append(
            ObservedTone(
                frequency_hz=f,
                label=tone.label,
                role=tone.role,
                amplitude=0.0 if silent else expected_amp,
                amplitude_unit="V" if voltage else "mA",
                gain_db=(DB_FLOOR if silent else float(analytic.gain_db(h))) if voltage else None,
                phase_deg=0.0 if silent else float(analytic.phase_deg(h if voltage else i)),
                expected_amplitude=expected_amp,
                expected_gain_db=float(analytic.gain_db(h)) if voltage else None,
                expected_phase_deg=float(analytic.phase_deg(h if voltage else i)),
            )
        )
    waveform = ObservedWaveform(
        sample_rate_hz=48_000,
        start_s=0.0,
        unit="V" if probe == "output_voltage" else "mA",
        input_v=[0.1] * 4,
        observed=[0.0] * 4,
        expected=[0.1] * 4,
    )
    transient = TransientObservation(probe=probe, tones=tones, waveform=waveform)  # type: ignore[arg-type]
    return _obs(kind="transient_capture", probe=probe, transient=transient)


def _shape(node: Any) -> Any:
    """Structure only: keys and list lengths (scalar values are erased)."""
    if isinstance(node, dict):
        return {k: _shape(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_shape(v) for v in node]
    return None


# --------------------------------------------------------------------------------------
# Views: family/topology text and the hypothesis catalog
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("template_id", ["rc_lowpass", "rc_highpass"])
def test_case_view_describes_the_family_topology_and_catalog(template_id: TemplateId) -> None:
    cases = FakeCaseService(SESSION, mode="agent", template_id=template_id)
    view = views.case_view(cases.public(), run_actions=["run_experiment"])
    design = view["intended_design"]
    title, topology = FAMILY_TOPOLOGY[template_id]
    assert design["template_id"] == template_id and design["title"] == title
    assert title in design["label"] and topology in design["label"]
    assert "the hidden circuit may differ" in design["label"]
    hypotheses = {h["id"]: h["label"] for h in view["hypotheses"]}
    assert hypotheses["c1_disconnected"] == "C1 is disconnected (open)"
    assert set(hypotheses) == {
        "no_fault",
        "r1_increased",
        "r1_decreased",
        "c1_increased",
        "c1_decreased",
        "c1_disconnected",
    }
    tones = [t["frequency_hz"] for t in design["source"]["test_tones"]]
    assert tones == [t.frequency_hz for t in TEMPLATE_TONES[template_id]]


# --------------------------------------------------------------------------------------
# Views: readings at the measurement floor are never shown as numbers
# --------------------------------------------------------------------------------------


def test_highpass_open_capacitor_sweeps_render_floors_honestly() -> None:
    silent_gain = _sweep_obs("output_gain", [DB_FLOOR] * 31, [0.0] * 31, "rc_highpass")
    silent_current = _sweep_obs("source_current", [0.0] * 31, [0.0] * 31, "rc_highpass")
    for obs in (silent_gain, silent_current):
        view = views.observation_view(obs)
        sweep = view["sweep"]
        assert len(sweep["rows"]) == 31
        for f, measured, expected, phase, expected_phase in sweep["rows"]:
            assert isinstance(f, float)
            assert measured == views.BELOW_FLOOR and phase == views.UNDEFINED_PHASE
            # The intended design's reference is a genuine, non-zero response.
            assert isinstance(expected, float) and isinstance(expected_phase, float)
        assert views.BELOW_FLOOR in sweep["reference"] and "-120 dB" in sweep["reference"]
        text = json.dumps(view, allow_nan=False)
        assert not re.search(r"-200(\.0)?\b", text), "the dB clamp is not a measurement"


def test_lowpass_open_capacitor_keeps_measurable_output_and_floors_current() -> None:
    # Low-pass with C1 open: the output follows the input (0 dB, 0° are real readings), but
    # no current flows (no load), so the current reading and its phase are not numbers.
    passthrough = _sweep_obs("output_gain", [0.0] * 31, [0.0] * 31, "rc_lowpass")
    rows = views.observation_view(passthrough)["sweep"]["rows"]
    assert all(row[1] == 0.0 and row[3] == 0.0 for row in rows)
    no_current = _sweep_obs("source_current", [0.0] * 31, [0.0] * 31, "rc_lowpass")
    rows = views.observation_view(no_current)["sweep"]["rows"]
    assert all(row[1] == views.BELOW_FLOOR and row[3] == views.UNDEFINED_PHASE for row in rows)


@pytest.mark.parametrize("probe", ["output_voltage", "source_current"])
def test_silent_tone_captures_render_floors_honestly(probe: str) -> None:
    view = views.observation_view(_transient_obs(probe, "rc_highpass", silent=True))
    transient = view["transient"]
    assert views.BELOW_FLOOR in transient["note"]
    for tone in transient["tones"]:
        assert tone["amplitude"] == views.BELOW_FLOOR
        assert tone["phase_deg"] == views.UNDEFINED_PHASE
        assert isinstance(tone["expected_amplitude"], float)
        assert isinstance(tone["expected_phase_deg"], float)
        if probe == "output_voltage":
            assert tone["gain_db"] == views.BELOW_FLOOR
            assert isinstance(tone["expected_gain_db"], float)
        else:
            assert "gain_db" not in tone
    assert not re.search(r"-200(\.0)?\b", json.dumps(view, allow_nan=False))


def test_floor_rendering_adds_no_structural_side_channel() -> None:
    """A silent and a normal reading differ only in measured cell values, never in keys or
    lengths (the verification differential masks exactly those cells)."""
    pairs = [
        (
            _sweep_obs("output_gain", [DB_FLOOR] * 31, [0.0] * 31, "rc_highpass"),
            _sweep_obs("output_gain", [-3.0] * 31, [10.0] * 31, "rc_highpass"),
        ),
        (
            _transient_obs("output_voltage", "rc_highpass", silent=True),
            _transient_obs("output_voltage", "rc_highpass", silent=False),
        ),
        (
            _transient_obs("source_current", "rc_lowpass", silent=True),
            _transient_obs("source_current", "rc_lowpass", silent=False),
        ),
    ]
    for silent, normal in pairs:
        a, b = views.observation_view(silent), views.observation_view(normal)
        assert _shape(a) == _shape(b)
        part = "sweep" if "sweep" in a else "transient"
        texts = ("reference", "note") if part == "transient" else ("reference",)
        for key in texts:
            assert a[part][key] == b[part][key], "explanatory text is constant"


def test_repair_without_measurable_output_reports_no_deviation_number() -> None:
    cases = FakeCaseService(SESSION, mode="agent", template_id="rc_highpass")
    proposal = RepairProposal(
        id=new_id("rep"),
        component_id="R1",
        value_si=1000.0,
        display_value="1 kΩ",
        rationale="",
        proposed_by="user",
        created_at=datetime.now(UTC),
    )
    points = [
        VerificationPoint(
            frequency_hz=f,
            repaired_gain_db=DB_FLOOR,
            expected_gain_db=-3.0,
            repaired_phase_deg=0.0,
            expected_phase_deg=45.0,
            within_tolerance=False,
        )
        for f in GRID
    ]
    repair = RepairVerification(
        id=proposal.id,
        proposal=proposal,
        status="succeeded",
        criteria=VerificationCriteria(
            frequency_hz=GRID, gain_tolerance_db=0.5, phase_tolerance_deg=3.0, description="x"
        ),
        functional_recovery=False,
        max_gain_deviation_db=197.0,
        max_phase_deviation_deg=0.0,
        worst_frequency_hz=20.0,
        points=points,
        summary="Still no measurable output.",
    )
    public = cases.public().model_copy(update={"repair": repair})
    shown = views.case_view(public)["repair_verification"]
    assert shown["max_gain_deviation_db"].startswith("not defined: no measurable output at 31")
    assert shown["functional_recovery"] is False


# --------------------------------------------------------------------------------------
# Prompts: family, catalog and floor rule from trusted data; no solving strategy
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["investigate", "hint"])
@pytest.mark.parametrize("template_id", ["rc_lowpass", "rc_highpass"])
async def test_case_prompts_name_the_family_and_catalog_but_no_strategy(
    settings, store, kind: str, template_id: TemplateId
) -> None:
    mode = "agent" if kind == "investigate" else "guide"
    cases = FakeCaseService(SESSION, mode=mode, template_id=template_id)  # type: ignore[arg-type]
    provider = ScriptedProvider(
        [
            completion("Prediction: I need more evidence."),
            completion("Still undecided."),  # investigate: ignores the one diagnosis reminder
        ]
    )
    service = make_service(settings, store, provider, cases)
    run = await service.start_case_run(
        SESSION,
        cases.case_id,
        CaseAgentRunRequest(kind=kind),  # type: ignore[arg-type]
    )
    await wait_finished(service, run.id)
    system = provider.requests[0]["messages"][0]["content"]
    title, topology = FAMILY_TOPOLOGY[template_id]
    assert f"The intended design is an {title}: {topology}" in system
    assert "- c1_disconnected: C1 is disconnected (open)" in system
    assert "- r1_increased: R1 is about twice its nominal value" in system
    assert f'"{views.BELOW_FLOOR}"' in system and f'"{views.UNDEFINED_PHASE}"' in system
    # No investigative strategy: the prompt never names an instrument (the model chooses
    # from get_case's instrument descriptions, as the baseline comparison requires).
    lowered = system.lower()
    for name in INSTRUMENT_KINDS:
        assert name not in lowered
    for phrase in ("current sweep", "voltage sweep", "source current", "two-tone capture"):
        assert phrase not in lowered
    other = next(t for t in FAMILY_TOPOLOGY if t != template_id)
    assert FAMILY_TOPOLOGY[other][0] not in system, "only the case's own family is described"


# --------------------------------------------------------------------------------------
# Port typing: the agent's CaseServicePort matches the real CaseService
# --------------------------------------------------------------------------------------


def _port_methods() -> list[str]:
    return [
        name
        for name, member in vars(CaseServicePort).items()
        if inspect.isfunction(member) and not name.startswith("_")
    ]


def test_case_service_port_matches_the_real_case_service() -> None:
    """mypy also checks this statically (``ports._case_service_conforms``); this runtime check
    compares parameter names, kinds, defaults, async-ness and resolved type hints."""
    names = _port_methods()
    assert "claim_for_agent" in names and len(names) >= 10
    for name in names:
        port_fn = getattr(CaseServicePort, name)
        real_fn = getattr(CaseService, name)
        assert inspect.iscoroutinefunction(port_fn) == inspect.iscoroutinefunction(real_fn), name
        port_sig, real_sig = inspect.signature(port_fn), inspect.signature(real_fn)
        assert [(p.name, p.kind, p.default) for p in port_sig.parameters.values()] == [
            (p.name, p.kind, p.default) for p in real_sig.parameters.values()
        ], name
        assert typing.get_type_hints(port_fn) == typing.get_type_hints(real_fn), name
    kind = typing.get_type_hints(CaseServicePort.claim_for_agent)["kind"]
    assert set(typing.get_args(typing.get_args(kind)[0])) == {"investigate", "hint"}


# --------------------------------------------------------------------------------------
# Real CaseService + evaluator + ngspice: an open-capacitor high-pass case (provider scripted)
# --------------------------------------------------------------------------------------


def _seed(template_id: TemplateId, condition: str) -> str:
    """TEST SETUP ONLY: a seed whose hidden case has ``condition`` (ground truth for the
    assertions; the agent package never imports the evaluator)."""
    for i in range(10_000):
        seed = f"agent-m2-{template_id}-{condition}-{i}"
        if faults.generate(seed, template_id).fault.hypothesis_id == condition:
            return seed
    raise AssertionError("no seed found")


def _tool_results(provider: ScriptedProvider) -> list[dict[str, Any]]:
    last = provider.requests[-1]["messages"]
    return [json.loads(m["content"]) for m in last if m["role"] == "tool"]


@pytest.mark.ngspice
async def test_real_open_capacitor_highpass_case_reaches_the_model_honestly(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    if resolve_ngspice_bin(settings) is None and not os.environ.get("RESONANCE_REQUIRE_NGSPICE"):
        pytest.skip("ngspice is not available")
    store = DocumentStore(settings.db_path)
    artifacts = ArtifactStore(settings.artifacts_dir, store, max_count=200, max_bytes=100 << 20)
    evaluator = Evaluator(
        EvaluatorStore(settings.evaluator_db_path), create_simulator(settings), artifacts
    )
    cases = CaseService(store, evaluator)

    def diagnose(body: dict[str, Any]) -> dict[str, Any]:
        current = next(
            r["observation_id"]
            for r in (json.loads(m["content"]) for m in body["messages"] if m["role"] == "tool")
            if r.get("kind") == "source_current_sweep"
        )
        args = {
            "conclusion": "fault",
            "hypothesis_id": "c1_disconnected",
            "citations": [{"observation_id": current, "quantity": "source_current_ma"}],
            "explanation": "No measurable source current at any frequency.",
        }
        return completion(None, [("submit_diagnosis", args)])

    provider = ScriptedProvider(
        [
            completion(None, [("get_case", {})]),
            completion(None, [("run_experiment", {"instrument": "output_voltage_sweep"})]),
            completion(None, [("run_experiment", {"instrument": "source_current_sweep"})]),
            # Finishes without a diagnosis -> one diagnosis reminder from the REAL case state.
            completion("Prediction: nothing measurable reaches the output."),
            diagnose,
            # Fault diagnosed without a repair -> one repair reminder.
            completion("C1 behaves as if it were open."),
            completion(None, [("propose_repair", {"component_id": "C1"})]),
            completion("I proposed replacing C1 with its intended part."),
        ]
    )
    service = AgentService(
        settings=settings,
        store=store,
        cases=cases,
        explore=None,
        client_factory=provider.client_factory(),
    )
    cases.register_run_canceller(service.cancel_and_wait)
    cases.register_run_liveness(service.is_live)
    seed = _seed("rc_highpass", "c1_disconnected")
    try:
        case = await cases.harness_create_case(SESSION, seed, "rc_highpass")
        run = await service.start_case_run(
            SESSION, case.id, CaseAgentRunRequest(kind="investigate")
        )
        await wait_finished(service, run.id, limit_s=120)
        assert service.get_run(SESSION, run.id).status == "completed"
        assert provider.steps == [] and len(provider.requests) == 8
        # The real CaseService offered submit_diagnosis, then propose_repair, to this run; the
        # open capacitor is repaired by replacing C1 (the intended nominal part).
        public = cases.get_case(SESSION, case.id)
        assert public.diagnosis is not None and public.diagnosis.agent_run_id == run.id
        assert public.repair_proposal is not None and public.repair_proposal.component_id == "C1"
        reminders = [
            m["content"]
            for m in provider.requests[-1]["messages"]
            if m["role"] == "user" and m["content"].startswith("Note from the Resonance")
        ]
        assert reminders == [prompts.DIAGNOSIS_REMINDER, prompts.REPAIR_REMINDER]

        system = provider.requests[0]["messages"][0]["content"]
        assert "RC high-pass filter: a capacitor (C1) from Input to Output" in system
        get_case, ovs, scs, *_ = _tool_results(provider)
        assert get_case["intended_design"]["template_id"] == "rc_highpass"
        assert "c1_disconnected" in {h["id"] for h in get_case["hypotheses"]}
        symptom = get_case["observations"][0]
        for tone in symptom["transient"]["tones"]:  # nothing reaches the output
            assert tone["gain_db"] == views.BELOW_FLOOR and tone["amplitude"] == views.BELOW_FLOOR
            assert tone["phase_deg"] == views.UNDEFINED_PHASE
            assert isinstance(tone["expected_gain_db"], float)
        for result in (ovs, scs):
            for row in result["sweep"]["rows"]:
                assert row[1] == views.BELOW_FLOOR and row[3] == views.UNDEFINED_PHASE
                assert isinstance(row[2], float) and isinstance(row[4], float)
        sent = json.dumps(provider.requests)
        assert not re.search(r"-200(\.0)?\b", sent), "the dB clamp never reaches the model"
        assert seed not in sent

        closed = await cases.close_case(SESSION, case.id)
        assert closed.reveal is not None
        assert closed.reveal.fault.hypothesis_id == "c1_disconnected"  # setup sanity
    finally:
        await service.shutdown()
        cases.close()
        store.close()
