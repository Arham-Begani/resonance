"""Shared helpers for agent tests (imported by conftest.py and the test modules).

TEST-ONLY doubles live here:
- ``ScriptedProvider``: scripted OpenAI-compatible responses served to the real
  ``NebiusChatClient`` through ``httpx.MockTransport``. It is not an AI and is never used
  outside tests.
- ``FakeCaseService``: an in-memory double of the CaseService interface that returns PUBLIC
  contract models only. Its "hidden" fault exists only to produce measured numbers and a reveal
  that the agent must never see.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from resonance.agent.client import ChatClient, create_chat_client
from resonance.agent.service import AgentService, limits_for
from resonance.api.errors import ApiException, not_found
from resonance.challenges.hypotheses import hypothesis_catalog
from resonance.circuits.revisions import build_revision
from resonance.config import Settings
from resonance.contracts.agent import AgentLimits, AgentRunKind
from resonance.contracts.circuits import TemplateId
from resonance.contracts.common import ErrorCode, SimulatorInfo, new_id
from resonance.contracts.experiments import AudioClip, AudioComparison
from resonance.contracts.internal import CancelToken
from resonance.contracts.investigation import (
    Budget,
    CaseExperimentRequest,
    CaseOutcome,
    CaseReveal,
    CaseStatus,
    DiagnosisConclusion,
    DiagnosisRecord,
    DiagnosisSubmission,
    EvidenceCheck,
    EvidencePolicy,
    EvidenceState,
    FaultReveal,
    HypothesisAssessment,
    InstrumentKind,
    InstrumentSpec,
    InvestigationMode,
    Observation,
    ObservationResponse,
    ObservationSettings,
    ObservedTone,
    ObservedWaveform,
    PublicCase,
    RepairProposal,
    RepairProposalInput,
    SweepObservation,
    TransientObservation,
)
from resonance.contracts.policy import TEMPLATE_TONES
from resonance.simulation import analytic
from resonance.store.db import DocumentStore

TEST_API_KEY = "sk-test-SECRET-KEY-0123456789"
TEST_MODEL = "test/nemotron-scripted"
SECRET_SEED = "seed-SECRET-4242"
WAVEFORM_MARKER = 0.123456789  # appears only in raw waveform arrays
GRID = [20.0 * 10 ** (k / 10) for k in range(31)]
NOMINAL = {"R1": 1000.0, "C1": 1e-7}
HIDDEN = {"R1": 2000.0, "C1": 1e-7}  # the fake's hidden fault: R1 doubled
# The real PUBLIC hypothesis catalog (both families, incl. the open capacitor).
HYPOTHESIS_IDS = [h.id for h in hypothesis_catalog()]


# --------------------------------------------------------------------------------------
# Scripted provider (TEST-ONLY)
# --------------------------------------------------------------------------------------


def completion(
    content: str | None = None,
    tool_calls: list[tuple[str, Any]] | None = None,
    *,
    usage: tuple[int, int] | None = (120, 30),
    reasoning: str | None = None,
    finish_reason: str | None = None,
) -> dict[str, Any]:
    """An OpenAI-compatible chat.completion body (TEST-ONLY scripted output)."""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    if tool_calls:
        message["tool_calls"] = [
            {
                "id": f"call_{i}_{name}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": args if isinstance(args, str) else json.dumps(args),
                },
            }
            for i, (name, args) in enumerate(tool_calls)
        ]
    body: dict[str, Any] = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": TEST_MODEL,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason or ("tool_calls" if tool_calls else "stop"),
            }
        ],
    }
    if usage is not None:
        body["usage"] = {
            "prompt_tokens": usage[0],
            "completion_tokens": usage[1],
            "total_tokens": usage[0] + usage[1],
        }
    return body


def abstain(*candidates: str) -> dict[str, Any]:
    """A scripted ``submit_diagnosis`` call with conclusion insufficient_evidence (TEST-ONLY).

    Investigate runs get one completion reminder when they end without a diagnosis; tests that
    are not about that reminder submit this abstention before their final text."""
    args: dict[str, Any] = {
        "conclusion": "insufficient_evidence",
        "candidate_hypothesis_ids": list(candidates or ("r1_increased", "c1_increased")),
        "explanation": "More than one explanation still fits the recorded evidence.",
    }
    return completion(None, [("submit_diagnosis", args)])


class Hang:
    """Step that keeps the request open (until cancelled or ``seconds`` elapse)."""

    def __init__(self, seconds: float = 30.0) -> None:
        self.seconds = seconds


class Status:
    """Step that returns an HTTP error status."""

    def __init__(self, code: int) -> None:
        self.code = code


Step = dict[str, Any] | Hang | Status | Callable[[dict[str, Any]], dict[str, Any]]


class ScriptedProvider:
    """TEST-ONLY: serves scripted chat completions and records every request body."""

    def __init__(self, steps: list[Step] | None = None) -> None:
        self.steps: list[Step] = list(steps or [])
        self.requests: list[dict[str, Any]] = []
        self.started = asyncio.Event()
        self.cancelled = 0

    async def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        self.started.set()
        if not self.steps:
            return httpx.Response(500, json={"error": {"message": "script exhausted"}})
        step = self.steps.pop(0)
        if isinstance(step, Hang):
            try:
                await asyncio.sleep(step.seconds)
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
            return httpx.Response(200, json=completion("late reply"))
        if isinstance(step, Status):
            return httpx.Response(step.code, json={"error": {"message": f"HTTP {step.code}"}})
        if callable(step):
            return httpx.Response(200, json=step(body))
        return httpx.Response(200, json=step)

    def client_factory(self) -> Callable[[Settings], ChatClient]:
        def factory(settings: Settings) -> ChatClient:
            transport = httpx.MockTransport(self.handler)
            return create_chat_client(settings, http_client=httpx.AsyncClient(transport=transport))

        return factory

    def tool_names(self, index: int = 0) -> set[str]:
        return {t["function"]["name"] for t in self.requests[index].get("tools", [])}


def last_tool_result(body: dict[str, Any]) -> dict[str, Any]:
    """The JSON content of the most recent tool message the loop sent to the provider."""
    for message in reversed(body["messages"]):
        if message["role"] == "tool":
            result: dict[str, Any] = json.loads(message["content"])
            return result
    raise AssertionError("no tool message")


# --------------------------------------------------------------------------------------
# Fake CaseService (TEST-ONLY)
# --------------------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _audio(source_id: str) -> AudioComparison:
    return AudioComparison(
        id=new_id("cmp"),
        clips=[
            AudioClip(
                artifact_id="art_SECRETARTIFACT01",
                url="/api/artifacts/art_SECRETARTIFACT01",
                role="observed",
                label="Observed",
                source_id=source_id,
                duration_s=1.2,
                sample_rate_hz=48_000,
                peak_fs=0.5,
                clipped=False,
            )
        ],
        playback_gain_fs_per_v=1.0,
        fade_ms=15.0,
        note="Shared gain.",
    )


class FakeCaseService:
    """TEST-ONLY in-memory double of the CaseService interface (public models only)."""

    def __init__(
        self,
        session_id: str,
        *,
        mode: InvestigationMode | None = None,
        budget: int = 3,
        template_id: TemplateId = "rc_lowpass",
    ) -> None:
        self.session_id = session_id
        self.case_id = new_id("case")
        self.template_id: TemplateId = template_id
        self.revision = build_revision(template_id, NOMINAL, origin="case_intended", frozen=True)
        self.status = CaseStatus.OBSERVING if mode else CaseStatus.READY
        self.mode: InvestigationMode | None = mode
        self.budget_total = budget
        self.budget_used = 0
        self.hints_used = 0
        self.observations: list[Observation] = [self._symptom()]
        self.diagnosis: DiagnosisRecord | None = None
        self.repair_proposal: RepairProposal | None = None
        self.active_run_id: str | None = None
        self.closing = False
        self.reveal: CaseReveal | None = None
        self.canceller: Callable[[str], Awaitable[None]] | None = None
        self.liveness: Callable[[str], bool] | None = None
        self.calls: list[str] = []
        self.experiment_requests: list[tuple[CaseExperimentRequest, str, str | None]] = []
        self.experiment_gate: asyncio.Event | None = None
        self.experiment_started = asyncio.Event()

    # --- helpers ------------------------------------------------------------------------

    def _check(self, session_id: str, case_id: str) -> None:
        if session_id != self.session_id or case_id != self.case_id:
            raise not_found("Case")

    def _symptom(self) -> Observation:
        obs_id = new_id("obs")
        tones = []
        for tone in TEMPLATE_TONES[self.template_id]:
            f, amp = tone.frequency_hz, tone.amplitude_v
            h = analytic.transfer(self.template_id, f, HIDDEN)
            e = analytic.transfer(self.template_id, f, NOMINAL)
            tones.append(
                ObservedTone(
                    frequency_hz=f,
                    label=tone.label,
                    role=tone.role,
                    amplitude=float(abs(h)) * amp,
                    amplitude_unit="V",
                    gain_db=float(analytic.gain_db(h)),
                    phase_deg=float(analytic.phase_deg(h)),
                    expected_amplitude=float(abs(e)) * amp,
                    expected_gain_db=float(analytic.gain_db(e)),
                    expected_phase_deg=float(analytic.phase_deg(e)),
                )
            )
        return Observation(
            id=obs_id,
            case_id=self.case_id,
            sequence=0,
            kind="symptom",
            probe="output_voltage",
            counted=False,
            requested_by="system",
            status="succeeded",
            created_at=_now(),
            simulator=SimulatorInfo(version="47"),
            settings=ObservationSettings(),
            transient=TransientObservation(
                probe="output_voltage",
                tones=tones,
                waveform=ObservedWaveform(
                    sample_rate_hz=48_000,
                    start_s=0.0,
                    unit="V",
                    input_v=[WAVEFORM_MARKER] * 8,
                    observed=[WAVEFORM_MARKER] * 8,
                    expected=[WAVEFORM_MARKER] * 8,
                ),
                audio=_audio(obs_id),
            ),
            summary="The whistle is quieter than expected at the output.",
        )

    def _sweep(self, kind: InstrumentKind, run_id: str | None, requested_by: str) -> Observation:
        if kind == InstrumentKind.OUTPUT_VOLTAGE_SWEEP:
            h = analytic.transfer(self.template_id, GRID, HIDDEN)
            e = analytic.transfer(self.template_id, GRID, NOMINAL)
            sweep = SweepObservation(
                quantity="output_gain",
                frequency_hz=GRID,
                magnitude=[float(x) for x in analytic.gain_db(h)],
                magnitude_unit="dB",
                phase_deg=[float(x) for x in analytic.phase_deg(h)],
                expected_magnitude=[float(x) for x in analytic.gain_db(e)],
                expected_phase_deg=[float(x) for x in analytic.phase_deg(e)],
            )
        else:
            i = analytic.source_current(self.template_id, GRID, HIDDEN)
            e = analytic.source_current(self.template_id, GRID, NOMINAL)
            sweep = SweepObservation(
                quantity="source_current",
                frequency_hz=GRID,
                magnitude=[float(abs(x)) * 1000 for x in i],
                magnitude_unit="mA",
                phase_deg=[float(x) for x in analytic.phase_deg(i)],
                expected_magnitude=[float(abs(x)) * 1000 for x in e],
                expected_phase_deg=[float(x) for x in analytic.phase_deg(e)],
            )
        return Observation(
            id=new_id("obs"),
            case_id=self.case_id,
            sequence=len(self.observations),
            kind=kind.value,  # type: ignore[arg-type]
            counted=True,
            requested_by=requested_by,  # type: ignore[arg-type]
            agent_run_id=run_id,
            status="succeeded",
            created_at=_now(),
            simulator=SimulatorInfo(version="47"),
            settings=ObservationSettings(),
            sweep=sweep,
            summary=f"Measured {kind.value.replace('_', ' ')} on 31 frequencies.",
        )

    def _actions(self) -> list[Any]:
        if self.status == CaseStatus.CLOSED:
            return []
        if self.status == CaseStatus.READY:
            return ["choose_mode", "close"]
        actions: list[Any] = []
        active = self.active_run_id is not None
        if self.status == CaseStatus.OBSERVING and not active:
            if self.budget_used < self.budget_total:
                actions.append("run_experiment")
                if self.mode == "agent":
                    actions.append("start_agent")
            if self.mode == "guide":
                actions.append("request_hint")
            actions.append("submit_diagnosis")
        elif self.status in (CaseStatus.DIAGNOSIS_SUBMITTED, CaseStatus.UNRESOLVED) and not active:
            actions.append("propose_repair")
        if active:
            actions.append("cancel_agent")
        actions.append("close")
        return actions

    def public(self) -> PublicCase:
        return PublicCase(
            id=self.case_id,
            status=self.status,
            mode=self.mode,
            created_at=_now(),
            updated_at=_now(),
            intended_revision=self.revision,
            instruments=[
                InstrumentSpec(
                    kind=InstrumentKind.OUTPUT_VOLTAGE_SWEEP,
                    title="Output response sweep",
                    description="Output gain and phase on the declared grid.",
                    returns=["output_gain_db", "output_phase_deg"],
                    frequency_hz=GRID,
                ),
                InstrumentSpec(
                    kind=InstrumentKind.SOURCE_CURRENT_SWEEP,
                    title="Source current sweep",
                    description="Source current magnitude and phase on the declared grid.",
                    returns=["source_current_ma", "source_current_phase_deg"],
                    frequency_hz=GRID,
                ),
                InstrumentSpec(
                    kind=InstrumentKind.TRANSIENT_CAPTURE,
                    title="Two-tone capture",
                    description="Tone levels of the two-tone signal.",
                    returns=["tone_gain_db", "tone_phase_deg", "tone_current_ma"],
                    probes=["output_voltage", "source_current"],
                ),
            ],
            hypotheses=hypothesis_catalog(),
            policy=EvidencePolicy(
                gain_tolerance_db=0.5,
                phase_tolerance_deg=3.0,
                current_tolerance_pct=3.0,
                current_abs_tolerance_ma=1e-6,
                tone_gain_tolerance_db=0.5,
                tone_phase_tolerance_deg=3.0,
                description="Declared tolerances.",
            ),
            budget=Budget(
                total=self.budget_total,
                used=self.budget_used,
                remaining=self.budget_total - self.budget_used,
                infrastructure_retries_per_experiment=1,
                infrastructure_failures=0,
            ),
            hints_used=self.hints_used,
            observations=list(self.observations),
            evidence=EvidenceState(
                assessments=[
                    HypothesisAssessment(hypothesis_id=h, status="unresolved", checks=[])
                    for h in HYPOTHESIS_IDS
                ],
                remaining=list(HYPOTHESIS_IDS),
                statement="STATEMENT-MARKER: several explanations fit.",
                distinguishing_instruments=[InstrumentKind.SOURCE_CURRENT_SWEEP],
            ),
            diagnosis=self.diagnosis,
            repair_proposal=self.repair_proposal,
            reveal=self.reveal,
            active_run_id=self.active_run_id,
            available_actions=self._actions(),
        )

    # --- CaseService interface ----------------------------------------------------------

    def get_case(self, session_id: str, case_id: str) -> PublicCase:
        self._check(session_id, case_id)
        return self.public()

    def get_observation(self, session_id: str, case_id: str, observation_id: str) -> Observation:
        self._check(session_id, case_id)
        for obs in self.observations:
            if obs.id == observation_id:
                return obs
        raise not_found("Observation")

    async def set_mode(self, session_id: str, case_id: str, mode: InvestigationMode) -> PublicCase:
        self._check(session_id, case_id)
        self.calls.append(f"set_mode:{mode}")
        self.mode = mode
        self.status = CaseStatus.OBSERVING
        return self.public()

    async def run_experiment(
        self,
        session_id: str,
        case_id: str,
        request: CaseExperimentRequest,
        *,
        requested_by: str,
        agent_run_id: str | None = None,
        cancel: CancelToken | None = None,
    ) -> ObservationResponse:
        self._check(session_id, case_id)
        self.calls.append("run_experiment")
        self.experiment_requests.append((request, requested_by, agent_run_id))
        if requested_by == "agent" and agent_run_id != self.active_run_id:
            raise ApiException(ErrorCode.RUN_IN_PROGRESS, "This AI run does not own the case.")
        if self.status != CaseStatus.OBSERVING:
            raise ApiException(ErrorCode.CONFLICT, "Experiments end once a diagnosis is in.")
        if self.budget_used >= self.budget_total:
            raise ApiException(ErrorCode.BUDGET_EXHAUSTED, "All experiments have been used.")
        self.experiment_started.set()
        if self.experiment_gate is not None:
            while not self.experiment_gate.is_set():
                if cancel is not None and cancel.cancelled:
                    raise ApiException(ErrorCode.CANCELLED, "The experiment was cancelled.")
                await asyncio.sleep(0.01)
        obs = self._sweep(request.instrument, agent_run_id, requested_by)
        self.observations.append(obs)
        self.budget_used += 1
        return ObservationResponse(observation=obs, case=self.public())

    async def submit_diagnosis(
        self,
        session_id: str,
        case_id: str,
        submission: DiagnosisSubmission,
        *,
        submitted_by: str,
        agent_run_id: str | None = None,
    ) -> PublicCase:
        self._check(session_id, case_id)
        self.calls.append("submit_diagnosis")
        if self.diagnosis is not None:
            raise ApiException(ErrorCode.CONFLICT, "A diagnosis has already been submitted.")
        self.diagnosis = DiagnosisRecord(
            id=new_id("dx"),
            submission=submission,
            submitted_by=submitted_by,  # type: ignore[arg-type]
            agent_run_id=agent_run_id,
            evidence_check=EvidenceCheck(
                status="supported", remaining=["r1_increased"], message="Supported."
            ),
            created_at=_now(),
        )
        self.status = (
            CaseStatus.UNRESOLVED
            if submission.conclusion == DiagnosisConclusion.INSUFFICIENT_EVIDENCE
            else CaseStatus.DIAGNOSIS_SUBMITTED
        )
        return self.public()

    async def propose_repair(
        self,
        session_id: str,
        case_id: str,
        body: RepairProposalInput,
        *,
        proposed_by: str,
        agent_run_id: str | None = None,
    ) -> PublicCase:
        self._check(session_id, case_id)
        self.calls.append("propose_repair")
        if self.diagnosis is None:
            raise ApiException(ErrorCode.CONFLICT, "Submit a diagnosis before proposing a repair.")
        self.repair_proposal = RepairProposal(
            id=new_id("rep"),
            component_id=body.component_id,
            value_si=NOMINAL[body.component_id],
            display_value="1 kΩ" if body.component_id == "R1" else "100 nF",
            rationale=body.rationale,
            proposed_by=proposed_by,  # type: ignore[arg-type]
            created_at=_now(),
        )
        self.status = CaseStatus.REPAIR_VERIFICATION
        return self.public()

    def is_closing_or_closed(self, session_id: str, case_id: str) -> bool:
        self._check(session_id, case_id)
        return self.closing or self.status == CaseStatus.CLOSED

    async def record_hint(self, session_id: str, case_id: str) -> PublicCase:
        self._check(session_id, case_id)
        self.calls.append("record_hint")
        if self.mode != "guide":
            raise ApiException(ErrorCode.CONFLICT, "Hints are available in Guide me mode only.")
        self.hints_used += 1
        return self.public()

    def claim_for_agent(
        self, session_id: str, case_id: str, run_id: str, kind: str | None = None
    ) -> None:
        self._check(session_id, case_id)
        self.calls.append("claim")
        if self.closing or self.status == CaseStatus.CLOSED:
            raise ApiException(ErrorCode.CONFLICT, "This case is closed to new AI runs.")
        if self.status != CaseStatus.OBSERVING or self.mode is None:
            raise ApiException(ErrorCode.CONFLICT, "Choose a mode first.")
        if kind == "investigate" and self.mode != "agent":
            raise ApiException(ErrorCode.CONFLICT, "Choose Investigate for me first.")
        if kind == "hint" and self.mode != "guide":
            raise ApiException(ErrorCode.CONFLICT, "Hints are available in Guide me mode.")
        if self.active_run_id is not None and self.active_run_id != run_id:
            raise ApiException(ErrorCode.RUN_IN_PROGRESS, "Another AI run owns this case.")
        self.active_run_id = run_id

    def release_agent(self, session_id: str, case_id: str, run_id: str) -> None:
        self.calls.append("release")
        if self.active_run_id == run_id:
            self.active_run_id = None

    def register_run_canceller(self, fn: Callable[[str], Awaitable[None]]) -> None:
        self.canceller = fn

    def register_run_liveness(self, fn: Callable[[str], bool]) -> None:
        self.liveness = fn

    def available_actions_for_run(self, session_id: str, case_id: str, run_id: str) -> list[Any]:
        """What the owning run may do (mirrors the real service's B-04 semantics)."""
        self._check(session_id, case_id)
        if self.active_run_id != run_id or self.closing or self.status == CaseStatus.CLOSED:
            return []
        if self.status == CaseStatus.OBSERVING:
            actions: list[Any] = []
            if self.budget_used < self.budget_total:
                actions.append("run_experiment")
            return [*actions, "submit_diagnosis"]
        if self.status in (CaseStatus.DIAGNOSIS_SUBMITTED, CaseStatus.UNRESOLVED):
            return ["propose_repair"]
        return []

    async def close_case(self, session_id: str, case_id: str) -> PublicCase:
        """Mirrors the real ordering: mark closing, await the run canceller, then reveal."""
        self._check(session_id, case_id)
        self.closing = True
        if self.active_run_id is not None and self.canceller is not None:
            await self.canceller(self.active_run_id)
        self.active_run_id = None
        self.status = CaseStatus.CLOSED
        self.reveal = CaseReveal(
            fault=FaultReveal(
                hypothesis_id="r1_increased",
                component_id="R1",
                multiplier=2.0,
                nominal_value_si=NOMINAL["R1"],
                actual_value_si=HIDDEN["R1"],
                description="R1 was 2 kΩ instead of the intended 1 kΩ.",
            ),
            seed=SECRET_SEED,
            generator_version="test",
            outcome=CaseOutcome(
                terminal_status="closed",
                exact_identification="not_submitted",
                functional_recovery="not_attempted",
                unsupported_certainty=False,
                experiments_used=self.budget_used,
                experiments_by_user=0,
                experiments_by_agent=self.budget_used,
                hints_used=self.hints_used,
            ),
            revealed_at=_now(),
        )
        return self.public()


# --------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------


SESSION = "s" * 40


def make_settings(tmp_path: Path, *, key: str | None = TEST_API_KEY) -> Settings:
    return Settings(
        _env_file=None,  # never read the developer's real .env in tests
        nebius_api_key=key,
        nebius_model=TEST_MODEL,
        nebius_base_url="https://provider.test/v1/",
        resonance_data_dir=tmp_path,
        web_dist_dir=tmp_path / "no-web",
        resonance_dev_agent_fixture=False,
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[DocumentStore]:
    db = DocumentStore(tmp_path / "test.sqlite3")
    yield db
    db.close()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


def make_service(
    settings: Settings,
    store: DocumentStore,
    provider: ScriptedProvider,
    cases: FakeCaseService | None = None,
    *,
    explore: Any = None,
    limits: dict[str, Any] | None = None,
) -> AgentService:
    def limits_fn(kind: AgentRunKind) -> AgentLimits:
        base = limits_for(kind)
        return base.model_copy(update=limits or {})

    service = AgentService(
        settings=settings,
        store=store,
        cases=cases,
        explore=explore,
        client_factory=provider.client_factory(),
        limits=limits_fn,
    )
    if cases is not None:
        cases.register_run_canceller(service.cancel_and_wait)
        cases.register_run_liveness(service.is_live)
    return service


async def wait_finished(service: AgentService, run_id: str, limit_s: float = 10.0) -> None:
    handle = service.handle_for(SESSION, run_id)
    await asyncio.wait_for(handle.finished.wait(), timeout=limit_s)
