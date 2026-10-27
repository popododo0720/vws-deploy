"""Rate-limit retry and the verify-gate / verify-queue call cut (vws-deploy#573).

The helper is .github/gh-api-retry.sh. Workflows inline it because verify-gate checks out the caller
base and the other jobs check out nothing. gh is a fake: no network.

Run: python3 -m unittest discover -s tests/workflows
"""
import json
import os
import stat
import subprocess
import tempfile
import textwrap
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HELPER = os.path.join(ROOT, ".github", "gh-api-retry.sh")
SHA = "a" * 40


def marker(text):
    start = text.index("# BEGIN gh-api-retry\n")
    end = text.index("# END gh-api-retry\n") + len("# END gh-api-retry\n")
    return text[start:end]


def run_script(step_run):
    """bash -n, then the script is returned for callers that execute it."""
    proc = subprocess.run(["bash", "-n"], input=step_run, text=True, capture_output=True)
    return proc


class Helper(unittest.TestCase):
    def go(self, gh_body, argv, env=None):
        d = tempfile.mkdtemp()
        self.addCleanup(subprocess.run, ["rm", "-rf", d])
        gh = os.path.join(d, "gh")
        with open(gh, "w") as f:
            f.write("#!/usr/bin/env bash\n" + gh_body)
        os.chmod(gh, os.stat(gh).st_mode | stat.S_IEXEC)
        sleep = os.path.join(d, "sleep")
        with open(sleep, "w") as f:
            f.write("#!/usr/bin/env bash\nexit 0\n")
        os.chmod(sleep, os.stat(sleep).st_mode | stat.S_IEXEC)
        script = 'set -euo pipefail\nsource %s\ngh_api_retry "$@"\n' % json.dumps(HELPER)
        e = dict(os.environ, PATH=d + ":" + os.environ["PATH"])
        if env:
            e.update(env)
        proc = subprocess.run(["bash", "-c", script, "bash", *argv], env=e, capture_output=True, text=True)
        return proc.returncode, proc.stdout, proc.stderr

    def test_installation_limit_then_success_drops_the_error_body(self):
        gh = textwrap.dedent("""\
            n=$(cat "$0.n" 2>/dev/null || echo 0)
            echo $((n + 1)) >"$0.n"
            if [ "$n" -lt 2 ]; then
              echo "gh: API rate limit exceeded for installation ID 99. (HTTP 403)" >&2
              printf '%s\\n' '{"message":"API rate limit exceeded for installation ID 99."}'
              exit 1
            fi
            printf '%s\\n' '{"ok":true}'
            """)
        rc, out, err = self.go(gh, ["repos/o/r/issues/1/labels"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, '{"ok":true}\n')
        self.assertNotIn("installation ID 99.", out)
        self.assertIn("retry 1/4", err)
        self.assertIn("retry 2/4", err)

    def test_budget_exhausted_fails_closed(self):
        gh = textwrap.dedent("""\
            echo "gh: API rate limit exceeded for installation ID 99. (HTTP 403)" >&2
            exit 1
            """)
        rc, out, err = self.go(gh, ["repos/o/r/x"], {"GH_API_RETRY_ATTEMPTS": "3"})
        self.assertNotEqual(rc, 0)
        self.assertEqual(out, "")
        # The last error is printed once. Two retry lines means three attempts, then fail closed.
        self.assertIn("API rate limit exceeded for installation", err)
        self.assertIn("retry 1/2", err)
        self.assertIn("retry 2/2", err)

    def test_permission_403_is_not_retried(self):
        gh = textwrap.dedent("""\
            echo "gh: Resource not accessible by integration (HTTP 403)" >&2
            exit 1
            """)
        rc, out, err = self.go(gh, ["repos/o/r/x"])
        self.assertNotEqual(rc, 0)
        self.assertEqual(err.count("HTTP 403"), 1)
        self.assertNotIn("retry", err)

    def test_graphql_rate_limited_document_retries(self):
        gh = textwrap.dedent("""\
            n=$(cat "$0.n" 2>/dev/null || echo 0)
            echo $((n + 1)) >"$0.n"
            if [ "$n" -lt 1 ]; then
              printf '%s\\n' '{"errors":[{"type":"RATE_LIMITED","message":"API rate limit exceeded"}]}'
              exit 0
            fi
            printf '%s\\n' '{"data":{"ok":true}}'
            """)
        rc, out, err = self.go(gh, ["graphql", "-f", "query={}"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, '{"data":{"ok":true}}\n')
        self.assertIn("retry 1/4", err)

    def test_secondary_limit_retries(self):
        gh = textwrap.dedent("""\
            n=$(cat "$0.n" 2>/dev/null || echo 0)
            echo $((n + 1)) >"$0.n"
            if [ "$n" -lt 1 ]; then
              echo "gh: You have exceeded a secondary rate limit. (HTTP 403)" >&2
              exit 1
            fi
            printf '%s\\n' ok
            """)
        rc, out, err = self.go(gh, ["repos/o/r/x"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, "ok\n")
        self.assertIn("retry 1/4", err)


class Inlined(unittest.TestCase):
    def test_workflows_inline_the_canonical_helper(self):
        want = marker(open(HELPER, encoding="utf-8").read())
        names = [
            "repo-guard-verify-gate.yml",
            "repo-guard-verify-reset.yml",
            "repo-guard-verify-rerun.yml",
            "verify-queue.yml",
        ]
        for name in names:
            wf = yaml.safe_load(open(os.path.join(ROOT, ".github", "workflows", name), encoding="utf-8"))
            runs = [s.get("run", "") for job in wf["jobs"].values() for s in job["steps"]]
            blob = "\n".join(runs)
            self.assertIn(want, blob, name)

    def test_run_scripts_are_valid_bash(self):
        names = [
            "repo-guard-verify-gate.yml",
            "repo-guard-verify-reset.yml",
            "repo-guard-verify-rerun.yml",
            "verify-queue.yml",
        ]
        for name in names:
            wf = yaml.safe_load(open(os.path.join(ROOT, ".github", "workflows", name), encoding="utf-8"))
            for job in wf["jobs"].values():
                for step in job["steps"]:
                    if "run" not in step:
                        continue
                    proc = run_script(step["run"])
                    self.assertEqual(proc.returncode, 0, "%s: %s" % (name, proc.stderr))


def verdict_body(sha):
    lines = ["## Verified-by: vws-verify", "Head: %s" % sha]
    lines.extend("%d. https://example.invalid/log/%d" % (i, i) for i in range(1, 10))
    lines.append("Codex review: /workspace/handoff/codex/vws-deploy-1-aaaaaaa.md blocking=0")
    return "\n".join(lines)


FAKE_GH = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
log = Path(os.environ["GH_LOG"])
with log.open("a") as fh:
    fh.write(json.dumps(args) + "\n")
scenario = json.loads(Path(os.environ["GH_SCENARIO"]).read_text())
blob = "\n".join(args)

def kind():
    if "graphql" in args:
        if "vg-comments" in blob:
            return "comments"
        if "vg-labels" in blob:
            return "labels-gql"
        if "vg-bundle" in blob:
            return "bundle"
        return "graphql"
    if "/activity" in blob or blob.endswith("activity") or "/activity" in "".join(args):
        return "activity"
    if any("/labels" in a and "graphql" not in args for a in args):
        return "labels-rest"
    return "other"

k = kind()
state_path = Path(os.environ["GH_STATE"])
state = json.loads(state_path.read_text()) if state_path.exists() else {}
spec = (scenario.get("fail") or {}).get(k)
used = state.get(k, 0)
if spec and used < spec["times"]:
    state[k] = used + 1
    state_path.write_text(json.dumps(state))
    sys.stderr.write(spec.get("message", "gh: API rate limit exceeded for installation ID 99. (HTTP 403)\n"))
    if not spec.get("message", "").endswith("\n"):
        sys.stderr.write("\n")
    if spec.get("stdout"):
        sys.stdout.write(spec["stdout"])
    sys.exit(spec.get("code", 1))

def flag(name):
    for a in args:
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None

def emit(payload):
    raw = json.dumps(payload)
    if "--jq" in args:
        expr = args[args.index("--jq") + 1]
        proc = subprocess.run(["jq", "-r", expr], input=raw, text=True, capture_output=True)
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        sys.exit(proc.returncode)
    sys.stdout.write(raw)
    sys.exit(0)

def nodes(page):
    return page.get("nodes") or []

if k == "labels-rest":
    emit([{"name": n} for n in scenario.get("labels", [])])
if k == "activity":
    emit([{"after": a["after"], "actor": {"login": a["login"]}} for a in scenario.get("activity", [])])
if k == "bundle":
    comments = (scenario.get("comment_pages") or [{"nodes": [], "hasNextPage": False}])[0]
    labels = (scenario.get("label_pages") or [{"nodes": [], "hasPreviousPage": False}])[0]
    login = scenario.get("commit_login", "")
    obj = None if scenario.get("missing_commit") else {"committer": {"user": {"login": login}}}
    emit({"data": {"repository": {
        "pullRequest": {
            "comments": {"pageInfo": {"hasNextPage": bool(comments.get("hasNextPage")), "endCursor": comments.get("endCursor")},
                         "nodes": nodes(comments)},
            "timelineItems": {"pageInfo": {"hasPreviousPage": bool(labels.get("hasPreviousPage")),
                                           "startCursor": labels.get("startCursor")},
                              "nodes": nodes(labels)},
        },
        "object": obj,
    }}})
if k == "comments":
    cur = flag("cursor")
    page = next(p for p in scenario.get("comment_pages", []) if p.get("when") == cur)
    emit({"data": {"repository": {"pullRequest": {
        "comments": {"pageInfo": {"hasNextPage": bool(page.get("hasNextPage")), "endCursor": page.get("endCursor")},
                     "nodes": nodes(page)}}}}})
if k == "labels-gql":
    cur = flag("cursor")
    page = next(p for p in scenario.get("label_pages", []) if p.get("when") == cur)
    emit({"data": {"repository": {"pullRequest": {
        "timelineItems": {"pageInfo": {"hasPreviousPage": bool(page.get("hasPreviousPage")),
                                       "startCursor": page.get("startCursor")},
                          "nodes": nodes(page)}}}}})
sys.stderr.write("unexpected gh " + blob + "\n")
sys.exit(9)
'''


class VerifyGate(unittest.TestCase):
    """The gate step, with gh faked. Counts calls and checks the guard script still judges."""

    def gate(self):
        wf = yaml.safe_load(open(os.path.join(ROOT, ".github", "workflows", "repo-guard-verify-gate.yml"), encoding="utf-8"))
        return wf["jobs"]["verify-gate"]["steps"][1]["run"]

    def go(self, scenario, env=None):
        d = tempfile.mkdtemp()
        self.addCleanup(subprocess.run, ["rm", "-rf", d])
        gh = os.path.join(d, "gh")
        with open(gh, "w") as f:
            f.write(FAKE_GH)
        os.chmod(gh, os.stat(gh).st_mode | stat.S_IEXEC)
        sleep = os.path.join(d, "sleep")
        with open(sleep, "w") as f:
            f.write("#!/usr/bin/env bash\nexit 0\n")
        os.chmod(sleep, os.stat(sleep).st_mode | stat.S_IEXEC)
        log = os.path.join(d, "gh.log")
        state = os.path.join(d, "state.json")
        scen = os.path.join(d, "scenario.json")
        with open(scen, "w") as f:
            json.dump(scenario, f)
        e = dict(os.environ, PATH=d + ":" + os.environ["PATH"], GH_LOG=log, GH_STATE=state, GH_SCENARIO=scen,
                 GH_TOKEN="x", REPO="o/r", NUMBER="7", HEAD_SHA=SHA, HEAD_REPO="o/r", HEAD_REF="topic",
                 PR_AUTHOR="alice", GUARD_VERIFY_ACTORS="verifier", GUARD_VERIFY_IDENTITY="verifier",
                 GH_API_RETRY_DELAY="1")
        if env:
            e.update(env)
        proc = subprocess.run(["bash", "-c", self.gate()], cwd=ROOT, env=e, capture_output=True, text=True)
        calls = open(log).read() if os.path.exists(log) else ""
        return proc.returncode, calls, proc.stdout + proc.stderr

    def test_no_label_is_one_read_and_does_not_touch_activity(self):
        # main always read labels, comments, events, activity and the commit (5) before failing.
        rc, calls, out = self.go({"labels": ["bug"]})
        self.assertNotEqual(rc, 0)
        self.assertIn("no 'verify-approved' label", out)
        self.assertEqual(calls.count("/labels"), 1, calls)
        self.assertNotIn("activity", calls)
        self.assertNotIn("vg-bundle", calls)
        self.assertNotIn("/events", calls)
        self.assertNotIn("/commits/", calls)

    def test_approved_is_three_reads_and_passes(self):
        rc, calls, out = self.go(self.approved())
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.ncalls(calls), 3, calls)
        self.assertIn("vg-bundle", calls)
        self.assertNotIn("vg-comments", calls)
        self.assertNotIn("vg-labels", calls)
        self.assertNotIn("/events", calls)
        self.assertNotIn("/commits/", calls)
        self.assertEqual(calls.count("/activity"), 1)

    def test_activity_rate_limit_then_succeeds(self):
        # vws-deploy#572: the activity read returned 403 "API rate limit exceeded for installation"
        # and the job exited 1 before the guard script ran.
        scenario = self.approved()
        scenario["fail"] = {"activity": {"times": 2}}
        rc, calls, out = self.go(scenario)
        self.assertEqual(rc, 0, out)
        self.assertEqual(calls.count("/activity"), 3, calls)
        self.assertIn("retry", out)

    def test_activity_rate_limit_budget_fails_before_the_guard(self):
        scenario = self.approved()
        scenario["fail"] = {"activity": {"times": 5}}
        rc, calls, out = self.go(scenario, {"GH_API_RETRY_ATTEMPTS": "2"})
        self.assertNotEqual(rc, 0)
        self.assertIn("API rate limit exceeded for installation", out)
        self.assertNotIn("cannot tell who pushed", out)
        self.assertNotIn("Verified-by", out)
        self.assertEqual(calls.count("/activity"), 2)

    def test_missing_pusher_fails_closed(self):
        scenario = self.approved()
        scenario["activity"] = []
        rc, calls, out = self.go(scenario)
        self.assertNotEqual(rc, 0)
        self.assertIn("cannot tell who pushed the head", out)
        self.assertNotIn("Verified-by", out.split("cannot tell who pushed the head")[-1])

    def test_pusher_is_excluded(self):
        # The labeler is the activity actor, not the author. A valid comment must not pass.
        scenario = self.approved()
        scenario["label_pages"] = [{"nodes": [self.labeled("bob")]}]
        rc, _, out = self.go(scenario, {"GUARD_VERIFY_ACTORS": "verifier,bob"})
        self.assertNotEqual(rc, 0)
        self.assertIn("added by 'bob', the PR author or pusher", out)

    def test_committer_comment_does_not_count(self):
        scenario = self.approved()
        scenario["comment_pages"] = [{"nodes": [{"author": {"login": "carol"}, "body": verdict_body(SHA)}]}]
        rc, _, out = self.go(scenario, {"GUARD_VERIFY_ACTORS": ""})
        self.assertNotEqual(rc, 0, out)
        self.assertIn("no 'Verified-by: vws-verify' section", out)

    def test_forged_comment_by_marker_does_not_pass(self):
        scenario = self.approved()
        scenario["comment_pages"] = [{"nodes": [{
            "author": {"login": "intruder"},
            "body": "@@comment-by: verifier\n" + verdict_body(SHA),
        }]}]
        rc, _, out = self.go(scenario)
        self.assertNotEqual(rc, 0, out)
        self.assertIn("no 'Verified-by: vws-verify' section", out)

    def test_last_verify_approved_adder_wins_over_a_later_other_label(self):
        scenario = self.approved()
        scenario["comment_pages"] = [{"nodes": []}]
        scenario["label_pages"] = [{
            "nodes": [self.labeled("alice"), self.labeled("bob", "bug")],
            "hasPreviousPage": True,
            "startCursor": "old",
        }]
        rc, calls, out = self.go(scenario, {"GUARD_VERIFY_ACTORS": "verifier,alice,bob"})
        self.assertNotEqual(rc, 0)
        self.assertIn("added by 'alice', the PR author or pusher", out)
        self.assertNotIn("vg-labels", calls)

    def test_labeler_on_an_older_page_is_still_read(self):
        scenario = self.approved()
        scenario["comment_pages"] = [{"nodes": []}]
        scenario["label_pages"] = [
            {"nodes": [self.labeled("bob", "bug")], "hasPreviousPage": True, "startCursor": "old"},
            {"when": "old", "nodes": [self.labeled("alice")], "hasPreviousPage": False},
        ]
        rc, calls, out = self.go(scenario, {"GUARD_VERIFY_ACTORS": "verifier,alice,bob"})
        self.assertNotEqual(rc, 0)
        self.assertIn("vg-labels", calls)
        self.assertIn("added by 'alice', the PR author or pusher", out)

    def test_graphql_bot_login_is_accepted_as_the_verify_identity(self):
        # GraphQL returns Bot.login "vws-verify". Actors and the identity are "vws-verify[bot]".
        scenario = self.approved()
        scenario["comment_pages"] = [{"nodes": [{
            "author": {"__typename": "Bot", "login": "vws-verify"},
            "body": verdict_body(SHA),
        }]}]
        scenario["label_pages"] = [{"nodes": [{
            "actor": {"__typename": "Bot", "login": "vws-verify"},
            "label": {"name": "verify-approved"},
        }]}]
        rc, _, out = self.go(scenario, {
            "GUARD_VERIFY_ACTORS": "vws-verify[bot]",
            "GUARD_VERIFY_IDENTITY": "vws-verify[bot]",
        })
        self.assertEqual(rc, 0, out)

    def test_user_login_is_not_given_a_bot_suffix(self):
        scenario = self.approved()
        scenario["comment_pages"] = [{"nodes": [{
            "author": {"__typename": "User", "login": "vws-verify"},
            "body": verdict_body(SHA),
        }]}]
        scenario["label_pages"] = [{"nodes": [{
            "actor": {"__typename": "User", "login": "vws-verify"},
            "label": {"name": "verify-approved"},
        }]}]
        rc, _, out = self.go(scenario, {
            "GUARD_VERIFY_ACTORS": "vws-verify[bot]",
            "GUARD_VERIFY_IDENTITY": "vws-verify[bot]",
        })
        self.assertNotEqual(rc, 0)
        self.assertIn("added by 'vws-verify', not by one of", out)

    def test_newer_comment_page_replaces_an_older_verdict(self):
        scenario = self.approved()
        scenario["comment_pages"] = [
            {"nodes": [{"author": {"login": "verifier"}, "body": verdict_body(SHA)}],
             "hasNextPage": True, "endCursor": "c1"},
            {"when": "c1", "nodes": [{"author": {"login": "verifier"}, "body":
              "## Verified-by: vws-verify\nHead: %s\n1. withdrawn\n" % SHA}], "hasNextPage": False},
        ]
        rc, calls, out = self.go(scenario)
        self.assertNotEqual(rc, 0, out)
        self.assertIn("vg-comments", calls)
        self.assertIn("no evidence", out)

    def approved(self):
        return {
            "labels": ["bug", "verify-approved"],
            "comment_pages": [{"nodes": [{"author": {"login": "verifier"}, "body": verdict_body(SHA)}]}],
            "label_pages": [{"nodes": [self.labeled("verifier")]}],
            "commit_login": "carol",
            "activity": [{"after": SHA, "login": "bob"}],
        }

    @staticmethod
    def labeled(login, name="verify-approved"):
        return {"actor": {"login": login}, "label": {"name": name}}

    @staticmethod
    def ncalls(calls):
        return sum(1 for line in calls.splitlines() if line.startswith("["))


class RerunAndReset(unittest.TestCase):
    def script(self, filename, step_name):
        wf = yaml.safe_load(open(os.path.join(ROOT, ".github", "workflows", filename), encoding="utf-8"))
        job = next(iter(wf["jobs"].values()))
        return next(s["run"] for s in job["steps"] if s.get("name") == step_name)

    def go(self, script, gh_body, env):
        d = tempfile.mkdtemp()
        self.addCleanup(subprocess.run, ["rm", "-rf", d])
        gh = os.path.join(d, "gh")
        with open(gh, "w") as f:
            f.write("#!/usr/bin/env bash\n" + gh_body)
        os.chmod(gh, os.stat(gh).st_mode | stat.S_IEXEC)
        sleep = os.path.join(d, "sleep")
        with open(sleep, "w") as f:
            f.write("#!/usr/bin/env bash\nexit 0\n")
        os.chmod(sleep, os.stat(sleep).st_mode | stat.S_IEXEC)
        log = os.path.join(d, "gh.log")
        # The body is appended by the gh script itself via GH_LOG.
        e = dict(os.environ, PATH=d + ":" + os.environ["PATH"], GH_LOG=log, **env)
        proc = subprocess.run(["bash", "-c", script], env=e, capture_output=True, text=True)
        calls = open(log).read() if os.path.exists(log) else ""
        return proc.returncode, calls, proc.stdout + proc.stderr

    def test_reset_retries_an_installation_limit_on_the_head_read(self):
        gh = textwrap.dedent("""\
            echo "$*" >>"$GH_LOG"
            n=$(cat "$0.n" 2>/dev/null || echo 0)
            echo $((n + 1)) >"$0.n"
            if [ "$n" -lt 1 ]; then
              echo "gh: API rate limit exceeded for installation ID 99. (HTTP 403)" >&2
              exit 1
            fi
            case "$*" in
              *pulls/7*) echo %s ;;
              *"-X DELETE"*) exit 0 ;;
              *) echo "unexpected $*" >&2; exit 9 ;;
            esac
            """) % SHA
        rc, calls, out = self.go(
            self.script("repo-guard-verify-reset.yml", "Remove verify-approved"),
            gh, {"GH_TOKEN": "x", "REPO": "o/r", "NUMBER": "7", "HEAD_SHA": SHA, "GH_API_RETRY_DELAY": "1"})
        self.assertEqual(rc, 0, out)
        self.assertIn("verify-approved removed", out)
        self.assertGreaterEqual(calls.count("pulls/7"), 2)

    def test_rerun_retries_an_installation_limit_on_the_job_poll(self):
        gh = textwrap.dedent("""\
            echo "$*" >>"$GH_LOG"
            case "$*" in
              *pulls/7*) echo abc ;;
              *workflows/repo-guard.yml/runs*) echo 55 ;;
              *runs/55/jobs*) echo 99 ;;
              *jobs/99*--jq*.status|*jobs/99*--jq*'.status'*)
                n=$(cat "$0.n" 2>/dev/null || echo 0)
                echo $((n + 1)) >"$0.n"
                if [ "$n" -lt 1 ]; then
                  echo "gh: API rate limit exceeded for installation ID 99. (HTTP 403)" >&2
                  exit 1
                fi
                echo completed ;;
              *jobs/99/rerun*) exit 0 ;;
              *) echo "unexpected $*" >&2; exit 9 ;;
            esac
            """)
        rc, calls, out = self.go(
            self.script("repo-guard-verify-rerun.yml", "Re-run verify-gate for the PR head"),
            gh, {"GH_TOKEN": "x", "REPO": "o/r", "NUMBER": "7", "GH_API_RETRY_DELAY": "1"})
        self.assertEqual(rc, 0, out)
        self.assertIn("verify-gate job 99 re-run", out)
        self.assertIn("jobs/99/rerun", calls)
        self.assertGreaterEqual(calls.count("jobs/99"), 2)


if __name__ == "__main__":
    unittest.main()
