from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from setpoint.fleet import run_fleet


class FakeRoom:
    def __init__(self):
        self.calls = []
        self.msgs = []
        self._n = 0

    def _id(self):
        self._n += 1
        return f"t{self._n}"

    def create_room(self, run_id, repo):
        self.calls.append(("create", run_id, repo))
        return {"id": "room1", "status": "open"}

    def post_task(self, room_id, title, body="", depends_on=None, interfaces=""):
        tid = self._id()
        self.calls.append(("task", title, tuple(depends_on or [])))
        return {"id": tid, "title": title}

    def post(self, room_id, from_, kind, body, task_id=""):
        self.msgs.append((kind, from_, body))
        return {"seq": len(self.msgs)}

    def read(self, room_id, cursor=0, limit=50):
        if cursor:
            return {"messages": [], "cursor": cursor}
        out = [{"seq": i + 1, "kind": k, "from": f, "body": b, "task_id": ""}
               for i, (k, f, b) in enumerate(self.msgs)]
        return {"messages": out, "cursor": len(out)}

    def list_tasks(self, room_id):
        return []

    def close_room(self, room_id):
        self.calls.append(("close", room_id))
        return {"id": room_id, "status": "closed"}

    def close(self):
        self.calls.append(("client_close",))


def _write_bundle(tmp_path: Path) -> Path:
    for name, engine, deps in (("api", "claude", []), ("ui", "codex", ["api"])):
        (tmp_path / f"{name}.setpoint.yaml").write_text(yaml.safe_dump({
            "name": name, "type": "coding", "goal": f"do {name}",
            "workspace": {"repo": str(tmp_path), "worktree": False},
            "execute": {"engine": engine},
            "verify": {"gate": "command", "command": "true"},
            "deliver": {},
        }, sort_keys=False))
    fleet = tmp_path / "fleet.yaml"
    fleet.write_text(yaml.safe_dump({
        "name": "demo", "concurrency": 2,
        "members": ["./api.setpoint.yaml", "./ui.setpoint.yaml"],
        "room": {"repo": str(tmp_path),
                 "tasks": [
                     {"member": "api", "title": "API", "interfaces": "GET /x",
                      "depends_on": []},
                     {"member": "ui", "title": "UI", "interfaces": "",
                      "depends_on": ["api"]},
                 ]},
    }, sort_keys=False))
    return fleet


def test_room_mode_orchestration(tmp_path, monkeypatch):
    monkeypatch.setenv("SETPOINT_RUNS_ROOT", str(tmp_path / "runs"))
    room = FakeRoom()
    seen_notes = {}
    reviews = []

    class State:
        status = "passed"

    def fake_run_loop(spec, *, fresh=False, ui=None, abort_check=None):
        # context.notes is a str (spec.py:26) -- Cycle._discover joins it as
        # a scalar, so a room-mode member must still see a plain string here.
        seen_notes[spec.name] = spec.context.notes
        return State()

    def fake_oneshot(engine, prompt):
        reviews.append((engine, prompt))
        return "APPROVED"

    results = run_fleet(str(_write_bundle(tmp_path)), run_loop=fake_run_loop,
                        room_client=room, oneshot=fake_oneshot)

    assert results == {"api": "passed", "ui": "passed"}
    # room lifecycle
    assert room.calls[0] == ("create", "demo", str(tmp_path))
    assert ("task", "API", ()) in room.calls
    assert ("task", "UI", ("t1",)) in room.calls  # dep resolved to room task id
    assert room.calls[-2:] == [("close", "room1"), ("client_close",)]
    # room context injected into member notes (as a string, not a list --
    # context.notes is a plain str in spec.py and Cycle._discover joins it
    # as a scalar)
    api_notes = seen_notes["api"]
    assert isinstance(api_notes, str)
    assert "room_id: room1" in api_notes and "task_id: t1" in api_notes
    assert "agent: claude-api" in api_notes
    # cross-review dispatched with a different engine than the author
    assert len(reviews) == 2
    for engine, prompt in reviews:
        assert "room1" in prompt
    api_review = next(p for e, p in reviews if "setpoint/api" in p)
    api_reviewer = next(e for e, p in reviews if "setpoint/api" in p)
    assert api_reviewer != "claude"
    # report written with transcript, alongside status.md's established
    # location: runs_root.parent / "fleets" / <name> (runs_root here is
    # <tmp>/runs, so its parent is <tmp>)
    report = (tmp_path / "fleets" / "demo" / "report.md").read_text()
    assert "Room transcript" in report and "launched" in report


def test_no_room_section_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("SETPOINT_RUNS_ROOT", str(tmp_path / "runs"))
    (tmp_path / "solo.setpoint.yaml").write_text(yaml.safe_dump({
        "name": "solo", "type": "coding", "goal": "g",
        "workspace": {"repo": str(tmp_path), "worktree": False},
        "execute": {"engine": "claude"},
        "verify": {"gate": "command", "command": "true"},
        "deliver": {},
    }, sort_keys=False))
    fleet = tmp_path / "fleet.yaml"
    fleet.write_text(yaml.safe_dump({"name": "plain",
                                     "members": ["./solo.setpoint.yaml"]}))

    class State:
        status = "passed"

    boom = object()  # a room_client that must never be touched

    results = run_fleet(str(fleet),
                        run_loop=lambda spec, **kw: State(),
                        room_client=boom)
    assert results == {"solo": "passed"}


def _write_member(tmp_path: Path, name: str, engine: str) -> None:
    (tmp_path / f"{name}.setpoint.yaml").write_text(yaml.safe_dump({
        "name": name, "type": "coding", "goal": f"do {name}",
        "workspace": {"repo": str(tmp_path), "worktree": False},
        "execute": {"engine": engine},
        "verify": {"gate": "command", "command": "true"},
        "deliver": {},
    }, sort_keys=False))


def test_room_tasks_forward_reference_resolves(tmp_path, monkeypatch):
    """A room.tasks entry may declare depends_on before the member it depends
    on appears later in the list -- tasks must still post in an order that
    satisfies dependencies, not file order."""
    monkeypatch.setenv("SETPOINT_RUNS_ROOT", str(tmp_path / "runs"))
    _write_member(tmp_path, "api", "claude")
    _write_member(tmp_path, "ui", "codex")
    fleet = tmp_path / "fleet.yaml"
    fleet.write_text(yaml.safe_dump({
        "name": "demo", "concurrency": 2,
        "members": ["./api.setpoint.yaml", "./ui.setpoint.yaml"],
        "room": {"repo": str(tmp_path),
                 "tasks": [
                     {"member": "ui", "title": "UI", "depends_on": ["api"]},
                     {"member": "api", "title": "API", "depends_on": []},
                 ]},
    }, sort_keys=False))
    room = FakeRoom()

    class State:
        status = "passed"

    run_fleet(str(fleet), run_loop=lambda spec, **kw: State(),
             room_client=room, oneshot=lambda e, p: "APPROVED")

    # "api" is posted first (gets t1) despite being declared second in
    # room.tasks; "ui"'s depends_on resolves to that id.
    assert ("task", "API", ()) in room.calls
    assert ("task", "UI", ("t1",)) in room.calls


def test_room_tasks_unknown_depends_on_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("SETPOINT_RUNS_ROOT", str(tmp_path / "runs"))
    _write_member(tmp_path, "api", "claude")
    fleet = tmp_path / "fleet.yaml"
    fleet.write_text(yaml.safe_dump({
        "name": "demo",
        "members": ["./api.setpoint.yaml"],
        "room": {"repo": str(tmp_path),
                 "tasks": [{"member": "api", "title": "API",
                            "depends_on": ["ghost"]}]},
    }, sort_keys=False))
    room = FakeRoom()

    with pytest.raises(ValueError, match="ghost"):
        run_fleet(str(fleet), run_loop=lambda spec, **kw: None,
                 room_client=room, oneshot=lambda e, p: "APPROVED")

    # the dependency error is raised after create_room but before any task
    # is posted -- teardown must still close the room.
    assert ("close", "room1") in room.calls
    assert ("client_close",) in room.calls


def test_room_tasks_malformed_entry_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("SETPOINT_RUNS_ROOT", str(tmp_path / "runs"))
    _write_member(tmp_path, "api", "claude")
    fleet = tmp_path / "fleet.yaml"
    fleet.write_text(yaml.safe_dump({
        "name": "demo",
        "members": ["./api.setpoint.yaml"],
        "room": {"repo": str(tmp_path),
                 "tasks": [{"member": "api"}]},  # missing title
    }, sort_keys=False))

    with pytest.raises(ValueError, match=r"room\.tasks\[0\]"):
        run_fleet(str(fleet), run_loop=lambda spec, **kw: None,
                 room_client=FakeRoom(), oneshot=lambda e, p: "APPROVED")


def test_room_setup_failure_still_closes_room(tmp_path, monkeypatch):
    """A post_task RPC failure mid-setup must not leak the room/subprocess:
    the room was already created (has a room_id) by the time it fails."""
    monkeypatch.setenv("SETPOINT_RUNS_ROOT", str(tmp_path / "runs"))
    _write_member(tmp_path, "api", "claude")
    _write_member(tmp_path, "ui", "codex")
    fleet = tmp_path / "fleet.yaml"
    fleet.write_text(yaml.safe_dump({
        "name": "demo", "concurrency": 2,
        "members": ["./api.setpoint.yaml", "./ui.setpoint.yaml"],
        "room": {"repo": str(tmp_path),
                 "tasks": [
                     {"member": "api", "title": "API", "depends_on": []},
                     {"member": "ui", "title": "UI", "depends_on": ["api"]},
                 ]},
    }, sort_keys=False))

    class FailingPostTaskRoom(FakeRoom):
        def post_task(self, room_id, title, body="", depends_on=None, interfaces=""):
            if title == "UI":
                raise RuntimeError("scry mcp: post_task boom")
            return super().post_task(room_id, title, body=body,
                                     depends_on=depends_on, interfaces=interfaces)

    room = FailingPostTaskRoom()

    with pytest.raises(RuntimeError, match="boom"):
        run_fleet(str(fleet), run_loop=lambda spec, **kw: None,
                 room_client=room, oneshot=lambda e, p: "APPROVED")

    assert ("create", "demo", str(tmp_path)) in room.calls
    assert ("close", "room1") in room.calls
    assert ("client_close",) in room.calls
