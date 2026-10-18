// One SVG line panel: hairline grid, readable ticks, 2px traces distinguished by line style,
// optional tone markers, goal limits, and selective direct labels. Hover/keyboard crosshair is
// controlled by the parent so stacked panels (gain + phase) stay in sync.
import { line } from "d3-shape";
import { useMemo, type KeyboardEvent, type PointerEvent } from "react";
import { decimateByPixel, linearTicks, logTicks, makeXScale, nearestIndex, type XScaleKind } from "./scales";

export type SeriesKind = "reference" | "primary" | "muted";

export interface PlotSeries {
  id: string;
  label: string;
  kind: SeriesKind;
  y: number[];
}

export interface VMarker {
  x: number;
  label: string;
}

export interface LimitMarker {
  x: number;
  y: number;
  /** "above": the trace must stay at or above y; "below": at or below. */
  direction: "above" | "below";
  label: string;
}

export interface PointLabel {
  x: number;
  y: number;
  text: string;
}

export interface LinePlotProps {
  x: number[];
  xScale: XScaleKind;
  xDomain: [number, number];
  yDomain: [number, number];
  series: PlotSeries[];
  width: number;
  height: number;
  showXAxis: boolean;
  xTickFormat: (v: number) => string;
  yTickFormat: (v: number) => string;
  markers?: VMarker[];
  /** Draw marker text (only on one panel of a stack, to avoid repeating it over traces). */
  markerLabels?: boolean;
  limits?: LimitMarker[];
  pointLabels?: PointLabel[];
  hoverIndex: number | null;
  onHoverIndex: (index: number | null, source: "pointer" | "keyboard") => void;
  ariaLabel: string;
  maxDrawPoints?: number;
  focusable?: boolean;
}

export const PLOT_MARGIN = { top: 16, right: 16, left: 52, bottom: 28 };

const DASH: Record<SeriesKind, string | undefined> = {
  reference: "6 4",
  primary: undefined,
  muted: "2 3",
};

export function LinePlot(props: LinePlotProps) {
  const {
    x,
    xScale: kind,
    xDomain,
    yDomain,
    series,
    width,
    height,
    showXAxis,
    xTickFormat,
    yTickFormat,
    markers = [],
    markerLabels = true,
    limits = [],
    pointLabels = [],
    hoverIndex,
    onHoverIndex,
    ariaLabel,
    maxDrawPoints = 1200,
    focusable = true,
  } = props;
  const m = { ...PLOT_MARGIN, bottom: showXAxis ? PLOT_MARGIN.bottom : 6 };
  const innerW = Math.max(40, width - m.left - m.right);
  const innerH = Math.max(30, height - m.top - m.bottom);
  const sx = useMemo(() => makeXScale(kind, xDomain, [0, innerW]), [kind, xDomain, innerW]);
  const sy = useMemo(() => makeXScale("linear", yDomain, [innerH, 0]), [yDomain, innerH]);

  const xTicks = useMemo(() => {
    if (kind === "log") return logTicks(xDomain);
    return { major: linearTicks(xDomain, Math.max(2, Math.floor(innerW / 90))), minor: [] as number[] };
  }, [kind, xDomain, innerW]);
  const yTicks = useMemo(() => linearTicks(yDomain, Math.max(2, Math.floor(innerH / 36))), [yDomain, innerH]);

  const drawIndices = useMemo(
    () => decimateByPixel(x, series.map((s) => s.y), (v) => sx(v), maxDrawPoints),
    [x, series, sx, maxDrawPoints],
  );

  const clampY = (v: number) => Math.max(yDomain[0], Math.min(yDomain[1], v));

  const paths = useMemo(
    () =>
      series.map((s) => {
        const gen = line<number>()
          .defined((i) => Number.isFinite(s.y[i] ?? NaN) && Number.isFinite(x[i] ?? NaN))
          .x((i) => sx(x[i]!))
          .y((i) => sy(Math.max(yDomain[0], Math.min(yDomain[1], s.y[i]!))));
        return { id: s.id, kind: s.kind, d: gen(drawIndices) ?? "" };
      }),
    [series, drawIndices, sx, sy, x, yDomain],
  );

  const indexFromPointer = (event: PointerEvent<SVGRectElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    const px = event.clientX - rect.left;
    const value = sx.invert(Math.max(0, Math.min(innerW, px)));
    return nearestIndex(x, value, kind === "log");
  };

  const onKeyDown = (event: KeyboardEvent<SVGSVGElement>) => {
    if (x.length === 0) return;
    const current = hoverIndex ?? -1;
    const big = Math.max(1, Math.round(x.length / 10));
    let next: number | null = null;
    if (event.key === "ArrowRight") next = Math.min(x.length - 1, current + 1);
    else if (event.key === "ArrowLeft") next = Math.max(0, current < 0 ? 0 : current - 1);
    else if (event.key === "PageUp") next = Math.min(x.length - 1, current + big);
    else if (event.key === "PageDown") next = Math.max(0, current - big);
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = x.length - 1;
    else if (event.key === "Escape") {
      onHoverIndex(null, "keyboard");
      return;
    }
    if (next !== null) {
      event.preventDefault();
      onHoverIndex(next, "keyboard");
    }
  };

  const hx = hoverIndex !== null && x[hoverIndex] !== undefined ? sx(x[hoverIndex]!) : null;
  const inDomain = (v: number) => v >= Math.min(...xDomain) && v <= Math.max(...xDomain);

  return (
    <svg
      className="plot"
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={ariaLabel}
      tabIndex={focusable ? 0 : undefined}
      onKeyDown={focusable ? onKeyDown : undefined}
      onBlur={() => onHoverIndex(null, "keyboard")}
    >
      <g transform={`translate(${m.left} ${m.top})`}>
        {/* Grid */}
        <g className="plot__grid" aria-hidden="true">
          {xTicks.minor.map((v) => (
            <line key={`mx${v}`} x1={sx(v)} x2={sx(v)} y1={0} y2={innerH} className="plot__grid-minor" />
          ))}
          {xTicks.major.map((v) => (
            <line key={`x${v}`} x1={sx(v)} x2={sx(v)} y1={0} y2={innerH} />
          ))}
          {yTicks.map((v) => (
            <line key={`y${v}`} x1={0} x2={innerW} y1={sy(v)} y2={sy(v)} />
          ))}
        </g>

        {/* Y ticks */}
        <g className="plot__ticks" aria-hidden="true">
          {yTicks.map((v) => (
            <text key={`yt${v}`} x={-8} y={sy(v)} dy="0.32em" textAnchor="end">
              {yTickFormat(v)}
            </text>
          ))}
        </g>

        {/* X ticks */}
        {showXAxis ? (
          <g className="plot__ticks" aria-hidden="true">
            <line x1={0} x2={innerW} y1={innerH} y2={innerH} className="plot__axis" />
            {xTicks.major.map((v) => (
              <text key={`xt${v}`} x={sx(v)} y={innerH + 18} textAnchor="middle">
                {xTickFormat(v)}
              </text>
            ))}
          </g>
        ) : null}

        {/* Tone markers */}
        <g className="plot__markers" aria-hidden="true">
          {markers.filter((mk) => inDomain(mk.x)).map((mk) => (
            <g key={`mk${mk.x}`}>
              <line x1={sx(mk.x)} x2={sx(mk.x)} y1={0} y2={innerH} />
              {markerLabels ? (
                <text x={sx(mk.x) + 4} y={innerH - 6}>
                  {mk.label}
                </text>
              ) : null}
            </g>
          ))}
        </g>

        {/* Traces */}
        <g className="plot__traces" aria-hidden="true">
          {paths.map((p) => (
            <path key={p.id} d={p.d} className={`trace trace--${p.kind}`} strokeDasharray={DASH[p.kind]} />
          ))}
        </g>

        {/* Goal limits */}
        <g className="plot__limits" aria-hidden="true">
          {limits.filter((l) => inDomain(l.x) && l.y >= yDomain[0] && l.y <= yDomain[1]).map((l) => {
            const cx = sx(l.x);
            const cy = sy(l.y);
            const caret = l.direction === "above" ? `M${cx - 5} ${cy + 7} L${cx} ${cy + 1} L${cx + 5} ${cy + 7}` : `M${cx - 5} ${cy - 7} L${cx} ${cy - 1} L${cx + 5} ${cy - 7}`;
            // Labels sit left of the marker and below the limit line, away from the direct
            // value labels (right/above), so the two never collide.
            const labelLeft = cx > 70;
            return (
              <g key={`lim${l.x}${l.y}`}>
                <line x1={cx - 14} x2={cx + 14} y1={cy} y2={cy} />
                <path d={caret} />
                <text x={labelLeft ? cx - 16 : cx + 16} y={cy + 14} textAnchor={labelLeft ? "end" : "start"}>
                  {l.label}
                </text>
              </g>
            );
          })}
        </g>

        {/* Direct labels */}
        <g className="plot__points" aria-hidden="true">
          {pointLabels.filter((p) => inDomain(p.x) && Number.isFinite(p.y)).map((p) => {
            const px = sx(p.x);
            const py = sy(clampY(p.y));
            // Right of the point, clear of the 14px goal-limit segment drawn at the same x.
            const right = px < innerW - 80;
            return (
              <g key={`pt${p.x}`}>
                <circle cx={px} cy={py} r={4} className="plot__point" />
                <text x={right ? px + 18 : px - 18} y={py} dy="0.32em" textAnchor={right ? "start" : "end"} className="plot__point-label">
                  {p.text}
                </text>
              </g>
            );
          })}
        </g>

        {/* Crosshair */}
        {hx !== null && hoverIndex !== null ? (
          <g className="plot__crosshair" aria-hidden="true">
            <line x1={hx} x2={hx} y1={0} y2={innerH} />
            {series.map((s) => {
              const v = s.y[hoverIndex];
              if (v === undefined || !Number.isFinite(v)) return null;
              return <circle key={s.id} cx={hx} cy={sy(clampY(v))} r={4} className={`plot__dot plot__dot--${s.kind}`} />;
            })}
          </g>
        ) : null}

        {/* Pointer layer: the whole plot is the hit target; the crosshair snaps to the nearest x. */}
        <rect
          className="plot__hit"
          x={0}
          y={0}
          width={innerW}
          height={innerH}
          onPointerMove={(e) => onHoverIndex(indexFromPointer(e), "pointer")}
          onPointerLeave={() => onHoverIndex(null, "pointer")}
        />
      </g>
    </svg>
  );
}
