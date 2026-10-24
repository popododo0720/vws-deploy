"""One set of 'Part of' vectors for the orchestrator's Rule D (PART_OF_RE in orchestrate.py) and the
pr-body / pr-issue checks in scripts/guard-agents.sh (the pr-hygiene job of repo-guard), vws-deploy#397.

A body that pr-hygiene accepts must be one Rule D merges: otherwise a verify-approved PR sits with
`no_part_of` and nothing fails (vws-console-api#368, vws-storage#138, compute#122 and four more on
2026-10-24). On plain visible lines both sides must agree exactly. Where they differ on purpose, the guard
is stricter (text hidden in HTML comments or fences, #0), never looser.

vws-deploy#477 may widen Rule D to owner/repo#N. Then flip the owner/repo vectors here and widen
PR_PART_OF_RE in guard-agents.sh in the same change, so both still accept the same set.
Run: python3 -m unittest discover -s tests/orchestrate"""
import os
import subprocess
import unittest

from orch_testlib import ROOT, orch

GUARD = os.path.join(ROOT, "scripts", "guard-agents.sh")

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
]


def rule_d(body):
    m = orch.PART_OF_RE.search(body)
    return m.group(1) if m else None


def guard(cmd, body):
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


if __name__ == "__main__":
    unittest.main()
