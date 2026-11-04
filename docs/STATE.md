# Current state (as of 2026-10-19)

## Done
- Timestamped SQLite store (commits, per-file changes, issues/PRs, issue events, comments).
- Ingestion: git (`git log --numstat`) and GitHub (`gh api`: issues, repo-wide issue events, issue + review comments).
- `TimeView`: leak-proof snapshot at T, the only thing a predictor may touch.
- `Oracle`: ground truth for Outputs 1 (SZZ), 2, 3.
- Evaluation loop with precision/recall/F1, lead time, hit@k; two baselines (random, most-changed-files).
- CLI (`tm ingest`, `tm replay`, `tm snapshot`) and leakage tests (TimeView commits; snapshot has no future objects/refs/source path).
- `tm snapshot`: exports the code as of T into a fresh repo for a coding agent, so `.git` cannot reveal later commits. Network/filesystem isolation of the agent is still up to the runner (see HOWTO).

## Not done
- **The actual insight system** (Claude-backed `Predictor` + knowledge base). Nothing here uses an LLM yet.
- Output 4 (similar bug from diffs), 5 (outdated docs), 7 (requirement vs implementation), 6 / Input 5 (CI data). Core tier is Outputs 1-4, Inputs 1-4.
- PR data beyond comments: PR-to-commit links, merge commits, PR descriptions are only in `issues` (is_pr=1); no PR->commit mapping.
- Repo selection, incl. repos/periods after the model's training cutoff (memorization check in spec).
- Random baseline averaged over seeds; confidence intervals; manual review sample (~30 cases) for Output 7.
- More tests (only leakage on commits is covered; no tests for issue-state reconstruction, oracle, evaluator).

## Known gaps / caveats
- **Edited issue text leaks**: GitHub returns only the latest title/body, so edits after T are visible. Only unclosed leakage hole. Mitigate or document per repo.
- Visibility time of a commit = committer date; rebases/force-pushes can distort.
- Output 1 positives are few: bug-fix detection is only message keywords (`fix|bug|hotfix|regression|resolve|patch`) or closing an issue labeled `bug`. Fix detector is crude.
- Output 3 duplicate truth only from "duplicate of #N" text in comments/body (misses `marked_as_duplicate` targets and cross-references).
- Output 2 truth is heuristic (stem match of later test file, or later bug fix on same files).
- `ingest_github` re-fetches everything (no incremental); click took ~7 min.
- Replay re-materializes a snapshot per T and per queried issue; fine for click, will be slow on big repos.

## Results so far (pallets/click, T every 60 days 2021-01 to 2025-06, window 180d, lookback 30d, flag top 20%)
| | random | most-changed-files |
|---|---|---|
| Out1 F1 (10 positives) | 0.01 | 0.083 |
| Out2 F1 (72 positives) | 0.16 | 0.29 |
| Out3 hit@5 (15 dup issues) | 0.0 | 0.167 |

Tiny sample sizes; treat as smoke test, not a benchmark.
