// Learn panel: choose a question, predict what happens to each tone (louder / quieter / about the
// same), optionally explain in your own words, then run the experiment. The result keeps the
// prediction beside the MEASURED change, graded by the server from ngspice tone gains.
import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import type { Expectation, LearnAttempt, LearnPrompt } from "../../api/types";
import { Badge, Button, Callout, ErrorCallout } from "../../components/ui/primitives";
import { formatHz, formatSignedDb } from "../../lib/format";
import { focusWhenReady } from "../../lib/focus";
import { experimentOrdinal, readableIds, useWorkbench } from "../../state/workbench";
import { AgentRunLog, CitationChip, type CitationResolver } from "../agent/AgentRunLog";
import { useAgentRuns } from "../agent/agentRuns";
import { useWorkbenchResolver } from "../agent/useWorkbenchResolver";
import { PlayClipButtons } from "../explore/ListenCard";
import { useLearn } from "./learnStore";
import { changeText, EXPECTATION_LABEL, EXPECTATIONS, LEARN_RESULT_HEADING } from "./learnText";
import "../explore/explore.css";
import "./learn.css";

function PredictionForm({ prompts }: { prompts: LearnPrompt[] }) {
  const id = useId();
  const wb = useWorkbench();
  const learn = useLearn();
  const [promptId, setPromptId] = useState<string>(prompts[0]?.id ?? "");
  const [predictions, setPredictions] = useState<Record<string, Expectation>>({});
  const [reasoning, setReasoning] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const formRef = useRef<HTMLFormElement>(null);
  const prompt = prompts.find((p) => p.id === promptId) ?? prompts[0] ?? null;

  // A new prompt list (new revision) keeps the same question when it is still offered.
  useEffect(() => {
    if (!prompts.some((p) => p.id === promptId)) {
      setPromptId(prompts[0]?.id ?? "");
      setPredictions({});
    }
  }, [prompts, promptId]);

  if (!prompt) return null;
  const busy = learn.running || wb.state.pending !== null;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (learn.running) return;
    const missing = prompt.tones.filter((t) => !predictions[String(t.frequency_hz)]);
    if (missing.length) {
      setProblem(`Predict every tone first: ${missing.map((t) => t.label.toLowerCase()).join(" and ")}.`);
      requestAnimationFrame(() => formRef.current?.querySelector<HTMLInputElement>(`[data-tone="${missing[0]!.frequency_hz}"]`)?.focus());
      return;
    }
    setProblem(null);
    const revisionId = wb.state.currentRevisionId;
    if (!revisionId) return;
    const attempt = await learn.submit({
      revision_id: revisionId,
      change: prompt.change,
      predictions: prompt.tones.map((t) => ({ frequency_hz: t.frequency_hz, expectation: predictions[String(t.frequency_hz)]! })),
      reasoning: reasoning.trim() || null,
    });
    if (attempt) {
      setPredictions({});
      setReasoning("");
      focusWhenReady(LEARN_RESULT_HEADING);
    }
  };

  return (
    <form ref={formRef} className="stack-4" onSubmit={(e) => void submit(e)} aria-label="Make a prediction" noValidate>
      <fieldset className="form-group">
        <legend className="field__label">Question</legend>
        {prompts.map((p) => (
          <label key={p.id} className="choice learn-question">
            <input
              type="radio"
              name={`${id}-prompt`}
              checked={p.id === prompt.id}
              disabled={learn.running}
              onChange={() => {
                setPromptId(p.id);
                setPredictions({});
                setProblem(null);
              }}
            />
            <span>{p.question}</span>
          </label>
        ))}
      </fieldset>

      <div className="stack-3">
        {prompt.tones.map((t) => {
          const key = String(t.frequency_hz);
          return (
            <fieldset key={key} className={`prediction${problem && !predictions[key] ? " is-missing" : ""}`}>
              <legend className="prediction__legend">
                <span>{t.label}</span> <span className="num muted">{formatHz(t.frequency_hz)}</span>
                <span className="prediction__role">{t.role === "wanted" ? "wanted" : "interference"}</span>
              </legend>
              <div className="segmented prediction__choices">
                {EXPECTATIONS.map((e, i) => (
                  <label key={e}>
                    <input
                      type="radio"
                      name={`${id}-${key}`}
                      data-tone={i === 0 ? key : undefined}
                      checked={predictions[key] === e}
                      disabled={learn.running}
                      onChange={() => setPredictions((prev) => ({ ...prev, [key]: e }))}
                    />
                    {EXPECTATION_LABEL[e]}
                  </label>
                ))}
              </div>
            </fieldset>
          );
        })}
      </div>

      <div className="field">
        <label className="field__label" htmlFor={`${id}-why`}>
          In my own words (optional)
        </label>
        <textarea
          id={`${id}-why`}
          className="textarea"
          maxLength={600}
          value={reasoning}
          disabled={learn.running}
          placeholder="What do you expect, and why?"
          onChange={(e) => setReasoning(e.target.value)}
        />
      </div>

      {problem ? (
        <p className="field__error" role="alert">
          {problem}
        </p>
      ) : null}
      {learn.runError ? <ErrorCallout error={learn.runError} /> : null}

      <div className="row">
        <Button id={`${id}-run`} type="submit" variant="primary" icon="flask" loading={learn.running} disabled={busy && !learn.running}>
          {learn.running ? "Measuring…" : "Run the experiment"}
        </Button>
        {learn.running ? (
          <Button
            variant="danger"
            onClick={() => {
              learn.cancel();
              // Cancel disappears when the run stops; keep keyboard focus on the Run button.
              focusWhenReady(`${id}-run`);
            }}
          >
            Cancel
          </Button>
        ) : null}
      </div>
      <p className="panel-card__note">
        Runs ngspice on this revision and on the changed one, then grades each prediction from the measured tone gains. Nothing plays automatically.
      </p>
    </form>
  );
}

function PromptCard() {
  const learn = useLearn();
  const wb = useWorkbench();
  const notImplemented = learn.promptsError?.code === "not_implemented";
  return (
    <section className="panel-card" aria-labelledby="learn-heading">
      <h2 id="learn-heading" className="panel-card__title">
        Predict, then measure
      </h2>
      <p className="panel-card__note">Choose a change, predict what happens to each tone, then run it. Your prediction stays beside the measured result.</p>
      {wb.state.status !== "ready" || (learn.loadingPrompts && !learn.prompts) ? (
        <div className="stack-2" aria-busy="true">
          <span className="visually-hidden">Loading questions…</span>
          <div className="skeleton skeleton--row" />
          <div className="skeleton skeleton--row" />
        </div>
      ) : notImplemented ? (
        <Callout tone="neutral" title="Learn is not available on this server yet">
          Explore and Investigate keep working. Learn questions appear here once the server supports them.
        </Callout>
      ) : learn.promptsError ? (
        <ErrorCallout error={learn.promptsError} onRetry={learn.reloadPrompts} retryLabel="Load questions" />
      ) : learn.prompts && learn.prompts.length === 0 ? (
        <div className="empty empty--compact">
          <p className="empty__title">No questions for this revision</p>
          <p>Every offered change would leave the supported range. Undo or edit a value to get new questions.</p>
        </div>
      ) : learn.prompts ? (
        <PredictionForm prompts={learn.prompts} />
      ) : null}
    </section>
  );
}

export function GradedList({ attempt, resolver }: { attempt: LearnAttempt; resolver: CitationResolver }) {
  return (
    <ul className="graded" aria-label="Your predictions beside the measured change for each tone">
      {attempt.graded.map((g) => (
        <li key={g.frequency_hz} className="graded__item">
          <p className="graded__head">
            <span className="graded__tone">
              {g.label} <span className="num muted">{formatHz(g.frequency_hz)}</span>
            </span>
            <Badge tone={g.correct ? "success" : "warning"}>{g.correct ? "Matched" : "Didn’t match"}</Badge>
          </p>
          <p className="graded__row">
            <span className="muted">You predicted:</span> {EXPECTATION_LABEL[g.predicted].toLowerCase()} <span className="muted">· Measured:</span>{" "}
            <strong className="num">{formatSignedDb(g.delta_db)}</strong> ({EXPECTATION_LABEL[g.observed].toLowerCase()})
          </p>
          {g.evidence.length ? (
            <p className="graded__evidence">
              {g.evidence.map((ref, i) => (
                <CitationChip key={`${ref.result_id}-${i}`} refItem={ref} resolver={resolver} />
              ))}
            </p>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

function ResultCard({ attempt, index }: { attempt: LearnAttempt; index: number }) {
  const wb = useWorkbench();
  const { state } = wb;
  const before = experimentOrdinal(state, attempt.base_experiment_id);
  const after = experimentOrdinal(state, attempt.new_experiment_id);
  const resolver: CitationResolver = {
    label: (rid) => {
      const ord = experimentOrdinal(state, rid);
      return ord ? `Experiment ${ord}` : null;
    },
  };
  const all = attempt.score === attempt.total;
  return (
    <section className="panel-card learn-result" aria-labelledby={LEARN_RESULT_HEADING}>
      <div className="panel-card__header">
        <h2 id={LEARN_RESULT_HEADING} tabIndex={-1} className="panel-card__title">
          Result · Attempt {index}
        </h2>
        <Badge tone={all ? "success" : "warning"}>
          {attempt.score} of {attempt.total} matched
        </Badge>
      </div>
      <p className="learn-result__change num">
        {changeText(attempt)}
        <span className="muted">
          {" "}
          · Experiment {before ?? "?"} → Experiment {after ?? "?"}
        </span>
      </p>
      <GradedList attempt={attempt} resolver={resolver} />
      <div className="stack-1">
        <h3 className="eyebrow">What the measurement shows</h3>
        <p className="learn-result__text">{readableIds(state, attempt.explanation)}</p>
      </div>
      <div className="stack-1">
        <h3 className="eyebrow">Tradeoff</h3>
        <p className="learn-result__text">{readableIds(state, attempt.tradeoff)}</p>
      </div>
      {attempt.reasoning ? (
        <div className="stack-1">
          <h3 className="eyebrow">In your words</h3>
          <p className="learn-result__quote">{attempt.reasoning}</p>
        </div>
      ) : null}
      {attempt.comparison.audio ? (
        <div className="stack-1">
          <h3 className="eyebrow">Listen</h3>
          <div className="row row--wrap">
            <PlayClipButtons audio={attempt.comparison.audio} title={`Attempt ${index}: before and after`} />
          </div>
          <p className="panel-card__note">Both clips use one shared gain, so the level difference you hear is the measured one.</p>
        </div>
      ) : null}
    </section>
  );
}

function AttemptHistory() {
  const learn = useLearn();
  const attempts = learn.attempts;
  if (attempts.length === 0) return null;
  return (
    <section className="panel-card" aria-labelledby="learn-history-heading">
      <h2 id="learn-history-heading" className="panel-card__title">
        Attempts
      </h2>
      <ol className="attempts" reversed>
        {[...attempts].reverse().map((a) => {
          const index = attempts.indexOf(a) + 1;
          const shown = learn.activeAttempt?.id === a.id;
          return (
            <li key={a.id} className={`attempts__item${shown ? " is-shown" : ""}`}>
              <div className="attempts__body">
                <p className="attempts__title">
                  Attempt {index} <span className="muted num">· {changeText(a)}</span>
                </p>
                <p className="attempts__meta num">
                  {a.score} of {a.total} predictions matched
                </p>
              </div>
              <Button
                size="sm"
                variant={shown ? "secondary" : "ghost"}
                disabled={shown}
                aria-label={shown ? `Attempt ${index} is shown` : `Show Attempt ${index}`}
                onClick={() => {
                  learn.show(a.id);
                  focusWhenReady(LEARN_RESULT_HEADING);
                }}
              >
                {shown ? "Shown" : "Show"}
              </Button>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

export function LearnPanel() {
  const learn = useLearn();
  const runs = useAgentRuns();
  const resolver = useWorkbenchResolver();
  const active = learn.activeAttempt;
  const index = active ? learn.attempts.indexOf(active) + 1 : 0;
  return (
    <div className="panel-stack">
      {active ? <ResultCard key={active.id} attempt={active} index={index} /> : null}
      <PromptCard />
      <AgentRunLog
        title="AI lab partner"
        view={runs.views.explore}
        resolver={resolver}
        onCancel={() => void runs.cancel("explore")}
        onDismiss={() => runs.clear("explore")}
      />
      <AttemptHistory />
    </div>
  );
}
