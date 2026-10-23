"""Pure-function tests for .github/metrics/metrics.py (vws-deploy#345): compute() on a recorded fixture,
determinism, the due-day table, the orchestrator log reader and the summary.
Run: python3 -m unittest discover -s tests/metrics"""
import copy
import json
import unittest

from metrics_testlib import CFG, DAY, log_records, metrics, orch, raw

SNAP = "2026-10-20T15:20:00Z"


def doc(r=None, exclusions=frozenset(), snapshot_at=SNAP, covered=None):
    recs, cov = log_records()
    return metrics.compute(r or raw(), recs, cov if covered is None else covered, CFG, exclusions, snapshot_at=snapshot_at)


class SummaryWindows(unittest.TestCase):
    def test_spans_are_calendar_days(self):
        def d(day, merged):
            return {"day": day, "prs": {"totals": {"opened": 0, "merged": merged}}, "lead_time_hours": {"all": {"median": 1}},
                    "verify": {"pass_rate": 1.0}, "deepseek": {"dispatched": 0, "done": 0, "failed": 0, "timeout": 0},
                    "grok_swaps": {"labeled": 0}, "ci": {"failure_rate": 0.0}, "queue_depth": None}
        docs = [d(f"2026-10-0{k}", 1) for k in range(1, 7)] + [d(DAY, 5)]
        _, md = metrics.summarize(docs, DAY)
        self.assertIn("- Last 7 days (1 with data): 5 PRs merged", md)
        self.assertIn("- Last 28 days (7 with data): 11 PRs merged", md)


class Compute(unittest.TestCase):
    def test_prs_and_agents(self):
        d = doc()
        self.assertEqual(d["prs"]["opened"], {"vws-deploy": {"eggbot": 1, "deepseek": 1}, "vws-iam": {"grok": 1}})
        self.assertEqual(d["prs"]["merged"], {"vws-deploy": {"eggbot": 1, "unattributed": 1}, "vws-iam": {"grok": 1}})
        self.assertEqual(d["prs"]["closed_unmerged"], {"vws-deploy": 1})
        self.assertEqual(d["prs"]["totals"], {"opened": 3, "merged": 3, "closed_unmerged": 1})
        self.assertEqual(d["prs"]["by_agent"]["merged"], {"eggbot": 1, "grok": 1, "unattributed": 1})

    def test_window_edges_are_kst(self):
        d = doc()
        self.assertEqual(d["window_utc"], ["2026-10-19T15:00:00Z", "2026-10-20T15:00:00Z"])
        # #12 at 14:59:59Z is in, #13 at 14:59:59Z the day before and #14 at 15:00:00Z are out.
        self.assertEqual(d["prs"]["opened"]["vws-deploy"].get("deepseek"), 1)
        self.assertEqual(sum(d["prs"]["opened"]["vws-deploy"].values()), 2)

    def test_lead_time(self):
        d = doc()["lead_time_hours"]
        self.assertEqual(d["all"], {"n": 3, "median": 12.0, "p90": 58.0, "max": 58.0})
        self.assertEqual(d["by_repo"]["vws-deploy"], {"n": 2, "median": 35.0, "p90": 58.0, "max": 58.0})

    def test_verify(self):
        v = doc()["verify"]
        self.assertEqual((v["runs"], v["success"], v["failure"], v["pass_rate"]), (3, 2, 1, 0.667))
        self.assertEqual((v["first_pass"], v["first_pass_n"], v["first_pass_rate"]), (1, 2, 0.5))
        self.assertEqual(v["approved_labels"], 1)

    def test_deepseek_and_grok(self):
        d = doc()
        ds = d["deepseek"]
        self.assertEqual((ds["dispatched"], ds["done"], ds["failed"], ds["timeout"]), (3, 2, 1, 2))
        self.assertEqual(ds["dispatched_by_orchestrator"], 1, "dry-run 'applied' lines do not count")
        self.assertEqual(d["grok_swaps"], {"labeled": 1, "by_orchestrator": 1})

    def test_timeout_respects_exclusions(self):
        excl = {"https://github.com/popododo0720/vws-deploy/issues/20#issuecomment-101"}
        self.assertEqual(doc(exclusions=excl)["deepseek"]["timeout"], 1)

    def test_ci(self):
        ci = doc()["ci"]
        self.assertEqual(ci["by_repo"]["vws-deploy"]["ci"], {"success": 3, "failure": 1, "failure_rate": 0.25})
        self.assertEqual((ci["success"], ci["failure"], ci["failure_rate"]), (5, 1, 0.167))
        self.assertEqual(ci["separate"]["vws-deploy"]["repo-guard"]["failure"], 2)
        self.assertNotIn("drift-check", ci["by_repo"]["vws-deploy"], "schedule runs are not PR or push CI")

    def test_codex_and_queue(self):
        d = doc()
        self.assertEqual(d["codex"], {"merged_prs_with_codex": 1, "rounds_declared": 3, "verify_reports": 1, "blocking_sum": 0})
        self.assertEqual(d["queue_depth"], {"ready": 1, "deepseek": 1, "grok": 1, "blocked": 1, "in-review": 0,
                                            "priority/blocker": 0, "verify_backlog": 1})

    def test_orchestrator_log(self):
        o = doc()["orchestrator"]
        self.assertEqual(o, {"ticks": 3, "failed_ticks": 2, "apply_failed": 1, "rate_low": 1, "failure_alerts": 1,
                             "gh_get": 15, "gh_not_modified": 9, "gh_write": 3, "modes": {"apply": 3}})

    def test_hash_ignores_whether_the_snapshot_was_taken(self):
        self.assertEqual(doc(snapshot_at=SNAP)["content_sha256"], doc(snapshot_at=None)["content_sha256"])

    def test_source_gaps(self):
        d = doc(covered=False, snapshot_at=None)
        self.assertIsNone(d["orchestrator"])
        self.assertIsNone(d["deepseek"]["dispatched_by_orchestrator"])
        self.assertIsNone(d["grok_swaps"]["by_orchestrator"])
        self.assertIsNone(d["queue_depth"])
        self.assertEqual(len(d["source_gaps"]), 4)
        self.assertIn("no token-usage input for this day", d["source_gaps"])
        self.assertIsNone(d["stalls"])

    def test_log_reader_spans_rotated_files(self):
        recs, covered = log_records()
        self.assertTrue(covered)
        self.assertEqual(len(recs), 12, "the window keeps 12 of 14 records; a bad line is skipped")
        _, covered = metrics.read_orchestrator_log("/nonexistent", 0, 1)
        self.assertFalse(covered)

    def test_deterministic_and_hash_ignores_volatile(self):
        a, b = doc(), doc()
        self.assertEqual(metrics.dumps(a), metrics.dumps(b))
        c = doc(snapshot_at="2026-10-21T00:00:00Z")
        c["queue_depth"]["ready"] = 9
        self.assertEqual(metrics.content_hash(c), a["content_sha256"])
        r = raw()
        r["repos"]["vws-iam"]["prs"][0]["body"] = "Agent: other"
        self.assertNotEqual(doc(r)["content_sha256"], a["content_sha256"])

    def test_input_order_does_not_matter(self):
        r = raw()
        for repo in r["repos"].values():
            for k in ("prs", "comments", "events", "runs"):
                repo[k].reverse()
        self.assertEqual(doc(r)["content_sha256"], doc()["content_sha256"])

    def test_output_is_ascii_json(self):
        text = metrics.dumps(doc())
        self.assertTrue(text.isascii())
        self.assertEqual(json.loads(text)["day"], DAY)


class Helpers(unittest.TestCase):
    def test_agent_line(self):
        cases = {"Agent: Eggbot": "eggbot", "x\nagent:  deepseek-dev \ny": "deepseek-dev", "Agent: two words": "unattributed",
                 "My Agent: x": "unattributed", "": "unattributed", None: "unattributed"}
        for body, want in cases.items():
            with self.subTest(body=body):
                self.assertEqual(metrics.agent_of(body), want)

    def test_stats(self):
        self.assertEqual(metrics.stats([]), {"n": 0, "median": None, "p90": None, "max": None})
        self.assertEqual(metrics.stats(list(range(1, 11))), {"n": 10, "median": 5.5, "p90": 9, "max": 10})

    def test_due_days(self):
        t = lambda s: orch.utc(s)
        cfg = dict(CFG)
        # 00:19 KST on 10-21 is 15:19Z on 10-20: 10-20 is not due yet, older unpublished days are.
        self.assertEqual(metrics.due_days(t("2026-10-20T15:19:00Z"), cfg, ["2026-10-1" + str(d) for d in range(3, 10)]), [])
        self.assertEqual(metrics.due_days(t("2026-10-20T15:20:00Z"), cfg, ["2026-10-1" + str(d) for d in range(4, 10)]),
                         ["2026-10-20"])
        done = [f"2026-10-{d:02d}" for d in range(14, 21)]
        self.assertEqual(metrics.due_days(t("2026-10-21T00:00:00Z"), cfg, done), [])
        self.assertEqual(metrics.due_days(t("2026-10-21T00:00:00Z"), cfg, done[:4]), ["2026-10-18", "2026-10-19", "2026-10-20"])
        self.assertEqual(len(metrics.due_days(t("2026-10-21T00:00:00Z"), cfg, [])), 7, "never more than the backfill window")
        self.assertEqual(metrics.due_days(t("2026-10-21T00:00:00Z"), dict(cfg, METRICS_BACKFILL_DAYS="2"), []),
                         ["2026-10-19", "2026-10-20"])

    def test_summary(self):
        base = doc()
        docs = []
        for k in range(30):
            d = copy.deepcopy(base)
            d["day"] = (orch.dt.date(2026, 10, 20) - orch.dt.timedelta(days=k)).isoformat()
            docs.append(d)
        summary, md = metrics.summarize(docs, DAY)
        self.assertEqual(len(summary["days"]), 28)
        self.assertEqual(summary["days"][0]["day"], "2026-09-23")
        self.assertIn("- Last 7 days (7 with data): 21 PRs merged", md)
        self.assertTrue(md.isascii())
        self.assertEqual(md.count("\n| 2026-"), 28)


if __name__ == "__main__":
    unittest.main()
