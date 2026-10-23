"""The ci-fail.yml contract that every per-repo caller depends on (vws-deploy#412).

Ported from the workflow tests of the superseded #421, adapted to the merged design: the
reusable workflow takes no inputs, and the caller only lists its workflows and grants permissions.

Run: python3 -m unittest discover -s tests/workflows
"""
import os
import re
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WF_PATH = os.path.join(ROOT, ".github", "workflows", "ci-fail.yml")
DOC_PATH = os.path.join(ROOT, "docs", "ci-fail.md")
JOB_PERMISSIONS = {"actions": "read", "contents": "read", "pull-requests": "write"}


def on(wf):
    return wf.get("on", wf.get(True))


def doc_caller():
    blocks = re.findall(r"^```yaml\n(.*?)^```$", open(DOC_PATH).read(), re.M | re.S)
    callers = [yaml.safe_load(b) for b in blocks if "ci-fail.yml@" in b]
    if len(callers) != 1:
        raise AssertionError(f"docs/ci-fail.md must have exactly one sample caller, found {len(callers)}")
    return callers[0]


class ReusableWorkflow(unittest.TestCase):
    def setUp(self):
        self.wf = yaml.safe_load(open(WF_PATH))
        self.job = self.wf["jobs"]["marker"]

    def test_is_workflow_call_only_without_inputs_or_secrets(self):
        self.assertEqual(list(on(self.wf)), ["workflow_call"])
        self.assertFalse(on(self.wf)["workflow_call"], "callers pass nothing; the list lives in the caller trigger")

    def test_permissions_are_least_privilege(self):
        self.assertEqual(self.wf["permissions"], {})
        self.assertEqual(self.job["permissions"], JOB_PERMISSIONS)

    def test_job_runs_only_for_failed_pull_request_workflow_runs(self):
        cond = " ".join(self.job["if"].split())
        for text in ("github.event_name == 'workflow_run'",
                     "github.event.workflow_run.event == 'pull_request'",
                     '["failure","timed_out","startup_failure","action_required"]',
                     "github.event.workflow_run.conclusion"):
            self.assertIn(text, cond)

    def test_one_serialized_run_per_head_sha(self):
        self.assertEqual(self.job["concurrency"]["group"],
                         "vws-ci-fail-${{ github.repository }}-${{ github.event.workflow_run.head_sha }}")
        self.assertIs(self.job["concurrency"]["cancel-in-progress"], False)
        self.assertLessEqual(int(self.job["timeout-minutes"]), 10)

    def test_the_token_reaches_only_the_script_step(self):
        for s in self.job["steps"]:
            env = s.get("env") or {}
            with_ = s.get("with") or {}
            if "run" in s:
                self.assertEqual(env, {"GH_TOKEN": "${{ github.token }}"})
                self.assertNotIn("${{ github.event", s["run"], "event fields must not reach a shell line")
            else:
                self.assertNotIn("token", with_)
                self.assertNotIn("GH_TOKEN", env)


class DocSampleCaller(unittest.TestCase):
    def setUp(self):
        self.caller = doc_caller()

    def test_triggers_on_completed_workflow_runs_by_name(self):
        trig = on(self.caller)
        self.assertEqual(list(trig), ["workflow_run"])
        self.assertEqual(trig["workflow_run"]["types"], ["completed"])
        names = trig["workflow_run"]["workflows"]
        self.assertTrue(names)
        self.assertTrue(all(isinstance(n, str) and n for n in names))

    def test_grants_exactly_the_reusable_job_permissions_and_passes_nothing(self):
        self.assertEqual(self.caller["permissions"], {})
        (job,) = self.caller["jobs"].values()
        self.assertEqual(job["permissions"], JOB_PERMISSIONS)
        self.assertNotIn("with", job)
        self.assertNotIn("secrets", job)
        self.assertTrue(job["uses"].startswith("popododo0720/vws-deploy/.github/workflows/ci-fail.yml@"))


if __name__ == "__main__":
    unittest.main()
