# How to use

Setup: Python >= 3.11, git, `gh` CLI authenticated (`gh auth login`).
```
pip install -e . pytest
tm ingest owner/repo [--no-github]       # clones to data/owner__repo/repo, writes tm.db (slow: GitHub API)
tm replay owner/repo --from YYYY-MM-DD --to YYYY-MM-DD --every-days 60 --window-days 180
python -m pytest tests
```
`tm replay` currently runs only the two baselines.

## Writing the real system
Subclass `Predictor` (`timemachine/predictor.py`), implement what you support, and use ONLY the `view` argument:
```python
class MyPredictor(Predictor):
    name = "claude_kb"
    def risky_commits(self, view, candidates):
        # view.diff(sha), view.files(sha), view.issues(), view.issue_state(n), view.comments(n)
        return [Flag(c["sha"], score) for c in candidates]
```
Then call `evaluate.replay(db, repo, MyPredictor(), times)` and register it in `cli.py`. Scores rank candidates; the evaluator flags the top `top_frac`.

Knowledge-base/agent code must read data through `TimeView`; building the KB per T from the view is the safest way to keep the leakage guarantee.

## Giving an agent (Claude Code) the code as of T
Never `git checkout <old sha>` in the ingested clone and point the agent at it: `.git` still holds the future (`git log --all`, `origin/main`, tags, reflog). Export a snapshot instead:
```
tm snapshot owner/repo --at YYYY-MM-DD --out ../snap_T            # one commit, tree as of T, no history
tm snapshot owner/repo --at YYYY-MM-DD --out ../snap_T --history  # real commits up to T, nothing after
```
T picks the last first-parent commit on the default branch with committer time <= T. The result has no remote, no tags, no reflog and no objects after T. `--out` must be outside the clone and empty.

The snapshot only closes the git leak. The agent must also not reach the network (GitHub, PyPI: `pip install <the package>` fetches the latest source), the full clone under `data/`, or `tm.db`. Run it in a container that mounts only the snapshot, with dependencies pinned to T and outbound traffic limited to the model API.

## Gotchas
- `tm ingest` can take minutes on issue-heavy repos; re-running re-fetches everything.
- The package imports `Oracle` only in `evaluate.py`. Keep it out of predictor code.
- `data/` is large and regenerable; do not commit it.
