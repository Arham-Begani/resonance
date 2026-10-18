"""Explicit SI value parsing and formatting for component values.

Owner: circuit & simulation engineer.

Public API (stable; other modules depend on these names):

- ``ValueParseError(ValueError)`` — learner-safe ``str()``; optional ``component_id``.
- ``parse_value(value, unit) -> float`` — text or number → canonical SI float (ohms/farads).
- ``parse_value_detailed(value, unit) -> ParsedValue`` — same, plus the SI prefix used.
- ``format_value(value_si, unit) -> str`` — e.g. ``"4.7 kΩ"``, ``"100 nF"``, ``"2.2 µF"``.
- ``format_frequency(hz) -> str`` — e.g. ``"440 Hz"``, ``"4 kHz"``.
- ``parse_value_request(ValueParseRequest) -> ValueParseResult`` — never raises for bad input;
  includes the template's per-component bounds check.

Accepted text (whitespace-tolerant, at most 32 characters):

- A decimal number (``4.7``, ``.5``, ``4700``) or scientific notation (``4.7e3``, ``1e-7``),
  optionally followed by ONE case-sensitive SI prefix and an optional unit:
  prefixes ``p`` (1e-12), ``n`` (1e-9), ``u``/``µ``/``μ`` (1e-6), ``m`` (milli, 1e-3),
  ``k``/``K`` (1e3), ``M``/``meg``/``Meg``/``MEG`` (mega, 1e6), ``G`` (1e9), and ``f``
  (femto, 1e-15) only when followed by the unit ``F`` (``fF``).
- Units: resistors accept ``Ω``, ``ohm``, ``ohms`` (any case) or a bare ``R`` with no prefix
  (``100R``); capacitors accept ``F``, ``farad``, ``farads``.
- RKM notation, where the prefix letter is the decimal point: ``4k7`` = 4.7 k, ``2n2`` = 2.2 n,
  ``1M5`` = 1.5 M; for resistors ``4R7`` = 4.7 Ω.
- Numbers (JSON floats/ints) are canonical SI values.

Rejected: empty text, NaN/infinity, zero or negative values, commas (decimal comma vs. thousands
separator is ambiguous), unknown or wrong-case prefixes (``N``, ``U``), double prefixes
(``1kk``), mixing scientific notation with a prefix (``1e3k``), a unit that does not match the
component (``100 nF`` for a resistor), and the AMBIGUOUS bare trailing ``f`` (femto or farad?).

Note: SPICE reads ``M`` as milli. Resonance uses SI: ``M`` is mega and ``m`` is milli. Messages
say so whenever an out-of-range value used one of these prefixes.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from ..contracts.circuits import ValueParseRequest, ValueParseResult, ValueUnit

MAX_TEXT_LENGTH = 32

_PREFIX_EXPONENT: dict[str, int] = {
    "": 0,
    "f": -15,  # femto: only valid together with an explicit "F" unit (see _resolve_prefix)
    "p": -12,
    "n": -9,
    "u": -6,
    "m": -3,
    "k": 3,
    "K": 3,
    "M": 6,
    "meg": 6,
    "Meg": 6,
    "MEG": 6,
    "G": 9,
}
_SINGLE_PREFIX_CHARS = set("fpnumkKMG")

_UNIT_NAME: dict[ValueUnit, str] = {"ohm": "ohms (Ω)", "farad": "farads (F)"}
_UNIT_SYMBOL: dict[ValueUnit, str] = {"ohm": "Ω", "farad": "F"}
_KIND_NAME: dict[ValueUnit, str] = {"ohm": "a resistor", "farad": "a capacitor"}
_EXAMPLE: dict[ValueUnit, str] = {"ohm": "“4.7k” or “4.7 kΩ”", "farad": "“100n” or “100 nF”"}

_FORMAT_PREFIXES: dict[ValueUnit, dict[int, str]] = {
    "ohm": {-3: "m", 0: "", 3: "k", 6: "M", 9: "G"},
    "farad": {-15: "f", -12: "p", -9: "n", -6: "µ", -3: "m", 0: ""},
}

_NUMBER_RE = re.compile(
    r"^(?P<num>[+-]?(?:\d+\.?\d*|\.\d+))(?P<exp>[eE][+-]?\d+)?\s*(?P<suffix>[A-Za-zΩ ]*)$"
)
_RKM_RE = re.compile(r"^(?P<int>\d+)(?P<mark>[pnumkKMGR])(?P<frac>\d+)\s*(?P<unit>[A-Za-zΩ ]*)$")
_ALLOWED_CHARS_RE = re.compile(r"^[0-9A-Za-z.+\- Ω]*$")


class ValueParseError(ValueError):
    """Invalid, ambiguous, or out-of-range component value. ``str()`` is learner-safe: it only
    repeats what the learner typed and public template bounds."""

    def __init__(self, message: str, *, component_id: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.component_id = component_id


@dataclass(frozen=True)
class ParsedValue:
    value_si: float
    prefix: str  # the SI prefix as typed ("" if none or numeric input)


def _normalize(text: str) -> str:
    return (
        text.strip()
        .replace("µ", "u")  # MICRO SIGN
        .replace("μ", "u")  # GREEK SMALL LETTER MU
        .replace("Ω", "Ω")  # OHM SIGN → GREEK CAPITAL OMEGA
    )


def _split_unit(suffix: str, unit: ValueUnit) -> tuple[str, str | None]:
    """Split a suffix (spaces removed) into (prefix text, unit kind or None).

    Raises ValueParseError for a unit that belongs to the other component kind."""
    lower = suffix.lower()
    for word in ("ohms", "ohm"):
        if lower.endswith(word):
            return suffix[: -len(word)], "ohm"
    if suffix.endswith("Ω"):
        return suffix[:-1], "ohm"
    for word in ("farads", "farad"):
        if lower.endswith(word):
            return suffix[: -len(word)], "farad"
    if suffix.endswith("F"):
        return suffix[:-1], "farad"
    if suffix == "R":
        return "", "ohm_r"
    return suffix, None


def _resolve_prefix(prefix: str, unit_found: str | None, unit: ValueUnit, shown: str) -> int:
    if prefix == "f":
        if unit_found == "farad":
            return _PREFIX_EXPONENT["f"]
        if unit == "farad":
            raise ValueParseError(
                f"“{shown}” is ambiguous: a trailing “f” could mean femto or farads. "
                "Write “10 fF” for femtofarads or “10 F” for farads."
            )
        raise ValueParseError(
            f"“{shown}” has an unrecognized suffix “f”. Use ohms, e.g. {_EXAMPLE[unit]}."
        )
    if prefix in _PREFIX_EXPONENT:
        return _PREFIX_EXPONENT[prefix]
    if unit == "farad" and len(prefix) >= 2 and prefix.endswith("f"):
        head = prefix[:-1]
        if head in _PREFIX_EXPONENT and head != "f":
            shown_head = "µ" if head == "u" else head
            raise ValueParseError(
                f"“{shown}”: write the farad unit as a capital F, e.g. “{shown_head}F” "
                "(a lowercase f means femto)."
            )
    if len(prefix) >= 2 and all(ch in _SINGLE_PREFIX_CHARS for ch in prefix):
        raise ValueParseError(
            f"“{shown}” has more than one SI prefix. Use a single prefix, e.g. {_EXAMPLE[unit]}."
        )
    raise ValueParseError(
        f"“{shown}” has an unrecognized suffix “{prefix}”. Prefixes are case-sensitive: "
        "p, n, u (µ), m (milli), k, M (mega), G — e.g. "
        f"{_EXAMPLE[unit]}."
    )


def _check_unit(unit_found: str | None, unit: ValueUnit, shown: str, prefix: str) -> None:
    if unit_found is None:
        return
    if unit_found == "ohm_r":
        if unit != "ohm":
            raise ValueParseError(
                f"“{shown}” uses “R” (ohms), but this is {_KIND_NAME[unit]}; "
                f"use {_UNIT_NAME[unit]}, e.g. {_EXAMPLE[unit]}."
            )
        return
    if unit_found != unit:
        raise ValueParseError(
            f"“{shown}” is in {_UNIT_NAME['ohm' if unit_found == 'ohm' else 'farad']}, but this "
            f"is {_KIND_NAME[unit]}; use {_UNIT_NAME[unit]}, e.g. {_EXAMPLE[unit]}."
        )


def _to_float(number: str, exponent: int, shown: str) -> float:
    try:
        value = float(Decimal(number).scaleb(exponent))
    except (InvalidOperation, OverflowError, ValueError):
        raise ValueParseError(f"“{shown}” is not a number we can read.") from None
    if not math.isfinite(value):
        raise ValueParseError(f"“{shown}” is too large.")
    return value


def _check_positive(value: float, shown: str) -> None:
    if value <= 0:
        raise ValueParseError(f"“{shown}”: component values must be greater than zero.")


def parse_value_detailed(value: str | float | int, unit: ValueUnit) -> ParsedValue:
    """Parse a learner-entered value into canonical SI. See the module docstring for the
    grammar. Raises ``ValueParseError`` with a learner-safe message."""
    if isinstance(value, bool):
        raise ValueParseError("Enter a number, not true/false.")
    if isinstance(value, int | float):
        try:
            number = float(value)
        except OverflowError:
            raise ValueParseError("The value is too large.") from None
        if not math.isfinite(number):
            raise ValueParseError("The value must be a finite number.")
        _check_positive(number, f"{value:g}")
        return ParsedValue(value_si=number, prefix="")
    if not isinstance(value, str):
        raise ValueParseError("Enter a value as a number or text such as “4.7k”.")

    text = _normalize(value)
    shown = value.strip()
    if not text:
        raise ValueParseError(f"Enter a value, e.g. {_EXAMPLE[unit]}.")
    if len(text) > MAX_TEXT_LENGTH:
        raise ValueParseError(f"That value is too long; use at most {MAX_TEXT_LENGTH} characters.")
    if "," in text:
        raise ValueParseError(
            f"“{shown}”: commas are ambiguous. Use a dot for decimals and no thousands "
            f"separators, e.g. {_EXAMPLE[unit]}."
        )
    if not _ALLOWED_CHARS_RE.fullmatch(text):
        raise ValueParseError(
            f"“{shown}” contains characters that aren't part of a value. Try {_EXAMPLE[unit]}."
        )

    rkm = _RKM_RE.fullmatch(text)
    if rkm:
        mark = rkm["mark"]
        unit_text = rkm["unit"].replace(" ", "")
        prefix, unit_found = _split_unit(unit_text, unit) if unit_text else ("", None)
        if prefix or unit_found == "ohm_r":
            raise ValueParseError(f"“{shown}” has an unrecognized suffix. Try {_EXAMPLE[unit]}.")
        _check_unit(unit_found, unit, shown, prefix)
        if mark == "R":
            if unit != "ohm":
                _check_unit("ohm_r", unit, shown, "")
            exponent = 0
        else:
            exponent = _PREFIX_EXPONENT[mark]
        digits = f"{rkm['int']}.{rkm['frac']}"
        result = _to_float(digits, exponent, shown)
        _check_positive(result, shown)
        return ParsedValue(value_si=result, prefix="" if mark == "R" else mark)

    match = _NUMBER_RE.fullmatch(text)
    if not match:
        raise ValueParseError(f"“{shown}” is not a value we can read. Try {_EXAMPLE[unit]}.")
    suffix = match["suffix"].replace(" ", "")
    prefix, unit_found = _split_unit(suffix, unit)
    _check_unit(unit_found, unit, shown, prefix)
    exponent = _resolve_prefix(prefix, unit_found, unit, shown)
    if match["exp"] and prefix:
        raise ValueParseError(
            f"“{shown}” mixes scientific notation with an SI prefix. Use one, e.g. "
            f"{_EXAMPLE[unit]}."
        )
    sci = int(match["exp"][1:]) if match["exp"] else 0
    result = _to_float(match["num"], exponent + sci, shown)
    _check_positive(result, shown)
    return ParsedValue(value_si=result, prefix=prefix)


def parse_value(value: str | float | int, unit: ValueUnit) -> float:
    """Parse text (``"4.7k"``, ``"100 nF"``) or a number (canonical SI) into ohms/farads."""
    return parse_value_detailed(value, unit).value_si


def format_value(value_si: float, unit: ValueUnit) -> str:
    """Format a canonical SI value with an SI prefix and at most 4 significant figures,
    e.g. ``1000.0 → "1 kΩ"``, ``1e-7 → "100 nF"``, ``2.2e-6 → "2.2 µF"``."""
    symbol = _UNIT_SYMBOL[unit]
    if not math.isfinite(value_si) or value_si <= 0:
        return f"{value_si:g} {symbol}"
    rounded = Decimal(f"{value_si:.4g}")
    exp3 = (rounded.adjusted() // 3) * 3
    prefixes = _FORMAT_PREFIXES[unit]
    exp3 = min(max(exp3, min(prefixes)), max(prefixes))
    mantissa = rounded.scaleb(-exp3).normalize()
    text = format(mantissa, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{text} {prefixes[exp3]}{symbol}"


def format_frequency(hz: float) -> str:
    """``440.0 → "440 Hz"``, ``4000.0 → "4 kHz"``, ``1591.55 → "1.592 kHz"``."""
    if not math.isfinite(hz) or hz <= 0:
        return f"{hz:g} Hz"
    rounded = Decimal(f"{hz:.4g}")
    if rounded >= 1_000_000:
        mantissa, unit = rounded.scaleb(-6), "MHz"
    elif rounded >= 1000:
        mantissa, unit = rounded.scaleb(-3), "kHz"
    else:
        mantissa, unit = rounded, "Hz"
    text = format(mantissa.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{text} {unit}"


def parse_value_request(body: ValueParseRequest) -> ValueParseResult:
    """Parse ``body.text`` for ``body.component_id`` of ``body.template_id`` and check the
    template's per-component bounds. Never raises for bad input: returns ``ok=False`` with a
    learner-safe message. (The joint cutoff constraint needs both values, so it is checked when
    a revision is created.)"""
    from .templates import TemplateUnavailableError, parse_component_value

    try:
        value_si, unit = parse_component_value(body.template_id, body.component_id, body.text)
    except (ValueParseError, TemplateUnavailableError) as exc:
        return ValueParseResult(ok=False, message=str(exc))
    return ValueParseResult(ok=True, value_si=value_si, display_value=format_value(value_si, unit))
