"""rules/ci_fail.py: the CI-failure marker notes (vws-deploy#412) (vws-deploy#501 AC3).

The protocol and purity checks, and the rule's pinned share of the recorded pre-split decide() output.
Its behavior tests live in the tests/orchestrate/test_*.py files that drive orch.decide.
Run: python3 -m unittest discover -s tests/orchestrate"""
import unittest

from rules.rules_testlib import RuleContract


class CiFail(RuleContract, unittest.TestCase):
    RULE = "ci_fail"
    # (cases with output, action types, note kinds, state keys, actions, notes) over decide-cases.jsonl
    PROFILE = (2, [], ['ci_fail_grok_pending', 'ci_fail_untrusted'], [], 0, 2)


if __name__ == "__main__":
    unittest.main()
