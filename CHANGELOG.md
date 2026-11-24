# Changelog

## 0.5.0 (2026-11-24)
- New "Cache & context" block in the text, Markdown, HTML and JSON reports (`cache`, `first_turns`, `big_outputs`):
  - cache hit rate (cache reads ÷ input-side tokens) for the last 7 days and the whole range, plus what was paid above the cache-read price (an upper bound on what caching could save);
  - each session's first-request context in tokens and dollars (median, largest, total);
  - tool results over 50 KB, with an estimated re-sending cost (bytes ÷ 4 tokens × later requests × cache-read price; a floor).
- README: one-line quick start.
- 56 tests.

## 0.4.0 (2026-11-23)
- `--dispute FILE` (with `--bill`): dispute pack in Markdown or HTML. Day-by-day bill vs logs, same-day multiple charges (with times as the bill writes them), per-day log evidence (requests, sessions, first/last time, top model), and a fact-only English draft message. It does not promise a refund; you send it yourself.
- Example pack built from the amounts in a public issue: `examples/dispute-demo/`.
- The browser version shows the same pack and lets you download it or copy the draft (parity tests: the pack text is byte-identical in 3 time zones).
- 45 tests.

## 0.3.0 (2026-11-23)
- `--since` / `--until YYYY-MM-DD` date range (works together with `--days`).
- `--period week|month`: totals by week (starting Monday) or by month.
- `--breakdown`: cost, share and requests by model and by project.
- `--blocks`: 5-hour usage windows, with the most expensive window.
- Month pace: month-to-date spend and projected month total in every report; `--budget USD` exits with code 2 when the month is on pace to go over.
- JSON output adds `weeks`, `months`, `by_model`, `by_project`, `blocks`, `pace`. Markdown/HTML reports add cost by model and by project.
- The browser version computes the same numbers (parity tests in 3 time zones).
- 37 tests.

## 0.2.1 (2026-11-23)
- Bill check picks the money column (net amount / cost / amount / usd / total) and never a token column, so Cursor-style exports with "Total Tokens" read correctly.
- Browser version with the same engine: https://abwd.vercel.app/app (runs in the tab, nothing uploaded).
- 26 tests.

## 0.2.0 (2026-11-23)
- Top 10 most expensive sessions (cost, share, model, project).
- Loop detection: same tool call with the same input 3+ times in a row, with failures counted and the cost of retries after the 3rd try.
- "What to change" list; every saving labelled measured / modelled upper bound / not quantified.
- One-page report: `--report report.html` or `.md`.
- Bill check: `--bill file.csv` compares your bill with what the logs explain, per day.
- Multi-line API responses (same message id) are counted once.
- Haiku 5.5 long-context prices applied above 100k prompt tokens.
- English output; MIT license; 25 tests.

## 0.1.0
- Daily usage and API-equivalent cost from Claude Code logs.
