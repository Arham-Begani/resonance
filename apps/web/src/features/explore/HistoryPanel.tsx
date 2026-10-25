// Experiment history (newest first): show any recorded experiment, select two to compare, and
// undo to the parent revision. Previous results always stay available.
import { useState } from "react";
import type { ApiFailure } from "../../api/client";
import { toFailure } from "../../api/client";
import { endpoints } from "../../api/endpoints";
import type { ExperimentComparison } from "../../api/types";
import { usePlayback } from "../../components/audio/PlaybackProvider";
import { Badge, Button, ErrorCallout } from "../../components/ui/primitives";
import { formatClock, formatDb, formatSignedDb, formatHz } from "../../lib/format";
import { FOCUS, focusWhenReady } from "../../lib/focus";
import { useAnnouncer } from "../../state/announcer";
import { FAMILY_LABEL } from "../../lib/family";
import { currentRevision, experimentOrdinal, revisionById, revisionOrdinal, undoTargetId, useWorkbench } from "../../state/workbench";

export function HistoryPanel() {
  const wb = useWorkbench();
  const { state } = wb;
  const playback = usePlayback();
  const { announce } = useAnnouncer();
  const [picked, setPicked] = useState<string[]>([]);
  const [comparison, setComparison] = useState<ExperimentComparison | null>(null);
  const [comparing, setComparing] = useState(false);
  const [compareError, setCompareError] = useState<ApiFailure | null>(null);
  const revision = currentRevision(state);
  const undoTarget = undoTargetId(state, revision?.id);
  const undoRevision = revisionById(state, undoTarget);
  const parentOrdinal = revisionOrdinal(state, undoTarget);
  const crossFamily = Boolean(undoRevision && revision && undoRevision.template_id !== revision.template_id);
  const experiments = [...state.experiments].reverse();
  // Name the family on each entry once the session has used more than one.
  const families = new Set(state.revisions.filter((r) => r.origin !== "case_intended").map((r) => r.template_id));
  const familyOf = (revisionId: string) => {
    const t = revisionById(state, revisionId)?.template_id;
    return families.size > 1 && t ? ` · ${FAMILY_LABEL[t]}` : "";
  };

  const toggle = (id: string) => {
    setPicked((prev) => (prev.includes(id) ? prev.filter((p) => p !== id) : [...prev.slice(-1), id]));
  };

  const compare = async () => {
    if (picked.length !== 2) return;
    // A = the earlier experiment, B = the later one.
    const [a, b] = [...picked].sort((x, y) => (experimentOrdinal(state, x) ?? 0) - (experimentOrdinal(state, y) ?? 0));
    setComparing(true);
    setCompareError(null);
    try {
      const result = await endpoints.compareExperiments(a!, b!);
      setComparison(result);
      announce(result.summary);
      focusWhenReady(FOCUS.comparisonTitle);
    } catch (error) {
      setCompareError(toFailure(error));
    } finally {
      setComparing(false);
    }
  };

  const ordA = comparison ? experimentOrdinal(state, comparison.a_id) : null;
  const ordB = comparison ? experimentOrdinal(state, comparison.b_id) : null;

  return (
    <section className="panel-card" aria-labelledby="history-heading">
      <div className="panel-card__header">
        <h2 id="history-heading" className="panel-card__title">
          Experiment history
        </h2>
        <Button
          size="sm"
          icon="undo"
          disabled={!undoTarget || state.pending !== null}
          onClick={() => {
            // When Undo becomes unavailable at the original revision, move focus to the next step.
            const next = undoTargetId(state, undoTarget);
            wb.undo();
            if (!next) focusWhenReady(FOCUS.runExperiment);
          }}
          title={undoTarget ? (crossFamily && undoRevision ? `Returns to the ${FAMILY_LABEL[undoRevision.template_id].toLowerCase()} circuit` : undefined) : "This is the original circuit."}
        >
          {undoTarget ? `Undo to Revision ${parentOrdinal ?? ""}` : "Undo"}
        </Button>
      </div>

      {experiments.length === 0 ? (
        <p className="panel-card__note">No experiments yet. Each run is recorded here with its revision and goal result.</p>
      ) : (
        <>
          <fieldset className="history">
            <legend className="visually-hidden">Recorded experiments. Choose one to show, or tick two to compare.</legend>
            <ol className="history__list">
              {experiments.map((e) => {
                const ord = experimentOrdinal(state, e.id);
                const rev = revisionOrdinal(state, e.revision_id);
                const shown = state.shownExperimentId === e.id;
                return (
                  <li key={e.id} className={`history__item${shown ? " is-shown" : ""}`}>
                    <label className="history__pick">
                      <input
                        type="checkbox"
                        className="checkbox"
                        checked={picked.includes(e.id)}
                        disabled={e.status !== "succeeded"}
                        onChange={() => toggle(e.id)}
                      />
                      <span className="visually-hidden">Compare Experiment {ord}</span>
                    </label>
                    <div className="history__body">
                      <p className="history__title">
                        Experiment {ord} <span className="muted">· Revision {rev}{familyOf(e.revision_id)}</span>
                      </p>
                      <p className="history__meta num">
                        <span>{formatClock(e.created_at)}</span>
                      {e.status !== "succeeded" ? (
                        <Badge tone="danger">{e.status === "cancelled" ? "Cancelled" : "Failed"}</Badge>
                      ) : e.goal_passed === true ? (
                        <Badge tone="success">Goal met</Badge>
                      ) : e.goal_passed === false ? (
                        <Badge tone="danger">Goal not met</Badge>
                      ) : (
                        <Badge>No goal</Badge>
                      )}
                      </p>
                    </div>
                    <Button
                      size="sm"
                      variant={shown ? "secondary" : "ghost"}
                      disabled={e.status !== "succeeded" || shown}
                      aria-label={shown ? `Experiment ${ord} is shown` : `Show Experiment ${ord}`}
                      onClick={() => void wb.showExperiment(e.id)}
                    >
                      {shown ? "Shown" : "Show"}
                    </Button>
                  </li>
                );
              })}
            </ol>
          </fieldset>
          <div className="row row--between">
            <p className="panel-card__note">{picked.length === 2 ? "Two selected." : "Tick two experiments to compare."}</p>
            <Button id="compare-button" size="sm" disabled={picked.length !== 2} loading={comparing} onClick={() => void compare()}>
              Compare
            </Button>
          </div>
        </>
      )}

      {compareError ? <ErrorCallout error={compareError} onRetry={() => void compare()} /> : null}

      {comparison ? (
        <div className="comparison stack-2" role="region" aria-label="Comparison result">
          <h3 className="comparison__title" id={FOCUS.comparisonTitle} tabIndex={-1}>
            Experiment {ordA} → Experiment {ordB}
          </h3>
          {comparison.changes.length ? (
            <ul className="comparison__changes num">
              {comparison.changes.map((c) => (
                <li key={c.component_id}>
                  {c.component_id}: {c.from_display} → {c.to_display}
                </li>
              ))}
            </ul>
          ) : (
            <p className="panel-card__note">Same component values in both experiments.</p>
          )}
          <table className="data-table">
            <caption className="visually-hidden">Tone gain before and after</caption>
            <thead>
              <tr>
                <th scope="col">Tone</th>
                <th scope="col" className="n">
                  Exp {ordA}
                </th>
                <th scope="col" className="n">
                  Exp {ordB}
                </th>
                <th scope="col" className="n">
                  Change
                </th>
              </tr>
            </thead>
            <tbody>
              {comparison.tones.map((t) => (
                <tr key={t.frequency_hz}>
                  <th scope="row">
                    {t.label} · {formatHz(t.frequency_hz)}
                  </th>
                  <td className="n">{formatDb(t.a_gain_db)}</td>
                  <td className="n">{formatDb(t.b_gain_db)}</td>
                  <td className="n">{formatSignedDb(t.delta_db)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="panel-card__note">{comparison.summary}</p>
          {comparison.audio ? (
            <div className="row row--wrap">
              {comparison.audio.clips.map((clip) => (
                <Button
                  key={clip.artifact_id}
                  size="sm"
                  icon="play"
                  onClick={() => playback.playClip(comparison.audio!, `Comparison: Experiment ${ordA} vs ${ordB}`, clip.artifact_id)}
                >
                  Play {clip.label}
                </Button>
              ))}
            </div>
          ) : null}
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setComparison(null);
              focusWhenReady("compare-button");
            }}
          >
            Close comparison
          </Button>
        </div>
      ) : null}
    </section>
  );
}
