from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from setpoint.tuning import BOUNDS, PLAN_HINT_MAX, Overlay

_DEFAULT_MAX_TURNS = 25
_DEFAULT_NO_PROGRESS = 4


def _clamp(name: str, value: int) -> int:
    lo, hi = BOUNDS[name]
    return max(lo, min(hi, int(value)))


@dataclass
class RunStats:
    passed: bool
    iters: int
    usd: float
    repeat_strikes: int
    cutoffs: int


def compute_stats(state) -> RunStats:
    return RunStats(
        passed=(state.status == "passed"),
        iters=len(state.iters),
        usd=state.spent_usd,
        repeat_strikes=sum(1 for r in state.iters if r.repeat_of),
        cutoffs=sum(1 for r in state.iters if r.stop_reason != "done"),
    )


def propose_knobs(stats: RunStats, current: dict, state) -> dict | None:
    knobs = dict(current)

    # Cut-off EXECUTE stages mean steps can't finish inside the turn budget.
    if stats.cutoffs >= 2:
        knobs["max_turns"] = _clamp(
            "max_turns", current.get("max_turns", _DEFAULT_MAX_TURNS) + 10)
    elif stats.cutoffs == 0 and current.get("max_turns", _DEFAULT_MAX_TURNS) > _DEFAULT_MAX_TURNS:
        knobs["max_turns"] = max(_DEFAULT_MAX_TURNS, current["max_turns"] - 5)

    # Repeated known mistakes with no pass: stop such runs sooner.
    if stats.repeat_strikes >= 2 and not stats.passed:
        knobs["no_progress_after"] = _clamp(
            "no_progress_after", current.get("no_progress_after", _DEFAULT_NO_PROGRESS) - 1)

    # Most-repeated lesson becomes standing guidance for the next run's plans.
    repeated = Counter(r.repeat_of for r in state.iters if r.repeat_of)
    if repeated:
        top_fp = repeated.most_common(1)[0][0]
        lesson = next((r.lesson for r in state.iters
                       if r.fingerprint == top_fp and r.lesson), "")
        if lesson:
            knobs["plan_hint"] = f"Known pitfall: {lesson}"[:PLAN_HINT_MAX]

    return knobs if knobs != current else None


def run_retro(state, overlay: Overlay, out_dir: Path) -> Path | None:
    """Post-run self-tuning. Never raises — a retro failure must not mark a
    passed run as failed."""
    try:
        stats = compute_stats(state)
        action = overlay.reconcile(asdict(stats))
        current = overlay.load()
        proposed = propose_knobs(stats, current, state)
        if proposed is not None:
            overlay.push(proposed, asdict(stats))
        lines = [
            "# Retro", "",
            f"- status: {state.status}",
            f"- iterations: {stats.iters}  cost: ${stats.usd:.4f}",
            f"- repeat strikes: {stats.repeat_strikes}  execute cutoffs: {stats.cutoffs}",
            f"- overlay reconcile: {action}",
            f"- knobs now: {overlay.load() or 'defaults'}",
        ]
        if proposed:
            lines.append(f"- proposed change: {proposed}")
        if stats.cutoffs >= 2:
            lines.append("- critique: EXECUTE was cut off repeatedly — steps too "
                         "big for the turn budget; raised max_turns.")
        if stats.repeat_strikes >= 2:
            lines.append("- critique: the loop repeated known mistakes — "
                         "tightened no-progress tolerance.")
        out = Path(out_dir) / "retro.md"
        out.write_text("\n".join(lines) + "\n")
        return out
    except Exception as e:
        import sys
        print(f"retro skipped: {e}", file=sys.stderr)
        return None
