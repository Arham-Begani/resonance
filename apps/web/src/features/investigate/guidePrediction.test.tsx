import { render, screen, within } from "@testing-library/react";
import type { HypothesisAssessment } from "../../api/types";
import { makeCase } from "../../test/fixtures";
import { comparePrediction } from "./guidePrediction";
import { PredictionCheckView } from "./InvestigatePanel";

const after: HypothesisAssessment[] = [
  { hypothesis_id: "no_fault", status: "contradicted", checks: [] },
  { hypothesis_id: "r1_increased", status: "contradicted", checks: [] },
  { hypothesis_id: "c1_increased", status: "supported", checks: [] },
  { hypothesis_id: "c1_disconnected", status: "unresolved", checks: [] },
];

describe("Guide-me prediction (compared with recorded evidence only)", () => {
  it("scores each still-possible explanation against its evidence status after the measurement", () => {
    const result = comparePrediction(
      { candidates: ["r1_increased", "c1_increased", "c1_disconnected"], expected: ["r1_increased", "c1_disconnected"] },
      after,
    );
    expect(result.checks).toEqual([
      { hypothesis: "r1_increased", expectedRuledOut: true, ruledOut: true, onlyOneLeft: false, agrees: true },
      { hypothesis: "c1_increased", expectedRuledOut: false, ruledOut: false, onlyOneLeft: true, agrees: true },
      { hypothesis: "c1_disconnected", expectedRuledOut: true, ruledOut: false, onlyOneLeft: false, agrees: false },
    ]);
    expect(result.matched).toBe(2);
  });

  it("treats an explanation with no recorded status as still possible (missing data rules nothing out)", () => {
    const result = comparePrediction({ candidates: ["r1_decreased"], expected: ["r1_decreased"] }, after);
    expect(result.checks[0]).toMatchObject({ ruledOut: false, agrees: false });
  });

  it("shows Ruled out / Still possible per explanation with the case's own labels", () => {
    const c = makeCase({
      hypotheses: [
        ...makeCase().hypotheses,
        { id: "c1_disconnected", label: "C1 is disconnected (open)", component_id: "C1", change: "disconnected", multiplier: null },
      ],
    });
    const result = comparePrediction(
      { candidates: ["r1_increased", "c1_increased", "c1_disconnected"], expected: ["r1_increased", "c1_disconnected"] },
      after,
    );
    render(<PredictionCheckView c={c} result={result} />);
    expect(screen.getByText("Your prediction: 2 of 3 matched the evidence")).toBeInTheDocument();
    const list = screen.getByRole("list", { name: /compared with the recorded evidence/ });
    const items = within(list).getAllByRole("listitem");
    expect(items[0]).toHaveTextContent(/R1 is about twice its nominal value.*You expected: ruled out.*Ruled out.*\(as you expected\)/);
    expect(items[1]).toHaveTextContent(/C1 is about twice its nominal value.*still possible.*Still possible \(only one left\)/);
    expect(items[2]).toHaveTextContent(/C1 is disconnected \(open\).*You expected: ruled out.*Still possible.*\(not what you expected\)/);
  });
});
