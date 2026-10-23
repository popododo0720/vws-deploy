"""Tests for .github/ac/ac-lint.sh (ported from the box vws-issue-ac skill) and the post-merge
ac-pending labeler (.github/ac/post-merge.sh, .github/workflows/post-merge.yml), vws-deploy#399.

Run: python3 -m unittest discover -s tests/workflows
"""
import json
import os
import re
import stat
import subprocess
import tempfile
import unittest

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LINT = os.path.join(ROOT, ".github", "ac", "ac-lint.sh")
POST = os.path.join(ROOT, ".github", "ac", "post-merge.sh")
GUARD = os.path.join(ROOT, "scripts", "guard-agents.sh")
WF_PATH = os.path.join(ROOT, ".github", "workflows", "post-merge.yml")

OPEN_BODY = "## Acceptance criteria\n- [x] parser (evidence: tests/x_test.go:12)\n- [ ] live run on vws-test\n"
DONE_BODY = ("## Acceptance criteria\n- [x] parser (evidence: tests/x_test.go:12)\n"
             "- [x] live run\n  evidence: /workspace/vws-test-x.txt lines 3-9\n")
NOEV_BODY = "- [x] parser works\n- [x] also done, see https://github.com/o/r/actions/runs/1\n"
NO_AC_BODY = "Just a description, no boxes.\n"


def lint(body):
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(body)
    try:
        return subprocess.run(["bash", LINT, "--file", f.name], capture_output=True, text=True)
    finally:
        os.unlink(f.name)


class AcLint(unittest.TestCase):
    def test_open_box_is_listed_and_exit_0(self):
        r = lint(OPEN_BODY)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"(?m)^OK +\[x\] parser")
        self.assertRegex(r.stdout, r"(?m)^OPEN +\[ \] live run")
        self.assertIn("2 boxes, 0 ticked without evidence", r.stdout)

    def test_indented_evidence_counts(self):
        r = lint(DONE_BODY)
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("OPEN", r.stdout)
        self.assertNotIn("NO-EVIDENCE", r.stdout)

    def test_tick_without_evidence_exits_1(self):
        r = lint(NOEV_BODY)
        self.assertEqual(r.returncode, 1)
        self.assertRegex(r.stdout, r"(?m)^NO-EVIDENCE +\[x\] parser works")
        self.assertRegex(r.stdout, r"(?m)^OK +\[x\] also done")

    def test_evidence_kinds(self):
        for ev in ("https://x.y/z", "/workspace/a.txt", "foo.go:12", "#123", "vws-iam#4", "TestParse"):
            with self.subTest(ev=ev):
                self.assertRegex(lint(f"- [x] item {ev}\n").stdout, r"(?m)^OK ")

    def test_crlf_and_star_bullets(self):
        r = lint("* [X] item #1\r\n* [ ] open one\r\n")
        self.assertRegex(r.stdout, r"(?m)^OK +\[X\] item")
        self.assertRegex(r.stdout, r"(?m)^OPEN ")

    def test_no_boxes(self):
        r = lint(NO_AC_BODY)
        self.assertEqual(r.returncode, 0)
        self.assertIn("0 boxes", r.stdout)

    def test_usage_error(self):
        self.assertEqual(subprocess.run(["bash", LINT], capture_output=True).returncode, 2)


class Stub:
    """A PATH dir with a fake gh: `gh api repos/R/issues/N` answers from issues.json; every call is logged."""

    def __init__(self, test, issues, fail_label=False):
        self.dir = tempfile.mkdtemp()
        test.addCleanup(subprocess.run, ["rm", "-rf", self.dir])
        self.log = os.path.join(self.dir, "gh.log")
        with open(os.path.join(self.dir, "issues.json"), "w") as f:
            json.dump(issues, f)
        gh = f'''#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
open({self.log!r}, "a").write(json.dumps(args) + "\\n")
issues = json.load(open({os.path.join(self.dir, "issues.json")!r}))
if args[:1] == ["api"] and "-X" not in args and "--method" not in args:
    path = args[1]
    if path in issues:
        print(json.dumps(issues[path]))
        sys.exit(0)
    print('{{"message": "Not Found"}}', file=sys.stderr)
    sys.exit(1)
if {fail_label!r}:
    sys.exit(1)
print("[]")
'''
        p = os.path.join(self.dir, "gh")
        with open(p, "w") as f:
            f.write(gh)
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)

    def calls(self):
        if not os.path.exists(self.log):
            return []
        return [json.loads(line) for line in open(self.log)]

    def writes(self):
        return [c for c in self.calls() if "-X" in c or "--method" in c]

    def run(self, pr_body, repo="popododo0720/vws-iam", **extra):
        body = os.path.join(self.dir, "pr-body.md")
        with open(body, "w") as f:
            f.write(pr_body)
        summary = os.path.join(self.dir, "summary.md")
        env = dict(os.environ, PATH=self.dir + ":" + os.environ["PATH"], REPO=repo, PR_BODY_FILE=body,
                   GITHUB_STEP_SUMMARY=summary, **extra)
        r = subprocess.run(["bash", POST], env=env, capture_output=True, text=True)
        r.summary = open(summary).read() if os.path.exists(summary) else ""
        return r


def issue(body, state="open", pr=False):
    d = {"state": state, "body": body, "labels": []}
    if pr:
        d["pull_request"] = {"url": "x"}
    return d


I = "repos/popododo0720/vws-iam/issues/"
LABEL_CALL = ["api", "-X", "POST", I + "{n}/labels", "-f", "labels[]=ac-pending"]


def label_call(n):
    return [a.replace("{n}", str(n)) for a in LABEL_CALL]


class PostMerge(unittest.TestCase):
    def test_unchecked_ac_gets_label(self):
        s = Stub(self, {I + "7": issue(OPEN_BODY)})
        r = s.run("Fixes a thing.\n\nPart of #7\n\nAgent: vws-platform\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(s.writes(), [label_call(7)])
        self.assertRegex(r.summary, r"#7 .*ac-pending.*OPEN=1 NO-EVIDENCE=0")

    def test_no_evidence_tick_gets_label(self):
        s = Stub(self, {I + "8": issue(NOEV_BODY)})
        r = s.run("Part of #8")
        self.assertEqual(s.writes(), [label_call(8)])
        self.assertRegex(r.summary, r"OPEN=0 NO-EVIDENCE=1")

    def test_all_checked_with_evidence_gets_none(self):
        s = Stub(self, {I + "9": issue(DONE_BODY)})
        r = s.run("Part of #9")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(s.writes(), [])
        self.assertRegex(r.summary, r"#9 .*complete")

    def test_issue_without_boxes_gets_none(self):
        s = Stub(self, {I + "3": issue(NO_AC_BODY)})
        s.run("Part of #3")
        self.assertEqual(s.writes(), [])

    def test_several_refs_deduped_same_repo_name_form(self):
        s = Stub(self, {I + "7": issue(OPEN_BODY), I + "9": issue(DONE_BODY), I + "10": issue(OPEN_BODY)})
        s.run("Part of #7\nPart of vws-iam#10\npart of #7\nPart of #9\n")
        self.assertEqual(s.writes(), [label_call(7), label_call(10)])

    def test_other_repo_ref_is_skipped(self):
        s = Stub(self, {})
        r = s.run("Part of vws-proto#5\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(s.calls(), [])
        self.assertIn("vws-proto#5", r.summary)
        self.assertIn("other repo", r.summary)

    def test_closed_issue_and_pr_refs_skipped(self):
        s = Stub(self, {I + "4": issue(OPEN_BODY, state="closed"), I + "5": issue(OPEN_BODY, pr=True)})
        s.run("Part of #4\nPart of #5\n")
        self.assertEqual(s.writes(), [])

    def test_no_part_of_does_nothing(self):
        s = Stub(self, {})
        r = s.run("Closes nothing here.\n")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(s.calls(), [])

    def test_refs_resolve_like_guard_agents(self):
        # vws-deploy#418: every form guard-agents.sh pr-body accepts (PART_OF_RE) resolves here too.
        s = Stub(self, {I + n: issue(OPEN_BODY) for n in ("7", "8", "9", "11")})
        r = s.run("This change is Part of #7 in prose.\n"
                  "Part of popododo0720/vws-iam#8\n"
                  "(part of POPODODO0720/VWS-IAM#9)\n"
                  "Part of vws-iam#11\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(s.writes(), [label_call(n) for n in (7, 8, 9, 11)])

    def test_one_issue_in_three_forms_is_read_once(self):
        s = Stub(self, {I + "7": issue(OPEN_BODY)})
        s.run("Part of #7\nPart of vws-iam#7\nPart of popododo0720/vws-iam#7\n")
        self.assertEqual([c for c in s.calls() if c == ["api", I + "7"]], [["api", I + "7"]])
        self.assertEqual(s.writes(), [label_call(7)])

    def test_other_owner_or_repo_with_owner_is_skipped(self):
        s = Stub(self, {})
        r = s.run("Part of popododo0720/vws-proto#5\nPart of someone/vws-iam#6\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(s.calls(), [])
        self.assertIn("popododo0720/vws-proto#5 skipped, other repo", r.summary)
        self.assertIn("someone/vws-iam#6 skipped, other repo", r.summary)

    def test_hidden_or_zero_refs_do_not_count(self):
        # guard-agents.sh ignores HTML comments and code fences, and #0 names no issue.
        s = Stub(self, {I + n: issue(OPEN_BODY) for n in ("7", "8", "9")})
        s.run("<!-- Part of #7 -->\n```\nPart of #8\n```\nPart of #0\nnot apart of #9\n")
        self.assertEqual(s.calls(), [])

    # vws-verify FAIL r1 on PR #429 (/workspace/handoff/verdicts/d429): guard-agents.sh pr-body sees
    # "Part of #7" in each of these bodies, so post-merge must read #7 too.
    GUARD_REGRESSIONS = [
        ("fence line inside an HTML comment", "<!--\n```\n-->\nPart of #7\n"),
        ("backtick fence inside a tilde fence", "~~~\n```\n~~~\nPart of #7\n"),
        ("shorter fence inside a longer one", "````\n```\n````\nPart of #7\n"),
    ]

    def test_guard_agents_regression_bodies(self):
        for name, body in self.GUARD_REGRESSIONS:
            with self.subTest(name):
                ok = subprocess.run(["bash", GUARD, "pr-body"], env=dict(os.environ, PR_BODY=body),
                                    capture_output=True, text=True)
                self.assertEqual(ok.returncode, 0, "guard-agents accepts it: " + ok.stderr)
                s = Stub(self, {I + "7": issue(OPEN_BODY)})
                r = s.run(body)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(s.writes(), [label_call(7)])
                self.assertNotIn("No Part of #N", r.summary)

    def test_hidden_text_comes_from_guard_agents_not_a_copy(self):
        # One parser: post-merge.sh calls guard-agents.sh visible_text and PART_OF_RE, with no fence
        # or comment handling of its own.
        text = open(POST).read()
        self.assertIn("visible_text", text)
        self.assertIn("PART_OF_RE", text)
        self.assertNotIn("<!--", text)
        self.assertNotRegex(text, r"```|~~~|fence")
        self.assertNotIn("part of[[:space:]]", text.lower())

    def test_missing_guard_agents_fails_closed(self):
        for name, guard in [("missing", "/nonexistent/guard-agents.sh"), ("no visible_text", "/dev/null")]:
            with self.subTest(name):
                s = Stub(self, {I + "7": issue(OPEN_BODY)})
                r = s.run("Part of #7\n", GUARD_AGENTS=guard)
                self.assertNotEqual(r.returncode, 0, "never read as 'no Part of #N'")
                self.assertEqual(s.calls(), [])
                self.assertIn("guard-agents", r.stderr)

    def test_lint_exit_2_with_summary_is_a_crash(self):
        # A summary line does not make exit 2 a normal result (lrc > 1, not lrc > 2).
        self._crash_case("echo '0 boxes, 0 ticked without evidence'; exit 2", r"exit 2")

    def test_lint_two_summary_lines_is_a_crash(self):
        # The real ac-lint prints exactly one summary line; two mean corrupt or concatenated output.
        self._crash_case("printf '1 boxes, 0 ticked without evidence\\n0 boxes, 0 ticked without evidence\\n'; exit 0",
                         r"exit 0")

    def _crash_case(self, script, exit_re):
        crash = os.path.join(tempfile.mkdtemp(), "lint.sh")
        self.addCleanup(subprocess.run, ["rm", "-rf", os.path.dirname(crash)])
        with open(crash, "w") as f:
            f.write("#!/usr/bin/env bash\ncat >/dev/null\n" + script + "\n")
        os.chmod(crash, 0o755)
        s = Stub(self, {I + "9": issue(DONE_BODY)})
        r = s.run("Part of #9\n", AC_LINT=crash)
        self.assertNotEqual(r.returncode, 0, "the job fails")
        self.assertEqual(s.writes(), [label_call(9)], "and the issue is marked ac-pending")
        self.assertRegex(r.summary, r"#9 ac-lint failed \(" + exit_re + r"\)")
        self.assertNotIn("complete", r.summary)

    def test_lint_crash_fails_closed(self):
        # vws-deploy#418: ac-lint dying (exit 2+, or no summary line) is never "all AC met".
        crash = os.path.join(tempfile.mkdtemp(), "lint.sh")
        self.addCleanup(subprocess.run, ["rm", "-rf", os.path.dirname(crash)])
        for name, script in [("exit 2", "echo boom >&2; exit 2"), ("killed", "kill -9 $$"),
                             ("exit 0 without output", "exit 0"), ("exit 1 without output", "exit 1")]:
            with self.subTest(name):
                with open(crash, "w") as f:
                    f.write("#!/usr/bin/env bash\ncat >/dev/null\n" + script + "\n")
                os.chmod(crash, 0o755)
                s = Stub(self, {I + "9": issue(DONE_BODY)})
                r = s.run("Part of #9\n", AC_LINT=crash)
                self.assertNotEqual(r.returncode, 0, "the job fails")
                self.assertEqual(s.writes(), [label_call(9)], "and the issue is marked ac-pending")
                self.assertRegex(r.summary, r"#9 ac-lint failed \(exit [0-9]+\)")
                self.assertNotIn("complete", r.summary)

    def test_lint_exit_1_is_no_evidence_not_a_crash(self):
        s = Stub(self, {I + "8": issue(NOEV_BODY)})
        r = s.run("Part of #8\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("ac-lint failed", r.summary)

    def test_fork_pr_is_skipped_with_a_log(self):
        for head in ("someone/vws-iam", ""):
            with self.subTest(head=head or "deleted fork"):
                s = Stub(self, {I + "7": issue(OPEN_BODY)})
                r = s.run("Part of #7\n", HEAD_REPO=head)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(s.calls(), [], "no API call with the read-only fork token")
                self.assertIn("skipped: fork pull request", r.summary)
                self.assertIn("skipped: fork pull request", r.stdout)

    def test_same_repo_head_still_labels(self):
        s = Stub(self, {I + "7": issue(OPEN_BODY)})
        s.run("Part of #7\n", HEAD_REPO="popododo0720/vws-iam")
        self.assertEqual(s.writes(), [label_call(7)])

    def test_unreadable_issue_fails(self):
        s = Stub(self, {})
        r = s.run("Part of #77\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(s.writes(), [])

    def test_label_write_failure_fails(self):
        s = Stub(self, {I + "7": issue(OPEN_BODY)}, fail_label=True)
        self.assertNotEqual(s.run("Part of #7\n").returncode, 0)

    def test_only_label_writes(self):
        text = open(POST).read()
        self.assertNotRegex(text, r"/comments|state=closed|issue (close|comment|edit)|pr (close|comment|merge)")
        writes = re.findall(r"gh api -X (\w+) (\S+)", text)
        self.assertEqual({(m, p.strip("\"").split("/")[-1]) for m, p in writes}, {("POST", "labels")})


WF = yaml.safe_load(open(WF_PATH)) if os.path.exists(WF_PATH) else {}


class Workflow(unittest.TestCase):
    def test_trigger_merged_pull_request_only(self):
        on = WF.get("on", WF.get(True))
        self.assertEqual(on, {"pull_request": {"types": ["closed"]}})
        job = WF["jobs"]["ac-pending"]
        self.assertEqual(job["if"], "github.event.pull_request.merged == true")

    def test_permissions_issues_write_only(self):
        self.assertEqual(WF["permissions"], {"contents": "read", "issues": "write"})
        self.assertNotIn("permissions", WF["jobs"]["ac-pending"])

    def test_never_checks_out_pr_code(self):
        text = open(WF_PATH).read()
        self.assertNotIn("pull_request_target", text)
        checkouts = [s for s in WF["jobs"]["ac-pending"]["steps"] if str(s.get("uses", "")).startswith("actions/checkout")]
        self.assertEqual(len(checkouts), 1)
        w = checkouts[0]["with"]
        self.assertEqual(w["ref"], "${{ github.event.repository.default_branch }}")
        self.assertEqual(w["sparse-checkout"].split(), [".github/ac", "scripts/guard-agents.sh"])
        # Cone mode takes directories only; a file pattern needs non-cone mode (vws-deploy#418).
        self.assertIs(w["sparse-checkout-cone-mode"], False)
        self.assertNotRegex(text, r"head\.(sha|ref)|refs/pull")

    def test_head_repo_passed_for_the_fork_skip(self):
        run = next(s for s in WF["jobs"]["ac-pending"]["steps"] if s.get("name") == "Label issues with open AC")
        self.assertEqual(run["env"]["HEAD_REPO"], "${{ github.event.pull_request.head.repo.full_name }}")

    def test_pr_body_via_file_not_shell_interpolation(self):
        text = open(WF_PATH).read()
        self.assertNotIn("${{ github.event.pull_request.body }}", text.replace("PR_BODY: ${{ github.event.pull_request.body }}", ""))
        run = next(s for s in WF["jobs"]["ac-pending"]["steps"] if s.get("name") == "Label issues with open AC")
        self.assertIn("PR_BODY", run["env"])
        self.assertNotIn("${{", run["run"])


if __name__ == "__main__":
    unittest.main()
