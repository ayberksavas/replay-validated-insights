# Architecture

```
git repo + GitHub API --ingest--> tm.db (timestamped)
                                    |            \
                          TimeView(T) [snapshot]   Oracle [full history]
                                    |                 |
                              Predictor  --flags-->  evaluate.replay --> metrics
```
Data lives in `data/<owner>__<repo>/{repo,tm.db}` (gitignore it).

## Modules (`timemachine/`)
- `store.py`: schema. Every table has `ts` = when the fact became visible. Tables: `commits`, `commit_files` (flags `is_test`, `is_doc` by path regex), `issues` (issues and PRs, `is_pr`), `issue_events` (closed/reopened/labeled/... as separate timestamped rows; `data` JSON has `commit_id`, `label`), `comments` (`kind` issue|review).
- `ingest_git.py`, `ingest_github.py`: fill the store.
- `view.py`: `TimeView(db, repo, as_of)`. Copies only rows with ts <= T into an in-memory DB (read-only). Methods: `commits`, `files`, `issues`, `issue_state` (state/labels rebuilt from events <= T), `comments`, `diff(sha)`, `file_at(sha, path)`. The last two raise `LeakageError` for commits not in the snapshot. The repo path is name-mangled and never given to predictors.
- `predictor.py`: `Flag(target, score)` and `Predictor` with `risky_commits`, `missing_tests`, `similar_issues`. Baselines here.
- `groundtruth.py`: `Oracle`, full-history labeler (never give it to a predictor).
- `evaluate.py`: `replay(...)` loop and metric aggregation.
- `snapshot.py`: `export_snapshot(repo, T, dest, history=False)`. Writes the code as of T to a fresh git repo for an agent to work in (no remote/tags/reflog/future objects). Use this, never a checkout of the full clone.
- `cli.py`: `tm ingest`, `tm replay`, `tm snapshot`.

## Leakage rule (spec: "enforced in code")
Predictors receive only a `TimeView`. Do not pass them the db path, repo path, or an `Oracle`. If you add a data source, add it to `TimeView._copy` with a timestamp filter and add a test in `tests/test_leakage.py`. Anything an agent can shell out to (e.g. running `git` on the clone itself) bypasses the guarantee, so agent tools must be built on `TimeView` methods only.

## Replay semantics
- Outputs 1/2: at each T, candidates = commits in (T - lookback, T]. The predictor scores them; the top `top_frac` are flagged and compared to the oracle's positives from (T, T+window].
- Output 3: each new issue in (T, T+lookback] is queried with a view at `issue.ts - 1`; hit if any top-k result is in the issue's later-declared duplicate targets.
