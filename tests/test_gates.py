from types import SimpleNamespace

from setpoint.gates import GateResult
from setpoint.gates.command import CommandGate
from setpoint.gates.judge import JudgeGate


def test_command_gate_pass(tmp_path):
    g = CommandGate(command="true")
    r = g.verify(cwd=tmp_path, on_event=lambda e: None)
    assert isinstance(r, GateResult)
    assert r.passed is True


def test_command_gate_fail_captures_output(tmp_path):
    g = CommandGate(command="echo boom >&2; exit 1")
    r = g.verify(cwd=tmp_path, on_event=lambda e: None)
    assert r.passed is False
    assert "boom" in r.feedback


def test_judge_gate_parses_score(tmp_path):
    artifact = tmp_path / "brief.md"
    artifact.write_text("a draft")

    def fake_create(**kw):
        content = '{"score": 0.9, "feedback": "great"}'
        msg = SimpleNamespace(content=content)
        usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage)

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))
    g = JudgeGate(client=client, model="gpt-oss-20b",
                  rubric_text="be good", threshold=0.8, artifact=str(artifact))
    r = g.verify(cwd=tmp_path, on_event=lambda e: None)
    assert r.passed is True
    assert r.score == 0.9


def test_judge_gate_fail_below_threshold(tmp_path):
    artifact = tmp_path / "brief.md"
    artifact.write_text("weak")

    def fake_create(**kw):
        msg = SimpleNamespace(content='{"score": 0.4, "feedback": "thin"}')
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)],
                               usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))
    g = JudgeGate(client=client, model="gpt-oss-20b",
                  rubric_text="r", threshold=0.8, artifact=str(artifact))
    r = g.verify(cwd=tmp_path, on_event=lambda e: None)
    assert r.passed is False
    assert "thin" in r.feedback


def test_build_gate_local_judge_disables_thinking(tmp_path):
    from setpoint.spec import load_spec
    from setpoint.gates import build_gate
    rubric = tmp_path / "r.md"
    rubric.write_text("be good")
    spec_file = tmp_path / "c.yaml"
    spec_file.write_text(
        f"name: x\ngoal: g\ntype: content\n"
        f"workspace:\n  repo: {tmp_path}\n"
        f"execute:\n  model: deepseek-v4-flash\n"
        f"verify:\n  gate: judge\n  rubric: {rubric}\n  judge_model: qwen3.6:27b\n"
    )
    spec = load_spec(str(spec_file))
    gate = build_gate(spec, judge_client=object())
    assert gate.extra_body == {"reasoning_effort": "none"}


def test_build_gate_deepseek_judge_no_extra_body(tmp_path):
    from setpoint.spec import load_spec
    from setpoint.gates import build_gate
    rubric = tmp_path / "r.md"
    rubric.write_text("be good")
    spec_file = tmp_path / "c.yaml"
    spec_file.write_text(
        f"name: x\ngoal: g\ntype: content\n"
        f"workspace:\n  repo: {tmp_path}\n"
        f"execute:\n  model: deepseek-v4-flash\n"
        f"verify:\n  gate: judge\n  rubric: {rubric}\n  judge_model: deepseek-v4-pro\n"
    )
    spec = load_spec(str(spec_file))
    gate = build_gate(spec, judge_client=object())
    assert gate.extra_body is None


def test_judge_gate_fails_fast_on_deterministic_check(tmp_path):
    # 664-word artifact must fail max_words BEFORE any LLM call.
    artifact = tmp_path / "brief.md"
    artifact.write_text(" ".join(["word"] * 664))

    class ExplodingClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    raise AssertionError("LLM should not be called when checks fail")

    from setpoint.gates.judge import JudgeGate
    g = JudgeGate(client=ExplodingClient(), model="qwen3.6:27b", rubric_text="r",
                  threshold=0.8, artifact=str(artifact), checks=[{"max_words": 400}])
    r = g.verify(cwd=tmp_path, on_event=lambda e: None)
    assert r.passed is False
    assert "max_words" in r.feedback and "664" in r.feedback


def test_judge_gate_structured_criteria_can_fail_high_score(tmp_path):
    from types import SimpleNamespace
    from setpoint.gates.judge import JudgeGate
    artifact = tmp_path / "a.md"
    artifact.write_text("short enough")

    def fake_create(**kw):
        content = ('{"criteria":[{"name":"under 400 words","pass":false,"evidence":"700 words"}],'
                   '"score":0.95,"feedback":"too long"}')
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
                               usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))
    g = JudgeGate(client=client, model="qwen3.6:27b", rubric_text="r",
                  threshold=0.8, artifact=str(artifact))  # no deterministic checks
    r = g.verify(cwd=tmp_path, on_event=lambda e: None)
    assert r.passed is False  # a self-reported failed criterion overrides the high score
    assert "under 400 words" in r.feedback


def test_command_gate_keeps_tail_of_long_output(tmp_path):
    # Test runners print the failure summary LAST — truncation must keep the tail.
    g = CommandGate(command="python3 -c \"print('x'*20000); print('FAILED tail_marker')\"; exit 1")
    r = g.verify(cwd=tmp_path, on_event=lambda e: None)
    assert r.passed is False
    assert "tail_marker" in r.feedback
    assert len(r.feedback) < 8000


def test_command_gate_times_out(tmp_path):
    import time
    g = CommandGate(command="sleep 30", timeout=1)
    t0 = time.monotonic()
    r = g.verify(cwd=tmp_path, on_event=lambda e: None)
    assert time.monotonic() - t0 < 10
    assert r.passed is False
    assert r.timed_out is True
    assert "timed out" in r.feedback


def test_command_gate_timeout_bounds_orphaned_pipe_holder(tmp_path):
    # A gate command that leaks a background child holding stdout must not
    # hang the loop past the timeout, even though the shell itself exits 0.
    import time
    g = CommandGate(command="(sleep 30 &); echo started; exit 0", timeout=1)
    t0 = time.monotonic()
    r = g.verify(cwd=tmp_path, on_event=lambda e: None)
    assert time.monotonic() - t0 < 10
    assert r.passed is False


def test_command_gate_reports_returncode(tmp_path):
    g = CommandGate(command="exit 127")
    r = g.verify(cwd=tmp_path, on_event=lambda e: None)
    assert r.returncode == 127


def test_command_gate_passes_env_to_the_verify_subprocess(tmp_path):
    from setpoint.gates.command import CommandGate
    gate = CommandGate(command='test "$SETPOINT_PORT_BASE" = "31337"',
                       env={"SETPOINT_PORT_BASE": "31337"})
    res = gate.verify(cwd=tmp_path, on_event=lambda e: None)
    assert res.passed is True


def test_failed_gate_feedback_names_the_command(tmp_path):
    """A compound gate can fail on a later clause while the visible output
    reads like success (`a && b | grep -q c`). Without the command in the
    feedback the agent sees 'ok' and a red gate, and cannot act."""
    from setpoint.gates.command import CommandGate
    gate = CommandGate(command="echo ok && false")
    res = gate.verify(cwd=tmp_path, on_event=lambda e: None)
    assert res.passed is False
    assert "echo ok && false" in res.feedback


def test_passing_gate_feedback_does_not_name_the_command(tmp_path):
    from setpoint.gates.command import CommandGate
    res = CommandGate(command="true").verify(cwd=tmp_path, on_event=lambda e: None)
    assert res.passed is True
    assert "true" not in res.feedback
