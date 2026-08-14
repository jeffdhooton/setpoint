from __future__ import annotations

from pathlib import Path


def test_survey_asks_about_the_repo_and_returns_findings(tmp_path):
    from setpoint.survey import survey
    seen = {}

    def fake_oneshot(engine, prompt, cwd=None):
        seen["engine"] = engine
        seen["prompt"] = prompt
        seen["cwd"] = cwd
        return "## Already built\n- Settings: shipped 2026-07-14"

    out = survey("wire up the settings screens", str(tmp_path),
                 oneshot=fake_oneshot, engine="claude")
    assert "Settings: shipped" in out
    # The survey must run INSIDE the repo, or it cannot read the code.
    assert seen["cwd"] == str(tmp_path)
    assert "wire up the settings screens" in seen["prompt"]
    # It has to ask the question that would have caught the stale issue.
    assert "already" in seen["prompt"].lower()
    assert "evidence" in seen["prompt"].lower() or "file" in seen["prompt"].lower()


def test_survey_prompt_asks_for_unmentioned_gaps(tmp_path):
    from setpoint.survey import SURVEY_PROMPT
    # Trends was the one genuinely unwired page and no issue mentioned it.
    assert "not mentioned" in SURVEY_PROMPT.lower() or "missing" in SURVEY_PROMPT.lower()


def test_decompose_injects_survey_findings_into_the_prompt(tmp_path):
    import json
    from setpoint.decompose import decompose
    prompts = []

    def fake_oneshot(engine, prompt, cwd=None):
        prompts.append(prompt)
        return json.dumps({"tasks": [
            {"name": "a", "title": "A", "goal": "g", "interfaces": "",
             "depends_on": [], "verify_command": "true", "engine": "claude"}]})

    idea = tmp_path / "i.md"
    idea.write_text("build the settings editors")
    repo = tmp_path / "r"
    repo.mkdir()
    decompose(str(idea), str(repo), ["claude"], str(tmp_path / "out"),
              oneshot=fake_oneshot,
              survey_text="## Already built\n- Settings editors shipped 2026-07-14")
    assert "Settings editors shipped 2026-07-14" in prompts[0]
    # And the model must be told not to re-plan what already exists.
    assert "already" in prompts[0].lower()


def test_empty_task_list_after_a_survey_is_a_clean_refusal(tmp_path):
    """The whole point: when the survey says the work already exists, the
    model returns no tasks and the command must say so plainly and exit 0 —
    not raise. This is the shape of the failure that wasted a real fleet."""
    import json
    from setpoint.decompose import NothingLeftToBuild, decompose

    def fake_oneshot(engine, prompt, cwd=None):
        return json.dumps({"tasks": []})

    idea = tmp_path / "i.md"
    idea.write_text("build the settings editors")
    repo = tmp_path / "r"
    repo.mkdir()
    try:
        decompose(str(idea), str(repo), ["claude"], str(tmp_path / "out"),
                  oneshot=fake_oneshot,
                  survey_text="## Already built\n- Settings editors shipped 2026-07-14")
    except NothingLeftToBuild as e:
        assert "survey" in str(e).lower()
        return
    raise AssertionError("expected NothingLeftToBuild")


def test_no_tasks_without_a_survey_is_still_a_plain_error(tmp_path):
    import json
    import pytest
    from setpoint.decompose import decompose

    def fake_oneshot(engine, prompt, cwd=None):
        return json.dumps({"tasks": []})

    idea = tmp_path / "i.md"
    idea.write_text("x")
    repo = tmp_path / "r"
    repo.mkdir()
    with pytest.raises(ValueError, match="no tasks"):
        decompose(str(idea), str(repo), ["claude"], str(tmp_path / "out2"),
                  oneshot=fake_oneshot)
