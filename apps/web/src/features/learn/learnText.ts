// Display words for Learn results. Every number shown comes from the attempt payload.
import type { Expectation, LearnAttempt } from "../../api/types";

export const EXPECTATION_LABEL: Record<Expectation, string> = {
  louder: "Louder",
  quieter: "Quieter",
  about_the_same: "About the same",
};

export const EXPECTATIONS: Expectation[] = ["louder", "quieter", "about_the_same"];

export const LEARN_RESULT_HEADING = "learn-result-heading";

function multiplierText(m: number): string {
  if (m === 2) return "doubled";
  if (m === 0.5) return "halved";
  if (m === 4) return "×4";
  if (m === 0.25) return "÷4";
  return `×${m}`;
}

/** "C1 doubled (100 nF → 200 nF)" from the attempt's change and its measured comparison. */
export function changeText(attempt: Pick<LearnAttempt, "change" | "comparison">): string {
  const c = attempt.comparison.changes.find((x) => x.component_id === attempt.change.component_id);
  const values = c ? `${c.from_display} → ${c.to_display}` : "";
  return `${attempt.change.component_id} ${multiplierText(attempt.change.multiplier)}${values ? ` (${values})` : ""}`;
}
