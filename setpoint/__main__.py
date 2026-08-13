from __future__ import annotations

import os
import sys
from pathlib import Path

from setpoint.clients import make_deepseek_client


def _runs_root() -> Path:
    return Path(os.environ.get("SETPOINT_RUNS_ROOT", str(Path.home() / ".setpoint" / "runs")))


def _build_executor(spec):
    engine = spec.execute.engine
    if engine == "claude":
        from setpoint.executor import ClaudeExecutor
        return ClaudeExecutor()
    if engine == "codex":
        from setpoint.executor import CodexExecutor
        return CodexExecutor()
    if engine == "kimi":
        from setpoint.executor.agent_cli import KimiExecutor
        return KimiExecutor()
    from setpoint.clients import make_deepseek_client
    from setpoint.executor import DeepSeekExecutor
    from setpoint.budget import PRICING
    return DeepSeekExecutor(client=make_deepseek_client(), pricing=PRICING,
                            max_turns=spec.execute.max_turns)


def _build_plan_client(spec):
    if spec.execute.engine in ("claude", "codex", "kimi"):
        from setpoint.executor.agent_plan import AgentPlanClient
        return AgentPlanClient()
    from setpoint.clients import make_deepseek_client
    return make_deepseek_client()


def run_loop(spec, *, fresh: bool = False, ui=None, abort_check=None):
    from setpoint.workspace import prepare_workspace
    from setpoint.budget import Budget, PRICING
    from setpoint.memory import Memory
    from setpoint.gates import build_gate, build_scoped_gate
    from setpoint.clients import make_judge_client
    from setpoint.ui import StreamUI
    from setpoint.cycle import Cycle

    memory = Memory(spec.name, root=_runs_root())
    if fresh:
        import shutil
        if memory.root.exists():
            shutil.rmtree(memory.root)

    from setpoint.lessons import LessonStore, promote_validated, repo_key
    lesson_store = LessonStore(repo_key(spec.workspace.repo))

    from setpoint.tuning import Overlay, apply_overlay, slug
    overlay = Overlay(f"{repo_key(spec.workspace.repo)}--{slug(spec.name)}")
    apply_overlay(spec, overlay.load())

    budget = Budget(spec.budget.max_usd, spec.budget.max_tokens, PRICING,
                     wall_clock_secs=spec.stop.wall_clock_secs)
    if ui is None:
        ui = StreamUI(name=spec.name, budget=budget)
    ui.header()

    judge_client = (make_judge_client(spec.verify.judge_model, engine=spec.verify.judge_engine)
                    if spec.verify.gate == "judge" else None)
    # The workspace comes first: the gate needs the worktree's derived port
    # base, so that a member's verify measures its own stack rather than a
    # sibling worktree's on a reused port.
    cwd, wt = prepare_workspace(spec)
    gate_env = ({"SETPOINT_PORT_BASE": str(wt.port_base)}
                if wt is not None and wt.port_base else None)
    if gate_env:
        spec.goal += (
            f"\n\nPorts: this worktree owns the port range starting at "
            f"{wt.port_base}. Any server you start must bind {wt.port_base} or "
            f"above (the value is also in .setpoint-ports.env). Never reuse a "
            f"default port — a sibling worktree is running the same stack.")

    gate = build_gate(spec, judge_client=judge_client, env=gate_env)
    scoped_gate = build_scoped_gate(spec, env=gate_env)
    executor = _build_executor(spec)
    plan_client = _build_plan_client(spec)

    try:
        cycle = Cycle(spec, executor, gate, memory, budget, ui, plan_client,
                      abort_check=abort_check, lesson_store=lesson_store,
                      scoped_gate=scoped_gate)
        state = cycle.run(cwd=cwd)

        # Lesson promotion, scry export, and retro tuning are best-effort
        # bookkeeping — a filesystem error here (e.g. an unwritable
        # ~/.setpoint/lessons or tuning dir) must never skip deliver() below,
        # or the finally block's worktree cleanup would destroy a passed
        # run's undelivered work.
        try:
            promoted = promote_validated(state, spec.goal, lesson_store)

            if spec.memory.scry_export and promoted:
                from setpoint.scry_export import export_lessons
                export_lessons(promoted, spec.workspace.repo)

            from setpoint.retro import run_retro
            run_retro(state, overlay, memory.root)
        except Exception as e:
            print(f"self-improvement bookkeeping skipped: {e}", file=sys.stderr)

        # deliver() must run while `cwd` still exists — a worktree cwd is
        # removed by wt.cleanup() below, so this has to happen inside the try.
        if getattr(spec, "deliver", None):
            from setpoint.deliver import deliver as _deliver
            # report_dir=memory.root: for `worktree: true` runs, cwd is a temp
            # worktree removed by wt.cleanup() in this finally block, so a
            # failure-path report.md must land somewhere durable instead.
            result = _deliver(spec, cwd, state, report_dir=memory.root)
            if result.delivered:
                print(f"delivered: {', '.join(result.actions)}"
                      + (f" — {result.pr_url}" if result.pr_url else ""))
            elif result.report_path:
                print(f"not delivered — report at {result.report_path}")
    finally:
        if wt is not None:
            wt.cleanup()
    return state


def cmd_run(spec_path: str, fresh: bool = False) -> int:
    from setpoint.spec import load_spec
    spec = load_spec(spec_path)
    state = run_loop(spec, fresh=fresh)
    return 0 if state.status == "passed" else 2


def cmd_ls() -> int:
    root = _runs_root()
    if not root.exists():
        print("no runs yet")
        return 0
    import json
    for d in sorted(root.iterdir()):
        sp = d / "state.json"
        if sp.exists():
            s = json.loads(sp.read_text())
            print(f"{s['name']:30} {s['status']:18} "
                  f"iters={len(s.get('iters', []))} ${s.get('spent_usd', 0):.2f}")
    return 0


def cmd_logs(name: str) -> int:
    log = _runs_root() / name / "log.md"
    if not log.exists():
        print(f"no log for {name}", file=sys.stderr)
        return 1
    print(log.read_text())
    return 0


def cmd_migrate(repo: str, dry_run: bool = False) -> int:
    from setpoint.migrate import apply_migration, plan_migration, render_plan

    root = Path(repo).expanduser()
    if not root.is_dir():
        print(f"migrate: no such directory: {root}", file=sys.stderr)
        return 1

    plan = plan_migration(root)
    print(render_plan(plan))
    if plan.is_blocked:
        return 1
    if dry_run or plan.is_empty:
        return 0
    apply_migration(plan)
    return 0


def cmd_fleet(rest: list[str]) -> int:
    from setpoint import fleet
    if not rest:
        print("fleet: usage: setpoint fleet {run <fleet.yaml> [--fresh] | status <fleet.yaml> | "
              "stop | plan <idea.md> --repo <path> --engines a,b,c [--out DIR] [--checks CMD]}",
              file=sys.stderr)
        return 1
    sub, args = rest[0], rest[1:]
    if sub == "stop":
        path = fleet.stop_sentinel_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("stop")
        print(f"fleet stop requested — sentinel at {path}")
        return 0
    if sub == "plan":
        # setpoint fleet plan <idea.md> --repo <path> --engines a,b,c [--out DIR]
        if not args:
            print("fleet plan: missing idea path", file=sys.stderr)
            return 1
        from setpoint.decompose import decompose
        from setpoint.spec import VALID_ENGINES
        idea = args[0]
        opts = args[1:]
        known_flags = ("--repo", "--engines", "--out", "--checks")
        values: dict[str, str] = {}
        i = 0
        while i < len(opts):
            tok = opts[i]
            if tok in known_flags:
                if i + 1 >= len(opts):
                    print(f"fleet plan: {tok} requires a value", file=sys.stderr)
                    return 2
                values[tok] = opts[i + 1]
                i += 2
                continue
            if tok.startswith("--"):
                print(f"fleet plan: unknown flag {tok}", file=sys.stderr)
                return 2
            i += 1
        repo = values.get("--repo")
        if not repo:
            print("setpoint fleet plan: --repo is required", file=sys.stderr)
            return 2
        engines = (values.get("--engines") or "claude").split(",")
        unknown = set(engines) - VALID_ENGINES
        if unknown:
            print(f"fleet plan: unknown engine(s) {sorted(unknown)}, "
                  f"must be one of {sorted(VALID_ENGINES)}", file=sys.stderr)
            return 2
        out = values.get("--out") or f"fleets/{Path(idea).stem}"
        from setpoint.decompose import detect_repo_checks
        checks = values.get("--checks")
        if checks is None:
            checks = detect_repo_checks(Path(repo).expanduser())
            if checks:
                print(f"fleet plan: using detected repo checks as the broad gate: {checks}")
            else:
                print("fleet plan: no repo check command detected — member gates will "
                      "be the task commands only. Pass --checks '<cmd>' to add the "
                      "repo's required check.", file=sys.stderr)
        fleet_path = decompose(idea, repo, engines, out, repo_checks=checks or None)
        print(f"fleet bundle written to {fleet_path.parent}")
        print(f"review plan.md, then: setpoint fleet run {fleet_path}")
        return 0
    if not args:
        print(f"fleet {sub}: missing fleet.yaml", file=sys.stderr)
        return 1
    if sub == "run":
        results = fleet.run_fleet(args[0], fresh=("--fresh" in args))
        print(fleet.fleet_status(args[0]))
        return 0 if all(v == "passed" for v in results.values()) else 2
    if sub == "status":
        print(fleet.fleet_status(args[0]))
        return 0
    print(f"unknown fleet subcommand: {sub}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv, find_dotenv
        load_dotenv(find_dotenv(usecwd=True))
    except ImportError:
        pass
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print("setpoint — DISCOVER->PLAN->EXECUTE->VERIFY->ITERATE loop engine")
        print("usage: setpoint {run <spec.yaml> [--fresh] | resume <spec.yaml> | ls | "
              "logs <name> | migrate <repo> [--dry-run] | fleet run|status|stop|plan}")
        return 0

    cmd, rest = argv[0], argv[1:]
    if cmd in ("run", "resume"):
        if not rest:
            print(f"{cmd}: missing spec path", file=sys.stderr)
            return 1
        return cmd_run(rest[0], fresh=("--fresh" in rest))
    if cmd == "ls":
        return cmd_ls()
    if cmd == "logs":
        if not rest:
            print("logs: missing run name", file=sys.stderr)
            return 1
        return cmd_logs(rest[0])
    if cmd == "migrate":
        if not rest:
            print("migrate: missing repo path", file=sys.stderr)
            return 1
        return cmd_migrate(rest[0], dry_run=("--dry-run" in rest))
    if cmd == "fleet":
        return cmd_fleet(rest)
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
