// Number formatting for measurements. Uses a true minus sign (U+2212), which screen readers
// announce as "minus", and SI prefixes that match the backend's display values.

const MINUS = "−";

function trimZeros(text: string): string {
  return text.includes(".") ? text.replace(/\.?0+$/, "") : text;
}

export function signed(value: number, digits = 1): string {
  const rounded = Number(value.toFixed(digits));
  const abs = Math.abs(rounded).toFixed(digits);
  if (rounded === 0) return abs;
  return rounded < 0 ? `${MINUS}${abs}` : `+${abs}`;
}

/** A plain number with a true minus sign (no forced plus). */
export function num(value: number, digits = 1): string {
  const rounded = Number(value.toFixed(digits));
  const abs = Math.abs(rounded).toFixed(digits);
  return rounded < 0 ? `${MINUS}${abs}` : abs;
}

export function formatDb(value: number, digits = 1): string {
  return `${num(value, digits)} dB`;
}

export function formatSignedDb(value: number, digits = 1): string {
  return `${signed(value, digits)} dB`;
}

export function formatDeg(value: number, digits = 1): string {
  return `${num(value, digits)}°`;
}

export function formatHz(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 1000) {
    const k = value / 1000;
    return `${trimZeros(k.toFixed(k >= 100 ? 0 : k >= 10 ? 1 : 2))} kHz`;
  }
  return `${trimZeros(value.toFixed(value >= 100 ? 0 : value >= 10 ? 1 : 2))} Hz`;
}

/** Compact axis tick label for a frequency (20, 100, 1k, 10k). */
export function formatHzTick(value: number): string {
  if (value >= 1000) return `${trimZeros((value / 1000).toFixed(1))}k`;
  return trimZeros(value.toFixed(value < 10 ? 1 : 0));
}

export function formatMa(value: number, digits = 3): string {
  return `${num(value, digits)} mA`;
}

export function formatMs(seconds: number, digits = 2): string {
  return `${num(seconds * 1000, digits)} ms`;
}

export function formatVolts(value: number, digits = 3): string {
  return `${num(value, digits)} V`;
}

const PREFIXES: [number, string][] = [
  [1e6, "M"],
  [1e3, "k"],
  [1, ""],
  [1e-3, "m"],
  [1e-6, "µ"],
  [1e-9, "n"],
  [1e-12, "p"],
];

/** Format a canonical SI component value, e.g. 4700 ohm → "4.7 kΩ", 1e-7 farad → "100 nF". */
export function formatSi(value: number, unit: "ohm" | "farad" | null | undefined): string {
  const symbol = unit === "ohm" ? "Ω" : unit === "farad" ? "F" : "";
  if (value === 0) return `0 ${symbol}`.trim();
  const abs = Math.abs(value);
  const [scale, prefix] = PREFIXES.find(([s]) => abs >= s * 0.9999) ?? PREFIXES[PREFIXES.length - 1]!;
  const scaled = value / scale;
  const digits = Math.abs(scaled) >= 100 ? 0 : Math.abs(scaled) >= 10 ? 1 : 2;
  return `${trimZeros(scaled.toFixed(digits))} ${prefix}${symbol}`;
}

export function formatClock(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function formatDurationMs(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${trimZeros((ms / 1000).toFixed(1))} s`;
}

export function dbToGain(db: number): number {
  return 10 ** (db / 20);
}

export function capitalize(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}
