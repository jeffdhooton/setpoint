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
    assert spec["deliver"] == {}

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
