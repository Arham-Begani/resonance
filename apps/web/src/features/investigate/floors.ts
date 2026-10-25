// Measurement floor (verification finding M2-02). A gain at or below the floor, or a current at
// or below the current floor, is "no measurable output/current": it is shown as such, its phase
// is not meaningful ("—"), and no difference is computed against it.
//
// The floors come from EvidencePolicy.measurement_floor_db / current_floor_ma once the backend
// publishes them; until then the values the server already states in its instrument
// descriptions (−120 dB, 1e-6 mA) are used.
import type { HypothesisCheck, ObservedTone, PublicCase } from "../../api/types";
import { formatDb, formatDeg, formatMa, num } from "../../lib/format";

export const DEFAULT_FLOOR_DB = -120;
export const DEFAULT_CURRENT_FLOOR_MA = 1e-6;

export interface Floors {
  db: number;
  ma: number;
}

type PolicyWithFloors = PublicCase["policy"] & { measurement_floor_db?: number | null; current_floor_ma?: number | null };

export function floorsOf(c: Pick<PublicCase, "policy">): Floors {
  const p = c.policy as PolicyWithFloors;
  return { db: p.measurement_floor_db ?? DEFAULT_FLOOR_DB, ma: p.current_floor_ma ?? DEFAULT_CURRENT_FLOOR_MA };
}

export const NO_OUTPUT = "No measurable output";
export const NO_CURRENT = "No measurable current";

export function dbBelow(value: number | null | undefined, f: Floors): boolean {
  return value != null && (!Number.isFinite(value) || value <= f.db + 1e-9);
}

export function maBelow(value: number | null | undefined, f: Floors): boolean {
  return value != null && (!Number.isFinite(value) || Math.abs(value) <= f.ma);
}

/** A tone side (observed or expected) is unmeasurable when its gain (V) or current (mA) is at the floor. */
export function toneBelow(t: ObservedTone, which: "observed" | "expected", f: Floors): boolean {
  if (t.amplitude_unit === "mA") return maBelow(which === "observed" ? t.amplitude : t.expected_amplitude, f);
  return dbBelow(which === "observed" ? t.gain_db : t.expected_gain_db, f);
}

export function toneAmplitudeText(t: ObservedTone, which: "observed" | "expected", f: Floors): string {
  if (toneBelow(t, which, f)) return t.amplitude_unit === "mA" ? NO_CURRENT : NO_OUTPUT;
  const amp = which === "observed" ? t.amplitude : t.expected_amplitude;
  return t.amplitude_unit === "mA" ? formatMa(amp) : `${num(amp, 3)} V`;
}

export function toneGainText(t: ObservedTone, which: "observed" | "expected", f: Floors): string {
  const g = which === "observed" ? t.gain_db : t.expected_gain_db;
  if (g == null) return "—";
  return dbBelow(g, f) ? NO_OUTPUT : formatDb(g);
}

/** Phase of an unmeasurable side is not meaningful. */
export function tonePhaseText(t: ObservedTone, which: "observed" | "expected", f: Floors): string {
  if (toneBelow(t, which, f)) return "—";
  return formatDeg(which === "observed" ? t.phase_deg : t.expected_phase_deg);
}

/** "observed −14.3 vs predicted −8.6 dB", with floored sides named instead of numbered. */
export function checkText(ch: HypothesisCheck, f: Floors): string {
  const below = (v: number) => (ch.unit === "dB" ? dbBelow(v, f) : ch.unit === "mA" ? maBelow(v, f) : false);
  const word = ch.unit === "mA" ? "no measurable current" : "no measurable output";
  const side = (v: number) => (below(v) ? word : `${num(v, 2)} ${ch.unit}`);
  return `observed ${side(ch.observed)} vs predicted ${side(ch.predicted)}`;
}
