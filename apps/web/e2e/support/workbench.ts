// Shared helpers for the verification e2e suite (owner: independent verification engineer).
// Everything here drives the real UI against the real API + ngspice started by
// playwright.config.ts. Nothing is mocked unless a test says so explicitly.
import AxeBuilder from "@axe-core/playwright";
import { expect, type Locator, type Page, type TestInfo } from "@playwright/test";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

export const SHOT_DIR = "test-results/verification";

/** Learner-facing text must never contain raw opaque IDs (contracts.md "Conventions"). */
export const RAW_ID = /\b(?:rev|exp|obs|case|art|cmp|goal|dx|rep|prop|run)_[A-Za-z0-9_-]{8,}/;
/** Stack traces, Python/ngspice internals, or HTTP status codes in ordinary text. */
export const INTERNALS = [
  /Traceback \(most recent call last\)/,
  /File "[^"]+\.py"/,
  /\bat [A-Za-z0-9_$.]+ \([^)]*:\d+:\d+\)/,
  /\bHTTP\s?[1-5]\d\d\b/,
  /\b(?:status|code)\s*[:=]?\s*[45]\d\d\b/i,
  /\((?:4|5)\d\d\)/,
  /\bInternal Server Error\b/,
  /\bundefined\b|\bNaN\b|\[object Object\]/,
  /\.data[\\/]|[A-Z]:\\[A-Za-z]/,
  /\b(?:\.tran|\.control|\.endc)\b/,
];

export interface LiveRecord {
  t: number;
  kind: "polite" | "assertive";
  text: string;
}

export interface AudioLog {
  contexts: { t: number; sinceGesture: number | null }[];
  starts: { t: number; sinceGesture: number | null }[];
  mediaPlays: { t: number; sinceGesture: number | null }[];
  active: number;
  maxActive: number;
  lastGesture: number | null;
}

/**
 * Instrument the page before any app script runs:
 * - record every text written into the two announcer live regions;
 * - record AudioContext creation, AudioBufferSourceNode.start/stop and HTMLMediaElement.play,
 *   with the time since the last trusted pointer/keyboard gesture, and the number of sources
 *   playing at once.
 */
export async function instrument(page: Page, options: { disableWebAudio?: boolean } = {}): Promise<void> {
  await page.addInitScript(
    ({ disableWebAudio }) => {
      const w = window as unknown as Record<string, unknown>;
      const log = { contexts: [], starts: [], mediaPlays: [], active: 0, maxActive: 0, lastGesture: null } as {
        contexts: { t: number; sinceGesture: number | null }[];
        starts: { t: number; sinceGesture: number | null }[];
        mediaPlays: { t: number; sinceGesture: number | null }[];
        active: number;
        maxActive: number;
        lastGesture: number | null;
      };
      w.__audio = log;
      const since = () => (log.lastGesture === null ? null : performance.now() - log.lastGesture);
      for (const type of ["pointerdown", "keydown"]) {
        window.addEventListener(
          type,
          (e) => {
            if (e.isTrusted) log.lastGesture = performance.now();
          },
          true,
        );
      }
      if (disableWebAudio) {
        delete w.AudioContext;
        delete w.webkitAudioContext;
      } else if (typeof AudioContext !== "undefined") {
        const Orig = AudioContext;
        class Recorded extends Orig {
          constructor(...args: ConstructorParameters<typeof AudioContext>) {
            super(...args);
            log.contexts.push({ t: performance.now(), sinceGesture: since() });
          }
        }
        w.AudioContext = Recorded;
        const origStart = AudioBufferSourceNode.prototype.start;
        AudioBufferSourceNode.prototype.start = function (this: AudioBufferSourceNode, ...args: Parameters<AudioBufferSourceNode["start"]>) {
          let done = false;
          const end = () => {
            if (done) return;
            done = true;
            log.active -= 1;
          };
          log.active += 1;
          log.maxActive = Math.max(log.maxActive, log.active);
          log.starts.push({ t: performance.now(), sinceGesture: since() });
          this.addEventListener("ended", end);
          const origStop = this.stop.bind(this);
          this.stop = (...a: Parameters<AudioBufferSourceNode["stop"]>) => {
            end();
            return origStop(...a);
          };
          return origStart.apply(this, args);
        };
      }
      const origPlay = HTMLMediaElement.prototype.play;
      HTMLMediaElement.prototype.play = function (this: HTMLMediaElement) {
        log.mediaPlays.push({ t: performance.now(), sinceGesture: since() });
        return origPlay.call(this);
      };

      const live: { t: number; kind: string; text: string }[] = [];
      w.__live = live;
      const watch = () => {
        for (const [kind, sel] of [
          ["polite", '[data-testid="live-polite"]'],
          ["assertive", '[data-testid="live-assertive"]'],
        ] as const) {
          const el = document.querySelector(sel);
          if (!el || (el as HTMLElement).dataset.watched) continue;
          (el as HTMLElement).dataset.watched = "1";
          new MutationObserver(() => {
            const text = (el.textContent ?? "").trim();
            if (text) live.push({ t: performance.now(), kind, text });
          }).observe(el, { childList: true, characterData: true, subtree: true });
        }
      };
      new MutationObserver(watch).observe(document, { childList: true, subtree: true });
    },
    { disableWebAudio: options.disableWebAudio ?? false },
  );
}

export async function audioLog(page: Page): Promise<AudioLog> {
  return page.evaluate(() => (window as unknown as { __audio: AudioLog }).__audio);
}

export async function liveLog(page: Page): Promise<LiveRecord[]> {
  return page.evaluate(() => (window as unknown as { __live: LiveRecord[] }).__live ?? []);
}

/** "dev" (Vite dev server, React StrictMode) or "prod" (production build served by the API). */
export const TARGET: "dev" | "prod" = process.env.RESONANCE_E2E_TARGET === "prod" ? "prod" : "dev";

/**
 * Establish the session cookie before the app loads. Used while D-06 (first-visit
 * initialisation split into two sessions on the dev server) was open; D-06 is fixed, so
 * openWorkbench no longer primes and every test exercises the real first visit.
 */
export async function primeSession(page: Page): Promise<void> {
  const res = await page.request.get("/api/health");
  expect(res.ok()).toBeTruthy();
}

/** Open the workbench; on the plain URL, wait until the opening experiment (Experiment 1) is measured. */
export async function openWorkbench(page: Page, hash = ""): Promise<void> {
  await page.goto(`/lab${hash}`);
  if (!hash) {
    // A first visit measures the opening experiment automatically: it is Experiment 1.
    await waitForExperiment(page, 1);
    await expect(page.getByRole("button", { name: /^Run experiment on Revision \d+$/ })).toBeEnabled();
  } else {
    await expect(page.getByRole("heading", { name: "Resonance" }).or(page.getByRole("heading", { level: 1 }))).toBeVisible();
  }
}

export function runButton(page: Page): Locator {
  return page.getByRole("button", { name: /^(Run experiment on Revision \d+|Simulating…)$/ });
}

/** Click "Run experiment" (or press Enter if focused) and wait for experiment `n` to be shown. */
export async function waitForExperiment(page: Page, n: number): Promise<void> {
  await expect(page.locator(".instruments__meta").first()).toContainText(`Experiment ${n} ·`, { timeout: 90_000 });
  await expect(runButton(page)).toHaveText(/Run experiment on Revision \d+/, { timeout: 90_000 });
}

export async function runExploreExperiment(page: Page, n: number): Promise<void> {
  await runButton(page).click();
  await waitForExperiment(page, n);
}

/** Edit an editable component through its inline editor (mouse path). */
export async function editValue(page: Page, component: "R1" | "C1", text: string, expectRevision: number): Promise<void> {
  await page.getByRole("button", { name: `Edit ${component} value` }).click();
  const input = page.getByLabel(new RegExp(`value for ${component}$`));
  await expect(input).toBeFocused();
  await input.fill(text);
  await expect(page.locator(".value-editor .field__ok")).toBeVisible();
  await page.locator(".value-editor").getByRole("button", { name: "Apply" }).click();
  await expect(page.locator(".header__rev")).toHaveText(`Revision ${expectRevision}`);
}

export async function caseIdOf(page: Page): Promise<string> {
  const wb = await (await page.request.get("/api/workbench")).json();
  expect(wb.active_case_id, "an open case is recorded for this session").toBeTruthy();
  return wb.active_case_id as string;
}

// The PublicCase payload, read with the browser's own session cookie (public data only).
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type CaseJson = any;

export async function getCase(page: Page, caseId: string): Promise<CaseJson> {
  const res = await page.request.get(`/api/cases/${caseId}`);
  expect(res.ok()).toBeTruthy();
  return res.json();
}

/** Text a learner can perceive: visible text plus accessible names, values, and hints. */
export async function learnerText(page: Page): Promise<string> {
  return page.evaluate(() => {
    const parts = [document.body.innerText];
    document.querySelectorAll("[aria-label],[title],[placeholder],[aria-valuetext],[alt]").forEach((el) => {
      for (const a of ["aria-label", "title", "placeholder", "aria-valuetext", "alt"]) {
        const v = el.getAttribute(a);
        if (v) parts.push(v);
      }
    });
    document.querySelectorAll('[data-testid^="live-"]').forEach((el) => parts.push(el.textContent ?? ""));
    return parts.join("\n");
  });
}

export interface CopyProblem {
  pattern: string;
  excerpt: string;
}

/** Find raw IDs, stack traces, HTTP codes, or internals in learner-facing text. */
export function copyProblems(text: string, allow: RegExp[] = []): CopyProblem[] {
  const problems: CopyProblem[] = [];
  for (const pattern of [RAW_ID, ...INTERNALS]) {
    const global = new RegExp(pattern.source, pattern.flags.includes("g") ? pattern.flags : `${pattern.flags}g`);
    for (const m of text.matchAll(global)) {
      const start = Math.max(0, (m.index ?? 0) - 50);
      const excerpt = text.slice(start, (m.index ?? 0) + m[0].length + 50).replace(/\s+/g, " ");
      if (allow.some((a) => a.test(excerpt))) continue;
      problems.push({ pattern: pattern.source, excerpt });
    }
  }
  return problems;
}

export interface FocusInfo {
  tag: string;
  name: string;
  role: string | null;
  indicator: boolean;
  detail: string;
  inMain: boolean;
  region: string;
  rect: { x: number; y: number; w: number; h: number };
}

/** Describe the focused element and whether a visible focus indicator is rendered for it. */
export async function focusInfo(page: Page): Promise<FocusInfo | null> {
  return page.evaluate(() => {
    const el = document.activeElement as HTMLElement | null;
    if (!el || el === document.body || el === document.documentElement) return null;
    const visible = (node: Element) => {
      const cs = getComputedStyle(node);
      const outline = cs.outlineStyle !== "none" && parseFloat(cs.outlineWidth) > 0;
      const shadow = cs.boxShadow !== "none" && /\d+px/.test(cs.boxShadow);
      return { ok: outline || shadow, detail: `outline ${cs.outlineStyle} ${cs.outlineWidth}; shadow ${cs.boxShadow}` };
    };
    let check = visible(el);
    // Visually hidden native inputs (segmented controls) draw their ring on the wrapping label.
    if (!check.ok && el instanceof HTMLInputElement && parseFloat(getComputedStyle(el).opacity) === 0) {
      const label = el.closest("label");
      if (label) check = visible(label);
    }
    const name =
      el.getAttribute("aria-label") ??
      (el instanceof HTMLInputElement && el.labels?.[0] ? el.labels[0].innerText : null) ??
      el.innerText ??
      "";
    const region =
      el.closest("nav")?.getAttribute("aria-label") ??
      el.closest("header")?.className ??
      el.closest("main")?.getAttribute("aria-label") ??
      el.closest("aside")?.getAttribute("aria-label") ??
      el.closest("section.strip")?.getAttribute("aria-label") ??
      (el.classList.contains("skip-link") ? "skip-link" : "other");
    const r = el.getBoundingClientRect();
    return {
      tag: el.tagName.toLowerCase(),
      name: name.replace(/\s+/g, " ").trim().slice(0, 80),
      role: el.getAttribute("role"),
      indicator: check.ok,
      detail: check.detail,
      inMain: Boolean(el.closest("main")),
      region,
      rect: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
    };
  });
}

/**
 * Press Tab (or Shift+Tab) until `target` is focused, asserting a visible focus indicator on
 * every stop. Returns the focus path. Fails if the target is not reached within `max` presses.
 */
export async function tabTo(page: Page, target: Locator, opts: { max?: number; reverse?: boolean; path?: FocusInfo[] } = {}): Promise<FocusInfo[]> {
  const max = opts.max ?? 150;
  const path = opts.path ?? [];
  for (let i = 0; i < max; i++) {
    if (await target.evaluate((el) => el === document.activeElement).catch(() => false)) return path;
    await page.keyboard.press(opts.reverse ? "Shift+Tab" : "Tab");
    const info = await focusInfo(page);
    if (info) {
      path.push(info);
      expect(info.indicator, `visible focus indicator on ${info.tag} "${info.name}" (${info.detail})`).toBe(true);
    }
  }
  const last = path.slice(-8).map((p) => `${p.tag}:${p.name}`).join(" | ");
  throw new Error(`Could not reach the target with ${max} Tab presses. Last stops: ${last}`);
}

export async function shot(page: Page, testInfo: TestInfo, name: string, fullPage = false): Promise<void> {
  const dir = join(SHOT_DIR, testInfo.project.name);
  mkdirSync(dir, { recursive: true });
  const path = join(dir, `${name}.png`);
  await page.screenshot({ path, fullPage });
}

// ---------------------------------------------------------------------------------------------
// Accessibility scanning
// ---------------------------------------------------------------------------------------------

/** Tags asserted by the suite (the task's contract) and tags recorded for information only. */
export const AXE_ASSERTED = ["wcag2a", "wcag2aa"];
export const AXE_INFO = ["wcag21a", "wcag21aa", "wcag22aa"];

export interface AxeFinding {
  rule: string;
  impact: string | null;
  help: string;
  asserted: boolean;
  targets: string[];
  html: string[];
}

/**
 * Run axe-core on the page as rendered now. Violations tagged wcag2a/wcag2aa are "asserted";
 * WCAG 2.1/2.2-only rules (e.g. target-size) are returned with asserted=false for the report.
 * The full axe output is written to test-results/verification/axe/<label>.json.
 */
export async function axeScan(page: Page, label: string): Promise<AxeFinding[]> {
  const results = await new AxeBuilder({ page }).withTags([...AXE_ASSERTED, ...AXE_INFO]).analyze();
  const dir = join(SHOT_DIR, "axe");
  mkdirSync(dir, { recursive: true });
  writeFileSync(
    join(dir, `${label}.json`),
    JSON.stringify(
      {
        label,
        url: results.url,
        violations: results.violations,
        incomplete: results.incomplete.map((i) => ({ id: i.id, impact: i.impact, help: i.help, nodes: i.nodes.map((n) => n.target.join(" ")) })),
        passes: results.passes.map((p) => p.id),
      },
      null,
      2,
    ),
  );
  return results.violations.map((v) => ({
    rule: v.id,
    impact: v.impact ?? null,
    help: v.help,
    asserted: v.tags.some((t) => AXE_ASSERTED.includes(t)),
    targets: v.nodes.map((n) => n.target.join(" ")),
    html: v.nodes.map((n) => n.html.replace(/\s+/g, " ").slice(0, 200)),
  }));
}

/** Horizontal page overflow in CSS px (0 when the page fits the viewport width). */
export async function horizontalOverflow(page: Page): Promise<number> {
  return page.evaluate(() => Math.max(document.documentElement.scrollWidth, document.body.scrollWidth) - window.innerWidth);
}

// ---------------------------------------------------------------------------------------------
// Investigation steps (mouse path, real API + ngspice)
// ---------------------------------------------------------------------------------------------

export function measureRegion(page: Page): Locator {
  return page.getByRole("region", { name: "Take a measurement" });
}

/** Switch to Investigate and start a fresh mystery; returns the case ID (from the API). */
export async function startMystery(page: Page): Promise<string> {
  await page.getByRole("navigation", { name: "Workbench mode" }).getByRole("button", { name: "Investigate" }).click();
  await page.getByRole("button", { name: "Start a mystery" }).click();
  await expect(page.getByRole("heading", { name: "What we observed" })).toBeVisible({ timeout: 90_000 });
  return caseIdOf(page);
}

export async function chooseGuideMe(page: Page): Promise<void> {
  await page.getByRole("button", { name: /^Guide me/ }).click();
  await expect(measureRegion(page).getByRole("group", { name: "3 of 3 experiments left" })).toBeVisible();
}

/** Run one case instrument (optionally choosing the transient probe) and wait for the budget to drop. */
export async function runCaseInstrument(page: Page, title: string, probe?: "Output voltage" | "Source current"): Promise<void> {
  const measure = measureRegion(page);
  const label = (await measure.getByRole("group", { name: /experiments left$/ }).getAttribute("aria-label")) ?? "";
  const left = Number(/^(\d+) of/.exec(label)?.[1]);
  const card = measure.locator("li.instrument", { has: page.getByRole("heading", { name: title }) });
  if (probe) await card.getByRole("radio", { name: probe }).check();
  await card.getByRole("button", { name: /^Run/ }).click();
  await expect(measure.getByRole("group", { name: `${left - 1} of 3 experiments left` })).toBeVisible({ timeout: 90_000 });
}

export type Plan = ReturnType<typeof plannedDiagnosis>;

/** Fill and submit the diagnosis form following `plan`, citing the given observation checkbox. */
export async function submitDiagnosis(page: Page, plan: Plan, cite: string): Promise<void> {
  const dx = page.getByRole("form", { name: "Submit a diagnosis" });
  const conclusionLabel = { fault: "A component fault", no_fault: "No fault", insufficient_evidence: "Not enough evidence" }[plan.conclusion];
  await dx.getByRole("radio", { name: new RegExp(`^${conclusionLabel}`) }).check();
  if (plan.conclusion === "fault") await dx.getByLabel("Which fault?").selectOption(plan.hypothesisId!);
  await dx.getByRole("checkbox", { name: cite }).check();
  await dx.getByRole("button", { name: "Submit diagnosis" }).click();
  await expect(page.getByRole("region", { name: "Diagnosis" })).toContainText(/(Your|AI) diagnosis:/);
}

/** Propose replacing one component with its nominal part and verify; returns the badge text. */
export async function proposeAndVerify(page: Page, component: "R1" | "C1"): Promise<string> {
  const repair = page.getByRole("region", { name: "Repair" });
  await repair.getByRole("radio", { name: new RegExp(`^Replace ${component} with`) }).check();
  await repair.getByRole("button", { name: "Propose repair" }).click();
  await expect(repair).toContainText(`You proposed: replace ${component}`);
  await repair.getByRole("button", { name: "Apply repair and verify" }).click();
  const badge = repair.getByText(/^(Behaviour restored|Not restored)$/).first();
  await expect(badge).toBeVisible({ timeout: 90_000 });
  return (await badge.innerText()).trim();
}

export async function closeAndReveal(page: Page): Promise<void> {
  await page.getByRole("button", { name: "Close & reveal…" }).click();
  await page.getByRole("button", { name: "Close & reveal", exact: true }).click();
  await expect(page.getByRole("region", { name: "Revealed" })).toBeVisible();
}

/** Choose a diagnosis consistent with the recorded evidence (public data only). */
export function plannedDiagnosis(c: CaseJson): { conclusion: "fault" | "no_fault" | "insufficient_evidence"; hypothesisId: string | null; label: string | null; repair: "R1" | "C1" } {
  const remaining: string[] = c.evidence.remaining;
  if (remaining.length === 1) {
    const h = remaining[0]!;
    const label = c.hypotheses.find((x: { id: string }) => x.id === h)?.label ?? null;
    if (h === "no_fault") return { conclusion: "no_fault", hypothesisId: null, label: null, repair: "R1" };
    return { conclusion: "fault", hypothesisId: h, label, repair: h.startsWith("r1") ? "R1" : "C1" };
  }
  return { conclusion: "insufficient_evidence", hypothesisId: null, label: null, repair: remaining.some((h) => h.startsWith("c1")) ? "C1" : "R1" };
}
