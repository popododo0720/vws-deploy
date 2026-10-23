"""CI-failure rework (vws-deploy#412, orchestrator side): a `<!-- vws-ci-fail ... -->` comment by
github-actions[bot] on a DeepSeek PR's current head queues one rework on that PR."""
import importlib.util
import json
import os
import unittest

import test_runtime

from orch_testlib import CFG, ROOT, NOW, U, comment, event, item, orch, ready_task, report, ts

from test_rework import FAIL_BODY, dispatches, ds_pr, review, run

BOT = "github-actions[bot]"
CAP1 = dict(CFG, ORCHESTRATE_DEEPSEEK_CAP="1")
CAP3 = dict(CAP1, DEEPSEEK_MAX_FAILURES="3")


def marker(sha, repo="vws-iam", pr=253, owner="deepseek", agent=None, n=1, wake=True):
    agent = agent or ("deepseek-dev" if owner == "deepseek" else ("-" if owner == "none" else "vws-iam"))
    return (f"<!-- vws-ci-fail sha={sha} v=1 repo={repo} pr={pr} owner={owner} agent={agent} n={n} "
            f"wake={'true' if wake else 'false'} -->")


def ci_record(sha, repo="vws-iam", pr=253, owner="deepseek", agent=None, n=1, wake=True):
    """A #420 JSON record (.github/ci-fail/ci_fail.py record fields) that agrees with marker(...)."""
    agent = agent or ("deepseek-dev" if owner == "deepseek" else ("-" if owner == "none" else "vws-iam"))
    return {"v": 1, "repo": repo, "pr": pr, "head_sha": sha, "head_branch": "deepseek/fix-x", "owner": owner,
            "agent": "" if agent == "-" else agent, "owner_source": "label:deepseek", "chain": [pr], "n": n,
            "cap": 3, "wake": wake, "runs": [], "failed_jobs": [], "created_at": "2026-10-23T06:00:00Z",
            "updated_at": "2026-10-23T06:00:00Z"}


def json_block(rec):
    """The fenced record as #420 format_comment writes it (<, >, @, backtick and ~ as \\u escapes)."""
    blob = json.dumps(rec, indent=2, ensure_ascii=True)
    for ch in "<>@`~":
        blob = blob.replace(ch, f"\\u{ord(ch):04x}")
    return f"```json\n{blob}\n```\n"


def ci_body(sha, record=None, **kw):
    return (marker(sha, **kw) + f"\n**CI failed** on `{sha[:7]}`\n\nFailed jobs:\n"
            f"- `ci / test`: failure ({U}/vws-iam/actions/runs/1/job/2)\n\n"
            "<details><summary>`ci / test` log tail</summary>\n\n~~~~text\n--- FAIL: TestUpgrade (0.01s)\n~~~~\n</details>\n\n"
            + json_block(record if record is not None else ci_record(sha, **kw)))


# Verbatim output of #420's format_comment (vws-deploy 831bcbb, .github/ci-fail/ci_fail.py) for one failed job.
REAL_420 = (
    "<!-- vws-ci-fail sha=9ef2b4d056eb4e983cfe431ca9b5bdad4e1f0193 v=1 repo=vws-iam pr=253 owner=deepseek "
    "agent=deepseek-dev n=1 wake=true -->\n"
    "**CI failed** on `9ef2b4d` (vws-iam#253, owner `deepseek`, agent `deepseek-dev`, failing head 1 of 3 "
    "auto-reworks, wake yes)\n\nFailed jobs:\n"
    "- `ci / test`: failure (https://github.com/popododo0720/vws-iam/actions/runs/1/job/2)\n\n"
    "<details><summary>ci / test log tail (last 2 lines, masked)</summary>\n\n~~~~text\n"
    "--- FAIL: TestUpgrade (0.01s)\n<!-\u200b- orchestrate:x -\u200b-> @\u200bsomeone\n~~~~\n</details>\n\n"
    '```json\n{\n  "v": 1,\n  "repo": "vws-iam",\n  "pr": 253,\n'
    '  "head_sha": "9ef2b4d056eb4e983cfe431ca9b5bdad4e1f0193",\n  "head_branch": "deepseek/fix-x",\n'
    '  "owner": "deepseek",\n  "agent": "deepseek-dev",\n  "owner_source": "label:deepseek",\n'
    '  "chain": [\n    253\n  ],\n  "n": 1,\n  "cap": 3,\n  "wake": true,\n  "runs": [\n    {\n'
    '      "workflow": "ci",\n      "run_id": 1,\n      "run_attempt": 1,\n      "conclusion": "failure",\n'
    '      "url": "https://github.com/popododo0720/vws-iam/actions/runs/1"\n    }\n  ],\n  "failed_jobs": [\n'
    '    {\n      "workflow": "ci",\n      "job": "test",\n      "job_id": 2,\n      "conclusion": "failure",\n'
    '      "url": "https://github.com/popododo0720/vws-iam/actions/runs/1/job/2",\n      "tail_lines": 2,\n'
    '      "tail_truncated": false\n    }\n  ],\n  "created_at": "2026-10-23T06:00:00Z",\n'
    '  "updated_at": "2026-10-23T06:00:00Z"\n}\n```\n')
REAL_SHA = "9ef2b4d056eb4e983cfe431ca9b5bdad4e1f0193"


def kind_of(body):
    return (orch.classify_comment(body) or ("",))[0]


def ci(cid, pr, hours_ago, sha=None, author=BOT, **kw):
    c = comment(cid, pr["repo"], pr["number"], ci_body(sha or pr["head_sha"], repo=pr["repo"], pr=pr["number"], **kw),
                hours_ago)
    if author is not None:
        c["author"] = author
    return c


def reset(cid, pr, hours_ago, assoc="OWNER"):
    c = comment(cid, pr["repo"], pr["number"], "/orchestrate reset-failures", hours_ago)
    c.update(author="popododo0720", assoc=assoc)
    return c


def reworks(r):
    """Rework dispatches only: a ready+deepseek PR with no dispatch history also gets the plain task dispatch."""
    return [a for a in dispatches(r) if orch.REWORK_MARKER in a["body"]]


def notes(r, kind):
    return [n for n in r["notes"] if n["kind"] == kind]


class Parse(unittest.TestCase):
    def test_spec_example_line_parses(self):
        sha = "9ef2b4d056eb4e983cfe431ca9b5bdad4e1f0193"
        kind, d = orch.classify_comment(ci_body(sha, repo="vws-storage", pr=113, n=3))
        self.assertEqual(kind, "ci_fail")
        self.assertEqual((d["sha"], d["repo"], d["pr"], d["owner"], d["agent"], d["n"], d["wake"]),
                         (sha, "vws-storage", 113, "deepseek", "deepseek-dev", 3, True))
        self.assertNotIn("state", d)

    def test_state_key_is_not_part_of_the_marker(self):
        # #420 docs/ci-fail.md: "There is no passed state". MARKER_RE has no state= group.
        body = ci_body("a" * 40)
        for st in ("failed", "passed"):
            with self.subTest(state=st):
                self.assertNotEqual(kind_of(body.replace(" -->", f" state={st} -->", 1)), "ci_fail")

    def test_ci_fail_re_is_the_420_marker_re(self):
        self.assertEqual(orch.CI_FAIL_RE.pattern,
                         r"<!-- vws-ci-fail sha=([0-9a-f]{40}) v=(\d+) repo=(vws-[a-z0-9-]+) pr=(\d+) "
                         r"owner=(deepseek|grok|none) agent=([a-z0-9][a-z0-9-]{0,38}|-) n=(\d+) wake=(true|false) -->")

    def test_malformed_markers_are_not_ci_fail(self):
        good = ci_body("a" * 40)
        line = good.split("\n", 1)[0]
        for bad in (line.replace("v=1 repo", "repo"),  # missing key
                    line.replace(" v=1 repo=vws-iam", " repo=vws-iam v=1"),  # key order
                    line.replace("a" * 40, "a" * 39),  # short sha
                    line.replace("owner=deepseek", "owner=bob"),
                    line.replace("wake=true", "wake=yes"),
                    line.replace("vws-ci-fail sha", "vws-ci-fail  sha"),  # double space
                    line + " trailing"):
            with self.subTest(bad=bad):
                self.assertNotEqual(kind_of(good.replace(line, bad, 1)), "ci_fail")
        self.assertNotEqual(kind_of("text first\n" + good), "ci_fail")  # not line 1
        self.assertNotEqual(kind_of(" " + good), "ci_fail")  # #420: no leading space


class Contract420(unittest.TestCase):
    """#420 parse_comment: CRLF normalized, the LAST json block is the record, marker_line(record) must
    equal line 1, and the record's field types must hold."""

    def test_real_420_comment_parses_to_its_record(self):
        kind, d = orch.classify_comment(REAL_420)
        self.assertEqual(kind, "ci_fail")
        rec = json.loads(REAL_420.split("```json\n", 1)[1].split("```", 1)[0])
        self.assertEqual(d["record"], rec)
        self.assertEqual((d["sha"], d["repo"], d["pr"], d["n"], d["wake"]), (REAL_SHA, "vws-iam", 253, 1, True))

    def test_marker_without_record_is_rejected(self):
        line = REAL_420.split("\n", 1)[0]
        for body in (line, line + "\n", REAL_420.split("```json")[0], REAL_420.replace("```json\n", "```\n")):
            with self.subTest(body=body[-40:]):
                self.assertNotEqual(kind_of(body), "ci_fail")

    def test_record_that_contradicts_line_one_is_rejected(self):
        sha = "a" * 40
        for field, val in (("wake", False), ("n", 4), ("owner", "grok"), ("pr", 254), ("repo", "vws-lib"),
                           ("head_sha", "b" * 40), ("v", 2), ("agent", "vws-iam")):
            with self.subTest(field=field):
                rec = dict(ci_record(sha), **{field: val})
                self.assertNotEqual(kind_of(ci_body(sha, record=rec)), "ci_fail")

    def test_crlf_body(self):
        kind, d = orch.classify_comment(REAL_420.replace("\n", "\r\n"))
        self.assertEqual(kind, "ci_fail")
        self.assertEqual(d["record"]["head_sha"], REAL_SHA)
        bad = ci_body("a" * 40, record=dict(ci_record("a" * 40), wake=False)).replace("\n", "\r\n")
        self.assertNotEqual(kind_of(bad), "ci_fail")

    def test_last_json_block_is_the_record(self):
        sha = "a" * 40
        good, bad = ci_record(sha), dict(ci_record(sha), n=4)
        # A trailing block that disagrees wins, so the comment is rejected.
        self.assertNotEqual(kind_of(ci_body(sha) + "\n" + json_block(bad)), "ci_fail")
        # An earlier block that disagrees is ignored when the last one agrees.
        body = ci_body(sha).replace("```json", json_block(bad) + "\n```json", 1)
        kind, d = orch.classify_comment(body)
        self.assertEqual(kind, "ci_fail")
        self.assertEqual(d["record"], good)

    def test_bad_record_types_are_rejected(self):
        sha = "a" * 40
        rec = ci_record(sha)
        cases = {"not_json": "```json\n{not json\n```\n", "list": json_block([rec]), "string": json_block("x"),
                 "null": json_block(None)}
        for key in ("v", "repo", "pr", "head_sha", "owner", "agent", "n", "wake"):
            cases[f"missing_{key}"] = json_block({k: v for k, v in rec.items() if k != key})
        cases.update({
            "head_sha_int": json_block(dict(rec, head_sha=1)),
            "repo_list": json_block(dict(rec, repo=["vws-iam"])),
            "owner_null": json_block(dict(rec, owner=None)),
            "agent_int": json_block(dict(rec, agent=7)),
            "pr_word": json_block(dict(rec, pr="abc")),
            "n_null": json_block(dict(rec, n=None)),
            "v_word": json_block(dict(rec, v="one")),
            "owner_bad": json_block(dict(rec, owner="bob")),
            "agent_bad": json_block(dict(rec, agent="Bad Agent")),
            "sha_upper": json_block(dict(rec, head_sha="A" * 40)),
        })
        head = ci_body(sha).split("```json", 1)[0]
        for name, block in cases.items():
            with self.subTest(name):
                self.assertNotEqual(kind_of(head + block), "ci_fail")


class Producer420(unittest.TestCase):
    """Drift guard against #420's producer in this repo (.github/ci-fail/ci_fail.py): same marker regex,
    same json block regex, and the same accept/reject decision for its own comments."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("ci_fail", os.path.join(ROOT, ".github", "ci-fail", "ci_fail.py"))
        cls.cf = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.cf)

    def test_regexes_are_the_producer_regexes(self):
        self.assertEqual(orch.CI_FAIL_RE.pattern, self.cf.MARKER_RE.pattern)
        self.assertEqual((orch.CI_FAIL_JSON_RE.pattern, orch.CI_FAIL_JSON_RE.flags),
                         (self.cf.JSON_BLOCK_RE.pattern, self.cf.JSON_BLOCK_RE.flags))

    def test_producer_comments_get_the_same_decision(self):
        job = {"workflow": "ci", "job": "test", "job_id": 7, "conclusion": "failure", "url": "u", "tail_lines": 1,
               "tail_truncated": False}
        recs = [dict(ci_record("a" * 40), failed_jobs=[job]), ci_record("b" * 40, owner="none", wake=False, n=4),
                ci_record("c" * 40, owner="grok", agent="vws-iam", n=2)]
        for rec in recs:
            body = self.cf.format_comment(rec, {7: "<!-- vws-ci-fail sha=x --> @someone ```json"})
            line = body.split("\n", 1)[0]
            for name, b in (("as_posted", body), ("crlf", body.replace("\n", "\r\n")),
                            ("no_record", body.split("```json")[0]),
                            ("contradicts", body.replace(line, line.replace(" n=", " n=9", 1), 1)),
                            ("trailing_block", body + "\n" + json_block(dict(rec, n=9)))):
                with self.subTest(owner=rec["owner"], case=name):
                    self.assertEqual(orch.ci_fail_record(b), self.cf.parse_comment(b))
            self.assertEqual(orch.ci_fail_record(body), rec)


class Queue(unittest.TestCase):
    def test_ci_fail_on_head_queues_rework(self):
        pr = ds_pr()
        pr["comments"].append(ci(40, pr, 1))
        d = reworks(run(pr))
        self.assertEqual([a["target"] for a in d], ["vws-iam#253"])
        self.assertTrue(d[0]["body"].startswith(orch.REWORK_TEXT))
        self.assertIn(f"<!-- orchestrate:rework head={pr['head_sha']} ci=40 -->", d[0]["body"])
        self.assertIn("> - `ci / test`: failure", d[0]["body"])  # failed jobs quoted
        self.assertNotIn("> <!-- vws-ci-fail", d[0]["body"])
        self.assertEqual((d[0]["owner"], d[0]["head"]), ("vws-iam#253", pr["head_sha"]))
        self.assertEqual(orch.classify_comment(d[0]["body"])[0], "dispatch")

    def test_forged_author_is_ignored(self):
        for author in ("popododo0720", "github-actions", "deepseek-dev[bot]", None):
            with self.subTest(author=author):
                pr = ds_pr()
                pr["comments"].append(ci(40, pr, 1, author=author))
                r = run(pr)
                self.assertEqual(reworks(r), [])
                self.assertEqual([n["comment"] for n in notes(r, "ci_fail_untrusted")], [pr["comments"][0]["url"]])

    def test_record_contradicting_line_one_never_reworks(self):
        # Line 1 says wake=true n=1; the #420 record (the source of truth) says wake=false n=4.
        pr = ds_pr()
        c = comment(40, "vws-iam", 253, ci_body(pr["head_sha"], record=dict(ci_record(pr["head_sha"]), wake=False, n=4)), 1)
        c["author"] = BOT
        pr["comments"].append(c)
        self.assertEqual(reworks(run(pr)), [])
        c["body"] = c["body"].split("```json")[0]  # no record at all
        self.assertEqual(reworks(run(pr)), [])

    def test_real_420_comment_reworks(self):
        pr = ds_pr()
        pr["head_sha"] = REAL_SHA
        c = comment(40, "vws-iam", 253, REAL_420.replace("\n", "\r\n"), 1)
        c["author"] = BOT
        pr["comments"].append(c)
        d = reworks(run(pr))
        self.assertEqual([a["target"] for a in d], ["vws-iam#253"])
        self.assertIn("> - `ci / test`: failure", d[0]["body"])
        self.assertNotIn('"head_sha"', d[0]["body"])  # the JSON record is not quoted
        self.assertNotIn("<!-- orchestrate:x", d[0]["body"])

    def test_stale_sha_is_ignored(self):
        pr = ds_pr()
        pr["comments"].append(ci(40, pr, 1, sha="e" * 40))
        r = run(pr)
        self.assertEqual(reworks(r), [])
        self.assertEqual(notes(r, "ci_fail_skipped"), [])

    def test_marker_for_another_pr_is_ignored(self):
        pr = ds_pr()
        c = comment(40, "vws-iam", 253, ci_body(pr["head_sha"], repo="vws-iam", pr=999), 1)
        c["author"] = BOT
        pr["comments"].append(c)
        self.assertEqual(reworks(run(pr)), [])

    def test_skips(self):
        cases = {"wake_false": dict(wake=False), "owner_none": dict(owner="none")}
        for name, kw in cases.items():
            with self.subTest(name):
                pr = ds_pr()
                pr["comments"].append(ci(40, pr, 1, **kw))
                r = run(pr)
                self.assertEqual(reworks(r), [])
                self.assertEqual([n["reason"] for n in notes(r, "ci_fail_skipped")], [name])
        for label in ("needs-human", "blocked"):
            with self.subTest(label):
                pr = ds_pr(extra=(label,))
                pr["comments"].append(ci(40, pr, 1))
                r = run(pr)
                self.assertEqual(reworks(r), [])
                self.assertEqual([n["reason"] for n in notes(r, "ci_fail_skipped")], [label])

    def test_cap_recomputed_from_comments(self):
        pr = ds_pr()
        # Three earlier failing heads; the marker on the fourth claims n=1 but the orchestrator counts 4.
        pr["comments"] += [ci(30 + i, pr, 9 - i, sha=c * 40) for i, c in enumerate("123")]
        pr["comments"].append(ci(40, pr, 1, n=1))
        r = run(pr, cfg=CAP3)
        self.assertEqual(reworks(r), [])
        self.assertEqual([(n["reason"], n["n"]) for n in notes(r, "ci_fail_skipped")], [("cap", 4)])
        # A repeated marker for one sha is one head, not two.
        pr["comments"] = [ci(30, pr, 9, sha="1" * 40), ci(31, pr, 8, sha="1" * 40), ci(40, pr, 1)]
        self.assertEqual(len(reworks(run(pr, cfg=CAP3))), 1)

    def test_marker_n_can_only_tighten(self):
        pr = ds_pr()
        pr["comments"].append(ci(40, pr, 1, n=4))
        r = run(pr)
        self.assertEqual(reworks(r), [])
        self.assertEqual([n["reason"] for n in notes(r, "ci_fail_skipped")], ["cap"])

    def test_authorized_reset_resets_ci_cap(self):
        pr = ds_pr()
        pr["comments"] += [ci(30 + i, pr, 9 - i, sha=c * 40) for i, c in enumerate("123")]
        pr["comments"].append(reset(35, pr, 2, assoc="NONE"))
        pr["comments"].append(ci(40, pr, 1))
        self.assertEqual(reworks(run(pr, cfg=CAP3)), [], "an unauthorized reset does not reset the CI cap")
        pr["comments"][3] = reset(35, pr, 2, assoc="MEMBER")
        self.assertEqual(len(reworks(run(pr, cfg=CAP3))), 1)

    def test_authorized_reset_helper(self):
        r = "/orchestrate reset-failures"
        for assoc, want in (("OWNER", True), ("MEMBER", True), ("COLLABORATOR", True), ("CONTRIBUTOR", False),
                            ("NONE", False), ("", False), (None, False)):
            with self.subTest(assoc=assoc):
                self.assertIs(orch.authorized_reset({"body": r, "assoc": assoc}), want)
        self.assertFalse(orch.authorized_reset({"body": r}))
        self.assertFalse(orch.authorized_reset({"body": "please /orchestrate reset-failures", "assoc": "OWNER"}))

    def test_needs_human_removal_resets_ci_cap(self):
        pr = ds_pr(events=[event("labeled", "needs-human", 3), event("unlabeled", "needs-human", 2)])
        pr["comments"] += [ci(30 + i, pr, 9 - i, sha=c * 40) for i, c in enumerate("123")]
        pr["comments"].append(ci(40, pr, 1))
        self.assertEqual(len(reworks(run(pr, cfg=CAP3))), 1)

    def test_deepseek_cap_and_dispatch_switch(self):
        pr = ds_pr()
        pr["comments"].append(ci(40, pr, 1))
        busy = item("vws-proto", 51, comments=[comment(1, "vws-proto", 51, orch.DISPATCH_TEXT, 1)])
        self.assertEqual(reworks(run(pr, busy)), [])
        self.assertEqual(len(reworks(run(pr, busy, cfg=dict(CAP1, ORCHESTRATE_DEEPSEEK_CAP="2")))), 1)
        self.assertEqual(reworks(run(pr, cfg=dict(CAP1, ORCHESTRATE_DISPATCH="off"))), [])

    def test_grok_owner_is_only_logged(self):
        pr = item("vws-iam", 253, labels=("ready", "grok"), is_pr=True)
        pr["comments"].append(ci(40, pr, 1, owner="grok"))
        r = run(pr)
        self.assertEqual(reworks(r), [])
        self.assertEqual(notes(r, "ci_fail_grok_pending"),
                         [{"kind": "ci_fail_grok_pending", "target": "vws-iam#253", "comment": pr["comments"][0]["url"],
                           "sha": pr["head_sha"], "agent": "vws-iam"}])


class Dedupe(unittest.TestCase):
    def test_verify_rework_done_blocks_ci_rework(self):
        pr = ds_pr(reviews=[lambda p: review(9, p, "CHANGES_REQUESTED", 3)])
        done = f"{orch.REWORK_TEXT}\n\n<!-- orchestrate:rework head={pr['head_sha']} verdict=9 -->"
        pr["comments"] += [comment(20, "vws-iam", 253, done, 2.5), ci(40, pr, 1)]
        r = run(pr, cfg=CAP3)
        self.assertEqual(reworks(r), [])
        self.assertEqual([n["reason"] for n in r["notes"] if n["kind"] == "rework_skipped"], ["done"])

    def test_ci_rework_done_blocks_verify_rework(self):
        pr = ds_pr(reviews=[lambda p: review(9, p, "CHANGES_REQUESTED", 0.5)])
        done = f"{orch.REWORK_TEXT}\n\n<!-- orchestrate:rework head={pr['head_sha']} ci=40 -->"
        pr["comments"] += [ci(40, pr, 3), comment(41, "vws-iam", 253, done, 2.5)]
        self.assertEqual(reworks(run(pr, cfg=CAP3)), [])

    def test_both_pending_one_rework_counted_once(self):
        pr = ds_pr(reviews=[lambda p: review(9, p, "CHANGES_REQUESTED", 1)])
        pr["comments"].append(ci(40, pr, 1.5))
        plan = orch.rework_plan("vws-iam#253", pr, {"vws-iam#253": pr}, 0)
        self.assertEqual(len(plan["counted"]), 1)
        d = reworks(run(pr))
        self.assertEqual(len(d), 1)
        self.assertIn(f"<!-- orchestrate:rework head={pr['head_sha']} verdict=9 ci=40 -->", d[0]["body"])
        self.assertIn("> - actions.go:137", d[0]["body"])
        self.assertIn("> - `ci / test`: failure", d[0]["body"])

    def test_ci_reworks_count_toward_grok_swap(self):
        pr = ds_pr()
        first = f"{orch.REWORK_TEXT}\n\n<!-- orchestrate:rework head={'c' * 40} ci=30 -->"
        pr["comments"] += [ci(30, pr, 6, sha="c" * 40), comment(31, "vws-iam", 253, first, 5), ci(40, pr, 1)]
        r = run(pr)
        self.assertEqual(reworks(r), [])
        swap = [a for a in r["actions"] if a["type"] == "swap_label"]
        self.assertEqual(len(swap), 1)
        self.assertIn("ci_fail_rework", swap[0]["body"])
        self.assertIn("ci_fail)", swap[0]["body"])


class Collect(unittest.TestCase):
    def test_comment_record_keeps_author(self):
        c = {"id": 1, "html_url": "u", "created_at": ts(1), "body": "b", "user": {"login": BOT},
             "author_association": "NONE"}
        self.assertEqual(orch.comment_record(c), {"id": 1, "url": "u", "created_at": ts(1), "body": "b",
                                                  "author": BOT, "assoc": "NONE"})

    def test_history_cache_without_authors_is_reread(self):
        self.assertGreaterEqual(orch.HISTORY_VERSION, 3)


class CiFailTick(test_runtime.Env):
    """A github-actions[bot] marker on the head -> one rework posted on the PR through apply."""
    O = test_runtime.O

    def setup_pr(self, head, comments, updated_hours_ago=2):
        r = test_runtime.routes()
        ready = [{"name": "ready"}, {"name": "deepseek"}]
        r[f"{self.O}/vws-iam/issues"] = {"body": [{"number": 7, "html_url": f"{U}/vws-iam/pull/7", "state": "open",
                                                   "created_at": "2026-10-01T00:00:00Z", "body": "", "labels": ready,
                                                   "pull_request": {}, "updated_at": orch.iso(NOW - 3600 * updated_hours_ago)}]}
        r[f"{self.O}/vws-iam/pulls"] = {"body": [{"number": 7, "draft": False, "base": {"ref": "main"}, "head": {"sha": head}}]}
        r[f"{self.O}/vws-iam/issues/7"] = {"body": {"state": "open", "labels": ready}}
        r[f"{self.O}/vws-iam/pulls/7"] = {"body": {"state": "open", "head": {"sha": head}}}
        r[f"{self.O}/vws-iam/pulls/7/reviews"] = {"body": []}
        r[f"{self.O}/vws-iam/issues/7/comments"] = {"body": list(comments)}
        self.set_routes(r)

    def gh_comment(self, cid, body, login, at="2026-10-21T10:00:00Z"):
        return {"id": cid, "html_url": f"{U}/vws-iam/pull/7#issuecomment-{cid}", "created_at": at,
                "body": body, "user": {"login": login}, "author_association": "NONE"}

    def posted(self):
        return [json.loads(c["stdin"])["body"] for c in self.writes() if c["path"] == f"{self.O}/vws-iam/issues/7/comments"]

    def history(self):
        d = self.gh_comment(50, orch.DISPATCH_TEXT, "popododo0720", "2026-10-21T08:00:00Z")
        done = self.gh_comment(51, f"[deepseek-dev] Done for {d['html_url']}\nPR: {U}/vws-iam/pull/7", "popododo0720",
                               "2026-10-21T09:00:00Z")
        return [d, done]

    def test_bot_marker_posts_rework_forged_does_not(self):
        h = "1" * 40
        self.setup_pr(h, self.history() + [self.gh_comment(60, ci_body(h, repo="vws-iam", pr=7), "someone")])
        self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_DEEPSEEK_CAP="1")
        self.assertEqual(self.posted(), [])
        os.remove(self.calls_file)
        # A new comment bumps updated_at, so the history cache re-reads the comments.
        self.setup_pr(h, self.history() + [self.gh_comment(61, ci_body(h, repo="vws-iam", pr=7), BOT)], updated_hours_ago=1)
        self.tick(ORCHESTRATE_MODE="apply", ORCHESTRATE_DEEPSEEK_CAP="1")
        bodies = self.posted()
        self.assertEqual(len(bodies), 1)
        self.assertIn(f"orchestrate:rework head={h} ci=61 -->", bodies[0])


if __name__ == "__main__":
    unittest.main()
