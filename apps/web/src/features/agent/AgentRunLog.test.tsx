import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiFailure } from "../../api/client";
import { makeRun, runEvents } from "../../test/fixtures";
import { AgentRunLog, groupEvents } from "./AgentRunLog";
import type { RunView } from "./agentRuns";

const resolver = { label: (id: string) => (id === "obs_fixture000001" ? "Obs 1" : null), onSelect: vi.fn() };

function view(overrides: Partial<RunView> = {}): RunView {
  return { run: makeRun(), events: runEvents, connection: "streaming", startError: null, cancelling: false, ...overrides };
}

describe("AgentRunLog tool labels", () => {
  it("names the design-suggestion and Learn tools readably and never prints raw result IDs", () => {
    const at = "2026-10-15T12:01:00Z";
    const events: RunView["events"] = [
      { type: "tool_call", seq: 1, run_id: "run_fixture000001", at, call_id: "c1", tool: "suggest_design", arguments: { vary: "C1", revision_id: "rev_AbCdEf123456-_xy" } },
      { type: "tool_result", seq: 2, run_id: "run_fixture000001", at, call_id: "c1", tool: "suggest_design", ok: true, summary: "Prediction for rev_AbCdEf123456-_xy: C1 82 nF (E12).", observation_id: null, experiment_id: null, proposal_id: null, error: null },
      { type: "tool_call", seq: 3, run_id: "run_fixture000001", at, call_id: "c2", tool: "read_learn_attempt", arguments: { attempt_id: "learn_Zz9-Yy8_Xx7Ww6V" } },
      { type: "tool_result", seq: 4, run_id: "run_fixture000001", at, call_id: "c2", tool: "read_learn_attempt", ok: true, summary: "Read Learn attempt learn_Zz9-Yy8_Xx7Ww6V (2 of 2 predictions matched the measurement).", observation_id: null, experiment_id: null, proposal_id: null, error: null },
    ];
    const labels = { label: (id: string) => (id === "learn_Zz9-Yy8_Xx7Ww6V" ? "Attempt 1" : null) };
    const { container } = render(<AgentRunLog title="AI lab partner" view={view({ run: makeRun({ kind: "explore", case_id: null }), events })} resolver={labels} />);
    expect(screen.getByText("Suggest standard parts (prediction)")).toBeInTheDocument();
    expect(screen.getByText("(vary: C1)")).toBeInTheDocument();
    expect(screen.getByText("Read Learn attempt")).toBeInTheDocument();
    expect(screen.getByText("Prediction for a revision: C1 82 nF (E12).")).toBeInTheDocument();
    expect(screen.getByText("Read Learn attempt Attempt 1 (2 of 2 predictions matched the measurement).")).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/(rev|learn)_[A-Za-z0-9_-]{6}/);
  });
});

describe("AgentRunLog", () => {
  it("pairs tool calls with their results and shows the genuine current status", () => {
    render(<AgentRunLog title="AI lab partner" view={view()} resolver={resolver} onCancel={vi.fn()} />);
    expect(screen.getByText("Waiting for the model")).toBeInTheDocument();
    expect(screen.getByText("Run experiment")).toBeInTheDocument();
    expect(screen.getByText("(instrument: source current sweep)")).toBeInTheDocument();
    expect(screen.getByText(/Source current recorded on 31 frequencies/)).toBeInTheDocument();
    expect(groupEvents(runEvents).filter((i) => i.kind === "tool")).toHaveLength(1);
  });

  it("renders assistant text with citation chips that open the cited observation", async () => {
    render(<AgentRunLog title="AI lab partner" view={view()} resolver={resolver} onCancel={vi.fn()} />);
    expect(screen.getByText("The current at high frequency is lower than expected.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Obs 1 · source current @ 20 kHz" }));
    expect(resolver.onSelect).toHaveBeenCalledWith("obs_fixture000001");
  });

  it("offers Cancel while running and reports a cancelled run calmly", async () => {
    const onCancel = vi.fn();
    const { rerender } = render(<AgentRunLog title="AI lab partner" view={view()} resolver={resolver} onCancel={onCancel} />);
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalled();
    rerender(<AgentRunLog title="AI lab partner" view={view({ run: makeRun({ status: "cancelled" }), connection: "closed" })} resolver={resolver} onCancel={onCancel} />);
    expect(screen.getByText("Cancelled")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
  });

  it("labels development-fixture runs as not AI", () => {
    render(<AgentRunLog title="AI lab partner" view={view({ run: makeRun({ source: "dev_fixture" }) })} resolver={resolver} />);
    expect(screen.getByText("Development fixture")).toBeInTheDocument();
    expect(screen.getByText(/scripted test data, not AI inference/)).toBeInTheDocument();
  });

  it("explains an AI-unavailable start failure and keeps manual tools available", () => {
    const startError = new ApiFailure("ai_unavailable", "AI lab partner unavailable: NEBIUS_API_KEY is not set.", 503);
    render(<AgentRunLog title="AI lab partner" view={view({ run: null, events: [], connection: "idle", startError })} />);
    expect(screen.getByText("AI lab partner unavailable")).toBeInTheDocument();
    expect(screen.getByText(/Manual tools keep working/)).toBeInTheDocument();
  });

  it("shows measured usage only when the provider reported it", () => {
    const finished = makeRun({
      status: "completed",
      usage: { model_calls: 3, tool_calls: 2, prompt_tokens: 3000, completion_tokens: 210, total_tokens: 3210, latency_ms: 8400 },
    });
    render(<AgentRunLog title="AI lab partner" view={view({ run: finished, connection: "closed" })} resolver={resolver} />);
    expect(screen.getByText(/Measured: 3 model calls · 2 tool calls · 3,210 tokens · 8.4 s/)).toBeInTheDocument();
  });
});
