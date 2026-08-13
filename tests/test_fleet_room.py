from __future__ import annotations

from pathlib import Path

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
        seen_notes[spec.name] = list(spec.context.notes)
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
    # room context injected into member notes
    api_notes = "\n".join(seen_notes["api"])
    assert "room_id: room1" in api_notes and "task_id: t1" in api_notes
    assert "agent: claude-api" in api_notes
    # cross-review dispatched with a different engine than the author
    assert len(reviews) == 2
    for engine, prompt in reviews:
        assert "room1" in prompt
    api_review = next(p for e, p in reviews if "setpoint/api" in p)
    api_reviewer = next(e for e, p in reviews if "setpoint/api" in p)
    assert api_reviewer != "claude"
    # report written with transcript
    report = (tmp_path / "runs" / "fleets" / "demo" / "report.md").read_text()
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
