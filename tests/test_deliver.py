from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _passed_state():
    return SimpleNamespace(status="passed", iters=[SimpleNamespace(n=1, passed=True, feedback="ok")])


def _stopped_state():
    return SimpleNamespace(status="stopped",
                           iters=[SimpleNamespace(n=1, passed=False, feedback="2 tests fail")])


def _spec(deliver):
    ws = SimpleNamespace(repo=Path("/tmp/repo"), worktree=True, branch=None)
    return SimpleNamespace(name="CS-341", goal="g", workspace=ws, deliver=deliver)


def test_deliver_passed_opens_pr_and_updates_sheet(tmp_path):
    from setpoint.deliver import deliver
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["gh", "pr", "create"]:
            return _FakeCompleted(0, "https://github.com/x/y/pull/42\n")
        return _FakeCompleted(0, "")

    spec = _spec({"push": True, "pr": True, "sheet_task": "CS-341", "notify": False})
    res = deliver(spec, tmp_path, _passed_state(), runner=fake_run)
    assert res.delivered is True
    assert res.pr_url == "https://github.com/x/y/pull/42"
    flat = [" ".join(a) for a in calls]
    assert any(c.startswith("git push") for c in flat)
    assert any(c.startswith("gh pr create") for c in flat)
    assert any("CS-341" in c for c in flat)  # sheet update referenced the task
    # never a merge, never a deploy:
    assert not any("pr merge" in c for c in flat)
    assert not any("deploy" in c for c in flat)


def test_deliver_stopped_writes_report_only(tmp_path):
    from setpoint.deliver import deliver
    calls = []
    res = deliver(_spec({"push": True, "pr": True}), tmp_path, _stopped_state(),
                  runner=lambda a, **k: calls.append(a) or _FakeCompleted(0, ""))
    assert res.delivered is False
    assert res.report_path and Path(res.report_path).exists()
    assert "2 tests fail" in Path(res.report_path).read_text()
    assert not any(a[:2] == ["gh", "pr"] for a in calls)


def test_deliver_merge_flag_is_refused(tmp_path):
    import pytest
    from setpoint.deliver import deliver
    with pytest.raises(ValueError, match="merge"):
        deliver(_spec({"merge": True}), tmp_path, _passed_state(),
                runner=lambda a, **k: _FakeCompleted(0, ""))


def test_deliver_goal_containing_deploy_word_still_delivers(tmp_path):
    # Regression: the guard scanned the whole joined argv string, which
    # includes the commit message and PR title/body derived from spec.goal.
    # A goal containing the word "deploy" (not a deploy command) must not
    # trip the guard and abort delivery.
    from setpoint.deliver import deliver
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["gh", "pr", "create"]:
            return _FakeCompleted(0, "https://github.com/x/y/pull/43\n")
        return _FakeCompleted(0, "")

    spec = _spec({"push": True, "pr": True})
    spec.goal = "fix the deploy pipeline docs"
    res = deliver(spec, tmp_path, _passed_state(), runner=fake_run)
    assert res.delivered is True
    assert res.pr_url == "https://github.com/x/y/pull/43"


def test_deliver_branch_main_is_refused(tmp_path):
    import pytest
    from setpoint.deliver import deliver
    with pytest.raises(ValueError, match="(?i)branch|main"):
        deliver(_spec({"branch": "main"}), tmp_path, _passed_state(),
                runner=lambda a, **k: _FakeCompleted(0, ""))


def test_deliver_stopped_writes_report_to_durable_report_dir(tmp_path):
    from setpoint.deliver import deliver
    cwd = tmp_path / "worktree"
    cwd.mkdir()
    report_dir = tmp_path / "durable"
    report_dir.mkdir()
    res = deliver(_spec({"push": True, "pr": True}), cwd, _stopped_state(),
                  runner=lambda a, **k: _FakeCompleted(0, ""),
                  report_dir=report_dir)
    assert res.delivered is False
    assert res.report_path
    report_path = Path(res.report_path)
    assert report_path.exists()
    assert report_path.parent == report_dir
    assert not (cwd / "report.md").exists()
    assert "2 tests fail" in report_path.read_text()


def test_deliver_pr_defaults_base_to_main(tmp_path):
    from setpoint.deliver import deliver
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["gh", "pr", "create"]:
            return _FakeCompleted(0, "https://github.com/x/y/pull/44\n")
        return _FakeCompleted(0, "")

    deliver(_spec({"push": True, "pr": True}), tmp_path, _passed_state(), runner=fake_run)
    pr = next(a for a in calls if a[:3] == ["gh", "pr", "create"])
    assert pr[pr.index("--base") + 1] == "main"


def test_deliver_pr_targets_configured_base(tmp_path):
    from setpoint.deliver import deliver
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["gh", "pr", "create"]:
            return _FakeCompleted(0, "https://github.com/x/y/pull/45\n")
        return _FakeCompleted(0, "")

    spec = _spec({"push": True, "pr": True, "base": "develop", "branch": "loop/CS-179"})
    deliver(spec, tmp_path, _passed_state(), runner=fake_run)
    pr = next(a for a in calls if a[:3] == ["gh", "pr", "create"])
    assert pr[pr.index("--base") + 1] == "develop"


def test_deliver_base_equal_to_branch_is_refused(tmp_path):
    import pytest
    from setpoint.deliver import deliver
    with pytest.raises(ValueError, match="(?i)base"):
        deliver(_spec({"branch": "develop", "base": "develop"}), tmp_path, _passed_state(),
                runner=lambda a, **k: _FakeCompleted(0, ""))


def test_guards_refuse_real_deploy_and_merge_commands():
    import pytest
    from setpoint.deliver import _check_allowed_verb, _check_no_merge

    with pytest.raises(ValueError):
        _check_allowed_verb(["fly", "deploy", "-c", "x"])

    with pytest.raises(ValueError):
        _check_no_merge(["gh", "pr", "merge", "42"])


def test_deliver_adopts_an_existing_open_pr(tmp_path):
    from setpoint.deliver import deliver
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["gh", "pr", "list"]:
            return _FakeCompleted(0, "https://github.com/x/y/pull/7\n")
        return _FakeCompleted(0, "")

    spec = _spec({"push": True, "pr": True})
    res = deliver(spec, tmp_path, _passed_state(), runner=fake_run)
    assert res.pr_url == "https://github.com/x/y/pull/7"
    assert "pr (existing)" in res.actions
    flat = [" ".join(a) for a in calls]
    assert not any(c.startswith("gh pr create") for c in flat)


def test_deliver_creates_a_pr_when_none_exists(tmp_path):
    from setpoint.deliver import deliver
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["gh", "pr", "list"]:
            return _FakeCompleted(0, "\n")  # no open PR for this head
        if argv[:3] == ["gh", "pr", "create"]:
            return _FakeCompleted(0, "https://github.com/x/y/pull/8\n")
        return _FakeCompleted(0, "")

    spec = _spec({"push": True, "pr": True})
    res = deliver(spec, tmp_path, _passed_state(), runner=fake_run)
    assert res.pr_url == "https://github.com/x/y/pull/8"
    assert "pr" in res.actions


def test_deliver_never_commits_setpoint_scaffolding(tmp_path):
    """`git add -A` swept .setpoint-ports.env and .setpoint/orientation.md
    into a customer PR. Those are setpoint's own files."""
    from setpoint.deliver import deliver
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return _FakeCompleted(0, "")

    deliver(_spec({"push": False, "pr": False}), tmp_path, _passed_state(), runner=fake_run)
    add = next(a for a in calls if a[:2] == ["git", "add"])
    joined = " ".join(add)
    assert ":(exclude).setpoint-ports.env" in joined
    assert ":(exclude).setpoint/**" in joined
