"""The verify-queue labels as one orchestrator rule (vws-deploy#487): needs-verify and verify-approved follow
the PR's current head in every vws-* repo, whether or not the repo has verify-queue.yml.
Run: python3 -m unittest discover -s tests/orchestrate -p 'test_*.py'"""
import json
import unittest

from orch_testlib import CFG, item, orch, ts
from test_runtime import O, Env, pr_issue, routes

NV, VA = "needs-verify", "verify-approved"


def review(it, result, sha=None, hours_ago=1, rid=1):
    return {"id": rid, "url": it["url"], "at": ts(hours_ago), "sha": sha or it["head_sha"], "result": result, "body": ""}


def pr(number, labels=(), ci="green", verdict=None, old_verdict=None, created_hours_ago=100, repo="vws-deploy", **kw):
    """A PR as collect leaves it for this rule: verify_ci set, reviews read."""
    it = item(repo, number, labels=labels, is_pr=True, created_hours_ago=created_hours_ago, **kw)
    it["verify_ci"] = ci
    it["reviews"] = []
    if old_verdict:
        it["reviews"].append(review(it, old_verdict, sha="f" * 40, hours_ago=5, rid=2))
    if verdict:
        it["reviews"].append(review(it, verdict))
    return it


def plan(*items, cfg=None):
    notes = []
    acts = orch.decide_verify_labels({f"{i['repo']}#{i['number']}": i for i in items}, cfg or CFG, notes)
    return [(a["type"], a["target"], a["label"]) for a in acts], notes


def add(n, label, repo="vws-deploy"):
    return ("add_label", f"{repo}#{n}", label)


def rm(n, label, repo="vws-deploy"):
    return ("remove_label", f"{repo}#{n}", label)


class Table(unittest.TestCase):
    """The four rows of the #487 table, from any starting labels."""

    def test_green_without_verdict_waits_for_verify(self):
        self.assertEqual(plan(pr(1))[0], [add(1, NV)])
        self.assertEqual(plan(pr(1, labels=(VA,)))[0], [add(1, NV), rm(1, VA)])
        self.assertEqual(plan(pr(1, labels=(NV,)))[0], [], "already right: no write (idempotent)")

    def test_pass_on_head_is_approved(self):
        self.assertEqual(plan(pr(1, verdict="pass"))[0], [add(1, VA)])
        self.assertEqual(plan(pr(1, labels=(NV,), verdict="pass"))[0], [rm(1, NV), add(1, VA)])
        self.assertEqual(plan(pr(1, labels=(VA,), verdict="pass"))[0], [])

    def test_both_labels_reduce_to_approved(self):
        # vws-compute#125 carried needs-verify and verify-approved at once.
        self.assertEqual(plan(pr(125, labels=(NV, VA), verdict="pass", repo="vws-compute"))[0],
                         [rm(125, NV, "vws-compute")])

    def test_fail_on_head_has_neither(self):
        self.assertEqual(plan(pr(1, labels=(NV, VA), verdict="fail"))[0], [rm(1, NV), rm(1, VA)])
        self.assertEqual(plan(pr(1, verdict="fail"))[0], [])
        # A newer PASS on the same head wins over an older FAIL, and the other way round.
        it = pr(1, verdict="pass")
        it["reviews"].insert(0, review(it, "fail", hours_ago=3, rid=3))
        self.assertEqual(plan(it)[0], [add(1, VA)])
        it = pr(1, labels=(VA,), verdict="fail")
        it["reviews"].insert(0, review(it, "pass", hours_ago=3, rid=3))
        self.assertEqual(plan(it)[0], [rm(1, VA)])

    def test_failed_vws_verify_check_on_head_counts_as_fail(self):
        it = pr(1, labels=(NV,))
        it["verify_check_failed"] = True
        self.assertEqual(plan(it)[0], [rm(1, NV)])

    def test_red_or_pending_has_neither(self):
        for ci in ("red", "pending"):
            with self.subTest(ci):
                self.assertEqual(plan(pr(1, labels=(NV, VA), ci=ci))[0], [rm(1, NV), rm(1, VA)])
                self.assertEqual(plan(pr(1, ci=ci))[0], [])

    def test_pass_holds_while_ci_reruns(self):
        # The verdict row wins: a PASS on this head keeps verify-approved; Rule D checks CI again before merging.
        self.assertEqual(plan(pr(1, labels=(VA,), ci="pending", verdict="pass"))[0], [])

    def test_draft_other_base_or_closed_has_neither(self):
        self.assertEqual(plan(pr(1, labels=(NV, VA), ci="ineligible", verdict="pass"))[0], [rm(1, NV), rm(1, VA)])
        self.assertEqual(plan(pr(1, ci="ineligible"))[0], [])

    def test_head_moved_after_pass(self):
        # The PASS was for an older head: it no longer counts. While CI runs on the new head both labels are off;
        # once it is green the PR waits for verify again.
        self.assertEqual(plan(pr(1, labels=(VA,), ci="pending", old_verdict="pass"))[0], [rm(1, VA)])
        self.assertEqual(plan(pr(1, labels=(VA,), ci="green", old_verdict="pass"))[0], [add(1, NV), rm(1, VA)])

    def test_head_moved_after_fail_waits_again(self):
        self.assertEqual(plan(pr(1, old_verdict="fail"))[0], [add(1, NV)])

    def test_verdict_comment_on_head_counts(self):
        it = pr(1, labels=(NV,))
        it["comments"] = [{"id": 9, "url": it["url"] + "#issuecomment-9", "created_at": ts(1),
                           "body": f"# vws-deploy#1 @ {it['head_sha']}\nResult: FAIL\n"}]
        self.assertEqual(plan(it)[0], [rm(1, NV)])


class Parked(unittest.TestCase):
    """AC7: a blocked (or held) PR never gets a label from this rule; it only loses them."""

    def test_blocked_gets_removals_only(self):
        for lab in orch.OPT_OUT_LABELS:
            with self.subTest(lab):
                self.assertEqual(plan(pr(483, labels=(lab,)))[0], [], "green, no verdict: no needs-verify")
                self.assertEqual(plan(pr(483, labels=(lab, NV)))[0], [rm(483, NV)], "a parked PR does not wait for verify")
                self.assertEqual(plan(pr(483, labels=(lab, VA), verdict="pass"))[0], [], "PASS on head: kept")
                self.assertEqual(plan(pr(483, labels=(lab,), verdict="pass"))[0], [], "PASS: no verify-approved")
                self.assertEqual(plan(pr(483, labels=(lab, NV), verdict="pass"))[0], [rm(483, NV)])
                self.assertEqual(plan(pr(483, labels=(lab, NV, VA), ci="red"))[0], [rm(483, NV), rm(483, VA)])

    def test_rule_a_never_wakes_verify_for_a_parked_pr(self):
        # A needs-verify label that is still on a blocked PR (hand-added, or from an old verify-queue run)
        # must not count toward the wake in the tick that removes it.
        snap = {"run_id": "x", "repos": [{"name": "vws-deploy", "archived": False}], "deps": {}, "status": {"wakes": []},
                "labels": {"vws-deploy": [NV, VA, "blocked"]},
                "items": {f"vws-deploy#{n}": pr(n, labels=(NV, "blocked")) for n in (1, 2, 3)}}
        res = orch.decide(snap, {}, orch.utc(ts(0)), CFG, set())
        self.assertEqual(res["pending_count"], 0)
        self.assertNotIn("wake_verify", [a["type"] for a in res["actions"]])
        self.assertEqual(sorted(n["target"] for n in res["notes"] if n["kind"] == "needs_verify_parked"),
                         ["vws-deploy#1", "vws-deploy#2", "vws-deploy#3"])

    def test_pr_483_shape(self):
        # Recorded: #483 blocked by the lead, CI green, needs-verify removed by hand; nothing may put it back.
        self.assertEqual(plan(pr(483, labels=("blocked", "grok")))[0], [])


class Order(unittest.TestCase):
    def test_longest_waiting_first(self):
        acts, _ = plan(pr(3, created_hours_ago=1), pr(1, created_hours_ago=50), pr(2, created_hours_ago=10))
        self.assertEqual(acts, [add(1, NV), add(2, NV), add(3, NV)])

    def test_only_prs_collect_read(self):
        # No verify_ci (an issue, or a PR collect did not read): nothing to decide.
        it = item("vws-deploy", 5, labels=(NV,), is_pr=True)
        issue = item("vws-deploy", 6, labels=(NV,))
        self.assertEqual(plan(it, issue)[0], [])
        unknown = pr(7, labels=(NV,), ci="unknown")
        self.assertEqual(plan(unknown)[0], [], "an unreadable CI state writes nothing")

    def test_switch_off(self):
        self.assertEqual(plan(pr(1), cfg=dict(CFG, ORCHESTRATE_VERIFY_LABELS="off"))[0], [])

    def test_reasons_name_the_row(self):
        acts = orch.decide_verify_labels({"vws-deploy#1": pr(1, labels=(VA,))}, CFG, [])
        self.assertTrue(all(a["reason"] for a in acts))
        self.assertIn("green", acts[0]["reason"])

    def test_runs_inside_decide_after_the_other_rules(self):
        snap = {"run_id": "x", "repos": [{"name": "vws-deploy", "archived": False}], "items": {"vws-deploy#1": pr(1)},
                "deps": {}, "status": {"wakes": []}, "labels": {"vws-deploy": [NV, VA]}}
        res = orch.decide(snap, {}, orch.utc(ts(0)), CFG, set())
        self.assertIn(("add_label", "vws-deploy#1", NV),
                      [(a["type"], a["target"], a.get("label")) for a in res["actions"]])


class PrCi(unittest.TestCase):
    """pr_ci: green, pending or red from the head's check runs and statuses, verify checks excluded."""

    @staticmethod
    def cr(rid, name, status="completed", conclusion="success"):
        return {"id": rid, "name": name, "status": status, "conclusion": conclusion}

    def ci(self, runs, statuses=(), total=None):
        body = {"check_runs": list(runs), "total_count": len(runs) if total is None else total}
        return orch.pr_ci(body, {"statuses": list(statuses)})

    def test_rows(self):
        base = [self.cr(1, "test"), self.cr(2, "verify-gate", conclusion="failure"),
                self.cr(3, "gate / verify-gate", conclusion="failure"), self.cr(4, "vws-verify", "in_progress", None),
                self.cr(5, "needs-verify", "in_progress", None), self.cr(6, "verify-reset", conclusion="skipped")]
        self.assertEqual(self.ci(base), ("green", False))
        self.assertEqual(self.ci(base + [self.cr(9, "e2e", "queued", None)]), ("pending", False))
        self.assertEqual(self.ci(base + [self.cr(9, "e2e", conclusion="cancelled")]), ("red", False))
        self.assertEqual(self.ci(base + [self.cr(9, "e2e", conclusion="failure"), self.cr(10, "e2e")]), ("green", False),
                         "a rerun supersedes the failed attempt")
        self.assertEqual(self.ci(base, [{"context": "ext", "state": "pending"}]), ("pending", False))
        self.assertEqual(self.ci(base, [{"context": "ext", "state": "error"}]), ("red", False))
        self.assertEqual(self.ci([]), ("pending", False), "no checks yet")
        self.assertEqual(self.ci(base, total=500), ("pending", False), "a truncated page is not green")
        self.assertEqual(self.ci(base + [self.cr(7, "vws-verify", conclusion="failure")]), ("green", True))
        self.assertEqual(orch.pr_ci([], []), ("pending", False), "an empty stub page")


class Tick(Env):
    """Through collect, decide and apply against the stub gh."""

    def lib_routes(self, prs, labels=(NV, VA, "blocked")):
        r = routes()
        r[f"{O}/vws-lib"] = {"body": {"archived": False}}
        r[f"{O}/vws-lib/labels"] = {"body": [{"name": x} for x in labels]}
        r[f"{O}/vws-lib/pulls"] = {"body": [{"number": n, "draft": False, "base": {"ref": "main"}, "head": {"sha": f"{n:040x}"}}
                                           for n, _l, _h in prs]}
        r[f"{O}/vws-lib/issues"] = {"body": [dict(pr_issue("vws-lib", n, lab), created_at=h) for n, lab, h in prs]}
        for n, _l, _h in prs:
            r[f"{O}/vws-lib/commits/{n:040x}/check-runs"] = {"body": {"total_count": 1, "check_runs": [
                {"id": 1, "name": "test", "status": "completed", "conclusion": "success"}]}}
            r[f"{O}/vws-lib/commits/{n:040x}/status"] = {"body": {"state": "success", "statuses": []}}
        for n in range(400, 405):
            r[f"{O}/vws-deploy/commits/{n:040x}/check-runs"] = {"body": {"total_count": 1, "check_runs": [
                {"id": 1, "name": "test", "status": "completed", "conclusion": "success"}]}}
        with open(self.repos, "w") as f:
            f.write("# repo\tkind\nvws-deploy\tgo\nvws-hypervisor\tgo\nvws-lib\tlib\n")
        self.set_routes(r)

    def label_writes(self):
        out = []
        for c in self.writes():
            if c["path"].startswith(f"{O}/vws-lib/issues/") and "/labels" in c["path"]:
                n = int(c["path"].split("/")[4])
                out.append((c["method"], n, json.loads(c["stdin"])["labels"][0] if c["method"] == "POST" else c["path"].rsplit("/", 1)[1]))
        return out

    def test_repo_without_verify_queue_gets_the_label(self):
        # AC4: vws-lib has no verify-queue.yml; the orchestrator alone labels its green PR.
        self.lib_routes([(7, (), "2026-10-01T00:00:00Z")])
        self.assertEqual(self.tick(ORCHESTRATE_MODE="apply"), 0)
        self.assertEqual(self.label_writes(), [("POST", 7, NV)])

    def test_second_tick_writes_nothing(self):
        self.lib_routes([(7, (NV,), "2026-10-01T00:00:00Z")])
        self.assertEqual(self.tick(ORCHESTRATE_MODE="apply"), 0)
        self.assertEqual(self.label_writes(), [])

    def test_blocked_removal_only_through_a_tick(self):
        self.lib_routes([(7, ("blocked",), "2026-10-01T00:00:00Z"), (8, ("blocked", VA), "2026-10-01T00:00:00Z")])
        self.assertEqual(self.tick(ORCHESTRATE_MODE="apply"), 0)
        self.assertEqual(self.label_writes(), [("DELETE", 8, VA)])

    def test_cap_oldest_first(self):
        self.lib_routes([(7, (), "2026-10-03T00:00:00Z"), (8, (), "2026-10-01T00:00:00Z"), (9, (), "2026-10-02T00:00:00Z")])
        # The default routes also plan a verify wake and a dispatch (2 writes, planned before this rule).
        self.assertEqual(self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_MAX_WRITES="4"), 0)
        self.assertEqual(self.label_writes(), [("POST", 8, NV), ("POST", 9, NV)])
        self.assertTrue(any(e.get("reason") == "write cap 4 reached" and e.get("target") == "vws-lib#7" for e in self.log()))

    def test_scope(self):
        self.lib_routes([(7, (), "2026-10-01T00:00:00Z"), (8, (), "2026-10-02T00:00:00Z")])
        self.assertEqual(self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_SCOPE="vws-lib#8"), 0)
        self.assertEqual(self.label_writes(), [("POST", 8, NV)])

    def test_label_missing_in_repo_is_skipped(self):
        self.lib_routes([(7, (), "2026-10-01T00:00:00Z")], labels=(VA,))
        self.assertEqual(self.tick(ORCHESTRATE_MODE="apply"), 0)
        self.assertEqual(self.label_writes(), [])

    def test_new_read_errors_skip_the_pr_not_the_tick(self):
        # A reviews read on an unlabeled PR is new in #487: its error skips that PR only.
        self.lib_routes([(7, (), "2026-10-01T00:00:00Z")])
        r = json.load(open(self.routes_file))
        r[f"{O}/vws-lib/pulls/7/reviews"] = {"status": 403, "body": {"message": "Forbidden"}}
        self.set_routes(r)
        self.assertEqual(self.tick(ORCHESTRATE_MODE="apply"), 0)
        self.assertEqual(self.label_writes(), [])

    def test_gh_allows_the_two_label_deletes(self):
        gh = orch.Gh()
        for lab in (NV, VA):
            gh.write("DELETE", f"{O}/vws-lib/issues/7/labels/{lab}", None)
        with self.assertRaises(PermissionError):
            gh.write("DELETE", f"{O}/vws-lib/issues/7/labels/ready", None)


if __name__ == "__main__":
    unittest.main()
