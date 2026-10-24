"""Rule D tests for .github/orchestrate/orchestrate.py (vws-deploy#346): serialized merges of PRs that
passed vws-verify. decide() table tests for every eligibility condition, the per-repo lock and the
green-wait, plus apply against the stub gh. Run: python3 -m unittest discover -s tests/orchestrate"""
import json
import os
import shutil
import unittest
from unittest import mock

import test_runtime as rt
from orch_testlib import CFG, NOW, item, orch, snapshot

O = "repos/popododo0720"
MAIN = "a" * 40
MAIN2 = "b" * 40
MERGED = "c" * 40
REQUIRED = [{"context": "lint", "app_id": 15368}, {"context": "pr-hygiene", "app_id": 15368},
            {"context": "verify-gate", "app_id": 15368}, {"context": "vws-verify", "app_id": 5313004}]


def check(cid, name, sha, conclusion="success", app=15368, status="completed"):
    return {"id": cid, "name": name, "app_id": app, "status": status, "conclusion": conclusion, "head_sha": sha}


def green_checks(sha):
    return [check(1, "lint", sha), check(2, "pr-hygiene", sha), check(3, "verify-reset", sha, "skipped"),
            check(4, "vws-verify", sha, app=5313004), check(5, "verify-gate", sha)]


def candidate(repo="vws-deploy", number=500, labels=("verify-approved",), body="Part of #346\n\nAgent: vws-platform",
              info=None, **kw):
    it = item(repo, number, labels=labels, is_pr=True, body=body, **kw)
    sha = it["head_sha"]
    mi = {"head_sha": sha, "state": "open", "merged": False, "draft": it["draft"], "base": it["base"], "mergeable": True,
          "mergeable_state": "clean", "title": f"ci: change {number}", "checks": green_checks(sha), "statuses": [], "files": [],
          "checks_truncated": False}
    mi.update(info or {})
    it["merge_info"] = mi
    return it


def ctx(main_ci="green", main_sha=MAIN, required=None, **kw):
    c = {"main_sha": main_sha, "main_ci": main_ci, "required": REQUIRED if required is None else required}
    c.update(kw)
    return c


def snap(*items, merge=None, keys=(), deps=None):
    s = snapshot(*items, deps=deps)
    s["merge"] = {"vws-deploy": ctx()} if merge is None else merge
    s["status"]["keys"] = list(keys)
    return s


def run(s, state=None, cfg=None, now=NOW):
    return orch.decide(s, state or {}, now, cfg or CFG, set())


def merges(r):
    return [a["target"] for a in r["actions"] if a["type"] == "merge"]


def reasons(r, target):
    return next(n["reasons"] for n in r["notes"] if n["kind"] == "merge_check" and n["target"] == target)


class Eligibility(unittest.TestCase):
    def test_happy_path_payload(self):
        r = run(snap(candidate()))
        self.assertEqual(merges(r), ["vws-deploy#500"])
        a = r["actions"][-1]
        sha = candidate()["head_sha"]
        self.assertEqual(a["payload"], {"merge_method": "squash", "sha": sha, "commit_title": "ci: change 500 (#500)"})
        self.assertEqual(a["agent"], "vws-platform")
        self.assertEqual(reasons(r, "vws-deploy#500"), [])

    def test_owner_repo_part_of_is_named_in_the_reason(self):
        # vws-deploy#477: pr-hygiene rejects the owner/repo form (vws-deploy#482); Rule D says so
        # instead of the generic no_part_of.
        r = run(snap(candidate(body="Part of popododo0720/vws-deploy#412")))
        self.assertEqual(reasons(r, "vws-deploy#500"), ["part_of_owner_repo"])
        self.assertEqual(merges(r), [])

    def test_every_condition(self):
        sha = candidate()["head_sha"]
        bad_verify = [c for c in green_checks(sha) if c["name"] != "vws-verify"]
        cases = [
            ("draft", dict(draft=True), "draft"),
            ("base not main", dict(base="release"), "base_not_main"),
            ("no verify-approved label", dict(labels=()), "no_verify_approved"),
            ("opt-out label do-not-merge", dict(labels=("verify-approved", "do-not-merge")), "opted_out"),
            ("opt-out label hold", dict(labels=("verify-approved", "hold")), "opted_out"),
            ("opt-out label blocked", dict(labels=("verify-approved", "blocked")), "opted_out"),
            ("opt-out marker", dict(body="Part of #1\n<!-- vws-orchestrate:no-merge -->"), "opted_out"),
            ("verify-wake thread", dict(number=339), "opted_out"),
            ("status issue", dict(number=415), "opted_out"),
            ("vws-verify missing", dict(info={"checks": bad_verify}), "verify_check_missing"),
            ("vws-verify failure", dict(info={"checks": bad_verify + [check(9, "vws-verify", sha, "failure", 5313004)]}),
             "verify_check_failure"),
            ("vws-verify pending", dict(info={"checks": bad_verify + [check(9, "vws-verify", sha, None, 5313004, "in_progress")]}),
             "verify_check_in_progress"),
            ("vws-verify from another app", dict(info={"checks": bad_verify + [check(9, "vws-verify", sha, app=15368)]}),
             "verify_check_missing"),
            ("vws-verify on an older head", dict(info={"checks": bad_verify + [check(9, "vws-verify", "f" * 40, app=5313004)]}),
             "verify_check_missing"),
            ("required check failed", dict(info={"checks": green_checks(sha) + [check(9, "lint", sha, "failure")]}),
             "required_failure:lint"),
            ("required check pending", dict(info={"checks": green_checks(sha) + [check(9, "lint", sha, None, status="queued")]}),
             "required_pending:lint"),
            ("required check missing", dict(info={"checks": [c for c in green_checks(sha) if c["name"] != "lint"]}),
             "required_missing:lint"),
            ("required check from the wrong app", dict(info={"checks": [c for c in green_checks(sha) if c["name"] != "lint"]
                                                       + [check(9, "lint", sha, app=1)]}), "required_missing:lint"),
            ("other check failed", dict(info={"checks": green_checks(sha) + [check(9, "drift", sha, "failure")]}),
             "check_failed:drift"),
            ("other check running", dict(info={"checks": green_checks(sha) + [check(9, "e2e", sha, None, status="in_progress")]}),
             "check_pending:e2e"),
            ("commit status pending", dict(info={"statuses": [{"context": "ext", "state": "pending"}]}), "status_pending:ext"),
            ("checks truncated", dict(info={"checks_truncated": True}), "checks_truncated"),
            ("statuses truncated", dict(info={"statuses_truncated": True}), "statuses_truncated"),
            ("combined status failure", dict(info={"statuses": [{"context": "ext", "state": "success"}], "status_state": "failure"}),
             "combined_status_failure"),
            ("conflicts", dict(info={"mergeable": False, "mergeable_state": "dirty"}), "conflicts"),
            ("mergeable not computed", dict(info={"mergeable": None, "mergeable_state": "unknown"}), "mergeable_unknown"),
            ("blocked by protection", dict(info={"mergeable_state": "blocked"}), "mergeable_state:blocked"),
            ("head moved during collect", dict(info={"head_sha": "e" * 40}), "head_moved_during_collect"),
            ("closed meanwhile", dict(info={"state": "closed"}), "not_open"),
            ("no Part of", dict(body="Agent: x"), "no_part_of"),
            ("another owner's Part of", dict(body="Part of acme/vws-deploy#412"), "no_part_of"),
            ("closing keyword in title", dict(info={"title": "fix: thing, fixes #12"}), "closing_keyword_in_title"),
            ("dependency open", dict(body="Part of #1\nDepends on vws-iam#7"), "dep_unmet:vws-iam#7"),
        ]
        for name, kw, want in cases:
            with self.subTest(name):
                c = candidate(**kw)
                r = run(snap(c))
                self.assertEqual(merges(r), [], name)
                self.assertIn(want, reasons(r, f"vws-deploy#{c['number']}"))

    def test_verify_gate_required_by_name(self):
        # vws-deploy#393: verify-gate must pass on the head even when main's protection does not require it.
        sha = candidate()["head_sha"]
        no_gate = [c for c in green_checks(sha) if c["name"] != "verify-gate"]
        unrequired = {"vws-deploy": ctx(required=[q for q in REQUIRED if q["context"] != "verify-gate"])}
        cases = [
            ("verify-gate missing", no_gate, "verify_gate_missing"),
            ("verify-gate pending", no_gate + [check(9, "verify-gate", sha, None, status="in_progress")], "verify_gate_pending"),
            ("verify-gate failed", no_gate + [check(9, "verify-gate", sha, "failure")], "verify_gate_failure"),
            ("verify-gate skipped", no_gate + [check(9, "verify-gate", sha, "skipped")], "verify_gate_skipped"),
            ("verify-gate from another app", no_gate + [check(9, "verify-gate", sha, app=1)], "verify_gate_missing"),
            ("verify-gate on an older head", no_gate + [check(9, "verify-gate", "f" * 40)], "verify_gate_missing"),
            ("verify-gate failed in another workflow", [dict(c, suite_id=1) for c in green_checks(sha)]
             + [dict(check(9, "verify-gate", sha, "failure"), suite_id=2)], "verify_gate_failure"),
        ]
        for name, checks, want in cases:
            with self.subTest(name):
                r = run(snap(candidate(info={"checks": checks}), merge=unrequired))
                self.assertEqual(merges(r), [], name)
                self.assertIn(want, reasons(r, "vws-deploy#500"))
        r = run(snap(candidate(), merge=unrequired))
        self.assertEqual(merges(r), ["vws-deploy#500"], "verify-gate success and everything else OK merges")
        self.assertEqual(reasons(r, "vws-deploy#500"), [])

    def test_status_cannot_stand_in_for_an_app_bound_check(self):
        sha = candidate()["head_sha"]
        info = {"checks": [c for c in green_checks(sha) if c["name"] != "lint"],
                "statuses": [{"context": "lint", "state": "success"}]}
        r = run(snap(candidate(info=info)))
        self.assertIn("required_missing:lint", reasons(r, "vws-deploy#500"))
        unbound = [{"context": "lint", "app_id": None}] + REQUIRED[1:]
        r = run(snap(candidate(info=info), merge={"vws-deploy": ctx(required=unbound)}))
        self.assertEqual(merges(r), ["vws-deploy#500"], "a status may satisfy a requirement without an app")

    def test_ruleset_required_check_is_enforced(self):
        # vws-main ruleset: required checks come from rules/branches/main too (english, GitHub Actions).
        sha = candidate()["head_sha"]
        req = REQUIRED + [{"context": "english", "app_id": 15368}]
        r = run(snap(candidate(), merge={"vws-deploy": ctx(required=req)}))
        self.assertIn("required_missing:english", reasons(r, "vws-deploy#500"))
        ok = candidate(info={"checks": green_checks(sha) + [check(8, "english", sha)]})
        self.assertEqual(merges(run(snap(ok, merge={"vws-deploy": ctx(required=req)}))), ["vws-deploy#500"])

    def test_rerun_and_skipped_helpers_are_fine(self):
        sha = candidate()["head_sha"]
        checks = green_checks(sha) + [check(0, "verify-gate", sha, "failure"), check(6, "verify-rerun", sha, "skipped")]
        self.assertEqual(merges(run(snap(candidate(info={"checks": checks})))), ["vws-deploy#500"],
                         "an older failed verify-gate run is superseded by the newer success")

    def test_same_name_failure_in_another_workflow_blocks(self):
        # Two workflows (check suites) both have a job "lint"; the newer success must not hide the failure.
        sha = candidate()["head_sha"]
        checks = [dict(c, suite_id=1) for c in green_checks(sha)] + [dict(check(0, "lint", sha, "failure"), suite_id=2)]
        r = run(snap(candidate(info={"checks": checks})))
        self.assertEqual(merges(r), [])
        self.assertIn("check_failed:lint", reasons(r, "vws-deploy#500"))
        self.assertIn("required_failure:lint", reasons(r, "vws-deploy#500"))

    def test_rerun_in_same_suite_still_supersedes(self):
        sha = candidate()["head_sha"]
        checks = [dict(c, suite_id=1) for c in green_checks(sha)] + [dict(check(0, "lint", sha, "failure"), suite_id=1)]
        self.assertEqual(merges(run(snap(candidate(info={"checks": checks})))), ["vws-deploy#500"])

    def test_actions_rerun_in_new_suite_same_workflow(self):
        # An Actions re-run gets a new check suite; the same workflow makes the newest run decide.
        sha = candidate()["head_sha"]
        wf = ".github/workflows/verify-gate.yml"
        old = dict(check(10, "verify-gate", sha, "failure"), suite_id=1, workflow=wf, started_at="2026-10-22T01:00:00Z")
        new = dict(check(11, "verify-gate", sha), suite_id=2, workflow=wf, started_at="2026-10-22T02:00:00Z")
        base = [c for c in green_checks(sha) if c["name"] != "verify-gate"]
        self.assertEqual(merges(run(snap(candidate(info={"checks": base + [old, new]})))), ["vws-deploy#500"],
                         "older failure superseded by newer success")
        old2 = dict(old, conclusion="success")
        new2 = dict(new, conclusion="failure")
        r = run(snap(candidate(info={"checks": base + [new2, old2]})))
        self.assertEqual(merges(r), [])
        self.assertIn("check_failed:verify-gate", reasons(r, "vws-deploy#500"))
        queued = dict(new, id=12, status="queued", conclusion=None, started_at=None)
        r = run(snap(candidate(info={"checks": base + [old, new, queued]})))
        self.assertEqual(merges(r), [], "a queued re-run holds the gate")

    def test_unmapped_suite_keeps_old_failure_blocking(self):
        sha = candidate()["head_sha"]
        base = [c for c in green_checks(sha) if c["name"] != "verify-gate"]
        old = dict(check(10, "verify-gate", sha, "failure"), suite_id=1, workflow=None, started_at="2026-10-22T01:00:00Z")
        new = dict(check(11, "verify-gate", sha), suite_id=2, workflow=".github/workflows/verify-gate.yml",
                   started_at="2026-10-22T02:00:00Z")
        r = run(snap(candidate(info={"checks": base + [old, new], "unmapped": ["verify-gate"]})))
        self.assertEqual(merges(r), [])
        self.assertIn("check_failed:verify-gate", reasons(r, "vws-deploy#500"))
        note = [n for n in r["notes"] if n.get("kind") == "merge_check" and n["target"] == "vws-deploy#500"][0]
        self.assertEqual(note["unmapped_suite_checks"], ["verify-gate"])

    def test_merge_info_reads_check_suite(self):
        gh = mock.Mock()
        sha = "a" * 40
        gh.get.side_effect = [
            ({"head": {"sha": sha}, "base": {"ref": "main"}}, None),
            ({"total_count": 1, "check_runs": [{"id": 1, "name": "lint", "app": {"id": 15368}, "status": "completed",
                                                "conclusion": "success", "head_sha": sha, "check_suite": {"id": 77}},
                                               {"id": 2, "name": "x", "app": {"id": 15368}, "status": "completed",
                                                "conclusion": "success", "head_sha": sha, "check_suite": {"id": 78}}]}, None),
            ({"state": "success", "statuses": [], "total_count": 0}, None),
            ({"total_count": 1, "workflow_runs": [{"id": 5, "check_suite_id": 77, "workflow_id": 9,
                                                   "path": ".github/workflows/ci.yml", "event": "pull_request"}]}, None)]
        info = orch.merge_info(gh, "vws-deploy", 1)
        self.assertEqual([(c["suite_id"], c["workflow"]) for c in info["checks"]],
                         [(77, ".github/workflows/ci.yml@pull_request"), (78, None)])
        self.assertEqual(info["unmapped"], ["x"])
        self.assertEqual(gh.get.call_args_list[3].args[0],
                         f"repos/popododo0720/vws-deploy/actions/runs?head_sha={sha}&per_page=100")

    def test_dependency_met_and_required_unreadable(self):
        c = candidate(body="Part of #1\nDepends on vws-iam#7")
        self.assertEqual(merges(run(snap(c, deps={"vws-iam#7": {"state": "closed", "merged": True}}))), ["vws-deploy#500"])
        r = run(snap(candidate(), merge={"vws-deploy": ctx(required=None) | {"required": None}}))
        self.assertIn("required_checks_unreadable", reasons(r, "vws-deploy#500"))

    def test_agent_line_does_not_matter(self):
        for body in ("Part of #346", "Part of #346\nAgent: someone-else", "Part of #346\nAgent: "):
            with self.subTest(body):
                self.assertEqual(merges(run(snap(candidate(body=body)))), ["vws-deploy#500"])
        self.assertEqual(run(snap(candidate(body="Part of #346")))["actions"][-1]["agent"], "")

    def test_attempts_exhausted(self):
        c = candidate()
        state = {"merge_attempts": {f"vws-deploy#500@{c['head_sha']}": 2}}
        r = run(snap(c), state)
        self.assertIn("merge_attempts_exhausted", reasons(r, "vws-deploy#500"))
        self.assertEqual(merges(run(snap(c), {"merge_attempts": {f"vws-deploy#500@{c['head_sha']}": 1}})), ["vws-deploy#500"])

    def test_attempts_pruned_when_the_head_is_gone(self):
        r = run(snap(candidate()), {"merge_attempts": {"vws-deploy#1@" + "d" * 40: 2}})
        self.assertEqual(r["state"]["merge_attempts"], {})

    def test_not_read_repo_reports_cheap_reasons(self):
        r = run(snap(item("vws-iam", 3, is_pr=True), merge={}))
        self.assertEqual(reasons(r, "vws-iam#3"), ["no_verify_approved"])
        r = run(snap(candidate(), merge={"vws-deploy": {"error": "HTTP 502"}}))
        self.assertEqual(merges(r), [])
        self.assertEqual(reasons(r, "vws-deploy#500")[0], "merge_repo_unreadable")

    def test_merge_off(self):
        r = run(snap(candidate()), cfg=dict(CFG, ORCHESTRATE_MERGE="off"))
        self.assertEqual(merges(r), [])
        self.assertFalse([n for n in r["notes"] if n["kind"].startswith("merge")])


class Serialization(unittest.TestCase):
    def test_one_merge_per_repo_lowest_number_first(self):
        r = run(snap(candidate(number=502), candidate(number=501), candidate("vws-iam", 9),
                     merge={"vws-deploy": ctx(), "vws-iam": ctx()}))
        self.assertEqual(merges(r), ["vws-deploy#501", "vws-iam#9"], "one per repo, repos independent")
        self.assertEqual(reasons(r, "vws-deploy#502"), ["one_merge_per_repo:vws-deploy#501"])

    def test_main_ci_gate(self):
        for state, want in (("pending", "main_ci_pending"), ("none", "main_ci_none"), ("red", "main_ci_red")):
            with self.subTest(state):
                r = run(snap(candidate(), merge={"vws-deploy": ctx(main_ci=state)}))
                self.assertEqual(merges(r), [])
                self.assertEqual(reasons(r, "vws-deploy#500"), [want])

    def test_red_main_posts_one_failure_comment(self):
        r = run(snap(candidate(), merge={"vws-deploy": ctx(main_ci="red")}))
        posts = [a for a in r["actions"] if a["type"] == "merge_status"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["target"], "vws-deploy#415", "an entry in the status issue body")
        lines = posts[0]["body"].split("\n")
        self.assertEqual(lines[0], "<!-- vws-orchestrate:failure -->", "routines match the exact first line")
        self.assertIn("Kind: merge-main-CI-red", lines)
        self.assertIn(f"Idempotency key: `{posts[0]['key']}`", lines)
        again = run(snap(candidate(), merge={"vws-deploy": ctx(main_ci="red")}, keys=[posts[0]["key"]]))
        self.assertEqual([a for a in again["actions"] if a["type"] == "merge_status"], [], "no retry loop: once per sha")


class Locks(unittest.TestCase):
    def waiting(self, **kw):
        lock = {"status": "waiting", "pr": "vws-deploy#499", "head": "9" * 40, "merge_sha": MERGED, "at": NOW - 600,
                "merged_at": NOW - 600}
        lock.update(kw)
        return {"merge_locks": {"vws-deploy": lock}}

    def test_waiting_lock_blocks_until_merge_commit_green(self):
        for ci in ("pending", "none"):
            with self.subTest(ci):
                r = run(snap(candidate(), merge={"vws-deploy": ctx(main_sha=MERGED, main_ci=ci)}), self.waiting())
                self.assertEqual(merges(r), [])
                self.assertTrue(reasons(r, "vws-deploy#500")[0].startswith("repo_locked:push CI"))
                self.assertEqual(r["state"]["merge_locks"]["vws-deploy"]["status"], "waiting")

    def test_stale_branch_read_still_locked(self):
        # branches/main still shows the old head right after the merge: its green CI must not release the lock.
        r = run(snap(candidate(), merge={"vws-deploy": ctx(main_sha=MAIN, main_ci="green", lock_ci="none")}), self.waiting())
        self.assertEqual(merges(r), [])

    def test_green_releases_and_next_merge_goes(self):
        r = run(snap(candidate(), merge={"vws-deploy": ctx(main_sha=MERGED, main_ci="green")}), self.waiting())
        self.assertEqual(merges(r), ["vws-deploy#500"])
        self.assertNotIn("vws-deploy", r["state"]["merge_locks"])
        self.assertIn("merge_lock_released", [n["kind"] for n in r["notes"]])

    def test_newer_green_main_releases(self):
        r = run(snap(candidate(), merge={"vws-deploy": ctx(main_sha=MAIN2, main_ci="green", lock_ci="pending",
                                                           lock_ahead=True)}), self.waiting())
        self.assertEqual(merges(r), ["vws-deploy#500"])
        r = run(snap(candidate(), merge={"vws-deploy": ctx(main_sha=MAIN2, main_ci="green", lock_ci="pending",
                                                           lock_ahead=False)}), self.waiting())
        self.assertEqual(merges(r), [], "a main head that does not contain the merge commit releases nothing")

    def test_red_after_merge_stops_and_posts_once(self):
        s = snap(candidate(), merge={"vws-deploy": ctx(main_sha=MERGED, main_ci="red")})
        r = run(s, self.waiting())
        self.assertEqual(merges(r), [])
        self.assertEqual(r["state"]["merge_locks"]["vws-deploy"]["status"], "red")
        posts = [a for a in r["actions"] if a["type"] == "merge_status"]
        self.assertEqual(len(posts), 1, "lock path and main gate share one key")
        r2 = run(snap(candidate(), merge={"vws-deploy": ctx(main_sha=MERGED, main_ci="red")}, keys=[posts[0]["key"]]), r["state"])
        self.assertEqual([a["type"] for a in r2["actions"]], [], "red stays red, no new comment, no merge")
        self.assertTrue(reasons(r2, "vws-deploy#500")[0].startswith("repo_locked:main CI red"))

    def test_red_lock_survives_green_rerun_of_same_sha_only_newer_head_clears(self):
        red = self.waiting(status="red", red_sha=MERGED)
        r = run(snap(candidate(), merge={"vws-deploy": ctx(main_sha=MERGED, main_ci="green")}), red)
        self.assertEqual(merges(r), [], "a re-run turning the same commit green does not clear red")
        r = run(snap(candidate(), merge={"vws-deploy": ctx(main_sha=MAIN2, main_ci="pending", red_ahead=True)}), red)
        self.assertEqual(merges(r), [])
        r = run(snap(candidate(), merge={"vws-deploy": ctx(main_sha=MAIN, main_ci="green", red_ahead=False)}), red)
        self.assertEqual(merges(r), [], "a stale read of the pre-merge head does not clear red")
        r = run(snap(candidate(), merge={"vws-deploy": ctx(main_sha=MAIN2, main_ci="green", red_ahead=True)}), red)
        self.assertEqual(merges(r), ["vws-deploy#500"], "a newer green main head (a fix landed) resumes merges")
        self.assertNotIn("vws-deploy", r["state"]["merge_locks"])

    def test_wait_timeout_posts_once_and_stays_locked(self):
        old = self.waiting(merged_at=NOW - 3 * 3600)
        r = run(snap(candidate(), merge={"vws-deploy": ctx(main_sha=MERGED, main_ci="pending")}), old)
        posts = [a for a in r["actions"] if a["type"] == "merge_status"]
        self.assertEqual(len(posts), 1)
        self.assertIn("Kind: merge-main-CI-wait-timeout", posts[0]["body"])
        self.assertEqual(merges(r), [])

    def test_merging_lock_resolution(self):
        merging = {"merge_locks": {"vws-deploy": {"status": "merging", "pr": "vws-deploy#499", "head": "9" * 40, "at": NOW - 60}}}
        landed = ctx(main_sha=MAIN, main_ci="green", lock_pr={"state": "closed", "merged": True, "merge_commit_sha": MERGED},
                     lock_ci="none")
        r = run(snap(candidate(), merge={"vws-deploy": landed}), merging)
        self.assertEqual(r["state"]["merge_locks"]["vws-deploy"]["status"], "waiting", "crash after a merge that landed")
        self.assertEqual(r["state"]["merge_locks"]["vws-deploy"]["merge_sha"], MERGED)
        self.assertEqual(merges(r), [])
        not_landed = ctx(lock_pr={"state": "open", "merged": False, "merge_commit_sha": ""})
        r = run(snap(candidate(), merge={"vws-deploy": not_landed}), merging)
        self.assertNotIn("vws-deploy", r["state"]["merge_locks"])
        self.assertEqual(merges(r), ["vws-deploy#500"], "the merge did not land: the repo is free again")
        unknown = ctx(lock_pr={})
        r = run(snap(candidate(), merge={"vws-deploy": unknown}), merging)
        self.assertEqual(merges(r), [])
        self.assertEqual(reasons(r, "vws-deploy#500"), ["repo_locked:merge outcome unknown"])

    def test_purity(self):
        s = snap(candidate(), merge={"vws-deploy": ctx(main_sha=MERGED, main_ci="red")})
        st = self.waiting()
        before = json.dumps([s, st], sort_keys=True)
        a, b = run(s, st), run(s, st)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
        self.assertEqual(json.dumps([s, st], sort_keys=True), before)


class Verdicts(unittest.TestCase):
    def test_main_ci_verdict(self):
        run_ = lambda i, wf, c, st="completed": {"id": i, "workflow_id": wf, "status": st, "conclusion": c}
        cases = [
            ([], "none"),
            ([run_(1, 1, "success"), run_(2, 2, "skipped")], "green"),
            ([run_(1, 1, "success"), run_(2, 2, None, "in_progress")], "pending"),
            ([run_(1, 1, "success"), run_(2, 2, "failure")], "red"),
            ([run_(1, 1, "failure"), run_(3, 1, "success")], "green"),
            ([run_(1, 1, "cancelled")], "red"),
        ]
        for runs, want in cases:
            with self.subTest(runs=runs):
                self.assertEqual(orch.main_ci_verdict(runs), want)


def branch(sha, classic=(), protected=True, enabled=True):
    """GET branches/main as GitHub sends it: classic protection with its required checks ({context, app_id})."""
    checks = [{"context": c, "app_id": a} for c, a in classic]
    rsc = {"enforcement_level": "everyone" if checks else "off", "contexts": [c for c, _ in classic], "checks": checks}
    return {"name": "main", "commit": {"sha": sha}, "protected": protected,
            "protection": {"enabled": enabled, "required_status_checks": rsc}}


def routes(labels=("verify-approved",), head="1" * 40):
    r = rt.routes()
    issue = {"number": 500, "html_url": "https://github.com/popododo0720/vws-deploy/pull/500", "state": "open",
             "created_at": "2026-10-01T00:00:00Z", "labels": [{"name": n} for n in labels],
             "body": "Part of #346\nAgent: vws-platform", "pull_request": {}}
    r[f"{O}/vws-deploy/issues"]["body"].append(issue)
    pull = {"number": 500, "draft": False, "base": {"ref": "main"}, "head": {"sha": head}}
    r[f"{O}/vws-deploy/pulls"]["body"].append(pull)
    r[f"{O}/vws-deploy/pulls/500"] = {"body": dict(pull, state="open", merged=False, mergeable=True, mergeable_state="clean",
                                                   title="ci: change", labels=issue["labels"], body=issue["body"])}
    runs = [{"id": c["id"], "name": c["name"], "app": {"id": c["app_id"]}, "status": c["status"],
             "conclusion": c["conclusion"], "head_sha": c["head_sha"]} for c in green_checks(head)]
    r[f"{O}/vws-deploy/commits/{head}/check-runs"] = {"body": {"total_count": len(runs), "check_runs": runs}}
    r[f"{O}/vws-deploy/commits/{head}/status"] = {"body": {"statuses": []}}
    # vws-verify's PASS on the head, behind verify-approved (vws-deploy#487 keeps the label only with it).
    if "verify-approved" in labels:
        r[f"{O}/vws-deploy/pulls/500/reviews"] = {"body": [
            {"id": 1, "user": {"login": orch.VERIFY_LOGIN, "id": 1}, "state": "APPROVED", "commit_id": head,
             "submitted_at": "2026-10-01T01:00:00Z", "html_url": "https://github.com/popododo0720/vws-deploy/pull/500"}]}
    r[f"{O}/vws-deploy/branches/main"] = {"body": branch(MAIN)}
    r[f"{O}/vws-deploy/actions/runs"] = {"body": {"total_count": 1, "workflow_runs": [
        {"id": 1, "workflow_id": 7, "name": "ci", "status": "completed", "conclusion": "success"}]}}
    r[f"{O}/vws-deploy/rules/branches/main"] = {"body": []}
    r[f"PUT {O}/vws-deploy/pulls/500/merge"] = {"body": {"sha": MERGED, "merged": True, "message": "Pull Request successfully merged"}}
    r[f"{O}/vws-deploy/compare/{MERGED}...{MAIN}"] = {"body": {"status": "behind"}}
    # No push run for the squash commit yet (an exact path with its query wins in the stub).
    r[f"{O}/vws-deploy/actions/runs?event=push&branch=main&head_sha={MERGED}&per_page=100"] = {
        "body": {"total_count": 0, "workflow_runs": []}}
    return r


class ApplyMerges(rt.Env):
    def setUp(self):
        super().setUp()
        self.set_routes(routes())

    def merge_writes(self):
        return [c for c in self.writes() if c["method"] == "PUT"]

    def test_dry_run_plans_but_never_merges(self):
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.writes(), [])
        planned = [(e["action"], e["target"]) for e in self.log() if e["event"] == "planned"]
        self.assertIn(("merge", "vws-deploy#500"), planned)
        checks = [e for e in self.log() if e.get("kind") == "merge_check" and e["target"] == "vws-deploy#500"]
        self.assertEqual(checks[0]["eligible"], True)

    def test_apply_needs_merge_apply_too(self):
        self.assertEqual(self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_SCOPE="vws-deploy#500"), 0)
        self.assertEqual(self.merge_writes(), [], "ORCHESTRATE_MERGE defaults to dry-run")
        self.assertTrue(any(e.get("reason") == "ORCHESTRATE_MERGE=dry-run" for e in self.log()))

    def test_apply_merges_pinned_squash_and_locks(self):
        env = dict(ORCHESTRATE_MODE="apply", ORCHESTRATE_MERGE="apply", ORCHESTRATE_SCOPE="vws-deploy#500")
        self.assertEqual(self.tick(**env), 0)
        w = self.merge_writes()
        self.assertEqual([c["path"] for c in w], [f"{O}/vws-deploy/pulls/500/merge"])
        self.assertEqual(json.loads(w[0]["stdin"]),
                         {"merge_method": "squash", "sha": "1" * 40, "commit_title": "ci: change (#500)"})
        self.assertFalse([c for c in self.calls() if "update-branch" in c["path"]])
        lock = rt.load(self.paths.state_file)["merge_locks"]["vws-deploy"]
        self.assertEqual((lock["status"], lock["merge_sha"]), ("waiting", MERGED))
        merged = [e for e in self.log() if e["event"] == "merged"]
        self.assertEqual((merged[0]["agent"], merged[0]["merge_sha"]), ("vws-platform", MERGED))
        # Next tick: main still at the old head (or its CI not done): locked, no second merge.
        os.remove(self.calls_file)
        self.assertEqual(self.tick(**env), 0)
        self.assertEqual(self.merge_writes(), [])
        self.assertTrue(any(e.get("kind") == "merge_lock" for e in self.log()))
        # Main reaches the squash commit and its push CI turns green: the lock is released.
        r = routes()
        r[f"{O}/vws-deploy/branches/main"] = {"body": {"commit": {"sha": MERGED}}}
        del r[f"{O}/vws-deploy/actions/runs?event=push&branch=main&head_sha={MERGED}&per_page=100"]
        r[f"{O}/vws-deploy/pulls"]["body"] = [p for p in r[f"{O}/vws-deploy/pulls"]["body"] if p["number"] != 500]
        r[f"{O}/vws-deploy/issues"]["body"] = [i for i in r[f"{O}/vws-deploy/issues"]["body"] if i["number"] != 500]
        self.set_routes(r)
        self.assertEqual(self.tick(**env), 0)
        self.assertNotIn("vws-deploy", rt.load(self.paths.state_file)["merge_locks"])
        self.assertTrue(any(e.get("kind") == "merge_lock_released" for e in self.log()))

    def test_failed_merge_keeps_merging_lock_then_resolves(self):
        r = routes()
        r[f"PUT {O}/vws-deploy/pulls/500/merge"] = {"status": 409, "body": {"message": "Head branch was modified"}}
        self.set_routes(r)
        env = dict(ORCHESTRATE_MODE="apply", ORCHESTRATE_MERGE="apply", ORCHESTRATE_SCOPE="vws-deploy#500")
        self.assertEqual(self.tick(**env), orch.FAIL_EXIT)
        st = rt.load(self.paths.state_file)
        self.assertEqual(st["merge_locks"]["vws-deploy"]["status"], "merging", "journaled before the PUT")
        self.assertEqual(st["merge_attempts"], {f"vws-deploy#500@{'1' * 40}": 1})
        # The PR is still open and unmerged: the lock goes, one more attempt is allowed, then no more.
        self.tick(**env)
        self.tick(**env)
        os.remove(self.calls_file)
        self.tick(**env)
        self.assertEqual(self.merge_writes(), [])
        self.assertEqual(len([e for e in self.log() if e.get("event") == "apply_failed"]), 2)

    def test_ruleset_required_checks_are_read(self):
        r = routes()
        r[f"{O}/vws-deploy/rules/branches/main"] = {"body": [
            {"type": "pull_request", "parameters": {"allowed_merge_methods": ["squash"]}},
            {"type": "required_status_checks", "parameters": {"required_status_checks": [
                {"context": "english", "integration_id": 15368}]}}]}
        self.set_routes(r)
        self.tick()
        check_ = next(e for e in self.log() if e.get("kind") == "merge_check" and e["target"] == "vws-deploy#500")
        self.assertIn("required_missing:english", check_["reasons"])

    def test_rulesets_only_no_classic_protection_read(self):
        self.assertEqual(self.tick(), 0)
        self.assertFalse([c for c in self.calls() if "/protection" in c["path"]])
        self.assertIn(("merge", "vws-deploy#500"),
                      [(e["action"], e["target"]) for e in self.log() if e["event"] == "planned"])

    def test_main_or_checks_changed_before_put_are_skipped(self):
        snap_file = os.path.join(self.tmp, "snap.json")
        self.tick("--dump-snapshot", snap_file)
        s = rt.load(snap_file)
        cases = {
            "main moved": lambda r: r.__setitem__(f"{O}/vws-deploy/branches/main", {"body": {"commit": {"sha": MAIN2}}}),
            "main push CI is no longer green": lambda r: r[f"{O}/vws-deploy/actions/runs"]["body"]["workflow_runs"][0]
            .__setitem__("conclusion", "failure"),
            "head checks changed": lambda r: r[f"{O}/vws-deploy/commits/{'1' * 40}/check-runs"]["body"]["check_runs"].append(
                {"id": 99, "name": "lint", "app": {"id": 15368}, "status": "completed", "conclusion": "failure",
                 "head_sha": "1" * 40}),
        }
        for why, mutate in cases.items():
            with self.subTest(why):
                r = routes()
                mutate(r)
                self.set_routes(r)
                got = orch.recheck_merge(orch.Gh(), "vws-deploy", "500", "1" * 40, {}, s["merge"]["vws-deploy"])
                self.assertTrue(got.startswith(why), got)
        self.set_routes(routes())
        self.assertEqual(orch.recheck_merge(orch.Gh(), "vws-deploy", "500", "1" * 40, {}, s["merge"]["vws-deploy"]), "")

    def test_head_moved_before_merge_is_skipped(self):
        r = routes()
        r[f"{O}/vws-deploy/pulls/500"]["body"]["head"] = {"sha": "2" * 40}
        self.set_routes(r)
        self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_MERGE="apply", ORCHESTRATE_SCOPE="vws-deploy#500")
        self.assertEqual(self.merge_writes(), [])

    def test_recheck_refuses_new_opt_out(self):
        r = routes()
        r[f"{O}/vws-deploy/pulls/500"]["body"]["labels"] = [{"name": "verify-approved"}, {"name": "do-not-merge"}]
        gh = orch.Gh()
        self.set_routes(r)
        c = {"main_sha": MAIN, "required": REQUIRED}
        self.assertEqual(orch.recheck_merge(gh, "vws-deploy", "500", "1" * 40, {}, c), "labels or opt-out changed")
        self.assertEqual(orch.recheck_merge(gh, "vws-deploy", "500", "1" * 40, {"merge_locks": {"vws-deploy": {}}}, c),
                         "repo is locked")

    def test_recheck_refuses_new_blocked_label(self):
        r = routes()
        r[f"{O}/vws-deploy/pulls/500"]["body"]["labels"] = [{"name": "verify-approved"}, {"name": "blocked"}]
        self.set_routes(r)
        c = {"main_sha": MAIN, "required": REQUIRED}
        self.assertEqual(orch.recheck_merge(orch.Gh(), "vws-deploy", "500", "1" * 40, {}, c), "labels or opt-out changed")

    def test_red_main_comment_and_write_cap(self):
        r = routes()
        r[f"{O}/vws-deploy/actions/runs"]["body"]["workflow_runs"][0]["conclusion"] = "failure"
        self.set_routes(r)
        self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_MERGE="apply", ORCHESTRATE_MAX_WRITES="5")
        self.assertFalse([c for c in self.writes() if c["path"].endswith("/comments") and "failure" in c["stdin"]],
                         "no failure comment anywhere (vws-deploy#416)")
        events = rt.load(self.paths.state_file)["status_events"]
        self.assertEqual([e["kind"] for e in events], ["merge"])
        self.assertIn(f"(key `{events[0]['key']}`)", self.status_edits()[-1])
        self.assertIn("merge: main CI red in vws-deploy", self.status_edits()[-1])
        self.assertEqual(self.merge_writes(), [])

    def test_scope_skips_status_comment(self):
        r = routes()
        r[f"{O}/vws-deploy/actions/runs"]["body"]["workflow_runs"][0]["conclusion"] = "failure"
        self.set_routes(r)
        self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_MERGE="apply", ORCHESTRATE_SCOPE="vws-deploy#500")
        self.assertEqual(self.writes(), [])

    def test_merge_reads_skipped_when_off(self):
        self.tick(ORCHESTRATE_MERGE="off")
        self.assertFalse([c for c in self.calls() if "branches/main" in c["path"] or "actions/runs" in c["path"]])


class MergeAllowlist(unittest.TestCase):
    def test_only_pinned_squash(self):
        gh = orch.Gh(runner=mock.Mock(side_effect=AssertionError("must not run")))
        path = f"{O}/vws-deploy/pulls/5/merge"
        for payload in ({"merge_method": "merge", "sha": "1" * 40}, {"merge_method": "rebase", "sha": "1" * 40},
                        {"merge_method": "squash"}, {"merge_method": "squash", "sha": "abc"},
                        {"merge_method": "squash", "sha": "1" * 40, "admin": True}, None):
            with self.subTest(payload=payload), self.assertRaises(PermissionError):
                gh.write("PUT", path, payload)
        for method, p in (("PUT", f"{O}/vws-deploy/pulls/5/update-branch"), ("POST", f"{O}/vws-deploy/merges"),
                          ("PUT", f"{O}/other/pulls/5/merge"), ("PATCH", f"{O}/vws-deploy/pulls/5")):
            with self.subTest(path=p), self.assertRaises(PermissionError):
                gh.write(method, p, {"merge_method": "squash", "sha": "1" * 40})


PROTECTION = f"{O}/vws-deploy/branches/main/protection/required_status_checks"
PAT_403 = {"status": 403, "body": {"message": "Resource not accessible by personal access token"}}
NOT_CLEAN = ("blocked", "behind", "dirty", "unknown", "unstable", "has_hooks", "draft", None)


class MergeStateOnly408(unittest.TestCase):
    """vws-deploy#408: GitHub's mergeable_state (GraphQL mergeStateStatus) must be clean; nothing else merges."""

    def test_only_clean_merge_state_merges(self):
        for state in NOT_CLEAN:
            with self.subTest(state=state):
                r = run(snap(candidate(info={"mergeable": True, "mergeable_state": state})))
                self.assertEqual(merges(r), [])
                self.assertIn(f"mergeable_state:{state}", reasons(r, "vws-deploy#500"))
        for mergeable, want in ((None, "mergeable_unknown"), (False, "conflicts")):
            with self.subTest(mergeable=mergeable):
                r = run(snap(candidate(info={"mergeable": mergeable, "mergeable_state": "clean"})))
                self.assertEqual(merges(r), [])
                self.assertIn(want, reasons(r, "vws-deploy#500"))
        self.assertEqual(merges(run(snap(candidate()))), ["vws-deploy#500"])

    def test_protection_path_is_not_allowlisted(self):
        gh = orch.Gh(runner=mock.Mock(side_effect=AssertionError("must not run")))
        with self.assertRaises(PermissionError):
            gh.get(PROTECTION)


class NoProtectionRead408(rt.Env):
    APPLY = dict(ORCHESTRATE_MODE="apply", ORCHESTRATE_MERGE="apply", ORCHESTRATE_SCOPE="vws-deploy#500")

    def setUp(self):
        super().setUp()
        self.set_routes(routes())

    def merge_writes(self):
        return [c for c in self.writes() if c["method"] == "PUT"]

    def fresh(self, r):
        """One isolated tick per case: new routes, no calls, no state from an earlier case."""
        self.set_routes(r)
        if os.path.exists(self.calls_file):
            os.remove(self.calls_file)
        shutil.rmtree(self.env["ORCHESTRATE_STATE_DIR"], ignore_errors=True)

    def unreadable(self):
        return [e for e in self.log() if e.get("kind") == "merge_repo_unreadable"]

    def test_protection_403_does_not_block_a_clean_green_pr(self):
        r = routes()
        r[PROTECTION] = PAT_403
        self.set_routes(r)
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.unreadable(), [])
        self.assertIn(("merge", "vws-deploy#500"),
                      [(e["action"], e["target"]) for e in self.log() if e["event"] == "planned"])
        self.assertFalse([c for c in self.calls() if "/protection" in c["path"]], "branch protection is never read")
        os.remove(self.calls_file)
        self.assertEqual(self.tick(**self.APPLY), 0)
        self.assertEqual([c["path"] for c in self.merge_writes()], [f"{O}/vws-deploy/pulls/500/merge"])

    def test_non_clean_state_never_merges_in_apply(self):
        for state in NOT_CLEAN:
            with self.subTest(state=state):
                r = routes()
                r[f"{O}/vws-deploy/pulls/500"]["body"]["mergeable_state"] = state
                self.fresh(r)
                self.assertEqual(self.tick(**self.APPLY), 0)
                self.assertEqual(self.merge_writes(), [])
                check_ = [e for e in self.log() if e.get("kind") == "merge_check" and e["target"] == "vws-deploy#500"][-1]
                self.assertIn(f"mergeable_state:{state}", check_["reasons"])

    def test_read_errors_fail_closed_with_a_reason(self):
        head = "1" * 40
        cases = {"rulesets 403": f"{O}/vws-deploy/rules/branches/main",
                 "pull 403": f"{O}/vws-deploy/pulls/500",
                 "check runs 403": f"{O}/vws-deploy/commits/{head}/check-runs",
                 "status 404": f"{O}/vws-deploy/commits/{head}/status",
                 "main 500": f"{O}/vws-deploy/branches/main"}
        for why, path in cases.items():
            with self.subTest(why):
                r = routes()
                r[PROTECTION] = PAT_403
                r[path] = {"status": int(why.split()[-1]), "body": {"message": "nope"}}
                self.fresh(r)
                self.assertEqual(self.tick(**self.APPLY), 0, "a read error never fails the tick")
                self.assertEqual(self.merge_writes(), [])
                notes = self.unreadable()
                self.assertTrue(notes and notes[-1]["repo"] == "vws-deploy" and notes[-1]["error"], why)

    def test_recheck_refuses_a_state_that_is_no_longer_clean(self):
        snap_file = os.path.join(self.tmp, "snap.json")
        r = routes()
        r[PROTECTION] = PAT_403
        self.set_routes(r)
        self.tick("--dump-snapshot", snap_file)
        c = rt.load(snap_file)["merge"]["vws-deploy"]
        self.assertNotIn("error", c)
        for state, mergeable in (("blocked", True), ("unstable", True), (None, True), ("clean", None), ("clean", False)):
            with self.subTest(state=state, mergeable=mergeable):
                r2 = routes()
                r2[f"{O}/vws-deploy/pulls/500"]["body"].update(mergeable_state=state, mergeable=mergeable)
                self.set_routes(r2)
                got = orch.recheck_merge(orch.Gh(), "vws-deploy", "500", "1" * 40, {}, c)
                self.assertTrue(got.startswith("merge state changed"), got)
        self.set_routes(routes())
        self.assertEqual(orch.recheck_merge(orch.Gh(), "vws-deploy", "500", "1" * 40, {}, c), "")

    def test_recheck_error_on_the_last_pr_read_fails_closed(self):
        snap_file = os.path.join(self.tmp, "snap.json")
        self.tick("--dump-snapshot", snap_file)
        c = rt.load(snap_file)["merge"]["vws-deploy"]
        real, pulls = orch.Gh(), []

        class Flaky:
            def get(self, path):
                if path.endswith("/pulls/500"):
                    pulls.append(path)
                    if len(pulls) == 2:
                        raise orch.GhError(403, "Resource not accessible by personal access token")
                return real.get(path)

        got = orch.recheck_merge(Flaky(), "vws-deploy", "500", "1" * 40, {}, c)
        self.assertTrue(got.startswith("re-read failed: "), got)


if __name__ == "__main__":
    unittest.main()
