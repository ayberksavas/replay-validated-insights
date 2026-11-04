# Insight system: plan

Goal: a `Predictor` (see `timemachine/predictor.py`) that beats the random and most-changed-files baselines on Outputs 1-3 and is explainable. It receives only a `TimeView`; the evaluator is unchanged.

## Layers
1. **Knowledge base** (`timemachine/kb/`): deterministic, no LLM, built from one `TimeView`. File history, commit/issue link graph, pre-T bug-fix memory, BM25 over issues.
2. **Heuristic predictors** (`timemachine/heuristics.py`): KB-feature predictors without LLM. They are the "strong baselines": any LLM gain must be measured against them, not only against random.
3. **Tools** (`timemachine/tools.py`, later): typed functions over the KB for the agent; every call logged.
4. **Claude agent** (`timemachine/agent.py`, later): plain Anthropic API tool-use loop; per-candidate evidence packet in, score + rationale out.
5. **`InsightPredictor`** (later): pre-filter, cache per (T, target), record model/prompt/params.

## Tasks and status
| | Task | Status |
|---|---|---|
| C | Heuristic predictors + BM25 issue search | done v1 (`KBHeuristic`, in `tm replay`), not yet run on real data |
| B | KB v1 (file stats, link graph, fix memory) | done v1, `tests/test_kb.py` |
| E | Output 3 agent: BM25 retrieve, LLM rerank | not started (needs API key and budget decision) |
| D | Tools layer + call log + per-tool leakage tests | not started |
| F | Agent for Outputs 1, 2 | not started |
| G | Output 4 diff similarity | not started |
| H | Cost controls, run log, replay vs baselines | not started |

## Rules
- KB and predictors import nothing from `groundtruth.py` (the Oracle). Shared regexes are duplicated in `kb/links.py` on purpose.
- Everything is derived from `TimeView` methods or `view.db`; never a path or the full DB.
- Every KB feature gets a leakage test: build the KB at T, add later data to the store, assert the KB is unchanged.
- No LLM, API key or network is needed for B and C.

## Known limitations of v1
- Fix detection is keyword-based, as in the oracle, so KB fixes and oracle fixes share the same blind spots. Better detection (roadmap item 1) improves both.
- Heuristic weights are hand-set, not learned. A per-T trained model on pre-T SZZ labels is a possible later step.
- A fix commit already inside the view (between a candidate and T) counts as history for that candidate's files; that is legitimate at T but means very recent bugs are partly "known".
