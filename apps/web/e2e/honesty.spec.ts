// Honest UI states (CLAUDE.md "Agent behavior and tools", "Interface and accessibility"):
// AI-unavailable explanations, calm field-specific validation, failures that preserve previous
// results, no raw IDs / stack traces / HTTP status codes in learner-facing text, and labels that
// do not overstate outcomes. NEBIUS_API_KEY is deliberately empty: live AI is never exercised.
//
// Tests named "D-xx …" assert the corrected behaviour for finding D-xx in
// docs/verification/review-D-ui-a11y.md (all fixed and re-verified on 2026-10-19). They were
// `test.fail()` while the defects were open and are now plain regression tests.
import { expect, test, type Page } from "@playwright/test";
import {
  axeScan,
  caseIdOf,
  chooseGuideMe,
  closeAndReveal,
  copyProblems,
  editValue,
  getCase,
  learnerText,
  measureRegion,
  openWorkbench,
  plannedDiagnosis,
  proposeAndVerify,
  runCaseInstrument,
  runExploreExperiment,
  shot,
  startMystery,
  submitDiagnosis,
  TARGET,
} from "./support/workbench";

const strip = (page: Page) => page.getByRole("region", { name: "Playback and commands" });
const notes = (page: Page) => page.getByRole("complementary", { name: "Lab notes" });

/** Text of the element(s) an element's aria-describedby points at. */
async function describedBy(page: Page, locator: ReturnType<Page["locator"]>): Promise<string> {
  const ids = (await locator.getAttribute("aria-describedby")) ?? "";
  const parts: string[] = [];
  for (const id of ids.split(/\s+/).filter(Boolean)) parts.push((await page.locator(`[id="${id}"]`).innerText()).trim());
  return parts.join(" ");
}

// ---------------------------------------------------------------------------------------------
// AI unavailable
// ---------------------------------------------------------------------------------------------

test("AI unavailable: setup is explained and AI controls are disabled with a stated reason", async ({ page }, testInfo) => {
  await openWorkbench(page);
  await expect(page.getByRole("banner")).toContainText("AI unavailable");
  const callout = notes(page).locator(".callout", { hasText: "AI lab partner unavailable" });
  await expect(callout).toContainText("NEBIUS_API_KEY");
  await expect(callout).toContainText(".env");
  await expect(callout).toContainText(/restart the API/i);
  await expect(callout).toContainText(/simulator and manual investigation keep working/i);

  // Explore command box: disabled, and the reason is programmatically attached.
  const ask = strip(page).getByRole("textbox", { name: "Ask the AI lab partner" });
  await expect(ask).toBeDisabled();
  expect(await describedBy(page, ask)).toMatch(/AI lab partner unavailable/);
  await expect(strip(page).getByRole("button", { name: "Send" })).toBeDisabled();
  // Manual tools still work.
  await runExploreExperiment(page, 1);

  // Investigate: "Investigate for me" is disabled and says why; Guide me works.
  await startMystery(page);
  const agentOption = page.getByRole("button", { name: /^Investigate for me/ });
  await expect(agentOption).toBeDisabled();
  expect(await describedBy(page, agentOption)).toMatch(/AI lab partner unavailable.*Guide me works without AI/);
  const hintBox = strip(page).getByRole("textbox", { name: "Ask for a hint" });
  await expect(hintBox).toBeDisabled();
  expect(await describedBy(page, hintBox)).toMatch(/AI lab partner unavailable/);
  await chooseGuideMe(page);
  await expect(strip(page).getByRole("button", { name: "Hint" })).toBeDisabled();
  await shot(page, testInfo, "honesty-ai-unavailable-guide");
  // The header never claims the AI is ready, and no AI output is shown anywhere.
  await expect(page.getByText(/AI ready/)).toHaveCount(0);
  await expect(page.getByText(/AI diagnosis:|The AI proposed/)).toHaveCount(0);
});

test("D-01 'Ask for a hint' in the measurement card is disabled with a reason while AI is unavailable", async ({ page }, testInfo) => {
  await openWorkbench(page);
  await startMystery(page);
  await chooseGuideMe(page);
  const hint = measureRegion(page).getByRole("button", { name: "Ask for a hint" });
  await expect(hint).toBeVisible();
  const enabled = await hint.isEnabled();
  testInfo.annotations.push({ type: "hint-button-enabled", description: String(enabled) });
  if (enabled) {
    // Record what a learner gets when pressing it (for the report), then assert the contract.
    await hint.click();
    await page.waitForTimeout(1500);
    const panel = measureRegion(page).locator("xpath=ancestor::div[contains(@class,'panel-stack')]");
    testInfo.annotations.push({ type: "after-hint-click", description: (await panel.innerText()).slice(0, 1500) });
    await shot(page, testInfo, "honesty-hint-click-without-ai");
  }
  await expect(hint).toBeDisabled({ timeout: 3_000 });
  expect(await describedBy(page, hint)).toMatch(/AI lab partner unavailable/);
});

test("D-10 after dismissing the AI notice, setup steps are still where the UI says they are", async ({ page }) => {
  await openWorkbench(page);
  await notes(page).getByRole("button", { name: "Dismiss" }).click();
  const note = strip(page).locator(".command__note");
  await expect(note).toContainText("setup steps are in Lab notes");
  // The steps must be perceivable there, not merely present in the DOM: visible directly, or
  // one visible disclosure away.
  const steps = notes(page).getByText(/NEBIUS_API_KEY/).first();
  if (!(await steps.isVisible())) {
    const disclosure = notes(page).locator("summary", { hasText: /setup/i });
    await expect(disclosure, "a visible way to reach the setup steps").toBeVisible({ timeout: 3_000 });
    await disclosure.click();
  }
  await expect(steps).toBeVisible({ timeout: 3_000 });
  await expect(steps).toContainText(".env");
});

// ---------------------------------------------------------------------------------------------
// Validation and failures keep previous results
// ---------------------------------------------------------------------------------------------

test("invalid component values get a calm, field-specific message; no revision; previous results kept", async ({ page }, testInfo) => {
  await openWorkbench(page);
  const levels = page.getByRole("list", { name: "Measured tone levels" });
  const levelsBefore = await levels.innerText();
  const revisionCount = async () => ((await (await page.request.get("/api/workbench")).json()).revisions as { origin: string }[]).filter((r) => r.origin !== "case_intended").length;
  const revisionsBefore = await revisionCount();

  await page.getByRole("button", { name: "Edit C1 value" }).click();
  const input = page.getByLabel(/value for C1$/);
  await expect(input).toBeFocused();
  const messages: Record<string, string> = {};
  for (const text of ["abc", "50", "100 kΩ", "1M", "-5n", ""]) {
    // Wait for this text's own server-side parse (debounced) so the message is not stale.
    const parsed = text ? page.waitForResponse((r) => r.url().endsWith("/api/units/parse") && (r.request().postData() ?? "").includes(JSON.stringify(text).slice(1, -1))) : null;
    await input.fill(text);
    if (parsed) await parsed;
    const hintId = await input.getAttribute("aria-describedby");
    const message = page.locator(`[id="${hintId}"]`);
    if (text === "") {
      // Empty input: a neutral hint, not an error.
      await expect(message).toHaveClass(/field__hint/);
      await expect(input).not.toHaveAttribute("aria-invalid", "true");
    } else {
      await expect(message).toHaveClass(/field__error/, { timeout: 5_000 });
      await expect(input).toHaveAttribute("aria-invalid", "true");
    }
    messages[text || "(empty)"] = (await message.innerText()).trim();
    // Applying an invalid value creates nothing and the measurement stays.
    await page.locator(".value-editor").getByRole("button", { name: "Apply" }).click();
    await expect(page.locator(".header__rev")).toHaveText("Revision 1");
  }
  testInfo.annotations.push({ type: "validation-messages", description: JSON.stringify(messages) });
  await shot(page, testInfo, "honesty-invalid-value");
  for (const [text, msg] of Object.entries(messages)) {
    expect(msg.length, `message for "${text}"`).toBeGreaterThan(5);
    expect(copyProblems(msg), `message for "${text}" is free of codes/internals`).toEqual([]);
    expect(msg, `message for "${text}" is calm`).not.toMatch(/error code|exception|invalid input|422|!$/i);
  }
  // The out-of-range message states the allowed range.
  expect(messages["50"]).toMatch(/1 nF|10 µF|range|between|allowed/i);
  await page.keyboard.press("Escape");
  await expect(input).toHaveCount(0);
  await expect(page.getByRole("button", { name: /^C1 — capacitor — 100 nF/ })).toBeFocused();
  await expect(levels).toHaveText(levelsBefore, { useInnerText: true });
  await expect(page.locator(".instruments__meta").first()).toContainText("Experiment 1 · Revision 1");
  expect(await revisionCount(), "no revision was created by invalid input").toBe(revisionsBefore);
});

test("an invalid goal is explained per field and the saved goal and results are unchanged", async ({ page }) => {
  await openWorkbench(page);
  const goal = page.getByRole("region", { name: "Goal" });
  const before = await goal.locator("table").innerText();
  await goal.getByRole("button", { name: "Edit goal" }).click();
  const form = page.getByRole("form", { name: "Edit goal" });
  await form.getByLabel("Frequency (Hz)").first().fill("5");
  await form.getByRole("button", { name: "Save goal" }).click();
  const problem = form.getByRole("alert");
  await expect(problem).toHaveText("Constraint 1: frequency must be 20 to 20,000 Hz.");
  await form.getByLabel("Frequency (Hz)").first().fill("440");
  await form.getByLabel("Limit (dB)").first().fill("x");
  await form.getByRole("button", { name: "Save goal" }).click();
  await expect(problem).toHaveText("Constraint 1: limit must be 0 to 60 dB.");
  await form.getByRole("button", { name: "Cancel" }).click();
  await expect(goal.locator("table")).toHaveText(before, { useInnerText: true });
  await expect(page.getByRole("list", { name: "Measured tone levels" })).toBeVisible();
});

test("cancelled and failed experiments are explained calmly and keep the previous result", async ({ page }, testInfo) => {
  await openWorkbench(page);
  const levels = page.getByRole("list", { name: "Measured tone levels" });
  const before = await levels.innerText();
  const instruments = page.getByRole("region", { name: "Instruments" });

  // 1. Cancel during a real run.
  await page.getByRole("button", { name: "Run experiment on Revision 1" }).click();
  await instruments.getByRole("button", { name: "Cancel" }).click();
  await expect(instruments).toContainText("The experiment was cancelled. Previous results are unchanged.");
  await expect(page.getByRole("button", { name: "Run experiment on Revision 1" })).toBeFocused();
  await expect(levels).toHaveText(before, { useInnerText: true });
  await shot(page, testInfo, "honesty-cancelled");
  // Whether the server still recorded the cancelled run (reported as an observation).
  await page.waitForTimeout(8000);
  const wb = await (await page.request.get("/api/workbench")).json();
  testInfo.annotations.push({ type: "server-experiments-after-cancel", description: JSON.stringify(wb.experiments.map((e: { status: string }) => e.status)) });

  // 2. A structured simulator failure from the backend (response body mocked for this one request).
  await page.route("**/api/experiments", (route) =>
    route.request().method() === "POST"
      ? route.fulfill({
          status: 500,
          contentType: "application/json",
          body: JSON.stringify({ error: { code: "simulation_failed", message: "The simulator could not finish this run. Nothing was recorded; try again.", retryable: true } }),
        })
      : route.continue(),
  );
  await page.getByRole("button", { name: "Run experiment on Revision 1" }).click();
  await expect(instruments).toContainText("Simulation failed");
  await expect(instruments).toContainText("The previous result is still shown.");
  await expect(levels).toHaveText(before, { useInnerText: true });
  await page.unroute("**/api/experiments");

  // 3. A structured timeout from the backend (response body mocked for this one request).
  await page.route("**/api/experiments", (route) =>
    route.request().method() === "POST"
      ? route.fulfill({
          status: 504,
          contentType: "application/json",
          body: JSON.stringify({ error: { code: "simulation_timeout", message: "The simulation took too long and was stopped. Nothing was recorded; try again.", retryable: true } }),
        })
      : route.continue(),
  );
  await page.getByRole("button", { name: "Run experiment on Revision 1" }).click();
  await expect(instruments).toContainText("Simulation timed out");
  await expect(instruments).toContainText("The previous result is still shown.");
  await expect(levels).toHaveText(before, { useInnerText: true });
  await page.unroute("**/api/experiments");

  // 4. The server cannot be reached at all.
  await page.route("**/api/experiments", (route) => (route.request().method() === "POST" ? route.abort("connectionrefused") : route.continue()));
  await page.getByRole("button", { name: "Run experiment on Revision 1" }).click();
  await expect(instruments).toContainText("could not be reached");
  await expect(levels).toHaveText(before, { useInnerText: true });
  await page.unroute("**/api/experiments");
  await shot(page, testInfo, "honesty-unreachable");
  expect(copyProblems(await learnerText(page))).toEqual([]);
  // The polite region carried the failure messages; nothing assertive for recoverable failures.
});

test("D-12 a cancelled explore experiment does not reappear later as a completed experiment", async ({ page }, testInfo) => {
  await openWorkbench(page);
  const instruments = page.getByRole("region", { name: "Instruments" });
  await page.getByRole("button", { name: "Run experiment on Revision 1" }).click();
  await instruments.getByRole("button", { name: "Cancel" }).click();
  await expect(instruments).toContainText("The experiment was cancelled. Previous results are unchanged.");
  // Give the server time to finish what it was doing, then come back to the workbench.
  await expect
    .poll(async () => ((await (await page.request.get("/api/workbench")).json()).experiments as unknown[]).length, { timeout: 30_000 })
    .toBeGreaterThanOrEqual(1);
  await page.waitForTimeout(8_000);
  const statuses = ((await (await page.request.get("/api/workbench")).json()).experiments as { status: string }[]).map((e) => e.status);
  testInfo.annotations.push({ type: "server-statuses-after-cancel", description: JSON.stringify(statuses) });
  await page.reload();
  const history = page.getByRole("region", { name: "Experiment history" });
  await expect(history.getByText("Experiment 1", { exact: false }).first()).toBeVisible();
  await shot(page, testInfo, "honesty-cancel-after-reload");
  const second = history.locator("li.history__item", { hasText: "Experiment 2" });
  if (await second.count()) await expect(second, "a cancelled run is labelled as cancelled").toContainText("Cancelled", { timeout: 3_000 });
  expect(statuses.filter((st) => st === "succeeded"), "only the first experiment completed").toHaveLength(1);
});

test("D-05 an unexpected gateway response is explained without an HTTP status code", async ({ page }, testInfo) => {
  await openWorkbench(page);
  await page.route("**/api/experiments", (route) =>
    route.request().method() === "POST" ? route.fulfill({ status: 502, contentType: "text/html", body: "<html><body>Bad Gateway</body></html>" }) : route.continue(),
  );
  await page.getByRole("button", { name: "Run experiment on Revision 1" }).click();
  const instruments = page.getByRole("region", { name: "Instruments" });
  await expect(instruments.locator(".callout--danger")).toBeVisible();
  const text = await instruments.locator(".callout--danger").innerText();
  testInfo.annotations.push({ type: "gateway-message", description: text });
  await shot(page, testInfo, "honesty-gateway-502");
  await expect(page.getByRole("list", { name: "Measured tone levels" })).toBeVisible();
  expect(copyProblems(text)).toEqual([]);
});

// ---------------------------------------------------------------------------------------------
// Investigation states
// ---------------------------------------------------------------------------------------------

test("ambiguity: voltage-only evidence leads to an honest abstention, and the reveal reports it", async ({ page }, testInfo) => {
  test.setTimeout(400_000);
  await openWorkbench(page);
  // A faulty case: its symptom leaves an R-vs-C pair (identical voltage transfer for R and C drift).
  let caseId = await startMystery(page);
  let atSymptom = (await getCase(page, caseId)).evidence.remaining as string[];
  for (let i = 0; i < 6 && atSymptom.length < 2; i++) {
    await closeAndReveal(page);
    await page.getByRole("button", { name: "New mystery" }).click();
    await expect(page.getByRole("heading", { name: "What we observed" })).toBeVisible({ timeout: 90_000 });
    caseId = await caseIdOf(page);
    atSymptom = (await getCase(page, caseId)).evidence.remaining as string[];
  }
  expect(atSymptom.length, "a case whose symptom is ambiguous").toBe(2);
  await chooseGuideMe(page);
  // More output-voltage data cannot separate R from C: the pair must stay unresolved.
  await runCaseInstrument(page, "Output response sweep");
  const c = await getCase(page, caseId);
  expect([...c.evidence.remaining].sort()).toEqual([...atSymptom].sort());
  const evidence = page.getByRole("region", { name: "Which explanations still fit" });
  // Both explanations are shown as still possible; no likelihood percentages anywhere.
  for (const h of atSymptom) {
    const label = (c.hypotheses as { id: string; label: string }[]).find((x) => x.id === h)!.label;
    await expect(evidence.getByRole("row").filter({ hasText: label })).toContainText(/Still possible|Supported/);
  }
  expect(await evidence.innerText()).not.toMatch(/\d\s?%/);

  const dx = page.getByRole("form", { name: "Submit a diagnosis" });
  await dx.getByRole("radio", { name: /^Not enough evidence/ }).check();
  for (const h of c.hypotheses as { id: string; label: string }[]) {
    const box = dx.getByRole("checkbox", { name: h.label, exact: true });
    if (c.evidence.remaining.includes(h.id)) await expect(box).toBeChecked();
    else await expect(box).not.toBeChecked();
  }
  await dx.getByLabel("Which measurement would help? (optional)").selectOption("source_current_sweep");
  await dx.getByRole("checkbox", { name: "Obs 1 · Output voltage sweep" }).check();
  await dx.getByRole("button", { name: "Submit diagnosis" }).click();
  const summary = page.getByRole("region", { name: "Diagnosis" });
  await expect(summary).toContainText("Not enough evidence to decide");
  await expect(summary).toContainText("Still possible:");
  await expect(summary).toContainText("Suggested next measurement: Source current sweep");
  await expect(summary).toContainText("Consistent: the evidence is ambiguous");
  // Once the diagnosis is in, the panel names the measurement that would separate them.
  await expect(evidence.getByText("What would tell them apart")).toBeVisible();
  await expect(evidence).toContainText("Source current sweep");
  await shot(page, testInfo, "honesty-ambiguous-abstention");

  await proposeAndVerify(page, atSymptom[0]!.startsWith("r1") ? "R1" : "C1");
  await closeAndReveal(page);
  const kv = page.getByRole("region", { name: "Revealed" }).locator("dl.kv");
  await expect(kv.locator("dt:has-text('Exact identification') + dd")).toHaveText("Abstained (not enough evidence)");
  await expect(kv.locator("dt:has-text('True cause among your candidates') + dd")).toHaveText("Yes");
  await expect(kv.locator("dt:has-text('Unsupported certainty') + dd")).toHaveText("No");
  await expect(kv.locator("dt:has-text('Functional recovery') + dd")).toHaveText(/^(Restored|Not restored)$/);
});

test("D-07 a repair that did not restore the behaviour is not labelled 'Repair verified'", async ({ page }, testInfo) => {
  test.setTimeout(400_000);
  await openWorkbench(page);
  let wrong: "R1" | "C1" | null = null;
  for (let attempt = 0; attempt < 6 && !wrong; attempt++) {
    if (attempt === 0) await startMystery(page);
    else {
      await closeAndReveal(page);
      await page.getByRole("button", { name: "New mystery" }).click();
      await expect(page.getByRole("heading", { name: "What we observed" })).toBeVisible({ timeout: 90_000 });
    }
    await chooseGuideMe(page);
    await runCaseInstrument(page, "Source current sweep");
    const plan = plannedDiagnosis(await getCase(page, await caseIdOf(page)));
    if (plan.conclusion !== "fault") continue; // no fault: every single-part repair restores
    await submitDiagnosis(page, plan, "Obs 1 · Source current sweep");
    wrong = plan.repair === "R1" ? "C1" : "R1";
  }
  expect(wrong, "found a faulty case within six attempts").not.toBeNull();
  const badge = await proposeAndVerify(page, wrong!);
  expect(badge).toBe("Not restored");
  const status = page.getByRole("region", { name: "Case status" }).locator(".badge");
  testInfo.annotations.push({ type: "case-status-after-failed-repair", description: await status.innerText() });
  await shot(page, testInfo, "honesty-failed-repair");
  await expect(status).not.toHaveText(/Repair verified/, { timeout: 3_000 });
});

test("D-08 'Not enough evidence' pre-ticks only the explanations that still fit", async ({ page }, testInfo) => {
  await openWorkbench(page);
  // Needs a case whose symptom leaves more than one explanation (any faulty case); a no-fault
  // symptom already narrows to one, so start another mystery in that case (no budget used).
  let caseId = await startMystery(page);
  let atSymptom = (await getCase(page, caseId)).evidence.remaining as string[];
  for (let i = 0; i < 6 && atSymptom.length < 2; i++) {
    await closeAndReveal(page);
    await page.getByRole("button", { name: "New mystery" }).click();
    await expect(page.getByRole("heading", { name: "What we observed" })).toBeVisible({ timeout: 90_000 });
    caseId = await caseIdOf(page);
    atSymptom = (await getCase(page, caseId)).evidence.remaining as string[];
  }
  expect(atSymptom.length, "a case whose symptom is ambiguous").toBeGreaterThan(1);
  await chooseGuideMe(page);
  const observationsBefore = ((await getCase(page, caseId)).observations as unknown[]).length;
  await runCaseInstrument(page, "Output response sweep");
  await runCaseInstrument(page, "Source current sweep");
  // Compare against the settled server state: both experiments recorded (under heavy load the
  // UI can show the new budget a moment before a separate GET sees the second observation).
  await expect.poll(async () => ((await getCase(page, caseId)).observations as unknown[]).length, { timeout: 30_000 }).toBe(observationsBefore + 2);
  const c = await getCase(page, caseId);
  const remaining = c.evidence.remaining as string[];
  const dx = page.getByRole("form", { name: "Submit a diagnosis" });
  await dx.getByRole("radio", { name: /^Not enough evidence/ }).check();
  const tickedNow = async () => {
    const ticked: string[] = [];
    for (const h of c.hypotheses as { id: string; label: string }[]) {
      if (await dx.getByRole("checkbox", { name: h.label, exact: true }).isChecked()) ticked.push(h.id);
    }
    return ticked.sort();
  };
  await expect.poll(tickedNow, { timeout: 10_000 }).toEqual([...remaining].sort());
  testInfo.annotations.push({ type: "candidates", description: JSON.stringify({ atSymptom, remainingNow: remaining, ticked: await tickedNow() }) });
  await shot(page, testInfo, "honesty-stale-candidates");
});

test("D-09 the playback strip does not claim no experiment was run when a sweep is shown", async ({ page }, testInfo) => {
  await openWorkbench(page);
  await startMystery(page);
  await chooseGuideMe(page);
  await runCaseInstrument(page, "Source current sweep");
  const status = strip(page).locator(".playback__status-text");
  testInfo.annotations.push({ type: "strip-status", description: await status.innerText() });
  await expect(status).not.toHaveText(/Run an experiment to render audio/, { timeout: 3_000 });
});

// ---------------------------------------------------------------------------------------------
// First visit (dev server): one session, consistent state
// ---------------------------------------------------------------------------------------------

test("D-06 a first visit creates exactly one session", async ({ page }, testInfo) => {
  const cookies: string[] = [];
  page.on("response", async (res) => {
    const header = (await res.headersArray()).filter((h) => h.name.toLowerCase() === "set-cookie").map((h) => h.value);
    for (const v of header) {
      const m = /rs_session=([^;]+)/.exec(v);
      if (m) cookies.push(m[1]!);
    }
  });
  await page.goto("/lab");
  await expect(page.getByRole("button", { name: /^Run experiment on Revision \d+$/ })).toBeEnabled();
  await page.waitForLoadState("networkidle");
  const wb = await (await page.request.get("/api/workbench")).json();
  testInfo.annotations.push({
    type: "first-visit",
    description: JSON.stringify({ target: TARGET, distinctSessionCookies: new Set(cookies).size, exploreRevisionsInFinalSession: wb.revisions.filter((r: { origin: string }) => r.origin !== "case_intended").length }),
  });
  expect(new Set(cookies).size, "distinct rs_session cookies issued during the first load").toBe(1);
});

test("D-06 after a first visit, a reload shows the revision that was measured", async ({ page }, testInfo) => {
  // Natural timing, no priming: this is what a first-time visitor of the dev server gets.
  await page.goto("/lab");
  const run = page.getByRole("button", { name: /^Run experiment on Revision \d+$/ });
  await expect(run).toBeEnabled();
  await page.waitForLoadState("networkidle");
  const before = (await page.locator(".header__rev").innerText()).trim();
  // The first visit measures the opening experiment by itself (no click needed).
  const instruments = page.getByRole("region", { name: "Instruments" });
  const meta = instruments.locator(".instruments__meta");
  const failure = instruments.locator(".callout--danger");
  await expect(meta.or(failure)).toBeVisible({ timeout: 90_000 });
  const firstRun = (await failure.isVisible()) ? `failed: ${await failure.innerText()}` : (await meta.innerText()).trim();
  await page.reload();
  await expect(page.getByRole("button", { name: /^Run experiment on Revision \d+$/ })).toBeEnabled();
  const after = (await page.locator(".header__rev").innerText()).trim();
  const wb = await (await page.request.get("/api/workbench")).json();
  testInfo.annotations.push({
    type: "first-visit-reload",
    description: JSON.stringify({ target: TARGET, before, firstRun, after, exploreRevisions: wb.revisions.filter((r: { origin: string }) => r.origin !== "case_intended").length }),
  });
  expect(firstRun, "the opening experiment of a first visit succeeds").toMatch(/^Experiment 1 · Revision 1/);
  expect(after, "a reload shows the same revision").toBe(before);
  expect(wb.revisions.filter((r: { origin: string }) => r.origin !== "case_intended")).toHaveLength(1);
});

// ---------------------------------------------------------------------------------------------
// ARIA naming
// ---------------------------------------------------------------------------------------------

test("D-04 component rows: the accessible name contains the visible label (WCAG 2.5.3)", async ({ page }, testInfo) => {
  await openWorkbench(page);
  const findings = (await axeScan(page, "D-04-label-in-name")).filter((f) => f.rule === "label-content-name-mismatch");
  testInfo.annotations.push({ type: "label-in-name", description: JSON.stringify(findings.map((f) => f.html)) });
  expect(findings).toEqual([]);
});

test("D-03 chart data-table scroll containers are keyboard reachable", async ({ page }) => {
  await openWorkbench(page);
  await page.getByRole("figure", { name: "Frequency response" }).locator("summary", { hasText: "Data table" }).click();
  const findings = (await axeScan(page, "D-03-scrollable-region")).filter((f) => f.rule === "scrollable-region-focusable");
  expect(findings).toEqual([]);
});

test("D-11 named groups use a role that permits a name (no aria-label on generic elements)", async ({ page }) => {
  await openWorkbench(page);
  await editValue(page, "C1", "220n", 2);
  await runExploreExperiment(page, 2);
  const history = page.getByRole("region", { name: "Experiment history" });
  await history.getByLabel("Compare Experiment 1").check();
  await history.getByLabel("Compare Experiment 2").check();
  await history.getByRole("button", { name: "Compare" }).click();
  await expect(page.locator(".comparison")).toBeVisible();
  const generic = await page.evaluate(() =>
    [...document.querySelectorAll("div[aria-label], span[aria-label], p[aria-label]")]
      .filter((el) => !el.getAttribute("role"))
      .map((el) => `${el.tagName.toLowerCase()}.${el.className}: ${el.getAttribute("aria-label")}`),
  );
  expect(generic).toEqual([]);
});

// D-14 (found by the lead in the final run): the Finish card kept its local "confirming" state
// across cases, so after "New mystery" the fresh case showed an armed one-click "Close & reveal".
test("D-14 a new mystery never starts with an armed 'Close & reveal' confirmation", async ({ page }) => {
  test.setTimeout(300_000);
  await openWorkbench(page);
  await startMystery(page);
  await closeAndReveal(page);
  await page.getByRole("button", { name: "New mystery" }).click();
  await expect(page.getByRole("heading", { name: "What we observed" })).toBeVisible({ timeout: 90_000 });
  const finish = page.getByRole("region", { name: "Finish" });
  await expect(finish.getByRole("button", { name: "Close & reveal…" })).toBeVisible();
  await expect(finish.getByRole("button", { name: "Keep investigating" })).toHaveCount(0);
  await expect(finish.getByRole("button", { name: "Close & reveal", exact: true })).toHaveCount(0);
});
