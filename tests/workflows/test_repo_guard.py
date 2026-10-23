"""Behavior tests for .github/workflows/repo-guard.yml run scripts, with gh and sleep stubbed.

Run: python3 -m unittest discover -s tests/workflows
"""
import os
import stat
import subprocess
import tempfile
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WF = yaml.safe_load(open(os.path.join(ROOT, ".github", "workflows", "repo-guard.yml")))
ON = WF.get("on", WF.get(True))  # PyYAML reads the "on" key as True


def step(job, name):
    return next(s for s in WF["jobs"][job]["steps"] if s.get("name") == name)


class Stub:
    """A PATH dir with fake gh/sleep; gh appends its args to a log and runs a bash case on them."""

    def __init__(self, test, gh_body):
        self.dir = tempfile.mkdtemp()
        self.log = os.path.join(self.dir, "gh.log")
        self.write("gh", 'echo "$*" >>"%s"\n%s\n' % (self.log, gh_body))
        self.write("sleep", "exit 0\n")
        test.addCleanup(subprocess.run, ["rm", "-rf", self.dir])

    def write(self, name, body):
        p = os.path.join(self.dir, name)
        with open(p, "w") as f:
            f.write("#!/usr/bin/env bash\n" + body)
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)

    def run(self, script, env):
        e = dict(os.environ, PATH=self.dir + ":" + os.environ["PATH"], **env)
        r = subprocess.run(["bash", "-c", script], env=e, capture_output=True, text=True)
        calls = open(self.log).read() if os.path.exists(self.log) else ""
        return r.returncode, calls, r.stdout + r.stderr


class VerifyReset(unittest.TestCase):
    ENV = {"GH_TOKEN": "x", "REPO": "o/r", "NUMBER": "7", "LABELS": "bug,verify-approved"}

    def script(self):
        return step("verify-reset", "Remove verify-approved")["run"]

    def test_label_already_gone_is_ok(self):
        rc, _, out = Stub(self, 'echo "gh: Label does not exist (HTTP 404)" >&2; exit 1').run(self.script(), self.ENV)
        self.assertEqual(rc, 0, out)
        self.assertIn("already removed", out)

    def test_other_error_fails(self):
        rc, _, _ = Stub(self, 'echo "gh: Server Error (HTTP 500)" >&2; exit 1').run(self.script(), self.ENV)
        self.assertNotEqual(rc, 0)

    def test_removed(self):
        rc, calls, _ = Stub(self, "exit 0").run(self.script(), self.ENV)
        self.assertEqual(rc, 0)
        self.assertIn("-X DELETE repos/o/r/issues/7/labels/verify-approved", calls)


class PrHygiene(unittest.TestCase):
    def test_body_checks_run_on_a_body_edit(self):
        for action in ("opened", "edited", "synchronize"):
            self.assertIn(action, ON["pull_request"]["types"])
        self.assertIn("pr-checklist", step("pr-hygiene", "Body has the 9-item Quality checklist, all ticked with evidence")["run"])
        self.assertIn("pr-commits", step("pr-hygiene", "Commit subjects have a type prefix and Part of #N")["run"])

    def script(self):
        return step("pr-hygiene", "Acceptance criteria of the linked issue")["run"]

    def summary(self):
        fd, path = tempfile.mkstemp()
        os.close(fd)
        self.addCleanup(os.unlink, path)
        return path

    def run_step(self, gh, env):
        return Stub(self, gh).run(self.script(), dict(env, GH_TOKEN="x", REPO="o/r"))

    def test_ac_lint_goes_to_the_job_summary(self):
        path = self.summary()
        gh = r'''case "$*" in
  *issues/7*) printf '## Acceptance criteria\n\n- [x] one https://example.invalid/1\n- [ ] two\n- [ ] \n' ;;
  *) exit 1 ;;
esac'''
        rc, calls, out = self.run_step(gh, {"PR_BODY": "Part of #7", "GITHUB_STEP_SUMMARY": path})
        self.assertEqual(rc, 0, out)
        self.assertIn("issues/7", calls)
        text = open(path).read()
        self.assertIn("OK", text)
        self.assertIn("OPEN", text)
        self.assertIn("3 boxes, 0 ticked without evidence", text)

    def test_a_ticked_box_without_evidence_does_not_fail_the_job(self):
        path = self.summary()
        gh = r'''case "$*" in
  *issues/7*) printf '## Acceptance criteria\n\n- [x] no evidence\n' ;;
  *) exit 1 ;;
esac'''
        rc, _, out = self.run_step(gh, {"PR_BODY": "Part of #7", "GITHUB_STEP_SUMMARY": path})
        self.assertEqual(rc, 0, out)
        text = open(path).read()
        self.assertIn("NO-EVIDENCE", text)
        self.assertIn("1 boxes, 1 ticked without evidence", text)

    def test_ac_lint_read_failure_does_not_fail_the_job(self):
        path = self.summary()
        rc, _, out = self.run_step("exit 1", {"PR_BODY": "Part of #7", "GITHUB_STEP_SUMMARY": path})
        self.assertEqual(rc, 0, out)
        self.assertIn("could not be read", open(path).read())

    def test_no_part_of_does_not_fail_the_job(self):
        path = self.summary()
        rc, _, out = self.run_step("exit 1", {"PR_BODY": "no issue", "GITHUB_STEP_SUMMARY": path})
        self.assertEqual(rc, 0, out)
        self.assertIn("no 'Part of #N' issue to lint", open(path).read())


class CommentRerun(unittest.TestCase):
    ENV = {"GH_TOKEN": "x", "REPO": "o/r", "NUMBER": "7"}
    GH = """case "$*" in
  *pulls/7*) echo abc123 ;;
  *workflows/repo-guard.yml/runs*) echo 55 ;;
  *runs/55/jobs*) echo 99 ;;
  "api repos/o/r/actions/jobs/99 --jq .status")
     n=$(cat "$0.n" 2>/dev/null || echo 0); echo $((n + 1)) >"$0.n"
     if [ "$n" -lt %d ]; then echo in_progress; else echo completed; fi ;;
  *jobs/99/rerun*) exit 0 ;;
esac"""

    def test_triggers(self):
        self.assertEqual(sorted(ON["issue_comment"]["types"]), ["created", "deleted", "edited"])
        for job in ("pr-hygiene", "verify-gate"):
            self.assertIn("github.event_name == 'pull_request'", WF["jobs"][job]["if"])
        self.assertIn("issue_comment", WF["jobs"]["verify-rerun"]["if"])
        self.assertEqual(WF["jobs"]["verify-rerun"]["permissions"], {"actions": "write", "pull-requests": "read"})

    def script(self):
        return step("verify-rerun", "Re-run verify-gate for the PR head")["run"]

    def test_waits_for_running_job_then_reruns(self):
        rc, calls, out = Stub(self, self.GH % 3).run(self.script(), self.ENV)
        self.assertEqual(rc, 0, out)
        self.assertIn("-X POST repos/o/r/actions/jobs/99/rerun", calls)
        self.assertIn("head_sha=abc123", calls)

    def test_gives_up_with_failure(self):
        rc, calls, _ = Stub(self, self.GH % 1000).run(self.script(), self.ENV)
        self.assertNotEqual(rc, 0)
        self.assertNotIn("rerun", calls.replace("verify-rerun", ""))


if __name__ == "__main__":
    unittest.main()
