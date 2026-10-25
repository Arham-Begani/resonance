// Plain-language labels for case data. All text here is generic; case-specific facts always
// come from the PublicCase payload.
import type { CaseStatus, Hypothesis, HypothesisId, InstrumentKind, Observation, PublicCase } from "../../api/types";

export type TransientProbe = NonNullable<Observation["probe"]>;

export const STATUS_LABEL: Record<CaseStatus, string> = {
  ready: "Ready",
  observing: "Observing",
  diagnosis_submitted: "Diagnosis submitted",
  unresolved: "Unresolved",
  repair_verification: "Repair checked",
  closed: "Closed",
  failed: "Stopped (simulator failure)",
};

export const INSTRUMENT_SHORT: Record<InstrumentKind, string> = {
  output_voltage_sweep: "Output voltage sweep",
  source_current_sweep: "Source current sweep",
  transient_capture: "Transient capture",
};

export const PROBE_LABEL: Record<TransientProbe, string> = {
  output_voltage: "Output voltage",
  source_current: "Source current",
};

export const HYPOTHESIS_STATUS: Record<"supported" | "contradicted" | "unresolved", { text: string; tone: "success" | "danger" | "neutral" }> = {
  supported: { text: "Supported", tone: "success" },
  contradicted: { text: "Ruled out", tone: "danger" },
  unresolved: { text: "Still possible", tone: "neutral" },
};

const QUANTITY_LABEL: Record<string, string> = {
  output_gain_db: "output gain",
  output_phase_deg: "output phase",
  source_current_ma: "source current",
  source_current_phase_deg: "source current phase",
  tone_gain_db: "tone gain",
  tone_phase_deg: "tone phase",
  tone_amplitude_v: "tone amplitude",
  tone_current_ma: "tone current",
  tone_current_phase_deg: "tone current phase",
};

export function quantityLabel(q: string): string {
  return QUANTITY_LABEL[q] ?? q.replace(/_/g, " ");
}

export function observationTitle(o: Observation): string {
  if (o.kind === "symptom") return "Initial symptom";
  if (o.kind === "transient_capture") {
    return `Transient capture · ${o.probe ? PROBE_LABEL[o.probe] : "output"}`;
  }
  return INSTRUMENT_SHORT[o.kind];
}

export function observationLabel(o: Observation): string {
  return o.sequence === 0 ? "Symptom" : `Obs ${o.sequence}`;
}

export function hypothesisLabel(c: PublicCase, id: HypothesisId | null | undefined): string {
  if (!id) return "";
  // Labels come from the server catalogue (e.g. "C1 is disconnected (open)").
  return c.hypotheses.find((h: Hypothesis) => h.id === id)?.label ?? id;
}

export function requesterLabel(o: Observation): string {
  return o.requested_by === "system" ? "Standard check" : o.requested_by === "agent" ? "Requested by AI" : "Requested by you";
}

/** Quantities a citation may reference for each observation kind (mirrors the server list). */
export function citableQuantities(o: Observation): string[] {
  if (o.kind === "output_voltage_sweep") return ["output_gain_db", "output_phase_deg"];
  if (o.kind === "source_current_sweep") return ["source_current_ma", "source_current_phase_deg"];
  if (o.probe === "source_current") return ["tone_current_ma", "tone_current_phase_deg"];
  return ["tone_gain_db", "tone_phase_deg", "tone_amplitude_v"];
}

export function isLiveStatus(status: CaseStatus): boolean {
  return status !== "closed" && status !== "failed";
}
