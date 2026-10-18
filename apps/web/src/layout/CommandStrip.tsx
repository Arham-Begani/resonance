// Bottom strip: playback (with tone levels in text) and the typed command box. In Explore the
// box sends a request to the explore agent; in Investigate it asks for a hint (Guide me only).
import { useId, useState, type FormEvent } from "react";
import { usePlayback } from "../components/audio/PlaybackProvider";
import { PlaybackControls, type EmptyPlayback } from "../components/audio/PlaybackStrip";
import { Button } from "../components/ui/primitives";
import { Icon } from "../components/ui/Icon";
import { endpoints } from "../api/endpoints";
import { formatDb, formatHz } from "../lib/format";
import { currentRevision, shownRecord, useWorkbench } from "../state/workbench";
import { isRunActive, useAgentRuns } from "../features/agent/agentRuns";
import { useCase } from "../features/investigate/caseStore";

function useLevels(): string | null {
  const { state } = useWorkbench();
  const cs = useCase();
  if (state.mode === "explore") {
    const record = shownRecord(state);
    if (!record?.tones?.length) return null;
    return record.tones.map((t) => `${formatHz(t.frequency_hz)} ${formatDb(t.transient_gain_db)}`).join(" · ");
  }
  const c = cs.current;
  const o = cs.view?.kind === "observation" ? c?.observations.find((x) => cs.view?.kind === "observation" && x.id === cs.view.id) : null;
  const tones = cs.view?.kind === "repair" ? (c?.repair?.tones ?? []) : (o?.transient?.tones ?? []);
  const withGain = tones.filter((t) => t.gain_db != null && t.expected_gain_db != null);
  if (!withGain.length) return null;
  const obsWord = cs.view?.kind === "repair" ? "repaired" : "observed";
  return withGain.map((t) => `${formatHz(t.frequency_hz)} expected ${formatDb(t.expected_gain_db!)}, ${obsWord} ${formatDb(t.gain_db!)}`).join(" · ");
}

/** In a case, explain why the shown measurement has no audio and offer clips that exist. */
function useCaseEmptyPlayback(): EmptyPlayback | null {
  const { state } = useWorkbench();
  const cs = useCase();
  const playback = usePlayback();
  if (state.mode !== "investigate") return null;
  const c = cs.current;
  if (!c) return { text: "Start a mystery to hear its symptom." };
  const withAudio = [...c.observations].reverse().find((o) => o.status === "succeeded" && o.transient?.audio);
  const action = withAudio?.transient?.audio
    ? {
        label: withAudio.sequence === 0 ? "Load symptom clips" : "Load capture clips",
        onClick: () =>
          playback.setActive(withAudio.transient!.audio!, withAudio.sequence === 0 ? "Symptom · Initial symptom" : `Obs ${withAudio.sequence} · Transient capture`),
      }
    : undefined;
  return {
    text: "This measurement has no audio: sweeps and current captures are shown as charts, not played.",
    action,
  };
}

export function CommandStrip() {
  const id = useId();
  const wb = useWorkbench();
  const { state } = wb;
  const cs = useCase();
  const runs = useAgentRuns();
  const [text, setText] = useState("");
  const levels = useLevels();
  const emptyPlayback = useCaseEmptyPlayback();
  const ai = state.health?.ai;
  const explore = state.mode === "explore";
  const scope = explore ? "explore" : "case";
  const view = runs.views[scope];
  const active = isRunActive(view);
  const revision = currentRevision(state);
  const c = cs.current;

  let disabledReason: string | null = null;
  if (!ai?.available) disabledReason = "AI lab partner unavailable. Manual tools keep working; setup steps are in Lab notes.";
  else if (active) disabledReason = "The AI lab partner is working. Cancel it to send something new.";
  else if (explore && !revision) disabledReason = "The circuit is still loading.";
  else if (!explore && !c) disabledReason = "Start a mystery to ask for hints.";
  else if (!explore && c && !c.available_actions.includes("request_hint"))
    disabledReason = c.mode === "agent" ? "Hints are for Guide me; the AI is investigating this case." : c.mode ? "Hints are not available at this stage." : "Choose Guide me to ask for hints.";

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (disabledReason) return;
    const message = text.trim();
    if (explore) {
      if (!message || !revision) return;
      const run = await runs.start("explore", () =>
        endpoints.startExploreAgentRun({ revision_id: revision.id, message, goal_id: state.goal?.id ?? null }),
      );
      if (run) setText("");
    } else {
      await cs.startAgent("hint", message || undefined);
      setText("");
    }
  };

  const label = explore ? "Ask the AI lab partner" : "Ask for a hint";
  const placeholder = explore ? "e.g. Reduce the whistle without losing the musical tone" : "Optional question, e.g. What should I measure next?";
  const noteId = `${id}-note`;

  return (
    <section className="strip" aria-label="Playback and commands">
      <PlaybackControls levels={levels} empty={emptyPlayback} />
      <form className="command" onSubmit={(e) => void submit(e)}>
        <label className="command__label" htmlFor={`${id}-cmd`}>
          <Icon name="ai" size={14} />
          {label}
        </label>
        <div className="command__row">
          <input
            id={`${id}-cmd`}
            className="input command__input"
            value={text}
            maxLength={500}
            placeholder={placeholder}
            disabled={Boolean(disabledReason)}
            aria-describedby={disabledReason ? noteId : undefined}
            onChange={(e) => setText(e.target.value)}
          />
          {active ? (
            <Button variant="danger" onClick={() => void runs.cancel(scope)} loading={view.cancelling}>
              Cancel
            </Button>
          ) : (
            <Button type="submit" icon="send" disabled={Boolean(disabledReason) || (explore && !text.trim())}>
              {explore ? "Send" : "Hint"}
            </Button>
          )}
        </div>
        {disabledReason ? (
          <p id={noteId} className="command__note">
            {disabledReason}
          </p>
        ) : null}
      </form>
    </section>
  );
}
