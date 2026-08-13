import subprocess
from pathlib import Path

import pytest

from setpoint.workspace import Worktree, prepare_workspace
from setpoint.spec import LoopSpec, Workspace, Context, ExecuteCfg, VerifyCfg, StopCfg, BudgetCfg


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _make_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("hi")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def test_worktree_create_and_cleanup(tmp_path):
    repo = _make_repo(tmp_path)
    wt = Worktree(repo=repo, branch="setpoint/test")
    path = wt.create()
    assert path.exists()
    assert (path / "f.txt").read_text() == "hi"
    assert path != repo
    wt.cleanup()
    assert not path.exists()


def _make_origin_repo(tmp_path) -> tuple[Path, Path]:
    """Return (clone, origin). The clone is one commit behind origin/main."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "--initial-branch=main")
    _git(origin, "config", "user.email", "t@t")
    _git(origin, "config", "user.name", "t")
    (origin / "f.txt").write_text("hi")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-qm", "init")

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)],
                   check=True, capture_output=True)
    _git(clone, "config", "user.email", "t@t")
    _git(clone, "config", "user.name", "t")

    # origin moves ahead; the clone does not know about it yet.
    (origin / "new.txt").write_text("ahead")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-qm", "ahead")
    return clone, origin


def test_worktree_branches_from_fetched_origin_base(tmp_path):
    clone, _origin = _make_origin_repo(tmp_path)
    wt = Worktree(repo=clone, branch="setpoint/test", base="main")
    path = wt.create()
    # The commit only origin had must be present: the worktree was cut from
    # origin/main after a fetch, not from the stale local HEAD.
    assert (path / "new.txt").read_text() == "ahead"
    assert wt.base_ref == "origin/main"
    wt.cleanup()


def test_worktree_falls_back_to_head_without_origin(tmp_path):
    repo = _make_repo(tmp_path)  # no remote at all
    wt = Worktree(repo=repo, branch="setpoint/test", base="main")
    path = wt.create()
    assert (path / "f.txt").read_text() == "hi"
    assert wt.base_ref == "HEAD"
    wt.cleanup()


def test_prepare_workspace_passes_deliver_base_as_worktree_base(tmp_path):
    repo = _make_repo(tmp_path)
    spec = LoopSpec(name="n", goal="g", type="coding",
                    workspace=Workspace(repo=repo, worktree=True, branch=None),
                    context=Context(), execute=ExecuteCfg(),
                    verify=VerifyCfg(command="true"),
                    stop=StopCfg(), budget=BudgetCfg(),
                    deliver={"base": "develop"})
    cwd, wt = prepare_workspace(spec)
    assert wt is not None
    assert wt.base == "develop"
    wt.cleanup()


def test_prepare_command_runs_once_in_the_worktree(tmp_path):
    repo = _make_repo(tmp_path)
    spec = LoopSpec(name="n", goal="g", type="coding",
                    workspace=Workspace(repo=repo, worktree=True, branch=None,
                                        prepare="echo built > built.txt"),
                    context=Context(), execute=ExecuteCfg(),
                    verify=VerifyCfg(command="true"),
                    stop=StopCfg(), budget=BudgetCfg())
    cwd, wt = prepare_workspace(spec)
    assert (cwd / "built.txt").read_text().strip() == "built"
    assert not (repo / "built.txt").exists()  # ran in the worktree, not the repo
    wt.cleanup()


def test_prepare_command_failure_raises(tmp_path):
    repo = _make_repo(tmp_path)
    spec = LoopSpec(name="n", goal="g", type="coding",
                    workspace=Workspace(repo=repo, worktree=True, branch=None,
                                        prepare="exit 3"),
                    context=Context(), execute=ExecuteCfg(),
                    verify=VerifyCfg(command="true"),
                    stop=StopCfg(), budget=BudgetCfg())
    with pytest.raises(RuntimeError, match="workspace.prepare failed"):
        prepare_workspace(spec)


def test_prepare_workspace_no_worktree(tmp_path):
    repo = _make_repo(tmp_path)
    spec = LoopSpec(name="n", goal="g", type="coding",
                    workspace=Workspace(repo=repo, worktree=False, branch=None),
                    context=Context(), execute=ExecuteCfg(), verify=VerifyCfg(command="true"),
                    stop=StopCfg(), budget=BudgetCfg())
    cwd, wt = prepare_workspace(spec)
    assert cwd == repo
    assert wt is None
