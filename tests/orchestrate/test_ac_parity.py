"""One set of `AC:` vectors for the pr-body check in scripts/guard-agents.sh (the pr-hygiene job of
repo-guard) and the orchestrator's parser (parse_ac in orchestrate.py), vws-deploy#486.

A PR body lists the acceptance-criteria boxes of its `Part of` issue it covers, one `AC: <n>` line each
(n is the 1-based box index), or one `AC: none - <reason>` line. Both sides read only the visible text
(no HTML comments, no code fences) and must accept exactly the same bodies, so a body pr-hygiene passes
is one the orchestrator can tick boxes from. Run: python3 -m unittest discover -s tests/orchestrate"""
import os
import subprocess
import unittest

from orch_testlib import ROOT, orch

GUARD = os.path.join(ROOT, "scripts", "guard-agents.sh")

# (name, AC part of the body, parse_ac result: (boxes, none reason), or None when both sides reject it)
VECTORS = [
    ("one box", "AC: 2", ([2], "")),
    ("several lines", "AC: 1\nAC: 3", ([1, 3], "")),
    ("unordered and repeated", "AC: 3\nAC: 1\nAC: 3", ([1, 3], "")),
    ("two-digit box", "AC: 12", ([12], "")),
    ("indent and trailing blanks", "  AC: 4 \t", ([4], "")),
    ("no space after the colon", "AC:5", ([5], "")),
    ("CRLF", "AC: 1\r\nAC: 2\r\n", ([1, 2], "")),
    ("among other text", "Some text.\n\nAC: 1\n\n## What changed\n- x", ([1], "")),
    ("none with a reason", "AC: none - docs only", ([], "docs only")),
    ("none, reason keeps inner text", "AC: none - lead step, see #12", ([], "lead step, see #12")),
    ("hidden bad line is ignored", "AC: 1\n<!-- AC: 0 -->", ([1], "")),
    ("bad line in a fence is ignored", "AC: 1\n```\nAC: x\n```", ([1], "")),
    ("no AC line", "nothing here", None),
    ("empty", "", None),
    ("AC: 0", "AC: 0", None),
    ("leading zero", "AC: 01", None),
    ("negative", "AC: -1", None),
    ("not a number", "AC: x", None),
    ("none without a reason", "AC: none", None),
    ("none with a dash only", "AC: none -", None),
    ("none with a blank reason", "AC: none -   ", None),
    ("none without the dash", "AC: none docs only", None),
    ("none with an en dash", "AC: none \u2013 docs only", None),
    ("two boxes on one line", "AC: 1, 3", None),
    ("two boxes with a space", "AC: 1 3", None),
    ("text after the box", "AC: 2 (tests)", None),
    ("one good, one bad line", "AC: 1\nAC: x", None),
    ("none mixed with a box", "AC: 1\nAC: none - docs only", None),
    ("empty AC line", "AC:", None),
    ("list item", "- AC: 1", None),
    ("lower case", "ac: 1", None),
    ("only in an HTML comment", "<!-- AC: 1 -->", None),
    ("only in a multi-line HTML comment", "<!--\nAC: 1\n-->", None),
    ("only in a fence", "```\nAC: 1\n```", None),
    ("only in a tilde fence", "~~~\nAC: 1\n~~~", None),
]


def guard(body):
    return subprocess.run(["bash", GUARD, "pr-body"], env=dict(os.environ, PR_BODY=body), capture_output=True, text=True)


class AcParity(unittest.TestCase):
    def test_guard_and_parser_accept_the_same_bodies(self):
        for name, ac, want in VECTORS:
            body = "Part of #12\n\nAgent: vws-platform\n\n" + ac
            with self.subTest(name):
                got = orch.parse_ac(body)
                if want is None:
                    self.assertTrue(got["error"], f"parse_ac accepts it: {got}")
                else:
                    self.assertEqual((got["boxes"], got["none"], got["error"]), (want[0], want[1], ""), "parse_ac")
                r = guard(body)
                self.assertEqual(r.returncode == 0, want is not None, f"guard-agents.sh pr-body: {r.stderr}")

    def test_guard_names_the_bad_line(self):
        for ac, text in (("AC: x", "AC: x"), ("AC: none", "AC: none"), ("nothing", "AC: <n>")):
            with self.subTest(ac):
                self.assertIn(text, guard("Part of #12\n" + ac).stderr)

    def test_parser_names_the_bad_line(self):
        self.assertIn("AC: 0", orch.parse_ac("AC: 0")["error"])


if __name__ == "__main__":
    unittest.main()
