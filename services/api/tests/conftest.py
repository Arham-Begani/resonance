"""Shared test fixtures (owner: circuit & simulation engineer; others may request additions).

- ``settings``: Settings with every data/jobs directory under pytest's ``tmp_path`` (never the
  shared ``.data/``), no ``.env`` file, and no Nebius credentials.
- ``settings_factory(**overrides)``: the same, with overrides (e.g. ``ngspice_timeout_s``).
- ``ngspice_bin``: the resolved ngspice binary path.
- Tests marked ``@pytest.mark.ngspice`` are skipped when ngspice is missing, unless the
  environment sets ``RESONANCE_REQUIRE_NGSPICE=1``, in which case they FAIL instead.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

import pytest

from resonance.config import Settings, resolve_ngspice_bin

_REQUIRE_ENV = "RESONANCE_REQUIRE_NGSPICE"


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "resonance_data_dir": tmp_path / "data",
        "web_dist_dir": tmp_path / "no-web",
        "nebius_api_key": None,
        "nebius_model": None,
        "resonance_dev_agent_fixture": False,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


@lru_cache(maxsize=1)
def _resolved_ngspice() -> str | None:
    return resolve_ngspice_bin(Settings(_env_file=None))  # type: ignore[call-arg]


def _missing_ngspice() -> None:
    if os.environ.get(_REQUIRE_ENV) == "1":
        pytest.fail(f"ngspice is required ({_REQUIRE_ENV}=1) but no binary was found.")
    pytest.skip("ngspice not available (set NGSPICE_BIN or run scripts/install-ngspice.py)")


def pytest_runtest_setup(item: pytest.Item) -> None:
    if item.get_closest_marker("ngspice") is not None and _resolved_ngspice() is None:
        _missing_ngspice()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def settings_factory(tmp_path: Path) -> Callable[..., Settings]:
    """``settings_factory(ngspice_timeout_s=1.0, ...)`` → Settings under ``tmp_path``."""

    def factory(**overrides: object) -> Settings:
        return make_settings(tmp_path, **overrides)

    return factory


@pytest.fixture
def ngspice_bin() -> str:
    path = _resolved_ngspice()
    if path is None:
        _missing_ngspice()
    assert path is not None
    return path
