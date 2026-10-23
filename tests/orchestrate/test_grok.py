"""Rule G (vws-deploy#378): wake the Grok dev bot for the top allowlisted ready+grok issue when the
single Grok slot is free. Only issues on ORCHESTRATE_GROK_ALLOWLIST are ever assigned."""
import os
import unittest

import test_runtime
from orch_testlib import CFG, NOW, comment, event, item, orch, snapshot

ALLOW = "vws-iam#1, vws-deploy#2, vws-proto#3, vws-iam#4"
ON = dict(CFG, ORCHESTRATE_GROK_ALLOWLIST=ALLOW)


def grok_task(repo, number, ready_hours_ago=10, extra=(), **kw):
    events = list(kw.pop("events", [])) + [event("labeled", "ready", ready_hours_ago)]
    return item(repo, number, labels=("ready", "grok") + tuple(extra), events=events, **kw)


def wakes(r):
    return [a for a in r["actions"] if a["type"] == "grok_wake"]


def run(s, cfg=ON, state=None):
    return orch.decide(s, state or {}, NOW, cfg, set())


def posted(s, r):
    """Record the wake comments of tick r on the status thread, as the next collect would see them."""
    s["status"]["keys"] = sorted(set(s["status"].get("keys", [])) | {a["key"] for a in wakes(r)})
    return s


class Allowlist(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(CFG["ORCHESTRATE_GROK_ALLOWLIST"], "")
        self.assertEqual(CFG["ORCHESTRATE_GROK_CAP"], "1")

    def test_empty_or_unset_allowlist_assigns_nothing(self):
        s = snapshot(grok_task("vws-iam", 1))
        unset = {k: v for k, v in CFG.items() if k != "ORCHESTRATE_GROK_ALLOWLIST"}
        for cfg in (CFG, dict(CFG, ORCHESTRATE_GROK_ALLOWLIST="  "), unset):
            with self.subTest(allowlist=cfg.get("ORCHESTRATE_GROK_ALLOWLIST")):
                self.assertEqual(wakes(run(s, cfg)), [])

    def test_non_allowlisted_never_assigned(self):
        s = snapshot(grok_task("vws-iam", 9, ready_hours_ago=500, extra=("priority/blocker",)), grok_task("vws-iam", 1))
        w = wakes(run(s))
        self.assertEqual([a["item"] for a in w], ["vws-iam#1"])
        s = snapshot(grok_task("vws-iam", 9))
        self.assertEqual(wakes(run(s)), [])

    def test_allowlisted_issue_is_assigned(self):
        s = snapshot(grok_task("vws-iam", 1))
        w = wakes(run(s))
        self.assertEqual(len(w), 1)
        a = w[0]
        self.assertEqual((a["target"], a["item"]), (orch.status_target(ON), "vws-iam#1"))
        self.assertTrue(a["body"].startswith(orch.GROK_WAKE_MARKER + "\n"))
        self.assertIn("https://github.com/popododo0720/vws-iam/issues/1", a["body"])
        self.assertIn(f"Idempotency key: `{a['key']}`", a["body"])
        self.assertEqual(orch.KEY_RE.search(a["body"]).group(1), a["key"])
        self.assertTrue(a["body"].endswith("\n<!-- orchestrate:grok-wake item=vws-iam#1 -->"))
        self.assertEqual(orch.classify_comment(a["body"]), ("marker", {"marker": "grok-wake"}))
        self.assertEqual(orch.status_wakes([{"body": a["body"], "created_at": "2026-10-21T12:00:00Z"}]), [],
                         "rule A never reads a grok wake as a verify wake")

    def test_malformed_entries_ignored_with_a_note(self):
        cfg = dict(CFG, ORCHESTRATE_GROK_ALLOWLIST="vws-iam#1,#2,vws-iam#x,other/vws-iam#3,iam#4,vws-iam#1 ;rm")
        s = snapshot(grok_task("vws-iam", 1), grok_task("vws-iam", 2), grok_task("vws-iam", 4))
        r = run(s, cfg)
        self.assertEqual([a["item"] for a in wakes(r)], ["vws-iam#1"])
        bad = sorted(n["token"] for n in r["notes"] if n["kind"] == "grok_allowlist_invalid")
        self.assertEqual(bad, sorted(["#2", "vws-iam#x", "other/vws-iam#3", "iam#4", ";rm"]))

    def test_whitespace_and_commas_both_separate(self):
        self.assertEqual(orch.grok_allowlist("vws-iam#1 vws-deploy#2,\nvws-iam#1, vws-iam#0"),
                         ({"vws-iam#1", "vws-deploy#2"}, ["vws-iam#0"]))

    def test_allowlist_key_is_text_config(self):
        env = {"ORCHESTRATE_CONFIG_DIR": "/nonexistent/vws-orchestrate-test"}
        paths = orch.Paths(env)
        cfg = orch.load_config(paths, dict(env, ORCHESTRATE_GROK_ALLOWLIST="vws-iam#1,vws-deploy#2"))
        self.assertEqual(cfg["ORCHESTRATE_GROK_ALLOWLIST"], "vws-iam#1,vws-deploy#2")
        for bad in ("0", "-1", "x", ""):
            with self.subTest(cap=bad), self.assertRaises(ValueError):
                orch.load_config(paths, dict(env, ORCHESTRATE_GROK_CAP=bad))


class Slot(unittest.TestCase):
    def test_slot_free_one_wake(self):
        s = snapshot(grok_task("vws-iam", 1), grok_task("vws-deploy", 2), grok_task("vws-proto", 3))
        self.assertEqual(len(wakes(run(s))), 1, "global cap ORCHESTRATE_GROK_CAP=1")

    def test_slot_busy_no_wake(self):
        s = snapshot(grok_task("vws-iam", 1, ready_hours_ago=20), grok_task("vws-deploy", 2))
        s = posted(s, run(s))
        r = run(s)
        self.assertEqual(wakes(r), [])
        self.assertIn({"kind": "grok_in_flight", "targets": ["vws-iam#1"]}, r["notes"])

    def test_repeated_ticks_no_duplicate(self):
        s = snapshot(grok_task("vws-iam", 1))
        first = wakes(run(s))
        self.assertEqual(len(first), 1)
        s = posted(s, run(s))
        for _ in range(3):
            self.assertEqual(wakes(run(s)), [])

    def test_slot_frees_when_task_closes(self):
        s = snapshot(grok_task("vws-iam", 1, ready_hours_ago=20), grok_task("vws-deploy", 2))
        s = posted(s, run(s))
        s["items"]["vws-iam#1"]["state"] = "closed"
        self.assertEqual([a["item"] for a in wakes(run(s))], ["vws-deploy#2"])

    def test_reset_failures_allows_a_new_wake(self):
        s = snapshot(grok_task("vws-iam", 1))
        s = posted(s, run(s))
        s["items"]["vws-iam#1"]["comments"].append(comment(50, "vws-iam", 1, "/orchestrate reset-failures", 1))
        self.assertEqual(len(wakes(run(s))), 1)

    def test_cap_two_one_per_repo(self):
        cfg = dict(ON, ORCHESTRATE_GROK_CAP="2")
        s = snapshot(grok_task("vws-iam", 1, 30), grok_task("vws-iam", 4, 20), grok_task("vws-deploy", 2, 10))
        self.assertEqual(sorted(a["item"] for a in wakes(run(s, cfg))), ["vws-deploy#2", "vws-iam#1"])

    def test_dispatch_off_pauses_grok_wakes(self):
        s = snapshot(grok_task("vws-iam", 1))
        self.assertEqual(wakes(run(s, dict(ON, ORCHESTRATE_DISPATCH="off"))), [])

    def test_deepseek_slot_is_separate(self):
        s = snapshot(grok_task("vws-iam", 1), item("vws-deploy", 7, labels=("ready", "deepseek"),
                                                    events=[event("labeled", "ready", 5)]))
        types = [a["type"] for a in run(s)["actions"]]
        self.assertIn("dispatch", types)
        self.assertIn("grok_wake", types)


class Eligibility(unittest.TestCase):
    def test_order_blocker_then_security_then_age(self):
        s = snapshot(grok_task("vws-iam", 1, 100), grok_task("vws-deploy", 2, 5, extra=("security",)),
                     grok_task("vws-proto", 3, 1, extra=("priority/blocker",)))
        self.assertEqual(wakes(run(s))[0]["item"], "vws-proto#3")
        del s["items"]["vws-proto#3"]
        self.assertEqual(wakes(run(s))[0]["item"], "vws-deploy#2")
        del s["items"]["vws-deploy#2"]
        self.assertEqual(wakes(run(s))[0]["item"], "vws-iam#1")

    def test_security_in_title_counts(self):
        s = snapshot(grok_task("vws-iam", 1, 100), grok_task("vws-deploy", 2, 5))
        s["items"]["vws-deploy#2"]["title"] = "security: redact tokens in logs"
        self.assertEqual(wakes(run(s))[0]["item"], "vws-deploy#2")

    def test_blocked_never_assigned(self):
        s = snapshot(grok_task("vws-iam", 1, extra=("blocked",)))
        self.assertEqual(wakes(run(s)), [])

    def test_unmet_depends_on_never_assigned(self):
        s = snapshot(grok_task("vws-iam", 1, body="Depends on vws-proto#9"),
                     deps={"vws-proto#9": {"state": "open", "state_reason": None, "merged": False}})
        r = run(s)
        self.assertEqual(wakes(r), [])
        self.assertTrue(any(n["kind"] == "dep_unmet" and n["target"] == "vws-iam#1" for n in r["notes"]))
        s["deps"]["vws-proto#9"] = {"state": "closed", "state_reason": "completed", "merged": False}
        self.assertEqual(len(wakes(run(s))), 1)

    def test_needs_ready_label_and_not_deepseek(self):
        s = snapshot(item("vws-iam", 1, labels=("grok",)))
        self.assertEqual(wakes(run(s)), [])
        s = snapshot(grok_task("vws-iam", 1, extra=("deepseek",)))
        self.assertEqual(wakes(run(s)), [], "an interrupted swap is finished first")

    def test_draft_pr_not_assigned(self):
        s = snapshot(grok_task("vws-iam", 1, is_pr=True, draft=True))
        self.assertEqual(wakes(run(s)), [])

    def test_swap_then_wake_on_next_tick(self):
        fails = [comment(10, "vws-iam", 1, orch.DISPATCH_TEXT, 30),
                 comment(11, "vws-iam", 1, f"[deepseek-dev] Failed for https://github.com/popododo0720/vws-iam/issues/1#issuecomment-10: x", 29),
                 comment(12, "vws-iam", 1, orch.DISPATCH_TEXT, 20),
                 comment(13, "vws-iam", 1, f"[deepseek-dev] Failed for https://github.com/popododo0720/vws-iam/issues/1#issuecomment-12: x", 19)]
        s = snapshot(item("vws-iam", 1, labels=("ready", "deepseek"), comments=fails, events=[event("labeled", "ready", 40)]))
        r = run(s)
        self.assertIn("swap_label", [a["type"] for a in r["actions"]])
        self.assertEqual(wakes(r), [], "the swap tick itself does not wake")
        s["items"]["vws-iam#1"]["labels"] = ["grok", "ready"]
        s["items"]["vws-iam#1"]["events"].append(event("labeled", "grok", 1))
        self.assertEqual([a["item"] for a in wakes(run(s))], ["vws-iam#1"])


class InFlight(unittest.TestCase):
    """Grok work that is already running without a wake key still holds the slot (verify FAIL r1, P1)."""

    def grok_pr(self, repo, number, body, labels=("grok",), draft=False):
        return item(repo, number, labels=labels, is_pr=True, draft=draft, body=body)

    def test_assigned_grok_issue_holds_the_slot(self):
        s = snapshot(grok_task("vws-iam", 1, 30), grok_task("vws-deploy", 2, 10), item("vws-proto", 9, labels=("grok",)))
        s["items"]["vws-proto#9"]["assignees"] = ["grok-dev"]
        r = run(s)
        self.assertEqual(wakes(r), [], "a hand-started, assigned grok issue is in flight")
        self.assertIn({"kind": "grok_in_flight", "targets": ["vws-proto#9"]}, r["notes"])

    def test_assigned_candidate_is_not_woken(self):
        s = snapshot(grok_task("vws-iam", 1, 30), grok_task("vws-deploy", 2, 10))
        s["items"]["vws-iam#1"]["assignees"] = ["grok-dev"]
        self.assertEqual(wakes(run(s)), [])
        self.assertEqual([a["item"] for a in wakes(run(s, dict(ON, ORCHESTRATE_GROK_CAP="2")))], ["vws-deploy#2"])

    def test_unassigned_issue_without_grok_does_not_hold(self):
        s = snapshot(grok_task("vws-iam", 1), item("vws-proto", 9, labels=("ready", "deepseek")))
        s["items"]["vws-proto#9"]["assignees"] = ["someone"]
        self.assertEqual([a["item"] for a in wakes(run(s))], ["vws-iam#1"], "only grok work counts")

    def test_open_grok_pr_linked_to_the_issue_holds_it(self):
        for body in ("Part of #1", "Part of vws-iam#1"):
            for draft in (False, True):
                with self.subTest(body=body, draft=draft):
                    repo = "vws-iam" if body == "Part of #1" else "vws-proto"
                    s = snapshot(grok_task("vws-iam", 1, 30), grok_task("vws-deploy", 2, 10),
                                 self.grok_pr(repo, 20, f"Fix it.\n\n{body}\n\nAgent: grok-dev", draft=draft))
                    r = run(s)
                    self.assertEqual(wakes(r), [])
                    self.assertIn({"kind": "grok_in_flight", "targets": ["vws-iam#1"]}, r["notes"])

    def test_linked_pr_without_grok_label_does_not_hold(self):
        s = snapshot(grok_task("vws-iam", 1), self.grok_pr("vws-iam", 20, "Part of #1", labels=("deepseek",)))
        self.assertEqual([a["item"] for a in wakes(run(s))], ["vws-iam#1"])

    def test_woken_issue_and_its_pr_count_once(self):
        cfg = dict(ON, ORCHESTRATE_GROK_CAP="2")
        s = snapshot(grok_task("vws-iam", 1, 30))
        s = posted(s, run(s, cfg))
        s["items"]["vws-iam#20"] = self.grok_pr("vws-iam", 20, "Part of #1")
        s["items"]["vws-deploy#2"] = grok_task("vws-deploy", 2, 10)
        r = run(s, cfg)
        self.assertEqual([a["item"] for a in wakes(r)], ["vws-deploy#2"], "one task, one slot")
        self.assertIn({"kind": "grok_in_flight", "targets": ["vws-iam#1"]}, r["notes"])

    def test_cross_repo_pr_keeps_its_own_repo_busy(self):
        """verify r2 (P2): a grok PR in vws-proto working on vws-iam#1 occupies vws-proto too."""
        cfg = dict(ON, ORCHESTRATE_GROK_CAP="2")
        s = snapshot(grok_task("vws-proto", 3), self.grok_pr("vws-proto", 20, "Part of vws-iam#1"))
        r = run(s, cfg)
        self.assertEqual(wakes(r), [])
        self.assertIn({"kind": "repo_busy", "target": "vws-proto#3", "repo": "vws-proto"}, r["notes"])

    def test_held_repo_is_busy(self):
        cfg = dict(ON, ORCHESTRATE_GROK_CAP="2")
        s = snapshot(grok_task("vws-iam", 1, 30), grok_task("vws-iam", 4, 20), self.grok_pr("vws-iam", 20, "Part of #1"))
        r = run(s, cfg)
        self.assertEqual(wakes(r), [])
        self.assertIn({"kind": "repo_busy", "target": "vws-iam#4", "repo": "vws-iam"}, r["notes"])


class OptOut(unittest.TestCase):
    def test_opt_out_labels_never_assigned(self):
        for label in orch.OPT_OUT_LABELS:
            with self.subTest(label=label):
                self.assertIn(label, ("hold", "do-not-merge", "blocked"))
                s = snapshot(grok_task("vws-iam", 1, extra=(label,)))
                self.assertEqual(wakes(run(s)), [])
        self.assertEqual({"hold", "do-not-merge"} - set(orch.OPT_OUT_LABELS), set())


def gh_item(number, labels=("grok",), body="", pr=False, assignees=()):
    """An entry of GET issues?state=open&labels=grok as GitHub returns it."""
    d = {"number": number, "state": "open", "body": body, "labels": [{"name": n} for n in labels],
         "assignees": [{"login": a} for a in assignees]}
    if pr:
        d["pull_request"] = {}
    return d


CAND = gh_item(1, ("ready", "grok"))  # the candidate vws-iam#1 itself, still unassigned


class FakeGh:
    def __init__(self, labels=("ready", "grok"), state="open", comments=(), assignees=(), open_items=(),
                 fail_list=False):
        self.issue = {"state": state, "labels": [{"name": n} for n in labels],
                      "assignees": [{"login": a} for a in assignees]}
        # open_items: vws-iam's open grok items as a list, or {repo: [items]} for several repos.
        self.open_items = dict(open_items) if isinstance(open_items, dict) else {"vws-iam": list(open_items)}
        self.comments, self.writes, self.lists, self.fail_list = list(comments), [], [], fail_list

    def get(self, path):
        return self.issue, None

    def get_all(self, path, limit=20):
        if "/issues?" not in path:
            return self.comments
        self.lists.append(path)
        if self.fail_list:
            raise orch.GhError(502, "bad gateway")
        return self.open_items.get(path.split("/")[2], [])

    def write(self, method, path, payload=None):
        self.writes.append((method, path, payload))
        return {"id": 1}


class Apply(unittest.TestCase):
    def plan(self, s=None, cfg=ON):
        s = s or snapshot(grok_task("vws-iam", 1))
        return run(s, cfg), s

    def apply(self, gh, cfg=ON, s=None):
        r, s = self.plan(s, cfg)
        events = []
        orch.apply(r, s, gh, dict(cfg, ORCHESTRATE_MERGE="dry-run"), None,
                   lambda ev, **kw: events.append(dict(kw, event=ev)), {}, NOW)
        return r, events

    def test_posts_on_status_thread(self):
        gh = FakeGh()
        r, events = self.apply(gh)
        self.assertEqual(gh.writes, [("POST", "repos/popododo0720/vws-deploy/issues/339/comments",
                                      {"body": wakes(r)[0]["body"]})])
        self.assertIn("applied", [e["event"] for e in events])

    def test_recheck_skips_changed_issue(self):
        for gh in (FakeGh(labels=("ready", "grok", "blocked")), FakeGh(state="closed"), FakeGh(labels=("grok",))):
            self.apply(gh)
            self.assertEqual(gh.writes, [])

    def test_recheck_skips_assigned_held_or_opted_out(self):
        pr = {"number": 20, "pull_request": {}, "body": "Part of #1", "labels": [{"name": "grok"}]}
        for gh in (FakeGh(assignees=("grok-dev",)), FakeGh(open_items=[pr]),
                   FakeGh(labels=("ready", "grok", "hold")), FakeGh(labels=("ready", "grok", "do-not-merge"))):
            self.apply(gh)
            self.assertEqual(gh.writes, [])

    def grok_skips(self, events):
        return [e for e in events if e["event"] == "skipped" and e.get("action") == "grok_wake"]

    def test_recheck_counts_other_grok_work_toward_the_cap(self):
        """verify r2 (P1): Grok work that started between collect and apply, for any issue, fills the
        single slot, so the planned wake is not posted."""
        others = {"grok PR for another issue": gh_item(20, body="Part of #2", pr=True),
                  "assigned grok issue": gh_item(5, assignees=("grok-dev",)),
                  "assigned grok PR without a link": gh_item(21, pr=True, assignees=("grok-dev",)),
                  "grok PR for an issue in another repo": gh_item(22, pr=True, body="Part of vws-proto#3")}
        for name, other in others.items():
            with self.subTest(name):
                gh = FakeGh(open_items=[CAND, other])
                _, events = self.apply(gh)
                self.assertEqual(gh.writes, [])
                self.assertEqual(len(self.grok_skips(events)), 1)
                self.assertIn("Grok cap 1 reached", self.grok_skips(events)[0]["reason"])

    def test_recheck_ignores_idle_and_non_grok_items(self):
        idle = [CAND, gh_item(21, ("deepseek",), body="Part of #1", pr=True),
                gh_item(22, body="Part of #1"), gh_item(23, ("ready", "grok"))]
        gh = FakeGh(open_items=idle)
        self.apply(gh)
        self.assertEqual(len(gh.writes), 1)

    def test_recheck_reads_every_scoped_repo(self):
        gh = FakeGh(open_items=[CAND])
        self.apply(gh)
        self.assertEqual(len(gh.writes), 1)
        self.assertEqual(sorted(p.split("/")[2] for p in gh.lists), ["vws-deploy", "vws-iam", "vws-proto"],
                         "every non-archived scoped repo, once")
        self.assertTrue(all(p.endswith("/issues?state=open&labels=grok&per_page=100") for p in gh.lists))

    def test_recheck_cross_repo_work_blocks_the_wake(self):
        """Grok work held in another repo blocks the wake for vws-iam#1 at cap 1."""
        for other in ({"vws-proto": [gh_item(9, assignees=("grok-dev",))]},
                      {"vws-deploy": [gh_item(30, pr=True, body="Part of vws-proto#3")]},
                      {"vws-proto": [gh_item(31, pr=True, body="Part of vws-deploy#2")]}):
            with self.subTest(other=other):
                gh = FakeGh(open_items=dict(other, **{"vws-iam": [CAND]}))
                _, events = self.apply(gh)
                self.assertEqual(gh.writes, [])
                self.assertIn("Grok cap 1 reached", self.grok_skips(events)[0]["reason"])

    def test_recheck_cap_two_keeps_one_per_repo(self):
        cfg = dict(ON, ORCHESTRATE_GROK_CAP="2")
        gh = FakeGh(open_items={"vws-iam": [CAND], "vws-proto": [gh_item(9, assignees=("grok-dev",))]})
        self.apply(gh, cfg)
        self.assertEqual(len(gh.writes), 1, "a free slot and a free repo")
        gh = FakeGh(open_items={"vws-iam": [CAND, gh_item(20, pr=True, body="Part of vws-proto#3")]})
        _, events = self.apply(gh, cfg)
        self.assertEqual(gh.writes, [], "a grok PR in vws-iam keeps vws-iam busy whatever issue it works on")
        self.assertIn("vws-iam", self.grok_skips(events)[0]["reason"])

    def test_recheck_counts_wakes_posted_this_tick(self):
        cfg = dict(ON, ORCHESTRATE_GROK_CAP="2")
        s = snapshot(grok_task("vws-iam", 1, 30), grok_task("vws-deploy", 2, 10))
        self.assertEqual([a["item"] for a in wakes(run(s, cfg))], ["vws-iam#1", "vws-deploy#2"])
        gh = FakeGh(open_items={"vws-iam": [CAND], "vws-deploy": [gh_item(2, ("ready", "grok"))],
                                "vws-proto": [gh_item(9, assignees=("grok-dev",))]})
        _, events = self.apply(gh, cfg, s)
        self.assertEqual(len(gh.writes), 1, "vws-proto#9 started since collect: one slot left")
        self.assertIn("vws-iam/issues/1", gh.writes[0][2]["body"])
        self.assertIn("Grok cap 2 reached", self.grok_skips(events)[0]["reason"])

    def test_recheck_counts_woken_tasks_still_open(self):
        cfg = dict(ON, ORCHESTRATE_GROK_CAP="2")
        s = snapshot(grok_task("vws-proto", 3, 30))
        s = posted(s, run(s, cfg))
        s["items"]["vws-iam#1"] = grok_task("vws-iam", 1, 10)
        self.assertEqual([a["item"] for a in wakes(run(s, cfg))], ["vws-iam#1"])
        fresh = {"vws-iam": [CAND], "vws-deploy": [gh_item(7, assignees=("grok-dev",))]}
        gh = FakeGh(open_items=dict(fresh, **{"vws-proto": [gh_item(3, ("ready", "grok"))]}))
        self.apply(gh, cfg, s)
        self.assertEqual(gh.writes, [], "woken vws-proto#3 and the new vws-deploy#7 fill both slots")
        gh = FakeGh(open_items=fresh)
        self.apply(gh, cfg, s)
        self.assertEqual(len(gh.writes), 1, "vws-proto#3 closed since collect: its slot is free")

    def test_recheck_read_error_fails_closed(self):
        gh = FakeGh(fail_list=True)
        _, events = self.apply(gh)
        self.assertEqual(gh.writes, [])
        self.assertIn("re-read failed", self.grok_skips(events)[0]["reason"])

    def test_recheck_skips_posted_key(self):
        r, _ = self.plan()
        gh = FakeGh(comments=[{"body": wakes(r)[0]["body"]}])
        self.apply(gh)
        self.assertEqual(gh.writes, [])

    def test_scope_skips(self):
        gh = FakeGh()
        self.apply(gh, dict(ON, ORCHESTRATE_SCOPE="vws-iam#1"))
        self.assertEqual(gh.writes, [])



class CollectTick(test_runtime.Env):
    """collect() keeps the assignees, so an assigned grok issue holds the slot in a real tick."""
    O = test_runtime.O

    def setup_issue(self, assignees):
        r = test_runtime.routes()
        labels = [{"name": "ready"}, {"name": "grok"}]
        r[f"{self.O}/vws-iam/issues"] = {"body": [{"number": 1, "html_url": "https://github.com/popododo0720/vws-iam/issues/1",
                                                   "state": "open", "created_at": "2026-10-01T00:00:00Z", "body": "",
                                                   "labels": labels, "assignees": [{"login": a} for a in assignees]}]}
        r[f"{self.O}/vws-iam/issues/1"] = {"body": {"state": "open", "labels": labels}}
        self.set_routes(r)

    def planned(self):
        return [e["target"] for e in self.log() if e["event"] == "planned" and e["action"] == "grok_wake"]

    def test_assigned_issue_is_not_woken(self):
        self.setup_issue(["grok-dev"])
        self.assertEqual(self.tick(ORCHESTRATE_GROK_ALLOWLIST="vws-iam#1"), 0)
        self.assertEqual(self.planned(), [])
        os.remove(self.paths.log)
        self.setup_issue([])
        self.assertEqual(self.tick(ORCHESTRATE_GROK_ALLOWLIST="vws-iam#1"), 0)
        self.assertEqual(self.planned(), ["vws-deploy#339"])


if __name__ == "__main__":
    unittest.main()
