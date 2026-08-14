# Retro: the first real fleets (sim-hookup waves 1-2)

Two coordinated fleets shipped program-health's sim hookup end to end
(6 member runs, ~90 room messages, 6 PRs merged, target independently
verified: provenance journey 9/9 across 8 roles). This retro mines the
room transcripts and member logs for what the *coordination system*
should learn. App bugs are out of scope.

Sources: `~/.setpoint/fleets/sim-hookup-{idea,wave2}/report.md`, member
run logs, and the operating session's own notes.

## Setpoint

1. **`passed` fires before review resolves.** **FIXED** (Gate fleet success on a resolved review). Wave 1 declared a member
   passed while its cross-reviewer was mid-CHANGES on real findings.
   Split the state: `gate-passed` vs `review-approved`; only the latter
   is a fleet-level success. (Highest priority — it makes "passed" a
   false signal.)
2. **Iteration cap burned on out-of-scope gate failures.** **FIXED** (Add a scoped gate and the completed-capped status). The sweep
   finished at iteration 2 but spent 3-6 re-running a full-journey gate
   red on pre-existing flakes, ending `stopped`. Add scoped gates per
   task + a distinct terminal status (`completed-capped`) when the
   worker's deliverable is verified but the broad gate isn't its fault.
3. **Worktrees cut from stale local refs.** **FIXED** (Cut fleet worktrees from the fetched origin base). All wave-1 worktrees started
   236 commits behind origin; a worker noticed by luck and broadcast a
   manual fix. Fleet launch must fetch and branch from `origin/<base>`.
4. **Port/stack collisions inside worktrees.** **FIXED** (Derive a port base per worktree). The sweep's first dynamic
   results measured the wrong web tree via a reused port (retracted by
   the agent itself). Derive ports per worktree; never silently reuse.
   (#156 partially fixed repo-side; setpoint should own this generally.)
5. **Declared gate ≠ repo CI.** **FIXED** (Wire the repo's own checks into fleet plans). A PR passed the member gate while the
   repo's `bar` check was red; only reviewer initiative caught it. Fleet
   plan should copy the repo's required checks into verify commands.
6. **Duplicate PRs per branch.** **FIXED** (Adopt an existing open PR, never open a second). Worker-opened PR (room protocol) +
   deliver-opened PR. Deliver must detect an existing open PR first;
   the room-worker skill should request review in-room and leave PR
   creation to deliver.
7. **Review routing is broadcast-and-hope.** **FIXED** (Assign a named reviewer per task at launch). A worker pinged three named
   agents over three messages before its review was picked up. The
   orchestrator should assign a specific different-engine reviewer.
8. **Single-engine fleets silently skip review.** **FIXED** (Gate fleet success on a resolved review). Warn at `fleet plan`
   time; at run time mark affected tasks explicitly `unreviewed` in the
   outcome rather than omitting quietly.
9. **Run state is global per spec name.** **FIXED** (Namespace member run state per fleet). Wave 2 reusing a member spec
   overwrote wave 1's run state (viewer showed a finished fleet as 3/4).
   Namespace `~/.setpoint/runs/` per fleet. Viewer works around it by
   freezing ended fleets from report.md; the store should be right.
10. **Cold-start builds.** **FIXED** (Run a prepare command once per fresh worktree). Fresh worktrees lack workspace `dist/`;
    `demo:verify` fails until `pnpm build`. Spec-level `prepare` command
    run once per worktree before the loop starts.
11. **Spend column reads $0.00 for CLI engines.** **FIXED** (Report elapsed time and honest spend). Track wall-time and
    iteration counts as the cost signal, or label the column honestly.

## Scry rooms

12. **`room.get(run_id)` / list-rooms.** **FIXED** — room.get + room.list, RPC and MCP. Bit us three separate times
    (viewer, wave relaunch, mid-run monitoring) before room.json
    manifests papered over it. The daemon should answer it natively.
13. **Message IDs are already a de-facto need.** **FIXED** — reply_to on every message. Reviewers hand-cite
    "seq 24" / "at seq 8 you accepted" — agents invented structural
    citation because `scry_post` doesn't return an addressable id they
    can reference (seq exists; make it a first-class citation surface in
    prompts and tools, incl. reply-to).
14. **Verdicts/severities/PR links are prose conventions.** **FIXED** — verdict/severity/findings/pr_url fields. APPROVED/
    CHANGES, [P0]-[P3], and deliverable URLs all live in free text; the
    closing ceremony regex-harvests PRs. Add optional structured fields
    to `review` (verdict, severity, findings[]) and `handoff`/`status`
    (pr_url) posts.
15. **`contract` kind is overloaded.** **FIXED** — publish kind added. Self-contained tasks used it as a
    one-way broadcast. Add a `publish` kind (or document the one-way
    use) so propose/accept semantics stay meaningful.
16. **Long-lived `scry mcp` processes go stale across daemon upgrades.** **FIXED** — RoomProtocolVersion + restart advice.
    The operating session's MCP predated the room domain and couldn't
    see the new tools. Version-stamp the handshake; advertise restart.
17. **Memory extraction reliability.** **FIXED** — loud dormancy, 2 repair retries, dead-letter file. Same-day: silent `dormant` (env
    key lost on daemon restart) and a fact lost to `extract: invalid
    JSON after retry`. Dormancy should be loud; extraction needs a
    second retry with repair + a dead-letter file instead of dropping.

## Protocol skill (room-worker)

18. **Iteration amnesia.** **FIXED** — room-worker orientation cache (dotfiles f07306a). Workers re-read SKILL.md and re-oriented each
    iteration; one re-hit a permission block a prior iteration had
    cleared. Carry a per-run orientation cache into the prompt.
19. **PR etiquette** **FIXED** — skill stops at review-request; deliver owns the PR (dotfiles f07306a). — see 6: skill says request review then open PR;
    with deliver in the picture the skill should stop at review-request.

## Already fixed during the runs

- Room context injected where agent engines actually see it (goal).
- Codex `--approve-for-me`; Claude `mcp__scry` allowlist.
- Port claiming in demo-stage (#156, repo-side).
- Closing ceremony: board reconciliation, FLEET CLOSED message,
  Outcome + needs-a-human in report.md.
- Fleet self-description: room.json global + bundle-local.
