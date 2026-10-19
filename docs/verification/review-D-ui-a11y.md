# Review D: browser end-to-end, keyboard, accessibility and honest UI states

Owner: independent verification engineer (lens D). Date: 2026-10-18. Milestone 1 (RC low-pass).

Tests: `apps/web/e2e/` (Playwright 1.64.1 + @axe-core/playwright 4.14.0, Chromium headless).
Config: `apps/web/playwright.config.ts` starts its own API (127.0.0.1:8044, real ngspice 47,
isolated data dir) and either the Vite dev server (127.0.0.1:5191, default) or, with
`RESONANCE_E2E_TARGET=prod`, a fresh `vite build` served by the API itself as deployed.
Each confirmed defect is asserted as correct behaviour in its own test marked `test.fail()`
with the finding ID, so the suite stays green while documenting it and turns red ("expected to
fail, but passed") once the defect is fixed. Remove the marker then.

## Method and limits

- Everything runs against the real UI, the real FastAPI app and real ngspice. Responses are
  mocked in exactly three places, each stated in the test: a structured simulator failure, a
  timeout body, and an HTML 502 / refused connection, all for `POST /api/experiments`.
- `NEBIUS_API_KEY` is set to an empty string for the API under test. **No live AI was tested.**
  Only the manual Guide-me path and the AI-unavailable states are covered.
- **No real screen-reader session was performed.** No screen reader (NVDA, JAWS) is installed
  on this host. axe-core and DOM assertions do not validate accessibility. Keyboard behaviour
  was checked with synthesized key presses only. Accessibility remains unvalidated by
  assistive-technology users.
- The hidden fault is random. Tests read only the public case payload (`GET /api/cases/{id}`
  with the browser's own cookie) to choose a diagnosis, so they work for every fault.
- Visual review: I looked at the screenshots in `apps/web/test-results/verification/chromium/`
  (desktop 1440×900 and narrow 390×844).

## Findings

No blocker was found. Severity scale: **blocker** stops release or a core task; **major**
significantly harms a core task, one user group's access, or honesty; **minor** has limited
impact or an easy workaround. Every finding below was reproduced at least twice.

| ID | Severity | Summary | Test |
|---|---|---|---|
| D-02 | major | Focus falls back to `<body>` after all 9 primary actions (Run experiment, Compare, Undo, Start a mystery, Guide me, Run instrument, Submit diagnosis, Propose repair, Apply repair and verify) | `keyboard.spec.ts` "D-02 …" ×2 |
| D-08 | minor | "Not enough evidence" pre-ticks explanations that the evidence table above it shows as "Ruled out" | `honesty.spec.ts` "D-08 …" |
| D-12 | minor (UI side of B-01) | Explore "Cancel" says "The experiment was cancelled", but the server finishes and stores the run. After a reload it is the experiment shown, labelled as an ordinary result | `honesty.spec.ts` "D-12 …" |
| D-13 | minor | Visually-hidden captions inside the scroll panels make the whole page scroll (232 px desktop, 348 px narrow). Header, rail and tabs move off-screen over a blank band | `a11y.spec.ts` "D-13 …" |
| D-07 | minor | Case status badge reads "Repair verified" next to the repair's own "Not restored" badge | `honesty.spec.ts` "D-07 …" |
| D-01 | minor | With AI unavailable, the measurement card's "Ask for a hint" stays enabled with no reason given. Pressing it only produces an error | `honesty.spec.ts` "D-01 …" |
| D-06 | minor (dev server only) | First visit on the dev server creates 2 sessions and 2 "original" revisions. A reload then jumps from the measured Revision 1 to an unmeasured "Revision 2". The first experiment once failed with "That goal is no longer the current goal". The production build is unaffected | `honesty.spec.ts` "D-06 …" ×2 |
| D-03 | minor | Chart data-table scroll containers are not keyboard-focusable (axe `scrollable-region-focusable`, WCAG 2.1.1, the only WCAG 2.0 A/AA violation) | `honesty.spec.ts` "D-03 …" |
| D-04 | minor | Component-row buttons' `aria-label` omits visible text (WCAG 2.1 2.5.3 Label in Name, axe `label-content-name-mismatch`) | `honesty.spec.ts` "D-04 …" |
| D-05 | minor | Non-JSON error responses show "The server returned an unexpected response (502)." to learners | `honesty.spec.ts` "D-05 …" |
| D-09 | minor | In a case, viewing a sweep observation shows "No clips yet. Run an experiment to render audio." in the playback strip | `honesty.spec.ts` "D-09 …" |
| D-10 | minor | After "Dismiss" on the AI notice, the strip still says "setup steps are in Lab notes", but they are gone from Lab notes | `honesty.spec.ts` "D-10 …" |
| D-11 | minor | `aria-label` on two generic `<div>`s (`.header__status` "System status", `.comparison` "Comparison result") is prohibited by ARIA 1.2 and ignored by assistive technology | `honesty.spec.ts` "D-11 …" |

### D-02 (major): focus is lost after every primary action

**Reproduction:**
1. Open the workbench.
2. Tab to "Run experiment on Revision 1" and press Enter.
3. When the result appears, `document.activeElement` is `<body>`.

The same happens after Compare and after Undo to Revision 1. In Investigate it happens after
Start a mystery, Guide me, Run (any instrument), Submit diagnosis, Propose repair, and Apply
repair and verify: 9 of 9 actions. Recorded in both the keyboard-only runs and the dedicated
D-02 tests.

**Cause:**
- `Button` sets `disabled={disabled || loading}` (`src/components/ui/primitives.tsx:34`).
  Chromium blurs a focused element when it becomes disabled.
- Other actions unmount the focused control. For example, StartCard, ApproachCard and
  DiagnosisForm are replaced, and the repair form is replaced by the result.
- Nothing moves focus afterwards. The exceptions are handled correctly: the value editor
  returns focus to the row, Cancel returns focus to Run, mode switches focus the main region,
  and Close focuses the "Revealed" heading.

**Impact:**
- Keyboard users in Chromium can usually continue with Tab, because the sequential-navigation
  starting point is kept.
- Screen-reader users are moved to the top of the document after each experiment, diagnosis
  and repair, and lose their place in the investigation panel. This applies to every step of
  the core Investigate task.

**Suggested fix:**
- Keep loading buttons focusable: use `aria-disabled="true"`, ignore clicks while busy, and
  keep `aria-busy`.
- After completion, move focus to the new content's heading (`tabIndex={-1}`):
  - the new observation or "Recorded measurements" after an instrument run;
  - the "Diagnosis" heading after submit;
  - the repair result after verify;
  - "What we observed" after Start;
  - "Take a measurement" after Guide me;
  - the comparison title after Compare.
- After Undo at the root revision, focus the revision label or the Run button.

### D-08 (minor): stale candidate ticks in "Not enough evidence"

**Reproduction:**
1. Start mysteries until the symptom leaves two explanations (any faulty case, for example
   `r1_decreased` and `c1_decreased`).
2. Choose Guide me and run the Output response sweep and the Source current sweep.
   `evidence.remaining` becomes `["r1_decreased"]`.
3. Choose "Not enough evidence".

"Explanations that still fit" has both `r1_decreased` and `c1_decreased` ticked. The table
above it shows "C1 is about half its nominal value — Ruled out". See the screenshot
`honesty-stale-candidates.png`.

**Cause:** `useState(() => [...c.evidence.remaining])`
(`src/features/investigate/DiagnosisForm.tsx:57`). The form first mounts in `ready`, because
`submit_diagnosis` is available before any experiment, and its state is never re-synced.

**Impact:**
- This applies to premature abstentions, which the evidence check is designed to flag.
- The default contradicts the evidence on screen. If submitted, the recorded diagnosis lists
  ruled-out explanations under "Still possible".
- "True cause among your candidates" at reveal uses that list.

**Suggested fix:** derive the default from the current `remaining` until the user edits the
checkboxes. Alternatively, mount the form only when the conclusion is chosen, or key it on the
observation count.

### D-12 (minor here; root cause B-01): Explore "Cancel" does not cancel

**Reproduction:**
1. Measure Experiment 1.
2. Click Run, then Cancel immediately. The UI shows "The experiment was cancelled. Previous
   results are unchanged."
3. About 8 s later, `GET /api/workbench` lists `["succeeded","succeeded"]`.
4. Reload. History shows "Experiment 2 · … · Goal not met · Shown", and its result is the one
   displayed.

**Cause:** the browser aborts its request (`state/workbench.tsx:452`). The server does not
notice the disconnect (B-01 in review-B) and has no cancel endpoint for Explore experiments.

**Suggested fix:** fix B-01, or add a cancel operation keyed by `client_request_id`.
Until then, word it honestly, for example: "Stopped waiting. The simulation may still finish
and appear in history."

### D-13 (minor): the page itself scrolls under the panels

**Reproduction:** in a case at 1440×900, wheel-scroll the lab-notes panel to its end. Then
`scrollY = 232`: the whole app moves up, the header and rail leave the screen, and an empty
band appears under the strip. At 390×844 on the Lab notes tab, `scrollY = 348`.

**Cause:**
- `.visually-hidden` is `position: absolute` (`src/styles/base.css:86`).
- The scroll containers `.workspace__panel` and `.workspace__center` are not positioned.
- Hidden captions, legends and labels far down a panel therefore take the initial containing
  block as their containing block. They escape the panel's `overflow: auto` and `.app`'s
  `overflow: hidden`, and extend the document. Measured: the bottom of the caption
  "Explanations and their status…" equals `scrollHeight` (1192).
- Injecting `.workspace__panel, .workspace__center { position: relative; }` brought
  `scrollHeight` back to the viewport height, 844 of 844.

**Suggested fix:** that one CSS rule. Alternatively, make `.app` the containing block.

### D-07 (minor): "Repair verified" on a repair that failed

**Reproduction:**
1. In a faulty case, run the Source current sweep and submit the supported fault.
2. Propose replacing the other component, then Apply repair and verify.

The repair card shows "Not restored" (for example, "28 of 31 frequencies are outside
±0.5 dB / ±3°"). The case status badge reads **"Repair verified"**.

**Cause:** `STATUS_LABEL.repair_verification = "Repair verified"`
(`src/features/investigate/labels.ts:12`).

**Suggested fix:** use a neutral label ("Repair checked" or "Repair tested"), or derive it
from `repair.functional_recovery`.

### D-01 (minor): AI-dependent hint button enabled without AI

**Reproduction:** with no key, start a mystery and choose Guide me. "Ask for a hint" in "Take a
measurement" is enabled and nothing nearby says why it cannot work. Pressing it shows
"AI lab partner unavailable: NEBIUS_API_KEY is not set. … Manual tools keep working." The
title is repeated inside the message.

The strip's own Hint control is correctly disabled with the reason attached through
`aria-describedby`.

**Cause:**
- `disabled={!canHint}` uses only `available_actions`
  (`src/features/investigate/InvestigatePanel.tsx:265`).
- The server offers `request_hint` regardless of AI availability
  (`resonance/challenges/service.py:394`).

**Suggested fix:** also disable when `!health.ai.available`, and attach the same reason note.

### D-06 (minor, dev server only): doubled first-visit initialisation

**Reproduction (dev server, natural timing, 5 of 5 runs):**
- A first visit receives 2 distinct `rs_session` cookies.
- The final session holds 2 `template_default` revisions with an identical `created_at`.
- After measuring "Revision 1" and reloading, the header shows "Revision 2" (original values,
  unmeasured).
- Observed once in this session: the first experiment of a first visit failed with "Not
  available right now — That goal is no longer the current goal. Reload the workbench and try
  again." The goal came from one session and the cookie belonged to the other.

**Production build** (`RESONANCE_E2E_TARGET=prod`, 3 of 3 runs): 1 session, 1 revision, and the
reload is consistent.

**Cause:**
- React StrictMode runs the init effect twice
  (`src/state/workbench.tsx:343`: `refreshHealth().then(() => reload())`). Both cookieless
  health calls create sessions.
- Both `reload()` calls see no revisions and each POST a root
  (`src/state/workbench.tsx:312`).
- On the first load the current revision is never persisted (lines 349-361), so a reload falls
  back to the last root.

**Suggested fix:**
- Make init idempotent, for example a module-level `initPromise ??= …`.
- Persist the initially shown revision.
- Optionally, have the server create the default revision atomically in `GET /api/workbench`.

**Effect on these tests:** all other tests prime the session cookie with one
`GET /api/health` before loading the page (`primeSession` in `e2e/support/workbench.ts`), so
they test the rest of the app deterministically. The two D-06 tests do not.

### D-03 (minor): data-table scroll containers not keyboard reachable

axe `scrollable-region-focusable` (serious, WCAG 2.1.1) flags `.chart__table-scroll`
(`src/components/charts/ChartFigure.tsx:106`) in every state with an open "Data table". It is
the only WCAG 2.0 A/AA violation in the whole walk.

Current Chromium and Firefox make scrollers keyboard-focusable on their own; Safari does not.

**Suggested fix:** `tabIndex={0}`, `role="region"` and `aria-label={`${title} data`}` on the
scroll container.

### D-04 (minor): Label in Name on component rows

The row buttons use `aria-label={c.description}` (`ComponentList.tsx:133`), for example
"R1 — resistor — 1 kΩ — connected between Input and Output". The visible text is "R1 resistor
1 kΩ Input → Output", and V1's visible "0.6 V + 0.3 V" is absent from its name. Speech-input
users cannot reliably say what they see.

axe `label-content-name-mismatch` (WCAG 2.1 A, 2.5.3) flags 3 nodes. axe's wcag2a/wcag2aa tags
do not cover it, so it was recorded as information in the walk and asserted separately.

**Suggested fix:** keep the visible text as the name and move the long description to
`aria-describedby`.

### D-05 (minor): HTTP status code shown to learners

**Reproduction:** `POST /api/experiments` returns 502 with an HTML body, as from a proxy or
gateway. The callout reads "Something went wrong — The server returned an unexpected response
(502). The previous result is still shown." The previous result is kept (good).

**Cause:** `src/api/client.ts:56`.

**Suggested fix:** use a calm message without the code ("The server could not complete the
request. Try again in a moment."). Keep the status for developer logs only.

### D-09 (minor): misleading playback status in a case

**Reproduction:** in a case, run the Source current sweep. The strip says "No audio" and "No
clips yet. Run an experiment to render audio." This shows right after an experiment was run,
and the symptom has playable clips.

**Cause:** the generic status text in `src/components/audio/PlaybackStrip.tsx:24` when the
shown observation has no audio.

**Suggested fix:** context text such as "This measurement has no audio (sweeps are shown as
charts). Choose Symptom or a capture to listen."

### D-10 (minor): pointer to setup steps that were dismissed

**Reproduction:**
1. Press "Dismiss" on the AI notice.
2. The command strip still says "AI lab partner unavailable. Manual tools keep working; setup
   steps are in Lab notes." (`src/layout/CommandStrip.tsx:47`).
3. Lab notes no longer contains them until a reload (`src/layout/Shell.tsx:52`).

**Suggested fix:** keep a compact, non-dismissible "AI unavailable · setup" disclosure, or
change the strip text after dismissal.

### D-11 (minor): names on generic elements

`<div className="header__status" aria-label="System status">` (`HeaderStrip.tsx:54`) and
`<div className="comparison" aria-label="Comparison result">` (`HistoryPanel.tsx:134`) have no
role. ARIA 1.2 prohibits `aria-label` on `generic`, so screen readers ignore these names (axe
reports `aria-prohibited-attr` as "needs review").

**Suggested fix:** `role="group"` on both, or `role="region"` for the comparison.

RESULTS_PLACEHOLDER

## Final status (lead addendum, 2026-10-19)

The lens D agent stopped on account rate limits during its last run, so this section was
written by the lead from that run's `e2e-results.json` and from targeted re-runs.

- All 13 findings D-01…D-13 were fixed in `apps/web/src` by the interface engineer. In the
  verifier's last full run (Oct 19, 01:47) every D test passed as a plain regression test:
  **29 passed, 2 failed**.
  - *Audio test* (failed once on "3 source starts", later once on a 1.9 s gesture latency) and
    the *keyboard-only Explore journey* (timed out at 300 s under full-suite load). The lead
    reproduced both in isolation: the audio test passed 2/2 with one source at a time, the
    AudioContext created inside the Play click handler and 2 starts; the keyboard test passed
    in 3.1 min. Changes (lead): the gesture latency is now bounded by Chrome's 5 s activation
    window, because the test stamps the gesture at `pointerdown` and so includes main-thread
    queueing on the loaded dev server; the keyboard journey gets a 10 min budget. No assertion
    about single-source playback, start counts or focus was relaxed. The one-off "3 starts"
    observation was not reproduced and remains unexplained.
- **D-14 (major, new, fixed):** a later full run (29 passed, 2 failed) showed the ambiguity
  and D-08 tests timing out in `closeAndReveal`. The page snapshot showed the new case's
  Finish card already armed ("Close & reveal" + "Keep investigating") right after
  "New mystery": the per-case cards kept React local state across cases, so one click would
  reveal a fresh case without confirmation (and drafts/choices could carry over). Fixed by
  keying all per-case cards by case ID (`InvestigatePanel.tsx`); new regression test
  `honesty.spec.ts` "D-14 …". The D-14, D-08 and ambiguity tests then passed (3/3, 2.4 min).
- Final full-suite confirmation run after the D-14 fix (lead, 2026-10-19 21:38): **32 passed,
  0 failed in 14.8 min** (exit 0).
- Unconfirmed observation from the verifier's last session: on the narrow layout the header's
  goal pill may be clipped behind the tab bar (seen on screenshots since Oct 16; not measured).
- Still not performed: a real screen-reader session and testing with assistive-technology
  users. Accessibility remains unvalidated by such users.
