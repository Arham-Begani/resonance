// Semantic component list, built from the same Revision as the schematic. Arrow keys move
// between rows (roving tabindex); Tab reaches the active row's actions; Escape cancels editing.
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import type { Component, ComponentId, EditableComponentId, NodeId, Revision } from "../../api/types";
import { formatSi } from "../../lib/format";
import type { Probe } from "../../state/workbench";
import { Icon } from "../ui/Icon";
import { ValueEditor } from "./ValueEditor";
import "./component-list.css";

export interface ComponentListProps {
  revision: Revision;
  selectedId: ComponentId | null;
  editingId: EditableComponentId | null;
  probe: Probe;
  /** False while a case freezes the intended design. */
  editable: boolean;
  lockedReason?: string;
  applying: boolean;
  applyError: string | null;
  onSelect: (id: ComponentId) => void;
  onEdit: (id: EditableComponentId | null) => void;
  onApply: (id: EditableComponentId, text: string) => void;
  onProbe: (probe: Probe) => void;
}

function nodeLabel(revision: Revision, id: NodeId): string {
  return revision.nodes.find((n) => n.id === id)?.label ?? id;
}

function endpointsText(revision: Revision, c: Component): string {
  const a = c.nodes[0] ?? "in";
  const b = c.nodes[1] ?? "gnd";
  return c.kind === "voltage_source"
    ? `+ ${nodeLabel(revision, a)}, − ${nodeLabel(revision, b)}`
    : `${nodeLabel(revision, a)} → ${nodeLabel(revision, b)}`;
}

function componentProbe(c: Component): { probe: Probe; label: string } | null {
  if (c.kind === "voltage_source") return { probe: { kind: "current", component: "V1" }, label: "Probe current" };
  // The shunt part (Output to Ground) carries the output voltage in both RC families.
  if (c.nodes.includes("out") && c.nodes.includes("gnd")) return { probe: { kind: "node", node: "out" }, label: "Probe voltage" };
  return null;
}

function sameProbe(a: Probe, b: Probe): boolean {
  if (!a || !b) return a === b;
  if (a.kind === "node" && b.kind === "node") return a.node === b.node;
  return a.kind === b.kind;
}

export function ComponentList(props: ComponentListProps) {
  const { revision, selectedId, editingId, probe, editable, lockedReason, applying, applyError, onSelect, onEdit, onApply, onProbe } = props;
  const components = revision.components;
  const [activeIndex, setActiveIndex] = useState(() => Math.max(0, components.findIndex((c) => c.id === selectedId)));
  const rowRefs = useRef<Map<ComponentId, HTMLButtonElement>>(new Map());
  const focusAfterEdit = useRef<ComponentId | null>(null);

  // Keep the roving index in step with selection made elsewhere (e.g. clicking the schematic).
  useEffect(() => {
    const index = components.findIndex((c) => c.id === selectedId);
    if (index >= 0) setActiveIndex(index);
  }, [selectedId, components]);

  // Preserve focus after an edit closes (applied or cancelled): return to the edited row.
  useEffect(() => {
    if (editingId === null && focusAfterEdit.current) {
      rowRefs.current.get(focusAfterEdit.current)?.focus();
      focusAfterEdit.current = null;
    }
  }, [editingId, revision.id]);

  const moveTo = (index: number) => {
    const clamped = Math.max(0, Math.min(components.length - 1, index));
    setActiveIndex(clamped);
    const c = components[clamped];
    if (c) rowRefs.current.get(c.id)?.focus();
  };

  const onRowKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        moveTo(index + 1);
        break;
      case "ArrowUp":
        event.preventDefault();
        moveTo(index - 1);
        break;
      case "Home":
        event.preventDefault();
        moveTo(0);
        break;
      case "End":
        event.preventDefault();
        moveTo(components.length - 1);
        break;
      default:
    }
  };

  const startEdit = (c: Component) => {
    focusAfterEdit.current = c.id;
    onEdit(c.id as EditableComponentId);
  };

  const selected = components.find((c) => c.id === selectedId) ?? null;
  const probeNodes = revision.nodes.filter((n) => n.id !== "gnd");

  return (
    <div className="component-list">
      {/* The rows are self-evidently the parts; the heading stays for screen-reader navigation. */}
      <h3 className="visually-hidden" id="components-heading">
        Components
      </h3>
      <ul className="component-list__items" aria-labelledby="components-heading">
        {components.map((c, index) => {
          const isActive = index === activeIndex;
          const isSelected = c.id === selectedId;
          const isEditing = editingId === c.id;
          const probeAction = componentProbe(c);
          const tab = isActive ? 0 : -1;
          return (
            <li key={c.id} className={`component-row${isSelected ? " is-selected" : ""}`}>
              <div className="component-row__main">
                <button
                  ref={(el) => {
                    if (el) rowRefs.current.set(c.id, el);
                    else rowRefs.current.delete(c.id);
                  }}
                  type="button"
                  className="component-row__select"
                  tabIndex={tab}
                  aria-pressed={isSelected}
                  onClick={() => {
                    setActiveIndex(index);
                    onSelect(c.id);
                  }}
                  onFocus={() => setActiveIndex(index)}
                  onKeyDown={(e) => onRowKeyDown(e, index)}
                >
                  <RowText component={c} />
                </button>
                <div className="component-row__actions">
                  {c.editable ? (
                    <button
                      type="button"
                      className="btn btn--sm btn--ghost"
                      tabIndex={tab}
                      disabled={!editable || isEditing}
                      aria-label={`Edit ${c.label} value`}
                      title={!editable ? lockedReason : undefined}
                      onClick={() => startEdit(c)}
                    >
                      <Icon name={editable ? "edit" : "lock"} size={14} />
                      Edit
                    </button>
                  ) : null}
                  {probeAction ? (
                    <button
                      type="button"
                      className="btn btn--sm btn--ghost"
                      tabIndex={tab}
                      aria-pressed={sameProbe(probe, probeAction.probe)}
                      aria-label={`${probeAction.label} at ${c.label}`}
                      onClick={() => onProbe(probeAction.probe)}
                    >
                      <Icon name="probe" size={14} />
                      Probe
                    </button>
                  ) : null}
                </div>
              </div>
              {isEditing ? (
                <ValueEditor
                  component={c}
                  templateId={revision.template_id}
                  applying={applying}
                  serverError={applyError}
                  onApply={onApply}
                  onCancel={() => onEdit(null)}
                />
              ) : null}
            </li>
          );
        })}
      </ul>

      {!editable && lockedReason ? (
        <p className="component-list__locked">
          <Icon name="lock" size={14} />
          {lockedReason}
        </p>
      ) : null}

      <div className="probe-row">
      <h3 className="eyebrow probe-row__label" id="nodes-heading">
        Probe
      </h3>
      <ul className="node-list" aria-labelledby="nodes-heading">
        {probeNodes.map((n) => {
          const p: Probe = { kind: "node", node: n.id };
          const active = sameProbe(probe, p);
          return (
            <li key={n.id}>
              <button
                type="button"
                className={`node-chip${active ? " is-active" : ""}`}
                aria-pressed={active}
                onClick={() => onProbe(p)}
              >
                <Icon name="probe" size={14} />
                {n.label} voltage
              </button>
            </li>
          );
        })}
        <li>
          <button
            type="button"
            className={`node-chip${probe?.kind === "current" ? " is-active" : ""}`}
            aria-pressed={probe?.kind === "current"}
            onClick={() => onProbe({ kind: "current", component: "V1" })}
          >
            <Icon name="probe" size={14} />
            Source current
          </button>
        </li>
      </ul>
      </div>

      {selected ? <Inspector revision={revision} component={selected} /> : null}
    </div>
  );
}

/**
 * The row's visible text IS its accessible name (WCAG 2.5.3 Label in Name): the trusted
 * description "R1 — resistor — 1 kΩ — connected between Input and Output" is shown in parts,
 * with the dashes kept for screen readers only.
 */
export function descriptionParts(c: Component): { id: string; kind: string; value: string; ends: string } | null {
  const parts = c.description.split(" — ");
  if (parts.length < 4) return null;
  return { id: parts[0]!, kind: parts[1]!, value: parts.slice(2, -1).join(" — "), ends: parts[parts.length - 1]! };
}

function RowText({ component }: { component: Component }) {
  const parts = descriptionParts(component);
  if (!parts) return <span className="component-row__ends">{component.description}</span>;
  const sep = <span className="visually-hidden"> — </span>;
  return (
    <>
      <span className="component-row__id">{parts.id}</span>
      {sep}
      <span className="component-row__kind">{parts.kind}</span>
      {sep}
      <span className="component-row__value num">{parts.value}</span>
      {sep}
      <span className="component-row__ends">{parts.ends}</span>
    </>
  );
}

function Inspector({ revision, component }: { revision: Revision; component: Component }) {
  return (
    <section className="inspector" aria-label={`${component.label} details`}>
      <h4 className="inspector__title">
        {component.label} <span className="muted">{component.name}</span>
      </h4>
      <dl className="inspector__grid">
        {component.value_si != null ? (
          <>
            <dt>Value</dt>
            <dd className="num">
              {component.display_value} ({component.value_si.toPrecision(4)} {component.unit === "ohm" ? "Ω" : "F"})
            </dd>
          </>
        ) : null}
        {component.bounds ? (
          <>
            <dt>Allowed</dt>
            <dd className="num">
              {formatSi(component.bounds.min_si, component.bounds.unit)} – {formatSi(component.bounds.max_si, component.bounds.unit)}
            </dd>
          </>
        ) : null}
        <dt>Connected</dt>
        <dd>{endpointsText(revision, component)}</dd>
        {component.kind === "voltage_source" ? (
          <>
            <dt>Signal</dt>
            <dd>
              {revision.source.tones.map((t) => (
                <span key={t.frequency_hz} className="inspector__tone num">
                  {t.label}: {t.amplitude_v} V peak at {t.frequency_hz} Hz
                </span>
              ))}
            </dd>
          </>
        ) : null}
      </dl>
    </section>
  );
}
