"""Structured API errors (owner: lead). Messages are learner-safe by construction."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..contracts.common import ApiError, ErrorCode, ErrorResponse
from ..contracts.internal import (
    SimulationCancelled,
    SimulationError,
    SimulationTimeout,
    SimulatorUnavailable,
)

log = logging.getLogger("resonance.api")

_STATUS: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_ERROR: 422,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.CONFLICT: 409,
    ErrorCode.STALE_REVISION: 409,
    ErrorCode.BUDGET_EXHAUSTED: 409,
    ErrorCode.RUN_IN_PROGRESS: 409,
    ErrorCode.SIMULATION_FAILED: 502,
    ErrorCode.SIMULATION_TIMEOUT: 504,
    ErrorCode.SIMULATOR_UNAVAILABLE: 503,
    ErrorCode.AI_UNAVAILABLE: 503,
    ErrorCode.AI_ERROR: 502,
    ErrorCode.LIMIT_REACHED: 429,
    ErrorCode.CANCELLED: 409,
    ErrorCode.INTERNAL_ERROR: 500,
}


class ApiException(Exception):
    """Raise from route/service code to return a structured error."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        field: str | None = None,
        retryable: bool = False,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.error = ApiError(code=code, message=message, field=field, retryable=retryable)
        self.status = status if status is not None else _STATUS[code]


def not_found(what: str = "Item") -> ApiException:
    return ApiException(ErrorCode.NOT_FOUND, f"{what} was not found.")


def not_implemented(feature: str) -> ApiException:
    """Temporary development stub marker. Must not remain in an accepted milestone."""
    return ApiException(ErrorCode.INTERNAL_ERROR, f"{feature} is not implemented yet.", status=501)


def simulation_error_to_api(exc: SimulationError) -> ApiException:
    if isinstance(exc, SimulatorUnavailable):
        return ApiException(
            ErrorCode.SIMULATOR_UNAVAILABLE,
            "The circuit simulator is not available on this server.",
        )
    if isinstance(exc, SimulationTimeout):
        return ApiException(
            ErrorCode.SIMULATION_TIMEOUT,
            "The simulation took too long and was stopped.",
            retryable=True,
        )
    if isinstance(exc, SimulationCancelled):
        return ApiException(ErrorCode.CANCELLED, "The simulation was cancelled.")
    return ApiException(
        ErrorCode.SIMULATION_FAILED, "The simulator could not complete this run.", retryable=True
    )


def _response(status: int, error: ApiError) -> JSONResponse:
    return JSONResponse(status_code=status, content=ErrorResponse(error=error).model_dump())


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiException)
    async def _api_exc(_: Request, exc: ApiException) -> JSONResponse:
        return _response(exc.status, exc.error)

    @app.exception_handler(SimulationError)
    async def _sim_exc(_: Request, exc: SimulationError) -> JSONResponse:
        api_exc = simulation_error_to_api(exc)
        return _response(api_exc.status, api_exc.error)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = exc.errors()
        first = errors[0] if errors else {}
        loc = [str(p) for p in first.get("loc", ()) if p not in ("body", "query", "path")]
        field = ".".join(loc) or None
        msg = str(first.get("msg", "Invalid request."))
        message = f"{field}: {msg}" if field else msg
        return _response(
            422, ApiError(code=ErrorCode.VALIDATION_ERROR, message=message, field=field)
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = ErrorCode.NOT_FOUND if exc.status_code == 404 else ErrorCode.INTERNAL_ERROR
        message = "Not found." if exc.status_code == 404 else "Request failed."
        return _response(exc.status_code, ApiError(code=code, message=message))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        # Log the type only: exception text from deep layers could contain hidden values.
        log.error("unhandled error: %s", type(exc).__name__)
        return _response(
            500, ApiError(code=ErrorCode.INTERNAL_ERROR, message="Something went wrong.")
        )
