# Lesson compliance: evidence-rich lessons + deterministic anchoring

**Date:** 2026-08-08
**Status:** Approved design, not yet implemented
**Builds on:** `2026-08-07-self-improvement-design.md` (Stage 1/2 shipped at `ecf1575`, ANALYZE path-specificity fix at `a618991`)

## Problem — from live A/B evidence

Four A/B pairs on a rename-trap sandbox (run A cold, run B seeded with run A's
validated lessons) showed the machinery works — lessons distill, persist,
inject, and cross-run repeats strike deterministically — but produced **zero
behavioral lift**: run B took the same 2 iterations as run A in 4/4 pairs.

Two failure modes, both observed directly in plan texts:

1. **Generic lessons are inert.** "Update configuration files" names no
   target. (Partially fixed by `a618991`; the evidence residue is fixed here.)
2. **Citation ≠ compliance.** With a lesson naming `calc/config.json`
   verbatim, run B's plan wrote *"Lessons: none apply — we are not renaming a
   function; we are correcting an import"* — reframed the situation, satisfied
   cite-or-die's format, and repeated the exact predicted failure.

Success bar (chosen): **run B one-shots the trap** — stored lessons change
behavior *before* the failure re-occurs, not just after a strike.

## Design — three reinforcing changes

### 1. Persist and inject evidence, not just the rule

ANALYZE already produces `symptom` and `root_cause`; today they are dropped
after the iteration. Carry them through:

- `IterRecord` gains `symptom: str = ""`, `root_cause: str = ""` (defaulted —
  same back-compat pattern as the existing lesson fields; old `state.json`
  loads unchanged).
- `StoredLesson` gains `symptom: str = ""`, `root_cause: str = ""`. Old JSONL
  lines lack the keys and load via defaults; no migration.
- `promote_validated` copies both fields from the `IterRecord`.

A single rendering helper composes the injected form everywhere a lesson is
shown (DISCOVER stored-lessons block, PLAN lessons rule, EXECUTE task block):

```
<lesson text> (bit this repo before: <symptom> — because: <root_cause>)
```

The parenthetical is omitted when both fields are empty (fallback lessons).
Rationale: a rule can be reframed away; a symptom describing the repo's
current state cannot — pair 4's dodge argues with "when renaming…" but not
with "config entrypoint 'sum_values' missing from calc.core".

### 2. Anchored-lesson engagement check (deterministic)

- `anchored_files(lesson_text, repo) -> list[str]`: extract path-like tokens
  (contain `/` or end in a dot-extension), strip surrounding punctuation and
  backticks, keep those where `(repo / token).exists()`, cap at 3 per lesson.
  Pure function, unit-testable.
- PLAN enforcement extends the existing cite-or-die check. After the first
  plan response, two deterministic conditions are checked together:
  a. plan has a `Lessons:` line (existing rule), AND
  b. for every lesson with anchored files, the plan text mentions at least
     one of them (full path or basename, case-insensitive substring).
- On failure of either condition: the existing **single** re-prompt fires
  (one per iteration total, shared by both conditions), now naming what is
  missing — e.g. *"lesson [b70cda7] names calc/config.json, which exists in
  this repo; state how your plan addresses it or justify, per file, why it is
  exempt."* A second miss proceeds anyway with `memory.note(...)` — the loop
  still never wedges on formatting.
- Noop plan clients (agent engines) skip PLAN enforcement exactly as today.

### 3. Flip the dismissal default in the prompts

- `_LESSONS_RULE` reframed: *"These lessons come from previous runs of this
  goal in this repo. Assume each applies unless you can name specific
  evidence that it does not. Your plan MUST include a line starting with
  `Lessons:` stating, for each lesson ID, how the plan addresses it — or the
  specific evidence it does not apply here."* The `Lessons:` marker line is
  unchanged (existing regex and tests keep working).
- EXECUTE task block: enriched lesson text plus, when anchored files exist, a
  checklist line: `Verify before finishing: calc/config.json`. Benefits agent
  engines too (their only lesson channel is the EXECUTE task).

## Out of scope (deliberate)

- ANALYZE "lesson linter" retry loop — `a618991`'s prompt change already
  yields path-specific lessons; revisit only if regressions appear.
- Compiling lessons into gate pre-checks (Approach C) — unjustified by
  current evidence.
- Any change to strikes, promotion/validation rules, RETRO, or the overlay.

## Error handling

- `anchored_files` failures (weird tokens, permission errors on `exists()`)
  degrade to "no anchors" — enforcement condition (b) then never fires.
- All new fields defaulted; unreadable/legacy store lines skip as today.
- Re-prompt budget stays at exactly one per iteration.

## Testing

Unit (pytest, existing fake-client patterns):
- Rendering: with/without symptom+root_cause; fallback lesson renders bare.
- `anchored_files`: extracts `calc/config.json` from prose and backticks;
  ignores non-existent paths; caps at 3; no false anchors from version
  strings like "3.11".
- Enforcement: plan missing anchored-file mention → exactly one re-prompt
  naming the file; plan mentioning basename passes; second miss → proceed +
  note; noop client skips; no anchored files → condition (b) inert.
- Back-compat: old state.json and old lessons JSONL load with empty new fields.
- Promotion carries symptom/root_cause into the store.

Live gate (manual, same harness at scratchpad `selfimprove-ab`):
- Re-run 3 A/B pairs; assert run B passes in **1 iteration in ≥2 of 3 pairs**
  and stored lessons carry non-empty symptom/root_cause.
