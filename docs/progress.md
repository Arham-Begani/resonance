# Progress notes

Concise, factual notes for resuming work. Newest first. Distinguish implemented / tested / proposed.

## 2026-10-16 — Milestone 1 builders finished; verification pass NOT yet run

Accepted (lead reran each suite with RESONANCE_REQUIRE_NGSPICE=1):
- circuits/simulation 206 tests; audio/explore 45; agent 59 (+3 live tests skipped: no key);
  challenges 47; core 15. Worker full-suite runs: 364 passed / 3 skipped. ruff + mypy clean.
- Frontend: typecheck, lint, 38 vitest tests, build (391 kB JS) pass; contrast 42/42 pairs.
- Explore end-to-end via API verified by lead (1 k/100 n fails 12 dB goal at 8.64 dB; 1.6 k passes
  at 12.35 dB; transient vs AC ≤ 0.05 dB; shared-gain WAVs). Single-server mode verified.
- UI worker verified Explore + Guide-me investigation against the live backend (screenshots in
  apps/web/test-results/screenshots/). AI paths are fixture-tested only.

Update (same day): full backend suite 372 passed / 3 skipped (live AI) in 5:16 with
RESONANCE_REQUIRE_NGSPICE=1. Fresh-setup test PASSED in a clean copy of the clone file set
(D:\tmp\fresh): install-ngspice.py (download + SHA-256 + extract, 2:07), `uv sync`, `npm ci`,
frontend typecheck/lint/38 tests/build, backend core+circuits+simulation 221 passed, single
server: health, revision, experiment (1.9 s), case creation, SPA. Independent 4-lens
verification workflow launched (results pending).

## 2026-10-19 — Milestone 1 committed; final browser confirmation pending

- Git history created (the project now asks for granular, meaningful commits): ~48 focused
  commits from scaffold through contracts, backend layers, tests, verification suites, web
  features, the D-14 fix and docs. Commits end with the Claude co-author trailer. No remote
  configured; nothing pushed.
- Lens D (browser/a11y): 13 findings fixed by the UI worker; the lead found and fixed D-14
  (per-case cards kept local state across cases → armed one-click "Close & reveal" on every
  new mystery) by keying them by case ID, with a regression test. Two e2e timing assertions
  were made robust (gesture latency vs 5 s activation window; 10 min budget for the long
  keyboard journey) without relaxing behavior checks.
- Last complete full e2e run: 29 passed / 2 failed (both caused by D-14, since fixed);
  targeted re-runs of D-14, D-08, ambiguity, audio and keyboard tests passed. A final full
  run after the D-14 fix was started at 21:38 but the host hit critically low memory
  (374 MB free, mostly non-project processes); its output is in D:\tmp\e2e_final2.txt.
  NEXT: confirm that run (or rerun `cd apps/web && npx playwright test` when memory allows).
- Live Nemotron still unverified (no NEBIUS_API_KEY).

## 2026-10-18 — Independent verification results and fixes

Verification workflow (project `verification-engineer` agents, Opus): lenses A (isolation) and B
(lifecycle/agent) completed; lens C (physics/audio) wrote its test file before an account error;
lens D (UI/e2e) was relaunched separately. Findings and status:
- A-01 major (fixed, investigation worker): hidden-run numbers left the evaluator at full float64
  precision; ngspice round-off differs ~1e-15 between voltage-equivalent R/C variants, so Explore
  could fingerprint ~7 % of hosted cases. Now quantized at the evaluator boundary (contract text in
  docs/contracts.md "Payload hygiene"); 15 fingerprintable designs give byte-identical payloads.
- A-02 minor (fixed): retry wording only when a retry ran.
- B-01 major (fixed, lead): BaseHTTPMiddleware hid client disconnects → experiments kept running
  and consumed budget. Session middleware is now pure ASGI; confirmed on real uvicorn (case:
  budget 0 used, observation cancelled; Explore: nothing stored).
- B-02/B-03/B-04/B-05 (fixed): stale run ownership after crash released via liveness predicate;
  repair endpoints report run_in_progress; agent sees its real available actions; hints_used
  counts delivered hints only.
- C-01 (fixed, lead): goal feasibility now intersects with the template's 20 Hz–20 kHz range.
- C-03 (fixed, lead): JSON booleans no longer coerce to 1.0 in component values.
All strict-xfail markers for fixed findings were removed after observing XPASS.

NEXT (in order):
1. Independent verification pass (4 lenses using the `verification-engineer` project agent,
   model opus): leakage/isolation; lifecycle/budget/agent honesty; physics/audio/explore;
   UI/a11y + Playwright e2e (apps/web/e2e, owned by verification). Read
   docs/verification/plan.md. Triage and fix material findings, rerun suites.
2. Fresh-setup test in a copied tree (uv sync, npm ci, install-ngspice, run, test) → finalize
   README commands. Commit only when the user asks (no commits made yet; baseline is staged).
3. Live Nemotron: needs NEBIUS_API_KEY + NEBIUS_MODEL in .env, then
   `RESONANCE_LIVE_AI=1 uv run pytest tests/agent/test_agent_live.py -s -m live_ai`; record in
   docs/ai-integration.md; then 10 varied end-to-end agent trials.
4. Known gaps: Docker image unbuilt; no screen-reader/user testing; charts below the fold at
   1440×900; milestone-1 cases are solvable with one current measurement (state honestly).

## 2026-10-15 — Milestone 1 kickoff (low-pass workflow + foundations)

Environment (verified): Windows 10, Python 3.12.10, uv 0.12.3, Node 20.15.1, npm 10.7.
No Docker, no ngspice preinstalled, no Nebius key yet (user asked to add `NEBIUS_API_KEY` to `.env`).

Done by lead:
- Repo scaffold: `services/api` (uv project, flat `resonance/` package), `apps/web` (Vite 6.4 +
  React 19.3 + TS 5.9; versions chosen for Node 20.15), `docs/`, `scripts/`, `.claude/agents/`.
- ngspice 47 (official Windows build, SHA-256 verified) installed to `.tools/` via
  `scripts/install-ngspice.py` (tested end to end into a scratch dir).
- Nebius facts from the official cookbook: base URL `https://api.tokenfactory.nebius.com/v1/`;
  Nemotron ids `nvidia/nemotron-3-super-120b-a12b`, `nvidia/Nemotron-3_5-Lightning`,
  `nvidia/Nemotron-3-Ultra-550b-a55b`. Not yet verified with a key.
- Contracts (`services/api/resonance/contracts/`), boundary doc (`docs/contracts.md`), config,
  SQLite document store, bounded artifact store, session cookie middleware, structured errors,
  all route signatures (26 endpoints), OpenAPI export → generated TS types.
- `.env.example`, `Dockerfile` (untested: no Docker on host), `LICENSE` (MIT), notices.

Delegation: project agents defined with `model: opus` but only load at session start, so
workers run as `general-purpose` with `model: "opus"`; transcripts show `claude-opus-5-5`.
Workers: verification (contract review), circuit/simulation, audio/explore, investigation
(evaluator + cases), AI (agent runtime), UI/accessibility.

Early contract review (verification, `docs/verification/contract-review.md`): 2 blockers,
10 majors, 8 minors. Adopted (contracts + `docs/contracts.md` updated, workers notified):
agent allow-list views + explicit `assess_evidence` tool (V-01); per-case serialization,
required `client_request_id`, agent run blocks mutations (V-02); close cancels and awaits the
agent before reveal (V-03); no caches for hidden runs (V-04); 128-bit seeds, i.i.d. independent
fault/nominal draws, published priors (V-05); public-only pre-close state (V-06); case nominal
cutoff 40 Hz–5 kHz inside template 20 Hz–20 kHz (V-07); transient instrument probes
output_voltage|source_current (V-08); repair = nominal replacement, verify recorded proposal
(V-09); closed `QuantityName` vocabulary (V-10); transition table (V-11); transient max step
10 µs pending audio validation (V-12); minors V-13–V-20 (validators, outcome fields, settings
echo, dB floor, per-session artifact eviction, current-revision pointer, 429 mapping).
Verified by the reviewer: R-vs-C voltage equivalence and 2.000 current ratio in ngspice,
current sign, 31-point grid, clip-free headroom (0.898 FS worst), settling, no timestep side
channel. Honest note: M1 cases are solvable with one current measurement.

Open: integrate worker results; live Nemotron verification (needs key); README with verified
commands; independent verification pass; browser review.
