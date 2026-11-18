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
from conftest import ts



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
