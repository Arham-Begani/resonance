// Figure shell shared by every chart: title, line-key legend, crosshair readout, the plot,
// a caption with the key measured numbers, and a "Data table" disclosure. Handles the
// loading / empty / error states and holds a stale render at reduced opacity while refreshing.
import { useId, type ReactNode } from "react";
import { useElementWidth } from "../../lib/hooks";
import type { SeriesKind } from "./LinePlot";
import "./charts.css";

export interface LegendItem {
  label: string;
  kind: SeriesKind;
}

export type ChartState = "ready" | "loading" | "empty" | "error";

interface ChartFigureProps {
  title: string;
  legend?: LegendItem[];
  readout?: string | null;
  summary?: ReactNode;
  table?: ReactNode;
  tableLabel?: string;
  state: ChartState;
  emptyText?: ReactNode;
  errorText?: ReactNode;
  stale?: boolean;
  toolbar?: ReactNode;
  xAxisLabel?: string;
  children: (width: number) => ReactNode;
}

export function LineKey({ kind }: { kind: SeriesKind }) {
  return (
    <svg width="24" height="8" aria-hidden="true" className="line-key">
      <line
        x1="1"
        x2="23"
        y1="4"
        y2="4"
        className={`trace trace--${kind}`}
        strokeDasharray={kind === "reference" ? "6 4" : kind === "muted" ? "2 3" : undefined}
      />
    </svg>
  );
}

export function ChartFigure({
  title,
  legend = [],
  readout,
  summary,
  table,
  tableLabel = "Data table",
  state,
  emptyText,
  errorText,
  stale = false,
  toolbar,
  xAxisLabel,
  children,
}: ChartFigureProps) {
  const id = useId();
  const [ref, width] = useElementWidth<HTMLDivElement>(520);
  return (
    <figure className="chart" aria-labelledby={`${id}-title`} aria-busy={state === "loading" || stale || undefined}>
      <div className="chart__header">
        <h3 className="chart__title" id={`${id}-title`}>
          {title}
        </h3>
        {toolbar}
        {legend.length > 1 && state === "ready" ? (
          <ul className="chart__legend" aria-label={`${title} legend`}>
            {legend.map((item) => (
              <li key={item.label}>
                <LineKey kind={item.kind} />
                {item.label}
              </li>
            ))}
          </ul>
        ) : null}
      </div>
      <div ref={ref} className={`chart__body${stale ? " is-stale" : ""}`}>
        {state === "ready" ? (
          <>
            <p className="chart__readout num" aria-hidden="true">
              {readout ?? " "}
            </p>
            {children(width)}
            {xAxisLabel ? <p className="chart__axis-label">{xAxisLabel}</p> : null}
          </>
        ) : state === "loading" ? (
          <div className="chart__placeholder">
            <div className="skeleton chart__skeleton" />
            <span className="visually-hidden">Loading {title}</span>
          </div>
        ) : state === "error" ? (
          <div className="chart__placeholder chart__placeholder--error">{errorText ?? "This measurement could not be loaded."}</div>
        ) : (
          <div className="chart__placeholder">{emptyText ?? "No measurement yet."}</div>
        )}
      </div>
      {state === "ready" && summary ? <figcaption className="chart__summary">{summary}</figcaption> : null}
      {state === "ready" && table ? (
        <details className="disclosure chart__table">
          <summary>{tableLabel}</summary>
          {/* Scrollable region: focusable so keyboard users can scroll it (WCAG 2.1.1). */}
          <div className="chart__table-scroll" tabIndex={0} role="region" aria-label={`${title} ${tableLabel.toLowerCase()}`}>
            {table}
          </div>
        </details>
      ) : null}
    </figure>
  );
}
