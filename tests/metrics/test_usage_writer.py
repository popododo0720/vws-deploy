"""The daily usage writer (vws-deploy#410): metrics.py writes lead_wakes into the daily usage file
from the lead-wake comments on the wake thread, reads it back, and SUMMARY.md shows it. tokens has no
deterministic source, so the writer never writes by_agent and tokens stays null, never 0.
Run: python3 -m unittest discover -s tests/metrics"""
import json
import os
import tempfile
import unittest

from metrics_testlib import CFG, DAY, metrics, orch, raw
from test_runtime import D, NOW, Env, routes

LEAD = "<!-- vws-orchestrate:lead-wake -->"


def gh_comment(cid, issue, created, body):
    """A comment as the issues/comments API returns it."""
    return {"id": cid, "html_url": f"https://github.com/popododo0720/vws-deploy/issues/{issue}#issuecomment-{cid}",
            "created_at": created, "issue_url": f"https://api.github.com/{D}/issues/{issue}", "body": body}


def lead(cid, created, issue=339):
    return gh_comment(cid, issue, created, f"{LEAD}\nvws-orchestrate: lead wake for vws-deploy#7 at `{'a' * 40}`")


def text(path):
    with open(path) as f:
        return f.read()


def raw_comment(c):
    return {"id": c["id"], "url": c["html_url"], "created_at": c["created_at"],
            "issue": int(c["issue_url"].rsplit("/", 1)[1]), "body": c["body"]}


COMMENTS = [
    lead(801, "2026-10-19T15:00:00Z"),   # 00:00 KST on D: in
    lead(802, "2026-10-20T14:59:59Z"),   # 23:59:59 KST on D: in
    lead(803, "2026-10-19T14:59:59Z"),   # the day before: out
    lead(804, "2026-10-20T03:00:00Z", issue=415),  # not the wake thread: out
    gh_comment(805, 339, "2026-10-20T05:00:00Z", f"quoting {LEAD} not on the first line"),
]


class Count(unittest.TestCase):
    def raw(self):
        r = raw()
        r["repos"]["vws-deploy"]["comments"] += [raw_comment(c) for c in COMMENTS]
        return r

    def test_counts_marker_comments_on_the_wake_thread_in_the_kst_day(self):
        self.assertEqual(metrics.lead_wakes_from_github(self.raw(), dict(CFG, ORCHESTRATE_WAKE_THREAD="339")), 2)

    def test_a_real_zero_stays_zero(self):
        self.assertEqual(metrics.lead_wakes_from_github(raw(), dict(CFG, ORCHESTRATE_WAKE_THREAD="339")), 0)

    def test_no_source_is_none_never_zero(self):
        self.assertIsNone(metrics.lead_wakes_from_github(self.raw(), dict(CFG, ORCHESTRATE_WAKE_THREAD="")))
        r = self.raw()
        del r["repos"]["vws-deploy"]  # archived or not collected: no wake thread to read
        self.assertIsNone(metrics.lead_wakes_from_github(r, dict(CFG, ORCHESTRATE_WAKE_THREAD="339")))


class Write(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "tokens", f"{DAY}.json")

    def read(self):
        with open(self.path) as f:
            return json.load(f)

    def test_new_file_has_only_lead_wakes(self):
        self.assertTrue(metrics.write_usage(self.path, 3))
        self.assertEqual(self.read(), {"lead_wakes": 3})
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertIsNone(metrics.read_tokens(self.path), "no by_agent: tokens stay null, never 0")
        self.assertEqual(metrics.read_lead_wakes(self.path), 3)

    def test_rerun_is_idempotent(self):
        metrics.write_usage(self.path, 3)
        before = (text(self.path), os.stat(self.path).st_mtime_ns)
        self.assertFalse(metrics.write_usage(self.path, 3))
        self.assertEqual((text(self.path), os.stat(self.path).st_mtime_ns), before)

    def test_keeps_by_agent_from_another_writer(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            json.dump({"by_agent": {"grok": 9}, "lead_wakes": 1}, f)
        self.assertTrue(metrics.write_usage(self.path, 2))
        self.assertEqual(self.read(), {"by_agent": {"grok": 9}, "lead_wakes": 2})

    def test_an_unreadable_file_is_left_alone(self):
        os.makedirs(os.path.dirname(self.path))
        for text in ("{not json", "[1, 2]"):
            with self.subTest(text=text):
                with open(self.path, "w") as f:
                    f.write(text)
                self.assertIsNone(metrics.write_usage(self.path, 2))
                self.assertEqual(text(self.path), text)

    def test_none_writes_nothing(self):
        self.assertFalse(metrics.write_usage(self.path, None))
        self.assertFalse(os.path.exists(self.path))


class Runtime(Env):
    """metrics.py --date D against the stub gh: the writer's file is read back and lands in SUMMARY.md."""

    def usage(self):
        return os.path.join(self.state, "metrics-input", "tokens", f"{DAY}.json")

    def publish(self, now=NOW, **env):
        r = routes()
        r[f"{D}/issues/comments"] = {"body": COMMENTS}
        self.set_routes(r)
        return self.run_m("--date", DAY, now=now, METRICS_MODE="apply", **env)

    def summary_md(self):
        tree = next(json.loads(c["stdin"]) for c in self.writes() if c["path"] == f"{D}/git/trees")
        return next(e["content"] for e in tree["tree"] if e["path"] == "metrics/SUMMARY.md")

    def daily(self, mode="apply"):
        with open(self.local(mode)) as f:
            return json.load(f)

    def test_writer_output_is_read_and_shows_in_summary(self):
        self.assertEqual(self.publish(ORCHESTRATE_WAKE_THREAD="339"), 0)
        with open(self.usage()) as f:
            self.assertEqual(json.load(f), {"lead_wakes": 2})
        d = self.daily()
        self.assertEqual(d["wakes"]["lead_wakes"], 2)
        self.assertNotIn("no lead-wake input for this day", d["source_gaps"])
        self.assertIsNone(d["tokens"], "no tokens source: null, never 0")
        self.assertIn("no token-usage input for this day", d["source_gaps"])
        md = self.summary_md()
        self.assertIn("lead wakes: 2 (1 days with input); tokens per merged PR: - (0 days with input).", md)
        self.assertIn("metrics_usage_written", self.events())

    def test_rerun_rewrites_nothing(self):
        self.publish(ORCHESTRATE_WAKE_THREAD="339")
        before = os.stat(self.usage()).st_mtime_ns
        self.publish(ORCHESTRATE_WAKE_THREAD="339")
        self.assertEqual(os.stat(self.usage()).st_mtime_ns, before)
        self.assertIn("metrics_usage_unchanged", self.events())
        self.assertEqual(self.daily()["wakes"]["lead_wakes"], 2)

    def test_missing_source_shows_a_dash(self):
        self.assertEqual(self.publish(), 0)  # ORCHESTRATE_WAKE_THREAD unset: no lead-wake source
        self.assertFalse(os.path.exists(self.usage()))
        d = self.daily()
        self.assertIsNone(d["wakes"]["lead_wakes"])
        self.assertIn("no lead-wake input for this day", d["source_gaps"])
        md = self.summary_md()
        self.assertIn("lead wakes: - (0 days with input); tokens per merged PR: - (0 days with input).", md)
        row = next(line for line in md.splitlines() if line.startswith(f"| {DAY} |"))
        cells = [c.strip() for c in row.strip("|").split("|")]
        self.assertEqual(cells[-2], "-", "the lead_wakes column of the day is a dash")
        self.assertIn("metrics_usage_skipped", self.events())

    def test_a_day_not_over_is_not_written(self):
        self.publish(now=orch.utc("2026-10-20T06:00:00Z"), ORCHESTRATE_WAKE_THREAD="339")  # 15:00 KST on D
        self.assertFalse(os.path.exists(self.usage()))

    def test_fixture_never_writes_the_usage_file(self):
        fx = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", f"raw-{DAY}.json")
        self.assertEqual(self.run_m("--date", DAY, "--fixture", fx, ORCHESTRATE_WAKE_THREAD="339"), 0)
        self.assertFalse(os.path.exists(self.usage()))


if __name__ == "__main__":
    unittest.main()
