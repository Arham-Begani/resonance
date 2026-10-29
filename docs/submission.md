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
| Hosted app | **Deployed**: https://resonance-circuit-lab.vercel.app (Vercel: static app plus `/api` proxy) backed by https://resonance-lwdf.onrender.com (Render: API, ngspice, Nemotron). Smoke-tested through the proxy: a real experiment ran in 1.3 s, and the session cookie works on the Vercel domain |
| Demo video (< 3 min) | **Recorded 2026-10-29**: 2 min 3 s, 1920×1080, 60 fps, H.264/AAC (`resonance-demo.mp4`, not committed). Real interactions on the Vercel site with live Nemotron inference. The hidden fault was a disconnected C1; Nemotron diagnosed it from the symptom (0 of 3 experiments) in 13 s, and the repair was verified ("Behaviour restored"). App content was captured at about 20 frames/s and the cursor is drawn at 60 fps. One overlay caption written by the recorder ("…run an experiment…") was corrected in post, because no experiment was run in this take; app content is unedited. **Upload to YouTube is the owner's step** |
| Evaluation results | **Measured** on `heldout_v1` (24 cases). Baseline: 24/24 correct, 20/20 restored, 4.29 s/case. Nemotron run 1 (25 Oct): 20/24 correct, 1 incorrect, 3 not submitted, 9/20 restored, 28.41 s/case. Nemotron run 2 (28 Oct, after the completion-reminder change, so not a fresh held-out measurement): 21/24 correct, 0 incorrect, 3 honest abstentions, 17/20 restored, 19.85 s/case, 940,583 tokens |
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

## Devpost fields (ready to paste)

**Project name:** Resonance

**Elevator pitch** (at most 200 characters):
Hear what a circuit does, then find out why it fails: a browser electronics lab with real
ngspice simulation and an NVIDIA Nemotron lab partner that has to measure, not guess.

**Track:** Best Apps and Agents

**Try it out:**
- https://resonance-circuit-lab.vercel.app
- https://github.com/Arham-Begani/resonance

**Video:** the YouTube link, once uploaded (public; the 2 min 3 s 1080p60 take).

**Built with:** react, typescript, vite, d3, python, fastapi, pydantic, numpy, ngspice, sqlite,
nvidia-nemotron, nebius-token-factory, openai-python, docker, render, vercel, playwright

**Was this project started before the hackathon?** No. It was built during the hackathon:
work began on 15 October 2026, and the public commit history runs from 19 October.

### About the project

#### Inspiration
Electronics is usually taught with equations and graphs, but people experience circuits with
their ears. Two things kept me interested. First, a low-pass filter is something you can *hear*
removing a whistle from a tone. Second, the most important diagnostic lesson for a first-order
RC filter is an *ambiguity*: doubling the resistor and doubling the capacitor produce exactly
the same output, H(jω) = 1/(1 + jωRC). I wanted a lab where a learner, and an AI, have to
discover that by measuring, not by being told.

#### What it does
One workbench, three modes, sharing the same schematic, component list, instruments and
experiment history:
- **Explore:** edit an RC low-pass or high-pass filter with explicit SI units and run a real
  ngspice AC sweep and two-tone transient. Hear input and output at one shared listening level
  (never normalized), check a measured goal, preview an analytical suggestion (labeled as a
  prediction), apply it, measure it, compare and undo.
- **Learn:** predict whether a change makes each tone louder or quieter, run it, and get graded
  on the *measured* change.
- **Investigate:** a hidden single fault (R or C drifted ×2 or ×0.5, a disconnected capacitor,
  or no fault). You get one standard symptom and up to three bounded experiments.
  - Choose **Guide me** (you measure, with hints) or **Investigate for me**, where Nemotron
    chooses its own experiments through tool calls, assesses the evidence, submits a
    diagnosis that cites recorded observations (or says the evidence is not enough), and
    proposes a repair.
  - The repair is verified numerically across 31 frequencies, then the case is revealed.
    Exact identification and functional recovery are reported separately.

#### How we built it
- **Simulation:** a FastAPI + Pydantic backend compiles a typed circuit graph into trusted
  netlists and runs ngspice as an isolated subprocess per job. Results are checked against
  analytical RC equations within 1 % / 1°. Audio is resampled from the transient onto a
  uniform 48 kHz grid with one shared gain, and tone amplitudes are validated against AC
  within 0.5 dB.
- **Hidden state:** it lives in a separate evaluator with its own database. The agent and the
  browser only ever see sanitized observations, and values are quantized so they cannot be
  fingerprinted.
- **The agent:** NVIDIA Nemotron (`nvidia/nemotron-3-super-120b-a12b`) on Nebius Token
  Factory, through the OpenAI-compatible API, in a small explicit tool loop. It has separate
  tool sets for Explore, Investigate and hints, and caps on model calls, tool calls, tokens
  and wall time. Deterministic code executes every tool and decides which explanations are
  supported, contradicted or unresolved. The model never sees the answer.
- **Frontend:** React + TypeScript + Vite with types generated from the OpenAPI schema,
  accessible SVG charts with data tables and text summaries, and full keyboard operation.
- **Deployment:** Vercel serves the app and proxies the API to a Docker service on Render
  that runs ngspice.

#### Challenges we ran into
- **Honest audio:** getting sound that is honest, not just pleasant. Every clip shares one
  gain, so a quieter whistle really is the filter's attenuation.
- **Leaking the answer:** keeping the hidden fault from leaking through floating-point
  precision, error text, caches, or the model's context.
- **Agent completion:** in the first held-out run, Nemotron sometimes ended a run without
  submitting a diagnosis or proposing a repair. The prompt now requires both, and the loop
  sends at most one fixed reminder that never hints at the answer.

#### Accomplishments that we're proud of
- **Evaluation:** a frozen held-out set of 24 cases, used to compare Nemotron with a
  fixed-experiment baseline under the same symptom, budget and tolerances.
  - Baseline: 24/24 correct, 20/20 repairs verified.
  - Nemotron run 1: 20/24 correct, 1 unsupported claim, 9/20 repairs verified.
  - Nemotron run 2, after the completion change: 21/24 correct, 0 wrong, 3 honest
    abstentions (the true cause was among the candidates every time), 17/20 repairs
    verified.
  - Run 2 follows a change made after run 1 on these cases, so it is not a fresh held-out
    measurement. These cases are easy (one current measurement resolves them), so the
    baseline does better, and that is what we report.
- **Tests:** 734 backend tests with real ngspice, plus browser end-to-end, keyboard and axe
  WCAG checks.

#### What we learned
- Constraining an agent with deterministic evidence checks makes its claims checkable.
- An honest "not enough evidence" is a feature, not a failure.

#### What's next
- More circuit families: second-order RLC and an op-amp buffer.
- Fault cases that need a deliberate intervention, not only more measurements.
- Sessions with real beginners and assistive-technology users. Accessibility has been checked
  with automated tools and keyboard tests, not yet with screen-reader users.

### Feedback on Nebius and NVIDIA tools
These are our observations from building and evaluating Resonance:
- **Getting started:** the Token Factory cookbook made setup quick. With the
  OpenAI-compatible endpoint we used the standard `openai` Python client unchanged, and
  `/v1/models` listed the five Nemotron models we needed to choose from.
- **Tool calls** with `nvidia/nemotron-3-super-120b-a12b` were reliable:
  - all 48 held-out investigation runs completed without a provider or tool-loop error, and
    every tool call in our live verification runs succeeded;
  - `parallel_tool_calls=false` was accepted;
  - reasoning came back in separate `reasoning`/`reasoning_content` fields, not as `<think>`
    tags in the content.
- **Latency and caching:** about 1.6 s for a single tool-call turn and 11–30 s for a full
  investigation (7–9 model calls). Prompt caching was reported in usage, which helped with
  our repeated system context.
- **What we had to handle ourselves:**
  - the model occasionally ended a run with a final message instead of a required tool call;
  - a full investigation costs about 40k tokens, because each turn resends the conversation.
  Clearer guidance on enforcing a required final tool call (for example a documented
  `tool_choice` behaviour for Nemotron) would help agent builders.

## Short description (older draft)

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
identified 21/24 with 1.46 experiments on average, made no wrong or unsupported claims, and
abstained honestly in 3 cases. It verified a repair in 17 of the 20 faulty circuits. This is
the second run on these cases, after the agent was changed to always finish with a diagnosis
and a repair proposal; the first run scored 20/24 and 9/20.
Try it:
https://resonance-circuit-lab.vercel.app
