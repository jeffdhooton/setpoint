# Setpoint self-improvement: ANALYZE, cross-run lessons, engine auto-tune

**Date:** 2026-08-07
**Status:** Approved design, not yet implemented

## Problem

Setpoint's loop analyzes its own failures in only two shallow ways: the PLAN
stage reads the last gate feedback verbatim, and the memory spine
(`memory.py`) replays raw per-iteration summaries into DISCOVER. Nothing
distills *why* an iteration failed, nothing survives across runs, and nothing
tunes the engine itself. Runs relearn the same mistakes — within a run and
especially between runs.

Goal: make self-improvement structural, not aspirational. An ignored lesson
must cost the loop something deterministic.

Delivered in three sequenced stages, each independently shippable.

---

## Stage 1 — In-run: ANALYZE stage + cite-or-die planning

### ANALYZE stage

New stage, runs after every **failed** VERIFY, on `execute.plan_model` (one
extra cheap call per failed iteration). Input: the iteration's plan, execute
summary, and gate feedback. Output, parsed as JSON:

```json
{
  "category": "short model-assigned failure class, e.g. import-error",
  "symptom": "what the gate observed",
  "root_cause": "why it actually happened",
  "lesson": "one imperative rule the next plan must respect"
}
```

### Fingerprint

Deterministic failure signature computed in Python (not by the model):
normalize the gate feedback — strip absolute paths to basenames, strip line
and column numbers, strip timestamps, durations, addresses, and counters,
collapse whitespace — then hash. Stored as
`fingerprint = sha256(normalized)[:12]` plus the model's `category`.

Two failures match when their hashes match, OR when their categories match
and the normalized texts are near-identical (ratio ≥ 0.9 via
`difflib.SequenceMatcher`). Pure functions, unit-testable.

### Memory spine changes

`IterRecord` gains `lesson: str = ""`, `category: str = ""`,
`fingerprint: str = ""` (defaulted, so existing `state.json` files load —
same pattern as `stop_reason`).

`Memory.context_block()` grows a `## Lessons so far` section listing each
distilled lesson once (deduped by fingerprint), after the iteration history.

### Cite-or-die in PLAN

The PLAN prompt lists active lessons as `L1..Ln` and requires the plan to
contain a line starting with `Lessons:` that either names which lessons apply
and how the plan avoids them, or states `Lessons: none apply — <reason>`.

Enforcement is deterministic and cheap: if the returned plan has no `Lessons:`
line, re-prompt once with a corrective instruction. If the second attempt
still lacks it, proceed anyway (never wedge the loop on formatting) but log
the omission in `log.md`. When there are no lessons yet, the requirement is
omitted entirely.

### Repeat detector

Today `no_progress` increments only on *identical* feedback text. New rule:
a failure whose fingerprint matches any **prior lesson's** fingerprint (this
run or, after Stage 2, the repo store) is a **repeat strike** and increments
the same `no_progress` counter even when the raw feedback text differs.
Repeating a known mistake burns toward `stop.no_progress_after`. This is the
"force": ignored lessons structurally shorten the run.

`log.md` marks such iterations `⚠️ repeat of lesson <fingerprint>`.

### Degradation

If the ANALYZE call errors or returns unparseable JSON (one retry via the
existing `with_retries`), the iteration records an empty lesson and the loop
behaves exactly as today. ANALYZE can never abort a run.

---

## Stage 2 — Cross-run: native lesson store + optional scry export

### Store

`~/.setpoint/lessons/<repo-key>.jsonl`, where `repo-key` is the slugified
first git remote URL, falling back to the slugified absolute repo path. One
JSON object per line:

```json
{"ts": "...", "run": "<run name>", "goal": "...", "fingerprint": "...",
 "category": "...", "lesson": "...", "hits": 1, "validated": true}
```

### Promotion at run end

Only **validated** lessons are promoted: a lesson is validated when the next
iteration that cited it either passed the gate or produced a *different*
failure fingerprint (progress). Unvalidated lessons stay in the run's
`state.json` only.

Dedup by fingerprint on write — a re-encountered lesson increments `hits`
and refreshes `ts` instead of appending. Store capped at 100 lessons per
repo; eviction drops lowest `hits`, then oldest.

### Injection at DISCOVER

DISCOVER loads the repo's store and injects the top-K lessons (K = 10) ranked
by `hits` desc, then recency, as a `## Lessons from previous runs` block.
Cite-or-die covers these too. A failure matching a *stored* (previous-run)
lesson is an immediate repeat strike — cross-run relearning is what this
whole design exists to kill.

### Scry export (optional adapter)

New spec field `memory.scry_export: bool = false` (new top-level `memory`
block; `context.scry` — the existing tool flag — is unrelated and unchanged).
When enabled, promoted lessons are POSTed to the local scry daemon as
project-type facts (equivalent of `scry_remember`), tagged with repo and
goal. Best-effort only: connection failure logs one line and moves on.
Setpoint remains fully standalone on machines without scry (the mini).

---

## Stage 3 — Engine self-tuning with rollback

### RETRO pass

Runs after every run ends (any terminal status). Computes run stats: passed?,
iterations used, repeat strikes, cutoff count (`stop_reason != "done"`), USD
spent. Two outputs:

1. `retro.md` next to `log.md` — human-readable critique: gate too
   weak/strict, budget fit, whether cutoffs suggest steps are too big.
2. A **tuning overlay** update (below), when stats justify one.

### Tuning overlay

`~/.setpoint/tuning/<spec-key>.json` (spec-key = repo-key + slugified spec
filename). The user's spec YAML is never modified — spec is intent, overlay
is learned tuning. Overlay may set only a bounded whitelist:

| Knob | Bounds | Signal that moves it |
|------|--------|----------------------|
| `max_turns` | 10–50 (default 25; becomes an `execute` spec field, plumbed to the executor) | cutoffs (`max_turns` stop_reason) raise it; chronic early finishes lower it |
| `no_progress_after` | 2–6 | repeat strikes with slack left lower it; premature no-progress stops raise it |
| `plan_hint` | ≤ 400 chars of text appended to the PLAN prompt | distilled from retro critique |

Never touched: the verify gate, budget ceilings, delivery settings,
`max_iters`, models/engines. Spec-file values, when explicitly set by the
user, win over the overlay for numeric knobs — the overlay only fills or
nudges within bounds around defaults.

### Versioning and rollback

The overlay file keeps `history`: each version stores the knob values plus
the stats of the run that produced it. On run end, RETRO compares this run's
outcome tuple `(passed, iters, usd)` — lexicographic: passing beats not,
fewer iters beats more, cheaper beats dearer — against the run that produced
the current overlay version. Worse → revert to the previous version. Better
or equal → keep, and this run's stats become the new baseline. Corrupt or
unreadable overlay → ignored, defaults used, warning logged.

---

## Error handling summary

- ANALYZE failure → empty lesson, loop unchanged (never aborts).
- Lesson store unreadable/corrupt line → skip line, keep going.
- Scry daemon unreachable → one log line, no retry loop.
- Overlay corrupt → ignore, use spec/defaults.
- Cite-or-die formatting failure → one re-prompt, then proceed + log.

## Testing

Pytest, existing fake-client pattern:

- Fingerprint normalization: paths/line-numbers/timestamps stripped; same
  failure from different files at different lines → same fingerprint;
  near-match ratio path.
- Cite-or-die: missing `Lessons:` line triggers exactly one re-prompt; second
  failure proceeds.
- Repeat detector: fingerprint match increments `no_progress` across
  differing feedback text; stops at `no_progress_after`.
- Store: promotion only for validated lessons; dedup increments `hits`; cap
  eviction order.
- Overlay: bounds clamping; user-set spec values win; rollback on worse
  outcome tuple; corrupt file ignored.
- Back-compat: pre-existing `state.json` without new fields loads.

## Sequencing

1. **Stage 1** — ANALYZE + fingerprint + cite-or-die + repeat detector.
2. **Stage 2** — lesson store, promotion, DISCOVER injection, scry adapter.
3. **Stage 3** — RETRO, overlay, rollback.

Each stage lands with tests green before the next begins.
