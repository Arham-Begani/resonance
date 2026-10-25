import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { makeRevision, makeSuggestion } from "../../test/fixtures";
import { SuggestionPreview } from "./SuggestPanel";

function renderPreview(overrides: Parameters<typeof makeSuggestion>[0] = {}, stale: string | null = null) {
  const onApply = vi.fn();
  render(
    <SuggestionPreview
      suggestion={makeSuggestion(overrides)}
      revision={makeRevision()}
      stale={stale}
      applying={false}
      nextRevisionOrdinal={2}
      onApply={onApply}
      onDiscard={vi.fn()}
    />,
  );
  return onApply;
}

describe("Design suggestion preview", () => {
  it("is labelled as a prediction and never presents predicted gains as measurements", async () => {
    const onApply = renderPreview();
    const card = screen.getByRole("article", { name: "Suggested values" });
    expect(within(card).getByText("Prediction")).toBeInTheDocument();
    expect(within(card).getByText("Analytical first-order RC estimate, not a measurement.")).toBeInTheDocument();
    expect(within(card).getByText("150 nF")).toBeInTheDocument();
    expect(within(card).getByText(/E12 standard part/)).toBeInTheDocument();
    expect(within(card).getByText("950 Hz")).toBeInTheDocument();
    // Each constraint's verdict says "Predicted"; no bare measured-style Pass/Fail appears.
    expect(within(card).getAllByText("Predicted pass")).toHaveLength(2);
    expect(within(card).queryByText(/^(Pass|Fail)$/)).not.toBeInTheDocument();
    expect(within(card).getByRole("table", { name: /analytical, not measured/ })).toBeInTheDocument();
    expect(within(card).getByText("Applying creates Revision 2. Nothing is measured until you run it.")).toBeInTheDocument();
    await userEvent.click(within(card).getByRole("button", { name: "Apply as Revision 2" }));
    expect(onApply).toHaveBeenCalledTimes(1);
  });

  it("states the tradeoff when the goal is out of reach", () => {
    renderPreview({
      feasible: false,
      predicted_goal_pass: false,
      predicted: [
        { constraint_label: "Musical tone", frequency_hz: 440, predicted_gain_db: -0.4, margin_db: 0.6, passes: true },
        { constraint_label: "Whistle", frequency_hz: 600, predicted_gain_db: -4.1, margin_db: -7.9, passes: false },
      ],
      tradeoff: "No cutoff meets both: keeping the tone within 1 dB leaves the 600 Hz whistle 7.9 dB short of 12 dB.",
    });
    expect(screen.getByText("Predicted fail")).toBeInTheDocument();
    expect(screen.getByText("Not achievable with this circuit")).toBeInTheDocument();
    expect(screen.getByText(/7\.9 dB short of 12 dB/)).toBeInTheDocument();
  });

  it("cannot be applied once the circuit or goal has moved on", () => {
    renderPreview({}, "The goal changed since this prediction. Suggest again.");
    expect(screen.getByText("The goal changed since this prediction. Suggest again.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Apply as Revision 2" })).toBeDisabled();
  });
});
