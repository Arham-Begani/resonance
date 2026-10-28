"""Completion reminders of INVESTIGATE runs (scripted provider, TEST-ONLY; no live API calls).

Measured motivation: in the first held-out Nemotron evaluation (evals/results/
nemotron-heldout_v1-20261025T115418Z) 3 of 24 runs ended without submit_diagnosis and 11 of 20
faulty cases had no propose_repair. The loop now answers such a final reply with at most ONE
reminder per missing step (diagnosis; repair after a fault diagnosis), inside the run's caps,
announced as a status event and never as an agent message.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from agent_testkit import (
    HIDDEN,
    HYPOTHESIS_IDS,
    SECRET_SEED,
    SESSION,
    WAVEFORM_MARKER,
    FakeCaseService,
    ScriptedProvider,
    abstain,
    completion,
    make_service,
    wait_finished,
)

from resonance.agent import prompts
from resonance.agent.service import AgentService
from resonance.challenges.hypotheses import hypothesis_catalog
from resonance.contracts.agent import AgentRun, CaseAgentRunRequest
from resonance.contracts.investigation import DiagnosisConclusion
from resonance.store.db import DocumentStore

INVESTIGATE = CaseAgentRunRequest(kind="investigate")
REMINDERS = (prompts.DIAGNOSIS_REMINDER, prompts.REPAIR_REMINDER)


async def _run(
    service: AgentService, cases: FakeCaseService, body: CaseAgentRunRequest = INVESTIGATE
) -> AgentRun:
    run = await service.start_case_run(SESSION, cases.case_id, body)
    await wait_finished(service, run.id)
    return service.get_run(SESSION, run.id)


def _events(service: AgentService, run_id: str) -> list[Any]:
    return list(service.handle_for(SESSION, run_id).events)


def _reminder_statuses(service: AgentService, run_id: str) -> list[str]:
    return [e.text for e in _events(service, run_id) if e.type == "status" and "reminder" in e.text]


def _reminders_sent(provider: ScriptedProvider) -> list[str]:
    """Reminder turns in the final request (the conversation only grows)."""
    return [
        m["content"]
        for m in provider.requests[-1]["messages"]
        if m["role"] == "user" and m["content"] in REMINDERS
    ]


def _fault(cases: FakeCaseService, hypothesis_id: str = "r1_increased") -> dict[str, Any]:
    args = {
        "conclusion": "fault",
        "hypothesis_id": hypothesis_id,
        "citations": [{"observation_id": cases.observations[0].id, "quantity": "tone_gain_db"}],
        "explanation": "The whistle is quieter than intended.",
    }
    return completion(None, [("submit_diagnosis", args)])


def _no_fault(cases: FakeCaseService) -> dict[str, Any]:
    args = {
        "conclusion": "no_fault",
        "citations": [{"observation_id": cases.observations[0].id, "quantity": "tone_gain_db"}],
        "explanation": "The symptom matches the intended design.",
    }
    return completion(None, [("submit_diagnosis", args)])


# --------------------------------------------------------------------------------------
# Diagnosis reminder
# --------------------------------------------------------------------------------------


async def test_missing_diagnosis_gets_one_reminder_and_the_diagnosis_is_recorded(
    settings, store: DocumentStore
) -> None:
    cases = FakeCaseService(SESSION, mode="agent")
    provider = ScriptedProvider(
        [
            completion(None, [("get_case", {})]),
            completion("The whistle is quieter than expected."),  # forgot to diagnose
            abstain("r1_increased", "c1_increased"),
            completion("Two explanations still fit, so I did not pick one."),
        ]
    )
    service = make_service(settings, store, provider, cases)
    run = await _run(service, cases)

    assert run.status == "completed", run.error
    assert run.final_message == "Two explanations still fit, so I did not pick one."
    assert cases.diagnosis is not None
    assert cases.diagnosis.submission.conclusion == DiagnosisConclusion.INSUFFICIENT_EVIDENCE
    assert cases.diagnosis.agent_run_id == run.id
    assert run.usage.model_calls == 4 and len(provider.requests) == 4
    # The third request carries the model's own reply, then exactly one reminder turn.
    tail = provider.requests[2]["messages"][-2:]
    assert tail == [
        {"role": "assistant", "content": "The whistle is quieter than expected."},
        {"role": "user", "content": prompts.DIAGNOSIS_REMINDER},
    ]
    assert _reminders_sent(provider) == [prompts.DIAGNOSIS_REMINDER]
    # Announced honestly as one status event; never rendered as an agent message.
    assert len(_reminder_statuses(service, run.id)) == 1
    messages = [e.text for e in _events(service, run.id) if e.type == "message"]
    assert messages == [
        "The whistle is quieter than expected.",
        "Two explanations still fit, so I did not pick one.",
    ]
    assert not any(note in json.dumps(messages) for note in ("lab software", "reminder"))
    # No repair reminder after an abstention.
    assert "propose_repair" not in cases.calls


async def test_ignored_diagnosis_reminder_ends_the_run_after_one_reminder(
    settings, store: DocumentStore
) -> None:
    cases = FakeCaseService(SESSION, mode="agent")
    provider = ScriptedProvider(
        [
            completion("Done."),
            completion(None, [("get_case", {})]),  # it may still use tools after the reminder
            completion("Still done."),  # no second diagnosis reminder
            completion("never requested"),
        ]
    )
    service = make_service(settings, store, provider, cases)
    run = await _run(service, cases)

    assert run.status == "completed" and run.error is None
    assert run.final_message == "Still done."
    assert cases.diagnosis is None, "the run ends honestly without a diagnosis"
    assert len(provider.requests) == 3 and len(provider.steps) == 1
    assert _reminders_sent(provider) == [prompts.DIAGNOSIS_REMINDER]
    assert len(_reminder_statuses(service, run.id)) == 1
    assert cases.active_run_id is None


# --------------------------------------------------------------------------------------
# Repair reminder
# --------------------------------------------------------------------------------------


async def test_fault_without_repair_gets_one_reminder_then_the_repair_is_proposed(
    settings, store: DocumentStore
) -> None:
    cases = FakeCaseService(SESSION, mode="agent")
    provider = ScriptedProvider(
        [
            _fault(cases),
            completion("R1 is larger than intended."),  # forgot the repair
            completion(None, [("propose_repair", {"component_id": "R1"})]),
            completion("I proposed replacing R1 with its intended part."),
        ]
    )
    service = make_service(settings, store, provider, cases)
    run = await _run(service, cases)

    assert run.status == "completed", run.error
    assert cases.repair_proposal is not None and cases.repair_proposal.component_id == "R1"
    assert cases.repair_proposal.proposed_by == "agent"
    assert _reminders_sent(provider) == [prompts.REPAIR_REMINDER]
    assert len(_reminder_statuses(service, run.id)) == 1
    assert "repair_proposal" in [e.type for e in _events(service, run.id)]
    assert run.usage.model_calls == 4


async def test_ignored_repair_reminder_ends_the_run_after_one_reminder(
    settings, store: DocumentStore
) -> None:
    cases = FakeCaseService(SESSION, mode="agent")
    provider = ScriptedProvider(
        [
            _fault(cases),
            completion("R1 is larger than intended."),
            completion("I will leave the repair to you."),
            completion("never requested"),
        ]
    )
    service = make_service(settings, store, provider, cases)
    run = await _run(service, cases)

    assert run.status == "completed"
    assert run.final_message == "I will leave the repair to you."
    assert cases.diagnosis is not None and cases.repair_proposal is None
    assert len(provider.requests) == 3 and len(provider.steps) == 1
    assert _reminders_sent(provider) == [prompts.REPAIR_REMINDER]


async def test_both_reminders_at_most_once_each_in_one_run(settings, store: DocumentStore) -> None:
    cases = FakeCaseService(SESSION, mode="agent")
    provider = ScriptedProvider(
        [
            completion("Done."),
            _fault(cases),
            completion("R1 is larger than intended."),
            completion("Finished."),
            completion("never requested"),
        ]
    )
    service = make_service(settings, store, provider, cases)
    run = await _run(service, cases)

    assert run.status == "completed" and run.final_message == "Finished."
    assert _reminders_sent(provider) == [prompts.DIAGNOSIS_REMINDER, prompts.REPAIR_REMINDER]
    assert len(_reminder_statuses(service, run.id)) == 2
    assert len(provider.requests) == 4


@pytest.mark.parametrize("conclusion", ["no_fault", "insufficient_evidence"])
async def test_no_repair_reminder_after_no_fault_or_abstention(
    settings, store: DocumentStore, conclusion: str
) -> None:
    cases = FakeCaseService(SESSION, mode="agent")
    submit = _no_fault(cases) if conclusion == "no_fault" else abstain()
    provider = ScriptedProvider([submit, completion("Summary."), completion("never requested")])
    service = make_service(settings, store, provider, cases)
    run = await _run(service, cases)

    assert run.status == "completed" and run.final_message == "Summary."
    assert cases.diagnosis is not None and cases.diagnosis.submission.conclusion == conclusion
    assert len(provider.requests) == 2
    assert _reminders_sent(provider) == [] and _reminder_statuses(service, run.id) == []
    assert "propose_repair" not in cases.calls


async def test_hint_runs_never_get_a_reminder(settings, store: DocumentStore) -> None:
    cases = FakeCaseService(SESSION, mode="guide")
    provider = ScriptedProvider(
        [completion("What do you expect the current sweep to show?"), completion("never")]
    )
    service = make_service(settings, store, provider, cases)
    run = await _run(service, cases, CaseAgentRunRequest(kind="hint"))
    assert run.status == "completed"
    assert len(provider.requests) == 1 and _reminder_statuses(service, run.id) == []


async def test_no_reminder_once_the_case_is_closing(settings, store: DocumentStore) -> None:
    cases = FakeCaseService(SESSION, mode="agent")

    def finish_while_closing(_: dict[str, Any]) -> dict[str, Any]:
        cases.closing = True
        return completion("Done.")

    provider = ScriptedProvider([finish_while_closing, completion("never requested")])
    service = make_service(settings, store, provider, cases)
    run = await _run(service, cases)
    assert run.status == "completed" and len(provider.requests) == 1


# --------------------------------------------------------------------------------------
# Caps: reminders never extend a run beyond its limits
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("limits", "script_prefix"),
    [
        ({"max_model_calls": 1}, []),  # no model call left for a reminder
        ({"max_total_tokens": 600}, []),  # 150 used: less than MIN_TOKENS_FOR_CALL left
        ({"max_tool_calls": 1}, [completion(None, [("get_case", {})])]),  # no tool call left
    ],
    ids=["model_calls", "tokens", "tool_calls"],
)
async def test_no_reminder_without_room_inside_the_caps(
    settings, store: DocumentStore, limits: dict[str, Any], script_prefix: list[Any]
) -> None:
    cases = FakeCaseService(SESSION, mode="agent")
    provider = ScriptedProvider([*script_prefix, completion("Done."), completion("never")])
    service = make_service(settings, store, provider, cases, limits=limits)
    run = await _run(service, cases)

    assert run.status == "completed" and run.final_message == "Done."
    assert len(provider.requests) == len(script_prefix) + 1
    assert _reminder_statuses(service, run.id) == []


async def test_reminder_turns_count_against_the_model_call_cap(
    settings, store: DocumentStore
) -> None:
    cases = FakeCaseService(SESSION, mode="agent")
    provider = ScriptedProvider([completion("Done."), abstain(), completion("never requested")])
    service = make_service(settings, store, provider, cases, limits={"max_model_calls": 2})
    run = await _run(service, cases)

    # The reminder's reply was the second (last allowed) model call: the diagnosis it made is
    # recorded, and the run then stops at its cap instead of making a third call.
    assert cases.diagnosis is not None
    assert run.status == "limit_reached" and run.error is not None
    assert run.error.code == "limit_reached"
    assert run.usage.model_calls == 2 and len(provider.requests) == 2


# --------------------------------------------------------------------------------------
# Isolation: what the model receives
# --------------------------------------------------------------------------------------


def test_reminder_texts_name_no_hypothesis_component_or_number() -> None:
    labels = [h.label for h in hypothesis_catalog()]
    for text in REMINDERS:
        assert text.startswith("Note from the Resonance lab software (not from the learner)")
        for hid in HYPOTHESIS_IDS:
            if hid != "no_fault":  # "no_fault" is a conclusion value of submit_diagnosis
                assert hid not in text
        for label in labels:
            assert label not in text
        assert not re.search(r"\b(R1|C1)\b", text)
        assert not re.search(r"\d", text), "no measured or hidden values"


async def test_agent_visible_messages_with_reminders_contain_nothing_hidden(
    settings, store: DocumentStore
) -> None:
    cases = FakeCaseService(SESSION, mode="agent")
    provider = ScriptedProvider(
        [
            completion(None, [("get_case", {})]),
            completion("Done."),
            _fault(cases),
            completion("R1 is larger than intended."),
            completion(None, [("propose_repair", {"component_id": "R1"})]),
            completion("Summary."),
        ]
    )
    service = make_service(settings, store, provider, cases)
    run = await _run(service, cases)
    assert run.status == "completed"
    assert _reminders_sent(provider) == list(REMINDERS)

    sent = json.dumps(provider.requests, ensure_ascii=False)
    assert SECRET_SEED not in sent
    assert str(WAVEFORM_MARKER) not in sent, "no raw waveform samples"
    assert "STATEMENT-MARKER" not in sent, "evidence statements only via assess_evidence"
    assert "distinguishing_instruments" not in sent
    # The fake's hidden R1 is 2 kΩ (the numeral 2000 also appears as a public grid frequency,
    # so the formatted value is the meaningful marker here).
    assert HIDDEN["R1"] == 2000.0 and "2 kΩ" not in sent, "no hidden component value"
    # Every user turn is either the learner's turn or one of the two fixed reminder texts.
    users = [m["content"] for m in provider.requests[-1]["messages"] if m["role"] == "user"]
    assert users == [prompts.DEFAULT_INVESTIGATE_TURN, *REMINDERS]
