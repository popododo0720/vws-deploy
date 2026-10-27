"""rules/rebuild.py: Rule R, the rebuild wake (vws-deploy#501 AC3).

The protocol and purity checks, and the rule's pinned share of the recorded pre-split decide() output.
Its behavior tests live in the tests/orchestrate/test_*.py files that drive orch.decide.
Run: python3 -m unittest discover -s tests/orchestrate"""
import unittest

from rules.rules_testlib import RuleContract


class Rebuild(RuleContract, unittest.TestCase):
    RULE = "rebuild"
    # (cases with output, action types, note kinds, state keys, actions, notes) over decide-cases.jsonl
    PROFILE = (3, ['rebuild_wake'], ['rebuild_wake_off'], [], 2, 1)


if __name__ == "__main__":
    unittest.main()
