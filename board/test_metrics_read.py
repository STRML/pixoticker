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
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import agent  # noqa: E402

GOOD = {"counters": {}, "gauges": {"requests_running": 0}, "histograms": {}}


def serve(statuses):
    """A server that answers its requests with `statuses` in turn: 200 sends GOOD, another code sends an error
    body, and a (code, body) pair sends that body."""
    answers = iter(statuses)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            code = next(answers)
            code, given = code if isinstance(code, tuple) else (code, None)
            default = GOOD if code == 200 else {"error": "missing or invalid API key"}
            body = json.dumps(default if given is None else given).encode()
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

    def test_a_workers_404_is_its_normal_state_and_is_not_reported(self):
        server, url = serve([(404, {"error": "worker", "follows": "studio1"})])
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        seen = []
        self.assertIsNone(agent.fetch_metrics(url, refused=seen.append))
        self.assertEqual(seen, [])

    def test_a_404_cut_off_mid_body_is_a_failed_read_not_an_exception(self):
        class Cut(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(404)
                self.send_header("Content-Length", "200")
                self.end_headers()
                self.wfile.write(b'{"error": "wor')
                self.wfile.flush()
                self.close_connection = True

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Cut)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        seen = []
        url = f"http://127.0.0.1:{server.server_port}/metrics.json"
        self.assertIsNone(agent.fetch_metrics(url, refused=seen.append))
        self.assertEqual([e.code for e in seen], [404])

    def test_a_404_that_is_not_a_worker_is_reported(self):
        server, url = serve([(404, {"error": "unknown node"})])
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        seen = []
        agent.fetch_metrics(url, refused=seen.append)
        self.assertEqual([e.code for e in seen], [404])

    def test_no_server_at_all_is_not_a_refusal(self):
        server, url = serve([])
        server.server_close()
        seen = []
        self.assertIsNone(agent.fetch_metrics(url, refused=seen.append))
        self.assertEqual(seen, [])


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
