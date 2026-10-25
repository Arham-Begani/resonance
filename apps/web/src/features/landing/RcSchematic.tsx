// Static drawing of the lab's default RC low-pass (R1 in series, C1 to ground), styled like the
// workbench schematic. The output net is drawn in the accent color because the plot beside it is
// the output response; the text alternative states the topology in words.
function zigzag(x0: number, x1: number, y: number, amp = 7, n = 6): string {
  const step = (x1 - x0) / n;
  let d = `M${x0} ${y}`;
  for (let i = 0; i < n; i++) d += ` L${x0 + step * (i + 0.5)} ${y + (i % 2 === 0 ? -amp : amp)}`;
  return `${d} L${x1} ${y}`;
}

const TOP = 34;
const BOT = 122;
const SRC = { x: 56, y: 78, r: 18 };
const R0 = 124;
const R1 = 204;
const CX = 272;
const TERM = 364;
const GND = 164;

export function RcSchematic({ resistor, capacitor }: { resistor: string; capacitor: string }) {
  return (
    <svg
      className="l-sch"
      viewBox="0 0 400 152"
      role="img"
      aria-label={`Schematic of the RC low-pass: source V1 drives R1 (${resistor}) from Input to Output; C1 (${capacitor}) connects Output to Ground. The output is highlighted.`}
    >
      <g className="l-sch__wire" aria-hidden="true">
        <path d={`M${SRC.x} ${SRC.y - SRC.r} V${TOP} H${R0}`} />
        <path d={`M${SRC.x} ${SRC.y + SRC.r} V${BOT} H${CX}`} />
        <path d={`M${GND} ${BOT} V${BOT + 10} M${GND - 12} ${BOT + 10} H${GND + 12} M${GND - 7} ${BOT + 15} H${GND + 7} M${GND - 3} ${BOT + 20} H${GND + 3}`} />
        <path d={`M${CX} ${78 + 6} V${BOT}`} />
      </g>
      <g className="l-sch__out" aria-hidden="true">
        <path d={`M${R1} ${TOP} H${TERM - 5} M${CX} ${TOP} V${78 - 6}`} />
        <circle cx={CX} cy={TOP} r={3} className="l-sch__node" />
        <circle cx={TERM} cy={TOP} r={5} className="l-sch__term" />
      </g>
      <g className="l-sch__body" aria-hidden="true">
        <circle cx={SRC.x} cy={SRC.y} r={SRC.r} />
        <path d={`M${SRC.x - 9} ${SRC.y} q4.5 -8 9 0 t9 0`} className="l-sch__glyph" />
        <path d={zigzag(R0, R1, TOP)} />
        <path d={`M${CX - 18} ${78 - 6} H${CX + 18} M${CX - 18} ${78 + 6} H${CX + 18}`} />
      </g>
      <g className="l-sch__text" aria-hidden="true">
        <text x={SRC.x - SRC.r - 8} y={SRC.y} dy="0.35em" textAnchor="end">
          V1
        </text>
        <text x={(R0 + R1) / 2} y={TOP - 16} textAnchor="middle">
          R1 <tspan className="l-sch__value">{resistor}</tspan>
        </text>
        <text x={CX + 26} y={78} dy="0.35em">
          C1 <tspan className="l-sch__value">{capacitor}</tspan>
        </text>
        <text x={TERM} y={TOP - 16} textAnchor="middle" className="l-sch__outlabel">
          Output
        </text>
        <text x={GND + 18} y={BOT + 18}>
          Ground
        </text>
      </g>
    </svg>
  );
}
