// The landing figures are labeled analytical; these checks keep the illustrated physics correct.
import { cutoffHz, LOWPASS_DEFAULT, lowpassGainDb, scaled, sourceCurrentPerVolt } from "./rc";

describe("analytical RC relations behind the landing figures", () => {
  it("places the default low-pass cutoff at 1/(2πRC) with −3.01 dB there", () => {
    const fc = cutoffHz(LOWPASS_DEFAULT);
    expect(fc).toBeCloseTo(1591.55, 1);
    expect(lowpassGainDb(fc, LOWPASS_DEFAULT)).toBeCloseTo(-3.0103, 3);
    expect(lowpassGainDb(10, LOWPASS_DEFAULT)).toBeGreaterThan(-0.001);
  });

  it("doubling R or C gives identical output gain but a 2× source-current difference", () => {
    const r2 = scaled(LOWPASS_DEFAULT, { r: 2 });
    const c2 = scaled(LOWPASS_DEFAULT, { c: 2 });
    for (const f of [20, 100, 440, 1_000, 4_000, 20_000]) {
      expect(lowpassGainDb(f, r2)).toBeCloseTo(lowpassGainDb(f, c2), 10);
      expect(sourceCurrentPerVolt(f, c2) / sourceCurrentPerVolt(f, r2)).toBeCloseTo(2, 10);
    }
    // Far above cutoff the capacitor is a short, so the current approaches V/R.
    expect(sourceCurrentPerVolt(1e7, r2)).toBeCloseTo(1 / r2.r, 6);
  });
});
