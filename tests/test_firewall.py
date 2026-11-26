import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import firewall as fw  # noqa: E402

FAKE_KEY = "sk-ant-TESTKEY-should-never-be-logged"


class Upstream(BaseHTTPRequestHandler):
    calls = []
    mode = "json"

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        Upstream.calls.append((self.path, dict(self.headers), body))
        if Upstream.mode == "sse":
            events = [
                {"type": "message_start", "message": {"usage": {"input_tokens": 1000, "output_tokens": 1,
                                                                 "cache_read_input_tokens": 0,
                                                                 "cache_creation_input_tokens": 0}}},
                {"type": "content_block_delta", "delta": {"text": "hi"}},
                {"type": "message_delta", "usage": {"output_tokens": 500}},
            ]
            data = b"".join(b"event: x\ndata: " + json.dumps(e).encode() + b"\n\n" for e in events)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.startswith("/v1/chat"):
            out = {"choices": [], "usage": {"prompt_tokens": 2000, "completion_tokens": 100,
                                            "prompt_tokens_details": {"cached_tokens": 1000}}}
        else:
            out = {"content": [], "usage": {"input_tokens": 1000, "output_tokens": 200,
                                            "cache_read_input_tokens": 0,
                                            "cache_creation_input_tokens": 0}}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class FirewallTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=cls.up.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.up.shutdown()

    def start(self, *extra):
        self.tmp = tempfile.mkdtemp()
        self.ledger = os.path.join(self.tmp, "ledger.jsonl")
        url = "http://127.0.0.1:%d" % self.up.server_address[1]
        argv = ["--port", "0", "--ledger", self.ledger, "--anthropic-upstream", url,
                "--openai-upstream", url] + list(extra)
        opts = fw.build_parser().parse_args(argv)
        holder = {}
        fw.serve(opts, ready=lambda s: holder.setdefault("s", s), log=lambda m: None)
        srv = holder["s"]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.port = srv.server_address[1]
        self.srv = srv
        Upstream.calls = []
        Upstream.mode = "json"

    def post(self, path, payload, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Content-Type": "application/json", "x-api-key": FAKE_KEY,
             "anthropic-version": "2023-06-01"}
        if path.startswith("/v1/chat"):
            h = {"Content-Type": "application/json", "Authorization": "Bearer " + FAKE_KEY}
        h.update(headers or {})
        c.request("POST", path, body=json.dumps(payload), headers=h)
        r = c.getresponse()
        return r.status, r.read(), dict(r.getheaders())

    def msg(self, text="hello", model="claude-sonnet-4-6", max_tokens=1000):
        return {"model": model, "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": text}]}

    def rows(self):
        return fw.read_ledger(self.ledger)

    def test_allowed_request_settles_with_real_usage(self):
        self.start()
        st, body, _ = self.post("/v1/messages", self.msg())
        self.assertEqual(st, 200)
        self.assertEqual(len(Upstream.calls), 1)
        r = self.rows()[-1]
        self.assertEqual(r["decision"], "allowed")
        self.assertEqual(r["cost_source"], "usage")
        # 1000 in * $3 + 200 out * $15 per MTok on Sonnet 4.6
        self.assertAlmostEqual(r["cost"], 0.006, places=6)

    def test_key_and_prompt_never_in_ledger(self):
        self.start()
        self.post("/v1/messages", self.msg("my secret prompt text"))
        raw = open(self.ledger, encoding="utf-8").read()
        self.assertNotIn("TESTKEY", raw)
        self.assertNotIn("secret prompt", raw)

    def test_key_passed_through_to_provider(self):
        self.start()
        self.post("/v1/messages", self.msg())
        hdrs = {k.lower(): v for k, v in Upstream.calls[-1][1].items()}
        self.assertEqual(hdrs.get("x-api-key"), FAKE_KEY)

    def test_request_cap_blocks_before_provider_with_402(self):
        self.start("--request-usd", "0.01")
        st, body, h = self.post("/v1/messages", self.msg(max_tokens=64000))
        self.assertEqual(st, 402)
        self.assertEqual(Upstream.calls, [])
        err = json.loads(body)
        self.assertEqual(err["type"], "error")
        self.assertEqual(err["error"]["type"], "budget_exceeded")
        self.assertIn("per-request cap", err["error"]["message"])
        self.assertEqual(self.rows()[-1]["decision"], "blocked")

    def test_openai_shape_error(self):
        self.start("--request-usd", "0.0001")
        st, body, _ = self.post("/v1/chat/completions",
                                {"model": "gpt-x", "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(st, 402)
        self.assertEqual(json.loads(body)["error"]["code"], "budget_exceeded")

    def test_daily_cap_and_restart_keeps_spend(self):
        self.start("--day-usd", "0.05", "--request-usd", "0", "--burst-usd", "0")
        st, _, _ = self.post("/v1/messages", self.msg(max_tokens=100))
        self.assertEqual(st, 200)
        # pretend a lot was already spent today and restart
        with open(self.ledger, "a", encoding="utf-8") as f:
            f.write(json.dumps({"day": fw.date.today().isoformat(), "decision": "allowed",
                                "cost": 0.049}) + "\n")
        ledger = self.ledger
        url = "http://127.0.0.1:%d" % self.up.server_address[1]
        opts = fw.build_parser().parse_args(["--port", "0", "--ledger", ledger, "--day-usd", "0.05",
                                             "--request-usd", "0", "--burst-usd", "0",
                                             "--anthropic-upstream", url])
        h = {}
        fw.serve(opts, ready=lambda s: h.setdefault("s", s), log=lambda m: None)
        threading.Thread(target=h["s"].serve_forever, daemon=True).start()
        self.addCleanup(h["s"].shutdown)
        self.port = h["s"].server_address[1]
        st, body, _ = self.post("/v1/messages", self.msg(max_tokens=100))
        self.assertEqual(st, 402)
        self.assertIn("daily budget", json.loads(body)["error"]["message"])

    def test_loop_breaker(self):
        self.start("--loop-repeats", "3", "--burst-usd", "0", "--day-usd", "0")
        for _ in range(3):
            st, _, _ = self.post("/v1/messages", self.msg("same tool result"))
            self.assertEqual(st, 200)
        st, body, _ = self.post("/v1/messages", self.msg("same tool result"))
        self.assertEqual(st, 402)
        self.assertIn("loop breaker", json.loads(body)["error"]["message"])
        st, _, _ = self.post("/v1/messages", self.msg("a different step"))
        self.assertEqual(st, 200)

    def test_burst_breaker(self):
        self.start("--burst-usd", "0.03", "--day-usd", "0", "--request-usd", "0", "--loop-repeats", "0")
        codes = [self.post("/v1/messages", self.msg("step %d" % i, max_tokens=100))[0] for i in range(12)]
        self.assertIn(402, codes)
        self.assertEqual(codes[0], 200)

    def test_streaming_usage_parsed_and_bytes_unchanged(self):
        self.start()
        Upstream.mode = "sse"
        p = self.msg()
        p["stream"] = True
        st, body, _ = self.post("/v1/messages", p)
        self.assertEqual(st, 200)
        self.assertIn(b"message_delta", body)
        r = self.rows()[-1]
        self.assertEqual(r["cost_source"], "usage")
        self.assertEqual(r["tokens"]["output"], 500)

    def test_openai_cached_tokens(self):
        self.start()
        st, _, _ = self.post("/v1/chat/completions",
                             {"model": "unknown-model", "messages": [{"role": "user", "content": "x"}],
                              "max_tokens": 100})
        self.assertEqual(st, 200)
        r = self.rows()[-1]
        self.assertFalse(r["priced"])      # unknown model priced at the ceiling, never $0
        self.assertEqual(r["tokens"]["cache_read"], 1000)
        self.assertGreater(r["cost"], 0)

    def test_count_tokens_is_free(self):
        self.start("--request-usd", "0.0000001")
        st, _, _ = self.post("/v1/messages/count_tokens", self.msg())
        self.assertEqual(st, 200)
        self.assertEqual(self.rows(), [])

    def test_reconcile_flags_bill_above_traffic(self):
        self.start()
        self.post("/v1/messages", self.msg())
        bill = os.path.join(self.tmp, "bill.csv")
        with open(bill, "w") as f:
            f.write("date,amount\n%s,25.00\n" % fw.date.today().isoformat())
        opts = fw.build_parser().parse_args(["reconcile", "--ledger", self.ledger, "--bill", bill])
        self.assertEqual(fw.cmd_reconcile(opts), 1)

    def test_head_probe_passes_through(self):
        self.start()
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("HEAD", "/api/hello")
        self.assertEqual(c.getresponse().status, 200)
        self.assertEqual(self.rows(), [])

    def test_estimate_is_conservative(self):
        cfg = fw.wd.load_prices(os.path.join(os.path.dirname(HERE), "prices.json"))
        body = json.dumps(self.msg("x" * 3000, max_tokens=1000)).encode()
        est, _, known = fw.estimate(body, json.loads(body), cfg)
        self.assertTrue(known)
        # real cost for ~750 input tokens + 1000 output on Sonnet 4.6 is ~$0.017
        self.assertGreater(est, 0.017)


if __name__ == "__main__":
    unittest.main()
