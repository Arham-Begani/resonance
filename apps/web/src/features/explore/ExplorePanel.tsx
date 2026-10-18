// Explore "lab notes" panel: goal, AI results and proposal previews, and experiment history.
import type { EditProposal } from "../../api/types";
import { Badge, Button, Callout } from "../../components/ui/primitives";
import { formatDb, formatHz } from "../../lib/format";
import { FOCUS, focusWhenReady } from "../../lib/focus";
import { experimentOrdinal, revisionOrdinal, shownRecord, useWorkbench } from "../../state/workbench";
import { AgentRunLog } from "../agent/AgentRunLog";
import { useAgentRuns } from "../agent/agentRuns";
import { GoalCard } from "./GoalCard";
import { HistoryPanel } from "./HistoryPanel";
import "./explore.css";

function ProposalCard({ proposal }: { proposal: EditProposal }) {
  const wb = useWorkbench();
  const baseOrdinal = revisionOrdinal(wb.state, proposal.base_revision_id);
  const busy = wb.state.pending === "proposal";
  return (
    <article className="proposal" aria-label="Proposed change">
      <div className="panel-card__header">
        <h3 className="panel-card__title">Proposed change</h3>
        <Badge tone="accent" icon={null}>
          Preview
        </Badge>
      </div>
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

  return (
    <div className="panel-stack">
      <GoalCard
        goal={state.goal}
        feasibility={state.feasibility}
        record={record}
        experimentLabel={label}
        saving={state.pending === "goal"}
        error={state.error?.op === "goal" ? state.error.error : null}
        onSave={wb.setGoal}
      />

      <AgentRunLog
        title="AI lab partner"
        view={view}
        resolver={{
          label: (id) => {
            const ord = experimentOrdinal(state, id);
            return ord ? `Experiment ${ord}` : null;
          },
          onSelect: (id) => void wb.showExperiment(id),
        }}
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

      <HistoryPanel />
    </div>
  );
}
