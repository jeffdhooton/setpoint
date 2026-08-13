from __future__ import annotations

import concurrent.futures
import json
import sys
import threading
import traceback
from pathlib import Path

from setpoint.__main__ import _runs_root, run_loop as _default_run_loop
from setpoint.fleet_spec import load_fleet
from setpoint.ui import NullUI

ROOM_CONTEXT_TEMPLATE = """ROOM CONTEXT — you are a fleet worker.
room_id: {room_id}
task_id: {task_id}
agent: {agent}
Before writing any code, invoke your `room-worker` skill and follow it exactly:
claim your task, read the channel from cursor 0, negotiate any interface
contract before building the boundary, post status/handoff messages, request
review when your gate passes, and mark your task done or abandoned. All room
access is through your scry_* MCP tools."""

REVIEW_PROMPT = """You are the cross-engine reviewer for a fleet task.
Using your scry room MCP tools: read the channel thread for task {task_id}
in room {room_id} (scry_read from cursor 0, filter by task_id), then review
the work on branch {branch} of repository {repo} (git diff against the
default branch). Post your findings into the thread as messages with
kind "review" and task_id {task_id}, from "{reviewer}". End with a final
review message whose body starts with APPROVED or CHANGES followed by a
one-line justification."""


def stop_sentinel_path() -> Path:
    return _runs_root().parent / "STOP"


def _member_name(member_path: Path) -> str:
    # Fallback when the spec can't be loaded: member paths look like
    # ".../<name>.setpoint.yaml". Accept the legacy ".loom" suffix too, so an
    # un-migrated fleet still resolves its run keys. removesuffix, not replace:
    # a run named "deploy.loomtest" must not be mangled to "deploytest".
    # DEPRECATED: drop the ".loom" entry in the same release that removes the
    # ".loom.yaml" branch in spec.py:load_spec — they are one compat surface.
    stem = member_path.stem
    for suffix in (".setpoint", ".loom"):
        if stem.endswith(suffix):
            return stem.removesuffix(suffix)
    return stem


def _run_name(member_path: Path) -> str:
    """Resolve the run-lookup key for a member: the spec's declared `name:`
    field (that's what `Memory` keys `~/.setpoint/runs/<name>/` by), falling
    back to the filename stem if the spec can't be loaded."""
    from setpoint.spec import load_spec

    try:
        return load_spec(str(member_path)).name
    except Exception:
        return _member_name(member_path)


def _run_member(member_path: Path, fresh: bool, run_loop, *,
                room_ctx: dict | None = None, room=None, oneshot=None,
                room_lock: threading.Lock | None = None) -> tuple[str, str]:
    from setpoint.spec import load_spec

    sentinel = stop_sentinel_path()
    try:
        spec = load_spec(str(member_path))
    except Exception:
        print(f"setpoint fleet: member {member_path.name} failed to load:\n{traceback.format_exc()}",
              file=sys.stderr)
        return _member_name(member_path), "error"

    if room_ctx is not None:
        # context.notes is typed as str in spec.py, but a fleet worker needs
        # its own room-context block kept distinct from any author-supplied
        # notes -- normalize to a list (preserving any existing note as its
        # first element) rather than string-concatenating blindly.
        block = ROOM_CONTEXT_TEMPLATE.format(room_id=room_ctx["room_id"],
                                             task_id=room_ctx["task_id"],
                                             agent=room_ctx["agent"])
        existing = spec.context.notes
        notes = list(existing) if isinstance(existing, list) else ([existing] if existing else [])
        notes.append(block)
        spec.context.notes = notes

    try:
        state = run_loop(spec, fresh=fresh, ui=NullUI(),
                          abort_check=lambda: sentinel.exists())
        status = getattr(state, "status", "error")
    except Exception:
        print(f"setpoint fleet: member {spec.name} failed:\n{traceback.format_exc()}",
              file=sys.stderr)
        status = "error"

    if room_ctx is not None and room is not None:
        try:
            _report_member_to_room(spec.name, status, room_ctx, room, oneshot, room_lock)
        except Exception:
            print(f"setpoint fleet: room reporting for {spec.name} failed:\n{traceback.format_exc()}",
                  file=sys.stderr)

    return spec.name, status


def _report_member_to_room(member_name: str, status: str, room_ctx: dict, room, oneshot,
                           room_lock: threading.Lock | None) -> None:
    room_id = room_ctx["room_id"]
    task_id = room_ctx["task_id"]

    def _post(kind: str, body: str) -> None:
        if room_lock is not None:
            with room_lock:
                room.post(room_id, "orchestrator", kind, body, task_id=task_id)
        else:
            room.post(room_id, "orchestrator", kind, body, task_id=task_id)

    _post("status", f"{member_name}: {status}")
    if status != "passed":
        return

    engine = room_ctx["engine"]
    reviewer_engine = next((e for e in room_ctx.get("fleet_engines", []) if e != engine), None)
    if reviewer_engine is None:
        _post("status", f"{member_name}: skipping cross-review — fleet is single-engine")
        return

    prompt = REVIEW_PROMPT.format(task_id=task_id, room_id=room_id,
                                  branch=room_ctx["branch"], repo=room_ctx["repo"],
                                  reviewer=f"{reviewer_engine}-reviewer")
    oneshot(reviewer_engine, prompt)


def _setup_room(fs, room) -> tuple[str, dict[str, dict]]:
    """Create the room and post every `room.tasks` entry in declared order,
    resolving `depends_on` member-names to the room task ids of already-posted
    entries. Returns the room id and a per-member context dict (room_id,
    task_id, engine, agent, branch, repo, fleet_engines) used later to inject
    the ROOM CONTEXT block and to pick a cross-review engine."""
    from setpoint.spec import load_spec

    name_to_path = {_run_name(m): m for m in fs.members}
    room_info = room.create_room(run_id=fs.name, repo=fs.room["repo"])
    room_id = room_info["id"]

    name_to_task_id: dict[str, str] = {}
    member_room_ctx: dict[str, dict] = {}
    fleet_engines: list[str] = []
    tasks = fs.room.get("tasks") or []
    for entry in tasks:
        member = entry["member"]
        member_path = name_to_path.get(member)
        try:
            engine = load_spec(str(member_path)).execute.engine if member_path else ""
        except Exception:
            engine = ""
        if engine and engine not in fleet_engines:
            fleet_engines.append(engine)

        deps = [name_to_task_id[d] for d in (entry.get("depends_on") or [])
               if d in name_to_task_id]
        task = room.post_task(room_id, entry["title"], body=entry.get("body", ""),
                              depends_on=deps, interfaces=entry.get("interfaces", ""))
        task_id = task["id"]
        name_to_task_id[member] = task_id
        member_room_ctx[member] = {
            "room_id": room_id,
            "task_id": task_id,
            "engine": engine,
            "agent": f"{engine}-{member}",
            "branch": f"setpoint/{member}",
            "repo": fs.room["repo"],
        }

    room.post(room_id, "orchestrator", "status",
             f"fleet {fs.name} launched: {len(tasks)} tasks")

    for ctx in member_room_ctx.values():
        ctx["fleet_engines"] = fleet_engines

    return room_id, member_room_ctx


def _write_room_report(fs, room, room_id: str) -> str:
    runs_root = _runs_root()
    lines = _status_lines(fs, runs_root)
    lines += ["", "## Room transcript", ""]
    cursor = 0
    while True:
        resp = room.read(room_id, cursor=cursor, limit=1000)
        msgs = resp.get("messages") or []
        if not msgs:
            break
        for m in msgs:
            lines.append(f"- [{m.get('kind')}] {m.get('from')} "
                         f"(task {m.get('task_id')}): {m.get('body')}")
        cursor = resp.get("cursor", cursor)
    text = "\n".join(lines) + "\n"
    # Unlike status.md (which lives beside "runs" under runs_root.parent),
    # the room report is keyed off runs_root itself -- see
    # tests/test_fleet_room.py, which sets SETPOINT_RUNS_ROOT to <tmp>/runs
    # and expects the report at <tmp>/runs/fleets/<name>/report.md.
    out_dir = runs_root / "fleets" / fs.name
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text(text)
    return text


def run_fleet(fleet_path: str, *, fresh: bool = False, run_loop=None,
              room_client=None, oneshot=None) -> dict[str, str]:
    """Run every member of a fleet spec in a bounded thread pool.

    Each member is fully isolated (its own worktree via `prepare_workspace`),
    so threads are safe here even though agent turns are subprocesses.

    STOP semantics: a stale sentinel from a previous run is cleared at the
    start of every `run_fleet` call. If a member's run_loop (or an external
    actor) re-creates the sentinel while the fleet is in flight, in-progress
    members keep running until their own `abort_check` trips (they exit at
    the next iteration boundary, bounded by max_iters/wall_clock_secs) but
    no *new* member is submitted -- it is recorded as "skipped" instead.

    Room mode: when the fleet spec has a `room:` section, members are
    coordinated through a scry fleet room -- a room is created and each
    `room.tasks` entry posted before any member is submitted, each member's
    spec gets a ROOM CONTEXT block appended to `context.notes`, and after a
    member's run_loop passes a cross-engine review one-shot is dispatched
    from inside its worker thread (so review runs in parallel with the rest
    of the fleet). `room_client`/`oneshot` are injection seams for tests; in
    real use they default to a real `RoomClient` / engine one-shot, built
    only when the fleet actually has a `room:` section. When there is no
    `room:` section, both kwargs are ignored and this function behaves
    exactly as it always has.
    """
    run_loop = run_loop or _default_run_loop
    fs = load_fleet(fleet_path)

    # Each member's run state lives at ~/.setpoint/runs/<run-name>/. If two
    # members resolve to the same run name, they'd race the same
    # state.json/log.md (and --fresh could rmtree one mid-run) -- fail fast
    # before submitting any work.
    run_names = [_run_name(member) for member in fs.members]
    seen: set[str] = set()
    dups: set[str] = set()
    for n in run_names:
        if n in seen:
            dups.add(n)
        seen.add(n)
    if dups:
        raise ValueError(f"fleet has duplicate member names: {sorted(dups)}")

    sentinel = stop_sentinel_path()
    sentinel.unlink(missing_ok=True)  # clear a stale sentinel so a fresh fleet is not blocked

    room = None
    room_id: str | None = None
    member_room_ctx: dict[str, dict] = {}
    room_lock: threading.Lock | None = None

    if fs.room:
        if room_client is None:
            from setpoint.room import RoomClient
            room_client = RoomClient()
        if oneshot is None:
            from setpoint.decompose import _default_oneshot
            oneshot = _default_oneshot
        room = room_client
        room_lock = threading.Lock()

    results: dict[str, str] = {}
    skipped = 0

    try:
        # Room setup (create_room + post_task per room.tasks entry) runs
        # inside the try too: if it fails partway -- e.g. create_room
        # succeeds but a later post_task raises -- the finally below must
        # still see a room to tear down instead of leaking the subprocess.
        if room is not None:
            room_id, member_room_ctx = _setup_room(fs, room)

        # ThreadPoolExecutor.submit() enqueues work immediately regardless of
        # worker availability -- it does not block until a slot is actually
        # free. If members were submitted in a tight, unthrottled loop, the
        # STOP check ahead of member N+1 would race against member N's
        # *execution* (thread start + run_loop) rather than reflecting
        # completed work, which would make the "skip unstarted members"
        # behavior nondeterministic. A semaphore sized to `concurrency` gives
        # real backpressure: the next member is only considered for
        # submission once an in-flight slot has genuinely been released by a
        # completed run_loop call, so the STOP check is meaningful.
        sem = threading.Semaphore(fs.concurrency)

        with concurrent.futures.ThreadPoolExecutor(max_workers=fs.concurrency) as ex:
            futures: list[concurrent.futures.Future] = []

            def wrapped(member_path: Path) -> tuple[str, str]:
                try:
                    return _run_member(member_path, fresh, run_loop,
                                       room_ctx=member_room_ctx.get(_run_name(member_path)),
                                       room=room, oneshot=oneshot, room_lock=room_lock)
                finally:
                    sem.release()

            for member in fs.members:
                sem.acquire()
                if sentinel.exists():
                    sem.release()
                    results[_run_name(member)] = "skipped"
                    skipped += 1
                    continue
                futures.append(ex.submit(wrapped, member))

            for fut in concurrent.futures.as_completed(futures):
                name, status = fut.result()
                results[name] = status

        if skipped:
            print(f"setpoint fleet: STOP sentinel detected — skipped {skipped} unstarted member(s)")

        return results
    finally:
        # Room teardown runs in `finally` so a member crash still closes the
        # room and writes the report with whatever transcript exists. Guard
        # on room_id specifically (not just `room`) because setup itself can
        # fail before create_room ever returns one.
        if room is not None:
            if room_id is not None:
                _write_room_report(fs, room, room_id)
                room.close_room(room_id)
            room.close()


def _status_lines(fs, runs_root: Path) -> list[str]:
    lines = [f"# fleet {fs.name}", "", f"{'member':30} {'status':16} {'iters':>6} {'spend':>8}"]
    for member in fs.members:
        name = _run_name(member)
        sp = runs_root / name / "state.json"
        if sp.exists():
            s = json.loads(sp.read_text())
            lines.append(f"{name:30} {s.get('status','?'):16} "
                         f"{len(s.get('iters', [])):>6} ${s.get('spent_usd', 0):>7.2f}")
        else:
            lines.append(f"{name:30} {'pending':16} {0:>6} ${0:>7.2f}")
    return lines


def fleet_status(fleet_path: str) -> str:
    fs = load_fleet(fleet_path)
    runs_root = _runs_root()
    text = "\n".join(_status_lines(fs, runs_root)) + "\n"
    out_dir = runs_root.parent / "fleets" / fs.name
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "status.md").write_text(text)
    return text
