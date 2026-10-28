// Shown only after the case is closed: the actual fault, the published seed, and the two
// separate outcomes (exact identification vs functional recovery).
import { useEffect, useRef } from "react";
import type { PublicCase } from "../../api/types";
import { Badge, Button } from "../../components/ui/primitives";
import { formatSi } from "../../lib/format";

const IDENT: Record<string, { text: string; tone: "success" | "danger" | "neutral" | "warning" }> = {
  correct: { text: "Correct", tone: "success" },
  incorrect: { text: "Incorrect", tone: "danger" },
  abstained: { text: "Abstained (not enough evidence)", tone: "neutral" },
  not_submitted: { text: "No diagnosis submitted", tone: "neutral" },
};

const RECOVERY: Record<string, { text: string; tone: "success" | "danger" | "neutral" }> = {
  restored: { text: "Restored", tone: "success" },
  not_restored: { text: "Not restored", tone: "danger" },
  not_attempted: { text: "Not attempted", tone: "neutral" },
  not_needed: { text: "Not needed (no fault)", tone: "neutral" },
};

export function RevealCard({ c, onNew, creating, focusOnMount = false }: { c: PublicCase; onNew: () => void; creating: boolean; focusOnMount?: boolean }) {
  const reveal = c.reveal;
  const headingRef = useRef<HTMLHeadingElement>(null);
  // When the reveal appears after closing, bring it into view and move focus to it.
  useEffect(() => {
    if (focusOnMount) headingRef.current?.focus();
  }, [focusOnMount]);
  if (!reveal) return null;
  const f = reveal.fault;
  const o = reveal.outcome;
  const unit = f.component_id === "R1" ? "ohm" : f.component_id === "C1" ? "farad" : null;
  const ident = IDENT[o.exact_identification] ?? IDENT.not_submitted!;
  const recovery = RECOVERY[o.functional_recovery] ?? RECOVERY.not_attempted!;
  return (
    <section className="panel-card panel-card--focus reveal" aria-labelledby="reveal-heading">
      <div className="panel-card__header">
        <h2 id="reveal-heading" ref={headingRef} tabIndex={-1} className="panel-card__title">
          Revealed
        </h2>
        <Badge icon="lock">Case closed</Badge>
      </div>
      <div className="reveal__answer">
        <p className="eyebrow">The actual fault</p>
        <p className="reveal__fault">{f.description}</p>
        {f.component_id && f.nominal_value_si != null && f.actual_value_si != null ? (
          <p className="reveal__change num">
            {f.component_id}: nominal <span className="reveal__value">{formatSi(f.nominal_value_si, unit)}</span> → actual{" "}
            <span className="reveal__value">{formatSi(f.actual_value_si, unit)}</span>
            {f.multiplier != null ? ` (${f.multiplier}×)` : ""}
          </p>
        ) : null}
      </div>
      {/* Two separate outcomes: naming the cause and restoring the behaviour are scored apart. */}
      <dl className="kv reveal__outcomes">
        <div className={`reveal__outcome is-${ident.tone}`}>
          <dt>Exact identification</dt>
          <dd>
            <Badge tone={ident.tone}>{ident.text}</Badge>
          </dd>
        </div>
        <div className={`reveal__outcome is-${recovery.tone}`}>
          <dt>Functional recovery</dt>
          <dd>
            <Badge tone={recovery.tone}>{recovery.text}</Badge>
          </dd>
        </div>
      </dl>
      <dl className="kv reveal__details">
        {o.true_cause_among_candidates != null ? (
          <>
            <dt>True cause among your candidates</dt>
            <dd>{o.true_cause_among_candidates ? "Yes" : "No"}</dd>
          </>
        ) : null}
        <dt>Unsupported certainty</dt>
        <dd>{o.unsupported_certainty ? "Yes: the conclusion went beyond the evidence" : "No"}</dd>
        <dt>Experiments used</dt>
        <dd className="num">
          {o.experiments_used}
          {o.experiments_by_user != null ? ` (you ${o.experiments_by_user}, AI ${o.experiments_by_agent ?? 0})` : ""}
        </dd>
        {o.hints_used != null ? (
          <>
            <dt>Hints used</dt>
            <dd className="num">{o.hints_used}</dd>
          </>
        ) : null}
        <dt>Seed</dt>
        <dd className="mono reveal__seed">{reveal.seed}</dd>
        <dt>Generator</dt>
        <dd className="mono">{reveal.generator_version}</dd>
      </dl>
      <p className="panel-card__note">The seed is published now that the case is closed, so the case can be reproduced.</p>
      <Button variant="primary" icon="investigate" loading={creating} onClick={onNew}>
        New mystery
      </Button>
    </section>
  );
}
