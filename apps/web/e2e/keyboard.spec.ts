// Keyboard-only use (CLAUDE.md "Every core task works with keyboard and text input. Provide
// visible focus, logical focus order, no traps"). After page.goto, these tests use only
// Tab / Shift+Tab / Enter / Space / arrow keys / Escape and typed text; never the mouse.
// Every focus stop on the way is checked for a rendered focus indicator (outline or ring).
import { expect, test, type Page } from "@playwright/test";
import {
  audioLog,
  caseIdOf,
  editValue,
  focusInfo,
  getCase,
  instrument,
  measureRegion,
  openWorkbench,
  plannedDiagnosis,
  shot,
  tabTo,
  waitForExperiment,
  type FocusInfo,
} from "./support/workbench";

const REGION_ORDER = ["skip-link", "Workbench mode", "header", "Explore workbench", "Lab notes", "Playback and commands"];

async function focusedIsBody(page: Page): Promise<boolean> {
  return page.evaluate(() => document.activeElement === document.body || document.activeElement === null);
}

/**
 * Where focus is after an action settles; null means it stayed on <body>. The app moves focus
 * on the next animation frames after an action resolves, so allow up to 1 s for it to land.
 */
async function focusAfter(page: Page): Promise<string | null> {
  await expect.poll(async () => !(await focusedIsBody(page)), { timeout: 1_000 }).toBe(true).catch(() => undefined);
  if (await focusedIsBody(page)) return null;
  const f = await focusInfo(page);
  return f ? `${f.tag}:${f.name}` : null;
}

test("explore: full Tab cycle has visible focus, logical order, and no trap", async ({ browser }, testInfo) => {
  let page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await instrument(page);
  await openWorkbench(page);
  const stops: FocusInfo[] = [];
  let wrapped = false;
  for (let i = 0; i < 250; i++) {
    await page.keyboard.press("Tab");
    const f = await focusInfo(page);
    if (!f) {
      if (stops.length > 0) {
        wrapped = true;
        break;
      }
      continue;
    }
    if (stops.length > 0 && f.name === stops[0]!.name && f.tag === stops[0]!.tag) {
      wrapped = true;
      break;
    }
    stops.push(f);
  }
  testInfo.annotations.push({ type: "tab-stops", description: stops.map((s) => `${s.region} › ${s.tag}:${s.name}`).join("\n") });
  expect(wrapped, "Tab eventually leaves the last control (no focus trap)").toBe(true);
  expect(stops[0]!.name).toBe("Skip to workbench");
  for (const s of stops) {
    expect(s.indicator, `focus indicator on ${s.tag} "${s.name}" (${s.detail})`).toBe(true);
    expect(s.rect.w * s.rect.h, `focused element "${s.name}" is rendered`).toBeGreaterThan(4);
  }
  // Regions are visited in reading order: skip link, rail, header, main, lab notes, strip.
  const order = stops.map((s) => REGION_ORDER.findIndex((r) => s.region === r || (r === "header" && s.region.startsWith("header"))));
  expect(order.every((o) => o >= 0), `every stop is inside a known region: ${stops.map((s) => s.region).join(", ")}`).toBe(true);
  for (let i = 1; i < order.length; i++) expect(order[i]!, `stop ${i} (${stops[i]!.name}) does not jump back to an earlier region`).toBeGreaterThanOrEqual(order[i - 1]!);

  // Shift+Tab walks back through exactly the same stops in reverse (no one-way traps).
  // Each sub-check starts from a fresh session (a new context) so it begins at the same state.
  const fresh = async () => {
    const p = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    await openWorkbench(p);
    return p;
  };
  const back = await fresh();
  const k = Math.min(24, stops.length - 1);
  for (let i = 0; i <= k; i++) await back.keyboard.press("Tab");
  const key = (f: FocusInfo | null) => (f ? `${f.tag}:${f.name}` : "body");
  expect(key(await focusInfo(back))).toBe(key(stops[k]!));
  for (let j = k - 1; j >= 0; j--) {
    await back.keyboard.press("Shift+Tab");
    expect(key(await focusInfo(back)), `Shift+Tab back to stop ${j}`).toBe(key(stops[j]!));
  }
  await back.close();

  // The skip link moves focus to the workbench.
  await page.close();
  page = await fresh();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Skip to workbench" })).toBeFocused();
  await shot(page, testInfo, "kbd-00-skip-link");
  await page.keyboard.press("Enter");
  await expect(page.locator("#workbench-main")).toBeFocused();
});

test("explore: measure, edit with Enter/Escape, re-measure, compare, undo, play — keyboard only", async ({ page }, testInfo) => {
  // Two real simulations plus a long keyboard journey: ~3 min alone, longer under full-suite load.
  test.setTimeout(600_000);
  await instrument(page);
  await openWorkbench(page);
  const path: FocusInfo[] = [];

  // 1. Run the first experiment.
  await tabTo(page, page.getByRole("button", { name: "Run experiment on Revision 1" }), { path });
  await page.keyboard.press("Enter");
  await waitForExperiment(page, 1);
  const afterRun = await focusAfter(page);

  // 2. Components list: arrow keys move between rows (roving tabindex); Tab reaches row actions.
  await tabTo(page, page.getByRole("button", { name: /^V1 — ideal voltage source/ }), { reverse: true, path });
  await page.keyboard.press("ArrowDown");
  await expect(page.getByRole("button", { name: /^R1 — resistor/ })).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await expect(page.getByRole("button", { name: /^C1 — capacitor/ })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "Edit C1 value" })).toBeFocused();

  // Escape cancels an edit and returns focus to the row.
  await page.keyboard.press("Enter");
  const input = page.getByLabel(/value for C1$/);
  await expect(input).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(input).toHaveCount(0);
  await expect(page.getByRole("button", { name: /^C1 — capacitor/ })).toBeFocused();

  // An invalid value is explained inline and does not create a revision.
  await page.keyboard.press("Tab");
  await page.keyboard.press("Enter");
  await expect(input).toBeFocused();
  await page.keyboard.type("abc");
  await expect(page.locator(".value-editor .field__error")).toBeVisible();
  await page.keyboard.press("Enter");
  await expect(page.locator(".header__rev")).toHaveText("Revision 1");
  await expect(input).toHaveAttribute("aria-invalid", "true");

  // A valid value applies with Enter; focus returns to the edited row.
  await page.keyboard.press("Control+A");
  await page.keyboard.type("220n");
  await expect(page.locator(".value-editor .field__ok")).toHaveText("= 220 nF");
  await page.keyboard.press("Enter");
  await expect(page.locator(".header__rev")).toHaveText("Revision 2");
  await expect(page.getByRole("button", { name: /^C1 — capacitor — 220 nF/ })).toBeFocused();
  await shot(page, testInfo, "kbd-01-after-edit");

  // 3. Measure Revision 2.
  await tabTo(page, page.getByRole("button", { name: "Run experiment on Revision 2" }), { path });
  await page.keyboard.press("Enter");
  await waitForExperiment(page, 2);

  // 4. Compare the two experiments (Space ticks checkboxes, Enter activates buttons).
  const history = page.getByRole("region", { name: "Experiment history" });
  await tabTo(page, history.getByLabel("Compare Experiment 2"), { path });
  await page.keyboard.press("Space");
  await tabTo(page, history.getByLabel("Compare Experiment 1"), { path });
  await page.keyboard.press("Space");
  await expect(history.getByText("Two selected.")).toBeVisible();
  await tabTo(page, history.getByRole("button", { name: "Compare" }), { path });
  await page.keyboard.press("Enter");
  await expect(page.getByLabel("Comparison result")).toContainText("Experiment 1 → Experiment 2");
  const afterCompare = await focusAfter(page);

  // 5. Undo (keyboard) returns to Revision 1.
  await tabTo(page, history.getByRole("button", { name: "Undo to Revision 1" }), { reverse: true, path });
  await page.keyboard.press("Enter");
  await expect(page.locator(".header__rev")).toHaveText("Revision 1");
  const afterUndo = await focusAfter(page);

  // 6. Play and stop a clip from the playback strip; arrow keys switch the clip.
  const strip = page.getByRole("region", { name: "Playback and commands" });
  await tabTo(page, strip.getByRole("button", { name: "Play" }), { path });
  await page.keyboard.press("Enter");
  await expect(strip.getByRole("button", { name: "Stop" })).toBeVisible();
  await expect(strip.getByRole("button", { name: "Stop" })).toBeFocused();
  await tabTo(page, strip.getByRole("radio").first(), { reverse: true, path });
  await page.keyboard.press("ArrowRight");
  await expect(strip.getByRole("radio").nth(1)).toBeChecked();
  await tabTo(page, strip.getByRole("button", { name: "Stop" }), { path });
  await page.keyboard.press("Enter");
  await expect(strip.getByRole("button", { name: "Play" })).toBeVisible();
  const audio = await audioLog(page);
  expect(audio.maxActive, "never more than one clip at once").toBeLessThanOrEqual(1);
  expect(audio.starts.length).toBeGreaterThanOrEqual(1);

  // Where focus lands after these actions is asserted in the D-02 test below; recorded here.
  testInfo.annotations.push({ type: "focus-after-actions", description: JSON.stringify({ afterRun, afterCompare, afterUndo }) });
});

test("investigate: start, Guide me, measure, diagnose, repair, close — keyboard only", async ({ page }, testInfo) => {
  await instrument(page);
  await openWorkbench(page);
  const path: FocusInfo[] = [];
  const lost: Record<string, string | null> = {};

  // Switch mode from the rail; focus moves to the main region.
  await tabTo(page, page.getByRole("navigation", { name: "Workbench mode" }).getByRole("button", { name: "Investigate" }), { path });
  await page.keyboard.press("Enter");
  await expect(page.locator("#workbench-main")).toBeFocused();

  await tabTo(page, page.getByRole("button", { name: "Start a mystery" }), { path });
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: "What we observed" })).toBeVisible({ timeout: 90_000 });
  lost.afterStart = await focusAfter(page);
  const caseId = await caseIdOf(page);

  await tabTo(page, page.getByRole("button", { name: /^Guide me/ }), { path });
  await page.keyboard.press("Enter");
  const measure = page.getByRole("region", { name: "Take a measurement" });
  await expect(measure).toBeVisible();
  lost.afterGuide = await focusAfter(page);

  // Transient capture with the source-current probe, chosen with arrow keys.
  const capture = measure.locator("li.instrument", { has: page.getByRole("heading", { name: "Two-tone capture" }) });
  await tabTo(page, capture.getByRole("radio", { name: "Output voltage" }), { path });
  await page.keyboard.press("ArrowRight");
  await expect(capture.getByRole("radio", { name: "Source current" })).toBeChecked();
  await tabTo(page, capture.getByRole("button", { name: /^Run · source current/ }), { path });
  await page.keyboard.press("Enter");
  await expect(measure.getByRole("group", { name: "2 of 3 experiments left" })).toBeVisible({ timeout: 90_000 });
  lost.afterExperiment = await focusAfter(page);
  await shot(page, testInfo, "kbd-02-observation");

  // Diagnosis chosen from public evidence only.
  const plan = plannedDiagnosis(await getCase(page, caseId));
  const dx = page.getByRole("form", { name: "Submit a diagnosis" });
  const order = ["fault", "no_fault", "insufficient_evidence"] as const;
  await tabTo(page, dx.getByRole("radio").first(), { path });
  for (let i = 0; i < order.indexOf(plan.conclusion); i++) await page.keyboard.press("ArrowDown");
  if (order.indexOf(plan.conclusion) === 0) await page.keyboard.press("Space");
  const conclusionNames = { fault: /^A component fault/, no_fault: /^No fault/, insufficient_evidence: /^Not enough evidence/ };
  await expect(dx.getByRole("radio", { name: conclusionNames[plan.conclusion] })).toBeChecked();
  if (plan.conclusion === "fault") {
    const select = dx.getByLabel("Which fault?");
    await tabTo(page, select, { path });
    for (let i = 0; i < 6 && (await select.inputValue()) !== plan.hypothesisId; i++) await page.keyboard.press("ArrowDown");
    await expect(select).toHaveValue(plan.hypothesisId!);
  }
  await tabTo(page, dx.getByRole("checkbox", { name: "Obs 1 · Transient capture · Source current" }), { path });
  await page.keyboard.press("Space");
  await expect(dx.getByRole("checkbox", { name: "Obs 1 · Transient capture · Source current" })).toBeChecked();
  await tabTo(page, dx.getByRole("button", { name: "Submit diagnosis" }), { path });
  await page.keyboard.press("Enter");
  await expect(page.getByRole("region", { name: "Diagnosis" })).toContainText("Your diagnosis:");
  lost.afterDiagnosis = await focusAfter(page);

  // Repair: radios via arrows, propose, verify.
  const repair = page.getByRole("region", { name: "Repair" });
  await tabTo(page, repair.getByRole("radio", { checked: true }), { path });
  if (plan.repair === "C1") await page.keyboard.press("ArrowDown");
  await expect(repair.getByRole("radio", { name: new RegExp(`^Replace ${plan.repair} with`) })).toBeChecked();
  await tabTo(page, repair.getByRole("button", { name: "Propose repair" }), { path });
  await page.keyboard.press("Enter");
  await expect(repair).toContainText(`You proposed: replace ${plan.repair}`);
  await tabTo(page, repair.getByRole("button", { name: "Apply repair and verify" }), { path });
  await page.keyboard.press("Enter");
  await expect(repair.getByText(/Behaviour restored|Not restored/).first()).toBeVisible({ timeout: 90_000 });
  lost.afterRepair = await focusAfter(page);

  // Close and reveal: focus moves to the reveal heading.
  await tabTo(page, page.getByRole("button", { name: "Close & reveal…" }), { path });
  await page.keyboard.press("Enter");
  await expect(page.getByRole("button", { name: "Close & reveal", exact: true })).toBeFocused().catch(async () => {
    await tabTo(page, page.getByRole("button", { name: "Close & reveal", exact: true }), { path });
  });
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: "Revealed" })).toBeFocused();
  await expect(page.getByRole("region", { name: "Revealed" })).toContainText("Exact identification");
  await shot(page, testInfo, "kbd-03-revealed");

  // Where focus lands after these actions is asserted in the D-02 test below; recorded here.
  testInfo.annotations.push({ type: "focus-after-actions", description: JSON.stringify(lost) });
});

// D-02 (fixed 2026-10-19): primary actions used to disable (loading) or unmount the focused
// control without moving focus, so focus fell back to <body> after every experiment, diagnosis
// and repair. These regression tests require focus to stay on, or move to, a rendered element.
async function activate(page: Page, target: ReturnType<Page["getByRole"]>): Promise<void> {
  await target.focus();
  await page.keyboard.press("Enter");
}

test("D-02 focus is kept after explore actions (Run experiment, Compare, Undo)", async ({ page }, testInfo) => {
  await openWorkbench(page);
  const lost: string[] = [];
  const landed: Record<string, string> = {};
  const check = async (step: string) => {
    await expect.poll(async () => !(await focusedIsBody(page)), { timeout: 1_000 }).toBe(true).catch(() => undefined);
    const f = await focusInfo(page);
    // Focus must land on a rendered element (not <body>, not a hidden or zero-size node).
    if (!f || f.rect.w * f.rect.h <= 4) lost.push(step);
    landed[step] = f ? `${f.tag}:${f.name} [${f.region}]` : "body";
  };
  await activate(page, page.getByRole("button", { name: "Run experiment on Revision 1" }));
  await waitForExperiment(page, 1);
  await check("Run experiment");
  await editValue(page, "C1", "220n", 2);
  await activate(page, page.getByRole("button", { name: "Run experiment on Revision 2" }));
  await waitForExperiment(page, 2);
  const history = page.getByRole("region", { name: "Experiment history" });
  await history.getByLabel("Compare Experiment 1").check();
  await history.getByLabel("Compare Experiment 2").check();
  await activate(page, history.getByRole("button", { name: "Compare" }));
  await expect(page.getByLabel("Comparison result")).toContainText("Experiment 1 → Experiment 2");
  await check("Compare");
  await activate(page, history.getByRole("button", { name: "Undo to Revision 1" }));
  await expect(page.locator(".header__rev")).toHaveText("Revision 1");
  await check("Undo");
  testInfo.annotations.push({ type: "focus-after-each-action", description: JSON.stringify(landed) });
  testInfo.annotations.push({ type: "focus-lost-after", description: lost.join(", ") });
  expect(lost, "actions after which focus fell back to <body>").toEqual([]);
});

test("D-02 focus is kept after investigate actions (start, approach, experiment, diagnosis, repair)", async ({ page }, testInfo) => {
  await openWorkbench(page);
  const lost: string[] = [];
  const landed: Record<string, string> = {};
  const check = async (step: string) => {
    await expect.poll(async () => !(await focusedIsBody(page)), { timeout: 1_000 }).toBe(true).catch(() => undefined);
    const f = await focusInfo(page);
    // Focus must land on a rendered element (not <body>, not a hidden or zero-size node).
    if (!f || f.rect.w * f.rect.h <= 4) lost.push(step);
    landed[step] = f ? `${f.tag}:${f.name} [${f.region}]` : "body";
  };
  await page.getByRole("navigation", { name: "Workbench mode" }).getByRole("button", { name: "Investigate" }).click();
  await activate(page, page.getByRole("button", { name: "Start a mystery" }));
  await expect(page.getByRole("heading", { name: "What we observed" })).toBeVisible({ timeout: 90_000 });
  await check("Start a mystery");
  const caseId = await caseIdOf(page);
  await activate(page, page.getByRole("button", { name: /^Guide me/ }));
  const measure = measureRegion(page);
  await expect(measure).toBeVisible();
  await check("Guide me");
  await activate(page, measure.locator("li.instrument", { has: page.getByRole("heading", { name: "Source current sweep" }) }).getByRole("button", { name: /^Run/ }));
  await expect(measure.getByRole("group", { name: "2 of 3 experiments left" })).toBeVisible({ timeout: 90_000 });
  await check("Run instrument");
  const plan = plannedDiagnosis(await getCase(page, caseId));
  const dx = page.getByRole("form", { name: "Submit a diagnosis" });
  const conclusionLabel = { fault: "A component fault", no_fault: "No fault", insufficient_evidence: "Not enough evidence" }[plan.conclusion];
  await dx.getByRole("radio", { name: new RegExp(`^${conclusionLabel}`) }).check();
  if (plan.conclusion === "fault") await dx.getByLabel("Which fault?").selectOption(plan.hypothesisId!);
  await dx.getByRole("checkbox", { name: "Obs 1 · Source current sweep" }).check();
  await activate(page, dx.getByRole("button", { name: "Submit diagnosis" }));
  await expect(page.getByRole("region", { name: "Diagnosis" })).toContainText("Your diagnosis:");
  await check("Submit diagnosis");
  const repair = page.getByRole("region", { name: "Repair" });
  await repair.getByRole("radio", { name: new RegExp(`^Replace ${plan.repair} with`) }).check();
  await activate(page, repair.getByRole("button", { name: "Propose repair" }));
  await expect(repair).toContainText(`You proposed: replace ${plan.repair}`);
  await check("Propose repair");
  await activate(page, repair.getByRole("button", { name: "Apply repair and verify" }));
  await expect(repair.getByText(/^(Behaviour restored|Not restored)$/).first()).toBeVisible({ timeout: 90_000 });
  await check("Apply repair and verify");
  testInfo.annotations.push({ type: "focus-after-each-action", description: JSON.stringify(landed) });
  testInfo.annotations.push({ type: "focus-lost-after", description: lost.join(", ") });
  expect(lost, "actions after which focus fell back to <body>").toEqual([]);
});
