"""rules/ac_tick.py: Rule T, the AC tick (vws-deploy#486) (vws-deploy#501 AC3).

The protocol and purity checks, and the rule's pinned share of the recorded pre-split decide() output.
Its behavior tests live in the tests/orchestrate/test_*.py files that drive orch.decide.
Run: python3 -m unittest discover -s tests/orchestrate"""
import unittest

from rules.rules_testlib import RuleContract


class AcTick(RuleContract, unittest.TestCase):
    RULE = "ac_tick"
    # (cases with output, action types, note kinds, state keys, actions, notes) over decide-cases.jsonl
    PROFILE = (34, ['ac_tick'], ['ac_body_edited', 'ac_issue_closed', 'ac_read_error', 'ac_tick_done', 'ac_tick_start', 'ac_unverified'], ['ac_tick_since', 'ac_ticked'], 1, 31)


if __name__ == "__main__":
    unittest.main()
