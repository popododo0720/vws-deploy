"""Behavior tests for the shared repo-guard jobs, with gh and sleep stubbed.

The jobs live in .github/workflows/repo-guard-<job>.yml, one per file (vws-deploy#402); repo-guard.yml calls them.

Run: python3 -m unittest discover -s tests/workflows
"""
import os
import stat
import subprocess
import tempfile
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
JOBS = ("pr-hygiene", "verify-gate", "verify-reset", "verify-rerun")
FILES = {job: yaml.safe_load(open(os.path.join(ROOT, ".github", "workflows", f"repo-guard-{job}.yml"))) for job in JOBS}
WF = {"jobs": {job: FILES[job]["jobs"][job] for job in JOBS}}
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
    """vws-deploy#439 AC2 class 1: no event-snapshot labels; the DELETE is pinned to the pushed head."""
    HEAD = "a" * 40
    ENV = {"GH_TOKEN": "x", "REPO": "o/r", "NUMBER": "7", "HEAD_SHA": HEAD}

    def script(self):
        return step("verify-reset", "Remove verify-approved")["run"]

    def gh(self, delete, head=HEAD):
        return f'''case "$*" in
  *"-X DELETE"*) {delete} ;;
  *pulls/7*) echo {head} ;;
  *) exit 1 ;;
esac'''

    def test_no_event_snapshot(self):
        """A re-run replays the push-time labels; the step must not take them (the pre-fix LABELS env)."""
        env = step("verify-reset", "Remove verify-approved")["env"]
        self.assertNotIn("LABELS", env)
        self.assertEqual(env["HEAD_SHA"], "${{ github.event.pull_request.head.sha }}")

    def test_removed_even_when_the_event_snapshot_lacked_it(self):
        """Labels added after the push event (or missing from its snapshot) are still removed."""
        rc, calls, out = Stub(self, self.gh("exit 0")).run(self.script(), dict(self.ENV, LABELS="bug"))
        self.assertEqual(rc, 0, out)
        self.assertIn("-X DELETE repos/o/r/issues/7/labels/verify-approved", calls)

    def test_head_moved_writes_nothing(self):
        """A re-run of an older push: the run for the newer head owns the reset."""
        rc, calls, out = Stub(self, self.gh("exit 0", head="b" * 40)).run(self.script(), self.ENV)
        self.assertEqual(rc, 0, out)
        self.assertNotIn("DELETE", calls)
        self.assertIn("head moved", out)

    def test_unreadable_head_fails(self):
        rc, calls, _ = Stub(self, 'exit 1').run(self.script(), self.ENV)
        self.assertNotEqual(rc, 0)
        self.assertNotIn("DELETE", calls)

    def test_label_already_gone_is_ok(self):
        rc, _, out = Stub(self, self.gh('echo "gh: Label does not exist (HTTP 404)" >&2; exit 1')).run(self.script(), self.ENV)
        self.assertEqual(rc, 0, out)
        self.assertIn("already removed", out)

    def test_other_error_fails(self):
        rc, _, _ = Stub(self, self.gh('echo "gh: Server Error (HTTP 500)" >&2; exit 1')).run(self.script(), self.ENV)
        self.assertNotEqual(rc, 0)


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
    PR = '{"head": {"sha": "abc123", "ref": "feat", "repo": {"full_name": "o/r"}}}'
    FORK_PR = '{"head": {"sha": "abc123", "ref": "feat", "repo": {"full_name": "fork/r"}}}'
    # Newest first, as the API lists them: another PR (#8) with the same head SHA has the newest run.
    RUNS = ('{"workflow_runs": ['
            '{"id": 66, "pull_requests": [{"number": 8}], "head_repository": {"full_name": "o/r"}, "head_branch": "other"},'
            '{"id": 77, "pull_requests": [], "head_repository": {"full_name": "fork/r"}, "head_branch": "x"},'
            '{"id": 55, "pull_requests": [{"number": 7}], "head_repository": {"full_name": "o/r"}, "head_branch": "feat"}]}')
    FORK_RUNS = ('{"workflow_runs": ['
                 '{"id": 66, "pull_requests": [{"number": 8}], "head_repository": {"full_name": "o/r"}, "head_branch": "feat"},'
                 '{"id": 88, "pull_requests": [], "head_repository": {"full_name": "fork/r"}, "head_branch": "other"},'
                 '{"id": 55, "pull_requests": [], "head_repository": {"full_name": "fork/r"}, "head_branch": "feat"}]}')

    def gh(self, busy=0, rerun="exit 0", pr=PR, runs=RUNS, jobs="echo 99"):
        """busy: how many status reads say in_progress. rerun: the POST's body, given the job id in $job."""
        return f"""count() {{ n=$(cat "$0.$1" 2>/dev/null || echo 0); echo $((n + 1)) >"$0.$1"; echo "$n"; }}
case "$*" in
  "api repos/o/r/pulls/7") echo '{pr}' ;;
  *workflows/repo-guard.yml/runs*) echo '{runs}' ;;
  *runs/55/jobs*) {jobs} ;;
  "api repos/o/r/actions/jobs/"*" --jq .status")
     if [ "$(count status)" -lt {busy} ]; then echo in_progress; else echo completed; fi ;;
  *"-X POST"*) job=${{4#repos/o/r/actions/jobs/}}; job=${{job%/rerun}}; {rerun} ;;
  *"-X DELETE"*) exit 0 ;;
  *) exit 1 ;;
esac"""

    def go(self, **kw):
        return Stub(self, self.gh(**kw)).run(self.script(), self.ENV)

    def script(self):
        return step("verify-rerun", "Re-run verify-gate for the PR head")["run"]

    def test_triggers(self):
        self.assertEqual(sorted(ON["issue_comment"]["types"]), ["created", "deleted", "edited"])
        for job in ("pr-hygiene", "verify-gate"):
            self.assertIn("github.event_name == 'pull_request'", WF["jobs"][job]["if"])
        self.assertIn("issue_comment", WF["jobs"]["verify-rerun"]["if"])
        self.assertEqual(WF["jobs"]["verify-rerun"]["permissions"],
                         {"actions": "write", "issues": "write", "pull-requests": "write"})

    def test_reruns_the_called_gate_job_not_the_shim(self):
        # The caller's bare "verify-gate" shim only reads the result of "gate / verify-gate"; re-running the
        # shim alone would replay the old result, so the called job is the one re-run.
        rc, calls, out = self.go()
        self.assertEqual(rc, 0, out)
        self.assertIn('.name == "gate / verify-gate"', calls)

    def test_waits_for_running_job_then_reruns(self):
        rc, calls, out = self.go(busy=3)
        self.assertEqual(rc, 0, out)
        self.assertIn("-X POST repos/o/r/actions/jobs/99/rerun", calls)
        self.assertIn("head_sha=abc123", calls)
        self.assertNotIn("DELETE", calls)

    def test_two_prs_with_the_same_head_sha(self):
        """vws-deploy#312 AC1: the newest run for the SHA is PR #8's; PR #7's run is the one re-run."""
        rc, calls, out = self.go()
        self.assertEqual(rc, 0, out)
        self.assertIn("runs/55/jobs", calls)
        self.assertNotIn("runs/66", calls)

    def test_fork_pr_picks_its_own_head_branch(self):
        """A fork PR's runs list no pull_requests: the run from its head repo and branch, not the newest."""
        rc, calls, out = self.go(pr=self.FORK_PR, runs=self.FORK_RUNS)
        self.assertEqual(rc, 0, out)
        self.assertIn("runs/55/jobs", calls)
        self.assertNotIn("runs/66", calls)
        self.assertNotIn("runs/88", calls)

    def test_no_run_of_this_pr_drops_the_label(self):
        """Only another PR's run has the SHA: nothing is re-run, and the label goes."""
        runs = '{"workflow_runs": [{"id": 66, "pull_requests": [{"number": 8}], "head_repository": {"full_name": "o/r"}, "head_branch": "feat"}]}'
        rc, calls, out = self.go(runs=runs)
        self.assertNotEqual(rc, 0)
        self.assertNotIn("runs/66", calls)
        self.assertNotIn("POST", calls)
        self.assertIn("-X DELETE repos/o/r/issues/7/labels/verify-approved", calls)

    def test_quote_in_a_fork_branch_is_data(self):
        pr = '{"head": {"sha": "abc123", "ref": "a\\\" or true or \\\"", "repo": {"full_name": "fork/r"}}}'
        rc, calls, out = self.go(pr=pr, runs=self.FORK_RUNS)
        self.assertNotEqual(rc, 0, out)
        self.assertNotIn("/jobs", calls)
        self.assertIn("DELETE", calls)

    def test_gives_up_and_drops_the_label(self):
        """vws-deploy#312 AC2: the wait window ran out, so the label goes and the log says so."""
        rc, calls, out = self.go(busy=1000)
        self.assertNotEqual(rc, 0)
        self.assertNotIn("POST", calls)
        self.assertIn("-X DELETE repos/o/r/issues/7/labels/verify-approved", calls)
        self.assertIn("could not be re-run within 15 minutes", out)
        self.assertIn("verify-approved removed", out)

    def test_past_the_rerun_window_drops_the_label_at_once(self):
        """vws-deploy#312 AC2: GitHub refuses the re-run (HTTP 403); waiting cannot change that."""
        msg = 'echo "gh: Unable to re-run this workflow run because it was created over a month ago (HTTP 403)" >&2; exit 1'
        rc, calls, out = self.go(rerun=msg)
        self.assertNotEqual(rc, 0)
        self.assertEqual(calls.count("-X POST"), 1)
        self.assertIn("-X DELETE repos/o/r/issues/7/labels/verify-approved", calls)
        self.assertIn("created over a month ago", out)
        self.assertIn("verify-approved removed", out)

    def test_label_already_gone_still_fails_the_job(self):
        stub = self.gh(busy=1000).replace('*"-X DELETE"*) exit 0', '*"-X DELETE"*) echo "gh: Label does not exist (HTTP 404)" >&2; exit 1')
        rc, _, out = Stub(self, stub).run(self.script(), self.ENV)
        self.assertNotEqual(rc, 0)
        self.assertIn("verify-approved was not set", out)

    def test_newer_attempt_already_ran(self):
        """vws-proto run 41954745801: another re-run made attempt 2 (job 98) after job 99 was read. Job 99 is
        refused with "Only jobs from the current attempt can be re-run"; the newest attempt's job is re-run and
        the job passes instead of retrying the stale id for 15 minutes."""
        jobs = 'if [ "$(count jobs)" -lt 1 ]; then echo 99; else echo 98; fi'
        refuse = ('if [ "$job" = 99 ]; then echo "gh: Only jobs from the current attempt can be re-run (HTTP 403)" >&2; '
                  'exit 1; fi; exit 0')
        rc, calls, out = self.go(jobs=jobs, rerun=refuse)
        self.assertEqual(rc, 0, out)
        self.assertIn("-X POST repos/o/r/actions/jobs/98/rerun", calls)
        self.assertEqual(calls.count("-X POST repos/o/r/actions/jobs/99/rerun"), 1)
        self.assertNotIn("DELETE", calls)

    def test_run_already_running_waits_then_reruns(self):
        """vws-deploy#721: on #720 the re-run got HTTP 403 "already running" (the run the verdict's label event
        started) and the label was dropped. Now the job waits for the run and re-runs the gate; the label stays."""
        refuse = ('if [ "$(count post)" -lt 2 ]; then echo "gh: The workflow run containing this job is already '
                  'running (HTTP 403)" >&2; exit 1; fi; exit 0')
        rc, calls, out = self.go(rerun=refuse)
        self.assertEqual(rc, 0, out)
        self.assertEqual(calls.count("-X POST repos/o/r/actions/jobs/99/rerun"), 3)
        self.assertNotIn("DELETE", calls)
        self.assertIn("still running; waiting to re-run it", out)

    def test_run_running_past_the_window_drops_the_label(self):
        """vws-deploy#721: a run that stays running for the whole 15-minute window still drops the label."""
        refuse = 'echo "gh: The workflow run containing this job is already running (HTTP 403)" >&2; exit 1'
        rc, calls, out = self.go(rerun=refuse)
        self.assertNotEqual(rc, 0)
        self.assertEqual(calls.count("-X POST"), 30)
        self.assertIn("-X DELETE repos/o/r/issues/7/labels/verify-approved", calls)
        self.assertIn("could not be re-run within 15 minutes", out)

    def test_failed_lookup_drops_the_label(self):
        """A lookup that fails (here the jobs list, HTTP 502) leaves no stale approval behind."""
        rc, calls, out = self.go(jobs='echo "gh: Server Error (HTTP 502)" >&2; exit 1')
        self.assertNotEqual(rc, 0)
        self.assertNotIn("POST", calls)
        self.assertIn("-X DELETE repos/o/r/issues/7/labels/verify-approved", calls)
        self.assertIn("lookup failed", out)

    def test_server_error_is_retried(self):
        flaky = 'if [ "$(count post)" -lt 2 ]; then echo "gh: Server Error (HTTP 502)" >&2; exit 1; fi; exit 0'
        rc, calls, out = self.go(rerun=flaky)
        self.assertEqual(rc, 0, out)
        self.assertEqual(calls.count("-X POST"), 3)
        self.assertNotIn("DELETE", calls)


class Reusable(unittest.TestCase):
    """Each job file runs only on workflow_call, holds only its own job and never sees pull_request_target."""

    def test_only_workflow_call_and_one_job(self):
        for job, wf in FILES.items():
            on = wf.get("on", wf.get(True))
            self.assertEqual(list(on), ["workflow_call"], job)
            self.assertNotIn("inputs", on["workflow_call"] or {}, job)
            self.assertNotIn("secrets", on["workflow_call"] or {}, job)
            self.assertEqual(wf["permissions"], {}, job)
            self.assertEqual(list(wf["jobs"]), [job])

    def test_no_job_input_left(self):
        for job, wf in FILES.items():
            self.assertNotIn("inputs.", str(wf), job)

    def test_no_pull_request_target(self):
        names = ["repo-guard.yml"] + [f"repo-guard-{job}.yml" for job in JOBS]
        for name in names:
            self.assertNotIn("pull_request_target", open(os.path.join(ROOT, ".github", "workflows", name)).read())

    def test_gate_jobs_report_passed_last(self):
        for job in ("pr-hygiene", "verify-gate"):
            j = WF["jobs"][job]
            self.assertEqual(j["outputs"], {"passed": "${{ steps.passed.outputs.passed }}"})
            last = j["steps"][-1]
            self.assertEqual(last["id"], "passed")
            self.assertEqual(last["run"].strip(), 'echo "passed=%s" >>"$GITHUB_OUTPUT"' % job)
            out = FILES[job].get("on", FILES[job].get(True))["workflow_call"]["outputs"]["passed"]["value"]
            self.assertEqual(out, "${{ jobs.%s.outputs.passed }}" % job)

    def test_verify_gate_runs_the_base_commit_guard(self):
        co = WF["jobs"]["verify-gate"]["steps"][0]
        self.assertTrue(co["uses"].startswith("actions/checkout@"))
        self.assertEqual(co["with"]["ref"], "${{ github.event.pull_request.base.sha }}")


if __name__ == "__main__":
    unittest.main()
