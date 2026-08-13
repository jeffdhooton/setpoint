# setpoint

[![tests](https://github.com/jeffdhooton/setpoint/actions/workflows/tests.yml/badge.svg)](https://github.com/jeffdhooton/setpoint/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![python](https://img.shields.io/badge/python-3.11%2B-blue)

A closed-loop engine for AI agents. Given a spec file, setpoint runs
DISCOVER → PLAN → EXECUTE → VERIFY → ITERATE until the work passes a real,
deterministic gate — or hits an iteration, budget, or wall-clock limit.
Progress streams in real time and is watchable in tmux.

The thesis: loops beat one-shot prompting, but only if the verify gate is
real (an exit code, not vibes) and the loop is affordable. setpoint's default
executor drives a cheap frontier-class model (DeepSeek-v4) through its own
tool-calling loop; alternatively it shells out to the Claude Code or Codex
CLI and lets that agent own the EXECUTE stage. Either way, the gate — not
the model's self-assessment — decides when the work is done.

## Install

macOS or Linux (setpoint manages gate subprocesses with POSIX process groups;
Windows is not supported). Python 3.11+.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

## Setup

Set `DEEPSEEK_API_KEY` (only needed for the default deepseek engine — agent
engines use your `claude`/`codex` CLI login instead):

```bash
# Option A — shell export
export DEEPSEEK_API_KEY=sk-...

# Option B — .env file in the repo root (loaded automatically at startup)
cp .env.example .env
# then edit .env
```

## Quickstart

```bash
bash examples/setup.sh                 # create the sandbox: a repo with one failing test
setpoint run examples/coding.setpoint.yaml     # watch the loop fix it until pytest passes
```

## Usage

```bash
setpoint run examples/coding.setpoint.yaml          # run a coding loop
setpoint run examples/content.setpoint.yaml         # run a content loop
setpoint run examples/coding.setpoint.yaml --fresh  # discard prior state and restart
setpoint resume examples/coding.setpoint.yaml       # continue a stopped run (numbering continues)
setpoint ls                                     # list all runs with status + spend
setpoint logs <name>                            # print the markdown log for a run
setpoint fleet run examples/fleet-demo.yaml     # run several loops as a supervised fleet
setpoint fleet status examples/fleet-demo.yaml  # fleet dashboard (table + status.md)
setpoint fleet stop                             # request a graceful fleet stop
setpoint migrate <repo> [--dry-run]             # convert a repo's .loom/ specs to .setpoint/
```

### Fleet rooms

`setpoint fleet plan idea.md --repo <path> --engines claude,codex,kimi`
decomposes an idea into member specs; review the generated `plan.md`, then
`setpoint fleet run <bundle>/fleet.yaml` executes them as a coordinated team:
a scry-served task board + message channel where workers claim tasks,
negotiate interface contracts before building shared boundaries, and
cross-review each other's branches across engines. See
[docs/fleet-rooms.md](docs/fleet-rooms.md).

## Migrating from loom

This tool was previously named **loom**. The rename is the only breaking
change: the command is now `setpoint`, spec files are `*.setpoint.yaml`, spec
directories are `<repo>/.setpoint/`, and run state lives in `~/.setpoint/runs/`.

`*.loom.yaml` specs still load for **one release** — they print a deprecation
warning and will stop loading in the next minor. To convert a repo:

```bash
setpoint migrate <repo> --dry-run   # show exactly what would change
setpoint migrate <repo>             # do it
```

`migrate` renames `<repo>/.loom/` to `<repo>/.setpoint/`, renames each
`*.loom.yaml` spec, and rewrites references inside spec bodies — both fleet
member refs and `.loom/` paths to non-spec files such as rubrics. Tracked
files move with `git mv` so history is preserved; note that `git mv` **stages**
what it moves, including unrelated uncommitted edits inside the directory.
`migrate` never commits — review and commit yourself.

If anything is ambiguous (a `.setpoint/` directory already exists, a rename
would overwrite a file, or a spec nested in a subdirectory references files
this migration will not rename) it **refuses and changes nothing** rather than
half-migrating. Fix what it reports, then re-run.

## Loop specs

A loop spec (`.setpoint.yaml`) declares everything setpoint needs: the goal, the
workspace repo, which models to use for planning and execution, the verify
gate (a shell command or an LLM judge with a rubric), stop conditions, and
a budget cap.

`examples/coding.setpoint.yaml` shows a coding loop that runs pytest as its gate.
`examples/content.setpoint.yaml` shows a content loop where a judge model scores
the output against a rubric and iterates until the score meets the threshold.
`examples/agent-coding.setpoint.yaml` shows an agent-engine coding loop (see
"Engines" below).

### Command gates

A command gate is a shell command whose exit code decides the iteration:

```yaml
verify:
  gate: command
  command: "pytest -q"
  timeout_secs: 600     # optional; kills the whole process group on expiry
  preflight: true       # optional (default); run the gate once cold before iter 1
```

Gate output fed back to the model is tail-biased — test runners print
failures last, so the tail is what the next iteration needs to see. The
gate runs in its own process session with a timeout, so a hung command (or
one that leaks a background child holding the output pipe) fails the
iteration instead of hanging the loop.

**Preflight:** before iteration 1, setpoint runs the gate once, cold. A gate
whose command can't even execute (exit 126/127) or that hangs cold can
never pass, so the run aborts immediately with status `gate_error` rather
than burning `max_iters` discovering it. A normal cold failure instead
seeds the first PLAN with real feedback. The rule this enforces: **a gate
must be self-contained** — runnable from a fresh checkout with no manually
staged state.

### Engines

`execute.engine` selects who drives the EXECUTE stage: `deepseek` (default —
setpoint's own tool-calling loop against the DeepSeek API) or `claude` / `codex`
(shell out to the `claude` / `codex` CLI as a subprocess and let that agent
own its own tool use — read/write/edit/bash — inside the workspace). Agent
engines are subscription-based and **unmetered on tokens** (priced at $0 in
`budget.py`), so their `stop` conditions are `stop.max_iters` and
`stop.wall_clock_secs` rather than a USD/token budget cap.

`verify.judge_engine: claude | codex` runs the VERIFY-stage judge gate as a
**fresh, read-only** Claude/Codex agent process (`--permission-mode plan` /
`--sandbox read-only`) instead of an OpenAI-compatible chat model — a
different engine grading the work, not the model that produced it. Set
`deliver.artifact: "@diff"` (the judge gate's artifact source lives under the
spec's `deliver:` block, e.g. `gates/judge.py`) to have the grader review the
`git diff HEAD` of a coding loop rather than a single output file.

A `deliver:` block (`branch`, `push`, `pr`, `sheet_task`, `notify`) opens a
feature branch, pushes it, and opens a PR via `gh` on a **passed** run —
setpoint **never merges to `main` and never deploys**; every side-effecting
command setpoint emits is restricted to `git`/`gh`/`gog` (`gog` is a Google
Workspace CLI, invoked only if the optional `deliver.sheet_task` tracker
hook is configured), and `deliver.merge` is rejected at spec load time. On a run that does not pass, `deliver` instead
writes a local `report.md` and takes no git action.

## Local judge (content loops)

Content loops use a local LLM judge via ollama at `http://localhost:11434/v1` (OpenAI-compatible). The default judge model is `qwen3.6:27b`. To override the endpoint, set `SETPOINT_JUDGE_BASE_URL` (e.g. `export SETPOINT_JUDGE_BASE_URL=http://127.0.0.1:8000/v1` to point at OMLX instead). Thinking models (such as qwen3) are handled automatically — setpoint passes `reasoning_effort: "none"` so they return clean JSON rather than empty content. DeepSeek judge models skip ollama and reuse the main DeepSeek client.

## Security note

setpoint executes model-generated shell commands and file writes inside the
configured workspace repo. Confinement is workspace-level (the cwd is set
to the repo), not a strict sandbox. Run setpoint only on repos or worktrees you
trust — treat it the same as running an AI coding agent on your machine.

## Optional integrations

If the [scry](https://github.com/jeffdhooton/scry) code-intelligence daemon
is on your PATH, deepseek-engine loops can use it as a symbol-lookup tool
(`tools: [..., scry]`); without it, the tool degrades gracefully and the
loop falls back to plain search.

## License

MIT — see [LICENSE](LICENSE).
