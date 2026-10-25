// Labels for result IDs cited by the workbench assistant (Explore and Learn): the same numbering
// learners see elsewhere ("Experiment 3", "Revision 2", "Attempt 1"). Raw IDs are never shown.
import { experimentOrdinal, revisionOrdinal, useWorkbench } from "../../state/workbench";
import { useLearn } from "../learn/learnStore";
import type { CitationResolver } from "./AgentRunLog";

export function useWorkbenchResolver(): CitationResolver {
  const wb = useWorkbench();
  const learn = useLearn();
  return {
    label: (id) => {
      if (id.startsWith("exp_")) {
        const n = experimentOrdinal(wb.state, id);
        return n ? `Experiment ${n}` : null;
      }
      if (id.startsWith("rev_")) {
        const n = revisionOrdinal(wb.state, id);
        return n ? `Revision ${n}` : null;
      }
      if (id.startsWith("learn_")) {
        const index = learn.attempts.findIndex((a) => a.id === id);
        return index >= 0 ? `Attempt ${index + 1}` : null;
      }
      return null;
    },
    // Only measurements open in the instruments; other labels are plain text.
    canSelect: (id) => id.startsWith("exp_"),
    onSelect: (id) => {
      if (id.startsWith("exp_")) void wb.showExperiment(id);
    },
  };
}
