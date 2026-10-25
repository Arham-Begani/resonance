// Learn mode state: prediction prompts for the current revision, the attempt being measured, and
// this tab's attempt history. Grading, explanations, and tradeoffs come from the server, which
// derives them from MEASURED (ngspice) tone gains; nothing here computes a result.
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ApiFailure, toFailure } from "../../api/client";
import { endpoints, type Endpoints } from "../../api/endpoints";
import type { LearnAttempt, LearnAttemptRequest, LearnPrompt } from "../../api/types";
import { useAnnouncer } from "../../state/announcer";
import { currentRevision, useWorkbench } from "../../state/workbench";

export interface LearnApi {
  prompts: LearnPrompt[] | null;
  promptsFor: string | null;
  loadingPrompts: boolean;
  promptsError: ApiFailure | null;
  attempts: LearnAttempt[];
  activeAttempt: LearnAttempt | null;
  running: boolean;
  runError: ApiFailure | null;
  reloadPrompts: () => void;
  submit: (request: LearnAttemptRequest) => Promise<LearnAttempt | null>;
  cancel: () => void;
  show: (attemptId: string | null) => void;
  clearError: () => void;
}

const LearnContext = createContext<LearnApi | null>(null);

const STORAGE_KEY = "resonance.learnAttempts";

function readIds(): string[] {
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter((x): x is string => typeof x === "string").slice(-20) : [];
  } catch {
    return [];
  }
}

function writeIds(ids: string[]): void {
  try {
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(ids.slice(-20)));
  } catch {
    // Per-viewer convenience only; the history simply starts empty after a reload.
  }
}

export function LearnProvider({ children, client = endpoints }: { children: ReactNode; client?: Endpoints }) {
  const wb = useWorkbench();
  const { announce, announceError } = useAnnouncer();
  const [prompts, setPrompts] = useState<LearnPrompt[] | null>(null);
  const [promptsFor, setPromptsFor] = useState<string | null>(null);
  const [loadingPrompts, setLoadingPrompts] = useState(false);
  const [promptsError, setPromptsError] = useState<ApiFailure | null>(null);
  const [attempts, setAttempts] = useState<LearnAttempt[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<ApiFailure | null>(null);
  const abort = useRef<AbortController | null>(null);

  const revision = currentRevision(wb.state);
  const learnMode = wb.state.mode === "learn";
  const revisionId = revision && !revision.frozen ? revision.id : null;

  const loadPrompts = useCallback(
    async (id: string) => {
      setLoadingPrompts(true);
      setPromptsError(null);
      try {
        const list = await client.learnPrompts(id);
        setPrompts(list);
        setPromptsFor(id);
      } catch (thrown) {
        setPromptsError(toFailure(thrown));
        setPrompts(null);
        setPromptsFor(id);
      } finally {
        setLoadingPrompts(false);
      }
    },
    [client],
  );

  // Prompts belong to a revision: (re)load them when Learn is open and the revision changes.
  useEffect(() => {
    if (!learnMode || !revisionId || promptsFor === revisionId || running) return;
    void loadPrompts(revisionId);
  }, [learnMode, revisionId, promptsFor, running, loadPrompts]);

  // Restore this tab's attempt history once (attempt records are session-scoped on the server).
  const restored = useRef(false);
  useEffect(() => {
    if (restored.current || wb.state.status !== "ready") return;
    restored.current = true;
    const ids = readIds();
    if (ids.length === 0) return;
    void Promise.all(ids.map((id) => client.getLearnAttempt(id).catch(() => null))).then((list) => {
      const found = list.filter((a): a is LearnAttempt => a !== null);
      setAttempts((prev) => [...found.filter((a) => !prev.some((p) => p.id === a.id)), ...prev]);
    });
  }, [wb.state.status, client]);

  const submit = useCallback(
    async (request: LearnAttemptRequest) => {
      abort.current?.abort();
      const controller = new AbortController();
      abort.current = controller;
      setRunning(true);
      setRunError(null);
      announce("Measuring the circuit before and after the change in ngspice…");
      try {
        const attempt = await client.createLearnAttempt(request, controller.signal);
        setAttempts((prev) => {
          const next = [...prev.filter((a) => a.id !== attempt.id), attempt];
          writeIds(next.map((a) => a.id));
          return next;
        });
        setActiveId(attempt.id);
        await wb.adoptLearnAttempt(attempt);
        announce(`Measured. ${attempt.score} of ${attempt.total} predictions matched the measurement.`);
        return attempt;
      } catch (thrown) {
        const failure = controller.signal.aborted
          ? new ApiFailure("cancelled", "The measurement was cancelled. Your predictions are still here; run it again when you are ready.", 0)
          : toFailure(thrown);
        setRunError(failure);
        if (failure.code === "cancelled") announce("Measurement cancelled.");
        else announceError(failure.message);
        return null;
      } finally {
        if (abort.current === controller) abort.current = null;
        setRunning(false);
      }
    },
    [client, wb, announce, announceError],
  );

  const api = useMemo<LearnApi>(
    () => ({
      prompts,
      promptsFor,
      loadingPrompts,
      promptsError,
      attempts,
      activeAttempt: attempts.find((a) => a.id === activeId) ?? null,
      running,
      runError,
      reloadPrompts: () => {
        if (revisionId) void loadPrompts(revisionId);
      },
      submit,
      cancel: () => abort.current?.abort(),
      show: (attemptId) => setActiveId(attemptId),
      clearError: () => setRunError(null),
    }),
    [prompts, promptsFor, loadingPrompts, promptsError, attempts, activeId, running, runError, revisionId, loadPrompts, submit],
  );

  return <LearnContext.Provider value={api}>{children}</LearnContext.Provider>;
}

export function useLearn(): LearnApi {
  const ctx = useContext(LearnContext);
  if (!ctx) throw new Error("useLearn must be used inside LearnProvider");
  return ctx;
}
