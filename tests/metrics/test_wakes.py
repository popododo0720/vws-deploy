"""Wake, dispatch and token counts in the daily metrics (vws-deploy#400): verify-wake comments,
DeepSeek and grok dispatches, reworks, lead wakes and tokens from the daily usage input, and the
7/28-day lines of SUMMARY.md. A missing input is null, never 0.
Run: python3 -m unittest discover -s tests/metrics"""
import json
import os
import tempfile
import unittest

from metrics_testlib import CFG, DAY, log_records, metrics, orch, raw

SNAP = "2026-10-20T15:20:00Z"
SHA = "a" * 40


def comment(cid, issue, created, body):
    return {"id": cid, "url": f"https://github.com/popododo0720/vws-deploy/issues/{issue}#issuecomment-{cid}",
            "created_at": created, "issue": issue, "body": body}


def wake(cid, created):
    return comment(cid, 339, created, f"{orch.WAKE_MARKER}\nvws-orchestrate: 5 PRs wait for verify (threshold 5).\n\n"
                                      f"Idempotency key: `sha256:{cid:064x}`")


def fixture():
    """The recorded day plus wake, rework and swap comments; the out-of-window ones must not count."""
    r = raw()
    r["repos"]["vws-deploy"]["comments"] += [
        wake(901, "2026-10-19T15:00:00Z"),   # 00:00 KST on D: in
        wake(902, "2026-10-20T14:59:59Z"),   # 23:59:59 KST on D: in
        wake(903, "2026-10-19T14:59:59Z"),   # the day before: out
        wake(904, "2026-10-20T15:00:00Z"),   # the day after: out
        comment(905, 339, "2026-10-20T05:00:00Z", f"quoting {orch.WAKE_MARKER} not on the first line"),
        comment(906, 30, "2026-10-20T04:00:00Z", f"{orch.REWORK_TEXT}\n\n> Result: FAIL\n\n{orch.REWORK_MARKER}{SHA} verdict=1 -->"),
        comment(907, 31, "2026-10-20T04:30:00Z", "Moved to grok after 2 failures.\n\n<!-- orchestrate:swap -->"),
        comment(908, 32, "2026-10-19T10:00:00Z", "Moved to grok after 2 failures.\n\n<!-- orchestrate:swap -->"),
    ]
    return r


def doc(r=None, tokens=None, lead_wakes=None):
    recs, cov = log_records()
    return metrics.compute(r or fixture(), recs, cov, CFG, snapshot_at=SNAP, tokens=tokens, lead_wakes=lead_wakes)


class Wakes(unittest.TestCase):
    def test_verify_wake_comments_in_the_kst_day(self):
        self.assertEqual(doc()["wakes"]["verify_wake_comments"], 2)

    def test_dispatches_per_bot_and_reworks(self):
        w = doc()["wakes"]
        # 3 issue dispatches in the recorded day plus the rework (it starts with @deepseek-dev too).
        self.assertEqual(w["dispatches"], {"deepseek": 4, "grok": 1})
        self.assertEqual(w["rework_dispatches"], 1)
        self.assertEqual(doc()["deepseek"]["dispatched"], 4, "the DeepSeek dispatch count is the same number")

    def test_grok_wake_from_378_counts_as_a_grok_dispatch(self):
        # The vws-deploy#378 wake: first line <!-- vws-orchestrate:grok-wake -->, last line names the item.
        r = fixture()
        r["repos"]["vws-deploy"]["comments"].append(comment(
            909, 339, "2026-10-20T06:00:00Z", "<!-- vws-orchestrate:grok-wake -->\nvws-orchestrate: Grok task vws-iam#7 "
            "https://github.com/popododo0720/vws-iam/issues/7\n\n<!-- orchestrate:grok-wake item=vws-iam#7 -->"))
        self.assertEqual(doc(r)["wakes"]["dispatches"]["grok"], 2, "the swap notice plus the grok wake")

    def test_recorded_day_without_new_comments(self):
        w = doc(raw())["wakes"]
        self.assertEqual(w, {"verify_wake_comments": 0, "dispatches": {"deepseek": 3, "grok": 0},
                             "rework_dispatches": 0, "lead_wakes": None})

    def test_lead_wakes_from_input_else_null(self):
        self.assertEqual(doc(lead_wakes=17)["wakes"]["lead_wakes"], 17)
        self.assertEqual(doc(lead_wakes=0)["wakes"]["lead_wakes"], 0, "a reported 0 stays 0")
        d = doc()
        self.assertIsNone(d["wakes"]["lead_wakes"])
        self.assertIn("no lead-wake input for this day", d["source_gaps"])
        self.assertNotIn("no lead-wake input for this day", doc(lead_wakes=3)["source_gaps"])

    def test_wakes_change_the_content_hash(self):
        self.assertNotEqual(doc()["content_sha256"], doc(lead_wakes=1)["content_sha256"])
        self.assertNotEqual(doc()["content_sha256"], doc(raw())["content_sha256"])


class TokensInput(unittest.TestCase):
    def test_tokens_per_bot_and_missing_is_null(self):
        t = {"total": 12, "by_agent": {"deepseek": 5, "grok": 7}}
        self.assertEqual(doc(tokens=t)["tokens"], t)
        d = doc()
        self.assertIsNone(d["tokens"])
        self.assertIn("no token-usage input for this day", d["source_gaps"])

    def test_one_input_file_for_tokens_and_lead_wakes(self):
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, f"{DAY}.json")
            cases = (
                ({"by_agent": {"grok": 7}, "lead_wakes": 4}, {"total": 7, "by_agent": {"grok": 7}}, 4),
                ({"by_agent": {"grok": 7}}, {"total": 7, "by_agent": {"grok": 7}}, None),
                ({"lead_wakes": 4}, None, 4),            # no token data: tokens stay null, never 0
                ({"by_agent": {}, "lead_wakes": 0}, {"total": 0, "by_agent": {}}, 0),
                ({"lead_wakes": -1}, None, None), ({"lead_wakes": True}, None, None), ({"lead_wakes": "3"}, None, None),
                ([], None, None),
            )
            for body, want_tokens, want_lead in cases:
                with self.subTest(body=body):
                    with open(p, "w") as f:
                        json.dump(body, f)
                    self.assertEqual(metrics.read_tokens(p), want_tokens)
                    self.assertEqual(metrics.read_lead_wakes(p), want_lead)
            self.assertIsNone(metrics.read_lead_wakes(os.path.join(t, "missing.json")))

    def test_run_day_reads_the_usage_file(self):
        with tempfile.TemporaryDirectory() as t:
            fx = os.path.join(t, "raw.json")
            with open(fx, "w") as f:
                json.dump(fixture(), f)
            tok = os.path.join(t, "tokens")
            os.makedirs(tok)
            with open(os.path.join(tok, f"{DAY}.json"), "w") as f:
                json.dump({"by_agent": {"Grok": 9}, "lead_wakes": 6}, f)
            env = {"ORCHESTRATE_STATE_DIR": os.path.join(t, "state"), "ORCHESTRATE_CONFIG_DIR": os.path.join(t, "cfg"),
                   "METRICS_TOKEN_DIR": tok}
            self.assertEqual(metrics.main(["--date", DAY, "--fixture", fx, "--orchestrator-log-dir", os.path.join(t, "nolog")],
                                          env, now=orch.utc("2026-10-21T00:00:00Z")), 0)
            with open(os.path.join(t, "state", "metrics", "dry-run", "daily", f"{DAY}.json")) as f:
                d = json.load(f)
            self.assertEqual(d["tokens"], {"total": 9, "by_agent": {"grok": 9}})
            self.assertEqual(d["wakes"]["lead_wakes"], 6)
            self.assertEqual(d["wakes"]["verify_wake_comments"], 2)


class Summary(unittest.TestCase):
    def docs(self):
        out = []
        for k, (merged, tokens, lead) in enumerate([(4, 4000, 10), (2, None, None), (0, 1000, 3)]):
            d = doc(tokens=None if tokens is None else {"total": tokens, "by_agent": {"grok": tokens}}, lead_wakes=lead)
            d["day"] = (orch.dt.date(2026, 10, 20) - orch.dt.timedelta(days=k)).isoformat()
            d["prs"]["totals"]["merged"] = merged
            out.append(d)
        old = doc(raw())
        old["day"] = "2026-10-01"  # in the 28-day window only; written before #400, without "wakes"
        del old["wakes"]
        out.append(old)
        return out

    def test_seven_and_twenty_eight_day_wakes_and_tokens_per_merged_pr(self):
        summary, md = metrics.summarize(self.docs(), DAY)
        # 3 days in 7, each from fixture(): 2 verify wakes, 4 DeepSeek and 1 grok dispatch, 1 rework.
        self.assertIn("- Last 7 days wakes: 6 verify wakes, 12 DeepSeek and 3 grok dispatches (3 reworks), "
                      "lead wakes: 13 (2 days with input); tokens per merged PR: 1250 (2 days with input).", md)
        # The pre-#400 day adds its 3 DeepSeek dispatches; its unknown wakes are not 0, so the
        # wake totals say they cover 3 of the 4 days in the window.
        self.assertIn("- Last 28 days wakes: 6 verify wakes, 15 DeepSeek and 3 grok dispatches (3 reworks) "
                      "(3 days with data), lead wakes: 13 (2 days with input); tokens per merged PR: 1250 "
                      "(2 days with input).", md)
        row = summary["days"][-1]
        self.assertEqual((row["verify_wakes"], row["grok_dispatches"], row["rework_dispatches"], row["lead_wakes"]), (2, 1, 1, 10))
        self.assertIsNone(summary["days"][0]["verify_wakes"], "a day written before #400 has no wake data")
        self.assertTrue(md.isascii())

    def test_window_with_only_pre_change_days_shows_a_dash_not_zero(self):
        old = doc(raw())
        del old["wakes"]  # written before #400: every wake value is unknown
        _, md = metrics.summarize([old], DAY)
        for span in (7, 28):
            self.assertIn(f"- Last {span} days wakes: - verify wakes, 3 DeepSeek and - grok dispatches (- reworks) "
                          "(0 days with data), lead wakes: - (0 days with input); tokens per merged PR: - "
                          "(0 days with input).", md)
        self.assertNotIn("0 verify wakes", md)

    def test_window_without_daily_files_shows_a_dash_not_zero(self):
        old = doc(raw())
        old["day"] = "2026-10-01"  # in the 28-day window only
        _, md = metrics.summarize([old], DAY)
        self.assertIn("- Last 7 days (0 with data): - PRs merged, - opened, - DeepSeek dispatches (- done, - failed, "
                      "- timed out), - grok swaps.", md)
        self.assertIn("- Last 7 days wakes: - verify wakes, - DeepSeek and - grok dispatches (- reworks), "
                      "lead wakes: - (0 days with input); tokens per merged PR: - (0 days with input).", md)
        self.assertIn("- Last 28 days wakes: 0 verify wakes, 3 DeepSeek and 0 grok dispatches (0 reworks), ", md,
                      "a day with a wakes block keeps its real 0")

    def test_no_input_shows_a_dash_not_zero(self):
        d = doc()
        _, md = metrics.summarize([d], DAY)
        self.assertIn("lead wakes: - (0 days with input); tokens per merged PR: - (0 days with input).", md)


if __name__ == "__main__":
    unittest.main()
