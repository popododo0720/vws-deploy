"""Anomaly alert tests for .github/metrics/metrics.py (vws-deploy#345): a normal day raises no alert,
each anomaly type (stall, a rising failure or grok-escalation rate, a token spike) raises one at its
threshold constant and not just below it, and the alert is one marker comment on the status thread,
posted at most once, in apply mode only.
Run: python3 -m unittest discover -s tests/metrics"""
import copy
import datetime as dt
import json
import os
import unittest
from unittest import mock

from metrics_testlib import CFG, DAY, log_records, metrics, orch, raw
from test_runtime import D, Env, routes

SNAP = "2026-10-20T15:20:00Z"
M = metrics


def day_doc(day, ci=(18, 2), ds=(10, 1, 0, 11), grok=1, tokens=2_000_000):
    """A minimal daily doc. ds = (done, failed, timeout, dispatched)."""
    return {"day": day, "ci": {"success": ci[0], "failure": ci[1]},
            "deepseek": {"done": ds[0], "failed": ds[1], "timeout": ds[2], "dispatched": ds[3]},
            "grok_swaps": {"labeled": grok}, "tokens": None if tokens is None else {"total": tokens, "by_agent": {}},
            "stalls": []}


def history(n=7, **kw):
    end = dt.date.fromisoformat(DAY)
    return [day_doc((end - dt.timedelta(days=k)).isoformat(), **kw) for k in range(1, n + 1)]


def kinds(doc, hist):
    return [a["kind"] for a in M.anomalies(doc, hist)]


class Thresholds(unittest.TestCase):
    def test_constants_are_explicit(self):
        self.assertEqual((M.STALL_HOURS, M.BASELINE_DAYS, M.BASELINE_MIN_DAYS, M.RATE_MIN_EVENTS, M.RATE_MIN_RISE,
                          M.RATE_MIN_RATIO, M.TOKEN_SPIKE_RATIO, M.TOKEN_MIN_DAILY),
                         (48, 7, 3, 5, 0.20, 2.0, 3.0, 1_000_000))
        self.assertEqual(M.ALERT_MARKER, "<!-- vws-orchestrate:metrics-alert -->")

    def test_normal_day_raises_nothing(self):
        self.assertEqual(kinds(day_doc(DAY), history()), [])

    def test_recorded_fixture_day_is_normal(self):
        recs, cov = log_records()
        d = M.compute(raw(), recs, cov, CFG, snapshot_at=SNAP)
        self.assertEqual(d["stalls"], [], "#21 is old but blocked; #339 is a draft")
        self.assertEqual(M.anomalies(d, []), [])


class Stall(unittest.TestCase):
    def doc_at(self, hours, labels=("ready",), draft=False):
        r = copy.deepcopy(raw())
        updated = orch.iso(orch.utc(SNAP) - hours * 3600)
        repo = r["repos"]["vws-deploy"]
        repo["open_issues"].append({"number": 99, "labels": list(labels), "is_pr": False, "updated_at": updated})
        repo["open_pulls"].append({"number": 98, "draft": draft, "base": "main", "labels": [], "updated_at": updated})
        recs, cov = log_records()
        return M.compute(r, recs, cov, CFG, snapshot_at=SNAP)

    def test_at_threshold_alerts(self):
        d = self.doc_at(M.STALL_HOURS)
        self.assertEqual([s["target"] for s in d["stalls"]], ["vws-deploy#98", "vws-deploy#99"])
        a = M.anomalies(d, [])
        self.assertEqual(([x["kind"] for x in a], a[0]["count"]), (["stall"], 2))

    def test_just_below_threshold_is_quiet(self):
        self.assertEqual(self.doc_at(M.STALL_HOURS - 0.1)["stalls"], [])

    def test_blocked_and_unlabelled_and_draft_are_not_tasks(self):
        self.assertEqual(self.doc_at(500, labels=("blocked", "ready"), draft=True)["stalls"], [])
        self.assertEqual(self.doc_at(500, labels=("enhancement",), draft=True)["stalls"], [])

    def test_backfilled_day_has_no_parked_count(self):
        recs, cov = log_records()
        self.assertIsNone(M.compute(raw(), recs, cov, CFG, snapshot_at=None)["parked"])


class Parked(unittest.TestCase):
    """vws-deploy#463: parked issues wait by design and are not stalls. 2026-10-23: 88 of the 89 stalls
    were grok issues that no rule may pick up (not on ORCHESTRATE_GROK_ALLOWLIST)."""

    def doc(self, labels, assignees=0, allow="", hours=500):
        r = copy.deepcopy(raw())
        updated = orch.iso(orch.utc(SNAP) - hours * 3600)
        r["repos"]["vws-deploy"]["open_issues"].append(
            {"number": 99, "labels": list(labels), "is_pr": False, "updated_at": updated, "assignees": assignees})
        recs, cov = log_records()
        return M.compute(r, recs, cov, dict(CFG, ORCHESTRATE_GROK_ALLOWLIST=allow), snapshot_at=SNAP)

    def targets(self, d):
        return [s["target"] for s in d["stalls"]]

    def test_grok_issue_off_the_allowlist_is_parked_not_stalled(self):
        for labels in (("grok",), ("aws-parity", "grok"), ("grok", "in-review")):
            d = self.doc(labels)
            self.assertEqual(self.targets(d), [], labels)
            self.assertEqual(d["parked"], 2, "#99 and the fixture's blocked #21")

    def test_allowlisted_grok_issue_can_stall(self):
        d = self.doc(("grok", "ready"), allow="vws-iam#1, vws-deploy#99")
        self.assertEqual((self.targets(d), d["parked"]), (["vws-deploy#99"], 1))

    def test_assigned_grok_issue_holds_a_slot_and_can_stall(self):
        d = self.doc(("grok",), assignees=1)
        self.assertEqual((self.targets(d), d["parked"]), (["vws-deploy#99"], 1))

    def test_deepseek_owned_issue_can_stall(self):
        d = self.doc(("deepseek", "grok", "ready"))
        self.assertEqual((self.targets(d), d["parked"]), (["vws-deploy#99"], 1))

    def test_opt_out_labels_are_parked(self):
        for lb in ("hold", "do-not-merge", "needs-human", "blocked"):
            d = self.doc(("ready", "deepseek", lb))
            self.assertEqual((self.targets(d), d["parked"]), ([], 2), lb)

    def test_missing_assignee_field_counts_as_unassigned(self):
        r = copy.deepcopy(raw())
        r["repos"]["vws-deploy"]["open_issues"].append(
            {"number": 99, "labels": ["grok"], "is_pr": False, "updated_at": "2026-10-01T00:00:00Z"})
        recs, cov = log_records()
        d = M.compute(r, recs, cov, CFG, snapshot_at=SNAP)
        self.assertEqual((d["stalls"], d["parked"]), ([], 2))

    def test_a_parked_issue_that_is_also_a_task_label_counts_once(self):
        d = self.doc(("grok", "ready", "hold"))
        self.assertEqual(d["parked"], 2)

    def test_parked_is_volatile_like_stalls(self):
        self.assertIn("parked", M.VOLATILE)
        self.assertEqual(self.doc(("grok",))["content_sha256"], self.doc(("grok",), hours=1)["content_sha256"])

    def test_snapshot_keeps_the_assignee_count_not_the_logins(self):
        class Gh:
            def get(self, path):
                if path.endswith("/vws-deploy"):
                    return {"archived": False}, None
                if "/issues?state=open" in path:
                    return [{"number": 7, "labels": [{"name": "grok"}], "updated_at": "2026-10-01T00:00:00Z",
                             "assignees": [{"login": "someone"}, {"login": "other"}]},
                            {"number": 8, "labels": [], "updated_at": "2026-10-01T00:00:00Z"}], None
                return [], None
        got = M.collect(Gh(), ["vws-deploy"], DAY, CFG, snapshot=True)["repos"]["vws-deploy"]["open_issues"]
        self.assertEqual([(i["number"], i["assignees"]) for i in got], [(7, 2), (8, 0)])
        self.assertNotIn("someone", json.dumps(got))

    def test_backfilled_day_has_no_stall_data(self):
        recs, cov = log_records()
        self.assertIsNone(M.compute(raw(), recs, cov, CFG, snapshot_at=None)["stalls"])


class Rates(unittest.TestCase):
    # Baseline ci failure rate 0.1 (2 of 20 a day).
    def test_ci_failure_rate_rise(self):
        self.assertEqual(kinds(day_doc(DAY, ci=(12, 8)), history()), ["ci_failure_rate"])  # 0.4 >= 0.3 and >= 0.2
        self.assertEqual(kinds(day_doc(DAY, ci=(15, 5)), history()), [])  # 0.25 < 0.1 + 0.20

    def test_exactly_at_the_threshold_alerts(self):
        # 6/20 = 0.1 + 0.20 exactly; a float sum (0.30000000000000004) would miss it.
        self.assertEqual(kinds(day_doc(DAY, ci=(14, 6)), history()), ["ci_failure_rate"])

    def test_ratio_also_needed(self):
        hist = history(ci=(13, 7))  # baseline 0.35
        self.assertEqual(kinds(day_doc(DAY, ci=(9, 11)), hist), [])  # 0.55 = +0.20 but only 1.57x
        self.assertEqual(kinds(day_doc(DAY, ci=(6, 14)), hist), ["ci_failure_rate"])  # 0.70 = 2x

    def test_deepseek_failure_rate_rise(self):
        self.assertEqual(kinds(day_doc(DAY, ds=(3, 2, 2, 7)), history()), ["deepseek_failure_rate"])

    def test_grok_escalation_rate_rise(self):
        self.assertEqual(kinds(day_doc(DAY, grok=5), history()), ["grok_escalation_rate"])  # 5/11 vs 1/11

    def test_zero_baseline_uses_the_absolute_rise(self):
        self.assertEqual(kinds(day_doc(DAY, grok=3), history(grok=0)), ["grok_escalation_rate"])  # 0.27
        self.assertEqual(kinds(day_doc(DAY, grok=2), history(grok=0)), [])  # 0.18

    def test_small_samples_are_quiet(self):
        self.assertEqual(kinds(day_doc(DAY, ci=(0, 4)), history()), [])  # 4 < RATE_MIN_EVENTS
        self.assertEqual(kinds(day_doc(DAY, ci=(12, 8)), history(n=2)), [])  # 2 < BASELINE_MIN_DAYS

    def test_only_the_trailing_window_counts(self):
        old = [day_doc("2026-10-01", ci=(0, 20))] * 5  # outside BASELINE_DAYS: ignored
        self.assertEqual(kinds(day_doc(DAY, ci=(12, 8)), history(n=3) + old), ["ci_failure_rate"])
        later = day_doc("2026-10-21", ci=(0, 20))  # a later day never forms the baseline
        self.assertEqual(kinds(day_doc(DAY, ci=(12, 8)), history(n=3) + [later]), ["ci_failure_rate"])


class Tokens(unittest.TestCase):
    def test_spike(self):
        self.assertEqual(kinds(day_doc(DAY, tokens=6_000_000), history()), ["token_spike"])  # 3x the 2M median
        self.assertEqual(kinds(day_doc(DAY, tokens=5_999_999), history()), [])

    def test_floor(self):
        self.assertEqual(kinds(day_doc(DAY, tokens=900_000), history(tokens=100_000)), [])

    def test_no_input_no_alert(self):
        self.assertEqual(kinds(day_doc(DAY, tokens=None), history()), [])
        self.assertEqual(kinds(day_doc(DAY, tokens=9_000_000), history(tokens=None)), [])

    def test_reader(self):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "x.json")
            for body, want in (({"by_agent": {"DeepSeek": 5, "grok": 7}}, {"total": 12, "by_agent": {"deepseek": 5, "grok": 7}}),
                               ({"by_agent": {"grok": -1}}, None), ({"by_agent": {"grok": True}}, None),
                               ({"by_agent": {"bad name!": 1}}, None), ([], None)):
                with open(p, "w") as f:
                    json.dump(body, f)
                self.assertEqual(M.read_tokens(p), want)
            self.assertIsNone(M.read_tokens(os.path.join(t, "missing.json")))


class Every(unittest.TestCase):
    def test_each_kind_at_once_and_the_body(self):
        d = day_doc(DAY, ci=(12, 8), ds=(3, 2, 2, 7), grok=5, tokens=9_000_000)
        d["stalls"] = [{"target": "vws-iam#7", "idle_hours": 50.0}]
        d["alerts"] = M.anomalies(d, history())
        self.assertEqual(sorted(a["kind"] for a in d["alerts"]),
                         ["ci_failure_rate", "deepseek_failure_rate", "grok_escalation_rate", "stall", "token_spike"])
        body = M.alert_body(d, "metrics/daily/x.json")
        self.assertEqual(body.splitlines()[0], M.ALERT_MARKER)
        self.assertIn("vws-iam#7 (50.0h)", body)
        self.assertIn(f"Idempotency key: `{M.alert_key(d)}`", body)
        self.assertTrue(body.isascii())


ALERT = [{"kind": "stall", "count": 1, "threshold_hours": 48, "items": [{"target": "vws-iam#7", "idle_hours": 50.0}]}]


class Delivery(Env):
    def comments(self):
        return [c for c in self.writes() if c["path"].endswith("/comments")]

    def test_normal_day_is_silent(self):
        self.assertEqual(self.run_m("--date", DAY, METRICS_MODE="apply"), 0)
        self.assertEqual(self.comments(), [])
        self.assertNotIn(f"{D}/issues/339/comments", [c["path"].split("?")[0] for c in self.calls()])

    def test_anomaly_posts_one_marker_comment_after_publishing(self):
        with mock.patch.object(metrics, "anomalies", return_value=ALERT):
            self.assertEqual(self.run_m("--date", DAY, METRICS_MODE="apply"), 0)
            w = [(c["method"], c["path"]) for c in self.writes()]
            self.assertEqual(w[-1], ("POST", f"{D}/issues/339/comments"))
            self.assertEqual(w[:3][2][0], "PATCH", "the data is on the branch before the alert")
            body = json.loads(self.comments()[0]["stdin"])["body"]
            self.assertEqual(body.splitlines()[0], metrics.ALERT_MARKER)
            self.assertIn("vws-iam#7", body)
            self.assertIn("metrics_alerted", self.events())
            # Again (state remembers the key): no second comment.
            self.assertEqual(self.run_m("--recompute", DAY, METRICS_MODE="apply"), 0)
            self.assertEqual(len(self.comments()), 1)

    def test_key_already_on_the_thread_is_not_reposted(self):
        with mock.patch.object(metrics, "anomalies", return_value=ALERT):
            self.run_m("--date", DAY, METRICS_MODE="apply")
            body = json.loads(self.comments()[0]["stdin"])["body"]
        os.remove(os.path.join(self.state, "metrics-state.json"))
        r = routes()
        r[f"{D}/issues/339/comments?per_page=100"] = {"body": [{"body": body}]}
        self.set_routes(r)
        with mock.patch.object(metrics, "anomalies", return_value=ALERT):
            self.assertEqual(self.run_m("--recompute", DAY, METRICS_MODE="apply"), 0)
        self.assertEqual(len(self.comments()), 1)
        self.assertIn("metrics_alert_skipped", self.events())

    def test_dry_run_only_plans(self):
        with mock.patch.object(metrics, "anomalies", return_value=ALERT):
            self.assertEqual(self.run_m("--date", DAY), 0)
        self.assertEqual(self.writes(), [])
        self.assertIn("metrics_alert_planned", self.events())
        with open(os.path.join(self.state, "metrics", "dry-run", "alerts", f"{DAY}.md")) as f:
            self.assertEqual(f.readline().strip(), metrics.ALERT_MARKER)

    def test_status_pr_comes_from_config(self):
        with mock.patch.object(metrics, "anomalies", return_value=ALERT):
            self.assertEqual(self.run_m("--date", DAY, METRICS_MODE="apply", ORCHESTRATE_STATUS_PR="400"), 0)
        self.assertEqual(self.comments()[0]["path"], f"{D}/issues/400/comments")

    def test_only_an_alert_comment_on_the_status_thread_may_be_posted(self):
        gh = metrics.MetricsGh(status_pr=339)
        for path, body in ((f"{D}/issues/339/comments", "hello"), (f"{D}/issues/12/comments", metrics.ALERT_MARKER + "\nx"),
                           ("repos/popododo0720/vws-iam/issues/339/comments", metrics.ALERT_MARKER + "\nx")):
            with self.subTest(path=path, body=body), self.assertRaises(PermissionError):
                gh.write("POST", path, {"body": body})
        self.assertEqual(self.calls(), [])

    def test_a_journaled_alert_is_posted_even_if_the_anomaly_is_gone(self):
        r = routes()
        r[f"POST {D}/issues/339/comments"] = {"status": 502, "body": {"message": "Bad Gateway"}}
        self.set_routes(r)
        with mock.patch.object(metrics, "anomalies", return_value=ALERT):
            self.assertEqual(self.run_m("--date", DAY, METRICS_MODE="apply"), metrics.FAIL_EXIT)
        first = json.loads(self.comments()[0]["stdin"])["body"]
        self.set_routes(routes())
        self.assertEqual(self.run_m("--date", DAY, METRICS_MODE="apply"), 0)  # the stall got an update meanwhile
        self.assertEqual(json.loads(self.comments()[-1]["stdin"])["body"], first)
        state = orch.read_json(os.path.join(self.state, "metrics-state.json"), {})
        self.assertEqual(state["pending_alerts"], {})
        self.assertIn(DAY, state["done"]["apply"])

    def test_an_interrupted_ref_update_keeps_the_alert(self):
        # The PATCH times out (it may have landed); the run fails before the POST. A restart posts the
        # journaled alert although a fresh computation no longer sees the anomaly.
        r = routes()
        r[f"PATCH {D}/git/refs/heads/factory-metrics"] = {"status": 504, "body": {"message": "Gateway Timeout"}}
        self.set_routes(r)
        with mock.patch.object(metrics, "anomalies", return_value=ALERT):
            self.assertEqual(self.run_m("--date", DAY, METRICS_MODE="apply"), metrics.FAIL_EXIT)
        self.assertEqual(self.comments(), [])
        state = orch.read_json(os.path.join(self.state, "metrics-state.json"), {})
        self.assertEqual(list(state["pending_alerts"]), [DAY])
        self.set_routes(routes())
        self.assertEqual(self.run_m("--date", DAY, METRICS_MODE="apply"), 0)
        body = json.loads(self.comments()[0]["stdin"])["body"]
        self.assertTrue(body.startswith(metrics.ALERT_MARKER) and "vws-iam#7" in body)
        self.assertEqual(orch.read_json(os.path.join(self.state, "metrics-state.json"), {})["pending_alerts"], {})

    def test_failed_alert_fails_the_run_and_retries_next_tick(self):
        r = routes()
        r[f"POST {D}/issues/339/comments"] = {"status": 502, "body": {"message": "Bad Gateway"}}
        self.set_routes(r)
        with mock.patch.object(metrics, "anomalies", return_value=ALERT):
            self.assertEqual(self.run_m("--date", DAY, METRICS_MODE="apply"), metrics.FAIL_EXIT)
        state = orch.read_json(os.path.join(self.state, "metrics-state.json"), {})
        self.assertNotIn(DAY, state.get("done", {}).get("apply", []), "the day stays due")
        self.assertIn("metrics_failed", self.events())


if __name__ == "__main__":
    unittest.main()
