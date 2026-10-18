// Scale and tick helpers shared by the charts.
import { scaleLinear, scaleLog, type ScaleContinuousNumeric } from "d3-scale";

export type XScaleKind = "log" | "linear";

export function makeXScale(kind: XScaleKind, domain: [number, number], range: [number, number]): ScaleContinuousNumeric<number, number> {
  return kind === "log" ? scaleLog().domain(domain).range(range) : scaleLinear().domain(domain).range(range);
}

/** Major (labelled) and minor ticks for a log frequency axis. */
export function logTicks(domain: [number, number]): { major: number[]; minor: number[] } {
  const [lo, hi] = domain;
  const major: number[] = [];
  const minor: number[] = [];
  for (let exp = Math.floor(Math.log10(lo)); exp <= Math.ceil(Math.log10(hi)); exp++) {
    const decade = 10 ** exp;
    for (let m = 1; m <= 9; m++) {
      const v = decade * m;
      if (v < lo * 0.999 || v > hi * 1.001) continue;
      if (m === 1) major.push(v);
      else minor.push(v);
    }
  }
  return { major, minor };
}

/** A padded, nicely rounded y domain covering every finite value. */
export function niceDomain(values: number[], opts: { include?: number[]; pad?: number; minSpan?: number } = {}): [number, number] {
  const finite = values.filter((v) => Number.isFinite(v)).concat(opts.include ?? []);
  if (finite.length === 0) return [0, 1];
  let lo = Math.min(...finite);
  let hi = Math.max(...finite);
  const minSpan = opts.minSpan ?? 1;
  if (hi - lo < minSpan) {
    const mid = (hi + lo) / 2;
    lo = mid - minSpan / 2;
    hi = mid + minSpan / 2;
  }
  const pad = (hi - lo) * (opts.pad ?? 0.08);
  const scale = scaleLinear().domain([lo - pad, hi + pad]).nice(5);
  const [a, b] = scale.domain() as [number, number];
  return [a, b];
}

/** Gain axis: 10 dB steps below, a little headroom above 0 dB. */
export function gainDomain(values: number[], extra: number[] = []): [number, number] {
  const finite = values.concat(extra).filter((v) => Number.isFinite(v));
  if (finite.length === 0) return [-40, 5];
  const lo = Math.floor((Math.min(...finite) - 3) / 10) * 10;
  const hi = Math.max(5, Math.ceil((Math.max(...finite) + 1) / 5) * 5);
  return [lo, hi];
}

/** Magnitudes (e.g. current in mA) start at zero. */
export function magnitudeDomain(values: number[]): [number, number] {
  const finite = values.filter((v) => Number.isFinite(v));
  const max = finite.length ? Math.max(...finite) : 1;
  const [, hi] = scaleLinear().domain([0, max > 0 ? max * 1.08 : 1]).nice(5).domain() as [number, number];
  return [0, hi];
}

export function linearTicks(domain: [number, number], count = 5): number[] {
  return scaleLinear().domain(domain).ticks(count);
}

/** Index of the x value nearest to `target` (x must be sorted ascending). */
export function nearestIndex(xs: number[], target: number, log = false): number {
  if (xs.length === 0) return -1;
  const t = log ? Math.log10(Math.max(target, 1e-12)) : target;
  let best = 0;
  let bestDist = Infinity;
  for (let i = 0; i < xs.length; i++) {
    const v = log ? Math.log10(Math.max(xs[i]!, 1e-12)) : xs[i]!;
    const d = Math.abs(v - t);
    if (d < bestDist) {
      bestDist = d;
      best = i;
    }
  }
  return best;
}

/** Linear interpolation of y at x on a log-x series (used for −3 dB crossing estimates). */
export function crossing(xs: number[], ys: number[], level: number): number | null {
  for (let i = 1; i < xs.length; i++) {
    const y0 = ys[i - 1]!;
    const y1 = ys[i]!;
    if ((y0 - level) * (y1 - level) <= 0 && y0 !== y1) {
      const t = (level - y0) / (y1 - y0);
      const lx = Math.log10(xs[i - 1]!) + t * (Math.log10(xs[i]!) - Math.log10(xs[i - 1]!));
      return 10 ** lx;
    }
  }
  return null;
}

/**
 * Peak-preserving decimation for dense series: for every horizontal pixel, keeps the index of
 * each series' largest value there. Used only for drawing; tables keep the full data.
 */
export function decimateByPixel(xs: number[], series: number[][], toPx: (x: number) => number, maxPoints: number): number[] {
  if (xs.length <= maxPoints) return xs.map((_, i) => i);
  const keep = new Set<number>();
  for (const ys of series) {
    const best = new Map<number, number>();
    for (let i = 0; i < xs.length; i++) {
      const px = Math.round(toPx(xs[i]!));
      const current = best.get(px);
      if (current === undefined || (ys[i] ?? -Infinity) > (ys[current] ?? -Infinity)) best.set(px, i);
    }
    best.forEach((i) => keep.add(i));
  }
  return [...keep].sort((a, b) => a - b);
}
