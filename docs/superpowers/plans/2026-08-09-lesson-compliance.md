# Lesson Compliance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make stored lessons change behavior preemptively — evidence-rich lesson injection plus a deterministic anchored-file engagement check, so run B of the A/B trap one-shots.

**Architecture:** ANALYZE's `symptom`/`root_cause` get persisted through `IterRecord` → `StoredLesson` and rendered into every lesson injection point via one helper. A pure function extracts repo-anchored file paths from lesson text; the existing single-re-prompt cite-or-die check grows a second deterministic condition (plans must mention anchored files or justify per-file). Prompt framing flips from "state which apply" to "assume each applies."

**Tech Stack:** Python 3.11+, stdlib only, pytest with the repo's fake-client patterns.

**Spec:** `docs/superpowers/specs/2026-08-08-lesson-compliance-design.md`

## Global Constraints

- All new dataclass fields defaulted (`str = ""`) — old `state.json` and old lessons JSONL must load unchanged; no migration.
- Re-prompt budget stays exactly ONE per iteration, shared by both enforcement conditions; second miss → proceed + `memory.note(...)`. The loop never wedges on formatting.
- Noop plan clients (agent engines) skip PLAN enforcement exactly as today.
- `anchored_files` failures degrade to "no anchors" (never raise); cap 3 anchors per lesson.
- The `Lessons:` marker line and `_LESSONS_LINE` regex are unchanged.
- No changes to strikes, promotion/validation rules, RETRO, or the overlay.
- Run all tests from repo root `~/workspace/setpoint`: `.venv/bin/python -m pytest tests/ -q`. Full suite green before every commit. Baseline: 260 tests.

---

### Task 1: Evidence fields + `render_lesson`

**Files:**
- Modify: `setpoint/memory.py` (IterRecord fields)
- Modify: `setpoint/lessons.py` (StoredLesson fields, `render_lesson`, promote merge, promote_validated carry)
- Modify: `setpoint/cycle.py` (record symptom/root_cause on the IterRecord)
- Test: `tests/test_lessons.py`, `tests/test_memory.py`, `tests/test_cycle.py`

**Interfaces:**
- Consumes: `Lesson.symptom` / `Lesson.root_cause` (already produced by `analyze()`).
- Produces: `IterRecord.symptom: str = ""`, `IterRecord.root_cause: str = ""`; `StoredLesson.symptom: str = ""`, `StoredLesson.root_cause: str = ""`; `render_lesson(lesson: str, symptom: str = "", root_cause: str = "") -> str` in `setpoint/lessons.py` — returns `"<lesson> (bit this repo before: <symptom> — because: <root_cause>)"`, omitting either clause when its field is empty and the whole parenthetical when both are.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_lessons.py`:

```python
def test_render_lesson_full_and_partial():
    from setpoint.lessons import render_lesson
    assert render_lesson("update the config", "entrypoint missing", "rename skipped it") == \
        "update the config (bit this repo before: entrypoint missing — because: rename skipped it)"
    assert render_lesson("update the config", "entrypoint missing", "") == \
        "update the config (bit this repo before: entrypoint missing)"
    assert render_lesson("update the config", "", "rename skipped it") == \
        "update the config (because: rename skipped it)"
    assert render_lesson("update the config") == "update the config"


def test_stored_lesson_old_jsonl_line_loads_with_empty_evidence(tmp_path):
    store = LessonStore("k", root=tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        '{"ts": "2026-08-07T00:00:00", "run": "r", "goal": "g", "fingerprint": "abc",'
        ' "normalized": "n", "category": "c", "lesson": "old-format lesson"}\n')
    loaded = store.load()
    assert loaded[0].symptom == "" and loaded[0].root_cause == ""


def test_promote_validated_carries_evidence(tmp_path):
    from setpoint.lessons import promote_validated
    from setpoint.memory import IterRecord, RunState
    state = RunState(name="t", status="passed", iters=[
        IterRecord(n=1, plan="", summary="", passed=False, feedback="f", usd=0,
                   lesson="fix config", fingerprint="fpA",
                   symptom="entrypoint missing", root_cause="rename skipped it"),
        IterRecord(n=2, plan="", summary="", passed=True, feedback="ok", usd=0),
    ])
    store = LessonStore("k", root=tmp_path)
    promoted = promote_validated(state, "g", store)
    assert promoted[0].symptom == "entrypoint missing"
    assert promoted[0].root_cause == "rename skipped it"
    assert store.load()[0].symptom == "entrypoint missing"


def test_promote_merge_refreshes_evidence_with_text(tmp_path):
    store = LessonStore("k", root=tmp_path)
    store.promote([_lesson("aaa", ts="2026-08-01T00:00:00")])
    fresher = _lesson("aaa", ts="2026-08-07T00:00:00", text="newer text")
    fresher.symptom, fresher.root_cause = "new symptom", "new cause"
    store.promote([fresher])
    got = store.load()[0]
    assert got.lesson == "newer text" and got.symptom == "new symptom" \
        and got.root_cause == "new cause"
```

Append to `tests/test_memory.py`:

```python
def test_iterrecord_evidence_fields_default_and_backcompat(tmp_path):
    import json
    from setpoint.memory import Memory
    root = tmp_path / "runs"
    (root / "t").mkdir(parents=True)
    (root / "t" / "state.json").write_text(json.dumps({
        "name": "t", "status": "stopped", "spent_usd": 0.0,
        "iters": [{"n": 1, "plan": "p", "summary": "s", "passed": False,
                   "feedback": "f", "usd": 0.0}],
    }))
    r = Memory("t", root=root).load().iters[0]
    assert r.symptom == "" and r.root_cause == ""
```

Append to `tests/test_cycle.py`:

```python
def test_failed_iteration_records_evidence_fields(tmp_path):
    import json
    lesson_json = json.dumps({"category": "c", "symptom": "the symptom",
                              "root_cause": "the cause", "lesson": "the rule"})

    def create(**kw):
        text = lesson_json if "ANALYZE stage" in kw["messages"][0]["content"] \
            else "plan\nLessons: none apply — first attempt"
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                  prompt_cache_hit_tokens=0))

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    state = Cycle(_spec(tmp_path, max_iters=1), FakeExecutor(), FakeGate(pass_on_iter=99),
                  Memory("t", root=tmp_path / "r"), Budget(10.0, None, PRICING),
                  StubUI(), client).run(cwd=tmp_path)
    assert state.iters[0].symptom == "the symptom"
    assert state.iters[0].root_cause == "the cause"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_lessons.py tests/test_memory.py tests/test_cycle.py -q`
Expected: FAIL — `ImportError: cannot import name 'render_lesson'`, `TypeError: ... unexpected keyword argument 'symptom'`.

- [ ] **Step 3: Implement**

`setpoint/memory.py` — extend `IterRecord` (after `repeat_of`):

```python
    symptom: str = ""     # what the gate observed (from ANALYZE)
    root_cause: str = ""  # why it happened (from ANALYZE)
```

`setpoint/lessons.py` — extend `StoredLesson` (after `validated`):

```python
    symptom: str = ""
    root_cause: str = ""
```

Add below `CAP = 100`:

```python
def render_lesson(lesson: str, symptom: str = "", root_cause: str = "") -> str:
    """One display form for every injection point. Evidence makes a lesson
    hard to reframe away; a bare rule is easy to dismiss."""
    bits = []
    if symptom:
        bits.append(f"bit this repo before: {symptom}")
    if root_cause:
        bits.append(f"because: {root_cause}")
    return f"{lesson} ({' — '.join(bits)})" if bits else lesson
```

In `LessonStore.promote`, extend the freshness branch:

```python
                if sl.lesson and sl.ts >= existing.ts:
                    existing.lesson = sl.lesson  # keep the freshest phrasing
                    existing.symptom = sl.symptom
                    existing.root_cause = sl.root_cause
```

In `promote_validated`, add to the `StoredLesson(...)` construction:

```python
                category=r.category, lesson=r.lesson,
                symptom=r.symptom, root_cause=r.root_cause))
```

`setpoint/cycle.py` — in `run()`'s `IterRecord(...)` construction, after `fingerprint=...`:

```python
                             symptom=lesson.symptom if lesson else "",
                             root_cause=lesson.root_cause if lesson else "",
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_lessons.py tests/test_memory.py tests/test_cycle.py -q` — PASS.
Then: `.venv/bin/python -m pytest tests/ -q` — full suite PASS.

- [ ] **Step 5: Commit**

```bash
git add setpoint/memory.py setpoint/lessons.py setpoint/cycle.py tests/
git commit -m "feat(lessons): persist symptom/root_cause evidence through the store"
```

---

### Task 2: `anchored_files` extraction

**Files:**
- Modify: `setpoint/lessons.py`
- Test: `tests/test_lessons.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `anchored_files(text: str, repo: Path, cap: int = 3) -> list[str]` — path-like tokens from lesson text that exist as files under `repo`, deduped, order of appearance, capped; never raises.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_lessons.py`:

```python
def _repo_with(tmp_path, *files):
    for f in files:
        p = tmp_path / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    return tmp_path


def test_anchored_files_extracts_existing_paths(tmp_path):
    from setpoint.lessons import anchored_files
    repo = _repo_with(tmp_path, "calc/config.json", "calc/core.py")
    text = "When renaming, update the entrypoint field in calc/config.json too."
    assert anchored_files(text, repo) == ["calc/config.json"]


def test_anchored_files_handles_backticks_and_trailing_punctuation(tmp_path):
    from setpoint.lessons import anchored_files
    repo = _repo_with(tmp_path, "calc/config.json")
    assert anchored_files("update `calc/config.json`.", repo) == ["calc/config.json"]


def test_anchored_files_ignores_nonexistent_versions_and_modules(tmp_path):
    from setpoint.lessons import anchored_files
    repo = _repo_with(tmp_path, "calc/config.json")
    text = "on python 3.11 the calc.core module needs docs/missing.md updated"
    assert anchored_files(text, repo) == []


def test_anchored_files_caps_and_dedupes(tmp_path):
    from setpoint.lessons import anchored_files
    repo = _repo_with(tmp_path, "a.txt", "b.txt", "c.txt", "d.txt")
    text = "fix a.txt a.txt b.txt c.txt d.txt"
    assert anchored_files(text, repo) == ["a.txt", "b.txt", "c.txt"]


def test_anchored_files_never_raises(tmp_path):
    from setpoint.lessons import anchored_files
    assert anchored_files("x" * 10000, tmp_path / "no-such-dir") == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_lessons.py -q`
Expected: FAIL — `ImportError: cannot import name 'anchored_files'`.

- [ ] **Step 3: Implement**

Add to `setpoint/lessons.py` (below `render_lesson`; `re`, `Path` already imported):

```python
_PATHISH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*")
_VERSIONISH = re.compile(r"^\d+(?:\.\d+)*$")


def anchored_files(text: str, repo: Path, cap: int = 3) -> list[str]:
    """File paths a lesson names that actually exist in the repo. Anchors make
    lesson engagement deterministically checkable. Never raises."""
    out: list[str] = []
    try:
        for tok in _PATHISH.findall(text):
            tok = tok.rstrip(".,;:").lstrip("./")
            if not tok or tok in out or _VERSIONISH.match(tok):
                continue
            if "/" not in tok and "." not in tok:
                continue  # bare words can't be file paths
            try:
                if (Path(repo) / tok).is_file():
                    out.append(tok)
            except OSError:
                continue
            if len(out) >= cap:
                break
    except Exception:
        pass
    return out
```

Note: `test_anchored_files_ignores_nonexistent_versions_and_modules` passes because `3.11` matches `_VERSIONISH`, `calc.core` and `docs/missing.md` fail `is_file()`. The regex won't match backticks, so `` `calc/config.json` `` tokenizes cleanly.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_lessons.py -q` — PASS.
Then: `.venv/bin/python -m pytest tests/ -q` — full suite PASS.

- [ ] **Step 5: Commit**

```bash
git add setpoint/lessons.py tests/test_lessons.py
git commit -m "feat(lessons): extract repo-anchored file paths from lesson text"
```

---

### Task 3: Evidence-rich injection + default-apply framing

**Files:**
- Modify: `setpoint/cycle.py`
- Test: `tests/test_cycle.py`

**Interfaces:**
- Consumes: `render_lesson`, `anchored_files` from Tasks 1–2.
- Produces: `Cycle._lessons() -> list[tuple[str, str, list[str]]]` — `(fingerprint, display_text, anchors)` triples, stored first then in-run, deduped. `_discover()` renders stored lessons with evidence. EXECUTE task gains `Verify before finishing: <anchors>` when any anchors exist. `_LESSONS_RULE` reframed to default-apply. Task 4 relies on the triple shape.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cycle.py`:

```python
def _store_with_lesson(tmp_path, lesson, symptom="", root_cause="", fb="AssertionError: boom"):
    from setpoint.analyze import fingerprint, normalize_feedback
    from setpoint.lessons import LessonStore, StoredLesson
    store = LessonStore("k", root=tmp_path / "lessons")
    store.promote([StoredLesson(ts="2026-08-01T00:00:00", run="old", goal="g",
                                fingerprint=fingerprint(fb),
                                normalized=normalize_feedback(fb), category="assertion",
                                lesson=lesson, symptom=symptom, root_cause=root_cause)])
    return store


def test_injected_lessons_carry_evidence(tmp_path):
    store = _store_with_lesson(tmp_path, "update the entrypoint",
                               symptom="entrypoint missing", root_cause="rename skipped it")
    prompts = []

    def create(**kw):
        prompts.append(kw["messages"][-1]["content"])
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="p\nLessons: applies"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                  prompt_cache_hit_tokens=0))

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    Cycle(_spec(tmp_path, max_iters=1), FakeExecutor(), FakeGate(pass_on_iter=1),
          Memory("t", root=tmp_path / "r"), Budget(10.0, None, PRICING),
          StubUI(), client, lesson_store=store).run(cwd=tmp_path)
    assert "bit this repo before: entrypoint missing" in prompts[0]
    assert "because: rename skipped it" in prompts[0]
    assert "assume each applies" in prompts[0].lower()


def test_execute_task_lists_anchor_checklist(tmp_path):
    (tmp_path / "calc").mkdir()
    (tmp_path / "calc" / "config.json").write_text("{}")
    store = _store_with_lesson(tmp_path, "update the entrypoint in calc/config.json")

    class TaskCapture(FakeExecutor):
        def __init__(self):
            super().__init__()
            self.tasks = []
        def execute(self, system, task, tools, model, cwd, on_event):
            self.tasks.append(task)
            return super().execute(system, task, tools, model, cwd, on_event)

    def create(**kw):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content="p\nLessons: applies — will update calc/config.json"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                  prompt_cache_hit_tokens=0))

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    ex = TaskCapture()
    Cycle(_spec(tmp_path, max_iters=1), ex, FakeGate(pass_on_iter=1),
          Memory("t", root=tmp_path / "r"), Budget(10.0, None, PRICING),
          StubUI(), client, lesson_store=store).run(cwd=tmp_path)
    assert "Verify before finishing: calc/config.json" in ex.tasks[0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_cycle.py -q`
Expected: the two new tests FAIL (no evidence text, no checklist line).

- [ ] **Step 3: Implement**

In `setpoint/cycle.py`, add the import:

```python
from setpoint.lessons import anchored_files, render_lesson
```

Replace `_LESSONS_RULE`:

```python
_LESSONS_RULE = """
Lessons from previous runs of this goal in this repo. Assume each applies
unless you can name specific evidence that it does not:
{lessons}

Your plan MUST include a line starting with "Lessons:" stating, for each
lesson ID, how the plan addresses it — or the specific evidence that it does
not apply here.
"""
```

Replace `_lessons()`:

```python
    def _lessons(self) -> list[tuple[str, str, list[str]]]:
        """(fingerprint, display, anchors) triples: stored lessons first, then
        this run's, deduped by fingerprint."""
        out: dict[str, tuple[str, list[str]]] = {}
        repo = self.spec.workspace.repo
        for sl in self._stored_lessons():
            if sl.lesson and sl.fingerprint not in out:
                out[sl.fingerprint] = (render_lesson(sl.lesson, sl.symptom, sl.root_cause),
                                       anchored_files(sl.lesson, repo))
        for r in self.memory.load().iters:
            if r.lesson and r.fingerprint not in out:
                out[r.fingerprint] = (render_lesson(r.lesson, r.symptom, r.root_cause),
                                      anchored_files(r.lesson, repo))
        return [(fp, d, a) for fp, (d, a) in out.items()]
```

In `_discover()`, replace the stored-lessons block body:

```python
        if stored:
            parts.append("## Lessons from previous runs\n" + "\n".join(
                f"- [{sl.fingerprint}] {render_lesson(sl.lesson, sl.symptom, sl.root_cause)}"
                for sl in stored))
```

In `_plan()`, the listing unpacks triples:

```python
            listing = "\n".join(f"- {fp}: {text}" for fp, text, _ in lessons)
```

In `run()`'s EXECUTE block, replace the lessons append:

```python
            if lessons:
                task += ("\n\nLessons from previous iterations "
                         "(do not repeat these mistakes):\n"
                         + "\n".join(f"- {d}" for _, d, _ in lessons))
                anchors = sorted({a for _, _, aa in lessons for a in aa})
                if anchors:
                    task += "\nVerify before finishing: " + ", ".join(anchors)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_cycle.py -q` — PASS (existing lesson tests keep passing: their lesson texts anchor no real files, and the `Lessons:` marker is unchanged).
Then: `.venv/bin/python -m pytest tests/ -q` — full suite PASS.

- [ ] **Step 5: Commit**

```bash
git add setpoint/cycle.py tests/test_cycle.py
git commit -m "feat(cycle): inject lesson evidence and anchor checklist; default-apply framing"
```

---

### Task 4: Deterministic anchored-engagement enforcement

**Files:**
- Modify: `setpoint/cycle.py`
- Test: `tests/test_cycle.py`

**Interfaces:**
- Consumes: `_lessons()` triples from Task 3.
- Produces: module function `_plan_problems(plan: str, lessons: list[tuple[str, str, list[str]]]) -> str` — returns "" when the plan is acceptable, else a corrective message covering BOTH a missing `Lessons:` line and any anchored lesson whose anchors the plan never mentions (full path or basename, case-insensitive). `_plan` uses it for the single shared re-prompt. `_CITE_REPROMPT` constant is deleted.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cycle.py`:

```python
def test_plan_problems_pure():
    from setpoint.cycle import _plan_problems
    lessons = [("abc123", "update calc/config.json", ["calc/config.json"])]
    ok = "plan\nLessons: abc123 — updating calc/config.json"
    assert _plan_problems(ok, lessons) == ""
    basename_ok = "plan touches config.json as needed\nLessons: abc123 applies"
    assert _plan_problems(basename_ok, lessons) == ""
    dodged = "plan\nLessons: none apply — we are only fixing an import"
    msg = _plan_problems(dodged, lessons)
    assert "calc/config.json" in msg and "abc123" in msg
    no_line = "plan without the marker, mentions calc/config.json"
    assert "Lessons:" in _plan_problems(no_line, lessons)
    assert _plan_problems("anything\nLessons: none apply — x",
                          [("abc123", "generic rule", [])]) == ""


def test_anchor_dodge_gets_one_reprompt_then_proceeds(tmp_path):
    (tmp_path / "calc").mkdir()
    (tmp_path / "calc" / "config.json").write_text("{}")
    store = _store_with_lesson(tmp_path, "update the entrypoint in calc/config.json")
    mem = Memory("t", root=tmp_path / "r")
    client = _lesson_client([
        "plan\nLessons: none apply — just fixing an import",   # iter 1 attempt 1: dodge
        "plan\nLessons: none apply — still just an import",    # iter 1 attempt 2: dodge again
    ])
    Cycle(_spec(tmp_path, max_iters=1), FakeExecutor(), FakeGate(pass_on_iter=99),
          mem, Budget(10.0, None, PRICING), StubUI(), client,
          lesson_store=store).run(cwd=tmp_path)
    plans = _plan_calls(client)
    assert len(plans) == 2                                  # exactly one re-prompt
    assert "calc/config.json" in plans[1][-1]["content"]    # corrective names the file
    assert "engagement" in mem.log_path.read_text()         # second miss noted


def test_anchor_mention_avoids_reprompt(tmp_path):
    (tmp_path / "calc").mkdir()
    (tmp_path / "calc" / "config.json").write_text("{}")
    store = _store_with_lesson(tmp_path, "update the entrypoint in calc/config.json")
    client = _lesson_client([
        "fix cli and update calc/config.json entrypoint\nLessons: applies",
    ])
    Cycle(_spec(tmp_path, max_iters=1), FakeExecutor(), FakeGate(pass_on_iter=1),
          Memory("t", root=tmp_path / "r"), Budget(10.0, None, PRICING),
          StubUI(), client, lesson_store=store).run(cwd=tmp_path)
    assert len(_plan_calls(client)) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_cycle.py -q`
Expected: FAIL — `ImportError: cannot import name '_plan_problems'`.

- [ ] **Step 3: Implement**

In `setpoint/cycle.py`, delete `_CITE_REPROMPT` and add below `_LESSONS_LINE`:

```python
def _plan_problems(plan: str, lessons: list[tuple[str, str, list[str]]]) -> str:
    """Deterministic lesson-engagement check. Returns a corrective message,
    or "" when the plan satisfies both conditions."""
    issues = []
    if not _LESSONS_LINE.search(plan):
        issues.append('your plan is missing the required "Lessons:" line')
    low = plan.lower()
    for fp, _, anchors in lessons:
        if anchors and not any(a.lower() in low or Path(a).name.lower() in low
                               for a in anchors):
            issues.append(
                f"lesson [{fp}] names {', '.join(anchors)}, which exist in this repo — "
                f"state how your plan addresses them, or justify per file why they are exempt")
    if not issues:
        return ""
    return "Revise your plan: " + "; ".join(issues) + ". Reply with the full revised plan."
```

In `_plan()`, replace the enforcement block after the first `self._plan_call(messages)`:

```python
        if enforce:
            problems = _plan_problems(plan, lessons)
            if problems:
                messages += [{"role": "assistant", "content": plan},
                             {"role": "user", "content": problems}]
                plan, usage2 = self._plan_call(messages)
                usage = Usage(usage.input_tokens + usage2.input_tokens,
                              usage.output_tokens + usage2.output_tokens,
                              usage.cache_read_tokens + usage2.cache_read_tokens)
                if _plan_problems(plan, lessons):
                    self.memory.note("PLAN omitted required lesson engagement after re-prompt")
        return plan, usage
```

No existing-test edits are needed: `test_plan_reprompted_once_when_lessons_line_missing` asserts `"missing" in` the corrective message — `_plan_problems`' first issue text contains "missing" — and asserts `"omitted" in` the log note, which the new note text ("PLAN omitted required lesson engagement after re-prompt") still contains. Both hold by construction.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_cycle.py -q` — PASS.
Then: `.venv/bin/python -m pytest tests/ -q` — full suite PASS.

- [ ] **Step 5: Commit**

```bash
git add setpoint/cycle.py tests/test_cycle.py
git commit -m "feat(cycle): anchored lessons are deterministically non-dismissible"
```

---

## Final verification

- [ ] Full suite green: `.venv/bin/python -m pytest tests/ -q`.
- [ ] **Live A/B gate** (manual, harness at the session scratchpad `selfimprove-ab`): wipe pair state, re-run 3 pairs with `run-pair.sh`. Success bar from the spec: run B passes in **1 iteration in ≥2 of 3 pairs**, and stored lessons carry non-empty `symptom`/`root_cause`. If the bar fails, report the B-run plan texts — do not tune further without evidence.
