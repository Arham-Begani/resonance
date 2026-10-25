# Evaluation run `nemotron-heldout_v1-20261025T115418Z`

- Runner: **nemotron** (model `nvidia/nemotron-3-super-120b-a12b`)
- Case set: `heldout_v1` (24 cases, generator `rc-m2-v1`, file sha256 `c84b0d8996dcebe1…`)
- Simulator: ngspice 47; host: Windows-10-10.0.19045-SP0; Python 3.12.10
- Started 2026-10-25T11:54:18.216124+00:00, finished 2026-10-25T12:05:40.496134+00:00
- Protocol: standardized symptom; one 'Investigate for me' Nemotron run (real Nebius inference, budget 3, policy caps); the agent's own repair proposal (if any) verified once by the harness; close and reveal.

## Outcomes by family and true condition

| Group | Cases | Correct | Incorrect | Abstained | Not submitted | Unsupported certainty | Restored | Not restored | Repair not attempted | Repair not needed | Experiments (mean) | Infra failures | Stopped by error |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| RC high-pass / no_fault | 2 | 1 | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 2 | 1.5 | 0 | 0 |
| RC high-pass / r1_increased | 2 | 1 | 0 | 0 | 1 | 0 | 0 | 0 | 2 | 0 | 1.5 | 0 | 0 |
| RC high-pass / r1_decreased | 2 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 1 | 0 | 0 |
| RC high-pass / c1_increased | 2 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 1 | 0 | 0 |
| RC high-pass / c1_decreased | 2 | 2 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 2 | 0 | 0 |
| RC high-pass / c1_disconnected | 2 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 1.5 | 0 | 0 |
| RC low-pass / no_fault | 2 | 1 | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 2 | 1.5 | 0 | 0 |
| RC low-pass / r1_increased | 2 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 1.5 | 0 | 0 |
| RC low-pass / r1_decreased | 2 | 1 | 1 | 0 | 0 | 1 | 1 | 0 | 1 | 0 | 2 | 0 | 0 |
| RC low-pass / c1_increased | 2 | 2 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 1 | 0 | 0 |
| RC low-pass / c1_decreased | 2 | 2 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 1.5 | 0 | 0 |
| RC low-pass / c1_disconnected | 2 | 2 | 0 | 0 | 0 | 0 | 2 | 0 | 0 | 0 | 1 | 0 | 0 |

## Totals

| Group | Cases | Correct | Incorrect | Abstained | Not submitted | Unsupported certainty | Restored | Not restored | Repair not attempted | Repair not needed | Experiments (mean) | Infra failures | Stopped by error |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| RC high-pass | 12 | 10 | 0 | 0 | 2 | 0 | 2 | 0 | 8 | 2 | 1.42 | 0 | 0 |
| RC low-pass | 12 | 10 | 1 | 0 | 1 | 1 | 7 | 0 | 3 | 2 | 1.42 | 0 | 0 |
| **All cases** | 24 | 20 | 1 | 0 | 3 | 1 | 9 | 0 | 11 | 4 | 1.42 | 0 | 0 |

Definitions follow `CaseOutcome`: *correct/incorrect* = a fault or no-fault conclusion naming / not naming the true cause; *abstained* = insufficient_evidence; *unsupported certainty* = a definite conclusion whose evidence check was ambiguous or contradicted; *restored* = the measured repair verification met ±0.5 dB / ±3° on all 31 grid frequencies; *not needed* = the truth was no fault.

## Measured latency (wall clock, seconds)

| Step | n | Mean | Median | p90 | Max |
|---|---|---|---|---|---|
| agent_run | 24 | 20.48 | 18.82 | 30.38 | 35.22 |
| close | 24 | 0.04 | 0.03 | 0.04 | 0.16 |
| symptom | 24 | 5.52 | 4.07 | 12.45 | 13.88 |
| total | 24 | 28.41 | 24.53 | 48.70 | 51.16 |
| verify_repair | 9 | 6.26 | 4.64 | 19.34 | 19.34 |

## Agent usage (as reported by the provider)

- Runs: 24; statuses: {'completed': 24}
- Totals: model_calls 140, tool_calls 116, prompt_tokens 785254, completion_tokens 40494, total_tokens 825748, latency_ms 432335
- Cost: not reported (no verified pricing applied to measured usage).

## Honest limitations

- Milestone-2 cases are solvable with the standardized symptom plus ONE current measurement: the symptom separates every condition except the two voltage-equivalent pairs (R1×2 vs C1×2 and R1×0.5 vs C1×0.5, identical output voltage), and either current instrument separates those. A perfect score shows that the evidence pipeline, the budget and the repair verification work end to end on these cases; it is not evidence of diagnostic skill and 100 % identification is not an achievement by itself.
- Small sample (one frozen held-out set). No statistical comparison or superiority claim is made between runners.
- Circuits are simulated with ngspice (ideal source, ideal R and C, no load, parasitics or component tolerances). Results are simulations, not physical measurements.
- Latencies are wall-clock times on the machine that ran the evaluation, including the ngspice subprocesses; they are not a service-level benchmark.
