"""Shared fixtures: a tiny repo with a known history plus hand-written issues/events/comments."""
import json, os, subprocess, tempfile
from datetime import datetime, timezone
import pytest
from timemachine.ingest_git import ingest_git
from timemachine.store import connect


def ts(s): return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())


@pytest.fixture
def world():
    d = tempfile.mkdtemp()
    def git(*a, date="2020-01-01T00:00:00Z"):
        env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        subprocess.run(["git", "-C", d, *a], check=True, env=env, capture_output=True)
    git("init", "-q"); git("config", "user.email", "a@b.c"); git("config", "user.name", "a")
    def commit(msg, files, date):
        for p, body in files.items():
            os.makedirs(os.path.dirname(f"{d}/{p}") or d, exist_ok=True)
            open(f"{d}/{p}", "w").write(body)
        git("add", "."); git("commit", "-qm", msg, date=date)
    commit("add parser", {"src/a.py": "a=1\n" * 5, "tests/test_a.py": "t=1\n"}, "2020-01-10T00:00:00Z")
    commit("Fix crash on empty config, fixes #1", {"src/a.py": "a=2\n" * 5}, "2020-02-10T00:00:00Z")
    commit("Revert \"Fix crash\"", {"src/a.py": "a=3\n" * 5}, "2020-02-20T00:00:00Z")
    commit("rewrite parser", {"src/a.py": "b=1\n" * 80, "src/b.py": "c=1\n" * 40}, "2020-03-10T00:00:00Z")
    commit("update readme", {"README.md": "hello\n"}, "2020-03-12T00:00:00Z")
    commit("future change", {"src/a.py": "z=1\n"}, "2020-09-01T00:00:00Z")
    db = os.path.join(d, "..", f"kb{os.path.basename(d)}.db")
    ingest_git(d, db)
    c = connect(db)
    def issue(n, date, title, body): c.execute("INSERT INTO issues VALUES(?,0,?,?,?,?)", (n, ts(date), "u", title, body))
    issue(1, "2020-02-01T00:00:00Z", "Crash when parsing empty config file", "Parser raises IndexError on an empty config")
    issue(2, "2020-02-05T00:00:00Z", "Typo in documentation", "readme has a typo")
    issue(3, "2020-05-01T00:00:00Z", "Parser crashes on empty configuration", "same IndexError as in #1")
    issue(9, "2020-10-01T00:00:00Z", "future issue", "after T")
    c.execute("INSERT INTO issue_events VALUES(1,?,?,?,?)", (ts("2020-02-01T01:00:00Z"), "labeled", "m", json.dumps({"label": "bug"})))
    c.execute("INSERT INTO issue_events VALUES(1,?,?,?,?)", (ts("2020-02-11T00:00:00Z"), "closed", "m", json.dumps({})))
    c.execute("INSERT INTO comments VALUES(1,3,?,?,?,?)", (ts("2020-06-01T00:00:00Z"), "m", "duplicate of #1", "issue"))
    c.commit(); c.close()
    return db, d


@pytest.fixture(scope="session")
def history():   # read-only for every test; built once (git on Windows is slow)
    """A year of history: features, later fixes that blame them (Output 1), test commits for the same
    stem (Output 2), duplicate issues (Output 3), and a bug label that arrives AFTER the commit that
    closed the issue (the KB must re-classify that commit as a fix)."""
    d = tempfile.mkdtemp()

    def git(*a, date="2020-01-01T00:00:00Z"):
        env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        subprocess.run(["git", "-C", d, *a], check=True, env=env, capture_output=True)

    git("init", "-q", "-b", "main"); git("config", "user.email", "a@b.c"); git("config", "user.name", "a")
    core = [f"line{i} = {i}\n" for i in range(40)]
    util = [f"u{i} = {i}\n" for i in range(10)]

    def commit(msg, date, core_lines=(), tag="", util_tag=None, extra=None):
        for i in core_lines:
            core[i % 40] = f"line{i % 40} = '{tag}'\n"
        if util_tag:
            util[len(util_tag) % 10] = f"u = '{util_tag}'\n"
        os.makedirs(f"{d}/src", exist_ok=True)
        open(f"{d}/src/core.py", "w").write("".join(core))
        open(f"{d}/src/util.py", "w").write("".join(util))
        for p, body in (extra or {}).items():
            os.makedirs(os.path.dirname(f"{d}/{p}"), exist_ok=True)
            open(f"{d}/{p}", "w").write(body)
        git("add", "."); git("commit", "-qm", msg, date=date)

    commit("Initial import", "2020-01-02T10:00:00Z", extra={"README.md": "hi\n"})
    for m in range(1, 13):
        commit(f"Add feature {m} to core", f"2020-{m:02d}-05T10:00:00Z", [3 * m, 3 * m + 1, 3 * m + 2], f"feat{m}")
        if m >= 3:
            msg = f"Fix crash in feature {m - 2}" if m % 2 else f"Handle edge case for feature {m - 2}"
            commit(msg, f"2020-{m:02d}-12T10:00:00Z", [3 * (m - 2), 3 * (m - 2) + 1], f"fix{m}")
        commit(f"Refactor util {m}", f"2020-{m:02d}-20T10:00:00Z", util_tag="x" * m)
        if m == 3:   # no fix keyword: it becomes a known fix only once #4 is labeled bug (May 2)
            commit("Speed up plugin loading, closes #4", "2020-03-21T10:00:00Z", extra={"src/plugins.py": "p = 1\n"})
        if m == 4:
            commit("Document options", "2020-04-22T10:00:00Z", extra={"docs/options.md": "opts\n"})
        if m in (2, 6, 9):
            commit("Add tests for core", f"2020-{m:02d}-25T10:00:00Z", extra={"tests/test_core.py": f"t = {m}\n"})
    db = os.path.join(d, "..", f"hist{os.path.basename(d)}.db")
    ingest_git(d, db)
    c = connect(db)

    def issue(n, date, title, body, is_pr=0):
        c.execute("INSERT INTO issues VALUES(?,?,?,?,?,?)", (n, is_pr, ts(date), "u", title, body))

    def event(n, date, typ, **data):
        c.execute("INSERT INTO issue_events VALUES(?,?,?,?,?)", (n, ts(date), typ, "m", json.dumps(data)))

    def comment(cid, n, date, body):
        c.execute("INSERT INTO comments VALUES(?,?,?,?,?,?)", (cid, n, ts(date), "m", body, "issue"))

    issue(1, "2020-01-15T00:00:00Z", "Crash when parsing empty config file", "Parser raises IndexError on an empty config")
    issue(2, "2020-02-01T00:00:00Z", "Typo in documentation", "readme has a typo")
    issue(3, "2020-04-10T00:00:00Z", "Parser crashes on empty configuration", "IndexError again when the config is empty")
    comment(1, 3, "2020-05-01T00:00:00Z", "duplicate of #1")
    issue(4, "2020-03-01T00:00:00Z", "Slow startup with many plugins", "startup takes 10s with 50 plugins installed")
    event(4, "2020-03-21T12:00:00Z", "closed")
    event(4, "2020-05-02T00:00:00Z", "labeled", label="bug")          # label arrives after the closing commit
    issue(5, "2020-07-10T00:00:00Z", "Startup is slow when many plugins are installed", "Duplicate of #4. plugins load slowly")
    issue(6, "2020-09-03T00:00:00Z", "Unicode error in help output", "help crashes on non-ascii option names")
    event(6, "2020-09-04T00:00:00Z", "labeled", label="type: bug")
    issue(7, "2020-10-05T00:00:00Z", "help output fails with non-ascii characters", "UnicodeEncodeError printing help")
    comment(2, 7, "2020-10-06T00:00:00Z", "dup of #6")
    issue(8, "2020-06-01T00:00:00Z", "Refactor util module", "PR body", is_pr=1)
    event(1, "2020-01-16T00:00:00Z", "labeled", label="bug")
    event(1, "2020-02-01T00:00:00Z", "unlabeled", label="bug")
    event(1, "2020-02-01T00:00:00Z", "labeled", label="confirmed bug")  # same second as the unlabel
    c.commit(); c.close()
    return db, d
