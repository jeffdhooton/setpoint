from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

CAP = 100


@dataclass
class StoredLesson:
    ts: str
    run: str
    goal: str
    fingerprint: str
    normalized: str
    category: str
    lesson: str
    hits: int = 1
    validated: bool = True


def repo_key(repo: Path) -> str:
    base = ""
    try:
        out = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            base = out.stdout.strip()
    except Exception:
        pass
    if not base:
        base = str(Path(repo).resolve())
    return re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")[:100]


class LessonStore:
    def __init__(self, key: str, root: Path | None = None):
        root = root or Path(os.environ.get(
            "SETPOINT_LESSONS_ROOT", str(Path.home() / ".setpoint" / "lessons")))
        self.path = Path(root) / f"{key}.jsonl"

    def load(self) -> list[StoredLesson]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                out.append(StoredLesson(**json.loads(line)))
            except (json.JSONDecodeError, TypeError):
                continue  # corrupt line: skip, keep going
        return out

    def top(self, k: int = 10) -> list[StoredLesson]:
        return sorted(self.load(), key=lambda sl: (sl.hits, sl.ts), reverse=True)[:k]

    def promote(self, new: list[StoredLesson]) -> list[StoredLesson]:
        by_fp = {sl.fingerprint: sl for sl in self.load()}
        for sl in new:
            if sl.fingerprint in by_fp:
                existing = by_fp[sl.fingerprint]
                existing.hits += 1
                if sl.lesson and sl.ts >= existing.ts:
                    existing.lesson = sl.lesson  # keep the freshest phrasing
                existing.ts = max(existing.ts, sl.ts)
            else:
                by_fp[sl.fingerprint] = sl
        kept = sorted(by_fp.values(), key=lambda sl: (sl.hits, sl.ts), reverse=True)[:CAP]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".jsonl.tmp")
        tmp.write_text("".join(json.dumps(asdict(sl)) + "\n" for sl in kept))
        tmp.replace(self.path)
        return kept


from datetime import datetime, timezone


def promote_validated(state, goal: str, store: LessonStore,
                      now: str | None = None) -> list[StoredLesson]:
    """Promote lessons whose next iteration passed or changed the failure.
    `state` is a setpoint.memory.RunState (duck-typed to avoid a cycle)."""
    ts = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    candidates = []
    iters = state.iters
    for i, r in enumerate(iters):
        if r.passed or not r.lesson or not r.fingerprint:
            continue
        if i + 1 >= len(iters):
            continue  # nothing after it: unvalidated
        nxt = iters[i + 1]
        if nxt.passed or (nxt.fingerprint and nxt.fingerprint != r.fingerprint):
            candidates.append(StoredLesson(
                ts=ts, run=state.name, goal=goal,
                fingerprint=r.fingerprint,
                normalized="",  # filled below from analyze to avoid storing raw feedback twice
                category=r.category, lesson=r.lesson))
    if not candidates:
        return []
    # normalized text is recomputable from feedback; store it for near-matching
    from setpoint.analyze import normalize_feedback
    by_fp = {r.fingerprint: r.feedback for r in iters if r.fingerprint}
    for sl in candidates:
        sl.normalized = normalize_feedback(by_fp.get(sl.fingerprint, ""))
    store.promote(candidates)
    return candidates
