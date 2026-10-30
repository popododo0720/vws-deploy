"""rules/hold_release.py: serial-merge hold auto-release (vws-deploy#501 box 8).

A PR labeled `hold` whose body has one `Held for: #N` (or `Held for: vws-<repo>#N`) line is a serial-merge
hold. The rule removes `hold` once #N is no longer an open PR (merged, closed, missing, or an issue). A
`hold` without the line is a human hold and is never touched.
Run: python3 -m unittest discover -s tests/orchestrate"""
import json
import unittest

from orch_testlib import CFG, NOW, item, orch, snapshot
from rules.rules_testlib import BY_NAME, RuleContract, rule_cfg
from test_runtime import O, Env, pr_issue, routes


def held(number, ref, repo="vws-deploy", labels=("hold",), assoc="OWNER", body=None, author=None, **kw):
    it = item(repo, number, labels=labels, is_pr=True,
              body=body if body is not None else f"Part of #501\n\nHeld for: {ref}\n", **kw)
    # The owner login is trusted by itself; any other login needs an OWNER/MEMBER/COLLABORATOR association.
    it["author"], it["author_assoc"] = author or ("popododo0720" if assoc == "OWNER" else "someone"), assoc
    return it


def run(*items, cfg=None):
    d = BY_NAME["hold_release"].decide(snapshot(*items), {}, rule_cfg(cfg or CFG, set()), NOW)
    return d["actions"], d["notes"]


def kinds(notes):
    return [(n["kind"], n["target"], n.get("reason")) for n in notes]


class HoldRelease(RuleContract, unittest.TestCase):
    RULE = "hold_release"
    # (cases with output, action types, note kinds, state keys, actions, notes) over decide-cases.jsonl:
    # no recorded case has a `Held for:` line, so the new rule adds nothing to the pre-split output.
    PROFILE = (0, [], [], [], 0, 0)

    def test_target_open_pr_keeps_the_hold(self):
        acts, notes = run(held(10, "#11"), item("vws-deploy", 11, is_pr=True))
        self.assertEqual((acts, notes), ([], []))

    def test_target_merged_or_closed_releases(self):
        # A merged or closed PR is not in the open-items snapshot of an active repo.
        it = held(10, "#11")
        acts, notes = run(it)
        self.assertEqual(acts, [{"type": "remove_label", "target": "vws-deploy#10", "label": "hold",
                                 "head": it["head_sha"], "held_for": "vws-deploy#11",
                                 "reason": "hold released: vws-deploy#11 is not an open PR (merged, closed or missing)"}])
        self.assertEqual(kinds(notes), [("hold_release", "vws-deploy#10", "target_not_open")])
        self.assertEqual(notes[0]["held_for"], "vws-deploy#11")

    def test_target_is_an_issue_releases(self):
        acts, notes = run(held(10, "#12"), item("vws-deploy", 12))
        self.assertEqual([(a["type"], a["label"], a["target"]) for a in acts], [("remove_label", "hold", "vws-deploy#10")])
        self.assertEqual(kinds(notes), [("hold_release", "vws-deploy#10", "target_not_a_pr")])

    def test_cross_repo_reference(self):
        self.assertEqual(run(held(10, "vws-iam#3"), item("vws-iam", 3, is_pr=True)), ([], []))
        acts, notes = run(held(10, "vws-iam#3"))
        self.assertEqual(acts[0]["held_for"], "vws-iam#3")
        self.assertEqual(kinds(notes), [("hold_release", "vws-deploy#10", "target_not_open")])

    def test_human_hold_is_never_touched(self):
        self.assertEqual(run(held(10, "", body="Part of #501\n")), ([], []))
        self.assertEqual(run(held(10, "", body="Part of #501\nheld for review by the lead\n")), ([], []))

    def test_line_without_hold_label_does_nothing(self):
        self.assertEqual(run(held(10, "#11", labels=("verify-approved",))), ([], []))

    def test_issue_with_hold_is_not_a_serial_merge_hold(self):
        it = item("vws-deploy", 10, labels=("hold",), body="Held for: #11\n")
        it["author"], it["author_assoc"] = "popododo0720", "OWNER"
        self.assertEqual(run(it), ([], []))

    def test_untrusted_author_keeps_the_hold(self):
        # Anyone can open a PR on the public repos and edit its body; only a body that only trusted
        # accounts can edit may release a hold (vws-deploy#439 class 1).
        for assoc in ("NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", ""):
            acts, notes = run(held(10, "#11", assoc=assoc))
            self.assertEqual(acts, [], assoc)
            self.assertEqual(kinds(notes), [("hold_ref_untrusted", "vws-deploy#10", None)], assoc)
        it = held(10, "#11", assoc="NONE")
        del it["author_assoc"]
        self.assertEqual(run(it)[0], [], "a missing association fails closed")
        self.assertEqual(len(run(held(10, "#11", assoc="COLLABORATOR"))[0]), 1)

    def test_bad_lines_keep_the_hold(self):
        for body in ("Held for: popododo0720/vws-iam#3\n", "Held for: #11, #12\n", "Held for: #11\nHeld for: #12\n",
                     "Held for: soon\n", "Held for: #10\n", "Held for: other-repo#3\n"):
            acts, notes = run(held(10, "", body=body))
            self.assertEqual(acts, [], body)
            self.assertEqual([n["kind"] for n in notes], ["hold_ref_invalid"], body)

    def test_unknown_or_archived_repo_keeps_the_hold(self):
        # vws-hypervisor is archived in the fixture and vws-nope is not collected: open state is unknown.
        for ref in ("vws-hypervisor#3", "vws-nope#3"):
            acts, notes = run(held(10, ref))
            self.assertEqual(acts, [], ref)
            self.assertEqual(kinds(notes), [("hold_ref_unresolved", "vws-deploy#10", None)], ref)

    def test_merges_off_releases_nothing(self):
        cfg = dict(CFG, ORCHESTRATE_MERGE="off")
        self.assertEqual(run(held(10, "#11"), cfg=cfg), ([], []))

    def test_through_dispatcher(self):
        # One registry line runs it: orch.decide plans the removal, and no other rule plans one for `hold`.
        snap = snapshot(held(10, "#11"))
        res = orch.decide(snap, {}, NOW, CFG, set())
        self.assertEqual([(a["type"], a["target"], a["label"]) for a in res["actions"] if a.get("label") == "hold"],
                         [("remove_label", "vws-deploy#10", "hold")])

    def test_delete_hold_is_allowlisted(self):
        self.assertTrue(orch.allowed("DELETE", f"{O}/vws-deploy/issues/10/labels/hold"))
        self.assertFalse(orch.allowed("DELETE", f"{O}/vws-deploy/issues/10/labels/do-not-merge"))


class Tick(Env):
    """collect, decide and apply against the stub gh."""

    BODY = "Part of #501\n\nHeld for: #11\n"

    def hold_routes(self, fresh_body=None, fresh_labels=("hold",), fresh_assoc="OWNER", assoc="OWNER"):
        login = {"OWNER": "popododo0720"}
        r = routes()
        sha = f"{10:040x}"
        r[f"{O}/vws-deploy/labels"]["body"].append({"name": "hold"})
        r[f"{O}/vws-deploy/pulls"]["body"].append({"number": 10, "draft": False, "base": {"ref": "main"}, "head": {"sha": sha}})
        issue = dict(pr_issue("vws-deploy", 10, ("hold",)), body=self.BODY, user={"login": login.get(assoc, "someone")},
                     author_association=assoc)
        r[f"{O}/vws-deploy/issues"]["body"].append(issue)
        r[f"{O}/vws-deploy/pulls/10"] = {"body": {
            "number": 10, "state": "open", "head": {"sha": sha}, "user": {"login": login.get(fresh_assoc, "someone")},
            "author_association": fresh_assoc, "body": self.BODY if fresh_body is None else fresh_body,
            "labels": [{"name": x} for x in fresh_labels]}}
        self.set_routes(r)

    def deletes(self):
        return [c["path"] for c in self.writes() if c["method"] == "DELETE" and c["path"].endswith("/labels/hold")]

    def test_release_is_applied_and_logged(self):
        self.hold_routes()
        self.assertEqual(self.tick(ORCHESTRATE_MODE="apply"), 0)
        self.assertEqual(self.deletes(), [f"{O}/vws-deploy/issues/10/labels/hold"])
        rel = [e for e in self.log() if e["event"] == "hold_released"]
        self.assertEqual([(e["target"], e["held_for"]) for e in rel], [("vws-deploy#10", "vws-deploy#11")])

    def test_dry_run_writes_nothing(self):
        self.hold_routes()
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.deletes(), [])

    def test_recheck_skips_when_the_line_or_label_changed(self):
        for kw in ({"fresh_body": "Part of #501\n"}, {"fresh_body": "Held for: #12\n"}, {"fresh_labels": ()},
                   {"fresh_assoc": "NONE"}):
            with self.subTest(**kw):
                open(self.calls_file, "w").close()
                self.hold_routes(**kw)
                self.assertEqual(self.tick(ORCHESTRATE_MODE="apply"), 0)
                self.assertEqual(self.deletes(), [])
                skips = [e for e in self.log() if e["event"] == "skipped" and e.get("target") == "vws-deploy#10"
                         and e.get("label") == "hold"]
                self.assertTrue(skips and skips[-1]["reason"].startswith("PR changed since collect: hold"), skips)

    def test_untrusted_author_writes_nothing(self):
        self.hold_routes(assoc="NONE")
        self.assertEqual(self.tick(ORCHESTRATE_MODE="apply"), 0)
        self.assertEqual(self.deletes(), [])


if __name__ == "__main__":
    unittest.main()
