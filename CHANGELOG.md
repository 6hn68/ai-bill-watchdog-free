# Changelog

## firewall 0.1.0 (2026-11-26)
- New `firewall.py`: a local proxy that stops spend before a request is charged. Point `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` at `http://127.0.0.1:8787`.
  - Daily, session and single-request dollar caps, checked against a conservative worst-case estimate before forwarding. Daily spend survives a restart (read back from the ledger).
  - Loop breaker (same newest message repeated) and burst breaker (dollars per time window).
  - Blocked requests get HTTP 402 with a plain reason in the provider's own error shape. 402 because SDKs auto-retry 429.
  - Streaming passes through unchanged; real usage is read from the stream or body to settle the cost. Unknown models are priced at the highest known rate, never $0.
  - API keys pass straight through and are never stored; prompts are never logged. The ledger keeps time, model, tokens, dollars and the decision.
  - `firewall.py reconcile --bill billing.csv` compares the bill with every request that went through the firewall; `summary` prints spend and blocks per day.
- 73 tests (13 new).

## 0.6.0 (2026-11-25)
- `--dry-run`: print one line of totals and exit. Reads usage numbers only, writes no files, reads no key. Write options (`--report`, `--csv`, `--dispute`, `--export-letter`) are skipped when it is set.
- `--export-letter FILE` (with `--bill`): write only the dispute draft message to a file, so you can edit it and send it yourself. It promises no refund. If nothing in the files points to a billing problem, no file is written (exit 1).
- 60 tests.

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
