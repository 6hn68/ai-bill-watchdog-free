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
import math
import os
import re
import sys
from collections import defaultdict
import calendar
from datetime import date, datetime, timedelta, timezone

VERSION = "0.4.0"
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
# Claude subscription usage windows are 5 hours long.
BLOCK_HOURS = 5
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


def filter_range(data, since=None, until=None):
    """Keep messages whose local date is within [since, until] (ISO dates)."""
    if not since and not until:
        return data
    lo = date.fromisoformat(since) if since else None
    hi = date.fromisoformat(until) if until else None
    out = dict(data)
    out["messages"] = [m for m in data["messages"]
                       if (lo is None or m["date"] >= lo)
                       and (hi is None or m["date"] <= hi)]
    return out


def period_key(d, kind):
    if kind == "week":
        return (d - timedelta(days=d.weekday())).isoformat()
    if kind == "month":
        return d.strftime("%Y-%m")
    return d.isoformat()


def periods(days, kind):
    """Roll daily rows up into weeks (key = Monday) or months (YYYY-MM)."""
    out = {}
    for d, row in days.items():
        k = period_key(d, kind)
        p = out.setdefault(k, dict({b: 0 for b in BUCKETS}, cost=0.0,
                                   unknown=0, messages=0, days=0))
        for b in BUCKETS:
            p[b] += row[b]
        p["cost"] += row["cost"]
        p["unknown"] += row["unknown"]
        p["messages"] += row["messages"]
        p["days"] += 1
    return dict(sorted(out.items()))


def breakdown(messages, field):
    """Cost per model or per project, most expensive first."""
    out = {}
    order = []
    for m in messages:
        k = m[field] or "(none)"
        if k not in out:
            out[k] = {"name": k, "cost": 0.0, "messages": 0, "tokens": 0,
                      "unknown": 0}
            order.append(k)
        r = out[k]
        r["cost"] += m["cost"]
        r["messages"] += 1
        r["tokens"] += sum(m["tokens"][b] for b in BUCKETS)
        r["unknown"] += m["unknown"]
    rows = [out[k] for k in order]
    return sorted(rows, key=lambda r: -r["cost"])


def blocks(messages, hours=BLOCK_HOURS):
    """Group usage into 5-hour windows. A window opens at the hour of the
    first message after the previous window closed (same rule Claude uses
    for its usage limits), and closes `hours` later."""
    out = []
    cur = None
    for m in sorted(messages, key=lambda m: m["ts"]):
        if cur is None or m["ts"] >= cur["end"]:
            start = m["ts"].replace(minute=0, second=0, microsecond=0)
            cur = {"start": start, "end": start + timedelta(hours=hours),
                   "first": m["ts"], "last": m["ts"], "cost": 0.0,
                   "messages": 0, "tokens": 0, "unknown": 0}
            out.append(cur)
        cur["last"] = m["ts"]
        cur["cost"] += m["cost"]
        cur["messages"] += 1
        cur["tokens"] += sum(m["tokens"][b] for b in BUCKETS)
        cur["unknown"] += m["unknown"]
    return out


def pace(days, budget=None):
    """Month-to-date spend for the month of the latest activity and the
    straight-line projection for the whole month."""
    if not days:
        return None
    asof = max(days)
    mtd = sum(r["cost"] for d, r in days.items()
              if d.year == asof.year and d.month == asof.month)
    in_month = calendar.monthrange(asof.year, asof.month)[1]
    projected = mtd / asof.day * in_month
    out = {"month": asof.strftime("%Y-%m"), "as_of": asof, "mtd": mtd,
           "elapsed_days": asof.day, "days_in_month": in_month,
           "projected": projected, "budget": budget, "over_budget": None}
    if budget is not None:
        out["over_budget"] = projected > budget
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


def analyse(data, cfg, bill=None, budget=None):
    sess = sessions(data["messages"])
    loops = find_loops(data)
    cheaper = cheaper_whatif(data, cfg)
    fixes, total = build_fixes(data, cfg, loops, sess, cheaper)
    days = daily(data["messages"])
    result = {"days": days, "sessions": sess, "loops": loops,
              "fixes": fixes, "total": total, "cheaper": cheaper,
              "unknown_models": data["unknown_models"],
              "counters": data["counters"], "bill": None,
              "weeks": periods(days, "week"), "months": periods(days, "month"),
              "by_model": breakdown(data["messages"], "model"),
              "by_project": breakdown(data["messages"], "project"),
              "blocks": blocks(data["messages"]),
              "pace": pace(days, budget)}
    if bill:
        result["bill"] = reconcile(days, bill)
    return result


# --------------------------------------------------------------------------
# dispute pack (v0.4.0): facts for a billing review you send yourself
# --------------------------------------------------------------------------

# Words that mark a bill row as a charge/payment rather than a usage line.
CHARGE_WORDS = re.compile(r"recharge|reload|top[- ]?up|purchase|payment|"
                          r"invoice|receipt|prepaid|credit", re.I)
# Column names that describe what a bill row is.
DESC_KEYS = ("description", "type", "kind", "category", "item", "product",
             "event", "memo")
# This many charges on one day is a "same-day multiple charges" burst.
BURST_MIN = 2
# A day is flagged when |gap| >= max(GAP_MIN_USD, GAP_SHARE * larger side).
GAP_MIN_USD = 1.0
GAP_SHARE = 0.2
LETTER_MAX_DAYS = 10


def usd(v):
    """$1,234.56 with round-half-up on cents (same arithmetic as engine.js)."""
    cents = int(math.floor(abs(v) * 100 + 0.5))
    s = "{:,}".format(cents // 100) + ".%02d" % (cents % 100)
    return ("-$" if v < 0 and cents else "$") + s


def gap_flag(bill, logs):
    gap = bill - logs
    if abs(gap) < max(GAP_MIN_USD, GAP_SHARE * max(bill, logs)):
        return ""
    return "bill higher" if gap > 0 else "logs higher"


def bill_charges(bill_path):
    """Charge rows from a bill CSV: [{date, time, amount, label}] in file
    order, plus which column decided it. Times are kept as written (HH:MM)."""
    with open(bill_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        rows = [r for r in reader if len(r) > 1 or (r and r[0].strip())]
    if not rows:
        return [], None
    fields = [h.strip() for h in rows[0]]
    lower = [h.lower() for h in fields]
    dcol = next((i for i, h in enumerate(lower) if "date" in h), None)
    acol = next((i for k in MONEY_KEYS for i, h in enumerate(lower)
                 if k in h and "token" not in h), None)
    mcol = next((i for i, h in enumerate(lower)
                 if h == "model" or h.startswith("model") or h == "sku"), None)
    xcol = next((i for i, h in enumerate(lower)
                 if any(k in h for k in DESC_KEYS)), None)
    if dcol is None or acol is None:
        return [], None
    if xcol is None and mcol is not None:
        return [], "usage export (has a model column, no description column)"
    out = []
    for r in rows[1:]:
        raw_d = (r[dcol] if dcol < len(r) else "").strip()
        raw_a = (r[acol] if acol < len(r) else "").replace("$", "").replace(",", "").strip()
        if not ISO_AMOUNT.match(raw_a):
            continue
        try:
            d = date.fromisoformat(raw_d[:10])
        except ValueError:
            continue
        a = float(raw_a)
        label = (r[xcol] if xcol is not None and xcol < len(r) else "").strip()
        if a <= 0 or (xcol is not None and not CHARGE_WORDS.search(label)):
            continue
        m = TIME_RE.match(raw_d)
        out.append({"date": d, "time": ("%s:%s" % m.groups()) if m else None,
                    "amount": a, "label": label})
    return out, (fields[xcol] if xcol is not None else "every row (no description column)")


ISO_AMOUNT = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ](\d{2}):(\d{2})")


def find_bursts(charges, daily_rows):
    by_day = defaultdict(list)
    for c in charges:
        by_day[c["date"]].append(c)
    out = []
    for d in sorted(by_day):
        cs = by_day[d]
        if len(cs) < BURST_MIN:
            continue
        times = sorted(c["time"] for c in cs if c["time"])
        span = None
        if len(times) >= 2:
            h1, m1 = map(int, times[0].split(":"))
            h2, m2 = map(int, times[-1].split(":"))
            span = (h2 * 60 + m2) - (h1 * 60 + m1)
        row = daily_rows.get(d)
        out.append({"date": d, "count": len(cs),
                    "total": sum(c["amount"] for c in cs),
                    "amounts": [c["amount"] for c in cs],
                    "first": times[0] if times else None,
                    "last": times[-1] if times else None,
                    "span_minutes": span,
                    "logs": row["cost"] if row else 0.0,
                    "log_messages": row["messages"] if row else 0})
    return out


def day_evidence(messages, d):
    ms = [m for m in messages if m["date"] == d]
    if not ms:
        return None
    by_model = defaultdict(float)
    order = []
    for m in ms:
        if m["model"] not in by_model:
            order.append(m["model"])
        by_model[m["model"]] += m["cost"]
    top = None
    for k in order:
        if top is None or by_model[k] > by_model[top]:
            top = k
    ts = sorted(m["ts"] for m in ms)
    return {"date": d, "messages": len(ms),
            "sessions": len(set(m["session"] for m in ms)),
            "first": ts[0].strftime("%H:%M"), "last": ts[-1].strftime("%H:%M"),
            "cost": sum(m["cost"] for m in ms), "top_model": top or "(none)"}


def dispute(data, r, bill_path):
    """Everything the pack needs, as plain values."""
    rows = r["bill"] or []
    charges, charge_rule = bill_charges(bill_path)
    bursts = find_bursts(charges, r["days"])
    has_logs = bool(data["messages"])
    table = [{"date": d, "bill": b, "logs": lg, "gap": g,
              "note": gap_flag(b, lg) if has_logs else ""}
             for d, lg, b, g in rows]
    high = [t for t in table if t["note"] == "bill higher"]
    ev_days = sorted(set([t["date"] for t in high] + [b["date"] for b in bursts]))
    evidence = [e for e in (day_evidence(data["messages"], d) for d in ev_days) if e]
    return {"period": (rows[0][0], rows[-1][0]) if rows else (None, None),
            "bill_total": sum(t["bill"] for t in table),
            "logs_total": sum(t["logs"] for t in table),
            "has_logs": has_logs,
            "log_messages": sum(r["days"][t["date"]]["messages"]
                                for t in table if t["date"] in r["days"]),
            "table": table, "high_days": high,
            "high_total": sum(t["gap"] for t in high),
            "bursts": bursts, "charge_rule": charge_rule,
            "evidence": evidence}


def dispute_letter(dp):
    """Plain-fact draft. Never promises an outcome."""
    if not dp["high_days"] and not dp["bursts"]:
        return None
    p1, p2 = dp["period"]
    span = str(p1) if p1 == p2 else "%s to %s" % (p1, p2)
    when = "on %s" % p1 if p1 == p2 else "from %s to %s" % (p1, p2)
    L = ["Subject: Request for a billing review, %s" % span, "",
         "Hello,", "",
         "I would like a review of the charges on my account %s. "
         "The figures below come from my own bill export%s." % (
             when, " and my local usage logs" if dp["has_logs"] else ""), "",
         "- My bill for these dates totals %s." % usd(dp["bill_total"])]
    if dp["has_logs"]:
        L.append("- My local usage logs for the same dates come to an estimated "
                 "%s at published list prices." % usd(dp["logs_total"]))
    if dp["high_days"]:
        parts = ["%s (%s)" % (t["date"], usd(t["gap"]))
                 for t in dp["high_days"][:LETTER_MAX_DAYS]]
        more = len(dp["high_days"]) - LETTER_MAX_DAYS
        L.append("- On %d day(s) the bill is higher than the logs, by %s in "
                 "total: %s%s." % (len(dp["high_days"]), usd(dp["high_total"]),
                                   ", ".join(parts),
                                   (" and %d more" % more) if more > 0 else ""))
    for b in dp["bursts"]:
        when = ""
        if b["span_minutes"] is not None:
            when = " between %s and %s (%d minutes, times as shown in the bill)" % (
                b["first"], b["last"], b["span_minutes"])
        logs = ""
        if dp["has_logs"]:
            logs = " My logs show an estimated %s of usage that day." % usd(b["logs"])
        L.append("- On %s, %d charges totaling %s were made%s: %s.%s" % (
            b["date"], b["count"], usd(b["total"]), when,
            ", ".join(usd(a) for a in b["amounts"]), logs))
    L += ["", "The attached day-by-day table shows each date.", "",
          "Could you please:",
          "1. Tell me which usage each of these charges paid for.",
          "2. Review whether any of these charges should be credited or refunded.",
          ""]
    if dp["has_logs"]:
        L += ["I understand that log-based figures are estimates and may not "
              "include every billable item.", ""]
    L += ["Thank you,", "[Your name]", "[Account email or organization ID]"]
    return "\n".join(L)


def dispute_markdown(dp, cfg):
    p1, p2 = dp["period"]
    L = ["# Billing dispute evidence pack", "",
         "Made by AI Bill Watchdog v%s on your own computer from your own files. "
         "Nothing was uploaded." % VERSION, "",
         "This pack states facts from your files. It does not promise a refund "
         "or credit. You decide whether to send it, and you send it yourself.",
         "", "## Summary", "",
         "- Bill dates in your CSV: %s" % (p1 if p1 == p2 else "%s to %s" % (p1, p2)),
         "- Bill total: %s" % usd(dp["bill_total"])]
    if dp["has_logs"]:
        L.append("- Local log estimate for the same dates: %s (%d requests)"
                 % (usd(dp["logs_total"]), dp["log_messages"]))
        L.append("- Days where the bill is higher than the logs: %d, %s in total"
                 % (len(dp["high_days"]), usd(dp["high_total"])))
    else:
        L.append("- Local logs: none provided, so no bill-vs-logs comparison")
    L.append("- Days with %d or more charges: %d" % (BURST_MIN, len(dp["bursts"])))
    if dp["charge_rule"]:
        L.append("- Charge rows read from: %s" % dp["charge_rule"])
    if dp["bursts"]:
        L += ["", "## Same-day multiple charges"]
        for b in dp["bursts"]:
            L += ["", "### %s: %d charges, %s" % (b["date"], b["count"], usd(b["total"])), ""]
            if b["span_minutes"] is not None:
                L.append("- Times as written in the bill: %s to %s (%d minutes)"
                         % (b["first"], b["last"], b["span_minutes"]))
            else:
                L.append("- The bill has no times for these charges")
            L.append("- Amounts: %s" % ", ".join(usd(a) for a in b["amounts"]))
            if dp["has_logs"]:
                L.append("- Local log estimate for this day: %s (%d requests)"
                         % (usd(b["logs"]), b["log_messages"]))
    L += ["", "## Day by day", ""]
    if dp["has_logs"]:
        L += ["| Date | Bill | Logs (estimate) | Difference | Note |",
              "|---|---:|---:|---:|---|"]
        for t in dp["table"]:
            L.append("| %s | %s | %s | %s | %s |" % (
                t["date"], usd(t["bill"]), usd(t["logs"]), usd(t["gap"]), t["note"]))
    else:
        L += ["| Date | Bill |", "|---|---:|"]
        for t in dp["table"]:
            L.append("| %s | %s |" % (t["date"], usd(t["bill"])))
    if dp["evidence"]:
        L += ["", "## What your logs show on those days", "",
              "| Date | Requests | Sessions | First | Last | Logs cost | Top model |",
              "|---|---:|---:|---|---|---:|---|"]
        for e in dp["evidence"]:
            L.append("| %s | %d | %d | %s | %s | %s | %s |" % (
                e["date"], e["messages"], e["sessions"], e["first"], e["last"],
                usd(e["cost"]), e["top_model"]))
        L.append("")
        L.append("Times are local time on this computer.")
    L += ["", "## How the numbers are made", "",
          "Bill figures are the sum of the money column in your CSV, by the date "
          "as written. A day with %d or more charge rows (recharges, top-ups, "
          "purchases, invoices) is listed above." % BURST_MIN]
    if dp["has_logs"]:
        L.append("")
        L.append("The log estimate is the token counts in your Claude Code logs "
                 "times the list prices in prices.json (%s, checked %s). Logs "
                 "can miss some billable usage, so a difference is a question "
                 "to ask the provider, not proof of an error. A day is noted "
                 "when the difference is at least $1 and at least 20%% of the "
                 "larger figure." % (cfg.get("source", "?"), cfg.get("fetched_at", "?")))
    letter = dispute_letter(dp)
    L += ["", "## Draft message (edit before sending)", ""]
    if letter:
        L += ["```", letter, "```"]
    else:
        L.append("Nothing in these files points to a billing problem, so no "
                 "draft was written.")
    return "\n".join(L) + "\n"


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


def pace_line(pc):
    if not pc:
        return None
    line = ("This month (%s) so far: %s in %d of %d days -> on pace for %s."
            % (pc["month"], money(pc["mtd"]), pc["elapsed_days"],
               pc["days_in_month"], money(pc["projected"])))
    if pc["budget"] is not None:
        line += ("  OVER your %s budget." if pc["over_budget"]
                 else "  Within your %s budget.") % money(pc["budget"])
    return line


def extra_sections(r, opts):
    """Optional tables: --period week|month, --breakdown, --blocks."""
    L = []
    kind = opts.get("period")
    if kind in ("week", "month"):
        rows = r["weeks"] if kind == "week" else r["months"]
        L.append("")
        L.append("BY %s" % ("WEEK (starting Monday)" if kind == "week" else "MONTH"))
        L.append("  period       cost       msgs  active days")
        for k, v in rows.items():
            L.append("  %-11s  %-9s  %5d  %d" % (k, money(v["cost"]),
                                                 v["messages"], v["days"]))
    if opts.get("breakdown"):
        total = r["total"] or 0.0
        for title, rows in (("BY MODEL", r["by_model"]),
                            ("BY PROJECT", r["by_project"])):
            L.append("")
            L.append(title)
            for row in rows:
                share = 100.0 * row["cost"] / total if total else 0.0
                L.append("  %-9s %5.1f%%  %5d msgs  %s%s" % (
                    money(row["cost"]), share, row["messages"], row["name"],
                    "  (some tokens unpriced)" if row["unknown"] else ""))
    if opts.get("blocks"):
        L.append("")
        L.append("5-HOUR WINDOWS (the window Claude uses for usage limits)")
        L.append("  window start      cost       msgs  first-last")
        for b in r["blocks"]:
            L.append("  %s  %-9s  %5d  %s-%s" % (
                fmt_dt(b["start"]), money(b["cost"]), b["messages"],
                b["first"].strftime("%H:%M"), b["last"].strftime("%H:%M")))
        if r["blocks"]:
            worst = max(r["blocks"], key=lambda b: b["cost"])
            L.append("  Most expensive window: %s, %s" % (
                fmt_dt(worst["start"]), money(worst["cost"])))
    return L


def text_report(r, cfg, top=10, opts=None):
    opts = opts or {}
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
    pl = pace_line(r.get("pace"))
    if pl:
        L.append(pl)
    L.extend(extra_sections(r, opts))
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
    pl = pace_line(r.get("pace"))
    if pl:
        L.append("")
        L.append(pl)
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
    total = r["total"] or 0.0
    for title, rows in (("Cost by model", r["by_model"]),
                        ("Cost by project", r["by_project"])):
        L.append("## %s" % title)
        L.append("")
        L.append("| Cost | Share | Messages | Name |")
        L.append("|---|---|---|---|")
        for row in rows[:10]:
            share = 100.0 * row["cost"] / total if total else 0.0
            L.append("| %s | %.1f%% | %d | %s |" % (money(row["cost"]), share,
                                                 row["messages"], row["name"]))
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


def dispute_html(md):
    """Small, dependency-free Markdown-to-HTML for the dispute pack."""
    body, in_table, in_pre, in_list = [], False, False, False
    for line in md.splitlines():
        if line.startswith("```"):
            body.append("</pre>" if in_pre else "<pre>")
            in_pre = not in_pre
            continue
        if in_pre:
            body.append(html.escape(line))
            continue
        if in_list and not line.startswith("- "):
            body.append("</ul>")
            in_list = False
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if set("".join(cells)) <= set("-:"):
                continue
            tag = "td" if in_table else "th"
            if not in_table:
                body.append("<table>")
                in_table = True
            body.append("<tr>" + "".join("<%s>%s</%s>" % (tag, html.escape(c), tag)
                                         for c in cells) + "</tr>")
            continue
        if in_table:
            body.append("</table>")
            in_table = False
        if line.startswith("### "):
            body.append("<h3>%s</h3>" % html.escape(line[4:]))
        elif line.startswith("## "):
            body.append("<h2>%s</h2>" % html.escape(line[3:]))
        elif line.startswith("# "):
            body.append("<h1>%s</h1>" % html.escape(line[2:]))
        elif line.startswith("- "):
            if not in_list:
                body.append("<ul>")
                in_list = True
            body.append("<li>%s</li>" % html.escape(line[2:]))
        elif line.strip():
            body.append("<p>%s</p>" % html.escape(line))
    for flag, tag in ((in_list, "</ul>"), (in_table, "</table>"), (in_pre, "</pre>")):
        if flag:
            body.append(tag)
    style = ("body{font-family:system-ui,sans-serif;max-width:860px;margin:2em auto;"
             "padding:0 1em;line-height:1.5}table{border-collapse:collapse;width:100%}"
             "td,th{border:1px solid #ccc;padding:4px 6px;font-size:14px;text-align:left}"
             "pre{white-space:pre-wrap;background:#f6f6f6;padding:1em;border:1px solid #ddd}"
             "h1{font-size:26px}h2{font-size:19px;margin-top:1.6em}h3{font-size:16px}")
    return ("<!doctype html><meta charset='utf-8'><title>Billing dispute evidence pack"
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
    ap.add_argument("--since", metavar="YYYY-MM-DD",
                    help="only usage on or after this date")
    ap.add_argument("--until", metavar="YYYY-MM-DD",
                    help="only usage on or before this date")
    ap.add_argument("--period", choices=("day", "week", "month"),
                    default="day", help="also show totals by week or month")
    ap.add_argument("--breakdown", action="store_true",
                    help="show cost by model and by project")
    ap.add_argument("--blocks", action="store_true",
                    help="show 5-hour usage windows")
    ap.add_argument("--budget", type=float, metavar="USD",
                    help="monthly budget; exit code 2 if this month is on "
                         "pace to go over it")
    ap.add_argument("--top", type=int, default=10,
                    help="how many sessions to list (default 10)")
    ap.add_argument("--report", metavar="FILE",
                    help="write a one-page report (.md or .html)")
    ap.add_argument("--csv", metavar="PREFIX",
                    help="write PREFIX-daily.csv and PREFIX-sessions.csv")
    ap.add_argument("--bill", metavar="CSV",
                    help="compare with a bill CSV (date + cost/amount columns)")
    ap.add_argument("--dispute", metavar="FILE",
                    help="with --bill: write a billing dispute evidence pack "
                         "(.md or .html): day-by-day gaps, same-day multiple "
                         "charges, log summary, draft message")
    ap.add_argument("--json", action="store_true",
                    help="print JSON instead of text")
    ap.add_argument("--version", action="version", version=VERSION)
    a = ap.parse_args(argv)
    if a.dispute and not a.bill:
        print("--dispute needs --bill (your bill CSV).", file=sys.stderr)
        return 1
    if not os.path.isdir(a.logs):
        print("Log folder not found: %s\nUse --logs to point at your Claude "
              "Code projects folder." % a.logs, file=sys.stderr)
        return 1
    try:
        cfg = load_prices(a.prices)
    except (OSError, ValueError) as e:
        print("Cannot read prices file %s: %s" % (a.prices, e), file=sys.stderr)
        return 1
    try:
        data = filter_range(filter_days(scan(a.logs, cfg), a.days),
                            a.since, a.until)
    except ValueError as e:
        print("Bad date (use YYYY-MM-DD): %s" % e, file=sys.stderr)
        return 1
    try:
        r = analyse(data, cfg, a.bill, a.budget)
    except (OSError, ValueError) as e:
        print("Bill check failed: %s" % e, file=sys.stderr)
        return 1
    opts = {"period": a.period, "breakdown": a.breakdown, "blocks": a.blocks}
    print(to_json(r) if a.json else text_report(r, cfg, a.top, opts))
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
    if a.dispute:
        dp = dispute(data, r, a.bill)
        md = dispute_markdown(dp, cfg)
        content = (dispute_html(md) if a.dispute.lower().endswith(
            (".html", ".htm")) else md)
        with open(a.dispute, "w", encoding="utf-8") as f:
            f.write(content)
        if not a.json:
            print("\nDispute pack written: %s (%d day(s) bill higher, %d "
                  "day(s) with multiple charges)" % (
                      a.dispute, len(dp["high_days"]), len(dp["bursts"])))
    if r["pace"] and r["pace"]["over_budget"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
