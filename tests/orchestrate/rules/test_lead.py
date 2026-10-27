"""rules/lead.py: the lead wake (vws-deploy#491) (vws-deploy#501 AC3).

The protocol and purity checks, and the rule's pinned share of the recorded pre-split decide() output.
Its behavior tests live in the tests/orchestrate/test_*.py files that drive orch.decide.
Run: python3 -m unittest discover -s tests/orchestrate"""
import unittest

from rules.rules_testlib import RuleContract


class Lead(RuleContract, unittest.TestCase):
    RULE = "lead"
    # (cases with output, action types, note kinds, state keys, actions, notes) over decide-cases.jsonl
    PROFILE = (4, ['lead_wake'], ['lead_wake_off'], [], 3, 1)


if __name__ == "__main__":
    unittest.main()
