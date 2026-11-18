"""Demo output: a live terminal timeline (Console) and a self-contained HTML report (render_html).
Host-side display only; it reads commit subjects from the full store, which the sandbox never sees."""
import html
from datetime import datetime, timezone

from .llm import est_tokens
from .store import connect

OUTPUTS = (("output1_risky_change", "Out 1  risky change", "f1"), ("output2_missing_tests", "Out 2  missing tests", "f1"),
           ("output3_similar_issue_hit@k", "Out 3  similar issue (hit@5)", "hit_rate"))


def day(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d") if t else "-"


def fmt(v, nd=3):
    return "n/a" if v is None else (f"{v:.{nd}f}" if isinstance(v, float) else f"{v:,}")


def token_summary(res):
    """Numbers for the token panel: what claude_insight sent vs a naive design."""
    r = next((x for x in res["results"] if x["predictor"] == "claude_insight"), None)
    if not r:
        return None
    st = r["sandbox"]["predictor_stats"]
    llm = res["run"].get("llm") or {}
    return {
        "seen": st.get("commits_seen", 0), "sent": st.get("commits_sent", 0),
        "requests": st.get("requests", 0), "naive_requests": st.get("naive_requests", 0),
        "diff_raw": st.get("diff_chars_raw", 0), "diff_sent": st.get("diff_chars_sent", 0),
        "prompt_tokens": est_tokens(st.get("prompt_chars", 0)), "naive_tokens": est_tokens(st.get("naive_chars", 0)),
        "system_tokens": est_tokens(st.get("system_chars", 0)),
        "issue_queries": st.get("issue_queries", 0), "issue_llm": st.get("issue_llm", 0), "issue_gated": st.get("issue_gated", 0),
        "fallbacks": st.get("fallback_requests", 0), "llm": llm,
    }


class Console:
    def __init__(self, db_path, featured, quiet=False):
        self.db, self.featured, self.quiet = connect(db_path), featured, quiet
        self.subjects = {}

    def subj(self, sha):
        if sha not in self.subjects:
            r = self.db.execute("SELECT message FROM commits WHERE sha=?", (sha,)).fetchone()
            self.subjects[sha] = (r[0].splitlines()[0] if r and r[0] else "")[:72]
        return self.subjects[sha]

    def header(self, a, names, llm, times):
        print(f"\n== TIME MACHINE ==  {a.slug}")
        print(f"replay {a.start} -> {a.end}, every {a.every_days} d ({len(times)} points) | truth window {a.window_days} d "
              f"| candidates = commits of the previous {a.lookback_days} d")
        print(f"predictors, each in its own sandbox process: {', '.join(names)}")
        if llm:
            mode = "LIVE" if llm.mode == "live" else "DRY RUN (no API calls; falls back to the KB heuristic)"
            print(f"claude_insight: {llm.model} | {mode} | token budget {llm.budget:,}")
        if getattr(a, "sandbox", "process") == "docker":
            print(f"sandbox runtime: docker ({a.docker_image or 'python:3.12-slim'}) | --network none, read-only root fs, "
                  "no capabilities, unprivileged user, Python jail on top")
        print(f"timeline below: {self.featured}")

    def step(self, ev):
        if self.quiet:
            return
        if "T" in ev:
            kb = ev.get("kb") or {}
            code = f" | code@{ev['code_sha'][:7]}" if ev.get("code_sha") else ""
            print(f"\n-- {day(ev['T'])}  +{ev['events']:,} events | KB {kb.get('commits', 0):,} commits, "
                  f"{kb.get('issues', 0):,} issues, {kb.get('fixes', 0):,} known fixes{code} | "
                  f"{ev['candidates']} candidates, {ev.get('positives_risky', 0)} later buggy")
            p = ev.get("by_predictor", {}).get(self.featured)
            if not p:
                return
            for i, f in enumerate(p["risky"][:3]):
                hit = f"BUG, fixed {day(f['bug_fixed_at'])} (+{(f['bug_fixed_at'] - ev['T']) // 86400} d)" if f["bug_fixed_at"] else "-"
                print(f"   {'risky' if i == 0 else '     '}  {f['sha'][:7]}  {self.subj(f['sha']):<60.60} {hit}")
                if f.get("why"):
                    print(f"                   why: {f['why']}")
            ok = sum(1 for f in p["tests"] if f["covered_at"])
            print(f"   tests  {ok}/{len(p['tests'])} flagged commits later got tests or a fix")
        elif ev.get("predictor") == self.featured:
            mark = f"HIT (duplicate of #{', #'.join(map(str, ev['truth']))})" if ev["hit"] else f"miss (was a duplicate of #{', #'.join(map(str, ev['truth']))})"
            print(f"   issue  #{ev['issue']} {ev['title'][:50]!r} -> {' '.join('#' + str(n) for n in ev['returned'])}  {mark}")

    def footer(self, res):
        rs = res["results"]
        names = [r["predictor"] for r in rs]
        w = max(14, *(len(n) + 2 for n in names))
        print("\n== RESULTS ==  averaged over replay points that have positives")
        print(" " * 32 + "".join(f"{n:>{w}}" for n in names))
        for key, label, metric in OUTPUTS:
            print(f"{label:<32}" + "".join(f"{fmt(r[key][metric]):>{w}}" for r in rs))
            if metric == "f1":
                print(f"{'    precision / recall':<32}" + "".join(
                    f"{fmt(r[key]['precision'], 2) + ' / ' + fmt(r[key]['recall'], 2):>{w}}" for r in rs))
            n = rs[0][key].get("positives", rs[0][key].get("n_dup_issues"))
            print(f"{'    positives, points':<32}" + f"{str(n) + ', ' + str(rs[0][key]['replay_points']):>{w}}")
        run = res["run"]
        print("\n== INCREMENTAL TIME MACHINE ==")
        ratio = run["rows_a_rebuild_would_copy"] / max(1, run["events_fed_once"])
        print(f"  {run['checkpoints']} checkpoints ({run['replay_points']} replay points + {run['issue_queries']} issue queries), "
              f"one forward pass, {run['wall_seconds']} s")
        print(f"  each sandbox received {run['events_fed_once']:,} events once; rebuilding a view per checkpoint "
              f"would copy {run['rows_a_rebuild_would_copy']:,} rows ({ratio:.1f}x more)")
        docker = run.get("runtime") == "docker"
        print("\n== SANDBOX ==  every predictor ran in its own " + ("container (no network)" if docker else "jailed process"))
        for r in rs:
            sb = r["sandbox"]
            hr = sb["host_requests"]
            where = f"container {sb['container']}" if sb.get("container") else sb["dir"]
            print(f"  {r['predictor']:<20} pid {sb['pid']:<6} events {sb['events_received']:,} | diffs served "
                  f"{hr.get('git_diff', 0)} | refused {hr.get('blocked_requests', 0)} | {where}")
        if run.get("container_check"):
            print("  container check (same docker flags, Python jail OFF: what the container blocks by itself):")
            for k, v in run["container_check"].items():
                print(f"     {k:<38} {v}")
        if run.get("probe"):
            name, probe = next(iter(run["probe"].items()))
            print(f"  leak probe ({name}, at the first replay point, tried on purpose):")
            for k, v in probe.items():
                print(f"     {k:<38} {v}")
        t = token_summary(res)
        if t:
            print("\n== TOKENS ==  claude_insight")
            print(f"  prefilter      {t['seen']:,} candidate commits -> {t['sent']:,} sent to Claude")
            print(f"  batching       {t['requests']:,} requests instead of {t['naive_requests']:,} (one per commit / issue)")
            if t["diff_raw"]:
                print(f"  diffs          {t['diff_raw']:,} chars raw -> {t['diff_sent']:,} sent ({t['diff_raw'] / max(1, t['diff_sent']):.1f}x smaller)")
            if t["naive_tokens"]:
                print(f"  prompt         ~{t['prompt_tokens']:,} tokens vs ~{t['naive_tokens']:,} naive "
                      f"({100 * (1 - t['prompt_tokens'] / t['naive_tokens']):.0f}% less; estimates, 4 chars/token)")
            print(f"  cacheable      ~{t['system_tokens']:,} of those tokens are the fixed system prompts (prompt cache)")
            print(f"  issue search   {t['issue_queries']} queries: {t['issue_llm']} reranked by Claude, {t['issue_gated']} answered by BM25 alone")
            llm = t["llm"]
            if llm:
                c, u = llm.get("calls", {}), llm.get("usage", {})
                print(f"  gateway        mode {llm['mode']} | API calls {c.get('api_calls', 0)} | response-cache hits "
                      f"{c.get('response_cache_hits', 0)} | dry-run {c.get('dry_run', 0)} | budget-skipped {c.get('budget_skipped', 0)}")
                if c.get("api_calls"):
                    cost = llm.get("cost_usd")
                    print(f"  usage          input {u.get('input_tokens', 0):,} | cache read {u.get('cache_read_input_tokens', 0):,} | "
                          f"cache write {u.get('cache_creation_input_tokens', 0):,} | output {u.get('output_tokens', 0):,}"
                          + (f" | ${cost:.2f}" if cost is not None else ""))
                for e in llm.get("errors") or []:
                    print(f"  error          {e}")


CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#6b6a64;--line:#e4e1d8;--card:#fff;--hit:#1f7a4d;--hitbg:#e3f3ea;--miss:#9a5b13;--accent:#3d5a99}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#171716;--fg:#ecebe6;--muted:#a3a19a;--line:#33322f;--card:#1f1f1d;--hit:#6fd3a0;--hitbg:#173326;--miss:#e0a35a;--accent:#9db4ea}}
:root[data-theme=dark]{--bg:#171716;--fg:#ecebe6;--muted:#a3a19a;--line:#33322f;--card:#1f1f1d;--hit:#6fd3a0;--hitbg:#173326;--miss:#e0a35a;--accent:#9db4ea}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1060px;margin:0 auto;padding:28px 16px 60px}h1{font-size:26px;margin:0 0 4px}h2{font-size:18px;margin:34px 0 10px}
.sub{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.big{font-size:26px;font-weight:650;font-variant-numeric:tabular-nums}.lbl{color:var(--muted);font-size:13px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}tr:last-child td{border-bottom:0}
.best{font-weight:700;color:var(--accent)}code{font:13px ui-monospace,Consolas,monospace}
.hit{color:var(--hit);background:var(--hitbg);border-radius:4px;padding:0 5px;font-size:12px;white-space:nowrap}
.miss{color:var(--muted);font-size:12px}.why{color:var(--muted);font-size:13px}
details{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:8px 0;padding:8px 12px}
summary{cursor:pointer;font-weight:600}.wrap{overflow-x:auto}
"""


def render_html(res, slug, args, subjects):
    e = html.escape
    rs = res["results"]
    run = res["run"]
    names = [r["predictor"] for r in rs]
    out = [f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
           f"<title>Time Machine Replay</title><style>{CSS}</style></head><body><main>",
           f"<h1>Time machine replay: {e(slug)}</h1><div class=sub>{e(args['start'])} to {e(args['end'])}, every {args['every_days']} days; "
           f"truth window {args['window_days']} days; candidates are the commits of the previous {args['lookback_days']} days. "
           f"Each predictor ran in its own sandbox {'container' if run.get('runtime') == 'docker' else 'process'} that only ever received data up to the replay point.</div>"]
    ratio = run["rows_a_rebuild_would_copy"] / max(1, run["events_fed_once"])
    probe = next(iter((run.get("probe") or {}).values()), {})
    blocked = sum(1 for v in probe.values() if v.startswith("blocked"))
    t = token_summary(res)
    cards = [(f"{run['checkpoints']}", "checkpoints, one forward pass"), (f"{ratio:.1f}x", "less data moved than rebuilding per checkpoint"),
             (f"{blocked}/{len(probe)}", "leak attempts blocked")]
    if t and t["naive_tokens"]:
        cards.append((f"{100 * (1 - t['prompt_tokens'] / t['naive_tokens']):.0f}%", "fewer prompt tokens than a naive design"))
    out.append("<div class=grid style='margin-top:18px'>" + "".join(f"<div class=card><div class=big>{e(a)}</div><div class=lbl>{e(b)}</div></div>" for a, b in cards) + "</div>")
    out.append("<h2>Results</h2><div class=wrap><table><tr><th>Output</th>" + "".join(f"<th class=n>{e(n)}</th>" for n in names) + "<th class=n>positives, points</th></tr>")
    for key, label, metric in OUTPUTS:
        vals = [r[key][metric] for r in rs]
        best = max((v for v in vals if v is not None), default=None)
        cells = "".join(f"<td class='n{' best' if v is not None and v == best and best > 0 else ''}'>{fmt(v)}</td>" for v in vals)
        n = rs[0][key].get("positives", rs[0][key].get("n_dup_issues"))
        out.append(f"<tr><td>{e(label.split('  ', 1)[1])} <span class=lbl>({e(metric)})</span></td>{cells}<td class=n>{n}, {rs[0][key]['replay_points']}</td></tr>")
    out.append("</table></div><div class=lbl style='margin-top:6px'>Averaged over replay points that have at least one positive. Output 3 queries have 'duplicate of #N' text removed.</div>")
    if t:
        llm = t["llm"] or {}
        c, u = llm.get("calls", {}), llm.get("usage", {})
        rows = [("Prefilter", f"{t['seen']:,} candidate commits, {t['sent']:,} sent to Claude"),
                ("Batching", f"{t['requests']:,} requests instead of {t['naive_requests']:,}"),
                ("Diff compaction", f"{t['diff_raw']:,} chars raw, {t['diff_sent']:,} sent"),
                ("Prompt size (est.)", f"~{t['prompt_tokens']:,} tokens vs ~{t['naive_tokens']:,} naive"),
                ("Prompt cache", f"~{t['system_tokens']:,} tokens are fixed system prompts"),
                ("Issue search", f"{t['issue_llm']} reranked by Claude, {t['issue_gated']} answered by BM25 alone"),
                ("Gateway", f"{llm.get('model', '')}, mode {llm.get('mode', '')}: {c.get('api_calls', 0)} API calls, "
                            f"{c.get('response_cache_hits', 0)} response-cache hits, {c.get('dry_run', 0)} dry-run")]
        if c.get("api_calls"):
            rows.append(("Usage", f"input {u.get('input_tokens', 0):,}, cache read {u.get('cache_read_input_tokens', 0):,}, "
                                  f"cache write {u.get('cache_creation_input_tokens', 0):,}, output {u.get('output_tokens', 0):,}"
                                  + (f", ${llm['cost_usd']:.2f}" if llm.get("cost_usd") is not None else "")))
        out.append("<h2>Token use (claude_insight)</h2><table>" + "".join(f"<tr><td>{e(a)}</td><td>{e(b)}</td></tr>" for a, b in rows) + "</table>")
    out.append("<h2>Sandbox</h2><table><tr><th>Predictor</th><th class=n>pid</th><th class=n>events received</th><th class=n>diffs served</th><th class=n>refused</th></tr>")
    for r in rs:
        sb = r["sandbox"]
        out.append(f"<tr><td>{e(r['predictor'])}</td><td class=n>{sb['pid']}</td><td class=n>{sb['events_received']:,}</td>"
                   f"<td class=n>{sb['host_requests'].get('git_diff', 0)}</td><td class=n>{sb['host_requests'].get('blocked_requests', 0)}</td></tr>")
    out.append("</table>")
    cc = run.get("container_check")
    if cc:
        out.append("<p class=lbl>Each sandbox ran in a Docker container (--network none, read-only root fs, no capabilities, "
                   "unprivileged user). Container check with the same flags and the Python jail OFF:</p><table>" + "".join(
            f"<tr><td>{e(k)}</td><td>{'<span class=hit>' + e(v) + '</span>' if v.startswith('blocked') else e(v)}</td></tr>"
            for k, v in cc.items()) + "</table>")
    if probe:
        out.append("<p class=lbl>Leak probe, run inside a sandbox at the first replay point:</p><table>" + "".join(
            f"<tr><td>{e(k)}</td><td>{'<span class=hit>' + e(v) + '</span>' if v.startswith('blocked') else '<b>' + e(v) + '</b>'}</td></tr>" for k, v in probe.items()) + "</table>")
    feat = names[-1]
    out.append(f"<h2>Timeline ({e(feat)})</h2>")
    for s in res["steps"]:
        p = s.get("by_predictor", {}).get(feat)
        head = f"{day(s['T'])} &middot; +{s['events']:,} events &middot; {s['candidates']} candidates, {s.get('positives_risky', 0)} later buggy"
        if not p:
            out.append(f"<details><summary>{head}</summary><div class=lbl>no candidate commits</div></details>")
            continue
        items = []
        for f in p["risky"]:
            tag = (f"<span class=hit>bug, fixed {day(f['bug_fixed_at'])}</span>" if f["bug_fixed_at"] else "<span class=miss>no later fix</span>")
            why = f"<div class=why>{e(f['why'])}</div>" if f.get("why") else ""
            items.append(f"<tr><td><code>{e(f['sha'][:7])}</code></td><td>{e(subjects(f['sha']))}{why}</td><td>{tag}</td></tr>")
        hits = sum(1 for f in p["risky"] if f["bug_fixed_at"])
        out.append(f"<details{' open' if hits else ''}><summary>{head} &middot; {hits} flagged were buggy</summary><table>{''.join(items)}</table></details>")
    out.append("</main></body></html>")
    return "\n".join(out)


NC_CSS = """pre{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px;overflow-x:auto;
font:12.5px/1.45 ui-monospace,Consolas,monospace;white-space:pre}.add{color:var(--hit)}.del{color:#b4443a}
.task h3{font-size:14px;margin:14px 0 6px}.ok{color:var(--hit);font-weight:600}.no{color:var(--muted)}"""


def _diff_html(text):
    e = html.escape
    rows = []
    for line in (text or "").splitlines():
        cls = "add" if line.startswith("+") else ("del" if line.startswith("-") else "")
        rows.append(f"<span class={cls}>{e(line)}</span>" if cls else e(line))
    return "\n".join(rows)


def render_nextchange_html(res, records, slug, args, featured):
    """Report for `tm nextchange`: the scores, then every task with what was asked, what really changed,
    and what each predictor guessed."""
    from .nextchange import METRICS, day as nday
    e = html.escape
    rs, run = res["results"], res["run"]
    names = [r["predictor"] for r in rs]
    kinds = [k for k in ("issue", "pr") if any(k in r for r in rs)]
    key = "all" if len(kinds) > 1 else (kinds[0] if kinds else "all")
    by = {r["predictor"]: r.get(key, {}) for r in rs}
    pct = lambda v: "n/a" if v is None else f"{100 * v:.0f}%"
    out = [f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
           f"<title>Next Change Replay</title><style>{CSS}{NC_CSS}</style></head><body><main>",
           f"<h1>Next code change: {e(slug)}</h1><div class=sub>Each task is a real commit and the {' or '.join(kinds)} that asked for it. "
           f"One second before the commit, every predictor got only the task number and the code as it was then, inside its own "
           f"sandbox, and predicted which files the change would touch. {e(args['start'])} to {e(args['end'])}.</div>"]
    best = by.get("kb_heuristic") or by.get(featured) or {}
    rnd = by.get("random") or {}
    cards = [(f"{run['tasks_scored']}", "real changes replayed"),
             (pct(best.get("hit@1")), "first guess was a file the developers changed (KB heuristic)"),
             (pct(best.get("hit@3")), "right file in the top 3 (KB heuristic)"),
             (pct(rnd.get("hit@1")), "first guess right when picking at random")]
    out.append("<div class=grid style='margin-top:18px'>" + "".join(
        f"<div class=card><div class=big>{e(a)}</div><div class=lbl>{e(b)}</div></div>" for a, b in cards) + "</div>")
    out.append(f"<h2>Results ({e(key)} tasks)</h2><div class=wrap><table><tr><th>Metric</th>"
               + "".join(f"<th class=n>{e(n)}</th>" for n in names) + "</tr>")
    labels = {"hit@1": "first guess right (hit@1)", "hit@3": "right file in top 3 (hit@3)",
              "recall@5": "share of changed files in top 5", "mrr": "mean reciprocal rank", "line_f1": "patch: changed-line F1"}
    for m in METRICS + ("line_f1",):
        vals = [by[n].get(m) for n in names]
        if all(v is None for v in vals):
            continue
        top = max((v for v in vals if v is not None), default=None)
        out.append(f"<tr><td>{e(labels[m])}</td>" + "".join(
            f"<td class='n{' best' if v is not None and v == top and top > 0 else ''}'>{fmt(v)}</td>" for v in vals) + "</tr>")
    out.append("</table></div>")
    st = next((r["sandbox"]["predictor_stats"] for r in rs if r["predictor"] == "claude_insight"), None)
    llm = run.get("llm") or {}
    if st:
        est = (st.get("change_prompt_chars", 0) + st.get("patch_est_chars", 0)) // 4
        naive = st.get("change_naive_chars", 0) // 4
        mode = llm.get("mode", "dry-run")
        note = ("Claude was not called (no API key): claude_insight used the KB heuristic's ranking, so its scores equal it. "
                "Each task below shows the compact request Claude would have received." if mode == "dry-run" else
                f"Claude ({e(llm.get('model', ''))}) was called live.")
        out.append(f"<p class=lbl>{note} Prompt size for all tasks: ~{est:,} tokens (localization + patch requests) "
                   f"versus ~{naive:,} for sending each shortlist in full (estimates, 4 characters per token).</p>")
    out.append(f"<h2>Tasks</h2><p class=lbl>Result marks are for {e(featured)}. Tasks are in time order.</p>")
    shown_open = 0
    for i, rec in enumerate(records, 1):
        t, sc = rec["task"], rec["scores"].get(featured, {})
        mark = ("<span class=hit>first guess right</span>" if sc.get("hit@1") else
                "<span class=hit>in top 3</span>" if sc.get("hit@3") else "<span class=miss>missed</span>")
        is_open = sc.get("hit@1") and shown_open < 4
        shown_open += bool(is_open)
        target = set(t["target"])
        rows = []
        for n in names:
            p = rec["preds"].get(n, {})
            cells = "<br>".join(f"<span class={'ok' if f in target else 'no'}>{'&#10003; ' if f in target else ''}{e(f)}</span>"
                                for f in p.get("files", [])[:3]) or "<span class=no>-</span>"
            s = rec["scores"].get(n, {})
            res_txt = "first guess" if s.get("hit@1") else ("top 3" if s.get("hit@3") else "miss")
            rows.append(f"<tr><td>{e(n)}</td><td>{cells}</td><td>{res_txt}</td></tr>")
        fp = rec["preds"].get(featured, {})
        extra = ""
        if fp.get("why"):
            extra += f"<h3>Claude's reasoning</h3><p>{e(fp['why'])}</p>"
        if fp.get("patch"):
            extra += f"<h3>Claude's patch</h3><pre>{_diff_html(fp['patch'])}</pre>"
        prev = next((p.get("preview") for p in rec["preds"].values() if p.get("preview")), None)
        if prev:
            extra += (f"<details><summary>What Claude would receive (~{len(prev) // 4:,} tokens; the code itself is not sent "
                      f"at this step)</summary><pre>{e(prev)}</pre></details>")
        out.append(
            f"<details class=task{' open' if is_open else ''}><summary>{i}. {nday(t['ts'])} &middot; {e(t['kind'])} #{t['number']}: "
            f"{e(t['title'][:90])} &middot; {mark}</summary>"
            f"<h3>What was asked (written before the change)</h3><p>{e(rec['body'][:500]) or '<span class=no>(no description)</span>'}</p>"
            f"<h3>What the developers changed: <code>{e(t['id'])}</code> {e(t['subject'])}</h3>"
            f"<pre>{_diff_html(rec['real_diff'])}</pre>"
            f"<h3>What each predictor guessed (top 3, &#10003; = a file that really changed)</h3>"
            f"<table><tr><th>Predictor</th><th>Files</th><th>Result</th></tr>{''.join(rows)}</table>{extra}</details>")
    out.append("</main></body></html>")
    return "\n".join(out)
