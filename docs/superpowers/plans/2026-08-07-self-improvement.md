# Setpoint Self-Improvement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make setpoint's loop learn from its own failures — a per-iteration ANALYZE stage with deterministic repeat detection (Stage 1), a cross-run lesson store with optional scry export (Stage 2), and a bounded self-tuning overlay with rollback (Stage 3).

**Architecture:** New pure-function module `analyze.py` computes failure fingerprints in Python (deterministic, unit-testable) and optionally distills lessons via the plan model. `cycle.py` gains an ANALYZE step, cite-or-die plan enforcement, and repeat strikes that feed the existing `no_progress` counter. `lessons.py` persists validated lessons per repo; `tuning.py` + `retro.py` adjust a bounded knob overlay after each run, never touching the user's spec YAML.

**Tech Stack:** Python 3.11+, stdlib only for new modules (hashlib, difflib, json, re, subprocess), pytest with the repo's existing fake-client pattern.

**Spec:** `docs/superpowers/specs/2026-08-07-self-improvement-design.md`

## Global Constraints

- Python 3.11+; no new pip dependencies — new modules use stdlib only.
- ANALYZE can never abort a run: any failure degrades to an empty/fallback lesson.
- The user's spec YAML is never modified; overlay knobs live in `~/.setpoint/tuning/`.
- Explicit user-set spec values always win over the overlay.
- Overlay whitelist and bounds: `max_turns` 10–50, `no_progress_after` 2–6, `plan_hint` ≤ 400 chars. Never touch gate, budget, delivery, `max_iters`, models/engines.
- Scry export is best-effort: shells the `scry` binary, one log line on failure, never raises, never retries.
- All new state roots honor env overrides like the existing `SETPOINT_RUNS_ROOT`: `SETPOINT_LESSONS_ROOT`, `SETPOINT_TUNING_ROOT`.
- Pre-existing `state.json` files (no new fields) must still load — default every new `IterRecord` field.
- Lesson store cap: 100 per repo; DISCOVER injection top-K with K=10; near-match ratio ≥ 0.9; fingerprint = `sha256(normalized)[:12]`.
- Run all tests from repo root `~/workspace/setpoint`: `python -m pytest tests/ -q`. Full suite must pass before every commit.

---

## Stage 1 — In-run ANALYZE + cite-or-die + repeat detector

### Task 1: Fingerprint core (`analyze.py` pure functions)

**Files:**
- Create: `setpoint/analyze.py`
- Test: `tests/test_analyze.py`

**Interfaces:**
- Consumes: nothing (stdlib only).
- Produces: `normalize_feedback(text: str) -> str`, `fingerprint(text: str) -> str` (12 hex chars), `is_repeat(feedback: str, category: str, priors: list[tuple[str, str, str]]) -> str | None` where each prior is `(fingerprint, normalized, category)` and the return is the matched fingerprint or None.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_analyze.py
from setpoint.analyze import fingerprint, is_repeat, normalize_feedback


def test_same_failure_different_paths_and_lines_match():
    a = "FAILED /Users/jeff/proj/tests/test_api.py:42: AssertionError: expected 200 got 500"
    b = "FAILED /home/ci/build/tests/test_api.py:97: AssertionError: expected 200 got 500"
    assert fingerprint(a) == fingerprint(b)


def test_different_failures_differ():
    assert fingerprint("ImportError: no module named foo") != \
        fingerprint("SyntaxError: invalid syntax on line 3")


def test_normalize_strips_durations_and_timestamps():
    a = normalize_feedback("2 failed in 0.34s at 2026-08-07T10:00:01")
    b = normalize_feedback("2 failed in 1.99s at 2026-08-07T11:23:45")
    assert a == b


def test_normalize_collapses_whitespace_and_case():
    assert normalize_feedback("Error:   Foo\n\tbar") == normalize_feedback("error: foo bar")


def test_is_repeat_exact_fingerprint():
    fb = "AssertionError: expected True"
    priors = [(fingerprint(fb), normalize_feedback(fb), "assertion")]
    assert is_repeat("AssertionError: expected True", "", priors) == fingerprint(fb)


def test_is_repeat_near_match_needs_same_category():
    a = "ImportError: cannot import name 'get_user' from 'app.auth.helpers'"
    b = "ImportError: cannot import name 'get_users' from 'app.auth.helpers'"
    priors = [(fingerprint(a), normalize_feedback(a), "import-error")]
    assert fingerprint(b) != fingerprint(a)          # not an exact match
    assert is_repeat(b, "import-error", priors) is not None   # near-match + same category
    assert is_repeat(b, "type-error", priors) is None          # category differs
    assert is_repeat(b, "", priors) is None                    # no category, no near-match


def test_is_repeat_none_when_unrelated():
    priors = [(fingerprint("ImportError: no module named foo"),
               normalize_feedback("ImportError: no module named foo"), "import-error")]
    assert is_repeat("SyntaxError: invalid syntax", "import-error", priors) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_analyze.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'setpoint.analyze'`

- [ ] **Step 3: Write the implementation**

```python
# setpoint/analyze.py
from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher

# Absolute POSIX paths -> basename, so the same failure reported from two
# checkouts (or a worktree) fingerprints identically.
_PATH = re.compile(r"(?:/[\w.\-]+){2,}")
# Bare numbers (line/col numbers, counts, durations, ports, timestamps).
_NUM = re.compile(r"\b\d+(?:\.\d+)?\b")
_WS = re.compile(r"\s+")

NEAR_MATCH_RATIO = 0.9


def normalize_feedback(text: str) -> str:
    t = _PATH.sub(lambda m: m.group(0).rsplit("/", 1)[-1], text)
    t = _NUM.sub("#", t)
    t = _WS.sub(" ", t).strip().lower()
    return t[:2000]


def fingerprint(text: str) -> str:
    return hashlib.sha256(normalize_feedback(text).encode()).hexdigest()[:12]


def is_repeat(feedback: str, category: str,
              priors: list[tuple[str, str, str]]) -> str | None:
    """priors: (fingerprint, normalized, category) of earlier failures/lessons.
    Returns the matched prior fingerprint, or None."""
    fp = fingerprint(feedback)
    norm = normalize_feedback(feedback)
    for pfp, pnorm, pcat in priors:
        if fp == pfp:
            return pfp
        if (category and pcat and category == pcat and pnorm
                and SequenceMatcher(None, norm, pnorm).ratio() >= NEAR_MATCH_RATIO):
            return pfp
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_analyze.py -q` — Expected: PASS.
Then: `python -m pytest tests/ -q` — Expected: full suite PASS.

- [ ] **Step 5: Commit**

```bash
git add setpoint/analyze.py tests/test_analyze.py
git commit -m "feat(analyze): deterministic failure fingerprints and repeat matching"
```

---

### Task 2: `Lesson` dataclass and the `analyze()` LLM call

**Files:**
- Modify: `setpoint/analyze.py`
- Modify: `setpoint/executor/agent_plan.py` (add `is_noop = True` class attribute)
- Test: `tests/test_analyze.py`

**Interfaces:**
- Consumes: `setpoint.budget.Usage`, `setpoint.retry.with_retries`, Task 1's `fingerprint`/`normalize_feedback`.
- Produces: `@dataclass Lesson(fingerprint: str, normalized: str, category: str = "", symptom: str = "", root_cause: str = "", lesson: str = "")` and `analyze(client, model: str, plan: str, summary: str, feedback: str) -> tuple[Lesson, Usage]`. Clients with a truthy `is_noop` attribute skip the LLM. `AgentPlanClient.is_noop` is True.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_analyze.py`:

```python
import json
from types import SimpleNamespace

from setpoint.analyze import Lesson, analyze
from setpoint.budget import Usage


def _client(text):
    def create(**kw):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5,
                                  prompt_cache_hit_tokens=0))
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


GOOD = json.dumps({"category": "import-error", "symptom": "module not found",
                   "root_cause": "renamed module not updated in caller",
                   "lesson": "update every import site when renaming a module"})


def test_analyze_parses_model_json():
    lesson, usage = analyze(_client(GOOD), "m", "plan", "did stuff",
                            "ImportError: no module named foo")
    assert lesson.category == "import-error"
    assert lesson.lesson.startswith("update every import")
    assert lesson.fingerprint and lesson.normalized
    assert usage.prompt_tokens == 10


def test_analyze_parses_fenced_json():
    lesson, _ = analyze(_client(f"```json\n{GOOD}\n```"), "m", "p", "s", "boom")
    assert lesson.category == "import-error"


def test_analyze_falls_back_on_bad_json():
    lesson, _ = analyze(_client("not json at all"), "m", "p", "s",
                        "AssertionError: nope\nsecond line")
    assert lesson.lesson == ""
    assert lesson.symptom == "AssertionError: nope"   # first feedback line
    assert lesson.fingerprint                          # fingerprint still computed


def test_analyze_never_raises():
    def create(**kw):
        raise RuntimeError("api down")
    broken = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    lesson, usage = analyze(broken, "m", "p", "s", "boom")
    assert lesson.fingerprint and lesson.lesson == ""
    assert usage.prompt_tokens == 0


def test_analyze_noop_client_skips_llm():
    from setpoint.executor.agent_plan import AgentPlanClient
    client = AgentPlanClient()
    assert getattr(client, "is_noop", False) is True
    lesson, _ = analyze(client, "m", "p", "s", "gate said no")
    assert lesson.fingerprint and lesson.lesson == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_analyze.py -q`
Expected: FAIL — `ImportError: cannot import name 'Lesson'`

- [ ] **Step 3: Write the implementation**

Append to `setpoint/analyze.py`:

```python
import json
from dataclasses import dataclass

from setpoint.budget import Usage
from setpoint.retry import with_retries

_ANALYZE_PROMPT = """You are the ANALYZE stage of a closed loop. An iteration just failed its verify gate.

Plan for the iteration:
{plan}

What the executor did:
{summary}

Gate feedback:
{feedback}

Respond with ONLY a JSON object:
{{"category": "<short kebab-case failure class, e.g. import-error>",
  "symptom": "<what the gate observed, one line>",
  "root_cause": "<why it actually happened, one line>",
  "lesson": "<one imperative rule the next plan must respect>"}}"""

_JSON_BLOB = re.compile(r"\{.*\}", re.S)


@dataclass
class Lesson:
    fingerprint: str
    normalized: str
    category: str = ""
    symptom: str = ""
    root_cause: str = ""
    lesson: str = ""


def _fallback(feedback: str) -> Lesson:
    first = feedback.strip().splitlines()[0][:200] if feedback.strip() else ""
    return Lesson(fingerprint=fingerprint(feedback),
                  normalized=normalize_feedback(feedback), symptom=first)


def analyze(client, model: str, plan: str, summary: str,
            feedback: str) -> tuple[Lesson, Usage]:
    """Distill a failed iteration into a lesson. Never raises — any failure
    returns a fingerprint-only fallback so the loop is never blocked."""
    if getattr(client, "is_noop", False):
        return _fallback(feedback), Usage()
    prompt = _ANALYZE_PROMPT.format(plan=plan[:2000], summary=summary[:2000],
                                    feedback=feedback[:4000])
    try:
        resp = with_retries(lambda: client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": prompt}],
        ), attempts=2)
    except Exception:
        return _fallback(feedback), Usage()
    u = resp.usage
    usage = Usage(getattr(u, "prompt_tokens", 0) or 0,
                  getattr(u, "completion_tokens", 0) or 0,
                  getattr(u, "prompt_cache_hit_tokens", 0) or 0)
    m = _JSON_BLOB.search(resp.choices[0].message.content or "")
    if not m:
        return _fallback(feedback), usage
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return _fallback(feedback), usage
    return Lesson(
        fingerprint=fingerprint(feedback),
        normalized=normalize_feedback(feedback),
        category=str(data.get("category", ""))[:60],
        symptom=str(data.get("symptom", ""))[:200],
        root_cause=str(data.get("root_cause", ""))[:300],
        lesson=str(data.get("lesson", ""))[:300],
    ), usage
```

In `setpoint/executor/agent_plan.py`, add one line to the `AgentPlanClient` class body (above `__init__`):

```python
class AgentPlanClient:
    """... (docstring unchanged) ..."""

    is_noop = True  # cycle/analyze skip LLM prompting through this shim
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_analyze.py tests/test_agent_plan.py -q` — Expected: PASS.
Then: `python -m pytest tests/ -q` — Expected: full suite PASS.

- [ ] **Step 5: Commit**

```bash
git add setpoint/analyze.py setpoint/executor/agent_plan.py tests/test_analyze.py
git commit -m "feat(analyze): Lesson distillation via plan model with safe fallback"
```

---

### Task 3: Memory spine carries lessons (`memory.py`)

**Files:**
- Modify: `setpoint/memory.py`
- Test: `tests/test_memory.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `IterRecord` gains `lesson: str = ""`, `category: str = ""`, `fingerprint: str = ""`, `repeat_of: str = ""`. `Memory.context_block()` appends a `## Lessons so far` section (deduped by fingerprint). `Memory.note(text: str) -> None` appends a one-line note to `log.md`. `_append_log` marks repeats.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_memory.py`:

```python
def test_old_state_json_without_lesson_fields_loads(tmp_path):
    import json
    from setpoint.memory import Memory
    root = tmp_path / "runs"
    (root / "t").mkdir(parents=True)
    (root / "t" / "state.json").write_text(json.dumps({
        "name": "t", "status": "stopped", "spent_usd": 0.1,
        "iters": [{"n": 1, "plan": "p", "summary": "s", "passed": False,
                   "feedback": "f", "usd": 0.1}],
    }))
    state = Memory("t", root=root).load()
    assert state.iters[0].lesson == ""
    assert state.iters[0].fingerprint == ""
    assert state.iters[0].repeat_of == ""


def test_context_block_lists_lessons_deduped(tmp_path):
    from setpoint.memory import IterRecord, Memory
    mem = Memory("t", root=tmp_path / "runs")
    mem.start()
    mem.append(IterRecord(n=1, plan="p", summary="s", passed=False, feedback="f",
                          usd=0.0, lesson="pin the dep version", fingerprint="abc123"))
    mem.append(IterRecord(n=2, plan="p", summary="s", passed=False, feedback="f",
                          usd=0.0, lesson="pin the dep version", fingerprint="abc123"))
    block = mem.context_block()
    assert "## Lessons so far" in block
    assert block.count("pin the dep version") == 1
    assert "[abc123]" in block


def test_context_block_no_lessons_section_when_none(tmp_path):
    from setpoint.memory import IterRecord, Memory
    mem = Memory("t", root=tmp_path / "runs")
    mem.start()
    mem.append(IterRecord(n=1, plan="p", summary="s", passed=False, feedback="f", usd=0.0))
    assert "Lessons so far" not in mem.context_block()


def test_log_marks_repeat_iterations(tmp_path):
    from setpoint.memory import IterRecord, Memory
    mem = Memory("t", root=tmp_path / "runs")
    mem.start()
    mem.append(IterRecord(n=1, plan="p", summary="s", passed=False, feedback="f",
                          usd=0.0, repeat_of="abc123"))
    assert "repeat of lesson abc123" in mem.log_path.read_text()


def test_memory_note_appends_to_log(tmp_path):
    from setpoint.memory import Memory
    mem = Memory("t", root=tmp_path / "runs")
    mem.start()
    mem.note("PLAN omitted required Lessons line after re-prompt")
    assert "PLAN omitted" in mem.log_path.read_text()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_memory.py -q`
Expected: FAIL — `TypeError: IterRecord.__init__() got an unexpected keyword argument 'lesson'`

- [ ] **Step 3: Write the implementation**

In `setpoint/memory.py`, extend `IterRecord` (after `stop_reason`):

```python
    # Stage-1 self-improvement fields. Defaulted so pre-existing state.json loads.
    lesson: str = ""       # imperative rule distilled by ANALYZE ("" = none)
    category: str = ""     # model-assigned failure class
    fingerprint: str = ""  # deterministic failure signature (12 hex chars)
    repeat_of: str = ""    # fingerprint of the prior lesson this failure repeated
```

Extend `context_block()` — after the existing loop over `state.iters`, before the `return`:

```python
        lessons: dict[str, str] = {}
        for r in state.iters:
            if r.lesson and r.fingerprint not in lessons:
                lessons[r.fingerprint] = r.lesson
        if lessons:
            lines.append("")
            lines.append("## Lessons so far")
            for fp, text in lessons.items():
                lines.append(f"- [{fp}] {text}")
```

Extend `_append_log` — after the `verdict` is built:

```python
        if rec.repeat_of:
            verdict += f" ⚠️ repeat of lesson {rec.repeat_of}"
```

And append the lesson line to the block when present (after the `**Verify:**` line):

```python
        if rec.lesson:
            block += f"\n**Lesson:** {rec.lesson}\n"
```

Add the `note` method to `Memory`:

```python
    def note(self, text: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a") as f:
            f.write(f"\n> note: {text}\n")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_memory.py -q` — Expected: PASS.
Then: `python -m pytest tests/ -q` — Expected: full suite PASS.

- [ ] **Step 5: Commit**

```bash
git add setpoint/memory.py tests/test_memory.py
git commit -m "feat(memory): lesson fields on the spine, repeat markers, log notes"
```

---

### Task 4: Wire ANALYZE into the cycle

**Files:**
- Modify: `setpoint/cycle.py`
- Test: `tests/test_cycle.py`

**Interfaces:**
- Consumes: `analyze()`, `Lesson` from Task 2; `IterRecord` fields from Task 3.
- Produces: after every failed VERIFY, `Cycle.run` calls `self.ui.stage("ANALYZE", i, ...)` then `analyze(self.plan_client, self.spec.execute.plan_model, plan, result.text, gate_result.feedback)`; the returned lesson populates `rec.lesson/category/fingerprint`; analyze usage is added to the budget and `iter_usd`. Lessons (in-run, deduped) are appended to the EXECUTE task text under `Lessons from previous iterations`. Helper `self._lessons(...)` returns `list[tuple[str, str]]` of `(fingerprint, lesson_text)` — later tasks reuse it.

**NOTE:** ANALYZE calls go through the same `plan_client`, so existing tests that index into captured prompts (`test_cutoff_executor_warns_the_next_plan`, `test_preflight_cold_feedback_seeds_first_plan`, `test_clean_executor_adds_no_cutoff_note`) must filter to PLAN prompts. Update them as shown.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cycle.py`:

```python
def test_failed_iteration_records_fingerprint_and_lesson(tmp_path):
    import json
    lesson_json = json.dumps({"category": "test-failure", "symptom": "s",
                              "root_cause": "r", "lesson": "run the linter first"})

    def create(**kw):
        content = kw["messages"][0]["content"]
        text = lesson_json if "ANALYZE stage" in content else "here is the plan"
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5,
                                  prompt_cache_hit_tokens=0))

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    state = Cycle(_spec(tmp_path, max_iters=2), FakeExecutor(), FakeGate(pass_on_iter=99),
                  Memory("t", root=tmp_path / "r"), Budget(10.0, None, PRICING),
                  StubUI(), client).run(cwd=tmp_path)
    assert state.iters[0].fingerprint
    assert state.iters[0].lesson == "run the linter first"
    assert state.iters[0].category == "test-failure"


def test_passed_iteration_gets_no_lesson(tmp_path):
    state = Cycle(_spec(tmp_path), FakeExecutor(), FakeGate(pass_on_iter=1),
                  Memory("t", root=tmp_path / "r"), Budget(10.0, None, PRICING),
                  StubUI(), _plan_client()).run(cwd=tmp_path)
    assert state.iters[0].lesson == ""
    assert state.iters[0].fingerprint == ""


def test_lessons_reach_execute_task_text(tmp_path):
    import json
    lesson_json = json.dumps({"category": "c", "symptom": "s", "root_cause": "r",
                              "lesson": "never touch conftest"})

    def create(**kw):
        content = kw["messages"][0]["content"]
        text = lesson_json if "ANALYZE stage" in content else "plan (Lessons: none apply — first try)"
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                  prompt_cache_hit_tokens=0))

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))

    class TaskCapturingExecutor(FakeExecutor):
        def __init__(self):
            super().__init__()
            self.tasks = []
        def execute(self, system, task, tools, model, cwd, on_event):
            self.tasks.append(task)
            return super().execute(system, task, tools, model, cwd, on_event)

    ex = TaskCapturingExecutor()
    Cycle(_spec(tmp_path, max_iters=2), ex, FakeGate(pass_on_iter=99),
          Memory("t", root=tmp_path / "r"), Budget(10.0, None, PRICING),
          StubUI(), client).run(cwd=tmp_path)
    assert "never touch conftest" not in ex.tasks[0]   # iter 1: no lessons yet
    assert "never touch conftest" in ex.tasks[1]       # iter 2 sees iter 1's lesson
```

Update the three prompt-indexing tests to filter PLAN prompts. In `test_cutoff_executor_warns_the_next_plan`, `test_preflight_cold_feedback_seeds_first_plan`, and `test_clean_executor_adds_no_cutoff_note`, after prompts are captured, replace direct indexing with:

```python
    plans = [p for p in prompts if p.startswith("You are the PLAN stage")]
```

and assert against `plans[0]` / `plans[1]` (and `all(... for p in plans)` in the clean-executor test) instead of `prompts[0]` / `prompts[1]`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cycle.py -q`
Expected: the three new tests FAIL (no fingerprint recorded, no lessons in task text).

- [ ] **Step 3: Write the implementation**

In `setpoint/cycle.py`:

Add import:

```python
from setpoint.analyze import analyze
```

Add a helper method to `Cycle`:

```python
    def _lessons(self) -> list[tuple[str, str]]:
        """(fingerprint, lesson_text) pairs from this run, deduped, oldest first."""
        out: dict[str, str] = {}
        for r in self.memory.load().iters:
            if r.lesson and r.fingerprint not in out:
                out[r.fingerprint] = r.lesson
        return list(out.items())
```

In `run()`, before EXECUTE, build the lesson block and append to the task text — replace the `result = self.executor.execute(...)` call's `task=` argument:

```python
            lessons = self._lessons()
            task = (f"Working directory (all paths are relative to here): {cwd}\n"
                    f"Plan for this iteration:\n{plan}")
            if lessons:
                task += ("\n\nLessons from previous iterations "
                         "(do not repeat these mistakes):\n"
                         + "\n".join(f"- {t}" for _, t in lessons))
            result = self.executor.execute(
                system=_EXEC_SYSTEM.format(goal=self.spec.goal),
                task=task,
                tools=tools, model=self.spec.execute.model, cwd=cwd,
                on_event=lambda e: self.ui.tool(e.data.get("name", ""), e.data.get("args", {}))
                if e.kind == "tool" else None,
            )
```

After VERIFY, before building the `IterRecord`, add the ANALYZE step and thread its results in:

```python
            lesson = None
            analyze_usage = Usage()
            if not gate_result.passed:
                self.ui.stage("ANALYZE", i, self.spec.stop.max_iters)
                lesson, analyze_usage = analyze(
                    self.plan_client, self.spec.execute.plan_model,
                    plan, result.text, gate_result.feedback)
                self.budget.add(self.spec.execute.plan_model, analyze_usage)

            iter_usd = (plan_usage.cost(self.spec.execute.plan_model, self.budget.pricing)
                        + analyze_usage.cost(self.spec.execute.plan_model, self.budget.pricing)
                        + result.usage.cost(self.spec.execute.model, self.budget.pricing))
            rec = IterRecord(n=n, plan=plan, summary=result.text,
                             passed=gate_result.passed, feedback=gate_result.feedback,
                             usd=iter_usd, score=gate_result.score,
                             stop_reason=getattr(result, "stop_reason", "done"),
                             lesson=lesson.lesson if lesson else "",
                             category=lesson.category if lesson else "",
                             fingerprint=lesson.fingerprint if lesson else "")
```

(`Usage` is already imported in cycle.py via `from setpoint.budget import Budget, Usage` — extend the existing import if it only imports `Budget`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_cycle.py -q` — Expected: PASS.
Then: `python -m pytest tests/ -q` — Expected: full suite PASS.

- [ ] **Step 5: Commit**

```bash
git add setpoint/cycle.py tests/test_cycle.py
git commit -m "feat(cycle): ANALYZE stage distills lessons; EXECUTE sees them"
```

---

### Task 5: Cite-or-die in PLAN

**Files:**
- Modify: `setpoint/cycle.py`
- Test: `tests/test_cycle.py`

**Interfaces:**
- Consumes: `self._lessons()` from Task 4, `Memory.note()` from Task 3.
- Produces: `_plan(self, context, last, lessons)` — third parameter is the `list[tuple[str, str]]` pairs. When lessons exist and the client is not `is_noop`, the prompt requires a `Lessons:` line; a plan missing it is re-prompted exactly once; a second miss proceeds with `memory.note(...)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cycle.py`:

```python
def _lesson_client(plan_texts):
    """Returns lesson JSON for ANALYZE calls; pops plan_texts for PLAN calls.
    Records the full messages list of every call in .calls — a call's FIRST
    message identifies it (a cite-or-die re-prompt's last message is the
    corrective instruction, but its first is still the PLAN prompt)."""
    import json
    lesson_json = json.dumps({"category": "c", "symptom": "s", "root_cause": "r",
                              "lesson": "always run pytest before finishing"})
    calls = []

    def create(**kw):
        calls.append(kw["messages"])
        if "ANALYZE stage" in kw["messages"][0]["content"]:
            text = lesson_json
        else:
            text = plan_texts.pop(0) if plan_texts else "bare plan"
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                  prompt_cache_hit_tokens=0))

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    client.calls = calls
    return client


def _plan_calls(client):
    return [m for m in client.calls if "PLAN stage" in m[0]["content"]]


def test_plan_reprompted_once_when_lessons_line_missing(tmp_path):
    # iter 1: no lessons -> 1 plan call. iter 2: lessons exist, plan lacks the
    # line -> re-prompt once, second attempt also bare -> proceed + note.
    client = _lesson_client(["plan one", "bare plan", "still bare"])
    mem = Memory("t", root=tmp_path / "r")
    Cycle(_spec(tmp_path, max_iters=2), FakeExecutor(), FakeGate(pass_on_iter=99),
          mem, Budget(10.0, None, PRICING), StubUI(), client).run(cwd=tmp_path)
    plans = _plan_calls(client)
    assert len(plans) == 3                          # 1 (iter1) + 2 (iter2: original + re-prompt)
    assert "Lessons:" in plans[1][0]["content"]     # iter-2 prompt states the requirement
    assert "missing" in plans[2][-1]["content"]     # re-prompt call carries the corrective msg
    assert "omitted" in mem.log_path.read_text().lower()


def test_plan_with_lessons_line_not_reprompted(tmp_path):
    client = _lesson_client(["plan one",
                             "fix import\nLessons: L1 applies — will update all import sites"])
    Cycle(_spec(tmp_path, max_iters=2), FakeExecutor(), FakeGate(pass_on_iter=99),
          Memory("t", root=tmp_path / "r"), Budget(10.0, None, PRICING),
          StubUI(), client).run(cwd=tmp_path)
    assert len(_plan_calls(client)) == 2            # no re-prompt on iter 2


def test_first_iteration_prompt_has_no_lessons_requirement(tmp_path):
    client = _lesson_client(["plan one"])
    Cycle(_spec(tmp_path, max_iters=1), FakeExecutor(), FakeGate(pass_on_iter=99),
          Memory("t", root=tmp_path / "r"), Budget(10.0, None, PRICING),
          StubUI(), client).run(cwd=tmp_path)
    assert "Lessons:" not in _plan_calls(client)[0][0]["content"]


def test_noop_plan_client_skips_cite_or_die(tmp_path):
    from setpoint.executor.agent_plan import AgentPlanClient
    state = Cycle(_spec(tmp_path, max_iters=2), FakeExecutor(), FakeGate(pass_on_iter=99),
                  Memory("t", root=tmp_path / "r"), Budget(10.0, None, PRICING),
                  StubUI(), AgentPlanClient()).run(cwd=tmp_path)
    assert len(state.iters) == 2           # loop ran normally, no re-prompt wedging
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cycle.py -q`
Expected: new tests FAIL (plan prompt never mentions Lessons, no re-prompt happens).

- [ ] **Step 3: Write the implementation**

In `setpoint/cycle.py`, add the rule text next to the other prompts:

```python
_LESSONS_RULE = """
Lessons learned so far (respect these):
{lessons}

Your plan MUST include a line starting with "Lessons:" naming which lesson
IDs apply to this step and how the plan avoids repeating them, or exactly:
Lessons: none apply — <reason>
"""

_CITE_REPROMPT = ('Your plan is missing the required "Lessons:" line. '
                  'Reply with the same plan plus that line.')

_LESSONS_LINE = re.compile(r"^\s*lessons\s*:", re.IGNORECASE | re.MULTILINE)
```

(add `import re` at the top of cycle.py.)

Replace `_plan` with a lessons-aware version:

```python
    def _plan(self, context: str, last: IterRecord | None,
              lessons: list[tuple[str, str]]) -> tuple[str, Usage]:
        verdict = "failed" if last and not last.passed else "has not run yet"
        feedback_block = f"Failure feedback:\n{last.feedback}\n" if last and not last.passed else ""
        if last is not None and last.stop_reason != "done":
            feedback_block += _CUTOFF_NOTE.format(reason=last.stop_reason)
        enforce = bool(lessons) and not getattr(self.plan_client, "is_noop", False)
        if enforce:
            listing = "\n".join(f"- {fp}: {text}" for fp, text in lessons)
            feedback_block += _LESSONS_RULE.format(lessons=listing)
        prompt = _PLAN_PROMPT.format(
            goal=self.spec.goal, context=context,
            verdict=verdict, feedback_block=feedback_block)
        messages = [{"role": "user", "content": prompt}]
        plan, usage = self._plan_call(messages)
        if enforce and not _LESSONS_LINE.search(plan):
            messages += [{"role": "assistant", "content": plan},
                         {"role": "user", "content": _CITE_REPROMPT}]
            plan, usage2 = self._plan_call(messages)
            usage = Usage(usage.input_tokens + usage2.input_tokens,
                          usage.output_tokens + usage2.output_tokens,
                          usage.cache_read_tokens + usage2.cache_read_tokens)
            if not _LESSONS_LINE.search(plan):
                self.memory.note("PLAN omitted required Lessons line after re-prompt")
        return plan, usage

    def _plan_call(self, messages) -> tuple[str, Usage]:
        resp = with_retries(lambda: self.plan_client.chat.completions.create(
            model=self.spec.execute.plan_model, messages=messages,
        ))
        u = resp.usage
        usage = Usage(getattr(u, "prompt_tokens", 0) or 0,
                      getattr(u, "completion_tokens", 0) or 0,
                      getattr(u, "prompt_cache_hit_tokens", 0) or 0)
        return resp.choices[0].message.content or "", usage
```

In `run()`, hoist the `lessons = self._lessons()` call to before PLAN (it currently sits before EXECUTE from Task 4) and pass it through:

```python
            # PLAN
            self.ui.stage("PLAN", i, self.spec.stop.max_iters)
            lessons = self._lessons()
            plan, plan_usage = self._plan(context, last, lessons)
```

(EXECUTE keeps using the same `lessons` local.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_cycle.py -q` — Expected: PASS.
Then: `python -m pytest tests/ -q` — Expected: full suite PASS.

- [ ] **Step 5: Commit**

```bash
git add setpoint/cycle.py tests/test_cycle.py
git commit -m "feat(cycle): cite-or-die — plans must address known lessons"
```

---

### Task 6: Repeat detector feeds no-progress strikes

**Files:**
- Modify: `setpoint/cycle.py`
- Test: `tests/test_cycle.py`

**Interfaces:**
- Consumes: `is_repeat`, `normalize_feedback` from Task 1; `IterRecord.repeat_of` from Task 3.
- Produces: `Cycle.run` maintains `priors: list[tuple[str, str, str]]` of earlier failed iterations' `(fingerprint, normalized, category)`. A failure matching a prior sets `rec.repeat_of` and increments `no_progress` even when feedback text differs. Stage 2 extends `priors` with stored lessons.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cycle.py`:

```python
class VaryingPathGate:
    """Same underlying failure, cosmetically different feedback each call —
    old exact-text no-progress tracking never fires on this."""
    def __init__(self):
        self.calls = 0
    def verify(self, cwd, on_event):
        self.calls += 1
        return GateResult(passed=False,
                          feedback=f"FAILED /build{self.calls}/tests/test_x.py:{self.calls}0: "
                                   f"AssertionError: expected 200 got 500")


def test_repeat_fingerprint_counts_toward_no_progress(tmp_path):
    spec = _spec(tmp_path, max_iters=99, no_progress=3)
    state = Cycle(spec, FakeExecutor(), VaryingPathGate(),
                  Memory("t", root=tmp_path / "r"), Budget(100.0, None, PRICING),
                  StubUI(), _plan_client()).run(cwd=tmp_path)
    assert state.status == "stopped"
    assert len(state.iters) == 3            # struck out on repeats, not max_iters
    assert state.iters[1].repeat_of == state.iters[0].fingerprint
    assert state.iters[2].repeat_of == state.iters[0].fingerprint


def test_distinct_failures_reset_no_progress(tmp_path):
    class RotatingGate:
        def __init__(self):
            self.calls = 0
        def verify(self, cwd, on_event):
            self.calls += 1
            return GateResult(passed=False, feedback=f"totally different error kind {chr(64 + self.calls)}"
                              * (self.calls + 1))
    spec = _spec(tmp_path, max_iters=4, no_progress=3)
    state = Cycle(spec, FakeExecutor(), RotatingGate(),
                  Memory("t", root=tmp_path / "r"), Budget(100.0, None, PRICING),
                  StubUI(), _plan_client()).run(cwd=tmp_path)
    assert len(state.iters) == 4            # no premature strike-out
    assert all(r.repeat_of == "" for r in state.iters)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cycle.py -q`
Expected: `test_repeat_fingerprint_counts_toward_no_progress` FAILS (runs 99 iters or `repeat_of` empty).

- [ ] **Step 3: Write the implementation**

In `setpoint/cycle.py`, import the detector:

```python
from setpoint.analyze import analyze, is_repeat
```

(this replaces Task 4's `from setpoint.analyze import analyze` line.)

In `run()`, initialize priors before the loop (after `no_progress = 0`):

```python
        priors: list[tuple[str, str, str]] = []
```

In the ANALYZE block (Task 4), after `lesson, analyze_usage = analyze(...)`, compute the repeat before appending this failure to priors:

```python
                repeat_of = is_repeat(gate_result.feedback, lesson.category, priors) or ""
                priors.append((lesson.fingerprint, lesson.normalized, lesson.category))
```

(initialize `repeat_of = ""` next to `lesson = None` so the passed branch has it), then add `repeat_of=repeat_of` to the `IterRecord(...)` construction.

Update the no-progress tracking to count repeats:

```python
            # no-progress tracking: identical feedback OR a repeat of a known lesson
            if (last is not None and last.feedback == rec.feedback) or rec.repeat_of:
                no_progress += 1
            else:
                no_progress = 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_cycle.py -q` — Expected: PASS (including the pre-existing `test_cycle_no_progress_bailout`, whose identical-feedback gate now also fingerprint-matches — same outcome).
Then: `python -m pytest tests/ -q` — Expected: full suite PASS.

- [ ] **Step 5: Commit**

```bash
git add setpoint/cycle.py tests/test_cycle.py
git commit -m "feat(cycle): repeating a known lesson burns no-progress strikes"
```

---

## Stage 2 — Cross-run lesson store + scry export

### Task 7: Lesson store (`lessons.py`)

**Files:**
- Create: `setpoint/lessons.py`
- Test: `tests/test_lessons.py`

**Interfaces:**
- Consumes: nothing from the loop (stdlib + dataclasses).
- Produces:
  - `repo_key(repo: Path) -> str` — slug of the origin remote URL, else the resolved path.
  - `@dataclass StoredLesson(ts: str, run: str, goal: str, fingerprint: str, normalized: str, category: str, lesson: str, hits: int = 1, validated: bool = True)`
  - `class LessonStore(key: str, root: Path | None = None)` — root defaults to `$SETPOINT_LESSONS_ROOT` or `~/.setpoint/lessons`; file `<root>/<key>.jsonl`. Methods: `load() -> list[StoredLesson]` (skips corrupt lines), `top(k: int = 10) -> list[StoredLesson]` (hits desc, then ts desc), `promote(new: list[StoredLesson]) -> list[StoredLesson]` (dedupe by fingerprint incrementing `hits` + refreshing `ts`; cap 100, evict lowest hits then oldest; returns the list as written).
  - `CAP = 100`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_lessons.py
import json
import subprocess
from pathlib import Path

from setpoint.lessons import CAP, LessonStore, StoredLesson, repo_key


def _lesson(fp, ts="2026-08-07T00:00:00", hits=1, text="do the thing"):
    return StoredLesson(ts=ts, run="r", goal="g", fingerprint=fp,
                        normalized=f"norm {fp}", category="c", lesson=text, hits=hits)


def test_repo_key_uses_origin_remote(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin",
                    "git@github.com:jeffdhooton/setpoint.git"], check=True)
    key = repo_key(tmp_path)
    assert "jeffdhooton" in key and "setpoint" in key
    assert "/" not in key and ":" not in key


def test_repo_key_falls_back_to_path(tmp_path):
    key = repo_key(tmp_path)          # no git repo at all
    assert key and "/" not in key


def test_promote_and_load_roundtrip(tmp_path):
    store = LessonStore("k", root=tmp_path)
    store.promote([_lesson("aaa")])
    loaded = store.load()
    assert len(loaded) == 1 and loaded[0].fingerprint == "aaa"


def test_promote_dedupes_by_fingerprint_incrementing_hits(tmp_path):
    store = LessonStore("k", root=tmp_path)
    store.promote([_lesson("aaa", ts="2026-08-01T00:00:00")])
    store.promote([_lesson("aaa", ts="2026-08-07T00:00:00")])
    loaded = store.load()
    assert len(loaded) == 1
    assert loaded[0].hits == 2
    assert loaded[0].ts == "2026-08-07T00:00:00"


def test_cap_evicts_lowest_hits_then_oldest(tmp_path):
    store = LessonStore("k", root=tmp_path)
    old_low = _lesson("victim", ts="2026-01-01T00:00:00", hits=1)
    keepers = [_lesson(f"fp{i:03}", ts="2026-06-01T00:00:00", hits=2) for i in range(CAP - 1)]
    store.promote([old_low] + keepers)
    store.promote([_lesson("newcomer", ts="2026-08-07T00:00:00", hits=1)])
    fps = {sl.fingerprint for sl in store.load()}
    assert len(fps) == CAP
    assert "victim" not in fps and "newcomer" in fps


def test_top_ranks_hits_then_recency(tmp_path):
    store = LessonStore("k", root=tmp_path)
    store.promote([_lesson("low", ts="2026-08-07T00:00:00", hits=1),
                   _lesson("high", ts="2026-01-01T00:00:00", hits=5)])
    assert store.top(1)[0].fingerprint == "high"


def test_load_skips_corrupt_lines(tmp_path):
    store = LessonStore("k", root=tmp_path)
    store.promote([_lesson("aaa")])
    with store.path.open("a") as f:
        f.write("{not json\n")
    assert len(store.load()) == 1


def test_env_root_override(tmp_path, monkeypatch):
    monkeypatch.setenv("SETPOINT_LESSONS_ROOT", str(tmp_path / "custom"))
    store = LessonStore("k")
    assert store.path == tmp_path / "custom" / "k.jsonl"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_lessons.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'setpoint.lessons'`

- [ ] **Step 3: Write the implementation**

```python
# setpoint/lessons.py
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

CAP = 100


@dataclass
class StoredLesson:
    ts: str
    run: str
    goal: str
    fingerprint: str
    normalized: str
    category: str
    lesson: str
    hits: int = 1
    validated: bool = True


def repo_key(repo: Path) -> str:
    base = ""
    try:
        out = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            base = out.stdout.strip()
    except Exception:
        pass
    if not base:
        base = str(Path(repo).resolve())
    return re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")[:100]


class LessonStore:
    def __init__(self, key: str, root: Path | None = None):
        root = root or Path(os.environ.get(
            "SETPOINT_LESSONS_ROOT", str(Path.home() / ".setpoint" / "lessons")))
        self.path = Path(root) / f"{key}.jsonl"

    def load(self) -> list[StoredLesson]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                out.append(StoredLesson(**json.loads(line)))
            except (json.JSONDecodeError, TypeError):
                continue  # corrupt line: skip, keep going
        return out

    def top(self, k: int = 10) -> list[StoredLesson]:
        return sorted(self.load(), key=lambda sl: (sl.hits, sl.ts), reverse=True)[:k]

    def promote(self, new: list[StoredLesson]) -> list[StoredLesson]:
        by_fp = {sl.fingerprint: sl for sl in self.load()}
        for sl in new:
            if sl.fingerprint in by_fp:
                existing = by_fp[sl.fingerprint]
                existing.hits += 1
                existing.ts = max(existing.ts, sl.ts)
                if sl.lesson:
                    existing.lesson = sl.lesson  # keep the freshest phrasing
            else:
                by_fp[sl.fingerprint] = sl
        kept = sorted(by_fp.values(), key=lambda sl: (sl.hits, sl.ts), reverse=True)[:CAP]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".jsonl.tmp")
        tmp.write_text("".join(json.dumps(asdict(sl)) + "\n" for sl in kept))
        tmp.replace(self.path)
        return kept
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_lessons.py -q` — Expected: PASS.
Then: `python -m pytest tests/ -q` — Expected: full suite PASS.

- [ ] **Step 5: Commit**

```bash
git add setpoint/lessons.py tests/test_lessons.py
git commit -m "feat(lessons): per-repo JSONL lesson store with dedupe and cap"
```

---

### Task 8: Promotion at run end + cross-run injection

**Files:**
- Modify: `setpoint/lessons.py` (add `promote_validated`)
- Modify: `setpoint/cycle.py` (optional `lesson_store` param: DISCOVER injection + priors seeding)
- Modify: `setpoint/__main__.py` (build store, pass to Cycle, promote after run)
- Test: `tests/test_lessons.py`, `tests/test_cycle.py`

**Interfaces:**
- Consumes: `RunState`/`IterRecord` (memory), `LessonStore`, `StoredLesson`, `repo_key` from Task 7.
- Produces:
  - `promote_validated(state: RunState, goal: str, store: LessonStore, now: str | None = None) -> list[StoredLesson]` in `lessons.py` — a failed iteration's lesson is validated when the next recorded iteration passed OR has a different fingerprint; only validated, non-empty lessons are promoted; returns the promoted subset (new/refreshed entries handed to `store.promote`).
  - `Cycle.__init__(..., lesson_store=None)` — when set, `_discover()` appends `## Lessons from previous runs` (top-10) and `run()` seeds `priors` with stored `(fingerprint, normalized, category)` triples so a first-iteration failure matching a stored lesson strikes immediately; `_lessons()` includes stored lessons (stored first, then in-run).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_lessons.py`:

```python
def test_promote_validated_rules(tmp_path):
    # A validated by progress (next iter's fingerprint differs), B validated by
    # the pass that follows it.
    from setpoint.lessons import promote_validated
    from setpoint.memory import IterRecord, RunState
    state = RunState(name="t", status="passed", iters=[
        IterRecord(n=1, plan="", summary="", passed=False, feedback="f1", usd=0,
                   lesson="lesson A", fingerprint="fpA"),
        IterRecord(n=2, plan="", summary="", passed=False, feedback="f2", usd=0,
                   lesson="lesson B", fingerprint="fpB"),
        IterRecord(n=3, plan="", summary="", passed=True, feedback="ok", usd=0),
    ])
    store = LessonStore("k", root=tmp_path)
    promoted = promote_validated(state, "the goal", store)
    fps = {sl.fingerprint for sl in promoted}
    assert fps == {"fpA", "fpB"}
    assert all(sl.goal == "the goal" for sl in promoted)


def test_promote_validated_skips_unvalidated_tail_and_repeats(tmp_path):
    from setpoint.lessons import promote_validated
    from setpoint.memory import IterRecord, RunState
    state = RunState(name="t", status="stopped", iters=[
        IterRecord(n=1, plan="", summary="", passed=False, feedback="f", usd=0,
                   lesson="lesson A", fingerprint="fpA"),
        IterRecord(n=2, plan="", summary="", passed=False, feedback="f", usd=0,
                   lesson="lesson A", fingerprint="fpA"),  # same failure again
    ])
    store = LessonStore("k", root=tmp_path)
    assert promote_validated(state, "g", store) == []
    assert store.load() == []


def test_promote_validated_skips_empty_lessons(tmp_path):
    from setpoint.lessons import promote_validated
    from setpoint.memory import IterRecord, RunState
    state = RunState(name="t", status="passed", iters=[
        IterRecord(n=1, plan="", summary="", passed=False, feedback="f", usd=0,
                   lesson="", fingerprint="fpA"),   # fallback lesson: no text
        IterRecord(n=2, plan="", summary="", passed=True, feedback="ok", usd=0),
    ])
    store = LessonStore("k", root=tmp_path)
    assert promote_validated(state, "g", store) == []
```

Append to `tests/test_cycle.py`:

```python
def test_stored_lessons_injected_into_discover_and_strike(tmp_path):
    from setpoint.analyze import fingerprint, normalize_feedback
    from setpoint.lessons import LessonStore, StoredLesson
    fb = "FAILED tests/test_x.py:10: AssertionError: expected 200 got 500"
    store = LessonStore("k", root=tmp_path / "lessons")
    store.promote([StoredLesson(ts="2026-08-01T00:00:00", run="old", goal="g",
                                fingerprint=fingerprint(fb),
                                normalized=normalize_feedback(fb), category="assertion",
                                lesson="mock the upstream, don't hit the network")])

    class SameFailureGate:
        def verify(self, cwd, on_event):
            return GateResult(passed=False, feedback=fb)

    prompts = []

    def create(**kw):
        prompts.append(kw["messages"][-1]["content"])
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content="p\nLessons: applies"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                  prompt_cache_hit_tokens=0))

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    spec = _spec(tmp_path, max_iters=99, no_progress=2)
    state = Cycle(spec, FakeExecutor(), SameFailureGate(),
                  Memory("t", root=tmp_path / "r"), Budget(100.0, None, PRICING),
                  StubUI(), client, lesson_store=store).run(cwd=tmp_path)
    assert "mock the upstream" in prompts[0]                    # DISCOVER injection
    assert state.iters[0].repeat_of == fingerprint(fb)          # immediate strike
    assert state.status == "stopped" and len(state.iters) == 2  # struck out fast
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_lessons.py tests/test_cycle.py -q`
Expected: FAIL — `ImportError: cannot import name 'promote_validated'` and `TypeError: Cycle.__init__() got an unexpected keyword argument 'lesson_store'`.

- [ ] **Step 3: Write the implementation**

Append to `setpoint/lessons.py`:

```python
from datetime import datetime, timezone


def promote_validated(state, goal: str, store: LessonStore,
                      now: str | None = None) -> list[StoredLesson]:
    """Promote lessons whose next iteration passed or changed the failure.
    `state` is a setpoint.memory.RunState (duck-typed to avoid a cycle)."""
    ts = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    candidates = []
    iters = state.iters
    for i, r in enumerate(iters):
        if r.passed or not r.lesson or not r.fingerprint:
            continue
        if i + 1 >= len(iters):
            continue  # nothing after it: unvalidated
        nxt = iters[i + 1]
        if nxt.passed or (nxt.fingerprint and nxt.fingerprint != r.fingerprint):
            candidates.append(StoredLesson(
                ts=ts, run=state.name, goal=goal,
                fingerprint=r.fingerprint,
                normalized="",  # filled below from analyze to avoid storing raw feedback twice
                category=r.category, lesson=r.lesson))
    if not candidates:
        return []
    # normalized text is recomputable from feedback; store it for near-matching
    from setpoint.analyze import normalize_feedback
    by_fp = {r.fingerprint: r.feedback for r in iters if r.fingerprint}
    for sl in candidates:
        sl.normalized = normalize_feedback(by_fp.get(sl.fingerprint, ""))
    store.promote(candidates)
    return candidates
```

In `setpoint/cycle.py`:

- `__init__` gains `lesson_store=None` (keyword, after `abort_check`): `self.lesson_store = lesson_store`.
- Add a helper for stored lessons:

```python
    def _stored_lessons(self):
        if self.lesson_store is None:
            return []
        try:
            return self.lesson_store.top(10)
        except Exception:
            return []
```

- `_discover()` — after `parts.append(self.memory.context_block())`:

```python
        stored = self._stored_lessons()
        if stored:
            parts.append("## Lessons from previous runs\n"
                         + "\n".join(f"- [{sl.fingerprint}] {sl.lesson}" for sl in stored))
```

- `_lessons()` — include stored lessons first:

```python
    def _lessons(self) -> list[tuple[str, str]]:
        out: dict[str, str] = {}
        for sl in self._stored_lessons():
            if sl.lesson:
                out.setdefault(sl.fingerprint, sl.lesson)
        for r in self.memory.load().iters:
            if r.lesson and r.fingerprint not in out:
                out[r.fingerprint] = r.lesson
        return list(out.items())
```

- `run()` — seed priors from the store (replacing the bare initialization from Task 6):

```python
        priors: list[tuple[str, str, str]] = [
            (sl.fingerprint, sl.normalized, sl.category) for sl in self._stored_lessons()]
```

In `setpoint/__main__.py` `run_loop()`, after `memory = Memory(...)` add:

```python
    from setpoint.lessons import LessonStore, promote_validated, repo_key
    lesson_store = LessonStore(repo_key(spec.workspace.repo))
```

Pass it to the Cycle:

```python
        cycle = Cycle(spec, executor, gate, memory, budget, ui, plan_client,
                      abort_check=abort_check, lesson_store=lesson_store)
        state = cycle.run(cwd=cwd)
        promoted = promote_validated(state, spec.goal, lesson_store)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_lessons.py tests/test_cycle.py tests/test_cli.py -q` — Expected: PASS.
Then: `python -m pytest tests/ -q` — Expected: full suite PASS.

- [ ] **Step 5: Commit**

```bash
git add setpoint/lessons.py setpoint/cycle.py setpoint/__main__.py tests/test_lessons.py tests/test_cycle.py
git commit -m "feat(lessons): validated lessons persist per repo and seed new runs"
```

---

### Task 9: `memory` spec block + scry export adapter

**Files:**
- Modify: `setpoint/spec.py` (add `MemoryCfg`, parse `memory:` block)
- Create: `setpoint/scry_export.py`
- Modify: `setpoint/__main__.py` (export after promotion)
- Test: `tests/test_spec.py`, `tests/test_scry_export.py`

**Interfaces:**
- Consumes: `StoredLesson` from Task 7.
- Produces: `@dataclass MemoryCfg(scry_export: bool = False)`; `LoopSpec.memory: MemoryCfg` (default factory). `export_lessons(lessons: list[StoredLesson], repo: Path) -> int` in `scry_export.py` — shells `scry memory remember "<fact>" --repo <repo>` per lesson with a 10s timeout; returns count succeeded; never raises.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_spec.py` (follow that file's existing spec-YAML fixture pattern — write a minimal valid spec YAML to `tmp_path` the same way its other tests do, adding the `memory:` block):

```python
def test_spec_parses_memory_block(tmp_path):
    p = tmp_path / "s.setpoint.yaml"
    p.write_text("""
name: t
goal: g
type: coding
workspace: {repo: /tmp/x}
verify: {gate: command, command: "true"}
memory: {scry_export: true}
""")
    from setpoint.spec import load_spec
    spec = load_spec(str(p))
    assert spec.memory.scry_export is True


def test_spec_memory_defaults_off(tmp_path):
    p = tmp_path / "s.setpoint.yaml"
    p.write_text("""
name: t
goal: g
type: coding
workspace: {repo: /tmp/x}
verify: {gate: command, command: "true"}
""")
    from setpoint.spec import load_spec
    assert load_spec(str(p)).memory.scry_export is False
```

Create `tests/test_scry_export.py`:

```python
from pathlib import Path

from setpoint.lessons import StoredLesson
from setpoint.scry_export import export_lessons


def _lesson(text="update imports after renames"):
    return StoredLesson(ts="2026-08-07T00:00:00", run="r", goal="fix the tests",
                        fingerprint="abc", normalized="n", category="import-error",
                        lesson=text)


def test_export_shells_scry_memory_remember(monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        class R: returncode = 0
        return R()

    monkeypatch.setattr("setpoint.scry_export.subprocess.run", fake_run)
    n = export_lessons([_lesson()], Path("/tmp/repo"))
    assert n == 1
    argv = calls[0]
    assert argv[:3] == ["scry", "memory", "remember"]
    assert "update imports after renames" in argv[3]
    assert "fix the tests" in argv[3]        # fact mentions the goal
    assert "--repo" in argv and "/tmp/repo" in argv


def test_export_swallows_missing_binary(monkeypatch):
    def boom(argv, **kw):
        raise FileNotFoundError("scry not on PATH")
    monkeypatch.setattr("setpoint.scry_export.subprocess.run", boom)
    assert export_lessons([_lesson()], Path("/tmp/repo")) == 0   # no raise


def test_export_counts_only_successes(monkeypatch):
    codes = iter([0, 1])

    def fake_run(argv, **kw):
        class R: pass
        r = R(); r.returncode = next(codes)
        return r

    monkeypatch.setattr("setpoint.scry_export.subprocess.run", fake_run)
    assert export_lessons([_lesson("a"), _lesson("b")], Path("/tmp/repo")) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_spec.py tests/test_scry_export.py -q`
Expected: FAIL — `AttributeError: 'LoopSpec' object has no attribute 'memory'` and missing module.

- [ ] **Step 3: Write the implementation**

In `setpoint/spec.py`:

```python
@dataclass
class MemoryCfg:
    scry_export: bool = False
```

Add to `LoopSpec` (after `deliver`):

```python
    memory: MemoryCfg = field(default_factory=MemoryCfg)
```

In `load_spec`, before the final `return`:

```python
    m_raw = raw.get("memory") or {}
    memory = MemoryCfg(scry_export=bool(m_raw.get("scry_export", False)))
```

and add `memory=memory` to the `LoopSpec(...)` constructor call.

Create `setpoint/scry_export.py`:

```python
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from setpoint.lessons import StoredLesson


def export_lessons(lessons: list[StoredLesson], repo: Path) -> int:
    """Best-effort push of promoted lessons into scry's global memory.
    Shells the `scry` binary; any failure logs one line and moves on."""
    ok = 0
    for sl in lessons:
        fact = (f"setpoint lesson ({sl.category or 'general'}) while working on "
                f"'{sl.goal}': {sl.lesson}")
        try:
            r = subprocess.run(
                ["scry", "memory", "remember", fact, "--repo", str(repo)],
                capture_output=True, timeout=10)
            if r.returncode == 0:
                ok += 1
        except Exception as e:
            print(f"scry export skipped: {e}", file=sys.stderr)
            return ok
    return ok
```

In `setpoint/__main__.py` `run_loop()`, right after the `promoted = promote_validated(...)` line from Task 8:

```python
        if spec.memory.scry_export and promoted:
            from setpoint.scry_export import export_lessons
            export_lessons(promoted, spec.workspace.repo)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_spec.py tests/test_scry_export.py -q` — Expected: PASS.
Then: `python -m pytest tests/ -q` — Expected: full suite PASS.

- [ ] **Step 5: Commit**

```bash
git add setpoint/spec.py setpoint/scry_export.py setpoint/__main__.py tests/test_spec.py tests/test_scry_export.py
git commit -m "feat(memory): optional best-effort lesson export to scry global memory"
```

---

## Stage 3 — Engine self-tuning with rollback

### Task 10: `execute.max_turns` spec field + explicit-knob tracking

**Files:**
- Modify: `setpoint/spec.py` (field, parse, `explicit` list)
- Modify: `setpoint/__main__.py` (`_build_executor` plumbs it)
- Test: `tests/test_spec.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `DeepSeekExecutor(client, pricing, max_turns=25)` (existing constructor).
- Produces: `ExecuteCfg.max_turns: int = 25`, `ExecuteCfg.plan_hint: str = ""` (never parsed from YAML — reserved for the overlay); `LoopSpec.explicit: list[str]` recording `"execute.max_turns"` / `"stop.no_progress_after"` when present in the YAML. `_build_executor` passes `max_turns=spec.execute.max_turns` to `DeepSeekExecutor`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_spec.py`:

```python
def test_spec_parses_max_turns_and_tracks_explicit(tmp_path):
    p = tmp_path / "s.setpoint.yaml"
    p.write_text("""
name: t
goal: g
type: coding
workspace: {repo: /tmp/x}
execute: {max_turns: 40}
verify: {gate: command, command: "true"}
stop: {no_progress_after: 3}
""")
    from setpoint.spec import load_spec
    spec = load_spec(str(p))
    assert spec.execute.max_turns == 40
    assert "execute.max_turns" in spec.explicit
    assert "stop.no_progress_after" in spec.explicit


def test_spec_max_turns_default_not_explicit(tmp_path):
    p = tmp_path / "s.setpoint.yaml"
    p.write_text("""
name: t
goal: g
type: coding
workspace: {repo: /tmp/x}
verify: {gate: command, command: "true"}
""")
    from setpoint.spec import load_spec
    spec = load_spec(str(p))
    assert spec.execute.max_turns == 25
    assert spec.explicit == []
    assert spec.execute.plan_hint == ""
```

Append to `tests/test_cli.py`:

```python
def test_build_executor_passes_max_turns(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    from setpoint.__main__ import _build_executor
    from setpoint.spec import (BudgetCfg, Context, ExecuteCfg, LoopSpec,
                               StopCfg, VerifyCfg, Workspace)
    spec = LoopSpec(name="t", goal="g", type="coding",
                    workspace=Workspace(repo=tmp_path), context=Context(),
                    execute=ExecuteCfg(max_turns=33), verify=VerifyCfg(command="true"),
                    stop=StopCfg(), budget=BudgetCfg())
    ex = _build_executor(spec)
    assert ex.max_turns == 33
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_spec.py tests/test_cli.py -q`
Expected: FAIL — `TypeError: ExecuteCfg.__init__() got an unexpected keyword argument 'max_turns'`.

- [ ] **Step 3: Write the implementation**

In `setpoint/spec.py`:

```python
@dataclass
class ExecuteCfg:
    plan_model: str = "deepseek-v4-pro"
    model: str = "deepseek-v4-flash"
    engine: str = "deepseek"
    tools: list[str] = field(default_factory=lambda: ["read", "write", "edit", "bash"])
    max_turns: int = 25
    plan_hint: str = ""  # overlay-injected; never read from YAML
```

Add to `LoopSpec` (after `memory`):

```python
    explicit: list[str] = field(default_factory=list)
```

In `load_spec` — extend the `ExecuteCfg(...)` construction with `max_turns=int(ex_raw.get("max_turns", 25))`, then before the final return:

```python
    explicit = []
    if "max_turns" in ex_raw:
        explicit.append("execute.max_turns")
    if s_raw.get("no_progress_after") is not None:
        explicit.append("stop.no_progress_after")
```

and add `explicit=explicit` to the `LoopSpec(...)` call.

In `setpoint/__main__.py` `_build_executor`, change the DeepSeek branch:

```python
    return DeepSeekExecutor(client=make_deepseek_client(), pricing=PRICING,
                            max_turns=spec.execute.max_turns)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_spec.py tests/test_cli.py -q` — Expected: PASS.
Then: `python -m pytest tests/ -q` — Expected: full suite PASS.

- [ ] **Step 5: Commit**

```bash
git add setpoint/spec.py setpoint/__main__.py tests/test_spec.py tests/test_cli.py
git commit -m "feat(spec): execute.max_turns field with explicit-knob tracking"
```

---

### Task 11: Tuning overlay (`tuning.py`)

**Files:**
- Create: `setpoint/tuning.py`
- Test: `tests/test_tuning.py`

**Interfaces:**
- Consumes: `LoopSpec` (duck-typed: `spec.execute.max_turns`, `spec.execute.plan_hint`, `spec.stop.no_progress_after`, `spec.explicit`).
- Produces:
  - `BOUNDS = {"max_turns": (10, 50), "no_progress_after": (2, 6)}`, `PLAN_HINT_MAX = 400`.
  - `slug(text: str) -> str`.
  - `better_or_equal(a: dict, b: dict) -> bool` — lexicographic on `(passed, -iters, -usd)`.
  - `class Overlay(key: str, root: Path | None = None)` — file `<root>/<key>.json`, root from `$SETPOINT_TUNING_ROOT` or `~/.setpoint/tuning`. Methods: `load() -> dict` (current knobs; `{}` on missing/corrupt), `push(knobs: dict, stats: dict) -> None` (append version), `reconcile(run_stats: dict) -> str` returning `"kept"` (better/equal: rebaseline), `"reverted"` (worse: pop version), or `"empty"` (no versions).
  - `apply_overlay(spec, knobs: dict) -> None` — clamps to BOUNDS, truncates `plan_hint`, skips any knob named in `spec.explicit`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tuning.py
import json
from types import SimpleNamespace

from setpoint.tuning import (BOUNDS, Overlay, apply_overlay, better_or_equal,
                             slug)


def _spec(max_turns=25, no_progress=None, explicit=()):
    return SimpleNamespace(
        execute=SimpleNamespace(max_turns=max_turns, plan_hint=""),
        stop=SimpleNamespace(no_progress_after=no_progress),
        explicit=list(explicit))


def test_better_or_equal_ordering():
    assert better_or_equal({"passed": True, "iters": 5, "usd": 1.0},
                           {"passed": False, "iters": 1, "usd": 0.1})
    assert better_or_equal({"passed": True, "iters": 3, "usd": 2.0},
                           {"passed": True, "iters": 4, "usd": 0.5})
    assert not better_or_equal({"passed": True, "iters": 4, "usd": 2.0},
                               {"passed": True, "iters": 4, "usd": 0.5})
    assert better_or_equal({"passed": True, "iters": 4, "usd": 0.5},
                           {"passed": True, "iters": 4, "usd": 0.5})


def test_apply_overlay_sets_and_clamps(tmp_path):
    spec = _spec()
    apply_overlay(spec, {"max_turns": 999, "no_progress_after": 1,
                         "plan_hint": "x" * 999})
    assert spec.execute.max_turns == BOUNDS["max_turns"][1]        # clamped to 50
    assert spec.stop.no_progress_after == BOUNDS["no_progress_after"][0]  # clamped to 2
    assert len(spec.execute.plan_hint) == 400


def test_apply_overlay_respects_explicit_user_values(tmp_path):
    spec = _spec(max_turns=40, no_progress=3,
                 explicit=["execute.max_turns", "stop.no_progress_after"])
    apply_overlay(spec, {"max_turns": 20, "no_progress_after": 5, "plan_hint": "h"})
    assert spec.execute.max_turns == 40
    assert spec.stop.no_progress_after == 3
    assert spec.execute.plan_hint == "h"     # hint is never user-set, always applies


def test_overlay_push_load_roundtrip(tmp_path):
    ov = Overlay("k", root=tmp_path)
    assert ov.load() == {}
    ov.push({"max_turns": 35}, {"passed": False, "iters": 8, "usd": 1.0})
    assert ov.load() == {"max_turns": 35}


def test_reconcile_reverts_on_worse(tmp_path):
    ov = Overlay("k", root=tmp_path)
    ov.push({"max_turns": 30}, {"passed": True, "iters": 3, "usd": 0.5})
    ov.push({"max_turns": 40}, {"passed": True, "iters": 3, "usd": 0.5})
    assert ov.load() == {"max_turns": 40}
    assert ov.reconcile({"passed": False, "iters": 8, "usd": 2.0}) == "reverted"
    assert ov.load() == {"max_turns": 30}


def test_reconcile_keeps_and_rebaselines_on_better(tmp_path):
    ov = Overlay("k", root=tmp_path)
    ov.push({"max_turns": 30}, {"passed": False, "iters": 8, "usd": 1.0})
    assert ov.reconcile({"passed": True, "iters": 2, "usd": 0.2}) == "kept"
    raw = json.loads(ov.path.read_text())
    assert raw["versions"][-1]["stats"]["passed"] is True   # new baseline


def test_reconcile_empty_overlay(tmp_path):
    assert Overlay("k", root=tmp_path).reconcile({"passed": True, "iters": 1, "usd": 0}) == "empty"


def test_corrupt_overlay_ignored(tmp_path):
    ov = Overlay("k", root=tmp_path)
    ov.path.parent.mkdir(parents=True, exist_ok=True)
    ov.path.write_text("{broken")
    assert ov.load() == {}
    assert ov.reconcile({"passed": True, "iters": 1, "usd": 0}) == "empty"


def test_slug():
    assert slug("My Spec.setpoint.yaml") == "my-spec-setpoint-yaml"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_tuning.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'setpoint.tuning'`

- [ ] **Step 3: Write the implementation**

```python
# setpoint/tuning.py
from __future__ import annotations

import json
import os
import re
from pathlib import Path

# The full self-tuning surface. Anything not listed here (gate, budget,
# delivery, max_iters, models) is off-limits to RETRO by construction.
BOUNDS = {"max_turns": (10, 50), "no_progress_after": (2, 6)}
PLAN_HINT_MAX = 400


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80]


def better_or_equal(a: dict, b: dict) -> bool:
    """Lexicographic run-outcome comparison: passing beats not, then fewer
    iterations, then cheaper."""
    ka = (bool(a.get("passed")), -int(a.get("iters", 0)), -float(a.get("usd", 0.0)))
    kb = (bool(b.get("passed")), -int(b.get("iters", 0)), -float(b.get("usd", 0.0)))
    return ka >= kb


def _clamp(name: str, value: int) -> int:
    lo, hi = BOUNDS[name]
    return max(lo, min(hi, int(value)))


def apply_overlay(spec, knobs: dict) -> None:
    if not knobs:
        return
    if "max_turns" in knobs and "execute.max_turns" not in spec.explicit:
        spec.execute.max_turns = _clamp("max_turns", knobs["max_turns"])
    if "no_progress_after" in knobs and "stop.no_progress_after" not in spec.explicit:
        spec.stop.no_progress_after = _clamp("no_progress_after", knobs["no_progress_after"])
    if knobs.get("plan_hint"):
        spec.execute.plan_hint = str(knobs["plan_hint"])[:PLAN_HINT_MAX]


class Overlay:
    def __init__(self, key: str, root: Path | None = None):
        root = root or Path(os.environ.get(
            "SETPOINT_TUNING_ROOT", str(Path.home() / ".setpoint" / "tuning")))
        self.path = Path(root) / f"{key}.json"

    def _read(self) -> dict:
        if not self.path.exists():
            return {"versions": []}
        try:
            data = json.loads(self.path.read_text())
            if not isinstance(data.get("versions"), list):
                return {"versions": []}
            return data
        except json.JSONDecodeError:
            return {"versions": []}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.path)

    def load(self) -> dict:
        versions = self._read()["versions"]
        return dict(versions[-1]["knobs"]) if versions else {}

    def push(self, knobs: dict, stats: dict) -> None:
        data = self._read()
        data["versions"].append({"knobs": knobs, "stats": stats})
        self._write(data)

    def reconcile(self, run_stats: dict) -> str:
        data = self._read()
        if not data["versions"]:
            return "empty"
        current = data["versions"][-1]
        if better_or_equal(run_stats, current.get("stats") or {}):
            current["stats"] = run_stats
            self._write(data)
            return "kept"
        data["versions"].pop()
        self._write(data)
        return "reverted"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_tuning.py -q` — Expected: PASS.
Then: `python -m pytest tests/ -q` — Expected: full suite PASS.

- [ ] **Step 5: Commit**

```bash
git add setpoint/tuning.py tests/test_tuning.py
git commit -m "feat(tuning): bounded knob overlay with versioned rollback"
```

---

### Task 12: RETRO pass + full wiring

**Files:**
- Create: `setpoint/retro.py`
- Modify: `setpoint/cycle.py` (plan_hint injection into `_plan`)
- Modify: `setpoint/__main__.py` (apply overlay before build; retro after run)
- Test: `tests/test_retro.py`, `tests/test_cycle.py`

**Interfaces:**
- Consumes: `RunState`/`IterRecord`, `Overlay`, `apply_overlay`, `better_or_equal`, `slug`, `BOUNDS`, `PLAN_HINT_MAX`.
- Produces:
  - `@dataclass RunStats(passed: bool, iters: int, usd: float, repeat_strikes: int, cutoffs: int)` and `compute_stats(state) -> RunStats` in `retro.py`.
  - `propose_knobs(stats: RunStats, current: dict, state) -> dict | None` — deterministic rules; None when nothing changes.
  - `run_retro(state, overlay: Overlay, out_dir: Path) -> Path` — reconciles, proposes, pushes, writes `<out_dir>/retro.md`, returns its path. Never raises.
  - Cycle: `spec.execute.plan_hint`, when non-empty, is appended to every PLAN prompt as `Standing guidance from previous runs: <hint>`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_retro.py
from pathlib import Path

from setpoint.memory import IterRecord, RunState
from setpoint.retro import RunStats, compute_stats, propose_knobs, run_retro
from setpoint.tuning import Overlay


def _iter(n, passed=False, stop_reason="done", repeat_of="", lesson="", fingerprint=""):
    return IterRecord(n=n, plan="p", summary="s", passed=passed, feedback="f",
                      usd=0.5, stop_reason=stop_reason, repeat_of=repeat_of,
                      lesson=lesson, fingerprint=fingerprint)


def test_compute_stats_counts_everything():
    state = RunState(name="t", status="passed", spent_usd=1.5, iters=[
        _iter(1, stop_reason="max_turns"),
        _iter(2, repeat_of="abc", stop_reason="max_turns"),
        _iter(3, passed=True),
    ])
    s = compute_stats(state)
    assert s.passed is True and s.iters == 3
    assert s.cutoffs == 2 and s.repeat_strikes == 1
    assert s.usd == 1.5


def test_propose_raises_max_turns_on_cutoffs():
    stats = RunStats(passed=False, iters=8, usd=1.0, repeat_strikes=0, cutoffs=2)
    knobs = propose_knobs(stats, {}, RunState(name="t"))
    assert knobs["max_turns"] == 35          # 25 default + 10


def test_propose_steps_max_turns_back_when_clean():
    stats = RunStats(passed=True, iters=2, usd=0.2, repeat_strikes=0, cutoffs=0)
    knobs = propose_knobs(stats, {"max_turns": 45}, RunState(name="t"))
    assert knobs["max_turns"] == 40          # decays toward the 25 default


def test_propose_lowers_no_progress_on_repeat_strikes():
    stats = RunStats(passed=False, iters=8, usd=1.0, repeat_strikes=3, cutoffs=0)
    knobs = propose_knobs(stats, {"no_progress_after": 4}, RunState(name="t"))
    assert knobs["no_progress_after"] == 3


def test_propose_plan_hint_from_most_repeated_lesson():
    state = RunState(name="t", status="stopped", iters=[
        _iter(1, lesson="pin the dep", fingerprint="abc"),
        _iter(2, repeat_of="abc", lesson="pin the dep", fingerprint="abc"),
        _iter(3, repeat_of="abc", lesson="pin the dep", fingerprint="abc"),
    ])
    stats = compute_stats(state)
    knobs = propose_knobs(stats, {}, state)
    assert "pin the dep" in knobs["plan_hint"]


def test_propose_none_when_nothing_to_change():
    stats = RunStats(passed=True, iters=2, usd=0.2, repeat_strikes=0, cutoffs=0)
    assert propose_knobs(stats, {}, RunState(name="t")) is None


def test_run_retro_writes_report_and_pushes(tmp_path):
    state = RunState(name="t", status="stopped", spent_usd=1.0, iters=[
        _iter(1, stop_reason="max_turns"), _iter(2, stop_reason="max_turns"),
    ])
    ov = Overlay("k", root=tmp_path / "tuning")
    path = run_retro(state, ov, tmp_path)
    assert path == tmp_path / "retro.md"
    text = path.read_text()
    assert "max_turns" in text
    assert ov.load()["max_turns"] == 35


def test_run_retro_never_raises(tmp_path, monkeypatch):
    ov = Overlay("k", root=tmp_path / "tuning")
    monkeypatch.setattr(ov, "push", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    path = run_retro(RunState(name="t", status="stopped",
                              iters=[_iter(1, stop_reason="max_turns"),
                                     _iter(2, stop_reason="max_turns")]),
                     ov, tmp_path)   # must not raise
    assert path is None              # retro skipped cleanly, exception swallowed
```

Append to `tests/test_cycle.py`:

```python
def test_plan_hint_reaches_plan_prompt(tmp_path):
    prompts = []

    def create(**kw):
        prompts.append(kw["messages"][-1]["content"])
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="plan"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                  prompt_cache_hit_tokens=0))

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    spec = _spec(tmp_path, max_iters=1)
    spec.execute.plan_hint = "Known pitfall: never edit conftest.py"
    Cycle(spec, FakeExecutor(), FakeGate(pass_on_iter=99),
          Memory("t", root=tmp_path / "r"), Budget(10.0, None, PRICING),
          StubUI(), client).run(cwd=tmp_path)
    assert "never edit conftest.py" in prompts[0]
```

(`_spec` in test_cycle.py builds a real `ExecuteCfg`, which has `plan_hint` after Task 10.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_retro.py tests/test_cycle.py -q`
Expected: FAIL — missing `setpoint.retro`; hint not in prompt.

- [ ] **Step 3: Write the implementation**

```python
# setpoint/retro.py
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from setpoint.tuning import BOUNDS, PLAN_HINT_MAX, Overlay

_DEFAULT_MAX_TURNS = 25
_DEFAULT_NO_PROGRESS = 4


def _clamp(name: str, value: int) -> int:
    lo, hi = BOUNDS[name]
    return max(lo, min(hi, int(value)))


@dataclass
class RunStats:
    passed: bool
    iters: int
    usd: float
    repeat_strikes: int
    cutoffs: int


def compute_stats(state) -> RunStats:
    return RunStats(
        passed=(state.status == "passed"),
        iters=len(state.iters),
        usd=state.spent_usd,
        repeat_strikes=sum(1 for r in state.iters if r.repeat_of),
        cutoffs=sum(1 for r in state.iters if r.stop_reason != "done"),
    )


def propose_knobs(stats: RunStats, current: dict, state) -> dict | None:
    knobs = dict(current)

    # Cut-off EXECUTE stages mean steps can't finish inside the turn budget.
    if stats.cutoffs >= 2:
        knobs["max_turns"] = _clamp(
            "max_turns", current.get("max_turns", _DEFAULT_MAX_TURNS) + 10)
    elif stats.cutoffs == 0 and current.get("max_turns", _DEFAULT_MAX_TURNS) > _DEFAULT_MAX_TURNS:
        knobs["max_turns"] = max(_DEFAULT_MAX_TURNS, current["max_turns"] - 5)

    # Repeated known mistakes with no pass: stop such runs sooner.
    if stats.repeat_strikes >= 2 and not stats.passed:
        knobs["no_progress_after"] = _clamp(
            "no_progress_after", current.get("no_progress_after", _DEFAULT_NO_PROGRESS) - 1)

    # Most-repeated lesson becomes standing guidance for the next run's plans.
    repeated = Counter(r.repeat_of for r in state.iters if r.repeat_of)
    if repeated:
        top_fp = repeated.most_common(1)[0][0]
        lesson = next((r.lesson for r in state.iters
                       if r.fingerprint == top_fp and r.lesson), "")
        if lesson:
            knobs["plan_hint"] = f"Known pitfall: {lesson}"[:PLAN_HINT_MAX]

    return knobs if knobs != current else None


def run_retro(state, overlay: Overlay, out_dir: Path) -> Path | None:
    """Post-run self-tuning. Never raises — a retro failure must not mark a
    passed run as failed."""
    try:
        stats = compute_stats(state)
        action = overlay.reconcile(asdict(stats))
        current = overlay.load()
        proposed = propose_knobs(stats, current, state)
        if proposed is not None:
            overlay.push(proposed, asdict(stats))
        lines = [
            "# Retro", "",
            f"- status: {state.status}",
            f"- iterations: {stats.iters}  cost: ${stats.usd:.4f}",
            f"- repeat strikes: {stats.repeat_strikes}  execute cutoffs: {stats.cutoffs}",
            f"- overlay reconcile: {action}",
            f"- knobs now: {overlay.load() or 'defaults'}",
        ]
        if proposed:
            lines.append(f"- proposed change: {proposed}")
        if stats.cutoffs >= 2:
            lines.append("- critique: EXECUTE was cut off repeatedly — steps too "
                         "big for the turn budget; raised max_turns.")
        if stats.repeat_strikes >= 2:
            lines.append("- critique: the loop repeated known mistakes — "
                         "tightened no-progress tolerance.")
        out = Path(out_dir) / "retro.md"
        out.write_text("\n".join(lines) + "\n")
        return out
    except Exception as e:
        import sys
        print(f"retro skipped: {e}", file=sys.stderr)
        return None
```

In `setpoint/cycle.py`, inject the hint in `_plan` — right after `prompt = _PLAN_PROMPT.format(...)`:

```python
        hint = getattr(self.spec.execute, "plan_hint", "")
        if hint:
            prompt += f"\nStanding guidance from previous runs: {hint}\n"
```

In `setpoint/__main__.py` `run_loop()` — apply the overlay before anything reads spec knobs (immediately after the `lesson_store = ...` line from Task 8):

```python
    from setpoint.tuning import Overlay, apply_overlay, slug
    overlay = Overlay(f"{repo_key(spec.workspace.repo)}--{slug(spec.name)}")
    apply_overlay(spec, overlay.load())
```

and after the promotion/export block, inside the `try`:

```python
        from setpoint.retro import run_retro
        run_retro(state, overlay, memory.root)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_retro.py tests/test_cycle.py tests/test_cli.py -q` — Expected: PASS.
Then: `python -m pytest tests/ -q` — Expected: full suite PASS.

- [ ] **Step 5: Commit**

```bash
git add setpoint/retro.py setpoint/cycle.py setpoint/__main__.py tests/test_retro.py tests/test_cycle.py
git commit -m "feat(retro): post-run self-tuning with bounded knobs and rollback"
```

---

## Final verification

- [ ] Run the whole suite: `python -m pytest tests/ -q` — everything green.
- [ ] Smoke the example loop end-to-end (needs no API key with agent engines; with DeepSeek key: `bash examples/setup.sh && setpoint run examples/coding.setpoint.yaml`) and confirm `~/.setpoint/runs/<name>/retro.md` exists and `setpoint ls` still works.
- [ ] Confirm `~/.setpoint/lessons/` and `~/.setpoint/tuning/` appear only after a run that produces lessons/proposals.
