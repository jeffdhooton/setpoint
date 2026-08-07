from __future__ import annotations

import re
from pathlib import Path

from setpoint.analyze import analyze
from setpoint.budget import Budget, Usage
from setpoint.memory import IterRecord, Memory, RunState
from setpoint.retry import with_retries
from setpoint.tools import build_registry

_CUTOFF_NOTE = """
NOTE: the previous EXECUTE stage was cut off ({reason}) before the agent
finished. The verify failure above may be unfinished work rather than a wrong
approach — plan a SMALLER step this iteration so it can complete.
"""

_PLAN_PROMPT = """You are the PLAN stage of a closed loop.
Goal: {goal}

Context:
{context}

The last verification {verdict}.
{feedback_block}
Produce a short, concrete plan for THIS iteration only — what to change/do next.
"""

_EXEC_SYSTEM = """You are the EXECUTE stage of a closed loop working toward this goal:
{goal}

Use the provided tools to do the work in the workspace. When the planned step is
complete, stop and briefly state what you did. Do not ask questions."""

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


class Cycle:
    def __init__(self, spec, executor, gate, memory: Memory, budget: Budget, ui,
                 plan_client, abort_check=None):
        self.spec = spec
        self.executor = executor
        self.gate = gate
        self.memory = memory
        self.budget = budget
        self.ui = ui
        self.plan_client = plan_client
        self.abort_check = abort_check

    def _discover(self) -> str:
        parts = [self.spec.context.notes] if self.spec.context.notes else []
        for f in self.spec.context.files:
            p = self.spec.workspace.repo / f
            if p.exists():
                parts.append(f"### {f}\n{p.read_text()[:4000]}")
        parts.append(self.memory.context_block())
        return "\n\n".join(parts)

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

    def _lessons(self) -> list[tuple[str, str]]:
        """(fingerprint, lesson_text) pairs from this run, deduped, oldest first."""
        out: dict[str, str] = {}
        for r in self.memory.load().iters:
            if r.lesson and r.fingerprint not in out:
                out[r.fingerprint] = r.lesson
        return list(out.items())

    def run(self, cwd: Path) -> RunState:
        self.memory.start()
        tools = build_registry(self.spec.execute.tools)
        last: IterRecord | None = None
        no_progress = 0
        prior = len(self.memory.load().iters)  # resume-aware: continue the spine's numbering

        # PREFLIGHT: run a cheap gate once cold, before any work. A gate whose
        # command can't even run (127/126) or that hangs cold can never pass —
        # abort now instead of burning max_iters discovering it.
        if (getattr(self.gate, "supports_preflight", False)
                and getattr(getattr(self.spec, "verify", None), "preflight", True)):
            self.ui.stage("PREFLIGHT", 0, self.spec.stop.max_iters)
            pre = self.gate.verify(cwd=cwd, on_event=lambda e: None)
            self.ui.verify(pre)
            if pre.timed_out or pre.returncode in (126, 127):
                self.memory.set_status("gate_error")
                state = self.memory.load()
                self.ui.summary(state)
                return state
            if not pre.passed:
                # seed iter 1's PLAN with the cold failure instead of "has not run yet"
                last = IterRecord(n=prior, plan="", summary="[preflight] gate run cold before any work",
                                  passed=False, feedback=pre.feedback, usd=0.0, score=pre.score)

        for i in range(1, self.spec.stop.max_iters + 1):
            n = prior + i  # persistent record label; `i` is the per-run counter (UI/cap)
            if self.budget.should_stop():
                self.memory.set_status("budget_exhausted")
                break

            if self.abort_check is not None and self.abort_check():
                self.memory.set_status("stopped")
                break

            # DISCOVER
            self.ui.stage("DISCOVER", i, self.spec.stop.max_iters)
            context = self._discover()

            # PLAN
            self.ui.stage("PLAN", i, self.spec.stop.max_iters)
            lessons = self._lessons()
            plan, plan_usage = self._plan(context, last, lessons)
            self.budget.add(self.spec.execute.plan_model, plan_usage)

            # EXECUTE
            self.ui.stage("EXECUTE", i, self.spec.stop.max_iters)
            if hasattr(self.executor, "set_deadline"):
                self.executor.set_deadline(self.budget.remaining_secs())
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
            self.budget.add(self.spec.execute.model, result.usage)

            # VERIFY
            self.ui.stage("VERIFY", i, self.spec.stop.max_iters)
            gate_result = self.gate.verify(cwd=cwd, on_event=lambda e: None)
            self.ui.verify(gate_result)

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
            self.memory.append(rec)

            # ITERATE
            if gate_result.passed:
                self.memory.set_status("passed")
                last = rec
                break

            # no-progress tracking: feedback unchanged from prior failing iter
            if last is not None and last.feedback == rec.feedback:
                no_progress += 1
            else:
                no_progress = 0
            last = rec
            if (self.spec.stop.no_progress_after is not None
                    and no_progress + 1 >= self.spec.stop.no_progress_after):
                self.memory.set_status("stopped")
                break
        else:
            self.memory.set_status("stopped")

        state = self.memory.load()
        self.ui.summary(state)
        return state
