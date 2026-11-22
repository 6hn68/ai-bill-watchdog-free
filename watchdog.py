#!/usr/bin/env python3
"""AI Bill Watchdog (free) -- find where your Claude Code money went.

Reads the session logs Claude Code already writes to ~/.claude/projects/
and answers three questions:

  1. Which sessions cost the most?            (Top 10 sessions)
  2. Did an agent get stuck repeating itself?  (failure-loop detection)
  3. What exactly should I change?             (fixes, each with a $ figure)

100% local. No network calls, no upload, no account. Standard library only.

Dollar figures are API-equivalent cost at list price. If you are on a
Pro/Max subscription you pay a flat fee, so treat them as "how much usage",
not "what you were charged".
"""

import argparse
import csv
import hashlib
import html
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timezone

VERSION = "0.2.1"
MTOK = 1_000_000
BUCKETS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")

# A run of identical tool calls (same tool, same input) this long is a loop.
LOOP_MIN_RUN = 3
# "If it had stopped after N tries": calls past this many count as waste.
LOOP_STOP_AFTER = 3
# Per-request context above this is a "long context" turn.
LONG_CONTEXT_TOKENS = 150_000
# Premium tier = list input price at or above this ($/MTok).
PREMIUM_INPUT_PRICE = 5.0
# Model used to price the "a cheaper model would do" what-if.
DEFAULT_CHEAPER_MODEL = "claude-sonnet-4-6"
# Bill CSV money column, most specific first.
MONEY_KEYS = ("net_amount", "net amount", "cost", "amount", "usd", "total")


# --------------------------------------------------------------------------
# prices
# --------------------------------------------------------------------------

def load_prices(path):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    if not isinstance(cfg.get("models"), list):
        raise ValueError("prices.json has no 'models' list")
    return cfg


def match_model(model_id, models):
    """Longest-prefix match. Returns the price entry or None (unpriced)."""
    best, best_len = None, -1
    for m in models:
        for prefix in m.get("match", []):
            if model_id.startswith(prefix) and len(prefix) > best_len:
                best, best_len = m, len(prefix)
    return best


def tier_for(price, tokens):
    """Pick standard or long-context prices for one request."""
    if price is None:
        return None
    limit = price.get("long_context_threshold")
    if limit and isinstance(price.get("long_context"), dict):
        prompt = (tokens["input"] + tokens["cache_read"]
                  + tokens["cache_write_5m"] + tokens["cache_write_1h"])
        if prompt > limit:
            return price["long_context"]
    return price


def price_tokens(tokens, price):
    """Return (cost_usd, unpriced_tokens). A missing price is never $0."""
    if price is None:
        return 0.0, sum(tokens[b] for b in BUCKETS)
    p = tier_for(price, tokens)
    cost, unknown = 0.0, 0
    for b in BUCKETS:
        rate = p.get(b)
        if rate is None:
            unknown += tokens[b]
        else:
            cost += tokens[b] * rate / MTOK
    return cost, unknown


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def _num(v):
    return int(v) if isinstance(v, (int, float)) and v > 0 else 0


def parse_usage(usage):
    t = {"input": _num(usage.get("input_tokens")),
         "output": _num(usage.get("output_tokens")),
         "cache_read": _num(usage.get("cache_read_input_tokens")),
         "cache_write_5m": 0, "cache_write_1h": 0}
    creation = usage.get("cache_creation")
    if isinstance(creation, dict):
        t["cache_write_5m"] = _num(creation.get("ephemeral_5m_input_tokens"))
        t["cache_write_1h"] = _num(creation.get("ephemeral_1h_input_tokens"))
    if t["cache_write_5m"] == 0 and t["cache_write_1h"] == 0:
        t["cache_write_5m"] = _num(usage.get("cache_creation_input_tokens"))
    return t


def parse_ts(ts):
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()


def tool_signature(name, tool_input):
    try:
        blob = json.dumps(tool_input, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        blob = repr(tool_input)
    digest = hashlib.sha1(blob.encode("utf-8", "replace")).hexdigest()[:12]
    return "%s:%s" % (name, digest), blob


def short(text, n=60):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 3] + "..."


def scan(log_dir, cfg):
    """Read every *.jsonl under log_dir. Returns a dict with messages,
    tool calls, tool results and counters. Nothing leaves this function
    except plain numbers and short tool labels."""
    models = cfg["models"]
    messages = {}          # dedup key -> message record
    order = []             # dedup keys in first-seen order
    tool_calls = {}        # tool_use id -> call record
    tool_results = {}      # tool_use id -> is_error (bool)
    unknown_models = defaultdict(int)
    c = defaultdict(int)

    for root, _dirs, files in os.walk(log_dir):
        for name in sorted(files):
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(root, name)
            c["files"] += 1
            project = os.path.basename(root) if root != log_dir else "(root)"
            try:
                fallback = datetime.fromtimestamp(os.path.getmtime(path))
                fallback = fallback.astimezone()
            except OSError:
                fallback = None
            try:
                fh = open(path, encoding="utf-8", errors="replace")
            except OSError:
                c["files_unreadable"] += 1
                continue
            with fh:
                for seq, raw in enumerate(fh):
                    line = raw.strip()
                    if not line:
                        continue
                    c["lines"] += 1
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        c["bad_json"] += 1
                        continue
                    if not isinstance(obj, dict):
                        c["bad_json"] += 1
                        continue
                    msg = obj.get("message")
                    if not isinstance(msg, dict):
                        c["no_usage"] += 1
                        continue
                    session = (obj.get("sessionId")
                               or os.path.splitext(name)[0])
                    content = msg.get("content")
                    blocks = content if isinstance(content, list) else []

                    # tool results live in user lines
                    for b in blocks:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            tid = b.get("tool_use_id")
                            if tid:
                                tool_results[tid] = bool(b.get("is_error"))

                    usage = msg.get("usage")
                    if not isinstance(usage, dict):
                        if not any(isinstance(b, dict)
                                   and b.get("type") == "tool_result"
                                   for b in blocks):
                            c["no_usage"] += 1
                        continue

                    # Claude Code writes one API response as several lines
                    # (one per content block) with the same message id and
                    # the same usage. Count usage once per message id.
                    key = msg.get("id") or obj.get("uuid")
                    if key and obj.get("requestId"):
                        key = "%s|%s" % (key, obj["requestId"])
                    if not key:
                        key = "line:%s:%d" % (path, seq)
                    dt = parse_ts(obj.get("timestamp"))
                    is_dup = key in messages
                    if is_dup:
                        c["duplicate"] += 1
                        rec = messages[key]
                    else:
                        if dt is None:
                            c["ts_missing"] += 1
                            dt = fallback
                        if dt is None:
                            c["no_usage"] += 1
                            continue
                        tokens = parse_usage(usage)
                        model = msg.get("model") or ""
                        price = match_model(model, models)
                        cost, unknown = price_tokens(tokens, price)
                        if price is None:
                            unknown_models[model or "(no model name)"] += sum(
                                tokens[b] for b in BUCKETS)
                        rec = {"key": key, "session": session,
                               "project": project, "ts": dt,
                               "date": dt.date(), "model": model,
                               "tokens": tokens, "cost": cost,
                               "unknown": unknown, "tool_ids": [],
                               "premium": bool(price and (price.get("input")
                                               or 0) >= PREMIUM_INPUT_PRICE)}
                        messages[key] = rec
                        order.append(key)
                        c["parsed"] += 1
                    for b in blocks:
                        if not (isinstance(b, dict)
                                and b.get("type") == "tool_use"):
                            continue
                        tid = b.get("id") or "%s#%d" % (key, len(rec["tool_ids"]))
                        if tid in tool_calls:
                            continue
                        tname = b.get("name") or "(tool)"
                        sig, blob = tool_signature(tname, b.get("input"))
                        tool_calls[tid] = {"id": tid, "name": tname, "sig": sig,
                                           "label": "%s %s" % (tname, short(blob, 50)),
                                           "msg": key}
                        rec["tool_ids"].append(tid)
    return {"messages": [messages[k] for k in order], "tool_calls": tool_calls,
            "tool_results": tool_results, "unknown_models": dict(unknown_models),
            "counters": dict(c)}


# --------------------------------------------------------------------------
# analysis
# --------------------------------------------------------------------------

def filter_days(data, days):
    if not days or days <= 0 or not data["messages"]:
        return data
    latest = max(m["date"] for m in data["messages"])
    kept = [m for m in data["messages"] if (latest - m["date"]).days < days]
    out = dict(data)
    out["messages"] = kept
    return out


def daily(messages):
    days = defaultdict(lambda: dict({b: 0 for b in BUCKETS}, cost=0.0,
                                    unknown=0, messages=0))
    for m in messages:
        d = days[m["date"]]
        for b in BUCKETS:
            d[b] += m["tokens"][b]
        d["cost"] += m["cost"]
        d["unknown"] += m["unknown"]
        d["messages"] += 1
    return dict(sorted(days.items()))


def sessions(messages):
    out = {}
    for m in messages:
        s = out.setdefault(m["session"], {
            "session": m["session"], "project": m["project"], "cost": 0.0,
            "messages": 0, "start": m["ts"], "end": m["ts"],
            "models": defaultdict(float), "unknown": 0, "max_context": 0})
        s["cost"] += m["cost"]
        s["messages"] += 1
        s["unknown"] += m["unknown"]
        s["models"][m["model"] or "(no model name)"] += m["cost"]
        s["start"] = min(s["start"], m["ts"])
        s["end"] = max(s["end"], m["ts"])
        t = m["tokens"]
        ctx = t["input"] + t["cache_read"] + t["cache_write_5m"] + t["cache_write_1h"]
        s["max_context"] = max(s["max_context"], ctx)
    for s in out.values():
        s["top_model"] = max(s["models"].items(), key=lambda kv: kv[1])[0]
        s["models"] = dict(s["models"])
    return sorted(out.values(), key=lambda s: -s["cost"])


def call_cost(call, by_key):
    """A message's cost split evenly across the tool calls it made."""
    m = by_key.get(call["msg"])
    if not m or not m["tool_ids"]:
        return 0.0
    return m["cost"] / len(m["tool_ids"])


def find_loops(data, min_run=LOOP_MIN_RUN, stop_after=LOOP_STOP_AFTER):
    """Runs of >= min_run consecutive identical tool calls in one session.
    Waste = cost of the calls after the first `stop_after` tries."""
    by_key = {m["key"]: m for m in data["messages"]}
    per_session = defaultdict(list)
    for m in data["messages"]:   # already in file order
        for tid in m["tool_ids"]:
            call = data["tool_calls"].get(tid)
            if call:
                per_session[m["session"]].append(call)
    loops = []
    for session, calls in per_session.items():
        i = 0
        while i < len(calls):
            j = i
            while j + 1 < len(calls) and calls[j + 1]["sig"] == calls[i]["sig"]:
                j += 1
            run = calls[i:j + 1]
            if len(run) >= min_run:
                errors = sum(1 for c in run
                             if data["tool_results"].get(c["id"]) is True)
                total = sum(call_cost(c, by_key) for c in run)
                waste = sum(call_cost(c, by_key) for c in run[stop_after:])
                first = by_key[run[0]["msg"]]
                loops.append({
                    "session": session, "project": first["project"],
                    "tool": run[0]["name"], "label": run[0]["label"],
                    "repeats": len(run), "errors": errors,
                    "kind": "failing" if errors >= 2 else "repeating",
                    "cost": total, "waste": waste,
                    "start": first["ts"]})
            i = j + 1
    return sorted(loops, key=lambda l: (-l["waste"], -l["repeats"]))


def cheaper_whatif(data, cfg, target=DEFAULT_CHEAPER_MODEL):
    """Upper bound: premium-model turns re-priced at the cheaper model."""
    price = match_model(target, cfg["models"])
    if price is None:
        return None
    current = alt = 0.0
    turns = 0
    for m in data["messages"]:
        if not m["premium"] or m["unknown"]:
            continue
        cost, unknown = price_tokens(m["tokens"], price)
        if unknown:
            continue
        current += m["cost"]
        alt += cost
        turns += 1
    if turns == 0 or current <= alt:
        return None
    return {"target": target, "turns": turns, "current": current,
            "alt": alt, "saving": current - alt}


def build_fixes(data, cfg, loops, sess, cheaper):
    """Concrete changes, biggest money first. Each fix says whether its
    dollar figure is measured from your logs or modelled."""
    fixes = []
    total = sum(m["cost"] for m in data["messages"])
    waste = sum(l["waste"] for l in loops)
    if loops:
        worst = loops[0]
        fixes.append({
            "title": "Stop retry loops after %d tries" % LOOP_STOP_AFTER,
            "why": "%d loop(s) found. Worst: `%s` repeated %d times "
                   "(%d failed) in one session." % (
                       len(loops), worst["label"], worst["repeats"],
                       worst["errors"]),
            "do": "Add this line to CLAUDE.md: \"If the same command fails "
                  "twice, stop and tell me instead of retrying.\" For "
                  "headless runs also pass --max-turns.",
            "saving": waste, "basis": "measured"})
    if cheaper:
        fixes.append({
            "title": "Use %s for routine turns" % cheaper["target"],
            "why": "%d turn(s) ran on a premium model ($%.2f). The same tokens "
                   "at %s prices would be $%.2f." % (
                       cheaper["turns"], cheaper["current"],
                       cheaper["target"], cheaper["alt"]),
            "do": "Run /model and pick a Sonnet model for edits, tests and "
                  "docs. Switch back to the premium model only for design "
                  "and hard debugging.",
            "saving": cheaper["saving"],
            "basis": "modelled upper bound (only true for turns the cheaper "
                     "model can handle)"})
    long_sessions = [s for s in sess if s["max_context"] > LONG_CONTEXT_TOKENS]
    if long_sessions:
        fixes.append({
            "title": "Clear context between tasks",
            "why": "%d session(s) sent more than %dk tokens of context in a "
                   "single request. Every turn re-sends the whole history." % (
                       len(long_sessions), LONG_CONTEXT_TOKENS // 1000),
            "do": "Type /clear when you switch task and /compact when a "
                  "session gets long.",
            "saving": None, "basis": "not quantified"})
    if data["unknown_models"]:
        fixes.append({
            "title": "Add missing prices",
            "why": "%d model(s) have no price in prices.json, so their tokens "
                   "are not in the totals." % len(data["unknown_models"]),
            "do": "Add the model to prices.json from the official pricing "
                  "page.",
            "saving": None, "basis": "not quantified"})
    # Measured money first, then modelled, then unquantified.
    rank = {"measured": 0}
    fixes.sort(key=lambda f: (rank.get(f["basis"], 1 if f["saving"] else 2),
                              -(f["saving"] or 0)))
    return fixes, total


def reconcile(daily_rows, bill_path):
    """Compare log-based cost with a bill CSV (any CSV with a date column
    and a cost/amount column). Returns rows of (day, logs, bill, gap)."""
    with open(bill_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fields = [h for h in (reader.fieldnames or [])]
        dcol = next((h for h in fields if "date" in h.lower()), None)
        # Money column: net amount > cost > amount > usd > total, and
        # never a token count (Cursor exports have "Total Tokens").
        acol = next((h for k in MONEY_KEYS for h in fields
                     if k in h.lower() and "token" not in h.lower()), None)
        if not dcol or not acol:
            raise ValueError("bill CSV needs a date column and a "
                             "cost/amount column; found %s" % fields)
        bill = defaultdict(float)
        for row in reader:
            raw_d = (row.get(dcol) or "").strip()[:10]
            raw_a = (row.get(acol) or "").replace("$", "").replace(",", "").strip()
            try:
                bill[date.fromisoformat(raw_d)] += float(raw_a)
            except ValueError:
                continue
    days = sorted(set(bill) | set(daily_rows))
    rows = []
    for d in days:
        logs = daily_rows[d]["cost"] if d in daily_rows else 0.0
        rows.append((d, logs, bill.get(d, 0.0), bill.get(d, 0.0) - logs))
    return rows


def analyse(data, cfg, bill=None):
    sess = sessions(data["messages"])
    loops = find_loops(data)
    cheaper = cheaper_whatif(data, cfg)
    fixes, total = build_fixes(data, cfg, loops, sess, cheaper)
    days = daily(data["messages"])
    result = {"days": days, "sessions": sess, "loops": loops,
              "fixes": fixes, "total": total, "cheaper": cheaper,
              "unknown_models": data["unknown_models"],
              "counters": data["counters"], "bill": None}
    if bill:
        result["bill"] = reconcile(days, bill)
    return result


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

def money(v):
    return "n/a" if v is None else "$%.2f" % v


def fmt_dt(dt):
    return dt.strftime("%Y-%m-%d %H:%M")


def price_age_warning(cfg):
    try:
        fetched = datetime.strptime(cfg.get("fetched_at", ""), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return "prices.json has no valid fetched_at date; check prices."
    age = (date.today() - fetched).days
    if age > 7:
        return "prices.json is %d days old; check the official pricing page." % age
    return None


def text_report(r, cfg, top=10):
    L = []
    L.append("AI Bill Watchdog v%s  (local only, nothing uploaded)" % VERSION)
    L.append("Prices: %s, source %s" % (cfg.get("fetched_at", "?"),
                                         cfg.get("source", "?")))
    w = price_age_warning(cfg)
    if w:
        L.append("WARNING: " + w)
    L.append("")
    if not r["days"]:
        L.append("No Claude Code usage found. Default folder is "
                 "~/.claude/projects (use --logs to point elsewhere).")
        return "\n".join(L)
    days = r["days"]
    first, last = min(days), max(days)
    worst_day = max(days.items(), key=lambda kv: kv[1]["cost"])
    L.append("Total: %s API-equivalent over %d day(s), %s to %s, %d sessions."
             % (money(r["total"]), len(days), first, last, len(r["sessions"])))
    L.append("Most expensive day: %s, %s" % (worst_day[0],
                                             money(worst_day[1]["cost"])))
    L.append("")
    L.append("TOP %d SESSIONS" % min(top, len(r["sessions"])))
    L.append("  #  cost       share  msgs  started           model / project")
    for i, s in enumerate(r["sessions"][:top], 1):
        share = 100.0 * s["cost"] / r["total"] if r["total"] else 0.0
        L.append("%3d  %-9s %5.1f%%  %4d  %s  %s / %s" % (
            i, money(s["cost"]), share, s["messages"], fmt_dt(s["start"]),
            s["top_model"], s["project"]))
    L.append("")
    if r["loops"]:
        L.append("LOOPS (same tool call, same input, %d+ times in a row)"
                 % LOOP_MIN_RUN)
        for l in r["loops"][:10]:
            L.append("  %s x%d (%d failed)  cost %s, wasted after %d tries %s"
                     % (l["label"], l["repeats"], l["errors"], money(l["cost"]),
                        LOOP_STOP_AFTER, money(l["waste"])))
            L.append("     session %s, %s" % (l["session"], fmt_dt(l["start"])))
    else:
        L.append("LOOPS: none found.")
    L.append("")
    L.append("WHAT TO CHANGE")
    if not r["fixes"]:
        L.append("  Nothing stands out. Your setup looks clean.")
    for i, f in enumerate(r["fixes"], 1):
        L.append("  %d. %s  -> saves %s (%s)" % (i, f["title"],
                                                money(f["saving"]), f["basis"]))
        L.append("     Why: " + f["why"])
        L.append("     Do:  " + f["do"])
    if r["bill"]:
        L.append("")
        L.append("BILL CHECK (your bill CSV minus what the logs explain)")
        for d, logs, bill, gap in r["bill"]:
            flag = "  <-- check" if abs(gap) >= max(1.0, 0.2 * max(bill, logs)) else ""
            L.append("  %s  logs %s  bill %s  gap %s%s" % (
                d, money(logs), money(bill), money(gap), flag))
    if r["unknown_models"]:
        L.append("")
        L.append("UNPRICED (counted, not priced, never treated as $0):")
        for k, v in sorted(r["unknown_models"].items(), key=lambda kv: -kv[1]):
            L.append("  %s: %d tokens" % (k, v))
    c = r["counters"]
    L.append("")
    L.append("Read %d files, %d lines: %d messages, %d duplicate lines merged, "
             "%d bad lines, %d without timestamp (file date used)." % (
                 c.get("files", 0), c.get("lines", 0), c.get("parsed", 0),
                 c.get("duplicate", 0), c.get("bad_json", 0),
                 c.get("ts_missing", 0)))
    L.append("Dollar figures are API-equivalent at list price. On Pro/Max you "
             "pay a flat fee; use them as a usage measure.")
    return "\n".join(L)


def markdown_report(r, cfg, top=10):
    L = ["# AI Bill Watchdog report", ""]
    if not r["days"]:
        return "# AI Bill Watchdog report\n\nNo usage found.\n"
    days = r["days"]
    L.append("Generated by AI Bill Watchdog v%s on your own machine. Prices "
             "from %s (%s)." % (VERSION, cfg.get("source", "?"),
                                cfg.get("fetched_at", "?")))
    L.append("")
    L.append("**Total: %s** API-equivalent, %s to %s, %d sessions."
             % (money(r["total"]), min(days), max(days), len(r["sessions"])))
    save = sum(f["saving"] for f in r["fixes"]
               if f["saving"] and f["basis"] == "measured")
    if save:
        L.append("")
        L.append("**Measured waste you can stop: %s.**" % money(save))
    L.append("")
    L.append("## What to change")
    L.append("")
    if not r["fixes"]:
        L.append("Nothing stands out.")
    for i, f in enumerate(r["fixes"], 1):
        L.append("%d. **%s** - saves %s (%s)  " % (i, f["title"],
                                                 money(f["saving"]), f["basis"]))
        L.append("   Why: %s  " % f["why"])
        L.append("   Do: %s" % f["do"])
    L.append("")
    L.append("## Top %d sessions" % min(top, len(r["sessions"])))
    L.append("")
    L.append("| # | Cost | Share | Messages | Started | Model | Project |")
    L.append("|---|---|---|---|---|---|---|")
    for i, s in enumerate(r["sessions"][:top], 1):
        share = 100.0 * s["cost"] / r["total"] if r["total"] else 0.0
        L.append("| %d | %s | %.1f%% | %d | %s | %s | %s |" % (
            i, money(s["cost"]), share, s["messages"], fmt_dt(s["start"]),
            s["top_model"], s["project"]))
    L.append("")
    L.append("## Loops")
    L.append("")
    if not r["loops"]:
        L.append("None found.")
    for l in r["loops"][:10]:
        L.append("- `%s` x%d, %d failed. Cost %s; wasted after %d tries %s."
                 % (l["label"], l["repeats"], l["errors"], money(l["cost"]),
                    LOOP_STOP_AFTER, money(l["waste"])))
    if r["bill"]:
        L.append("")
        L.append("## Bill check")
        L.append("")
        L.append("| Day | Logs | Bill | Gap |")
        L.append("|---|---|---|---|")
        for d, logs, bill, gap in r["bill"]:
            L.append("| %s | %s | %s | %s |" % (d, money(logs), money(bill),
                                             money(gap)))
    L.append("")
    L.append("_API-equivalent cost at list price, not an invoice. Nothing in "
             "this report left your machine._")
    return "\n".join(L) + "\n"


def html_report(r, cfg, top=10):
    md = markdown_report(r, cfg, top)
    body = []
    in_table = False
    for line in md.splitlines():
        esc = html.escape(line)
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if set("".join(cells)) <= set("-"):
                continue
            tag = "th" if not in_table else "td"
            if not in_table:
                body.append("<table>")
                in_table = True
            body.append("<tr>" + "".join("<%s>%s</%s>" % (tag, html.escape(c), tag)
                                         for c in cells) + "</tr>")
            continue
        if in_table:
            body.append("</table>")
            in_table = False
        if line.startswith("# "):
            body.append("<h1>%s</h1>" % html.escape(line[2:]))
        elif line.startswith("## "):
            body.append("<h2>%s</h2>" % html.escape(line[3:]))
        elif line.strip():
            esc = esc.replace("**", "")
            body.append("<p>%s</p>" % esc)
    if in_table:
        body.append("</table>")
    style = ("body{font-family:system-ui,sans-serif;max-width:860px;margin:2em auto;"
             "padding:0 1em;line-height:1.5}table{border-collapse:collapse;width:100%}"
             "td,th{border:1px solid #ccc;padding:4px 6px;font-size:14px;text-align:left}"
             "h1{font-size:26px}h2{font-size:19px;margin-top:1.6em}")
    return ("<!doctype html><meta charset='utf-8'><title>AI Bill Watchdog report"
            "</title><style>%s</style>%s" % (style, "\n".join(body)))


def write_csvs(r, prefix):
    with open(prefix + "-daily.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "input_tokens", "output_tokens", "cache_read_tokens",
                    "cache_write_5m_tokens", "cache_write_1h_tokens", "cost_usd",
                    "unpriced_tokens", "messages"])
        for d, v in r["days"].items():
            w.writerow([d.isoformat(), v["input"], v["output"], v["cache_read"],
                        v["cache_write_5m"], v["cache_write_1h"],
                        round(v["cost"], 6), v["unknown"], v["messages"]])
    with open(prefix + "-sessions.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["session", "project", "cost_usd", "messages", "start",
                    "end", "top_model", "max_context_tokens"])
        for s in r["sessions"]:
            w.writerow([s["session"], s["project"], round(s["cost"], 6),
                        s["messages"], s["start"].isoformat(),
                        s["end"].isoformat(), s["top_model"], s["max_context"]])


def to_json(r):
    def conv(o):
        if isinstance(o, (date, datetime)):
            return o.isoformat()
        raise TypeError(type(o))
    out = dict(r)
    out["days"] = {d.isoformat(): v for d, v in r["days"].items()}
    out["bill"] = ([{"date": d.isoformat(), "logs": a, "bill": b, "gap": g}
                    for d, a, b, g in r["bill"]] if r["bill"] else None)
    return json.dumps(out, default=conv, indent=2)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="watchdog.py",
        description="Find where your Claude Code money went. Local only.")
    ap.add_argument("--logs", default=os.path.expanduser("~/.claude/projects"),
                    help="Claude Code log folder (default ~/.claude/projects)")
    ap.add_argument("--prices", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "prices.json"),
        help="price file (default: prices.json next to this script)")
    ap.add_argument("--days", type=int, default=0,
                    help="only the last N days (default: all)")
    ap.add_argument("--top", type=int, default=10,
                    help="how many sessions to list (default 10)")
    ap.add_argument("--report", metavar="FILE",
                    help="write a one-page report (.md or .html)")
    ap.add_argument("--csv", metavar="PREFIX",
                    help="write PREFIX-daily.csv and PREFIX-sessions.csv")
    ap.add_argument("--bill", metavar="CSV",
                    help="compare with a bill CSV (date + cost/amount columns)")
    ap.add_argument("--json", action="store_true",
                    help="print JSON instead of text")
    ap.add_argument("--version", action="version", version=VERSION)
    a = ap.parse_args(argv)
    if not os.path.isdir(a.logs):
        print("Log folder not found: %s\nUse --logs to point at your Claude "
              "Code projects folder." % a.logs, file=sys.stderr)
        return 1
    try:
        cfg = load_prices(a.prices)
    except (OSError, ValueError) as e:
        print("Cannot read prices file %s: %s" % (a.prices, e), file=sys.stderr)
        return 1
    data = filter_days(scan(a.logs, cfg), a.days)
    try:
        r = analyse(data, cfg, a.bill)
    except (OSError, ValueError) as e:
        print("Bill check failed: %s" % e, file=sys.stderr)
        return 1
    print(to_json(r) if a.json else text_report(r, cfg, a.top))
    if a.report:
        content = (html_report(r, cfg, a.top) if a.report.lower().endswith(
            (".html", ".htm")) else markdown_report(r, cfg, a.top))
        with open(a.report, "w", encoding="utf-8") as f:
            f.write(content)
        if not a.json:
            print("\nReport written: %s" % a.report)
    if a.csv:
        write_csvs(r, a.csv)
        if not a.json:
            print("CSV written: %s-daily.csv, %s-sessions.csv" % (a.csv, a.csv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
