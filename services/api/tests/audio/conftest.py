"""Audio test fixtures."""

from __future__ import annotations

import pytest
from audio_helpers import AnalyticRcTestDouble


@pytest.fixture
def analytic_double() -> AnalyticRcTestDouble:
    return AnalyticRcTestDouble()
