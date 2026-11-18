# Demo script (about 10 minutes)

## Before the demo (once, needs internet)
PowerShell, from the repo folder:
```
.venv\Scripts\Activate.ps1
winget install GitHub.cli; gh auth login        # or: $env:GITHUB_TOKEN = "<token>"
tm ingest pallets/click                         # ~7 min the first time (GitHub issues/events/comments)
$env:ANTHROPIC_API_KEY = "<key>"                # optional: live Claude; without it claude_insight is a dry run
tm demo pallets/click --from 2021-01-01 --to 2025-06-01 --token-budget 300000
```
The live run fills `data/pallets__click/llm_cache.jsonl`. In the demo, the same command replays from that cache: **0 tokens, no network needed for Claude**, same results.

## During the demo
1. **The problem** (PROJECT.pdf): an insight system that learns from a repo's history must be tested without seeing the future. Our time machine replays history and checks each warning against what really happened in the next 6 months.
2. **Run** `tm demo pallets/click --from 2021-01-01 --to 2025-06-01` and talk over the live timeline:
   - each line is one moment T; "+N events" is all the sandbox receives (incremental, never a rebuild);
   - flagged commits show "BUG, fixed <date> (+d)" when a later fix was traced back to them (SZZ), with Claude's one-line reason;
   - issue lines show whether a new issue's real duplicate was in the top 5.
3. **Results table**: random vs most-changed-files vs KB heuristic vs Claude.
4. **Sandbox**: the leak probe at the bottom. The system tried, on purpose, to read the full database, list the data folder, run git on the clone, reach GitHub, and diff a future commit. All 7 attempts were blocked. Point out that the oracle code is not even present inside the sandbox (`pkg/timemachine/` in the printed sandbox dir).
5. **Incremental**: "each sandbox received 1,731 events once; a rebuild per checkpoint would copy 40,271 rows (23x)".
6. **Tokens**: prefilter, batching, diff compaction, cached prompts, BM25 gating, response cache. Show the numbers (~91% fewer prompt tokens than one request per commit with full diffs).
7. Open `data/pallets__click/demo_report.html` for the same story as a page (timeline is clickable per step).

## If something fails
- No API key: say so; claude_insight runs as a dry run (equals the KB heuristic) and the token panel still shows what would be sent.
- No GitHub data: Output 3 shows n/a; Outputs 1 and 2 work from git alone.
- Backup: run `tm demo ... --quiet` (no timeline, ~1.5 min) or open a report generated beforehand.
