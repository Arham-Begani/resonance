// Renders a run's genuine events as concise evidence cards: tool requests paired with their
// results, visible assistant messages with citation chips, errors, and measured usage.
// Never shows hidden reasoning, fake progress, or invented messages.
import { useEffect, useRef, type ReactNode } from "react";
import type { AgentEvent, AgentRun, EvidenceRef } from "../../api/types";
import { Badge, Button, Callout } from "../../components/ui/primitives";
import { Icon, type IconName } from "../../components/ui/Icon";
import { formatDurationMs, formatHz } from "../../lib/format";
import type { RunView } from "./agentRuns";
import { Markdown } from "./Markdown";
import "./agent.css";

export interface CitationResolver {
  /** Short label for an observation or experiment ID, e.g. "Obs 2" or "Experiment 3". */
  label: (resultId: string) => string | null;
  onSelect?: (resultId: string) => void;
  /** Which labelled IDs onSelect can open (default: all). Others render as plain chips. */
  canSelect?: (resultId: string) => boolean;
}

const UNIT_SUFFIX = /_(db|dbv|ma|deg|v|hz|s|ms|fs|si)$/;

export function humanizeQuantity(quantity: string): string {
  return quantity.replace(UNIT_SUFFIX, "").replace(/_/g, " ");
}

export function humanize(name: string): string {
  const text = name.replace(/_/g, " ").trim();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

export function citationText(ref: EvidenceRef, resolver?: CitationResolver): string {
  const base = resolver?.label(ref.result_id) ?? "Result";
  const at = ref.frequency_hz != null ? ` @ ${formatHz(ref.frequency_hz)}` : "";
  return `${base} · ${humanizeQuantity(ref.quantity)}${at}`;
}

export function CitationChip({ refItem, resolver }: { refItem: EvidenceRef; resolver?: CitationResolver }) {
  const text = citationText(refItem, resolver);
  if (resolver?.onSelect && resolver.label(refItem.result_id) && (resolver.canSelect?.(refItem.result_id) ?? true)) {
    return (
      <button type="button" className="citation" onClick={() => resolver.onSelect?.(refItem.result_id)}>
        {text}
      </button>
    );
  }
  return <span className="citation">{text}</span>;
}

/** Readable names for every tool the explore, investigate, and hint runs can call. */
export const TOOL_LABEL: Record<string, string> = {
  inspect_circuit: "Inspect circuit",
  set_goal: "Set goal",
  propose_values: "Propose values",
  suggest_design: "Suggest standard parts (prediction)",
  run_experiment: "Run experiment",
  compare_experiments: "Compare experiments",
  read_learn_attempt: "Read Learn attempt",
  get_case: "Read case",
  assess_evidence: "Assess evidence",
  read_observation: "Read observation",
  submit_diagnosis: "Submit diagnosis",
  propose_repair: "Propose repair",
};

export function toolLabel(tool: string): string {
  return TOOL_LABEL[tool] ?? humanize(tool);
}

const ID_PATTERN = /(?<![A-Za-z0-9_-])(rev|exp|art|cmp|goal|case|obs|dx|rep|prop|run|learn)_[A-Za-z0-9_-]{6,16}/g;
const ID_NOUN: Record<string, string> = {
  rev: "a revision",
  exp: "an experiment",
  obs: "an observation",
  learn: "a Learn attempt",
  case: "the case",
  prop: "a proposal",
  goal: "the goal",
  dx: "the diagnosis",
  rep: "the repair",
};

/** Learners never see raw result IDs: cited ones get their workbench label, others a plain noun. */
export function readableIdText(text: string, resolver?: CitationResolver): string {
  return text.replace(ID_PATTERN, (id, kind: string) => resolver?.label(id) ?? ID_NOUN[kind] ?? "a result");
}

const ARG_PREVIEW = 48;

export function describeArgs(args: Record<string, unknown>, resolver?: CitationResolver): string {
  return Object.entries(args)
    .filter(([, v]) => v !== null && v !== undefined && (typeof v === "string" || typeof v === "number" || typeof v === "boolean"))
    .map(([k, v]): [string, string | null] => {
      if (typeof v !== "string") return [k, String(v)];
      ID_PATTERN.lastIndex = 0;
      if (ID_PATTERN.test(v)) return [k, resolver?.label(v) ?? null]; // never print a raw ID
      if (/^[A-Z][0-9]+$/.test(v)) return [k, v]; // component IDs keep their case (R1, C1)
      const text = humanize(v).toLowerCase();
      // Free-text arguments (an explanation, a rationale) are shown in full where they land
      // (the diagnosis or repair card); the feed keeps a short preview.
      return [k, text.length > ARG_PREVIEW ? `${text.slice(0, ARG_PREVIEW - 1).trimEnd()}…` : text];
    })
    .filter((entry): entry is [string, string] => entry[1] !== null)
    .slice(0, 3)
    .map(([k, v]) => `${humanize(k.replace(/_id$/, "")).toLowerCase()}: ${v}`)
    .join(", ");
}

const STATUS_LABEL: Record<AgentRun["status"], { text: string; tone: "accent" | "success" | "warning" | "danger" | "neutral" }> = {
  running: { text: "Running", tone: "accent" },
  completed: { text: "Finished", tone: "success" },
  cancelled: { text: "Cancelled", tone: "neutral" },
  limit_reached: { text: "Limit reached", tone: "warning" },
  failed: { text: "Failed", tone: "danger" },
};

type Item =
  | { kind: "tool"; call: Extract<AgentEvent, { type: "tool_call" }> | null; result: Extract<AgentEvent, { type: "tool_result" }> | null; seq: number }
  | { kind: "event"; event: AgentEvent; seq: number };

export function groupEvents(events: AgentEvent[]): Item[] {
  const items: Item[] = [];
  const byCall = new Map<string, Extract<Item, { kind: "tool" }>>();
  for (const e of events) {
    if (e.type === "tool_call") {
      const item: Extract<Item, { kind: "tool" }> = { kind: "tool", call: e, result: null, seq: e.seq };
      byCall.set(e.call_id, item);
      items.push(item);
    } else if (e.type === "tool_result") {
      const existing = byCall.get(e.call_id);
      if (existing) existing.result = e;
      else items.push({ kind: "tool", call: null, result: e, seq: e.seq });
    } else if (e.type === "message" || e.type === "proposal" || e.type === "diagnosis" || e.type === "repair_proposal" || e.type === "error") {
      items.push({ kind: "event", event: e, seq: e.seq });
    }
  }
  return items;
}

function itemKind(item: Item): string {
  if (item.kind === "tool") return item.result ? (item.result.ok ? "tool" : "tool-failed") : "tool-pending";
  return item.event.type === "message" ? "message" : item.event.type === "error" ? "error" : "event";
}

interface AgentRunLogProps {
  title: string;
  /** ID for the heading so callers can move focus to the run after starting it. */
  headingId?: string;
  view: RunView;
  resolver?: CitationResolver;
  onCancel?: () => void;
  onDismiss?: () => void;
}

export function AgentRunLog({ title, headingId, view, resolver, onCancel, onDismiss }: AgentRunLogProps) {
  const { run, events, connection, startError, cancelling } = view;
  const sectionRef = useRef<HTMLElement>(null);
  // A run started from the command strip appears in the lab notes: scroll it into view (focus is
  // not moved; the strip keeps it, and status is announced politely elsewhere).
  useEffect(() => {
    if (connection === "starting" || startError) sectionRef.current?.scrollIntoView?.({ block: "nearest" });
  }, [connection, startError]);
  if (startError) {
    return (
      <section ref={sectionRef} className="agent-log" aria-label={title}>
        {headingId ? (
          <h3 id={headingId} tabIndex={-1} className="visually-hidden">
            {title}
          </h3>
        ) : null}
        <Callout
          tone={startError.code === "ai_unavailable" ? "warning" : "danger"}
          title={startError.code === "ai_unavailable" ? "AI lab partner unavailable" : startError.code === "run_in_progress" ? "The AI is already working on this" : "The AI run could not start"}
          action={onDismiss ? <Button size="sm" onClick={onDismiss}>Dismiss</Button> : undefined}
        >
          {startError.message} Manual tools keep working.
        </Callout>
      </section>
    );
  }
  if (!run && connection !== "starting") return null;

  const running = connection === "starting" || run?.status === "running";
  const latestStatus = [...events].reverse().find((e) => e.type === "status");
  const items = groupEvents(events);
  const hasMessage = events.some((e) => e.type === "message");
  const status = run ? STATUS_LABEL[run.status] : STATUS_LABEL.running;

  return (
    <section ref={sectionRef} className="agent-log" aria-label={title}>
      <div className="agent-log__header">
        <Icon name="ai" />
        <h3 className="agent-log__title" id={headingId} tabIndex={headingId ? -1 : undefined}>
          {title}
        </h3>
        <Badge tone={status.tone} icon={null}>
          {connection === "starting" ? "Starting" : status.text}
        </Badge>
        {running && onCancel ? (
          <Button size="sm" variant="danger" onClick={onCancel} loading={cancelling} disabled={!run} className="agent-log__cancel">
            Cancel
          </Button>
        ) : null}
        {!running && onDismiss ? (
          <Button size="sm" variant="ghost" onClick={onDismiss} className="agent-log__cancel">
            Dismiss
          </Button>
        ) : null}
      </div>
      {run && run.source !== "nebius" ? (
        <Callout tone="warning" title="Development fixture">
          This run uses scripted test data, not AI inference.
        </Callout>
      ) : null}
      {run?.model ? (
        <p className="agent-log__meta">
          {run.source === "nebius" ? "Nemotron via Nebius Token Factory · " : ""}Model: <span className="mono">{run.model}</span>
        </p>
      ) : null}

      {items.length > 0 || running ? (
        <ol className="agent-log__items">
          {items.map((item) => (
            <li key={item.seq} className={`agent-item agent-item--${itemKind(item)}`}>
              {item.kind === "tool" ? <ToolItem call={item.call} result={item.result} resolver={resolver} /> : <EventItem event={item.event} resolver={resolver} />}
            </li>
          ))}
          {running ? (
            <li className="agent-item agent-item--status">
              <span className="agent-item__marker" aria-hidden="true">
                <span className="spinner" />
              </span>
              <p className="agent-log__status">
                {latestStatus && latestStatus.type === "status" ? latestStatus.text : connection === "starting" ? "Starting the run…" : "Working…"}
              </p>
            </li>
          ) : null}
        </ol>
      ) : null}

      {run && !running && run.final_message && !hasMessage ? <Markdown className="agent-log__final" text={readableIdText(run.final_message, resolver)} /> : null}
      {run && run.status === "failed" && run.error ? (
        <Callout tone="danger" title="The AI run failed">
          {run.error.message} Manual tools keep working.
        </Callout>
      ) : null}
      {run && run.status === "limit_reached" ? (
        <Callout tone="warning" title="Stopped at a declared limit">
          {run.error?.message ?? "The run reached one of its caps (model calls, tool calls, tokens, or time)."}
        </Callout>
      ) : null}

      {run && run.usage.model_calls > 0 ? (
        <p className="agent-log__usage num">
          Measured: {run.usage.model_calls} model call{run.usage.model_calls === 1 ? "" : "s"} · {run.usage.tool_calls} tool call
          {run.usage.tool_calls === 1 ? "" : "s"} · {run.usage.total_tokens.toLocaleString()} tokens · {formatDurationMs(run.usage.latency_ms)}
        </p>
      ) : null}
      {connection === "polling" && running ? <p className="agent-log__meta">Live stream unavailable; refreshing from the event log.</p> : null}
    </section>
  );
}

function Marker({ children }: { children: ReactNode }) {
  return (
    <span className="agent-item__marker" aria-hidden="true">
      {children}
    </span>
  );
}

function ToolItem({
  call,
  result,
  resolver,
}: {
  call: Extract<AgentEvent, { type: "tool_call" }> | null;
  result: Extract<AgentEvent, { type: "tool_result" }> | null;
  resolver?: CitationResolver;
}) {
  const name = toolLabel(call?.tool ?? result?.tool ?? "tool");
  const args = call ? describeArgs(call.arguments, resolver) : "";
  const resultId = result?.observation_id ?? result?.experiment_id ?? null;
  return (
    <>
      <Marker>{result ? <Icon name={result.ok ? "check" : "x"} size={12} /> : <span className="spinner" />}</Marker>
      <div className="agent-item__body">
        <p className="agent-item__head">
          <strong className="agent-item__name">{name}</strong>
          {args ? <span className="agent-item__args"> ({args})</span> : null}
          <span className="visually-hidden">{result ? (result.ok ? "succeeded" : "failed") : "in progress"}</span>
        </p>
        {result ? (
          <p className="agent-item__text">
            {readableIdText(result.ok ? result.summary : (result.error?.message ?? result.summary), resolver)}
            {resultId && resolver?.label(resultId) ? (
              <>
                {" "}
                {resolver.onSelect ? (
                  <button type="button" className="citation" onClick={() => resolver.onSelect?.(resultId)}>
                    View {resolver.label(resultId)}
                  </button>
                ) : (
                  <span className="citation">{resolver.label(resultId)}</span>
                )}
              </>
            ) : null}
          </p>
        ) : null}
      </div>
    </>
  );
}

function EventLine({ icon, children }: { icon: IconName; children: ReactNode }) {
  return (
    <>
      <Marker>
        <Icon name={icon} size={12} />
      </Marker>
      <p className="agent-item__body agent-item__head">{children}</p>
    </>
  );
}

function EventItem({ event, resolver }: { event: AgentEvent; resolver?: CitationResolver }) {
  switch (event.type) {
    case "message":
      return (
        <>
          <Marker>
            <Icon name="ai" size={12} />
          </Marker>
          <div className="agent-item__body agent-item__message">
            <Markdown className="agent-item__text" text={readableIdText(event.text, resolver)} />
            {event.citations && event.citations.length > 0 ? (
              <p className="agent-item__citations">
                {event.citations.map((c, i) => (
                  <CitationChip key={`${c.result_id}-${c.quantity}-${i}`} refItem={c} resolver={resolver} />
                ))}
              </p>
            ) : null}
          </div>
        </>
      );
    case "proposal": {
      const changes = event.proposal.changes.map((c) => `${c.component_id} ${c.from_display} → ${c.to_display}`).join(", ");
      return (
        <EventLine icon="edit">
          Proposed a change: <span className="num">{changes}</span> (preview below; nothing is applied until you choose Apply)
        </EventLine>
      );
    }
    case "diagnosis":
      return <EventLine icon="check">Submitted a diagnosis (shown in the case panel).</EventLine>;
    case "repair_proposal":
      return <EventLine icon="edit">Proposed a repair (shown in the case panel).</EventLine>;
    case "error":
      return <EventLine icon="alert">{event.error.message}</EventLine>;
    default:
      return null;
  }
}
