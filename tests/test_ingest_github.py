"""GitHub ingest against canned API pages (no network): full sync, then an incremental sync."""
import os
import tempfile

from timemachine import ingest_github as ig
from timemachine.store import connect


def user(n):
    return {"login": n}


ISSUES = [{"number": 1, "created_at": "2024-01-01T00:00:00Z", "user": user("a"), "title": "Crash", "body": "boom"},
          {"number": 2, "created_at": "2024-01-02T00:00:00Z", "user": user("b"), "title": "PR", "body": None, "pull_request": {}}]
EVENTS = [{"issue": {"number": 1}, "created_at": "2024-01-03T00:00:00Z", "event": "labeled", "actor": user("m"), "label": {"name": "bug"}},
          {"issue": {"number": 1}, "created_at": "2024-01-04T00:00:00Z", "event": "closed", "actor": user("m"), "commit_id": "abc"}]
COMMENTS = [{"id": 10, "issue_url": "https://api.github.com/repos/o/r/issues/1", "created_at": "2024-01-03T00:00:00Z", "user": user("c"), "body": "dup of #0"}]
REVIEWS = [{"id": 20, "pull_request_url": "https://api.github.com/repos/o/r/pulls/2", "created_at": "2024-01-05T00:00:00Z", "user": user("d"), "body": "nit"}]


class FakeGitHub(ig.GitHub):
    def __init__(self, data):
        super().__init__()
        self.data, self.urls = data, []

    def page(self, endpoint, n):
        self.urls.append(endpoint)
        key = next(k for k in ("issues/events", "issues/comments", "pulls/comments", "issues") if f"/{k}" in endpoint)
        return self.data[key] if n == 1 else []


def run(monkeypatch, db, data, full=False):
    fake = FakeGitHub(data)
    monkeypatch.setattr(ig, "GitHub", lambda: fake)
    counts = ig.ingest_github("o/r", db, full=full, log=lambda *_: None)
    return fake, counts


def test_full_then_incremental(monkeypatch):
    db = os.path.join(tempfile.mkdtemp(), "t.db")
    data = {"issues": ISSUES, "issues/events": EVENTS, "issues/comments": COMMENTS, "pulls/comments": REVIEWS}
    fake, counts = run(monkeypatch, db, data)
    c = connect(db)
    assert counts == {"issues": 2, "events": 2, "comments": 2}
    assert [tuple(r) for r in c.execute("SELECT number, is_pr, body FROM issues ORDER BY number")] == [(1, 0, "boom"), (2, 1, "")]
    assert c.execute("SELECT kind FROM comments WHERE id=20").fetchone()[0] == "review"
    assert not any("since=" in u for u in fake.urls)
    synced = c.execute("SELECT v FROM meta WHERE k='github_synced_at:o/r'").fetchone()[0]
    # second run: GitHub returns the old events again plus one new one; only the new one is added
    new_event = {"issue": {"number": 1}, "created_at": "2999-01-01T00:00:00Z", "event": "reopened", "actor": user("m")}
    fake, counts = run(monkeypatch, db, {**data, "issues/events": EVENTS + [new_event], "issues": [], "issues/comments": [], "pulls/comments": []})
    assert all(f"since={synced}" in u for u in fake.urls if "/events" not in u)
    assert counts["events"] == 1
    assert c.execute("SELECT COUNT(*) FROM issue_events").fetchone()[0] == 3
    assert c.execute("SELECT COUNT(*) FROM issues").fetchone()[0] == 2          # kept, not wiped
