// Repair: replace ONE component with a part of its intended nominal value, then verify the
// repaired circuit against the intended design on the declared grid (numbers + audio).
// Functional recovery is reported here; exact identification is only scored at reveal.
import { useId, useState, type FormEvent } from "react";
import type { PublicCase } from "../../api/types";
import { usePlayback } from "../../components/audio/PlaybackProvider";
import { Badge, Button, Callout, ErrorCallout } from "../../components/ui/primitives";
import { formatDb, formatDeg, formatHz } from "../../lib/format";
import { FOCUS, focusWhenReady } from "../../lib/focus";
import { useCase } from "./caseStore";
import { dbBelow, floorsOf, NO_OUTPUT } from "./floors";

export function RepairSection({ c }: { c: PublicCase }) {
  const cs = useCase();
  const playback = usePlayback();
  const id = useId();
  const editable = c.intended_revision.components.filter((comp) => comp.editable);
  const [component, setComponent] = useState<"R1" | "C1">(c.repair_proposal?.component_id ?? "R1");
  const [rationale, setRationale] = useState(c.repair_proposal?.rationale ?? "");
  const canPropose = c.available_actions.includes("propose_repair");
  const canVerify = c.available_actions.includes("verify_repair");
  const proposal = c.repair_proposal;
  const repair = c.repair;

  if (!repair && !proposal && !canPropose) return null;

  // Points where either side has no measurable output: no difference is computed against them.
  const floors = floorsOf(c);
  const flooredPoints = (repair?.points ?? []).filter((p) => dbBelow(p.repaired_gain_db, floors) || dbBelow(p.expected_gain_db, floors));
  const nominal = (cid: string) => c.intended_revision.components.find((x) => x.id === cid)?.display_value ?? "";

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const ok = await cs.proposeRepair(component, rationale.trim());
    // The next step is verifying; land there (it reads the proposal via aria-describedby).
    if (ok) focusWhenReady(FOCUS.repairVerify, { frames: 10, fallbackId: FOCUS.repairHeading });
  };

  return (
    <section className="panel-card" aria-labelledby="repair-heading">
      <div className="panel-card__header">
        <h2 id={FOCUS.repairHeading} tabIndex={-1} className="panel-card__title">
          Repair
        </h2>
        {repair?.status === "succeeded" ? (
          repair.functional_recovery ? (
            <Badge tone="success">Behaviour restored</Badge>
          ) : (
            <Badge tone="danger">Not restored</Badge>
          )
        ) : null}
      </div>

      {repair ? (
        <div className="stack-2">
          <p>
            Replaced <strong>{repair.proposal.component_id}</strong> with a {repair.proposal.display_value} part (nominal).
          </p>
          {repair.status === "succeeded" ? (
            <>
              <dl className="kv num">
                {flooredPoints.length ? (
                  <>
                    <dt>Repaired output</dt>
                    <dd>
                      {NO_OUTPUT} at {flooredPoints.length} of {(repair.points ?? []).length} frequencies
                      {flooredPoints.length === (repair.points ?? []).length ? "" : ` (from ${formatHz(flooredPoints[0]!.frequency_hz)})`}
                    </dd>
                    <dt>Largest difference</dt>
                    <dd>Not computed: there is no measurable output to compare.</dd>
                  </>
                ) : (
                  <>
                    <dt>Largest gain difference</dt>
                    <dd>{repair.max_gain_deviation_db != null ? formatDb(repair.max_gain_deviation_db, 2) : "—"}</dd>
                    <dt>Largest phase difference</dt>
                    <dd>{repair.max_phase_deviation_deg != null ? formatDeg(repair.max_phase_deviation_deg) : "—"}</dd>
                    <dt>Worst frequency</dt>
                    <dd>{repair.worst_frequency_hz != null ? formatHz(repair.worst_frequency_hz) : "—"}</dd>
                  </>
                )}
                <dt>Tolerance</dt>
                <dd>
                  ±{repair.criteria.gain_tolerance_db} dB, ±{repair.criteria.phase_tolerance_deg}° on {repair.criteria.frequency_hz.length} frequencies
                </dd>
              </dl>
              <p className="panel-card__note">{repair.summary}</p>
              <div className="row row--wrap">
                {repair.audio?.clips.map((clip) => (
                  <Button key={clip.artifact_id} size="sm" icon="play" onClick={() => playback.playClip(repair.audio!, "Repair verification", clip.artifact_id)}>
                    Play {clip.label}
                  </Button>
                ))}
                <Button size="sm" variant="ghost" onClick={() => cs.setView({ kind: "repair" })}>
                  Show in instruments
                </Button>
              </div>
              <p className="panel-card__note">Verified numerically against the intended design. Whether the diagnosis named the true cause is reported at reveal.</p>
            </>
          ) : (
            <Callout tone="danger" title="The repair could not be verified">
              {repair.error?.message ?? repair.summary}
            </Callout>
          )}
        </div>
      ) : (
        <>
          {proposal ? (
            <div id={`${id}-proposal`}>
            <Callout tone="neutral" icon="edit" title="Proposed repair">
              {proposal.proposed_by === "agent" ? "The AI proposed: " : "You proposed: "}replace {proposal.component_id} with a {proposal.display_value} part
              (its nominal value).{proposal.rationale ? ` ${proposal.rationale}` : ""}
            </Callout>
            </div>
          ) : null}
          {canPropose ? (
            <form className="stack-3" onSubmit={(e) => void submit(e)} aria-label="Propose a repair">
              <fieldset className="form-group">
                <legend className="field__label">Replace one component with its intended part</legend>
                {editable.map((comp) => (
                  <label key={comp.id} className="choice">
                    <input
                      type="radio"
                      name={`${id}-component`}
                      checked={component === comp.id}
                      onChange={() => setComponent(comp.id as "R1" | "C1")}
                    />
                    <span>
                      Replace {comp.id} with a {nominal(comp.id)} part <span className="muted">(nominal)</span>
                    </span>
                  </label>
                ))}
              </fieldset>
              <div className="field">
                <label className="field__label" htmlFor={`${id}-why`}>
                  Why this component? (optional)
                </label>
                <textarea id={`${id}-why`} className="textarea" maxLength={600} value={rationale} onChange={(e) => setRationale(e.target.value)} />
              </div>
              <Button type="submit" loading={cs.pending === "repair_proposal"}>
                {proposal ? "Update proposal" : "Propose repair"}
              </Button>
            </form>
          ) : null}
          {canVerify && proposal ? (
            <div className="stack-2">
              <p className="panel-card__note">Only the proposed component changes in the mystery circuit. The result is compared with the intended design; you can verify once.</p>
              <Button
                id={FOCUS.repairVerify}
                variant="primary"
                loading={cs.pending === "repair"}
                aria-describedby={`${id}-proposal`}
                onClick={() => void cs.verifyRepair().then((ok) => ok && focusWhenReady(FOCUS.repairHeading))}
              >
                Apply repair and verify
              </Button>
            </div>
          ) : null}
        </>
      )}
      {cs.error && (cs.error.action === "repair" || cs.error.action === "repair_proposal") ? <ErrorCallout error={cs.error.error} /> : null}
    </section>
  );
}
