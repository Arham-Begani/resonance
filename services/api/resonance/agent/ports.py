"""Interfaces the agent consumes from other feature packages (structural, so tests can use
doubles). The agent never imports the evaluator or any hidden-state module at runtime.

``CaseServicePort`` must match ``resonance.challenges.service.CaseService`` exactly; mypy checks
that conformance statically (``_case_service_conforms`` below, type-checking only, so nothing
from the challenges service is imported at runtime).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Literal, Protocol

from ..contracts.experiments import ExperimentComparison, ExperimentRecord
from ..contracts.internal import CancelToken
from ..contracts.investigation import (
    CaseAction,
    CaseExperimentRequest,
    DiagnosisSubmission,
    InvestigationMode,
    Observation,
    ObservationResponse,
    PublicCase,
    RepairProposalInput,
)

RunCanceller = Callable[[str], Awaitable[None]]
RunLiveness = Callable[[str], bool]

CaseRunKind = Literal["investigate", "hint"]
"""Run kinds that may own a case. Explore runs never claim a case (they have no case tools)."""


class CaseServicePort(Protocol):
    """The subset of ``resonance.challenges`` CaseService used by agent runs.

    Every method returns PUBLIC models only and raises ``ApiException`` on refusal."""

    def get_case(self, session_id: str, case_id: str) -> PublicCase: ...

    def get_observation(
        self, session_id: str, case_id: str, observation_id: str
    ) -> Observation: ...

    async def set_mode(
        self, session_id: str, case_id: str, mode: InvestigationMode
    ) -> PublicCase: ...

    async def run_experiment(
        self,
        session_id: str,
        case_id: str,
        request: CaseExperimentRequest,
        *,
        requested_by: Literal["user", "agent"],
        agent_run_id: str | None = None,
        cancel: CancelToken | None = None,
    ) -> ObservationResponse: ...

    async def submit_diagnosis(
        self,
        session_id: str,
        case_id: str,
        submission: DiagnosisSubmission,
        *,
        submitted_by: Literal["user", "agent"],
        agent_run_id: str | None = None,
    ) -> PublicCase: ...

    async def propose_repair(
        self,
        session_id: str,
        case_id: str,
        body: RepairProposalInput,
        *,
        proposed_by: Literal["user", "agent"],
        agent_run_id: str | None = None,
    ) -> PublicCase: ...

    def is_closing_or_closed(self, session_id: str, case_id: str) -> bool: ...

    async def record_hint(self, session_id: str, case_id: str) -> PublicCase: ...

    def claim_for_agent(
        self,
        session_id: str,
        case_id: str,
        run_id: str,
        kind: CaseRunKind | None = None,
    ) -> None: ...

    def release_agent(self, session_id: str, case_id: str, run_id: str) -> None: ...

    def register_run_canceller(self, fn: RunCanceller) -> None: ...

    def register_run_liveness(self, fn: RunLiveness) -> None: ...

    def available_actions_for_run(
        self, session_id: str, case_id: str, run_id: str
    ) -> list[CaseAction]: ...


class ExplorePort(Protocol):
    """Explore experiment execution and comparison for public session revisions only."""

    async def run_experiment(
        self,
        session_id: str,
        revision_id: str,
        goal_id: str | None,
        cancel: CancelToken | None,
    ) -> ExperimentRecord: ...

    async def compare(self, session_id: str, a_id: str, b_id: str) -> ExperimentComparison: ...


if TYPE_CHECKING:  # static conformance check only; no runtime import of the case service
    from ..challenges.service import CaseService

    def _case_service_conforms(service: CaseService) -> CaseServicePort:
        return service
