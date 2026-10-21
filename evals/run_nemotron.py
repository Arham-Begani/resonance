"""Run the Nemotron agent ("Investigate for me") on a frozen case set with REAL ngspice and
REAL Nebius Token Factory inference, under the same protocol as the baseline.

    cd services/api
    uv run python ../../evals/run_nemotron.py --set dev_v1 --limit 2   # smoke run first
    uv run python ../../evals/run_nemotron.py                          # held-out set

Requires NEBIUS_API_KEY and NEBIUS_MODEL (repository ``.env`` or environment). Without them it
refuses to run, says so, writes nothing and exits with status 2: there is no scripted stand-in
and no result is ever fabricated. The development fixture mode is refused as well.
Writes ``evals/results/nemotron-<set>-<UTC time>.json`` + ``.md`` with the provider-reported
usage (model calls, tokens, latency) of every run. Secrets are never printed or stored.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

import casesets
import harness
import report

from resonance.agent.status import ai_status
from resonance.config import Settings

RESULTS_DIR = casesets.EVALS_DIR / "results"
EXIT_AI_UNAVAILABLE = 2


def refusal(settings: Settings) -> str | None:
    """A plain explanation when real inference is not configured, else None."""
    status = ai_status(settings)
    if status.available and status.state == "ready":
        return None
    return (
        "Nemotron evaluation NOT run: the AI lab partner is unavailable "
        f"({status.state}). {status.message} No results were written."
    )


def main(argv: list[str] | None = None, settings: Settings | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", default="heldout_v1", choices=casesets.SET_NAMES)
    parser.add_argument("--case", action="append", default=None, help="run only these keys")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    args = parser.parse_args(argv)

    base = settings if settings is not None else Settings()
    message = refusal(base)
    if message is not None:
        print(message, file=sys.stderr)
        return EXIT_AI_UNAVAILABLE
    case_set = casesets.load(args.set)
    specs = casesets.select(case_set, args.case, args.limit)
    data_dir = Path(tempfile.mkdtemp(prefix="resonance-eval-nemotron-"))
    run_settings = harness.scratch_settings(base, data_dir)
    try:
        run = asyncio.run(harness.run_set("nemotron", case_set, specs, run_settings))
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)
    sources = {r["agent"]["source"] for r in run["results"] if r.get("agent")}
    if sources - {"nebius"}:
        print("Refusing to report runs that were not real Nebius inference.", file=sys.stderr)
        return EXIT_AI_UNAVAILABLE
    run["summary"] = report.summarize(run["results"])
    run["notes"] = list(report.HONEST_NOTES)
    json_path, md_path = report.write_run(run, args.results_dir)
    print(f"wrote {json_path}\nwrote {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
