"""Behavior tests for the shared repo-guard jobs, with gh and sleep stubbed.

The jobs live in .github/workflows/repo-guard-reusable.yml (vws-deploy#402); repo-guard.yml is a caller.

Run: python3 -m unittest discover -s tests/workflows
"""
import os
import stat
import subprocess
import tempfile
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WF = yaml.safe_load(open(os.path.join(ROOT, ".github", "workflows", "repo-guard-reusable.yml")))
CALLER = yaml.safe_load(open(os.path.join(ROOT, ".github", "workflows", "repo-guard.yml")))
ON = CALLER.get("on", CALLER.get(True))  # PyYAML reads the "on" key as True


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

    def test_reruns_the_called_gate_job_not_the_shim(self):
        # The caller's bare "verify-gate" shim only reads the result of "gate / verify-gate"; re-running the
        # shim alone would replay the old result, so the called job is the one re-run.
        rc, calls, out = Stub(self, self.GH % 0).run(self.script(), self.ENV)
        self.assertEqual(rc, 0, out)
        self.assertIn('.name == "gate / verify-gate"', calls)


class Reusable(unittest.TestCase):
    """The callee runs only on workflow_call, picks one job per call and never sees pull_request_target."""

    def test_only_workflow_call(self):
        on = WF.get("on", WF.get(True))
        self.assertEqual(list(on), ["workflow_call"])
        self.assertEqual(on["workflow_call"]["inputs"]["job"]["type"], "string")
        self.assertTrue(on["workflow_call"]["inputs"]["job"]["required"])
        self.assertNotIn("secrets", on["workflow_call"])
        self.assertEqual(WF["permissions"], {})

    def test_each_job_runs_only_for_its_input(self):
        for job in ("pr-hygiene", "verify-gate", "verify-reset", "verify-rerun"):
            self.assertIn("inputs.job == '%s'" % job, WF["jobs"][job]["if"])

    def test_no_pull_request_target(self):
        for name in ("repo-guard-reusable.yml", "repo-guard.yml"):
            self.assertNotIn("pull_request_target", open(os.path.join(ROOT, ".github", "workflows", name)).read())

    def test_gate_jobs_report_passed_last(self):
        for job in ("pr-hygiene", "verify-gate"):
            j = WF["jobs"][job]
            self.assertEqual(j["outputs"], {"passed": "${{ steps.passed.outputs.passed }}"})
            last = j["steps"][-1]
            self.assertEqual(last["id"], "passed")
            self.assertEqual(last["run"].strip(), 'echo "passed=%s" >>"$GITHUB_OUTPUT"' % job)
        out = WF.get("on", WF.get(True))["workflow_call"]["outputs"]["passed"]["value"]
        self.assertEqual(out, "${{ jobs.pr-hygiene.outputs.passed || jobs.verify-gate.outputs.passed }}")

    def test_verify_gate_runs_the_base_commit_guard(self):
        co = WF["jobs"]["verify-gate"]["steps"][0]
        self.assertTrue(co["uses"].startswith("actions/checkout@"))
        self.assertEqual(co["with"]["ref"], "${{ github.event.pull_request.base.sha }}")

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
