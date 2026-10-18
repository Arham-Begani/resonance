"""Strict raw-file parsing and simulator-side output validation (no ngspice needed)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pytest

from resonance.circuits.revisions import build_revision, revision_to_circuit
from resonance.contracts.internal import AcPoints, AcSweep, CancelToken, SimulationFailed
from resonance.simulation import NgspiceRunner, NgspiceSimulator
from resonance.simulation.raw import RawFormatError, parse_raw


def _raw(
    names: Sequence[str],
    rows: np.ndarray,
    *,
    complex_data: bool,
    points: int | None = None,
    nvars: int | None = None,
) -> bytes:
    flags = "complex" if complex_data else "real"
    header = [
        "Title: * resonance trusted netlist",
        "Date: Thu Oct 15 22:27:27  2026",
        "Command: ngspice-47",
        "Plotname: AC Analysis" if complex_data else "Plotname: Transient Analysis",
        f"Flags: {flags}",
        f"No. Variables: {nvars if nvars is not None else len(names)}",
        f"No. Points: {points if points is not None else rows.shape[0]}",
        "Variables:",
        *(f"\t{i}\t{n}\t{'frequency' if i == 0 else 'voltage'}" for i, n in enumerate(names)),
        "Binary:",
    ]
    if complex_data:
        body = np.stack([rows.real, rows.imag], axis=-1).astype("<f8").tobytes()
    else:
        body = rows.astype("<f8").tobytes()
    return ("\n".join(header) + "\n").encode("ascii") + body


def test_parses_real_and_appended_complex_plots() -> None:
    real_rows = np.array([[0.0, 1.0], [1e-6, 2.0], [2e-6, 3.0]])
    plots = parse_raw(_raw(["time", "v(out)"], real_rows, complex_data=False))
    assert plots[0].variables == ("time", "v(out)") and not plots[0].is_complex
    assert np.array_equal(plots[0].data, real_rows)

    one = np.array([[440.0 + 0j, 0.9 - 0.25j]])
    two = np.array([[4000.0 + 0j, 0.1 - 0.3j]])
    blob = _raw(["frequency", "v(out)"], one, complex_data=True)
    blob += _raw(["frequency", "v(out)"], two, complex_data=True)
    plots = parse_raw(blob)
    assert len(plots) == 2 and plots[1].data[0, 1] == 0.1 - 0.3j


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b[:-8],  # truncated data
        lambda b: b + b"garbage",  # trailing bytes that are not a plot
        lambda b: b.replace(b"Binary:\n", b"Binery:\n"),
        lambda b: b.replace(b"Flags: real", b"Flags: weird"),
        lambda b: b.replace(b"No. Points: 3", b"No. Points: 4"),
        lambda b: b.replace(b"No. Points: 3", b"No. Points: x"),
        lambda b: b.replace(b"\t1\tv(out)", b"\t7\tv(out)"),
        lambda b: b.replace(b"Title: *", "Title: é".encode()),
        lambda b: b"",
    ],
)
def test_malformed_raw_files_are_rejected(mutate: object) -> None:
    good = _raw(
        ["time", "v(out)"], np.array([[0.0, 1.0], [1.0, 2.0], [2.0, 3.0]]), complex_data=False
    )
    with pytest.raises(RawFormatError):
        parse_raw(mutate(good))  # type: ignore[operator]


class _StubRunner(NgspiceRunner):
    """Returns a canned raw file instead of running ngspice."""

    def __init__(self, blob: bytes, tmp: Path) -> None:
        super().__init__(
            "unused", jobs_dir=tmp, timeout_s=5, max_output_bytes=1 << 24, max_concurrency=1
        )
        self.blob = blob

    def run(
        self,
        netlist: str,
        outputs: Sequence[str],
        *,
        cancel: Sequence[CancelToken | None] = (),
        kind: str = "job",
    ) -> dict[str, bytes]:
        return {outputs[0]: self.blob}


def _circuit() -> object:
    return revision_to_circuit(build_revision("rc_lowpass", {}, origin="user_edit"))


def test_unexpected_variables_are_rejected(tmp_path: Path) -> None:
    rows = np.array([[440.0 + 0j, 1.0 + 0j, 0.5 + 0j, 1e-3 + 0j]])
    blob = _raw(["frequency", "v(in)", "v(out)", "v(secret)"], rows, complex_data=True)
    sim = NgspiceSimulator(_StubRunner(blob, tmp_path))
    with pytest.raises(SimulationFailed):
        asyncio.run(sim.ac(_circuit(), AcPoints((440.0,))))  # type: ignore[arg-type]


def test_wrong_frequencies_and_non_finite_values_are_rejected(tmp_path: Path) -> None:
    probes = ("v(in)", "v(out)", "i(v1)")
    shifted = np.array([[441.0 + 0j, 1.0 + 0j, 0.5 + 0j, 1e-3 + 0j]])
    sim = NgspiceSimulator(
        _StubRunner(_raw(["frequency", *probes], shifted, complex_data=True), tmp_path)
    )
    with pytest.raises(SimulationFailed):
        asyncio.run(sim.ac(_circuit(), AcPoints((440.0,))))  # type: ignore[arg-type]

    nan_rows = np.array([[440.0 + 0j, 1.0 + 0j, np.nan + 0j, 1e-3 + 0j]])
    sim = NgspiceSimulator(
        _StubRunner(_raw(["frequency", *probes], nan_rows, complex_data=True), tmp_path)
    )
    with pytest.raises(SimulationFailed):
        asyncio.run(sim.ac(_circuit(), AcPoints((440.0,))))  # type: ignore[arg-type]

    grid = np.geomspace(10.0, 1000.0, 5)  # wrong point count for 10 points/decade
    rows = np.column_stack([grid, np.ones(5), np.ones(5), np.ones(5)]).astype(complex)
    sim = NgspiceSimulator(
        _StubRunner(_raw(["frequency", *probes], rows, complex_data=True), tmp_path)
    )
    with pytest.raises(SimulationFailed):
        asyncio.run(sim.ac(_circuit(), AcSweep(10.0, 1000.0, 10)))  # type: ignore[arg-type]


def test_source_current_sign_is_converted_to_delivered(tmp_path: Path) -> None:
    rows = np.array([[440.0 + 0j, 1.0 + 0j, 0.9 - 0.2j, -7e-5 - 2.5e-4j]])
    blob = _raw(["frequency", "v(in)", "v(out)", "i(v1)"], rows, complex_data=True)
    data = asyncio.run(
        NgspiceSimulator(_StubRunner(blob, tmp_path)).ac(_circuit(), AcPoints((440.0,)))
    )  # type: ignore[arg-type]
    assert data.i_source is not None and data.i_source[0] == 7e-5 + 2.5e-4j
