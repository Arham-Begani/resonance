"""Assemble a recorded take (frames + played clips + cursor path) into a 1080p60 mp4.

Video: CDP screencast frames, each shown from the time its content clock says it was rendered
(so picture and sound share one clock even when the screencast lags). The app's content updates
at the capture rate; the cursor is drawn here at a true 60 fps from the recorded, eased path.
Audio: every clip the app actually played, placed at its real start time and cut where the app
stopped it, all with ONE shared gain (no per-clip normalization, matching the app's rule).
Needs Python with numpy and Pillow, and ffmpeg on PATH.
"""

import json
import os
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

take = Path(sys.argv[1] if len(sys.argv) > 1 else "demo-take")
out = Path(sys.argv[2] if len(sys.argv) > 2 else "resonance-demo.mp4")
SHARED_GAIN = 10 ** (-6 / 20)  # one fixed playback gain for every clip
SR = 48_000
FPS = 60

frames = json.loads((take / "frames.json").read_text())
clips = json.loads((take / "clips.json").read_text())
marks = json.loads((take / "marks.json").read_text())
cursor = json.loads((take / "cursor.json").read_text())
view = json.loads((take / "meta.json").read_text())
end_mark = next(m["t"] for m in marks if m["name"] in ("end", "error"))


def content_time(path: Path, near_ms: float) -> float | None:
    """Decode the 32-bit content clock drawn in the bottom-left corner of a frame."""
    img = Image.open(path).convert("L")
    k = img.width / view["width"]  # CSS px → frame px
    y = img.height - round(2 * k)
    bits = [1 if img.getpixel((round((i * 4 + 2) * k), y)) > 127 else 0 for i in range(32)]
    low = int("".join(map(str, bits)), 2)
    # Restore the high bits from the delivery time (they agree to well within 2^32 ms).
    base = int(near_ms) - (int(near_ms) % 4294967296)
    best = min((base + low + j * 4294967296 for j in (-1, 0, 1)), key=lambda v: abs(v - near_ms))
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
unique_fps = len(frames) / duration
print(f"frames={len(frames)} (~{unique_fps:.1f} unique/s) clips={len(clips)} duration={duration:.1f}s")

# Video: concat list with real per-frame durations.
lines = []
for a, b in zip(frames, frames[1:] + [{"i": None, "t": end_t}]):
    lines.append(f"file 'frames/{a['i']:06d}.jpg'")
    lines.append(f"duration {max(0.001, (b['t'] - a['t']) / 1000):.4f}")
lines.append(f"file 'frames/{frames[-1]['i']:06d}.jpg'")
(take / "frames.txt").write_text("\n".join(lines))

# Cursor sprite (drawn at 2x and downsampled for smooth edges).
S = 48
big = Image.new("RGBA", (S * 2, S * 2), (0, 0, 0, 0))
d = ImageDraw.Draw(big)
c = S
d.ellipse((c - 34, c - 34, c + 34, c + 34), fill=(101, 197, 212, 70))
d.ellipse((c - 22, c - 22, c + 22, c + 22), fill=(11, 16, 20, 230))
d.ellipse((c - 17, c - 17, c + 17, c + 17), fill=(232, 238, 242, 245))
big.resize((S, S), Image.LANCZOS).save(take / "cursor.png")


def cursor_at(t: float) -> tuple[float, float]:
    if not cursor or t <= cursor[0]["t"]:
        return (cursor[0]["x"], cursor[0]["y"]) if cursor else (-200.0, -200.0)
    for a, b in zip(cursor, cursor[1:]):
        if a["t"] <= t <= b["t"]:
            k = 0 if b["t"] == a["t"] else (t - a["t"]) / (b["t"] - a["t"])
            return a["x"] + (b["x"] - a["x"]) * k, a["y"] + (b["y"] - a["y"]) * k
    return cursor[-1]["x"], cursor[-1]["y"]


# sendcmd script: move the overlay every output frame where the position changes.
cmds, last = [], None
for n in range(int(duration * FPS) + 1):
    x, y = cursor_at(t0 + n * 1000 / FPS)
    pos = (round(x - S / 2), round(y - S / 2))
    if pos != last:
        cmds.append(f"{n / FPS:.4f} overlay@cur x {pos[0]}, overlay@cur y {pos[1]};")
        last = pos
(take / "cursor.cmd").write_text("\n".join(cmds))

# Audio: place each played clip at its real start, cut at its real stop.
track = np.zeros(int(duration * SR) + SR, dtype=np.float64)
for clip in clips:
    data = np.asarray(clip["data"], dtype=np.float64)
    if clip["sr"] != SR:
        x = np.linspace(0, len(data) / clip["sr"], num=int(len(data) * SR / clip["sr"]), endpoint=False)
        data = np.interp(x, np.arange(len(data)) / clip["sr"], data)
    if clip.get("stop"):
        data = data[: max(0, int((clip["stop"] - clip["t"]) / 1000 * SR))]
    start = int((clip["t"] - t0) / 1000 * SR)
    if start < 0 or start >= len(track):
        continue
    seg = data[: len(track) - start] * SHARED_GAIN
    n = min(len(seg), 240)  # 5 ms fade-out where a clip was cut, to avoid a click
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

graph = (
    f"[0:v]fps={FPS},crop={view['width']}:1080:0:0,sendcmd=f=cursor.cmd[base];"
    "[2:v]format=rgba[cur];"
    "[base][cur]overlay@cur=x=-200:y=-200:shortest=1:eval=frame,format=yuv420p[v]"
)
subprocess.run(
    [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", "frames.txt",
        "-i", "audio.wav",
        "-loop", "1", "-framerate", str(FPS), "-i", "cursor.png",
        "-filter_complex", graph, "-map", "[v]", "-map", "1:a",
        "-c:v", "libx264", "-preset", os.environ.get("PRESET", "slow"), "-crf", "17", "-r", str(FPS),
        "-c:a", "aac", "-b:a", "160k",
        "-t", f"{duration:.2f}", "-movflags", "+faststart",
        str(out.resolve()),
    ],
    check=True,
    cwd=take,
)
print("wrote", out)
