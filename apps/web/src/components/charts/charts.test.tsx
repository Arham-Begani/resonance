import { fireEvent, render, screen, within } from "@testing-library/react";
import { AnnouncerProvider } from "../../state/announcer";
import { grid } from "../../test/fixtures";
import { ResponseChart } from "./ResponseChart";
import { crossing, decimateByPixel, logTicks } from "./scales";

function renderChart(state: "ready" | "empty" | "loading" = "ready") {
  const f = grid(20, 20000, 10);
  const expected = f.map((x) => -10 * Math.log10(1 + (x / 1591.5) ** 2));
  const observed = f.map((x) => -10 * Math.log10(1 + (x / 795.7) ** 2));
  return render(
    <AnnouncerProvider>
      <ResponseChart
        title="Output voltage sweep"
        frequency={f}
        panels={[
          {
            id: "gain",
            title: "Output gain (dB)",
            series: [
              { id: "exp", label: "Expected", kind: "reference", y: expected },
              { id: "obs", label: "Observed", kind: "primary", y: observed },
            ],
            height: 160,
            valueFormat: (v) => `${v.toFixed(2)} dB`,
            tickFormat: (v) => v.toFixed(0),
          },
        ]}
        summary="Observed gain is lower than expected above 300 Hz."
        state={state}
        emptyText="No measurement yet."
        tableCaption="Observed and expected gain"
      />
    </AnnouncerProvider>,
  );
}

describe("ResponseChart", () => {
  it("has a caption summary, a labelled legend, and a complete data table", () => {
    renderChart();
    const figure = screen.getByRole("figure", { name: "Output voltage sweep" });
    expect(within(figure).getByText("Observed gain is lower than expected above 300 Hz.")).toBeInTheDocument();
    const legend = within(figure).getByRole("list", { name: /legend/ });
    expect(within(legend).getByText("Expected")).toBeInTheDocument();
    expect(within(legend).getByText("Observed")).toBeInTheDocument();
    // Line style distinguishes the series, not only color.
    const traces = figure.querySelectorAll("path.trace");
    expect(traces[0]).toHaveAttribute("stroke-dasharray", "6 4");
    expect(traces[1]).not.toHaveAttribute("stroke-dasharray");
    const table = within(figure).getByRole("table", { name: "Observed and expected gain" });
    // 31 frequency rows + header row.
    expect(within(table).getAllByRole("row")).toHaveLength(32);
    expect(within(table).getByRole("rowheader", { name: "20 kHz" })).toBeInTheDocument();
  });

  it("lets keyboard users step through values", () => {
    renderChart();
    const plot = screen.getByRole("img", { name: /Output gain \(dB\) versus frequency/ });
    plot.focus();
    fireEvent.keyDown(plot, { key: "Home" });
    expect(screen.getByText(/^20 Hz · Output gain: Expected/)).toBeInTheDocument();
    fireEvent.keyDown(plot, { key: "End" });
    expect(screen.getByText(/^20 kHz · Output gain: Expected/)).toBeInTheDocument();
  });

  it("shows the empty state instead of an empty plot", () => {
    renderChart("empty");
    expect(screen.getByText("No measurement yet.")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });
});

describe("chart helpers", () => {
  it("labels decades on a log axis", () => {
    expect(logTicks([20, 20000]).major).toEqual([100, 1000, 10000]);
  });

  it("finds the −3 dB crossing on a measured curve", () => {
    const f = grid(10, 100000, 40);
    const g = f.map((x) => -10 * Math.log10(1 + (x / 1591.5) ** 2));
    expect(crossing(f, g, -3)).toBeGreaterThan(1500);
    expect(crossing(f, g, -3)).toBeLessThan(1700);
  });

  it("decimation keeps each series' peak per pixel", () => {
    const xs = Array.from({ length: 1000 }, (_, i) => i);
    const ys = xs.map((i) => (i === 503 ? 10 : 0));
    const kept = decimateByPixel(xs, [ys], (x) => x / 10, 50);
    expect(kept).toContain(503);
    expect(kept.length).toBeLessThanOrEqual(101);
  });
});
