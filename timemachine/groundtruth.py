"""Oracle: sees the FULL history and labels what *actually* happened in (T, T+window].
Never imported by predictors."""
import re
from functools import lru_cache
from .store import connect
from .gitutil import run_git, require_repo

FIX_RE = re.compile(r"\b(fix(e[sd])?|bug(fix)?|hotfix|regression|resolve[sd]?|patch)\b", re.I)
DUP_RE = re.compile(r"duplicate\s+of\s+#(\d+)|dup(licate)?\s*(of)?:?\s*#(\d+)", re.I)
DAY = 86400


class Oracle:
    def __init__(self, db_path: str, repo: str):
        require_repo(repo)
        self.db, self.repo = connect(db_path), repo
        self._szz_cache = {}

    def _git(self, *a):
        return run_git(self.repo, *a)

    # ---- bug-fix commits & SZZ ----
    def fix_commits(self, t0, t1):
        rows = self.db.execute("SELECT * FROM commits WHERE ts>? AND ts<=?", (t0, t1)).fetchall()
        closers = {r[0] for r in self.db.execute(
            "SELECT e.data FROM issue_events e JOIN issue_events l ON l.number=e.number AND l.type='labeled' "
            "WHERE e.type='closed' AND l.data LIKE '%bug%'")}
        import json
        closers = {json.loads(d).get("commit_id") for d in closers}
        return [r for r in rows if FIX_RE.search(r["message"].split("\n")[0]) or r["sha"] in closers]

    def introducers(self, fix_sha):
        """SZZ: blame the lines a fix deleted/changed in the parent revision."""
        if fix_sha in self._szz_cache:
            return self._szz_cache[fix_sha]
        out, path = set(), None
        if not self._git("rev-list", "--parents", "-n", "1", fix_sha).split()[1:]:
            self._szz_cache[fix_sha] = out  # root commit: no parent to blame
            return out
        diff = self._git("diff", "-U0", "--no-renames", f"{fix_sha}^", fix_sha)
        for line in diff.splitlines():
            if line.startswith("--- a/"):
                path = line[6:]
            elif line.startswith("--- "):
                path = None
            elif line.startswith("@@") and path:
                m = re.match(r"@@ -(\d+)(?:,(\d+))? ", line)
                start, n = int(m.group(1)), int(m.group(2) or 1)
                if n == 0:
                    continue
                if re.search(r"(^|/)tests?/|test_|_test\.|\.md$|\.rst$", path):
                    continue
                blame = self._git("blame", "-w", "--porcelain", "-L", f"{start},{start+n-1}",
                                  f"{fix_sha}^", "--", path)
                out |= {l.split()[0] for l in blame.splitlines() if re.match(r"^[0-9a-f]{40} \d+ \d+", l)}
        self._szz_cache[fix_sha] = out
        return out

    def buggy_commits(self, T, window_days):
        """{introducing_sha: earliest fix ts} for fixes landing in (T, T+window]."""
        res = {}
        for fx in self.fix_commits(T, T + window_days * DAY):
            for s in self.introducers(fx["sha"]):
                res[s] = min(res.get(s, 1 << 60), fx["ts"])
        return res

    # ---- Output 2 ----
    def untested_later_covered(self, T, window_days, candidates):
        """candidates w/o test changes, later covered by a test commit touching same stem or a bug fix on same files."""
        t1 = T + window_days * DAY
        later = self.db.execute("SELECT c.sha,c.ts,f.path,f.is_test FROM commits c JOIN commit_files f ON f.sha=c.sha "
                                "WHERE c.ts>? AND c.ts<=?", (T, t1)).fetchall()
        fixes = {r["sha"] for r in self.fix_commits(T, t1)}
        res = {}
        for sha in candidates:
            files = self.db.execute("SELECT path,is_test FROM commit_files WHERE sha=?", (sha,)).fetchall()
            if any(f["is_test"] for f in files) or not files:
                continue
            src = {f["path"] for f in files if not f["is_test"]}
            stems = {re.sub(r"\.[^.]+$", "", p.rsplit("/", 1)[-1]) for p in src}
            for r in later:
                stem = re.sub(r"(^test_|_test$|\.test$|\.spec$)", "", re.sub(r"\.[^.]+$", "", r["path"].rsplit("/", 1)[-1]))
                if (r["is_test"] and any(s and s in stem for s in stems)) or (r["sha"] in fixes and r["path"] in src):
                    res[sha] = min(res.get(sha, 1 << 60), r["ts"])
        return res

    # ---- Output 3 ----
    def duplicates_of(self, number):
        """Issue numbers this issue was later declared duplicate of / explicitly linked to."""
        targets = set()
        for c in self.db.execute("SELECT body FROM comments WHERE number=?", (number,)):
            for m in DUP_RE.finditer(c["body"] or ""):
                targets.add(int(m.group(1) or m.group(4)))
        body = self.db.execute("SELECT body FROM issues WHERE number=?", (number,)).fetchone()
        for m in DUP_RE.finditer(body["body"] or "" if body else ""):
            targets.add(int(m.group(1) or m.group(4)))
        targets.discard(number)
        return targets
