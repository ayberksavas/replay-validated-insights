"""Export the repo as it was at T into a fresh directory for an agent (e.g. Claude Code) to work in.
A plain `git checkout <old sha>` is NOT safe: the future is still in .git (`git log --all`,
origin/*, tags, reflog). The export below has no remote, no tags, no reflog and no objects after T."""
import io
import os
import shutil
import subprocess
import tarfile
from datetime import datetime, timezone

REF = "refs/tm/snapshot"


def git(repo, *args, env=None):
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True,
                          text=True, errors="replace", env=env).stdout.strip()


def commit_at(repo: str, as_of: int, ref: str = "HEAD") -> str:
    """Last commit on the first-parent line of `ref` whose committer time is <= as_of."""
    sha = git(repo, "rev-list", "-1", "--first-parent", f"--before={as_of}", ref)
    if not sha:
        raise ValueError(f"no commit on {ref} at or before T={as_of}")
    return sha


def export_snapshot(repo: str, as_of: int, dest: str, history: bool = False) -> str:
    """Write the tree at T to `dest` as a new git repo. history=False: one commit, no past either.
    history=True: the real commits up to T (same shas), nothing after. Returns the source sha."""
    src, dest = os.path.realpath(repo), os.path.realpath(dest)
    if os.path.commonpath([src, dest]) in (src, dest):
        raise ValueError("dest must be outside the source clone (and vice versa)")
    if os.path.exists(dest) and os.listdir(dest):
        raise FileExistsError(f"{dest} is not empty")
    os.makedirs(dest, exist_ok=True)
    sha = commit_at(src, as_of)
    stamp = f"@{as_of} +0000"
    env = {**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    ident = ["-c", "user.name=timemachine", "-c", "user.email=timemachine@localhost"]
    git(dest, "init", "-q", "-b", "main")
    if history:
        # fetch only what is reachable from sha, via a temporary ref (local fetch by raw sha is refused)
        git(src, "update-ref", REF, sha)
        try:
            git(dest, "fetch", "-q", "--no-tags", src, f"{REF}:{REF}")
        finally:
            git(src, "update-ref", "-d", REF)
        git(dest, "checkout", "-q", "-B", "main", sha)
        git(dest, "update-ref", "-d", REF)
        # FETCH_HEAD records the source path
        os.remove(os.path.join(dest, ".git", "FETCH_HEAD"))
    else:
        tar = subprocess.run(["git", "-C", src, "archive", "--format=tar", sha],
                             check=True, capture_output=True).stdout
        with tarfile.open(fileobj=io.BytesIO(tar)) as t:
            t.extractall(dest, filter="data")
        git(dest, "add", "-A")
        day = datetime.fromtimestamp(as_of, timezone.utc).date()
        git(dest, *ident, "commit", "-q", "--allow-empty", "-m", f"snapshot as of {day}", env=env)
    shutil.rmtree(os.path.join(dest, ".git", "logs"), ignore_errors=True)  # reflog
    if git(dest, "remote"):
        raise RuntimeError("snapshot unexpectedly has a remote")
    return sha
