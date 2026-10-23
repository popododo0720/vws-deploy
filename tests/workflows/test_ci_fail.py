"""Behavior tests for the CI-failure marker (vws-deploy#412).

Run: python3 -m unittest discover -s tests/workflows
The tests load .github/ci-fail/ci_fail.py and use an in-memory GitHub, so no network or token.
"""
import datetime as dt
import importlib.util
import json
import os
import re
import unittest
import unittest.mock
import urllib.error

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(ROOT, ".github", "ci-fail", "ci_fail.py")
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "ci-fail.yml")
CALLER = os.path.join(ROOT, ".github", "ci-fail", "caller.yml")
LABELS = os.path.join(ROOT, ".github", "labels.yml")

spec = importlib.util.spec_from_file_location("ci_fail", SCRIPT)
cf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cf)

NOW = dt.datetime(2026, 10, 23, 5, 40, 12, tzinfo=dt.timezone.utc)
SHA = "9ef2b4d056eb4e983cfe431ca9b5bdad4e1f0193"
OTHER = ["a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
         "0c1d2e3f405162738495a6b7c8d9e0f123456789",
         "4e5f6a708192a3b4c5d6e7f8091a2b3c4d5e6f70"]


def record(number=113, sha=SHA, chain=(113,), n=1, wake=True, state="failing"):
    return {"v": 1, "repo": "vws-storage", "pr": number, "head_sha": sha, "head_branch": "fix/x-v2",
            "owner": "deepseek", "owner_source": "label:deepseek", "chain": list(chain), "n": n,
            "cap": 3, "wake": wake, "state": state, "runs": [], "failed_jobs": [],
            "created_at": cf.rfc3339(NOW), "updated_at": cf.rfc3339(NOW)}


def marker_comment(number=113, sha=SHA, n=1, wake=True, state="failing", when=NOW, login="github-actions[bot]"):
    rec = record(number, sha, (number,), n, wake, state)
    body = cf.marker_line(rec) + "\n\n```json\n" + json.dumps(rec) + "\n```"
    return {"id": 1, "user": {"login": login}, "body": body, "created_at": cf.rfc3339(when)}


def event(name="ci", conclusion="failure", sha=SHA, attempt=1, branch="fix/x-v2", kind="pull_request"):
    return {"repository": {"default_branch": "main"},
            "workflow_run": {"event": kind, "head_branch": branch, "head_sha": sha, "name": name,
                             "conclusion": conclusion, "id": 1, "run_attempt": attempt}}


class Fake:
    """In-memory GitHub for one repo, with the routes ci_fail.py reads and writes."""

    def __init__(self, labels=("deepseek",), body="", number=113, sha=SHA):
        self.repo = "popododo0720/vws-storage"
        self.name = "vws-storage"
        self.number = number
        self.sha = sha
        self.pulls = [self.pull(number, sha, "fix/x-v2", "open", list(labels), body)]
        self.comments = {number: []}
        self.events = {number: []}
        self.runs = []
        self.jobs = {}
        self.logs = {}
        self.calls = []
        self.fail_labels = False
        self.next_id = 500

    @staticmethod
    def pull(number, sha, ref, state="open", labels=(), body=""):
        return {"number": number, "state": state, "body": body,
                "head": {"sha": sha, "ref": ref}, "base": {"ref": "main"},
                "labels": [{"name": x} for x in labels]}

    def add_marker(self, number=113, sha=SHA, n=1, wake=True, when=NOW, login="github-actions[bot]"):
        c = marker_comment(number, sha, n, wake, when=when, login=login)
        c["id"] = self.next_id
        self.next_id += 1
        self.comments.setdefault(number, []).append(c)
        return c

    def add_run(self, name="ci", conclusion="failure", attempt=1, jobs=()):
        run_id = self.next_id
        self.next_id += 1
        run = {"id": run_id, "name": name, "run_attempt": attempt, "conclusion": conclusion,
               "head_sha": self.sha, "html_url": f"https://github.com/{self.repo}/actions/runs/{run_id}"}
        self.runs.append(run)
        entries = []
        for job_name, log in jobs:
            job_id = self.next_id
            self.next_id += 1
            entries.append({"id": job_id, "name": job_name, "conclusion": "failure",
                            "html_url": f"https://github.com/{self.repo}/actions/runs/{run_id}/job/{job_id}"})
            self.logs[job_id] = log
        self.jobs[(run_id, attempt)] = entries
        return run

    # client interface

    def get(self, path, params=None):
        self.calls.append(("GET", path, params))
        if path == f"repos/{self.repo}/commits/{self.sha}/pulls":
            return self.pulls
        if path == f"repos/{self.repo}/pulls":
            return self.pulls
        m = re.fullmatch(rf"repos/{self.repo}/issues/(\d+)/comments", path)
        if m:
            return self.comments.setdefault(int(m.group(1)), [])
        m = re.fullmatch(rf"repos/{self.repo}/issues/(\d+)/events", path)
        if m:
            return self.events.setdefault(int(m.group(1)), [])
        if path == f"repos/{self.repo}/actions/runs":
            return self.runs
        m = re.fullmatch(rf"repos/{self.repo}/actions/runs/(\d+)/attempts/(\d+)/jobs", path)
        if m:
            return self.jobs.get((int(m.group(1)), int(m.group(2))), [])
        return None

    def get_all(self, path, params=None):
        return self.get(path, params) or []

    def get_text(self, path):
        self.calls.append(("GET", path, None))
        m = re.search(r"/actions/jobs/(\d+)/logs$", path)
        return self.logs.get(int(m.group(1)), "") if m else ""

    def post(self, path, data, retry=True):
        self.calls.append(("POST", path, data))
        m = re.fullmatch(rf"repos/{self.repo}/issues/(\d+)/labels", path)
        if m:
            if self.fail_labels:
                raise cf.GhError(422, path, "label does not exist")
            pull = next(p for p in self.pulls if p["number"] == int(m.group(1)))
            pull["labels"] += [{"name": x} for x in data["labels"]]
            return [{"name": x} for x in data["labels"]]
        m = re.fullmatch(rf"repos/{self.repo}/issues/(\d+)/comments", path)
        if m:
            c = {"id": self.next_id, "user": {"login": "github-actions[bot]"},
                 "body": data["body"], "created_at": cf.rfc3339(NOW)}
            self.next_id += 1
            self.comments.setdefault(int(m.group(1)), []).append(c)
            return c
        raise AssertionError(f"unexpected POST {path}")

    def patch(self, path, data):
        self.calls.append(("PATCH", path, data))
        m = re.fullmatch(rf"repos/{self.repo}/issues/comments/(\d+)", path)
        if m:
            cid = int(m.group(1))
            for comments in self.comments.values():
                for c in comments:
                    if c["id"] == cid:
                        c["body"] = data["body"]
                        return c
        raise AssertionError(f"unexpected PATCH {path}")

    def posted(self):
        return [c for c in self.calls if c[0] == "POST" and c[1].endswith("/comments")]

    def patched(self):
        return [c for c in self.calls if c[0] == "PATCH"]

    def labels(self):
        return [c for c in self.calls if c[0] == "POST" and c[1].endswith("/labels")]


class _Response:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.data


class Api(unittest.TestCase):
    def openers(self):
        class Opener:
            def __init__(self):
                self.calls = 0

            def open(self, req, timeout):
                self.calls += 1
                if self.calls < 3:
                    raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, None)
                return _Response(b'{"ok": true}')
        return Opener()

    def test_transient_get_retries_with_a_bounded_attempt_count(self):
        client = cf.Client("popododo0720/vws-storage", "x")
        client.opener = self.openers()
        with unittest.mock.patch.object(cf.time, "sleep", lambda _: None):
            self.assertEqual(client.get("repos/x/y"), {"ok": True})
        self.assertEqual(client.opener.calls, 3)

        class Always:
            def __init__(self):
                self.calls = 0

            def open(self, req, timeout):
                self.calls += 1
                raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, None)
        client.opener = Always()
        with unittest.mock.patch.object(cf.time, "sleep", lambda _: None):
            with self.assertRaises(cf.GhError):
                client.get("repos/x/y")
        self.assertEqual(client.opener.calls, cf.RETRY_ATTEMPTS)

    def test_comment_post_is_not_retried(self):
        class Busy:
            def __init__(self):
                self.calls = 0

            def open(self, req, timeout):
                self.calls += 1
                raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, None)
        client = cf.Client("popododo0720/vws-storage", "x")
        client.opener = Busy()
        with unittest.mock.patch.object(cf.time, "sleep", lambda _: None):
            with self.assertRaises(cf.GhError):
                client.post("repos/x/y/issues/1/comments", {"body": "x"}, retry=False)
        self.assertEqual(client.opener.calls, 1)


class Create(unittest.TestCase):
    def test_creates_one_marker_with_the_record_and_a_masked_tail(self):
        w = Fake()
        w.add_run("ci", jobs=[("test", "line one\nS3 password=hunter2\n")])
        result = cf.run(w, event(), ["ci"], NOW)
        self.assertEqual(result["action"], "created")
        self.assertEqual(len(w.comments[113]), 1)
        body = w.comments[113][0]["body"]
        m = cf.parse_marker(body)
        self.assertEqual(m, {"sha": SHA, "v": 1, "repo": "vws-storage", "pr": 113,
                             "owner": "deepseek", "n": 1, "wake": True, "state": "failing"})
        rec = cf.record_from_comment(body)
        self.assertEqual(rec["runs"][0]["workflow"], "ci")
        self.assertEqual(rec["failed_jobs"][0]["job"], "test")
        self.assertEqual(rec["failed_jobs"][0]["tail_lines"], 2)
        self.assertIn("password=[redacted]", body)
        self.assertNotIn("hunter2", body)
        self.assertEqual(len(w.posted()), 1)

    def test_ignores_push_main_and_other_workflows(self):
        for ev, workflows in [
            (event(kind="push"), ["ci"]),
            (event(branch="main"), ["ci"]),
            (event(name="repo-guard"), ["ci"]),
            (event(conclusion="cancelled"), ["ci"]),
            (event(), []),
        ]:
            with self.subTest(ev=ev, workflows=workflows):
                w = Fake()
                self.assertEqual(cf.run(w, ev, workflows, NOW)["action"], "skip")
                self.assertEqual(w.posted(), [])

    def test_a_marker_by_another_login_does_not_dedupe(self):
        w = Fake()
        w.add_marker(login="mallory")
        w.add_run("ci")
        cf.run(w, event(), ["ci"], NOW)
        self.assertEqual(len(w.comments[113]), 2)
        self.assertEqual(len(w.posted()), 1)


class Dedupe(unittest.TestCase):
    def test_second_workflow_patches_one_comment(self):
        w = Fake()
        w.add_run("ci", jobs=[("test", "boom")])
        cf.run(w, event(), ["ci", "english"], NOW)
        created = cf.record_from_comment(w.comments[113][0]["body"])["created_at"]
        w.add_run("english", jobs=[("english", "Hangul found")])
        result = cf.run(w, event(name="english"), ["ci", "english"], NOW + dt.timedelta(minutes=1))
        self.assertEqual(result["action"], "updated")
        self.assertEqual(len(w.comments[113]), 1)
        self.assertEqual(len(w.posted()), 1)
        self.assertEqual(len(w.patched()), 1)
        rec = cf.record_from_comment(w.comments[113][0]["body"])
        self.assertEqual([r["workflow"] for r in rec["runs"]], ["ci", "english"])
        self.assertEqual(rec["n"], 1)
        self.assertTrue(rec["wake"])
        self.assertEqual(rec["created_at"], created)
        self.assertEqual(len(rec["failed_jobs"]), 2)

    def test_a_new_attempt_replaces_only_its_workflow(self):
        w = Fake()
        w.add_run("ci", jobs=[("old", "one")])
        w.add_run("english", jobs=[("english", "two")])
        cf.run(w, event(), ["ci", "english"], NOW)
        w.add_run("ci", attempt=2, jobs=[("new", "three")])
        cf.run(w, event(attempt=2), ["ci", "english"], NOW + dt.timedelta(minutes=1))
        rec = cf.record_from_comment(w.comments[113][0]["body"])
        ci = next(r for r in rec["runs"] if r["workflow"] == "ci")
        self.assertEqual(ci["run_attempt"], 2)
        self.assertEqual([j["job"] for j in rec["failed_jobs"] if j["workflow"] == "ci"], ["new"])

    def test_same_event_twice_patches_not_posts(self):
        w = Fake()
        w.add_run("ci")
        cf.run(w, event(), ["ci"], NOW)
        cf.run(w, event(), ["ci"], NOW)
        self.assertEqual(len(w.comments[113]), 1)
        self.assertEqual(len(w.posted()), 1)
        self.assertEqual(len(w.patched()), 1)


class Cap(unittest.TestCase):
    def chain(self):
        w = Fake(labels=["deepseek"])
        w.pulls.append(Fake.pull(110, OTHER[0], "fix/x", state="closed"))
        for sha in OTHER[:3]:
            w.add_marker(number=110, sha=sha, n=1)
        w.add_run("ci")
        return w

    def test_fourth_failing_head_labels_and_stops_waking(self):
        w = self.chain()
        result = cf.run(w, event(), ["ci"], NOW)
        self.assertEqual(result["n"], 4)
        self.assertFalse(result["wake"])
        body = w.comments[113][0]["body"]
        self.assertIn("n=4 wake=false", body)
        self.assertIn("**needs-human**: 4th failing head in this chain", body)
        current = next(p for p in w.pulls if p["number"] == 113)
        self.assertTrue(any(l["name"] == "needs-human" for l in current["labels"]))
        self.assertEqual(len(w.labels()), 1)

    def test_update_keeps_the_escalation_and_does_not_relabel(self):
        w = self.chain()
        cf.run(w, event(), ["ci"], NOW)
        w.add_run("english")
        result = cf.run(w, event(name="english"), ["ci", "english"], NOW + dt.timedelta(minutes=1))
        self.assertEqual(result["action"], "updated")
        self.assertEqual(len(w.labels()), 1)
        self.assertIn("**needs-human**", w.comments[113][0]["body"])
        self.assertEqual(cf.parse_marker(w.comments[113][0]["body"])["n"], 4)

    def test_a_missing_needs_human_label_still_posts_the_escalation(self):
        w = self.chain()
        w.fail_labels = True
        result = cf.run(w, event(), ["ci"], NOW)
        self.assertEqual(result["action"], "created")
        self.assertIn("**needs-human**", w.comments[113][0]["body"])
        self.assertEqual(len(w.labels()), 1)

    def test_needs_human_blocks_wake_without_an_escalation(self):
        w = Fake(labels=["deepseek", "needs-human"])
        w.add_marker(number=110, sha=OTHER[0])
        w.pulls.append(Fake.pull(110, OTHER[0], "fix/x", state="closed"))
        w.add_run("ci")
        result = cf.run(w, event(), ["ci"], NOW)
        self.assertEqual(result["n"], 2)
        self.assertFalse(result["wake"])
        self.assertNotIn("**needs-human**", w.comments[113][0]["body"])
        self.assertEqual(w.labels(), [])

    def test_human_reset_restarts_the_count(self):
        w = Fake(labels=["deepseek"])
        w.pulls.append(Fake.pull(110, OTHER[0], "fix/x", state="closed"))
        for sha in OTHER[:3]:
            w.add_marker(number=110, sha=sha, when=NOW - dt.timedelta(hours=3))
        w.events[113].append({"event": "unlabeled", "label": {"name": "needs-human"},
                              "created_at": cf.rfc3339(NOW - dt.timedelta(hours=1))})
        w.add_run("ci")
        result = cf.run(w, event(), ["ci"], NOW)
        self.assertEqual(result["n"], 1)
        self.assertTrue(result["wake"])


class Passed(unittest.TestCase):
    def test_all_failed_workflows_green_marks_passed(self):
        w = Fake()
        w.add_run("ci", jobs=[("test", "boom")])
        cf.run(w, event(), ["ci"], NOW)
        before = cf.record_from_comment(w.comments[113][0]["body"])
        w.add_run("ci", conclusion="success", attempt=2)
        result = cf.run(w, event(conclusion="success", attempt=2), ["ci"], NOW + dt.timedelta(minutes=2))
        self.assertEqual(result["action"], "passed")
        body = w.comments[113][0]["body"]
        m = cf.parse_marker(body)
        self.assertEqual(m["state"], "passed")
        self.assertEqual(m["wake"], before["wake"])
        rec = cf.record_from_comment(body)
        self.assertEqual(rec["state"], "passed")
        self.assertEqual(rec["created_at"], before["created_at"])
        self.assertEqual(len(w.posted()), 1)
        again = cf.run(w, event(conclusion="success", attempt=2), ["ci"], NOW + dt.timedelta(minutes=3))
        self.assertEqual(again["action"], "skip")

    def test_one_failed_workflow_without_a_green_rerun_stays_failing(self):
        w = Fake()
        w.add_run("ci", jobs=[("test", "boom")])
        w.add_run("english", jobs=[("english", "bad")])
        cf.run(w, event(), ["ci", "english"], NOW)
        w.add_run("ci", conclusion="success", attempt=2)
        result = cf.run(w, event(conclusion="success", attempt=2), ["ci"], NOW + dt.timedelta(minutes=2))
        self.assertEqual(result["action"], "skip")
        self.assertEqual(cf.parse_marker(w.comments[113][0]["body"])["state"], "failing")


class Owner(unittest.TestCase):
    def test_table(self):
        cases = [
            (["grok", "deepseek"], "", "deepseek/x", "vws-storage", ("vws-storage", "label:grok")),
            (["deepseek"], "", "fix/x", "vws-storage", ("deepseek", "label:deepseek")),
            ([], "Agent: deepseek-dev", "fix/x", "vws-storage", ("deepseek", "agent_line")),
            ([], "Agent: vws-conformance grok", "fix/x", "vws-storage", ("vws-conformance", "agent_line")),
            ([], "Agent: dr eggbot", "fix/x", "vws-storage", ("eggbot", "agent_line")),
            ([], "Agent: someone", "fix/x", "vws-storage", ("none", "agent_line")),
            ([], "", "deepseek/fix", "vws-storage", ("deepseek", "branch_prefix")),
            ([], "", "fix/x", "vws-storage", ("none", "none")),
        ]
        for labels, body, branch, repo, want in cases:
            with self.subTest(labels=labels, body=body, branch=branch):
                self.assertEqual(cf.owner_of(labels, body, branch, repo), want)

    def test_wakeable(self):
        self.assertTrue(cf.wakeable("deepseek", "vws-storage"))
        self.assertTrue(cf.wakeable("vws-storage", "vws-storage"))
        self.assertFalse(cf.wakeable("vws-iam", "vws-storage"))
        self.assertFalse(cf.wakeable("eggbot", "vws-storage"))
        self.assertFalse(cf.wakeable("none", "vws-storage"))

    def test_eggbot_is_recorded_but_not_woken(self):
        w = Fake(labels=[], body="Agent: eggbot")
        w.add_run("ci")
        result = cf.run(w, event(), ["ci"], NOW)
        self.assertEqual(result["wake"], False)
        rec = cf.record_from_comment(w.comments[113][0]["body"])
        self.assertEqual((rec["owner"], rec["owner_source"]), ("eggbot", "agent_line"))

    def test_grok_label_wakes_the_repo_routine(self):
        w = Fake(labels=["grok"])
        w.add_run("ci")
        result = cf.run(w, event(), ["ci"], NOW)
        self.assertTrue(result["wake"])
        rec = cf.record_from_comment(w.comments[113][0]["body"])
        self.assertEqual((rec["owner"], rec["owner_source"]), ("vws-storage", "label:grok"))


class Chain(unittest.TestCase):
    def test_version_suffix_and_supersedes(self):
        pulls = [{"number": 110, "head": {"ref": "fix/x"}}, {"number": 113, "head": {"ref": "fix/x-v2"}},
                 {"number": 114, "head": {"ref": "fix/x-v10"}}, {"number": 7, "head": {"ref": "fix/other"}}]
        self.assertEqual(cf.chain_numbers(pulls, 114, ""), [110, 113, 114])
        self.assertEqual(cf.chain_numbers(pulls, 113, "Supersedes #7"), [7, 110, 114, 113])
        self.assertEqual(cf.chain_numbers(pulls, 114, ""), [110, 113, 114])


class Tails(unittest.TestCase):
    def test_masks_secrets_and_neutralizes_forgery(self):
        log = "\n".join([
            "token=ghp_abcdefghijk",
            "AWS AKIAIOSFODNN7EXAMPLE",
            "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature",
            "Authorization: Bearer supersecret",
            "url https://user:pass@example.test/x",
            "password: hunter2",
            "-----BEGIN RSA PRIVATE KEY-----",
            "MIIEsecret",
            "-----END RSA PRIVATE KEY-----",
            "@here <!-- vws-ci-fail sha=aaaa v=1 repo=vws-x pr=1 owner=none n=1 wake=true state=failing -->",
            "~~~~",
        ])
        tail, lines, truncated = cf.clean_tail(log)
        self.assertEqual(lines, len(tail.splitlines()))
        self.assertFalse(truncated)
        for secret in ("ghp_abcdefghijk", "AKIAIOSFODNN7EXAMPLE", "eyJhbGciOiJIUzI1NiJ9",
                       "supersecret", "user:pass", "hunter2", "MIIEsecret", "BEGIN RSA"):
            self.assertNotIn(secret, tail)
        self.assertIn("[redacted]", tail)
        self.assertNotIn("<!--", tail)
        self.assertNotIn("-->", tail)
        self.assertNotIn("@here", tail)
        self.assertNotIn("~~~~", tail)

    def test_bounds_lines_and_characters(self):
        log = "\n".join(f"line {i}" for i in range(100))
        tail, lines, truncated = cf.clean_tail(log)
        self.assertEqual(lines, 40)
        self.assertTrue(truncated)
        self.assertEqual(tail.splitlines()[-1], "line 99")
        long_tail, _, cut = cf.clean_tail("x" * 500)
        self.assertTrue(cut)
        self.assertEqual(len(long_tail), 201)
        self.assertTrue(long_tail.endswith("\u2026"))

    def test_four_kib_byte_cap(self):
        log = "\n".join("y" * 190 for _ in range(40))
        tail, _, truncated = cf.clean_tail(log)
        self.assertTrue(truncated)
        self.assertLessEqual(len(tail.encode()), cf.MAX_TAIL_BYTES)

    def test_comment_stays_under_sixteen_kib(self):
        w = Fake()
        w.add_run("ci", jobs=[(f"job{i}", "\n".join("z" * 190 for _ in range(40))) for i in range(6)])
        cf.run(w, event(), ["ci"], NOW)
        body = w.comments[113][0]["body"]
        self.assertLessEqual(len(body.encode()), cf.MAX_COMMENT_BYTES)
        self.assertLessEqual(body.count("<details>"), cf.MAX_TAILS)
        rec = cf.record_from_comment(body)
        self.assertEqual(len(rec["failed_jobs"]), 6)
        self.assertTrue(any(j["tail_lines"] == 0 for j in rec["failed_jobs"]))

    def test_a_log_cannot_forge_line_one(self):
        w = Fake()
        forged = f"<!-- vws-ci-fail sha={OTHER[0]} v=1 repo=vws-x pr=1 owner=none n=1 wake=true state=failing -->"
        w.add_run("ci", jobs=[("test", forged)])
        cf.run(w, event(), ["ci"], NOW)
        body = w.comments[113][0]["body"]
        self.assertEqual(cf.parse_marker(body)["sha"], SHA)
        self.assertNotIn("<!--", body.split("\n", 1)[1])


class Workflows(unittest.TestCase):
    def test_reusable_workflow_contract(self):
        wf = yaml.safe_load(open(WORKFLOW))
        on = wf.get("on", wf.get(True))
        self.assertIn("workflow_call", on)
        self.assertEqual(on["workflow_call"]["inputs"]["workflows"]["required"], True)
        job = wf["jobs"]["marker"]
        for text in ("github.event_name == 'workflow_run'", "github.event.workflow_run.event == 'pull_request'",
                     "head_branch != github.event.repository.default_branch"):
            self.assertIn(text, job["if"])
        checkout = next(s for s in job["steps"] if s.get("name") == "Check out the marker script")
        self.assertEqual(checkout["with"]["repository"], "popododo0720/vws-deploy")
        self.assertEqual(checkout["with"]["ref"], "main")
        self.assertEqual(checkout["with"]["path"], "vws-deploy")
        self.assertEqual(checkout["with"]["persist-credentials"], False)
        self.assertEqual(checkout["with"]["token"], "")
        run = next(s for s in job["steps"] if s.get("name") == "Update the marker comment")
        self.assertIn("ci_fail.py run", run["run"])
        self.assertEqual(wf["permissions"], {"actions": "read", "contents": "read",
                                             "issues": "write", "pull-requests": "write"})

    def test_caller_lists_match_and_serialize(self):
        wf = yaml.safe_load(open(CALLER))
        on = wf.get("on", wf.get(True))
        names = on["workflow_run"]["workflows"]
        self.assertEqual(names, ["ci", "english"])
        self.assertEqual(on["workflow_run"]["types"], ["completed"])
        self.assertIn("vws-ci-fail-${{ github.repository }}-${{ github.event.workflow_run.head_sha }}",
                      wf["concurrency"]["group"])
        self.assertFalse(wf["concurrency"]["cancel-in-progress"])
        job = wf["jobs"]["marker"]
        self.assertEqual(job["uses"], "popododo0720/vws-deploy/.github/workflows/ci-fail.yml@main")
        self.assertEqual(job["with"]["workflows"], ",".join(names))
        self.assertEqual(job["permissions"], {"actions": "read", "contents": "read",
                                              "issues": "write", "pull-requests": "write"})

    def test_labels_define_needs_human(self):
        labels = {l["name"]: l for l in yaml.safe_load(open(LABELS))}
        self.assertIn("needs-human", labels)
        self.assertTrue(labels["needs-human"]["description"])

    def test_marker_regex_matches_the_documented_example(self):
        line = (f"<!-- vws-ci-fail sha={SHA} v=1 repo=vws-storage pr=113 owner=deepseek n=3 "
                "wake=true state=failing -->")
        m = cf.MARKER_RE.match(line)
        self.assertIsNotNone(m)
        self.assertEqual(cf.parse_marker(line)["owner"], "deepseek")
        self.assertEqual(cf.parse_marker(line)["n"], 3)


if __name__ == "__main__":
    unittest.main()
