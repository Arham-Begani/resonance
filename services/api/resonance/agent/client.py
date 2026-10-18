"""Nebius Token Factory chat client (OpenAI-compatible) for the Nemotron lab partner.

- The model ID comes only from configuration (``NEBIUS_MODEL``); there is no fallback model.
- SDK-level retries are disabled (``max_retries=0``): the agent loop owns the retry policy so it
  can count retries against the run's declared cap and retry only transient failures.
- Provider exceptions are mapped to learner-safe ``ProviderError``s. Exception text from the SDK
  is never logged or returned (it can echo request details); only the type and status code are
  logged.
- Hidden reasoning (``reasoning_content`` / ``reasoning`` fields) is never read from responses.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
import openai

from ..api.errors import ApiException
from ..config import Settings
from ..contracts.common import ApiError, ErrorCode
from .status import ai_status

log = logging.getLogger("resonance.agent")

# Model-card recommendation for Nemotron 3 Super (reasoning on/off and tool calling):
# temperature 1.0, top_p 0.95. Source: huggingface.co/nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-FP8
DEFAULT_TEMPERATURE = 1.0
DEFAULT_TOP_P = 0.95

# Per-request HTTP timeouts. The loop additionally enforces the run's wall-time cap.
CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 90.0


@dataclass(frozen=True)
class ToolCallRequest:
    """One tool call requested by the model (arguments are the raw JSON text)."""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class CompletionUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class Completion:
    """The parts of a chat completion the loop is allowed to use.

    ``content`` is the raw visible content (it may still contain think-tags, which the loop
    strips). Provider reasoning fields are deliberately not copied here."""

    content: str | None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str | None = None
    usage: CompletionUsage | None = None


class ProviderError(Exception):
    """A failed model request, already mapped to a learner-safe ``ApiError``."""

    def __init__(self, error: ApiError, *, transient: bool) -> None:
        super().__init__(error.message)
        self.error = error
        self.transient = transient


class ChatClient(Protocol):
    """What the agent loop needs from a model client (tests may provide doubles)."""

    @property
    def model(self) -> str: ...

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, max_tokens: int
    ) -> Completion: ...

    async def aclose(self) -> None: ...


class NebiusChatClient:
    """Thin wrapper over ``openai.AsyncOpenAI`` pointed at Nebius Token Factory."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        http_client: httpx.AsyncClient | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> None:
        if not model.strip():
            raise ValueError("model must be configured")
        self._model = model.strip()
        self._extra_body = dict(extra_body or {})
        kwargs: dict[str, Any] = {
            "base_url": base_url,
            "api_key": api_key,
            "max_retries": 0,
            "timeout": httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
        }
        if http_client is not None:
            # The SDK accepts httpx.AsyncClient (legacy) as well as its own httpx2 client.
            kwargs["http_client"] = http_client
        self._sdk = openai.AsyncOpenAI(**kwargs)

    @property
    def model(self) -> str:
        return self._model

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, max_tokens: int
    ) -> Completion:
        params: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": DEFAULT_TEMPERATURE,
            "top_p": DEFAULT_TOP_P,
        }
        if tools:
            params["tools"] = tools
            params["tool_choice"] = "auto"
        if self._extra_body:
            params["extra_body"] = self._extra_body
        try:
            response = await self._sdk.chat.completions.create(**params)
        except openai.APIError as exc:
            raise map_provider_exception(exc) from None
        return parse_completion(response)

    async def list_models(self) -> list[str]:
        """Model IDs visible to this key (used by live verification only)."""
        try:
            page = await self._sdk.models.list()
        except openai.APIError as exc:
            raise map_provider_exception(exc) from None
        return [m.id for m in page.data]

    async def aclose(self) -> None:
        await self._sdk.close()


def parse_completion(response: Any) -> Completion:
    """Copy only content, tool calls, finish reason and usage from an SDK response."""
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise ProviderError(
            ApiError(code=ErrorCode.AI_ERROR, message="The AI model returned an empty response."),
            transient=False,
        )
    choice = choices[0]
    message = getattr(choice, "message", None)
    content = getattr(message, "content", None) if message is not None else None
    calls: list[ToolCallRequest] = []
    for index, call in enumerate(getattr(message, "tool_calls", None) or []):
        fn = getattr(call, "function", None)
        name = getattr(fn, "name", None) if fn is not None else None
        if not name:
            continue
        call_id = getattr(call, "id", None) or f"call_{index}"
        arguments = getattr(fn, "arguments", None) or ""
        calls.append(ToolCallRequest(id=str(call_id), name=str(name), arguments=str(arguments)))
    usage_obj = getattr(response, "usage", None)
    usage: CompletionUsage | None = None
    if usage_obj is not None and getattr(usage_obj, "total_tokens", None) is not None:
        usage = CompletionUsage(
            prompt_tokens=int(getattr(usage_obj, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(usage_obj, "completion_tokens", 0) or 0),
            total_tokens=int(usage_obj.total_tokens),
        )
    return Completion(
        content=content if isinstance(content, str) else None,
        tool_calls=calls,
        finish_reason=getattr(choice, "finish_reason", None),
        usage=usage,
    )


def map_provider_exception(exc: openai.APIError) -> ProviderError:
    """Classify an SDK exception. Only timeouts, connection errors, 429 and 5xx are transient."""
    status = getattr(exc, "status_code", None)
    log.warning("nebius request failed: %s status=%s", type(exc).__name__, status)
    if isinstance(exc, openai.APITimeoutError):
        return ProviderError(
            ApiError(
                code=ErrorCode.AI_ERROR,
                message="The AI model did not respond in time.",
                retryable=True,
            ),
            transient=True,
        )
    if isinstance(exc, openai.APIConnectionError):
        return ProviderError(
            ApiError(
                code=ErrorCode.AI_ERROR,
                message="Could not reach Nebius Token Factory.",
                retryable=True,
            ),
            transient=True,
        )
    if isinstance(exc, openai.RateLimitError):
        return ProviderError(
            ApiError(
                code=ErrorCode.AI_ERROR,
                message="Nebius Token Factory is rate limiting requests. Try again shortly.",
                retryable=True,
            ),
            transient=True,
        )
    if isinstance(exc, openai.InternalServerError):
        return ProviderError(
            ApiError(
                code=ErrorCode.AI_ERROR,
                message="Nebius Token Factory reported a temporary server error.",
                retryable=True,
            ),
            transient=True,
        )
    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        return ProviderError(
            ApiError(
                code=ErrorCode.AI_ERROR,
                message="Nebius Token Factory rejected the configured API key. Check "
                "NEBIUS_API_KEY in the server's .env file.",
            ),
            transient=False,
        )
    if isinstance(exc, openai.NotFoundError):
        return ProviderError(
            ApiError(
                code=ErrorCode.AI_ERROR,
                message="The configured model was not found on Nebius Token Factory. Check "
                "NEBIUS_MODEL.",
            ),
            transient=False,
        )
    return ProviderError(
        ApiError(code=ErrorCode.AI_ERROR, message="The AI model request was rejected."),
        transient=False,
    )


def ai_unavailable(settings: Settings) -> ApiException:
    status = ai_status(settings)
    if status.state == "dev_fixture":
        # No scripted fixture agent exists in this build; never pretend one is AI.
        return ApiException(
            ErrorCode.AI_UNAVAILABLE,
            "AI lab partner unavailable: development fixture mode is on, but this build has "
            "no fixture agent. Unset RESONANCE_DEV_AGENT_FIXTURE and set NEBIUS_API_KEY and "
            "NEBIUS_MODEL in the project .env to use Nemotron.",
        )
    return ApiException(ErrorCode.AI_UNAVAILABLE, status.message)


def create_chat_client(
    settings: Settings, *, http_client: httpx.AsyncClient | None = None
) -> NebiusChatClient:
    """Build the configured client or raise ``ai_unavailable`` (503) with setup instructions."""
    status = ai_status(settings)
    if not status.available or settings.nebius_api_key is None or not settings.nebius_model:
        raise ai_unavailable(settings)
    return NebiusChatClient(
        base_url=settings.nebius_base_url,
        api_key=settings.nebius_api_key.get_secret_value().strip(),
        model=settings.nebius_model,
        http_client=http_client,
    )
