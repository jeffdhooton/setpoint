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
