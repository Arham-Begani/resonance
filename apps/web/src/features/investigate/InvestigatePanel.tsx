// Case panel: start → symptom → approach → instruments/AI run → observations → evidence →
// diagnosis → repair → close & reveal. Everything rendered here comes from PublicCase; the
// hidden fault is shown only in the reveal, after the case is closed.
import { Fragment, useEffect, useId, useState } from "react";
import type { HypothesisId, InstrumentSpec, PublicCase, TemplateId } from "../../api/types";
import { usePlayback } from "../../components/audio/PlaybackProvider";
import { Icon } from "../../components/ui/Icon";
import { Badge, Button, Callout, ErrorCallout } from "../../components/ui/primitives";
import { formatHz } from "../../lib/format";
import { FOCUS, focusWhenReady } from "../../lib/focus";
import { FAMILIES, FAMILY_LABEL, lowToneNote } from "../../lib/family";
import { useWorkbench } from "../../state/workbench";
import { AgentRunLog } from "../agent/AgentRunLog";
import { isRunActive, useAgentRuns } from "../agent/agentRuns";
import { useCase } from "./caseStore";
import { DiagnosisForm, DiagnosisSummary } from "./DiagnosisForm";
import type { GuidePrediction, PredictionResult } from "./guidePrediction";
import { checkText, floorsOf, toneGainText } from "./floors";
import { RepairSection } from "./RepairSection";
import { RevealCard } from "./RevealCard";
import { HYPOTHESIS_STATUS, INSTRUMENT_SHORT, PROBE_LABEL, STATUS_LABEL, hypothesisLabel, isLiveStatus, observationLabel, observationTitle, quantityLabel, requesterLabel, type TransientProbe } from "./labels";
import "../explore/explore.css";
import "./investigate.css";

const CASE_AGENT_TITLE = "case-agent-title";

function BudgetMeter({ c }: { c: PublicCase }) {
  const { total, remaining } = c.budget;
  return (
    <div className="budget" role="group" aria-label={`${remaining} of ${total} experiments left`}>
      <div className="budget__cells" aria-hidden="true">
        {Array.from({ length: total }, (_, i) => (
          <span key={i} className={`budget__cell${i < total - remaining ? " is-used" : ""}`} />
        ))}
      </div>
      <span className="budget__text num">
        {remaining} of {total} experiments left
      </span>
    </div>
  );
}

type StepState = "done" | "current" | "available" | "upcoming" | "skipped" | "stopped";

/**
 * The case lifecycle as five steps, derived only from the public case (status, budget, offered
 * actions, recorded diagnosis/repair/reveal). Never a percentage: each step is done, current,
 * available (can be done now, alongside the current step), upcoming, skipped or stopped.
 * A diagnosis can be submitted any time after the symptom, so while experiments remain Measure is
 * current and Diagnose is available; once the budget is spent Diagnose becomes current.
 */
export interface CaseStep {
  key: string;
  label: string;
  detail: string | null;
  state: StepState;
}

export function caseSteps(c: PublicCase): CaseStep[] {
  const live = isLiveStatus(c.status);
  const used = c.budget.total - c.budget.remaining;
  const canRepair = c.available_actions.includes("propose_repair") || c.available_actions.includes("verify_repair");
  const canDiagnose = c.available_actions.includes("submit_diagnosis");
  // Choosing an approach is the doorway to measuring, so "Measure" is current from the start.
  const measure: StepState = c.diagnosis || !live || c.budget.remaining === 0 ? "done" : "current";
  const diagnose: StepState = c.diagnosis
    ? "done"
    : !live
      ? "skipped"
      : canDiagnose
        ? measure === "current"
          ? "available"
          : "current"
        : c.budget.remaining === 0 && c.mode
          ? "current"
          : "upcoming";
  const repair: StepState = c.repair ? "done" : canRepair ? "current" : !live ? "skipped" : "upcoming";
  const reveal: StepState = c.reveal ? "done" : c.diagnosis && !canRepair && live ? "current" : "upcoming";
  const steps: CaseStep[] = [
    { key: "symptom", label: "Symptom", detail: null, state: "done" },
    { key: "measure", label: "Measure", detail: `${used} of ${c.budget.total} used`, state: measure },
    { key: "diagnose", label: "Diagnose", detail: diagnose === "available" ? "any time" : null, state: diagnose },
    { key: "repair", label: "Repair", detail: c.repair ? (c.repair.status !== "succeeded" ? "not verified" : c.repair.functional_recovery ? "restored" : "not restored") : null, state: repair },
    { key: "reveal", label: "Reveal", detail: null, state: reveal },
  ];
  if (c.status === "failed") {
    const i = steps.findIndex((st) => st.state === "current" || st.state === "available" || st.state === "upcoming" || st.state === "skipped");
    if (i >= 0) steps[i] = { ...steps[i]!, state: "stopped" };
  }
  return steps;
}

const STEP_WORD: Record<StepState, string> = {
  done: "done",
  current: "current step",
  available: "available now",
  upcoming: "not yet",
  skipped: "skipped",
  stopped: "stopped",
};

function CaseProgress({ c }: { c: PublicCase }) {
  const steps = caseSteps(c);
  return (
    <ol className="case-progress" aria-label="Case progress">
      {steps.map((st, i) => (
        <li key={st.key} className={`case-progress__step is-${st.state}`} aria-current={st.state === "current" ? "step" : undefined}>
          <span className="case-progress__mark" aria-hidden="true">
            {st.state === "done" ? <Icon name="check" size={12} /> : st.state === "stopped" ? <Icon name="x" size={12} /> : i + 1}
          </span>
          <span className="case-progress__label">{st.label}</span>
          {st.detail ? <span className="case-progress__detail num">{st.detail}</span> : null}
          <span className="visually-hidden">, {STEP_WORD[st.state]}</span>
        </li>
      ))}
    </ol>
  );
}

function FamilyChoice({ disabled }: { disabled: boolean }) {
  const cs = useCase();
  const id = useId();
  const options: { value: TemplateId | null; label: string }[] = [
    ...FAMILIES.map((f) => ({ value: f, label: FAMILY_LABEL[f] })),
    { value: null, label: "Surprise me" },
  ];
  return (
    <div className="field">
      <p className="field__label" id={`${id}-label`}>
        Circuit family
      </p>
      <div className="segmented" role="radiogroup" aria-labelledby={`${id}-label`}>
        {options.map((o) => (
          <label key={o.label}>
            <input type="radio" name={`${id}-family`} checked={cs.family === o.value} disabled={disabled} onChange={() => cs.setFamily(o.value)} />
            {o.label}
          </label>
        ))}
      </div>
      <p className="field__hint">
        {cs.family === null
          ? "The family is drawn at random (50/50) and shown once the mystery starts."
          : `An RC ${FAMILY_LABEL[cs.family].toLowerCase()} filter.`}
      </p>
    </div>
  );
}

function StartCard() {
  const cs = useCase();
  const wb = useWorkbench();
  const conflict = cs.error?.action === "create" && cs.error.error.code === "conflict";
  return (
    <section className="panel-card" aria-labelledby="start-heading">
      <h2 id="start-heading" className="panel-card__title">
        Circuit mystery
      </h2>
      <p className="panel-card__lead">
        The intended design stays visible, but the circuit&apos;s simulated behaviour may differ, or it may be working correctly. You get one
        symptom and three experiments; the possible explanations are listed with the symptom.
      </p>
      <FamilyChoice disabled={cs.pending === "create"} />
      {conflict ? (
        <Callout
          tone="warning"
          title="A mystery is already open"
          action={
            wb.state.activeCaseId ? (
              <>
                <Button size="sm" onClick={() => void cs.load(wb.state.activeCaseId!)}>
                  Resume it
                </Button>
                <Button
                  size="sm"
                  variant="danger"
                  onClick={() =>
                    void cs
                      .close(wb.state.activeCaseId!)
                      .then(() => cs.create())
                      .then((ok) => ok && focusWhenReady(FOCUS.symptomHeading))
                  }
                >
                  Close &amp; reveal it, then start a new one
                </Button>
              </>
            ) : undefined
          }
        >
          {cs.error!.error.message}
        </Callout>
      ) : cs.error && (cs.error.action === "create" || cs.error.action === "load") ? (
        <ErrorCallout error={cs.error.error} onRetry={() => void cs.create()} />
      ) : null}
      <Button
        variant="primary"
        size="lg"
        icon="investigate"
        loading={cs.pending === "create"}
        onClick={() => void cs.create().then((ok) => ok && focusWhenReady(FOCUS.symptomHeading))}
      >
        Start a mystery
      </Button>
    </section>
  );
}

function SymptomCard({ c }: { c: PublicCase }) {
  const cs = useCase();
  const playback = usePlayback();
  const symptom = c.observations.find((o) => o.sequence === 0);
  if (!symptom) return null;
  const tones = symptom.transient?.tones ?? [];
  const audio = symptom.transient?.audio ?? null;
  const lowNote = lowToneNote(c.intended_revision.source.tones);
  return (
    <section className="panel-card" aria-labelledby="symptom-heading">
      <div className="panel-card__header">
        <h2 id={FOCUS.symptomHeading} tabIndex={-1} className="panel-card__title">
          What we observed
        </h2>
        <Badge icon={null}>Symptom · no budget used</Badge>
      </div>
      <p>{symptom.summary}</p>
      {lowNote ? <p className="panel-card__note">{lowNote}</p> : null}
      {tones.length ? (
        <table className="data-table">
          <caption className="visually-hidden">Symptom tone levels at the output</caption>
          <thead>
            <tr>
              <th scope="col">Tone</th>
              <th scope="col" className="n">
                Expected
              </th>
              <th scope="col" className="n">
                Observed
              </th>
            </tr>
          </thead>
          <tbody>
            {tones.map((t) => (
              <tr key={t.frequency_hz}>
                <th scope="row">
                  {t.label} · {formatHz(t.frequency_hz)}
                </th>
                <td className="n">{toneGainText(t, "expected", floorsOf(c))}</td>
                <td className="n">{toneGainText(t, "observed", floorsOf(c))}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
      <div className="row row--wrap">
        {audio?.clips.map((clip) => (
          <Button key={clip.artifact_id} size="sm" icon="play" onClick={() => playback.playClip(audio, "Symptom", clip.artifact_id)}>
            Play {clip.label}
          </Button>
        ))}
        <Button size="sm" variant="ghost" onClick={() => cs.setView({ kind: "observation", id: symptom.id })}>
          Show in instruments
        </Button>
      </div>
    </section>
  );
}

function ApproachCard({ c }: { c: PublicCase }) {
  const cs = useCase();
  const wb = useWorkbench();
  const ai = wb.state.health?.ai;
  const aiReady = Boolean(ai?.available);
  if (!c.available_actions.includes("choose_mode")) return null;
  return (
    <section className="panel-card" aria-labelledby="approach-heading">
      <h2 id="approach-heading" className="panel-card__title">
        Choose an approach
      </h2>
      <div className="approach">
        <button type="button" className="approach__option" disabled={cs.pending !== null} onClick={() => void cs.chooseMode("guide").then((ok) => ok && focusWhenReady(FOCUS.measureHeading))}>
          <span className="approach__title">Guide me</span>
          <span className="approach__text">You choose the measurements. Ask for a hint when you want one.</span>
        </button>
        <button
          type="button"
          className="approach__option"
          disabled={!aiReady || cs.pending !== null}
          aria-describedby={!aiReady ? "approach-ai-note" : undefined}
          onClick={() =>
            void cs.chooseMode("agent").then((ok) => {
              if (!ok) return;
              focusWhenReady(CASE_AGENT_TITLE);
              void cs.startAgent("investigate");
            })
          }
        >
          <span className="approach__title">Investigate for me</span>
          <span className="approach__text">The AI lab partner chooses up to three experiments and presents the evidence.</span>
        </button>
      </div>
      {!aiReady ? (
        <p id="approach-ai-note" className="panel-card__note">
          AI lab partner unavailable{ai?.state === "dev_fixture" ? " (development fixture mode)" : ""}. Guide me works without AI.
        </p>
      ) : null}
      {cs.error?.action === "mode" ? <ErrorCallout error={cs.error.error} /> : null}
    </section>
  );
}

function blockedReason(c: PublicCase, agentActive: boolean): string | null {
  if (c.available_actions.includes("run_experiment")) return null;
  if (agentActive) return "The AI lab partner is running experiments. Cancel it to take over.";
  if (c.budget.remaining <= 0) return "All experiments are used. Submit a diagnosis next.";
  if (c.status === "ready") return "Choose an approach first.";
  if (c.diagnosis) return "A diagnosis has been submitted; no more experiments.";
  if (!isLiveStatus(c.status)) return "This case has ended.";
  return "Experiments are not available right now.";
}

function PredictStep({
  c,
  expected,
  onChange,
  disabled,
}: {
  c: PublicCase;
  expected: HypothesisId[];
  onChange: (next: HypothesisId[]) => void;
  disabled: boolean;
}) {
  return (
    <fieldset className="guide-predict">
      <legend className="field__label">Predict first (optional): which explanations do you expect the next measurement to rule out?</legend>
      <div className="guide-predict__options">
        {c.evidence.remaining.map((h) => (
          <label key={h} className="choice">
            <input
              type="checkbox"
              className="checkbox"
              checked={expected.includes(h)}
              disabled={disabled}
              onChange={(e) => onChange(e.target.checked ? [...expected, h] : expected.filter((x) => x !== h))}
            />
            <span>{hypothesisLabel(c, h)}</span>
          </label>
        ))}
      </div>
      <p className="panel-card__note">
        Leave everything unticked to skip. Afterwards your prediction is compared with the recorded evidence, not with the hidden answer.
      </p>
    </fieldset>
  );
}

function InstrumentCard({ spec, disabled, running, onRun }: { spec: InstrumentSpec; disabled: boolean; running: boolean; onRun: (probe: TransientProbe | null) => void }) {
  const id = useId();
  const probes: TransientProbe[] = spec.probes ?? [];
  const [probe, setProbe] = useState<TransientProbe | null>(probes[0] ?? null);
  const grid = spec.frequency_hz;
  return (
    <li className="instrument">
      <div className="instrument__head">
        <h3 className="instrument__title">{spec.title || INSTRUMENT_SHORT[spec.kind]}</h3>
        <span className="muted num">uses {spec.cost}</span>
      </div>
      <p className="instrument__desc">{spec.description}</p>
      <p className="instrument__returns">
        Returns: {spec.returns.map((q) => quantityLabel(q)).join(", ")}
        {grid && grid.length ? ` · ${formatHz(grid[0]!)}–${formatHz(grid[grid.length - 1]!)}, ${grid.length} points` : ""}
      </p>
      {probes.length > 0 ? (
        <div className="segmented" role="radiogroup" aria-label={`${spec.title} probe`}>
          {probes.map((p) => (
            <label key={p}>
              <input type="radio" name={`${id}-probe`} checked={probe === p} disabled={disabled} onChange={() => setProbe(p)} />
              {PROBE_LABEL[p]}
            </label>
          ))}
        </div>
      ) : null}
      <Button size="sm" icon="flask" disabled={disabled} loading={running} onClick={() => onRun(probes.length ? probe : null)}>
        Run{probe && probes.length ? ` · ${PROBE_LABEL[probe].toLowerCase()}` : ""}
      </Button>
    </li>
  );
}

function InstrumentsCard({ c }: { c: PublicCase }) {
  const cs = useCase();
  const runs = useAgentRuns();
  const agentActive = isRunActive(runs.views.case) || Boolean(c.active_run_id);
  const reason = blockedReason(c, agentActive);
  const [ranKind, setRanKind] = useState<string | null>(null);
  const [expected, setExpected] = useState<HypothesisId[]>([]);
  const wb = useWorkbench();
  if (c.status === "ready" || !isLiveStatus(c.status)) return null;
  const canPredict = c.mode === "guide" && !reason && c.evidence.remaining.length >= 2;
  const canHint = c.available_actions.includes("request_hint");
  const canStartAgent = c.available_actions.includes("start_agent") && !agentActive;
  const aiReady = Boolean(wb.state.health?.ai?.available);
  return (
    <section className="panel-card" aria-labelledby="instr-heading">
      <div className="panel-card__header">
        <h2 id={FOCUS.measureHeading} tabIndex={-1} className="panel-card__title">
          Take a measurement
        </h2>
        <BudgetMeter c={c} />
      </div>
      {reason ? <p className="panel-card__note">{reason}</p> : null}
      {canPredict ? <PredictStep c={c} expected={expected} onChange={setExpected} disabled={cs.pending !== null} /> : null}
      <ul className="instrument-list">
        {c.instruments.map((spec) => (
          <InstrumentCard
            key={spec.kind}
            spec={spec}
            disabled={Boolean(reason) || cs.pending !== null}
            running={cs.pending === "experiment" && ranKind === spec.kind}
            onRun={(probe) => {
              setRanKind(spec.kind);
              const ticked = expected.filter((h) => c.evidence.remaining.includes(h));
              const prediction: GuidePrediction | null =
                canPredict && ticked.length ? { candidates: [...c.evidence.remaining], expected: ticked } : null;
              // Focus the new measurement (the run button may become unavailable afterwards).
              void cs.runExperiment(spec.kind, probe, prediction).then((obsId) => {
                if (!obsId) return;
                setExpected([]);
                focusWhenReady(FOCUS.observation(obsId));
              });
            }}
          />
        ))}
      </ul>
      {cs.error?.action === "experiment" ? <ErrorCallout error={cs.error.error} /> : null}
      {canStartAgent ? (
        <div className="row row--between">
          <p className="panel-card__note">{aiReady ? "Let the AI lab partner choose the remaining experiments." : "AI lab partner unavailable."}</p>
          <Button
            size="sm"
            icon="ai"
            disabled={!aiReady}
            onClick={() => {
              focusWhenReady(CASE_AGENT_TITLE);
              void cs.startAgent("investigate");
            }}
          >
            Investigate for me
          </Button>
        </div>
      ) : null}
      {c.mode === "guide" ? (
        <div className="stack-1">
          <div className="row row--between">
            <p className="panel-card__note">Hints used: {c.hints_used ?? 0}</p>
            <Button
              size="sm"
              icon="ai"
              disabled={!canHint || !aiReady}
              aria-describedby={!aiReady ? "hint-ai-note" : undefined}
              loading={runs.views.case.connection === "starting"}
              onClick={() => void cs.startAgent("hint")}
            >
              Ask for a hint
            </Button>
          </div>
          {!aiReady ? (
            <p id="hint-ai-note" className="panel-card__note">
              AI lab partner unavailable, so hints are off. Every instrument and the diagnosis still work without AI.
            </p>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

export function PredictionCheckView({ c, result }: { c: PublicCase; result: PredictionResult }) {
  return (
    <div className="prediction-check">
      <p className="prediction-check__title">
        Your prediction: {result.matched} of {result.checks.length} matched the evidence
      </p>
      <ul className="prediction-check__list" aria-label="Your prediction compared with the recorded evidence">
        {result.checks.map((ch) => (
          <li key={ch.hypothesis} className="prediction-check__item">
            <span className="prediction-check__label">{hypothesisLabel(c, ch.hypothesis)}</span>
            <span className="prediction-check__row">
              <span className="muted">You expected: {ch.expectedRuledOut ? "ruled out" : "still possible"}</span>
              <span className="muted"> · Evidence: </span>
              <Badge tone={ch.agrees ? "success" : "warning"} icon={ch.agrees ? "check" : "alert"}>
                {ch.ruledOut ? "Ruled out" : ch.onlyOneLeft ? "Still possible (only one left)" : "Still possible"}
              </Badge>
              <span className="visually-hidden">{ch.agrees ? " (as you expected)" : " (not what you expected)"}</span>
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function ObservationsCard({ c }: { c: PublicCase }) {
  const cs = useCase();
  const list = c.observations.filter((o) => o.sequence > 0);
  if (list.length === 0) return null;
  return (
    <section className="panel-card" aria-labelledby="obs-heading">
      <h2 id="obs-heading" className="panel-card__title">
        Recorded measurements
      </h2>
      <ol className="obs-list">
        {list.map((o) => {
          const shown = cs.view?.kind === "observation" && cs.view.id === o.id;
          return (
            <li key={o.id} className={`obs${shown ? " is-shown" : ""}${o.status !== "succeeded" ? " is-failed" : ""}`}>
              <div className="obs__head">
                <span className="citation">{observationLabel(o)}</span>
                <h3 className="obs__title" id={FOCUS.observation(o.id)} tabIndex={-1}>
                  {observationTitle(o)}
                </h3>
              </div>
              <p className="obs__meta">
                {requesterLabel(o)} · {o.status === "succeeded" ? (o.counted ? "used 1 experiment" : "no budget used") : "failed · no budget used"}
              </p>
              <p className="obs__summary">{o.status === "succeeded" ? o.summary : (o.error?.message ?? o.summary)}</p>
              {cs.predictions[o.id] ? <PredictionCheckView c={c} result={cs.predictions[o.id]!} /> : null}
              {o.status === "succeeded" ? (
                <Button size="sm" variant="ghost" disabled={shown} onClick={() => cs.setView({ kind: "observation", id: o.id })}>
                  {shown ? "Shown in instruments" : "Show in instruments"}
                </Button>
              ) : null}
            </li>
          );
        })}
      </ol>
    </section>
  );
}

function EvidenceCard({ c }: { c: PublicCase }) {
  const e = c.evidence;
  return (
    <section className="panel-card" aria-labelledby="evidence-heading">
      <h2 id="evidence-heading" className="panel-card__title">
        Which explanations still fit
      </h2>
      <p>{e.statement}</p>
      <table className="data-table hypotheses">
        <caption className="visually-hidden">Explanations and their status given the recorded observations</caption>
        <thead>
          <tr>
            <th scope="col">Explanation</th>
            <th scope="col">Status</th>
          </tr>
        </thead>
        <tbody>
          {e.assessments.map((a) => {
            const st = HYPOTHESIS_STATUS[a.status];
            const worst = a.checks.find((ch) => !ch.consistent);
            return (
              <tr key={a.hypothesis_id}>
                <th scope="row">
                  {hypothesisLabel(c, a.hypothesis_id)}
                  {worst ? (
                    <span className="hypotheses__why num">
                      {quantityLabel(worst.quantity)}
                      {worst.frequency_hz != null ? ` @ ${formatHz(worst.frequency_hz)}` : ""}: {checkText(worst, floorsOf(c))}
                    </span>
                  ) : null}
                </th>
                <td>
                  <Badge tone={st.tone} icon={st.tone === "neutral" ? "circle" : undefined}>
                    {st.text}
                  </Badge>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <details className="disclosure">
        <summary>How explanations are ruled out</summary>
        <p className="panel-card__note">{c.policy.description}</p>
      </details>
      {e.distinguishing_instruments && e.distinguishing_instruments.length && e.remaining.length > 1 ? (
        <Callout tone="neutral" icon="info" title="What would tell them apart">
          {e.distinguishing_instruments.map((k) => INSTRUMENT_SHORT[k]).join(" or ")} would distinguish the remaining explanations.
        </Callout>
      ) : null}
    </section>
  );
}

function DiagnosisCard({ c }: { c: PublicCase }) {
  const cs = useCase();
  if (c.diagnosis) {
    return (
      <section className="panel-card" aria-labelledby="dx-heading">
        <h2 id={FOCUS.diagnosisHeading} tabIndex={-1} className="panel-card__title">
          Diagnosis
        </h2>
        <DiagnosisSummary c={c} record={c.diagnosis} />
      </section>
    );
  }
  if (!c.available_actions.includes("submit_diagnosis")) return null;
  return (
    <section className="panel-card" aria-labelledby="dx-heading">
      <h2 id={FOCUS.diagnosisHeading} tabIndex={-1} className="panel-card__title">
        Submit a diagnosis
      </h2>
      {cs.error?.action === "diagnosis" ? <ErrorCallout error={cs.error.error} /> : null}
      <DiagnosisForm
        c={c}
        submitting={cs.pending === "diagnosis"}
        onSubmit={async (submission) => {
          const ok = await cs.submitDiagnosis(submission);
          if (ok) focusWhenReady(FOCUS.diagnosisHeading);
          return ok;
        }}
      />
    </section>
  );
}

function CloseCard({ c }: { c: PublicCase }) {
  const cs = useCase();
  const [confirming, setConfirming] = useState(false);
  if (!c.available_actions.includes("close")) return null;
  return (
    <section className="panel-card" aria-labelledby="close-heading">
      <h2 id="close-heading" className="panel-card__title">
        Finish
      </h2>
      {confirming ? (
        <>
          <p>Closing stops any AI run and reveals the actual fault and seed. No further measurements are possible on this case.</p>
          <div className="row">
            <Button variant="primary" loading={cs.pending === "close"} onClick={() => void cs.close()}>
              Close &amp; reveal
            </Button>
            <Button onClick={() => setConfirming(false)} disabled={cs.pending === "close"}>
              Keep investigating
            </Button>
          </div>
        </>
      ) : (
        <Button onClick={() => setConfirming(true)}>Close &amp; reveal…</Button>
      )}
      {cs.error?.action === "close" ? <ErrorCallout error={cs.error.error} /> : null}
    </section>
  );
}

export function InvestigatePanel() {
  const cs = useCase();
  const runs = useAgentRuns();
  const c = cs.current;
  // Focus the reveal only when it appears during this visit (not when reopening a closed case).
  const [revealSeenOpen, setRevealSeenOpen] = useState<string | null>(null);
  useEffect(() => {
    if (c && !c.reveal) setRevealSeenOpen(c.id);
  }, [c]);

  if (!c) {
    return (
      <div className="panel-stack">
        {cs.pending === "load" ? <p className="muted">Loading the open mystery…</p> : <StartCard />}
      </div>
    );
  }

  const view = runs.views.case;
  const obsLabelFor = (id: string) => {
    const o = c.observations.find((x) => x.id === id);
    return o ? observationLabel(o) : null;
  };

  return (
    <div className="panel-stack">
      <section className="case-head" aria-label="Case status">
        <div className="row row--between">
          <p className="case-head__title">
            Mystery case <span className="case-head__family">· RC {FAMILY_LABEL[c.intended_revision.template_id].toLowerCase()}</span>
          </p>
          <Badge tone={c.status === "failed" ? "danger" : "neutral"} icon={null}>
            {STATUS_LABEL[c.status]}
          </Badge>
        </div>
        <CaseProgress c={c} />
        {c.mode ? <p className="panel-card__note">Approach: {c.mode === "guide" ? "Guide me" : "Investigate for me"}</p> : null}
      </section>

      {c.status === "failed" ? (
        <Callout tone="danger" title="The simulator failed repeatedly">
          This case stopped after repeated infrastructure failures. Close it to see the reveal, then start a new mystery.
        </Callout>
      ) : null}

      {c.reveal ? (
        <RevealCard
          c={c}
          onNew={() => void cs.create().then((ok) => ok && focusWhenReady(FOCUS.symptomHeading))}
          creating={cs.pending === "create"}
          focusOnMount={revealSeenOpen === c.id}
        />
      ) : null}

      {/* Keyed by case: a new mystery must never inherit the previous case's local state
          (an armed "Close & reveal" confirmation, diagnosis drafts, probe or repair choice). */}
      <Fragment key={c.id}>
        <SymptomCard c={c} />
        <ApproachCard c={c} />
        <AgentRunLog
          headingId={CASE_AGENT_TITLE}
          title={view.run?.kind === "hint" ? "Hint" : "AI lab partner"}
          view={view}
          resolver={{ label: obsLabelFor, onSelect: (id) => cs.setView({ kind: "observation", id }) }}
          onCancel={() => void cs.cancelAgent()}
          onDismiss={() => runs.clear("case")}
        />
        <InstrumentsCard c={c} />
        <ObservationsCard c={c} />
        <EvidenceCard c={c} />
        <DiagnosisCard c={c} />
        <RepairSection c={c} />
        <CloseCard c={c} />
      </Fragment>
    </div>
  );
}
