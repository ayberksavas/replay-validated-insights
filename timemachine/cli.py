import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

from .evaluate import replay, replay_incremental
from .gitutil import require_repo, GitError
from .groundtruth import DAY
from .heuristics import KBHeuristic
from .ingest_git import ingest_git
from .ingest_github import ingest_github
from .predictor import RandomBaseline, MostChangedFiles
from .snapshot import export_snapshot

BASELINES = ["random", "most_changed_files", "kb_heuristic"]
IN_PROCESS = {"random": RandomBaseline, "most_changed_files": MostChangedFiles, "kb_heuristic": KBHeuristic}


def parse_date(s):
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())


def day(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


def add_replay_args(p, demo=False):
    p.add_argument("slug"); p.add_argument("--workdir", default="data")
    p.add_argument("--from", dest="start", required=True); p.add_argument("--to", dest="end", required=True)
    p.add_argument("--every-days", type=int, default=60 if demo else 30)
    p.add_argument("--window-days", type=int, default=180); p.add_argument("--lookback-days", type=int, default=30)
    p.add_argument("--predictors", default=",".join(BASELINES + (["claude_insight"] if demo else [])),
                   help="comma list of: random, most_changed_files, kb_heuristic, claude_insight")
    p.add_argument("--llm", choices=["auto", "live", "dry-run"], default="auto",
                   help="claude_insight: live API calls, or dry-run (no calls; falls back to the KB heuristic). "
                        "auto = live if Anthropic credentials are found")
    p.add_argument("--model", default=None, help="Claude model for claude_insight (default claude-opus-5-5)")
    p.add_argument("--token-budget", type=int, default=300_000, help="stop calling Claude after this many tokens")
    p.add_argument("--keep-dup-markers", action="store_true",
                   help="do not redact 'duplicate of #N' from Output 3 queries (they leak the answer)")
    p.add_argument("--json", dest="json_out", help="write the full result as JSON")
    p.add_argument("--sandbox", choices=["process", "docker"], default="process",
                   help="process: local isolated python + jail; docker: the same worker in a container with no network")
    p.add_argument("--docker-image", default=None, help="image for --sandbox docker (default python:3.12-slim)")


def make_llm(a, d, names):
    if "claude_insight" not in names:
        return None
    from .llm import LLMGateway, DEFAULT_MODEL
    return LLMGateway(model=a.model or DEFAULT_MODEL, cache_path=os.path.join(d, "llm_cache.jsonl"),
                      budget_tokens=a.token_budget, mode=a.llm)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="tm")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("ingest", help="clone/fetch + ingest a repo (owner/name); incremental on re-runs")
    i.add_argument("slug"); i.add_argument("--workdir", default="data"); i.add_argument("--no-github", action="store_true")
    i.add_argument("--full", action="store_true", help="re-fetch all GitHub data instead of only what changed")
    r = sub.add_parser("replay", help="replay history and score predictors (incremental + sandboxed by default)")
    add_replay_args(r)
    r.add_argument("--mode", choices=["incremental", "rebuild"], default="incremental")
    dm = sub.add_parser("demo", help="incremental sandboxed replay with a live timeline and an HTML report")
    add_replay_args(dm, demo=True)
    dm.add_argument("--html", help="report path (default data/<slug>/demo_report.html)")
    dm.add_argument("--no-code-snapshot", action="store_true", help="do not keep a git snapshot of the code at T in the featured sandbox")
    dm.add_argument("--quiet", action="store_true", help="no per-step timeline")
    nc = sub.add_parser("nextchange", help="Output 8: predict the next code change (files + patch) for issues/PRs, "
                                          "scored against the real commit")
    add_replay_args(nc, demo=True)
    nc.add_argument("--kinds", default="issue", help="comma list: issue (issue a commit closed), pr (PR description)")
    nc.add_argument("--limit", type=int, default=None, help="score at most this many tasks")
    nc.add_argument("--top-k", type=int, default=10)
    nc.add_argument("--quiet", action="store_true")
    s = sub.add_parser("snapshot", help="export the code as of T into a fresh repo (no future history)")
    s.add_argument("slug"); s.add_argument("--workdir", default="data")
    s.add_argument("--at", required=True); s.add_argument("--out", required=True)
    s.add_argument("--history", action="store_true", help="keep the real commits up to T")
    a = ap.parse_args()
    d = os.path.join(a.workdir, a.slug.replace("/", "__")); os.makedirs(d, exist_ok=True)
    repo, db = os.path.join(d, "repo"), os.path.join(d, "tm.db")
    if a.cmd == "ingest":
        try:
            require_repo(repo)
            subprocess.run(["git", "-C", repo, "fetch", "--quiet", "--prune", "origin"], check=True)
        except GitError:
            if os.path.isdir(repo) and os.listdir(repo):
                raise SystemExit(f"{repo} exists, is not empty and is not a git clone; remove it first")
            os.makedirs(repo, exist_ok=True)
            subprocess.run(["git", "clone", "--quiet", f"https://github.com/{a.slug}.git", repo], check=True)
        print("commits:", ingest_git(repo, db))
        if not a.no_github:
            ingest_github(a.slug, db, full=a.full)
        return
    if a.cmd == "snapshot":
        sha = export_snapshot(repo, parse_date(a.at), a.out, history=a.history)
        print(f"{a.out}: tree of {sha} (last commit at or before {a.at})")
        return
    names = [n.strip() for n in a.predictors.split(",") if n.strip()]
    if a.cmd == "nextchange":
        from .nextchange import replay_next_change, Console as ChangeConsole
        llm = make_llm(a, d, names)
        con = ChangeConsole(featured=names[-1], quiet=a.quiet)
        print(f"\n== NEXT CODE CHANGE ==  {a.slug}  {a.start} -> {a.end} | kinds: {a.kinds} | predictors: {', '.join(names)}"
              + (f" | Claude: {llm.model}, {llm.mode}" if llm else ""))
        res = replay_next_change(db, repo, names, parse_date(a.start), parse_date(a.end),
                                 kinds=tuple(x.strip() for x in a.kinds.split(",")), k=a.top_k, limit=a.limit, llm=llm,
                                 on_task=con.task, runtime=a.sandbox, image=a.docker_image)
        con.footer(res)
        if a.json_out:
            with open(a.json_out, "w", encoding="utf-8") as f:
                json.dump(res, f, indent=2, default=str)
        return
    times = list(range(parse_date(a.start), parse_date(a.end), a.every_days * DAY))
    redact = not a.keep_dup_markers
    if a.cmd == "replay" and a.mode == "rebuild":
        out = [replay(db, repo, IN_PROCESS[n](), times, window_days=a.window_days, lookback_days=a.lookback_days,
                      redact=redact) for n in names if n in IN_PROCESS]
        print(json.dumps(out, indent=2))
        return
    llm = make_llm(a, d, names)
    if a.cmd == "replay":
        res = replay_incremental(db, repo, names, times, window_days=a.window_days, lookback_days=a.lookback_days,
                                 redact=redact, llm=llm, runtime=a.sandbox, image=a.docker_image)
        print(json.dumps(res["results"], indent=2)); print(json.dumps(res["run"], indent=2))
    else:
        from .report import Console, render_html
        con = Console(db, featured=names[-1], quiet=a.quiet)
        con.header(a, names, llm, times)
        res = replay_incremental(db, repo, names, times, window_days=a.window_days, lookback_days=a.lookback_days,
                                 redact=redact, llm=llm, code_snapshot=set() if a.no_code_snapshot else {names[-1]}, on_step=con.step, probe=True,
                                 runtime=a.sandbox, image=a.docker_image)
        con.footer(res)
        html = a.html or os.path.join(d, "demo_report.html")
        with open(html, "w", encoding="utf-8") as f:
            f.write(render_html(res, slug=a.slug, args=vars(a), subjects=con.subj))
        print(f"\nreport: {os.path.abspath(html)}")
    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8") as f:
            json.dump(res, f, indent=2, default=str)


if __name__ == "__main__":
    main()
