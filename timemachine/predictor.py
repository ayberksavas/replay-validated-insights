"""Interface the system under test implements + trivial baselines (random, most-changed-files)."""
import random
import re
from dataclasses import dataclass


@dataclass
class Flag:
    target: str | int   # commit sha (outputs 1,2) or issue number (output 3)
    score: float
    why: str = ""         # optional one-line rationale (LLM predictors)


class Predictor:
    name = "base"

    def risky_commits(self, view, candidates) -> list[Flag]: return []
    def missing_tests(self, view, candidates) -> list[Flag]: return []
    def similar_issues(self, view, issue, k) -> list[Flag]: return []
    # Output 8 (next code change): task = {kind: issue|pr, number, title, body, comments}, all visible at T.
    # Return {"files": ranked code paths the change will touch, "patch": unified diff or None, "why": str}.
    # The code as of T is in view.code (kb/code.py: files(), read(), outline(), search()).
    def predict_change(self, view, task, k) -> dict: return {"files": []}


def task_text(task: dict, max_chars: int = 4000) -> str:
    """The words of a change task: title, description, then the discussion so far (all written before T)."""
    parts = [task.get("title") or "", task.get("body") or ""] + [c for c in task.get("comments") or [] if c]
    text = "\n\n".join(p.strip() for p in parts if p and p.strip())
    return text[:max_chars]


class RandomBaseline(Predictor):
    name = "random"

    def __init__(self, seed=0): self.r = random.Random(seed)
    def risky_commits(self, view, candidates): return [Flag(c["sha"], self.r.random()) for c in candidates]
    def missing_tests(self, view, candidates):
        return [Flag(c["sha"], self.r.random()) for c in candidates if not any(f["is_test"] for f in view.files(c["sha"]))]
    def similar_issues(self, view, issue, k):
        pool = [i["number"] for i in view.issues(is_pr=False) if i["number"] != issue["number"]]
        return [Flag(n, self.r.random()) for n in self.r.sample(pool, min(k, len(pool)))]
    def predict_change(self, view, task, k):
        files = view.code.files()
        return {"files": self.r.sample(files, min(k, len(files)))}


class MostChangedFiles(Predictor):
    """Score a commit by how historically churny the files it touches are."""
    name = "most_changed_files"

    def _churn(self, view):
        return {r["path"]: r["n"] for r in view.db.execute("SELECT path,COUNT(*) n FROM commit_files GROUP BY path")}

    def risky_commits(self, view, candidates):
        ch = self._churn(view)
        return [Flag(c["sha"], sum(ch.get(f["path"], 0) for f in view.files(c["sha"]))) for c in candidates]

    def missing_tests(self, view, candidates):
        ch = self._churn(view)
        out = []
        for c in candidates:
            fs = view.files(c["sha"])
            if not any(f["is_test"] for f in fs):
                out.append(Flag(c["sha"], sum(ch.get(f["path"], 0) for f in fs)))
        return out

    def similar_issues(self, view, issue, k):  # token-overlap stand-in
        tok = lambda s: set(re.findall(r"[a-z]{3,}", (s or "").lower()))
        q = tok(issue["title"] + " " + (issue["body"] or ""))
        sc = [(len(q & tok(i["title"])) / (1 + len(q)), i["number"]) for i in view.issues(is_pr=False) if i["number"] != issue["number"]]
        return [Flag(n, s) for s, n in sorted(sc, reverse=True)[:k]]

    def predict_change(self, view, task, k):   # the files changed most often so far, whatever the task says
        ch = self._churn(view)
        return {"files": sorted(view.code.files(), key=lambda p: (-ch.get(p, 0), p))[:k]}
