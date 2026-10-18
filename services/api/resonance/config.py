"""Runtime configuration (owner: lead). Secrets stay server-side and are never logged."""

from __future__ import annotations

import os
import shutil
import sys
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]
API_ROOT = Path(__file__).resolve().parents[1]

# Project-local ngspice installed by scripts/install-ngspice.py (Windows build).
_LOCAL_NGSPICE_WINDOWS = REPO_ROOT / ".tools" / "ngspice-47" / "Spice64" / "bin" / "ngspice_con.exe"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env",),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Nebius Token Factory (OpenAI-compatible) ---
    nebius_api_key: SecretStr | None = None
    nebius_base_url: str = "https://api.tokenfactory.nebius.com/v1/"
    nebius_model: str | None = None

    # --- Simulator ---
    ngspice_bin: str | None = None
    ngspice_timeout_s: float = 20.0
    ngspice_max_concurrency: int = 2
    ngspice_max_output_mb: int = 48

    # --- Storage ---
    resonance_data_dir: Path = REPO_ROOT / ".data"
    artifact_max_count: int = 2000
    artifact_max_mb: int = 600
    artifact_max_per_session: int = 300

    # --- Web ---
    web_dist_dir: Path = REPO_ROOT / "apps" / "web" / "dist"
    session_cookie_secure: bool = False

    # --- Development only: a clearly labeled scripted agent fixture (never "AI") ---
    resonance_dev_agent_fixture: bool = False

    @property
    def nebius_base_url_host(self) -> str | None:
        host = urlparse(self.nebius_base_url).hostname
        return host or None

    @property
    def db_path(self) -> Path:
        return self.resonance_data_dir / "resonance.sqlite3"

    @property
    def evaluator_db_path(self) -> Path:
        """Separate database for hidden case state (evaluator boundary)."""
        return self.resonance_data_dir / "evaluator.sqlite3"

    @property
    def artifacts_dir(self) -> Path:
        return self.resonance_data_dir / "artifacts"

    @property
    def jobs_dir(self) -> Path:
        return self.resonance_data_dir / "jobs"


def resolve_ngspice_bin(settings: Settings) -> str | None:
    """NGSPICE_BIN → project-local Windows build → ngspice_con/ngspice on PATH."""
    if settings.ngspice_bin:
        candidate = Path(settings.ngspice_bin)
        if candidate.is_file():
            return str(candidate)
        found = shutil.which(settings.ngspice_bin)
        return found
    if sys.platform == "win32" and _LOCAL_NGSPICE_WINDOWS.is_file():
        return str(_LOCAL_NGSPICE_WINDOWS)
    for name in ("ngspice_con", "ngspice"):
        found = shutil.which(name)
        if found:
            return found
    return None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def subprocess_env() -> dict[str, str]:
    """Minimal environment for child processes: never pass API keys or other secrets."""
    keep = ("PATH", "SYSTEMROOT", "SystemRoot", "TEMP", "TMP", "HOME", "USERPROFILE", "LANG")
    return {k: v for k, v in os.environ.items() if k in keep}
