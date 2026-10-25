import { render, screen, within } from "@testing-library/react";
import landingCss from "./landing.css?raw";
import { Landing, REPO_URL } from "./Landing";

describe("Landing page", () => {
  it("has the page landmarks, a skip link to main, and one h1", () => {
    render(<Landing />);
    expect(screen.getByRole("banner")).toBeInTheDocument();
    expect(screen.getByRole("contentinfo")).toBeInTheDocument();
    const main = screen.getByRole("main");
    expect(main).toHaveAttribute("id", "main");
    expect(screen.getByRole("link", { name: "Skip to content" })).toHaveAttribute("href", "#main");
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    const sections = screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent);
    expect(sections).toEqual(
      expect.arrayContaining([
        "Explore, learn, then investigate.",
        "Why a doubled resistor and a doubled capacitor sound the same.",
        "Every number has a source.",
      ]),
    );
  });

  it("points every call to action at the real workbench routes", () => {
    render(<Landing />);
    const openLab = screen.getAllByRole("link", { name: "Open the lab" });
    expect(openLab.length).toBeGreaterThanOrEqual(2);
    for (const link of openLab) expect(link).toHaveAttribute("href", "/lab");
    for (const link of screen.getAllByRole("link", { name: "Solve a mystery" })) {
      expect(link).toHaveAttribute("href", "/lab#investigate");
    }
    const modes = screen.getByRole("heading", { level: 2, name: /Explore, learn/ }).closest("section")!;
    const hrefs = within(modes)
      .getAllByRole("link")
      .map((a) => a.getAttribute("href"));
    expect(hrefs).toEqual(["/lab", "/lab#learn", "/lab#investigate"]);
    expect(screen.getAllByRole("link", { name: "GitHub" })[0]).toHaveAttribute("href", REPO_URL);
  });

  it("labels its figures as analytical and makes no unmeasured claims", () => {
    const { container } = render(<Landing />);
    expect(screen.getAllByText("Analytical")).toHaveLength(2);
    expect(container).toHaveTextContent(/calculated, not measured/i);
    expect(container).toHaveTextContent("Simulated circuits, not physical measurements.");
    // No accuracy percentages, testimonials or social proof.
    expect(container.textContent).not.toMatch(/%|testimonial|trusted by/i);
    // The values behind the second figure are available as a table.
    expect(screen.getByRole("table", { name: /R1 doubled vs C1 doubled/ })).toBeInTheDocument();
  });

  it("animates only for viewers who have not asked for reduced motion", () => {
    const css = landingCss.replace(/\/\*[\s\S]*?\*\//g, "");
    const block = "@media (prefers-reduced-motion: no-preference)";
    const start = css.indexOf(block);
    expect(start).toBeGreaterThan(-1);
    // The guarded block ends at the first line that closes it at column 0.
    const end = css.indexOf("\n}\n", start);
    const outside = css.slice(0, start) + css.slice(end);
    expect(outside).not.toMatch(/animation\s*:/);
    expect(css.slice(start, end)).toMatch(/animation\s*:/);
  });
});
