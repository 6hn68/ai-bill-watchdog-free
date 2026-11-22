"""Tests for watchdog.py. Run: python3 -m unittest discover -s tests -v

Fixture logs are generated into a temp folder. Every expected number below
is worked out by hand from prices.json list prices, not by calling the code
under test.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import date

os.environ["TZ"] = "UTC"
time.tzset()

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import watchdog  # noqa: E402

PRICES = os.path.join(ROOT, "prices.json")
SONNET = "claude-sonnet-4-5-20250929"
OPUS = "claude-opus-4-1-20250805"
HAIKU = "claude-haiku-4-5-20251001"
HAIKU55 = "claude-haiku-5-5-20261101"
UNKNOWN = "claude-foo-9-9"


def asst(mid, ts, model, usage, session, tools=(), uuid=None):
    content = [{"type": "tool_use", "id": t[0], "name": t[1], "input": t[2]}
               for t in tools]
    d = {"type": "assistant", "sessionId": session, "uuid": uuid or ("u-" + mid),
         "message": {"id": mid, "role": "assistant", "model": model,
                     "content": content, "usage": usage}}
    if ts:
        d["timestamp"] = ts
    return d


def result(tid, ts, session, error):
    return {"type": "user", "sessionId": session, "timestamp": ts,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tid,
                 "is_error": error, "content": "boom" if error else "ok"}]}}


def u(inp, out, read=0, w5=0, w1=0, split=False):
    d = {"input_tokens": inp, "output_tokens": out,
         "cache_read_input_tokens": read}
    if split:
        d["cache_creation"] = {"ephemeral_5m_input_tokens": w5,
                               "ephemeral_1h_input_tokens": w1}
    else:
        d["cache_creation_input_tokens"] = w5
    return d


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def write(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(r if isinstance(r, str) else json.dumps(r))
            f.write("\n")


def build(root):
    # ---- s1 (proj-a), 2026-11-17, Sonnet 4.5 ----
    # A: 100000*3 + 20000*15 + 500000*0.30 + 100000*3.75 = 1,125,000 -> $1.125
    a = asst("m-a", "2026-11-17T12:00:00Z", SONNET,
             u(100000, 20000, read=500000, w5=100000), "s1")
    # same API message written again as a 2nd line carrying a tool_use block
    a2 = asst("m-a", "2026-11-17T12:00:01Z", SONNET,
              u(100000, 20000, read=500000, w5=100000), "s1",
              tools=[("t-a", "Read", {"file_path": "x.py"})], uuid="u-a-2")
    # B: 50000*3 + 10000*15 + 20000*3.75 + 50000*6 = 675,000 -> $0.675
    b = asst("m-b", "2026-11-17T14:00:00Z", SONNET,
             u(50000, 10000, w5=20000, w1=50000, split=True), "s1")
    write(os.path.join(root, "proj-a", "s1.jsonl"), [a, a2, b])

    # ---- s2 (proj-a), 2026-11-18: Opus once, then a Haiku retry loop ----
    # Opus: 20000*15 + 5000*75 + 100000*1.5 = 825,000 -> $0.825
    rows = [asst("m-c", "2026-11-18T12:00:00Z", OPUS,
                 u(20000, 5000, read=100000), "s2")]
    # each Haiku turn: 10000*1 + 5000*5 + 20000*0.1 + 5000*1.25 = 43,250 -> $0.04325
    for i in range(10):
        tid = "t-loop-%d" % i
        rows.append(asst("m-loop-%d" % i, "2026-11-18T15:%02d:00Z" % i, HAIKU,
                         u(10000, 5000, read=20000, w5=5000), "s2",
                         tools=[(tid, "Bash", {"command": "npm test"})]))
        rows.append(result(tid, "2026-11-18T15:%02d:30Z" % i, "s2", True))
    write(os.path.join(root, "proj-a", "s2.jsonl"), rows)

    # ---- s3 (proj-b), 2026-11-19: messy lines ----
    rows = [asst("m-u%d" % i, "2026-11-19T12:0%d:00Z" % i, UNKNOWN,
                 u(100000, 50000), "s3") for i in range(3)]
    rows.append("THIS IS NOT JSON")
    rows.append({"type": "user", "sessionId": "s3",
                 "timestamp": "2026-11-19T13:00:00Z",
                 "message": {"role": "user", "content": "hi"}})
    nomodel = asst("m-nomodel", "2026-11-19T16:00:00Z", SONNET, u(1000, 500), "s3")
    del nomodel["message"]["model"]
    rows.append(nomodel)
    # no timestamp: falls back to the file date, set below to 2026-11-20
    # 10000*3 + 2000*15 = 60,000 -> $0.06
    rows.append(asst("m-nots", None, SONNET, u(10000, 2000), "s3"))
    p3 = os.path.join(root, "proj-b", "s3.jsonl")
    write(p3, rows)
    t = time.mktime((2026, 11, 20, 12, 0, 0, 0, 0, -1))
    os.utime(p3, (t, t))

    # ---- s4 (proj-b), 2026-11-21: Haiku 5.5 over its 100k long-context line
    # prompt = 50000 + 80000 + 20000 = 150,000 > 100,000 -> long-context prices
    # 50000*0.5 + 4000*2.5 + 80000*0.05 + 20000*0.625 = 51,500 -> $0.0515
    # (standard tier would be 5000 + 2000 + 800 + 2500 = 10,300 -> $0.0103)
    write(os.path.join(root, "proj-b", "s4.jsonl"), [
        asst("m-lc", "2026-11-21T09:00:00Z", HAIKU55,
             u(50000, 4000, read=80000, w5=20000), "s4")])

    # ---- s5 (proj-c), 2026-11-21: repeats that are NOT loops ----
    # A,B,A,B alternating and a run of only 2. Each Sonnet turn
    # 1000*3 + 100*15 = 4,500 -> $0.0045, 6 turns -> $0.027
    seq = [("Bash", {"command": "make"}), ("Read", {"file_path": "a"}),
           ("Bash", {"command": "make"}), ("Read", {"file_path": "a"}),
           ("Grep", {"pattern": "x"}), ("Grep", {"pattern": "x"})]
    rows = []
    for i, (name, inp) in enumerate(seq):
        tid = "t-s5-%d" % i
        rows.append(asst("m-s5-%d" % i, "2026-11-21T10:%02d:00Z" % i, SONNET,
                         u(1000, 100), "s5", tools=[(tid, name, inp)]))
        rows.append(result(tid, "2026-11-21T10:%02d:30Z" % i, "s5", True))
    write(os.path.join(root, "proj-c", "s5.jsonl"), rows)


EXPECTED_DAYS = {
    date(2026, 11, 17): 1.125 + 0.675,
    date(2026, 11, 18): 0.825 + 10 * 0.04325,
    date(2026, 11, 19): 0.0,
    date(2026, 11, 20): 0.06,
    date(2026, 11, 21): 0.0515 + 0.027,
}
EXPECTED_TOTAL = sum(EXPECTED_DAYS.values())   # 3.2


class WatchdogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="watchdog-test-")
        cls.logs = os.path.join(cls.tmp, "projects")
        build(cls.logs)
        cls.cfg = watchdog.load_prices(PRICES)
        cls.data = watchdog.scan(cls.logs, cls.cfg)
        cls.r = watchdog.analyse(cls.data, cls.cfg)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---- totals ----
    def test_daily_costs(self):
        days = self.r["days"]
        self.assertEqual(set(days), set(EXPECTED_DAYS))
        for d, cost in EXPECTED_DAYS.items():
            self.assertAlmostEqual(days[d]["cost"], cost, places=6, msg=str(d))

    def test_total(self):
        self.assertAlmostEqual(self.r["total"], EXPECTED_TOTAL, places=6)

    def test_counters(self):
        c = self.data["counters"]
        self.assertEqual(c["files"], 5)
        self.assertEqual(c["bad_json"], 1)
        self.assertEqual(c["duplicate"], 1)       # the 2nd line of m-a
        self.assertEqual(c["ts_missing"], 1)
        self.assertEqual(c["no_usage"], 1)        # the plain "hi" user line
        self.assertEqual(c["parsed"], 2 + 11 + 5 + 1 + 6)

    def test_multiline_message_counted_once_but_tool_kept(self):
        a = [m for m in self.data["messages"] if m["key"].startswith("m-a")]
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0]["tool_ids"], ["t-a"])

    def test_unpriced_never_zero(self):
        um = self.r["unknown_models"]
        self.assertEqual(um.get(UNKNOWN), 3 * 150000)
        self.assertEqual(um.get("(no model name)"), 1500)
        self.assertEqual(self.r["days"][date(2026, 11, 19)]["unknown"], 451500)

    def test_long_context_tier(self):
        lc = [m for m in self.data["messages"] if m["key"].startswith("m-lc")][0]
        self.assertAlmostEqual(lc["cost"], 0.0515, places=6)

    def test_split_cache_creation(self):
        b = [m for m in self.data["messages"] if m["key"].startswith("m-b")][0]
        self.assertEqual(b["tokens"]["cache_write_1h"], 50000)
        self.assertEqual(b["tokens"]["cache_write_5m"], 20000)
        self.assertAlmostEqual(b["cost"], 0.675, places=6)

    # ---- top sessions ----
    def test_top_sessions_order(self):
        s = self.r["sessions"]
        self.assertEqual([x["session"] for x in s[:2]], ["s1", "s2"])
        self.assertAlmostEqual(s[0]["cost"], 1.8, places=6)
        self.assertAlmostEqual(s[1]["cost"], 1.2575, places=6)
        self.assertEqual(s[1]["messages"], 11)
        self.assertEqual(s[0]["project"], "proj-a")
        self.assertEqual(s[1]["top_model"], OPUS)

    def test_top_limit(self):
        out = watchdog.text_report(self.r, self.cfg, top=2)
        self.assertIn("TOP 2 SESSIONS", out)
        self.assertNotIn("  3  $", out)

    # ---- loops ----
    def test_loop_found(self):
        loops = self.r["loops"]
        self.assertEqual(len(loops), 1)
        lp = loops[0]
        self.assertEqual(lp["session"], "s2")
        self.assertEqual(lp["tool"], "Bash")
        self.assertEqual(lp["repeats"], 10)
        self.assertEqual(lp["errors"], 10)
        self.assertEqual(lp["kind"], "failing")
        self.assertAlmostEqual(lp["cost"], 10 * 0.04325, places=6)
        self.assertAlmostEqual(lp["waste"], 7 * 0.04325, places=6)

    def test_alternating_and_short_runs_are_not_loops(self):
        self.assertFalse(any(l["session"] == "s5" for l in self.r["loops"]))

    def test_loop_threshold_exact(self):
        loops = watchdog.find_loops(self.data, min_run=2, stop_after=1)
        grep = [l for l in loops if l["session"] == "s5"]
        self.assertEqual(len(grep), 1)
        self.assertEqual(grep[0]["tool"], "Grep")
        self.assertAlmostEqual(grep[0]["waste"], 0.0045, places=6)

    # ---- fixes ----
    def test_fixes_first_is_measured_loop(self):
        fixes = self.r["fixes"]
        loop_fix = [f for f in fixes if f["basis"] == "measured"]
        self.assertEqual(len(loop_fix), 1)
        self.assertAlmostEqual(loop_fix[0]["saving"], 0.30275, places=6)
        self.assertIn("CLAUDE.md", loop_fix[0]["do"])

    def test_cheaper_model_whatif(self):
        ch = self.r["cheaper"]
        # Opus 4.1 turn re-priced at Sonnet 4.6:
        # 20000*3 + 5000*15 + 100000*0.3 = 165,000 -> $0.165; saving 0.66
        self.assertEqual(ch["turns"], 1)
        self.assertAlmostEqual(ch["current"], 0.825, places=6)
        self.assertAlmostEqual(ch["alt"], 0.165, places=6)
        self.assertAlmostEqual(ch["saving"], 0.66, places=6)
        fx = [f for f in self.r["fixes"] if f["title"].startswith("Use ")][0]
        self.assertIn("upper bound", fx["basis"])

    def test_fixes_measured_first_then_modelled(self):
        bases = [f["basis"] for f in self.r["fixes"]]
        self.assertEqual(bases[0], "measured")
        self.assertIn("upper bound", bases[1])
        self.assertEqual(bases[2:], ["not quantified", "not quantified"])

    def test_long_context_fix(self):
        titles = [f["title"] for f in self.r["fixes"]]
        self.assertIn("Clear context between tasks", titles)   # s1 had 700k ctx
        self.assertIn("Add missing prices", titles)

    def test_clean_logs_no_fixes(self):
        tmp = tempfile.mkdtemp()
        try:
            write(os.path.join(tmp, "p", "x.jsonl"), [asst(
                "m1", "2026-11-21T10:00:00Z", SONNET, u(1000, 100), "x")])
            r = watchdog.analyse(watchdog.scan(tmp, self.cfg), self.cfg)
            self.assertEqual(r["fixes"], [])
            self.assertIn("looks clean", watchdog.text_report(r, self.cfg))
        finally:
            shutil.rmtree(tmp)

    # ---- bill check ----
    def test_bill_reconcile(self):
        bill = os.path.join(self.tmp, "bill.csv")
        with open(bill, "w") as f:
            f.write("Date,Cost (USD)\n2026-11-17,$2.30\n2026-11-18,1.2575\n"
                    "2026-11-22,4.00\n")
        rows = {d: (lg, bl, gap) for d, lg, bl, gap in
                watchdog.reconcile(self.r["days"], bill)}
        self.assertAlmostEqual(rows[date(2026, 11, 17)][2], 0.5, places=6)
        self.assertAlmostEqual(rows[date(2026, 11, 18)][2], 0.0, places=6)
        self.assertAlmostEqual(rows[date(2026, 11, 22)][0], 0.0, places=6)
        self.assertAlmostEqual(rows[date(2026, 11, 22)][2], 4.0, places=6)

    def test_bill_bad_columns(self):
        bill = os.path.join(self.tmp, "bad.csv")
        with open(bill, "w") as f:
            f.write("when,what\nx,y\n")
        with self.assertRaises(ValueError):
            watchdog.reconcile(self.r["days"], bill)

    # ---- days filter ----
    def test_days_filter(self):
        d = watchdog.filter_days(self.data, 2)
        r = watchdog.analyse(d, self.cfg)
        self.assertEqual(set(r["days"]), {date(2026, 11, 20), date(2026, 11, 21)})

    # ---- outputs ----
    def test_reports_and_csv(self):
        md = os.path.join(self.tmp, "r.md")
        ht = os.path.join(self.tmp, "r.html")
        pre = os.path.join(self.tmp, "out")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(watchdog.main(["--logs", self.logs, "--report", md,
                                            "--csv", pre]), 0)
            self.assertEqual(watchdog.main(["--logs", self.logs, "--report", ht]), 0)
        text = read(md)
        self.assertIn("## What to change", text)
        self.assertIn("## Top 5 sessions", text)
        self.assertIn("Measured waste you can stop: $0.30", text)
        page = read(ht)
        self.assertIn("<table>", page)
        self.assertNotIn("<script", page)
        with open(pre + "-sessions.csv") as f:
            self.assertEqual(len(f.read().strip().splitlines()), 1 + 5)
        with open(pre + "-daily.csv") as f:
            self.assertEqual(len(f.read().strip().splitlines()), 1 + 5)

    def test_json_output(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            watchdog.main(["--logs", self.logs, "--json"])
        j = json.loads(buf.getvalue())
        self.assertAlmostEqual(j["total"], EXPECTED_TOTAL, places=6)
        self.assertEqual(j["loops"][0]["repeats"], 10)

    def test_missing_folder_exit_1(self):
        with redirect_stdout(io.StringIO()):
            rc = subprocess.run([sys.executable, os.path.join(ROOT, "watchdog.py"),
                                 "--logs", os.path.join(self.tmp, "nope")],
                                capture_output=True).returncode
        self.assertEqual(rc, 1)

    def test_no_network_imports(self):
        src = read(os.path.join(ROOT, "watchdog.py"))
        for mod in ("urllib", "socket", "http.client", "requests", "ssl"):
            self.assertNotIn("import %s" % mod, src)
            self.assertNotIn("from %s" % mod, src)

    def test_cli_runs_as_script(self):
        p = subprocess.run([sys.executable, os.path.join(ROOT, "watchdog.py"),
                            "--logs", self.logs], capture_output=True, text=True,
                           env=dict(os.environ, TZ="UTC"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("LOOPS", p.stdout)
        self.assertIn("Total: $3.20", p.stdout)


if __name__ == "__main__":
    unittest.main()
