// Oscilloscope view of a short steady-state window. Reference/input traces are neutral dashed;
// output/observed traces are cyan solid.
import { useId, useMemo, useState, type ReactNode } from "react";
import { useAnnouncer } from "../../state/announcer";
import { formatMs, num } from "../../lib/format";
import { ChartFigure, type ChartState } from "./ChartFigure";
import { LinePlot, type PlotSeries } from "./LinePlot";
import { niceDomain } from "./scales";

interface WaveformChartProps {
  title: string;
  sampleRateHz: number;
  startS: number;
  series: PlotSeries[];
  summary?: ReactNode;
  state: ChartState;
  emptyText?: ReactNode;
  errorText?: ReactNode;
  stale?: boolean;
  toolbar?: ReactNode;
  height?: number;
  /** Highlight this series id (the active probe's trace). */
  emphasis?: string | null;
  unit?: "V" | "mA";
  /**
   * Offer a zoomed view of the first `zoomMs` of the clip (default view) beside the full window.
   * A two-tone signal over the whole window is too dense to read; the data table always lists
   * the full window.
   */
  zoomMs?: number;
}

const MAX_TABLE_ROWS = 400;

/** 2 → "2nd", 3 → "3rd", 11 → "11th". */
function ordinal(n: number): string {
  const tens = n % 100;
  const suffix = tens >= 11 && tens <= 13 ? "th" : n % 10 === 1 ? "st" : n % 10 === 2 ? "nd" : n % 10 === 3 ? "rd" : "th";
  return `${n}${suffix}`;
}

export function WaveformChart({ title, sampleRateHz, startS, series, summary, state, emptyText, errorText, stale, toolbar, height = 220, emphasis, unit = "V", zoomMs }: WaveformChartProps) {
  const zoomId = useId();
  const [hover, setHover] = useState<number | null>(null);
  const [zoomed, setZoomed] = useState(true);
  const { announce } = useAnnouncer();
  const length = Math.max(0, ...series.map((s) => s.y.length));
  const timeMs = useMemo(
    () => Array.from({ length }, (_, i) => (startS + i / Math.max(1, sampleRateHz)) * 1000),
    [length, startS, sampleRateHz],
  );
  const fullMs = length / Math.max(1, sampleRateHz) * 1000;
  const canZoom = zoomMs != null && fullMs > zoomMs * 1.3;
  const viewLength = canZoom && zoomed ? Math.min(length, Math.round((zoomMs! / 1000) * sampleRateHz) + 1) : length;
  const viewTime = useMemo(() => timeMs.slice(0, viewLength), [timeMs, viewLength]);
  const viewSeries = useMemo(() => series.map((s) => ({ ...s, y: s.y.slice(0, viewLength) })), [series, viewLength]);
  const xDomain = useMemo<[number, number]>(
    () => (viewTime.length > 1 ? [viewTime[0]!, viewTime[viewTime.length - 1]!] : [0, 1]),
    [viewTime],
  );
  const yDomain = useMemo(() => {
    const [lo, hi] = niceDomain(series.flatMap((s) => s.y), { minSpan: unit === "mA" ? 0.02 : 0.2, pad: 0.05 });
    const m = Math.max(Math.abs(lo), Math.abs(hi));
    return [-m, m] as [number, number];
  }, [series, unit]);

  const ordered = useMemo(() => {
    if (!emphasis) return viewSeries;
    // Draw the emphasized trace last so it sits on top.
    return [...viewSeries.filter((s) => s.id !== emphasis), ...viewSeries.filter((s) => s.id === emphasis)];
  }, [viewSeries, emphasis]);

  const readoutFor = (i: number | null) => {
    if (i === null || timeMs[i] === undefined) return null;
    const values = series
      .map((s) => (s.y[i] !== undefined ? `${s.label} ${num(s.y[i]!, 3)} ${unit}` : null))
      .filter(Boolean)
      .join(" · ");
    return `t = ${num(timeMs[i]!, 3)} ms · ${values}`;
  };

  const step = Math.max(1, Math.ceil(length / MAX_TABLE_ROWS));
  const rows = useMemo(() => {
    const out: number[] = [];
    for (let i = 0; i < length; i += step) out.push(i);
    return out;
  }, [length, step]);

  const table = (
    <table className="data-table">
      <caption>
        {title}: samples at {sampleRateHz.toLocaleString()} Hz{step > 1 ? `, every ${ordinal(step)} sample shown` : ""}, starting{" "}
        {formatMs(startS)} into the steady-state clip.
      </caption>
      <thead>
        <tr>
          <th scope="col" className="n">
            Time (ms)
          </th>
          {series.map((s) => (
            <th key={s.id} scope="col" className="n">
              {s.label} ({unit})
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((i) => (
          <tr key={i}>
            <th scope="row" className="n">
              {num(timeMs[i]!, 3)}
            </th>
            {series.map((s) => (
              <td key={s.id} className="n">
                {s.y[i] !== undefined ? num(s.y[i]!, 4) : "—"}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );

  return (
    <ChartFigure
      title={title}
      legend={series.map((s) => ({ label: s.label, kind: s.kind }))}
      readout={readoutFor(hover)}
      summary={summary}
      table={table}
      tableLabel="Sample table"
      state={state}
      emptyText={emptyText}
      errorText={errorText}
      stale={stale}
      toolbar={
        canZoom ? (
          <>
            {toolbar}
            <div className="segmented" role="radiogroup" aria-label={`${title} time window`}>
              <label>
                <input type="radio" name={`${zoomId}-zoom`} checked={zoomed} onChange={() => setZoomed(true)} />
                {num(zoomMs!, 0)} ms
              </label>
              <label>
                <input type="radio" name={`${zoomId}-zoom`} checked={!zoomed} onChange={() => setZoomed(false)} />
                Full
              </label>
            </div>
          </>
        ) : (
          toolbar
        )
      }
      xAxisLabel={canZoom && zoomed ? `Time (ms) · first ${num(zoomMs!, 0)} of ${num(fullMs, 0)} ms shown` : "Time (ms)"}
    >
      {(width) => (
        <div className="response-panel">
          <LinePlot
          title={unit === "mA" ? "Current (mA)" : "Voltage (V)"}
          x={viewTime}
          xScale="linear"
          xDomain={xDomain}
          yDomain={yDomain}
          series={ordered}
          width={width}
          height={height}
          showXAxis
          xTickFormat={(v) => num(v, 1)}
          yTickFormat={(v) => num(v, 2)}
          hoverIndex={hover}
          onHoverIndex={(i, source) => {
            setHover(i);
            if (source === "keyboard" && i !== null) {
              const text = readoutFor(i);
              if (text) announce(text);
            }
          }}
          ariaLabel={`${title}: ${unit === "mA" ? "current" : "voltage"} versus time. Use the arrow keys to read samples; the sample table lists the values.`}
          />
        </div>
      )}
    </ChartFigure>
  );
}
