// Learn instruments: after an attempt, the before/after comparison (measured tone changes and the
// two frequency responses overlaid, previous dashed and new solid); otherwise, and on request,
// the standard instruments for the current revision.
import { useEffect, useState } from "react";
import type { ExperimentRecord, LearnAttempt } from "../../api/types";
import { usePlayback } from "../../components/audio/PlaybackProvider";
import { ResponseChart, type ResponsePanel } from "../../components/charts/ResponseChart";
import { gainDomain } from "../../components/charts/scales";
import { formatDb, formatDeg, formatHz, formatSignedDb, num } from "../../lib/format";
import { experimentOrdinal, readableIds, revisionOrdinal, useWorkbench } from "../../state/workbench";
import { ExploreInstruments } from "../explore/ExploreInstruments";
import { useLearn } from "./learnStore";
import { changeText } from "./learnText";
import "../explore/explore.css";
import "./learn.css";

function comparisonPanels(before: ExperimentRecord, after: ExperimentRecord): ResponsePanel[] {
  const a = before.response!;
  const b = after.response!;
  return [
    {
      id: "gain",
      title: "Gain (dB)",
      series: [
        { id: "before", label: "Previous", kind: "reference", y: a.gain_db },
        { id: "after", label: "New", kind: "primary", y: b.gain_db },
      ],
      height: 160,
      valueFormat: (v) => formatDb(v, 2),
      tickFormat: (v) => num(v, 0),
      domain: gainDomain([...a.gain_db, ...b.gain_db]),
      pointLabels: (after.tones ?? []).map((t) => ({ x: t.frequency_hz, y: t.ac_gain_db, text: formatDb(t.ac_gain_db) })),
    },
    {
      id: "phase",
      title: "Phase (°)",
      series: [
        { id: "before", label: "Previous", kind: "reference", y: a.phase_deg },
        { id: "after", label: "New", kind: "primary", y: b.phase_deg },
      ],
      height: 100,
      valueFormat: (v) => formatDeg(v),
      tickFormat: (v) => num(v, 0),
      include: [0],
    },
  ];
}

function ToneDeltas({ attempt }: { attempt: LearnAttempt }) {
  const roleOf = (f: number) => attempt.graded.find((g) => Math.abs(g.frequency_hz - f) < 1e-6)?.role ?? null;
  return (
    <ul className="tone-levels" aria-label="Measured tone changes">
      {attempt.comparison.tones.map((t) => (
        <li key={t.frequency_hz} className="tone-level" data-role={roleOf(t.frequency_hz) ?? undefined}>
          <p className="tone-level__label">
            <span className="tone-level__name">{t.label}</span> · <span className="num">{formatHz(t.frequency_hz)}</span>
          </p>
          <p className="tone-level__value num">{formatSignedDb(t.delta_db)}</p>
          <p className="tone-level__detail num">
            {formatDb(t.a_gain_db)} → {formatDb(t.b_gain_db)}
          </p>
        </li>
      ))}
    </ul>
  );
}

function BeforeAfter({ attempt, index }: { attempt: LearnAttempt; index: number }) {
  const wb = useWorkbench();
  const { state } = wb;
  const playback = usePlayback();
  const before = state.records[attempt.base_experiment_id] ?? null;
  const after = state.records[attempt.new_experiment_id] ?? null;
  const missing = !before || !after;

  // Records of older attempts may not be loaded yet (e.g. after a reload).
  useEffect(() => {
    if (!before) void wb.loadExperiment(attempt.base_experiment_id);
    if (!after) void wb.loadExperiment(attempt.new_experiment_id);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- load once per attempt
  }, [attempt.id]);

  // The shared player offers this attempt's Previous/New clips (loading never plays).
  useEffect(() => {
    if (attempt.comparison.audio) playback.setActive(attempt.comparison.audio, `Attempt ${index}: before and after`);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only when the attempt changes
  }, [attempt.id]);

  const ordA = experimentOrdinal(state, attempt.base_experiment_id);
  const ordB = experimentOrdinal(state, attempt.new_experiment_id);
  const revA = revisionOrdinal(state, attempt.base_revision_id);
  const revB = revisionOrdinal(state, attempt.new_revision_id);
  const tones = after?.settings.transient.tones ?? before?.settings.transient.tones ?? [];
  const ready = Boolean(before?.response && after?.response);

  return (
    <>
      <p className="instruments__meta num">
        Attempt {index} · {changeText(attempt)} · Experiment {ordA ?? "?"} (Revision {revA ?? "?"}) → Experiment {ordB ?? "?"} (Revision {revB ?? "?"})
      </p>
      <ToneDeltas attempt={attempt} />
      <ResponseChart
        title="Before and after"
        frequency={before?.response?.frequency_hz ?? []}
        panels={ready ? comparisonPanels(before!, after!) : []}
        markers={tones.map((t) => ({ x: t.frequency_hz, label: formatHz(t.frequency_hz) }))}
        summary={readableIds(state, attempt.comparison.summary)}
        state={ready ? "ready" : missing ? "loading" : "empty"}
        emptyText="These experiments have no frequency response to compare."
        tableCaption={`Measured gain and phase before (Experiment ${ordA ?? "?"}) and after (Experiment ${ordB ?? "?"}) the change.`}
      />
    </>
  );
}

export function LearnInstruments() {
  const learn = useLearn();
  const [view, setView] = useState<"compare" | "details">("compare");
  const attempt = learn.activeAttempt;
  const index = attempt ? learn.attempts.indexOf(attempt) + 1 : 0;

  // A new attempt always opens on its comparison.
  useEffect(() => {
    if (attempt) setView("compare");
  }, [attempt?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!attempt || view === "details") {
    return (
      <div className="stack-4">
        {attempt ? <ViewSwitch view={view} onChange={setView} /> : null}
        <ExploreInstruments />
      </div>
    );
  }

  return (
    <section className="instruments" aria-labelledby="instruments-heading">
      <div className="instruments__header">
        <h2 id="instruments-heading" className="section-title">
          Instruments
        </h2>
        <ViewSwitch view={view} onChange={setView} />
      </div>
      <BeforeAfter attempt={attempt} index={index} />
    </section>
  );
}

function ViewSwitch({ view, onChange }: { view: "compare" | "details"; onChange: (v: "compare" | "details") => void }) {
  return (
    <div className="segmented instruments__actions" role="radiogroup" aria-label="Learn view">
      <label>
        <input type="radio" name="learn-view" checked={view === "compare"} onChange={() => onChange("compare")} />
        Before and after
      </label>
      <label>
        <input type="radio" name="learn-view" checked={view === "details"} onChange={() => onChange("details")} />
        Measurement details
      </label>
    </div>
  );
}
