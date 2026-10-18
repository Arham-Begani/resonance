"""AI configuration status (no network calls; 'ready' means configured, not verified)."""

from __future__ import annotations

from ..config import Settings
from ..contracts.agent import AiStatus

# The placeholder shipped in .env.example is not a key.
_PLACEHOLDER_KEYS = {"your_nebius_api_key_here"}

SETUP_HINT = (
    "Add NEBIUS_API_KEY and NEBIUS_MODEL to the .env file in the project root, then restart the "
    "API. The simulator and manual investigation keep working without AI."
)


def ai_status(settings: Settings) -> AiStatus:
    host = settings.nebius_base_url_host
    if settings.resonance_dev_agent_fixture:
        return AiStatus(
            available=False,
            state="dev_fixture",
            model=None,
            base_url_host=None,
            message="Development fixture mode: agent replies are scripted test data, not AI.",
        )
    key = settings.nebius_api_key.get_secret_value() if settings.nebius_api_key else ""
    if not key.strip() or key.strip() in _PLACEHOLDER_KEYS:
        return AiStatus(
            available=False,
            state="missing_key",
            model=settings.nebius_model,
            base_url_host=host,
            message="AI lab partner unavailable: NEBIUS_API_KEY is not set. " + SETUP_HINT,
        )
    if not (settings.nebius_model or "").strip():
        return AiStatus(
            available=False,
            state="missing_model",
            model=None,
            base_url_host=host,
            message="AI lab partner unavailable: NEBIUS_MODEL is not set. " + SETUP_HINT,
        )
    return AiStatus(
        available=True,
        state="ready",
        model=settings.nebius_model,
        base_url_host=host,
        message=f"Configured for {settings.nebius_model} via Nebius Token Factory.",
    )
