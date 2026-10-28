// Diagnosis form (submitted once). Mirrors the server's rules: a fault needs a hypothesis and at
// least one cited observation; no fault needs a citation; candidates and a suggested instrument
// only apply to "not enough evidence". The server re-validates and records an evidence check.
import { useId, useRef, useState, type FormEvent } from "react";
import type {
  DiagnosisConclusion,
  DiagnosisRecord,
  DiagnosisSubmission,
  EvidenceCitation,
  FaultHypothesisId,
  HypothesisId,
  InstrumentKind,
  Observation,
  PublicCase,
} from "../../api/types";
import { Badge, Button, Callout, type Tone } from "../../components/ui/primitives";
import { formatHz } from "../../lib/format";
import { Markdown } from "../agent/Markdown";
import { citableQuantities, hypothesisLabel, INSTRUMENT_SHORT, observationLabel, observationTitle, quantityLabel } from "./labels";

interface CitationDraft {
  checked: boolean;
  quantity: string;
  frequency: string; // "" = not specific
}

function frequenciesFor(o: Observation): number[] {
  if (o.sweep) return o.sweep.frequency_hz;
  if (o.transient) return o.transient.tones.map((t) => t.frequency_hz);
  return [];
}

export interface DiagnosisErrors {
  hypothesis?: string;
  citations?: string;
  candidates?: string;
}

export function validateDiagnosis(conclusion: DiagnosisConclusion | null, hypothesis: string, citations: EvidenceCitation[], candidates: HypothesisId[]): DiagnosisErrors & { conclusion?: string } {
  const errors: DiagnosisErrors & { conclusion?: string } = {};
  if (!conclusion) errors.conclusion = "Choose a conclusion.";
  if (conclusion === "fault" && !hypothesis) errors.hypothesis = "Choose which fault you think it is.";
  if ((conclusion === "fault" || conclusion === "no_fault") && citations.length === 0)
    errors.citations = "Cite at least one observation that supports this conclusion.";
  if (conclusion === "insufficient_evidence" && candidates.length === 0) errors.candidates = "Tick the explanations that still fit.";
  return errors;
}

export function DiagnosisForm({ c, submitting, onSubmit }: { c: PublicCase; submitting: boolean; onSubmit: (s: DiagnosisSubmission) => Promise<boolean> }) {
  const id = useId();
  const observations = c.observations.filter((o) => o.status === "succeeded");
  const faults = c.hypotheses.filter((h) => h.id !== "no_fault");
  const [conclusion, setConclusion] = useState<DiagnosisConclusion | null>(null);
  const [hypothesis, setHypothesis] = useState<string>("");
  const [drafts, setDrafts] = useState<Record<string, CitationDraft>>(() =>
    Object.fromEntries(observations.map((o) => [o.id, { checked: false, quantity: citableQuantities(o)[0] ?? "", frequency: "" }])),
  );
  // Until the learner edits the ticks, they follow the CURRENT evidence (explanations that are
  // not ruled out), so new measurements never leave ruled-out explanations pre-ticked.
  const [editedCandidates, setEditedCandidates] = useState<HypothesisId[] | null>(null);
  const candidates = editedCandidates ?? c.evidence.remaining;
  const [instrument, setInstrument] = useState<string>("");
  const [explanation, setExplanation] = useState("");
  const [errors, setErrors] = useState<ReturnType<typeof validateDiagnosis>>({});
  const formRef = useRef<HTMLFormElement>(null);

  const draftFor = (o: Observation): CitationDraft => drafts[o.id] ?? { checked: false, quantity: citableQuantities(o)[0] ?? "", frequency: "" };
  // Observations recorded after the form opened have no draft yet; start from their defaults.
  const setDraft = (o: Observation, patch: Partial<CitationDraft>) =>
    setDrafts((prev) => ({ ...prev, [o.id]: { ...(prev[o.id] ?? draftFor(o)), ...patch } }));

  const citations: EvidenceCitation[] = observations
    .filter((o) => draftFor(o).checked)
    .map((o) => {
      const d = draftFor(o);
      return {
        observation_id: o.id,
        quantity: d.quantity as EvidenceCitation["quantity"],
        frequency_hz: d.frequency ? Number(d.frequency) : null,
        note: "",
      };
    });

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const found = validateDiagnosis(conclusion, hypothesis, citations, candidates);
    setErrors(found);
    if (Object.keys(found).length > 0) {
      // Move focus to the first invalid control so keyboard and screen-reader users land on it.
      requestAnimationFrame(() => {
        const el = formRef.current?.querySelector<HTMLElement>("[aria-invalid='true']");
        const target = el?.matches("input, select, textarea") ? el : el?.querySelector<HTMLElement>("input, select, textarea");
        target?.focus();
      });
      return;
    }
    const submission: DiagnosisSubmission = {
      conclusion: conclusion!,
      hypothesis_id: conclusion === "fault" ? (hypothesis as FaultHypothesisId) : null,
      candidate_hypothesis_ids: conclusion === "insufficient_evidence" ? [...candidates] : [],
      citations,
      explanation: explanation.trim(),
      suggested_instrument: conclusion === "insufficient_evidence" && instrument ? (instrument as InstrumentKind) : null,
    };
    await onSubmit(submission);
  };

  const errorFor = (key: keyof typeof errors) =>
    errors[key] ? (
      <p className="field__error" id={`${id}-${key}-err`}>
        {errors[key]}
      </p>
    ) : null;

  return (
    <form ref={formRef} className="stack-3" onSubmit={(e) => void submit(e)} aria-label="Submit a diagnosis" noValidate>
      <div
        className="form-group"
        role="radiogroup"
        aria-labelledby={`${id}-conclusion-label`}
        aria-invalid={Boolean(errors.conclusion) || undefined}
        aria-describedby={errors.conclusion ? `${id}-conclusion-err` : undefined}
      >
        <p id={`${id}-conclusion-label`} className="field__label">
          Conclusion
        </p>
        {(
          [
            ["fault", "A component fault", "Name the change you think happened."],
            ["no_fault", "No fault", "The circuit behaves like the intended design."],
            ["insufficient_evidence", "Not enough evidence", "More than one explanation still fits."],
          ] as const
        ).map(([value, label, hint]) => (
          <label key={value} className="choice">
            <input
              type="radio"
              name={`${id}-conclusion`}
              value={value}
              checked={conclusion === value}
              onChange={() => setConclusion(value)}
            />
            <span>
              {label}
              <span className="choice__hint">{hint}</span>
            </span>
          </label>
        ))}
        {errorFor("conclusion")}
      </div>

      {conclusion === "fault" ? (
        <div className="field">
          <label className="field__label" htmlFor={`${id}-hyp`}>
            Which fault?
          </label>
          <select
            id={`${id}-hyp`}
            className="select"
            value={hypothesis}
            aria-invalid={Boolean(errors.hypothesis) || undefined}
            aria-describedby={errors.hypothesis ? `${id}-hypothesis-err` : undefined}
            onChange={(e) => setHypothesis(e.target.value)}
          >
            <option value="">Choose…</option>
            {faults.map((h) => (
              <option key={h.id} value={h.id}>
                {h.label}
              </option>
            ))}
          </select>
          {errorFor("hypothesis")}
        </div>
      ) : null}

      {conclusion === "insufficient_evidence" ? (
        <fieldset className="form-group">
          <legend className="field__label">Explanations that still fit</legend>
          {c.hypotheses.map((h) => (
            <label key={h.id} className="choice">
              <input
                type="checkbox"
                className="checkbox"
                checked={candidates.includes(h.id)}
                aria-invalid={Boolean(errors.candidates) || undefined}
                onChange={(e) =>
                  setEditedCandidates(e.target.checked ? [...candidates, h.id] : candidates.filter((x) => x !== h.id))
                }
              />
              <span>{h.label}</span>
            </label>
          ))}
          {errorFor("candidates")}
          <div className="field">
            <label className="field__label" htmlFor={`${id}-inst`}>
              Which measurement would help? (optional)
            </label>
            <select id={`${id}-inst`} className="select" value={instrument} onChange={(e) => setInstrument(e.target.value)}>
              <option value="">Not sure</option>
              {c.instruments.map((i) => (
                <option key={i.kind} value={i.kind}>
                  {i.title || INSTRUMENT_SHORT[i.kind]}
                </option>
              ))}
            </select>
          </div>
        </fieldset>
      ) : null}

      {conclusion ? (
        <fieldset className="form-group">
          <legend className="field__label">
            Evidence {conclusion === "insufficient_evidence" ? "(optional)" : "(at least one)"}
          </legend>
          {observations.map((o) => {
            const d = draftFor(o);
            const quantities = citableQuantities(o);
            const freqs = frequenciesFor(o);
            return (
              <div key={o.id} className="citation-row">
                <label className="choice">
                  <input
                    type="checkbox"
                    className="checkbox"
                    checked={d.checked}
                    aria-invalid={Boolean(errors.citations) || undefined}
                    onChange={(e) => setDraft(o, { checked: e.target.checked })}
                  />
                  <span>
                    {observationLabel(o)} · {observationTitle(o)}
                  </span>
                </label>
                {d.checked ? (
                  <div className="citation-row__detail">
                    <label className="visually-hidden" htmlFor={`${id}-${o.id}-q`}>
                      Quantity cited from {observationLabel(o)}
                    </label>
                    <select id={`${id}-${o.id}-q`} className="select" value={d.quantity} onChange={(e) => setDraft(o, { quantity: e.target.value })}>
                      {quantities.map((q) => (
                        <option key={q} value={q}>
                          {quantityLabel(q)}
                        </option>
                      ))}
                    </select>
                    <label className="visually-hidden" htmlFor={`${id}-${o.id}-f`}>
                      Frequency cited from {observationLabel(o)}
                    </label>
                    <select id={`${id}-${o.id}-f`} className="select" value={d.frequency} onChange={(e) => setDraft(o, { frequency: e.target.value })}>
                      <option value="">Whole measurement</option>
                      {freqs.map((f) => (
                        <option key={f} value={String(f)}>
                          {formatHz(f)}
                        </option>
                      ))}
                    </select>
                  </div>
                ) : null}
              </div>
            );
          })}
          {errorFor("citations")}
        </fieldset>
      ) : null}

      <div className="field">
        <label className="field__label" htmlFor={`${id}-why`}>
          Explanation (optional)
        </label>
        <textarea
          id={`${id}-why`}
          className="textarea"
          maxLength={1200}
          value={explanation}
          onChange={(e) => setExplanation(e.target.value)}
          placeholder="What did the measurements show?"
        />
      </div>

      <p className="panel-card__note">You can submit one diagnosis. It is checked against the recorded evidence, not against the hidden answer.</p>
      <Button type="submit" variant="primary" loading={submitting}>
        Submit diagnosis
      </Button>
    </form>
  );
}

const CHECK_TONE: Record<DiagnosisRecord["evidence_check"]["status"], Tone> = {
  supported: "success",
  ambiguous: "warning",
  contradicted: "danger",
  consistent_abstention: "success",
  premature_abstention: "warning",
  no_supported_hypothesis: "warning",
};

const CHECK_LABEL: Record<DiagnosisRecord["evidence_check"]["status"], string> = {
  supported: "Supported by the evidence",
  ambiguous: "Ambiguous: other explanations also fit",
  contradicted: "Contradicted by the evidence",
  consistent_abstention: "Consistent: the evidence is ambiguous",
  premature_abstention: "The evidence already pointed to one explanation",
  no_supported_hypothesis: "No catalogued explanation fits",
};

export function DiagnosisSummary({ c, record }: { c: PublicCase; record: DiagnosisRecord }) {
  const s = record.submission;
  const candidates = s.candidate_hypothesis_ids ?? [];
  const cited = s.citations ?? [];
  const check = record.evidence_check;
  const conclusionText =
    s.conclusion === "fault" ? hypothesisLabel(c, s.hypothesis_id) : s.conclusion === "no_fault" ? "No fault" : "Not enough evidence to decide";
  const obsLabel = (oid: string) => {
    const o = c.observations.find((x) => x.id === oid);
    return o ? observationLabel(o) : "Observation";
  };
  return (
    <div className="stack-2">
      <p>
        <span className="muted">{record.submitted_by === "agent" ? "AI diagnosis:" : "Your diagnosis:"}</span> <strong>{conclusionText}</strong>
      </p>
      {s.conclusion === "insufficient_evidence" && candidates.length ? (
        <p className="panel-card__note">Still possible: {candidates.map((h) => hypothesisLabel(c, h)).join("; ")}</p>
      ) : null}
      {s.suggested_instrument ? <p className="panel-card__note">Suggested next measurement: {INSTRUMENT_SHORT[s.suggested_instrument]}</p> : null}
      {cited.length ? (
        <p className="agent-item__citations">
          {cited.map((ct, i) => (
            <span key={`${ct.observation_id}-${i}`} className="citation">
              {obsLabel(ct.observation_id)} · {quantityLabel(ct.quantity)}
              {ct.frequency_hz != null ? ` @ ${formatHz(ct.frequency_hz)}` : ""}
            </span>
          ))}
        </p>
      ) : null}
      {s.explanation ? (
        record.submitted_by === "agent" ? (
          // The AI writes light Markdown; render the safe subset. A learner's own text stays verbatim.
          <Markdown className="diagnosis__explanation diagnosis__explanation--md" text={s.explanation} />
        ) : (
          <p className="diagnosis__explanation">{s.explanation}</p>
        )
      ) : null}
      <Callout tone={CHECK_TONE[check.status] === "neutral" ? "neutral" : (CHECK_TONE[check.status] as "success" | "warning" | "danger")} title={CHECK_LABEL[check.status]}>
        {check.message}
      </Callout>
      <p className="panel-card__note">
        <Badge icon={null}>Evidence check</Badge> Recorded by trusted code from the observations; whether the diagnosis is actually correct is only known at reveal.
      </p>
    </div>
  );
}
