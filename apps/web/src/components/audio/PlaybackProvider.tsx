// Shares one AudioController across the workbench: the playback strip and any "Play" buttons in
// cards drive the same single source and the same user volume.
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import type { AudioClip, AudioComparison } from "../../api/types";
import { useAnnouncer } from "../../state/announcer";
import { AudioController, type ClipRef, type PlaybackSnapshot } from "./AudioController";

export interface ActiveComparison {
  comparison: AudioComparison;
  /** Where these clips came from, e.g. "Experiment 2 · Revision 3". */
  title: string;
}

interface PlaybackApi {
  snapshot: PlaybackSnapshot;
  active: ActiveComparison | null;
  selectedClip: AudioClip | null;
  /** Make a comparison the strip's current clip set (stops playback if it changes). */
  setActive: (comparison: AudioComparison | null | undefined, title: string) => void;
  selectClip: (artifactId: string) => void;
  play: () => void;
  stop: () => void;
  /** User-initiated: load a comparison, select one of its clips, and play it. */
  playClip: (comparison: AudioComparison, title: string, artifactId: string) => void;
  setVolumeDb: (db: number) => void;
  setMuted: (muted: boolean) => void;
  setRepeat: (repeat: boolean) => void;
}

const PlaybackContext = createContext<PlaybackApi | null>(null);

function toRef(clip: AudioClip): ClipRef {
  return { id: clip.artifact_id, url: clip.url, label: clip.label };
}

export function PlaybackProvider({
  children,
  controller: provided,
}: {
  children: ReactNode;
  controller?: AudioController;
}) {
  const controllerRef = useRef<AudioController | null>(provided ?? null);
  if (!controllerRef.current) controllerRef.current = new AudioController();
  const controller = controllerRef.current;
  const snapshot = useSyncExternalStore(controller.subscribe, controller.getSnapshot);
  const { announce } = useAnnouncer();
  const [active, setActiveState] = useState<ActiveComparison | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const activeRef = useRef<ActiveComparison | null>(null);
  activeRef.current = active;

  useEffect(() => () => controller.dispose(), [controller]);

  const lastStatus = useRef(snapshot.status);
  useEffect(() => {
    if (snapshot.status === lastStatus.current) return;
    lastStatus.current = snapshot.status;
    if (snapshot.status === "playing" && snapshot.clipLabel) announce(`Playing: ${snapshot.clipLabel}`);
    if (snapshot.status === "error" && snapshot.error) announce(snapshot.error);
  }, [snapshot.status, snapshot.clipLabel, snapshot.error, announce]);

  const setActive = useCallback(
    (comparison: AudioComparison | null | undefined, title: string) => {
      const current = activeRef.current;
      if (!comparison) {
        if (current) {
          controller.stop();
          setActiveState(null);
          setSelectedId(null);
        }
        return;
      }
      if (current && current.comparison.id === comparison.id) {
        if (current.title !== title) setActiveState({ comparison, title });
        return;
      }
      controller.stop();
      setActiveState({ comparison, title });
      setSelectedId(comparison.clips[0]?.artifact_id ?? null);
    },
    [controller],
  );

  const findClip = useCallback(
    (id: string | null) => activeRef.current?.comparison.clips.find((c) => c.artifact_id === id) ?? null,
    [],
  );

  const selectClip = useCallback(
    (artifactId: string) => {
      setSelectedId(artifactId);
      const clip = findClip(artifactId);
      if (clip && controller.isPlaying()) void controller.switchTo(toRef(clip));
    },
    [controller, findClip],
  );

  const play = useCallback(() => {
    const clip = findClip(selectedId);
    if (clip) void controller.play(toRef(clip));
  }, [controller, findClip, selectedId]);

  const stop = useCallback(() => {
    const wasPlaying = controller.isPlaying();
    controller.stop();
    if (wasPlaying) announce("Playback stopped");
  }, [controller, announce]);

  const playClip = useCallback(
    (comparison: AudioComparison, title: string, artifactId: string) => {
      const clip = comparison.clips.find((c) => c.artifact_id === artifactId);
      if (!clip) return;
      activeRef.current = { comparison, title };
      setActiveState({ comparison, title });
      setSelectedId(artifactId);
      void controller.play(toRef(clip));
    },
    [controller],
  );

  const value = useMemo<PlaybackApi>(
    () => ({
      snapshot,
      active,
      selectedClip: findClip(selectedId),
      setActive,
      selectClip,
      play,
      stop,
      playClip,
      setVolumeDb: (db) => controller.setVolumeDb(db),
      setMuted: (m) => controller.setMuted(m),
      setRepeat: (r) => controller.setRepeat(r),
    }),
    [snapshot, active, selectedId, findClip, setActive, selectClip, play, stop, playClip, controller],
  );

  return <PlaybackContext.Provider value={value}>{children}</PlaybackContext.Provider>;
}

export function usePlayback(): PlaybackApi {
  const ctx = useContext(PlaybackContext);
  if (!ctx) throw new Error("usePlayback must be used inside PlaybackProvider");
  return ctx;
}
