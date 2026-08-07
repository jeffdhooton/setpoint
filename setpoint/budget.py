from __future__ import annotations

import time
from dataclasses import dataclass

# USD per 1,000,000 tokens. Sourced from DeepSeek docs (2026-06-21).
PRICING: dict[str, dict[str, float]] = {
    "deepseek-v4-pro": {"input": 1.74, "cache_read": 0.145, "output": 3.48},
    "deepseek-v4-flash": {"input": 0.14, "cache_read": 0.028, "output": 0.28},
    "gpt-oss-20b": {"input": 0.0, "cache_read": 0.0, "output": 0.0},  # local OMLX
    "claude": {"input": 0.0, "cache_read": 0.0, "output": 0.0},  # subscription — unmetered on tokens
    "codex": {"input": 0.0, "cache_read": 0.0, "output": 0.0},   # subscription — unmetered on tokens
}

_M = 1_000_000.0


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    def cost(self, model: str, pricing: dict) -> float:
        p = pricing.get(model)
        if p is None:  # unknown model -> treat as free, never crash a run on pricing
            return 0.0
        fresh_input = max(self.input_tokens - self.cache_read_tokens, 0)
        return (
            fresh_input / _M * p["input"]
            + self.cache_read_tokens / _M * p["cache_read"]
            + self.output_tokens / _M * p["output"]
        )


class Budget:
    def __init__(self, max_usd: float | None, max_tokens: int | None, pricing: dict,
                 wall_clock_secs: int | None = None):
        self.max_usd = max_usd
        self.max_tokens = max_tokens
        self.pricing = pricing
        self.wall_clock_secs = wall_clock_secs
        self.spent_usd = 0.0
        self.tokens = 0
        self._start = time.monotonic()

    def add(self, model: str, usage: Usage) -> None:
        self.spent_usd += usage.cost(model, self.pricing)
        self.tokens += usage.input_tokens + usage.output_tokens

    def remaining_secs(self) -> float | None:
        if self.wall_clock_secs is None:
            return None
        return max(0.0, self.wall_clock_secs - (time.monotonic() - self._start))

    def should_stop(self) -> bool:
        if self.max_usd is not None and self.spent_usd >= self.max_usd:
            return True
        if self.max_tokens is not None and self.tokens >= self.max_tokens:
            return True
        if self.wall_clock_secs is not None and (time.monotonic() - self._start) >= self.wall_clock_secs:
            return True
        return False

    def warn(self) -> bool:
        if self.max_usd is not None and self.spent_usd >= 0.8 * self.max_usd:
            return True
        if self.max_tokens is not None and self.tokens >= 0.8 * self.max_tokens:
            return True
        return False
