// TEST FIXTURES ONLY. Typed sample payloads for unit tests; never imported by shipped code.
// Values are synthetic (computed from the first-order RC formula) purely to exercise rendering.
import type {
  AgentEvent,
  AgentRun,
  AudioComparison,
  ExperimentRecord,
  HealthResponse,
  Observation,
  PublicCase,
  Revision,
  WorkbenchState,
} from "../api/types";

const T0 = "2026-10-15T12:00:00Z";

export const tones = [
  { frequency_hz: 440, amplitude_v: 0.6, label: "Musical tone", role: "wanted" as const },
  { frequency_hz: 4000, amplitude_v: 0.3, label: "Whistle", role: "interference" as const },
];

export function makeRevision(overrides: Partial<Revision> = {}): Revision {
  return {
    id: "rev_fixture000001",
    template_id: "rc_lowpass",
    title: "RC low-pass filter",
    parent_id: null,
    origin: "template_default",
    created_at: T0,
    frozen: false,
    changes: [],
    nodes: [
      { id: "in", label: "Input", role: "input" },
      { id: "out", label: "Output", role: "output" },
      { id: "gnd", label: "Ground", role: "ground" },
    ],
    components: [
      {
        id: "V1",
        kind: "voltage_source",
        label: "V1",
        name: "voltage source",
        value_si: null,
        unit: null,
        display_value: null,
        nodes: ["in", "gnd"],
        editable: false,
        bounds: null,
        description: "V1 — voltage source — two-tone signal — connected between Input and Ground",
      },
      {
        id: "R1",
        kind: "resistor",
        label: "R1",
        name: "resistor",
        value_si: 1000,
        unit: "ohm",
        display_value: "1 kΩ",
        nodes: ["in", "out"],
        editable: true,
        bounds: { min_si: 100, max_si: 100000, unit: "ohm" },
        description: "R1 — resistor — 1 kΩ — connected between Input and Output",
      },
      {
        id: "C1",
        kind: "capacitor",
        label: "C1",
        name: "capacitor",
        value_si: 1e-7,
        unit: "farad",
        display_value: "100 nF",
        nodes: ["out", "gnd"],
        editable: true,
        bounds: { min_si: 1e-9, max_si: 1e-5, unit: "farad" },
        description: "C1 — capacitor — 100 nF — connected between Output and Ground",
      },
    ],
    source: { kind: "ideal_voltage", ac_magnitude_v: 1, tones, description: "Two-tone source" },
    prediction: { basis: "analytical_first_order_rc", cutoff_hz: 1591.5, time_constant_s: 1e-4 },
    ...overrides,
  };
}

function rcGainDb(f: number, fc: number) {
  return -10 * Math.log10(1 + (f / fc) ** 2);
}
function rcPhase(f: number, fc: number) {
  return (-Math.atan(f / fc) * 180) / Math.PI;
}

export function grid(start: number, stop: number, perDecade: number): number[] {
  const n = Math.round(Math.log10(stop / start) * perDecade);
  return Array.from({ length: n + 1 }, (_, i) => start * 10 ** (i / perDecade));
}

export const comparisonAudio: AudioComparison = {
  id: "cmp_fixture000001",
  clips: [
    { artifact_id: "art_fixturein0001", url: "/api/artifacts/art_fixturein0001", role: "input", label: "Input", source_id: null, duration_s: 1.2, sample_rate_hz: 48000, peak_fs: 0.9, clipped: false },
    { artifact_id: "art_fixtureout001", url: "/api/artifacts/art_fixtureout001", role: "output", label: "Output", source_id: null, duration_s: 1.2, sample_rate_hz: 48000, peak_fs: 0.6, clipped: false },
  ],
  playback_gain_fs_per_v: 1,
  gain_adjustment_db: 0,
  fade_ms: 15,
  note: "Both clips use one shared gain of 1.0 full scale per volt.",
};

export function makeExperiment(overrides: Partial<ExperimentRecord> = {}): ExperimentRecord {
  const fc = 1591.5;
  const f = grid(10, 100000, 40);
  const sr = 48000;
  const n = 240;
  return {
    id: "exp_fixture000001",
    revision_id: "rev_fixture000001",
    status: "succeeded",
    created_at: T0,
    duration_ms: 1400,
    simulator: { name: "ngspice", version: "47" },
    settings: {
      ac: { start_hz: 10, stop_hz: 100000, points_per_decade: 40, points: f.length, source_ac_magnitude_v: 1 },
      transient: { tones, settle_s: 0.2, clip_s: 1.2, max_step_s: 5e-6, sample_rate_hz: sr, fade_ms: 15 },
    },
    response: {
      frequency_hz: f,
      gain_db: f.map((x) => rcGainDb(x, fc)),
      phase_deg: f.map((x) => rcPhase(x, fc)),
      source_current_ma: f.map((x) => 1 / Math.sqrt(1000 ** 2 + (1 / (2 * Math.PI * x * 1e-7)) ** 2) * 1000),
      source_current_phase_deg: f.map((x) => 90 + rcPhase(x, fc)),
    },
    tones: tones.map((t) => ({
      frequency_hz: t.frequency_hz,
      label: t.label,
      role: t.role,
      input_amplitude_v: t.amplitude_v,
      output_amplitude_v: t.amplitude_v * 10 ** (rcGainDb(t.frequency_hz, fc) / 20),
      transient_gain_db: rcGainDb(t.frequency_hz, fc),
      ac_gain_db: rcGainDb(t.frequency_hz, fc),
      agreement_db: 0.01,
      phase_deg: rcPhase(t.frequency_hz, fc),
    })),
    waveform: {
      sample_rate_hz: sr,
      start_s: 0,
      input_v: Array.from({ length: n }, (_, i) => 0.6 * Math.sin((2 * Math.PI * 440 * i) / sr) + 0.3 * Math.sin((2 * Math.PI * 4000 * i) / sr)),
      output_v: Array.from({ length: n }, (_, i) => 0.58 * Math.sin((2 * Math.PI * 440 * i) / sr - 0.27) + 0.11 * Math.sin((2 * Math.PI * 4000 * i) / sr - 1.19)),
    },
    spectrum: null,
    audio: comparisonAudio,
    goal: null,
    checks: [{ name: "transient vs AC", passed: true, detail: "within 0.5 dB" }],
    error: null,
    ...overrides,
  };
}

export const healthMissingKey: HealthResponse = {
  status: "degraded",
  version: "0.1.0",
  simulator: { available: true, version: "47", message: "ngspice 47 is ready." },
  ai: {
    available: false,
    state: "missing_key",
    provider: "nebius_token_factory",
    model: null,
    base_url_host: "api.tokenfactory.nebius.com",
    message: "AI lab partner unavailable: NEBIUS_API_KEY is not set.",
  },
};

export function makeWorkbench(overrides: Partial<WorkbenchState> = {}): WorkbenchState {
  return {
    revisions: [makeRevision()],
    current_revision_id: "rev_fixture000001",
    experiments: [],
    goal: {
      id: "goal_fixture00001",
      title: "Keep the musical tone. Reduce the whistle.",
      origin: "default",
      constraints: [
        { kind: "preserve", frequency_hz: 440, limit_db: 1, label: "Musical tone" },
        { kind: "attenuate", frequency_hz: 4000, limit_db: 12, label: "Whistle" },
      ],
    },
    feasibility: {
      basis: "analytical_first_order_rc",
      template_id: "rc_lowpass",
      feasible: true,
      cutoff_range_hz: [869, 1015],
      explanation: "Prediction: a first-order RC low-pass filter meets every constraint when its cutoff is between 869 Hz and 1,015 Hz.",
    },
    proposals: [],
    active_case_id: null,
    ...overrides,
  };
}

export function makeObservation(overrides: Partial<Observation> = {}): Observation {
  const f = grid(20, 20000, 10);
  return {
    id: "obs_fixture000001",
    case_id: "case_fixture00001",
    sequence: 1,
    kind: "output_voltage_sweep",
    probe: null,
    counted: true,
    requested_by: "user",
    agent_run_id: null,
    status: "succeeded",
    replay: false,
    created_at: T0,
    simulator: { name: "ngspice", version: "47" },
    settings: { sweep: null, transient: null },
    sweep: {
      quantity: "output_gain",
      frequency_hz: f,
      magnitude: f.map((x) => rcGainDb(x, 795.7)),
      magnitude_unit: "dB",
      phase_deg: f.map((x) => rcPhase(x, 795.7)),
      expected_magnitude: f.map((x) => rcGainDb(x, 1591.5)),
      expected_phase_deg: f.map((x) => rcPhase(x, 1591.5)),
    },
    transient: null,
    summary: "Output gain is lower than expected above 300 Hz.",
    error: null,
    ...overrides,
  };
}

export function makeCase(overrides: Partial<PublicCase> = {}): PublicCase {
  const symptom = makeObservation({
    id: "obs_fixturesymp01",
    sequence: 0,
    kind: "symptom",
    probe: "output_voltage",
    counted: false,
    requested_by: "system",
    sweep: null,
    transient: {
      probe: "output_voltage",
      tones: [
        { frequency_hz: 440, label: "Musical tone", role: "wanted", amplitude: 0.52, amplitude_unit: "V", gain_db: -1.2, phase_deg: -29, expected_amplitude: 0.58, expected_gain_db: -0.3, expected_phase_deg: -15.5, },
        { frequency_hz: 4000, label: "Whistle", role: "interference", amplitude: 0.058, amplitude_unit: "V", gain_db: -14.3, phase_deg: -79, expected_amplitude: 0.11, expected_gain_db: -8.6, expected_phase_deg: -68 },
      ],
      waveform: { sample_rate_hz: 48000, start_s: 0, unit: "V", input_v: [0, 0.1, 0.2], observed: [0, 0.05, 0.1], expected: [0, 0.08, 0.15] },
      audio: { ...comparisonAudio, id: "cmp_fixturesymp01", clips: comparisonAudio.clips.map((c, i) => ({ ...c, role: i === 0 ? "expected" : "observed", label: i === 0 ? "Expected" : "Observed" })) },
    },
    summary: "The whistle is 5.7 dB quieter than the intended design; the musical tone is 0.9 dB quieter.",
  });
  return {
    id: "case_fixture00001",
    status: "observing",
    mode: "guide",
    created_at: T0,
    updated_at: T0,
    intended_revision: makeRevision({ id: "rev_fixturecase01", origin: "case_intended", frozen: true }),
    instruments: [
      { kind: "output_voltage_sweep", title: "Output voltage sweep", description: "Gain and phase of the output, 20 Hz to 20 kHz.", returns: ["output_gain_db", "output_phase_deg"], frequency_hz: grid(20, 20000, 10), probes: null, cost: 1 },
      { kind: "source_current_sweep", title: "Source current sweep", description: "Current delivered by the source, 20 Hz to 20 kHz.", returns: ["source_current_ma", "source_current_phase_deg"], frequency_hz: grid(20, 20000, 10), probes: null, cost: 1 },
      { kind: "transient_capture", title: "Transient capture", description: "Two-tone capture of one quantity. The output-voltage capture repeats the symptom measurement.", returns: ["tone_gain_db", "tone_phase_deg"], frequency_hz: null, probes: ["output_voltage", "source_current"], cost: 1 },
    ],
    hypotheses: [
      { id: "no_fault", label: "No fault", component_id: null, change: null, multiplier: null },
      { id: "r1_increased", label: "R1 is about twice its nominal value", component_id: "R1", change: "increased", multiplier: 2 },
      { id: "r1_decreased", label: "R1 is about half its nominal value", component_id: "R1", change: "decreased", multiplier: 0.5 },
      { id: "c1_increased", label: "C1 is about twice its nominal value", component_id: "C1", change: "increased", multiplier: 2 },
      { id: "c1_decreased", label: "C1 is about half its nominal value", component_id: "C1", change: "decreased", multiplier: 0.5 },
    ],
    policy: { gain_tolerance_db: 0.5, phase_tolerance_deg: 3, current_tolerance_pct: 3, current_abs_tolerance_ma: 0.001, tone_gain_tolerance_db: 0.5, tone_phase_tolerance_deg: 3, description: "Tolerances: ±0.5 dB gain, ±3° phase, ±3% current." },
    budget: { total: 3, used: 1, remaining: 2, infrastructure_retries_per_experiment: 1, infrastructure_failures: 0 },
    hints_used: 0,
    observations: [symptom, makeObservation()],
    evidence: {
      assessments: [
        { hypothesis_id: "no_fault", status: "contradicted", checks: [{ observation_id: "obs_fixture000001", consistent: false, quantity: "output_gain_db", frequency_hz: 4000, observed: -14.3, predicted: -8.6, deviation: 5.7, tolerance: 0.5, unit: "dB" }] },
        { hypothesis_id: "r1_increased", status: "unresolved", checks: [] },
        { hypothesis_id: "r1_decreased", status: "contradicted", checks: [] },
        { hypothesis_id: "c1_increased", status: "unresolved", checks: [] },
        { hypothesis_id: "c1_decreased", status: "contradicted", checks: [] },
      ],
      remaining: ["r1_increased", "c1_increased"],
      statement: "Two explanations fit the recorded observations.",
      distinguishing_instruments: null,
    },
    diagnosis: null,
    repair_proposal: null,
    repair: null,
    reveal: null,
    active_run_id: null,
    available_actions: ["run_experiment", "request_hint", "submit_diagnosis", "close"],
    ...overrides,
  };
}

const RUN_AT = "2026-10-15T12:01:00Z";

export function makeRun(overrides: Partial<AgentRun> = {}): AgentRun {
  return {
    id: "run_fixture000001",
    kind: "investigate",
    source: "nebius",
    case_id: "case_fixture00001",
    revision_id: null,
    status: "running",
    model: "nvidia/nemotron-fixture",
    created_at: RUN_AT,
    finished_at: null,
    limits: { max_model_calls: 10, max_tool_calls: 12, max_experiments: 3, max_total_tokens: 60000, wall_time_s: 180, max_retries: 1 },
    usage: { model_calls: 0, tool_calls: 0, prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, latency_ms: 0 },
    final_message: null,
    error: null,
    ...overrides,
  };
}

export const runEvents: AgentEvent[] = [
  { type: "run_started", seq: 1, run_id: "run_fixture000001", at: RUN_AT, kind: "investigate", model: "nvidia/nemotron-fixture" },
  { type: "status", seq: 2, run_id: "run_fixture000001", at: RUN_AT, text: "Waiting for the model" },
  { type: "tool_call", seq: 3, run_id: "run_fixture000001", at: RUN_AT, call_id: "call_1", tool: "run_experiment", arguments: { instrument: "source_current_sweep" } },
  { type: "tool_result", seq: 4, run_id: "run_fixture000001", at: RUN_AT, call_id: "call_1", tool: "run_experiment", ok: true, summary: "Source current recorded on 31 frequencies.", observation_id: "obs_fixture000001", experiment_id: null, proposal_id: null, error: null },
  { type: "message", seq: 5, run_id: "run_fixture000001", at: RUN_AT, text: "The current at high frequency is lower than expected.", citations: [{ result_id: "obs_fixture000001", quantity: "source_current_ma", frequency_hz: 20000 }] },
];
