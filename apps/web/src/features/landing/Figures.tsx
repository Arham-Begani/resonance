// The landing page's two figures. Both are computed from textbook RC equations (rc.ts) and are
// labeled "Analytical" on screen: they illustrate the physics, they are not lab measurements.
import { formatDb, formatHz, num } from "../../lib/format";
import { RcSchematic } from "./RcSchematic";
import { ResponsePlot, type PlotPoint } from "./ResponsePlot";
import { cutoffHz, logspace, LOWPASS_DEFAULT, lowpassGainDb, scaled, sourceCurrentPerVolt, type RcValues } from "./rc";

const FREQS = logspace(20, 20_000, 160);

function gainCurve(v: RcValues): PlotPoint[] {
  return FREQS.map((f) => ({ f, y: lowpassGainDb(f, v) }));
}

/** Source current per volt of source, in mA/V. */
function currentCurve(v: RcValues): PlotPoint[] {
  return FREQS.map((f) => ({ f, y: sourceCurrentPerVolt(f, v) * 1e3 }));
}

const formatGainTick = (v: number) => (v === 0 ? "0 dB" : num(v, 0));
const formatMa = (v: number) => `${Number(v.toPrecision(2))} mA`;

function AnalyticalTag() {
  return <span className="l-tag">Analytical</span>;
}

export function HeroFigure() {
  const v = LOWPASS_DEFAULT;
  const fc = cutoffHz(v);
  const g440 = lowpassGainDb(440, v);
  const g4k = lowpassGainDb(4_000, v);
  return (
    <figure className="l-instrument" aria-labelledby="hero-figure-title">
      <div className="l-instrument__bar">
        <h2 className="l-instrument__title" id="hero-figure-title">
          RC low-pass response
        </h2>
        <AnalyticalTag />
      </div>
      <div className="l-instrument__body">
        <RcSchematic resistor="1 kΩ" capacitor="100 nF" />
        <ResponsePlot
          label={`Analytical output gain from 20 Hz to 20 kHz. Flat near 0 dB at low frequencies, −3 dB at the ${formatHz(fc)} cutoff, then falling 6 dB per octave. The 440 Hz tone is at ${formatDb(g440)}, the 4 kHz whistle at ${formatDb(g4k)}.`}
          series={[{ id: "gain", points: gainCurve(v), variant: "primary", draw: true }]}
          yDomain={[-30, 4]}
          yTicks={[0, -10, -20, -30]}
          formatY={formatGainTick}
          cutoff={{ f: fc, text: `fc ${formatHz(fc)}` }}
          markers={[
            { f: 440, y: g440 },
            { f: 4_000, y: g4k },
          ]}
          labels={[
            { f: 440, y: g440, text: `440 Hz  ${formatDb(g440)}`, dy: 22 },
            { f: 4_000, y: g4k, text: `4 kHz  ${formatDb(g4k)}`, anchor: "start", dx: 10, dy: -8 },
          ]}
        />
      </div>
      <figcaption className="l-instrument__caption">
        Output gain |H| = 1/√(1 + (f/f<sub>c</sub>)²) for the lab’s default values. This curve is calculated, not
        measured; in the lab, ngspice measures it.
      </figcaption>
    </figure>
  );
}

const R2 = scaled(LOWPASS_DEFAULT, { r: 2 });
const C2 = scaled(LOWPASS_DEFAULT, { c: 2 });
const TABLE_FREQS = [100, 1_000, 4_000];

function LegendSwatch({ variant }: { variant: "primary" | "reference" }) {
  return (
    <svg width="24" height="8" aria-hidden="true" className="l-legend__swatch">
      <line x1="0" x2="24" y1="4" y2="4" className={`l-plot__line l-plot__line--${variant}`} />
    </svg>
  );
}

export function InsightFigure() {
  const fc = cutoffHz(R2);
  return (
    <figure className="l-insight-figure" aria-labelledby="insight-figure-title">
      <div className="l-instrument__bar">
        <h3 className="l-instrument__title" id="insight-figure-title">
          Two faults, one sound
        </h3>
        <AnalyticalTag />
      </div>
      <ul className="l-legend" aria-label="Legend">
        <li>
          <LegendSwatch variant="primary" />
          R1 doubled <span className="mono muted">2 kΩ, 100 nF</span>
        </li>
        <li>
          <LegendSwatch variant="reference" />
          C1 doubled <span className="mono muted">1 kΩ, 200 nF</span>
        </li>
      </ul>
      <div className="l-insight-figure__plots">
        <div>
          <h4 className="l-plot-title">Output voltage gain</h4>
          <ResponsePlot
            label={`Output gain for R1 doubled and C1 doubled. The two curves are identical: both cut off at ${formatHz(fc)}.`}
            series={[
              { id: "r2", points: gainCurve(R2), variant: "primary" },
              { id: "c2", points: gainCurve(C2), variant: "reference" },
            ]}
            yDomain={[-30, 4]}
            yTicks={[0, -10, -20, -30]}
            formatY={formatGainTick}
            height={184}
            labels={[{ f: 26, y: 0, text: "Identical curves", anchor: "start", dy: 22 }]}
          />
        </div>
        <div>
          <h4 className="l-plot-title">Source current per volt</h4>
          <ResponsePlot
            label="Source current for R1 doubled and C1 doubled. The C1-doubled circuit draws exactly twice the current of the R1-doubled circuit at every frequency."
            series={[
              { id: "r2", points: currentCurve(R2), variant: "primary" },
              { id: "c2", points: currentCurve(C2), variant: "reference" },
            ]}
            yDomain={[0.01, 2]}
            yLog
            yTicks={[0.01, 0.1, 1]}
            formatY={formatMa}
            height={184}
            labels={[
              { f: 6_000, y: sourceCurrentPerVolt(6_000, C2) * 1e3, text: "C1 ×2", anchor: "middle", dy: -10 },
              { f: 6_000, y: sourceCurrentPerVolt(6_000, R2) * 1e3, text: "R1 ×2", anchor: "middle", dy: 20 },
            ]}
          />
        </div>
      </div>
      <details className="disclosure l-values">
        <summary>Show values as a table</summary>
        <div className="l-values__scroll" role="region" aria-label="Analytical values" tabIndex={0}>
          <table className="data-table">
            <caption>Analytical output gain and source current per volt, R1 doubled vs C1 doubled</caption>
            <thead>
              <tr>
                <th scope="col">Frequency</th>
                <th scope="col" className="n">
                  Gain, R1 ×2
                </th>
                <th scope="col" className="n">
                  Gain, C1 ×2
                </th>
                <th scope="col" className="n">
                  Current, R1 ×2
                </th>
                <th scope="col" className="n">
                  Current, C1 ×2
                </th>
              </tr>
            </thead>
            <tbody>
              {TABLE_FREQS.map((f) => (
                <tr key={f}>
                  <th scope="row" className="num">
                    {formatHz(f)}
                  </th>
                  <td className="n">{formatDb(lowpassGainDb(f, R2), 2)}</td>
                  <td className="n">{formatDb(lowpassGainDb(f, C2), 2)}</td>
                  <td className="n">{formatMa(sourceCurrentPerVolt(f, R2) * 1e3)}</td>
                  <td className="n">{formatMa(sourceCurrentPerVolt(f, C2) * 1e3)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
      <figcaption className="l-instrument__caption">
        Calculated from the ideal first-order equations, not measured. In Investigate, the same comparison is made from
        ngspice measurements of the hidden circuit.
      </figcaption>
    </figure>
  );
}
