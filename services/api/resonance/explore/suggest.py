"""Goal-to-design suggestions (owner: audio & signal engineer). STUB — replace the body.

Contract: ``suggest_design(revision, goal, vary) -> DesignSuggestion`` — analytical (labeled as
a prediction): target cutoff inside the feasible interval (and the template's supported
range), nearest E12 part for the varied component with the other kept, predicted tone gains
per constraint, and a tradeoff statement when the goal is infeasible or rounding misses.
"""

from __future__ import annotations

from typing import Literal

from ..api.errors import not_implemented
from ..contracts.circuits import Revision
from ..contracts.experiments import DesignSuggestion, Goal


def suggest_design(
    revision: Revision, goal: Goal, *, vary: Literal["R1", "C1"]
) -> DesignSuggestion:
    raise not_implemented("Design suggestions")
