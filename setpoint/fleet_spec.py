from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class FleetSpec:
    name: str
    members: list[Path] = field(default_factory=list)
    concurrency: int = 4
    room: dict | None = None


def load_fleet(path: str) -> FleetSpec:
    p = Path(path).expanduser()
    raw = yaml.safe_load(p.read_text()) or {}
    if not raw.get("name"):
        raise ValueError("fleet spec missing required field: name")
    members_raw = raw.get("members") or []
    if not members_raw:
        raise ValueError("fleet spec requires a non-empty members list")
    base = p.parent
    members = [(base / m).expanduser().resolve() for m in members_raw]
    c = raw.get("concurrency", 4)
    if c is None:
        c = 4  # explicit null → default
    c = int(c)
    if c < 1:
        raise ValueError("fleet concurrency must be >= 1")
    room = raw.get("room")
    if room:
        if not room.get("repo") or not room.get("tasks"):
            raise ValueError("fleet room section requires repo and tasks")
    else:
        room = None
    return FleetSpec(
        name=raw["name"],
        members=members,
        concurrency=c,
        room=room,
    )
