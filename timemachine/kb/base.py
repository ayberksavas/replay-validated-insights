"""KnowledgeBase v1: deterministic indices built from ONE TimeView. No LLM, no network, no repo access
beyond what TimeView allows. Everything here is a function of rows with ts <= view.as_of."""
import math
from collections import defaultdict
from .bm25 import BM25, tokenize
from .links import refs, closes, is_fix_message


class KnowledgeBase:
    def __init__(self, view):
        self.view = view
        self.as_of = view.as_of
        self._commits = {c["sha"]: c for c in view.commits()}
        self._files = defaultdict(list)                      # sha -> [row]
        for r in view.db.execute("SELECT * FROM commit_files"):
            self._files[r["sha"]].append(r)
        self._build_links()
        self._build_file_stats()
        self._bm25 = None

    # ---- links: commit <-> issue/PR ----
    def _build_links(self):
        bug_issues = {i["number"] for i in self.view.issues(is_pr=False)
                      if "bug" in " ".join(self.view.issue_state(i["number"])["labels"]).lower()}
        self.commit_refs = {s: refs(c["message"]) for s, c in self._commits.items()}
        self.issue_commits = defaultdict(set)                # issue number -> commits mentioning it
        for s, rs in self.commit_refs.items():
            for n in rs:
                self.issue_commits[n].add(s)
        # a commit is a known fix if its subject says so, or it explicitly closes an issue labeled bug
        self.fix_shas = {s for s, c in self._commits.items()
                         if is_fix_message(c["message"]) or (closes(c["message"]) & bug_issues)}
        # issue-side mentions (body + comments): which issues point at which
        self.issue_refs = defaultdict(set)
        for i in self.view.issues():
            self.issue_refs[i["number"]] |= refs(i["title"]) | refs(i["body"])
        for c in self.view.db.execute("SELECT number, body FROM comments"):
            self.issue_refs[c["number"]] |= refs(c["body"])

    # ---- per-file history ----
    def _build_file_stats(self):
        st = defaultdict(lambda: {"commits": 0, "fixes": 0, "lines": 0, "last_ts": 0, "authors": set(), "fix_shas": []})
        for sha, fs in self._files.items():
            c = self._commits[sha]
            for f in fs:
                s = st[f["path"]]
                s["commits"] += 1
                s["lines"] += (f["added"] or 0) + (f["deleted"] or 0)
                s["last_ts"] = max(s["last_ts"], c["ts"])
                s["authors"].add(c["author"])
                if sha in self.fix_shas:
                    s["fixes"] += 1
                    s["fix_shas"].append(sha)
        self._stats = dict(st)
        self._author_commits = defaultdict(int)
        for c in self._commits.values():
            self._author_commits[c["author"]] += 1

    def file_stats(self, path, exclude_sha=None) -> dict:
        """History of `path` visible at T. `exclude_sha` removes one commit's own contribution
        (so a candidate's own fix/churn does not count as its own history)."""
        s = self._stats.get(path)
        out = {"commits": 0, "fixes": 0, "lines": 0, "last_ts": 0, "n_authors": 0}
        if not s:
            return out
        out.update(commits=s["commits"], fixes=s["fixes"], lines=s["lines"], last_ts=s["last_ts"], n_authors=len(s["authors"]))
        if exclude_sha in self._commits and any(f["path"] == path for f in self._files[exclude_sha]):
            f = next(f for f in self._files[exclude_sha] if f["path"] == path)
            out["commits"] -= 1
            out["lines"] -= (f["added"] or 0) + (f["deleted"] or 0)
            if exclude_sha in self.fix_shas:
                out["fixes"] -= 1
        return out

    def author_commits(self, author) -> int:
        return self._author_commits.get(author, 0)

    def is_fix(self, sha) -> bool:
        return sha in self.fix_shas

    # ---- commit features (JIT defect prediction style) ----
    def commit_features(self, sha) -> dict:
        c = self._commits[sha]
        fs = self._files[sha]
        code = [f for f in fs if not f["is_test"] and not f["is_doc"]]
        hist = [self.file_stats(f["path"], exclude_sha=sha) for f in code]
        return {
            "n_files": len(fs),
            "n_code_files": len(code),
            "lines": sum((f["added"] or 0) + (f["deleted"] or 0) for f in code),
            "touches_tests": any(f["is_test"] for f in fs),
            "docs_only": bool(fs) and all(f["is_doc"] for f in fs),
            "is_fix": sha in self.fix_shas,
            "file_commits": sum(h["commits"] for h in hist),
            "file_fixes": sum(h["fixes"] for h in hist),
            "author_prior_commits": max(0, self.author_commits(c["author"]) - 1),
            "refs": sorted(self.commit_refs.get(sha, ())),
        }

    # ---- text search over issues ----
    def _index(self):
        if self._bm25 is None:
            docs = {}
            for i in self.view.issues(is_pr=False):
                # title counted twice: it is the densest, most reliable part of a report
                docs[i["number"]] = tokenize(f"{i['title']} {i['title']} {i['body'] or ''}")
            self._bm25 = BM25(docs)
        return self._bm25

    def search_issues(self, text: str, k: int = 5, exclude=()) -> list[tuple[int, float]]:
        """BM25 over issue title+body visible at T. Returns [(issue number, score)]."""
        return self._index().search(tokenize(text), k, exclude)
