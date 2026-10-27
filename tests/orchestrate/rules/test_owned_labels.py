"""rules/owned_labels.py: the pruned label-ownership map (vws-deploy#501 AC3).

The protocol and purity checks, and the rule's pinned share of the recorded pre-split decide() output.
Its behavior tests live in the tests/orchestrate/test_*.py files that drive orch.decide.
Run: python3 -m unittest discover -s tests/orchestrate"""
import unittest

from orch_testlib import CFG, NOW, item, snapshot
from rules.rules_testlib import BY_NAME, RuleContract, rule_cfg


class OwnedLabels(RuleContract, unittest.TestCase):
    RULE = "owned_labels"
    # (cases with output, action types, note kinds, state keys, actions, notes) over decide-cases.jsonl
    PROFILE = (34, [], [], ['owned_labels'], 0, 0)

    def test_drops_ownership_the_item_no_longer_shows(self):
        it = item("vws-iam", 1, labels=["in-review"])
        snap = snapshot(it, item("vws-iam", 2))
        state = {"owned_labels": {"in-review": ["vws-iam#1", "vws-iam#2", "vws-iam#9"], "blocked": ["vws-iam#1"]}}
        d = BY_NAME["owned_labels"].decide(snap, state, rule_cfg(CFG, set()), NOW)
        self.assertEqual(d["state"]["owned_labels"]["in-review"], ["vws-iam#1"])
        self.assertEqual(d["state"]["owned_labels"]["blocked"], [])
        self.assertEqual(state["owned_labels"]["in-review"], ["vws-iam#1", "vws-iam#2", "vws-iam#9"])
        self.assertEqual((d["actions"], d["notes"]), ([], []))


if __name__ == "__main__":
    unittest.main()
