# Time Machine: replay-validated insights for software quality

Two systems, one repo:

1. **Insight system (not built yet)**: an agentic knowledge base over a repo's history (commits, issues, PRs, tests/docs, optionally CI). Claude-based agents use it to flag risky changes, missing tests, similar past issues, etc.
2. **Time machine (built, this code)**: replays a repo's history. At a chosen moment T the insight system sees *only* data up to T and produces insights. We then check them against what actually happened after T (within a window, e.g. 6 months) and score precision / recall / F1 / lead time.

Spec of inputs, outputs and ground-truth rules: `PROJECT.pdf`. Original notes: `initial_info.md`.

## Docs (read in this order)
- `docs/STATE.md`: what is done, what is not, known gaps, results so far. **Start here.**
- `docs/ARCHITECTURE.md`: modules, data model, the leakage guarantee.
- `docs/GROUND_TRUTH.md`: how each output is labeled by the oracle, and its weaknesses.
- `docs/HOWTO.md`: setup, ingest, replay, writing your own `Predictor`.
- `docs/ROADMAP.md`: prioritized next steps.
- `docs/INSIGHT_PLAN.md`: design and task list for the insight system.

## Quick start
```
pip install -e . pytest
tm ingest pallets/click                       # clone + ingest git + GitHub data (needs `gh auth login`)
tm replay pallets/click --from 2021-01-01 --to 2025-06-01 --every-days 60
python -m pytest tests
```
