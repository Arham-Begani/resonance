// Analytical first-order RC relations used only for the landing page's illustrations. These are
// textbook equations, not measurements: the lab itself measures every response with ngspice.
//
// Low-pass, ideal source V driving R in series and C to ground:
//   H(jω) = 1 / (1 + jωRC)              → |H| depends only on the product RC
//   I(jω) = V / (R + 1/(jωC))           → |I|/|V| = ωC / √(1 + (ωRC)²)
// So doubling R and doubling C give identical voltage responses, while the source current of the
// doubled-C circuit is exactly twice that of the doubled-R circuit at every frequency.

const TWO_PI = 2 * Math.PI;

export interface RcValues {
  /** Resistance in ohms. */
  r: number;
  /** Capacitance in farads. */
  c: number;
}

/** The lab's low-pass default (R1 = 1 kΩ, C1 = 100 nF; see the backend template). */
export const LOWPASS_DEFAULT: RcValues = { r: 1_000, c: 100e-9 };

export function cutoffHz({ r, c }: RcValues): number {
  return 1 / (TWO_PI * r * c);
}

/** |H| of the RC low-pass in dB (≤ 0). */
export function lowpassGainDb(f: number, { r, c }: RcValues): number {
  const x = TWO_PI * f * r * c;
  return -10 * Math.log10(1 + x * x);
}

/** Source-current magnitude per volt of source amplitude, in amperes per volt. */
export function sourceCurrentPerVolt(f: number, { r, c }: RcValues): number {
  const w = TWO_PI * f;
  return (w * c) / Math.sqrt(1 + (w * r * c) ** 2);
}

/** `n` points spaced evenly on a log axis from `min` to `max` (inclusive). */
export function logspace(min: number, max: number, n: number): number[] {
  const a = Math.log10(min);
  const b = Math.log10(max);
  return Array.from({ length: n }, (_, i) => 10 ** (a + ((b - a) * i) / (n - 1)));
}

export function scaled({ r, c }: RcValues, by: { r?: number; c?: number }): RcValues {
  return { r: r * (by.r ?? 1), c: c * (by.c ?? 1) };
}
