from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# The full self-tuning surface. Anything not listed here (gate, budget,
# delivery, max_iters, models) is off-limits to RETRO by construction.
BOUNDS = {"max_turns": (10, 50), "no_progress_after": (2, 6)}
PLAN_HINT_MAX = 400


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80]


def better_or_equal(a: dict, b: dict) -> bool:
    """Lexicographic run-outcome comparison: passing beats not, then fewer
    iterations, then cheaper."""
    ka = (bool(a.get("passed")), -int(a.get("iters", 0)), -float(a.get("usd", 0.0)))
    kb = (bool(b.get("passed")), -int(b.get("iters", 0)), -float(b.get("usd", 0.0)))
    return ka >= kb


def _clamp(name: str, value: int) -> int:
    lo, hi = BOUNDS[name]
    return max(lo, min(hi, int(value)))


def apply_overlay(spec, knobs: dict) -> None:
    if not knobs:
        return
    if "max_turns" in knobs and "execute.max_turns" not in spec.explicit:
        try:
            spec.execute.max_turns = _clamp("max_turns", knobs["max_turns"])
        except (TypeError, ValueError) as e:
            print(f"tuning: ignoring invalid max_turns overlay value "
                  f"{knobs['max_turns']!r}: {e}", file=sys.stderr)
    if "no_progress_after" in knobs and "stop.no_progress_after" not in spec.explicit:
        try:
            spec.stop.no_progress_after = _clamp("no_progress_after", knobs["no_progress_after"])
        except (TypeError, ValueError) as e:
            print(f"tuning: ignoring invalid no_progress_after overlay value "
                  f"{knobs['no_progress_after']!r}: {e}", file=sys.stderr)
    if knobs.get("plan_hint"):
        spec.execute.plan_hint = str(knobs["plan_hint"])[:PLAN_HINT_MAX]


class Overlay:
    def __init__(self, key: str, root: Path | None = None):
        root = root or Path(os.environ.get(
            "SETPOINT_TUNING_ROOT", str(Path.home() / ".setpoint" / "tuning")))
        self.path = Path(root) / f"{key}.json"

    def _read(self) -> dict:
        if not self.path.exists():
            return {"versions": []}
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(f"tuning: overlay unreadable, ignoring ({self.path}): {e}",
                  file=sys.stderr)
            return {"versions": []}
        if not isinstance(data, dict) or not isinstance(data.get("versions"), list):
            print(f"tuning: overlay malformed, ignoring ({self.path})", file=sys.stderr)
            return {"versions": []}
        versions = data["versions"]
        # Only the last element is ever read (load()/reconcile() both key off
        # versions[-1], and _read() re-parses from disk on every call — a
        # revert's pop() is followed by a fresh _read() next time, not a
        # reuse of this list — so validating just the tail is sufficient.
        if versions:
            last = versions[-1]
            if not isinstance(last, dict) or not isinstance(last.get("knobs"), dict):
                print(f"tuning: overlay malformed, ignoring ({self.path})", file=sys.stderr)
                return {"versions": []}
        return data

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.path)

    def load(self) -> dict:
        versions = self._read()["versions"]
        return dict(versions[-1]["knobs"]) if versions else {}

    def push(self, knobs: dict, stats: dict) -> None:
        data = self._read()
        data["versions"].append({"knobs": knobs, "stats": stats})
        self._write(data)

    def reconcile(self, run_stats: dict) -> str:
        data = self._read()
        if not data["versions"]:
            return "empty"
        current = data["versions"][-1]
        if better_or_equal(run_stats, current.get("stats") or {}):
            current["stats"] = run_stats
            self._write(data)
            return "kept"
        data["versions"].pop()
        self._write(data)
        return "reverted"
