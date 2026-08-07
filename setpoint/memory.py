from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class IterRecord:
    n: int
    plan: str
    summary: str
    passed: bool
    feedback: str
    usd: float
    score: float | None = None
    # "done", or why EXECUTE was cut off ("max_turns", "timeout"). Defaulted so
    # state.json written before this field existed still loads.
    stop_reason: str = "done"
    # Stage-1 self-improvement fields. Defaulted so pre-existing state.json loads.
    lesson: str = ""       # imperative rule distilled by ANALYZE ("" = none)
    category: str = ""     # model-assigned failure class
    fingerprint: str = ""  # deterministic failure signature (12 hex chars)
    repeat_of: str = ""    # fingerprint of the prior lesson this failure repeated


@dataclass
class RunState:
    name: str
    status: str = "new"  # new | running | passed | stopped | budget_exhausted
    iters: list[IterRecord] = field(default_factory=list)
    spent_usd: float = 0.0


class Memory:
    def __init__(self, name: str, root: Path | None = None):
        self.name = name
        self.root = (root or (Path.home() / ".setpoint" / "runs")) / name
        self.state_path = self.root / "state.json"
        self.log_path = self.root / "log.md"

    def start(self) -> RunState:
        self.root.mkdir(parents=True, exist_ok=True)
        state = self.load()
        if state.status == "new":
            state.status = "running"
            self._write(state)
        return state

    def load(self) -> RunState:
        if not self.state_path.exists():
            return RunState(name=self.name)
        raw = json.loads(self.state_path.read_text())
        return RunState(
            name=raw["name"],
            status=raw.get("status", "new"),
            iters=[IterRecord(**r) for r in raw.get("iters", [])],
            spent_usd=raw.get("spent_usd", 0.0),
        )

    def append(self, rec: IterRecord) -> None:
        state = self.load()
        state.iters.append(rec)
        state.spent_usd += rec.usd
        self._write(state)
        self._append_log(rec)

    def set_status(self, status: str) -> None:
        state = self.load()
        state.status = status
        self._write(state)

    def context_block(self) -> str:
        state = self.load()
        if not state.iters:
            return "No previous iterations."
        lines = ["## Loop history (memory spine)"]
        for r in state.iters:
            verdict = "PASS" if r.passed else "FAIL"
            if r.stop_reason != "done":
                verdict += f", EXECUTE cut off: {r.stop_reason}"
            lines.append(f"- iteration {r.n} [{verdict}]: {r.summary}")
            if not r.passed and r.feedback:
                lines.append(f"  feedback: {r.feedback}")
        lessons: dict[str, str] = {}
        for r in state.iters:
            if r.lesson and r.fingerprint not in lessons:
                lessons[r.fingerprint] = r.lesson
        if lessons:
            lines.append("")
            lines.append("## Lessons so far")
            for fp, text in lessons.items():
                lines.append(f"- [{fp}] {text}")
        return "\n".join(lines)

    def _write(self, state: RunState) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(state), indent=2))
        tmp.replace(self.state_path)  # atomic

    def note(self, text: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a") as f:
            f.write(f"\n> note: {text}\n")

    def _append_log(self, rec: IterRecord) -> None:
        verdict = "✅ PASS" if rec.passed else "❌ FAIL"
        if rec.stop_reason != "done":
            verdict += f" ⚠️ EXECUTE cut off: {rec.stop_reason}"
        if rec.repeat_of:
            verdict += f" ⚠️ repeat of lesson {rec.repeat_of}"
        block = (
            f"\n## Iteration {rec.n} — {verdict} (${rec.usd:.4f})\n\n"
            f"**Plan:** {rec.plan}\n\n"
            f"**Did:** {rec.summary}\n\n"
            f"**Verify:** {rec.feedback}\n"
        )
        if rec.lesson:
            block += f"\n**Lesson:** {rec.lesson}\n"
        with self.log_path.open("a") as f:
            f.write(block)
