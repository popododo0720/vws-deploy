"""rules/deepseek.py: Rules B and C per item: failures, infra retries, dependencies, the queue (vws-deploy#501 AC3).

The protocol and purity checks, and the rule's pinned share of the recorded pre-split decide() output.
Its behavior tests live in the tests/orchestrate/test_*.py files that drive orch.decide.
Run: python3 -m unittest discover -s tests/orchestrate"""
import unittest

from rules.rules_testlib import RuleContract


class Deepseek(RuleContract, unittest.TestCase):
    RULE = "deepseek"
    # (cases with output, action types, note kinds, state keys, actions, notes) over decide-cases.jsonl
    PROFILE = (7, ['add_label', 'infra_status', 'swap_label'], ['ci_fail_skipped', 'dep_cycle', 'dep_unmet', 'infra_exhausted', 'infra_wait', 'report_pr_outside_vws', 'rework_skipped'], [], 6, 11)


if __name__ == "__main__":
    unittest.main()
