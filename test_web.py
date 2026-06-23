"""Tests for the HTTP status bridge (web.py)."""
import configparser
import http.client
import json
import os
import threading
import unittest
from unittest import mock

from status import Snapshot
from web import _Handler, _Server


HERE = os.path.dirname(os.path.abspath(__file__))


class TestWebServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _Server(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp, body

    def test_status_returns_snapshot_fields(self):
        snap = Snapshot(remaining_seconds=1800.0, max_seconds=3600.0,
                        is_active=True, refill_rate=1.0, history="2.0h today")
        with mock.patch.object(Snapshot, "read", return_value=snap):
            resp, body = self._get("/status")
        self.assertEqual(resp.status, 200)
        self.assertIn("application/json", resp.getheader("Content-Type"))
        data = json.loads(body)
        self.assertAlmostEqual(data["remaining_seconds"], 1800.0)
        self.assertAlmostEqual(data["max_seconds"], 3600.0)
        self.assertTrue(data["is_active"])
        self.assertNotIn("offline", data)

    def test_status_offline_when_no_snapshot(self):
        with mock.patch.object(Snapshot, "read", return_value=None):
            resp, body = self._get("/status")
        self.assertEqual(resp.status, 200)
        data = json.loads(body)
        self.assertTrue(data["offline"])

    def test_status_content_length_matches_body(self):
        with mock.patch.object(Snapshot, "read", return_value=None):
            resp, body = self._get("/status")
        self.assertEqual(int(resp.getheader("Content-Length")), len(body))

    def test_root_returns_html(self):
        resp, body = self._get("/")
        self.assertEqual(resp.status, 200)
        self.assertIn("text/html", resp.getheader("Content-Type"))
        text = body.decode()
        self.assertIn("breaktimer", text)
        self.assertIn("/status", text)   # JS polls /status

    def test_index_html_alias(self):
        resp, _ = self._get("/index.html")
        self.assertEqual(resp.status, 200)

    def test_html_content_length_matches_body(self):
        resp, body = self._get("/")
        self.assertEqual(int(resp.getheader("Content-Length")), len(body))

    def test_unknown_path_returns_404(self):
        resp, _ = self._get("/not-a-real-path")
        self.assertEqual(resp.status, 404)

    def test_grace_remaining_included_in_status(self):
        snap = Snapshot(remaining_seconds=0.0, max_seconds=3600.0, grace_remaining=45.0)
        with mock.patch.object(Snapshot, "read", return_value=snap):
            _, body = self._get("/status")
        data = json.loads(body)
        self.assertAlmostEqual(data["grace_remaining"], 45.0)

    def test_null_grace_remaining_in_status(self):
        snap = Snapshot(remaining_seconds=1800.0, max_seconds=3600.0, grace_remaining=None)
        with mock.patch.object(Snapshot, "read", return_value=snap):
            _, body = self._get("/status")
        data = json.loads(body)
        self.assertIsNone(data["grace_remaining"])


def _load_service(filename):
    cfg = configparser.RawConfigParser(strict=False)
    cfg.read(os.path.join(HERE, filename))
    result = {}
    for section in cfg.sections():
        for k, v in cfg.items(section):
            result[k.lower()] = v
    return result


class TestWebServiceConfig(unittest.TestCase):
    def test_web_never_gives_up_restarting(self):
        cfg = _load_service("breaktimer-web.service")
        self.assertEqual(cfg.get("startlimitintervalsec"), "0",
                         "web must set StartLimitIntervalSec=0 so it retries indefinitely")

    def test_web_restarts_on_any_exit(self):
        cfg = _load_service("breaktimer-web.service")
        self.assertEqual(cfg.get("restart"), "always")


if __name__ == "__main__":
    unittest.main()
