// Workbench store: session revisions, experiments, goal, proposals, circuit families, and the
// shared UI selection. React context + reducer; all server data comes from the typed endpoints.
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
  CircuitTemplate,
  ComponentId,
  DesignSuggestion,
  EditProposal,
  EditableComponentId,
  ExperimentRecord,
  ExperimentSummary,
  FeasibilityAnalysis,
  Goal,
  GoalInput,
  HealthResponse,
  LearnAttempt,
  NodeId,
  Revision,
  TemplateId,
  WorkbenchState,
} from "../api/types";
import { FAMILY_LABEL } from "../lib/family";
import { useAnnouncer } from "./announcer";

export type Mode = "explore" | "learn" | "investigate";
/** What the active probe measures: a node voltage or the source current. */
export type Probe = { kind: "node"; node: NodeId } | { kind: "current"; component: "V1" } | null;
export type Operation = "init" | "revision" | "experiment" | "goal" | "proposal" | "compare" | "template";

export interface OperationError {
  op: Operation;
  error: ApiFailure;
}

export interface WorkbenchData {
  status: "loading" | "ready" | "error";
  health: HealthResponse | null;
  healthError: ApiFailure | null;
  templates: CircuitTemplate[];
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
  /** Family switches: a new family's root revision → the revision it was switched from (for undo). */
  switchOrigins: Record<string, string>;
  /** Why the goal just changed without the learner editing it (never a silent change). */
  goalNotice: string | null;
}

type Action =
  | { type: "health"; health: HealthResponse | null; error: ApiFailure | null }
  | { type: "templates"; templates: CircuitTemplate[] }
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
  | { type: "sync"; revisions: Revision[]; experiments: ExperimentSummary[] }
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
  | { type: "probe"; probe: Probe }
  | { type: "switch_origin"; revisionId: string; fromId: string }
  | { type: "goal_notice"; text: string | null };

const MODE_HASH: Record<Mode, string> = { explore: "", learn: "#learn", investigate: "#investigate" };

function modeFromHash(hash: string): Mode {
  if (hash === "#investigate") return "investigate";
  if (hash === "#learn") return "learn";
  return "explore";
}

function initialMode(): Mode {
  return typeof window !== "undefined" ? modeFromHash(window.location.hash) : "explore";
}

const SWITCH_STORAGE_KEY = "resonance.switchOrigins";

/** Per-viewer convenience only: remembers which revision a family switch came from, for undo. */
function readSwitchOrigins(): Record<string, string> {
  try {
    const raw = window.sessionStorage.getItem(SWITCH_STORAGE_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : {};
    return parsed && typeof parsed === "object" ? (parsed as Record<string, string>) : {};
  } catch {
    return {};
  }
}

function writeSwitchOrigins(map: Record<string, string>): void {
  try {
    window.sessionStorage.setItem(SWITCH_STORAGE_KEY, JSON.stringify(map));
  } catch {
    // Storage unavailable (private window, blocked site data): undo falls back to creation order.
  }
}

export function createInitialState(): WorkbenchData {
  return {
    status: "loading",
    health: null,
    healthError: null,
    templates: [],
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
    switchOrigins: typeof window !== "undefined" ? readSwitchOrigins() : {},
    goalNotice: null,
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
    case "templates":
      return { ...state, templates: action.templates };
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
    case "sync": {
      // Server lists are authoritative; keep any locally known experiment the list lacks.
      const known = new Set(action.experiments.map((e) => e.id));
      return {
        ...state,
        revisions: action.revisions,
        experiments: [...action.experiments, ...state.experiments.filter((e) => !known.has(e.id))],
      };
    }
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
    case "switch_origin":
      return { ...state, switchOrigins: { ...state.switchOrigins, [action.revisionId]: action.fromId } };
    case "goal_notice":
      return { ...state, goalNotice: action.text };
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

/**
 * Server prose may cite result IDs (e.g. "exp_282f…") for traceability. Learners see the same
 * numbering as everywhere else in the workbench ("Experiment 3", "Revision 2"), never raw IDs.
 */
export function readableIds(state: WorkbenchData, text: string): string {
  // Backend IDs are prefix + token_urlsafe(12): 16 characters of [A-Za-z0-9_-].
  return text.replace(/(?<![A-Za-z0-9_-])(exp|rev)_[A-Za-z0-9_-]{6,16}/g, (id, kind: string) => {
    if (kind === "exp") {
      const n = experimentOrdinal(state, id);
      return n ? `Experiment ${n}` : "an experiment";
    }
    const n = revisionOrdinal(state, id);
    return n ? `Revision ${n}` : "a revision";
  });
}

export function currentRevision(state: WorkbenchData): Revision | null {
  return state.revisions.find((r) => r.id === state.currentRevisionId) ?? null;
}

export function revisionById(state: WorkbenchData, revisionId: string | null | undefined): Revision | null {
  return revisionId ? (state.revisions.find((r) => r.id === revisionId) ?? null) : null;
}

export function shownRecord(state: WorkbenchData): ExperimentRecord | null {
  return state.shownExperimentId ? (state.records[state.shownExperimentId] ?? null) : null;
}

export function templateFor(state: WorkbenchData, templateId: TemplateId | null | undefined): CircuitTemplate | null {
  return templateId ? (state.templates.find((t) => t.id === templateId) ?? null) : null;
}

/**
 * Where Undo goes from a revision: its parent; for the root of a family switch, the revision it
 * was switched from (remembered for this tab, otherwise the revision created just before it).
 */
export function undoTargetId(state: WorkbenchData, revisionId: string | null | undefined): string | null {
  const rev = revisionById(state, revisionId);
  if (!rev) return null;
  if (rev.parent_id) return rev.parent_id;
  const explore = exploreRevisions(state);
  const remembered = state.switchOrigins[rev.id];
  if (remembered && explore.some((r) => r.id === remembered)) return remembered;
  const index = explore.findIndex((r) => r.id === rev.id);
  // Only a later root came from a switch; the very first revision is the original circuit.
  if (index > 0 && explore[index - 1]!.template_id !== rev.template_id) return explore[index - 1]!.id;
  return null;
}

function latestSucceededFor(experiments: ExperimentSummary[], revisionId: string): ExperimentSummary | null {
  for (let i = experiments.length - 1; i >= 0; i--) {
    const e = experiments[i]!;
    if (e.revision_id === revisionId && e.status === "succeeded") return e;
  }
  return null;
}

function goalInput(goal: Goal): GoalInput {
  return { title: goal.title, constraints: goal.constraints.map((c) => ({ ...c })) };
}

// ---------- controller ----------

export interface RunOptions {
  /** The automatic first measurement of an opening experiment (simulation only; never plays). */
  opening?: boolean;
  /** Explicit target, used when the run starts in the same tick as the state that names it. */
  revisionId?: string;
  goalId?: string | null;
}

export interface WorkbenchApi {
  state: WorkbenchData;
  reload: () => Promise<void>;
  refreshHealth: () => Promise<void>;
  applyValues: (values: { component_id: EditableComponentId; value: string | number }[]) => Promise<boolean>;
  runExperiment: (options?: RunOptions) => Promise<ExperimentRecord | null>;
  cancelExperiment: () => void;
  showExperiment: (experimentId: string) => Promise<void>;
  loadExperiment: (experimentId: string) => Promise<ExperimentRecord | null>;
  undo: () => void;
  selectRevision: (revisionId: string) => Promise<void>;
  switchTemplate: (templateId: TemplateId) => Promise<boolean>;
  setGoal: (input: GoalInput) => Promise<boolean>;
  dismissGoalNotice: () => void;
  /** Re-read the session goal from the server (e.g. after the AI's set_goal tool changed it). */
  syncGoal: () => Promise<void>;
  applySuggestion: (suggestion: DesignSuggestion) => Promise<boolean>;
  adoptLearnAttempt: (attempt: LearnAttempt) => Promise<void>;
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
  // The goal each family last had in this tab, restored when the learner returns to it.
  const goalMemory = useRef(new Map<TemplateId, GoalInput>());

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

  /** Apply the goal/feasibility the server returns with the workbench state (it is authoritative). */
  const adoptGoal = useCallback((wb: Pick<WorkbenchState, "goal" | "feasibility">) => {
    dispatch({ type: "goal", goal: wb.goal, feasibility: wb.feasibility });
  }, []);

  // The revision the server records as current. Persisting is explicit and ordered, because the
  // server analyses goal feasibility for the family of its current revision.
  const persisted = useRef<string | null>(null);
  const persistCurrent = useCallback(
    async (revisionId: string): Promise<WorkbenchState | null> => {
      if (persisted.current === revisionId) return null;
      persisted.current = revisionId;
      try {
        const wb = await client.setCurrentRevision(revisionId);
        if (wb && persisted.current === revisionId) adoptGoal(wb);
        return wb ?? null;
      } catch {
        // Non-critical: the client still shows the right revision; a reload may fall back.
        return null;
      }
    },
    [client, adoptGoal],
  );

  /** When the current family changes, remember the old family's goal and restore the new one's. */
  const syncGoalForFamily = useCallback(
    async (from: TemplateId | null, to: TemplateId): Promise<string | null> => {
      const s = stateRef.current;
      if (from === to) return s.goal?.id ?? null;
      if (from && s.goal) goalMemory.current.set(from, goalInput(s.goal));
      const target = goalMemory.current.get(to) ?? templateFor(s, to)?.default_goal ?? null;
      if (!target) return s.goal?.id ?? null;
      try {
        const res = await client.setGoal(target);
        dispatch({ type: "goal", goal: res.goal, feasibility: res.feasibility });
        const text = `Goal for the ${FAMILY_LABEL[to].toLowerCase()} circuit: “${res.goal.title}”${from ? ` (the ${FAMILY_LABEL[from].toLowerCase()} goal returns when you go back)` : ""}.`;
        dispatch({ type: "goal_notice", text });
        announce(text);
        return res.goal.id;
      } catch (error) {
        fail("goal", error);
        return s.goal?.id ?? null;
      }
    },
    [client, fail, announce],
  );

  const runRef = useRef<WorkbenchApi["runExperiment"] | null>(null);

  // One bootstrap/reload at a time: a second caller (e.g. React StrictMode re-running effects)
  // joins the in-flight promise instead of creating another session or original revision.
  const reloadInFlight = useRef<Promise<void> | null>(null);

  const reload = useCallback((): Promise<void> => {
    if (reloadInFlight.current) return reloadInFlight.current;
    const task = (async () => {
      dispatch({ type: "pending", op: "init" });
      // Templates are needed for family switching and default goals; the workbench works without.
      client
        .templates()
        .then((templates) => dispatch({ type: "templates", templates }))
        .catch(() => undefined);
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
        persisted.current = remembered;
        const firstVisit = wb.experiments.length === 0 && exploreAll.length === 1;
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
        // First visit: the opening experiment is measured right away (simulation only; nothing
        // plays until the learner presses Play). Never during an investigation deep link. It
        // starts in the same tick as "loaded", so Run is never briefly offered before it.
        if (firstVisit && current && stateRef.current.mode !== "investigate") {
          void runRef.current?.({ opening: true, revisionId: current, goalId: wb.goal.id });
        }
        // Persist the initially shown revision so a reload shows the same one.
        if (current && current !== remembered) void persistCurrent(current);
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
  }, [client, loadRecord, announceError, persistCurrent]);

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

  // Deep links: #learn and #investigate open those modes (also when the hash changes later).
  useEffect(() => {
    const onHash = () => dispatch({ type: "mode", mode: modeFromHash(window.location.hash) });
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    const hash = MODE_HASH[state.mode];
    if (window.location.hash !== hash) {
      window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}${hash}`);
    }
  }, [state.mode]);

  useEffect(() => {
    writeSwitchOrigins(state.switchOrigins);
  }, [state.switchOrigins]);

  const applyValues = useCallback<WorkbenchApi["applyValues"]>(
    async (values) => {
      const parent = stateRef.current.currentRevisionId;
      if (!parent) return false;
      dispatch({ type: "pending", op: "revision" });
      dispatch({ type: "error", error: null });
      try {
        const revision = await client.createRevision({ parent_id: parent, values, origin: "user_edit" });
        dispatch({ type: "revision_added", revision });
        void persistCurrent(revision.id);
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
    [client, announce, fail, persistCurrent],
  );

  const experimentAbort = useRef<AbortController | null>(null);

  const runExperiment = useCallback(
    async (options: RunOptions = {}) => {
      const s = stateRef.current;
      const revisionId = options.revisionId ?? s.currentRevisionId;
      const goalId = options.goalId !== undefined ? options.goalId : (s.goal?.id ?? null);
      if (!revisionId) return null;
      experimentAbort.current?.abort();
      const controller = new AbortController();
      experimentAbort.current = controller;
      dispatch({ type: "pending", op: "experiment" });
      dispatch({ type: "error", error: null });
      announce(options.opening ? "Measuring the opening experiment in ngspice…" : "Running the experiment in ngspice…");
      try {
        const record = await client.runExperiment({ revision_id: revisionId, goal_id: goalId }, controller.signal);
        dispatch({ type: "record", record, show: record.status === "succeeded" });
        if (record.status === "succeeded") {
          const goalText = record.goal ? (record.goal.passed ? " Goal met." : " Goal not met.") : "";
          announce(
            options.opening
              ? `Opening experiment measured.${goalText} Nothing plays until you press Play.`
              : `Experiment finished.${goalText}`,
          );
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
            .then((wb) => adoptGoal(wb))
            .catch(() => undefined);
        }
        if (controller.signal.aborted) {
          // Dropping the request cancels the server-side simulation (verified by review D-12).
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
    },
    [client, announce, fail, adoptGoal],
  );
  runRef.current = runExperiment;

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

  const loadExperiment = useCallback(
    async (experimentId: string) => {
      try {
        return await loadRecord(experimentId, false);
      } catch {
        return null;
      }
    },
    [loadRecord],
  );

  /** Make a known revision current: show its latest result, persist, and keep the goal in its family. */
  const moveTo = useCallback(
    async (revisionId: string) => {
      const s = stateRef.current;
      const from = currentRevision(s);
      const to = revisionById(s, revisionId);
      dispatch({ type: "set_current", revisionId });
      const latest = latestSucceededFor(s.experiments, revisionId);
      if (latest) void showExperiment(latest.id);
      await persistCurrent(revisionId);
      if (to) await syncGoalForFamily(from?.template_id ?? null, to.template_id);
    },
    [showExperiment, persistCurrent, syncGoalForFamily],
  );

  const selectRevision = useCallback((revisionId: string) => moveTo(revisionId), [moveTo]);

  const undo = useCallback(() => {
    const s = stateRef.current;
    const target = undoTargetId(s, s.currentRevisionId);
    if (!target) return;
    void moveTo(target);
    const rev = revisionById(s, target);
    const cur = currentRevision(s);
    const family = rev && cur && rev.template_id !== cur.template_id ? ` (${FAMILY_LABEL[rev.template_id].toLowerCase()})` : "";
    announce(`Back to Revision ${revisionOrdinal(s, target) ?? ""}${family}.`);
  }, [moveTo, announce]);

  const switchTemplate = useCallback(
    async (templateId: TemplateId) => {
      const s = stateRef.current;
      const from = currentRevision(s);
      if (!from || from.template_id === templateId) return true;
      dispatch({ type: "pending", op: "template" });
      dispatch({ type: "error", error: null });
      let opening: RunOptions | null = null;
      try {
        // Return to this family's latest revision if the session has one; otherwise create its root.
        const existing = [...exploreRevisions(s)].reverse().find((r) => r.template_id === templateId) ?? null;
        let target: Revision;
        if (existing) {
          target = existing;
          await moveTo(existing.id);
        } else {
          target = await client.createRevision({ template_id: templateId, values: [], origin: "user_edit" });
          dispatch({ type: "revision_added", revision: target });
          dispatch({ type: "switch_origin", revisionId: target.id, fromId: from.id });
          await persistCurrent(target.id);
          const goalId = await syncGoalForFamily(from.template_id, templateId);
          // A new family starts with its opening experiment measured (simulation only, no audio).
          opening = { opening: true, revisionId: target.id, goalId };
        }
        const ordinal = revisionOrdinal(stateRef.current, target.id);
        const goal = stateRef.current.goal?.title;
        announce(`${target.title}, Revision ${ordinal ?? ""}.${goal ? ` Goal: ${goal}` : ""}`);
      } catch (error) {
        fail("template", error);
        return false;
      } finally {
        dispatch({ type: "pending", op: null });
      }
      // Same tick as clearing "template", so the switch flows straight into the measurement.
      if (opening) void runRef.current?.(opening);
      return true;
    },
    [client, moveTo, persistCurrent, syncGoalForFamily, announce, fail],
  );

  const setGoal = useCallback(
    async (input: GoalInput) => {
      dispatch({ type: "pending", op: "goal" });
      dispatch({ type: "error", error: null });
      try {
        const res = await client.setGoal(input);
        dispatch({ type: "goal", goal: res.goal, feasibility: res.feasibility });
        dispatch({ type: "goal_notice", text: null });
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

  const applySuggestion = useCallback(
    async (suggestion: DesignSuggestion) => {
      const changed = suggestion.values.filter((v) => v.series !== "unchanged");
      const rev = currentRevision(stateRef.current);
      const differs = changed.filter((v) => rev?.components.find((c) => c.id === v.component_id)?.value_si !== v.value_si);
      if (differs.length === 0) {
        announce("The circuit already has the suggested values.");
        return false;
      }
      return applyValues(differs.map((v) => ({ component_id: v.component_id, value: v.value_si })));
    },
    [applyValues, announce],
  );

  const syncGoal = useCallback(async () => {
    try {
      adoptGoal(await client.workbench());
    } catch {
      // Non-critical: the next reload shows the server's goal.
    }
  }, [client, adoptGoal]);

  const adoptLearnAttempt = useCallback(
    async (attempt: LearnAttempt) => {
      try {
        const wb = await client.workbench();
        dispatch({ type: "sync", revisions: wb.revisions, experiments: wb.experiments });
      } catch {
        // The attempt still carries everything needed to show its result.
      }
      await Promise.all([loadRecord(attempt.base_experiment_id, false).catch(() => null), loadRecord(attempt.new_experiment_id, false).catch(() => null)]);
      await moveTo(attempt.new_revision_id);
      dispatch({ type: "show_experiment", experimentId: attempt.new_experiment_id });
    },
    [client, loadRecord, moveTo],
  );

  const applyProposal = useCallback(
    async (proposal: EditProposal) => {
      dispatch({ type: "pending", op: "proposal" });
      dispatch({ type: "error", error: null });
      try {
        const from = currentRevision(stateRef.current);
        const goalBefore = stateRef.current.goal;
        const revision = await client.createRevision({
          parent_id: proposal.base_revision_id,
          values: proposal.changes.map((c) => ({ component_id: c.component_id, value: c.to_value_si })),
          origin: "agent_proposal_applied",
          proposal_id: proposal.id,
          template_id: proposal.template_id ?? null,
        });
        dispatch({ type: "revision_added", revision });
        dispatch({ type: "proposal_removed", proposalId: proposal.id });
        // The server analyses the CURRENT goal for the new revision's family when it becomes current.
        const wb = await persistCurrent(revision.id);
        let switched = "";
        if (from && from.template_id !== revision.template_id) {
          const family = FAMILY_LABEL[revision.template_id].toLowerCase();
          // Either way, going back to the old family restores this goal there.
          if (goalBefore) goalMemory.current.set(from.template_id, goalInput(goalBefore));
          const fallback = templateFor(stateRef.current, revision.template_id)?.default_goal ?? null;
          if (!wb || wb.feasibility.feasible || !fallback) {
            // The proposal was evaluated against this goal: keep it (also when feasibility is unknown).
            switched = ` Your goal stays the same${wb?.feasibility.feasible ? `: a ${family} filter can meet it (prediction)` : ""}.`;
          } else {
            const res = await client.setGoal(fallback);
            dispatch({ type: "goal", goal: res.goal, feasibility: res.feasibility });
            const text = `A ${family} filter cannot meet your previous goal (prediction), so the goal is now the ${family} opening goal: “${res.goal.title}”. Your previous goal returns if you go back to the ${FAMILY_LABEL[from.template_id].toLowerCase()} circuit.`;
            dispatch({ type: "goal_notice", text });
            switched = ` ${text}`;
          }
        }
        announce(`Proposal applied as a new revision.${switched} Run the experiment to measure it.`);
        return true;
      } catch (error) {
        fail("proposal", error);
        return false;
      } finally {
        dispatch({ type: "pending", op: null });
      }
    },
    [client, announce, fail, persistCurrent],
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
      loadExperiment,
      undo,
      selectRevision,
      switchTemplate,
      setGoal,
      dismissGoalNotice: () => dispatch({ type: "goal_notice", text: null }),
      syncGoal,
      applySuggestion,
      adoptLearnAttempt,
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
      loadExperiment,
      undo,
      selectRevision,
      switchTemplate,
      setGoal,
      syncGoal,
      applySuggestion,
      adoptLearnAttempt,
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
