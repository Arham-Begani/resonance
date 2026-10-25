import { render, screen } from "@testing-library/react";
import { ModeRail } from "./ModeRail";

describe("ModeRail", () => {
  it("links the brand mark to the landing page without assuming where the workbench lives", () => {
    render(<ModeRail mode="explore" onChange={() => undefined} caseOpen={false} />);
    const nav = screen.getByRole("navigation", { name: "Workbench mode" });
    expect(screen.getByRole("link", { name: "Resonance home" })).toHaveAttribute("href", "/");
    expect(nav).toContainElement(screen.getByRole("link", { name: "Resonance home" }));
    expect(screen.getByRole("button", { name: "Explore" })).toHaveAttribute("aria-current", "page");
  });
});
