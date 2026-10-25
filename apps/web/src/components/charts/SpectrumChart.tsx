// Magnitude spectrum of the simulated steady-state clip (input dashed, output solid), with the
// demo tone peaks labelled directly. Dense spectra are decimated for drawing only.
import { useMemo, useState, type ReactNode } from "react";
import type { Spectrum, Tone } from "../../api/types";
import { useAnnouncer } from "../../state/announcer";
import { formatHz, formatHzTick, num } from "../../lib/format";
import { ChartFigure, type ChartState } from "./ChartFigure";
import { LinePlot, type PlotSeries } from "./LinePlot";
import { nearestIndex } from "./scales";

interface SpectrumChartProps {
  spectrum: Spectrum | null | undefined;
  tones: Tone[];
  state: ChartState;
  emptyText?: string;
  stale?: boolean;
  toolbar?: ReactNode;
  height?: number;
}

const FLOOR_DBV = -100;

export function SpectrumChart({ spectrum, tones, state, emptyText, stale, toolbar, height = 212 }: SpectrumChartProps) {
  const [hover, setHover] = useState<number | null>(null);
  const { announce } = useAnnouncer();

  const data = useMemo(() => {
    if (!spectrum) return null;
    const idx = spectrum.frequency_hz.map((f, i) => (f >= 10 ? i : -1)).filter((i) => i >= 0);
    const f = idx.map((i) => spectrum.frequency_hz[i]!);
    const clampFloor = (v: number) => (Number.isFinite(v) ? Math.max(FLOOR_DBV, v) : FLOOR_DBV);
    const input = idx.map((i) => clampFloor(spectrum.input_dbv[i]!));
    const output = idx.map((i) => clampFloor(spectrum.output_dbv[i]!));
    const peaks = tones.map((t) => {
      const i = nearestIndex(f, t.frequency_hz, true);
      return { tone: t, frequency: f[i] ?? t.frequency_hz, input: input[i] ?? NaN, output: output[i] ?? NaN };
    });
    return { f, input, output, peaks };
  }, [spectrum, tones]);

  const series: PlotSeries[] = data
    ? [
        { id: "input", label: "Input", kind: "reference", y: data.input },
        { id: "output", label: "Output", kind: "primary", y: data.output },
      ]
    : [];

  const effectiveState: ChartState = state === "ready" && (!data || data.f.length < 2) ? "empty" : state;
  const xDomain: [number, number] = data && data.f.length > 1 ? [Math.max(20, data.f[0]!), Math.min(20_000, data.f[data.f.length - 1]!)] : [20, 20_000];

  const readoutFor = (i: number | null) => {
    if (!data || i === null || data.f[i] === undefined) return null;
    return `${formatHz(data.f[i]!)} · Input ${num(data.input[i]!, 1)} dBV · Output ${num(data.output[i]!, 1)} dBV`;
  };

  const summary = data ? (
    <>
      Peaks:{" "}
      {data.peaks.map((p, k) => (
        <span key={p.tone.frequency_hz}>
          {k > 0 ? "; " : ""}
          <strong>
            {p.tone.label.toLowerCase()} at {formatHz(p.tone.frequency_hz)}
          </strong>{" "}
          input {num(p.input, 1)} dBV, output {num(p.output, 1)} dBV
        </span>
      ))}
      . Derived from the simulated samples ({spectrum?.method}; resolution {num(spectrum?.resolution_hz ?? 0, 2)} Hz).
    </>
  ) : null;

  const table = data ? (
    <table className="data-table">
      <caption>Spectrum levels at the demo tones (nearest bin). The full spectrum has {data.f.length.toLocaleString()} bins.</caption>
      <thead>
        <tr>
          <th scope="col">Tone</th>
          <th scope="col" className="n">
            Bin
          </th>
          <th scope="col" className="n">
            Input (dBV)
          </th>
          <th scope="col" className="n">
            Output (dBV)
          </th>
          <th scope="col" className="n">
            Change (dB)
          </th>
        </tr>
      </thead>
      <tbody>
        {data.peaks.map((p) => (
          <tr key={p.tone.frequency_hz}>
            <th scope="row">{p.tone.label}</th>
            <td className="n">{formatHz(p.frequency)}</td>
            <td className="n">{num(p.input, 1)}</td>
            <td className="n">{num(p.output, 1)}</td>
            <td className="n">{num(p.output - p.input, 1)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  ) : null;

  return (
    <ChartFigure
      title="Spectrum"
      legend={[
        { label: "Input", kind: "reference" },
        { label: "Output", kind: "primary" },
      ]}
      readout={readoutFor(hover)}
      summary={summary}
      table={table}
      tableLabel="Peak table"
      state={effectiveState}
      emptyText={emptyText}
      stale={stale}
      toolbar={toolbar}
      xAxisLabel="Frequency (Hz, log scale)"
    >
      {(width) =>
        data ? (
          <div className="response-panel">
            <LinePlot
              title="Level (dBV)"
              x={data.f}
              xScale="log"
              xDomain={xDomain}
              yDomain={[FLOOR_DBV, 0]}
              series={series}
              width={width}
              height={height}
              showXAxis
              xTickFormat={formatHzTick}
              yTickFormat={(v) => num(v, 0)}
              markers={tones.map((t) => ({ x: t.frequency_hz, label: formatHz(t.frequency_hz) }))}
              pointLabels={data.peaks.map((p) => ({ x: p.frequency, y: p.output, text: `${num(p.output, 1)} dBV` }))}
              hoverIndex={hover}
              onHoverIndex={(i, source) => {
                setHover(i);
                if (source === "keyboard" && i !== null) {
                  const text = readoutFor(i);
                  if (text) announce(text);
                }
              }}
              maxDrawPoints={900}
              ariaLabel="Spectrum: level in dBV versus frequency for input and output. Use the arrow keys to read values; the peak table lists the tone levels."
            />
          </div>
        ) : null
      }
    </ChartFigure>
  );
}
