from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable

from setpoint.budget import Usage
from setpoint.tools import Tool
from .base import ExecEvent, ExecuteResult, Executor


class AgentCLIExecutor(Executor):
    """Drive a CLI coding agent (Claude Code / Codex) as a setpoint executor.

    Unlike DeepSeekExecutor, the agent owns its own tools; setpoint passes the
    composed prompt, sets cwd to the worktree, and captures the final text +
    usage. A failed agent turn never raises — it is just an unproductive
    iteration the gate will fail.
    """

    def __init__(self, argv_fn: Callable[[str, Path, str], list[str]],
                 parse_fn: Callable[[str], tuple[str, Usage]],
                 timeout: int = 1800, runner=subprocess.run):
        self.argv_fn = argv_fn
        self.parse_fn = parse_fn
        self.timeout = timeout
        self.runner = runner
        self._deadline_secs: float | None = None

    def set_deadline(self, remaining_secs: float | None) -> None:
        self._deadline_secs = remaining_secs

    def _effective_timeout(self) -> int:
        if self._deadline_secs is None:
            return self.timeout
        return max(1, min(self.timeout, int(self._deadline_secs)))

    def execute(self, system: str, task: str, tools: list[Tool], model: str,
                cwd: Path, on_event: Callable[[ExecEvent], None]) -> ExecuteResult:
        prompt = f"{system}\n\n{task}"
        argv = self.argv_fn(prompt, cwd, model)
        timeout = self._effective_timeout()
        on_event(ExecEvent("note", {"text": f"agent: {argv[0]} {argv[1] if len(argv) > 1 else ''}"}))
        try:
            proc = self.runner(argv, cwd=cwd, capture_output=True,
                               text=True, timeout=timeout,
                               stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            on_event(ExecEvent("note", {"text": f"agent timeout after {timeout}s"}))
            return ExecuteResult(text=f"[agent timeout after {timeout}s]",
                                 usage=Usage(), steps=[], stop_reason="timeout")
        except FileNotFoundError as e:
            on_event(ExecEvent("note", {"text": f"agent binary not found: {argv[0]} ({e})"}))
            return ExecuteResult(text=f"[agent binary not found: {argv[0]}]",
                                 usage=Usage(), steps=[])
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            on_event(ExecEvent("note", {"text": f"agent exit {proc.returncode}: {err[:200]}"}))
            return ExecuteResult(text=err or f"[agent exit {proc.returncode}]",
                                 usage=Usage(), steps=[])
        text, usage = self.parse_fn(proc.stdout or "")
        on_event(ExecEvent("assistant", {"text": text}))
        return ExecuteResult(text=text, usage=usage, steps=[])


def _claude_argv(prompt: str, cwd: Path, model: str) -> list[str]:
    argv = ["claude", "-p", prompt, "--output-format", "json",
            "--permission-mode", "acceptEdits"]
    if model and model != "claude":
        argv += ["--model", model]  # e.g. "claude:opus" -> pass the concrete model id you use
    return argv


def _claude_parse(stdout: str) -> tuple[str, Usage]:
    try:
        data = json.loads(stdout)
    except (ValueError, json.JSONDecodeError):
        return stdout.strip(), Usage()
    text = str(data.get("result") or data.get("text") or "").strip()
    u = data.get("usage") or {}
    usage = Usage(input_tokens=int(u.get("input_tokens", 0) or 0),
                  output_tokens=int(u.get("output_tokens", 0) or 0),
                  cache_read_tokens=int(u.get("cache_read_input_tokens", 0) or 0))
    return text, usage


def _codex_argv(prompt: str, cwd: Path, model: str) -> list[str]:
    # --sandbox workspace-write lets the maker edit files in the worktree
    # (exec defaults to read-only, which would block all edits). The judge
    # uses read-only separately (see gates/agent_judge.py).
    # -a never (--ask-for-approval never): setpoint executors always run
    # non-interactively, so an approval prompt -- including for MCP tool
    # calls like scry_task_claim -- has no one to answer it and codex
    # auto-denies it ("user cancelled MCP tool call"). "never" lets
    # sandboxed tool/MCP calls proceed without stalling on approval.
    argv = ["codex", "exec", "--json", "--sandbox", "workspace-write",
            "-a", "never"]
    if model and model != "codex":
        argv += ["--model", model]
    argv.append(prompt)
    return argv


def _codex_parse(stdout: str) -> tuple[str, Usage]:
    # codex exec --json streams JSONL events; take the last assistant/result line.
    text = stdout.strip()
    usage = Usage()
    last = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            continue
        if isinstance(ev, dict):
            last = ev
    if isinstance(last, dict):
        text = str(last.get("message") or last.get("text") or last.get("result") or text).strip()
        u = last.get("usage") or {}
        usage = Usage(input_tokens=int(u.get("input_tokens", 0) or 0),
                      output_tokens=int(u.get("output_tokens", 0) or 0))
    return text, usage


class ClaudeExecutor(AgentCLIExecutor):
    def __init__(self, timeout: int = 1800, runner=subprocess.run):
        super().__init__(_claude_argv, _claude_parse, timeout=timeout, runner=runner)


class CodexExecutor(AgentCLIExecutor):
    def __init__(self, timeout: int = 1800, runner=subprocess.run):
        super().__init__(_codex_argv, _codex_parse, timeout=timeout, runner=runner)


def _kimi_argv(prompt: str, cwd: Path, model: str) -> list[str]:
    # --auto: fully autonomous prompt mode (kimi's analog of acceptEdits).
    # Text output: kimi's stream-json event shape is undocumented, and the
    # gate — not the transcript — decides success, so raw text is enough.
    argv = ["kimi", "-p", prompt, "--output-format", "text", "--auto"]
    if model and model != "kimi":
        argv += ["-m", model]
    return argv


def _kimi_parse(stdout: str) -> tuple[str, Usage]:
    return stdout.strip(), Usage()


class KimiExecutor(AgentCLIExecutor):
    def __init__(self, timeout: int = 1800, runner=subprocess.run):
        super().__init__(_kimi_argv, _kimi_parse, timeout=timeout, runner=runner)
