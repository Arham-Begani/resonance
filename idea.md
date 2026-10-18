# RESONANCE

**Hear what a circuit does. Discover why it fails.**

Resonance is an interactive electronics lab that runs entirely in a browser. You build and change small audio circuits, hear their behavior, and investigate hidden faults with an AI lab partner. A laptop is enough; no electronics kit or special hardware is needed.

It is designed for people learning electronics who want to understand the connection between a component, a measurement, and a result they can hear.

## The first impression

The app opens directly into a working instrument panel. A compact circuit sits in the center, with clearly labeled input, resistor, capacitor, output, and ground. An oscilloscope and frequency-response view sit below it. A focused panel on the right holds the current goal and experiment history.

The interface is dark, precise, and calm: fine schematic lines, clear typography, large readable measurements, and a restrained cyan accent. A selected probe highlights its node and the matching trace. Reference and output traces have distinct labels and line styles, so they remain understandable without color.

The opening experiment is ready to try. Its invitation is simple:

> Keep the musical tone. Reduce the whistle.

Press Play to hear a tone with a high-pitched interference signal. Its two frequency peaks appear beside the sound controls. Nothing plays automatically.

## Explore: turn an intention into an experiment

Type a request such as:

> Build a filter that reduces the whistle without losing too much of the main tone.

Resonance proposes a supported circuit and makes its component values visible before you apply it. After you run the experiment, the waveform, frequency response, and measured attenuation appear together.

Switch between Input and Output to hear the effect at the same listening level. The comparison explains both sides of the tradeoff: how much interference was reduced and how much of the wanted tone was lost.

You can select the capacitor, change its value, and run the circuit again. The previous result remains available. A comparison shows the changed component, the new response, and the audible difference.

Requests such as “make the filtering stronger” become proposed changes you can inspect. If a requested result is beyond what the available circuit can do, the app explains the limitation and shows an achievable alternative.

Two circuit families are available: a low-pass filter for reducing higher-frequency interference and a high-pass filter for reducing lower-frequency interference. Each supports direct value editing, measurements, sound comparisons, and undo.

## Learn: make a prediction and hear the answer

Learn uses the same workbench. Before changing a component, it asks a short question:

> If we double the capacitance, what do you expect to happen to the sound?

Choose a prediction or enter your own, then run the experiment. Your prediction remains beside the observed result. The explanation connects the component change to the measured response and the sound you heard.

You can request a hint, inspect a connection, or ask why a measurement matters. The AI helps you decide what to try while leaving you in control of the experiment.

## Investigate: a circuit mystery

Select **Start a mystery**. The circuit's expected design stays visible, but its simulated behavior may now differ. A component value might have changed, a capacitor might be disconnected, or the circuit might actually be working correctly.

The actual condition is hidden during the investigation. Both you and the AI work from the same allowed observations.

Choose one of two approaches:

- **Guide me:** choose your measurements and request hints when needed.
- **Investigate for me:** let the AI select experiments and present the evidence.

Each case provides an initial symptom and a budget of three experiments. You can inspect output response, measure source current, or examine an allowed signal over time. Each instrument clearly states what information it will return.

The investigation panel records an understandable sequence:

1. What we observed.
2. Which explanations still fit.
3. What the next experiment can tell us.

For example, a changed resistor and a changed capacitor can sometimes produce the same output response. Resonance can recognize that the available evidence is insufficient and request a different kind of measurement:

> Either component change could explain this response. Measuring the source current will help distinguish them.

The selected probe appears on the schematic. The measurement arrives. The evidence panel updates. Explanations are tied to the displayed observations.

If the evidence remains ambiguous, the result says so and explains what additional information would be useful. A case can also conclude that no fault was detected.

## Repair: hear and see the verification

After a diagnosis, Resonance proposes a specific repair. Apply it to rerun the experiment and compare the repaired response with the expected circuit.

The result separates two questions: whether the cause was correctly identified, and whether the proposed repair restored the behavior. The sound comparison and measured response make the outcome visible and audible.

Once the investigation is complete, the actual fault is revealed. You can review the measurement sequence, inspect what ruled out each alternative, and replay the case. **New mystery** starts a fresh challenge.

## One workbench, several ways to use it

The visual schematic has a synchronized component list. An entry can read:

> C1 — capacitor — 100 nF — connected between output and ground.

You can navigate components, edit values, choose probes, inspect results, compare experiments, and undo using the keyboard. Graphs include readable measurements and short summaries. Important results are available as text, visuals, and optional audio.

Voice input is an optional shortcut. Spoken explanations and circuit playback have separate controls. The whole task remains possible with sound muted or voice input disabled.

Experiment history preserves the original circuit, each change, and its measured result. If an experiment fails, the previous valid result stays available. If the AI is unavailable, manual circuit controls and simulation remain usable.

## The experience people remember

You hear something wrong. You explore the circuit that produced it. You choose a measurement, learn what it rules out, and test a repair. The response returns to its expected shape, and the sound confirms the change.

Resonance makes the relationship between cause, evidence, and outcome something you can see, hear, and investigate yourself.
