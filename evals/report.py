"""Evaluation reports: ``results/<run>.json`` (every per-case record) + a Markdown summary.

    cd services/api
    uv run python ../../evals/report.py ../../evals/results/<run>.json   # re-render the .md

Only measured quantities are reported: outcomes come from the case reveal (``CaseOutcome``),
latencies are wall-clock measurements of this run, and agent usage is what the provider
reported. Costs are not reported (no measured usage-to-price mapping is verified here).
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

FAMILY_LABELS = {"rc_lowpass": "RC low-pass", "rc_highpass": "RC high-pass"}
CONDITION_ORDER = (
    "no_fault",
    "r1_increased",
    "r1_decreased",
    "c1_increased",
    "c1_decreased",
    "c1_disconnected",
)

HONEST_NOTES = (
    "Milestone-2 cases are solvable with the standardized symptom plus ONE current "
    "measurement: the symptom separates every condition except the two voltage-equivalent "
    "pairs (R1×2 vs C1×2 and R1×0.5 vs C1×0.5, identical output voltage), and either current "
    "instrument separates those. A perfect score shows that the evidence pipeline, the "
    "budget and the repair verification work end to end on these cases; it is not evidence of "
    "diagnostic skill and 100 % identification is not an achievement by itself.",
    "Small sample (one frozen held-out set). No statistical comparison or superiority claim "
    "is made between runners.",
    "Circuits are simulated with ngspice (ideal source, ideal R and C, no load, parasitics or "
    "component tolerances). Results are simulations, not physical measurements.",
    "Latencies are wall-clock times on the machine that ran the evaluation, including the "
    "ngspice subprocesses; they are not a service-level benchmark.",
)


def _percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile (q in [0, 1])."""
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[rank - 1]


def latency_stats(values: Iterable[float]) -> dict[str, float] | None:
    data = [float(v) for v in values]
    if not data:
        return None
    return {
        "n": len(data),
        "mean_s": round(statistics.fmean(data), 3),
        "median_s": round(statistics.median(data), 3),
        "p90_s": round(_percentile(data, 0.9), 3),
        "max_s": round(max(data), 3),
    }


def _group(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    def count(field: str, value: Any) -> int:
        return sum(1 for r in results if r.get(field) == value)

    n = len(results)
    return {
        "cases": n,
        "completed": sum(1 for r in results if r.get("completed")),
        "correct": count("exact_identification", "correct"),
        "incorrect": count("exact_identification", "incorrect"),
        "abstained": count("exact_identification", "abstained"),
        "not_submitted": count("exact_identification", "not_submitted"),
        "unsupported_certainty": sum(1 for r in results if r.get("unsupported_certainty")),
        "restored": count("functional_recovery", "restored"),
        "not_restored": count("functional_recovery", "not_restored"),
        "not_attempted": count("functional_recovery", "not_attempted"),
        "not_needed": count("functional_recovery", "not_needed"),
        "experiments_mean": round(
            statistics.fmean([r.get("experiments_used", 0) for r in results]), 2
        )
        if n
        else 0.0,
        "infrastructure_failures": sum(r.get("infrastructure_failures", 0) for r in results),
        "errors": sum(1 for r in results if r.get("error")),
    }


def summarize(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_cell: dict[str, Any] = {}
    families = sorted({r["template_id"] for r in results})
    for family in families:
        for condition in CONDITION_ORDER:
            cell = [
                r
                for r in results
                if r["template_id"] == family and r.get("true_condition") == condition
            ]
            if cell:
                by_cell[f"{family}|{condition}"] = _group(cell)
    by_family = {f: _group([r for r in results if r["template_id"] == f]) for f in families}
    steps = sorted({k for r in results for k in r.get("latency_s", {})})
    summary: dict[str, Any] = {
        "totals": _group(results),
        "by_family": by_family,
        "by_family_condition": by_cell,
        "latency": {
            step: latency_stats(
                r["latency_s"][step] for r in results if step in r.get("latency_s", {})
            )
            for step in steps
        },
    }
    agent_runs = [r["agent"] for r in results if r.get("agent")]
    if agent_runs:
        usage_keys = ("model_calls", "tool_calls", "prompt_tokens", "completion_tokens",
                      "total_tokens", "latency_ms")
        summary["agent_usage"] = {
            "runs": len(agent_runs),
            "statuses": {
                s: sum(1 for a in agent_runs if a["status"] == s)
                for s in sorted({a["status"] for a in agent_runs})
            },
            "totals": {k: sum(int(a["usage"].get(k, 0)) for a in agent_runs) for k in usage_keys},
        }
    return summary


def _row(label: str, g: Mapping[str, Any]) -> str:
    return (
        f"| {label} | {g['cases']} | {g['correct']} | {g['incorrect']} | {g['abstained']} | "
        f"{g['not_submitted']} | {g['unsupported_certainty']} | {g['restored']} | "
        f"{g['not_restored']} | {g['not_attempted']} | {g['not_needed']} | "
        f"{g['experiments_mean']:g} | {g['infrastructure_failures']} | {g['errors']} |"
    )


_HEADER = (
    "| Group | Cases | Correct | Incorrect | Abstained | Not submitted | Unsupported "
    "certainty | Restored | Not restored | Repair not attempted | Repair not needed | "
    "Experiments (mean) | Infra failures | Stopped by error |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"
)


def render_markdown(run: Mapping[str, Any]) -> str:
    s = run["summary"]
    lines = [
        f"# Evaluation run `{run['run_id']}`",
        "",
        f"- Runner: **{run['runner']}**"
        + (f" (model `{run['model']}`)" if run.get("model") else ""),
        f"- Case set: `{run['case_set']['name']}` ({run['case_set']['cases']} cases, "
        f"generator `{run['case_set']['generator_version']}`, file sha256 "
        f"`{run['case_set']['sha256'][:16]}…`)",
        f"- Simulator: {run['environment']['simulator']}; host: {run['environment']['platform']}"
        f"; Python {run['environment']['python']}",
        f"- Started {run['started_at']}, finished {run['finished_at']}",
        f"- Protocol: {run['protocol']['description']}",
        "",
        "## Outcomes by family and true condition",
        "",
        _HEADER,
    ]
    for key, group in s["by_family_condition"].items():
        family, condition = key.split("|")
        lines.append(_row(f"{FAMILY_LABELS.get(family, family)} / {condition}", group))
    lines += ["", "## Totals", "", _HEADER]
    for family, group in s["by_family"].items():
        lines.append(_row(FAMILY_LABELS.get(family, family), group))
    lines.append(_row("**All cases**", s["totals"]))
    lines += [
        "",
        "Definitions follow `CaseOutcome`: *correct/incorrect* = a fault or no-fault conclusion "
        "naming / not naming the true cause; *abstained* = insufficient_evidence; *unsupported "
        "certainty* = a definite conclusion whose evidence check was ambiguous or contradicted; "
        "*restored* = the measured repair verification met ±0.5 dB / ±3° on all 31 grid "
        "frequencies; *not needed* = the truth was no fault.",
        "",
        "## Measured latency (wall clock, seconds)",
        "",
        "| Step | n | Mean | Median | p90 | Max |",
        "|---|---|---|---|---|---|",
    ]
    for step, st in s["latency"].items():
        if st:
            lines.append(
                f"| {step} | {st['n']} | {st['mean_s']:.2f} | {st['median_s']:.2f} | "
                f"{st['p90_s']:.2f} | {st['max_s']:.2f} |"
            )
    if "agent_usage" in s:
        u = s["agent_usage"]
        lines += [
            "",
            "## Agent usage (as reported by the provider)",
            "",
            f"- Runs: {u['runs']}; statuses: {u['statuses']}",
            "- Totals: " + ", ".join(f"{k} {v}" for k, v in u["totals"].items()),
            "- Cost: not reported (no verified pricing applied to measured usage).",
        ]
    errors = [r for r in run["results"] if r.get("error")]
    if errors:
        lines += ["", "## Cases stopped by an error", ""]
        lines += [f"- `{r['key']}`: {r['error']}" for r in errors]
    lines += ["", "## Honest limitations", ""]
    lines += [f"- {note}" for note in run.get("notes", HONEST_NOTES)]
    lines.append("")
    return "\n".join(lines)


def write_run(run: dict[str, Any], results_dir: Path) -> tuple[Path, Path]:
    results_dir.mkdir(parents=True, exist_ok=True)
    json_path = results_dir / f"{run['run_id']}.json"
    md_path = results_dir / f"{run['run_id']}.md"
    json_path.write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8", newline="\n")
    md_path.write_text(render_markdown(run), encoding="utf-8", newline="\n")
    return json_path, md_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Re-render the Markdown summary of a run.")
    parser.add_argument("run_json", type=Path)
    args = parser.parse_args(argv)
    run = json.loads(args.run_json.read_text(encoding="utf-8"))
    run["summary"] = summarize(run["results"])
    md_path = args.run_json.with_suffix(".md")
    md_path.write_text(render_markdown(run), encoding="utf-8", newline="\n")
    print(f"wrote {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
