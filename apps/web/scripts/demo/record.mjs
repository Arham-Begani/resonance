// Records the Resonance demo: real interactions on a running deployment (real ngspice, real
// Nemotron), captured as a CDP screencast (JPEG frames with timestamps) plus every audio clip the
// app actually played (captured from AudioBufferSourceNode.start in the page). build.py turns the
// frames + clips into an mp4. Nothing is scripted on the server side; captions describe what is on
// screen and say when a value is measured vs predicted.
import { chromium } from "@playwright/test";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";

const BASE = process.env.BASE ?? "https://resonance-circuit-lab.vercel.app";
const OUT = process.env.OUT ?? "demo-take";
const SKIP_AI = process.env.SKIP_AI === "1";
rmSync(OUT, { recursive: true, force: true });
mkdirSync(`${OUT}/frames`, { recursive: true });

const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);
const browser = await chromium.launch({ args: ["--autoplay-policy=no-user-gesture-required"] });
// 1440x816 CSS px at 4/3 scale = 1920x1088 device px; build.py crops to 1920x1080 (the bottom
// 8 px hold the content clock).
const VIEW = { width: 1440, height: 816 };
const SCALE = 4 / 3;
const context = await browser.newContext({ viewport: VIEW, deviceScaleFactor: SCALE });

// In-page: capture played audio (decoded samples + start/stop wall times), a visible cursor and
// a caption bar. All of it is presentation for the recording only.
await context.addInitScript(() => {
  window.__clips = [];
  const proto = AudioBufferSourceNode.prototype;
  const start = proto.start;
  const stop = proto.stop;
  proto.start = function (...args) {
    const b = this.buffer;
    if (b) {
      const ch = b.getChannelData(0);
      const entry = { t: Date.now(), sr: b.sampleRate, data: Array.from(ch), stop: null };
      window.__clips.push(entry);
      this.__entry = entry;
      this.addEventListener("ended", () => { if (entry.stop === null) entry.stop = Date.now(); });
    }
    return start.apply(this, args);
  };
  proto.stop = function (...args) {
    if (this.__entry && this.__entry.stop === null) this.__entry.stop = Date.now();
    return stop.apply(this, args);
  };
  // Record the listening gain the app applies (GainNode values) so the mix uses the same level.
  window.__gains = [];
  const setter = Object.getOwnPropertyDescriptor(AudioParam.prototype, "value").set;
  Object.defineProperty(AudioParam.prototype, "value", {
    set(v) { window.__gains.push({ t: Date.now(), v }); return setter.call(this, v); },
    get: Object.getOwnPropertyDescriptor(AudioParam.prototype, "value").get,
  });
  const install = () => {
    if (document.getElementById("__demo_cursor")) return;
    const c = document.createElement("div");
    c.id = "__demo_cursor";
    c.setAttribute("aria-hidden", "true");
    c.style.cssText = "position:fixed;z-index:2147483647;left:0;top:0;width:18px;height:18px;margin:-9px 0 0 -9px;border-radius:50%;background:rgba(232,238,242,.92);box-shadow:0 0 0 2px rgba(11,16,20,.85),0 0 0 6px rgba(101,197,212,.35);pointer-events:none;transition:transform .12s;";
    document.documentElement.appendChild(c);
    addEventListener("mousemove", (e) => { c.style.left = e.clientX + "px"; c.style.top = e.clientY + "px"; }, true);
    addEventListener("mousedown", () => { c.style.transform = "scale(.7)"; }, true);
    addEventListener("mouseup", () => { c.style.transform = "scale(1)"; }, true);
    const cap = document.createElement("div");
    cap.id = "__demo_caption";
    cap.setAttribute("aria-hidden", "true");
    cap.style.cssText = "position:fixed;z-index:2147483646;left:50%;top:58px;transform:translateX(-50%);max-width:980px;padding:12px 20px;border-radius:12px;background:rgba(11,16,20,.94);border:1px solid rgba(101,197,212,.55);color:#E8EEF2;font:600 19px/1.45 system-ui,-apple-system,'Segoe UI',sans-serif;text-align:center;box-shadow:0 10px 30px rgba(0,0,0,.45);pointer-events:none;opacity:0;transition:opacity .2s;";
    document.documentElement.appendChild(cap);
  };
  // Content clock: the current time (ms, low 32 bits) drawn as 32 black/white 4x4 blocks in the
  // bottom-left corner every 50 ms, so each captured frame carries the time its content shows.
  const clock = () => {
    if (document.getElementById("__demo_clock")) return;
    const cv = document.createElement("canvas");
    cv.id = "__demo_clock";
    cv.width = 128; cv.height = 4;
    cv.setAttribute("aria-hidden", "true");
    cv.style.cssText = "position:fixed;left:0;bottom:0;width:128px;height:4px;z-index:2147483647;pointer-events:none;image-rendering:pixelated;";
    document.documentElement.appendChild(cv);
    const g = cv.getContext("2d");
    // Redrawn every animation frame, which also keeps the compositor producing frames (high fps).
    const tick = () => {
      const v = Date.now() % 4294967296;
      for (let i = 0; i < 32; i++) { g.fillStyle = (Math.floor(v / 2 ** (31 - i)) % 2) ? "#fff" : "#000"; g.fillRect(i * 4, 0, 4, 4); }
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  };
  const both = () => { install(); clock(); };
  if (document.readyState === "loading") addEventListener("DOMContentLoaded", both); else both();
});

const page = await context.newPage();
const cdp = await context.newCDPSession(page);
const frames = [];
cdp.on("Page.screencastFrame", async ({ data, metadata, sessionId }) => {
  const i = frames.length;
  frames.push({ i, t: metadata.timestamp * 1000 });
  writeFileSync(`${OUT}/frames/${String(i).padStart(6, "0")}.jpg`, Buffer.from(data, "base64"));
  await cdp.send("Page.screencastFrameAck", { sessionId }).catch(() => {});
});
const marks = [];
const mark = (name) => { marks.push({ name, t: Date.now() }); log("mark", name); };

const caption = async (text, ms = 0) => {
  await page.evaluate((t) => {
    const el = document.getElementById("__demo_caption");
    if (!el) return;
    if (!t) { el.style.opacity = "0"; return; }
    el.textContent = t;
    el.style.opacity = "1";
  }, text);
  if (ms) await page.waitForTimeout(ms);
};
let mouse = { x: 720, y: 450 };
const moveTo = async (loc) => {
  await loc.scrollIntoViewIfNeeded();
  const box = await loc.boundingBox();
  if (!box) throw new Error("no box");
  const x = box.x + box.width / 2, y = box.y + box.height / 2;
  await page.mouse.move(x, y, { steps: 18 });
  mouse = { x, y };
  await page.waitForTimeout(250);
};
const click = async (loc, pause = 500) => { await moveTo(loc); await loc.click(); await page.waitForTimeout(pause); };
const smoothScroll = async (y, ms = 1200) => {
  await page.evaluate(({ y, ms }) => new Promise((r) => {
    const from = scrollY, t0 = performance.now();
    const step = (now) => { const k = Math.min(1, (now - t0) / ms); const e = k < .5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2; scrollTo(0, from + (y - from) * e); if (k < 1) requestAnimationFrame(step); else r(); };
    requestAnimationFrame(step);
  }), { y, ms });
};

try {
  // Warm the deployment (Render may be cold) before recording starts.
  log("warming", BASE);
  await fetch(BASE + "/api/health").catch(() => {});
  await page.goto(BASE + "/");
  await page.getByRole("heading", { level: 1 }).waitFor();
  await page.mouse.move(mouse.x, mouse.y);
  await cdp.send("Page.startScreencast", { format: "jpeg", quality: 90, maxWidth: 1920, maxHeight: 1088, everyNthFrame: 1 });
  mark("start");

  // ---------- 1. Landing ----------
  await caption("Resonance — hear what a circuit does, then find out why it fails.", 3500);
  await caption("Real ngspice simulation in the browser · AI lab partner: NVIDIA Nemotron on Nebius Token Factory", 3000);
  const physics = page.getByRole("heading", { name: /Why a doubled resistor/ });
  await smoothScroll((await physics.boundingBox()).y + (await page.evaluate(() => scrollY)) - 140, 1600);
  await caption("The core puzzle: doubling R or doubling C sounds identical. Only the source current tells them apart.", 4200);
  await smoothScroll(0, 900);
  await caption("");
  await click(page.getByRole("link", { name: /Open the lab/ }).first(), 300);
  mark("lab");

  // ---------- 2. Explore: the opening experiment ----------
  await page.waitForURL(/\/lab$/);
  await page.locator(".instruments__meta").first().waitFor({ timeout: 120_000 });
  await page.getByRole("button", { name: /^Run experiment on Revision \d+$/ }).waitFor({ timeout: 120_000 });
  await caption("The lab opens on a measured experiment: an RC low-pass filter, simulated by ngspice.", 3200);
  await caption("Listen: input first — a 440 Hz tone plus a 4 kHz whistle.");
  await click(page.getByRole("button", { name: "Play input" }), 1900);
  await caption("Now the output, at the same listening level. Nothing is normalized.");
  await click(page.getByRole("button", { name: "Play output" }), 2100);
  const whistle = (await page.getByRole("list", { name: "Measured tone levels" }).innerText()).replace(/\s+/g, " ");
  log("levels", whistle);
  const wdb = /Whistle · 4 kHz\s*(−?[\d.]+ dB)/.exec(whistle)?.[1];
  await caption(wdb ? `Measured: the whistle is only ${wdb} down. The goal asks for at least −12 dB, so it is not met yet.` : "Measured tone levels are shown; the goal is not met yet.", 3800);

  // ---------- 3. Suggest, apply, measure ----------
  const design = page.getByRole("region", { name: "Design help" });
  await caption("Ask for a design suggestion. It is an analytical prediction, labeled as one.");
  await click(design.getByRole("button", { name: "Suggest values for this goal" }), 1200);
  await moveTo(page.getByRole("button", { name: /^Apply as Revision/ }));
  await page.waitForTimeout(2200);
  await caption("Apply it, then measure it for real.");
  await click(page.getByRole("button", { name: /^Apply as Revision/ }), 900);
  await click(page.getByRole("button", { name: /^Run experiment on Revision 2$/ }), 300);
  mark("run2");
  await page.locator(".instruments__meta").first().filter({ hasText: "Experiment 2 ·" }).waitFor({ timeout: 120_000 });
  await page.getByRole("button", { name: /^Run experiment on Revision \d+$/ }).waitFor({ timeout: 120_000 });
  await page.waitForTimeout(700);
  const goal = (await page.getByRole("region", { name: "Goal" }).innerText()).replace(/\s+/g, " ");
  const met = /Goal met/.test(goal) && !/Goal not met/.test(goal.split("Constraint")[0]);
  log("goal after", goal.slice(0, 200));
  await caption(met ? "ngspice measurement: goal met. The prediction was checked, not assumed." : "ngspice measurement recorded — the goal check uses measured gain, not the prediction.", 3200);
  await caption("Hear the new output.");
  await click(page.getByRole("button", { name: "Play output" }), 2100);

  // ---------- 4. Compare ----------
  const history = page.getByRole("region", { name: "Experiment history" });
  await caption("Compare the two experiments side by side.");
  await click(history.getByLabel("Compare Experiment 1"), 300);
  await click(history.getByLabel("Compare Experiment 2"), 300);
  await click(history.getByRole("button", { name: "Compare" }), 600);
  await page.getByLabel("Comparison result").scrollIntoViewIfNeeded();
  await page.waitForTimeout(3200);
  mark("compare");

  // ---------- 5. Investigate ----------
  await caption("Now a mystery: a circuit with at most one hidden fault.");
  await click(page.getByRole("navigation", { name: "Workbench mode" }).getByRole("button", { name: "Investigate" }), 700);
  await click(page.getByRole("button", { name: "Start a mystery" }), 300);
  await page.getByRole("heading", { name: "What we observed" }).waitFor({ timeout: 120_000 });
  mark("case");
  await caption("Nobody can see the hidden values — not you, not the AI. Only measurements count.", 3000);
  const expected = page.getByRole("button", { name: /^Play Expected/ });
  const observed = page.getByRole("button", { name: /^Play Observed/ });
  if (await expected.count()) {
    await caption("Expected (the intended design) versus Observed (the mystery circuit).");
    await click(expected, 1900);
    await click(observed, 2100);
  }

  if (!SKIP_AI) {
    await caption("Investigate for me: Nemotron chooses its own experiments, live. Up to 3 allowed.");
    await click(page.getByRole("button", { name: /^Investigate for me/ }), 400);
    mark("ai-start");
    const t0 = Date.now();
    const agentLog = page.locator(".agent-log").first();
    await agentLog.waitFor({ timeout: 60_000 });
    // Running → Finished / Cancelled / failed: wait until the Cancel button disappears.
    await agentLog.getByRole("button", { name: "Cancel" }).waitFor({ timeout: 60_000 }).catch(() => {});
    await caption("Each step is a real tool call: run an experiment, assess the evidence, then diagnose.");
    await agentLog.getByRole("button", { name: "Cancel" }).waitFor({ state: "detached", timeout: 240_000 });
    const secs = Math.round((Date.now() - t0) / 1000);
    mark("ai-end");
    log("ai seconds", secs);
    await caption(`The AI run finished in ${secs} s of real inference. Its diagnosis cites recorded observations.`, 3500);
    const dx = page.getByRole("region", { name: "Diagnosis" });
    if (await dx.count()) { await dx.scrollIntoViewIfNeeded(); await page.waitForTimeout(3000); }
  }

  // ---------- 6. Repair ----------
  const repair = page.getByRole("region", { name: "Repair" });
  if (await repair.count()) {
    await repair.scrollIntoViewIfNeeded();
    const apply = repair.getByRole("button", { name: "Apply repair and verify" });
    if (await apply.count()) {
      await caption("Apply the proposed repair, then verify it across the whole frequency grid.");
      await click(apply, 300);
      const badge = repair.getByText(/^(Behaviour restored|Not restored)$/).first();
      await badge.waitFor({ timeout: 120_000 });
      mark("repair");
      await caption(`Verified numerically: ${(await badge.innerText()).trim()}. A good sound alone is not proof.`, 3500);
    } else {
      log("no repair to apply (none proposed or no fault)");
    }
  }

  // ---------- 7. Reveal ----------
  await caption("Close the case to reveal the hidden fault.");
  await click(page.getByRole("button", { name: "Close & reveal…" }), 500);
  await click(page.getByRole("button", { name: "Close & reveal", exact: true }), 500);
  const revealed = page.getByRole("region", { name: "Revealed" });
  await revealed.waitFor({ timeout: 60_000 });
  await revealed.scrollIntoViewIfNeeded();
  mark("reveal");
  await caption("Identification and functional recovery are reported separately.", 4200);

  // ---------- 8. Closing card (measured numbers from evals/results) ----------
  await page.evaluate(() => {
    const card = document.createElement("div");
    card.setAttribute("aria-hidden", "true");
    card.style.cssText = "position:fixed;inset:0;z-index:2147483645;background:#0B1014;display:flex;align-items:center;justify-content:center;opacity:0;transition:opacity .4s;font-family:system-ui,-apple-system,'Segoe UI',sans-serif;color:#E8EEF2;";
    card.innerHTML = `<div style="max-width:920px;padding:48px">
      <div style="font-size:15px;letter-spacing:.08em;color:#65C5D4;font-weight:600;margin-bottom:12px">MEASURED · SAME 24 HELD-OUT CASES · SAME SYMPTOM, BUDGET AND TOLERANCES</div>
      <div style="font-size:40px;font-weight:600;margin-bottom:28px">Honest results, including where the AI falls short.</div>
      <table style="width:100%;border-collapse:collapse;font-size:20px;line-height:1.9">
        <tr style="color:#A7B8C3"><td></td><td>Fixed baseline</td><td>Nemotron run 1</td><td>Nemotron run 2*</td></tr>
        <tr><td>Correct diagnosis</td><td>24 / 24</td><td>20 / 24</td><td>21 / 24</td></tr>
        <tr><td>Wrong or unsupported</td><td>0</td><td>1</td><td>0</td></tr>
        <tr><td>Honest abstentions</td><td>0</td><td>0</td><td>3</td></tr>
        <tr><td>Repairs verified (of 20)</td><td>20</td><td>9</td><td>17</td></tr>
      </table>
      <div style="margin-top:14px;font-size:15px;color:#A7B8C3">* After requiring a diagnosis and a repair proposal before the agent finishes — a change made after run 1 on these cases.</div>
      <div style="margin-top:28px;font-size:18px;color:#A7B8C3">Every task works by keyboard and with sound off. Simulated circuits, not physical measurements.</div>
      <div style="margin-top:20px;font-size:22px;font-weight:600">resonance-circuit-lab.vercel.app · github.com/Arham-Begani/resonance</div>
    </div>`;
    document.documentElement.appendChild(card);
    requestAnimationFrame(() => { card.style.opacity = "1"; });
  });
  await caption("");
  await page.mouse.move(1400, 790);
  await page.waitForTimeout(7000);
  mark("end");
  // Let a lagging screencast catch up so the closing card is fully captured.
  await page.waitForTimeout(12000);
} catch (err) {
  log("ERROR", err.message);
  await page.screenshot({ path: `${OUT}/error.png` }).catch(() => {});
  mark("error");
} finally {
  await cdp.send("Page.stopScreencast").catch(() => {});
  const clips = await page.evaluate(() => window.__clips).catch(() => []);
  const gains = await page.evaluate(() => window.__gains).catch(() => []);
  writeFileSync(`${OUT}/clips.json`, JSON.stringify(clips));
  writeFileSync(`${OUT}/gains.json`, JSON.stringify(gains));
  writeFileSync(`${OUT}/frames.json`, JSON.stringify(frames));
  writeFileSync(`${OUT}/marks.json`, JSON.stringify(marks));
  log("frames", frames.length, "clips", clips.length);
  await browser.close();
}
