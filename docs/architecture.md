# Architecture

```
browser (React/TS, apps/web)
  │  same-origin fetch + SSE, anonymous session cookie
  ▼
FastAPI (services/api/resonance)
  ├─ api/            routes, session middleware, structured errors
  ├─ circuits/       SI units, rc_lowpass template, immutable revisions, trusted netlists
  ├─ simulation/     ngspice subprocess runner (isolated job dirs, limits), raw parser,
  │                  analytical RC equations (checks/predictions only)
  ├─ audio/          two-tone capture → uniform PCM, tone fits, spectra, shared-gain WAVs
  ├─ explore/        experiment bundle (AC + transient + audio), goals, comparisons
  ├─ challenges/     CaseService (public lifecycle, budget, hypotheses, diagnosis, repair)
  │   └─ evaluator/  HIDDEN state: faults, seeds, actual circuits (own SQLite file)
  ├─ agent/          Nebius Token Factory client, bounded Nemotron tool loop,
  │                  explore / investigate / hint tool sets, run registry + SSE events
  └─ store/          SQLite document store (session-scoped), bounded artifact directory
        │
        ▼
  ngspice (separate process per job)     Nebius Token Factory (Nemotron, HTTPS)
```

Key decisions:

- **One deployment**: FastAPI serves `/api/*` and the built SPA. Dev uses the Vite proxy.
- **Contracts first**: Pydantic models in `resonance/contracts/` → OpenAPI → generated TS
  (`npm run gen:api`). Shared rules in `docs/contracts.md`.
- **Real simulation only**: every displayed response comes from ngspice. Analytical formulas
  are used for tests, hypothesis predictions, feasibility, and proposal previews, and are
  always labeled as predictions.
- **Hidden-state boundary**: only `challenges/evaluator` touches actual faulted circuits; it
  returns sanitized measurements. Agents have no file/shell/HTTP/database tools.
- **Honest audio**: one global voltage-to-playback gain; clips are never normalized
  individually; any headroom reduction is shared and disclosed.
- **Agent**: one bounded loop (no framework), OpenAI-compatible client, validated tool
  arguments, measured usage, genuine events, cooperative cancellation.
- **Storage**: SQLite (public documents) + a separate SQLite file for hidden case state; WAV
  artifacts in a bounded directory with random IDs. No Redis or distributed infrastructure.
