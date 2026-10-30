"""Rule Z (vws-deploy#377): a rework or rebuild that lands as a new PR closes the old one with a link to
the new PR, and only the PR the new one names as its source.

Run: python3 -m unittest discover -s tests/orchestrate"""
import json
import unittest

import test_runtime
from orch_testlib import CFG, NOW, U, dispatch, item, orch, report, snapshot


def pr(repo, number, body="", title="", hours=100, **kw):
    return item(repo, number, is_pr=True, body=body, title=title, created_hours_ago=hours, **kw)


def run(*items, cfg=CFG):
    return orch.decide(snapshot(*items), {}, NOW, cfg, set())


def closes(r):
    return [a for a in r["actions"] if a["type"] == "close_pr"]


class Rule(unittest.TestCase):
    def test_new_pr_naming_its_source_closes_the_old_pr(self):
        old = pr("vws-iam", 255, body="Part of #171\n")
        new = pr("vws-iam", 264, body="Part of #171\n\nRework of #255 onto current main `26d4b36`.\n", hours=90)
        r = run(old, new)
        [a] = closes(r)
        self.assertEqual((a["target"], a["source"], a["via"]), ("vws-iam#255", "vws-iam#264", "body"))
        self.assertTrue(a["body"].startswith(f"Superseded by {new['url']}."))
        self.assertIn(new["url"], a["body"])
        self.assertRegex(a["body"], r"(?m)^Idempotency key: `sha256:[0-9a-f]{64}`$")
        self.assertEqual(a["key"], orch.superseded_key("vws-iam#255", "vws-iam#264"))
        self.assertIn({"kind": "superseded_close", "target": "vws-iam#255", "source": "vws-iam#264"}, r["notes"])

    def test_a_done_report_naming_the_new_pr_closes_the_dispatch_pr(self):
        old = pr("vws-iam", 255, comments=[dispatch(20, "vws-iam", 255, 5),
                                           report(21, "vws-iam", 255, 20, "Done", 4, pr=f"{U}/vws-iam/pull/264")])
        new = pr("vws-iam", 264, hours=90)
        [a] = closes(run(old, new))
        self.assertEqual((a["target"], a["source"], a["via"]), ("vws-iam#255", "vws-iam#264", "report"))

    def test_a_report_is_no_link_without_a_dispatch_on_the_old_pr(self):
        cases = {
            "no dispatch": [report(21, "vws-iam", 255, 20, "Done", 4, pr=f"{U}/vws-iam/pull/264")],
            "report for another dispatch": [dispatch(20, "vws-iam", 255, 6),
                                            report(21, "vws-iam", 255, 99, "Done", 4, pr=f"{U}/vws-iam/pull/264")],
            "failed report": [dispatch(20, "vws-iam", 255, 6),
                              report(21, "vws-iam", 255, 20, "Failed", 4)],
            "names itself": [dispatch(20, "vws-iam", 255, 6),
                             report(21, "vws-iam", 255, 20, "Done", 4, pr=f"{U}/vws-iam/pull/255")],
        }
        for why, comments in cases.items():
            with self.subTest(why):
                old = pr("vws-iam", 255, comments=comments)
                self.assertEqual(closes(run(old, pr("vws-iam", 264, hours=90))), [])
        old = pr("vws-iam", 255, comments=[dispatch(20, "vws-iam", 255, 6),
                                           report(21, "vws-iam", 255, 20, "Done", 4, pr=f"{U}/vws-iam/issues/12")])
        self.assertEqual(closes(run(old, pr("vws-iam", 264, hours=90), item("vws-iam", 12))), [])
        old = pr("vws-iam", 255, comments=[dispatch(20, "vws-iam", 255, 6),
                                           report(21, "vws-iam", 255, 20, "Done", 4,
                                                  pr=f"{U}/vws-proto/pull/264")])
        self.assertEqual(closes(run(old, pr("vws-proto", 264, hours=90))), [])

    def test_only_the_named_source_is_closed(self):
        old = pr("vws-iam", 255, body="Part of #171\n")
        newer = pr("vws-iam", 264, body="Rework of #255 onto current main.\n", hours=90)
        unrelated = pr("vws-iam", 300, body="Part of #171\n", hours=80)
        self.assertEqual([a["target"] for a in closes(run(old, newer, unrelated))], ["vws-iam#255"])

    def test_every_reference_form_vws_verify_reads(self):
        forms = {"-v2 of #10": 10, "rebuilt of #11": 11, "rebuild of #12": 12, "rework of #13": 13,
                 "reworked from #14": 14, "supersedes #15": 15, "replaces #16": 16}
        self.assertEqual(orch.superseded_numbers("", "\n".join(forms)), sorted(forms.values()))
        self.assertEqual(orch.superseded_numbers("", "Rework of vws-iam#255"), [])
        self.assertEqual(orch.superseded_numbers("", "Part of #171"), [])

    def test_a_reference_in_a_code_fence_or_html_comment_is_not_a_claim(self):
        for body in ("```\nRework of #255\n```\n", "<!-- Rework of #255 -->\n", "Part of #171\n"):
            with self.subTest(body=body):
                self.assertEqual(orch.superseded_numbers("", body), [])
        self.assertEqual(orch.superseded_numbers("", "rework of #255\n"), [255])

    def test_a_source_that_is_not_an_open_pr_is_never_touched(self):
        issue = item("vws-iam", 255, body="Part of #171\n")
        cases = {
            "missing": [pr("vws-iam", 255, state="closed")],
            "not a PR": [issue],
            "draft target": [pr("vws-iam", 255, draft=True)],
            "newer PR closed": [pr("vws-iam", 255), pr("vws-iam", 264, body="Rework of #255\n", state="closed")],
            "newer PR draft": [pr("vws-iam", 255), pr("vws-iam", 264, body="Rework of #255\n", draft=True)],
            "newer PR other base": [pr("vws-iam", 255), pr("vws-iam", 264, body="Rework of #255\n", base="release")],
            "self reference": [pr("vws-iam", 255, body="Rework of #255\n")],
            "cross repo": [pr("vws-deploy", 255), pr("vws-iam", 264, body="Rework of vws-deploy#255\n")],
            "older names newer": [pr("vws-iam", 255, body="Rework of #264\n"),
                                  pr("vws-iam", 264, body="Part of #171\n", hours=90)],
        }
        for why, items in cases.items():
            with self.subTest(why):
                self.assertEqual(closes(run(*items)), [])

    def test_cross_references_close_only_the_older_pr(self):
        a = pr("vws-iam", 255, body="Rework of #264\n")
        b = pr("vws-iam", 264, body="Rework of #255\n", hours=90)
        self.assertEqual([(x["target"], x["source"]) for x in closes(run(a, b))], [("vws-iam#255", "vws-iam#264")])

    def test_two_new_prs_claiming_one_source_close_it_once(self):
        old = pr("vws-iam", 255)
        first = pr("vws-iam", 264, body="Rework of #255\n", hours=90)
        second = pr("vws-iam", 265, body="Rebuild of #255\n", hours=80)
        [a] = closes(run(old, first, second))
        self.assertEqual((a["target"], a["source"]), ("vws-iam#255", "vws-iam#264"))


class Recheck(unittest.TestCase):
    class G:
        def __init__(self, old, source):
            self.old, self.source = old, source

        def get(self, path):
            return (self.old if path.endswith("/pulls/255") else self.source), False

    def test_still_the_source(self):
        g = self.G({"state": "open"}, {"state": "open", "title": "feat: x", "body": "Rework of #255\n"})
        self.assertTrue(orch.recheck_superseded(g, "vws-iam#255", "vws-iam#264"))
        # A report link was trusted at collect: only the two PR states are re-read.
        self.assertTrue(orch.recheck_superseded(g, "vws-iam#255", "vws-iam#264", "report"))

    def test_a_changed_pr_skips_the_close(self):
        target = {"state": "open"}
        source = {"state": "open", "title": "feat: x", "body": "Rework of #255\n"}
        cases = {
            "target closed": (dict(target, state="closed"), source),
            "target merged": (dict(target, merged=True), source),
            "source closed": (target, dict(source, state="closed")),
            "source merged": (target, dict(source, merged=True)),
            "claim dropped": (target, dict(source, body="Part of #171\n")),
        }
        for why, (old, src) in cases.items():
            with self.subTest(why):
                self.assertFalse(orch.recheck_superseded(self.G(old, src), "vws-iam#255", "vws-iam#264"))


class SupersededTick(test_runtime.Env):
    """An apply tick closes the old PR, links the new one, and leaves the new PR alone."""
    O = test_runtime.O

    def issue(self, number, body, head):
        return {"number": number, "html_url": f"{U}/vws-iam/pull/{number}", "state": "open",
                "created_at": "2026-10-01T00:00:00Z", "body": body, "title": f"feat: {number}",
                "labels": [], "pull_request": {}, "updated_at": orch.iso(NOW - 3600)}

    def setup_route(self):
        old, new = self.issue(255, "Part of #171\n", "1" * 40), self.issue(264, "Rework of #255\n", "2" * 40)
        r = test_runtime.routes()
        r[f"{self.O}/vws-deploy/issues"] = {"body": []}
        r[f"{self.O}/vws-deploy/pulls"] = {"body": []}
        r[f"{self.O}/vws-iam/issues"] = {"body": [old, new]}
        r[f"{self.O}/vws-iam/pulls"] = {"body": [
            {"number": 255, "draft": False, "base": {"ref": "main"}, "head": {"sha": "1" * 40}},
            {"number": 264, "draft": False, "base": {"ref": "main"}, "head": {"sha": "2" * 40}}]}
        for n, body, head in ((255, old["body"], "1" * 40), (264, new["body"], "2" * 40)):
            r[f"{self.O}/vws-iam/pulls/{n}"] = {"body": {
                "number": n, "state": "open", "merged": False, "title": f"feat: {n}", "body": body,
                "head": {"sha": head}}}
            r[f"{self.O}/vws-iam/commits/{head}/check-runs"] = {"body": {"total_count": 0, "check_runs": []}}
            r[f"{self.O}/vws-iam/commits/{head}/status"] = {"body": {"state": "success", "statuses": []}}
        self.set_routes(r)

    def test_apply_closes_the_old_pr_with_the_link(self):
        self.setup_route()
        self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_BOARD="off")
        wrote = [(c["method"], c["path"], json.loads(c["stdin"]) if c["stdin"] else None)
                 for c in self.writes()]
        self.assertEqual(wrote, [
            ("POST", f"{self.O}/vws-iam/issues/255/comments",
             {"body": f"Superseded by {U}/vws-iam/pull/264.\n\nThe newer PR is this PR's rework or rebuild, "
                      f"so this PR is closed to leave one open PR for the work.\n\n"
                      f"Idempotency key: `{orch.superseded_key('vws-iam#255', 'vws-iam#264')}`"}),
            ("PATCH", f"{self.O}/vws-iam/pulls/255", {"state": "closed"}),
        ])
        self.assertEqual([(e["target"], e["action"]) for e in self.log()
                          if e["event"] == "applied" and e.get("action") == "close_pr"],
                         [("vws-iam#255", "close_pr")])

    def test_a_closed_target_writes_nothing(self):
        self.setup_route()
        with open(self.routes_file) as f:
            r = json.load(f)
        r[f"{self.O}/vws-iam/pulls/255"] = {"body": {"state": "closed", "merged": False}}
        with open(self.routes_file, "w") as f:
            json.dump(r, f)
        self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_BOARD="off")
        self.assertEqual(self.writes(), [])

    def test_a_retried_tick_does_not_repost_the_comment(self):
        # The comment landed and the close did not (a crash between the two writes): the next tick closes
        # the PR without a second comment.
        self.setup_route()
        key = orch.superseded_key("vws-iam#255", "vws-iam#264")
        with open(self.routes_file) as f:
            r = json.load(f)
        r[f"{self.O}/vws-iam/issues/255/comments"] = {"body": [{
            "id": 1, "user": {"login": "popododo0720"}, "author_association": "OWNER",
            "created_at": "2026-10-21T11:00:00Z", "body": f"Superseded by {U}/vws-iam/pull/264.\n\n"
                                                         f"Idempotency key: `{key}`"}]}
        with open(self.routes_file, "w") as f:
            json.dump(r, f)
        self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_BOARD="off")
        self.assertEqual([(c["method"], c["path"]) for c in self.writes()],
                         [("PATCH", f"{self.O}/vws-iam/pulls/255")])

    def test_another_payload_never_closes_a_pr(self):
        self.assertTrue(orch.allowed("PATCH", f"{self.O}/vws-iam/pulls/255"))
        gh = orch.Gh()
        with self.assertRaises(PermissionError):
            gh.write("PATCH", f"{self.O}/vws-iam/pulls/255", {"state": "open"})
        self.assertEqual(self.calls(), [], "a refused call never reaches gh")


if __name__ == "__main__":
    unittest.main()
