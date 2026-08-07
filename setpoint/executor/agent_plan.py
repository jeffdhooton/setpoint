from __future__ import annotations

from dataclasses import dataclass

_PLAN_TEXT = ("Proceed toward the goal. You are a full coding agent — discover, plan, "
              "and make the smallest next change yourself, then stop.")


@dataclass
class _Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    prompt_cache_hit_tokens: int = 0


@dataclass
class _Message:
    content: str


@dataclass
class _Choice:
    message: _Message


@dataclass
class _Response:
    choices: list
    usage: _Usage


class _Completions:
    def create(self, model=None, messages=None, **kwargs):
        return _Response(choices=[_Choice(_Message(_PLAN_TEXT))], usage=_Usage())


class _Chat:
    def __init__(self):
        self.completions = _Completions()


class AgentPlanClient:
    """A plan_client shim for agent engines. The agent plans internally during
    EXECUTE, so the PLAN stage is a zero-cost pass-through that keeps cycle.py
    unchanged."""

    is_noop = True  # cycle/analyze skip LLM prompting through this shim

    def __init__(self):
        self.chat = _Chat()
