from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from typing import Callable

from setpoint.clip import clip
from . import Gate, GateResult


class CommandGate(Gate):
    supports_preflight = True

    def __init__(self, command: str, timeout: float = 600, env: dict | None = None):
        self.command = command
        self.timeout = timeout
        # Merged over os.environ for the verify subprocess. Carries
        # SETPOINT_PORT_BASE so the gate measures its own worktree's stack,
        # not a sibling's on a reused port.
        self.env = env

    def verify(self, cwd: Path, on_event: Callable) -> GateResult:
        on_event({"kind": "verify_start", "command": self.command})
        # start_new_session + killpg: a leaked background child would otherwise
        # hold the output pipe open and block communicate() past the shell's exit.
        proc = subprocess.Popen(self.command, shell=True, cwd=cwd,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True,
                                env={**os.environ, **(self.env or {})})
        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            # start_new_session makes the shell the group leader (pgid == pid),
            # so kill the group by pid — getpgid would fail once the shell has
            # exited even while orphaned children still hold the pipe.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            stdout, stderr = proc.communicate()
        out = clip(((stdout or "") + (stderr or "")).strip())
        if timed_out:
            feedback = (f"verify command timed out after {self.timeout}s "
                        "(hung, or left a background process holding its output pipe)")
            if out:
                feedback += f"\npartial output:\n{out}"
            return GateResult(passed=False, feedback=feedback, timed_out=True)
        passed = proc.returncode == 0
        feedback = out if not passed else "all checks passed"
        return GateResult(passed=passed, feedback=feedback or f"exit {proc.returncode}",
                          returncode=proc.returncode)
