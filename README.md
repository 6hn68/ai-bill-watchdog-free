# AI Bill Watchdog (free)

**Find out where your Claude Code money went, and what to change.** One Python file, runs on your machine, uploads nothing.

```bash
curl -LO https://raw.githubusercontent.com/6hn68/ai-bill-watchdog-free/main/watchdog.py
curl -LO https://raw.githubusercontent.com/6hn68/ai-bill-watchdog-free/main/prices.json
python3 watchdog.py
```

That's it. No `pip install` and no Node, account or API key. Python 3.8+ is already on macOS and most Linux machines.

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
python3 watchdog.py --report report.html      # one-page report (.html or .md)
python3 watchdog.py --csv usage               # usage-daily.csv + usage-sessions.csv
python3 watchdog.py --bill console-export.csv # compare logs with your bill, day by day
python3 watchdog.py --json                    # machine-readable output
python3 watchdog.py --top 20                  # list more sessions
python3 watchdog.py --logs /other/path        # a different log folder
```

**Bill check** (`--bill`) takes any CSV that has a date column and a cost or amount column, such as a usage export from your provider's console. It prints, day by day, the bill minus what your logs explain. A big gap means usage the logs don't show, for example another machine, another tool or a key you forgot about.

## Privacy

- It reads local files only and makes no network calls. The test suite fails if `watchdog.py` ever imports `urllib`, `socket`, `http.client`, `ssl` or `requests`.
- It never reads API keys or passwords, and never copies your prompts into reports. Loop entries show the tool name and the first 50 characters of its input.
- It's a single file of about 750 lines. You can read all of it before you run it.

## Honest limits

- **API-equivalent, not your invoice.** Dollar figures are tokens × list price. On a Pro or Max subscription you pay a flat fee, so read the dollars as "how much usage", not "what you were charged".
- **Claude Code only.** Cursor, Copilot and Codex logs aren't read (yet).
- **Prices live in `prices.json`**, copied from Anthropic's official pricing page on 2026-11-22. The tool warns when the file is more than 7 days old. Unknown models are listed as *unpriced*, never counted as $0.
- **Loops are found after the fact.** The tool reports them but doesn't stop a running session. To stop one live, use a hook or `--max-turns`.
- If an API response is written as several log lines with the same message id, its usage is counted once.

## Related tools

If you want dashboards, menu-bar widgets or many tools at once, look at [ccusage](https://github.com/ccusage/ccusage) and [CodeBurn](https://github.com/getagentseal/codeburn). Both are excellent, and both need Node. This project is deliberately small: one readable Python file that gives you a short list of what to change, with each saving labelled measured or modelled.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The 25 tests run on generated logs. Every expected dollar figure is worked out by hand from list prices.

## License

MIT, see [LICENSE](LICENSE).
