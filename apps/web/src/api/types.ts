// Friendly aliases for the generated OpenAPI schema types. Never hand-write API shapes here:
// every alias points at `schema.d.ts`, which `npm run gen:api` regenerates from the backend.
import type { components } from "./schema";

// openapi-fetch types response data as Readable<Schema>, which turns tuples into arrays. This
// local equivalent (our schema has no readOnly/writeOnly markers) keeps one consistent shape.
type Plain<T> = T extends (infer E)[] ? Plain<E>[] : T extends object ? { [K in keyof T]: Plain<T[K]> } : T;
type S = { [K in keyof components["schemas"]]: Plain<components["schemas"][K]> };

export type ApiErrorBody = S["ApiError"];
export type ErrorCode = ApiErrorBody["code"];

export type HealthResponse = S["HealthResponse"];
export type AiStatus = S["AiStatus"];
export type SimulatorStatus = S["SimulatorStatus"];

export type Revision = S["Revision"];
export type Component = S["Component"];
export type ComponentId = Component["id"];
export type EditableComponentId = S["ComponentValueInput"]["component_id"];
export type NodeId = S["CircuitNode"]["id"];
export type CircuitNode = S["CircuitNode"];
export type ComponentChange = S["ComponentChange"];
export type ValueParseResult = S["ValueParseResult"];
export type CircuitTemplate = S["CircuitTemplate"];
export type TemplateId = CircuitTemplate["id"];
export type Tone = S["Tone"];

export type WorkbenchState = S["WorkbenchState"];
export type Goal = S["Goal"];
export type GoalConstraint = S["GoalConstraint"];
export type GoalInput = S["GoalInput"];
export type FeasibilityAnalysis = S["FeasibilityAnalysis"];
export type GoalEvaluation = S["GoalEvaluation"];
export type ConstraintResult = S["ConstraintResult"];

export type ExperimentRecord = S["ExperimentRecord"];
export type ExperimentSummary = S["ExperimentSummary"];
export type ExperimentComparison = S["ExperimentComparison"];
export type FrequencyResponse = S["FrequencyResponse"];
export type ToneLevel = S["ToneLevel"];
export type WaveformWindow = S["WaveformWindow"];
export type Spectrum = S["Spectrum"];
export type AudioClip = S["AudioClip"];
export type AudioComparison = S["AudioComparison"];
export type ClipRole = AudioClip["role"];
export type EvidenceRef = S["EvidenceRef"];

export type EditProposal = S["EditProposal"];

export type DesignSuggestion = S["DesignSuggestion"];
export type DesignSuggestionRequest = S["DesignSuggestionRequest"];
export type PredictedGoalTone = S["PredictedGoalTone"];
export type SuggestedValue = S["SuggestedValue"];

export type LearnPrompt = S["LearnPrompt"];
export type LearnChange = S["LearnChange"];
export type LearnAttempt = S["LearnAttempt"];
export type LearnAttemptRequest = S["LearnAttemptRequest"];
export type GradedPrediction = S["GradedPrediction"];
export type TonePredictionInput = S["TonePredictionInput"];
export type Expectation = TonePredictionInput["expectation"];
export type ToneDelta = S["ToneDelta"];

export type PublicCase = S["PublicCase"];
export type CaseStatus = PublicCase["status"];
export type CaseAction = PublicCase["available_actions"][number];
export type InstrumentSpec = S["InstrumentSpec"];
export type InstrumentKind = InstrumentSpec["kind"];
export type Observation = S["Observation"];
export type SweepObservation = S["SweepObservation"];
export type TransientObservation = S["TransientObservation"];
export type ObservedTone = S["ObservedTone"];
export type Hypothesis = S["Hypothesis"];
export type HypothesisId = Hypothesis["id"];
export type HypothesisAssessment = S["HypothesisAssessment"];
export type HypothesisCheck = S["HypothesisCheck"];
export type EvidenceState = S["EvidenceState"];
export type DiagnosisSubmission = S["DiagnosisSubmission"];
export type DiagnosisConclusion = DiagnosisSubmission["conclusion"];
export type FaultHypothesisId = NonNullable<DiagnosisSubmission["hypothesis_id"]>;
export type EvidenceCitation = S["EvidenceCitation"];
export type DiagnosisRecord = S["DiagnosisRecord"];
export type RepairProposal = S["RepairProposal"];
export type RepairProposalInput = S["RepairProposalInput"];
export type RepairVerification = S["RepairVerification"];
export type CaseReveal = S["CaseReveal"];
export type Budget = S["Budget"];

export type AgentRun = S["AgentRun"];
export type AgentRunKind = AgentRun["kind"];
export type AgentRunStatus = AgentRun["status"];
export type AgentEventLog = S["AgentEventLog"];
export type AgentEvent = AgentEventLog["events"][number];
export type AgentEventType = AgentEvent["type"];
export type AgentUsage = S["AgentUsage"];
