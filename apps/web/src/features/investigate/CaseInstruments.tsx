// Central instruments area during a case: the selected observation (expected vs observed) or
// the repair verification (expected vs repaired). Everything shown comes from PublicCase.
import { useEffect } from "react";
import type { Observation, ObservedTone, PublicCase, RepairVerification } from "../../api/types";
import { usePlayback } from "../../components/audio/PlaybackProvider";
import { ResponseChart, type ResponsePanel } from "../../components/charts/ResponseChart";
import { WaveformChart } from "../../components/charts/WaveformChart";
import { gainDomain, magnitudeDomain } from "../../components/charts/scales";
import { Badge, Callout } from "../../components/ui/primitives";
import { formatDb, formatDeg, formatHz, formatMa, num } from "../../lib/format";
import { useCase } from "./caseStore";
import { observationLabel, observationTitle, requesterLabel } from "./labels";
import "./investigate.css";

function toneValue(t: ObservedTone, which: "observed" | "expected"): string {
  const amp = which === "observed" ? t.amplitude : t.expected_amplitude;
  return t.amplitude_unit === "mA" ? formatMa(amp) : `${num(amp, 3)} V`;
}

export function ToneComparisonTable({ tones, caption, observedLabel = "Observed" }: { tones: ObservedTone[]; caption: string; observedLabel?: string }) {
  const hasGain = tones.some((t) => t.gain_db != null);
  return (
    <table className="data-table tone-compare">
      <caption>{caption}</caption>
      <thead>
        <tr>
          <th scope="col">Tone</th>
          <th scope="col" className="n">
            Expected
          </th>
          <th scope="col" className="n">
            {observedLabel}
          </th>
          {hasGain ? (
            <>
              <th scope="col" className="n">
                Expected gain
              </th>
              <th scope="col" className="n">
                {observedLabel} gain
              </th>
            </>
          ) : null}
          <th scope="col" className="n">
            Phase (exp → obs)
          </th>
        </tr>
      </thead>
      <tbody>
        {tones.map((t) => (
          <tr key={t.frequency_hz}>
            <th scope="row">
              {t.label} · {formatHz(t.frequency_hz)}
            </th>
            <td className="n">{toneValue(t, "expected")}</td>
            <td className="n">{toneValue(t, "observed")}</td>
            {hasGain ? (
              <>
                <td className="n">{t.expected_gain_db != null ? formatDb(t.expected_gain_db) : "—"}</td>
                <td className="n">{t.gain_db != null ? formatDb(t.gain_db) : "—"}</td>
              </>
            ) : null}
            <td className="n">
              {formatDeg(t.expected_phase_deg)} → {formatDeg(t.phase_deg)}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function sweepPanels(o: Observation): { panels: ResponsePanel[]; summary: string } | null {
  const s = o.sweep;
  if (!s) return null;
  const isGain = s.quantity === "output_gain";
  const fmt = isGain ? (v: number) => formatDb(v, 2) : (v: number) => formatMa(v);
  return {
    panels: [
      {
        id: "mag",
        title: isGain ? "Output gain (dB)" : "Source current (mA, 1 V source)",
        series: [
          { id: "exp", label: "Expected", kind: "reference", y: s.expected_magnitude },
          { id: "obs", label: "Observed", kind: "primary", y: s.magnitude },
        ],
        height: 170,
        valueFormat: fmt,
        tickFormat: (v) => num(v, isGain ? 0 : 2),
        domain: isGain ? gainDomain([...s.magnitude, ...s.expected_magnitude]) : magnitudeDomain([...s.magnitude, ...s.expected_magnitude]),
      },
      {
        id: "phase",
        title: isGain ? "Output phase (°)" : "Current phase (°)",
        series: [
          { id: "exp", label: "Expected", kind: "reference", y: s.expected_phase_deg },
          { id: "obs", label: "Observed", kind: "primary", y: s.phase_deg },
        ],
        height: 100,
        valueFormat: (v) => formatDeg(v),
        tickFormat: (v) => num(v, 0),
        include: [0],
      },
    ],
    summary: o.summary,
  };
}

function ObservationView({ c, o }: { c: PublicCase; o: Observation }) {
  const playback = usePlayback();
  const title = `${observationLabel(o)} · ${observationTitle(o)}`;
  const audio = o.transient?.audio ?? null;

  useEffect(() => {
    playback.setActive(audio, title);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only when the observation changes
  }, [o.id, audio?.id]);

  if (o.status !== "succeeded") {
    return (
      <Callout tone="danger" title={`${title} did not complete`}>
        {o.error?.message ?? "The simulator could not complete this measurement."} {o.counted ? "" : "It did not use any of your experiment budget."}
      </Callout>
    );
  }

  const tones = c.intended_revision.source.tones;
  const markers = tones.map((t) => ({ x: t.frequency_hz, label: formatHz(t.frequency_hz) }));
  const sweep = sweepPanels(o);
  const tr = o.transient;
  const isCurrent = tr?.waveform.unit === "mA";

  return (
    <div className="stack-4">
      {sweep ? (
        <ResponseChart
          title={observationTitle(o)}
          frequency={o.sweep!.frequency_hz}
          panels={sweep.panels}
          markers={markers}
          summary={sweep.summary}
          state="ready"
          tableCaption={`${title}: observed values and the intended design's reference on the declared grid.`}
        />
      ) : null}
      {tr ? (
        <>
          <ToneComparisonTable tones={tr.tones} caption={`${title}: tone levels, intended design (expected) vs mystery circuit (observed).`} />
          <WaveformChart
            title={isCurrent ? "Source current over time" : "Output over time"}
            sampleRateHz={tr.waveform.sample_rate_hz}
            startS={tr.waveform.start_s}
            series={[
              { id: "exp", label: "Expected", kind: "reference", y: tr.waveform.expected },
              { id: "obs", label: "Observed", kind: "primary", y: tr.waveform.observed },
            ]}
            unit={tr.waveform.unit}
            summary={o.summary}
            state="ready"
          />
          {isCurrent ? <p className="panel-card__note">Current is shown, not played: only voltages are rendered as audio.</p> : null}
        </>
      ) : null}
    </div>
  );
}

function repairPanels(r: RepairVerification): ResponsePanel[] {
  return [
    {
      id: "gain",
      title: "Output gain (dB)",
      series: [
        { id: "exp", label: "Expected", kind: "reference", y: (r.points ?? []).map((p) => p.expected_gain_db) },
        { id: "rep", label: "Repaired", kind: "primary", y: (r.points ?? []).map((p) => p.repaired_gain_db) },
      ],
      height: 170,
      valueFormat: (v) => formatDb(v, 2),
      tickFormat: (v) => num(v, 0),
      domain: gainDomain((r.points ?? []).flatMap((p) => [p.expected_gain_db, p.repaired_gain_db])),
    },
    {
      id: "phase",
      title: "Output phase (°)",
      series: [
        { id: "exp", label: "Expected", kind: "reference", y: (r.points ?? []).map((p) => p.expected_phase_deg) },
        { id: "rep", label: "Repaired", kind: "primary", y: (r.points ?? []).map((p) => p.repaired_phase_deg) },
      ],
      height: 100,
      valueFormat: (v) => formatDeg(v),
      tickFormat: (v) => num(v, 0),
      include: [0],
    },
  ];
}

function RepairView({ c, r }: { c: PublicCase; r: RepairVerification }) {
  const playback = usePlayback();
  useEffect(() => {
    playback.setActive(r.audio ?? null, "Repair verification");
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only when the repair changes
  }, [r.id, r.audio?.id]);
  if (r.status !== "succeeded") {
    return (
      <Callout tone="danger" title="The repair could not be verified">
        {r.error?.message ?? r.summary}
      </Callout>
    );
  }
  const markers = c.intended_revision.source.tones.map((t) => ({ x: t.frequency_hz, label: formatHz(t.frequency_hz) }));
  return (
    <div className="stack-4">
      <ResponseChart
        title="Repair verification"
        frequency={(r.points ?? []).map((p) => p.frequency_hz)}
        panels={repairPanels(r)}
        markers={markers}
        summary={r.summary}
        state="ready"
        tableCaption={`Repaired vs intended design on the declared grid (tolerance ±${r.criteria.gain_tolerance_db} dB, ±${r.criteria.phase_tolerance_deg}°).`}
      />
      {r.tones?.length ? <ToneComparisonTable tones={r.tones} caption="Tone levels: intended design vs repaired circuit." observedLabel="Repaired" /> : null}
    </div>
  );
}

export function CaseInstruments() {
  const cs = useCase();
  const c = cs.current;
  if (!c) {
    return (
      <section className="instruments" aria-labelledby="instruments-heading">
        <h2 id="instruments-heading" className="section-title">
          Instruments
        </h2>
        <div className="empty">
          <p className="empty__title">No mystery in progress</p>
          <p>Start a mystery from the case panel. Its symptom and every measurement you take will appear here.</p>
        </div>
      </section>
    );
  }
  const view = cs.view;
  const selected = view?.kind === "observation" ? c.observations.find((o) => o.id === view.id) : null;
  const showRepair = cs.view?.kind === "repair" && c.repair;
  const options = c.observations;

  return (
    <section className="instruments" aria-labelledby="instruments-heading">
      <div className="instruments__header">
        <h2 id="instruments-heading" className="section-title">
          Instruments
        </h2>
        <div className="segmented case-views" role="radiogroup" aria-label="Measurement to show">
          {options.map((o) => (
            <label key={o.id}>
              <input
                type="radio"
                name="case-view"
                checked={cs.view?.kind === "observation" && cs.view.id === o.id}
                onChange={() => cs.setView({ kind: "observation", id: o.id })}
              />
              {observationLabel(o)}
            </label>
          ))}
          {c.repair ? (
            <label>
              <input type="radio" name="case-view" checked={cs.view?.kind === "repair"} onChange={() => cs.setView({ kind: "repair" })} />
              Repair
            </label>
          ) : null}
        </div>
      </div>
      {selected ? (
        <>
          <p className="instruments__meta">
            <Badge icon={null}>{requesterLabel(selected)}</Badge> {observationTitle(selected)} · {selected.counted ? "used 1 experiment" : "no budget used"} · ngspice{" "}
            {selected.simulator.version}
          </p>
          <ObservationView c={c} o={selected} />
        </>
      ) : showRepair ? (
        <RepairView c={c} r={c.repair!} />
      ) : (
        <p className="muted">Choose a measurement to show.</p>
      )}
    </section>
  );
}
