"""KnowledgeBase: deterministic indices over a repo's history, updated incrementally.

The KB's state is a pure function of the events applied so far (kb/events.py). In the sandbox the
worker applies each time-machine step's NEW events only, so a step costs O(new data) instead of a
rebuild. KnowledgeBase(view) replays a TimeView's rows through the same code path, and
tests/test_incremental.py checks that both give the same KB. No LLM, no network, no repo access.
"""
import json
from collections import defaultdict
from .bm25 import BM25, tokenize
from .events import events_from_db
from .links import refs, closes, is_fix_message

FILE_COLS = ("path", "added", "deleted", "is_test", "is_doc")


class KnowledgeBase:
    def __init__(self, view=None):
        self.as_of = None
        self.n_events = 0
        self._commits = {}                                   # sha -> {sha, ts, author, message}
        self._files = {}                                     # sha -> [{path, added, deleted, is_test, is_doc}]
        self._stats = {}                                     # path -> running file history
        self._author_commits = defaultdict(int)
        self.commit_refs = {}                                # sha -> {#N mentioned in message}
        self.issue_commits = defaultdict(set)                # issue number -> commits mentioning it
        self.issue_refs = defaultdict(set)                   # issue number -> numbers its text mentions
        self.issues = {}                                     # number -> {title, body, ts, is_pr}
        self.issue_state = {}                                # number -> open | closed
        self.fix_shas = set()
        self._msg_fix, self._commit_closes = set(), {}
        self._closers = defaultdict(set)                     # issue number -> commits that say "fixes #N"
        self._labels = defaultdict(set)
        self._bug_issues = set()
        self._bm25 = BM25()
        if view is not None:
            self.apply(events_from_db(view.db), as_of=view.as_of)

    # ---- incremental update ----
    def apply(self, events, as_of=None):
        for e in events:
            getattr(self, "_on_" + e["kind"])(e)
            self.n_events += 1
        if as_of is not None:
            self.as_of = as_of
        return self

    def _on_commit(self, e):
        sha, ts, _author_ts, author, message, _parents = e["row"]
        if sha in self._commits:
            return
        self._commits[sha] = {"sha": sha, "ts": ts, "author": author, "message": message}
        self._author_commits[author] += 1
        fs = self._files[sha] = [dict(zip(FILE_COLS, f)) for f in e["files"]]
        for f in fs:
            s = self._stats.get(f["path"])
            if s is None:
                s = self._stats[f["path"]] = {"commits": 0, "fixes": 0, "lines": 0, "last_ts": 0,
                                              "authors": set(), "fix_shas": set()}
            s["commits"] += 1
            s["lines"] += (f["added"] or 0) + (f["deleted"] or 0)
            s["last_ts"] = max(s["last_ts"], ts)
            s["authors"].add(author)
        self.commit_refs[sha] = refs(message)
        for n in self.commit_refs[sha]:
            self.issue_commits[n].add(sha)
        self._commit_closes[sha] = closes(message)
        for n in self._commit_closes[sha]:
            self._closers[n].add(sha)
        if is_fix_message(message):
            self._msg_fix.add(sha)
        self._refresh_fix(sha)

    def _on_issue(self, e):
        number, is_pr, ts, _author, title, body = e["row"]
        self.issues[number] = {"title": title, "body": body or "", "ts": ts, "is_pr": bool(is_pr)}
        self.issue_state.setdefault(number, "open")
        self.issue_refs[number] |= refs(title) | refs(body)
        if not is_pr:
            # title counted twice: it is the densest, most reliable part of a report
            self._bm25.add(number, tokenize(f"{title} {title} {body or ''}"))

    def _on_issue_event(self, e):
        number, _ts, etype, _actor, data = e["row"]
        if etype in ("closed", "merged"):
            self.issue_state[number] = "closed"
        elif etype == "reopened":
            self.issue_state[number] = "open"
        elif etype in ("labeled", "unlabeled"):
            label = json.loads(data or "{}").get("label")
            if label is None:
                return
            (self._labels[number].add if etype == "labeled" else self._labels[number].discard)(label)
            info = self.issues.get(number)
            bug = bool(info) and not info["is_pr"] and any("bug" in l.lower() for l in self._labels[number])
            if bug != (number in self._bug_issues):
                (self._bug_issues.add if bug else self._bug_issues.discard)(number)
                for sha in self._closers.get(number, ()):   # a commit that closed it may now be (or stop being) a fix
                    self._refresh_fix(sha)

    def _on_comment(self, e):
        _id, number, _ts, _author, body, _kind = e["row"]
        self.issue_refs[number] |= refs(body)

    def _refresh_fix(self, sha):
        """A commit is a known fix if its subject says so, or it explicitly closes an issue labeled bug."""
        want = sha in self._msg_fix or bool(self._commit_closes.get(sha, set()) & self._bug_issues)
        if want == (sha in self.fix_shas):
            return
        d = 1 if want else -1
        (self.fix_shas.add if want else self.fix_shas.discard)(sha)
        for f in self._files.get(sha, ()):
            s = self._stats[f["path"]]
            s["fixes"] += d
            (s["fix_shas"].add if want else s["fix_shas"].discard)(sha)

    # ---- queries ----
    def labels(self, number) -> list[str]:
        return sorted(self._labels.get(number, ()))

    def file_stats(self, path, exclude_sha=None) -> dict:
        """History of `path` visible at T. `exclude_sha` removes one commit's own contribution
        (so a candidate's own fix/churn does not count as its own history)."""
        s = self._stats.get(path)
        out = {"commits": 0, "fixes": 0, "lines": 0, "last_ts": 0, "n_authors": 0}
        if not s:
            return out
        out.update(commits=s["commits"], fixes=s["fixes"], lines=s["lines"], last_ts=s["last_ts"], n_authors=len(s["authors"]))
        own = next((f for f in self._files.get(exclude_sha, ()) if f["path"] == path), None)
        if own is not None:
            out["commits"] -= 1
            out["lines"] -= (own["added"] or 0) + (own["deleted"] or 0)
            if exclude_sha in self.fix_shas:
                out["fixes"] -= 1
        return out

    def author_commits(self, author) -> int:
        return self._author_commits.get(author, 0)

    def is_fix(self, sha) -> bool:
        return sha in self.fix_shas

    def commit(self, sha) -> dict | None:
        return self._commits.get(sha)

    def commit_files(self, sha) -> list[dict]:
        return self._files.get(sha, [])

    def commit_features(self, sha) -> dict:
        """JIT defect prediction style features for one commit."""
        c = self._commits[sha]
        fs = self._files.get(sha, [])
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

    def search_issues(self, text: str, k: int = 5, exclude=()) -> list[tuple[int, float]]:
        """BM25 over issue title+body visible at T. Returns [(issue number, score)]."""
        return self._bm25.search(tokenize(text), k, exclude)

    def summary(self) -> dict:
        return {"as_of": self.as_of, "events": self.n_events, "commits": len(self._commits),
                "files": len(self._stats), "issues": sum(1 for i in self.issues.values() if not i["is_pr"]),
                "prs": sum(1 for i in self.issues.values() if i["is_pr"]), "fixes": len(self.fix_shas),
                "bug_issues": len(self._bug_issues)}
