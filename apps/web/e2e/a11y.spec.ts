// Automated accessibility checks (CLAUDE.md "Interface and accessibility"; task lens D).
// axe-core runs on the real UI in each main state at desktop 1440×900 and narrow 390×844 (each
// narrow tab separately, because inactive tab panels are hidden). Automated audits do not
// validate accessibility on their own: no real screen-reader session is performed here.
import { expect, test, type Page, type TestInfo } from "@playwright/test";
import {
  audioLog,
  axeScan,
  caseIdOf,
  chooseGuideMe,
  closeAndReveal,
  copyProblems,
  getCase,
  horizontalOverflow,
  instrument,
  learnerText,
  liveLog,
  measureRegion,
  openWorkbench,
  plannedDiagnosis,
  proposeAndVerify,
  runCaseInstrument,
  runExploreExperiment,
  shot,
  startMystery,
  submitDiagnosis,
  waitForExperiment,
  type AxeFinding,
  type CopyProblem,
} from "./support/workbench";

// Reported axe findings that are still open, excluded from the walk's assertion so that any NEW
// violation still fails the suite. Empty: D-03 (scrollable-region-focusable) was fixed and is
// asserted again by the walk.
const KNOWN: { id: string; rule: string; target: RegExp }[] = [];
const isKnown = (f: AxeFinding) => KNOWN.some((k) => k.rule === f.rule && f.targets.every((t) => k.target.test(t)));

const DESKTOP = { width: 1440, height: 900 };
const NARROW = { width: 390, height: 844 };
const TABS = ["Circuit", "Instruments", "Lab notes"] as const;

interface Scan {
  state: string;
  viewport: string;
  findings: AxeFinding[];
  overflow: number;
  copy: CopyProblem[];
}

/** Open every closed disclosure that holds results (data tables, checks) so axe sees them. */
async function openDisclosures(page: Page): Promise<void> {
  for (const summary of await page.locator("details:not([open]) > summary").all()) {
    if (await summary.isVisible()) await summary.click();
  }
}

/** Scan the current state at desktop, then at narrow width on each tab, then restore desktop. */
async function scanState(page: Page, testInfo: TestInfo, state: string, scans: Scan[]): Promise<void> {
  await page.setViewportSize(DESKTOP);
  await expect(page.getByRole("tablist", { name: "Workbench sections" })).toHaveCount(0);
  await openDisclosures(page);
  scans.push({
    state,
    viewport: "1440x900",
    findings: await axeScan(page, `${state}-desktop`),
    overflow: await horizontalOverflow(page),
    copy: copyProblems(await learnerText(page)),
  });
  await shot(page, testInfo, `a11y-${state}-desktop`);

  await page.setViewportSize(NARROW);
  await expect(page.getByRole("tablist", { name: "Workbench sections" })).toBeVisible();
  for (const tab of TABS) {
    const control = page.getByRole("tab", { name: tab });
    await control.click();
    await expect(control).toHaveAttribute("aria-selected", "true");
    const slug = tab.toLowerCase().replace(/ /g, "-");
    scans.push({
      state,
      viewport: `390x844/${slug}`,
      findings: await axeScan(page, `${state}-narrow-${slug}`),
      overflow: await horizontalOverflow(page),
      copy: copyProblems(await learnerText(page)),
    });
    await shot(page, testInfo, `a11y-${state}-narrow-${slug}`);
  }
  await page.getByRole("tab", { name: "Circuit" }).click();
  await page.setViewportSize(DESKTOP);
}

test("axe WCAG 2 A/AA, overflow and learner copy across the main states (desktop and narrow)", async ({ page }, testInfo) => {
  test.setTimeout(600_000);
  const scans: Scan[] = [];

  await openWorkbench(page);
  await scanState(page, testInfo, "01-initial", scans);

  await runExploreExperiment(page, 2);
  await scanState(page, testInfo, "02-measured", scans);

  const caseId = await startMystery(page);
  await scanState(page, testInfo, "03-symptom", scans);

  await chooseGuideMe(page);
  await runCaseInstrument(page, "Two-tone capture", "Output voltage");
  await scanState(page, testInfo, "04-observation-transient", scans);
  await runCaseInstrument(page, "Source current sweep");
  await scanState(page, testInfo, "05-observation-sweep", scans);

  // The diagnosis form with validation errors showing (submitted empty).
  const dx = page.getByRole("form", { name: "Submit a diagnosis" });
  await dx.getByRole("button", { name: "Submit diagnosis" }).click();
  await expect(dx.getByText("Choose a conclusion.")).toBeVisible();
  scans.push({ state: "06-diagnosis-errors", viewport: "1440x900", findings: await axeScan(page, "06-diagnosis-errors-desktop"), overflow: 0, copy: [] });

  const plan = plannedDiagnosis(await getCase(page, caseId));
  await submitDiagnosis(page, plan, "Obs 2 · Source current sweep");
  await scanState(page, testInfo, "07-diagnosis", scans);

  await proposeAndVerify(page, plan.repair);
  await scanState(page, testInfo, "08-repair", scans);

  await closeAndReveal(page);
  await scanState(page, testInfo, "09-reveal", scans);

  // ---- report and assert ----
  const asserted = scans.flatMap((s) => s.findings.filter((f) => f.asserted).map((f) => ({ where: `${s.state} @ ${s.viewport}`, ...f })));
  const info = scans.flatMap((s) => s.findings.filter((f) => !f.asserted).map((f) => ({ where: `${s.state} @ ${s.viewport}`, rule: f.rule, impact: f.impact, nodes: f.targets.length })));
  testInfo.annotations.push({
    type: "axe-summary",
    description: scans.map((s) => `${s.state} @ ${s.viewport}: ${s.findings.filter((f) => f.asserted).map((f) => `${f.rule}(${f.targets.length})`).join(", ") || "0 A/AA violations"}`).join("\n"),
  });
  testInfo.annotations.push({ type: "axe-wcag21-22-info", description: JSON.stringify(info) });
  testInfo.annotations.push({ type: "axe-asserted-detail", description: JSON.stringify(asserted.map((a) => ({ where: a.where, rule: a.rule, impact: a.impact, targets: a.targets.slice(0, 6), html: a.html.slice(0, 3) }))) });
  const overflow = scans.filter((s) => s.overflow > 1).map((s) => `${s.state} @ ${s.viewport}: ${s.overflow}px`);
  const copy = scans.flatMap((s) => s.copy.map((c) => `${s.state} @ ${s.viewport}: ${c.excerpt}`));

  expect(overflow, "no horizontal page scroll").toEqual([]);
  expect(copy, "no raw IDs, stack traces or HTTP codes in learner-facing text").toEqual([]);
  const unexpected = asserted.filter((a) => !isKnown(a));
  expect(unexpected.map((a) => `${a.where}: ${a.rule} (${a.impact}) ${a.targets.slice(0, 4).join(" | ")}`), "axe wcag2a/wcag2aa violations (excluding reported findings)").toEqual([]);
});

test("charts are labelled, summarised in text, and distinguish traces by line style (not colour only)", async ({ page }) => {
  await openWorkbench(page);
  const figures = page.locator("figure.chart");
  const count = await figures.count();
  expect(count).toBeGreaterThanOrEqual(2);
  for (let i = 0; i < count; i++) {
    const fig = figures.nth(i);
    const name = await fig.evaluate((el) => {
      const id = el.getAttribute("aria-labelledby");
      return id ? (document.getElementById(id)?.textContent ?? "") : (el.getAttribute("aria-label") ?? "");
    });
    expect(name.trim(), `figure ${i} has an accessible name`).not.toBe("");
    await expect(fig.locator("figcaption"), `figure "${name}" has a text summary`).not.toBeEmpty();
    for (const svg of await fig.locator('svg[role="img"]').all()) {
      expect((await svg.getAttribute("aria-label"))?.trim(), `plot in "${name}" has a text alternative`).toBeTruthy();
    }
  }
  // Time domain: input is a dashed reference, output a solid primary trace; both are in the legend.
  const time = page.getByRole("figure", { name: "Time domain" });
  const dash = async (cls: string) => time.locator(`path.trace--${cls}`).first().evaluate((p) => getComputedStyle(p).strokeDasharray);
  expect(await dash("reference")).not.toBe("none");
  expect(await dash("primary")).toBe("none");
  await expect(time.getByRole("list", { name: "Time domain legend" })).toContainText("Input");
  await expect(time.getByRole("list", { name: "Time domain legend" })).toContainText("Output");
  // The measured response is also available as a data table.
  const response = page.getByRole("figure", { name: "Frequency response" });
  await response.locator("summary", { hasText: "Data table" }).click();
  await expect(response.getByRole("table")).toBeVisible();
  expect(await response.getByRole("table").getByRole("row").count()).toBeGreaterThan(10);
});

test("live regions: one polite and one assertive region; announcements are short and not flooded", async ({ page }, testInfo) => {
  await instrument(page);
  await openWorkbench(page);
  const regions = () =>
    page.evaluate(() => ({
      polite: document.querySelectorAll('[aria-live="polite"], [role="status"]').length,
      assertive: document.querySelectorAll('[aria-live="assertive"], [role="alert"]').length,
    }));
  expect(await regions()).toEqual({ polite: 1, assertive: 1 });

  const since = async (from: number) => (await liveLog(page)).slice(from);
  let mark = (await liveLog(page)).length;
  await runExploreExperiment(page, 2);
  await expect.poll(async () => (await since(mark)).some((r) => r.text.startsWith("Experiment finished"))).toBe(true);
  const exploreRun = await since(mark);

  const caseId = await startMystery(page);
  expect(caseId).toBeTruthy();
  await chooseGuideMe(page);
  mark = (await liveLog(page)).length;
  await runCaseInstrument(page, "Output response sweep");
  await expect.poll(async () => (await since(mark)).some((r) => /experiments left/.test(r.text))).toBe(true);
  const caseRun = await since(mark);
  expect(await regions()).toEqual({ polite: 1, assertive: 1 });

  const all = await liveLog(page);
  testInfo.annotations.push({ type: "announcements", description: all.map((r) => `[${r.kind}] ${r.text}`).join("\n") });
  // A run produces a start and a finish message, not a stream of progress or samples.
  expect(exploreRun.length, `explore run announcements: ${exploreRun.map((r) => r.text).join(" | ")}`).toBeLessThanOrEqual(3);
  expect(caseRun.length, `case experiment announcements: ${caseRun.map((r) => r.text).join(" | ")}`).toBeLessThanOrEqual(3);
  for (const r of all) {
    expect(r.text.length, `announcement is short: ${r.text}`).toBeLessThanOrEqual(320);
    expect((r.text.match(/\d+(?:\.\d+)?/g) ?? []).length, `announcement is not a number dump: ${r.text}`).toBeLessThanOrEqual(16);
    expect(copyProblems(r.text), `announcement copy: ${r.text}`).toEqual([]);
  }
  expect(all.filter((r) => r.kind === "assertive"), "no assertive announcements on a successful path").toEqual([]);
});

test("prefers-reduced-motion removes transitions and spinner/skeleton animation", async ({ page }) => {
  await openWorkbench(page);
  const maxDuration = (selector: string, prop: "transitionDuration" | "animationDuration") =>
    page.locator(selector).first().evaluate((el, p) => Math.max(...getComputedStyle(el)[p].split(",").map((v) => parseFloat(v) * (v.trim().endsWith("ms") ? 0.001 : 1))), prop);
  // Baseline: motion exists without the preference (so the check below is meaningful).
  await page.emulateMedia({ reducedMotion: "no-preference" });
  expect(await maxDuration(".btn", "transitionDuration")).toBeGreaterThan(0.05);

  await page.emulateMedia({ reducedMotion: "reduce" });
  expect(await page.evaluate(() => matchMedia("(prefers-reduced-motion: reduce)").matches)).toBe(true);
  expect(await maxDuration(".btn", "transitionDuration")).toBeLessThanOrEqual(0.001);
  // While a run is in progress the spinner does not spin and loading skeletons do not pulse.
  await page.getByRole("button", { name: /^Run experiment on Revision \d+$/ }).click();
  const spinner = page.locator(".spinner").first();
  await expect(spinner).toBeVisible();
  const spin = await spinner.evaluate((el) => ({ name: getComputedStyle(el).animationName, dur: getComputedStyle(el).animationDuration }));
  expect(spin.name === "none" || parseFloat(spin.dur) <= 0.001, `spinner animation ${JSON.stringify(spin)}`).toBe(true);
  const skeleton = page.locator(".skeleton").first();
  if (await skeleton.isVisible()) {
    const pulse = await skeleton.evaluate((el) => ({ name: getComputedStyle(el).animationName, dur: getComputedStyle(el).animationDuration, n: getComputedStyle(el).animationIterationCount }));
    expect(pulse.name === "none" || (parseFloat(pulse.dur) <= 0.001 && pulse.n === "1"), `skeleton animation ${JSON.stringify(pulse)}`).toBe(true);
  }
  await expect(page.locator(".instruments__meta").first()).toContainText("Experiment 2 ·", { timeout: 90_000 });
});

test("audio: nothing plays without a Play gesture, and only one source plays at a time", async ({ page }, testInfo) => {
  await instrument(page);
  await openWorkbench(page);
  await runExploreExperiment(page, 2);
  // Running an experiment (a click) must not start audio or even create an AudioContext.
  let log = await audioLog(page);
  expect(log.contexts.length, "no AudioContext before Play").toBe(0);
  expect(log.starts.length, "no source started before Play").toBe(0);
  expect(log.mediaPlays.length, "no media element playback").toBe(0);

  const strip = page.getByRole("region", { name: "Playback and commands" });
  await strip.getByRole("button", { name: "Play" }).click();
  await expect(strip.getByText(/^Playing: /)).toBeVisible();
  // Switch to the other clip while playing: the first source must stop before the next starts.
  await strip.getByRole("radio").nth(1).check();
  await expect(strip.getByText(/^Playing: Output/)).toBeVisible();
  await strip.getByRole("button", { name: "Stop" }).click();
  await expect(strip.getByRole("button", { name: "Play" })).toBeVisible();
  log = await audioLog(page);
  expect(log.contexts.length).toBe(1);
  expect(log.contexts[0]!.sinceGesture, "AudioContext created right after a user gesture").not.toBeNull();
  // The gesture is stamped at pointerdown, so this also measures main-thread queueing before the
  // click handler runs (up to ~1.9 s observed on the loaded dev server). The app creates the
  // context synchronously inside the Play click handler; 5 s is Chrome's user-activation window.
  expect(log.contexts[0]!.sinceGesture!).toBeLessThan(5000);
  expect(log.starts.length).toBe(2);
  expect(log.active, "nothing playing after Stop").toBe(0);

  // Investigate: the symptom never autoplays; rapid Play clicks never overlap.
  await startMystery(page);
  expect((await audioLog(page)).starts.length, "symptom does not autoplay").toBe(2);
  const symptom = page.getByRole("region", { name: "What we observed" });
  const playButtons = symptom.getByRole("button", { name: /^Play / });
  expect(await playButtons.count()).toBeGreaterThanOrEqual(2);
  await playButtons.nth(0).click();
  await playButtons.nth(1).click();
  await playButtons.nth(0).click();
  await expect(strip.getByText(/^Playing: /)).toBeVisible();
  await strip.getByRole("button", { name: "Stop" }).click();
  log = await audioLog(page);
  testInfo.annotations.push({ type: "audio-log", description: JSON.stringify(log) });
  expect(log.maxActive, "never more than one clip at once").toBe(1);
  expect(log.mediaPlays.length).toBe(0);
  for (const s of log.starts) expect(s.sinceGesture, "every start follows a user gesture").not.toBeNull();
  expect(log.active).toBe(0);
});

test("the whole flow is completable with circuit audio muted and Web Audio unavailable", async ({ page }, testInfo) => {
  await instrument(page, { disableWebAudio: true });
  await openWorkbench(page);
  expect(await page.evaluate(() => typeof (window as unknown as { AudioContext?: unknown }).AudioContext)).toBe("undefined");
  const strip = page.getByRole("region", { name: "Playback and commands" });
  await strip.getByRole("button", { name: "Mute circuit audio" }).click();
  await expect(strip.getByRole("button", { name: "Mute circuit audio" })).toHaveAttribute("aria-pressed", "true");

  // Explore: the measured tone levels are text, not sound.
  await runExploreExperiment(page, 2);
  const levels = page.getByRole("list", { name: "Measured tone levels" });
  await expect(levels).toContainText(/440 Hz\s*−?\d+(\.\d)? dB/);
  await expect(levels).toContainText(/4 kHz\s*−\d+(\.\d)? dB/);
  // Pressing Play without Web Audio explains itself calmly and the page keeps working.
  await strip.getByRole("button", { name: "Play" }).click();
  const status = strip.locator(".playback__status-text");
  await expect(status).toContainText(/Couldn’t play/);
  expect(copyProblems(await status.innerText())).toEqual([]);

  // Investigate with only an output-voltage capture: R and C changes are indistinguishable from
  // voltage alone, so this also exercises the ambiguity path when the fault is R or C.
  const caseId = await startMystery(page);
  const symptom = page.getByRole("region", { name: "What we observed" });
  await expect(symptom.getByRole("table")).toContainText("Expected");
  await chooseGuideMe(page);
  await runCaseInstrument(page, "Two-tone capture", "Output voltage");
  await expect(page.getByRole("table", { name: /Obs 1 · Transient capture · Output voltage: tone levels/ })).toBeVisible();
  const c = await getCase(page, caseId);
  const plan = plannedDiagnosis(c);
  testInfo.annotations.push({ type: "remaining-after-voltage-only", description: c.evidence.remaining.join(", ") });
  await submitDiagnosis(page, plan, "Obs 1 · Transient capture · Output voltage");
  if (plan.conclusion === "insufficient_evidence") await expect(page.getByRole("region", { name: "Diagnosis" })).toContainText("Not enough evidence to decide");
  await proposeAndVerify(page, plan.repair);
  await expect(page.getByRole("region", { name: "Repair" })).toContainText("Largest gain difference");
  await closeAndReveal(page);
  const reveal = page.getByRole("region", { name: "Revealed" });
  await expect(reveal).toContainText("Exact identification");
  await expect(reveal).toContainText("Functional recovery");
  expect((await audioLog(page)).starts.length).toBe(0);
  await shot(page, testInfo, "noaudio-reveal");
});

test("D-13 scrolling a panel to its end never scrolls the page itself", async ({ page }, testInfo) => {
  await openWorkbench(page);
  await startMystery(page);
  await chooseGuideMe(page);
  const wheelToEnd = async (x: number, y: number) => {
    await page.mouse.move(x, y);
    for (let i = 0; i < 30; i++) await page.mouse.wheel(0, 400);
    await page.waitForTimeout(300);
    return page.evaluate(() => ({ scrollY: Math.round(scrollY), extra: document.documentElement.scrollHeight - innerHeight }));
  };
  const desktop = await wheelToEnd(1260, 500);
  await shot(page, testInfo, "D-13-desktop-after-wheel");
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.setViewportSize(NARROW);
  await page.getByRole("tab", { name: "Lab notes" }).click();
  const narrow = await wheelToEnd(200, 400);
  await shot(page, testInfo, "D-13-narrow-after-wheel");
  testInfo.annotations.push({ type: "document-scroll", description: JSON.stringify({ desktop, narrow }) });
  expect({ desktop: desktop.scrollY, narrow: narrow.scrollY }, "the document stays at the top; only panels scroll").toEqual({ desktop: 0, narrow: 0 });
});

test.describe("narrow screen 390×844", () => {
  test.use({ viewport: NARROW });

  test("the full workflow is reachable through the section tabs without horizontal scroll", async ({ page }, testInfo) => {
    const overflow: string[] = [];
    const check = async (step: string) => {
      const px = await horizontalOverflow(page);
      if (px > 1) overflow.push(`${step}: ${px}px`);
    };
    const tab = async (name: (typeof TABS)[number]) => {
      await page.getByRole("tab", { name }).click();
      await expect(page.getByRole("tab", { name })).toHaveAttribute("aria-selected", "true");
    };
    await page.goto("/lab");
    const tabs = page.getByRole("tablist", { name: "Workbench sections" });
    await expect(tabs).toBeVisible();
    // Arrow keys move between tabs (roving tabindex).
    await page.getByRole("tab", { name: "Circuit" }).focus();
    await page.keyboard.press("ArrowRight");
    await expect(page.getByRole("tab", { name: "Instruments" })).toBeFocused();
    await expect(page.getByRole("tab", { name: "Instruments" })).toHaveAttribute("aria-selected", "true");
    await check("initial");

    // The opening experiment is measured automatically on a first visit.
    await waitForExperiment(page, 1);
    await expect(page.getByRole("list", { name: "Measured tone levels" })).toBeVisible();
    await check("measured");
    await shot(page, testInfo, "narrow-01-measured", true);

    await tab("Circuit");
    await page.getByRole("button", { name: "Edit C1 value" }).click();
    await page.getByLabel(/value for C1$/).fill("220n");
    await expect(page.locator(".value-editor .field__ok")).toBeVisible();
    await page.locator(".value-editor").getByRole("button", { name: "Apply" }).click();
    await expect(page.locator(".header__rev")).toHaveText("Revision 2");
    await check("edited");

    await page.getByRole("navigation", { name: "Workbench mode" }).getByRole("button", { name: "Investigate" }).click();
    await tab("Lab notes");
    await page.getByRole("button", { name: "Start a mystery" }).click();
    await expect(page.getByRole("heading", { name: "What we observed" })).toBeVisible({ timeout: 90_000 });
    const caseId = await caseIdOf(page);
    await check("symptom");
    await shot(page, testInfo, "narrow-02-symptom", true);
    await chooseGuideMe(page);
    await runCaseInstrument(page, "Source current sweep");
    await check("observation");
    await tab("Instruments");
    await expect(page.getByRole("figure", { name: "Source current sweep" })).toBeVisible();
    await check("observation-chart");
    await shot(page, testInfo, "narrow-03-observation-chart", true);
    await tab("Lab notes");
    const plan = plannedDiagnosis(await getCase(page, caseId));
    await submitDiagnosis(page, plan, "Obs 1 · Source current sweep");
    await proposeAndVerify(page, plan.repair);
    await check("repair");
    await closeAndReveal(page);
    await expect(page.getByRole("heading", { name: "Revealed" })).toBeFocused();
    await check("reveal");
    await shot(page, testInfo, "narrow-04-reveal", true);
    await expect(measureRegion(page)).toHaveCount(0);
    expect(overflow, "no horizontal page scroll at 390 px").toEqual([]);
  });
});
