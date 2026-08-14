from __future__ import annotations

import json
import subprocess
from pathlib import Path

from setpoint.executor import ClaudeExecutor, CodexExecutor


class _FakeCompleted:
    def __init__(self, returncode, stdout, stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_claude_executor_parses_json_result(monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["cwd"] = kwargs.get("cwd")
        payload = {"type": "result", "result": "did the work",
                   "total_cost_usd": 0.01,
                   "usage": {"input_tokens": 1200, "output_tokens": 340}}
        return _FakeCompleted(0, json.dumps(payload))

    ex = ClaudeExecutor(runner=fake_run)
    events = []
    res = ex.execute(system="SYS", task="TASK", tools=[], model="claude",
                     cwd=Path("/tmp/wt"), on_event=events.append)
    assert res.text == "did the work"
    assert res.usage.input_tokens == 1200
    assert res.usage.output_tokens == 340
    assert captured["argv"][0] == "claude"
    assert "-p" in captured["argv"]
    assert captured["cwd"] == Path("/tmp/wt")


def test_claude_executor_nonzero_exit_does_not_raise(monkeypatch):
    def fake_run(argv, **kwargs):
        return _FakeCompleted(1, "", "boom")

    ex = ClaudeExecutor(runner=fake_run)
    events = []
    res = ex.execute(system="SYS", task="TASK", tools=[], model="claude",
                     cwd=Path("/tmp/wt"), on_event=events.append)
    assert "boom" in res.text
    assert res.usage.input_tokens == 0
    assert any(e.kind == "note" for e in events)


def test_claude_executor_timeout_does_not_raise():
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

    ex = ClaudeExecutor(runner=fake_run, timeout=1)
    events = []
    res = ex.execute(system="S", task="T", tools=[], model="claude",
                     cwd=Path("/tmp/wt"), on_event=events.append)
    assert "timeout" in res.text.lower()
    assert any(e.kind == "note" for e in events)


def test_claude_executor_missing_binary_does_not_raise():
    def fake_run(argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    ex = ClaudeExecutor(runner=fake_run)
    events = []
    res = ex.execute(system="S", task="T", tools=[], model="claude",
                     cwd=Path("/tmp/wt"), on_event=events.append)
    assert res.usage.input_tokens == 0
    assert res.usage.output_tokens == 0
    assert res.text
    assert any(e.kind == "note" for e in events)


def test_codex_executor_falls_back_to_raw_text():
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["stdin"] = kwargs.get("stdin")
        return _FakeCompleted(0, "final answer line")

    ex = CodexExecutor(runner=fake_run)
    res = ex.execute(system="S", task="T", tools=[], model="codex",
                     cwd=Path("/tmp/wt"), on_event=lambda e: None)
    assert "final answer line" in res.text
    assert res.usage.output_tokens == 0
    assert captured["argv"][:2] == ["codex", "exec"]
    # non-interactive runs have no one to answer approval prompts (including
    # for MCP tool calls), so --approve-for-me must be set or codex
    # auto-denies them. It also implies the workspace-write sandbox the
    # maker needs to edit files, so no separate --sandbox flag is passed.
    assert "--approve-for-me" in captured["argv"]
    # never block reading stdin on an unattended run
    assert captured["stdin"] == subprocess.DEVNULL


def test_agent_cli_clamps_timeout_to_deadline():
    captured = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs)
        return _FakeCompleted(0, json.dumps({"result": "done", "usage": {}}))

    ex = ClaudeExecutor(timeout=1800, runner=fake_run)
    ex.set_deadline(10)
    ex.execute(system="SYS", task="TASK", tools=[], model="claude",
               cwd=Path("/tmp/wt"), on_event=lambda e: None)
    assert captured["timeout"] == 10


def test_agent_cli_deadline_none_keeps_base_timeout():
    captured = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs)
        return _FakeCompleted(0, json.dumps({"result": "done", "usage": {}}))

    ex = ClaudeExecutor(timeout=1800, runner=fake_run)
    ex.set_deadline(None)
    ex.execute(system="SYS", task="TASK", tools=[], model="claude",
               cwd=Path("/tmp/wt"), on_event=lambda e: None)
    assert captured["timeout"] == 1800


def test_codex_parse_extracts_nested_agent_message():
    """codex streams {"type":"item.completed","item":{"type":"agent_message",
    "text":...}} and ends on a turn event with no text. Reading only the last
    line's top-level keys dumped the whole JSONL blob into state.json, making
    every codex member's log unreadable."""
    from setpoint.executor.agent_cli import _codex_parse
    stdout = "\n".join([
        '{"type":"thread.started","thread_id":"abc"}',
        '{"type":"item.completed","item":{"id":"i0","type":"agent_message","text":"first"}}',
        '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"I fixed the parser."}}',
        '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":5}}',
    ])
    text, usage = _codex_parse(stdout)
    assert text == "I fixed the parser."   # last agent message wins
    assert "thread.started" not in text
    assert usage.input_tokens == 10 and usage.output_tokens == 5


def test_codex_parse_falls_back_to_plain_text():
    from setpoint.executor.agent_cli import _codex_parse
    text, _ = _codex_parse("not json at all")
    assert text == "not json at all"
