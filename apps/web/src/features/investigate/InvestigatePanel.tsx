// Case panel: start → symptom → approach → instruments/AI run → observations → evidence →
// diagnosis → repair → close & reveal. Everything rendered here comes from PublicCase; the
// hidden fault is shown only in the reveal, after the case is closed.
import { Fragment, useEffect, useId, useState } from "react";
import type { InstrumentSpec, PublicCase } from "../../api/types";
import { usePlayback } from "../../components/audio/PlaybackProvider";
import { Badge, Button, Callout, ErrorCallout } from "../../components/ui/primitives";
import { formatDb, formatHz, num } from "../../lib/format";
import { FOCUS, focusWhenReady } from "../../lib/focus";
import { useWorkbench } from "../../state/workbench";
import { AgentRunLog } from "../agent/AgentRunLog";
import { isRunActive, useAgentRuns } from "../agent/agentRuns";
import { useCase } from "./caseStore";
import { DiagnosisForm, DiagnosisSummary } from "./DiagnosisForm";
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

function StartCard() {
  const cs = useCase();
  const wb = useWorkbench();
  const conflict = cs.error?.action === "create" && cs.error.error.code === "conflict";
  return (
    <section className="panel-card" aria-labelledby="start-heading">
      <h2 id="start-heading" className="panel-card__title">
        Circuit mystery
      </h2>
      <p className="muted">
        A low-pass circuit with its intended design visible. Its simulated behaviour may differ: a component value may have changed, or it may be
        working correctly. You get one symptom and three experiments.
      </p>
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
  return (
    <section className="panel-card" aria-labelledby="symptom-heading">
      <div className="panel-card__header">
        <h2 id={FOCUS.symptomHeading} tabIndex={-1} className="panel-card__title">
          What we observed
        </h2>
        <Badge icon={null}>Symptom · no budget used</Badge>
      </div>
      <p>{symptom.summary}</p>
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
                <td className="n">{t.expected_gain_db != null ? formatDb(t.expected_gain_db) : "—"}</td>
                <td className="n">{t.gain_db != null ? formatDb(t.gain_db) : "—"}</td>
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
      <Button size="sm" variant="primary" disabled={disabled} loading={running} onClick={() => onRun(probes.length ? probe : null)}>
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
  const wb = useWorkbench();
  if (c.status === "ready" || !isLiveStatus(c.status)) return null;
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
      <ul className="instrument-list">
        {c.instruments.map((spec) => (
          <InstrumentCard
            key={spec.kind}
            spec={spec}
            disabled={Boolean(reason) || cs.pending !== null}
            running={cs.pending === "experiment" && ranKind === spec.kind}
            onRun={(probe) => {
              setRanKind(spec.kind);
              // Focus the new measurement (the run button may become unavailable afterwards).
              void cs.runExperiment(spec.kind, probe).then((obsId) => obsId && focusWhenReady(FOCUS.observation(obsId)));
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
                      {worst.frequency_hz != null ? ` @ ${formatHz(worst.frequency_hz)}` : ""}: observed {num(worst.observed, 2)} vs predicted {num(worst.predicted, 2)} {worst.unit}
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
        <Callout tone="accent" title="What would tell them apart">
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
          <p className="case-head__title">Mystery case</p>
          <Badge tone={c.status === "failed" ? "danger" : c.status === "closed" ? "neutral" : "accent"} icon={null}>
            {STATUS_LABEL[c.status]}
          </Badge>
        </div>
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
