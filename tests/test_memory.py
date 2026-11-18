"""InsightMemory: append-only dated notes, leak guards, deterministic hotspot notes, sandbox + notebook file."""
import json, os, tempfile
import pytest
from conftest import ts
from timemachine.kb import KnowledgeBase
from timemachine.kb.events import events_from_db
from timemachine.kb.memory import InsightMemory, NoteError, derive_notes
from timemachine.sandbox.host import SandboxHost, SandboxError
from timemachine.store import connect

T = [ts(f"2020-{m:02d}-28T00:00:00Z") for m in (3, 6, 9, 12)]


def kb_at(db, t):
    c = connect(db)
    kb = KnowledgeBase().apply([e for e in events_from_db(c) if e["vts"] <= t], as_of=t)
    c.close()
    return kb


def note(i="n1", **kw):
    return {"id": i, "kind": "file_hotspot", "claim": "src/core.py is fragile", "scope": ["src/core.py"], **kw}


def test_versions_are_append_only_and_time_filtered():
    m = InsightMemory()
    m.add(note(claim="v1"), 100)
    m.add(note(claim="v2"), 200)
    assert m.latest("n1", 150)["claim"] == "v1" and m.latest("n1", 250)["claim"] == "v2"
    assert m.latest("n1", 50) is None and m.active(50) == []
    assert [n["version"] for n in m._versions["n1"]] == [1, 2]
    with pytest.raises(NoteError):
        m.add(note(claim="back in time"), 150)          # older than the stored version
    with pytest.raises(NoteError):
        m.add(note(created_at=300), 200)                # dated after T
    m.mark("n1", "refuted", 300, "later fix did not touch it")
    assert m.active(250)[0]["claim"] == "v2" and m.active(300) == []


def test_retrieve_by_path_and_text_only_sees_the_past():
    m = InsightMemory()
    m.add(note("a", scope=["src/core.py"], claim="core parser crashes on empty input"), 100)
    m.add(note("b", scope=["src/util.py"], claim="util is stable"), 100)
    m.add(note("c", scope=["src/core.py"], claim="written later"), 500)
    assert [n["id"] for n in m.retrieve(200, paths=["src/core.py"])][0] == "a"
    assert {n["id"] for n in m.retrieve(200, paths=["src/other.py"])} == {"a", "b"}   # same directory
    assert "c" not in {n["id"] for n in m.retrieve(200, paths=["src/core.py"])}
    assert [n["id"] for n in m.retrieve(200, text="parser crash empty")] == ["a"]
    assert m.retrieve(50, paths=["src/core.py"]) == []
    assert m.render(m.retrieve(200, paths=["src/core.py"])).startswith("- [1970-01-01]")


def test_evidence_must_exist_before_the_note(history):
    db, _ = history
    kb = kb_at(db, T[0])
    early = next(iter(kb._commits))
    m = InsightMemory()
    m.add(note(evidence=[early]), T[0], kb=kb)
    later = kb_at(db, T[3])
    newest = max(later._commits.values(), key=lambda c: c["ts"])["sha"]
    with pytest.raises(NoteError):
        m.add(note("x", evidence=[newest]), T[0], kb=later)     # exists in this KB, but dated after the note
    with pytest.raises(NoteError):
        m.add(note("y", evidence=["f" * 40]), T[0], kb=kb)      # unknown to the KB
    with pytest.raises(NoteError):
        m.add(note("z", evidence=["#9999"]), T[0], kb=kb)


def test_hotspot_notes_grow_with_history(history):
    db, _ = history
    m = InsightMemory()
    counts = []
    for t in T:
        derive_notes(kb_at(db, t), m, t)
        counts.append(len(m.active(t, "file_hotspot")))
    assert counts[0] == 0 or counts[-1] >= counts[0]
    assert counts[-1] >= 1
    core = m.latest("hotspot:src/core.py", T[3])
    assert core and core["evidence"] and core["created_at"] <= T[3]
    # same data again -> no new versions (no churn)
    before = m.summary()["versions"]
    derive_notes(kb_at(db, T[3]), m, T[3])
    assert m.summary()["versions"] == before
    # a note dated T0 cannot be seen by a reader at an earlier time
    first = min(v["created_at"] for vs in m._versions.values() for v in vs)
    assert m.active(first - 1) == []


def test_import_refuses_newer_notes():
    m = InsightMemory()
    m.add(note(), 100)
    stored = m.drain()
    ok = InsightMemory(); ok.import_notes(stored, start=100)
    assert ok.active(100)
    with pytest.raises(NoteError):
        InsightMemory().import_notes(stored, start=99)


def test_sandbox_writes_notebook_and_warm_start_is_resumable(history):
    db, repo = history
    path = os.path.join(tempfile.mkdtemp(), "notes", "insights.jsonl")
    with SandboxHost(db, repo, "kb_heuristic", memory_path=path) as h:
        for t in T[:3]:
            h.advance(t)
        mid = h.worker_stats()["memory"]
    first = InsightMemory.loads(open(path).read())
    assert first and all(n["created_at"] <= T[2] for n in first) and mid["notes"] >= 1
    # resume: a new sandbox whose first step is T[2] loads the notebook and continues the same versions
    with SandboxHost(db, repo, "kb_heuristic", memory_path=path) as h:
        h.advance(T[2])    # notes up to T[2] are allowed
        assert h.stats["notes_imported"] == len(first)
        h.advance(T[3])
        resumed = h.worker_stats()["memory"]
    # continuous run for comparison
    with SandboxHost(db, repo, "kb_heuristic") as h:
        for t in (T[0], T[2], T[3]):
            h.advance(t)
        cont = h.worker_stats()["memory"]
    assert resumed["notes"] == cont["notes"]
    assert resumed["by_status"] == cont["by_status"]


def test_warm_start_from_a_notebook_of_the_future_is_refused(history):
    db, repo = history
    path = os.path.join(tempfile.mkdtemp(), "insights.jsonl")
    with SandboxHost(db, repo, "kb_heuristic", memory_path=path) as h:
        h.advance(T[3])
    assert os.path.getsize(path) > 0
    with SandboxHost(db, repo, "kb_heuristic", memory_path=path) as h:
        with pytest.raises(SandboxError, match="notes after the run's start"):
            h.advance(T[0])


def test_sandbox_memory_never_holds_notes_from_after_t(history):
    db, repo = history
    with SandboxHost(db, repo, "kb_heuristic") as h:
        for t in T:
            out = h.advance(t)
            assert all(n["created_at"] <= t for n in out["notes"])


# ---- notes in ClaudeInsight prompts ----
import re
from datetime import datetime, timezone
from test_llm import FakeClaude, TIMES
from timemachine.evaluate import replay_incremental
from timemachine.llm import LLMGateway

BLOCK = "NOTES FROM EARLIER STEPS"


def test_notes_reach_the_prompt_only_when_memory_is_on_and_only_from_the_past(history):
    db, repo = history
    path = os.path.join(tempfile.mkdtemp(), "insights.jsonl")
    steps = []

    class Tagged(FakeClaude):      # remember how many steps had finished when each request was made
        def create(self, **kw):
            r = super().create(**kw)
            self.calls[-1]["_done"] = len(steps)
            return r

    fake = Tagged()
    out = replay_incremental(db, repo, ["claude_insight"], TIMES, window_days=180, lookback_days=30,
                             llm=LLMGateway(client=fake), options={"claude_insight": {"memory": True}},
                             memory_paths={"claude_insight": path}, on_step=lambda s: steps.append(s))
    prompts = [(c["system"][0]["text"], c["messages"][0]["content"]) for c in fake.calls]
    with_notes = [(s, u) for s, u in prompts if BLOCK in u]
    assert with_notes, "no prompt contained notes"
    assert all("NOTES FROM EARLIER STEPS above the packets" in s for s, _ in prompts)   # system explains them
    stored = InsightMemory.loads(open(path).read())
    first_note = min(n["created_at"] for n in stored)
    for c in fake.calls:
        u = c["messages"][0]["content"]
        if BLOCK not in u:
            continue
        block = u.split(BLOCK, 1)[1].split("\n\n", 1)[0]
        days = re.findall(r"^- \[(\d{4}-\d{2}-\d{2})\]", block, re.M)
        assert days
        now = steps[c["_done"]]["T"] if c["_done"] < len(steps) else max(TIMES)   # the step this request belongs to
        assert all(datetime.fromisoformat(d).replace(tzinfo=timezone.utc).timestamp() <= now for d in days)
    # the earliest prompt cannot carry a note dated after that prompt's step: the first note is dated >= first_note
    assert first_note > TIMES[0]
    st = out["results"][0]["sandbox"]["predictor_stats"]
    assert st["notes_used"] >= 1 and st["notes_chars"] > 0

    # memory off: byte-identical prompts to a plain run, no notes block, no system addendum
    off = FakeClaude()
    replay_incremental(db, repo, ["claude_insight"], TIMES, window_days=180, lookback_days=30, llm=LLMGateway(client=off))
    assert all(BLOCK not in c["messages"][0]["content"] and "NOTES FROM" not in c["system"][0]["text"] for c in off.calls)


def test_placebo_modes_keep_the_notes_but_change_their_content(history):
    db, repo = history

    def run(mode):
        fake = FakeClaude()
        replay_incremental(db, repo, ["claude_insight"], TIMES, window_days=180, lookback_days=30,
                           llm=LLMGateway(client=fake), options={"claude_insight": {"memory": mode}})
        blocks = []
        for c in fake.calls:
            u = c["messages"][0]["content"]
            if BLOCK in u:
                blocks.append(u.split(BLOCK, 1)[1].split("\n\n", 1)[0])
        return blocks

    real, generic, shuffled = run("real"), run("generic"), run("shuffled")
    assert real and len(real) == len(generic) == len(shuffled)
    lines = lambda b: [l for l in b.splitlines() if l.startswith("- [")]
    dates = lambda b: [re.match(r"- \[([\d-]+)\] ([^:]+):", l).groups() for l in lines(b)]
    for r, g, s in zip(real, generic, shuffled):
        assert dates(r) == dates(g) == dates(s)                       # same notes: same dates, same paths
    assert all("bug fixes" not in l and not re.search(r"\d+ of \d+", l) for b in generic for l in lines(b))
    assert all("changed before; changes here deserve extra review" in l for b in generic for l in lines(b))
    assert generic != real and shuffled != real
    assert run("shuffled") == shuffled                                  # deterministic, so cacheable
    with pytest.raises(ValueError):
        from timemachine.insight import ClaudeInsight
        ClaudeInsight(memory="bogus")


def test_compare_reports_flips(history, tmp_path):
    db, repo = history
    outs = []
    for mode in (False, "generic"):
        res = replay_incremental(db, repo, ["kb_heuristic", "claude_insight"], TIMES, window_days=180, lookback_days=30,
                                 llm=LLMGateway(client=FakeClaude()), options={"claude_insight": {"memory": mode}})
        p = tmp_path / f"{mode}.json"
        p.write_text(json.dumps(res, default=str))
        outs.append(str(p))
    from timemachine import compare
    compare.main(outs)
