# Fleet Rooms Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `setpoint fleet plan idea.md` decomposes an idea into per-task member specs + a fleet file; `setpoint fleet run` then executes the members through the existing pool while coordinating them through a scry room (task board + message channel) that Claude/Codex/Kimi workers reach via their own scry MCP tools.

**Architecture:** Four additions composed onto existing machinery, none of which changes the core loop: (1) a `KimiExecutor` following the exact `AgentCLIExecutor` pattern; (2) `setpoint/room.py`, a `RoomClient` speaking MCP stdio JSON-RPC to a `scry mcp` subprocess (orchestrator-side only — workers use their own MCP config); (3) `setpoint/decompose.py` + `fleet plan` CLI, one-shot agent call → tasks JSON → generated member specs + fleet.yaml with a `room:` section; (4) room-mode in `run_fleet`: create room, post tasks, inject a room-context block into each member spec's notes before `run_loop`, dispatch a one-shot cross-engine reviewer after each member passes, close room, write a report with the channel transcript.

**Tech Stack:** Python 3.11+, stdlib only (subprocess, json, yaml already a dep). pytest with the repo's established injection seams (`runner=`, `run_loop=`).

**Deviations from the parent fleet spec (deliberate, v1):** Gate 1 (plan approval) = the human inspects the generated plan directory and *chooses* to run `setpoint fleet run` — no interactive prompt. Cross-review is one round: a different-engine one-shot reviews the passed member's branch and posts findings in-thread; findings surface in the fleet report and at PR review rather than resuming the worker.

## Global Constraints

- Work on branch `fleet-rooms` (controller creates it). Never commit to `main`.
- No new dependencies; engine binaries (`claude`, `codex`, `kimi`, `scry`) are invoked by bare name via PATH, matching `agent_cli.py`.
- The global commit-msg hook requires a Capitalized subject ≤50 chars; the commit messages below comply — do not lengthen. Every body ends with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
- Follow repo idioms: `from __future__ import annotations`, lazy imports in `__main__.py` subcommands, injection seams for tests instead of mocking modules.
- Room MCP tool names (server side, already live): `scry_room_create`, `scry_room_close`, `scry_task_post`, `scry_task_claim`, `scry_task_update`, `scry_task_list`, `scry_post`, `scry_read`. Message kinds: `status|handoff|contract|review`. Task statuses: `open|claimed|in_progress|review|done|abandoned`.
- Full verify per task: `cd ~/workspace/setpoint && GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/dev/null .venv/bin/python -m pytest tests/ -q` — the env vars neutralize the machine's global commit-msg hook, which otherwise fails the 26 tests that commit in temp repos (baseline: 277 passed). Use `.venv/bin/python` for every pytest run in this plan.
- Workers deliver PR-only through existing `deliver.py` machinery — nothing in this plan may add merge capability.

---

### Task 1: Kimi executor

**Files:**
- Modify: `setpoint/executor/agent_cli.py` (append)
- Modify: `setpoint/spec.py` (`VALID_ENGINES`)
- Modify: `setpoint/__main__.py` (`_build_executor`)
- Test: `tests/test_kimi_executor.py` (create)

**Interfaces:**
- Consumes: `AgentCLIExecutor(argv_fn, parse_fn, timeout, runner)` from agent_cli.py:13-64; `VALID_ENGINES` at spec.py:11; `_build_executor(spec)` at `__main__.py:14-26`.
- Produces: `KimiExecutor`, `_kimi_argv`, `_kimi_parse`; `"kimi"` accepted as `execute.engine`.

- [ ] **Step 1: Write the failing tests**

```python
from __future__ import annotations

from pathlib import Path

from setpoint.executor.agent_cli import KimiExecutor, _kimi_argv, _kimi_parse


def test_kimi_argv_shape():
    argv = _kimi_argv("do the thing", Path("/tmp"), "kimi")
    assert argv[0] == "kimi"
    assert argv[1:3] == ["-p", "do the thing"]
    assert "--auto" in argv
    assert "-m" not in argv  # default model alias omitted


def test_kimi_argv_model_override():
    argv = _kimi_argv("x", Path("/tmp"), "kimi-k3")
    i = argv.index("-m")
    assert argv[i + 1] == "kimi-k3"


def test_kimi_parse_plain_text():
    text, usage = _kimi_parse("did the work\n")
    assert text == "did the work"
    assert usage.input_tokens == 0 and usage.output_tokens == 0


class _FakeProc:
    returncode = 0
    stdout = "done\n"
    stderr = ""


def test_kimi_executor_runs_binary(tmp_path):
    calls = {}

    def fake_run(argv, **kw):
        calls["argv"] = argv
        calls["cwd"] = kw.get("cwd")
        return _FakeProc()

    ex = KimiExecutor(runner=fake_run)
    result = ex.execute("sys", "task", tools=[], model="kimi",
                        cwd=tmp_path, on_event=lambda e: None)
    assert result.text == "done"
    assert calls["argv"][0] == "kimi"
    assert calls["cwd"] == tmp_path


def test_spec_accepts_kimi_engine():
    from setpoint.spec import VALID_ENGINES
    assert "kimi" in VALID_ENGINES
```

- [ ] **Step 2: Run to verify failure**

Run: `cd ~/workspace/setpoint && .venv/bin/python -m pytest tests/test_kimi_executor.py -q`
Expected: ImportError (KimiExecutor undefined).

- [ ] **Step 3: Implement**

Append to `setpoint/executor/agent_cli.py`:

```python
def _kimi_argv(prompt: str, cwd: Path, model: str) -> list[str]:
    # --auto: fully autonomous prompt mode (kimi's analog of acceptEdits).
    # Text output: kimi's stream-json event shape is undocumented, and the
    # gate — not the transcript — decides success, so raw text is enough.
    argv = ["kimi", "-p", prompt, "--output-format", "text", "--auto"]
    if model and model != "kimi":
        argv += ["-m", model]
    return argv


def _kimi_parse(stdout: str) -> tuple[str, Usage]:
    return stdout.strip(), Usage()


class KimiExecutor(AgentCLIExecutor):
    def __init__(self, timeout: int = 1800, runner=subprocess.run):
        super().__init__(_kimi_argv, _kimi_parse, timeout=timeout, runner=runner)
```

In `setpoint/spec.py:11` add `"kimi"` to `VALID_ENGINES`. In `setpoint/__main__.py`'s `_build_executor`, add a branch mirroring claude/codex:

```python
    if spec.execute.engine == "kimi":
        from setpoint.executor.agent_cli import KimiExecutor
        return KimiExecutor()
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_kimi_executor.py -q` → 5 passed; then `python -m pytest tests/ -q` → all green (existing engine-validation tests must not have hardcoded the old set — if one asserts the exact VALID_ENGINES contents, update that assertion to include kimi and say so in your report).

- [ ] **Step 5: Commit**

```bash
git add setpoint/executor/agent_cli.py setpoint/spec.py setpoint/__main__.py tests/test_kimi_executor.py
git commit -m "Add Kimi Code executor engine

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: RoomClient — orchestrator's scry room access

**Files:**
- Create: `setpoint/room.py`
- Create: `tests/fake_room_mcp.py` (a fake `scry mcp` stdio server for tests)
- Test: `tests/test_room_client.py`

**Interfaces:**
- Produces (used by Tasks 4-5):
  - `class RoomError(RuntimeError)`
  - `class RoomClient` with `__init__(self, argv: list[str] | None = None)` (default `["scry", "mcp"]`), context-manager support, and methods:
    - `create_room(run_id: str, repo: str) -> dict`
    - `close_room(room_id: str) -> dict`
    - `post_task(room_id: str, title: str, body: str = "", depends_on: list[str] | None = None, interfaces: str = "") -> dict`
    - `list_tasks(room_id: str) -> list[dict]`
    - `post(room_id: str, from_: str, kind: str, body: str, task_id: str = "") -> dict`
    - `read(room_id: str, cursor: int = 0, limit: int = 50) -> dict`  (returns `{"messages": [...], "cursor": n}`)
    - `close() -> None` (terminates the subprocess; idempotent)

- [ ] **Step 1: Write the fake MCP server**

`tests/fake_room_mcp.py` — an executable stdio program with an in-memory room store, close enough to scry for client testing:

```python
"""Fake `scry mcp` stdio server: line-delimited JSON-RPC, in-memory rooms.

Run as: python tests/fake_room_mcp.py
Supports initialize, notifications/initialized, tools/call for the 8 room
tools. Tool results mirror scry's: result.content[0].text is a JSON blob,
result.isError True with a message text on failure.
"""
from __future__ import annotations

import json
import sys

rooms: dict[str, dict] = {}
tasks: dict[tuple[str, str], dict] = {}
msgs: dict[str, list[dict]] = {}
counter = {"n": 0}


def _id() -> str:
    counter["n"] += 1
    return f"id{counter['n']}"


def tool_result(rid, payload, is_error=False):
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return {"jsonrpc": "2.0", "id": rid,
            "result": {"content": [{"type": "text", "text": body}],
                       "isError": is_error}}


def handle(name, a, rid):
    if name == "scry_room_create":
        room = {"id": _id(), "run_id": a["run_id"], "repo": a.get("repo", ""),
                "status": "open"}
        rooms[room["id"]] = room
        msgs[room["id"]] = []
        return tool_result(rid, room)
    room = rooms.get(a.get("room_id", ""))
    if room is None:
        return tool_result(rid, f'room {a.get("room_id")!r} not found', True)
    if name == "scry_room_close":
        room["status"] = "closed"
        return tool_result(rid, room)
    if name == "scry_task_post":
        t = {"id": _id(), "room_id": room["id"], "title": a["title"],
             "body": a.get("body", ""), "depends_on": a.get("depends_on", []),
             "interfaces": a.get("interfaces", ""), "status": "open"}
        tasks[(room["id"], t["id"])] = t
        return tool_result(rid, t)
    if name == "scry_task_list":
        return tool_result(rid, [t for (r, _), t in sorted(tasks.items()) if r == room["id"]])
    if name == "scry_post":
        m = {"seq": len(msgs[room["id"]]) + 1, "room_id": room["id"],
             "task_id": a.get("task_id", ""), "from": a["from"],
             "kind": a["kind"], "body": a["body"]}
        msgs[room["id"]].append(m)
        return tool_result(rid, m)
    if name == "scry_read":
        cur = int(a.get("cursor", 0))
        out = [m for m in msgs[room["id"]] if m["seq"] > cur][: int(a.get("limit", 50))]
        return tool_result(rid, {"messages": out,
                                 "cursor": out[-1]["seq"] if out else cur})
    return tool_result(rid, f"unknown tool {name}", True)


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    method, rid = req.get("method"), req.get("id")
    if method == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": rid,
                          "result": {"protocolVersion": "2024-11-05",
                                     "capabilities": {}, "serverInfo": {"name": "fake"}}}),
              flush=True)
    elif method == "tools/call":
        p = req["params"]
        print(json.dumps(handle(p["name"], p.get("arguments", {}), rid)), flush=True)
    # notifications: no response
```

- [ ] **Step 2: Write the failing client tests**

```python
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from setpoint.room import RoomClient, RoomError

FAKE = [sys.executable, str(Path(__file__).parent / "fake_room_mcp.py")]


@pytest.fixture()
def client():
    with RoomClient(argv=FAKE) as c:
        yield c


def test_room_roundtrip(client):
    room = client.create_room("run-1", "/repo")
    assert room["status"] == "open" and room["id"]

    t1 = client.post_task(room["id"], "build API", interfaces="GET /leads")
    t2 = client.post_task(room["id"], "build UI", depends_on=[t1["id"]])
    board = client.list_tasks(room["id"])
    assert [t["title"] for t in board] == ["build API", "build UI"]
    assert board[1]["depends_on"] == [t1["id"]]

    client.post(room["id"], from_="orchestrator", kind="status", body="launched")
    read = client.read(room["id"], cursor=0)
    assert read["cursor"] == 1 and read["messages"][0]["kind"] == "status"
    # incremental
    read2 = client.read(room["id"], cursor=read["cursor"])
    assert read2["messages"] == [] and read2["cursor"] == 1

    closed = client.close_room(room["id"])
    assert closed["status"] == "closed"


def test_tool_error_raises(client):
    with pytest.raises(RoomError, match="not found"):
        client.list_tasks("nope")


def test_close_idempotent():
    c = RoomClient(argv=FAKE)
    c.create_room("r", "/x")
    c.close()
    c.close()  # second close must not raise
```

- [ ] **Step 3: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_room_client.py -q`
Expected: ImportError (setpoint.room does not exist).

- [ ] **Step 4: Implement setpoint/room.py**

```python
"""Orchestrator-side client for scry fleet rooms.

Speaks MCP stdio JSON-RPC to a `scry mcp` subprocess. This is only for the
fleet orchestrator (create room, post tasks, watch the board, close room);
workers talk to the room through their own engine's scry MCP configuration.
"""
from __future__ import annotations

import json
import subprocess


class RoomError(RuntimeError):
    pass


class RoomClient:
    def __init__(self, argv: list[str] | None = None):
        self.argv = argv or ["scry", "mcp"]
        self._proc: subprocess.Popen | None = None
        self._next_id = 0

    # -- lifecycle ---------------------------------------------------------

    def _ensure(self) -> subprocess.Popen:
        if self._proc is None or self._proc.poll() is not None:
            self._proc = subprocess.Popen(
                self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True)
            self._rpc("initialize", {"protocolVersion": "2024-11-05",
                                     "capabilities": {},
                                     "clientInfo": {"name": "setpoint", "version": "1"}})
            self._notify("notifications/initialized")
        return self._proc

    def close(self) -> None:
        if self._proc is not None:
            try:
                self._proc.stdin.close()
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                pass
            self._proc = None

    def __enter__(self) -> "RoomClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- transport ---------------------------------------------------------

    def _send(self, obj: dict) -> None:
        proc = self._proc
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def _notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def _rpc(self, method: str, params: dict) -> dict:
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": self._next_id,
                    "method": method, "params": params})
        line = self._proc.stdout.readline()
        if not line:
            raise RoomError(f"scry mcp exited during {method}")
        resp = json.loads(line)
        if "error" in resp:
            raise RoomError(f"{method}: {resp['error']}")
        return resp["result"]

    def _tool(self, name: str, args: dict) -> dict | list:
        self._ensure()
        result = self._rpc("tools/call", {"name": name, "arguments": args})
        text = result["content"][0]["text"]
        if result.get("isError"):
            raise RoomError(f"{name}: {text}")
        return json.loads(text)

    # -- room API ----------------------------------------------------------

    def create_room(self, run_id: str, repo: str) -> dict:
        return self._tool("scry_room_create", {"run_id": run_id, "repo": repo})

    def close_room(self, room_id: str) -> dict:
        return self._tool("scry_room_close", {"room_id": room_id})

    def post_task(self, room_id: str, title: str, body: str = "",
                  depends_on: list[str] | None = None, interfaces: str = "") -> dict:
        args: dict = {"room_id": room_id, "title": title}
        if body:
            args["body"] = body
        if depends_on:
            args["depends_on"] = depends_on
        if interfaces:
            args["interfaces"] = interfaces
        return self._tool("scry_task_post", args)

    def list_tasks(self, room_id: str) -> list[dict]:
        return self._tool("scry_task_list", {"room_id": room_id})

    def post(self, room_id: str, from_: str, kind: str, body: str,
             task_id: str = "") -> dict:
        args = {"room_id": room_id, "from": from_, "kind": kind, "body": body}
        if task_id:
            args["task_id"] = task_id
        return self._tool("scry_post", args)

    def read(self, room_id: str, cursor: int = 0, limit: int = 50) -> dict:
        return self._tool("scry_read", {"room_id": room_id, "cursor": cursor,
                                        "limit": limit})
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest tests/test_room_client.py -q` → 3 passed; full suite green.

- [ ] **Step 6: Commit**

```bash
git add setpoint/room.py tests/fake_room_mcp.py tests/test_room_client.py
git commit -m "Add RoomClient for scry fleet rooms

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Decompose — `setpoint fleet plan`

**Files:**
- Create: `setpoint/decompose.py`
- Modify: `setpoint/__main__.py` (`cmd_fleet` gains a `plan` subcommand)
- Test: `tests/test_decompose.py`

**Interfaces:**
- Consumes: `_claude_argv`/`_codex_argv`/`_kimi_argv` + parse fns from agent_cli.py (one-shot reuse); yaml (already a dep via spec loading).
- Produces:
  - `decompose(idea_path: str, repo: str, engines: list[str], out_dir: str, oneshot=None) -> Path` — writes `<out>/plan.md`, `<out>/tasks.json`, one `<out>/<task-name>.setpoint.yaml` per task, `<out>/fleet.yaml`; returns the fleet.yaml path. `oneshot(engine, prompt) -> str` is the injection seam (default shells the engine CLI).
  - fleet.yaml shape consumed by Task 4:

```yaml
name: <fleet name>            # slug of the idea filename
concurrency: 3
members:
  - ./build-api.setpoint.yaml
  - ./build-ui.setpoint.yaml
room:
  repo: /abs/path/to/repo
  tasks:
    - member: build-api       # member spec name (matches spec `name:`)
      title: Build the API
      interfaces: "GET /leads -> {id,name}[]"
      depends_on: []          # member names, resolved to room task ids at run time
    - member: build-ui
      title: Build the UI
      interfaces: ""
      depends_on: [build-api]
```

- [ ] **Step 1: Write the failing tests**

```python
from __future__ import annotations

import json
from pathlib import Path

import yaml

from setpoint.decompose import decompose

CANNED = json.dumps({
    "tasks": [
        {"name": "build-api", "title": "Build the API",
         "goal": "Implement GET /leads returning JSON",
         "interfaces": "GET /leads -> {id,name}[]", "depends_on": [],
         "verify_command": "pytest tests/api -q", "engine": "claude"},
        {"name": "build-ui", "title": "Build the UI",
         "goal": "Render the leads list",
         "interfaces": "", "depends_on": ["build-api"],
         "verify_command": "pytest tests/ui -q", "engine": "codex"},
    ]
})


def fake_oneshot(engine: str, prompt: str) -> str:
    assert "Implement lead tracking" in prompt  # idea text reaches the model
    return f"chatter before\n```json\n{CANNED}\n```\nchatter after"


def test_decompose_writes_bundle(tmp_path):
    idea = tmp_path / "lead-tracking.md"
    idea.write_text("Implement lead tracking end to end")
    repo = tmp_path / "repo"
    repo.mkdir()

    fleet_yaml = decompose(str(idea), str(repo), ["claude", "codex", "kimi"],
                           str(tmp_path / "out"), oneshot=fake_oneshot)

    out = fleet_yaml.parent
    assert (out / "plan.md").exists()
    tasks = json.loads((out / "tasks.json").read_text())
    assert [t["name"] for t in tasks["tasks"]] == ["build-api", "build-ui"]

    fleet = yaml.safe_load(fleet_yaml.read_text())
    assert fleet["room"]["repo"] == str(repo)
    assert fleet["room"]["tasks"][1]["depends_on"] == ["build-api"]
    assert len(fleet["members"]) == 2

    spec = yaml.safe_load((out / "build-api.setpoint.yaml").read_text())
    assert spec["name"] == "build-api"
    assert spec["type"] == "coding"
    assert spec["goal"].startswith("Implement GET /leads")
    assert spec["workspace"] == {"repo": str(repo), "worktree": True,
                                 "branch": "setpoint/build-api"}
    assert spec["execute"]["engine"] == "claude"
    assert spec["verify"] == {"gate": "command", "command": "pytest tests/api -q"}
    assert spec["deliver"] == {}


def test_decompose_rejects_bad_engine(tmp_path):
    idea = tmp_path / "i.md"
    idea.write_text("x")
    (tmp_path / "repo").mkdir()

    def bad(engine, prompt):
        return json.dumps({"tasks": [{"name": "a", "title": "A", "goal": "g",
                                      "interfaces": "", "depends_on": [],
                                      "verify_command": "true",
                                      "engine": "gemini"}]})

    import pytest
    with pytest.raises(ValueError, match="engine"):
        decompose(str(idea), str(tmp_path / "repo"), ["claude"],
                  str(tmp_path / "out"), oneshot=bad)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_decompose.py -q`
Expected: ImportError (setpoint.decompose missing).

- [ ] **Step 3: Implement setpoint/decompose.py**

```python
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


def _default_oneshot(engine: str, prompt: str) -> str:
    from setpoint.executor.agent_cli import (_claude_argv, _claude_parse,
                                             _codex_argv, _codex_parse,
                                             _kimi_argv, _kimi_parse)
    table = {"claude": (_claude_argv, _claude_parse),
             "codex": (_codex_argv, _codex_parse),
             "kimi": (_kimi_argv, _kimi_parse)}
    argv_fn, parse_fn = table[engine]
    proc = subprocess.run(argv_fn(prompt, Path.cwd(), engine),
                          capture_output=True, text=True, timeout=600,
                          stdin=subprocess.DEVNULL)
    text, _ = parse_fn(proc.stdout or "")
    return text


def _extract_json(text: str) -> dict:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text[text.find("{"): text.rfind("}") + 1]
    return json.loads(candidate)


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
        "deliver": {},
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
                       "interfaces": t.get("interfaces", ""),
                       "depends_on": t.get("depends_on", [])} for t in tasks],
        },
    }
    fleet_path = out / "fleet.yaml"
    fleet_path.write_text(yaml.safe_dump(fleet, sort_keys=False))
    return fleet_path
```

- [ ] **Step 4: Wire the CLI**

In `setpoint/__main__.py`, extend `cmd_fleet` (which currently handles `run|status|stop`) with a `plan` branch, following the file's lazy-import style:

```python
    if sub == "plan":
        # setpoint fleet plan <idea.md> --repo <path> --engines a,b,c [--out DIR]
        from setpoint.decompose import decompose
        idea = rest[1]
        opts = rest[2:]
        def _opt(flag, default=None):
            return opts[opts.index(flag) + 1] if flag in opts else default
        repo = _opt("--repo")
        if not repo:
            print("setpoint fleet plan: --repo is required", file=sys.stderr)
            return 2
        engines = (_opt("--engines") or "claude").split(",")
        out = _opt("--out") or f"fleets/{Path(idea).stem}"
        fleet_path = decompose(idea, repo, engines, out)
        print(f"fleet bundle written to {fleet_path.parent}")
        print(f"review plan.md, then: setpoint fleet run {fleet_path}")
        return 0
```

(Adapt to `cmd_fleet`'s actual argument variable names; keep its existing `run|status|stop` branches untouched.)

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest tests/test_decompose.py -q` → 2 passed; full suite green. Also sanity-check the generated member spec loads: add to the first test:

```python
    from setpoint.spec import load_spec
    loaded = load_spec(str(out / "build-api.setpoint.yaml"))
    assert loaded.execute.engine == "claude"
```

(If `load_spec` rejects the generated dict for a missing/extra field, fix `_member_spec` to satisfy it — the generated specs MUST load with the real loader.)

- [ ] **Step 6: Commit**

```bash
git add setpoint/decompose.py setpoint/__main__.py tests/test_decompose.py
git commit -m "Add fleet plan decomposition command

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Room-mode fleet runs

**Files:**
- Modify: `setpoint/fleet_spec.py` (parse `room:` section)
- Modify: `setpoint/fleet.py` (room orchestration around the existing pool)
- Test: `tests/test_fleet_room.py`

**Interfaces:**
- Consumes: `RoomClient` (Task 2 signatures), `FleetSpec`/`load_fleet` (fleet_spec.py:16-36), `run_fleet(fleet_path, *, fresh, run_loop)` (fleet.py:64-137) and its `run_loop=` test seam.
- Produces:
  - `FleetSpec.room: dict | None` (parsed verbatim from yaml; `None` when absent → behavior identical to today).
  - `run_fleet(..., room_client=None, oneshot=None)` new kwargs (both injection seams; defaults build a real `RoomClient` / engine one-shot only when the fleet has a `room:` section).
  - Room context injected into each member spec's `context.notes` before `run_loop` (see block below).
  - Fleet report at `~/.setpoint/fleets/<name>/report.md` (results table + full channel transcript).

**Behavior contract:**

1. If `fs.room` is None: `run_fleet` behaves byte-for-byte as before (existing tests in `tests/test_fleet.py` must pass unchanged).
2. Room mode, before submitting members: `create_room(run_id=fs.name, repo=fs.room["repo"])`; post each `room.tasks` entry in order, resolving `depends_on` member-names to the room task ids of already-posted entries; keep `member_name -> task` mapping; post one `status` message from `"orchestrator"`: `"fleet <name> launched: N tasks"`.
3. Each member's loaded spec gets this block appended to `spec.context.notes` (a `list[str]` — confirm the field type in spec.py and append accordingly) before `run_loop`:

```
ROOM CONTEXT — you are a fleet worker.
room_id: <room id>
task_id: <this member's room task id>
agent: <engine>-<member name>
Before writing any code, invoke your `room-worker` skill and follow it exactly:
claim your task, read the channel from cursor 0, negotiate any interface
contract before building the boundary, post status/handoff messages, request
review when your gate passes, and mark your task done or abandoned. All room
access is through your scry_* MCP tools.
```

4. After a member's `run_loop` returns: post `status` from `"orchestrator"` with the member name and status. If status == `"passed"`, dispatch ONE cross-review one-shot: pick the first engine in the fleet's engine set that differs from the member's engine (fall back to skipping review, with an orchestrator `status` note, when the fleet is single-engine); the one-shot prompt tells the reviewer to use its scry room tools to read thread `task_id` in `room_id`, review branch `setpoint/<member>` in `room.repo`, post findings in-thread as `kind: "review"` messages, ending with a verdict message starting `APPROVED` or `CHANGES`. The one-shot uses the same `oneshot(engine, prompt)` seam as decompose.
5. After all members: write `~/.setpoint/fleets/<name>/report.md` — the existing status table (reuse/extend `fleet_status` formatting) plus a `## Room transcript` section listing every channel message (`read` with cursor 0, limit 1000; paginate with the returned cursor until a read returns no messages) as `- [<kind>] <from> (task <task_id>): <body>`; then `close_room`; then `room_client.close()`. Room teardown runs in a `finally` so a member crash still closes the room and writes the report with whatever transcript exists.

- [ ] **Step 1: Write the failing tests**

```python
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
```

Note on `SETPOINT_RUNS_ROOT`: fleet.py reads `_runs_root()` from `__main__.py` (`__main__.py:10-11`); confirm the env var is read at call time — if it is captured at import, monkeypatch `fleet._runs_root` instead, matching `tests/test_fleet.py`'s existing approach.

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_fleet_room.py -q`
Expected: TypeError (`run_fleet` has no `room_client` kwarg).

- [ ] **Step 3: Implement**

`setpoint/fleet_spec.py`: parse an optional `room:` mapping into `FleetSpec.room: dict | None = None` (default None; no validation beyond requiring `repo` and `tasks` keys when present).

`setpoint/fleet.py`: room-mode orchestration per the Behavior contract above. Structure it as:

```python
def run_fleet(fleet_path: str, *, fresh: bool = False, run_loop=None,
              room_client=None, oneshot=None) -> dict[str, str]:
```

- When `fs.room` is falsy, the body is today's exactly (the new kwargs are ignored).
- Room mode wraps the existing pool: create/post before the pool starts; a `member_room_ctx: dict[member_name, dict]` carries `room_id`/`task_id`/`agent`; `_run_member` gains an optional `room_ctx` parameter — after `load_spec` succeeds it appends the ROOM CONTEXT block (verbatim from the Behavior contract, formatted with the ctx values) to `spec.context.notes`, and after `run_loop` returns it posts the orchestrator status message and (on "passed") dispatches the cross-review one-shot inside the worker thread (so reviews parallelize with remaining members).
- Engine lookup for cross-review: collect each member's engine at task-posting time (`load_spec(member).execute.engine`); reviewer = first engine among the fleet's members differing from the author's, else skip with an orchestrator note.
- Cross-review prompt (module constant):

```python
REVIEW_PROMPT = """You are the cross-engine reviewer for a fleet task.
Using your scry room MCP tools: read the channel thread for task {task_id}
in room {room_id} (scry_read from cursor 0, filter by task_id), then review
the work on branch {branch} of repository {repo} (git diff against the
default branch). Post your findings into the thread as messages with
kind "review" and task_id {task_id}, from "{reviewer}". End with a final
review message whose body starts with APPROVED or CHANGES followed by a
one-line justification."""
```

- Report + teardown in `finally` per the contract; reuse `fleet_status`'s table rendering for the results section (call it or extract a helper — do not duplicate the formatting logic).

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_fleet_room.py tests/test_fleet.py -q` → all pass (existing fleet tests unchanged); full suite green.

- [ ] **Step 5: Commit**

```bash
git add setpoint/fleet_spec.py setpoint/fleet.py tests/test_fleet_room.py
git commit -m "Coordinate fleet members through scry rooms

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: README + fleet runbook

**Files:**
- Modify: `README.md` (new "Fleet rooms" section after the existing fleet docs; if no fleet section exists, add one)
- Create: `docs/fleet-rooms.md` (the runbook)

**Interfaces:**
- Consumes: everything above, plus the room-worker skill name (`room-worker`, distributed to Claude/Codex/Kimi via ai-sync in ~/dotfiles).

- [ ] **Step 1: Write docs/fleet-rooms.md**

```markdown
# Fleet rooms: multi-agent runs with shared coordination

A fleet room turns N setpoint loops into a coordinated team: a task board
plus a message channel served by the scry daemon, reachable by every engine
(Claude Code, Codex, Kimi Code) through its own scry MCP configuration.

## Prerequisites

- scry daemon running with the room domain (`scry doctor` shows the daemon up)
- engine CLIs on PATH: `claude`, `codex`, `kimi` (any subset works)
- each engine has scry MCP configured (ai-sync does this) and the
  `room-worker` skill installed (shared skills dir)

## 1. Plan

    setpoint fleet plan idea.md --repo ~/workspace/myapp --engines claude,codex,kimi

Writes `fleets/idea/`: `plan.md` (read this), `tasks.json`, one
`<task>.setpoint.yaml` per task, and `fleet.yaml` with the room section.

## 2. Review (this is the approval gate)

Read `plan.md`. Edit any member spec — goals, verify commands, engine
assignments, budgets. Nothing runs until you say so.

## 3. Run

    setpoint fleet run fleets/idea/fleet.yaml

What happens:
- a scry room is created (`run_id` = fleet name); every task lands on its board
- members run through the normal setpoint loop (worktree, verify gate,
  PR-only deliver), each told its `room_id`/`task_id`/`agent` identity
- workers follow the `room-worker` skill: claim, negotiate interface
  contracts in-channel before building boundaries, post status/handoffs
- when a member's gate passes, a different engine reviews its branch and
  posts findings into the task's thread
- `setpoint fleet status fleets/idea/fleet.yaml` shows live member state;
  `setpoint fleet stop` halts new members

## 4. Read the results

- PRs: one per passed member (never merged by agents)
- `~/.setpoint/fleets/<name>/report.md`: member results + the full room
  transcript (contracts negotiated, reviews, handoffs)
- the room persists in scry after close — `scry_read` works forever, and
  the history feeds scry's memory graph

## Watching live

The room IS the live view. From any Claude session:
`scry_read {room_id, cursor: 0}` — or ask an agent to summarize the channel.
```

- [ ] **Step 2: Add the README section**

Add to `README.md` (place beside the existing fleet documentation, matching its tone), a short section:

```markdown
### Fleet rooms

`setpoint fleet plan idea.md --repo <path> --engines claude,codex,kimi`
decomposes an idea into member specs; review the generated `plan.md`, then
`setpoint fleet run <bundle>/fleet.yaml` executes them as a coordinated team:
a scry-served task board + message channel where workers claim tasks,
negotiate interface contracts before building shared boundaries, and
cross-review each other's branches across engines. See
[docs/fleet-rooms.md](docs/fleet-rooms.md).
```

- [ ] **Step 3: Full suite, commit**

Run: `.venv/bin/python -m pytest tests/ -q` → green.

```bash
git add README.md docs/fleet-rooms.md
git commit -m "Document fleet rooms workflow

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
