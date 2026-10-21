"""Run the fixed-experiment diagnostic baseline on a frozen case set with REAL ngspice.

    cd services/api
    uv run python ../../evals/run_baseline.py                    # held-out set (24 cases)
    uv run python ../../evals/run_baseline.py --set dev_v1       # development set
    uv run python ../../evals/run_baseline.py --limit 2          # quick check

Writes ``evals/results/baseline-<set>-<UTC time>.json`` (every per-case record) and a Markdown
summary next to it. Case state lives in a scratch data directory that is deleted afterwards.
No credentials are needed; nothing here calls a model.
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

from resonance.config import Settings

RESULTS_DIR = casesets.EVALS_DIR / "results"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", default="heldout_v1", choices=casesets.SET_NAMES)
    parser.add_argument("--case", action="append", default=None, help="run only these keys")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--keep-data", action="store_true", help="keep the scratch data dir")
    args = parser.parse_args(argv)

    case_set = casesets.load(args.set)
    specs = casesets.select(case_set, args.case, args.limit)
    data_dir = Path(tempfile.mkdtemp(prefix="resonance-eval-baseline-"))
    settings = harness.scratch_settings(Settings(_env_file=None), data_dir)
    try:
        run = asyncio.run(harness.run_set("baseline", case_set, specs, settings))
    finally:
        if not args.keep_data:
            shutil.rmtree(data_dir, ignore_errors=True)
    run["summary"] = report.summarize(run["results"])
    run["notes"] = list(report.HONEST_NOTES)
    json_path, md_path = report.write_run(run, args.results_dir)
    totals = run["summary"]["totals"]
    print(
        f"\n{totals['cases']} cases: {totals['correct']} correct, {totals['incorrect']} incorrect, "
        f"{totals['abstained']} abstained, {totals['unsupported_certainty']} unsupported "
        f"certainty, {totals['restored']} restored, {totals['errors']} stopped by errors"
    )
    print(f"wrote {json_path}\nwrote {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
