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
| Public repository with license and third-party notices | **Done**: https://github.com/Arham-Begani/resonance (MIT `LICENSE`, `THIRD_PARTY_NOTICES.md`); CI green |
| Nemotron via Nebius Token Factory | **Live verified 2026-10-25**: model listed, tool-call round trip 1.61 s, two full investigate runs (see `docs/ai-integration.md`) |
| Hosted app | **Deployed**: https://resonance-lwdf.onrender.com (Render, single Docker instance). Smoke test 2026-10-25: health OK with ngspice 44.2 and AI ready; a real experiment in 3.3 s; a case created with no reveal |
| Demo video (< 3 min) | **Recorded 2026-10-28** (118 s, 1440×892, H.264/AAC): real interactions on the live site with live Nemotron inference (the AI run took 21 s; the hidden fault was C1 at half value, identified correctly and repaired; behaviour restored). Captions are on screen; the audio is the actual clips the app played, at one shared gain. Reproduce with `apps/web/scripts/demo/` (see below). **Upload to YouTube is the owner's step** |
| Evaluation results | **Measured** on `heldout_v1` (24 cases): baseline 24/24 correct, 20/20 restored, 4.29 s/case. Nemotron 20/24 correct, 1 incorrect, 3 not submitted, 9/20 restored (11 repairs not proposed), 28.41 s/case, 825,748 tokens |
| Browser e2e suite | 32/32 at milestone 1; specs updated for milestone 2 but the full re-run is pending (host memory) |
| Submission text | Draft below |

### Owner steps before submitting

1. Put `NEBIUS_API_KEY` in `.env`, then run the live checks from `services/api`:
   - `RESONANCE_LIVE_AI=1 uv run pytest tests/agent/test_agent_live.py -s -m live_ai`
   - `uv run python ../../evals/run_nemotron.py --set dev_v1 --limit 2`
   - then the held-out set.
2. Deploy on Render: **New → Blueprint** → this repository → enter `NEBIUS_API_KEY`. Open the
   URL in a fresh browser and run one fresh mystery.
3. Record the demo following the script below, using live inference only.
4. Fill in the hosted URL and the measured Nemotron numbers. Re-check the official rules on
   Devpost, then submit.

## Recording the demo

`apps/web/scripts/demo/record.mjs` drives the deployed app with Playwright and records a CDP
screencast. It also captures every audio clip the app actually plays. The page draws a tiny
time barcode on every frame, and `build.py` (Python with numpy and Pillow, plus ffmpeg) uses it
to time each frame and place each clip, then crops the barcode out. Nothing on the server is
scripted: each take is a fresh session, a fresh random mystery and live inference, so takes
differ.

```bash
cd apps/web
BASE=https://resonance-lwdf.onrender.com OUT=demo-take node scripts/demo/record.mjs
python scripts/demo/build.py demo-take resonance-demo.mp4
```

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
learners edit RC low-pass and high-pass filters, predict and test changes in Learn mode, run real ngspice simulations, hear input and output at one honest
listening level, and investigate hidden faults with an NVIDIA Nemotron lab partner served by
Nebius Token Factory. The AI cannot see the fault: it must choose measurements within a budget,
cite the evidence, and admit ambiguity (doubling R and doubling C sound identical; only the
source current tells them apart). Trusted code runs the simulator, enforces budgets, evaluates
hypotheses against declared tolerances and verifies repairs; the hidden state lives in an
isolated evaluator. Built with React/TypeScript, FastAPI, NumPy and ngspice.
On a frozen held-out set of 24 cases across both filter families and six conditions, a
fixed-experiment baseline identified 24/24 and restored 20/20 faulty circuits. These cases need
only one current measurement after the symptom, so this shows that the pipeline works, not
diagnostic skill.
On the same cases, Nemotron (`nvidia/nemotron-3-super-120b-a12b` on Nebius Token Factory)
identified 20/24 using 1.42 experiments on average. It made one unsupported claim, and in
three runs it stopped before diagnosing. It verified a repair in 9 of the 20 faulty cases.
It never proposed a repair in the other 11, and no repair it attempted failed. Try it:
https://resonance-lwdf.onrender.com
