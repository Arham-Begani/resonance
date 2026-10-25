import "@testing-library/jest-dom/vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { Endpoints } from "./api/endpoints";
import type { EditProposal, GoalInput, LearnAttemptRequest } from "./api/types";
import { AudioController } from "./components/audio/AudioController";
import {
  healthMissingKey,
  highpassGoal,
  highpassTones,
  lowpassGoal,
  makeExperiment,
  makeCase,
  makeHighpassRevision,
  makeLearnAttempt,
  makeLearnPrompts,
  makeObservation,
  makeRevision,
  makeTemplates,
  makeWorkbench,
} from "./test/fixtures";

// Replace the typed endpoint module with test doubles (fixtures only exist in tests).
const mock = vi.hoisted(() => ({ endpoints: {} as Record<string, ReturnType<typeof vi.fn>> }));
vi.mock("./api/endpoints", () => ({
  endpoints: new Proxy(
    {},
    {
      get: (_t, key: string) => (mock.endpoints[key] ??= vi.fn(async () => {
        throw new Error(`unmocked endpoint ${key}`);
      })),
    },
  ),
  agentRunEventsUrl: (id: string) => `/api/agent-runs/${id}/events`,
}));

const { App } = await import("./App");

// Whole-app flows chain many async renders; under the full parallel suite one took 21 s against
// the 20 s default, so these get an explicit budget (each takes 2-8 s alone).
const INTEGRATION_TIMEOUT_MS = 60_000;

type Stubs = Partial<Record<keyof Endpoints, (...args: never[]) => unknown>>;

const feasibility = makeWorkbench().feasibility;
const goalFrom = (input: GoalInput, id: string) => ({ goal: { id, origin: "user" as const, ...input }, feasibility });

function stub(overrides: Stubs = {}) {
  mock.endpoints = {};
  const base: Stubs = {
    health: async () => healthMissingKey,
    templates: async () => makeTemplates(),
    workbench: async () =>
      makeWorkbench({ experiments: [{ id: "exp_fixture000001", revision_id: "rev_fixture000001", status: "succeeded", created_at: "2026-10-15T12:00:00Z", goal_passed: null }] }),
    getExperiment: async () => makeExperiment(),
    setCurrentRevision: async () => makeWorkbench(),
  };
  for (const [k, fn] of Object.entries({ ...base, ...overrides })) mock.endpoints[k] = vi.fn(fn as never);
}

beforeEach(() => {
  window.location.hash = "";
  window.sessionStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("first impression", { timeout: INTEGRATION_TIMEOUT_MS }, () => {
  it("measures the opening experiment on a first visit and plays nothing until Play is pressed", async () => {
    const play = vi.spyOn(AudioController.prototype, "play");
    const runs: unknown[] = [];
    stub({
      workbench: async () => makeWorkbench({ revisions: [], current_revision_id: null, experiments: [] }),
      createRevision: async () => makeRevision(),
      runExperiment: async (body: never) => {
        runs.push(body);
        return makeExperiment();
      },
    });
    render(<App />);
    expect(await screen.findByRole("list", { name: "Measured tone levels" })).toBeInTheDocument();
    // Exactly one automatic simulation, evaluated against the opening goal.
    expect(runs).toEqual([{ revision_id: "rev_fixture000001", goal_id: "goal_fixture00001" }]);
    const listen = screen.getByRole("region", { name: "Keep the musical tone. Reduce the whistle." });
    expect(within(listen).getByText("Opening experiment")).toBeInTheDocument();
    expect(within(listen).getByText(/Press Play to hear the 440 Hz musical tone with the 4 kHz whistle\. Nothing plays automatically\./)).toBeInTheDocument();
    expect(within(listen).getByRole("button", { name: "Play input" })).toBeEnabled();
    // Loading clips into the shared player is not playing them.
    expect(play).not.toHaveBeenCalled();
    expect(screen.queryByText(/^Playing:/)).not.toBeInTheDocument();
  });

  it("does not run anything automatically when the session already has measurements", async () => {
    stub({ runExperiment: async () => makeExperiment() });
    render(<App />);
    await screen.findByRole("list", { name: "Measured tone levels" });
    await screen.findByRole("button", { name: "Run experiment on Revision 1" });
    expect(mock.endpoints.runExperiment).not.toHaveBeenCalled();
  });
});

describe("circuit family switching", { timeout: INTEGRATION_TIMEOUT_MS }, () => {
  it("creates the high-pass root, sets its goal after the revision, draws it from the graph, and undoes across families", async () => {
    const calls: unknown[][] = [];
    const hp = makeHighpassRevision();
    stub({
      createRevision: async (body: never) => {
        calls.push(["createRevision", body]);
        return hp;
      },
      setCurrentRevision: async (id: never) => {
        calls.push(["setCurrentRevision", id]);
        return makeWorkbench({ current_revision_id: id });
      },
      setGoal: async (input: never) => {
        calls.push(["setGoal", (input as GoalInput).title]);
        return goalFrom(input as GoalInput, `goal_${calls.length}`);
      },
      runExperiment: async (body: never) => {
        calls.push(["runExperiment", (body as { revision_id: string }).revision_id]);
        return makeExperiment({
          id: "exp_fixture000002",
          revision_id: hp.id,
          settings: { ...makeExperiment().settings, transient: { ...makeExperiment().settings.transient, tones: highpassTones } },
          tones: highpassTones.map((t) => ({ frequency_hz: t.frequency_hz, label: t.label, role: t.role, input_amplitude_v: t.amplitude_v, output_amplitude_v: t.amplitude_v / 2, transient_gain_db: -6, ac_gain_db: -6, agreement_db: 0, phase_deg: 45 })),
        });
      },
    });
    const { container } = render(<App />);
    await screen.findByRole("list", { name: "Measured tone levels" });
    const family = screen.getByRole("radiogroup", { name: "Circuit family" });
    await userEvent.click(within(family).getByRole("radio", { name: "High-pass" }));

    // The opening experiment of the new family is measured (simulation only).
    await waitFor(() => expect(calls).toContainEqual(["runExperiment", hp.id]));
    expect(calls).toContainEqual(["createRevision", { template_id: "rc_highpass", values: [], origin: "user_edit" }]);
    // Feasibility is analysed for the server's current revision, so it moves before the goal.
    const current = calls.findIndex((c) => c[0] === "setCurrentRevision" && c[1] === hp.id);
    const goal = calls.findIndex((c) => c[0] === "setGoal" && c[1] === highpassGoal.title);
    expect(current).toBeGreaterThanOrEqual(0);
    expect(goal).toBeGreaterThan(current);

    // Schematic and component list both come from the high-pass graph.
    expect(within(family).getByRole("radio", { name: "High-pass" })).toBeChecked();
    expect(container.querySelector("figure.schematic")?.getAttribute("data-template")).toBe("rc_highpass");
    expect(screen.getByRole("button", { name: "C1 — capacitor — 100 nF — connected between Input and Output" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "R1 — resistor — 10 kΩ — connected between Output and Ground" })).toBeInTheDocument();
    // The hum is labelled, with the small-speaker note (charts still show it).
    expect(await screen.findByText(/Small speakers may not reproduce the 50 Hz mains hum/)).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Measured tone levels" })).toHaveTextContent("Mains hum");

    // Undo returns to the low-pass revision and restores the goal that family had.
    const undo = screen.getByRole("button", { name: "Undo to Revision 1" });
    await waitFor(() => expect(undo).toBeEnabled());
    await userEvent.click(undo);
    await waitFor(() => expect(calls).toContainEqual(["setGoal", lowpassGoal.title]));
    expect(calls).toContainEqual(["setCurrentRevision", "rev_fixture000001"]);
    expect(within(family).getByRole("radio", { name: "Low-pass" })).toBeChecked();
    expect(container.querySelector("figure.schematic")?.getAttribute("data-template")).toBe("rc_lowpass");
  });
});

describe("applying an agent proposal that switches family", { timeout: INTEGRATION_TIMEOUT_MS }, () => {
  const proposal: EditProposal = {
    id: "prop_fixture00001",
    base_revision_id: "rev_fixture000001",
    template_id: "rc_highpass",
    changes: [{ component_id: "C1", from_display: "100 nF", from_value_si: 1e-7, to_display: "82 nF", to_value_si: 8.2e-8 }],
    created_at: "2026-10-15T12:02:00Z",
    predicted: [],
    predicted_goal_pass: null,
    rationale: "A high-pass filter removes low-frequency hum.",
    status: "pending",
  };

  function setup(feasible: boolean) {
    const goals: string[] = [];
    stub({
      workbench: async () =>
        makeWorkbench({
          proposals: [proposal],
          experiments: [{ id: "exp_fixture000001", revision_id: "rev_fixture000001", status: "succeeded", created_at: "2026-10-15T12:00:00Z", goal_passed: null }],
        }),
      createRevision: async () => makeHighpassRevision({ parent_id: "rev_fixture000001", origin: "agent_proposal_applied" }),
      // The server analyses the learner's CURRENT goal for the new revision's family.
      setCurrentRevision: async (id: never) =>
        makeWorkbench({ current_revision_id: id, feasibility: { ...makeWorkbench().feasibility, template_id: "rc_highpass", feasible } }),
      setGoal: async (input: never) => {
        goals.push((input as GoalInput).title);
        return goalFrom(input as GoalInput, "goal_hp_default");
      },
    });
    render(<App />);
    return goals;
  }

  it("keeps the learner's goal when the new family can meet it", async () => {
    const goals = setup(true);
    const card = await screen.findByRole("article", { name: "Proposed change" });
    expect(within(card).getByText(/Switches the circuit to the/)).toHaveTextContent("high-pass family");
    await userEvent.click(within(card).getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(screen.getByTestId("live-polite")).toHaveTextContent(/Your goal stays the same: a high-pass filter can meet it \(prediction\)/));
    expect(goals).toEqual([]);
    expect(screen.queryByText("Goal changed")).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Goal" })).toHaveTextContent(lowpassGoal.title);
  });

  it("otherwise switches to the family's opening goal and says so visibly", async () => {
    const goals = setup(false);
    const card = await screen.findByRole("article", { name: "Proposed change" });
    await userEvent.click(within(card).getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(goals).toEqual([highpassGoal.title]));
    const goal = screen.getByRole("region", { name: "Goal" });
    expect(await within(goal).findByText("Goal changed")).toBeInTheDocument();
    expect(goal).toHaveTextContent(/A high-pass filter cannot meet your previous goal \(prediction\), so the goal is now the high-pass opening goal: “Keep the musical tone\. Remove the mains hum\.”/);
    await userEvent.click(within(goal).getByRole("button", { name: "Dismiss goal change note" }));
    expect(within(goal).queryByText("Goal changed")).not.toBeInTheDocument();
    await waitFor(() => expect(within(goal).getByRole("button", { name: "Edit goal" })).toHaveFocus());
  });
});

describe("Investigate: family choice and the Guide-me prediction step", { timeout: INTEGRATION_TIMEOUT_MS }, () => {
  it("starts the chosen family (null for Surprise me) and compares a skippable prediction with the evidence", async () => {
    const before = makeCase();
    const newObs = makeObservation({ id: "obs_fixture000002", sequence: 2, kind: "source_current_sweep", summary: "Source current differs from the intended design." });
    const afterCase = makeCase({
      observations: [...before.observations, newObs],
      budget: { ...before.budget, used: 2, remaining: 1 },
      evidence: {
        ...before.evidence,
        assessments: before.evidence.assessments.map((a) =>
          a.hypothesis_id === "r1_increased" ? { ...a, status: "contradicted" } : a.hypothesis_id === "c1_increased" ? { ...a, status: "supported" } : a,
        ),
        remaining: ["c1_increased"],
        statement: "One explanation fits.",
      },
    });
    const created: unknown[] = [];
    stub({
      createCase: async (templateId: never) => {
        created.push(templateId);
        return before;
      },
      runCaseExperiment: async () => ({ case: afterCase, observation: newObs }),
    });
    window.location.hash = "#investigate";
    render(<App />);
    const family = await screen.findByRole("radiogroup", { name: "Circuit family" });
    // Defaults to the family being explored; "Surprise me" lets the server draw it.
    expect(within(family).getByRole("radio", { name: "Low-pass" })).toBeChecked();
    await userEvent.click(within(family).getByRole("radio", { name: "Surprise me" }));
    await userEvent.click(screen.getByRole("button", { name: "Start a mystery" }));
    await waitFor(() => expect(created).toEqual([null]));

    const measure = await screen.findByRole("region", { name: "Take a measurement" });
    const predict = within(measure).getByRole("group", { name: /which explanations do you expect the next measurement to rule out/ });
    // Only explanations that are still possible are offered.
    expect(within(predict).getAllByRole("checkbox").map((c) => c.closest("label")?.textContent)).toEqual([
      "R1 is about twice its nominal value",
      "C1 is about twice its nominal value",
    ]);
    await userEvent.click(within(predict).getByRole("checkbox", { name: "R1 is about twice its nominal value" }));
    await userEvent.click(within(measure).getAllByRole("button", { name: /^Run/ })[1]!);

    const recorded = await screen.findByRole("region", { name: "Recorded measurements" });
    expect(await within(recorded).findByText("Your prediction: 2 of 2 matched the evidence")).toBeInTheDocument();
    const list = within(recorded).getByRole("list", { name: /compared with the recorded evidence/ });
    expect(list).toHaveTextContent(/R1 is about twice its nominal value.*You expected: ruled out.*Ruled out/);
    expect(list).toHaveTextContent(/C1 is about twice its nominal value.*You expected: still possible.*Still possible \(only one left\)/);
  });
});

describe("Learn mode", { timeout: INTEGRATION_TIMEOUT_MS }, () => {
  it("asks for every tone's prediction, then shows each prediction beside the measured change", async () => {
    let request: LearnAttemptRequest | null = null;
    const attempt = makeLearnAttempt();
    stub({
      learnPrompts: async () => makeLearnPrompts(),
      createLearnAttempt: async (body: never) => {
        request = body;
        return attempt;
      },
      workbench: async () =>
        makeWorkbench({
          revisions: [makeRevision(), makeRevision({ id: "rev_fixture000002", parent_id: "rev_fixture000001", origin: "user_edit" })],
          experiments: [
            { id: "exp_fixture000001", revision_id: "rev_fixture000001", status: "succeeded", created_at: "2026-10-15T12:00:00Z", goal_passed: null },
            { id: "exp_fixture000002", revision_id: "rev_fixture000002", status: "succeeded", created_at: "2026-10-15T12:01:00Z", goal_passed: null },
          ],
        }),
      getExperiment: async (id: never) => makeExperiment({ id: id as string, revision_id: id === "exp_fixture000002" ? "rev_fixture000002" : "rev_fixture000001" }),
    });
    render(<App />);
    await screen.findByRole("list", { name: "Measured tone levels" });
    await userEvent.click(within(screen.getByRole("navigation", { name: "Workbench mode" })).getByRole("button", { name: "Learn" }));

    const form = await screen.findByRole("form", { name: "Make a prediction" });
    expect(within(form).getByRole("radio", { name: /^Double C1/ })).toBeChecked();
    // One prediction is missing: nothing is sent and focus lands on the unanswered tone.
    const tone = within(form).getByRole("group", { name: /Musical tone/ });
    await userEvent.click(within(tone).getByRole("radio", { name: "About the same" }));
    await userEvent.click(within(form).getByRole("button", { name: "Run the experiment" }));
    expect(await within(form).findByRole("alert")).toHaveTextContent("Predict every tone first: whistle.");
    expect(mock.endpoints.createLearnAttempt).not.toHaveBeenCalled();
    const whistle = within(form).getByRole("group", { name: /Whistle/ });
    await waitFor(() => expect(within(whistle).getByRole("radio", { name: "Louder" })).toHaveFocus());

    await userEvent.click(within(whistle).getByRole("radio", { name: "Louder" }));
    await userEvent.type(within(form).getByRole("textbox", { name: "In my own words (optional)" }), "More capacitance shorts more of the high tone.");
    await userEvent.click(within(form).getByRole("button", { name: "Run the experiment" }));

    await waitFor(() => expect(request).not.toBeNull());
    expect(request).toEqual({
      revision_id: "rev_fixture000001",
      change: { component_id: "C1", multiplier: 2 },
      predictions: [
        { frequency_hz: 440, expectation: "about_the_same" },
        { frequency_hz: 4000, expectation: "louder" },
      ],
      reasoning: "More capacitance shorts more of the high tone.",
    });

    const result = await screen.findByRole("region", { name: "Result · Attempt 1" });
    await waitFor(() => expect(within(result).getByRole("heading", { name: "Result · Attempt 1" })).toHaveFocus());
    expect(within(result).getByText("1 of 2 matched")).toBeInTheDocument();
    const graded = within(result).getByRole("list", { name: /Your predictions beside the measured change/ });
    const rows = within(graded).getAllByRole("listitem");
    expect(rows[0]).toHaveTextContent(/Musical tone.*Matched.*You predicted: about the same.*Measured: −0\.9 dB \(about the same\)/);
    expect(rows[1]).toHaveTextContent(/Whistle.*Didn’t match.*You predicted: louder.*Measured: −5\.3 dB \(quieter\)/);
    // Evidence chips name the measured experiment and quantity.
    expect(within(rows[1]!).getByText("Experiment 2 · tone gain @ 4 kHz")).toBeInTheDocument();
    // Server prose citing result IDs is shown with the learner's numbering, never raw IDs.
    expect(within(result).getByText("Measured in Experiment 2: doubling C1 halved the cutoff; the whistle fell by 5.3 dB and the tone by 0.9 dB.")).toBeInTheDocument();
    expect(result).not.toHaveTextContent(/\b(exp|rev)_[A-Za-z0-9]+/);
    expect(within(result).getByText(attempt.tradeoff)).toBeInTheDocument();
    expect(within(result).getByRole("button", { name: "Play previous" })).toBeInTheDocument();
    expect(within(result).getByRole("button", { name: "Play new" })).toBeInTheDocument();
    // Before/after comparison chart, with both experiments named.
    expect(screen.getByRole("figure", { name: "Before and after" })).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Measured tone changes" })).toHaveTextContent("−5.3 dB");
    // The attempt is in this tab's history.
    expect(screen.getByRole("region", { name: "Attempts" })).toHaveTextContent("C1 doubled (100 nF → 200 nF)");
  });

  it("explains calmly when the server does not offer Learn yet", async () => {
    const { ApiFailure } = await import("./api/client");
    stub({
      learnPrompts: async () => {
        throw new ApiFailure("not_implemented", "Learn prompts is not implemented yet.", 501);
      },
    });
    window.location.hash = "#learn";
    render(<App />);
    expect(await screen.findByText("Learn is not available on this server yet")).toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Make a prediction" })).not.toBeInTheDocument();
  });
});
