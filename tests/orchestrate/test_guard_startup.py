"""vws-deploy#446: one repo-guard re-trigger when a verify-approved head's newest run is startup_failure,
and a #412 marker instead of a second touch. The stuck and after fixtures are a recorded actions/runs
page for vws-storage#133 head d208bce9; the second file adds one derived run (the live retry succeeded).
Run: python3 -m unittest tests.orchestrate.test_guard_startup
"""
import hashlib
import json
import os
import shutil
import tempfile
import unittest

from orch_testlib import CFG, item, orch, snapshot

HERE = os.path.dirname(os.path.abspath(__file__))
FX = os.path.join(HERE, "fixtures", "startup")
HEAD = "d208bce9dc0968b755c64ae2cac265193ea5d8e5"
RUN = 39917192133  # the recorded startup_failure; its run_attempt is 2 and its first attempt succeeded
NOW = orch.utc("2026-10-23T07:30:00Z")
KEY = f"vws-storage#133@{HEAD}"


def load(name):
    with open(os.path.join(FX, name), encoding="utf-8") as f:
        return json.load(f)


def runs(name):
    return orch.guard_runs_from(load(name))


def pr(guard, **kw):
    it = {"repo": "vws-storage", "number": 133, "is_pr": True, "state": "open", "draft": False,
          "base": "main", "labels": ["verify-approved"], "body": "Part of #446\n", "head_sha": HEAD,
          "comments": [], "merge_info": {"head_sha": HEAD, "head_ref": "ci/ci-fail-caller", "guard_runs": guard}}
    it.update(kw)
    return it


def plan(it, state=None, now=NOW):
    notes, st = [], {"guard_retry": dict(state or {})}
    actions = orch.decide_guard_startup({"vws-storage#133": it}, st, now, orch.DEFAULTS, notes)
    return actions, notes, st


def marker(author, sha=HEAD):
    body = orch.ci_fail_module().format_comment(
        {"v": 1, "repo": "vws-storage", "pr": 133, "head_sha": sha, "head_branch": "ci/ci-fail-caller",
         "owner": "grok", "agent": "vws-storage", "owner_source": "label:grok", "chain": [133], "n": 1,
         "cap": 3, "wake": True, "runs": [{"workflow": "repo-guard", "run_id": RUN, "run_attempt": 2,
                                           "conclusion": "startup_failure", "url": "https://example.test/run"}],
         "failed_jobs": [], "created_at": "2026-10-23T07:22:00Z", "updated_at": "2026-10-23T07:22:00Z"}, {})
    return {"id": 9, "url": "https://example.test/c", "created_at": "2026-10-23T07:22:00Z",
            "author": author, "body": body}


class RecordedRuns(unittest.TestCase):
    def test_one_startup_failure_retriggers_once(self):
        """The recorded page: attempt 2 is startup_failure and nothing newer succeeded. One retry, no marker."""
        got = runs("storage-133-stuck.json")
        self.assertEqual(got[-1]["id"], RUN)
        self.assertEqual(got[-1]["conclusion"], "startup_failure")
        self.assertEqual(got[-1]["run_attempt"], 2)  # attempt 1 succeeded; this is the first observation
        actions, notes, _ = plan(pr(got))
        self.assertEqual([a["type"] for a in actions], ["guard_retry"])
        self.assertEqual(actions[0]["run"], RUN)
        self.assertEqual(notes, [])

    def test_a_recorded_attempt_waits_then_reports(self):
        it = pr(runs("storage-133-stuck.json"))
        recent = {"run": RUN, "at": orch.iso(NOW - 60), "reported": False}
        actions, notes, _ = plan(it, {KEY: recent})
        self.assertEqual(actions, [])
        self.assertEqual(notes[0]["kind"], "guard_startup_wait")
        old = dict(recent, at=orch.iso(NOW - orch.GUARD_STARTUP_WAIT))
        actions, notes, _ = plan(it, {KEY: old})
        self.assertEqual([a["type"] for a in actions], ["guard_report"])
        self.assertEqual(notes, [])

    def test_a_second_startup_failure_is_a_marker_not_another_touch(self):
        """storage-133-second.json is the recorded slice plus derived run 39917192134. Not a live run."""
        actions, _, _ = plan(pr(runs("storage-133-second.json")))
        self.assertEqual([a["type"] for a in actions], ["guard_report"])
        self.assertNotIn("guard_retry", [a["type"] for a in actions])
        rec = orch.ci_fail_module().parse_comment(actions[0]["body"])
        self.assertEqual(rec["runs"][0]["conclusion"], "startup_failure")
        self.assertEqual(rec["runs"][0]["run_id"], 39917192134)
        self.assertEqual(rec["failed_jobs"], [])
        self.assertIn("none reported", actions[0]["body"])
        self.assertEqual(orch.ci_fail_record(actions[0]["body"])["head_sha"], HEAD)
        # Already reported, or a trusted marker for this sha: do not post again.
        self.assertEqual(plan(pr(runs("storage-133-second.json")), {KEY: {"run": RUN, "at": orch.iso(NOW), "reported": True}})[0], [])
        self.assertEqual(plan(pr(runs("storage-133-second.json"), comments=[marker("popododo0720")]))[0], [])
        self.assertEqual(plan(pr(runs("storage-133-second.json"), comments=[marker("github-actions[bot]")]))[0], [])

    def test_a_stranger_marker_does_not_count(self):
        actions, _, _ = plan(pr(runs("storage-133-stuck.json"), comments=[marker("mallory")]))
        self.assertEqual([a["type"] for a in actions], ["guard_retry"])

    def test_a_later_success_does_nothing(self):
        """The recorded page after the 08:55 body edit: the newest repo-guard run succeeded."""
        actions, notes, _ = plan(pr(runs("storage-133-after.json")),
                                 {KEY: {"run": RUN, "at": orch.iso(NOW), "reported": False}})
        self.assertEqual(actions, [])
        self.assertEqual(notes, [])

    def test_skipped_and_fail_closed(self):
        got = runs("storage-133-stuck.json")
        for it in (pr(got, labels=["ready"]),
                   pr(got, labels=["verify-approved", "blocked"]),
                   pr(got, labels=["verify-approved", "hold"]),
                   pr(got, draft=True),
                   pr(got, base="other"),
                   pr(got, body="Part of #446\n" + orch.OPT_OUT_MARKER + "\n"),
                   pr(got, state="closed")):
            self.assertEqual(plan(it)[0], [], it)
        waiting = [dict(r) for r in got]
        waiting[-1]["status"] = "in_progress"
        actions, notes, _ = plan(pr(waiting))
        self.assertEqual(actions, [])
        self.assertEqual(notes[0]["kind"], "guard_startup_wait")
        failed = [dict(r) for r in got]
        failed[-1]["conclusion"] = "failure"  # a real failure belongs to ci-fail, which sees workflow_run
        self.assertEqual(plan(pr(failed))[0], [])
        page = load("storage-133-stuck.json")
        page["total_count"] = page["total_count"] + 1
        actions, notes, _ = plan(pr(orch.guard_runs_from(page)))
        self.assertEqual(actions, [])
        self.assertEqual(notes[0]["kind"], "guard_startup_truncated")
        self.assertIsNone(orch.guard_runs_from({"total_count": "5", "workflow_runs": page["workflow_runs"]}))
        bare = pr(got)
        del bare["merge_info"]["guard_runs"]
        self.assertEqual(plan(bare)[0], [])  # not collected, including ORCHESTRATE_MERGE=off
        # A conformance-shaped run (no name, no id) is not a repo-guard run and not a truncated page.
        self.assertEqual(orch.guard_runs_from({"workflow_runs": [{"event": "pull_request", "conclusion": "success"}]}), [])

    def test_stop_does_not_drop_the_retry(self):
        it = item("vws-deploy", 133, labels=("verify-approved",), is_pr=True, body="Part of #446\n", head_sha=HEAD)
        it["merge_info"] = {"head_sha": HEAD, "head_ref": "ci/ci-fail-caller", "guard_runs": runs("storage-133-stuck.json"),
                            "state": "open", "merged": False, "draft": False, "base": "main", "title": "t",
                            "checks": [], "mergeable": False}
        s = snapshot(it)
        cfg = dict(CFG, ORCHESTRATE_MERGE="off", ORCHESTRATE_WAKES="off", ORCHESTRATE_VERIFY_LABELS="off")
        actions = orch.decide(s, {}, NOW, cfg, set())["actions"]
        self.assertIn("guard_retry", [a["type"] for a in actions])
        self.assertNotIn("guard_report", [a["type"] for a in actions])

    def test_records_are_kept_for_the_open_head_and_the_comment_window(self):
        fresh = orch.iso(NOW - 3600)
        stale = orch.iso(NOW - 20 * 86400)
        other = "vws-iam#144@" + "ab" * 20
        state = {"guard_retry": {KEY: {"run": RUN, "at": stale, "reported": False, "body_sha": "abc"},
                                 other: {"run": 1, "at": fresh},
                                 "vws-iam#9@" + "cd" * 20: {"run": 2, "at": stale}}}
        _, _, st = plan(pr(runs("storage-133-after.json")), state["guard_retry"])
        self.assertEqual(set(st["guard_retry"]), {KEY, other})
        self.assertEqual(st["guard_retry"][KEY]["body_sha"], "abc")


class Apply(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.paths = orch.Paths({"ORCHESTRATE_STATE_DIR": self.tmp, "ORCHESTRATE_CONFIG_DIR": self.tmp})
        self.events = []

    def log(self, *args, **kw):
        self.events.append(kw)

    def gh(self, body="Part of #446\n", sha=HEAD, comments=None, fail=False):
        return _Gh(body, sha, comments or [], fail)

    def apply(self, actions, gh, state=None):
        st = state if state is not None else {}
        result = {"actions": actions, "state": st}
        snap = {"repos": [], "labels": {"vws-storage": ["verify-approved"]}}
        cfg = dict(orch.DEFAULTS, ORCHESTRATE_MERGE="apply", ORCHESTRATE_SCOPE="")
        return orch.apply(result, snap, gh, cfg, self.paths, self.log, {}, NOW)

    def test_patch_once_and_a_rerun_does_not_patch_again(self):
        actions, _, st = plan(pr(runs("storage-133-stuck.json")))
        gh = self.gh()
        new_state, errors = self.apply(actions, gh, st)
        self.assertEqual(errors, 0)
        self.assertEqual(len(gh.writes), 1)
        self.assertEqual(gh.writes[0][0], "PATCH")
        self.assertIn(orch.tr.guard_retry_line(HEAD, RUN), gh.pr["body"])
        digest = hashlib.sha256(b"Part of #446\n").hexdigest()
        self.assertEqual(new_state["guard_retry"][KEY]["body_sha"], digest)
        self.assertEqual(new_state["guard_retry"][KEY]["run"], RUN)
        again, errors = self.apply(actions, gh, new_state)
        self.assertEqual(errors, 0)
        self.assertEqual(len(gh.writes), 1)  # the line is already there
        self.assertEqual(again["guard_retry"][KEY]["body_sha"], digest)

    def test_a_failed_write_still_records_the_attempt(self):
        actions, _, st = plan(pr(runs("storage-133-stuck.json")))
        gh = self.gh(fail=True)
        new_state, errors = self.apply(actions, gh, st)
        self.assertEqual(errors, 1)
        self.assertEqual(new_state["guard_retry"][KEY]["run"], RUN)
        with open(self.paths.state_file, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["guard_retry"][KEY]["run"], RUN)
        # The next decision waits. It does not plan a second touch.
        self.assertEqual(plan(pr(runs("storage-133-stuck.json")), new_state["guard_retry"])[0], [])

    def test_a_moved_head_writes_nothing(self):
        actions, _, st = plan(pr(runs("storage-133-stuck.json")))
        gh = self.gh(sha="a" * 40)
        new_state, errors = self.apply(actions, gh, st)
        self.assertEqual((errors, gh.writes), (0, []))
        self.assertNotIn(KEY, new_state.get("guard_retry", {}))

    def test_report_posts_once_even_when_the_post_fails(self):
        actions, _, st = plan(pr(runs("storage-133-second.json"), labels=["verify-approved", "grok"]))
        gh = self.gh()
        new_state, errors = self.apply(actions, gh, st)
        self.assertEqual(errors, 0)
        self.assertEqual(gh.writes[0][0], "POST")
        self.assertTrue(new_state["guard_retry"][KEY]["reported"])
        self.assertIn("owner=grok", gh.writes[0][2]["body"])
        posted = len(gh.writes)
        again, errors = self.apply(actions, gh, new_state)
        self.assertEqual(errors, 0)
        self.assertEqual(len(gh.writes), posted)
        lost = self.gh(fail=True)
        state2, errors = self.apply(actions, lost, {})
        self.assertEqual(errors, 1)
        self.assertTrue(state2["guard_retry"][KEY]["reported"])
        self.assertEqual(plan(pr(runs("storage-133-second.json")), state2["guard_retry"])[0], [])

    def test_a_marker_already_on_the_pr_is_not_posted_again(self):
        actions, _, st = plan(pr(runs("storage-133-second.json")))
        comment = {"id": 3, "html_url": "https://example.test/c", "created_at": "2026-10-23T07:40:00Z",
                   "user": {"login": "popododo0720"}, "body": actions[0]["body"]}
        gh = self.gh(comments=[comment])
        new_state, errors = self.apply(actions, gh, st)
        self.assertEqual((errors, gh.writes), (0, []))
        self.assertTrue(new_state["guard_retry"][KEY]["reported"])


class BodyGate(unittest.TestCase):
    def test_only_the_retry_line_may_be_appended(self):
        old = "Part of #446\nAC: 1\n"
        new = orch.tr.guard_retry_append(old, HEAD, RUN)
        self.assertTrue(orch.tr.guard_body_edit(old, new))
        self.assertEqual(orch.tr.guard_retry_append(new, HEAD, RUN), new)
        changed = new.replace("Part of #446", "Part of #1")
        self.assertFalse(orch.tr.guard_body_edit(old, changed))
        ran = []

        def runner(*args, **kw):
            ran.append(args)
            class Done:
                stdout = "HTTP/1.1 200 OK\n\n{}"
                returncode = 0
            return Done()

        gh = orch.Gh(runner=runner, allow=lambda method, path: True)
        with self.assertRaises(PermissionError):
            gh.write("PATCH", "repos/popododo0720/vws-storage/issues/133", {"body": changed}, before=old)
        self.assertEqual(ran, [])
        gh.write("PATCH", "repos/popododo0720/vws-storage/issues/133", {"body": new}, before=old)
        self.assertEqual(len(ran), 1)
        # Both trailing-newline shapes hash to a pre-image the restore accepts.
        for before in (old, old[:-1]):
            body = orch.tr.guard_retry_append(before, HEAD, RUN)
            m = {"pr": "vws-storage#133", "head_sha": HEAD, "body": body, "verified_at": "2026-10-23T07:00:00Z",
                 "body_edited_at": "2026-10-23T07:21:40Z"}
            state = {"guard_retry": {KEY: {"body_sha": hashlib.sha256(before.encode()).hexdigest()}}}
            self.assertTrue(orch.guard_retry_restores(m, state))
            self.assertEqual(m["body"], before)
            self.assertIsNotNone(orch.tr.verified_body(dict(m, body_edited_at=""), orch.utc))
        attacked = dict(m, body=changed)
        self.assertFalse(orch.guard_retry_restores(attacked, state))
        self.assertIn("Part of #1", attacked["body"])


class _Gh:
    def __init__(self, body, sha, comments, fail):
        self.pr = {"state": "open", "merged": False, "body": body, "head": {"sha": sha, "ref": "ci/ci-fail-caller"}}
        self.comments = comments
        self.fail = fail
        self.writes = []

    def get(self, path):
        return self.pr, False

    def get_all(self, path):
        return list(self.comments)

    def write(self, method, path, payload=None, before=None):
        if self.fail:
            raise orch.GhError(500, "lost")
        self.writes.append((method, path, payload, before))
        if method == "PATCH":
            self.pr["body"] = payload["body"]
        else:
            self.comments.append({"id": 50, "html_url": "https://example.test/c", "created_at": "2026-10-23T07:40:01Z",
                                  "user": {"login": "popododo0720"}, "body": (payload or {}).get("body") or ""})
