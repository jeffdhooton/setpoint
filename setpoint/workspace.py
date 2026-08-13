from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


class Worktree:
    def __init__(self, repo: Path, branch: str, base: str | None = None):
        self.repo = Path(repo)
        self.branch = branch
        self.base = base
        self.path: Path | None = None
        # The ref create() actually branched from. "HEAD" means the fallback
        # fired (no origin, or the fetch failed) -- read it when a run's
        # starting point is in question.
        self.base_ref: str | None = None

    def _resolve_base_ref(self) -> str:
        """Fetch and return `origin/<base>`, or "HEAD" when that is not
        available. Cutting from the local checkout is the bug this guards:
        every worktree in the first fleet started 236 commits behind origin.
        We degrade to HEAD rather than failing, because local test repos and
        remote-less scratch repos are legitimate."""
        if not self.base:
            return "HEAD"
        fetch = subprocess.run(
            ["git", "fetch", "origin", self.base],
            cwd=self.repo, capture_output=True, text=True,
        )
        if fetch.returncode != 0:
            print(f"setpoint: `git fetch origin {self.base}` failed in {self.repo} "
                  f"— branching from local HEAD instead:\n{fetch.stderr.strip()}",
                  file=sys.stderr)
            return "HEAD"
        verify = subprocess.run(
            ["git", "rev-parse", "--verify", f"origin/{self.base}"],
            cwd=self.repo, capture_output=True, text=True,
        )
        if verify.returncode != 0:
            print(f"setpoint: origin/{self.base} does not resolve in {self.repo} "
                  f"— branching from local HEAD instead", file=sys.stderr)
            return "HEAD"
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
        return target

    def cleanup(self) -> None:
        if self.path is None:
            return
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(self.path)],
            cwd=self.repo, capture_output=True, text=True,
        )
        self.path = None


def _run_prepare(command: str, cwd: Path) -> None:
    print(f"setpoint: workspace.prepare — {command}")
    proc = subprocess.run(command, shell=True, cwd=cwd,
                          capture_output=True, text=True)
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
        if spec.workspace.prepare:
            try:
                _run_prepare(spec.workspace.prepare, cwd)
            except Exception:
                # Do not leak the worktree when prepare fails -- the run is
                # over before it started.
                wt.cleanup()
                raise
        return cwd, wt
    if spec.workspace.prepare:
        _run_prepare(spec.workspace.prepare, spec.workspace.repo)
    return spec.workspace.repo, None
