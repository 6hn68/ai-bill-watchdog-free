# Dispute pack demo

`bill.csv` holds the five "Auto-recharge" amounts from a public report:
[anthropics/claude-code#110959](https://github.com/anthropics/claude-code/issues/110959), opened 2026-11-18.
The issue reports five invoices totaling $4,349.73 within 26 minutes (6:48 to 7:14 AM ET).

Only the amounts come from the issue. The issue gives the first and last times but not each charge's time,
so the CSV has dates only. No logs are used, so the pack has no bill-vs-logs comparison.

Rebuild:

```bash
mkdir -p /tmp/empty && python3 watchdog.py --logs /tmp/empty --bill examples/dispute-demo/bill.csv --dispute examples/dispute-demo/dispute-pack.md
```
