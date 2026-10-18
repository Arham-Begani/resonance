"""Trusted netlist compilation: only fixed tokens and validated numbers reach ngspice."""

from __future__ import annotations

import math
import re
from pathlib import Path

import pytest

from resonance.api.errors import ApiException
from resonance.circuits.netlist import (
    OUTPUT_FILE,
    TITLE,
    NetlistError,
    compile_ac,
    compile_transient,
)
from resonance.circuits.revisions import build_revision, create_revision, revision_to_circuit
from resonance.contracts.circuits import RevisionCreateRequest, SourceSpec, Tone
from resonance.contracts.internal import AcPoints, AcSweep, CircuitSpec, TransientRequest
from resonance.store.db import DocumentStore

_NUMBER = r"-?\d+(\.\d+)?(e[+-]?\d+)?"
_ALLOWED_LINES = [
    re.escape(TITLE),
    rf"V1 in 0 DC 0 AC {_NUMBER}",
    rf"V1[abcd] (in|v1_m\d) (v1_m\d|0) DC 0 SIN\(0 {_NUMBER} {_NUMBER} 0 0 0\)",
    rf"R1 in out {_NUMBER}",
    rf"C1 out 0 {_NUMBER}",
    r"\.control",
    r"set filetype=binary",
    r"set appendwrite",
    rf"ac dec \d+ {_NUMBER} {_NUMBER}",
    rf"ac lin 1 {_NUMBER} {_NUMBER}",
    rf"tran {_NUMBER} {_NUMBER} 0 {_NUMBER}",
    rf"write {re.escape(OUTPUT_FILE)}( (v\(in\)|v\(out\)|i\(V1a?\)))+",
    r"\.endc",
    r"\.end",
]
_LINE_RE = re.compile("^(" + "|".join(f"(?:{p})" for p in _ALLOWED_LINES) + ")$")


def _circuit(**values: float) -> CircuitSpec:
    rev = build_revision("rc_lowpass", values, origin="user_edit")
    return revision_to_circuit(rev)


def _assert_only_trusted_lines(text: str) -> None:
    assert text.isascii()
    for line in text.splitlines():
        assert _LINE_RE.fullmatch(line), f"unexpected netlist line: {line!r}"


def test_ac_and_transient_netlists_contain_only_fixed_tokens_and_numbers() -> None:
    circuit = _circuit(R1=4700.0, C1=33e-9)
    for compiled in (
        compile_ac(circuit, AcSweep(10.0, 100e3, 40)),
        compile_ac(circuit, AcPoints((440.0, 4000.0), probes=("v(out)",))),
        compile_transient(circuit, TransientRequest(1.4, 5e-6, ("v(in)", "v(out)", "i(V1)"))),
    ):
        _assert_only_trusted_lines(compiled.text)
        assert "include" not in compiled.text.lower() and "shell" not in compiled.text.lower()
        # write lists exactly the requested probes (never a bare 'write' that dumps everything)
        writes = [ln for ln in compiled.text.splitlines() if ln.startswith("write")]
        assert writes and all(len(ln.split()) == 2 + len(compiled.probes) for ln in writes)


def test_requested_probes_only_and_internal_nodes_stay_internal() -> None:
    circuit = _circuit()
    ac = compile_ac(circuit, AcSweep(20.0, 20e3, 10, probes=("i(V1)",)))
    assert ac.raw_variables == ("frequency", "i(v1)")
    assert "v(out)" not in ac.text.split(".control")[1]
    tr = compile_transient(circuit, TransientRequest(0.1, 5e-6, ("v(out)", "i(V1)")))
    assert tr.raw_variables == ("time", "v(out)", "i(v1a)")
    assert "V1a in v1_m1" in tr.text and "V1b v1_m1 0" in tr.text


@pytest.mark.parametrize(
    "smuggled",
    [
        "1k\n.control\nshell calc\n.endc",
        "1k\n.include /etc/passwd",
        "1k; shell rm -rf /",
        "1k .option",
        "{1k}",
        "1k\r\nV9 in 0 1",
    ],
)
def test_text_smuggling_is_rejected_before_compilation(tmp_path: Path, smuggled: str) -> None:
    store = DocumentStore(tmp_path / "db.sqlite3")
    try:
        with pytest.raises(ApiException) as excinfo:
            create_revision(
                store,
                "s1",
                RevisionCreateRequest.model_validate(
                    {
                        "template_id": "rc_lowpass",
                        "values": [{"component_id": "R1", "value": smuggled}],
                    }
                ),
            )
        assert excinfo.value.error.code == "validation_error"
        assert excinfo.value.error.field == "values.0.value"
    finally:
        store.close()


def _source(**tone: float) -> SourceSpec:
    return SourceSpec(
        tones=[Tone(label="t", role="wanted", **tone)],  # type: ignore[arg-type]
        description="test",
    )


@pytest.mark.parametrize(
    "circuit",
    [
        CircuitSpec(
            "rc_highpass", {"R1": 1e3, "C1": 1e-7}, _source(frequency_hz=440, amplitude_v=1)
        ),
        CircuitSpec("rc_lowpass", {"R1": 1e3}, _source(frequency_hz=440, amplitude_v=1)),
        CircuitSpec(
            "rc_lowpass",
            {"R1": 1e3, "C1": 1e-7, "L1": 1.0},
            _source(frequency_hz=440, amplitude_v=1),
        ),
        CircuitSpec(
            "rc_lowpass", {"R1": math.nan, "C1": 1e-7}, _source(frequency_hz=440, amplitude_v=1)
        ),
        CircuitSpec(
            "rc_lowpass", {"R1": math.inf, "C1": 1e-7}, _source(frequency_hz=440, amplitude_v=1)
        ),
        CircuitSpec(
            "rc_lowpass", {"R1": -1e3, "C1": 1e-7}, _source(frequency_hz=440, amplitude_v=1)
        ),
        CircuitSpec(
            "rc_lowpass", {"R1": "1k", "C1": 1e-7}, _source(frequency_hz=440, amplitude_v=1)
        ),  # type: ignore[dict-item]
        CircuitSpec(
            "rc_lowpass", {"R1": 1e3, "C1": 1e-7}, _source(frequency_hz=1e7, amplitude_v=1)
        ),
        CircuitSpec(
            "rc_lowpass",
            {"R1": 1e3, "C1": 1e-7},
            _source(frequency_hz=440, amplitude_v=1),
            disconnected=frozenset({"C1"}),
        ),
    ],
)
def test_unexpected_circuits_are_rejected(circuit: CircuitSpec) -> None:
    with pytest.raises(NetlistError):
        compile_transient(circuit, TransientRequest(0.1, 5e-6))
    if circuit.source.tones[0].frequency_hz < 1e5:  # AC ignores tones
        with pytest.raises(NetlistError):
            compile_ac(circuit, AcSweep(10.0, 1e3, 10))


@pytest.mark.parametrize(
    "analysis",
    [
        AcSweep(10.0, 10.0, 10),
        AcSweep(1e3, 10.0, 10),
        AcSweep(0.0, 10.0, 10),
        AcSweep(10.0, 1e9, 10),
        AcSweep(10.0, 1e3, 0),
        AcSweep(10.0, 1e3, 10_000),
        AcSweep(10.0, 1e3, 10, probes=()),
        AcSweep(10.0, 1e3, 10, probes=("v(mid)",)),  # type: ignore[arg-type]
        AcPoints(()),
        AcPoints(tuple(float(f) for f in range(100, 1800, 100))),  # 17 points
        AcPoints((math.nan,)),
    ],
)
def test_out_of_limit_ac_requests_are_rejected(analysis: AcSweep | AcPoints) -> None:
    with pytest.raises(NetlistError):
        compile_ac(_circuit(), analysis)


@pytest.mark.parametrize(
    "request_",
    [
        TransientRequest(0.0, 5e-6),
        TransientRequest(10.0, 5e-6),
        TransientRequest(1.4, 1e-9),
        TransientRequest(1.4, 0.5),
        TransientRequest(math.nan, 5e-6),
        TransientRequest(0.1, 5e-6, probes=()),
    ],
)
def test_out_of_limit_transients_are_rejected(request_: TransientRequest) -> None:
    with pytest.raises(NetlistError):
        compile_transient(_circuit(), request_)
