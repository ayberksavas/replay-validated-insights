"""Shared git runner. Fails loudly: a missing clone or bad ref must never look like 'no data'."""
import os
import subprocess


class GitError(RuntimeError):
    pass


def run_git(repo: str, *args: str) -> str:
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, errors="replace")
    if p.returncode:
        raise GitError(f"git {' '.join(args[:3])} failed in {repo!r}: {p.stderr.strip()[:300]}")
    return p.stdout


def require_repo(repo: str) -> None:
    """Raise early unless `repo` is itself a git clone. An empty dir inside another repo
    (e.g. data/<slug>/repo inside this project) must NOT resolve to the outer repo."""
    if not os.path.isdir(repo):
        raise GitError(f"clone not found: {repo!r}. Run `tm ingest <owner/repo>` first.")
    root = os.path.realpath(repo)
    p = subprocess.run(["git", "-C", root, "rev-parse", "--absolute-git-dir"], capture_output=True, text=True)
    gitdir = os.path.realpath(p.stdout.strip()) if p.returncode == 0 else None
    if gitdir not in (os.path.join(root, ".git"), root):
        raise GitError(f"{repo!r} is not a git clone (empty or inside another repo). Run `tm ingest <owner/repo>` first.")
