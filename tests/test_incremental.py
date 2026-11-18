"""Incremental time machine + sandbox: same answers as the rebuild path, and no way to the future."""
import os
import sqlite3
import subprocess
import tempfile

import pytest
from conftest import ts
from timemachine.evaluate import replay, replay_incremental
from timemachine.groundtruth import DAY
from timemachine.heuristics import KBHeuristic
from timemachine.kb import KnowledgeBase
from timemachine.predictor import RandomBaseline, MostChangedFiles
from timemachine.sandbox.host import SandboxHost, SandboxError
from timemachine.snapshot import IncrementalSnapshot
from timemachine.timeline import Timeline
from timemachine.view import TimeView

TIMES = list(range(ts("2020-02-01T00:00:00Z"), ts("2020-12-15T00:00:00Z"), 60 * DAY))


def kb_state(kb):
    return (sorted(kb.fix_shas), {p: kb.file_stats(p) for p in sorted(kb._stats)},
            {n: sorted(v) for n, v in sorted(kb.issue_refs.items())},
            {n: sorted(v) for n, v in sorted(kb.issue_commits.items())}, dict(sorted(kb._author_commits.items())),
            kb.search_issues("crash empty config parser", 10), kb.search_issues("slow startup plugins", 10),
            kb.summary(), {n: kb.labels(n) for n in sorted(kb.issues)})


def test_incremental_kb_equals_rebuild(history):
    db, repo = history
    tl, inc = Timeline(db), KnowledgeBase()
    for t in TIMES + [ts("2021-06-01T00:00:00Z")]:
        inc.apply(tl.advance(t), as_of=t)
        assert kb_state(inc) == kb_state(KnowledgeBase(TimeView(db, repo, t))), t


def test_label_after_commit_reclassifies_fix(history):
    db, _ = history
    tl, kb = Timeline(db), KnowledgeBase()
    kb.apply(tl.advance(ts("2020-04-01T00:00:00Z")))
    closer = next(s for s, c in kb._commits.items() if "closes #4" in c["message"])
    assert not kb.is_fix(closer) and kb.file_stats("src/plugins.py")["fixes"] == 0
    kb.apply(tl.advance(ts("2020-05-03T00:00:00Z")))       # #4 labeled bug on May 2
    assert kb.is_fix(closer) and kb.file_stats("src/plugins.py")["fixes"] == 1


def test_timeline_only_moves_forward(history):
    tl = Timeline(history[0])
    tl.advance(ts("2020-05-01T00:00:00Z"))
    with pytest.raises(ValueError):
        tl.advance(ts("2020-04-01T00:00:00Z"))


def test_sandbox_store_equals_timeview(history):
    db, repo = history
    with SandboxHost(db, repo, "kb_heuristic") as h:
        for t in TIMES:
            h.advance(t)
            sb, v = sqlite3.connect(os.path.join(h.root, "store.db")), TimeView(db, repo, t)
            for table, order in (("commits", "sha"), ("commit_files", "sha, path"), ("issues", "number"),
                                 ("issue_events", "number, ts, type, data"), ("comments", "id")):
                q = f"SELECT * FROM {table} ORDER BY {order}"
                assert sb.execute(q).fetchall() == [tuple(r) for r in v.db.execute(q)], (t, table)
            for table in ("commits", "issues", "issue_events", "comments"):
                assert (sb.execute(f"SELECT MAX(ts) FROM {table}").fetchone()[0] or 0) <= t
            sb.close()


def test_sandbox_cannot_reach_the_future(history):
    db, repo = history
    with SandboxHost(db, repo, "kb_heuristic") as h:
        h.advance(ts("2020-04-01T00:00:00Z"))
        res = h.probe()
        assert len(res) == 7 and all(v.startswith("blocked") for v in res.values()), res
        assert h.stats["blocked_requests"] == 1                     # the future diff was refused by the host
        future = subprocess.run(["git", "-C", repo, "log", "-1", "--format=%H"], capture_output=True, text=True).stdout.strip()
        with pytest.raises(SandboxError):
            h.risky([future])                                        # not in the sandbox store
        pkg = os.path.join(h.root, "pkg", "timemachine")
        for absent in ("groundtruth.py", "evaluate.py", "view.py", "ingest_github.py", "llm.py"):
            assert not os.path.exists(os.path.join(pkg, absent))     # oracle & host code are not even copied
        assert h.risky([]) == []                                     # still healthy after the attempts


def test_sandbox_dir_must_not_contain_the_data(history):
    db, repo = history
    with pytest.raises(ValueError):
        SandboxHost(db, repo, "kb_heuristic", root=os.path.dirname(os.path.abspath(db)))


def test_incremental_replay_matches_rebuild(history):
    db, repo = history
    inc = replay_incremental(db, repo, ["random", "most_changed_files", "kb_heuristic"], TIMES,
                             window_days=180, lookback_days=30)
    for p, got in zip((RandomBaseline(), MostChangedFiles(), KBHeuristic()), inc["results"]):
        want = replay(db, repo, p, TIMES, window_days=180, lookback_days=30)
        assert {k: v for k, v in got.items() if k != "sandbox"} == want, p.name
    kb = inc["results"][2]
    assert kb["output1_risky_change"]["positives"] > 0 and kb["output2_missing_tests"]["positives"] > 0
    assert kb["output3_similar_issue_hit@k"]["n_dup_issues"] >= 2
    run = inc["run"]
    assert run["events_fed_once"] < run["rows_a_rebuild_would_copy"]


def test_incremental_snapshot_holds_only_the_past(history):
    _, repo = history
    out = tempfile.mkdtemp()
    snap = IncrementalSnapshot(repo, out)
    g = lambda *a: subprocess.run(["git", "-C", out, *a], capture_output=True, text=True)
    shas = {}
    for t in (ts("2020-03-01T00:00:00Z"), ts("2020-08-01T00:00:00Z")):
        shas[t] = snap.update(t)
        dates = [int(x) for x in g("log", "--all", "--reflog", "--format=%ct").stdout.split()]
        assert dates and max(dates) <= t
        assert g("remote").stdout == "" and g("tag").stdout == ""
        assert not os.path.exists(os.path.join(out, ".git", "FETCH_HEAD"))
    assert len(set(shas.values())) == 2
    head = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert g("cat-file", "-e", head).returncode != 0                      # newest commit absent
    assert "refs/tm/" not in subprocess.run(["git", "-C", repo, "show-ref"], capture_output=True, text=True).stdout
