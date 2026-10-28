"""INVESTIGATE and HINT tool sets.

Every call goes through the public CaseService API and returns only the allow-listed views in
``agent.views``; no tool can reach the evaluator, the hidden circuit, or any file, HTTP,
database, or code facility. Every tool first checks that the case is not closing or closed and
refuses otherwise, so nothing can run (or be read) once a close has begun.

INVESTIGATE: get_case, assess_evidence, run_experiment, read_observation, submit_diagnosis,
propose_repair.  HINT: get_case, read_observation.

``InvestigateFinishCheck`` (INVESTIGATE runs only) decides, from the same PUBLIC case state the
tools read, whether a reply that ends without a tool call should get one completion reminder:
no diagnosis yet, or a "fault" diagnosis by this run without a repair proposal.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Self

from pydantic import Field, model_validator

from ..api.errors import ApiException
from ..contracts.circuits import EditableComponentId
from ..contracts.common import ApiError, Contract, ErrorCode, ObservationId, QuantityName
from ..contracts.internal import CancelToken
from ..contracts.investigation import (
    CaseAction,
    CaseExperimentRequest,
    DiagnosisConclusion,
    DiagnosisSubmission,
    InstrumentKind,
    RepairProposalInput,
    TransientProbe,
)
from . import prompts, views
from .loop import Reminder
from .ports import CaseServicePort
from .tools import NoArguments, Tool, ToolOutcome, ToolSet

# Infrastructure failures (after the CaseService's own automatic retry) stop the run as failed.
_FATAL_CODES = {
    ErrorCode.SIMULATION_FAILED,
    ErrorCode.SIMULATION_TIMEOUT,
    ErrorCode.SIMULATOR_UNAVAILABLE,
    ErrorCode.INTERNAL_ERROR,
}

_INSTRUMENT_STATUS = {
    InstrumentKind.OUTPUT_VOLTAGE_SWEEP: "Running output response sweep",
    InstrumentKind.SOURCE_CURRENT_SWEEP: "Running source-current sweep",
    InstrumentKind.TRANSIENT_CAPTURE: "Capturing the two-tone signal",
}
_PROBE_LABEL = {"output_voltage": "output voltage", "source_current": "source current"}

_CLOSED = ApiError(
    code=ErrorCode.CONFLICT,
    message="This case is closing or closed, so the AI run cannot continue.",
)


class RunExperimentArgs(Contract):
    instrument: InstrumentKind = Field(description="Which declared instrument to run.")
    probe: TransientProbe | None = Field(
        default=None,
        description="Required for transient_capture (what to record); omit for sweeps.",
    )

    @model_validator(mode="after")
    def _probe_matches_instrument(self) -> Self:
        needs_probe = self.instrument == InstrumentKind.TRANSIENT_CAPTURE
        if needs_probe and self.probe is None:
            raise ValueError("probe is required for transient_capture")
        if not needs_probe and self.probe is not None:
            raise ValueError("probe is only allowed for transient_capture")
        return self


class ReadObservationArgs(Contract):
    observation_id: ObservationId


class ProposeRepairArgs(Contract):
    component_id: EditableComponentId = Field(
        description="The component to replace with a part of its intended nominal value."
    )
    rationale: str = Field(default="", max_length=600)


@dataclass
class CaseRunContext:
    session_id: str
    case_id: str
    run_id: str
    cases: CaseServicePort
    cancel: CancelToken
    serial: int = 0

    def run_actions(self) -> list[CaseAction]:
        """What this run may do now, from the CaseService's point of view (B-04)."""
        return self.cases.available_actions_for_run(self.session_id, self.case_id, self.run_id)

    def next_request_id(self) -> str:
        """A fresh idempotency key per agent tool call (8–64 chars of [A-Za-z0-9_-])."""
        self.serial += 1
        return f"{self.run_id}_{self.serial}"


def _api_failure(exc: ApiException) -> ToolOutcome:
    return ToolOutcome.failure(exc.error, fatal=exc.error.code in _FATAL_CODES)


def _guarded(
    ctx: CaseRunContext, run: Callable[[Any], Awaitable[ToolOutcome]]
) -> Callable[[Any], Awaitable[ToolOutcome]]:
    """Refuse (fatally) once the case is closing/closed; map closed-view refusals likewise."""

    async def wrapper(args: Any) -> ToolOutcome:
        try:
            if ctx.cases.is_closing_or_closed(ctx.session_id, ctx.case_id):
                return ToolOutcome.failure(_CLOSED, fatal=True)
            return await run(args)
        except ApiException as exc:
            return _api_failure(exc)
        except views.ClosedCaseError:
            return ToolOutcome.failure(_CLOSED, fatal=True)

    return wrapper


def _tool(
    ctx: CaseRunContext,
    *,
    name: str,
    description: str,
    args_model: type[Contract],
    run: Callable[[Any], Awaitable[ToolOutcome]],
    status_text: Callable[[Any], str],
    is_experiment: bool = False,
) -> Tool:
    return Tool(
        name=name,
        description=description,
        args_model=args_model,
        run=_guarded(ctx, run),
        status_text=status_text,
        is_experiment=is_experiment,
    )


def _get_case_tool(ctx: CaseRunContext) -> Tool:
    async def run(_: NoArguments) -> ToolOutcome:
        case = ctx.cases.get_case(ctx.session_id, ctx.case_id)
        return ToolOutcome(
            ok=True,
            content=views.case_view(case, run_actions=ctx.run_actions()),
            summary=f"Read the case: {len(case.observations)} observation(s), "
            f"{case.budget.remaining} of {case.budget.total} experiments left.",
        )

    return _tool(
        ctx,
        name="get_case",
        description="Read the permitted case information: intended design (nominal values), "
        "instruments and what each returns, explanations to consider, evidence tolerances, "
        "budget, and the observations recorded so far.",
        args_model=NoArguments,
        run=run,
        status_text=lambda _: "Reading the case",
    )


def _assess_evidence_tool(ctx: CaseRunContext) -> Tool:
    async def run(_: NoArguments) -> ToolOutcome:
        case = ctx.cases.get_case(ctx.session_id, ctx.case_id)
        content = views.evidence_view(case)
        return ToolOutcome(ok=True, content=content, summary=case.evidence.statement)

    return _tool(
        ctx,
        name="assess_evidence",
        description="Ask trusted code which explanations are supported, contradicted, or "
        "unresolved by the observations recorded so far, using the declared tolerances.",
        args_model=NoArguments,
        run=run,
        status_text=lambda _: "Checking the evidence against each explanation",
    )


def _read_observation_tool(ctx: CaseRunContext) -> Tool:
    async def run(args: ReadObservationArgs) -> ToolOutcome:
        obs = ctx.cases.get_observation(ctx.session_id, ctx.case_id, args.observation_id)
        return ToolOutcome(
            ok=True,
            content=views.observation_view(obs),
            summary=f"Read observation {obs.sequence}: {obs.summary}",
            observation_id=obs.id,
        )

    return _tool(
        ctx,
        name="read_observation",
        description="Read one recorded observation of this case by its ID (does not use budget).",
        args_model=ReadObservationArgs,
        run=run,
        status_text=lambda _: "Reading a recorded observation",
    )


def _run_experiment_tool(ctx: CaseRunContext) -> Tool:
    async def run(args: RunExperimentArgs) -> ToolOutcome:
        request = CaseExperimentRequest(
            instrument=args.instrument, probe=args.probe, client_request_id=ctx.next_request_id()
        )
        response = await ctx.cases.run_experiment(
            ctx.session_id,
            ctx.case_id,
            request,
            requested_by="agent",
            agent_run_id=ctx.run_id,
            cancel=ctx.cancel,
        )
        obs, case = response.observation, response.case
        if obs.status != "succeeded":
            error = obs.error or ApiError(
                code=ErrorCode.SIMULATION_FAILED,
                message="The simulator could not complete this experiment.",
            )
            outcome = ToolOutcome.failure(error, fatal=obs.status == "failed")
            outcome.observation_id = obs.id
            return outcome
        content = views.observation_view(obs)
        content["budget"] = views.budget_view(case)
        return ToolOutcome(
            ok=True,
            content=content,
            summary=obs.summary,
            observation_id=obs.id,
            counted_experiment=obs.counted,
        )

    def status(args: RunExperimentArgs) -> str:
        text = _INSTRUMENT_STATUS[args.instrument]
        if args.probe is not None:
            text += f" ({_PROBE_LABEL[args.probe]})"
        return text

    return _tool(
        ctx,
        name="run_experiment",
        description="Run one allowed instrument on the hidden circuit. Uses one unit of the "
        "experiment budget when it succeeds; invalid requests do not use budget.",
        args_model=RunExperimentArgs,
        run=run,
        status_text=status,
        is_experiment=True,
    )


def _submit_diagnosis_tool(ctx: CaseRunContext) -> Tool:
    async def run(args: DiagnosisSubmission) -> ToolOutcome:
        case = await ctx.cases.submit_diagnosis(
            ctx.session_id, ctx.case_id, args, submitted_by="agent", agent_run_id=ctx.run_id
        )
        dx = case.diagnosis
        if dx is None:
            return ToolOutcome.failure(
                ApiError(code=ErrorCode.INTERNAL_ERROR, message="The diagnosis was not recorded.")
            )
        return ToolOutcome(
            ok=True,
            content={
                "ok": True,
                "diagnosis_id": dx.id,
                "case_status": case.status.value,
                "evidence_check_status": dx.evidence_check.status,
                "available_actions": views.agent_actions(ctx.run_actions()),
            },
            summary=f"Diagnosis submitted ({dx.submission.conclusion.value}). "
            f"{dx.evidence_check.message}",
            diagnosis_id=dx.id,
        )

    return _tool(
        ctx,
        name="submit_diagnosis",
        description="Submit the diagnosis once: fault (with hypothesis_id and at least one "
        "citation), no_fault (with at least one citation), or insufficient_evidence (with "
        "candidate_hypothesis_ids and an optional suggested_instrument).",
        args_model=DiagnosisSubmission,
        run=run,
        status_text=lambda _: "Submitting the diagnosis",
    )


def _propose_repair_tool(ctx: CaseRunContext) -> Tool:
    async def run(args: ProposeRepairArgs) -> ToolOutcome:
        body = RepairProposalInput(component_id=args.component_id, rationale=args.rationale)
        case = await ctx.cases.propose_repair(
            ctx.session_id, ctx.case_id, body, proposed_by="agent", agent_run_id=ctx.run_id
        )
        rp = case.repair_proposal
        if rp is None:
            return ToolOutcome.failure(
                ApiError(code=ErrorCode.INTERNAL_ERROR, message="The repair was not recorded.")
            )
        return ToolOutcome(
            ok=True,
            content={
                "ok": True,
                "repair_id": rp.id,
                "component_id": rp.component_id,
                "value": rp.display_value,
                "note": "Proposed only. The learner applies the repair to verify it.",
            },
            summary=f"Proposed repair: replace {rp.component_id} with {rp.display_value}.",
            repair_id=rp.id,
        )

    return _tool(
        ctx,
        name="propose_repair",
        description="After the diagnosis, propose replacing one component with a part of its "
        "intended nominal value. The learner applies and verifies the repair.",
        args_model=ProposeRepairArgs,
        run=run,
        status_text=lambda _: "Recording the proposed repair",
    )


def investigate_toolset(ctx: CaseRunContext) -> ToolSet:
    return ToolSet(
        "investigate",
        [
            _get_case_tool(ctx),
            _assess_evidence_tool(ctx),
            _run_experiment_tool(ctx),
            _read_observation_tool(ctx),
            _submit_diagnosis_tool(ctx),
            _propose_repair_tool(ctx),
        ],
    )


def hint_toolset(ctx: CaseRunContext) -> ToolSet:
    return ToolSet("hint", [_get_case_tool(ctx), _read_observation_tool(ctx)])


class CaseCitations:
    """Valid citations for a case run: this case's successful observations only."""

    def __init__(self, ctx: CaseRunContext) -> None:
        self._ctx = ctx

    async def __call__(self) -> Mapping[str, QuantityName]:
        try:
            case = self._ctx.cases.get_case(self._ctx.session_id, self._ctx.case_id)
        except ApiException:
            return {}
        return {
            obs.id: views.observation_quantity(obs)
            for obs in case.observations
            if obs.status == "succeeded"
        }


DIAGNOSIS_REMINDER = Reminder(
    kind="diagnosis",
    status="Nemotron replied without submitting a diagnosis; sending one reminder to submit one",
    text=prompts.DIAGNOSIS_REMINDER,
)
REPAIR_REMINDER = Reminder(
    kind="repair",
    status="Nemotron diagnosed a fault without proposing a repair; sending one reminder",
    text=prompts.REPAIR_REMINDER,
)


class InvestigateFinishCheck:
    """Completion check for an INVESTIGATE run (``AgentLoop`` ``finish_check``).

    Reads only the public case (``get_case``) and the run's own permitted actions, exactly what
    the tools see. Returns the diagnosis reminder while the case has no diagnosis and this run
    may still submit one; the repair reminder after a "fault" diagnosis by this run while no
    repair is proposed and this run may still propose one; otherwise ``None``. Never after
    ``no_fault`` or ``insufficient_evidence``. The loop sends each kind at most once.
    """

    def __init__(self, ctx: CaseRunContext) -> None:
        self._ctx = ctx

    async def __call__(self) -> Reminder | None:
        ctx = self._ctx
        try:
            if ctx.cases.is_closing_or_closed(ctx.session_id, ctx.case_id):
                return None
            case = ctx.cases.get_case(ctx.session_id, ctx.case_id)
            actions = ctx.run_actions()
        except ApiException:
            return None
        dx = case.diagnosis
        if dx is None:
            return DIAGNOSIS_REMINDER if "submit_diagnosis" in actions else None
        own = dx.submitted_by == "agent" and dx.agent_run_id == ctx.run_id
        if (
            own
            and dx.submission.conclusion == DiagnosisConclusion.FAULT
            and case.repair_proposal is None
            and "propose_repair" in actions
        ):
            return REPAIR_REMINDER
        return None
