"""Assemble a recorded take (frames + played clips) into an mp4.

Video: CDP screencast JPEG frames shown for their real durations (variable frame rate → 30 fps).
Audio: every clip the app actually played, placed at its real start time and cut where the app
stopped it, all with ONE shared gain (no per-clip normalization, matching the app's rule).
"""

import json
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

take = Path(sys.argv[1] if len(sys.argv) > 1 else "demo-take")
out = Path(sys.argv[2] if len(sys.argv) > 2 else "resonance-demo.mp4")
SHARED_GAIN = 10 ** (-6 / 20)  # one fixed playback gain for every clip
SR = 48_000

from PIL import Image

frames = json.loads((take / "frames.json").read_text())
clips = json.loads((take / "clips.json").read_text())
marks = json.loads((take / "marks.json").read_text())
end_mark = next(m["t"] for m in marks if m["name"] in ("end", "error"))


def content_time(path: Path, near_ms: float) -> float | None:
    """Decode the 32-bit content clock drawn in the bottom-left corner of a frame."""
    img = Image.open(path).convert("L")
    h = img.height
    bits = [1 if img.getpixel((i * 4 + 2, h - 2)) > 127 else 0 for i in range(32)]
    low = int("".join(map(str, bits)), 2)
    # Restore the high bits from the delivery time (they agree to well within 2^32 ms).
    base = int(near_ms) - (int(near_ms) % 4294967296)
    best = min((base + low + k * 4294967296 for k in (-1, 0, 1)), key=lambda v: abs(v - near_ms))
    return best if abs(best - near_ms) < 60_000 else None


timed = []
for f in frames:
    t = content_time(take / "frames" / f"{f['i']:06d}.jpg", f["t"])
    if t is not None and (not timed or t > timed[-1]["t"]):
        timed.append({"i": f["i"], "t": t})
print(f"frames with a readable clock: {len(timed)}/{len(frames)}")
frames = [f for f in timed if f["t"] <= end_mark + 500]
t0 = frames[0]["t"]
end_t = min(end_mark, frames[-1]["t"]) + 500
duration = (end_t - t0) / 1000
print(f"frames={len(frames)} clips={len(clips)} duration={duration:.1f}s")

# Video: concat list with real per-frame durations.
lines = []
for a, b in zip(frames, frames[1:] + [{"i": None, "t": end_t}]):
    lines.append(f"file 'frames/{a['i']:06d}.jpg'")
    lines.append(f"duration {max(0.001, (b['t'] - a['t']) / 1000):.4f}")
lines.append(f"file 'frames/{frames[-1]['i']:06d}.jpg'")
(take / "frames.txt").write_text("\n".join(lines))

# Audio: place each played clip at its real start, cut at its real stop.
track = np.zeros(int(duration * SR) + SR, dtype=np.float64)
for c in clips:
    data = np.asarray(c["data"], dtype=np.float64)
    if c["sr"] != SR:
        x = np.linspace(0, len(data) / c["sr"], num=int(len(data) * SR / c["sr"]), endpoint=False)
        data = np.interp(x, np.arange(len(data)) / c["sr"], data)
    if c.get("stop"):
        data = data[: max(0, int((c["stop"] - c["t"]) / 1000 * SR))]
    start = int((c["t"] - t0) / 1000 * SR)
    if start < 0 or start >= len(track):
        continue
    seg = data[: len(track) - start] * SHARED_GAIN
    # 5 ms fade-out where a clip was cut, to avoid a click in the video.
    n = min(len(seg), 240)
    seg[-n:] *= np.linspace(1, 0, n)
    track[start : start + len(seg)] += seg
peak = float(np.max(np.abs(track))) if track.size else 0
print(f"audio peak {peak:.3f} (shared gain {SHARED_GAIN:.3f})")
if peak > 0.99:
    raise SystemExit("clipping: lower SHARED_GAIN")
pcm = (np.clip(track, -1, 1) * 32767).astype("<i2")
with wave.open(str(take / "audio.wav"), "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes(pcm.tobytes())

subprocess.run(
    [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(take / "frames.txt"),
        "-i", str(take / "audio.wav"),
        "-vf", "fps=30,crop=1440:892:0:0,format=yuv420p",
        "-c:v", "libx264", "-preset", "slow", "-crf", "18",
        "-c:a", "aac", "-b:a", "160k",
        "-t", f"{duration:.2f}", "-movflags", "+faststart",
        str(out),
    ],
    check=True,
    cwd=take,
)
print("wrote", out)
