from __future__ import annotations

import json
from pathlib import Path

import yaml

from setpoint.decompose import decompose

CANNED = json.dumps({
    "tasks": [
        {"name": "build-api", "title": "Build the API",
         "goal": "Implement GET /leads returning JSON",
         "interfaces": "GET /leads -> {id,name}[]", "depends_on": [],
         "verify_command": "pytest tests/api -q", "engine": "claude"},
        {"name": "build-ui", "title": "Build the UI",
         "goal": "Render the leads list",
         "interfaces": "", "depends_on": ["build-api"],
         "verify_command": "pytest tests/ui -q", "engine": "codex"},
    ]
})


def fake_oneshot(engine: str, prompt: str) -> str:
    assert "Implement lead tracking" in prompt  # idea text reaches the model
    return f"chatter before\n```json\n{CANNED}\n```\nchatter after"


def test_decompose_writes_bundle(tmp_path):
    idea = tmp_path / "lead-tracking.md"
    idea.write_text("Implement lead tracking end to end")
    repo = tmp_path / "repo"
    repo.mkdir()

    fleet_yaml = decompose(str(idea), str(repo), ["claude", "codex", "kimi"],
                           str(tmp_path / "out"), oneshot=fake_oneshot)

    out = fleet_yaml.parent
    assert (out / "plan.md").exists()
    tasks = json.loads((out / "tasks.json").read_text())
    assert [t["name"] for t in tasks["tasks"]] == ["build-api", "build-ui"]

    fleet = yaml.safe_load(fleet_yaml.read_text())
    assert fleet["room"]["repo"] == str(repo)
    assert fleet["room"]["tasks"][1]["depends_on"] == ["build-api"]
    assert len(fleet["members"]) == 2

    spec = yaml.safe_load((out / "build-api.setpoint.yaml").read_text())
    assert spec["name"] == "build-api"
    assert spec["type"] == "coding"
    assert spec["goal"].startswith("Implement GET /leads")
    assert spec["workspace"] == {"repo": str(repo), "worktree": True,
                                 "branch": "setpoint/build-api"}
    assert spec["execute"]["engine"] == "claude"
    assert spec["verify"] == {"gate": "command", "command": "pytest tests/api -q"}
    # Must be truthy: run_loop gates deliver() on `if getattr(spec, "deliver", None)`,
    # so an empty {} would silently skip commit/push/PR for every fleet member.
    assert spec["deliver"]
    assert spec["deliver"]["push"] is True and spec["deliver"]["pr"] is True
    assert spec["deliver"]["base"]  # base is always pinned to the repo's trunk

    from setpoint.spec import load_spec
    loaded = load_spec(str(out / "build-api.setpoint.yaml"))
    assert loaded.execute.engine == "claude"


def test_decompose_rejects_bad_engine(tmp_path):
    idea = tmp_path / "i.md"
    idea.write_text("x")
    (tmp_path / "repo").mkdir()

    def bad(engine, prompt):
        return json.dumps({"tasks": [{"name": "a", "title": "A", "goal": "g",
                                      "interfaces": "", "depends_on": [],
                                      "verify_command": "true",
                                      "engine": "gemini"}]})

    import pytest
    with pytest.raises(ValueError, match="engine"):
        decompose(str(idea), str(tmp_path / "repo"), ["claude"],
                  str(tmp_path / "out"), oneshot=bad)


def test_extract_json_no_json_is_helpful(tmp_path):
    import pytest

    idea = tmp_path / "i.md"
    idea.write_text("x")
    (tmp_path / "repo").mkdir()

    def no_json(engine, prompt):
        return "I cannot help with that"

    with pytest.raises(ValueError, match="no JSON object"):
        decompose(str(idea), str(tmp_path / "repo"), ["claude"],
                  str(tmp_path / "out"), oneshot=no_json)


def test_cli_plan_repo_flag_requires_value(tmp_path, monkeypatch):
    import setpoint.__main__ as cli

    idea = tmp_path / "idea.md"
    idea.write_text("x")
    monkeypatch.chdir(tmp_path)

    assert cli.main(["fleet", "plan", "idea.md", "--repo"]) == 2


def test_cli_plan_unknown_flag(tmp_path, monkeypatch):
    import setpoint.__main__ as cli

    idea = tmp_path / "idea.md"
    idea.write_text("x")
    (tmp_path / "repo").mkdir()
    monkeypatch.chdir(tmp_path)

    assert cli.main(["fleet", "plan", "idea.md", "--repo", str(tmp_path / "repo"),
                     "--engiens", "claude"]) == 2


def test_cli_plan_rejects_unknown_engine(tmp_path, monkeypatch):
    import setpoint.__main__ as cli

    idea = tmp_path / "idea.md"
    idea.write_text("x")
    (tmp_path / "repo").mkdir()
    monkeypatch.chdir(tmp_path)

    assert cli.main(["fleet", "plan", "idea.md", "--repo", str(tmp_path / "repo"),
                     "--engines", "claude,gemini"]) == 2


def test_detect_repo_checks_prefers_bar_then_ci(tmp_path):
    from setpoint.decompose import detect_repo_checks
    (tmp_path / "pnpm-lock.yaml").write_text("")
    (tmp_path / "package.json").write_text(json.dumps(
        {"scripts": {"test": "vitest", "ci": "turbo ci", "bar": "turbo bar"}}))
    assert detect_repo_checks(tmp_path) == "pnpm bar"

    (tmp_path / "package.json").write_text(json.dumps(
        {"scripts": {"test": "vitest", "ci": "turbo ci"}}))
    assert detect_repo_checks(tmp_path) == "pnpm ci"


def test_detect_repo_checks_uses_the_lockfile_package_manager(tmp_path):
    from setpoint.decompose import detect_repo_checks
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"ci": "x"}}))
    assert detect_repo_checks(tmp_path) == "npm run ci"


def test_detect_repo_checks_returns_none_without_package_json(tmp_path):
    from setpoint.decompose import detect_repo_checks
    assert detect_repo_checks(tmp_path) is None


def test_decompose_wires_repo_checks_as_the_broad_gate(tmp_path):
    idea = tmp_path / "lead-tracking.md"
    idea.write_text("Implement lead tracking end to end")
    repo = tmp_path / "repo"
    repo.mkdir()

    fleet_yaml = decompose(str(idea), str(repo), ["claude", "codex"],
                           str(tmp_path / "out"), oneshot=fake_oneshot,
                           repo_checks="pnpm bar")
    member = yaml.safe_load((fleet_yaml.parent / "build-api.setpoint.yaml").read_text())
    assert member["verify"]["command"] == "pnpm bar"
    assert member["verify"]["scoped_command"] == "pytest tests/api -q"


def test_decompose_without_repo_checks_keeps_the_task_command(tmp_path):
    idea = tmp_path / "lead-tracking.md"
    idea.write_text("Implement lead tracking end to end")
    repo = tmp_path / "repo"
    repo.mkdir()

    fleet_yaml = decompose(str(idea), str(repo), ["claude", "codex"],
                           str(tmp_path / "out"), oneshot=fake_oneshot)
    member = yaml.safe_load((fleet_yaml.parent / "build-api.setpoint.yaml").read_text())
    assert member["verify"]["command"] == "pytest tests/api -q"
    assert "scoped_command" not in member["verify"]


def test_detect_prepare_command_from_lockfile(tmp_path):
    from setpoint.decompose import detect_prepare
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "vitest"}}))
    (tmp_path / "package-lock.json").write_text("{}")
    assert detect_prepare(tmp_path) == "npm ci"

    (tmp_path / "package-lock.json").unlink()
    (tmp_path / "pnpm-lock.yaml").write_text("")
    assert detect_prepare(tmp_path) == "pnpm install --frozen-lockfile"


def test_detect_prepare_returns_none_without_package_json(tmp_path):
    from setpoint.decompose import detect_prepare
    assert detect_prepare(tmp_path) is None


def test_decompose_sets_prepare_on_every_member(tmp_path):
    idea = tmp_path / "lead-tracking.md"
    idea.write_text("Implement lead tracking end to end")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text(json.dumps({"scripts": {"ci": "x"}}))
    (repo / "package-lock.json").write_text("{}")

    fleet_yaml = decompose(str(idea), str(repo), ["claude", "codex"],
                           str(tmp_path / "out"), oneshot=fake_oneshot)
    member = yaml.safe_load((fleet_yaml.parent / "build-api.setpoint.yaml").read_text())
    # A fresh worktree has no node_modules, so without this every gate exits
    # 127 cold and the member dies at preflight.
    assert member["workspace"]["prepare"] == "npm ci"


def test_detect_default_branch_prefers_develop_when_it_is_the_remote_head(tmp_path):
    import subprocess
    from setpoint.decompose import detect_default_branch
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "--initial-branch=main"], cwd=repo, check=True, capture_output=True)
    # No origin at all -> fall back to the local HEAD's branch.
    assert detect_default_branch(repo) == "main"


def test_decompose_sets_deliver_base_from_the_repo(tmp_path):
    idea = tmp_path / "lead-tracking.md"
    idea.write_text("Implement lead tracking end to end")
    repo = tmp_path / "repo"
    repo.mkdir()
    fleet_yaml = decompose(str(idea), str(repo), ["claude", "codex"],
                           str(tmp_path / "out"), oneshot=fake_oneshot,
                           base="develop")
    member = yaml.safe_load((fleet_yaml.parent / "build-api.setpoint.yaml").read_text())
    assert member["deliver"]["base"] == "develop"


def test_critical_path_finds_the_longest_real_chain():
    from setpoint.decompose import critical_path
    tasks = [
        {"name": "data", "depends_on": []},
        {"name": "tokens", "depends_on": []},
        {"name": "placement", "depends_on": ["data"]},
        {"name": "boards", "depends_on": ["data", "tokens", "placement"]},
        {"name": "my-week", "depends_on": ["data", "tokens"]},
    ]
    # The ops-calendar shape: data -> placement -> boards is the long pole.
    assert critical_path(tasks) == ["data", "placement", "boards"]


def test_critical_path_of_fully_independent_tasks_is_one():
    from setpoint.decompose import critical_path
    tasks = [{"name": "a", "depends_on": []}, {"name": "b", "depends_on": []}]
    assert len(critical_path(tasks)) == 1


def test_parallel_ceiling_matches_the_observed_fleet():
    from setpoint.decompose import parallel_ceiling
    tasks = [
        {"name": "data", "depends_on": []},
        {"name": "tokens", "depends_on": []},
        {"name": "placement", "depends_on": ["data"]},
        {"name": "boards", "depends_on": ["data", "tokens", "placement"]},
        {"name": "my-week", "depends_on": ["data", "tokens"]},
    ]
    # 5 tasks over a 3-deep chain: the fleet can never beat x1.67, and the
    # real ops-calendar run measured x1.65.
    assert round(parallel_ceiling(tasks), 2) == 1.67


def test_plan_md_reports_the_critical_path_and_ceiling(tmp_path):
    idea = tmp_path / "lead-tracking.md"
    idea.write_text("Implement lead tracking end to end")
    repo = tmp_path / "repo"
    repo.mkdir()
    fleet_yaml = decompose(str(idea), str(repo), ["claude", "codex"],
                           str(tmp_path / "out"), oneshot=fake_oneshot)
    plan = (fleet_yaml.parent / "plan.md").read_text()
    assert "Critical path" in plan
    assert "build-api" in plan          # the chain names its members
    assert "×" in plan or "x1" in plan.lower()   # a speedup ceiling is quoted


def test_plan_md_flags_an_unjustified_edge(tmp_path):
    idea = tmp_path / "i.md"
    idea.write_text("Implement lead tracking end to end")
    repo = tmp_path / "repo"
    repo.mkdir()

    def oneshot(engine, prompt, cwd=None):
        return json.dumps({"tasks": [
            {"name": "a", "title": "A", "goal": "g", "interfaces": "",
             "depends_on": [], "verify_command": "true", "engine": "claude"},
            {"name": "b", "title": "B", "goal": "g", "interfaces": "",
             "depends_on": ["a"], "verify_command": "true", "engine": "codex"},
        ]})

    fleet_yaml = decompose(str(idea), str(repo), ["claude", "codex"],
                           str(tmp_path / "out2"), oneshot=oneshot)
    plan = (fleet_yaml.parent / "plan.md").read_text()
    # b depends on a but never says what it reads -> candidate false edge.
    assert "UNJUSTIFIED" in plan
