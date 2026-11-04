import re
import subprocess
from .store import connect
from .gitutil import require_repo

TEST_RE = re.compile(r"(^|/)(tests?|__tests__|spec)(/|$)|(^|/)test_[^/]*$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$", re.I)
DOC_RE = re.compile(r"(^|/)(docs?|documentation)(/|$)|\.(md|rst|adoc|txt)$|(^|/)readme", re.I)

SEP = "\x1e"


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True,
                          text=True, errors="replace").stdout


def ingest_git(repo: str, db_path: str):
    require_repo(repo)
    db = connect(db_path)
    fmt = SEP + "%H\x1f%ct\x1f%at\x1f%an\x1f%P\x1f%B\x1f"
    out = git(repo, "log", "--all", "--no-merges", "-M", "--numstat", f"--format={fmt}")
    n = 0
    for chunk in out.split(SEP)[1:]:
        parts = chunk.split("\x1f")
        sha, ts, ats, author, parents, msg = parts[:6]
        numstat = parts[6] if len(parts) > 6 else ""
        db.execute("INSERT OR REPLACE INTO commits VALUES(?,?,?,?,?,?)",
                   (sha, int(ts), int(ats), author, msg.strip(), parents))
        db.execute("DELETE FROM commit_files WHERE sha=?", (sha,))
        for line in numstat.strip().splitlines():
            a, d, path = (line.split("\t") + ["", "", ""])[:3]
            if "=>" in path:  # rename: keep new path
                path = re.sub(r"\{.*? => (.*?)\}", r"\1", path).replace("//", "/") if "{" in path else path.split(" => ")[-1]
            db.execute("INSERT INTO commit_files VALUES(?,?,?,?,?,?)",
                       (sha, path, int(a) if a.isdigit() else 0, int(d) if d.isdigit() else 0,
                        int(bool(TEST_RE.search(path))), int(bool(DOC_RE.search(path)))))
        n += 1
    db.commit()
    return n
