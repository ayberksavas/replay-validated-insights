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

## Results: python-on-whales (dry run, no API key)
`tm nextchange gabrieldemarmiesse/python-on-whales --from 2020-08-01 --to 2026-11-01 --kinds issue --llm dry-run`

| 61 issue tasks | random | most_changed_files | kb_heuristic |
| --- | --- | --- | --- |
| hit@1 | 0.082 | 0.082 | 0.459 |
| hit@3 | 0.164 | 0.311 | 0.738 |
| recall@5 | 0.167 | 0.349 | 0.659 |
| MRR | 0.138 | 0.251 | 0.606 |

Estimated live cost for these 61 tasks: about 393k prompt tokens in 122 requests (vs about 2.4M if each
shortlist were sent in full), roughly $2.4-5.5 on Claude Opus 5.5 depending on output length.
Patch quality (line F1) needs a live run.

## Not done
- A live Claude run (needs `ANTHROPIC_API_KEY`); the patch path is tested with a fake client only.
- Running the repository's tests on the predicted patch (needs a container per task, e.g. the Docker mode).
- Tasks are biased to commits whose message or PR links an issue; edited issue text is the latest version.
