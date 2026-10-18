"""Hidden-state evaluator (the investigation boundary).

Only this package constructs hidden ``CircuitSpec``s or reads the evaluator database. It is
held privately by ``resonance.challenges.service.CaseService`` and is never attached to
``app.state`` or exposed to agent tools.
"""

from .service import (
    Evaluator,
    EvaluatorFailure,
    IntendedDesign,
    RepairMeasurement,
    RevealData,
    UnknownCase,
)
from .store import EvaluatorStore

__all__ = [
    "Evaluator",
    "EvaluatorFailure",
    "EvaluatorStore",
    "IntendedDesign",
    "RepairMeasurement",
    "RevealData",
    "UnknownCase",
]
