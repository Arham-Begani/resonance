"""Shared evaluation protocol for the fixed-experiment baseline and the Nemotron agent.

Both runners use the same in-process wiring as the app (``CaseService`` + private
``Evaluator`` + the real ngspice simulator; ``AgentService`` registered as the case's run
canceller/liveness exactly as ``resonance.agent.service.startup`` does), the same frozen seeds,
the same standardized symptom, budget (``policy.CASE_EXPERIMENT_BUDGET``), tolerances and
hypothesis catalog. Neither runner reads hidden state: they act only through the public
CaseService API, and the truth is read from ``PublicCase.reveal`` after closing.

Protocol per case
1. ``harness_create_case(session, seed, family)`` → the standardized symptom (uncounted).
2. Investigation:
   - baseline (``run_baseline_case``): Guide-me mode, ``output_voltage_sweep`` then
     ``source_current_sweep`` (2 of 3 budgeted experiments, requested_by="user"); then a
     deterministic decision from the recorded evidence: exactly one remaining hypothesis →
     that conclusion (fault or no_fault) citing both sweeps; otherwise insufficient_evidence
     listing the remaining candidates.
   - agent (``run_agent_case``): one "Investigate for me" run (AgentService, real Nebius
     inference). The agent chooses experiments, diagnosis and repair proposal itself.
3. If a repair proposal exists, the harness (acting as the learner) verifies it once. The
   baseline proposes the nominal part of the component it named; the agent's proposal is
   used as-is. No proposal → no verification.
4. ``close_case`` → reveal → ``CaseOutcome`` (exact identification, abstention, unsupported
   certainty, functional recovery, experiments) plus measured wall-clock latencies.
An infrastructure failure after the CaseService's own automatic retry stops the case (it is
closed and recorded with its error); it is never retried by the harness and never scored as a
diagnosis.
"""

from __future__ import annotations

import asyncio
import platform
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypeVar, cast

from casesets import CaseSet, CaseSpec

from resonance.agent.ports import CaseServicePort
from resonance.agent.service import AgentService
from resonance.api.errors import ApiException
from resonance.challenges.evaluator import Evaluator, EvaluatorStore
from resonance.challenges.hypotheses import HYPOTHESES_BY_ID, named_hypothesis
from resonance.challenges.service import CaseService
from resonance.config import Settings
from resonance.contracts.agent import CaseAgentRunRequest
from resonance.contracts.internal import Simulator
from resonance.contracts.investigation import (
    CaseExperimentRequest,
    CaseStatus,
    DiagnosisSubmission,
    EvidenceCitation,
    InstrumentKind,
    PublicCase,
    RepairProposalInput,
)
from resonance.contracts.policy import (
    AGENT_WALL_TIME_S,
    CASE_EXPERIMENT_BUDGET,
    EVIDENCE_CURRENT_ABS_TOLERANCE_MA,
    EVIDENCE_CURRENT_TOLERANCE_PCT,
    EVIDENCE_GAIN_TOLERANCE_DB,
    EVIDENCE_PHASE_TOLERANCE_DEG,
    REPAIR_GAIN_TOLERANCE_DB,
    REPAIR_PHASE_TOLERANCE_DEG,
)
from resonance.simulation import create_simulator
from resonance.store.artifacts import ArtifactStore
from resonance.store.db import DocumentStore

T = TypeVar("T")
Runner = Literal["baseline", "nemotron"]
BASELINE_PLAN: tuple[InstrumentKind, ...] = (
    InstrumentKind.OUTPUT_VOLTAGE_SWEEP,
    InstrumentKind.SOURCE_CURRENT_SWEEP,
)
_AFTER_DIAGNOSIS = (CaseStatus.DIAGNOSIS_SUBMITTED, CaseStatus.UNRESOLVED)


@dataclass
class Lab:
    settings: Settings
    simulator: Simulator
    cases: CaseService
    agents: AgentService | None
    store: DocumentStore


@asynccontextmanager
async def open_lab(
    settings: Settings, *, simulator: Simulator | None = None, with_agent: bool = False
) -> AsyncIterator[Lab]:
    """In-process services under ``settings.resonance_data_dir`` (use a scratch directory)."""
    settings.resonance_data_dir.mkdir(parents=True, exist_ok=True)
    sim = simulator if simulator is not None else create_simulator(settings)
    sim.info()  # fail loudly if the simulator is unusable
    store = DocumentStore(settings.db_path)
    artifacts = ArtifactStore(
        settings.artifacts_dir,
        store,
        max_count=settings.artifact_max_count,
        max_bytes=settings.artifact_max_mb * 1024 * 1024,
        max_per_session=settings.artifact_max_per_session,
    )
    evaluator = Evaluator(EvaluatorStore(settings.evaluator_db_path), sim, artifacts)
    cases = CaseService(store, evaluator)
    agents: AgentService | None = None
    if with_agent:
        # Same wiring as resonance.agent.service.startup (which reads the service from
        # app.state untyped); the port is the subset of CaseService the agent uses.
        port = cast(CaseServicePort, cases)
        agents = AgentService(settings=settings, store=store, cases=port, explore=None)
        cases.register_run_canceller(agents.cancel_and_wait)
        cases.register_run_liveness(agents.is_live)
    try:
        yield Lab(settings=settings, simulator=sim, cases=cases, agents=agents, store=store)
    finally:
        if agents is not None:
            await agents.shutdown()
        cases.close()
        store.close()


@dataclass
class CaseResult:
    key: str
    runner: Runner
    template_id: str
    nominal: dict[str, float]
    seed: str
    case_id: str | None = None
    completed: bool = False  # the protocol ran to close without an infrastructure stop
    error: str | None = None
    true_condition: str | None = None
    terminal_status: str | None = None
    conclusion: str | None = None
    named_hypothesis: str | None = None
    candidates: list[str] = field(default_factory=list)
    evidence_check: str | None = None
    exact_identification: str | None = None
    true_cause_among_candidates: bool | None = None
    unsupported_certainty: bool | None = None
    repair_component: str | None = None
    functional_recovery: str | None = None
    experiments_used: int = 0
    experiment_sequence: list[str] = field(default_factory=list)
    infrastructure_failures: int = 0
    latency_s: dict[str, float] = field(default_factory=dict)
    agent: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class _Clock:
    def __init__(self, result: CaseResult) -> None:
        self._result = result
        self._start = time.perf_counter()

    async def time(self, label: str, awaitable: Awaitable[T]) -> T:
        start = time.perf_counter()
        try:
            return await awaitable
        finally:
            self._result.latency_s[label] = round(time.perf_counter() - start, 3)

    def finish(self) -> None:
        self._result.latency_s["total"] = round(time.perf_counter() - self._start, 3)


def new_session() -> str:
    return f"evalsession_{secrets.token_hex(12)}"


def _experiment(kind: InstrumentKind, index: int) -> CaseExperimentRequest:
    return CaseExperimentRequest(instrument=kind, client_request_id=f"eval-exp-{index:04d}")


def baseline_submission(case: PublicCase, sweep_ids: list[str]) -> DiagnosisSubmission:
    """Deterministic decision from the recorded (public) evidence."""
    remaining = list(case.evidence.remaining)
    citations = [
        EvidenceCitation(observation_id=sweep_ids[1], quantity="source_current_ma"),
        EvidenceCitation(observation_id=sweep_ids[0], quantity="output_gain_db"),
    ]
    if len(remaining) == 1:
        only = remaining[0]
        if only == "no_fault":
            return DiagnosisSubmission(
                conclusion="no_fault",
                citations=citations,
                explanation="Fixed baseline: only 'no fault' fits every recorded measurement.",
            )
        return DiagnosisSubmission(
            conclusion="fault",
            hypothesis_id=only,
            citations=citations,
            explanation=f"Fixed baseline: only '{only}' fits every recorded measurement.",
        )
    return DiagnosisSubmission(
        conclusion="insufficient_evidence",
        candidate_hypothesis_ids=remaining[:5],
        explanation=(
            "Fixed baseline: "
            + (
                f"{len(remaining)} explanations still fit the recorded measurements."
                if remaining
                else "no catalog explanation fits the recorded measurements."
            )
        ),
    )


def _record_outcome(result: CaseResult, case: PublicCase) -> None:
    result.terminal_status = case.reveal.outcome.terminal_status if case.reveal else None
    if case.diagnosis is not None:
        sub = case.diagnosis.submission
        result.conclusion = sub.conclusion.value
        result.named_hypothesis = named_hypothesis(sub)
        result.candidates = list(sub.candidate_hypothesis_ids)
        result.evidence_check = case.diagnosis.evidence_check.status
    if case.repair_proposal is not None:
        result.repair_component = case.repair_proposal.component_id
    result.experiment_sequence = [
        f"{o.kind}{':' + o.probe if o.probe else ''}" for o in case.observations if o.counted
    ]
    result.infrastructure_failures = case.budget.infrastructure_failures
    if case.reveal is not None:
        outcome = case.reveal.outcome
        result.true_condition = case.reveal.fault.hypothesis_id
        result.exact_identification = outcome.exact_identification
        result.true_cause_among_candidates = outcome.true_cause_among_candidates
        result.unsupported_certainty = outcome.unsupported_certainty
        result.functional_recovery = outcome.functional_recovery
        result.experiments_used = outcome.experiments_used


async def _close(lab: Lab, session: str, result: CaseResult, clock: _Clock) -> None:
    if result.case_id is None:
        return
    closed = await clock.time("close", lab.cases.close_case(session, result.case_id))
    _record_outcome(result, closed)


async def _verify_if_proposed(lab: Lab, session: str, case: PublicCase, clock: _Clock) -> None:
    if case.repair_proposal is not None and case.repair is None and case.status in (
        _AFTER_DIAGNOSIS
    ):
        await clock.time("verify_repair", lab.cases.verify_repair(session, case.id))


async def run_baseline_case(lab: Lab, spec: CaseSpec) -> CaseResult:
    result = CaseResult(
        key=spec.key,
        runner="baseline",
        template_id=spec.template_id,
        nominal={"R1": spec.expected_nominal[0], "C1": spec.expected_nominal[1]},
        seed=spec.seed,
    )
    session = new_session()
    clock = _Clock(result)
    try:
        case = await clock.time(
            "symptom", lab.cases.harness_create_case(session, spec.seed, spec.template_id)
        )
        result.case_id = case.id
        case = await lab.cases.set_mode(session, case.id, "guide")
        sweep_ids: list[str] = []
        for i, kind in enumerate(BASELINE_PLAN):
            response = await clock.time(
                kind.value,
                lab.cases.run_experiment(
                    session, case.id, _experiment(kind, i), requested_by="user"
                ),
            )
            sweep_ids.append(response.observation.id)
            case = response.case
        submission = baseline_submission(case, sweep_ids)
        case = await clock.time(
            "diagnosis",
            lab.cases.submit_diagnosis(session, case.id, submission, submitted_by="user"),
        )
        if submission.conclusion.value == "fault" and submission.hypothesis_id is not None:
            component = HYPOTHESES_BY_ID[submission.hypothesis_id].component_id
            assert component is not None
            case = await lab.cases.propose_repair(
                session,
                case.id,
                RepairProposalInput(
                    component_id=component,
                    rationale="Fixed baseline: replace the named component with its nominal part.",
                ),
                proposed_by="user",
            )
            await _verify_if_proposed(lab, session, case, clock)
        result.completed = True
    except ApiException as exc:
        result.error = f"{exc.error.code.value}: {exc.error.message}"
    finally:
        await _close(lab, session, result, clock)
        clock.finish()
    return result


async def run_agent_case(lab: Lab, spec: CaseSpec, *, message: str | None = None) -> CaseResult:
    """One genuine 'Investigate for me' run (real Nebius inference) on a frozen case."""
    if lab.agents is None:
        raise RuntimeError("the lab was opened without an agent service")
    result = CaseResult(
        key=spec.key,
        runner="nemotron",
        template_id=spec.template_id,
        nominal={"R1": spec.expected_nominal[0], "C1": spec.expected_nominal[1]},
        seed=spec.seed,
    )
    session = new_session()
    clock = _Clock(result)
    try:
        case = await clock.time(
            "symptom", lab.cases.harness_create_case(session, spec.seed, spec.template_id)
        )
        result.case_id = case.id
        start = time.perf_counter()
        run = await lab.agents.start_case_run(
            session, case.id, CaseAgentRunRequest(kind="investigate", message=message)
        )
        handle = lab.agents.handle_for(session, run.id)
        try:
            await asyncio.wait_for(handle.finished.wait(), timeout=AGENT_WALL_TIME_S + 30.0)
        except TimeoutError:
            await lab.agents.cancel(session, run.id)
        result.latency_s["agent_run"] = round(time.perf_counter() - start, 3)
        run = lab.agents.get_run(session, run.id)
        result.agent = {
            "run_id": run.id,
            "source": run.source,
            "model": run.model,
            "status": run.status,
            "error": None if run.error is None else f"{run.error.code.value}: {run.error.message}",
            "usage": run.usage.model_dump(),
            "final_message": run.final_message,
        }
        case = lab.cases.get_case(session, case.id)
        await _verify_if_proposed(lab, session, case, clock)
        result.completed = run.status == "completed"
    except ApiException as exc:
        result.error = f"{exc.error.code.value}: {exc.error.message}"
    finally:
        await _close(lab, session, result, clock)
        clock.finish()
    return result


def scratch_settings(base: Settings, data_dir: Path) -> Settings:
    """``base`` with every data path under ``data_dir`` and no web build."""
    return base.model_copy(
        update={"resonance_data_dir": data_dir, "web_dist_dir": data_dir / "no-web"}
    )


ProgressFn = Callable[[str], None]


def protocol_description(runner: Runner) -> str:
    plan = " → ".join(k.value for k in BASELINE_PLAN)
    if runner == "baseline":
        return (
            f"standardized symptom; Guide-me mode; fixed experiments {plan} (2 of "
            f"{CASE_EXPERIMENT_BUDGET} budgeted); unique remaining hypothesis → that conclusion, "
            "else insufficient_evidence with the remaining candidates; nominal repair of the "
            "named component verified once; close and reveal."
        )
    return (
        "standardized symptom; one 'Investigate for me' Nemotron run (real Nebius inference, "
        f"budget {CASE_EXPERIMENT_BUDGET}, policy caps); the agent's own repair proposal (if "
        "any) verified once by the harness; close and reveal."
    )


async def run_set(
    runner: Runner,
    case_set: CaseSet,
    specs: Sequence[CaseSpec],
    settings: Settings,
    *,
    simulator: Simulator | None = None,
    message: str | None = None,
    progress: ProgressFn = print,
) -> dict[str, Any]:
    """Run every case sequentially and return the run record (without its summary)."""
    started = datetime.now(UTC)
    results: list[dict[str, Any]] = []
    async with open_lab(settings, simulator=simulator, with_agent=runner == "nemotron") as lab:
        sim_info = lab.simulator.info()
        for spec in specs:
            if runner == "baseline":
                result = await run_baseline_case(lab, spec)
            else:
                result = await run_agent_case(lab, spec, message=message)
            results.append(result.as_dict())
            progress(
                f"{spec.key}: truth={result.true_condition} conclusion={result.conclusion}"
                f"/{result.named_hypothesis} identification={result.exact_identification} "
                f"recovery={result.functional_recovery} experiments={result.experiments_used} "
                f"total={result.latency_s.get('total', 0.0):.1f}s"
                + (f" ERROR {result.error}" if result.error else "")
            )
    finished = datetime.now(UTC)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    return {
        "run_id": f"{runner}-{case_set.name}-{stamp}",
        "runner": runner,
        "model": settings.nebius_model if runner == "nemotron" else None,
        "case_set": {
            "name": case_set.name,
            "sha256": case_set.sha256,
            "generator_version": case_set.generator_version,
            "cases": len(specs),
            "selected_keys": [s.key for s in specs],
        },
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "environment": {
            "simulator": f"{sim_info.name} {sim_info.version}",
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "protocol": {
            "description": protocol_description(runner),
            "budget": CASE_EXPERIMENT_BUDGET,
            "evidence_tolerances": {
                "gain_db": EVIDENCE_GAIN_TOLERANCE_DB,
                "phase_deg": EVIDENCE_PHASE_TOLERANCE_DEG,
                "current_pct": EVIDENCE_CURRENT_TOLERANCE_PCT,
                "current_abs_ma": EVIDENCE_CURRENT_ABS_TOLERANCE_MA,
            },
            "repair_tolerances": {
                "gain_db": REPAIR_GAIN_TOLERANCE_DB,
                "phase_deg": REPAIR_PHASE_TOLERANCE_DEG,
            },
        },
        "results": results,
    }
