# MoneyMan Development History

This document is a public-safe rendering of the complete Git history preserved in the private MoneyMan repository. It shows every commit reachable from the real branch heads, including dates, original commit subjects, branch tips, and the point where the development lines separate. Commit hashes, author metadata, emails, and private operational details are omitted.

AI assistance was used to extract, verify, and format this graph. The dates and subjects come from the preserved Git objects. The only subject not reproduced verbatim is GitHub's synthetic pull-request merge preview, whose original subject consisted mainly of commit hashes.

## Project Arc

MoneyMan is still an exploratory research project. Its history reflects that. An early Coinbase logger and roadmap remained on a separate prototype branch. The main line later recovered, reworked, and expanded the data pipeline, then moved through progressively stricter gridbot, order-book, fill-model, provenance, and normalization questions. The graph shows research being narrowed and audited rather than a rush toward live trading.

## How to Read the Graph

- `*` is a commit.
- Vertical and diagonal lines show parent relationships.
- A branch name in brackets marks the preserved tip of that branch.
- The graph runs from newest to oldest.
- The graph includes real branch heads only. GitHub-generated pull-request test refs are explained separately because they are not evidence of an actual merge.

## Branch Status

| Branch | Began from | Preserved tip | Result |
|---|---|---|---|
| `main` | 2025-08-03, `Initial commit` | 2026-07-22, `Close collector and normalization audit gaps` | Primary research line with 20 commits and no merge commits. |
| `codex/add-coinbase-logger-roadmap` | 2025-08-03, `Initial commit` | 2026-06-19, `add Coinbase logger and research roadmap` | One branch-only prototype commit plus the shared root. It was not merged into `main`. Later main-line work recovered and substantially expanded the useful pipeline instead. |

## Complete Sanitized Git Graph

```text
* [main] 2026-07-22 | Close collector and normalization audit gaps
*        2026-07-22 | Finish audited collector and normalization foundation
*        2026-07-22 | Validate 500 ms strict L2 latency stress
*        2026-07-22 | Pre-register 500 ms strict L2 latency stress
*        2026-07-22 | Validate strict L2 clock sensitivity
*        2026-07-21 | Validate strict L2 fills on second XRP window
*        2026-07-21 | Add conservative strict L2 grid fills
*        2026-07-21 | Add audited Coinbase L2 book reconstruction
*        2026-07-20 | Add causal EMA reserve-grid entry guard
*        2026-07-12 | Document multi-window reserve grid validation
*        2026-07-12 | Add gridbot lot recovery diagnostics
*        2026-07-11 | Add bounded gridbot overflow experiment
*        2026-07-11 | Add banded reserve gridbot research
*        2026-07-09 | Add Coinbase fee profile for gridbot
*        2026-07-09 | Add first gridbot backtester
*        2026-07-09 | Add external candle fallback tools
*        2026-07-08 | Add cleanup and fallback data tools
*        2026-07-08 | Recover MoneyMan pipeline and data tools
*        2026-07-04 | Add MoneyMan project planning docs
| * [codex/add-coinbase-logger-roadmap] 2026-06-19 | add Coinbase logger and research roadmap
|/
*        2025-08-03 | Initial commit
```

## Pull-Request Metadata

GitHub preserved a pull-request head ref that points to the logger-roadmap branch tip and contributes no additional commit. It also preserved one synthetic test merge of that prototype into the initial base. That synthetic commit is not reachable from either real branch, so it is not presented as a completed merge.

Sanitized synthetic subject:

```text
2026-06-19 | Merge logger-roadmap prototype into initial base (GitHub PR test merge)
```

## Completeness Check

- Commits reachable from `main`: 20
- Commits reachable from the prototype branch: 2, including the shared root
- Unique commits across real branch heads: 21
- Commit entries shown in the graph: 21
- Actual merge commits across real branch heads: 0
- Additional GitHub-generated PR test commits: 1
- Unique commits across every preserved ref: 22

The complete private repository remains the source of truth. This document is a readable history summary, not a replacement Git history.
