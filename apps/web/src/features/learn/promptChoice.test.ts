// Learn step 1 names each change briefly ("Double C1", "100 nF → 200 nF") from the prompt's own
// change and the current circuit; the full question is shown in step 2.
import { makeLearnPrompts, makeRevision } from "../../test/fixtures";
import { promptChoice } from "./LearnPanel";

describe("promptChoice", () => {
  it("names the change and its values from the circuit", () => {
    const [doubleC1, halveR1] = makeLearnPrompts();
    const revision = makeRevision();
    expect(promptChoice(doubleC1!, revision)).toEqual({ title: "Double C1", values: "100 nF → 200 nF" });
    expect(promptChoice(halveR1!, revision)).toEqual({ title: "Halve R1", values: "1 kΩ → 500 Ω" });
  });

  it("still names the change when the circuit is not loaded", () => {
    expect(promptChoice(makeLearnPrompts()[0]!, null)).toEqual({ title: "Double C1", values: null });
  });
});
