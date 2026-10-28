"""One bounded Nemotron tool loop.

The model proposes; trusted code disposes:
- every tool call is looked up in the run's own ``ToolSet`` (anything else is refused) and its
  arguments are validated with Pydantic before execution (invalid calls are never executed;
  the validation error is returned to the model as the tool result);
- tools run sequentially;
- caps from ``AgentLimits`` are enforced: model calls, tool calls, experiments, total tokens
  (from measured ``usage`` only), wall time, and retries of transient provider errors;
- cancellation stops in-flight inference immediately and in-flight simulation cooperatively
  (through the run's ``CancelToken``), with a bounded grace period;
- only visible assistant text is emitted; think-tags are stripped and provider reasoning fields
  are never read. Cited result IDs are checked against recorded results;
- an optional ``FinishCheck`` (INVESTIGATE runs only) may answer a reply without tool calls with
  one bounded ``Reminder`` (a trusted-code user turn, announced as a ``status`` event, never as
  an agent message). Each reminder kind is sent at most once per run, only while another model
  call and tool call still fit inside the caps; reminders count against the same caps. If the
  model finishes again, the run ends as ``completed`` with that reply, exactly as without a
  check.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from pydantic import ValidationError

from ..contracts.agent import (
    AgentLimits,
    AgentRunStatus,
    AgentUsage,
    DiagnosisEvent,
    MessageEvent,
    ProposalEvent,
    RepairProposalEvent,
    StatusEvent,
    ToolCallEvent,
    ToolResultEvent,
    UsageEvent,
)
from ..contracts.common import ApiError, ErrorCode, QuantityName
from ..contracts.internal import CancelToken
from .client import ChatClient, Completion, ProviderError, ToolCallRequest
from .text import check_citations, strip_reasoning
from .tools import ToolOutcome, ToolSet

log = logging.getLogger("resonance.agent")

T = TypeVar("T")

# Upper bound for one reply (reasoning + visible text + tool calls).
MAX_COMPLETION_TOKENS = 4096
# Do not start a model call with less than this much of the token cap left.
MIN_TOKENS_FOR_CALL = 512
# After cancellation/wall time, how long a running tool may take to stop cooperatively.
TOOL_CANCEL_GRACE_S = 5.0
RETRY_BACKOFF_S = 1.0


class EventSink(Protocol):
    """Receives genuine run events; the service assigns seq/run_id/time."""

    def emit(self, event_type: type[Any], **fields: Any) -> Any: ...


CitationResolver = Callable[[], Awaitable[Mapping[str, QuantityName]]]


@dataclass(frozen=True)
class Reminder:
    """One bounded note from trusted code, sent to the model as an extra user turn.

    ``kind`` identifies the reminder (sent at most once per run); ``status`` is the learner-
    visible ``status`` event text; ``text`` is the user turn sent to the model. Neither may
    depend on hidden state.
    """

    kind: str
    status: str
    text: str


FinishCheck = Callable[[], Awaitable[Reminder | None]]
"""Called when the model ends a reply without tool calls; returns a reminder or ``None``."""


class StopSignal:
    """Cooperative stop request shared by the service (cancel) and the loop."""

    def __init__(self) -> None:
        self.event = asyncio.Event()
        self.token = CancelToken()

    def request(self) -> None:
        self.token.cancel()
        self.event.set()

    @property
    def requested(self) -> bool:
        return self.event.is_set()


@dataclass
class LoopResult:
    status: AgentRunStatus
    final_message: str | None = None
    error: ApiError | None = None


@dataclass
class _Stop(Exception):
    """Internal control-flow signal: end the loop with this status."""

    status: AgentRunStatus
    error: ApiError | None = None


@dataclass
class _Counters:
    tool_calls: int = 0
    experiments: int = 0
    retries: int = 0
    tool_serial: int = 0
    call_ids: set[str] = field(default_factory=set)


def _limit(message: str) -> _Stop:
    return _Stop("limit_reached", ApiError(code=ErrorCode.LIMIT_REACHED, message=message))


_CANCELLED = ApiError(code=ErrorCode.CANCELLED, message="The run was cancelled.")


class AgentLoop:
    def __init__(
        self,
        *,
        client: ChatClient,
        toolset: ToolSet,
        limits: AgentLimits,
        sink: EventSink,
        stop: StopSignal,
        usage: AgentUsage,
        resolve_citations: CitationResolver,
        finish_check: FinishCheck | None = None,
        clock: Callable[[], float] = time.monotonic,
        max_completion_tokens: int = MAX_COMPLETION_TOKENS,
        tool_cancel_grace_s: float | None = None,
        retry_backoff_s: float | None = None,
    ) -> None:
        self._client = client
        self._tools = toolset
        self._limits = limits
        self._sink = sink
        self._stop = stop
        self._usage = usage
        self._resolve_citations = resolve_citations
        self._finish_check = finish_check
        self._reminded: set[str] = set()
        self._clock = clock
        self._max_completion_tokens = max_completion_tokens
        self._grace = TOOL_CANCEL_GRACE_S if tool_cancel_grace_s is None else tool_cancel_grace_s
        self._backoff = RETRY_BACKOFF_S if retry_backoff_s is None else retry_backoff_s
        self._deadline = clock() + limits.wall_time_s
        self._n = _Counters()

    # ----------------------------------------------------------------------------------
    # Public entry point
    # ----------------------------------------------------------------------------------

    async def run(self, system_prompt: str, user_message: str) -> LoopResult:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        try:
            return await self._run(messages)
        except _Stop as stop:
            return LoopResult(status=stop.status, error=stop.error)

    # ----------------------------------------------------------------------------------
    # Loop
    # ----------------------------------------------------------------------------------

    async def _run(self, messages: list[dict[str, Any]]) -> LoopResult:
        schemas = self._tools.schemas()
        while True:
            completion = await self._call_model(messages, schemas)
            visible = strip_reasoning(completion.content)
            if visible:
                visible = await self._emit_message(visible)
            if not completion.tool_calls:
                if visible:
                    reminder = await self._reminder()
                    if reminder is None:
                        return LoopResult(status="completed", final_message=visible)
                    messages.append({"role": "assistant", "content": visible})
                    messages.append({"role": "user", "content": reminder.text})
                    self._sink.emit(StatusEvent, text=reminder.status)
                    continue
                reason = (
                    "The AI model used its whole reply allowance without a visible answer."
                    if completion.finish_reason == "length"
                    else "The AI model returned no visible answer."
                )
                raise _Stop("failed", ApiError(code=ErrorCode.AI_ERROR, message=reason))

            calls = _unique_ids(completion.tool_calls)
            messages.append(
                {
                    "role": "assistant",
                    "content": visible or None,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {"name": call.name, "arguments": call.arguments},
                        }
                        for call in calls
                    ],
                }
            )
            # Strictly sequential: only the first call of a reply is executed. Extra calls get
            # an error result (never executed) so the model can repeat them one at a time.
            first, extras = calls[0], calls[1:]
            self._check_stop()
            if self._n.tool_calls >= self._limits.max_tool_calls:
                raise _limit(
                    f"The run reached its limit of {self._limits.max_tool_calls} tool calls."
                )
            self._n.tool_calls += 1
            self._usage.tool_calls = self._n.tool_calls
            outcome, stopped = await self._execute(first.id, first.name, first.arguments)
            messages.append(_tool_message(first, outcome))
            if stopped is not None:
                raise stopped
            if outcome.fatal is not None:
                raise _Stop("failed", outcome.fatal)
            for extra in extras:
                refused = ToolOutcome.failure(
                    ApiError(
                        code=ErrorCode.VALIDATION_ERROR,
                        message="Not run: only one tool call per reply is executed. Call it "
                        "again in a separate reply if it is still needed.",
                    )
                )
                self._sink.emit(ToolCallEvent, call_id=extra.id, tool=extra.name, arguments={})
                self._emit_result(extra.id, extra.name, refused)
                messages.append(_tool_message(extra, refused))

    async def _reminder(self) -> Reminder | None:
        """The finish check's reminder, if one is due and another model call and tool call
        still fit inside every cap (otherwise the run simply completes, as without a check)."""
        if self._finish_check is None or not self._room_for_another_turn():
            return None
        try:
            reminder = await self._finish_check()
        except Exception as exc:  # noqa: BLE001 - a failed check never fails a finished run
            log.warning("agent finish check failed: %s", type(exc).__name__)
            return None
        if reminder is None or reminder.kind in self._reminded:
            return None
        self._reminded.add(reminder.kind)
        return reminder

    def _room_for_another_turn(self) -> bool:
        remaining_tokens = self._limits.max_total_tokens - self._usage.total_tokens
        return (
            not self._stop.requested
            and self._clock() < self._deadline
            and self._usage.model_calls < self._limits.max_model_calls
            and self._n.tool_calls < self._limits.max_tool_calls
            and remaining_tokens >= MIN_TOKENS_FOR_CALL
        )

    def _check_stop(self) -> None:
        if self._stop.requested:
            raise _Stop("cancelled", _CANCELLED)
        if self._clock() >= self._deadline:
            self._stop.token.cancel()
            raise self._wall_time_stop()

    def _wall_time_stop(self) -> _Stop:
        return _limit(f"The run reached its time limit of {self._limits.wall_time_s:g} seconds.")

    # ----------------------------------------------------------------------------------
    # Inference
    # ----------------------------------------------------------------------------------

    async def _call_model(
        self, messages: list[dict[str, Any]], schemas: list[dict[str, Any]]
    ) -> Completion:
        while True:
            self._check_stop()
            if self._usage.model_calls >= self._limits.max_model_calls:
                raise _limit(
                    f"The run reached its limit of {self._limits.max_model_calls} model calls."
                )
            remaining_tokens = self._limits.max_total_tokens - self._usage.total_tokens
            if remaining_tokens < MIN_TOKENS_FOR_CALL:
                raise _limit(
                    f"The run reached its limit of {self._limits.max_total_tokens} tokens."
                )
            max_tokens = min(self._max_completion_tokens, remaining_tokens)
            self._sink.emit(StatusEvent, text="Waiting for Nemotron")
            self._usage.model_calls += 1
            started = self._clock()
            try:
                completion, stopped = await self._guard(
                    self._client.complete(messages, schemas, max_tokens=max_tokens),
                    cooperative=False,
                )
            except ProviderError as exc:
                self._usage.latency_ms += int((self._clock() - started) * 1000)
                if exc.transient and self._n.retries < self._limits.max_retries:
                    self._n.retries += 1
                    self._sink.emit(
                        StatusEvent,
                        text="Nemotron request failed with a temporary error; retrying once",
                    )
                    _, stopped = await self._guard(asyncio.sleep(self._backoff), cooperative=False)
                    if stopped is not None:
                        raise stopped from None
                    continue
                raise _Stop("failed", exc.error) from None
            self._usage.latency_ms += int((self._clock() - started) * 1000)
            if stopped is not None:
                raise stopped
            assert completion is not None
            self._record_usage(completion)
            return completion

    def _record_usage(self, completion: Completion) -> None:
        if completion.usage is None:
            # Fail closed: the token cap is enforced from measured usage only.
            raise _Stop(
                "failed",
                ApiError(
                    code=ErrorCode.AI_ERROR,
                    message="The AI provider did not report token usage, so the token limit "
                    "could not be enforced. The run was stopped.",
                ),
            )
        self._usage.prompt_tokens += completion.usage.prompt_tokens
        self._usage.completion_tokens += completion.usage.completion_tokens
        self._usage.total_tokens += completion.usage.total_tokens
        self._sink.emit(UsageEvent, usage=self._usage.model_copy())
        if self._usage.total_tokens >= self._limits.max_total_tokens:
            raise _limit(f"The run reached its limit of {self._limits.max_total_tokens} tokens.")

    async def _emit_message(self, visible: str) -> str:
        known = await self._resolve_citations()
        checked = check_citations(visible, known)
        self._sink.emit(MessageEvent, text=checked.text, citations=checked.citations)
        if checked.unverified:
            count = len(checked.unverified)
            noun = "citation" if count == 1 else "citations"
            self._sink.emit(
                StatusEvent,
                text=f"{count} {noun} in the reply did not match a recorded result and "
                "were marked unverified.",
            )
        return checked.text

    # ----------------------------------------------------------------------------------
    # Tools
    # ----------------------------------------------------------------------------------

    async def _execute(
        self, call_id: str, name: str, raw_arguments: str
    ) -> tuple[ToolOutcome, _Stop | None]:
        tool = self._tools.get(name)
        parsed, parse_error = _parse_arguments(raw_arguments)
        if tool is None:
            self._sink.emit(ToolCallEvent, call_id=call_id, tool=name, arguments={})
            outcome = ToolOutcome.failure(
                ApiError(
                    code=ErrorCode.VALIDATION_ERROR,
                    message=f"The tool '{_clip(name, 60)}' is not available in this mode. "
                    f"Available tools: {', '.join(self._tools.names)}.",
                )
            )
            self._emit_result(call_id, name, outcome)
            return outcome, None
        if parse_error is not None:
            self._sink.emit(ToolCallEvent, call_id=call_id, tool=name, arguments={})
            outcome = ToolOutcome.failure(
                ApiError(code=ErrorCode.VALIDATION_ERROR, message=parse_error)
            )
            self._emit_result(call_id, name, outcome)
            return outcome, None
        try:
            args = tool.args_model.model_validate(parsed)
        except ValidationError as exc:
            self._sink.emit(ToolCallEvent, call_id=call_id, tool=name, arguments={})
            outcome = ToolOutcome.failure(
                ApiError(
                    code=ErrorCode.VALIDATION_ERROR,
                    message="Invalid arguments: " + _validation_summary(exc),
                )
            )
            self._emit_result(call_id, name, outcome)
            return outcome, None

        self._sink.emit(
            ToolCallEvent,
            call_id=call_id,
            tool=name,
            arguments=cap_arguments(args.model_dump(mode="json", exclude_none=True)),
        )
        if tool.is_experiment and self._n.experiments >= self._limits.max_experiments:
            outcome = ToolOutcome.failure(
                ApiError(
                    code=ErrorCode.LIMIT_REACHED,
                    message=f"This run may use at most {self._limits.max_experiments} experiments.",
                )
            )
            self._emit_result(call_id, name, outcome)
            return outcome, None

        self._sink.emit(StatusEvent, text=tool.status_text(args))
        result, stopped = await self._guard(tool.run(args), cooperative=True)
        if result is None:
            # The tool was stopped before it produced a result.
            outcome = ToolOutcome.failure(_CANCELLED if stopped is None else _stop_error(stopped))
            self._emit_result(call_id, name, outcome)
            return outcome, stopped
        if result.counted_experiment:
            self._n.experiments += 1
        self._emit_result(call_id, name, result)
        return result, stopped

    def _emit_result(self, call_id: str, name: str, outcome: ToolOutcome) -> None:
        self._sink.emit(
            ToolResultEvent,
            call_id=call_id,
            tool=name,
            ok=outcome.ok,
            summary=outcome.summary,
            observation_id=outcome.observation_id,
            experiment_id=outcome.experiment_id,
            proposal_id=outcome.proposal_id,
            error=outcome.error,
        )
        if outcome.proposal is not None:
            self._sink.emit(ProposalEvent, proposal=outcome.proposal)
        if outcome.diagnosis_id is not None:
            self._sink.emit(DiagnosisEvent, diagnosis_id=outcome.diagnosis_id)
        if outcome.repair_id is not None:
            self._sink.emit(RepairProposalEvent, repair_id=outcome.repair_id)

    # ----------------------------------------------------------------------------------
    # Cancellation / wall-time guard
    # ----------------------------------------------------------------------------------

    async def _guard(
        self, awaitable: Awaitable[T], *, cooperative: bool
    ) -> tuple[T | None, _Stop | None]:
        """Await ``awaitable`` unless a stop is requested or the wall time runs out.

        Returns ``(result, None)`` normally. On stop: the run's CancelToken is set; inference
        (``cooperative=False``) is cancelled at once, while tools get a grace period to stop
        cooperatively (so case state stays consistent) before being cancelled. If a tool still
        finishes, its result is returned together with the stop so it can be reported.
        Exceptions raised by ``awaitable`` propagate unchanged.
        """
        task: asyncio.Future[T] = asyncio.ensure_future(awaitable)
        stopper: asyncio.Future[Any] = asyncio.ensure_future(self._stop.event.wait())
        waiters: set[asyncio.Future[Any]] = {task, stopper}
        try:
            timeout = max(0.0, self._deadline - self._clock())
            done, _ = await asyncio.wait(
                waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            task.cancel()
            stopper.cancel()
            raise
        finally:
            stopper.cancel()
        if task in done:
            return task.result(), None

        stop = _Stop("cancelled", _CANCELLED) if self._stop.requested else self._wall_time_stop()
        self._stop.token.cancel()
        if cooperative:
            await asyncio.wait({task}, timeout=self._grace)
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
            return None, stop
        if task.cancelled():
            return None, stop
        exc = task.exception()
        if exc is not None:
            if cooperative:
                return None, stop
            raise exc
        return task.result(), stop


def _stop_error(stop: _Stop) -> ApiError:
    return stop.error or _CANCELLED


def _parse_arguments(raw: str) -> tuple[Any, str | None]:
    text = (raw or "").strip()
    if not text:
        return {}, None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None, "Arguments were not valid JSON."
    if not isinstance(value, dict):
        return None, "Arguments must be a JSON object."
    return value, None


def cap_arguments(arguments: dict[str, Any], limit_bytes: int = 2048) -> dict[str, Any]:
    """Tool-call arguments for the event stream: serialized size ≤ 2 KB, long strings cut with
    an ellipsis (the executed call itself always uses the full validated arguments)."""
    for max_len in (300, 120, 40, 12):
        capped = _truncate_strings(arguments, max_len)
        if len(json.dumps(capped, ensure_ascii=False).encode("utf-8")) <= limit_bytes:
            return capped
    return {}


def _truncate_strings(value: Any, max_len: int) -> Any:
    if isinstance(value, str):
        return _clip(value, max_len)
    if isinstance(value, dict):
        return {k: _truncate_strings(v, max_len) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate_strings(v, max_len) for v in value]
    return value


def _unique_ids(calls: list[ToolCallRequest]) -> list[ToolCallRequest]:
    seen: set[str] = set()
    out: list[ToolCallRequest] = []
    for index, call in enumerate(calls):
        call_id = call.id
        if call_id in seen:
            call_id = f"{call.id}_{index}"
        seen.add(call_id)
        out.append(ToolCallRequest(id=call_id, name=call.name, arguments=call.arguments))
    return out


def _tool_message(call: ToolCallRequest, outcome: ToolOutcome) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": call.id,
        "name": call.name,
        "content": json.dumps(outcome.content, separators=(",", ":")),
    }


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _validation_summary(exc: ValidationError) -> str:
    parts: list[str] = []
    for err in exc.errors()[:5]:
        loc = ".".join(str(p) for p in err.get("loc", ())) or "arguments"
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    return "; ".join(parts)
