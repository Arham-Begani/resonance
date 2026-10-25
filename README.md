# Resonance

**Hear what a circuit does. Discover why it fails.**

Resonance is a browser electronics lab. You edit a small RC filter, run a real circuit
simulation (ngspice), hear the input and output at the same listening level, and investigate
hidden faults with measurements — alongside an NVIDIA Nemotron lab partner served by Nebius
Token Factory. No electronics kit or GPU is needed.

> Status: milestone 2 (low-pass and high-pass, Explore / Learn / Investigate). Live Nemotron
> inference is still unverified because no API key has been configured yet. See
> [Status and limitations](#status-and-limitations) for what is implemented, tested, and pending.

Repository: https://github.com/Arham-Begani/resonance

## What you can do

One workbench with three modes that share the schematic, component list, instruments and
experiment history:

- **Explore** an RC low-pass or RC high-pass filter. A first visit opens on a measured
  experiment (simulation only; nothing plays until you press Play). Edit R1/C1 with explicit
  SI units, run an ngspice AC sweep and a two-tone transient (low-pass: 440 Hz tone + 4 kHz
  whistle; high-pass: 50 Hz hum + 440 Hz tone), and see gain/phase, source current, waveform
  and spectrum. Play Input vs Output at one shared gain, and check a measured goal (e.g. "keep
  440 Hz within 1 dB, cut 4 kHz by ≥ 12 dB"). "Suggest values" gives an analytical E12
  preview, labeled as a prediction until you apply and measure it. Compare revisions and undo.
- **Learn**: predict whether a component change raises, lowers or leaves a tone's level, run
  the experiment, and get graded against the *measured* change (1 dB threshold), with optional
  hints.
- **Investigate** a hidden single-fault mystery in either family. Conditions: R1 or C1 at 2× or
  0.5×, capacitor disconnected, or no fault.
  1. Hear and see the standardized symptom.
  2. Choose up to three bounded experiments: output-voltage sweep, source-current sweep, or a
     two-tone capture of output voltage or source current.
  3. Track which explanations are supported, ruled out, or still possible.
  4. Submit an evidence-linked diagnosis, or say the evidence is insufficient.
  5. Verify a nominal-part repair.
  6. Close to reveal the fault, the seed, and the outcome. Identification and functional
     recovery are reported separately.

  Readings below the measurement floor (−120 dB, 1 nA) are shown as "below floor", never as
  numbers.
- **AI lab partner** (Nemotron via Nebius) offers "Investigate for me", hints and predictions
  in Guide-me mode, and natural-language Explore requests that produce previewed proposals you
  apply yourself. Without credentials every manual path keeps working, and the UI says the AI
  is unavailable.

## Requirements

- Python 3.12 and [uv](https://docs.astral.sh/uv/) (tested with uv 0.12.3)
- Node.js ≥ 20.15 and npm (tested with Node 20.15.1 / npm 10.7)
- ngspice: on Windows, the script below installs the official ngspice 47 build into `.tools/`;
  on Linux/macOS install the system package (e.g. `apt-get install ngspice`) or set `NGSPICE_BIN`.

## Setup

```bash
# 1. Simulator (Windows; verifies the SHA-256 published on SourceForge)
uv run --project services/api --with py7zr python scripts/install-ngspice.py

# 2. Backend dependencies
cd services/api && uv sync && cd ../..

# 3. Frontend dependencies
cd apps/web && npm ci && cd ../..

# 4. Configuration: copy and fill in your Nebius key (never commit .env)
cp .env.example .env
```

`.env` variables: `NEBIUS_API_KEY`, `NEBIUS_BASE_URL` (default
`https://api.tokenfactory.nebius.com/v1/`), `NEBIUS_MODEL` (e.g.
`nvidia/nemotron-3-super-120b-a12b`), optional `NGSPICE_BIN`, optional `RESONANCE_DATA_DIR`.

## Run

Development (two terminals; Vite proxies `/api` to the API):

```bash
cd services/api && uv run uvicorn resonance.app:app --host 127.0.0.1 --port 8000
cd apps/web && npm run dev          # http://localhost:5173
```

Single server (serves the built app and the API from one process, as in deployment):

```bash
cd apps/web && npm run build
cd services/api && uv run uvicorn resonance.app:app --host 127.0.0.1 --port 8000   # http://localhost:8000
```

Run exactly one uvicorn worker: case locks, cancellation, and agent event streams are in-process.

## Test

```bash
# Backend: unit, physics, audio, investigation, agent (real ngspice required)
cd services/api
RESONANCE_REQUIRE_NGSPICE=1 uv run pytest
uv run ruff check resonance tests && uv run mypy resonance

# Live Nemotron checks (opt-in; needs NEBIUS_API_KEY + NEBIUS_MODEL)
RESONANCE_LIVE_AI=1 uv run pytest tests/agent/test_agent_live.py -s -m live_ai

# Frontend (from the repo root)
cd apps/web
npm run typecheck && npm run lint && npm run test && npm run build

# Regenerate frontend API types after backend contract changes
npm run gen:api

# Browser end-to-end, keyboard and axe checks (starts its own API on :8044 and Vite on :5191
# with an isolated data dir; real ngspice; takes ~25 min on a 4-core machine)
npx playwright install chromium
npx playwright test
```

Independent verification reports (isolation, lifecycle and agent honesty, physics and audio,
browser and accessibility) are in [docs/verification/](docs/verification/).

## Architecture

React/TypeScript/Vite frontend (`apps/web`) and a FastAPI backend (`services/api/resonance`)
that runs ngspice as an isolated subprocess per job. Contracts are Pydantic models →
OpenAPI → generated TypeScript types. Hidden fault state lives only in an evaluator module
with its own SQLite file; the browser and the agent receive only sanitized measurements.
Details: [docs/architecture.md](docs/architecture.md), rules and boundary:
[docs/contracts.md](docs/contracts.md), AI integration: [docs/ai-integration.md](docs/ai-integration.md).

## Supported circuits and faults

| | Supported |
|---|---|
| RC low-pass | ideal source V1 (Input→Ground), R1 Input→Output, C1 Output→Ground; default 1 kΩ / 100 nF; tones 440 Hz + 4 kHz |
| RC high-pass | V1 (Input→Ground), C1 Input→Output, R1 Output→Ground; default 10 kΩ / 100 nF; tones 50 Hz hum + 440 Hz |
| Values | R1 100 Ω–100 kΩ, C1 1 nF–10 µF, cutoff 20 Hz–20 kHz |
| Faults | R1 ×2, R1 ×0.5, C1 ×2, C1 ×0.5, C1 disconnected, or no fault (1/6 each, drawn independently of the design); family 50/50 unless chosen |
| Not supported | other topologies, simultaneous faults, netlist import, voice |

## Modeling assumptions and limitations

- Ideal voltage source, ideal R and C; no parasitics, tolerances, temperature effects, or load.
  Results are simulations, not physical measurements.
- AC analyses use a 1 V source; source current uses the delivered convention.
- Audio is a 1.2 s clip of the simulated steady state after a fixed 0.2 s settling period,
  resampled to 48 kHz; every clip uses one shared gain (1 V = 100 % full scale) with matched
  15 ms fades; nothing is normalized.
- Output voltage of a first-order RC depends only on R·C, so R-vs-C faults are
  indistinguishable from voltage alone; source current separates them. With the symptom, one
  current measurement identifies every supported case.
- Measurement floors: gains below −120 dB and currents below 1 nA are reported as below the
  floor and never used as evidence (an open capacitor makes some readings exactly zero).

## Evaluation

`evals/` holds a frozen held-out set (`heldout_v1`: 24 cases, 2 per family × condition, unseen
nominal values) and two runners that use the same symptom, budget, tolerances and repair check.
Commands are in [evals/README.md](evals/README.md).

Measured baseline (`evals/results/baseline-heldout_v1-20261021T174452Z.md`, ngspice 47,
Windows): 24/24 correct, 0 abstentions, 0 unsupported certainty, 20/20 faulty cases restored by
the verified repair, 2 experiments per case, mean 4.29 s per case. These cases need only the
symptom plus one current measurement, so a perfect score shows the pipeline works; it is not
evidence of diagnostic skill. **No Nemotron evaluation has been run yet** (no API key).

## Deploy (Render)

`render.yaml` defines one Docker web service (the image builds the web app and installs
Debian's ngspice). In the Render dashboard choose **New → Blueprint**, select this repository,
and enter `NEBIUS_API_KEY` when prompted. `NEBIUS_MODEL` is pinned to
`nvidia/nemotron-3-super-120b-a12b`. Keep a single instance: case locks, cancellation and agent
streams are in-process. Health check: `/api/health`. The container is built and smoke-tested in
CI (`.github/workflows/ci.yml`); a Render deployment has not been performed yet.

## Status and limitations

See [docs/progress.md](docs/progress.md) for the current, dated status.

Continuous integration (GitHub Actions, Ubuntu, Debian ngspice 44.2) runs on every push. At
commit `d5dc196` it was green on all three jobs: backend ruff + mypy + 719 tests passed
(5 skipped: live-AI tests), frontend typecheck + lint + 69 unit tests + build, and Docker build +
container smoke test (health, a real experiment, case creation without a reveal).

Also verified on the development host (Windows 10, Python 3.12, Node 20.15, ngspice 47):

- Backend tests with real ngspice (`RESONANCE_REQUIRE_NGSPICE=1`), including
  physics within 1 % / 1° of independent formulas, audio tone levels within 0.5 dB of AC with
  no per-clip normalization, R-vs-C identifiability and ambiguity handling, hidden-state
  isolation across all fault types, budget and reveal-ordering races, and agent-loop honesty
  with scripted provider responses.
- Fresh setup from a clean copy of the repository with the commands above (milestone 1).
- Browser end-to-end suite (Playwright, real API and ngspice): 32/32 passed in 14.8 min at
  milestone 1. It covers the complete edit → simulate → compare → mystery → diagnose → repair
  → reveal flow, keyboard-only journeys, axe WCAG 2 A/AA scans and honest error states. The
  specs have since been updated for the automatic opening experiment, but **the full suite has
  not yet been re-run against the milestone-2 UI**. The last attempt was stopped by low memory
  on the development host, and every failure in it was a browser crash.
- Independent verification found and fixed 2 isolation/lifecycle majors, an accessibility
  focus major and an accidental-reveal UI major among others (see docs/verification/).

Not yet verified:

- **Live Nemotron inference**: no `NEBIUS_API_KEY` has been configured, so the agent has only
  run against scripted test responses (see docs/ai-integration.md).
- A real Render deployment (the image is only verified in CI).
- No real screen-reader session and no user testing; accessibility is unvalidated by
  assistive-technology users.
- Cases are solvable with one source-current measurement after the symptom, so perfect
  identification is not by itself evidence of agent skill.

## License

MIT — see [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
