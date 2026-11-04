import json, os, subprocess, tempfile
from datetime import datetime, timezone
import pytest
from timemachine.ingest_git import ingest_git
from timemachine.store import connect
from timemachine.view import TimeView
from timemachine.kb import KnowledgeBase
from timemachine.kb.bm25 import tokenize
from timemachine.kb.links import refs, closes, is_fix_message
from timemachine.heuristics import KBHeuristic
from timemachine.evaluate import replay
from timemachine.groundtruth import DAY


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


def view(world, date): return TimeView(world[0], world[1], ts(date))


def test_text_helpers():
    assert refs("see #12 and org/repo#7, (#3)") == {12, 3}
    assert closes("Fixes #4, closes: #5") == {4, 5}
    assert is_fix_message("Fix crash") and not is_fix_message('Revert "Fix crash"') and not is_fix_message("add feature")
    assert "empty" in tokenize("emptyConfig_file") and "config" in tokenize("emptyConfig_file")


def test_fix_memory_and_file_stats(world):
    kb = KnowledgeBase(view(world, "2020-04-01T00:00:00Z"))
    assert len(kb.fix_shas) == 1                      # the fix, not the revert
    st = kb.file_stats("src/a.py")
    assert st["commits"] == 4 and st["fixes"] == 1
    fix = next(iter(kb.fix_shas))
    assert kb.file_stats("src/a.py", exclude_sha=fix)["fixes"] == 0 and kb.file_stats("src/a.py", exclude_sha=fix)["commits"] == 3
    assert kb.issue_commits[1] == {fix}


def test_kb_is_a_function_of_the_past_only(world):
    """Adding rows after T must not change anything the KB exposes."""
    def snap(kb):
        return (sorted(kb.fix_shas), kb.file_stats("src/a.py"), kb.search_issues("crash empty config", 10), dict(kb.issue_refs))
    before = snap(KnowledgeBase(view(world, "2020-04-01T00:00:00Z")))
    c = connect(world[0])
    c.execute("INSERT INTO issues VALUES(50,0,?,?,?,?)", (ts("2020-12-01T00:00:00Z"), "u", "crash empty config again", "x #1"))
    c.execute("INSERT INTO comments VALUES(77,1,?,?,?,?)", (ts("2020-12-02T00:00:00Z"), "u", "see #2", "issue")); c.commit()
    assert snap(KnowledgeBase(view(world, "2020-04-01T00:00:00Z"))) == before


def test_search_issues_finds_duplicate(world):
    kb = KnowledgeBase(view(world, "2020-04-01T00:00:00Z"))
    assert kb.search_issues("Parser crashes on empty configuration", 3)[0][0] == 1
    assert 9 not in dict(kb.search_issues("future issue after T", 10))


def test_heuristic_predictor(world):
    v = view(world, "2020-04-01T00:00:00Z")
    cands = v.commits(since=ts("2020-03-01T00:00:00Z"))
    h = KBHeuristic()
    risky = {f.target: f.score for f in h.risky_commits(v, cands)}
    rewrite = next(c["sha"] for c in cands if c["message"].startswith("rewrite"))
    readme = next(c["sha"] for c in cands if c["message"].startswith("update"))
    assert risky[rewrite] > risky[readme]
    assert {f.target for f in h.missing_tests(v, cands)} == {rewrite}   # docs-only commit is not flagged
    iss3 = next(i for i in connect(world[0]).execute("SELECT * FROM issues WHERE number=3"))
    v3 = TimeView(world[0], world[1], iss3["ts"] - 1)
    assert [f.target for f in h.similar_issues(v3, iss3, 2)][0] == 1


def test_kb_predictor_runs_in_replay(world):
    out = replay(world[0], world[1], KBHeuristic(), [ts("2020-04-01T00:00:00Z")], window_days=250, lookback_days=40)
    assert out["predictor"] == "kb_heuristic"
