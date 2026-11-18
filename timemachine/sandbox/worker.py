"""Runs INSIDE the sandbox process (started by sandbox/host.py with `python -I`, cwd = sandbox dir).

What it has: its own store (sandbox/store.db) filled only with events the host released (all <= T),
an incremental KnowledgeBase, the predictor, and a JSON-lines channel to the host on stdin/stdout.
What it does not have: the full DB, the clone, the oracle code, an API key, network, subprocesses.
diff()/file_at() and LLM calls are requests to the host, which checks T and meters tokens.

Protocol (one JSON object per line):
  host -> worker  {"op": ...}                       worker -> host  {"result": ...} | {"error": ...}
  worker -> host  {"rpc": "diff"|"file_at"|"llm"}   host -> worker  {"result": ...} | {"error": ...}
"""
import json
import os
import sqlite3
import sys

from ..store import SCHEMA
from ..kb import KnowledgeBase
from ..kb.code import CodeIndex
from ..kb.events import COMMIT, ISSUE, ISSUE_EVENT, COMMENT
from ..predictor import Flag, RandomBaseline, MostChangedFiles
from ..heuristics import KBHeuristic
from ..insight import ClaudeInsight
from .jail import install_jail

PREDICTORS = {p.name: p for p in (RandomBaseline, MostChangedFiles, KBHeuristic, ClaudeInsight)}
READ_ONLY = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}


class LeakageError(Exception):
    pass


class HostError(Exception):
    pass


class Channel:
    def __init__(self, inp, out):
        self.inp, self.out = inp, out

    def send(self, msg):
        self.out.write((json.dumps(msg) + "\n").encode("utf-8"))
        self.out.flush()

    def recv(self):
        line = self.inp.readline()
        if not line:
            raise EOFError("host closed the channel")
        return json.loads(line)

    def rpc(self, op, **kw):
        self.send({"rpc": op, **kw})
        r = self.recv()
        if "error" in r:
            err = r["error"]
            raise (LeakageError if err.startswith("LeakageError") else HostError)(err)
        return r["result"]


class SandboxView:
    """Same read API as TimeView, backed by the sandbox's own store. The predictor's connection is
    strictly read-only (SELECT only: no ATTACH, no PRAGMA, no writes)."""

    def __init__(self, path, channel, kb):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.set_authorizer(lambda action, *_: sqlite3.SQLITE_OK if action in READ_ONLY else sqlite3.SQLITE_DENY)
        self.kb, self._ch, self.as_of = kb, channel, None

    def commits(self, since=None):
        return self.db.execute("SELECT * FROM commits WHERE ts>=? ORDER BY ts, sha", (since or 0,)).fetchall()

    def commit(self, sha):
        return self.db.execute("SELECT * FROM commits WHERE sha=?", (sha,)).fetchone()

    def files(self, sha):
        return self.db.execute("SELECT * FROM commit_files WHERE sha=?", (sha,)).fetchall()

    def issues(self, since=None, is_pr=None):
        q, p = "SELECT * FROM issues WHERE ts>=?", [since or 0]
        if is_pr is not None:
            q += " AND is_pr=?"; p.append(int(is_pr))
        return self.db.execute(q + " ORDER BY ts, number", p).fetchall()

    def issue_state(self, number):
        state, labels = "open", set()
        for e in self.db.execute("SELECT * FROM issue_events WHERE number=? ORDER BY ts, rowid", (number,)):
            d = json.loads(e["data"])
            if e["type"] in ("closed", "merged"):
                state = "closed"
            elif e["type"] == "reopened":
                state = "open"
            elif e["type"] == "labeled" and d.get("label") is not None:
                labels.add(d["label"])
            elif e["type"] == "unlabeled":
                labels.discard(d.get("label"))
        return {"state": state, "labels": sorted(labels)}

    def comments(self, number):
        return self.db.execute("SELECT * FROM comments WHERE number=? ORDER BY ts, id", (number,)).fetchall()

    # ---- served by the host, which refuses commits after T ----
    def diff(self, sha):
        return self._ch.rpc("diff", sha=sha)

    def file_at(self, sha, path):
        return self._ch.rpc("file_at", sha=sha, path=path)

    def llm(self, **request):
        """Ask the host's LLM gateway (cache, token meter, budget). Returns {"text": str|None, ...}."""
        return self._ch.rpc("llm", request=request)


class Worker:
    def __init__(self, root, channel, predictor, options):
        self.root, self.ch = root, channel
        self.store_path = os.path.join(root, "store.db")
        self.store = sqlite3.connect(self.store_path)
        self.store.executescript(SCHEMA)
        self.kb = KnowledgeBase()
        self.view = SandboxView(self.store_path, channel, self.kb)
        self.view.code = CodeIndex(os.path.join(root, "code"))   # the code as of T, kept there by the host
        self.predictor = PREDICTORS[predictor](**(options or {}))

    def advance(self, as_of, events):
        if self.view.as_of is not None and as_of < self.view.as_of:
            raise ValueError("time only moves forward")
        late = [e for e in events if e["vts"] > as_of]
        if late:   # the host must never send the future; check anyway
            raise LeakageError(f"{len(late)} events after T={as_of} were sent to the sandbox")
        st = self.store
        for e in events:
            k, r = e["kind"], e["row"]
            if k == COMMIT:
                st.execute("INSERT OR REPLACE INTO commits VALUES(?,?,?,?,?,?)", r)
                st.executemany("INSERT INTO commit_files VALUES(?,?,?,?,?,?)", [[r[0], *f] for f in e["files"]])
            elif k == ISSUE:
                st.execute("INSERT OR REPLACE INTO issues VALUES(?,?,?,?,?,?)", r)
            elif k == ISSUE_EVENT:
                st.execute("INSERT INTO issue_events VALUES(?,?,?,?,?)", r)
            elif k == COMMENT:
                st.execute("INSERT OR REPLACE INTO comments VALUES(?,?,?,?,?,?)", r)
        st.commit()
        self.kb.apply(events, as_of=as_of)
        self.view.as_of = as_of
        return {"as_of": as_of, "new_events": len(events), "kb": self.kb.summary()}

    def change_task(self, ref):
        """The host names a task only by kind + number; its words come from the sandbox's own store,
        so the predictor can only read what was written before T."""
        row = self.view.db.execute("SELECT * FROM issues WHERE number=?", (ref["number"],)).fetchone()
        if row is None or bool(row["is_pr"]) != (ref["kind"] == "pr"):
            raise LeakageError(f"task {ref['kind']} #{ref['number']} is not visible in the sandbox")
        return {"kind": ref["kind"], "number": row["number"], "title": row["title"], "body": row["body"] or "",
                "comments": [c["body"] for c in self.view.comments(row["number"])]}

    def _rows(self, shas):
        rows = []
        for s in shas:
            r = self.view.commit(s)
            if r is None:
                raise LeakageError(f"host asked about commit {s[:12]}, which is not visible in the sandbox")
            rows.append(r)
        return rows

    @staticmethod
    def _out(flags):
        return [[f.target, f.score, getattr(f, "why", "")] for f in flags]

    def handle(self, msg):
        op = msg["op"]
        if op == "advance":
            return self.advance(msg["as_of"], msg["events"])
        if op == "risky":
            return self._out(self.predictor.risky_commits(self.view, self._rows(msg["candidates"])))
        if op == "tests":
            return self._out(self.predictor.missing_tests(self.view, self._rows(msg["candidates"])))
        if op == "similar":
            return self._out(self.predictor.similar_issues(self.view, msg["issue"], msg["k"]))
        if op == "change":
            k = msg["k"]
            self.view.code.refresh()
            out = self.predictor.predict_change(self.view, self.change_task(msg["task"]), k) or {}
            return {"files": list(out.get("files") or [])[:k], "patch": out.get("patch"), "why": out.get("why", ""),
                    "preview": (out.get("preview") or "")[:2000] or None}
        if op == "stats":
            return {"predictor": dict(getattr(self.predictor, "stats", {}) or {}), "kb": self.kb.summary(),
                    "code_files_reindexed": self.view.code.reindexed}
        if op == "probe":
            return self.probe(msg)
        raise ValueError(f"unknown op {op!r}")

    def probe(self, msg):
        """Try to cheat on purpose and report what happened (for tests and the demo)."""
        import socket, subprocess
        tries = {
            "read the full database": lambda: open(msg["db_path"], "rb").read(16),
            "list the data directory": lambda: os.listdir(msg["data_dir"]),
            "open the full database with sqlite": lambda: sqlite3.connect(msg["db_path"]).execute("SELECT 1"),
            "ATTACH the full database": lambda: self.view.db.execute(f"ATTACH DATABASE '{msg['db_path']}' AS full"),
            "run git on the clone": lambda: subprocess.run(["git", "-C", msg["repo"], "log", "--all", "-1"]),
            "connect to github.com": lambda: socket.create_connection(("github.com", 443), timeout=3),
            "diff of a commit after T": lambda: self.view.diff(msg["future_sha"]) if msg.get("future_sha") else None,
        }
        out = {}
        for label, fn in tries.items():
            try:
                fn()
                out[label] = "ALLOWED"
            except Exception as e:  # noqa: BLE001 - we report every failure mode
                out[label] = f"blocked ({type(e).__name__})"
        return out


def main():
    root = os.path.abspath(os.getcwd())
    inp, out = sys.stdin.buffer, sys.stdout.buffer
    sys.stdout = sys.stderr                     # stray prints from predictors must not corrupt the protocol
    install_jail(root)
    ch = Channel(inp, out)
    init = ch.recv()
    try:
        w = Worker(root, ch, init["predictor"], init.get("options"))
        ch.send({"result": {"ready": True, "pid": os.getpid(), "predictor": w.predictor.name}})
    except Exception as e:  # noqa: BLE001
        ch.send({"error": f"{type(e).__name__}: {e}"})
        return
    while True:
        try:
            msg = ch.recv()
        except EOFError:
            return
        if msg.get("op") == "shutdown":
            ch.send({"result": "bye"})
            return
        try:
            ch.send({"result": w.handle(msg)})
        except Exception as e:  # noqa: BLE001 - errors go back to the host, the worker keeps running
            ch.send({"error": f"{type(e).__name__}: {e}"})
