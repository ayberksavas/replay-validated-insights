"""TimeView: the ONLY interface the system under test gets. Leakage rule is enforced here:
the snapshot is physically materialized with rows where ts <= T, and git access is limited to
commits inside that snapshot."""
import json
import sqlite3
from .store import SCHEMA, connect
from .gitutil import run_git, require_repo


class LeakageError(Exception):
    pass


class TimeView:
    def __init__(self, db_path: str, repo: str, as_of: int):
        self.as_of = as_of
        require_repo(repo)
        self.__repo = repo  # name-mangled; predictors never receive a path
        src = connect(db_path)
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        T = (as_of,)
        self._copy(src, "commits", "ts<=?", T)
        self._copy(src, "commit_files", "sha IN (SELECT sha FROM commits WHERE ts<=?)", T)
        self._copy(src, "issues", "ts<=?", T)
        self._copy(src, "issue_events", "ts<=? AND number IN (SELECT number FROM issues WHERE ts<=?)", (as_of, as_of))
        self._copy(src, "comments", "ts<=? AND number IN (SELECT number FROM issues WHERE ts<=?)", (as_of, as_of))
        self.db.commit()
        src.close()
        self.db.execute("PRAGMA query_only=ON")
        # a predictor must not be able to ATTACH the full database (or any file) and read the future
        self.db.set_authorizer(lambda action, *_: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_ATTACH else sqlite3.SQLITE_OK)

    def _copy(self, src, table, where, params):
        rows = src.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY rowid", params).fetchall()
        if rows:
            q = ",".join("?" * len(rows[0]))
            self.db.executemany(f"INSERT INTO {table} VALUES({q})", [tuple(r) for r in rows])

    # ---- history ----
    def commits(self, since: int | None = None):
        return self.db.execute("SELECT * FROM commits WHERE ts>=? ORDER BY ts, sha", (since or 0,)).fetchall()

    def files(self, sha):
        return self.db.execute("SELECT * FROM commit_files WHERE sha=?", (sha,)).fetchall()

    def issues(self, since: int | None = None, is_pr: bool | None = None):
        q, p = "SELECT * FROM issues WHERE ts>=?", [since or 0]
        if is_pr is not None:
            q += " AND is_pr=?"; p.append(int(is_pr))
        return self.db.execute(q + " ORDER BY ts, number", p).fetchall()

    def issue_state(self, number):
        """State of an issue as of T, reconstructed from timestamped events."""
        state, labels = "open", set()
        for e in self.db.execute("SELECT * FROM issue_events WHERE number=? ORDER BY ts, rowid", (number,)):
            d = json.loads(e["data"])
            if e["type"] in ("closed", "merged"):
                state = "closed"
            elif e["type"] == "reopened":
                state = "open"
            elif e["type"] == "labeled":
                if d.get("label") is not None:
                    labels.add(d["label"])
            elif e["type"] == "unlabeled":
                labels.discard(d["label"])
        return {"state": state, "labels": sorted(labels)}

    def comments(self, number):
        return self.db.execute("SELECT * FROM comments WHERE number=? ORDER BY ts, id", (number,)).fetchall()

    # ---- code at a commit (guarded) ----
    def _check(self, sha):
        if not self.db.execute("SELECT 1 FROM commits WHERE sha=?", (sha,)).fetchone():
            raise LeakageError(f"commit {sha} is not visible at T={self.as_of}")

    def _git(self, *args):
        return run_git(self.__repo, *args)

    def diff(self, sha):
        self._check(sha)
        return self._git("show", "--format=", "-U3", sha)

    def file_at(self, sha, path):
        self._check(sha)
        return self._git("show", f"{sha}:{path}")
