# Changelog

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
