import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import type { ComponentId, EditableComponentId, ValueParseResult } from "../../api/types";
import type { Probe } from "../../state/workbench";
import { makeRevision } from "../../test/fixtures";
import { Schematic } from "../schematic/Schematic";
import { ComponentList, ProbeBar } from "./ComponentList";
import { PARSE_DEBOUNCE_MS, ValueEditor } from "./ValueEditor";

function Harness({ editable = true, onApply = vi.fn() }: { editable?: boolean; onApply?: (id: EditableComponentId, text: string) => void }) {
  const revision = makeRevision();
  const [selected, setSelected] = useState<ComponentId | null>(null);
  const [editing, setEditing] = useState<EditableComponentId | null>(null);
  const [probe, setProbe] = useState<Probe>({ kind: "node", node: "out" });
  return (
    <>
      <Schematic revision={revision} selectedId={selected} probe={probe} onSelect={setSelected} />
      <ProbeBar revision={revision} probe={probe} onProbe={setProbe} />
      <ComponentList
        revision={revision}
        selectedId={selected}
        editingId={editing}
        probe={probe}
        editable={editable}
        lockedReason="Editing is locked during a mystery."
        applying={false}
        applyError={null}
        onSelect={setSelected}
        onEdit={setEditing}
        onApply={onApply}
        onProbe={setProbe}
      />
    </>
  );
}

describe("ComponentList and Schematic", () => {
  it("builds both views from the same revision and keeps selection in sync", async () => {
    const { container } = render(<Harness />);
    // Schematic shows the same values as the list.
    expect(container.querySelector("svg")?.textContent).toContain("1 kΩ");
    const r1Row = screen.getByRole("button", { name: /R1 — resistor — 1 kΩ — connected between Input and Output/ });
    expect(r1Row).toHaveAttribute("aria-pressed", "false");

    // Clicking the schematic part selects the matching list entry.
    const part = container.querySelector('[data-part="R1"] path') as Element;
    fireEvent.click(part);
    expect(r1Row).toHaveAttribute("aria-pressed", "true");
    expect(container.querySelector('[data-part="R1"]')).toHaveClass("is-selected");

    // Selecting in the list highlights the schematic part.
    await userEvent.click(screen.getByRole("button", { name: /C1 — capacitor/ }));
    expect(container.querySelector('[data-part="C1"]')).toHaveClass("is-selected");
    expect(container.querySelector('[data-part="R1"]')).not.toHaveClass("is-selected");
  });

  it("moves between rows with arrow keys (roving tabindex)", async () => {
    render(<Harness />);
    const rows = [/V1 — voltage source/, /R1 — resistor/, /C1 — capacitor/].map((name) => screen.getByRole("button", { name }));
    expect(rows[0]).toHaveAttribute("tabindex", "0");
    expect(rows[1]).toHaveAttribute("tabindex", "-1");
    rows[0]!.focus();
    await userEvent.keyboard("{ArrowDown}");
    expect(rows[1]).toHaveFocus();
    await userEvent.keyboard("{End}");
    expect(rows[2]).toHaveFocus();
    expect(rows[2]).toHaveAttribute("tabindex", "0");
  });

  it("probe actions mark the probed node on the schematic", async () => {
    const { container } = render(<Harness />);
    await userEvent.click(screen.getByRole("button", { name: "Input voltage" }));
    expect(screen.getByRole("button", { name: "Input voltage" })).toHaveAttribute("aria-pressed", "true");
    expect(container.querySelectorAll(".sch-net.is-probed")).toHaveLength(1);
    expect(container.querySelector(".sch-probe")).not.toBeNull();
    // The row shortcut (icon-only, named by its action) probes the same thing as the bar.
    await userEvent.click(screen.getByRole("button", { name: "Probe current at V1" }));
    expect(screen.getByRole("button", { name: "Source current" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Probe current at V1" })).toHaveAttribute("aria-pressed", "true");
  });

  it("disables editing with a visible reason when the design is frozen", () => {
    render(<Harness editable={false} />);
    const editR1 = screen.getByRole("button", { name: "Edit R1 value" });
    expect(editR1).toBeDisabled();
    expect(screen.getByText("Editing is locked during a mystery.")).toBeInTheDocument();
  });

  it("Escape cancels an inline edit and returns focus to the row", async () => {
    render(<Harness />);
    const r1Row = screen.getByRole("button", { name: /R1 — resistor/ });
    await userEvent.click(r1Row);
    await userEvent.click(screen.getByRole("button", { name: "Edit R1 value" }));
    const input = screen.getByLabelText("New resistor value for R1");
    expect(input).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByLabelText("New resistor value for R1")).not.toBeInTheDocument();
    expect(r1Row).toHaveFocus();
  });
});

describe("ValueEditor live validation", () => {
  beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }));
  afterEach(() => vi.useRealTimers());

  function renderEditor(result: ValueParseResult, onApply = vi.fn()) {
    const parseValue = vi.fn(async (_body: { component_id: string; text: string; template_id?: string }) => result);
    const r1 = makeRevision().components[1]!;
    render(
      <ValueEditor component={r1} templateId="rc_lowpass" applying={false} serverError={null} onApply={onApply} onCancel={vi.fn()} client={{ parseValue }} />,
    );
    return { parseValue, onApply };
  }

  it("shows the parsed canonical value after the debounce and applies on Enter", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const { parseValue, onApply } = renderEditor({ ok: true, value_si: 4700, display_value: "4.7 kΩ", message: null });
    const input = screen.getByLabelText("New resistor value for R1");
    await user.clear(input);
    await user.type(input, "4.7k");
    expect(parseValue).not.toHaveBeenCalled();
    await act(async () => {
      vi.advanceTimersByTime(PARSE_DEBOUNCE_MS + 10);
    });
    expect(parseValue).toHaveBeenCalledTimes(1);
    expect(parseValue.mock.calls[0]![0]).toMatchObject({ component_id: "R1", text: "4.7k", template_id: "rc_lowpass" });
    expect(await screen.findByText("= 4.7 kΩ")).toBeInTheDocument();
    await user.keyboard("{Enter}");
    expect(onApply).toHaveBeenCalledWith("R1", "4.7k");
  });

  it("shows the server's rejection and refuses to apply", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const { onApply } = renderEditor({ ok: false, value_si: null, display_value: null, message: "Ambiguous unit: use 'm' for milli or 'M' for mega." });
    const input = screen.getByLabelText("New resistor value for R1");
    await user.clear(input);
    await user.type(input, "4.7m");
    await act(async () => {
      vi.advanceTimersByTime(PARSE_DEBOUNCE_MS + 10);
    });
    expect(await screen.findByText(/Ambiguous unit/)).toBeInTheDocument();
    expect(input).toHaveAttribute("aria-invalid", "true");
    const form = screen.getByRole("form", { name: "Edit R1 value" });
    await user.click(within(form).getByRole("button", { name: "Apply" }));
    expect(onApply).not.toHaveBeenCalled();
  });
});
