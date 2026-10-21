# Resonance evaluation harness

Private, in-process evaluation of hidden-fault investigations. It creates cases from frozen
seeds through `CaseService.harness_create_case` (never exposed over HTTP), runs them on real
ngspice, and scores them from the case reveal after closing. Neither runner reads hidden state.

## Commands (run from `services/api`)

```
cd services/api

# Check that the frozen case sets still match the generator (or write missing ones)
uv run python ../../evals/build_cases.py --check
uv run python ../../evals/build_cases.py --write

# Fixed-experiment baseline, real ngspice, no credentials needed
uv run python ../../evals/run_baseline.py                      # held-out set, 24 cases
uv run python ../../evals/run_baseline.py --set dev_v1         # development set, 12 cases
uv run python ../../evals/run_baseline.py --limit 2            # quick check

# Nemotron agent, real ngspice + real Nebius inference (needs NEBIUS_API_KEY, NEBIUS_MODEL)
uv run python ../../evals/run_nemotron.py --set dev_v1 --limit 2
uv run python ../../evals/run_nemotron.py                      # held-out set

# Re-render a Markdown summary from a run file
uv run python ../../evals/report.py ../../evals/results/<run>.json

# Harness tests (frozen-set integrity, held-out disjointness, smoke run, refusal)
uv run pytest tests/challenges/test_evals_harness.py
```

`run_nemotron.py` refuses to run when the credentials are missing or the development fixture
mode is on: it prints why, writes nothing, and exits with status 2. There is no scripted
stand-in, and it rejects any run whose `AgentRun.source` is not `nebius`.

## Case sets (`cases/`)

| Set | Cases | Nominal designs |
|---|---|---|
| `heldout_v1` | 24 = 2 families × 6 conditions × 2 | `casesets.HELDOUT_DESIGNS`: six per family, spread over the cutoff range |
| `dev_v1` | 12 = 2 families × 6 conditions × 1 | `casesets.DEV_DESIGNS`: 2.2 kΩ / 47 nF and 1 kΩ / 100 nF (designs used in tests) |

The conditions are `no_fault`, `r1_increased`, `r1_decreased`, `c1_increased`, `c1_decreased`
and `c1_disconnected`.

Held-out nominal designs:

| Family | R1 / C1 (approximate cutoff) |
|---|---|
| RC low-pass | 6.8 kΩ / 220 nF (106 Hz); 4.7 kΩ / 150 nF (226 Hz); 680 Ω / 680 nF (344 Hz); 1.5 kΩ / 150 nF (707 Hz); 4.7 kΩ / 22 nF (1.54 kHz); 3.3 kΩ / 15 nF (3.22 kHz) |
| RC high-pass | 4.7 kΩ / 680 nF (49.8 Hz); 10 kΩ / 220 nF (72 Hz); 2.2 kΩ / 330 nF (219 Hz); 10 kΩ / 33 nF (482 Hz); 680 Ω / 330 nF (709 Hz); 6.8 kΩ / 15 nF (1.56 kHz) |

Within a family, condition *i* uses designs *i* and *(i + 3) mod 6*. Every held-out design is
drawable by the hosted generator. It is also disjoint from every development fixture.
`test_evals_harness.py` checks this by scanning every test file in `tests/challenges`,
`tests/verification` and `tests/agent`; a design counts as used if its R and its C both appear
as literals in the same file. The computed extreme-cutoff designs are excluded too. Exhaustive
property tests over every drawable design, such as the generator rules and the identifiability
proofs, are not fixtures and are not excluded.

Seeds are opaque (`sha256("resonance-eval|<set>|<family>|<index>|<attempt>")[:32]`). Each file
records the generator version and the expected condition and design. `casesets.load` refuses a
set whose seeds no longer reproduce those cases. After a generator change, build a new version
of the set (for example `heldout_v2`); never edit a frozen file.

## Protocol (identical for both runners)

1. Create the case from its seed; the standardized symptom is recorded (uncounted).
2. Investigate.
   - **Baseline**: Guide-me mode. Run `output_voltage_sweep`, then `source_current_sweep` (2 of
     the 3 budgeted experiments). Decide deterministically from the recorded evidence: if
     exactly one hypothesis remains, conclude it (fault or no fault) and cite both sweeps;
     otherwise conclude `insufficient_evidence` and list the remaining candidates.
   - **Nemotron**: one "Investigate for me" run through `AgentService`, with the same budget,
     tolerances and caps as the app.
3. If a repair was proposed, the harness verifies it once. The baseline proposes the nominal
   part for the component it named.
4. Close, then record the `CaseOutcome` from the reveal. Each record holds exact
   identification, abstention, unsupported certainty, functional recovery, experiments used,
   infrastructure failures and the measured wall-clock latency of each step. Nemotron records
   also hold the provider-reported usage.

Case state lives in a scratch data directory that is deleted after the run.

## Results (`results/`)

Each run writes `<runner>-<set>-<UTC time>.json`, containing every per-case record, the
protocol, the environment and the case-set hash, and a Markdown summary next to it. The summary
has a table per family and condition, totals, and latency statistics.

Baseline on `heldout_v1`, real ngspice 47: `results/baseline-heldout_v1-20261021T174452Z.md`.
24/24 cases were identified correctly, with 0 abstentions and 0 unsupported certainty. 20/20
fault repairs were restored; the 4 no-fault cases needed none. Each case used 2 experiments.
There were 0 infrastructure failures. The mean wall time was 4.3 s per case (median 3.9 s,
max 8.5 s) on a shared 4-core Windows host. An earlier identical-outcome run on a less loaded
host measured 2.2 s.

No Nemotron run has been made yet: this machine has no `NEBIUS_API_KEY`.

## Honest reading of the numbers

- These cases can be solved with the standardized symptom plus **one** current measurement.
  The symptom separates every condition except the voltage-equivalent pairs (R1×k vs C1×k).
  Either current instrument separates those pairs. A perfect baseline score shows that the
  evidence pipeline, budgets and repair verification work end to end. It is not a measure of
  diagnostic skill.
- With 24 cases, no claim of statistical superiority is made for either runner.
- The results come from ngspice simulations of ideal components, not physical measurements.
- Costs are reported only from measured usage with verified pricing; none is applied here.
