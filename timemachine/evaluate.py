"""Replay loop: for each T build a leak-proof view, ask the predictor, score against the oracle."""
import statistics
from .groundtruth import Oracle, DAY
from .view import TimeView


def prf(flagged, positives):
    tp = len(flagged & positives)
    p = tp / len(flagged) if flagged else 0.0
    r = tp / len(positives) if positives else 0.0
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


def replay(db_path, repo, predictor, replay_times, window_days=180, lookback_days=30, top_frac=0.2, k=5):
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
        flags = sorted(predictor.risky_commits(view, cands), key=lambda f: -f.score)[:n_top]
        flagged = {f.target for f in flags}
        if buggy:  # no positives at this T: precision/recall are undefined, so skip rather than score 0
            rows["risky"].append(prf(flagged, set(buggy)) + (len(cands), len(buggy)))
        lead += [(buggy[s] - T) / DAY for s in flagged & set(buggy)]
        # Output 2
        covered = oracle.untested_later_covered(T, window_days, shas)
        flags = sorted(predictor.missing_tests(view, cands), key=lambda f: -f.score)[:n_top]
        if covered:
            rows["tests"].append(prf({f.target for f in flags}, set(covered)) + (len(cands), len(covered)))
        # Output 3: each new issue is queried with the view as of just before its creation
        new = oracle.db.execute("SELECT * FROM issues WHERE is_pr=0 AND ts>? AND ts<=?", (T, T + lookback_days * DAY)).fetchall()
        hits = tot = 0
        for iss in new:
            truth = oracle.duplicates_of(iss["number"])
            if not truth:
                continue
            v = TimeView(db_path, repo, iss["ts"] - 1)
            tot += 1
            hits += bool({f.target for f in predictor.similar_issues(v, iss, k)} & truth)
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
