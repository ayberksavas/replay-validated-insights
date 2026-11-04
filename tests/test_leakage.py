import os, subprocess, tempfile
import pytest
from timemachine.ingest_git import ingest_git
from timemachine.view import TimeView, LeakageError
from timemachine.snapshot import export_snapshot


@pytest.fixture
def repo():
    d = tempfile.mkdtemp()
    def run(*a, date):
        env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        subprocess.run(["git", "-C", d, *a], check=True, env=env, capture_output=True)
    run("init", "-q", date="2020-01-01T00:00:00Z")
    run("config", "user.email", "a@b.c", date="2020-01-01T00:00:00Z"); run("config", "user.name", "a", date="2020-01-01T00:00:00Z")
    for n, date in enumerate(["2020-01-01T00:00:00Z", "2020-06-01T00:00:00Z"]):
        open(f"{d}/f.py", "w").write(f"x={n}\n"); run("add", ".", date=date); run("commit", "-qm", f"c{n}", date=date)
    return d


def test_future_commits_invisible(repo):
    db = os.path.join(repo, "..", "t.db"); ingest_git(repo, db)
    shas = subprocess.run(["git", "-C", repo, "log", "--format=%H"], capture_output=True, text=True).stdout.split()
    v = TimeView(db, repo, 1580000000)  # ~2020-01-26
    assert len(v.commits()) == 1
    with pytest.raises(LeakageError):
        v.diff(shas[0])  # newest commit, after T
    assert v.diff(shas[1])


@pytest.mark.parametrize("history", [False, True])
def test_snapshot_has_no_future(repo, history):
    future, past = subprocess.run(["git", "-C", repo, "log", "--format=%H"], capture_output=True, text=True).stdout.split()
    subprocess.run(["git", "-C", repo, "tag", "v1", future], check=True)
    out = tempfile.mkdtemp()
    assert export_snapshot(repo, 1580000000, out, history=history) == past
    g = lambda *a: subprocess.run(["git", "-C", out, *a], capture_output=True, text=True)
    assert open(f"{out}/f.py").read() == "x=0\n"
    assert g("remote").stdout == "" and g("tag").stdout == ""
    assert g("cat-file", "-e", future).returncode != 0  # future object absent
    assert g("log", "--all", "--reflog", "--format=%H").stdout.split() == ([past] if history else g("rev-parse", "HEAD").stdout.split())
    assert not os.path.exists(f"{out}/.git/FETCH_HEAD")
    for root, _, files in os.walk(f"{out}/.git"):  # source path must not be recorded anywhere
        for f in files:
            assert os.path.basename(repo).encode() not in open(os.path.join(root, f), "rb").read()
    assert subprocess.run(["git", "-C", repo, "show-ref", "refs/tm/snapshot"]).returncode != 0  # temp ref cleaned


def test_snapshot_refuses_dest_inside_clone(repo):
    with pytest.raises(ValueError):
        export_snapshot(repo, 1580000000, os.path.join(repo, "snap"))
