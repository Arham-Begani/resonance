// SVG schematic for the tested first-order RC templates, drawn from the same Revision data as
// the semantic component list. The topology comes from the graph itself: the series element
// joins Input and Output, the shunt element joins Output and Ground (low-pass: R1 series, C1
// shunt; high-pass: C1 series, R1 shunt). The list is the keyboard interface; clicking a part
// here is a pointer shortcut for the same selection.
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

const VIEW_W = 540;
const VIEW_H = 196;
const Y_TOP = 46;
const Y_BOT = 150;
const Y_MID = (Y_TOP + Y_BOT) / 2;
const X_SRC = 120;
const X_S0 = 212; // series element span
const X_S1 = 308;
const X_OUT = 400;
const X_TERM = 488;
const X_GND = 260;

function between(c: Component, a: string, b: string): boolean {
  return c.nodes.length === 2 && c.nodes.includes(a as never) && c.nodes.includes(b as never);
}

/** The template's two passive parts, identified by their connections (never by template name). */
export function layoutOf(revision: Revision): { series: Component; shunt: Component; source: Component } | null {
  const source = revision.components.find((c) => c.kind === "voltage_source");
  const series = revision.components.find((c) => c.kind !== "voltage_source" && between(c, "in", "out"));
  const shunt = revision.components.find((c) => c.kind !== "voltage_source" && between(c, "out", "gnd"));
  return source && series && shunt ? { source, series, shunt } : null;
}

function zigzagH(x0: number, x1: number, y: number): string {
  const n = 6;
  const step = (x1 - x0) / n;
  let d = `M${x0} ${y}`;
  for (let i = 0; i < n; i++) d += ` L${x0 + step * (i + 0.5)} ${y + (i % 2 === 0 ? -8 : 8)}`;
  return `${d} L${x1} ${y}`;
}

function zigzagV(x: number, y0: number, y1: number): string {
  const n = 6;
  const step = (y1 - y0) / n;
  let d = `M${x} ${y0}`;
  for (let i = 0; i < n; i++) d += ` L${x + (i % 2 === 0 ? 8 : -8)} ${y0 + step * (i + 0.5)}`;
  return `${d} L${x} ${y1}`;
}

export function topologySummary(revision: Revision): string {
  return revision.components.map((c) => c.description).join(". ") + ".";
}

/** Series element on the top wire between X_S0 and X_S1. */
function SeriesPart({ c }: { c: Component }) {
  const mid = (X_S0 + X_S1) / 2;
  if (c.kind === "capacitor") {
    return (
      <>
        <path className="sch-lead" d={`M${X_S0} ${Y_TOP} H${mid - 6} M${mid + 6} ${Y_TOP} H${X_S1}`} />
        <path className="sch-body" d={`M${mid - 6} ${Y_TOP - 14} V${Y_TOP + 14} M${mid + 6} ${Y_TOP - 14} V${Y_TOP + 14}`} />
      </>
    );
  }
  return <path className="sch-body" d={zigzagH(X_S0, X_S1, Y_TOP)} />;
}

/** Shunt element on the vertical wire at X_OUT between the top and bottom rails. */
function ShuntPart({ c }: { c: Component }) {
  if (c.kind === "capacitor") {
    return (
      <>
        <path className="sch-lead" d={`M${X_OUT} ${Y_TOP} V${Y_MID - 6} M${X_OUT} ${Y_MID + 6} V${Y_BOT}`} />
        <path className="sch-body" d={`M${X_OUT - 20} ${Y_MID - 6} H${X_OUT + 20} M${X_OUT - 20} ${Y_MID + 6} H${X_OUT + 20}`} />
      </>
    );
  }
  return (
    <>
      <path className="sch-lead" d={`M${X_OUT} ${Y_TOP} V${Y_MID - 28} M${X_OUT} ${Y_MID + 28} V${Y_BOT}`} />
      <path className="sch-body" d={zigzagV(X_OUT, Y_MID - 28, Y_MID + 28)} />
    </>
  );
}

export function Schematic({ revision, selectedId, probe, onSelect, caption }: SchematicProps) {
  const layout = layoutOf(revision);
  if (!layout) {
    return <p className="muted">No schematic drawing is available for this circuit template.</p>;
  }
  const { series, shunt } = layout;
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
    <figure className="schematic" data-template={revision.template_id}>
      {/* Pointer shortcut only: every action here is also available from the component list. */}
      <svg
        viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
        role="img"
        aria-labelledby={`${titleId} ${descId}`}
        className="schematic__svg"
        onClick={handleClick}
      >
        <title id={titleId}>{`Schematic: ${revision.title}`}</title>
        <desc id={descId}>{topologySummary(revision)}</desc>

        {/* Input net: V1 + to the series element */}
        <g className={`sch-net${probeIn ? " is-probed" : ""}`}>
          <path d={`M${X_SRC} ${Y_MID - 20} V${Y_TOP} H${X_S0}`} />
        </g>
        {/* Output net: series element to the shunt element and the output terminal */}
        <g className={`sch-net${probeOut ? " is-probed" : ""}`}>
          <path d={`M${X_S1} ${Y_TOP} H${X_TERM - 6}`} />
        </g>
        {/* Ground net */}
        <g className="sch-net">
          <path d={`M${X_SRC} ${Y_MID + 20} V${Y_BOT} H${X_TERM - 6}`} />
          <path d={`M${X_GND} ${Y_BOT} V${Y_BOT + 12} M${X_GND - 14} ${Y_BOT + 12} H${X_GND + 14} M${X_GND - 9} ${Y_BOT + 18} H${X_GND + 9} M${X_GND - 4} ${Y_BOT + 24} H${X_GND + 4}`} />
        </g>

        {/* V1 */}
        <g className={partClass("V1")} data-part="V1">
          <rect className="sch-hit" x={X_SRC - 112} y={Y_MID - 28} width={144} height={56} />
          <circle cx={X_SRC} cy={Y_MID} r={20} className="sch-body" />
          <path className="sch-glyph" d={`M${X_SRC - 10} ${Y_MID} q5 -9 10 0 t10 0`} />
          <text x={X_SRC - 4} y={Y_MID - 12} className="sch-sign">+</text>
          <text x={X_SRC - 28} y={Y_MID - 4} className="sch-id" textAnchor="end">V1</text>
          <text x={X_SRC - 28} y={Y_MID + 12} className="sch-value sch-source-value" textAnchor="end">{tones}</text>
        </g>

        {/* Series element (Input → Output) */}
        <g className={partClass(series.id)} data-part={series.id}>
          <rect className="sch-hit" x={X_S0 - 4} y={Y_TOP - 38} width={X_S1 - X_S0 + 8} height={56} />
          <SeriesPart c={series} />
          <text x={(X_S0 + X_S1) / 2} y={Y_TOP - 22} className="sch-id" textAnchor="middle">
            {series.label} <tspan className="sch-value">{series.display_value ?? ""}</tspan>
          </text>
        </g>

        {/* Shunt element (Output → Ground) */}
        <g className={partClass(shunt.id)} data-part={shunt.id}>
          <rect className="sch-hit" x={X_OUT - 26} y={Y_MID - 32} width={132} height={64} />
          <ShuntPart c={shunt} />
          <text x={X_OUT + 30} y={Y_MID - 4} className="sch-id">{shunt.label}</text>
          <text x={X_OUT + 30} y={Y_MID + 12} className="sch-value">{shunt.display_value ?? ""}</text>
        </g>

        {/* Junctions and terminals */}
        <circle className={`sch-node${probeIn ? " is-probed" : ""}`} cx={X_SRC} cy={Y_TOP} r={3.5} />
        <circle className={`sch-node${probeOut ? " is-probed" : ""}`} cx={X_OUT} cy={Y_TOP} r={3.5} />
        <circle className="sch-node" cx={X_OUT} cy={Y_BOT} r={3.5} />
        <circle className={`sch-terminal${probeOut ? " is-probed" : ""}`} cx={X_TERM} cy={Y_TOP} r={5} />
        <circle className="sch-terminal" cx={X_TERM} cy={Y_BOT} r={5} />

        <text x={X_SRC} y={Y_TOP - 12} className="sch-node-label" textAnchor="middle">{nodeLabel("in")}</text>
        <text x={X_TERM} y={Y_TOP - 12} className="sch-node-label" textAnchor="middle">{nodeLabel("out")}</text>
        <text x={X_TERM} y={Y_BOT + 22} className="sch-node-label" textAnchor="middle">{nodeLabel("gnd")}</text>

        {/* Probe markers */}
        {probeIn ? <ProbeTag x={X_SRC + 40} y={Y_TOP} label="Probe" /> : null}
        {probeOut ? <ProbeTag x={X_TERM - 50} y={Y_TOP} label="Probe" /> : null}
        {probeCurrent ? (
          <g className="sch-current">
            <path d={`M${X_SRC + 12} ${Y_MID - 24} V${Y_TOP + 8} M${X_SRC + 7} ${Y_TOP + 14} L${X_SRC + 12} ${Y_TOP + 7} L${X_SRC + 17} ${Y_TOP + 14}`} />
            <text x={X_SRC + 22} y={Y_TOP + 22} className="sch-probe-label">I(V1)</text>
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
      <path d="M0 0 L-6 -10" />
      <rect x={-28} y={-30} width={44} height={18} rx={4} />
      <text x={-6} y={-17} textAnchor="middle" className="sch-probe-label">{label}</text>
    </g>
  );
}
