// Tone readouts: the measured number is shown with the goal verdict for that tone, and the verdict
// comes only from the experiment's own goal evaluation (never computed or assumed in the UI).
import { render, screen, within } from "@testing-library/react";
import type { ToneLevel } from "../../api/types";
import { ToneLevels } from "./ListenCard";

const tones: ToneLevel[] = [
  { frequency_hz: 440, label: "Musical tone", role: "wanted", input_amplitude_v: 0.6, output_amplitude_v: 0.578, transient_gain_db: -0.32, ac_gain_db: -0.32 } as ToneLevel,
  { frequency_hz: 4000, label: "Whistle", role: "interference", input_amplitude_v: 0.3, output_amplitude_v: 0.11, transient_gain_db: -8.7, ac_gain_db: -8.64 } as ToneLevel,
];

describe("ToneLevels", () => {
  it("shows each tone's goal verdict next to its measured level", () => {
    render(
      <ToneLevels
        tones={tones}
        goals={[
          { frequency_hz: 440, kind: "preserve", limit_db: 1, passed: true, margin_db: 0.68 },
          { frequency_hz: 4000, kind: "attenuate", limit_db: 12, passed: false, margin_db: -3.36 },
        ]}
      />,
    );
    const list = screen.getByRole("list", { name: "Measured tone levels" });
    const [tone, whistle] = within(list).getAllByRole("listitem");
    expect(tone).toHaveTextContent("Goal met · lose at most 1.0 dB");
    expect(whistle).toHaveTextContent("Goal not met · cut at least 12.0 dB");
  });

  it("shows no verdict when the shown experiment has no evaluation for the current goal", () => {
    render(<ToneLevels tones={tones} />);
    expect(screen.queryByText(/Goal (not )?met/)).not.toBeInTheDocument();
  });
});
