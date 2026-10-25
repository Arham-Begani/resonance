import type { HypothesisCheck, ObservedTone } from "../../api/types";
import { makeCase } from "../../test/fixtures";
import { checkText, floorsOf, toneAmplitudeText, toneGainText, tonePhaseText } from "./floors";

const tone = (overrides: Partial<ObservedTone> = {}): ObservedTone => ({
  frequency_hz: 440, label: "Musical tone", role: "wanted", amplitude: 0, amplitude_unit: "V", gain_db: -120, phase_deg: 0,
  expected_amplitude: 0.58, expected_gain_db: -0.3, expected_phase_deg: -15.5, ...overrides,
});

describe("measurement floor (M2-02): floored values are never shown as measurements", () => {
  const f = floorsOf(makeCase());

  it("uses the published floors until EvidencePolicy carries them, then the policy's values", () => {
    expect(f).toEqual({ db: -120, ma: 1e-6 });
    const c = makeCase();
    expect(floorsOf({ policy: { ...c.policy, measurement_floor_db: -100, current_floor_ma: 1e-5 } as typeof c.policy })).toEqual({ db: -100, ma: 1e-5 });
  });

  it("names a floored tone instead of numbering it and blanks its phase", () => {
    const t = tone();
    expect(toneAmplitudeText(t, "observed", f)).toBe("No measurable output");
    expect(toneGainText(t, "observed", f)).toBe("No measurable output");
    expect(tonePhaseText(t, "observed", f)).toBe("—");
    expect(toneGainText(t, "expected", f)).toBe("−0.3 dB");
    const i = tone({ amplitude_unit: "mA", amplitude: 1e-7, gain_db: null, expected_gain_db: null, expected_amplitude: 0.05 });
    expect(toneAmplitudeText(i, "observed", f)).toBe("No measurable current");
  });

  it("does not present a difference against a floored value as measured", () => {
    const ch: HypothesisCheck = { observation_id: "obs_x", consistent: false, quantity: "output_gain_db", frequency_hz: 4000, observed: -200, predicted: -8.6, deviation: 191.4, tolerance: 0.5, unit: "dB" };
    expect(checkText(ch, f)).toBe("observed no measurable output vs predicted −8.60 dB");
  });
});
