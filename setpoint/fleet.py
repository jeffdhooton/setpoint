from __future__ import annotations

import concurrent.futures
import json
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

from setpoint.__main__ import _runs_root, run_loop as _default_run_loop
from setpoint.fleet_spec import load_fleet
from setpoint.ui import NullUI

ROOM_CONTEXT_TEMPLATE = """ROOM CONTEXT — you are a fleet worker.
room_id: {room_id}
task_id: {task_id}
agent: {agent}
reviewer: {reviewer}
Before writing any code, invoke your `room-worker` skill and follow it exactly:
claim your task, read the channel from cursor 0, negotiate any interface
contract before building the boundary, post status/handoff messages, request
review when your gate passes, and mark your task done or abandoned. All room
access is through your scry_* MCP tools.
Your reviewer is already assigned: {reviewer}. Address your review request to
that agent by name in your task thread — do not broadcast to the room and do
not ping other agents hoping someone picks it up.

GIT DISCIPLINE — these are hard rules, not suggestions:
- NEVER `git stash`, `git reset`, `git checkout -- .`, or any command that
  discards working-tree state. Your worktree carries work from your own earlier
  iterations; a worker on this fleet already destroyed its previous iteration's
  commit this way and only noticed by accident.
- Before you write, run `git log --oneline -5` and `git status`. If a previous
  iteration already committed part of this task, build ON it — do not restart.
- Commit specific files. No `git add -A` sweeps that hoover up another
  process's artifacts.
- Never rebase, force-push, or rewrite history. Your branch is yours alone;
  the trunk is nobody's to move."""

REVIEW_PROMPT = """You are the cross-engine reviewer for a fleet task.
Using your scry room MCP tools: read the channel thread for task {task_id}
in room {room_id} (scry_read from cursor 0, filter by task_id), then review
the work on branch {branch} of repository {repo} (git diff against the
default branch). Post your findings into the thread as messages with
kind "review" and task_id {task_id}, from "{reviewer}". End with a final
review message whose body starts with APPROVED or CHANGES followed by a
one-line justification."""


# A member's loop passing its gate is NOT the fleet's success condition: in
# the first production fleet a member was declared passed while its reviewer
# was mid-CHANGES on real findings. These are the fleet-level outcomes.
REVIEW_APPROVED = "review-approved"
CHANGES_REQUESTED = "changes-requested"
UNREVIEWED = "unreviewed"          # gate passed, no reviewer could run
GATE_PASSED = "gate-passed"        # gate passed, review outcome unknown
# `setpoint fleet run` exits 0 only for these. `unreviewed` is included
# because a single-engine fleet cannot review by construction — but it is
# always listed in "Needs a human" so it is never silently accepted. Bare
# "passed" only ever reaches here from a fleet with no `room:` section, which
# has no cross-review concept at all; in room mode every gate pass is mapped
# to one of the review-aware statuses above.
FLEET_OK = frozenset({REVIEW_APPROVED, UNREVIEWED, "completed-capped", "passed"})

_APPROVED_RE = re.compile(r"^\s*APPROVED\b", re.IGNORECASE)
_CHANGES_RE = re.compile(r"^\s*CHANGES\b", re.IGNORECASE)


def review_verdict(messages: list[dict], task_id: str, reviewer: str) -> str:
    """The reviewer's final verdict on a task: "approved", "changes", or
    "none" when that reviewer never rendered one. Prefers a structured
    `verdict` field (scry room domain) and falls back to the prose convention
    the review prompt asks for. Later messages win — a reviewer that posts
    CHANGES then APPROVED has resolved the thread."""
    verdict = "none"
    for m in messages:
        if m.get("kind") != "review" or m.get("task_id") != task_id:
            continue
        if m.get("from") != reviewer:
            continue  # never let a worker approve its own task
        structured = (m.get("verdict") or "").strip().upper()
        if structured in ("APPROVED", "CHANGES"):
            verdict = "approved" if structured == "APPROVED" else "changes"
            continue
        body = m.get("body") or ""
        if _APPROVED_RE.match(body):
            verdict = "approved"
        elif _CHANGES_RE.match(body):
            verdict = "changes"
    return verdict


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
                room_lock: threading.Lock | None = None,
                runs_root: Path | None = None) -> tuple[str, str]:
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
        block = ROOM_CONTEXT_TEMPLATE.format(
            room_id=room_ctx["room_id"], task_id=room_ctx["task_id"],
            agent=room_ctx["agent"],
            reviewer=room_ctx.get("reviewer") or "none (single-engine fleet)")
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
                          abort_check=lambda: sentinel.exists(),
                          runs_root=runs_root)
        status = getattr(state, "status", "error")
    except Exception:
        print(f"setpoint fleet: member {spec.name} failed:\n{traceback.format_exc()}",
              file=sys.stderr)
        status = "error"

    if room_ctx is not None and room is not None:
        try:
            status = _report_member_to_room(spec.name, status, room_ctx, room,
                                            oneshot, room_lock)
        except Exception:
            print(f"setpoint fleet: room reporting for {spec.name} failed:\n{traceback.format_exc()}",
                  file=sys.stderr)
            # In room mode a bare "passed" must never escape as a fleet-level
            # success: the review never resolved, so say exactly that.
            if status == "passed":
                status = GATE_PASSED

    return spec.name, status


def _report_member_to_room(member_name: str, status: str, room_ctx: dict, room, oneshot,
                           room_lock: threading.Lock | None) -> str:
    """Post the member's outcome, run cross-review when possible, and return
    the FLEET-level status — which is not the loop's status. Anything that did
    not pass its gate is returned unchanged."""
    room_id = room_ctx["room_id"]
    task_id = room_ctx["task_id"]

    def _post(kind: str, body: str) -> None:
        if room_lock is not None:
            with room_lock:
                room.post(room_id, "orchestrator", kind, body, task_id=task_id)
        else:
            room.post(room_id, "orchestrator", kind, body, task_id=task_id)

    _post("status", f"{member_name}: {status}")
    if status not in ("passed", "completed-capped"):
        # Tell the dependents. A producer whose gate never goes green never
        # posts a handoff, so every dependent waits, gives up, and rebuilds the
        # boundary itself — three of four scry members independently created
        # the same test file, four of four ops-calendar members rebuilt the
        # same module. Silence is what makes them duplicate; say it plainly.
        dependents = room_ctx.get("dependents") or []
        if dependents:
            _post("handoff",
                  f"NO HANDOFF COMING from {member_name}: it ended '{status}'. "
                  f"Blocked: {', '.join(dependents)}. Do not keep waiting, and do "
                  f"NOT rebuild its deliverable — say in your own status that you "
                  f"are blocked on it and work only what you own.")
        return status

    # The reviewer was assigned and announced at launch (_post_tasks); use
    # that assignment rather than recomputing it, so what the worker was told
    # and who actually reviews are guaranteed to be the same agent.
    reviewer = room_ctx.get("reviewer") or ""
    if not reviewer:
        _post("status", f"{member_name}: UNREVIEWED — fleet is single-engine, so no "
                        f"cross-review is possible (maker == checker). A human must "
                        f"review this member's diff.")
        return UNREVIEWED
    reviewer_engine = reviewer.rsplit("-reviewer", 1)[0]
    prompt = REVIEW_PROMPT.format(task_id=task_id, room_id=room_id,
                                  branch=room_ctx["branch"], repo=room_ctx["repo"],
                                  reviewer=reviewer)
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

    # The verdict lives in the room, not in the one-shot's stdout: the
    # reviewer posts its findings as messages, and the last one it authored on
    # this thread is the verdict that counts.
    try:
        if room_lock is not None:
            with room_lock:
                msgs = room.read(room_id, cursor=0, limit=1000).get("messages") or []
        else:
            msgs = room.read(room_id, cursor=0, limit=1000).get("messages") or []
    except Exception:
        print(f"setpoint fleet: could not read the channel for {member_name}'s "
              f"verdict:\n{traceback.format_exc()}", file=sys.stderr)
        msgs = []

    verdict = review_verdict(msgs, task_id, reviewer)
    if verdict == "approved":
        _post("status", f"{member_name}: review approved by {reviewer}")
        return REVIEW_APPROVED
    if verdict == "changes":
        _post("status", f"{member_name}: CHANGES requested by {reviewer} — "
                        f"gate passed but the review did not resolve")
        return CHANGES_REQUESTED
    _post("status", f"{member_name}: no verdict from {reviewer} — recording "
                    f"gate-passed, review unresolved")
    return GATE_PASSED


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

    # Reverse the dependency edges so a failing producer can name exactly who
    # it just blocked.
    dependents: dict[str, list[str]] = {}
    for entry in tasks:
        for dep in entry.get("depends_on") or []:
            dependents.setdefault(dep, []).append(entry["member"])

    for name, ctx in member_room_ctx.items():
        ctx["fleet_engines"] = fleet_engines
        ctx["dependents"] = dependents.get(name, [])
        # Assign the reviewer at plan time, not at review time: a worker that
        # has to find its own reviewer broadcasts and waits.
        reviewer_engine = next((e for e in fleet_engines if e != ctx["engine"]), None)
        ctx["reviewer"] = f"{reviewer_engine}-reviewer" if reviewer_engine else ""

    for i in order:
        entry = tasks[i]
        ctx = member_room_ctx[entry["member"]]
        if ctx["reviewer"]:
            room.post(room_id, "orchestrator", "status",
                      f"reviewer for {entry['title']} is {ctx['reviewer']} — "
                      f"{ctx['agent']} requests review in this thread when its gate "
                      f"passes; no other agent should pick it up",
                      task_id=ctx["task_id"])
        else:
            room.post(room_id, "orchestrator", "status",
                      f"{entry['title']} has no reviewer — this fleet is "
                      f"single-engine, so its member will be reported unreviewed",
                      task_id=ctx["task_id"])

    return member_room_ctx


def _write_room_report(fs, room, room_id: str, outcome=None) -> str:
    runs_root = _runs_root()
    lines = _status_lines(fs, runs_root)
    if outcome:
        lines += ["", "## Outcome", ""]
        lines += [f"- {m}: {st}" for m, st in outcome["results"].items()]
        if outcome["prs"]:
            lines += ["", "### Deliverables", ""] + [f"- {u}" for u in outcome["prs"]]
        lines += ["", "### Needs a human", ""] + [f"- {n}" for n in outcome["needs_human"]]
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

    member_runs_root = fleet_runs_root(fs, _runs_root())

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
            # Establish whether the base is already red BEFORE any member runs.
            # program-health's develop was failing CI when a fleet launched onto
            # it; every member inherited the failure and nothing said so, which
            # makes "this worker broke it" indistinguishable from "it arrived
            # broken".
            baseline = _baseline_gate(fs)
            if baseline is not None and not baseline["passed"]:
                print(f"setpoint fleet: WARNING — the repo-wide gate is ALREADY RED on "
                      f"the base branch before any member has run:\n"
                      f"  {baseline['command']}\n"
                      f"Members cannot be judged against it. Fix the base first, or "
                      f"read every result as 'red for reasons that predate this fleet'.",
                      file=sys.stderr)

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
                                       room=room, oneshot=oneshot, room_lock=room_lock,
                                       runs_root=member_runs_root)
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
                outcome = None
                try:
                    outcome = _close_the_loop(fs, room, room_id, results)
                except Exception:
                    print(f"setpoint fleet: closing ceremony failed:\n"
                          f"{traceback.format_exc()}", file=sys.stderr)
                try:
                    _write_room_report(fs, room, room_id, outcome=outcome)
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



_TERMINAL_TASK_STATUSES = {"done", "abandoned"}
_PR_RE = re.compile(r"https://github\.com/\S+/pull/\d+")


def _close_the_loop(fs, room, room_id, results):
    """The closing ceremony. A fleet is not over when its processes exit; it
    is over when the outcome is declared and every residual is assigned.
    Reconciles non-terminal board tasks, posts the FLEET CLOSED message as
    the room's final word, and returns the outcome for the report.

    Reconciliation policy: a passed member's lingering task is finalized
    done (its loop's gate passed); anything else is marked abandoned, which
    clears the claim so a future wave can pick it up.
    """
    tasks = room.list_tasks(room_id)
    msgs = room.read(room_id, cursor=0, limit=1000).get("messages") or []
    prs = sorted({m for msg in msgs for m in _PR_RE.findall(msg.get("body", ""))})

    reconciled = []
    for t in tasks:
        if t.get("status") in _TERMINAL_TASK_STATUSES:
            continue
        member = next((m for m in results
                       if (t.get("claimed_by") or "").endswith(m)), None)
        final = "done" if results.get(member) in FLEET_OK else "abandoned"
        try:
            room.post(room_id, "orchestrator", "status",
                      f"reconciling board: task '{t.get('title', t['id'])}' left "
                      f"'{t.get('status')}' by {t.get('claimed_by') or 'nobody'} — "
                      f"marking {final} (member outcome: {results.get(member, 'unknown')})",
                      task_id=t["id"])
            room.update_task_status(room_id, t["id"], final)
        except Exception as e:
            print(f"setpoint fleet: board reconcile failed for {t['id']}: {e}",
                  file=sys.stderr)
        reconciled.append((t.get("title", t["id"]), t.get("status"), final))

    # A member that committed nothing is a different failure from one that
    # built 3,000 lines and tripped its gate; both used to report "stopped".
    repo = Path(fs.room["repo"]) if fs.room else None
    no_work = []
    if repo is not None:
        for member in sorted(results):
            if branch_commit_count(repo, f"setpoint/{member}", "HEAD") == 0:
                no_work.append(member)

    needs_human = _needs_human_lines(results, prs, reconciled, no_work)

    body = ("FLEET CLOSED — " + fs.name + "\n\n"
            + "Member outcomes:\n"
            + "\n".join(f"- {m}: {st}" for m, st in sorted(results.items())) + "\n\n"
            + ("Deliverables:\n" + "\n".join(f"- {u}" for u in prs) + "\n\n" if prs else "")
            + "Needs a human:\n" + "\n".join(f"- {n}" for n in needs_human))
    try:
        room.post(room_id, "orchestrator", "status", body)
    except Exception as e:
        print(f"setpoint fleet: failed to post closing message: {e}", file=sys.stderr)
    return {"prs": prs, "needs_human": needs_human, "reconciled": reconciled,
            "results": dict(sorted(results.items()))}


def fleet_runs_root(fs, runs_root: Path) -> Path:
    """Where this fleet's member run state lives. Run state used to be global
    per spec name, so a second wave reusing a member spec overwrote the first
    wave's state (the viewer then showed a finished fleet as 3/4).
    Namespacing by fleet makes each wave's record its own."""
    return _fleet_out_dir(fs, runs_root) / "runs"


def _fleet_out_dir(fs, runs_root: Path) -> Path:
    """Where fleet-level artifacts (status.md, report.md) live: beside
    "runs", not inside it -- runs_root is ~/.setpoint/runs by default, so
    this resolves to ~/.setpoint/fleets/<name>."""
    return runs_root.parent / "fleets" / fs.name


def _fmt_elapsed(secs: float) -> str:
    if not secs:
        return "—"
    m, s = divmod(int(secs), 60)
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def _member_engine(member_path: Path) -> str:
    from setpoint.spec import load_spec
    try:
        return load_spec(str(member_path)).execute.engine
    except Exception:
        return ""


def _status_lines(fs, runs_root: Path) -> list[str]:
    lines = [f"# fleet {fs.name}", "",
             f"{'member':30} {'status':20} {'iters':>6} {'elapsed':>9} {'spend':>9}"]
    member_runs = fleet_runs_root(fs, runs_root)
    for member in fs.members:
        name = _run_name(member)
        # Only the deepseek engine's spend flows through this process's
        # budget; claude/codex/kimi bill through their own CLIs, so a "$0.00"
        # there is a lie, not a measurement.
        billable = _member_engine(member) == "deepseek"
        sp = member_runs / name / "state.json"
        if sp.exists():
            s = json.loads(sp.read_text())
            spend = f"${s.get('spent_usd', 0):.2f}" if billable else "—"
            lines.append(f"{name:30} {s.get('status','?'):20} "
                         f"{len(s.get('iters', [])):>6} "
                         f"{_fmt_elapsed(s.get('elapsed_secs', 0)):>9} {spend:>9}")
        else:
            lines.append(f"{name:30} {'pending':20} {0:>6} {'—':>9} {'—':>9}")
    return lines


def _baseline_gate(fs) -> dict | None:
    """Run the members' shared broad gate once against the base checkout, so
    the fleet knows whether it is starting from green. Returns None when the
    members do not agree on one broad command (nothing meaningful to check).
    Best-effort: any failure to even run it yields None rather than blocking
    the fleet."""
    from setpoint.spec import load_spec

    try:
        commands = set()
        for m in fs.members:
            spec = load_spec(str(m))
            if spec.verify.gate == "command" and spec.verify.command:
                commands.add(spec.verify.command)
        if len(commands) != 1:
            return None
        command = commands.pop()
        proc = subprocess.run(command, shell=True, cwd=fs.room["repo"],
                              capture_output=True, text=True, timeout=900)
        return {"command": command, "passed": proc.returncode == 0}
    except Exception:
        return None


def branch_commit_count(repo: Path, branch: str, base: str) -> int | None:
    """Commits on `branch` that are not on `base`, or None if the branch does
    not exist. A member that committed nothing and one carrying thousands of
    lines both report 'stopped'; this is what tells them apart."""
    proc = subprocess.run(
        ["git", "rev-list", "--count", f"{base}..{branch}"],
        cwd=repo, capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    try:
        return int((proc.stdout or "").strip())
    except ValueError:
        return None


def _needs_human_lines(results: dict, prs: list, reconciled: list,
                       no_work: list) -> list[str]:
    """The "needs a human" list, grouped by reason.

    Ungrouped, this emitted one near-identical line per member — five copies
    of "abandoned task needs a decision or a next wave" and four of "ended
    'stopped' — read its run log" — which buries the one line that differs.
    One line per reason, members named on it.
    """
    lines: list[str] = []
    if prs:
        lines.append(f"review and merge: {', '.join(prs)}")

    abandoned = [title for title, _was, final in reconciled if final == "abandoned"]
    if abandoned:
        lines.append(f"{len(abandoned)} abandoned task(s) need a decision or a next "
                     f"wave: {', '.join(abandoned)}")
    if no_work:
        lines.append(f"{len(no_work)} member(s) committed NOTHING — they burned their "
                     f"iterations without producing work: {', '.join(sorted(no_work))}")

    # Group members by the reason they need attention.
    by_reason: dict[str, list[str]] = {}
    for member, st in sorted(results.items()):
        if st == UNREVIEWED:
            reason = "passed their gate but were never cross-reviewed — review the diffs"
        elif st == CHANGES_REQUESTED:
            reason = "have unresolved review findings — read the review thread before merging"
        elif st == GATE_PASSED:
            reason = "passed their gate but no reviewer rendered a verdict"
        elif st == "completed-capped":
            reason = "verified their own deliverable but the repo-wide gate is red — confirm it is pre-existing"
        elif st not in FLEET_OK:
            reason = f"ended '{st}' — read the run log and transcript before trusting the work"
        else:
            continue
        by_reason.setdefault(reason, []).append(member)

    for reason, members in by_reason.items():
        lines.append(f"member(s) {', '.join(members)} {reason}")

    if not lines:
        lines.append("nothing — every member was reviewed, approved and delivered")
    return lines


def _fleets_dir(runs_root: Path) -> Path:
    """The directory holding every fleet's artifacts: ~/.setpoint/fleets."""
    return runs_root.parent / "fleets"


def list_fleets(runs_root: Path) -> list[dict]:
    """Every fleet on disk, newest first: name, path, age in days, and the
    member run counts. Fleet artifacts accumulate indefinitely — this is what
    `fleet ls`, `fleet rm`, and `fleet prune` all read."""
    root = _fleets_dir(runs_root)
    if not root.is_dir():
        return []
    now = time.time()
    out = []
    for d in root.iterdir():
        if not d.is_dir():
            continue
        # The newest artifact is the fleet's real age: the directory's own
        # mtime moves whenever anything is written beneath it, but a
        # long-finished fleet's files stay put.
        stamps = [p.stat().st_mtime for p in d.rglob("*") if p.is_file()]
        mtime = max(stamps or [d.stat().st_mtime])
        runs = d / "runs"
        members = sorted(p.name for p in runs.iterdir()) if runs.is_dir() else []
        out.append({"name": d.name, "path": d, "age_days": (now - mtime) / 86400,
                    "mtime": mtime, "members": members})
    out.sort(key=lambda f: f["mtime"], reverse=True)
    return out


def remove_fleet(name: str, runs_root: Path) -> bool:
    """Delete one fleet's artifacts and member run state. Returns False when
    there is no such fleet. Raises ValueError for a name that is not a plain
    directory name — this function deletes trees, so the name must never be
    able to escape the fleets directory."""
    if not name or "/" in name or "\\" in name or name in (".", "..") or Path(name).is_absolute():
        raise ValueError(f"refusing to remove: {name!r} is not a plain fleet name")
    target = _fleets_dir(runs_root) / name
    if not target.is_dir():
        return False
    # Belt and braces: confirm the resolved path really sits inside the
    # fleets directory before rmtree touches anything.
    fleets = _fleets_dir(runs_root).resolve()
    if not target.resolve().is_relative_to(fleets):
        raise ValueError(f"refusing to remove: {name!r} resolves outside {fleets}")
    shutil.rmtree(target)
    return True


def prune_fleets(runs_root: Path, older_than_days: float = 30,
                 confirm: bool = False) -> list[dict]:
    """Fleets older than `older_than_days`. Returns the list either way;
    only deletes when `confirm` is True, so the default call is a dry run —
    this removes run history, so the safe mode is the one you get for free."""
    stale = [f for f in list_fleets(runs_root) if f["age_days"] >= older_than_days]
    if confirm:
        for f in stale:
            remove_fleet(f["name"], runs_root)
    return stale


def fleet_status(fleet_path: str) -> str:
    fs = load_fleet(fleet_path)
    runs_root = _runs_root()
    text = "\n".join(_status_lines(fs, runs_root)) + "\n"
    out_dir = _fleet_out_dir(fs, runs_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "status.md").write_text(text)
    return text
