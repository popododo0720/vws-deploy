"""Tests for the CI-failure marker comment (vws-deploy#412). Fake gh, no network."""
import importlib.util
import os
import re
import subprocess
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("ci_fail", os.path.join(ROOT, ".github", "ci-fail", "ci_fail.py"))
cf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cf)

REPO = "vws-storage"
FULL = f"popododo0720/{REPO}"
A = f"repos/{FULL}"
BOT = "github-actions[bot]"
NOW = "2026-10-23T06:00:00Z"
SHA = "9ef2b4d056eb4e983cfe431ca9b5bdad4e1f0193"


def sha(i):
    return f"{i:040x}"


def record(**kw):
    rec = {"v": 1, "repo": REPO, "pr": 113, "head_sha": SHA, "head_branch": "fix/x-v2", "owner": "deepseek",
           "agent": "deepseek-dev", "owner_source": "label:deepseek", "chain": [110, 113], "n": 3, "cap": 3,
           "wake": True, "runs": [], "failed_jobs": [], "created_at": NOW, "updated_at": NOW}
    rec.update(kw)
    return rec


class FakeGh:
    """Routes GETs to fixtures and records writes; posted comments become visible to later GETs."""

    def __init__(self, prs, comments=None, events=None, jobs=None, logs=None):
        self.prs = prs                      # number -> {head branch, body, labels}
        self.comments = comments or {}      # number -> [comment]
        self.events = events or {}          # number -> [event]
        self.jobs = jobs or {}              # (run_id, attempt) -> [job]
        self.logs = logs or {}              # job_id -> text
        self.writes = []
        self.retried = []                   # paths of writes made with retry=True
        self.job_reads = 0
        self.job_pages = []                 # page numbers of every jobs read
        self.endless_jobs = False
        self.next_id = 1000

    def _pr(self, n):
        p = self.prs[n]
        return {"number": n, "state": p.get("state", "open"), "head": {"ref": p["branch"], "sha": p.get("sha", SHA),
                "repo": {"full_name": p.get("head_repo", FULL)}}, "base": {"repo": {"full_name": FULL}}, "body": p.get("body", ""),
                "labels": [{"name": x} for x in p.get("labels", [])]}

    def get_all(self, path):
        m = re.fullmatch(rf"{A}/pulls\?state=open&head=popododo0720:(.+)&per_page=100", path)
        if m:
            return [self._pr(n) for n, p in self.prs.items() if p["branch"] == m.group(1) and p.get("state", "open") == "open"]
        if path == f"{A}/pulls?state=all&per_page=100":
            return [self._pr(n) for n in self.prs]
        m = re.fullmatch(rf"{A}/issues/(\d+)/(comments|events)\?per_page=100", path)
        if m:
            src = self.comments if m.group(2) == "comments" else self.events
            return list(src.get(int(m.group(1)), []))
        raise AssertionError(f"unexpected GET {path}")

    def get(self, path):
        m = re.fullmatch(rf"{A}/actions/runs/(\d+)/attempts/(\d+)/jobs\?per_page=100(&page=(\d+))?", path)
        if m:
            page = int(m.group(4) or 1)
            if page == 1:
                self.job_reads += 1
            self.job_pages.append(page)
            got = self.jobs.get((int(m.group(1)), int(m.group(2))), [])
            if callable(got):                 # a lagging jobs API: the answer depends on the read count
                got = got(self.job_reads)
            if self.endless_jobs:             # a jobs API that never ends: full pages forever
                return {"total_count": 10 ** 6, "jobs": [job(100000 + page * 100 + i, conclusion="success") for i in range(100)]}
            return {"total_count": len(got), "jobs": got[(page - 1) * 100:page * 100]}
        raise AssertionError(f"unexpected GET {path}")

    def text(self, path):
        m = re.fullmatch(rf"{A}/actions/jobs/(\d+)/logs", path)
        assert m, path
        return self.logs.get(int(m.group(1)), "")

    def post(self, path, payload, retry=False):
        self.writes.append(("POST", path, payload))
        if retry:
            self.retried.append(path)
        m = re.fullmatch(rf"{A}/issues/(\d+)/comments", path)
        if m:
            self.next_id += 1
            self.comments.setdefault(int(m.group(1)), []).append(
                {"id": self.next_id, "user": {"login": BOT}, "author_association": "NONE",
                 "created_at": NOW, "body": payload["body"]})
        return {}

    def patch(self, path, payload):
        self.writes.append(("PATCH", path, payload))
        cid = int(path.rsplit("/", 1)[1])
        for cs in self.comments.values():
            for c in cs:
                if c["id"] == cid:
                    c["body"] = payload["body"]
        return {}


def event(run_id=500, attempt=1, workflow="ci", conclusion="failure", branch="fix/x-v2", head_sha=SHA,
          ev="pull_request", head_repo=FULL):
    return {"workflow_run": {"id": run_id, "run_attempt": attempt, "name": workflow, "conclusion": conclusion,
                             "event": ev, "head_branch": branch, "head_sha": head_sha,
                             "html_url": f"https://github.com/{FULL}/actions/runs/{run_id}",
                             "head_repository": {"full_name": head_repo}}}


def job(jid, name="test", workflow="ci", conclusion="failure"):
    return {"id": jid, "name": name, "workflow_name": workflow, "conclusion": conclusion,
            "html_url": f"https://github.com/{FULL}/actions/runs/1/job/{jid}"}


def marker_comment(cid, s, n, user=BOT, pr=113, at="2026-10-23T05:00:00Z"):
    body = cf.format_comment(record(head_sha=s, n=n, pr=pr), {})
    return {"id": cid, "user": {"login": user}, "author_association": "NONE", "created_at": at, "body": body}


def posts(gh):
    return [w for w in gh.writes if w[0] == "POST" and w[1].endswith("/comments")]


class RoundTrip(unittest.TestCase):
    def test_format_then_parse_returns_the_record(self):
        rec = record(failed_jobs=[{"workflow": "ci", "job": "test", "job_id": 7, "conclusion": "failure",
                                   "url": "u", "tail_lines": 1, "tail_truncated": False}])
        body = cf.format_comment(rec, {7: "boom"})
        self.assertEqual(cf.parse_comment(body), rec)

    def test_first_line_is_the_marker_in_key_order(self):
        body = cf.format_comment(record(), {})
        self.assertEqual(body.split("\n", 1)[0],
                         f"<!-- vws-ci-fail sha={SHA} v=1 repo={REPO} pr=113 owner=deepseek agent=deepseek-dev n=3 wake=true -->")

    def test_empty_agent_is_a_dash_on_line_one(self):
        body = cf.format_comment(record(owner="none", agent="", owner_source="none", wake=False), {})
        self.assertIn(" owner=none agent=- n=3 wake=false -->", body.split("\n", 1)[0])
        self.assertEqual(cf.parse_comment(body)["agent"], "")

    def test_marker_and_json_must_agree(self):
        body = cf.format_comment(record(), {}).replace("wake=true", "wake=false", 1)
        self.assertIsNone(cf.parse_comment(body))

    def test_no_marker_or_no_json_is_not_a_record(self):
        self.assertIsNone(cf.parse_comment("hello"))
        body = cf.format_comment(record(), {})
        self.assertIsNone(cf.parse_comment(body.split("```json")[0]))
        self.assertIsNone(cf.parse_comment(" " + body))


class Masking(unittest.TestCase):
    def test_secrets_are_redacted(self):
        log = "\n".join([
            "token ghp_abcdefghijklmnop1234 used",
            "pat github_pat_11ABCDEFG_abcdefghijk",
            "Authorization: Bearer abc.def",
            "key AKIAABCDEFGHIJKLMNOP",
            "password=hunter2 next",
            "API_KEY: s3cr3t",
            "clone https://user:pw123@example.com/r.git",
            "jwt eyJhbGciOi.eyJzdWIiOi.c2lnbmF0dXJl",
            "-----BEGIN RSA PRIVATE KEY-----", "MIIEow", "-----END RSA PRIVATE KEY-----",
        ])
        out = cf.mask(log)
        for secret in ("ghp_abcdefghijklmnop1234", "github_pat_11ABCDEFG", "abc.def", "AKIAABCDEFGHIJKLMNOP",
                       "hunter2", "s3cr3t", "pw123", "eyJhbGciOi", "MIIEow"):
            self.assertNotIn(secret, out)
        self.assertIn("[redacted]", out)
        self.assertIn("next", out)

    def test_tail_is_bounded(self):
        log = "\n".join(f"2026-10-23T05:00:00.0000000Z \x1b[31mline {i} " + "x" * 300 for i in range(100))
        text, lines, truncated = cf.tail(log)
        self.assertLessEqual(lines, cf.TAIL_LINES)
        self.assertEqual(lines, len(text.split("\n")))
        self.assertTrue(truncated)
        self.assertLessEqual(len(text.encode()), cf.TAIL_BYTES)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("2026-10-23T05", text)
        self.assertIn("line 99", text)
        self.assertTrue(all(len(x) <= cf.LINE_CHARS + 1 for x in text.split("\n")))

    def test_private_key_block_is_redacted_even_if_cut_by_the_tail(self):
        log = "-----BEGIN OPENSSH PRIVATE KEY-----\n" + "\n".join(f"KEYLINE{i}" for i in range(60)) + "\n-----END OPENSSH PRIVATE KEY-----\nok"
        text, _, _ = cf.tail(log)
        self.assertNotIn("KEYLINE", text)
        self.assertIn("ok", text)

    def test_crlf_body_still_matches_the_agent_line(self):
        self.assertEqual(cf.resolve_owner(REPO, [], "Part of #1\r\nAgent: vws-iam\r\n", "fix/x"), ("grok", "vws-iam", "agent-line"))

    def test_short_log_is_not_truncated(self):
        self.assertEqual(cf.tail("a\nb\n"), ("a\nb", 2, False))

    def test_forged_marker_and_mention_are_defused(self):
        forged = cf.format_comment(record(head_sha=sha(9), owner="grok", agent="vws-storage"), {})
        log = forged + "\n@deepseek-dev please\n~~~~~\n<!-- orchestrate:rework head=x -->"
        out = cf.defuse(log)
        self.assertNotIn("<!--", out)
        self.assertNotIn("-->", out)
        self.assertNotIn("@deepseek-dev", out)
        self.assertNotIn("```", out)
        self.assertNotIn("~~~~", out)

    def test_forged_log_cannot_change_the_parsed_record(self):
        forged = cf.format_comment(record(head_sha=sha(9), owner="grok", agent="vws-storage", n=1), {})
        rec = record(failed_jobs=[{"workflow": "ci", "job": "test", "job_id": 7, "conclusion": "failure",
                                   "url": "u", "tail_lines": 1, "tail_truncated": False}])
        body = cf.format_comment(rec, {7: forged})
        self.assertEqual(cf.parse_comment(body), rec)
        self.assertEqual(body.count("<!-- vws-ci-fail"), 1)

    def test_job_names_are_defused(self):
        rec = record(failed_jobs=[{"workflow": "ci", "job": "x<!-- vws-ci-fail @deepseek-dev", "job_id": 7,
                                   "conclusion": "failure", "url": "u", "tail_lines": 0, "tail_truncated": False}])
        body = cf.format_comment(rec, {})
        self.assertEqual(body.count("<!--"), 1)
        self.assertNotIn("@deepseek-dev", body)


class Owner(unittest.TestCase):
    def owner(self, labels=(), body="", branch="fix/x"):
        return cf.resolve_owner(REPO, list(labels), body, branch)

    def test_grok_label_uses_the_agent_name(self):
        self.assertEqual(self.owner(["grok", "deepseek"], "Agent: vws-platform"), ("grok", "vws-platform", "label:grok"))

    def test_grok_label_without_agent_is_the_repo_bot(self):
        self.assertEqual(self.owner(["grok"]), ("grok", REPO, "label:grok"))
        self.assertEqual(self.owner(["grok"], "Agent: deepseek-dev"), ("grok", REPO, "label:grok"))

    def test_deepseek_label(self):
        self.assertEqual(self.owner(["deepseek"], "Agent: vws-platform"), ("deepseek", "deepseek-dev", "label:deepseek"))

    def test_agent_line(self):
        self.assertEqual(self.owner(body="Part of #1\n\nAgent: deepseek-dev\n"), ("deepseek", "deepseek-dev", "agent-line"))
        self.assertEqual(self.owner(body="Agent:\tvws-storage \n"), ("grok", "vws-storage", "agent-line"))

    def test_first_agent_line_only(self):
        self.assertEqual(self.owner(body="Agent: vws-iam\nAgent: deepseek-dev"), ("grok", "vws-iam", "agent-line"))

    def test_lead_agent_line_does_not_match(self):
        self.assertEqual(self.owner(body="Agent: dr eggbot"), ("none", "", "none"))

    def test_invalid_agent_values_fall_through(self):
        for body in ("Agent:", "Agent: ", "agent: vws-iam", "AGENT: vws-iam", "Agent: VWS-iam", "  Agent: vws-iam",
                     "Agent: -vws", "Agent: " + "a" * 40):
            self.assertEqual(self.owner(body=body), ("none", "", "none"), body)

    def test_empty_agent_falls_to_branch_prefix(self):
        self.assertEqual(self.owner(body="Agent:", branch="deepseek/x"), ("deepseek", "deepseek-dev", "branch-prefix"))

    def test_other_prefixes_carry_no_owner(self):
        for b in ("fix/x", "test/x", "feat/deepseek/x"):
            self.assertEqual(self.owner(branch=b), ("none", "", "none"))


def storage_gh(labels=("deepseek",), body="Part of #1", comments=None, events=None, extra_prs=None):
    prs = {113: {"branch": "fix/x-v2", "body": body, "labels": list(labels)}}
    prs.update(extra_prs or {})
    jobs = {(500, 1): [job(1, "test"), job(2, "lint", conclusion="success")], (500, 2): [job(3, "test")],
            (600, 1): [job(4, "english", workflow="english")]}
    logs = {1: "FAIL TestA\ntoken ghp_abcdefghijklmnop1234", 3: "FAIL TestB", 4: "Hangul found"}
    return FakeGh(prs, comments=comments, events=events, jobs=jobs, logs=logs)


class Gate(unittest.TestCase):
    def test_only_failed_same_repo_pr_runs(self):
        cases = [("workflow_run", event(ev="push")), ("workflow_run", event(conclusion="success")),
                 ("workflow_run", event(conclusion="cancelled")), ("workflow_run", event(branch="main")),
                 ("workflow_run", event(head_repo="someone/vws-storage")), ("push", event()),
                 ("workflow_run", event(head_sha=sha(5)))]
        for name, ev in cases:
            gh = storage_gh()
            out = cf.run(gh, name, ev, FULL, NOW)
            self.assertEqual(out["action"], "skip", (name, ev))
            self.assertEqual(gh.writes, [])

    def test_repo_outside_vws_is_skipped(self):
        gh = storage_gh()
        self.assertEqual(cf.run(gh, "workflow_run", event(), "someone/other", NOW)["action"], "skip")


class Dedupe(unittest.TestCase):
    def test_first_failure_posts_one_comment(self):
        gh = storage_gh()
        out = cf.run(gh, "workflow_run", event(), FULL, NOW)
        self.assertEqual(out["action"], "post")
        self.assertEqual(len(posts(gh)), 1)
        rec = cf.parse_comment(posts(gh)[0][2]["body"])
        self.assertEqual((rec["n"], rec["wake"], rec["owner"], rec["chain"]), (1, True, "deepseek", [113]))
        self.assertEqual([j["job"] for j in rec["failed_jobs"]], ["test"])
        self.assertNotIn("ghp_abcdefghijklmnop1234", posts(gh)[0][2]["body"])
        self.assertIn("FAIL TestA", posts(gh)[0][2]["body"])

    def test_rerun_and_second_workflow_patch_the_same_comment(self):
        gh = storage_gh()
        cf.run(gh, "workflow_run", event(), FULL, NOW)
        out = cf.run(gh, "workflow_run", event(attempt=2), FULL, NOW)
        self.assertEqual(out["action"], "patch")
        out = cf.run(gh, "workflow_run", event(run_id=600, workflow="english"), FULL, NOW)
        self.assertEqual(out["action"], "patch")
        self.assertEqual(len(posts(gh)), 1)
        self.assertEqual(len(gh.comments[113]), 1)
        rec = cf.parse_comment(gh.comments[113][0]["body"])
        self.assertEqual(rec["n"], 1)
        self.assertEqual([(r["workflow"], r["run_attempt"]) for r in rec["runs"]], [("ci", 2), ("english", 1)])
        self.assertEqual([j["job_id"] for j in rec["failed_jobs"]], [3, 4])
        self.assertIn("FAIL TestB", gh.comments[113][0]["body"])
        self.assertIn("Hangul found", gh.comments[113][0]["body"])
        self.assertNotIn("FAIL TestA", gh.comments[113][0]["body"])

    def test_patch_keeps_n_and_wake(self):
        gh = storage_gh(comments={113: [marker_comment(1, sha(1), 1), marker_comment(2, sha(2), 2)]})
        cf.run(gh, "workflow_run", event(), FULL, NOW)
        gh.prs[113]["labels"] = ["needs-human"]
        gh.comments[113].append({"id": 60, "user": {"login": "popododo0720"}, "author_association": "OWNER",
                                 "created_at": "2026-10-23T07:00:00Z", "body": "/orchestrate reset-failures"})
        self.assertEqual(cf.run(gh, "workflow_run", event(attempt=2), FULL, NOW)["action"], "patch")
        rec = cf.parse_comment(gh.comments[113][2]["body"])
        self.assertEqual((rec["n"], rec["wake"]), (3, True))

    def test_a_marker_by_another_user_is_not_reused(self):
        gh = storage_gh(comments={113: [marker_comment(1, SHA, 1, user="mallory")]})
        self.assertEqual(cf.run(gh, "workflow_run", event(), FULL, NOW)["action"], "post")


class Cap(unittest.TestCase):
    def test_third_failure_still_wakes(self):
        gh = storage_gh(comments={113: [marker_comment(1, sha(1), 1), marker_comment(2, sha(2), 2)]})
        cf.run(gh, "workflow_run", event(), FULL, NOW)
        rec = cf.parse_comment(posts(gh)[0][2]["body"])
        self.assertEqual((rec["n"], rec["wake"]), (3, True))
        self.assertFalse([w for w in gh.writes if w[1].endswith("/labels")])

    def test_fourth_failure_across_the_chain_escalates(self):
        old = {110: {"branch": "fix/x", "state": "closed", "body": ""}}
        gh = storage_gh(extra_prs=old, comments={110: [marker_comment(1, sha(1), 1, pr=110), marker_comment(2, sha(2), 2, pr=110)],
                                                 113: [marker_comment(3, sha(3), 3)]})
        out = cf.run(gh, "workflow_run", event(), FULL, NOW)
        body = posts(gh)[0][2]["body"]
        rec = cf.parse_comment(body)
        self.assertEqual((rec["n"], rec["wake"], rec["chain"]), (4, False, [110, 113]))
        self.assertIn(("POST", f"{A}/issues/113/labels", {"labels": ["needs-human"]}), gh.writes)
        self.assertEqual(body.count("**needs-human**"), 1)
        self.assertEqual(out["action"], "post")

    def test_the_label_post_is_retried_and_the_comment_post_is_not(self):
        gh = storage_gh(comments={113: [marker_comment(1, sha(1), 1), marker_comment(2, sha(2), 2),
                                        marker_comment(3, sha(3), 3)]})
        cf.run(gh, "workflow_run", event(), FULL, NOW)
        self.assertEqual(gh.retried, [f"{A}/issues/113/labels"])

    def test_a_fork_pr_on_the_same_branch_is_not_in_the_chain(self):
        fork = {111: {"branch": "fix/x", "state": "closed", "head_repo": "mallory/vws-storage"}}
        gh = storage_gh(extra_prs=fork, comments={111: [marker_comment(1, sha(1), 1, pr=111),
                                                        marker_comment(2, sha(2), 2, pr=111),
                                                        marker_comment(3, sha(3), 3, pr=111)]})
        cf.run(gh, "workflow_run", event(), FULL, NOW)
        rec = cf.parse_comment(posts(gh)[0][2]["body"])
        self.assertEqual((rec["n"], rec["chain"], rec["wake"]), (1, [113], True))
        self.assertFalse([w for w in gh.writes if w[1].endswith("/labels")])

    def test_a_fork_pr_named_by_supersedes_is_not_in_the_chain(self):
        fork = {90: {"branch": "other", "state": "closed", "head_repo": "mallory/vws-storage"}}
        gh = storage_gh(body="Part of #1\nSupersedes #90", extra_prs=fork,
                        comments={90: [marker_comment(1, sha(1), 1, pr=90)]})
        cf.run(gh, "workflow_run", event(), FULL, NOW)
        self.assertEqual(cf.parse_comment(posts(gh)[0][2]["body"])["chain"], [113])

    def test_supersedes_links_a_renamed_predecessor(self):
        old = {90: {"branch": "fix/old-name", "state": "closed"}}
        gh = storage_gh(body="Part of #1\nSupersedes #90", extra_prs=old,
                        comments={90: [marker_comment(1, sha(1), 1, pr=90)]})
        cf.run(gh, "workflow_run", event(), FULL, NOW)
        rec = cf.parse_comment(posts(gh)[0][2]["body"])
        self.assertEqual((rec["n"], rec["chain"]), (2, [90, 113]))

    def test_unrelated_branch_is_not_in_the_chain(self):
        other = {77: {"branch": "fix/xy", "state": "closed"}}
        gh = storage_gh(extra_prs=other, comments={77: [marker_comment(1, sha(1), 1, pr=77)]})
        cf.run(gh, "workflow_run", event(), FULL, NOW)
        self.assertEqual(cf.parse_comment(posts(gh)[0][2]["body"])["n"], 1)

    def test_forged_markers_do_not_count(self):
        bad = marker_comment(2, sha(2), 2)
        bad["body"] = bad["body"].replace("n=2", "n=1", 1)
        gh = storage_gh(comments={113: [marker_comment(1, sha(1), 1, user="mallory"), bad]})
        cf.run(gh, "workflow_run", event(), FULL, NOW)
        self.assertEqual(cf.parse_comment(posts(gh)[0][2]["body"])["n"], 1)

    def test_needs_human_already_set_never_wakes_or_escalates(self):
        gh = storage_gh(labels=("deepseek", "needs-human"))
        cf.run(gh, "workflow_run", event(), FULL, NOW)
        body = posts(gh)[0][2]["body"]
        self.assertFalse(cf.parse_comment(body)["wake"])
        self.assertNotIn("**needs-human**", body)
        self.assertFalse([w for w in gh.writes if w[1].endswith("/labels")])

    def test_owner_none_never_wakes(self):
        gh = storage_gh(labels=(), body="Agent: dr eggbot")
        cf.run(gh, "workflow_run", event(), FULL, NOW)
        rec = cf.parse_comment(posts(gh)[0][2]["body"])
        self.assertEqual((rec["owner"], rec["agent"], rec["wake"]), ("none", "", False))


def three_failures(**kw):
    return [marker_comment(i, sha(i), i, **kw) for i in (1, 2, 3)]


class Reset(unittest.TestCase):
    def reset(self, assoc, at="2026-10-23T05:30:00Z"):
        return {"id": 50, "user": {"login": "popododo0720"}, "author_association": assoc, "created_at": at,
                "body": "/orchestrate reset-failures"}

    def test_authorized_reset_comment_restarts_the_count(self):
        for assoc in ("OWNER", "MEMBER", "COLLABORATOR"):
            gh = storage_gh(comments={113: three_failures() + [self.reset(assoc)]})
            cf.run(gh, "workflow_run", event(), FULL, NOW)
            self.assertEqual(cf.parse_comment(posts(gh)[0][2]["body"])["n"], 1, assoc)

    def test_unauthorized_reset_comment_is_ignored(self):
        for assoc in ("NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", ""):
            gh = storage_gh(comments={113: three_failures() + [self.reset(assoc)]})
            cf.run(gh, "workflow_run", event(), FULL, NOW)
            self.assertEqual(cf.parse_comment(posts(gh)[0][2]["body"])["n"], 4, assoc)

    def test_reset_before_the_markers_does_not_reset(self):
        gh = storage_gh(comments={113: three_failures() + [self.reset("OWNER", at="2026-10-23T04:00:00Z")]})
        cf.run(gh, "workflow_run", event(), FULL, NOW)
        self.assertEqual(cf.parse_comment(posts(gh)[0][2]["body"])["n"], 4)

    def test_needs_human_removal_restarts_the_count(self):
        ev = [{"event": "unlabeled", "label": {"name": "needs-human"}, "created_at": "2026-10-23T05:30:00Z"}]
        gh = storage_gh(comments={113: three_failures()}, events={113: ev})
        cf.run(gh, "workflow_run", event(), FULL, NOW)
        self.assertEqual(cf.parse_comment(posts(gh)[0][2]["body"])["n"], 1)


class Pagination(unittest.TestCase):
    def test_full_last_page_fails_closed(self):
        gh = cf.Gh(pages=2)
        gh.get = lambda path: [{"n": 1}] * 100
        with self.assertRaises(RuntimeError):
            gh.get_all("repos/x?per_page=100")

    def test_short_page_ends_the_list(self):
        gh = cf.Gh(pages=3)
        pages = {1: [{}] * 100, 2: [{}] * 5}
        gh.get = lambda path: pages[int(path.rsplit("=", 1)[1])]
        self.assertEqual(len(gh.get_all("repos/x?per_page=100")), 105)


class JobsLag(unittest.TestCase):
    """#443: right after a failure the jobs API can list no failed job yet."""

    def setUp(self):
        self.sleeps = []
        p = mock.patch.object(cf.time, "sleep", side_effect=self.sleeps.append)
        p.start()
        self.addCleanup(p.stop)

    def lagging(self, ready_at, conclusion="failure"):
        gh = storage_gh()
        gh.jobs[(500, 1)] = lambda reads: ([job(1, "test"), job(2, "lint", conclusion="success")]
                                           if reads >= ready_at else [job(2, "lint", conclusion="success")])
        return gh, cf.run(gh, "workflow_run", event(conclusion=conclusion), FULL, NOW)

    def test_a_failed_job_listed_on_a_retry_is_reported(self):
        gh, out = self.lagging(ready_at=2)
        self.assertEqual(out["action"], "post")
        rec = cf.parse_comment(posts(gh)[0][2]["body"])
        self.assertEqual([j["job"] for j in rec["failed_jobs"]], ["test"])
        self.assertEqual((gh.job_reads, self.sleeps), (2, [5]))

    def test_retries_are_bounded_and_then_say_the_list_was_unavailable(self):
        gh, out = self.lagging(ready_at=99)
        self.assertEqual(gh.job_reads, cf.JOBS_TRIES)
        self.assertLessEqual(sum(self.sleeps), 30)
        body = posts(gh)[0][2]["body"]
        self.assertEqual(cf.parse_comment(body)["failed_jobs"], [])
        self.assertIn("Failed jobs: unavailable", body)
        self.assertNotIn("none reported", body)

    def test_the_normal_path_reads_the_jobs_once(self):
        gh = storage_gh()
        cf.run(gh, "workflow_run", event(), FULL, NOW)
        self.assertEqual((gh.job_reads, self.sleeps), (1, []))

    def test_a_startup_failure_without_jobs_is_not_retried(self):
        gh = storage_gh()
        gh.jobs[(500, 1)] = []
        cf.run(gh, "workflow_run", event(conclusion="startup_failure"), FULL, NOW)
        self.assertEqual((gh.job_reads, self.sleeps), (1, []))
        self.assertIn("none reported (the run failed before any job ran)", posts(gh)[0][2]["body"])


class GhRetry(unittest.TestCase):
    """Bounded backoff (ported from #421): reads and idempotent writes retry, comment writes do not."""

    def setUp(self):
        self.calls, self.sleeps = [], []
        p1 = mock.patch.object(cf.subprocess, "run", side_effect=self.fake_run)
        p2 = mock.patch.object(cf.time, "sleep", side_effect=self.sleeps.append)
        p1.start(), p2.start()
        self.addCleanup(mock.patch.stopall)
        self.results = []

    def fake_run(self, args, **kw):
        self.calls.append(args)
        r = self.results.pop(0) if self.results else ("fail", "HTTP 502")
        if r[0] == "timeout":
            raise subprocess.TimeoutExpired(args, 60)
        return subprocess.CompletedProcess(args, 0 if r[0] == "ok" else 1, stdout=r[1], stderr=r[1])

    def test_a_read_retries_transient_failures_with_backoff(self):
        self.results = [("fail", "HTTP 502"), ("timeout", ""), ("ok", "[1]")]
        self.assertEqual(cf.Gh().get("repos/x"), [1])
        self.assertEqual((len(self.calls), self.sleeps), (3, [2, 4]))

    def test_a_read_fails_closed_after_a_bounded_number_of_tries(self):
        with self.assertRaises(RuntimeError):
            cf.Gh(tries=3).get("repos/x")
        self.assertEqual(len(self.calls), 3)

    def test_comment_writes_are_never_retried(self):
        gh = cf.Gh()
        with self.assertRaises(RuntimeError):
            gh.post("repos/x/issues/1/comments", {"body": "b"})
        with self.assertRaises(RuntimeError):
            gh.patch("repos/x/issues/comments/1", {"body": "b"})
        self.assertEqual((len(self.calls), self.sleeps), (2, []))

    def test_an_idempotent_write_retries_then_succeeds(self):
        self.results = [("fail", "HTTP 502"), ("ok", "{}")]
        cf.Gh().post("repos/x/issues/1/labels", {"labels": ["needs-human"]}, retry=True)
        self.assertEqual((len(self.calls), self.sleeps), (2, [2]))

    def test_an_idempotent_write_still_fails_closed(self):
        with self.assertRaises(RuntimeError):
            cf.Gh(tries=3).post("repos/x/issues/1/labels", {"labels": ["needs-human"]}, retry=True)
        self.assertEqual(len(self.calls), 3)

    def test_the_error_is_masked(self):
        self.results = [("fail", "bad token ghp_abcdefghijklmnop1234")] * 3
        with self.assertRaises(RuntimeError) as cm:
            cf.Gh().get("repos/x")
        self.assertNotIn("ghp_abcdefghijklmnop1234", str(cm.exception))


class JobsPagination(unittest.TestCase):
    """vws-deploy#432: a run with more than 100 jobs is read page by page, fail-closed."""

    def test_a_failed_job_past_the_first_100_is_reported(self):
        gh = storage_gh()
        gh.jobs[(500, 1)] = [job(i, f"matrix-{i}", conclusion="success") for i in range(1, 141)] + [job(141, "late")]
        out = cf.run(gh, "workflow_run", event(), FULL, NOW)
        self.assertEqual(out["action"], "post")
        rec = cf.parse_comment(posts(gh)[0][2]["body"])
        self.assertEqual([j["job_id"] for j in rec["failed_jobs"]], [141])
        self.assertEqual(gh.job_pages, [1, 2])

    def test_an_endless_job_list_fails_closed_without_a_comment(self):
        gh = storage_gh()
        gh.endless_jobs = True
        with self.assertRaises(RuntimeError):
            cf.run(gh, "workflow_run", event(), FULL, NOW)
        self.assertEqual(gh.writes, [])
        self.assertEqual(len(gh.job_pages), cf.JOBS_PAGES)


class VerifyGateOnly(unittest.TestCase):
    """vws-deploy#432: verify-gate is red on every head until vws-verify approves; alone it is not a CI failure."""

    def gh(self, *jobs):
        gh = storage_gh()
        gh.jobs[(700, 1)] = list(jobs)
        return gh

    def test_only_verify_gate_failed_is_skipped(self):
        for name in ("verify-gate", "guard / verify-gate"):
            gh = self.gh(job(9, name, workflow="repo-guard"), job(8, "pr-hygiene", workflow="repo-guard", conclusion="success"))
            out = cf.run(gh, "workflow_run", event(run_id=700, workflow="repo-guard"), FULL, NOW)
            self.assertEqual(out, {"action": "skip", "reason": "only verify-gate failed"}, name)
            self.assertEqual(gh.writes, [])

    def test_verify_gate_with_another_failed_job_still_posts(self):
        gh = self.gh(job(9, "verify-gate", workflow="repo-guard"), job(8, "pr-hygiene", workflow="repo-guard"))
        out = cf.run(gh, "workflow_run", event(run_id=700, workflow="repo-guard"), FULL, NOW)
        self.assertEqual(out["action"], "post")
        rec = cf.parse_comment(posts(gh)[0][2]["body"])
        self.assertEqual(sorted(j["job"] for j in rec["failed_jobs"]), ["pr-hygiene", "verify-gate"])

    def test_a_job_merely_named_like_verify_gate_is_not_ignored(self):
        gh = self.gh(job(9, "verify-gate-lint", workflow="repo-guard"))
        self.assertEqual(cf.run(gh, "workflow_run", event(run_id=700, workflow="repo-guard"), FULL, NOW)["action"], "post")


if __name__ == "__main__":
    unittest.main()
