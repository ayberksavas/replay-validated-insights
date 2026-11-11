"""Pull issues/PRs/events/comments from GitHub. Events give us the *timeline*, so issue state can be
reconstructed at any T (closed/reopened/labeled/duplicate...).

Transport: the `gh` CLI when it is installed (uses its login), otherwise the REST API directly with
GITHUB_TOKEN / GH_TOKEN if set (without a token GitHub allows only 60 requests per hour).

Incremental: the start time of the last successful sync is kept in `meta`. The next run fetches only
issues and comments updated since then and stops paging events once a page is entirely older.
`full=True` re-fetches everything."""
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from .store import connect

API = "https://api.github.com/"


def ts(s):
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None


def login(u):
    return (u or {}).get("login")


class GitHub:
    def __init__(self):
        self.gh = shutil.which("gh")
        self.token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        self.requests = 0

    @property
    def transport(self):
        return "gh" if self.gh else ("rest+token" if self.token else "rest (anonymous, 60 req/h)")

    def page(self, endpoint, n):
        url = f"{endpoint}{'&' if '?' in endpoint else '?'}page={n}"
        self.requests += 1
        if self.gh:
            p = subprocess.run([self.gh, "api", url], capture_output=True, text=True, encoding="utf-8")
            if p.returncode:
                raise RuntimeError(f"gh api {url}: {p.stderr.strip()[:300]}")
            return json.loads(p.stdout)
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "timemachine-ingest"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            with urllib.request.urlopen(urllib.request.Request(API + url, headers=headers), timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                raise RuntimeError(f"GitHub rate limit after {self.requests} requests ({self.transport}). "
                                   "Install gh and run `gh auth login`, or set GITHUB_TOKEN, then re-run.") from e
            raise

    def pages(self, endpoint, stop=None):
        n = 1
        while True:
            items = self.page(endpoint, n)
            if not items:
                return
            yield items
            if len(items) < 100 or (stop and stop(items)):
                return
            n += 1


def ingest_github(slug: str, db_path: str, full: bool = False, log=print):
    db = connect(db_path)
    gh = GitHub()
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    key = f"github_synced_at:{slug}"
    row = None if full else db.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    since = row[0] if row else None
    since_ts = ts(since)
    upd = f"&since={since}" if since else ""
    log(f"github: {'incremental since ' + since if since else 'full sync'} via {gh.transport}")
    counts = {"issues": 0, "events": 0, "comments": 0}

    def issues():
        for items in gh.pages(f"repos/{slug}/issues?state=all&per_page=100&sort=updated&direction=asc{upd}"):
            for i in items:
                db.execute("INSERT OR REPLACE INTO issues VALUES(?,?,?,?,?,?)",
                           (i["number"], int("pull_request" in i), ts(i["created_at"]),
                            login(i["user"]), i["title"], i.get("body") or ""))
            counts["issues"] += len(items)

    def comments(kind, endpoint, url_key):
        for items in gh.pages(f"repos/{slug}/{endpoint}?per_page=100&sort=updated&direction=asc{upd}"):
            for c in items:
                num = int(c[url_key].rsplit("/", 1)[-1])
                db.execute("INSERT OR REPLACE INTO comments VALUES(?,?,?,?,?,?)",
                           (c["id"], num, ts(c["created_at"]), login(c["user"]), c["body"], kind))
            counts["comments"] += len(items)

    def events():
        if not since:
            db.execute("DELETE FROM issue_events")
        older = (lambda items: all(ts(e["created_at"]) < since_ts for e in items)) if since_ts else None
        for items in gh.pages(f"repos/{slug}/issues/events?per_page=100", stop=older):
            for e in items:
                iss = e.get("issue") or {}
                if not iss or (since_ts and ts(e["created_at"]) < since_ts):
                    continue
                data = json.dumps({"commit_id": e.get("commit_id"), "label": (e.get("label") or {}).get("name"),
                                   "rename": e.get("rename"), "dup_of": None})
                r = (iss["number"], ts(e["created_at"]), e["event"], login(e.get("actor")), data)
                if since and db.execute("SELECT 1 FROM issue_events WHERE number=? AND ts=? AND type=? AND "
                                        "IFNULL(actor,'')=IFNULL(?,'') AND data=?", r).fetchone():
                    continue
                db.execute("INSERT INTO issue_events VALUES(?,?,?,?,?)", r)
                counts["events"] += 1

    # most useful first (issues, issue comments = Output 3 truth, events = labels/closes); each section is
    # committed on its own, so running out of quota keeps what was fetched (the sync time is then not
    # recorded, and the next run fetches everything again)
    steps = (("issues", issues), ("issue comments", lambda: comments("issue", "issues/comments", "issue_url")),
             ("issue events", events), ("review comments", lambda: comments("review", "pulls/comments", "pull_request_url")))
    for label, fn in steps:
        try:
            fn()
            db.commit()
        except RuntimeError as e:
            db.commit()
            log(f"github: stopped during {label}: {e}")
            log(f"github: kept {counts} ({gh.requests} requests); re-run later to complete")
            counts["incomplete"] = label
            return counts
    db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, started))
    db.commit()
    log(f"github: {counts['issues']} issues/PRs, {counts['events']} new events, {counts['comments']} comments "
        f"({gh.requests} requests)")
    return counts
