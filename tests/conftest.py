"""Shared pytest fixtures: strip env vars that leak into tested code."""

import pytest


_LEAKY_VARS = ("WORKSPACE_ORIGIN", "HOST_PROJECT_DIR", "HOST_HOME", "MYSQL_CONFIG")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in _LEAKY_VARS:
        monkeypatch.delenv(var, raising=False)
