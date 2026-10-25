// Playback controls: clip selector, Play/Stop, repeat, one shared volume, mute, and the shared
// gain disclosure. Never autoplays; exactly one clip plays at a time.
import { useId } from "react";
import { formatDb } from "../../lib/format";
import { Icon } from "../ui/Icon";
import { MAX_VOLUME_DB, MIN_VOLUME_DB } from "./AudioController";
import { usePlayback } from "./PlaybackProvider";
import "./playback.css";

export interface EmptyPlayback {
  /** Why there is nothing to play right now, in context (e.g. a sweep is shown). */
  text: string;
  /** Optional way to load clips that do exist (e.g. the symptom's). */
  action?: { label: string; onClick: () => void };
}

export function PlaybackControls({ levels, empty }: { levels?: string | null; empty?: EmptyPlayback | null }) {
  const id = useId();
  const pb = usePlayback();
  const { snapshot, active, selectedClip } = pb;
  const clips = active?.comparison.clips ?? [];
  const playing = snapshot.status === "playing" || snapshot.status === "loading";
  const clipped = clips.some((c) => c.clipped);
  const adjust = active?.comparison.gain_adjustment_db ?? 0;

  let status: string;
  if (snapshot.status === "error") status = snapshot.error ?? "Playback failed.";
  else if (snapshot.status === "loading") status = `Loading ${snapshot.clipLabel ?? "clip"}…`;
  else if (snapshot.status === "playing") status = `Playing: ${snapshot.clipLabel ?? ""}`;
  else if (active) status = active.title;
  else status = empty?.text ?? "No clips yet. Run an experiment to render audio.";

  return (
    <div className="playback" role="group" aria-label="Circuit audio">
      <div className="segmented playback__clips" role="radiogroup" aria-label="Clip">
        {clips.length === 0 ? (
          <span className="playback__noclip">No audio</span>
        ) : (
          clips.map((clip) => (
            <label key={clip.artifact_id}>
              <input
                type="radio"
                name={`${id}-clip`}
                checked={selectedClip?.artifact_id === clip.artifact_id}
                onChange={() => pb.selectClip(clip.artifact_id)}
              />
              {clip.label}
            </label>
          ))
        )}
      </div>

      <button
        type="button"
        className={`btn ${playing ? "btn--danger" : "btn--primary"} playback__play`}
        disabled={!playing && !selectedClip}
        onClick={playing ? pb.stop : pb.play}
      >
        <Icon name={playing ? "stop" : "play"} />
        {playing ? "Stop" : "Play"}
      </button>

      <button
        type="button"
        className="btn btn--ghost btn--icon"
        aria-pressed={snapshot.repeat}
        aria-label="Repeat clip"
        title="Repeat clip"
        onClick={() => pb.setRepeat(!snapshot.repeat)}
      >
        <Icon name="repeat" />
      </button>

      {active ? (
        <details className="playback__note">
          <summary aria-label="About these clips">
            <Icon name="info" />
            {clipped || adjust < 0 ? <span className="playback__flag">{clipped ? "Clipped" : "Gain reduced"}</span> : null}
          </summary>
          <div className="playback__note-panel">
            <p>{active.comparison.note}</p>
            <p className="muted">
              Every clip in this comparison uses one shared gain{adjust < 0 ? ` (reduced by ${formatDb(-adjust)} for headroom)` : ""}; clips are never
              normalized separately.{clipped ? " At least one clip clipped; its level is not trustworthy by ear—use the numbers." : ""}
            </p>
          </div>
        </details>
      ) : null}
      <div className="playback__volume">
        <button
          type="button"
          className="btn btn--ghost btn--icon"
          aria-pressed={snapshot.muted}
          aria-label="Mute circuit audio"
          title="Mute circuit audio"
          onClick={() => pb.setMuted(!snapshot.muted)}
        >
          <Icon name={snapshot.muted ? "mute" : "volume"} />
        </button>
        <label className="visually-hidden" htmlFor={`${id}-vol`}>
          Volume for all clips
        </label>
        <input
          id={`${id}-vol`}
          type="range"
          className="range"
          min={MIN_VOLUME_DB}
          max={MAX_VOLUME_DB}
          step={1}
          value={snapshot.volumeDb}
          aria-valuetext={snapshot.muted ? `Muted (${formatDb(snapshot.volumeDb, 0)})` : formatDb(snapshot.volumeDb, 0)}
          onChange={(e) => pb.setVolumeDb(Number(e.target.value))}
        />
        <span className="playback__vol-value num" aria-hidden="true">
          {snapshot.muted ? "Muted" : formatDb(snapshot.volumeDb, 0)}
        </span>
      </div>

      <div className="playback__status">
        <p className={`playback__status-text${snapshot.status === "error" ? " is-error" : ""}`} title={status}>
          {status}
        </p>
        {levels ? <p className="playback__levels num">{levels}</p> : null}
      </div>

      {!active && empty?.action ? (
        <button type="button" className="btn btn--sm" onClick={empty.action.onClick}>
          {empty.action.label}
        </button>
      ) : null}

    </div>
  );
}
