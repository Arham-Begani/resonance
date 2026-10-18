// Deliberate focus moves after user-initiated actions (never for passive updates). The target
// may not exist yet when the action resolves (React renders it next), so retry for a few frames.
export function focusWhenReady(id: string, options: { frames?: number; fallbackId?: string } = {}): void {
  const { frames = 30, fallbackId } = options;
  const tryFocus = (target: string) => {
    const el = document.getElementById(target);
    if (!el || el.hasAttribute("disabled")) return false;
    el.focus();
    return document.activeElement === el;
  };
  const attempt = (left: number) => {
    if (tryFocus(id)) return;
    if (left > 0) requestAnimationFrame(() => attempt(left - 1));
    else if (fallbackId) tryFocus(fallbackId);
  };
  requestAnimationFrame(() => attempt(frames));
}

/** Stable IDs for focus targets that several components refer to. */
export const FOCUS = {
  runExperiment: "run-experiment-button",
  comparisonTitle: "comparison-title",
  symptomHeading: "symptom-heading",
  measureHeading: "instr-heading",
  diagnosisHeading: "dx-heading",
  repairHeading: "repair-heading",
  repairVerify: "repair-verify-button",
  editGoal: "edit-goal-button",
  observation: (id: string) => `obs-title-${id}`,
} as const;
