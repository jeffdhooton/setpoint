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
        # context.notes is a plain str (spec.py:26), and Cycle._discover
        # joins it as a scalar ("\n\n".join([spec.context.notes])) --
        # cycle.py:89. A list here would blow up every real member's first
        # DISCOVER with a TypeError. Concatenate, don't wrap.
        block = ROOM_CONTEXT_TEMPLATE.format(room_id=room_ctx["room_id"],
                                             task_id=room_ctx["task_id"],
                                             agent=room_ctx["agent"])
        spec.context.notes = (
            (spec.context.notes + "\n\n" if spec.context.notes else "") + block)
        # notes only feeds Cycle._discover, which agent engines (claude/codex/
        # kimi) never actually consult: their plan client is the no-op
        # AgentPlanClient (agent_plan.py) returning fixed _PLAN_TEXT, so the
        # executor prompt is built from spec.goal alone (cycle.py). Append the
        # room block to goal too, or agent-engine workers never see it.
        spec.goal = spec.goal + "\n\n" + block

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
    # cwd=repo: the review targets that repo (git diff, reading the branch),
    # and codex's sandbox / claude's trust context are scoped to the process
    # cwd -- running the one-shot from the orchestrator's own cwd would point
    # the reviewer at the wrong (or no) repo.
    try:
        result_text = oneshot(reviewer_engine, prompt, cwd=room_ctx["repo"])
    except Exception:
        result_text = ""
        print(f"setpoint fleet: review one-shot for {member_name} by "
              f"{reviewer_engine} failed:\n{traceback.format_exc()}", file=sys.stderr)
    if not (result_text or "").strip():
        _post("status", f"review of {member_name} by {reviewer_engine} "
                        f"produced no output/failed")


def _order_tasks_by_dependency(tasks: list[dict]) -> list[int]:
    """Return task indices in an order where every entry's depends_on
    members have already appeared -- i.e. a topological order, not
    necessarily file order (a dependent entry may be declared before the
    member it depends on). Raises ValueError on an unknown depends_on target
    or a dependency cycle."""
    member_names = {entry["member"] for entry in tasks}
    for i, entry in enumerate(tasks):
        for dep in entry.get("depends_on") or []:
            if dep not in member_names:
                raise ValueError(
                    f"room.tasks[{i}] ({entry['member']!r}) depends_on unknown "
                    f"member {dep!r}")

    posted_idx: set[int] = set()
    posted_members: set[str] = set()
    order: list[int] = []
    while len(posted_idx) < len(tasks):
        progressed = False
        for i, entry in enumerate(tasks):
            if i in posted_idx:
                continue
            deps = entry.get("depends_on") or []
            if all(d in posted_members for d in deps):
                order.append(i)
                posted_idx.add(i)
                posted_members.add(entry["member"])
                progressed = True
        if not progressed:
            missing = [tasks[i]["member"] for i in range(len(tasks)) if i not in posted_idx]
            raise ValueError(f"dependency cycle in room.tasks: {missing}")
    return order


def _post_tasks(fs, room, room_id: str) -> dict[str, dict]:
    """Post every `room.tasks` entry, resolving `depends_on` member-names to
    the room task ids of already-posted entries (posting in
    dependency-satisfying order, so a dependent entry may be declared before
    the member it depends on). Returns a per-member context dict (room_id,
    task_id, engine, agent, branch, repo, fleet_engines) used later to inject
    the ROOM CONTEXT block and to pick a cross-review engine.

    Raises ValueError for a malformed entry (missing member/title), an
    unknown depends_on target, or a dependency cycle."""
    from setpoint.spec import load_spec

    name_to_path = {_run_name(m): m for m in fs.members}
    tasks = fs.room.get("tasks") or []
    for i, entry in enumerate(tasks):
        if not entry.get("member") or not entry.get("title"):
            raise ValueError(f"room.tasks[{i}] missing required 'member' or 'title'")

    # room.tasks and fleet members must be the same set in both directions --
    # a task naming a member that isn't in the fleet can never be worked, and
    # a fleet member with no room.tasks entry would run outside room
    # coordination while its siblings believe every member is tracked on the
    # board (ROOM CONTEXT injection, cross-review dispatch, and the "fleet
    # launched: N tasks" post all key off room.tasks).
    task_members = {entry["member"] for entry in tasks}
    unknown = task_members - set(name_to_path)
    if unknown:
        raise ValueError(f"room.tasks references unknown fleet member(s): {sorted(unknown)}")
    untracked = set(name_to_path) - task_members
    if untracked:
        raise ValueError(f"fleet member(s) have no room.tasks entry: {sorted(untracked)}")

    order = _order_tasks_by_dependency(tasks)

    name_to_task_id: dict[str, str] = {}
    member_room_ctx: dict[str, dict] = {}
    fleet_engines: list[str] = []
    for i in order:
        entry = tasks[i]
        member = entry["member"]
        member_path = name_to_path.get(member)
        try:
            engine = load_spec(str(member_path)).execute.engine if member_path else ""
        except Exception:
            engine = ""
        if engine and engine not in fleet_engines:
            fleet_engines.append(engine)

        deps = [name_to_task_id[d] for d in (entry.get("depends_on") or [])]
        # decompose() puts the task's goal on the "goal" field; fall back to
        # a plain "body" for hand-authored fleet.yaml files that use it
        # directly. Missing/empty is fine -- post_task treats "" as no body.
        body = entry.get("goal") or entry.get("body") or ""
        task = room.post_task(room_id, entry["title"], body=body,
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

    return member_room_ctx


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
        new_cursor = resp.get("cursor", cursor)
        if new_cursor == cursor:
            # A buggy/misbehaving server that returns messages but never
            # advances the cursor would otherwise loop here forever.
            break
        cursor = new_cursor
    text = "\n".join(lines) + "\n"
    out_dir = _fleet_out_dir(fs, runs_root)
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
        # Room setup runs inside the try too: create_room is called first so
        # room_id is assigned as early as possible, then _post_tasks does the
        # rest. If _post_tasks fails partway (bad room.tasks entry, a
        # dependency cycle, or a post_task RPC error), the finally below
        # still sees a non-None room_id and tears the room down instead of
        # leaking the subprocess.
        if room is not None:
            room_info = room.create_room(run_id=fs.name, repo=fs.room["repo"])
            room_id = room_info["id"]
            member_room_ctx = _post_tasks(fs, room, room_id)
            # Self-describing fleet: write the room manifest next to
            # status.md/report.md so external tools (viewers, wave restarts)
            # can find the room without a room.get lookup.
            manifest = json.dumps({
                "room_id": room_id, "run_id": fs.name,
                "repo": fs.room["repo"],
                "members": {name: ctx["agent"] for name, ctx in member_room_ctx.items()},
            }, indent=2) + "\n"
            out_dir = _fleet_out_dir(fs, _runs_root())
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "room.json").write_text(manifest)
            # Bundle-local copy: the fleet dir is the project-side record of
            # its own runs, so the manifest lives there too.
            (Path(fleet_path).resolve().parent / "room.json").write_text(manifest)

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
                    name = _run_name(member)
                    results[name] = "skipped"
                    skipped += 1
                    if room is not None and room_id is not None:
                        try:
                            task_id = member_room_ctx.get(name, {}).get("task_id", "")
                            body = f"{name}: skipped (STOP sentinel)"
                            if room_lock is not None:
                                with room_lock:
                                    room.post(room_id, "orchestrator", "status", body,
                                             task_id=task_id)
                            else:
                                room.post(room_id, "orchestrator", "status", body,
                                         task_id=task_id)
                        except Exception:
                            print(f"setpoint fleet: failed to post skip notice for "
                                  f"{name} to room:\n{traceback.format_exc()}", file=sys.stderr)
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
                try:
                    _write_room_report(fs, room, room_id)
                except Exception:
                    print(f"setpoint fleet: failed to write room report:\n"
                          f"{traceback.format_exc()}", file=sys.stderr)
                try:
                    room.close_room(room_id)
                except Exception:
                    print(f"setpoint fleet: failed to close room {room_id}:\n"
                          f"{traceback.format_exc()}", file=sys.stderr)
            # Unconditional last step regardless of what happened above --
            # otherwise a report-write or close_room failure would leak the
            # scry mcp subprocess.
            room.close()


def _fleet_out_dir(fs, runs_root: Path) -> Path:
    """Where fleet-level artifacts (status.md, report.md) live: beside
    "runs", not inside it -- runs_root is ~/.setpoint/runs by default, so
    this resolves to ~/.setpoint/fleets/<name>."""
    return runs_root.parent / "fleets" / fs.name


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
    out_dir = _fleet_out_dir(fs, runs_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "status.md").write_text(text)
    return text
