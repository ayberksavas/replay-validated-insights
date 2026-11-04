import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from .ingest_git import ingest_git
from .ingest_github import ingest_github
from .predictor import RandomBaseline, MostChangedFiles
from .evaluate import replay
from .groundtruth import DAY
from .snapshot import export_snapshot


def parse_date(s):
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())


def main():
    ap = argparse.ArgumentParser(prog="tm")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("ingest", help="clone + ingest a repo (owner/name)")
    i.add_argument("slug"); i.add_argument("--workdir", default="data"); i.add_argument("--no-github", action="store_true")
    r = sub.add_parser("replay", help="replay & evaluate baselines")
    r.add_argument("slug"); r.add_argument("--workdir", default="data")
    r.add_argument("--from", dest="start", required=True); r.add_argument("--to", dest="end", required=True)
    r.add_argument("--every-days", type=int, default=30); r.add_argument("--window-days", type=int, default=180)
    s = sub.add_parser("snapshot", help="export the code as of T into a fresh repo (no future history)")
    s.add_argument("slug"); s.add_argument("--workdir", default="data")
    s.add_argument("--at", required=True); s.add_argument("--out", required=True)
    s.add_argument("--history", action="store_true", help="keep the real commits up to T")
    a = ap.parse_args()
    d = os.path.join(a.workdir, a.slug.replace("/", "__")); os.makedirs(d, exist_ok=True)
    repo, db = os.path.join(d, "repo"), os.path.join(d, "tm.db")
    if a.cmd == "ingest":
        if not os.path.exists(repo):
            subprocess.run(["git", "clone", "--quiet", f"https://github.com/{a.slug}.git", repo], check=True)
        print("commits:", ingest_git(repo, db))
        if not a.no_github:
            ingest_github(a.slug, db); print("github data ingested")
    elif a.cmd == "snapshot":
        sha = export_snapshot(repo, parse_date(a.at), a.out, history=a.history)
        print(f"{a.out}: tree of {sha} (last commit at or before {a.at})")
    else:
        ts = list(range(parse_date(a.start), parse_date(a.end), a.every_days * DAY))
        for p in (RandomBaseline(), MostChangedFiles()):
            print(json.dumps(replay(db, repo, p, ts, window_days=a.window_days), indent=2))
