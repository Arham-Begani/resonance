"""Finding A-01: measured values leave the evaluator only at declared resolutions.

Without quantization, ngspice round-off (~1e-15 relative) differs between voltage-equivalent
hidden circuits (R1×0.5 vs C1×0.5 for nominal C1 ∈ {150 nF, 470 nF}: 15 of 83 designs), and an
Explore run of either candidate reproduces it bit for bit, identifying the drifted component
from the free symptom. These tests check (1) every published measurement sits on its declared
grid, and (2) on REAL ngspice the voltage-only payloads of the R- and C-variant are
byte-identical (IDs, timestamps, and artifact IDs aside), including the observed WAV data.
"""

from __future__ import annotations

import json
import math
import os
import re
from typing import Any

import pytest
from challenges_testkit import SESSION_A, Rig, iter_leaves, seed_for

from resonance.challenges.evaluator import faults
from resonance.challenges.evaluator.quantize import (
    QUANT_AMPLITUDE_SIG_FIGS,
    QUANT_PEAK_FS_DECIMALS,
    QUANT_WAVEFORM_SIG_FIGS,
    q_db,
    q_phase,
    q_sig,
)
from resonance.contracts.investigation import (
    CaseExperimentRequest,
    DiagnosisSubmission,
    PublicCase,
    RepairProposalInput,
)

FINGERPRINTABLE = [d for d in faults.nominal_designs() if d[1] in (150e-9, 470e-9)]
REPRESENTATIVE = [(470.0, 150e-9), (1000.0, 150e-9), (3300.0, 470e-9), (10_000.0, 150e-9)]

_DB_KEY = re.compile(r"(gain_db|gain_adjustment_db)$|sweep\.(magnitude|expected_magnitude)\[")
_PHASE_KEY = re.compile(r"phase_deg(\[\d+\])?$")
_AMPLITUDE_KEY = re.compile(r"\.(amplitude|expected_amplitude)$")
_WAVE_KEY = re.compile(r"waveform\.(input_v|observed|expected)\[\d+\]$")


def _on_grid(path: str, value: float, sweep_unit: str | None) -> bool:
    if path.endswith("peak_fs"):
        return value == round(value, QUANT_PEAK_FS_DECIMALS)
    if _WAVE_KEY.search(path):
        return value == q_sig(value, QUANT_WAVEFORM_SIG_FIGS)
    if _PHASE_KEY.search(path):
        return value == q_phase(value)
    if _AMPLITUDE_KEY.search(path):
        return value == q_sig(value, QUANT_AMPLITUDE_SIG_FIGS)
    if "sweep.magnitude" in path or "sweep.expected_magnitude" in path:
        return value == (q_db(value) if sweep_unit == "dB" else q_sig(value))
    if _DB_KEY.search(path):
        return value == q_db(value)
    return True


def _measured_leaves(case: PublicCase) -> list[tuple[str, float, str | None]]:
    out: list[tuple[str, float, str | None]] = []
    payload = case.model_dump(mode="json")
    for obs in payload["observations"]:
        unit = obs["sweep"]["magnitude_unit"] if obs.get("sweep") else None
        for path, leaf in iter_leaves(obs):
            if isinstance(leaf, float):
                out.append((path, leaf, unit))
    repair = payload.get("repair") or {}
    for path, leaf in iter_leaves(repair):
        if isinstance(leaf, float) and ("repaired" in path or "tones" in path or "peak" in path):
            out.append((f"repair.{path}", leaf, None))
    return out


async def test_every_published_measurement_is_on_its_declared_grid(rig: Rig) -> None:
    case = await rig.service.harness_create_case(SESSION_A, seed_for("r1_decreased"))
    await rig.service.set_mode(SESSION_A, case.id, "guide")
    for i, (instrument, probe) in enumerate(
        [("output_voltage_sweep", None), ("source_current_sweep", None),
         ("transient_capture", "source_current")]
    ):
        await rig.service.run_experiment(
            SESSION_A, case.id,
            CaseExperimentRequest.model_validate(
                {"instrument": instrument, "probe": probe, "client_request_id": f"grid-{i:05d}"}
            ),
            requested_by="user",
        )
    await rig.service.submit_diagnosis(
        SESSION_A, case.id,
        DiagnosisSubmission.model_validate({
            "conclusion": "insufficient_evidence", "explanation": "x",
            "candidate_hypothesis_ids": ["r1_decreased", "c1_decreased"]}),
        submitted_by="user",
    )
    await rig.service.propose_repair(SESSION_A, case.id, RepairProposalInput(component_id="R1"),
                                     proposed_by="user")
    case = await rig.service.verify_repair(SESSION_A, case.id)
    leaves = _measured_leaves(case)
    assert len(leaves) > 1000
    off_grid = [(p, v) for p, v, unit in leaves if not _on_grid(p, v, unit)]
    assert off_grid == [], off_grid[:5]
    assert all(math.isfinite(v) for _, v, _ in leaves)


def _strip(payload: Any) -> Any:
    """Drop IDs, timestamps, and artifact references (random per case, not measurements)."""
    if isinstance(payload, dict):
        return {
            k: _strip(v)
            for k, v in payload.items()
            if k not in (
                "id", "case_id", "observation_id", "created_at", "artifact_id", "url", "source_id"
            )
        }
    if isinstance(payload, list):
        return [_strip(v) for v in payload]
    return payload


async def _voltage_payload(rig: Rig, fault: str, nominal: tuple[float, float], i: int) -> (
    tuple[str, list[bytes]]
):
    session = f"{SESSION_A}q{i}"
    case = await rig.service.harness_create_case(session, seed_for(fault, nominal))  # type: ignore[arg-type]
    await rig.service.set_mode(session, case.id, "guide")
    resp = await rig.service.run_experiment(
        session, case.id,
        CaseExperimentRequest(instrument="output_voltage_sweep", client_request_id="volt-00001"),  # type: ignore[arg-type]
        requested_by="user",
    )
    final = resp.case
    voltage_only = {
        "observations": [_strip(o.model_dump(mode="json")) for o in final.observations],
        "evidence": _strip(final.evidence.model_dump(mode="json")),
    }
    wavs: list[bytes] = []
    symptom = final.observations[0].transient
    assert symptom is not None and symptom.audio is not None
    for clip in symptom.audio.clips:
        opened = rig.artifacts.open(clip.artifact_id, session)
        assert opened is not None
        wavs.append(opened[0].read_bytes())
    await rig.service.close_case(session, case.id)
    return json.dumps(voltage_only, sort_keys=True), wavs


@pytest.mark.ngspice
@pytest.mark.parametrize(
    "nominal",
    FINGERPRINTABLE if os.environ.get("RESONANCE_A01_ALL_DESIGNS") == "1" else REPRESENTATIVE,
)
async def test_voltage_equivalent_drifts_publish_byte_identical_payloads(
    ngspice_rig: Rig, nominal: tuple[float, float]
) -> None:
    assert nominal in FINGERPRINTABLE
    r_json, r_wavs = await _voltage_payload(ngspice_rig, "r1_decreased", nominal, 0)
    c_json, c_wavs = await _voltage_payload(ngspice_rig, "c1_decreased", nominal, 1)
    assert r_json == c_json  # symptom, output sweep, evidence: byte-identical
    assert r_wavs == c_wavs  # expected and observed clips: identical PCM
