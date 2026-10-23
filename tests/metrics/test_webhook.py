"""The metrics alert webhook (vws-deploy#416): when an alert fires, metrics.py POSTs
{"kind": "metrics-alert", "summary", "comment_url"} to VWS_ALERT_WEBHOOK_URL with the key from
VWS_ALERT_WEBHOOK_KEY. Both come from the environment only; either unset skips it; a failed POST is
logged without the key or URL and never fails the run. Served by a local HTTP server on 127.0.0.1.
Run: python3 -m unittest discover -s tests/metrics"""
import contextlib
import http.server
import io
import json
import os
import threading
import unittest
from unittest import mock

from metrics_testlib import DAY, metrics
from test_runtime import D, Env, routes

ALERT = [{"kind": "stall", "count": 1, "threshold_hours": 48, "items": [{"target": "vws-iam#7", "idle_hours": 50.0}]}]
KEY = "whk_test_key_never_logged_8c1f"


class Hook(http.server.BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - http.server API
        srv = self.server
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        srv.got.append({"path": self.path, "auth": self.headers.get("Authorization"),
                        "type": self.headers.get("Content-Type"), "body": json.loads(body)})
        code = srv.codes.pop(0) if srv.codes else 204
        self.send_response(code)
        if 300 <= code < 400:
            self.send_header("Location", f"http://127.0.0.1:{srv.server_address[1]}/elsewhere")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


class Webhook(Env):
    def setUp(self):
        super().setUp()
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), Hook)
        self.srv.got, self.srv.codes = [], []
        t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        t.start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/hook"
        self.sleeps = mock.Mock()
        real = metrics.alert_webhook
        hook = mock.patch.object(metrics, "alert_webhook",
                                 lambda *a, **kw: real(*a, **dict(kw, sleep=self.sleeps)))
        hook.start()
        self.addCleanup(hook.stop)

    def alert(self, **env):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(metrics, "anomalies", return_value=ALERT), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = self.run_m("--date", DAY, METRICS_MODE="apply", **env)
        self.printed = out.getvalue() + err.getvalue()
        return rc

    def log_text(self):
        with open(os.path.join(self.state, "log", "metrics.jsonl"), encoding="utf-8") as f:
            return f.read()

    def assert_no_secret_leak(self):
        for where, text in (("log", self.log_text()), ("stdout/stderr", self.printed)):
            self.assertNotIn(KEY, text, f"the key is never in the {where}")
            self.assertNotIn(self.url, text, f"the URL is never in the {where}")

    def test_set_posts_once_after_the_comment(self):
        r = routes()
        r[f"POST {D}/issues/415/comments"] = {"status": 201, "body": {
            "id": 9, "html_url": "https://github.com/popododo0720/vws-deploy/issues/415#issuecomment-9"}}
        self.set_routes(r)
        self.assertEqual(self.alert(VWS_ALERT_WEBHOOK_URL=self.url, VWS_ALERT_WEBHOOK_KEY=KEY), 0)
        self.assertEqual(len(self.srv.got), 1)
        got = self.srv.got[0]
        self.assertEqual((got["path"], got["auth"], got["type"]), ("/hook", f"Bearer {KEY}", "application/json"))
        self.assertEqual(got["body"]["kind"], "metrics-alert")
        self.assertIn("stall", got["body"]["summary"])
        self.assertIn(DAY, got["body"]["summary"])
        self.assertEqual(got["body"]["comment_url"], "https://github.com/popododo0720/vws-deploy/issues/415#issuecomment-9")
        self.assertEqual(set(got["body"]), {"kind", "summary", "comment_url"})
        self.assertIn("metrics_webhook_sent", self.events())
        self.assert_no_secret_leak()
        # The same alert again (state remembers it): no second comment, no second webhook.
        self.assertEqual(self.alert(VWS_ALERT_WEBHOOK_URL=self.url, VWS_ALERT_WEBHOOK_KEY=KEY), 0)
        self.assertEqual(len(self.srv.got), 1)

    def test_unset_is_skipped_and_the_run_succeeds(self):
        for env in ({}, {"VWS_ALERT_WEBHOOK_URL": self.url}, {"VWS_ALERT_WEBHOOK_KEY": KEY},
                    {"VWS_ALERT_WEBHOOK_URL": "", "VWS_ALERT_WEBHOOK_KEY": KEY}):
            with self.subTest(env=sorted(env)):
                os.remove(os.path.join(self.state, "metrics-state.json")) if os.path.exists(
                    os.path.join(self.state, "metrics-state.json")) else None
                self.assertEqual(self.alert(**env), 0)
                self.assertIn("metrics_alerted", self.events())
                self.assertIn("metrics_webhook_skipped", self.events())
        self.assertEqual(self.srv.got, [], "nothing is sent without both the URL and the key")

    def test_env_file_values_are_ignored(self):
        os.makedirs(self.env["ORCHESTRATE_CONFIG_DIR"], exist_ok=True)
        with open(os.path.join(self.env["ORCHESTRATE_CONFIG_DIR"], "env"), "w") as f:
            f.write(f"VWS_ALERT_WEBHOOK_URL={self.url}\nVWS_ALERT_WEBHOOK_KEY={KEY}\n")
        self.assertEqual(self.alert(), 0)
        self.assertEqual(self.srv.got, [], "the URL and key come from the process environment only")

    def test_server_error_is_retried_a_bounded_number_of_times_and_never_fails_the_run(self):
        self.srv.codes = [500] * 10
        self.assertEqual(self.alert(VWS_ALERT_WEBHOOK_URL=self.url, VWS_ALERT_WEBHOOK_KEY=KEY), 0)
        self.assertEqual(len(self.srv.got), metrics.WEBHOOK_TRIES)
        self.assertEqual([c.args[0] for c in self.sleeps.call_args_list], [1, 2], "backoff 1 s, then 2 s")
        failed = [e for e in self.log() if e["event"] == "metrics_webhook_failed"]
        self.assertEqual((failed[0]["http_status"], failed[0]["attempts"]), (500, metrics.WEBHOOK_TRIES))
        self.assert_no_secret_leak()

    def test_client_error_is_not_retried(self):
        self.srv.codes = [401]
        self.assertEqual(self.alert(VWS_ALERT_WEBHOOK_URL=self.url, VWS_ALERT_WEBHOOK_KEY=KEY), 0)
        self.assertEqual(len(self.srv.got), 1)
        self.assertIn("metrics_webhook_failed", self.events())
        self.assert_no_secret_leak()

    def test_unreachable_endpoint_is_logged_and_the_run_succeeds(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.assertEqual(self.alert(VWS_ALERT_WEBHOOK_URL=self.url, VWS_ALERT_WEBHOOK_KEY=KEY), 0)
        failed = [e for e in self.log() if e["event"] == "metrics_webhook_failed"]
        self.assertEqual(failed[0]["attempts"], metrics.WEBHOOK_TRIES)
        self.assertTrue(failed[0]["error"])
        self.assert_no_secret_leak()

    def test_redirect_is_not_followed(self):
        self.srv.codes = [307]
        self.assertEqual(self.alert(VWS_ALERT_WEBHOOK_URL=self.url, VWS_ALERT_WEBHOOK_KEY=KEY), 0)
        self.assertEqual([g["path"] for g in self.srv.got], ["/hook"], "the key is not resent to a redirect target")
        self.assertIn("metrics_webhook_failed", self.events())

    def test_plain_http_to_a_remote_host_is_refused(self):
        self.assertEqual(self.alert(VWS_ALERT_WEBHOOK_URL="http://example.invalid/hook", VWS_ALERT_WEBHOOK_KEY=KEY), 0)
        skipped = [e for e in self.log() if e["event"] == "metrics_webhook_skipped"]
        self.assertIn("https", skipped[0]["reason"])
        self.assertNotIn("example.invalid", self.log_text())

    def test_a_bad_host_never_leaks_and_never_fails_the_run(self):
        # vws-deploy#431 verdict: Request() raised ValueError with the host text, outside the try, so it
        # reached metrics_failed (log and stderr) and the run exited 4.
        secret = "hooksecret-abc123"
        self.url = f"https://[{secret}]/hook"
        self.assertEqual(self.alert(VWS_ALERT_WEBHOOK_URL=self.url, VWS_ALERT_WEBHOOK_KEY=KEY), 0)
        events = self.events()
        self.assertIn("metrics_alerted", events)
        self.assertNotIn("metrics_failed", events)
        failed = [e for e in self.log() if e["event"] == "metrics_webhook_failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["error"], "ValueError", "only the exception class is logged")
        for where, text in (("log", self.log_text()), ("stdout/stderr", self.printed)):
            self.assertNotIn(secret, text, f"no part of the URL is in the {where}")
        self.assert_no_secret_leak()
        self.assertEqual(self.srv.got, [])

    def test_a_pasted_bearer_header_is_not_doubled(self):
        self.assertEqual(self.alert(VWS_ALERT_WEBHOOK_URL=self.url, VWS_ALERT_WEBHOOK_KEY="Bearer " + KEY), 0)
        self.assertEqual(self.srv.got[0]["auth"], f"Bearer {KEY}")

    def test_no_alert_no_webhook(self):
        self.assertEqual(self.run_m("--date", DAY, METRICS_MODE="apply", VWS_ALERT_WEBHOOK_URL=self.url,
                                    VWS_ALERT_WEBHOOK_KEY=KEY), 0)
        self.assertEqual(self.srv.got, [])
        self.assertNotIn("metrics_webhook_skipped", self.events())

    def test_dry_run_sends_nothing(self):
        with mock.patch.object(metrics, "anomalies", return_value=ALERT):
            self.assertEqual(self.run_m("--date", DAY, VWS_ALERT_WEBHOOK_URL=self.url, VWS_ALERT_WEBHOOK_KEY=KEY), 0)
        self.assertEqual(self.srv.got, [])


if __name__ == "__main__":
    unittest.main()
