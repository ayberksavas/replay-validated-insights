# Time Machine: replay-validated insights for software quality

Two systems, one repo:

1. **Insight system (first version)**: an incrementally updated knowledge base over a repo's history (commits, issues, PRs, tests/docs) plus a token-optimized Claude predictor that flags risky changes, missing tests and similar past issues.
2. **Time machine (built)**: replays a repo's history *incrementally*. Each system under test runs in its own sandbox process that is fed only the data up to T, step by step, and produces insights. We then check them against what actually happened after T (within a window, e.g. 6 months) and score precision / recall / F1 / lead time.

Spec of inputs, outputs and ground-truth rules: `PROJECT.pdf`. Original notes: `initial_info.md`.

## Docs (read in this order)
- `docs/STATE.md`: what is done, what is not, known gaps, results so far. **Start here.**
- `docs/ARCHITECTURE.md`: modules, data model, the leakage guarantee.
- `docs/GROUND_TRUTH.md`: how each output is labeled by the oracle, and its weaknesses.
- `docs/HOWTO.md`: setup, ingest, replay, writing your own `Predictor`.
- `docs/ROADMAP.md`: prioritized next steps.
- `docs/INSIGHT_PLAN.md`: design and task list for the insight system.
- `docs/DEMO.md`: demo script.
- `docs/NEXT_CHANGE.md`: Output 8, predicting the next code change (files + patch) for an issue or PR.

## Quick start
```
pip install -e ".[llm,dev]"
tm ingest pallets/click                       # clone + git + GitHub data (gh login or GITHUB_TOKEN); incremental on re-runs
tm demo pallets/click --from 2021-01-01 --to 2025-06-01   # sandboxed incremental replay, live timeline + HTML report
tm replay pallets/click --from 2021-01-01 --to 2025-06-01 --every-days 60
tm nextchange gabrieldemarmiesse/python-on-whales --from 2020-08-01 --to 2026-11-01 --kinds issue   # Output 8
python -m pytest tests
```
