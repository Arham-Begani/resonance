"""LIVE Nebius Token Factory checks (opt-in; real inference, real cost).

Run with:  RESONANCE_LIVE_AI=1 uv run pytest tests/agent/test_agent_live.py -s -m live_ai
Requires NEBIUS_API_KEY and NEBIUS_MODEL in the repository .env (never printed).

These tests print measured facts (model list, latency, usage, response shape) for
docs/ai-integration.md. The investigation run uses the TEST-ONLY FakeCaseService as its case
backend; the model calls themselves are genuine.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import httpx
import pytest
from agent_testkit import SESSION, FakeCaseService, wait_finished

from resonance.agent.client import create_chat_client
from resonance.agent.service import AgentService
from resonance.config import Settings
from resonance.contracts.agent import CaseAgentRunRequest

pytestmark = [
    pytest.mark.live_ai,
    pytest.mark.skipif(
        os.environ.get("RESONANCE_LIVE_AI") != "1", reason="set RESONANCE_LIVE_AI=1 to run"
    ),
]


def _say(text: str) -> None:
    """Print a live report even on consoles whose code page lacks model punctuation."""
    encoding = sys.stdout.encoding or "utf-8"
    print(text.encode(encoding, "backslashreplace").decode(encoding))


@pytest.fixture
def live_settings(tmp_path: Any) -> Settings:
    settings = Settings(resonance_data_dir=tmp_path)
    key = settings.nebius_api_key.get_secret_value().strip() if settings.nebius_api_key else ""
    if not key or not (settings.nebius_model or "").strip():
        pytest.skip("NEBIUS_API_KEY / NEBIUS_MODEL not configured")
    return settings


def _raw_chat(settings: Settings, body: dict[str, Any]) -> tuple[int, dict[str, Any], float]:
    assert settings.nebius_api_key is not None
    started = time.perf_counter()
    response = httpx.post(
        settings.nebius_base_url.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {settings.nebius_api_key.get_secret_value().strip()}"},
        json=body,
        timeout=120,
    )
    elapsed = time.perf_counter() - started
    data: dict[str, Any] = response.json() if response.content else {}
    return response.status_code, data, elapsed


async def test_live_configured_model_is_listed(live_settings: Settings) -> None:
    client = create_chat_client(live_settings)
    try:
        models = await client.list_models()
    finally:
        await client.aclose()
    nemotron = sorted(m for m in models if "nemotron" in m.lower())
    _say(f"\n[live] {len(models)} models visible; Nemotron models: {nemotron}")
    assert live_settings.nebius_model in models


def test_live_tool_call_round_trip_shape(live_settings: Settings) -> None:
    """One raw request with a single tool: reports the message fields Nebius returns (field
    names only; reasoning text is never printed) plus latency and usage."""
    tool = {
        "type": "function",
        "function": {
            "name": "get_case",
            "description": "Read the case.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    }
    body = {
        "model": live_settings.nebius_model,
        "messages": [
            {"role": "system", "content": "Call get_case before answering."},
            {"role": "user", "content": "Investigate this case."},
        ],
        "tools": [tool],
        "tool_choice": "auto",
        "max_tokens": 1024,
        "temperature": 1.0,
        "top_p": 0.95,
    }
    status, data, elapsed = _raw_chat(live_settings, body)
    assert status == 200, json.dumps(data)[:300]
    message = data["choices"][0]["message"]
    content = message.get("content") or ""
    choice = data["choices"][0]
    calls = [c["function"]["name"] for c in message.get("tool_calls") or []]
    think = "<think>" in content or "</think>" in content
    _say(
        f"\n[live] status=200 latency_s={elapsed:.2f} finish={choice.get('finish_reason')} "
        f"message_fields={sorted(message)} usage={data.get('usage')} tool_calls={calls} "
        f"content_has_think_tag={think}"
    )
    assert data.get("usage", {}).get("total_tokens")
    assert any(c["function"]["name"] == "get_case" for c in message.get("tool_calls") or [])

    # Does the endpoint accept parallel_tool_calls=false (verification V-02)?
    status_p, data_p, _ = _raw_chat(live_settings, {**body, "parallel_tool_calls": False})
    _say(f"[live] parallel_tool_calls=false accepted: {status_p == 200} (HTTP {status_p})")


async def test_live_investigation_run(live_settings: Settings, store: Any) -> None:
    """A full INVESTIGATE run with genuine Nemotron inference against the TEST-ONLY fake case
    (hidden fault: R1 doubled). Reports the measured outcome; it does not require a correct
    diagnosis (that is what the evaluation harness measures)."""
    cases = FakeCaseService(SESSION, mode="agent")
    service = AgentService(settings=live_settings, store=store, cases=cases, explore=None)
    cases.register_run_canceller(service.cancel_and_wait)
    started = time.perf_counter()
    run = await service.start_case_run(
        SESSION, cases.case_id, CaseAgentRunRequest(kind="investigate")
    )
    await wait_finished(service, run.id, limit_s=240)
    elapsed = time.perf_counter() - started
    handle = service.handle_for(SESSION, run.id)
    final = handle.snapshot()
    tools = [(e.tool, e.ok) for e in handle.events if e.type == "tool_result"]
    _say(
        f"\n[live] run status={final.status} wall_s={elapsed:.1f} usage={final.usage.model_dump()}"
        f"\n[live] tools={tools}"
        f"\n[live] diagnosis={cases.diagnosis.submission.model_dump() if cases.diagnosis else None}"
        f"\n[live] error={final.error.model_dump() if final.error else None}"
    )
    for event in handle.events:
        if event.type == "message":
            assert "<think>" not in event.text and "</think>" not in event.text
    assert final.status in ("completed", "limit_reached"), final.error
    assert any(ok for _, ok in tools)
