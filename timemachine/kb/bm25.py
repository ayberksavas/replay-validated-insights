"""Small dependency-free BM25 over short documents. Incremental: add() a document at any time;
idf and average length are computed at query time from running counts, so adding N documents one
by one gives exactly the same scores as building the index from all N at once."""
import math
import re
from collections import Counter, defaultdict

STOP = frozenset("the a an and or of to in on for with is are was be it this that as at by from not no but if when "
                 "can cant does doesnt dont have has had will would should could you your we our they them there "
                 "what which how why about into than then also just use using used get got".split())


def tokenize(text: str) -> list[str]:
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text or "")          # camelCase -> camel Case
    toks = re.findall(r"[a-z][a-z0-9]{1,}|\d{2,}", text.lower())   # snake_case splits on "_" via the regex
    return [t for t in toks if t not in STOP]


class BM25:
    def __init__(self, docs: dict | None = None, k1=1.5, b=0.75):
        """docs: optional {doc_id: token list} to start with."""
        self.k1, self.b = k1, b
        self.tf, self.len = {}, {}
        self.df = Counter()
        self.inv = defaultdict(list)
        self.total_len = 0
        for d, toks in (docs or {}).items():
            self.add(d, toks)

    def __len__(self):
        return len(self.tf)

    def add(self, doc_id, tokens: list[str]):
        if doc_id in self.tf:
            return
        c = Counter(tokens)
        self.tf[doc_id], self.len[doc_id] = c, len(tokens)
        self.total_len += len(tokens)
        for w in c:
            self.df[w] += 1
            self.inv[w].append(doc_id)

    def search(self, query_tokens: list[str], k: int, exclude=()) -> list[tuple]:
        n = len(self.tf)
        avg = self.total_len / n if n else 0.0
        scores = Counter()
        for w in sorted(set(query_tokens)):     # fixed order: identical float sums in every process
            f = self.df.get(w)
            if not f:
                continue
            idf = math.log(1 + (n - f + 0.5) / (f + 0.5))
            for d in self.inv[w]:
                tf = self.tf[d][w]
                norm = 1 - self.b + self.b * self.len[d] / (avg or 1)
                scores[d] += idf * tf * (self.k1 + 1) / (tf + self.k1 * norm)
        for d in exclude:
            scores.pop(d, None)
        return sorted(scores.items(), key=lambda x: (-x[1], str(x[0])))[:k]
