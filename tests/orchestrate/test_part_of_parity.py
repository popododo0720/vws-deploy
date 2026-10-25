"""One set of 'Part of' vectors for the orchestrator's Rule D (PART_OF_RE in orchestrate.py) and the
pr-body / pr-issue checks in scripts/guard-agents.sh (the pr-hygiene job of repo-guard), vws-deploy#397.

A body that pr-hygiene accepts must be one Rule D merges: otherwise a verify-approved PR sits with
`no_part_of` and nothing fails (vws-console-api#368, vws-storage#138, compute#122 and four more on
2026-10-24). On plain visible lines both sides must agree exactly. Where they differ on purpose, the guard
is stricter (text hidden in HTML comments or fences, #0), never looser.

The owner/repo form `Part of popododo0720/vws-x#N` is rejected by both (vws-deploy#477). vws-compute#127
still parked on it because pr-hygiene passed there; vws-deploy#504 makes the guard fail closed on any line
that starts with `Part of <owner>/<repo>#N` with a message naming the allowed forms, gives Rule D's
part_of_owner_repo skip the same hint, and adds `guard-agents.sh pr-normalize`, which rewrites the form
before a PR opens. OWNER_REPO runs both sides and the rewrite over one vector set.
Run: python3 -m unittest discover -s tests/orchestrate"""
import os
import subprocess
import unittest

from orch_testlib import FIXTURES, ROOT, orch

GUARD = os.path.join(ROOT, "scripts", "guard-agents.sh")
# The vws-compute#127 body Rule D parked with part_of_owner_repo while pr-hygiene passed (vws-deploy#504).
with open(os.path.join(FIXTURES, "compute-127-body.md"), encoding="utf-8") as f:
    COMPUTE_127_BODY = f.read()
ISSUE_504 = "https://github.com/popododo0720/vws-deploy/issues/504"

# (name, body, Rule D ref or None when neither side accepts the body)
SAME = [
    ("same repo", "Part of #12", "#12"),
    ("vws repo", "Part of vws-deploy#448", "vws-deploy#448"),
    ("later line", "intro\n\nPart of vws-lib#7\n", "vws-lib#7"),
    ("indented, any case", "  part of VWS-IAM#7", "VWS-IAM#7"),
    ("text after the number", "Part of #12 (v2)", "#12"),
    ("two spaces", "Part of  #12", "#12"),
    ("tab", "Part of\t#12", "#12"),
    ("CRLF body", "Part of #12\r\n\r\nAgent: dr eggbot\r\n", "#12"),
    ("first line wins", "Part of #3\nPart of vws-iam#4", "#3"),
    ("owner/repo form (vws-deploy#477)", "Part of popododo0720/vws-deploy#448", None),
    ("other owner", "Part of acme/repo#7", None),
    ("owner/repo, indented", "  Part of popododo0720/vws-lib#7", None),
    ("owner/repo, any case", "part of POPODODO0720/VWS-IAM#7", None),
    ("vws-compute#127 body (vws-deploy#504)", COMPUTE_127_BODY, None),
    ("non-vws repo name", "Part of deploy#7", None),
    ("list item", "- Part of #12", None),
    ("mid-sentence", "This is Part of #12", None),
    ("after a comment on the same line", "<!-- note --> Part of #12", None),
    ("no number", "Part of the gap", None),
    ("TBD", "Part of #TBD", None),
    ("no Part of", "mentions #12 only", None),
    ("empty", "", None),
]

# Rule D accepts these; the guard rejects them on purpose (stricter, never looser).
GUARD_STRICTER = [
    ("only in an HTML comment", "<!--\nPart of #12\n-->"),
    ("only in a fence", "```\nPart of #12\n```"),
    ("hidden line comes before the visible one", "<!--\nPart of #5\n-->\nPart of #12"),
    ("#0", "Part of #0"),
    # vws-deploy#504: any line that starts with the owner/repo form fails, even next to a valid one.
    ("owner/repo line before a valid one", "Part of popododo0720/vws-deploy#448\nPart of #12"),
    ("owner/repo line after a valid one", "Part of #12\nPart of acme/repo#7"),
]

# vws-deploy#504: (name, body, Rule D skip reason, body after guard-agents.sh pr-normalize). Rule D names
# part_of_owner_repo only for this owner's vws-* repos; pr-normalize rewrites only those, so another
# owner's reference stays as written and still fails.
OWNER_REPO = [
    ("vws-compute#127 body", COMPUTE_127_BODY, "part_of_owner_repo",
     COMPUTE_127_BODY.replace("Part of popododo0720/vws-deploy#448", "Part of vws-deploy#448", 1)),
    ("this owner, vws repo", "Part of popododo0720/vws-lib#7", "part_of_owner_repo", "Part of vws-lib#7"),
    ("indented", "  Part of popododo0720/vws-iam#7", "part_of_owner_repo", "  Part of vws-iam#7"),
    ("any case", "part of Popododo0720/vws-iam#7", "part_of_owner_repo", "part of vws-iam#7"),
    ("tab", "Part of\tpopododo0720/vws-iam#7", "part_of_owner_repo", "Part of vws-iam#7"),
    ("CRLF", "Part of popododo0720/vws-iam#7\r\nAgent: x\r\n", "part_of_owner_repo", "Part of vws-iam#7\r\nAgent: x\r\n"),
    ("other owner", "Part of acme/vws-deploy#7", "no_part_of", "Part of acme/vws-deploy#7"),
    ("non-vws repo", "Part of popododo0720/deploy#7", "no_part_of", "Part of popododo0720/deploy#7"),
]


def rule_d(body):
    m = orch.PART_OF_RE.search(body)
    return m.group(1) if m else None


def rule_d_reason(body):
    """The Part of reason merge_reasons gives a body ("" when Rule D reads a reference)."""
    if orch.PART_OF_RE.search(body):
        return ""
    return "part_of_owner_repo" if orch.OWNER_PART_OF_RE.search(body) else "no_part_of"


def guard(cmd, body):
    # pr-body also needs an AC: line (vws-deploy#486, test_ac_parity.py); a valid one is added at the end.
    if cmd == "pr-body":
        body += "\nAC: none - Part of vector"
    return subprocess.run(["bash", GUARD, cmd], env=dict(os.environ, PR_BODY=body), capture_output=True, text=True)


class PartOfParity(unittest.TestCase):
    def test_rule_d_and_guard_accept_the_same_bodies(self):
        for name, body, ref in SAME:
            with self.subTest(name):
                self.assertEqual(rule_d(body), ref, "Rule D (orchestrate.py PART_OF_RE)")
                r = guard("pr-body", body)
                self.assertEqual(r.returncode == 0, ref is not None, f"guard-agents.sh pr-body: {r.stderr}")

    def test_pr_issue_names_the_issue_rule_d_links(self):
        for name, body, ref in SAME:
            if ref is None:
                continue
            with self.subTest(name):
                r = guard("pr-issue", body)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout.strip(), ref)

    def test_guard_is_never_looser_than_rule_d(self):
        for name, body in GUARD_STRICTER:
            with self.subTest(name):
                self.assertIsNotNone(rule_d(body), "Rule D accepts it")
                self.assertNotEqual(guard("pr-body", body).returncode, 0, "the guard rejects it")


def normalize(body, *args):
    """guard-agents.sh pr-normalize, bytes in and out so CR and a missing final newline stay visible."""
    r = subprocess.run(["bash", GUARD, "pr-normalize", *args], env=dict(os.environ, PR_BODY=body), capture_output=True)
    return r.returncode, r.stdout.decode(), r.stderr.decode()


class OwnerRepoPartOf(unittest.TestCase):
    """vws-deploy#504: the guard fails closed on the owner/repo form with the allowed forms and a link, and
    pr-normalize rewrites this owner's form into one both sides read."""

    def test_guard_names_the_allowed_forms_and_the_issue(self):
        for name, body, reason, _ in OWNER_REPO:
            with self.subTest(name):
                self.assertEqual(rule_d_reason(body), reason, "Rule D skips it")
                r = guard("pr-body", body)
                self.assertNotEqual(r.returncode, 0, "the guard rejects it")
                for want in ("owner/repo", "Part of #N", "Part of vws-<repo>#N", ISSUE_504, "pr-normalize"):
                    self.assertIn(want, r.stderr)
                self.assertNotEqual(guard("pr-issue", body).returncode, 0)

    def test_pr_normalize_rewrites_this_owners_form_only(self):
        for name, body, reason, want in OWNER_REPO:
            with self.subTest(name):
                rc, out, err = normalize(body)
                self.assertEqual(rc, 0, err)
                self.assertEqual(out, want)
                fixed = reason == "part_of_owner_repo"
                self.assertEqual(rule_d(out) is not None, fixed, "Rule D reads the rewritten body")
                self.assertEqual(guard("pr-body", out).returncode == 0, fixed, "the guard accepts it")

    def test_pr_normalize_reads_a_file_and_is_idempotent(self):
        path = os.path.join(FIXTURES, "compute-127-body.md")
        rc, once, err = normalize("", path)
        self.assertEqual(rc, 0, err)
        self.assertEqual(once, COMPUTE_127_BODY.replace("popododo0720/", "", 1))
        self.assertEqual(normalize(once)[1], once)

    def test_pr_normalize_leaves_other_text_alone(self):
        for body in ("This is Part of popododo0720/vws-lib#7", "- Part of popododo0720/vws-lib#7",
                     "see popododo0720/vws-lib#7", "Part of #12\n\nbody", ""):
            with self.subTest(body):
                self.assertEqual(normalize(body)[:2], (0, body))


if __name__ == "__main__":
    unittest.main()
