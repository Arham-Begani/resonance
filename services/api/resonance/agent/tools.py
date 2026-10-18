"""Tool declarations, JSON-schema generation, and validated execution results.

A run receives exactly one ``ToolSet``. A tool name outside that set cannot be executed: the
loop looks names up only in the run's own set, so EXPLORE, INVESTIGATE and HINT capabilities
never mix. No tool provides file, shell, code-execution, HTTP, database, or hidden-state access.
"""

from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

from ..contracts.agent import EditProposal
from ..contracts.common import ApiError, Contract

RunKind = Literal["investigate", "hint", "explore"]


class NoArguments(Contract):
    """Tools that take no arguments still validate that none were sent."""


@dataclass
class ToolOutcome:
    """Result of executing one validated tool call.

    ``content`` is the JSON returned to the model; ``summary`` is the short learner-visible text
    for the ``tool_result`` event. ``fatal`` stops the run as failed after this result is
    recorded (e.g. a simulation that failed again after its permitted infrastructure retry).
    """

    ok: bool
    content: dict[str, Any]
    summary: str
    observation_id: str | None = None
    experiment_id: str | None = None
    proposal_id: str | None = None
    error: ApiError | None = None
    proposal: EditProposal | None = None
    diagnosis_id: str | None = None
    repair_id: str | None = None
    counted_experiment: bool = False
    fatal: ApiError | None = None

    @classmethod
    def failure(cls, error: ApiError, *, fatal: bool = False) -> ToolOutcome:
        return cls(
            ok=False,
            content={"ok": False, "error": {"code": error.code.value, "message": error.message}},
            summary=error.message,
            error=error,
            fatal=error if fatal else None,
        )


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    run: Callable[[Any], Awaitable[ToolOutcome]]
    status_text: Callable[[Any], str]
    is_experiment: bool = False


class ToolSet:
    def __init__(self, kind: RunKind, tools: Iterable[Tool]) -> None:
        self.kind = kind
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"duplicate tool {tool.name}")
            self._tools[tool.name] = tool

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool_parameters_schema(tool.args_model),
                },
            }
            for tool in self._tools.values()
        ]


def tool_parameters_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON Schema for a tool's arguments with ``$ref``s inlined and titles removed.

    Inlining avoids relying on provider support for ``$defs``; titles only cost tokens.
    """
    schema = model.model_json_schema()
    defs: dict[str, Any] = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                target = copy.deepcopy(defs[ref.removeprefix("#/$defs/")])
                extra = {k: v for k, v in node.items() if k != "$ref"}
                return resolve({**target, **extra})
            out: dict[str, Any] = {}
            for key, value in node.items():
                if key == "title" and isinstance(value, str):
                    continue
                if key == "properties" and isinstance(value, dict):
                    out[key] = {name: resolve(sub) for name, sub in value.items()}
                else:
                    out[key] = resolve(value)
            return out
        if isinstance(node, list):
            return [resolve(item) for item in node]
        return node

    resolved: dict[str, Any] = resolve(schema)
    resolved.pop("description", None)  # the tool description already explains the call
    resolved.setdefault("type", "object")
    resolved.setdefault("properties", {})
    return resolved
