import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import setpoint.__main__ as cli
from setpoint.executor.base import ExecuteResult
from setpoint.budget import Usage


def _make_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for a in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)
    # a failing test that passes once marker file exists
    (repo / "check.sh").write_text('test -f PASS')
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True, capture_output=True)
    return repo


def test_cmd_run_converges(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    spec = tmp_path / "loop.yaml"
    spec.write_text(
        f"name: cli-demo\ngoal: make check pass\ntype: coding\n"
        f"workspace:\n  repo: {repo}\n  worktree: false\n"
        f"execute:\n  tools: [write]\n"
        f"verify:\n  gate: command\n  command: 'sh check.sh'\n"
        f"stop:\n  max_iters: 3\nbudget:\n  max_usd: 5.0\n")

    # Executor that creates the PASS marker so the command gate flips to green.
    class WinningExecutor:
        def execute(self, system, task, tools, model, cwd, on_event):
            (Path(cwd) / "PASS").write_text("")
            return ExecuteResult(text="created PASS", usage=Usage(100, 50, 0))

    monkeypatch.setattr(cli, "_build_executor", lambda spec: WinningExecutor())
    monkeypatch.setattr(cli, "_build_plan_client",
                        lambda spec: _fake_plan_client())
    monkeypatch.setenv("SETPOINT_RUNS_ROOT", str(tmp_path / "runs"))

    rc = cli.main(["run", str(spec)])
    assert rc == 0
    state = json.loads((tmp_path / "runs" / "cli-demo" / "state.json").read_text())
    assert state["status"] == "passed"


def test_run_loop_delivers_even_when_lesson_bookkeeping_fails(tmp_path, monkeypatch):
    # A filesystem error out of the lesson store (e.g. unwritable
    # ~/.setpoint/lessons) must not skip deliver() — the passed run's
    # undelivered work would otherwise be destroyed by the worktree cleanup
    # in run_loop's finally block.
    import setpoint.lessons as lessons

    repo = _make_repo(tmp_path)
    spec_path = tmp_path / "loop.yaml"
    spec_path.write_text(
        f"name: cli-demo-bookkeeping\ngoal: make check pass\ntype: coding\n"
        f"workspace:\n  repo: {repo}\n  worktree: false\n"
        f"execute:\n  tools: [write]\n"
        f"verify:\n  gate: command\n  command: 'sh check.sh'\n"
        f"stop:\n  max_iters: 3\nbudget:\n  max_usd: 5.0\n")

    class WinningExecutor:
        def execute(self, system, task, tools, model, cwd, on_event):
            (Path(cwd) / "PASS").write_text("")
            return ExecuteResult(text="created PASS", usage=Usage(100, 50, 0))

    def boom(*a, **k):
        raise OSError("unwritable lesson store")

    monkeypatch.setattr(cli, "_build_executor", lambda spec: WinningExecutor())
    monkeypatch.setattr(cli, "_build_plan_client",
                        lambda spec: _fake_plan_client())
    monkeypatch.setattr(lessons, "promote_validated", boom)
    monkeypatch.setenv("SETPOINT_RUNS_ROOT", str(tmp_path / "runs"))

    from setpoint.spec import load_spec
    spec = load_spec(str(spec_path))

    state = cli.run_loop(spec)
    assert state.status == "passed"


def _fake_plan_client():
    def create(**kw):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="plan: touch PASS"))],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=2, prompt_cache_hit_tokens=0))
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def test_ls_empty(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SETPOINT_RUNS_ROOT", str(tmp_path / "runs"))
    assert cli.main(["ls"]) == 0


def test_build_executor_selects_engine(tmp_path, monkeypatch):
    from setpoint import __main__ as m
    from setpoint.executor import ClaudeExecutor, CodexExecutor, DeepSeekExecutor
    from setpoint.executor.agent_plan import AgentPlanClient

    def spec_with(engine):
        from setpoint.spec import ExecuteCfg
        class S:  # minimal stand-in
            execute = ExecuteCfg(engine=engine)
        return S()

    # DeepSeek path must not require a real key for this unit test:
    monkeypatch.setattr(m, "make_deepseek_client", lambda: object(), raising=False)

    assert isinstance(m._build_executor(spec_with("claude")), ClaudeExecutor)
    assert isinstance(m._build_executor(spec_with("codex")), CodexExecutor)
    assert isinstance(m._build_plan_client(spec_with("claude")), AgentPlanClient)


def test_run_loop_is_reused_by_cmd_run(monkeypatch, tmp_path):
    # cmd_run delegates to run_loop and maps status to an exit code.
    from setpoint import __main__ as m
    from setpoint.memory import RunState

    called = {}

    def fake_load_spec(path):
        called["path"] = path
        return object()

    def fake_run_loop(spec, *, fresh=False, ui=None, abort_check=None):
        called["fresh"] = fresh
        return RunState(name="x", status="passed")

    monkeypatch.setattr("setpoint.spec.load_spec", fake_load_spec)
    monkeypatch.setattr(m, "run_loop", fake_run_loop)
    assert m.cmd_run("some.yaml", fresh=True) == 0
    assert called["path"] == "some.yaml" and called["fresh"] is True


def test_fleet_stop_creates_sentinel(monkeypatch, tmp_path):
    from setpoint import __main__ as m
    from setpoint import fleet
    monkeypatch.setattr(fleet, "_runs_root", lambda: tmp_path / "runs")
    assert m.main(["fleet", "stop"]) == 0
    assert fleet.stop_sentinel_path().exists()


def test_fleet_run_dispatches(monkeypatch, tmp_path):
    from setpoint import __main__ as m
    from setpoint import fleet
    called = {}
    monkeypatch.setattr(fleet, "run_fleet",
                        lambda p, **k: called.setdefault("run", p) and {"a": "passed"})
    monkeypatch.setattr(fleet, "fleet_status", lambda p: "STATUS")
    rc = m.main(["fleet", "run", "f.yaml"])
    assert rc == 0 and called["run"] == "f.yaml"


def _loom_repo(tmp_path) -> Path:
    repo = tmp_path / "dep"
    (repo / ".loom").mkdir(parents=True)
    (repo / ".loom" / "one.loom.yaml").write_text("name: one\n")
    (repo / ".loom" / "fleet.yaml").write_text("name: f\nmembers:\n  - one.loom.yaml\n")
    return repo


def test_migrate_dry_run_changes_nothing(tmp_path, capsys):
    repo = _loom_repo(tmp_path)
    assert cli.main(["migrate", str(repo), "--dry-run"]) == 0
    assert (repo / ".loom" / "one.loom.yaml").exists()
    assert not (repo / ".setpoint").exists()
    assert ".setpoint" in capsys.readouterr().out


def test_migrate_applies(tmp_path):
    repo = _loom_repo(tmp_path)
    assert cli.main(["migrate", str(repo)]) == 0
    assert (repo / ".setpoint" / "one.setpoint.yaml").exists()
    assert "one.setpoint.yaml" in (repo / ".setpoint" / "fleet.yaml").read_text()


def test_migrate_missing_repo_errors(tmp_path):
    assert cli.main(["migrate", str(tmp_path / "nope")]) == 1


def test_migrate_requires_a_path(capsys):
    assert cli.main(["migrate"]) == 1


def test_migrate_on_clean_repo_is_a_noop(tmp_path, capsys):
    repo = tmp_path / "clean"
    repo.mkdir()
    assert cli.main(["migrate", str(repo)]) == 0
    assert "nothing to migrate" in capsys.readouterr().out


def test_migrate_blocked_when_setpoint_dir_already_exists(tmp_path, capsys):
    repo = _loom_repo(tmp_path)
    (repo / ".setpoint").mkdir()
    assert cli.main(["migrate", str(repo)]) == 1
    out = capsys.readouterr().out
    assert "REFUSING" in out
    assert ".setpoint/ already exists" in out
    # filesystem must be untouched: nothing renamed, no apply happened
    assert (repo / ".loom" / "one.loom.yaml").exists()
    assert (repo / ".loom" / "fleet.yaml").read_text() == "name: f\nmembers:\n  - one.loom.yaml\n"
    assert list((repo / ".setpoint").iterdir()) == []


def test_build_executor_passes_max_turns(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    from setpoint.__main__ import _build_executor
    from setpoint.spec import (BudgetCfg, Context, ExecuteCfg, LoopSpec,
                               StopCfg, VerifyCfg, Workspace)
    spec = LoopSpec(name="t", goal="g", type="coding",
                    workspace=Workspace(repo=tmp_path), context=Context(),
                    execute=ExecuteCfg(max_turns=33), verify=VerifyCfg(command="true"),
                    stop=StopCfg(), budget=BudgetCfg())
    ex = _build_executor(spec)
    assert ex.max_turns == 33
