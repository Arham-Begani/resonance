// The listening station at the top of the instruments: the current invitation (goal), a clear
// Play affordance for the shown experiment's clips, and the two measured tone levels beside it.
// Playback is always user initiated and goes through the one shared player (single source).
import type { AudioComparison, ExperimentRecord, Tone, ToneLevel } from "../../api/types";
import { usePlayback } from "../../components/audio/PlaybackProvider";
import { Button } from "../../components/ui/primitives";
import { formatDb, formatHz, num } from "../../lib/format";
import { lowToneNote } from "../../lib/family";

function joinTones(tones: Tone[], role: Tone["role"]): string {
  return tones
    .filter((t) => t.role === role)
    .map((t) => `the ${formatHz(t.frequency_hz)} ${t.label.toLowerCase()}`)
    .join(" and ");
}

/** "Press Play to hear the 440 Hz musical tone with the 4 kHz whistle." from the template's tones. */
export function listenHint(tones: Tone[]): string {
  const wanted = joinTones(tones, "wanted");
  const interference = joinTones(tones, "interference");
  if (wanted && interference) return `Press Play to hear ${wanted} with ${interference}. Nothing plays automatically.`;
  return "Press Play to hear the input. Nothing plays automatically.";
}

const ROLE_WORD: Record<ToneLevel["role"], string> = { wanted: "Keep", interference: "Reduce" };

export function ToneLevels({ tones, pending = false }: { tones: ToneLevel[]; pending?: boolean }) {
  return (
    // While a new measurement runs, these stay at full contrast (dimmed text would fail 4.5:1);
    // the card's eyebrow says "Measuring…" and the list is marked busy instead.
    <ul className="tone-levels" aria-label="Measured tone levels" aria-busy={pending || undefined}>
      {tones.map((t) => (
        <li key={t.frequency_hz} className="tone-level" data-role={t.role}>
          <p className="tone-level__label">
            <span className="tone-level__name">{t.label}</span> · <span className="num">{formatHz(t.frequency_hz)}</span>
          </p>
          <p className="tone-level__main">
            <span className="tone-level__value num">{formatDb(t.transient_gain_db)}</span>
            <span className="tone-level__role">{ROLE_WORD[t.role]}</span>
          </p>
          <p className="tone-level__detail num">
            {num(t.output_amplitude_v, 3)} V of {num(t.input_amplitude_v, 3)} V peak
          </p>
          <p className="tone-level__detail num">AC check {formatDb(t.ac_gain_db, 2)}</p>
        </li>
      ))}
    </ul>
  );
}

/** Placeholder tiles with the tone names known before measuring (no invented numbers). */
function TonePlaceholders({ tones, loading }: { tones: Tone[]; loading: boolean }) {
  return (
    // Named differently from the measured list on purpose: nothing here is a measurement yet.
    <ul className="tone-levels" aria-label="Tone levels, not measured yet" aria-busy={loading || undefined}>
      {tones.map((t) => (
        <li key={t.frequency_hz} className="tone-level is-empty" data-role={t.role}>
          <p className="tone-level__label">
            <span className="tone-level__name">{t.label}</span> · <span className="num">{formatHz(t.frequency_hz)}</span>
          </p>
          {loading ? <div className="skeleton tone-level__skeleton" aria-hidden="true" /> : <p className="tone-level__value tone-level__value--empty">Not measured</p>}
          <p className="tone-level__detail">{loading ? "Simulating…" : "Run the experiment"}</p>
          <p className="tone-level__detail" aria-hidden="true">&nbsp;</p>
        </li>
      ))}
    </ul>
  );
}

export function PlayClipButtons({ audio, title, verb = "Play" }: { audio: AudioComparison | null | undefined; title: string; verb?: string }) {
  const playback = usePlayback();
  if (!audio?.clips.length) return null;
  return (
    <>
      {audio.clips.map((clip, i) => (
        <Button
          key={clip.artifact_id}
          size="sm"
          variant={i === 0 ? "primary" : "secondary"}
          icon="play"
          onClick={() => playback.playClip(audio, title, clip.artifact_id)}
        >
          {verb} {clip.label.toLowerCase()}
        </Button>
      ))}
    </>
  );
}

interface ListenCardProps {
  invitation: string | null;
  tones: Tone[];
  record: ExperimentRecord | null;
  audioTitle: string;
  eyebrow: string;
  running: boolean;
}

export function ListenCard({ invitation, tones, record, audioTitle, eyebrow, running }: ListenCardProps) {
  const note = lowToneNote(tones);
  const measured = record?.tones?.length ? record.tones : null;
  return (
    <section className="listen" aria-labelledby="listen-title">
      <div className="listen__lead">
        <p className="eyebrow">{running && measured ? `Measuring a new result… · showing ${eyebrow.toLowerCase()}` : eyebrow}</p>
        <h3 id="listen-title" className="listen__title">
          {invitation ?? "Listen to the circuit"}
        </h3>
        <p className="listen__hint">
          {record?.audio ? listenHint(tones) : running ? "Rendering audio from the simulation…" : "Audio appears after the first measurement."}
          {note ? <span className="listen__note"> {note}</span> : null}
        </p>
      </div>
      {record?.audio ? (
        <div className="listen__actions">
          <PlayClipButtons audio={record.audio} title={audioTitle} />
        </div>
      ) : null}
      {measured ? <ToneLevels tones={measured} pending={running} /> : <TonePlaceholders tones={tones} loading={running} />}
    </section>
  );
}
