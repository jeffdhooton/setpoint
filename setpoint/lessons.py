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
                existing.ts = max(existing.ts, sl.ts)
                if sl.lesson:
                    existing.lesson = sl.lesson  # keep the freshest phrasing
            else:
                by_fp[sl.fingerprint] = sl
        kept = sorted(by_fp.values(), key=lambda sl: (sl.hits, sl.ts), reverse=True)[:CAP]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".jsonl.tmp")
        tmp.write_text("".join(json.dumps(asdict(sl)) + "\n" for sl in kept))
        tmp.replace(self.path)
        return kept
