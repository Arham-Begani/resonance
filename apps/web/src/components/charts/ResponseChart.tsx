// Frequency-response chart: stacked panels (e.g. gain over phase) sharing one log-frequency
// axis — never a dual-axis plot. Used for explore sweeps, case sweeps, and repair checks.
import { useMemo, useState, type ReactNode } from "react";
import { useAnnouncer } from "../../state/announcer";
import { formatHz, formatHzTick } from "../../lib/format";
import { ChartFigure, type ChartState, type LegendItem } from "./ChartFigure";
import { LinePlot, type LimitMarker, type PlotSeries, type PointLabel, type VMarker } from "./LinePlot";
import { niceDomain } from "./scales";

export interface ResponsePanel {
  id: string;
  /** Panel heading including the unit, e.g. "Gain (dB)". */
  title: string;
  series: PlotSeries[];
  height: number;
  valueFormat: (v: number) => string;
  tickFormat: (v: number) => string;
  domain?: [number, number];
  include?: number[];
  limits?: LimitMarker[];
  pointLabels?: PointLabel[];
}

interface ResponseChartProps {
  title: string;
  frequency: number[];
  panels: ResponsePanel[];
  markers?: VMarker[];
  summary?: ReactNode;
  state: ChartState;
  emptyText?: ReactNode;
  errorText?: ReactNode;
  stale?: boolean;
  toolbar?: ReactNode;
  tableCaption: string;
}

export function ResponseChart({ title, frequency, panels, markers, summary, state, emptyText, errorText, stale, toolbar, tableCaption }: ResponseChartProps) {
  const [hover, setHover] = useState<number | null>(null);
  const { announce } = useAnnouncer();

  const xDomain = useMemo<[number, number]>(() => {
    const finite = frequency.filter((f) => f > 0);
    return finite.length ? [Math.min(...finite), Math.max(...finite)] : [10, 100_000];
  }, [frequency]);

  const domains = useMemo(
    () => panels.map((p) => p.domain ?? niceDomain(p.series.flatMap((s) => s.y), { include: p.include })),
    [panels],
  );

  const legend: LegendItem[] = useMemo(() => {
    const seen = new Map<string, LegendItem>();
    panels.forEach((p) => p.series.forEach((s) => seen.set(s.label, { label: s.label, kind: s.kind })));
    return [...seen.values()];
  }, [panels]);

  const readoutFor = (i: number | null): string | null => {
    if (i === null || frequency[i] === undefined) return null;
    const parts = panels.map((p) => {
      const values = p.series
        .map((s) => (s.y[i] !== undefined && Number.isFinite(s.y[i]) ? `${s.label} ${p.valueFormat(s.y[i]!)}` : null))
        .filter(Boolean)
        .join(", ");
      return `${p.title.replace(/\s*\(.*\)$/, "")}: ${values}`;
    });
    return `${formatHz(frequency[i]!)} · ${parts.join(" · ")}`;
  };

  const onHover = (i: number | null, source: "pointer" | "keyboard") => {
    setHover(i);
    if (source === "keyboard" && i !== null) {
      const text = readoutFor(i);
      if (text) announce(text);
    }
  };

  const table = (
    <table className="data-table">
      <caption>{tableCaption}</caption>
      <thead>
        <tr>
          <th scope="col" className="n">
            Frequency
          </th>
          {panels.flatMap((p) =>
            p.series.map((s) => (
              <th key={`${p.id}-${s.id}`} scope="col" className="n">
                {s.label} {p.title.toLowerCase()}
              </th>
            )),
          )}
        </tr>
      </thead>
      <tbody>
        {frequency.map((f, i) => (
          <tr key={f}>
            <th scope="row" className="n">
              {formatHz(f)}
            </th>
            {panels.flatMap((p) =>
              p.series.map((s) => (
                <td key={`${p.id}-${s.id}`} className="n">
                  {s.y[i] !== undefined && Number.isFinite(s.y[i]) ? p.valueFormat(s.y[i]!) : "—"}
                </td>
              )),
            )}
          </tr>
        ))}
      </tbody>
    </table>
  );

  return (
    <ChartFigure
      title={title}
      legend={legend}
      readout={readoutFor(hover)}
      summary={summary}
      table={table}
      state={state}
      emptyText={emptyText}
      errorText={errorText}
      stale={stale}
      toolbar={toolbar}
      xAxisLabel="Frequency (Hz, log scale)"
    >
      {(width) => (
        <div className="response-panels">
          {panels.map((p, index) => (
            <div key={p.id} className="response-panel">
              <p className="response-panel__title">{p.title}</p>
              <LinePlot
                x={frequency}
                xScale="log"
                xDomain={xDomain}
                yDomain={domains[index]!}
                series={p.series}
                width={width}
                height={p.height}
                showXAxis={index === panels.length - 1}
                xTickFormat={formatHzTick}
                yTickFormat={p.tickFormat}
                markers={markers}
                markerLabels={index === 0}
                limits={p.limits}
                pointLabels={p.pointLabels}
                hoverIndex={hover}
                onHoverIndex={onHover}
                focusable={index === 0}
                ariaLabel={`${title}, ${p.title} versus frequency. Use the arrow keys to read values; the data table lists every point.`}
              />
            </div>
          ))}
        </div>
      )}
    </ChartFigure>
  );
}
