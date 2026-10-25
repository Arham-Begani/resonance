// "Suggest values for this goal": an analytical preview (labelled Prediction, never a
// measurement) of a standard part for R1 or C1. Applying creates an ordinary revision; the
// learner then runs the experiment to measure it.
import { useId, useState } from "react";
import { toFailure, type ApiFailure } from "../../api/client";
import { endpoints } from "../../api/endpoints";
import type { DesignSuggestion, Revision } from "../../api/types";
import { Badge, Button, Callout, ErrorCallout } from "../../components/ui/primitives";
import { formatDb, formatHz } from "../../lib/format";
import { FOCUS, focusWhenReady } from "../../lib/focus";
import { useAnnouncer } from "../../state/announcer";
import { currentRevision, revisionOrdinal, useWorkbench } from "../../state/workbench";

type Vary = "R1" | "C1";

export function SuggestionPreview({
  suggestion,
  revision,
  stale,
  applying,
  nextRevisionOrdinal,
  onApply,
  onDiscard,
  headingId,
}: {
  headingId?: string;
  suggestion: DesignSuggestion;
  revision: Revision | null;
  stale: string | null;
  applying: boolean;
  nextRevisionOrdinal: number;
  onApply: () => void;
  onDiscard: () => void;
}) {
  const ownId = useId();
  const titleId = headingId ?? `${ownId}-title`;
  const changed = suggestion.values.filter((v) => v.series !== "unchanged");
  const current = (cid: string) => revision?.components.find((c) => c.id === cid)?.display_value ?? "";
  const alreadyThere = changed.every((v) => revision?.components.find((c) => c.id === v.component_id)?.value_si === v.value_si);
  return (
    <article className="suggestion" aria-labelledby={titleId}>
      <div className="panel-card__header">
        <h4 id={titleId} tabIndex={-1} className="suggestion__title">
          Suggested values
        </h4>
        <Badge tone="accent" icon={null}>
          Prediction
        </Badge>
      </div>
      <p className="panel-card__note">Analytical first-order RC estimate, not a measurement.</p>
      <ul className="suggestion__values num">
        {suggestion.values.map((v) => (
          <li key={v.component_id}>
            <span className="suggestion__cid">{v.component_id}</span>
            {v.series === "unchanged" ? (
              <span className="muted">{v.display} · kept</span>
            ) : (
              <span>
                {current(v.component_id)} → <strong>{v.display}</strong> <span className="muted">· {v.series} standard part</span>
              </span>
            )}
          </li>
        ))}
      </ul>
      <dl className="kv num">
        <dt>Target cutoff</dt>
        <dd>{formatHz(suggestion.target_cutoff_hz)}</dd>
        <dt>Predicted cutoff</dt>
        <dd>{formatHz(suggestion.predicted_cutoff_hz)} with the standard part</dd>
      </dl>
      <table className="data-table">
        <caption className="visually-hidden">Predicted gain for each goal constraint (analytical, not measured)</caption>
        <thead>
          <tr>
            <th scope="col">Constraint</th>
            <th scope="col" className="n">
              Predicted gain
            </th>
          </tr>
        </thead>
        <tbody>
          {suggestion.predicted.map((p) => (
            <tr key={`${p.frequency_hz}-${p.constraint_label}`}>
              <th scope="row">
                {p.constraint_label} · <span className="num">{formatHz(p.frequency_hz)}</span>
              </th>
              <td className="n">
                <span className="suggestion__gain">{formatDb(p.predicted_gain_db)}</span>
                <Badge tone={p.passes ? "success" : "danger"}>{p.passes ? "Predicted pass" : "Predicted fail"}</Badge>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {suggestion.tradeoff ? (
        <Callout tone="warning" title={suggestion.feasible ? "Standard part misses a limit" : "Not achievable with this circuit"}>
          {suggestion.tradeoff}
        </Callout>
      ) : null}
      <p className="panel-card__note">{suggestion.explanation}</p>
      {stale ? (
        <p className="panel-card__note suggestion__stale">{stale}</p>
      ) : alreadyThere ? (
        <p className="panel-card__note">The circuit already has these values.</p>
      ) : (
        <p className="panel-card__note">Applying creates Revision {nextRevisionOrdinal}. Nothing is measured until you run it.</p>
      )}
      <div className="row">
        <Button variant="primary" size="sm" loading={applying} disabled={Boolean(stale) || alreadyThere} onClick={onApply}>
          Apply as Revision {nextRevisionOrdinal}
        </Button>
        <Button size="sm" variant="ghost" disabled={applying} onClick={onDiscard}>
          Discard
        </Button>
      </div>
    </article>
  );
}

export function SuggestPanel() {
  const id = useId();
  const wb = useWorkbench();
  const { state } = wb;
  const { announce } = useAnnouncer();
  const revision = currentRevision(state);
  const [vary, setVary] = useState<Vary>("C1");
  const [suggestion, setSuggestion] = useState<DesignSuggestion | null>(null);
  const [goalId, setGoalId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<ApiFailure | null>(null);
  if (!revision || !state.goal) return null;

  const suggest = async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await endpoints.suggestDesign({ revision_id: revision.id, vary });
      setSuggestion(result);
      setGoalId(state.goal?.id ?? null);
      announce(`Prediction ready: ${result.values.filter((v) => v.series !== "unchanged").map((v) => `${v.component_id} ${v.display}`).join(", ") || "no change"}. ${result.predicted_goal_pass ? "Predicted to meet the goal." : "Predicted not to meet every constraint."}`);
      focusWhenReady(`${id}-preview`);
    } catch (thrown) {
      setError(toFailure(thrown));
    } finally {
      setLoading(false);
    }
  };

  const ordinal = revisionOrdinal(state, revision.id);
  const stale =
    suggestion && suggestion.revision_id !== revision.id
      ? `Based on an earlier revision; the circuit is now Revision ${ordinal ?? ""}. Suggest again.`
      : suggestion && goalId !== state.goal.id
        ? "The goal changed since this prediction. Suggest again."
        : null;
  const nextOrdinal = state.revisions.filter((r) => r.origin !== "case_intended").length + 1;

  return (
    <div className="suggest">
      <div className="suggest__row">
        <div className="segmented" role="radiogroup" aria-label="Component to choose">
          {(["R1", "C1"] as const).map((v) => (
            <label key={v}>
              <input type="radio" name={`${id}-vary`} checked={vary === v} onChange={() => setVary(v)} />
              {v === "R1" ? "Choose R1" : "Choose C1"}
            </label>
          ))}
        </div>
        <Button id={`${id}-suggest`} size="sm" icon="flask" loading={loading} disabled={state.pending !== null} onClick={() => void suggest()}>
          Suggest values for this goal
        </Button>
      </div>
      <p className="panel-card__note">The other component keeps its current value.</p>
      {error ? <ErrorCallout error={error} onRetry={() => void suggest()} /> : null}
      {suggestion ? (
        <div className="suggest__preview">
          <SuggestionPreview
            headingId={`${id}-preview`}
            suggestion={suggestion}
            revision={revision}
            stale={stale}
            applying={state.pending === "revision"}
            nextRevisionOrdinal={nextOrdinal}
            onApply={() =>
              void wb.applySuggestion(suggestion).then((ok) => {
                if (!ok) return;
                setSuggestion(null);
                focusWhenReady(FOCUS.runExperiment);
              })
            }
            onDiscard={() => {
              setSuggestion(null);
              announce("Prediction discarded.");
              // The Discard button unmounts with the preview; return focus to where it started.
              focusWhenReady(`${id}-suggest`);
            }}
          />
        </div>
      ) : null}
    </div>
  );
}
