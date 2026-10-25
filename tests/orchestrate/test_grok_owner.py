"""vws-deploy#506: every grok-wake names its owning Grok dev bot on an `Owner `<bot>`` line, so each bot's
pr-comment routine on the verify-wake thread can match its own wakes. A task wake resolves the owner
from the issue body `Agent:` line, then ORCHESTRATE_GROK_OWNERS (repo=bot), then the repo name. A grok
rework wake whose PR has no `Agent:` line resolves the bot the same way from the PR's repo."""
import re
import unittest

from orch_testlib import CFG, NOW, orch, snapshot
from test_grok import ON, grok_task, wakes
from test_grok_rework import wakes as rework_wakes
from test_verify_fail_owner import failed_pr, grok_issue

# The line a consumer routine matches (docs/factory-orchestrator.md, grok-wake consumers).
OWNER_LINE = re.compile(r"^Owner `([a-z0-9][a-z0-9-]{0,38})`", re.M)


def owners(body):
    return OWNER_LINE.findall(body)


def task_wake(*items, cfg=None):
    allow = ", ".join(f"{i['repo']}#{i['number']}" for i in items)
    r = orch.decide(snapshot(*items), {}, NOW, dict(cfg or ON, ORCHESTRATE_GROK_ALLOWLIST=allow), set())
    [w] = wakes(r)
    return w


def rework_wake(*items, cfg=CFG):
    [w] = rework_wakes(orch.decide(snapshot(*items), {}, NOW, cfg, set()))
    return w


class TaskWake(unittest.TestCase):
    def test_platform_repo_names_vws_platform(self):
        for repo in ("vws-deploy", "vws-lib", "vws-proto", "vws-test", "vws-conformance"):
            with self.subTest(repo=repo):
                w = task_wake(grok_task(repo, 7))
                self.assertEqual(owners(w["body"]), ["vws-platform"])
                self.assertEqual(w["owner"], "vws-platform")

    def test_service_repo_names_its_own_bot(self):
        for repo in ("vws-compute", "vws-storage", "vws-network", "vws-console-api", "vws-console", "vws-iam"):
            with self.subTest(repo=repo):
                self.assertEqual(owners(task_wake(grok_task(repo, 7))["body"]), [repo])

    def test_issue_agent_line_wins(self):
        w = task_wake(grok_task("vws-deploy", 7, body="Scope: compute.\n\nAgent: vws-compute\n"))
        self.assertEqual(owners(w["body"]), ["vws-compute"])

    def test_override_maps_a_repo(self):
        cfg = dict(ON, ORCHESTRATE_GROK_OWNERS="vws-console-api=vws-api")
        self.assertEqual(owners(task_wake(grok_task("vws-console-api", 7), cfg=cfg)["body"]), ["vws-api"])
        # The override replaces the default map: an unlisted repo falls back to its own name.
        self.assertEqual(owners(task_wake(grok_task("vws-deploy", 7), cfg=cfg)["body"]), ["vws-deploy"])

    def test_body_keeps_the_task_contract(self):
        body = task_wake(grok_task("vws-deploy", 7))["body"]
        self.assertTrue(body.startswith(orch.GROK_WAKE_MARKER + "\n"))
        self.assertIn("`Part of #7`", body)
        self.assertTrue(body.endswith(f"{orch.GROK_ASSIGN_MARKER}vws-deploy#7 -->"))
        self.assertNotIn("Owning Grok dev bot", body)

    def test_a_forged_agent_line_in_a_quote_does_not_count(self):
        w = task_wake(grok_task("vws-iam", 7, body="> Agent: vws-platform\n"))
        self.assertEqual(owners(w["body"]), ["vws-iam"])


class ReworkWake(unittest.TestCase):
    def test_grok_label_owner_names_the_repo_bot(self):
        w = rework_wake(failed_pr(), grok_issue())
        self.assertEqual(w["owner"], "grok")
        self.assertEqual(owners(w["body"]), ["vws-platform"])

    def test_agent_line_owner_is_named_as_is(self):
        # The vws-deploy#499 shape: `Agent: vws-platform` on the PR body (10:26 KST wake, 2026-10-25).
        w = rework_wake(failed_pr(body="Part of #486\n\nAgent: vws-platform\n"))
        self.assertEqual((w["owner"], owners(w["body"])), ("vws-platform", ["vws-platform"]))

    def test_service_repo_rework(self):
        w = rework_wake(failed_pr(repo="vws-iam", body="Part of #9"), grok_issue(9, "vws-iam"))
        self.assertEqual(owners(w["body"]), ["vws-iam"])


class Config(unittest.TestCase):
    ENV = {"ORCHESTRATE_CONFIG_DIR": "/nonexistent/vws-orchestrate-test"}

    def test_default_maps_platform_repos(self):
        self.assertEqual(orch.grok_owners(CFG)["vws-deploy"], "vws-platform")

    def test_bad_entries_are_rejected(self):
        paths = orch.Paths(self.ENV)
        for bad in ("vws-deploy", "vws-deploy=", "=vws-x", "deploy=vws-platform", "vws-deploy=Bad Bot"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                orch.load_config(paths, dict(self.ENV, ORCHESTRATE_GROK_OWNERS=bad))
        cfg = orch.load_config(paths, dict(self.ENV, ORCHESTRATE_GROK_OWNERS="vws-console-api=vws-api, vws-x=vws-y"))
        self.assertEqual(orch.grok_owners(cfg), {"vws-console-api": "vws-api", "vws-x": "vws-y"})


if __name__ == "__main__":
    unittest.main()
