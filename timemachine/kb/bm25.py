"""Small dependency-free BM25 over short documents."""
import math
import re
from collections import Counter

STOP = frozenset("the a an and or of to in on for with is are was be it this that as at by from not no but if when "
                 "can cant does doesnt dont have has had will would should could you your we our they them there "
                 "what which how why about into than then also just use using used get got".split())


def tokenize(text: str) -> list[str]:
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text or "")          # camelCase -> camel Case
    toks = re.findall(r"[a-z][a-z0-9]{1,}|\d{2,}", text.lower())   # snake_case splits on "_" via the regex
    return [t for t in toks if t not in STOP]


class BM25:
    def __init__(self, docs: dict, k1=1.5, b=0.75):
        """docs: {doc_id: token list}"""
        self.k1, self.b = k1, b
        self.tf = {d: Counter(t) for d, t in docs.items()}
        self.len = {d: len(t) for d, t in docs.items()}
        self.avg = (sum(self.len.values()) / len(docs)) if docs else 0.0
        df = Counter(w for c in self.tf.values() for w in c)
        n = len(docs)
        self.idf = {w: math.log(1 + (n - f + 0.5) / (f + 0.5)) for w, f in df.items()}
        self.inv = {}
        for d, c in self.tf.items():
            for w in c:
                self.inv.setdefault(w, []).append(d)

    def search(self, query_tokens: list[str], k: int, exclude=()) -> list[tuple]:
        scores = Counter()
        for w in set(query_tokens):
            idf = self.idf.get(w)
            if idf is None:
                continue
            for d in self.inv[w]:
                tf = self.tf[d][w]
                norm = 1 - self.b + self.b * self.len[d] / (self.avg or 1)
                scores[d] += idf * tf * (self.k1 + 1) / (tf + self.k1 * norm)
        for d in exclude:
            scores.pop(d, None)
        return sorted(scores.items(), key=lambda x: (-x[1], str(x[0])))[:k]
