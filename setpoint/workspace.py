from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Ports are derived, never reused: two worktrees of the same repo run the
# same stack, and a reused port silently measures the *other* tree.
# 20000-39999 avoids the ephemeral range and common dev defaults.
_PORT_FLOOR = 20000
_PORT_SPAN = 20000


def port_base(worktree: Path) -> int:
    digest = hashlib.sha256(str(Path(worktree).resolve()).encode()).digest()
    return _PORT_FLOOR + int.from_bytes(digest[:4], "big") % _PORT_SPAN


class Worktree:
    def __init__(self, repo: Path, branch: str, base: str | None = None):
        self.repo = Path(repo)
        self.branch = branch
        self.base = base
        self.path: Path | None = None
        self.port_base: int | None = None
        # The ref create() actually branched from. "HEAD" means the fallback
        # fired (no origin, or the fetch failed) -- read it when a run's
        # starting point is in question.
        self.base_ref: str | None = None

    def _has_origin(self) -> bool:
        remotes = subprocess.run(
            ["git", "remote"], cwd=self.repo, capture_output=True, text=True,
        )
        return "origin" in remotes.stdout.split()

    def _resolve_base_ref(self) -> str:
        """Fetch and return `origin/<base>`, or "HEAD" when that is not
        available. Cutting from the local checkout is the bug this guards:
        every worktree in the first fleet started 236 commits behind origin.
        We degrade to HEAD only for repos with no `origin` at all, because
        local test repos and remote-less scratch repos are legitimate.

        When `origin` exists and the fetch fails we raise instead. A failed
        fetch against a real remote means we cannot know the remote state, and
        silently substituting a possibly-stale local HEAD is the exact bug this
        guards against — it produced three PRs gated against a four-commit-old
        tree, whose greens proved nothing about the branch they targeted."""
        if not self.base:
            return "HEAD"
        fetch = subprocess.run(
            ["git", "fetch", "origin", self.base],
            cwd=self.repo, capture_output=True, text=True,
        )
        if fetch.returncode != 0:
            if self._has_origin():
                raise RuntimeError(
                    f"`git fetch origin {self.base}` failed in {self.repo} "
                    f"(exit {fetch.returncode}). Refusing to branch from local "
                    f"HEAD: origin exists, so the local base may be stale and "
                    f"the run would verify against the wrong tree. Fix the "
                    f"fetch and retry.\n{fetch.stderr.strip()}")
            print(f"setpoint: `git fetch origin {self.base}` failed in {self.repo} "
                  f"and the repo has no `origin` — branching from local HEAD:\n"
                  f"{fetch.stderr.strip()}", file=sys.stderr)
            return "HEAD"
        verify = subprocess.run(
            ["git", "rev-parse", "--verify", f"origin/{self.base}"],
            cwd=self.repo, capture_output=True, text=True,
        )
        if verify.returncode != 0:
            print(f"setpoint: origin/{self.base} does not resolve in {self.repo} "
                  f"— branching from local HEAD instead", file=sys.stderr)
            return "HEAD"

        # origin/<base> is the right start point only when the local base is
        # stale (the failure this guards). When local is AHEAD, those unpushed
        # commits are usually the very work the run was launched to build on —
        # branching from origin would silently discard them, and the agent
        # would build against a tree that is missing files it was told to read.
        local = subprocess.run(
            ["git", "rev-parse", "--verify", self.base],
            cwd=self.repo, capture_output=True, text=True,
        )
        if local.returncode == 0:
            origin_is_ancestor = subprocess.run(
                ["git", "merge-base", "--is-ancestor", f"origin/{self.base}", self.base],
                cwd=self.repo, capture_output=True, text=True,
            ).returncode == 0
            if origin_is_ancestor:
                ahead = subprocess.run(
                    ["git", "rev-list", "--count", f"origin/{self.base}..{self.base}"],
                    cwd=self.repo, capture_output=True, text=True,
                ).stdout.strip() or "0"
                if ahead != "0":
                    print(f"setpoint: local {self.base} is {ahead} commit(s) ahead of "
                          f"origin/{self.base} — branching from the local branch so "
                          f"unpushed work is not lost. Push {self.base} if you meant "
                          f"the remote state.", file=sys.stderr)
                return self.base
        return f"origin/{self.base}"

    def create(self) -> Path:
        target = Path(tempfile.mkdtemp(prefix="setpoint-wt-"))
        self.base_ref = self._resolve_base_ref()
        # -B resets the branch if it already exists (resume-friendly). The
        # trailing ref is the start point: without it git uses the current
        # HEAD of the invoking checkout.
        subprocess.run(
            ["git", "worktree", "add", "-B", self.branch, str(target), self.base_ref],
            cwd=self.repo, check=True, capture_output=True, text=True,
        )
        self.path = target
        self.port_base = port_base(target)
        return target

    def cleanup(self) -> None:
        if self.path is None:
            return
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(self.path)],
            cwd=self.repo, capture_output=True, text=True,
        )
        self.path = None


def _run_prepare(command: str, cwd: Path, env: dict | None = None) -> None:
    print(f"setpoint: workspace.prepare — {command}")
    proc = subprocess.run(command, shell=True, cwd=cwd,
                          capture_output=True, text=True,
                          env={**os.environ, **(env or {})})
    if proc.returncode != 0:
        tail = ((proc.stdout or "") + (proc.stderr or "")).strip()[-2000:]
        raise RuntimeError(
            f"workspace.prepare failed (exit {proc.returncode}): {command}\n{tail}")


def prepare_workspace(spec) -> tuple[Path, Worktree | None]:
    if spec.workspace.worktree:
        branch = spec.workspace.branch or f"setpoint/{spec.name}"
        # The PR's base is the branch point: a member that PRs into `develop`
        # must be cut from origin/develop, not from main.
        base = (getattr(spec, "deliver", None) or {}).get("base") or "main"
        wt = Worktree(repo=spec.workspace.repo, branch=branch, base=base)
        cwd = wt.create()
        (cwd / ".setpoint-ports.env").write_text(
            f"SETPOINT_PORT_BASE={wt.port_base}\n")
        if spec.workspace.prepare:
            try:
                _run_prepare(spec.workspace.prepare, cwd,
                             {"SETPOINT_PORT_BASE": str(wt.port_base)})
            except Exception:
                # Do not leak the worktree when prepare fails -- the run is
                # over before it started.
                wt.cleanup()
                raise
        return cwd, wt
    if spec.workspace.prepare:
        _run_prepare(spec.workspace.prepare, spec.workspace.repo)
    return spec.workspace.repo, None
