# Fleet Retro — setpoint Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix retro items 1–11 so a fleet reports the truth about its own outcome, cuts worktrees from the right base, and stops burning iterations on failures that are not the worker's fault.

**Architecture:** All changes are inside the existing `setpoint` Python package. Three layers get touched: the workspace layer (`workspace.py` — worktree base, prepare command, port derivation), the loop layer (`cycle.py`, `spec.py`, `gates/command.py` — scoped gates and the `completed-capped` terminal status), and the fleet layer (`fleet.py`, `decompose.py`, `deliver.py`, `memory.py`, `__main__.py` — review-gated success, per-fleet run state, honest status columns). No new modules; no new dependencies.

**Tech Stack:** Python 3.12+, pytest, PyYAML, stdlib `subprocess`/`threading`/`concurrent.futures`.

**Spec:** `docs/retros/2026-08-13-first-fleets.md` (items 1–11)

## Global Constraints

- setpoint NEVER merges and NEVER deploys. `deliver.py`'s `ALLOWED_VERBS = ("git", "gh", "gog")` allow-list and `_check_no_merge` stay intact; no task may widen them.
- Every new spec field is optional with a backward-compatible default. An existing `.setpoint.yaml` must load unchanged.
- Every new `RunState`/`IterRecord` field must be defaulted so a `state.json` written before this work still loads (`memory.py` dataclass defaults).
- Run the full suite with `python -m pytest -q` from the repo root before each commit. It is currently green at 186 tests; it must stay green.
- Fleet members run in threads inside one process. Never mutate `os.environ` from per-member code — pass values explicitly.
- Tests must not require network, a real `gh`, or a real `scry` binary. Use the existing seams: `runner=` in `deliver`, `run_loop=`/`room_client=`/`oneshot=` in `run_fleet`, `tests/fake_room_mcp.py` for the MCP transport.

---

### Task 1: Cut worktrees from `origin/<base>` (retro item 3)

Wave 1 started every worktree 236 commits behind origin because `git worktree add -B <branch> <path>` branches from whatever the local checkout's HEAD happens to be. The worktree must be cut from the freshly fetched remote base.

**Files:**
- Modify: `setpoint/workspace.py:8-39`
- Test: `tests/test_workspace.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `Worktree(repo: Path, branch: str, base: str | None = None)` with `.create() -> Path`; `prepare_workspace(spec) -> tuple[Path, Worktree | None]` unchanged in signature. `Worktree.base_ref` attribute holds the ref actually used (`"origin/main"` or `"HEAD"`), which Task 8's status line does not use but Task 3's port test does not either — it exists for debugging and for the assertion in this task's tests.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_workspace.py`:

```python
def _make_origin_repo(tmp_path) -> tuple[Path, Path]:
    """Return (clone, origin). The clone is one commit behind origin/main."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "--initial-branch=main")
    _git(origin, "config", "user.email", "t@t")
    _git(origin, "config", "user.name", "t")
    (origin / "f.txt").write_text("hi")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-qm", "init")

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)],
                   check=True, capture_output=True)
    _git(clone, "config", "user.email", "t@t")
    _git(clone, "config", "user.name", "t")

    # origin moves ahead; the clone does not know about it yet.
    (origin / "new.txt").write_text("ahead")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-qm", "ahead")
    return clone, origin


def test_worktree_branches_from_fetched_origin_base(tmp_path):
    clone, _origin = _make_origin_repo(tmp_path)
    wt = Worktree(repo=clone, branch="setpoint/test", base="main")
    path = wt.create()
    # The commit only origin had must be present: the worktree was cut from
    # origin/main after a fetch, not from the stale local HEAD.
    assert (path / "new.txt").read_text() == "ahead"
    assert wt.base_ref == "origin/main"
    wt.cleanup()


def test_worktree_falls_back_to_head_without_origin(tmp_path):
    repo = _make_repo(tmp_path)  # no remote at all
    wt = Worktree(repo=repo, branch="setpoint/test", base="main")
    path = wt.create()
    assert (path / "f.txt").read_text() == "hi"
    assert wt.base_ref == "HEAD"
    wt.cleanup()


def test_prepare_workspace_passes_deliver_base_as_worktree_base(tmp_path):
    repo = _make_repo(tmp_path)
    spec = LoopSpec(name="n", goal="g", type="coding",
                    workspace=Workspace(repo=repo, worktree=True, branch=None),
                    context=Context(), execute=ExecuteCfg(),
                    verify=VerifyCfg(command="true"),
                    stop=StopCfg(), budget=BudgetCfg(),
                    deliver={"base": "develop"})
    cwd, wt = prepare_workspace(spec)
    assert wt is not None
    assert wt.base == "develop"
    wt.cleanup()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_workspace.py -q`
Expected: FAIL — `Worktree.__init__() got an unexpected keyword argument 'base'`

- [ ] **Step 3: Implement**

Replace `setpoint/workspace.py:8-39` with:

```python
class Worktree:
    def __init__(self, repo: Path, branch: str, base: str | None = None):
        self.repo = Path(repo)
        self.branch = branch
        self.base = base
        self.path: Path | None = None
        # The ref create() actually branched from. "HEAD" means the fallback
        # fired (no origin, or the fetch failed) — read it when a run's
        # starting point is in question.
        self.base_ref: str | None = None

    def _resolve_base_ref(self) -> str:
        """Fetch and return `origin/<base>`, or "HEAD" when that is not
        available. Cutting from the local checkout is the bug this guards
        (retro item 3): every wave-1 worktree started 236 commits behind.
        We degrade to HEAD rather than failing, because local test repos and
        remote-less scratch repos are legitimate."""
        if not self.base:
            return "HEAD"
        fetch = subprocess.run(
            ["git", "fetch", "origin", self.base],
            cwd=self.repo, capture_output=True, text=True,
        )
        if fetch.returncode != 0:
            print(f"setpoint: `git fetch origin {self.base}` failed in {self.repo} "
                  f"— branching from local HEAD instead:\n{fetch.stderr.strip()}",
                  file=sys.stderr)
            return "HEAD"
        verify = subprocess.run(
            ["git", "rev-parse", "--verify", f"origin/{self.base}"],
            cwd=self.repo, capture_output=True, text=True,
        )
        if verify.returncode != 0:
            print(f"setpoint: origin/{self.base} does not resolve in {self.repo} "
                  f"— branching from local HEAD instead", file=sys.stderr)
            return "HEAD"
        return f"origin/{self.base}"

    def create(self) -> Path:
        target = Path(tempfile.mkdtemp(prefix="setpoint-wt-"))
        self.base_ref = self._resolve_base_ref()
        # -B resets the branch if it already exists (resume-friendly). The
        # trailing ref is the start point: without it git uses the current
        # HEAD of the invoking checkout.
        subprocess.run(
            ["git", "worktree", "add", "-B", self.branch, str(target), self.base_ref],
            cwd=self.repo, check=True, capture_output=True, text=True,
        )
        self.path = target
        return target

    def cleanup(self) -> None:
        if self.path is None:
            return
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(self.path)],
            cwd=self.repo, capture_output=True, text=True,
        )
        self.path = None


def prepare_workspace(spec) -> tuple[Path, Worktree | None]:
    if spec.workspace.worktree:
        branch = spec.workspace.branch or f"setpoint/{spec.name}"
        # The PR's base is the branch point: a member that PRs into `develop`
        # must be cut from origin/develop, not from main.
        base = (spec.deliver or {}).get("base") or "main"
        wt = Worktree(repo=spec.workspace.repo, branch=branch, base=base)
        return wt.create(), wt
    return spec.workspace.repo, None
```

Add `import sys` to the imports at the top of the file.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_workspace.py -q && python -m pytest -q`
Expected: PASS, full suite green.

- [ ] **Step 5: Commit**

```bash
git add setpoint/workspace.py tests/test_workspace.py
git commit -m "Cut fleet worktrees from the fetched origin base"
```

---

### Task 2: Per-worktree `prepare` command (retro item 10)

Fresh worktrees lack workspace `dist/`, so a `demo:verify` gate fails cold until `pnpm build` runs. Give the spec a `workspace.prepare` command that runs once per worktree, before PREFLIGHT.

**Files:**
- Modify: `setpoint/spec.py:15-20` (Workspace dataclass), `setpoint/spec.py:104-111` (loader)
- Modify: `setpoint/workspace.py` (`prepare_workspace`)
- Test: `tests/test_spec.py`, `tests/test_workspace.py`

**Interfaces:**
- Consumes: `Worktree`/`prepare_workspace` from Task 1.
- Produces: `Workspace.prepare: str | None`; `prepare_workspace` runs it in the returned cwd and raises `RuntimeError` when it exits non-zero.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_workspace.py`:

```python
def test_prepare_command_runs_once_in_the_worktree(tmp_path):
    repo = _make_repo(tmp_path)
    spec = LoopSpec(name="n", goal="g", type="coding",
                    workspace=Workspace(repo=repo, worktree=True, branch=None,
                                        prepare="echo built > built.txt"),
                    context=Context(), execute=ExecuteCfg(),
                    verify=VerifyCfg(command="true"),
                    stop=StopCfg(), budget=BudgetCfg())
    cwd, wt = prepare_workspace(spec)
    assert (cwd / "built.txt").read_text().strip() == "built"
    assert not (repo / "built.txt").exists()  # ran in the worktree, not the repo
    wt.cleanup()


def test_prepare_command_failure_raises(tmp_path):
    import pytest
    repo = _make_repo(tmp_path)
    spec = LoopSpec(name="n", goal="g", type="coding",
                    workspace=Workspace(repo=repo, worktree=True, branch=None,
                                        prepare="exit 3"),
                    context=Context(), execute=ExecuteCfg(),
                    verify=VerifyCfg(command="true"),
                    stop=StopCfg(), budget=BudgetCfg())
    with pytest.raises(RuntimeError, match="workspace.prepare failed"):
        prepare_workspace(spec)
```

Add to `tests/test_spec.py`:

```python
def test_workspace_prepare_loads_and_defaults_to_none(tmp_path):
    from setpoint.spec import load_spec
    import yaml
    p = tmp_path / "s.setpoint.yaml"
    p.write_text(yaml.safe_dump({
        "name": "n", "type": "coding", "goal": "g",
        "workspace": {"repo": str(tmp_path), "prepare": "pnpm build"},
        "verify": {"gate": "command", "command": "true"},
    }))
    assert load_spec(str(p)).workspace.prepare == "pnpm build"

    p2 = tmp_path / "s2.setpoint.yaml"
    p2.write_text(yaml.safe_dump({
        "name": "n", "type": "coding", "goal": "g",
        "workspace": {"repo": str(tmp_path)},
        "verify": {"gate": "command", "command": "true"},
    }))
    assert load_spec(str(p2)).workspace.prepare is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_workspace.py tests/test_spec.py -q`
Expected: FAIL — `Workspace.__init__() got an unexpected keyword argument 'prepare'`

- [ ] **Step 3: Implement**

In `setpoint/spec.py`, extend the dataclass:

```python
@dataclass
class Workspace:
    repo: Path
    worktree: bool = False
    branch: str | None = None
    # Shell command run once in a freshly created worktree before the loop
    # starts. Fresh worktrees have no build output, so a gate that needs
    # `dist/` fails cold through every iteration otherwise (retro item 10).
    prepare: str | None = None
```

and in `load_spec`:

```python
    workspace = Workspace(
        repo=Path(ws_raw["repo"]).expanduser(),
        worktree=bool(ws_raw.get("worktree", False)),
        branch=ws_raw.get("branch"),
        prepare=ws_raw.get("prepare"),
    )
```

In `setpoint/workspace.py`, replace `prepare_workspace` with:

```python
def _run_prepare(command: str, cwd: Path) -> None:
    print(f"setpoint: workspace.prepare — {command}")
    proc = subprocess.run(command, shell=True, cwd=cwd,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        tail = ((proc.stdout or "") + (proc.stderr or "")).strip()[-2000:]
        raise RuntimeError(
            f"workspace.prepare failed (exit {proc.returncode}): {command}\n{tail}")


def prepare_workspace(spec) -> tuple[Path, Worktree | None]:
    if spec.workspace.worktree:
        branch = spec.workspace.branch or f"setpoint/{spec.name}"
        base = (spec.deliver or {}).get("base") or "main"
        wt = Worktree(repo=spec.workspace.repo, branch=branch, base=base)
        cwd = wt.create()
        if spec.workspace.prepare:
            try:
                _run_prepare(spec.workspace.prepare, cwd)
            except Exception:
                # Do not leak the worktree when prepare fails — the run is
                # over before it started.
                wt.cleanup()
                raise
        return cwd, wt
    if spec.workspace.prepare:
        _run_prepare(spec.workspace.prepare, spec.workspace.repo)
    return spec.workspace.repo, None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_workspace.py tests/test_spec.py -q && python -m pytest -q`
Expected: PASS, full suite green.

- [ ] **Step 5: Commit**

```bash
git add setpoint/workspace.py setpoint/spec.py tests/test_workspace.py tests/test_spec.py
git commit -m "Run a prepare command once per fresh worktree"
```

---

### Task 3: Derive a port base per worktree (retro item 4)

The sweep measured the wrong web tree through a reused port. Derive a deterministic, per-worktree port base and hand it to both the agent and the gate, so two members never share a port by accident.

**Files:**
- Modify: `setpoint/workspace.py`
- Modify: `setpoint/gates/command.py:16-50`
- Modify: `setpoint/gates/__init__.py:27-42`
- Modify: `setpoint/__main__.py:74-78` (thread the port base into the gate)
- Test: `tests/test_workspace.py`, `tests/test_gates.py`

**Interfaces:**
- Consumes: `prepare_workspace` from Task 2.
- Produces: `port_base(worktree: Path) -> int` (deterministic, in 20000–39999); `prepare_workspace` writes `<cwd>/.setpoint-ports.env` containing `SETPOINT_PORT_BASE=<n>` and returns the same value through the new attribute `Worktree.port_base`; `CommandGate(command, timeout, env=None)` merges `env` over `os.environ` for the verify subprocess; `build_gate(spec, judge_client=None, env=None)` forwards it.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_workspace.py`:

```python
def test_port_base_is_deterministic_and_distinct(tmp_path):
    from setpoint.workspace import port_base
    a, b = tmp_path / "wt-a", tmp_path / "wt-b"
    assert port_base(a) == port_base(a)          # deterministic
    assert port_base(a) != port_base(b)          # distinct per worktree
    assert 20000 <= port_base(a) < 40000         # in the private range


def test_worktree_writes_ports_env_file(tmp_path):
    from setpoint.workspace import port_base
    repo = _make_repo(tmp_path)
    spec = LoopSpec(name="n", goal="g", type="coding",
                    workspace=Workspace(repo=repo, worktree=True, branch=None),
                    context=Context(), execute=ExecuteCfg(),
                    verify=VerifyCfg(command="true"),
                    stop=StopCfg(), budget=BudgetCfg())
    cwd, wt = prepare_workspace(spec)
    assert wt.port_base == port_base(cwd)
    assert f"SETPOINT_PORT_BASE={wt.port_base}" in (cwd / ".setpoint-ports.env").read_text()
    wt.cleanup()
```

Add to `tests/test_gates.py`:

```python
def test_command_gate_passes_env_to_the_verify_subprocess(tmp_path):
    from setpoint.gates.command import CommandGate
    gate = CommandGate(command='test "$SETPOINT_PORT_BASE" = "31337"',
                       env={"SETPOINT_PORT_BASE": "31337"})
    res = gate.verify(cwd=tmp_path, on_event=lambda e: None)
    assert res.passed is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_workspace.py tests/test_gates.py -q`
Expected: FAIL — `cannot import name 'port_base'`, and `CommandGate.__init__() got an unexpected keyword argument 'env'`

- [ ] **Step 3: Implement**

In `setpoint/workspace.py` add (imports: `hashlib`, `os`):

```python
# Ports are derived, never reused: two worktrees of the same repo run the
# same stack, and a reused port silently measures the *other* tree (retro
# item 4). 20000-39999 avoids the ephemeral range and common dev defaults.
_PORT_FLOOR = 20000
_PORT_SPAN = 20000


def port_base(worktree: Path) -> int:
    digest = hashlib.sha256(str(Path(worktree).resolve()).encode()).digest()
    return _PORT_FLOOR + int.from_bytes(digest[:4], "big") % _PORT_SPAN
```

Give `Worktree.__init__` a `self.port_base: int | None = None`, and set it at the end of `create()`:

```python
        self.path = target
        self.port_base = port_base(target)
        return target
```

In `prepare_workspace`, right after `cwd = wt.create()` (before `prepare` runs, so the prepare command can use it):

```python
        (cwd / ".setpoint-ports.env").write_text(
            f"SETPOINT_PORT_BASE={wt.port_base}\n")
```

and pass the env through to `_run_prepare`:

```python
def _run_prepare(command: str, cwd: Path, env: dict | None = None) -> None:
    print(f"setpoint: workspace.prepare — {command}")
    proc = subprocess.run(command, shell=True, cwd=cwd,
                          capture_output=True, text=True,
                          env={**os.environ, **(env or {})})
```

with the worktree call site becoming `_run_prepare(spec.workspace.prepare, cwd, {"SETPOINT_PORT_BASE": str(wt.port_base)})`.

In `setpoint/gates/command.py`:

```python
class CommandGate(Gate):
    supports_preflight = True

    def __init__(self, command: str, timeout: float = 600, env: dict | None = None):
        self.command = command
        self.timeout = timeout
        # Merged over os.environ for the verify subprocess. Carries
        # SETPOINT_PORT_BASE so the gate measures its own worktree's stack.
        self.env = env
```

and in `verify`, add `env={**os.environ, **(self.env or {})}` to the `subprocess.Popen(...)` call.

In `setpoint/gates/__init__.py`:

```python
def build_gate(spec, judge_client=None, env=None) -> Gate:
    from .command import CommandGate
    from .judge import JudgeGate

    if spec.verify.gate == "command":
        return CommandGate(command=spec.verify.command,
                           timeout=getattr(spec.verify, "timeout_secs", 600),
                           env=env)
```

(the judge branch is unchanged).

In `setpoint/__main__.py` `run_loop`, move `prepare_workspace` above `build_gate` and pass the env:

```python
    cwd, wt = prepare_workspace(spec)
    gate_env = ({"SETPOINT_PORT_BASE": str(wt.port_base)}
                if wt is not None and wt.port_base else None)
    gate = build_gate(spec, judge_client=judge_client, env=gate_env)
    executor = _build_executor(spec)
    plan_client = _build_plan_client(spec)

    try:
```

Delete the now-duplicated `cwd, wt = prepare_workspace(spec)` that preceded the old `try:`, and make sure the `gate = build_gate(...)` line that used to sit above it is removed. Also tell the agent about its ports by appending to the goal in `run_loop`, immediately after the `prepare_workspace` call:

```python
    if gate_env:
        spec.goal += (
            f"\n\nPorts: this worktree owns the port range starting at "
            f"{wt.port_base}. Any server you start must bind {wt.port_base} or "
            f"above (the values are also in .setpoint-ports.env). Never reuse a "
            f"default port — a sibling worktree is running the same stack.")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_workspace.py tests/test_gates.py -q && python -m pytest -q`
Expected: PASS, full suite green.

- [ ] **Step 5: Commit**

```bash
git add setpoint/workspace.py setpoint/gates/command.py setpoint/gates/__init__.py setpoint/__main__.py tests/test_workspace.py tests/test_gates.py
git commit -m "Derive a port base per worktree"
```

---

### Task 4: Scoped gate and the `completed-capped` status (retro item 2)

The sweep's deliverable was verified at iteration 2, then iterations 3–6 died re-running a full-journey gate that was red on pre-existing flakes — and the run ended `stopped`, indistinguishable from failure. Add a task-scoped gate: when the scoped gate passes but the broad gate does not, stop immediately with a distinct terminal status.

**Files:**
- Modify: `setpoint/spec.py` (`VerifyCfg`, loader)
- Modify: `setpoint/gates/__init__.py` (`build_scoped_gate`)
- Modify: `setpoint/cycle.py:158-286` (`Cycle.run`, `Cycle.__init__`)
- Modify: `setpoint/__main__.py` (`run_loop` builds and passes the scoped gate)
- Test: `tests/test_cycle.py`, `tests/test_spec.py`

**Interfaces:**
- Consumes: `build_gate(spec, judge_client, env)` from Task 3.
- Produces: `VerifyCfg.scoped_command: str | None`; `build_scoped_gate(spec, env=None) -> Gate | None`; `Cycle(..., scoped_gate=None)`; new terminal `RunState.status` value `"completed-capped"`, meaning *this worker's deliverable verified, the broad gate did not pass and is not attributable to this worker*.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_spec.py`:

```python
def test_verify_scoped_command_loads(tmp_path):
    from setpoint.spec import load_spec
    import yaml
    p = tmp_path / "s.setpoint.yaml"
    p.write_text(yaml.safe_dump({
        "name": "n", "type": "coding", "goal": "g",
        "workspace": {"repo": str(tmp_path)},
        "verify": {"gate": "command", "command": "pnpm bar",
                   "scoped_command": "pnpm test admissions"},
    }))
    spec = load_spec(str(p))
    assert spec.verify.scoped_command == "pnpm test admissions"
    assert spec.verify.command == "pnpm bar"
```

Add to `tests/test_cycle.py` (match the fake executor/gate helpers already in that file; these use the same shapes):

```python
class _ScriptedGate:
    """Gate returning a canned pass/fail sequence, one entry per verify()."""
    supports_preflight = False

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def verify(self, cwd, on_event):
        from setpoint.gates import GateResult
        self.calls += 1
        passed = self.results[min(self.calls - 1, len(self.results) - 1)]
        return GateResult(passed=passed,
                          feedback="ok" if passed else "broad gate red")


def test_scoped_pass_with_broad_fail_ends_completed_capped(tmp_path):
    spec = _spec(tmp_path, max_iters=6)
    scoped = _ScriptedGate([True])        # the worker's own deliverable verifies
    broad = _ScriptedGate([False])        # repo-wide gate is red regardless
    cycle = _cycle(spec, gate=broad, scoped_gate=scoped, tmp_path=tmp_path)
    state = cycle.run(cwd=tmp_path)
    assert state.status == "completed-capped"
    # It must stop at once, not burn the remaining iterations.
    assert broad.calls == 1
    assert len(state.iters) == 1


def test_scoped_and_broad_both_pass_is_plain_passed(tmp_path):
    spec = _spec(tmp_path, max_iters=6)
    cycle = _cycle(spec, gate=_ScriptedGate([True]),
                   scoped_gate=_ScriptedGate([True]), tmp_path=tmp_path)
    state = cycle.run(cwd=tmp_path)
    assert state.status == "passed"


def test_scoped_fail_keeps_iterating(tmp_path):
    spec = _spec(tmp_path, max_iters=2)
    scoped = _ScriptedGate([False, False])
    cycle = _cycle(spec, gate=_ScriptedGate([False]),
                   scoped_gate=scoped, tmp_path=tmp_path)
    state = cycle.run(cwd=tmp_path)
    assert state.status == "stopped"
    assert scoped.calls == 2  # the scoped gate is what drives iteration
```

If `tests/test_cycle.py` has no `_spec`/`_cycle` helpers with those signatures, add them next to the existing fixtures, forwarding `scoped_gate` into `Cycle(...)` and defaulting it to `None`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cycle.py tests/test_spec.py -q`
Expected: FAIL — `Cycle.__init__() got an unexpected keyword argument 'scoped_gate'`

- [ ] **Step 3: Implement**

`setpoint/spec.py` — add to `VerifyCfg`:

```python
    # Task-scoped gate. When set, it is the gate that drives iteration; the
    # broad `command` gate then only decides between "passed" and
    # "completed-capped" (retro item 2). Optional: without it behavior is
    # exactly as before.
    scoped_command: str | None = None
```

and in the loader's `VerifyCfg(...)` call: `scoped_command=v_raw.get("scoped_command"),`.

`setpoint/gates/__init__.py` — add:

```python
def build_scoped_gate(spec, env=None) -> Gate | None:
    """The task-scoped gate, or None when the spec declares no scope. Always
    a CommandGate: a scoped gate exists to be cheap and deterministic."""
    from .command import CommandGate

    if not getattr(spec.verify, "scoped_command", None):
        return None
    return CommandGate(command=spec.verify.scoped_command,
                       timeout=getattr(spec.verify, "timeout_secs", 600),
                       env=env)
```

`setpoint/cycle.py` — add `scoped_gate=None` to `Cycle.__init__` (store as `self.scoped_gate = scoped_gate`), then change the VERIFY/ITERATE section of `run()`. Replace the VERIFY block:

```python
            # VERIFY
            self.ui.stage("VERIFY", i, self.spec.stop.max_iters)
            gate_result = self.gate.verify(cwd=cwd, on_event=lambda e: None)
            self.ui.verify(gate_result)
```

with:

```python
            # VERIFY. With a scoped gate configured, the scoped gate is the
            # one that decides whether the worker's own deliverable is done;
            # the broad gate is consulted only once the scoped gate is green,
            # to separate "passed" from "completed-capped".
            self.ui.stage("VERIFY", i, self.spec.stop.max_iters)
            capped = False
            if self.scoped_gate is not None:
                gate_result = self.scoped_gate.verify(cwd=cwd, on_event=lambda e: None)
                if gate_result.passed:
                    broad = self.gate.verify(cwd=cwd, on_event=lambda e: None)
                    if not broad.passed:
                        capped = True
                        gate_result = GateResult(
                            passed=True,
                            feedback="scoped gate passed; repo-wide gate still red "
                                     f"(not attributable to this task):\n{broad.feedback}",
                            score=gate_result.score)
            else:
                gate_result = self.gate.verify(cwd=cwd, on_event=lambda e: None)
            self.ui.verify(gate_result)
```

Add `from setpoint.gates import GateResult` to the imports at the top of `cycle.py`.

Then in the ITERATE block, replace:

```python
            if gate_result.passed:
                self.memory.set_status("passed")
                last = rec
                break
```

with:

```python
            if gate_result.passed:
                # completed-capped: the deliverable is verified but the broad
                # gate is red for reasons outside this task. A distinct
                # terminal status so the fleet stops reporting verified work
                # as a failure — and stops burning iterations on it.
                self.memory.set_status("completed-capped" if capped else "passed")
                last = rec
                break
```

`setpoint/__main__.py` `run_loop` — build and pass the scoped gate next to the gate (after the `build_gate` call added in Task 3):

```python
    from setpoint.gates import build_scoped_gate
    scoped_gate = build_scoped_gate(spec, env=gate_env)
```

and pass it into the `Cycle(...)` construction: `scoped_gate=scoped_gate,`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_cycle.py tests/test_spec.py -q && python -m pytest -q`
Expected: PASS, full suite green.

- [ ] **Step 5: Commit**

```bash
git add setpoint/spec.py setpoint/cycle.py setpoint/gates/__init__.py setpoint/__main__.py tests/test_cycle.py tests/test_spec.py
git commit -m "Add a scoped gate and the completed-capped status"
```

---

### Task 5: Copy the repo's own checks into fleet plans; warn on single-engine (retro items 5 and 8, plan half)

A member PR passed its declared gate while the repo's `bar` check was red — only a reviewer's initiative caught it. `fleet plan` should detect the repo's real check command and wire it in as the broad gate, with the model's per-task command as the scoped gate. It should also refuse to silently produce a fleet that cannot cross-review.

**Files:**
- Modify: `setpoint/decompose.py` (`_member_spec`, `decompose`)
- Modify: `setpoint/__main__.py:179-218` (`fleet plan` argument handling)
- Test: `tests/test_decompose.py`

**Interfaces:**
- Consumes: `VerifyCfg.scoped_command` from Task 4.
- Produces: `detect_repo_checks(repo: Path) -> str | None`; `decompose(idea_path, repo, engines, out_dir, oneshot=None, repo_checks=None)`. When a checks command is known, each member spec gets `verify.command = <repo checks>` and `verify.scoped_command = <task's verify_command>`; when it is not, `verify.command` keeps the task's command as before and `scoped_command` is absent.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_decompose.py`:

```python
def test_detect_repo_checks_prefers_bar_then_ci(tmp_path):
    import json
    from setpoint.decompose import detect_repo_checks
    (tmp_path / "pnpm-lock.yaml").write_text("")
    (tmp_path / "package.json").write_text(json.dumps(
        {"scripts": {"test": "vitest", "ci": "turbo ci", "bar": "turbo bar"}}))
    assert detect_repo_checks(tmp_path) == "pnpm bar"

    (tmp_path / "package.json").write_text(json.dumps(
        {"scripts": {"test": "vitest", "ci": "turbo ci"}}))
    assert detect_repo_checks(tmp_path) == "pnpm ci"


def test_detect_repo_checks_uses_the_lockfile_package_manager(tmp_path):
    import json
    from setpoint.decompose import detect_repo_checks
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"ci": "x"}}))
    assert detect_repo_checks(tmp_path) == "npm run ci"


def test_detect_repo_checks_returns_none_without_package_json(tmp_path):
    from setpoint.decompose import detect_repo_checks
    assert detect_repo_checks(tmp_path) is None


def test_decompose_wires_repo_checks_as_the_broad_gate(tmp_path):
    import yaml
    from setpoint.decompose import decompose
    idea = tmp_path / "idea.md"
    idea.write_text("build a thing")

    def fake_oneshot(engine, prompt, cwd=None):
        return ('{"tasks": [{"name": "a", "title": "A", "goal": "do a", '
                '"interfaces": "", "depends_on": [], '
                '"verify_command": "pnpm test a", "engine": "claude"}]}')

    out = tmp_path / "bundle"
    decompose(str(idea), str(tmp_path), ["claude"], str(out),
              oneshot=fake_oneshot, repo_checks="pnpm bar")
    member = yaml.safe_load((out / "a.setpoint.yaml").read_text())
    assert member["verify"]["command"] == "pnpm bar"
    assert member["verify"]["scoped_command"] == "pnpm test a"


def test_decompose_without_repo_checks_keeps_the_task_command(tmp_path):
    import yaml
    from setpoint.decompose import decompose
    idea = tmp_path / "idea.md"
    idea.write_text("build a thing")

    def fake_oneshot(engine, prompt, cwd=None):
        return ('{"tasks": [{"name": "a", "title": "A", "goal": "do a", '
                '"interfaces": "", "depends_on": [], '
                '"verify_command": "pnpm test a", "engine": "claude"}]}')

    out = tmp_path / "bundle"
    decompose(str(idea), str(tmp_path), ["claude"], str(out), oneshot=fake_oneshot)
    member = yaml.safe_load((out / "a.setpoint.yaml").read_text())
    assert member["verify"]["command"] == "pnpm test a"
    assert "scoped_command" not in member["verify"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_decompose.py -q`
Expected: FAIL — `cannot import name 'detect_repo_checks'`

- [ ] **Step 3: Implement**

Add to `setpoint/decompose.py`:

```python
# Priority order for "the check this repo actually gates PRs on". `bar` is
# the program-health convention; ci/check/verify cover the common rest. A
# member gate that skips these lets a PR pass green while the repo's own
# required check is red (retro item 5).
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
```

Change `_member_spec` to take the checks command:

```python
def _member_spec(t: dict, repo: str, repo_checks: str | None = None) -> dict:
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
                      "branch": f"setpoint/{t['name']}"},
        "execute": {"engine": t["engine"]},
        "verify": verify,
        "stop": {"max_iters": 6},
        "deliver": {"push": True, "pr": True},
    }
```

(keep the existing long comment above `deliver` verbatim).

In `decompose(...)`, add the parameter and the single-engine warning:

```python
def decompose(idea_path: str, repo: str, engines: list[str], out_dir: str,
              oneshot=None, repo_checks: str | None = None) -> Path:
```

and right after `_validate(tasks, engines)`:

```python
    if len({t["engine"] for t in tasks}) < 2:
        print("setpoint fleet plan: WARNING — this fleet uses a single engine, so "
              "no member can be cross-reviewed (maker == checker). Members will be "
              "reported 'unreviewed'. Re-run with --engines a,b to enable review.",
              file=sys.stderr)
```

with `import sys` added to the imports, and change the member-spec write to `_member_spec(t, repo, repo_checks)`.

In `setpoint/__main__.py`'s `fleet plan` branch, accept `--checks` and auto-detect otherwise. Add `"--checks"` to `known_flags`, then before the `decompose(...)` call:

```python
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
```

replacing the existing `fleet_path = decompose(idea, repo, engines, out)` line. Update the usage string in `cmd_fleet` to mention `[--checks CMD]`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_decompose.py -q && python -m pytest -q`
Expected: PASS, full suite green.

- [ ] **Step 5: Commit**

```bash
git add setpoint/decompose.py setpoint/__main__.py tests/test_decompose.py
git commit -m "Wire the repo's own checks into fleet plans"
```

---

### Task 6: Never open a second PR for a branch (retro item 6)

Every branch got two PRs: one the worker opened per the room protocol, one `deliver()` opened. `deliver()` must look first and adopt an existing open PR.

**Files:**
- Modify: `setpoint/deliver.py:97-108`
- Test: `tests/test_deliver.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `DeliverResult.actions` gains the string `"pr (existing)"` when an open PR for the head branch was found; `pr_url` is that PR's URL. No signature change.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_deliver.py`:

```python
def test_deliver_adopts_an_existing_open_pr(tmp_path):
    from setpoint.deliver import deliver
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["gh", "pr", "list"]:
            return _FakeCompleted(0, "https://github.com/x/y/pull/7\n")
        return _FakeCompleted(0, "")

    spec = _spec({"push": True, "pr": True})
    res = deliver(spec, tmp_path, _passed_state(), runner=fake_run)
    assert res.pr_url == "https://github.com/x/y/pull/7"
    assert "pr (existing)" in res.actions
    flat = [" ".join(a) for a in calls]
    assert not any(c.startswith("gh pr create") for c in flat)


def test_deliver_creates_a_pr_when_none_exists(tmp_path):
    from setpoint.deliver import deliver
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["gh", "pr", "list"]:
            return _FakeCompleted(0, "\n")  # no open PR for this head
        if argv[:3] == ["gh", "pr", "create"]:
            return _FakeCompleted(0, "https://github.com/x/y/pull/8\n")
        return _FakeCompleted(0, "")

    spec = _spec({"push": True, "pr": True})
    res = deliver(spec, tmp_path, _passed_state(), runner=fake_run)
    assert res.pr_url == "https://github.com/x/y/pull/8"
    assert "pr" in res.actions
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_deliver.py -q`
Expected: FAIL — the first test fails because `gh pr create` is still called.

- [ ] **Step 3: Implement**

Replace the `if d.get("pr", True):` block in `setpoint/deliver.py` with:

```python
    if d.get("pr", True):
        # A room worker may already have opened the PR for this branch (the
        # room protocol asks it to request review, and older skill versions
        # opened the PR too). Opening a second one for the same head is noise
        # a human then has to close — adopt the existing one instead.
        existing = _run(runner, ["gh", "pr", "list", "--head", branch,
                                 "--state", "open", "--json", "url",
                                 "--jq", ".[0].url"], cwd)
        pr_url = (existing.stdout or "").strip() or None
        if pr_url:
            actions.append("pr (existing)")
        else:
            body = (f"Autonomous setpoint run for {spec.name}.\n\n"
                     f"Goal: {spec.goal}\n\nVerifier + grader passed. Review before merge.")
            proc = _run(runner, ["gh", "pr", "create", "--base", base,
                                 "--head", branch, "--title", f"{spec.name}: {spec.goal[:60]}",
                                 "--body", body], cwd)
            pr_url = (proc.stdout or "").strip() or None
            actions.append("pr")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_deliver.py -q && python -m pytest -q`
Expected: PASS, full suite green.

- [ ] **Step 5: Commit**

```bash
git add setpoint/deliver.py tests/test_deliver.py
git commit -m "Adopt an existing open PR instead of opening a second"
```

---

### Task 7: Namespace run state per fleet (retro item 9)

Wave 2 reused a member spec name and overwrote wave 1's run state, so the viewer showed a finished fleet as 3/4. A fleet's member runs belong to that fleet.

**Files:**
- Modify: `setpoint/__main__.py` (`run_loop` accepts `runs_root`; `cmd_ls` walks fleet dirs)
- Modify: `setpoint/fleet.py` (`_run_member`, `run_fleet`, `_status_lines`, `fleet_status`)
- Test: `tests/test_fleet.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `run_loop(spec, *, fresh=False, ui=None, abort_check=None, runs_root: Path | None = None)`; `fleet_runs_root(fs, runs_root) -> Path` returning `<runs_root>/../fleets/<fleet name>/runs`. Every fleet member's `~/.setpoint` state moves under its fleet; standalone `setpoint run` is unchanged.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_fleet.py`:

```python
def test_member_run_state_is_namespaced_per_fleet(tmp_path, monkeypatch):
    import json
    from setpoint import fleet
    monkeypatch.setenv("SETPOINT_RUNS_ROOT", str(tmp_path / "runs"))

    seen = {}

    def fake_run_loop(spec, *, fresh=False, ui=None, abort_check=None, runs_root=None):
        seen[spec.name] = runs_root
        from setpoint.memory import Memory
        m = Memory(spec.name, root=runs_root)
        m.start()
        m.set_status("passed")
        return m.load()

    fleet_path = _write_two_member_fleet(tmp_path)   # existing helper in this file
    fleet.run_fleet(str(fleet_path), run_loop=fake_run_loop)

    expected = tmp_path / "fleets" / "demo" / "runs"
    assert set(seen.values()) == {expected}
    assert (expected / "api" / "state.json").exists()
    # Nothing leaked into the global runs root.
    assert not (tmp_path / "runs" / "api").exists()


def test_two_fleets_reusing_a_member_name_do_not_collide(tmp_path, monkeypatch):
    from setpoint.fleet import fleet_runs_root
    from setpoint.fleet_spec import FleetSpec
    from pathlib import Path
    runs = tmp_path / "runs"
    a = fleet_runs_root(FleetSpec(name="wave1", members=[Path("m.setpoint.yaml")]), runs)
    b = fleet_runs_root(FleetSpec(name="wave2", members=[Path("m.setpoint.yaml")]), runs)
    assert a != b
```

If `_write_two_member_fleet` does not exist in `tests/test_fleet.py`, reuse the `_write_bundle` helper from `tests/test_fleet_room.py` by copying it in (it writes members `api` and `ui`) and drop the `room` key for this non-room test.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_fleet.py -q`
Expected: FAIL — `cannot import name 'fleet_runs_root'`

- [ ] **Step 3: Implement**

`setpoint/__main__.py` — `run_loop` gains the parameter and uses it:

```python
def run_loop(spec, *, fresh: bool = False, ui=None, abort_check=None,
             runs_root: Path | None = None):
    ...
    memory = Memory(spec.name, root=runs_root or _runs_root())
```

`setpoint/fleet.py` — add:

```python
def fleet_runs_root(fs, runs_root: Path) -> Path:
    """Where this fleet's member run state lives. Run state used to be global
    per spec name, so a second wave reusing a member spec overwrote the
    first wave's state (retro item 9). Namespacing by fleet makes each wave's
    record its own."""
    return _fleet_out_dir(fs, runs_root) / "runs"
```

Thread it through `_run_member` (new keyword `runs_root=None`, passed to `run_loop(spec, fresh=fresh, ui=NullUI(), abort_check=..., runs_root=runs_root)`), and in `run_fleet` compute it once before the executor block:

```python
    member_runs_root = fleet_runs_root(fs, _runs_root())
```

passing `runs_root=member_runs_root` in the `wrapped()` call to `_run_member`.

Change `_status_lines(fs, runs_root)` so its per-member lookup uses the fleet root: replace `sp = runs_root / name / "state.json"` with `sp = fleet_runs_root(fs, runs_root) / name / "state.json"`.

`cmd_ls` in `__main__.py` — also list fleet-scoped runs:

```python
def cmd_ls() -> int:
    import json
    root = _runs_root()
    fleets_root = root.parent / "fleets"
    dirs = []
    if root.exists():
        dirs += [(None, d) for d in sorted(root.iterdir()) if d.is_dir()]
    if fleets_root.exists():
        for f in sorted(fleets_root.iterdir()):
            runs = f / "runs"
            if runs.is_dir():
                dirs += [(f.name, d) for d in sorted(runs.iterdir()) if d.is_dir()]
    if not dirs:
        print("no runs yet")
        return 0
    for fleet_name, d in dirs:
        sp = d / "state.json"
        if sp.exists():
            s = json.loads(sp.read_text())
            label = f"{fleet_name}/{s['name']}" if fleet_name else s["name"]
            print(f"{label:38} {s['status']:18} "
                  f"iters={len(s.get('iters', []))} ${s.get('spent_usd', 0):.2f}")
    return 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_fleet.py -q && python -m pytest -q`
Expected: PASS, full suite green.

- [ ] **Step 5: Commit**

```bash
git add setpoint/fleet.py setpoint/__main__.py tests/test_fleet.py
git commit -m "Namespace member run state per fleet"
```

---

### Task 8: Record elapsed time and stop pretending CLI runs cost $0.00 (retro item 11)

The spend column reads `$0.00` for every claude/codex/kimi member because those engines bill outside the process. Track wall-time and report it; show spend only where it is real.

**Files:**
- Modify: `setpoint/memory.py` (`RunState`, `Memory.start`, `Memory.set_status`)
- Modify: `setpoint/fleet.py:540-551` (`_status_lines`)
- Test: `tests/test_memory.py`, `tests/test_fleet.py`

**Interfaces:**
- Consumes: `fleet_runs_root` from Task 7.
- Produces: `RunState.started_at: float = 0.0` and `RunState.elapsed_secs: float = 0.0` (epoch seconds / duration); `_status_lines` emits columns `member | status | iters | elapsed | spend`, with spend rendered `"—"` for members whose `execute.engine` is not `deepseek`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_memory.py`:

```python
def test_start_stamps_started_at_and_set_status_records_elapsed(tmp_path):
    from setpoint.memory import Memory
    m = Memory("r", root=tmp_path)
    m.start()
    assert m.load().started_at > 0
    m.set_status("passed")
    st = m.load()
    assert st.elapsed_secs >= 0
    assert st.status == "passed"


def test_started_at_is_not_reset_by_a_resume(tmp_path):
    from setpoint.memory import Memory
    m = Memory("r", root=tmp_path)
    m.start()
    first = m.load().started_at
    m.set_status("stopped")
    Memory("r", root=tmp_path).start()
    assert Memory("r", root=tmp_path).load().started_at == first


def test_state_json_without_the_new_fields_still_loads(tmp_path):
    import json
    from setpoint.memory import Memory
    root = tmp_path / "r"
    root.mkdir()
    (root / "state.json").write_text(json.dumps(
        {"name": "r", "status": "passed", "iters": [], "spent_usd": 1.5}))
    st = Memory("r", root=tmp_path).load()
    assert st.elapsed_secs == 0.0 and st.started_at == 0.0
```

Add to `tests/test_fleet.py`:

```python
def test_status_lines_show_elapsed_and_hide_fake_spend_for_cli_engines(tmp_path, monkeypatch):
    import json
    from setpoint import fleet
    from setpoint.fleet_spec import load_fleet
    monkeypatch.setenv("SETPOINT_RUNS_ROOT", str(tmp_path / "runs"))
    fleet_path = _write_bundle(tmp_path)          # api=claude, ui=codex
    fs = load_fleet(str(fleet_path))
    runs = fleet.fleet_runs_root(fs, tmp_path / "runs")
    (runs / "api").mkdir(parents=True)
    (runs / "api" / "state.json").write_text(json.dumps(
        {"name": "api", "status": "passed", "iters": [{}], "spent_usd": 0.0,
         "elapsed_secs": 754.0}))
    text = "\n".join(fleet._status_lines(fs, tmp_path / "runs"))
    assert "12m34s" in text
    assert "—" in text            # claude/codex spend is not ours to report
    assert "$0.00" not in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_memory.py tests/test_fleet.py -q`
Expected: FAIL — `RunState` has no `started_at`.

- [ ] **Step 3: Implement**

`setpoint/memory.py` — add `import time`, extend `RunState`:

```python
@dataclass
class RunState:
    name: str
    status: str = "new"  # new | running | passed | completed-capped | stopped |
                         # budget_exhausted | gate_error
    iters: list[IterRecord] = field(default_factory=list)
    spent_usd: float = 0.0
    # Wall-time is the honest cost signal for CLI engines, which bill outside
    # this process (retro item 11). Defaulted so old state.json still loads.
    started_at: float = 0.0
    elapsed_secs: float = 0.0
```

In `load()`, add `started_at=raw.get("started_at", 0.0), elapsed_secs=raw.get("elapsed_secs", 0.0),` to the `RunState(...)` construction.

In `start()`:

```python
    def start(self) -> RunState:
        self.root.mkdir(parents=True, exist_ok=True)
        state = self.load()
        if not state.started_at:      # resume keeps the original start
            state.started_at = time.time()
        if state.status == "new":
            state.status = "running"
        self._write(state)
        return state
```

In `set_status()`:

```python
    def set_status(self, status: str) -> None:
        state = self.load()
        state.status = status
        if state.started_at:
            state.elapsed_secs = time.time() - state.started_at
        self._write(state)
```

`setpoint/fleet.py` — replace `_status_lines`:

```python
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
        # budget; claude/codex/kimi bill through their own CLIs, so a
        # "$0.00" there is a lie, not a measurement (retro item 11).
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_memory.py tests/test_fleet.py -q && python -m pytest -q`
Expected: PASS, full suite green.

- [ ] **Step 5: Commit**

```bash
git add setpoint/memory.py setpoint/fleet.py tests/test_memory.py tests/test_fleet.py
git commit -m "Report elapsed time and honest spend per member"
```

---

### Task 9: A member is not a success until its review resolves (retro items 1 and 8, runtime half)

This is the highest-priority item. Wave 1 declared a member `passed` while its cross-reviewer was mid-CHANGES on real findings. Split the states: the loop's gate passing is `gate-passed`; only a resolved approving review is a fleet-level success.

**Files:**
- Modify: `setpoint/fleet.py:114-152` (`_report_member_to_room`), `:474-530` (`_close_the_loop`), `:295-467` (`run_fleet` result mapping)
- Modify: `setpoint/__main__.py:222-225` (`fleet run` exit code)
- Test: `tests/test_fleet_room.py`

**Interfaces:**
- Consumes: `fleet_runs_root` (Task 7), `_status_lines` (Task 8).
- Produces: `review_verdict(messages: list[dict], task_id: str, reviewer: str) -> str` returning `"approved" | "changes" | "none"`; member statuses returned by `run_fleet` become `review-approved`, `changes-requested`, `unreviewed`, `gate-passed`, plus the pre-existing non-passing statuses and `completed-capped` from Task 4. `FLEET_OK: frozenset` names the statuses that make `setpoint fleet run` exit 0.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_fleet_room.py`:

```python
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

    room = ReviewingRoom()
    results = run_fleet(str(_write_bundle(tmp_path)),
                        run_loop=_passing_run_loop, room_client=room,
                        oneshot=lambda engine, prompt, cwd=None: "reviewed")
    assert results["api"] == "changes-requested"


def test_single_engine_fleet_marks_members_unreviewed(tmp_path):
    from setpoint.fleet import run_fleet
    room = FakeRoom()
    results = run_fleet(str(_write_single_engine_bundle(tmp_path)),
                        run_loop=_passing_run_loop, room_client=room,
                        oneshot=lambda engine, prompt, cwd=None: "")
    assert set(results.values()) == {"unreviewed"}
    assert any("single-engine" in b for _, _, b in room.msgs)
```

Add the two helpers to the same file:

```python
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
```

`FakeRoom.post_task` already returns ids `t1`, `t2` in declaration order, which the first test relies on.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_fleet_room.py -q`
Expected: FAIL — `cannot import name 'review_verdict'`

- [ ] **Step 3: Implement**

In `setpoint/fleet.py`, add near the top:

```python
# A member's loop passing its gate is NOT the fleet's success condition: in
# wave 1 a member was declared passed while its reviewer was mid-CHANGES on
# real findings (retro item 1). These are the fleet-level outcomes.
REVIEW_APPROVED = "review-approved"
CHANGES_REQUESTED = "changes-requested"
UNREVIEWED = "unreviewed"          # gate passed, no reviewer could run
GATE_PASSED = "gate-passed"        # gate passed, review outcome unknown
# `setpoint fleet run` exits 0 only for these. `unreviewed` is included
# because a single-engine fleet cannot review by construction — but it is
# always listed in "Needs a human" so it is never silently accepted.
FLEET_OK = frozenset({REVIEW_APPROVED, UNREVIEWED, "completed-capped"})

_APPROVED_RE = re.compile(r"^\s*APPROVED\b", re.IGNORECASE)
_CHANGES_RE = re.compile(r"^\s*CHANGES\b", re.IGNORECASE)


def review_verdict(messages: list[dict], task_id: str, reviewer: str) -> str:
    """The reviewer's final verdict on a task: "approved", "changes", or
    "none" when that reviewer never rendered one. Prefers a structured
    `verdict` field (scry room domain) and falls back to the prose
    convention the review prompt asks for. Later messages win — a reviewer
    that posts CHANGES then APPROVED has resolved the thread."""
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
```

Rewrite `_report_member_to_room` to return the fleet-level status:

```python
def _report_member_to_room(member_name: str, status: str, room_ctx: dict, room, oneshot,
                           room_lock: threading.Lock | None) -> str:
    """Post the member's outcome, run cross-review when possible, and return
    the FLEET-level status — which is not the loop's status. Returns the loop
    status unchanged for anything that did not pass its gate."""
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
        return status

    engine = room_ctx["engine"]
    reviewer_engine = next((e for e in room_ctx.get("fleet_engines", []) if e != engine), None)
    if reviewer_engine is None:
        _post("status", f"{member_name}: UNREVIEWED — fleet is single-engine, so no "
                        f"cross-review is possible (maker == checker). A human must "
                        f"review this member's diff.")
        return UNREVIEWED

    reviewer = f"{reviewer_engine}-reviewer"
    prompt = REVIEW_PROMPT.format(task_id=task_id, room_id=room_id,
                                  branch=room_ctx["branch"], repo=room_ctx["repo"],
                                  reviewer=reviewer)
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
    # reviewer posts its findings as messages, and the last one it authored
    # on this thread is the verdict that counts.
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
```

In `_run_member`, use the returned status:

```python
    if room_ctx is not None and room is not None:
        try:
            status = _report_member_to_room(spec.name, status, room_ctx, room,
                                            oneshot, room_lock)
        except Exception:
            print(f"setpoint fleet: room reporting for {spec.name} failed:\n{traceback.format_exc()}",
                  file=sys.stderr)

    return spec.name, status
```

In `_close_the_loop`, fix the reconciliation policy and the needs-human list:

```python
        final = "done" if results.get(member) in FLEET_OK else "abandoned"
```

and:

```python
    for member, st in sorted(results.items()):
        if st == UNREVIEWED:
            needs_human.append(f"member '{member}' passed its gate but was never "
                               f"cross-reviewed — review the diff yourself")
        elif st == CHANGES_REQUESTED:
            needs_human.append(f"member '{member}' has unresolved review findings — "
                               f"read the review thread before merging")
        elif st == "completed-capped":
            needs_human.append(f"member '{member}' verified its own deliverable but the "
                               f"repo-wide gate is red — confirm the red is pre-existing")
        elif st not in FLEET_OK:
            needs_human.append(f"member '{member}' ended '{st}' — read its run log "
                               f"and the transcript before trusting or discarding its work")
    if not needs_human:
        needs_human.append("nothing — every member was reviewed, approved and delivered")
```

In `setpoint/__main__.py`'s `fleet run` branch:

```python
    if sub == "run":
        results = fleet.run_fleet(args[0], fresh=("--fresh" in args))
        print(fleet.fleet_status(args[0]))
        return 0 if all(v in fleet.FLEET_OK for v in results.values()) else 2
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_fleet_room.py -q && python -m pytest -q`
Expected: PASS, full suite green. Existing tests asserting `results == {"api": "passed", ...}` under a room must be updated to the new vocabulary — that is the point of the change, not a regression.

- [ ] **Step 5: Commit**

```bash
git add setpoint/fleet.py setpoint/__main__.py tests/test_fleet_room.py
git commit -m "Gate fleet success on a resolved review"
```

---

### Task 10: Assign each task a named reviewer up front (retro item 7)

Review routing was broadcast-and-hope: a worker pinged three named agents over three messages before anyone picked its review up. The orchestrator already knows which engine will review — say so, in the room and in the worker's context.

**Files:**
- Modify: `setpoint/fleet.py:15-23` (`ROOM_CONTEXT_TEMPLATE`), `:188-260` (`_post_tasks`), `:78-93` (context injection)
- Test: `tests/test_fleet_room.py`

**Interfaces:**
- Consumes: `_report_member_to_room` / `review_verdict` from Task 9.
- Produces: each member's `room_ctx` gains `"reviewer"` (e.g. `"codex-reviewer"`, or `""` for a single-engine fleet); the ROOM CONTEXT block names it; `_post_tasks` posts one `status` per task announcing the assignment.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_fleet_room.py`:

```python
def test_each_task_gets_a_named_reviewer_announced_in_room(tmp_path):
    from setpoint.fleet import run_fleet
    room = FakeRoom()
    run_fleet(str(_write_bundle(tmp_path)), run_loop=_passing_run_loop,
              room_client=room, oneshot=lambda engine, prompt, cwd=None: "")
    bodies = [b for _, _, b in room.msgs]
    assert any("reviewer for API is codex-reviewer" in b for b in bodies)
    assert any("reviewer for UI is claude-reviewer" in b for b in bodies)


def test_room_context_names_the_reviewer(tmp_path):
    from setpoint.fleet import run_fleet
    goals = {}

    def capture(spec, *, fresh=False, ui=None, abort_check=None, runs_root=None):
        from types import SimpleNamespace
        goals[spec.name] = spec.goal
        return SimpleNamespace(status="passed")

    run_fleet(str(_write_bundle(tmp_path)), run_loop=capture,
              room_client=FakeRoom(), oneshot=lambda e, p, cwd=None: "")
    assert "codex-reviewer" in goals["api"]
    assert "do not broadcast" in goals["api"].lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_fleet_room.py -q`
Expected: FAIL — no "reviewer for API is codex-reviewer" message exists.

- [ ] **Step 3: Implement**

Replace `ROOM_CONTEXT_TEMPLATE` in `setpoint/fleet.py`:

```python
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
not ping other agents hoping someone picks it up."""
```

In `_run_member`'s context injection, pass the new field:

```python
        block = ROOM_CONTEXT_TEMPLATE.format(room_id=room_ctx["room_id"],
                                             task_id=room_ctx["task_id"],
                                             agent=room_ctx["agent"],
                                             reviewer=room_ctx.get("reviewer")
                                                      or "none (single-engine fleet)")
```

In `_post_tasks`, after the per-member loop has built `member_room_ctx` and `fleet_engines` (i.e. where `ctx["fleet_engines"] = fleet_engines` is set today), assign and announce reviewers:

```python
    for name, ctx in member_room_ctx.items():
        ctx["fleet_engines"] = fleet_engines
        # Assign the reviewer at plan time, not at review time: a worker that
        # has to find its own reviewer broadcasts and waits (retro item 7).
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
```

Delete the old trailing `for ctx in member_room_ctx.values(): ctx["fleet_engines"] = fleet_engines` loop, which the first loop above replaces.

In `_report_member_to_room`, use the assigned reviewer instead of recomputing it:

```python
    reviewer = room_ctx.get("reviewer") or ""
    if not reviewer:
        _post("status", f"{member_name}: UNREVIEWED — fleet is single-engine, so no "
                        f"cross-review is possible (maker == checker). A human must "
                        f"review this member's diff.")
        return UNREVIEWED
    reviewer_engine = reviewer.rsplit("-reviewer", 1)[0]
```

(replacing the `reviewer_engine = next(...)` / `if reviewer_engine is None:` / `reviewer = f"..."` lines).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_fleet_room.py -q && python -m pytest -q`
Expected: PASS, full suite green.

- [ ] **Step 5: Commit**

```bash
git add setpoint/fleet.py tests/test_fleet_room.py
git commit -m "Assign a named reviewer per task at launch"
```

---

### Task 11: Mark the retro items done

**Files:**
- Modify: `docs/retros/2026-08-13-first-fleets.md`

- [ ] **Step 1: Annotate the retro**

For each of items 1–11, append ` — FIXED (<commit subject>)` to the item's first line. Leave items 12–19 untouched; they belong to the scry and room-worker plans.

- [ ] **Step 2: Run the full suite one more time**

Run: `python -m pytest -q`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add docs/retros/2026-08-13-first-fleets.md
git commit -m "Mark setpoint retro items fixed"
```
