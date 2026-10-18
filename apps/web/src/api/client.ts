// Typed API client. All requests go through openapi-fetch against the generated `paths`, and
// every failure becomes an `ApiFailure` carrying the backend's learner-safe ErrorResponse.
import createClient from "openapi-fetch";
import type { paths } from "./schema";
import type { ApiErrorBody, ErrorCode } from "./types";

export const api = createClient<paths>({ baseUrl: "", credentials: "same-origin" });

/** Codes the client adds for failures that never reached a structured backend response. */
export type ClientErrorCode = ErrorCode | "network_error" | "not_implemented";

export class ApiFailure extends Error {
  readonly code: ClientErrorCode;
  readonly status: number;
  readonly field: string | null;
  readonly retryable: boolean;

  constructor(code: ClientErrorCode, message: string, status: number, field: string | null = null, retryable = false) {
    super(message);
    this.name = "ApiFailure";
    this.code = code;
    this.status = status;
    this.field = field;
    this.retryable = retryable;
  }
}

function isErrorBody(value: unknown): value is { error: ApiErrorBody } {
  if (typeof value !== "object" || value === null || !("error" in value)) return false;
  const inner = (value as { error: unknown }).error;
  return typeof inner === "object" && inner !== null && "code" in inner && "message" in inner;
}

/** Convert any failed response (or thrown fetch error) into an ApiFailure. */
export function toFailure(error: unknown, response?: Response): ApiFailure {
  if (error instanceof ApiFailure) return error;
  const status = response?.status ?? 0;
  if (isErrorBody(error)) {
    const e = error.error;
    // The backend marks endpoints that are still being built with HTTP 501.
    const code: ClientErrorCode = status === 501 ? "not_implemented" : e.code;
    return new ApiFailure(code, e.message, status, e.field ?? null, e.retryable ?? false);
  }
  if (error instanceof DOMException && error.name === "AbortError") {
    return new ApiFailure("cancelled", "The request was cancelled.", 0);
  }
  if (status === 0) {
    return new ApiFailure(
      "network_error",
      "The Resonance server could not be reached. Check that the API is running, then try again.",
      0,
      null,
      true,
    );
  }
  // Non-JSON failures (e.g. a proxy or gateway page): keep the status for developers only.
  return new ApiFailure(
    "internal_error",
    "The server could not complete the request. Try again in a moment.",
    status,
    null,
    status >= 500,
  );
}

type Result<T> = { data?: T; error?: unknown; response: Response };

/** Await an openapi-fetch call and return its data, or throw an ApiFailure. */
export async function unwrap<T>(call: Promise<Result<T>>): Promise<T> {
  let result: Result<T>;
  try {
    result = await call;
  } catch (thrown) {
    throw toFailure(thrown);
  }
  if (result.error !== undefined || !result.response.ok) {
    throw toFailure(result.error, result.response);
  }
  if (result.data === undefined) {
    throw new ApiFailure("internal_error", "The server returned an empty response.", result.response.status);
  }
  return result.data;
}

export function failureMessage(error: unknown): string {
  if (error instanceof ApiFailure) return error.message;
  if (error instanceof Error) return error.message;
  return "Something went wrong.";
}

/** A short, calm title for each failure class (used by error callouts). */
export function failureTitle(error: unknown): string {
  const code = error instanceof ApiFailure ? error.code : "internal_error";
  switch (code) {
    case "validation_error":
      return "Check the value";
    case "simulation_timeout":
      return "Simulation timed out";
    case "simulation_failed":
      return "Simulation failed";
    case "simulator_unavailable":
      return "Simulator unavailable";
    case "ai_unavailable":
      return "AI lab partner unavailable";
    case "ai_error":
      return "AI request failed";
    case "limit_reached":
      return "Limit reached";
    case "budget_exhausted":
      return "No experiments left";
    case "run_in_progress":
      return "The AI is working on this case";
    case "stale_revision":
      return "This revision can’t be changed";
    case "cancelled":
      return "Cancelled";
    case "not_found":
      return "Not found";
    case "conflict":
      return "Not available right now";
    case "network_error":
      return "Server unreachable";
    case "not_implemented":
      return "Not available yet";
    default:
      return "Something went wrong";
  }
}
