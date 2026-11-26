#!/usr/bin/env python3
"""Tollen firewall (free) -- stop AI spend *before* the request is charged.

A tiny proxy that runs on your own computer. Point Claude Code, the
Anthropic SDK or the OpenAI SDK at it, and every billable request is
priced and checked against your limits before it is forwarded:

  * daily / session / single-request dollar caps (hard stop)
  * repeat-loop breaker (the same last message sent again and again)
  * burst breaker (too many dollars inside a short window)

A blocked request gets HTTP 402 with a plain-English reason. 402, not 429:
SDKs auto-retry 429, so a 429 cap gets hammered; 402 is not retried.

Your API key is passed straight through to the provider over HTTPS. It is
never stored, logged or sent anywhere else. Prompts are never logged.
The ledger keeps only time, model, token counts, dollars and the decision,
and `firewall.py reconcile --bill billing.csv` lines it up with your bill.

Standard library only. One file (plus prices.json and watchdog.py beside it).

  python3 firewall.py --day-usd 20            # start, listens on 127.0.0.1:8787
  export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
  export OPENAI_BASE_URL=http://127.0.0.1:8787/v1
"""

import argparse
import hashlib
import http.client
import json
import os
import sys
import threading
import time
from collections import defaultdict, deque
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import watchdog as wd  # noqa: E402  (shared prices + bill reconciliation)

VERSION = "0.1.0"
DEFAULT_LEDGER = os.path.expanduser("~/.tollen/firewall-ledger.jsonl")
# Conservative text->token ratio for the pre-flight estimate. Real English
# runs ~4 bytes/token; 3 over-estimates on purpose so the cap holds.
BYTES_PER_TOKEN_EST = 3
DEFAULT_MAX_OUTPUT = 8192
# Headers we never forward or copy back.
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailers", "transfer-encoding", "upgrade", "host",
       "content-length", "accept-encoding"}
ANTHROPIC_PATHS = ("/v1/messages", "/v1/complete")
FREE_PATHS = ("/v1/messages/count_tokens", "/v1/models")
OPENAI_BILLABLE = ("/v1/chat/completions", "/v1/responses", "/v1/completions",
                   "/v1/embeddings")


# --------------------------------------------------------------------------
# pricing
# --------------------------------------------------------------------------

def ceiling_price(cfg):
    """Most expensive known rate per bucket. Used for models we can't price,
    so an unknown model can never slip past a cap as $0."""
    top = {}
    for m in cfg.get("models", []):
        for b in wd.BUCKETS:
            v = m.get(b)
            if isinstance(v, (int, float)):
                top[b] = max(top.get(b, 0), v)
    return top


def price_for(model, cfg):
    p = wd.match_model(model or "", cfg.get("models", []))
    if p is None:
        return ceiling_price(cfg), False
    return p, True


def worst_rate_in(p):
    return max(p.get(b) or 0 for b in ("input", "cache_write_5m",
                                       "cache_write_1h"))


def estimate(body_bytes, req, cfg):
    """Worst-case dollars for one request, before it is sent."""
    model = req.get("model") or ""
    p, known = price_for(model, cfg)
    p = wd.tier_for(p, {"input": len(body_bytes) // BYTES_PER_TOKEN_EST,
                        "output": 0, "cache_read": 0, "cache_write_5m": 0,
                        "cache_write_1h": 0}) or p
    in_tok = len(body_bytes) // BYTES_PER_TOKEN_EST + 1
    out_tok = 0
    for k in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        if isinstance(req.get(k), int) and req[k] > 0:
            out_tok = req[k]
            break
    if not out_tok:
        out_tok = DEFAULT_MAX_OUTPUT
    n = req.get("n") if isinstance(req.get("n"), int) and req["n"] > 1 else 1
    cost = (in_tok * worst_rate_in(p) + out_tok * n * (p.get("output") or 0)) / wd.MTOK
    return cost, model, known


def usage_tokens(u):
    """Anthropic or OpenAI usage dict -> watchdog token buckets."""
    if not isinstance(u, dict):
        return None
    if "input_tokens" in u and ("cache_read_input_tokens" in u
                                or "cache_creation_input_tokens" in u
                                or "cache_creation" in u):
        return wd.parse_usage(u)
    inp = u.get("prompt_tokens", u.get("input_tokens"))
    out = u.get("completion_tokens", u.get("output_tokens"))
    if inp is None and out is None:
        return None
    det = u.get("prompt_tokens_details") or u.get("input_tokens_details") or {}
    cached = wd._num(det.get("cached_tokens")) if isinstance(det, dict) else 0
    inp = wd._num(inp)
    return {"input": max(inp - cached, 0), "output": wd._num(out),
            "cache_read": cached, "cache_write_5m": 0, "cache_write_1h": 0}


def merge_usage(acc, t):
    if t is None:
        return acc
    if acc is None:
        return dict(t)
    return {b: max(acc[b], t[b]) for b in wd.BUCKETS}


def find_usage(obj):
    if not isinstance(obj, dict):
        return None
    for path in (("usage",), ("message", "usage"), ("response", "usage")):
        cur = obj
        for k in path:
            cur = cur.get(k) if isinstance(cur, dict) else None
        t = usage_tokens(cur)
        if t is not None:
            return t
    return None


class UsageScanner:
    """Reads usage out of a JSON body or an SSE stream as it passes by."""

    def __init__(self, limit=32 * 1024 * 1024):
        self.buf = b""
        self.whole = []
        self.size = 0
        self.limit = limit
        self.usage = None

    def feed(self, chunk):
        if self.size < self.limit:
            self.whole.append(chunk)
            self.size += len(chunk)
        self.buf += chunk
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            line = line.strip()
            if line.startswith(b"data:"):
                data = line[5:].strip()
                if data and data != b"[DONE]":
                    try:
                        self.usage = merge_usage(self.usage,
                                                 find_usage(json.loads(data)))
                    except ValueError:
                        pass

    def finish(self):
        if self.usage is None and self.size < self.limit:
            try:
                self.usage = find_usage(json.loads(b"".join(self.whole)))
            except ValueError:
                pass
        return self.usage


# --------------------------------------------------------------------------
# budgets
# --------------------------------------------------------------------------

class Guard:
    def __init__(self, opts, cfg):
        self.o = opts
        self.cfg = cfg
        self.lock = threading.Lock()
        self.day = date.today()
        self.day_spent = 0.0
        self.session_spent = 0.0
        self.inflight = 0.0
        self.recent = deque()           # (t, dollars) for the burst breaker
        self.repeats = defaultdict(deque)  # last-message hash -> times
        self.blocked = 0
        self.allowed = 0
        self._restore()

    def _restore(self):
        """A restart must not hand you a fresh daily budget."""
        try:
            with open(self.o.ledger, encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("day") == self.day.isoformat() and r.get("decision") == "allowed":
                        self.day_spent += float(r.get("cost") or 0)
        except FileNotFoundError:
            pass

    def _roll(self):
        today = date.today()
        if today != self.day:
            self.day, self.day_spent = today, 0.0

    def check(self, est, loop_key, now=None):
        """Reserve or refuse. Returns (ok, reason)."""
        now = time.time() if now is None else now
        o = self.o
        with self.lock:
            self._roll()
            if o.request_usd and est > o.request_usd:
                return False, ("this single request could cost up to %s, over "
                               "your per-request cap of %s (--request-usd)"
                               % (usd(est), usd(o.request_usd)))
            if o.day_usd and self.day_spent + self.inflight + est > o.day_usd:
                return False, ("daily budget %s would be exceeded: spent %s "
                               "today, this request could cost up to %s "
                               "(--day-usd; resets at local midnight)"
                               % (usd(o.day_usd), usd(self.day_spent), usd(est)))
            if o.session_usd and self.session_spent + self.inflight + est > o.session_usd:
                return False, ("session budget %s would be exceeded: spent %s "
                               "since the firewall started (--session-usd; "
                               "restart it to start a new session)"
                               % (usd(o.session_usd), usd(self.session_spent)))
            while self.recent and now - self.recent[0][0] > o.burst_window:
                self.recent.popleft()
            burst = sum(d for _, d in self.recent)
            if o.burst_usd and burst + est > o.burst_usd:
                return False, ("burst breaker: %s spent in the last %d "
                               "seconds, limit %s (--burst-usd). Something may "
                               "be running in a loop"
                               % (usd(burst), o.burst_window, usd(o.burst_usd)))
            if loop_key and o.loop_repeats:
                q = self.repeats[loop_key]
                while q and now - q[0] > o.loop_window:
                    q.popleft()
                if len(q) >= o.loop_repeats:
                    return False, ("loop breaker: the same message was sent %d "
                                   "times in %d seconds (--loop-repeats). An "
                                   "agent is probably retrying the same step"
                                   % (len(q), o.loop_window))
                q.append(now)
            self.inflight += est
            self.recent.append((now, est))
            return True, ""

    def settle(self, est, actual, now=None):
        now = time.time() if now is None else now
        with self.lock:
            self._roll()
            self.inflight = max(self.inflight - est, 0.0)
            self.day_spent += actual
            self.session_spent += actual
            for i in range(len(self.recent) - 1, -1, -1):
                if self.recent[i][1] == est:
                    self.recent[i] = (self.recent[i][0], actual)
                    break


def usd(v):
    return "$%.2f" % v


def last_message_key(req):
    """Hash of the newest message only. Agents resend the full history each
    turn, so the newest message repeating means the same step is retried."""
    msgs = req.get("messages")
    if isinstance(msgs, list) and msgs:
        last = msgs[-1]
    elif "input" in req:
        last = req["input"][-1] if isinstance(req["input"], list) and req["input"] else req["input"]
    else:
        return None
    raw = json.dumps([req.get("model"), last], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# proxy
# --------------------------------------------------------------------------

def provider_for(path, headers):
    p = path.split("?", 1)[0]
    if p.startswith(ANTHROPIC_PATHS) or headers.get("anthropic-version") or headers.get("x-api-key"):
        return "anthropic"
    return "openai"


def billable(method, path, provider):
    p = path.split("?", 1)[0].rstrip("/")
    if method != "POST" or p.startswith(FREE_PATHS):
        return False
    if provider == "anthropic":
        return p.startswith(ANTHROPIC_PATHS)
    return p.startswith(OPENAI_BILLABLE)


def error_body(provider, reason):
    msg = "Blocked by your local Tollen firewall before anything was charged: " + reason + "."
    if provider == "anthropic":
        return {"type": "error", "error": {"type": "budget_exceeded", "message": msg}}
    return {"error": {"message": msg, "type": "budget_exceeded",
                      "code": "budget_exceeded", "param": None}}


class Ledger:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)

    def write(self, row):
        with self.lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")


def make_handler(guard, ledger, upstreams, cfg, log):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"
        server_version = "tollen-firewall/" + VERSION

        def log_message(self, *a):  # quiet; the ledger is the record
            pass

        def _send_json(self, code, obj, extra=None):
            data = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _proxy(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
            hdr = {k.lower(): v for k, v in self.headers.items()}
            prov = provider_for(self.path, hdr)
            path = self.path
            if prov == "openai" and not path.startswith("/v1") and upstreams["openai_prefix_v1"]:
                path = "/v1" + path
            bill = billable(self.command, path, prov)
            est, model, known, ok, reason = 0.0, "", True, True, ""
            req = {}
            if bill:
                try:
                    req = json.loads(body or b"{}")
                except ValueError:
                    req = {}
                est, model, known = estimate(body, req, cfg)
                ok, reason = guard.check(est, last_message_key(req))
            base = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "day": date.today().isoformat(), "provider": prov,
                    "path": path.split("?", 1)[0], "model": model,
                    "estimate": round(est, 6), "priced": known}
            if bill and not ok:
                guard.blocked += 1
                ledger.write(dict(base, decision="blocked", reason=reason, cost=0))
                log("BLOCKED %s %s: %s" % (prov, model or "?", reason))
                self._send_json(402, error_body(prov, reason),
                                {"x-tollen-reason": reason[:200]})
                return
            up = urlsplit(upstreams[prov])
            conn_cls = http.client.HTTPSConnection if up.scheme == "https" else http.client.HTTPConnection
            conn = conn_cls(up.hostname, up.port, timeout=upstreams["timeout"])
            fwd = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
            fwd["Accept-Encoding"] = "identity"
            fwd["Content-Length"] = str(len(body))
            fwd["Host"] = up.netloc
            scanner = UsageScanner()
            status = 502
            try:
                conn.request(self.command, (up.path.rstrip("/") + path), body=body, headers=fwd)
                resp = conn.getresponse()
                status = resp.status
                self.send_response(resp.status, resp.reason)
                for k, v in resp.getheaders():
                    if k.lower() not in HOP:
                        self.send_header(k, v)
                self.end_headers()
                while True:
                    chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
                    if not chunk:
                        break
                    if bill:
                        scanner.feed(chunk)
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (OSError, http.client.HTTPException) as e:
                if status == 502:
                    try:
                        self._send_json(502, error_body(prov, "could not reach the provider (%s)" % e.__class__.__name__))
                    except OSError:
                        pass
            finally:
                conn.close()
            if not bill:
                return
            toks = scanner.finish()
            if toks is not None:
                p, _ = price_for(model, cfg)
                actual, _ = wd.price_tokens(toks, p)
                src = "usage"
            elif status >= 400:
                actual, src = 0.0, "error-response"
            else:
                actual, src = est, "estimate-kept"  # unknown outcome: stay conservative
            guard.settle(est, actual)
            guard.allowed += 1
            ledger.write(dict(base, decision="allowed", status=status,
                              cost=round(actual, 6), cost_source=src,
                              tokens=toks or {}))

        do_POST = _proxy
        do_GET = _proxy
        do_DELETE = _proxy
        do_HEAD = _proxy  # Claude Code sends HEAD /api/hello to warm the connection

    return Handler


# --------------------------------------------------------------------------
# ledger tools
# --------------------------------------------------------------------------

def read_ledger(path):
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except FileNotFoundError:
        pass
    return rows


def daily_from_ledger(rows):
    out = defaultdict(lambda: {"cost": 0.0, "blocked": 0, "requests": 0})
    for r in rows:
        try:
            d = date.fromisoformat(r.get("day", ""))
        except ValueError:
            continue
        if r.get("decision") == "allowed":
            out[d]["cost"] += float(r.get("cost") or 0)
            out[d]["requests"] += 1
        else:
            out[d]["blocked"] += 1
    return out


def cmd_summary(opts):
    rows = read_ledger(opts.ledger)
    days = daily_from_ledger(rows)
    if not days:
        print("No firewall ledger yet at %s" % opts.ledger)
        return 0
    print("day          spent   requests  blocked")
    for d in sorted(days):
        v = days[d]
        print("%s %9s %10d %8d" % (d, usd(v["cost"]), v["requests"], v["blocked"]))
    reasons = defaultdict(int)
    for r in rows:
        if r.get("decision") == "blocked":
            reasons[(r.get("reason") or "").split(":")[0]] += 1
    for k, v in sorted(reasons.items(), key=lambda x: -x[1]):
        print("blocked %3d x %s" % (v, k))
    return 0


def cmd_reconcile(opts):
    days = daily_from_ledger(read_ledger(opts.ledger))
    rows = wd.reconcile(days, opts.bill)
    print("Bill vs every request that passed the firewall")
    print("day          firewall       bill        gap")
    flagged = 0
    for d, logs, bill, gap in rows:
        mark = ""
        if bill > 0 and gap > max(1.0, 0.1 * bill):
            mark, flagged = "  <- bill higher than traffic you sent", flagged + 1
        print("%s %10s %10s %10s%s" % (d, usd(logs), usd(bill), usd(gap), mark))
    if flagged:
        print("\n%d day(s) billed above what went through the firewall. Usually "
              "another machine, another key, or traffic that bypassed it. "
              "Build a dispute pack with: python3 watchdog.py --bill %s "
              "--dispute pack.md" % (flagged, opts.bill))
    return 1 if flagged else 0


def cmd_env(opts):
    host = "http://127.0.0.1:%d" % opts.port
    print("export ANTHROPIC_BASE_URL=%s" % host)
    print("export OPENAI_BASE_URL=%s/v1" % host)
    return 0


def build_parser():
    ap = argparse.ArgumentParser(description="Local spend firewall for Anthropic/OpenAI API calls.")
    ap.add_argument("command", nargs="?", default="run",
                    choices=("run", "summary", "reconcile", "env"))
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--day-usd", type=float, default=20.0, help="daily cap, 0 = off (default 20)")
    ap.add_argument("--session-usd", type=float, default=0.0, help="cap since start, 0 = off")
    ap.add_argument("--request-usd", type=float, default=5.0, help="single-request cap (default 5)")
    ap.add_argument("--burst-usd", type=float, default=5.0, help="dollars allowed per burst window (default 5)")
    ap.add_argument("--burst-window", type=int, default=600, help="seconds (default 600)")
    ap.add_argument("--loop-repeats", type=int, default=5, help="same newest message this many times = loop (default 5)")
    ap.add_argument("--loop-window", type=int, default=600, help="seconds (default 600)")
    ap.add_argument("--ledger", default=DEFAULT_LEDGER)
    ap.add_argument("--prices", default=os.path.join(HERE, "prices.json"))
    ap.add_argument("--bill", help="bill CSV for `reconcile`")
    ap.add_argument("--anthropic-upstream", default="https://api.anthropic.com")
    ap.add_argument("--openai-upstream", default="https://api.openai.com")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--version", action="version", version=VERSION)
    return ap


def serve(opts, ready=None, log=None):
    cfg = wd.load_prices(opts.prices)
    guard = Guard(opts, cfg)
    ledger = Ledger(opts.ledger)
    ups = {"anthropic": opts.anthropic_upstream, "openai": opts.openai_upstream,
           "timeout": opts.timeout, "openai_prefix_v1": True}
    log = log or (lambda m: print(m, file=sys.stderr, flush=True))
    srv = ThreadingHTTPServer(("127.0.0.1", opts.port), make_handler(guard, ledger, ups, cfg, log))
    srv.daemon_threads = True
    srv.guard = guard
    if ready:
        ready(srv)
        return srv
    log("Tollen firewall %s on http://127.0.0.1:%d  (day %s, request %s, burst %s/%ds, loop %dx)"
        % (VERSION, srv.server_address[1], usd(opts.day_usd), usd(opts.request_usd),
           usd(opts.burst_usd), opts.burst_window, opts.loop_repeats))
    log("Point your tools here:  python3 firewall.py env  |  ledger: %s" % opts.ledger)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return srv


def main(argv=None):
    opts = build_parser().parse_args(argv)
    if opts.command == "summary":
        return cmd_summary(opts)
    if opts.command == "reconcile":
        if not opts.bill:
            print("reconcile needs --bill billing.csv", file=sys.stderr)
            return 2
        return cmd_reconcile(opts)
    if opts.command == "env":
        return cmd_env(opts)
    serve(opts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
