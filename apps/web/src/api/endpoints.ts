// One typed function per backend operation. Feature code calls these (never fetch directly),
// so tests can replace this module and the UI stays in step with the generated contract.
import { api, unwrap } from "./client";
import type { components } from "./schema";
import type { InstrumentKind } from "./types";

type S = components["schemas"];

export const endpoints = {
  health: () => unwrap(api.GET("/api/health")),
  templates: () => unwrap(api.GET("/api/templates")),
  workbench: () => unwrap(api.GET("/api/workbench")),
  setGoal: (body: S["GoalInput"]) => unwrap(api.PUT("/api/workbench/goal", { body })),
  setCurrentRevision: (revisionId: string) =>
    unwrap(api.PUT("/api/workbench/current-revision", { body: { revision_id: revisionId } })),
  parseValue: (body: S["ValueParseRequest"], signal?: AbortSignal) =>
    unwrap(api.POST("/api/units/parse", { body, signal })),

  createRevision: (body: S["RevisionCreateRequest"]) => unwrap(api.POST("/api/revisions", { body })),
  getRevision: (revisionId: string) =>
    unwrap(api.GET("/api/revisions/{revision_id}", { params: { path: { revision_id: revisionId } } })),

  runExperiment: (body: S["ExperimentRunRequest"], signal?: AbortSignal) =>
    unwrap(api.POST("/api/experiments", { body, signal })),
  getExperiment: (experimentId: string) =>
    unwrap(
      api.GET("/api/experiments/{experiment_id}", { params: { path: { experiment_id: experimentId } } }),
    ),
  compareExperiments: (a: string, b: string) =>
    unwrap(api.GET("/api/experiments/compare", { params: { query: { a, b } } })),

  createCase: () => unwrap(api.POST("/api/cases", { body: { template_id: "rc_lowpass" } })),
  getCase: (caseId: string) =>
    unwrap(api.GET("/api/cases/{case_id}", { params: { path: { case_id: caseId } } })),
  setCaseMode: (caseId: string, mode: "guide" | "agent") =>
    unwrap(api.POST("/api/cases/{case_id}/mode", { params: { path: { case_id: caseId } }, body: { mode } })),
  runCaseExperiment: (
    caseId: string,
    instrument: InstrumentKind,
    probe: S["CaseExperimentRequest"]["probe"],
    clientRequestId: string,
  ) =>
    unwrap(
      api.POST("/api/cases/{case_id}/experiments", {
        params: { path: { case_id: caseId } },
        body: { instrument, probe: probe ?? null, client_request_id: clientRequestId },
      }),
    ),
  submitDiagnosis: (caseId: string, body: S["DiagnosisSubmission"]) =>
    unwrap(api.POST("/api/cases/{case_id}/diagnosis", { params: { path: { case_id: caseId } }, body })),
  proposeRepair: (caseId: string, body: S["RepairProposalInput"]) =>
    unwrap(api.POST("/api/cases/{case_id}/repair-proposal", { params: { path: { case_id: caseId } }, body })),
  verifyRepair: (caseId: string) =>
    unwrap(api.POST("/api/cases/{case_id}/repair", { params: { path: { case_id: caseId } } })),
  closeCase: (caseId: string) =>
    unwrap(api.POST("/api/cases/{case_id}/close", { params: { path: { case_id: caseId } } })),

  startCaseAgentRun: (caseId: string, body: S["CaseAgentRunRequest"]) =>
    unwrap(api.POST("/api/cases/{case_id}/agent-runs", { params: { path: { case_id: caseId } }, body })),
  startExploreAgentRun: (body: S["ExploreRunRequest"]) => unwrap(api.POST("/api/explore/agent-runs", { body })),
  getAgentRun: (runId: string) =>
    unwrap(api.GET("/api/agent-runs/{run_id}", { params: { path: { run_id: runId } } })),
  getAgentRunEventLog: (runId: string) =>
    unwrap(api.GET("/api/agent-runs/{run_id}/events/log", { params: { path: { run_id: runId } } })),
  cancelAgentRun: (runId: string) =>
    unwrap(api.POST("/api/agent-runs/{run_id}/cancel", { params: { path: { run_id: runId } } })),
  dismissProposal: (proposalId: string) =>
    unwrap(api.POST("/api/proposals/{proposal_id}/dismiss", { params: { path: { proposal_id: proposalId } } })),
};

export type Endpoints = typeof endpoints;

/** URL of the server-sent event stream for an agent run (same origin, cookie-scoped). */
export function agentRunEventsUrl(runId: string): string {
  return `/api/agent-runs/${encodeURIComponent(runId)}/events`;
}
