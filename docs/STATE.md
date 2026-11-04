# Current state (as of 2026-11-04)

## Done
- Timestamped SQLite store (commits, per-file changes, issues/PRs, issue events, comments).
- Ingestion: git (`git log --numstat`) and GitHub (`gh api`: issues, repo-wide issue events, issue + review comments).
- `TimeView`: leak-proof snapshot at T, the only thing a predictor may touch.
- `Oracle`: ground truth for Outputs 1 (SZZ), 2, 3.
- Evaluation loop with precision/recall/F1, lead time, hit@k; two baselines (random, most-changed-files). Replay points with no positives are skipped (not scored 0); each output reports `replay_points` (points actually averaged).
- CLI (`tm ingest`, `tm replay`, `tm snapshot`) and tests (TimeView commits; snapshot has no future objects/refs/source path; `ATTACH` blocked in TimeView; missing/empty clone raises; SZZ on a fixture repo; metric averaging).
- Git failures are loud: `gitutil.run_git` raises `GitError`, and `Oracle`/`TimeView`/`ingest_git` require an actual clone (`require_repo`), so a missing clone can no longer look like "no positives". `tm ingest` clones when the folder is not a clone.
- `data/` (clones, `tm.db`) is gitignored and regenerable; it is not in git.
- `tm snapshot`: exports the code as of T into a fresh repo for a coding agent, so `.git` cannot reveal later commits. Network/filesystem isolation of the agent is still up to the runner (see HOWTO).

## Not done
- **The actual insight system** (Claude-backed `Predictor` + knowledge base). Nothing here uses an LLM yet.
- Output 4 (similar bug from diffs), 5 (outdated docs), 7 (requirement vs implementation), 6 / Input 5 (CI data). Core tier is Outputs 1-4, Inputs 1-4.
- PR data beyond comments: PR-to-commit links, merge commits, PR descriptions are only in `issues` (is_pr=1); no PR->commit mapping.
- Repo selection, incl. repos/periods after the model's training cutoff (memorization check in spec).
- Random baseline averaged over seeds; confidence intervals; manual review sample (~30 cases) for Output 7.
- More tests: still none for issue-state reconstruction, Output 2/3 truth, or the full replay loop.
- **Sandboxed agent runtime** (container, network limited to the model API, pinned deps). Only the snapshot half exists.
- Ingest records no provenance: the `meta` table is unused, so `tm.db` does not say which repo/when it was ingested.

## Known gaps / caveats
- **Edited text leaks**: GitHub returns only the latest issue/PR title and body (and comment text), so edits after T are visible (click has 805 `renamed` events). Mitigate or document per repo.
- **In-process predictors are not isolated**: `TimeView` blocks `ATTACH`, but Python code can still read the clone path (`view._TimeView__repo`) or import the Oracle. Real isolation needs a separate process/container.
- Output 3 truth can point at an issue number newer than the query issue (not in the view, so never a hit); `marked_as_duplicate` events (53 on click) are unused.
- Output 2 truth overlaps Output 1 ("later bug fix on same files") and matches test files by substring of the file stem.
- Visibility time of a commit = committer date; rebases/force-pushes can distort.
- Output 1 positives are few: bug-fix detection is only message keywords (`fix|bug|hotfix|regression|resolve|patch`) or closing an issue labeled `bug`. Fix detector is crude.
- Output 3 duplicate truth only from "duplicate of #N" text in comments/body (misses `marked_as_duplicate` targets and cross-references).
- Output 2 truth is heuristic (stem match of later test file, or later bug fix on same files).
- `ingest_github` re-fetches everything (no incremental); click took ~7 min.
- Replay re-materializes a snapshot per T and per queried issue; fine for click, will be slow on big repos.

## Results so far (pallets/click, T every 60 days 2021-01 to 2025-06, window 180d, lookback 30d, flag top 20%)
Averaged over replay points that have at least one positive (rule changed 2026-11-04; earlier numbers counted empty points as 0 and were understated: 0.01/0.083, 0.16/0.29).
| | random | most-changed-files |
|---|---|---|
| Out1 F1 (10 positives, 6 points) | 0.042 | 0.333 |
| Out2 F1 (72 positives, 15 points) | 0.256 | 0.464 |
| Out3 hit@5 (15 dup issues, 10 points) | 0.0 | 0.167 |

Tiny sample sizes; treat as smoke test, not a benchmark. The proposal's Table 2 still shows the old averaging and should be updated.
