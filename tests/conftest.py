"""Preserve explicit real-workspace setup in tests written before demo onboarding."""

import pytest


@pytest.fixture(autouse=True)
def _legacy_real_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NONGSHI_DEFAULT_MODE", "real")
