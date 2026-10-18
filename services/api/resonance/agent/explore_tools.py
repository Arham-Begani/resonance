"""EXPLORE tool set: inspect public session revisions, set a goal, preview value proposals,
simulate revisions already in the learner's history, and compare recorded experiments.

Proposals are never applied by the agent: the learner applies one through ``POST
/api/revisions`` with its ``proposal_id``. Case revisions (``origin="case_intended"``) are not
reachable from these tools, so Explore can never touch an investigation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import Field, model_validator

from ..api.errors import ApiException
from ..circuits import revisions as circuit_revisions
from ..circuits.templates import parse_component_value
from ..circuits.units import ValueParseError
from ..contracts.agent import EditProposal, PredictedToneGain, ProposedChange
from ..contracts.circuits import EditableComponentId, Revision
from ..contracts.common import (
    ApiError,
    Contract,
    ErrorCode,
    ExperimentId,
    QuantityName,
    RevisionId,
    new_id,
)
from ..contracts.experiments import (
    ExperimentComparison,
    ExperimentRecord,
    Goal,
    GoalConstraint,
    GoalInput,
)
from ..contracts.internal import CancelToken
from ..explore import goals as explore_goals
from ..simulation import analytic
from ..store.db import DocumentStore, utcnow
from ..store.session_state import load_session_state, save_session_state
from . import views
from .ports import ExplorePort
from .tools import Tool, ToolOutcome, ToolSet


class InspectCircuitArgs(Contract):
    revision_id: RevisionId


class ValueChange(Contract):
    component_id: EditableComponentId
    value: str | float = Field(
        description="New value, e.g. '2.2k', '47 nF', or a number in SI units (ohms/farads)."
    )


class ProposeValuesArgs(Contract):
    changes: list[ValueChange] = Field(min_length=1, max_length=2)
    rationale: str = Field(max_length=600)

    @model_validator(mode="after")
    def _unique(self) -> ProposeValuesArgs:
        ids = [c.component_id for c in self.changes]
        if len(set(ids)) != len(ids):
            raise ValueError("each component may appear only once")
        return self


class RunExploreExperimentArgs(Contract):
    revision_id: RevisionId


class CompareExperimentsArgs(Contract):
    a_id: ExperimentId = Field(description="Earlier experiment (A, before).")
    b_id: ExperimentId = Field(description="Later experiment (B, after).")


@dataclass
class ExploreRunContext:
    session_id: str
    run_id: str
    revision_id: str
    goal_id: str | None
    store: DocumentStore
    explore: ExplorePort
    cancel: CancelToken


def _not_found(what: str) -> ToolOutcome:
    return ToolOutcome.failure(
        ApiError(code=ErrorCode.NOT_FOUND, message=f"{what} was not found in this session.")
    )


def load_session_revision(store: DocumentStore, session_id: str, revision_id: str) -> Revision:
    """A public session revision usable by Explore (case designs are excluded)."""
    rev = circuit_revisions.find_revision(store, session_id, revision_id)
    if rev is None or rev.origin == "case_intended":
        raise ApiException(ErrorCode.NOT_FOUND, "Revision was not found.")
    return rev


def _revision_experiments(ctx: ExploreRunContext, revision_id: str) -> list[dict[str, Any]]:
    records = ctx.store.list_docs("experiment", ctx.session_id, ExperimentRecord, limit=200)
    return [
        {
            "experiment_id": r.id,
            "status": r.status,
            "goal_passed": r.goal.passed if r.goal else None,
        }
        for r in records
        if r.revision_id == revision_id
    ][-10:]


def _inspect_tool(ctx: ExploreRunContext) -> Tool:
    async def run(args: InspectCircuitArgs) -> ToolOutcome:
        try:
            rev = load_session_revision(ctx.store, ctx.session_id, args.revision_id)
        except ApiException:
            return _not_found("Revision")
        content = views.revision_view(rev, label="Circuit revision in the learner's history.")
        content["parent_id"] = rev.parent_id
        content["changes_from_parent"] = [
            {"component_id": c.component_id, "from": c.from_display, "to": c.to_display}
            for c in rev.changes
        ]
        content["experiments"] = _revision_experiments(ctx, rev.id)
        return ToolOutcome(
            ok=True,
            content=content,
            summary=f"Inspected revision {rev.id}.",
        )

    return Tool(
        name="inspect_circuit",
        description="Read a circuit revision from the learner's history: components, values, "
        "connections, test tones, analytical cutoff, and experiments already run on it.",
        args_model=InspectCircuitArgs,
        run=run,
        status_text=lambda _: "Inspecting the circuit",
    )


def _template_of(ctx: ExploreRunContext) -> str:
    try:
        return load_session_revision(ctx.store, ctx.session_id, ctx.revision_id).template_id
    except ApiException:
        return "rc_lowpass"


def _set_goal_tool(ctx: ExploreRunContext) -> Tool:
    async def run(args: GoalInput) -> ToolOutcome:
        goal = explore_goals.goal_from_input(args, origin="agent")
        feasibility = explore_goals.analyze_feasibility(goal, _template_of(ctx))
        state = load_session_state(ctx.store, ctx.session_id)
        state.goal = goal
        save_session_state(ctx.store, ctx.session_id, state)
        ctx.goal_id = goal.id
        return ToolOutcome(
            ok=True,
            content={
                "ok": True,
                "goal_id": goal.id,
                "title": goal.title,
                "constraints": [c.model_dump() for c in goal.constraints],
                "feasibility": views.feasibility_view(feasibility),
            },
            summary=f"Set the goal “{goal.title}”. {feasibility.explanation}",
        )

    return Tool(
        name="set_goal",
        description="Record the learner's filtering goal as 1–4 constraints and return an "
        "analytical feasibility prediction for this circuit family. preserve: attenuation at "
        "the frequency must be at most limit_db; attenuate: at least limit_db.",
        args_model=GoalInput,
        run=run,
        status_text=lambda _: "Recording the goal",
    )


def _predicted_gains(
    rev: Revision, values: Mapping[str, float], goal: Goal | None
) -> list[PredictedToneGain]:
    points: list[tuple[float, str]] = [(t.frequency_hz, t.label) for t in rev.source.tones]
    if goal is not None:
        for c in goal.constraints:
            if all(abs(c.frequency_hz - f) > 1e-9 for f, _ in points):
                points.append((c.frequency_hz, c.label))
    freqs = [f for f, _ in points]
    gains = analytic.gain_db(analytic.transfer(rev.template_id, freqs, values))
    return [
        PredictedToneGain(frequency_hz=f, label=label, predicted_gain_db=float(g))
        for (f, label), g in zip(points, gains, strict=True)
    ]


def _goal_pass(goal: Goal | None, predicted: list[PredictedToneGain]) -> bool | None:
    if goal is None:
        return None
    by_freq = {p.frequency_hz: p.predicted_gain_db for p in predicted}
    for c in goal.constraints:
        gain = by_freq.get(c.frequency_hz)
        if gain is None:
            return None
        if not _constraint_met(c, -gain):
            return False
    return True


def _constraint_met(c: GoalConstraint, attenuation_db: float) -> bool:
    return attenuation_db <= c.limit_db if c.kind == "preserve" else attenuation_db >= c.limit_db


def _propose_tool(ctx: ExploreRunContext) -> Tool:
    async def run(args: ProposeValuesArgs) -> ToolOutcome:
        try:
            base = load_session_revision(ctx.store, ctx.session_id, ctx.revision_id)
        except ApiException:
            return _not_found("Revision")
        values: dict[str, float] = {}
        try:
            for change in args.changes:
                value_si, _unit = parse_component_value(
                    base.template_id, change.component_id, change.value
                )
                values[change.component_id] = value_si
            # Validates bounds and the joint cutoff constraint without storing anything.
            preview = circuit_revisions.build_revision(
                base.template_id,
                values,
                origin="agent_proposal_applied",
                parent=base,
                edited=tuple(values),
            )
        except ValueParseError as exc:
            return ToolOutcome.failure(ApiError(code=ErrorCode.VALIDATION_ERROR, message=str(exc)))
        if not preview.changes:
            return ToolOutcome.failure(
                ApiError(
                    code=ErrorCode.VALIDATION_ERROR,
                    message="The proposed values equal the current values; nothing would change.",
                )
            )
        goal = load_session_state(ctx.store, ctx.session_id).goal
        predicted = _predicted_gains(base, circuit_revisions.revision_values(preview), goal)
        proposal = EditProposal(
            id=new_id("prop"),
            base_revision_id=base.id,
            changes=[
                ProposedChange(
                    component_id=ch.component_id,
                    from_value_si=ch.from_value_si,
                    to_value_si=ch.to_value_si,
                    from_display=ch.from_display,
                    to_display=ch.to_display,
                )
                for ch in preview.changes
            ],
            rationale=args.rationale,
            predicted=predicted,
            predicted_goal_pass=_goal_pass(goal, predicted),
            status="pending",
            created_at=utcnow(),
        )
        ctx.store.put("proposal", proposal.id, ctx.session_id, proposal)
        change_text = ", ".join(
            f"{c.component_id} {c.from_display} → {c.to_display}" for c in proposal.changes
        )
        return ToolOutcome(
            ok=True,
            content={
                "ok": True,
                "proposal_id": proposal.id,
                "applied": False,
                "changes": [
                    {"component_id": c.component_id, "from": c.from_display, "to": c.to_display}
                    for c in proposal.changes
                ],
                "predicted_cutoff_hz": views.sig(preview.prediction.cutoff_hz),
                "predicted": [
                    {
                        "label": p.label,
                        "frequency_hz": p.frequency_hz,
                        "predicted_gain_db": views.sig(p.predicted_gain_db),
                    }
                    for p in predicted
                ],
                "predicted_goal_pass": proposal.predicted_goal_pass,
                "note": "Analytical predictions, not measurements. The learner decides whether "
                "to apply this proposal; it must then be simulated to measure it.",
            },
            summary=f"Proposed {change_text} (not applied).",
            proposal_id=proposal.id,
            proposal=proposal,
        )

    return Tool(
        name="propose_values",
        description="Preview new values for R1 and/or C1 on the current revision. Returns "
        "analytical predictions. The proposal is NOT applied; the learner decides.",
        args_model=ProposeValuesArgs,
        run=run,
        status_text=lambda _: "Preparing a proposal",
    )


def _run_experiment_tool(ctx: ExploreRunContext) -> Tool:
    async def run(args: RunExploreExperimentArgs) -> ToolOutcome:
        try:
            load_session_revision(ctx.store, ctx.session_id, args.revision_id)
        except ApiException:
            return _not_found("Revision")
        try:
            record = await ctx.explore.run_experiment(
                ctx.session_id, args.revision_id, ctx.goal_id, ctx.cancel
            )
        except ApiException as exc:
            return ToolOutcome.failure(exc.error)
        if record.status != "succeeded":
            error = record.error or ApiError(
                code=ErrorCode.SIMULATION_FAILED,
                message="The simulator could not complete this run.",
            )
            outcome = ToolOutcome.failure(error)
            outcome.experiment_id = record.id
            return outcome
        goal_text = ""
        if record.goal is not None:
            goal_text = " Goal met." if record.goal.passed else " Goal not met."
        return ToolOutcome(
            ok=True,
            content=views.experiment_view(record),
            summary=f"Simulated revision {record.revision_id}.{goal_text}",
            experiment_id=record.id,
            counted_experiment=True,
        )

    return Tool(
        name="run_experiment",
        description="Simulate a revision that already exists in the learner's history (AC "
        "sweep, two-tone capture, audio). Returns measured tone gains, a subset of the "
        "frequency response, and the goal evaluation.",
        args_model=RunExploreExperimentArgs,
        run=run,
        status_text=lambda _: "Running the simulation",
        is_experiment=True,
    )


def _compare_tool(ctx: ExploreRunContext) -> Tool:
    async def run(args: CompareExperimentsArgs) -> ToolOutcome:
        try:
            cmp = await ctx.explore.compare(ctx.session_id, args.a_id, args.b_id)
        except ApiException as exc:
            return ToolOutcome.failure(exc.error)
        return ToolOutcome(ok=True, content=views.comparison_view(cmp), summary=cmp.summary)

    return Tool(
        name="compare_experiments",
        description="Compare two recorded experiments (A before, B after): component changes "
        "and measured tone gain differences.",
        args_model=CompareExperimentsArgs,
        run=run,
        status_text=lambda _: "Comparing experiments",
    )


def explore_toolset(ctx: ExploreRunContext) -> ToolSet:
    return ToolSet(
        "explore",
        [
            _inspect_tool(ctx),
            _set_goal_tool(ctx),
            _propose_tool(ctx),
            _run_experiment_tool(ctx),
            _compare_tool(ctx),
        ],
    )


class ExploreCitations:
    """Valid citations for an explore run: this session's recorded experiments only."""

    def __init__(self, ctx: ExploreRunContext) -> None:
        self._ctx = ctx

    async def __call__(self) -> Mapping[str, QuantityName]:
        records = self._ctx.store.list_docs(
            "experiment", self._ctx.session_id, ExperimentRecord, limit=200, newest_first=True
        )
        return {r.id: views.EXPERIMENT_QUANTITY for r in records if r.status == "succeeded"}


class AppExplorePort:
    """Adapter over ``resonance.explore.service`` using the app's shared singletons."""

    def __init__(self, app_state: Any) -> None:
        self._state = app_state

    async def run_experiment(
        self,
        session_id: str,
        revision_id: str,
        goal_id: str | None,
        cancel: CancelToken | None,
    ) -> ExperimentRecord:
        from ..explore.service import run_explore_experiment

        store: DocumentStore = self._state.store
        revision = load_session_revision(store, session_id, revision_id)
        goal: Goal | None = None
        if goal_id is not None:
            current = load_session_state(store, session_id).goal
            goal = current if current.id == goal_id else None
        return await run_explore_experiment(
            simulator=self._state.simulator,
            store=store,
            artifacts=self._state.artifacts,
            session_id=session_id,
            revision=revision,
            circuit=circuit_revisions.revision_to_circuit(revision),
            goal=goal,
            cancel=cancel,
        )

    async def compare(self, session_id: str, a_id: str, b_id: str) -> ExperimentComparison:
        from ..explore.service import compare_experiments, get_experiment

        store: DocumentStore = self._state.store
        a = get_experiment(store, session_id, a_id)
        b = get_experiment(store, session_id, b_id)
        rev_a = load_session_revision(store, session_id, a.revision_id)
        rev_b = load_session_revision(store, session_id, b.revision_id)
        # Comparison audio rendering is CPU work: keep it off the event loop.
        return await asyncio.to_thread(
            compare_experiments,
            artifacts=self._state.artifacts,
            session_id=session_id,
            a=a,
            b=b,
            rev_a=rev_a,
            rev_b=rev_b,
        )
