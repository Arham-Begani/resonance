// CLAUDE.md "Meaningful verification → UI": one complete browser flow covering
// edit → simulate → compare → challenge → investigate → repair, against the real API + ngspice.
// The hidden fault is random; the test chooses its diagnosis from the PUBLIC evidence only and
// checks, after the reveal, that identification and functional recovery are reported separately.
import { expect, test } from "@playwright/test";
import {
  caseIdOf,
  copyProblems,
  editValue,
  getCase,
  instrument,
  learnerText,
  openWorkbench,
  plannedDiagnosis,
  runExploreExperiment,
  shot,
} from "./support/workbench";

test("complete flow: edit, simulate, compare, mystery, Guide me, cited diagnosis, repair, reveal", async ({ page }, testInfo) => {
  await instrument(page);
  // Every case payload the browser receives for requests issued before close was requested
  // (to prove nothing case-specific leaked early). The close response itself is excluded.
  const caseBodies: string[] = [];
  let closing = false;
  const preClose = new WeakSet<object>();
  page.on("request", (req) => {
    if (!closing && req.url().includes("/api/")) preClose.add(req);
  });
  page.on("response", async (res) => {
    if (!preClose.has(res.request())) return;
    try {
      caseBodies.push(await res.text());
    } catch {
      // Redirects/aborted bodies carry nothing to inspect.
    }
  });

  // ---------- Explore: measure the original, edit C1, measure again ----------
  await openWorkbench(page);
  await runExploreExperiment(page, 1);
  await expect(page.getByRole("list", { name: "Measured tone levels" })).toContainText("440 Hz");
  await expect(page.getByRole("region", { name: "Goal" })).toContainText(/Goal (met|not met)/);

  await editValue(page, "C1", "220n", 2);
  await expect(page.getByRole("figure", { name: /Revision 2 · changed C1 100 nF → 220 nF/ })).toBeVisible();
  // The previous measurement stays visible and is labelled as belonging to Revision 1.
  await expect(page.getByText("Showing Experiment 1 from Revision 1")).toBeVisible();
  await runExploreExperiment(page, 2);
  await shot(page, testInfo, "flow-01-explore-measured");

  // ---------- Compare the two experiments ----------
  const history = page.getByRole("region", { name: "Experiment history" });
  await history.getByLabel("Compare Experiment 1").check();
  await history.getByLabel("Compare Experiment 2").check();
  await history.getByRole("button", { name: "Compare" }).click();
  const comparison = page.getByLabel("Comparison result");
  await expect(comparison).toContainText("Experiment 1 → Experiment 2");
  await expect(comparison).toContainText("C1: 100 nF → 220 nF");
  const rows = comparison.getByRole("table").getByRole("row");
  await expect(rows).toHaveCount(3); // header + two tones
  // Doubling-ish C lowers a low-pass cutoff: both tone gains must drop (negative change).
  for (const tone of ["440 Hz", "4 kHz"]) {
    const change = await comparison.getByRole("row", { name: new RegExp(tone) }).getByRole("cell").nth(2).innerText();
    expect(change.trim(), `gain change at ${tone}`).toMatch(/^[−-]\d/);
  }
  await expect(comparison.getByRole("button", { name: /^Play / })).toHaveCount(2);
  await shot(page, testInfo, "flow-02-compare");

  // Undo returns to the parent revision without losing either measurement.
  await history.getByRole("button", { name: "Undo to Revision 1" }).click();
  await expect(page.locator(".header__rev")).toHaveText("Revision 1");
  await expect(history.getByText("Experiment 2", { exact: false }).first()).toBeVisible();

  // ---------- Investigate: start a mystery, Guide me, take real measurements ----------
  await page.getByRole("navigation", { name: "Workbench mode" }).getByRole("button", { name: "Investigate" }).click();
  await page.getByRole("button", { name: "Start a mystery" }).click();
  await expect(page.getByRole("heading", { name: "What we observed" })).toBeVisible({ timeout: 90_000 });
  const caseId = await caseIdOf(page);
  await expect(page.getByText("3 of 3 experiments left")).toHaveCount(0); // budget appears after an approach is chosen
  await shot(page, testInfo, "flow-03-symptom");

  await page.getByRole("button", { name: /^Guide me/ }).click();
  const measure = page.getByRole("region", { name: "Take a measurement" });
  await expect(measure).toBeVisible();
  await expect(measure.getByRole("group", { name: "3 of 3 experiments left" })).toBeVisible();

  const runInstrument = async (title: string, left: number) => {
    const card = measure.locator("li.instrument", { has: page.getByRole("heading", { name: title }) });
    await card.getByRole("button", { name: /^Run/ }).click();
    await expect(measure.getByRole("group", { name: `${left} of 3 experiments left` })).toBeVisible({ timeout: 90_000 });
  };
  await runInstrument("Output response sweep", 2);
  await runInstrument("Source current sweep", 1);
  await expect(page.getByRole("region", { name: "Recorded measurements" }).getByText("Obs 2")).toBeVisible();
  await shot(page, testInfo, "flow-04-observations");

  // The evidence table is rendered from the recorded observations (public data only).
  const before = await getCase(page, caseId);
  expect(before.reveal).toBeNull();
  expect(before.budget.used).toBe(2);
  const plan = plannedDiagnosis(before);
  const evidence = page.getByRole("region", { name: "Which explanations still fit" });
  for (const a of before.evidence.assessments as { hypothesis_id: string; status: string }[]) {
    const label = before.hypotheses.find((h: { id: string }) => h.id === a.hypothesis_id).label as string;
    const text = { supported: "Supported", contradicted: "Ruled out", unresolved: "Still possible" }[a.status]!;
    await expect(evidence.getByRole("row", { name: new RegExp(`^${label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`) })).toContainText(text);
  }

  // ---------- Submit a cited diagnosis ----------
  const dx = page.getByRole("form", { name: "Submit a diagnosis" });
  const conclusionLabel = { fault: "A component fault", no_fault: "No fault", insufficient_evidence: "Not enough evidence" }[plan.conclusion];
  await dx.getByRole("radio", { name: new RegExp(`^${conclusionLabel}`) }).check();
  if (plan.conclusion === "fault") await dx.getByLabel("Which fault?").selectOption(plan.hypothesisId!);
  await dx.getByRole("checkbox", { name: "Obs 2 · Source current sweep" }).check();
  await dx.getByLabel("Explanation (optional)").fill("Source current sweep compared with each explanation's prediction.");
  await dx.getByRole("button", { name: "Submit diagnosis" }).click();
  const dxCard = page.getByRole("region", { name: "Diagnosis" });
  await expect(dxCard).toContainText(/Your diagnosis:/);
  await expect(dxCard).toContainText("Obs 2 · source current");
  await expect(dxCard).toContainText("whether the diagnosis is actually correct is only known at reveal");
  await shot(page, testInfo, "flow-05-diagnosis");

  // ---------- Propose and verify a one-component repair ----------
  const repair = page.getByRole("region", { name: "Repair" });
  await repair.getByRole("radio", { name: new RegExp(`^Replace ${plan.repair} with`) }).check();
  await repair.getByRole("button", { name: "Propose repair" }).click();
  await expect(repair).toContainText(`You proposed: replace ${plan.repair}`);
  await repair.getByRole("button", { name: "Apply repair and verify" }).click();
  await expect(repair.getByText(/Behaviour restored|Not restored/).first()).toBeVisible({ timeout: 90_000 });
  await expect(repair).toContainText("Largest gain difference");
  await expect(page.getByRole("figure", { name: "Repair verification" })).toBeVisible();
  const afterRepair = await getCase(page, caseId);
  expect(afterRepair.reveal).toBeNull();
  await shot(page, testInfo, "flow-06-repair");

  // Nothing case-specific (seed, hidden labels as truth) reached the browser before close.
  const beforeText = await learnerText(page);
  expect(copyProblems(beforeText), "learner text before reveal").toEqual([]);

  // ---------- Close and reveal ----------
  await page.getByRole("button", { name: "Close & reveal…" }).click();
  await expect(page.getByText(/reveals the actual fault and seed/)).toBeVisible();
  const closeResponse = page.waitForResponse((r) => r.url().endsWith("/close") && r.request().method() === "POST");
  closing = true;
  await page.getByRole("button", { name: "Close & reveal", exact: true }).click();
  await closeResponse;
  const reveal = page.getByRole("region", { name: "Revealed" });
  await expect(reveal).toBeVisible();
  await expect(page.getByRole("heading", { name: "Revealed" })).toBeFocused();
  const final = await getCase(page, caseId);
  expect(final.status).toBe("closed");
  expect(final.reveal).not.toBeNull();

  // Two separate outcomes, each rendered from the reveal payload.
  const outcome = final.reveal.outcome;
  const identText = { correct: "Correct", incorrect: "Incorrect", abstained: "Abstained (not enough evidence)", not_submitted: "No diagnosis submitted" }[
    outcome.exact_identification as string
  ]!;
  const recoveryText = { restored: "Restored", not_restored: "Not restored", not_attempted: "Not attempted", not_needed: "Not needed (no fault)" }[
    outcome.functional_recovery as string
  ]!;
  const kv = reveal.locator("dl.kv");
  await expect(kv.locator("dt", { hasText: "Exact identification" })).toBeVisible();
  await expect(kv.locator("dt", { hasText: "Functional recovery" })).toBeVisible();
  await expect(kv.locator("dt:has-text('Exact identification') + dd")).toHaveText(identText);
  await expect(kv.locator("dt:has-text('Functional recovery') + dd")).toHaveText(recoveryText);
  await expect(reveal).toContainText(final.reveal.fault.description);
  await expect(reveal).toContainText(final.reveal.seed);

  // The evidence pointed to exactly one explanation (current sweep + symptom), so following it
  // must identify the fault and the matching repair must restore the response.
  if (plan.conclusion !== "insufficient_evidence") {
    expect(final.reveal.fault.hypothesis_id).toBe(plan.hypothesisId ?? "no_fault");
    expect(outcome.exact_identification).toBe("correct");
    expect(outcome.functional_recovery).toBe(plan.conclusion === "no_fault" ? "not_needed" : "restored");
  }
  await shot(page, testInfo, "flow-07-reveal");

  // The seed and the reveal text (published only now) never appeared in any earlier API response.
  expect(caseBodies.length).toBeGreaterThan(5);
  for (const body of caseBodies) {
    expect(body.includes(final.reveal.seed), "seed absent before close").toBe(false);
    expect(body.includes(final.reveal.fault.description), "fault description absent before close").toBe(false);
  }
  // After close, experiments are refused by the UI (no instrument card) and by the API.
  await expect(page.getByRole("region", { name: "Take a measurement" })).toHaveCount(0);
  const refused = await page.request.post(`/api/cases/${caseId}/experiments`, {
    data: { instrument: "output_voltage_sweep", probe: null, client_request_id: "e2e_after_close_1" },
  });
  expect(refused.status()).toBe(409);
});
