// Agent runs: start, stream genuine events over SSE (with a JSON log polling fallback), and
// cancel. One run view per scope ("explore" for the workbench, "case" for investigations).
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
import { agentRunEventsUrl, endpoints, type Endpoints } from "../../api/endpoints";
import type { AgentEvent, AgentEventType, AgentRun } from "../../api/types";
import { useAnnouncer } from "../../state/announcer";

export type RunScope = "explore" | "case";
export type Connection = "idle" | "starting" | "streaming" | "polling" | "closed";

export interface RunView {
  run: AgentRun | null;
  events: AgentEvent[];
  connection: Connection;
  startError: ApiFailure | null;
  cancelling: boolean;
}

const EMPTY: RunView = { run: null, events: [], connection: "idle", startError: null, cancelling: false };

const EVENT_TYPES: AgentEventType[] = [
  "run_started",
  "status",
  "tool_call",
  "tool_result",
  "message",
  "proposal",
  "diagnosis",
  "repair_proposal",
  "usage",
  "error",
  "run_finished",
];

const POLL_MS = 1500;

type Listener = (scope: RunScope, event: AgentEvent) => void;

interface AgentRunsApi {
  views: Record<RunScope, RunView>;
  start: (scope: RunScope, starter: () => Promise<AgentRun>) => Promise<AgentRun | null>;
  /** Attach to a run that already exists (e.g. a case's active_run_id after reload). */
  attach: (scope: RunScope, runId: string) => void;
  cancel: (scope: RunScope) => Promise<void>;
  clear: (scope: RunScope) => void;
  subscribe: (listener: Listener) => () => void;
}

const AgentRunsContext = createContext<AgentRunsApi | null>(null);

export function isRunActive(view: RunView | undefined): boolean {
  if (!view) return false;
  if (view.connection === "starting") return true;
  return view.run?.status === "running";
}

export function AgentRunsProvider({ children, client = endpoints }: { children: ReactNode; client?: Endpoints }) {
  const [views, setViews] = useState<Record<RunScope, RunView>>({ explore: EMPTY, case: EMPTY });
  const listeners = useRef(new Set<Listener>());
  const streams = useRef<Partial<Record<RunScope, { close: () => void; runId: string }>>>({});
  const { announce } = useAnnouncer();

  const patch = useCallback((scope: RunScope, update: (v: RunView) => RunView) => {
    setViews((prev) => ({ ...prev, [scope]: update(prev[scope]) }));
  }, []);

  const emit = useCallback((scope: RunScope, event: AgentEvent) => {
    listeners.current.forEach((l) => l(scope, event));
  }, []);

  // Sequence numbers already applied, tracked synchronously so listeners fire exactly once.
  const seen = useRef<Record<RunScope, Set<number>>>({ explore: new Set(), case: new Set() });

  const merge = useCallback(
    (scope: RunScope, incoming: AgentEvent[], run?: AgentRun) => {
      const added = incoming.filter((e) => !seen.current[scope].has(e.seq));
      added.forEach((e) => seen.current[scope].add(e.seq));
      if (added.length === 0 && !run) return;
      const finished = [...added].reverse().find((e) => e.type === "run_finished");
      patch(scope, (v) => {
        const nextRun = finished && finished.type === "run_finished" ? finished.run : (run ?? v.run);
        return {
          ...v,
          events: [...v.events, ...added].sort((a, b) => a.seq - b.seq),
          run: nextRun,
          cancelling: nextRun?.status === "running" ? v.cancelling : false,
        };
      });
      added.forEach((e) => emit(scope, e));
    },
    [patch, emit],
  );

  const stopStream = useCallback((scope: RunScope) => {
    streams.current[scope]?.close();
    delete streams.current[scope];
  }, []);

  const reset = useCallback(
    (scope: RunScope, view: RunView) => {
      seen.current[scope] = new Set();
      setViews((prev) => ({ ...prev, [scope]: view }));
    },
    [],
  );

  const poll = useCallback(
    (scope: RunScope, runId: string) => {
      let stopped = false;
      let timer = 0;
      const tick = async () => {
        if (stopped) return;
        try {
          const log = await client.getAgentRunEventLog(runId);
          if (stopped) return;
          merge(scope, log.events, log.run);
          if (log.run.status !== "running") {
            patch(scope, (v) => ({ ...v, connection: "closed" }));
            stopped = true;
            return;
          }
        } catch {
          // Keep polling; a transient failure should not end the view.
        }
        timer = window.setTimeout(tick, POLL_MS);
      };
      patch(scope, (v) => ({ ...v, connection: "polling" }));
      void tick();
      streams.current[scope] = {
        runId,
        close: () => {
          stopped = true;
          window.clearTimeout(timer);
        },
      };
    },
    [client, merge, patch],
  );

  const connect = useCallback(
    (scope: RunScope, runId: string) => {
      stopStream(scope);
      if (typeof EventSource === "undefined") {
        poll(scope, runId);
        return;
      }
      const source = new EventSource(agentRunEventsUrl(runId), { withCredentials: true });
      let finished = false;
      const handle = (message: MessageEvent<string>) => {
        try {
          const event = JSON.parse(message.data) as AgentEvent;
          merge(scope, [event]);
          if (event.type === "run_finished") {
            finished = true;
            source.close();
            patch(scope, (v) => ({ ...v, connection: "closed" }));
          }
        } catch {
          // Ignore malformed frames; the log fallback can recover the full history.
        }
      };
      EVENT_TYPES.forEach((type) => source.addEventListener(type, handle as EventListener));
      source.onmessage = handle;
      source.onopen = () => patch(scope, (v) => ({ ...v, connection: "streaming" }));
      source.onerror = () => {
        if (finished) return;
        source.close();
        // Fall back to the JSON log, which also recovers anything missed while disconnected.
        poll(scope, runId);
      };
      streams.current[scope] = { runId, close: () => source.close() };
    },
    [merge, patch, poll, stopStream],
  );

  const start = useCallback<AgentRunsApi["start"]>(
    async (scope, starter) => {
      stopStream(scope);
      reset(scope, { ...EMPTY, connection: "starting" });
      try {
        const run = await starter();
        patch(scope, (v) => ({ ...v, run }));
        announce(run.kind === "hint" ? "Asking the AI lab partner for a hint…" : "AI lab partner started.");
        connect(scope, run.id);
        return run;
      } catch (error) {
        const failure = toFailure(error);
        patch(scope, () => ({ ...EMPTY, startError: failure }));
        announce(failure.message);
        return null;
      }
    },
    [announce, connect, patch, stopStream, reset],
  );

  const attach = useCallback(
    (scope: RunScope, runId: string) => {
      if (streams.current[scope]?.runId === runId) return;
      reset(scope, { ...EMPTY, connection: "starting" });
      client
        .getAgentRun(runId)
        .then((run) => {
          patch(scope, (v) => ({ ...v, run }));
          connect(scope, runId);
        })
        .catch((error: unknown) => patch(scope, () => ({ ...EMPTY, startError: toFailure(error) })));
    },
    [client, connect, patch, reset],
  );

  const cancel = useCallback(
    async (scope: RunScope) => {
      const run = views[scope].run;
      if (!run || run.status !== "running") return;
      patch(scope, (v) => ({ ...v, cancelling: true }));
      try {
        const updated = await client.cancelAgentRun(run.id);
        patch(scope, (v) => ({ ...v, run: updated.status === "running" ? v.run : updated }));
        announce("Cancelling the AI run…");
      } catch (error) {
        patch(scope, (v) => ({ ...v, cancelling: false }));
        announce(toFailure(error).message);
      }
    },
    [client, patch, views, announce],
  );

  const clear = useCallback(
    (scope: RunScope) => {
      stopStream(scope);
      reset(scope, EMPTY);
    },
    [stopStream, reset],
  );

  const subscribe = useCallback((listener: Listener) => {
    listeners.current.add(listener);
    return () => listeners.current.delete(listener);
  }, []);

  useEffect(
    () => () => {
      Object.values(streams.current).forEach((s) => s?.close());
    },
    [],
  );

  const announcedFinish = useRef<Record<string, boolean>>({});
  useEffect(() => {
    (Object.keys(views) as RunScope[]).forEach((scope) => {
      const run = views[scope].run;
      if (run && run.status !== "running" && !announcedFinish.current[run.id]) {
        announcedFinish.current[run.id] = true;
        announce(finishMessage(run));
      }
    });
  }, [views, announce]);

  const api = useMemo<AgentRunsApi>(() => ({ views, start, attach, cancel, clear, subscribe }), [views, start, attach, cancel, clear, subscribe]);
  return <AgentRunsContext.Provider value={api}>{children}</AgentRunsContext.Provider>;
}

export function finishMessage(run: AgentRun): string {
  switch (run.status) {
    case "completed":
      return "The AI run finished.";
    case "cancelled":
      return "The AI run was cancelled.";
    case "limit_reached":
      return "The AI run stopped at a declared limit.";
    case "failed":
      return `The AI run failed${run.error ? `: ${run.error.message}` : "."}`;
    default:
      return "The AI run is still working.";
  }
}

export function useAgentRuns(): AgentRunsApi {
  const ctx = useContext(AgentRunsContext);
  if (!ctx) throw new Error("useAgentRuns must be used inside AgentRunsProvider");
  return ctx;
}

/** Subscribe to events from agent runs (stable callback recommended). */
export function useAgentEvents(listener: Listener): void {
  const { subscribe } = useAgentRuns();
  const ref = useRef(listener);
  ref.current = listener;
  useEffect(() => subscribe((scope, event) => ref.current(scope, event)), [subscribe]);
}
