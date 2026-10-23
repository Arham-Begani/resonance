"""Learn prompts and request validation (pure; no simulation).

Range filtering is checked against independently computed cutoffs, and every refusal must
happen before anything is simulated or stored (validation errors are 422, frozen designs 409).
"""

from __future__ import annotations

import math

import pytest

from resonance.api.errors import ApiException
from resonance.circuits.revisions import build_revision
from resonance.contracts.common import ErrorCode
from resonance.contracts.learn import LearnAttemptRequest
from resonance.contracts.policy import LEARN_SAME_THRESHOLD_DB
from resonance.explore.learn import learn_prompts, observed_change, plan_attempt


def _fc(r: float, c: float) -> float:
    return 1 / (2 * math.pi * r * c)


def test_default_lowpass_prompts_ask_about_its_own_tones() -> None:
    rev = build_revision("rc_lowpass", {}, origin="template_default")
    prompts = learn_prompts(rev)
    assert [p.id for p in prompts] == ["R1x0.5", "R1x2", "C1x0.5", "C1x2"]
    c1x2 = prompts[-1]
    assert c1x2.question == (
        "If we double C1 (100 nF → 200 nF), what happens to the whistle (4 kHz) and the "
        "musical tone (440 Hz)?"
    )
    assert (c1x2.change.component_id, c1x2.change.multiplier) == ("C1", 2.0)
    assert c1x2.template_id == "rc_lowpass"
    assert c1x2.tones == rev.source.tones


def test_highpass_prompts_follow_its_signal_path_and_tone_labels() -> None:
    rev = build_revision("rc_highpass", {}, origin="template_default")
    prompts = learn_prompts(rev)
    assert [p.id for p in prompts] == ["C1x0.5", "C1x2", "R1x0.5", "R1x2"]
    assert prompts[3].question == (
        "If we double R1 (10 kΩ → 20 kΩ), what happens to the mains hum (50 Hz) and the "
        "musical tone (440 Hz)?"
    )
    assert [t.frequency_hz for t in prompts[0].tones] == [50.0, 440.0]


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        # R1 at its 100 Ω minimum: halving R1 leaves the bounds, halving C1 pushes the cutoff
        # above 20 kHz (fc = 15.9 kHz → 31.8 kHz).
        ({"R1": 100.0, "C1": 100e-9}, ["R1x2", "C1x2"]),
        # fc ≈ 20.0 Hz with R1 = 100 kΩ: doubling either would leave the range.
        ({"R1": 100e3, "C1": 79.5e-9}, ["R1x0.5", "C1x0.5"]),
        # C1 at its 10 µF maximum with fc = 159 Hz: doubling C1 leaves the bounds.
        ({"R1": 100.0, "C1": 10e-6}, ["R1x2", "C1x0.5"]),
    ],
)
def test_prompts_outside_the_supported_range_are_not_offered(
    values: dict[str, float], expected: list[str]
) -> None:
    rev = build_revision("rc_lowpass", values, origin="user_edit")
    offered = [p.id for p in learn_prompts(rev)]
    assert offered == expected
    for prompt in learn_prompts(rev):
        changed = dict(values)
        changed[prompt.change.component_id] *= prompt.change.multiplier
        assert 20.0 <= _fc(changed["R1"], changed["C1"]) <= 20_000.0


def _request(
    rev_id: str, component: str, multiplier: float, freqs: list[float]
) -> LearnAttemptRequest:
    return LearnAttemptRequest.model_validate(
        {
            "revision_id": rev_id,
            "change": {"component_id": component, "multiplier": multiplier},
            "predictions": [{"frequency_hz": f, "expectation": "quieter"} for f in freqs],
        }
    )


def test_plan_builds_an_unsaved_child_revision() -> None:
    rev = build_revision("rc_highpass", {}, origin="template_default")
    planned = plan_attempt(rev, _request(rev.id, "R1", 0.5, [440.0, 50.0]))
    child = planned.changed
    assert child.parent_id == rev.id and child.origin == "user_edit"
    assert child.template_id == "rc_highpass" and child.source == rev.source
    assert [(c.component_id, c.from_value_si, c.to_value_si) for c in child.changes] == [
        ("R1", 10e3, 5e3)
    ]
    # Predictions are matched to the revision's tones in signal order.
    assert [t.frequency_hz for t, _ in planned.predictions] == [50.0, 440.0]


@pytest.mark.parametrize(
    ("component", "multiplier", "freqs", "field"),
    [
        ("R1", 2.0, [440.0], "predictions"),  # the whistle is missing
        ("R1", 2.0, [440.0, 4000.0, 50.0], "predictions.2.frequency_hz"),  # not in the signal
        ("R1", 2.0, [440.0, 440.0], "predictions.1.frequency_hz"),  # duplicate
        ("R1", 4.0, [440.0, 4000.0], "change.multiplier"),  # 47 kΩ × 4 > 100 kΩ
        ("C1", 0.25, [440.0, 4000.0], "change.multiplier"),  # 375 pF < the 1 nF minimum
    ],
)
def test_invalid_attempts_are_validation_errors(
    component: str, multiplier: float, freqs: list[float], field: str
) -> None:
    # R1 = 47 kΩ, C1 = 1.5 nF: fc = 2.26 kHz; C1 × 0.25 = 375 pF is below the 1 nF minimum.
    rev = build_revision("rc_lowpass", {"R1": 47e3, "C1": 1.5e-9}, origin="user_edit")
    with pytest.raises(ApiException) as info:
        plan_attempt(rev, _request(rev.id, component, multiplier, freqs))
    assert info.value.error.code == ErrorCode.VALIDATION_ERROR and info.value.status == 422
    assert info.value.error.field == field


def test_multipliers_outside_the_contract_are_rejected_by_the_schema() -> None:
    with pytest.raises(ValueError, match="multiplier must be one of"):
        _request("rev_abcdefghijkl", "R1", 3.0, [440.0, 4000.0])


def test_frozen_case_designs_are_refused() -> None:
    frozen = build_revision("rc_lowpass", {}, origin="case_intended", frozen=True)
    for call in (
        lambda: learn_prompts(frozen),
        lambda: plan_attempt(frozen, _request(frozen.id, "C1", 2.0, [440.0, 4000.0])),
    ):
        with pytest.raises(ApiException) as info:
            call()
        assert info.value.error.code == ErrorCode.STALE_REVISION and info.value.status == 409


def test_observed_change_threshold_is_strict() -> None:
    t = LEARN_SAME_THRESHOLD_DB
    assert observed_change(0.0) == "about_the_same"
    assert observed_change(-(t - 1e-9)) == "about_the_same"
    assert observed_change(t - 1e-9) == "about_the_same"
    assert observed_change(-t) == "quieter"  # |Δ| < threshold is "about the same"; = is not
    assert observed_change(t) == "louder"
