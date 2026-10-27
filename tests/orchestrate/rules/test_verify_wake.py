"""rules/verify_wake.py: Rule A, the verify wake (vws-deploy#501 AC3).

The protocol and purity checks, and the rule's pinned share of the recorded pre-split decide() output.
Its behavior tests live in the tests/orchestrate/test_*.py files that drive orch.decide.
Run: python3 -m unittest discover -s tests/orchestrate"""
import unittest

from orch_testlib import CFG, NOW, item, snapshot
from rules.rules_testlib import BY_NAME, RuleContract, rule_cfg


class VerifyWake(RuleContract, unittest.TestCase):
    RULE = "verify_wake"
    # (cases with output, action types, note kinds, state keys, actions, notes) over decide-cases.jsonl
    PROFILE = (34, ['wake_verify'], ['needs_verify_failed_head', 'needs_verify_parked', 'wake_off'], ['prev_pending_count'], 6, 5)

    def test_counts_pending_prs_into_state_and_extra(self):
        prs = [item("vws-iam", n, labels=["needs-verify"], is_pr=True) for n in (1, 2)]
        d = BY_NAME["verify_wake"].decide(snapshot(*prs), {}, rule_cfg(CFG, set()), NOW)
        self.assertEqual(d["state"], {"prev_pending_count": 2})
        self.assertEqual(d["extra"], {"pending_count": 2})


if __name__ == "__main__":
    unittest.main()
