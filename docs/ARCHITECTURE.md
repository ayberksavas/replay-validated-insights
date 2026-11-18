# Architecture

```
git repo + GitHub API --ingest (incremental)--> tm.db (every row timestamped)
                                                   |
                                   Timeline: rows -> time-ordered events, cursor T
                                                   |  advance(T): only events in (prev T, T]
          +----------------------------------------+-----------------------------+
          | HOST (time machine)                                                  |
          |   Oracle [full history] ----- scores ---- replay_incremental --> metrics, report
          |   LLM gateway [API key, cache, budget]     |  queries / answers        |
          |   git on the clone [diff only for released commits]                  |
          +--------------------------------------------|-------------------------+
                         JSON lines on stdin/stdout    |
          +--------------------------------------------v-------------------------+
          | SANDBOX process per predictor (python -I, audit-hook jail)           |
          |   store.db (events <= T)  ->  incremental KnowledgeBase  ->  Predictor|
          |   code/ (git history up to T, optional)                              |
          +----------------------------------------------------------------------+
```
Data lives in `data/<owner>__<repo>/{repo,tm.db,llm_cache.jsonl,demo_report.html}` (gitignored).

## Modules (`timemachine/`)
- `store.py`: schema. Every table has `ts` = when the fact became visible. Tables: `commits`, `commit_files` (flags `is_test`, `is_doc`), `issues` (issues and PRs, `is_pr`), `issue_events`, `comments` (`kind` issue|review), `meta` (GitHub sync time).
- `ingest_git.py`, `ingest_github.py`: fill the store. GitHub via `gh` or REST + `GITHUB_TOKEN`; incremental after the first sync.
- `kb/events.py`: the single rule for *when a row becomes visible* (commits at committer time, issue events/comments at max(own time, issue creation)). Shared by the timeline and by `KnowledgeBase(view)`.
- `timeline.py`: host-side event list + cursor; time only moves forward.
- `kb/`: incremental KnowledgeBase (file history, commit/issue links, fix memory incl. late labels, incremental BM25).
- `sandbox/host.py`: starts and feeds a sandbox, serves its `diff`/`file_at` (refuses unreleased commits) and LLM requests, runs the leak probe.
- `sandbox/worker.py`: runs inside the sandbox; `SandboxView` has the same read API as `TimeView`.
- `sandbox/jail.py`: audit hooks (no files outside the sandbox dir, no network, no subprocesses, no ctypes).
- `predictor.py`, `heuristics.py`: interface + baselines (random, most-changed-files, KB heuristic).
- `insight.py`: `ClaudeInsight`, the token-optimized Claude predictor (runs in the sandbox, no network).
- `llm.py`: host-side gateway to the Claude API (prompt caching, structured output, response cache, budget, usage/cost).
- `groundtruth.py`: `Oracle`, full-history labeler. Host only; never copied into a sandbox.
- `evaluate.py`: `replay` (rebuild, in-process) and `replay_incremental` (sandboxed, default).
- `snapshot.py`: `export_snapshot` (one-off) and `IncrementalSnapshot` (code at T inside a sandbox).
- `report.py`, `cli.py`: `tm ingest | replay | demo | snapshot`.

## Leakage guarantee (spec: "enforced in code")
1. **Data**: a sandbox store only ever receives events with visibility time <= T (the worker also rejects any later event). Tested: sandbox store == TimeView(T) at every T.
2. **Code**: only predictor-side modules exist in the sandbox; the oracle, evaluator, TimeView and ingest code are not there.
3. **Git**: the sandbox cannot run git; the host serves diffs only for released commits. The optional code snapshot holds no objects after T.
4. **Process**: `python -I` (no env, no user site), env stripped of tokens/keys, cwd = sandbox dir, audit-hook jail, read-only SQL (SELECT only).
5. **LLM**: the API key stays on the host; the sandbox asks the gateway.
A shell-capable agent should run the same worker in a container (no network, only the sandbox mounted). Adding a data source = add it to `kb/events.py` and `TimeView._copy` with the same visibility rule, plus a test.

## Replay semantics
- Checkpoints: replay points T (Outputs 1/2) and, for each new issue with a known duplicate in (T, T+lookback], the second before it was opened (Output 3). One forward pass in time order.
- Outputs 1/2: candidates = commits in [T - lookback, T]. Each predictor scores them; the top `top_frac` are flagged and compared to the oracle's positives from (T, T+window].
- Output 3: hit if any top-k result is in the issue's later-declared duplicate targets. "duplicate of #N" is removed from the query text.
