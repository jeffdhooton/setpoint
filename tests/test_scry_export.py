from pathlib import Path

from setpoint.lessons import StoredLesson
from setpoint.scry_export import export_lessons


def _lesson(text="update imports after renames"):
    return StoredLesson(ts="2026-08-07T00:00:00", run="r", goal="fix the tests",
                        fingerprint="abc", normalized="n", category="import-error",
                        lesson=text)


def test_export_shells_scry_memory_remember(monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        class R: returncode = 0
        return R()

    monkeypatch.setattr("setpoint.scry_export.subprocess.run", fake_run)
    n = export_lessons([_lesson()], Path("/tmp/repo"))
    assert n == 1
    argv = calls[0]
    assert argv[:3] == ["scry", "memory", "remember"]
    assert "update imports after renames" in argv[3]
    assert "fix the tests" in argv[3]        # fact mentions the goal
    assert "--repo" in argv and "/tmp/repo" in argv


def test_export_swallows_missing_binary(monkeypatch):
    def boom(argv, **kw):
        raise FileNotFoundError("scry not on PATH")
    monkeypatch.setattr("setpoint.scry_export.subprocess.run", boom)
    assert export_lessons([_lesson()], Path("/tmp/repo")) == 0   # no raise


def test_export_counts_only_successes(monkeypatch):
    codes = iter([0, 1])

    def fake_run(argv, **kw):
        class R: pass
        r = R(); r.returncode = next(codes)
        return r

    monkeypatch.setattr("setpoint.scry_export.subprocess.run", fake_run)
    assert export_lessons([_lesson("a"), _lesson("b")], Path("/tmp/repo")) == 1
