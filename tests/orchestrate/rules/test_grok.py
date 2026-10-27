"""rules/grok.py: Rule G, the Grok wake (vws-deploy#501 AC3).

The protocol and purity checks, and the rule's pinned share of the recorded pre-split decide() output.
Its behavior tests live in the tests/orchestrate/test_*.py files that drive orch.decide.
Run: python3 -m unittest discover -s tests/orchestrate"""
import unittest

from rules.rules_testlib import RuleContract


class Grok(RuleContract, unittest.TestCase):
    RULE = "grok"
    # (cases with output, action types, note kinds, state keys, actions, notes) over decide-cases.jsonl
    PROFILE = (34, ['grok_wake'], ['grok_allowlist_invalid', 'grok_in_flight', 'grok_task_done', 'grok_wake_expired', 'grok_wake_off', 'repo_busy'], ['grok_released'], 4, 10)


if __name__ == "__main__":
    unittest.main()
