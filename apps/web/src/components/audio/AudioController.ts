// Web Audio playback for simulated clips.
//
// Rules (CLAUDE.md "Audio correctness"):
// - Playback starts only from a user action; the AudioContext is created lazily on first play.
// - Exactly one source plays at a time. Starting a clip stops the previous one first.
// - One GainNode carries the single user volume for every clip. Clips are never re-gained or
//   normalized here: the backend rendered every clip of a comparison with one shared gain.
// - Switching clips while playing restarts the other clip at the same offset (matched timing).

export interface ClipRef {
  /** Stable identity (the artifact ID). */
  id: string;
  url: string;
  label: string;
}

export type PlaybackStatus = "idle" | "loading" | "playing" | "error";

export interface PlaybackSnapshot {
  status: PlaybackStatus;
  clipId: string | null;
  clipLabel: string | null;
  error: string | null;
  volumeDb: number;
  muted: boolean;
  repeat: boolean;
}

export interface AudioControllerOptions {
  createContext?: () => AudioContext;
  fetchClip?: (url: string) => Promise<ArrayBuffer>;
  initialVolumeDb?: number;
}

export const DEFAULT_VOLUME_DB = -12;
export const MIN_VOLUME_DB = -40;
export const MAX_VOLUME_DB = 0;

async function defaultFetchClip(url: string): Promise<ArrayBuffer> {
  const response = await fetch(url, { credentials: "same-origin" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.arrayBuffer();
}

function defaultCreateContext(): AudioContext {
  const Ctor =
    window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!Ctor) throw new Error("This browser does not support Web Audio playback.");
  return new Ctor();
}

export class AudioController {
  private readonly createContext: () => AudioContext;
  private readonly fetchClip: (url: string) => Promise<ArrayBuffer>;
  private ctx: AudioContext | null = null;
  private gain: GainNode | null = null;
  private source: AudioBufferSourceNode | null = null;
  private readonly buffers = new Map<string, Promise<AudioBuffer>>();
  private startedAtCtx = 0;
  private startOffset = 0;
  private currentDuration = 0;
  private request = 0;
  private snapshot: PlaybackSnapshot;
  private readonly listeners = new Set<() => void>();

  constructor(options: AudioControllerOptions = {}) {
    this.createContext = options.createContext ?? defaultCreateContext;
    this.fetchClip = options.fetchClip ?? defaultFetchClip;
    this.snapshot = {
      status: "idle",
      clipId: null,
      clipLabel: null,
      error: null,
      volumeDb: options.initialVolumeDb ?? DEFAULT_VOLUME_DB,
      muted: false,
      repeat: false,
    };
  }

  // ---- subscription (useSyncExternalStore) ----

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getSnapshot = (): PlaybackSnapshot => this.snapshot;

  private update(patch: Partial<PlaybackSnapshot>): void {
    this.snapshot = { ...this.snapshot, ...patch };
    this.listeners.forEach((l) => l());
  }

  // ---- volume ----

  setVolumeDb(db: number): void {
    const clamped = Math.min(MAX_VOLUME_DB, Math.max(MIN_VOLUME_DB, db));
    this.update({ volumeDb: clamped });
    this.applyGain();
  }

  setMuted(muted: boolean): void {
    this.update({ muted });
    this.applyGain();
  }

  setRepeat(repeat: boolean): void {
    this.update({ repeat });
    if (this.source) this.source.loop = repeat;
  }

  private applyGain(): void {
    if (!this.gain || !this.ctx) return;
    const value = this.snapshot.muted ? 0 : 10 ** (this.snapshot.volumeDb / 20);
    this.gain.gain.setValueAtTime(value, this.ctx.currentTime);
  }

  // ---- playback ----

  /** Seconds into the current clip, or 0 when nothing is playing. */
  currentOffset(): number {
    if (!this.ctx || !this.source || this.currentDuration <= 0) return 0;
    const elapsed = this.ctx.currentTime - this.startedAtCtx + this.startOffset;
    if (this.snapshot.repeat) return elapsed % this.currentDuration;
    return Math.min(elapsed, this.currentDuration);
  }

  isPlaying(): boolean {
    return this.snapshot.status === "playing" || this.snapshot.status === "loading";
  }

  /** Play a clip from `offsetS`. Always stops whatever was playing first. */
  async play(clip: ClipRef, offsetS = 0): Promise<void> {
    const token = ++this.request;
    this.stopSource();
    this.update({ status: "loading", clipId: clip.id, clipLabel: clip.label, error: null });
    let buffer: AudioBuffer;
    try {
      const ctx = this.ensureContext();
      if (ctx.state === "suspended") await ctx.resume();
      buffer = await this.load(clip);
    } catch (error) {
      if (token !== this.request) return;
      this.buffers.delete(clip.id);
      this.update({
        status: "error",
        error: `Couldn’t play “${clip.label}”. ${error instanceof Error ? error.message : ""}`.trim(),
      });
      return;
    }
    if (token !== this.request) return; // a newer play/stop superseded this request
    this.startBuffer(buffer, offsetS);
  }

  /** Switch to another clip. If one is playing, the new clip starts at the same offset. */
  async switchTo(clip: ClipRef): Promise<void> {
    if (!this.isPlaying()) return;
    const offset = this.currentOffset();
    await this.play(clip, offset);
  }

  stop(): void {
    this.request++;
    this.stopSource();
    this.update({ status: "idle", error: null });
  }

  /** Preload and cache a clip without playing it (never creates a context on its own). */
  async preload(clip: ClipRef): Promise<void> {
    if (!this.ctx) return;
    await this.load(clip).catch(() => this.buffers.delete(clip.id));
  }

  dispose(): void {
    this.stop();
    this.buffers.clear();
    void this.ctx?.close();
    this.ctx = null;
    this.gain = null;
  }

  private ensureContext(): AudioContext {
    if (!this.ctx) {
      this.ctx = this.createContext();
      this.gain = this.ctx.createGain();
      this.gain.connect(this.ctx.destination);
      this.applyGain();
    }
    return this.ctx;
  }

  private load(clip: ClipRef): Promise<AudioBuffer> {
    let cached = this.buffers.get(clip.id);
    if (!cached) {
      const ctx = this.ensureContext();
      cached = this.fetchClip(clip.url).then((data) => ctx.decodeAudioData(data));
      this.buffers.set(clip.id, cached);
    }
    return cached;
  }

  private startBuffer(buffer: AudioBuffer, offsetS: number): void {
    const ctx = this.ensureContext();
    const gain = this.gain!;
    this.stopSource();
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.loop = this.snapshot.repeat;
    source.connect(gain);
    const offset = buffer.duration > 0 ? Math.max(0, offsetS) % buffer.duration : 0;
    source.onended = () => {
      if (this.source !== source) return;
      this.source = null;
      this.update({ status: "idle" });
    };
    this.source = source;
    this.currentDuration = buffer.duration;
    this.startOffset = offset;
    this.startedAtCtx = ctx.currentTime;
    source.start(0, offset);
    this.update({ status: "playing" });
  }

  private stopSource(): void {
    const source = this.source;
    if (!source) return;
    this.source = null;
    source.onended = null;
    try {
      source.stop();
    } catch {
      // Already stopped.
    }
    source.disconnect();
  }
}
