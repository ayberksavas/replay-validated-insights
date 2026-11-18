"""The code as of T, seen from inside the sandbox.

The host keeps `<sandbox>/code` checked out at the last main-line commit at or before T (an
IncrementalSnapshot: no objects after T). CodeIndex lists its files and keeps a BM25 index over
path + content, refreshed incrementally: only files whose size or modification time changed since
the previous step are re-read (a checkout rewrites only the files a commit touched)."""
import os
import re
from .bm25 import BM25, tokenize

# same rules as ingest_git (duplicated on purpose: host modules are not copied into the sandbox)
TEST_RE = re.compile(r"(^|/)(tests?|__tests__|spec)(/|$)|(^|/)test_[^/]*$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$", re.I)
DOC_RE = re.compile(r"(^|/)(docs?|documentation)(/|$)|\.(md|rst|adoc|txt)$|(^|/)readme", re.I)
CODE_EXT = frozenset("py pyi js jsx ts tsx go rs java kt scala rb php c h cc cpp hpp cs swift m sh bash ps1 sql "
                     "toml yaml yml cfg ini json".split())
MAX_BYTES = 300_000
DEF_RE = re.compile(r"^\s*(?:async\s+def|def|class|func|function|fn|public|private|protected)\s+([A-Za-z_][\w]*)", re.M)


def is_source(path: str) -> bool:
    return not TEST_RE.search(path) and not DOC_RE.search(path)


class CodeIndex:
    def __init__(self, root: str):
        self.root = root
        self._sig = {}          # path -> (size, mtime_ns)
        self._bm25 = BM25()
        self.reindexed = 0      # files (re)read so far: shows the incremental refresh at work

    def refresh(self) -> int:
        """Bring the index in line with the files on disk. Returns how many files were re-read."""
        if not os.path.isdir(self.root):
            return 0
        seen, changed = set(), 0
        for d, dirs, files in os.walk(self.root):
            dirs[:] = [x for x in dirs if x != ".git"]
            for f in files:
                full = os.path.join(d, f)
                rel = os.path.relpath(full, self.root).replace(os.sep, "/")
                if rel.rsplit(".", 1)[-1].lower() not in CODE_EXT:
                    continue
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                if st.st_size > MAX_BYTES:
                    continue
                seen.add(rel)
                sig = (st.st_size, st.st_mtime_ns)
                if self._sig.get(rel) == sig:
                    continue
                self._bm25.remove(rel)
                path_toks = tokenize(rel.replace("/", " ").replace("_", " ").replace(".", " "))
                self._bm25.add(rel, path_toks * 3 + tokenize(self.read(rel)))
                self._sig[rel] = sig
                changed += 1
        for gone in set(self._sig) - seen:
            self._bm25.remove(gone)
            del self._sig[gone]
        self.reindexed += changed
        return changed

    def files(self, source_only=True) -> list[str]:
        return sorted(p for p in self._sig if not source_only or is_source(p))

    def exists(self, path) -> bool:
        return path in self._sig

    def read(self, path: str, max_chars: int | None = None) -> str:
        with open(os.path.join(self.root, *path.split("/")), encoding="utf-8", errors="replace") as f:
            text = f.read()
        return text if max_chars is None else text[:max_chars]

    def outline(self, path: str, limit=12) -> list[str]:
        """Top names defined in a file (functions, classes): a cheap summary for a prompt."""
        names = list(dict.fromkeys(DEF_RE.findall(self.read(path))))
        return names[:limit] + ([f"... {len(names) - limit} more"] if len(names) > limit else [])

    def search(self, text: str, k: int = 10, source_only=True) -> list[tuple[str, float]]:
        hits = self._bm25.search(tokenize(text), k * 4 if source_only else k)
        return [(p, s) for p, s in hits if not source_only or is_source(p)][:k]
