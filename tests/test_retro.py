from pathlib import Path

from setpoint.memory import IterRecord, RunState
from setpoint.retro import RunStats, compute_stats, propose_knobs, run_retro
from setpoint.tuning import Overlay


def _iter(n, passed=False, stop_reason="done", repeat_of="", lesson="", fingerprint=""):
    return IterRecord(n=n, plan="p", summary="s", passed=passed, feedback="f",
                      usd=0.5, stop_reason=stop_reason, repeat_of=repeat_of,
                      lesson=lesson, fingerprint=fingerprint)


def test_compute_stats_counts_everything():
    state = RunState(name="t", status="passed", spent_usd=1.5, iters=[
        _iter(1, stop_reason="max_turns"),
        _iter(2, repeat_of="abc", stop_reason="max_turns"),
        _iter(3, passed=True),
    ])
    s = compute_stats(state)
    assert s.passed is True and s.iters == 3
    assert s.cutoffs == 2 and s.repeat_strikes == 1
    assert s.usd == 1.5


def test_propose_raises_max_turns_on_cutoffs():
    stats = RunStats(passed=False, iters=8, usd=1.0, repeat_strikes=0, cutoffs=2)
    knobs = propose_knobs(stats, {}, RunState(name="t"))
    assert knobs["max_turns"] == 35          # 25 default + 10


def test_propose_steps_max_turns_back_when_clean():
    stats = RunStats(passed=True, iters=2, usd=0.2, repeat_strikes=0, cutoffs=0)
    knobs = propose_knobs(stats, {"max_turns": 45}, RunState(name="t"))
    assert knobs["max_turns"] == 40          # decays toward the 25 default


def test_propose_lowers_no_progress_on_repeat_strikes():
    stats = RunStats(passed=False, iters=8, usd=1.0, repeat_strikes=3, cutoffs=0)
    knobs = propose_knobs(stats, {"no_progress_after": 4}, RunState(name="t"))
    assert knobs["no_progress_after"] == 3


def test_propose_plan_hint_from_most_repeated_lesson():
    state = RunState(name="t", status="stopped", iters=[
        _iter(1, lesson="pin the dep", fingerprint="abc"),
        _iter(2, repeat_of="abc", lesson="pin the dep", fingerprint="abc"),
        _iter(3, repeat_of="abc", lesson="pin the dep", fingerprint="abc"),
    ])
    stats = compute_stats(state)
    knobs = propose_knobs(stats, {}, state)
    assert "pin the dep" in knobs["plan_hint"]


def test_propose_none_when_nothing_to_change():
    stats = RunStats(passed=True, iters=2, usd=0.2, repeat_strikes=0, cutoffs=0)
    assert propose_knobs(stats, {}, RunState(name="t")) is None


def test_run_retro_writes_report_and_pushes(tmp_path):
    state = RunState(name="t", status="stopped", spent_usd=1.0, iters=[
        _iter(1, stop_reason="max_turns"), _iter(2, stop_reason="max_turns"),
    ])
    ov = Overlay("k", root=tmp_path / "tuning")
    path = run_retro(state, ov, tmp_path)
    assert path == tmp_path / "retro.md"
    text = path.read_text()
    assert "max_turns" in text
    assert ov.load()["max_turns"] == 35


def test_run_retro_never_raises(tmp_path, monkeypatch):
    ov = Overlay("k", root=tmp_path / "tuning")
    monkeypatch.setattr(ov, "push", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    path = run_retro(RunState(name="t", status="stopped",
                              iters=[_iter(1, stop_reason="max_turns"),
                                     _iter(2, stop_reason="max_turns")]),
                     ov, tmp_path)   # must not raise
    assert path is None              # retro skipped cleanly, exception swallowed
