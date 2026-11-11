"""Replay loops and metric aggregation.

replay()              rebuild mode: a fresh TimeView (copy of all rows <= T) per replay point and per
                      queried issue; the predictor runs in this process. Simple, slow, in-process.
replay_incremental()  incremental time machine: every predictor runs in its own sandbox process that
                      is fed only the events of (previous T, T]. One pass over history, all sandboxes in
                      lockstep, each oracle label computed once. Same metrics as replay() (tested).
"""
import statistics
import time
from collections import Counter, defaultdict

from .groundtruth import Oracle, DAY, DUP_RE
from .view import TimeView


def prf(flagged, positives):
    tp = len(flagged & positives)
    p = tp / len(flagged) if flagged else 0.0
    r = tp / len(positives) if positives else 0.0
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


def redact_dup_markers(text):
    """Remove 'duplicate of #N' from a query issue: the oracle's answer comes from the same text, and the
    stored body is the LATEST edit (maintainers often add the marker after triage)."""
    return DUP_RE.sub("[duplicate reference removed]", text or "")


def _query_issue(row, redact):
    body = row["body"] or ""
    return {"number": row["number"], "title": row["title"], "body": redact_dup_markers(body) if redact else body}


def _top(flags, n_top):
    return sorted(flags, key=lambda f: -f.score)[:n_top]


def replay(db_path, repo, predictor, replay_times, window_days=180, lookback_days=30, top_frac=0.2, k=5, redact=True):
    oracle = Oracle(db_path, repo)
    rows = {"risky": [], "tests": [], "dups": []}
    lead = []
    for T in replay_times:
        view = TimeView(db_path, repo, T)
        cands = view.commits(since=T - lookback_days * DAY)
        if not cands:
            continue
        shas = {c["sha"] for c in cands}
        n_top = max(1, int(len(cands) * top_frac))
        # Output 1
        buggy = {s: t for s, t in oracle.buggy_commits(T, window_days).items() if s in shas}
        flagged = {f.target for f in _top(predictor.risky_commits(view, cands), n_top)}
        if buggy:  # no positives at this T: precision/recall are undefined, so skip rather than score 0
            rows["risky"].append(prf(flagged, set(buggy)) + (len(cands), len(buggy)))
        lead += [(buggy[s] - T) / DAY for s in flagged & set(buggy)]
        # Output 2
        covered = oracle.untested_later_covered(T, window_days, shas)
        flags = _top(predictor.missing_tests(view, cands), n_top)
        if covered:
            rows["tests"].append(prf({f.target for f in flags}, set(covered)) + (len(cands), len(covered)))
        # Output 3: each new issue is queried with the view as of just before its creation
        new = oracle.db.execute("SELECT * FROM issues WHERE is_pr=0 AND ts>? AND ts<=? ORDER BY ts, number",
                                (T, T + lookback_days * DAY)).fetchall()
        hits = tot = 0
        for iss in new:
            truth = oracle.duplicates_of(iss["number"])
            if not truth:
                continue
            v = TimeView(db_path, repo, iss["ts"] - 1)
            tot += 1
            hits += bool({f.target for f in predictor.similar_issues(v, _query_issue(iss, redact), k)} & truth)
        if tot:
            rows["dups"].append((hits / tot, tot))
    return summarize(rows, lead, predictor.name)


def summarize(rows, lead, name):
    def avg(rs, i): return round(statistics.mean(r[i] for r in rs), 3) if rs else None
    return {
        "predictor": name,
        "output1_risky_change": {"precision": avg(rows["risky"], 0), "recall": avg(rows["risky"], 1), "f1": avg(rows["risky"], 2),
                                  "positives": sum(r[4] for r in rows["risky"]), "replay_points": len(rows["risky"]), "median_lead_days": round(statistics.median(lead), 1) if lead else None},
        "output2_missing_tests": {"precision": avg(rows["tests"], 0), "recall": avg(rows["tests"], 1), "f1": avg(rows["tests"], 2),
                                   "positives": sum(r[4] for r in rows["tests"]), "replay_points": len(rows["tests"])},
        "output3_similar_issue_hit@k": {"hit_rate": avg(rows["dups"], 0), "n_dup_issues": sum(r[1] for r in rows["dups"]), "replay_points": len(rows["dups"])},
    }


def plan_checkpoints(oracle, replay_times, lookback_days):
    """All moments the time machine stops at, in time order: replay points T (Outputs 1, 2) and, for each
    new issue with a known duplicate in (T, T+lookback], the second before it was opened (Output 3)."""
    plan = []
    for g, T in enumerate(replay_times):
        plan.append((T, 0, g, None))
        for iss in oracle.db.execute("SELECT * FROM issues WHERE is_pr=0 AND ts>? AND ts<=? ORDER BY ts, number",
                                     (T, T + lookback_days * DAY)):
            truth = oracle.duplicates_of(iss["number"])
            if truth:
                plan.append((iss["ts"] - 1, 1, g, (dict(iss), truth)))
    plan.sort(key=lambda x: (x[0], x[1], x[2]))
    return plan


def replay_incremental(db_path, repo, predictors, replay_times, window_days=180, lookback_days=30, top_frac=0.2,
                       k=5, redact=True, llm=None, options=None, sandbox_dirs=None, code_snapshot=False,  # bool or set of names
                       on_step=None, probe=False, runtime="process", image=None):
    """predictors: names (random, most_changed_files, kb_heuristic, claude_insight). Returns
    {"results": [summary per predictor], "run": run stats, "steps": per-T log}."""
    from .sandbox.host import SandboxHost
    from .timeline import Timeline

    t0 = time.perf_counter()
    oracle = Oracle(db_path, repo)
    replay_times = sorted(replay_times)
    timeline = Timeline(db_path)
    hosts, container = {}, None
    if runtime == "docker":
        from .sandbox.host import ensure_image, container_check, DEFAULT_IMAGE
        ensure_image(image or DEFAULT_IMAGE)
        if probe:
            container = container_check(image or DEFAULT_IMAGE)
    try:
        for name in predictors:
            hosts[name] = SandboxHost(db_path, repo, name, options=(options or {}).get(name), llm=llm,
                                      root=(sandbox_dirs or {}).get(name),
                                      code_snapshot=(name in code_snapshot) if isinstance(code_snapshot, (set, list, tuple)) else code_snapshot,
                                      timeline=timeline, runtime=runtime, **({"image": image} if image else {}))
        rows = {n: {"risky": [], "tests": [], "dups": defaultdict(lambda: [0, 0])} for n in hosts}
        lead = {n: [] for n in hosts}
        active, steps, probes = set(), [], {}
        plan = plan_checkpoints(oracle, replay_times, lookback_days)
        for when, kind, g, payload in plan:
            info = {n: h.advance(when, snapshot=(kind == 0)) for n, h in hosts.items()}
            if kind == 0:
                T = when
                cands = [r["sha"] for r in oracle.db.execute(
                    "SELECT sha FROM commits WHERE ts>=? AND ts<=? ORDER BY ts, sha", (T - lookback_days * DAY, T))]
                step = {"T": T, "candidates": len(cands), "events": next(iter(info.values()))["new_events"] if info else 0,
                        "kb": next(iter(info.values()))["kb"] if info else {}, "code_sha": next((i["code_sha"] for i in info.values() if i.get("code_sha")), None),
                        "by_predictor": {}}
                if not cands:
                    steps.append(step)
                    continue          # same rule as replay(): a T without candidates is skipped entirely
                active.add(g)
                n_top = max(1, int(len(cands) * top_frac))
                buggy = {s: t for s, t in oracle.buggy_commits(T, window_days).items() if s in set(cands)}
                covered = oracle.untested_later_covered(T, window_days, set(cands))
                step.update(positives_risky=len(buggy), positives_tests=len(covered))
                for n, h in hosts.items():
                    top_r = _top(h.risky(cands), n_top)
                    flagged = {f.target for f in top_r}
                    if buggy:
                        rows[n]["risky"].append(prf(flagged, set(buggy)) + (len(cands), len(buggy)))
                    lead[n] += [(buggy[s] - T) / DAY for s in flagged & set(buggy)]
                    top_t = _top(h.tests(cands), n_top)
                    if covered:
                        rows[n]["tests"].append(prf({f.target for f in top_t}, set(covered)) + (len(cands), len(covered)))
                    step["by_predictor"][n] = {
                        "risky": [{"sha": f.target, "score": round(f.score, 3), "why": f.why,
                                   "bug_fixed_at": buggy.get(f.target)} for f in top_r],
                        "tests": [{"sha": f.target, "why": f.why, "covered_at": covered.get(f.target)} for f in top_t]}
                steps.append(step)
                if probe and not probes:
                    probes = {n: h.probe() for n, h in hosts.items()}
                if on_step:
                    on_step(step)
            elif g in active:
                iss, truth = payload
                q = _query_issue(iss, redact)
                for n, h in hosts.items():
                    got = [f.target for f in h.similar(q, k)]
                    rows[n]["dups"][g][0] += bool(set(got) & truth)
                    rows[n]["dups"][g][1] += 1
                    if on_step:
                        on_step({"issue": iss["number"], "title": iss["title"], "at": when, "predictor": n,
                                 "returned": got, "truth": sorted(truth), "hit": bool(set(got) & truth)})
        results = []
        for n, h in hosts.items():
            r = dict(rows[n])
            r["dups"] = [(hit / tot, tot) for _, (hit, tot) in sorted(rows[n]["dups"].items()) if tot]
            s = summarize(r, lead[n], h.predictor)
            ws = h.worker_stats()
            s["sandbox"] = {"pid": h.pid, "dir": h.root, "runtime": h.runtime, "container": h.container, "events_received": h.stats["events_sent"],
                            "steps": h.stats["steps"], "host_requests": {k2: v for k2, v in h.stats.items()
                                                                         if k2 not in ("events_sent", "steps")},
                            "kb": ws["kb"], "predictor_stats": ws["predictor"]}
            results.append(s)
        n_checkpoints = len(plan)
        rebuild_rows = sum(timeline.position(w) for w, *_ in plan)
        run = {"checkpoints": n_checkpoints, "replay_points": len(replay_times), "issue_queries": n_checkpoints - len(replay_times),
               "events_total": len(timeline), "events_fed_once": timeline.position(plan[-1][0]) if plan else 0,
               "rows_a_rebuild_would_copy": rebuild_rows, "wall_seconds": round(time.perf_counter() - t0, 1),
               "llm": llm.report() if llm else None, "probe": probes or None,
               "runtime": runtime, "container_check": container}
        return {"results": results, "run": run, "steps": steps}
    finally:
        for h in hosts.values():
            h.close()
