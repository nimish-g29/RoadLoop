"""TinyFish client against the mock service (same REST shapes as the real API).

Starts mock_tinyfish in-process on a free port. Needs: starlette, uvicorn, sse-starlette,
python-multipart, requests (all in mock_tinyfish/Dockerfile).
"""
import json
import socket
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

from app.tinyfish.client import TinyFish, TinyFishError, AgentGoalFailed  # noqa: E402
from app.tinyfish import goals  # noqa: E402
from app.core.lifecycle import parse_portal_status, PortalStatus  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Hooks(BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        Hooks.received.append((self.path, json.loads(self.rfile.read(n))))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["MOCK_RUN_SECONDS"] = "0.3"
        os.environ["MOCK_AUTO_TICK"] = "0"
        import uvicorn
        cls.port = free_port()
        cfg = uvicorn.Config(_load_mock().app, host="127.0.0.1", port=cls.port, log_level="warning")
        cls.server = uvicorn.Server(cfg)
        threading.Thread(target=cls.server.run, daemon=True).start()
        cls.hook_port = free_port()
        cls.hooks = HTTPServer(("127.0.0.1", cls.hook_port), Hooks)
        threading.Thread(target=cls.hooks.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{cls.port}"
        for _ in range(100):
            try:
                import requests
                requests.get(base + "/health", timeout=0.2)
                break
            except Exception:
                time.sleep(0.05)
        cls.base = base
        cls.tf = TinyFish("test-key", agent_base=base, search_base=base + "/search", fetch_base=base + "/fetch",
                          max_retries=0)
        cls.portals = goals.load_portals(base)

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.hooks.shutdown()

    def _complaint(self):
        return goals.Complaint("Aman Kumar", "9999999999", 28.5459, 77.2732, "Okhla Phase 3, New Delhi",
                               "high", "2026-11-01", 6, "https://example.org/p/1.jpg")

    def test_missing_key(self):
        bad = TinyFish("", agent_base=self.base, max_retries=0)
        bad.s.headers.pop("X-API-Key")
        with self.assertRaises(TinyFishError) as cm:
            bad.get_run("x")
        self.assertEqual(cm.exception.code, "MISSING_API_KEY")

    def test_file_poll_and_webhook(self):
        demo = self.portals["demo"]
        hook = f"http://127.0.0.1:{self.hook_port}/webhooks/tok/agent"
        rid = self.tf.run_async(demo.filing_url, goals.file_goal(demo, self._complaint()),
                                goals.FILE_SCHEMA, webhook_url=hook)
        run = self.tf.wait_for_run(rid, timeout_s=10, poll_s=0.1)
        res = TinyFish.agent_result(run)
        self.assertEqual(res["status"], "filed")
        self.assertTrue(res["complaint_id"].startswith("DMG-"))
        time.sleep(0.3)
        self.assertTrue(any(p.endswith("/agent") and b["run_id"] == rid for p, b in Hooks.received))

        # status: public page -> Fetch -> parser
        page = self.tf.fetch([demo.status_url_for(res["complaint_id"])])
        text = page["results"][0]["text"]
        self.assertEqual(parse_portal_status(text, res["complaint_id"]), PortalStatus.OPEN)

        # status: agent lookup (for login-only trackers)
        run2 = self.tf.run(demo.filing_url, goals.status_goal(res["complaint_id"]), goals.STATUS_SCHEMA)
        self.assertEqual(TinyFish.agent_result(run2)["status_label"], "Pending")

    def test_goal_failure_is_raised(self):
        run = self.tf.run("https://example.org", "do something odd")
        with self.assertRaises(AgentGoalFailed):
            TinyFish.agent_result(run)

    def test_sse_stream(self):
        demo = self.portals["demo"]
        events = [e["type"] for e in self.tf.stream(demo.filing_url, goals.file_goal(demo, self._complaint()))]
        self.assertEqual(events[0], "STARTED")
        self.assertIn("PROGRESS", events)
        self.assertEqual(events[-1], "COMPLETE")

    def test_monitor_lifecycle_and_webhook(self):
        demo = self.portals["demo"]
        rid = self.tf.run_async(demo.filing_url, goals.file_goal(demo, self._complaint()), goals.FILE_SCHEMA)
        cid = TinyFish.agent_result(self.tf.wait_for_run(rid, timeout_s=10, poll_s=0.1))["complaint_id"]
        hook = f"http://127.0.0.1:{self.hook_port}/webhooks/tok/monitor"
        created = self.tf.create_page_monitor(f"status {cid}", demo.status_url_for(cid), "0 */6 * * *",
                                              purpose="Complaint status becomes Closed or Resolved", webhook_url=hook)
        mid = created["monitor"]["id"]
        self.assertTrue(created["run"]["is_baseline"])
        self.assertIn(mid, [m["id"] for m in self.tf.list_monitors()])
        self.tf.set_monitor_status(mid, "paused")
        self.tf.set_monitor_status(mid, "active")
        import requests
        requests.post(f"{self.base}/portal/admin/{cid}", data={"status": "Closed"}, allow_redirects=False)
        requests.post(f"{self.base}/mock/monitors/tick")
        time.sleep(0.3)
        payloads = [b for p, b in Hooks.received if p.endswith("/monitor") and b.get("fetch_monitor_id") == mid]
        self.assertTrue(payloads)
        self.assertEqual(parse_portal_status(payloads[-1]["results"][0]["text"], cid), PortalStatus.CLOSED)
        self.assertTrue(self.tf.delete_monitor(mid)["deleted"])

    def test_search_and_fetch_validation(self):
        self.assertTrue(self.tf.search("who maintains Outer Ring Road Delhi"))
        with self.assertRaises(ValueError):
            self.tf.fetch([])


def _load_mock():
    import importlib.util
    path = next((p for p in (ROOT / "mock_tinyfish" / "app.py", Path("/mock_tinyfish/app.py")) if p.exists()), None)
    if path is None:
        raise unittest.SkipTest("mock_tinyfish/app.py not found")
    spec = importlib.util.spec_from_file_location("mock_tinyfish_app", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


if __name__ == "__main__":
    unittest.main()
