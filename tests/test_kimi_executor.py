from __future__ import annotations

from pathlib import Path

from setpoint.executor.agent_cli import KimiExecutor, _kimi_argv, _kimi_parse


def test_kimi_argv_shape():
    argv = _kimi_argv("do the thing", Path("/tmp"), "kimi")
    assert argv[0] == "kimi"
    assert argv[1:3] == ["-p", "do the thing"]
    assert "--auto" in argv
    assert "-m" not in argv  # default model alias omitted


def test_kimi_argv_model_override():
    argv = _kimi_argv("x", Path("/tmp"), "kimi-k3")
    i = argv.index("-m")
    assert argv[i + 1] == "kimi-k3"


def test_kimi_parse_plain_text():
    text, usage = _kimi_parse("did the work\n")
    assert text == "did the work"
    assert usage.input_tokens == 0 and usage.output_tokens == 0


class _FakeProc:
    returncode = 0
    stdout = "done\n"
    stderr = ""


def test_kimi_executor_runs_binary(tmp_path):
    calls = {}

    def fake_run(argv, **kw):
        calls["argv"] = argv
        calls["cwd"] = kw.get("cwd")
        return _FakeProc()

    ex = KimiExecutor(runner=fake_run)
    result = ex.execute("sys", "task", tools=[], model="kimi",
                        cwd=tmp_path, on_event=lambda e: None)
    assert result.text == "done"
    assert calls["argv"][0] == "kimi"
    assert calls["cwd"] == tmp_path


def test_spec_accepts_kimi_engine():
    from setpoint.spec import VALID_ENGINES
    assert "kimi" in VALID_ENGINES
