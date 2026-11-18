# Output 8: predicting the next code change

An extension beyond the outputs in `PROJECT.pdf`: given what developers wrote down *before* a change
(an issue, or a pull-request description), predict where the code will change and write the change.
The time machine checks the prediction against the commit that really landed.

## Task
A task is a real commit C on the main line (first-parent history of the default branch) that changes at
least one code file (not a test or doc file), and whose intent was written before C:
- **issue**: an issue C closes: "fixes/closes/resolves #N" in C's message or in the description of the
  pull request C came from (`(#123)` in the subject), or a GitHub `closed` event naming C.
- **pr**: otherwise, the pull request C came from (title + description).

At T = C's time minus one second:
- each sandbox receives only the task's kind and number; it reads the title, description and the
  comments written so far from its own store (so nothing after T);
- the sandbox's `code/` folder is a git repo checked out at the last main-line commit at or before T,
  holding no objects after T. The host also checks that this commit is an ancestor of C and is not C;
- the predictor returns ranked file paths and, optionally, a patch.

## Ground truth and metrics (`nextchange.py`, host only)
| Metric | Meaning |
| --- | --- |
| hit@1, hit@3 | a file C really modified is the first guess / in the top 3 |
| recall@5 | share of C's code files found in the top 5 |
| MRR | 1 / rank of the first correct file |
| line precision / recall / F1 | changed lines of the predicted patch vs C's changed lines (whitespace-normalized, blank and bracket-only lines ignored); only for predictors that write patches |

New files created by C cannot be ranked (they do not exist at T) but still count in recall.

## Predictors
| Predictor | How it predicts |
| --- | --- |
| random | random code files at T |
| most_changed_files | code files changed most often so far, ignoring the task |
| kb_heuristic | BM25 from the task's words to the code at T (paths weigh 3x), a file named in the task first, earlier bug fixes as a tie-breaker |
| claude_insight | the heuristic's top 8 files go to Claude with only path, size and defined names; Claude picks up to 3 and says where. Then its top 2 files (whole if <= 300 lines, else the parts around the named functions) go to Claude, which writes search/replace edits. The sandbox applies them and returns the diff |

Token levers: shortlist from BM25 (Claude never sees the whole repo), outline-only localization, patch
request with 1-2 files or excerpts, JSON-schema output, cached system prompts, response cache, budget.
Effort `low` for localization, `medium` for the patch. Without an API key Claude is not called and
`claude_insight` returns the heuristic's ranking (no patch).

## Results: python-on-whales, 61 issue tasks, live Claude Haiku 5.5 (2026-11-11)
`tm nextchange gabrieldemarmiesse/python-on-whales --from 2020-08-01 --to 2026-11-01 --kinds issue --llm live --model claude-haiku-5-5 --token-budget 1500000`

| 61 issue tasks | random | most_changed_files | kb_heuristic | claude_insight (Haiku 5.5) |
| --- | --- | --- | --- | --- |
| hit@1 | 0.082 | 0.082 | 0.459 | **0.738** |
| hit@3 | 0.164 | 0.311 | 0.738 | **0.836** |
| recall@5 | 0.167 | 0.349 | 0.659 | **0.740** |
| MRR | 0.138 | 0.251 | 0.606 | **0.786** |
| patch changed-line F1 | | | | 0.385 (47 tasks with a patch) |

- Claude got 19 first guesses right that the heuristic missed, and lost 2 the heuristic had right.
- 92 search/replace edits applied, 12 dropped (their `find` text did not match the file exactly once).
- Several patches equal the real fix line for line, e.g. issue #78 (`volumes_from: Optional[str]` to
  `Optional[List[str]]`), #175 (`extra_hosts` type), #216 (`entrypoint` field).
- Real usage: 116 API calls (+6 answered from the response cache), 348k input tokens, 90k cache-read
  tokens, 64k output tokens: **$0.07**, 517 s. The same tokens on Opus 5.5 would cost about $2.7 before
  any extra thinking.
- Per-task view with Claude's patch next to the real diff: `data/<owner>__<repo>/nextchange_report.html`
  (`--html` to choose the path).

`--kinds pr` (every main-line commit with a linked PR or issue, described by its PR; 397 mined, 2 skipped
because the snapshot at T was not an ancestor of the commit):

| 395 PR tasks | random | most_changed_files | kb_heuristic |
| --- | --- | --- | --- |
| hit@1 | 0.033 | 0.109 | 0.435 |
| hit@3 | 0.104 | 0.342 | 0.691 |
| recall@5 | 0.100 | 0.363 | 0.636 |
| MRR | 0.088 | 0.268 | 0.580 |

The PR tasks have not been run live. Dry-run estimate: about 2.1M prompt tokens (vs 13.3M naive); real
input tokens came out about 2x the 4-chars-per-token estimate on the issue run, so expect ~$1-2 on
Haiku 5.5 and far more on Opus 5.5. Use `--limit` and `--token-budget`.

## Not done
- A live run on Opus 5.5, and on the 395 PR tasks.
- Running the repository's tests on the predicted patch (needs a container per task, e.g. the Docker mode).
- Tasks are biased to commits whose message or PR links an issue; edited issue text is the latest version.
