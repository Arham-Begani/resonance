# Resonance

**Hear what a circuit does. Discover why it fails.**

Resonance is a browser electronics lab. You edit a small RC filter, run a real circuit
simulation (ngspice), hear the input and output at the same listening level, and investigate
hidden faults with measurements — alongside an NVIDIA Nemotron lab partner served by Nebius
Token Factory. No electronics kit or GPU is needed.

> Status: milestone 1 (low-pass workflow). See [Status and limitations](#status-and-limitations)
> for exactly what is implemented, tested, and still pending.

## What you can do (milestone 1)

- **Explore** an RC low-pass filter: edit R1/C1 with explicit SI units, run an ngspice AC sweep
  and a two-tone transient (440 Hz musical tone + 4 kHz whistle), see gain/phase, source
  current, waveform and spectrum, play Input vs Output at one shared gain, check a measured
  goal ("keep 440 Hz within 1 dB, cut 4 kHz by ≥ 12 dB"), compare revisions, undo.
- **Investigate** a hidden single-fault mystery (R1 or C1 at 2× or 0.5×, or no fault): hear and
  see the standardized symptom, choose up to three bounded experiments (output-voltage sweep,
  source-current sweep, two-tone capture of output voltage or source current), track which
  explanations are supported / ruled out / still possible, submit an evidence-linked diagnosis
  (or say the evidence is insufficient), verify a nominal-part repair, then close to reveal the
  fault, seed, and outcome (identification and functional recovery reported separately).
- **AI lab partner** (Nemotron via Nebius): "Investigate for me", hints in Guide-me mode, and
  natural-language explore requests that produce previewed, user-applied proposals. Without
  credentials every manual path keeps working and the UI says the AI is unavailable.

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

| | Milestone 1 |
|---|---|
| Circuit | RC low-pass: ideal source V1 (Input→Ground), R1 Input→Output, C1 Output→Ground |
| Values | R1 100 Ω–100 kΩ, C1 1 nF–10 µF, cutoff 20 Hz–20 kHz |
| Faults | R1 ×2, R1 ×0.5, C1 ×2, C1 ×0.5, or no fault (20 % each, drawn independently of the design) |
| Not yet | RC high-pass, capacitor disconnected, Learn mode, voice |

## Modeling assumptions and limitations

- Ideal voltage source, ideal R and C; no parasitics, tolerances, temperature effects, or load.
  Results are simulations, not physical measurements.
- AC analyses use a 1 V source; source current uses the delivered convention.
- Audio is a 1.2 s clip of the simulated steady state after a fixed 0.2 s settling period,
  resampled to 48 kHz; every clip uses one shared gain (1 V = 100 % full scale) with matched
  15 ms fades; nothing is normalized.
- Output voltage of a first-order RC depends only on R·C, so R-vs-C faults are
  indistinguishable from voltage alone; source current separates them. With the symptom, one
  current measurement identifies every milestone-1 case.

## Status and limitations

See [docs/progress.md](docs/progress.md) for the current, dated status.

Verified on the development host (Windows 10, Python 3.12, Node 20.15, ngspice 47):

- Backend: 544 tests pass with real ngspice (`RESONANCE_REQUIRE_NGSPICE=1`), including
  physics within 1 % / 1° of independent formulas, audio tone levels within 0.5 dB of AC with
  no per-clip normalization, R-vs-C identifiability and ambiguity handling, hidden-state
  isolation across all fault types, budget and reveal-ordering races, and agent-loop honesty
  with scripted provider responses.
- Frontend: typecheck, lint, 43 unit tests and the production build pass; 42/42 contrast pairs.
- Fresh setup from a clean copy of the repository with the commands above.
- Independent verification found and fixed 2 isolation/lifecycle majors, an accessibility
  focus major and an accidental-reveal UI major among others (see docs/verification/).

Not yet verified:

- **Live Nemotron inference**: no `NEBIUS_API_KEY` has been configured, so the agent has only
  run against scripted test responses (see docs/ai-integration.md).
- The final full browser-suite run after the last UI fix (targeted re-runs passed).
- The Docker image (Docker is unavailable on the development host).
- No real screen-reader session and no user testing; accessibility is unvalidated by
  assistive-technology users.
- Milestone-1 cases are solvable with one source-current measurement after the symptom, so
  perfect identification is not by itself evidence of agent skill.

## License

MIT — see [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
