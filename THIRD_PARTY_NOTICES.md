# Third-party notices

Resonance is MIT-licensed (see `LICENSE`). It uses the following third-party software.
This repository does not vendor these projects; they are installed by package managers or,
for ngspice on Windows, downloaded by `scripts/install-ngspice.py`.

## Simulator (runtime, separate process)

- **ngspice** — https://ngspice.sourceforge.io/ — "Modified BSD" license for most of its
  source, with some files under LGPLv2, GPLv2-or-later, MIT-style, and other licenses
  (see `COPYING` in the ngspice distribution). Resonance invokes the unmodified ngspice
  binary as an external subprocess. The container image installs the Debian `ngspice` package.

## Backend (Python)

- FastAPI (MIT), Starlette (BSD-3-Clause), Uvicorn (BSD-3-Clause), Pydantic and
  pydantic-settings (MIT), NumPy (BSD-3-Clause), httpx (BSD-3-Clause),
  OpenAI Python SDK (Apache-2.0; used as an OpenAI-compatible client for Nebius Token Factory).
- Development: pytest (MIT), pytest-asyncio (Apache-2.0), Ruff (MIT), mypy (MIT).

## Frontend (JavaScript/TypeScript)

- React and React DOM (MIT), d3-scale and d3-shape (ISC), openapi-fetch and
  openapi-typescript (MIT).
- Development: Vite (MIT), TypeScript (Apache-2.0), Vitest (MIT), Testing Library (MIT),
  jsdom (MIT), ESLint and plugins (MIT), Playwright (Apache-2.0), axe-core (MPL-2.0).

The complete dependency trees with exact versions are recorded in `services/api/uv.lock`
and `apps/web/package-lock.json`.

## AI model

- NVIDIA Nemotron models are accessed through the Nebius Token Factory API under the
  provider's terms; no model weights are included in this repository.
