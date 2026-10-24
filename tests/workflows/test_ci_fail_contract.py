"""The ci-fail.yml contract that every per-repo caller depends on (vws-deploy#412).

Ported from the workflow tests of the superseded #421, adapted to the merged design: the
reusable workflow takes no inputs, and the caller only lists its workflows and grants permissions.

Run: python3 -m unittest discover -s tests/workflows
"""
import os
import re
import subprocess
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WF_PATH = os.path.join(ROOT, ".github", "workflows", "ci-fail.yml")
DOC_PATH = os.path.join(ROOT, "docs", "ci-fail.md")
JOB_PERMISSIONS = {"actions": "read", "contents": "read", "pull-requests": "write"}
CALLER_PATH = os.path.join(ROOT, ".github", "workflows", "ci-fail-caller.yml")
LABELS_PATH = os.path.join(ROOT, ".github", "labels.yml")
CI_FAIL_PY = os.path.join(ROOT, ".github", "ci-fail", "ci_fail.py")
# vws-deploy#432: the script must come only from the pinned checkout. A run: line that clones or downloads
# vws-deploy (a branch, raw file or tarball) would bypass the caller's @sha pin.
FETCH_RE = re.compile(r"(\bgit\b[^\n]*\b(clone|fetch|pull|archive)\b|\bgh\s+repo\s+clone\b|\bcurl\b|\bwget\b|"
                      r"raw\.githubusercontent\.com|codeload\.github\.com)[^\n]*vws-deploy", re.I)


def fetches_vws_deploy(run):
    """The run: text with line continuations joined, so a split command is still seen."""
    return bool(FETCH_RE.search(re.sub(r"\\\n\s*", " ", run or "")))


def run_steps(wf):
    return [s for job in (wf.get("jobs") or {}).values() for s in job.get("steps", []) if "run" in s]


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
            if "run" in s and s is self.job["steps"][0]:
                self.assertNotIn("GH_TOKEN", env)
                self.assertNotIn("token", s["run"].lower())
            elif "run" in s:
                self.assertEqual(env, {"GH_TOKEN": "${{ github.token }}"})
                self.assertNotIn("${{ github.event", s["run"], "event fields must not reach a shell line")
            else:
                self.assertNotIn("token", with_)
                self.assertNotIn("GH_TOKEN", env)


class PinnedSource(unittest.TestCase):
    """vws-deploy#432: fail closed without a pinned workflow commit; never fetch vws-deploy any other way."""

    def setUp(self):
        self.job = yaml.safe_load(open(WF_PATH))["jobs"]["marker"]
        self.first = self.job["steps"][0]

    def run_first(self, repo, sha):
        env = {"PATH": os.environ["PATH"]}
        env.update({k: v.replace("${{ job.workflow_repository }}", repo).replace("${{ job.workflow_sha }}", sha)
                    for k, v in (self.first.get("env") or {}).items()})
        return subprocess.run(["bash", "-c", self.first["run"]], env=env, capture_output=True, text=True)

    def test_first_step_checks_the_pin_before_checkout(self):
        self.assertIn("run", self.first)
        self.assertEqual(set((self.first.get("env") or {}).values()),
                         {"${{ job.workflow_repository }}", "${{ job.workflow_sha }}"})
        self.assertNotIn("${{", self.first["run"])

    def test_empty_or_bad_pin_fails_closed(self):
        good_repo, good_sha = "popododo0720/vws-deploy", "0123456789abcdef0123456789abcdef01234567"
        self.assertEqual(self.run_first(good_repo, good_sha).returncode, 0)
        for repo, sha in (("", good_sha), (good_repo, ""), ("", ""), (good_repo, "main"),
                          ("someone/vws-deploy", good_sha), (good_repo, good_sha[:39])):
            r = self.run_first(repo, sha)
            self.assertNotEqual(r.returncode, 0, (repo, sha))
            self.assertIn("ci-fail:", r.stderr)

    def test_the_fetch_guard_matches(self):
        for bad in ("git clone https://github.com/popododo0720/vws-deploy",
                    "gh repo clone popododo0720/vws-deploy x",
                    "curl -fsSL https://raw.githubusercontent.com/popododo0720/vws-deploy/main/.github/ci-fail/ci_fail.py",
                    "wget -q https://codeload.github.com/popododo0720/vws-deploy/tar.gz/main",
                    "git -C x fetch https://github.com/popododo0720/vws-deploy main",
                    "git clone \\\n  https://github.com/popododo0720/vws-deploy"):
            self.assertTrue(fetches_vws_deploy(bad), bad)
        for ok in ("python3 vws-deploy-ci-fail/.github/ci-fail/ci_fail.py", "git status", "curl -fsS https://example.com"):
            self.assertFalse(fetches_vws_deploy(ok), ok)

    def test_no_workflow_or_sample_caller_fetches_vws_deploy(self):
        for wf in (yaml.safe_load(open(WF_PATH)), yaml.safe_load(open(CALLER_PATH)), doc_caller()):
            for s in run_steps(wf):
                self.assertFalse(fetches_vws_deploy(s["run"]), s.get("name"))


class Labels(unittest.TestCase):
    def test_needs_human_is_defined(self):
        names = [x["name"] for x in yaml.safe_load(open(LABELS_PATH))]
        src = open(CI_FAIL_PY).read()
        label = re.search(r'^NEEDS_HUMAN = "([^"]+)"$', src, re.M).group(1)
        self.assertIn(label, names)


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
