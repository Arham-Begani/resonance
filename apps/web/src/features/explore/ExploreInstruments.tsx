// Explore instruments: the listening station (invitation, Play, measured tone levels), the
// frequency response (gain/phase or source current), and the oscilloscope/spectrum views for the
// experiment currently shown.
import { useEffect, useRef, useState } from "react";
import type { ExperimentRecord, Goal, Revision } from "../../api/types";
import { usePlayback } from "../../components/audio/PlaybackProvider";
import { ResponseChart, type ResponsePanel } from "../../components/charts/ResponseChart";
import { SpectrumChart } from "../../components/charts/SpectrumChart";
import { WaveformChart } from "../../components/charts/WaveformChart";
import { crossing, gainDomain, magnitudeDomain } from "../../components/charts/scales";
import type { ChartState } from "../../components/charts/ChartFigure";
import { Button, Callout, ErrorCallout } from "../../components/ui/primitives";
import { formatDb, formatDeg, formatDurationMs, formatHz, formatMa, num } from "../../lib/format";
import { FOCUS } from "../../lib/focus";
import {
  currentRevision,
  experimentOrdinal,
  revisionById,
  revisionOrdinal,
  shownRecord,
  useWorkbench,
  type Probe,
} from "../../state/workbench";
import { ListenCard } from "./ListenCard";

export { ToneLevels } from "./ListenCard";

function responseSummary(record: ExperimentRecord) {
  const r = record.response;
  if (!r) return null;
  const fc = crossing(r.frequency_hz, r.gain_db, -3);
  return (
    <>
      {(record.tones ?? []).map((t, i) => (
        <span key={t.frequency_hz}>
          {i > 0 ? "; " : ""}
          <strong>
            {t.label} at {formatHz(t.frequency_hz)}: {formatDb(t.ac_gain_db)}
          </strong>
        </span>
      ))}
      {(record.tones ?? []).length ? " (AC analysis at the exact tone frequencies). " : ""}
      {fc ? <>Measured −3 dB point ≈ {formatHz(fc)}. </> : null}
      Simulated with ngspice {record.simulator.version}, 1 V AC source.
    </>
  );
}

export function goalLimits(goal: Goal | null) {
  if (!goal) return [];
  return goal.constraints.map((c) => ({
    x: c.frequency_hz,
    y: -c.limit_db,
    direction: c.kind === "preserve" ? ("above" as const) : ("below" as const),
    label: `${c.kind === "preserve" ? "≥" : "≤"} ${formatDb(-c.limit_db, 0)}`,
  }));
}

/** Gain/phase (or source current) panels for one measured response. */
export function responsePanels(record: ExperimentRecord, view: "voltage" | "current", goal: Goal | null): ResponsePanel[] {
  const r = record.response;
  if (!r) return [];
  if (view === "voltage") {
    return [
      {
        id: "gain",
        title: "Gain (dB)",
        series: [{ id: "out", label: "Output", kind: "primary", y: r.gain_db }],
        height: 124,
        valueFormat: (v) => formatDb(v, 2),
        tickFormat: (v) => num(v, 0),
        domain: gainDomain(r.gain_db, goalLimits(goal).map((l) => l.y)),
        limits: goalLimits(goal),
        pointLabels: (record.tones ?? []).map((t) => ({ x: t.frequency_hz, y: t.ac_gain_db, text: formatDb(t.ac_gain_db) })),
      },
      {
        id: "phase",
        title: "Phase (°)",
        series: [{ id: "out", label: "Output", kind: "primary", y: r.phase_deg }],
        height: 88,
        valueFormat: (v) => formatDeg(v),
        tickFormat: (v) => num(v, 0),
        include: [0],
      },
    ];
  }
  return [
    {
      id: "current",
      title: "Source current (mA, 1 V source)",
      series: [{ id: "i", label: "I(V1)", kind: "primary", y: r.source_current_ma }],
      height: 124,
      valueFormat: (v) => formatMa(v),
      tickFormat: (v) => num(v, 2),
      domain: magnitudeDomain(r.source_current_ma),
    },
    {
      id: "iphase",
      title: "Current phase (°)",
      series: [{ id: "i", label: "I(V1)", kind: "primary", y: r.source_current_phase_deg }],
      height: 88,
      valueFormat: (v) => formatDeg(v),
      tickFormat: (v) => num(v, 0),
      include: [0],
    },
  ];
}

function invitationFor(goal: Goal | null, revision: Revision | null): string | null {
  return goal?.title ?? revision?.title ?? null;
}

export function ExploreInstruments() {
  const wb = useWorkbench();
  const { state } = wb;
  const playback = usePlayback();
  const record = shownRecord(state);
  const revision = currentRevision(state);
  const running = state.pending === "experiment";
  const [view, setView] = useState<"voltage" | "current">("voltage");
  const [scope, setScope] = useState<"time" | "spectrum">("time");
  const runRef = useRef<HTMLButtonElement>(null);
  const cancelRequested = useRef(false);

  // The Cancel button disappears when the run stops; keep keyboard focus on the Run button.
  useEffect(() => {
    if (!running && cancelRequested.current) {
      cancelRequested.current = false;
      runRef.current?.focus();
    }
  }, [running]);

  // The active probe chooses the matching view and trace.
  useEffect(() => {
    if (state.probe?.kind === "current") setView("current");
    else if (state.probe?.kind === "node") setView("voltage");
  }, [state.probe]);

  const expOrdinal = experimentOrdinal(state, record?.id);
  const recRevOrdinal = revisionOrdinal(state, record?.revision_id);
  const curRevOrdinal = revisionOrdinal(state, revision?.id);
  const audioTitle = record ? `Experiment ${expOrdinal ?? ""} · Revision ${recRevOrdinal ?? ""}` : "";

  // Loading clips into the shared player never starts playback.
  useEffect(() => {
    if (state.mode === "investigate") return;
    if (record?.audio) playback.setActive(record.audio, audioTitle);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only when the shown record changes
  }, [record?.id, record?.audio, state.mode, audioTitle]);

  const isStaleRevision = Boolean(record && revision && record.revision_id !== revision.id);
  const experimentError = state.error?.op === "experiment" ? state.error.error : null;
  const chartState: ChartState = record?.response ? "ready" : running ? "loading" : "empty";
  const emptyText = running ? "Simulating…" : "No measurement yet. Run the experiment to simulate this circuit with ngspice.";
  // Everything that describes the SHOWN measurement (tone markers, listening hint) comes from
  // that measurement, which may belong to an earlier revision or even the other family.
  const tones = record?.settings.transient.tones ?? revision?.source.tones ?? [];
  // Goals are per family: never draw one family's limits over the other family's result.
  const sameFamily = !record || revisionById(state, record.revision_id)?.template_id === revision?.template_id;
  const markers = tones.map((t) => ({ x: t.frequency_hz, label: formatHz(t.frequency_hz) }));
  const panels = record ? responsePanels(record, view, sameFamily ? state.goal : null) : [];
  const opening = Boolean(revision && !revision.parent_id && !revision.changes?.length && !isStaleRevision);
  const eyebrow = opening ? "Opening experiment" : record ? `Experiment ${expOrdinal} · Revision ${recRevOrdinal}` : `Revision ${curRevOrdinal ?? ""}`;

  const probeTrace = (probe: Probe) => (probe?.kind === "node" ? (probe.node === "in" ? "in" : "out") : null);
  const scopeSwitch = (
    <div className="segmented" role="radiogroup" aria-label="Time or frequency view">
      <label>
        <input type="radio" name="scope-view" checked={scope === "time"} onChange={() => setScope("time")} />
        Time domain
      </label>
      <label>
        <input type="radio" name="scope-view" checked={scope === "spectrum"} onChange={() => setScope("spectrum")} />
        Spectrum
      </label>
    </div>
  );

  return (
    <section className="instruments" aria-labelledby="instruments-heading">
      <div className="instruments__header">
        <h2 id="instruments-heading" className="section-title">
          Instruments
        </h2>
        {record ? (
          <p className="instruments__meta num">
            Experiment {expOrdinal} · Revision {recRevOrdinal} · ngspice {record.simulator.version} · {formatDurationMs(record.duration_ms)}
          </p>
        ) : null}
        <div className="instruments__actions">
          {running ? (
            <Button
              onClick={() => {
                cancelRequested.current = true;
                wb.cancelExperiment();
              }}
              variant="danger"
              size="md"
            >
              Cancel
            </Button>
          ) : null}
          <Button ref={runRef} id={FOCUS.runExperiment} variant="primary" icon="flask" loading={running} disabled={!revision || state.status !== "ready"} onClick={() => void wb.runExperiment()}>
            {running ? "Simulating…" : `Run experiment${curRevOrdinal ? ` on Revision ${curRevOrdinal}` : ""}`}
          </Button>
        </div>
      </div>

      {experimentError ? <ErrorCallout error={experimentError} onRetry={experimentError.code === "cancelled" ? undefined : () => void wb.runExperiment()} extra={record ? " The previous result is still shown." : null} /> : null}

      {isStaleRevision && !running ? (
        <Callout tone="accent" title={`Showing Experiment ${expOrdinal} from Revision ${recRevOrdinal}`}>
          Revision {curRevOrdinal} has not been measured yet. Run the experiment to compare.
        </Callout>
      ) : null}

      {state.status === "loading" && !revision ? (
        <div className="listen listen--skeleton" aria-hidden="true">
          <div className="skeleton listen__skeleton" />
          <div className="skeleton listen__skeleton" />
        </div>
      ) : (
        <ListenCard invitation={invitationFor(state.goal, revision)} tones={tones} record={record} audioTitle={audioTitle} eyebrow={eyebrow} running={running} />
      )}

      <div className="instruments__grid">
        <ResponseChart
          title="Frequency response"
          frequency={record?.response?.frequency_hz ?? []}
          panels={panels}
          markers={markers}
          summary={record ? responseSummary(record) : null}
          state={chartState}
          emptyText={emptyText}
          stale={running && Boolean(record)}
          tableCaption={`Measured frequency response, Experiment ${expOrdinal ?? ""} (ngspice AC analysis, 1 V source).`}
          toolbar={
            <div className="segmented" role="radiogroup" aria-label="Frequency response quantity">
              <label>
                <input type="radio" name="fr-view" checked={view === "voltage"} onChange={() => setView("voltage")} />
                Voltage gain
              </label>
              <label>
                <input type="radio" name="fr-view" checked={view === "current"} onChange={() => setView("current")} />
                Source current
              </label>
            </div>
          }
        />
        {scope === "time" ? (
          <WaveformChart
            title="Time domain"
            toolbar={scopeSwitch}
            height={212}
            sampleRateHz={record?.waveform?.sample_rate_hz ?? 48_000}
            startS={record?.waveform?.start_s ?? 0}
            series={
              record?.waveform
                ? [
                    { id: "in", label: "Input", kind: "reference", y: record.waveform.input_v },
                    { id: "out", label: "Output", kind: "primary", y: record.waveform.output_v },
                  ]
                : []
            }
            emphasis={probeTrace(state.probe)}
            summary={
              record && (record.tones ?? []).length ? (
                <>
                  Steady-state window after {num(record.settings.transient.settle_s * 1000, 0)} ms of settling.{" "}
                  {(record.tones ?? []).map((t, i) => (
                    <span key={t.frequency_hz}>
                      {i > 0 ? "; " : ""}
                      <strong>{t.label}</strong> output {num(t.output_amplitude_v, 3)} V of {num(t.input_amplitude_v, 3)} V
                    </span>
                  ))}
                  .
                </>
              ) : null
            }
            state={record?.waveform ? "ready" : running ? "loading" : "empty"}
            emptyText={emptyText}
            stale={running && Boolean(record)}
          />
        ) : (
          <SpectrumChart
            toolbar={scopeSwitch}
            spectrum={record?.spectrum}
            tones={tones}
            state={record?.spectrum ? "ready" : running ? "loading" : "empty"}
            emptyText={emptyText}
            stale={running && Boolean(record)}
          />
        )}
      </div>

      {record && (record.checks ?? []).length ? (
        <details className="disclosure">
          <summary>
            Consistency checks ({(record.checks ?? []).filter((c) => c.passed).length} of {(record.checks ?? []).length} passed)
          </summary>
          <ul className="checks">
            {(record.checks ?? []).map((c) => (
              <li key={c.name} className={c.passed ? "is-pass" : "is-fail"}>
                <strong>{c.passed ? "Passed" : "Failed"}</strong> · {c.name}: {c.detail}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </section>
  );
}

export function revisionLabel(revision: Revision | null, ordinal: number | null): string {
  if (!revision) return "";
  return `Revision ${ordinal ?? "?"}`;
}
