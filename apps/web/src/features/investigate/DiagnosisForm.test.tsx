import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { makeCase } from "../../test/fixtures";
import { DiagnosisForm, DiagnosisSummary, validateDiagnosis } from "./DiagnosisForm";

describe("validateDiagnosis mirrors the server rules", () => {
  it("requires a hypothesis and a citation for a fault", () => {
    expect(validateDiagnosis("fault", "", [], [])).toMatchObject({ hypothesis: expect.any(String), citations: expect.any(String) });
  });
  it("requires a citation for no fault", () => {
    expect(validateDiagnosis("no_fault", "", [], [])).toMatchObject({ citations: expect.any(String) });
  });
  it("allows insufficient evidence without citations but needs candidates", () => {
    expect(validateDiagnosis("insufficient_evidence", "", [], [])).toEqual({ candidates: expect.any(String) });
    expect(validateDiagnosis("insufficient_evidence", "", [], ["r1_increased", "c1_increased"])).toEqual({});
  });
});

describe("DiagnosisForm", () => {
  it("blocks an uncited fault diagnosis and focuses the first invalid field", async () => {
    const onSubmit = vi.fn(async () => true);
    render(<DiagnosisForm c={makeCase()} submitting={false} onSubmit={onSubmit} />);
    await userEvent.click(screen.getByRole("radio", { name: /A component fault/ }));
    await userEvent.click(screen.getByRole("button", { name: "Submit diagnosis" }));
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByText("Choose which fault you think it is.")).toBeInTheDocument();
    expect(screen.getByText(/Cite at least one observation/)).toBeInTheDocument();
    expect(screen.getByLabelText("Which fault?")).toHaveAttribute("aria-invalid", "true");
  });

  it("submits a cited fault diagnosis with only the allowed fields", async () => {
    const onSubmit = vi.fn(async () => true);
    render(<DiagnosisForm c={makeCase()} submitting={false} onSubmit={onSubmit} />);
    await userEvent.click(screen.getByRole("radio", { name: /A component fault/ }));
    await userEvent.selectOptions(screen.getByLabelText("Which fault?"), "c1_increased");
    await userEvent.click(screen.getByRole("checkbox", { name: /Obs 1 · Output voltage sweep/ }));
    const freq = screen.getByLabelText("Frequency cited from Obs 1");
    await userEvent.selectOptions(freq, within(freq).getByRole("option", { name: "20 kHz" }));
    await userEvent.click(screen.getByRole("button", { name: "Submit diagnosis" }));
    expect(onSubmit).toHaveBeenCalledTimes(1);
    const payload = (onSubmit.mock.calls[0] as unknown as [Record<string, unknown>])[0];
    expect(payload).toMatchObject({
      conclusion: "fault",
      hypothesis_id: "c1_increased",
      candidate_hypothesis_ids: [],
      suggested_instrument: null,
      citations: [{ observation_id: "obs_fixture000001", quantity: "output_gain_db" }],
    });
    const cited = (payload.citations as { frequency_hz: number }[])[0]!;
    expect(cited.frequency_hz).toBeCloseTo(20000, 6);
  });

  it("offers candidates and a suggested instrument only for insufficient evidence", async () => {
    const onSubmit = vi.fn(async () => true);
    render(<DiagnosisForm c={makeCase()} submitting={false} onSubmit={onSubmit} />);
    expect(screen.queryByText("Explanations that still fit")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("radio", { name: /Not enough evidence/ }));
    expect(screen.getByRole("checkbox", { name: "R1 is about twice its nominal value" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "C1 is about twice its nominal value" })).toBeChecked();
    await userEvent.selectOptions(screen.getByLabelText(/Which measurement would help/), "source_current_sweep");
    await userEvent.click(screen.getByRole("button", { name: "Submit diagnosis" }));
    const payload = (onSubmit.mock.calls[0] as unknown as [Record<string, unknown>])[0];
    expect(payload).toMatchObject({
      conclusion: "insufficient_evidence",
      hypothesis_id: null,
      candidate_hypothesis_ids: ["r1_increased", "c1_increased"],
      suggested_instrument: "source_current_sweep",
      citations: [],
    });
  });
});

describe("DiagnosisForm candidate defaults", () => {
  it("follows the current evidence: explanations ruled out by later measurements are not pre-ticked", async () => {
    const onSubmit = vi.fn(async () => true);
    const atSymptom = makeCase();
    const { rerender } = render(<DiagnosisForm c={atSymptom} submitting={false} onSubmit={onSubmit} />);
    // A later measurement rules out c1_increased: only r1_increased still fits.
    const later = makeCase({
      evidence: {
        ...atSymptom.evidence,
        remaining: ["r1_increased"],
        assessments: atSymptom.evidence.assessments.map((a) => (a.hypothesis_id === "c1_increased" ? { ...a, status: "contradicted" } : a)),
      },
    });
    rerender(<DiagnosisForm c={later} submitting={false} onSubmit={onSubmit} />);
    await userEvent.click(screen.getByRole("radio", { name: /Not enough evidence/ }));
    expect(screen.getByRole("checkbox", { name: "R1 is about twice its nominal value" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "C1 is about twice its nominal value" })).not.toBeChecked();
  });
});

describe("DiagnosisSummary", () => {
  it("shows the recorded evidence check honestly, including ambiguity", () => {
    const c = makeCase({
      status: "diagnosis_submitted",
      diagnosis: {
        id: "dx_fixture0000001",
        submitted_by: "user",
        agent_run_id: null,
        created_at: "2026-10-15T12:05:00Z",
        submission: {
          conclusion: "fault",
          hypothesis_id: "r1_increased",
          candidate_hypothesis_ids: [],
          citations: [{ observation_id: "obs_fixture000001", quantity: "output_gain_db", frequency_hz: 4000, note: "" }],
          explanation: "The output is too low.",
          suggested_instrument: null,
        },
        evidence_check: { status: "ambiguous", remaining: ["r1_increased", "c1_increased"], message: "The recorded evidence also fits C1 increased." },
      },
    });
    render(<DiagnosisSummary c={c} record={c.diagnosis!} />);
    expect(screen.getByText("R1 is about twice its nominal value")).toBeInTheDocument();
    expect(screen.getByText("Ambiguous: other explanations also fit")).toBeInTheDocument();
    expect(screen.getByText("The recorded evidence also fits C1 increased.")).toBeInTheDocument();
    expect(screen.getByText("Obs 1 · output gain @ 4 kHz")).toBeInTheDocument();
  });
});
