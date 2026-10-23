"""ci-fail.yml runs ci_fail.py from the commit the caller pinned, never from a branch (vws-deploy#412, #402).

Run: python3 -m unittest discover -s tests/workflows
"""
import os
import re
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WF_PATH = os.path.join(ROOT, ".github", "workflows", "ci-fail.yml")
DOC_PATH = os.path.join(ROOT, "docs", "ci-fail.md")
SHA_PIN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(/[^@]+)?@[0-9a-f]{40}$")


def steps():
    wf = yaml.safe_load(open(WF_PATH))
    return [s for job in wf["jobs"].values() for s in job.get("steps", [])]


class CiFailPin(unittest.TestCase):
    def test_every_checkout_is_the_workflow_commit(self):
        checkouts = [s for s in steps() if str(s.get("uses", "")).startswith("actions/checkout@")]
        self.assertTrue(checkouts)
        for s in checkouts:
            w = s.get("with") or {}
            self.assertEqual(w.get("ref"), "${{ job.workflow_sha }}", "checkout must use the pinned workflow commit")
            self.assertEqual(w.get("repository"), "${{ job.workflow_repository }}")
            self.assertIs(w.get("persist-credentials"), False)

    def test_actions_are_pinned_to_a_full_sha(self):
        for s in steps():
            if "uses" in s:
                self.assertRegex(s["uses"], SHA_PIN)

    def test_doc_sample_caller_pins_a_full_sha(self):
        uses = re.findall(r"^\s*uses:\s*(\S+)", open(DOC_PATH).read(), re.M)
        self.assertTrue([u for u in uses if "ci-fail.yml" in u])
        for u in uses:
            self.assertRegex(u, SHA_PIN)


if __name__ == "__main__":
    unittest.main()
