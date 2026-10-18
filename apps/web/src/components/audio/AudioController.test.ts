import { AudioController } from "./AudioController";

// Minimal Web Audio stand-in (tests only): records sources, their start offsets, and gain values.
class FakeSource {
  buffer: { duration: number } | null = null;
  loop = false;
  onended: (() => void) | null = null;
  started: { when: number; offset: number } | null = null;
  stopped = false;
  connect = vi.fn();
  disconnect = vi.fn();
  start(when: number, offset: number) {
    this.started = { when, offset };
  }
  stop() {
    this.stopped = true;
  }
}

class FakeContext {
  currentTime = 0;
  state: "running" | "suspended" = "running";
  destination = {};
  sources: FakeSource[] = [];
  gainValues: number[] = [];
  gains = 0;
  createGain() {
    this.gains += 1;
    return {
      gain: { setValueAtTime: (v: number) => this.gainValues.push(v) },
      connect: vi.fn(),
    };
  }
  createBufferSource() {
    const s = new FakeSource();
    this.sources.push(s);
    return s;
  }
  decodeAudioData = vi.fn(async () => ({ duration: 1.2 }));
  resume = vi.fn(async () => {
    this.state = "running";
  });
  close = vi.fn(async () => {});
}

function setup() {
  const ctx = new FakeContext();
  const fetchClip = vi.fn(async () => new ArrayBuffer(8));
  const controller = new AudioController({ createContext: () => ctx as unknown as AudioContext, fetchClip });
  return { ctx, controller, fetchClip };
}

const input = { id: "art_in", url: "/api/artifacts/art_in", label: "Input" };
const output = { id: "art_out", url: "/api/artifacts/art_out", label: "Output" };

describe("AudioController", () => {
  it("never creates an AudioContext until the user plays", async () => {
    const created = vi.fn();
    const controller = new AudioController({
      createContext: () => {
        created();
        return new FakeContext() as unknown as AudioContext;
      },
      fetchClip: async () => new ArrayBuffer(8),
    });
    await controller.preload(input);
    expect(created).not.toHaveBeenCalled();
    await controller.play(input);
    expect(created).toHaveBeenCalledTimes(1);
  });

  it("plays exactly one source at a time", async () => {
    const { ctx, controller } = setup();
    await controller.play(input);
    await controller.play(output);
    expect(ctx.sources).toHaveLength(2);
    expect(ctx.sources[0]!.stopped).toBe(true);
    expect(ctx.sources[1]!.stopped).toBe(false);
    expect(controller.getSnapshot()).toMatchObject({ status: "playing", clipId: "art_out", clipLabel: "Output" });
  });

  it("stops playback and returns to idle", async () => {
    const { ctx, controller } = setup();
    await controller.play(input);
    controller.stop();
    expect(ctx.sources[0]!.stopped).toBe(true);
    expect(controller.getSnapshot().status).toBe("idle");
  });

  it("switching while playing restarts the other clip at the same offset", async () => {
    const { ctx, controller } = setup();
    await controller.play(input);
    ctx.currentTime = 0.45;
    await controller.switchTo(output);
    const second = ctx.sources[1]!;
    expect(second.started?.offset).toBeCloseTo(0.45, 6);
    expect(ctx.sources[0]!.stopped).toBe(true);
  });

  it("switching while idle does not start playback (no autoplay)", async () => {
    const { ctx, controller } = setup();
    await controller.switchTo(output);
    expect(ctx.sources).toHaveLength(0);
    expect(controller.getSnapshot().status).toBe("idle");
  });

  it("uses one shared gain node for the user volume and never re-gains clips", async () => {
    const { ctx, controller } = setup();
    await controller.play(input);
    await controller.play(output);
    expect(ctx.gains).toBe(1);
    controller.setVolumeDb(-20);
    expect(ctx.gainValues.at(-1)).toBeCloseTo(0.1, 6);
    controller.setMuted(true);
    expect(ctx.gainValues.at(-1)).toBe(0);
  });

  it("decodes each clip once and reuses the buffer", async () => {
    const { ctx, controller, fetchClip } = setup();
    await controller.play(input);
    await controller.play(input);
    expect(fetchClip).toHaveBeenCalledTimes(1);
    expect(ctx.decodeAudioData).toHaveBeenCalledTimes(1);
  });

  it("a stop during loading cancels the pending start", async () => {
    const ctx = new FakeContext();
    let release: (b: ArrayBuffer) => void = () => {};
    const controller = new AudioController({
      createContext: () => ctx as unknown as AudioContext,
      fetchClip: () => new Promise<ArrayBuffer>((r) => (release = r)),
    });
    const pending = controller.play(input);
    controller.stop();
    release(new ArrayBuffer(8));
    await pending;
    expect(ctx.sources).toHaveLength(0);
    expect(controller.getSnapshot().status).toBe("idle");
  });

  it("reports a load failure without throwing", async () => {
    const ctx = new FakeContext();
    const controller = new AudioController({
      createContext: () => ctx as unknown as AudioContext,
      fetchClip: async () => {
        throw new Error("HTTP 404");
      },
    });
    await controller.play(output);
    expect(controller.getSnapshot()).toMatchObject({ status: "error" });
    expect(controller.getSnapshot().error).toContain("Output");
  });
});
