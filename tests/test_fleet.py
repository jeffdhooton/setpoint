from __future__ import annotations

import threading
from pathlib import Path

from types import SimpleNamespace

import pytest


def _make_fleet(tmp_path, n=4, concurrency=2):
    names = []
    members = []
    for i in range(n):
        sp = tmp_path / f"m{i}.setpoint.yaml"
        sp.write_text(f"name: m{i}\n")
        names.append(f"m{i}")
        members.append(sp.name)
    fp = tmp_path / "fleet.yaml"
    fp.write_text(f"name: f\nconcurrency: {concurrency}\nmembers:\n"
                  + "".join(f"  - {m}\n" for m in members))
    return fp, names


def test_run_fleet_runs_all_members(tmp_path, monkeypatch):
    from setpoint import fleet
    monkeypatch.setattr(fleet, "_runs_root", lambda: tmp_path / "runs")
    monkeypatch.setattr("setpoint.spec.load_spec",
                        lambda p: SimpleNamespace(name=Path(p).stem.replace(".setpoint", "")))

    def fake_run_loop(spec, *, fresh=False, ui=None, abort_check=None, runs_root=None):
        return SimpleNamespace(status="passed")

    fp, names = _make_fleet(tmp_path, n=3, concurrency=2)
    result = fleet.run_fleet(str(fp), run_loop=fake_run_loop)
    assert set(result) == set(names)
    assert all(v == "passed" for v in result.values())


def test_run_fleet_honors_concurrency(tmp_path, monkeypatch):
    from setpoint import fleet
    monkeypatch.setattr(fleet, "_runs_root", lambda: tmp_path / "runs")
    monkeypatch.setattr("setpoint.spec.load_spec",
                        lambda p: SimpleNamespace(name=Path(p).stem.replace(".setpoint", "")))

    lock = threading.Lock()
    state = {"cur": 0, "max": 0}
    release = threading.Event()

    def fake_run_loop(spec, *, fresh=False, ui=None, abort_check=None, runs_root=None):
        with lock:
            state["cur"] += 1
            state["max"] = max(state["max"], state["cur"])
        release.wait(timeout=5)
        with lock:
            state["cur"] -= 1
        return SimpleNamespace(status="passed")

    fp, _ = _make_fleet(tmp_path, n=6, concurrency=2)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor() as ex:
        fut = ex.submit(fleet.run_fleet, str(fp), run_loop=fake_run_loop)
        # give workers time to saturate, then release
        import time
        for _ in range(50):
            if state["max"] >= 2:
                break
            time.sleep(0.01)
        release.set()
        fut.result(timeout=10)
    assert state["max"] <= 2  # never exceeded the concurrency cap


def test_run_fleet_stop_sentinel_skips_unstarted(tmp_path, monkeypatch):
    from setpoint import fleet
    monkeypatch.setattr(fleet, "_runs_root", lambda: tmp_path / "runs")
    monkeypatch.setattr("setpoint.spec.load_spec",
                        lambda p: SimpleNamespace(name=Path(p).stem.replace(".setpoint", "")))
    (tmp_path / "runs").mkdir(parents=True)
    # Pre-create the sentinel; run_fleet clears it at start, so create it via a
    # run_loop that re-touches it after the first member.
    calls = {"n": 0}

    def fake_run_loop(spec, *, fresh=False, ui=None, abort_check=None, runs_root=None):
        calls["n"] += 1
        fleet.stop_sentinel_path().parent.mkdir(parents=True, exist_ok=True)
        fleet.stop_sentinel_path().write_text("stop")  # trip it after first member
        return SimpleNamespace(status="passed")

    fp, names = _make_fleet(tmp_path, n=4, concurrency=1)
    result = fleet.run_fleet(str(fp), run_loop=fake_run_loop)
    assert any(v == "skipped" for v in result.values())
    assert calls["n"] < len(names)  # not all members ran


def test_fleet_status_renders_from_state(tmp_path, monkeypatch):
    import json
    from setpoint import fleet
    runs = tmp_path / "runs"
    monkeypatch.setattr(fleet, "_runs_root", lambda: runs)
    monkeypatch.setattr("setpoint.spec.load_spec",
                        lambda p: SimpleNamespace(name=Path(p).stem.replace(".setpoint", "")))
    # Member state is namespaced under the fleet, not the global runs root.
    member_runs = runs.parent / "fleets" / "f" / "runs"
    (member_runs / "m0").mkdir(parents=True)
    (member_runs / "m0" / "state.json").write_text(json.dumps(
        {"name": "m0", "status": "passed", "iters": [{"n": 1}], "spent_usd": 0.0}))
    fp, _ = _make_fleet(tmp_path, n=2, concurrency=2)  # m0 ran, m1 pending
    out = fleet.fleet_status(str(fp))
    assert "m0" in out and "passed" in out
    assert "m1" in out and "pending" in out
    assert (tmp_path / "runs").parent.joinpath("fleets", "f", "status.md").exists()


def test_fleet_keys_by_spec_name_not_filename_stem(tmp_path, monkeypatch):
    """Regression: a member file's stem can differ from its spec's declared
    `name:` (e.g. the scribe fleet names files by task but sets name: CS-###).
    Both fleet_status and run_fleet must key/lookup by the spec name."""
    import json
    from setpoint import fleet
    runs = tmp_path / "runs"
    monkeypatch.setattr(fleet, "_runs_root", lambda: runs)

    def fake_load_spec(p):
        if Path(p).name == "task-a.setpoint.yaml":
            return SimpleNamespace(name="CS-100")
        return SimpleNamespace(name=Path(p).stem)

    monkeypatch.setattr("setpoint.spec.load_spec", fake_load_spec)

    # The run directory is keyed by the spec name, not the filename stem,
    # and lives under the fleet's own namespace.
    member_runs = runs.parent / "fleets" / "f" / "runs"
    (member_runs / "CS-100").mkdir(parents=True)
    (member_runs / "CS-100" / "state.json").write_text(json.dumps(
        {"name": "CS-100", "status": "passed", "iters": [], "spent_usd": 0.0}))

    member = tmp_path / "task-a.setpoint.yaml"
    member.write_text("name: CS-100\n")
    fp = tmp_path / "fleet.yaml"
    fp.write_text("name: f\nconcurrency: 1\nmembers:\n  - task-a.setpoint.yaml\n")

    out = fleet.fleet_status(str(fp))
    status_line = next(l for l in out.splitlines() if l.startswith("CS-100"))
    assert "passed" in status_line
    assert "pending" not in status_line

    def fake_run_loop(spec, *, fresh=False, ui=None, abort_check=None, runs_root=None):
        return SimpleNamespace(status="passed")

    result = fleet.run_fleet(str(fp), run_loop=fake_run_loop)
    assert result == {"CS-100": "passed"}


def test_run_fleet_raises_on_duplicate_member_names(tmp_path, monkeypatch):
    """Two members that resolve to the same spec name would race the same
    ~/.setpoint/runs/<name>/ state — fail fast instead of corrupting state."""
    from setpoint import fleet
    monkeypatch.setattr(fleet, "_runs_root", lambda: tmp_path / "runs")
    monkeypatch.setattr("setpoint.spec.load_spec",
                        lambda p: SimpleNamespace(name="dup"))

    fp, names = _make_fleet(tmp_path, n=2, concurrency=2)

    def fake_run_loop(spec, *, fresh=False, ui=None, abort_check=None, runs_root=None):
        return SimpleNamespace(status="passed")

    with pytest.raises(ValueError, match="dup"):
        fleet.run_fleet(str(fp), run_loop=fake_run_loop)


def test_run_fleet_member_run_loop_error_isolated(tmp_path, monkeypatch):
    """A single member's run_loop raising must not crash the fleet -- the
    other members still run and the fleet returns a full status dict."""
    from setpoint import fleet
    monkeypatch.setattr(fleet, "_runs_root", lambda: tmp_path / "runs")
    monkeypatch.setattr("setpoint.spec.load_spec",
                        lambda p: SimpleNamespace(name=Path(p).stem.replace(".setpoint", "")))

    def fake_run_loop(spec, *, fresh=False, ui=None, abort_check=None, runs_root=None):
        if spec.name == "m0":
            raise RuntimeError("boom")
        return SimpleNamespace(status="passed")

    fp, names = _make_fleet(tmp_path, n=3, concurrency=2)
    result = fleet.run_fleet(str(fp), run_loop=fake_run_loop)
    assert set(result) == set(names)
    assert result["m0"] == "error"
    assert result["m1"] == "passed"
    assert result["m2"] == "passed"


def test_run_fleet_member_load_spec_error_isolated(tmp_path, monkeypatch):
    """A single member with an unparseable spec must not crash the fleet --
    it is recorded as 'error' while the other members complete normally."""
    from setpoint import fleet
    monkeypatch.setattr(fleet, "_runs_root", lambda: tmp_path / "runs")

    def fake_load_spec(p):
        if Path(p).name == "m0.setpoint.yaml":
            raise ValueError("unparseable spec")
        return SimpleNamespace(name=Path(p).stem.replace(".setpoint", ""))

    monkeypatch.setattr("setpoint.spec.load_spec", fake_load_spec)

    def fake_run_loop(spec, *, fresh=False, ui=None, abort_check=None, runs_root=None):
        return SimpleNamespace(status="passed")

    fp, names = _make_fleet(tmp_path, n=3, concurrency=2)
    result = fleet.run_fleet(str(fp), run_loop=fake_run_loop)
    assert set(result) == set(names)
    assert result["m0"] == "error"
    assert result["m1"] == "passed"
    assert result["m2"] == "passed"


def test_member_name_strips_only_the_suffix():
    from pathlib import Path
    from setpoint.fleet import _member_name

    # a run legitimately named "deploy.loomtest" must survive intact
    assert _member_name(Path("/x/.setpoint/deploy.loomtest.setpoint.yaml")) == "deploy.loomtest"
    assert _member_name(Path("/x/.setpoint/plain.setpoint.yaml")) == "plain"
    assert _member_name(Path("/x/.setpoint/nosuffix.yaml")) == "nosuffix"
    # legacy suffix: un-migrated fleets (still on ".loom.yaml" members) must
    # keep resolving their run keys too
    assert _member_name(Path("/x/.setpoint/plain.loom.yaml")) == "plain"


def test_member_run_state_is_namespaced_per_fleet(tmp_path, monkeypatch):
    from setpoint import fleet
    from setpoint.memory import Memory
    monkeypatch.setattr(fleet, "_runs_root", lambda: tmp_path / "runs")
    monkeypatch.setattr("setpoint.spec.load_spec",
                        lambda p: SimpleNamespace(name=Path(p).stem.replace(".setpoint", "")))

    seen = {}

    def fake_run_loop(spec, *, fresh=False, ui=None, abort_check=None, runs_root=None):
        seen[spec.name] = runs_root
        m = Memory(spec.name, root=runs_root)
        m.start()
        m.set_status("passed")
        return m.load()

    fp, names = _make_fleet(tmp_path, n=2, concurrency=2)
    fleet.run_fleet(str(fp), run_loop=fake_run_loop)

    expected = tmp_path / "fleets" / "f" / "runs"
    assert set(seen.values()) == {expected}
    assert (expected / "m0" / "state.json").exists()
    # Nothing leaked into the global runs root.
    assert not (tmp_path / "runs" / "m0").exists()


def test_two_fleets_reusing_a_member_name_do_not_collide(tmp_path):
    from setpoint.fleet import fleet_runs_root
    from setpoint.fleet_spec import FleetSpec
    runs = tmp_path / "runs"
    a = fleet_runs_root(FleetSpec(name="wave1", members=[Path("m.setpoint.yaml")]), runs)
    b = fleet_runs_root(FleetSpec(name="wave2", members=[Path("m.setpoint.yaml")]), runs)
    assert a != b
    assert a == runs.parent / "fleets" / "wave1" / "runs"


def test_status_lines_show_elapsed_and_hide_fake_spend(tmp_path, monkeypatch):
    import json
    from setpoint import fleet
    from setpoint.fleet_spec import load_fleet
    monkeypatch.setattr("setpoint.spec.load_spec",
                        lambda p: SimpleNamespace(
                            name=Path(p).stem.replace(".setpoint", ""),
                            execute=SimpleNamespace(engine="claude")))
    runs = tmp_path / "runs"
    fp, _ = _make_fleet(tmp_path, n=1, concurrency=1)
    fs = load_fleet(str(fp))
    member_runs = fleet.fleet_runs_root(fs, runs)
    (member_runs / "m0").mkdir(parents=True)
    (member_runs / "m0" / "state.json").write_text(json.dumps(
        {"name": "m0", "status": "passed", "iters": [{}], "spent_usd": 0.0,
         "elapsed_secs": 754.0}))
    text = "\n".join(fleet._status_lines(fs, runs))
    assert "12m34s" in text
    assert "—" in text          # claude spend is not ours to report
    assert "$0.00" not in text
