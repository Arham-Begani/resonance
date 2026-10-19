"""Filtering goals (requested constraints) and their analytical feasibility.

Kept in their own module so circuit templates can carry a default goal without an import
cycle; ``contracts.experiments`` re-exports these names.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .common import Contract, GoalId


class GoalConstraint(Contract):
    kind: Literal["preserve", "attenuate"] = Field(
        description="preserve: attenuation ≤ limit_db. attenuate: attenuation ≥ limit_db."
    )
    frequency_hz: float = Field(ge=20, le=20_000)
    limit_db: float = Field(ge=0, le=60)
    label: str = Field(max_length=60, examples=["Musical tone", "Whistle"])


class GoalInput(Contract):
    title: str = Field(max_length=120)
    constraints: list[GoalConstraint] = Field(min_length=1, max_length=4)


class Goal(Contract):
    id: GoalId
    title: str = Field(examples=["Keep the musical tone. Reduce the whistle."])
    constraints: list[GoalConstraint]
    origin: Literal["default", "user", "agent"]


class FeasibilityAnalysis(Contract):
    """Analytical feasibility for the template family (a prediction, not a measurement)."""

    basis: Literal["analytical_first_order_rc"] = "analytical_first_order_rc"
    template_id: str
    feasible: bool
    cutoff_range_hz: tuple[float, float] | None = Field(
        default=None, description="Cutoff frequencies that satisfy every constraint, if any."
    )
    explanation: str
