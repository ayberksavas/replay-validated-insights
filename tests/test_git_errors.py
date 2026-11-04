import os, subprocess, tempfile
import pytest
from timemachine.gitutil import GitError
from timemachine.groundtruth import Oracle
from timemachine.ingest_git import ingest_git
from timemachine.view import TimeView


def test_missing_or_empty_clone_fails_loudly():
    empty = tempfile.mkdtemp()  # like an empty data/<slug>/repo
    db = os.path.join(tempfile.mkdtemp(), "t.db")
    for bad in (empty, os.path.join(empty, "nope")):
        with pytest.raises(GitError):
            Oracle(db, bad)
        with pytest.raises(GitError):
            TimeView(db, bad, 0)


def test_szz_blames_introducer_and_skips_root():
    d = tempfile.mkdtemp()
    def run(*a, date):
        env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        return subprocess.run(["git", "-C", d, *a], check=True, env=env, capture_output=True, text=True).stdout.strip()
    run("init", "-q", date="2020-01-01T00:00:00Z")
    run("config", "user.email", "a@b.c", date="2020-01-01T00:00:00Z"); run("config", "user.name", "a", date="2020-01-01T00:00:00Z")
    steps = [("ok\n", "base", "2020-01-01T00:00:00Z"), ("bad\n", "add feature", "2020-02-01T00:00:00Z"), ("ok\n", "fix bug", "2020-03-01T00:00:00Z")]
    shas = []
    for content, msg, date in steps:
        open(f"{d}/f.py", "w").write(content); run("add", ".", date=date); run("commit", "-qm", msg, date=date)
        shas.append(run("rev-parse", "HEAD", date=date))
    db = os.path.join(tempfile.mkdtemp(), "t.db"); ingest_git(d, db)
    o = Oracle(db, d)
    assert o.introducers(shas[0]) == set()       # root commit: skipped, no error
    assert o.introducers(shas[2]) == {shas[1]}   # fix blames the commit that added "bad"
