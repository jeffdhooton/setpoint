# Retro: the second pair of fleets (indexing-resilience, owner-admin-config)

Two fleets, nine members, ~11,000 lines of work produced. **Nine of nine members
were reported as failures. Every one of those reports was wrong or misleading.**
The scry work was complete and green; three of five program-health members
produced substantial code. Not one member's status said so.

This retro is about why a system that did the work could not tell anyone it had.

Sources: `~/.setpoint/fleets/{indexing-resilience,owner-admin-config}/report.md`,
member run state, and the branches themselves.

## The one that caused everything else

1. **The gate contract is invisible to the worker.** **FIXED** — verify_contract_block injects both gates into the goal. Every scoped gate in both
   fleets required a named test or a phrase — `TestBuildContinuesAfterMissingIndexer`,
   `"payor rule config"` — that appears *only inside the verify command*. The agent
   never sees that command. It sees the goal, the plan, and the gate's feedback.
   So nine workers implemented the behavior correctly, named it sensibly, and were
   refused by a contract they had no way to read. `partial-degrade-build` wrote
   `build_degrade_test.go` with five tests covering the contract exactly, and was
   marked `stopped`.

   **Fix: inject the verify commands into the agent's goal.** A gate the worker
   cannot read is not a gate, it is a trap. (Highest priority — it is the whole
   retro.)

   This is the third distinct form of the same error across two nights. The first
   fleet's gates named test *files* the implementation legitimately didn't create;
   the second's named *existing suites* and were vacuous; the third's named
   *invisible strings*. The invariant to hold: **a gate must be satisfiable by an
   agent that has read only the goal.**

## The rest

2. **Gate failure can read as success.** **FIXED** — CommandGate names the failing command. `a && b | grep -q c` prints a's `ok` and
   swallows b entirely, so the feedback was literally `ok  .../internal/index
   (cached)` next to a red gate. Fixed during the run (the failing command is now
   named in feedback), but the deeper rule stands: never let a gate fail silently.

3. **Codex members produce nothing.** **FIXED** — room-worker orientation cache. Both codex members in owner-admin-config
   burned all six iterations and committed *zero* lines, re-reading the room-worker
   skill and re-orienting from scratch every iteration. The three claude members on
   the identical setup produced 957–2,951 line diffs. The orientation cache (item
   18 of the first retro) is now written; it is unverified against codex.

4. **Codex summaries are raw JSONL.** **FIXED** — _codex_parse reads nested agent_message. `state.json` stores
   `{"type":"thread.started"...}` rather than the agent's prose, so log.md and
   report.md are unreadable for codex members. The parse that works for the final
   text does not run over the summary path.

5. **Workers open their own PRs.** **FIXED** — room-worker stops at review-request. Three PRs (#157–159) exist for members that
   `deliver` never delivered, because the skill told workers to open them. Fixed in
   the skill; `deliver` already adopts an existing PR rather than duplicating.

6. **A red baseline is invisible.** **FIXED** — _baseline_gate warns before any member runs. program-health's `develop` was already failing
   CI before the fleet launched. Every member inherited it, and nothing said so —
   the fleet cannot distinguish "this worker broke it" from "it arrived broken".
   **Fix: run the broad gate against the base branch once at fleet launch and
   record the result in the report.** A fleet that starts red should say so in its
   first line.

7. **A member that wrote nothing looks like a member that wrote a lot.** **FIXED** — branch_commit_count in the outcome. Both
   zero-commit codex members reported `stopped` with 6 iterations, identical to
   members carrying 3,000 lines. **Fix: record the branch's commit count in the
   outcome**, and name the zero case explicitly.

8. **`completed-capped` cannot fire when the scoped gate is the broken one.** **FIXED** — moot once the contract is visible. It
   only triggers on scoped-green + broad-red. Every failure here was scoped-red, so
   the status designed for "verified but blocked" never applied.

9. **`deliver.base` defaults to `main`.** **FIXED** — detect_default_branch. program-health integrates on `develop`,
   23 commits ahead. Caught by hand at the approval gate; unfixed it would have cut
   every worktree from the wrong trunk and aimed five PRs at the wrong branch.
   **Fix: default the base to the repo's actual default branch.**

10. **Dependents still duplicate the producer's file.** **FIXED** — failing producer posts a NO HANDOFF COMING notice. Three of four scry members
    independently created `internal/index/build_degrade_test.go`; four of four
    ops-calendar members rebuilt `week.ts`. When a producer's gate never goes green
    it never posts a handoff, so every dependent eventually builds the boundary
    itself. The dependency edge is advisory; nothing enforces waiting, and nothing
    tells a dependent that its producer failed rather than is merely slow.

## What went right

- `workspace.prepare` auto-detection: zero cold-gate deaths across nine members,
  against five-for-five the night before.
- Repo-check detection picked up `pnpm bar` with no flag.
- Worktrees cut from the correct base, including the local-ahead case.
- Every scry branch passed the repo's real gate independently, and the four
  integrated with exactly one union conflict.
