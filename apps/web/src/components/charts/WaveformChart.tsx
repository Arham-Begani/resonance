// Oscilloscope view of a short steady-state window. Reference/input traces are neutral dashed;
// output/observed traces are cyan solid.
import { useMemo, useState, type ReactNode } from "react";
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
}

const MAX_TABLE_ROWS = 400;

export function WaveformChart({ title, sampleRateHz, startS, series, summary, state, emptyText, errorText, stale, toolbar, height = 220, emphasis, unit = "V" }: WaveformChartProps) {
  const [hover, setHover] = useState<number | null>(null);
  const { announce } = useAnnouncer();
  const length = Math.max(0, ...series.map((s) => s.y.length));
  const timeMs = useMemo(
    () => Array.from({ length }, (_, i) => (startS + i / Math.max(1, sampleRateHz)) * 1000),
    [length, startS, sampleRateHz],
  );
  const xDomain = useMemo<[number, number]>(
    () => (timeMs.length > 1 ? [timeMs[0]!, timeMs[timeMs.length - 1]!] : [0, 1]),
    [timeMs],
  );
  const yDomain = useMemo(() => {
    const [lo, hi] = niceDomain(series.flatMap((s) => s.y), { minSpan: unit === "mA" ? 0.02 : 0.2, pad: 0.05 });
    const m = Math.max(Math.abs(lo), Math.abs(hi));
    return [-m, m] as [number, number];
  }, [series, unit]);

  const ordered = useMemo(() => {
    if (!emphasis) return series;
    // Draw the emphasized trace last so it sits on top.
    return [...series.filter((s) => s.id !== emphasis), ...series.filter((s) => s.id === emphasis)];
  }, [series, emphasis]);

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
        {title}: samples at {sampleRateHz.toLocaleString()} Hz{step > 1 ? `, every ${step}th sample shown` : ""}, starting{" "}
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
      toolbar={toolbar}
      xAxisLabel="Time (ms)"
    >
      {(width) => (
        <div className="response-panel">
          <LinePlot
          title={unit === "mA" ? "Current (mA)" : "Voltage (V)"}
          x={timeMs}
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
