// Guide-me prediction step (UI only, skippable): before a measurement the learner ticks the
// explanations they expect it to rule out; afterwards each expectation is compared with the
// RECORDED evidence status (Ruled out / Still possible). It is never compared with the hidden
// answer, which the browser does not have.
import type { HypothesisAssessment, HypothesisId } from "../../api/types";

export interface GuidePrediction {
  /** Explanations still possible when the prediction was made. */
  candidates: HypothesisId[];
  /** The subset the learner expected the measurement to rule out. */
  expected: HypothesisId[];
}

export interface PredictionCheck {
  hypothesis: HypothesisId;
  expectedRuledOut: boolean;
  ruledOut: boolean;
  /** Supported = consistent and the only explanation left (still possible). */
  onlyOneLeft: boolean;
  agrees: boolean;
}

export interface PredictionResult extends GuidePrediction {
  checks: PredictionCheck[];
  matched: number;
}

export function comparePrediction(prediction: GuidePrediction, assessments: HypothesisAssessment[]): PredictionResult {
  const statusOf = new Map(assessments.map((a) => [a.hypothesis_id, a.status]));
  const checks = prediction.candidates.map((h) => {
    const status = statusOf.get(h) ?? "unresolved";
    const ruledOut = status === "contradicted";
    const expectedRuledOut = prediction.expected.includes(h);
    return { hypothesis: h, expectedRuledOut, ruledOut, onlyOneLeft: status === "supported", agrees: expectedRuledOut === ruledOut };
  });
  return { ...prediction, checks, matched: checks.filter((c) => c.agrees).length };
}
