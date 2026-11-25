# Tollen (free), formerly AI Bill Watchdog

[![GitHub stars](https://img.shields.io/github/stars/6hn68/ai-bill-watchdog-free)](https://github.com/6hn68/ai-bill-watchdog-free/stargazers) [Star history](https://star-history.com/#6hn68/ai-bill-watchdog-free&Date)

**For developers who pay cash for API usage: reconcile your API bill against your own logs, spot overcharges and auto-recharges, and build dispute evidence you can send yourself.** Free, one Python file, runs locally, uploads nothing.

- **Reconcile your bill, day by day.** `python3 watchdog.py --bill billing.csv` compares your bill CSV with what your logs explain. A big gap means usage your logs don't show. Usually that's another machine, another tool, or a key you forgot about.
- **Turn a gap into dispute evidence.** `python3 watchdog.py --bill billing.csv --dispute pack.md` writes a pack you can attach to a billing review request: bill vs. logs day by day, same-day multiple charges flagged, and what your logs show for each flagged day, plus a short draft message. It states facts from your own files only. It does not promise a refund or credit, nobody sends it for you, and no one takes a cut.
- **Local only, no sign-up.** No `pip install`, no Node, no account, no API key. It reads local files only and makes no network calls. It never reads API keys or passwords, and never copies your prompts into reports.

Example: the demo in `examples/dispute-demo/` rebuilds the five auto-recharge invoices reported publicly in anthropics/claude-code#110959 (2026-11-18, five charges totaling $4,349.73).

## Quick start (one line)

```bash
curl -fsSLO 'https://raw.githubusercontent.com/6hn68/ai-bill-watchdog-free/main/{watchdog.py,prices.json}' && python3 watchdog.py
```

Two files, one command, no install. Already in Claude Code? `claude plugin marketplace add 6hn68/ai-bill-watchdog-free && claude plugin install ai-bill-watchdog@tollen`, then type `/bill-check`.

## Try it

```bash
curl -LO https://raw.githubusercontent.com/6hn68/ai-bill-watchdog-free/main/watchdog.py
curl -LO https://raw.githubusercontent.com/6hn68/ai-bill-watchdog-free/main/prices.json
python3 watchdog.py
```

That's it. Python 3.8+ is already on macOS and most Linux machines. By default it reads the session logs Claude Code already writes to `~/.claude/projects/` and writes `report.html`.

No download? Try the free in-browser check-up: https://abwd.vercel.app/app

> Honest limit: logs are read from Claude Code only, for now. The bill CSV for `--bill` can come from any provider, including OpenAI. Dollar figures from logs are API-equivalent (tokens × list price), not your invoice.

## Easiest ways to run it

No folder hunting. Each line downloads `watchdog.py` and `prices.json` from this repo, reads the default log folder (`~/.claude/projects`), writes `report.html` and opens it in your browser. Nothing is uploaded. [Read the source first](https://github.com/6hn68/ai-bill-watchdog-free/blob/main/watchdog.py).

macOS / Linux:

```sh
mkdir -p ~/ai-bill-watchdog && cd ~/ai-bill-watchdog && curl -fsSLO https://raw.githubusercontent.com/6hn68/ai-bill-watchdog-free/main/watchdog.py && curl -fsSLO https://raw.githubusercontent.com/6hn68/ai-bill-watchdog-free/main/prices.json && python3 watchdog.py --report report.html && (open report.html 2>/dev/null || xdg-open report.html 2>/dev/null || echo "Open $PWD/report.html")
```

Windows PowerShell:

```powershell
$u="https://raw.githubusercontent.com/6hn68/ai-bill-watchdog-free/main"; $d=Join-Path $HOME "ai-bill-watchdog"; New-Item -ItemType Directory -Force $d | Out-Null; Set-Location $d; iwr "$u/watchdog.py" -OutFile watchdog.py -UseBasicParsing; iwr "$u/prices.json" -OutFile prices.json -UseBasicParsing; $py=(Get-Command py,python,python3 -ErrorAction SilentlyContinue | Select-Object -First 1).Name; & $py watchdog.py --report report.html; if ($LASTEXITCODE -le 2) { Start-Process report.html }
```

Inside Claude Code: install the `/bill-check` skill once, then type `/bill-check` in any session.

```sh
mkdir -p ~/.claude/skills/bill-check && cd ~/.claude/skills/bill-check && curl -fsSLO https://raw.githubusercontent.com/6hn68/ai-bill-watchdog-free/main/watchdog.py && curl -fsSLO https://raw.githubusercontent.com/6hn68/ai-bill-watchdog-free/main/prices.json && curl -fsSLO https://raw.githubusercontent.com/6hn68/ai-bill-watchdog-free/main/claude-skill/bill-check/SKILL.md
```

The skill runs the script locally ([skill file](claude-skill/bill-check/SKILL.md)), writes `~/ai-bill-report.html`, and asks Claude for an 8-line summary, which uses a few tokens. Prefer no Claude at all? Use the one-liners above, or the in-browser check-up at https://abwd.vercel.app/app.

Install it as a Claude Code plugin instead, and it updates with the repo:

```sh
claude plugin marketplace add 6hn68/ai-bill-watchdog-free
claude plugin install ai-bill-watchdog@tollen
```

That installs the `/bill-check` skill above. (One command also works on recent Claude Code: `claude plugin install ai-bill-watchdog --marketplace 6hn68/ai-bill-watchdog-free`.)

## Cache & context (v0.5.0)

Every report now has a **Cache & context** block, worked out from your own logs:

- **Cache hit rate** = cache-read tokens ÷ all input-side tokens (input + cache reads + cache writes), for the last 7 days and the whole range. Next to it: what you paid above the cache-read price on the rest. That is the most better caching could save; the first write of any prompt can't be avoided.
- **First-request context**: input-side tokens of each session's first API request (system prompt, tools, memory files, first message), with the median, the largest and what those first requests cost.
- **Big tool outputs**: any single tool result over 50 KB. It is re-sent with every later request in its session; the estimate uses bytes ÷ 4 as tokens at the cache-read price, so it's a floor, not an exact figure.

## What you get

It reads the session logs Claude Code already writes to `~/.claude/projects/` and answers three questions:

1. **Which sessions cost the most?** You get the top 10 sessions with cost, share of total, model and project.
2. **Did an agent get stuck?** It flags runs of the same tool call with the same input, 3 or more times in a row, counts how many failed, and works out what the retries after the 3rd try cost you.
3. **What exactly should I change?** You get a short list of fixes. Each one has a dollar figure and says how that figure was worked out:
   - **measured**: added up from your own logs (for example, money spent on retries after the 3rd try).
   - **modelled upper bound**: a what-if, such as "premium-model turns priced at Sonnet". It is only true for turns a cheaper model can actually handle.
   - **not quantified**: worth doing, but we won't guess a number.

Real output on the demo logs in `examples/demo-logs`:

```
Total: $3.20 API-equivalent over 5 day(s), 2026-11-17 to 2026-11-21, 5 sessions.
Most expensive day: 2026-11-17, $1.80

TOP 5 SESSIONS
  #  cost       share  msgs  started           model / project
  1  $1.80      56.3%     2  2026-11-17 12:00  claude-sonnet-4-5-20250929 / proj-a
  2  $1.26      39.3%    11  2026-11-18 12:00  claude-opus-4-1-20250805 / proj-a
  ...

LOOPS (same tool call, same input, 3+ times in a row)
  Bash {"command": "npm test"} x10 (10 failed)  cost $0.43, wasted after 3 tries $0.30

WHAT TO CHANGE
  1. Stop retry loops after 3 tries  -> saves $0.30 (measured)
     Do:  Add this line to CLAUDE.md: "If the same command fails twice,
          stop and tell me instead of retrying." For headless runs also pass --max-turns.
  2. Use claude-sonnet-4-6 for routine turns  -> saves $0.66 (modelled upper bound ...)
  3. Clear context between tasks  -> saves n/a (not quantified)
```

You can see the full text in [`examples/sample-output.txt`](examples/sample-output.txt) and the one-page report in [`examples/sample-report.md`](examples/sample-report.md). Try it yourself with `python3 watchdog.py --logs examples/demo-logs`.

## Options

```bash
python3 watchdog.py --days 30                 # only the last 30 days
python3 watchdog.py --since 2026-11-01 --until 2026-11-15   # a date range
python3 watchdog.py --period week             # also totals by week (or: month)
python3 watchdog.py --breakdown               # cost by model and by project
python3 watchdog.py --blocks                  # 5-hour usage windows
python3 watchdog.py --budget 200              # exit code 2 if this month is on pace to go over $200
python3 watchdog.py --report report.html      # one-page report (.html or .md)
python3 watchdog.py --csv usage               # usage-daily.csv + usage-sessions.csv
python3 watchdog.py --bill console-export.csv # compare logs with your bill, day by day
python3 watchdog.py --bill billing.csv --dispute pack.md   # dispute pack (.md or .html)
python3 watchdog.py --bill billing.csv --export-letter letter.txt  # just the draft message, edit and send it yourself
python3 watchdog.py --dry-run                  # one line of totals, writes nothing, reads no key
python3 watchdog.py --json                    # machine-readable output
python3 watchdog.py --format ccusage         # ccusage-compatible JSON (daily + totals)
python3 watchdog.py --top 20                  # list more sessions
python3 watchdog.py --logs /other/path        # a different log folder
```

**Bill check** (`--bill`) takes any CSV that has a date column and a cost or amount column, such as a usage export from your provider's console. It prints, day by day, the bill minus what your logs explain. A big gap means usage the logs don't show, for example another machine, another tool or a key you forgot about.

**Dispute pack** (`--dispute FILE`, needs `--bill`) writes a file you can attach to a billing review request. It holds:

- day by day, your bill next to what your logs explain, with the days where the bill is higher marked;
- same-day multiple charges: 2 or more charge rows (recharge, top-up, credit purchase, invoice, payment) on one date, with the times exactly as your bill writes them;
- for each marked day, what your logs show: requests, sessions, first and last request time, top model;
- a short English draft message that states these facts and asks for a review.

The pack only states facts from your own files. It does not promise a refund or credit, nobody sends it for you, and no one takes a cut. You read it, edit it, and decide whether to send it yourself.

**Export letter** (`--export-letter FILE`, needs `--bill`) writes only that draft message to a file, so you can edit it and send it from your own account. If nothing in the files points to a billing problem, no file is written. **Dry run** (`--dry-run`) prints one line of totals and exits: it reads usage numbers only, writes no files, and never reads a key — combine it with `--report`, `--csv`, `--dispute` or `--export-letter` and those writes are skipped.

This tool and the pack stay free and MIT-licensed. For three kinds of case there is an optional recovery service at [abwd.vercel.app](https://abwd.vercel.app/#pricing): a card or session used to buy without you, a duplicate charge, or a stolen key with $500+ lost (or a provider incident with an official payout). We draft the appeal and every follow-up until a final answer, you send them from your own account. About 1 in 5 public cases with a known outcome got money back, so most get nothing. The fee is 25% of what comes back, only if it is $100 or more. Nothing back, $0. Anything else gets the free stop and the templates, no case and no fee. Nothing is paid up front, and no password or API key is ever asked for.

Which rows count as charges: if the CSV has a description-type column (description, type, item, category, ...), a row counts when its amount is above zero and the text mentions a recharge, top-up, purchase, credit, invoice, payment or charge. A CSV with no description and no model column (a plain list of payments) counts every positive row. A CSV with a model column and no description is read as a usage export, so it has no charges, only daily totals.

Example: [`examples/dispute-demo/`](examples/dispute-demo/) rebuilds the five auto-recharge invoices reported publicly in [anthropics/claude-code#110959](https://github.com/anthropics/claude-code/issues/110959) (2026-11-18, five charges totaling $4,349.73). Only the amounts from that issue are used; the issue gives the first and last times, not each charge's time, so the demo bill has no times.

**Month pace** is always shown: spend so far in the month of your latest activity, and a straight-line projection for the whole month. With `--budget` the script exits with code 2 when the projection is over budget, so you can use it in a cron job or CI step.

**5-hour windows** (`--blocks`) group usage the way Claude's usage limits do: a window opens at the hour of the first request and lasts 5 hours.

## Privacy

- It reads local files only and makes no network calls. The test suite fails if `watchdog.py` ever imports `urllib`, `socket`, `http.client`, `ssl` or `requests`.
- It never reads API keys or passwords, and never copies your prompts into reports. Loop entries show the tool name and the first 50 characters of its input.
- It's a single file of about 1,000 lines. You can read all of it before you run it.

## Honest limits

- **API-equivalent, not your invoice.** Dollar figures are tokens × list price. On a Pro or Max subscription you pay a flat fee, so read the dollars as "how much usage", not "what you were charged".
- **Claude Code only.** Cursor, Copilot and Codex logs aren't read (yet).
- **Prices live in `prices.json`**, copied from Anthropic's official pricing page on 2026-11-22. The tool warns when the file is more than 7 days old. Unknown models are listed as *unpriced*, never counted as $0.
- **Loops are found after the fact.** The tool reports them but doesn't stop a running session. To stop one live, use a hook or `--max-turns`.
- If an API response is written as several log lines with the same message id, its usage is counted once.

## Related tools

If you want dashboards, menu-bar widgets or many tools at once, look at [ccusage](https://github.com/ccusage/ccusage) and [CodeBurn](https://github.com/getagentseal/codeburn). Both are excellent, and both need Node. The date-range, week/month, breakdown and 5-hour-window reports here follow the report shapes those tools made popular; the code is written from scratch, nothing is copied. This project is deliberately small: one readable Python file that gives you a short list of what to change, with each saving labelled measured or modelled.

**ccusage-compatible export** (`--format ccusage`) prints the same daily usage as JSON shaped like `ccusage daily --json` (see https://ccusage.com/guide/json-output), so dashboards and scripts that read ccusage output can read this too. Field mapping: `inputTokens` / `outputTokens` / `cacheReadTokens` are copied straight across; `cacheCreationTokens` is our 5-minute plus 1-hour cache writes, with `cacheCreation1hTokens` on a model row only when it has 1-hour writes; `totalCost` is API-equivalent at list price; a model row with no price in `prices.json` carries `"missingPricing": true` and the model is listed in `totals.unpricedModels`. The default text report and `--json` output are unchanged.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The 45 tests run on generated logs. Every expected dollar figure is worked out by hand from list prices.

## License

MIT, see [LICENSE](LICENSE).
