from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

VALID_TYPES = {"coding", "content"}
VALID_GATES = {"command", "judge"}
VALID_ENGINES = {"deepseek", "claude", "codex", "kimi"}
VALID_JUDGE_ENGINES = {"claude", "codex"}


@dataclass
class Workspace:
    repo: Path
    worktree: bool = False
    branch: str | None = None


@dataclass
class Context:
    files: list[str] = field(default_factory=list)
    scry: bool = False
    notes: str = ""


@dataclass
class ExecuteCfg:
    plan_model: str = "deepseek-v4-pro"
    model: str = "deepseek-v4-flash"
    engine: str = "deepseek"
    tools: list[str] = field(default_factory=lambda: ["read", "write", "edit", "bash"])
    max_turns: int = 25
    plan_hint: str = ""  # overlay-injected; never read from YAML


@dataclass
class VerifyCfg:
    gate: str = "command"
    command: str | None = None
    judge_model: str = "gpt-oss-20b"
    judge_engine: str | None = None
    rubric: str | None = None
    pass_threshold: float = 0.8
    checks: list[dict] = field(default_factory=list)
    timeout_secs: int = 600
    preflight: bool = True


@dataclass
class StopCfg:
    max_iters: int = 8
    no_progress_after: int | None = None
    wall_clock_secs: int | None = None
    on_pass: bool = True


@dataclass
class BudgetCfg:
    max_usd: float | None = None
    max_tokens: int | None = None


@dataclass
class MemoryCfg:
    scry_export: bool = False


@dataclass
class LoopSpec:
    name: str
    goal: str
    type: str
    workspace: Workspace
    context: Context
    execute: ExecuteCfg
    verify: VerifyCfg
    stop: StopCfg
    budget: BudgetCfg
    deliver: dict = field(default_factory=dict)
    memory: MemoryCfg = field(default_factory=MemoryCfg)
    explicit: list[str] = field(default_factory=list)


def load_spec(path: str) -> LoopSpec:
    p = Path(path).expanduser()
    # Emit deprecation warning before read_text() so users still get the tip
    # even if the file is missing or unreadable.
    if p.name.endswith(".loom.yaml"):
        print(f"warning: '{p.name}' uses the deprecated .loom.yaml extension, which "
              f"will stop loading in the next minor.\n"
              f"         run: setpoint migrate {p.parent.parent}",
              file=sys.stderr)
    raw = yaml.safe_load(p.read_text()) or {}

    for required in ("name", "goal", "type"):
        if not raw.get(required):
            raise ValueError(f"spec missing required field: {required}")
    if raw["type"] not in VALID_TYPES:
        raise ValueError(f"type must be one of {sorted(VALID_TYPES)}")

    ws_raw = raw.get("workspace") or {}
    if not ws_raw.get("repo"):
        raise ValueError("spec missing workspace.repo")
    workspace = Workspace(
        repo=Path(ws_raw["repo"]).expanduser(),
        worktree=bool(ws_raw.get("worktree", False)),
        branch=ws_raw.get("branch"),
    )

    ctx_raw = raw.get("context") or {}
    context = Context(
        files=list(ctx_raw.get("files") or []),
        scry=bool(ctx_raw.get("scry", False)),
        notes=ctx_raw.get("notes", ""),
    )

    ex_raw = raw.get("execute") or {}
    engine = ex_raw.get("engine", "deepseek")
    if engine not in VALID_ENGINES:
        raise ValueError(f"execute.engine must be one of {sorted(VALID_ENGINES)}")
    # Agent engines shell the `claude`/`codex` CLI, whose --model rejects a
    # deepseek model id (404). When such a spec omits execute.model, default it
    # to the engine sentinel ("claude"/"codex") so _claude_argv/_codex_argv omit
    # --model and the CLI uses its own configured default. Only the deepseek
    # engine keeps the deepseek default.
    default_model = engine if engine in {"claude", "codex", "kimi"} else "deepseek-v4-flash"
    execute = ExecuteCfg(
        plan_model=ex_raw.get("plan_model", "deepseek-v4-pro"),
        model=ex_raw.get("model", default_model),
        engine=engine,
        tools=list(ex_raw.get("tools") or ["read", "write", "edit", "bash"]),
        max_turns=int(ex_raw.get("max_turns", 25)),
    )

    v_raw = raw.get("verify") or {}
    gate = v_raw.get("gate", "command")
    if gate not in VALID_GATES:
        raise ValueError(f"verify.gate must be one of {sorted(VALID_GATES)}")
    judge_engine = v_raw.get("judge_engine")
    if judge_engine is not None and judge_engine not in VALID_JUDGE_ENGINES:
        raise ValueError(f"verify.judge_engine must be one of {sorted(VALID_JUDGE_ENGINES)}")
    verify = VerifyCfg(
        gate=gate,
        command=v_raw.get("command"),
        judge_model=v_raw.get("judge_model", "gpt-oss-20b"),
        judge_engine=judge_engine,
        rubric=v_raw.get("rubric"),
        pass_threshold=float(v_raw.get("pass_threshold", 0.8)),
        checks=list(v_raw.get("checks") or []),
        timeout_secs=int(v_raw.get("timeout_secs", 600)),
        preflight=bool(v_raw.get("preflight", True)),
    )
    if gate == "command" and not verify.command:
        raise ValueError("command gate requires verify.command")
    if gate == "judge" and not verify.rubric:
        raise ValueError("judge gate requires verify.rubric")
    if gate == "judge":
        if judge_engine is not None:
            # Agent judges (judge_engine set) ignore judge_model entirely —
            # AgentJudgeClient just runs the CLI for that engine — so the
            # only real signal is the engine itself.
            if judge_engine == execute.engine:
                raise ValueError("maker != checker: judge_engine must differ from execute.engine")
        else:
            # judge_engine unset -> judge runs on the same engine as the
            # executor, so the only remaining signal is the model.
            if verify.judge_model == execute.model:
                raise ValueError("maker != checker: judge must differ from executor by engine or model")

    s_raw = raw.get("stop") or {}
    stop = StopCfg(
        max_iters=int(s_raw.get("max_iters", 8)),
        no_progress_after=int(s_raw["no_progress_after"]) if s_raw.get("no_progress_after") is not None else None,
        wall_clock_secs=s_raw.get("wall_clock_secs"),
        on_pass=bool(s_raw.get("on_pass", True)),
    )

    b_raw = raw.get("budget") or {}
    budget = BudgetCfg(
        max_usd=b_raw.get("max_usd"),
        max_tokens=b_raw.get("max_tokens"),
    )

    deliver = raw.get("deliver") or {}
    if deliver.get("merge"):
        raise ValueError("deliver.merge must be false — setpoint never merges to main")
    if str(deliver.get("branch") or "").lower() in {"main", "master"}:
        raise ValueError("deliver.branch must not be main/master — setpoint never touches the trunk")
    if deliver.get("branch") and deliver.get("base") and \
            str(deliver["branch"]).lower() == str(deliver["base"]).lower():
        raise ValueError("deliver.base must differ from deliver.branch")

    m_raw = raw.get("memory") or {}
    memory = MemoryCfg(scry_export=bool(m_raw.get("scry_export", False)))

    explicit = []
    if "max_turns" in ex_raw:
        explicit.append("execute.max_turns")
    if "no_progress_after" in s_raw:
        explicit.append("stop.no_progress_after")

    return LoopSpec(
        name=raw["name"], goal=raw["goal"], type=raw["type"],
        workspace=workspace, context=context, execute=execute,
        verify=verify, stop=stop, budget=budget, deliver=deliver,
        memory=memory, explicit=explicit,
    )
