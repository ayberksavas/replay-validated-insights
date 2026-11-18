"""Output 8, next code change: task mining, scoring, sandbox isolation, and the Claude patch path (fake client)."""
import json
import os
import re
import subprocess
import tempfile
from types import SimpleNamespace

import pytest
from conftest import ts
from timemachine.ingest_git import ingest_git
from timemachine.llm import LLMGateway
from timemachine.nextchange import mine_tasks, changed_lines, score, replay_next_change, safe_start
from timemachine.gitutil import run_git
from timemachine.sandbox.host import SandboxHost, SandboxError
from timemachine.store import connect

PARSER = 'def parse(text):\n    """Return the first field of a comma separated line."""\n    return text.split(",")[0]\n\n\ndef count(text):\n    return len(text.split(","))\n'
CLI = 'import argparse\n\n\ndef build_parser():\n    p = argparse.ArgumentParser(prog="app")\n    p.add_argument("path")\n    return p\n'
UTIL = 'def slugify(name):\n    return name.lower().replace(" ", "-")\n'
FIX_FIND, FIX_REPLACE = '    return text.split(",")[0]', '    if not text:\n        return ""\n    return text.split(",")[0]'
FLAG_FIND, FLAG_REPLACE = '    p.add_argument("path")', '    p.add_argument("path")\n    p.add_argument("--verbose", action="store_true")'
START, END = ts("2022-01-01T00:00:00Z"), ts("2022-12-31T00:00:00Z")


@pytest.fixture(scope="module")
def app():
    d = tempfile.mkdtemp()

    def git(*a, date="2022-01-01T00:00:00Z"):
        env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        subprocess.run(["git", "-C", d, *a], check=True, env=env, capture_output=True)

    def commit(msg, date, files):
        for p, body in files.items():
            os.makedirs(os.path.dirname(f"{d}/{p}"), exist_ok=True)
            with open(f"{d}/{p}", "w", newline="\n") as f:
                f.write(body)
        git("add", "."); git("commit", "-qm", msg, date=date)

    git("init", "-q", "-b", "main"); git("config", "user.email", "a@b.c"); git("config", "user.name", "a")
    git("config", "core.autocrlf", "false")
    commit("Initial import", "2022-01-05T10:00:00Z", {"src/app/parser.py": PARSER, "src/app/cli.py": CLI,
                                                      "src/app/util.py": UTIL, "README.md": "app\n",
                                                      "tests/test_parser.py": "def test_parse():\n    pass\n"})
    commit("Refactor util", "2022-02-01T10:00:00Z", {"src/app/util.py": UTIL + "\n\ndef shout(x):\n    return x.upper()\n"})
    commit("Handle empty input in parser, fixes #1", "2022-03-10T10:00:00Z",
           {"src/app/parser.py": PARSER.replace(FIX_FIND, FIX_REPLACE)})
    commit("Add --verbose flag (#3)", "2022-04-10T10:00:00Z", {"src/app/cli.py": CLI.replace(FLAG_FIND, FLAG_REPLACE)})
    commit("Tidy docs", "2022-05-01T10:00:00Z", {"README.md": "app, a tiny tool\n"})
    db = os.path.join(d, "..", f"app{os.path.basename(d)}.db")
    ingest_git(d, db)
    c = connect(db)
    for n, is_pr, date, title, body in (
            (1, 0, "2022-03-01T00:00:00Z", "Parser crashes on empty input", "parse('') should return an empty string."),
            (2, 0, "2022-01-20T00:00:00Z", "Slugify drops unicode", "slugify loses accents"),
            (3, 1, "2022-04-05T00:00:00Z", "Add a --verbose flag to the command line", "Adds a verbose option to the CLI argument parser.")):
        c.execute("INSERT INTO issues VALUES(?,?,?,?,?,?)", (n, is_pr, ts(date), "u", title, body))
    c.execute("INSERT INTO comments VALUES(1,1,?,?,?,?)", (ts("2022-03-02T00:00:00Z"), "m", "Confirmed, parse() fails on ''.", "issue"))
    c.commit(); c.close()
    return db, d


class FakeClaude:
    """Picks the first shortlisted file; writes the known fix for the file it is shown."""

    def __init__(self):
        self.calls = []
        self.beta = SimpleNamespace(messages=self)
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        props = kw["output_config"]["format"]["schema"]["properties"]
        user = kw["messages"][0]["content"]
        if "files" in props:
            first = re.search(r"^(f\d+) ", user, re.M).group(1)
            out = {"files": [{"id": first, "confidence": 90, "where": "parse, build_parser"}]}
        else:
            path = re.search(r"^=== (.+) ===$", user, re.M).group(1)
            find, rep = (FIX_FIND, FIX_REPLACE) if path.endswith("parser.py") else (FLAG_FIND, FLAG_REPLACE)
            out = {"edits": [{"file": path, "find": find, "replace": rep}], "summary": f"change {path}"}
        usage = SimpleNamespace(input_tokens=300, output_tokens=80, cache_creation_input_tokens=0, cache_read_input_tokens=500)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(out))], stop_reason="end_turn", usage=usage)


def test_mine_tasks(app):
    db, repo = app
    tasks = mine_tasks(db, repo, START, END)
    assert [(t["kind"], t["number"], t["target"]) for t in tasks] == [
        ("issue", 1, ["src/app/parser.py"]), ("pr", 3, ["src/app/cli.py"])]   # docs-only commit is not a task
    assert [t["number"] for t in mine_tasks(db, repo, START, END, kinds=("issue",))] == [1]
    assert all(safe_start(repo, t) for t in tasks)


def test_changed_lines_and_score(app):
    db, repo = app
    t = mine_tasks(db, repo, START, END)[0]
    real = changed_lines(run_git(repo, "show", "--format=", "-U0", t["sha"]), keep=set(t["target"]))
    assert real == {("src/app/parser.py", "+", "if not text:"), ("src/app/parser.py", "+", 'return ""')}
    perfect = score({"files": ["src/app/parser.py"], "patch": run_git(repo, "show", "--format=", t["sha"])}, t, real)
    assert perfect == {"hit@1": 1.0, "hit@3": 1.0, "recall@5": 1.0, "mrr": 1.0,
                       "line_precision": 1.0, "line_recall": 1.0, "line_f1": 1.0}
    assert score({"files": ["src/app/cli.py", "src/app/parser.py"]}, t, real)["mrr"] == 0.5


def test_sandbox_sees_only_the_code_before_the_change(app):
    db, repo = app
    issue_task, pr_task = mine_tasks(db, repo, START, END)
    with SandboxHost(db, repo, "kb_heuristic", code_snapshot=True) as h:
        h.advance(issue_task["T"], snapshot=True)
        with open(os.path.join(h.root, "code", "src", "app", "parser.py")) as f:
            assert "if not text" not in f.read()                     # the fix is not there yet
        assert h.predict_change(issue_task, 3)["files"][0] == "src/app/parser.py"
        with pytest.raises(SandboxError):
            h.predict_change(pr_task, 3)                               # PR #3 does not exist yet at this T
        probe = h.probe()
        assert all(v.startswith("blocked") for v in probe.values()), probe


def test_baselines_and_kb_heuristic(app):
    db, repo = app
    res = replay_next_change(db, repo, ["random", "most_changed_files", "kb_heuristic"], START, END, kinds=("issue", "pr"))
    by = {r["predictor"]: r for r in res["results"]}
    assert res["run"]["tasks_scored"] == 2
    assert by["kb_heuristic"]["all"]["hit@1"] == 1.0
    assert by["kb_heuristic"]["all"]["patches"] == 0 and by["kb_heuristic"]["all"]["line_f1"] is None


def test_claude_localizes_and_patches(app):
    db, repo = app
    fake = FakeClaude()
    res = replay_next_change(db, repo, ["kb_heuristic", "claude_insight"], START, END, kinds=("issue", "pr"),
                             llm=LLMGateway(client=fake))
    claude = next(r for r in res["results"] if r["predictor"] == "claude_insight")
    assert claude["all"]["hit@1"] == 1.0 and claude["all"]["patches"] == 2 and claude["all"]["line_f1"] == 1.0
    st = claude["sandbox"]["predictor_stats"]
    assert st["edits_applied"] == 2 and st["edits_failed"] == 0
    assert len(fake.calls) == 4                                        # per task: one localize + one patch request
    assert all(c["system"][0]["cache_control"] == {"type": "ephemeral"} for c in fake.calls)
    patch_prompts = [c["messages"][0]["content"] for c in fake.calls if "=== " in c["messages"][0]["content"]]
    assert all("util.py" not in p for p in patch_prompts)              # only the chosen file is sent in full


def test_claude_dry_run_equals_heuristic(app):
    db, repo = app
    res = replay_next_change(db, repo, ["kb_heuristic", "claude_insight"], START, END, kinds=("issue", "pr"),
                             llm=LLMGateway(mode="dry-run"))
    a, b = ({k: v for k, v in r.items() if k not in ("sandbox", "predictor")} for r in res["results"])
    assert a == b
