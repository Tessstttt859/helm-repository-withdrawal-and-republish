from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def chart_dir() -> Path:
    return Path(__file__).parents[1] / "charts" / "ledger-api"
