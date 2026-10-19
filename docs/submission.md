# Submission checklist and demo script

Target: Nebius x NVIDIA Global AI Hackathon — Best Apps and Agents. Deadline: **30 October 2026,
10:00 a.m. PDT** (aim to ship 29 October).

## What the rules require (verify on the official page before submitting)

The official rules page (https://nebiusglobalaihackathon.devpost.com/rules) blocks automated
access (HTTP 403), so these points come from secondary listings and must be re-checked by a
person on Devpost before submission:

- Projects must run on **Nebius Token Factory** (or Nebius AI Cloud) and use at least one
  **NVIDIA open model** (Nemotron). Sources: startupgrantsindia.com, a Luma event listing.
- Judging: Technological Implementation, Design, Potential Impact, Quality of the Idea, equally
  weighted. Source: lucaberton.com summary.
- Solo and team entries allowed; legal age in the country of residence.
- Not confirmed from any source: demo-video length/platform, required text fields, hosted-app
  access rules. CLAUDE.md asks for a public YouTube demo under three minutes, a public
  repository with an open-source license, and a hosted app kept available for judging.

## Status of each deliverable

| Deliverable | Status |
|---|---|
| Public repository with license and third-party notices | Code, MIT `LICENSE` and `THIRD_PARTY_NOTICES.md` exist locally; **no remote yet** (needs the owner's GitHub account) |
| Nemotron via Nebius Token Factory | Integration built and tested with scripted responses; **live verification blocked on `NEBIUS_API_KEY`** |
| Hosted app | **Not deployed**: needs a hosting account/decision; the Dockerfile is untested (no Docker on the dev host) |
| Demo video (< 3 min) | Script below; **not recorded** (needs a human narrator and real Nemotron runs) |
| Evaluation results | Baseline harness and held-out set (milestone 2); Nemotron comparison blocked on the key |
| Submission text | Draft below |

## Demo script (~150 s, actual interactions only)

| Time | Show | Notes |
|---|---|---|
| 0–15 s | Open the workbench: the opening experiment is ready. Press Play: the 440 Hz tone with the 4 kHz whistle; point at the two peaks and the tone levels. | Nothing autoplays. |
| 15–40 s | Type the goal or press "Suggest values for this goal"; show the analytical preview labeled "Prediction"; apply it; run the measurement (real ngspice, show the latency). | Goal turns from "not met" to "met" on measured numbers. |
| 40–55 s | Compare Previous vs Current: play both at the same listening level; show the measured dB change. | Shared gain, no normalization. |
| 55–65 s | Start a mystery (fresh case; family chosen or "Surprise me"). Hear Expected vs Observed. | Hidden values never shown. |
| 65–110 s | "Investigate for me" with Nemotron: real tool calls stream; the voltage sweep leaves R-vs-C ambiguous; the agent chooses source current; evidence cards cite observation IDs; diagnosis. | If the live call fails, use Guide me and say so — never present a replay as fresh inference. |
| 110–130 s | Apply the nominal-part repair and verify: measured grid within ±0.5 dB, Expected vs Repaired audio. Close and reveal: identification and recovery reported separately. | |
| 130–150 s | Keyboard-only control and screen-reader-friendly summaries; muted completion; the evaluation table (baseline vs Nemotron with measured latency). | State limitations plainly. |

## Draft submission text

**Resonance — hear what a circuit does, discover why it fails.** A browser electronics lab where
learners edit RC filters, run real ngspice simulations, hear input and output at one honest
listening level, and investigate hidden faults with an NVIDIA Nemotron lab partner served by
Nebius Token Factory. The AI cannot see the fault: it must choose measurements within a budget,
cite the evidence, and admit ambiguity (doubling R and doubling C sound identical; only the
source current tells them apart). Trusted code runs the simulator, enforces budgets, evaluates
hypotheses against declared tolerances and verifies repairs; the hidden state lives in an
isolated evaluator. Built with React/TypeScript, FastAPI, NumPy and ngspice.
*(Fill in measured evaluation numbers and the hosted URL before submitting; do not claim
results that were not measured.)*
