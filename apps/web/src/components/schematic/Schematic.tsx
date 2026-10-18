// SVG schematic for the tested RC low-pass template, drawn from the same Revision data as the
// semantic component list. The list is the keyboard interface; clicking a part here is a pointer
// shortcut for the same selection.
import type { MouseEvent } from "react";
import type { Component, ComponentId, Revision } from "../../api/types";
import { formatHz } from "../../lib/format";
import type { Probe } from "../../state/workbench";
import "./schematic.css";

interface SchematicProps {
  revision: Revision;
  selectedId: ComponentId | null;
  probe: Probe;
  onSelect: (id: ComponentId) => void;
  /** Short status shown under the drawing, e.g. "Intended design (frozen)". */
  caption?: string;
}

const Y_TOP = 56;
const Y_BOT = 192;
const X_SRC = 120;
const X_R_START = 216;
const X_R_END = 304;
const X_OUT = 400;
const X_TERM = 488;
const X_GND = 260;

function zigzag(x0: number, x1: number, y: number): string {
  const n = 6;
  const step = (x1 - x0) / n;
  let d = `M${x0} ${y}`;
  for (let i = 0; i < n; i++) {
    const x = x0 + step * (i + 0.5);
    d += ` L${x} ${y + (i % 2 === 0 ? -9 : 9)}`;
  }
  return `${d} L${x1} ${y}`;
}

function findComponent(revision: Revision, id: ComponentId): Component | undefined {
  return revision.components.find((c) => c.id === id);
}

export function topologySummary(revision: Revision): string {
  return revision.components.map((c) => c.description).join(". ") + ".";
}

export function Schematic({ revision, selectedId, probe, onSelect, caption }: SchematicProps) {
  if (revision.template_id !== "rc_lowpass") {
    return <p className="muted">No schematic drawing is available for this circuit template.</p>;
  }
  const r1 = findComponent(revision, "R1");
  const c1 = findComponent(revision, "C1");
  const tones = revision.source.tones.map((t) => formatHz(t.frequency_hz)).join(" + ");
  const probeIn = probe?.kind === "node" && probe.node === "in";
  const probeOut = probe?.kind === "node" && probe.node === "out";
  const probeCurrent = probe?.kind === "current";
  const titleId = `schematic-title-${revision.id}`;
  const descId = `schematic-desc-${revision.id}`;

  const handleClick = (event: MouseEvent<SVGSVGElement>) => {
    const target = (event.target as Element).closest("[data-part]");
    const part = target?.getAttribute("data-part") as ComponentId | null;
    if (part) onSelect(part);
  };

  const partClass = (id: ComponentId) => `sch-part${selectedId === id ? " is-selected" : ""}`;
  const nodeLabel = (id: "in" | "out" | "gnd") => revision.nodes.find((n) => n.id === id)?.label ?? id;

  return (
    <figure className="schematic">
      {/* Pointer shortcut only: every action here is also available from the component list. */}
      <svg
        viewBox="0 0 540 236"
        role="img"
        aria-labelledby={`${titleId} ${descId}`}
        className="schematic__svg"
        onClick={handleClick}
      >
        <title id={titleId}>{`Schematic: ${revision.title}`}</title>
        <desc id={descId}>{topologySummary(revision)}</desc>

        {/* Input net: V1 + to R1 */}
        <g className={`sch-net${probeIn ? " is-probed" : ""}`}>
          <path d={`M${X_SRC} 102 V${Y_TOP} H${X_R_START}`} />
        </g>
        {/* Output net: R1 to C1 and the output terminal */}
        <g className={`sch-net${probeOut ? " is-probed" : ""}`}>
          <path d={`M${X_R_END} ${Y_TOP} H${X_TERM - 6} M${X_OUT} ${Y_TOP} V112`} />
        </g>
        {/* Ground net */}
        <g className="sch-net">
          <path d={`M${X_SRC} 146 V${Y_BOT} H${X_TERM - 6} M${X_OUT} 124 V${Y_BOT}`} />
          <path d={`M${X_GND} ${Y_BOT} V206 M${X_GND - 14} 206 H${X_GND + 14} M${X_GND - 9} 212 H${X_GND + 9} M${X_GND - 4} 218 H${X_GND + 4}`} />
        </g>

        {/* V1 */}
        <g className={partClass("V1")} data-part="V1">
          <rect className="sch-hit" x={X_SRC - 60} y={96} width={92} height={56} />
          <circle cx={X_SRC} cy={124} r={22} className="sch-body" />
          <path className="sch-glyph" d={`M${X_SRC - 11} 124 q5.5 -10 11 0 t11 0`} />
          <text x={X_SRC - 4} y={110} className="sch-sign">+</text>
          <text x={X_SRC - 30} y={120} className="sch-id" textAnchor="end">V1</text>
          <text x={X_SRC - 30} y={136} className="sch-value" textAnchor="end">{tones}</text>
        </g>

        {/* R1 */}
        <g className={partClass("R1")} data-part="R1">
          <rect className="sch-hit" x={X_R_START - 4} y={Y_TOP - 40} width={X_R_END - X_R_START + 8} height={60} />
          <path className="sch-body" d={zigzag(X_R_START, X_R_END, Y_TOP)} />
          <text x={(X_R_START + X_R_END) / 2} y={Y_TOP - 22} className="sch-id" textAnchor="middle">
            R1 <tspan className="sch-value">{r1?.display_value ?? ""}</tspan>
          </text>
        </g>

        {/* C1 */}
        <g className={partClass("C1")} data-part="C1">
          <rect className="sch-hit" x={X_OUT - 28} y={100} width={130} height={36} />
          <path className="sch-body" d={`M${X_OUT - 20} 112 H${X_OUT + 20} M${X_OUT - 20} 124 H${X_OUT + 20}`} />
          <text x={X_OUT + 30} y={116} className="sch-id">C1</text>
          <text x={X_OUT + 30} y={132} className="sch-value">{c1?.display_value ?? ""}</text>
        </g>

        {/* Junctions and terminals */}
        <circle className={`sch-node${probeIn ? " is-probed" : ""}`} cx={X_SRC} cy={Y_TOP} r={3.5} />
        <circle className={`sch-node${probeOut ? " is-probed" : ""}`} cx={X_OUT} cy={Y_TOP} r={3.5} />
        <circle className="sch-node" cx={X_OUT} cy={Y_BOT} r={3.5} />
        <circle className={`sch-terminal${probeOut ? " is-probed" : ""}`} cx={X_TERM} cy={Y_TOP} r={5} />
        <circle className="sch-terminal" cx={X_TERM} cy={Y_BOT} r={5} />

        <text x={X_SRC} y={Y_TOP - 14} className="sch-node-label" textAnchor="middle">{nodeLabel("in")}</text>
        <text x={X_TERM} y={Y_TOP - 14} className="sch-node-label" textAnchor="middle">{nodeLabel("out")}</text>
        <text x={X_TERM} y={Y_BOT + 24} className="sch-node-label" textAnchor="middle">{nodeLabel("gnd")}</text>

        {/* Probe markers */}
        {probeIn ? <ProbeTag x={X_SRC + 34} y={Y_TOP} label="Probe" /> : null}
        {probeOut ? <ProbeTag x={X_TERM - 56} y={Y_TOP} label="Probe" /> : null}
        {probeCurrent ? (
          <g className="sch-current">
            <path d={`M${X_SRC + 12} 92 V70 M${X_SRC + 7} 76 L${X_SRC + 12} 69 L${X_SRC + 17} 76`} />
            <text x={X_SRC + 22} y={84} className="sch-probe-label">I(V1)</text>
          </g>
        ) : null}
      </svg>
      {caption ? <figcaption className="schematic__caption">{caption}</figcaption> : null}
    </figure>
  );
}

function ProbeTag({ x, y, label }: { x: number; y: number; label: string }) {
  return (
    <g className="sch-probe" transform={`translate(${x} ${y})`}>
      <path d="M0 0 L-6 -10 M-6 -10 H-6" />
      <rect x={-28} y={-30} width={44} height={18} rx={4} />
      <text x={-6} y={-17} textAnchor="middle" className="sch-probe-label">{label}</text>
    </g>
  );
}
