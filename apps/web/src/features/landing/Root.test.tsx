// Routing between the landing page and the workbench. The workbench module is replaced with a
// stub so these tests never touch the API; the stub also records whether it was ever loaded.
import { render, screen } from "@testing-library/react";

const loaded = vi.hoisted(() => ({ app: 0 }));
vi.mock("../../App", () => {
  loaded.app += 1;
  return { App: () => <p>Workbench stub</p> };
});

const { Root, routeFor, TITLES } = await import("../../Root");

afterEach(() => {
  window.history.replaceState(null, "", "/");
});

describe("routeFor", () => {
  it.each([
    ["/", "landing"],
    ["", "landing"],
    ["/index.html", "landing"],
    ["/lab", "lab"],
    ["/lab/", "lab"],
    ["/labs", "not-found"],
    ["/lab/extra", "not-found"],
    ["/api/health", "not-found"],
  ])("%s → %s", (path, route) => {
    expect(routeFor(path)).toBe(route);
  });
});

describe("Root", () => {
  it("renders the landing page at / without loading the workbench or calling the API", () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    render(<Root pathname="/" />);
    expect(screen.getByRole("heading", { level: 1, name: /Hear what a circuit does/ })).toBeInTheDocument();
    expect(screen.queryByText("Workbench stub")).not.toBeInTheDocument();
    expect(loaded.app).toBe(0);
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(document.title).toBe(TITLES.landing);
    fetchSpy.mockRestore();
  });

  it("lazily loads the workbench at /lab behind an accessible loading state", async () => {
    render(<Root pathname="/lab/" />);
    expect(screen.getByRole("status")).toHaveTextContent("Opening the lab");
    expect(await screen.findByText("Workbench stub")).toBeInTheDocument();
    expect(loaded.app).toBe(1);
    expect(document.title).toBe(TITLES.lab);
    expect(screen.queryByRole("heading", { level: 1, name: /Hear what a circuit does/ })).not.toBeInTheDocument();
  });

  it("keeps the mode hash on /lab#investigate for the workbench to read", async () => {
    window.history.replaceState(null, "", "/lab#investigate");
    render(<Root />);
    expect(await screen.findByText("Workbench stub")).toBeInTheDocument();
    expect(window.location.pathname).toBe("/lab");
    expect(window.location.hash).toBe("#investigate");
  });

  it("shows a not-found page with links home and to the lab for unknown paths", () => {
    render(<Root pathname="/nowhere" />);
    expect(screen.getByRole("heading", { level: 1, name: "Page not found" })).toBeInTheDocument();
    expect(screen.getByText("/nowhere")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open the lab" })).toHaveAttribute("href", "/lab");
    expect(screen.getByRole("link", { name: "Go to the home page" })).toHaveAttribute("href", "/");
    expect(document.title).toBe(TITLES["not-found"]);
  });
});
