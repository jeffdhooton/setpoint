from __future__ import annotations

import abc
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class GateResult:
    passed: bool
    feedback: str
    score: float | None = None
    returncode: int | None = None
    timed_out: bool = False


class Gate(abc.ABC):
    # True only for gates cheap+deterministic enough to run cold before iter 1.
    supports_preflight = False

    @abc.abstractmethod
    def verify(self, cwd: Path, on_event: Callable) -> GateResult:
        ...


def build_gate(spec, judge_client=None, env=None) -> Gate:
    from .command import CommandGate
    from .judge import JudgeGate

    if spec.verify.gate == "command":
        return CommandGate(command=spec.verify.command,
                           timeout=getattr(spec.verify, "timeout_secs", 600),
                           env=env)
    rubric_text = Path(spec.verify.rubric).expanduser().read_text()
    artifact = spec.deliver.get("artifact") if spec.deliver else None
    judge_model = spec.verify.judge_model
    extra_body = None if judge_model.startswith("deepseek") else {"reasoning_effort": "none"}
    diff_base = spec.deliver.get("base") if spec.deliver else None
    return JudgeGate(client=judge_client, model=judge_model,
                     rubric_text=rubric_text, threshold=spec.verify.pass_threshold,
                     artifact=artifact, extra_body=extra_body,
                     checks=spec.verify.checks, diff_base=diff_base)
