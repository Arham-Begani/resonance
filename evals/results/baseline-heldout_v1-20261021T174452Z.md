# Evaluation run `baseline-heldout_v1-20261021T174452Z`

- Runner: **baseline**
- Case set: `heldout_v1` (24 cases, generator `rc-m2-v1`, file sha256 `c84b0d8996dcebe1…`)
- Simulator: ngspice 47; host: Windows-10-10.0.19045-SP0; Python 3.12.10
- Started 2026-10-21T17:44:52.775431+00:00, finished 2026-10-21T17:46:35.966149+00:00
- Protocol: standardized symptom; Guide-me mode; fixed experiments output_voltage_sweep → source_current_sweep (2 of 3 budgeted); unique remaining hypothesis → that conclusion, else insufficient_evidence with the remaining candidates; nominal repair of the named component verified once; close and reveal.

## Outcomes by family and true condition

| Group | Cases | Correct | Incorrect | Abstained | Not submitted | Unsupported certainty | Restored | Not restored | Repair not attempted | Repair not needed | Experiments (mean) | Infra failures | Stopped by error |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| RC high-pass / no_fault | 2 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 2 | 0 | 0 |
| RC high-pass / r1_increased | 2 | 2 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 2 | 0 | 0 |
| RC high-pass / r1_decreased | 2 | 2 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 2 | 0 | 0 |
| RC high-pass / c1_increased | 2 | 2 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 2 | 0 | 0 |
| RC high-pass / c1_decreased | 2 | 2 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 2 | 0 | 0 |
| RC high-pass / c1_disconnected | 2 | 2 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 2 | 0 | 0 |
| RC low-pass / no_fault | 2 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 2 | 0 | 0 |
| RC low-pass / r1_increased | 2 | 2 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 2 | 0 | 0 |
| RC low-pass / r1_decreased | 2 | 2 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 2 | 0 | 0 |
| RC low-pass / c1_increased | 2 | 2 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 2 | 0 | 0 |
| RC low-pass / c1_decreased | 2 | 2 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 2 | 0 | 0 |
| RC low-pass / c1_disconnected | 2 | 2 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 2 | 0 | 0 |

## Totals

| Group | Cases | Correct | Incorrect | Abstained | Not submitted | Unsupported certainty | Restored | Not restored | Repair not attempted | Repair not needed | Experiments (mean) | Infra failures | Stopped by error |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| RC high-pass | 12 | 12 | 0 | 0 | 0 | 0 | 10 | 0 | 0 | 2 | 2 | 0 | 0 |
| RC low-pass | 12 | 12 | 0 | 0 | 0 | 0 | 10 | 0 | 0 | 2 | 2 | 0 | 0 |
| **All cases** | 24 | 24 | 0 | 0 | 0 | 0 | 20 | 0 | 0 | 4 | 2 | 0 | 0 |

Definitions follow `CaseOutcome`: *correct/incorrect* = a fault or no-fault conclusion naming / not naming the true cause; *abstained* = insufficient_evidence; *unsupported certainty* = a definite conclusion whose evidence check was ambiguous or contradicted; *restored* = the measured repair verification met ±0.5 dB / ±3° on all 31 grid frequencies; *not needed* = the truth was no fault.

## Measured latency (wall clock, seconds)

| Step | n | Mean | Median | p90 | Max |
|---|---|---|---|---|---|
| close | 24 | 0.03 | 0.03 | 0.04 | 0.04 |
| diagnosis | 24 | 0.06 | 0.06 | 0.06 | 0.07 |
| output_voltage_sweep | 24 | 0.33 | 0.22 | 0.70 | 1.23 |
| source_current_sweep | 24 | 0.32 | 0.23 | 0.70 | 0.85 |
| symptom | 24 | 1.98 | 1.82 | 2.70 | 4.06 |
| total | 24 | 4.29 | 3.88 | 6.64 | 8.50 |
| verify_repair | 20 | 1.84 | 1.68 | 2.71 | 2.77 |

## Honest limitations

- Milestone-2 cases are solvable with the standardized symptom plus ONE current measurement: the symptom separates every condition except the two voltage-equivalent pairs (R1×2 vs C1×2 and R1×0.5 vs C1×0.5, identical output voltage), and either current instrument separates those. A perfect score shows that the evidence pipeline, the budget and the repair verification work end to end on these cases; it is not evidence of diagnostic skill and 100 % identification is not an achievement by itself.
- Small sample (one frozen held-out set). No statistical comparison or superiority claim is made between runners.
- Circuits are simulated with ngspice (ideal source, ideal R and C, no load, parasitics or component tolerances). Results are simulations, not physical measurements.
- Latencies are wall-clock times on the machine that ran the evaluation, including the ngspice subprocesses; they are not a service-level benchmark.
