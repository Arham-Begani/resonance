// The one workbench shell: mode rail, header, circuit + instruments, lab-notes panel, and the
// playback/command strip. Narrow screens use accessible tabs that preserve the full workflow.
import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Button, Callout, ErrorCallout } from "../components/ui/primitives";
import { useMediaQuery } from "../lib/hooks";
import { focusWhenReady } from "../lib/focus";
import { currentRevision, revisionOrdinal, useWorkbench } from "../state/workbench";
import { useAgentEvents } from "../features/agent/agentRuns";
import { ExploreInstruments } from "../features/explore/ExploreInstruments";
import { ExplorePanel } from "../features/explore/ExplorePanel";
import { CaseInstruments } from "../features/investigate/CaseInstruments";
import { InvestigatePanel } from "../features/investigate/InvestigatePanel";
import { useCase } from "../features/investigate/caseStore";
import { LearnInstruments } from "../features/learn/LearnInstruments";
import { LearnPanel } from "../features/learn/LearnPanel";
import { CircuitArea } from "./CircuitArea";
import { CommandStrip } from "./CommandStrip";
import { HeaderStrip } from "./HeaderStrip";
import { ModeRail } from "./ModeRail";
import "./layout.css";

type TabId = "circuit" | "instruments" | "notes";
const TABS: { id: TabId; label: string }[] = [
  { id: "circuit", label: "Circuit" },
  { id: "instruments", label: "Instruments" },
  { id: "notes", label: "Lab notes" },
];

/** Wires explore agent events into the workbench store (proposals, measured experiments). */
function ExploreAgentBridge() {
  const wb = useWorkbench();
  useAgentEvents((scope, event) => {
    if (scope !== "explore") return;
    if (event.type === "proposal") wb.addProposal(event.proposal);
    if (event.type === "tool_result" && event.ok && event.experiment_id) void wb.showExperiment(event.experiment_id);
  });
  return null;
}

/** The backend message may already start with the title; avoid saying it twice. */
export function aiSetupText(message: string): string {
  return message.replace(/^AI lab partner unavailable:\s*/i, "");
}

function AiNotice() {
  const { state } = useWorkbench();
  const [dismissed, setDismissed] = useState(false);
  const ai = state.health?.ai;
  if (!ai || ai.available) return null;
  const title = ai.state === "dev_fixture" ? "Development fixture mode" : "AI lab partner unavailable";
  if (dismissed) {
    // Collapsed, not removed: the strip points here for the setup steps.
    return (
      <details className="disclosure ai-setup">
        <summary id="ai-setup-summary">{title} · setup steps</summary>
        <p className="panel-card__note">{aiSetupText(ai.message)}</p>
      </details>
    );
  }
  return (
    <Callout
      tone="warning"
      title={title}
      action={
        <Button
          size="sm"
          variant="ghost"
          onClick={() => {
            setDismissed(true);
            focusWhenReady("ai-setup-summary");
          }}
        >
          Dismiss
        </Button>
      }
    >
      {aiSetupText(ai.message)}
    </Callout>
  );
}

function SimulatorNotice() {
  const { state } = useWorkbench();
  const sim = state.health?.simulator;
  if (!sim || sim.available) return null;
  return (
    <Callout tone="danger" title="Simulator unavailable">
      {sim.message} Measurements can’t run until it is available; previous results stay visible.
    </Callout>
  );
}

function NarrowTabs({ active, onChange }: { active: TabId; onChange: (id: TabId) => void }) {
  const refs = useRef<Record<TabId, HTMLButtonElement | null>>({ circuit: null, instruments: null, notes: null });
  const onKeyDown = (event: KeyboardEvent, index: number) => {
    let next = index;
    if (event.key === "ArrowRight") next = (index + 1) % TABS.length;
    else if (event.key === "ArrowLeft") next = (index - 1 + TABS.length) % TABS.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = TABS.length - 1;
    else return;
    event.preventDefault();
    const tab = TABS[next]!;
    onChange(tab.id);
    refs.current[tab.id]?.focus();
  };
  return (
    <div className="tabs" role="tablist" aria-label="Workbench sections">
      {TABS.map((t, i) => (
        <button
          key={t.id}
          ref={(el) => {
            refs.current[t.id] = el;
          }}
          type="button"
          role="tab"
          id={`tab-${t.id}`}
          aria-selected={active === t.id}
          aria-controls={`panel-${t.id}`}
          tabIndex={active === t.id ? 0 : -1}
          className={`tabs__tab${active === t.id ? " is-active" : ""}`}
          onClick={() => onChange(t.id)}
          onKeyDown={(e) => onKeyDown(e, i)}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}

function TabPanel({ id, narrow, active, children, className, as = "div", label }: { id: TabId; narrow: boolean; active: TabId; children: ReactNode; className: string; as?: "div" | "aside"; label?: string }) {
  const Tag = as;
  if (!narrow) {
    return (
      <Tag className={className} aria-label={label}>
        {children}
      </Tag>
    );
  }
  return (
    <Tag className={className} role="tabpanel" id={`panel-${id}`} aria-labelledby={`tab-${id}`} hidden={active !== id} aria-label={undefined}>
      {children}
    </Tag>
  );
}

export function Shell() {
  const wb = useWorkbench();
  const { state } = wb;
  const cs = useCase();
  const narrow = useMediaQuery("(max-width: 899px)");
  const [tab, setTab] = useState<TabId>("circuit");
  const explore = state.mode === "explore";
  const investigate = state.mode === "investigate";
  const revision = !investigate ? currentRevision(state) : (cs.current?.intended_revision ?? currentRevision(state));
  const ordinal = revisionOrdinal(state, revision?.id);
  const revisionText = !investigate ? (ordinal ? `Revision ${ordinal}` : null) : cs.current ? "Intended design · frozen" : null;
  const mainLabel = explore ? "Explore workbench" : investigate ? "Investigation workbench" : "Learn workbench";
  const caseOpen = Boolean(cs.current && cs.current.status !== "closed");

  // Moving between modes keeps focus predictable: send it to the main heading region.
  const mainRef = useRef<HTMLElement>(null);
  const previousMode = useRef(state.mode);
  useEffect(() => {
    if (previousMode.current === state.mode) return;
    previousMode.current = state.mode;
    mainRef.current?.focus();
  }, [state.mode]);

  return (
    <div className={`app${narrow ? " app--narrow" : ""}`}>
      <a className="skip-link" href="#workbench-main">
        Skip to workbench
      </a>
      <ExploreAgentBridge />
      <ModeRail mode={state.mode} onChange={wb.setMode} caseOpen={caseOpen} />
      <div className="app__main">
        <HeaderStrip
          revision={revision}
          revisionText={revisionText}
          goalTitle={explore ? (state.goal?.title ?? null) : null}
          ai={state.health?.ai}
          sim={state.health?.simulator}
          healthError={Boolean(state.healthError && !state.health)}
        />
        {narrow ? <NarrowTabs active={tab} onChange={setTab} /> : null}
        <div className="workspace">
          <main id="workbench-main" ref={mainRef} tabIndex={-1} hidden={narrow && tab === "notes"} className="workspace__center" aria-label={mainLabel}>
            {state.status === "error" && state.error ? (
              <ErrorCallout error={state.error.error} onRetry={() => void wb.reload()} retryLabel="Reload workbench" />
            ) : null}
            <TabPanel id="circuit" narrow={narrow} active={tab} className="workspace__section">
              <CircuitArea />
            </TabPanel>
            <TabPanel id="instruments" narrow={narrow} active={tab} className="workspace__section">
              <SimulatorNotice />
              {explore ? <ExploreInstruments /> : investigate ? <CaseInstruments /> : <LearnInstruments />}
            </TabPanel>
          </main>
          <TabPanel id="notes" narrow={narrow} active={tab} className="workspace__panel" as="aside" label="Lab notes">
            <AiNotice />
            {explore ? <ExplorePanel /> : investigate ? <InvestigatePanel /> : <LearnPanel />}
          </TabPanel>
        </div>
        <CommandStrip />
      </div>
    </div>
  );
}
