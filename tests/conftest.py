from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_setpoint_home(tmp_path_factory, monkeypatch):
    """Never let a test write into the developer's real ~/.setpoint.

    `_runs_root()` falls back to ~/.setpoint/runs when SETPOINT_RUNS_ROOT is
    unset, and fleet artifacts (report.md, room.json, status.md) land beside
    it in ~/.setpoint/fleets/<name>. A test that called run_fleet() without
    pinning the env var therefore created real fleet directories named after
    the test's fixtures — they showed up in the fleet viewer as if they were
    genuine runs.

    Autouse, so it protects tests that have not thought about it. A test that
    sets SETPOINT_RUNS_ROOT itself still wins: its monkeypatch.setenv runs
    after this fixture.
    """
    root = tmp_path_factory.mktemp("setpoint-home") / "runs"
    monkeypatch.setenv("SETPOINT_RUNS_ROOT", str(root))
