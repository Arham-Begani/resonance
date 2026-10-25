// Small log-frequency line plot for the landing page's analytical illustrations. Drawn at the
// container's real pixel width so 12px labels stay 12px at every screen size. Identity never
// relies on color alone: the primary series is a solid cyan line, the reference a neutral dashed
// line, and both carry direct labels. Each figure that uses this plot also ships a data table.
import { useElementWidth } from "../../lib/hooks";

export interface PlotPoint {
  f: number;
  y: number;
}

export interface PlotSeries {
  id: string;
  points: PlotPoint[];
  variant: "primary" | "reference";
  /** Draw the line in once on first paint (only when the viewer allows motion; see landing.css). */
  draw?: boolean;
}

export interface PlotLabel {
  f: number;
  y: number;
  text: string;
  anchor?: "start" | "middle" | "end";
  /** Offsets in pixels from the point (negative dy is up). */
  dx?: number;
  dy?: number;
}

interface ResponsePlotProps {
  /** Accessible name: a one-sentence summary of what the plot shows. */
  label: string;
  series: PlotSeries[];
  yDomain: [number, number];
  yLog?: boolean;
  yTicks: number[];
  formatY: (value: number) => string;
  height?: number;
  cutoff?: { f: number; text: string };
  markers?: PlotPoint[];
  labels?: PlotLabel[];
}

const X_MIN = 20;
const X_MAX = 20_000;
const X_TICKS: { f: number; text: string }[] = [
  { f: 100, text: "100 Hz" },
  { f: 1_000, text: "1 kHz" },
  { f: 10_000, text: "10 kHz" },
];
const M = { top: 16, right: 12, bottom: 28, left: 48 };

export function ResponsePlot({
  label,
  series,
  yDomain,
  yLog = false,
  yTicks,
  formatY,
  height = 200,
  cutoff,
  markers = [],
  labels = [],
}: ResponsePlotProps) {
  const [ref, width] = useElementWidth<HTMLDivElement>(480);
  const innerW = Math.max(width - M.left - M.right, 120);
  const innerH = height - M.top - M.bottom;

  const lx0 = Math.log10(X_MIN);
  const lx1 = Math.log10(X_MAX);
  const x = (f: number) => M.left + ((Math.log10(f) - lx0) / (lx1 - lx0)) * innerW;
  const ty = (v: number) => (yLog ? Math.log10(v) : v);
  const [y0, y1] = [ty(yDomain[0]), ty(yDomain[1])];
  const y = (v: number) => M.top + (1 - (ty(v) - y0) / (y1 - y0)) * innerH;
  const path = (pts: PlotPoint[]) =>
    pts.map((p, i) => `${i === 0 ? "M" : "L"}${x(p.f).toFixed(1)} ${y(p.y).toFixed(1)}`).join(" ");

  return (
    <div ref={ref} className="l-plot">
      <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={label}>
        <g className="l-plot__grid" aria-hidden="true">
          {yTicks.map((t) => (
            <line key={`y${t}`} x1={M.left} x2={M.left + innerW} y1={y(t)} y2={y(t)} />
          ))}
          {X_TICKS.map((t) => (
            <line key={`x${t.f}`} x1={x(t.f)} x2={x(t.f)} y1={M.top} y2={M.top + innerH} />
          ))}
        </g>
        <g className="l-plot__ticks" aria-hidden="true">
          {yTicks.map((t) => (
            <text key={`y${t}`} x={M.left - 8} y={y(t)} dy="0.35em" textAnchor="end">
              {formatY(t)}
            </text>
          ))}
          {X_TICKS.map((t) => (
            <text key={`x${t.f}`} x={x(t.f)} y={height - 8} textAnchor="middle">
              {t.text}
            </text>
          ))}
        </g>
        {cutoff ? (
          <g className="l-plot__cutoff" aria-hidden="true">
            <line x1={x(cutoff.f)} x2={x(cutoff.f)} y1={M.top} y2={M.top + innerH} />
            <text x={x(cutoff.f) + 6} y={M.top + 12}>
              {cutoff.text}
            </text>
          </g>
        ) : null}
        {series.map((s) => (
          <path
            key={s.id}
            d={path(s.points)}
            className={`l-plot__line l-plot__line--${s.variant}${s.draw ? " l-plot__line--draw" : ""}`}
            pathLength={s.draw ? 1 : undefined}
            data-series={s.id}
          />
        ))}
        {markers.map((m) => (
          <circle key={`m${m.f}`} className="l-plot__marker" cx={x(m.f)} cy={y(m.y)} r={4} aria-hidden="true" />
        ))}
        {labels.map((l) => (
          <text
            key={`l${l.text}`}
            className="l-plot__label"
            x={x(l.f) + (l.dx ?? 0)}
            y={y(l.y) + (l.dy ?? -10)}
            textAnchor={l.anchor ?? "middle"}
            aria-hidden="true"
          >
            {l.text}
          </text>
        ))}
      </svg>
    </div>
  );
}
