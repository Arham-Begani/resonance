// Investigation state: the PublicCase (never hidden state), pending actions, and the view
// selected in the central instruments area. All mutations go through typed endpoints.
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { ApiFailure, toFailure } from "../../api/client";
import { endpoints, type Endpoints } from "../../api/endpoints";
import type { DiagnosisSubmission, InstrumentKind, PublicCase, TemplateId } from "../../api/types";
import { newRequestId } from "../../lib/hooks";
import { useAnnouncer } from "../../state/announcer";
import { useWorkbench } from "../../state/workbench";
import { useAgentEvents, useAgentRuns } from "../agent/agentRuns";
import { comparePrediction, type GuidePrediction, type PredictionResult } from "./guidePrediction";
import type { TransientProbe } from "./labels";

export type CaseAction = "load" | "create" | "mode" | "experiment" | "diagnosis" | "repair_proposal" | "repair" | "close" | "agent";
export type CaseView = { kind: "observation"; id: string } | { kind: "repair" } | null;

export interface CaseError {
  action: CaseAction;
  error: ApiFailure;
}

interface CaseApi {
  current: PublicCase | null;
  pending: CaseAction | null;
  error: CaseError | null;
  view: CaseView;
  setView: (view: CaseView) => void;
  /** The family chosen for the next mystery (null = Surprise me). */
  family: TemplateId | null;
  setFamily: (family: TemplateId | null) => void;
  /** Guide-me predictions compared with the evidence, by observation ID (UI only). */
  predictions: Record<string, PredictionResult>;
  load: (caseId: string) => Promise<void>;
  create: (family?: TemplateId | null) => Promise<boolean>;
  chooseMode: (mode: "guide" | "agent") => Promise<boolean>;
  /** Resolves to the new observation's ID (or null if the request failed). */
  runExperiment: (instrument: InstrumentKind, probe: TransientProbe | null, prediction?: GuidePrediction | null) => Promise<string | null>;
  submitDiagnosis: (submission: DiagnosisSubmission) => Promise<boolean>;
  proposeRepair: (componentId: "R1" | "C1", rationale: string) => Promise<boolean>;
  verifyRepair: () => Promise<boolean>;
  close: (caseId?: string) => Promise<void>;
  startAgent: (kind: "investigate" | "hint", message?: string) => Promise<void>;
  cancelAgent: () => Promise<void>;
  clearError: () => void;
  forget: () => void;
}

const CaseContext = createContext<CaseApi | null>(null);

function latestObservationId(c: PublicCase): string | null {
  const ok = c.observations.filter((o) => o.status === "succeeded");
  return ok.length ? ok[ok.length - 1]!.id : null;
}

export function CaseProvider({ children, client = endpoints }: { children: ReactNode; client?: Endpoints }) {
  const wb = useWorkbench();
  const runs = useAgentRuns();
  const { announce, announceError } = useAnnouncer();
  const [current, setCurrent] = useState<PublicCase | null>(null);
  const [pending, setPending] = useState<CaseAction | null>(null);
  const [error, setError] = useState<CaseError | null>(null);
  const [view, setView] = useState<CaseView>(null);
  const [family, setFamily] = useState<TemplateId | null>(null);
  const familyChosen = useRef(false);
  const [predictions, setPredictions] = useState<Record<string, PredictionResult>>({});
  const currentRef = useRef<PublicCase | null>(null);
  currentRef.current = current;

  const accept = useCallback(
    (next: PublicCase, focusLatest = false) => {
      setCurrent(next);
      if (focusLatest) {
        const id = latestObservationId(next);
        if (id) setView({ kind: "observation", id });
      }
      setView((v) => v ?? (latestObservationId(next) ? { kind: "observation", id: latestObservationId(next)! } : null));
    },
    [],
  );

  const run = useCallback(
    async <T,>(action: CaseAction, fn: () => Promise<T>, onError?: (f: ApiFailure) => void): Promise<T | null> => {
      setPending(action);
      setError(null);
      try {
        return await fn();
      } catch (thrown) {
        const failure = toFailure(thrown);
        setError({ action, error: failure });
        if (onError) onError(failure);
        else announce(failure.message);
        return null;
      } finally {
        setPending(null);
      }
    },
    [announce],
  );

  const load = useCallback(
    async (caseId: string) => {
      const c = await run("load", () => client.getCase(caseId));
      if (c) accept(c);
    },
    [client, run, accept],
  );

  // Until the learner picks a family, a new mystery uses the family they are exploring.
  const exploringFamily = wb.state.revisions.find((r) => r.id === wb.state.currentRevisionId)?.template_id ?? "rc_lowpass";
  const effectiveFamily = familyChosen.current ? family : exploringFamily;

  // Restore the session's active case on first load.
  const restored = useRef(false);
  useEffect(() => {
    if (restored.current || wb.state.status !== "ready") return;
    restored.current = true;
    if (wb.state.activeCaseId) void load(wb.state.activeCaseId);
  }, [wb.state.status, wb.state.activeCaseId, load]);

  // Re-attach to a run that is still active (e.g. after a reload).
  useEffect(() => {
    const runId = current?.active_run_id;
    if (runId && runs.views.case.run?.id !== runId && runs.views.case.connection !== "starting") runs.attach("case", runId);
  }, [current?.active_run_id, runs]);

  const refresh = useCallback(async () => {
    const id = currentRef.current?.id;
    if (!id) return;
    try {
      const c = await client.getCase(id);
      setCurrent(c);
    } catch {
      // Keep the last valid state; the next event or action will refresh again.
    }
  }, [client]);

  // Case data changes when the agent records observations, diagnoses, or repairs.
  useAgentEvents((scope, event) => {
    if (scope !== "case") return;
    if (event.type === "tool_result" && event.observation_id) {
      void refresh().then(() => setView({ kind: "observation", id: event.observation_id! }));
    } else if (event.type === "diagnosis" || event.type === "repair_proposal" || event.type === "run_finished") {
      void refresh();
    }
  });

  const familyRef = useRef(effectiveFamily);
  familyRef.current = effectiveFamily;

  const create = useCallback(async (chosen?: TemplateId | null) => {
    const templateId = chosen === undefined ? familyRef.current : chosen;
    const c = await run("create", () => client.createCase(templateId));
    if (c) {
      setView(null);
      setPredictions({});
      accept(c, true);
      runs.clear("case");
      wb.setActiveCase(c.id);
      announce("New mystery started. The symptom is ready to inspect.");
      return true;
    }
    return false;
  }, [client, run, accept, runs, wb, announce]);

  const chooseMode = useCallback(
    async (mode: "guide" | "agent") => {
      const id = currentRef.current?.id;
      if (!id) return false;
      const c = await run("mode", () => client.setCaseMode(id, mode));
      if (c) {
        accept(c);
        announce(mode === "guide" ? "Guide me: choose an instrument to run." : "Investigate for me selected.");
        return true;
      }
      return false;
    },
    [client, run, accept, announce],
  );

  const runExperiment = useCallback(
    async (instrument: InstrumentKind, probe: TransientProbe | null, prediction: GuidePrediction | null = null) => {
      const id = currentRef.current?.id;
      if (!id) return null;
      // One key per click; reused only for the single automatic retry of this same click.
      const key = newRequestId("ui");
      announce("Running the experiment on the mystery circuit…");
      const res = await run("experiment", async () => {
        try {
          return await client.runCaseExperiment(id, instrument, probe, key);
        } catch (thrown) {
          const failure = toFailure(thrown);
          if (failure.code === "network_error") return client.runCaseExperiment(id, instrument, probe, key);
          throw failure;
        }
      });
      if (res) {
        accept(res.case);
        setView({ kind: "observation", id: res.observation.id });
        if (res.observation.status === "succeeded") {
          let check = "";
          if (prediction && prediction.expected.length > 0) {
            const result = comparePrediction(prediction, res.case.evidence.assessments);
            setPredictions((prev) => ({ ...prev, [res.observation.id]: result }));
            check = ` Your prediction: ${result.matched} of ${result.checks.length} matched the evidence.`;
          }
          announce(`${res.observation.summary}${check} ${res.case.budget.remaining} of ${res.case.budget.total} experiments left.`);
        } else {
          announce(res.observation.error?.message ?? "The experiment failed and did not use budget.");
        }
        return res.observation.id;
      }
      return null;
    },
    [client, run, accept, announce],
  );

  const submitDiagnosis = useCallback(
    async (submission: DiagnosisSubmission) => {
      const id = currentRef.current?.id;
      if (!id) return false;
      const c = await run("diagnosis", () => client.submitDiagnosis(id, submission), (f) => announceError(f.message));
      if (c) {
        accept(c);
        announce(`Diagnosis recorded. ${c.diagnosis?.evidence_check.message ?? ""}`);
        return true;
      }
      return false;
    },
    [client, run, accept, announce, announceError],
  );

  const proposeRepair = useCallback(
    async (componentId: "R1" | "C1", rationale: string) => {
      const id = currentRef.current?.id;
      if (!id) return false;
      const c = await run("repair_proposal", () => client.proposeRepair(id, { component_id: componentId, rationale }));
      if (c) {
        accept(c);
        announce(`Repair proposed: replace ${componentId} with its nominal part.`);
        return true;
      }
      return false;
    },
    [client, run, accept, announce],
  );

  const verifyRepair = useCallback(async () => {
    const id = currentRef.current?.id;
    if (!id) return false;
    announce("Applying the repair and verifying it against the intended design…");
    const c = await run("repair", () => client.verifyRepair(id));
    if (c) {
      accept(c);
      setView({ kind: "repair" });
      const r = c.repair;
      announce(r ? (r.functional_recovery ? "Repair checked: behaviour restored." : `Repair checked: the behaviour was not restored. ${r.summary}`) : "Repair recorded.");
      return true;
    }
    return false;
  }, [client, run, accept, announce]);

  const close = useCallback(
    async (caseId?: string) => {
      const id = caseId ?? currentRef.current?.id;
      if (!id) return;
      const c = await run("close", () => client.closeCase(id));
      if (c) {
        accept(c);
        wb.setActiveCase(null);
        announce("Case closed. The actual fault is revealed.");
      }
    },
    [client, run, accept, wb, announce],
  );

  const startAgent = useCallback(
    async (kind: "investigate" | "hint", message?: string) => {
      const id = currentRef.current?.id;
      if (!id) return;
      setError(null);
      await runs.start("case", () => client.startCaseAgentRun(id, { kind, message: message?.trim() || null }));
      void refresh();
    },
    [client, runs, refresh],
  );

  const cancelAgent = useCallback(async () => {
    await runs.cancel("case");
  }, [runs]);

  const api = useMemo<CaseApi>(
    () => ({
      current,
      pending,
      error,
      view,
      setView,
      family: effectiveFamily,
      setFamily: (f) => {
        familyChosen.current = true;
        setFamily(f);
      },
      predictions,
      load,
      create,
      chooseMode,
      runExperiment,
      submitDiagnosis,
      proposeRepair,
      verifyRepair,
      close,
      startAgent,
      cancelAgent,
      clearError: () => setError(null),
      forget: () => {
        setCurrent(null);
        setView(null);
        setPredictions({});
        runs.clear("case");
      },
    }),
    [current, pending, error, view, effectiveFamily, predictions, load, create, chooseMode, runExperiment, submitDiagnosis, proposeRepair, verifyRepair, close, startAgent, cancelAgent, runs],
  );

  return <CaseContext.Provider value={api}>{children}</CaseContext.Provider>;
}

export function useCase(): CaseApi {
  const ctx = useContext(CaseContext);
  if (!ctx) throw new Error("useCase must be used inside CaseProvider");
  return ctx;
}
