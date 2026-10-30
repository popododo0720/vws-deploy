"""rules/superseded.py: Rule Z, close a PR a newer rework or rebuild PR names as its source (vws-deploy#377).

The protocol and purity checks, and the rule's pinned share of the recorded pre-split decide() output.
Its behavior tests live in tests/orchestrate/test_superseded.py.
Run: python3 -m unittest discover -s tests/orchestrate"""
import unittest

from rules.rules_testlib import RuleContract


class Superseded(RuleContract, unittest.TestCase):
    RULE = "superseded"
    # (cases with output, action types, note kinds, state keys, actions, notes) over decide-cases.jsonl:
    # no recorded case names an older PR as its source, so the new rule adds nothing to that output.
    PROFILE = (0, [], [], [], 0, 0)


if __name__ == "__main__":
    unittest.main()
