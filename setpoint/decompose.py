"""Turn an idea file into a fleet bundle: plan.md, tasks.json, per-task
member specs, and a fleet.yaml with a room section.

Gate 1 of the fleet design is human review of the generated bundle — nothing
here launches anything.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import yaml

DECOMPOSE_PROMPT = """You are decomposing a software idea into 2-6 parallelizable \
engineering tasks for a fleet of coding agents working in one repository.

Rules:
- Tasks must be independently implementable in separate git worktrees; put a
  dependency edge (depends_on) only where one task consumes another's output.
- Where two tasks share a boundary (API/schema/function), describe it in the
  producing task's "interfaces" field concretely enough to negotiate from.
- Every task needs a deterministic verify_command that exits 0 on success,
  runnable from the repo root.
- Assign each task an engine from this list, spreading work across them: {engines}
- Task names are kebab-case slugs, unique.

Repository: {repo}

Idea:
{idea}

Respond with ONLY a JSON object (fenced or bare) of the shape:
{{"tasks": [{{"name", "title", "goal", "interfaces", "depends_on", "verify_command", "engine"}}]}}
"""


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


def _member_spec(t: dict, repo: str) -> dict:
    return {
        "name": t["name"],
        "type": "coding",
        "goal": t["goal"],
        "workspace": {"repo": repo, "worktree": True,
                      "branch": f"setpoint/{t['name']}"},
        "execute": {"engine": t["engine"]},
        "verify": {"gate": "command", "command": t["verify_command"]},
        "stop": {"max_iters": 6},
        # Must be truthy: run_loop only calls deliver() when
        # `getattr(spec, "deliver", None)` is truthy (spec.py / __main__.py
        # run_loop), so an empty {} here would silently skip commit/push/PR
        # and the worktree cleanup would then discard the member's work.
        # push/pr already default True inside deliver() even for an empty
        # dict, so these keys are set explicitly just to keep the dict
        # non-empty/truthy — see setpoint/deliver.py deliver().
        "deliver": {"push": True, "pr": True},
    }


def _plan_md(idea_name: str, tasks: list[dict]) -> str:
    lines = [f"# Fleet plan: {idea_name}", ""]
    for t in tasks:
        deps = ", ".join(t.get("depends_on", [])) or "none"
        lines += [f"## {t['name']} — {t['title']} ({t['engine']})", "",
                  t["goal"], "",
                  f"- interfaces: {t.get('interfaces') or 'none'}",
                  f"- depends on: {deps}",
                  f"- verify: `{t['verify_command']}`", ""]
    lines += ["---", "Review this bundle, edit any member spec, then launch with:",
              "", "    setpoint fleet run <this dir>/fleet.yaml", ""]
    return "\n".join(lines)


def decompose(idea_path: str, repo: str, engines: list[str], out_dir: str,
              oneshot=None) -> Path:
    oneshot = oneshot or _default_oneshot
    # Absolutize here, at the single entry point, so every downstream
    # consumer (member specs' workspace.repo, fleet.yaml's room.repo) gets an
    # absolute path regardless of the caller's cwd -- a cwd-relative repo
    # path baked into those files would break as soon as they're read from
    # anywhere other than the directory decompose() itself ran in.
    repo = str(Path(repo).expanduser().resolve())
    idea = Path(idea_path).read_text()
    name = Path(idea_path).stem

    raw = oneshot(engines[0], DECOMPOSE_PROMPT.format(
        engines=", ".join(engines), repo=repo, idea=idea))
    tasks = _extract_json(raw)["tasks"]
    _validate(tasks, engines)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "plan.md").write_text(_plan_md(name, tasks))
    (out / "tasks.json").write_text(json.dumps({"tasks": tasks}, indent=2) + "\n")

    members = []
    for t in tasks:
        member = f"{t['name']}.setpoint.yaml"
        (out / member).write_text(yaml.safe_dump(_member_spec(t, repo),
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
