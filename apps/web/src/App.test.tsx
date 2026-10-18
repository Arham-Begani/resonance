import { render, screen, waitFor, within } from "@testing-library/react";
import { StrictMode } from "react";
import userEvent from "@testing-library/user-event";
import { ApiFailure } from "./api/client";
import type { Endpoints } from "./api/endpoints";
import { healthMissingKey, makeCase, makeExperiment, makeWorkbench } from "./test/fixtures";

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

function stub(overrides: Partial<Record<keyof Endpoints, (...args: never[]) => unknown>> = {}) {
  mock.endpoints = {};
  const base: Partial<Record<keyof Endpoints, (...args: never[]) => unknown>> = {
    health: async () => healthMissingKey,
    workbench: async () => makeWorkbench({ experiments: [{ id: "exp_fixture000001", revision_id: "rev_fixture000001", status: "succeeded", created_at: "2026-10-15T12:00:00Z", goal_passed: null }] }),
    getExperiment: async () => makeExperiment(),
    setCurrentRevision: async () => makeWorkbench(),
  };
  for (const [k, fn] of Object.entries({ ...base, ...overrides })) mock.endpoints[k] = vi.fn(fn as never);
}

describe("first visit bootstrap", () => {
  it("runs once under StrictMode: health first, then one workbench load and at most one original revision", async () => {
    const calls: string[] = [];
    stub({
      health: async () => {
        calls.push("health");
        return healthMissingKey;
      },
      workbench: async () => {
        calls.push("workbench");
        return makeWorkbench({ revisions: [], current_revision_id: null });
      },
      createRevision: async () => {
        calls.push("createRevision");
        return makeWorkbench().revisions[0]!;
      },
    });
    render(
      <StrictMode>
        <App />
      </StrictMode>,
    );
    expect(await screen.findByRole("button", { name: /Run experiment on Revision 1/ })).toBeEnabled();
    expect(calls.slice(0, 3)).toEqual(["health", "workbench", "createRevision"]);
    expect(calls.filter((c) => c === "createRevision")).toHaveLength(1);
    expect(calls.filter((c) => c === "workbench")).toHaveLength(1);
    // The initially shown revision is persisted so a reload shows the same one.
    await waitFor(() => expect(mock.endpoints.setCurrentRevision).toHaveBeenCalledWith("rev_fixture000001"));
  });
});

describe("App with AI unavailable", () => {
  beforeEach(() => {
    window.location.hash = "";
  });

  it("shows the missing-key state everywhere AI appears while manual tools keep working", async () => {
    stub();
    render(<App />);
    expect(await screen.findByText("AI unavailable")).toBeInTheDocument();
    const notice = await screen.findByText("AI lab partner unavailable", { selector: "strong" });
    expect(notice.parentElement?.textContent).toContain("NEBIUS_API_KEY is not set");

    // The typed command box is disabled with the setup message, not hidden.
    const command = screen.getByRole("textbox", { name: /Ask the AI lab partner/ });
    expect(command).toBeDisabled();
    expect(command).toHaveAccessibleDescription(/AI lab partner unavailable/);

    // The manual path is intact: measured tone levels and the run button are available.
    expect(await screen.findByRole("list", { name: "Measured tone levels" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Run experiment on Revision 1/ })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Play" })).toBeEnabled();
  });

  it("keeps the previous result visible when a new experiment fails", async () => {
    stub({
      runExperiment: async () => {
        throw new ApiFailure("simulation_timeout", "The simulation took too long and was stopped.", 504, null, true);
      },
    });
    render(<App />);
    await screen.findByRole("list", { name: "Measured tone levels" });
    await userEvent.click(screen.getByRole("button", { name: /Run experiment on Revision 1/ }));
    expect(await screen.findByText("Simulation timed out")).toBeInTheDocument();
    expect(screen.getByText(/The previous result is still shown/)).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Measured tone levels" })).toBeInTheDocument();
  });

  it("Investigate: Guide me works without AI and Investigate for me is disabled with a reason", async () => {
    stub({ createCase: async () => makeCase({ status: "ready", mode: null, observations: [makeCase().observations[0]!], available_actions: ["choose_mode", "close"] }) });
    render(<App />);
    await screen.findByText("AI unavailable");
    await userEvent.click(screen.getByRole("button", { name: "Investigate" }));
    await userEvent.click(await screen.findByRole("button", { name: "Start a mystery" }));
    const approach = await screen.findByRole("region", { name: "Choose an approach" });
    expect(within(approach).getByRole("button", { name: /Guide me/ })).toBeEnabled();
    const agent = within(approach).getByRole("button", { name: /Investigate for me/ });
    expect(agent).toBeDisabled();
    expect(agent).toHaveAccessibleDescription(/Guide me works without AI/);
    // The symptom is presented from the public case data.
    expect(screen.getAllByText(/The whistle is 5.7 dB quieter/).length).toBeGreaterThan(0);
    // The intended design is frozen: editing is locked with an explanation.
    await waitFor(() => expect(screen.getByRole("button", { name: "Edit R1 value" })).toBeDisabled());
  });
});
