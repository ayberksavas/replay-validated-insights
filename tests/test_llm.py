"""LLM gateway (host) + ClaudeInsight (sandbox) with a fake Claude client: no network, no key."""
import json
import os
import re
import tempfile
from types import SimpleNamespace

from conftest import ts
from timemachine.evaluate import replay_incremental
from timemachine.groundtruth import DAY
from timemachine.insight import compact_diff
from timemachine.llm import LLMGateway

TIMES = list(range(ts("2020-02-01T00:00:00Z"), ts("2020-12-15T00:00:00Z"), 60 * DAY))


class FakeClaude:
    """Answers like Claude would under the JSON schema: risk from the subject, issues in given order."""

    def __init__(self):
        self.calls = []
        self.beta = SimpleNamespace(messages=self)
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        user = kw["messages"][0]["content"]
        if "commits" in kw["output_config"]["format"]["schema"]["properties"]:
            ids = re.findall(r"^(c\d+) \| (.*)$", user, re.M)
            out = {"commits": [{"id": i, "risk": 90 if "feature" in subj else 10, "needs_tests": 60,
                                "why": "new logic in a bug-prone file"} for i, subj in ids]}
        else:
            ids = re.findall(r"^(i\d+) \[", user, re.M)
            out = {"matches": [{"id": i, "same_problem": 100 - 10 * n} for n, i in enumerate(ids)]}
        usage = SimpleNamespace(input_tokens=200, output_tokens=40, cache_creation_input_tokens=0, cache_read_input_tokens=600)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(out))], stop_reason="end_turn", usage=usage)


REQ = {"system": "rubric " * 200, "user": "c1 | Fix x", "schema": {"type": "object", "properties": {"commits": {}}},
       "effort": "low", "max_tokens": 100, "tag": "commits"}


def test_gateway_request_shape_and_response_cache():
    fake, path = FakeClaude(), os.path.join(tempfile.mkdtemp(), "cache.jsonl")
    gw = LLMGateway(cache_path=path, client=fake)
    assert gw.mode == "live"
    gw.call(REQ)
    kw = fake.calls[0]
    assert kw["model"] == "claude-opus-5-5"
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}            # prompt caching on the fixed rubric
    assert kw["output_config"]["effort"] == "low" and kw["output_config"]["format"]["type"] == "json_schema"
    assert kw["fallbacks"] == "default" and kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert gw.call(REQ)["cached"] and len(fake.calls) == 1                         # same request: no second call
    again = LLMGateway(cache_path=path, client=FakeClaude())
    assert again.call(REQ)["cached"] and again.spent() == 0                        # persists across runs


def test_gateway_budget_and_dry_run():
    gw = LLMGateway(client=FakeClaude(), budget_tokens=1)
    assert gw.call(REQ)["text"]
    assert gw.call({**REQ, "user": "c1 | other"}) == {"text": None, "skipped": "budget"}
    dry = LLMGateway(mode="dry-run")
    r = dry.call(REQ)
    assert r["text"] is None and r["est_input_tokens"] > 0 and dry.spent() == 0


def test_compact_diff_keeps_only_changed_code_lines():
    diff = ("diff --git a/src/a.py b/src/a.py\nindex 1..2 100644\n--- a/src/a.py\n+++ b/src/a.py\n"
            "@@ -1,4 +1,4 @@ def parse(x):\n     keep = 1\n-    if x:\n+    if x is not None:\n \n"
            "diff --git a/tests/test_a.py b/tests/test_a.py\n--- a/tests/test_a.py\n+++ b/tests/test_a.py\n@@ -1 +1 @@\n-a\n+b\n")
    text, dropped = compact_diff(diff, keep=lambda p: p.startswith("src/"))
    assert text.splitlines() == ["# src/a.py", "@@ def parse(x):", "- if x:", "+ if x is not None:"] and dropped == 0


def test_claude_insight_in_sandbox_uses_few_requests_and_replays_free(history):
    db, repo = history
    path = os.path.join(tempfile.mkdtemp(), "cache.jsonl")
    fake = FakeClaude()
    gw = LLMGateway(cache_path=path, client=fake)
    out = replay_incremental(db, repo, ["claude_insight"], TIMES, window_days=180, lookback_days=30, llm=gw)
    st = out["results"][0]["sandbox"]["predictor_stats"]
    steps = sum(1 for s in out["steps"] if s["candidates"])
    assert st["requests_commits" if "requests_commits" in st else "requests"] >= 1
    assert gw.calls["requests_commits"] == steps                    # ONE request per step for Outputs 1 + 2
    assert st["commits_sent"] <= st["commits_seen"] and st["diff_chars_sent"] < st["diff_chars_raw"]
    assert st["prompt_chars"] < st["naive_chars"]
    assert any(f["why"] for s in out["steps"] for f in s["by_predictor"]["claude_insight"]["risky"])
    # replay again with the same cache: zero API calls
    fake2 = FakeClaude()
    gw2 = LLMGateway(cache_path=path, client=fake2)
    out2 = replay_incremental(db, repo, ["claude_insight"], TIMES, window_days=180, lookback_days=30, llm=gw2)
    assert fake2.calls == [] and gw2.calls["response_cache_hits"] == gw.calls["requests"]
    assert {k: v for k, v in out2["results"][0].items() if k != "sandbox"} == {k: v for k, v in out["results"][0].items() if k != "sandbox"}


def test_claude_insight_dry_run_equals_heuristic(history):
    db, repo = history
    out = replay_incremental(db, repo, ["kb_heuristic", "claude_insight"], TIMES, window_days=180, lookback_days=30,
                             llm=LLMGateway(mode="dry-run"))
    a, b = ({k: v for k, v in r.items() if k not in ("sandbox", "predictor")} for r in out["results"])
    assert a == b
