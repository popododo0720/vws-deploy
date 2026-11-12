"""Every third-party `uses:` in .github/workflows/*.yml is pinned to a 40-character commit SHA with the tag in a
trailing comment (vws-deploy#700). A local `./` path is exempt. Runs in scripts/guard.sh R13 on every pull request.

Run: python3 -m unittest discover -s .github/drift
"""
import glob
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)(.*)$")
PINNED = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
TAG_COMMENT = re.compile(r"^\s+#\s*\S")

SHA = "11bd71901bbe5b1630ceea73d27597364c9af683"


def unpinned(text):
    """Return the uses: lines that are neither local nor SHA-pinned with a tag comment."""
    bad = []
    for line in text.splitlines():
        m = USES.match(line)
        if not m or m.group(1).startswith("./"):
            continue
        if not (PINNED.match(m.group(1)) and TAG_COMMENT.match(m.group(2))):
            bad.append(line.strip())
    return bad


class ActionPin(unittest.TestCase):
    def test_workflows_are_pinned(self):
        for path in sorted(glob.glob(os.path.join(ROOT, ".github", "workflows", "*.yml"))):
            with open(path, encoding="utf-8") as f:
                self.assertEqual(unpinned(f.read()), [], os.path.relpath(path, ROOT))

    def test_pinned_fixture_passes(self):
        text = "steps:\n  - uses: actions/checkout@%s # v4.2.2\n  - uses: ./.github/actions/x\n" % SHA
        self.assertEqual(unpinned(text), [])

    def test_unpinned_fixtures_fail(self):
        for ref in (
            "actions/checkout@v4",  # tag
            "actions/checkout@main",  # branch
            "actions/checkout@" + SHA[:39],  # short SHA
            "actions/checkout@" + SHA + "0",  # 41 characters
            "actions/checkout@" + SHA.upper(),  # not a lowercase commit id
            "actions/checkout",  # no ref at all
        ):
            with self.subTest(ref=ref):
                self.assertEqual(len(unpinned("steps:\n  - uses: %s # v4\n" % ref)), 1)

    def test_pin_without_tag_comment_fails(self):
        self.assertEqual(len(unpinned("steps:\n  - uses: actions/checkout@%s\n" % SHA)), 1)

    def test_nested_uses_key_is_checked(self):
        self.assertEqual(len(unpinned("jobs:\n  a:\n    uses: o/r/.github/workflows/x.yml@main\n")), 1)


if __name__ == "__main__":
    unittest.main()
