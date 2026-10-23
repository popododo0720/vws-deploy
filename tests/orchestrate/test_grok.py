"""Rule G (vws-deploy#378): wake the Grok dev bot for the top allowlisted ready+grok issue when the
single Grok slot is free. Only issues on ORCHESTRATE_GROK_ALLOWLIST are ever assigned."""
import unittest

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

    def test_malformed_entries_ignored_with_a_note(self):
        cfg = dict(CFG, ORCHESTRATE_GROK_ALLOWLIST="vws-iam#1,#2,vws-iam#x,other/vws-iam#3,iam#4,vws-iam#1 ;rm")
        s = snapshot(grok_task("vws-iam", 1), grok_task("vws-iam", 2), grok_task("vws-iam", 4))
        r = run(s, cfg)
        self.assertEqual([a["item"] for a in wakes(r)], ["vws-iam#1"])
        bad = sorted(n["token"] for n in r["notes"] if n["kind"] == "grok_allowlist_invalid")
        self.assertEqual(bad, sorted(["#2", "vws-iam#x", "other/vws-iam#3", "iam#4", "vws-iam#1 ;rm"]))

    def test_whitespace_and_commas_both_separate(self):
        self.assertEqual(orch.grok_allowlist("vws-iam#1 vws-deploy#2,\nvws-iam#1")[0], {"vws-iam#1", "vws-deploy#2"})

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


class FakeGh:
    def __init__(self, labels=("ready", "grok"), state="open", comments=()):
        self.issue = {"state": state, "labels": [{"name": n} for n in labels]}
        self.comments, self.writes = list(comments), []

    def get(self, path):
        return self.issue, None

    def get_all(self, path):
        return self.comments

    def write(self, method, path, payload=None):
        self.writes.append((method, path, payload))
        return {"id": 1}


class Apply(unittest.TestCase):
    def plan(self):
        s = snapshot(grok_task("vws-iam", 1))
        return run(s), s

    def apply(self, gh, cfg=ON):
        r, s = self.plan()
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

    def test_recheck_skips_posted_key(self):
        r, _ = self.plan()
        gh = FakeGh(comments=[{"body": wakes(r)[0]["body"]}])
        self.apply(gh)
        self.assertEqual(gh.writes, [])

    def test_scope_skips(self):
        gh = FakeGh()
        self.apply(gh, dict(ON, ORCHESTRATE_SCOPE="vws-iam#1"))
        self.assertEqual(gh.writes, [])


if __name__ == "__main__":
    unittest.main()
