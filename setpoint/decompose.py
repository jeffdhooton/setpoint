"""Turn an idea file into a fleet bundle: plan.md, tasks.json, per-task
member specs, and a fleet.yaml with a room section.

Gate 1 of the fleet design is human review of the generated bundle — nothing
here launches anything.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

DECOMPOSE_PROMPT = """You are decomposing a software idea into 2-6 parallelizable \
engineering tasks for a fleet of coding agents working in one repository.

Rules:
- Tasks must be independently implementable in separate git worktrees; put a
  dependency edge (depends_on) only where one task consumes another's output.
- For EVERY depends_on entry, add a matching "depends_on_reason" map naming what
  this task actually reads from that producer (e.g. {{"data-layer": "calls
  getScheduleWeek()"}}). If you cannot say what is read, it is NOT an edge —
  delete it and let the two tasks run at the same time. False edges are the
  most expensive mistake here: they make the fleet serial for nothing.
- Keep the longest dependency chain SHORT. The fleet can never finish faster
  than its longest chain, so four independent tasks beat a four-deep chain
  every time.
- Where two tasks share a boundary (API/schema/function), describe it in the
  producing task's "interfaces" field concretely enough to negotiate from.
- Every task needs a deterministic verify_command that exits 0 on success,
  runnable from the repo root. Make it the NARROWEST command that proves THIS
  task's deliverable. Do NOT include the repo's full test/build/lint command:
  that is run separately as a second, broader gate, and duplicating it here
  makes every iteration pay for the whole suite.
- CRITICAL: the verify_command must NOT name a test file that does not exist
  yet. The implementer decides where its tests belong, and a repo with several
  test projects (unit vs integration vs worker/binding suites) has more than
  one correct home — a gate pointing at the wrong one can never go green no
  matter how good the work is, and the whole iteration budget burns on it.
  Name an EXISTING runnable target instead: a package/workspace test script, a
  test project, or an existing directory. Prefer the smallest such target that
  covers this task's area.
- Assign each task an engine from this list, spreading work across them: {engines}
- Task names are kebab-case slugs, unique.

Repository: {repo}
{survey_block}
Idea:
{idea}

Respond with ONLY a JSON object (fenced or bare) of the shape:
{{"tasks": [{{"name", "title", "goal", "interfaces", "depends_on", "depends_on_reason", "verify_command", "engine"}}]}}
"""


class NothingLeftToBuild(Exception):
    """The survey found the proposed work already exists, so there is nothing
    to plan. This is a SUCCESS: catching a stale premise before a fleet runs
    is the entire reason the survey exists."""


def _default_oneshot(engine: str, prompt: str, cwd: str | None = None) -> str:
    from setpoint.executor.agent_cli import (_claude_argv, _claude_parse,
                                             _codex_argv, _codex_parse,
                                             _kimi_argv, _kimi_parse)
    table = {"claude": (_claude_argv, _claude_parse),
             "codex": (_codex_argv, _codex_parse),
             "kimi": (_kimi_argv, _kimi_parse)}
    argv_fn, parse_fn = table[engine]
    # cwd matters: codex's sandbox and claude's trust context are scoped to
    # the process cwd, and a cross-engine review one-shot needs to actually
    # be run inside the repo it's reviewing (`git diff` etc.), not wherever
    # the orchestrator process happens to be. decompose's own planning call
    # has no repo checked out yet, so it leaves cwd=None and this falls back
    # to the process cwd.
    run_dir = Path(cwd) if cwd else Path.cwd()
    proc = subprocess.run(argv_fn(prompt, run_dir, engine), cwd=run_dir,
                          capture_output=True, text=True, timeout=600,
                          stdin=subprocess.DEVNULL)
    text, _ = parse_fn(proc.stdout or "")
    return text


# Priority order for "the check this repo actually gates PRs on". `bar` is
# the program-health convention; ci/check/verify cover the common rest. A
# member gate that skips these lets a PR pass green while the repo's own
# required check is red.
_CHECK_SCRIPTS = ("bar", "ci", "check", "verify")


def detect_repo_checks(repo: Path) -> str | None:
    """The repo's own check command, or None when it cannot be determined.
    Only Node package.json scripts are auto-detected; anything else should be
    passed explicitly with `fleet plan --checks`."""
    pkg = Path(repo) / "package.json"
    if not pkg.exists():
        return None
    try:
        scripts = (json.loads(pkg.read_text()) or {}).get("scripts") or {}
    except json.JSONDecodeError:
        return None
    name = next((s for s in _CHECK_SCRIPTS if s in scripts), None)
    if name is None:
        return None
    if (Path(repo) / "pnpm-lock.yaml").exists():
        return f"pnpm {name}"
    if (Path(repo) / "yarn.lock").exists():
        return f"yarn {name}"
    return f"npm run {name}"


def critical_path(tasks: list[dict]) -> list[str]:
    """The longest chain of dependency edges through the task graph.

    This is the floor on wall-clock: no number of agents shortens it, because
    every edge in it is a genuine wait. Measured against a real run — a 5-task
    fleet over a 3-deep chain — the prediction held to within 1%."""
    by_name = {t["name"]: t for t in tasks}
    memo: dict[str, list[str]] = {}

    def longest(name: str, seen: frozenset) -> list[str]:
        if name in memo:
            return memo[name]
        if name in seen:  # a cycle: stop rather than recurse forever
            return [name]
        best: list[str] = []
        for dep in by_name.get(name, {}).get("depends_on") or []:
            if dep not in by_name:
                continue
            chain = longest(dep, seen | {name})
            if len(chain) > len(best):
                best = chain
        out = best + [name]
        memo[name] = out
        return out

    return max((longest(t["name"], frozenset()) for t in tasks),
               key=len, default=[])


def parallel_ceiling(tasks: list[dict]) -> float:
    """Best achievable speedup over doing the tasks one at a time: the task
    count divided by the critical path's length. Amdahl's law with each task
    counted as one unit of work."""
    depth = len(critical_path(tasks))
    return (len(tasks) / depth) if depth else 1.0


def unjustified_edges(tasks: list[dict]) -> list[tuple[str, str]]:
    """(consumer, producer) pairs whose dependency is never explained.

    An edge is only real when the consumer reads the producer's output. The
    ones that cannot say what they read are the cheapest speedup available —
    cutting a false edge beats adding an agent."""
    out = []
    for t in tasks:
        reasons = t.get("depends_on_reason") or {}
        for dep in t.get("depends_on") or []:
            if not str(reasons.get(dep, "")).strip():
                out.append((t["name"], dep))
    return out


def detect_default_branch(repo: Path) -> str:
    """The branch members should branch from and PR into.

    `deliver.base` used to default to "main", which is wrong for any repo that
    integrates elsewhere: program-health's `develop` was 23 commits ahead of
    main, so an unfixed fleet would have cut every worktree from a stale trunk
    and aimed every PR at the wrong branch. Prefer the remote's declared HEAD,
    then a local `develop`, then whatever HEAD is on."""
    head = subprocess.run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
                          cwd=repo, capture_output=True, text=True)
    if head.returncode == 0 and head.stdout.strip():
        return head.stdout.strip().rsplit("/", 1)[-1]
    for candidate in ("develop", "main", "master"):
        got = subprocess.run(["git", "rev-parse", "--verify", f"origin/{candidate}"],
                             cwd=repo, capture_output=True, text=True)
        if got.returncode == 0:
            return candidate
    # symbolic-ref, not rev-parse: on a repo with no commits yet rev-parse
    # answers the literal string "HEAD", which is not a branch name.
    cur = subprocess.run(["git", "symbolic-ref", "--short", "HEAD"],
                         cwd=repo, capture_output=True, text=True)
    name = (cur.stdout or "").strip()
    return name if name and name != "HEAD" else "main"


def detect_prepare(repo: Path) -> str | None:
    """The command that makes a *fresh worktree* buildable, or None.

    Every member runs in a brand-new worktree, which has no `node_modules`.
    Without this the first gate exits 127 ("vitest: not found") and the member
    dies at preflight before writing a line — the cold-start failure the fleet
    retro named. Prefer the frozen-lockfile install so a member can never
    silently drift its dependency tree."""
    if not (Path(repo) / "package.json").exists():
        return None
    if (Path(repo) / "pnpm-lock.yaml").exists():
        return "pnpm install --frozen-lockfile"
    if (Path(repo) / "yarn.lock").exists():
        return "yarn install --frozen-lockfile"
    if (Path(repo) / "package-lock.json").exists():
        return "npm ci"
    return "npm install"


def _extract_json(text: str) -> dict:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        candidate = fenced.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(
                f"decompose: no JSON object found in model output: {text[:200]!r}")
        candidate = text[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"decompose: no JSON object found in model output: {text[:200]!r}") from e


def _validate(tasks: list[dict], engines: list[str]) -> None:
    if not tasks:
        raise ValueError("decompose produced no tasks")
    names = [t["name"] for t in tasks]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate task names: {names}")
    for t in tasks:
        for field in ("name", "title", "goal", "verify_command", "engine"):
            if not t.get(field):
                raise ValueError(f"task {t.get('name')!r} missing {field}")
        if t["engine"] not in engines:
            raise ValueError(f"task {t['name']!r} uses engine {t['engine']!r} "
                             f"not in requested engines {engines}")
        for dep in t.get("depends_on", []):
            if dep not in names:
                raise ValueError(f"task {t['name']!r} depends on unknown {dep!r}")


def _member_spec(t: dict, repo: str, repo_checks: str | None = None,
                 prepare: str | None = None, base: str | None = None) -> dict:
    # With repo checks known, the task's own command becomes the scoped gate
    # (what this worker is responsible for) and the repo's check becomes the
    # broad gate (what the repo requires). Cycle then distinguishes "passed"
    # from "completed-capped" instead of failing verified work.
    verify = {"gate": "command", "command": t["verify_command"]}
    if repo_checks:
        verify = {"gate": "command", "command": repo_checks,
                  "scoped_command": t["verify_command"]}
    return {
        "name": t["name"],
        "type": "coding",
        "goal": t["goal"],
        "workspace": {"repo": repo, "worktree": True,
                      "branch": f"setpoint/{t['name']}",
                      **({"prepare": prepare} if prepare else {})},
        "execute": {"engine": t["engine"]},
        "verify": verify,
        # no_progress_after matters more than max_iters. Four members once
        # burned all six iterations against byte-identical gate feedback;
        # raising the cap would have doubled the waste. Stopping when nothing
        # changes surfaces a broken gate at iteration 3 instead of 6.
        "stop": {"max_iters": 8, "no_progress_after": 3},
        # Must be truthy: run_loop only calls deliver() when
        # `getattr(spec, "deliver", None)` is truthy (spec.py / __main__.py
        # run_loop), so an empty {} here would silently skip commit/push/PR
        # and the worktree cleanup would then discard the member's work.
        # push/pr already default True inside deliver() even for an empty
        # dict, so these keys are set explicitly just to keep the dict
        # non-empty/truthy — see setpoint/deliver.py deliver().
        "deliver": {"push": True, "pr": True,
                    **({"base": base} if base else {})},
    }


def _plan_md(idea_name: str, tasks: list[dict]) -> str:
    lines = [f"# Fleet plan: {idea_name}", ""]
    lines += _parallelism_lines(tasks)
    for t in tasks:
        deps = t.get("depends_on") or []
        reasons = t.get("depends_on_reason") or {}
        if deps:
            rendered = []
            for d in deps:
                why = str(reasons.get(d, "")).strip()
                rendered.append(f"{d} ({why})" if why
                                else f"{d} (⚠ UNJUSTIFIED — candidate false edge)")
            dep_line = "; ".join(rendered)
        else:
            dep_line = "none"
        lines += [f"## {t['name']} — {t['title']} ({t['engine']})", "",
                  t["goal"], "",
                  f"- interfaces: {t.get('interfaces') or 'none'}",
                  f"- depends on: {dep_line}",
                  f"- verify: `{t['verify_command']}`", ""]
    lines += ["---", "Review this bundle, edit any member spec, then launch with:",
              "", "    setpoint fleet run <this dir>/fleet.yaml", ""]
    return "\n".join(lines)


def _parallelism_lines(tasks: list[dict]) -> list[str]:
    """The shape of the fan-out, stated up front. A deep chain means the fleet
    is nearly serial no matter how many agents run, and that is the single
    most useful thing to know before approving a plan."""
    chain = critical_path(tasks)
    ceiling = parallel_ceiling(tasks)
    lines = ["## Parallelism", "",
             f"- Tasks: {len(tasks)}",
             f"- Critical path: {len(chain)} deep — {' → '.join(chain)}",
             f"- Best possible speedup: ×{ceiling:.2f} "
             f"(no number of agents beats the critical path)"]
    unjustified = unjustified_edges(tasks)
    if unjustified:
        lines += ["", f"- ⚠ {len(unjustified)} dependency edge(s) with no stated "
                      f"reason: " + ", ".join(f"{c}→{p}" for c, p in unjustified)
                  + ". Cutting a false edge beats adding an agent — check each "
                    "one before approving."]
    if ceiling < 1.5 and len(tasks) > 2:
        lines += ["", "- ⚠ This fleet is close to serial. Consider re-cutting the "
                      "tasks so more of them are independent, or running it as a "
                      "single loop instead."]
    lines.append("")
    return lines


def decompose(idea_path: str, repo: str, engines: list[str], out_dir: str,
              oneshot=None, repo_checks: str | None = None,
              base: str | None = None, survey_text: str | None = None) -> Path:
    oneshot = oneshot or _default_oneshot
    # Absolutize here, at the single entry point, so every downstream
    # consumer (member specs' workspace.repo, fleet.yaml's room.repo) gets an
    # absolute path regardless of the caller's cwd -- a cwd-relative repo
    # path baked into those files would break as soon as they're read from
    # anywhere other than the directory decompose() itself ran in.
    repo = str(Path(repo).expanduser().resolve())
    idea = Path(idea_path).read_text()
    name = Path(idea_path).stem

    # A survey of what already exists outranks the idea file. The idea is a
    # proposal; the survey is the repo. Plan against the repo.
    survey_block = ""
    if survey_text and survey_text.strip():
        survey_block = (
            "\nWHAT IS ALREADY TRUE IN THIS REPO (a read-only survey run just now).\n"
            "This outranks the idea below. Do NOT create a task for anything the\n"
            "survey reports as already built — re-planning finished work is the\n"
            "most expensive mistake available here. If the survey says most of the\n"
            "idea already exists, return only the tasks that are genuinely left,\n"
            "and return an empty task list rather than inventing work.\n\n"
            + survey_text.strip() + "\n")

    raw = oneshot(engines[0], DECOMPOSE_PROMPT.format(
        engines=", ".join(engines), repo=repo, idea=idea,
        survey_block=survey_block))
    tasks = _extract_json(raw)["tasks"]
    if not tasks and survey_text:
        raise NothingLeftToBuild(
            "the survey found nothing left to build — every part of this idea "
            "already exists in the repo. Re-scope the idea or drop it; do not "
            "launch a fleet. The survey is in the bundle if you want to check it.")

    _validate(tasks, engines)

    # A single-engine fleet cannot cross-review: maker == checker for every
    # task. Say so at plan time, when it is still cheap to add an engine.
    if len({t["engine"] for t in tasks}) < 2:
        print("setpoint fleet plan: WARNING — this fleet uses a single engine, so "
              "no member can be cross-reviewed (maker == checker). Members will be "
              "reported 'unreviewed'. Re-run with --engines a,b to enable review.",
              file=sys.stderr)

    prepare = detect_prepare(Path(repo))
    base = base or detect_default_branch(Path(repo))

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "plan.md").write_text(_plan_md(name, tasks))
    (out / "tasks.json").write_text(json.dumps({"tasks": tasks}, indent=2) + "\n")

    members = []
    for t in tasks:
        member = f"{t['name']}.setpoint.yaml"
        (out / member).write_text(yaml.safe_dump(_member_spec(t, repo, repo_checks, prepare, base),
                                                 sort_keys=False))
        members.append(f"./{member}")

    fleet = {
        "name": name,
        "concurrency": min(4, len(tasks)),
        "members": members,
        "room": {
            "repo": repo,
            "tasks": [{"member": t["name"], "title": t["title"],
                       "goal": t.get("goal", ""),
                       "interfaces": t.get("interfaces", ""),
                       "depends_on": t.get("depends_on", [])} for t in tasks],
        },
    }
    fleet_path = out / "fleet.yaml"
    fleet_path.write_text(yaml.safe_dump(fleet, sort_keys=False))
    return fleet_path
