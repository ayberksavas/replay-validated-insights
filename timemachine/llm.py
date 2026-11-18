"""Host-side LLM gateway. The ONLY place that talks to the Claude API.

The sandbox sends {system, user, schema, effort, max_tokens, tag}; the gateway
- answers from a persistent response cache when the exact request was seen before (0 tokens),
- otherwise calls Claude with the system prompt marked for prompt caching and a JSON-schema
  structured output, and records input / cache-write / cache-read / output tokens,
- stops calling once a token budget is spent (the predictor then falls back to its heuristic),
- in dry-run mode (no credentials, or --llm dry-run) makes no call and only estimates tokens.
The API key never enters the sandbox process.
"""
import hashlib
import json
import os
from collections import Counter

DEFAULT_MODEL = "claude-opus-5-5"
# $ per million tokens: input, output, cache read, cache write (5 min). Haiku cache rates assumed 0.1x / 1.25x input.
PRICES = {"claude-opus-5-5": (4.0, 20.0, 0.20, 5.0), "claude-sonnet-5-5": (2.0, 10.0, 0.10, 2.5),
          "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125)}
SERVER_FALLBACK_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-5", "claude-fable-5-2", "claude-fable-5-1")
CHARS_PER_TOKEN = 4.0   # rough estimate, used only for dry runs and the naive-vs-optimized comparison


def est_tokens(chars: int) -> int:
    return int(round(chars / CHARS_PER_TOKEN))


def has_credentials() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    return os.path.isdir(os.path.join(os.path.expanduser("~"), ".config", "anthropic"))


class LLMGateway:
    def __init__(self, model=DEFAULT_MODEL, cache_path=None, budget_tokens=None, mode="auto", client=None):
        """mode: auto (live if credentials are found, else dry-run) | live | dry-run."""
        self.model, self.cache_path, self.budget = model, cache_path, budget_tokens
        self.usage, self.calls = Counter(), Counter()
        self.errors = []
        self._cache = {}
        if cache_path and os.path.exists(cache_path):
            with open(cache_path, encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                        self._cache[r["key"]] = r["text"]
                    except (ValueError, KeyError):
                        continue
        self._client = client
        if mode == "auto":
            mode = "live" if (client is not None or has_credentials()) else "dry-run"
        self.mode = mode
        if self.mode == "live" and self._client is None:
            import anthropic   # host-only dependency: pip install -e .[llm]
            self._client = anthropic.Anthropic(max_retries=4)

    def key(self, req) -> str:
        blob = json.dumps({"model": self.model, **{k: req.get(k) for k in ("system", "user", "schema", "effort")}},
                          sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def spent(self) -> int:
        u = self.usage
        return u["input_tokens"] + u["cache_creation_input_tokens"] + u["cache_read_input_tokens"] + u["output_tokens"]

    def call(self, req: dict) -> dict:
        tag = req.get("tag", "other")
        self.calls["requests"] += 1
        self.calls[f"requests_{tag}"] += 1
        k = self.key(req)
        if k in self._cache:
            self.calls["response_cache_hits"] += 1
            return {"text": self._cache[k], "cached": True}
        est = est_tokens(len(req["system"]) + len(req["user"]))
        if self.mode == "dry-run":
            self.calls["dry_run"] += 1
            self.usage["est_input_tokens"] += est
            return {"text": None, "dry_run": True, "est_input_tokens": est}
        if self.budget is not None and self.spent() >= self.budget:
            self.calls["budget_skipped"] += 1
            return {"text": None, "skipped": "budget"}
        try:
            resp = self._create(req)
        except Exception as e:  # noqa: BLE001 - the replay continues on the heuristic
            self.calls["errors"] += 1
            self.errors.append(f"{type(e).__name__}: {str(e)[:200]}")
            if type(e).__name__ in ("AuthenticationError", "PermissionDeniedError"):
                self.mode = "dry-run"     # no point retrying every step
            return {"text": None, "error": type(e).__name__}
        u = resp.usage
        for f in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
            self.usage[f] += getattr(u, f, 0) or 0
        self.calls["api_calls"] += 1
        if getattr(resp, "stop_reason", None) == "refusal":
            self.calls["refusals"] += 1
            return {"text": None, "refused": True}
        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), None)
        if text:
            self._cache[k] = text
            if self.cache_path:
                with open(self.cache_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"key": k, "tag": tag, "model": self.model, "text": text}) + "\n")
        return {"text": text, "usage": {f: getattr(u, f, 0) or 0 for f in
                                        ("input_tokens", "output_tokens", "cache_read_input_tokens")}}

    def _create(self, req):
        kw = dict(
            model=self.model,
            max_tokens=req.get("max_tokens", 4000),
            # long, fixed instructions first and marked for caching; the varying packets go in the user turn
            system=[{"type": "text", "text": req["system"], "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": req["user"]}],
            output_config={"effort": req.get("effort", "low"),
                           "format": {"type": "json_schema", "schema": req["schema"]}},
        )
        if self.model in SERVER_FALLBACK_MODELS:
            # server-side refusal fallback: a declined request is re-run on a fallback model in the same call
            return self._client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kw)
        return self._client.messages.create(**kw)

    def cost_usd(self) -> float | None:
        p = PRICES.get(self.model)
        if not p:
            return None
        u = self.usage
        return (u["input_tokens"] * p[0] + u["output_tokens"] * p[1] + u["cache_read_input_tokens"] * p[2]
                + u["cache_creation_input_tokens"] * p[3]) / 1e6

    def report(self) -> dict:
        return {"model": self.model, "mode": self.mode, "calls": dict(self.calls), "usage": dict(self.usage),
                "tokens_spent": self.spent(), "cost_usd": self.cost_usd(), "budget": self.budget,
                "errors": self.errors[:5]}
