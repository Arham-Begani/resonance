// The AI's text is rendered through a small Markdown subset built from React elements. These tests
// pin the risks: the subset must render (no literal ** or list dashes), model-written HTML must stay
// inert text, and result IDs must still resolve to workbench labels instead of leaking.
import { render, screen, within } from "@testing-library/react";
import { makeRun } from "../../test/fixtures";
import { AgentRunLog } from "./AgentRunLog";
import type { RunView } from "./agentRuns";
import { Markdown, parseInline } from "./Markdown";

describe("Markdown subset", () => {
  it("renders bold, italic and inline code without leaving the markers", () => {
    const { container } = render(<Markdown text="The **4 kHz** tone is *quieter* than `expected`." />);
    expect(container.querySelector("strong")).toHaveTextContent("4 kHz");
    expect(container.querySelector("em")).toHaveTextContent("quieter");
    expect(container.querySelector("code")).toHaveTextContent("expected");
    expect(container.textContent).toBe("The 4 kHz tone is quieter than expected.");
  });

  it("renders bullet and numbered lists, and separates paragraphs", () => {
    const text = "Findings:\n\n- R1 fits\n* C1 is **ruled out**\n\n1. Measure current\n2. Submit\n\nDone.";
    const { container } = render(<Markdown text={text} />);
    const bullets = container.querySelector("ul")!;
    expect(within(bullets).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["R1 fits", "C1 is ruled out"]);
    const numbered = container.querySelector("ol")!;
    expect(within(numbered).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["Measure current", "Submit"]);
    expect(container.querySelectorAll("p")).toHaveLength(2);
    expect(container.textContent).not.toMatch(/\*\*|^- /m);
  });

  it("keeps HTML in the model's text as inert, visible text", () => {
    const { container } = render(<Markdown text={'<img src=x onerror="alert(1)"> **<script>bad()</script>**\n- <a href="javascript:x">link</a>'} />);
    expect(container.querySelector("img, script, a")).toBeNull();
    expect(container.textContent).toContain('<img src=x onerror="alert(1)">');
    expect(container.querySelector("strong")).toHaveTextContent("<script>bad()</script>");
  });

  it("leaves unmatched or spaced markers as typed (arithmetic, snake_case)", () => {
    expect(parseInline("2 * 3 * 4")).toEqual([{ kind: "text", text: "2 * 3 * 4" }]);
    expect(parseInline("a **dangling marker")).toEqual([{ kind: "text", text: "a **dangling marker" }]);
    expect(parseInline("source_current_ma")).toEqual([{ kind: "text", text: "source_current_ma" }]);
  });
});

describe("AgentRunLog renders AI messages as Markdown with resolved citations", () => {
  it("formats the message, resolves raw result IDs to labels, and keeps citation chips", () => {
    const at = "2026-10-15T12:01:00Z";
    const events: RunView["events"] = [
      {
        type: "message",
        seq: 1,
        run_id: "run_fixture000001",
        at,
        text: "**Evidence**\n- obs_fixture000001 shows *half* the current\n- obs_unknown999999 is not cited",
        citations: [{ result_id: "obs_fixture000001", quantity: "source_current_ma", frequency_hz: 20000 }],
      } as RunView["events"][number],
    ];
    const resolver = { label: (id: string) => (id === "obs_fixture000001" ? "Obs 1" : null), onSelect: vi.fn() };
    const view: RunView = { run: makeRun({ status: "completed" }), events, connection: "closed", startError: null, cancelling: false };
    const { container } = render(<AgentRunLog title="AI lab partner" view={view} resolver={resolver} />);
    const items = within(container.querySelector(".agent-item--message ul")!).getAllByRole("listitem");
    expect(items.map((li) => li.textContent)).toEqual(["Obs 1 shows half the current", "an observation is not cited"]);
    expect(container.querySelector(".agent-item--message strong")).toHaveTextContent("Evidence");
    expect(screen.getByRole("button", { name: "Obs 1 · source current @ 20 kHz" })).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/obs_[A-Za-z0-9]{6}|\*\*/);
  });
});
