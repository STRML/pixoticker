#!/usr/bin/env python3
"""A server that answers /metrics.json with an error is a misconfiguration, and the agent says so in its log:
python3 board/test_metrics_read.py

A router in front of two groups once answered 200 from one and 401 from the other on alternate reads, and the
panel flipped between 0 and -- while the agent's log stayed empty.
"""
import http.server
import json
import os
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import agent  # noqa: E402

GOOD = {"counters": {}, "gauges": {"requests_running": 0}, "histograms": {}}


def serve(statuses):
    """A server that answers its requests with `statuses` in turn (200 sends GOOD, anything else is an error)."""
    answers = iter(statuses)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            code = next(answers)
            body = json.dumps(GOOD if code == 200 else {"error": "missing or invalid API key"}).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}/metrics.json"


class FetchReportsARefusal(unittest.TestCase):
    def test_an_error_status_is_reported_and_the_read_is_none(self):
        server, url = serve([401, 200, 401])
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        seen = []
        got = [agent.fetch_metrics(url, refused=seen.append) for _ in range(3)]
        self.assertEqual([g is None for g in got], [True, False, True])
        self.assertEqual([e.code for e in seen], [401, 401])

    def test_no_server_at_all_is_not_a_refusal(self):
        server, url = serve([])
        server.server_close()
        seen = []
        self.assertIsNone(agent.fetch_metrics(url, refused=seen.append))
        self.assertEqual(seen, [])


class FetchSendsTheKey(unittest.TestCase):
    KEY = "k" * 24

    def serve_locked(self):
        key = self.KEY

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                ok = self.headers.get("x-api-key") == key
                body = json.dumps(GOOD if ok else {"error": "missing or invalid API key"}).encode()
                self.send_response(200 if ok else 401)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}/metrics.json"

    def test_a_key_opens_a_locked_server(self):
        url = self.serve_locked()
        self.assertIsNone(agent.fetch_metrics(url))
        self.assertEqual(agent.fetch_metrics(url, key=self.KEY), GOOD)

    def test_the_key_stays_with_the_server_it_was_given_to(self):
        seen = []

        class Elsewhere(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("x-api-key"))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        other = http.server.HTTPServer(("127.0.0.1", 0), Elsewhere)
        threading.Thread(target=other.serve_forever, daemon=True).start()
        self.addCleanup(other.server_close)
        self.addCleanup(other.shutdown)
        target = f"http://127.0.0.1:{other.server_port}/metrics.json"

        class Redirector(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", target)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        front = http.server.HTTPServer(("127.0.0.1", 0), Redirector)
        threading.Thread(target=front.serve_forever, daemon=True).start()
        self.addCleanup(front.server_close)
        self.addCleanup(front.shutdown)
        agent.fetch_metrics(f"http://127.0.0.1:{front.server_port}/metrics.json", key=self.KEY)
        self.assertEqual(seen, [None])

    def test_a_key_file_is_read_with_its_newline_stripped(self):
        with tempfile.NamedTemporaryFile("w", suffix=".key") as f:
            f.write(self.KEY + "\n")
            f.flush()
            self.assertEqual(agent.read_key(f.name), self.KEY)


class RefusalLog(unittest.TestCase):
    def setUp(self):
        self.lines = []
        self.log = agent.RefusalLog(self.lines.append)

    def refuse(self, code, now):
        self.log.note(now, agent.urllib.error.HTTPError("http://x/metrics.json", code, "Unauthorized", {}, None))

    def test_the_first_refusal_is_named_with_its_status_and_url(self):
        self.refuse(401, 0.0)
        self.assertEqual(len(self.lines), 1)
        self.assertIn("401", self.lines[0])
        self.assertIn("http://x/metrics.json", self.lines[0])

    def test_a_refusal_that_alternates_with_good_reads_logs_once_a_minute(self):
        for second in range(0, 59):
            self.refuse(401, float(second))
        self.assertEqual(len(self.lines), 1)
        self.refuse(401, 61.0)
        self.assertEqual(len(self.lines), 2)

    def test_a_different_status_is_logged_at_once(self):
        self.refuse(401, 0.0)
        self.refuse(500, 1.0)
        self.assertEqual(len(self.lines), 2)

    def test_two_statuses_alternating_each_log_once_a_minute(self):
        for second in range(0, 59):
            self.refuse(401 if second % 2 else 503, float(second))
        self.assertEqual(len(self.lines), 2)

    def test_a_log_that_cannot_be_written_does_not_stop_the_agent(self):
        def broken(line):
            raise OSError(9, "Bad file descriptor")
        agent.RefusalLog(broken).note(0.0, agent.urllib.error.HTTPError("http://x/", 401, "Unauthorized", {}, None))


if __name__ == "__main__":
    unittest.main()
