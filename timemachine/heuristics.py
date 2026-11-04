"""Knowledge-base predictors without an LLM. These are the strong baselines the LLM agent must beat.
Weights are hand-set (just-in-time defect prediction style: size, spread, bug-prone files, author experience)."""
import math
from .kb import KnowledgeBase
from .kb.links import refs
from .predictor import Flag, Predictor

L = math.log1p


class KBHeuristic(Predictor):
    name = "kb_heuristic"

    def __init__(self):
        self._cache = (None, None)

    def kb(self, view) -> KnowledgeBase:
        if self._cache[0] is not view:           # one KB per TimeView (== per T)
            self._cache = (view, KnowledgeBase(view))
        return self._cache[1]

    @staticmethod
    def risk_score(f: dict) -> float:
        if f["docs_only"] or f["n_code_files"] == 0:
            return -10.0
        return (1.0 * L(f["lines"]) + 0.5 * L(f["n_code_files"]) + 1.0 * L(f["file_fixes"])
                + 0.3 * L(f["file_commits"]) - 0.3 * L(f["author_prior_commits"]))

    def risky_commits(self, view, candidates):
        kb = self.kb(view)
        return [Flag(c["sha"], self.risk_score(kb.commit_features(c["sha"]))) for c in candidates]

    def missing_tests(self, view, candidates):
        kb, out = self.kb(view), []
        for c in candidates:
            f = kb.commit_features(c["sha"])
            if f["touches_tests"] or f["docs_only"] or f["n_code_files"] == 0:
                continue
            out.append(Flag(c["sha"], L(f["lines"]) + L(f["file_fixes"])))
        return out

    def similar_issues(self, view, issue, k):
        kb = self.kb(view)
        text = f"{issue['title']} {issue['body'] or ''}"
        hits = dict(kb.search_issues(text, k=k * 3, exclude={issue["number"]}))
        top = max(hits.values(), default=0.0)
        known = {i["number"] for i in view.issues(is_pr=False)}
        for n in refs(text) & known - {issue["number"]}:   # explicitly mentioned past issues rank first
            hits[n] = top + 1.0 + hits.get(n, 0.0)
        return [Flag(n, s) for n, s in sorted(hits.items(), key=lambda x: -x[1])[:k]]
