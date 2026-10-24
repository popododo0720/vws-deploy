"""vws-deploy's own CI-failure marker caller (vws-deploy#412): .github/workflows/ci-fail-caller.yml.

ci-fail.yml in this repo is the reusable workflow; the caller fires it on workflow_run for every CI
workflow that runs on pull_request, pinned to a full vws-deploy commit like every other repo's caller.
Run: python3 -m unittest discover -s tests/workflows
"""
import glob
import os
import re
import subprocess
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WF_DIR = os.path.join(ROOT, ".github", "workflows")
CALLER = os.path.join(WF_DIR, "ci-fail-caller.yml")
USES_RE = re.compile(r"^popododo0720/vws-deploy/\.github/workflows/ci-fail\.yml@([0-9a-f]{40})$")
# Not CI on a PR head: repo-guard's verify-gate is red on every head until vws-verify approves,
# post-merge runs only on closed PRs, and verify-queue only moves the needs-verify label.
NOT_CI = {"repo-guard", "post-merge", "verify-queue"}


def load(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def triggers(wf):
    # PyYAML reads the bare key "on" as True.
    on = wf.get("on", wf.get(True))
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return {k: None for k in on}
    return on or {}


def pr_ci_workflows():
    names = set()
    for path in glob.glob(os.path.join(WF_DIR, "*.y*ml")):
        wf = load(path)
        if "pull_request" in triggers(wf) and wf["name"] not in NOT_CI:
            names.add(wf["name"])
    return names


class CiFailCaller(unittest.TestCase):
    def setUp(self):
        self.assertTrue(os.path.isfile(CALLER), "vws-deploy has no ci-fail caller (.github/workflows/ci-fail-caller.yml)")
        self.wf = load(CALLER)

    def test_fires_on_every_pull_request_ci_workflow(self):
        run = triggers(self.wf).get("workflow_run") or {}
        self.assertEqual(run.get("types"), ["completed"])
        self.assertEqual(set(run.get("workflows") or []), pr_ci_workflows())
        self.assertEqual(set(triggers(self.wf)), {"workflow_run"}, "the caller must run only on workflow_run")

    def test_least_privilege(self):
        self.assertEqual(self.wf.get("permissions"), {})
        jobs = self.wf["jobs"]
        self.assertEqual(len(jobs), 1)
        job = next(iter(jobs.values()))
        self.assertEqual(job.get("permissions"), {"actions": "read", "contents": "read", "pull-requests": "write"})
        self.assertNotIn("secrets", job)

    def test_uses_the_reusable_workflow_at_a_full_sha(self):
        job = next(iter(self.wf["jobs"].values()))
        m = USES_RE.match(job.get("uses", ""))
        self.assertIsNotNone(m, "uses must be popododo0720/vws-deploy/.github/workflows/ci-fail.yml@<40 hex>")
        sha = m.group(1)
        has = subprocess.run(["git", "-C", ROOT, "cat-file", "-e", f"{sha}^{{commit}}"], capture_output=True)
        if has.returncode != 0:
            self.skipTest(f"{sha} is not in this checkout (shallow clone)")
        for path in (".github/workflows/ci-fail.yml", ".github/ci-fail/ci_fail.py"):
            got = subprocess.run(["git", "-C", ROOT, "cat-file", "-e", f"{sha}:{path}"], capture_output=True)
            self.assertEqual(got.returncode, 0, f"the pinned commit {sha} has no {path}")
        anc = subprocess.run(["git", "-C", ROOT, "merge-base", "--is-ancestor", sha, "HEAD"], capture_output=True)
        self.assertEqual(anc.returncode, 0, f"the pinned commit {sha} is not on this branch's history (main)")


if __name__ == "__main__":
    unittest.main()
