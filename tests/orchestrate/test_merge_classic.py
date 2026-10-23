"""vws-deploy#408 follow-up: required checks that only classic branch protection lists keep the success-only
rule (AGENTS.md insider-risk rule 5). GitHub counts a skipped or neutral required check as passing, so
mergeable_state alone can be clean. The classic list comes from GET branches/main (no Administration read).
Run: python3 -m unittest discover -s tests/orchestrate"""
import os
import shutil
import unittest

import test_runtime as rt
from test_merge import MAIN, O, branch, routes

HEAD = "1" * 40
BRANCH = f"{O}/vws-deploy/branches/main"
RULES = f"{O}/vws-deploy/rules/branches/main"
RUNS = f"{O}/vws-deploy/commits/{HEAD}/check-runs"
STATUS = f"{O}/vws-deploy/commits/{HEAD}/status"
APPLY = dict(ORCHESTRATE_MODE="apply", ORCHESTRATE_MERGE="apply", ORCHESTRATE_SCOPE="vws-deploy#500")


def add_run(r, name, conclusion="success", app=15368, rid=90):
    body = r[RUNS]["body"]
    body["check_runs"].append({"id": rid, "name": name, "app": {"id": app}, "status": "completed",
                               "conclusion": conclusion, "head_sha": HEAD})
    body["total_count"] = len(body["check_runs"])


class Base(rt.Env):
    def fresh(self, r):
        """One isolated tick per case: new routes, no calls, no state from an earlier case."""
        self.set_routes(r)
        if os.path.exists(self.calls_file):
            os.remove(self.calls_file)
        shutil.rmtree(self.env["ORCHESTRATE_STATE_DIR"], ignore_errors=True)

    def apply_tick(self, r):
        self.fresh(r)
        self.assertEqual(self.tick(**APPLY), 0, "a read error or a refusal never fails the tick")
        return [c["path"] for c in self.writes() if c["method"] == "PUT"]

    def check_(self):
        return [e for e in self.log() if e.get("kind") == "merge_check" and e["target"] == "vws-deploy#500"][-1]

    def unreadable(self):
        return [e for e in self.log() if e.get("kind") == "merge_repo_unreadable"]

    def required(self, r):
        snap_file = os.path.join(self.tmp, "snap.json")
        self.fresh(r)
        self.tick("--dump-snapshot", snap_file)
        return rt.load(snap_file)["merge"]["vws-deploy"]


class ClassicRequiredChecks(Base):
    def test_classic_only_check_skipped_or_neutral_never_merges(self):
        for conclusion in ("skipped", "neutral"):
            with self.subTest(conclusion=conclusion):
                r = routes()
                r[BRANCH] = {"body": branch(MAIN, classic=[("e2e", 15368)])}
                add_run(r, "e2e", conclusion)
                self.assertEqual(self.apply_tick(r), [])
                self.assertIn(f"required_{conclusion}:e2e", self.check_()["reasons"])

    def test_classic_only_check_missing_never_merges(self):
        r = routes()
        r[BRANCH] = {"body": branch(MAIN, classic=[("e2e", 15368)])}
        self.assertEqual(self.apply_tick(r), [])
        self.assertIn("required_missing:e2e", self.check_()["reasons"])

    def test_classic_only_check_success_merges(self):
        r = routes()
        r[BRANCH] = {"body": branch(MAIN, classic=[("e2e", 15368)])}
        add_run(r, "e2e", "success")
        self.assertEqual(self.apply_tick(r), [f"{O}/vws-deploy/pulls/500/merge"])

    def test_classic_app_id_must_match(self):
        r = routes()
        r[BRANCH] = {"body": branch(MAIN, classic=[("e2e", 999)])}
        add_run(r, "e2e", "success", app=15368)
        self.assertEqual(self.apply_tick(r), [])
        self.assertIn("required_missing:e2e", self.check_()["reasons"])
        # A commit status has no app, so it cannot satisfy an app-bound classic check either.
        r[STATUS] = {"body": {"state": "success", "total_count": 1, "statuses": [{"context": "e2e", "state": "success"}]}}
        r[RUNS]["body"]["check_runs"] = [c for c in r[RUNS]["body"]["check_runs"] if c["name"] != "e2e"]
        r[RUNS]["body"]["total_count"] = len(r[RUNS]["body"]["check_runs"])
        self.assertEqual(self.apply_tick(r), [])
        self.assertIn("required_missing:e2e", self.check_()["reasons"])

    def test_classic_any_app_check_is_satisfied_by_any_source(self):
        # app_id null (any app): a success from any app or a success commit status counts.
        r = routes()
        r[BRANCH] = {"body": branch(MAIN, classic=[("e2e", None)])}
        add_run(r, "e2e", "success", app=4242)
        self.assertEqual(self.apply_tick(r), [f"{O}/vws-deploy/pulls/500/merge"])
        r = routes()
        r[BRANCH] = {"body": branch(MAIN, classic=[("e2e", None)])}
        r[STATUS] = {"body": {"state": "success", "total_count": 1, "statuses": [{"context": "e2e", "state": "success"}]}}
        self.assertEqual(self.apply_tick(r), [f"{O}/vws-deploy/pulls/500/merge"])
        r[STATUS]["body"]["statuses"][0]["state"] = "pending"
        r[STATUS]["body"]["state"] = "pending"
        self.assertEqual(self.apply_tick(r), [])

    def test_classic_context_without_checks_entry_fails_closed(self):
        # A context listed only in `contexts` has no readable app binding; it must not merge.
        r = routes()
        b = branch(MAIN)
        b["protection"]["required_status_checks"].update(enforcement_level="everyone", contexts=["e2e"])
        r[BRANCH] = {"body": b}
        add_run(r, "e2e", "success", app=4242)
        self.assertEqual(self.apply_tick(r), [])
        self.assertNotEqual(self.unreadable(), [])

    def test_classic_check_without_app_id_key_fails_closed(self):
        r = routes()
        b = branch(MAIN, classic=[("e2e", 15368)])
        del b["protection"]["required_status_checks"]["checks"][0]["app_id"]
        r[BRANCH] = {"body": b}
        add_run(r, "e2e", "success", app=4242)
        self.assertEqual(self.apply_tick(r), [])
        self.assertNotEqual(self.unreadable(), [])

    def test_union_with_rulesets_deduped_by_context_and_app(self):
        r = routes()
        r[BRANCH] = {"body": branch(MAIN, classic=[("lint", 15368), ("e2e", 15368), ("lint", None)])}
        r[RULES] = {"body": [{"type": "required_status_checks", "parameters": {"required_status_checks": [
            {"context": "lint", "integration_id": 15368}, {"context": "english", "integration_id": 15368}]}}]}
        self.assertEqual(self.required(r)["required"], [
            {"context": "e2e", "app_id": 15368}, {"context": "english", "app_id": 15368},
            {"context": "lint", "app_id": None}, {"context": "lint", "app_id": 15368}])

    def test_no_classic_protection_means_rulesets_only(self):
        # GitHub answers the protection endpoint 404 "Branch not protected" here; branches/main shows
        # protected false, or protection.enabled false when only rulesets protect main.
        for b in (branch(MAIN, protected=False, enabled=False), branch(MAIN, protected=True, enabled=False),
                  branch(MAIN, protected=False, enabled=False, classic=[("e2e", 15368)])):
            with self.subTest(b=b["protected"]):
                r = routes()
                r[BRANCH] = {"body": b}
                self.assertEqual(self.apply_tick(r), [f"{O}/vws-deploy/pulls/500/merge"])
                self.assertEqual(self.unreadable(), [])

    def test_unreadable_or_malformed_branch_protection_fails_closed(self):
        def without(key):
            b = branch(MAIN)
            del b[key]
            return b

        def edit(fn):
            b = branch(MAIN, classic=[("e2e", 15368)])
            fn(b["protection"])
            return b

        cases = {
            "branch 404": {"status": 404, "body": {"message": "Branch not found"}},
            "branch 403": {"status": 403, "body": {"message": "Resource not accessible by personal access token"}},
            "body is a list": {"body": [branch(MAIN)]},
            "no protected field": {"body": without("protected")},
            "protected not a bool": {"body": dict(branch(MAIN), protected="yes")},
            "protection hidden": {"body": without("protection")},
            "protection null": {"body": dict(branch(MAIN), protection=None)},
            "enabled missing": {"body": edit(lambda p: p.pop("enabled"))},
            "no required_status_checks": {"body": edit(lambda p: p.pop("required_status_checks"))},
            "checks not a list": {"body": edit(lambda p: p["required_status_checks"].update(checks={"e2e": 1}))},
            "contexts missing": {"body": edit(lambda p: p["required_status_checks"].pop("contexts"))},
            "check without context": {"body": edit(lambda p: p["required_status_checks"]["checks"].append({"app_id": 1}))},
            "app_id not an int": {"body": edit(lambda p: p["required_status_checks"]["checks"].append(
                {"context": "x", "app_id": "15368"}))},
            "app_id a bool": {"body": edit(lambda p: p["required_status_checks"]["checks"].append(
                {"context": "x", "app_id": True}))},
        }
        for why, route in cases.items():
            with self.subTest(why):
                r = routes()
                add_run(r, "e2e", "success")
                r[BRANCH] = route
                self.assertEqual(self.apply_tick(r), [])
                notes = self.unreadable()
                self.assertTrue(notes and notes[-1]["repo"] == "vws-deploy" and notes[-1]["error"], why)
                self.assertFalse([c for c in self.calls() if "/protection" in c["path"]], "never the protection API")


if __name__ == "__main__":
    unittest.main()
