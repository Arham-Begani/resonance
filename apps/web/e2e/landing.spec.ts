// The landing page (/) is an entry point, not a replacement for the workbench (CLAUDE.md):
// it must not start a session or a simulation, it must pass the same WCAG 2 A/AA scan as the
// workbench, and one click must open a working lab at /lab.
import { expect, test } from "@playwright/test";
import { axeScan, copyProblems, horizontalOverflow, learnerText, shot, waitForExperiment } from "./support/workbench";

test("landing: no API traffic, accessible, no horizontal scroll, and Open the lab reaches a measured workbench", async ({ page }, testInfo) => {
  const apiCalls: string[] = [];
  page.on("request", (req) => {
    if (new URL(req.url()).pathname.startsWith("/api/")) apiCalls.push(req.url());
  });
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Hear what a circuit does.");
  await page.waitForLoadState("networkidle");
  expect(apiCalls, "the landing page never calls the API (no session, no simulation)").toEqual([]);

  const findings = await axeScan(page, "landing-desktop");
  expect(findings.filter((f) => f.asserted).map((f) => `${f.rule}: ${f.targets.slice(0, 3).join(" | ")}`)).toEqual([]);
  expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  expect(copyProblems(await learnerText(page))).toEqual([]);
  await shot(page, testInfo, "landing-desktop", true);

  // The skip link is the first stop and moves focus to the main content.
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: /^Skip to/ })).toBeFocused();

  // Every lab entry point targets the lab.
  const hrefs = await page.getByRole("link", { name: /Open the lab|Solve a mystery/ }).evaluateAll((els) => els.map((el) => el.getAttribute("href")));
  expect(hrefs.length).toBeGreaterThanOrEqual(2);
  for (const href of hrefs) expect(href).toMatch(/^\/lab(#investigate)?$/);

  await page.getByRole("link", { name: /Open the lab/ }).first().click();
  await expect(page).toHaveURL(/\/lab$/);
  await waitForExperiment(page, 1);
  await expect(page.getByRole("list", { name: "Measured tone levels" })).toContainText("440 Hz");
});

test("landing: narrow screen keeps everything reachable without horizontal scroll", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 360, height: 780 });
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  expect(await horizontalOverflow(page)).toBeLessThanOrEqual(0);
  const findings = await axeScan(page, "landing-narrow");
  expect(findings.filter((f) => f.asserted).map((f) => `${f.rule}: ${f.targets.slice(0, 3).join(" | ")}`)).toEqual([]);
  await shot(page, testInfo, "landing-narrow", true);
  await page.getByRole("link", { name: /Solve a mystery/ }).first().click();
  await expect(page).toHaveURL(/\/lab#investigate$/);
  await expect(page.getByRole("navigation", { name: "Workbench mode" }).or(page.getByRole("tablist", { name: "Workbench sections" })).first()).toBeVisible();
});
