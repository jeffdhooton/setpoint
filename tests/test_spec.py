import pytest
from setpoint.spec import load_spec


def test_load_coding_spec():
    s = load_spec("tests/fixtures/coding.yaml")
    assert s.name == "demo-coding"
    assert s.type == "coding"
    assert s.workspace.worktree is True
    assert s.workspace.branch == "setpoint/demo"
    assert s.context.files == ["VISION.md"]
    assert s.execute.plan_model == "deepseek-v4-pro"
    assert s.execute.model == "deepseek-v4-flash"
    assert s.verify.gate == "command"
    assert s.verify.command == "pytest -q"
    assert s.stop.max_iters == 5
    assert s.stop.no_progress_after == 2
    assert s.budget.max_usd == 1.5


def test_rejects_unknown_type(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("name: x\ngoal: g\ntype: nope\nworkspace:\n  repo: /tmp\n")
    with pytest.raises(ValueError, match="type must be"):
        load_spec(str(p))


def test_judge_gate_defaults(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        "name: x\ngoal: g\ntype: content\n"
        "workspace:\n  repo: /tmp\n"
        "verify:\n  gate: judge\n  rubric: r.md\n"
    )
    s = load_spec(str(p))
    assert s.verify.judge_model == "gpt-oss-20b"
    assert s.verify.pass_threshold == 0.8
    assert s.workspace.worktree is False  # default


def test_judge_model_must_differ_from_execute_model(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        "name: x\ngoal: g\ntype: content\n"
        "workspace:\n  repo: /tmp\n"
        "execute:\n  model: deepseek-v4-flash\n"
        "verify:\n  gate: judge\n  rubric: r.md\n  judge_model: deepseek-v4-flash\n"
    )
    import pytest
    with pytest.raises(ValueError, match="maker != checker"):
        load_spec(str(p))


def test_load_spec_expands_tilde_in_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    spec_file = tmp_path / "s.yaml"
    spec_file.write_text(
        "name: x\ngoal: g\ntype: coding\n"
        "workspace:\n  repo: /tmp\n"
        "verify:\n  gate: command\n  command: 'true'\n"
    )
    s = load_spec("~/s.yaml")
    assert s.name == "x"


def test_engine_defaults_to_deepseek(tmp_path):
    from setpoint.spec import load_spec
    p = tmp_path / "s.setpoint.yaml"
    p.write_text(
        "name: t\ngoal: g\ntype: coding\n"
        "workspace:\n  repo: .\n"
        "verify:\n  gate: command\n  command: 'true'\n"
    )
    spec = load_spec(str(p))
    assert spec.execute.engine == "deepseek"
    assert spec.stop.wall_clock_secs is None


def test_engine_claude_and_wall_clock_parse(tmp_path):
    from setpoint.spec import load_spec
    p = tmp_path / "s.setpoint.yaml"
    p.write_text(
        "name: t\ngoal: g\ntype: coding\n"
        "workspace:\n  repo: .\n"
        "execute:\n  engine: claude\n"
        "verify:\n  gate: command\n  command: 'true'\n"
        "stop:\n  max_iters: 3\n  wall_clock_secs: 900\n"
    )
    spec = load_spec(str(p))
    assert spec.execute.engine == "claude"
    assert spec.stop.wall_clock_secs == 900


def test_invalid_engine_rejected(tmp_path):
    import pytest
    from setpoint.spec import load_spec
    p = tmp_path / "s.setpoint.yaml"
    p.write_text(
        "name: t\ngoal: g\ntype: coding\n"
        "workspace:\n  repo: .\n"
        "execute:\n  engine: gpt5\n"
        "verify:\n  gate: command\n  command: 'true'\n"
    )
    with pytest.raises(ValueError, match="execute.engine"):
        load_spec(str(p))


def test_judge_engine_same_as_executor_engine_rejected(tmp_path):
    import pytest
    from setpoint.spec import load_spec
    p = tmp_path / "s.setpoint.yaml"
    p.write_text(
        "name: t\ngoal: g\ntype: content\n"
        "workspace:\n  repo: .\n"
        "execute:\n  engine: claude\n  model: claude\n"
        "verify:\n  gate: judge\n  judge_engine: claude\n  judge_model: claude\n"
        "  rubric: rubric.md\n"
    )
    with pytest.raises(ValueError, match="maker != checker"):
        load_spec(str(p))


def test_deliver_merge_true_rejected(tmp_path):
    import pytest
    from setpoint.spec import load_spec
    p = tmp_path / "s.setpoint.yaml"
    p.write_text(
        "name: t\ngoal: g\ntype: coding\n"
        "workspace:\n  repo: .\n"
        "verify:\n  gate: command\n  command: 'true'\n"
        "deliver:\n  merge: true\n"
    )
    with pytest.raises(ValueError, match="merge"):
        load_spec(str(p))


def test_deliver_branch_main_rejected(tmp_path):
    import pytest
    from setpoint.spec import load_spec
    p = tmp_path / "s.setpoint.yaml"
    p.write_text(
        "name: t\ngoal: g\ntype: coding\n"
        "workspace:\n  repo: .\n"
        "verify:\n  gate: command\n  command: 'true'\n"
        "deliver:\n  branch: main\n"
    )
    with pytest.raises(ValueError, match="branch"):
        load_spec(str(p))


def test_agent_engine_without_model_defaults_to_engine_sentinel(tmp_path):
    # Regression: engine:claude with no execute.model must NOT inherit the
    # deepseek default ("deepseek-v4-flash"), which the claude CLI rejects with
    # a 404. It must default to the "claude" sentinel so _claude_argv omits --model.
    from setpoint.spec import load_spec
    p = tmp_path / "s.setpoint.yaml"
    p.write_text(
        "name: t\ngoal: g\ntype: coding\n"
        "workspace:\n  repo: .\n"
        "execute:\n  engine: claude\n"
        "verify:\n  gate: command\n  command: 'true'\n"
    )
    assert load_spec(str(p)).execute.model == "claude"


def test_agent_engine_respects_explicit_model(tmp_path):
    from setpoint.spec import load_spec
    p = tmp_path / "s.setpoint.yaml"
    p.write_text(
        "name: t\ngoal: g\ntype: coding\n"
        "workspace:\n  repo: .\n"
        "execute:\n  engine: codex\n  model: gpt-5.6-sol\n"
        "verify:\n  gate: command\n  command: 'true'\n"
    )
    assert load_spec(str(p)).execute.model == "gpt-5.6-sol"


def test_deepseek_engine_keeps_default_model(tmp_path):
    from setpoint.spec import load_spec
    p = tmp_path / "s.setpoint.yaml"
    p.write_text(
        "name: t\ngoal: g\ntype: content\n"
        "workspace:\n  repo: .\n"
        "verify:\n  gate: command\n  command: 'true'\n"
    )
    assert load_spec(str(p)).execute.model == "deepseek-v4-flash"


def test_deliver_base_equal_branch_rejected(tmp_path):
    import pytest
    from setpoint.spec import load_spec
    p = tmp_path / "s.setpoint.yaml"
    p.write_text(
        "name: t\ngoal: g\ntype: coding\n"
        "workspace:\n  repo: .\n"
        "verify:\n  gate: command\n  command: 'true'\n"
        "deliver:\n  branch: develop\n  base: develop\n"
    )
    with pytest.raises(ValueError, match="(?i)base"):
        load_spec(str(p))


def test_deliver_base_develop_accepted(tmp_path):
    from setpoint.spec import load_spec
    p = tmp_path / "s.setpoint.yaml"
    p.write_text(
        "name: t\ngoal: g\ntype: coding\n"
        "workspace:\n  repo: .\n"
        "verify:\n  gate: command\n  command: 'true'\n"
        "deliver:\n  branch: loop/CS-179\n  base: develop\n"
    )
    spec = load_spec(str(p))
    assert spec.deliver["base"] == "develop"


def test_judge_engine_set_must_differ_from_execute_engine_regardless_of_model(tmp_path):
    import pytest
    from setpoint.spec import load_spec
    p = tmp_path / "s.setpoint.yaml"
    p.write_text(
        "name: t\ngoal: g\ntype: content\n"
        "workspace:\n  repo: .\n"
        "execute:\n  engine: claude\n  model: claude\n"
        "verify:\n  gate: judge\n  judge_engine: claude\n  judge_model: gpt-oss-20b\n"
        "  rubric: rubric.md\n"
    )
    with pytest.raises(ValueError, match="judge_engine"):
        load_spec(str(p))


def test_judge_engine_cross_engine_still_loads(tmp_path):
    from setpoint.spec import load_spec
    p = tmp_path / "s.setpoint.yaml"
    p.write_text(
        "name: t\ngoal: g\ntype: content\n"
        "workspace:\n  repo: .\n"
        "execute:\n  engine: claude\n  model: claude\n"
        "verify:\n  gate: judge\n  judge_engine: codex\n  judge_model: codex\n"
        "  rubric: rubric.md\n"
    )
    s = load_spec(str(p))
    assert s.verify.judge_engine == "codex"


def test_verify_timeout_and_preflight_parse(tmp_path):
    from setpoint.spec import load_spec
    p = tmp_path / "s.setpoint.yaml"
    p.write_text(
        "name: t\ngoal: g\ntype: coding\n"
        "workspace:\n  repo: .\n"
        "verify:\n  gate: command\n  command: 'true'\n"
        "  timeout_secs: 120\n  preflight: false\n"
    )
    spec = load_spec(str(p))
    assert spec.verify.timeout_secs == 120
    assert spec.verify.preflight is False


def test_verify_timeout_and_preflight_defaults(tmp_path):
    from setpoint.spec import load_spec
    p = tmp_path / "s.setpoint.yaml"
    p.write_text(
        "name: t\ngoal: g\ntype: coding\n"
        "workspace:\n  repo: .\n"
        "verify:\n  gate: command\n  command: 'true'\n"
    )
    spec = load_spec(str(p))
    assert spec.verify.timeout_secs == 600
    assert spec.verify.preflight is True


def _minimal_spec_text(tmp_path):
    return (
        f"name: dep\ngoal: g\ntype: coding\n"
        f"workspace:\n  repo: {tmp_path}\n"
        f"verify:\n  gate: command\n  command: 'true'\n"
    )


def test_legacy_loom_extension_still_loads_and_warns(tmp_path, capsys):
    from setpoint.spec import load_spec

    p = tmp_path / "old.loom.yaml"
    p.write_text(_minimal_spec_text(tmp_path))

    spec = load_spec(str(p))
    assert spec.name == "dep"  # still loads

    captured = capsys.readouterr()
    assert "deprecated" in captured.err
    assert "setpoint migrate" in captured.err
    assert captured.out == ""


def test_new_extension_is_silent(tmp_path, capsys):
    from setpoint.spec import load_spec

    p = tmp_path / "new.setpoint.yaml"
    p.write_text(_minimal_spec_text(tmp_path))

    load_spec(str(p))
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


def test_spec_parses_memory_block(tmp_path):
    p = tmp_path / "s.setpoint.yaml"
    p.write_text("""
name: t
goal: g
type: coding
workspace: {repo: /tmp/x}
verify: {gate: command, command: "true"}
memory: {scry_export: true}
""")
    from setpoint.spec import load_spec
    spec = load_spec(str(p))
    assert spec.memory.scry_export is True


def test_spec_memory_defaults_off(tmp_path):
    p = tmp_path / "s.setpoint.yaml"
    p.write_text("""
name: t
goal: g
type: coding
workspace: {repo: /tmp/x}
verify: {gate: command, command: "true"}
""")
    from setpoint.spec import load_spec
    assert load_spec(str(p)).memory.scry_export is False


def test_spec_parses_max_turns_and_tracks_explicit(tmp_path):
    p = tmp_path / "s.setpoint.yaml"
    p.write_text("""
name: t
goal: g
type: coding
workspace: {repo: /tmp/x}
execute: {max_turns: 40}
verify: {gate: command, command: "true"}
stop: {no_progress_after: 3}
""")
    from setpoint.spec import load_spec
    spec = load_spec(str(p))
    assert spec.execute.max_turns == 40
    assert "execute.max_turns" in spec.explicit
    assert "stop.no_progress_after" in spec.explicit


def test_spec_max_turns_default_not_explicit(tmp_path):
    p = tmp_path / "s.setpoint.yaml"
    p.write_text("""
name: t
goal: g
type: coding
workspace: {repo: /tmp/x}
verify: {gate: command, command: "true"}
""")
    from setpoint.spec import load_spec
    spec = load_spec(str(p))
    assert spec.execute.max_turns == 25
    assert spec.explicit == []
    assert spec.execute.plan_hint == ""
