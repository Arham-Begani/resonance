// The shared circuit view: schematic + semantic component list, both built from one Revision.
// Explore and Learn edit the current session revision; Investigate shows the frozen intended design.
import { Schematic } from "../components/schematic/Schematic";
import { ComponentList, ProbeBar } from "../components/components-list/ComponentList";
import { Badge, ErrorCallout } from "../components/ui/primitives";
import { Icon } from "../components/ui/Icon";
import { FamilySwitch } from "../features/explore/FamilySwitch";
import { formatHz } from "../lib/format";
import { FAMILY_LABEL } from "../lib/family";
import { currentRevision, revisionOrdinal, useWorkbench } from "../state/workbench";
import { useCase } from "../features/investigate/caseStore";

export const FROZEN_REASON =
  "Editing is locked during a mystery: the intended design stays frozen so every measurement compares against the same reference.";

function CircuitSkeleton() {
  return (
    <div className="circuit__card" aria-hidden="true">
      <div className="skeleton circuit__skeleton" />
      <div className="circuit__skeleton-list">
        <div className="skeleton skeleton--row" />
        <div className="skeleton skeleton--row" />
        <div className="skeleton skeleton--row" />
      </div>
    </div>
  );
}

export function CircuitArea() {
  const wb = useWorkbench();
  const { state } = wb;
  const cs = useCase();
  const investigate = state.mode === "investigate";
  const revision = !investigate ? currentRevision(state) : (cs.current?.intended_revision ?? null);

  if (!revision) {
    if (!investigate && state.status === "error") {
      return (
        <section className="circuit" aria-labelledby="circuit-heading">
          <h2 id="circuit-heading" className="section-title">
            Circuit
          </h2>
          <div className="empty circuit__empty">
            <p className="empty__title">The circuit could not be loaded</p>
            <p>Reload the workbench when the server is ready. Nothing you measured is lost.</p>
          </div>
        </section>
      );
    }
    if (investigate) {
      return (
        <section className="circuit" aria-labelledby="circuit-heading">
          <h2 id="circuit-heading" className="section-title">
            Intended design
          </h2>
          <div className="empty circuit__empty">
            <p className="empty__title">No mystery open</p>
            <p>The intended design appears here when a mystery starts. Its actual values stay hidden until the case is closed.</p>
          </div>
        </section>
      );
    }
    return (
      <section className="circuit" aria-labelledby="circuit-heading" aria-busy="true">
        <h2 id="circuit-heading" className="section-title">
          Circuit
        </h2>
        <span className="visually-hidden">Loading the circuit…</span>
        <CircuitSkeleton />
      </section>
    );
  }

  const ordinal = revisionOrdinal(state, revision.id);
  const applyError = state.error?.op === "revision" ? state.error.error.message : null;
  const templateError = state.error?.op === "template" ? state.error.error : null;
  const caption = !investigate
    ? `Revision ${ordinal ?? ""}${revision.changes?.length ? ` · changed ${revision.changes.map((c) => `${c.component_id} ${c.from_display} → ${c.to_display}`).join(", ")}` : " · original values"}`
    : "Intended design (frozen) · nominal values";

  return (
    <section className="circuit" aria-labelledby="circuit-heading">
      <div className="circuit__header">
        <h2 id="circuit-heading" className="section-title">
          {investigate ? "Intended design" : "Circuit"}
        </h2>
        {investigate ? <Badge icon={null}>{FAMILY_LABEL[revision.template_id]}</Badge> : <FamilySwitch />}
        <p className="circuit__prediction">
          Predicted cutoff ≈ <span className="num">{formatHz(revision.prediction.cutoff_hz)}</span>{" "}
          <span className="circuit__basis">analytical, 1/(2πRC)</span>
        </p>
      </div>
      {templateError ? <ErrorCallout error={templateError} /> : null}
      <div className="circuit__card">
        <div className="circuit__drawing">
          <Schematic revision={revision} selectedId={state.selectedComponentId} probe={state.probe} onSelect={(id) => wb.select(id)} caption={caption} />
        </div>
        <div className="circuit__parts">
          {investigate ? (
            // One quiet note instead of a banner: what this drawing is, and why editing is locked.
            <p className="circuit__frozen">
              <Icon name="lock" size={14} />
              <span>
                <strong>Actual behaviour may differ.</strong> This is the intended design; the mystery circuit&apos;s actual values stay hidden
                until the case is closed. Editing is locked so every measurement compares against this reference.
              </span>
            </p>
          ) : null}
          <ComponentList
            revision={revision}
            selectedId={state.selectedComponentId}
            editingId={!investigate ? state.editingComponentId : null}
            probe={state.probe}
            editable={!investigate && !revision.frozen}
            lockedReason={!investigate ? "This revision is frozen." : FROZEN_REASON}
            showLockedNote={!investigate}
            applying={state.pending === "revision"}
            applyError={applyError}
            onSelect={(id) => wb.select(id)}
            onEdit={(id) => wb.edit(id)}
            onApply={(id, text) => void wb.applyValues([{ component_id: id, value: text }])}
            onProbe={(p) => wb.setProbe(p)}
          />
          <ProbeBar revision={revision} probe={state.probe} onProbe={(p) => wb.setProbe(p)} />
        </div>
      </div>
    </section>
  );
}
