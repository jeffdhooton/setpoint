from pathlib import Path
from types import SimpleNamespace

from setpoint.cycle import Cycle
from setpoint.spec import LoopSpec, Workspace, Context, ExecuteCfg, VerifyCfg, StopCfg, BudgetCfg
from setpoint.budget import Budget, Usage, PRICING
from setpoint.memory import Memory
from setpoint.gates import GateResult
from setpoint.executor.base import ExecuteResult


class StubUI:
    def __init__(self): self.events = []
    def stage(self, name, n, total): self.events.append(("stage", name, n))
    def tool(self, name, args): pass
    def verify(self, result): self.events.append(("verify", result.passed))
    def header(self, **kw): pass
    def summary(self, state): self.events.append(("summary", state.status))


class FakeExecutor:
    def __init__(self, usage_per=Usage(1000, 500, 0)):
        self.usage_per = usage_per
        self.calls = 0
    def execute(self, system, task, tools, model, cwd, on_event):
        self.calls += 1
        return ExecuteResult(text=f"did work {self.calls}", usage=self.usage_per)


class FakeGate:
    def __init__(self, pass_on_iter):
        self.pass_on_iter = pass_on_iter
        self.calls = 0
    def verify(self, cwd, on_event):
        self.calls += 1
        passed = self.calls >= self.pass_on_iter
        return GateResult(passed=passed, feedback="ok" if passed else "still failing")


def _plan_client(text="here is the plan"):
    def create(**kw):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5,
                                  prompt_cache_hit_tokens=0))
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _spec(tmp_path, max_iters=5, no_progress=None):
    return LoopSpec(name="t", goal="make it green", type="coding",
                    workspace=Workspace(repo=tmp_path, worktree=False, branch=None),
                    context=Context(notes="n"), execute=ExecuteCfg(tools=["read"]),
                    verify=VerifyCfg(command="true"),
                    stop=StopCfg(max_iters=max_iters, no_progress_after=no_progress),
                    budget=BudgetCfg(max_usd=10.0))


def test_cycle_stops_on_pass(tmp_path):
    spec = _spec(tmp_path)
    ex, gate, ui = FakeExecutor(), FakeGate(pass_on_iter=3), StubUI()
    mem = Memory("t", root=tmp_path / "runs")
    cyc = Cycle(spec, ex, gate, mem, Budget(10.0, None, PRICING), ui, _plan_client())
    state = cyc.run(cwd=tmp_path)
    assert state.status == "passed"
    assert len(state.iters) == 3
    assert ex.calls == 3


def test_cycle_stops_on_max_iters(tmp_path):
    spec = _spec(tmp_path, max_iters=2)
    ex, gate = FakeExecutor(), FakeGate(pass_on_iter=99)
    cyc = Cycle(spec, ex, gate, Memory("t", root=tmp_path / "r"),
                Budget(10.0, None, PRICING), StubUI(), _plan_client())
    state = cyc.run(cwd=tmp_path)
    assert state.status == "stopped"
    assert len(state.iters) == 2


def test_cycle_stops_on_budget(tmp_path):
    spec = _spec(tmp_path, max_iters=99)
    # each iter costs pro-rate; cap forces stop after ~1-2 iters
    ex = FakeExecutor(usage_per=Usage(1_000_000, 1_000_000, 0))  # ~$0.42 flash/iter
    cyc = Cycle(spec, ex, FakeGate(pass_on_iter=99),
                Memory("t", root=tmp_path / "r"),
                Budget(0.50, None, PRICING), StubUI(), _plan_client())
    state = cyc.run(cwd=tmp_path)
    assert state.status == "budget_exhausted"


def test_cycle_resume_continues_iter_numbering(tmp_path):
    # A resume (second run on the same memory) must continue the iteration labels
    # rather than restarting at 1 — so the spine reads 1,2,3,4 not 1,2,1,2.
    spec = _spec(tmp_path, max_iters=2)
    mem = Memory("t", root=tmp_path / "r")
    Cycle(spec, FakeExecutor(), FakeGate(pass_on_iter=99), mem,
          Budget(10.0, None, PRICING), StubUI(), _plan_client()).run(cwd=tmp_path)
    Cycle(spec, FakeExecutor(), FakeGate(pass_on_iter=99), mem,
          Budget(10.0, None, PRICING), StubUI(), _plan_client()).run(cwd=tmp_path)
    state = mem.load()
    assert [r.n for r in state.iters] == [1, 2, 3, 4]


def test_cycle_no_progress_bailout(tmp_path):
    spec = _spec(tmp_path, max_iters=99, no_progress=3)
    cyc = Cycle(spec, FakeExecutor(), FakeGate(pass_on_iter=99),
                Memory("t", root=tmp_path / "r"),
                Budget(100.0, None, PRICING), StubUI(), _plan_client())
    state = cyc.run(cwd=tmp_path)
    assert state.status == "stopped"
    assert len(state.iters) == 3  # bailed after 3 no-progress iters


def test_cycle_aborts_when_abort_check_true(tmp_path, monkeypatch):
    # A cycle whose abort_check() is True stops immediately with status "stopped".
    from types import SimpleNamespace
    from setpoint.cycle import Cycle
    from setpoint.memory import Memory
    from setpoint.budget import Budget, PRICING

    class _Gate:
        def verify(self, cwd, on_event):  # never reached
            from setpoint.gates import GateResult
            return GateResult(passed=True, feedback="", score=1.0)

    class _Exec:
        def execute(self, **kw):
            from setpoint.budget import Usage
            from setpoint.executor.base import ExecuteResult
            return ExecuteResult(text="", usage=Usage(), steps=[])

    class _UI:
        def stage(self, *a): ...
        def tool(self, *a): ...
        def verify(self, *a): ...
        def header(self, *a): ...
        def summary(self, *a): ...

    spec = SimpleNamespace(
        name="ab", goal="g",
        context=SimpleNamespace(notes="", files=[], scry=False),
        execute=SimpleNamespace(plan_model="m", model="m", engine="claude",
                                tools=["read"]),
        stop=SimpleNamespace(max_iters=5, no_progress_after=None),
        workspace=SimpleNamespace(repo=tmp_path),
    )
    from setpoint.executor.agent_plan import AgentPlanClient
    mem = Memory("ab", root=tmp_path / "runs")
    budget = Budget(None, None, PRICING)
    cyc = Cycle(spec, _Exec(), _Gate(), mem, budget, _UI(), AgentPlanClient(),
                abort_check=lambda: True)
    state = cyc.run(cwd=tmp_path)
    assert state.status == "stopped"
    assert len(state.iters) == 0


class UnrunnableGate:
    # exit 127: the verify command itself cannot run — not self-contained.
    supports_preflight = True

    def __init__(self):
        self.calls = 0

    def verify(self, cwd, on_event):
        self.calls += 1
        return GateResult(passed=False,
                          feedback="sh: demo:verify: command not found",
                          returncode=127)


def test_preflight_aborts_on_unrunnable_gate(tmp_path):
    ex = FakeExecutor()
    cyc = Cycle(_spec(tmp_path), ex, UnrunnableGate(), Memory("t", root=tmp_path / "r"),
                Budget(10.0, None, PRICING), StubUI(), _plan_client())
    state = cyc.run(cwd=tmp_path)
    assert state.status == "gate_error"
    assert ex.calls == 0  # no iterations burned on a gate that can never pass


def test_preflight_respects_spec_opt_out(tmp_path):
    spec = _spec(tmp_path, max_iters=2)
    spec.verify.preflight = False
    gate = UnrunnableGate()
    state = Cycle(spec, FakeExecutor(), gate, Memory("t", root=tmp_path / "r"),
                  Budget(10.0, None, PRICING), StubUI(), _plan_client()).run(cwd=tmp_path)
    assert state.status != "gate_error"
    assert gate.calls == 2  # ran as normal iterations only


def test_preflight_cold_feedback_seeds_first_plan(tmp_path):
    prompts = []

    def create(**kw):
        prompts.append(kw["messages"][0]["content"])
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="plan"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                  prompt_cache_hit_tokens=0))

    plan_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    class ColdFailGate:
        supports_preflight = True

        def __init__(self):
            self.calls = 0

        def verify(self, cwd, on_event):
            self.calls += 1
            if self.calls == 1:  # the cold preflight run
                return GateResult(passed=False, feedback="ECONNREFUSED :5290",
                                  returncode=1)
            return GateResult(passed=True, feedback="ok", returncode=0)

    cyc = Cycle(_spec(tmp_path), FakeExecutor(), ColdFailGate(),
                Memory("t", root=tmp_path / "r"), Budget(10.0, None, PRICING),
                StubUI(), plan_client)
    state = cyc.run(cwd=tmp_path)
    assert state.status == "passed"
    plans = [p for p in prompts if p.startswith("You are the PLAN stage")]
    assert "ECONNREFUSED" in plans[0]  # iter-1 plan already sees the cold failure


def test_cutoff_executor_warns_the_next_plan(tmp_path):
    # An EXECUTE that ran out of tool turns leaves half-finished work. The next
    # PLAN must be told, or it reads the gate failure as "wrong approach" and
    # rewrites working code.
    prompts = []

    def create(**kw):
        prompts.append(kw["messages"][0]["content"])
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="plan"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                  prompt_cache_hit_tokens=0))

    plan_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    class CutOffExecutor(FakeExecutor):
        def execute(self, system, task, tools, model, cwd, on_event):
            self.calls += 1
            return ExecuteResult(text="[hit max tool turns]", usage=self.usage_per,
                                 stop_reason="max_turns")

    mem = Memory("t", root=tmp_path / "r")
    state = Cycle(_spec(tmp_path, max_iters=2), CutOffExecutor(),
                  FakeGate(pass_on_iter=99), mem,
                  Budget(10.0, None, PRICING), StubUI(), plan_client).run(cwd=tmp_path)

    plans = [p for p in prompts if p.startswith("You are the PLAN stage")]
    assert "cut off" in plans[1].lower()      # iter 2's plan sees it
    assert "max_turns" in plans[1]
    assert "cut off" not in plans[0].lower()  # iter 1 had no prior iteration
    assert state.iters[0].stop_reason == "max_turns"  # and it persists to state.json


def test_clean_executor_adds_no_cutoff_note(tmp_path):
    prompts = []

    def create(**kw):
        prompts.append(kw["messages"][0]["content"])
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="plan"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                  prompt_cache_hit_tokens=0))

    plan_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    Cycle(_spec(tmp_path, max_iters=2), FakeExecutor(), FakeGate(pass_on_iter=99),
          Memory("t", root=tmp_path / "r"), Budget(10.0, None, PRICING),
          StubUI(), plan_client).run(cwd=tmp_path)
    plans = [p for p in prompts if p.startswith("You are the PLAN stage")]
    assert all("cut off" not in p.lower() for p in plans)


def test_cycle_retries_transient_plan_errors(tmp_path, monkeypatch):
    from setpoint import retry
    monkeypatch.setattr(retry, "_sleep", lambda s: None)

    class Transient(Exception):
        status_code = 503

    attempts = []

    def create(**kw):
        attempts.append(1)
        if len(attempts) < 3:
            raise Transient()
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="plan"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                  prompt_cache_hit_tokens=0))

    plan_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    state = Cycle(_spec(tmp_path, max_iters=1), FakeExecutor(), FakeGate(pass_on_iter=1),
                  Memory("t", root=tmp_path / "r"), Budget(10.0, None, PRICING),
                  StubUI(), plan_client).run(cwd=tmp_path)

    assert state.status == "passed"  # a 503 blip no longer kills the run
    assert len(attempts) == 3


def test_cycle_passes_wall_clock_deadline_to_executor(tmp_path):
    class DeadlineExec(FakeExecutor):
        def __init__(self):
            super().__init__()
            self.deadlines = []

        def set_deadline(self, remaining):
            self.deadlines.append(remaining)

    ex = DeadlineExec()
    cyc = Cycle(_spec(tmp_path, max_iters=1), ex, FakeGate(pass_on_iter=1),
                Memory("t", root=tmp_path / "r"),
                Budget(None, None, PRICING, wall_clock_secs=100), StubUI(), _plan_client())
    cyc.run(cwd=tmp_path)
    assert len(ex.deadlines) == 1
    assert ex.deadlines[0] is not None and 0 < ex.deadlines[0] <= 100


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
