import { render, screen } from "@testing-library/react";
import { makeHighpassRevision, makeRevision } from "../../test/fixtures";
import { layoutOf, Schematic } from "./Schematic";

describe("Schematic topology comes from the graph", () => {
  it("low-pass: R1 in series (Input→Output), C1 shunt (Output→Ground)", () => {
    const l = layoutOf(makeRevision())!;
    expect([l.series.id, l.shunt.id]).toEqual(["R1", "C1"]);
  });

  it("high-pass: C1 in series (Input→Output), R1 shunt (Output→Ground)", () => {
    const revision = makeHighpassRevision();
    const l = layoutOf(revision)!;
    expect([l.series.id, l.shunt.id]).toEqual(["C1", "R1"]);
    const { container } = render(<Schematic revision={revision} selectedId={null} probe={null} onSelect={() => undefined} />);
    // The text alternative is the trusted topology description.
    expect(screen.getByRole("img", { name: /C1 — capacitor — 100 nF — connected between Input and Output/ })).toBeInTheDocument();
    // The series capacitor is two vertical plates on the top rail; the shunt resistor is a zigzag.
    const c1 = container.querySelector('[data-part="C1"] .sch-body')!.getAttribute("d")!;
    const r1 = container.querySelector('[data-part="R1"] .sch-body')!.getAttribute("d")!;
    expect(c1).toMatch(/^M\S+ \S+ V\S+ M\S+ \S+ V\S+$/);
    expect(r1.split(" L").length).toBeGreaterThan(5);
  });

  it("does not draw an unknown topology", () => {
    const broken = makeRevision();
    broken.components = broken.components.filter((c) => c.id !== "C1");
    render(<Schematic revision={broken} selectedId={null} probe={null} onSelect={() => undefined} />);
    expect(screen.getByText("No schematic drawing is available for this circuit template.")).toBeInTheDocument();
  });
});
