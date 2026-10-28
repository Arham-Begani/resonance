// The case lifecycle stepper is derived only from the public case. It must never invent progress
// (no percentages), must follow the server's status/budget/actions, and must not render the
// "N of 3 experiments left" wording before an approach is chosen (the budget meter owns that).
import type { PublicCase } from "../../api/types";
import { makeCase } from "../../test/fixtures";
import { caseSteps } from "./InvestigatePanel";

const states = (c: PublicCase) => Object.fromEntries(caseSteps(c).map((s) => [s.key, s.state]));

describe("caseSteps", () => {
  it("starts on Measure with the budget as used / total, before any approach is chosen", () => {
    const c = makeCase({ status: "ready", mode: null, budget: { ...makeCase().budget, used: 0, remaining: 3 }, available_actions: ["choose_mode", "close"] });
    expect(states(c)).toEqual({ symptom: "done", measure: "current", diagnose: "upcoming", repair: "upcoming", reveal: "upcoming" });
    const measure = caseSteps(c).find((s) => s.key === "measure")!;
    expect(measure.detail).toBe("0 of 3 used");
    expect(caseSteps(c).map((s) => s.detail ?? "").join(" ")).not.toMatch(/experiments left/);
  });

  it("moves to Diagnose when the budget is spent, then to Repair when the server offers it", () => {
    const spent = makeCase({ budget: { ...makeCase().budget, used: 3, remaining: 0 }, available_actions: ["submit_diagnosis", "close"] });
    expect(states(spent)).toMatchObject({ measure: "done", diagnose: "current", repair: "upcoming" });
    const diagnosed = makeCase({ status: "diagnosis_submitted", diagnosis: {} as never, available_actions: ["propose_repair", "close"] });
    expect(states(diagnosed)).toMatchObject({ measure: "done", diagnose: "done", repair: "current", reveal: "upcoming" });
  });

  it("offers Diagnose alongside Measure whenever the server accepts a diagnosis", () => {
    const ready = makeCase({ status: "ready", mode: null, available_actions: ["choose_mode", "submit_diagnosis", "close"] });
    expect(states(ready)).toMatchObject({ measure: "current", diagnose: "available" });
    const observing = makeCase({ budget: { ...makeCase().budget, used: 1, remaining: 2 }, available_actions: ["run_experiment", "request_hint", "submit_diagnosis", "close"] });
    expect(states(observing)).toMatchObject({ measure: "current", diagnose: "available", repair: "upcoming" });
    const diagnose = caseSteps(observing).find((s) => s.key === "diagnose")!;
    expect(diagnose.detail).toBe("any time");
    // Only one step is ever the current one (aria-current).
    expect(caseSteps(observing).filter((s) => s.state === "current")).toHaveLength(1);
    // While the AI runs, the server withholds submit_diagnosis, so Diagnose is not offered.
    const agentRunning = makeCase({ mode: "agent", budget: { ...makeCase().budget, used: 1, remaining: 2 }, available_actions: ["cancel_agent", "close"] });
    expect(states(agentRunning)).toMatchObject({ measure: "current", diagnose: "upcoming" });
  });

  it("reports the repair outcome from the verification, and Reveal as next", () => {
    const repaired = makeCase({
      status: "repair_verification",
      diagnosis: {} as never,
      repair: { status: "succeeded", functional_recovery: false } as never,
      available_actions: ["close"],
    });
    const repair = caseSteps(repaired).find((s) => s.key === "repair")!;
    expect(repair).toMatchObject({ state: "done", detail: "not restored" });
    expect(states(repaired).reveal).toBe("current");
  });

  it("marks skipped steps when a case closes early, and a stopped step when it failed", () => {
    const closed = makeCase({ status: "closed", reveal: {} as never, available_actions: [] });
    expect(states(closed)).toEqual({ symptom: "done", measure: "done", diagnose: "skipped", repair: "skipped", reveal: "done" });
    const failed = makeCase({ status: "failed", available_actions: ["close"] });
    expect(caseSteps(failed).filter((s) => s.state === "stopped")).toHaveLength(1);
  });
});
