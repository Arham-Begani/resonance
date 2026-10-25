// Current goal with measured pass/fail per constraint (from the shown experiment's goal
// evaluation) and an editor. The analytical feasibility lives in Explore's "Design help".
import { useId, useState, type FormEvent } from "react";
import type { ExperimentRecord, Goal, GoalConstraint, GoalInput } from "../../api/types";
import { Badge, Button, Callout, ErrorCallout } from "../../components/ui/primitives";
import { formatDb, formatHz, num } from "../../lib/format";
import type { ApiFailure } from "../../api/client";
import { FOCUS, focusWhenReady } from "../../lib/focus";

function requirement(c: GoalConstraint): string {
  return c.kind === "preserve"
    ? `lose at most ${num(c.limit_db, 1)} dB`
    : `reduce by at least ${num(c.limit_db, 1)} dB`;
}

interface GoalCardProps {
  goal: Goal | null;
  record: ExperimentRecord | null;
  experimentLabel: string | null;
  saving: boolean;
  error: ApiFailure | null;
  onSave: (input: GoalInput) => Promise<boolean>;
  /** Why the goal changed without an edit (e.g. a family switch); shown until dismissed. */
  notice?: string | null;
  onDismissNotice?: () => void;
}

export function GoalCard({ goal, record, experimentLabel, saving, error, onSave, notice, onDismissNotice }: GoalCardProps) {
  const [editing, setEditing] = useState(false);
  if (!goal) return null;
  const evaluation = record?.goal && record.goal.goal_id === goal.id ? record.goal : null;
  const stale = record?.goal && record.goal.goal_id !== goal.id;

  return (
    <section className="panel-card" aria-labelledby="goal-heading">
      <div className="panel-card__header">
        <h2 id="goal-heading" className="panel-card__title">
          Goal
        </h2>
        {evaluation ? (
          evaluation.passed ? (
            <Badge tone="success">Goal met</Badge>
          ) : (
            <Badge tone="danger">Goal not met</Badge>
          )
        ) : (
          <Badge>Not measured</Badge>
        )}
      </div>
      <p className="goal-title">{goal.title}</p>
      {notice ? (
        <Callout
          tone="neutral"
          title="Goal changed"
          action={
            onDismissNotice ? (
              <Button
                size="sm"
                variant="ghost"
                aria-label="Dismiss goal change note"
                onClick={() => {
                  onDismissNotice();
                  focusWhenReady(FOCUS.editGoal);
                }}
              >
                Dismiss
              </Button>
            ) : undefined
          }
        >
          {notice}
        </Callout>
      ) : null}

      {editing ? (
        <GoalEditor
          goal={goal}
          saving={saving}
          error={error}
          onCancel={() => {
            setEditing(false);
            focusWhenReady(FOCUS.editGoal);
          }}
          onSave={async (input) => {
            const ok = await onSave(input);
            if (ok) {
              setEditing(false);
              focusWhenReady(FOCUS.editGoal);
            }
          }}
        />
      ) : (
        <>
          <table className="data-table goal-table">
            <caption className="visually-hidden">
              Goal constraints{evaluation ? ` measured in ${experimentLabel ?? "the shown experiment"}` : ""}
            </caption>
            <thead>
              <tr>
                <th scope="col">Constraint</th>
                <th scope="col" className="n">
                  Measured
                </th>
              </tr>
            </thead>
            <tbody>
              {goal.constraints.map((c, i) => {
                const result = evaluation?.results[i];
                return (
                  <tr key={`${c.frequency_hz}-${c.kind}`}>
                    <th scope="row">
                      <span className="goal-table__label">
                        {c.label} · {formatHz(c.frequency_hz)}
                      </span>
                      <span className="goal-table__req">{requirement(c)}</span>
                    </th>
                    <td className="n">
                      {result ? (
                        <span className="goal-table__result">
                          <span>{formatDb(-result.measured_attenuation_db)}</span>
                          {result.passed ? <Badge tone="success">Pass</Badge> : <Badge tone="danger">Fail</Badge>}
                        </span>
                      ) : (
                        <span className="muted">Not measured</span>
                      )}
                      {result ? <span className="goal-table__margin">margin {formatDb(result.margin_db)}</span> : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {evaluation ? (
            <p className="panel-card__note">Measured in {experimentLabel} (ngspice AC gain at each frequency).</p>
          ) : stale ? (
            <p className="panel-card__note">The goal changed since the shown experiment. Run the experiment to evaluate it.</p>
          ) : (
            <p className="panel-card__note">Run the experiment to measure these constraints.</p>
          )}
          <div className="row">
            <Button id={FOCUS.editGoal} size="sm" icon="edit" onClick={() => setEditing(true)}>
              Edit goal
            </Button>
          </div>
        </>
      )}
    </section>
  );
}

interface DraftConstraint {
  kind: "preserve" | "attenuate";
  label: string;
  frequency: string;
  limit: string;
}

function GoalEditor({
  goal,
  saving,
  error,
  onCancel,
  onSave,
}: {
  goal: Goal;
  saving: boolean;
  error: ApiFailure | null;
  onCancel: () => void;
  onSave: (input: GoalInput) => Promise<void>;
}) {
  const id = useId();
  const [title, setTitle] = useState(goal.title);
  const [rows, setRows] = useState<DraftConstraint[]>(
    goal.constraints.map((c) => ({ kind: c.kind, label: c.label, frequency: String(c.frequency_hz), limit: String(c.limit_db) })),
  );
  const [problem, setProblem] = useState<string | null>(null);

  const update = (i: number, patch: Partial<DraftConstraint>) =>
    setRows((prev) => prev.map((r, k) => (k === i ? { ...r, ...patch } : r)));

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const constraints: GoalConstraint[] = [];
    for (const [i, r] of rows.entries()) {
      const f = Number(r.frequency);
      const l = Number(r.limit);
      if (!r.label.trim()) return setProblem(`Constraint ${i + 1}: add a short label.`);
      if (!Number.isFinite(f) || f < 20 || f > 20000) return setProblem(`Constraint ${i + 1}: frequency must be 20 to 20,000 Hz.`);
      if (!Number.isFinite(l) || l < 0 || l > 60) return setProblem(`Constraint ${i + 1}: limit must be 0 to 60 dB.`);
      constraints.push({ kind: r.kind, label: r.label.trim().slice(0, 60), frequency_hz: f, limit_db: l });
    }
    if (!title.trim()) return setProblem("Give the goal a title.");
    if (constraints.length === 0) return setProblem("Add at least one constraint.");
    setProblem(null);
    await onSave({ title: title.trim().slice(0, 120), constraints });
  };

  return (
    <form className="goal-editor stack-3" onSubmit={(e) => void submit(e)} aria-label="Edit goal">
      <div className="field">
        <label className="field__label" htmlFor={`${id}-title`}>
          Goal title
        </label>
        <input id={`${id}-title`} className="input" value={title} maxLength={120} onChange={(e) => setTitle(e.target.value)} />
      </div>
      {rows.map((r, i) => (
        <fieldset key={i} className="goal-editor__row">
          <legend className="field__label">Constraint {i + 1}</legend>
          <div className="goal-editor__grid">
            <div className="field">
              <label className="field__label" htmlFor={`${id}-${i}-label`}>
                Label
              </label>
              <input id={`${id}-${i}-label`} className="input" value={r.label} maxLength={60} onChange={(e) => update(i, { label: e.target.value })} />
            </div>
            <div className="field">
              <label className="field__label" htmlFor={`${id}-${i}-kind`}>
                Requirement
              </label>
              <select id={`${id}-${i}-kind`} className="select" value={r.kind} onChange={(e) => update(i, { kind: e.target.value as DraftConstraint["kind"] })}>
                <option value="preserve">Keep (lose at most)</option>
                <option value="attenuate">Reduce (by at least)</option>
              </select>
            </div>
            <div className="field">
              <label className="field__label" htmlFor={`${id}-${i}-f`}>
                Frequency (Hz)
              </label>
              <input id={`${id}-${i}-f`} className="input num" inputMode="decimal" value={r.frequency} onChange={(e) => update(i, { frequency: e.target.value })} />
            </div>
            <div className="field">
              <label className="field__label" htmlFor={`${id}-${i}-l`}>
                Limit (dB)
              </label>
              <input id={`${id}-${i}-l`} className="input num" inputMode="decimal" value={r.limit} onChange={(e) => update(i, { limit: e.target.value })} />
            </div>
          </div>
          {rows.length > 1 ? (
            <Button size="sm" variant="ghost" onClick={() => setRows((prev) => prev.filter((_, k) => k !== i))}>
              Remove constraint {i + 1}
            </Button>
          ) : null}
        </fieldset>
      ))}
      {rows.length < 4 ? (
        <Button size="sm" icon="plus" onClick={() => setRows((prev) => [...prev, { kind: "attenuate", label: "", frequency: "", limit: "" }])}>
          Add constraint
        </Button>
      ) : null}
      {problem ? (
        <p className="field__error" role="alert">
          {problem}
        </p>
      ) : null}
      {error ? <ErrorCallout error={error} /> : null}
      <div className="row">
        <Button type="submit" variant="primary" loading={saving}>
          Save goal
        </Button>
        <Button onClick={onCancel} disabled={saving}>
          Cancel
        </Button>
      </div>
    </form>
  );
}
