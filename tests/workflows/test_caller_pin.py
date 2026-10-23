"""Tests for .github/drift/caller_pin.py: a repo-guard.yml caller pins the shared jobs by full SHA (vws-deploy#402).

Run: python3 -m unittest discover -s tests/workflows
"""
import importlib.util
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PIN_PY = os.path.join(ROOT, ".github", "drift", "caller_pin.py")
spec = importlib.util.spec_from_file_location("caller_pin", PIN_PY)
cp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cp)

SHA = "0123456789abcdef0123456789abcdef01234567"
REF = "popododo0720/vws-deploy/.github/workflows/repo-guard-reusable.yml@" + SHA

# The caller every other repo carries, with REF in each uses line.
GOOD = """name: repo-guard
on:
  pull_request:
    types: [opened, edited, reopened, synchronize, labeled, unlabeled]
  issue_comment:
    types: [created, edited, deleted]
permissions: {}
jobs:
  hygiene:
    if: github.event_name == 'pull_request'
    uses: REF
    with: {job: pr-hygiene}
    permissions: {contents: read, pull-requests: read}
  gate:
    if: github.event_name == 'pull_request'
    uses: REF
    with: {job: verify-gate}
    permissions: {contents: read, pull-requests: read, issues: read}
  reset:
    if: github.event_name == 'pull_request' && github.event.action == 'synchronize'
    uses: REF
    with: {job: verify-reset}
    permissions: {issues: write, pull-requests: write}
  rerun:
    if: github.event_name == 'issue_comment' && github.event.issue.pull_request
    uses: REF
    with: {job: verify-rerun}
    permissions: {actions: write, pull-requests: read}
  pr-hygiene:
    needs: hygiene
    if: always() && github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    steps: [{env: {R: "${{ needs.hygiene.result }}/${{ needs.hygiene.outputs.passed }}"}, run: test "$R" = success/pr-hygiene}]
  verify-gate:
    needs: gate
    if: always() && github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    steps: [{env: {R: "${{ needs.gate.result }}/${{ needs.gate.outputs.passed }}"}, run: test "$R" = success/verify-gate}]
"""


def caller(ref=REF, text=GOOD):
    return text.replace("REF", ref)


class CallerPin(unittest.TestCase):
    def test_good_caller_returns_the_pin(self):
        self.assertEqual(cp.check(caller()), ([], SHA))

    def test_branch_tag_and_short_refs_fail(self):
        base = REF.split("@")[0]
        for ref in (base + "@main", base + "@v1", base + "@" + SHA[:12], base + "@" + SHA.upper(),
                    base + "@refs/heads/main", "popododo0720/vws-other/.github/workflows/repo-guard-reusable.yml@" + SHA):
            errs, _ = cp.check(caller(ref))
            self.assertTrue(errs, ref)
            self.assertTrue(any("40-hex" in e for e in errs), (ref, errs))

    def test_local_ref_only_in_the_home_repo(self):
        local = "./.github/workflows/repo-guard-reusable.yml"
        errs, _ = cp.check(caller(local))
        self.assertTrue(errs)
        self.assertEqual(cp.check(caller(local), home=True), ([], "local"))

    def test_home_repo_must_not_pin_a_sha(self):
        errs, _ = cp.check(caller(), home=True)
        self.assertTrue(errs)

    def test_mixed_pins_fail(self):
        other = REF.replace(SHA, "f" * 40)
        text = GOOD.replace("REF", other, 1).replace("REF", REF)
        errs, _ = cp.check(text)
        self.assertTrue(any("same SHA" in e for e in errs), errs)

    def test_forbidden_triggers_fail(self):
        for trig in ("  pull_request_target:\n    types: [opened]\n", "  push:\n", "  workflow_run:\n    workflows: [x]\n"):
            text = caller().replace("  issue_comment:\n", trig + "  issue_comment:\n")
            errs, _ = cp.check(text)
            self.assertTrue(errs, trig)

    def test_wider_permissions_fail(self):
        text = caller().replace("{contents: read, pull-requests: read, issues: read}", "{contents: write, pull-requests: read, issues: read}")
        self.assertTrue(cp.check(text)[0])
        text = caller().replace("permissions: {}\n", "permissions: write-all\n")
        self.assertTrue(cp.check(text)[0])

    def test_secrets_are_never_passed(self):
        text = caller().replace("    with: {job: verify-gate}\n", "    with: {job: verify-gate}\n    secrets: inherit\n")
        self.assertTrue(cp.check(text)[0])

    def test_wrong_job_input_fails(self):
        text = caller().replace("with: {job: verify-gate}", "with: {job: pr-hygiene}")
        self.assertTrue(cp.check(text)[0])

    def test_shim_that_ignores_the_result_fails(self):
        text = caller().replace('run: test "$R" = success/verify-gate', "run: 'true'")
        self.assertTrue(cp.check(text)[0])
        text = caller().replace("    if: always() && github.event_name == 'pull_request'\n    runs-on: ubuntu-latest\n    steps: [{env: {R: \"${{ needs.gate",
                                "    if: github.event_name == 'pull_request'\n    runs-on: ubuntu-latest\n    steps: [{env: {R: \"${{ needs.gate")
        self.assertTrue(cp.check(text)[0])

    def test_missing_or_extra_job_fails(self):
        text = caller() + "  extra:\n    runs-on: ubuntu-latest\n    steps: [{run: 'true'}]\n"
        self.assertTrue(cp.check(text)[0])
        cut = caller().split("  verify-gate:\n")[0]
        self.assertTrue(cp.check(cut)[0])

    def test_caller_longer_than_40_lines_fails(self):
        text = caller() + "#\n" * (41 - len(caller().splitlines()))
        self.assertGreater(len(text.splitlines()), 40)
        errs, _ = cp.check(text)
        self.assertTrue(any("40 lines" in e for e in errs), errs)

    def test_not_yaml_fails(self):
        self.assertTrue(cp.check("jobs: [unclosed\n")[0])
        self.assertTrue(cp.check("")[0])

    def test_this_repo_is_a_home_caller(self):
        path = os.path.join(ROOT, ".github", "workflows", "repo-guard.yml")
        self.assertEqual(cp.check(open(path).read(), home=True), ([], "local"))

    def test_cli(self):
        path = os.path.join(ROOT, ".github", "workflows", "repo-guard.yml")
        r = subprocess.run([sys.executable, PIN_PY, "--home", path], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run([sys.executable, PIN_PY, path], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("40-hex", r.stderr)


class PinDrift(unittest.TestCase):
    """Every caller pins the same SHA, and that SHA is on vws-deploy main."""

    def test_same_sha_on_main_is_clean(self):
        self.assertEqual(cp.pin_drift({"a": SHA, "b": SHA}, lambda s: True), {})

    def test_sha_not_on_main(self):
        self.assertEqual(cp.pin_drift({"a": SHA}, lambda s: False), {"a": "pin %s is not on vws-deploy main" % SHA[:12]})

    def test_odd_one_out(self):
        other = "f" * 40
        out = cp.pin_drift({"a": SHA, "b": SHA, "c": other}, lambda s: True)
        self.assertEqual(list(out), ["c"])
        self.assertIn("not the common pin", out["c"])

    def test_tie_flags_every_caller(self):
        out = cp.pin_drift({"a": SHA, "b": "f" * 40}, lambda s: True)
        self.assertEqual(sorted(out), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
