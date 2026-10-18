// Visual check helper: captures the LIVE app (no mocked data) at desktop and narrow sizes.
// Usage: node scripts/screenshots.mjs [baseUrl] [outDir] [--steps=explore,investigate,focus]
// Requires `npx playwright install chromium` once. Output goes to test-results/ (git-ignored).
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
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
const steps = (flags.steps ?? "explore,investigate,focus").split(",");
mkdirSync(out, { recursive: true });

const viewports = [
  { name: "desktop", width: 1440, height: 900 },
  { name: "narrow", width: 390, height: 844 },
];

const browser = await chromium.launch();
try {
  for (const vp of viewports) {
    const context = await browser.newContext({ viewport: { width: vp.width, height: vp.height }, reducedMotion: "reduce" });
    const page = await context.newPage();
    page.on("console", (msg) => {
      if (msg.type() === "error") console.log(`[${vp.name}] console error: ${msg.text()}`);
    });
    if (steps.includes("explore")) {
      await page.goto(base, { waitUntil: "networkidle" });
      await page.waitForTimeout(800);
      await page.screenshot({ path: `${out}/${vp.name}-explore.png` });
      await page.screenshot({ path: `${out}/${vp.name}-explore-full.png`, fullPage: true });
    }
    if (steps.includes("focus")) {
      await page.goto(base, { waitUntil: "networkidle" });
      for (let i = 0; i < 6; i++) await page.keyboard.press("Tab");
      await page.screenshot({ path: `${out}/${vp.name}-focus.png` });
    }
    if (steps.includes("investigate")) {
      await page.goto(`${base}/#investigate`, { waitUntil: "networkidle" });
      await page.waitForTimeout(800);
      await page.screenshot({ path: `${out}/${vp.name}-investigate.png` });
    }
    await context.close();
  }
} finally {
  await browser.close();
}
console.log("Screenshots written to", out);
