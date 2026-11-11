"""ClaudeInsight: knowledge-base-grounded Claude review, built to spend few tokens.

Runs inside the sandbox. It never sees an API key and has no network: requests go to the host's LLM
gateway through view.llm(), which caches responses, meters tokens and enforces a budget. With no
LLM available (dry run, budget spent, error) every method falls back to the KB heuristic, so the
predictor always returns a full ranking.

Token levers (each one is counted in `stats`):
1. Prefilter: the KB heuristic picks the top `prefilter` share of candidates (bounded by
   min_llm/max_llm); docs-only commits are never sent. Claude only re-ranks a short list.
2. Batching: ONE request per time step covers Output 1 (risk) and Output 2 (needs tests).
3. Compact packets: short ids (c1, c2) instead of 40-char SHAs, file history as numbers from the
   KB, diffs cut to changed code lines (no context lines, no test/doc files), capped per file and
   per commit.
4. Prompt caching: the long instructions are a fixed system prompt (cache_control on the host);
   only the per-step packets vary.
5. Issue search: BM25 retrieves; Claude reranks only when BM25 is ambiguous (top hit not clearly
   ahead), from titles and short excerpts.
6. The host caches responses by request hash: re-running a replay costs zero tokens.
"""
import difflib
import json
import math
import re
from collections import Counter

from .heuristics import KBHeuristic
from .predictor import Flag, task_text

COMMIT_SYSTEM = """You are a senior reviewer inside a software-quality assistant. You see commits that \
landed in an open-source repository during the last few weeks, as compact evidence packets built from \
the repository's own history. You see nothing that happened after these commits, so judge from the \
evidence only.

For every packet return:
- risk (integer 0-100): how likely it is that this commit introduced a defect that a later bug-fix \
commit will have to repair (the fix would change lines this commit wrote).
- needs_tests (integer 0-100): how likely it is that this commit changed behaviour without adequate \
tests, so that tests for this code get added later or a later bug fix touches it.
- why: at most 12 words naming the main reason. No preamble.

Packet format:
  <id> | <commit subject>
  author: <n> earlier commits in this repo
  files: <code path> +<added>/-<deleted> hist=<commits touching it>c/<earlier bug fixes on it>f  (one per line)
  tests touched: yes|no
  diff: changed code lines only, '+' added, '-' removed, '@@' gives the enclosing function; test and \
documentation files and unchanged context lines are left out; long diffs are cut.

What usually raises risk:
- new or rewritten logic (conditions, parsing, error handling, defaults, encodings, concurrency, \
platform-specific paths), especially in files with several earlier bug fixes;
- large or scattered changes across many code files; changes to public APIs or behaviour others rely on;
- a fix that only treats a symptom, or touches code that was fixed repeatedly before;
- authors with few earlier commits in this repository.
What usually lowers risk:
- typo, formatting, typing-only, comments, version bumps, dependency pins, renames with no logic change;
- small, local changes that come with tests;
- reverts of a recent change (they restore known-good code).
For needs_tests, behaviour changes without 'tests touched: yes' score high; pure refactors, \
documentation and build/CI changes score low.

Calibrate across the batch: typically only about one commit in five is later traced to a bug. Use \
the whole 0-100 range and make the scores rank the packets; do not give everything a middle value. \
Return exactly one entry per packet id, using the ids as given."""

COMMIT_SCHEMA = {
    "type": "object",
    "properties": {"commits": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "risk": {"type": "integer"},
                       "needs_tests": {"type": "integer"}, "why": {"type": "string"}},
        "required": ["id", "risk", "needs_tests", "why"], "additionalProperties": False}}},
    "required": ["commits"], "additionalProperties": False,
}

ISSUE_SYSTEM = """You help maintainers triage a NEW issue report by finding EARLIER reports of the same \
problem, so the earlier discussion or fix can be reused and the new report closed as a duplicate.

You get the new report (title and the start of its body) and a short list of earlier reports \
retrieved by keyword search. Each candidate line is:
  <id> [<state>, <labels>] <title> | <start of body>
Bodies are truncated and whitespace is collapsed; stack traces and code may be cut.

For every candidate return same_problem (integer 0-100): how likely the new report describes the same \
underlying problem (same bug, same missing feature, same confusing behaviour) as that candidate.
- Shared words are not enough: the same component AND the same symptom or request are needed.
- A candidate about the same function but a different failure is low.
- A closed candidate with the same symptom is the best possible answer: it may already have a fix.
- When nothing matches, give every candidate a low score.
Return one entry per candidate id, using the ids as given."""

ISSUE_SCHEMA = {
    "type": "object",
    "properties": {"matches": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "same_problem": {"type": "integer"}},
        "required": ["id", "same_problem"], "additionalProperties": False}}},
    "required": ["matches"], "additionalProperties": False,
}

_WS = re.compile(r"\s+")
_DIFF_FILE = re.compile(r"^diff --git a/(.*?) b/(.*)$")
_SKIP = ("index ", "--- ", "+++ ", "new file", "deleted file", "similarity", "rename ", "old mode", "new mode",
         "Binary", "copy ", "dissimilarity")


def clip(text, n):
    text = _WS.sub(" ", text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def compact_diff(diff: str, keep, max_lines=40, per_file=12, width=110) -> tuple[str, int]:
    """Changed code lines only. Returns (text, number of changed lines left out)."""
    out, total, dropped = [], 0, 0
    skip, header, n_file = True, None, 0
    for line in diff.splitlines():
        m = _DIFF_FILE.match(line)
        if m:
            path = m.group(2)
            skip, header, n_file = not keep(path), f"# {path}", 0
            continue
        if skip or line.startswith(_SKIP) or not line:
            continue
        if line.startswith("@@"):
            ctx = line.rsplit("@@", 1)[-1].strip()
            if not ctx:
                continue
            text = "@@ " + ctx
        elif line[0] in "+-":
            body = line[1:].rstrip()
            if not body.strip():
                continue
            indent = len(body.expandtabs(4)) - len(body.expandtabs(4).lstrip())
            text = line[0] + " " * (indent // 4) + body.strip()
        else:
            continue                            # unchanged context line
        if n_file >= per_file or total >= max_lines:
            dropped += line[0] in "+-"
            continue
        if header:
            out.append(header)
            header = None
        out.append(text[:width])
        n_file += 1
        total += 1
    if dropped:
        out.append(f"… {dropped} more changed lines")
    return "\n".join(out), dropped


def _parse(resp, key=None):
    text = (resp or {}).get("text")
    if not text:
        return None
    try:
        data = json.loads(text)
        return data if key is None else data[key]
    except (ValueError, KeyError, TypeError):
        return None


LOCALIZE_SYSTEM = """You are a senior engineer planning a code change in an open-source repository. You get a \
TASK, written before the change was made: an issue report (a bug or a request) or a pull-request \
description, with the discussion so far. You also get a short list of candidate source files as they \
are right now, each with its size and the main names it defines. You cannot see how the repository \
looks later; decide from the task and the code only.

Return the files the change will most likely MODIFY. For each file you pick:
- id: the candidate id exactly as given (f1, f2, ...)
- confidence (integer 0-100): how likely this file is modified by the change
- where: the functions, classes, options or constants to change, at most 12 words

How to decide:
- The file that IMPLEMENTS the behaviour named in the task beats files that only call it, re-export it, \
or document it. Wrappers around a command line usually keep each command's options in one module per \
command: an option or flag request lands where that command's arguments are built.
- When the task names a function, method, parameter, option, class or error message, the file that \
defines it is the strongest candidate. Error messages and tracebacks name the failing function.
- Most changes modify one source file, sometimes two or three. Pick at most three files and give the \
rest no entry. Order your picks by confidence.
- Packaging, configuration and test files are only picked when the task is clearly about them.
- If no candidate fits well, still return your best guess with a low confidence.
Return JSON only, matching the schema."""

LOCALIZE_SCHEMA = {
    "type": "object",
    "properties": {"files": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "confidence": {"type": "integer"}, "where": {"type": "string"}},
        "required": ["id", "confidence", "where"], "additionalProperties": False}}},
    "required": ["files"], "additionalProperties": False,
}

PATCH_SYSTEM = """You write the code change for a TASK in an open-source repository, as search-and-replace \
edits on the files shown. The task (an issue report or a pull-request description, with the discussion so \
far) was written before the change; the files are shown exactly as they are now, sometimes only the \
relevant parts (a line with "..." marks code left out).

Return:
- edits: a list of edits. Each edit has
  - file: the path exactly as shown after "===",
  - find: an exact, contiguous snippet copied from that file, including its indentation, long enough to \
occur exactly once in the file (usually 2 to 6 lines),
  - replace: the new text for that snippet.
- summary: one sentence saying what the change does.

Rules:
- Make the smallest change that does what the task asks. Keep the file's style, naming and indentation.
- Do not reformat or reorder code you are not changing, and do not touch unrelated code.
- To add new code, use an edit whose find is an existing nearby line and whose replace keeps that line \
and adds the new code before or after it.
- Never copy the "..." markers or the "===" headers into find or replace.
- Each find must match the file as shown, character for character; an edit whose find does not match \
exactly once is dropped.
- Prefer changing the implementation over adding workarounds in callers. Add or adjust a parameter, a \
default, a check or a branch where the behaviour is decided.
Return JSON only, matching the schema."""

PATCH_SCHEMA = {
    "type": "object",
    "properties": {"edits": {"type": "array", "items": {
        "type": "object",
        "properties": {"file": {"type": "string"}, "find": {"type": "string"}, "replace": {"type": "string"}},
        "required": ["file", "find", "replace"], "additionalProperties": False}},
        "summary": {"type": "string"}},
    "required": ["edits", "summary"], "additionalProperties": False,
}

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def excerpt(text: str, where: str, max_lines: int = 300, window: int = 60) -> str:
    """The whole file when it is short; otherwise the parts around the names Claude said it would change."""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    names = set(_IDENT.findall(where or ""))
    starts = [i for i, l in enumerate(lines) if any(re.match(rf"\s*(?:async\s+def|def|class)\s+{re.escape(n)}\b", l) for n in names)]
    if not starts:
        return "\n".join(lines[:max_lines]) + "\n..."
    keep = set()
    for s in starts:
        keep.update(range(max(0, s - 5), min(len(lines), s + window)))
    out, prev = [], -2
    for i in sorted(keep)[:max_lines]:
        if i != prev + 1:
            out.append("...")
        out.append(lines[i])
        prev = i
    return "\n".join(out) + ("\n..." if prev < len(lines) - 1 else "")


def apply_edits(code, edits, allowed):
    """Apply search/replace edits to copies of the files; returns (unified diff, applied, failed)."""
    new, applied, failed = {}, 0, 0
    for e in edits or []:
        p = e.get("file")
        if p not in allowed:
            failed += 1
            continue
        cur = new[p] if p in new else code.read(p)
        find = e.get("find") or ""
        if find and cur.count(find) == 1:
            new[p] = cur.replace(find, e.get("replace") or "", 1)
            applied += 1
        else:
            new.setdefault(p, cur)
            failed += 1
    parts = []
    for p, text in new.items():
        old = code.read(p)
        if text != old:
            diff = difflib.unified_diff(old.splitlines(True), text.splitlines(True), f"a/{p}", f"b/{p}", n=0)
            parts.extend(l if l.endswith("\n") else l + "\n" for l in diff)
    return "".join(parts) or None, applied, failed


class ClaudeInsight(KBHeuristic):
    name = "claude_insight"

    def __init__(self, prefilter=0.5, min_llm=3, max_llm=12, diff_lines=40, per_file=12, issue_pool=8,
                 gate_ratio=2.0, effort="low", measure_naive=True, change_pool=8, patch_files=2, patch_lines=300,
                 patch_effort="medium", write_patches=True):
        super().__init__()
        self.change_pool, self.patch_files, self.patch_lines = change_pool, patch_files, patch_lines
        self.patch_effort, self.write_patches = patch_effort, write_patches
        self.prefilter, self.min_llm, self.max_llm = prefilter, min_llm, max_llm
        self.diff_lines, self.per_file = diff_lines, per_file
        self.issue_pool, self.gate_ratio, self.effort = issue_pool, gate_ratio, effort
        self.measure_naive = measure_naive
        self.stats = Counter()
        self._review_key, self._review = None, None
        self._diffs = (None, {})      # diffs of the current step, fetched from the host once

    # ---- Outputs 1 + 2: one batched review per step ----
    def _packet(self, view, kb, cid, sha, f, compact=True):
        c = kb.commit(sha)
        code = [x for x in kb.commit_files(sha) if not x["is_test"] and not x["is_doc"]]
        lines = [f"{cid} | {clip(c['message'].splitlines()[0] if c['message'] else '', 100)}",
                 f"author: {f['author_prior_commits']} earlier commits in this repo", "files:"]
        for x in code[:8]:
            h = kb.file_stats(x["path"], exclude_sha=sha)
            lines.append(f"  {x['path']} +{x['added'] or 0}/-{x['deleted'] or 0} hist={h['commits']}c/{h['fixes']}f")
        if len(code) > 8:
            lines.append(f"  … {len(code) - 8} more code files")
        lines.append(f"tests touched: {'yes' if f['touches_tests'] else 'no'}")
        if self._diffs[0] != view.as_of:
            self._diffs = (view.as_of, {})
        raw = self._diffs[1].get(sha)
        if raw is None:
            raw = self._diffs[1][sha] = view.diff(sha)
        paths = {x["path"] for x in code}
        if compact:
            d, _ = compact_diff(raw, lambda p: p in paths, self.diff_lines, self.per_file)
        else:
            d = raw
        self.stats["diff_chars_raw"] += len(raw)
        self.stats["diff_chars_sent" if compact else "diff_chars_naive"] += len(d)
        lines.append("diff:\n" + d if d else "diff: (no code lines)")
        return "\n".join(lines)

    def _reviewed(self, view, candidates):
        key = (view.as_of, tuple(c["sha"] for c in candidates))
        if key == self._review_key:
            return self._review
        kb = self.kb(view)
        feats = {c["sha"]: kb.commit_features(c["sha"]) for c in candidates}
        code = [s for s, f in feats.items() if not f["docs_only"] and f["n_code_files"]]
        m = max(self.min_llm, min(self.max_llm, math.ceil(self.prefilter * len(candidates))))
        by_risk = sorted(code, key=lambda s: -self.risk_score(feats[s]))[:m]
        by_tests = sorted((s for s in code if self.needs_tests_candidate(feats[s])), key=lambda s: -self.tests_score(feats[s]))[:m]
        chosen = list(dict.fromkeys(by_risk + by_tests))
        self.stats["commits_seen"] += len(candidates)
        self.stats["commits_sent"] += len(chosen)
        if self.measure_naive:   # what "every candidate, full diff, one request each" would have sent
            for i, s in enumerate(feats):
                if s in kb._commits:
                    self.stats["naive_chars"] += len(COMMIT_SYSTEM) + len(self._packet(view, kb, f"c{i + 1}", s, feats[s], compact=False))
            self.stats["naive_requests"] += len(feats)
        result = {}
        if chosen:
            ids = {f"c{i + 1}": s for i, s in enumerate(chosen)}
            user = "\n\n".join(self._packet(view, kb, cid, s, feats[s]) for cid, s in ids.items())
            self.stats["requests"] += 1
            self.stats["prompt_chars"] += len(COMMIT_SYSTEM) + len(user)
            self.stats["system_chars"] += len(COMMIT_SYSTEM)
            resp = view.llm(system=COMMIT_SYSTEM, user=user, schema=COMMIT_SCHEMA, effort=self.effort,
                            max_tokens=4000, tag="commits")
            rows = _parse(resp, "commits")
            if rows is None:
                self.stats["fallback_requests"] += 1
                result = None
            else:
                for r in rows:
                    s = ids.get(str(r.get("id")))
                    if s:
                        result[s] = {"risk": max(0, min(100, int(r.get("risk", 0)))),
                                     "needs_tests": max(0, min(100, int(r.get("needs_tests", 0)))),
                                     "why": clip(r.get("why", ""), 120)}
                self.stats["llm_scored"] += len(result)
        self._review_key, self._review = key, result
        return result

    @staticmethod
    def _norm(scores):
        lo, hi = min(scores.values(), default=0), max(scores.values(), default=0)
        return {s: (v - lo) / (hi - lo) if hi > lo else 0.0 for s, v in scores.items()}

    def risky_commits(self, view, candidates):
        kb = self.kb(view)
        h = {c["sha"]: self.risk_score(kb.commit_features(c["sha"])) for c in candidates}
        rev = self._reviewed(view, candidates)
        if rev is None:   # no LLM answer: exactly the heuristic ranking
            return [Flag(s, v) for s, v in h.items()]
        hn = self._norm(h)
        out = []
        for s in h:     # reviewed commits rank above unreviewed ones; Claude orders the short list
            r = rev.get(s)
            out.append(Flag(s, 1 + r["risk"] / 100 + 1e-3 * hn[s], r["why"]) if r else Flag(s, 0.999 * hn[s]))
        return out

    def missing_tests(self, view, candidates):
        kb = self.kb(view)
        feats = {c["sha"]: kb.commit_features(c["sha"]) for c in candidates}
        h = {s: self.tests_score(f) for s, f in feats.items() if self.needs_tests_candidate(f)}
        rev = self._reviewed(view, candidates)
        if rev is None:
            return [Flag(s, v) for s, v in h.items()]
        hn = self._norm(h)
        out = []
        for s in h:
            r = rev.get(s)
            out.append(Flag(s, 1 + r["needs_tests"] / 100 + 1e-3 * hn[s], r["why"]) if r else Flag(s, 0.999 * hn[s]))
        return out

    # ---- Output 3: BM25 retrieve, Claude rerank when ambiguous ----
    def similar_issues(self, view, issue, k):
        kb = self.kb(view)
        base = super().similar_issues(view, issue, max(k, self.issue_pool))   # explicit #refs first, then BM25
        self.stats["issue_queries"] += 1
        text = f"{issue['title']} {issue['body'] or ''}"
        bm = kb.search_issues(text, k=max(k, self.issue_pool), exclude={issue["number"]})
        if self.measure_naive:
            pool = kb.search_issues(text, k=20, exclude={issue["number"]})
            self.stats["naive_chars"] += len(ISSUE_SYSTEM) + len(text) + sum(
                len(kb.issues[n]["title"]) + len(kb.issues[n]["body"]) for n, _ in pool)
            self.stats["naive_requests"] += 1
        ambiguous = len(bm) > 1 and bm[0][1] < self.gate_ratio * bm[1][1]
        if not ambiguous:
            self.stats["issue_gated"] += 1
            return base[:k]
        ids = {f"i{i + 1}": n for i, (n, _) in enumerate(bm)}
        cand = []
        for cid, n in ids.items():
            info = kb.issues[n]
            tags = ", ".join([kb.issue_state.get(n, "open")] + kb.labels(n)[:3])
            cand.append(f"{cid} [{tags}] {clip(info['title'], 100)} | {clip(info['body'], 180)}")
        user = f"NEW: {clip(issue['title'], 140)}\n{clip(issue['body'], 500)}\n\nCANDIDATES:\n" + "\n".join(cand)
        self.stats["requests"] += 1
        self.stats["issue_llm"] += 1
        self.stats["prompt_chars"] += len(ISSUE_SYSTEM) + len(user)
        self.stats["system_chars"] += len(ISSUE_SYSTEM)
        rows = _parse(view.llm(system=ISSUE_SYSTEM, user=user, schema=ISSUE_SCHEMA, effort=self.effort,
                               max_tokens=2000, tag="issues"), "matches")
        if rows is None:
            self.stats["fallback_requests"] += 1
            return base[:k]
        llm = {ids[str(r.get("id"))]: int(r.get("same_problem", 0)) for r in rows if str(r.get("id")) in ids}
        explicit = [f.target for f in base if f.score > (bm[0][1] if bm else 0)]   # boosted #refs stay on top
        pos = {n: i for i, (n, _) in enumerate(bm)}
        rest = sorted((n for n in pos if n not in explicit), key=lambda n: (-llm.get(n, -1), pos[n]))
        order = explicit + rest
        return [Flag(n, float(len(order) - i)) for i, n in enumerate(order[:k])]

    # ---- Output 8: predict the next code change ----
    def predict_change(self, view, task, k):
        """1. KB heuristic shortlists files (BM25 over the code at T). 2. Claude picks the files from that
        shortlist, seeing only paths, sizes and defined names (cheap). 3. Claude writes search/replace
        edits for its top picks, seeing only those files (or the parts around the names it chose).
        No LLM answer (dry run, budget, error) = the heuristic's ranking and no patch."""
        base = super().predict_change(view, task, max(k, self.change_pool))["files"]
        self.stats["change_tasks"] += 1
        if not base:
            return {"files": []}
        text = task_text(task, 2500)
        ids = {f"f{i + 1}": p for i, p in enumerate(base[:self.change_pool])}
        lines = []
        for cid, p in ids.items():
            body = view.code.read(p)
            lines.append(f"{cid} {p} ({body.count(chr(10)) + 1} lines): {', '.join(view.code.outline(p)) or '-'}")
        user = f"TASK ({task['kind']} #{task['number']}):\n{text}\n\nCANDIDATE FILES:\n" + "\n".join(lines)
        if self.measure_naive:   # naive: the task plus every shortlisted file in full, one big request
            self.stats["change_naive_chars"] += len(PATCH_SYSTEM) + len(text) + sum(len(view.code.read(p)) for p in ids.values())
        self.stats["requests"] += 1
        self.stats["change_prompt_chars"] += len(LOCALIZE_SYSTEM) + len(user)
        self.stats["system_chars"] += len(LOCALIZE_SYSTEM)
        rows = _parse(view.llm(system=LOCALIZE_SYSTEM, user=user, schema=LOCALIZE_SCHEMA, effort=self.effort,
                               max_tokens=2000, tag="localize"), "files")
        if rows is None:
            self.stats["fallback_requests"] += 1
            if self.write_patches:   # what the patch request would have cost, had Claude picked the heuristic's top files
                self.stats["patch_est_chars"] += len(PATCH_SYSTEM) + len(text) + sum(
                    len(excerpt(view.code.read(p), "", self.patch_lines)) + 12 for p in base[:self.patch_files])
            return {"files": base[:k]}
        picks = sorted((r for r in rows if str(r.get("id")) in ids), key=lambda r: -int(r.get("confidence", 0)))
        chosen = [ids[str(r["id"])] for r in picks]
        where = {ids[str(r["id"])]: str(r.get("where", "")) for r in picks}
        ranked = list(dict.fromkeys(chosen + base))[:k]
        to_edit = chosen[:self.patch_files]
        if not self.write_patches or not to_edit:
            return {"files": ranked, "why": "; ".join(f"{p}: {where[p]}" for p in to_edit)}
        blocks = [f"=== {p} ===\n{excerpt(view.code.read(p), where.get(p, ''), self.patch_lines)}" for p in to_edit]
        user2 = f"TASK ({task['kind']} #{task['number']}):\n{text}\n\nFILES:\n" + "\n\n".join(blocks)
        self.stats["requests"] += 1
        self.stats["change_prompt_chars"] += len(PATCH_SYSTEM) + len(user2)
        self.stats["system_chars"] += len(PATCH_SYSTEM)
        data = _parse(view.llm(system=PATCH_SYSTEM, user=user2, schema=PATCH_SCHEMA, effort=self.patch_effort,
                               max_tokens=8000, tag="patch"))
        if not data:
            self.stats["fallback_requests"] += 1
            return {"files": ranked}
        patch, ok, bad = apply_edits(view.code, data.get("edits"), set(to_edit))
        self.stats["edits_applied"] += ok
        self.stats["edits_failed"] += bad
        self.stats["patches"] += bool(patch)
        return {"files": ranked, "patch": patch, "why": clip(data.get("summary", ""), 160)}
