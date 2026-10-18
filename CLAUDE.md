# RESONANCE — project instructions

## Mission and working agreement

Build Resonance: a software electronics learning lab where people hear circuit behavior, choose experiments, and diagnose hidden faults alongside an AI. Read `idea.md` for the final user experience; this file governs implementation and scope.
- Deliver a working, attractive browser application with genuine simulation and NVIDIA Nemotron inference through Nebius Token Factory. No physical electronics or local GPU is required.
- The target is the Nebius x NVIDIA Global AI Hackathon, Best Apps and Agents. Submission deadline: October 30, 2026, 10:00 a.m. PDT / 10:30 p.m. IST. Aim to ship October 29. Recheck official requirements before submission.
- Work incrementally from a functioning simulation to a complete product. Make routine implementation choices autonomously, explain consequential changes, and preserve existing work. Ask only for missing credentials, a real blocker, or a material scope change.
- Do not use, install, or consult the hackathon-ideator skill. Do not restart ideation or replace this concept with a different project.
- Distinguish implemented, tested, and proposed work. Never fabricate measurements, inference, test results, user feedback, or prize prospects. A replay must be labeled as a replay.
- Inspect the repository and environment before choosing versions or installing dependencies. Record actual working commands in README; do not claim commands below already exist.

## Development delegation and quality

- Use parallel development subagents for independent tasks. All delegated work must use an available Claude Opus model, explicitly selected through supported model configuration. Verify the resolved model when the runtime exposes it; do not claim verification based only on a requested alias.
- Use custom project agents with `model: opus` where supported, or an explicitly available Opus model ID. Check for overrides and fallbacks. Do not silently delegate to Sonnet, Haiku, or another model family. If Opus delegation is unavailable, report the limitation and continue suitable work in the main session without misrepresenting the model.
- The main agent owns architecture, shared schemas, integration, and release acceptance. Delegate simulation, audio, interface, agent/evaluator implementation, and independent verification with explicit ownership and dependencies. Parallelize only ready, independent tasks; respect actual concurrency limits.
- Each assignment includes required context, owned paths, contracts, acceptance checks, prohibited scope expansion, and a concrete handoff. Workers must not overwrite another worker's files or change shared contracts without coordination. Reuse workers for follow-ups; no uncontrolled recursive delegation.
- Development subagents are coding assistants. The shipped app still uses its specified Nemotron agent through Nebius; do not substitute Opus into the product runtime.
- Never compromise on correctness, scientific honesty, hidden-state isolation, accessibility fundamentals, maintainability, or verified integration to finish faster. Save time through focused scope, parallel independent work, and early integration. Do not disable tests, weaken thresholds to hide bugs, or ship decorative placeholders as working functionality.
- Quality means satisfying concrete acceptance criteria, not endless rewrites. Fix material review findings and rerun relevant checks; stop optional polishing when the milestone is verified. Document blockers and unfinished requirements accurately.

## Product scope: commit to this

1. **Explore:** choose a low-pass or high-pass RC circuit; describe a supported filtering goal; preview a proposed change; simulate; compare sound and measured response; undo.
2. **Learn:** predict the effect of a component change, run the experiment, and explain the observed tradeoff with optional hints.
3. **Investigate:** start a hidden single-fault case; choose Guide me or Investigate for me; use up to three bounded experiments; submit a diagnosis or acknowledge unresolved ambiguity; verify a repair.
4. These are states of one workbench, with a shared schematic, instruments, component list, and experiment history. Avoid three independent application shells.
- Circuit families: first-order RC low-pass and RC high-pass, with an ideal voltage source, one resistor, one capacitor, and named input/output/ground nodes. Use tested template layouts and editable values, not arbitrary wiring.
- Supported mystery faults: resistor value changed, capacitor value changed, or capacitor disconnected. Include no-fault controls. Start drift cases with 0.5x and 2x multipliers. Add a fault only after proving it is simulatable and identifiable using allowed observations.
- One hidden fault at most. No simultaneous faults, arbitrary netlist import, microcontrollers, transistors, PCB layout, component marketplace, account system, collaboration, or decorative multi-agent architecture.
- Typed interaction is complete. Voice input and read-aloud are optional enhancements after the core works; lack of microphone support must never block a task.
- Render short audio clips after simulation. Continuous live SPICE audio is out of scope. No arbitrary uploaded music or speech is needed; use generated signals.

## Default stack and repository

- Frontend: React, TypeScript, Vite, CSS variables, semantic HTML, SVG circuit templates, and a small plotting library selected for accessible integration. Avoid a full schematic-editor dependency.
- Backend: Python, FastAPI, Pydantic, NumPy, and an isolated ngspice subprocess. Use a small explicit agent loop and an OpenAI-compatible client for Nebius; do not introduce an agent framework unless a concrete need appears.
- Keep frontend types synchronized with backend schemas, preferably through OpenAPI generation. Validate all requests on the backend.
- Suggested layout: `apps/web/`, `services/api/`, `services/api/resonance/{circuits,simulation,audio,agent,challenges}/`, `tests/`, `evals/`, `docs/`.
- Store anonymous session state and experiment metadata in SQLite; keep generated artifacts in a bounded local directory. Avoid Redis or distributed infrastructure for this version.
- Support local development on the available host and a reproducible Linux container with ngspice for deployment. Prefer one deployment serving the frontend and API. Document a native ngspice path option if Docker is unavailable.
- Environment variables: `NEBIUS_API_KEY`, `NEBIUS_BASE_URL`, `NEBIUS_MODEL`, `NGSPICE_BIN`. Keep secrets server-side, ignored by Git, and out of logs. Supply `.env.example` with placeholders.
- Confirm the current Nebius endpoint and available Nemotron model with official documentation/account access. Smoke-test structured tool calls; pin the working model identifier in deployment configuration. No guessed model IDs or silent provider substitutions.

## Domain model and simulation contract

- Use a typed circuit graph with stable component/node IDs, canonical SI values, template ID, source specification, and immutable revision IDs. Parse user units explicitly; reject ambiguous or invalid values.
- Trusted code compiles graphs and approved experiment configurations to netlists. Never execute model-authored netlists, `.control` scripts, filesystem paths, shell commands, or includes.
- Define allowed component/value/frequency ranges from validated examples. Limit circuit size, sweep points, transient duration, subprocess time, output size, and concurrency. Run subprocesses with argument arrays in isolated per-job directories and clean them up.
- Return structured success/failure results. An experiment record contains revision, experiment ID, settings, units, actual measurements, simulator version, execution status, and artifact references. Backend failure is not a diagnosis.
- Implement AC magnitude/phase and transient voltage/current measurements using ngspice. Keep analytical RC equations as independent checks and hypothesis predictions, never as a secretly substituted simulation engine.
- Goal example: preserve a 440 Hz tone with at most 1 dB attenuation and reduce a 4 kHz tone by at least 12 dB. These are requested constraints, not preclaimed results. Evaluate constraints against measured gain. If a requested target is infeasible for the supported family, explain and offer a measured tradeoff.
- Keep source and load assumptions explicit. Do not describe simulated results as physical measurements or imply that parasitics and component tolerances are modeled when they are not.

## Audio correctness

- Generate a repeatable two-tone input, initially 440 Hz + 4 kHz. Add a high-pass example with low-frequency interference only after the first path works.
- Produce playable PCM from ngspice transient output. Adaptive time samples must be resampled to a uniform audio grid. Choose timestep, settling period, sample rate, and supported signal bandwidth deliberately; validate tone amplitudes against AC results.
- Keep a shared, fixed voltage-to-playback gain and common user volume across each input/output/before/after comparison. Do not independently normalize clips or silently compress away attenuation.
- Limit levels and detect clipping. If reducing gain is necessary, apply the same adjustment to the entire comparison and disclose it. Use short matched fades to prevent playback clicks without changing the measured steady-state analysis.
- Playback is user initiated and stoppable. Never overlap clips or force circuit audio to compete with screen-reader speech. Distinguish actual signal playback from any future pitch-based sonification.
- Charts and numerical results must communicate the result even when muted. Provide time-domain and frequency-response views; spectra of the demo tones may be derived from simulated samples.

## Hidden-fault investigation: the central engineering requirement

- Maintain separate intended circuit, hidden physical-model state, and sanitized observation history. The agent sees the intended schematic and nominal labels, never the hidden values or actual netlist.
- Keep hidden state in an evaluator module/service outside the agent's callable tool surface. The agent gets no file, code execution, general HTTP, or database tools. Apply the boundary to all browser responses, error text, artifact metadata, caches, and logs exposed through the UI.
- Give cases opaque IDs. Keep random seeds and fault manifests private until the investigation is closed. A public seed that reveals a reproducible fault is an answer leak. Publish seeds afterward for reproducibility.
- Circuit edits, broad simulation, reference comparisons, and audio requests must not bypass the mystery's observation policy. Freeze the intended revision during a case. Use a different permitted tool set for Explore and Investigate.
- Before investigation, supply one standardized symptom observation to both agent and baseline. Count subsequent experiments equally. A frequency sweep counts as one experiment but has a fixed declared frequency range and point cap; it is not unlimited access to all nodes and currents.
- Initial allowed experiments: output voltage gain/phase sweep; source-current magnitude/phase sweep; bounded transient measurement at an allowed node. Specify frequency/sample limits and quantities returned in schemas. Do not return extra hidden vectors from ngspice.
- Three successful experiments is the default challenge budget. Validation errors do not consume it; permit one retry for a transient infrastructure failure, then stop and mark the run failed. Retries, total tool calls, model tokens, and wall time all have separate caps.
- State machine: ready → observing → diagnosis submitted / unresolved → repair verification → closed. Cancellation is available during inference and simulation. Reveal the answer only after the agent loop is stopped and the case is closed; further investigation of that revealed case is a labeled replay.
- **Identifiability rule:** for an ideal RC low-pass, H(jw)=1/(1+jwRC). Doubling R and doubling C produce identical voltage transfer responses. Additional output-only frequencies cannot identify which changed. Current/impedance information or an approved intervention is needed. High-pass voltage response also depends on RC.
- Represent hypotheses as supported, contradicted, or unresolved according to recorded observations and declared tolerances. Never invent likelihood percentages. Missing data cannot eliminate a hypothesis. Display a short evidence summary, not an imitation of private model reasoning.
- Allow `insufficient_evidence` and `no_fault`. If two causes remain indistinguishable, do not force a single diagnosis. Explain which measurement would help, even if the budget is exhausted.
- Submit a diagnosis once before repair. Apply only the proposed component correction and verify against the intended reference. Do not replace the entire hidden circuit with the reference. Exact fault identification and functional recovery are separate outcomes; both must be reported.
- Verify repairs numerically across a declared frequency grid and with audible output; a convincing sound alone is not proof.

## Agent behavior and tools

- One Nemotron reasoning agent is sufficient. It interprets goals, proposes supported edits, chooses experiments, gives hints, and explains measured evidence. Deterministic code executes tools and evaluates numeric constraints.
- Explore tools: inspect intended circuit, propose template/value edit, simulate approved revision, compare named results. User applies changes explicitly or authorizes a bounded experiment sequence.
- Investigate tools: inspect permitted case information, request allowed experiment, read existing observation, submit evidence-linked diagnosis, and request verification after submission. None may read the answer.
- Guide me asks for a prediction or offers one useful hint without immediately revealing the solution. Investigate for me can execute the bounded measurement loop after the user selects it.
- Every measured numerical claim must reference a result ID and relevant quantity. Unmeasured predictions are labeled as predictions. Render numerical summaries from structured data wherever possible.
- Stream genuine tool status and concise evidence cards. Never use fake progress percentages, fabricated agent messages, or predetermined successful diagnoses presented as model decisions.
- If credentials are missing, keep simulator/manual tools usable and show AI unavailable with setup instructions. A test fixture may exist only as an explicitly labeled development mode; do not treat it as a working AI integration.

## Interface and accessibility

- House style: Linear/Vercel-inspired instrument application. Compact application density, dark theme, one restrained cyan accent. All other color is neutral or reserved for semantic status. No glass effects, gradient blobs, decorative 3D, or marketing homepage in place of the workbench.
- Define tokens before components: spacing 4/8/12/16/24/32/48; type 12/14/16/24/32 px; weights 400/600; radii 4/8/12 px; motion 150/200 ms. Use a system sans stack and tabular/monospace measurement values.
- Start palette: background #0B1014, surface #121A20, raised #1A252D, primary text #E8EEF2, secondary text #A7B8C3, accent #65C5D4. Verify contrast before use; do not assume these tokens meet every context. Use clearly visible control boundaries and focus indicators.
- Desktop: slim mode/navigation rail, large central schematic and instruments, focused investigation panel, compact playback/command strip. On narrow screens use accessible tabs; preserve the full workflow.
- Input/reference traces use a neutral dashed line; output/current selection uses cyan with explicit labels and different line styles. Color must never be the only differentiator.
- Reveal advanced settings progressively. Keep the current goal, circuit revision, active probe, comparison pair, and experiment budget discoverable without showing every metric at once.
- Build schematic and semantic component list from the same graph. A component entry exposes ID, type, value, endpoints, and inspect/edit/probe actions. Topology descriptions come from trusted data rather than generated prose.
- Every core task works with keyboard and text input. Provide visible focus, logical focus order, no traps, labeled controls, sufficiently large targets, polite status announcements, result tables, graph summaries, and reduced-motion behavior.
- Preserve focus after edits. Do not stream every token or waveform sample into screen-reader announcements. Speech and circuit audio have independent controls. Muting all audio must leave the task completable.
- Design loading, empty, invalid-value, timeout, cancelled, missing-key, simulator-error, ambiguous-diagnosis, failed-repair, and success states. Preserve prior valid results after a failed operation.

## Implementation sequence and gates

1. **First 48 hours: prove the engine.** Scaffold only what is needed; compile one low-pass template; run actual AC and transient simulation; render correctly scaled A/B audio; establish an isolated hidden-fault case; connect one real Nebius tool loop. Start with a functional small screen, then polish after this gate.
2. **Days 3–5: complete the investigation.** Add current probes, budget enforcement, evidence history, honest ambiguity, single diagnosis, repair verification, cancellation, and leakage checks. Add high-pass and the remaining supported fault catalog only after low-pass passes.
3. **Days 6–8: complete the workbench.** Implement component editing, goal preview, comparisons, undo, Guide me, semantic navigation, and the designed state coverage. Keep all three modes in the same shell.
4. **Days 9–11: evaluate and refine.** Run held-out cases and novice usability sessions; fix failures; add optional voice only if the core is reliable. Measure actual latency and make the demo concise.
5. **Days 12–14: ship.** Deploy, test from a fresh browser, document setup, publish honest results, prepare submission assets, and record the demonstration. Reserve the remaining time for integration issues rather than features.
- If starting later, compress optional enhancements; retain physics checks, fault isolation, real model calls, accessibility fundamentals, and repair verification.

## Meaningful verification

- Physics: multiple low/high-pass values match analytical gain within 1% and phase within 1 degree on an appropriate test grid. Use absolute tolerances near zero and wrapped phase differences. State test assumptions.
- Audio: extracted steady-state tone amplitudes agree with expected transfer within a declared tolerance, initially 0.5 dB; no clipping or per-clip normalization; muted use still works.
- Identifiability: paired doubled-R/doubled-C cases are equivalent with voltage-only measurements and distinguishable with current. At least one test deliberately withholds the resolving probe and requires ambiguity handling.
- Isolation: inspect complete agent-visible tool outputs and browser payloads for hidden values, seeds, files, logs, and accidental diagnostic hints. Test forbidden IDs, stale revisions, cross-case reads, budget bypass, and reveal timing.
- Agent: ten varied end-to-end trials first, including no-fault and ambiguous cases. Then freeze a held-out set of at least 24 balanced cases across the two families and supported conditions, with unseen nominal values. Keep development fixtures separate.
- Compare Nemotron with a simple fixed-experiment diagnostic baseline using the same hypotheses, observations, initial symptom, budgets, and measurement tolerances. Report exact diagnosis, abstentions, unsupported certainty, verified repair, experiment count, failure count, and actual latency. Report costs only when measured from usage and verified pricing. Do not claim statistical superiority from this small set.
- UI: one complete browser test covers edit → simulate → compare → challenge → investigate → repair. Manually check keyboard and a real screen reader; automated audits alone do not validate accessibility.
- User evidence: aim for 3–5 electronics beginners and relevant assistive-technology users where available. Observe uncoached tasks and assistance needed. Report small-sample observations accurately; if no relevant users test it, state that accessibility remains unvalidated.
- Stop adding tests once the concrete risks and release gates are covered. Avoid tests that merely duplicate implementation.

## Delivery and demonstration

- README: actual setup/run/test commands, architecture, supported circuits/faults, modeling limitations, model configuration, NVIDIA/Nebius usage, and evaluation reproduction. Maintain concise progress notes for subsequent coding sessions.
- Public repository with an appropriate open-source license and third-party notices; working hosted app/test build; public YouTube demo under three minutes; submission text and platform feedback. Recheck current official rules. Keep the hosted app available for judging, not only the submission date.
- Demo, approximately 150 seconds: 0–15 hear/see the symptom; 15–40 create supported filter and simulate; 40–55 compare; 55–65 start hidden fault; 65–110 choose experiments and diagnose; 110–130 verify repair; 130–150 show accessible control and actual evaluation results.
- Show actual interactions and latency. A prerecorded fallback is clearly labeled; never present a replay as fresh inference. Give judges a fresh-case control for supported mysteries.
- Completion requires a working end-to-end app, reproducible physics/diagnosis evidence, a usable manual path if inference fails, and accurate limitations. Visual polish cannot substitute for these.

## Primary references

- [Official hackathon rules](https://nebiusglobalaihackathon.devpost.com/rules)
- [ngspice tutorial and analysis capabilities](https://ngspice.sourceforge.io/ngspice-tutorial.html)
- [Official Nebius Token Factory cookbook](https://github.com/nebius/token-factory-cookbook)
- [Nemotron on Nebius](https://nebius.com/blog/posts/nemotron3-super-now-available)
- [W3C guidance for complex diagrams and charts](https://www.w3.org/WAI/tutorials/images/complex/)

## Github rules

- always commit everything one by one so i get max amount of commits
- always make meaningful commits
- always make quality commits