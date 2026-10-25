// Visual check helper: drives the LIVE app (no mocked data) at desktop and narrow sizes and
// captures every main state. It also measures the desktop fold rule: at 1440×900 the tone
// readouts and the whole frequency-response figure must be visible without scrolling.
// Usage: node scripts/screenshots.mjs [baseUrl] [outDir] [--steps=explore,highpass,suggest,learn,investigate,focus]
// Requires `npx playwright install chromium` once. Output goes to test-results/ (git-ignored).
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import AxeBuilder from "@axe-core/playwright";
import { chromium } from "@playwright/test";

const args = process.argv.slice(2).filter((a) => !a.startsWith("--"));
const flags = Object.fromEntries(
  process.argv
    .slice(2)
    .filter((a) => a.startsWith("--"))
    .map((a) => a.slice(2).split("=")),
);
const base = args[0] ?? "http://localhost:5180";
const out = resolve(args[1] ?? "test-results/screenshots");
const steps = (flags.steps ?? "explore,highpass,suggest,learn,investigate,focus").split(",");
const only = flags.viewport ?? null;
mkdirSync(out, { recursive: true });

const viewports = [
  { name: "desktop", width: 1440, height: 900 },
  { name: "narrow", width: 390, height: 844 },
].filter((v) => !only || v.name === only);

async function waitForMeasurement(page) {
  await page.locator(".instruments__meta").first().waitFor({ state: "attached", timeout: 90_000 });
  await page.waitForFunction(() => !document.querySelector('[aria-busy="true"] .spinner, .btn[aria-busy="true"]'), null, { timeout: 90_000 });
}

async function fold(page, label) {
  const result = await page.evaluate(() => {
    const center = document.querySelector(".workspace__center");
    const fig = [...document.querySelectorAll("figure.chart")].find((f) => f.querySelector(".chart__title")?.textContent === "Frequency response");
    const tones = document.querySelector('[aria-label="Measured tone levels"]');
    const box = (el) => (el ? el.getBoundingClientRect() : null);
    const c = box(center);
    // The plot itself (both panels and the x-axis label); the caption and data table may follow below.
    const f = box(fig?.querySelector(".chart__axis-label") ?? fig);
    const t = box(tones);
    return {
      centerBottom: c && Math.round(c.bottom),
      scrollTop: center?.scrollTop ?? null,
      figureBottom: f && Math.round(f.bottom),
      captionBottom: fig ? Math.round(fig.getBoundingClientRect().bottom) : null,
      tonesBottom: t && Math.round(t.bottom),
      pageScroll: Math.round(document.documentElement.scrollHeight - window.innerHeight),
    };
  });
  const ok = result.figureBottom !== null && result.tonesBottom !== null && result.figureBottom <= result.centerBottom && result.scrollTop === 0;
  console.log(`[fold ${label}] ${ok ? "OK" : "FAIL"} ${JSON.stringify(result)}`);
}

const browser = await chromium.launch();
try {
  for (const vp of viewports) {
    const context = await browser.newContext({ viewport: { width: vp.width, height: vp.height }, reducedMotion: "reduce" });
    // Count AudioContexts so the run can confirm that nothing plays without a Play press.
    await context.addInitScript(() => {
      const Native = window.AudioContext;
      window.__audioContexts = 0;
      if (Native) {
        window.AudioContext = class extends Native {
          constructor(...a) {
            super(...a);
            window.__audioContexts += 1;
          }
        };
      }
    });
    const page = await context.newPage();
    page.on("console", (msg) => {
      if (msg.type() === "error") console.log(`[${vp.name}] console error: ${msg.text()}`);
    });
    // --axe: also scan every captured state for WCAG 2 A/AA violations (axe-core; not a substitute for AT testing).
    const shot = async (name, fullPage = false) => {
      await page.screenshot({ path: `${out}/${vp.name}-${name}.png`, fullPage });
      if (!("axe" in flags)) return;
      const result = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
      const found = result.violations.map((v) => `${v.id}(${v.nodes.length}): ${v.nodes.slice(0, 3).map((n) => n.target.join(" ")).join(" | ")}`);
      console.log(`[axe ${vp.name}-${name}] ${found.length ? found.join("; ") : "0 violations"}`);
    };
    const tab = async (name) => {
      if (vp.name === "narrow") await page.getByRole("tab", { name }).click();
    };
    // Fresh session per viewport: the first visit runs the opening experiment automatically.
    await page.request.get(`${base}/api/health`);
    if (steps.includes("explore")) {
      await page.goto(base, { waitUntil: "domcontentloaded" });
      await page.waitForTimeout(400);
      await shot("explore-loading");
      await waitForMeasurement(page);
      await page.waitForTimeout(300);
      await shot("explore");
      if (vp.name === "desktop") await fold(page, "explore low-pass");
      const contexts = await page.evaluate(() => (window.__audioContexts ?? 0));
      console.log(`[${vp.name}] audio contexts after auto-run: ${contexts}`);
      await tab("Instruments");
      await shot("explore-instruments");
      await tab("Lab notes");
      await shot("explore-notes");
      await tab("Circuit");
    }
    if (steps.includes("highpass")) {
      await tab("Circuit");
      await page.getByRole("radio", { name: "High-pass" }).check();
      await page.waitForTimeout(500);
      await shot("highpass-switching");
      await waitForMeasurement(page);
      await page.waitForFunction(() => document.querySelector(".instruments__meta")?.textContent?.includes("Experiment 2"), null, { timeout: 90_000 });
      await page.waitForTimeout(300);
      await shot("highpass");
      if (vp.name === "desktop") await fold(page, "explore high-pass");
      await tab("Instruments");
      await shot("highpass-instruments");
      await tab("Lab notes");
      await shot("highpass-notes");
    }
    if (steps.includes("suggest")) {
      await tab("Lab notes");
      await page.getByRole("button", { name: "Suggest values for this goal" }).click();
      await page.waitForTimeout(1200);
      await page.locator(".suggestion, .callout").last().scrollIntoViewIfNeeded();
      await shot("suggest");
      const apply = page.getByRole("button", { name: /^Apply as Revision \d+$/ });
      if ((await apply.count()) && (await apply.isEnabled())) {
        await apply.click();
        await page.waitForTimeout(800);
        await shot("suggest-applied");
        await tab("Instruments");
        await shot("suggest-applied-instruments");
      }
    }
    if (steps.includes("learn")) {
      await page.getByRole("navigation", { name: "Workbench mode" }).getByRole("button", { name: "Learn" }).click();
      await page.waitForTimeout(1500);
      await tab("Lab notes");
      await shot("learn-notes");
      const form = page.getByRole("form", { name: "Make a prediction" });
      if (await form.count()) {
        const groups = form.locator("fieldset.prediction");
        const n = await groups.count();
        for (let i = 0; i < n; i++) await groups.nth(i).getByRole("radio").nth(i === 0 ? 2 : 1).check();
        await form.getByRole("textbox").fill("A bigger capacitor should lower the cutoff.");
        await shot("learn-predicted");
        await form.getByRole("button", { name: "Run the experiment" }).click();
        await page.getByRole("heading", { name: /^Result · Attempt \d+$/ }).waitFor({ state: "attached", timeout: 120_000 });
        await page.waitForTimeout(600);
        const raw = await page.evaluate(() => document.body.innerText.match(/(?<![A-Za-z0-9_-])(exp|rev|obs|case|art|cmp)_[A-Za-z0-9_-]{6,}/g) ?? []);
        console.log(`[${vp.name}] raw IDs in learner-visible text after the Learn attempt: ${raw.length ? raw.join(", ") : "none"}`);
        await shot("learn-result");
        await tab("Instruments");
        await page.waitForTimeout(300);
        await shot("learn-compare");
      } else {
        await shot("learn");
      }
    }
    if (steps.includes("investigate")) {
      await page.getByRole("navigation", { name: "Workbench mode" }).getByRole("button", { name: "Investigate" }).click();
      await page.waitForTimeout(600);
      await tab("Lab notes");
      await shot("investigate-start");
      await page.getByRole("radio", { name: "High-pass" }).last().check();
      await page.getByRole("button", { name: "Start a mystery" }).click();
      await page.getByRole("heading", { name: "What we observed" }).waitFor({ timeout: 90_000 });
      await page.waitForTimeout(400);
      await shot("investigate-symptom");
      await page.getByRole("button", { name: /^Guide me/ }).click();
      await page.getByRole("heading", { name: "Take a measurement" }).waitFor({ state: "attached" });
      await page.waitForTimeout(300);
      await shot("investigate-guide");
      const predict = page.locator(".guide-predict input[type=checkbox]");
      if (await predict.count()) {
        await predict.first().check();
        await page.getByRole("region", { name: "Take a measurement" }).getByRole("button", { name: /^Run/ }).nth(1).click();
        await page.locator(".prediction-check").first().waitFor({ timeout: 90_000 });
        await page.waitForTimeout(400);
        await page.locator(".prediction-check").first().scrollIntoViewIfNeeded();
        await shot("investigate-prediction-check");
      }
      await tab("Instruments");
      await shot("investigate-instruments");
      // Close so the next viewport's session starts clean (and to see the reveal).
      await tab("Lab notes");
      await page.getByRole("button", { name: "Close & reveal…" }).click();
      await page.getByRole("button", { name: "Close & reveal", exact: true }).click();
      await page.getByRole("heading", { name: "Revealed" }).waitFor({ timeout: 30_000 });
      await shot("investigate-reveal");
    }
    if (steps.includes("focus")) {
      await page.goto(base, { waitUntil: "networkidle" });
      for (let i = 0; i < 6; i++) await page.keyboard.press("Tab");
      await shot("focus");
    }
    await context.close();
  }
} finally {
  await browser.close();
}
console.log("Screenshots written to", out);
