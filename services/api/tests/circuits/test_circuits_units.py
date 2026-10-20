"""SI value parsing/formatting: explicit units, rejected ambiguity, exact round trips."""

from __future__ import annotations

import math

import pytest

from resonance.circuits.units import (
    ValueParseError,
    format_frequency,
    format_value,
    parse_value,
    parse_value_request,
)
from resonance.contracts.circuits import ValueParseRequest


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1k", 1e3),
        ("1K", 1e3),
        ("4.7k", 4.7e3),
        ("4.7 kΩ", 4.7e3),
        ("4.7 k Ω", 4.7e3),
        ("4.7kohm", 4.7e3),
        ("4.7 KOhms", 4.7e3),
        ("4k7", 4.7e3),
        ("4K7Ω", 4.7e3),
        ("4R7", 4.7),
        ("100R", 100.0),
        ("100", 100.0),
        ("  220  ", 220.0),
        ("1M", 1e6),
        ("1meg", 1e6),
        ("1 MEG", 1e6),
        ("2.2Meg ohm", 2.2e6),
        ("1M5", 1.5e6),
        ("1G", 1e9),
        ("1e3", 1e3),
        ("4.7E3 Ω", 4.7e3),
        (".5k", 500.0),
        ("+1k", 1e3),
        ("1 mΩ", 1e-3),
        ("1Ω", 1.0),  # OHM SIGN
    ],
)
def test_resistor_values_parse(text: str, expected: float) -> None:
    assert parse_value(text, "ohm") == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("100n", 100e-9),
        ("100 nF", 100e-9),
        ("100nF", 100e-9),
        ("0.1u", 0.1e-6),
        ("0.1 µF", 0.1e-6),  # MICRO SIGN
        ("0.1 μF", 0.1e-6),  # GREEK MU
        ("2n2", 2.2e-9),
        ("470p", 470e-12),
        ("470 pF", 470e-12),
        ("1e-7", 1e-7),
        ("1e-7 F", 1e-7),
        ("10 fF", 10e-15),
        ("10 F", 10.0),
        ("1 mF", 1e-3),
        ("3.3 farads", 3.3),
        ("2.2uF", 2.2e-6),
    ],
)
def test_capacitor_values_parse(text: str, expected: float) -> None:
    assert parse_value(text, "farad") == expected


def test_prefix_scaling_has_no_float_noise() -> None:
    # 4.7 * 1000 == 4700.000000000001 in binary floating point; decimal scaling avoids it.
    assert parse_value("4.7k", "ohm") == 4700.0
    assert parse_value("2.2u", "farad") == 2.2e-6
    assert parse_value("0.33u", "farad") == 0.33e-6


def test_numbers_are_canonical_si() -> None:
    assert parse_value(1000, "ohm") == 1000.0
    assert parse_value(1e-7, "farad") == 1e-7


@pytest.mark.parametrize(
    ("text", "unit", "fragment"),
    [
        ("", "ohm", "Enter a value"),
        ("   ", "ohm", "Enter a value"),
        ("10f", "farad", "ambiguous"),
        ("10 f", "farad", "ambiguous"),
        ("100 nf", "farad", "capital F"),
        ("100 nF", "ohm", "resistor"),
        ("1kΩ", "farad", "capacitor"),
        ("100R", "farad", "capacitor"),
        ("4R7", "farad", "capacitor"),
        ("1kk", "ohm", "more than one SI prefix"),
        ("1 uuF", "farad", "more than one SI prefix"),
        ("1e3k", "ohm", "scientific notation"),
        ("1,5k", "ohm", "commas"),
        ("1,000", "ohm", "commas"),
        ("1N", "farad", "case-sensitive"),
        ("1U", "farad", "case-sensitive"),
        ("1 Hz", "ohm", "unrecognized"),
        ("nan", "ohm", "not a value"),
        ("inf", "ohm", "not a value"),
        ("1e400", "ohm", "too large"),
        ("0", "ohm", "greater than zero"),
        ("-1k", "ohm", "greater than zero"),
        ("0.0n", "farad", "greater than zero"),
        ("1_000", "ohm", "characters"),
        ("1k\n.control", "ohm", "characters"),
        ("1k; shell", "ohm", "characters"),
        ("4.7.1k", "ohm", "not a value"),
        ("x" * 40, "ohm", "too long"),
    ],
)
def test_invalid_and_ambiguous_values_are_rejected(text: str, unit: str, fragment: str) -> None:
    with pytest.raises(ValueParseError) as excinfo:
        parse_value(text, unit)  # type: ignore[arg-type]
    assert fragment.lower() in str(excinfo.value).lower()


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, 0.0, -5.0, True, 10**400])
def test_invalid_numbers_are_rejected(bad: float) -> None:
    with pytest.raises(ValueParseError):
        parse_value(bad, "ohm")


@pytest.mark.parametrize(
    ("value", "unit", "text"),
    [
        (1000.0, "ohm", "1 kΩ"),
        (4700.0, "ohm", "4.7 kΩ"),
        (100.0, "ohm", "100 Ω"),
        (100e3, "ohm", "100 kΩ"),
        (1.5e6, "ohm", "1.5 MΩ"),
        (1591.549, "ohm", "1.592 kΩ"),
        (100e-9, "farad", "100 nF"),
        (2.2e-6, "farad", "2.2 µF"),
        (470e-12, "farad", "470 pF"),
        (1e-9, "farad", "1 nF"),
        (10e-6, "farad", "10 µF"),
        (0.1e-6, "farad", "100 nF"),
        (999.96, "ohm", "1 kΩ"),
    ],
)
def test_format_value(value: float, unit: str, text: str) -> None:
    assert format_value(value, unit) == text  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("value", "unit"),
    [(v, "ohm") for v in (100.0, 330.0, 1e3, 4.7e3, 12.3e3, 68e3, 100e3, 1.234e3)]
    + [(v, "farad") for v in (1e-9, 2.2e-9, 47e-9, 100e-9, 330e-9, 1e-6, 4.7e-6, 10e-6)],
)
def test_format_parse_round_trip(value: float, unit: str) -> None:
    assert parse_value(format_value(value, unit), unit) == value  # type: ignore[arg-type]


def test_format_frequency() -> None:
    assert format_frequency(440.0) == "440 Hz"
    assert format_frequency(4000.0) == "4 kHz"
    assert format_frequency(1591.549) == "1.592 kHz"
    assert format_frequency(20.0) == "20 Hz"


def test_parse_request_reports_bounds_and_never_raises() -> None:
    ok = parse_value_request(ValueParseRequest(component_id="R1", text="4k7"))
    assert ok.ok and ok.value_si == 4700.0 and ok.display_value == "4.7 kΩ"

    low = parse_value_request(ValueParseRequest(component_id="R1", text="10"))
    assert not low.ok and low.value_si is None
    assert "R1" in (low.message or "") and "100 Ω" in (low.message or "")

    mega = parse_value_request(ValueParseRequest(component_id="C1", text="1M"))
    assert not mega.ok and "mega" in (mega.message or "")

    ambiguous = parse_value_request(ValueParseRequest(component_id="C1", text="10f"))
    assert not ambiguous.ok and "ambiguous" in (ambiguous.message or "")

    # The high-pass uses the same per-component bounds.
    high = parse_value_request(
        ValueParseRequest(component_id="R1", text="1k", template_id="rc_highpass")
    )
    assert high.ok and high.value_si == 1000.0
    high_low = parse_value_request(
        ValueParseRequest(component_id="C1", text="100p", template_id="rc_highpass")
    )
    assert not high_low.ok and "1 nF" in (high_low.message or "")
