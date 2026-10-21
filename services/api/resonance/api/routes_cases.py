"""Hidden-fault cases (owner: investigation & AI engineer).

Contract (fixed by lead; propose changes before editing signatures):
- POST /api/cases: new case with a fresh opaque ID and hidden state created by the evaluator;
  records the standardized symptom (uncounted). Sets the session's active case. 409 if the
  session already has an unclosed case. ``template_id`` (public) picks the circuit family;
  null draws it with ``policy.CASE_FAMILY_PRIOR``.
- GET /api/cases/{id}: public state only. Cases are session-scoped (others → 404).
- POST /api/cases/{id}/mode: choose guide | agent (ready|observing → observing; no run active).
- POST /api/cases/{id}/experiments: run one allowed instrument against the hidden circuit.
  Budget enforced server-side; validation errors never consume budget; one automatic retry
  for infrastructure failures; idempotent per (required) client_request_id; 409 while an agent
  run owns the case, outside observing, or when the budget is exhausted. A client disconnect
  cancels the experiment (not counted).
- POST /api/cases/{id}/diagnosis: submit once (fault | no_fault | insufficient_evidence).
- POST /api/cases/{id}/repair-proposal: record or replace the repair (one component back to
  its intended nominal value) after a diagnosis, until verification.
- POST /api/cases/{id}/repair: no body; verify the RECORDED proposal once (only that component
  changes in the hidden circuit) against the intended design on the declared grid.
- POST /api/cases/{id}/close: stop any agent run and in-flight work, close, and reveal.

All behavior lives in ``resonance.challenges.service.CaseService``.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from ..challenges.service import CaseService
from ..contracts.internal import CancelToken
from ..contracts.investigation import (
    CaseCreateRequest,
    CaseExperimentRequest,
    CaseModeRequest,
    DiagnosisSubmission,
    ObservationResponse,
    PublicCase,
    RepairProposalInput,
)
from .deps import get_session_id
from .routes_meta import ERRORS

router = APIRouter(prefix="/api", tags=["cases"])

SessionId = Annotated[str, Depends(get_session_id)]


def get_case_service(request: Request) -> CaseService:
    service: CaseService = request.app.state.case_service
    return service


Cases = Annotated[CaseService, Depends(get_case_service)]


@router.post(
    "/cases",
    response_model=PublicCase,
    status_code=201,
    operation_id="createCase",
    responses=ERRORS,
)
async def create_case(body: CaseCreateRequest, session_id: SessionId, cases: Cases) -> PublicCase:
    # A null family is drawn with the published policy.CASE_FAMILY_PRIOR (secrets-based).
    return await cases.create_case(session_id, body.template_id)


@router.get("/cases/{case_id}", response_model=PublicCase, operation_id="getCase", responses=ERRORS)
async def get_case(case_id: str, session_id: SessionId, cases: Cases) -> PublicCase:
    return cases.get_case(session_id, case_id)


@router.post(
    "/cases/{case_id}/mode",
    response_model=PublicCase,
    operation_id="setCaseMode",
    responses=ERRORS,
)
async def set_case_mode(
    case_id: str, body: CaseModeRequest, session_id: SessionId, cases: Cases
) -> PublicCase:
    return await cases.set_mode(session_id, case_id, body.mode)


@router.post(
    "/cases/{case_id}/experiments",
    response_model=ObservationResponse,
    status_code=201,
    operation_id="runCaseExperiment",
    responses=ERRORS,
)
async def run_case_experiment(
    case_id: str, body: CaseExperimentRequest, request: Request, session_id: SessionId, cases: Cases
) -> ObservationResponse:
    cancel = CancelToken()
    watcher = asyncio.create_task(_cancel_on_disconnect(request, cancel))
    try:
        return await cases.run_experiment(
            session_id, case_id, body, requested_by="user", cancel=cancel
        )
    finally:
        watcher.cancel()


async def _cancel_on_disconnect(request: Request, cancel: CancelToken) -> None:
    """A client disconnect cancels the running experiment (it is not counted)."""
    while not cancel.cancelled:
        if await request.is_disconnected():
            cancel.cancel()
            return
        await asyncio.sleep(0.25)


@router.post(
    "/cases/{case_id}/diagnosis",
    response_model=PublicCase,
    operation_id="submitDiagnosis",
    responses=ERRORS,
)
async def submit_diagnosis(
    case_id: str, body: DiagnosisSubmission, session_id: SessionId, cases: Cases
) -> PublicCase:
    return await cases.submit_diagnosis(session_id, case_id, body, submitted_by="user")


@router.post(
    "/cases/{case_id}/repair-proposal",
    response_model=PublicCase,
    operation_id="proposeRepair",
    responses=ERRORS,
)
async def propose_repair(
    case_id: str, body: RepairProposalInput, session_id: SessionId, cases: Cases
) -> PublicCase:
    return await cases.propose_repair(session_id, case_id, body, proposed_by="user")


@router.post(
    "/cases/{case_id}/repair",
    response_model=PublicCase,
    operation_id="verifyRepair",
    responses=ERRORS,
)
async def verify_repair(case_id: str, session_id: SessionId, cases: Cases) -> PublicCase:
    return await cases.verify_repair(session_id, case_id, requested_by="user")


@router.post(
    "/cases/{case_id}/close",
    response_model=PublicCase,
    operation_id="closeCase",
    responses=ERRORS,
)
async def close_case(case_id: str, session_id: SessionId, cases: Cases) -> PublicCase:
    return await cases.close_case(session_id, case_id)
