// Circuit family switch (Low-pass / High-pass) for Explore and Learn. Switching returns to the
// session's latest revision of that family, or creates the family's root revision and sets its
// opening goal. Undo and history keep working across families.
import { useId } from "react";
import type { TemplateId } from "../../api/types";
import { FAMILIES, FAMILY_LABEL } from "../../lib/family";
import { currentRevision, useWorkbench } from "../../state/workbench";

export function FamilySwitch() {
  const id = useId();
  const wb = useWorkbench();
  const { state } = wb;
  const revision = currentRevision(state);
  if (!revision) return null;
  const busy = state.pending !== null;
  const served = new Set(state.templates.map((t) => t.id));
  const noteId = `${id}-note`;
  const missing = state.templates.length > 0 ? FAMILIES.filter((f) => !served.has(f)) : [];

  const choose = (templateId: TemplateId) => {
    if (templateId !== revision.template_id) void wb.switchTemplate(templateId);
  };

  return (
    <div className="family-switch">
      <div className="segmented" role="radiogroup" aria-label="Circuit family" aria-describedby={missing.length ? noteId : undefined} aria-busy={state.pending === "template" || undefined}>
        {FAMILIES.map((f) => {
          const unavailable = state.templates.length > 0 && !served.has(f);
          return (
            <label key={f}>
              <input
                type="radio"
                name={`${id}-family`}
                checked={revision.template_id === f}
                disabled={unavailable || (busy && revision.template_id !== f)}
                onChange={() => choose(f)}
              />
              {FAMILY_LABEL[f]}
            </label>
          );
        })}
      </div>
      {missing.length ? (
        <p id={noteId} className="family-switch__note">
          {missing.map((f) => FAMILY_LABEL[f]).join(", ")} is not available on this server yet.
        </p>
      ) : null}
    </div>
  );
}
