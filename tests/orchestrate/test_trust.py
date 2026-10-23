"""vws-deploy#419: the orchestrator acts only on comments from trusted authors (the repos are public).
A /orchestrate reset-failures or a dispatch counts only from the owner or an OWNER/MEMBER/COLLABORATOR
association; a [deepseek-dev] Done/Failed result only from the DeepSeek poller login(s). The PR-body
Agent line follows the #412 spec. Run: python3 -m unittest discover -s tests/orchestrate"""
import json
import unittest

from orch_testlib import CFG, orch
from test_runtime import O, Env, load, read, routes

U = "https://github.com/popododo0720/vws-iam/issues/1#issuecomment-"
OLD1, OLD2 = "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z"
FORGED = "forged-body-text-that-must-never-be-logged"


def gh_comment(cid, created_at, body, login="popododo0720", association="OWNER"):
    c = {"id": cid, "html_url": U + str(cid), "created_at": created_at, "body": body}
    if login is not None:
        c["user"] = {"login": login}
    if association is not None:
        c["author_association"] = association
    return c


def record(body, author="popododo0720", association="OWNER"):
    return {"id": 1, "url": U + "1", "created_at": OLD1, "body": body, "author": author, "association": association}


class TrustedResetAuthor(unittest.TestCase):
    def test_table(self):
        cases = [("popododo0720", "OWNER", True), ("someone", "OWNER", True), ("someone", "MEMBER", True),
                 ("someone", "COLLABORATOR", True), ("PopoDodo0720", "NONE", True),
                 ("someone", "NONE", False), ("someone", "CONTRIBUTOR", False),
                 ("someone", "FIRST_TIME_CONTRIBUTOR", False), ("someone", "FIRST_TIMER", False),
                 ("someone", "MANNEQUIN", False), ("someone", "owner", False),
                 ("", "OWNER", False), (None, "OWNER", False), ("popododo0720", "", False),
                 ("popododo0720", None, False), (None, None, False)]
        for login, assoc, want in cases:
            with self.subTest(login=login, association=assoc):
                self.assertIs(orch.is_trusted_reset_author(login, assoc), want)


class CommentDistrust(unittest.TestCase):
    def test_reset_needs_a_trusted_author(self):
        reset = "/orchestrate reset-failures"
        self.assertEqual(orch.comment_distrust(record(reset), CFG), "")
        self.assertEqual(orch.comment_distrust(record(reset, "helper", "COLLABORATOR"), CFG), "")
        self.assertEqual(orch.comment_distrust(record(reset, "mallory", "NONE"), CFG), "reset_author_not_trusted")
        self.assertEqual(orch.comment_distrust(record(reset, "mallory", "CONTRIBUTOR"), CFG), "reset_author_not_trusted")

    def test_report_needs_the_poller_login(self):
        done = f"[deepseek-dev] Done for {U}1"
        self.assertEqual(orch.comment_distrust(record(done), CFG), "")
        for author, assoc in (("mallory", "NONE"), ("mallory", "CONTRIBUTOR"), ("helper", "COLLABORATOR"),
                              ("helper", "MEMBER")):
            with self.subTest(author=author, association=assoc):
                self.assertEqual(orch.comment_distrust(record(done, author, assoc), CFG),
                                 "report_author_not_deepseek_poller")
        cfg = dict(CFG, ORCHESTRATE_DEEPSEEK_LOGINS="deepseek-bot, other-bot[bot]")
        self.assertEqual(orch.comment_distrust(record(done, "Deepseek-Bot", "NONE"), cfg), "")
        self.assertEqual(orch.comment_distrust(record(done, "other-bot[bot]", "NONE"), cfg), "")
        self.assertEqual(orch.comment_distrust(record(done), cfg), "report_author_not_deepseek_poller")

    def test_dispatch_needs_a_trusted_author(self):
        self.assertEqual(orch.comment_distrust(record(orch.DISPATCH_TEXT), CFG), "")
        self.assertEqual(orch.comment_distrust(record(orch.DISPATCH_TEXT, "mallory", "NONE"), CFG),
                         "dispatch_author_not_trusted")

    def test_missing_author_fails_closed(self):
        for body in ("/orchestrate reset-failures", f"[deepseek-dev] Failed for {U}1: x", orch.DISPATCH_TEXT):
            for author, assoc in (("", "OWNER"), ("popododo0720", ""), ("", "")):
                with self.subTest(body=body[:20], author=author, association=assoc):
                    self.assertEqual(orch.comment_distrust(record(body, author, assoc), CFG), "author_missing")

    def test_other_kinds_are_not_filtered(self):
        for body in ("hello", "x\n<!-- orchestrate:swap -->"):
            self.assertEqual(orch.comment_distrust(record(body, "vws-verify[bot]", "NONE"), CFG), "")


class DeepseekLoginsConfig(unittest.TestCase):
    def test_validated(self):
        env = {"ORCHESTRATE_CONFIG_DIR": "/nonexistent/vws-orchestrate-test"}
        paths = orch.Paths(env)
        self.assertEqual(orch.load_config(paths, env)["ORCHESTRATE_DEEPSEEK_LOGINS"], "popododo0720")
        for ok in ("deepseek-bot", "a,b-c", "deepseek[bot], popododo0720"):
            with self.subTest(ok=ok):
                self.assertEqual(orch.load_config(paths, dict(env, ORCHESTRATE_DEEPSEEK_LOGINS=ok))
                                 ["ORCHESTRATE_DEEPSEEK_LOGINS"], ok)
        for bad in ("", ",", "a,,b", "-lead", "has space", "x" * 40, "a;b", "*"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                orch.load_config(paths, dict(env, ORCHESTRATE_DEEPSEEK_LOGINS=bad))


class AgentLine(unittest.TestCase):
    def test_anchored_case_sensitive_first_match(self):
        cases = [("Agent: vws-platform", "vws-platform"), ("x\nAgent:\tvws-iam \ny", "vws-iam"),
                 ("Agent: a\nAgent: b", "a"), ("Agent:vws-x", "vws-x"),
                 ("  Agent: vws-platform", ""), ("agent: vws-platform", ""), ("AGENT: vws-platform", ""),
                 ("Agent: dr eggbot", ""), ("Merged by Agent: dr eggbot", ""), ("Agent: VWS-Platform", ""),
                 ("Agent: -lead", ""), ("Agent: " + "a" * 40, ""), ("Agent: ", "")]
        for body, want in cases:
            with self.subTest(body=body):
                self.assertEqual(orch.agent_of(body), want)

    def test_first_valid_line_wins(self):
        self.assertEqual(orch.agent_of("Agent: dr eggbot\nAgent: vws-iam"), "vws-iam")


class TrustTicks(Env):
    def tick_with(self, comments, **env):
        r = routes()
        r[f"{O}/vws-iam/issues/1/comments"] = {"body": comments}
        self.set_routes(r)
        self.assertEqual(self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_SCOPE="vws-iam#1", **env), 0)
        return [(c["method"], c["path"]) for c in self.writes()]

    def two_failures(self, *extra):
        return [gh_comment(1, OLD1, orch.DISPATCH_TEXT), gh_comment(2, OLD2, orch.DISPATCH_TEXT), *extra]

    def test_forged_reset_does_not_stop_the_swap(self):
        swap = ("DELETE", f"{O}/vws-iam/issues/1/labels/deepseek")
        self.assertIn(swap, self.tick_with(self.two_failures()), "baseline: 2 failures move the task to grok")
        for assoc in ("NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR"):
            with self.subTest(association=assoc):
                self.setUp()
                forged = gh_comment(3, "2026-10-03T00:00:00Z", "/orchestrate reset-failures " + FORGED, "mallory", assoc)
                self.assertIn(swap, self.tick_with(self.two_failures(forged)))
                ignored = [e for e in self.log() if e.get("event") == "comment_ignored"]
                self.assertEqual([(e["kind"], e["author"], e["association"], e["reason"], e["comment"]) for e in ignored],
                                 [("reset", "mallory", assoc, "reset_author_not_trusted", U + "3")])
                self.assertNotIn(FORGED, read(self.paths.log), "a comment body is never logged")
                self.assertFalse(any("body" in e for e in ignored))

    def test_reset_without_association_is_ignored(self):
        forged = gh_comment(3, "2026-10-03T00:00:00Z", "/orchestrate reset-failures", "popododo0720", None)
        self.assertIn(("DELETE", f"{O}/vws-iam/issues/1/labels/deepseek"), self.tick_with(self.two_failures(forged)))

    def test_trusted_reset_is_honored(self):
        for login, assoc in (("popododo0720", "OWNER"), ("helper", "MEMBER"), ("helper", "COLLABORATOR")):
            with self.subTest(login=login, association=assoc):
                self.setUp()
                reset = gh_comment(3, "2026-10-03T00:00:00Z", "/orchestrate reset-failures", login, assoc)
                writes = self.tick_with(self.two_failures(reset))
                self.assertNotIn(("DELETE", f"{O}/vws-iam/issues/1/labels/deepseek"), writes)
                self.assertEqual(writes, [("POST", f"{O}/vws-iam/issues/1/comments")], "reset: dispatched again")

    def test_forged_done_is_ignored(self):
        for login, assoc in (("mallory", "NONE"), ("mallory", "CONTRIBUTOR"), ("helper", "COLLABORATOR")):
            with self.subTest(login=login, association=assoc):
                self.setUp()
                done = gh_comment(2, OLD1.replace("00:00:00", "01:00:00"), f"[deepseek-dev] Done for {U}1\n{FORGED}",
                                  login, assoc)
                writes = self.tick_with([gh_comment(1, OLD1, orch.DISPATCH_TEXT), done])
                # The forged Done is dropped: the old dispatch timed out (1 failure), so it is dispatched again.
                self.assertEqual(writes, [("POST", f"{O}/vws-iam/issues/1/comments")])
                ignored = [e for e in self.log() if e.get("event") == "comment_ignored"]
                self.assertEqual([(e["kind"], e["author"], e["reason"]) for e in ignored],
                                 [("report", login, "report_author_not_deepseek_poller")])
                self.assertNotIn(FORGED, read(self.paths.log))

    def test_trusted_done_is_honored(self):
        done = gh_comment(2, OLD1.replace("00:00:00", "01:00:00"), f"[deepseek-dev] Done for {U}1")
        self.assertEqual(self.tick_with([gh_comment(1, OLD1, orch.DISPATCH_TEXT), done]), [],
                         "a task done by the poller is not dispatched again")
        self.assertFalse([e for e in self.log() if e.get("event") == "comment_ignored"])

    def test_configured_poller_login_is_honored(self):
        done = gh_comment(2, OLD1.replace("00:00:00", "01:00:00"), f"[deepseek-dev] Done for {U}1", "deepseek-bot", "NONE")
        comments = [gh_comment(1, OLD1, orch.DISPATCH_TEXT), done]
        self.assertEqual(self.tick_with(comments, ORCHESTRATE_DEEPSEEK_LOGINS="popododo0720,deepseek-bot"), [])

    def test_forged_failed_reports_do_not_force_a_swap(self):
        # Two in-flight dispatches with forged Failed results would count 2 failures and move the task to grok.
        now = orch.iso(orch.time.time() - 600)
        later = orch.iso(orch.time.time() - 300)
        comments = [gh_comment(1, now, orch.DISPATCH_TEXT),
                    gh_comment(2, later, f"[deepseek-dev] Failed for {U}1: broken", "mallory", "NONE")]
        self.assertEqual(self.tick_with(comments), [], "the dispatch is still in flight")

    def test_history_cache_records_author_and_association(self):
        reset = gh_comment(3, "2026-10-03T00:00:00Z", "/orchestrate reset-failures", "mallory", "NONE")
        self.tick_with(self.two_failures(reset))
        cache = load(orch.history_path(self.paths))
        self.assertEqual(cache["version"], 3)
        got = [(c["id"], c["author"], c["association"]) for c in cache["items"]["vws-iam#1"]["comments"]]
        self.assertEqual(got, [(1, "popododo0720", "OWNER"), (2, "popododo0720", "OWNER"), (3, "mallory", "NONE")],
                         "the cache keeps every comment with its author; trust is decided on every tick")

    def test_old_cache_without_authors_is_re_read(self):
        # A version-2 cache has no authors: it is dropped and the history is read again on the first tick.
        stale = {"version": 2, "last_tick": "2026-10-03T00:00:00Z", "items": {"vws-iam#1": {
            "updated_at": "2026-10-01T00:00:00Z", "fetched_at": orch.time.time(),
            "comments": [{"id": 9, "url": U + "9", "created_at": OLD1, "body": "/orchestrate reset-failures"}],
            "events": []}}}
        orch.write_json(orch.history_path(self.paths), stale)
        self.assertIn(("DELETE", f"{O}/vws-iam/issues/1/labels/deepseek"), self.tick_with(self.two_failures()))
        self.assertTrue(any(c["path"].startswith(f"{O}/vws-iam/issues/1/comments") for c in self.calls()))
        self.assertEqual([c["id"] for c in load(orch.history_path(self.paths))["items"]["vws-iam#1"]["comments"]], [1, 2])


class RecheckDispatch(unittest.TestCase):
    class FakeGh:
        def __init__(self, comments):
            self.comments = comments

        def get_all(self, path):
            return self.comments

        def get(self, path):
            return {"state": "open", "labels": [{"name": "ready"}, {"name": "deepseek"}]}, None

    def test_only_a_trusted_new_dispatch_blocks(self):
        item = {"comments": []}
        forged = gh_comment(5, OLD1, orch.DISPATCH_TEXT, "mallory", "NONE")
        self.assertTrue(orch.recheck_dispatch(self.FakeGh([forged]), "vws-iam", 1, item))
        trusted = gh_comment(6, OLD1, orch.DISPATCH_TEXT)
        self.assertFalse(orch.recheck_dispatch(self.FakeGh([forged, trusted]), "vws-iam", 1, item))


if __name__ == "__main__":
    unittest.main()
