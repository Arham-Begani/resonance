// Circuit-family vocabulary shared by every mode. Facts about a family (tones, goals, ranges)
// always come from the backend templates; only display words live here.
import type { TemplateId, Tone } from "../api/types";

export const FAMILIES: TemplateId[] = ["rc_lowpass", "rc_highpass"];

export const FAMILY_LABEL: Record<TemplateId, string> = {
  rc_lowpass: "Low-pass",
  rc_highpass: "High-pass",
};

/** Plain description of what each family is for (shown next to the switch). */
export const FAMILY_PURPOSE: Record<TemplateId, string> = {
  rc_lowpass: "reduces higher-frequency interference",
  rc_highpass: "reduces lower-frequency interference",
};

/** Tones below this are often inaudible on laptop and phone speakers. */
export const LOW_TONE_HZ = 100;

/** A note for very low tones (e.g. 50 Hz mains hum); null when every tone is comfortably audible. */
export function lowToneNote(tones: Pick<Tone, "frequency_hz" | "label">[]): string | null {
  const low = tones.filter((t) => t.frequency_hz < LOW_TONE_HZ);
  if (low.length === 0) return null;
  const names = low.map((t) => `${Math.round(t.frequency_hz)} Hz ${t.label.toLowerCase()}`).join(" and ");
  return `Small speakers may not reproduce the ${names}; the charts and numbers still show it.`;
}
