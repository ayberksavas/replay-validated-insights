"""Compare replay results (--json files): headline metrics and, per step, which flagged items were
confirmed by the oracle in one run but not in another. Host-side only (never copied into the sandbox).

    python -m timemachine.compare off.json on.json placebo.json [--predictor claude_insight]
The first file is the baseline; every other file is compared against it.
"""
import argparse
import json


def _row(res, name):
    r = next(x for x in res["results"] if x["predictor"] == name)
    o1, o2, o3 = r["output1_risky_change"], r["output2_missing_tests"], r["output3_similar_issue_hit@k"]
    return {"out1_f1": o1["f1"], "out1_pos": o1["positives"], "out1_pts": o1["replay_points"],
            "out2_f1": o2["f1"], "out2_pos": o2["positives"], "out2_pts": o2["replay_points"],
            "out3_hit": o3["hit_rate"], "out3_n": o3["n_dup_issues"],
            "notes_used": (r.get("sandbox") or {}).get("predictor_stats", {}).get("notes_used", 0)}


def _hits(step, name, key, flag):
    p = (step.get("by_predictor") or {}).get(name)
    return {f["sha"] for f in p[key] if f[flag]} if p else set()


def flips(base, other, name):
    """(gained, lost) confirmed flags of `other` relative to `base`, for Outputs 1 and 2."""
    out = {}
    for key, flag, label in (("risky", "bug_fixed_at", "out1"), ("tests", "covered_at", "out2")):
        g = l = 0
        for a, b in zip(base["steps"], other["steps"]):
            ha, hb = _hits(a, name, key, flag), _hits(b, name, key, flag)
            g, l = g + len(hb - ha), l + len(ha - hb)
        out[label] = (g, l)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m timemachine.compare")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--predictor", default="claude_insight")
    a = ap.parse_args(argv)
    runs = [(f, json.load(open(f, encoding="utf-8"))) for f in a.files]
    keys = ["out1_f1", "out2_f1", "out3_hit", "notes_used"]
    print(f"{'run':28}" + "".join(f"{k:>12}" for k in keys) + "   Out1 flips   Out2 flips   (vs first file)")
    base = runs[0][1]
    for i, (f, res) in enumerate(runs):
        r = _row(res, a.predictor)
        line = f"{f[:27]:28}" + "".join(f"{r[k]!s:>12}" for k in keys)
        if i:
            fl = flips(base, res, a.predictor)
            line += f"   +{fl['out1'][0]}/-{fl['out1'][1]}        +{fl['out2'][0]}/-{fl['out2'][1]}"
        print(line)
    r = _row(base, a.predictor)
    print(f"\nOut1: {r['out1_pos']} positives over {r['out1_pts']} replay points; "
          f"Out2: {r['out2_pos']} over {r['out2_pts']}; Out3: {r['out3_n']} queries.")
    h = next((x for x in base["results"] if x["predictor"] == "kb_heuristic"), None)
    if h:
        print(f"kb_heuristic: out1_f1={h['output1_risky_change']['f1']} out2_f1={h['output2_missing_tests']['f1']} "
              f"out3_hit={h['output3_similar_issue_hit@k']['hit_rate']}")
    print("'+g/-l' = flagged items the oracle confirmed in this run but not the first (g), or the reverse (l).")


if __name__ == "__main__":
    main()
