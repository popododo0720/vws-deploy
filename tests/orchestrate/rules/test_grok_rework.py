"""rules/grok_rework.py: Rule B4, the grok rework wake (vws-deploy#398(a)) (vws-deploy#501 AC3).

The protocol and purity checks, and the rule's pinned share of the recorded pre-split decide() output.
Its behavior tests live in the tests/orchestrate/test_*.py files that drive orch.decide.
Run: python3 -m unittest discover -s tests/orchestrate"""
import unittest

from rules.rules_testlib import RuleContract


class GrokRework(RuleContract, unittest.TestCase):
    RULE = "grok_rework"
    # (cases with output, action types, note kinds, state keys, actions, notes) over decide-cases.jsonl
    PROFILE = (34, ['grok_rework'], ['grok_rework_off', 'verify_fail_unrouted'], ['verify_fail_unrouted'], 1, 3)


if __name__ == "__main__":
    unittest.main()
