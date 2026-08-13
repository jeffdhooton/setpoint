from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from setpoint.fleet import run_fleet


class FakeRoom:
    def __init__(self):
        self.calls = []
        self.msgs = []
        self.records = []
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
        # `records` keeps the full message (task_id included) because the
        # orchestrator threads verdicts by task_id; `msgs` stays a 3-tuple
        # for the assertions that only care about kind/from/body.
        self.records.append({"seq": len(self.msgs), "kind": kind, "from": from_,
                             "body": body, "task_id": task_id})
        return {"seq": len(self.msgs)}

    def read(self, room_id, cursor=0, limit=50):
        if cursor:
            return {"messages": [], "cursor": cursor}
        return {"messages": list(self.records), "cursor": len(self.records)}

    def update_task_status(self, room_id, task_id, status):
        self.calls.append(("task_update", task_id, status))
        return {"id": task_id, "status": status}

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



def _approving_oneshot(room):
    """A reviewer one-shot that behaves like a real one: it posts its verdict
    into the room thread (which is where the orchestrator reads it from),
    rather than returning it on stdout."""
    import re

    def oneshot(engine, prompt, cwd=None):
        task_id = re.search(r"for task (\S+)", prompt)
        reviewer = re.search(r'from "([^"]+)"', prompt)
        if task_id and reviewer:
            room.post("room1", reviewer.group(1), "review", "APPROVED — clean",
                      task_id=task_id.group(1))
        return "APPROVED"

    return oneshot


def test_room_mode_orchestration(tmp_path, monkeypatch):
    monkeypatch.setenv("SETPOINT_RUNS_ROOT", str(tmp_path / "runs"))
    room = FakeRoom()
    seen_notes = {}
    seen_goals = {}
    reviews = []

    class State:
        status = "passed"

    def fake_run_loop(spec, *, fresh=False, ui=None, abort_check=None, runs_root=None):
        # context.notes is a str (spec.py:26) -- Cycle._discover joins it as
        # a scalar, so a room-mode member must still see a plain string here.
        seen_notes[spec.name] = spec.context.notes
        # spec.goal must also carry the room block: agent engines (claude/
        # codex/kimi) never consult DISCOVER's notes -- their plan client is
        # the no-op AgentPlanClient, so the executor prompt is built from
        # spec.goal alone.
        seen_goals[spec.name] = spec.goal
        return State()

    approve = _approving_oneshot(room)

    def fake_oneshot(engine, prompt, cwd=None):
        reviews.append((engine, prompt, cwd))
        return approve(engine, prompt, cwd)

    results = run_fleet(str(_write_bundle(tmp_path)), run_loop=fake_run_loop,
                        room_client=room, oneshot=fake_oneshot)

    # A gate pass is not the fleet outcome — a resolved approving review is.
    assert results == {"api": "review-approved", "ui": "review-approved"}
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
    api_goal = seen_goals["api"]
    assert isinstance(api_goal, str)
    assert "ROOM CONTEXT" in api_goal
    assert "room_id: room1" in api_goal and "task_id: t1" in api_goal
    # cross-review dispatched with a different engine than the author, and
    # run with cwd=repo (codex's sandbox / claude's trust context are
    # cwd-scoped, so the review must run inside the repo it targets)
    assert len(reviews) == 2
    for engine, prompt, cwd in reviews:
        assert "room1" in prompt
        assert cwd == str(tmp_path)
    api_review = next(p for e, p, c in reviews if "setpoint/api" in p)
    api_reviewer = next(e for e, p, c in reviews if "setpoint/api" in p)
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
             room_client=room, oneshot=lambda e, p, cwd=None: "APPROVED")

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
                 room_client=room, oneshot=lambda e, p, cwd=None: "APPROVED")

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
                 room_client=FakeRoom(), oneshot=lambda e, p, cwd=None: "APPROVED")


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
                 room_client=room, oneshot=lambda e, p, cwd=None: "APPROVED")

    assert ("create", "demo", str(tmp_path)) in room.calls
    assert ("close", "room1") in room.calls
    assert ("client_close",) in room.calls


def test_decompose_bundle_runs_room_mode(tmp_path, monkeypatch):
    """End-to-end: decompose() a two-task idea (one claude task, one kimi
    task) into a fleet bundle, then run_fleet it in room mode. This covers
    all three Criticals from the final review in one shot:
      1. spec.py's kimi model default (a bad default would surface as a
         DeepSeek model id being handed to the kimi CLI).
      2. __main__._build_plan_client routing kimi to AgentPlanClient instead
         of make_deepseek_client() (checked directly below, without
         DEEPSEEK_API_KEY, so a wrong route would raise SystemExit here).
      3. decompose's deliver dict being truthy (checked directly on the specs
         run_fleet actually hands to run_loop).
    """
    from setpoint.decompose import decompose

    idea = tmp_path / "idea.md"
    idea.write_text("Build a lead tracker")
    repo = tmp_path / "repo"
    repo.mkdir()

    def canned_oneshot(engine, prompt):
        return json.dumps({"tasks": [
            {"name": "build-api", "title": "Build the API",
             "goal": "Implement GET /leads", "interfaces": "", "depends_on": [],
             "verify_command": "true", "engine": "claude"},
            {"name": "build-worker", "title": "Build the worker",
             "goal": "Implement the background worker", "interfaces": "",
             "depends_on": [], "verify_command": "true", "engine": "kimi"},
        ]})

    fleet_path = decompose(str(idea), str(repo), ["claude", "kimi"],
                           str(tmp_path / "out"), oneshot=canned_oneshot)

    monkeypatch.setenv("SETPOINT_RUNS_ROOT", str(tmp_path / "runs"))
    seen_specs = {}

    class State:
        status = "passed"

    def fake_run_loop(spec, *, fresh=False, ui=None, abort_check=None, runs_root=None):
        seen_specs[spec.name] = spec
        return State()

    room = FakeRoom()
    results = run_fleet(str(fleet_path), run_loop=fake_run_loop, room_client=room,
                        oneshot=_approving_oneshot(room))

    assert results == {"build-api": "review-approved",
                       "build-worker": "review-approved"}
    assert set(seen_specs) == {"build-api", "build-worker"}

    # Critical 3: deliver must be truthy on every member spec, or run_loop's
    # `if getattr(spec, "deliver", None):` gate would skip commit/push/PR.
    for spec in seen_specs.values():
        assert spec.deliver

    # kimi task keeps engine == "kimi" all the way through the bundle, and
    # Critical 1: load_spec's default_model must not hand the kimi CLI a
    # DeepSeek model id (404) -- it should fall back to the "kimi" sentinel.
    assert seen_specs["build-worker"].execute.engine == "kimi"
    assert seen_specs["build-worker"].execute.model == "kimi"

    # Every member is room-coordinated: ROOM CONTEXT appended to notes, and
    # also to goal -- agent engines (claude/kimi here) only ever see the
    # executor prompt built from spec.goal (their plan client is the no-op
    # AgentPlanClient, so DISCOVER's notes never reach them).
    for spec in seen_specs.values():
        assert "ROOM CONTEXT" in spec.context.notes
        assert "ROOM CONTEXT" in spec.goal
        assert "room_id: room1" in spec.goal

    # Criticals 1 & 2, checked directly: _build_plan_client for a kimi spec
    # must return the no-op AgentPlanClient, not fall through to
    # make_deepseek_client() -- which would raise SystemExit here since
    # DEEPSEEK_API_KEY is deliberately unset.
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    from setpoint.__main__ import _build_plan_client
    from setpoint.executor.agent_plan import AgentPlanClient
    from openai import OpenAI

    kimi_spec = seen_specs["build-worker"]
    plan_client = _build_plan_client(kimi_spec)
    assert isinstance(plan_client, AgentPlanClient)
    assert not isinstance(plan_client, OpenAI)


def test_room_manifest_written(tmp_path, monkeypatch):
    import json as _json
    monkeypatch.setenv("SETPOINT_RUNS_ROOT", str(tmp_path / "runs"))
    from setpoint import fleet as fleet_mod
    monkeypatch.setattr(fleet_mod, "_runs_root", lambda: tmp_path / "runs")
    room = FakeRoom()

    class State:
        status = "passed"

    run_fleet(str(_write_bundle(tmp_path)), run_loop=lambda spec, **kw: State(),
              room_client=room, oneshot=lambda e, p, cwd=None: "APPROVED")
    manifest = _json.loads((tmp_path / "fleets" / "demo" / "room.json").read_text())
    assert manifest["room_id"] == "room1"
    assert manifest["run_id"] == "demo"
    assert set(manifest["members"]) == {"api", "ui"}
    assert manifest["members"]["api"].endswith("-api")
    local = _json.loads((tmp_path / "room.json").read_text())
    assert local == manifest


def test_closing_ceremony_declares_outcome(tmp_path, monkeypatch):
    monkeypatch.setenv("SETPOINT_RUNS_ROOT", str(tmp_path / "runs"))
    from setpoint import fleet as fleet_mod
    monkeypatch.setattr(fleet_mod, "_runs_root", lambda: tmp_path / "runs")

    class LingeringRoom(FakeRoom):
        def post_task(self, room_id, title, body="", depends_on=None, interfaces=""):
            t = super().post_task(room_id, title, body, depends_on, interfaces)
            # simulate workers leaving tasks non-terminal, with claims
            tasks[(("room1"), t["id"])] = {"id": t["id"], "title": title,
                                           "status": "review",
                                           "claimed_by": "claude-" + ("api" if title == "API" else "ui")}
            return t
        def list_tasks(self, room_id):
            return [t for (r, _), t in sorted(tasks.items()) if r == "room1"]
        def post(self, room_id, from_, kind, body, task_id=""):
            m = super().post(room_id, from_, kind, body, task_id)
            return m

    tasks = {}

    class State:
        status = "passed"

    class FailState:
        status = "stopped"

    def run_loop(spec, **kw):
        return State() if spec.name == "api" else FailState()

    room = LingeringRoom()
    run_fleet(str(_write_bundle(tmp_path)), run_loop=run_loop,
              room_client=room, oneshot=_approving_oneshot(room))

    updates = [c for c in room.calls if c[0] == "task_update"]
    finals = {c[2] for c in updates}
    assert finals == {"done", "abandoned"}  # passed member finalized, stopped member abandoned

    closing = [b for k, f, b in room.msgs if "FLEET CLOSED" in b]
    assert len(closing) == 1
    assert "Needs a human" in closing[0]
    assert "ended 'stopped'" in closing[0]

    report = (tmp_path / "fleets" / "demo" / "report.md").read_text()
    assert "## Outcome" in report and "Needs a human" in report


def _passing_run_loop(spec, *, fresh=False, ui=None, abort_check=None, runs_root=None):
    from types import SimpleNamespace
    return SimpleNamespace(status="passed")


def _write_single_engine_bundle(tmp_path: Path) -> Path:
    """Same shape as _write_bundle but both members run claude."""
    for name in ("api", "ui"):
        (tmp_path / f"{name}.setpoint.yaml").write_text(yaml.safe_dump({
            "name": name, "type": "coding", "goal": f"do {name}",
            "workspace": {"repo": str(tmp_path), "worktree": False},
            "execute": {"engine": "claude"},
            "verify": {"gate": "command", "command": "true"},
            "deliver": {},
        }, sort_keys=False))
    fleet = tmp_path / "fleet.yaml"
    fleet.write_text(yaml.safe_dump({
        "name": "solo", "concurrency": 2,
        "members": ["./api.setpoint.yaml", "./ui.setpoint.yaml"],
        "room": {"repo": str(tmp_path),
                 "tasks": [{"member": "api", "title": "API", "depends_on": []},
                           {"member": "ui", "title": "UI", "depends_on": []}]},
    }, sort_keys=False))
    return fleet


def test_review_verdict_reads_the_structured_field_first():
    from setpoint.fleet import review_verdict
    msgs = [{"kind": "review", "task_id": "t1", "from": "codex-reviewer",
             "verdict": "CHANGES", "body": "APPROVED in prose, changes in truth"}]
    assert review_verdict(msgs, "t1", "codex-reviewer") == "changes"


def test_review_verdict_falls_back_to_prose():
    from setpoint.fleet import review_verdict
    msgs = [{"kind": "review", "task_id": "t1", "from": "codex-reviewer",
             "body": "CHANGES — the DTO leaks a FIN field"},
            {"kind": "review", "task_id": "t1", "from": "codex-reviewer",
             "body": "APPROVED — fixed in 2f9a1c"}]
    assert review_verdict(msgs, "t1", "codex-reviewer") == "approved"  # last wins


def test_review_verdict_ignores_other_tasks_and_other_authors():
    from setpoint.fleet import review_verdict
    msgs = [{"kind": "review", "task_id": "t2", "from": "codex-reviewer",
             "body": "APPROVED"},
            {"kind": "review", "task_id": "t1", "from": "claude-worker",
             "body": "APPROVED (self-approval)"}]
    assert review_verdict(msgs, "t1", "codex-reviewer") == "none"


def test_gate_pass_with_changes_requested_is_not_a_fleet_success(tmp_path):
    from setpoint.fleet import run_fleet

    class ReviewingRoom(FakeRoom):
        def read(self, room_id, cursor=0, limit=50):
            base = super().read(room_id, cursor=cursor, limit=limit)
            if cursor:
                return base
            base["messages"].append(
                {"seq": 99, "kind": "review", "from": "codex-reviewer",
                 "task_id": "t1", "body": "CHANGES — missing RBAC coverage"})
            return base

    results = run_fleet(str(_write_bundle(tmp_path)),
                        run_loop=_passing_run_loop, room_client=ReviewingRoom(),
                        oneshot=lambda engine, prompt, cwd=None: "reviewed")
    assert results["api"] == "changes-requested"


def test_approved_review_is_the_fleet_success_status(tmp_path):
    from setpoint.fleet import run_fleet

    class ApprovingRoom(FakeRoom):
        def read(self, room_id, cursor=0, limit=50):
            base = super().read(room_id, cursor=cursor, limit=limit)
            if cursor:
                return base
            for tid, reviewer in (("t1", "codex-reviewer"), ("t2", "claude-reviewer")):
                base["messages"].append(
                    {"seq": 99, "kind": "review", "from": reviewer,
                     "task_id": tid, "verdict": "APPROVED", "body": "APPROVED — clean"})
            return base

    results = run_fleet(str(_write_bundle(tmp_path)),
                        run_loop=_passing_run_loop, room_client=ApprovingRoom(),
                        oneshot=lambda engine, prompt, cwd=None: "reviewed")
    assert set(results.values()) == {"review-approved"}


def test_single_engine_fleet_marks_members_unreviewed(tmp_path):
    from setpoint.fleet import run_fleet
    room = FakeRoom()
    results = run_fleet(str(_write_single_engine_bundle(tmp_path)),
                        run_loop=_passing_run_loop, room_client=room,
                        oneshot=lambda engine, prompt, cwd=None: "")
    assert set(results.values()) == {"unreviewed"}
    assert any("single-engine" in b for _, _, b in room.msgs)


def test_each_task_gets_a_named_reviewer_announced_in_room(tmp_path):
    room = FakeRoom()
    run_fleet(str(_write_bundle(tmp_path)), run_loop=_passing_run_loop,
              room_client=room, oneshot=lambda engine, prompt, cwd=None: "")
    bodies = [b for _, _, b in room.msgs]
    assert any("reviewer for API is codex-reviewer" in b for b in bodies)
    assert any("reviewer for UI is claude-reviewer" in b for b in bodies)


def test_room_context_names_the_reviewer(tmp_path):
    goals = {}

    def capture(spec, *, fresh=False, ui=None, abort_check=None, runs_root=None):
        from types import SimpleNamespace
        goals[spec.name] = spec.goal
        return SimpleNamespace(status="passed")

    run_fleet(str(_write_bundle(tmp_path)), run_loop=capture,
              room_client=FakeRoom(), oneshot=lambda e, p, cwd=None: "")
    assert "codex-reviewer" in goals["api"]
    assert "do not broadcast" in goals["api"].lower()
