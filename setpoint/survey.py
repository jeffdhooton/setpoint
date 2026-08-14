"""Establish what is already true in a repo, before any task is planned.

The failure this exists to prevent: a fleet was planned from an issue whose
text had been stale for a month. Five agents spent two and a half hours
rebuilding a Settings surface that had shipped on 2026-07-14, and nothing in
the pipeline was capable of noticing — because nothing in the pipeline ever
looked at the repo before writing tasks.

The inverse failure matters just as much. The one genuinely unwired page in
that product had no issue filed at all, so no amount of reading the tracker
would ever have surfaced it. Work that needs doing is invisible; work already
done is what gets planned.

A survey runs before `decompose` and answers one question against the code
itself: what of this already exists, and what is missing that nobody asked
about?
"""
from __future__ import annotations

from pathlib import Path

SURVEY_PROMPT = """You are surveying a repository BEFORE any work is planned.
Your job is not to build anything. It is to establish what is already true, so
that a fleet of agents is not sent to build something that already exists.

The work someone is proposing:
{question}

Survey the repository at {repo}. Read the code. Where the repo has a way to run
itself (a dev script, a demo stage, a test suite), prefer evidence from running
it over evidence from documentation — issue text and progress docs go stale,
and a stale premise is exactly the failure this survey exists to catch.

Report in markdown, under these four headings:

## Already built
For each capability the proposal implies, say whether it EXISTS TODAY. Cite
evidence: file paths, symbol names, and the commit or date it landed where you
can find it. Be specific enough that a reader can check you. This section is
the most important one — anything listed here must not be planned as new work.

## Partially built
What exists but is incomplete, and precisely what is missing from it.

## Genuinely missing
What the proposal asks for that truly does not exist yet.

## Missing but unmentioned
Gaps you found that the proposal does NOT mention. Nobody filed these; that is
why they matter. Include them even when they are outside the proposal's scope.

## Verdict
One paragraph. If most of the proposal is already built, say so plainly and
say the proposal should be re-scoped or dropped. Do not soften this — a fleet
launched on a false premise wastes hours and produces PRs nobody can use.

Do not modify a single file. This is a read-only survey."""


def survey(question: str, repo: str, oneshot=None, engine: str = "claude") -> str:
    """Run a read-only survey of `repo` against `question`, returning markdown.

    Runs with cwd=repo: the agent's file access and sandbox are scoped to the
    process cwd, so surveying from anywhere else reads the wrong tree (or no
    tree at all).
    """
    if oneshot is None:
        from setpoint.decompose import _default_oneshot
        oneshot = _default_oneshot
    repo = str(Path(repo).expanduser().resolve())
    prompt = SURVEY_PROMPT.format(question=question.strip(), repo=repo)
    return oneshot(engine, prompt, cwd=repo)
