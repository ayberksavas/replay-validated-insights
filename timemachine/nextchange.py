"""Output 8 (extension, not in PROJECT.pdf): predict the next code change.

A task is a real commit C on the main line whose intent was written down BEFORE C landed:
- kind "issue": an issue that C closes (in C's message, in the description of the pull request C came
  from, or a GitHub "closed" event that names C)
- kind "pr": the pull request C came from (its title and description), when no issue is linked
At T = one second before C, each sandbox gets only the task's kind and number. It reads the words from its
own store (written before T), looks at the code as of T, and predicts which code files C modifies and,
optionally, the patch. The host scores the prediction against C itself.

Host only: it reads the full history. Never copied into a sandbox.
"""
import json
import re
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

from .gitutil import run_git, GitError
from .kb.links import refs, closes
from .snapshot import commit_at
from .store import connect

METRICS = ("hit@1", "hit@3", "recall@5", "mrr")
LINE_METRICS = ("line_precision", "line_recall", "line_f1")
_TRIVIAL = re.compile(r"^[\s{}()\[\];:,.'\"`]*$")


def day(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


def mine_tasks(db_path, repo, start, end, kinds=("issue", "pr")):
    db = connect(db_path)
    main_line = set(run_git(repo, "rev-list", "--first-parent", "HEAD").split())
    items = {r["number"]: dict(r) for r in db.execute("SELECT * FROM issues")}
    closed_by = defaultdict(set)       # commit sha -> issues GitHub says it closed
    for e in db.execute("SELECT number, data FROM issue_events WHERE type='closed'"):
        sha = json.loads(e["data"] or "{}").get("commit_id")
        if sha:
            closed_by[sha].add(e["number"])
    tasks = []
    for c in db.execute("SELECT * FROM commits WHERE ts>? AND ts<=? ORDER BY ts, sha", (start, end)):
        if c["sha"] not in main_line:
            continue
        target = sorted(r[0] for r in db.execute(
            "SELECT DISTINCT path FROM commit_files WHERE sha=? AND is_test=0 AND is_doc=0", (c["sha"],)))
        if not target:
            continue
        subject = (c["message"] or "").split("\n")[0]
        before = lambda n: n in items and items[n]["ts"] < c["ts"]
        prs = [n for n in sorted(refs(subject)) if before(n) and items[n]["is_pr"]]
        linked = closes(c["message"]) | closed_by.get(c["sha"], set())
        for p in prs:
            linked |= closes(items[p]["body"] or "")
        linked = sorted(n for n in linked if before(n) and not items[n]["is_pr"])
        if linked and "issue" in kinds:
            kind, number = "issue", linked[0]
        elif prs and "pr" in kinds:
            kind, number = "pr", prs[0]
        else:
            continue
        tasks.append({"id": f"{c['sha'][:7]}", "kind": kind, "number": number, "sha": c["sha"], "ts": c["ts"],
                      "T": c["ts"] - 1, "subject": subject[:100], "title": items[number]["title"], "target": target})
    return tasks


def safe_start(repo, task):
    """The code snapshot at T must be an ancestor of C and must not contain C (committer dates can be out
    of order after rebases). Returns the snapshot sha, or None when the task cannot be replayed safely."""
    try:
        snap = commit_at(repo, task["T"])
        run_git(repo, "merge-base", "--is-ancestor", snap, task["sha"])
    except (ValueError, GitError):
        return None
    return snap if snap != task["sha"] else None


def changed_lines(diff: str, keep=None) -> set:
    """{(path, '+'|'-', normalized line)} from a unified diff; blank and bracket-only lines are ignored."""
    out, old, path = set(), None, None
    for line in (diff or "").splitlines():
        if line.startswith("--- "):
            old = line[6:] if line.startswith("--- a/") else None
            continue
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else old
            continue
        if line and line[0] in "+-" and path and (keep is None or path in keep):
            norm = " ".join(line[1:].split())
            if len(norm) >= 3 and not _TRIVIAL.match(norm):
                out.add((path, line[0], norm))
    return out


def score(pred: dict, task: dict, real: set) -> dict:
    target = set(task["target"])
    ranked = list(dict.fromkeys(pred.get("files") or []))
    first = next((i for i, p in enumerate(ranked) if p in target), None)
    s = {"hit@1": float(bool(set(ranked[:1]) & target)), "hit@3": float(bool(set(ranked[:3]) & target)),
         "recall@5": len(set(ranked[:5]) & target) / len(target), "mrr": 1.0 / (first + 1) if first is not None else 0.0}
    if pred.get("patch"):
        p = changed_lines(pred["patch"])
        tp = len(p & real)
        prec = tp / len(p) if p else 0.0
        rec = tp / len(real) if real else 0.0
        s.update(line_precision=prec, line_recall=rec, line_f1=2 * prec * rec / (prec + rec) if tp else 0.0)
    return s


def summarize(rows):
    def mean(key, rs):
        vals = [r[key] for r in rs if key in r]
        return round(statistics.mean(vals), 3) if vals else None
    out = {}
    for kind in ("all", "issue", "pr"):
        rs = [r for r in rows if kind == "all" or r["kind"] == kind]
        if rs:
            out[kind] = {"tasks": len(rs), **{m: mean(m, rs) for m in METRICS},
                         "patches": sum(1 for r in rs if "line_f1" in r), **{m: mean(m, rs) for m in LINE_METRICS}}
    return out


def replay_next_change(db_path, repo, predictors, start, end, kinds=("issue",), k=10, limit=None, llm=None,
                       options=None, on_task=None, runtime="process", image=None):
    from .sandbox.host import SandboxHost
    from .timeline import Timeline
    t0 = time.perf_counter()
    tasks = mine_tasks(db_path, repo, start, end, kinds)
    skipped = Counter()
    timeline, hosts = Timeline(db_path), {}
    rows = {n: [] for n in predictors}
    try:
        for n in predictors:
            hosts[n] = SandboxHost(db_path, repo, n, options=(options or {}).get(n), llm=llm, code_snapshot=True,
                                   timeline=timeline, runtime=runtime, **({"image": image} if image else {}))
        done = 0
        for t in tasks:
            if limit and done >= limit:
                break
            snap = safe_start(repo, t)
            if snap is None:
                skipped["unsafe snapshot"] += 1
                continue
            real = changed_lines(run_git(repo, "show", "--format=", "-U0", "--no-color", t["sha"]), keep=set(t["target"]))
            preds = {}
            for n, h in hosts.items():
                h.advance(t["T"], snapshot=True)
                if h.code.sha != snap:
                    raise RuntimeError(f"sandbox code is at {h.code.sha}, expected {snap}")
                preds[n] = h.predict_change(t, k)
                rows[n].append({"kind": t["kind"], **score(preds[n], t, real)})
            done += 1
            if on_task:
                on_task(t, preds, {n: rows[n][-1] for n in hosts}, snap)
        results = []
        for n, h in hosts.items():
            ws = h.worker_stats()
            results.append({"predictor": h.predictor, **summarize(rows[n]),
                            "sandbox": {"dir": h.root, "events_received": h.stats["events_sent"],
                                        "code_files_reindexed": ws.get("code_files_reindexed"),
                                        "host_requests": {k2: v for k2, v in h.stats.items() if k2 not in ("events_sent", "steps")},
                                        "predictor_stats": ws["predictor"]}})
        return {"results": results, "run": {"tasks_mined": len(tasks), "tasks_scored": done, "skipped": dict(skipped),
                                             "wall_seconds": round(time.perf_counter() - t0, 1),
                                             "llm": llm.report() if llm else None}}
    finally:
        for h in hosts.values():
            h.close()


class Console:
    """Live lines for `tm nextchange`: one task per line, the featured predictor's top files vs the truth."""

    def __init__(self, featured, quiet=False):
        self.featured, self.quiet, self.n = featured, quiet, 0

    def task(self, t, preds, scores, snap):
        self.n += 1
        if self.quiet:
            return
        p, s = preds[self.featured], scores[self.featured]
        mark = "HIT@1" if s["hit@1"] else ("hit@3" if s["hit@3"] else "miss")
        print(f"\n[{self.n}] {day(t['ts'])}  {t['kind']} #{t['number']}: {t['title'][:70]}")
        print(f"     real change {t['id']}: {', '.join(t['target'][:3])}{' ...' if len(t['target']) > 3 else ''}")
        print(f"     {self.featured}: {', '.join(p['files'][:3]) or '-'}  -> {mark}"
              + (f" | patch line F1 {s['line_f1']:.2f}" if "line_f1" in s else ""))
        if p.get("why"):
            print(f"     why: {p['why'][:110]}")

    @staticmethod
    def footer(res):
        rs = res["results"]
        names = [r["predictor"] for r in rs]
        w = max(14, *(len(n) + 2 for n in names))
        run = res["run"]
        print(f"\n== NEXT CHANGE ==  {run['tasks_scored']} tasks scored ({run['tasks_mined']} mined, skipped {run['skipped'] or 0}), {run['wall_seconds']} s")
        kinds = [k for k in ("issue", "pr") if any(k in r for r in rs)]
        for kind in (["all"] if len(kinds) > 1 else []) + kinds:
            print(f"\n  {kind} tasks ({rs[0].get(kind, {}).get('tasks', 0)})" + " " * 12 + "".join(f"{n:>{w}}" for n in names))
            for m in METRICS + ("line_f1",):
                vals = [r.get(kind, {}).get(m) for r in rs]
                if any(v is not None for v in vals):
                    print(f"  {m:<30}" + "".join(f"{('n/a' if v is None else f'{v:.3f}'):>{w}}" for v in vals))
        for r in rs:
            st = r["sandbox"]["predictor_stats"]
            if st.get("change_tasks"):
                est = st.get("change_prompt_chars", 0) + st.get("patch_est_chars", 0)
                print(f"\n  {r['predictor']}: {st.get('requests', 0)} requests prepared, ~{est // 4:,} prompt tokens "
                      f"vs ~{st.get('change_naive_chars', 0) // 4:,} naive (estimates, 4 chars/token"
                      + (", patch requests estimated from the heuristic's picks" if st.get("patch_est_chars") else "") + ")")
                print(f"  patches {st.get('patches', 0)}, edits applied {st.get('edits_applied', 0)}, dropped {st.get('edits_failed', 0)}")
        llm = run.get("llm")
        if llm:
            c = llm.get("calls", {})
            print(f"  gateway: mode {llm['mode']}, API calls {c.get('api_calls', 0)}, cache hits {c.get('response_cache_hits', 0)}, "
                  f"dry-run {c.get('dry_run', 0)}" + (f", ${llm['cost_usd']:.2f}" if c.get("api_calls") and llm.get("cost_usd") is not None else ""))
