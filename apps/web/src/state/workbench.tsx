// Workbench store: session revisions, experiments, goal, proposals, and shared UI selection.
// React context + reducer; all server data comes from the typed endpoints.
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  type ReactNode,
} from "react";
import { ApiFailure, toFailure } from "../api/client";
import { endpoints, type Endpoints } from "../api/endpoints";
import type {
  ComponentId,
  EditProposal,
  EditableComponentId,
  ExperimentRecord,
  ExperimentSummary,
  FeasibilityAnalysis,
  Goal,
  GoalInput,
  HealthResponse,
  NodeId,
  Revision,
} from "../api/types";
import { useAnnouncer } from "./announcer";

export type Mode = "explore" | "investigate";
/** What the active probe measures: a node voltage or the source current. */
export type Probe = { kind: "node"; node: NodeId } | { kind: "current"; component: "V1" } | null;
export type Operation = "init" | "revision" | "experiment" | "goal" | "proposal" | "compare";

export interface OperationError {
  op: Operation;
  error: ApiFailure;
}

export interface WorkbenchData {
  status: "loading" | "ready" | "error";
  health: HealthResponse | null;
  healthError: ApiFailure | null;
  revisions: Revision[];
  currentRevisionId: string | null;
  experiments: ExperimentSummary[];
  records: Record<string, ExperimentRecord>;
  shownExperimentId: string | null;
  goal: Goal | null;
  feasibility: FeasibilityAnalysis | null;
  proposals: EditProposal[];
  activeCaseId: string | null;
  pending: Operation | null;
  error: OperationError | null;
  mode: Mode;
  selectedComponentId: ComponentId | null;
  editingComponentId: EditableComponentId | null;
  probe: Probe;
}

type Action =
  | { type: "health"; health: HealthResponse | null; error: ApiFailure | null }
  | {
      type: "loaded";
      revisions: Revision[];
      currentRevisionId: string | null;
      experiments: ExperimentSummary[];
      goal: Goal;
      feasibility: FeasibilityAnalysis;
      proposals: EditProposal[];
      activeCaseId: string | null;
    }
  | { type: "load_failed"; error: ApiFailure }
  | { type: "pending"; op: Operation | null }
  | { type: "error"; error: OperationError | null }
  | { type: "revision_added"; revision: Revision }
  | { type: "set_current"; revisionId: string }
  | { type: "record"; record: ExperimentRecord; show: boolean }
  | { type: "show_experiment"; experimentId: string | null }
  | { type: "goal"; goal: Goal; feasibility: FeasibilityAnalysis }
  | { type: "proposal_added"; proposal: EditProposal }
  | { type: "proposal_removed"; proposalId: string }
  | { type: "active_case"; caseId: string | null }
  | { type: "mode"; mode: Mode }
  | { type: "select"; componentId: ComponentId | null }
  | { type: "edit"; componentId: EditableComponentId | null }
  | { type: "probe"; probe: Probe };

function initialMode(): Mode {
  if (typeof window !== "undefined" && window.location.hash === "#investigate") return "investigate";
  return "explore";
}

export function createInitialState(): WorkbenchData {
  return {
    status: "loading",
    health: null,
    healthError: null,
    revisions: [],
    currentRevisionId: null,
    experiments: [],
    records: {},
    shownExperimentId: null,
    goal: null,
    feasibility: null,
    proposals: [],
    activeCaseId: null,
    pending: "init",
    error: null,
    mode: initialMode(),
    selectedComponentId: null,
    editingComponentId: null,
    probe: { kind: "node", node: "out" },
  };
}

function summarize(record: ExperimentRecord): ExperimentSummary {
  return {
    id: record.id,
    revision_id: record.revision_id,
    status: record.status,
    created_at: record.created_at,
    goal_passed: record.goal ? record.goal.passed : null,
  };
}

export function reducer(state: WorkbenchData, action: Action): WorkbenchData {
  switch (action.type) {
    case "health":
      return { ...state, health: action.health ?? state.health, healthError: action.error };
    case "loaded":
      return {
        ...state,
        status: "ready",
        revisions: action.revisions,
        currentRevisionId: action.currentRevisionId,
        experiments: action.experiments,
        goal: action.goal,
        feasibility: action.feasibility,
        proposals: action.proposals,
        activeCaseId: action.activeCaseId,
        pending: null,
        error: null,
      };
    case "load_failed":
      return { ...state, status: "error", pending: null, error: { op: "init", error: action.error } };
    case "pending":
      return { ...state, pending: action.op };
    case "error":
      return { ...state, error: action.error };
    case "revision_added":
      return {
        ...state,
        revisions: [...state.revisions.filter((r) => r.id !== action.revision.id), action.revision],
        currentRevisionId: action.revision.id,
        editingComponentId: null,
      };
    case "set_current":
      return { ...state, currentRevisionId: action.revisionId, editingComponentId: null };
    case "record": {
      const summary = summarize(action.record);
      const exists = state.experiments.some((e) => e.id === summary.id);
      return {
        ...state,
        records: { ...state.records, [action.record.id]: action.record },
        experiments: exists
          ? state.experiments.map((e) => (e.id === summary.id ? summary : e))
          : [...state.experiments, summary],
        shownExperimentId: action.show ? action.record.id : state.shownExperimentId,
      };
    }
    case "show_experiment":
      return { ...state, shownExperimentId: action.experimentId };
    case "goal":
      return { ...state, goal: action.goal, feasibility: action.feasibility };
    case "proposal_added":
      return {
        ...state,
        proposals: [...state.proposals.filter((p) => p.id !== action.proposal.id), action.proposal],
      };
    case "proposal_removed":
      return { ...state, proposals: state.proposals.filter((p) => p.id !== action.proposalId) };
    case "active_case":
      return { ...state, activeCaseId: action.caseId };
    case "mode":
      return { ...state, mode: action.mode, editingComponentId: null };
    case "select":
      return { ...state, selectedComponentId: action.componentId };
    case "edit":
      return {
        ...state,
        editingComponentId: action.componentId,
        selectedComponentId: action.componentId ?? state.selectedComponentId,
      };
    case "probe":
      return { ...state, probe: action.probe };
    default:
      return state;
  }
}

// ---------- selectors ----------

/** Explore revisions only (case designs are numbered separately). */
export function exploreRevisions(state: WorkbenchData): Revision[] {
  return state.revisions.filter((r) => r.origin !== "case_intended");
}

export function revisionOrdinal(state: WorkbenchData, revisionId: string | null | undefined): number | null {
  if (!revisionId) return null;
  const index = exploreRevisions(state).findIndex((r) => r.id === revisionId);
  return index < 0 ? null : index + 1;
}

export function experimentOrdinal(state: WorkbenchData, experimentId: string | null | undefined): number | null {
  if (!experimentId) return null;
  const index = state.experiments.findIndex((e) => e.id === experimentId);
  return index < 0 ? null : index + 1;
}

export function currentRevision(state: WorkbenchData): Revision | null {
  return state.revisions.find((r) => r.id === state.currentRevisionId) ?? null;
}

export function shownRecord(state: WorkbenchData): ExperimentRecord | null {
  return state.shownExperimentId ? (state.records[state.shownExperimentId] ?? null) : null;
}

function latestSucceededFor(experiments: ExperimentSummary[], revisionId: string): ExperimentSummary | null {
  for (let i = experiments.length - 1; i >= 0; i--) {
    const e = experiments[i]!;
    if (e.revision_id === revisionId && e.status === "succeeded") return e;
  }
  return null;
}

// ---------- controller ----------

export interface WorkbenchApi {
  state: WorkbenchData;
  reload: () => Promise<void>;
  refreshHealth: () => Promise<void>;
  applyValues: (values: { component_id: EditableComponentId; value: string | number }[]) => Promise<boolean>;
  runExperiment: () => Promise<ExperimentRecord | null>;
  cancelExperiment: () => void;
  showExperiment: (experimentId: string) => Promise<void>;
  undo: () => void;
  selectRevision: (revisionId: string) => void;
  setGoal: (input: GoalInput) => Promise<boolean>;
  addProposal: (proposal: EditProposal) => void;
  applyProposal: (proposal: EditProposal) => Promise<boolean>;
  dismissProposal: (proposalId: string) => Promise<void>;
  setActiveCase: (caseId: string | null) => void;
  setMode: (mode: Mode) => void;
  select: (componentId: ComponentId | null) => void;
  edit: (componentId: EditableComponentId | null) => void;
  setProbe: (probe: Probe) => void;
  clearError: () => void;
}

const WorkbenchContext = createContext<WorkbenchApi | null>(null);

const HEALTH_POLL_MS = 30_000;

export function WorkbenchProvider({ children, client = endpoints }: { children: ReactNode; client?: Endpoints }) {
  const [state, dispatch] = useReducer(reducer, undefined, createInitialState);
  const stateRef = useRef(state);
  stateRef.current = state;
  const { announce, announceError } = useAnnouncer();

  const fail = useCallback(
    (op: Operation, error: unknown, blocking = false) => {
      const failure = toFailure(error);
      dispatch({ type: "error", error: { op, error: failure } });
      if (blocking) announceError(failure.message);
      else announce(failure.message);
      return failure;
    },
    [announce, announceError],
  );

  const refreshHealth = useCallback(async () => {
    try {
      const health = await client.health();
      dispatch({ type: "health", health, error: null });
    } catch (error) {
      dispatch({ type: "health", health: null, error: toFailure(error) });
    }
  }, [client]);

  const loadRecord = useCallback(
    async (experimentId: string, show: boolean) => {
      const cached = stateRef.current.records[experimentId];
      if (cached) {
        if (show) dispatch({ type: "show_experiment", experimentId });
        return cached;
      }
      const record = await client.getExperiment(experimentId);
      dispatch({ type: "record", record, show });
      return record;
    },
    [client],
  );

  // One bootstrap/reload at a time: a second caller (e.g. React StrictMode re-running effects)
  // joins the in-flight promise instead of creating another session or original revision.
  const reloadInFlight = useRef<Promise<void> | null>(null);

  const reload = useCallback((): Promise<void> => {
    if (reloadInFlight.current) return reloadInFlight.current;
    const task = (async () => {
      dispatch({ type: "pending", op: "init" });
      try {
        const wb = await client.workbench();
        let revisions = wb.revisions;
        const explore = revisions.filter((r) => r.origin !== "case_intended");
        if (explore.length === 0) {
          const root = await client.createRevision({ template_id: "rc_lowpass", values: [], origin: "user_edit" });
          revisions = [...revisions, root];
        }
        const exploreAll = revisions.filter((r) => r.origin !== "case_intended");
        const remembered = wb.current_revision_id ?? null;
        const current =
          exploreAll.find((r) => r.id === remembered)?.id ?? exploreAll[exploreAll.length - 1]?.id ?? null;
        // Persist the initially shown revision so a reload shows the same one.
        if (current && current !== remembered) client.setCurrentRevision(current).catch(() => undefined);
        dispatch({
          type: "loaded",
          revisions,
          currentRevisionId: current,
          experiments: wb.experiments,
          goal: wb.goal,
          feasibility: wb.feasibility,
          proposals: wb.proposals,
          activeCaseId: wb.active_case_id ?? null,
        });
        const latest = current ? latestSucceededFor(wb.experiments, current) : null;
        const fallback = [...wb.experiments].reverse().find((e) => e.status === "succeeded") ?? null;
        const toShow = latest ?? fallback;
        if (toShow) await loadRecord(toShow.id, true).catch(() => undefined);
      } catch (error) {
        const failure = toFailure(error);
        dispatch({ type: "load_failed", error: failure });
        announceError(failure.message);
      }
    })();
    reloadInFlight.current = task.finally(() => {
      reloadInFlight.current = null;
    });
    return reloadInFlight.current;
  }, [client, loadRecord, announceError]);

  // Bootstrap exactly once per mounted provider (StrictMode runs effects twice in development;
  // refs survive that re-run). Health goes first: on a first visit it establishes the session
  // cookie, so every request that follows belongs to one session.
  const booted = useRef(false);
  useEffect(() => {
    if (booted.current) return;
    booted.current = true;
    void refreshHealth().then(() => reload());
  }, [refreshHealth, reload]);

  useEffect(() => {
    const timer = window.setInterval(() => void refreshHealth(), HEALTH_POLL_MS);
    return () => window.clearInterval(timer);
  }, [refreshHealth]);

  // Persist the shown revision per session (server-side) whenever it changes after loading.
  const persisted = useRef<string | null>(null);
  useEffect(() => {
    const id = state.currentRevisionId;
    if (state.status !== "ready" || !id) return;
    if (persisted.current === null) {
      persisted.current = id;
      return;
    }
    if (persisted.current === id) return;
    persisted.current = id;
    client.setCurrentRevision(id).catch(() => {
      // Non-critical: the client still shows the right revision; a reload may fall back.
    });
  }, [state.currentRevisionId, state.status, client]);

  // Deep links: #investigate opens the Investigate mode (also when the hash changes later).
  useEffect(() => {
    const onHash = () => dispatch({ type: "mode", mode: window.location.hash === "#investigate" ? "investigate" : "explore" });
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    const hash = state.mode === "investigate" ? "#investigate" : "";
    if (window.location.hash !== hash) {
      window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}${hash}`);
    }
  }, [state.mode]);

  const applyValues = useCallback<WorkbenchApi["applyValues"]>(
    async (values) => {
      const parent = stateRef.current.currentRevisionId;
      if (!parent) return false;
      dispatch({ type: "pending", op: "revision" });
      dispatch({ type: "error", error: null });
      try {
        const revision = await client.createRevision({ parent_id: parent, values, origin: "user_edit" });
        dispatch({ type: "revision_added", revision });
        const ordinal = revisionOrdinal({ ...stateRef.current, revisions: [...stateRef.current.revisions, revision] }, revision.id);
        const change = revision.changes?.[0];
        announce(
          `Revision ${ordinal ?? ""} created${change ? `: ${change.component_id} ${change.from_display} to ${change.to_display}` : ""}. Run the experiment to measure it.`,
        );
        return true;
      } catch (error) {
        fail("revision", error);
        return false;
      } finally {
        dispatch({ type: "pending", op: null });
      }
    },
    [client, announce, fail],
  );

  const experimentAbort = useRef<AbortController | null>(null);

  const runExperiment = useCallback(async () => {
    const s = stateRef.current;
    if (!s.currentRevisionId) return null;
    experimentAbort.current?.abort();
    const controller = new AbortController();
    experimentAbort.current = controller;
    dispatch({ type: "pending", op: "experiment" });
    dispatch({ type: "error", error: null });
    announce("Running the experiment in ngspice…");
    try {
      const record = await client.runExperiment(
        { revision_id: s.currentRevisionId, goal_id: s.goal?.id ?? null },
        controller.signal,
      );
      dispatch({ type: "record", record, show: record.status === "succeeded" });
      if (record.status === "succeeded") {
        const goalText = record.goal ? (record.goal.passed ? " Goal met." : " Goal not met.") : "";
        announce(`Experiment finished.${goalText}`);
      } else if (record.error) {
        fail("experiment", new ApiFailure(record.error.code, record.error.message, 0));
      }
      return record;
    } catch (error) {
      const failure = toFailure(error);
      if (failure.code === "conflict" && failure.field === "goal_id") {
        // The goal changed elsewhere; pick up the current one so the next run uses it.
        client
          .workbench()
          .then((wb) => dispatch({ type: "goal", goal: wb.goal, feasibility: wb.feasibility }))
          .catch(() => undefined);
      }
      if (controller.signal.aborted) {
        dispatch({
          type: "error",
          error: { op: "experiment", error: new ApiFailure("cancelled", "The experiment was cancelled. Previous results are unchanged.", 0) },
        });
        announce("Experiment cancelled.");
      } else {
        fail("experiment", error);
      }
      return null;
    } finally {
      if (experimentAbort.current === controller) experimentAbort.current = null;
      dispatch({ type: "pending", op: null });
    }
  }, [client, announce, fail]);

  const cancelExperiment = useCallback(() => {
    experimentAbort.current?.abort();
  }, []);

  const showExperiment = useCallback(
    async (experimentId: string) => {
      try {
        await loadRecord(experimentId, true);
      } catch (error) {
        fail("experiment", error);
      }
    },
    [loadRecord, fail],
  );

  const selectRevision = useCallback(
    (revisionId: string) => {
      dispatch({ type: "set_current", revisionId });
      const latest = latestSucceededFor(stateRef.current.experiments, revisionId);
      if (latest) void showExperiment(latest.id);
    },
    [showExperiment],
  );

  const undo = useCallback(() => {
    const s = stateRef.current;
    const rev = currentRevision(s);
    if (!rev?.parent_id) return;
    const parentId = rev.parent_id;
    selectRevision(parentId);
    const ordinal = revisionOrdinal(s, parentId);
    announce(`Back to Revision ${ordinal ?? ""}.`);
  }, [selectRevision, announce]);

  const setGoal = useCallback(
    async (input: GoalInput) => {
      dispatch({ type: "pending", op: "goal" });
      dispatch({ type: "error", error: null });
      try {
        const res = await client.setGoal(input);
        dispatch({ type: "goal", goal: res.goal, feasibility: res.feasibility });
        announce("Goal updated. Run the experiment to evaluate it.");
        return true;
      } catch (error) {
        fail("goal", error);
        return false;
      } finally {
        dispatch({ type: "pending", op: null });
      }
    },
    [client, announce, fail],
  );

  const applyProposal = useCallback(
    async (proposal: EditProposal) => {
      dispatch({ type: "pending", op: "proposal" });
      dispatch({ type: "error", error: null });
      try {
        const revision = await client.createRevision({
          parent_id: proposal.base_revision_id,
          values: proposal.changes.map((c) => ({ component_id: c.component_id, value: c.to_value_si })),
          origin: "agent_proposal_applied",
          proposal_id: proposal.id,
        });
        dispatch({ type: "revision_added", revision });
        dispatch({ type: "proposal_removed", proposalId: proposal.id });
        announce("Proposal applied as a new revision. Run the experiment to measure it.");
        return true;
      } catch (error) {
        fail("proposal", error);
        return false;
      } finally {
        dispatch({ type: "pending", op: null });
      }
    },
    [client, announce, fail],
  );

  const dismissProposal = useCallback(
    async (proposalId: string) => {
      try {
        await client.dismissProposal(proposalId);
        dispatch({ type: "proposal_removed", proposalId });
        announce("Proposal dismissed.");
      } catch (error) {
        fail("proposal", error);
      }
    },
    [client, announce, fail],
  );

  const api = useMemo<WorkbenchApi>(
    () => ({
      state,
      reload,
      refreshHealth,
      applyValues,
      runExperiment,
      cancelExperiment,
      showExperiment,
      undo,
      selectRevision,
      setGoal,
      addProposal: (proposal) => dispatch({ type: "proposal_added", proposal }),
      applyProposal,
      dismissProposal,
      setActiveCase: (caseId) => dispatch({ type: "active_case", caseId }),
      setMode: (mode) => dispatch({ type: "mode", mode }),
      select: (componentId) => dispatch({ type: "select", componentId }),
      edit: (componentId) => dispatch({ type: "edit", componentId }),
      setProbe: (probe) => dispatch({ type: "probe", probe }),
      clearError: () => dispatch({ type: "error", error: null }),
    }),
    [
      state,
      reload,
      refreshHealth,
      applyValues,
      runExperiment,
      cancelExperiment,
      showExperiment,
      undo,
      selectRevision,
      setGoal,
      applyProposal,
      dismissProposal,
    ],
  );

  return <WorkbenchContext.Provider value={api}>{children}</WorkbenchContext.Provider>;
}

export function useWorkbench(): WorkbenchApi {
  const ctx = useContext(WorkbenchContext);
  if (!ctx) throw new Error("useWorkbench must be used inside WorkbenchProvider");
  return ctx;
}
