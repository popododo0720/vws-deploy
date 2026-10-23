"""Infra (provider error) retries for DeepSeek dispatches (vws-deploy#333): a [deepseek-dev] Failed
report that shows a provider 429/503 is re-mentioned after ORCHESTRATE_INFRA_RETRY_DELAY_MIN, at most
INFRA_MAX_RETRIES times, never counts toward the grok swap, and ends with one failure comment.
Run: python3 -m unittest discover -s tests/orchestrate"""
import json
import os
import time
import unittest

from orch_testlib import CFG, FIXTURES, NOW, U, comment, dispatch, item, orch, ready_task, report, snapshot
from test_runtime import O, Env, routes

with open(os.path.join(FIXTURES, "deepseek-failed-reports.json"), encoding="utf-8") as _f:
    REAL = _f.read()
REAL_REPORTS = json.loads(REAL)["reports"]
RATE_LIMIT_429 = REAL_REPORTS[0]["body"]   # vws-proto#51 comment 6310291529, 07:13Z
SERVER_503 = REAL_REPORTS[1]["body"]       # vws-proto#51 comment 6311725351, 08:37Z
NOT_INFRA = REAL_REPORTS[2]["body"]        # vws-iam#123 comment 6309297481 (token problem, exit 0)
MID_JOB_503 = REAL_REPORTS[3]["body"]      # synthetic: turn 7, partial work pushed, then a 503
FIRST_LINE_503 = REAL_REPORTS[4]["body"]   # synthetic: the future first-line form
TASK_FAILURE = REAL_REPORTS[5]["body"]     # synthetic: exit 1, HTTP statuses only in test output
MIN = 60


def run(snap, state=None, cfg=None, now=NOW, excl=frozenset()):
    return orch.decide(snap, state or {}, now, cfg or CFG, set(excl))


def kinds(result):
    return [(a["type"], a["target"]) for a in result["actions"]]


def infra_report(cid, repo, number, for_cid, minutes_ago, tail=RATE_LIMIT_429):
    """A Failed report for dispatch for_cid whose body is the real 429 report, retargeted."""
    target = f"{U}/{repo}/issues/{number}#issuecomment-{for_cid}"
    body = f"[deepseek-dev] Failed for {target}: " + tail.split(": ", 1)[1]
    return comment(cid, repo, number, body, minutes_ago / 60)


def attempts(repo, number, n_infra, last_report_minutes_ago, start_cid=1):
    """n_infra dispatches, each followed by an infra Failed report; the last report is the newest."""
    out, cid = [], start_cid
    for i in range(n_infra):
        age = last_report_minutes_ago + (n_infra - 1 - i) * 20
        out.append(dispatch(cid, repo, number, (age + 2) / 60))
        out.append(infra_report(cid + 1, repo, number, cid, age))
        cid += 2
    return out


class Patterns(unittest.TestCase):
    def test_real_reports(self):
        self.assertTrue(orch.INFRA_ERROR_RE.search(RATE_LIMIT_429))
        self.assertTrue(orch.INFRA_ERROR_RE.search(SERVER_503))
        self.assertEqual(orch.infra_error(RATE_LIMIT_429),
                         'dsh: RATE_LIMIT: 429: {"type":"server_error","message":"Upstream request failed: Endpoint is unavailable."}')
        self.assertTrue(orch.infra_error(SERVER_503).startswith("dsh: SERVER: 503:"))
        self.assertEqual(orch.infra_error(NOT_INFRA), "")

    def test_mid_job_and_first_line_forms(self):
        self.assertTrue(orch.infra_error(MID_JOB_503).startswith("dsh: SERVER: 503:"))
        self.assertTrue(orch.infra_error(FIRST_LINE_503).endswith("provider 503 (SERVER)"))
        self.assertEqual(orch.infra_error(TASK_FAILURE), "", "exit 1 without the dsh provider line is a task failure")

    def test_positive(self):
        for text in ("dsh: RATE_LIMIT: 429: {}", "dsh: SERVER: 503: {}", "dsh: SERVER: 500: x", "dsh: RATE_LIMIT: 429:"):
            with self.subTest(text):
                body = f"[deepseek-dev] Failed for x: dsh exited with code 1.\n\nLast output:\n```\n{{}}\n{text}\n```\nLogs"
                self.assertTrue(orch.infra_error(body))
        self.assertTrue(orch.infra_error("[deepseek-dev] Failed for https://x/1#c: provider 429 (RATE_LIMIT)"))

    def test_negative(self):
        for text in ("dsh exited with code 1.", "dsh exited with code 1.\n\nLast output:\n```\ngo test ./...\nFAIL\n```",
                     "see vws-proto#429 and port 5030", "HTTP 503 from the API under test",
                     'got 429: {"code":"Throttling"} in TestThrottle', "RATE_LIMIT exceeded in the vws-iam limiter test",
                     'dsh: TIMEOUT: 504: {"type":"server_error"}', "dsh: AUTH: 401", "dsh: 503", "dsh: server: 503: {}",
                     '"code":"SERVER"', "Upstream request failed: Endpoint is unavailable.",
                     "x\n  dsh: SERVER: 503: indented, not the dsh line", "tests mention provider 503 (SERVER) mid-line", ""):
            with self.subTest(text):
                self.assertEqual(orch.infra_error(f"[deepseek-dev] Failed for x: {text}"), "")

    def test_one_constant(self):
        with open(orch.__file__, encoding="utf-8") as f:
            src = f.read()
        self.assertEqual(src.count("(?:RATE_LIMIT|SERVER)"), 2, "the patterns live in INFRA_ERROR_RE only")

    def test_history_kind(self):
        t = ready_task("vws-iam", 1, comments=attempts("vws-iam", 1, 1, 30))
        h = orch.dispatch_history(t, NOW, CFG, set())
        self.assertEqual([(x["outcome"], x["kind"]) for x in h], [("infra", "provider_error")])
        plain = ready_task("vws-iam", 1, comments=[dispatch(1, "vws-iam", 1, 9), report(2, "vws-iam", 1, 1, "Failed", 8)])
        self.assertEqual([x["outcome"] for x in orch.dispatch_history(plain, NOW, CFG, set())], ["failed"])

    def test_exclusion_still_wins(self):
        c = attempts("vws-iam", 1, 1, 1)
        h = orch.dispatch_history(ready_task("vws-iam", 1, comments=c), NOW, CFG, {c[1]["url"]})
        self.assertEqual([x["outcome"] for x in h], ["excluded"])


class Delay(unittest.TestCase):
    def test_default_is_5_minutes(self):
        self.assertEqual(orch.DEFAULTS["ORCHESTRATE_INFRA_RETRY_DELAY_MIN"], "5")

    def test_not_before_5m_yes_after(self):
        for minutes, want in ((0, []), (4.9, []), (5, [("dispatch", "vws-iam#1")]), (9, [("dispatch", "vws-iam#1")])):
            with self.subTest(minutes=minutes):
                r = run(snapshot(ready_task("vws-iam", 1, comments=attempts("vws-iam", 1, 1, minutes))))
                self.assertEqual(kinds(r), want)
                if not want:
                    wait = [n for n in r["notes"] if n["kind"] == "infra_wait"]
                    self.assertEqual(len(wait), 1)
                    self.assertEqual(wait[0]["retry"], 1)

    def test_timer_runs_from_the_report(self):
        # Dispatch 3h ago, report 4 minutes ago: still waiting.
        c = [dispatch(1, "vws-iam", 1, 3), infra_report(2, "vws-iam", 1, 1, 4)]
        self.assertEqual(kinds(run(snapshot(ready_task("vws-iam", 1, comments=c)))), [])

    def test_configurable(self):
        cfg = dict(CFG, ORCHESTRATE_INFRA_RETRY_DELAY_MIN="10")
        t = ready_task("vws-iam", 1, comments=attempts("vws-iam", 1, 1, 6))
        self.assertEqual(kinds(run(snapshot(t), cfg=cfg)), [])
        self.assertEqual(kinds(run(snapshot(ready_task("vws-iam", 1, comments=attempts("vws-iam", 1, 1, 10))), cfg=cfg)),
                         [("dispatch", "vws-iam#1")])

    def test_must_be_a_positive_integer(self):
        env = {"ORCHESTRATE_CONFIG_DIR": "/nonexistent/vws-orchestrate-test"}
        paths = orch.Paths(env)
        for bad in ("0", "-5", "2.5", "five", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                orch.load_config(paths, dict(env, ORCHESTRATE_INFRA_RETRY_DELAY_MIN=bad))
        self.assertEqual(orch.load_config(paths, dict(env, ORCHESTRATE_INFRA_RETRY_DELAY_MIN="7"))
                         ["ORCHESTRATE_INFRA_RETRY_DELAY_MIN"], "7")


class RetryCount(unittest.TestCase):
    def test_count_from_comments(self):
        for n in (1, 2, 3):
            with self.subTest(n=n):
                r = run(snapshot(ready_task("vws-iam", 1, comments=attempts("vws-iam", 1, n, 6))))
                self.assertEqual(kinds(r), [("dispatch", "vws-iam#1")])
                body = r["actions"][0]["body"]
                self.assertTrue(body.startswith(orch.DISPATCH_TEXT), "same dispatch body")
                self.assertIn(f"Retry {n}/3 after provider error.", body)
                self.assertTrue(body.endswith("<!-- orchestrate:dispatch -->"), "same marker")
                self.assertEqual(orch.classify_comment(body)[0], "dispatch")
                self.assertIn(f"infra retry {n}/3", r["actions"][0]["reason"])

    def test_retry_in_flight_is_not_dispatched_again(self):
        c = attempts("vws-iam", 1, 2, 30) + [dispatch(9, "vws-iam", 1, 0.2)]
        r = run(snapshot(ready_task("vws-iam", 1, comments=c)))
        self.assertEqual(kinds(r), [])
        self.assertIn({"kind": "in_flight", "targets": ["vws-iam#1"]}, r["notes"])

    def test_first_dispatch_has_no_retry_line(self):
        r = run(snapshot(ready_task("vws-iam", 1)))
        self.assertEqual(r["actions"][0]["body"], f"{orch.DISPATCH_TEXT}\n\n<!-- orchestrate:dispatch -->")

    def test_reset_clears_the_count(self):
        c = attempts("vws-iam", 1, 4, 30) + [comment(99, "vws-iam", 1, "/orchestrate reset-failures provider back", 0.1)]
        r = run(snapshot(ready_task("vws-iam", 1, comments=c)))
        self.assertEqual(kinds(r), [("dispatch", "vws-iam#1")])
        self.assertNotIn("Retry", r["actions"][0]["body"])


class Exhaustion(unittest.TestCase):
    def exhausted(self, **kw):
        return ready_task("vws-iam", 1, comments=attempts("vws-iam", 1, 4, 30), **kw)

    def test_posts_one_marker_and_blocks(self):
        r = run(snapshot(self.exhausted()))
        self.assertEqual(kinds(r), [("infra_status", "vws-deploy#415"), ("add_label", "vws-iam#1")])
        a = r["actions"][0]
        lines = a["body"].split("\n")
        self.assertEqual(lines[0], orch.FAILURE_MARKER)
        self.assertIn("Kind: infra-retries-exhausted", lines)
        self.assertIn("Item: vws-iam#1", lines)
        self.assertTrue(any(line.startswith("Last error (redacted): dsh: RATE_LIMIT: 429") for line in lines))
        self.assertIn(f"Idempotency key: `{a['key']}`", lines)
        self.assertNotIn("@deepseek-dev", a["body"])
        self.assertEqual(r["actions"][1]["reason"], "infra retries exhausted")
        self.assertNotIn("swap_label", [t for t, _ in kinds(r)], "never swapped to grok")

    def test_rerun_posts_nothing(self):
        key = run(snapshot(self.exhausted()))["actions"][0]["key"]
        snap = snapshot(self.exhausted(extra=("blocked",)))
        snap["status"]["keys"] = [key]
        r = run(snap, {"owned_labels": {"blocked": ["vws-iam#1"]}})
        self.assertEqual(kinds(r), [], "no second marker, the owned blocked label stays, no dispatch")
        self.assertIn("infra_exhausted", [n["kind"] for n in r["notes"]])

    def test_error_is_redacted(self):
        tail = "x: dsh exited with code 1.\n\nLast output:\n```\ndsh: RATE_LIMIT: 429: token ghp_ABCDEFGHIJKLMNOP1234\n```"
        c = attempts("vws-iam", 1, 3, 50) + [dispatch(20, "vws-iam", 1, 0.6), infra_report(21, "vws-iam", 1, 20, 30, tail=tail)]
        body = run(snapshot(ready_task("vws-iam", 1, comments=c)))["actions"][0]["body"]
        self.assertNotIn("ghp_", body)
        self.assertIn("[redacted]", body)


class Swap(unittest.TestCase):
    def test_infra_never_counts(self):
        # One task failure and three infra failures, the latest still waiting: no swap.
        c = [dispatch(1, "vws-iam", 1, 9), report(2, "vws-iam", 1, 1, "Failed", 8)] + attempts("vws-iam", 1, 3, 2, start_cid=10)
        r = run(snapshot(ready_task("vws-iam", 1, comments=c)))
        self.assertEqual(kinds(r), [])
        c = attempts("vws-iam", 1, 4, 30)
        self.assertNotIn("swap_label", [t for t, _ in kinds(run(snapshot(ready_task("vws-iam", 1, comments=c))))])

    def test_task_failures_still_swap_at_2(self):
        c = ([dispatch(1, "vws-iam", 1, 20), report(2, "vws-iam", 1, 1, "Failed", 19)]
             + attempts("vws-iam", 1, 2, 600, start_cid=10)
             + [dispatch(30, "vws-iam", 1, 5), report(31, "vws-iam", 1, 30, "Failed", 4)])
        self.assertEqual(kinds(run(snapshot(ready_task("vws-iam", 1, comments=c)))), [("swap_label", "vws-iam#1")])

    def test_task_failure_after_infra_redispatches_normally(self):
        c = attempts("vws-iam", 1, 1, 600) + [dispatch(10, "vws-iam", 1, 5), report(11, "vws-iam", 1, 10, "Failed", 4)]
        r = run(snapshot(ready_task("vws-iam", 1, comments=c)))
        self.assertEqual(kinds(r), [("dispatch", "vws-iam#1")])
        self.assertNotIn("Retry", r["actions"][0]["body"])


class Limits(unittest.TestCase):
    def test_cap_respected(self):
        busy = [ready_task("vws-deploy", 10, comments=[dispatch(1, "vws-deploy", 10, 1)]),
                ready_task("vws-proto", 5, comments=[dispatch(2, "vws-proto", 5, 1)])]
        r = run(snapshot(ready_task("vws-iam", 1, comments=attempts("vws-iam", 1, 1, 6)), *busy))
        self.assertEqual(kinds(r), [], "both DeepSeek slots are taken")

    def test_one_task_per_repo(self):
        other = ready_task("vws-iam", 2, comments=[dispatch(50, "vws-iam", 2, 1)])
        r = run(snapshot(ready_task("vws-iam", 1, comments=attempts("vws-iam", 1, 1, 6)), other))
        self.assertEqual(kinds(r), [])
        self.assertIn({"kind": "repo_busy", "target": "vws-iam#1", "repo": "vws-iam"}, r["notes"])

    def test_blocked_dependency_wins(self):
        t = ready_task("vws-iam", 1, comments=attempts("vws-iam", 1, 1, 6), body="Depends on #2")
        r = run(snapshot(t, item("vws-iam", 2)))
        self.assertEqual(kinds(r), [("add_label", "vws-iam#1")])


def iso_ago(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds))


class ApplyTicks(Env):
    URL = "https://github.com/popododo0720/vws-iam/issues/1#issuecomment-"

    def history(self, n, last_report_seconds_ago):
        out = []
        for i in range(n):
            age = last_report_seconds_ago + (n - 1 - i) * 1200
            out.append({"id": 2 * i + 1, "html_url": f"{self.URL}{2 * i + 1}", "created_at": iso_ago(age + 120),
                        "body": orch.DISPATCH_TEXT})
            out.append({"id": 2 * i + 2, "html_url": f"{self.URL}{2 * i + 2}", "created_at": iso_ago(age),
                        "body": f"[deepseek-dev] Failed for {self.URL}{2 * i + 1}: " + RATE_LIMIT_429.split(": ", 1)[1]})
        return out

    def with_history(self, comments, status="<!-- vws-orchestrate:status -->\n<!-- /vws-orchestrate:status -->\n"):
        r = routes()
        r[f"{O}/vws-iam/issues/1/comments"] = {"body": comments}
        r[f"{O}/vws-deploy/issues/415"] = {"body": {"number": 415, "state": "open", "body": status}}
        self.set_routes(r)

    def test_retry_is_a_dispatch_comment_under_the_write_cap(self):
        self.with_history(self.history(1, 400))
        self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_SCOPE="vws-iam#1")
        w = self.writes()
        self.assertEqual([(c["method"], c["path"]) for c in w], [("POST", f"{O}/vws-iam/issues/1/comments")])
        self.assertIn("Retry 1/3 after provider error.", json.loads(w[0]["stdin"])["body"])
        os.remove(self.calls_file)
        self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_SCOPE="vws-iam#1", ORCHESTRATE_MAX_WRITES="0")
        self.assertEqual(self.writes(), [])
        self.assertTrue(any(e["event"] == "skipped" and "write cap 0" in e.get("reason", "") for e in self.log()))

    def test_not_before_the_delay(self):
        self.with_history(self.history(1, 120))
        self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_SCOPE="vws-iam#1")
        self.assertEqual(self.writes(), [])
        self.assertTrue(any(e["event"] == "note" and e.get("kind") == "infra_wait" for e in self.log()))

    def test_exhaustion_posts_one_marker_across_reruns(self):
        self.with_history(self.history(4, 600))
        self.tick(ORCHESTRATE_MODE="apply")
        w = [(c["method"], c["path"]) for c in self.writes()]
        self.assertIn(("POST", f"{O}/vws-iam/issues/1/labels"), w)
        self.assertNotIn(("POST", f"{O}/vws-iam/issues/1/comments"), w, "no dispatch after exhaustion")
        self.assertFalse([c for c in self.writes() if orch.FAILURE_MARKER in c["stdin"]], "no status comment (vws-deploy#416)")
        body = self.status_edits()
        self.assertEqual(len(body), 1)
        self.assertIn("vws-iam#1: infra retries exhausted", body[0])
        # Rerun with the state gone: the entry is in the status issue body now, so it is not added again.
        os.remove(self.paths.state_file)
        self.with_history(self.history(4, 600), status=body[0])
        os.remove(self.calls_file)
        seen = len(self.log())
        self.tick(ORCHESTRATE_MODE="apply")
        self.assertFalse([e for e in self.log()[seen:] if e["event"] == "status_event"])
        self.assertFalse(any("infra-retries" in c["stdin"] for c in self.writes()))


if __name__ == "__main__":
    unittest.main()
