// The shared circuit view: schematic + semantic component list, both built from one Revision.
// Explore edits the current session revision; Investigate shows the frozen intended design.
import { Schematic } from "../components/schematic/Schematic";
import { ComponentList } from "../components/components-list/ComponentList";
import { Callout } from "../components/ui/primitives";
import { currentRevision, revisionOrdinal, useWorkbench } from "../state/workbench";
import { useCase } from "../features/investigate/caseStore";

export const FROZEN_REASON =
  "Editing is locked during a mystery: the intended design stays frozen so every measurement compares against the same reference.";

export function CircuitArea() {
  const wb = useWorkbench();
  const { state } = wb;
  const cs = useCase();
  const explore = state.mode === "explore";
  const revision = explore ? currentRevision(state) : (cs.current?.intended_revision ?? null);

  if (!revision) {
    if (explore && state.status === "error") {
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
    if (!explore) {
      return (
        <section className="circuit" aria-labelledby="circuit-heading">
          <h2 id="circuit-heading" className="section-title">
            Circuit
          </h2>
          <p className="muted">The intended design appears here when a mystery starts.</p>
        </section>
      );
    }
    return (
      <section className="circuit" aria-labelledby="circuit-heading" aria-busy="true">
        <h2 id="circuit-heading" className="section-title">
          Circuit
        </h2>
        <div className="circuit__grid">
          <div className="skeleton circuit__skeleton" />
          <div className="skeleton circuit__skeleton" />
        </div>
      </section>
    );
  }

  const ordinal = revisionOrdinal(state, revision.id);
  const applyError = state.error?.op === "revision" ? state.error.error.message : null;
  const caption = explore
    ? `Revision ${ordinal ?? ""}${revision.changes?.length ? ` · changed ${revision.changes.map((c) => `${c.component_id} ${c.from_display} → ${c.to_display}`).join(", ")}` : " · original values"}`
    : "Intended design (frozen) · nominal values";

  return (
    <section className="circuit" aria-labelledby="circuit-heading">
      <div className="circuit__header">
        <h2 id="circuit-heading" className="section-title">
          {explore ? "Circuit" : "Intended design"}
        </h2>
        <p className="circuit__prediction muted">
          Prediction: cutoff ≈ {Math.round(revision.prediction.cutoff_hz).toLocaleString()} Hz (analytical, 1/(2πRC))
        </p>
      </div>
      {!explore ? (
        <Callout tone="accent" icon="lock" title="Actual behaviour may differ">
          This is how the circuit is supposed to be built. The mystery circuit&apos;s actual values are hidden until the case is closed.
        </Callout>
      ) : null}
      <div className="circuit__grid">
        <Schematic revision={revision} selectedId={state.selectedComponentId} probe={state.probe} onSelect={(id) => wb.select(id)} caption={caption} />
        <ComponentList
          revision={revision}
          selectedId={state.selectedComponentId}
          editingId={explore ? state.editingComponentId : null}
          probe={state.probe}
          editable={explore && !revision.frozen}
          lockedReason={explore ? "This revision is frozen." : FROZEN_REASON}
          applying={state.pending === "revision"}
          applyError={applyError}
          onSelect={(id) => wb.select(id)}
          onEdit={(id) => wb.edit(id)}
          onApply={(id, text) => void wb.applyValues([{ component_id: id, value: text }])}
          onProbe={(p) => wb.setProbe(p)}
        />
      </div>
    </section>
  );
}
