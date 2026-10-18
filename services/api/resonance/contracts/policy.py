"""Declared, public policy constants. Changing a value here is a contract change: tell the lead.

Values marked (owner: …) are initial choices that the named specialist must validate with
tests and may propose to change.
"""

from __future__ import annotations

from .circuits import Tone

# --- Demo signal (owner: audio) ---------------------------------------------------------
DEFAULT_TONES: tuple[Tone, ...] = (
    Tone(frequency_hz=440.0, amplitude_v=0.6, label="Musical tone", role="wanted"),
    Tone(frequency_hz=4000.0, amplitude_v=0.3, label="Whistle", role="interference"),
)

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
# Hosted cases draw nominal design and fault independently and i.i.d.; published priors:
FAULT_PRIOR = {
    "no_fault": 0.2,
    "r1_increased": 0.2,
    "r1_decreased": 0.2,
    "c1_increased": 0.2,
    "c1_decreased": 0.2,
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
