"""Visible-text hygiene for model replies: hidden-reasoning removal and citation checking."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from ..contracts.common import EvidenceRef, QuantityName

_THINK_BLOCK = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.DOTALL | re.IGNORECASE)
_THINK_OPEN = re.compile(r"<(think|thinking|reasoning)>", re.IGNORECASE)
_THINK_CLOSE = re.compile(r"</(think|thinking|reasoning)>", re.IGNORECASE)

RESULT_ID = re.compile(r"\b(?:obs|exp)_[A-Za-z0-9_-]{8,64}")
UNVERIFIED = "unverified citation"


def strip_reasoning(content: str | None) -> str:
    """Remove hidden-reasoning segments from model content and return the visible text.

    Handles complete ``<think>…</think>`` blocks, a dangling close tag (reasoning emitted before
    ``</think>`` without an opening tag), and an unterminated opening tag (everything after it
    is reasoning).
    """
    if not content:
        return ""
    text = _THINK_BLOCK.sub("", content)
    closes = list(_THINK_CLOSE.finditer(text))
    if closes:
        text = text[closes[-1].end() :]
    opened = _THINK_OPEN.search(text)
    if opened is not None:
        text = text[: opened.start()]
    return text.strip()


@dataclass
class CitationCheck:
    text: str
    citations: list[EvidenceRef] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)


def check_citations(text: str, known: Mapping[str, QuantityName]) -> CitationCheck:
    """Validate result-ID citations against the IDs recorded for this case/session.

    ``known`` maps each valid result ID to its primary quantity name. Valid IDs become
    ``EvidenceRef``s (deduplicated, in order). Any ID-shaped token that does not match a
    recorded result is replaced in the visible text by ``unverified citation``; it is never
    turned into evidence.
    """
    citations: list[EvidenceRef] = []
    seen: set[str] = set()
    unverified: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        token = match.group(0)
        quantity = known.get(token)
        if quantity is None:
            unverified.append(token)
            return UNVERIFIED
        if token not in seen:
            seen.add(token)
            citations.append(EvidenceRef(result_id=token, quantity=quantity))
        return token

    cleaned = RESULT_ID.sub(_replace, text)
    return CitationCheck(text=cleaned, citations=citations, unverified=unverified)
