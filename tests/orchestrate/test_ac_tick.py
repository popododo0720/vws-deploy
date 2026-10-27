"""vws-deploy#486 AC3-AC7: a merged PR's `AC: <n>` lines tick boxes of its `Part of` issue, with an evidence
suffix ac-lint.sh accepts, one marker comment per PR, ac-partial while boxes stay open, and a close when the
last box is ticked."""
import json
import os
import subprocess
import tempfile
import unittest

import test_runtime
from orch_testlib import CFG, NOW, ROOT, orch, ts

O = "repos/popododo0720"
SHA = "a" * 40
URL = "https://github.com/popododo0720/vws-deploy/pull/500"
ISSUE = ("Context.\n\n## Acceptance criteria\n\n- [ ] AC1: one\n- [ ] AC2: two\n  more of two\n- [ ] AC3: three\n\n"
         "## Notes\n\n- [ ] not a criterion\n")
ON = dict(CFG, ORCHESTRATE_AC_TICK="apply")
HEAD = "b" * 40
VERIFIED = "2026-01-02T00:00:00Z"


def verdict(at, login=orch.VERIFY_LOGIN, sha=HEAD, state="APPROVED"):
    return {"id": 9, "html_url": URL, "user": {"login": login, "id": 1}, "submitted_at": at, "commit_id": sha,
            "state": state, "body": "vws-verify: PASS"}


def merged(n=500, repo="vws-deploy", body="Part of #7\n\nAC: 1\nAC: 3\n", issue="vws-deploy#7", issue_body=ISSUE,
           issue_state="open", labels=(), marker=False, hours_ago=1, verified_at=None, body_edited_at=""):
    return {"pr": f"{repo}#{n}", "url": f"https://github.com/popododo0720/{repo}/pull/{n}", "merged_at": ts(hours_ago),
            "merge_sha": SHA, "body": body, "issue": issue, "issue_state": issue_state, "issue_body": issue_body,
            "issue_labels": list(labels), "marker_seen": marker,
            "verified_at": ts(hours_ago + 1) if verified_at is None else verified_at, "body_edited_at": body_edited_at}


def snap(*m, labels=("ac-partial", "ac-pending")):
    return {"run_id": "r", "repos": [{"name": "vws-deploy", "archived": False}, {"name": "vws-iam", "archived": False}],
            "items": {}, "labels": {"vws-deploy": list(labels), "vws-iam": list(labels)}, "ac_merged": list(m)}


def plan(s, cfg=ON, state=None):
    notes = []
    new_state = json.loads(json.dumps({"ac_tick_since": ts(48)} if state is None else state))
    acts = orch.decide_ac_ticks(s, new_state, NOW, cfg, notes)
    return acts, notes, new_state


def ac_lint(body):
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(body)
    try:
        return subprocess.run(["bash", os.path.join(ROOT, ".github", "ac", "ac-lint.sh"), "--file", f.name],
                              capture_output=True, text=True)
    finally:
        os.unlink(f.name)


class Body(unittest.TestCase):
    def test_boxes_only_under_the_heading(self):
        self.assertEqual([(b["n"], b["checked"]) for b in orch.ac_boxes(ISSUE)], [(1, False), (2, False), (3, False)])
        self.assertEqual(orch.ac_boxes("no section\n- [ ] x\n"), [])

    def test_ticks_the_listed_boxes_with_evidence_ac_lint_accepts(self):
        p = orch.ac_tick_body(ISSUE, [1, 3], URL, SHA)
        self.assertEqual(p["error"], "")
        self.assertEqual(p["ticked"], [1, 3])
        self.assertEqual([b["n"] for b in p["open"]], [2])
        self.assertIn(f"- [x] AC1: one (evidence: {URL} merged {SHA})\n", p["body"])
        self.assertIn("- [ ] AC2: two\n", p["body"])
        self.assertIn(f"- [x] AC3: three (evidence: {URL} merged {SHA})\n", p["body"])
        self.assertIn("## Notes\n\n- [ ] not a criterion\n", p["body"])
        r = ac_lint(p["body"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(orch.ac_tick_only(ISSUE, p["body"]))

    def test_an_already_ticked_box_is_left_alone(self):
        once = orch.ac_tick_body(ISSUE, [1], URL, SHA)["body"]
        again = orch.ac_tick_body(once, [1], URL, SHA)
        self.assertEqual((again["body"], again["ticked"]), (once, []))

    def test_errors(self):
        self.assertEqual(orch.ac_tick_body(ISSUE, [4], URL, SHA)["error"], "ac_index_invalid")
        self.assertEqual(orch.ac_tick_body("no criteria\n", [1], URL, SHA)["error"], "ac_no_section")

    def test_tick_only_refuses_any_other_edit(self):
        good = orch.ac_tick_body(ISSUE, [2], URL, SHA)["body"]
        for bad in (good.replace("Context.", "Changed."), good + "x", ISSUE.replace("- [ ] AC1", "- [x] AC1"),
                    ISSUE.replace("## Notes\n\n- [ ] not", "## Notes\n\n- [x] not") , ""):
            with self.subTest(bad=bad[:30]):
                self.assertFalse(orch.ac_tick_only(ISSUE, bad))


class Rule(unittest.TestCase):
    def test_ac3_partial(self):
        acts, notes, _ = plan(snap(merged()))
        self.assertEqual(len(acts), 1)
        a = acts[0]
        self.assertEqual((a["type"], a["target"], a["pr"], a["boxes"], a["close"]),
                         ("ac_tick", "vws-deploy#7", "vws-deploy#500", [1, 3], False))
        self.assertEqual(a["labels_add"], ["ac-partial"])

    def test_ac4_last_box_closes(self):
        body = orch.ac_tick_body(ISSUE, [1, 3], URL, SHA)["body"]
        acts, _, _ = plan(snap(merged(body="Part of #7\nAC: 2\n", issue_body=body, labels=("ac-partial", "ac-pending"))))
        self.assertTrue(acts[0]["close"])
        self.assertEqual(acts[0]["labels_add"], [])
        self.assertEqual(acts[0]["labels_remove"], ["ac-partial", "ac-pending"])

    def test_ac5_marker_and_invalid_index(self):
        acts, notes, st = plan(snap(merged(marker=True)))
        self.assertEqual(acts, [])
        self.assertIn("vws-deploy#500", st["ac_ticked"])
        acts, notes, _ = plan(snap(merged(body="Part of #7\nAC: 9\n")))
        self.assertEqual(acts, [])
        self.assertIn("ac_index_invalid", [n["kind"] for n in notes])

    def test_ac6_other_repo(self):
        acts, _, _ = plan(snap(merged(n=12, repo="vws-iam", body="Part of vws-deploy#7\nAC: 2\n")))
        self.assertEqual((acts[0]["target"], acts[0]["pr"]), ("vws-deploy#7", "vws-iam#12"))

    def test_skips_and_notes(self):
        cases = {"ac_none": merged(body="Part of #7\nAC: none - docs only\n"),
                 "ac_unparsed": merged(body="Part of #7\n"),
                 "ac_issue_closed": merged(issue_state="closed"),
                 "ac_no_section": merged(issue_body="no criteria\n"),
                 "ac_read_error": dict(merged(), error="HTTP 502")}
        for kind, m in cases.items():
            with self.subTest(kind):
                acts, notes, _ = plan(snap(m))
                self.assertEqual(acts, [])
                self.assertIn(kind, [n["kind"] for n in notes])

    def test_malicious_body_edit_after_verify_ticks_and_closes_nothing(self):
        """Review 5640903019: a PR body edited after the trusted vws-verify verdict for the merged head (here
        after the merge too) retargets `Part of` at another watched issue and lists every box. Nothing ticks or
        closes; the edited body is never trusted."""
        last = orch.ac_tick_body(ISSUE, [1, 3], URL, SHA)["body"]
        for edited in (ts(0.5), ts(2)):  # after the merge; after the verdict, before the merge
            with self.subTest(edited=edited):
                evil = merged(body="Part of #8\nAC: 1\nAC: 2\nAC: 3\n", issue="vws-deploy#8", issue_body=last,
                              verified_at=ts(3), body_edited_at=edited)
                acts, notes, st = plan(snap(evil))
                self.assertEqual(acts, [])
                self.assertIn("ac_body_edited", [n["kind"] for n in notes])
                self.assertIn("vws-deploy#500", st["ac_ticked"], "handled once, not re-noted every tick")

    def test_body_edit_at_the_verdict_second_counts_as_after(self):
        acts, notes, _ = plan(snap(merged(verified_at=ts(2), body_edited_at=ts(2))))
        self.assertEqual(acts, [])
        self.assertIn("ac_body_edited", [n["kind"] for n in notes])

    def test_body_edited_before_the_verdict_still_ticks(self):
        acts, _, _ = plan(snap(merged(verified_at=ts(2), body_edited_at=ts(3))))
        self.assertEqual([a["target"] for a in acts], ["vws-deploy#7"])

    def test_no_trusted_verdict_for_the_merged_head_ticks_nothing(self):
        acts, notes, st = plan(snap(merged(verified_at="")))
        self.assertEqual(acts, [])
        self.assertIn("ac_unverified", [n["kind"] for n in notes])
        self.assertIn("vws-deploy#500", st["ac_ticked"])

    def test_already_ticked_or_before_start_is_skipped(self):
        acts, _, _ = plan(snap(merged()), state={"ac_tick_since": ts(48), "ac_ticked": {"vws-deploy#500": ts(1)}})
        self.assertEqual(acts, [])
        acts, _, _ = plan(snap(merged(hours_ago=50)))
        self.assertEqual(acts, [])

    def test_first_tick_sets_the_start_and_backfills_nothing(self):
        acts, _, st = plan(snap(merged()), state={})
        self.assertEqual((acts, st["ac_tick_since"]), ([], orch.iso(NOW)))

    def test_off_plans_nothing(self):
        acts, _, st = plan(snap(merged()), cfg=dict(CFG, ORCHESTRATE_AC_TICK="off"))
        self.assertEqual(acts, [])

    def test_oldest_merge_first(self):
        acts, _, _ = plan(snap(merged(n=501, hours_ago=1), merged(n=500, hours_ago=3)))
        self.assertEqual([a["pr"] for a in acts], ["vws-deploy#500", "vws-deploy#501"])

    def test_two_merges_ticking_one_issue_both_survive_decide(self):
        """vws-deploy#567: decide() dedupes merged actions by action_key; two merged PRs ticking different
        boxes of one issue in the same tick are two writes, so both ac_tick actions must survive."""
        s = snap(merged(n=500, body="Part of #7\nAC: 1\n", hours_ago=3),
                 merged(n=501, body="Part of #7\nAC: 2\n", hours_ago=1))
        r = orch.decide(s, {"ac_tick_since": ts(48)}, NOW, ON, set())
        ticks = [(a["target"], a["pr"], a["boxes"]) for a in r["actions"] if a["type"] == "ac_tick"]
        self.assertEqual(ticks, [("vws-deploy#7", "vws-deploy#500", [1]), ("vws-deploy#7", "vws-deploy#501", [2])])


class FakeGh:
    def __init__(self, body=ISSUE, comments=(), state="open", labels=()):
        self.issue = {"state": state, "body": body, "labels": [{"name": x} for x in labels]}
        self.comments, self.writes = list(comments), []

    def get(self, path):
        return self.issue, None

    def get_all(self, path, limit=20):
        return self.comments

    def write(self, method, path, payload=None, before=None):
        self.writes.append((method, path, payload))
        if method == "PATCH" and "body" in (payload or {}):
            assert orch.ac_tick_only(before, payload["body"])
        return {"id": 1}


class Apply(unittest.TestCase):
    def apply(self, gh, cfg=ON, m=None, state=None):
        s = snap(m or merged())
        acts, _, st = plan(s, cfg)
        events = []
        new_state, errors = orch.apply({"actions": acts, "state": st}, s, gh, cfg, None,
                                       lambda ev, **kw: events.append(dict(kw, event=ev)), {}, NOW)
        return new_state, events, errors

    def test_ac3_writes(self):
        gh = FakeGh()
        st, _, errors = self.apply(gh)
        base = f"{O}/vws-deploy/issues/7"
        self.assertEqual(errors, 0)
        self.assertEqual([(w[0], w[1]) for w in gh.writes],
                         [("PATCH", base), ("POST", f"{base}/labels"), ("POST", f"{base}/comments")])
        self.assertEqual(gh.writes[1][2], {"labels": ["ac-partial"]})
        comment = gh.writes[2][2]["body"]
        self.assertIn(URL, comment)
        self.assertIn(SHA, comment)
        self.assertIn("- 2: AC2: two", comment)
        self.assertIn("<!-- vws-orchestrate:ac-tick pr=vws-deploy#500 -->", comment)
        self.assertIn("vws-deploy#500", st["ac_ticked"])

    def test_ac4_close_and_label_removal(self):
        body = orch.ac_tick_body(ISSUE, [1, 3], URL, SHA)["body"]
        gh = FakeGh(body=body, labels=("ac-partial", "ac-pending"))
        self.apply(gh, m=merged(body="Part of #7\nAC: 2\n", issue_body=body, labels=("ac-partial", "ac-pending")))
        base = f"{O}/vws-deploy/issues/7"
        self.assertEqual([(w[0], w[1]) for w in gh.writes],
                         [("PATCH", base), ("DELETE", f"{base}/labels/ac-partial"), ("DELETE", f"{base}/labels/ac-pending"),
                          ("PATCH", base), ("POST", f"{base}/comments")])
        self.assertEqual(gh.writes[3][2], {"state": "closed", "state_reason": "completed"})

    def test_ac5_marker_rechecked_at_apply(self):
        gh = FakeGh(comments=[{"user": {"login": "popododo0720"}, "body": "x\n<!-- vws-orchestrate:ac-tick pr=vws-deploy#500 -->"}])
        st, events, _ = self.apply(gh)
        self.assertEqual(gh.writes, [])
        self.assertIn("vws-deploy#500", st["ac_ticked"])

    def test_issue_edited_since_collect_is_reread(self):
        edited = ISSUE.replace("Context.", "Context, edited.")
        gh = FakeGh(body=edited)
        self.apply(gh)
        self.assertTrue(gh.writes[0][2]["body"].startswith("Context, edited."))

    def test_ac7_cap_leaves_the_rest_for_the_next_tick(self):
        gh = FakeGh()
        st, events, _ = self.apply(gh, cfg=dict(ON, ORCHESTRATE_MAX_WRITES="2"))
        self.assertEqual(gh.writes, [])
        self.assertNotIn("vws-deploy#500", st.get("ac_ticked", {}))
        self.assertIn("write cap 2 reached", [e.get("reason") for e in events])

    def test_ac7_scope(self):
        gh = FakeGh()
        self.apply(gh, cfg=dict(ON, ORCHESTRATE_SCOPE="vws-deploy#8"))
        self.assertEqual(gh.writes, [])
        gh = FakeGh()
        self.apply(gh, cfg=dict(ON, ORCHESTRATE_SCOPE="vws-deploy#7"))
        self.assertEqual(len(gh.writes), 3)

    def test_dry_run_switch_writes_nothing(self):
        gh = FakeGh()
        _, events, _ = self.apply(gh, cfg=dict(CFG, ORCHESTRATE_AC_TICK="dry-run"))
        self.assertEqual(gh.writes, [])
        self.assertIn("ORCHESTRATE_AC_TICK=dry-run", [e.get("reason") for e in events])


class GhGuard(unittest.TestCase):
    def test_issue_edits(self):
        gh = orch.Gh(runner=lambda *a, **k: subprocess.CompletedProcess(a, 0, "HTTP/2.0 200 OK\n\n{}", ""),
                     status_issue="415")
        path = f"{O}/vws-iam/issues/7"
        good = orch.ac_tick_body(ISSUE, [1], URL, SHA)["body"]
        gh.write("PATCH", path, {"body": good}, before=ISSUE)
        gh.write("PATCH", path, {"state": "closed", "state_reason": "completed"})
        gh.write("DELETE", f"{path}/labels/ac-partial")
        gh.write("DELETE", f"{path}/labels/ac-pending")
        for payload, before in (({"body": good}, None), ({"body": "rewritten"}, ISSUE), ({"state": "open"}, None),
                                ({"title": "x"}, None), ({"body": good, "state": "closed"}, ISSUE)):
            with self.subTest(payload=str(payload)[:40]), self.assertRaises(PermissionError):
                gh.write("PATCH", path, payload, before=before)
        with self.assertRaises(PermissionError):
            gh.write("DELETE", f"{path}/labels/ready")

    def test_edit_time_query_is_a_read_on_the_box_login(self):
        seen = []

        def runner(args, **k):
            seen.append((args, k["input"], "GH_TOKEN" in k["env"]))
            return subprocess.CompletedProcess(args, 0, 'HTTP/2.0 200 OK\n\n{"data": {}}', "")
        gh = orch.Gh(runner=runner)  # no board token set
        self.assertEqual(gh.graphql(orch.AC_EDIT_QUERY, {"o": "popododo0720", "r": "vws-deploy", "n": 5}), {})
        self.assertEqual(seen[0][0][:4], ["gh", "api", "-i", "graphql"])
        self.assertFalse(seen[0][2], "no board token: the box login reads it")
        self.assertEqual(json.loads(seen[0][1])["query"], orch.AC_EDIT_QUERY)
        for q in ("query{ viewer{ login } }", orch.AC_EDIT_QUERY.replace("lastEditedAt", "body")):
            with self.subTest(q=q[:30]), self.assertRaises(PermissionError):
                gh.graphql(q, {})
        with self.assertRaises(PermissionError):
            gh.graphql(orch.AC_EDIT_QUERY, {}, write=True)


class CollectTick(test_runtime.Env):
    def routes(self, issue_comments=(), reviews=None, edited_at=None, head_ref=None):
        r = test_runtime.routes()
        r[f"{O}/vws-deploy/pulls/500/reviews"] = {"body": [verdict(VERIFIED)] if reviews is None else reviews}
        r["graphql"] = {"body": {"data": {"repository": {"pullRequest": {
            "lastEditedAt": edited_at, "headRefOid": HEAD if head_ref is None else head_ref}}}}}
        r[f"{O}/vws-deploy/pulls?state=closed&sort=updated&direction=desc&per_page=100"] = {"body": [
            {"number": 500, "html_url": URL, "merged_at": test_runtime.NOWISO, "merge_commit_sha": SHA,
             "base": {"ref": "main"}, "head": {"sha": HEAD}, "body": "Part of #7\n\nAC: 1\nAC: 3\n"},
            {"number": 499, "html_url": URL[:-3] + "499", "merged_at": None, "merge_commit_sha": SHA,
             "base": {"ref": "main"}, "body": "Part of #7\n\nAC: 2\n"}]}
        r[f"{O}/vws-deploy/issues/7"] = {"body": {"state": "open", "body": ISSUE, "labels": []}}
        r[f"{O}/vws-deploy/issues/7/comments"] = {"body": list(issue_comments)}
        labels = r[f"{O}/vws-deploy/labels"]["body"] + [{"name": "ac-partial"}]
        r[f"{O}/vws-deploy/labels"] = {"body": labels}
        self.set_routes(r)

    def ac_writes(self):
        return [c for c in self.writes() if "/issues/7" in c["path"]]

    def test_first_tick_starts_then_a_merge_is_ticked(self):
        os.makedirs(self.paths.state, exist_ok=True)
        with open(self.paths.state_file, "w") as f:
            json.dump({"ac_tick_since": "2026-01-01T00:00:00Z"}, f)
        self.routes()
        env = {"ORCHESTRATE_MODE": "apply", "ORCHESTRATE_AC_TICK": "apply", "ORCHESTRATE_MAX_WRITES": "20"}
        self.assertEqual(self.tick(**env), 0)
        self.assertEqual([(c["method"], c["path"]) for c in self.ac_writes()],
                         [("PATCH", f"{O}/vws-deploy/issues/7"), ("POST", f"{O}/vws-deploy/issues/7/labels"),
                          ("POST", f"{O}/vws-deploy/issues/7/comments")])
        before = len(self.calls())
        self.assertEqual(self.tick(**env), 0)
        self.assertEqual(len(self.ac_writes()), 3, "the second tick writes nothing for the same PR")
        self.assertFalse([c for c in self.calls()[before:] if c["path"].startswith(f"{O}/vws-deploy/issues/7")],
                         "a ticked PR is not read again")

    def ac_tick_state(self):
        os.makedirs(self.paths.state, exist_ok=True)
        with open(self.paths.state_file, "w") as f:
            json.dump({"ac_tick_since": "2026-01-01T00:00:00Z"}, f)

    def notes(self):
        try:
            return [e.get("kind") for e in self.log() if e["event"] == "note"]
        except FileNotFoundError:
            return []  # no tick yet

    def test_malicious_body_edit_after_verify_ticks_and_closes_nothing(self):
        """End to end, apply mode: the body was edited a minute after the trusted verdict for the merged head
        (the issue would close on its last box). No write and no read touch issue 7."""
        self.ac_tick_state()
        self.routes(edited_at="2026-01-02T00:01:00Z")
        r = json.load(open(self.routes_file))
        r[f"{O}/vws-deploy/issues/7"] = {"body": {"state": "open", "labels": [],
                                                  "body": orch.ac_tick_body(ISSUE, [2], URL, SHA)["body"]}}
        self.set_routes(r)
        env = {"ORCHESTRATE_MODE": "apply", "ORCHESTRATE_AC_TICK": "apply", "ORCHESTRATE_MAX_WRITES": "20"}
        self.assertEqual(self.tick(**env), 0)
        self.assertEqual(self.ac_writes(), [])
        self.assertFalse([c for c in self.calls() if c["path"].startswith(f"{O}/vws-deploy/issues/7")])
        self.assertIn("ac_body_edited", self.notes())
        self.assertEqual(self.tick(**env), 0)
        self.assertEqual(self.ac_writes(), [], "a later tick does not retry it")

    def test_untrusted_or_other_head_verdicts_tick_nothing(self):
        cases = {"other login": [verdict(VERIFIED, login="mallory")],
                 "other head": [verdict(VERIFIED, sha="c" * 40)],
                 "fail verdict": [verdict(VERIFIED, state="CHANGES_REQUESTED")],
                 "none": []}
        for name, reviews in cases.items():
            with self.subTest(name):
                self.ac_tick_state()
                self.routes(reviews=reviews)
                before, logged = len(self.writes()), len(self.notes())
                self.assertEqual(self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_AC_TICK="apply"), 0)
                self.assertEqual([c for c in self.writes()[before:] if "/issues/7" in c["path"]], [])
                self.assertIn("ac_unverified", self.notes()[logged:])

    def test_graphql_head_mismatch_is_a_read_error(self):
        self.ac_tick_state()
        self.routes(head_ref="d" * 40)
        self.assertEqual(self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_AC_TICK="apply"), 0)
        self.assertEqual(self.ac_writes(), [])
        self.assertIn("ac_read_error", self.notes())

    def test_off_reads_no_closed_pulls(self):
        self.routes()
        self.assertEqual(self.tick(ORCHESTRATE_AC_TICK="off"), 0)
        self.assertFalse([c for c in self.calls() if "state=closed" in c["path"]])

    def test_issue_read_error_does_not_fail_the_tick(self):
        self.routes()
        r = json.load(open(self.routes_file))
        r[f"{O}/vws-deploy/issues/7"] = {"status": 502, "body": {"message": "bad"}}
        r[f"{O}/vws-deploy/issues/7/comments"] = {"status": 502, "body": {"message": "bad"}}
        self.set_routes(r)
        os.makedirs(self.paths.state, exist_ok=True)
        with open(self.paths.state_file, "w") as f:
            json.dump({"ac_tick_since": "2026-01-01T00:00:00Z"}, f)
        self.assertEqual(self.tick(ORCHESTRATE_AC_TICK="dry-run"), 0)
        self.assertIn("ac_read_error", [e.get("kind") for e in self.log() if e["event"] == "note"])


if __name__ == "__main__":
    unittest.main()
