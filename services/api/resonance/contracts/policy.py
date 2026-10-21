"""Declared, public policy constants. Changing a value here is a contract change: tell the lead.

Values marked (owner: …) are initial choices that the named specialist must validate with
tests and may propose to change.
"""

from __future__ import annotations

from .circuits import Tone

# --- Demo signals (owner: audio) --------------------------------------------------------
# Each template has its own two-tone test signal: the low-pass reduces a high whistle, the
# high-pass removes a low mains hum. Peak sum stays 0.9 V, so the shared playback gain never
# clips a passive first-order filter in steady state.
TEMPLATE_TONES: dict[str, tuple[Tone, ...]] = {
    "rc_lowpass": (
        Tone(frequency_hz=440.0, amplitude_v=0.6, label="Musical tone", role="wanted"),
        Tone(frequency_hz=4000.0, amplitude_v=0.3, label="Whistle", role="interference"),
    ),
    "rc_highpass": (
        Tone(frequency_hz=50.0, amplitude_v=0.3, label="Mains hum", role="interference"),
        Tone(frequency_hz=440.0, amplitude_v=0.6, label="Musical tone", role="wanted"),
    ),
}
DEFAULT_TONES: tuple[Tone, ...] = TEMPLATE_TONES["rc_lowpass"]

# Opening goals: (title, ((kind, frequency_hz, limit_db, label), ...)). Analytical feasible
# cutoffs: low-pass 864.8-1037.9 Hz; high-pass 192.7-223.9 Hz.
TEMPLATE_DEFAULT_GOALS: dict[str, tuple[str, tuple[tuple[str, float, float, str], ...]]] = {
    "rc_lowpass": (
        "Keep the musical tone. Reduce the whistle.",
        (("preserve", 440.0, 1.0, "Musical tone"), ("attenuate", 4000.0, 12.0, "Whistle")),
    ),
    "rc_highpass": (
        "Keep the musical tone. Remove the mains hum.",
        (("preserve", 440.0, 1.0, "Musical tone"), ("attenuate", 50.0, 12.0, "Mains hum")),
    ),
}

# --- Learn mode (owner: audio/explore) ---------------------------------------------------
LEARN_MULTIPLIERS = (0.5, 2.0)  # offered prompts; requests may also use 0.25 and 4.0
LEARN_SAME_THRESHOLD_DB = 1.0  # a measured change smaller than this is "about the same"

# --- Design suggestions (owner: audio/explore) -------------------------------------------
E12_SERIES = (1.0, 1.2, 1.5, 1.8, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2)

# --- Audio rendering (owner: audio) -----------------------------------------------------
AUDIO_SAMPLE_RATE_HZ = 48_000
# One global voltage-to-playback gain shared by EVERY clip, so any two clips are comparable.
# Passive RC steady-state output peak ≤ Σ tone amplitudes (0.9 V) → ≤ 0.9 FS with gain 1.0.
PLAYBACK_GAIN_FS_PER_V = 1.0
AUDIO_FADE_MS = 15.0
AUDIO_CLIP_S = 1.2
# Settling must depend only on PUBLIC information (never on hidden values): a fixed period.
# The rc_lowpass template bounds the cutoff to 20 Hz–20 kHz (τ ≤ 7.96 ms), so 0.2 s covers
# ≥ 25 τ for any allowed design and ≥ 12.5 τ for its 2× fault variant (requirement: ≥ 7 τ).
TRANSIENT_SETTLE_S = 0.2
# 10 µs: verification measured ≤ 0.051 dB tone-fit error vs analytical (budget 0.5 dB) at about
# half the cost of 5 µs. (owner: audio — validate with the real-ngspice tone tests.)
TRANSIENT_MAX_STEP_S = 1e-5
WAVEFORM_WINDOW_MAX_SAMPLES = 1024
# Non-finite values never reach JSON: dB quantities are clamped to this floor.
DB_FLOOR = -200.0

# --- Explore analyses (owner: simulation) -----------------------------------------------
EXPLORE_SWEEP_START_HZ = 10.0
EXPLORE_SWEEP_STOP_HZ = 100_000.0
EXPLORE_SWEEP_POINTS_PER_DECADE = 40

# --- Investigation (owner: investigation) -----------------------------------------------
CASE_EXPERIMENT_BUDGET = 3
CASE_INFRA_RETRIES_PER_EXPERIMENT = 1
CASE_MAX_INFRA_FAILURES = 3
CASE_SWEEP_START_HZ = 20.0
CASE_SWEEP_STOP_HZ = 20_000.0
CASE_SWEEP_POINTS_PER_DECADE = 10  # → 31 points
CASE_TRANSIENT_PROBES = ("output_voltage", "source_current")
FAULT_MULTIPLIERS = (0.5, 2.0)
# Nominal case designs keep every fault variant inside the template's 20 Hz–20 kHz cutoff
# range and keep symptom predictions of non-equivalent hypotheses ≥ 3× tolerance apart.
CASE_NOMINAL_CUTOFF_RANGE_HZ = (40.0, 5000.0)
# Case family (public, shown in the intended design) when the learner does not pick one:
CASE_FAMILY_PRIOR = {"rc_lowpass": 0.5, "rc_highpass": 0.5}
# Hosted cases draw nominal design and condition independently and i.i.d.; published priors.
# c1_disconnected is proven simulatable (real ngspice: low-pass 0 dB / 0 mA, high-pass no output
# / 0 mA) and identifiable from the standardized symptom for every drawable design of both
# families (tests/challenges/test_case_open_capacitor.py, test_case_families_ngspice.py), so
# all six conditions have prior 1/6 (owner: investigation).
FAULT_PRIOR = {
    "no_fault": 1 / 6,
    "r1_increased": 1 / 6,
    "r1_decreased": 1 / 6,
    "c1_increased": 1 / 6,
    "c1_decreased": 1 / 6,
    "c1_disconnected": 1 / 6,
}
CASE_SEED_BYTES = 16  # secrets.token_hex(16): ≥128-bit seeds, never reused in hosted mode
CASE_CLOSE_AGENT_WAIT_S = 10.0

EVIDENCE_GAIN_TOLERANCE_DB = 0.5
EVIDENCE_PHASE_TOLERANCE_DEG = 3.0
EVIDENCE_CURRENT_TOLERANCE_PCT = 3.0
EVIDENCE_CURRENT_ABS_TOLERANCE_MA = 1e-6
EVIDENCE_TONE_GAIN_TOLERANCE_DB = 0.5
EVIDENCE_TONE_PHASE_TOLERANCE_DEG = 3.0

REPAIR_GAIN_TOLERANCE_DB = 0.5
REPAIR_PHASE_TOLERANCE_DEG = 3.0

# --- Agent loop caps (owner: investigation/AI) ------------------------------------------
AGENT_MAX_MODEL_CALLS = 10
AGENT_MAX_TOOL_CALLS = 12
AGENT_MAX_TOTAL_TOKENS = 60_000
AGENT_WALL_TIME_S = 180.0
AGENT_MAX_RETRIES = 1
