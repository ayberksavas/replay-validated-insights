# Current state (as of 2026-11-11)

## Done
- Timestamped SQLite store (commits, per-file changes, issues/PRs, issue events, comments).
- Ingestion: git (`git log --numstat`) and GitHub (issues, repo-wide issue events, issue + review comments) via `gh` or, without `gh`, the REST API with `GITHUB_TOKEN`. **Incremental**: re-running `tm ingest` fetches the clone and only GitHub data updated since the last sync (`meta` table keeps the sync time).
- `TimeView`: leak-proof snapshot at T (rebuild mode).
- **Incremental time machine** (`timeline.py`, `kb/events.py`): the store becomes one time-ordered event list; each step releases only the events in (previous T, T]. The KB (`kb/base.py`) updates from new events instead of being rebuilt, including late facts (a `bug` label that arrives after the commit that closed the issue turns that commit into a fix).
- **Sandbox** (`sandbox/`): each predictor runs in its own `python -I` process with only the predictor-side modules copied in (no oracle, evaluator, TimeView or ingest code), its own store fed with events <= T, an audit-hook jail (no files outside its dir, no network, no subprocesses, no ctypes), a strictly read-only SQL connection, and host-checked `diff()`/LLM requests. Optional: a git repo with the code as of T, updated incrementally (`snapshot.IncrementalSnapshot`). A leak probe tries 7 ways to read the future; all are blocked.
- `Oracle`: ground truth for Outputs 1 (SZZ), 2, 3.
- Evaluation: rebuild loop (`replay`) and incremental sandboxed loop (`replay_incremental`, the default). Tests show both give identical metrics. Precision/recall/F1, lead time, hit@k. Replay points with no positives are skipped (not scored 0).
- **Claude predictor** (`insight.py`, `ClaudeInsight`), token-optimized: heuristic prefilter, one batched request per step for Outputs 1+2, compact packets (short ids, numbers from the KB, diffs cut to changed code lines), cached system prompts, BM25-gated issue reranking, host-side response cache and token budget (`llm.py`). With no API key it runs as a dry run and equals `kb_heuristic`.
- Output 3 queries have "duplicate of #N" removed (the stored body is the latest edit and the oracle reads its answer from that text). `--keep-dup-markers` restores the old behaviour.
- `tm demo`: live timeline + HTML report (results, token use, sandbox, leak probe, per-step flags).
- **Output 8, next code change** (`nextchange.py`, extension beyond PROJECT.pdf; see `docs/NEXT_CHANGE.md`): tasks mined from history (an issue a commit closed, or the PR it came from); at T = just before the commit each sandbox gets only the task number and the code as of T, predicts the files to change and (Claude) the patch; scored by hit@1/hit@3/recall@5/MRR and changed-line F1 against the real commit. `tm nextchange`.
- Tests: 34 (incremental KB == rebuilt KB; sandbox store == TimeView at every T; incremental replay == rebuild replay for all baselines; leak probe; snapshot has no future objects; LLM gateway caching/budget/request shape with a fake client; GitHub ingest full + incremental with canned pages).

## Not done
- **Live Claude run**: everything LLM-side is tested with a fake client only. Needs `ANTHROPIC_API_KEY`.
- **click GitHub data on this machine**: needs `gh auth login` or `GITHUB_TOKEN` (anonymous REST is 60 requests/hour). Until then Output 3 is n/a in `tm demo`.
- Container runner for the sandbox (Docker with `--network none`, only the sandbox dir mounted). The worker protocol is ready for it; required before giving a shell-capable agent (Claude Code) the sandbox.
- Output 4 (similar bug from diffs), 5, 7, 6 / Input 5 (CI data).
- PR-to-commit mapping; repo selection incl. periods after the model's training cutoff; random baseline over several seeds; confidence intervals.

## Known gaps / caveats
- **Edited text leaks**: GitHub returns only the latest issue/PR title and body (and comment text). Dup markers are redacted for Output 3; other edits are still visible.
- The audit-hook jail stops Python-level access, not native code. Our predictors are fine; untrusted agents need the container.
- Visibility time of a commit = committer date; rebases/force-pushes can distort (also for the code snapshot, which picks the last first-parent commit with committer date <= T).
- Output 3 truth can point at an issue newer than the query; `marked_as_duplicate` events are unused.
- Output 1 positives are few (keyword fix detection); Output 2 truth is heuristic.

## Results (pallets/click, git data only, T every 60 days 2021-01 to 2025-06, window 180d, lookback 30d, top 20%)
From `tm demo pallets/click --from 2021-01-01 --to 2025-06-01 --llm dry-run` (incremental, sandboxed):
| | random | most-changed-files | kb_heuristic | claude_insight (dry run) |
|---|---|---|---|---|
| Out1 F1 (10 positives, 6 points) | 0.083 | 0.333 | 0.444 | 0.444 |
| Out2 F1 (72 positives, 15 points) | 0.344 | 0.464 | 0.481 | 0.481 |
| Out3 hit@5 | needs GitHub data | | | |

Random differs from the 2026-11-04 table because its random stream is no longer shared with Output 3. Dry-run claude_insight is by design identical to kb_heuristic; a live run needs an API key.
Incremental run: 27 checkpoints, each sandbox received 1,731 events once vs 40,271 rows a per-checkpoint rebuild copies (23x); ~90 s for 4 predictors.
Token design (estimates, 4 chars/token): 257 candidates -> 154 sent, 23 requests instead of 257, diffs 1.7 MB -> 65 KB, prompts ~36k tokens vs ~398k naive (-91%).
