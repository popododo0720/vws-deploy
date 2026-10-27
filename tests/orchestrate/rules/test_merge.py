"""rules/merge.py: Rule D, serialized merges (vws-deploy#501 AC3).

The protocol and purity checks, and the rule's pinned share of the recorded pre-split decide() output.
Its behavior tests live in the tests/orchestrate/test_*.py files that drive orch.decide.
Run: python3 -m unittest discover -s tests/orchestrate"""
import unittest

from rules.rules_testlib import RuleContract


class Merge(RuleContract, unittest.TestCase):
    RULE = "merge"
    # (cases with output, action types, note kinds, state keys, actions, notes) over decide-cases.jsonl
    PROFILE = (34, ['merge', 'merge_status'], ['merge_check', 'merge_confirmed', 'merge_lock', 'merge_lock_released', 'merge_not_landed', 'merge_red_cleared', 'merge_repo_unreadable'], ['merge_attempts', 'merge_locks'], 4, 63)


if __name__ == "__main__":
    unittest.main()
