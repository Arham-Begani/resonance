// Explore "lab notes" panel: goal, AI results and proposal previews, and experiment history.
import type { EditProposal } from "../../api/types";
import { Badge, Button, Callout } from "../../components/ui/primitives";
import { capitalize, formatDb, formatHz } from "../../lib/format";
import { FAMILY_LABEL } from "../../lib/family";
import { FOCUS, focusWhenReady } from "../../lib/focus";
import { experimentOrdinal, revisionOrdinal, shownRecord, useWorkbench } from "../../state/workbench";
import { AgentRunLog } from "../agent/AgentRunLog";
import { useAgentRuns } from "../agent/agentRuns";
import { useWorkbenchResolver } from "../agent/useWorkbenchResolver";
import { GoalCard } from "./GoalCard";
import { HistoryPanel } from "./HistoryPanel";
import { SuggestPanel } from "./SuggestPanel";
import "./explore.css";

function ProposalCard({ proposal }: { proposal: EditProposal }) {
  const wb = useWorkbench();
  const baseOrdinal = revisionOrdinal(wb.state, proposal.base_revision_id);
  const base = wb.state.revisions.find((r) => r.id === proposal.base_revision_id);
  const switchesTo = proposal.template_id && proposal.template_id !== base?.template_id ? proposal.template_id : null;
  const busy = wb.state.pending === "proposal";
  return (
    <article className="proposal" aria-label="Proposed change">
      <div className="panel-card__header">
        <h3 className="panel-card__title">Proposed change</h3>
        <Badge tone="prediction" icon={null}>
          Preview
        </Badge>
      </div>
      {switchesTo ? (
        <p className="proposal__switch">
          Switches the circuit to the <strong>{FAMILY_LABEL[switchesTo].toLowerCase()}</strong> family. Your goal is kept if that family can meet it;
          otherwise you will be told which goal applies.
        </p>
      ) : null}
      <ul className="proposal__changes num">
        {proposal.changes.map((c) => (
          <li key={c.component_id}>
            {c.component_id}: {c.from_display} → <strong>{c.to_display}</strong>
          </li>
        ))}
      </ul>
      <p className="proposal__rationale">{proposal.rationale}</p>
      {proposal.predicted.length ? (
        <div className="proposal__prediction">
          <p className="eyebrow">Prediction (analytical, not measured)</p>
          <ul className="num">
            {proposal.predicted.map((p) => (
              <li key={p.frequency_hz}>
                {p.label} · {formatHz(p.frequency_hz)}: {formatDb(p.predicted_gain_db)}
              </li>
            ))}
          </ul>
          {proposal.predicted_goal_pass != null ? (
            <p className="panel-card__note">Predicted goal result: {proposal.predicted_goal_pass ? "meets the goal" : "does not meet the goal"}.</p>
          ) : null}
        </div>
      ) : null}
      <p className="panel-card__note">Based on Revision {baseOrdinal ?? "?"}. Applying creates a new revision; nothing is measured until you run it.</p>
      <div className="row">
        <Button variant="primary" size="sm" loading={busy} onClick={() => void wb.applyProposal(proposal).then((ok) => ok && focusWhenReady(FOCUS.runExperiment))}>
          Apply
        </Button>
        <Button size="sm" disabled={busy} onClick={() => void wb.dismissProposal(proposal.id).then(() => focusWhenReady(FOCUS.runExperiment))}>
          Dismiss
        </Button>
      </div>
    </article>
  );
}

export function ExplorePanel() {
  const wb = useWorkbench();
  const { state } = wb;
  const runs = useAgentRuns();
  const record = shownRecord(state);
  const label = record ? `Experiment ${experimentOrdinal(state, record.id) ?? ""}` : null;
  const view = runs.views.explore;
  const proposalError = state.error?.op === "proposal" ? state.error.error : null;
  const resolver = useWorkbenchResolver();

  return (
    <div className="panel-stack">
      {/* Live work first: an AI run and the changes it proposes need the learner's attention. */}
      <AgentRunLog
        title="AI lab partner"
        view={view}
        resolver={resolver}
        onCancel={() => void runs.cancel("explore")}
        onDismiss={() => runs.clear("explore")}
      />

      {proposalError ? (
        <Callout tone="danger" title="The proposal could not be applied">
          {proposalError.message}
        </Callout>
      ) : null}
      {state.proposals.map((p) => (
        <ProposalCard key={p.id} proposal={p} />
      ))}

      <GoalCard
        goal={state.goal}
        record={record}
        experimentLabel={label}
        saving={state.pending === "goal"}
        error={state.error?.op === "goal" ? state.error.error : null}
        onSave={wb.setGoal}
        notice={state.goalNotice}
        onDismissNotice={wb.dismissGoalNotice}
      />

      <HistoryPanel />

      <DesignHelp />
    </div>
  );
}

/** The analytical feasibility of the goal and standard-part suggestions, both predictions. */
function DesignHelp() {
  const { state } = useWorkbench();
  const feasibility = state.feasibility;
  if (!state.goal) return null;
  return (
    <section className="panel-card" aria-labelledby="design-help-heading">
      <div className="panel-card__header">
        <h2 id="design-help-heading" className="panel-card__title">
          Design help
        </h2>
        <Badge tone="prediction" icon={null}>
          Analytical
        </Badge>
      </div>
      {feasibility ? (
        <Callout tone={feasibility.feasible ? "neutral" : "warning"} title="Prediction">
          {capitalize(feasibility.explanation.replace(/^Prediction:\s*/i, ""))}
        </Callout>
      ) : null}
      <SuggestPanel />
    </section>
  );
}
