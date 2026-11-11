# How to use

Setup: Python >= 3.11, git. For GitHub data: the `gh` CLI logged in (`gh auth login`) or `GITHUB_TOKEN` set. For live Claude calls: `ANTHROPIC_API_KEY`.
```
python -m venv .venv && .venv/Scripts/activate        # Windows; source .venv/bin/activate elsewhere
pip install -e ".[llm,dev]"
tm ingest pallets/click                     # clone + git + GitHub; re-run any time: only new data is fetched
tm demo pallets/click --from 2021-01-01 --to 2025-06-01            # live timeline + data/pallets__click/demo_report.html
tm replay pallets/click --from 2021-01-01 --to 2025-06-01 --every-days 60   # JSON metrics (baselines)
python -m pytest tests
```
Useful flags (`replay` and `demo`):
- `--predictors random,most_changed_files,kb_heuristic,claude_insight` (demo default includes `claude_insight`).
- `--llm auto|live|dry-run`: `auto` calls Claude only when credentials exist. `dry-run` makes no calls and falls back to the KB heuristic, but still reports what would be sent.
- `--model claude-opus-5-5` (default), `--token-budget 300000` (Claude stops being called after this; the run continues on the heuristic).
- Responses are cached in `data/<slug>/llm_cache.jsonl`; re-running the same replay costs 0 tokens.
- `--mode rebuild` (replay only): the old in-process loop, for comparison.
- `--keep-dup-markers`: do not redact "duplicate of #N" from Output 3 queries.

## Writing a predictor
Subclass `Predictor` (`timemachine/predictor.py`) and use ONLY the `view` argument (`commits`, `files`, `issues`, `issue_state`, `comments`, `diff`, `file_at`, `db`; in a sandbox also `view.kb`, the incremental KB, and `view.llm(...)`). Register it in `sandbox/worker.py:PREDICTORS` and add its module to `sandbox/host.py:SANDBOX_MODULES` (only listed modules exist in the sandbox; it must not import host modules or third-party packages that need the network).

## Giving an agent (Claude Code) the code as of T
Never `git checkout <old sha>` in the ingested clone: `.git` still holds the future. Use
```
tm snapshot owner/repo --at YYYY-MM-DD --out ../snap_T [--history]
```
or, during a replay, the sandbox's `code/` folder (kept at T by `IncrementalSnapshot`). The agent must also not reach the network or the `data/` folder: run it in a container that mounts only the sandbox.

## Gotchas
- `tm ingest` on an issue-heavy repo takes minutes the first time (click ~7 min); later runs are incremental. `--full` re-fetches everything, `--no-github` skips GitHub.
- Without `gh` or a token, GitHub allows 60 requests/hour per IP: enough for a tiny repo, not for click.
- Run `tm` from the project root (`data/` is relative) or pass `--workdir`.
- `data/` holds contributor names and comment text: do not publish it.
- Sandboxes are created under the system temp dir (`tm_sandbox_<predictor>_*`); each run makes new ones. Delete old ones when you like.
