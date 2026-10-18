"""Strict parser for ngspice binary raw files (owner: circuit & simulation engineer).

Format (ngspice ``set filetype=binary`` + ``write``), one or more plots concatenated
(``set appendwrite``)::

    Title: ...
    Date: ...
    Command: ...            (optional extra "Key: value" lines are tolerated)
    Plotname: AC Analysis
    Flags: complex          ("real" for transient)
    No. Variables: 4
    No. Points: 161
    Variables:
    \t0\tfrequency\tfrequency grid=3
    \t1\tv(in)\tvoltage
    ...
    Binary:
    <points × variables × float64 (real) or × 2 float64 (complex: re, im)>, little-endian

The parser validates mandatory header keys, variable indices, counts, and the exact data
length, and rejects trailing garbage. It never echoes file content in its error messages.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

MAX_POINTS = 5_000_000
MAX_VARIABLES = 16
MAX_PLOTS = 64
_MAX_HEADER_BYTES = 8192
_BINARY_MARKER = b"Binary:\n"


class RawFormatError(ValueError):
    """The raw file is malformed or incomplete. Messages are generic (no file content)."""


@dataclass(frozen=True)
class RawPlot:
    plotname: str
    is_complex: bool
    variables: tuple[str, ...]  # names as written by ngspice (lower-case), scale first
    data: NDArray[np.float64] | NDArray[np.complex128]  # shape (points, variables)

    def column(self, name: str) -> NDArray[np.float64] | NDArray[np.complex128]:
        try:
            idx = self.variables.index(name)
        except ValueError:
            raise RawFormatError("missing variable") from None
        return self.data[:, idx]


def _parse_header(text: str) -> tuple[str, bool, int, int, tuple[str, ...]]:
    lines = text.split("\n")
    fields: dict[str, str] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if line.strip() == "Variables:":
            break
        if ":" not in line:
            raise RawFormatError("malformed header line")
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    else:
        raise RawFormatError("missing variable list")
    for key in ("Title", "Plotname", "Flags", "No. Variables", "No. Points"):
        if key not in fields:
            raise RawFormatError("missing header field")
    flags = fields["Flags"].split()
    if ("complex" in flags) == ("real" in flags):
        raise RawFormatError("unsupported flags")
    try:
        nvars = int(fields["No. Variables"])
        npoints = int(fields["No. Points"])
    except ValueError:
        raise RawFormatError("bad counts") from None
    if not (1 <= nvars <= MAX_VARIABLES) or not (0 <= npoints <= MAX_POINTS):
        raise RawFormatError("counts out of range")
    names: list[str] = []
    for idx in range(nvars):
        if i >= len(lines):
            raise RawFormatError("truncated variable list")
        parts = lines[i].split()
        i += 1
        if len(parts) < 3 or parts[0] != str(idx):
            raise RawFormatError("malformed variable entry")
        names.append(parts[1])
    if any(line.strip() for line in lines[i:]):
        raise RawFormatError("unexpected header content")
    return fields["Plotname"], "complex" in flags, nvars, npoints, tuple(names)


def parse_raw(blob: bytes) -> list[RawPlot]:
    """Parse every plot in a binary raw file. Raises ``RawFormatError``."""
    plots: list[RawPlot] = []
    pos = 0
    while pos < len(blob):
        if len(plots) >= MAX_PLOTS:
            raise RawFormatError("too many plots")
        if not blob.startswith(b"Title:", pos):
            raise RawFormatError("unexpected data")
        marker = blob.find(_BINARY_MARKER, pos, pos + _MAX_HEADER_BYTES)
        if marker < 0:
            raise RawFormatError("missing binary marker")
        try:
            header = blob[pos:marker].decode("ascii")
        except UnicodeDecodeError:
            raise RawFormatError("non-ASCII header") from None
        plotname, is_complex, nvars, npoints, names = _parse_header(header)
        start = marker + len(_BINARY_MARKER)
        width = 16 if is_complex else 8
        nbytes = npoints * nvars * width
        if start + nbytes > len(blob):
            raise RawFormatError("truncated data")
        values = np.frombuffer(blob, dtype="<f8", count=nbytes // 8, offset=start)
        if is_complex:
            pairs = values.reshape(npoints, nvars, 2)
            data: NDArray[np.float64] | NDArray[np.complex128] = (
                pairs[..., 0] + 1j * pairs[..., 1]
            ).astype(np.complex128)
        else:
            data = values.reshape(npoints, nvars).astype(np.float64)
        plots.append(RawPlot(plotname=plotname, is_complex=is_complex, variables=names, data=data))
        pos = start + nbytes
    if not plots:
        raise RawFormatError("empty file")
    return plots
