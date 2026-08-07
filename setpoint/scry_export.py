from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from setpoint.lessons import StoredLesson


def export_lessons(lessons: list[StoredLesson], repo: Path) -> int:
    """Best-effort push of promoted lessons into scry's global memory.
    Shells the `scry` binary; any failure logs one line and moves on."""
    ok = 0
    for sl in lessons:
        fact = (f"setpoint lesson ({sl.category or 'general'}) while working on "
                f"'{sl.goal}': {sl.lesson}")
        try:
            r = subprocess.run(
                ["scry", "memory", "remember", fact, "--repo", str(repo)],
                capture_output=True, timeout=10)
            if r.returncode == 0:
                ok += 1
        except Exception as e:
            print(f"scry export skipped: {e}", file=sys.stderr)
            return ok
    return ok
