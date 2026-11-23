---
name: bill-check
description: Show where your Claude Code money went (Tollen, formerly AI Bill Watchdog, runs locally, uploads nothing)
disable-model-invocation: true
allowed-tools: Bash(python3 ${CLAUDE_SKILL_DIR}/watchdog.py *)
---
!`python3 ${CLAUDE_SKILL_DIR}/watchdog.py --days 30 --report ~/ai-bill-report.html`

Above is the output of Tollen for the last 30 days of this machine's Claude Code logs. Reply in at most 8 short lines: the total, the most expensive day, the biggest retry loop if any, and the top fix with its dollar figure, copied from the output without changing any number. End with: "Full one-page report: ~/ai-bill-report.html". Do not run any other command.
